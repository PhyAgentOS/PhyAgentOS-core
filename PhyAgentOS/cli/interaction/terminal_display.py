"""Single owner of the live terminal region: turn status and the tool area.

The interactive prompt owns the input line. When a prompt is attached, this
module supplies the prompt message: the live rows render directly above the
session line and the input line, so they stay next to the permanent request
without the gap a terminal-anchored bottom toolbar leaves. prompt_toolkit's own
refresh timer animates the spinner and the elapsed time. There is no second
cursor-moving writer: Rich prints messages above the prompt through
``patch_stdout``, which the terminal input owns.

Without a prompt (a one-shot ``-m`` run, startup, or a redirected stream) the
same state model falls back to plain lines on state changes. That path never
emits a carriage return, an escape sequence or an animation frame, so a
redirected log stays readable even though ``NO_COLOR`` is not set.

Only events already persisted by the store are consumed. Tool arguments,
outcomes and raw provider text are never read; an error summary is redacted,
control characters are stripped and long text is truncated.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

from prompt_toolkit.utils import get_cwidth

from PhyAgentOS.agent.core.events import EventType, RuntimeEvent
from PhyAgentOS.agent.core.types import ToolStatus

__all__ = [
    "IDLE",
    "PREPARING",
    "STOPPING",
    "THINKING",
    "TOOL",
    "TerminalDisplay",
    "display_text",
    "safe_detail",
]

IDLE = "idle"
PREPARING = "preparing"
THINKING = "thinking"
TOOL = "tool"
STOPPING = "stopping"

_TERMINAL_TURNS = frozenset(
    {EventType.TURN_COMPLETED, EventType.TURN_FAILED, EventType.TURN_INTERRUPTED}
)
_TOOL_EVENTS = frozenset(
    {EventType.TOOL_INTENT, EventType.TOOL_DISPATCHED, EventType.TOOL_SETTLED}
)

_SPINNER = (".  ", ".. ", "...", " ..")
_SPINNER_SECONDS = 0.30
STOP_WINDOW_SECONDS = 2.0
STOP_HINT = (
    "Press Esc again within 2s to stop this task "
    "(local stop; remote stop is not confirmed)."
)
TOOL_ROWS = 4
REQUEST_PROMPT = "│ "
_ERROR_LIMIT = 400
_STATUS_LABELS = {
    PREPARING: "Preparing",
    THINKING: "Thinking",
    TOOL: "Calling tool",
    STOPPING: "Stopping locally (remote unconfirmed)",
    IDLE: "Ready",
}
_PHASE_STYLES = {
    PREPARING: "fg:ansiblue",
    THINKING: "fg:ansiblue",
    TOOL: "fg:ansiblue",
    STOPPING: "fg:ansiyellow",
    IDLE: "fg:ansibrightblack",
}
_TOOL_MARKERS = {
    "requested": ".",
    "running": ">",
    "succeeded": "+",
    "failed": "x",
    "rejected": "x",
    "cancelled": "-",
    "unknown": "?",
}
_TOOL_STYLES = {
    "succeeded": "fg:ansigreen",
    "failed": "fg:ansired",
    "rejected": "fg:ansired",
    "cancelled": "fg:ansiyellow",
    "unknown": "fg:ansiyellow",
    "running": "fg:ansiblue",
    "requested": "fg:ansiblue",
}

# The same C0/C1 filter the renderer applies to model and tool text. Keeping it
# here lets the live rows and the line fallback share one safety rule.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_URL_RE = re.compile(r"\b(?:https?|wss?)://[^\s'\"]+", re.IGNORECASE)
_BEARER_RE = re.compile(r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_KEY_VALUE_RE = re.compile(
    r"\b(authorization|x-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"token|password|passwd|secret)\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;)}\]]+)",
    re.IGNORECASE,
)
_JSON_KEY_VALUE_RE = re.compile(
    r'"(authorization|x-api-key|api[_-]?key|access[_-]?token|refresh[_-]?token|'
    r'id[_-]?token|client[_-]?secret|private[_-]?key|token|password|passwd|secret)"'
    r'\s*:\s*(?:"(?:[^"\\]|\\.)*"|\'[^\']*\'|[^\s,;}]+)',
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"\b(?:sk-|sk_|ghp_|github_pat_|xox[abp]-|AIza)[A-Za-z0-9._-]{6,}"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_LONG_TOKEN_RE = re.compile(
    r"(?<![\w-])(?=[A-Za-z0-9_-]*[0-9])(?=[A-Za-z0-9_-]*[A-Za-z])"
    r"[A-Za-z0-9_-]{32,}(?![\w-])"
)


def display_text(text: str) -> str:
    """Remove control characters from untrusted display text."""
    return _CONTROL_CHARS.sub("", text)


def safe_detail(value: object, *, limit: int = _ERROR_LIMIT) -> str:
    """One-line, redacted, length-limited error text for the expanded record."""
    text = value if isinstance(value, str) else str(value or "")
    text = display_text(text.replace("\n", " ").replace("\t", " "))
    text = " ".join(text.split())
    text = _URL_RE.sub("<url>", text)
    # Auth schemes first, so a scheme word cannot consume the token and leave
    # the credential behind for the key/value rule.
    text = _BEARER_RE.sub("<redacted>", text)
    text = _JSON_KEY_VALUE_RE.sub(lambda match: f'"{match.group(1)}": "<redacted>"', text)
    text = _KEY_VALUE_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = _SECRET_RE.sub("<redacted>", text)
    text = _JWT_RE.sub("<redacted>", text)
    text = _LONG_TOKEN_RE.sub("<redacted>", text)
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def _fit(text: str, width: int) -> str:
    """Truncate to terminal columns, not code points (CJK/emoji are double)."""
    if width <= 0:
        return ""
    if get_cwidth(text) <= width:
        return text
    kept: list[str] = []
    used = 0
    for char in text:
        char_width = get_cwidth(char)
        if used + char_width > width - 1:
            break
        kept.append(char)
        used += char_width
    return "".join(kept) + "…"


def _wrap_display(text: str, width: int) -> list[str]:
    """Wrap text at display-column boundaries, preserving zero-width marks."""
    if width <= 0:
        return []
    lines: list[str] = []
    current: list[str] = []
    used = 0
    for char in text:
        if char == "\n":
            lines.append("".join(current))
            current = []
            used = 0
            continue
        char_width = get_cwidth(char)
        if current and used + char_width > width:
            lines.append("".join(current))
            current = []
            used = 0
        current.append(char)
        used += char_width
    lines.append("".join(current))
    return lines


def _clean(value: object, *, limit: int = 120) -> str:
    return _fit(display_text(" ".join(str(value or "").split())), limit)


def _error_value(payload) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        code = _clean(error.get("code"), limit=60)
        message = safe_detail(error.get("message"))
        if code and message:
            return f"{code}: {message}"
        return code or message
    return safe_detail(error) if error else ""


@dataclass(slots=True)
class _ToolRecord:
    session_id: str
    turn_id: str
    tool_call_id: str
    name: str
    status: str
    error: str
    started: float
    ended: float | None = None

    @property
    def settled(self) -> bool:
        return self.status not in {"requested", "running"}

    @property
    def failed(self) -> bool:
        return self.status in {"failed", "rejected", "unknown"}


class TerminalDisplay:
    """Track turn status and this turn's tool calls; render through one owner."""

    def __init__(
        self,
        console,
        *,
        session_label: str = "",
        clock: Callable[[], float] = time.monotonic,
        size_provider: Callable[[], tuple[int, int]] | None = None,
    ) -> None:
        self._console = console
        self._clock = clock
        self._size_provider = size_provider
        self._session_label = session_label
        self._context_label = ""
        self._stream_text = ""
        self._prompt = None
        self._phase = IDLE
        self._detail = ""
        self._since = clock()
        self._active_since: float | None = None
        self._turn_id: str | None = None
        self._turn_active = False
        self._records: list[_ToolRecord] = []
        self._index: dict[tuple[str, str, str], _ToolRecord] = {}
        self._expanded = False
        # User-selected display preference survives turn boundaries. The
        # per-turn _expanded flag still closes when the live region closes.
        self._display_all_steps = False
        self._collapsed = False
        self._summary_shown = False
        self._last_line = ""
        self._thinking_announced = False
        self._stop_hint = False
        self._armed_until: float | None = None

    # -- ownership -------------------------------------------------------

    def attach_prompt(self, prompt) -> None:
        """Make the attached prompt's message the live region."""
        self._prompt = prompt
        self._last_line = ""
        # Hide the real renderer cursor while busy; the official Window flag is
        # evaluated on every render, so it survives later invalidation.
        try:
            from prompt_toolkit.filters import Condition
            from prompt_toolkit.layout.controls import BufferControl

            hide = Condition(lambda: self.busy)
            for window in prompt.layout.find_all_windows():
                control = window.content
                if (
                    isinstance(control, BufferControl)
                    and control.buffer is prompt.default_buffer
                ):
                    window.always_hide_cursor = hide
        except Exception:
            pass
        self._invalidate()

    def set_session_label(self, label: str) -> None:
        self._session_label = display_text(label)
        self._invalidate()

    def set_context_label(self, label: str) -> None:
        """Set the context line (``context ≈23%``); never re-estimate here."""
        label = display_text(label)
        if label == self._context_label:
            return
        self._context_label = label
        self._invalidate()

    def set_stream_text(self, text: str) -> None:
        """Preview streamed body text in the live region while a prompt owns it."""
        text = display_text(text)
        if text == self._stream_text:
            return
        self._stream_text = text
        self._invalidate()

    @property
    def session_label(self) -> str:
        return self._session_label

    @property
    def context_label(self) -> str:
        return self._context_label

    @property
    def stream_text(self) -> str:
        return self._stream_text

    def prompt_message(self):
        """Live rows, the session line, then the request marker when idle.

        The live area is part of the prompt message so it renders immediately
        above the input line, close to the permanent request. A bottom toolbar
        would pin the same rows to the last terminal line and leave a large
        blank gap in between.
        """
        fragments: list[tuple[str, str]] = []
        if self._prompt is not None:
            try:
                width, rows = self._size()
                for index, line in enumerate(
                    self.render_lines(width=width, rows=rows)
                ):
                    fragments.append((self._style_for(line, index), line + "\n"))
            except Exception:
                # A render error must never take down the prompt. The fallback
                # has no remote or secret text.
                fragments.append(("fg:ansibrightblack", "status unavailable\n"))
        line = self._session_label
        if self._context_label:
            line = f"{line} · {self._context_label}" if line else self._context_label
        if line:
            fragments.append(("fg:ansibrightblack", line + "\n"))
        if not self.busy:
            fragments.append(("bold fg:ansicyan", REQUEST_PROMPT))
        return fragments

    # -- stop hint -------------------------------------------------------

    def arm_stop_hint(self) -> bool:
        """Arm the first Esc; True when this is the confirming second Esc."""
        now = self._clock()
        if self._armed_until is not None and now <= self._armed_until:
            self.disarm_stop_hint()
            return True
        self._armed_until = now + STOP_WINDOW_SECONDS
        self._stop_hint = True
        self._invalidate()
        return False

    def disarm_stop_hint(self) -> None:
        if self._armed_until is None and not self._stop_hint:
            return
        self._armed_until = None
        self._stop_hint = False
        self._invalidate()

    @property
    def stop_hint(self) -> bool:
        return self._stop_hint

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def expanded(self) -> bool:
        return self._expanded

    @property
    def turn_id(self) -> str | None:
        return self._turn_id

    @property
    def records(self) -> tuple[_ToolRecord, ...]:
        return tuple(self._records)

    @property
    def live(self) -> bool:
        return self._prompt is not None

    @property
    def busy(self) -> bool:
        """A turn is preparing, running, calling a tool, or stopping."""
        return self._phase != IDLE

    # -- turn lifecycle --------------------------------------------------

    def set_phase(self, phase: str, detail: str = "") -> None:
        # Once a stop is requested, later model/tool events must not repaint
        # the live status as if the turn were still progressing. The tool rows
        # still update; only the phase is held until the terminal event or
        # end_turn clears it.
        if self._phase == STOPPING and phase != STOPPING:
            return
        if phase == self._phase and detail == self._detail:
            return
        if phase != IDLE and not self._turn_active and phase != PREPARING:
            self._turn_active = True
        if phase != IDLE and self._active_since is None:
            self._active_since = self._clock()
        self._phase = phase
        self._detail = detail
        self._announce()
        self._invalidate()

    def begin_turn(self) -> None:
        """Called as soon as the submitted message leaves the queue."""
        self._phase = PREPARING
        self._detail = ""
        self._since = self._clock()
        self._active_since = self._since
        self._turn_active = True
        self._turn_id = None
        self._records = []
        self._index = {}
        self._expanded = self._display_all_steps
        self._collapsed = False
        self._summary_shown = False
        self._last_line = ""
        self._thinking_announced = False
        self._stop_hint = False
        self._armed_until = None
        self._stream_text = ""
        self._announce()
        self._invalidate()

    def request_stop(self) -> None:
        # A stop with no running turn must not leave a spinner behind; the
        # cancellation notice alone explains that local stop was requested.
        if self._turn_active or self._active_since is not None:
            self.set_phase(STOPPING)

    def end_turn(self) -> None:
        """Clear the live region once, even if several owners report the end."""
        if not self._turn_active and self._phase == IDLE:
            return
        self._phase = IDLE
        self._detail = ""
        self._active_since = None
        self._turn_active = False
        if self.live and self._records and not self._summary_shown:
            self._summary_shown = True
            self._print_text(self._summary_text())
        self._collapsed = True
        self._expanded = False
        self._stop_hint = False
        self._armed_until = None
        self._stream_text = ""
        self._announce()
        self._invalidate()

    # -- events ----------------------------------------------------------

    def note_event(self, event: RuntimeEvent) -> None:
        if event.type in _TERMINAL_TURNS:
            self.end_turn()
            return
        if event.type is EventType.TURN_STARTED:
            self._note_turn_started(event)
            return
        if event.type in (EventType.MODEL_ATTEMPT_STARTED, EventType.REQUEST_PREPARED):
            if self._turn_active:
                self.set_phase(THINKING)
            return
        if event.type in _TOOL_EVENTS:
            self._note_tool(event)

    def _note_turn_started(self, event: RuntimeEvent) -> None:
        if self._turn_id != event.turn_id:
            self._turn_id = event.turn_id
            self._records = []
            self._index = {}
            self._expanded = self._display_all_steps
            self._collapsed = False
            self._summary_shown = False
        self._turn_active = True
        if self._active_since is None:
            self._since = self._clock()
            self._active_since = self._since
        self.set_phase(THINKING)

    def _note_tool(self, event: RuntimeEvent) -> None:
        # Events of another turn are not this turn's state. Ignoring them keeps
        # a new turn from inheriting old rows, and a duplicate event updates the
        # existing row instead of adding a second one.
        if event.turn_id is None:
            return
        if self._turn_id is None:
            # A display attached mid-turn can still show that turn; this does
            # not reopen a previous turn because a new turn always arrives with
            # its own turn.started and resets the record set. Between submit and
            # turn.started the current turn's id is not known yet, so an event
            # from an older turn is ignored instead of being adopted.
            if self._turn_active:
                return
            self._turn_id = event.turn_id
            self._turn_active = True
        if event.turn_id != self._turn_id:
            return
        payload = event.payload
        call_id = payload.get("tool_call_id")
        if not call_id:
            return
        key = (event.session_id, event.turn_id, str(call_id))
        record = self._index.get(key)
        if record is None:
            record = _ToolRecord(
                event.session_id,
                event.turn_id,
                str(call_id),
                name=_clean(payload.get("tool_name") or "tool"),
                status="requested",
                error="",
                started=self._clock(),
            )
            self._records.append(record)
            self._index[key] = record

        previous = record.status
        if event.type is EventType.TOOL_SETTLED:
            status = payload.get("status")
            try:
                status = ToolStatus(status).value
            except (TypeError, ValueError):
                status = "unknown"
            record.status = status
            record.ended = self._clock()
            record.error = _error_value(payload)
        elif event.type is EventType.TOOL_DISPATCHED:
            if not record.settled:
                record.status = "running"
        else:
            if not record.settled:
                record.status = "requested"
        if not record.settled:
            self.set_phase(TOOL, record.name)
        elif self._phase == TOOL:
            running = next((item for item in reversed(self._records) if not item.settled), None)
            self.set_phase(TOOL, running.name) if running else self.set_phase(THINKING)

        if self.live:
            if self._expanded and record.settled and previous != record.status:
                index = self._records.index(record) + 1
                self._print_text(self._record_line(record, index))
            self._invalidate()
        elif event.type is EventType.TOOL_INTENT:
            self._line(f"Tool {record.name}: requested")
        elif event.type is EventType.TOOL_SETTLED:
            marker = "" if record.status == "succeeded" else "! "
            self._line(f"{marker}Tool {record.name}: {record.status}")

    # -- rendering -------------------------------------------------------

    def render_lines(self, *, width: int = 80, rows: int = 24) -> list[str]:
        lines: list[str] = []
        if (
            self._armed_until is not None
            and self._clock() > self._armed_until
        ):
            self._armed_until = None
            self._stop_hint = False
        if self._stop_hint and self._phase != IDLE:
            lines.append(_fit(STOP_HINT, width))
        if self._phase != IDLE:
            elapsed = max(0.0, self._clock() - (self._active_since or self._since))
            frame = _SPINNER[int(elapsed / _SPINNER_SECONDS) % len(_SPINNER)]
            label = _STATUS_LABELS.get(self._phase, self._phase)
            if self._phase == TOOL and self._detail:
                label = f"Calling {self._detail}"
            elif self._phase == PREPARING and self._detail:
                label = f"Preparing ({self._detail})"
            if self._phase == STOPPING:
                label = _STATUS_LABELS[STOPPING]
            lines.append(_fit(f"{frame} {label} {elapsed:.1f}s", width))

        if self._stream_text:
            # Wrap by terminal display columns, not by code points; CJK and
            # emoji take two columns. Show the newest wrapped lines so a long
            # reply without newlines keeps moving. The transcript gets the
            # final Markdown once, printed by AgentOutput above the prompt.
            inner_width = max(1, width - 2)
            wrapped = _wrap_display(self._stream_text, inner_width)
            reserved = 3  # status + session line + input line
            if self._stop_hint and self._phase != IDLE:
                reserved += 1
            if self._turn_active and self._records and not self._collapsed:
                reserved += self._row_limit(rows) + 1
            available = max(1, rows - reserved)
            for line in wrapped[-available:]:
                lines.append(_fit(f"│ {line}", width))

        if not self._turn_active or not self._records or self._collapsed:
            return lines

        visible = self._records[-self._row_limit(rows):]
        hidden = len(self._records) - len(visible)
        ok = sum(1 for item in self._records if item.status == "succeeded")
        bad = sum(1 for item in self._records if item.failed)
        cancelled = sum(1 for item in self._records if item.status == "cancelled")
        counts = [f"{len(self._records)} calls", f"{hidden} hidden"]
        if hidden and any(item.failed for item in self._records[:-len(visible)]):
            counts.append("hidden include failures")
        counts.append(f"{ok} ok")
        if bad:
            counts.append(f"{bad} failed/unknown")
        if cancelled:
            counts.append(f"{cancelled} cancelled")
        header = "| " + " · ".join(counts)
        if self._expanded:
            header += " · full record on"
        lines.append(_fit(header, width))

        for record in visible:
            marker = _TOOL_MARKERS.get(record.status, "?")
            row = f"| {marker} {record.name} {record.status}"
            if record.error and record.status in {"failed", "rejected", "unknown"}:
                row += f" · {_fit(record.error, 60)}"
            lines.append(_fit(row, width))
        return lines

    def _style_for(self, line: str, index: int) -> str:
        if self._stop_hint and index == 0:
            return "fg:ansiyellow bold"
        if line.startswith("│ "):
            return "fg:ansibrightblack"
        if index > 0 and line.startswith("| "):
            token = line.split(" ", 2)[1] if line.count(" ") >= 2 else ""
            if token in _TOOL_MARKERS.values():
                status = line.split(" · ")[0].rsplit(" ", 1)[-1]
                return _TOOL_STYLES.get(status, "fg:ansibrightblack")
            if "failed/unknown" in line:
                return _TOOL_STYLES["failed"]
            if "cancelled" in line:
                return _TOOL_STYLES["cancelled"]
            return "fg:ansibrightblack"
        if self._phase != IDLE:
            return _PHASE_STYLES.get(self._phase, _PHASE_STYLES[THINKING])
        return "fg:ansibrightblack"

    def _row_limit(self, rows: int) -> int:
        if rows <= 8:
            return 1
        if rows <= 12:
            return 2
        if rows <= 16:
            return 3
        return TOOL_ROWS

    def _size(self) -> tuple[int, int]:
        if self._size_provider is not None:
            return self._size_provider()
        from prompt_toolkit.application import get_app

        size = get_app().output.get_size()
        return size.columns, size.rows

    def _summary_text(self) -> str:
        ok = sum(1 for item in self._records if item.status == "succeeded")
        bad = sum(1 for item in self._records if item.failed)
        cancelled = sum(1 for item in self._records if item.status == "cancelled")
        parts = [f"{len(self._records)} calls", f"{ok} ok"]
        if bad:
            parts.append(f"{bad} failed/unknown")
        if cancelled:
            parts.append(f"{cancelled} cancelled")
        prefix = "! " if bad else ""
        return (
            f"{prefix}Tools this turn: " + " · ".join(parts)
            + ". /display_all_step shows the full record."
        )

    def _record_line(self, record: _ToolRecord, index: int) -> str:
        duration = ""
        if record.ended is not None:
            duration = f" · {max(0.0, record.ended - record.started):.1f}s"
        detail = f" · {record.error}" if record.error else ""
        return f"  {index}. {record.name} · {record.status}{duration}{detail}"

    def _record_text(self) -> str:
        if not self._records:
            return "No tool calls in this turn."
        turn = display_text(self._turn_id or "unknown")
        heading = (
            f"Tool record for turn {turn} "
            "(redacted; arguments and outcomes are not stored):"
        )
        lines = [
            heading,
            *(self._record_line(item, index) for index, item in enumerate(self._records, 1)),
        ]
        return "\n".join(lines)

    # -- local commands --------------------------------------------------

    def tool_view(self, argument: str = "") -> str:
        argument = argument.strip().lower()
        if argument in {"on", "show", "expand"}:
            self._display_all_steps = True
        elif argument in {"off", "hide", "collapse"}:
            self._display_all_steps = False
        elif argument:
            return "Usage: /display_all_step [on|off]. The command is local and was not sent to the model."
        else:
            self._display_all_steps = not self._display_all_steps
        self._expanded = self._display_all_steps
        self._invalidate()
        if not self._expanded:
            return "Tool record collapsed."
        if self._turn_active:
            self._print_text(self._record_text())
            return "Tool record expanded; new settlements are printed as they arrive."
        return self._record_text()

    # -- line fallback ---------------------------------------------------

    def _announce(self) -> None:
        if self.live:
            self._last_line = ""
            return
        if self._phase == IDLE or self._phase == TOOL:
            self._last_line = ""
            return
        if self._phase == STOPPING:
            line = _STATUS_LABELS[STOPPING]
        elif self._phase == PREPARING and self._detail:
            line = f"Preparing ({self._detail})"
        else:
            line = _STATUS_LABELS.get(self._phase, self._phase)
        if self._phase == THINKING:
            if self._thinking_announced:
                return
            self._thinking_announced = True
        if line != self._last_line:
            self._last_line = line
            self._line(line)

    def _print_text(self, text: str) -> None:
        # Text() bypasses Rich markup parsing: tool names and remote error text
        # are data, and "[red]" in them must not become formatting or raise.
        from rich.text import Text

        self._console.print(Text(display_text(text)))

    def _line(self, text: str) -> None:
        self._print_text(text)

    def _invalidate(self) -> None:
        if self._prompt is not None:
            try:
                self._prompt.app.invalidate()
            except Exception:
                pass
