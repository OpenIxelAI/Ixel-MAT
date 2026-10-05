"""The terminal's look and feel: welcome card, completion, the rule above the prompt, the prompt's keys,
the live progress view, and ctrl+c cancelling a run."""
import asyncio
import io
import os
import signal
import sys
import time

import pytest
from rich.console import Console

from ixel_mat import mat, review_ui, theme, tui
from ixel_mat.commands import COMMANDS
from ixel_mat.modes.review import ReviewEvent, ReviewMode

AGENTS = ["claude", "gpt", "gemini"]


# ── completion ────────────────────────────────────────────────────────────────

def texts(suggestions):
    return [s.text for s in suggestions]


def test_a_slash_offers_every_terminal_command_with_what_it_does():
    suggestions = tui.complete("/")
    assert texts(suggestions) == [cmd.usage.split()[0] for cmd in COMMANDS if cmd.mode in ("mat", "both")]
    assert all(s.meta for s in suggestions) and all(s.back == -1 for s in suggestions)


def test_a_command_is_completed_by_its_first_letters():
    assert texts(tui.complete("/re")) == ["/review"]
    assert texts(tui.complete("/s")) == ["/saver", "/saves"]
    assert tui.complete("/re")[0].back == -3  # replaces what's been typed of it
    assert tui.complete("/nope") == []


def test_a_plain_question_or_a_path_inside_one_offers_nothing():
    assert tui.complete("what is /re") == []
    assert tui.complete("") == []
    assert tui.complete("/review why\n/re") == []  # only the first line can hold a command


def test_options_complete_after_the_command_and_stop_once_the_question_starts():
    assert texts(tui.complete("/review --q")) == ["--quick "]
    assert "--diff " in texts(tui.complete("/review --"))
    assert texts(tui.complete("/saver --d")) == ["--deep ", "--diff "]
    assert texts(tui.complete("/saver --de")) == ["--deep "]
    assert tui.complete("/review explain this --q") == []      # the question has started
    assert tui.complete("/new --q") == []                      # a command that takes none
    assert texts(tui.complete("/review --quick --de")) == ["--deep "]  # more than one, as typed


def test_options_are_listed_on_tab_even_before_a_dash_is_typed():
    assert tui.complete("/review ") == []                       # while typing a question: stay out of the way
    assert "--quick " in texts(tui.complete("/review ", explicit=True))
    assert tui.complete("/review why ", explicit=True) == []


def test_a_value_completes_for_the_options_that_take_one():
    assert texts(tui.complete("/review --moderator ", AGENTS)) == AGENTS
    assert texts(tui.complete("/review --moderator g", AGENTS)) == ["gpt", "gemini"]
    assert texts(tui.complete("/review --mode d")) == ["deep"]
    assert tui.complete("/review --timeout ") == []             # a number: nothing to offer
    assert tui.complete("/review --moderator gpt why ", AGENTS, explicit=True) == []  # value taken, question begun
    assert texts(tui.complete("/review --moderator gpt --q", AGENTS)) == ["--quick "]


