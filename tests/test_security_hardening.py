"""Regression tests for security fixes."""
from ixel_mat.config import loader


def test_project_local_config_is_not_loaded(monkeypatch, tmp_path):
    # A cloned repo shipping a malicious .ixel-mat.toml must not define agents.
    (tmp_path / ".ixel-mat.toml").write_text(
        '[agents.evil]\ntype = "subprocess"\ncommand = "sh"\nlabel = "Evil"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(loader, "_GLOBAL_CONFIG", tmp_path / "missing" / "config.toml")

    assert loader.find_config() is None
    config = loader.load_config()
    assert config["agents"] == {}  # no config yet: no agents, not stand-ins
    assert config["_source"] == "none"


def test_global_config_is_still_loaded(monkeypatch, tmp_path):
    global_config = tmp_path / "config.toml"
    global_config.write_text(
        '[agents.gpt]\ntype = "http"\nurl = "https://api.openai.com/v1/chat/completions"\nlabel = "GPT"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(loader, "_GLOBAL_CONFIG", global_config)

    config = loader.load_config()
    assert set(config["agents"]) == {"gpt"}
    assert config["_source"] == str(global_config)


# ── Cleartext transports ──────────────────────────────────────────────────────

import asyncio

import pytest

from ixel_mat.agents.base import AgentConfig, is_loopback_host, needs_api_key
from ixel_mat.agents.http import HttpAgent


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.1", "127.0.0.2", "::1"])
def test_is_loopback_host_accepts_this_machine(host):
    assert is_loopback_host(host)


@pytest.mark.parametrize("host", [None, "", "example.com", "10.0.0.5", "localhost.evil.com", "0.0.0.0"])
def test_is_loopback_host_rejects_everything_else(host):
    assert not is_loopback_host(host)


def _http_agent(url):
    return HttpAgent(AgentConfig(name="a", label="A", type="http", url=url, token="sk-secret", model="m"))


def test_http_agent_refuses_cleartext_to_remote_host():
    agent = _http_agent("http://attacker.example/v1/chat/completions")
    with pytest.raises(ValueError, match="plain http://"):
        asyncio.run(agent.connect())
    assert not agent.is_connected


@pytest.mark.parametrize("url", [
    "https://api.openai.com/v1/chat/completions",
    "http://127.0.0.1:11434/v1/chat/completions",  # local model servers stay allowed
    "http://localhost:8080/v1/chat/completions",
])
def test_http_agent_allows_https_and_local_http(url):
    agent = _http_agent(url)

    async def go():
        await agent.connect()
        connected = agent.is_connected
        await agent.disconnect()
        return connected

    assert asyncio.run(go())


def test_validate_config_flags_cleartext_remote_urls():
    issues = loader.validate_config({"agents": {
        "gpt": {"type": "http", "label": "G", "url": "http://api.example.com/v1", "token": "k", "model": "m"},
        "gw": {"type": "websocket", "label": "W", "url": "ws://gateway.example.com", "token": "t"},
        "local": {"type": "http", "label": "L", "url": "http://127.0.0.1:1234/v1", "token": "k", "model": "m"},
    }})
    assert any(i.startswith("Agent 'gpt'") and "https://" in i for i in issues)
    assert any(i.startswith("Agent 'gw'") and "wss://" in i for i in issues)
    assert not any(i.startswith("Agent 'local'") for i in issues)


@pytest.mark.parametrize("url", ["http://192.168.1.5:11434/v1/chat/completions", "http://nas.local:1234/v1",
                                 "http://gpu-box:8000/v1", "http://100.101.102.103:11434/v1"])
def test_a_keyless_model_server_on_your_own_network_may_use_plain_http(url):
    cfg = AgentConfig(name="lan", label="LAN", type="http", url=url, model="llama3.3")
    assert not needs_api_key(cfg)
    agent = HttpAgent(cfg)

    async def go():
        await agent.connect()
        connected = agent.is_connected
        await agent.disconnect()
        return connected

    assert asyncio.run(go())
    assert not loader.validate_config({"agents": {"lan": {"type": "http", "label": "L", "url": url,
                                                           "model": "llama3.3"}}})


def test_a_key_never_goes_over_plain_http_even_on_your_own_network():
    agent = _http_agent("http://192.168.1.5:11434/v1/chat/completions")
    with pytest.raises(ValueError, match="won't send its API key over plain http:// to 192.168.1.5"):
        asyncio.run(agent.connect())
    issues = loader.validate_config({"agents": {"lan": {
        "type": "http", "label": "L", "url": "http://192.168.1.5:11434/v1", "token": "k", "model": "m"}}})
    assert any("plain http://" in i and "https://" in i for i in issues)


@pytest.mark.parametrize("url", ["http://example.com/v1", "https://api.openai.com/v1"])
def test_a_server_on_the_internet_still_needs_a_key(url):
    assert needs_api_key(AgentConfig(name="a", label="A", type="http", url=url))


def test_checking_a_model_never_sends_its_key_over_plain_http(monkeypatch):
    """`ixel agents`, `ixel status`, `ixel doctor --check`, Health's Check now and `ixel model` ask a server for
    its models: the same rule as a real call, so a key never goes to another computer over plain http."""
    from ixel_mat import cli
    from ixel_mat.config import setup

    sent = []
    monkeypatch.setattr(setup, "_get_json", lambda request, timeout=8.0: sent.append(request) or {"data": []})
    cfg = AgentConfig(name="gpt", label="GPT", type="http", url="http://192.0.2.2:35051/v1/chat/completions",
                      token="sk-ant-AUDITCANARY123", model="m")
    status, detail, _ = cli._probe_other_http(cfg)
    assert status == "unreachable" and "plain http://" in detail
    assert cli._live_models(cfg) == ([], None)
    assert sent == []
    # Without a key, a server on your own network is asked as before
    cfg = AgentConfig(name="lan", label="LAN", type="http", url="http://192.168.1.5:11434/v1/chat/completions",
                      model="m")
    cli._probe_other_http(cfg)
    assert len(sent) == 1 and not sent[0].has_header("Authorization")


def test_a_plain_http_check_never_goes_through_a_proxy(tmp_path):
    """urllib sends even 127.0.0.1 through http_proxy unless no_proxy lists it: a key allowed to a server on
    this computer would reach the proxy unencrypted."""
    import subprocess
    import sys
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    seen = []

    class Models(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            body = b'{"data": [{"id": "local-model"}]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Models)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        code = ("import urllib.request\nfrom ixel_mat.config.setup import _get_json\n"
                f"r = urllib.request.Request('http://127.0.0.1:{server.server_port}/v1/models', "
                "headers={'Authorization': 'Bearer sk-local'})\nprint(_get_json(r)['data'][0]['id'])")
        env = {**os.environ, "http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
               "no_proxy": "", "NO_PROXY": ""}
        proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
    finally:
        server.shutdown()
    assert proc.stdout.strip() == "local-model", proc.stderr
    assert seen == ["Bearer sk-local"]


def test_validate_config_reports_malformed_url_instead_of_crashing():
    issues = loader.validate_config({"agents": {
        "bad": {"type": "http", "label": "B", "url": "http://[oops/v1", "token": "k", "model": "m"},
    }})
    assert any(i.startswith("Agent 'bad'") and "https://" in i for i in issues)


# ── Secret file permissions ───────────────────────────────────────────────────

import os
import stat

from ixel_mat.config import secrets


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_save_secret_is_never_world_readable(monkeypatch, tmp_path):
    env_dir = tmp_path / "ixel-mat"
    monkeypatch.setattr(secrets, "_ENV_DIR", env_dir)
    monkeypatch.setattr(secrets, "_ENV_FILE", env_dir / ".env")
    old_umask = os.umask(0o022)

    modes_before_rename = []
    real_replace = os.replace

    def spy_replace(src, dst):
        modes_before_rename.append(_mode(src))
        return real_replace(src, dst)

    monkeypatch.setattr(secrets.os, "replace", spy_replace)
    try:
        secrets.save_secret("OPENAI_API_KEY", "sk-one")
        secrets.save_secret("XAI_API_KEY", "xai-two")
    finally:
        os.umask(old_umask)

    assert modes_before_rename == [0o600, 0o600]
    assert _mode(env_dir / ".env") == 0o600
    assert _mode(env_dir) == 0o700
    assert sorted(p.name for p in env_dir.iterdir()) == [".env"]  # no temp files left
    text = (env_dir / ".env").read_text()
    assert 'OPENAI_API_KEY="sk-one"' in text and 'XAI_API_KEY="xai-two"' in text


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_load_env_tightens_loose_permissions(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("SOME_KEY=value\n")
    env_path.chmod(0o644)
    monkeypatch.setattr(secrets, "_ENV_FILE", env_path)
    monkeypatch.delenv("SOME_KEY", raising=False)

    assert secrets.load_env() == {"SOME_KEY": "value"}
    assert _mode(env_path) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_gateway_device_key_is_created_private(monkeypatch, tmp_path):
    from ixel_mat.agents import websocket

    key_file = tmp_path / "ixel-mat" / "device_key"
    monkeypatch.setattr(websocket, "_KEY_FILE", key_file)

    _, device_id, _ = websocket._load_or_gen_key()
    assert _mode(key_file) == 0o600
    assert websocket._load_or_gen_key()[1] == device_id  # same key reloads


# ── Setup wizard ──────────────────────────────────────────────────────────────

from ixel_mat.config import setup as setup_wizard
from ixel_mat.config.loader import tomllib


def test_build_toml_escapes_labels_and_gateway_session_keys():
    hostile_session_key = 'agent:x"\n[agents.pwn]\ntype = "subprocess"\ncommand = "sh'
    toml_text = setup_wizard._build_toml([
        {"id": "main", "type": "websocket", "url": "ws://127.0.0.1:18789",
         "token_env": "IXELMAT_GATEWAY_TOKEN", "session_key": hostile_session_key,
         "label": 'My "fast" agent \\ [beta]', "color": "cyan"},
        {"id": "odd.id", "type": "http", "url": "https://api.x.ai/v1/chat/completions",
         "token_env": "XAI_API_KEY", "model": "grok-4", "label": "Grök", "color": "magenta"},
    ])

    parsed = tomllib.loads(toml_text)["agents"]
    assert set(parsed) == {"main", "odd.id"}  # no injected [agents.pwn]
    assert parsed["main"]["session_key"] == hostile_session_key
    assert parsed["main"]["label"] == 'My "fast" agent \\ [beta]'
    assert "command" not in parsed["main"]
    assert parsed["odd.id"]["label"] == "Grök"


def test_google_probe_sends_key_in_header_not_url(monkeypatch):
    seen = []

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    def fake_urlopen(req, timeout=None):
        seen.append(req)
        return _Resp()

    monkeypatch.setattr(setup_wizard, "_urlopen", fake_urlopen)
    ok, _ = setup_wizard._probe_google("AIza-secret")

    assert ok
    assert "AIza-secret" not in seen[0].full_url
    assert dict((k.lower(), v) for k, v in seen[0].header_items())["x-goog-api-key"] == "AIza-secret"


def test_setup_hides_api_key_input(monkeypatch):
    prompts = []

    def fake_ask(label, **kwargs):
        prompts.append(kwargs)
        return "sk-typed"

    monkeypatch.setattr(setup_wizard.Prompt, "ask", fake_ask)
    monkeypatch.setattr(setup_wizard.Confirm, "ask", lambda *a, **k: True)
    monkeypatch.setattr(setup_wizard, "_validate_key", lambda provider, key: (True, "key valid"))
    monkeypatch.setattr(setup_wizard, "save_secret", lambda key, value: None)
    # setenv first so teardown removes the key _setup_provider writes to os.environ
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder")
    monkeypatch.delenv("OPENAI_API_KEY")
    openai = next(p for p in setup_wizard.PROVIDERS if p["id"] == "openai")

    status = {"openai": {"configured": False, "key": "", "masked": ""}}
    assert setup_wizard._setup_provider(openai, status) == "sk-typed"
    assert prompts and all(kw.get("password") is True for kw in prompts)

    status = {"openai": {"configured": True, "key": "sk-old", "masked": "sk-old..."}}
    monkeypatch.setattr(setup_wizard.Confirm, "ask", lambda *a, **k: False)  # replace the key
    prompts.clear()
    assert setup_wizard._setup_provider(openai, status) == "sk-typed"
    assert prompts and all(kw.get("password") is True for kw in prompts)


# ── Untrusted text in the terminal ────────────────────────────────────────────

import io
import json

from rich.console import Console

from ixel_mat import mat
from ixel_mat.sanitize import safe_markup, sanitize_terminal_text

OSC_TITLE = "\x1b]0;PWNED\x07"
OSC_CLIPBOARD = "\x1b]52;c;ZWNobyBoaQ==\x07"
HOSTILE = f"[/INST] Sure!{OSC_TITLE}{OSC_CLIPBOARD} \x1b[31mred\x1b[0m [bold]x"


def _capture_console(monkeypatch, module):
    buf = io.StringIO()
    monkeypatch.setattr(module, "console", Console(file=buf, force_terminal=True, width=120))
    return buf


def _plain(output):
    """Drop Rich's own color codes so literal text can be matched."""
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", output)


def _assert_no_injected_sequences(output):
    assert "\x1b]52" not in output  # clipboard write
    assert "\x1b]0;" not in output  # window title


def test_sanitize_strips_control_sequences_but_keeps_text():
    raw = f"line one{OSC_CLIPBOARD}\r\n\ttab \x1b[1mbold\x1b[0m \x9b31m c1 ‮evil\nline two"
    assert sanitize_terminal_text(raw) == "line one\n\ttab bold 31m c1 evil\nline two"
    assert sanitize_terminal_text("Grök → ✓") == "Grök → ✓"
    assert sanitize_terminal_text(None) == ""


def test_unterminated_osc_does_not_swallow_the_rest_of_the_answer():
    assert sanitize_terminal_text("\x1b]0;half-open\nThe real answer") == "\nThe real answer"


def test_safe_markup_renders_model_brackets_literally():
    buf = io.StringIO()
    Console(file=buf, width=120).print(f"[dim]{safe_markup(HOSTILE)}[/]")
    assert "[/INST] Sure!" in buf.getvalue()
    assert "[bold]x" in buf.getvalue()


class _FakeAgent:
    def __init__(self, name, reply):
        self.name = self.label = name
        self.reply = reply
        self.is_connected = True

    async def send_and_receive(self, message, **kwargs):
        return self.reply

    async def disconnect(self):
        self.is_connected = False


def test_full_mode_panels_strip_hostile_sequences():
    renderable = mat._build_full_renderable(f"prompt{OSC_TITLE}", {
        "a": {"label": "Agent [/beta]", "status": "done", "response": {
            "answer": HOSTILE, "latency_ms": 5, "degraded": False,
            "evidence": [OSC_CLIPBOARD + "RFC 1"], "followup": OSC_TITLE + "next",
        }},
    })
    buf = io.StringIO()
    Console(file=buf, force_terminal=True, width=120).print(renderable)
    _assert_no_injected_sequences(buf.getvalue())
    assert "Agent [/beta]" in _plain(buf.getvalue())


def test_ixel_agents_survives_hostile_error_text(monkeypatch):
    from ixel_mat import cli
    import ixel_mat.config.loader

    buf = _capture_console(monkeypatch, cli)
    monkeypatch.setattr(ixel_mat.config.loader, "load_config", lambda: {"agents": {}})
    monkeypatch.setattr(ixel_mat.config.loader, "build_agent_configs", lambda cfg: (
        {"gw": AgentConfig(name="gw", label="Gateway [/x]", type="websocket", url="ws://127.0.0.1:1")},
        [f"warn [/y]{OSC_TITLE}"],
    ))

    async def fake_probe(cfg):
        return "unreachable", f"Rejected: [/INST] {OSC_CLIPBOARD}", None, None

    monkeypatch.setattr(cli, "_probe_agent_connection", fake_probe)
    cli.cmd_agents()

    output = buf.getvalue()
    assert "Rejected: [/INST]" in _plain(output)
    _assert_no_injected_sequences(output)


def test_repl_keeps_running_after_a_command_fails(monkeypatch):
    buf = _capture_console(monkeypatch, mat)
    agent = _FakeAgent("a", "")
    inputs = iter(["first prompt", "second prompt"])
    sent = []

    async def fake_connect():
        return {"a": agent}

    async def fake_read(*args, **kwargs):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    async def fake_ask(text, agents):
        sent.append(text)
        if len(sent) == 1:
            raise RuntimeError("provider exploded [/oops]")

    monkeypatch.setattr(mat, "print_splash", lambda: None)
    monkeypatch.setattr(mat, "connect_agents", fake_connect)
    monkeypatch.setattr(mat, "read_burst_submission", fake_read)
    monkeypatch.setattr(mat, "run_plain_question", fake_ask)

    asyncio.run(mat.main())

    assert sent == ["first prompt", "second prompt"]
    assert "Command failed" in _plain(buf.getvalue())
    assert "provider exploded [/oops]" in _plain(buf.getvalue())
    assert not agent.is_connected  # still disconnected cleanly on exit


# ── Parser robustness ─────────────────────────────────────────────────────────

from ixel_mat.modes.full import FullModeDispatcher
from ixel_mat.schema.response import Confidence, parse_structured_response

GOOD = json.dumps({"answer": "Paris", "confidence": "high", "evidence": [], "next_step": "none"})
NULL_NEXT_STEP = json.dumps({"answer": "Paris", "confidence": "high", "evidence": [], "next_step": None})


def test_null_fields_do_not_degrade_a_good_answer():
    parsed = parse_structured_response("a", json.dumps({
        "answer": "Paris", "confidence": None, "evidence": None,
        "uncertainties": [None, "", {"source": "atlas"}], "commands": None, "next_step": None,
    }))
    assert not parsed.degraded
    assert parsed.answer == "Paris"
    assert parsed.confidence == Confidence.UNCERTAIN
    assert parsed.evidence == []
    assert parsed.uncertainties == ['{"source": "atlas"}']
    assert parsed.followup == ""


def test_non_string_answers_are_coerced():
    assert parse_structured_response("a", '{"answer": 42, "confidence": "high"}').answer == "42"
    listed = parse_structured_response("a", '{"answer": ["step one", "step two"]}')
    assert listed.answer == "step one\nstep two"
    assert parse_structured_response("a", '{"answer": "x", "evidence": "RFC 2328"}').evidence == ["RFC 2328"]


def test_null_answer_is_degraded_not_an_empty_valid_answer():
    raw = '{"answer": null, "confidence": "high"}'
    parsed = parse_structured_response("a", raw)
    assert parsed.degraded
    assert parsed.answer == raw


def test_non_string_raw_reply_does_not_crash():
    assert parse_structured_response("a", None).degraded
    assert parse_structured_response("a", 503).answer == "503"


def test_full_mode_keeps_answer_with_null_next_step():
    result = asyncio.run(FullModeDispatcher([_FakeAgent("a", NULL_NEXT_STEP)]).dispatch("q"))
    assert not result.responses[0].degraded
    assert result.responses[0].answer == "Paris"


# ── One-shot agent ────────────────────────────────────────────────────────────

import sys
import time
from contextlib import suppress

from ixel_mat.agents.oneshot import OneShotAgent


def _oneshot(script, timeout=30.0):
    return OneShotAgent(
        AgentConfig(name="x", label="x", type="oneshot", command=sys.executable, args=["-c", script]),
        timeout=timeout,
    )


def _ask(agent, message="q"):
    async def go():
        await agent.connect()
        return await agent.send_and_receive(message)
    return asyncio.run(go())


def test_oneshot_keeps_every_answer_line_and_session_id():
    agent = _oneshot(
        "print('The answer is:'); print('42'); "
        "print('Set session = prod in the config'); print('Wait 10s then retry')"
    )
    assert _ask(agent) == "The answer is:\n42\nSet session = prod in the config\nWait 10s then retry"
    assert agent.config.last_session_id == ""


def test_oneshot_reads_session_id_from_trailing_footer():
    agent = _oneshot(
        "print('Answer line'); print(); print('session_id: 20260408_abc'); "
        "print('Duration: 4s'); print('Messages: 2')"
    )
    assert _ask(agent) == "Answer line"
    assert agent.config.last_session_id == "20260408_abc"


def test_oneshot_reads_session_id_from_stderr():
    agent = _oneshot("import sys; print('Answer'); print('Session: s-42', file=sys.stderr); print('bye', file=sys.stderr)")
    assert _ask(agent) == "Answer"
    assert agent.config.last_session_id == "s-42"


def test_oneshot_applies_carriage_returns_like_a_terminal():
    agent = _oneshot("import sys; sys.stdout.write('1s\\r2s\\rFinal answer\\n')")
    assert _ask(agent) == "Final answer"


def _alive(pid):
    if os.name == "nt":  # os.kill() would terminate it there, not check it
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # an unreaped zombie is not running
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[-1].split()[0] != "Z"
    except OSError:
        return True


def _wait_dead(pid, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


# Spawns a grandchild, records both pids, then hangs.
_HANG = (
    "import os, subprocess, sys, time; "
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
    "open(sys.argv[1], 'w').write(f'{os.getpid()} {child.pid}'); "
    "time.sleep(60)"
)


def test_oneshot_timeout_kills_command_and_its_children(tmp_path):
    pid_file = tmp_path / "pids"
    agent = OneShotAgent(
        AgentConfig(name="x", label="x", type="oneshot", command=sys.executable, args=["-c", _HANG, str(pid_file)]),
        timeout=1.5,
    )
    with pytest.raises(TimeoutError, match="timed out after 1.5s"):
        _ask(agent)
    pids = [int(p) for p in pid_file.read_text().split()]
    assert all(_wait_dead(pid) for pid in pids), "orphaned process left running"


def test_oneshot_cancellation_kills_command(tmp_path):
    # a review cancels an agent it stops waiting for (see slowest_wait)
    pid_file = tmp_path / "pids"
    agent = OneShotAgent(
        AgentConfig(name="x", label="x", type="oneshot", command=sys.executable, args=["-c", _HANG, str(pid_file)]),
        timeout=30,
    )

    async def go():
        await agent.connect()
        task = asyncio.create_task(agent.send_and_receive("q"))
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    pids = [int(p) for p in pid_file.read_text().split()]
    assert all(_wait_dead(pid) for pid in pids), "orphaned process left running"


# Starts a helper that keeps the output pipe open, then exits at once
_ORPHAN = (
    "import subprocess, sys; "
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
    "open(sys.argv[1], 'w').write(str(child.pid))"
)


def test_oneshot_kills_helpers_that_outlive_the_command(tmp_path):
    # The command exits, but a helper it started still holds stdout, so the call runs until
    # the timeout. The command was no longer running then, so nothing used to be killed.
    pid_file = tmp_path / "pids"
    agent = OneShotAgent(
        AgentConfig(name="x", label="x", type="oneshot", command=sys.executable,
                    args=["-c", _ORPHAN, str(pid_file)]),
        timeout=2,
    )
    with suppress(TimeoutError):  # Windows may see end-of-output as soon as the command exits
        _ask(agent)
    assert _wait_dead(int(pid_file.read_text())), "orphaned process left running"


# Answers and exits, leaving a helper running that doesn't hold the output pipe
_LEAVES_A_HELPER = (
    "import subprocess, sys; "
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdout=subprocess.DEVNULL, "
    "stderr=subprocess.DEVNULL); "
    "open(sys.argv[1], 'w').write(str(child.pid)); print('42')"
)


def test_oneshot_ends_helpers_left_running_after_a_clean_answer(tmp_path):
    pid_file = tmp_path / "pids"
    agent = OneShotAgent(
        AgentConfig(name="x", label="x", type="oneshot", command=sys.executable,
                    args=["-c", _LEAVES_A_HELPER, str(pid_file)]),
        timeout=20,
    )
    assert _ask(agent) == "42"
    assert _wait_dead(int(pid_file.read_text())), "a helper was left running after the answer"


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended process startup")
def test_cancellation_during_windows_spawn_does_not_leave_a_suspended_process(monkeypatch):
    from ixel_mat.agents import process_tree

    real_spawn = asyncio.create_subprocess_exec
    created = asyncio.Event()
    spawned = []

    async def delayed_spawn(*args, **kwargs):
        proc = await real_spawn(*args, **kwargs)
        spawned.append(proc)
        created.set()
        await asyncio.sleep(0.1)  # cancel after CreateProcess, before the transport is returned
        return proc

    monkeypatch.setattr(process_tree.asyncio, "create_subprocess_exec", delayed_spawn)

    async def go():
        task = asyncio.create_task(process_tree.create_process_tree(
            sys.executable, "-c", "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **process_tree.SPAWN_OPTIONS,
        ))
        await created.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(go())
    assert spawned and _wait_dead(spawned[0].pid)


# ── Windows (no pty / termios) ────────────────────────────────────────────────

import subprocess
from pathlib import Path

from ixel_mat.agents.subprocess import SubprocessAgent

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_app_imports_without_pty(tmp_path):
    # Simulates Windows, where termios (and therefore pty) does not exist.
    code = (
        "import sys; sys.modules['termios'] = None\n"
        "from ixel_mat import mat, cli, agents\n"
        "from ixel_mat.agents.base import AgentConfig\n"
        "agent = agents.create_agent(AgentConfig(name='x', label='x', type='subprocess', command='echo'))\n"
        "print('use_pty', agent.use_pty)\n"
    )
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "use_pty False" in proc.stdout


def test_subprocess_agent_pipe_mode_round_trip():
    echo = "import sys\nfor line in sys.stdin:\n    print('echo:' + line.strip(), flush=True)\n"
    agent = SubprocessAgent(
        AgentConfig(name="x", label="x", type="subprocess", command=sys.executable, args=["-u", "-c", echo]),
        use_pty=False, response_idle_timeout=0.5,
    )

    async def go():
        await agent.connect()
        try:
            return await agent.send_and_receive("hello")
        finally:
            await agent.disconnect()

    assert asyncio.run(go()) == "echo:hello"


@pytest.mark.skipif(os.name != "nt", reason="Windows process tree cancellation")
def test_cancelling_subprocess_connect_cleans_up_its_started_tree(tmp_path):
    pid_file = tmp_path / "pid"
    code = "import os,sys,time;open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(60)"
    agent = SubprocessAgent(
        AgentConfig(name="x", label="x", type="subprocess", command=sys.executable,
                    args=["-u", "-c", code, str(pid_file)]),
        use_pty=False,
    )

    async def go():
        task = asyncio.create_task(agent.connect())
        for _ in range(100):
            if agent._tree is not None and pid_file.exists():
                break
            await asyncio.sleep(0.01)
        assert agent._tree is not None and pid_file.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert _wait_dead(int(pid_file.read_text()))


@pytest.mark.parametrize("use_pty", [False, pytest.param(True, marks=pytest.mark.skipif(
    os.name == "nt", reason="no PTY on Windows"))], ids=["pipes", "pty"])
def test_subprocess_agent_waits_for_a_slow_first_reply(use_pty):
    # Thinks for longer than the idle timeout before answering. That reply used to come back
    # empty, and then turn up as the answer to the next prompt.
    slow = ("import sys, time\n"
            "for n, line in enumerate(sys.stdin):\n"
            "    time.sleep(1.5 if n == 0 else 0.1)\n"
            "    print('reply to ' + line.strip(), flush=True)\n")
    agent = SubprocessAgent(
        AgentConfig(name="x", label="X", type="subprocess", command=sys.executable, args=["-u", "-c", slow],
                    timeout=20),
        use_pty=use_pty, response_idle_timeout=0.4,
    )

    async def go():
        await agent.connect()
        try:
            return [await agent.send_and_receive("first"), await agent.send_and_receive("second")]
        finally:
            await agent.disconnect()

    first, second = asyncio.run(go())
    assert first.endswith("reply to first") and "second" not in first
    assert second.endswith("reply to second") and "reply to first" not in second


def test_subprocess_agent_reply_that_starts_like_the_question():
    # Only a PTY echoes the prompt back, so over pipes "yes" to "yes or no?" is the reply
    script = "import sys\nfor line in sys.stdin:\n    print('yes', flush=True)\n"
    agent = SubprocessAgent(
        AgentConfig(name="x", label="X", type="subprocess", command=sys.executable, args=["-u", "-c", script],
                    timeout=20),
        use_pty=False, response_idle_timeout=0.3,
    )

    async def go():
        await agent.connect()
        try:
            started = time.monotonic()
            return await agent.send_and_receive("yes or no?"), time.monotonic() - started
        finally:
            await agent.disconnect()

    reply, elapsed = asyncio.run(go())
    assert reply == "yes" and elapsed < 5


def test_subprocess_agent_reports_a_silent_process():
    agent = SubprocessAgent(
        AgentConfig(name="x", label="X", type="subprocess", command=sys.executable,
                    args=["-u", "-c", "import time; time.sleep(30)"], timeout=1),
        use_pty=False, response_idle_timeout=0.2,
    )

    async def go():
        await agent.connect()
        try:
            return await agent.send_and_receive("anyone?")
        finally:
            await agent.disconnect()

    with pytest.raises(TimeoutError, match="didn't reply within 1s"):
        asyncio.run(go())


# ── Hostile model output can't stall Ixel (regex backtracking) ────────────────

@pytest.mark.parametrize("parse,text", [
    # Each took 3–40 s at these sizes with the old patterns; linear now
    ("json", "```{" * 50_000),
    ("sections", "**" + " " * 200_000 + "x"),
    ("cli", "\x1b]" * 100_000),
    ("terminal", "\x1b]" * 100_000 + "\x1bP" * 100_000),
], ids=["fenced-json", "sections", "cli-output", "terminal"])
def test_parsers_are_linear_on_hostile_output(parse, text):
    from ixel_mat.agents.oneshot import _visible_text
    from ixel_mat.schema.response import _extract_sections, extract_json_object

    run = {"json": extract_json_object, "sections": _extract_sections,
           "cli": lambda t: _visible_text(t.encode()), "terminal": sanitize_terminal_text}[parse]
    started = time.perf_counter()
    run(text)
    assert time.perf_counter() - started < 3


def test_fenced_json_still_found_after_decoys():
    from ixel_mat.schema.response import extract_json_object
    reply = 'Sure.\n```\nnot json\n```\n```json\n{"best": "B"}\n```\nand {broken'
    assert extract_json_object(reply) == {"best": "B"}


def test_terminal_markdown_shows_where_links_really_go():
    from rich.console import Console

    from ixel_mat.review_ui import _body
    console = Console(force_terminal=True, width=100)
    with console.capture() as captured:
        console.print(_body("Log in at [https://your-bank.com](https://evil.example/phish) **now**"))
    out = captured.get()
    assert "\x1b]8;" not in out  # no clickable link hiding its target
    assert "evil.example/phish" in out


# ── Config files written by Windows' default encoding still load ──────────────

def test_config_in_the_windows_code_page_still_loads(monkeypatch, tmp_path):
    # `ixel setup` on Windows wrote config.toml in cp1252 ("—" is byte 0x97)
    cfg = tmp_path / "config.toml"
    cfg.write_bytes("# Ixel MAT — Agent Configuration\n[agents.gpt]\ntype = \"http\"\n"
                    "url = \"https://api.openai.com/v1/chat/completions\"\nlabel = \"GPT — main\"\n".encode("cp1252"))
    monkeypatch.setattr(loader, "_GLOBAL_CONFIG", cfg)
    config = loader.load_config()
    assert config["agents"]["gpt"]["label"] == "GPT — main" and "_error" not in config


def test_config_with_a_notepad_bom_loads(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_bytes(b"\xef\xbb\xbf" + '[agents.gpt]\ntype = "http"\nlabel = "GPT"\n'.encode("utf-8"))
    monkeypatch.setattr(loader, "_GLOBAL_CONFIG", cfg)
    assert set(loader.load_config()["agents"]) == {"gpt"}


def test_unreadable_config_means_no_agents_and_a_clear_error(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[agents.gpt\n", encoding="utf-8")
    monkeypatch.setattr(loader, "_GLOBAL_CONFIG", cfg)
    config = loader.load_config()
    assert config["agents"] == {} and "couldn't read" in config["_error"]
    assert "ixel setup" in capsys.readouterr().err


def test_wizard_writes_the_config_as_utf8(monkeypatch, tmp_path):
    from ixel_mat.config import setup as wizard
    monkeypatch.setattr(wizard, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(wizard, "_CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(wizard.Confirm, "ask", lambda *a, **k: True)
    agents = [{"id": "gpt", "type": "http", "url": "https://api.openai.com/v1/chat/completions",
               "token_env": "OPENAI_API_KEY", "model": "gpt-5", "label": "GPT — main", "color": "green"}]
    wizard._print_summary_and_write(agents)
    data = (tmp_path / "config.toml").read_bytes()
    assert "GPT — main".encode("utf-8") in data and data.decode("utf-8").startswith("# Ixel MAT — ")


# ── A server's error that quotes a key back ───────────────────────────────────

from fake_providers import ThreadedFakeProvider, error_reply
from ixel_mat.material import mask_secrets
from ixel_mat.modes.review import ReviewMode, run_review

ECHOED = "sk-ant-api03-" + "Q" * 40


def test_mask_secrets_blanks_keys_and_keeps_the_rest():
    text = mask_secrets(f"invalid key {ECHOED} for org; token ghp_{'a' * 36} too")
    assert ECHOED not in text and "ghp_" not in text
    assert text == "invalid key [API key hidden] for org; token [GitHub token hidden] too"


def test_a_providers_error_never_shows_the_key_it_quoted():
    with ThreadedFakeProvider(lambda r: error_reply(400, f"invalid key {ECHOED} for org")) as fake:
        cfg = AgentConfig(name="gpt", label="GPT", type="http", url=fake.openai_url, model="m", token=ECHOED)

        async def go():
            agent = HttpAgent(cfg)
            await agent.connect()
            try:
                with pytest.raises(RuntimeError) as raised:
                    await agent.send_and_receive("q")
                return str(raised.value)
            finally:
                await agent.disconnect()

        error = asyncio.run(go())
    assert "API 400" in error and "invalid key [API key hidden] for org" in error and ECHOED not in error


def test_a_panel_members_error_is_masked_in_the_result():
    class Leaky:
        name, label, model, is_connected = "leaky", "Leaky", "m", True

        async def send_and_receive(self, message, **kwargs):
            raise RuntimeError(f"401: bad key {ECHOED}")

    class Fine(Leaky):
        name, label = "fine", "Fine"

        async def send_and_receive(self, message, **kwargs):
            return "391"

    result = asyncio.run(run_review("q", [Leaky(), Fine()], mode=ReviewMode.QUICK))
    assert result.failures and all(ECHOED not in f.error for f in result.failures)
    assert "[API key hidden]" in result.failures[0].error


OWN_KEY = "local-gateway-key-0123456789abcdef"  # a format no pattern knows: only the agent's own key catches it


def test_an_agents_own_key_is_blanked_out_whatever_it_looks_like():
    class Leaky:
        name, label, model, is_connected = "leaky", "Leaky", "m", True
        config = AgentConfig(name="leaky", label="Leaky", type="http", url="http://127.0.0.1:1/v1", token=OWN_KEY)

        async def send_and_receive(self, message, **kwargs):
            raise RuntimeError(f"401: bad key {OWN_KEY}")

    class Fine(Leaky):
        name, label = "fine", "Fine"

        async def send_and_receive(self, message, **kwargs):
            return "391"

    result = asyncio.run(run_review("q", [Leaky(), Fine()], mode=ReviewMode.QUICK))
    assert result.failures[0].error == "401: bad key [key hidden]"
    compared = asyncio.run(FullModeDispatcher([Leaky(), Fine()], timeout=5).dispatch("q"))
    assert all(OWN_KEY not in r.answer + r.raw + r.degraded_reason for r in compared.responses)


def test_a_cli_error_is_masked_before_it_is_cut():
    """The last 500 characters are kept: cut first, a key's sk- start could go and the rest stay."""
    key = "sk-proj-" + "K" * 60
    agent = _oneshot(f"import sys; sys.stderr.write('Error: {key} ' + 'y' * 470); sys.exit(1)")
    with pytest.raises(RuntimeError) as raised:
        _ask(agent)
    assert "K" * 20 not in str(raised.value) and "hidden]" in str(raised.value)


def test_ixel_ask_and_connecting_never_show_the_agents_own_key(monkeypatch):
    from ixel_mat.ask import AskError, ask
    from ixel_mat.runtime import connect_agents

    with ThreadedFakeProvider(lambda r: error_reply(401, f"no such key {OWN_KEY}")) as fake:
        cfg = AgentConfig(name="gw", label="Gateway", type="http", url=fake.openai_url, model="m", token=OWN_KEY)
        with pytest.raises(AskError) as raised:
            asyncio.run(ask(cfg, "q"))
    assert OWN_KEY not in str(raised.value) and "[key hidden]" in str(raised.value)

    class Refused(HttpAgent):
        async def connect(self):
            raise RuntimeError(f"gateway refused {OWN_KEY}")

    seen = []
    cfg = AgentConfig(name="gw", label="Gateway", type="http", url="http://127.0.0.1:1/v1", model="m", token=OWN_KEY)
    monkeypatch.setattr("ixel_mat.runtime.create_agent", Refused)
    asyncio.run(connect_agents({"gw": cfg}, on_result=lambda c, e: seen.append(str(e))))
    assert seen == ["gateway refused [key hidden]"]
