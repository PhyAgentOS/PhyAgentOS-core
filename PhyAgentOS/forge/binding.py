"""Immutable binding between an installed Forge Skill runtime and an AgentTask."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from PhyAgentOS.skill_runtime.catalog import SkillCatalog
from PhyAgentOS.verification.contracts import utc_now


class ForgeSkillBindingError(RuntimeError):
    """A Skill cannot be bound to the current live Forge runtime."""


class BindingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BoundToolSpec(BindingModel):
    tool_id: str = Field(min_length=1)
    semantics: Literal["query", "action", "session"]
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ready_at_binding: bool


class ForgeSkillBindingCandidate(BindingModel):
    candidate_id: str
    skill_name: str
    skill_version: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_profile: str
    runtime_instance_id: str
    gateway_url: str
    gateway_identity: str | None = None
    required_tools: tuple[BoundToolSpec, ...]
    node_artifact_ids: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)


class ForgeSkillBinding(BindingModel):
    version: Literal["forge_skill_binding_v1"] = "forge_skill_binding_v1"
    binding_id: str
    skill_name: str
    skill_version: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_profile: str
    runtime_instance_id: str
    gateway_url: str
    gateway_identity: str | None = None
    required_tools: tuple[BoundToolSpec, ...]
    node_artifact_ids: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)

    def tool(self, tool_id: str) -> BoundToolSpec | None:
        return next((item for item in self.required_tools if item.tool_id == tool_id), None)


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _response_data(response: dict[str, Any], label: str) -> dict[str, Any]:
    data = response.get("data")
    if not isinstance(data, dict):
        raise ForgeSkillBindingError(f"Gateway {label} response omitted object data")
    return data


def _candidate_payload(candidate: ForgeSkillBindingCandidate) -> dict[str, Any]:
    value = candidate.model_dump(mode="json", exclude={"candidate_id", "created_at"})
    value["required_tools"] = sorted(value["required_tools"], key=lambda item: item["tool_id"])
    return value


class ForgeSkillBindingResolver:
    """Preview, freeze, and continuously validate one active Skill runtime."""

    def __init__(self, runtime_registry: Any, *, catalog: SkillCatalog | None = None) -> None:
        self.runtime_registry = runtime_registry
        self.catalog = catalog or SkillCatalog()
        self._candidates: dict[str, ForgeSkillBindingCandidate] = {}
        self._lock = threading.RLock()

    def _runtime(self) -> Any:
        runtime = self.runtime_registry.current()
        if runtime is None:
            raise ForgeSkillBindingError("no ready Forge Skill runtime is active")
        return runtime

    async def preview(self, skill_name: str) -> ForgeSkillBindingCandidate:
        runtime = self._runtime()
        if runtime.skill_name != skill_name:
            raise ForgeSkillBindingError(
                f"active Forge runtime is {runtime.skill_name!r}, not {skill_name!r}"
            )
        manifest = self.catalog.get(skill_name)
        if manifest.version != runtime.skill_version:
            raise ForgeSkillBindingError("installed Skill changed after Runtime startup")
        tools: list[BoundToolSpec] = []
        for tool_id in sorted(manifest.tools_for_profile(runtime.profile)):
            response = await runtime.client.get_tool(tool_id)
            spec = _response_data(response, f"ToolSpec {tool_id!r}")
            semantics = spec.get("semantics")
            if semantics not in {"query", "action", "session"}:
                raise ForgeSkillBindingError(
                    f"Forge Tool {tool_id!r} has unsupported semantics {semantics!r}"
                )
            context = _response_data(
                await runtime.client.get_tool_context(tool_id),
                f"Tool context {tool_id!r}",
            )
            ready = context.get("ready") is True and context.get("binding_error") is None
            if not ready:
                raise ForgeSkillBindingError(f"Forge Tool {tool_id!r} is not ready")
            tools.append(
                BoundToolSpec(
                    tool_id=tool_id,
                    semantics=semantics,
                    spec_sha256=canonical_sha256(spec),
                    ready_at_binding=True,
                )
            )
        manifest_path = manifest.bundle_root / "skill.yaml"
        document_path = manifest.resolve_bundle_path(manifest.skill_document)
        seed = {
            "skill": skill_name,
            "runtime": runtime.runtime_instance_id,
            "tools": [item.model_dump(mode="json") for item in tools],
        }
        candidate = ForgeSkillBindingCandidate(
            candidate_id=f"candidate_{canonical_sha256(seed)[:20]}",
            skill_name=skill_name,
            skill_version=manifest.version,
            manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            skill_document_sha256=hashlib.sha256(document_path.read_bytes()).hexdigest(),
            runtime_profile=runtime.profile,
            runtime_instance_id=runtime.runtime_instance_id,
            gateway_url=runtime.gateway_url,
            gateway_identity=runtime.gateway_identity,
            required_tools=tuple(tools),
            node_artifact_ids=tuple(
                sorted(lock.artifact_id for lock in manifest.artifacts.nodes.values())
            ),
        )
        with self._lock:
            self._candidates[candidate.candidate_id] = candidate
        return candidate

    async def freeze(self, candidate_id: str, *, task_id: str) -> ForgeSkillBinding:
        with self._lock:
            candidate = self._candidates.get(candidate_id)
        if candidate is None:
            raise ForgeSkillBindingError("activation binding candidate is missing or expired")
        current = await self.preview(candidate.skill_name)
        if _candidate_payload(current) != _candidate_payload(candidate):
            raise ForgeSkillBindingError(
                "Skill Runtime or ToolSpec changed after activation; activate the Skill again"
            )
        binding_payload = _candidate_payload(current)
        binding_payload["task_id"] = task_id
        return ForgeSkillBinding(
            binding_id=f"binding_{canonical_sha256(binding_payload)[:24]}",
            **current.model_dump(exclude={"candidate_id"}),
        )

    async def validate_tool(
        self,
        binding: ForgeSkillBinding,
        tool_id: str,
        semantics: Literal["query", "action", "session"],
    ) -> BoundToolSpec:
        runtime = self._runtime()
        if (
            runtime.runtime_instance_id != binding.runtime_instance_id
            or runtime.gateway_url != binding.gateway_url
            or runtime.skill_name != binding.skill_name
            or runtime.skill_version != binding.skill_version
            or runtime.profile != binding.runtime_profile
            or runtime.gateway_identity != binding.gateway_identity
        ):
            raise ForgeSkillBindingError("AgentTask Forge runtime binding is no longer active")
        bound = binding.tool(tool_id)
        if bound is None:
            raise ForgeSkillBindingError(f"Forge Tool {tool_id!r} is not in the Skill allowlist")
        if bound.semantics != semantics:
            raise ForgeSkillBindingError(
                f"Forge Tool {tool_id!r} is {bound.semantics}, not {semantics}"
            )
        spec = _response_data(await runtime.client.get_tool(tool_id), f"ToolSpec {tool_id!r}")
        if canonical_sha256(spec) != bound.spec_sha256:
            raise ForgeSkillBindingError(
                f"Forge ToolSpec {tool_id!r} changed after AgentTask binding"
            )
        context = _response_data(
            await runtime.client.get_tool_context(tool_id), f"Tool context {tool_id!r}"
        )
        if context.get("ready") is not True or context.get("binding_error") is not None:
            raise ForgeSkillBindingError(f"Forge Tool {tool_id!r} is no longer ready")
        return bound


__all__ = [
    "BoundToolSpec",
    "ForgeSkillBinding",
    "ForgeSkillBindingCandidate",
    "ForgeSkillBindingError",
    "ForgeSkillBindingResolver",
    "canonical_sha256",
]
