"""Questions in the terminal (ixel setup, the plain prompt): the line editor draws the prompt, so backspace and
the arrow keys stay inside the answer; pipes, hidden answers and Windows go Rich's own way."""
import io
import re
import sys
import types
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text

from ixel_mat import asking
from ixel_mat.asking import Confirm, Prompt

# setup's question, in colors no other test draws: Rich keeps the codes it made for a color, whatever terminal
# it draws for next, and the truecolor ones would leak into tests of 16-color output
VERDICT = "  [#a1b2c3]Who writes the verdict?[/] [#4d5e6f](auto = best-rated answer's author)[/]"


class Tty(io.StringIO):
    def isatty(self):
        return True


def start_terminal(monkeypatch):
    """stdin and stdout are a terminal (from inside the test: pytest sets its own stdout after fixtures); what
    Rich prints itself lands in the returned buffer."""
    out = Tty()
    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr(sys, "stdout", out)
    # A macOS or Linux terminal, wherever the tests run: Windows' console goes Rich's way (its own test, below)
    monkeypatch.setattr(asking, "os", types.SimpleNamespace(name="posix"))
    return out


def colors():
    return Console(force_terminal=True, color_system="truecolor", width=200)


def fake_editor(monkeypatch, *answers):
    """Stands in for prompt_toolkit: records the prompt line it was given, and answers in turn."""
    lines, replies = [], iter(answers)

    class Editor:
        def prompt(self):
            reply = next(replies)
            if isinstance(reply, BaseException):
                raise reply
            return reply

    monkeypatch.setattr(asking, "_editor", lambda line, console: lines.append(line) or Editor())
    return lines


def no_editor(monkeypatch):
    def refuse(line, console):
        raise AssertionError("the line editor was used")
    monkeypatch.setattr(asking, "_editor", refuse)


def plain_input(monkeypatch, *answers):
    """builtins.input, as Rich calls it (with no prompt: Rich has printed it)."""
    calls, replies = [], iter(answers)
    monkeypatch.setattr("builtins.input", lambda *args: calls.append(args) or next(replies))
    return calls


# ── on a terminal ─────────────────────────────────────────────────────────────

def test_on_a_terminal_the_editor_draws_the_prompt_rich_would_have_printed(monkeypatch):
    terminal = start_terminal(monkeypatch)
    lines = fake_editor(monkeypatch, "codex")
    answer = Prompt.ask(VERDICT, choices=["auto", "codex"], default="auto", console=colors())
    assert answer == "codex" and terminal.getvalue() == ""  # Rich printed nothing itself
    console = colors()
    with console.capture() as rich:
        console.print(Prompt(VERDICT, console=console, choices=["auto", "codex"]).make_prompt("auto"), end="")
    assert lines == [rich.get()]  # the same words in the same colors
    assert "\x1b[38;2;161;178;195m" in lines[0]
    assert Text.from_ansi(lines[0]).plain == ("  Who writes the verdict? (auto = best-rated answer's author) "
                                              "[auto/codex] (auto): ")


def test_choices_defaults_and_wrong_answers_work_as_before(monkeypatch):
    terminal = start_terminal(monkeypatch)
    lines = fake_editor(monkeypatch, "nope", "", "maybe", "y")
    assert Prompt.ask(VERDICT, choices=["auto", "codex"], default="auto", console=colors()) == "auto"
    assert "Please select one of the available options" in terminal.getvalue() and len(lines) == 2
    assert Confirm.ask("  Use triage?", default=False, console=colors()) is True
    assert "Please enter Y or N" in terminal.getvalue()


def test_a_long_prompt_prints_its_first_lines_and_edits_after_the_last(monkeypatch):
    terminal = start_terminal(monkeypatch)
    lines = fake_editor(monkeypatch, "auto")
    Prompt.ask(VERDICT, choices=["auto", "codex"], default="auto", console=Console(force_terminal=True, width=40))
    above = Text.from_ansi(terminal.getvalue()).plain
    assert above.startswith("  Who writes the verdict?") and above.endswith("\n")
    assert "\n" not in lines[0] and Text.from_ansi(lines[0]).plain.endswith("(auto): ")


