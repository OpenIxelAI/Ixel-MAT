"""The Settings page offers each model its provider's own effort levels, labelled, and saves only one it takes."""
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
