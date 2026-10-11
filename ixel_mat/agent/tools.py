"""
The agent's workspace: the one folder it works in, and the actions that read and change it.

The model never gets a file handle. It names a path relative to the workspace, and every path goes through
Workspace.resolve first, which refuses anything outside the folder (`..`, absolute paths, symlinks that lead
out), Git's own files, files that look like secrets (.env, keys, .netrc) and Ixel's own config folder, for
reading and writing alike.

Reads happen at once. A write or an edit happens in two halves: prepare_write / prepare_edit checks
everything and returns the diff the person approves, and apply writes it later, only if the file is still
what it was when the diff was made (another program, or the person, may have changed it meanwhile).
"""
from __future__ import annotations

import difflib
import fnmatch
import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from ixel_mat.config import secrets as _secrets
from ixel_mat.material import mask_secrets

MAX_LIST_ENTRIES = 500
MAX_READ_BYTES = 200 * 1024
MAX_SEARCH_LINES = 200
MAX_SEARCH_FILE_BYTES = 1024 * 1024
MAX_SEARCH_FILES = 20_000      # a search stops looking after this many files, so a huge folder can't hang it
MAX_SEARCH_LINE_CHARS = 300    # a long matching line is shown cut to this
MAX_WRITE_BYTES = 1024 * 1024
BINARY_PROBE = 8 * 1024        # a NUL byte in this much of the start means the file isn't text
# A file that isn't valid UTF-8 is still read when at most this share of its characters had to be replaced
# (a stray Latin-1 byte in an otherwise fine file); more than that, and it's treated as not text.
MAX_REPLACED_SHARE = 0.01

# Names that look like secrets, compared in lower case (a Mac's disk doesn't care about case, so .ENV is .env)
_SECRET_PREFIXES = (".env", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")
_SECRET_SUFFIXES = (".pem", ".key")
_SECRET_NAMES = frozenset({".netrc", ".npmrc", ".pypirc"})


class WorkspaceError(Exception):
    """An action the workspace refused, with a short reason the runner can send back to the model."""


@dataclass
class PendingWrite:
    """A write or edit that's been checked and is waiting for the person's approval.

    path is the real path inside the workspace; fingerprint is the file as it was when the diff was made
    (None for a new file), so apply can tell whether it changed since."""
    path: Path
    new_text: str
    diff: str
    fingerprint: tuple[int, int, str] | None


def is_secret_name(name: str) -> bool:
    """Whether a file or folder name looks like it holds a secret: .env*, *.pem, *.key, id_rsa*, .netrc …"""
    low = name.lower()
    return low in _SECRET_NAMES or low.startswith(_SECRET_PREFIXES) or low.endswith(_SECRET_SUFFIXES)


def _config_folder() -> str:
    """Ixel's own config folder (where its keys live), as a real path. Read each time, so tests can move it."""
    return os.path.realpath(_secrets._ENV_DIR)


def _fingerprint(path: Path) -> tuple[int, int, str] | None:
    """Size, modification time and SHA-256 of a file's bytes; None when there's no file."""
    try:
        info = path.stat()
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkspaceError(f"can't read the file: {exc.strerror or type(exc).__name__}") from exc
    return (info.st_size, info.st_mtime_ns, hashlib.sha256(data).hexdigest())


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:BINARY_PROBE]


def _decode(data: bytes) -> str | None:
    """The bytes as text: UTF-8, or UTF-8 with a few bad bytes replaced; None when it isn't text."""
    if _looks_binary(data):
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
        return text if text.count("�") <= max(1, len(text) * MAX_REPLACED_SHARE) else None


def _unified_diff(rel: str, old: str, new: str, is_new: bool) -> str:
    lines = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                 fromfile="/dev/null" if is_new else f"a/{rel}", tofile=f"b/{rel}")
    # difflib leaves a last line without its newline as it is; say so the way git does
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


