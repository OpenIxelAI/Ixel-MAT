"""Setup wizard: live model lists, command-line agent presets, review defaults, config output."""
import io
import json

import pytest

from ixel_mat.agents.base import AgentConfig
from ixel_mat.agents.oneshot import OneShotAgent
from ixel_mat.config import setup as wizard
from ixel_mat.config.loader import build_agent_configs, tomllib
from ixel_mat.presets import PRESETS_BY_ID


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def serve_json(monkeypatch, payload_by_url_part):
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req)
        for part, payload in payload_by_url_part.items():
            if part in req.full_url:
                return FakeResponse(json.dumps(payload).encode())
        raise OSError("offline")

    monkeypatch.setattr(wizard, "_urlopen", fake_urlopen)
    return seen


def provider(pid):
    return next(p for p in wizard.PROVIDERS if p["id"] == pid)


def test_openai_models_are_filtered_to_chat_models_newest_first(monkeypatch):
    serve_json(monkeypatch, {"api.openai.com/v1/models": {"data": [
        {"id": "gpt-4o"}, {"id": "text-embedding-3-large"}, {"id": "gpt-5"}, {"id": "o3"},
        {"id": "gpt-4o-realtime-preview"}, {"id": "dall-e-3"}, {"id": "gpt-5-mini"}, {"id": "whisper-1"}]}})
    assert wizard.list_models(provider("openai"), "sk") == ["o3", "gpt-5-mini", "gpt-5", "gpt-4o"]


def test_gemini_models_come_without_prefix_and_only_if_they_generate(monkeypatch):
    seen = serve_json(monkeypatch, {"generativelanguage.googleapis.com": {"models": [
        {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/gemini-3-pro", "supportedGenerationMethods": ["generateContent", "countTokens"]},
        {"name": "models/gemini-2.5-flash-preview-tts", "supportedGenerationMethods": ["generateContent"]}]}})
    assert wizard.list_models(provider("gemini"), "AIza-key") == ["gemini-3-pro", "gemini-2.5-pro"]
    assert "AIza-key" not in seen[0].full_url  # key goes in a header


def test_anthropic_keeps_api_order(monkeypatch):
    serve_json(monkeypatch, {"api.anthropic.com/v1/models": {"data": [
        {"id": "claude-opus-5"}, {"id": "claude-sonnet-5"}, {"id": "claude-haiku-4-5"}]}})
    assert wizard.list_models(provider("anthropic"), "sk-ant") == ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]


def test_model_listing_failures_fall_back_quietly(monkeypatch):
    serve_json(monkeypatch, {})
    assert wizard.list_models(provider("xai"), "k") == []


def test_no_hardcoded_model_lists_remain():
    # Models come live from the provider; a built-in list would go stale
    assert not any("models" in p or "default_model" in p for p in wizard.PROVIDERS)


def test_model_question_defaults_to_latest_and_rejects_flags(monkeypatch):
    answers = iter(["--evil", "", ])
    monkeypatch.setattr(wizard.Prompt, "ask", lambda *a, **k: next(answers) or k.get("default"))
    assert wizard._ask_model(["gpt-5.5"]) == "latest"
    monkeypatch.setattr(wizard.Prompt, "ask", lambda *a, **k: "gpt-5.5-mini")
    assert wizard._ask_model(["gpt-5.5", "gpt-5.5-mini"]) == "gpt-5.5-mini"


def test_aliases_are_explained_with_what_they_mean_today(capsys):
    wizard._explain_aliases("openai", ["gpt-5.5", "gpt-5.5-mini", "gpt-5"])
    out = capsys.readouterr().out
    assert "latest = gpt-5.5 today" in out and "latest-fast = gpt-5.5-mini today" in out


# ── Command-line agent presets ────────────────────────────────────────────────

