"""
The app's Board: Handoff's board for a project, read and changed through `handoff api` (one JSON request
on stdin, one JSON reply on stdout; see Handoff's handoff/api.py).

Handoff runs as its own install's Python (`python -I -m handoff api`), found where Handoff's installers
put it, else from the `handoff` command on PATH. The command line is fixed, so nothing a task says passes
through a shell, and Handoff gets the environment without the keys Ixel loaded from its own .env (an API
key there would otherwise be billed instead of Claude's or Codex's subscription when Handoff runs them).

Watching a board is cheap: the board's files are looked at first (their size and time), and Handoff
only starts when they've changed, so an open Board doesn't start a program every few seconds.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ixel_mat.agents.launch import NO_WINDOW_FLAGS, LaunchError, find_on_path, resolve_argv
from ixel_mat.sanitize import sanitize_terminal_text

SCHEMA = 1
TIMEOUT = 30.0
SLOW_TIMEOUT = 120.0  # agents asks Ixel MAT who it knows; run.start checks the agent's CLI starts
SLOW_OPS = ("agents", "run.start")
WRITE_OPS = ("init", "add", "assign", "note", "review", "status", "delete", "approve", "revoke", "run.start")
MAX_REPLY_BYTES = 8 * 1024 * 1024
FULL_READ_EVERY = 30.0  # seconds: an unchanged-looking board is still read this often
NO_BOARD = "none"       # the revision of a project with no board yet, so a page can still say `since`
MAX_OUTPUT_BYTES = 30 * 1024 * 1024
OUTPUT_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
TASK_REF = re.compile(r"^T-[1-9][0-9]{0,11}$")
# The launcher lines Handoff's installers write (install.sh, install.ps1)
_SH_WRAPPER = re.compile(r'^exec "([^"\r\n]+)/bin/handoff" "\$@"', re.MULTILINE)
_CMD_WRAPPER = re.compile(r'^"([^"\r\n]+python\.exe)" -I -m handoff %\*', re.MULTILINE | re.IGNORECASE)


class HandoffApiError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ── Finding Handoff ───────────────────────────────────────────────────────────

def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def install_roots(env=os.environ, home: Path | None = None) -> list[Path]:
    """Where Handoff's installers put it: HANDOFF_INSTALL_ROOT if set, else the default."""
    roots = [Path(env["HANDOFF_INSTALL_ROOT"])] if env.get("HANDOFF_INSTALL_ROOT") else []
    if os.name == "nt":
        if env.get("LOCALAPPDATA"):
            roots.append(Path(env["LOCALAPPDATA"], "Handoff"))
    else:
        roots.append((home or Path.home()) / ".local" / "share" / "handoff")
    return roots


def python_from_launcher(path: Path, env=os.environ) -> Path | None:
    """The Python behind a `handoff` launcher on PATH: the wrapper an installer wrote, or a pip-made
    script's #! line. None when it's something else (a .exe), or the Python it names isn't there."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096).decode("utf-8", "replace")
    except OSError:
        return None
    found = None
    if match := _SH_WRAPPER.search(head):
        found = Path(match.group(1)) / "bin" / "python"
    elif match := _CMD_WRAPPER.search(head):
        text = match.group(1).replace("%LOCALAPPDATA%", env.get("LOCALAPPDATA", "%LOCALAPPDATA%"))
        found = Path(text)
    elif head.startswith("#!"):
        first = head[2:].splitlines()[0].strip() if head[2:].strip() else ""
        if first and " " not in first and Path(first).name.startswith("python"):
            found = Path(first)
    return found if found is not None and found.is_file() else None


def handoff_command(env=os.environ, home: Path | None = None) -> list[str] | None:
    """How to start Handoff: its install's Python, or the `handoff` command on PATH; None if neither."""
    for root in install_roots(env, home):
        python = _venv_python(root / ".venv")
        if python.is_file():
            return [str(python), "-I", "-m", "handoff"]
    launcher = find_on_path("handoff")
    if launcher is None:
        return None
    python = python_from_launcher(Path(launcher), env)
    return [str(python), "-I", "-m", "handoff"] if python else [launcher]


# ── Calling it ────────────────────────────────────────────────────────────────

