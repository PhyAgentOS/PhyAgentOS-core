"""Terminal presentation of core results, without runtime or tool ownership.

Streaming has one owner per surface:

* with an attached prompt, deltas update the prompt toolbar preview and the
  final answer is printed once as Markdown above the prompt; the partial text
  is never copied into the transcript;
* without a prompt but on a terminal, Rich ``Live`` rewrites one transient
  region and the final Markdown is printed once after it;
* on a redirected stream (no TTY), deltas are written as plain text as they
  arrive. The final answer is not printed a second time.

Only visible body text is streamed. Provider reasoning is never shown, and
``<think>`` content is filtered across chunk boundaries. Tool-call rounds are
marked as intermediate where plain text cannot be erased; their text is never
presented as the turn's final answer.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from PhyAgentOS import __logo__
from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
from PhyAgentOS.agent.core.types import TurnResult, TurnStatus
from PhyAgentOS.agent.services.usage import (
    ContextSnapshot,
    UsageLedger,
    format_session_totals,
    format_turn_summary,
)
from PhyAgentOS.cli.interaction.terminal_display import (
    IDLE,
    PREPARING,
    TerminalDisplay,
    display_text,
    safe_detail,
)

__all__ = [
    "AgentOutput",
    "ThinkFilter",
    "display_text",
    "safe_detail",
    "visible_text",
]

_TERMINAL_TURNS = frozenset(
    {EventType.TURN_COMPLETED, EventType.TURN_FAILED, EventType.TURN_INTERRUPTED}
)
_STREAM_TEXT_LIMIT = 4000


class ThinkFilter:
    """Hold back <think>...</think> text, including split tags."""

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        self._buffer = ""
        self._in_think = False

    @staticmethod
    def _partial_suffix(text: str, tag: str) -> int:
        for size in range(min(len(text), len(tag) - 1), 0, -1):
            if tag.startswith(text[-size:]):
                return size
        return 0

    def feed(self, text: str | None) -> str:
        if text:
            self._buffer += text
        visible: list[str] = []
        while self._buffer:
            if self._in_think:
                end = self._buffer.find(self._CLOSE)
                if end < 0:
                    keep = self._partial_suffix(self._buffer, self._CLOSE)
                    self._buffer = self._buffer[-keep:] if keep else ""
                    return "".join(visible)
                self._buffer = self._buffer[end + len(self._CLOSE):]
                self._in_think = False
                continue

            start = self._buffer.find(self._OPEN)
            if start < 0:
                keep = self._partial_suffix(self._buffer, self._OPEN)
                if keep:
                    visible.append(self._buffer[:-keep])
                    self._buffer = self._buffer[-keep:]
                else:
                    visible.append(self._buffer)
                    self._buffer = ""
                return "".join(visible)
            visible.append(self._buffer[:start])
            self._buffer = self._buffer[start + len(self._OPEN):]
            self._in_think = True
        return "".join(visible)

    def flush(self) -> str:
        """Text still safe to show; an unclosed think block stays hidden."""
        if self._in_think:
            self._buffer = ""
            return ""
        text, self._buffer = self._buffer, ""
        return text


def visible_text(text: str | None) -> str:
    think = ThinkFilter()
    return think.feed(text) + think.flush()


class _StreamSanitizer:
    """Remove control characters and escape sequences across chunk boundaries.

    A chunk may end in the middle of an ANSI sequence. Holding that suffix back
    until the next chunk keeps a harmless model string from being half-printed
    as escape data; a final lone ESC is dropped at flush.
    """

    _CONTROL = set(range(0x00, 0x09)) | set(range(0x0B, 0x20)) | set(range(0x7F, 0xA0))

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, text: str) -> str:
        data = self._pending + (text or "")
        self._pending = ""
        output: list[str] = []
        index = 0
        while index < len(data):
            char = data[index]
            if char == "\x1b":
                consumed = self._escape_end(data, index)
                if consumed is None:
                    self._pending = data[index:]
                    break
                index = consumed
                continue
            if ord(char) in self._CONTROL:
                index += 1
                continue
            output.append(char)
            index += 1
        return "".join(output)

    @staticmethod
    def _escape_end(data: str, start: int) -> int | None:
        if start + 1 >= len(data):
            return None
        kind = data[start + 1]
        if kind == "[":
            index = start + 2
            while index < len(data):
                if "@" <= data[index] <= "~":
                    return index + 1
                index += 1
            return None
        if kind == "]":
            index = start + 2
            while index < len(data):
                if data[index] == "\x07":
                    return index + 1
                if data[index] == "\x1b" and index + 1 < len(data) and data[index + 1] == "\\":
                    return index + 2
                index += 1
            return None
        return start + 2

    def flush(self) -> str:
        text, self._pending = self._pending, ""
        # An incomplete escape sequence is not displayable text.
        if text.startswith("\x1b"):
            return ""
        return display_text(text)


class AgentOutput:
    """Render each terminal result once; never infer remote action state.

    The display owns the live region (turn status and this turn's tool rows).
    This class only decides what a message looks like: replies carry the
    product title, notices are dim, errors are red. Replies are the only place
    a green/success-looking treatment is avoided entirely; tool success never
    stands in for task success.
    """

    def __init__(
        self,
        console: Console,
        *,
        markdown: bool = True,
        ledger: UsageLedger | None = None,
    ) -> None:
        self._console = console
        self._markdown = markdown
        self._shown: set[tuple[str, str]] = set()
        self.display = TerminalDisplay(console)
        self.ledger = ledger or UsageLedger()
        self._context_provider: Callable[[str], ContextSnapshot] | None = None
        self._event_observer: Callable[[RuntimeEvent], None] | None = None
        self._active_session_id: str | None = None
        self._turn_started_at: float | None = None
        self._partial_text = ""
        self._stream_final_text = ""
        self._raw_final_written = False
        self._stream_active = False
        self._stream_call_id: str | None = None
        self._stream_parts: list[str] = []
        self._stream_sanitizer = _StreamSanitizer()
        self._stream_think = ThinkFilter()
        self._live: Live | None = None
        self._live_buffer = ""
        self._fallback_stream_notified = False

    # -- statistics ------------------------------------------------------

    def configure_stats(
        self,
        *,
        ledger: UsageLedger | None = None,
        context_provider: Callable[[str], ContextSnapshot] | None = None,
        event_observer: Callable[[RuntimeEvent], None] | None = None,
    ) -> None:
        if ledger is not None:
            self.ledger = ledger
        self._context_provider = context_provider
        self._event_observer = event_observer

    def refresh_status(self, session_id: str | None = None) -> None:
        target = session_id or self._active_session_id
        if target is None:
            return
        label = "context --%"
        if self._context_provider is not None:
            try:
                label = self._context_provider(target).label()
            except Exception:
                label = "context --%"
        self.display.set_context_label(label)

    # -- live region -----------------------------------------------------

    def attach_prompt(self, prompt) -> None:
        self.display.attach_prompt(prompt)

    def set_session_label(self, label: str) -> None:
        self.display.set_session_label(label)

    def startup(self, detail: str = "Forge recovery") -> None:
        self.display.set_phase(PREPARING, detail)

    def ready(self) -> None:
        self.display.set_phase(IDLE)

    def turn_started(self, session_id: str | None = None) -> None:
        if session_id is not None:
            self._active_session_id = session_id
        self._turn_started_at = time.monotonic()
        self._partial_text = ""
        self._stream_final_text = ""
        self._raw_final_written = False
        self.display.begin_turn()
        self.refresh_status()

    def turn_finished(self) -> None:
        self.display.end_turn()
        self.refresh_status()

    def cancel_requested(self) -> None:
        self.display.request_stop()

    def compact_started(self) -> None:
        """Show local busy state for a manual summary (no model turn exists)."""
        self.display.begin_turn()
        self.display.set_phase(PREPARING, "context compaction")

    def compact_finished(self) -> None:
        self.display.end_turn()

    def tool_view(self, argument: str = "") -> str:
        return self.display.tool_view(argument)

    # -- messages --------------------------------------------------------

    def info(self, text: str) -> None:
        self._console.print(Text(display_text(visible_text(text))), style="dim")

    def error(self, text: str) -> None:
        self._console.print(Text(display_text(visible_text(text))), style="red")

    def result(self, result: TurnResult) -> None:
        key = (result.session_id, result.turn_id)
        if key in self._shown:
            return
        self._shown.add(key)
        self._finish_stream()
        self.display.end_turn()

        content = display_text(visible_text(result.reply))
        mode = self._stream_mode()
        if content.strip():
            if mode == "raw" and self._raw_final_written:
                # The same body was already written as deltas; printing it
                # again would duplicate the transcript.
                pass
            else:
                self._print_reply(content)
        if result.status is not TurnStatus.COMPLETED:
            if self._partial_text.strip() and mode != "raw":
                self._console.print(
                    Text(
                        "（中断前的部分输出，未完成）" if result.status is TurnStatus.INTERRUPTED
                        else "（失败前的部分输出，未完成）"
                    ),
                    style="dim",
                )
                self._console.print(Text(self._partial_text))
            elif self._partial_text.strip():
                self._console.print(Text("（以上为未完成的部分输出）"), style="dim")
        if result.status is TurnStatus.FAILED:
            self.error(
                f"Turn failed: {result.error}\n"
                "A failed turn does not mean a remote action failed; "
                "its state has not been verified."
            )
        elif result.status is TurnStatus.INTERRUPTED:
            self.info(
                "Turn interrupted locally. Remote actions may still be running; "
                "their state has not been verified."
            )

        self._print_usage(result)
        self._active_session_id = result.session_id
        self._turn_started_at = None
        self.refresh_status(result.session_id)

    def event(self, event: RuntimeEvent) -> None:
        """Consume persisted lifecycle events; never arguments or outcomes."""
        self.ledger.ingest(event)
        if self._event_observer is not None:
            try:
                self._event_observer(event)
            except Exception:
                pass
        if event.type is EventType.MODEL_RESPONSE_DELTA:
            self.model_delta(event)
            return
        if event.type is EventType.MODEL_ATTEMPT_COMPLETED:
            if (
                event.payload.get("streaming") in {"fallback", "blocking"}
                and not self._fallback_stream_notified
            ):
                self._fallback_stream_notified = True
                self.info(
                    "This provider has no native streaming; the answer is shown "
                    "once it is complete."
                )
            self._end_attempt(tool_calls=bool(event.payload.get("tool_calls")))
            self.refresh_status(event.session_id)
            return
        if event.type in _TERMINAL_TURNS:
            self._end_attempt(tool_calls=False, terminal=True)
            self.display.note_event(event)
            self.refresh_status(event.session_id)
            return
        if event.type is EventType.TOOL_INTENT:
            # Defensive: a tool intent before the completion event still means
            # the current body was an intermediate answer.
            self._end_attempt(tool_calls=True)
        self.display.note_event(event)
        if event.type in (EventType.REQUEST_PREPARED, EventType.TOOL_SETTLED):
            self.refresh_status(event.session_id)

    # -- streaming -------------------------------------------------------

    def model_delta(self, event_or_id: object, text: str | None = None) -> None:
        """Render one visible body delta.

        ``event_or_id`` may be a persisted ``model.response.delta`` event or a
        bare call id. Only body text is accepted; reasoning belongs to the
        terminal response and is never displayed.
        """
        if isinstance(event_or_id, RuntimeEvent):
            call_id = event_or_id.payload.get("model_call_id")
            content = event_or_id.payload.get("content")
            text = content if isinstance(content, str) else text
        else:
            call_id = str(event_or_id)
        if not isinstance(text, str) or not text:
            return
        if call_id != self._stream_call_id:
            self._end_attempt(tool_calls=False)
            self._begin_attempt(str(call_id) if call_id else None)
        visible = self._stream_think.feed(self._stream_sanitizer.feed(text))
        if not visible:
            return
        self._stream_parts.append(visible)
        self._emit_stream(visible)

    def _begin_attempt(self, call_id: str | None) -> None:
        self._stream_active = True
        self._stream_call_id = call_id
        self._stream_parts = []
        self._stream_sanitizer = _StreamSanitizer()
        self._stream_think = ThinkFilter()
        self._live_buffer = ""

    def _stream_mode(self) -> str:
        if self.display.live:
            return "preview"
        try:
            return "live" if self._console.is_terminal else "raw"
        except Exception:
            return "raw"

    def _current_text(self) -> str:
        return "".join(self._stream_parts)

    def _emit_stream(self, chunk: str) -> None:
        mode = self._stream_mode()
        if mode == "preview":
            text = self._current_text()
            self.display.set_stream_text(text[-_STREAM_TEXT_LIMIT:])
            return
        if mode == "live":
            self._live_buffer = self._current_text()
            if self._live is None:
                self._live = Live(
                    Text(""),
                    console=self._console,
                    refresh_per_second=10,
                    transient=True,
                    vertical_overflow="visible",
                )
                self._live.start()
            self._live.update(Text(self._live_buffer))
            return
        self._print_chunk(chunk)

    def _print_chunk(self, chunk: str) -> None:
        if not chunk:
            return
        self._console.print(Text(chunk), end="", soft_wrap=True, highlight=False)
        try:
            self._console.file.flush()
        except Exception:
            pass

    def _end_attempt(self, *, tool_calls: bool, terminal: bool = False) -> None:
        if not self._stream_active:
            return
        tail = self._stream_sanitizer.flush()
        visible = "".join(self._stream_parts)
        if tail:
            visible += self._stream_think.feed(tail)
        visible += self._stream_think.flush()
        mode = self._stream_mode()
        if mode == "preview":
            self.display.set_stream_text("")
            if terminal:
                self._partial_text = visible
            elif not tool_calls:
                self._stream_final_text = visible
        elif mode == "live":
            if self._live is not None:
                self._live.stop()
                self._live = None
            if terminal:
                self._partial_text = visible
            elif not tool_calls:
                self._stream_final_text = visible
        else:
            if visible and not visible.endswith("\n"):
                self._console.print()
            if tool_calls:
                if visible.strip():
                    self._console.print(
                        Text("（以上为工具调用前的中间回复，不是最终答复）"), style="dim"
                    )
            elif terminal:
                self._partial_text = visible
            else:
                self._stream_final_text = visible
                self._raw_final_written = bool(visible.strip())
        self._stream_active = False
        self._stream_call_id = None
        self._stream_parts = []
        self._stream_sanitizer = _StreamSanitizer()
        self._stream_think = ThinkFilter()

    def _finish_stream(self) -> None:
        if self._stream_active:
            self._end_attempt(tool_calls=False, terminal=True)
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _print_reply(self, content: str) -> None:
        self._console.print()
        self._console.print(Text(f"{__logo__} PhyAgentOS:", style="cyan"))
        self._console.print(Markdown(content) if self._markdown else Text(content))
        self._console.print()

    def _print_usage(self, result: TurnResult) -> None:
        turn = self.ledger.turn(result.session_id, result.turn_id)
        total = self.ledger.session(result.session_id)
        if not (turn.has_any() or turn.model_calls or turn.tool_calls):
            return
        duration = None
        if self._turn_started_at is not None:
            duration = time.monotonic() - self._turn_started_at
        self._console.print(Text(format_turn_summary(turn, duration)), style="dim")
        self._console.print(Text(format_session_totals(total)), style="dim")
