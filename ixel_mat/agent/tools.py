"""
The agent's workspace: the one folder it works in, and the actions that read and change it.

The model never gets a file handle. It names a path relative to the workspace, and every path goes through
Workspace.resolve first, which refuses anything outside the folder (`..`, absolute paths, symlinks that lead
out), Git's own files, files that look like secrets (.env, keys, .netrc) and Ixel's own config folder, for
reading and writing alike. With Computer access on, paths may reach the home folder instead; keys and
browser profiles still can't be used.

Reads happen at once. A write or an edit happens in two halves: prepare_write / prepare_edit checks
everything and returns the diff the person approves, and apply writes it later, only if the file is still
what it was when the diff was made (another program, or the person, may have changed it meanwhile).

read_file also reads documents (PDF, Word, Excel, PowerPoint …) as their text, and view_file looks at a
picture, a few frames of a video (with ffmpeg) or a document's first pictures: those go to the model with
its next call. Files the person attached to a run are named upload:NAME, and only read_file and view_file
reach them; they live in Ixel's own folder, outside the workspace, so nothing can write or search them.
"""
from __future__ import annotations

import difflib
import fnmatch
import hashlib
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

from ixel_mat import documents, pictures
from ixel_mat.agent.sandbox import CommandResult, run_command
from ixel_mat.config import secrets as _secrets
from ixel_mat.material import mask_secrets
from ixel_mat.pictures import Picture

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

# What view_file looks at
VIDEOS = frozenset({".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"})
MAX_VIDEO_FRAMES = 6
MAX_PICTURES_PER_CALL = pictures.MAX_PER_QUESTION          # 8: the pictures that go with one model call
MAX_PICTURE_BYTES_PER_CALL = pictures.MAX_PER_QUESTION_BYTES  # 20 MB of them in all
MAX_VIEW_BYTES = documents.MAX_FILE_BYTES                  # 50 MB: a document read whole
FFMPEG_TIMEOUT = 60.0                                      # seconds, for each time ffmpeg runs
UPLOAD_PREFIX = "upload:"
# What list_dir says a file is: what the model can use on it
KINDS = ("picture", "video", "document", "text", "other")
_DURATION = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")