def test_cli_presets_are_answer_only_and_never_bypass_safety():
    for preset in wizard.CLI_PRESETS:
        joined = " ".join(preset["args"])
        assert "dangerously" not in joined and "bypass" not in joined.lower()
    claude = next(p for p in wizard.CLI_PRESETS if p["id"] == "claude_code")
    assert claude["args"][claude["args"].index("--tools") + 1] == ""  # all tools off
    assert "--strict-mcp-config" in claude["args"] and claude["prompt_via"] == "stdin"
    # Hooks (yours, or a plugin's) can't add to a panel member's prompt
    assert json.loads(claude["args"][claude["args"].index("--settings") + 1]) == {"disableAllHooks": True}
    assert "ANTHROPIC_API_KEY" in claude["drop_env"]  # bill the subscription, not a stray key
    codex = next(p for p in wizard.CLI_PRESETS if p["id"] == "codex")
    assert codex["args"][codex["args"].index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in codex["args"] and codex["prompt_via"] == "stdin" and codex["output_flag"] == "-o"
    disabled = {codex["args"][i + 1] for i, a in enumerate(codex["args"]) if a == "--disable"}
    assert {"shell_tool", "unified_exec"} <= disabled  # read-only still lets a shell read your files
    gemini = next(p for p in wizard.CLI_PRESETS if p["id"] == "gemini_cli")
    assert gemini["args"][gemini["args"].index("--approval-mode") + 1] == "plan" and "--skip-trust" in gemini["args"]
    copilot = next(p for p in wizard.CLI_PRESETS if p["id"] == "copilot")
    assert "--available-tools=ixel_none" in copilot["args"] and "COPILOT_ALLOW_ALL" in copilot["drop_env"]
    opencode = next(p for p in wizard.CLI_PRESETS if p["id"] == "opencode")
    lockdown = json.loads(opencode["env"]["OPENCODE_CONFIG_CONTENT"])
    assert lockdown["agent"]["ixel"]["tools"] == {"*": False} and opencode["args"][-2:] == ["--agent", "ixel"]
    # OpenCode 2's background service never sees the lockdown, so each run starts a server of its own;
    # OpenCode 1 has no such flag (or service)
    assert "--standalone" in opencode["args"] and opencode["args_by_version"]["1"][-2:] == ["--agent", "ixel"]
    assert "--standalone" not in opencode["args_by_version"]["1"]
    # A prompt is never a positional argument that a CLI could parse as a flag
    assert all(p["prompt_via"] in ("arg", "stdin", "auto") for p in wizard.CLI_PRESETS)


def test_claude_code_preset_puts_the_prompt_after_double_dash():
    claude = next(p for p in wizard.CLI_PRESETS if p["id"] == "claude_code")
    agent = OneShotAgent(AgentConfig(name="cc", label="Claude Code", type="oneshot", command="claude",
                                     args=claude["args"], prompt_via="arg"))
    cmd, stdin = agent._build_command("--dangerously-skip-permissions please")
    assert cmd[-2:] == ["--", "--dangerously-skip-permissions please"] and stdin is None


def test_cli_agents_offered_only_when_installed(monkeypatch):
    monkeypatch.setattr(wizard, "find_on_path", lambda cmd: "/usr/bin/claude" if cmd == "claude" else None)
    monkeypatch.setattr(wizard.Confirm, "ask", lambda *a, **k: True)
    monkeypatch.setattr(wizard.Prompt, "ask", lambda *a, **k: "")  # Enter: the CLI's own default model
    agents = wizard._configure_cli_agents(set())
    assert [a["id"] for a in agents] == ["claude_code"]
    assert agents[0]["type"] == "oneshot" and agents[0]["preset"] == "claude_code"
    assert "why" not in agents[0] and "model_hint" not in agents[0] and "model" not in agents[0]
    assert wizard._configure_cli_agents({"claude_code"}) == []


def test_cli_agent_model_can_be_chosen(monkeypatch):
    monkeypatch.setattr(wizard, "find_on_path", lambda cmd: "/usr/bin/claude" if cmd == "claude" else None)
    monkeypatch.setattr(wizard.Confirm, "ask", lambda *a, **k: True)
    answers = iter(["--dangerously-skip-permissions", "opus"])
    monkeypatch.setattr(wizard.Prompt, "ask", lambda *a, **k: next(answers))
    agent = wizard._configure_cli_agents(set())[0]
    assert agent["model"] == "opus"
    configs, _ = build_agent_configs(tomllib.loads(wizard._build_toml([agent])))
    cmd, stdin = OneShotAgent(configs["claude_code"])._build_command("q")
    assert cmd[cmd.index("--model") + 1] == "opus" and "q" not in cmd and stdin == b"q"


def test_small_panels_hear_about_free_members(monkeypatch):
    from rich.console import Console

    def tips(agents, installed):
        buf = io.StringIO()
        monkeypatch.setattr(wizard, "console", Console(file=buf, width=200))
        monkeypatch.setattr(wizard, "find_on_path", lambda cmd: f"/usr/bin/{cmd}" if cmd in installed else None)
        wizard._suggest_more_members(agents)
        return buf.getvalue()

    two = [{"id": "codex", "preset": "codex"}, {"id": "claude_code", "preset": "claude_code"}]
    out = tips(two, {"codex", "claude"})
    assert "a disagreement is a tie" in out
    assert "npm install -g @google/gemini-cli" in out and "https://ollama.com" in out
    words = " ".join(out.split())  # the tip wraps
    assert "asks for Google (Gemini)" in words and "once to sign in" not in words  # no personal sign-in
    assert "@openai/codex" not in out and "@anthropic-ai/claude-code" not in out  # already on the panel
    assert "npm install -g @github/copilot" in out
    out = tips(two, {"codex", "claude", "gemini", "ollama"})
    assert "gemini-cli" not in out and "ollama pull" in out
    assert tips(two + [{"id": "llama", "url": "http://127.0.0.1:11434/v1/chat/completions"}], set()) == ""
    assert "nobody to review" in tips(two[:1], {"codex"})


def test_saver_for_typed_questions_needs_a_verifier():
    review = {"mode": "review", "plain_questions": "saver"}
    wizard._drop_saver_without_verifier(review, {})
    assert review == {"mode": "review"}
    for review, saver in [({"plain_questions": "saver"}, {"verifier": "opus"}),
                          ({"plain_questions": "saver", "moderator": "opus"}, {})]:
        wizard._drop_saver_without_verifier(review, saver)
        assert review["plain_questions"] == "saver"


def test_every_cli_preset_can_take_a_model():
    for preset in wizard.CLI_PRESETS:
        assert "{model}" in " ".join(preset["model_args"]) and preset["model_hint"], preset["id"]


def test_review_defaults_step(monkeypatch):
    answers = iter(["deep", "quick", "claude"])
    monkeypatch.setattr(wizard.Prompt, "ask", lambda *a, **k: next(answers))
    assert wizard._configure_review([{"id": "gpt"}, {"id": "claude"}]) == {"mode": "deep", "moderator": "claude"}
    assert wizard._configure_review([{"id": "gpt"}]) is None  # not asked
    answers = iter(["review", "compare", "auto"])
    monkeypatch.setattr(wizard.Prompt, "ask", lambda *a, **k: next(answers))
    review = wizard._configure_review([{"id": "gpt"}, {"id": "claude"}])
    assert review == {"mode": "review", "plain_questions": "compare"}
    parsed = tomllib.loads(wizard._build_toml([], review))
    from ixel_mat.runtime import parse_review_settings
    assert parse_review_settings(parsed, set())[0].plain == "compare"


# ── Generated config round-trips through the loader ──────────────────────────

def test_generated_config_loads_back_exactly():
    codex = next(p for p in wizard.CLI_PRESETS if p["id"] == "codex")
    claude = next(p for p in wizard.CLI_PRESETS if p["id"] == "claude_code")
    agents = [
        {"id": "anthropic", "type": "http", "url": "https://api.anthropic.com/v1/messages",
         "token_env": "ANTHROPIC_API_KEY", "model": "claude-opus-5", "label": "Claude", "color": "blue"},
        {**{k: v for k, v in claude.items() if k != "why"}, "type": "oneshot", "workdir": "temp", "color": "white"},
        {**{k: v for k, v in codex.items() if k != "why"}, "type": "oneshot", "workdir": "temp", "color": "white"},
    ]
    parsed = tomllib.loads(wizard._build_toml(agents, {"mode": "deep", "moderator": "anthropic"}))
    assert parsed["review"] == {"mode": "deep", "moderator": "anthropic"}
    configs, warnings = build_agent_configs(parsed)
    assert not warnings
    cc = configs["claude_code"]
    assert cc.args == claude["args"] and cc.prompt_via == "stdin" and cc.call_timeout == 300
    cx = configs["codex"]
    assert (cx.command, cx.prompt_via, cx.output_flag, cx.workdir) == ("codex", "stdin", "-o", "temp")
    assert cx.args == codex["args"] and cx.effort_args == codex["effort_args"]
    assert configs["anthropic"].model == "claude-opus-5"


def test_every_cli_preset_round_trips_through_the_config_file():
    agents = [{**{k: v for k, v in p.items() if k != "why"}, "type": "oneshot", "workdir": "temp", "color": "white"}
              for p in wizard.CLI_PRESETS]
    configs, warnings = build_agent_configs(tomllib.loads(wizard._build_toml(agents)))
    assert not warnings
    for preset in wizard.CLI_PRESETS:
        cfg = configs[preset["id"]]
        for field in ("command", "args", "args_by_version", "prompt_via", "env", "drop_env", "effort_args",
                      "effort_levels", "model_args"):
            assert getattr(cfg, field) == (preset.get(field) or getattr(AgentConfig("x", "x", "oneshot"), field)), \
                (preset["id"], field)


def test_bad_cli_settings_are_reported_not_used():
    configs, warnings = build_agent_configs({"agents": {"x": {
        "type": "oneshot", "command": "x", "label": "X", "env": {"BAD NAME": "v"}, "drop_env": "OPENAI_API_KEY",
        "effort_args": [1]}}})
    assert configs["x"].env is None and configs["x"].drop_env is None and configs["x"].effort_args is None
    assert len(warnings) == 3


def test_prompts_too_long_for_a_command_line_fail_clearly():
    agent = OneShotAgent(AgentConfig(name="cc", label="CC", type="oneshot", command="claude", prompt_via="arg"))
    with pytest.raises(RuntimeError, match="too long to pass as a command-line argument"):
        agent._build_command("x" * 200_000)
    stdin_agent = OneShotAgent(AgentConfig(name="cx", label="CX", type="oneshot", command="codex", prompt_via="stdin"))
    assert stdin_agent._build_command("x" * 200_000)[1] == b"x" * 200_000


def test_every_preset_takes_a_review_round_with_code_in_it():
    """Code to review goes into every round's prompt, beside the other models' answers: no preset may
    need that on its command line (OpenCode used to, and dropped out of code reviews)."""
    from ixel_mat.material import MAX_MATERIAL_CHARS
    from ixel_mat.presets import PRESET_ABOUT
    prompt = "x" * (3 * MAX_MATERIAL_CHARS)
    for preset in wizard.CLI_PRESETS:
        fields = {k: v for k, v in preset.items() if k not in PRESET_ABOUT}
        agent = OneShotAgent(AgentConfig(name=preset["id"], type="oneshot", **fields))
        cmd, stdin = agent._build_command(prompt)
        assert stdin == prompt.encode() and prompt not in cmd, preset["id"]
    for preset in wizard.CLI_PRESETS:  # a short one too: a command line shows in `ps` to everyone here
        fields = {k: v for k, v in preset.items() if k not in PRESET_ABOUT}
        cmd, stdin = OneShotAgent(AgentConfig(name=preset["id"], type="oneshot", **fields))._build_command("17 x 23?")
        assert stdin == b"17 x 23?" and "17 x 23?" not in cmd, preset["id"]



# ── Local models and the usage saver step ─────────────────────────────────────

import asyncio

from fake_providers import FakeProvider
from ixel_mat.runtime import SaverSettings, parse_saver_settings


def test_local_servers_are_detected(monkeypatch):
    async def go():
        async with FakeProvider(models=["llama3.2", "nomic-embed-text", "qwen3:14b"]) as fake:
            monkeypatch.setattr(wizard, "LOCAL_SERVERS", [("Ollama", f"http://127.0.0.1:{fake.port}/v1"),
                                                          ("LM Studio", "http://127.0.0.1:9/v1")])
            return await asyncio.to_thread(wizard.detect_local_servers), fake.port

    found, port = asyncio.run(go())
    assert found == [("Ollama", f"http://127.0.0.1:{port}/v1", ["llama3.2", "qwen3:14b"])]


def test_local_models_are_added_as_keyless_agents(monkeypatch):
    monkeypatch.setattr(wizard, "detect_local_servers",
                        lambda: [("Ollama", "http://127.0.0.1:11434/v1", ["llama3.2", "qwen3:14b"])])
    confirms = iter([True, True, False, False])  # two models, no more, and no other computer
    models = iter(["llama3.2", "qwen3:14b"])
    monkeypatch.setattr(wizard.Confirm, "ask", lambda *a, **k: next(confirms))
    monkeypatch.setattr(wizard.Prompt, "ask", lambda *a, **k: next(models))
    agents = wizard._configure_local_agents({"llama3_2"})
    assert [(a["id"], a["model"], a["url"]) for a in agents] == [
        ("llama3_2_2", "llama3.2", "http://127.0.0.1:11434/v1/chat/completions"),
        ("qwen3_14b", "qwen3:14b", "http://127.0.0.1:11434/v1/chat/completions")]
    assert all("token_env" not in a for a in agents)


def test_saver_step_writes_a_saver_section():
    answers = iter(["opus", "disagreement", "low"])
    import unittest.mock as mock
    with mock.patch.object(wizard.Prompt, "ask", lambda *a, **k: next(answers)):
        saver = wizard._configure_saver([{"id": "opus"}, {"id": "haiku"}])
    assert saver == {"verifier": "opus", "escalate": "disagreement", "verifier_effort": "low"}
    agents = [
        {"id": "opus", "type": "http", "url": "https://api.anthropic.com/v1/messages", "token_env": "K",
         "model": "claude-opus-5", "label": "Opus", "color": "blue"},
        {"id": "llama", "type": "http", "url": "http://127.0.0.1:11434/v1/chat/completions",
         "model": "llama3.2", "label": "llama3.2 (local)", "color": "yellow"},
    ]
    parsed = tomllib.loads(wizard._build_toml(agents, None, saver))
    settings, warnings = parse_saver_settings(parsed, set(parsed["agents"]))
    assert settings == SaverSettings(verifier="opus", escalate="disagreement", verifier_effort="low")
    assert not warnings and "token_env" not in parsed["agents"]["llama"]


def test_saver_step_can_be_skipped():
    import unittest.mock as mock
    with mock.patch.object(wizard.Prompt, "ask", lambda *a, **k: "skip"):
        assert wizard._configure_saver([{"id": "a"}, {"id": "b"}]) == {}
    assert wizard._configure_saver([{"id": "a"}]) is None


def test_wizard_requests_do_not_follow_redirects():
    # urllib would re-send the Authorization header to wherever a redirect points
    import threading
    import urllib.error
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/v1/models",
                                     headers={"Authorization": "Bearer sk-test"})
        with pytest.raises(urllib.error.HTTPError) as err:
            wizard._get_json(req)
    finally:
        server.shutdown()
        server.server_close()
    assert err.value.code == 302
    assert seen == [("/v1/models", "Bearer sk-test")]  # never re-sent to /elsewhere


