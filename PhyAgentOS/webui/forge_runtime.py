"""Read-only Forge Skill runtime information for the settings UI.

This module never starts, stops, installs or downloads anything; it only
reads the installed Skill catalog, the persisted runtime state files and
the local Forge node installation root. Registry access is limited to
metadata search (``/v1/skills``), never artifact downloads.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, ContextManager

from PhyAgentOS.config.loader import load_config
from PhyAgentOS.skill_runtime.catalog import SkillCatalog
from PhyAgentOS.skill_runtime.installer import NodeInstaller
from PhyAgentOS.skill_runtime.manifest import ManifestError, SkillManifest
from PhyAgentOS.skill_runtime.registry import RegistryClient, RegistryError
from PhyAgentOS.skill_runtime.state import RuntimeState, RuntimeStateStore, StateError


def registry_search(
    data_dir: Path,
    config_path: Path,
    query: str = "",
    *,
    client_factory: Callable[[str], ContextManager[Any]] | None = None,
) -> dict[str, Any]:
    """Search the configured Resource Registry and compare with local installs.

    Read-only: metadata request plus a local catalog listing. ``PAOS_RESOURCE_REGISTRY_URL``
    keeps its CLI precedence over the instance config's ``resourceRegistry.url``.
    """
    base_url = os.environ.get("PAOS_RESOURCE_REGISTRY_URL", "").strip()
    if not base_url:
        base_url = load_config(config_path).resource_registry.url.strip()
    if not base_url.startswith(("http://", "https://")):
        raise RegistryError(
            "Resource Registry URL is not configured; set PAOS_RESOURCE_REGISTRY_URL "
            "or resourceRegistry.url"
        )
    base_url = base_url.rstrip("/")

    factory = client_factory or (lambda url: RegistryClient(base_url=url))
    with factory(base_url) as source:
        items = source.search_skills(query)

    local = {
        manifest.name: manifest.version
        for manifest in SkillCatalog(data_dir / "skills").list()
    }
    results: list[dict[str, Any]] = []
    for item in items:
        name = str(item.get("name", ""))
        version = str(item.get("version", ""))
        installed = local.get(name)
        results.append(
            {
                "name": name,
                "version": version,
                "description": str(item.get("description", "")),
                "installedVersion": installed,
                "updateAvailable": bool(installed and installed != version),
            }
        )
    return {"sourceUrl": base_url, "items": results}


def forge_runtime_snapshot(data_dir: Path) -> dict[str, Any]:
    """Collect installed Skill info without executing or mutating anything.

    ``data_dir`` is the instance data directory (the config file's parent),
    matching where the CLI installs bundles, state files and Forge nodes.
    """
    data_dir = Path(data_dir)
    catalog = SkillCatalog(data_dir / "skills")
    states = RuntimeStateStore(data_dir / "run" / "skills")
    nodes = NodeInstaller(data_dir / "forge_runtime")

    skills = [_skill_info(manifest, states, nodes) for manifest in catalog.list()]
    invalid = [
        {"name": name, "error": error} for name, error in catalog.errors().items()
    ]
    return {"skills": skills, "invalid": invalid}


def _skill_info(
    manifest: SkillManifest,
    states: RuntimeStateStore,
    nodes: NodeInstaller,
) -> dict[str, Any]:
    state_error: str | None = None
    state: RuntimeState | None = None
    try:
        state = states.load(manifest.name)
    except StateError as exc:
        state_error = str(exc)

    profile_name = state.profile if state is not None else None
    tools = list(manifest.required_tools)
    if profile_name is not None:
        try:
            tools = list(manifest.tools_for_profile(profile_name))
        except ManifestError:
            pass  # stale profile name in state; fall back to the Skill-wide list

    return {
        "name": manifest.name,
        "version": manifest.version,
        "description": manifest.description,
        "profiles": sorted(manifest.profiles),
        "status": state.status if state is not None else "not_started",
        "profile": profile_name,
        "gatewayUrl": state.gateway_url if state is not None else manifest.gateway_url,
        "tools": tools,
        "startedAt": state.started_at if state is not None else None,
        "updatedAt": state.updated_at if state is not None else None,
        "lastError": state.last_error if state is not None else state_error,
        "activeInvocations": len(state.active_invocations) if state is not None else 0,
        "activeSessions": len(state.active_sessions) if state is not None else 0,
        "activeTaskBindings": len(state.active_task_bindings) if state is not None else 0,
        "nodes": [
            {
                "nodeId": node_id,
                "version": lock.version,
                "installed": nodes.installed_light(lock),
            }
            for node_id, lock in sorted(manifest.artifacts.nodes.items())
        ],
    }
