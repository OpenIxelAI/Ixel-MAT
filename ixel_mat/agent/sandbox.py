"""
Running a command the person approved (or always allows), for the agent's run_command.

The model never gets a shell: a command is a list of words, run as it is, with the workspace as its folder,
a time limit, at most 64 KB of its output kept, and the environment without any keys saved in Ixel
(child_env). On a Mac it runs under sandbox-exec and on Linux under bubblewrap (bwrap), with the network
denied and writes allowed only in the workspace and the temp folder; on Linux the secret and browser folders
(secret_folders) are hidden too. On Windows (winsandbox.py) it runs with a low-integrity token in a job
object: writes land only in the workspace and a private temp folder, but the network is on and reads aren't
limited, so the secret and browser folders can be read there. CommandResult.sandbox says which of "full",
"writes-only" (Windows) and "none" a run had, and sandbox_kind which the next one will have. Where there is
none to use (no bwrap, a system that won't let it start, a Windows sandbox that fails its test, or another
system), the result says it ran unsandboxed, and the approval card says so before it runs;
sandbox_unavailable_reason says why.
With Computer access on, it runs without the sandbox and with the network, keeping the same environment,
time limit, output limit and way of stopping its children.

is_shell_or_interpreter tells the commands that can run anything at all (a shell, `python -c`, `node -e`)
from the rest. The allowlist never matches those, so they always ask.
"""
from __future__ import annotations

import functools
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from ixel_mat.agent import winsandbox
from ixel_mat.agent.secret_folders import secret_folders
from ixel_mat.config.secrets import child_env
from ixel_mat.material import mask_secrets

DEFAULT_TIMEOUT = 120.0
MAX_OUTPUT_BYTES = 64 * 1024
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# "1" here means Ubuntu 24.04+'s AppArmor refuses user namespaces to programs without a profile (bwrap needs them)
_APPARMOR_USERNS = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
_BWRAP_INSTALL = ("Debian/Ubuntu: sudo apt install bubblewrap; Fedora: sudo dnf install bubblewrap; "
                  "Arch: sudo pacman -S bubblewrap")
_READ_CHUNK = 8 * 1024
# After the command itself ended, how long the output is still read while programs it left running in the
# background hold it open, before they're stopped too
_DRAIN_WAIT = 2.0

_SHELLS = frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh", "mksh", "ash", "csh", "tcsh", "yash", "nu",
                     "xonsh", "elvish", "busybox", "cmd", "powershell", "pwsh"})
# Programs that run the command after them (after their own options): what counts is that command
_WRAPPERS = frozenset({"env", "sudo", "doas", "xargs", "nohup", "time", "nice", "timeout", "command", "exec",
                       "stdbuf", "caffeinate", "arch", "chroot", "unbuffer"})
# Interpreters, and the one-letter options (alone or in a cluster like perl's -ne) that make them run code
# given on the command line rather than a file
_INTERPRETERS: dict[str, frozenset[str]] = {
    "python": frozenset("c"), "pypy": frozenset("c"), "py": frozenset("c"),
    "node": frozenset("ep"), "nodejs": frozenset("ep"), "bun": frozenset("ep"), "deno": frozenset("ep"),
    "ruby": frozenset("e"), "perl": frozenset("eE"), "php": frozenset("rRBE"), "osascript": frozenset("e"),
    "lua": frozenset("e"), "tclsh": frozenset(), "rscript": frozenset("e"),
}
_EVAL_WORDS = frozenset({"--eval", "-eval", "--print", "--command", "--execute", "eval"})
_PYTHON_NAME = re.compile(r"(python|pypy)(\d+(\.\d+)*)?[a-z]?")
_VERSIONED_INTERPRETER = re.compile(r"(ruby|perl|php|lua|tclsh|rscript)(\d+(\.\d+)*)?")


SANDBOX_KINDS = ("full", "writes-only", "none")