def test_cli_agents_are_saved_as_preset_references(monkeypatch):
    # A reference, so a preset tightened later reaches configs written today
    monkeypatch.setattr(wizard, "find_on_path", lambda cmd: "/usr/bin/codex" if cmd == "codex" else None)
    monkeypatch.setattr(wizard.Confirm, "ask", lambda *a, **k: True)
    monkeypatch.setattr(wizard.Prompt, "ask", lambda *a, **k: "")
    agents = wizard._configure_cli_agents(set())
    text = wizard._build_toml(agents)
    assert 'preset = "codex"' in text and "--ignore-user-config" not in text
    configs, warnings = build_agent_configs(tomllib.loads(text))
    codex = next(p for p in wizard.CLI_PRESETS if p["id"] == "codex")
    assert not warnings and configs["codex"].args == codex["args"] and configs["codex"].workdir == "temp"


def test_preset_settings_can_be_overridden_and_unknown_presets_are_reported():
    configs, warnings = build_agent_configs({"agents": {
        "cc": {"preset": "claude_code", "model": "opus", "timeout": 120, "label": "Mine"},
        "x": {"preset": "no-such-cli"}}})
    assert configs["cc"].model == "opus" and configs["cc"].call_timeout == 120 and configs["cc"].label == "Mine"
    assert configs["cc"].command == "claude" and "x" not in configs
    assert any("unknown preset" in w for w in warnings)
    # Your own args are what runs, whichever version of the CLI is installed
    configs, _ = build_agent_configs({"agents": {"mine": {"preset": "opencode", "args": ["run"]},
                                                 "oc": {"preset": "opencode"}}})
    assert configs["mine"].args == ["run"] and configs["mine"].args_by_version is None
    assert configs["oc"].args_by_version == PRESETS_BY_ID["opencode"]["args_by_version"]
    # ...but they can't leave out what the lockdown needs, even by saying so
    configs, _ = build_agent_configs({"agents": {"mine": {"preset": "opencode", "args": ["run"],
                                                          "required_args": {"*": []}}}})
    assert configs["mine"].required_args == PRESETS_BY_ID["opencode"]["required_args"]


