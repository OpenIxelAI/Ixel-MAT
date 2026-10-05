"""model = "latest" / "latest-fast": resolved from the provider's live list, never a stale name."""
import asyncio

import pytest

from fake_providers import FakeProvider
from ixel_mat.agents import http as http_mod
from ixel_mat.agents.base import AgentConfig
from ixel_mat.agents.http import HttpAgent
from ixel_mat.models import models_url, pick_latest, provider_for_url, valid_model_id

OPENAI = [{"id": "gpt-4o", "created": 1}, {"id": "gpt-5", "created": 2}, {"id": "gpt-5-mini", "created": 3},
          {"id": "gpt-5.5", "created": 5}, {"id": "gpt-5.5-mini", "created": 6}, {"id": "gpt-5.5-codex"},
          {"id": "gpt-5-2025-08-07"}, {"id": "o3"}, {"id": "gpt-image-1"}]
GEMINI = ["models/gemini-2.5-pro", "models/gemini-2.5-flash", "models/gemini-3-pro-preview",
          "models/gemini-3-flash-preview", "models/gemini-3-flash-lite", "models/gemini-2.5-flash-image"]
XAI = ["grok-3", "grok-3-mini", "grok-4", "grok-4-fast", "grok-4-0709", "grok-code-fast-1"]
ANTHROPIC = ["claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5", "claude-haiku-4-5-20251001",
             "claude-opus-5"]  # newest first, as the API lists them


@pytest.mark.parametrize("provider,models,alias,expected", [
    ("openai", OPENAI, "latest", "gpt-5.5"),
    ("openai", OPENAI, "latest-fast", "gpt-5.5-mini"),
    ("gemini", GEMINI, "latest", "gemini-3-pro-preview"),
    ("gemini", GEMINI, "latest-fast", "gemini-3-flash-preview"),
    ("xai", XAI, "latest", "grok-4"),
    ("xai", XAI, "latest-fast", "grok-4-fast"),
    ("anthropic", ANTHROPIC, "latest", "claude-opus-5-5"),
    ("anthropic", ANTHROPIC, "latest-fast", "claude-haiku-4-5-20251001"),
    ("openai", ["o3", "gpt-image-1"], "latest", None),
    ("somewhere", OPENAI, "latest", None),
])
def test_pick_latest(provider, models, alias, expected):
    assert pick_latest(provider, models, alias) == expected


def test_stable_release_beats_a_preview_of_the_same_version():
    assert pick_latest("gemini", ["gemini-3-pro-preview", "gemini-3-pro"]) == "gemini-3-pro"
    # A dated preview's date isn't more version: it used to outrank the stable release
    assert pick_latest("gemini", ["gemini-3-pro", "gemini-3-pro-preview-06-05"]) == "gemini-3-pro"
    assert pick_latest("gemini", ["gemini-3-pro-preview-06-05", "gemini-3-pro"]) == "gemini-3-pro"
    # Previews of one version: the newer date. A preview of a newer version still beats an older stable one
    assert pick_latest("gemini", ["gemini-3-pro-preview-06-05", "gemini-3-pro-preview-05-06"]) == \
        "gemini-3-pro-preview-06-05"
    assert pick_latest("gemini", ["gemini-2.5-pro", "gemini-3-pro-preview-06-05"]) == "gemini-3-pro-preview-06-05"


def test_provider_and_models_url():
    assert provider_for_url("https://api.anthropic.com/v1/messages") == "anthropic"
    assert provider_for_url("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions") == "gemini"
    assert provider_for_url("http://127.0.0.1:11434/v1/chat/completions") is None
    assert models_url("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions") == \
        "https://generativelanguage.googleapis.com/v1beta/openai/models"


@pytest.mark.parametrize("value,ok", [("gpt-5.5", True), ("openai/gpt-5.5", True), ("llama3.3:latest", True),
                                      ("--dangerously-skip-permissions", False), ("", False), ("a b", False)])
def test_model_ids_can_never_be_flags(value, ok):
    assert valid_model_id(value) is ok


@pytest.fixture(autouse=True)
def fresh_cache():
    http_mod._RESOLVED.clear()
    yield
    http_mod._RESOLVED.clear()


def _agent(url, model):
    return HttpAgent(AgentConfig(name="a", label="A", type="http", url=url, token="sk-test", model=model))


