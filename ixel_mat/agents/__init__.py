# agents package
from ixel_mat.agents.base import AgentConfig, BaseAgent

# Each transport is imported only when it's used, since its SDK is slow to load (anthropic alone takes a
# second) and every `ixel` command imports this package as it starts (`launch.add_install_folders`).
_TRANSPORTS = {
    "HttpAgent": "ixel_mat.agents.http",
    "WebSocketAgent": "ixel_mat.agents.websocket",
    "SubprocessAgent": "ixel_mat.agents.subprocess",
    "OneShotAgent": "ixel_mat.agents.oneshot",
}


def __getattr__(name):
    """`from ixel_mat.agents import HttpAgent` still works: the transport is imported the first time it's named."""
    if name in _TRANSPORTS:
        import importlib
        return getattr(importlib.import_module(_TRANSPORTS[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def create_agent(config: AgentConfig) -> BaseAgent:
    """Factory: create the right agent transport from config type."""
    match config.type:
        case "http":
            from ixel_mat.agents.http import HttpAgent
            return HttpAgent(config)
        case "websocket":
            from ixel_mat.agents.websocket import WebSocketAgent
            return WebSocketAgent(config, response_timeout=config.transport_timeout)
        case "subprocess":
            from ixel_mat.agents.subprocess import SubprocessAgent
            return SubprocessAgent(config)
        case "oneshot":
            from ixel_mat.agents.oneshot import OneShotAgent
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
