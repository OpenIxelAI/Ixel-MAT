"""`ixel mcp`: the plugin for Claude Desktop / Claude Code / Codex / Cursor."""
import asyncio
import json
import os
import re
import site
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import mcp
import pytest
from mcp.client.stdio import StdioServerParameters

from fake_providers import ThreadedFakeProvider, panel_handler, panel_text
from ixel_mat import mcp_server
from ixel_mat.config import loader, secrets
from ixel_mat.mcp_server import build_server, format_result, host_snippets
from ixel_mat.modes.review import ReviewMode, ReviewResult
from ixel_mat.runtime import Settings

ANSWERS = {"m-gpt": "It's 391.", "m-claude": "17 × 23 = 391", "m-wrong": "The answer is 381."}


class Agent:
    def __init__(self, name, model, delay=0.0, answer=None):
        self.name, self.label, self.model, self.is_connected = name, name.title(), model, True
        self.delay, self.answer = delay, answer

    async def send_and_receive(self, message, **kwargs):
        await asyncio.sleep(self.delay)
        if self.answer is not None and "You are" not in message:
            return self.answer
        return panel_text(ANSWERS, self.model, message)


def fake_panel(agents=None, problems=(), settings=None):
    @asynccontextmanager
    async def panel(mode=None):
        yield settings or Settings({}, {}), agents if agents is not None else [
            Agent("gpt", "m-gpt"), Agent("claude", "m-claude"), Agent("gemini", "m-wrong")], list(problems)
    return panel


async def call_tool(server, tool, args=None, **kwargs):
    async with mcp.Client(server) as client:
        return await client.call_tool(tool, args or {}, **kwargs)


async def call(server, tool, args=None, **kwargs):
    return (await call_tool(server, tool, args, **kwargs)).content[0].text


def test_tool_annotations_are_honest():
    async def go():
        async with mcp.Client(build_server(fake_panel())) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in asyncio.run(go())}
    assert set(tools) == {"ixel_review", "ixel_panel"}
    # ixel_review spends model usage and records saves: a client may skip confirming read-only tools
    review, panel = tools["ixel_review"].annotations, tools["ixel_panel"].annotations
    assert review.read_only_hint is False and review.open_world_hint and not review.destructive_hint
    assert panel.read_only_hint and not panel.destructive_hint and not panel.open_world_hint
    mode = tools["ixel_review"].input_schema["properties"]["mode"]
    assert mode["anyOf"][0]["enum"] == ["quick", "review", "deep", "saver", "auto"]
    assert "ctx" not in tools["ixel_review"].input_schema["properties"]


def test_review_returns_verdict_scoreboard_and_answers():
    progress = []

    async def on_progress(done, total, message):
        progress.append((done, total, message))

    text = asyncio.run(call(build_server(fake_panel()), "ixel_review", {"question": "What is 17 × 23?"},
                            progress_callback=on_progress))
    assert text.startswith("The following was produced by other AI models")
    assert "## Verdict (review mode" in text and "17 × 23 = 391" in text
    assert "| Answer | Model | Peer score |" in text and "Gemini rated" in text
    assert "The correct result is 391." in text and "### " in text
    assert progress[-1] == (7, 7, "verdict ready")
    assert [p[0] for p in progress] == sorted(p[0] for p in progress)


def test_auto_mode_reports_how_the_mode_was_chosen(monkeypatch):
    # No Triage set up: auto runs the default mode, and the host model is told why
    text = asyncio.run(call(build_server(fake_panel()), "ixel_review", {"question": "What is 17 × 23?", "mode": "auto"}))
    assert "## Verdict (review mode" in text
    assert "**Triage**" in text and "Auto mode needs triage (it isn't set up)" in text
    assert "triage call" not in text  # nothing was sent

    # With Triage: it picks, and the run says so
    from fake_providers import typesafe_handler
    from ixel_mat.triage import parse_triage_settings

    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")

    def triage_panel(url):
        @asynccontextmanager
        async def panel(mode=None):
            triage, _ = parse_triage_settings({"triage": {"enabled": True, "url": url}})
            yield Settings({}, {}, triage=triage), [Agent("gpt", "m-gpt"), Agent("claude", "m-claude"),
                                              Agent("gemini", "m-wrong")], []
        return panel

    with ThreadedFakeProvider() as fake:
        fake.typesafe_handler = typesafe_handler(depth=0.1)
        text = asyncio.run(call(build_server(triage_panel(fake.typesafe_url)), "ixel_review",
                                {"question": "What is 17 × 23?", "mode": "auto"}))
    assert "## Verdict (quick mode" in text and "Triage picked quick mode" in text and "1 triage call" in text


