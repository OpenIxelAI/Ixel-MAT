"""
`ixel update`: get the latest Ixel, the way this copy was installed, plus a quiet
once-a-day check that says when there's something new.

- install.sh / install.ps1 record where they installed from in install.json, next
  to the virtualenv, and (once the install has finished) the commit they installed:
  the checkout is pulled and its installer run again, also when an earlier update
  pulled but didn't finish installing (unless it's still installing, in its window).
- `pipx install git+https://…` / `uv tool install git+https://…`: pip records the
  repository and commit in direct_url.json; `pipx reinstall` / `uv tool upgrade`
  fetch the latest.
- Other copies (pip install -e from a checkout, for instance) are updated the way
  they were installed; `ixel update` says how.
"""
from __future__ import annotations

import argparse
import base64
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ixel_mat.agents.launch import find_on_path

INSTALL_INFO = Path(sys.prefix).parent / "install.json"
CHECK_FILE = Path.home() / ".config" / "ixel-mat" / "update_check.json"
CHECK_EVERY = timedelta(days=1)

# For the background check: never ask for a password or open a browser. Terminal prompts off,
# every askpass program off (an empty GIT_ASKPASS stops git falling back to core.askPass or
# SSH_ASKPASS), SSH never prompts, and only credential helpers that read a saved login run
# (see _quiet_config), so a private repository can still be checked.
_QUIET_GIT = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "", "GCM_INTERACTIVE": "never",
              "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}
# Helpers that hand over a login already saved and never ask for one. Git Credential Manager
# asks only when interactive, which GCM_INTERACTIVE / credential.interactive turn off.
_QUIET_HELPERS = {"manager", "manager-core", "osxkeychain", "libsecret", "gnome-keyring", "store", "cache",
                  "wincred", "winstore"}


def _quiet_helper(value: str) -> bool:
    value = value.strip()
    if not value:
        return True  # an empty entry only clears the ones before it
    if value.startswith("!"):  # a shell command: only the GitHub CLI's own helper
        words = value[1:].split()
        return (len(words) >= 3 and re.split(r"[\\/]", words[0].strip("'\""))[-1].removesuffix(".exe") == "gh"
                and words[1:3] == ["auth", "git-credential"])
    if value[0] in "'\"":
        first = value[1:].split(value[0], 1)[0]
    else:
        first = re.split(r"(?<!\\)\s", value, maxsplit=1)[0]
    name = re.split(r"[\\/]", first)[-1].removesuffix(".exe").removeprefix("git-credential-")
    return name in _QUIET_HELPERS


def _program(name: str) -> str:
    """A program's full path from PATH (never the current folder, which Windows would search
    first: that may be a repository you just cloned). FileNotFoundError if it isn't there."""
    found = find_on_path(name)
    if not found:
        raise FileNotFoundError(name)
    return found


def _powershell() -> str:
    """Windows PowerShell, from where Windows keeps it (or PATH)."""
    builtin = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(builtin) if builtin.is_file() else (find_on_path("powershell") or str(builtin))


