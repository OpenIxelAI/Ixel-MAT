"""Each program is offered its provider's own effort levels for the model it runs, in the provider's own names."""
import pytest

from ixel_mat.agents.base import EFFORT_LEVELS, AgentConfig
from ixel_mat.config import loader
from ixel_mat.agents.oneshot import OneShotAgent
from ixel_mat.effort import (LOW_TO_HIGH, NONE, UP_TO_MAX, UP_TO_ULTRA, UP_TO_XHIGH, agent_choices, agent_levels,
                             native_levels, program_levels, to_send, why_none)
from ixel_mat.presets import PRESETS_BY_ID


def _values(choices):
    return [c["value"] for c in choices]


def _preset(pid, **extra):
    preset = PRESETS_BY_ID[pid]
    fields = {k: preset[k] for k in ("command", "args", "effort_args", "effort_levels", "effort_in_model", "model_args")
              if k in preset}
    return AgentConfig(name=pid, label=preset["label"], type="oneshot", **{**fields, **extra})


def test_ultra_comes_after_max_on_ixels_scale():
    assert EFFORT_LEVELS[-2:] == ("max", "ultra")
    assert to_send(UP_TO_MAX, "ultra") == "max"
    assert to_send(UP_TO_XHIGH, "ultra") == "xhigh"


def test_codex_offers_each_models_own_levels():
    sol = native_levels("codex", "gpt-6.1-sol")
    assert _values(sol) == list(UP_TO_ULTRA)
    assert sol[-1] == {"value": "ultra", "label": "Ultra"}
    for model in ("gpt-6-astra", "gpt-6-sol", "gpt-5.6-sol", "gpt-5.6-terra"):
        assert program_levels("codex", model) == UP_TO_ULTRA
    for model in ("gpt-6-luna", "gpt-5.6-luna"):
        assert program_levels("codex", model) == UP_TO_MAX
    assert _values(native_levels("codex", "gpt-5.5"))[-1] == "xhigh"
    assert program_levels("codex", "gpt-5.5") == UP_TO_XHIGH
    assert program_levels("codex", None) == UP_TO_XHIGH              # its own default isn't known
    assert program_levels("codex", "gpt-5") == ("minimal", "low", "medium", "high")
    assert program_levels("codex", "gpt-4.1") == NONE


def test_codex_sends_ultra_to_a_model_that_takes_it_and_the_nearest_to_one_that_doesnt():
    def flags(effort, **extra):
        cmd, _ = OneShotAgent(_preset("codex", **extra))._build_command("q", effort=effort)
        return [a for a in cmd if "model_reasoning_effort" in a]

    assert agent_levels(_preset("codex", model="gpt-6.1-sol")) == UP_TO_ULTRA
    assert flags("ultra", model="gpt-6.1-sol") == ['model_reasoning_effort="ultra"']
    assert flags("ultra", model="gpt-6-luna") == ['model_reasoning_effort="max"']
    assert flags("ultra", model="gpt-5.5") == ['model_reasoning_effort="xhigh"']


def test_claude_code_knows_its_aliases_and_haiku_takes_none():
    assert native_levels("claude_code", "haiku") == []
    assert native_levels("claude_code", "claude-haiku-4-5") == []
    assert program_levels("claude_code", "opus") == UP_TO_MAX
    assert program_levels("claude_code", "fable") == UP_TO_MAX
    assert program_levels("claude_code", "sonnet") == UP_TO_MAX
    assert program_levels("claude_code", "claude-opus-4-6") == ("low", "medium", "high", "max")
    assert program_levels("claude_code", None) == UP_TO_MAX
    assert "minimal" not in _values(native_levels("claude_code", None))

    def flags(effort, **extra):
        cmd, _ = OneShotAgent(_preset("claude_code", **extra))._build_command("q", effort=effort)
        return cmd[cmd.index("--effort") + 1] if "--effort" in cmd else None

    assert flags("high", model="haiku") is None
    assert flags("high", model="opus") == "high"


def test_grok_build_never_takes_max():
    assert _values(native_levels("grok_build", "grok-4.5"))[-1] == "high"
    assert program_levels("grok_build", "grok-4.5") == LOW_TO_HIGH
    for model in ("grok-4.7", "grok-4.7-build-fast", "grok-4.6", None):
        assert program_levels("grok_build", model) == UP_TO_XHIGH
    assert native_levels("grok_build", "grok-4.7")[-1] == {"value": "xhigh", "label": "Extra high"}
    assert program_levels("grok_build", "grok-4.7-fast") == NONE       # an API-only model