def test_quick_mode_via_tool_argument():
    text = asyncio.run(call(build_server(fake_panel()), "ixel_review",
                            {"question": "What is 17 × 23?", "mode": "quick"}))
    assert "## Verdict (quick mode" in text and "## Peer review" not in text


@pytest.mark.parametrize("args,expected", [
    ({"question": "   "}, "the question is empty"),
    ({"question": "x" * 50_001}, "longer than 50,000"),
    ({"question": "check this", "code": "KEY = 'sk-ant-api03-" + "a" * 40 + "'"}, "looks like an API key"),
], ids=["blank", "too-long", "secret"])
def test_bad_input_is_rejected_before_any_model_call(args, expected):
    agents = [Agent("gpt", "m-gpt", answer="no")]
    asked = []
    agents[0].send_and_receive = lambda *a, **k: asked.append(a)
    result = asyncio.run(call_tool(build_server(fake_panel(agents)), "ixel_review", args))
    # An error result, so the host knows nothing was asked
    assert result.is_error and expected in result.content[0].text and not asked


def test_invalid_mode_is_rejected():
    async def go():
        async with mcp.Client(build_server(fake_panel())) as client:
            return await client.call_tool("ixel_review", {"question": "q", "mode": "loud"})
    assert asyncio.run(go()).is_error


def test_only_one_review_runs_at_a_time():
    slow = [Agent("gpt", "m-gpt", delay=0.3), Agent("claude", "m-claude", delay=0.3)]

    async def go():
        async with mcp.Client(build_server(fake_panel(slow))) as client:
            first = asyncio.create_task(client.call_tool("ixel_review", {"question": "What is 17 × 23?"}))
            await asyncio.sleep(0.1)
            second = await client.call_tool("ixel_review", {"question": "again"})
            return await first, second

    first, second = asyncio.run(go())
    assert "## Verdict" in first.content[0].text and not first.is_error
    assert "already running a panel review" in second.content[0].text and second.is_error


def test_no_agents_explains_what_to_do():
    result = asyncio.run(call_tool(build_server(fake_panel([], ["GPT: couldn't connect (bad key)"])),
                                   "ixel_review", {"question": "q"}))
    text = result.content[0].text
    assert result.is_error
    assert "No panel agents are available" in text and "bad key" in text and "ixel setup" in text


def _triage(**section):
    from ixel_mat.config.loader import build_agent_configs
    from ixel_mat.triage import parse_triage_settings

    agents, _ = build_agent_configs({"agents": {"mini": {"type": "http", "url": "http://127.0.0.1:9/v1",
                                                         "model": "m", "label": "Mini"}}})
    triage, warnings = parse_triage_settings({"triage": {"enabled": True, **section}}, agents)
    return Settings({}, agents, warnings, triage=triage)


@pytest.mark.parametrize("section,says", [
    ({"provider": "model", "agent": "mini"}, "by Mini, one of the user's own models"),
    ({"url": "https://api.typesafe.ai/v1/systemone"}, "by TypeSafe's decision API"),
    ({"url": "https://resell.example/v1/systemone"}, "by resell.example, which is not TypeSafe's own API"),
], ids=["own-model", "typesafe", "other-host"])
def test_the_result_says_who_made_triage_decisions(monkeypatch, section, says):
    from ixel_mat.triage import TriageDecision

    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    result = ReviewResult("q", ReviewMode.REVIEW, error="x")
    result.triage = [TriageDecision(about="mode", ok=True, value="review", note="Triage picked review mode")]
    text = format_result(result, settings=_triage(**section))
    assert f"**Triage** (quick decisions between rounds, {says}):" in text