@dataclass
class CommandResult:
    """What a command did. returncode is None when it was stopped at the time limit. sandbox says how it was
    confined: "full" (sandbox-exec on a Mac or bubblewrap on Linux: network denied, writes only in the
    workspace and the temp folder), "writes-only" (Windows: writes only in the workspace and a private temp
    folder, but the network is on and reads aren't limited) or "none". sandboxed is True only for "full";
    when sandbox is left out it follows sandboxed."""
    argv: list[str]
    returncode: int | None
    output: str
    timed_out: bool
    sandboxed: bool
    cancelled: bool = False
    sandbox: str = ""

    def __post_init__(self) -> None:
        if not self.sandbox:
            self.sandbox = "full" if self.sandboxed else "none"
        elif self.sandbox not in SANDBOX_KINDS:
            raise ValueError(f"sandbox is one of {', '.join(SANDBOX_KINDS)}")
        self.sandboxed = self.sandbox == "full"


def _program(word: str) -> str:
    """A command's program name as compared: the last part of its path, in lower case, without .exe."""
    name = re.split(r"[\\/]", word)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _interpreter(name: str) -> str | None:
    """Which of _INTERPRETERS a program name is (python3.12 is python), or None."""
    match = _PYTHON_NAME.fullmatch(name)
    if match:
        return match.group(1)
    match = _VERSIONED_INTERPRETER.fullmatch(name)
    if match:
        return match.group(1)
    return name if name in _INTERPRETERS else None


def _runs_code(kind: str, args: list[str]) -> bool:
    """Whether an interpreter's arguments give it code to run (rather than a file to run). Leans toward yes:
    a script's own -e option counts too, which only means that command always asks."""
    letters = _INTERPRETERS[kind]
    for arg in args:
        if kind in ("python", "pypy") and arg.startswith("-m"):
            return False  # python -m module: the rest are the module's own arguments
        if arg == "-":
            return True   # the program comes from its input
        if arg.split("=", 1)[0].lower() in _EVAL_WORDS:
            return True
        if kind == "php" and arg.split("=", 1)[0] in ("--run", "--process-code", "--run-begin", "--run-end"):
            return True
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1 and set(arg[1:]) & letters:
            return True
    return False


def is_shell_or_interpreter(argv: list[str]) -> bool:
    """Whether a command can run anything at all: a shell, an interpreter given code on the command line
    (python -c, node -e, perl -e …), or env/sudo/xargs/nohup/time running one of those. An empty command
    counts too. The allowlist never matches these."""
    if not argv or not isinstance(argv, (list, tuple)) or not all(isinstance(word, str) for word in argv):
        return True
    name = _program(argv[0])
    if name in _SHELLS:
        return True
    kind = _interpreter(name)
    if kind is not None:
        return _runs_code(kind, list(argv[1:]))
    if name in _WRAPPERS:
        # The wrapped command can start at any later word (wrappers' options differ, and some take values),
        # so each is tried: `env -i FOO=1 bash` and `sudo -u x python -c …` are both caught
        if name == "env" and any(word.startswith("-S") or word.split("=", 1)[0] == "--split-string"
                                 for word in argv[1:]):
            return True  # env -S splits a string into a command line, like a shell
        return any(is_shell_or_interpreter(list(argv[i:])) for i in range(1, len(argv)))
    return False


def _quoted(path: str) -> str:
    """A path as a string in a sandbox profile."""
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_profile(workspace: Path, *, network: bool = False, files: str = "chosen-folder",
                    temporary: bool = True) -> str:
    """The shared sandbox profile. Commands default to no network and workspace/temp writes; MCP cards
    pass their network/files permissions and temporary=False to allow only workspace writes, or none.
    Paths are real paths, because the sandbox sees /var as /private/var."""
    if files not in ("none", "output-folder", "chosen-folder"):
        raise ValueError("unknown files permission")
    allowed = [os.path.realpath(workspace)] if files != "none" else []
    if temporary:
        allowed += [os.path.realpath(tempfile.gettempdir()), "/private/tmp"]
    subpaths = " ".join(f"(subpath {_quoted(path)})" for path in dict.fromkeys(allowed))
    devices = ' (literal "/dev/null") (regex #"^/dev/tty")' if temporary else ""
    writes = f" (allow file-write* {subpaths}{devices})" if subpaths or devices else ""
    return ("(version 1) (allow default) " + ("" if network else "(deny network*) ")
            + "(deny file-write*)" + writes)