@pytest.fixture
def project(tmp_path, monkeypatch):
    for name in ("app.py", "notes.md", "my file.txt", ".env", "src/main.py", "src/util.py", "my docs/a b.md"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def test_file_completes_paths_folders_first_and_hides_dotfiles(project):
    first = tui.complete("/review --file ")
    # a folder's quote stays open; it ends in the system's separator (\ on Windows) until you type a /
    assert texts(first) == [f'"my docs{os.sep}', f"src{os.sep}", "app.py", '"my file.txt"', "notes.md"]
    assert [s.meta for s in first][:2] == ["folder", "folder"] and first[2].meta == "file"
    assert all(s.back == 0 for s in first)
    assert texts(tui.complete("/review --file a")) == ["app.py"] and tui.complete("/review --file a")[0].back == -1
    assert texts(tui.complete("/review --file .")) == [".env"]
    assert texts(tui.complete("/review --file src/")) == ["src/main.py", "src/util.py"]
    assert texts(tui.complete("/review --file src/m")) == ["src/main.py"]
    assert tui.complete("/review --file nope/") == [] and tui.complete("/review --file zzz") == []


def test_file_paths_with_spaces_are_offered_in_quotes_and_quoted_words_are_one_value(project):
    assert texts(tui.complete("/review --file my")) == [f'"my docs{os.sep}', '"my file.txt"']
    assert texts(tui.complete('/review --file "my')) == [f'"my docs{os.sep}', '"my file.txt"']
    assert texts(tui.complete('/review --file "my docs/')) == ['"my docs/a b.md"']
    assert tui.complete('/review --file "my docs/')[0].back == -len('"my docs/')
    # a finished quoted value is one word: the next option is still offered, and so is another path
    assert texts(tui.complete('/review --file "my file.txt" --d')) == ["--deep ", "--diff "]
    assert f"src{os.sep}" in texts(tui.complete('/review --file "my file.txt" --file s'))


def test_file_completion_stops_when_the_question_starts_and_follows_home(project):
    assert tui.complete("/review --file app.py what does this do ", explicit=True) == []
    assert tui.complete("/review --file app.py is it safe to ap") == []
    assert "~/src/" in texts(tui.complete("/review --file ~/"))
    assert texts(tui.complete("/saver --file ~/s")) == ["~/src/"]


def test_a_big_folder_is_cut_short_and_a_file_is_not_a_folder(project):
    big = project / "big"
    big.mkdir()
    for n in range(tui.PATH_LIMIT + 20):
        (big / f"f{n:03}.txt").write_text("x", encoding="utf-8")
    assert len(tui.complete("/review --file big/")) == tui.PATH_LIMIT
    assert tui.complete("/review --file app.py/") == []


def test_every_option_the_commands_take_can_be_completed():
    assert set(tui.FLAG_HELP) == set(mat.REVIEW_VALUE_FLAGS | mat.REVIEW_BOOL_FLAGS | mat.REVIEW_LIST_FLAGS)
    assert tui.VALUE_FLAGS == set(mat.REVIEW_VALUE_FLAGS | mat.REVIEW_LIST_FLAGS)


# ── the rule above the prompt ─────────────────────────────────────────────────

def rule(width=110, **kwargs):
    status = kwargs.pop("status", tui.Status("quick", 3))
    parts = tui.rule_fragments(status, width, **kwargs)
    return parts, "".join(text for _, text in parts)


@pytest.mark.parametrize("width", [1, 10, 30, 40, 60, 80, 110, 200])
def test_the_rule_always_fills_the_width_exactly(width):
    for kwargs in ({}, {"done": True}, {"exit_armed": True}, {"status": tui.Status("deep", 1, 2)}):
        assert len(rule(width, **kwargs)[1]) == width, (width, kwargs)


def test_the_rule_says_how_a_question_will_be_asked_and_what_keys_there_are():
    _, text = rule()
    assert "quick · 3 agents" in text and "ctrl+j new line" in text and "ctrl+c exit" in text
    assert "follow-up" not in text
    assert "1 agent" in rule(status=tui.Status("review", 1))[1] and "1 agents" not in rule(status=tui.Status("review", 1))[1]
    assert "follow-up ×2" in rule(status=tui.Status("quick", 3, 2))[1]


def test_a_narrow_terminal_gets_fewer_keys_and_a_sent_line_none():
    assert "ctrl+c exit" in rule(110)[1] and "ctrl+c exit" not in rule(55)[1]  # the last keys go first
    assert "tab completes" in rule(55)[1] and "ctrl+j" not in rule(55)[1]
    assert "/ commands" in rule(40)[1] and "tab completes" not in rule(40)[1]
    assert "/ commands" not in rule(25)[1] and "quick · 3 agents" in rule(25)[1]  # the status is kept longest
    done = rule(110, done=True)[1]
    assert "quick · 3 agents" in done and "ctrl+" not in done and set(done[done.index("agents") + 6:]) <= {"─", " "}


def test_pressing_ctrl_c_once_says_what_a_second_press_does():
    parts, text = rule(exit_armed=True)
    assert "Press ctrl+c again to exit" in text and "quick" not in text
    assert ("class:rule.warn", "Press ctrl+c again to exit ") in parts


# ── the welcome card ──────────────────────────────────────────────────────────

def render(renderable, width, **console_options):
    buf = io.StringIO()
    Console(file=buf, width=width, **console_options).print(renderable)
    return buf.getvalue()


ROWS = [("mode", "quick · answer → verdict"), ("folder", "~/code/app")]


def test_the_logo_is_the_moon_from_the_asset_file():
    lines = tui.logo_lines()
    assert len(lines) > 15 and lines[0].plain.strip() and lines[-1].plain.strip()
    assert "I  X  E  L" in lines[-1].plain
    assert any(span.style for line in lines for span in line.spans)  # colored, not raw escape codes in the text
    assert not any("\x1b" in line.plain for line in lines)


def test_a_wide_terminal_gets_the_logo_beside_the_details():
    out = render(tui.splash(110, "9.9.9", ROWS), 110)
    assert "I  X  E  L" in out and "▒" in out
    row = next(line for line in out.splitlines() if "Ixel MAT" in line)
    assert row.index("Ixel MAT") >= max(len(line.plain) for line in tui.logo_lines())  # right of the moon
    for needle in ("v9.9.9", "Multi-Agent Terminal", "quick · answer → verdict", "~/code/app", "/review --diff", "/help"):
        assert needle in out


def test_a_narrow_terminal_gets_the_text_alone():
    out = render(tui.splash(60, "9.9.9", ROWS), 60)
    assert "I  X  E  L" not in out and "Ixel MAT  v9.9.9" in out
    assert "/saver <question>" in out and "…" not in out  # wraps rather than cutting a command short
    assert render(tui.splash(110, "1", ROWS, logo=[]), 110).count("Ixel MAT") == 1  # no logo file: the same, alone


def test_the_card_prints_plain_text_to_a_pipe_and_a_terminal_that_can_only_do_16_colors():
    plain = render(tui.splash(110, "1", ROWS), 110, force_terminal=False)
    assert "\x1b" not in plain and "I  X  E  L" in plain
    sixteen = render(tui.splash(110, "1", ROWS), 110, force_terminal=True, color_system="standard")
    assert "38;2;" not in sixteen and "\x1b[" in sixteen  # still colored, with colors it has


def test_folders_under_home_start_with_a_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert tui.tilde(tmp_path) == "~"
    assert tui.tilde(tmp_path / "code" / "app") == "~" + os.sep + os.path.join("code", "app")
    other = tmp_path.parent / (tmp_path.name + "-other")
    assert tui.tilde(other) == str(other)  # a sibling whose name merely starts the same


def test_one_palette_for_every_screen():
    from ixel_mat import cli
    assert mat.C is theme.C and review_ui.C is theme.C and cli.C is theme.C
    assert theme.C["gold"] == "#d4af37" and theme.C["violet"] == "#9b7fc7"  # the logo's colors


def test_the_welcome_card_says_what_a_plain_question_does_and_where_you_are(monkeypatch, tmp_path):
    from ixel_mat.runtime import ReviewSettings
    monkeypatch.chdir(tmp_path)
    for plain, expected in (("quick", "quick · answer → verdict"), ("review", "review · answer → review → verdict"),
                            ("saver", "saver · answer → review → verify"), ("auto", "Triage picks"),
                            ("compare", "side by side")):
        monkeypatch.setattr(mat, "_REVIEW", ReviewSettings(plain=plain))
        buf = io.StringIO()
        monkeypatch.setattr(mat, "console", Console(file=buf, width=120))
        mat.print_splash()
        out = buf.getvalue()
        assert expected in out and f"v{mat.__version__}" in out and "folder" in out and "I  X  E  L" in out, plain


@pytest.mark.skipif(os.name == "nt", reason="Windows can't delete the folder a program is in")
def test_the_welcome_card_survives_a_folder_that_was_deleted(monkeypatch, tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    buf = io.StringIO()
    monkeypatch.setattr(mat, "console", Console(file=buf, width=120))
    mat.print_splash()
    assert "Ixel MAT" in buf.getvalue() and "folder" not in buf.getvalue()


# ── /help ─────────────────────────────────────────────────────────────────────

def test_help_groups_the_commands_and_lists_the_keys(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(mat, "console", Console(file=buf, width=100))
    mat.print_help()
    out = buf.getvalue()
    assert out.index("Ask the panel") < out.index("/review") < out.index("Look back") < out.index("/saves")
    assert out.index("/saves") < out.index("This session") < out.index("/quit") < out.index("Keys")
    assert "Ctrl+J" in out and "Tab" in out
    buf.truncate(0)
    mat.print_help(keys=False)  # plain line input has none of these keys
    assert "Keys" not in buf.getvalue() and "/review [--quick|--deep] <question>" in buf.getvalue()


# ── the live view of a run ────────────────────────────────────────────────────

def event(kind, **data):
    return ReviewEvent(kind, data)


def feed(progress, *events):
    for e in events:
        progress.on_event(e)


def test_finished_rounds_are_printed_into_history_and_the_live_view_holds_only_the_current_one():
    history = []
    progress = review_ui.ReviewProgress("q", ReviewMode.REVIEW, 2, echo=history.append)
    feed(progress,
         event("round", number=1, total=3, round="answer", agents=["A", "B"]),
         event("agent_started", agent="a", agent_label="A"), event("agent_started", agent="b", agent_label="B"),
         event("answer", agent="a", agent_label="A", latency_ms=1200))
    assert history == []  # the round isn't over
    feed(progress, event("round", number=2, total=3, round="review", agents=["A", "B"]))
    assert len(history) == 1
    flushed = render(history[0], 100)
    assert "Round 1/3" in flushed and "A  answered in 1.2s" in flushed
    live = render(progress, 100)
    assert "Round 2/3" in live and "Round 1/3" not in live  # moved up into history
    assert "B" in live and "working…" in live  # B hasn't answered yet
    assert "▸" not in live  # the header was printed once, before; it isn't repeated in the live view
    progress.flush()
    assert len(history) == 2 and "Round 2/3" in render(history[1], 100)
    progress.flush()
    assert len(history) == 2  # nothing left to print


def test_without_echo_the_whole_run_is_in_the_live_view_as_before():
    progress = review_ui.ReviewProgress("What is 17 × 23?", ReviewMode.QUICK, 2)
    feed(progress, event("round", number=1, total=2, round="answer", agents=["A", "B"]),
         event("round", number=2, total=2, round="verdict", agents=["A"]))
    live = render(progress, 100)
    assert "What is 17 × 23?" in live and "Round 1/2" in live and "Round 2/2" in live
    progress.flush()  # a no-op
    assert "Round 1/2" in render(progress, 100)


def test_the_status_line_counts_the_round_and_says_how_to_stop():
    progress = review_ui.ReviewProgress("q", ReviewMode.REVIEW, 3, echo=lambda r: None)
    assert "starting" in render(progress, 100) and "ctrl+c cancels" in render(progress, 100)
    feed(progress, event("round", number=1, total=3, round="answer", agents=["A", "B", "C"]))
    for name in "abc":
        feed(progress, event("agent_started", agent=name, agent_label=name.upper()))
    assert "0/3 done" in render(progress, 100)
    feed(progress, event("answer", agent="a", agent_label="A", latency_ms=1000),
         event("agent_failed", agent="b", agent_label="B", error="left out"))
    out = render(progress, 100)
    assert "2/3 done" in out and "C" in out and "ctrl+c cancels" in out
    feed(progress, event("round", number=2, total=3, round="review", agents=["A", "B", "C"]))
    assert "0/3 done" in render(progress, 100)  # a new round starts its count again


def test_the_prompt_moon_grows_with_the_conversation_and_is_gold_when_full():
    from ixel_mat.conversation import MAX_EARLIER_TURNS
    phases = [theme.moon(n) for n in range(MAX_EARLIER_TURNS + 1)]
    assert [g for g, _ in phases] == list(theme.MOON_PHASES) and len(set(g for g, _ in phases)) == 4
    assert [c for _, c in phases] == [theme.C["violet"]] * 3 + [theme.C["gold"]]
    assert theme.moon(0)[0] == theme.PROMPT_GLYPH  # a new conversation: the crescent
    assert theme.moon(99) == phases[-1] and theme.moon(-1) == phases[0]  # never out of range
    assert len(theme.MOON_PHASES) == MAX_EARLIER_TURNS + 1  # the full moon is exactly "the panel sees everything it can"


def test_the_spinner_turns():
    frames = {theme.spinner(n / 10) for n in range(40)}
    assert frames == set(theme.SPINNER_FRAMES)


# ── ctrl+c while a run is going ───────────────────────────────────────────────

@pytest.fixture
def captured(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(mat, "console", Console(file=buf, width=100))
    return buf


@pytest.mark.skipif(sys.platform == "win32", reason="the signal is sent with os.kill")
def test_ctrl_c_cancels_the_run_and_nothing_else(captured):
    stopped = []
    before = signal.getsignal(signal.SIGINT)

    async def go():
        async def work():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                stopped.append(True)
                raise

        asyncio.get_running_loop().call_later(0.2, os.kill, os.getpid(), signal.SIGINT)
        return await mat._interruptible(work())

    assert asyncio.run(go()) is None  # no KeyboardInterrupt: the caller carries on
    assert stopped == [True]
    assert "Cancelled." in captured.getvalue() and "conversation is unchanged" in captured.getvalue()
    assert signal.getsignal(signal.SIGINT) is before  # ctrl+c means what it did before


@pytest.mark.skipif(sys.platform == "win32", reason="the signal is sent with os.kill")
def test_a_second_ctrl_c_gives_up_on_a_run_that_is_slow_to_stop(captured):
    before = signal.getsignal(signal.SIGINT)

    async def go():
        async def stubborn():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.sleep(30)  # takes its time stopping

        loop = asyncio.get_running_loop()
        loop.call_later(0.2, os.kill, os.getpid(), signal.SIGINT)
        loop.call_later(0.6, os.kill, os.getpid(), signal.SIGINT)
        await mat._interruptible(stubborn())

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(go())
    assert signal.getsignal(signal.SIGINT) is before


@pytest.mark.skipif(sys.platform == "win32", reason="the signal is sent with os.kill")
def test_cancelling_compare_stops_every_model_and_leaves_no_task_behind(captured):
    class Slow:
        is_connected = True

        def __init__(self, name):
            self.name, self.label, self.cancelled = name, name.title(), False

        async def send_and_receive(self, message, **kwargs):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    agents = {n: Slow(n) for n in ("a", "b", "c")}

    async def go():
        asyncio.get_running_loop().call_later(0.4, os.kill, os.getpid(), signal.SIGINT)
        await mat.run_full("What is 17 × 23?", agents)
        # right now, not when the loop shuts down: nothing may still be answering (and billing)
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    assert asyncio.run(go()) == []
    assert all(a.cancelled for a in agents.values())
    assert "Cancelled." in captured.getvalue()


def test_leaving_by_a_double_ctrl_c_says_so_instead_of_printing_a_traceback(monkeypatch):
    from ixel_mat import cli
    buf = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=100))

    async def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(mat, "main", interrupted)
    with pytest.raises(SystemExit) as leaving:
        cli.cmd_run()
    assert leaving.value.code == 130 and "Interrupted." in buf.getvalue()


def test_a_run_that_finishes_returns_its_result_and_puts_ctrl_c_back(captured):
    before = signal.getsignal(signal.SIGINT)

    async def go():
        await asyncio.sleep(0)
        return "done"

    assert asyncio.run(mat._interruptible(go())) == "done"
    assert signal.getsignal(signal.SIGINT) is before and captured.getvalue() == ""


def test_a_run_cancelled_from_outside_is_not_reported_as_a_ctrl_c(captured):
    async def go():
        task = asyncio.ensure_future(mat._interruptible(asyncio.sleep(30)))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert "Cancelled." not in captured.getvalue()


def test_a_ctrl_c_outside_the_main_thread_is_left_alone(captured):
    import threading
    result = []

    def worker():
        async def go():
            await asyncio.sleep(0)
            return "ok"
        result.append(asyncio.run(mat._interruptible(go())))  # signal.signal() would refuse here

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(10)
    assert result == ["ok"]


# ── the prompt ────────────────────────────────────────────────────────────────

prompt_toolkit = pytest.importorskip("prompt_toolkit")


def make_prompt(inp, big_at=5):
    from prompt_toolkit.output import DummyOutput

    from ixel_mat.prompt_ui import PromptUI
    count = {"n": 0}

    def placeholder(text):
        if text.count("\n") >= big_at:
            count["n"] += 1
            return f"[paste #{count['n']} +{text.count(chr(10))} lines]"

    return PromptUI(lambda: tui.Status("quick", 3), lambda: AGENTS, placeholder, input=inp, output=DummyOutput())


def typed(keys):
    """What the prompt returns for these keystrokes, or the exception it raises."""
    from prompt_toolkit.input import create_pipe_input

    async def go():
        with create_pipe_input() as inp:
            ui = make_prompt(inp)
            inp.send_text(keys)
            return await asyncio.wait_for(ui.read(), 10)

    try:
        return asyncio.run(go())
    except BaseException as exc:  # noqa: BLE001
        return type(exc)


def test_enter_sends():
    assert typed("hello there\r") == "hello there"


@pytest.mark.parametrize("keys", ["one\x0atwo\r", "one\x1b\rtwo\r", "one\\\rtwo\r"], ids=["ctrl+j", "alt+enter", "backslash"])
def test_three_ways_to_start_a_new_line(keys):
    assert typed(keys) == "one\ntwo"


def test_a_big_paste_shows_as_a_placeholder_but_is_sent_whole():
    big = "\n".join(f"row {n}" for n in range(8))
    assert typed(f"look: \x1b[200~{big}\x1b[201~ ok?\r") == f"look: {big} ok?"


def test_a_line_recalled_with_the_up_arrow_still_sends_the_paste_it_stands_for():
    from prompt_toolkit.input import create_pipe_input

    big = "\n".join(f"row {n}" for n in range(8))

    async def go():
        with create_pipe_input() as inp:
            ui = make_prompt(inp)
            inp.send_text(f"look: \x1b[200~{big}\x1b[201~\r")
            first = await asyncio.wait_for(ui.read(), 10)
            inp.send_text("\x1b[A\r")  # up arrow, then enter: the same line again
            return first, await asyncio.wait_for(ui.read(), 10)

    first, again = asyncio.run(go())
    assert first == again == f"look: {big}"


def test_a_small_paste_goes_in_as_it_is():
    assert typed("x \x1b[200~a\r\nb\x1b[201~\r") == "x a\nb"


def test_the_box_shows_the_placeholder_not_the_paste():
    from prompt_toolkit.input import create_pipe_input

    async def go():
        with create_pipe_input() as inp:
            ui = make_prompt(inp)
            seen = []
            ui.session.default_buffer.on_text_changed += lambda buf: seen.append(buf.text)
            inp.send_text("\x1b[200~" + "\n".join("x" * 5 for _ in range(9)) + "\x1b[201~\r")
            await asyncio.wait_for(ui.read(), 10)
            return seen

    assert asyncio.run(go())[-1] == "[paste #1 +8 lines]"


def test_ctrl_c_clears_a_typed_line_and_a_second_one_on_an_empty_line_exits():
    assert typed("junk\x03fresh\r") == "fresh"
    assert typed("\x03\x03") is EOFError
    assert typed("junk\x03\x03\r") == ""  # clearing doesn't count as the first press
    assert typed("\x04") is EOFError  # ctrl+d


def test_the_first_ctrl_c_on_an_empty_line_arms_the_exit_hint_and_it_lapses():
    from prompt_toolkit.input import create_pipe_input

    from ixel_mat import prompt_ui

    async def go():
        with create_pipe_input() as inp:
            ui = make_prompt(inp)
            inp.send_text("\x03")
            task = asyncio.ensure_future(ui.read())
            await asyncio.sleep(0.3)
            armed = time.monotonic() < ui._exit_armed_until
            inp.send_text("ok\r")
            await asyncio.wait_for(task, 10)
            return armed

    assert asyncio.run(go()) is True
    assert 0 < prompt_ui.EXIT_WINDOW <= 5


def test_the_completer_hands_prompt_toolkit_what_tui_complete_says():
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    from ixel_mat.prompt_ui import _Completer
    found = list(_Completer(lambda: AGENTS).get_completions(Document("/review --moderator c"), CompleteEvent()))
    assert [(c.text, c.start_position) for c in found] == [("claude", -1)]
    found = list(_Completer(lambda: AGENTS).get_completions(Document("/rev"), CompleteEvent()))
    assert found[0].text == "/review" and found[0].start_position == -4 and "answer" in found[0].display_meta_text


# ── the terminal app uses it ──────────────────────────────────────────────────

class _Agent:
    def __init__(self, name):
        self.name, self.label, self.is_connected = name, name.title(), True


def drive(monkeypatch, buf, lines, prompt_ui, agents=None):
    """Run mat.main() with these lines typed; returns the questions that reached run_plain_question."""
    agents = agents or {"gpt": _Agent("gpt"), "claude": _Agent("claude")}
    asked = []

    async def fake_connect():
        return agents

    async def fake_plain(text, _agents):
        asked.append(text)

    monkeypatch.setattr(mat, "console", Console(file=buf, width=120))
    monkeypatch.setattr(mat, "connect_agents", fake_connect)
    monkeypatch.setattr(mat, "print_splash", lambda: None)
    monkeypatch.setattr(mat, "run_plain_question", fake_plain)
    monkeypatch.setattr(mat, "_make_prompt_ui", lambda _agents: prompt_ui)
    monkeypatch.setattr(mat, "_CONVERSATION", [])
    asyncio.run(mat.main())
    return asked


class _FakePromptUI:
    def __init__(self, lines):
        self.lines = iter(lines)

    async def read(self):
        try:
            return next(self.lines)
        except StopIteration:
            raise EOFError from None


def test_with_the_interactive_prompt_a_big_paste_is_not_asked_about_again(monkeypatch):
    async def never(*args, **kwargs):
        raise AssertionError("asked to confirm a paste the box already showed as a placeholder")

    monkeypatch.setattr(mat, "_confirm_async", never)
    big = "word " * 1000
    buf = io.StringIO()
    asked = drive(monkeypatch, buf, [big, "/help"], _FakePromptUI([big, "/help"]))
    assert asked == [big.strip()]
    assert "Keys" in buf.getvalue() and "2 agents ready" in buf.getvalue()  # the help lists keys; the panel is summed up


def test_if_the_line_editor_breaks_midway_the_session_goes_on_with_plain_lines(monkeypatch):
    class Breaks:
        async def read(self):
            raise OSError("the console went away")

    plain = iter(["a question", "/quit"])

    async def fake_read(*args, **kwargs):
        return next(plain)

    monkeypatch.setattr(mat, "read_burst_submission", fake_read)
    buf = io.StringIO()
    asked = drive(monkeypatch, buf, [], Breaks())
    assert asked == ["a question"]
    assert "The line editor stopped (OSError: the console went away); reading plain lines." in buf.getvalue()


def test_without_a_terminal_lines_are_read_the_old_way_and_help_has_no_keys(monkeypatch):
    seen = []

    async def fake_read(*args, **kwargs):
        seen.append(kwargs["main_prompt"])
        raise EOFError

    monkeypatch.setattr(mat, "read_burst_submission", fake_read)
    buf = io.StringIO()
    drive(monkeypatch, buf, [], None)
    assert seen and theme.moon(0)[0] in seen[0] and "❯" in seen[0]


def test_the_prompt_is_only_built_for_a_real_terminal_and_gives_way_when_it_cant_be(monkeypatch):
    agents = {"gpt": _Agent("gpt")}
    monkeypatch.setattr(mat, "_PASTE_STATE", {"count": 0})
    tty = type("Tty", (), {"isatty": staticmethod(lambda: True)})()
    pipe = type("Pipe", (), {"isatty": staticmethod(lambda: False)})()

    quiet = io.StringIO()
    monkeypatch.setattr(mat, "console", Console(file=quiet, width=120))
    monkeypatch.setattr(sys, "stdin", pipe)
    monkeypatch.setattr(sys, "stdout", tty)
    assert mat._make_prompt_ui(agents) is None  # input comes from a pipe

    monkeypatch.setattr(sys, "stdin", tty)
    monkeypatch.setattr(sys, "stdout", pipe)
    assert mat._make_prompt_ui(agents) is None  # output goes to a file
    assert quiet.getvalue() == ""  # plain lines are what's expected there: nothing to explain

    monkeypatch.setattr(sys, "stdout", tty)
    from ixel_mat import prompt_ui

    def cant(*args, **kwargs):
        raise RuntimeError("not a console prompt_toolkit can drive")

    monkeypatch.setattr(prompt_ui, "PromptUI", cant)
    buf = io.StringIO()
    monkeypatch.setattr(mat, "console", Console(file=buf, width=120))
    assert mat._make_prompt_ui(agents) is None  # e.g. mintty on Windows: fall back to plain lines…
    assert "Reading plain lines, without history or completion (RuntimeError: not a console" in buf.getvalue()  # …and say so


def test_a_big_paste_is_folded_only_past_the_size_that_used_to_ask_for_confirmation(monkeypatch):
    monkeypatch.setattr(mat, "_PASTE_STATE", {"count": 0})
    assert mat._paste_placeholder("a short question") is None
    assert mat._paste_placeholder("\n".join(["line"] * 11)) is None
    assert mat._paste_placeholder("\n".join(["line"] * 12)) == "[paste #1 +11 lines]"
    assert mat._paste_placeholder("x" * 3000) == "[paste #2 +2999 chars]"  # numbered through the session
