from ixel_mat.config.loader import validate_config


def test_validate_config_catches_missing_fields_for_each_agent_type():
    config = {
        "agents": {
            "ws": {"type": "websocket", "label": "WS"},
            "http": {"type": "http", "label": "HTTP"},
            "sub": {"type": "subprocess", "label": "Sub"},
            "one": {"type": "oneshot", "label": "One"},
        }
    }

    issues = validate_config(config)

    assert "Agent 'ws': missing url" in issues
    assert any("Agent 'ws': token not set" in issue for issue in issues)
    assert "Agent 'http': missing url" in issues
    assert "Agent 'http': API key not set (need: )" in issues
    assert "Agent 'http': missing model (ixel model http lists the server's models)" in issues
    assert "Agent 'sub': missing command" in issues
    assert "Agent 'one': missing command" in issues


def test_validate_config_catches_invalid_url_scheme_and_unknown_type():
    config = {
        "agents": {
            "http": {
                "type": "http",
                "label": "HTTP",
                "url": "notaurl",
                "token": "secret",
                "model": "gpt-4o",
            },
            "bad": {"type": "mystery", "label": "Bad"},
        }
    }

    issues = validate_config(config)

    assert "Agent 'http': url must start with http:// or https://" in issues
    assert "Agent 'bad': unknown type 'mystery'" in issues
