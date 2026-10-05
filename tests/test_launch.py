"""Starting command-line agents: npm shims on Windows, PATH-only lookup, and long prompts on stdin."""
import asyncio
import os
import sys

import pytest

from ixel_mat.agents import launch, oneshot
from ixel_mat.agents.base import AgentConfig
from ixel_mat.agents.launch import LaunchError, find_on_path, resolve_argv, windows_argv
from ixel_mat.agents.oneshot import ARG_LIMIT, OneShotAgent

# What `npm install -g` writes today (cmd-shim), for a package whose bin is a script...
NPM_SHIM = r"""@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@openai\codex\bin\codex.js" %*
"""
# ...for one whose bin is a program...
NPM_EXE_SHIM = r"""@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0
"%dp0%\node_modules\@anthropic-ai\claude-code\bin\claude.exe"   %*
"""
# ...and what older npm wrote
OLD_NPM_SHIM = r"""@IF EXIST "%~dp0\node.exe" (
  "%~dp0\node.exe"  "%~dp0\node_modules\@google\gemini-cli\dist\index.js" %*
) ELSE (
  @SETLOCAL
  @SET PATHEXT=%PATHEXT:;.JS;=;%
  node  "%~dp0\node_modules\@google\gemini-cli\dist\index.js" %*
)
"""


def npm_prefix(tmp_path, name, shim, target):
    """A global npm folder holding name.cmd and the file it runs."""
    prefix = tmp_path / "npm"
    (prefix / target).parent.mkdir(parents=True, exist_ok=True)
    (prefix / target).write_text("// the CLI\n")
    (prefix / f"{name}.cmd").write_text(shim)
    return prefix


def which_in(prefix, node="C:/Program Files/nodejs/node.exe"):
    table = {p.stem: str(p) for p in prefix.glob("*.cmd")}
    return lambda name: node if name == "node.exe" else table.get(name)


def test_an_npm_script_runs_through_node_not_cmd(tmp_path):
    prefix = npm_prefix(tmp_path, "codex", NPM_SHIM, "node_modules/@openai/codex/bin/codex.js")
    argv = windows_argv(["codex", "exec", "--", 'say "hi" & del *.*'], which_in(prefix))
    script = prefix / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    assert argv == ["C:/Program Files/nodejs/node.exe", str(script.resolve()), "exec", "--", 'say "hi" & del *.*']


def test_the_node_next_to_the_shim_comes_first(tmp_path):
    prefix = npm_prefix(tmp_path, "gemini", OLD_NPM_SHIM, "node_modules/@google/gemini-cli/dist/index.js")
    (prefix / "node.exe").write_text("")
    argv = windows_argv(["gemini", "-o", "text"], which_in(prefix))
    assert argv[0] == str(prefix / "node.exe") and argv[1].endswith("index.js") and argv[2:] == ["-o", "text"]


def test_an_npm_program_runs_directly(tmp_path):
    prefix = npm_prefix(tmp_path, "claude", NPM_EXE_SHIM, "node_modules/@anthropic-ai/claude-code/bin/claude.exe")
    argv = windows_argv(["claude", "-p", "a | b"], which_in(prefix, node=None))
    assert argv == [str((prefix / "node_modules/@anthropic-ai/claude-code/bin/claude.exe").resolve()), "-p", "a | b"]


def test_no_node_leaves_nothing_to_run_but_the_batch_file(tmp_path):
    prefix = npm_prefix(tmp_path, "codex", NPM_SHIM, "node_modules/@openai/codex/bin/codex.js")
    with pytest.raises(LaunchError, match="batch file"):
        windows_argv(["codex", "a&b"], which_in(prefix, node=None))


def test_another_batch_file_runs_only_with_plain_arguments(tmp_path):
    (tmp_path / "hermes.bat").write_text("@echo off\npython -m hermes %*\n")
    which = lambda name: str(tmp_path / "hermes.bat") if name == "hermes" else None  # noqa: E731
    assert windows_argv(["hermes", "--quiet", "what is 17 x 23?"], which) == \
        [str(tmp_path / "hermes.bat"), "--quiet", "what is 17 x 23?"]
    for unsafe in ('a "quoted" word', "a & b", "50%", "x | y", "(a)", "hi!", "line\nbreak"):
        with pytest.raises(LaunchError, match="prompt_via"):
            windows_argv(["hermes", unsafe], which)


def test_a_shim_pointing_nowhere_is_treated_as_a_plain_batch_file(tmp_path):
    prefix = npm_prefix(tmp_path, "codex", NPM_SHIM, "elsewhere/codex.js")  # the target isn't where it says
    assert windows_argv(["codex", "exec"], which_in(prefix)) == [str(prefix / "codex.cmd"), "exec"]
    with pytest.raises(LaunchError):
        windows_argv(["codex", "a > b"], which_in(prefix))


