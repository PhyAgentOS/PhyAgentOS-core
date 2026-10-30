"""Expose agent-mode management to the Core only where it is supported.

The legacy feature mutates a ``ProvidersManager``'s in-memory modes. A normal
Core session resolves a leaf provider from :class:`ProviderService`, which has
no mode list, no default mode, and no add/remove/update operations. Registering
this tool there would offer model configuration changes that cannot happen, so
the capability is available only when the resolver actually returns a manager
with the required methods.

The resolver is session-scoped for the same reason as the image capability: a
turn's current provider wins over one captured at composition time. Because the
required manager is not known before a session exists, availability is
per-session: ``tool_registrations(session_id)`` returns the tool for a
compatible session and an empty tuple otherwise. With no session id the
capability cannot prove compatibility and registers nothing.

The tool changes only the manager's in-memory modes. It does not persist
``~/.PhyAgentOS/config.json`` and does not invent a mode or model the manager
rejected.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from PhyAgentOS.agent.core.types import (
    ExecutionContext,
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolOutcome,
    ToolRegistration,
    ToolStatus,
)
from PhyAgentOS.tooling.agent_mode import AgentModeTool

__all__ = ["AgentModeCapability", "AGENT_MODE_TOOL"]

_SCHEMA_TOOL = AgentModeTool(provider=None)
AGENT_MODE_TOOL = ToolDefinition(
    name=_SCHEMA_TOOL.name,
    description=_SCHEMA_TOOL.description,
    parameters=copy.deepcopy(_SCHEMA_TOOL.parameters),
)

_REQUIRED_MANAGER_METHODS = (
    "list_models",
    "get_default_mode",
    "set_default_mode",
    "add_mode",
    "remove_mode",
    "update_mode",
)


def _manager_from(value: Any) -> Any | None:
    """Unwrap a selection and accept only a manager with the legacy methods."""
    provider = getattr(value, "provider", None)
    if provider is None:
        provider = value
    if provider is None:
        return None
    if all(callable(getattr(provider, name, None)) for name in _REQUIRED_MANAGER_METHODS):
        return provider
    return None


class AgentModeCapability:
    """Native Core ``agent_mode`` tool, available only for compatible managers."""

    def __init__(self, *, provider_resolver: Callable[[str], Any]) -> None:
        self._provider_resolver = provider_resolver

    @property
    def name(self) -> str:
        return "agent_mode"

    def available(self, session_id: str) -> bool:
        try:
            return _manager_from(self._provider_resolver(session_id)) is not None
        except Exception:
            return False

    def unavailable_reason(self, session_id: str) -> str | None:
        try:
            value = self._provider_resolver(session_id)
        except Exception as exc:
            return (
                "agent_mode is unavailable for this session: provider resolution "
                f"failed ({type(exc).__name__}). Manage providers with `paos provider`."
            )
        manager = _manager_from(value)
        if manager is not None:
            return None
        provider = getattr(value, "provider", value)
        return (
            "agent_mode is unavailable for this session: the current provider "
            f"({type(provider).__name__}) is not a compatible ProvidersManager. "
            "Manage providers with `paos provider`."
        )

    def tool_registrations(self, session_id: str | None = None) -> tuple[ToolRegistration, ...]:
        # No session means no proven manager; exposing the tool would claim
        # operations this runtime cannot perform.
        if session_id is None or not self.available(session_id):
            return ()
        return (
            ToolRegistration(definition=AGENT_MODE_TOOL, handler=self._handle),
        )

    def for_selection(self, selection: Any) -> "_BoundAgentModeCapability":
        """Return a view that can register before a session id is known.

        Composition happens before a turn exists, but it does know the seed
        ``RuntimeSelection``. The bound view gates no-arg registration on that
        selection's provider contract. The handler still resolves the manager
        per session, so a later selection change is not hidden by the seed.
        """
        return _BoundAgentModeCapability(self, selection)

    def prompt_context(self, context: ExecutionContext) -> str | None:
        return None

    async def cancel(
        self, session_id: str, context: ExecutionContext | None
    ) -> None:
        # Mode management has no per-session remote work to stop.
        del session_id, context

    async def aclose(self) -> None:
        return None

    async def _handle(self, call: ToolCall, context: ExecutionContext) -> ToolOutcome:
        try:
            manager = _manager_from(self._provider_resolver(context.session_id))
        except Exception as exc:
            return self._failed(
                call, f"Error: provider resolution failed: {exc}"
            )
        if manager is None:
            reason = self.unavailable_reason(context.session_id)
            return self._failed(call, f"Error: {reason}")
        arguments = call.arguments_dict()
        try:
            text = await AgentModeTool(manager).execute(
                method=arguments["method"],
                mode=arguments.get("mode", "common"),
                model=arguments.get("model"),
                describe=arguments.get("describe"),
            )
        except Exception as exc:
            return self._failed(call, f"Error: agent mode operation failed: {exc}")
        if text.startswith("Error"):
            return self._failed(call, text)
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text=text,
        )

    @staticmethod
    def _failed(call: ToolCall, text: str) -> ToolOutcome:
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.FAILED,
            text=text,
            error=ToolError(
                "agent_mode_failed",
                text.removeprefix("Error: ").strip() or "agent mode operation failed",
            ),
        )


class _BoundAgentModeCapability:
    """Composition-time view gated by one known ``RuntimeSelection``.

    No-auth registration uses the bound selection only as a contract check.
    Calls still go through the parent's per-session resolver, and shutdown is
    a no-op because the canonical capability owns no resources.
    """

    def __init__(self, capability: AgentModeCapability, selection: Any) -> None:
        self._capability = capability
        self._selection = selection

    @property
    def name(self) -> str:
        return self._capability.name

    def available(self, session_id: str) -> bool:
        return self._capability.available(session_id)

    def unavailable_reason(self, session_id: str) -> str | None:
        return self._capability.unavailable_reason(session_id)

    def tool_registrations(self, session_id: str | None = None) -> tuple[ToolRegistration, ...]:
        if session_id is not None:
            return self._capability.tool_registrations(session_id)
        if _manager_from(self._selection) is None:
            return ()
        return (
            ToolRegistration(
                definition=AGENT_MODE_TOOL,
                handler=self._capability._handle,
            ),
        )

    def prompt_context(self, context: ExecutionContext) -> str | None:
        return self._capability.prompt_context(context)

    async def cancel(
        self, session_id: str, context: ExecutionContext | None
    ) -> None:
        # The canonical capability is closed once by the host; this view owns
        # nothing of its own.
        del session_id, context

    async def aclose(self) -> None:
        return None
