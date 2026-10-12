"""
Running the agent's commands (ixel_mat/agent/sandbox.py): which commands count as a shell or an interpreter
(never always-allowed); on every system, the bubblewrap command it builds, the secret folders it hides, which
sandbox sandboxed_argv picks, and when a sandbox is available (with the probe faked); and, on a Mac under
sandbox-exec and on Linux under bubblewrap, that a command stays off the network and out of folders other
than the workspace and the temp folder, that Linux hides the secret folders, and that the time limit stops it.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ixel_mat.agent import sandbox
from ixel_mat.agent.sandbox import is_shell_or_interpreter, run_command
from ixel_mat.agent.secret_folders import secret_folders
from ixel_mat.config import secrets

darwin = pytest.mark.skipif(sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"),
                            reason="sandbox-exec is macOS only")
linux_bwrap = pytest.mark.skipif(not sys.platform.startswith("linux") or not sandbox.can_sandbox(),
                                 reason="needs Linux with a working bubblewrap")

BWRAP = "/usr/bin/bwrap"


@pytest.fixture(autouse=True)
def _probe_afresh():
    """bubblewrap's test run is remembered: a test that fakes it must not leave its answer for the next test,
    and a test that runs bubblewrap for real must not be given a faked one."""
    sandbox._bwrap_status.cache_clear()
    yield
    sandbox._bwrap_status.cache_clear()


@pytest.fixture
def fake_bwrap(monkeypatch):
    """shutil.which finds bwrap at BWRAP, whether or not this computer has it."""
    which = shutil.which
    monkeypatch.setattr(shutil, "which",
                        lambda name, *args, **kwargs: BWRAP if name == "bwrap" else which(name, *args, **kwargs))
    return BWRAP


@pytest.fixture
def places(tmp_path, monkeypatch):
    """A fake home with a secret folder (.ssh, with an id_x in it) and a secret file (.aws), Ixel's config folder,
    the temp folder and a workspace, all in the test's own folder, so no path depends on this computer."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_x").write_text("fake private key", encoding="utf-8")
    (home / ".aws").write_text("fake credentials", encoding="utf-8")
    config = tmp_path / "cfg"
    temp = tmp_path / "temp"
    work = tmp_path / "work"
    for folder in (config, temp, work):
        folder.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(secrets, "_ENV_DIR", config)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    return SimpleNamespace(home=home, config=config, temp=temp, work=work)


class _Probe:
    """Stands in for subprocess.run: answers with a return code and what it wrote to stderr, or raises error,
    and keeps each call."""

    def __init__(self, returncode: int = 0, stderr: str = "", error: Exception | None = None):
        self.returncode = returncode
        self.stderr = stderr
        self.error = error
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(args, self.returncode, "", self.stderr)


def _at(argv: list[str], *words: str) -> list[int]:
    """Where in argv these words come one after another (none: an empty list)."""
    return [i for i in range(len(argv) - len(words) + 1) if argv[i:i + len(words)] == list(words)]


def _has(argv: list[str], *words: str) -> bool:
    return bool(_at(argv, *words))


def _temps() -> list[str]:
    """The temp folders as bwrap_argv takes them: the real path of the temp folder and of /tmp, those there."""
    both = dict.fromkeys([os.path.realpath(tempfile.gettempdir()), os.path.realpath("/tmp")])
    return [path for path in both if os.path.exists(path)]


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


# ── The bubblewrap command (built the same on every system) ──────────────────

