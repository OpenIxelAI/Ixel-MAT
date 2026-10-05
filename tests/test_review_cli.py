"""`ixel review` end to end, and the /review command inside the terminal app."""
import asyncio
import io
import json
import os
import subprocess
import sys

import pytest
from rich.console import Console

from fake_providers import ThreadedFakeProvider, panel_handler
from ixel_mat import mat
from ixel_mat.modes.review import ReviewMode
from ixel_mat.runtime import ReviewSettings, parse_review_settings
from ixel_mat.triage import TriageSettings

ANSWERS = {"m-gpt": "It's 391.", "m-claude": "17 × 23 = 391", "m-wrong": "The answer is 381."}


def write_config(home, url, extra=""):
    cfg_dir = home / ".config" / "ixel-mat"
    cfg_dir.mkdir(parents=True)
    agents = ""
    for agent_id, (model, label) in {"gpt": ("m-gpt", "GPT"), "claude": ("m-claude", "Claude"),
                                     "gemini": ("m-wrong", "Gemini")}.items():
        agents += (f'[agents.{agent_id}]\ntype = "http"\nurl = "{url}"\ntoken_env = "IXEL_TEST_PANEL_KEY"\n'
                   f'model = "{model}"\nlabel = "{label}"\n\n')
    (cfg_dir / "config.toml").write_text(agents + extra)


def run_ixel(home, *args, stdin=None):
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
           "IXEL_TEST_PANEL_KEY": "sk-test", "PYTHONIOENCODING": "utf-8", "COLUMNS": "120"}
    return subprocess.run([sys.executable, "-m", "ixel_mat", *args], cwd=home, env=env, input=stdin,
                          capture_output=True, text=True, encoding="utf-8", timeout=120)


@pytest.fixture
def panel_home(tmp_path):
    with ThreadedFakeProvider(panel_handler(ANSWERS)) as fake:
        write_config(tmp_path, fake.openai_url)
        yield tmp_path, fake


def test_ixel_review_json(panel_home):
    home, fake = panel_home
    proc = run_ixel(home, "review", "--json", "What is 17 × 23?")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)  # stdout is pure JSON; progress went to stderr
    assert data["final"]["answer"] == "17 × 23 = 391"
    scores = {s["agent_label"]: s["score"] for s in data["standings"]}
    assert scores == {"GPT": 1.0, "Claude": 1.0, "Gemini": 0.0}
    assert [c["reviewer_label"] for c in data["concessions"]] == ["Gemini"]
    assert data["agreement"] == "strong" and data["calls"] == 7
    assert len(fake.requests) == 7


def test_ixel_review_report(panel_home):
    home, _ = panel_home
    proc = run_ixel(home, "review", "What is 17 × 23?")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    for expected in ("IXEL VERDICT", "Peer review", "The correct result is 391.",
                     "Gemini conceded", "Agreement: strong", "7 model calls"):
        assert expected in out, expected


def test_ixel_review_prints_each_round_and_the_question_once(panel_home):
    # Finished rounds are printed into the terminal's history as the next one starts (so the live view stays
    # small), and the last one when the run ends: every round exactly once, in order, whether or not a
    # terminal is watching
    home, _ = panel_home
    proc = run_ixel(home, "review", "What is 17 × 23?")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert out.count("▸ What is 17 × 23?") == 1 and out.count("review mode · 3 agents") == 1
    rounds = ["Round 1/3 · answering independently", "Round 2/3 · anonymous peer review",
              "Round 3/3 · moderator's verdict"]
    assert [out.count(r) for r in rounds] == [1, 1, 1]
    assert out.index(rounds[0]) < out.index(rounds[1]) < out.index(rounds[2]) < out.index("IXEL VERDICT")
    assert out.count("answered in") == 3 and out.count("picked") == 3
    assert "ctrl+c cancels" not in out  # the live status line is gone once the run is over


def test_ixel_review_quick_mode_and_stdin(panel_home):
    home, fake = panel_home
    proc = run_ixel(home, "review", "--quick", "--json", "-", stdin="What is 17 × 23?\n")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["mode"] == "quick" and data["calls"] == 4 and data["standings"][0]["score"] is None
    assert "What is 17 × 23?" in fake.requests[0].body["messages"][0]["content"]


