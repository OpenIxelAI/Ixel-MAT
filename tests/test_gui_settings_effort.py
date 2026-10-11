"""The Settings page offers each model its provider's own effort levels, labelled, and saves only one it takes;
Claude Code also has an Ultracode switch of its own."""
import json

from ixel_mat.config import loader
from ixel_mat.runtime import load_settings
from test_gui_settings import CONFIG, _keys_put_back, change, config, snapshot  # noqa: F401

PROGRAMS = CONFIG + """
[agents.codex]
preset = "codex"
model = "gpt-6.1-sol"

[agents.grok]
preset = "grok_build"
model = "grok-4.5"

[agents.short]
preset = "claude_code"
model = "haiku"

[agents.anti]
preset = "antigravity"
model = "gemini-3.8-flash-high"

[agents.gem]
preset = "gemini_cli"
"""


def _agents():
    return {a["name"]: a for a in snapshot()["agents"]}


def test_each_agent_is_sent_its_own_levels_with_labels(config):
    config.write_text(PROGRAMS, encoding="utf-8")
    agents = _agents()
    codex = agents["codex"]
    assert [c["value"] for c in codex["levels"]] == codex["efforts"] == \
        ["low", "medium", "high", "xhigh", "max", "ultra"]
    assert codex["levels"][-1] == {"value": "ultra", "label": "Ultra"}
    assert {"value": "xhigh", "label": "Extra high"} in agents["claude"]["levels"]
    assert agents["grok"]["efforts"] == ["low", "medium", "high"]  # grok-4.5: no xhigh, and never max
    assert agents["gpt"]["levels"][0] == {"value": "minimal", "label": "Minimal"}
    for name in ("short", "anti", "gem"):
        assert agents[name]["levels"] == [] and not agents[name]["has_effort"]
    assert agents["short"]["effort_note"] == "Claude Code takes no effort level with haiku, so Ixel sends none."
    assert "carries its own effort" in agents["anti"]["effort_note"]
    assert "Gemini's own settings" in agents["gem"]["effort_note"]


def test_a_level_the_model_takes_is_saved_even_outside_ixels_old_scale(config):
    config.write_text(PROGRAMS, encoding="utf-8")
    assert change("agent", {"effort": "ultra"}, agent="codex")[0] == 200
    assert load_settings().agent_configs["codex"].effort == "ultra"
    assert _agents()["codex"]["effort_sent"] == "ultra"
    assert change("agent", {"effort": "default"}, agent="codex")[0] == 200
    assert load_settings().agent_configs["codex"].effort == ""


def test_a_level_the_model_doesnt_take_is_refused_and_says_what_it_takes(config):
    config.write_text(PROGRAMS, encoding="utf-8")
    status, data = change("agent", {"effort": "max"}, agent="grok")
    assert status == 400 and data["error"] == "Grok Build doesn't take 'max'; it takes: low, medium, high."
    status, data = change("agent", {"effort": "minimal"}, agent="claude")
    assert status == 400 and "doesn't take 'minimal'" in data["error"]
    status, data = change("agent", {"effort": "turbo"}, agent="gpt")
    assert status == 400 and "doesn't take 'turbo'" in data["error"]
    status, data = change("agent", {"effort": "high"}, agent="short")
    assert status == 400 and data["error"] == "Claude Code takes no effort level with haiku, so Ixel sends none."
    status, data = change("agent", {"effort": "high"}, agent="gem")
    assert status == 400 and "Gemini's own settings" in data["error"]
    assert config.read_text(encoding="utf-8") == PROGRAMS


def test_a_level_set_with_a_new_model_is_checked_against_that_model(config):
    config.write_text(PROGRAMS, encoding="utf-8")
    status, data = change("agent", {"model": "gpt-5.5", "effort": "ultra"}, agent="codex")
    assert status == 400 and "it takes: low, medium, high, xhigh." in data["error"]
    assert config.read_text(encoding="utf-8") == PROGRAMS
    # Whichever comes first in the request
    assert change("agent", {"effort": "ultra", "model": "gpt-6-sol"}, agent="codex")[0] == 200
    settings = load_settings()
    assert (settings.agent_configs["codex"].model, settings.agent_configs["codex"].effort) == ("gpt-6-sol", "ultra")
    assert change("agent", {"model": "opus", "effort": "xhigh"}, agent="short")[0] == 200