def test_bwrap_argv_has_the_sandbox_rules(places, fake_bwrap, monkeypatch):
    probe = _Probe()
    monkeypatch.setattr(sandbox.subprocess, "run", probe)
    argv = sandbox.bwrap_argv(places.work, ["ls", "-l"])
    assert argv[:4] == [BWRAP, "--ro-bind", "/", "/"]
    assert _has(argv, "--dev", "/dev")
    assert _has(argv, "--proc", "/proc")
    for flag in ("--unshare-net", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--die-with-parent"):
        assert flag in argv
    assert _has(argv, "--chdir", os.path.realpath(places.work))
    assert argv[argv.index("--"):] == ["--", "ls", "-l"]
    assert probe.calls == []  # it builds the command and doesn't try it


def test_bwrap_argv_without_the_network_rule_when_the_network_is_allowed(places, fake_bwrap):
    argv = sandbox.bwrap_argv(places.work, ["ls"], network=True)
    assert "--unshare-net" not in argv
    for flag in ("--unshare-pid", "--unshare-ipc", "--unshare-uts", "--die-with-parent"):
        assert flag in argv


def test_bwrap_argv_names_bwrap_when_it_isnt_found(places, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: None)
    assert sandbox.bwrap_argv(places.work, ["ls"])[0] == "bwrap"


@pytest.mark.parametrize("files, bound, not_bound", [
    ("none", "--ro-bind", "--bind"),
    ("output-folder", "--bind", "--ro-bind"),
    ("chosen-folder", "--bind", "--ro-bind"),
])
def test_bwrap_argv_binds_the_workspace_by_the_files_permission(places, fake_bwrap, files, bound, not_bound):
    argv = sandbox.bwrap_argv(places.work, ["ls"], files=files)
    ws = os.path.realpath(places.work)
    assert _has(argv, bound, ws, ws)
    assert not _has(argv, not_bound, ws, ws)
    assert _has(argv, "--chdir", ws)


def test_bwrap_argv_binds_the_temp_folders_or_gives_a_private_one(places, fake_bwrap):
    temps = _temps()
    assert os.path.realpath(places.temp) in temps
    argv = sandbox.bwrap_argv(places.work, ["ls"])
    for path in temps:
        assert _has(argv, "--bind", path, path)
        assert not _has(argv, "--tmpfs", path)
    argv = sandbox.bwrap_argv(places.work, ["ls"], temporary=False)
    for path in temps:
        assert _has(argv, "--tmpfs", path)
        assert not _has(argv, "--bind", path, path)


def test_bwrap_argv_refuses_an_unknown_files_permission(places, fake_bwrap):
    with pytest.raises(ValueError):
        sandbox.bwrap_argv(places.work, ["ls"], files="everything")


# ── The secret folders it hides ──────────────────────────────────────────────

def test_secret_folders_are_the_home_ones_and_ixels_config_folder(places):
    paths = secret_folders()
    assert os.path.realpath(places.home / ".ssh") in paths
    assert os.path.realpath(places.home / ".aws") in paths
    assert os.path.realpath(places.config) in paths
    assert len(paths) == len(set(paths))


def test_bwrap_argv_hides_the_secrets_that_exist_after_the_temp_binds(places, fake_bwrap):
    argv = sandbox.bwrap_argv(places.work, ["ls"])
    ssh = os.path.realpath(places.home / ".ssh")
    aws = os.path.realpath(places.home / ".aws")
    cfg = os.path.realpath(places.config)
    assert _has(argv, "--tmpfs", ssh)         # a folder: an empty one over it
    assert _has(argv, "--ro-bind", "/dev/null", aws)  # a file: /dev/null over it
    assert _has(argv, "--tmpfs", cfg)
    for missing in (".gnupg", ".config/chromium", "Library/Keychains"):
        assert os.path.realpath(places.home / missing) not in argv
    last_temp = max(i for path in _temps() for i in _at(argv, "--bind", path, path))
    first_mask = min(_at(argv, "--tmpfs", ssh) + _at(argv, "--ro-bind", "/dev/null", aws)
                     + _at(argv, "--tmpfs", cfg))
    assert last_temp < first_mask


def test_bwrap_argv_binds_a_workspace_in_a_hidden_folder_back(places, fake_bwrap):
    repo = places.config / "agent" / "scratch" / "repo"
    repo.mkdir(parents=True)
    argv = sandbox.bwrap_argv(repo, ["ls"])
    ws = os.path.realpath(repo)
    mask = _at(argv, "--tmpfs", os.path.realpath(places.config))
    assert len(mask) == 1
    assert _at(argv, "--bind", ws, ws)[-1] > mask[0]
    assert _has(argv, "--chdir", ws)


def test_bwrap_argv_binds_a_workspace_that_is_the_hidden_folder_back(places, fake_bwrap):
    argv = sandbox.bwrap_argv(places.config, ["ls"])
    ws = os.path.realpath(places.config)
    assert _at(argv, "--bind", ws, ws)[-1] > _at(argv, "--tmpfs", ws)[0]


def test_bwrap_argv_keeps_a_secret_inside_the_workspace_hidden(places, fake_bwrap):
    argv = sandbox.bwrap_argv(places.home, ["ls"])  # the home folder holds .ssh and .aws
    ws = os.path.realpath(places.home)
    last_bind = _at(argv, "--bind", ws, ws)[-1]
    assert _at(argv, "--tmpfs", os.path.realpath(places.home / ".ssh"))[-1] > last_bind
    assert _at(argv, "--ro-bind", "/dev/null", os.path.realpath(places.home / ".aws"))[-1] > last_bind


def test_bwrap_argv_hides_a_secret_again_inside_a_workspace_bound_back(places, fake_bwrap, monkeypatch):
    home = places.config / "home"  # a home inside Ixel's config folder, which is also the workspace
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    argv = sandbox.bwrap_argv(places.config, ["ls"])
    ws = os.path.realpath(places.config)
    ssh = os.path.realpath(home / ".ssh")
    assert _at(argv, "--tmpfs", ssh)[-1] > _at(argv, "--bind", ws, ws)[-1]


# ── Which sandbox sandboxed_argv picks ───────────────────────────────────────

def test_sandboxed_argv_on_a_mac_is_sandbox_exec(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sandbox, "can_sandbox", lambda: True)
    argv = sandbox.sandboxed_argv(["ls", "-l"], tmp_path)
    assert argv[:2] == [sandbox.SANDBOX_EXEC, "-p"]
    assert argv[2] == sandbox.sandbox_profile(tmp_path)
    assert argv[3:] == ["ls", "-l"]
    argv = sandbox.sandboxed_argv(["ls"], tmp_path, network=True, files="none", temporary=False)
    assert argv[2] == sandbox.sandbox_profile(tmp_path, network=True, files="none", temporary=False)


def test_sandboxed_argv_on_linux_is_bubblewrap(places, fake_bwrap, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "can_sandbox", lambda: True)
    argv = sandbox.sandboxed_argv(["ls", "-l"], places.work)
    assert argv[0] == BWRAP
    assert argv == sandbox.bwrap_argv(places.work, ["ls", "-l"])
    argv = sandbox.sandboxed_argv(["ls"], places.work, network=True, files="none", temporary=False)
    assert argv == sandbox.bwrap_argv(places.work, ["ls"], network=True, files="none", temporary=False)


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_sandboxed_argv_is_the_command_as_it_is_without_a_sandbox(tmp_path, monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sandbox, "can_sandbox", lambda: False)
    command = ["ls", "-l"]
    argv = sandbox.sandboxed_argv(command, tmp_path)
    assert argv == ["ls", "-l"]
    assert argv is not command


# ── Whether there is a sandbox here ──────────────────────────────────────────

def test_a_mac_has_a_sandbox_when_sandbox_exec_is_there(tmp_path, monkeypatch):
    present = tmp_path / "sandbox-exec"
    present.write_bytes(b"")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", str(present))
    assert sandbox.can_sandbox() is True
    assert sandbox.sandbox_unavailable_reason() is None
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", str(tmp_path / "missing"))
    assert sandbox.can_sandbox() is False
    assert "sandbox-exec" in sandbox.sandbox_unavailable_reason()


def test_linux_without_bwrap_has_no_sandbox_and_says_how_to_install_it(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: None)
    probe = _Probe()
    monkeypatch.setattr(sandbox.subprocess, "run", probe)
    assert sandbox.can_sandbox() is False
    reason = sandbox.sandbox_unavailable_reason()
    assert "bwrap" in reason
    for line in ("sudo apt install bubblewrap", "sudo dnf install bubblewrap", "sudo pacman -S bubblewrap"):
        assert line in reason
    assert probe.calls == []  # nothing to try


def test_linux_with_a_working_bwrap_has_a_sandbox_and_tries_it_once(fake_bwrap, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    probe = _Probe(0)
    monkeypatch.setattr(sandbox.subprocess, "run", probe)
    assert sandbox.can_sandbox() is True
    assert sandbox.can_sandbox() is True
    assert sandbox.sandbox_unavailable_reason() is None
    assert len(probe.calls) == 1
    argv, _ = probe.calls[0]
    assert argv[0] == BWRAP
    assert argv[-2:] == ["--", "true"]
    assert "--unshare-net" in argv


def test_the_answer_is_remembered_until_it_is_cleared(fake_bwrap, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox.subprocess, "run", _Probe(1, "bwrap: refused\n"))
    assert sandbox.can_sandbox() is False
    monkeypatch.setattr(sandbox.subprocess, "run", _Probe(0))
    assert sandbox.can_sandbox() is False  # still the first answer
    sandbox._bwrap_status.cache_clear()
    assert sandbox.can_sandbox() is True


def test_linux_where_apparmor_blocks_user_namespaces_says_the_one_line_fix(tmp_path, fake_bwrap, monkeypatch):
    restrict = tmp_path / "apparmor_restrict_unprivileged_userns"
    restrict.write_text("1\n", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_APPARMOR_USERNS", restrict)
    monkeypatch.setattr(sandbox.subprocess, "run", _Probe(1, "bwrap: setting up uid map: Permission denied\n"))
    assert sandbox.can_sandbox() is False
    reason = sandbox.sandbox_unavailable_reason()
    assert "AppArmor" in reason
    assert "apparmor_parser -r /etc/apparmor.d/bwrap" in reason
    assert "flags=(unconfined)" in reason
    assert f"profile bwrap {os.path.realpath(BWRAP)} flags=(unconfined)" in reason


@pytest.mark.parametrize("setting", ["0\n", None])
def test_linux_where_bwrap_fails_otherwise_says_what_it_printed(tmp_path, fake_bwrap, monkeypatch, setting):
    restrict = tmp_path / "apparmor_restrict_unprivileged_userns"
    if setting is not None:
        restrict.write_text(setting, encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_APPARMOR_USERNS", restrict)
    monkeypatch.setattr(sandbox.subprocess, "run",
                        _Probe(1, "bwrap: No permissions to create new namespace\nsecond line\n"))
    assert sandbox.can_sandbox() is False
    reason = sandbox.sandbox_unavailable_reason()
    assert "AppArmor" not in reason
    assert "bwrap: No permissions to create new namespace" in reason
    assert "second line" not in reason


def test_linux_where_bwrap_fails_silently_says_the_exit_code(tmp_path, fake_bwrap, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_APPARMOR_USERNS", tmp_path / "missing")
    monkeypatch.setattr(sandbox.subprocess, "run", _Probe(3))
    assert sandbox.can_sandbox() is False
    assert "code 3" in sandbox.sandbox_unavailable_reason()


@pytest.mark.parametrize("error", [OSError("Exec format error"), subprocess.TimeoutExpired("bwrap", 10)])
def test_linux_where_bwrap_cannot_be_run_has_no_sandbox(fake_bwrap, monkeypatch, error):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox.subprocess, "run", _Probe(error=error))
    assert sandbox.can_sandbox() is False
    reason = sandbox.sandbox_unavailable_reason()
    assert isinstance(reason, str) and reason


def test_windows_has_no_sandbox_yet(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert sandbox.can_sandbox() is False
    reason = sandbox.sandbox_unavailable_reason()
    assert isinstance(reason, str) and reason


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


# ── Under bubblewrap ─────────────────────────────────────────────────────────

SECRET = "ixel-fake-secret-7f3a91"
NOTE = "ixel-fake-note-2c84be"


@linux_bwrap
def test_linux_a_command_runs_sandboxed(tmp_path):
    result = run_command(["echo", "hi"], tmp_path)
    assert result.returncode == 0
    assert "hi" in result.output
    assert result.sandboxed is True
    assert result.timed_out is False


@linux_bwrap
def test_linux_a_write_inside_the_workspace_works(tmp_path):
    result = run_command(["touch", "made.txt"], tmp_path)
    assert result.sandboxed is True
    assert result.returncode == 0
    assert (tmp_path / "made.txt").exists()


@linux_bwrap
def test_linux_a_write_outside_the_workspace_fails(tmp_path):
    target = _outside_target(tmp_path)
    writable = (os.path.realpath(tempfile.gettempdir()), os.path.realpath("/tmp"), os.path.realpath(tmp_path))
    if any(_inside(os.path.realpath(target.parent), folder) for folder in writable):
        pytest.skip(f"{target.parent} is a folder the sandbox lets a command write in")
    if not os.access(target.parent, os.W_OK):
        pytest.skip(f"{target.parent} can't be written by this user anyway")
    if target.exists():
        pytest.skip(f"{target.name} is already there; can't tell whether the sandbox wrote it")
    try:
        result = run_command(["touch", str(target)], tmp_path)
        assert result.sandboxed is True
        assert result.returncode not in (0, None)
        assert not target.exists()
    finally:
        if target.exists():
            target.unlink()


@linux_bwrap
def test_linux_the_network_is_unreachable(tmp_path):
    script = ("import socket\n"
              "try:\n"
              "    socket.create_connection(('1.1.1.1', 53), timeout=5)\n"
              "except OSError as exc:\n"
              "    print('blocked', type(exc).__name__)\n"
              "    raise SystemExit(3)\n"
              "print('reached')\n")
    result = run_command([sys.executable, "-c", script], tmp_path)
    assert result.sandboxed is True
    assert result.returncode not in (0, None)
    assert "blocked" in result.output
    assert "reached" not in result.output


@linux_bwrap
def test_linux_a_secret_folder_cannot_be_read(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    key = home / ".ssh" / "id_x"
    key.write_text(SECRET, encoding="utf-8")
    notes = home / "notes.txt"
    notes.write_text(NOTE, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # Other files of the home folder are still readable: only the secret folders are hidden
    result = run_command(["cat", str(notes)], work)
    assert result.sandboxed is True
    assert result.returncode == 0
    assert NOTE in result.output
    result = run_command(["cat", str(key)], work)
    assert result.sandboxed is True
    assert result.returncode not in (0, None)
    assert SECRET not in result.output


@linux_bwrap
def test_linux_a_workspace_in_the_config_folder_works_and_the_rest_of_it_is_hidden(tmp_path, monkeypatch):
    config = tmp_path / "config"
    repo = config / "agent" / "scratch" / "repo"
    repo.mkdir(parents=True)
    (repo / "README").write_text(NOTE, encoding="utf-8")
    (config / "keys.enc").write_text(SECRET, encoding="utf-8")
    monkeypatch.setattr(secrets, "_ENV_DIR", config)
    result = run_command(["cat", "README"], repo)
    assert result.sandboxed is True
    assert result.returncode == 0
    assert NOTE in result.output
    result = run_command(["touch", "made.txt"], repo)
    assert result.returncode == 0
    assert (repo / "made.txt").exists()
    result = run_command(["cat", str(config / "keys.enc")], repo)
    assert result.returncode not in (0, None)
    assert SECRET not in result.output
