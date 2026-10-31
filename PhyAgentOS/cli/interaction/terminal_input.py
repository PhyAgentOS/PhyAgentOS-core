"""Terminal input ownership, independent of Agent execution and commands.

The prompt session is also the single live-region owner when a display is
attached: the live status and tool rows are part of its prompt message, right
above the input line, and its own refresh timer animates them. A menu needs
exclusive stdin, so :meth:`TerminalInput.pause` ends the active prompt and
waits for it to stop before the caller opens a second prompt_toolkit
application; :meth:`resume` starts a fresh prompt afterwards.

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

from PhyAgentOS.cli.interaction.terminal_display import REQUEST_PROMPT
from PhyAgentOS.config.paths import get_cli_history_path


def _fragment_rows(fragments) -> int:
    """Rows the prompt block occupies, including the input line."""
    rows = 0
    for _, text in fragments:
        rows += text.count("\n")
        if not text.endswith("\n"):
            rows += 1
    if fragments and fragments[-1][1].endswith("\n"):
        # The input line follows the message on its own row.
        rows += 1
    return max(1, rows)


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
        self._prompt_rows = 1
        self._on_stop = None
        # A correction editor runs inside the same prompt. It never submits a
        # normal chat line: Enter is accepted only for /correct with text, and
        # the original draft is restored before the next read.
        self._correcting = False
        self._correction_saved_draft = ""

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
            refresh_interval=0.1,
            key_bindings=self._key_bindings(),
        )
        # The lone-Esc hint is useless if the key disambiguation waits for the
        # 1s default. Keep it short; arrow/meta sequences are still parsed as
        # their own keys before this timeout.
        self.prompt.app.timeoutlen = 0.3
        if self._display is not None:
            # Official Buffer flag: edits are refused while busy; the accept
            # handler below also refuses Enter so no text is submitted silently.
            # The correction editor is the one exception: it is local input for
            # /correct, never a normal task.
            self.prompt.default_buffer.read_only = Condition(self._blocked)
            self.prompt.default_buffer.on_cursor_position_changed += self._on_cursor
            original_accept = self.prompt.default_buffer.accept_handler

            def accept(buffer):
                def finish():
                    handled = (
                        original_accept(buffer)
                        if original_accept is not None
                        else True
                    )
                    # PromptSession's default accept keeps text so it can be
                    # reset later. Clear it now, after the result was captured;
                    # otherwise a following read can briefly inherit the
                    # previous line instead of the staged draft.
                    try:
                        buffer.reset()
                    except Exception:
                        pass
                    return handled

                if self._correcting:
                    # The Enter binding handles valid corrections. This wrapper
                    # is the fallback: invalid text stays in the editor and is
                    # never returned as an ordinary prompt result.
                    if self._valid_correction(buffer.text.strip()):
                        self._draft = self._correction_saved_draft
                        self._correcting = False
                        self._correction_saved_draft = ""
                        return finish()
                    return True
                if self._blocked():
                    self._disarm()
                    # Keep the text if this path is ever reached; the blocked
                    # Enter binding above normally prevents it entirely.
                    return True
                return finish()

            self.prompt.default_buffer.accept_handler = accept
            self._display.attach_prompt(self.prompt)
            # The display hides the cursor while busy. The correction editor
            # needs it visible even though the turn is still running.
            self._install_cursor_policy()

    def _on_cursor(self, buffer) -> None:
        if self._blocked():
            self._disarm()

    def bind_stop(self, handler) -> None:
        """Inject the local stop request; never reach into the agent privately."""
        self._on_stop = handler

    def _message(self):
        fragments = self._prompt_fragments()
        # Remember the rendered height so the submitted prompt block can be
        # erased exactly, even if the display state changes while the prompt
        # is finishing.
        self._prompt_rows = _fragment_rows(fragments)
        return fragments

    def _prompt_fragments(self):
        # While a turn runs, the session line stays above the input and the
        # request marker is hidden. The same prompt keeps the draft; ordinary
        # keys are blocked by the key bindings/read-only buffer. The correction
        # editor gets its own visible marker and cursor.
        if self._correcting:
            fragments = []
            if self._display is not None:
                fragments = [
                    (style, text)
                    for style, text in self._display.prompt_message()
                    if text != REQUEST_PROMPT
                ]
            fragments.append(("bold fg:ansiyellow", "Correct: "))
            return fragments
        if self._display is not None:
            return self._display.prompt_message()
        return HTML(f"<b fg='ansicyan'>{REQUEST_PROMPT.strip()}</b> ")

    def _erase_submitted_prompt(self) -> None:
        """Remove the submitted prompt block before the permanent request.

        The prompt is erased only after ``prompt_async`` has finished its final
        render, so the permanent request block printed next cannot be caught by
        a late erase. ``erase_when_done`` did race with that block and is not
        used. Leave the block alone when the reader is pausing for a menu.
        """
        if self.prompt is None or not self._gate.is_set():
            return
        try:
            output = self.prompt.app.output
            for _ in range(max(1, self._prompt_rows)):
                output.cursor_up(1)
                output.write_raw("\r")
                output.erase_end_of_line()
            output.flush()
        except Exception:
            pass

    def _busy(self) -> bool:
        return self._display is not None and self._display.busy

    def _blocked(self) -> bool:
        """True when ordinary typing must not reach the buffer.

        The correction editor is deliberately exempt so a real TTY can type a
        correction while a turn is running.
        """
        return self._busy() and not self._correcting

    def _valid_correction(self, text: str) -> bool:
        command, _separator, argument = text.partition(" ")
        return command == "/correct" and bool(argument.strip())

    def _install_cursor_policy(self) -> None:
        """Keep the cursor hidden while busy except in the correction editor."""
        if self.prompt is None:
            return
        try:
            from prompt_toolkit.filters import Condition
            from prompt_toolkit.layout.controls import BufferControl

            hide = Condition(self._blocked)
            for window in self.prompt.layout.find_all_windows():
                control = window.content
                if (
                    isinstance(control, BufferControl)
                    and control.buffer is self.prompt.default_buffer
                ):
                    window.always_hide_cursor = hide
        except Exception:
            pass

    def _enter_correction(self, buffer) -> None:
        """Replace the visible line with a /correct editor and save the draft."""
        self._disarm()
        self._correction_saved_draft = buffer.text
        self._correcting = True
        text = "/correct "
        buffer.text = text
        buffer.cursor_position = len(text)

    def _disarm(self) -> None:
        if self._display is not None:
            self._display.disarm_stop_hint()

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()
        busy = Condition(self._blocked)
        correcting = Condition(lambda: self._correcting)

        @bindings.add("c-g", filter=busy)
        def start_correction(event):
            # Real TTYs have no other way to type while a turn is busy: the
            # buffer is read-only and Keys.Any is filtered. Ctrl+G opens a
            # dedicated local editor instead of changing that blocking rule.
            self._enter_correction(event.current_buffer)

        @bindings.add("enter", filter=correcting)
        def submit_correction(event):
            text = event.current_buffer.text.strip()
            if not self._valid_correction(text):
                # Enter never turns an incomplete or ordinary line into a
                # normal task while the correction editor is open.
                event.app.invalidate()
                return
            self._draft = self._correction_saved_draft
            self._correcting = False
            self._correction_saved_draft = ""
            event.app.exit(result=text)

        @bindings.add("escape", filter=correcting)
        def cancel_correction(event):
            # Esc only leaves the correction editor. It does not arm or trigger
            # the busy stop, and the original draft comes back untouched.
            event.current_buffer.text = self._correction_saved_draft
            event.current_buffer.cursor_position = len(
                self._correction_saved_draft
            )
            self._correcting = False
            self._correction_saved_draft = ""
            self._disarm()
            event.app.invalidate()

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
        value = ""
        try:
            # Raw passthrough preserves the ANSI styling Rich emits. Vt100_Output's
            # safe ``write`` replaces ESC with "?", which turned styled output into
            # visible "?[36m" text. Untrusted model/tool text is stripped of
            # control characters by ``AgentOutput`` before it reaches this proxy.
            with patch_stdout(raw=True):
                value = await self.prompt.prompt_async(self._message, default=draft)
        finally:
            self._reading = False
            # A paused/aborted prompt must not leave the correction editor
            # active for the next read. A normal Enter already restored the
            # saved draft before exiting; this only covers external exit paths.
            if self._correcting and self._correction_saved_draft is not None:
                self._draft = self._correction_saved_draft
            self._correcting = False
            self._correction_saved_draft = ""
            if self.prompt is not None:
                try:
                    # The accept handler keeps text by design. Clear the
                    # finished line here so the next prompt starts from the
                    # staged draft rather than the submitted text.
                    self.prompt.default_buffer.reset()
                except Exception:
                    pass
        if value:
            self._erase_submitted_prompt()
        return value

    def prefill(self, text: str) -> bool:
        """Stage text as the next prompt's default input.

        The running prompt already took its default, so this only takes effect
        when it is called after a prompt returned and before the next
        :meth:`read`; it never rewrites the prompt that is currently reading.
        Returns False when there is no prompt session (redirected input), where
        nothing can be refilled.
        """
        if self.prompt is None:
            return False
        self._draft = text
        return True

    async def _read_plain(self) -> str:
        def read_line() -> str:
            # No prompt marker is written here: on a redirected stream the
            # marker cannot share the line with the input, and the permanent
            # request block printed after submit already shows the text once.
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