def test_claude_code_is_told_it_is_a_panel_member():
    # By default it's a coding assistant and prefaces other answers with a disclaimer
    claude = next(p for p in wizard.CLI_PRESETS if p["id"] == "claude_code")
    prompt = claude["args"][claude["args"].index("--append-system-prompt") + 1]
    assert "panel" in prompt and "--system-prompt" not in claude["args"]


def test_triage_is_off_unless_you_say_yes(monkeypatch):
    confirms = iter([False])
    monkeypatch.setattr(wizard.Confirm, "ask", lambda *a, **k: next(confirms))
    review = {"mode": "review"}
    assert wizard._configure_triage([{"id": "gpt"}, {"id": "claude"}], review) == {} and review == {"mode": "review"}
    assert wizard._configure_triage([{"id": "gpt"}], review) is None  # one model: nothing for it to decide


def test_triage_with_your_own_model_needs_no_account(monkeypatch):
    from ixel_mat.triage import parse_triage_settings
    from ixel_mat.runtime import parse_review_settings
    saved = {}
    monkeypatch.setattr(wizard, "save_secret", lambda k, v: saved.update({k: v}))
    confirms = iter([True, True, False])      # use triage · let it pick the mode · don't skip agreed reviews
    prompts = iter(["model", "haiku"])        # who decides · which model
    monkeypatch.setattr(wizard.Confirm, "ask", lambda *a, **k: next(confirms))
    monkeypatch.setattr(wizard.Prompt, "ask", lambda *a, **k: next(prompts))
    review = {"mode": "review"}
    triage = wizard._configure_triage([{"id": "opus"}, {"id": "haiku"}], review)
    assert triage == {"enabled": True, "provider": "model", "agent": "haiku", "skip_review": False}
    assert review == {"mode": "auto"} and saved == {}  # no key asked for, none saved

    parsed = tomllib.loads(wizard._build_toml([], review, None, triage))
    agents = {"haiku": AgentConfig(name="haiku", label="Haiku", type="http", url="https://api.anthropic.com",
                                   token="k")}
    settings, warnings = parse_triage_settings(parsed, agents)
    assert settings.ready and settings.provider == "model" and settings.via == "Haiku" and not warnings
    assert parse_review_settings(parsed, set())[0].auto