def test_programs_get_their_full_path_and_unknown_ones_are_never_started():
    which = {"git": r"C:\Program Files\Git\cmd\git.exe"}.get
    assert windows_argv(["git", "diff"], which) == [r"C:\Program Files\Git\cmd\git.exe", "diff"]
    # CreateProcess would look for a bare name in the current folder first
    with pytest.raises(FileNotFoundError):
        windows_argv(["nope", "x"], which)
    assert windows_argv([], which) == []


def test_a_cli_not_on_path_is_reported_not_run(monkeypatch):
    monkeypatch.setattr(launch, "WINDOWS", True)
    agent = OneShotAgent(AgentConfig(name="cli", label="CLI", type="oneshot", command="ixel-no-such-cli",
                                     prompt_via="stdin"))
    with pytest.raises(RuntimeError, match="Command not found: ixel-no-such-cli"):
        _ask(agent, "q")


def test_a_node_that_is_itself_a_batch_file_is_not_used(tmp_path):
    """A version manager's node.cmd would put cmd.exe back in between."""
    prefix = npm_prefix(tmp_path, "codex", NPM_SHIM, "node_modules/@openai/codex/bin/codex.js")
    shims = tmp_path / "shims"
    shims.mkdir()
    (shims / "node.cmd").write_text("@mise x -- node %*\n")
    table = {"codex": str(prefix / "codex.cmd"), "node": str(shims / "node.cmd")}
    with pytest.raises(LaunchError):
        windows_argv(["codex", "a & b"], table.get)  # node.exe isn't on PATH: the checked batch path


@pytest.mark.parametrize("shim", [
    NPM_SHIM.replace('SET "_prog=node"', 'SET "_prog=bun"'),
    NPM_SHIM.replace('"%_prog%"  "%dp0%', '"%_prog%" --experimental-vm-modules "%dp0%'),
    NPM_SHIM.replace("_prog=%dp0%\\node.exe", "_prog=%dp0%\\sh.exe").replace('_prog=node"', '_prog=sh"'),
], ids=["bun", "node-flags", "sh"])
def test_a_shim_that_runs_something_else_is_not_second_guessed(tmp_path, shim):
    prefix = npm_prefix(tmp_path, "tool", shim, "node_modules/@openai/codex/bin/codex.js")
    assert windows_argv(["tool", "exec"], which_in(prefix)) == [str(prefix / "tool.cmd"), "exec"]
    with pytest.raises(LaunchError):
        windows_argv(["tool", "a | b"], which_in(prefix))


def _start_subprocess_agent(monkeypatch, command, args):
    """The argv a persistent (subprocess) agent would start, as Windows would see it."""
    from ixel_mat.agents import subprocess as subprocess_mod
    started = []

    async def not_started(*cmd, **kwargs):
        started.append(list(cmd))
        raise OSError("not started in a test")

    monkeypatch.setattr(launch, "WINDOWS", True)
    monkeypatch.setattr(subprocess_mod, "create_process_tree", not_started)
    agent = subprocess_mod.SubprocessAgent(AgentConfig(name="cli", label="CLI", type="subprocess", command=command,
                                                       args=args), use_pty=False)
    with pytest.raises(OSError, match="not started"):
        asyncio.run(agent.connect())
    return started[0]


def test_a_persistent_cli_agent_runs_an_npm_script_through_node_not_cmd(tmp_path, monkeypatch):
    prefix = npm_prefix(tmp_path, "codex", NPM_SHIM, "node_modules/@openai/codex/bin/codex.js")
    (prefix / "node.exe").write_text("")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setenv("PATH", str(prefix))
    script = prefix / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    assert _start_subprocess_agent(monkeypatch, "codex", ["--profile", "a & b"]) == \
        [str(prefix / "node.exe"), str(script.resolve()), "--profile", "a & b"]


def test_a_persistent_cli_agent_s_batch_file_gets_only_plain_arguments(tmp_path, monkeypatch):
    (tmp_path / "hermes.bat").write_text("@echo off\npython -m hermes %*\n")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _start_subprocess_agent(monkeypatch, "hermes", ["--quiet"]) == [str(tmp_path / "hermes.bat"), "--quiet"]
    with pytest.raises(LaunchError, match="batch file"):
        _start_subprocess_agent(monkeypatch, "hermes", ["--name", "a & b"])


def test_other_systems_start_the_command_as_given(monkeypatch):
    monkeypatch.setattr(launch, "WINDOWS", False)
    assert resolve_argv(["codex", "a&b"]) == ["codex", "a&b"]


