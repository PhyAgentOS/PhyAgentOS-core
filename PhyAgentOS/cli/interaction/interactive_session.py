"""Interactive input scheduling over an injected, entry-facing session.

The reader stays alive during turns. Ordinary input, model commands and
session switches share one FIFO; local commands (stop, help, tools, correct,
unknown slash commands) bypass it so a running turn never delays them. Every
queued ordinary message carries the session binding that was current when it
was read, so a switch cannot move an already queued message to another
session.

Session switches take effect at their FIFO position, which is a turn boundary.
A message read after a switch command carries the switch's binding; when the
switch executes it fills that binding in place. If the switch fails or is
cancelled, the binding falls back to the session that was active when the
command was read, and the message is never sent to an unrelated session.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from PhyAgentOS.agent.core.types import TurnRequest, TurnResult, require_non_empty

__all__ = ["HELP_TEXT", "InteractiveAgent"]

_EXIT_COMMANDS = frozenset({"exit", "quit", "/exit", "/quit", ":q"})
_MODEL_COMMANDS = frozenset({"/provider", "/model", "/effort", "/status"})
_SESSION_COMMANDS = frozenset({"/new", "/session"})
_COMPACT_COMMANDS = frozenset({"/compact"})
_LOCAL_COMMANDS = (
    frozenset({"/help", "/stop", "/tools", "/correct"})
    | _MODEL_COMMANDS
    | _SESSION_COMMANDS
    | _COMPACT_COMMANDS
)
_COMMAND_SHAPE = re.compile(r"^/[A-Za-z][A-Za-z0-9_-]*$")

HELP_TEXT = """\
Local commands (handled by the CLI; none of them is sent to the model):
  /help            Show this help.
  /tools [on|off]  Show or hide the full tool-call record for this turn.
  /new             Create and switch to a new session.
  /session         List sessions, or pick one on a terminal.
  /session <id>    Load an existing session.
  /compact         Manually summarize older history with the current model.
  /provider ...    Change provider with text arguments (that session).
  /model ...       Change model (menu on a terminal; text otherwise).
  /effort ...      Change reasoning effort (menu on a terminal; text otherwise).
  /status          Show the current session, model and effort.
  /correct         Not supported; no turn is submitted.
  exit | quit      Leave after earlier queued input.