def test_triage_step_saves_the_key_privately_and_writes_a_triage_section(monkeypatch, tmp_path):
    from ixel_mat.triage import parse_triage_settings
    from ixel_mat.runtime import parse_review_settings
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    saved = {}
    monkeypatch.setattr(wizard, "save_secret", lambda k, v: saved.update({k: v}))
    monkeypatch.setattr(wizard, "_check_typesafe_key", lambda key: (True, "key works (ts-decision-1)"))
    confirms = iter([True, True, True])       # use triage · let it pick the mode · skip agreed reviews
    prompts = iter(["typesafe", " ts-pasted-key\n"])
    monkeypatch.setattr(wizard.Confirm, "ask", lambda *a, **k: next(confirms))
    monkeypatch.setattr(wizard.Prompt, "ask", lambda *a, **k: next(prompts))
    review = {"mode": "review"}
    triage = wizard._configure_triage([{"id": "gpt"}, {"id": "claude"}], review)
    assert triage == {"enabled": True, "provider": "typesafe", "skip_review": True} and review == {"mode": "auto"}
    assert saved == {"TYPESAFE_API_KEY": "ts-pasted-key"}

    text = wizard._build_toml([], review, None, triage)
    assert "ts-pasted-key" not in text  # the key lives only in the private .env file
    parsed = tomllib.loads(text)
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-pasted-key")
    settings, warnings = parse_triage_settings(parsed)
    assert settings.ready and settings.skip_review and settings.official and not warnings
    assert parse_review_settings(parsed, set())[0].auto


def test_triage_key_check_sends_no_questions(monkeypatch):
    seen = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            return b'{"models": [{"name": "ts-decision-1"}]}'

    def fake_open(req, timeout):
        seen.append((req.full_url, req.get_method(), req.data, req.get_header("Authorization")))
        return Resp()

    monkeypatch.setattr(wizard, "_urlopen", fake_open)
    ok, message = wizard._check_typesafe_key("ts-key")
    assert ok and "ts-decision-1" in message
    assert seen == [("https://api.typesafe.ai/v1/models", "GET", None, "Bearer ts-key")]


# ── Models on another of your computers ───────────────────────────────────────

from ixel_mat import local_models


def test_models_on_another_computer_can_join_by_its_address(monkeypatch):
    monkeypatch.setattr(wizard, "detect_local_servers", lambda: [])
    looked = []

    def at_address(text):
        looked.append(text)
        if text == "typo":
            raise local_models.AddressError("Ixel can't find a computer called typo.")
        return [local_models.Server("LM Studio", "http://mac-mini:1234/v1", ["qwen3:14b", "gemma3:12b"])]

    monkeypatch.setattr(wizard.local_models, "at_address", at_address)
    confirms = iter([True, True, True, False, False])  # another computer, (typo), add one, no more, no other
    prompts = iter(["typo", "mac-mini", "qwen3:14b"])
    monkeypatch.setattr(wizard.Confirm, "ask", lambda *a, **k: next(confirms))
    monkeypatch.setattr(wizard.Prompt, "ask", lambda *a, **k: next(prompts))
    [agent] = wizard._configure_local_agents(set())
    assert looked == ["typo", "mac-mini"]
    assert agent == {"id": "qwen3_14b", "type": "http", "url": "http://mac-mini:1234/v1/chat/completions",
                     "model": "qwen3:14b", "label": "qwen3:14b (mac-mini)", "color": "yellow", "_asked": True}


