"""
The subscription CLI presets, run for real: each installed CLI is pointed at a
fake model API that answers the question with tool calls (run a shell command,
write a file, read Ixel's key file). Nothing may run, no secret (or anything from
the folder Ixel runs in) may reach the model, and the answer must still come back.

Each CLI is skipped when it isn't installed; CI installs the latest releases, so
a vendor changing a flag's meaning shows up here.
"""
import asyncio
import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cli_capture import ANSWER, CaptureServer, has_tool_results as _answered_tools, offered_tools
from ixel_mat.agents.base import AgentConfig
from ixel_mat.agents.oneshot import ARG_LIMIT, OneShotAgent
from ixel_mat.config.setup import CLI_PRESETS
from ixel_mat.presets import PRESET_ABOUT

PRESETS = {p["id"]: p for p in CLI_PRESETS}
SECRET = "sk-ixel-secret-must-not-leak"
FOLDER_NOTE = "ixel-note-from-the-folder-you-are-in"
STRAY = "sk-stray-key-must-be-dropped"


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _trap(tmp_path):
    """An MCP server entry that leaves a file behind if the CLI ever starts it."""
    marker = tmp_path / "PWNED_MCP"
    return marker, ["-c", f"touch '{marker}'; sleep 5"]


def _claude(home, tmp_path, url, monkeypatch):
    # Key and endpoint come from Claude Code's own settings, so the preset's
    # dropped ANTHROPIC_API_KEY below can't be what makes it work.
    _write(home / ".claude" / "settings.json", json.dumps(
        {"apiKeyHelper": "echo sk-fake-test", "env": {"ANTHROPIC_BASE_URL": url}}))
    marker, args = _trap(tmp_path)
    _write(home / ".claude.json", json.dumps({"mcpServers": {"trap": {"command": "sh", "args": args}}}))
    monkeypatch.setenv("ANTHROPIC_API_KEY", STRAY)
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    calls = [{"name": "Bash", "args": {"command": f"touch '{tmp_path / 'PWNED_SHELL'}'"}},
             {"name": "Write", "args": {"file_path": str(tmp_path / "PWNED_WRITE"), "content": "x"}},
             {"name": "Read", "args": {"file_path": str(home / ".config" / "ixel-mat" / ".env")}}]
    return calls, [], marker


def _codex(home, tmp_path, url, monkeypatch):
    marker, args = _trap(tmp_path)
    _write(home / ".codex" / "config.toml",
           f'[mcp_servers.trap]\ncommand = "sh"\nargs = {json.dumps(args)}\n')
    monkeypatch.setenv("OPENAI_API_KEY", STRAY)
    monkeypatch.setenv("IXEL_FAKE_KEY", "sk-fake-test")
    calls = [{"name": "exec_command", "args": {"cmd": f"touch '{tmp_path / 'PWNED_SHELL'}'"}},
             {"name": "shell", "args": {"command": ["sh", "-c", f"touch '{tmp_path / 'PWNED_WRITE'}'"]}},
             {"name": "view_image", "args": {"path": str(home / ".config" / "ixel-mat" / ".env")}}]
    # --ignore-user-config (part of the preset) hides config.toml, so the fake
    # provider is selected with command-line overrides instead.
    extra = ["-c", 'model_provider="ixelfake"', "-c", 'model="gpt-5"', "-c",
             f'model_providers.ixelfake={{name="fake",base_url="{url}/v1",env_key="IXEL_FAKE_KEY",'
             'wire_api="responses"}']
    return calls, extra, marker


def _gemini(home, tmp_path, url, monkeypatch):
    _write(home / ".gemini" / ".env",
           f"GEMINI_API_KEY=sk-fake-test\nGOOGLE_GEMINI_BASE_URL={url}\nGEMINI_MODEL=gemini-2.5-flash\n")
    marker, args = _trap(tmp_path)
    _write(home / ".gemini" / "settings.json", json.dumps({
        "security": {"auth": {"selectedType": "gemini-api-key"}},
        "mcpServers": {"trap": {"command": "sh", "args": args}}}))
    monkeypatch.setenv("GEMINI_API_KEY", STRAY)
    calls = [{"name": "run_shell_command", "args": {"command": f"touch '{tmp_path / 'PWNED_SHELL'}'"}},
             {"name": "write_file", "args": {"file_path": str(tmp_path / "PWNED_WRITE"), "content": "x"}},
             {"name": "read_file", "args": {"file_path": str(home / ".config" / "ixel-mat" / ".env")}}]
    return calls, [], marker


