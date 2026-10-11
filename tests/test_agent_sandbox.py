"""
Running the agent's commands (ixel_mat/agent/sandbox.py): which commands count as a shell or an interpreter
(never always-allowed), and on a Mac, that sandbox-exec keeps a command off the network and out of folders
other than the workspace and the temp folder, and that the time limit stops it.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

from ixel_mat.agent.sandbox import is_shell_or_interpreter, run_command

darwin = pytest.mark.skipif(sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"),
                            reason="sandbox-exec is macOS only")


# ── is_shell_or_interpreter ──────────────────────────────────────────────────

@pytest.mark.parametrize("argv", [
    ["sh", "-c", "x"], ["bash"], ["/bin/zsh"], ["python", "-c", "x"], ["python3.12", "-c", "x"],
    ["node", "-e", "x"], ["env", "bash", "-c", "x"], [],
])
def test_shells_and_interpreters_running_code(argv):
    assert is_shell_or_interpreter(argv) is True


@pytest.mark.parametrize("argv", [["pytest", "-q"], ["python", "-m", "pytest"], ["git", "status"], ["ls"]])
def test_ordinary_commands(argv):
    assert is_shell_or_interpreter(argv) is False


def test_run_command_refuses_argv_that_is_not_a_list(tmp_path):
    with pytest.raises(ValueError):
        run_command("ls", tmp_path)


# ── Under sandbox-exec ───────────────────────────────────────────────────────

def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _outside_target(tmp_path: Path) -> Path:
    """A place the sandbox must not let a command write: next to the workspace when that's outside the temp
    folders, otherwise a file in the home folder."""
    parent = os.path.realpath(tmp_path.parent)
    allowed = (os.path.realpath(tempfile.gettempdir()), "/private/tmp")
    if not any(_inside(parent, folder) for folder in allowed):
        return Path(parent) / "ixel_agent_escaped.txt"
    return Path(os.path.expanduser("~/ixel_agent_sandbox_should_not_exist.txt"))


@darwin
def test_a_command_runs_sandboxed(tmp_path):
    result = run_command(["/bin/echo", "hi"], tmp_path)
    assert result.returncode == 0
    assert "hi" in result.output
    assert result.sandboxed is True
    assert result.timed_out is False


@darwin
def test_a_write_inside_the_workspace_works(tmp_path):
    result = run_command(["/usr/bin/touch", "made.txt"], tmp_path)
    assert result.returncode == 0
    assert (tmp_path / "made.txt").exists()


@darwin
def test_a_write_outside_the_workspace_fails(tmp_path):
    target = _outside_target(tmp_path)
    if target.exists():
        pytest.skip(f"{target.name} is already there; can't tell whether the sandbox wrote it")
    try:
        result = run_command(["/usr/bin/touch", str(target)], tmp_path)
        assert result.returncode != 0
        assert not target.exists()
    finally:
        if target.exists():
            target.unlink()


@darwin
def test_the_network_is_denied(tmp_path):
    result = run_command(["/usr/bin/curl", "-sS", "--max-time", "5", "http://127.0.0.1:9"], tmp_path)
    assert result.sandboxed is True
    assert result.returncode not in (0, None)
    assert result.output.strip()


@darwin
def test_a_command_is_stopped_at_the_time_limit(tmp_path):
    started = time.monotonic()
    result = run_command(["/bin/sleep", "30"], tmp_path, timeout=1)
    assert time.monotonic() - started < 10
    assert result.timed_out is True
    assert result.returncode is None