@pytest.mark.parametrize("model", ["latest", ""])
def test_openai_compatible_agent_uses_the_latest_model(monkeypatch, model):
    monkeypatch.setattr(http_mod, "provider_for_url", lambda url: "openai")

    async def go():
        async with FakeProvider(models=["gpt-5", "gpt-5.5", "gpt-5.5-mini"]) as fake:
            agent = _agent(fake.openai_url, model)
            await agent.connect()
            await agent.send_and_receive("hi")
            await agent.disconnect()
            return agent.model, fake.requests[-1].body["model"]

    assert asyncio.run(go()) == ("gpt-5.5", "gpt-5.5")


def test_latest_is_looked_up_per_key_and_refreshed(monkeypatch):
    # Two accounts on one API can see different models, and a long-running server must
    # notice a new release: the lookup used to be shared by URL and kept forever.
    monkeypatch.setattr(http_mod, "provider_for_url", lambda url: "openai")

    async def resolve(url, token):
        agent = HttpAgent(AgentConfig(name="a", label="A", type="http", url=url, token=token, model="latest"))
        await agent.connect()
        await agent.disconnect()
        return agent.model

    async def go():
        async with FakeProvider(models=["gpt-5.5"]) as fake:
            first = await resolve(fake.openai_url, "sk-one")
            fake.models = ["gpt-5.5", "gpt-6"]
            other_account = await resolve(fake.openai_url, "sk-two")
            same_account = await resolve(fake.openai_url, "sk-one")      # still cached
            monkeypatch.setattr(http_mod, "RESOLVED_FOR", 0.0, raising=False)
            refreshed = await resolve(fake.openai_url, "sk-one")
            return first, other_account, same_account, refreshed

    assert asyncio.run(go()) == ("gpt-5.5", "gpt-6", "gpt-5.5", "gpt-6")
    assert not any("sk-one" in str(key) for key in http_mod._RESOLVED)  # the key itself isn't kept


def test_anthropic_agent_uses_the_newest_top_model(monkeypatch):
    monkeypatch.setattr(http_mod, "_anthropic_base_url", lambda url: holder["base"])
    holder = {}

    async def go():
        async with FakeProvider(models=ANTHROPIC) as fake:
            holder["base"] = f"http://127.0.0.1:{fake.port}"
            agent = _agent("https://api.anthropic.com/v1/messages", "latest-fast")
            await agent.connect()
            await agent.send_and_receive("hi")
            await agent.disconnect()
            return agent.model, fake.requests[-1].body["model"]

    assert asyncio.run(go()) == ("claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001")


def test_latest_on_a_local_server_asks_for_a_name():
    agent = _agent("http://127.0.0.1:9/v1/chat/completions", "latest")
    with pytest.raises(ValueError, match="set a model by name"):
        asyncio.run(agent.connect())


def test_no_fitting_model_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(http_mod, "provider_for_url", lambda url: "openai")

    async def go():
        async with FakeProvider(models=["o3"]) as fake:
            await _agent(fake.openai_url, "latest").connect()

    with pytest.raises(RuntimeError, match="ixel model a <model>"):
        asyncio.run(go())


# ── ixel model: see and change models without rerunning setup ────────────────

from ixel_mat.config import loader  # noqa: E402

CONFIG = ('# Ixel MAT \u2014 config\n[agents.gpt]\ntype = "http"\nurl = "https://api.openai.com/v1/chat/completions"\n'
          'model = "gpt-5"\nlabel = "GPT"\n\n[agents."claude code"]\ntype = "oneshot"\ncommand = "claude"\n'
          'label = "Claude Code"\n\n[review]\nmode = "review"\n')


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(loader, "_GLOBAL_CONFIG", path)
    return path


def test_changing_a_model_edits_only_that_line(config_file):
    loader.set_agent_model(config_file, "gpt", "latest")
    text = config_file.read_text(encoding="utf-8")
    assert text == CONFIG.replace('model = "gpt-5"', 'model = "latest"')
    assert (config_file.parent / "config.toml.bak").read_text(encoding="utf-8") == CONFIG


