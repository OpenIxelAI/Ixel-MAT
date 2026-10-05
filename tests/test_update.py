"""`ixel update` and the once-a-day "update available" notice, against real git checkouts."""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from ixel_mat import update

GIT_ID = ["-c", "user.name=Ixel Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]


def git(cwd, *args):
    return subprocess.run(["git", *GIT_ID, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def commit(repo, name):
    (repo / name).write_text(name, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", name)


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """An install's checkout (clone) of an upstream it can pull from; returns (upstream, clone)."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "main")
    commit(upstream, "one")
    clone = tmp_path / "ixel-mat"
    git(tmp_path, "clone", "-q", str(upstream), str(clone))
    info = tmp_path / "install.json"
    info.write_text(json.dumps({"source": str(clone), "install_root": str(tmp_path / "root"),
                                "bin_dir": str(tmp_path / "bin"), "installer": "install.sh"}), encoding="utf-8")
    monkeypatch.setattr(update, "INSTALL_INFO", info)
    monkeypatch.setattr(update, "CHECK_FILE", tmp_path / "update_check.json")
    monkeypatch.delenv("IXEL_NO_UPDATE_CHECK", raising=False)
    return upstream, clone


def test_install_info(tmp_path):
    path = tmp_path / "install.json"
    assert update.install_info(path) is None
    path.write_text("not json", encoding="utf-8")
    assert update.install_info(path) is None
    path.write_text(json.dumps({"install_root": "x"}), encoding="utf-8")
    assert update.install_info(path) is None
    path.write_text(json.dumps({"source": "/src"}), encoding="utf-8")
    assert update.install_info(path) == {"source": "/src"}


def test_notice_is_checked_once_a_day(checkout):
    upstream, _ = checkout
    now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
    assert update.update_notice(now=now) is None
    commit(upstream, "two")
    # Checked an hour ago: not asked again yet
    assert update.update_notice(now=now + timedelta(hours=1)) is None
    notice = update.update_notice(now=now + timedelta(days=1))
    assert "1 new change)" in notice and "ixel update" in notice
    commit(upstream, "three")
    assert "1 new change)" in update.update_notice(now=now + timedelta(days=1, hours=2))
    assert "2 new changes" in update.update_notice(now=now + timedelta(days=2, hours=1))


def test_notice_stays_quiet_when_offline(checkout):
    _, clone = checkout
    git(clone, "remote", "set-url", "origin", str(clone.parent / "gone"))
    assert update.update_notice() is None
    assert json.loads(update.CHECK_FILE.read_text(encoding="utf-8"))["behind"] is None


def test_checks_can_be_turned_off(checkout, monkeypatch):
    assert update.checks_enabled({})
    assert not update.checks_enabled({"updates": {"check": False}})
    assert update.start_background_notice({"updates": {"check": False}})(wait=5) is None
    monkeypatch.setenv("IXEL_NO_UPDATE_CHECK", "1")
    assert not update.checks_enabled({})


def test_background_notice(checkout):
    upstream, _ = checkout
    commit(upstream, "two")
    ready = update.start_background_notice({})
    assert "1 new change" in ready(wait=30)
    assert ready() is None  # shown once


def test_update_pulls_then_reinstalls(checkout, monkeypatch):
    upstream, clone = checkout
    reinstalled, said = [], []
    monkeypatch.setattr(update, "_reinstall", lambda info, say: reinstalled.append(info) or 0)
    assert update.run_update([], say=said.append) == 0
    assert reinstalled == [] and "already up to date" in said[-1]

    commit(upstream, "two")
    assert update.run_update(["--check"], say=said.append) == 0 and "1 new change available" in said[-1]
    update.CHECK_FILE.write_text("{}", encoding="utf-8")
    assert update.run_update([], say=said.append) == 0
    assert (clone / "two").exists() and reinstalled[0]["source"] == str(clone)
    assert not update.CHECK_FILE.exists()  # the next notice starts fresh
    assert update.run_update(["--force"], say=said.append) == 0 and len(reinstalled) == 2


def test_update_installs_again_when_the_last_install_did_not_finish(checkout, monkeypatch):
    """The installer records the commit it installed once it's done, so a pull whose install failed
    (or, on Windows, whose install window was closed) is installed by the next update."""
    upstream, clone = checkout
    reinstalled, said = [], []
    outcome = {"code": 0}
    monkeypatch.setattr(update, "_reinstall", lambda info, say: reinstalled.append(info) or outcome["code"])
    record = json.loads(update.INSTALL_INFO.read_text(encoding="utf-8"))

    def recorded(commit_id):
        update.INSTALL_INFO.write_text(json.dumps({**record, "commit": commit_id} if commit_id else record),
                                       encoding="utf-8")

    recorded(git(clone, "rev-parse", "HEAD").strip())
    assert update.run_update([], say=said.append) == 0
    assert reinstalled == [] and "already up to date" in said[-1]  # what's installed is what's checked out

    commit(upstream, "two")
    outcome["code"] = 1  # the pull works, the install doesn't (so the record isn't updated)
    assert update.run_update([], say=said.append) == 1 and len(reinstalled) == 1 and (clone / "two").exists()
    outcome["code"] = 0
    assert update.run_update([], say=said.append) == 0 and len(reinstalled) == 2  # nothing new pulled
    assert "didn't finish installing" in said[-1]

    recorded(None)  # an install from before commits were recorded: only a pull that moves HEAD reinstalls
    assert update.run_update([], say=said.append) == 0
    assert len(reinstalled) == 2 and "already up to date" in said[-1]


def test_update_waits_for_an_install_still_running(checkout, monkeypatch):
    """On Windows the install finishes in its own window: running `ixel update` again meanwhile must not
    pull or start a second installer into the same virtualenv."""
    upstream, clone = checkout
    reinstalled, said = [], []
    monkeypatch.setattr(update, "_reinstall", lambda info, say: reinstalled.append(info) or 0)
    record = json.loads(update.INSTALL_INFO.read_text(encoding="utf-8"))
    update.INSTALL_INFO.write_text(json.dumps({**record, "commit": git(clone, "rev-parse", "HEAD").strip()}),
                                   encoding="utf-8")
    commit(upstream, "two")
    assert update.run_update([], say=said.append) == 0 and len(reinstalled) == 1  # the window opens
    assert update.run_update(["--check"], say=said.append) == 0 and "didn't finish installing" in said[-1]

    monkeypatch.setattr(update, "_still_installing", lambda: True)  # and is still installing
    commit(upstream, "three")
    for argv in ([], ["--force"]):
        assert update.run_update(argv, say=said.append) == 0
        assert len(reinstalled) == 1 and not (clone / "three").exists() and "still installing" in said[-1]
    git(clone, "pull", "-q", "--ff-only")
    assert update.run_update(["--check"], say=said.append) == 0 and "still installing" in said[-1]


def test_an_install_holds_its_lock_until_it_is_done(tmp_path, monkeypatch):
    """install.ps1 keeps install.lock open, unshared: Windows then refuses to open it for anyone else."""
    monkeypatch.setattr(update, "INSTALL_INFO", tmp_path / "install.json")
    assert not update._windows_installing()  # no install since locks were added
    (tmp_path / "install.lock").write_bytes(b"")
    assert not update._windows_installing()  # the installer that held it is done

    def sharing_violation(path, mode="r", *args, **kwargs):
        raise PermissionError(13, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(update, "open", sharing_violation, raising=False)
    assert update._windows_installing()
    from pathlib import Path
    ps1 = (Path(__file__).resolve().parent.parent / "install.ps1").read_text(encoding="utf-8")
    lock = ps1.index("[IO.File]::Open((Join-Path $InstallRoot 'install.lock'), 'OpenOrCreate', 'ReadWrite', 'None')")
    assert lock < ps1.index("if ($WaitForPid) {")  # held while it waits, too
    assert ps1.index("Recording the install") < ps1.rindex("$InstallLock.Dispose()") < ps1.rindex("Read-Host")
    assert "if ($InstallLock) { $InstallLock.Dispose() }" in ps1[:ps1.index("break")]  # let go when it fails


@pytest.mark.skipif(os.name == "nt", reason="Windows locks through install.ps1 (the test above)")
def test_a_second_update_waits_while_the_first_installs(checkout, monkeypatch):
    """macOS and Linux: an update holds install.lock while it pulls and installs, so a second one started
    meanwhile (in another terminal) says so instead of installing into the same virtualenv."""
    upstream, clone = checkout
    commit(upstream, "two")
    lock = update.INSTALL_INFO.with_name("install.lock")
    holder = subprocess.Popen([sys.executable, "-c", "import fcntl, sys, time\n"
                               f"f = open({str(lock)!r}, 'a+b'); fcntl.flock(f, fcntl.LOCK_EX)\n"
                               "print('held', flush=True); time.sleep(60)"], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        reinstalled, said = [], []
        monkeypatch.setattr(update, "_reinstall", lambda info, say: reinstalled.append(info) or 0)
        assert update._still_installing()
        assert update.run_update([], say=said.append) == 0 and "still installing" in said[-1]
        assert not reinstalled and not (clone / "two").exists()
    finally:
        holder.kill()
        holder.wait()
    assert not update._still_installing()
    assert update.run_update([], say=said.append) == 0 and len(reinstalled) == 1


@pytest.mark.skipif(os.name == "nt", reason="Windows locks through install.ps1")
def test_a_file_system_without_locks_doesnt_block_updates(checkout, monkeypatch):
    import errno
    import fcntl

    def no_locks(handle, how):
        raise OSError(errno.ENOLCK, "No locks available")
    monkeypatch.setattr(fcntl, "flock", no_locks)
    assert not update._still_installing()


def test_commits_of_your_own_with_nothing_new_still_update(checkout, monkeypatch):
    """They're only in the way of something new to bring in on top."""
    upstream, clone = checkout
    commit(clone, "mine")
    reinstalled, said = [], []
    monkeypatch.setattr(update, "_reinstall", lambda info, say: reinstalled.append(info) or 0)
    assert update.run_update(["--force"], say=said.append) == 0 and len(reinstalled) == 1
    assert not any("of its own" in line for line in said)


def test_a_copy_with_commits_of_its_own_is_told_why_it_cant_update(checkout, monkeypatch):
    upstream, clone = checkout
    commit(upstream, "two")
    commit(clone, "mine")
    said = []
    monkeypatch.setattr(update, "_reinstall", lambda info, say: pytest.fail("installed anyway"))
    assert update.run_update(["--check"], say=said.append) == 0
    assert "1 new change available, but" in said[-1] and "1 commit of its own" in said[-1]
    assert update.run_update([], say=said.append) == 1 and "reset --keep @{upstream}" in said[-1]


def test_local_changes_are_put_aside_not_committed(checkout, monkeypatch):
    _, clone = checkout
    (clone / "one").write_text("edited", encoding="utf-8")
    said = []
    assert update.run_update([], say=said.append) == 1
    assert "stash" in said[-1] and "Commit" not in said[-1]


def test_a_check_cut_short_still_counts_as_todays(checkout, monkeypatch):
    """A session that ends while the network is slow mustn't fetch again at the next start."""
    monkeypatch.setattr(update, "_behind", lambda info: (_ for _ in ()).throw(SystemExit))  # the process ends
    now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
    with pytest.raises(SystemExit):
        update.update_notice(now=now)
    monkeypatch.setattr(update, "_behind", lambda info: pytest.fail("fetched again"))
    assert update.update_notice(now=now + timedelta(hours=1)) is None


def _powershell_string(text, variables):
    """The value of a double-quoted PowerShell string: its `escapes and $variables expanded."""
    return re.sub(r"`(.)|\$(\w+)", lambda m: variables.get(m.group(2), m.group(0)) if m.group(2)
                  else {"r": "\r", "n": "\n"}.get(m.group(1), m.group(1)), text)


def test_the_windows_command_runs_python_not_ixel_exe():
    """Smart App Control blocks the unsigned ixel.exe pip writes, so ixel.cmd runs the environment's python.exe."""
    from pathlib import Path
    ps1 = (Path(__file__).resolve().parent.parent / "install.ps1").read_text(encoding="utf-8")
    assert "$VenvPython = Join-Path $VenvDir 'Scripts\\python.exe'" in ps1
    line = next(line for line in ps1.splitlines() if line.startswith("Set-Content -Path $CmdWrapper"))
    value = re.fullmatch(r'Set-Content -Path \$CmdWrapper -Encoding ASCII -Value "(.*)"', line).group(1)
    python = r"%LOCALAPPDATA%\IxelMAT\.venv\Scripts\python.exe"
    # -I: nothing imported from the folder ixel runs in; %*: every argument; the last line's exit code is ixel's
    assert _powershell_string(value, {"WrapperPython": python}).split("\r\n") == [
        "@echo off", f'"{python}" -I -m ixel_mat %*']
    assert "IxelExe" not in line
    # The file is ASCII, so C:\Users\José's folder would be Jos?: under %LOCALAPPDATA%, cmd fills in the path
    choose = ps1[ps1.index("$WrapperPython = $VenvPython\n"):ps1.index(line)]
    assert ("if ($env:LOCALAPPDATA -and $VenvPython.StartsWith($env:LOCALAPPDATA + '\\', "
            "[StringComparison]::OrdinalIgnoreCase)) {") in choose
    assert "    $WrapperPython = '%LOCALAPPDATA%' + $VenvPython.Substring($env:LOCALAPPDATA.Length)\n}" in choose
    assert ps1.isascii()  # Windows PowerShell 5.1 reads a .ps1 with no BOM in the ANSI code page
    assert "& $VenvPython -I -c 'import ixel_mat." in ps1  # the install checks Ixel loads that way


def test_an_update_waits_for_every_ixel_of_this_install():
    """An open ixel window or an app running the plugin is this install's python.exe, the Start Menu's
    Ixel is its pythonw.exe; an older ixel.cmd ran ixel.exe."""
    from pathlib import Path
    ps1 = (Path(__file__).resolve().parent.parent / "install.ps1").read_text(encoding="utf-8")
    finder = ps1[ps1.index("function Get-IxelProcesses {"):ps1.index("if ($WaitForPid) {")]
    assert "$_.Path -eq $VenvPython -or $_.Path -eq $VenvPythonW -or $_.Path -eq $IxelExe" in finder
    assert ps1.index("$VenvPythonW = Join-Path $VenvDir 'Scripts\\pythonw.exe'") < ps1.index("function Get-IxelProcesses")
    assert ps1.index("$VenvPython = Join-Path") < ps1.index("function Get-IxelProcesses")
    waiting = ps1[ps1.index("if ($WaitForPid) {"):ps1.index("function Require-Command")]
    assert waiting.count("@(Get-IxelProcesses)") == 3 and "Get-Process" not in waiting


def test_the_start_menu_opens_ixel_app_through_signed_pythonw():
    """Smart App Control blocks unsigned launchers; pythonw.exe is signed and opens no console window."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    ps1 = (root / "install.ps1").read_text(encoding="utf-8")
    entry = ps1[ps1.index("$Programs = [Environment]::GetFolderPath('Programs')"):]
    entry = entry[:entry.index("$Lnk.Save()")]
    assert "$Lnk.TargetPath = $VenvPythonW" in entry and "$Lnk.Arguments = '-I -m ixel_mat app'" in entry
    # Never over an Ixel.lnk that isn't Ixel's (one Ixel's arguments): beside one, it's Ixel MAT.lnk
    assert "$WShell.CreateShortcut($Path).Arguments -eq '-I -m ixel_mat app'" in entry
    assert "$Shortcut = Join-Path $Programs 'Ixel.lnk'" in entry and "$Beside = Join-Path $Programs 'Ixel MAT.lnk'" in entry
    assert "-not (Test-IxelShortcut $Shortcut)" in entry and "$Lnk = $WShell.CreateShortcut($Shortcut)" in entry
    assert "$env:IXEL_SKIP_APP_ENTRY -ne '1'" in entry  # scripts/check_windows.py's installs leave it alone
    icon = re.search(r"\$Icon = Join-Path \$VenvDir '([^']+)'", entry).group(1)
    assert icon == r"Lib\site-packages\ixel_mat\assets\ixel.ico" and (root / "ixel_mat" / "assets" / "ixel.ico").is_file()
    assert "assets/*.ico" in (root / "pyproject.toml").read_text(encoding="utf-8")  # installed with the package


def test_installers_record_the_commit_last():
    """Only a finished install notes its commit, so one that failed is installed again next time."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for name, last_step, lookup in (("install.sh", 'chmod +x "$WRAPPER_PATH"', 'git -C "$SOURCE_DIR" rev-parse'),
                                    ("install.ps1", "Ensure-UserPath $BinDir", "$Commit = Get-Commit $SourceDir")):
        text = (root / name).read_text(encoding="utf-8")
        record = text.index("install.json")
        assert text.index(last_step) < text.index(lookup) < record, name
        assert "zip([" in text[record - 400:record] and "commit" in text[record - 400:record], name


def test_update_refuses_local_changes(checkout, monkeypatch):
    upstream, clone = checkout
    commit(upstream, "two")
    monkeypatch.setattr(update, "_reinstall", lambda info, say: pytest.fail("reinstalled"))
    (clone / "one").write_text("my edit", encoding="utf-8")
    said = []
    assert update.run_update([], say=said.append) == 1
    assert "local changes" in said[-1] and not (clone / "two").exists()


def test_update_without_an_installer_record(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "INSTALL_INFO", tmp_path / "missing.json")
    said = []
    assert update.run_update([], say=said.append) == 1 and "the way you installed it" in said[0]
    assert update.start_background_notice({})(wait=1) is None


def test_update_explains_a_branch_that_cannot_fast_forward(checkout, monkeypatch):
    upstream, clone = checkout
    git(clone, "switch", "-q", "-c", "old-branch")
    git(clone, "branch", "-q", "--set-upstream-to", "origin/main")
    commit(clone, "mine")
    commit(upstream, "theirs")
    monkeypatch.setattr(update, "_reinstall", lambda info, say: pytest.fail("reinstalled"))
    said = []
    assert update.run_update([], say=said.append) == 1
    assert "of its own that GitHub doesn't have" in said[-2] and "switch main" in said[-1]


def test_update_needs_a_branch_to_follow(checkout):
    _, clone = checkout
    git(clone, "switch", "-q", "--detach")
    said = []
    assert update.run_update(["--check"], say=said.append) == 1 and "switch main" in said[-1]


def test_update_without_git(checkout, monkeypatch):
    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(update.subprocess, "run", no_git)
    said = []
    assert update.run_update([], say=said.append) == 1 and "git isn't installed" in said[-1]


def test_help_and_unknown_options_change_nothing(checkout, monkeypatch, capsys):
    monkeypatch.setattr(update, "_run_update", lambda *a: pytest.fail("updated"))
    assert update.run_update(["--help"]) == 0 and "--check" in capsys.readouterr().out
    assert update.run_update(["--chek"]) == 2 and "unrecognized" in capsys.readouterr().err
    assert update.run_update(["now"]) == 2


@pytest.mark.skipif(os.name == "nt", reason="credential helper and askpass stubs are POSIX shell scripts")
def test_background_check_never_asks_for_credentials(checkout, tmp_path, monkeypatch):
    """A remote that wants a password: no helper, askpass program or prompt may run."""
    import http.server
    import threading

    class NeedsLogin(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="private"')
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NeedsLogin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _, clone = checkout
    asked = tmp_path / "asked"
    stub = tmp_path / "ask.sh"
    stub.write_text(f"#!/bin/sh\necho \"$0 $*\" >> {asked}\necho secret\n", encoding="utf-8")
    stub.chmod(0o755)
    try:
        git(clone, "remote", "set-url", "origin", f"http://127.0.0.1:{server.server_port}/ixel-mat.git")
        git(clone, "config", "credential.helper", f"!{stub}")
        git(clone, "config", "core.askPass", str(stub))
        monkeypatch.setenv("GIT_ASKPASS", str(stub))
        monkeypatch.setenv("SSH_ASKPASS", str(stub))
        assert update.commits_behind(str(clone)) is None
    finally:
        server.shutdown()
    assert not asked.exists(), asked.read_text()


def test_reinstall_runs_the_installer_it_came_from(tmp_path, monkeypatch):
    info = {"source": str(tmp_path), "install_root": str(tmp_path / "root"), "bin_dir": str(tmp_path / "bin")}
    calls = []
    if os.name == "nt":
        # python.exe (or ixel.exe) can't be replaced while it runs: a new window waits for this process to exit
        monkeypatch.setattr(update.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
        assert update._reinstall(info, say=lambda line: None) == 0
        cmd, kw = calls[0]
        assert cmd[cmd.index("-File") + 1] == str(tmp_path / "install.ps1")
        assert cmd[cmd.index("-WaitForPid") + 1] == str(os.getpid()) and "-PauseAtEnd" in cmd
        assert kw["creationflags"] & update.subprocess.CREATE_NEW_CONSOLE
    else:
        monkeypatch.setattr(update.subprocess, "run",
                            lambda cmd, **kw: calls.append((cmd, kw)) or subprocess.CompletedProcess(cmd, 0))
        assert update._reinstall(info, say=lambda line: None) == 0
        cmd, kw = calls[0]
        assert os.path.basename(cmd[0]) == "bash" and os.path.isabs(cmd[0]) and cmd[1:] == [str(tmp_path / "install.sh")]
    env = kw["env"]
    assert (env["IXEL_INSTALL_ROOT"], env["IXEL_BIN_DIR"], env["IXEL_SKIP_PATH_UPDATE"]) == (
        info["install_root"], info["bin_dir"], "1")


# ── Private repositories: saved logins yes, prompts never ─────────────────────

@pytest.mark.parametrize("helper,quiet", [
    ("manager", True), ("manager-core", True), ("osxkeychain", True), ("store --file ~/.creds", True),
    ("cache --timeout 3600", True), ("/usr/lib/git-core/git-credential-libsecret", True),
    ('"C:/Program Files/Git/mingw64/bin/git-credential-manager.exe"', True),
    ("!gh auth git-credential", True), ("", True),
    ("!/tmp/ask-me.sh", False), ("!f() { echo password=x; }; f", False), ("my-prompting-helper", False),
])
def test_which_credential_helpers_count_as_quiet(helper, quiet):
    assert update._quiet_helper(helper) is quiet


def _needs_login_server(seen):
    import http.server
    import threading

    class NeedsLogin(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="private"')
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NeedsLogin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_a_saved_login_is_used_for_a_private_repository(checkout, tmp_path):
    # A private repo needs a login: one the credential store already has is fine to use
    _, clone = checkout
    seen = []
    server = _needs_login_server(seen)
    url = f"http://127.0.0.1:{server.server_port}/ixel-mat.git"
    creds = tmp_path / "creds"
    # Bytes, not write_text: on Windows that writes \r\n, and git's store reads lines split on \n only
    creds.write_bytes(f"http://me:saved-token@127.0.0.1:{server.server_port}\n".encode())
    try:
        git(clone, "remote", "set-url", "origin", url)
        # Only this helper: an empty entry clears the machine's own (Windows runners have Git
        # Credential Manager, which won't serve a plain-http test server). Forward slashes,
        # because git runs helpers through a shell, which would eat Windows' backslashes.
        git(clone, "config", "--add", "credential.helper", "")
        git(clone, "config", "--add", "credential.helper", f"store --file={creds.as_posix()}")
        fetched = update._git(str(clone), "fetch", quiet=True, timeout=20)
    finally:
        server.shutdown()
    assert any(h and h.startswith("Basic ") for h in seen), (seen, fetched.stderr)


@pytest.mark.skipif(os.name == "nt", reason="the helper stub is a POSIX shell script")
def test_a_per_site_prompting_helper_is_switched_off_too(checkout, tmp_path):
    _, clone = checkout
    seen, asked = [], tmp_path / "asked"
    server = _needs_login_server(seen)
    stub = tmp_path / "ask.sh"
    stub.write_text(f"#!/bin/sh\necho \"$*\" >> {asked}\necho password=typed\n", encoding="utf-8")
    stub.chmod(0o755)
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        git(clone, "remote", "set-url", "origin", f"{url}/ixel-mat.git")
        git(clone, "config", "credential.helper", "cache")  # quiet, so it stays
        git(clone, "config", f"credential.{url}.helper", f"!{stub}")
        assert update.commits_behind(str(clone)) is None
    finally:
        server.shutdown()
    assert not asked.exists()


# ── Installed with pipx / uv / pip from git ───────────────────────────────────

DIRECT_URL = json.dumps({"url": "https://github.com/OpenIxelAI/ixel-mat.git",
                         "vcs_info": {"vcs": "git", "commit_id": "a" * 40, "requested_revision": "main"}})


@pytest.mark.parametrize("marker,kind", [("pipx_metadata.json", "pipx"), ("uv-receipt.toml", "uv"), (None, "pip")])
def test_git_installs_are_recognized(tmp_path, marker, kind):
    if marker:
        (tmp_path / marker).write_text("{}", encoding="utf-8")
    assert update.git_install(tmp_path, DIRECT_URL) == {
        "kind": kind, "url": "https://github.com/OpenIxelAI/ixel-mat.git", "commit": "a" * 40, "ref": "main"}


def test_other_installs_are_not_git_installs(tmp_path):
    editable = json.dumps({"url": "file:///home/me/ixel-mat", "dir_info": {"editable": True}})
    for direct_url in (editable, "not json", "", json.dumps({"vcs_info": {"vcs": "hg"}, "url": "x"})):
        assert update.git_install(tmp_path, direct_url) is None


@pytest.fixture
def git_installed(checkout, monkeypatch):
    """Ixel "installed with pipx" at the upstream's current commit."""
    upstream, _ = checkout
    head = git(upstream, "rev-parse", "HEAD").strip()
    install = {"kind": "pipx", "url": upstream.as_uri(), "commit": head, "ref": "HEAD"}
    monkeypatch.setattr(update, "INSTALL_INFO", upstream / "no-install.json")
    monkeypatch.setattr(update, "git_install", lambda *a, **k: dict(install))
    return upstream, install


def test_git_install_notice_and_check(git_installed, monkeypatch):
    upstream, _ = git_installed
    now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
    assert update.update_notice(now=now) is None
    said = []
    assert update.run_update(["--check"], say=said.append) == 0 and said[-1] == "Ixel is up to date."
    commit(upstream, "two")
    assert update.update_notice(now=now + timedelta(days=1)) == "An update for Ixel is available. Run: ixel update"
    assert update.run_update(["--check"], say=said.append) == 0 and "available" in said[-1]


def test_git_install_updates_with_its_own_tool(git_installed, monkeypatch):
    upstream, install = git_installed
    ran, said = [], []
    real_find = update.find_on_path
    # pipx and uv are found on PATH (and run by their full path); git is the real one
    monkeypatch.setattr(update, "find_on_path", lambda name: f"/usr/bin/{name}" if name in ("pipx", "uv")
                        else real_find(name))
    monkeypatch.setattr(update, "_finish_in_new_window", lambda cmd, say: ran.append(cmd))  # Windows
    real_run = update.subprocess.run

    def fake_run(cmd, **kw):
        if cmd[0] in ("/usr/bin/pipx", "/usr/bin/uv"):
            ran.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)
        return real_run(cmd, **kw)

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    assert update.run_update([], say=said.append) == 0 and said[-1] == "Ixel is already up to date." and not ran
    commit(upstream, "two")
    assert update.run_update([], say=said.append) == 0
    assert ran == [["/usr/bin/pipx", "reinstall", "ixel-mat"]]
    install["kind"] = "uv"
    monkeypatch.setattr(update, "git_install", lambda *a, **k: dict(install))
    ran.clear()
    assert update.run_update([], say=said.append) == 0 and ran == [["/usr/bin/uv", "tool", "upgrade", "ixel-mat"]]
    ran.clear()  # nothing new, but --force: uv's upgrade alone would do nothing
    install["commit"] = git(upstream, "rev-parse", "HEAD").strip()
    assert update.run_update(["--force"], say=said.append) == 0
    assert ran == [["/usr/bin/uv", "tool", "upgrade", "--reinstall", "ixel-mat"]]


def test_plain_pip_and_missing_tools_are_explained(git_installed, monkeypatch):
    upstream, install = git_installed
    commit(upstream, "two")
    said = []
    install["kind"] = "pip"
    monkeypatch.setattr(update, "git_install", lambda *a, **k: dict(install))
    assert update.run_update([], say=said.append) == 1
    assert "pip install --upgrade --force-reinstall" in said[-1] and install["url"] in said[-1]
    install["kind"] = "pipx"
    real_find = update.find_on_path
    monkeypatch.setattr(update, "find_on_path", lambda name: None if name == "pipx" else real_find(name))
    assert update.run_update([], say=said.append) == 1 and "pipx reinstall ixel-mat" in said[-1]


def test_windows_finishes_in_a_window_that_waits_for_ixel(monkeypatch):
    import base64
    calls = []
    monkeypatch.setattr(update.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    update._finish_in_new_window(["pipx", "reinstall", "ixel-mat"], say=lambda line: None)
    cmd, kw = calls[0]
    script = base64.b64decode(cmd[cmd.index("-EncodedCommand") + 1]).decode("utf-16-le")
    assert f"Wait-Process -Id {os.getpid()}" in script and "Get-Process ixel" in script
    # and this environment's python.exe: the plugin runs it (python -m ixel_mat) on Windows
    python = update._ps_quote(sys.executable)
    assert f"Get-Process python -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -eq {python} }}" in script
    assert script.index("Get-Process python") < script.index("& 'pipx'")
    assert "& 'pipx' 'reinstall' 'ixel-mat'" in script and "Read-Host" in script
    assert kw["creationflags"] == getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
    assert update._ps_quote(r"C:\Users\O'Neil\python.exe") == r"'C:\Users\O''Neil\python.exe'"
