"""Dynamic bridge from explicitly managed Skill runtimes into the Agent process."""

from __future__ import annotations

import threading
from collections.abc import Iterator, MutableSet
from dataclasses import dataclass
from typing import Any

from PhyAgentOS.forge.tool_client import ForgeToolClient
from PhyAgentOS.skill_runtime.catalog import SkillCatalog
from PhyAgentOS.skill_runtime.manager import RuntimeManager
from PhyAgentOS.skill_runtime.state import RuntimeStateStore


@dataclass(frozen=True)
class ActiveSkillRuntime:
    """One healthy runtime selected for Agent Tool registration."""

    skill_name: str
    skill_version: str
    profile: str
    runtime_instance_id: str
    gateway_url: str
    gateway_identity: str | None
    client: ForgeToolClient
    invocation_ids: MutableSet[str]
    session_ids: MutableSet[str]
    task_binding_ids: MutableSet[str]


class PersistentRuntimeSet(MutableSet[str]):
    """Persist one set-valued RuntimeState field using atomic state replacement."""

    _fields = {"active_invocations", "active_sessions", "active_task_bindings"}

    def __init__(self, skill_name: str, store: RuntimeStateStore, field_name: str) -> None:
        if field_name not in self._fields:
            raise ValueError(f"unsupported RuntimeState set: {field_name}")
        self.skill_name = skill_name
        self.store = store
        self.field_name = field_name
        self._lock = threading.RLock()

    def _items(self) -> set[str]:
        state = self.store.load(self.skill_name)
        return set(getattr(state, self.field_name) if state is not None else ())

    def __contains__(self, value: object) -> bool:
        return value in self._items()

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._items()))

    def __len__(self) -> int:
        return len(self._items())

    def _replace(self, items: set[str]) -> None:
        state = self.store.load(self.skill_name)
        if state is None:
            raise RuntimeError("Skill runtime state disappeared while updating ownership")
        kwargs = {self.field_name: tuple(sorted(items))}
        self.store.save(state.with_status(state.status, error=state.last_error, **kwargs))

    def add(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise ValueError("runtime-owned identifier must be a non-empty string")
        with self._lock:
            items = self._items()
            items.add(value)
            self._replace(items)

    def discard(self, value: str) -> None:
        with self._lock:
            items = self._items()
            if value not in items:
                return
            items.discard(value)
            self._replace(items)


class PersistentInvocationSet(PersistentRuntimeSet):
    def __init__(self, skill_name: str, store: RuntimeStateStore) -> None:
        super().__init__(skill_name, store, "active_invocations")


class PersistentSessionSet(PersistentRuntimeSet):
    def __init__(self, skill_name: str, store: RuntimeStateStore) -> None:
        super().__init__(skill_name, store, "active_sessions")


class PersistentTaskBindingSet(PersistentRuntimeSet):
    def __init__(self, skill_name: str, store: RuntimeStateStore) -> None:
        super().__init__(skill_name, store, "active_task_bindings")


class ActiveRuntimeRegistry:
    """Thread-safe single-runtime registry used by long-lived Agent components."""

    def __init__(
        self,
        active: ActiveSkillRuntime | None = None,
        *,
        catalog: SkillCatalog | None = None,
        state_store: RuntimeStateStore | None = None,
        auto_refresh: bool = False,
    ) -> None:
        self._active = active
        self._catalog = catalog
        self._state_store = state_store
        self._auto_refresh = auto_refresh
        self._retired_clients: list[ForgeToolClient] = []
        self._lock = threading.RLock()

    def current(self) -> ActiveSkillRuntime | None:
        with self._lock:
            if self._auto_refresh:
                self._refresh_locked()
            return self._active

    def is_available(self, skill_name: str) -> bool:
        active = self.current()
        return active is not None and active.skill_name == skill_name

    def replace(self, active: ActiveSkillRuntime | None) -> ActiveSkillRuntime | None:
        with self._lock:
            previous = self._active
            self._active = active
            if previous is not None and (
                active is None or previous.client is not active.client
            ):
                self._retired_clients.append(previous.client)
            return previous

    def _refresh_locked(self) -> None:
        """Atomically follow persistent Runtime switches made by the user-facing CLI."""
        if self._catalog is None or self._state_store is None:
            return
        running: list[tuple[Any, Any]] = []
        for manifest in self._catalog.list():
            state = self._state_store.load(manifest.name)
            if state is not None and state.status == "running":
                running.append((manifest, state))
        if len(running) > 1:
            names = ", ".join(sorted(manifest.name for manifest, _ in running))
            raise RuntimeError(
                f"Multiple Skill runtimes are active ({names}); stop all but one"
            )
        if not running:
            if self._active is not None:
                self._retired_clients.append(self._active.client)
                self._active = None
            return
        manifest, state = running[0]
        if self._active is not None and (
            self._active.skill_name == manifest.name
            and self._active.skill_version == manifest.version
            and self._active.profile == state.profile
            and self._active.runtime_instance_id == state.runtime_instance_id
            and self._active.gateway_identity == state.gateway_identity
        ):
            return
        if self._active is not None:
            self._retired_clients.append(self._active.client)
        self._active = _make_active_runtime(manifest, state, self._state_store)

    def clients_for_close(self) -> list[ForgeToolClient]:
        """Drain active and replaced clients for Agent shutdown."""
        with self._lock:
            clients = list(self._retired_clients)
            self._retired_clients.clear()
            if self._active is not None:
                clients.append(self._active.client)
            unique: dict[int, ForgeToolClient] = {id(client): client for client in clients}
            return list(unique.values())


class DynamicForgeToolClient:
    """ForgeToolClient-compatible proxy that follows the active runtime atomically."""

    def __init__(self, registry: ActiveRuntimeRegistry) -> None:
        self.registry = registry

    @property
    def base_url(self) -> str:
        return self._client().base_url

    def _client(self) -> ForgeToolClient:
        active = self.registry.current()
        if active is None:
            raise RuntimeError("no ready Forge Skill runtime is active")
        return active.client

    async def close(self) -> None:
        for client in self.registry.clients_for_close():
            await client.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client(), name)


