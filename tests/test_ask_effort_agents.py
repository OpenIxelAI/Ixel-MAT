"""`ixel ask --effort` with several agents: the level goes to the first one only, and --model still needs one agent."""
import json

import pytest
from test_cli_effort import CONFIG, _fake_ask, config_file  # noqa: F401

from ixel_mat import ask, cli


def test_effort_with_several_agents_goes_to_the_first_only(config_file, monkeypatch, capsys):  # noqa: F811
    recorded = []
    monkeypatch.setattr(ask, "ask_in_order", _fake_ask(recorded))
    assert cli.cmd_ask(["--json", "--agent", "claude code,codex", "--effort", "high", "q"]) == 0
    first, second = recorded[0]
    assert first.name == "claude code" and first.effort == "high"
    assert second.name == "codex" and second.effort == ""  # its config sets none, so it keeps that
    assert json.loads(capsys.readouterr().out)["answer"] == "ok"
    assert config_file.read_text(encoding="utf-8") == CONFIG  # nothing saved


def test_the_first_agents_effort_is_checked_like_a_single_agents(config_file, monkeypatch, capsys):  # noqa: F811
    recorded = []
    monkeypatch.setattr(ask, "ask_in_order", _fake_ask(recorded))
    # claude code refuses ultra; codex takes it, but the second agent's levels aren't checked at all
    assert cli.cmd_ask(["--json", "--agent", "claude code,codex", "--effort", "ultra", "q"]) == 1
    assert "takes" in json.loads(capsys.readouterr().out)["error"]
    assert recorded == []


def test_effort_default_with_several_agents_leaves_the_first_without_a_level(config_file, monkeypatch):  # noqa: F811
    recorded = []
    monkeypatch.setattr(ask, "ask_in_order", _fake_ask(recorded))
    assert cli.cmd_ask(["--json", "--agent", "claude code,codex", "--effort", "default", "q"]) == 0
    first, second = recorded[0]
    assert first.effort == "" and second.effort == ""


def test_model_with_several_agents_is_still_refused(config_file, monkeypatch, capsys):  # noqa: F811
    recorded = []
    monkeypatch.setattr(ask, "ask_in_order", _fake_ask(recorded))
    with pytest.raises(SystemExit) as stop:
        cli.cmd_ask(["--json", "--agent", "claude code,codex", "--model", "opus", "q"])
    assert stop.value.code == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert "--model" in error and "one --agent" in error
    assert recorded == []


def test_list_says_it_takes_an_effort_for_the_first_of_several_agents(config_file, capsys):  # noqa: F811
    assert cli.cmd_ask(["--list", "--json"]) == 0
    features = json.loads(capsys.readouterr().out)["features"]
    assert "effort-first" in features
    assert "head" in features and "effort" in features
