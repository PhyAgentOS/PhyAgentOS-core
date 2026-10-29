"""Terminal presentation of core results, without runtime or tool ownership."""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from PhyAgentOS import __logo__
from PhyAgentOS.agent.core.events import RuntimeEvent
from PhyAgentOS.agent.core.types import TurnResult, TurnStatus
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


class AgentOutput:
    """Render each terminal result once; never infer remote action state.

    The display owns the live region (turn status and this turn's tool rows).
    This class only decides what a message looks like: replies carry the
    product title, notices are dim, errors are red. Replies are the only place
    a green/success-looking treatment is avoided entirely; tool success never
    stands in for task success.
    """

    def __init__(self, console: Console, *, markdown: bool = True) -> None:
        self._console = console
        self._markdown = markdown
        self._shown: set[tuple[str, str]] = set()
        self.display = TerminalDisplay(console)

    # -- live region -----------------------------------------------------

    def attach_prompt(self, prompt) -> None:
        self.display.attach_prompt(prompt)

    def set_session_label(self, label: str) -> None:
        self.display.set_session_label(label)

    def startup(self, detail: str = "Forge recovery") -> None:
        self.display.set_phase(PREPARING, detail)

    def ready(self) -> None:
        self.display.set_phase(IDLE)

    def turn_started(self) -> None:
        self.display.begin_turn()

    def turn_finished(self) -> None:
        self.display.end_turn()

    def cancel_requested(self) -> None:
        self.display.request_stop()

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
        self.display.end_turn()
        content = display_text(visible_text(result.reply))
        if content.strip():
            self._console.print()
            self._console.print(Text(f"{__logo__} PhyAgentOS:", style="cyan"))
            self._console.print(Markdown(content) if self._markdown else Text(content))
            self._console.print()
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

    def event(self, event: RuntimeEvent) -> None:
        """Consume persisted lifecycle events; never arguments or outcomes."""
        self.display.note_event(event)
