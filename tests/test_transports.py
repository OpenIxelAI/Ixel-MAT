"""Agent transports: HTTP providers, Anthropic SDK path, and command-line agents."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from fake_providers import FakeProvider, Recorded, anthropic_reply, error_reply, openai_reply
from ixel_mat.agents import http as http_mod
from ixel_mat.agents import create_agent
from ixel_mat.agents.base import AgentConfig, nearest_effort
from ixel_mat.agents.http import (ANTHROPIC_DEEP_MAX_TOKENS, ANTHROPIC_MAX_TOKENS, FALLBACK_BETA, HttpAgent,
                                  _anthropic_base_url)
from ixel_mat.agents.oneshot import OneShotAgent
from ixel_mat.config import loader, secrets


def _run(coro):
    return asyncio.run(coro)


async def _ask_http(url, model, message="hello", *, times=1, handler=None, provider=None):
    agent = HttpAgent(AgentConfig(name="a", label="A", type="http", url=url, token="sk-test", model=model))
    await agent.connect()
    try:
        return [await agent.send_and_receive(message) for _ in range(times)]
    finally:
        await agent.disconnect()


# ── OpenAI-compatible ─────────────────────────────────────────────────────────

def test_openai_request_has_no_temperature():
    async def go():
        async with FakeProvider() as fake:
            answers = await _ask_http(fake.openai_url, "o3-mini")
            return answers, fake.requests

    answers, requests = _run(go())
    assert answers == ["fake answer"]
    body = requests[0].body
    assert "temperature" not in body  # reasoning models reject non-default values
    assert body["model"] == "o3-mini"
    assert requests[0].headers["authorization"] == "Bearer sk-test"


def test_openai_redirects_are_not_followed():
    # A 307 would re-POST the prompt to wherever it points, cleartext http included
    async def go():
        async with FakeProvider(handler=lambda r: (307, {}, {"Location": "http://example.com/steal"})) as fake:
            with pytest.raises(RuntimeError, match="redirected to http://example.com/steal"):
                await _ask_http(fake.openai_url, "gpt-x")
            return len(fake.requests)

    assert _run(go()) == 1


def test_openai_retries_rate_limits_then_succeeds():
    replies = iter([error_reply(429, "slow down", {"Retry-After": "0"}), openai_reply("finally")])

    async def go():
        async with FakeProvider(handler=lambda r: next(replies)) as fake:
            return await _ask_http(fake.openai_url, "gpt-x"), len(fake.requests)

    answers, calls = _run(go())
    assert answers == ["finally"] and calls == 2


def test_openai_gives_up_after_retries():
    async def go():
        async with FakeProvider(handler=lambda r: error_reply(503, "down", {"Retry-After": "0"})) as fake:
            with pytest.raises(RuntimeError, match="API 503"):
                await _ask_http(fake.openai_url, "gpt-x")
            return len(fake.requests)

    assert _run(go()) == 3  # first try + 2 retries


def test_openai_does_not_retry_client_errors():
    async def go():
        async with FakeProvider(handler=lambda r: error_reply(401, "bad key")) as fake:
            with pytest.raises(RuntimeError, match="API 401"):
                await _ask_http(fake.openai_url, "gpt-x")
            return len(fake.requests)

    assert _run(go()) == 1


def test_openai_content_part_arrays_are_joined():
    reply = (200, {"choices": [{"message": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}}]}, {})

    async def go():
        async with FakeProvider(handler=lambda r: reply) as fake:
            return await _ask_http(fake.openai_url, "m")

    assert _run(go()) == ["ab"]


# ── Anthropic (official SDK) ──────────────────────────────────────────────────

def test_anthropic_base_url():
    assert _anthropic_base_url("https://api.anthropic.com/v1/messages") == "https://api.anthropic.com"
    assert _anthropic_base_url("https://proxy.example/anthropic/v1/messages/") == "https://proxy.example/anthropic"


@pytest.fixture
def anthropic_to_fake(monkeypatch):
    """The SDK path is only taken for anthropic.com URLs; point it at the fake server."""
    from ixel_mat.agents import http as http_mod

    holder = {}
    monkeypatch.setattr(http_mod, "_anthropic_base_url", lambda url: f"http://127.0.0.1:{holder['port']}")
    return holder


def test_anthropic_uses_sdk_with_key_header_and_room_to_think(anthropic_to_fake):
    async def go():
        async with FakeProvider() as fake:
            anthropic_to_fake["port"] = fake.port
            answers = await _ask_http("https://api.anthropic.com/v1/messages", "claude-sonnet-5")
            return answers, fake.requests

    answers, requests = _run(go())
    assert answers == ["fake answer"]
    req = requests[0]
    assert req.api == "anthropic"
    assert req.headers["x-api-key"] == "sk-test"
    assert req.body["max_tokens"] == ANTHROPIC_MAX_TOKENS
    assert "fallbacks" not in req.body  # only for the Opus 5 / Fable 5 family
    assert FALLBACK_BETA not in req.headers.get("anthropic-beta", "")


def test_anthropic_redirects_are_not_followed(anthropic_to_fake):
    # x-api-key would survive a cross-host redirect, so none are followed at all
    async def go():
        async with FakeProvider(handler=lambda r: (307, {}, {"Location": "http://example.com/steal"})) as fake:
            anthropic_to_fake["port"] = fake.port
            with pytest.raises(Exception) as err:
                await _ask_http("https://api.anthropic.com/v1/messages", "claude-sonnet-5")
            return len(fake.requests), err.value

    calls, error = _run(go())
    assert calls == 1 and "307" in str(error)


def test_anthropic_opus5_enables_refusal_fallbacks_and_keeps_only_text(anthropic_to_fake):
    thinking = {"type": "thinking", "thinking": "", "signature": "sig"}

    async def go():
        async with FakeProvider(handler=lambda r: anthropic_reply("the answer", extra_blocks=[thinking])) as fake:
            anthropic_to_fake["port"] = fake.port
            return await _ask_http("https://api.anthropic.com/v1/messages", "claude-opus-5"), fake.requests

    answers, requests = _run(go())
    assert answers == ["the answer"]
    assert requests[0].body["fallbacks"] == "default"
    assert FALLBACK_BETA in requests[0].headers["anthropic-beta"]


def test_anthropic_drops_fallbacks_when_unsupported(anthropic_to_fake):
    def handler(r: Recorded):
        if "fallbacks" in r.body:
            return error_reply(400, "fallbacks: not supported for this model")
        return anthropic_reply("ok")

    async def go():
        async with FakeProvider(handler=handler) as fake:
            anthropic_to_fake["port"] = fake.port
            answers = await _ask_http("https://api.anthropic.com/v1/messages", "claude-opus-5", times=2)
            return answers, fake.requests

    answers, requests = _run(go())
    assert answers == ["ok", "ok"]
    # 1st call: rejected with fallbacks, retried without; 2nd call: straight without
    assert ["fallbacks" in r.body for r in requests] == [True, False, False]


def test_anthropic_refusal_is_an_error_not_an_empty_answer(anthropic_to_fake):
    refusal = anthropic_reply(stop_reason="refusal",
                              stop_details={"type": "refusal", "category": "cyber", "explanation": "x"})

    async def go():
        async with FakeProvider(handler=lambda r: refusal) as fake:
            anthropic_to_fake["port"] = fake.port
            await _ask_http("https://api.anthropic.com/v1/messages", "claude-sonnet-5")

    with pytest.raises(RuntimeError, match=r"declined this request \(cyber\)"):
        _run(go())


def test_anthropic_truncation_is_flagged(anthropic_to_fake):
    async def go():
        async with FakeProvider(handler=lambda r: anthropic_reply("partial", stop_reason="max_tokens")) as fake:
            anthropic_to_fake["port"] = fake.port
            return await _ask_http("https://api.anthropic.com/v1/messages", "claude-sonnet-5")

    assert _run(go()) == ["partial\n\n[Answer cut off at the output limit.]"]


# ── Command-line agents ───────────────────────────────────────────────────────

def _cli(script, **config):
    return OneShotAgent(AgentConfig(name="cli", label="CLI", type="oneshot", command=sys.executable,
                                    args=["-c", script], **config))


async def _ask_cli(agent, message="q"):
    await agent.connect()
    return await agent.send_and_receive(message)


def test_prompt_via_arg_cannot_become_a_flag():
    agent = _cli("import sys; print(sys.argv[1:])", prompt_via="arg")
    cmd, stdin = agent._build_command("--dangerously-skip-permissions")
    assert cmd[-2:] == ["--", "--dangerously-skip-permissions"] and stdin is None
    assert _run(_ask_cli(agent, "--dangerously-skip-permissions")) == "['--', '--dangerously-skip-permissions']"


def test_prompt_via_stdin_keeps_prompt_out_of_argv():
    agent = _cli("import sys; print('argv', sys.argv[1:]); print('stdin', sys.stdin.read())", prompt_via="stdin")
    assert _run(_ask_cli(agent, "secret question")) == "argv []\nstdin secret question"


def test_cli_agent_cannot_read_the_terminal():
    agent = _cli("input('confirm? ')")  # would hang forever if stdin were inherited
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="exited with code 1"):
        _run(_ask_cli(agent))
    assert time.monotonic() - started < 10


def test_cli_agent_runs_in_a_fresh_empty_dir_by_default(monkeypatch):
    monkeypatch.setenv("PWD", os.getcwd())
    agent = _cli("import os; print(os.getcwd()); print(os.listdir('.')); print(os.environ.get('PWD'))")
    cwd, listing, pwd = _run(_ask_cli(agent)).splitlines()
    assert os.path.realpath(cwd) != os.path.realpath(os.getcwd())
    assert listing == "[]"
    assert not os.path.exists(cwd)  # cleaned up afterwards
    # OpenCode 2 works in the folder PWD names, not the one it starts in
    assert os.path.realpath(pwd) == os.path.realpath(cwd)


def test_cli_agent_workdir_inherit():
    agent = _cli("import os; print(os.getcwd())", workdir="inherit")
    assert os.path.realpath(_run(_ask_cli(agent))) == os.path.realpath(os.getcwd())


def test_cli_agent_does_not_see_ixel_secrets_unless_allowed(monkeypatch):
    monkeypatch.setenv("IXEL_TEST_PROVIDER_KEY", "sk-from-ixel-dotenv")
    monkeypatch.setattr(secrets, "_INJECTED", {"IXEL_TEST_PROVIDER_KEY"})
    monkeypatch.setenv("IXEL_TEST_USER_VAR", "exported-by-user")
    script = "import os; print(os.environ.get('IXEL_TEST_PROVIDER_KEY')); print(os.environ.get('IXEL_TEST_USER_VAR'))"

    assert _run(_ask_cli(_cli(script))) == "None\nexported-by-user"
    allowed = _cli(script, pass_env=["IXEL_TEST_PROVIDER_KEY"])
    assert _run(_ask_cli(allowed)) == "sk-from-ixel-dotenv\nexported-by-user"


def test_cli_agent_env_drops_stray_keys_and_adds_fixed_settings(monkeypatch):
    # A key exported in the user's shell would be billed instead of the CLI's
    # subscription login; drop_env removes it. env adds the CLI's lock-down config.
    monkeypatch.setenv("IXEL_TEST_STRAY_KEY", "sk-exported")
    monkeypatch.setenv("IXEL_TEST_FIXED", "from-the-shell")
    script = ("import os; print(os.environ.get('IXEL_TEST_STRAY_KEY')); print(os.environ.get('IXEL_TEST_FIXED')); "
              "print(os.environ.get('IXEL_PANEL_DEPTH'))")
    agent = _cli(script, drop_env=["IXEL_TEST_STRAY_KEY"],
                 env={"IXEL_TEST_FIXED": "locked-down", "IXEL_PANEL_DEPTH": "0"})
    assert _run(_ask_cli(agent)) == "None\nlocked-down\n1"  # the loop guard can't be overridden
    # Named in pass_env, it goes after all: someone who lists a key wants that CLI to have it
    agent = _cli(script, drop_env=["IXEL_TEST_STRAY_KEY"], pass_env=["IXEL_TEST_STRAY_KEY"])
    assert _run(_ask_cli(agent)).splitlines()[0] == "sk-exported"


def test_cli_agent_args_can_depend_on_the_version_installed(monkeypatch):
    from ixel_mat import health
    from ixel_mat.agents import oneshot
    asked = []

    async def run_program(argv, timeout=None, env=None):
        asked.append(argv)
        return 0, "Python 3.99.0"

    monkeypatch.setattr(health, "run_program", run_program)
    monkeypatch.setattr(oneshot, "_MAJOR_VERSIONS", {})
    agent = _cli("print('newest')", prompt_via="stdin", args_by_version={"3": ["-c", "print('three')"]})
    assert _run(_ask_cli(agent)) == "three"
    assert _run(_ask_cli(agent)) == "three" and len(asked) == 1  # asked once
    assert asked[0][1:] == ["--version"]
    other = _cli("print('newest')", prompt_via="stdin", args_by_version={"2": ["-c", "print('two')"]})
    assert _run(_ask_cli(other)) == "newest" and len(asked) == 1


def test_a_failed_version_check_or_a_failed_run_asks_the_version_again(monkeypatch):
    from ixel_mat import health
    from ixel_mat.agents import oneshot
    answers, asked = iter([(1, "error"), (0, "v3.0.0"), (0, "v3.0.0")]), []

    async def run_program(argv, timeout=None, env=None):
        asked.append(argv)
        return next(answers)

    monkeypatch.setattr(health, "run_program", run_program)
    monkeypatch.setattr(oneshot, "_MAJOR_VERSIONS", {})
    agent = _cli("print('newest')", prompt_via="stdin", args_by_version={"3": ["-c", "import sys; sys.exit(2)"]})
    assert _run(_ask_cli(agent)) == "newest"  # --version failed: the usual args, and it isn't remembered
    for _ in range(2):  # version 3's args fail: a wrapper may hide an update, so it's asked again
        with pytest.raises(RuntimeError):
            _run(_ask_cli(agent))
    assert len(asked) == 3


def test_your_own_args_cant_leave_out_what_a_version_needs_for_its_lockdown(monkeypatch):
    from ixel_mat import health
    from ixel_mat.agents import oneshot
    version = ["v2.0.22"]

    async def run_program(argv, timeout=None, env=None):
        return 0, version[0]

    monkeypatch.setattr(health, "run_program", run_program)
    required = {"1": [], "*": ["--standalone"]}
    monkeypatch.setattr(oneshot, "_MAJOR_VERSIONS", {})
    agent = _cli("print('answered')", prompt_via="stdin", required_args=required)  # args: -c <script>
    with pytest.raises(RuntimeError, match="needs --standalone in its args"):
        _run(_ask_cli(agent))
    version[0], oneshot._MAJOR_VERSIONS = "v1.18.34", {}
    assert _run(_ask_cli(agent)) == "answered"  # OpenCode 1 has no --standalone, and needs none
    version[0], oneshot._MAJOR_VERSIONS = "unknown", {}
    with pytest.raises(RuntimeError, match="couldn't tell which version"):
        _run(_ask_cli(agent))
    # The preset's: each version's flag is one the other refuses, so a version remembered wrongly fails
    from ixel_mat.presets import PRESETS_BY_ID
    assert PRESETS_BY_ID["opencode"]["required_args"] == {"1": ["--pure"], "*": ["--standalone"]}


def test_pwd_names_the_folder_a_program_starts_in_in_full():
    from ixel_mat.agents.base import in_folder
    assert in_folder({}, "relative")["PWD"] == os.path.abspath("relative")


def test_subscription_clis_never_see_other_vendors_keys():
    from ixel_mat.presets import OTHER_KEYS, PRESETS_BY_ID
    for preset_id in ("claude_code", "codex", "gemini_cli", "copilot"):
        assert set(OTHER_KEYS) <= set(PRESETS_BY_ID[preset_id]["drop_env"]), preset_id
    assert {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY", "GROQ_API_KEY"} <= set(OTHER_KEYS)
    assert "GITHUB_TOKEN" not in OTHER_KEYS and "GH_TOKEN" not in OTHER_KEYS  # Copilot signs in with those
    # OpenCode may be set up to use them, so it keeps them
    assert "drop_env" not in PRESETS_BY_ID["opencode"]


def _gemini_env():
    from ixel_mat.agents.oneshot import cli_env
    from ixel_mat.presets import PRESETS_BY_ID
    return cli_env(AgentConfig(name="gemini", label="Gemini CLI", type="oneshot", command="gemini",
                               drop_env=PRESETS_BY_ID["gemini_cli"]["drop_env"]))


def test_gemini_cli_gets_your_google_key_only_while_it_has_no_sign_in(gemini_home, monkeypatch):
    from ixel_mat.agents.oneshot import cli_env
    from ixel_mat.presets import PRESETS_BY_ID
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-saved-under-keys")
    monkeypatch.setattr(secrets, "_INJECTED", {"GOOGLE_API_KEY"})  # from Ixel's .env (Settings, Keys)
    env = _gemini_env()
    assert env["GEMINI_API_KEY"] == "AIza-saved-under-keys" and "GOOGLE_API_KEY" not in env
    # Signed in with Google, it keeps the key out, which would otherwise be billed in place of the login
    (gemini_home / ".gemini" / "settings.json").write_text(
        json.dumps({"security": {"auth": {"selectedType": "oauth-personal"}}}), encoding="utf-8")
    assert "GEMINI_API_KEY" not in _gemini_env()
    # And the other CLIs never get it
    claude = cli_env(AgentConfig(name="claude", label="Claude Code", type="oneshot", command="claude",
                                 drop_env=PRESETS_BY_ID["claude_code"]["drop_env"]))
    assert "GEMINI_API_KEY" not in claude and "GOOGLE_API_KEY" not in claude


def test_a_gemini_key_from_your_shell_goes_only_while_gemini_cli_has_no_sign_in(gemini_home, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-exported")
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-other")
    assert _gemini_env()["GEMINI_API_KEY"] == "AIza-exported"  # its own name for it comes first
    monkeypatch.setenv("GOOGLE_GENAI_USE_GCA", "true")
    assert "GEMINI_API_KEY" not in _gemini_env()


def test_a_google_key_from_your_shell_isnt_given_to_gemini_cli(gemini_home, monkeypatch):
    # Exported under that name it's often a billed Google Cloud key: only the one saved in Settings goes
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-cloud-project")
    monkeypatch.setattr(secrets, "_INJECTED", set())
    assert "GEMINI_API_KEY" not in _gemini_env() and "GOOGLE_API_KEY" not in _gemini_env()


@pytest.mark.parametrize("files, variables, signed_in", [
    ({".gemini/settings.json": {"security": {"auth": {"selectedType": "oauth-personal"}}}}, {}, True),
    ({"system": {"security": {"auth": {"selectedType": "vertex-ai"}}}}, {}, True),
    ({".gemini/settings.json": '// signed in\n{"security": {"auth": {"selectedType": "oauth-personal"}}}'}, {}, True),
    ({".gemini/settings.json": '{"security": {"auth": {/* "selectedType": "x" */}}}  // "selectedType"'}, {}, False),
    ({".gemini/settings.json": '{"security": '}, {}, True),  # it can't read it, and won't start either
    ({}, {"GOOGLE_GENAI_USE_GCA": "true"}, True),
    ({}, {"CLOUD_SHELL": "true"}, True),  # a key would be used ahead of this one
    ({}, {"GOOGLE_GEMINI_BASE_URL": "https://gateway.example"}, True),  # the gateway would be sent the key
    ({".env": "GEMINI_API_KEY=its-own\n"}, {}, True),
    # It reads .env files before --skip-trust counts, so Ixel's empty folder isn't trusted: no ~/.gemini/.env,
    # and only keys from ~/.env, unless you trust every folder
    ({".gemini/.env": "GEMINI_API_KEY=its-own\n"}, {}, False),
    ({".env": "export GOOGLE_GENAI_USE_GCA=true\n"}, {}, False),
    ({".gemini/.env": "GOOGLE_GEMINI_BASE_URL=https://gateway.example\n"}, {}, True),  # never sent the key
    ({".gemini/.env": "GEMINI_API_KEY=its-own\n"}, {"GEMINI_CLI_TRUST_WORKSPACE": "true"}, True),
    ({".gemini/.env": "GOOGLE_GENAI_USE_GCA=true\n",
      ".gemini/settings.json": {"security": {"folderTrust": {"enabled": False}}}}, {}, True),
    # Not signed in: Gemini CLI stops with "Please set an Auth method"
    ({}, {}, False),
    ({".gemini/settings.json": {"mcpServers": {"handoff": {"command": "handoff"}}}}, {}, False),
    ({".gemini/settings.json": {"security": {"auth": {"selectedType": ""}}}}, {}, False),
    ({".gemini/oauth_creds.json": {"refresh_token": "x"}}, {}, False),  # not used without selectedType
    ({}, {"GOOGLE_GENAI_USE_GCA": "false"}, False),
])
def test_gemini_cli_is_judged_signed_in_as_it_judges_itself(gemini_home, files, variables, signed_in):
    from ixel_mat.presets import GEMINI_SIGN_IN, gemini_has_sign_in, gemini_key_env
    system = os.environ["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]
    for name, content in files.items():
        path = Path(system) if name == "system" else gemini_home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    env = {**os.environ, **variables}
    assert gemini_has_sign_in(env) is signed_in
    assert gemini_key_env(env, {"GOOGLE_API_KEY": "AIza-x"}) == ({} if signed_in else {"GEMINI_API_KEY": "AIza-x"})
    assert len(GEMINI_SIGN_IN) <= 160  # the terminal shows that much of a problem


def test_a_cli_that_isnt_signed_in_says_what_to_do_instead_of_its_own_error(gemini_home, monkeypatch):
    from ixel_mat.agents import oneshot
    from ixel_mat.presets import GEMINI_SIGN_IN, PRESETS_BY_ID
    script = ("import sys; sys.stderr.write('Please set an Auth method in your /home/x/.gemini/settings.json or "
              "specify one of the following environment variables before running: GEMINI_API_KEY, "
              "GOOGLE_GENAI_USE_VERTEXAI, GOOGLE_GENAI_USE_GCA\\n'); sys.exit(41)")
    with pytest.raises(RuntimeError) as failure:  # another CLI that says so keeps its own words
        _run(_ask_cli(_cli(script, prompt_via="stdin")))
    assert "exited with code 41: Please set an Auth method" in str(failure.value)
    monkeypatch.setattr(oneshot, "preset_for", lambda command: PRESETS_BY_ID["gemini_cli"])
    with pytest.raises(RuntimeError) as failure:
        _run(_ask_cli(_cli(script, prompt_via="stdin")))
    assert str(failure.value) == GEMINI_SIGN_IN


@pytest.mark.parametrize("says, tell", [
    ("Error: Error from provider (Console): OpenCode's free tier can only be used from within OpenCode",
     "OPENCODE_FREE_TIER"),
    ("Error: Unexpected error\n\nDatabase is not empty and has no session table", "OPENCODE_OLD_COPY"),
    ("Error: Model unavailable: anthropic/claude-zzz-future-9", "OPENCODE_UNKNOWN_MODEL"),  # kept from fetching it
])
def test_what_stops_opencode_is_said_plainly(monkeypatch, says, tell):
    from ixel_mat import presets
    from ixel_mat.agents import oneshot
    opencode = {k: v for k, v in presets.PRESETS_BY_ID["opencode"].items() if k != "required_args"}  # Python's args
    monkeypatch.setattr(oneshot, "preset_for", lambda command: opencode)
    script = f"import sys; sys.stderr.write({says!r}); sys.exit(1)"
    with pytest.raises(RuntimeError) as failure:
        _run(_ask_cli(_cli(script, prompt_via="stdin")))
    assert str(failure.value) == getattr(presets, tell) and len(str(failure.value)) <= 160


def test_questions_reach_the_subscription_clis_on_stdin_not_on_their_command_line():
    # Another person on this computer can read any program's command line (`ps`)
    from ixel_mat.presets import CLI_PRESETS
    assert {p["id"]: p["prompt_via"] for p in CLI_PRESETS} == {
        "claude_code": "stdin", "codex": "stdin", "gemini_cli": "stdin", "copilot": "stdin", "opencode": "stdin"}


def test_cli_agent_effort_flag_uses_the_nearest_supported_level():
    script = "import sys; print(sys.argv[1:])"
    agent = _cli(script, prompt_via="stdin", effort="max", effort_args=["--effort", "{effort}"],
                 effort_levels=["low", "medium", "high", "xhigh"])
    assert _run(_ask_cli(agent)) == "['--effort', 'xhigh']"

    async def per_call():
        await agent.connect()
        return await agent.send_and_receive("q", effort="minimal")  # saver mode's verifier effort

    assert _run(per_call()) == "['--effort', 'low']"
    assert _run(_ask_cli(_cli(script, prompt_via="stdin", effort_args=["--effort", "{effort}"]))) == "[]"


@pytest.mark.parametrize("effort,levels,expected", [
    ("high", ["low", "high"], "high"),
    ("minimal", ["low", "medium", "high"], "low"),
    ("max", ["minimal", "low", "medium", "high", "xhigh"], "xhigh"),
    ("medium", ["low", "high"], "low"),       # a tie goes to the cheaper level
    ("high", None, "high"),
    ("high", ["none", "auto"], "high"),       # nothing comparable: pass it through
])
def test_nearest_effort(effort, levels, expected):
    assert nearest_effort(effort, levels) == expected


def test_load_env_records_only_keys_it_injected(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("FROM_FILE_ONLY=a\nALREADY_EXPORTED=b\n")
    monkeypatch.setattr(secrets, "_ENV_FILE", env_path)
    monkeypatch.setattr(secrets, "_INJECTED", set())
    monkeypatch.setenv("FROM_FILE_ONLY", "placeholder")
    monkeypatch.delenv("FROM_FILE_ONLY")
    monkeypatch.setenv("ALREADY_EXPORTED", "user")

    secrets.load_env()
    assert secrets._INJECTED == {"FROM_FILE_ONLY"}


def test_missing_command_is_an_error():
    agent = OneShotAgent(AgentConfig(name="x", label="x", type="oneshot", command="definitely-not-a-real-cli-xyz"))
    with pytest.raises(RuntimeError, match="Command not found"):
        _run(_ask_cli(agent))


# ── Config ────────────────────────────────────────────────────────────────────

def test_agent_settings_are_parsed_and_validated():
    configs, warnings = loader.build_agent_configs({"agents": {
        "claude_code": {"type": "oneshot", "label": "Claude Code", "command": "claude", "args": ["-p"],
                        "prompt_via": "stdin", "workdir": "inherit", "timeout": 300, "pass_env": ["X"]},
        "bad": {"type": "oneshot", "label": "Bad", "command": "x", "prompt_via": "telepathy",
                "timeout": "soon", "pass_env": "X"},
    }})
    good = configs["claude_code"]
    assert (good.prompt_via, good.workdir, good.call_timeout, good.pass_env) == ("stdin", "inherit", 300.0, ["X"])
    bad = configs["bad"]
    assert (bad.prompt_via, bad.workdir, bad.call_timeout, bad.pass_env) == ("flag", "temp", 180.0, None)
    assert len([w for w in warnings if w.startswith("Agent 'bad'")]) == 3


def test_websocket_agent_does_not_follow_redirects():
    # The gateway token goes out right after connecting; a redirect must not carry it elsewhere
    from websockets.asyncio.server import serve
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    from ixel_mat.agents.websocket import WebSocketAgent

    reached = []

    async def target(ws):
        reached.append("target")
        await ws.close()

    async def go():
        async with serve(target, "127.0.0.1", 0) as target_server:
            target_port = target_server.sockets[0].getsockname()[1]

            def redirect(connection, request):
                return Response(302, "Found", Headers({"Location": f"ws://127.0.0.1:{target_port}/",
                                                       "Content-Length": "0"}))

            async with serve(target, "127.0.0.1", 0, process_request=redirect) as origin_server:
                port = origin_server.sockets[0].getsockname()[1]
                agent = WebSocketAgent(AgentConfig(name="gw", label="GW", type="websocket",
                                                   url=f"ws://127.0.0.1:{port}", token="gateway-token"))
                with pytest.raises(Exception, match="302"):
                    await agent.connect()

    _run(go())
    assert reached == []


def test_websocket_agent_catches_a_final_event_right_behind_the_send_reply(tmp_path, monkeypatch):
    # A fast gateway sends the chat.send reply and state=final back to back. The final
    # used to arrive before the run was registered, and the call timed out.
    import json

    from websockets.asyncio.server import serve

    from ixel_mat.agents import websocket as ws_agent

    monkeypatch.setattr(ws_agent, "_KEY_FILE", tmp_path / "device_key")

    async def gateway(ws):
        await ws.send(json.dumps({"type": "event", "event": "connect.challenge", "payload": {"nonce": "n"}}))
        hello = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "res", "id": hello["id"], "ok": True, "payload": {"type": "hello-ok"}}))
        async for raw in ws:
            req = json.loads(raw)
            if req["method"] == "chat.send":
                run_id = "run-" + req["id"][:8]
                # Back to back, so both frames are buffered before the client reads either
                await ws.send(json.dumps({"type": "res", "id": req["id"], "ok": True, "payload": {"runId": run_id}}))
                await ws.send(json.dumps({"type": "event", "event": "chat",
                                          "payload": {"state": "final", "runId": run_id}}))
            elif req["method"] == "chat.history":
                await ws.send(json.dumps({"type": "res", "id": req["id"], "ok": True, "payload": {
                    "messages": [{"role": "assistant", "content": "fast answer"}]}}))

    async def go():
        async with serve(gateway, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            agent = ws_agent.WebSocketAgent(AgentConfig(name="gw", label="GW", type="websocket",
                                                        url=f"ws://127.0.0.1:{port}", token="t"),
                                            response_timeout=3)
            await agent.connect()
            try:
                return [await agent.send_and_receive("q1"), await agent.send_and_receive("q2")]
            finally:
                await agent.disconnect()

    assert _run(go()) == ["fast answer", "fast answer"]


def test_factory_passes_configured_timeout_to_websocket_agents():
    agent = create_agent(AgentConfig(name="gw", label="GW", type="websocket", url="ws://127.0.0.1:1", timeout=42))
    assert agent.response_timeout == 42


# ── Output file (Codex `-o FILE`) and the panel loop guard ───────────────────

def test_output_flag_reads_the_final_answer_from_a_file():
    script = ("import sys; path = sys.argv[sys.argv.index('-o') + 1]; "
              "print('progress chatter the user should not see'); "
              "open(path, 'w').write('the final answer\\n')")
    agent = _cli(script, output_flag="-o", prompt_via="stdin")
    assert _run(_ask_cli(agent)) == "the final answer"


def test_output_flag_falls_back_to_stdout_when_no_file_is_written():
    agent = _cli("print('from stdout')", output_flag="-o", prompt_via="stdin")
    assert _run(_ask_cli(agent)) == "from stdout"


def test_output_file_dir_is_cleaned_up_with_inherited_workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    script = "import sys, os; path = sys.argv[sys.argv.index('-o') + 1]; open(path, 'w').write(path)"
    agent = _cli(script, output_flag="-o", prompt_via="stdin", workdir="inherit")
    path = _run(_ask_cli(agent))
    assert path.endswith("answer.txt") and not os.path.exists(path)
    assert list(tmp_path.iterdir()) == []  # nothing left in the user's directory


def test_launched_programs_are_marked_with_panel_depth(monkeypatch):
    monkeypatch.delenv("IXEL_PANEL_DEPTH", raising=False)
    agent = _cli("import os; print(os.environ.get('IXEL_PANEL_DEPTH'))")
    assert _run(_ask_cli(agent)) == "1"
    monkeypatch.setenv("IXEL_PANEL_DEPTH", "1")
    assert _run(_ask_cli(_cli("import os; print(os.environ.get('IXEL_PANEL_DEPTH'))"))) == "2"


def test_a_panel_member_cannot_start_another_panel(monkeypatch):
    from ixel_mat.modes.review import run_review

    class Agent:
        name = label = "x"
        is_connected = True
        calls = 0

        async def send_and_receive(self, message, **kwargs):
            Agent.calls += 1
            return "answer"

    monkeypatch.setenv("IXEL_PANEL_DEPTH", "1")
    events = []
    result = _run(run_review("q", [Agent(), Agent()], on_event=events.append))
    assert "Refusing to start a panel review from inside another one" in result.error
    assert Agent.calls == 0 and [e.kind for e in events] == ["final"]


# ── Thinking level (effort) ───────────────────────────────────────────────────

def _effort_agent(url, model, effort=""):
    return HttpAgent(AgentConfig(name="a", label="A", type="http", url=url, token="sk", model=model, effort=effort))


async def _ask_with(agent, **kwargs):
    await agent.connect()
    try:
        return await agent.send_and_receive("q", **kwargs)
    finally:
        await agent.disconnect()


def test_openai_effort_becomes_reasoning_effort():
    async def go():
        async with FakeProvider() as fake:
            await _ask_with(_effort_agent(fake.openai_url, "o3", "low"))
            await _ask_with(_effort_agent(fake.openai_url, "o3", "max"))       # capped to the API's top
            await _ask_with(_effort_agent(fake.openai_url, "o3"))              # unset: not sent
            await _ask_with(_effort_agent(fake.openai_url, "o3", "high"), effort="minimal")  # per-call wins
            return [r.body.get("reasoning_effort") for r in fake.requests]

    assert _run(go()) == ["low", "high", None, "minimal"]


def test_anthropic_effort_goes_in_output_config(anthropic_to_fake):
    async def go():
        async with FakeProvider() as fake:
            anthropic_to_fake["port"] = fake.port
            url = "https://api.anthropic.com/v1/messages"
            await _ask_with(_effort_agent(url, "claude-opus-5", "xhigh"))
            await _ask_with(_effort_agent(url, "claude-sonnet-5", "minimal"))  # no "minimal" on Claude
            await _ask_with(_effort_agent(url, "claude-sonnet-5"))
            await _ask_with(_effort_agent(url, "claude-opus-4-6", "xhigh"))    # no xhigh on the 4.6s
            await _ask_with(_effort_agent(url, "claude-haiku-4-5", "high"))    # Haiku 4.5 takes none
            return fake.requests

    requests = _run(go())
    assert [r.body.get("output_config") for r in requests] == [
        {"effort": "xhigh"}, {"effort": "low"}, None, {"effort": "high"}, None]
    # At xhigh there's room to think at length, and so long a reply is streamed (as the SDK requires)
    assert (requests[0].body["max_tokens"], requests[0].body.get("stream")) == (ANTHROPIC_DEEP_MAX_TOKENS, True)
    assert requests[1].body["max_tokens"] == ANTHROPIC_MAX_TOKENS and not requests[1].body.get("stream")


def test_effort_is_validated_in_config():
    configs, warnings = loader.build_agent_configs({"agents": {
        "a": {"type": "http", "label": "A", "effort": "low"},
        "b": {"type": "http", "label": "B", "effort": "turbo"},
    }})
    assert configs["a"].effort == "low" and configs["b"].effort == ""
    assert any("Agent 'b': effort must be one of" in w for w in warnings)



# ── Local model servers (no API key) ─────────────────────────────────────────

from ixel_mat.agents.base import needs_api_key


def test_only_local_http_agents_may_skip_the_key():
    assert not needs_api_key(AgentConfig(name="o", label="O", type="http", url="http://127.0.0.1:11434/v1/chat/completions"))
    assert needs_api_key(AgentConfig(name="g", label="G", type="http", url="https://api.openai.com/v1/chat/completions"))
    assert needs_api_key(AgentConfig(name="w", label="W", type="websocket", url="ws://127.0.0.1:1"))
    assert not needs_api_key(AgentConfig(name="c", label="C", type="oneshot", command="claude"))


def test_local_agent_works_without_a_key_and_sends_no_auth_header():
    async def go():
        async with FakeProvider() as fake:
            agent = HttpAgent(AgentConfig(name="llama", label="Llama", type="http",
                                          url=fake.openai_url, model="llama3.2"))
            answers = [await _ask_with(agent)]
            return answers, fake.requests[0].headers

    answers, headers = _run(go())
    assert answers == ["fake answer"] and "authorization" not in headers


def test_remote_agent_without_a_key_is_still_refused():
    agent = HttpAgent(AgentConfig(name="g", label="G", type="http",
                                  url="https://api.openai.com/v1/chat/completions", model="m"))
    with pytest.raises(ValueError, match="missing API token"):
        _run(agent.connect())


def test_validate_config_accepts_keyless_local_agents():
    issues = loader.validate_config({"agents": {
        "llama": {"type": "http", "label": "L", "url": "http://localhost:11434/v1/chat/completions", "model": "llama3.2"},
        "gpt": {"type": "http", "label": "G", "url": "https://api.openai.com/v1/chat/completions", "model": "m"},
    }})
    assert not any(i.startswith("Agent 'llama'") for i in issues)
    assert any(i.startswith("Agent 'gpt': API key not set") for i in issues)


def test_cli_agent_model_is_passed_only_as_a_valid_name():
    script = "import sys; print(sys.argv[1:])"
    agent = _cli(script, prompt_via="stdin", model="opus", model_args=["--model", "{model}"])
    assert _run(_ask_cli(agent)) == "['--model', 'opus']"
    assert _run(_ask_cli(_cli(script, prompt_via="stdin", model_args=["--model", "{model}"]))) == "[]"
    bad = _cli(script, prompt_via="stdin", model="--yolo", model_args=["--model", "{model}"])
    with pytest.raises(ValueError, match="isn't a valid model name"):
        bad._build_command("q")


# ── Streaming (the verdict as it's written) ───────────────────────────────────

PIECES = ["Seventeen ", "times twenty-three ", "is 391."]


def _timed_stream(make_agent):
    async def go():
        from fake_providers import StreamingProvider
        async with StreamingProvider(PIECES, delay=0.3) as fake:
            agent = make_agent(fake)
            await agent.connect()
            seen, loop = [], asyncio.get_running_loop()
            started = loop.time()

            async def on_text(text):
                seen.append((text, loop.time() - started))

            try:
                reply = await agent.send_and_receive("q", on_text=on_text)
                plain = await agent.send_and_receive("q")  # no on_text: an ordinary request
            finally:
                await agent.disconnect()
            return reply, plain, seen, loop.time() - started, fake.requests
    return _run(go())


def test_openai_compatible_agent_streams_when_asked():
    reply, plain, seen, _, requests = _timed_stream(lambda fake: HttpAgent(AgentConfig(
        name="a", label="A", type="http", url=f"http://127.0.0.1:{fake.port}/v1/chat/completions",
        token="sk", model="m")))
    assert reply == plain == "Seventeen times twenty-three is 391."
    assert [t for t, _ in seen] == PIECES
    assert seen[0][1] < seen[-1][1] - 0.4  # the first piece came well before the last
    assert requests[0]["stream"] is True and "stream" not in requests[1]


def test_anthropic_agent_streams_when_asked(monkeypatch):
    holder = {}
    monkeypatch.setattr(http_mod, "_anthropic_base_url", lambda url: holder["base"])

    def make(fake):
        holder["base"] = f"http://127.0.0.1:{fake.port}"
        return HttpAgent(AgentConfig(name="a", label="A", type="http", url="https://api.anthropic.com/v1/messages",
                                     token="sk", model="claude-haiku-5"))

    reply, plain, seen, _, requests = _timed_stream(make)
    assert reply == plain == "Seventeen times twenty-three is 391."
    assert [t for t, _ in seen] == PIECES and seen[0][1] < seen[-1][1] - 0.4
    assert requests[0]["stream"] is True and not requests[1].get("stream")


def test_a_server_that_ignores_stream_still_answers():
    async def go():
        async with FakeProvider(handler=lambda r: openai_reply("whole answer"), streams=False) as fake:
            agent = HttpAgent(AgentConfig(name="a", label="A", type="http", url=fake.openai_url, token="sk", model="m"))
            await agent.connect()
            got = []

            async def on_text(text):
                got.append(text)

            try:
                return await agent.send_and_receive("q", on_text=on_text), got
            finally:
                await agent.disconnect()

    assert _run(go()) == ("whole answer", [])


# A stand-in for Claude Code's --output-format stream-json --include-partial-messages
_CLAUDE_STREAM = """
import json, sys, time
def say(event):
    print(json.dumps(event), flush=True)