def test_the_real_editor_keeps_backspace_and_arrows_inside_the_answer(monkeypatch):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output.vt100 import Vt100_Output

    start_terminal(monkeypatch)
    screen = io.StringIO()
    output = Vt100_Output(screen, lambda: Size(rows=24, columns=200))
    with create_pipe_input() as keys, create_app_session(input=keys, output=output):
        keys.send_text("x" + "\x7f" * 5 + "cdex" + "\x1b[D" * 3 + "o\r")  # backspace past the start, ← ← ←, o
        answer = Prompt.ask(VERDICT, choices=["auto", "codex"], default="auto", console=colors())
    assert answer == "codex"
    assert "38;2;161;178;195mWho writes the verdict?" in screen.getvalue()  # drawn by the editor, in Rich's colors


def test_ctrl_c_and_ctrl_d_still_stop_the_question(monkeypatch):
    start_terminal(monkeypatch)
    fake_editor(monkeypatch, KeyboardInterrupt(), EOFError())
    with pytest.raises(KeyboardInterrupt):
        Prompt.ask("  Model", console=colors())
    with pytest.raises(EOFError):
        Prompt.ask("  Model", console=colors())


def test_if_the_editor_cant_start_or_stops_the_question_is_asked_the_plain_way(monkeypatch):
    def cant(line, console):
        raise RuntimeError("not a terminal prompt_toolkit can drive")

    terminal = start_terminal(monkeypatch)
    monkeypatch.setattr(asking, "_editor", cant)
    calls = plain_input(monkeypatch, "gpt-5.5", "opus")
    assert Prompt.ask("  Model", console=colors()) == "gpt-5.5"
    fake_editor(monkeypatch, OSError("the terminal went away"))
    assert Prompt.ask("  Model", console=colors()) == "opus"
    assert calls == [(), ()] and Text.from_ansi(terminal.getvalue()).plain == "  Model: \n  Model: "


# ── Rich's own way ────────────────────────────────────────────────────────────

def test_piped_answers_are_read_as_before(monkeypatch):
    no_editor(monkeypatch)
    buf = io.StringIO()
    console = Console(file=buf, width=200)
    assert Prompt.ask(VERDICT, choices=["auto", "codex"], default="auto", console=console,
                      stream=io.StringIO("nope\n\n")) == "auto"
    assert "Please select one of the available options" in buf.getvalue()
    calls = plain_input(monkeypatch, "y")  # not a terminal: Rich prints the prompt, input() reads the line
    assert Confirm.ask("  Use triage?", default=False, console=console) is True and calls == [()]
    assert "Use triage? [y/n] (n): " in buf.getvalue()


def test_hidden_answers_go_through_getpass_even_on_a_terminal(monkeypatch):
    terminal = start_terminal(monkeypatch)
    no_editor(monkeypatch)
    hidden = []
    monkeypatch.setattr("getpass.getpass", lambda prompt="", stream=None: hidden.append(prompt) or "sk-1")
    assert Prompt.ask("  TYPESAFE_API_KEY", password=True, console=colors()) == "sk-1"
    assert hidden == [""] and Text.from_ansi(terminal.getvalue()).plain == "  TYPESAFE_API_KEY: "


def test_on_windows_the_console_edits_the_line_as_before(monkeypatch):
    terminal = start_terminal(monkeypatch)
    no_editor(monkeypatch)
    monkeypatch.setattr(asking, "os", types.SimpleNamespace(name="nt"))
    calls = plain_input(monkeypatch, "codex")
    assert Prompt.ask(VERDICT, choices=["auto", "codex"], default="auto", console=colors()) == "codex"
    assert calls == [()] and "(auto): " in Text.from_ansi(terminal.getvalue()).plain


def test_every_question_in_the_terminal_goes_through_asking():
    # Rich's own Prompt and Confirm print the prompt themselves: their answers can be backspaced into it
    package = Path(asking.__file__).parent
    users = sorted(p.relative_to(package).as_posix() for p in package.rglob("*.py")
                   if re.search(r"rich\.prompt|from rich import .*\bprompt\b", p.read_text(encoding="utf-8")))
    assert users == ["asking.py"]


def test_the_plain_prompt_leaves_the_rest_of_a_paste_for_it_to_read(monkeypatch):
    # mat's plain prompt reads a paste's other lines from stdin after the first: an editor would take them
    start_terminal(monkeypatch)
    monkeypatch.setattr(asking, "_editor", lambda *args: pytest.fail("the plain prompt used the editor"))
    calls = plain_input(monkeypatch, "one")
    assert asking.LinePrompt.ask("  >", console=colors()) == "one" and calls == [()]
