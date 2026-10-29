"""Turn-scoped Skill authorization, independent of experience collection.

Document discovery belongs to SkillDocumentLoader; process lifecycle belongs
to skill_runtime. This manager only authorizes a document for the current
Core turn and supplies the existing Forge activation-manager contract.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from PhyAgentOS.forge.binding import ForgeSkillBindingResolver
from PhyAgentOS.skill_runtime.manifest import load_manifest

from .skill_documents import SkillDocumentLoader


class TurnSkillActivation(BaseModel):
    """Immutable activation identity consumed by Forge binding validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["skill_activation_v2"] = "skill_activation_v2"
    activation_id: str
    skill_name: str
    role: Literal["primary", "supporting"]
    source: Literal["workspace", "installed", "builtin"]
    content_sha256: str
    skill_version: str | None = None
    binding_candidate_id: str | None = None
    activated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class _TurnActivations:
    turn_id: str
    records: dict[str, tuple[TurnSkillActivation, str]] = field(default_factory=dict)


class TurnSkillActivationManager:
    """Keep one current activation scope per serialized Core session.

    begin_turn is idempotent for the same turn; it is called with explicit
    ExecutionContext identity by the capability, never by model arguments.
    An activation id from a previous turn or process is never adopted.
    """

    def __init__(
        self,
        documents: SkillDocumentLoader,
        *,
        binding_resolver: ForgeSkillBindingResolver | None = None,
    ) -> None:
        self.documents = documents
        self.binding_resolver = binding_resolver
        self._sessions: dict[str, _TurnActivations] = {}

    def begin_turn(self, session_key: str, turn_id: str) -> None:
        current = self._sessions.get(session_key)
        if current is None or current.turn_id != turn_id:
            self._sessions[session_key] = _TurnActivations(turn_id)

    async def activate(
        self,
        *,
        session_key: str,
        turn_id: str,
        name: str,
        role: Literal["primary", "supporting"] = "primary",
    ) -> tuple[TurnSkillActivation, str]:
        self.begin_turn(session_key, turn_id)
        scope = self._sessions[session_key]
        if role not in {"primary", "supporting"}:
            raise ValueError("role must be primary or supporting")
        skill = self.documents.resolve_skill(name, require_available=True)
        if skill is None:
            raise ValueError(f"Skill {name!r} is not registered or is unavailable")

        path = Path(skill["path"])
        content = path.read_text(encoding="utf-8")
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        manifest_path = path.parent / "skill.yaml"
        version = load_manifest(manifest_path).version if manifest_path.is_file() else None
        candidate_id = None
        if self.binding_resolver is not None:
            from PhyAgentOS.forge.interaction.binding import verified_extension

            try:
                installed = self.binding_resolver.catalog.get(name)
            except (LookupError, FileNotFoundError):
                installed = None
            if installed is not None and verified_extension(installed) is not None:
                if path.resolve() != (installed.bundle_root / "SKILL.md").resolve():
                    raise ValueError("interaction Skill cannot be shadowed by workspace content")
            if version is not None:
                candidate = await self.binding_resolver.preview(name)
                candidate_id = candidate.candidate_id

        if self._sessions.get(session_key) is not scope:
            raise ValueError("Skill activation turn changed during binding preview")
        # The preview can take a Gateway round trip. Re-read the document so
        # the activation record, the text returned to the model, and the
        # binding hash all describe the same bytes. A change aborts instead of
        # publishing an activation that no longer matches the Skill on disk.
        latest = path.read_text(encoding="utf-8")
        if hashlib.sha256(latest.encode("utf-8")).hexdigest() != content_sha256:
            raise ValueError(
                "Skill document changed during activation; call activate_skill again "
                "in this turn"
            )
        content = latest
        existing = scope.records.get(name)
        if existing is not None:
            if existing[0].role != role:
                raise ValueError(f"Skill {name!r} is already activated as {existing[0].role}")
            if existing[0].content_sha256 != content_sha256:
                raise ValueError("Skill document changed after activation; start a new turn")
            return existing
        if role == "primary" and any(a.role == "primary" for a, _ in scope.records.values()):
            raise ValueError("a primary Skill is already activated in this turn")
        activation = TurnSkillActivation(
            activation_id=f"activation_{uuid4().hex[:16]}",
            skill_name=name,
            role=role,
            source=skill["source"],
            content_sha256=content_sha256,
            skill_version=version,
            binding_candidate_id=candidate_id,
        )
        scope.records[name] = (activation, content)
        return activation, content

    def require_activation(
        self, *, session_key: str, activation_id: str, role: str | None = None
    ) -> TurnSkillActivation:
        scope = self._sessions.get(session_key)
        activation = next(
            (a for a, _ in scope.records.values() if a.activation_id == activation_id),
            None,
        ) if scope else None
        if activation is None:
            raise ValueError(
                "activation does not belong to the current session turn; "
                "call activate_skill in this turn and use its returned activation_id"
            )
        if role is not None and activation.role != role:
            raise ValueError(f"activation must have role {role!r}")
        return activation

    def current(self, session_key: str, turn_id: str) -> tuple[TurnSkillActivation, ...]:
        scope = self._sessions.get(session_key)
        if scope is None or scope.turn_id != turn_id:
            return ()
        return tuple(a for a, _ in scope.records.values())

    def clear(self) -> None:
        self._sessions.clear()
