"""Terminal input ownership, independent of Agent execution and commands.

The prompt session is also the single live-region owner when a display is
attached: its bottom toolbar shows the turn status and the tool area, and its
own refresh timer animates them. A menu needs exclusive stdin, so
:meth:`TerminalInput.pause` ends the active prompt and waits for it to stop
before the caller opens a second prompt_toolkit application; :meth:`resume`
starts a fresh prompt afterwards.

Without a terminal (redirected stdin/stdout) the same interface reads plain
lines. That path creates no prompt_toolkit session, so it emits no escape
sequence, no cursor movement and no animation.
"""

from __future__ import annotations

import asyncio
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from PhyAgentOS.config.paths import get_cli_history_path

# PromptSession's default UI style declares ``bottom-toolbar`` as ``reverse``,
# which paints a full-width block behind the session/model line and the tool
# rows. Override only those two class names with the terminal's default
# background; per-line foreground colours stay inline and the global theme is
# untouched.
_TOOLBAR_STYLE = Style.from_dict(
    {
        "bottom-toolbar": "noreverse bg:default",
        "bottom-toolbar.text": "noreverse bg:default",
    }
)


class TerminalInput:
    def __init__(self, display=None, *, input=None, output=None):
        self.prompt = None
        self._display = display
        self._input = input
        self._output = output
        self._interactive = None if input is None and output is None else True
        self._saved_attrs = None
        self._gate = asyncio.Event()
        self._gate.set()
        self._reading = False
        self._pause_lock = asyncio.Lock()
        self._draft = ""
        self._on_stop = None

    @property
    def interactive(self) -> bool:
        if self._interactive is None:
            self._interactive = sys.stdin.isatty() and sys.stdout.isatty()
        return self._interactive

    @property
    def menu_capable(self) -> bool:
        """A selector may take over stdin only when a terminal owns it."""
        return self.interactive

    @property
    def prompt_active(self) -> bool:
        """True while the prompt is actually reading keys."""
        return self._reading

    def initialize(self):
        if not self.interactive:
            return
        try:
            import termios
            self._saved_attrs = termios.tcgetattr(sys.stdin.fileno())
        except Exception:
            pass
        history_file = get_cli_history_path()
        history_file.parent.mkdir(parents=True, exist_ok=True)
        self.prompt = PromptSession(
            history=FileHistory(str(history_file)),
            enable_open_in_editor=False,
            multiline=False,
            input=self._input,
            output=self._output,
            bottom_toolbar=self._toolbar,
            refresh_interval=0.1,
            style=_TOOLBAR_STYLE,
            key_bindings=self._key_bindings(),
        )
        # The lone-Esc hint is useless if the key disambiguation waits for the
        # 1s default. Keep it short; arrow/meta sequences are still parsed as
        # their own keys before this timeout.
        self.prompt.app.timeoutlen = 0.3
        if self._display is not None:
            # Official Buffer flag: edits are refused while busy; the accept
            # handler below also refuses Enter so no text is submitted silently.
            self.prompt.default_buffer.read_only = Condition(self._busy)
            self.prompt.default_buffer.on_cursor_position_changed += self._on_cursor
            original_accept = self.prompt.default_buffer.accept_handler

            def accept(buffer):
                if self._busy():
                    self._disarm()
                    # Keep the text if this path is ever reached; the blocked
                    # Enter binding above normally prevents it entirely.
                    return True
                return original_accept(buffer) if original_accept is not None else False

            self.prompt.default_buffer.accept_handler = accept
            self._display.attach_prompt(self.prompt)

    def _on_cursor(self, buffer) -> None:
        if self._busy():
            self._disarm()

    def bind_stop(self, handler) -> None:
        """Inject the local stop request; never reach into the agent privately."""
        self._on_stop = handler

    def _toolbar(self):
        if self._display is None:
            return ""
        return self._display.toolbar()

    def _message(self):
        # While a turn runs, the session line stays above the input and the
        # "You:" marker is hidden. The same prompt keeps the draft; ordinary
        # keys are blocked by the key bindings/read-only buffer.
        if self._display is not None:
            return self._display.prompt_message()
        return HTML("<b fg='ansiblue'>You:</b> ")

    def _busy(self) -> bool:
        return self._display is not None and self._display.busy

    def _disarm(self) -> None:
        if self._display is not None:
            self._display.disarm_stop_hint()

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()
        busy = Condition(self._busy)

        @bindings.add("enter", filter=busy)
        def blocked_enter(event):
            # Never reach the accept handler: Buffer.validate_and_handle would
            # reset the draft when the accept is refused.
            self._disarm()

        @bindings.add("escape", filter=busy)
        def stop_or_arm(event):
            if self._display is None:
                return
            if self._display.arm_stop_hint():
                handler = self._on_stop
                if handler is not None:
                    handler()

        @bindings.add(Keys.Any, filter=busy)
        def blocked_key(event):
            # Busy keys never reach the buffer or the model queue. Any key
            # other than the confirming Esc clears the armed stop.
            self._disarm()

        # Explicit movement keys: arrows are their own keys, never a second
        # Esc, and they also clear an armed stop even when the cursor cannot
        # move (for example, down on an empty single-line buffer).
        def blocked_motion(event):
            self._disarm()

        for key in (
            "up", "down", "left", "right", "home", "end",
            "backspace", "delete", "tab",
            "c-a", "c-b", "c-e", "c-f", "c-k", "c-u", "c-w",
        ):
            bindings.add(key, filter=busy)(blocked_motion)

        return bindings

    async def read(self):
        await self._gate.wait()
        if self.prompt is None:
            return await self._read_plain()
        self._reading = True
        draft, self._draft = self._draft, ""
        try:
            # Raw passthrough preserves the ANSI styling Rich emits. Vt100_Output's
            # safe ``write`` replaces ESC with "?", which turned styled output into
            # visible "?[36m" text. Untrusted model/tool text is stripped of
            # control characters by ``AgentOutput`` before it reaches this proxy.
            with patch_stdout(raw=True):
                return await self.prompt.prompt_async(self._message, default=draft)
        finally:
            self._reading = False

    async def _read_plain(self) -> str:
        def read_line() -> str:
            sys.stdout.write("You: ")
            sys.stdout.flush()
            line = sys.stdin.readline()
            if not line:
                raise EOFError
            return line

        return await asyncio.to_thread(read_line)

    async def pause(self) -> None:
        """Release stdin: end the active prompt before a menu takes over."""
        async with self._pause_lock:
            self._gate.clear()
            captured = False
            while self._reading and self.prompt is not None:
                # The prompt may still be starting; retry until it is running
                # and its exit request lands. Keep a partially typed draft so
                # the menu does not throw away the user's unfinished line.
                try:
                    if self.prompt.app.is_running:
                        if not captured:
                            try:
                                self._draft = self.prompt.default_buffer.text
                            except Exception:
                                self._draft = ""
                            captured = True
                        self.prompt.app.exit(result="")
                except Exception:
                    pass
                await asyncio.sleep(0.005)

    def resume(self) -> None:
        self._gate.set()

    def restore(self):
        if self._saved_attrs is not None:
            try:
                import termios
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved_attrs)
            except Exception:
                pass