def test_an_unknown_moderator_is_refused_before_anything_is_asked(panel_home):
    home, fake = panel_home
    proc = run_ixel(home, "review", "--moderator", "claud", "What is 17 × 23?")
    assert proc.returncode == 2 and "no agent named 'claud'" in proc.stderr and "gpt, claude, gemini" in proc.stderr
    assert not fake.requests


def test_slash_review_refuses_an_unknown_moderator_too(monkeypatch):
    from ixel_mat.config.loader import build_agent_configs
    from ixel_mat.runtime import Settings

    out = io.StringIO()
    configs, _ = build_agent_configs({"agents": {"gpt": {"type": "http", "url": "https://x.example/v1",
                                                         "model": "m"}}})
    monkeypatch.setattr(mat, "console", Console(file=out, width=200, color_system=None))
    monkeypatch.setattr(mat, "_settings", lambda: Settings({}, configs))
    monkeypatch.setattr(mat, "choose_mode", lambda *a: (_ for _ in ()).throw(AssertionError("asked anyway")))
    asyncio.run(mat._review(mat.parse_review_args("--moderator claud What is 17 × 23?"), {}))
    assert "no agent named 'claud' (agents: gpt)" in out.getvalue()


def test_piped_lines_are_questions_of_their_own(panel_home):
    home, fake = panel_home
    proc = run_ixel(home, stdin="What is 17 × 23?\nWhat is 6 × 7?\n/quit\n")
    assert proc.returncode == 0, proc.stderr
    asked = [r.body["messages"][0]["content"] for r in fake.requests if r.api == "openai"]
    assert any(m.startswith("What is 17 × 23?\n\n") for m in asked)  # asked on its own
    assert any("follow-up:\nWhat is 6 × 7?\n\n" in m for m in asked)  # then the next line, as a follow-up
    assert not any("17 × 23?\nWhat is 6 × 7" in m or "/quit" in m for m in asked)


def test_ixel_review_continue_is_a_follow_up(panel_home):
    home, fake = panel_home
    first = run_ixel(home, "review", "--quick", "--json", "What is 17 × 23?")
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["earlier_turns"] == 0
    assert "earlier in this conversation" not in fake.requests[0].body["messages"][0]["content"]

    fake.requests.clear()
    second = run_ixel(home, "review", "--quick", "--json", "--continue", "And 17 × 24?")
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["earlier_turns"] == 1
    prompt = fake.requests[0].body["messages"][0]["content"]
    assert "earlier in this conversation" in prompt and "What is 17 × 23?" in prompt
    assert prompt.rstrip().endswith("say which part.)") and "And 17 × 24?" in prompt

    fake.requests.clear()
    fresh = run_ixel(home, "review", "--quick", "--json", "Unrelated")  # no --continue: a new conversation
    assert json.loads(fresh.stdout)["earlier_turns"] == 0
    assert "earlier in this conversation" not in fake.requests[0].body["messages"][0]["content"]
    saved = json.loads((home / ".config" / "ixel-mat" / "conversation.json").read_text(encoding="utf-8"))
    assert [t["question"] for t in saved["turns"]] == ["Unrelated"]


def test_ixel_review_continue_with_nothing_to_continue(panel_home):
    home, _ = panel_home
    proc = run_ixel(home, "review", "--quick", "-c", "What is 17 × 23?")
    assert proc.returncode == 0, proc.stderr
    assert "Nothing to continue" in proc.stdout


def test_review_settings_from_config(tmp_path):
    with ThreadedFakeProvider(panel_handler(ANSWERS)) as fake:
        write_config(tmp_path, fake.openai_url,
                     extra='[review]\nmode = "deep"\nmoderator = "claude"\nagents = ["gpt", "gemini"]\n')
        proc = run_ixel(tmp_path, "review", "--json", "What is 17 × 23?")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["mode"] == "deep"
    assert sorted(a["agent_label"] for a in data["answers"]) == ["GPT", "Gemini"]
    # moderator "claude" isn't on the panel, so the best-rated author moderates
    assert data["final"]["moderator_label"] == "GPT"
    gemini = next(a for a in data["answers"] if a["agent_label"] == "Gemini")
    assert gemini["text"] == "391 (revised)" and gemini["original_text"] == "The answer is 381."