class DynamicRuntimeSet(MutableSet[str]):
    """MutableSet proxy that follows one field of the active runtime."""

    def __init__(self, registry: ActiveRuntimeRegistry, field_name: str) -> None:
        if field_name not in {"invocation_ids", "session_ids", "task_binding_ids"}:
            raise ValueError("unsupported active runtime set")
        self.registry = registry
        self.field_name = field_name

    def _target(self) -> MutableSet[str]:
        active = self.registry.current()
        if active is None:
            raise RuntimeError("no ready Forge Skill runtime is active")
        return getattr(active, self.field_name)

    def __contains__(self, value: object) -> bool:
        active = self.registry.current()
        return False if active is None else value in getattr(active, self.field_name)

    def __iter__(self) -> Iterator[str]:
        active = self.registry.current()
        return iter(()) if active is None else iter(getattr(active, self.field_name))

    def __len__(self) -> int:
        active = self.registry.current()
        return 0 if active is None else len(getattr(active, self.field_name))

    def add(self, value: str) -> None:
        self._target().add(value)

    def discard(self, value: str) -> None:
        active = self.registry.current()
        if active is not None:
            getattr(active, self.field_name).discard(value)


def discover_active_runtime(
    *,
    catalog: SkillCatalog | None = None,
    state_store: RuntimeStateStore | None = None,
    manager: RuntimeManager | None = None,
) -> ActiveSkillRuntime | None:
    """Return the single healthy explicitly started runtime, if one exists."""
    catalog = catalog or SkillCatalog()
    state_store = state_store or RuntimeStateStore()
    manager = manager or RuntimeManager(catalog=catalog, state_store=state_store)
    active = []
    for manifest in catalog.list():
        try:
            report = manager.status(manifest.name)
        except Exception:
            continue
        if report.ready and report.state is not None:
            active.append((manifest, report.state))
    if not active:
        return None
    if len(active) > 1:
        names = ", ".join(sorted(manifest.name for manifest, _ in active))
        raise RuntimeError(
            f"Multiple Skill runtimes are active ({names}); stop all but one before starting PAOS"
        )
    manifest, state = active[0]
    return _make_active_runtime(manifest, state, state_store)


