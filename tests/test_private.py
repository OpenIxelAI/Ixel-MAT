"""Private ([review] private = true): only models on your own computers answer, and nothing else hears the
question: not a verdict writer, verifier or Triage elsewhere, and not a sound service."""
import asyncio
import json

import pytest

from ixel_mat import sound
from ixel_mat.config.loader import build_agent_configs
from ixel_mat.local_models import stays_on_your_computers
from ixel_mat.modes.review import ReviewMode
from ixel_mat.runtime import ReviewSettings, SaverSettings, Settings, choose_mode, settings_from
from ixel_mat.triage import TriageSettings

AGENTS = {
    "llama": {"type": "http", "url": "http://127.0.0.1:11434/v1", "model": "llama3.2", "label": "Llama"},
    "mac": {"type": "http", "url": "http://mac-mini:1234/v1", "model": "qwen3:14b", "label": "Qwen (mac-mini)"},
    "gpt": {"type": "http", "url": "https://api.openai.com/v1/chat/completions", "token_env": "IXEL_TEST_KEY",
            "model": "gpt-6-astra", "label": "GPT"},
    "claude_code": {"preset": "claude_code", "label": "Claude Code"},
}


def settings(private=True, agents=AGENTS, **review):
    config = {"agents": agents, "review": {"private": private, **review}}
    return settings_from(config)


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("IXEL_TEST_KEY", "sk-test")


# ── Who counts as yours ───────────────────────────────────────────────────────

@pytest.mark.parametrize("agent, yours", [
    ({"url": "http://127.0.0.1:11434/v1", "model": "llama3.2"}, True),
    ({"url": "http://192.168.1.20:1234/v1", "model": "qwen3:14b"}, True),
    ({"url": "http://mac-mini.tail1.ts.net:1234/v1", "model": "qwen3:14b"}, True),
    ({"url": "http://127.0.0.1:11434/v1", "model": "gpt-oss:120b-cloud"}, False),   # runs on ollama.com
    ({"url": "http://127.0.0.1:4000/v1", "model": "gpt-6", "token_env": "IXEL_TEST_KEY"}, False),  # a gateway
    ({"url": "http://127.0.0.1:4000/v1", "model": "gpt-6", "billing": "api"}, False),
    ({"url": "https://api.openai.com/v1/chat/completions", "model": "gpt-6"}, False),
    ({"url": "http://llm.example.com/v1", "model": "x"}, False),
])
def test_only_a_keyless_model_server_on_your_computers_counts(agent, yours):
    configs, _ = build_agent_configs({"agents": {"a": {"type": "http", "label": "A", **agent}}})
    assert stays_on_your_computers(configs["a"]) is yours


def test_a_malformed_address_doesnt_count_and_doesnt_stop_anything():
    configs, warnings = build_agent_configs({"agents": {
        "bad": {"type": "http", "url": "http://[bad/v1/chat/completions", "model": "x", "label": "Bad"},
        "llama": AGENTS["llama"]}})
    assert "bad" in configs and not stays_on_your_computers(configs["bad"])
    s = Settings({}, configs, review=ReviewSettings(private=True))
    assert list(s.panel_configs()) == ["llama"] and s.sitting_out() == ["Bad"]


def test_a_program_ixel_starts_never_counts():
    configs, _ = build_agent_configs({"agents": {"oc": {"preset": "opencode", "model": "ollama/qwen3:8b"}}})
    assert not stays_on_your_computers(configs["oc"])


# ── What runs ─────────────────────────────────────────────────────────────────

def test_only_your_models_answer_and_the_rest_sit_out():
    s = settings()
    assert list(s.panel_configs()) == ["llama", "mac"]
    assert s.sitting_out() == ["GPT", "Claude Code"]
    off = settings(private=False)
    assert list(off.panel_configs()) == list(AGENTS) and off.sitting_out() == []


def test_a_verdict_writer_elsewhere_sits_out_and_the_best_answers_author_writes_it():
    assert settings(moderator="gpt").moderator is None
    assert settings(moderator="mac").moderator == "mac"
    assert settings(private=False, moderator="gpt").moderator == "gpt"
    assert settings(moderator="gpt").run_options(ReviewMode.REVIEW)["moderator"] is None


def test_saver_needs_a_verifier_of_yours():
    s = settings_from({"agents": AGENTS, "review": {"private": True}, "saver": {"verifier": "gpt"}})
    assert s.verifier is None and "gpt" not in s.saver_configs() and "claude_code" not in s.saver_configs()
    assert "Saver's verifier (GPT) isn't on your computers" in s.private_problem(ReviewMode.SAVER)
    assert s.private_problem(ReviewMode.REVIEW) == ""
    mine = settings_from({"agents": AGENTS, "review": {"private": True}, "saver": {"verifier": "mac"}})
    assert list(mine.saver_configs()) == ["llama", "mac"] and mine.private_problem(ReviewMode.SAVER) == ""