def test_ixel_review_without_agents_fails_cleanly(tmp_path):
    (tmp_path / ".config" / "ixel-mat").mkdir(parents=True)
    (tmp_path / ".config" / "ixel-mat" / "config.toml").write_text(
        '[agents.gpt]\ntype = "http"\nurl = "https://api.openai.com/v1/chat/completions"\n'
        'token_env = "IXEL_TEST_UNSET_KEY"\nmodel = "m"\nlabel = "GPT"\n')
    proc = run_ixel(tmp_path, "review", "hello?")
    assert proc.returncode == 1
    assert "No agents connected" in proc.stdout + proc.stderr


def test_ixel_review_requires_a_question(tmp_path):
    proc = run_ixel(tmp_path, "review", stdin="")
    assert proc.returncode == 2 and "a question is required" in proc.stderr


# ── [review] config parsing ───────────────────────────────────────────────────

def test_parse_review_settings_validates():
    settings, warnings = parse_review_settings(
        {"review": {"mode": "deep", "moderator": "claude", "timeout": 90, "agents": ["gpt", "nope"]}},
        {"gpt", "claude"},
    )
    assert settings == ReviewSettings(ReviewMode.DEEP, "claude", 90.0, ["gpt"])
    assert warnings == ["[review] agents not configured: nope"]

    settings, warnings = parse_review_settings(
        {"review": {"mode": "loud", "moderator": "ghost", "timeout": -1, "agents": "gpt"}}, {"gpt"})
    assert settings == ReviewSettings()
    assert len(warnings) == 4


# ── Terminal: argument parsing ────────────────────────────────────────────────

def test_review_args_keep_the_question_verbatim():
    opts = mat.parse_review_args("--deep --moderator claude what's the difference between 'a' and \"b\"?")
    assert opts.mode is ReviewMode.DEEP and opts.moderator == "claude"
    assert opts.question == "what's the difference between 'a' and \"b\"?"


def test_review_args_defaults_and_errors():
    defaults = ReviewSettings(ReviewMode.QUICK, "gpt", 42.0)
    opts = mat.parse_review_args("multi\nline question", defaults)
    assert (opts.mode, opts.moderator, opts.timeout, opts.question) == (ReviewMode.QUICK, "gpt", 42.0, "multi\nline question")
    assert mat.parse_review_args("--mode=deep --timeout=5 q").timeout == 5.0
    assert mat.parse_review_args("-- --not-a-flag").question == "--not-a-flag"
    for bad, message in [("--mode loud q", "--mode"), ("--timeout soon q", "--timeout"),
                         ("--timeout 0 q", "--timeout"), ("--bogus q", "Unknown option"),
                         ("--deep", "Usage"), ("--moderator", "Missing value")]:
        with pytest.raises(ValueError, match=message):
            mat.parse_review_args(bad)


def test_review_args_auto():
    assert mat.parse_review_args("--auto q").auto and mat.parse_review_args("--mode auto q").auto
    assert not mat.parse_review_args("q").auto
    defaults = ReviewSettings(auto=True)
    assert mat.parse_review_args("q", defaults).auto
    assert not mat.parse_review_args("--deep q", defaults).auto  # an explicit mode wins over the default
    assert parse_review_settings({"review": {"plain_questions": "auto"}}, set())[0].plain == "auto"


def test_consensus_now_runs_a_quick_review():
    command, args, note = mat.renamed_command("consensus", "what's up?")
    assert command == "review" and mat.parse_review_args(args).mode is ReviewMode.QUICK
    assert mat.parse_review_args(args).question == "what's up?" and note == "/consensus is now /review --quick."
    assert mat.renamed_command("config", "") is None


@pytest.mark.parametrize("old", [
    "--min-responses 3 --timeout 45 what's up?",
    "--timeout=45 --min-responses=3 what's up?",
    "--timeout 45 --min-responses 3 -- what's up?",
])
def test_consensus_keeps_its_old_options(old):
    """--timeout is /review's too; --min-responses has no counterpart, so it's taken off, with a note."""
    command, args, note = mat.renamed_command("cons", old)
    opts = mat.parse_review_args(args)
    assert (opts.mode, opts.timeout, opts.question) == (ReviewMode.QUICK, 45.0, "what's up?")
    assert note.startswith("/cons is now /review --quick.") and "--min-responses isn't needed" in note