def test_an_ollama_with_only_cloud_models_says_why_none_are_offered(monkeypatch):
    from rich.console import Console
    buf = io.StringIO()
    monkeypatch.setattr(wizard, "console", Console(file=buf, width=300))
    monkeypatch.setattr(wizard, "detect_local_servers", lambda: [])
    monkeypatch.setattr(wizard.local_models, "at_address", lambda text: [
        local_models.Server("Ollama", "http://mac-mini:11434/v1", [], [], ollama=True, elsewhere=["gpt-oss:120b-cloud"])])
    confirms = iter([True, False])
    monkeypatch.setattr(wizard.Confirm, "ask", lambda *a, **k: next(confirms))
    monkeypatch.setattr(wizard.Prompt, "ask", lambda *a, **k: "mac-mini")
    assert wizard._configure_local_agents(set()) == []
    assert "has only models that run on ollama.com (gpt-oss:120b-cloud)" in buf.getvalue()


def test_more_servers_on_this_computer_are_looked_for():
    ports = {int(base.rsplit(":", 1)[1].split("/")[0]) for _, base in wizard.LOCAL_SERVERS}
    assert {11434, 1234, 8080, 8000, 1337} <= ports
    assert wizard.LOCAL_SERVERS[0] == ("Ollama", "http://127.0.0.1:11434/v1")
    assert all(base.startswith("http://127.0.0.1:") for _, base in wizard.LOCAL_SERVERS)


# ── Running setup again ───────────────────────────────────────────────────────

BEFORE = '''
top_level = "kept"

[agents.openai]
type = "http"
url = "https://api.openai.com/v1/chat/completions"
token_env = "OPENAI_API_KEY"
model = "gpt-6-astra"
effort = "high"
accepts = ["image"]
label = "OpenAI"
color = "green"

[agents.mac_qwen]
type = "http"
url = "http://mac-mini:1234/v1/chat/completions"
model = "qwen3:14b"
billing = "local"
label = "Qwen on the Mac"
color = "yellow"

[review]
mode = "review"
agents = ["openai", "mac_qwen", "gone"]
timeout = 300
moderator = "openai"

[saver]
verifier = "openai"
drafters = ["mac_qwen", "gone"]
on_wrong = "drop"

[triage]
enabled = true
provider = "model"
agent = "mac_qwen"
auto_mode = true

[images]
provider = "xai"

[sound]
provider = "groq"

[connections."mac-mini:3000"]
kind = "gitea"
url = "http://mac-mini:3000"

[pricing."qwen3:14b"]
input = 0.0
output = 0.0
'''

THIS_RUN = [
    {"id": "openai", "type": "http", "url": "https://api.openai.com/v1/chat/completions",
     "token_env": "OPENAI_API_KEY", "model": "latest", "label": "OpenAI", "color": "green"},
    {"id": "claude_code", "preset": "claude_code", "type": "oneshot", "label": "Claude Code", "color": "white"},
]