def test_a_comment_after_the_model_is_kept(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[agents.gpt]\ntype = "http"\n  model = "gpt-5"   # the one my team pays for\n', encoding="utf-8")
    loader.set_agent_model(path, "gpt", "gpt-5.5")
    assert path.read_text(encoding="utf-8").splitlines()[2] == '  model = "gpt-5.5"   # the one my team pays for'


def test_a_model_is_added_to_a_quoted_table_and_default_removes_it(config_file):
    loader.set_agent_model(config_file, "claude code", "opus")
    assert loader.load_config()["agents"]["claude code"]["model"] == "opus"
    loader.set_agent_model(config_file, "claude code", "default")
    assert "model" not in loader.load_config()["agents"]["claude code"]
    assert loader.load_config()["review"] == {"mode": "review"}


@pytest.mark.parametrize("agent,model,error", [("gpt", "--yolo", ValueError), ("nobody", "latest", KeyError)])
def test_bad_changes_are_refused_and_nothing_is_written(config_file, agent, model, error):
    with pytest.raises(error):
        loader.set_agent_model(config_file, agent, model)
    assert config_file.read_text(encoding="utf-8") == CONFIG


def test_windows_line_endings_are_kept(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes(CONFIG.replace("\n", "\r\n").encode("utf-8"))
    loader.set_agent_model(path, "gpt", "gpt-5.5")
    assert b'model = "gpt-5.5"\r\n' in path.read_bytes() and b"\n" not in path.read_bytes().replace(b"\r\n", b"")


def test_ixel_model_lists_and_changes(config_file, monkeypatch, capsys):
    from ixel_mat import cli
    monkeypatch.setattr(cli, "_live_models", lambda cfg: (["gpt-5", "gpt-5.5", "gpt-5.5-mini"], "openai"))
    monkeypatch.setattr(loader, "find_config", lambda explicit_path=None: config_file)
    monkeypatch.setattr(cli.console, "width", 160)

    assert cli.cmd_model([]) == 0
    listing = capsys.readouterr().out
    assert "gpt-5" in listing and "the CLI's own default" in listing

    assert cli.cmd_model(["gpt", "latest"]) == 0
    assert "now uses" in capsys.readouterr().out
    assert cli.cmd_model(["gpt"]) == 0
    one = capsys.readouterr().out
    assert "gpt-5.5" in one and "latest-fast = gpt-5.5-mini" in one
    assert "Available: gpt-5.5-mini, gpt-5.5, gpt-5" in one  # newest first
    assert cli.cmd_model(["gpt", "--bad"]) == 1


@pytest.mark.parametrize("agent,model", [("box", "latest"), ("box", "latest-fast"), ("box", "default"),
                                         ("claude code", "latest")])
def test_ixel_model_refuses_newest_where_no_one_can_pick_it(tmp_path, monkeypatch, capsys, agent, model):
    from ixel_mat import cli
    path = tmp_path / "config.toml"
    text = CONFIG + '\n[agents.box]\ntype = "http"\nurl = "http://127.0.0.1:11434/v1/chat/completions"\nmodel = "llama3"\n'
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(loader, "_GLOBAL_CONFIG", path)
    monkeypatch.setattr(loader, "find_config", lambda explicit_path=None: path)
    monkeypatch.setattr(cli.console, "width", 200)
    assert cli.cmd_model([agent, model]) == 1
    assert "can't work there" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == text  # nothing changed


def test_ixel_model_help_is_help(capsys):
    from ixel_mat import cli
    assert cli.cmd_model(["--help"]) == 0
    assert "usage: ixel model" in capsys.readouterr().out


def test_gpt_6_tiers_and_grok_release_dates():
    gpt6 = [{"id": "gpt-5.5", "created": 5}, {"id": "gpt-5.5-mini", "created": 6}, {"id": "gpt-6-astra", "created": 8},
            {"id": "gpt-6-sol", "created": 8}, {"id": "gpt-6-luna", "created": 8}, {"id": "gpt-6.1-sol", "created": 9}]
    assert pick_latest("openai", gpt6, "latest") == "gpt-6-astra"
    assert pick_latest("openai", gpt6, "latest-fast") == "gpt-6.1-sol"
    # grok-4.20 came out before grok-4.7, so the release dates decide when the list gives them
    grok = [{"id": "grok-4.20", "created": 100}, {"id": "grok-4.7", "created": 300}, {"id": "grok-4.6", "created": 200},
            {"id": "grok-4.20-fast", "created": 100}, {"id": "grok-4.7-fast", "created": 300}]
    assert pick_latest("xai", grok, "latest") == "grok-4.7"
    assert pick_latest("xai", grok, "latest-fast") == "grok-4.7-fast"
    assert pick_latest("xai", ["grok-4.6", "grok-4.7"]) == "grok-4.7"  # no dates: the version