def test_with_no_model_of_yours_nothing_is_asked():
    s = settings(agents={k: v for k, v in AGENTS.items() if k in ("gpt", "claude_code")})
    assert s.panel_configs() == {}
    assert "none of your models runs on your own computers" in s.private_problem(ReviewMode.REVIEW)


def test_a_bad_value_is_reported_and_private_stays_off():
    s = settings(private="yes")
    assert not s.private and any("[review] private must be true or false" in w for w in s.warnings)


def with_triage(triage, **review):
    configs, _ = build_agent_configs({"agents": AGENTS})
    return Settings({}, configs, review=ReviewSettings(private=True, **review), triage=triage)


def test_triage_elsewhere_is_off_and_auto_mode_says_why():
    s = with_triage(TriageSettings(enabled=True, token="ts-test", auto_mode=True), auto=True)
    assert s.triage.ready and not s.active_triage.ready and "triage" not in s.run_options(ReviewMode.REVIEW)
    mode, decision = asyncio.run(choose_mode(s, "auto", "q", []))
    assert mode is ReviewMode.REVIEW and not decision.asked
    assert "Private is on, and Triage isn't one of your own models" in decision.note


def test_triage_by_a_model_of_yours_still_decides():
    configs, _ = build_agent_configs({"agents": AGENTS})
    for agent, ready in (("mac", True), ("gpt", False)):
        triage = TriageSettings(enabled=True, provider="model", agent=agent, agent_config=configs[agent])
        assert with_triage(triage).active_triage.ready is ready


# ── Sound ─────────────────────────────────────────────────────────────────────

def test_sound_isnt_sent_to_a_company_under_private(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    assert sound.ready({"sound": {"provider": "groq"}})[0].name == "groq"
    provider, problem = sound.ready({"sound": {"provider": "groq"}, "review": {"private": True}})
    assert provider is None and "Private is on, and writing out sound would send it to Groq" in problem
    monkeypatch.delenv("GROQ_API_KEY")
    provider, problem = sound.ready({"review": {"private": True}})
    assert provider is None and problem == sound.PRIVATE
    # A sound server on this computer, set as the service's address, is fine
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    local = {"sound": {"provider": "openai", "openai_url": "http://127.0.0.1:8000/v1/audio/transcriptions"},
             "review": {"private": True}}
    assert sound.ready(local)[0].url.startswith("http://127.0.0.1:8000/")


# ── The app ───────────────────────────────────────────────────────────────────

def gui(s):
    from ixel_mat.gui.server import GuiServer
    connected = []

    async def connect(cfgs, on_result=None):
        connected.append(list(cfgs))
        return {}

    async def disconnect(agents):
        pass

    return GuiServer(token="t0ken", settings_loader=lambda: s, connect=connect, disconnect=disconnect), connected


def ask_app(server, scenario):
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        test_server = TestServer(server.app(), host="127.0.0.1")
        async with TestClient(test_server) as client:
            server.port = test_server.port
            return await scenario(client)
    return asyncio.run(go())


AUTH = {"Authorization": "Bearer t0ken", "Content-Type": "application/json"}


def test_the_ask_page_shows_private_and_who_sits_out():
    configs, _ = build_agent_configs({"agents": AGENTS})
    s = Settings({}, configs, review=ReviewSettings(private=True, moderator="gpt"),
                 triage=TriageSettings(enabled=True, token="ts-test"))
    server, _ = gui(s)

    async def scenario(client):
        return await (await client.get("/api/panel", headers=AUTH)).json()

    panel = ask_app(server, scenario)
    assert [a["name"] for a in panel["agents"]] == ["llama", "mac"]
    assert panel["private"] == {"on": True, "sitting_out": ["GPT", "Claude Code"]}
    assert panel["review"]["moderator"] is None and not panel["triage"]["ready"]


def test_a_question_with_no_model_of_yours_is_never_sent():
    configs, _ = build_agent_configs({"agents": {k: AGENTS[k] for k in ("gpt",)}})
    server, connected = gui(Settings({}, configs, review=ReviewSettings(private=True)))

    async def scenario(client):
        resp = await client.post("/api/review", data=json.dumps({"question": "What is 17 × 23?"}), headers=AUTH)
        return [json.loads(line) for line in (await resp.text()).splitlines() if line.strip()]

    events = ask_app(server, scenario)
    assert events[-1]["kind"] == "error" and "Private is on" in events[-1]["data"]["message"]
    assert connected == []


def test_settings_turns_private_on_and_off(tmp_path):
    from ixel_mat.config import loader
    from test_gui_settings import CONFIG, change, snapshot
    path = loader._GLOBAL_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG, encoding="utf-8")
    assert snapshot()["review"]["private"] is False
    yours = {a["name"]: a["yours"] for a in snapshot()["agents"]}
    assert yours["local"] and not yours["gpt"] and not yours["claude"]
    assert change("review", {"private": True})[0] == 200
    assert "private = true" in path.read_text(encoding="utf-8") and snapshot()["review"]["private"] is True
    assert change("review", {"private": False})[0] == 200
    assert "private" not in path.read_text(encoding="utf-8") and snapshot()["review"]["private"] is False
    assert change("review", {"private": "yes"})[0] == 400


