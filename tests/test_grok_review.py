"""Grok Build reviews: low effort by default, and 15 minutes to answer."""
import asyncio
import io
import random

import pytest
from rich.console import Console

from ixel_mat import ask as ask_mod
from ixel_mat import review_ui
from ixel_mat.agents.base import AgentConfig
from ixel_mat.effort import review_effort, slow_on_reviews
from ixel_mat.material import Material
from ixel_mat.modes.review import ReviewEvent, ReviewMode, run_review
from ixel_mat.presets import PRESETS_BY_ID
from test_review import QUESTION, PanelAgent


def _grok(**extra):
    return AgentConfig(name="grok", label="Grok Build", type="oneshot", command="grok", **extra)


def _claude():
    return AgentConfig(name="claude", label="Claude Code", type="oneshot", command="claude")


def _http():
    return AgentConfig(name="gpt", label="GPT", type="http", url="https://api.openai.com/v1/chat/completions")


def test_grok_build_gets_fifteen_minutes():
    assert PRESETS_BY_ID["grok_build"]["timeout"] == 900
    assert _grok(timeout=PRESETS_BY_ID["grok_build"]["timeout"]).own_timeout == 900
    assert PRESETS_BY_ID["claude_code"]["timeout"] == 300


def test_a_review_sends_grok_build_low_effort_unless_the_person_set_one():
    assert review_effort(_grok()) == "low"
    assert review_effort(_grok(effort="high")) == "high"
    assert review_effort(_grok(), "medium") == "medium"  # an explicit level wins
    assert review_effort(_claude()) == ""
    assert review_effort(_http()) == ""
    assert review_effort(None) == ""
    assert review_effort(None, "low") == "low"


def test_only_grok_build_is_slow_on_reviews():
    assert slow_on_reviews(_grok()) is True
    assert slow_on_reviews(_claude()) is False
    assert slow_on_reviews(_http()) is False
    assert slow_on_reviews(None) is False


class Recording(PanelAgent):
    """A scripted model that also remembers the effort each call was given."""

    def __init__(self, name, answer):
        super().__init__(name, answer)
        self.efforts = []

    async def send_and_receive(self, message, **kwargs):
        self.efforts.append(kwargs.get("effort"))
        return await super().send_and_receive(message, **kwargs)


def _review(grok, gpt, **kwargs):
    return asyncio.run(run_review(QUESTION, [grok, gpt], mode=ReviewMode.QUICK, moderator="gpt", timeout=5,
                                  rng=random.Random(7), **kwargs))


def test_a_panel_review_asks_grok_build_at_low_and_the_others_as_they_are():
    grok = Recording("grok", "17 × 23 = 391")
    grok.config = _grok()
    gpt = Recording("gpt", "It's 391.")  # no config at all
    result = _review(grok, gpt)
    assert grok.efforts and all(effort == "low" for effort in grok.efforts)
    assert gpt.efforts and all(effort is None for effort in gpt.efforts)
    assert not result.failures

    grok = Recording("grok", "17 × 23 = 391")
    grok.config = _grok(effort="xhigh")
    gpt = Recording("gpt", "It's 391.")
    _review(grok, gpt)
    assert grok.efforts and all(effort == "xhigh" for effort in grok.efforts)


def test_agent_started_says_which_models_are_slow():
    grok = Recording("grok", "17 × 23 = 391")
    grok.config = _grok()
    gpt = Recording("gpt", "It's 391.")
    events = []
    _review(grok, gpt, on_event=events.append)
    started = [e.data for e in events if e.kind == "agent_started"]
    grok_started = [d for d in started if d["agent"] == "grok"]
    gpt_started = [d for d in started if d["agent"] == "gpt"]
    assert grok_started and gpt_started
    assert all(d["slow"] is True for d in grok_started)
    assert all(d["slow"] is False for d in gpt_started)


class FakeAgent:
    def __init__(self):
        self.calls = []

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def send_and_receive(self, message, **kwargs):
        self.calls.append(kwargs)
        return "391"


DIFF = Material(title="git diff", text="-a\n+b", files=["a.py"])


@pytest.mark.parametrize("cfg,material,expected", [
    (_grok(), DIFF, "low"),
    (_grok(effort="medium"), DIFF, "medium"),
    (_grok(), None, None),  # no diff or files: nothing is sent
], ids=["diff at low", "own level kept", "no material"])
def test_ask_with_a_diff_attached_sends_grok_build_low(cfg, material, expected, monkeypatch):
    fake = FakeAgent()
    monkeypatch.setattr(ask_mod, "create_agent", lambda _cfg: fake)
    asyncio.run(ask_mod.ask(cfg, "q", material, timeout=5))
    [kwargs] = fake.calls
    if expected is None:
        assert "effort" not in kwargs
    else:
        assert kwargs["effort"] == expected


def render(renderable, width, **console_options):
    buf = io.StringIO()
    Console(file=buf, width=width, **console_options).print(renderable)
    return buf.getvalue()


def test_the_waiting_line_says_grok_build_can_take_minutes():
    progress = review_ui.ReviewProgress("q", ReviewMode.REVIEW, 2, echo=lambda r: None)
    progress.on_event(ReviewEvent("round", {"number": 1, "total": 3, "round": "answer", "agents": ["Grok Build", "B"]}))
    progress.on_event(ReviewEvent("agent_started", {"agent": "grok", "agent_label": "Grok Build", "slow": True}))
    progress.on_event(ReviewEvent("agent_started", {"agent": "b", "agent_label": "B"}))
    live = render(progress, 120)
    assert live.count("can take several minutes") == 1
    assert live.count("working…") == 2