To stop a running turn, press Esc twice within 2 seconds. The first Esc shows a
yellow hint; only the second one requests local cancellation. This is not
confirmation that remote actions stopped. Non-terminal input cannot capture
Esc; use Ctrl+C to interrupt the local turn instead.
Accepted/running is not task success."""


class _Session(Protocol):
    async def submit(self, request: TurnRequest) -> TurnResult: ...

    async def cancel(self, session_id: str) -> None: ...


class _Output(Protocol):
    def info(self, text: str) -> None: ...

    def error(self, text: str) -> None: ...

    def result(self, result: TurnResult) -> None: ...

    def turn_started(self, session_id: str | None = None) -> None: ...

    def turn_finished(self) -> None: ...

    def cancel_requested(self) -> None: ...

    def tool_view(self, argument: str) -> str: ...


@dataclass(slots=True)
class _Binding:
    """A session binding that a queued item carries.

    ``fallback`` is the session active when the switch command that created
    this binding was read. A failed/cancelled switch resolves to it instead of
    sending later input to whichever session happened to run last.
    """

    session_id: str | None
    fallback: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _Queued:
    text: str
    binding: _Binding
    sequence: int | None = None
    kind: str = "message"


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


class InteractiveAgent:
    """Run serial turns while continuously accepting injected async input.

    ``read_input`` takes no arguments and raises ``EOFError`` or
    ``KeyboardInterrupt`` to exit immediately. Exit commands instead wait for
    earlier queued input. Model and session commands are only called between
    turns; local commands are answered by the reader at once, in arrival
    order, so a slow turn never holds them behind ordinary text.
    """

    def __init__(
        self,
        session: _Session,
        *,
        session_id: str,
        output: _Output,
        read_input: Callable[[], Awaitable[str]],
        model_command: Callable[[str], Awaitable[str | None]] | None = None,
        session_command: Callable[[str, _Binding], Awaitable[str | None]] | None = None,
        compact_command: Callable[[str, _Binding], Awaitable[str | None]] | None = None,
        name_session: Callable[[str, str], Awaitable[None]] | None = None,
        on_active_session: Callable[[str | None], None] | None = None,
        on_turn_settled: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._session = session
        self._initial_session_id = require_non_empty("session_id", session_id)
        self._binding = _Binding(self._initial_session_id)
        self._output = output
        self._read_input = read_input
        self._model_command = model_command
        self._session_command = session_command
        self._compact_command = compact_command
        self._name_session = name_session
        self._on_active_session = on_active_session
        self._on_turn_settled = on_turn_settled
        self._pending: asyncio.Queue[_Queued | None] = asyncio.Queue()
        self._active: asyncio.Task[TurnResult] | None = None
        self._active_session_id: str | None = None
        self._command: asyncio.Task[str | None] | None = None
        self._command_kind: str | None = None
        self._cancellation: asyncio.Task[None] | None = None
        self._aborting = False
        self._sequence = 0
        self._stop_requested = False
        self._named: set[str] = set()

    async def run(self) -> None:
        """Read until exit, settling and awaiting every task we create."""
        reader = asyncio.create_task(self._read_inputs())
        try:
            await self._process_inputs()
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            if self._command is not None:
                self._command.cancel()
                await asyncio.gather(self._command, return_exceptions=True)
                self._command = None
            if self._active is not None and not self._active.done():
                await self._request_cancel()
            await self._wait_for_cancel()
            if self._active is not None:
                # The session normally returns its persisted interrupted result.
                # If cancellation failed, still leave no local task behind.
                if not self._active.done():
                    self._active.cancel()
                results = await asyncio.gather(self._active, return_exceptions=True)
                if isinstance(results[0], TurnResult):
                    self._output.result(results[0])
                self._active = None
                self._active_session_id = None
            # No exit path may leave a spinner or tool rows on screen.
            self._output.turn_finished()

    async def _read_inputs(self) -> None:
        try:
            while True:
                text = (await self._read_input()).strip()
                if not text:
                    pass
                else:
                    await self._handle_line(text)
                # Async input implementations may already have buffered lines.
                # Give submit/cancel a chance to run before reading the next one.
                await asyncio.sleep(0)
        except (EOFError, KeyboardInterrupt):
            await self._abort()
        except Exception as exc:
            self._output.error(f"Input failed: {exc}")
            await self._abort()

    def _fallback(self, binding: _Binding) -> str | None:
        return binding.fallback or self._initial_session_id

    def _new_binding(self) -> _Binding:
        fallback = self._binding.session_id or self._binding.fallback
        return _Binding(None, fallback=fallback)

    async def _handle_line(self, text: str) -> None:
        command, _, argument = text.partition(" ")
        command = command.lower()
        if command == "/stop":
            self._output.info(
                "/stop does not cancel anything. While a turn is running press Esc "
                "twice within 2 seconds (terminal only; Ctrl+C on redirected input)."
            )
        elif command == "/help":
            self._output.info(HELP_TEXT)
        elif command == "/tools":
            self._output.info(self._output.tool_view(argument))
        elif command == "/correct":
            self._output.error(
                "/correct is not supported by this CLI; no model turn was submitted."
            )
        elif command in _EXIT_COMMANDS:
            self._pending.put_nowait(_Queued(text, self._binding))
        elif command in _SESSION_COMMANDS:
            # The switch runs at its queue position. Later input is bound to
            # the same placeholder, so it follows the switch instead of being
            # sent to whichever session is active when it is processed.
            binding = self._new_binding()
            self._binding = binding
            self._pending.put_nowait(_Queued(text, binding, kind="session"))
        elif command in _COMPACT_COMMANDS:
            self._pending.put_nowait(_Queued(text, self._binding, kind="compact"))
        elif command in _MODEL_COMMANDS:
            self._pending.put_nowait(_Queued(text, self._binding, kind="model"))
        elif _COMMAND_SHAPE.match(command):
            # Only a command-shaped token is treated as a slash command. A path
            # such as /data/setup stays a normal message.
            safe = command[:40]
            self._output.error(
                f"Unknown command {safe}; it was not sent to the model. Use /help."
            )
        else:
            self._sequence += 1
            self._pending.put_nowait(
                _Queued(text, self._binding, sequence=self._sequence)
            )
            waiting = max(0, self._pending.qsize() - 1)
            suffix = f"; {waiting} more waiting." if waiting else "."
            self._output.info(f"Queued message {self._sequence}{suffix}")

    async def _abort(self) -> None:
        self._aborting = True
        if self._active is not None and not self._active.done():
            await self._request_cancel()
        if self._command is not None:
            self._command.cancel()
        self._pending.put_nowait(None)

    def request_stop(self) -> None:
        """Terminal key handler: request one local cancel for the running turn.

        The cancellation task is owned by ``_cancellation`` and awaited by
        :meth:`run`/the queue gate, so a key handler never leaks a task. A
        pending or already-requested stop is not duplicated.
        """
        if self._stop_requested:
            return
        self._stop_requested = True
        self._output.cancel_requested()
        if (
            self._command_kind == "compact"
            and self._command is not None
            and not self._command.done()
        ):
            self._command.cancel()
        if self._cancellation is None or self._cancellation.done():
            self._cancellation = asyncio.create_task(self._cancel())

    async def _request_cancel(self) -> None:
        self._output.cancel_requested()
        if self._cancellation is not None and self._cancellation.done():
            await self._cancellation
            self._cancellation = None
        if self._cancellation is None:
            self._cancellation = asyncio.create_task(self._cancel())

    async def _cancel(self) -> None:
        target = self._active_session_id or self._binding.session_id
        if target is None:
            return
        try:
            await self._session.cancel(target)
        except Exception as exc:
            self._output.error(f"Cancellation request failed: {exc}")
            if self._aborting and self._active is not None:
                self._active.cancel()

    async def _wait_for_cancel(self) -> None:
        while self._cancellation is not None:
            task = self._cancellation
            await asyncio.shield(task)
            if self._cancellation is task:
                self._cancellation = None

    async def _process_inputs(self) -> None:
        while not self._aborting:
            item = await self._pending.get()
            await self._wait_for_cancel()
            if self._aborting or item is None or item.text.lower() in _EXIT_COMMANDS:
                return
            if item.kind == "session":
                await self._run_session_command(item)
                continue
            if item.kind == "compact":
                await self._run_compact_command(item)
                continue
            if item.kind == "model":
                await self._run_model_command(item)
                continue
            target = item.binding.session_id
            if target is None or item.binding.error:
                self._output.error(
                    "This message was not sent because its session switch did not "
                    "complete. Use /session to pick an existing session."
                )
                item.binding.session_id = self._fallback(item.binding)
                continue
            if item.sequence is not None:
                waiting = self._pending.qsize()
                suffix = f"; {waiting} still queued." if waiting else "."
                self._output.info(f"Processing message {item.sequence}{suffix}")
            await self._submit(item.text, target)

    async def _run_session_command(self, item: _Queued) -> None:
        binding = item.binding
        if self._session_command is None:
            self._output.error("Session switching is not available in this session.")
            binding.session_id = self._fallback(binding)
            return
        try:
            message = await self._session_command(item.text, binding)
        except Exception as exc:
            self._output.error(f"Session command failed: {exc}")
            binding.session_id = self._fallback(binding)
            binding.error = str(exc)
            return
        if binding.session_id is None:
            binding.session_id = self._fallback(binding)
        if self._on_active_session is not None:
            self._on_active_session(binding.session_id)
        if message:
            self._output.info(message)

    async def _run_compact_command(self, item: _Queued) -> None:
        target = item.binding.session_id or item.binding.fallback
        if self._compact_command is None or target is None:
            self._output.error("Context compaction is not available in this session.")
            return
        self._active_session_id = target
        self._command_kind = "compact"
        self._command = asyncio.create_task(self._compact_command(item.text, item.binding))
        try:
            message = await asyncio.shield(self._command)
        except asyncio.CancelledError:
            if self._aborting:
                raise
            self._output.info(
                "Context compaction cancelled locally; no checkpoint was written "
                "and the interrupted attempt is recorded."
            )
            return
        except Exception as exc:
            self._output.error(f"Context compaction failed: {exc}")
            return
        else:
            if message:
                self._output.info(message)
        finally:
            self._command = None
            self._command_kind = None
            self._active_session_id = None

    async def _run_model_command(self, item: _Queued) -> None:
        target = item.binding.session_id
        if target is None or item.binding.error:
            self._output.error(
                "Model command not applied: its session switch did not complete."
            )
            return
        if self._model_command is None:
            self._output.error("Model commands are not available in this session.")
            return
        if self._on_active_session is not None:
            self._on_active_session(target)
        self._command = asyncio.create_task(self._model_command(item.text))
        self._command_kind = "model"
        try:
            message = await asyncio.shield(self._command)
        except asyncio.CancelledError:
            if not self._aborting or not self._command.cancelled():
                raise
        except Exception as exc:
            self._output.error(f"Model command failed: {exc}")
        else:
            if message:
                self._output.info(message)
        self._command = None
        self._command_kind = None

    async def _submit(self, text: str, session_id: str) -> None:
        self._stop_requested = False
        self._active_session_id = session_id
        if self._name_session is not None and session_id not in self._named:
            self._named.add(session_id)
            try:
                await _maybe_await(self._name_session(session_id, text))
            except Exception as exc:
                self._output.error(f"Session title could not be saved: {exc}")
        if self._on_active_session is not None:
            self._on_active_session(session_id)
        self._output.turn_started(session_id)
        self._active = asyncio.create_task(
            self._session.submit(TurnRequest(session_id=session_id, text=text))
        )
        parent_cancelled = False
        try:
            result = await asyncio.shield(self._active)
        except asyncio.CancelledError:
            # Keep the inner submit referenced: run()'s cleanup requests a
            # local cancel and settles it instead of orphaning the turn.
            parent_cancelled = True
            if not self._aborting or not self._active.cancelled():
                raise
        except Exception as exc:
            self._output.error(
                f"Turn submission failed: {exc}. "
                "A failed turn does not mean a remote action failed."
            )
        else:
            self._output.result(result)
        finally:
            self._output.turn_finished()
            if not parent_cancelled:
                if self._on_turn_settled is not None:
                    try:
                        await _maybe_await(self._on_turn_settled(session_id))
                    except Exception as exc:
                        self._output.error(f"Post-turn bookkeeping failed: {exc}")
                self._active = None
                self._active_session_id = None
