"""The interactive prompt: history, completion, multi-line input, big pastes folded into a placeholder, and a
status line underneath. Inline, so the terminal's own scrollback stays yours.

Imported only when there is a terminal to draw on (see mat._make_prompt_ui); without prompt_toolkit or a
terminal, mat.py reads lines the plain way.
"""
from __future__ import annotations

import time
from typing import Callable, Sequence

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.styles import Style

from ixel_mat.theme import C, PROMPT_ARROW, moon
from ixel_mat.tui import Status, complete, rule_fragments

EXIT_WINDOW = 2.0  # seconds in which a second ctrl+c exits

STYLE = Style.from_dict({
    "rule": C["dim"],
    "rule.text": C["dim"],
    "rule.mark": C["violet"],
    "rule.mode": C["blue"],
    "rule.warn": C["gold"],
    "prompt.arrow": C["dim"],
    "completion-menu": f"bg:{C['navy']} {C['moon']}",
    "completion-menu.completion.current": f"bg:{C['violet']} {C['bg']}",
    "completion-menu.meta.completion": f"bg:{C['navy']} {C['dim']}",
    "completion-menu.meta.completion.current": f"bg:{C['violet']} {C['bg']}",
    "scrollbar.background": f"bg:{C['navy']}",
    "scrollbar.button": f"bg:{C['dim']}",
})

MENU_ROWS = 8  # kept free under the prompt for the completion menu


class _Completer(Completer):
    def __init__(self, agents: Callable[[], Sequence[str]]):
        self._agents = agents

    def get_completions(self, document: Document, complete_event: CompleteEvent):
        explicit = bool(complete_event.completion_requested)
        for s in complete(document.text_before_cursor, self._agents(), explicit):
            yield Completion(s.text, start_position=s.back, display_meta=s.meta)


class PromptUI:
    """Reads one submission at a time. `status` says what the line underneath shows; `agents` names the
    panel (for --moderator); `placeholder(text)` is what stands in for a big paste in the box, or None to
    paste it as it is (the text itself is what read() returns)."""

    def __init__(self, status: Callable[[], Status], agents: Callable[[], Sequence[str]],
                 placeholder: Callable[[str], str | None], input=None, output=None):
        self._status = status
        self._placeholder = placeholder
        self._pastes: dict[str, str] = {}
        self._exit_armed_until = 0.0
        self.session = PromptSession(
            message=self._message,
            prompt_continuation=self._continuation,
            completer=_Completer(agents),
            complete_while_typing=True,
            history=InMemoryHistory(),  # this session's questions only: nothing is written to disk
            key_bindings=self._bindings(),
            style=STYLE,
            reserve_space_for_menu=MENU_ROWS,
            input=input,
            output=output,
        )

    # ── what's drawn ──

    @staticmethod
    def _columns() -> int:
        return get_app().output.get_size().columns

    def _message(self) -> FormattedText:
        # A rule across the terminal sets the box apart from what came before it, and carries the status.
        # (A bottom toolbar would sit at the bottom of the terminal, far from the line it describes.)
        armed = time.monotonic() < self._exit_armed_until
        status = self._status()
        rule = rule_fragments(status, self._columns(), armed, get_app().is_done)
        glyph, color = moon(status.follow_ups)  # grows with the conversation; stays in the history as it was
        return FormattedText([*rule, ("", "\n"), (f"fg:{color}", f"  {glyph} "),
                              ("class:prompt.arrow", f"{PROMPT_ARROW} ")])

    @staticmethod
    def _continuation(width: int, line_number: int, wrap_count: int) -> FormattedText:
        return FormattedText([("class:prompt.arrow", "  …".ljust(width))])

    # ── keys ──

    def _bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("c-j")
        @kb.add("escape", "enter")
        def _newline(event):  # ctrl+j or alt+enter: a new line instead of sending
            event.current_buffer.insert_text("\n")

        @kb.add("enter", filter=Condition(lambda: get_app().current_buffer.document.text_before_cursor.endswith("\\")))
        def _continue(event):  # a line ending in \ goes on to the next, as in a shell
            event.current_buffer.delete_before_cursor(1)
            event.current_buffer.insert_text("\n")

        @kb.add(Keys.BracketedPaste)
        def _paste(event):
            data = event.data.replace("\r\n", "\n").replace("\r", "\n")
            shown = self._placeholder(data)
            if shown:
                self._pastes[shown] = data
            event.current_buffer.insert_text(shown or data)

        @kb.add("c-c")
        def _interrupt(event):
            # like a shell: the first press clears what's typed; on an empty line, a second one within a
            # moment exits (ctrl+c while a review runs cancels it instead; see mat._interruptible)
            buffer = event.current_buffer
            if buffer.text:
                buffer.reset()
                self._exit_armed_until = 0.0
                return
            now = time.monotonic()
            if now < self._exit_armed_until:
                event.app.exit(exception=EOFError)
                return
            self._exit_armed_until = now + EXIT_WINDOW
            event.app.invalidate()
            event.app.loop.call_later(EXIT_WINDOW, event.app.invalidate)

        return kb

    async def read(self) -> str:
        """The next submission, with any folded paste put back. Raises EOFError on ctrl+d or ctrl+c twice.
        Pastes are kept for the session (their numbers don't repeat), so a line recalled with the up arrow
        still sends the text it stands for."""
        text = await self.session.prompt_async()
        for shown, data in self._pastes.items():
            text = text.replace(shown, data)
        return text