# ── Looking only on PATH ──────────────────────────────────────────────────────

def _program(folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("#!/bin/sh\necho hi\n")
    path.chmod(0o755)
    return path


@pytest.mark.skipif(os.name == "nt", reason="POSIX PATH rules")
def test_the_current_folder_and_relative_path_entries_are_skipped(tmp_path, monkeypatch):
    repo = tmp_path / "cloned-repo"
    _program(repo, "claude")
    _program(repo / "bin", "claude")
    real = _program(tmp_path / "global", "claude")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "bin", f'"{real.parent}"']))
    assert find_on_path("claude") == str(real)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "bin"]))
    assert find_on_path("claude") is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_only_programs_count(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "claude").write_text("not executable")
    real = _program(tmp_path / "b", "claude")
    monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")]))
    assert find_on_path("claude") == str(real)


def test_a_path_is_that_file_or_nothing(tmp_path):
    # On Windows only what CreateProcess can start counts: tool.cmd, given with or without its extension
    real = _program(tmp_path, "tool.cmd" if launch.WINDOWS else "tool")
    assert find_on_path(str(real)) == str(real)
    assert find_on_path(str(tmp_path / "tool")) == str(real)
    assert find_on_path(str(tmp_path / "missing")) is None
    assert find_on_path("") is None


def test_a_windows_path_without_a_program_extension_is_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(launch, "WINDOWS", True)
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    script = _program(tmp_path, "tool")  # a script with no extension: Windows can't start it
    assert find_on_path(str(script)) is None
    (tmp_path / "tool.cmd").write_text("@echo hi\n")
    assert find_on_path(str(script)) == str(tmp_path / "tool.cmd")
    assert find_on_path(str(tmp_path / "tool.cmd")) == str(tmp_path / "tool.cmd")


def test_windows_lookup_uses_pathext(tmp_path, monkeypatch):
    folder = tmp_path / "npm"
    folder.mkdir()
    (folder / "codex.cmd").write_text(NPM_SHIM)
    monkeypatch.setattr(launch, "WINDOWS", True)
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setenv("PATH", str(folder))
    assert find_on_path("codex").lower() == str(folder / "codex.cmd").lower()
    assert find_on_path("codex.cmd") == str(folder / "codex.cmd")
    assert find_on_path("gemini") is None


def test_windows_lookup_skips_what_createprocess_cant_start(tmp_path, monkeypatch):
    first, second = tmp_path / "bin", tmp_path / "local"
    first.mkdir()
    second.mkdir()
    (first / "claude.py").write_text("print('wrapper')\n")  # python.org adds .PY to PATHEXT
    (second / "claude.exe").write_text("")
    monkeypatch.setattr(launch, "WINDOWS", True)
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.PY;.PYW")
    monkeypatch.setenv("PATH", os.pathsep.join([str(first), str(second)]))
    assert find_on_path("claude") == str(second / "claude.exe")


# ── Long prompts go on stdin ──────────────────────────────────────────────────

ECHO = "import sys; data = sys.stdin.read(); print(len(sys.argv) - 1, len(data))"


def _auto_agent():
    return OneShotAgent(AgentConfig(name="cli", label="CLI", type="oneshot", command=sys.executable,
                                    args=["-c", ECHO], prompt_via="auto"))


def _ask(agent, message):
    async def go():
        await agent.connect()
        return await agent.send_and_receive(message)
    return asyncio.run(go())


def test_auto_passes_a_short_prompt_as_an_argument():
    agent = _auto_agent()
    cmd, stdin = agent._build_command("-v what is 17 x 23?")
    assert cmd[-2:] == ["--", "-v what is 17 x 23?"] and stdin is None
    assert _ask(agent, "what is 17 x 23?") == "2 0"  # sys.argv[1:] is ["--", prompt]; nothing on stdin


def test_auto_sends_a_long_prompt_on_stdin():
    agent = _auto_agent()
    long_prompt = "x" * (ARG_LIMIT + 1)
    cmd, stdin = agent._build_command(long_prompt)
    assert long_prompt not in cmd and stdin == long_prompt.encode()
    assert _ask(agent, long_prompt) == f"0 {ARG_LIMIT + 1}"


def test_arg_mode_refuses_a_prompt_too_long_for_a_command_line():
    agent = OneShotAgent(AgentConfig(name="cli", label="CLI", type="oneshot", command=sys.executable,
                                     args=["-c", ECHO], prompt_via="arg"))
    with pytest.raises(RuntimeError, match='prompt_via = "stdin"'):
        agent._build_command("x" * (ARG_LIMIT + 1))