def _inside(path: str, folder: str) -> bool:
    """Whether path is folder or something under it, compared on whole path parts."""
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _bwrap_hide(paths: list[str]) -> list[str]:
    """bwrap's words that hide each path: an empty folder over a folder, /dev/null over a file."""
    words: list[str] = []
    for path in paths:
        words += ["--tmpfs", path] if os.path.isdir(path) else ["--ro-bind", "/dev/null", path]
    return words


def bwrap_argv(workspace: Path, argv: list[str], *, network: bool = False, files: str = "chosen-folder",
               temporary: bool = True) -> list[str]:
    """argv under bubblewrap, with the same rules as sandbox_profile: no network unless network, writes only
    in the workspace (none when files is "none") and the temp folders (temporary=False gives the command a
    private empty /tmp instead), everything else read-only. Reads aren't blocked, except that the secret
    and browser folders (secret_folders) are hidden. Paths are real paths. It only builds the command;
    can_sandbox says whether bwrap works here."""
    if files not in ("none", "output-folder", "chosen-folder"):
        raise ValueError("unknown files permission")
    ws = os.path.realpath(workspace)
    temps = [path for path in dict.fromkeys([os.path.realpath(tempfile.gettempdir()), os.path.realpath("/tmp")])
             if os.path.exists(path)]
    # Always bound, read-only when it can't be written, so --chdir works when a --tmpfs or a hidden folder
    # covers it. A later mount covers an earlier one, so the order matters: the system, the temp folders,
    # the workspace, then the hidden folders on top
    bind = ["--bind" if files != "none" else "--ro-bind", ws, ws]
    command = [shutil.which("bwrap") or "bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for path in temps:
        command += ["--bind", path, path] if temporary else ["--tmpfs", path]
    command += bind
    hidden = [path for path in secret_folders() if os.path.exists(path)]
    command += _bwrap_hide(hidden)
    if any(_inside(ws, path) for path in hidden):
        # The workspace is in a hidden folder, or is one (a fetched repo under Ixel's config folder): bind it
        # back so it stays usable, and hide again what's hidden inside it
        command += bind + _bwrap_hide([path for path in hidden if path != ws and _inside(path, ws)])
    command += ["--unshare-pid", "--unshare-ipc", "--unshare-uts"]
    if not network:
        command.append("--unshare-net")
    return command + ["--die-with-parent", "--chdir", ws, "--", *argv]


@functools.cache
def _bwrap_status() -> tuple[str | None, str | None]:
    """(the bwrap program, None) when bubblewrap works here, otherwise (None, why not). Found out once, by
    running a tiny sandboxed command, because an installed bwrap can still be refused by the system."""
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return None, ("Commands run without a sandbox because bubblewrap (bwrap) isn't installed. "
                      f"Install it: {_BWRAP_INSTALL}")
    probe = [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-pid", "--unshare-ipc",
             "--unshare-uts", "--unshare-net", "--die-with-parent", "--", "true"]
    try:
        done = subprocess.run(probe, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                              timeout=10)
    except subprocess.TimeoutExpired:
        return None, ("Commands run without a sandbox because bubblewrap (bwrap) didn't finish a test run "
                      "within 10 seconds")
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"Commands run without a sandbox because bubblewrap (bwrap) can't be started: {exc}"
    if done.returncode == 0:
        return bwrap, None
    try:
        blocked = _APPARMOR_USERNS.read_text(encoding="utf-8").strip() == "1"
    except OSError:
        blocked = False
    if blocked:
        profile = f"abi <abi/4.0>, profile bwrap {os.path.realpath(bwrap)} flags=(unconfined) {{ userns, }}"
        return None, ("Commands run without a sandbox because AppArmor (Ubuntu 24.04+) blocks the user "
                      "namespaces bubblewrap (bwrap) needs. Allow it with this one line: "
                      f"echo '{profile}' | sudo tee /etc/apparmor.d/bwrap && "
                      "sudo apparmor_parser -r /etc/apparmor.d/bwrap")
    lines = (done.stderr or "").strip().splitlines()
    detail = lines[0].strip() if lines else f"it exited with code {done.returncode}"
    return None, f"Commands run without a sandbox because bubblewrap (bwrap) can't start here: {detail}"


def can_sandbox() -> bool:
    """Whether commands run fully sandboxed on this computer (network denied, writes only in the workspace and
    the temp folder): a Mac with sandbox-exec, or Linux with a working bubblewrap (bwrap on the PATH, and a
    test run of it that succeeded, done once). Always False on Windows, whose sandbox is for Agent commands
    only and limits writes alone: see sandbox_kind."""
    if sys.platform == "darwin":
        return os.path.exists(SANDBOX_EXEC)
    if sys.platform.startswith("linux"):
        return _bwrap_status()[0] is not None
    return False


def sandbox_kind() -> str:
    """How far an Agent command is sandboxed here: "full" (can_sandbox), "writes-only" (Windows, when the
    low-integrity sandbox passed its test: writes only in the workspace and a private temp folder, with the
    network on and reads not limited) or "none". Add-ons and sandboxed_argv follow can_sandbox, so they
    aren't sandboxed on Windows."""
    if can_sandbox():
        return "full"
    if sys.platform == "win32" and winsandbox.available():
        return "writes-only"
    return "none"


def sandbox_unavailable_reason() -> str | None:
    """None when commands run fully sandboxed here, otherwise a sentence saying why they don't and what to
    do. On Windows, where the sandbox limits writes only, it is the sentence saying what that leaves open
    when it works, and the reason when it doesn't."""
    if sys.platform == "darwin":
        if os.path.exists(SANDBOX_EXEC):
            return None
        return f"Commands run without a sandbox because sandbox-exec isn't at {SANDBOX_EXEC} on this Mac."
    if sys.platform.startswith("linux"):
        return _bwrap_status()[1]
    if sys.platform == "win32":
        return winsandbox.WRITES_ONLY_NOTE if winsandbox.available() else winsandbox.unavailable_reason()
    return ("Commands run without a sandbox because there's none for this system yet (macOS, Linux and, for "
            "writes only, Windows have one).")


def sandboxed_argv(argv: list[str], workspace: Path, *, network: bool = False, files: str = "chosen-folder",
                   temporary: bool = True) -> list[str]:
    """argv wrapped to run under the sandbox of this system (sandbox-exec on a Mac, bubblewrap on Linux),
    with the same network, files and temporary meaning as sandbox_profile. Unchanged when can_sandbox is
    False, so a caller that must not run unsandboxed checks that first."""
    if not can_sandbox():
        return list(argv)
    if sys.platform == "darwin":
        profile = sandbox_profile(workspace, network=network, files=files, temporary=temporary)
        return [SANDBOX_EXEC, "-p", profile, *argv]
    return bwrap_argv(workspace, argv, network=network, files=files, temporary=temporary)


def _kill_tree(proc: subprocess.Popen | winsandbox.LowProcess) -> None:
    """Stop a command and everything it started."""
    if isinstance(proc, winsandbox.LowProcess):
        with suppress(OSError):
            proc.kill()  # its job object holds everything it started
        return
    if os.name == "posix":
        # It leads its own process group (start_new_session), and its children are in it unless they left
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    else:
        # ixel_mat/agents/process_tree.py's job objects are built around asyncio processes, so a command run
        # here (a plain subprocess) is stopped with taskkill, which ends its children too (/T)
        from ixel_mat.agents.launch import NO_WINDOW_FLAGS
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10, creationflags=NO_WINDOW_FLAGS)
    with suppress(OSError):
        if proc.poll() is None:
            proc.kill()


