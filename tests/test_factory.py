import pytest

from ixel_mat.agents import create_agent
from ixel_mat.agents.base import AgentConfig
from ixel_mat.agents.http import HttpAgent
from ixel_mat.agents.oneshot import OneShotAgent
from ixel_mat.agents.subprocess import SubprocessAgent
from ixel_mat.agents.websocket import WebSocketAgent


@pytest.mark.parametrize(
    ("agent_type", "expected_type", "extra"),
    [
        ("http", HttpAgent, {"url": "https://example.com", "token": "tok", "model": "gpt-4o"}),
        ("websocket", WebSocketAgent, {"url": "ws://127.0.0.1:18789", "token": "tok"}),
        ("subprocess", SubprocessAgent, {"command": "python3"}),
        ("oneshot", OneShotAgent, {"command": "python3"}),
    ],
)
def test_create_agent_returns_expected_transport(agent_type, expected_type, extra):
    cfg = AgentConfig(name="a1", label="Agent", type=agent_type, **extra)
    agent = create_agent(cfg)
    assert isinstance(agent, expected_type)


def test_create_agent_raises_on_unknown_type():
    cfg = AgentConfig(name="bad", label="Bad", type="mystery")
    with pytest.raises(ValueError, match="Unknown agent type"):
        create_agent(cfg)
