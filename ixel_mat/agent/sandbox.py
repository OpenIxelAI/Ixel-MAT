"""
Running a command the person approved (or always allows), for the agent's run_command.

The model never gets a shell: a command is a list of words, run as it is, with the workspace as its folder,
a time limit, its first and last 32 KB of output kept (a line says how much was left out between), and the
environment without any keys saved in Ixel (child_env) or the ssh-agent socket. On a Mac it runs under
sandbox-exec, with the network denied, writes allowed only in the workspace and the temp folder, the secret
folders and files under the home folder and Ixel's config folder unreadable (other reads work, so programs
run), and the keychain services and Apple Events out of reach. Elsewhere nothing like that is there to use, so
the result says it ran unsandboxed, and the approval card says so before it runs.
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

from ixel_mat.config import secrets as _secrets
from ixel_mat.config.secrets import child_env
from ixel_mat.material import mask_secrets

DEFAULT_TIMEOUT = 120.0
MAX_OUTPUT_BYTES = 64 * 1024
# The output kept is its first and last parts, half of MAX_OUTPUT_BYTES each
_HEAD_BYTES = MAX_OUTPUT_BYTES // 2
_TAIL_BYTES = MAX_OUTPUT_BYTES - _HEAD_BYTES
# Extra bytes kept past each cut, so a secret across it is masked whole before the cut
_MASK_MARGIN = 4 * 1024
# The outermost bytes of a margin, longer than any secret's first part that no pattern matches yet
_MASK_EDGE = 128
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_READ_CHUNK = 8 * 1024
# After the command itself ended, how long the output is still read while programs it left running in the
# background hold it open, before they're stopped too
_DRAIN_WAIT = 2.0
# The services that reach the keychain over IPC. A profile is (allow default), so without denying these
# `security find-generic-password` still gets at the keychain whatever the file rules say. The trust services
# (trustd) aren't here: HTTPS certificate checks need them.
KEYCHAIN_MACH_SERVICES = (
    "com.apple.SecurityServer", "com.apple.securityd", "com.apple.securityd.xpc", "com.apple.secd",
    "com.apple.security.agent", "com.apple.security.agent.login", "com.apple.security.authhost",
)
# Home folders and files that hold keys, logins or browser sessions, on macOS, Linux and Windows. tools.py
# refuses paths under them for Computer access, and the sandbox denies reads of them (secret_folders). Refuse
# the whole browser folder, including cookies and login databases outside its Profiles subfolder. A file
# works here as a folder does: the sandbox's subpath rule and the path check both match the file itself.
# Files named .env* are found by name (secret_folders, is_secret_name).
SECRET_HOME_FOLDERS = (
    ".ssh", ".aws", ".gnupg", ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".config/gh", ".kube",
    ".docker/config.json", ".config/gcloud", ".azure",
    "Library/Keychains", "Library/Safari", "Library/Cookies",
    "Library/WebKit", "Library/Containers/com.apple.Safari",
    "Library/Group Containers/group.com.apple.Safari",
    "Library/Application Support/Google/Chrome", "Library/Application Support/Chromium",
    "Library/Application Support/Firefox", "Library/Application Support/BraveSoftware",
    "Library/Application Support/Microsoft Edge", "Library/Application Support/Arc",
    "Library/Application Support/Vivaldi", "Library/Application Support/com.operasoftware.Opera",
    ".mozilla", ".config/google-chrome", ".config/chromium", ".config/BraveSoftware",
    ".config/microsoft-edge", ".config/vivaldi", ".config/opera", ".config/mozilla",
    ".var/app/org.mozilla.firefox", ".var/app/com.google.Chrome", ".var/app/org.chromium.Chromium",
    ".var/app/com.brave.Browser", "snap/firefox", "snap/chromium",
    "AppData/Local/Google/Chrome", "AppData/Local/Chromium", "AppData/Local/BraveSoftware",
    "AppData/Local/Microsoft/Edge", "AppData/Local/Vivaldi", "AppData/Local/Mozilla/Firefox",
    "AppData/Roaming/Mozilla/Firefox", "AppData/Roaming/Opera Software",
)

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
    it ran under sandbox-exec (network denied, writes only in the workspace and the temp folder, the secret
    folders unreadable)."""
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


