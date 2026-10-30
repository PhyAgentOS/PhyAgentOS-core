"""Core agent-mode capability: availability gating and supported operations."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from PhyAgentOS.agent.capabilities.agent_mode_capability import (
    AGENT_MODE_TOOL,
    AgentModeCapability,
)
from PhyAgentOS.agent.core.types import (
    Budget,
    CancellationToken,
    ExecutionContext,
    ToolCall,
    ToolStatus,
)
from PhyAgentOS.agent_legacy.tools.agent import AgentModeTool as LegacyAgentModeTool
from PhyAgentOS.tooling.agent_mode import AgentModeTool


class FakeManager:
    """The subset of ProvidersManager the tool actually calls."""

    def __init__(self) -> None:
        self.modes: dict[str, dict[str, str]] = {
            "main": {"model": "model-main", "describe": "main mode"},
        }
        self.default = "main"
        self.add_calls: list[tuple[str, str, str]] = []
        self.update_calls: list[tuple[str, str]] = []
        self.removed: list[str] = []

    def list_models(self) -> list[dict[str, str]]:
        return [
            {"name": name, "model": value["model"], "describe": value["describe"]}
            for name, value in self.modes.items()
        ]

    def get_default_mode(self) -> str:
        return self.default

    def set_default_mode(self, mode: str) -> None:
        self.default = mode

    def add_mode(self, mode: str, model_id: str, describe: str) -> None:
        self.add_calls.append((mode, model_id, describe))
        self.modes[mode] = {"model": model_id, "describe": describe}

    def remove_mode(self, mode: str) -> None:
        self.removed.append(mode)
        self.modes.pop(mode, None)

    def update_mode(self, mode: str, model_id: str) -> None:
        self.update_calls.append((mode, model_id))
        self.modes[mode]["model"] = model_id


class FailingManager(FakeManager):
    def add_mode(self, mode: str, model_id: str, describe: str) -> None:
        raise ValueError("manager refused the mode")


def _context(session_id: str = "s1") -> ExecutionContext:
    return ExecutionContext(
        session_id=session_id,
        turn_id="turn-1",
        step_id="step-1",
        cancellation=CancellationToken(),
        budget=Budget(max_steps=4),
    )


def _call(method: str, **arguments: Any) -> ToolCall:
    return ToolCall(
        tool_call_id="call-1",
        name="agent_mode",
        arguments={"method": method, **arguments},
    )


def _capability(provider: Any) -> AgentModeCapability:
    return AgentModeCapability(provider_resolver=lambda session_id: provider)


def test_tool_is_not_registered_without_a_session_id() -> None:
    capability = _capability(FakeManager())

    assert capability.tool_registrations() == ()


def test_tool_registers_for_a_compatible_manager_session() -> None:
    manager = FakeManager()
    capability = _capability(SimpleNamespace(provider=manager, model="m", effort=None))

    assert capability.available("s1") is True
    registrations = capability.tool_registrations("s1")

    assert len(registrations) == 1
    definition = registrations[0].definition
    assert definition.name == "agent_mode"
    assert dict(definition.parameters) == AgentModeTool(provider=None).parameters


def test_tool_is_hidden_for_a_normal_leaf_provider() -> None:
    leaf = SimpleNamespace(chat_with_retry=lambda **kwargs: None, model="m")
    capability = _capability(SimpleNamespace(provider=leaf, model="m", effort=None))

    assert capability.available("s1") is False
    assert capability.tool_registrations("s1") == ()
    reason = capability.unavailable_reason("s1")
    assert reason is not None
    assert "ProvidersManager" in reason


async def test_operations_use_the_managers_actual_signatures() -> None:
    manager = FakeManager()
    capability = _capability(manager)
    context = _context()

    listed = await capability._handle(_call("list"), context)
    switched = await capability._handle(_call("switch", mode="main"), context)
    added = await capability._handle(
        _call("add", mode="coding", model="model-code", describe="code"),
        context,
    )
    updated = await capability._handle(
        _call("update", mode="coding", model="model-code-2", describe="ignored"),
        context,
    )
    removed = await capability._handle(_call("remove", mode="coding"), context)

    assert all(
        outcome.status is ToolStatus.SUCCEEDED
        for outcome in (listed, switched, added, updated, removed)
    )
    assert manager.add_calls == [("coding", "model-code", "code")]
    # update_mode takes two arguments; the old three-argument call was broken.
    assert manager.update_calls == [("coding", "model-code-2")]
    assert manager.removed == ["coding"]


async def test_manager_failure_maps_to_failed() -> None:
    capability = _capability(FailingManager())

    outcome = await capability._handle(
        _call("add", mode="coding", model="model-code", describe="code"),
        _context(),
    )

    assert outcome.status is ToolStatus.FAILED
    assert "manager refused" in outcome.text
    assert outcome.error is not None
    assert outcome.error.code == "agent_mode_failed"


async def test_unknown_method_is_failed_not_successful_text() -> None:
    capability = _capability(FakeManager())

    outcome = await capability._handle(_call("bogus"), _context())

    assert outcome.status is ToolStatus.FAILED
    assert outcome.error is not None
    assert outcome.text.startswith("Error: Unknown method")


async def test_incompatible_provider_fails_instead_of_fabricating_changes() -> None:
    leaf = SimpleNamespace(chat_with_retry=lambda **kwargs: None)
    capability = _capability(SimpleNamespace(provider=leaf, model="m", effort=None))

    outcome = await capability._handle(_call("list"), _context())

    assert outcome.status is ToolStatus.FAILED
    assert "not a compatible ProvidersManager" in outcome.text


async def test_cancel_and_close_have_no_session_work() -> None:
    capability = _capability(FakeManager())

    await capability.cancel("s1", None)
    await capability.aclose()

    assert AGENT_MODE_TOOL.name == "agent_mode"


async def test_legacy_shim_uses_the_fixed_update_signature() -> None:
    manager = FakeManager()
    tool = LegacyAgentModeTool(manager)

    result = await tool.execute(
        method="update", mode="main", model="model-main-2", describe="ignored"
    )

    assert result.startswith("✓ Model updated")
    assert manager.update_calls == [("main", "model-main-2")]


async def test_bound_view_registers_from_a_compatible_selection() -> None:
    session_manager = FakeManager()
    session_manager.add_mode("session-only", "model-session", "from resolver")
    capability = _capability(
        SimpleNamespace(provider=session_manager, model="m", effort=None)
    )
    bound = capability.for_selection(
        SimpleNamespace(provider=FakeManager(), model="seed", effort=None)
    )

    registrations = bound.tool_registrations()

    assert len(registrations) == 1
    assert registrations[0].definition.name == "agent_mode"
    outcome = await registrations[0].handler(_call("list"), _context("s1"))
    assert outcome.status is ToolStatus.SUCCEEDED
    # Registration used the seed selection, but the handler used the current
    # session's resolver result.
    assert "session-only" in outcome.text


def test_bound_view_does_not_register_for_a_leaf_selection() -> None:
    leaf = SimpleNamespace(chat_with_retry=lambda **kwargs: None)
    capability = _capability(FakeManager())

    bound = capability.for_selection(
        SimpleNamespace(provider=leaf, model="seed", effort=None)
    )

    assert bound.tool_registrations() == ()
    assert bound.available("s1") is True