class _Output:
    """Reads a command's output as it comes, keeping the first MAX_OUTPUT_BYTES and counting the rest (it's
    still read, so the command never stops on a full pipe)."""

    def __init__(self, stream):
        self.kept = bytearray()
        self.total = 0
        self._stream = stream
        self.thread = threading.Thread(target=self._read, name="ixel-agent-output", daemon=True)
        self.thread.start()

    def _read(self) -> None:
        with suppress(OSError, ValueError):
            while chunk := self._stream.read1(_READ_CHUNK):  # what's there now, up to a chunk
                self.total += len(chunk)
                room = MAX_OUTPUT_BYTES - len(self.kept)
                if room > 0:
                    self.kept += chunk[:room]

    def text(self) -> str:
        text = bytes(self.kept).decode("utf-8", errors="replace")
        if self.total > len(self.kept):
            text += f"\n[output cut at {MAX_OUTPUT_BYTES // 1024} KB; it was {self.total} bytes in all]"
        return text


def run_command(argv: list[str], workspace: Path, timeout: float = DEFAULT_TIMEOUT,
                cancel: threading.Event | None = None, computer: bool = False) -> CommandResult:
    """Run an approved command in the workspace and wait for it, at most timeout seconds (then it and
    everything it started are stopped). cancel lets Stop end it sooner. Output and errors come back
    together, secrets masked. computer skips the sandbox for a run the person opted into.

    ValueError when argv isn't a non-empty list of text; nothing has run then."""
    if not isinstance(argv, list) or not argv or not all(isinstance(word, str) for word in argv):
        raise ValueError("a command is a non-empty list of text, like [\"pytest\", \"-q\"]")
    if any("\x00" in word for word in argv):
        raise ValueError("a command can't contain a NUL character")
    if not isinstance(computer, bool):
        raise ValueError("computer must be true or false")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not timeout > 0:
        raise ValueError("the time limit must be a number of seconds above 0")
    folder = Path(workspace).resolve()
    if not folder.is_dir():
        raise ValueError("the workspace isn't a folder")
    argv = list(argv)
    kind = "none" if computer else sandbox_kind()
    sandboxed = kind == "full"
    if cancel is not None and cancel.is_set():
        return CommandResult(argv, None, "[stopped by the person]", False, sandboxed, cancelled=True, sandbox=kind)
    try:
        if kind == "writes-only":
            proc = winsandbox.start(argv, folder, child_env(nested=False))
        else:
            command = sandboxed_argv(argv, folder) if sandboxed else argv
            options: dict = {"start_new_session": True} if os.name == "posix" else {}
            if os.name == "nt":
                from ixel_mat.agents.launch import NO_WINDOW_FLAGS
                options["creationflags"] = NO_WINDOW_FLAGS
            proc = subprocess.Popen(command, cwd=folder, env=child_env(nested=False), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **options)
    except winsandbox.SetupError as exc:
        # Not run, rather than run without the limits the card promised; the next card says it isn't sandboxed
        winsandbox.mark_broken(exc)
        return CommandResult(argv, 126, f"[not run: the Windows sandbox couldn't be set up: {exc}. Nothing was "
                                        "started. Commands will say they aren't sandboxed until Ixel is "
                                        "restarted.]", False, False, sandbox="none")
    except FileNotFoundError:
        return CommandResult(argv, 127, f"there's no program {argv[0]!r} (check the name, or give its full path)",
                             False, sandboxed, sandbox=kind)
    except PermissionError:
        return CommandResult(argv, 126, f"{argv[0]!r} can't be run (it isn't a program, or isn't allowed)",
                             False, sandboxed, sandbox=kind)
    except OSError as exc:
        return CommandResult(argv, 126, f"{argv[0]!r} couldn't start: {exc.strerror or type(exc).__name__}",
                             False, sandboxed, sandbox=kind)

    output = _Output(proc.stdout)
    deadline = time.monotonic() + timeout
    timed_out = False
    cancelled = False
    note = ""
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                cancelled = True
                _kill_tree(proc)
                note = "\n[stopped by the person]"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _kill_tree(proc)
                note = f"\n[stopped after {timeout:g} seconds, the time limit]"
                break
            with suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=min(remaining, 0.1) if cancel is not None else remaining)
        if not timed_out and not cancelled:
            # Programs it started in the background can keep its output open after it ended
            output.thread.join(max(min(deadline - time.monotonic(), _DRAIN_WAIT), 0.1))
            if output.thread.is_alive():
                _kill_tree(proc)
                note = "\n[programs it left running in the background were stopped]"
    finally:
        if proc.poll() is None:  # interrupted while waiting: never leave it running
            _kill_tree(proc)
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        output.thread.join(_DRAIN_WAIT)
        with suppress(OSError):
            proc.stdout.close()
        if isinstance(proc, winsandbox.LowProcess):
            proc.close()  # its private temp folder goes, and the workspace's Low label if this was the last run
    return CommandResult(argv=argv, returncode=None if timed_out else proc.returncode,
                         output=mask_secrets(output.text()) + note, timed_out=timed_out, sandboxed=sandboxed,
                         cancelled=cancelled, sandbox=kind)