def _quiet_config(source: str | None = None) -> list[str]:
    """
    -c options for a git call that must never prompt. If every configured credential helper
    (credential.helper or a per-site credential.<url>.helper) only reads saved logins, they
    stay; otherwise the list is cleared and just those are put back.
    """
    options = ["-c", "credential.interactive=never", "-c", "core.askPass="]
    try:
        listed = subprocess.run([_program("git"), *(["-C", source] if source else []), "config", "--get-regexp",
                                 r"^credential\..*helper$"], capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=10, stdin=subprocess.DEVNULL)
        helpers = [line.partition(" ")[2] for line in listed.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        return options + ["-c", "credential.helper="]
    if all(_quiet_helper(h) for h in helpers):
        return options
    options += ["-c", "credential.helper="]  # the last entry, so it empties the whole list
    for helper in helpers:
        if helper.strip() and _quiet_helper(helper):
            options += ["-c", f"credential.helper={helper}"]
    return options


def install_info(path: Path | None = None) -> dict | None:
    try:
        info = json.loads((path or INSTALL_INFO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and info.get("source") else None


def _git(source: str | None, *args: str, quiet: bool = False, timeout: float = 120) -> subprocess.CompletedProcess:
    """git in the checkout `source` (or in no repository, for ls-remote)."""
    env = {**os.environ, **_QUIET_GIT} if quiet else None
    cmd = [_program("git"), *(_quiet_config(source) if quiet else []), *(["-C", source] if source else []), *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=env, stdin=subprocess.DEVNULL if quiet else None)


def git_install(prefix: Path | None = None, direct_url: str | None = None) -> dict | None:
    """
    Ixel installed from a git URL by pipx, uv or pip: {"kind", "url", "commit", "ref"}, from
    the direct_url.json pip records (and which tool made the environment); None otherwise.
    """
    prefix = prefix or Path(sys.prefix)
    if direct_url is None:
        try:
            direct_url = importlib.metadata.distribution("ixel-mat").read_text("direct_url.json")
        except importlib.metadata.PackageNotFoundError:
            return None
    try:
        data = json.loads(direct_url or "")
    except ValueError:
        return None
    vcs = data.get("vcs_info") if isinstance(data, dict) else None
    if not isinstance(vcs, dict) or vcs.get("vcs") != "git" or not isinstance(data.get("url"), str):
        return None
    kind = ("pipx" if (prefix / "pipx_metadata.json").exists()
            else "uv" if (prefix / "uv-receipt.toml").exists() else "pip")
    return {"kind": kind, "url": data["url"], "commit": str(vcs.get("commit_id") or ""),
            "ref": str(vcs.get("requested_revision") or "HEAD")}


def find_install() -> dict | None:
    """How this copy was installed: the installer's record, a git install, or None."""
    info = install_info()
    return {"kind": "installer", **info} if info else git_install()


def remote_commit(url: str, ref: str = "HEAD", quiet: bool = True) -> str | None:
    """The commit `ref` points to in the repository at `url`; None if that can't be told."""
    try:
        listed = _git(None, "ls-remote", url, ref, quiet=quiet, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    first = next((line.split()[0] for line in listed.stdout.splitlines() if line.strip()), "")
    return first if listed.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", first) else None


def _behind(install: dict) -> int | None:
    """New commits waiting: a count, -1 when there are some but not how many, 0, or None (unknown)."""
    if install.get("kind", "installer") == "installer":
        return commits_behind(install["source"])
    head = remote_commit(install["url"], install["ref"])
    return None if head is None else 0 if head == install["commit"] else -1


def commits_behind(source: str, fetch: bool = True, quiet: bool = True) -> int | None:
    """How many commits the checkout's branch is behind its upstream; None if that can't be told."""
    try:
        if fetch and _git(source, "fetch", "--quiet", quiet=quiet, timeout=20).returncode != 0:
            return None
        counted = _git(source, "rev-list", "--count", "HEAD..@{upstream}", quiet=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return int(counted.stdout.strip()) if counted.returncode == 0 and counted.stdout.strip().isdigit() else None


def checks_enabled(config: dict) -> bool:
    """Off with `[updates] check = false` in config.toml, or IXEL_NO_UPDATE_CHECK=1."""
    if os.environ.get("IXEL_NO_UPDATE_CHECK", "").strip() not in ("", "0"):
        return False
    updates = config.get("updates")
    return not (isinstance(updates, dict) and updates.get("check") is False)


def start_background_notice(config: dict):
    """Look for an update on a daemon thread, so a slow network never holds up start or exit.
    Returns a function that gives the notice if it's ready (None otherwise, or when checks are off)."""
    if not checks_enabled(config) or not find_install():
        return lambda wait=0.0: None
    box: list[str | None] = []

    def check() -> None:
        try:
            box.append(update_notice())
        except Exception:
            box.append(None)

    worker = threading.Thread(target=check, name="ixel-update-check", daemon=True)
    worker.start()

    def ready(wait: float = 0.0) -> str | None:
        worker.join(wait)
        return box.pop() if box else None

    return ready


def update_notice(info: dict | None = None, now: datetime | None = None, path: Path | None = None) -> str | None:
    """At most once a day, and never interactive: a line to show if an update is waiting."""
    info = info if info is not None else find_install()
    if not info:
        return None
    path = path or CHECK_FILE
    now = now or datetime.now(timezone.utc)
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(cached["checked"])
        behind = cached.get("behind")
    except (OSError, ValueError, KeyError, TypeError):
        checked, behind = None, None
    if checked is None or now - checked >= CHECK_EVERY:
        # Noted before asking too: a session that ends while the network is slow still counts as today's check
        _note_check(path, now, behind)
        behind = _behind(info)
        _note_check(path, now, behind)
    if isinstance(behind, int) and behind > 0:
        return f"An update for Ixel is available ({behind} new change{'s' if behind != 1 else ''}). Run: ixel update"
    if behind == -1:
        return "An update for Ixel is available. Run: ixel update"
    return None


def _note_check(path: Path, now: datetime, behind: int | None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checked": now.isoformat(), "behind": behind}), encoding="utf-8")
    except OSError:
        pass


def _ahead(source: str) -> int:
    """Commits in the checkout that its upstream doesn't have (a pull can't fast-forward past them)."""
    counted = _git(source, "rev-list", "--count", "@{upstream}..HEAD", quiet=True, timeout=10)
    return int(counted.stdout.strip()) if counted.returncode == 0 and counted.stdout.strip().isdigit() else 0


def _diverged(source: str, ahead: int) -> str:
    return (f"{source} has {ahead} commit{'s' if ahead != 1 else ''} of its own that GitHub doesn't have, so the "
            f"new version can't be brought in on top. To drop them and follow GitHub: "
            f'git -C "{source}" reset --keep @{{upstream}}, then ixel update.')


def run_update(argv: list[str], say=print) -> int:
    parser = argparse.ArgumentParser(
        prog="ixel update",
        description="Get the latest Ixel into the folder it was installed from, then reinstall it.")
    parser.add_argument("--check", action="store_true", help="only say whether an update is available")
    parser.add_argument("--force", action="store_true", help="reinstall even if nothing new was pulled")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help, or an option it doesn't know: stop before touching anything
        return exc.code if isinstance(exc.code, int) else 2
    try:
        return _run_update(args.check, args.force, say)
    except FileNotFoundError:
        say("Can't update: git isn't installed (or isn't on your PATH).")
    except subprocess.TimeoutExpired as exc:
        say(f"Can't update: git took too long ({' '.join(map(str, exc.cmd[3:]))}). Check your connection and try again.")
    return 1


def _run_update(check_only: bool, force: bool, say) -> int:
    install = find_install()
    if not install:
        say("This copy of Ixel wasn't installed with the installer, pipx or uv, so update it the way you "
            "installed it (for a checkout: git pull, then reinstall).")
        return 1
    if install["kind"] != "installer":
        return _update_git_install(install, check_only, force, say)
    info = install
    source = info["source"]
    if not (Path(source) / ".git").exists():
        say(f"Can't update: {source} isn't a git checkout any more.")
        return 1

    if _git(source, "rev-parse", "--abbrev-ref", "@{upstream}").returncode != 0:
        say(f"Can't update: {source} isn't following a branch on GitHub. "
            f'To follow the main version: git -C "{source}" switch main, then ixel update.')
        return 1
    installed = str(info.get("commit") or "")  # what the installer last finished installing; older ones don't say
    if check_only:
        behind = commits_behind(source, quiet=False)
        if behind is None:
            say("Couldn't reach GitHub to check. Try again, or run: ixel update")
            return 1
        ahead = _ahead(source)
        if behind and ahead:
            say(f"{behind} new change{'s' if behind != 1 else ''} available, but {_diverged(source, ahead)}")
            return 0
        if not behind and installed and installed != _head(source):
            say(STILL_INSTALLING if _still_installing()
                else "The last update didn't finish installing. Run: ixel update")
            return 0
        say(f"{behind} new change{'s' if behind != 1 else ''} available. Run: ixel update" if behind
            else "Ixel is up to date.")
        return 0
    # Pulling while an install runs would change the code under it, and a second install would collide
    if _still_installing():
        say(STILL_INSTALLING)
        return 0
    with _install_lock() as free:
        if not free:
            say(STILL_INSTALLING)
            return 0
        return _pull_and_reinstall(info, source, installed, force, say)


def _pull_and_reinstall(info: dict, source: str, installed: str, force: bool, say) -> int:
    dirty = _git(source, "status", "--porcelain", "--untracked-files=no")
    if dirty.returncode != 0 or dirty.stdout.strip():
        say(f"Can't update: {source} has local changes. Put them aside with "
            f'git -C "{source}" stash (or undo them), then run ixel update again.')
        return 1
    # Commits of your own are only in the way when there's something new to bring in on top of them
    ahead = _ahead(source)
    if ahead and commits_behind(source, quiet=False):
        say(f"Can't update: {_diverged(source, ahead)}")
        _say_branch(source, say)
        return 1
    say(f"Getting the latest Ixel into {source} …")
    before = _head(source)
    # Not quiet: a private repository may need you to sign in
    pulled = subprocess.run([_program("git"), "-C", source, "pull", "--ff-only"], text=True, encoding="utf-8",
                            errors="replace", capture_output=True, timeout=600)
    if pulled.returncode != 0:
        say((pulled.stderr or pulled.stdout).strip())
        say("git pull didn't complete, so nothing was changed.")
        _say_branch(source, say)
        return 1
    _forget_check()
    head = _head(source)
    # Up to date when that's what's checked out (without a record: when the pull brought nothing new)
    current = head == installed if installed else bool(before) and head == before
    if current and not force:
        say("Ixel is already up to date.")
        return 0
    if installed and head == before and not force:
        say("The last update didn't finish installing, so it's being installed again.")
    return _reinstall(info, say)


def _say_branch(source: str, say) -> None:
    branch = _git(source, "branch", "--show-current").stdout.strip()
    if branch and branch != "main":
        say(f"This checkout is on the branch {branch!r}. To follow the main version instead: "
            f'git -C "{source}" switch main, then ixel update.')


def _update_git_install(install: dict, check_only: bool, force: bool, say) -> int:
    """pipx / uv / pip installed from a git URL: compare commits, then let that tool reinstall."""
    head = remote_commit(install["url"], install["ref"], quiet=False)  # may ask you to sign in
    if head is None:
        say(f"Couldn't reach {install['url']} to check for updates. Try again later.")
        return 1
    current = head == install["commit"]
    if check_only:
        say("Ixel is up to date." if current else "An update for Ixel is available. Run: ixel update")
        return 0
    if current and not force:
        say("Ixel is already up to date.")
        return 0
    command = {"pipx": ["pipx", "reinstall", "ixel-mat"],
               "uv": ["uv", "tool", "upgrade", *(["--reinstall"] if force else []), "ixel-mat"]}.get(install["kind"])
    if command is None:  # plain pip, into an environment Ixel doesn't manage
        ref = "" if install["ref"] == "HEAD" else f"@{install['ref']}"
        say(f"Ixel was installed with pip from {install['url']}. To update it, run: "
            f'python -m pip install --upgrade --force-reinstall "git+{install["url"]}{ref}"')
        return 1
    program = find_on_path(command[0])
    if not program:
        say(f"Ixel was installed with {command[0]}, which isn't on your PATH now. Run: {' '.join(command)}")
        return 1
    command = [program, *command[1:]]
    _forget_check()
    if os.name == "nt":
        _finish_in_new_window(command, say)
        return 0
    result = subprocess.run(command)
    if result.returncode == 0:
        say("Updated. Restart any open ixel windows to use the new version.")
    return result.returncode


def _ps_quote(text: str) -> str:
    """A PowerShell string that holds text as it is."""
    return "'" + text.replace("'", "''") + "'"


def _finish_in_new_window(command: list[str], say) -> None:
    """
    Windows can't replace ixel.exe, or the environment's python.exe, while any copy of it runs: a new
    window waits for them to close. (The plugin runs python.exe -m ixel_mat on Windows, and so does
    anyone whose ixel.exe Smart App Control blocks.)
    """
    run = "& " + " ".join(map(_ps_quote, command))
    python = _ps_quote(sys.executable)
    script = "\n".join([
        f"Wait-Process -Id {os.getpid()} -Timeout 60 -ErrorAction SilentlyContinue",
        "Get-Process ixel -ErrorAction SilentlyContinue | Wait-Process -Timeout 120 -ErrorAction SilentlyContinue",
        f"Get-Process python -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -eq {python} }} | "
        "Wait-Process -Timeout 120 -ErrorAction SilentlyContinue",
        run,
        "if ($LASTEXITCODE -eq 0) { Write-Host 'Ixel is updated.' } else { Write-Host 'The update did not finish (see above).' }",
        "Read-Host 'Press Enter to close this window' | Out-Null",
    ])
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")  # no quoting to go wrong
    subprocess.Popen([_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                     creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10))
    say("Finishing the update in a new window. When it says Ixel is updated, you're done.")


def _head(source: str) -> str:
    return _git(source, "rev-parse", "HEAD").stdout.strip()


STILL_INSTALLING = ("An update is still installing, in another window. Let it finish; if it says the update "
                    "failed, run ixel update again then.")


@contextmanager
def _install_lock():
    """
    On macOS and Linux, for as long as the block runs: an exclusive lock on install.lock, so a second
    `ixel update` sees that one is installing. Yields False when another holds it. (On Windows,
    install.ps1 holds the file open itself while it runs.)
    """
    if os.name == "nt":
        yield not _windows_installing()
        return
    import fcntl
    try:
        handle = open(INSTALL_INFO.with_name("install.lock"), "a+b")
    except OSError:  # a folder Ixel can't write to: go on without the lock, as before
        yield True
        return
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:  # another update holds it
            yield False
            return
        except OSError:  # a file system without locks (some network and FUSE mounts): go on without it
            pass
        yield True


def _still_installing() -> bool:
    """Whether an update is installing now."""
    with _install_lock() as free:
        return not free


def _windows_installing() -> bool:
    """install.ps1 holds install.lock open, unshared, until it's done, so Windows won't let anyone else
    open it meanwhile."""
    try:
        with open(INSTALL_INFO.with_name("install.lock"), "rb"):
            return False
    except PermissionError:
        return True
    except OSError:  # there's no lock: no install since this was added
        return False


def _forget_check() -> None:
    try:
        CHECK_FILE.unlink()
    except OSError:
        pass


def _reinstall(info: dict, say) -> int:
    env = {**os.environ, "IXEL_SKIP_PATH_UPDATE": "1"}
    for key, name in (("install_root", "IXEL_INSTALL_ROOT"), ("bin_dir", "IXEL_BIN_DIR")):
        if info.get(key):
            env[name] = info[key]
    source = Path(info["source"])
    if os.name == "nt":
        # Windows can't replace the environment's python.exe (or ixel.exe) while it runs, so the
        # installer finishes in a new window that waits for this process to exit first.
        script = source / "install.ps1"
        subprocess.Popen([_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                          "-WaitForPid", str(os.getpid()), "-PauseAtEnd"], env=env,
                         creationflags=subprocess.CREATE_NEW_CONSOLE)  # type: ignore[attr-defined]
        say("Finishing the update in a new window. When it says Ixel is installed, you're done.")
        return 0
    result = subprocess.run([_program("bash"), str(source / "install.sh")], env=env)
    if result.returncode == 0:
        say("Updated. Restart any open ixel windows to use the new version.")
    return result.returncode
