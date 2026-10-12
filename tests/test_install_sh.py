"""install.sh's install lock and its offer of the GTK and WebKit packages, run under a plain `sh`."""
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="install.sh's functions run under sh")

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "install.sh"
SH = shutil.which("sh") or "/bin/sh"

INTRO = ("Ixel's own window needs GTK and WebKit for the system's Python; without them, `ixel app` opens in your "
         "browser instead.")
GENERIC = "  Install PyGObject and WebKit2GTK 4.1 for the system's python3 with your package manager."
APT = "apt install python3-gi gir1.2-webkit2-4.1"


def _function(name: str) -> str:
    """One function of install.sh, from `name() {` to the next line that is just `}`."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    start = text.index(f"\n{name}() {{\n") + 1
    return text[start:text.index("\n}\n", start) + 3]


def _env(**extra: str) -> dict:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **extra}


def _run(script: str, *args: str, env: dict, input: str | None = None) -> subprocess.CompletedProcess:
    """`sh -c script`, with no terminal unless `input` is a pipe of text."""
    kwargs = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run([SH, "-c", script, "sh", *args], capture_output=True, text=True, encoding="utf-8",
                          env=env, timeout=60, **kwargs)


# ── The install lock ──────────────────────────────────────────────────────────

def _take(lock: Path, env: dict) -> subprocess.CompletedProcess:
    """take_install_lock on its own, as install.sh runs it, until the shell ends."""
    return _run(_function("take_install_lock") + '\ntake_install_lock "$1"\n', str(lock), env=env)


def _stop(holder: subprocess.Popen) -> None:
    try:
        os.killpg(holder.pid, signal.SIGKILL)  # the shell, and a sleep it started
    except OSError:
        pass
    holder.wait(timeout=10)
    for pipe in (holder.stdin, holder.stdout):
        try:
            pipe.close()
        except OSError:
            pass


@contextmanager
def _holding(lock: Path, env: dict, tail: str = "read line"):
    """A shell that takes the install lock, says `held`, then runs `tail` (by default: waits for a line)."""
    script = _function("take_install_lock") + '\ntake_install_lock "$1"\necho held\n' + tail + "\n"
    holder = subprocess.Popen([SH, "-c", script, "sh", str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env,
                              start_new_session=True)
    watchdog = threading.Timer(30, holder.kill)
    watchdog.start()
    try:
        try:
            line = holder.stdout.readline()
        finally:
            watchdog.cancel()
        assert line.strip() == "held", "the first install didn't get the lock"
        yield holder
    finally:
        _stop(holder)


@pytest.fixture
def no_flock_env(tmp_path):
    """A PATH with only the tools take_install_lock and the holders use, and no flock: the lock is a folder."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("mkdir", "rm", "find", "sleep"):
        found = shutil.which(name)
        if found is None:
            pytest.skip(f"no {name} on PATH")
        (tools / name).symlink_to(found)
    return {"PATH": str(tools)}


@pytest.mark.skipif(shutil.which("flock") is None, reason="no flock command here")
def test_a_second_install_is_refused_while_the_first_holds_the_flock(tmp_path):
    lock = tmp_path / "install.lock"
    with _holding(lock, _env(), tail="exec sleep 30") as holder:
        refused = _take(lock, _env())
        assert refused.returncode == 1 and refused.stdout == ""
        assert "already being installed" in refused.stderr
        holder.kill()  # a lock goes with the process that held it
        holder.wait(timeout=10)
        assert _take(lock, _env()).returncode == 0