say({"type": "system", "subtype": "init", "tools": []})
for piece in ["Seventeen ", "times twenty-three ", "is 391."]:
    time.sleep(0.3)
    say({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                           "delta": {"type": "text_delta", "text": piece}}})
say({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                       "delta": {"type": "thinking_delta", "thinking": "hidden"}}})
say({"type": "assistant", "message": {"content": [{"type": "text", "text": "x" * 200000}]}})
say({"type": "result", "subtype": "success", "is_error": %s, "result": %s})
"""


def _claude_stream(is_error="False", result='"Seventeen times twenty-three is 391."'):
    return _cli(_CLAUDE_STREAM % (is_error, result), stdout_format="claude-stream-json", prompt_via="arg")


def test_claude_stream_json_is_passed_on_as_it_is_written():
    async def go():
        agent = _claude_stream()
        await agent.connect()
        loop, seen = asyncio.get_running_loop(), []
        started = loop.time()

        async def on_text(text):
            seen.append((text, loop.time() - started))

        return await agent.send_and_receive("q", on_text=on_text), seen

    answer, seen = _run(go())
    assert answer == "Seventeen times twenty-three is 391."  # the result event, not the long line before it
    assert [t for t, _ in seen] == ["Seventeen ", "times twenty-three ", "is 391."]  # no thinking
    assert seen[0][1] < seen[-1][1] - 0.4


def test_claude_stream_json_without_a_result_uses_the_pieces():
    assert _run(_ask_cli(_claude_stream(result="None"))) == "Seventeen times twenty-three is 391."


def test_claude_stream_json_error_result_is_an_error():
    with pytest.raises(RuntimeError, match="Claude Code reported an error: API Error: 401"):
        _run(_ask_cli(_claude_stream(is_error="True", result='"API Error: 401 unauthorized"')))


def test_plain_text_clis_ignore_on_text():
    async def go():
        agent = _cli("print('plain answer')")
        await agent.connect()
        got = []

        async def on_text(text):
            got.append(text)

        return await agent.send_and_receive("q", on_text=on_text), got

    assert _run(go()) == ("plain answer", [])


def test_unknown_stdout_format_is_reported():
    from ixel_mat.config.loader import build_agent_configs
    configs, warnings = build_agent_configs({"agents": {"x": {
        "type": "oneshot", "command": "x", "label": "X", "stdout_format": "xml"}}})
    assert configs["x"].stdout_format == "text" and "stdout_format" in warnings[0]


def test_opencode_never_fetches_its_model_catalog_whatever_an_agents_own_settings_say(monkeypatch):
    # The owner's choice: OpenCode uses the model list it has. An agent's own env, one with no preset, and one run
    # as a terminal (subprocess) all get the setting, and the lockdown with it
    from ixel_mat.agents.oneshot import cli_env
    from ixel_mat.agents.subprocess import SubprocessAgent
    from ixel_mat.config.loader import build_agent_configs
    from ixel_mat.presets import PRESETS_BY_ID
    monkeypatch.setenv("OPENCODE_DISABLE_MODELS_FETCH", "0")
    lockdown = PRESETS_BY_ID["opencode"]["env"]["OPENCODE_CONFIG_CONTENT"]
    assert PRESETS_BY_ID["opencode"]["env"]["OPENCODE_DISABLE_MODELS_FETCH"] == "1"
    configs, warnings = build_agent_configs({"agents": {
        "preset": {"preset": "opencode", "env": {"OPENCODE_DISABLE_MODELS_FETCH": "0", "OPENCODE_CONFIG_CONTENT": "{}",
                                                 "MINE": "x"}},
        "own": {"type": "oneshot", "command": "/usr/local/bin/opencode", "args": ["run"]},
        "windows": {"type": "oneshot", "command": "opencode.cmd", "env": {"OPENCODE_DISABLE_MODELS_FETCH": "false"}},
        "terminal": {"type": "subprocess", "command": "opencode"},
        "other": {"type": "oneshot", "command": "claude"}}})
    for name in ("preset", "own", "windows"):
        env = cli_env(configs[name])
        assert (env["OPENCODE_DISABLE_MODELS_FETCH"], env["OPENCODE_CONFIG_CONTENT"]) == ("1", lockdown), name
    terminal = SubprocessAgent(configs["terminal"])._build_env()
    assert (terminal["OPENCODE_DISABLE_MODELS_FETCH"], terminal["OPENCODE_CONFIG_CONTENT"]) == ("1", lockdown)
    # Your own settings join the preset's rather than replacing them (OpenCode's own updates stay off)
    assert cli_env(configs["preset"])["MINE"] == "x"
    assert cli_env(configs["preset"])["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    # Another program is left as it is
    assert "OPENCODE_CONFIG_CONTENT" not in cli_env(configs["other"])
    assert cli_env(configs["other"])["OPENCODE_DISABLE_MODELS_FETCH"] == "0"
    # An env that tries is told it can't
    assert "Agent 'preset': Ixel always sets OPENCODE_CONFIG_CONTENT, OPENCODE_DISABLE_MODELS_FETCH for this " \
           "program, so its env can't change them" in warnings
    assert any(w.startswith("Agent 'windows': Ixel always sets OPENCODE_DISABLE_MODELS_FETCH") for w in warnings)
    assert not any("'own'" in w or "'other'" in w for w in warnings)


def test_opencode_without_its_preset_still_cant_answer_through_your_background_service(monkeypatch, tmp_path):
    # OpenCode 2 without --standalone hands the question to your background service, with your settings, or
    # starts one with Ixel's: refused for a one-shot agent and for one run as a terminal
    from ixel_mat.agents import oneshot
    from ixel_mat.agents.subprocess import SubprocessAgent
    from ixel_mat.config.loader import build_agent_configs
    fake = tmp_path / "opencode"
    fake.write_text(f"#!{sys.executable}\nopen({str(tmp_path / 'started')!r}, 'w')\n")
    fake.chmod(0o755)
    if os.name == "nt":  # Windows finds a program by name only as a .exe, .cmd or the like
        (tmp_path / "opencode.cmd").write_text(f'@"{sys.executable}" "{fake}" %*\r\n', encoding="utf-8")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    configs, _ = build_agent_configs({"agents": {
        "own": {"type": "oneshot", "command": "/usr/local/bin/opencode", "args": ["run"]},
        "fine": {"type": "oneshot", "command": "opencode", "args": ["run", "--standalone"]},
        "terminal": {"type": "subprocess", "command": "opencode"}}})

    async def version(command, env=None):
        return "2"

    monkeypatch.setattr(oneshot, "major_version", version)
    with pytest.raises(RuntimeError, match='needs --standalone in its args.*use preset = "opencode"'):
        _run(oneshot.OneShotAgent(configs["own"])._args({}))
    assert _run(oneshot.OneShotAgent(configs["fine"])._args({})) == ["run", "--standalone"]
    with pytest.raises(RuntimeError, match="needs --standalone in its args"):
        _run(SubprocessAgent(configs["terminal"]).connect())
    assert not (tmp_path / "started").exists()


# ── Out of usage ──────────────────────────────────────────────────────────────

INSUFFICIENT_QUOTA = (429, {"error": {"message": "You exceeded your current quota, please check your plan and "
                                                  "billing details.", "type": "insufficient_quota"}}, {})
GOOGLE_PER_MINUTE = (429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                     "message": "You exceeded your current quota. Quota exceeded for metric: "
                                                "generate_content_free_tier_requests, limit: 10 per minute"}},
                     {"Retry-After": "0"})


def test_an_api_that_says_the_quota_is_used_up_is_not_asked_again():
    from ixel_mat.limits import UsageLimit

    async def go():
        reply = INSUFFICIENT_QUOTA
        async with FakeProvider(handler=lambda r: reply) as fake:
            with pytest.raises(UsageLimit, match="API 429: .*current quota"):
                await _ask_http(fake.openai_url, "gpt-x")
            return len(fake.requests)

    assert _run(go()) == 1  # no retries: waiting won't bring the quota back


def test_kimis_usage_limit_comes_as_a_403_and_still_counts():
    from ixel_mat.limits import UsageLimit

    async def go():
        reply = error_reply(403, "You've reached your 5-hour usage limit. Please try again later.")
        async with FakeProvider(handler=lambda r: reply) as fake:
            with pytest.raises(UsageLimit):
                await _ask_http(fake.openai_url, "k3")
            return len(fake.requests)

    assert _run(go()) == 1


def test_googles_per_minute_quota_still_gets_its_retries():
    replies = iter([GOOGLE_PER_MINUTE, openai_reply("391")])

    async def go():
        async with FakeProvider(handler=lambda r: next(replies)) as fake:
            return await _ask_http(fake.openai_url, "gemini-x"), len(fake.requests)

    assert _run(go()) == (["391"], 2)


def test_googles_quota_that_stays_used_up_is_out_of_usage():
    from ixel_mat.limits import UsageLimit

    async def go():
        async with FakeProvider(handler=lambda r: GOOGLE_PER_MINUTE) as fake:
            with pytest.raises(UsageLimit, match="RESOURCE_EXHAUSTED"):
                await _ask_http(fake.openai_url, "gemini-x")
            return len(fake.requests)

    assert _run(go()) == 3


def test_payment_required_is_out_of_usage():
    from ixel_mat.limits import UsageLimit

    async def go():
        async with FakeProvider(handler=lambda r: error_reply(402, "Payment required")) as fake:
            with pytest.raises(UsageLimit, match="API 402"):
                await _ask_http(fake.openai_url, "some/model")
            return len(fake.requests)

    assert _run(go()) == 1


def test_still_rate_limited_after_the_retries_is_out_of_usage_for_now():
    from ixel_mat.limits import UsageLimit

    async def go():
        async with FakeProvider(handler=lambda r: error_reply(429, "slow down", {"Retry-After": "0"})) as fake:
            with pytest.raises(UsageLimit, match="API 429: .*slow down"):
                await _ask_http(fake.openai_url, "gpt-x")
            return len(fake.requests)

    assert _run(go()) == 3  # a moment's rate limit still gets its retries first


def test_other_api_failures_are_not_out_of_usage():
    from ixel_mat.limits import UsageLimit, out_of_usage

    async def go():
        async with FakeProvider(handler=lambda r: error_reply(503, "down", {"Retry-After": "0"})) as fake:
            with pytest.raises(RuntimeError) as failure:
                await _ask_http(fake.openai_url, "gpt-x")
            return failure.value

    failure = _run(go())
    assert not isinstance(failure, UsageLimit) and not out_of_usage(str(failure))


def test_anthropic_credits_that_ran_out_are_out_of_usage(anthropic_to_fake):
    from ixel_mat.limits import UsageLimit

    async def go():
        reply = error_reply(400, "Your credit balance is too low to access the Anthropic API.")
        async with FakeProvider(handler=lambda r: reply) as fake:
            anthropic_to_fake["port"] = fake.port
            with pytest.raises(UsageLimit, match="API 400: .*credit balance"):
                await _ask_http("https://api.anthropic.com/v1/messages", "claude-sonnet-5")
            return len(fake.requests)

    assert _run(go()) == 1


def test_anthropic_still_rate_limited_after_the_sdks_retries_is_out_of_usage(anthropic_to_fake):
    from ixel_mat.limits import UsageLimit

    async def go():
        reply = error_reply(429, "slow down", {"Retry-After": "0"})
        async with FakeProvider(handler=lambda r: reply) as fake:
            anthropic_to_fake["port"] = fake.port
            with pytest.raises(UsageLimit, match="API 429"):
                await _ask_http("https://api.anthropic.com/v1/messages", "claude-sonnet-5")
            return len(fake.requests)

    assert _run(go()) == 3  # the SDK's own retries came first


def test_a_cli_that_says_its_out_of_usage_raises_usage_limit():
    from ixel_mat.limits import UsageLimit
    script = "import sys; sys.stderr.write(\"ERROR: You've hit your usage limit. Try again in 2 days.\\n\"); sys.exit(1)"
    with pytest.raises(UsageLimit, match="exited with code 1: ERROR: You've hit your usage limit"):
        _run(_ask_cli(_cli(script, prompt_via="stdin")))


def test_claude_codes_usage_limit_in_its_result_raises_usage_limit():
    from ixel_mat.limits import UsageLimit
    with pytest.raises(UsageLimit, match="Claude Code reported an error: You've hit your session limit"):
        _run(_ask_cli(_claude_stream(is_error="True", result='"You\'ve hit your session limit · resets 12pm"')))


_CODEX_LIKE = """import sys
prompt = sys.stdin.read()
sys.stderr.write("OpenAI Codex\\n--------\\nuser\\n" + prompt + "\\n")  # Codex repeats the prompt on stderr
sys.stderr.write(%r)
sys.exit(1)
"""


def test_a_prompt_that_mentions_limits_doesnt_make_a_cli_failure_out_of_usage():
    from ixel_mat.limits import UsageLimit
    agent = _cli(_CODEX_LIKE % "ERROR: unexpected status 401 Unauthorized: your refresh token has expired\n",
                 prompt_via="stdin")
    with pytest.raises(RuntimeError, match="401 Unauthorized") as failure:
        _run(_ask_cli(agent, "What happens when a user hits their daily limit, or the usage limit?"))
    assert not isinstance(failure.value, UsageLimit)


def test_a_cli_that_repeats_the_prompt_and_is_out_of_usage_still_says_so():
    from ixel_mat.limits import UsageLimit
    agent = _cli(_CODEX_LIKE % "ERROR: You've hit your usage limit. Try again in 2 days.\n", prompt_via="stdin")
    with pytest.raises(UsageLimit):
        _run(_ask_cli(agent, "What is 17 x 23?"))


def test_a_partial_answer_that_mentions_limits_isnt_out_of_usage():
    from ixel_mat.limits import UsageLimit
    agent = _cli("import sys; print('When a user hits the daily limit, the app'); sys.exit(1)", prompt_via="stdin")
    with pytest.raises(RuntimeError) as failure:
        _run(_ask_cli(agent))
    assert not isinstance(failure.value, UsageLimit)


def test_claude_code_failing_mid_answer_about_limits_isnt_out_of_usage():
    from ixel_mat.limits import UsageLimit
    script = (_CLAUDE_STREAM % ("True", "None")).replace(
        '"subtype": "success"', '"subtype": "error_during_execution", "errors": ["boom"]')
    script = script.replace("Seventeen times twenty-three is 391.", "When a user hits the daily limit, the app")
    with pytest.raises(RuntimeError, match="Claude Code reported an error") as failure:
        _run(_ask_cli(_cli(script, stdout_format="claude-stream-json", prompt_via="arg")))
    assert not isinstance(failure.value, UsageLimit)


def test_attached_code_with_blank_indented_lines_is_still_taken_out_of_the_error():
    # stderr is read the way a terminal shows it, which turns "    " lines into empty ones
    from ixel_mat.limits import UsageLimit
    agent = _cli(_CODEX_LIKE % "ERROR: stream disconnected before completion\n", prompt_via="stdin")
    prompt = "def f():\n    x = 1\n    \n    return x\n\nWhy does it say: You have hit your usage limit?"
    with pytest.raises(RuntimeError, match="stream disconnected") as failure:
        _run(_ask_cli(agent, prompt))
    assert not isinstance(failure.value, UsageLimit)


def test_codexs_reasoning_before_its_error_isnt_read_as_the_error():
    from ixel_mat.limits import UsageLimit
    agent = _cli(_CODEX_LIKE % "thinking\nThe user is asking what happens at the daily limit.\n"
                               "ERROR: stream disconnected before completion\n", prompt_via="stdin")
    with pytest.raises(RuntimeError, match="stream disconnected") as failure:
        _run(_ask_cli(agent, "Explain it"))
    assert not isinstance(failure.value, UsageLimit)
