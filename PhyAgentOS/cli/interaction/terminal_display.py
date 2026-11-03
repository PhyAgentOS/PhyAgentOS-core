"""Single owner of the live terminal region and this turn's settled tool rows.

The transcript keeps the order things happened: a settled tool call prints one
line immediately, before whatever the model writes next. The live region holds
only what is still changing: the status line and the streamed text of the
current model call. It renders directly above the session line and the input
line, so it stays next to the permanent request without the gap a
terminal-anchored bottom toolbar leaves. prompt_toolkit's own refresh timer
animates the spinner and the elapsed time. There is no second cursor-moving
writer: Rich prints messages above the prompt through ``patch_stdout``, which
the terminal input owns.

Without a prompt (a one-shot ``-m`` run, startup, or a redirected stream) the
same state model falls back to plain lines on state changes. That path never
emits a carriage return, an escape sequence or an animation frame, so a
redirected log stays readable even though ``NO_COLOR`` is not set.

Only events already persisted by the store are consumed. A tool row carries one
short value from the call's arguments, taken from a fixed key order and
redacted; whole argument objects, outcomes and raw provider text are never
read. Error summaries are redacted the same way, control characters are
stripped and long text is truncated.
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
REQUEST_PROMPT = "│ "
_ERROR_LIMIT = 400
# The live region only shows what fits; a long reply keeps its newest lines.
_PREVIEW_CHARS = 2500
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
# One short argument value per row, first key that holds a non-empty string.
# The order is the whole policy: it makes ``activate_skill(name)``,
# ``forge_tool_query(tool_id)``, ``read_file(path)`` and ``exec(command)`` show
# what they act on without a per-tool mapping that silently drifts.
_DETAIL_KEYS = (
    "path",
    "tool_id",
    "name",
    "command",
    "query",
    "url",
    "job_id",
    "task_id",
    "task_description",
    "label",
    "task",
    "child_session_id",
    "invocation_id",
    "method",
    "action",
    "query_type",
    "image_path",
    "content",
    "program",
)
_DETAIL_LIMIT = 60
# A settled call prints one line: marker, bold name, one argument detail,
# optional status, elapsed time and a redacted error. Colour follows the
# outcome, never the tool.
_TOOL_MARKERS = {
    "succeeded": "✓",
    "failed": "✗",
    "rejected": "✗",
    "cancelled": "−",
    "unknown": "?",
}
_TOOL_STYLES = {
    "succeeded": "fg:ansigreen",
    "failed": "fg:ansired",
    "rejected": "fg:ansired",
    "cancelled": "fg:ansiyellow",
    "unknown": "fg:ansiyellow",
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


def _url_brief(match: "re.Match[str]") -> str:
    """Keep scheme, host and path of a URL; drop its query and fragment."""
    from urllib.parse import urlsplit

    raw = match.group(0)
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "<url>"
    if not parts.netloc:
        return "<url>"
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def argument_detail(arguments: object, *, limit: int = _DETAIL_LIMIT) -> str:
    """Return one short, redacted value from a tool call's arguments.

    Only a single string value is considered, chosen by ``_DETAIL_KEYS``; an
    argument object with no such key shows no detail instead of dumping itself.
    URLs keep their host and path, credentials are replaced, whitespace is
    collapsed and the result is cut to ``limit`` display columns.
    """
    if not isinstance(arguments, dict):
        return ""
    values = (
        arguments.get(key)
        for key in _DETAIL_KEYS
        if isinstance(arguments.get(key), str) and arguments[key].strip()
    )
    value = next(values, None)
    if value is None:
        return ""
    text = display_text(" ".join(value.split()))
    text = _URL_RE.sub(_url_brief, text)
    text = _BEARER_RE.sub("<redacted>", text)
    text = _JSON_KEY_VALUE_RE.sub(
        lambda item: f'"{item.group(1)}": "<redacted>"', text
    )
    text = _KEY_VALUE_RE.sub(lambda item: f"{item.group(1)}=<redacted>", text)
    text = _SECRET_RE.sub("<redacted>", text)
    text = _JWT_RE.sub("<redacted>", text)
    text = _LONG_TOKEN_RE.sub("<redacted>", text)
    return _fit(text, limit)


# Rich renders Markdown links as OSC 8 hyperlinks. prompt_toolkit's ANSI parser
# does not consume OSC, so the address would be printed as body text.
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def markdown_lines(text: str, width: int) -> list[list[tuple[str, str]]]:
    """Render Markdown into prompt_toolkit fragments, one list per line.

    Rich owns the wrapping and styling; the ANSI string is parsed back into
    fragments because the prompt message only accepts styled text. Hyperlink
    OSC sequences are dropped first. Every line is padded to the render width
    on purpose: the caller renders one column narrower than the terminal, so
    the padding can never wrap.
    """
    import io

    from prompt_toolkit.formatted_text import ANSI, to_formatted_text
    from rich.console import Console
    from rich.markdown import Markdown

    buffer = io.StringIO()
    # One column narrower than the terminal: Rich pads every line to its width,
    # and that padding must not wrap in the prompt. highlight=False keeps Rich
    # from re-parsing code spans as its own highlight markup.
    console = Console(
        file=buffer,
        force_terminal=True,
        width=max(1, width - 1),
        color_system="truecolor",
        highlight=False,
    )
    console.print(Markdown(text), end="")
    fragments = to_formatted_text(ANSI(_OSC_RE.sub("", buffer.getvalue())))
    lines: list[list[tuple[str, str]]] = [[]]
    for style, chunk in fragments:
        parts = chunk.split("\n")
        for index, part in enumerate(parts):
            if index:
                lines.append([])
            if part:
                lines[-1].append((style, part))
    while lines and not lines[-1]:
        lines.pop()
    return lines


@dataclass(slots=True)
class _ToolRecord:
    session_id: str
    turn_id: str
    tool_call_id: str
    name: str
    status: str
    error: str
    detail: str
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
        self._preview_lines: list[list[tuple[str, str]]] = []
        self._preview_key: tuple[int, int, int] | None = None
        # Markdown re-rendering is throttled by its own measured cost, so a
        # long reply cannot spend most of the frame budget on the preview.
        self._preview_at = 0.0
        self._preview_cost = 0.0
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
        blank gap in between. Settled tool calls are not part of this area:
        they are printed once into the transcript, in the order they finished.
        """
        fragments: list[tuple[str, str]] = []
        if self._prompt is not None:
            try:
                width, rows = self._size()
                fragments.extend(self._live_fragments(width=width, rows=rows))
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
                detail=argument_detail(payload.get("arguments")),
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

        # A settled call becomes one permanent transcript line right here, so
        # every later model chunk lands after it. The live region never holds
        # a finished row.
        if record.settled and previous != record.status:
            self._print_row(record)
        if self.live:
            self._invalidate()
        elif event.type is EventType.TOOL_INTENT:
            self._line(f"Tool {record.name}: requested")

    # -- rendering -------------------------------------------------------

    def render_lines(self, *, width: int = 80, rows: int = 24) -> list[str]:
        """Plain-text projection of the live region.

        The live region only ever holds the status line and the streamed text
        of the current model call. Settled tool calls are already printed.
        """
        buffer = "".join(text for _, text in self._live_fragments(width=width, rows=rows))
        lines = buffer.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return [line.rstrip() for line in lines]

    def _live_fragments(self, *, width: int, rows: int) -> list[tuple[str, str]]:
        fragments: list[tuple[str, str]] = []
        if self._armed_until is not None and self._clock() > self._armed_until:
            self._armed_until = None
            self._stop_hint = False
        if self._stop_hint and self._phase != IDLE:
            fragments.append(("fg:ansiyellow bold", _fit(STOP_HINT, width) + "\n"))
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
            style = _PHASE_STYLES.get(self._phase, _PHASE_STYLES[THINKING])
            fragments.append((style, _fit(f"{frame} {label} {elapsed:.1f}s", width) + "\n"))

        preview = self._stream_preview(width)
        if preview:
            reserved = 3  # status line + session line + input line
            if self._stop_hint and self._phase != IDLE:
                reserved += 1
            available = max(1, rows - reserved)
            for line in preview[-available:]:
                for style, text in line:
                    fragments.append((style, text))
                fragments.append(("", "\n"))
        return fragments

    def _size(self) -> tuple[int, int]:
        if self._size_provider is not None:
            return self._size_provider()
        from prompt_toolkit.application import get_app

        size = get_app().output.get_size()
        return size.columns, size.rows

    def _stream_preview(self, width: int) -> list[list[tuple[str, str]]]:
        """Markdown-rendered live preview of the streamed body text.

        Rendering Markdown costs a few milliseconds per frame, so the result is
        cached by width and content. A very long reply previews its tail only;
        the complete text is printed once by AgentOutput when the call ends.
        """
        text = self._stream_text
        if not text:
            return []
        if len(text) > _PREVIEW_CHARS:
            text = text[-_PREVIEW_CHARS:]
        key = (len(text), hash(text), width)
        if key == self._preview_key:
            return self._preview_lines
        if (
            self._preview_cost
            and self._clock() - self._preview_at < self._preview_cost * 4
        ):
            # Too soon after an expensive render: keep the last frame and let
            # the next one catch up. The final block is printed in full anyway.
            return self._preview_lines
        started = self._clock()
        try:
            self._preview_lines = markdown_lines(text, width)
        except Exception:
            # A render failure must not blank the stream: fall back to plain
            # wrapped text at the same width.
            self._preview_lines = [
                [("fg:ansibrightblack", line)]
                for line in _wrap_display(display_text(text), max(1, width - 1))
            ]
        self._preview_key = key
        self._preview_at = started
        self._preview_cost = self._clock() - started
        return self._preview_lines

    def _print_row(self, record: _ToolRecord) -> None:
        """Print one settled call as a single line, in place of its event."""
        from rich.text import Text

        style = _TOOL_STYLES.get(record.status, "fg:ansibrightblack")
        row = Text("  ")
        row.append(_TOOL_MARKERS.get(record.status, "?"), style=style)
        row.append(" " + record.name, style="bold")
        if record.detail:
            row.append(" · " + record.detail, style="fg:ansibrightblack")
        if record.status != "succeeded":
            row.append(" " + record.status, style=style)
        if record.ended is not None:
            row.append(
                f" · {max(0.0, record.ended - record.started):.1f}s",
                style="fg:ansibrightblack",
            )
        if record.error:
            row.append(" · " + _fit(record.error, 80), style=style)
        self._console.print(row)

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
