"""Questions asked in the terminal (ixel setup, and the plain prompt): Rich's Prompt and Confirm, with the
answer typed into a line editor that knows where the prompt ends.

Rich prints a prompt, then reads the answer with input(). The terminal then edits the line by bytes and knows
nothing of the prompt: é is two bytes, so backspacing over it rubs out the end of the prompt on screen, and
an arrow key types ^[[D into the answer. prompt_toolkit (the main prompt's editor) draws the same prompt in
the same colors, and keeps backspace and the arrow keys inside the answer.
"""
from __future__ import annotations

import os
import sys

from rich import prompt as rich_prompt
from rich.console import Console
from rich.text import TextType


def _editor(prompt: str, console: Console):
    """A prompt_toolkit session that shows `prompt` (Rich's rendering of it) in the colors Rich used."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.output import ColorDepth
    depth = {"truecolor": ColorDepth.TRUE_COLOR, "256": ColorDepth.DEPTH_8_BIT,
             "standard": ColorDepth.DEPTH_4_BIT}.get(console.color_system or "", ColorDepth.DEPTH_1_BIT)
    return PromptSession(ANSI(prompt), color_depth=depth)


def ask(console: Console, prompt: TextType, password: bool = False, stream=None) -> str:
    """One line from the user, after the prompt as Rich draws it. Hidden answers (getpass), answers from a
    stream or a pipe, and Windows, whose console already keeps backspace inside the answer, go Rich's way."""
    if password or stream is not None or os.name == "nt" or console.file is not sys.stdout \
            or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return console.input(prompt, password=password, stream=stream)
    with console.capture() as captured:
        console.print(prompt, end="")
    # Rich wraps a long prompt: the lines above the last are printed as they are, the last goes with the answer
    above, newline, line = captured.get().rpartition("\n")
    try:
        editor = _editor(line, console)
    except Exception:  # not installed, or a terminal it can't drive
        return console.input(prompt)
    if above:
        console.file.write(above + newline)
        console.file.flush()
    try:
        return editor.prompt()
    except (EOFError, KeyboardInterrupt):
        raise
    except Exception:  # it stopped part way: ask again the plain way, on a line of its own
        console.print()
        return console.input(prompt)


class _Ask:
    """Rich's prompts read every answer, the wrong ones asked again too, through get_input."""

    @classmethod
    def get_input(cls, console: Console, prompt: TextType, password: bool, stream=None) -> str:
        return ask(console, prompt, password, stream)


class Prompt(_Ask, rich_prompt.Prompt):
    pass


class Confirm(_Ask, rich_prompt.Confirm):
    pass


class LinePrompt(rich_prompt.Prompt):
    """Rich's own way, for the plain prompt that reads the rest of a paste from stdin after the first line
    (mat.read_burst_submission): an editor would take those lines first."""
