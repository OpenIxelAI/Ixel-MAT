"""
Running a command the person approved (or always allows), for the agent's run_command.

The model never gets a shell: a command is a list of words, run as it is, with the workspace as its folder,
a time limit, at most 64 KB of its output kept, and the environment without any keys saved in Ixel
(child_env). On a Mac it runs under sandbox-exec, with the network denied and writes allowed only in the
workspace and the temp folder. Elsewhere nothing like that is there to use, so the result says it ran
unsandboxed, and the approval card says so before it runs.
With Computer access on, it runs without sandbox-exec and with the network, keeping the same environment,
time limit, output limit and way of stopping its children.

is_shell_or_interpreter tells the commands that can run anything at all (a shell, `python -c`, `node -e`)
from the rest. The allowlist never matches those, so they always ask.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from ixel_mat.config.secrets import child_env
from ixel_mat.material import mask_secrets

DEFAULT_TIMEOUT = 120.0
MAX_OUTPUT_BYTES = 64 * 1024
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
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


@dataclass
class CommandResult:
    """What a command did. returncode is None when it was stopped at the time limit; sandboxed says whether
    it ran under sandbox-exec (network denied, writes only in the workspace and the temp folder)."""
    argv: list[str]
    returncode: int | None
    output: str
    timed_out: bool
    sandboxed: bool
    cancelled: bool = False


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


def sandbox_profile(workspace: Path) -> str:
    """The sandbox-exec profile: everything allowed except the network, and writes outside the workspace and
    the temp folders. Paths are real paths, because the sandbox sees /var as /private/var."""
    allowed = [os.path.realpath(workspace), os.path.realpath(tempfile.gettempdir()), "/private/tmp"]
    subpaths = " ".join(f"(subpath {_quoted(path)})" for path in dict.fromkeys(allowed))
    return ("(version 1) (allow default) (deny network*) (deny file-write*) "
            f'(allow file-write* {subpaths} (literal "/dev/null") (regex #"^/dev/tty"))')


def can_sandbox() -> bool:
    """Whether commands run sandboxed on this computer (a Mac with sandbox-exec)."""
    return sys.platform == "darwin" and os.path.exists(SANDBOX_EXEC)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stop a command and everything it started."""
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
    sandboxed = not computer and can_sandbox()
    if cancel is not None and cancel.is_set():
        return CommandResult(argv, None, "[stopped by the person]", False, sandboxed, cancelled=True)
    command = [SANDBOX_EXEC, "-p", sandbox_profile(folder), *argv] if sandboxed else argv
    options: dict = {"start_new_session": True} if os.name == "posix" else {}
    if os.name == "nt":
        from ixel_mat.agents.launch import NO_WINDOW_FLAGS
        options["creationflags"] = NO_WINDOW_FLAGS
    try:
        proc = subprocess.Popen(command, cwd=folder, env=child_env(nested=False), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **options)
    except FileNotFoundError:
        return CommandResult(argv, 127, f"there's no program {argv[0]!r} (check the name, or give its full path)",
                             False, sandboxed)
    except PermissionError:
        return CommandResult(argv, 126, f"{argv[0]!r} can't be run (it isn't a program, or isn't allowed)",
                             False, sandboxed)
    except OSError as exc:
        return CommandResult(argv, 126, f"{argv[0]!r} couldn't start: {exc.strerror or type(exc).__name__}",
                             False, sandboxed)

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
    return CommandResult(argv=argv, returncode=None if timed_out else proc.returncode,
                         output=mask_secrets(output.text()) + note, timed_out=timed_out, sandboxed=sandboxed,
                         cancelled=cancelled)