# Names that look like secrets, compared in lower case (a Mac's disk doesn't care about case, so .ENV is .env)
_SECRET_PREFIXES = (".env", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")
_SECRET_SUFFIXES = (".pem", ".key")
_SECRET_NAMES = frozenset({".netrc", ".npmrc", ".pypirc"})
# Home folders that hold keys or browser sessions, on macOS, Linux and Windows. Refuse the whole browser
# folder, including cookies and login databases outside its Profiles subfolder.
_COMPUTER_SECRET_FOLDERS = (
    ".ssh", ".aws", ".gnupg", "Library/Keychains", "Library/Safari", "Library/Cookies",
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


@dataclass(frozen=True)
class Viewed:
    """What view_file found: text is what the model reads in the step's result, and pictures go to it with
    its next call (none when there was nothing to show)."""
    text: str
    pictures: list[Picture] = field(default_factory=list)


def kind_of(path: Path) -> str:
    """What a file is, one of KINDS: a picture, video or document by its name; else text, unless its start
    holds a NUL byte or it can't be read ('other')."""
    name = Path(path).name
    if documents.is_picture(name):
        return "picture"
    if documents.suffix(name) in VIDEOS:
        return "video"
    if documents.is_document(name):
        return "document"
    try:
        if not Path(path).is_file():
            return "other"
        with open(path, "rb") as handle:
            start = handle.read(BINARY_PROBE)
    except OSError:
        return "other"
    return "other" if _looks_binary(start) else "text"


def _a(kind: str) -> str:
    """A document's kind with 'a' or 'an' before it: 'a PDF', 'an Excel workbook'."""
    return f"an {kind}" if kind[:1].lower() in "aeiou" or kind.startswith("RTF") else f"a {kind}"


def _run_ffmpeg(argv: list[str], folder: Path) -> str:
    """Run ffmpeg as a list of words (never through a shell) in a private temp folder, with the key-free
    environment and a time limit; its output and errors as text. ffmpeg may exit with an error on purpose
    (asked only to describe a file, it says it was given no output), so the exit code isn't checked here."""
    options: dict = {}
    if os.name == "nt":
        from ixel_mat.agents.launch import NO_WINDOW_FLAGS
        options["creationflags"] = NO_WINDOW_FLAGS
    done = subprocess.run(argv, cwd=folder, env=_secrets.child_env(nested=False), stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=FFMPEG_TIMEOUT, **options)
    return (done.stdout + done.stderr).decode("utf-8", errors="replace")


def is_secret_name(name: str) -> bool:
    """Whether a file or folder name looks like it holds a secret: .env*, *.pem, *.key, id_rsa*, .netrc …"""
    low = name.split(":", 1)[0].lower()  # Windows may name an alternate stream after a colon.
    return low in _SECRET_NAMES or low.startswith(_SECRET_PREFIXES) or low.endswith(_SECRET_SUFFIXES)


def _config_folder() -> str:
    """Ixel's own config folder (where its keys live), as a real path. Read each time, so tests can move it."""
    return os.path.realpath(_secrets._ENV_DIR)


def _snapshot(path: Path) -> tuple[bytes, tuple[int, int, str] | None]:
    """A file's bytes and fingerprint from the same bounded read; empty bytes and None for a new file."""
    try:
        with path.open("rb") as handle:
            info = os.fstat(handle.fileno())
            data = handle.read(MAX_WRITE_BYTES + 1)
    except FileNotFoundError:
        return b"", None
    except OSError as exc:
        raise WorkspaceError(f"can't read the file: {exc.strerror or type(exc).__name__}") from exc
    if len(data) > MAX_WRITE_BYTES:
        raise WorkspaceError("the file is over 1 MB, too big for the agent to change")
    return data, (len(data), info.st_mtime_ns, hashlib.sha256(data).hexdigest())


def _fingerprint(path: Path) -> tuple[int, int, str] | None:
    """Size, modification time and SHA-256 of a file's bytes; None when there's no file."""
    return _snapshot(path)[1]


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
    """The folder the agent works in. With computer=True, paths can also reach anywhere in the home folder
    except secrets, Git's files and browser profiles. It is off unless the person opts in for this run.

    uploads is the folder holding the files the person attached to this run (uploads/<run id>/ in the
    agent's folder; the runner sets it), or None. read_file and view_file name those as upload:NAME."""

    def __init__(self, root: Path, uploads: Path | None = None, computer: bool = False):
        if not isinstance(computer, bool):
            raise ValueError("computer must be true or false")
        self.root = Path(root).resolve()
        self.uploads = Path(uploads) if uploads is not None else None
        self.computer = computer

    # ── Paths ────────────────────────────────────────────────────────────────

    def resolve(self, path: str) -> Path:
        """The real path for a path the model named, or WorkspaceError when it's not one the agent may use."""
        if isinstance(path, str) and path.startswith(UPLOAD_PREFIX):
            raise WorkspaceError("'upload:' files are the person's attachments; read_file and view_file can read "
                                 "them, nothing can write or search them")
        if not isinstance(path, str) or not path.strip():
            raise WorkspaceError("give a path relative to the workspace, like src/app.py")
        if "\x00" in path:
            raise WorkspaceError("a path can't contain a NUL character")
        if self.computer:
            return self._resolve_computer(path)
        win = PureWindowsPath(path)
        if (PurePosixPath(path).is_absolute() or win.is_absolute() or win.drive or win.root
                or path.startswith("~")):
            raise WorkspaceError(f"{path!r} isn't relative to the workspace; name it from the workspace, "
                                 "like src/app.py")
        parts = path.replace("\\", "/").split("/")
        if ".." in parts:
            raise WorkspaceError(f"{path!r} goes outside the workspace ('..' isn't allowed)")
        self._check_parts(path, self.root.parts)
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
        # On a case-insensitive disk, a different spelling of the config path is the same folder.
        for parent in (real, *real.parents):
            try:
                if parent.samefile(config):
                    raise WorkspaceError(f"{path!r} is in Ixel's own config folder, which the agent can't use")
            except OSError:
                continue
        return real

    def _resolve_computer(self, path: str) -> Path:
        """A path anywhere under the person's home folder, with both its spelling and symlink destination
        checked for secrets. A ~ names only this person's home, never another account's."""
        home = Path.home()
        if path == "~":
            target = home
        elif path.startswith(("~/", "~\\")):
            target = home / path[2:]
        elif path.startswith("~"):
            raise WorkspaceError("~ can only name your own home folder, like ~/Documents/notes.txt")
        else:
            win = PureWindowsPath(path)
            if os.name != "nt" and (win.drive or win.root) and not PurePosixPath(path).is_absolute():
                raise WorkspaceError(f"{path!r} isn't a path under your home folder")
            target = self.root / path
        self._check_parts(path, target.parts)
        self._check_parts(path, path.replace("\\", "/").split("/"))
        self._check_computer_folders(path, target, home)
        try:
            real = target.resolve(strict=False)
            real_home = home.resolve()
        except (OSError, RuntimeError) as exc:
            raise WorkspaceError(f"{path!r} can't be followed (a symlink loop?)") from exc
        if not real.is_relative_to(real_home):
            raise WorkspaceError(f"{path!r} leads outside your home folder")
        self._check_parts(path, real.parts)
        self._check_computer_folders(path, real, real_home)
        config = _config_folder()
        if str(real).casefold() == config.casefold() or str(real).casefold().startswith(
                config.rstrip(os.sep).casefold() + os.sep):
            raise WorkspaceError(f"{path!r} is in Ixel's own config folder, which the agent can't use")
        for parent in (real, *real.parents):
            try:
                if parent.samefile(config):
                    raise WorkspaceError(f"{path!r} is in Ixel's own config folder, which the agent can't use")
            except OSError:
                continue
        return real

    def _check_computer_folders(self, path: str, target: Path, home: Path) -> None:
        """Refuse keys and browser profiles, including a symlink to one or a different case spelling."""
        shown = str(target).casefold()
        for folder in _COMPUTER_SECRET_FOLDERS:
            secret = home / folder
            try:
                candidates = (secret, secret.resolve(strict=False))
            except (OSError, RuntimeError):
                candidates = (secret,)
            for candidate in candidates:
                low = str(candidate).casefold()
                if shown == low or shown.startswith(low.rstrip(os.sep) + os.sep):
                    raise WorkspaceError(f"{path!r} is in a secret or browser profile folder, which the agent "
                                         "can't use")

    def _check_parts(self, path: str, parts: list[str] | tuple[str, ...]) -> None:
        for part in parts:
            if part.split(":", 1)[0].lower() == ".git":
                raise WorkspaceError(f"{path!r} is inside .git, which the agent can't use (Git's own files)")
            if is_secret_name(part):
                raise WorkspaceError(f"{path!r} looks like a secret ({part}), so the agent can't read or "
                                     "write it")

    def _rel(self, real: Path) -> str:
        if self.computer and not real.is_relative_to(self.root):
            return str(real)
        rel = real.relative_to(self.root).as_posix()
        return rel if rel else "."

    def _upload(self, path: str) -> tuple[Path, str]:
        """The real path of an attached file named upload:NAME, and that name to show; WorkspaceError when
        there's no such file or NAME isn't one plain file name. Uploads are in Ixel's own folder, which
        resolve refuses on purpose, so this is the only way to them (and only read_file and view_file use it)."""
        if self.uploads is None:
            raise WorkspaceError("no files were attached to this run")
        name = path[len(UPLOAD_PREFIX):]
        shown = UPLOAD_PREFIX + name
        if (not name or name != name.strip() or name.startswith(".")
                or any(c in name for c in ("/", "\\", "\x00", ":"))):
            raise WorkspaceError(f"{path!r} isn't an attached file; name one as upload:NAME, as the prompt lists "
                                 "them")
        if is_secret_name(name):
            raise WorkspaceError(f"{path!r} looks like a secret, so the agent can't read it")
        try:
            folder = self.uploads.resolve()
            info = (folder / name).lstat()
            real = (folder / name).resolve()
        except FileNotFoundError:
            raise WorkspaceError(f"there's no attached file {shown!r}") from None
        except (OSError, RuntimeError) as exc:
            raise WorkspaceError(f"can't read {shown!r}") from exc
        # A symlink, or anything that isn't a plain file, could lead anywhere: only the file itself is read
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or real.parent != folder:
            raise WorkspaceError(f"{shown!r} isn't an attached file the agent can read")
        return real, shown

    def _source(self, path: str) -> tuple[Path, str]:
        """For read_file and view_file: the real path to read and the name to show, an attached file's
        (upload:NAME) or a workspace path's."""
        if isinstance(path, str) and path.startswith(UPLOAD_PREFIX):
            return self._upload(path)
        return self.resolve(path), path

    def _document(self, real: Path, shown: str) -> documents.Document:
        """A document file read whole (at most MAX_VIEW_BYTES), or WorkspaceError saying why it can't be."""
        try:
            size = real.stat().st_size
            if size > MAX_VIEW_BYTES:
                raise WorkspaceError(f"{shown!r} is {size} bytes, over the {MAX_VIEW_BYTES // (1024 * 1024)} MB "
                                     "a document can be")
            with real.open("rb") as handle:
                data = handle.read(MAX_VIEW_BYTES + 1)
        except OSError as exc:
            raise WorkspaceError(f"can't read {shown!r}: {exc.strerror or type(exc).__name__}") from exc
        try:
            return documents.read_document(data, real.name)
        except documents.DocumentError as exc:
            raise WorkspaceError(str(exc)) from None

    # ── Reading ──────────────────────────────────────────────────────────────

    def list_dir(self, path: str = ".") -> str:
        """One line per entry, sorted by name: 'd name/' for a folder, 'f name size' for a file, and
        'l name@' for a symlink (not followed, so one leading out tells nothing about where). .git is left out.

        A file line ends with the file's kind when it isn't plain text: 'f photo.jpg 1234 picture', or video,
        document, other."""
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
                    # A secret-looking file isn't opened, not even to see whether it's text
                    kind = "text" if is_secret_name(entry.name) else kind_of(Path(entry.path))
                    lines.append(f"f {entry.name} {entry.stat(follow_symlinks=False).st_size}"
                                 + ("" if kind == "text" else f" {kind}"))
            except OSError:
                lines.append(f"? {entry.name}")
        if len(entries) > MAX_LIST_ENTRIES:
            lines.append(f"... {len(entries) - MAX_LIST_ENTRIES} more")
        return "\n".join(lines) if lines else "(empty folder)"

    def read_file(self, path: str, start: int | None = None, end: int | None = None) -> str:
        """A text file's contents, or lines start to end (1-based, both included), with secrets masked.

        At most 200 KB: a bigger file is only read a line range at a time. Files that aren't text (a NUL
        byte near the start, or more than a few bytes that aren't UTF-8) are refused.

        A document (PDF, Word, Excel, PowerPoint, RTF, OpenDocument, a web page; at most 50 MB) is read as
        its text, with a last line saying what kind it is and how many pictures it holds; start and end are
        line numbers of that text. Pictures and videos are for view_file. path may name an attached file as
        upload:NAME."""
        for name, value in (("start", start), ("end", end)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise WorkspaceError(f"{name} must be a line number, 1 or more")
        if start is not None and end is not None and end < start:
            raise WorkspaceError("end must not be before start")
        target, path = self._source(path)
        if not target.is_file():
            raise WorkspaceError(f"{path!r} isn't a file" if target.exists() else f"there's no file {path!r}")
        if documents.is_picture(target.name) or documents.suffix(target.name) in VIDEOS:
            raise WorkspaceError(f"{path!r} is a {kind_of(target)}, not text; look at it with view_file")
        if documents.is_document(target.name):
            return self._document_text(target, path, start, end)
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

    def _document_text(self, target: Path, path: str, start: int | None, end: int | None) -> str:
        """A document's text for read_file (lines start to end of it when given), secrets masked, and a last
        line saying what kind it is and whether view_file has pictures to show."""
        doc = self._document(target, path)
        text = mask_secrets(doc.text)
        if start is not None or end is not None:
            lines = text.splitlines()
            text = "\n".join(lines[(start or 1) - 1:end])
            if not text:
                text = "(no lines in that range)"
        elif not text.strip():
            text = "(no text in it)"
        count = len(doc.images)
        if count:
            note = (f"[{_a(doc.kind)} with {count} picture{'' if count == 1 else 's'} inside; view_file shows "
                    "the first ones]")
        else:
            note = f"[{_a(doc.kind)}, no pictures]"
        # What the reader left out (text cut at its limit, parts it skipped), for the model to know
        return "\n".join([text, note, *(f"[{mask_secrets(line)}]" for line in doc.notes)])

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

    # ── Looking ──────────────────────────────────────────────────────────────

    def view_file(self, path: str) -> Viewed:
        """Pictures for the model's next call: a picture's own, up to MAX_VIDEO_FRAMES frames of a video (with
        ffmpeg), or a document's first pictures; at most MAX_PICTURES_PER_CALL. path may name an attached
        file as upload:NAME.

        Pictures are cleaned the way Ask cleans them (ixel_mat.pictures), which takes PNG and JPEG only: a
        GIF, WebP, HEIC, BMP or TIFF is refused in the beta, saying the person can convert it to one of those."""
        real, shown = self._source(path)
        if not real.is_file():
            raise WorkspaceError(f"{shown!r} isn't a file" if real.exists() else f"there's no file {shown!r}")
        kind = kind_of(real)
        if kind == "picture":
            return self._picture(real, shown)
        if kind == "video":
            return self._video_frames(real, shown)
        if kind == "document":
            return self._document_pictures(real, shown)
        raise WorkspaceError(f"{shown!r} isn't a picture, video or document"
                             + ("; read it with read_file" if kind == "text" else ""))

    def _picture(self, real: Path, shown: str) -> Viewed:
        try:
            size = real.stat().st_size
            if size > pictures.MAX_BYTES:
                raise WorkspaceError(f"{shown!r} is {pictures.megabytes(size)}, over the "
                                     f"{pictures.megabytes(pictures.MAX_BYTES)} a picture can be")
            with real.open("rb") as handle:
                data = handle.read(pictures.MAX_BYTES + 1)
        except OSError as exc:
            raise WorkspaceError(f"can't read {shown!r}: {exc.strerror or type(exc).__name__}") from exc
        if not (data.startswith(pictures.PNG_SIGNATURE) or data.startswith(b"\xff\xd8\xff")):
            raise WorkspaceError(f"{shown!r} isn't a PNG or JPEG, the only pictures the agent sends in the beta; "
                                 "the person can save it as one of those")
        try:
            picture = pictures.read_picture(data)
        except pictures.PictureError as exc:
            raise WorkspaceError(f"{shown!r}: {exc}") from None
        return Viewed(f"picture {shown}, {picture.width}x{picture.height}, {len(data)} bytes; it goes to you with "
                      "your next turn", [picture])

    def _video_frames(self, real: Path, shown: str) -> Viewed:
        """Up to MAX_VIDEO_FRAMES frames spread over a video, taken by ffmpeg into a private temp folder (each
        at most 1280 pixels wide) and cleaned like any picture. Without ffmpeg, only the video's size."""
        from ixel_mat.agents.launch import find_on_path
        try:
            size = real.stat().st_size
        except OSError as exc:
            raise WorkspaceError(f"can't read {shown!r}: {exc.strerror or type(exc).__name__}") from exc
        ffmpeg = find_on_path("ffmpeg")
        # A batch file would run through cmd.exe, which reads the filter's commas and brackets as its own syntax
        if ffmpeg is not None and Path(ffmpeg).suffix.lower() in (".cmd", ".bat"):
            ffmpeg = None
        if ffmpeg is None:
            return Viewed(f"[video {shown}, {size} bytes; ffmpeg isn't installed, so its frames can't be shown. "
                          f"With ffmpeg on PATH, view_file would show up to {MAX_VIDEO_FRAMES} frames spread "
                          "over it]")
        found: list[Picture] = []
        times: list[float] = []
        with tempfile.TemporaryDirectory(prefix="ixel-agent-") as tmp:
            folder = Path(tmp)
            try:
                # Asked only to describe the file, ffmpeg says its length and exits 1 (no output was named)
                about = _run_ffmpeg([ffmpeg, "-hide_banner", "-nostdin", "-i", str(real)], folder)
            except subprocess.TimeoutExpired:
                raise WorkspaceError(f"ffmpeg took over {FFMPEG_TIMEOUT:g} seconds to read {shown!r}") from None
            except OSError as exc:
                raise WorkspaceError(f"ffmpeg couldn't start: {exc.strerror or type(exc).__name__}") from None
            match = _DURATION.search(about)
            length = (int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])) if match else 0.0
            wanted = ([(i + 0.5) * length / MAX_VIDEO_FRAMES for i in range(MAX_VIDEO_FRAMES)] if length > 0
                      else [0.0])
            for number, at in enumerate(wanted, start=1):
                out = folder / f"frame-{number}.png"
                try:
                    _run_ffmpeg([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{at:.3f}",
                                 "-i", str(real), "-frames:v", "1", "-vf", "scale=min(1280\\,iw):-2", "-y",
                                 str(out)], folder)
                except (subprocess.TimeoutExpired, OSError):
                    break  # one frame that hangs or can't start: the rest would too
                try:
                    if not out.is_file() or out.stat().st_size > pictures.MAX_BYTES:
                        continue
                    found.append(pictures.read_picture(out.read_bytes()))
                    times.append(at)
                except (OSError, pictures.PictureError):
                    continue
        how_long = f"{length:.1f} s long" if length > 0 else "length unknown"
        if not found:
            raise WorkspaceError(f"ffmpeg couldn't take any frames from {shown!r} ({size} bytes, {how_long}); "
                                 "it may not be a video ffmpeg reads")
        at_times = ", ".join(f"{at:.1f} s" for at in times)
        return Viewed(f"video {shown}, {size} bytes, {how_long}; {len(found)} frame{'' if len(found) == 1 else 's'} "
                      f"(at {at_times}) go to you with your next turn", found)

    def _document_pictures(self, real: Path, shown: str) -> Viewed:
        """A document's first PNG and JPEG pictures, cleaned, at most MAX_PICTURES_PER_CALL of them."""
        doc = self._document(real, shown)
        if not doc.images:
            return Viewed(f"{shown} is {_a(doc.kind)} with no pictures; read_file reads its text")
        found, notes, _ = documents.pictures_of(doc, room=MAX_PICTURES_PER_CALL,
                                                max_bytes=MAX_PICTURE_BYTES_PER_CALL)
        lines = [f"{shown} is {_a(doc.kind)} with {len(doc.images)} picture{'' if len(doc.images) == 1 else 's'}; "
                 + (f"the first {len(found)} go to you with your next turn" if found
                    else "none of them is one the agent can send")]
        lines += [f"[{mask_secrets(note)}]" for note in notes]
        return Viewed("\n".join(lines), found)

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
            if self.computer:
                allowed = []
                for name in dirnames:
                    try:
                        self.resolve(str(Path(dirpath) / name))
                    except WorkspaceError:
                        continue
                    allowed.append(name)
                dirnames[:] = allowed
            for name in sorted(filenames):
                yield Path(dirpath) / name

    # ── Computer access ──────────────────────────────────────────────────────

    def screenshot(self, cancel: threading.Event | None = None) -> Viewed:
        """Take an approved screenshot on a Mac into a private temp file, then clean it like view_file.
        The image lives only in memory once this returns; the temporary file and folder are removed."""
        if not self.computer:
            raise WorkspaceError("unknown action 'screenshot'; Computer access is off")
        if sys.platform != "darwin":
            return Viewed("screenshot isn't supported on this computer; it is macOS only")
        with tempfile.TemporaryDirectory(prefix="ixel-agent-screen-") as tmp:
            with tempfile.NamedTemporaryFile(dir=tmp, suffix=".png") as capture:
                result = run_command(["screencapture", "-x", capture.name], self.root, computer=True, cancel=cancel)
                if result.returncode != 0 or result.timed_out or result.cancelled:
                    raise WorkspaceError(f"screenshot failed: {result.output}")
                return self._picture(Path(capture.name), "screenshot")

    def open_argv(self, target: str) -> list[str]:
        """Check what an open would do and return the exact words to approve. A bare app name uses open -a
        on macOS; paths are resolved with the same home and secret checks as any file action."""
        if not self.computer:
            raise WorkspaceError("unknown action 'open'; Computer access is off")
        if not isinstance(target, str) or not target.strip() or "\x00" in target:
            raise WorkspaceError("give a path, an https:// link or an app name to open")
        if target.startswith("https://"):
            try:
                link = urlsplit(target)
                valid = bool(link.hostname)
            except ValueError:
                valid = False
            if not valid or any(c.isspace() for c in target):
                raise WorkspaceError("give a complete https:// link to open")
            opened = target
        elif ":" in target and not PureWindowsPath(target).drive:
            raise WorkspaceError("only https:// links can be opened")
        elif (target.startswith(("~", ".")) or "/" in target or "\\" in target
              or PureWindowsPath(target).drive or (self.root / target).exists()
              or (Path(target).suffix and Path(target).suffix.lower() != ".app")):
            opened = str(self.resolve(target))
        elif sys.platform == "darwin":
            if target.startswith("-"):
                raise WorkspaceError("an app name can't start with '-'")
            return ["open", "-a", target]
        else:
            raise WorkspaceError("opening an app by name is supported only on macOS; give a path or https:// link")
        if sys.platform == "darwin":
            return ["open", "--", opened]
        if sys.platform == "win32":
            return ["start", opened]
        return ["xdg-open", opened]

    def open(self, target: str, expected_argv: list[str] | None = None,
             cancel: threading.Event | None = None) -> CommandResult:
        """Open an approved path, link or app, with an argv list and never a command shell. Windows' start is
        a cmd.exe built-in, so its direct equivalent, os.startfile, opens the target without that shell."""
        argv = self.open_argv(target)
        if expected_argv is not None and argv != expected_argv:
            raise WorkspaceError("the open target changed since it was shown; propose the action again")
        if sys.platform == "win32":
            if cancel is not None and cancel.is_set():
                return CommandResult(argv, None, "[stopped by the person]", False, False, cancelled=True)
            os.startfile(argv[1])
            return CommandResult(argv, 0, "", False, False)
        return run_command(argv, self.root, computer=True, cancel=cancel)

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

    def _current_text(self, real: Path, path: str) -> tuple[str, tuple[int, int, str] | None]:
        """The text and fingerprint of a file about to be changed, refusing what can't be edited."""
        if real.exists() and not real.is_file():
            raise WorkspaceError(f"{path!r} isn't a file")
        data, fingerprint = _snapshot(real)
        if _looks_binary(data):
            raise WorkspaceError(f"{path!r} isn't a text file, so the agent can't change it")
        try:
            return data.decode("utf-8"), fingerprint
        except UnicodeDecodeError as exc:
            raise WorkspaceError(f"{path!r} isn't UTF-8 text, so the agent can't change it") from exc

    def _pending(self, real: Path, old: str, new: str,
                 fingerprint: tuple[int, int, str] | None) -> PendingWrite:
        if "\x00" in new:
            raise WorkspaceError("the new text contains a NUL character; the agent only writes text")
        if len(new.encode("utf-8")) > MAX_WRITE_BYTES:
            raise WorkspaceError("the new text is over 1 MB, too big to write")
        if real.exists() and new == old:
            raise WorkspaceError("that changes nothing: the file already says that")
        diff = _unified_diff(self._rel(real), old, new, is_new=fingerprint is None)
        return PendingWrite(path=real, new_text=new, diff=diff, fingerprint=fingerprint)

    def prepare_write(self, path: str, content: str) -> PendingWrite:
        """Check a whole-file write and make its diff (against the current file, or empty for a new one)."""
        if not isinstance(content, str):
            raise WorkspaceError("content must be text")
        real = self.resolve(path)
        if real == self.root:
            raise WorkspaceError("name a file to write, not the workspace itself")
        old, fingerprint = self._current_text(real, path)
        return self._pending(real, old, content, fingerprint)

    def prepare_edit(self, path: str, old: str, new: str) -> PendingWrite:
        """Check an edit that replaces old with new, where old appears exactly once, and make its diff."""
        if not isinstance(old, str) or not old:
            raise WorkspaceError("old must be the exact text to replace")
        if not isinstance(new, str):
            raise WorkspaceError("new must be text")
        real = self.resolve(path)
        if not real.is_file():
            raise WorkspaceError(f"there's no file {path!r} to edit; use write_file to make one")
        text, fingerprint = self._current_text(real, path)
        # A file with Windows line endings: a model copies what read_file showed it with plain \n, so the
        # edit is matched (and written) with the file's own \r\n
        if "\r\n" in text and "\r\n" not in old and "\n" in old and text.count(old) == 0:
            old, new = old.replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")
        count = text.count(old)
        if count == 0:
            raise WorkspaceError(f"old appears 0 times in {path!r}; copy it exactly from read_file")
        if count > 1:
            raise WorkspaceError(f"old appears {count} times in {path!r}; include more of the lines around it "
                                 "so it appears once")
        return self._pending(real, text, text.replace(old, new, 1), fingerprint)

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
                if self.resolve(rel) != pending.path or _fingerprint(pending.path) != pending.fingerprint:
                    raise WorkspaceError(f"{rel} changed while the change was being written; propose it again")
                os.replace(tmp_path, pending.path)
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise WorkspaceError(f"can't write {rel}: {exc.strerror or type(exc).__name__}") from exc
        return f"wrote {rel} ({len(data)} bytes)"