# ── The plugin ────────────────────────────────────────────────────────────────

def test_the_plugin_refuses_too():
    import mcp
    from contextlib import asynccontextmanager
    from ixel_mat.mcp_server import build_server

    configs, _ = build_agent_configs({"agents": {"gpt": AGENTS["gpt"]}})
    s = Settings({}, configs, review=ReviewSettings(private=True))

    @asynccontextmanager
    async def panel(mode=None):
        yield s, [], []

    async def go():
        async with mcp.Client(build_server(panel)) as client:
            return await client.call_tool("ixel_review", {"question": "q"})

    result = asyncio.run(go())
    assert result.is_error and "Private is on" in result.content[0].text


# ── The terminal ──────────────────────────────────────────────────────────────

def test_ixel_review_says_why_and_asks_no_one(tmp_path):
    from fake_providers import ThreadedFakeProvider, panel_handler
    from test_review_cli import ANSWERS, run_ixel, write_config
    with ThreadedFakeProvider(panel_handler(ANSWERS)) as fake:
        # The panel here takes a key, so it's taken for a gateway: none of it is yours
        write_config(tmp_path, fake.openai_url, "[review]\nprivate = true\n")
        proc = run_ixel(tmp_path, "review", "What is 17 × 23?")
        assert proc.returncode == 1 and "Private is on" in proc.stdout + proc.stderr
        assert not fake.requests


def test_ixel_ask_asks_only_your_own_models(tmp_path):
    from fake_providers import ThreadedFakeProvider, panel_handler
    from test_review_cli import ANSWERS, run_ixel, write_config
    with ThreadedFakeProvider(panel_handler(ANSWERS)) as fake:
        write_config(tmp_path, fake.openai_url, "[review]\nprivate = true\n")
        proc = run_ixel(tmp_path, "ask", "--agent", "gpt", "What is 17 × 23?")
        assert proc.returncode == 1 and "Private is on, and GPT isn't on your computers" in proc.stdout + proc.stderr
        assert not fake.requests


def test_slash_review_refuses_a_verdict_writer_elsewhere(monkeypatch):
    import io
    from rich.console import Console
    from ixel_mat import mat

    out = io.StringIO()
    configs, _ = build_agent_configs({"agents": AGENTS})
    monkeypatch.setattr(mat, "console", Console(file=out, width=200, color_system=None))
    monkeypatch.setattr(mat, "_settings", lambda: Settings({}, configs, review=ReviewSettings(private=True)))
    asyncio.run(mat._review(mat.parse_review_args("--moderator gpt What is 17 × 23?"), {}))
    assert "Private is on, and gpt isn't on your computers" in out.getvalue()


def test_your_models_off_the_panel_are_named_as_the_reason():
    s = settings_from({"agents": AGENTS, "review": {"private": True, "agents": ["gpt", "claude_code"]}})
    assert "none of the models on your panel runs on your own computers" in s.private_problem(ReviewMode.REVIEW)


def test_handoff_in_ask_is_off(monkeypatch):
    from ixel_mat.gui import handoff

    async def dispatch(*args, **kwargs):
        pytest.fail("handed over")

    monkeypatch.setattr(handoff, "dispatch", dispatch)
    configs, _ = build_agent_configs({"agents": AGENTS})
    server, _ = gui(Settings({}, configs, review=ReviewSettings(private=True)))

    async def scenario(client):
        out = []
        for path in ("/api/handoff/plan", "/api/handoff/run"):
            resp = await client.post(path, data=json.dumps({"project": "/tmp/shop", "request": "fix it"}), headers=AUTH)
            out.append((resp.status, (await resp.json())["error"]))
        return out

    for status, error in ask_app(server, scenario):
        assert status == 403 and "Private is on, and /handoff gives your request to coding agents" in error