def test_settings_warnings_reach_the_host(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    settings = _triage(url="https://resell.example/v1/systemone")
    assert any("resell.example" in w for w in settings.warnings)
    review = asyncio.run(call(build_server(fake_panel(settings=settings)), "ixel_review", {"question": "17 × 23?"}))
    assert "## Settings to fix" in review and "resell.example, not TypeSafe's own API" in review

    monkeypatch.setattr(mcp_server, "load_settings", lambda: settings)
    panel = asyncio.run(call(build_server(fake_panel()), "ixel_panel"))
    assert "## Settings to fix" in panel and "resell.example" in panel


def test_model_output_is_sanitized_for_the_host():
    hostile = "391 \x1b]52;c;ZXZpbA==\x07 done \x1b[31mred\x1b[0m"
    agents = [Agent("gpt", "m-gpt", answer=hostile), Agent("claude", "m-claude")]
    text = asyncio.run(call(build_server(fake_panel(agents)), "ixel_review", {"question": "What is 17 × 23?"}))
    assert "\x1b" not in text and "\x07" not in text and "391  done red" in text


def test_format_result_without_a_verdict():
    text = format_result(ReviewResult("q", ReviewMode.REVIEW, error="No agent produced an answer."))
    assert "**No verdict:** No agent produced an answer." in text


def test_panel_tool_never_reveals_keys(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[agents.gpt]\ntype = "http"\nurl = "https://api.openai.com/v1/chat/completions"\n'
                   'token = "sk-super-secret-value"\nmodel = "gpt-x"\nlabel = "GPT"\n'
                   '[agents.grok]\ntype = "http"\nurl = "https://api.x.ai/v1/chat/completions"\n'
                   'token_env = "IXEL_TEST_UNSET"\nmodel = "grok-x"\nlabel = "Grok"\n')
    monkeypatch.setattr(loader, "_GLOBAL_CONFIG", cfg)
    monkeypatch.setattr(secrets, "_ENV_FILE", tmp_path / "missing.env")
    monkeypatch.delenv("IXEL_TEST_UNSET", raising=False)
    text = asyncio.run(call(build_server(fake_panel()), "ixel_panel"))
    assert "sk-super-secret" not in text
    assert "| GPT | http | gpt-x | yes |" in text and "| Grok | http | grok-x | missing API key |" in text


# ── Real protocol over stdio, as a host app runs it ───────────────────────────

def _env(home):
    return {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "IXEL_TEST_PANEL_KEY": "sk-test",
            "PYTHONIOENCODING": "utf-8"}


def test_stdio_server_end_to_end(tmp_path):
    with ThreadedFakeProvider(panel_handler(ANSWERS)) as fake:
        cfg_dir = tmp_path / ".config" / "ixel-mat"
        cfg_dir.mkdir(parents=True)
        cfg_dir.joinpath("config.toml").write_text("".join(
            f'[agents.{aid}]\ntype = "http"\nurl = "{fake.openai_url}"\ntoken_env = "IXEL_TEST_PANEL_KEY"\n'
            f'model = "{model}"\nlabel = "{label}"\n\n'
            for aid, model, label in [("gpt", "m-gpt", "GPT"), ("claude", "m-claude", "Claude"),
                                      ("gemini", "m-wrong", "Gemini")]))
        params = StdioServerParameters(command=sys.executable, args=["-m", "ixel_mat", "mcp"],
                                       env=_env(tmp_path), cwd=str(tmp_path))

        async def go():
            async with mcp.Client(params, read_timeout_seconds=60) as client:
                names = [t.name for t in (await client.list_tools()).tools]
                result = await client.call_tool("ixel_review", {"question": "What is 17 × 23?"})
                return names, result.content[0].text

        names, text = asyncio.run(go())
    assert sorted(names) == ["ixel_panel", "ixel_review"]
    assert "17 × 23 = 391" in text and "Gemini rated" in text
    assert len(fake.requests) == 7


def test_setup_snippets_use_absolute_paths(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "ixel_mat", "mcp", "--setup"], env=_env(tmp_path),
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert '"mcpServers"' in out and "claude mcp add --scope user ixel --" in out
    assert "[mcp_servers.ixel]" in out and "tool_timeout_sec = 600" in out
    exe_dir = os.path.dirname(sys.executable)
    assert exe_dir.replace("\\", "\\\\") in out or exe_dir in out

    bad = subprocess.run([sys.executable, "-m", "ixel_mat", "mcp", "--nope"], env=_env(tmp_path),
                         capture_output=True, text=True, timeout=60)
    assert bad.returncode == 2


def _install(folder, *programs):
    folder.mkdir(parents=True)
    for name in programs:
        (folder / name).write_bytes(b"")
    return folder


def _windows(monkeypatch, python, in_venv=True, user_site=False, version=None):
    monkeypatch.setattr(mcp_server, "WINDOWS", True)
    monkeypatch.setattr(mcp_server.sys, "executable", python)
    monkeypatch.setattr(mcp_server.sys, "base_prefix", r"C:\Python311" if in_venv else sys.prefix)
    monkeypatch.setattr(mcp_server.site, "ENABLE_USER_SITE", user_site)
    if version:
        monkeypatch.setattr(mcp_server.sys, "version_info", version)
    return host_snippets()


def test_windows_setup_runs_the_signed_python_not_ixel_exe(tmp_path, monkeypatch):
    # Smart App Control blocks the unsigned ixel.exe pip writes; python.exe is signed
    python = str(_install(tmp_path / "Ixel MAT" / "Scripts", "python.exe", "ixel.exe") / "python.exe")
    out = _windows(monkeypatch, python)  # in a virtualenv, like the installer's
    command = python.replace("\\", "\\\\")
    assert f'"ixel": {{ "command": "{command}", "args": ["-I", "-m", "ixel_mat", "mcp"] }}' in out  # Desktop, Cursor
    assert f'claude mcp add --scope user ixel -- "{python}" -I -m ixel_mat mcp' in out
    assert f'command = "{command}"\n  args = ["-I", "-m", "ixel_mat", "mcp"]' in out  # Codex
    assert "ixel.exe" not in out


@pytest.mark.parametrize("in_venv, user_site, version, flags", [
    (True, False, (3, 10, 11, "final", 0), ["-I"]),   # the installer's, pipx's or uv's virtualenv
    (False, True, (3, 11, 9, "final", 0), ["-P"]),    # pip install outside a virtualenv
    (False, True, (3, 10, 11, "final", 0), []),       # ... on 3.10, which has no -P
    (True, True, (3, 11, 9, "final", 0), ["-P"]),     # a virtualenv made with --system-site-packages
])
def test_windows_setup_leaves_out_user_site_packages_only_where_ixel_cant_be(monkeypatch, in_venv, user_site,
                                                                             version, flags):
    # -I also leaves out user site-packages, where pip puts Ixel when it can't write to Python's own folder
    python = r"C:\Program Files\Python311\python.exe"
    out = _windows(monkeypatch, python, in_venv, user_site, version)
    assert f'claude mcp add --scope user ixel -- "{python}" {" ".join([*flags, "-m", "ixel_mat", "mcp"])}\n' in out


@pytest.mark.skipif(sys.prefix == sys.base_prefix, reason="needs the Python this virtualenv was made from")
def test_windows_setup_for_a_user_site_install_starts(tmp_path):
    """`pip install` into a Python that isn't a virtualenv may put Ixel in user site-packages: the command
    `ixel mcp --setup` gives apps on Windows has to find it there."""
    base = getattr(sys, "_base_executable", "")  # (on Linux, what this virtualenv's python links to)
    if not base or base == sys.executable or not os.path.exists(base):
        pytest.skip("this virtualenv's Python isn't at hand")
    env = {k: v for k, v in _env(tmp_path).items()
           if k not in ("PYTHONSAFEPATH", "PYTHONPATH", "PYTHONNOUSERSITE", "PYTHONHOME")}
    env["PYTHONUSERBASE"] = str(tmp_path / "userbase")

    def python(*argv, cwd=tmp_path):
        return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)

    # Ixel and what it needs in that Python's user site-packages, as a user-site install has them
    user_site = Path(python(base, "-c", "import site; print(site.getusersitepackages())").stdout.strip())
    user_site.mkdir(parents=True)
    ixel = [*site.getsitepackages(), str(Path(mcp_server.__file__).parents[1])]
    # addsitedir, not the folders alone: their own .pth files run too, as pywin32's must for mcp on Windows
    (user_site / "ixel.pth").write_text("".join(f"import site; site.addsitedir({p!r})\n" for p in ixel),
                                        encoding="utf-8")
    setup = python(base, "-c", "from ixel_mat import mcp_server; mcp_server.WINDOWS = True; "
                               "print(mcp_server.host_snippets())")
    assert setup.returncode == 0, setup.stderr
    command = json.loads(re.search(r"^  command = (.*)$", setup.stdout, re.M).group(1))
    args = json.loads(re.search(r"^  args = (.*)$", setup.stdout, re.M).group(1))
    assert os.path.samefile(command, base) and args[-3:] == ["-m", "ixel_mat", "mcp"]

    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "PWNED"
    if "-P" in args:  # 3.11+: and still nothing from the folder the app starts it in
        (project / "mcp.py").write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
    plugin = python(command, *args, "--setup", cwd=project)
    # With -I, which leaves out user site-packages: "No module named ixel_mat"
    assert plugin.returncode == 0 and '"mcpServers"' in plugin.stdout, plugin.stderr
    assert not marker.exists()