def test_consensus_leaves_the_rest_to_review():
    _, args, note = mat.renamed_command("consensus", '--file "a b.py" --min-responses 2 is --min-responses 2 ok?')
    opts = mat.parse_review_args(args)
    assert opts.files == ["a b.py"] and opts.question == "is --min-responses 2 ok?" and "isn't needed" in note
    _, args, _ = mat.renamed_command("consensus", "--nope why?")
    with pytest.raises(ValueError, match="Unknown option --nope"):
        mat.parse_review_args(args)


# ── Terminal: /review and /answers ────────────────────────────────────────────

class _Agent:
    def __init__(self, name, model):
        self.name, self.label, self.model, self.is_connected = name, name.title(), model, True

    async def send_and_receive(self, message, **kwargs):
        from fake_providers import panel_text
        return panel_text(ANSWERS, self.model, message)


def test_review_command_in_the_terminal(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(mat, "console", Console(file=buf, width=120))
    monkeypatch.setattr(mat, "_REVIEW", ReviewSettings())
    agents = {"gpt": _Agent("gpt", "m-gpt"), "claude": _Agent("claude", "m-claude"), "gemini": _Agent("gemini", "m-wrong")}

    asyncio.run(mat.run_review_cmd("--deep What is 17 × 23?", agents))
    out = buf.getvalue()
    assert "IXEL VERDICT" in out and "deep · 10 model calls" in out

    buf.truncate(0)
    mat.print_last_answers()
    assert "391 (revised)" in buf.getvalue()


def test_a_plain_question_goes_to_the_panel(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(mat, "console", Console(file=buf, width=120))
    monkeypatch.setattr(mat, "_REVIEW", ReviewSettings())
    agents = {"gpt": _Agent("gpt", "m-gpt"), "claude": _Agent("claude", "m-claude"), "gemini": _Agent("gemini", "m-wrong")}
    asyncio.run(mat.run_plain_question("--deep What is 17 × 23?", agents))
    result = mat._LAST_REVIEW["result"]
    assert result.mode is ReviewMode.QUICK and result.question == "--deep What is 17 × 23?"
    assert "IXEL VERDICT" in buf.getvalue()

    compared = []

    async def fake_full(text, agents):
        compared.append(text)

    monkeypatch.setattr(mat, "run_full", fake_full)
    monkeypatch.setattr(mat, "_REVIEW", ReviewSettings(plain="compare"))
    asyncio.run(mat.run_plain_question("side by side?", agents))
    assert compared == ["side by side?"]


def test_auto_in_the_terminal_without_triage_says_what_ran(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(mat, "console", Console(file=buf, width=160))
    monkeypatch.setattr(mat, "_REVIEW", ReviewSettings())
    monkeypatch.setattr(mat, "_TRIAGE", TriageSettings())  # not the triage of whoever runs the tests
    agents = {"gpt": _Agent("gpt", "m-gpt"), "claude": _Agent("claude", "m-claude"), "gemini": _Agent("gemini", "m-wrong")}
    asyncio.run(mat.run_review_cmd("--auto What is 17 × 23?", agents))
    out = buf.getvalue()
    assert "Auto mode needs triage (it isn't set up), so this ran in review mode." in out
    assert mat._LAST_REVIEW["result"].mode is ReviewMode.REVIEW and "triage call" not in out


def test_ixel_review_auto_asks_triage_once(tmp_path, monkeypatch):
    from fake_providers import typesafe_handler
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    with ThreadedFakeProvider(panel_handler(ANSWERS)) as fake:
        fake.typesafe_handler = typesafe_handler(depth=0.1)
        write_config(tmp_path, fake.openai_url, f'[triage]\nenabled = true\nurl = "{fake.typesafe_url}"\n')
        proc = run_ixel(tmp_path, "review", "--auto", "--json", "What is 17 × 23?")
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        triage = [r for r in fake.requests if r.api == "triage"]
    assert data["mode"] == "quick" and data["triage"][0]["value"] == "quick" and data["triage_calls"] == 1
    assert len(triage) == 1 and triage[0].headers["authorization"] == "Bearer ts-test"
    assert "sk-test" not in json.dumps(triage[0].body)  # the panel's own keys never go to Triage


def test_plain_questions_setting():
    assert parse_review_settings({"review": {"plain_questions": "deep"}}, set())[0].plain == "deep"
    settings, warnings = parse_review_settings({"review": {"plain_questions": "loud"}}, set())
    assert settings.plain == "quick" and "plain_questions" in warnings[0]

    # Saver mode needs a verifier ([saver] verifier, else the moderator); without one, quick
    from ixel_mat.runtime import parse_saver_settings
    config = {"review": {"plain_questions": "saver"}}
    review, _ = parse_review_settings(config, {"gpt", "claude"})
    _, warnings = parse_saver_settings(config, {"gpt", "claude"}, review)
    assert review.plain == "quick" and "needs a verifier" in warnings[0]
    config = {"review": {"plain_questions": "saver", "moderator": "claude"}}
    review, _ = parse_review_settings(config, {"gpt", "claude"})
    assert parse_saver_settings(config, {"gpt", "claude"}, review)[0].verifier == "claude"
    assert review.plain == "saver"


def test_two_model_standoff_tip():
    from ixel_mat import review_ui
    from ixel_mat.modes.review import FinalAnswer, PanelAnswer, PeerReview, ReviewResult, Verdict

    def result(verdicts, disagreements=()):
        answers = [PanelAnswer(label, agent, agent.title(), f"answer {label}", 10)
                   for label, agent in (("A", "gpt"), ("B", "claude"))]
        reviews = [PeerReview(reviewer, reviewer.title(), target, verdict, [], [], False)
                   for (reviewer, target), verdict in zip((("claude", "A"), ("gpt", "B")), verdicts)]
        final = FinalAnswer(answer="x", moderator="gpt", moderator_label="Gpt", disagreements=list(disagreements))
        return ReviewResult("q", ReviewMode.REVIEW, answers=answers, reviews=reviews, final=final)

    assert review_ui.two_model_standoff(result([Verdict.INCORRECT, Verdict.PARTIAL]))
    assert not review_ui.two_model_standoff(result([Verdict.CORRECT, Verdict.INCORRECT]))
    assert review_ui.two_model_standoff(result([Verdict.CORRECT, Verdict.CORRECT], ["units"]))
    text = "".join(str(getattr(r, "plain", "")) for r in review_ui.report(result([Verdict.INCORRECT, Verdict.INCORRECT])))
    assert "a third member" in text.lower()


def test_terminal_questions_follow_up_until_new(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(mat, "console", Console(file=buf, width=120))
    monkeypatch.setattr(mat, "_REVIEW", ReviewSettings())
    monkeypatch.setattr(mat, "_CONVERSATION", [])
    agents = {"gpt": _Agent("gpt", "m-gpt"), "claude": _Agent("claude", "m-claude")}
    inputs = iter(["What is 17 × 23?", "And doubled?", "/new", "Something else"])
    seen = []
    real_run_review = mat.run_review

    async def recording_run_review(question, panel, **options):
        seen.append((question, [t.question for t in options.get("earlier", [])]))
        return await real_run_review(question, panel, **options)

    async def fake_read(*args, **kwargs):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    async def fake_connect():
        return agents

    monkeypatch.setattr(mat, "run_review", recording_run_review)
    monkeypatch.setattr(mat, "read_burst_submission", fake_read)
    monkeypatch.setattr(mat, "connect_agents", fake_connect)
    monkeypatch.setattr(mat, "print_splash", lambda: None)
    asyncio.run(mat.main())

    assert seen == [("What is 17 × 23?", []), ("And doubled?", ["What is 17 × 23?"]), ("Something else", [])]
    out = buf.getvalue()
    assert "Follow-up: the panel also sees your last question" in out and "Fresh start" in out
    assert "quick · follow-up ·" in out


def test_terminal_consensus_with_its_old_options_runs_a_review(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(mat, "console", Console(file=buf, width=200))
    monkeypatch.setattr(mat, "_REVIEW", ReviewSettings())
    monkeypatch.setattr(mat, "_CONVERSATION", [])
    agents = {"gpt": _Agent("gpt", "m-gpt"), "claude": _Agent("claude", "m-claude")}
    inputs = iter(["/consensus --timeout 45 --min-responses 2 What is 17 × 23?"])
    seen = []
    real_run_review = mat.run_review

    async def recording_run_review(question, panel, **options):
        seen.append((question, options["mode"], options["timeout"]))
        return await real_run_review(question, panel, **options)

    async def fake_read(*args, **kwargs):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    async def fake_connect():
        return agents

    monkeypatch.setattr(mat, "run_review", recording_run_review)
    monkeypatch.setattr(mat, "read_burst_submission", fake_read)
    monkeypatch.setattr(mat, "connect_agents", fake_connect)
    monkeypatch.setattr(mat, "print_splash", lambda: None)
    asyncio.run(mat.main())

    assert seen == [("What is 17 × 23?", ReviewMode.QUICK, 45.0)]
    out = buf.getvalue()
    assert "/consensus is now /review --quick. --min-responses isn't needed any more" in out
    assert "Unknown option" not in out


def test_review_panel_respects_configured_subset(monkeypatch):
    monkeypatch.setattr(mat, "_REVIEW", ReviewSettings(agents=["gpt"]))
    agents = {"gpt": _Agent("gpt", "m-gpt"), "claude": _Agent("claude", "m-claude")}
    assert [a.name for a in mat.review_panel(agents)] == ["gpt"]


def test_parse_saver_settings():
    from ixel_mat.runtime import SaverSettings, parse_saver_settings
    names = {"opus", "haiku", "llama"}
    settings, warnings = parse_saver_settings({"saver": {
        "verifier": "opus", "drafters": ["haiku", "llama", "ghost"], "escalate": "disagreement",
        "on_wrong": "correct", "verifier_effort": "low"}}, names)
    assert settings == SaverSettings("opus", ["haiku", "llama"], "disagreement", "low", "correct")
    assert warnings == ["[saver] drafters not configured: ghost"]

    # verifier defaults to the [review] moderator; bad values are reported and ignored
    settings, warnings = parse_saver_settings({"saver": {"escalate": "never", "on_wrong": "shrug",
                                                         "verifier_effort": "ludicrous"}},
                                              names, ReviewSettings(moderator="opus"))
    assert settings == SaverSettings(verifier="opus")
    assert len(warnings) == 3


def test_saver_run_options_carry_every_setting():
    from ixel_mat.agents.base import AgentConfig
    from ixel_mat.runtime import SaverSettings, Settings
    configs = {n: AgentConfig(name=n, label=n, type="http") for n in ("opus", "haiku", "llama")}
    settings = Settings({}, configs, saver=SaverSettings("opus", None, "disagreement", "low", "correct"))
    options = settings.run_options(ReviewMode.SAVER)
    assert options["verifier"] == "opus" and options["escalate"] == "disagreement"
    assert options["verifier_effort"] == "low" and options["on_wrong"] == "correct"
    assert list(settings.configs_for(ReviewMode.SAVER)) == ["haiku", "llama", "opus"]
    assert "verifier" not in settings.run_options(ReviewMode.REVIEW)


def test_terminal_shows_the_verdict_while_it_is_written():
    from ixel_mat import review_ui
    from ixel_mat.modes.review import ReviewEvent

    progress = review_ui.ReviewProgress("q", ReviewMode.QUICK, 2)

    def shown():
        buf = io.StringIO()
        Console(file=buf, width=100).print(progress)
        return buf.getvalue()

    for piece in ["17 × 23 ", "= 391\n", "\n".join(f"line {n}" for n in range(30))]:
        progress.on_event(ReviewEvent("verdict_text", {"text": piece}))
    out = shown()
    assert "being written" in out and "line 29" in out
    assert "17 × 23" not in out  # only the latest lines, so the live view stays inside the window
    progress.on_event(ReviewEvent("final", {"result": {}}))
    assert "being written" not in shown()  # the report prints the finished verdict


def test_piped_questions_are_read_as_utf8_whatever_the_console_uses():
    # ixel.cmd runs python -I, which ignores PYTHONIOENCODING: on Windows stdin was cp1252, and a curly quote
    # from Handoff's panel review arrived as mojibake
    import io

    from ixel_mat.cli import read_piped_text
    text = "Is “this” right? ✓ café"
    assert read_piped_text(io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding="cp1252")) == text
    assert read_piped_text(io.TextIOWrapper(io.BytesIO(b"\xef\xbb\xbf" + text.encode()), encoding="cp1252")) == text
    # Not UTF-8 at all: this computer's own encoding, never a crash
    assert read_piped_text(io.TextIOWrapper(io.BytesIO(b"caf\xe9"), encoding="cp1252")).startswith("caf")
    assert read_piped_text(io.StringIO("plain")) == "plain"