def rerun(monkeypatch, tmp_path, keep: bool, review=None, saver=None, triage=None, before=BEFORE):
    path = tmp_path / "config.toml"
    path.write_text(before, encoding="utf-8")
    monkeypatch.setattr(wizard, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(wizard, "_CONFIG_FILE", path)
    confirms = iter([keep, True])  # keep the others?, write it?
    monkeypatch.setattr(wizard.Confirm, "ask", lambda *a, **k: next(confirms))
    agents, existing = wizard.with_existing([dict(a) for a in THIS_RUN])
    assert [a["id"] for a in agents] == ["openai", "claude_code", *(["mac_qwen"] if keep else [])]
    wizard._print_summary_and_write(agents, review, saver, triage, existing)
    assert (tmp_path / "config.toml.bak").read_text(encoding="utf-8") == before
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_running_setup_again_keeps_what_it_didnt_ask_about(monkeypatch, tmp_path):
    after = rerun(monkeypatch, tmp_path, keep=True, review={"mode": "deep"}, saver={}, triage={})
    # The model added by hand is still there, with everything it had
    assert after["agents"]["mac_qwen"] == {"type": "http", "url": "http://mac-mini:1234/v1/chat/completions",
                                           "model": "qwen3:14b", "billing": "local", "label": "Qwen on the Mac",
                                           "color": "yellow"}
    # One set up again keeps its effort and pictures, and takes this run's model
    assert after["agents"]["openai"]["model"] == "latest"
    assert (after["agents"]["openai"]["effort"], after["agents"]["openai"]["accepts"]) == ("high", ["image"])
    # This run's answers, the rest of each table, an agent that's gone taken out, a new one on the panel
    assert after["review"] == {"mode": "deep", "agents": ["openai", "mac_qwen", "claude_code"], "timeout": 300}
    assert after["saver"] == {"drafters": ["mac_qwen"], "on_wrong": "drop"}
    assert after["triage"] == {"enabled": False, "auto_mode": True}  # no triage this time
    # Tables setup doesn't ask about at all
    assert after["images"] == {"provider": "xai"} and after["sound"] == {"provider": "groq"}
    assert after["connections"] == {"mac-mini:3000": {"kind": "gitea", "url": "http://mac-mini:3000"}}
    assert after["pricing"] == {"qwen3:14b": {"input": 0.0, "output": 0.0}}
    assert after["top_level"] == "kept"
    configs, warnings = build_agent_configs(after)
    assert set(configs) == {"openai", "claude_code", "mac_qwen"} and not warnings


def test_the_models_it_didnt_set_up_go_when_you_say_so(monkeypatch, tmp_path):
    after = rerun(monkeypatch, tmp_path, keep=False, review={"mode": "review", "moderator": "claude_code"},
                  saver={"verifier": "openai", "escalate": "always", "verifier_effort": "low"},
                  triage={"enabled": True, "provider": "model", "agent": "claude_code", "skip_review": True})
    assert set(after["agents"]) == {"openai", "claude_code"}
    assert after["review"] == {"mode": "review", "moderator": "claude_code", "agents": ["openai", "claude_code"],
                               "timeout": 300}
    assert after["saver"] == {"on_wrong": "drop", "verifier": "openai", "escalate": "always", "verifier_effort": "low"}
    assert after["triage"] == {"enabled": True, "provider": "model", "agent": "claude_code", "skip_review": True,
                               "auto_mode": True}


def test_with_one_model_nothing_is_asked_about_reviews_and_nothing_changes(monkeypatch, tmp_path):
    # One model this run (the others kept): the review, Saver and Triage steps aren't shown
    assert wizard._configure_review([THIS_RUN[0]]) is None and wizard._configure_saver([THIS_RUN[0]]) is None
    assert wizard._configure_triage([THIS_RUN[0]], None) is None
    after = rerun(monkeypatch, tmp_path, keep=True)
    assert after["review"] == {"mode": "review", "agents": ["openai", "mac_qwen", "claude_code"], "timeout": 300,
                               "moderator": "openai"}
    assert after["saver"] == {"verifier": "openai", "drafters": ["mac_qwen"], "on_wrong": "drop"}
    assert after["triage"] == {"enabled": True, "provider": "model", "agent": "mac_qwen", "auto_mode": True,
                               "skip_review": False}
    # A model that's gone stops being the one that decides or writes the verdict
    after = rerun(monkeypatch, tmp_path, keep=False)
    assert "moderator" in after["review"] and after["triage"] == {"enabled": False, "provider": "model",
                                                                   "auto_mode": True}


def test_settings_turned_off_stay_off(monkeypatch, tmp_path):
    """Pictures off (accepts = []) on a model set up again and on one kept as it was."""
    before = BEFORE.replace('accepts = ["image"]', 'accepts = []').replace('billing = "local"',
                                                                         'billing = "local"\naccepts = []')
    after = rerun(monkeypatch, tmp_path, keep=True, before=before)
    assert after["agents"]["openai"]["accepts"] == [] and after["agents"]["mac_qwen"]["accepts"] == []
    configs, _ = build_agent_configs(after)
    assert configs["openai"].accepts == [] and configs["mac_qwen"].accepts == []


def setup_again(monkeypatch, tmp_path, text, this_run, answers):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(wizard, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(wizard, "_CONFIG_FILE", path)
    answers = iter(answers)
    monkeypatch.setattr(wizard.Confirm, "ask", lambda *a, **k: next(answers))
    agents, existing = wizard.with_existing(this_run)
    wizard._print_summary_and_write(agents, existing=existing)
    return tomllib.loads(path.read_text(encoding="utf-8"))


LM_HERE = ('[agents.qwen3_14b]\ntype = "http"\nurl = "http://127.0.0.1:1234/v1/chat/completions"\n'
           'model = "qwen3:14b"\naccepts = ["image"]\nlabel = "Qwen here"\ncolor = "yellow"\n\n'
           '[review]\nagents = ["qwen3_14b"]\n')


def test_a_model_that_only_shares_a_name_doesnt_replace_the_one_in_the_file(monkeypatch, tmp_path):
    mac = {"id": "qwen3_14b", "type": "http", "url": "http://mac-mini:1234/v1/chat/completions",
           "model": "qwen3:14b", "label": "qwen3:14b (mac-mini)", "color": "yellow", "_asked": True}
    after = setup_again(monkeypatch, tmp_path, LM_HERE, [dict(mac)], [True, True])  # keep the other, write
    assert after["agents"]["qwen3_14b"]["label"] == "Qwen here"  # untouched
    assert after["agents"]["qwen3_14b_2"] == {k: v for k, v in mac.items() if k not in ("id", "_asked")}
    assert after["review"]["agents"] == ["qwen3_14b", "qwen3_14b_2"]
    # Said no to keeping it: only the new one, under its own name, with nothing of the old one's
    after = setup_again(monkeypatch, tmp_path, LM_HERE, [dict(mac)], [False, True])
    assert set(after["agents"]) == {"qwen3_14b_2"} and "accepts" not in after["agents"]["qwen3_14b_2"]
    assert after["review"]["agents"] == ["qwen3_14b_2"]


def test_a_model_set_up_again_keeps_its_own_name_when_two_in_the_file_match(monkeypatch, tmp_path):
    api = 'type = "http"\nurl = "https://api.openai.com/v1/chat/completions"\nmodel = "gpt-5"\n'
    text = (f'[agents.gpt_fast]\n{api}effort = "minimal"\nlabel = "GPT fast"\n\n'
            f'[agents.openai]\n{api}effort = "high"\nlabel = "OpenAI"\n')
    this_run = {**THIS_RUN[0], "model": "gpt-5"}
    after = setup_again(monkeypatch, tmp_path, text, [this_run], [True, True])
    assert after["agents"]["openai"]["effort"] == "high"
    assert after["agents"]["gpt_fast"] == {**tomllib.loads(text)["agents"]["gpt_fast"]}


TWO_QWENS = ('[agents.qwen3_8b]\ntype = "http"\nurl = "http://127.0.0.1:11434/v1/chat/completions"\n'
             'model = "qwen3:8b"\nlabel = "qwen3:8b (local)"\ncolor = "yellow"\n\n'
             '[agents.qwen3_8b_2]\ntype = "http"\nurl = "http://192.168.1.20:11434/v1/chat/completions"\n'
             'model = "qwen3:8b"\naccepts = ["image"]\neffort = "high"\nlabel = "qwen3:8b (192.168.1.20)"\n'
             'color = "yellow"\n\n[review]\nagents = ["qwen3_8b", "qwen3_8b_2"]\n')


def test_a_model_set_up_again_is_found_by_where_it_is_whatever_this_run_called_it(monkeypatch, tmp_path):
    # This computer's Ollama is off this time, so the other computer's qwen3:8b came first and got qwen3_8b
    there = {"id": "qwen3_8b", "type": "http", "url": "http://192.168.1.20:11434/v1/chat/completions",
             "model": "qwen3:8b", "label": "qwen3:8b (192.168.1.20)", "color": "yellow", "_asked": True}
    after = setup_again(monkeypatch, tmp_path, TWO_QWENS, [dict(there)], [False, True])  # don't keep the local one
    assert set(after["agents"]) == {"qwen3_8b_2"}
    assert (after["agents"]["qwen3_8b_2"]["accepts"], after["agents"]["qwen3_8b_2"]["effort"]) == (["image"], "high")
    assert after["review"]["agents"] == ["qwen3_8b_2"]
    # Both set up again in one run, and localhost is 127.0.0.1: each keeps its own name, neither is doubled
    here = {**there, "id": "qwen3_8b_3", "url": "http://localhost:11434/v1/chat/completions", "label": "qwen3:8b (local)"}
    after = setup_again(monkeypatch, tmp_path, TWO_QWENS, [dict(there), dict(here)], [True])  # nothing else to keep
    assert set(after["agents"]) == {"qwen3_8b", "qwen3_8b_2"}
    assert after["agents"]["qwen3_8b_2"]["effort"] == "high" and "effort" not in after["agents"]["qwen3_8b"]
    assert after["review"]["agents"] == ["qwen3_8b", "qwen3_8b_2"]


def test_a_note_of_your_own_in_a_models_settings_stays(monkeypatch, tmp_path):
    text = LM_HERE.replace('color = "yellow"\n', 'color = "yellow"\n_note = "the fast one"\n')
    after = setup_again(monkeypatch, tmp_path, text, [], [True, True])
    assert after["agents"]["qwen3_14b"]["_note"] == "the fast one"


def test_a_model_added_to_the_panel_again_rejoins_it_and_keeps_its_name_and_color(monkeypatch, tmp_path):
    text = ('[agents.claude_code]\npreset = "claude_code"\nlabel = "My Claude"\ncolor = "magenta"\n\n'
            '[agents.openai]\ntype = "http"\nurl = "https://api.openai.com/v1/chat/completions"\n'
            'token_env = "OPENAI_API_KEY"\nlabel = "OpenAI"\ncolor = "blue"\n\n'
            '[review]\nagents = ["claude_code"]\n')
    this_run = [{**THIS_RUN[0], "model": "gpt-6-astra"}, {**THIS_RUN[1], "_asked": True}]
    after = setup_again(monkeypatch, tmp_path, text, this_run, [True])
    assert (after["agents"]["claude_code"]["label"], after["agents"]["claude_code"]["color"]) == ("My Claude", "magenta")
    # An API model's name is the wizard's; it's taken off the panel in Settings, and stays off
    assert (after["agents"]["openai"]["label"], after["agents"]["openai"]["color"]) == ("OpenAI", "blue")
    assert after["review"]["agents"] == ["claude_code"]
    text = text.replace('agents = ["claude_code"]', 'agents = ["openai"]')
    after = setup_again(monkeypatch, tmp_path, text, [dict(a) for a in this_run], [True])
    assert after["review"]["agents"] == ["openai", "claude_code"]


def test_odd_characters_in_a_name_still_make_a_file_that_reads_back():
    from ixel_mat.config import edit
    for text in ("a\x7fb", 'quote " and \\ back', "tab\there", "é ✓"):
        assert tomllib.loads(f"x = {wizard._toml_str(text)}\n")["x"] == text
        assert tomllib.loads(f"x = {edit.toml_value(text)}\n")["x"] == text


def test_a_settings_file_that_cant_be_read_is_replaced_and_kept_as_the_backup(monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[agents.broken\n", encoding="utf-8")
    monkeypatch.setattr(wizard, "_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(wizard, "_CONFIG_FILE", path)
    monkeypatch.setattr(wizard.Confirm, "ask", lambda *a, **k: True)
    agents, existing = wizard.with_existing([dict(THIS_RUN[0])])
    assert existing is None
    wizard._print_summary_and_write(agents, existing=existing)
    assert set(tomllib.loads(path.read_text(encoding="utf-8"))["agents"]) == {"openai"}
    assert (tmp_path / "config.toml.bak").read_text(encoding="utf-8") == "[agents.broken\n"
    # And a copy that the next run's backup won't replace
    [aside] = tmp_path.glob("config.toml.unreadable-*")
    assert aside.read_text(encoding="utf-8") == "[agents.broken\n"