class Workspace:
    """The folder the agent works in. Every action takes a path relative to it."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    # ── Paths ────────────────────────────────────────────────────────────────

    def resolve(self, path: str) -> Path:
        """The real path for a path the model named, or WorkspaceError when it's not one the agent may use."""
        if not isinstance(path, str) or not path.strip():
            raise WorkspaceError("give a path relative to the workspace, like src/app.py")
        if "\x00" in path:
            raise WorkspaceError("a path can't contain a NUL character")
        win = PureWindowsPath(path)
        if (PurePosixPath(path).is_absolute() or win.is_absolute() or win.drive or win.root
                or path.startswith("~")):
            raise WorkspaceError(f"{path!r} isn't relative to the workspace; name it from the workspace, "
                                 "like src/app.py")
        parts = path.replace("\\", "/").split("/")
        if ".." in parts:
            raise WorkspaceError(f"{path!r} goes outside the workspace ('..' isn't allowed)")
        self._check_parts(path, parts)
        try:
            real = (self.root / path).resolve(strict=False)
        except (OSError, RuntimeError) as exc:  # a symlink loop (RuntimeError before Python 3.13)
            raise WorkspaceError(f"{path!r} can't be followed (a symlink loop?)") from exc
        if real != self.root and not real.is_relative_to(self.root):
            raise WorkspaceError(f"{path!r} leads outside the workspace")
        # A symlink inside the workspace can still lead to Git's files, a secret or Ixel's config
        self._check_parts(path, real.relative_to(self.root).parts)
        config = _config_folder()
        if str(real) == config or str(real).startswith(config.rstrip(os.sep) + os.sep):
            raise WorkspaceError(f"{path!r} is in Ixel's own config folder, which the agent can't use")
        return real

    def _check_parts(self, path: str, parts: list[str] | tuple[str, ...]) -> None:
        for part in parts:
            if part.lower() == ".git":
                raise WorkspaceError(f"{path!r} is inside .git, which the agent can't use (Git's own files)")
            if is_secret_name(part):
                raise WorkspaceError(f"{path!r} looks like a secret ({part}), so the agent can't read or "
                                     "write it")

    def _rel(self, real: Path) -> str:
        rel = real.relative_to(self.root).as_posix()
        return rel if rel else "."

    # ── Reading ──────────────────────────────────────────────────────────────

    def list_dir(self, path: str = ".") -> str:
        """One line per entry, sorted by name: 'd name/' for a folder, 'f name size' for a file, and
        'l name@' for a symlink (not followed, so one leading out tells nothing about where). .git is left out."""
        target = self.resolve(path)
        if not target.is_dir():
            raise WorkspaceError(f"{path!r} isn't a folder")
        try:
            with os.scandir(target) as it:
                entries = sorted((e for e in it if e.name.lower() != ".git"), key=lambda e: e.name)
        except OSError as exc:
            raise WorkspaceError(f"can't list {path!r}: {exc.strerror or type(exc).__name__}") from exc
        lines = []
        for entry in entries[:MAX_LIST_ENTRIES]:
            try:
                if entry.is_symlink():
                    lines.append(f"l {entry.name}@")
                elif entry.is_dir(follow_symlinks=False):
                    lines.append(f"d {entry.name}/")
                else:
                    lines.append(f"f {entry.name} {entry.stat(follow_symlinks=False).st_size}")
            except OSError:
                lines.append(f"? {entry.name}")
        if len(entries) > MAX_LIST_ENTRIES:
            lines.append(f"... {len(entries) - MAX_LIST_ENTRIES} more")
        return "\n".join(lines) if lines else "(empty folder)"

    def read_file(self, path: str, start: int | None = None, end: int | None = None) -> str:
        """A text file's contents, or lines start to end (1-based, both included), with secrets masked.

        At most 200 KB: a bigger file is only read a line range at a time. Files that aren't text (a NUL
        byte near the start, or more than a few bytes that aren't UTF-8) are refused."""
        for name, value in (("start", start), ("end", end)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise WorkspaceError(f"{name} must be a line number, 1 or more")
        if start is not None and end is not None and end < start:
            raise WorkspaceError("end must not be before start")
        target = self.resolve(path)
        if not target.is_file():
            raise WorkspaceError(f"{path!r} isn't a file" if target.exists() else f"there's no file {path!r}")
        try:
            size = target.stat().st_size
            if start is None and end is None:
                if size > MAX_READ_BYTES:
                    raise WorkspaceError(f"{path!r} is {size} bytes, over the {MAX_READ_BYTES // 1024} KB limit; "
                                         "read part of it with start and end line numbers")
                data = target.read_bytes()
                note = ""
            else:
                data, note = self._read_lines(target, start or 1, end)
        except OSError as exc:
            raise WorkspaceError(f"can't read {path!r}: {exc.strerror or type(exc).__name__}") from exc
        text = _decode(data)
        if text is None:
            raise WorkspaceError(f"{path!r} isn't a text file")
        if not text and not note:
            return "(empty file)" if start is None and end is None else "(no lines in that range)"
        return mask_secrets(text) + note

    def _read_lines(self, target: Path, start: int, end: int | None) -> tuple[bytes, str]:
        """Lines start..end of a file (end None: to the end) as bytes, at most MAX_READ_BYTES of them."""
        with target.open("rb") as handle:
            if _looks_binary(handle.read(BINARY_PROBE)):
                return b"\x00", ""  # _decode refuses it
            handle.seek(0)
            kept: list[bytes] = []
            total = 0
            for number, line in enumerate(handle, start=1):
                if end is not None and number > end:
                    break
                if number < start:
                    continue
                if total + len(line) > MAX_READ_BYTES:
                    return b"".join(kept), (f"\n[stopped at line {number - 1}: the {MAX_READ_BYTES // 1024} KB "
                                            "limit; read on from there]")
                kept.append(line)
                total += len(line)
        return b"".join(kept), ""

    def search(self, pattern: str, path: str = ".", glob: str | None = None) -> str:
        """Lines containing pattern (plain text, case matters) as 'path:line: text', in files under path.

        glob limits it to file names that match (like *.py). Git's files, secret-looking files, symlinks
        that lead out, files that aren't text and files over 1 MB are skipped. At most 200 lines."""
        if not isinstance(pattern, str) or not pattern:
            raise WorkspaceError("give the text to search for")
        if "\n" in pattern or "\x00" in pattern:
            raise WorkspaceError("search looks for text within one line")
        if glob is not None and (not isinstance(glob, str) or not glob):
            raise WorkspaceError("glob must be a file-name pattern like *.py")
        top = self.resolve(path)
        if top.is_file():
            files = iter([top])
        elif top.is_dir():
            files = self._walk(top)
        else:
            raise WorkspaceError(f"there's no file or folder {path!r}")
        found: list[str] = []
        stopped = ""
        for count, file in enumerate(files):
            if count >= MAX_SEARCH_FILES:
                stopped = f"[stopped after {MAX_SEARCH_FILES} files; search a smaller folder with path]"
                break
            if glob is not None and not fnmatch.fnmatch(file.name, glob):
                continue
            for line in self._matches(file, pattern):
                if len(found) >= MAX_SEARCH_LINES:
                    stopped = f"[stopped at {MAX_SEARCH_LINES} matching lines; narrow it with path or glob]"
                    break
                found.append(line)
            if stopped:
                break
        if not found:
            return f"no lines contain {pattern!r}" + (f"\n{stopped}" if stopped else "")
        return mask_secrets("\n".join(found)) + (f"\n{stopped}" if stopped else "")

    def _walk(self, top: Path):
        """The files under top that the agent may read, in name order, without following symlinked folders."""
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = sorted(d for d in dirnames if d.lower() != ".git" and not is_secret_name(d))
            for name in sorted(filenames):
                yield Path(dirpath) / name

    def _matches(self, file: Path, pattern: str) -> list[str]:
        try:
            real = self.resolve(self._rel(file))  # skips symlinks that lead out, or to a secret
            if not real.is_file() or real.stat().st_size > MAX_SEARCH_FILE_BYTES:
                return []
            text = _decode(real.read_bytes())
        except (WorkspaceError, OSError):
            return []
        if text is None or pattern not in text:
            return []
        rel = self._rel(file)
        out = []
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern in line:
                if len(line) > MAX_SEARCH_LINE_CHARS:
                    line = line[:MAX_SEARCH_LINE_CHARS] + " […]"
                out.append(f"{rel}:{number}: {line}")
        return out

    # ── Writing ──────────────────────────────────────────────────────────────

    def _current_text(self, real: Path, path: str) -> str:
        """The text of a file about to be changed ('' when there's none yet), refusing what can't be edited."""
        if real.exists() and not real.is_file():
            raise WorkspaceError(f"{path!r} isn't a file")
        if not real.exists():
            return ""
        try:
            data = real.read_bytes()
        except OSError as exc:
            raise WorkspaceError(f"can't read {path!r}: {exc.strerror or type(exc).__name__}") from exc
        if len(data) > MAX_WRITE_BYTES:
            raise WorkspaceError(f"{path!r} is over 1 MB, too big for the agent to change")
        if _looks_binary(data):
            raise WorkspaceError(f"{path!r} isn't a text file, so the agent can't change it")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(f"{path!r} isn't UTF-8 text, so the agent can't change it") from exc

    def _pending(self, real: Path, old: str, new: str) -> PendingWrite:
        if "\x00" in new:
            raise WorkspaceError("the new text contains a NUL character; the agent only writes text")
        if len(new.encode("utf-8")) > MAX_WRITE_BYTES:
            raise WorkspaceError("the new text is over 1 MB, too big to write")
        if real.exists() and new == old:
            raise WorkspaceError("that changes nothing: the file already says that")
        fingerprint = _fingerprint(real)
        diff = _unified_diff(self._rel(real), old, new, is_new=fingerprint is None)
        return PendingWrite(path=real, new_text=new, diff=diff, fingerprint=fingerprint)

    def prepare_write(self, path: str, content: str) -> PendingWrite:
        """Check a whole-file write and make its diff (against the current file, or empty for a new one)."""
        if not isinstance(content, str):
            raise WorkspaceError("content must be text")
        real = self.resolve(path)
        if real == self.root:
            raise WorkspaceError("name a file to write, not the workspace itself")
        return self._pending(real, self._current_text(real, path), content)

    def prepare_edit(self, path: str, old: str, new: str) -> PendingWrite:
        """Check an edit that replaces old with new, where old appears exactly once, and make its diff."""
        if not isinstance(old, str) or not old:
            raise WorkspaceError("old must be the exact text to replace")
        if not isinstance(new, str):
            raise WorkspaceError("new must be text")
        real = self.resolve(path)
        if not real.is_file():
            raise WorkspaceError(f"there's no file {path!r} to edit; use write_file to make one")
        text = self._current_text(real, path)
        count = text.count(old)
        if count == 0:
            raise WorkspaceError(f"old appears 0 times in {path!r}; copy it exactly from read_file")
        if count > 1:
            raise WorkspaceError(f"old appears {count} times in {path!r}; include more of the lines around it "
                                 "so it appears once")
        return self._pending(real, text, text.replace(old, new, 1))

    def apply(self, pending: PendingWrite) -> str:
        """Write an approved change: refused if the file changed since its diff was made, else written to a
        temp file in the same folder and moved into place, so the file is never half-written."""
        rel = self._rel(pending.path)
        if self.resolve(rel) != pending.path:
            raise WorkspaceError(f"{rel} now leads somewhere else; propose the change again")
        before = _fingerprint(pending.path)
        if before != pending.fingerprint:
            raise WorkspaceError(f"{rel} changed since the change was shown; read it again and propose it again")
        data = pending.new_text.encode("utf-8")
        parent = pending.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            mode = stat.S_IMODE(pending.path.stat().st_mode) if before is not None else 0o644
            with tempfile.NamedTemporaryFile(delete=False, dir=parent, prefix=".ixel-agent-", suffix=".tmp") as tmp:
                tmp_path = Path(tmp.name)
                try:
                    tmp.write(data)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                except BaseException:
                    tmp.close()
                    tmp_path.unlink(missing_ok=True)
                    raise
            try:
                os.chmod(tmp_path, mode)
                os.replace(tmp_path, pending.path)
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise WorkspaceError(f"can't write {rel}: {exc.strerror or type(exc).__name__}") from exc
        return f"wrote {rel} ({len(data)} bytes)"
