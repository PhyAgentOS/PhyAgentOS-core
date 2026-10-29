"""Interactive input scheduling over an injected, entry-facing session.

The reader stays alive during turns. Ordinary input and model commands share
one FIFO; local commands (stop, help, tools, correct, unknown slash commands)
bypass it so a running turn never delays them. The caller owns the session
lifetime and terminal setup.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from PhyAgentOS.agent.core.types import TurnRequest, TurnResult, require_non_empty

__all__ = ["HELP_TEXT", "InteractiveAgent"]

_EXIT_COMMANDS = frozenset({"exit", "quit", "/exit", "/quit", ":q"})
_MODEL_COMMANDS = frozenset({"/provider", "/model", "/effort", "/status"})
_LOCAL_COMMANDS = frozenset({"/help", "/stop", "/tools", "/correct"}) | _MODEL_COMMANDS
_COMMAND_SHAPE = re.compile(r"^/[A-Za-z][A-Za-z0-9_-]*$")

HELP_TEXT = """\
Local commands (handled by the CLI; none of them is sent to the model):
  /help            Show this help.
  /tools [on|off]  Show or hide the full tool-call record for this turn.
  /provider ...    Change provider with text arguments (this session).
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

    def turn_started(self) -> None: ...

    def turn_finished(self) -> None: ...

    def cancel_requested(self) -> None: ...

    def tool_view(self, argument: str) -> str: ...


@dataclass(frozen=True, slots=True)
class _Queued:
    text: str
    sequence: int | None = None


class InteractiveAgent:
    """Run serial turns while continuously accepting injected async input.

    ``read_input`` takes no arguments and raises ``EOFError`` or
    ``KeyboardInterrupt`` to exit immediately. Exit commands instead wait for
    earlier queued input. Model commands are only called between turns; local
    commands are answered by the reader at once, in arrival order, so a slow
    turn never holds them behind ordinary text.
    """

    def __init__(
        self,
        session: _Session,
        *,
        session_id: str,
        output: _Output,
        read_input: Callable[[], Awaitable[str]],
        model_command: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        self._session = session
        self._session_id = require_non_empty("session_id", session_id)
        self._output = output
        self._read_input = read_input
        self._model_command = model_command
        self._pending: asyncio.Queue[_Queued | None] = asyncio.Queue()
        self._active: asyncio.Task[TurnResult] | None = None
        self._command: asyncio.Task[str | None] | None = None
        self._cancellation: asyncio.Task[None] | None = None
        self._aborting = False
        self._sequence = 0
        self._stop_requested = False

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
            self._pending.put_nowait(_Queued(text))
        elif command in _MODEL_COMMANDS:
            self._pending.put_nowait(_Queued(text))
        elif _COMMAND_SHAPE.match(command):
            # Only a command-shaped token is treated as a slash command. A path
            # such as /data/setup stays a normal message.
            safe = command[:40]
            self._output.error(
                f"Unknown command {safe}; it was not sent to the model. Use /help."
            )
        else:
            self._sequence += 1
            self._pending.put_nowait(_Queued(text, self._sequence))
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
        try:
            await self._session.cancel(self._session_id)
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
            command = item.text.split(maxsplit=1)[0].lower()
            if command in _MODEL_COMMANDS:
                await self._run_model_command(item.text)
                continue
            if item.sequence is not None:
                waiting = self._pending.qsize()
                suffix = f"; {waiting} still queued." if waiting else "."
                self._output.info(f"Processing message {item.sequence}{suffix}")
            await self._submit(item.text)

    async def _run_model_command(self, text: str) -> None:
        if self._model_command is None:
            self._output.error("Model commands are not available in this session.")
            return
        self._command = asyncio.create_task(self._model_command(text))
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

    async def _submit(self, text: str) -> None:
        self._stop_requested = False
        self._output.turn_started()
        self._active = asyncio.create_task(
            self._session.submit(TurnRequest(session_id=self._session_id, text=text))
        )
        try:
            result = await asyncio.shield(self._active)
        except asyncio.CancelledError:
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
        self._active = None
