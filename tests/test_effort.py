"""Only the thinking levels each model takes: offered in Settings, and sent with a question."""
import pytest

from ixel_mat.agents.base import AgentConfig
from ixel_mat.agents.oneshot import OneShotAgent
from ixel_mat.effort import (LOW_TO_HIGH, MINIMAL_TO_HIGH, NONE, UP_TO_MAX, UP_TO_XHIGH, agent_levels, model_levels,
                             to_send)
from ixel_mat.presets import PRESETS_BY_ID


@pytest.mark.parametrize("company,model,levels", [
    # Anthropic: none on Haiku 4.5 and Sonnet 4.5, no xhigh on the 4.6s, the whole ladder from Opus 4.7 on
    ("anthropic", "claude-haiku-4-5-20251001", NONE),
    ("anthropic", "claude-sonnet-4-5", NONE),
    ("anthropic", "claude-opus-4-1-20250805", NONE),
    ("anthropic", "claude-3-7-sonnet-latest", NONE),
    ("anthropic", "claude-haiku-4", NONE),
    ("anthropic", "claude-opus-4-5", LOW_TO_HIGH),
    ("anthropic", "claude-opus-4-6", ("low", "medium", "high", "max")),
    ("anthropic", "claude-sonnet-4-6", ("low", "medium", "high", "max")),
    ("anthropic", "claude-opus-4-7", UP_TO_MAX),
    ("anthropic", "claude-opus-5-5", UP_TO_MAX),
    ("anthropic", "claude-sonnet-5-5", UP_TO_MAX),
    ("anthropic", "claude-fable-5-1", UP_TO_MAX),
    ("anthropic", "claude-opus-6", UP_TO_MAX),          # one that isn't out yet
    ("anthropic", "latest", UP_TO_MAX),
    ("anthropic", "latest-fast", NONE),                 # Haiku
    ("anthropic", "", UP_TO_MAX),
    # OpenAI: GPT-6 low to max (no minimal), GPT-5 minimal to high, none on the chat-only models
    ("openai", "gpt-6-astra", UP_TO_MAX),
    ("openai", "gpt-6.1-sol", UP_TO_MAX),
    ("openai", "gpt-6-luna", UP_TO_MAX),
    ("openai", "gpt-5", MINIMAL_TO_HIGH),
    ("openai", "gpt-5-mini", MINIMAL_TO_HIGH),
    ("openai", "gpt-5.1", LOW_TO_HIGH),
    ("openai", "gpt-5.5", UP_TO_XHIGH),
    ("openai", "o3", LOW_TO_HIGH),
    ("openai", "gpt-4.1", NONE),
    ("openai", "gpt-5-chat-latest", NONE),
    ("openai", "gpt-4o", NONE),
    ("openai", "gpt-30", UP_TO_MAX),                    # a two-digit version is a new one, not GPT-3
    ("openai", "latest-fast", UP_TO_MAX),
    ("openai", "gpt-5.1-codex-max", UP_TO_XHIGH),
    # xAI: xhigh from Grok 4.6 on; none on Grok 4 itself or the fast models, which turn it down
    ("xai", "grok-4.7", UP_TO_XHIGH),
    ("xai", "grok-4.6", UP_TO_XHIGH),
    ("xai", "grok-4.20-multi-agent", UP_TO_XHIGH),
    ("xai", "grok-4.5", LOW_TO_HIGH),
    ("xai", "grok-4.7-fast", NONE),
    ("xai", "grok-4-0709", NONE),
    ("xai", "grok-code-fast-1", NONE),
    ("xai", "latest-fast", NONE),
    # Google: no minimal on 3.8 Flash (it errors) or the Pros, minimal on 3 Flash and 2.5
    ("gemini", "gemini-3.8-flash", LOW_TO_HIGH),
    ("gemini", "gemini-3.7-flash", LOW_TO_HIGH),
    ("gemini", "gemini-3.1-pro-preview", LOW_TO_HIGH),
    ("gemini", "gemini-3-pro-preview", ("low", "high")),
    ("gemini", "gemini-3-flash-preview", MINIMAL_TO_HIGH),
    ("gemini", "gemini-3.1-flash-lite", MINIMAL_TO_HIGH),
    ("gemini", "gemini-2.5-pro", MINIMAL_TO_HIGH),
    ("gemini", "gemini-2.0-flash", NONE),
    ("gemini", "gemini-1.5-pro", NONE),
    ("gemini", "gemini-10-pro", LOW_TO_HIGH),          # ...nor Gemini 1
    ("gemini", "models/gemini-3-pro-preview", ("low", "high")),  # as Google's own list writes it
    # Any other server: the OpenAI scale it passes on or ignores
    (None, "llama3.3:latest", MINIMAL_TO_HIGH),
])
def test_each_model_takes_the_levels_its_company_documents(company, model, levels):
    assert model_levels(company, model) == levels


def test_a_level_the_model_doesnt_take_is_sent_as_the_nearest_or_not_at_all():
    assert to_send(UP_TO_MAX, "minimal") == "low"                     # GPT-6, Claude: no minimal
    assert to_send(UP_TO_XHIGH, "max") == "xhigh"                     # Grok 4.7
    assert to_send(("low", "medium", "high", "max"), "xhigh") == "high"  # Opus 4.6: ties go lower
    assert to_send(UP_TO_MAX, "medium") == "medium"
    assert to_send(NONE, "high") is None                              # Haiku 4.5: nothing sent
    assert to_send(UP_TO_MAX, "") is None


def _preset(pid, **extra):
    preset = PRESETS_BY_ID[pid]
    fields = {k: preset[k] for k in ("command", "args", "effort_args", "effort_levels", "model_args") if k in preset}
    return AgentConfig(name=pid, label=preset["label"], type="oneshot", **{**fields, **extra})


def test_programs_take_their_own_levels_and_codex_its_models_too():
    assert agent_levels(_preset("claude_code")) == UP_TO_MAX
    assert agent_levels(_preset("gemini_cli")) == NONE                # no effort flag
    assert agent_levels(_preset("opencode")) == NONE
    # Codex hands the level to OpenAI: GPT-6 (its default) has no minimal, GPT-5 no xhigh, GPT-4.1 none
    assert agent_levels(_preset("codex")) == UP_TO_XHIGH
    assert agent_levels(_preset("codex", model="gpt-5")) == MINIMAL_TO_HIGH
    assert agent_levels(_preset("codex", model="gpt-4.1")) == NONE
    # A program of your own with an effort flag and no list takes every level
    mine = AgentConfig(name="x", label="X", type="oneshot", command="mine", effort_args=["--e", "{effort}"])
    assert agent_levels(mine) == ("minimal", "low", "medium", "high", "xhigh", "max")


def test_codex_is_never_given_a_level_its_model_turns_down():
    def flags(effort, **extra):
        cmd, _ = OneShotAgent(_preset("codex", **extra))._build_command("q", effort=effort)
        return [a for a in cmd if "model_reasoning_effort" in a]

    assert flags(effort="minimal") == ['model_reasoning_effort="low"']
    assert flags(effort="max") == ['model_reasoning_effort="xhigh"']
    assert flags(effort="minimal", model="gpt-5") == ['model_reasoning_effort="minimal"']
    assert flags(effort="high", model="gpt-4.1") == []


def test_an_api_model_is_judged_by_the_model_it_resolved_to():
    cfg = AgentConfig(name="a", label="A", type="http", url="https://api.anthropic.com/v1/messages", model="latest")
    assert agent_levels(cfg) == UP_TO_MAX
    assert agent_levels(cfg, "claude-haiku-4-5") == NONE
