import os
import stat
import tempfile
import unittest
from pathlib import Path

from ixel_mat.agents.base import AgentConfig
from ixel_mat.cli import classify_probe_status, get_secret_file_status, remediation_hint, summarize_agent_probe


class CliStatusHelperTests(unittest.TestCase):
    def test_classify_probe_status_variants(self):
        self.assertEqual(classify_probe_status(True, "key valid")[0], "ok")
        self.assertEqual(classify_probe_status(False, "HTTP 429")[0], "rate_limited")
        self.assertEqual(classify_probe_status(False, "invalid key (401 Unauthorized)")[0], "auth_failed")
        self.assertEqual(classify_probe_status(False, "connection error: timeout")[0], "unreachable")

    def test_get_secret_file_status_reports_permissions_and_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("TOKEN=abc\n")
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            info = get_secret_file_status(path)
            self.assertTrue(info["exists"])
            if os.name == "posix":  # Windows has no owner/group/other mode bits
                self.assertEqual(info["permissions_octal"], "600")
            self.assertIn("last_modified", info)
            self.assertTrue(info["last_modified"])

    def test_remediation_hint_for_auth_failure(self):
        hint = remediation_hint("auth_failed", "invalid key (401 Unauthorized)", "anthropic")
        self.assertIn("ixel setup", hint)
        self.assertIn("anthropic", hint)

    def test_summarize_agent_probe_marks_http_as_auth_probe(self):
        cfg = AgentConfig(
            name="openai",
            label="OpenAI",
            type="http",
            url="https://api.openai.com/v1/chat/completions",
            token="tok",
            model="gpt-4o",
        )
        status, detail = summarize_agent_probe(cfg, "ok", "auth ok", latency_ms=123)
        self.assertIn("auth ok", status)
        self.assertIn("123ms", detail)

    def test_summarize_agent_probe_marks_websocket_as_connected(self):
        cfg = AgentConfig(name="gw", label="Gateway", type="websocket", url="ws://127.0.0.1", token="tok")
        status, detail = summarize_agent_probe(cfg, "ok", "connected")
        self.assertIn("connected", status)
        self.assertEqual(detail, "transport ready")


if __name__ == "__main__":
    unittest.main()


# ── Probing endpoints Ixel has no preset for (local model servers…) ──────────

import json as _json
import threading as _threading
from http.server import BaseHTTPRequestHandler as _Handler, ThreadingHTTPServer as _Server

import pytest

from ixel_mat.cli import _probe_other_http


@pytest.fixture
def models_server():
    state = {"status": 200, "models": ["llama3.3:latest", "qwen3:8b"], "auth": []}

    class Handler(_Handler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state["auth"].append(self.headers.get("Authorization"))
            body = _json.dumps({"data": [{"id": m} for m in state["models"]]}).encode()
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = _Server(("127.0.0.1", 0), Handler)
    _threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
    yield state
    server.shutdown()
    server.server_close()


def _local(url, model, token=""):
    return AgentConfig(name="x", label="X", type="http", url=url, model=model, token=token)


def test_local_server_with_the_model_is_reachable(models_server):
    status, detail, latency = _probe_other_http(_local(models_server["url"], "llama3.3"))
    assert status == "ok" and latency is not None
    assert models_server["auth"] == [None]  # no key, no Authorization header
    label, _ = summarize_agent_probe(_local(models_server["url"], "llama3.3"), status, detail, latency)
    assert "reachable" in label


def test_local_server_without_the_model_says_which_it_has(models_server):
    status, detail, _ = _probe_other_http(_local(models_server["url"], "mistral"))
    assert status == "model_missing" and "mistral" in detail and "qwen3:8b" in detail


def test_rejected_key_is_reported_as_auth_failure(models_server):
    models_server["status"] = 401
    status, _, _ = _probe_other_http(_local(models_server["url"], "llama3.3", token="sk-wrong"))
    assert status == "auth_failed" and models_server["auth"] == ["Bearer sk-wrong"]


def test_nothing_listening_is_unreachable():
    status, detail, _ = _probe_other_http(_local("http://127.0.0.1:9/v1/chat/completions", "m"))
    assert status == "unreachable" and "no answer" in detail


def test_remote_endpoint_without_a_key_is_not_called():
    status, detail, _ = _probe_other_http(_local("https://gateway.example/v1/chat/completions", "m"))
    assert status == "auth_failed" and detail == "no API key set"


# ── CLI agents: installed or not, never "connected" ───────────────────────────

import asyncio
import sys

import pytest

from ixel_mat.cli import _probe_agent_connection


@pytest.mark.parametrize("command,status,shown", [
    ("ixel-test-no-such-cli", "not_installed", "✗ not installed"),
    (sys.executable, "ok", "✓ installed"),
], ids=["missing", "installed"])
def test_status_and_agents_say_whether_a_cli_is_installed(command, status, shown):
    cfg = AgentConfig(name="cc", label="Claude Code", type="oneshot", command=command)
    got, detail, _, _ = asyncio.run(_probe_agent_connection(cfg))
    assert got == status
    label, _ = summarize_agent_probe(cfg, got, detail)
    assert shown in label and "connected" not in label


def test_status_says_where_saved_keys_are(monkeypatch, capsys, keychain):
    from ixel_mat import cli
    from ixel_mat.config import secrets
    from ixel_mat.config.setup import PROVIDERS
    for provider in PROVIDERS:  # no provider is asked anything
        monkeypatch.delenv(provider["env_name"], raising=False)
    monkeypatch.setattr(cli.console, "width", 300)
    cli.cmd_status()
    out = " ".join(capsys.readouterr().out.split())
    assert "Saved keys" in out and str(secrets.get_keys_file_path()) in out
    assert "Keys saved in Ixel are encrypted, and the key that opens them is kept in your Mac's Keychain." in out
    keychain.restart(present=False)
    cli.cmd_status()
    out = " ".join(capsys.readouterr().out.split())
    assert str(secrets.get_env_file_path()) in out and "because this computer has no keychain Ixel can use" in out