def _make_active_runtime(manifest: Any, state: Any, state_store: RuntimeStateStore) -> ActiveSkillRuntime:
    return ActiveSkillRuntime(
        skill_name=manifest.name,
        skill_version=manifest.version,
        profile=state.profile,
        runtime_instance_id=state.runtime_instance_id,
        gateway_url=manifest.gateway_url,
        gateway_identity=state.gateway_identity,
        client=ForgeToolClient(manifest.gateway_url),
        invocation_ids=PersistentInvocationSet(manifest.name, state_store),
        session_ids=PersistentSessionSet(manifest.name, state_store),
        task_binding_ids=PersistentTaskBindingSet(manifest.name, state_store),
    )


class SkillRuntimeController:
    """Start/switch a runtime and publish it only after readiness reconciliation."""

    def __init__(
        self,
        registry: ActiveRuntimeRegistry,
        *,
        manager: RuntimeManager | None = None,
        catalog: SkillCatalog | None = None,
        state_store: RuntimeStateStore | None = None,
        task_store: Any | None = None,
    ) -> None:
        self.catalog = catalog or SkillCatalog()
        self.state_store = state_store or RuntimeStateStore()
        self.manager = manager or RuntimeManager(
            catalog=self.catalog, state_store=self.state_store
        )
        self.registry = registry
        self.task_store = task_store
        self._lock = threading.RLock()

    def switch(self, skill_name: str, profile: str) -> ActiveSkillRuntime:
        with self._lock:
            if self.task_store is not None and self.task_store.active_tasks():
                raise RuntimeError("cannot switch Forge runtime while an AgentTask is non-terminal")
            current = self.registry.current()
            manifest = self.catalog.get(skill_name)
            if (
                current is not None
                and current.skill_name == skill_name
                and current.profile == profile
            ):
                return current
            stop_first = current is not None and (
                current.skill_name == skill_name
                or current.gateway_url == manifest.gateway_url
            )
            if stop_first and current is not None:
                self.manager.stop(current.skill_name)
            target_started = False
            try:
                self.manager.start(skill_name, profile)
                target_started = True
                report = self.manager.status(skill_name)
                if not report.ready or report.state is None:
                    raise RuntimeError("Forge Skill runtime did not become ready")
            except Exception as target_error:
                cleanup_error: Exception | None = None
                if target_started:
                    try:
                        self.manager.stop(skill_name, force=True)
                    except Exception as error:
                        cleanup_error = error
                if stop_first and current is not None:
                    try:
                        self.manager.start(current.skill_name, current.profile)
                        restored_manifest = self.catalog.get(current.skill_name)
                        restored_report = self.manager.status(current.skill_name)
                        if not restored_report.ready or restored_report.state is None:
                            raise RuntimeError("previous Forge Skill Runtime did not recover")
                        self.registry.replace(
                            _make_active_runtime(
                                restored_manifest,
                                restored_report.state,
                                self.state_store,
                            )
                        )
                    except Exception as rollback_error:
                        self.registry.replace(None)
                        detail = (
                            f"; target cleanup also failed: {cleanup_error}"
                            if cleanup_error is not None
                            else ""
                        )
                        raise RuntimeError(
                            "target Forge Runtime failed and previous Runtime rollback failed: "
                            f"{rollback_error}{detail}"
                        ) from target_error
                if cleanup_error is not None:
                    raise RuntimeError(
                        "target Forge Runtime failed and its partial Runtime could not be "
                        f"stopped: {cleanup_error}"
                    ) from target_error
                raise
            if current is not None and not stop_first:
                try:
                    self.manager.stop(current.skill_name)
                except Exception:
                    self.manager.stop(skill_name, force=True)
                    raise
            selected = _make_active_runtime(manifest, report.state, self.state_store)
            self.registry.replace(selected)
            return selected


__all__ = [
    "ActiveRuntimeRegistry",
    "ActiveSkillRuntime",
    "DynamicForgeToolClient",
    "DynamicRuntimeSet",
    "PersistentInvocationSet",
    "PersistentRuntimeSet",
    "PersistentSessionSet",
    "PersistentTaskBindingSet",
    "SkillRuntimeController",
    "discover_active_runtime",
]