def _copilot(home, tmp_path, url, monkeypatch):
    for name, value in {"COPILOT_PROVIDER_BASE_URL": f"{url}/v1", "COPILOT_PROVIDER_API_KEY": "sk-fake-test",
                        "COPILOT_MODEL": "gpt-5-mini", "COPILOT_OFFLINE": "true",
                        "COPILOT_ALLOW_ALL": "true"}.items():  # the last one must be dropped by the preset
        monkeypatch.setenv(name, value)
    calls = [{"name": "bash", "args": {"command": f"touch '{tmp_path / 'PWNED_SHELL'}'", "description": "x"}},
             {"name": "create", "args": {"path": str(tmp_path / "PWNED_WRITE"), "file_text": "x"}},
             {"name": "view", "args": {"path": str(home / ".config" / "ixel-mat" / ".env")}}]
    return calls, [], None  # Copilot starts the user's own MCP servers; their tools stay hidden


def _opencode(home, tmp_path, url, monkeypatch):
    # A user config that turns every tool back on, including for an agent named "ixel"
    _write(home / ".config" / "opencode" / "opencode.json", json.dumps({
        "model": "fake/m",
        "provider": {"fake": {"npm": "@ai-sdk/openai-compatible", "name": "Fake",
                              "options": {"baseURL": f"{url}/v1", "apiKey": "sk-fake-test"},
                              "models": {"m": {"name": "m", "tool_call": True}}}},
        "tools": {"*": True}, "permission": {"*": "allow"},
        "agent": {"build": {"tools": {"bash": True}}, "ixel": {"tools": {"bash": True, "write": True},
                                                               "permission": {"*": "allow"}}}}))
    calls = [{"name": "bash", "args": {"command": f"touch '{tmp_path / 'PWNED_SHELL'}'", "description": "x"}},
             {"name": "write", "args": {"filePath": str(tmp_path / "PWNED_WRITE"), "content": "x"}},
             {"name": "read", "args": {"filePath": str(home / ".config" / "ixel-mat" / ".env")}}]
    return calls, [], None


SETUPS = {"claude_code": _claude, "codex": _codex, "gemini_cli": _gemini, "copilot": _copilot,
          "opencode": _opencode}
# Harmless tools a CLI may still offer: Gemini's plan mode keeps read-only tools
# confined to the (empty) temp folder; Codex keeps a no-op "ask the user" tool.
ALLOWED_TOOLS = {"codex": {"request_user_input"},
                 "gemini_cli": {"list_directory", "read_file", "grep_search", "glob", "google_web_search",
                                "write_file", "replace", "exit_plan_mode", "update_topic", "invoke_agent",
                                "save_memory", "web_fetch", "ask_user", "enter_plan_mode", "activate_skill",
                                "codebase_investigator", "cli_help", "write_todos", "read_many_files",
                                "get_internal_docs"}}


KEEP_ENV = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "IXEL_REQUIRE_CLIS"}


def test_every_preset_has_a_live_check():
    assert set(SETUPS) == set(PRESETS)


# A preset that reads stdin is also asked a question too long for a command line (code to review makes
# one), which must reach the model whole
CASES = [(p, False) for p in sorted(SETUPS)] + [(p, True) for p in sorted(SETUPS)
                                                 if PRESETS[p]["prompt_via"] in ("auto", "stdin")]