def _homes() -> list[Path]:
    """The home folder, and the account's own home folder too when $HOME points somewhere else."""
    homes = [Path.home()]
    if os.name == "posix":
        try:
            import pwd
            account = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (ImportError, KeyError, OSError):
            account = None
        if account is not None and account != homes[0]:
            homes.append(account)
    return homes


def secret_folders() -> list[str]:
    """The real paths a command must not read: the secret folders and files (SECRET_HOME_FOLDERS) under the
    home folder, and under the account's own home folder too when $HOME points somewhere else, and the
    .env* files directly in a home folder (not deeper), then Ixel's own config folder, where its keys live.
    Read each time, so tests can move the home."""
    homes = _homes()
    paths = [os.path.realpath(home / folder) for home in homes for folder in SECRET_HOME_FOLDERS]
    for home in homes:
        with suppress(OSError), os.scandir(home) as entries:
            for name in sorted(entry.name for entry in entries if entry.name.lower().startswith(".env")):
                paths.append(os.path.realpath(home / name))
    paths.append(os.path.realpath(_secrets._ENV_DIR))
    return list(dict.fromkeys(paths))


def sandbox_profile(workspace: Path, *, network: bool = False, files: str = "chosen-folder",
                    temporary: bool = True, addon_files: Path | None = None) -> str:
    """The shared sandbox profile. Commands default to no network and workspace/temp writes; MCP cards
    pass their network/files permissions and temporary=False to allow only workspace writes, or none.
    Reads of the secret folders (secret_folders) are denied for every profile, MCP ones too. A workspace
    inside one of them (a fetched repo, in Ixel's config folder) is read again, when files allow it.
    With files="none" the home folder can't be read either, except addon_files (the add-on's own installed
    files folder); system folders stay readable so programs run, but an add-on whose program lives in the
    home folder won't run with files="none". addon_files changes nothing for the other files modes.
    Every profile also denies the keychain services (KEYCHAIN_MACH_SERVICES), Apple Events and the
    ssh-agent socket named by $SSH_AUTH_SOCK, which file rules alone wouldn't stop.
    Paths are real paths, because the sandbox sees /var as /private/var."""
    if files not in ("none", "output-folder", "chosen-folder"):
        raise ValueError("unknown files permission")
    real = os.path.realpath(workspace)
    allowed = [real] if files != "none" else []
    if temporary:
        allowed += [os.path.realpath(tempfile.gettempdir()), "/private/tmp"]
    subpaths = " ".join(f"(subpath {_quoted(path)})" for path in dict.fromkeys(allowed))
    devices = ' (literal "/dev/null") (regex #"^/dev/tty")' if temporary else ""
    writes = f" (allow file-write* {subpaths}{devices})" if subpaths or devices else ""
    # With no file access the home folder is unreadable, bar the add-on's own files. The secret deny comes
    # after, so a secret inside that folder stays denied.
    homes = ""
    if files == "none":
        for path in dict.fromkeys(os.path.realpath(home) for home in _homes()):
            homes += f" (deny file-read* (subpath {_quoted(path)}))"
        if addon_files is not None:
            homes += f" (allow file-read* (subpath {_quoted(os.path.realpath(addon_files))}))"
    secret = secret_folders()
    denied = " ".join(f"(subpath {_quoted(path)})" for path in secret)
    # A later rule wins, so a workspace inside a denied folder is allowed again after the deny. A workspace
    # that only contains one (the home folder, say) keeps the deny.
    reread = ""
    if files != "none" and any(Path(real).is_relative_to(folder) for folder in map(Path, secret)):
        reread = f" (allow file-read* (subpath {_quoted(real)}))"
    # The ssh-agent socket, by the path it's given as and by its real path; it sits in a readable and often
    # writable folder (/tmp), and talking to it signs with the person's keys. These come after the re-read.
    agent = ""
    agent_socket = os.environ.get("SSH_AUTH_SOCK", "")
    if agent_socket and os.path.isabs(agent_socket):
        for path in dict.fromkeys([os.path.realpath(agent_socket), agent_socket]):
            agent += (f" (deny file-read* file-write* (literal {_quoted(path)}))"
                      f" (deny network-outbound (remote unix-socket (path-literal {_quoted(path)})))")
    # Last, so nothing above can allow them again
    keychain = " (deny mach-lookup " + " ".join(f"(global-name {_quoted(name)})"
                                                for name in KEYCHAIN_MACH_SERVICES) + ")"
    return ("(version 1) (allow default) " + ("" if network else "(deny network*) ")
            + "(deny file-write*)" + writes + homes + f" (deny file-read* {denied})" + reread + agent
            + keychain + " (deny appleevent-send)")


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
    """Reads a command's output as it comes, keeping the first and last part of the output (each with
    _MASK_MARGIN more bytes, for masking) and counting the rest (it's still all read, so the command never
    stops on a full pipe)."""

    def __init__(self, stream):
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self._stream = stream
        self.thread = threading.Thread(target=self._read, name="ixel-agent-output", daemon=True)
        self.thread.start()

    def _read(self) -> None:
        with suppress(OSError, ValueError):
            while chunk := self._stream.read1(_READ_CHUNK):  # what's there now, up to a chunk
                self.total += len(chunk)
                room = _HEAD_BYTES + _MASK_MARGIN - len(self.head)
                if room > 0:
                    self.head += chunk[:room]
                    chunk = chunk[room:]
                if chunk:
                    self.tail += chunk
                    if len(self.tail) > _TAIL_BYTES + _MASK_MARGIN:
                        del self.tail[:len(self.tail) - _TAIL_BYTES - _MASK_MARGIN]

    def text(self) -> str:
        """The output with secrets masked: whole when it's within MAX_OUTPUT_BYTES, else its first and last
        parts with a line between. Each part is masked with the margin past its cut still in, so a secret
        across the cut is blanked out whole, and only then cut."""
        head, tail, total = bytes(self.head), bytes(self.tail), self.total
        if total <= MAX_OUTPUT_BYTES:
            return mask_secrets((head + tail).decode("utf-8", errors="replace"))
        if total == len(head) + len(tail):
            # Every byte is here: the margin past each cut comes from the other part's bytes
            data = head + tail
            head, tail = data[:_HEAD_BYTES + _MASK_MARGIN], data[-(_TAIL_BYTES + _MASK_MARGIN):]
        # A secret cut by the far edge of a margin is only part of itself there, too short to be matched. When
        # masking shortened the margin enough that this edge falls inside what's kept, it's dropped as well.
        masked = mask_secrets(head.decode("utf-8", errors="replace")).encode("utf-8")
        kept_head = masked[:min(_HEAD_BYTES, max(len(masked) - _MASK_EDGE, 0))]
        masked = mask_secrets(tail.decode("utf-8", errors="replace")).encode("utf-8")
        kept_tail = masked[max(_MASK_EDGE, len(masked) - _TAIL_BYTES):]
        left_out = total - _HEAD_BYTES - _TAIL_BYTES
        marker = (f"\n[output cut at {MAX_OUTPUT_BYTES // 1024} KB: {left_out} bytes left out here, "
                  f"of {total} in all]\n")
        return kept_head.decode("utf-8", errors="replace") + marker + kept_tail.decode("utf-8", errors="replace")


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
    # No ssh-agent socket for the command: it would sign with the person's keys, secret folders or not
    env = child_env(nested=False, drop_env=["SSH_AUTH_SOCK"])
    try:
        proc = subprocess.Popen(command, cwd=folder, env=env, stdin=subprocess.DEVNULL,
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
                         output=output.text() + note, timed_out=timed_out, sandboxed=sandboxed,
                         cancelled=cancelled)