def test_windows_counts_the_quoting_a_prompt_needs(monkeypatch):
    monkeypatch.setattr(launch, "WINDOWS", True)
    quotes = '"' * 20_000
    assert oneshot.arg_size(quotes) == 40_000  # each " is passed as \"
    assert oneshot.arg_size("é" * 100) == 100  # characters, not bytes


@pytest.mark.skipif(os.name != "posix", reason="Windows gives a program from the Start menu your whole PATH")
def test_programs_where_their_installers_put_them_are_found_from_the_app_menu(tmp_path, monkeypatch):
    # The app menu's PATH has nothing your shell's startup file adds, where OpenCode's installer adds ~/.opencode/bin
    monkeypatch.setenv("HOME", str(tmp_path))
    program = tmp_path / ".opencode" / "bin" / "opencode"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    (tmp_path / "empty").mkdir()
    env = {"PATH": str(tmp_path / "empty")}
    added = launch.add_install_folders(env)
    assert str(program.parent) in added and str(tmp_path / ".local" / "bin") in added
    assert not any(".bun" in folder for folder in added)  # only folders that are there
    # After what PATH had, so a program on it is still the one that runs
    assert env["PATH"].split(os.pathsep)[0] == str(tmp_path / "empty")
    monkeypatch.setenv("PATH", env["PATH"])
    assert find_on_path("opencode") == str(program)
    assert launch.add_install_folders(env) == [] and env["PATH"] == os.environ["PATH"]  # never twice


@pytest.mark.skipif(os.name != "posix", reason="Windows gives a program from the Start menu your whole PATH")
def test_a_folder_anyone_can_write_to_is_never_added(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    folder = tmp_path / ".opencode" / "bin"
    folder.mkdir(parents=True)
    folder.chmod(0o777)
    env = {"PATH": "/usr/bin"}
    assert not any(".opencode" in f for f in launch.add_install_folders(env))
    folder.chmod(0o755)
    monkeypatch.setattr(launch.os, "getuid", lambda: 4242)  # someone else's
    if os.stat(folder).st_uid != 0:
        assert not any(".opencode" in f for f in launch.add_install_folders(env))


@pytest.mark.skipif(os.name != "posix", reason="Windows gives a program from the Start menu your whole PATH")
def test_no_path_at_all_keeps_the_default_one(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".opencode" / "bin").mkdir(parents=True)
    env = {}
    launch.add_install_folders(env)
    folders = env["PATH"].split(os.pathsep)
    assert folders[:2] == [f for f in os.defpath.split(os.pathsep) if f]
    assert str(tmp_path / ".opencode" / "bin") in folders


@pytest.mark.skipif(os.name != "posix", reason="Windows gives a program from the Start menu your whole PATH")
def test_a_folder_already_on_path_keeps_its_place(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".opencode" / "bin").mkdir(parents=True)
    env = {"PATH": f"{tmp_path / '.opencode' / 'bin'}/{os.pathsep}/usr/bin"}  # spelled with a trailing /
    assert not any(".opencode" in folder for folder in launch.add_install_folders(env))
    assert env["PATH"].startswith(f"{tmp_path / '.opencode' / 'bin'}/{os.pathsep}/usr/bin")


def test_windows_path_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(launch, "WINDOWS", True)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".opencode" / "bin").mkdir(parents=True)
    env = {"PATH": "C:\\Windows"}
    assert launch.add_install_folders(env) == [] and env == {"PATH": "C:\\Windows"}


@pytest.mark.skipif(os.name != "posix", reason="Windows gives a program from the Start menu your whole PATH")
def test_ixel_opened_from_the_app_menu_finds_opencode_where_its_installer_put_it(tmp_path):
    # As the app menu starts Ixel: a bare PATH, OpenCode in ~/.opencode/bin, and a panel with OpenCode on it
    import subprocess
    program = tmp_path / ".opencode" / "bin" / "opencode"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\necho 'opencode v2.0.23'\n")
    program.chmod(0o755)
    config = tmp_path / ".config" / "ixel-mat" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[agents.opencode]\npreset = "opencode"\n', encoding="utf-8")
    (tmp_path / "empty").mkdir()
    env = {"HOME": str(tmp_path), "PATH": str(tmp_path / "empty"), "IXEL_TEST_KEYCHAIN": "memory",
           "IXEL_NO_UPDATE_CHECK": "1", "XDG_CONFIG_HOME": str(tmp_path / ".config"), "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run([sys.executable, "-m", "ixel_mat", "doctor"], env=env, capture_output=True,
                         encoding="utf-8", timeout=120, cwd=tmp_path).stdout
    assert "opencode is installed" in " ".join(out.split()) and "isn't installed" not in out
