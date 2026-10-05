# agents package
from ixel_mat.agents.base import AgentConfig, BaseAgent
from ixel_mat.agents.websocket import WebSocketAgent
from ixel_mat.agents.http import HttpAgent
from ixel_mat.agents.subprocess import SubprocessAgent
from ixel_mat.agents.oneshot import OneShotAgent


def create_agent(config: AgentConfig) -> BaseAgent:
    """Factory: create the right agent transport from config type."""
    match config.type:
        case "http":
            return HttpAgent(config)
        case "websocket":
            return WebSocketAgent(config, response_timeout=config.transport_timeout)
        case "subprocess":
            return SubprocessAgent(config)
        case "oneshot":
            return OneShotAgent(config)
        case _:
            raise ValueError(
                f"Unknown agent type: {config.type!r} for agent {config.name!r}"
            )


__all__ = [
    "AgentConfig",
    "BaseAgent",
    "WebSocketAgent",
    "HttpAgent",
    "SubprocessAgent",
    "OneShotAgent",
    "create_agent",
]
