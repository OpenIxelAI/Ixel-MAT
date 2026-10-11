"""The command line's model and thinking level: `ixel effort`, `ixel model --effort`, `ixel ask --model/--effort`."""
import json

import pytest

from ixel_mat import ask, cli, effort
from ixel_mat.ask import AskResult
from ixel_mat.config import loader

CONFIG = ('# Ixel MAT — config\n'
          '[agents.gpt]\ntype = "http"\nurl = "https://api.openai.com/v1/chat/completions"\nmodel = "gpt-5"\n'
          'label = "GPT"\n\n'
          '[agents."claude code"]\ntype = "oneshot"\ncommand = "claude"\npreset = "claude_code"\n'
          'label = "Claude Code"\n\n'
          '[agents.codex]\ntype = "oneshot"\npreset = "codex"\nlabel = "Codex"\n\n'
          '[agents.gemini]\ntype = "oneshot"\npreset = "gemini_cli"\nlabel = "Gemini CLI"\n\n'
          '[review]\nmode = "review"\n')


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setattr(loader, "_GLOBAL_CONFIG", path)
    monkeypatch.setattr(loader, "find_config", lambda explicit_path=None: path)
    monkeypatch.setattr(cli.console, "width", 200)
    monkeypatch.setattr(cli, "_live_models", lambda cfg: ([], "openai"))
    return path


def test_setting_an_effort_adds_one_line_under_its_table(config_file):
    loader.set_agent_effort(config_file, "claude code", "high")
    assert config_file.read_text(encoding="utf-8") == CONFIG.replace(
        '[agents."claude code"]\n', '[agents."claude code"]\neffort = "high"\n')
    assert (config_file.parent / "config.toml.bak").read_text(encoding="utf-8") == CONFIG
    loader.set_agent_effort(config_file, "claude code", "default")
    assert config_file.read_text(encoding="utf-8") == CONFIG
    assert loader.load_config()["review"] == {"mode": "review"}


def test_an_effort_the_agent_doesnt_take_is_refused_and_nothing_is_written(config_file):
    with pytest.raises(ValueError, match="takes: low, medium, high"):
        loader.set_agent_effort(config_file, "claude code", "max", takes=["low", "medium", "high"])
    with pytest.raises(ValueError, match="takes no effort level"):
        loader.set_agent_effort(config_file, "gemini", "high", takes=[])
    with pytest.raises(KeyError):
        loader.set_agent_effort(config_file, "nobody", "high")
    with pytest.raises(ValueError):
        loader.set_agent_effort(config_file, "codex", "--yolo")
    assert config_file.read_text(encoding="utf-8") == CONFIG


def test_ixel_effort_lists_every_agents_levels(config_file, monkeypatch, capsys):
    monkeypatch.delattr(effort, "native_levels", raising=False)
    assert cli.cmd_effort([]) == 0
    out = capsys.readouterr().out
    for name in ("gpt", "claude code", "codex", "gemini"):
        assert name in out
    assert "Low" in out and "Max" in out
    assert "—" in out  # the agents that take no level


def test_ixel_effort_for_one_agent_shows_its_levels_or_why_it_has_none(config_file, monkeypatch, capsys):
    monkeypatch.delattr(effort, "native_levels", raising=False)
    assert cli.cmd_effort(["claude code"]) == 0
    assert "xhigh (Extra high)" in capsys.readouterr().out
    assert cli.cmd_effort(["gemini"]) == 0
    assert "Gemini's own settings" in capsys.readouterr().out
    assert cli.cmd_effort(["nobody"]) == 1
    assert "No agent named" in capsys.readouterr().out


def test_ixel_effort_sets_a_level_it_takes_and_refuses_one_it_doesnt(config_file, monkeypatch, capsys):
    monkeypatch.delattr(effort, "native_levels", raising=False)
    assert cli.cmd_effort(["claude code", "xhigh"]) == 0
    assert "now thinks at xhigh" in capsys.readouterr().out
    assert 'effort = "xhigh"' in config_file.read_text(encoding="utf-8")

    before = config_file.read_text(encoding="utf-8")
    assert cli.cmd_effort(["claude code", "ultra"]) == 1
    assert "it takes: low, medium, high, xhigh, max" in capsys.readouterr().out
    assert config_file.read_text(encoding="utf-8") == before

    assert cli.cmd_effort(["gemini", "high"]) == 1
    assert config_file.read_text(encoding="utf-8") == before