def _env() -> dict[str, str]:
    """What Handoff runs with: none of the keys Ixel loaded from its .env (Handoff runs Claude and Codex,
    which would bill an API key instead of their subscription logins), and this Ixel findable on PATH,
    last, for the answers, reviews and pictures Handoff asks `ixel` for (an app started from a menu may
    not have the folder the installer put on PATH)."""
    import sys

    from ixel_mat.config.secrets import child_env
    env = {**child_env(nested=False), "PYTHONIOENCODING": "utf-8"}
    ixel_bin = str(Path(sys.executable).parent)
    folders = [f for f in env.get("PATH", "").split(os.pathsep) if f]
    if ixel_bin not in folders:
        env["PATH"] = os.pathsep.join([*folders, ixel_bin])
    return env


def call(op: str, project: Path | None = None, args: dict | None = None, command: list[str] | None = None,
         timeout: float | None = None) -> dict:
    """One request; Handoff's data, or HandoffApiError with its code and message."""
    command = command or handoff_command()
    if command is None:
        raise HandoffApiError("not_installed", "Handoff isn't installed on this computer.")
    request = {"schema": SCHEMA, "op": op, "args": args or {}}
    if project is not None:
        request["project"] = str(project)
    try:
        argv = resolve_argv([*command, "api"])
        proc = subprocess.run(argv, input=json.dumps(request).encode("utf-8"), capture_output=True,
                              timeout=timeout or (SLOW_TIMEOUT if op in SLOW_OPS else TIMEOUT),
                              cwd=tempfile.gettempdir(), env=_env(), creationflags=NO_WINDOW_FLAGS)
    except subprocess.TimeoutExpired:
        raise HandoffApiError("timeout", "Handoff took too long to answer.") from None
    except (OSError, LaunchError) as exc:
        raise HandoffApiError("not_installed", f"Couldn't start Handoff: {exc}") from None
    try:
        reply = json.loads(proc.stdout[:MAX_REPLY_BYTES].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        reply = None
    if not isinstance(reply, dict) or reply.get("schema") != SCHEMA:
        errors = proc.stderr.decode("utf-8", "replace")
        if "Unknown command" in errors or (isinstance(reply, dict) and reply.get("schema") != SCHEMA):
            raise HandoffApiError("outdated", "This Handoff is too old for the Board.")
        lines = [line.strip() for line in errors.splitlines() if line.strip()]
        raise HandoffApiError("internal", lines[-1][:300] if lines else f"Handoff stopped (exit code {proc.returncode}).")
    if not reply.get("ok"):
        error = reply.get("error") if isinstance(reply.get("error"), dict) else {}
        raise HandoffApiError(str(error.get("code", "internal")), str(error.get("message", "Handoff refused.")))
    data = reply.get("data")
    if not isinstance(data, dict):
        raise HandoffApiError("internal", "Handoff sent back something Ixel doesn't understand.")
    return data


# ── Projects and their boards ─────────────────────────────────────────────────

def check_project(value: object) -> Path:
    """A project folder the page sent: a full path to a folder in a git repository."""
    from ixel_mat.gui.handoff import project_root
    if not isinstance(value, str) or not value.strip():
        raise HandoffApiError("no_project", "Pick a project: the folder of a git repository on this computer.")
    if any(c in value for c in "\x00\r\n") or len(value) > 1000:
        raise HandoffApiError("no_project", "That isn't a folder's path.")
    value = value.strip()
    if len(value) > 2 and value[0] == value[-1] and value[0] in "\"'":  # Windows' "Copy as path" quotes it
        value = value[1:-1].strip()
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise HandoffApiError("no_project", "Give the project's full path, like C:\\Users\\you\\code\\shop or "
                                            "~/code/shop.")
    if not path.is_dir():
        raise HandoffApiError("no_project", f"There's no folder at {path}.")
    root = project_root(path)
    if root is None:
        raise HandoffApiError("no_project", f"{path} isn't in a git repository, and Handoff keeps its board in one.")
    return root


# Where the Board looks for projects to offer: where Visual Studio and GitHub Desktop put repositories, then the
# home folder's folders, then the folders in those
DEEPER_PLACES = (("source", "repos"), ("Documents", "GitHub"))
NOT_PROJECTS = {"Library", "Applications", "AppData", "Downloads", "Pictures", "Music", "Movies", "Videos",
                "Public", "node_modules"}
# A Mac asks before an app may look in these, and nobody asked for that: a project there is typed, not offered
MAC_ASKS_FIRST = {"Desktop", "Documents"}
MAX_PROJECTS = 24
MAX_FOLDERS_LOOKED_AT = 2000  # each is one look for a .git
MAX_ENTRIES_READ = 500        # of one folder: a big one doesn't use up the looks the others get
MAX_PLACE_ENTRIES_READ = 5000 # of the home folder and the places above, where dotfiles and files count too
FIND_SECONDS = 1.5            # a home folder on a slow network drive still answers soon


def _plain_name(name: str) -> bool:
    """A folder name the page can show and send back as it is: no control codes or bidi overrides (they could make
    one project look like another), and valid Unicode (Linux allows names that aren't)."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return sanitize_terminal_text(name) == name and "\t" not in name and "\n" not in name


def find_projects(home: Path | None = None, system: str = sys.platform, clock=time.monotonic) -> list[dict]:
    """Git repositories on this computer, for the Board to offer before a project is picked: the ones that
    have a board first, then by name."""
    home = (home or Path.home()).resolve()
    skip = NOT_PROJECTS | (MAC_ASKS_FIRST if system == "darwin" else set())
    deadline = clock() + FIND_SECONDS
    found: dict[str, bool] = {}
    looked = 0

    def folders(path: Path, most: int) -> list[Path]:
        names = []
        try:
            with os.scandir(path) as entries:
                for read, entry in enumerate(entries):
                    if read >= most:
                        break
                    if (entry.name.startswith(".") or entry.name in skip or not _plain_name(entry.name)
                            or not entry.is_dir(follow_symlinks=False) or getattr(entry, "is_junction", bool)()):
                        continue
                    names.append(entry.name)
        except OSError:
            pass  # a folder it can't open (or finish reading) offers what it read
        return [path / name for name in sorted(names, key=str.lower)]

    def ranked() -> list[dict]:
        best = sorted(found.items(), key=lambda item: (not item[1], Path(item[0]).name.lower()))
        return [{"path": path, "name": Path(path).name, "board": board} for path, board in best[:MAX_PROJECTS]]

    # Each folder's own folders before any deeper ones, so one big folder can't hide the rest
    level = [(home.joinpath(*parts), 1) for parts in DEEPER_PLACES if parts[0] not in skip] + [(home, 2)]
    places = {folder for folder, _ in level}
    while level:
        below = []
        for folder, depth in level:
            if clock() > deadline:
                return ranked()
            for sub in folders(folder, MAX_PLACE_ENTRIES_READ if folder in places else MAX_ENTRIES_READ):
                if looked >= MAX_FOLDERS_LOOKED_AT or clock() > deadline:
                    return ranked()
                looked += 1
                # (os.path's checks say False for a folder they can't open; Path's raise before Python 3.14)
                if os.path.exists(sub / ".git"):
                    found[str(sub)] = os.path.isdir(sub / ".handoff")
                elif depth > 1:
                    below.append((sub, depth - 1))
        level = below
    return ranked()


def board_signature(root: Path) -> tuple | None:
    """Size and time of the board's files: they change with every write (the board is SQLite in WAL mode,
    so a write lands in board.db-wal until it's folded into board.db)."""
    folder = root / ".handoff"
    parts = []
    for name in ("board.db", "board.db-wal"):
        try:
            st = os.stat(folder / name)
        except OSError:
            parts.append(None)
            continue
        parts.append((st.st_size, st.st_mtime_ns))
    return None if parts[0] is None else tuple(parts)


@dataclass
class Watched:
    signature: tuple | None
    revision: str
    at: float = field(default_factory=time.monotonic)


class BoardWatch:
    """Remembers each project's board files as last seen, so an unchanged board isn't read again."""

    def __init__(self):
        self._seen: dict[Path, Watched] = {}

    def unchanged(self, root: Path, revision: str) -> bool:
        """The page has `revision` and the board's files look as they did when it was read (or there's still
        no board). Read anyway now and then, in case a filesystem keeps coarse times."""
        seen = self._seen.get(root)
        return bool(revision) and seen is not None and seen.revision == revision \
            and time.monotonic() - seen.at < FULL_READ_EVERY and seen.signature == board_signature(root)

    def remember(self, root: Path, signature: tuple | None, revision: str) -> None:
        self._seen[root] = Watched(signature, revision)

    def forget(self, root: Path) -> None:
        self._seen.pop(root, None)

    def forget_all(self) -> None:
        self._seen.clear()


# ── Files a run left ──────────────────────────────────────────────────────────

_MOUNT_POINT = 0xA0000003  # a Windows junction


def _is_link(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0) == _MOUNT_POINT


def _no_links(path: Path, stop: Path) -> bool:
    """No folder below `stop` on the way to `path`, nor path itself, is a link (or a junction)."""
    current = path
    while current != stop and current != current.parent:
        if _is_link(current):
            return False
        current = current.parent
    return current == stop


def output_type(data: bytes, name: str) -> str | None:
    """What a run's file is, from its first bytes (pictures) or its name (an answer): None to refuse."""
    from ixel_mat.images import image_type
    kind = image_type(data[:16])
    if kind is not None:
        return {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}[kind]
    if name.lower().endswith((".md", ".txt")):
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return "text/plain; charset=utf-8"
    return None


def _open_inside(root: Path, parts: tuple[str, ...]) -> int:
    """Open root/parts... for reading, refusing a link at any step. POSIX: each folder is opened from the one
    before it, never by its whole path, so a folder swapped for a link between a check and the open can't
    redirect it. Windows: the opened file's final path must be exactly the one asked for."""
    if os.name == "nt":
        path = root.joinpath(*parts)
        fd = os.open(path, os.O_RDONLY | os.O_BINARY)
        try:
            final = _final_path(fd)
            expected = os.path.join(os.path.realpath(root), *parts)
            if final is None or os.path.normcase(final) != os.path.normcase(expected):
                raise HandoffApiError("forbidden", "That file is behind a link, so Ixel won't open it.")
        except BaseException:
            os.close(fd)
            raise
        return fd
    folder_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)  # the project itself is the person's pick
    try:
        for part in parts[:-1]:
            inner = os.open(part, folder_flags, dir_fd=fd)
            os.close(fd)
            fd = inner
        # O_NONBLOCK: a pipe planted there opens at once (and is refused below) instead of waiting for a writer
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    finally:
        os.close(fd)