OPENCODE = """
[agents.oc]
preset = "opencode"
model = "anthropic/claude-opus-5-5#high"
"""


def test_the_page_says_where_effort_is_set_when_there_is_no_dropdown(config):
    config.write_text(PROGRAMS + OPENCODE, encoding="utf-8")
    agents = _agents()
    assert agents["gem"]["effort_where"] == "Set in Gemini's own settings"
    assert agents["oc"]["effort_where"] == "Part of the model name"
    assert agents["codex"]["effort_where"] == ""
    assert snapshot()["choices"]["effort_labels"]["xhigh"] == "Extra high"


def test_ultracode_is_a_switch_on_claude_code_only_off_by_default(config):
    assert _agents()["claude"]["ultracode"] is False
    assert _agents()["claude"]["program"] == "claude_code"
    assert change("agent", {"ultracode": True}, agent="claude")[0] == 200
    assert loader.load_config()["agents"]["claude"]["ultracode"] is True
    assert load_settings().agent_configs["claude"].ultracode is True
    assert _agents()["claude"]["ultracode"] is True
    assert change("agent", {"ultracode": False}, agent="claude")[0] == 200
    assert "ultracode" not in loader.load_config()["agents"]["claude"]


def test_ultracode_is_refused_elsewhere_and_when_it_isnt_on_or_off(config):
    config.write_text(PROGRAMS, encoding="utf-8")
    status, data = change("agent", {"ultracode": True}, agent="codex")
    assert status == 400 and data["error"] == "Ultracode is Claude Code's own switch, so Codex hasn't got one."
    status, data = change("agent", {"ultracode": "yes"}, agent="claude")
    assert status == 400
    assert config.read_text(encoding="utf-8") == PROGRAMS


def test_ultracode_set_by_hand_on_another_program_is_ignored_with_a_warning(config):
    config.write_text(PROGRAMS.replace('model = "gpt-6.1-sol"\n', 'model = "gpt-6.1-sol"\nultracode = true\n'),
                      encoding="utf-8")
    settings = load_settings()
    assert settings.agent_configs["codex"].ultracode is False
    assert any("ultracode is Claude Code's own switch" in w for w in settings.warnings)


def _settings_args(cmd):
    """The JSON given to each --settings in cmd."""
    return [json.loads(cmd[i + 1]) for i, arg in enumerate(cmd) if arg == "--settings"]


def test_ultracode_goes_into_claude_codes_settings_json_at_any_effort(config):
    from ixel_mat.agents.oneshot import OneShotAgent, _with_ultracode
    config.write_text(CONFIG + "\n", encoding="utf-8")
    off = OneShotAgent(load_settings().agent_configs["claude"])._build_command("hi")[0]
    assert all("ultracode" not in s for s in _settings_args(off))
    assert change("agent", {"ultracode": True}, agent="claude")[0] == 200
    cfg = load_settings().agent_configs["claude"]
    for level in ("", "low", "high", "max"):
        cmd, _ = OneShotAgent(cfg)._build_command("hi", effort=level)
        # One --settings only (Claude Code keeps the last), the lockdown still in it
        [settings] = _settings_args(cmd)
        assert settings["ultracode"] is True and settings["disableAllHooks"] is True
        assert settings["permissions"]["deny"] == ["Read"]
        assert ("--effort" in cmd) == bool(level)
    # A config with its own args and no --settings gets one
    assert _with_ultracode(["claude", "-p"]) == ["claude", "-p", "--settings", '{"ultracode": true}']
    # Claude Code reads the last --settings, written either way: that's the one the switch goes into
    lockdown = '{"disableAllHooks": true}'
    assert _with_ultracode(["claude", "--settings", "{}", "--settings", lockdown]) == [
        "claude", "--settings", "{}", "--settings", '{"disableAllHooks": true, "ultracode": true}']
    assert _with_ultracode(["claude", f"--settings={lockdown}"]) == [
        "claude", '--settings={"disableAllHooks": true, "ultracode": true}']
    assert _with_ultracode(["claude", "--settings=/home/x/settings.json"]) == [
        "claude", "--settings=/home/x/settings.json"]  # a file's path: left as it is
