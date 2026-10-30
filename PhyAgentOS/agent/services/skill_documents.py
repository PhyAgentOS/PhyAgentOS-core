"""Skill document loader for agent capabilities."""

import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

import yaml

from PhyAgentOS.config.loader import get_config_path

# Default builtin skills directory (relative to the agent package root)
BUILTIN_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


class SkillDocumentLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.
    """

    def __init__(
        self,
        workspace: Path,
        builtin_skills_dir: Path | None = None,
        installed_skills_dir: Path | None = None,
        runtime_availability_provider: Callable[[str], bool] | None = None,
    ):
        self.workspace = workspace
        self.workspace_skills = workspace / "skills"
        self.installed_skills = (
            installed_skills_dir
            if installed_skills_dir is not None
            else get_config_path().parent / "skills"
        )
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR
        self.runtime_availability_provider = runtime_availability_provider
        # Reading the same SKILL.md 2-4 times per prompt build is the dominant
        # cost. Cache by (mtime_ns, size) so edits are still visible.
        self._content_cache: dict[str, tuple[tuple[int, int], str]] = {}
        self._summary_cache: dict[tuple, str] = {}

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source'.
        """
        skills = []

        # Workspace skills (highest priority)
        if self.workspace_skills.exists():
            for skill_dir in self.workspace_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists():
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "workspace"})

        # Installed bundles override built-ins but not workspace Skills.
        if self.installed_skills.exists():
            for skill_dir in self.installed_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists() and not any(
                        skill["name"] == skill_dir.name for skill in skills
                    ):
                        skills.append(
                            {
                                "name": skill_dir.name,
                                "path": str(skill_file),
                                "source": "installed",
                            }
                        )

        # Built-in skills
        if self.builtin_skills and self.builtin_skills.exists():
            for skill_dir in self.builtin_skills.iterdir():
                if skill_dir.is_dir():
                    skill_file = skill_dir / "SKILL.md"
                    if skill_file.exists() and not any(s["name"] == skill_dir.name for s in skills):
                        skills.append({"name": skill_dir.name, "path": str(skill_file), "source": "builtin"})

        # Filter by requirements
        if filter_unavailable:
            return [
                skill
                for skill in skills
                if self._check_requirements(
                    self._get_skill_meta(skill["name"]), skill_name=skill["name"]
                )
            ]
        return skills

    def resolve_skill(self, name: str, *, require_available: bool = True) -> dict[str, str] | None:
        """Resolve a registered Skill by exact name without accepting arbitrary paths."""
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            return None
        for skill in self.list_skills(filter_unavailable=False):
            if skill["name"] != name:
                continue
            if require_available and not self._check_requirements(
                self._get_skill_meta(name), skill_name=name
            ):
                return None
            return dict(skill)
        return None

    def _read_skill_file(self, path: Path) -> str | None:
        """Read a SKILL.md once per content version."""
        try:
            stat = path.stat()
        except OSError:
            return None
        if not path.is_file():
            return None
        key = (stat.st_mtime_ns, stat.st_size)
        cached = self._content_cache.get(str(path))
        if cached is not None and cached[0] == key:
            return cached[1]
        content = path.read_text(encoding="utf-8")
        self._content_cache[str(path)] = (key, content)
        return content

    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.

        Args:
            name: Skill name (directory name).

        Returns:
            Skill content or None if not found.
        """
        # Check workspace first
        workspace_skill = self.workspace_skills / name / "SKILL.md"
        if workspace_skill.exists():
            return self._read_skill_file(workspace_skill)

        installed_skill = self.installed_skills / name / "SKILL.md"
        if installed_skill.exists():
            return self._read_skill_file(installed_skill)

        # Check built-in
        if self.builtin_skills:
            builtin_skill = self.builtin_skills / name / "SKILL.md"
            if builtin_skill.exists():
                return self._read_skill_file(builtin_skill)

        return None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.

        Returns:
            Formatted skills content.
        """
        parts = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                content = self._strip_frontmatter(content)
                parts.append(f"### Skill: {name}\n\n{content}")

        return "\n\n---\n\n".join(parts) if parts else ""

    def build_skills_summary(self) -> str:
        """
        Build a summary of all skills (name, description, path, availability).

        This is used for progressive loading - the agent can read the full
        skill content using read_file when needed.

        The rendered string is cached against the directory/file mtimes and the
        evaluated availability/missing-requirement tuples, so a changed Skill
        file or a changed environment/runtime answer invalidates it. The
        availability checks themselves still run on every call: they depend on
        the current environment and must not be frozen.

        Returns:
            XML-formatted skills summary.
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        def escape_xml(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        entries = []
        for skill in all_skills:
            meta = self._get_skill_meta(skill["name"])
            available = self._check_requirements(meta, skill_name=skill["name"])
            missing = "" if available else self._get_missing_requirements(
                meta, skill_name=skill["name"]
            )
            entries.append((skill, available, missing))

        key = (
            self._skills_fingerprint(),
            tuple((s["name"], available, missing) for s, available, missing in entries),
        )
        cached = self._summary_cache.get(key)
        if cached is not None:
            return cached
        if len(self._summary_cache) > 8:
            self._summary_cache.clear()

        lines = ["<skills>"]
        for s, available, missing in entries:
            name = escape_xml(s["name"])
            path = s["path"]
            desc = escape_xml(self._get_skill_description(s["name"]))

            lines.append(f"  <skill available=\"{str(available).lower()}\">")
            lines.append(f"    <name>{name}</name>")
            lines.append(f"    <description>{desc}</description>")
            lines.append(f"    <location>{path}</location>")

            # Show missing requirements for unavailable skills
            if missing:
                lines.append(f"    <requires>{escape_xml(missing)}</requires>")

            lines.append("  </skill>")
        lines.append("</skills>")

        summary = "\n".join(lines)
        self._summary_cache[key] = summary
        return summary

    def _skills_fingerprint(self) -> tuple:
        """Directory and SKILL.md identity for cache invalidation."""
        parts: list[tuple] = []
        for root in (self.workspace_skills, self.installed_skills, self.builtin_skills):
            if root is None or not root.exists():
                parts.append((str(root), None))
                continue
            try:
                root_stat = root.stat()
                entries = []
                for skill_dir in sorted(root.iterdir()):
                    if not skill_dir.is_dir():
                        continue
                    skill_file = skill_dir / "SKILL.md"
                    if not skill_file.exists():
                        continue
                    stat = skill_file.stat()
                    entries.append((skill_dir.name, stat.st_mtime_ns, stat.st_size))
                parts.append((str(root), root_stat.st_mtime_ns, tuple(entries)))
            except OSError:
                parts.append((str(root), None))
        return tuple(parts)

    def _get_missing_requirements(
        self, skill_meta: dict, *, skill_name: str | None = None
    ) -> str:
        """Get a description of missing requirements."""
        missing = []
        if not self._metadata_available(skill_meta):
            missing.append("available: false")
        requires = skill_meta.get("requires", {})
        if not isinstance(requires, dict):
            missing.append("invalid requires metadata")
            return ", ".join(missing)
        for b in requires.get("bins", []):
            if not shutil.which(b):
                missing.append(f"CLI: {b}")
        for env in requires.get("env", []):
            if not os.environ.get(env):
                missing.append(f"ENV: {env}")
        for runtime in self._runtime_requirements(requires, skill_name=skill_name):
            if not self._runtime_available(runtime):
                missing.append(f"runtime: {runtime}")
        return ", ".join(missing)

    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        content = self.load_skill(name) or ""
        managed = re.search(
            r"<!-- paos:learned-workflow:start -->[\s\S]*?### Trigger\s+"
            r"([\s\S]*?)(?=\n### |\n<!-- paos:learned-workflow:end -->)",
            content,
        )
        if managed and managed.group(1).strip():
            return " ".join(managed.group(1).split())
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # Fallback to skill name

    def build_evolution_catalog(
        self, activated_names: set[str]
    ) -> list[dict[str, str]]:
        """Return redaction-safe summaries plus managed text for activated Skills only."""
        catalog: list[dict[str, str]] = []
        for skill in self.list_skills(filter_unavailable=False):
            item = {
                "name": skill["name"],
                "source": skill["source"],
                "description": self._get_skill_description(skill["name"]),
            }
            if skill["name"] in activated_names:
                content = self.load_skill(skill["name"]) or ""
                managed = re.search(
                    r"<!-- paos:learned-workflow:start -->[\s\S]*?"
                    r"<!-- paos:learned-workflow:end -->",
                    content,
                )
                if managed:
                    item["managed_workflow"] = managed.group(0)[:12000]
            catalog.append(item)
        return catalog

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content

    def _parse_paos_metadata(self, raw: str | dict) -> dict:
        """Parse skill metadata JSON from frontmatter (supports PhyAgentOS and openclaw keys)."""
        try:
            data = raw if isinstance(raw, dict) else json.loads(raw)
            return data.get("PhyAgentOS", data.get("openclaw", {})) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _check_requirements(
        self, skill_meta: dict, *, skill_name: str | None = None
    ) -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        if not self._metadata_available(skill_meta):
            return False
        requires = skill_meta.get("requires", {})
        if not isinstance(requires, dict):
            return False
        for b in requires.get("bins", []):
            if not shutil.which(b):
                return False
        for env in requires.get("env", []):
            if not os.environ.get(env):
                return False
        for runtime in self._runtime_requirements(requires, skill_name=skill_name):
            if not self._runtime_available(runtime):
                return False
        return True

    @staticmethod
    def _runtime_requirements(
        requires: dict, *, skill_name: str | None
    ) -> list[str]:
        value = requires.get("runtime", [])
        if value is True and skill_name:
            return [skill_name]
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]

    def _runtime_available(self, name: str) -> bool:
        return bool(
            self.runtime_availability_provider
            and self.runtime_availability_provider(name)
        )

    def get_active_skills(self) -> list[str]:
        """Return available Skills whose metadata requires an active runtime."""
        result: list[str] = []
        for skill in self.list_skills(filter_unavailable=True):
            meta = self._get_skill_meta(skill["name"])
            requires = meta.get("requires", {})
            if isinstance(requires, dict) and self._runtime_requirements(
                requires, skill_name=skill["name"]
            ):
                result.append(skill["name"])
        return result

    @staticmethod
    def _metadata_available(skill_meta: dict) -> bool:
        """Return false only for an explicit available=false metadata marker."""
        available = skill_meta.get("available")
        if isinstance(available, str):
            return available.strip().lower() not in {"false", "0", "no", "off"}
        return available is not False

    def _get_skill_meta(self, name: str) -> dict:
        """Get PhyAgentOS metadata for a skill (cached in frontmatter)."""
        meta = self.get_skill_metadata(name) or {}
        return self._parse_paos_metadata(meta.get("metadata", ""))

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        result = []
        for s in self.list_skills(filter_unavailable=True):
            meta = self.get_skill_metadata(s["name"]) or {}
            skill_meta = self._parse_paos_metadata(meta.get("metadata", ""))
            if skill_meta.get("always") or meta.get("always"):
                result.append(s["name"])
        return result

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.

        Args:
            name: Skill name.

        Returns:
            Metadata dict or None.
        """
        content = self.load_skill(name)
        if not content:
            return None

        if content.startswith("---"):
            match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if match:
                try:
                    metadata = yaml.safe_load(match.group(1))
                except yaml.YAMLError:
                    return None
                return metadata if isinstance(metadata, dict) else None

        return None


# Keep the original public class name for callers migrating from agent_legacy.skills.
SkillsLoader = SkillDocumentLoader