def test_other_systems_set_up_the_ixel_command(tmp_path, monkeypatch):
    folder = _install(tmp_path / "bin", "python", "ixel")
    monkeypatch.setattr(mcp_server, "WINDOWS", False)
    monkeypatch.setattr(mcp_server.sys, "executable", str(folder / "python"))
    shell = lambda path: f'"{path}"' if " " in str(path) else str(path)  # noqa: E731
    assert f'claude mcp add --scope user ixel -- {shell(folder / "ixel")} mcp' in host_snippets()
    (folder / "ixel").unlink()
    # No ixel script beside Python (pip install --user): python, kept from importing the current folder
    flags = " ".join(mcp_server._safe_path_flags())
    assert flags in ("-I", "-P")
    assert f'claude mcp add --scope user ixel -- {shell(folder / "python")} {flags} -m ixel_mat mcp' in host_snippets()


@pytest.mark.parametrize("flag", ["-I", pytest.param("-P", marks=pytest.mark.skipif(sys.version_info < (3, 11),
                                                                                    reason="-P is new in 3.11"))])
def test_python_never_imports_from_the_folder_it_starts_in(tmp_path, flag):
    """What ixel.cmd and the Windows plugin setup run. A host app may start the plugin in a project, or
    you may run ixel in a repository you just cloned: an mcp.py there mustn't stand in for the MCP SDK."""
    project = tmp_path / "cloned-repo"
    project.mkdir()
    marker = tmp_path / "PWNED"
    (project / "mcp.py").write_text(f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8")
    env = {k: v for k, v in _env(tmp_path).items() if k != "PYTHONSAFEPATH"}

    def start(*flags, option="--setup"):
        return subprocess.run([sys.executable, *flags, "-m", "ixel_mat", "mcp", option], cwd=project, env=env,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)

    proc = start(flag)
    assert proc.returncode == 0 and '"mcpServers"' in proc.stdout and not marker.exists(), proc.stderr
    assert start(flag, option="--nope").returncode == 2  # ixel's exit code comes through
    start()  # without it, python -m looks in the current folder first
    assert marker.exists()


def test_claude_desktop_from_the_windows_installer_is_pointed_at_the_config_it_reads(tmp_path, monkeypatch):
    """Anthropic's Windows installer makes Claude Desktop an app package: once it has a config in its own folder,
    it reads that one, not %APPDATA%'s."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert mcp_server.windows_desktop_config() == r"%APPDATA%\Claude\claude_desktop_config.json"
    own = tmp_path / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
    own.mkdir(parents=True)
    (own / "claude_desktop_config.json").write_text("{}\n", encoding="utf-8")
    assert mcp_server.windows_desktop_config() == ("%LOCALAPPDATA%\\Packages\\Claude_pzs8sxrjxfjjc\\LocalCache"
                                                   "\\Roaming\\Claude\\claude_desktop_config.json")
    assert f"({mcp_server.windows_desktop_config()})" in _windows(monkeypatch, r"C:\Python311\python.exe")