def test_a_program_can_give_its_own_levels_and_http_agents_dont_ask_it(config_file, monkeypatch, capsys):
    calls = []

    def native_levels(program, model):
        calls.append((program, model))
        return [{"value": "low", "label": "Low"}, {"value": "ultra", "label": "Ultra"}]

    monkeypatch.setattr(effort, "native_levels", native_levels, raising=False)
    assert cli.cmd_effort(["codex"]) == 0
    assert "ultra (Ultra)" in capsys.readouterr().out
    assert calls == [("codex", None)]

    assert cli.cmd_effort(["codex", "ultra"]) == 0
    assert 'effort = "ultra"' in config_file.read_text(encoding="utf-8")

    calls.clear()
    assert cli.cmd_effort(["gpt"]) == 0
    assert calls == []


def test_ixel_model_sets_a_model_and_its_effort_together(config_file, capsys):
    assert cli.cmd_model(["claude code", "opus", "--effort", "high"]) == 0
    out = capsys.readouterr().out
    assert "now uses" in out and "now thinks at" in out
    text = config_file.read_text(encoding="utf-8")
    assert 'model = "opus"' in text and 'effort = "high"' in text


def test_ixel_model_with_a_level_it_doesnt_take_saves_neither(config_file, capsys):
    assert cli.cmd_model(["claude code", "opus", "--effort", "ultra"]) == 1
    assert "takes" in capsys.readouterr().out
    assert config_file.read_text(encoding="utf-8") == CONFIG


def test_ixel_model_effort_needs_a_value(config_file, capsys):
    assert cli.cmd_model(["claude code", "opus", "--effort"]) == 1
    assert "--effort goes with an agent and a model" in capsys.readouterr().out
    assert config_file.read_text(encoding="utf-8") == CONFIG


def test_ixel_model_help_mentions_effort(capsys):
    assert cli.cmd_model(["--help"]) == 0
    assert "--effort" in capsys.readouterr().out


def _fake_ask(recorded):
    async def ask_in_order(cfgs, question, material, timeout=None, on_skip=None):
        recorded.append(cfgs)
        return AskResult(agent="claude code", label="Claude Code", model="opus", answer="ok", ms=1)

    return ask_in_order


def test_ask_uses_a_model_and_effort_for_one_run_and_saves_neither(config_file, monkeypatch, capsys):
    recorded = []
    monkeypatch.setattr(ask, "ask_in_order", _fake_ask(recorded))
    assert cli.cmd_ask(["--json", "--agent", "claude code", "--model", "opus", "--effort", "high", "q"]) == 0
    cfg = recorded[0][0]
    assert cfg.model == "opus" and cfg.effort == "high"
    assert json.loads(capsys.readouterr().out)["answer"] == "ok"
    assert config_file.read_text(encoding="utf-8") == CONFIG


def test_ask_refuses_a_level_the_model_doesnt_take(config_file, monkeypatch, capsys):
    recorded = []
    monkeypatch.setattr(ask, "ask_in_order", _fake_ask(recorded))
    assert cli.cmd_ask(["--json", "--agent", "claude code", "--effort", "ultra", "q"]) == 1
    assert "takes" in json.loads(capsys.readouterr().out)["error"]
    assert recorded == []


def test_ask_takes_one_agent_for_a_model_or_effort(config_file, monkeypatch, capsys):
    recorded = []
    monkeypatch.setattr(ask, "ask_in_order", _fake_ask(recorded))
    with pytest.raises(SystemExit) as stop:
        cli.cmd_ask(["--json", "--agent", "claude code,codex", "--model", "opus", "q"])
    assert stop.value.code == 1
    assert "one --agent" in json.loads(capsys.readouterr().out)["error"]
    assert recorded == []


def test_ask_list_says_it_takes_a_model_and_an_effort(config_file, capsys):
    assert cli.cmd_ask(["--list", "--json"]) == 0
    features = json.loads(capsys.readouterr().out)["features"]
    assert "model" in features and "effort" in features


def test_ixel_effort_help_is_help(capsys):
    assert cli.cmd_effort(["--help"]) == 0
    assert capsys.readouterr().out.startswith("usage: ixel effort")
