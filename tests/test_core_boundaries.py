"""Static decoupling and interruption-recovery acceptance for the agent core.

The other ``test_core_*`` modules pin each component on its own. This file
only keeps the assertions that need more than one component:

* the real source of ``PhyAgentOS.agent.core`` imports no legacy runtime, no
  Forge/verification/skill-runtime module, no entry layer, no provider SDK and
  no ``litellm`` (checked with ``ast``, so docstring prose is not mistaken for
  an import);
* a real SQLite store survived by a writer that never closed reports its
  unfinished model/tool calls honestly and keeps the request snapshot
  readable;
* a real ``AgentRuntime`` closes a crashed turn before it opens the next one
  and never replays the old model or tool call;
* ``turn.completed`` without ``turn.started`` is not a complete turn;
* the seven runtime components work with zero capabilities, so an
  installation without Forge can still run an ordinary conversation.

Every test writes to ``tmp_path`` where persistence is involved and bounds its
coroutine with ``asyncio.timeout``.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import PhyAgentOS.agent.core as core_package
from PhyAgentOS.agent.core.events import CallKind, EventType, RecoveryReport, RuntimeEvent
from PhyAgentOS.agent.core.host import AgentHostImpl
from PhyAgentOS.agent.core.loop import LoopDriverImpl
from PhyAgentOS.agent.core.prompt import PromptAssemblerImpl
from PhyAgentOS.agent.core.runtime import AgentRuntime
from PhyAgentOS.agent.core.session import SQLiteSessionStore
from PhyAgentOS.agent.core.tools import ToolExecutorImpl
from PhyAgentOS.agent.core.types import (
    DomainRefs,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolDefinition,
    ToolOutcome,
    ToolRegistration,
    ToolStatus,
    TurnRequest,
    TurnResult,
    TurnStart,
    TurnStatus,
)

CORE_DIR = Path(core_package.__file__).parent
SESSION = "telegram:42"

# Anything the core must not need at import time. Prefix matching keeps
# ``PhyAgentOS.forge.task`` and ``litellm.types`` covered by one entry.
FORBIDDEN_IMPORT_ROOTS = (
    "PhyAgentOS.agent.loop",
    "PhyAgentOS.agent.legacy",
    "PhyAgentOS.agent.agent_legacy",
    "PhyAgentOS.forge",
    "PhyAgentOS.verification",
    "PhyAgentOS.skill_runtime",
    "PhyAgentOS.cli",
    "PhyAgentOS.channels",
    "PhyAgentOS.providers",
    "agent_legacy",
    "litellm",
    "openai",
    "anthropic",
    "google.generativeai",
)


def _imported_modules(source: str) -> set[str]:
    """Return imported module names from real ``import`` statements only.

    Relative imports are returned with their leading dots, so ``from .types
    import X`` becomes ``".types"``. Text inside strings and docstrings is
    never inspected.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    return names


def _is_forbidden(module: str) -> bool:
    return any(
        module == root or module.startswith(root + ".")
        for root in FORBIDDEN_IMPORT_ROOTS
    )


def _echo_definition() -> ToolDefinition:
    return ToolDefinition(
        name="echo",
        description="Echo one string.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )


async def _seed_crashed_turn(
    store: SQLiteSessionStore,
    *,
    session: str = SESSION,
    turn_id: str = "turn-crashed",
    model_call_id: str = "model-crashed",
) -> tuple[list[dict[str, str]], list[dict[str, object]]]:
    """Write the log a killed process leaves behind for one active turn.

    The turn has an accepted input, a durable request snapshot, a model
    attempt that never finished, one tool call that only reached ``intent``
    and one that reached ``dispatched``. No terminal event exists.
    """
    messages = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "put the cup on the shelf"},
    ]
    tools = [_echo_definition().to_provider_dict()]
    step = f"{turn_id}:step:1"
    events = [
        RuntimeEvent(
            type=EventType.INPUT_ACCEPTED,
            session_id=session,
            payload={"characters": len(messages[1]["content"])},
        ),
        RuntimeEvent(
            type=EventType.TURN_STARTED,
            session_id=session,
            turn_id=turn_id,
            payload={"max_steps": 4},
        ),
        RuntimeEvent(
            type=EventType.REQUEST_PREPARED,
            session_id=session,
            turn_id=turn_id,
            step_id=step,
            payload={
                "model_call_id": model_call_id,
                "messages": messages,
                "tools": tools,
                "model": "fake-model",
                "temperature": 0.0,
                "max_tokens": 64,
            },
        ),
        RuntimeEvent(
            type=EventType.MODEL_ATTEMPT_STARTED,
            session_id=session,
            turn_id=turn_id,
            step_id=step,
            payload={"model_call_id": model_call_id},
        ),
        RuntimeEvent(
            type=EventType.TOOL_INTENT,
            session_id=session,
            turn_id=turn_id,
            step_id=step,
            payload={"tool_call_id": "call-intent", "tool_name": "echo"},
        ),
        RuntimeEvent(
            type=EventType.TOOL_INTENT,
            session_id=session,
            turn_id=turn_id,
            step_id=step,
            payload={"tool_call_id": "call-dispatched", "tool_name": "echo"},
        ),
        RuntimeEvent(
            type=EventType.TOOL_DISPATCHED,
            session_id=session,
            turn_id=turn_id,
            step_id=step,
            payload={"tool_call_id": "call-dispatched", "tool_name": "echo"},
        ),
    ]
    for event in events:
        await store.append(event)
    return messages, tools


