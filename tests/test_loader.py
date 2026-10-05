from ixel_mat.config.loader import build_agent_configs


def test_build_agent_configs_returns_configs_and_warnings_tuple(monkeypatch):
    monkeypatch.delenv("MISSING_WS_TOKEN", raising=False)
    config = {
        "agents": {
            "ws": {
                "type": "websocket",
                "label": "WS",
                "url": "ws://127.0.0.1:18789",
                "token_env": "MISSING_WS_TOKEN",
            },
            "http": {
                "type": "http",
                "label": "HTTP",
                "url": "https://example.com",
                "token": "abc",
                "model": "gpt-4o",
            },
        }
    }

    configs, warnings = build_agent_configs(config)

    assert set(configs) == {"ws", "http"}
    assert configs["ws"].type == "websocket"
    assert configs["http"].token == "abc"
    assert isinstance(warnings, list)
    assert any("Agent 'ws': no token found" in warning for warning in warnings)


def test_build_agent_configs_handles_empty_config():
    configs, warnings = build_agent_configs({})
    assert configs == {}
    assert warnings == []