def test_antigravity_takes_none_for_a_model_that_carries_its_own_effort():
    assert native_levels("antigravity", "gemini-3.8-flash-high") == []
    assert native_levels("antigravity", "gemini-3.1-pro-low") == []
    assert program_levels("antigravity", "gemini-3.1-pro-preview") == UP_TO_MAX
    assert program_levels("antigravity", None) == UP_TO_MAX
    cfg = _preset("antigravity", model="gemini-3.8-flash-high")
    assert agent_choices(cfg) == []
    cmd, _ = OneShotAgent(cfg)._build_command("q", effort="high")
    assert "--effort" not in cmd
    assert "carries its own effort" in why_none(cfg)


def test_gemini_cli_takes_none_and_says_where_to_set_it():
    assert native_levels("gemini_cli", None) == []
    assert native_levels("gemini_cli", "gemini-3.1-pro") == []
    reason = why_none(_preset("gemini_cli"))
    assert "Gemini's own settings" in reason and not reason.endswith(".")


def test_opencode_and_copilot_keep_their_levels():
    assert program_levels("opencode", "anthropic/claude-opus-5-5") == LOW_TO_HIGH
    assert program_levels("opencode", "anthropic/claude-opus-5-5#max") == NONE
    assert program_levels("opencode", "ollama/llama3.3") == NONE
    assert program_levels("copilot", None) == tuple(PRESETS_BY_ID["copilot"]["effort_levels"])
    assert program_levels("nobody", "x") == NONE


def test_an_agents_choices_are_its_programs_native_levels_narrowed_by_its_config():
    assert _values(agent_choices(_preset("codex", model="gpt-6.1-sol"))) == list(UP_TO_ULTRA)
    assert _values(agent_choices(_preset("codex", model="gpt-6.1-sol", effort_levels=["low", "high"]))) == \
        ["low", "high"]
    assert agent_choices(_preset("codex", model="gpt-6.1-sol", effort_args=None)) == []
    assert _values(agent_choices(_preset("codex"), "gpt-6-luna")) == list(UP_TO_MAX)  # another model than its own
    api = AgentConfig(name="a", label="A", type="http", url="https://api.anthropic.com/v1/messages",
                      model="claude-haiku-4-5")
    assert agent_choices(api) == []
    assert why_none(api) == "claude-haiku-4-5 has no effort setting, so Ixel sends none"
    mine = AgentConfig(name="x", label="X", type="oneshot", command="mine", effort_args=["--e", "{effort}"])
    assert _values(agent_choices(mine)) == ["minimal", "low", "medium", "high", "xhigh", "max"]


# -- loading and saving --

def _load(**agent):
    return loader.build_agent_configs({"agents": {"codex": {"preset": "codex", **agent}}})


def test_a_level_the_program_takes_for_its_model_is_kept_on_load():
    configs, warnings = _load(model="gpt-6.1-sol", effort="ultra")
    assert configs["codex"].effort == "ultra"
    assert not [w for w in warnings if "effort" in w]


def test_a_level_nobody_takes_is_dropped_on_load_with_a_warning():
    configs, warnings = _load(model="gpt-6.1-sol", effort="turbo")
    assert configs["codex"].effort == ""
    assert any("effort must be one of" in w for w in warnings)


def test_a_level_on_ixels_scale_the_model_doesnt_take_is_kept_and_sent_as_the_nearest():
    configs, _ = _load(effort="minimal")
    cfg = configs["codex"]
    assert cfg.effort == "minimal"
    assert to_send(agent_levels(cfg), "minimal") == "low"


SAVED = ('[agents.codex]\npreset = "codex"\nmodel = "gpt-6.1-sol"\n\n'
         '[agents."claude code"]\npreset = "claude_code"\n\n'
         '[agents.grok]\npreset = "grok_build"\n')


@pytest.fixture
def saved_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(SAVED, encoding="utf-8")
    monkeypatch.setattr(loader, "_GLOBAL_CONFIG", path)
    monkeypatch.setattr(loader, "find_config", lambda explicit_path=None: path)
    return path


def _takes(name):
    configs, _ = loader.build_agent_configs(loader.load_config())
    return [c["value"] for c in agent_choices(configs[name])]


def test_ultra_saves_on_a_codex_model_that_takes_it_and_loads_back(saved_file):
    loader.set_agent_effort(saved_file, "codex", "ultra", takes=_takes("codex"))
    assert 'effort = "ultra"' in saved_file.read_text(encoding="utf-8")
    configs, _ = loader.build_agent_configs(loader.load_config())
    assert configs["codex"].effort == "ultra"


def test_a_level_the_model_doesnt_take_isnt_saved(saved_file):
    with pytest.raises(ValueError, match="doesn't take"):
        loader.set_agent_effort(saved_file, "claude code", "minimal", takes=_takes("claude code"))
    with pytest.raises(ValueError, match="doesn't take"):
        loader.set_agent_effort(saved_file, "grok", "max", takes=_takes("grok"))
    assert saved_file.read_text(encoding="utf-8") == SAVED