class _ScriptedAdapter:
    """Minimal model adapter: replay canned responses and record requests."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self._responses.pop(0)


class _EchoHandler:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def __call__(self, call: ToolCall, context) -> ToolOutcome:
        self.calls.append(call)
        return ToolOutcome(
            tool_call_id=call.tool_call_id,
            tool_name=call.name,
            status=ToolStatus.SUCCEEDED,
            text=str(call.arguments_dict()["text"]),
        )


class _RecordingDriver:
    """Minimal driver for a new turn; it never touches the crashed turn."""

    def __init__(self, reply: str = "second turn reply") -> None:
        self.reply = reply
        self.runs: list[TurnStart] = []

    async def run(self, turn: TurnStart) -> TurnResult:
        self.runs.append(turn)
        return TurnResult(
            session_id=turn.request.session_id,
            turn_id=turn.turn_id,
            status=TurnStatus.COMPLETED,
            reply=self.reply,
        )


def test_core_sources_import_only_std_library_jsonschema_and_themselves() -> None:
    """Scan real imports with ``ast``; docstring prose never counts."""
    # Self-check: a source whose docstring names every forbidden area still
    # yields only its actual imports.
    decoy = '''"""No forge, verification, skill_runtime, cli, channels, litellm or providers."""

from __future__ import annotations

import jsonschema

from .types import ToolStatus
'''
    assert _imported_modules(decoy) == {"__future__", "jsonschema", ".types"}
    assert not any(_is_forbidden(name) for name in _imported_modules(decoy))

    paths = sorted(CORE_DIR.glob("*.py"))
    assert {path.name for path in paths} >= {
        "events.py",
        "host.py",
        "loop.py",
        "model.py",
        "prompt.py",
        "protocols.py",
        "runtime.py",
        "session.py",
        "tools.py",
        "types.py",
    }

    unexpected: dict[str, list[str]] = {}
    relative_escapes: dict[str, list[str]] = {}
    for path in paths:
        imports = _imported_modules(path.read_text(encoding="utf-8"))
        forbidden = sorted(name for name in imports if _is_forbidden(name))
        if forbidden:
            unexpected[path.name] = forbidden
        escapes = sorted(name for name in imports if name.startswith(".."))
        if escapes:
            relative_escapes[path.name] = escapes

    assert unexpected == {}
    # A relative import above ``core`` would reach the legacy agent package.
    assert relative_escapes == {}
    # The scan really saw the third-party dependency the executor declares.
    assert "jsonschema" in _imported_modules((CORE_DIR / "tools.py").read_text("utf-8"))


async def test_recover_after_unclosed_writer_never_fabricates_success(
    tmp_path: Path,
) -> None:
    """Crash simulation with a second reader on a real file database."""
    db = tmp_path / "sessions.sqlite"
    writer = SQLiteSessionStore(db)
    try:
        async with asyncio.timeout(10):
            messages, tools = await _seed_crashed_turn(writer)
            # Simulated kill: the writer is neither closed nor checkpointed.

            reader = SQLiteSessionStore(db)
            try:
                before = await reader.read_session(SESSION)
                report = await reader.recover(SESSION)
                recovered = await reader.read_session(SESSION)
                again = await reader.recover(SESSION)
                after_second = await reader.read_session(SESSION)
                conversation = await reader.read_conversation(SESSION)
            finally:
                await reader.aclose()
    finally:
        await writer.aclose()

    assert report == RecoveryReport(
        session_id=SESSION,
        unfinished_turns=("turn-crashed",),
        unfinished_calls=report.unfinished_calls,
    )
    assert {(call.kind, call.call_id) for call in report.unfinished_calls} == {
        (CallKind.MODEL, "model-crashed"),
        (CallKind.TOOL, "call-intent"),
        (CallKind.TOOL, "call-dispatched"),
    }
    flags = {call.call_id: call.dispatched for call in report.unfinished_calls}
    assert flags == {
        "model-crashed": False,
        "call-intent": False,
        "call-dispatched": True,
    }

    # The request snapshot survived the crash unchanged and is still readable.
    snapshot = next(event for event in recovered if event.type is EventType.REQUEST_PREPARED)
    assert snapshot.payload["model"] == "fake-model"
    assert snapshot.payload["messages"] == messages
    assert snapshot.payload["tools"] == tools

    settled = {
        event.payload["tool_call_id"]: event.payload
        for event in recovered
        if event.type is EventType.TOOL_SETTLED and event.payload.get("recovered") is True
    }
    assert settled["call-intent"]["status"] == ToolStatus.FAILED.value
    assert settled["call-intent"]["error"]["code"] == "not_dispatched"
    assert "never called" in settled["call-intent"]["error"]["message"]
    assert settled["call-dispatched"]["status"] == ToolStatus.UNKNOWN.value
    assert "error" not in settled["call-dispatched"]

    model_recovery = [
        event.payload
        for event in recovered
        if event.type is EventType.MODEL_ATTEMPT_INTERRUPTED
    ]
    assert model_recovery == [{"model_call_id": "model-crashed", "recovered": True}]

    types = [event.type for event in recovered]
    assert EventType.TURN_COMPLETED not in types
    assert EventType.TURN_FAILED not in types
    assert EventType.TURN_INTERRUPTED not in types  # closing turns belongs to the runtime
    assert ToolStatus.SUCCEEDED.value not in {event.payload.get("status") for event in recovered}
    assert [event.sequence for event in recovered] == list(range(1, len(recovered) + 1))
    assert len(recovered) == len(before) + 3

    # The recovery reports the same unfinished turn again (only the runtime can
    # close it) but appends nothing twice.
    assert again.unfinished_calls == ()
    assert again.unfinished_turns == ("turn-crashed",)
    assert after_second == recovered
    # Nothing about the crashed turn is projected as a usable conversation.
    assert conversation == ()


async def test_runtime_closes_crashed_turn_before_new_turn_and_never_replays(
    tmp_path: Path,
) -> None:
    db = tmp_path / "sessions.sqlite"
    writer = SQLiteSessionStore(db)
    try:
        async with asyncio.timeout(10):
            await _seed_crashed_turn(writer)
            # Simulated kill: writer stays open; the runtime uses a new store.
            store = SQLiteSessionStore(db)
            driver = _RecordingDriver()
            runtime = AgentRuntime(driver=driver, store=store, max_steps=4)
            try:
                result = await runtime.submit(
                    TurnRequest(session_id=SESSION, text="second request")
                )
                old_events = await store.read_turn(SESSION, "turn-crashed")
                all_events = await store.read_session(SESSION)
            finally:
                await runtime.aclose()
    finally:
        await writer.aclose()

    assert result.status is TurnStatus.COMPLETED
    assert result.reply == "second turn reply"
    assert len(driver.runs) == 1
    new_turn = driver.runs[0]
    assert new_turn.turn_id != "turn-crashed"
    # The crashed turn never completed, so it contributes no history and the
    # old request text is not replayed into the new turn.
    assert new_turn.history == ()
    assert new_turn.request.text == "second request"

    old_types = [event.type for event in old_events]
    assert old_types.count(EventType.TURN_INTERRUPTED) == 1
    assert EventType.TURN_COMPLETED not in old_types
    assert old_types.count(EventType.MODEL_ATTEMPT_STARTED) == 1
    assert old_types.count(EventType.MODEL_ATTEMPT_INTERRUPTED) == 1
    assert EventType.MODEL_ATTEMPT_COMPLETED not in old_types
    # No second intent/dispatch: the old calls were not resent.
    assert old_types.count(EventType.TOOL_INTENT) == 2
    assert old_types.count(EventType.TOOL_DISPATCHED) == 1
    assert old_types.count(EventType.TOOL_SETTLED) == 2

    settled = {
        event.payload["tool_call_id"]: event.payload["status"]
        for event in old_events
        if event.type is EventType.TOOL_SETTLED
    }
    assert settled == {
        "call-intent": ToolStatus.FAILED.value,
        "call-dispatched": ToolStatus.UNKNOWN.value,
    }

    interrupted = next(
        event for event in old_events if event.type is EventType.TURN_INTERRUPTED
    )
    assert interrupted.payload["recovered"] is True
    assert interrupted.turn_id == "turn-crashed"

    # Recovery closes the old calls and turn before the new turn begins.
    new_started = next(
        event
        for event in all_events
        if event.type is EventType.TURN_STARTED and event.turn_id == new_turn.turn_id
    )
    new_accepted = [
        event
        for event in all_events
        if event.type is EventType.INPUT_ACCEPTED and event.sequence < new_started.sequence
    ][-1]
    recovered_and_closed = [
        event
        for event in all_events
        if event.type
        in (
            EventType.MODEL_ATTEMPT_INTERRUPTED,
            EventType.TOOL_SETTLED,
            EventType.TURN_INTERRUPTED,
        )
    ]
    assert max(event.sequence for event in recovered_and_closed) < new_accepted.sequence
    assert new_accepted.sequence < new_started.sequence
    assert sum(1 for event in all_events if event.type is EventType.TURN_STARTED) == 2


async def test_turn_completed_without_turn_started_is_not_a_complete_turn(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite")
    try:
        async with asyncio.timeout(10):
            # A lone completion has the right identity but no start: it must
            # not become conversation history.
            await store.append(
                RuntimeEvent(
                    type=EventType.REQUEST_PREPARED,
                    session_id=SESSION,
                    turn_id="turn-ghost",
                    step_id="turn-ghost:step:1",
                    payload={
                        "model_call_id": "model-ghost",
                        "messages": [{"role": "user", "content": "ghost input"}],
                        "tools": [],
                    },
                )
            )
            await store.append(
                RuntimeEvent(
                    type=EventType.TURN_COMPLETED,
                    session_id=SESSION,
                    turn_id="turn-ghost",
                    payload={"reply": "ghost reply"},
                )
            )
            ghost_events = await store.read_turn(SESSION, "turn-ghost")
            ghost_conversation = await store.read_conversation(SESSION)
            ghost_report = await store.recover(SESSION)

            # A started turn with a terminal event is complete and is the only
            # thing the projection may return.
            await store.append(
                RuntimeEvent(
                    type=EventType.TURN_STARTED, session_id=SESSION, turn_id="turn-real"
                )
            )
            await store.append(
                RuntimeEvent(
                    type=EventType.REQUEST_PREPARED,
                    session_id=SESSION,
                    turn_id="turn-real",
                    step_id="turn-real:step:1",
                    payload={
                        "model_call_id": "model-real",
                        "messages": [{"role": "user", "content": "real input"}],
                        "tools": [],
                    },
                )
            )
            await store.append(
                RuntimeEvent(
                    type=EventType.TURN_COMPLETED,
                    session_id=SESSION,
                    turn_id="turn-real",
                    payload={"reply": "real reply"},
                )
            )
            conversation = await store.read_conversation(SESSION)
    finally:
        await store.aclose()

    ghost_types = {event.type for event in ghost_events}
    assert EventType.TURN_COMPLETED in ghost_types
    assert EventType.TURN_STARTED not in ghost_types
    assert ghost_conversation == ()
    # Nothing was started, so recovery has nothing to close and writes nothing.
    assert ghost_report == RecoveryReport(session_id=SESSION)

    assert list(conversation) == [
        {"role": "user", "content": "real input"},
        {"role": "assistant", "content": "real reply"},
    ]


def test_core_imports_and_a_plain_session_do_not_need_forge(tmp_path: Path) -> None:
    """Import the seven components in a clean interpreter, then run a turn.

    No Forge capability is registered anywhere; a local echo tool is the only
    tool. If any core module needed Forge, ``litellm`` or a provider SDK at
    import time, the subprocess check would report it.
    """
    repo_root = Path(__file__).resolve().parents[1]
    script = "\n".join(
        (
            "import importlib, json, sys",
            "for name in ('events', 'types', 'protocols', 'session', 'tools',",
            "             'prompt', 'model', 'loop', 'runtime', 'host'):",
            "    importlib.import_module('PhyAgentOS.agent.core.' + name)",
            "roots = ('PhyAgentOS.forge', 'PhyAgentOS.agent.loop',",
            "         'PhyAgentOS.agent.legacy', 'PhyAgentOS.verification',",
            "         'PhyAgentOS.skill_runtime', 'PhyAgentOS.cli',",
            "         'PhyAgentOS.channels', 'PhyAgentOS.providers',",
            "         'litellm', 'openai', 'anthropic')",
            "print(json.dumps(sorted(m for m in sys.modules",
            "    if any(m == r or m.startswith(r + '.') for r in roots))))",
        )
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(repo_root), env.get("PYTHONPATH", "")) if item
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == []


async def test_plain_conversation_runs_with_zero_capabilities(tmp_path: Path) -> None:
    async with asyncio.timeout(20):
        store = SQLiteSessionStore(tmp_path / "sessions.sqlite")
        definition = _echo_definition()
        handler = _EchoHandler()
        adapter = _ScriptedAdapter(
            [
                ModelResponse(
                    content=None,
                    finish_reason="tool_calls",
                    tool_calls=(
                        ToolCall(
                            tool_call_id="call-1",
                            name="echo",
                            arguments={"text": "hi"},
                        ),
                    ),
                ),
                ModelResponse(content="first done", finish_reason="stop"),
                ModelResponse(content="second done", finish_reason="stop"),
            ]
        )
        runtime = AgentRuntime(
            driver=LoopDriverImpl(
                assembler=PromptAssemblerImpl("You are a test."),
                adapter=adapter,
                executor=ToolExecutorImpl(
                    [ToolRegistration(definition=definition, handler=handler)], store
                ),
                store=store,
                tools=(definition,),
                model="fake-model",
            ),
            store=store,
            max_steps=4,
        )
        host = AgentHostImpl(runtime=runtime, capabilities=())
        try:
            first = await host.submit(TurnRequest(session_id=SESSION, text="hello"))
            first_history = await store.read_conversation(SESSION)
            second = await host.submit(TurnRequest(session_id=SESSION, text="again"))
        finally:
            await host.aclose()

    assert first.status is TurnStatus.COMPLETED
    assert first.reply == "first done"
    assert second.status is TurnStatus.COMPLETED
    assert second.reply == "second done"
    assert len(handler.calls) == 1

    # The second turn was built from the stored first turn without any
    # capability being present.
    history = list(first_history)
    assert history[0] == {"role": "user", "content": "hello"}
    assert any(message["role"] == "tool" and message["content"] == "hi" for message in history)
    assert history[-1] == {"role": "assistant", "content": "first done"}
    second_request = adapter.requests[2]
    roles = [message["role"] for message in second_request.messages]
    assert roles[0] == "system"
    assert "tool" in roles
    assert {"role": "user", "content": "again"} in list(second_request.messages)


def test_key_event_and_outcome_boundaries_stay_distinct() -> None:
    """One compact check of the edges that recovery relies on."""
    # Identity cannot be dropped from a step- or call-scoped event.
    with pytest.raises(ValueError):
        RuntimeEvent(
            type=EventType.TOOL_INTENT,
            session_id=SESSION,
            turn_id="turn-1",
            payload={"tool_call_id": "call-1"},
        )
    with pytest.raises(ValueError):
        RuntimeEvent(
            type=EventType.MODEL_ATTEMPT_STARTED,
            session_id=SESSION,
            turn_id="turn-1",
            step_id="step-1",
            payload={},
        )

    # A settlement is terminal; ``running`` can never be recorded as one.
    with pytest.raises(ValueError):
        RuntimeEvent(
            type=EventType.TOOL_SETTLED,
            session_id=SESSION,
            turn_id="turn-1",
            step_id="step-1",
            payload={"tool_call_id": "call-1", "status": ToolStatus.RUNNING.value},
        )

    # A domain "accepted" is carried as data with an unknown local result; it
    # has no status of its own and never equals local success.
    accepted = ToolOutcome(
        tool_call_id="call-1",
        tool_name="forge_task",
        status=ToolStatus.UNKNOWN,
        text="accepted, stop unconfirmed",
        content={"domain_status": "accepted"},
        domain_refs=DomainRefs(task_id="task-1", revision_id="rev-1"),
    )
    assert not accepted.is_success
    assert accepted.is_terminal
    assert accepted.for_model().startswith("Tool call result is unknown")
    assert "accepted" not in {status.value for status in ToolStatus}