PADDING = "(padding, to make the question too long for a command line) "
LONG = "\n\n" + PADDING * (ARG_LIMIT // len(PADDING) + 1)


@pytest.mark.skipif(os.name == "nt", reason="the traps use sh")
@pytest.mark.parametrize("preset_id, long_prompt", CASES, ids=[p + ("-long" if long else "") for p, long in CASES])
def test_preset_is_answer_only(preset_id, long_prompt, tmp_path, monkeypatch):
    preset = PRESETS[preset_id]
    if not shutil.which(preset["command"]):
        if os.environ.get("IXEL_REQUIRE_CLIS") == "1":  # set in CI, where they're all installed
            pytest.fail(f"{preset['command']} is not installed")
        pytest.skip(f"{preset['command']} is not installed")

    home = tmp_path / "home"
    _write(home / ".config" / "ixel-mat" / ".env", f"IXEL_TEST_SECRET={SECRET}\n")
    # Start from a near-empty environment: whatever credentials or endpoints the
    # machine running the tests has must not reach (or be billed by) a real service.
    for name in list(os.environ):
        if name not in KEEP_ENV:
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    # Ixel started in one of your repositories: its notes for agents are no business of the panel
    yours = tmp_path / "your-repo"
    _write(yours / "AGENTS.md", f"{FOLDER_NOTE}\n")
    monkeypatch.chdir(yours)
    monkeypatch.setenv("PWD", str(yours))

    with CaptureServer() as fake:
        calls, extra_args, mcp_marker = SETUPS[preset_id](home, tmp_path, fake.url, monkeypatch)
        fake.tool_calls = calls
        fields = {k: v for k, v in preset.items() if k not in PRESET_ABOUT}
        fields["args"] = list(preset["args"]) + extra_args
        agent = OneShotAgent(AgentConfig(name=preset_id, type="oneshot", workdir="temp", **fields))

        async def ask():
            await agent.connect()
            try:
                return await agent.send_and_receive("What is 17 x 23?" + (LONG if long_prompt else ""))
            finally:
                await agent.disconnect()

        answer = asyncio.run(ask())

    assert ANSWER in answer
    assert fake.requests, "the CLI never called the fake model"
    if long_prompt:
        assert any(PADDING * 100 in r["raw"] for r in fake.requests), "the question on stdin never reached the model"
    assert any(r["body"] and _answered_tools(r["body"]) for r in fake.requests), "the tool calls never came back"
    assert not (tmp_path / "PWNED_SHELL").exists(), "a shell command ran"
    assert not (tmp_path / "PWNED_WRITE").exists(), "a file was written"
    if mcp_marker is not None:
        assert not mcp_marker.exists(), "a user-configured MCP server was started"
    for request in fake.requests:
        assert SECRET not in request["raw"], "Ixel's key file reached the model"
        assert FOLDER_NOTE not in request["raw"], "the folder Ixel runs from reached the model"
        seen = request["raw"] + request["headers"] + request["path"]
        assert STRAY not in seen, "an API key from the environment was used instead of the CLI's login"
        unexpected = set(offered_tools(request["body"])) - ALLOWED_TOOLS.get(preset_id, set())
        assert not unexpected, f"tools offered to the model: {sorted(unexpected)}"


class _Catalog:
    """A stand-in for OpenCode's model catalog (models.opencode.ai) that writes down every request for it."""

    def __init__(self):
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.mark.skipif(os.name == "nt", reason="like the others here")
def test_opencode_never_asks_for_its_model_catalog(tmp_path, monkeypatch):
    # Settings lists the models OpenCode knows and a question gets its answer, and neither fetches OpenCode's
    # catalog. The catalog is a stand-in here, and the same listing without Ixel's setting must ask it: otherwise
    # this would pass on an OpenCode that fetches some other way. OpenCode 2's private server must be gone after.
    from ixel_mat import presets
    from ixel_mat.gui import model_choices
    if not shutil.which("opencode"):
        if os.environ.get("IXEL_REQUIRE_CLIS") == "1":
            pytest.fail("opencode is not installed")
        pytest.skip("opencode is not installed")
    home = tmp_path / "home"
    for name in list(os.environ):
        if name not in KEEP_ENV:
            monkeypatch.delenv(name)
    for name, value in {"HOME": str(home), "USERPROFILE": str(home), "NO_PROXY": "127.0.0.1,localhost",
                        "no_proxy": "127.0.0.1,localhost"}.items():
        monkeypatch.setenv(name, value)

    with CaptureServer() as fake, _Catalog() as catalog:
        monkeypatch.setenv("OPENCODE_MODELS_URL", catalog.url)
        _opencode(home, tmp_path, fake.url, monkeypatch)
        preset = PRESETS["opencode"]
        cfg = AgentConfig(name="opencode", type="oneshot", workdir="temp",
                          **{k: v for k, v in preset.items() if k not in PRESET_ABOUT})
        cfg.env = {**cfg.env, "OPENCODE_DISABLE_MODELS_FETCH": "0"}  # an agent's own env can't turn it back on
        listing = model_choices.agent_choices(cfg, {"preset": "opencode"})
        agent = OneShotAgent(cfg)

        async def ask():
            await agent.connect()
            try:
                return await agent.send_and_receive("What is 17 x 23?")
            finally:
                await agent.disconnect()

        answer = asyncio.run(ask())
        asked = list(catalog.seen)
        # Without Ixel's setting, the same listing does ask
        monkeypatch.setattr(presets, "LOCKED_ENV", {})
        cfg.env = {k: v for k, v in cfg.env.items() if k != "OPENCODE_DISABLE_MODELS_FETCH"}
        model_choices.agent_choices(cfg, {"preset": "opencode"})
        detector = list(catalog.seen)

    assert "fake/m" in listing["models"], listing["note"]
    assert ANSWER in answer
    assert asked == [], f"OpenCode asked for its catalog: {asked}"
    assert detector, "OpenCode didn't ask the stand-in catalog even without Ixel's setting: this test can't see it"
    assert not list(home.rglob("service.json")), "OpenCode 2's background service was started"