@pytest.mark.skipif(shutil.which("flock") is None, reason="no flock command here")
def test_the_shell_refuses_while_ixel_update_holds_the_file(tmp_path):
    """`ixel update` flocks install.lock from Python; install.sh run by hand has to see that."""
    import fcntl
    lock = tmp_path / "install.lock"
    with open(lock, "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        refused = _take(lock, _env())
        assert refused.returncode == 1 and "already being installed" in refused.stderr
    assert _take(lock, _env()).returncode == 0


@pytest.mark.skipif(shutil.which("flock") is None, reason="no flock command here")
def test_the_installer_ixel_update_runs_doesnt_wait_for_updates_own_lock(tmp_path):
    import fcntl
    lock = tmp_path / "install.lock"
    with open(lock, "a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ran = _take(lock, _env(IXEL_INSTALL_LOCK_HELD="1"))
        assert ran.returncode == 0 and ran.stderr == ""


def test_without_flock_a_folder_is_the_lock(tmp_path, no_flock_env):
    lock = tmp_path / "install.lock"
    folder = tmp_path / "install.lock.d"
    with _holding(lock, no_flock_env) as holder:
        assert folder.is_dir()
        assert (folder / "owner").read_text(encoding="utf-8").strip() == str(holder.pid)  # the shell, not a child
        refused = _take(lock, no_flock_env)
        assert refused.returncode == 1 and refused.stdout == ""
        assert "already being installed" in refused.stderr
        assert folder.is_dir()  # the refused install left the holder's lock alone
        holder.stdin.write("\n")
        holder.stdin.flush()
        assert holder.wait(timeout=10) == 0
        assert not folder.exists()  # removed on the way out
    assert _take(lock, no_flock_env).returncode == 0
    assert not folder.exists()


def test_a_terminated_install_leaves_no_folder_behind(tmp_path, no_flock_env):
    folder = tmp_path / "install.lock.d"
    with _holding(tmp_path / "install.lock", no_flock_env, tail="sleep 30 &\nwait") as holder:
        assert folder.is_dir()
        holder.terminate()
        assert holder.wait(timeout=10) == 143  # the TERM trap's exit, which runs the EXIT trap
    assert not folder.exists()


@pytest.mark.parametrize("minutes, taken", [(31, True), (24 * 60, True), (5, False), (0, False)])
def test_a_folder_lock_with_no_owner_is_taken_over_only_when_it_is_over_half_an_hour_old(
        tmp_path, no_flock_env, minutes, taken):
    lock = tmp_path / "install.lock"
    folder = tmp_path / "install.lock.d"
    folder.mkdir()
    then = time.time() - minutes * 60
    os.utime(folder, (then, then))
    run = _take(lock, no_flock_env)
    if taken:
        assert run.returncode == 0 and "stale install lock" in run.stderr
        assert not folder.exists()  # taken, and removed again when that install ended
    else:
        assert run.returncode == 1 and "already being installed" in run.stderr
        assert "stale" not in run.stderr and folder.is_dir()


def test_a_folder_lock_whose_owner_has_died_is_taken_over_at_once(tmp_path, no_flock_env):
    lock = tmp_path / "install.lock"
    folder = tmp_path / "install.lock.d"
    gone = subprocess.Popen([SH, "-c", "exit 0"])
    gone.wait(timeout=10)
    folder.mkdir()
    (folder / "owner").write_bytes(f"{gone.pid}\n".encode())  # fresh, but nobody is running it
    run = _take(lock, no_flock_env)
    assert run.returncode == 0 and "stale install lock" in run.stderr
    assert not folder.exists()  # taken, and removed again when that install ended


def test_a_folder_lock_whose_owner_is_running_is_never_taken_over(tmp_path, no_flock_env):
    lock = tmp_path / "install.lock"
    folder = tmp_path / "install.lock.d"
    folder.mkdir()
    (folder / "owner").write_bytes(f"{os.getpid()}\n".encode())  # this test's own process: alive
    then = time.time() - 24 * 60 * 60
    os.utime(folder, (then, then))
    run = _take(lock, no_flock_env)
    assert run.returncode == 1 and "already being installed" in run.stderr
    assert "stale" not in run.stderr
    assert folder.is_dir() and (folder / "owner").read_bytes() == f"{os.getpid()}\n".encode()


@pytest.mark.parametrize("minutes", [0, 24 * 60])
def test_a_folder_lock_whose_owner_is_not_a_pid_is_left_alone(tmp_path, no_flock_env, minutes):
    lock = tmp_path / "install.lock"
    folder = tmp_path / "install.lock.d"
    folder.mkdir()
    (folder / "owner").write_bytes(b"abc\n")
    then = time.time() - minutes * 60
    os.utime(folder, (then, then))
    run = _take(lock, no_flock_env)
    assert run.returncode == 1 and "already being installed" in run.stderr
    assert "stale" not in run.stderr and folder.is_dir()


def test_the_exit_trap_leaves_a_lock_another_process_now_owns(tmp_path, no_flock_env):
    folder = tmp_path / "install.lock.d"
    with _holding(tmp_path / "install.lock", no_flock_env) as holder:
        (folder / "owner").write_bytes(f"{os.getpid()}\n".encode())  # taken over while it ran
        holder.stdin.write("\n")
        holder.stdin.flush()
        assert holder.wait(timeout=10) == 0
    assert folder.is_dir()
    assert (folder / "owner").read_bytes() == f"{os.getpid()}\n".encode()


def test_ixel_updates_lock_keeps_install_sh_out_without_flock(tmp_path, no_flock_env, monkeypatch):
    """With no flock command install.sh can't see `ixel update`'s flock on install.lock: it sees the folder."""
    from ixel_mat import update
    monkeypatch.setattr(update, "INSTALL_INFO", tmp_path / "install.json")
    lock = tmp_path / "install.lock"
    folder = tmp_path / "install.lock.d"
    with update._install_lock() as free:
        assert free is True
        assert (folder / "owner").read_text(encoding="utf-8").strip() == str(os.getpid())
        refused = _take(lock, no_flock_env)
        assert refused.returncode == 1 and "already being installed" in refused.stderr
        assert folder.is_dir()  # the refused install left it alone
    assert not folder.exists()
    assert _take(lock, no_flock_env).returncode == 0


def test_ixel_update_tells_the_installer_it_already_holds_the_lock(tmp_path, no_flock_env):
    lock = tmp_path / "install.lock"
    folder = tmp_path / "install.lock.d"
    ran = _take(lock, _env(IXEL_INSTALL_LOCK_HELD="1"))
    assert ran.returncode == 0 and ran.stderr == ""
    assert not lock.exists() and not folder.exists()  # nothing taken

    folder.mkdir()  # a fresh one, which is not this install's to refuse or to remove
    ran = _take(lock, {**no_flock_env, "IXEL_INSTALL_LOCK_HELD": "1"})
    assert ran.returncode == 0 and folder.is_dir()
    refused = _take(lock, {**no_flock_env, "IXEL_INSTALL_LOCK_HELD": "0"})
    assert refused.returncode == 1 and folder.is_dir()


def test_ixel_update_sees_the_folder_an_install_without_flock_holds(tmp_path, no_flock_env, monkeypatch):
    from ixel_mat import update
    monkeypatch.setattr(update, "INSTALL_INFO", tmp_path / "install.json")
    assert not update._still_installing()
    with _holding(tmp_path / "install.lock", no_flock_env) as holder:
        assert update._still_installing()
        holder.stdin.write("\n")
        holder.stdin.flush()
        assert holder.wait(timeout=10) == 0
    assert not update._still_installing()


# ── The window packages ───────────────────────────────────────────────────────

def _sudo() -> str:
    return "" if os.geteuid() == 0 else "sudo "


def _offer_env(tmp_path: Path, release: str | None = "ID=ubuntu\nID_LIKE=debian\n", **extra: str) -> dict:
    """A fake os-release (none at all when `release` is None), and fake installers first on PATH that only
    record that they were run."""
    fake = tmp_path / "fake-bin"
    fake.mkdir(exist_ok=True)
    for name in ("sudo", "apt", "dnf", "pacman", "zypper"):
        tool = fake / name
        tool.write_text('#!/bin/sh\necho "${0##*/} $*" >> "$IXEL_TEST_RECORD"\nexit "${IXEL_TEST_EXIT:-0}"\n',
                        encoding="utf-8")
        tool.chmod(0o755)
    release_file = tmp_path / "os-release"
    if release is not None:
        release_file.write_text(release, encoding="utf-8")
    return {"PATH": f"{fake}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "OS_RELEASE": str(release_file), "IXEL_TEST_RECORD": str(tmp_path / "ran.txt"), **extra}


def _offer_script(ready: bool = True, strict: bool = False) -> str:
    """offer_window_packages, with window_ready standing in for the installed Ixel."""
    return (("set -e\n" if strict else "") + _function("offer_window_packages")
            + f"\nwindow_ready() {{ return {0 if ready else 1}; }}\noffer_window_packages\necho done\n")


def _ran(tmp_path: Path) -> list[str]:
    record = tmp_path / "ran.txt"
    return record.read_text(encoding="utf-8").splitlines() if record.exists() else []


def _at_terminal(script: str, env: dict, answer: str) -> subprocess.CompletedProcess:
    """The script with a real terminal (a pseudo-terminal) as its stdin, where `answer` has been typed."""
    import pty
    try:
        master, slave = pty.openpty()
    except OSError:
        pytest.skip("no pseudo-terminal here")
    try:
        os.write(master, answer.encode("utf-8"))
        return subprocess.run([SH, "-c", script, "sh"], stdin=slave, capture_output=True, text=True,
                              encoding="utf-8", env=env, timeout=60)
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("release, words", [
    ("ID=ubuntu\n", APT),
    ("ID=debian\n", APT),
    ('ID=linuxmint\nID_LIKE="ubuntu debian"\n', APT),
    ("ID=fedora\n", "dnf install python3-gobject webkit2gtk4.1"),
    ('ID="rocky"\nID_LIKE="rhel centos fedora"\n', "dnf install python3-gobject webkit2gtk4.1"),
    ("ID=arch\n", "pacman -S python-gobject webkit2gtk-4.1"),
    ("ID=manjaro\nID_LIKE=arch\n", "pacman -S python-gobject webkit2gtk-4.1"),
    ('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n', "zypper install python3-gobject typelib-1_0-WebKit2-4_1"),
    ('ID="opensuse-leap"\n', "zypper install python3-gobject typelib-1_0-WebKit2-4_1"),
])
def test_the_package_line_follows_the_distro(tmp_path, release, words):
    run = _run(_offer_script(), env=_offer_env(tmp_path, release))
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [INTRO, f"  Install them with:  {_sudo()}{words}", "done"]
    assert "webkit-6.0" not in run.stdout and "webkitgtk6.0" not in run.stdout  # GTK 4: Ixel's window is GTK 3
    assert _ran(tmp_path) == []


@pytest.mark.parametrize("release", ["ID=gentoo\n", "", None], ids=["gentoo", "empty", "missing"])
def test_an_unknown_distro_gets_the_generic_line(tmp_path, release):
    run = _run(_offer_script(), env=_offer_env(tmp_path, release))
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == [INTRO, GENERIC, "done"]
    assert _ran(tmp_path) == []


def test_without_a_terminal_it_only_prints(tmp_path):
    env = _offer_env(tmp_path)
    for input in (None, "y\n"):  # nothing at all, and a pipe that says yes (which isn't a terminal)
        run = _run(_offer_script(), env=env, input=input)
        assert run.returncode == 0, run.stderr
        assert f"  Install them with:  {_sudo()}{APT}" in run.stdout and "Install them now?" not in run.stdout
        assert _ran(tmp_path) == []


@pytest.mark.parametrize("answer", ["\n", "n\n", "N\n", "no\n", "yy\n", "sure\n"])
def test_at_a_terminal_anything_but_yes_runs_nothing(tmp_path, answer):
    env = _offer_env(tmp_path)
    run = _at_terminal(_offer_script(), env, answer)
    assert run.returncode == 0, run.stderr
    assert "Install them now? [y/N] " in run.stdout
    assert _ran(tmp_path) == []


@pytest.mark.parametrize("answer", ["y\n", "Y\n", "yes\n", "Yes\n"])
def test_yes_runs_the_line_once(tmp_path, answer):
    env = _offer_env(tmp_path)
    run = _at_terminal(_offer_script(), env, answer)
    assert run.returncode == 0, run.stderr
    assert _ran(tmp_path) == [f"{_sudo()}{APT}"]
    assert "Ixel's own window is ready." in run.stdout


def test_after_yes_it_says_when_the_window_still_isnt_found(tmp_path):
    env = _offer_env(tmp_path)
    run = _at_terminal(_offer_script(ready=False), env, "y\n")
    assert run.returncode == 0, run.stderr
    assert _ran(tmp_path) == [f"{_sudo()}{APT}"]
    assert "still wasn't found" in run.stdout and "`ixel app` opens in your browser instead" in run.stdout


def test_a_package_install_that_fails_warns_and_the_installer_goes_on(tmp_path):
    env = _offer_env(tmp_path, IXEL_TEST_EXIT="1")
    run = _at_terminal(_offer_script(strict=True), env, "y\n")  # set -e, as in install.sh
    assert run.returncode == 0, run.stderr
    assert run.stdout.endswith("done\n")
    assert "didn't finish" in run.stderr and "`ixel app` opens in your browser instead" in run.stderr


def test_it_never_asks_during_ixel_update(tmp_path):
    env = _offer_env(tmp_path, IXEL_INSTALL_LOCK_HELD="1")
    run = _at_terminal(_offer_script(), env, "y\n")
    assert run.returncode == 0, run.stderr
    assert f"  Install them with:  {_sudo()}{APT}" in run.stdout  # still said
    assert "Install them now?" not in run.stdout
    assert _ran(tmp_path) == []


def test_window_ready_asks_the_installed_python(tmp_path):
    venv = tmp_path / "venv"
    python = venv / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$IXEL_TEST_RECORD"\nexit "${IXEL_TEST_EXIT:-0}"\n',
                      encoding="utf-8")
    python.chmod(0o755)
    script = _function("window_ready") + "\nif window_ready; then echo ready; else echo not ready; fi\n"
    for code, said in (("0", "ready"), ("1", "not ready")):
        run = _run(script, env=_env(VENV_DIR=str(venv), IXEL_TEST_RECORD=str(tmp_path / "argv.txt"),
                                    IXEL_TEST_EXIT=code))
        assert run.returncode == 0, run.stderr
        assert run.stdout.strip() == said
    argv = (tmp_path / "argv.txt").read_text(encoding="utf-8").splitlines()
    assert argv[:2] == ["-I", "-c"] and "linux_window_command" in argv[2]


# ── The script itself ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["take_install_lock", "window_ready", "offer_window_packages"])
def test_the_new_functions_are_plain_posix_sh(name):
    """install.sh is bash, but these run under dash too: the tests above do, and so can anyone."""
    body = _function(name)
    for bashism in ("[[", "local ", "<<<", "$'", "function ", "read -p", "echo -n", "echo -e", "eval ", "==", "(("):
        assert bashism not in body, (name, bashism)


def test_the_lock_is_taken_before_anything_is_installed():
    text = INSTALL_SH.read_text(encoding="utf-8")
    call = text.index('take_install_lock "$INSTALL_ROOT/install.lock"')  # the file `ixel update` locks
    assert text.index("take_install_lock() {") < call
    assert text.index('mkdir -p "$INSTALL_ROOT"') < call
    for step in ("git clone", "git -C", "uv venv", "-m venv", "pip install", 'cat > "$WRAPPER_PATH"'):
        assert call < text.index(step), step


def test_the_window_packages_are_offered_last_and_never_from_install_app():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "offer_window_packages" not in _function("install_app")  # tests run that with a terminal as stdin
    calls = [m.start() for m in re.finditer(r"^[ \t]+offer_window_packages$", text, re.M)]
    assert len(calls) == 1
    assert text.index('"$INSTALL_ROOT/install.json"') < calls[0]
    guard = text[text.rindex("\nif ", 0, calls[0]):calls[0]]
    assert '"Linux"' in guard and "! window_ready" in guard