def test_the_terminal_app_connects_only_your_models(monkeypatch):
    import io
    from rich.console import Console
    from ixel_mat import mat

    configs, _ = build_agent_configs({"agents": AGENTS})
    seen = []

    async def connect(cfgs, on_result=None):
        seen.append(list(cfgs))
        return {}

    monkeypatch.setattr(mat, "_AGENT_CONFIGS", configs)
    monkeypatch.setattr(mat, "_settings", lambda: Settings({}, configs, review=ReviewSettings(private=True)))
    monkeypatch.setattr(mat, "_connect_agents", connect)
    monkeypatch.setattr(mat, "console", Console(file=io.StringIO(), width=200))
    asyncio.run(mat.connect_agents())
    assert seen == [["llama", "mac"]]


def test_compare_in_the_terminal_leaves_out_a_model_elsewhere(monkeypatch):
    import io
    from rich.console import Console
    from ixel_mat import mat

    class Connected:
        is_connected = True
        name = label = "gpt"

        async def query(self, *args, **kwargs):
            pytest.fail("asked a model elsewhere")

    out = io.StringIO()
    configs, _ = build_agent_configs({"agents": AGENTS})
    monkeypatch.setattr(mat, "console", Console(file=out, width=200, color_system=None))
    monkeypatch.setattr(mat, "_settings", lambda: Settings({}, configs, review=ReviewSettings(private=True)))
    asyncio.run(mat._compare("secret question", {"gpt": Connected()}))
    assert "No connected agents" in out.getvalue()


def test_ixel_ask_lists_only_your_own_models(tmp_path):
    from test_review_cli import run_ixel, write_config
    write_config(tmp_path, "https://api.openai.com/v1/chat/completions", "[review]\nprivate = true\n")
    proc = run_ixel(tmp_path, "ask", "--list")
    assert proc.returncode == 1 and "none of your models runs on your own computers" in proc.stdout + proc.stderr
    proc = run_ixel(tmp_path, "ask", "--list", "--json")
    assert json.loads(proc.stdout)["agents"] == []


def test_only_a_model_of_yours_reads_your_files_for_a_picture():
    from ixel_mat import images
    configs, _ = build_agent_configs({"agents": AGENTS})
    provider = images.PROVIDERS["xai"]
    mine = {n: c for n, c in configs.items() if n in ("llama", "mac")}
    assert images.pick_writer(mine, provider, private=True).name == "llama"
    with pytest.raises(images.ImageError, match="Private is on, so only a model on your own computers"):
        images.pick_writer(mine, provider, "gpt", private=True)
    with pytest.raises(images.ImageError, match="Private is on"):
        images.pick_writer({}, provider, private=True)


def test_a_conversation_asked_in_private_isnt_continued_with_it_off(tmp_path):
    from fake_providers import ThreadedFakeProvider, panel_handler
    from test_review_cli import ANSWERS, run_ixel, write_config
    from ixel_mat.conversation import asked_in_private, save_conversation
    from ixel_mat.modes.review import EarlierTurn
    with ThreadedFakeProvider(panel_handler(ANSWERS)) as fake:
        write_config(tmp_path, fake.openai_url)
        saved = tmp_path / ".config" / "ixel-mat" / "conversation.json"
        save_conversation([EarlierTurn("What is my salary?", "Secret.")], saved, private=True)
        assert asked_in_private(saved)
        proc = run_ixel(tmp_path, "review", "--quick", "--continue", "And doubled?")
        assert proc.returncode == 0, proc.stderr
        assert "asked with Private on, so it stays with your own models" in proc.stdout
        assert fake.requests and not any("What is my salary?" in json.dumps(r.body) for r in fake.requests)
        assert not asked_in_private(saved)  # what's saved now was asked with Private off


def test_a_private_conversation_isnt_carried_on_by_a_page_that_missed_private_going_off():
    """Turned off in the file or another window: the page still sends the turns, and the server says no."""
    configs, _ = build_agent_configs({"agents": AGENTS})
    earlier = [{"question": "What is my salary?", "answer": "Secret.", "private": True}]

    def ask(s):
        server, connected = gui(s)

        async def scenario(client):
            resp = await client.post("/api/review", data=json.dumps({"question": "And doubled?", "earlier": earlier}),
                                     headers=AUTH)
            return [json.loads(line) for line in (await resp.text()).splitlines() if line.strip()]
        return ask_app(server, scenario), connected

    events, connected = ask(Settings({}, configs, review=ReviewSettings()))
    assert events[-1] == {"kind": "error", "data": {"code": "private_off", "message": (
        "This conversation was asked with Private on, and Private is off now, so its earlier questions stay with "
        "your own models. Ask again to start a new conversation.")}}
    assert connected == []
    events, connected = ask(Settings({}, configs, review=ReviewSettings(private=True)))
    assert connected == [["llama", "mac"]]  # with Private still on, it goes on with your own models