def _final_path(fd: int) -> str | None:
    """Windows: where an open file really is, with every link and junction resolved."""
    import ctypes
    import msvcrt
    from ctypes import wintypes
    get = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    get.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    n = get(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0)
    if not 0 < n < len(buffer):
        return None
    final = buffer.value
    if final.startswith("\\\\?\\UNC\\"):
        return "\\\\" + final[8:]
    return final[4:] if final.startswith("\\\\?\\") else final


def read_output(root: Path, task: object, name: object) -> tuple[bytes, str]:
    """A file in .handoff/outputs/T-N, read without following a link anywhere on the way."""
    if not isinstance(task, str) or not TASK_REF.match(task):
        raise HandoffApiError("usage", "task must be like T-12.")
    if not isinstance(name, str) or not OUTPUT_NAME.match(name) or name in (".", ".."):
        raise HandoffApiError("usage", "That isn't a file a run leaves.")
    parts = (".handoff", "outputs", task, name)
    if not _no_links(root.joinpath(*parts), root):
        raise HandoffApiError("forbidden", "That file is behind a link, so Ixel won't open it.")
    try:
        fd = _open_inside(root, parts)
    except FileNotFoundError:
        raise HandoffApiError("not_found", f"{task} has no file {name}.") from None
    except OSError as exc:  # ELOOP / ENOTDIR: a link where a folder or the file should be
        raise HandoffApiError("forbidden", f"Couldn't open {name}: {exc.strerror}") from None
    with os.fdopen(fd, "rb") as handle:
        st = os.fstat(handle.fileno())
        # a plain file with no other name: a hard link would let a run show a file from anywhere on the disk
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > MAX_OUTPUT_BYTES:
            raise HandoffApiError("forbidden", f"{name} isn't a file Ixel shows.")
        data = handle.read(MAX_OUTPUT_BYTES + 1)
    kind = output_type(data, name)
    if kind is None:
        raise HandoffApiError("forbidden", f"{name} isn't a picture or an answer, so Ixel won't show it.")
    return data, kind


def version_ok(hello: dict) -> bool:
    return hello.get("schema") == SCHEMA and isinstance(hello.get("ops"), list)

