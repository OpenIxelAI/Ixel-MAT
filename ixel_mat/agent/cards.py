"""
Adding a tool from a GitHub link: fetch the repo, hash it, check the add-on card and write it.

These are the functions behind the agent's fetch_repo, tree_hash and write_addon_card actions. Nothing here
runs a program: a repo is downloaded as a tarball from codeload (no git, no shell) and unpacked by hand, file
by file, into the agent's scratch/ folder, where the agent reads it as scratch:<owner>-<repo>.

  parse_repo_url, resolve_commit, fetch_repo   a link to a full commit, and a private copy of its files
  tree_hash                                    the hash a card's [source] carries, worked out here and never
                                               by the model
  validate_card, check_source, prepare_card    every rule a card has to meet, and that its [source] is what
                                               this run fetched and hashed
  write_card                                   <addons>/<id>/addon.toml, private, after the person approved
                                               the whole card

The card's rules and the tree hash follow docs/addon-cards.md (on the open-source add-ons branch) and are all
kept in this one module. That file wasn't in the folder this was written in, so the rules below are the
task's own description of it: compare them with the file, and change them here if they differ.

A card never turns itself on: the keys that would (enabled, approved) are refused, and the only file written
is addon.toml. The person reviews and approves it in Settings › Add-ons.

The card, as validate_card reads it (anything else is refused):

  id = "example-tool"            required: [a-z0-9][a-z0-9-]{0,62}, and the id write_addon_card was given
  name = "Example tool"          required: 1 to 80 characters, no control characters
  description = "…"              optional: at most 500 characters
  kind = "cli"                   required: "skill", "mcp" or "cli"
  commands = [["{addon}/run", "--out", "{output}"]]
                                 argv lists, never a shell. skill: none; mcp: exactly one; cli: at least
                                 one. {addon} and {output} are the only placeholders, and {output} needs
                                 permissions.files other than "none". No shell, and no interpreter given
                                 code (sandbox.is_shell_or_interpreter).
  [source]                       required: where the files came from
    repo = "https://github.com/<owner>/<repo>"
    commit = "<40 lowercase hex>"
    subdir = ""                  optional: the folder installed, '' for the whole repo
    tree_sha256 = "<64 lowercase hex>"   tree_hash's answer for that folder
    files = 12                   tree_hash's file count
  [permissions]                  optional; the narrowest is the default
    network = false              a bool
    files = "none"               "none", "output-folder" or "chosen-folder"
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import threading
import unicodedata
import uuid
import zlib
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

from ixel_mat.agent.sandbox import is_shell_or_interpreter
from ixel_mat.config.secrets import write_private_file

# Use tomllib (3.11+) or fall back to tomli, the way the config loader does
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore

# Read when they're used (not copied), so tests can point the first two at a local server and make the
# limits small
GITHUB_API = "https://api.github.com"
CODELOAD = "https://codeload.github.com"
MAX_REPO_BYTES = 200 * 1024 * 1024    # the download, and all of a repo's files together
MAX_REPO_FILES = 20_000
README_LINES = 40

FETCH_TIMEOUT = 300.0                 # seconds for the whole download
API_TIMEOUT = 30.0                    # seconds to ask GitHub which commit a name is
MAX_API_BYTES = 8 * 1024 * 1024
MAX_CARD_BYTES = 32 * 1024
MAX_NAME_CHARS = 80
MAX_DESCRIPTION_CHARS = 500
MAX_COMMANDS = 20
MAX_WORDS = 64
MAX_WORD_CHARS = 1000
MAX_SUBDIR_CHARS = 200
MAX_LISTED = 60                       # top-level entries shown in a fetch's result
MAX_SKIPPED_LISTED = 20
MAX_README_BYTES = 64 * 1024
MAX_README_LINE_CHARS = 300
_CHUNK = 64 * 1024

_OWNER = re.compile(r"[A-Za-z0-9-]{1,39}")
_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}")
_REPO_URL = re.compile(r"https://github\.com/([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?")
_SCRATCH_NAME = re.compile(r"[A-Za-z0-9-]{1,39}-[A-Za-z0-9._-]{1,100}")
_REF = re.compile(r"[A-Za-z0-9._/-]+")
_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_TREE_SHA = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_DRIVE = re.compile(r"[A-Za-z]:")
_PLACEHOLDER = re.compile(r"\{[^{}]*\}")

KINDS = ("skill", "mcp", "cli")
FILE_MODES = ("none", "output-folder", "chosen-folder")
_CARD_KEYS = frozenset({"id", "name", "description", "kind", "commands", "source", "permissions"})
_SOURCE_KEYS = frozenset({"repo", "commit", "subdir", "tree_sha256", "files"})
_PERMISSION_KEYS = frozenset({"network", "files"})
_TURNS_ON = frozenset({"enabled", "approved"})
_PLACEHOLDERS = ("{addon}", "{output}")
_SUBDIR_RULE = ("must be '' (the whole repo) or a folder inside it like 'tools/x': relative, with '/', "
                "no '..', no backslash")
# pax's extra headers: tarfile reads them into the entry after them, but one that shows up is ignored
_HEADER_TYPES = (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE)
_HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "ixel-mat"}


class CardError(ValueError):
    """A card that breaks a rule. key is the dotted card key at fault ('source.commit', 'permissions.files',
    'commands'), or '' for the whole card; the message starts with it."""

    def __init__(self, key: str, why: str):
        self.key = key
        super().__init__(f"{key}: {why}" if key else why)


# ── Links, commits and folders ───────────────────────────────────────────────

def _check_owner(owner: object) -> str:
    if (not isinstance(owner, str) or not _OWNER.fullmatch(owner) or owner.startswith("-")
            or owner.endswith("-")):
        raise ValueError("that isn't a GitHub owner name (letters, digits and single hyphens, at most 39)")
    return owner


def _check_repo(repo: object) -> str:
    if not isinstance(repo, str) or not _REPO.fullmatch(repo) or repo in (".", ".."):
        raise ValueError("that isn't a GitHub repo name (letters, digits, . _ and -, at most 100)")
    return repo


def parse_repo_url(url: str) -> tuple[str, str]:
    """(owner, repo) of https://github.com/<owner>/<repo>, which may end in '/' or '.git'. ValueError for
    anything else: another host, user info, a port, a query, a fragment or a longer path."""
    if not isinstance(url, str):
        raise ValueError("the repo link must be text")
    match = _REPO_URL.fullmatch(url.strip())
    if not match:
        raise ValueError("the repo link must look like https://github.com/<owner>/<repo>")
    return _check_owner(match.group(1)), _check_repo(match.group(2))


def scratch_name(owner: str, repo: str) -> str:
    """The folder a fetched repo is kept in, and what follows scratch: in the agent's paths."""
    return f"{owner}-{repo}"


def repo_url(owner: str, repo: str) -> str:
    """The one way a repo's link is written, in a card's [source] repo."""
    return f"https://github.com/{owner}/{repo}"


def is_scratch_name(name: object) -> bool:
    """Whether name could be one fetch_repo made: <owner>-<repo>, one plain folder name."""
    return (isinstance(name, str) and _SCRATCH_NAME.fullmatch(name) is not None
            and not name.startswith("-") and name not in (".", ".."))


def check_subdir(subdir: object) -> str:
    """A card's subdir: '' (the whole repo) or a relative folder written with '/', no empty, '.', '..' or
    '.git' part, no backslash, drive name or control character. ValueError otherwise."""
    if not isinstance(subdir, str):
        raise ValueError(_SUBDIR_RULE)
    if subdir == "":
        return ""
    if len(subdir) > MAX_SUBDIR_CHARS or "\\" in subdir or subdir.startswith("/") or _has_control(subdir):
        raise ValueError(_SUBDIR_RULE)
    for part in subdir.split("/"):
        if part in ("", ".", "..") or part.lower() == ".git" or _DRIVE.match(part):
            raise ValueError(_SUBDIR_RULE)
    return subdir


def _has_control(text: str, allowed: str = "") -> bool:
    """Whether text holds a control, format or other invisible character (except those in allowed)."""
    return any(unicodedata.category(char).startswith("C") and char not in allowed for char in text)


def _size_text(count: int) -> str:
    return f"{count // (1024 * 1024)} MB" if count >= 1024 * 1024 else f"{count} bytes"


def _why(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__


def _check_status(status: int, what: str) -> None:
    """ValueError with a short reason unless GitHub answered 200."""
    if status == 200:
        return
    if 300 <= status < 400:
        raise ValueError(f"GitHub moved {what} (HTTP {status}); give the repo's current link")
    if status == 404:
        raise ValueError(f"GitHub has no {what} (HTTP 404): is the repo public, and the commit right?")
    if status in (403, 429):
        raise ValueError(f"GitHub is limiting requests for {what} (HTTP {status}); try again later")
    raise ValueError(f"GitHub answered HTTP {status} for {what}")


async def _read_limited(resp: aiohttp.ClientResponse, limit: int) -> bytes:
    data = bytearray()
    async for chunk in resp.content.iter_chunked(_CHUNK):
        data += chunk
        if len(data) > limit:
            raise ValueError("GitHub's answer was far too large")
    return bytes(data)


async def resolve_commit(owner: str, repo: str, ref: str | None = None) -> str:
    """The full commit sha (40 lowercase hex characters) of ref, which is HEAD when it's empty. A full sha is
    returned as it is, without asking GitHub; a branch, a tag or a short sha is looked up through
    GET {GITHUB_API}/repos/<owner>/<repo>/commits/<ref>. ValueError when it can't be found."""
    _check_owner(owner)
    _check_repo(repo)
    if ref is not None and not isinstance(ref, str):
        raise ValueError("a commit must be text")
    ref = (ref or "").strip() or "HEAD"
    if _FULL_SHA.fullmatch(ref):
        return ref.lower()
    if (not _REF.fullmatch(ref) or ".." in ref or ref.startswith("/") or ref.endswith("/") or "//" in ref):
        raise ValueError("a commit is a 40-character sha, a short sha, a branch or a tag "
                         "(letters, digits and . _ - /)")
    url = f"{GITHUB_API}/repos/{owner}/{repo}/commits/{ref}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)) as session:
            # No redirects: this answer decides what gets downloaded, and a moved repo should be asked again
            async with session.get(url, headers=_HEADERS, allow_redirects=False) as resp:
                _check_status(resp.status, f"commit {ref!r} of {owner}/{repo}")
                raw = await _read_limited(resp, MAX_API_BYTES)
    except (asyncio.TimeoutError, TimeoutError):
        raise ValueError("GitHub didn't answer in time") from None
    except aiohttp.ClientError as exc:
        raise ValueError(f"couldn't reach GitHub ({type(exc).__name__})") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError("GitHub's answer for the commit wasn't JSON") from None
    sha = data.get("sha") if isinstance(data, dict) else None
    if not isinstance(sha, str) or not _COMMIT.fullmatch(sha.lower()):
        raise ValueError("GitHub's answer for the commit has no sha")
    return sha.lower()


# ── Unpacking ────────────────────────────────────────────────────────────────

def _remove(path: Path) -> None:
    """Delete a file, a link or a folder and what's in it; nothing happens when it's gone or can't go."""
    with suppress(OSError):
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path, ignore_errors=True)


def _make_private(folder: Path) -> None:
    """Make folder, and any folder above it that's missing, private to this user (0700) where the system
    allows."""
    missing = []
    current = folder
    while not current.is_dir() and current != current.parent:
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        path.mkdir(mode=0o700, exist_ok=True)
        if os.name == "posix":
            with suppress(OSError):
                os.chmod(path, 0o700)


def _entry_parts(name: str) -> list[str]:
    """The parts of a tar entry's path, or ValueError naming it when it's absolute, has a '..' part, a
    backslash or a drive name."""
    shown = repr(name[:120])
    if "\x00" in name or "\\" in name:
        raise ValueError(f"{shown}: a backslash or a NUL in a path")
    if name.startswith("/") or _DRIVE.match(name):
        raise ValueError(f"{shown}: an absolute path")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    for part in parts:
        if part == "..":
            raise ValueError(f"{shown}: a '..' in a path")
        if _DRIVE.match(part):
            raise ValueError(f"{shown}: a drive name in a path")
    return parts


def extract_tarball(archive: Path, dest: Path, cancel: threading.Event | None = None) -> list[str]:
    """Unpack a .tar.gz (codeload's '<repo>-<sha>/…') into dest by hand, with its first folder stripped, and
    return the paths (inside the copy, first folder stripped) of what was left out.

    Only regular files and folders are written, each file with open(…, 'xb') (never over another), mode
    0o755 when the owner could run it and 0o644 otherwise. Links (symbolic and hard), devices, fifos and
    anything else are not extracted, and are what's returned. ValueError, naming the entry or the limit,
    for: an absolute path, a '..' part, a backslash or a drive name in any entry; more than MAX_REPO_FILES
    files or MAX_REPO_BYTES of them in all; a path twice; an archive that can't be read. When it fails,
    everything it wrote is removed again (dest too, when this call made it). cancel, when set from another
    thread, stops it the same way. tarfile.extractall is never used."""
    dest = Path(dest)
    made = not dest.exists()
    dest.mkdir(parents=True, exist_ok=True)
    before = set(os.listdir(dest))
    root = Path(os.path.realpath(dest))
    skipped: list[str] = []
    files = entries = total = 0
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                if cancel is not None and cancel.is_set():
                    raise ValueError("stopped")
                if member.type in _HEADER_TYPES:
                    continue
                entries += 1
                if entries > MAX_REPO_FILES * 2:  # folders and links count too, so a huge list can't pile up
                    raise ValueError(f"the archive has more than {MAX_REPO_FILES * 2} entries")
                inner = _entry_parts(member.name)[1:]
                if not inner:  # the archive's own folder (or something loose beside it)
                    if member.isdir():
                        continue
                    if not member.isreg():
                        skipped.append(member.name)
                        continue
                    raise ValueError(f"{member.name!r}: a file outside the repo's folder")
                shown = "/".join(inner)
                target = root.joinpath(*inner)
                if not Path(os.path.realpath(target)).is_relative_to(root):
                    raise ValueError(f"{shown!r}: leads outside the copy")
                if member.isdir():
                    try:
                        target.mkdir(parents=True, exist_ok=True)
                    except OSError as exc:
                        raise ValueError(f"{shown!r}: can't be made ({_why(exc)})") from None
                    continue
                if not member.isreg():
                    skipped.append(shown)
                    continue
                files += 1
                total += member.size
                if files > MAX_REPO_FILES:
                    raise ValueError(f"the repo has more than {MAX_REPO_FILES} files")
                if total > MAX_REPO_BYTES:
                    raise ValueError(f"the repo's files are over {_size_text(MAX_REPO_BYTES)}")
                source = tar.extractfile(member)
                if source is None:
                    skipped.append(shown)
                    continue
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source, open(target, "xb") as out:
                        shutil.copyfileobj(source, out, _CHUNK)
                except FileExistsError:
                    raise ValueError(f"{shown!r}: the archive has this path twice, or as a folder and a "
                                     "file") from None
                except OSError as exc:
                    raise ValueError(f"{shown!r}: can't be unpacked ({_why(exc)})") from None
                with suppress(OSError):
                    os.chmod(target, 0o755 if member.mode & stat.S_IXUSR else 0o644)
    except (tarfile.TarError, EOFError, zlib.error, OSError) as exc:  # not ours: the archive itself is bad
        _discard(dest, before, made)
        raise ValueError(f"the download isn't a readable .tar.gz ({type(exc).__name__})") from None
    except BaseException:
        _discard(dest, before, made)
        raise
    return skipped


def _discard(dest: Path, before: set[str], made: bool) -> None:
    """Undo a failed unpack: dest itself when it was made for it, else what it added to dest."""
    if made:
        _remove(dest)
        return
    with suppress(OSError):
        for name in os.listdir(dest):
            if name not in before:
                _remove(dest / name)


# ── The tree hash ────────────────────────────────────────────────────────────

def tree_hash(folder: Path, subdir: str = "") -> tuple[str, int]:
    """(tree_sha256, files) of folder/subdir, the way the card spec says.

    Every regular file under folder/subdir, at any depth, counts. Symbolic links aren't followed or counted
    (neither a link to a file nor one to a folder), and nothing else is. Each file gives one line

        <sha256 of its bytes, lowercase hex>  <its path from folder/subdir, with '/', UTF-8>\\n

    exactly as `sha256sum` writes it (two spaces). The lines are put in order of the paths' UTF-8 bytes,
    joined with nothing between them, and tree_sha256 is the lowercase hex SHA-256 of that text; files is
    how many lines there are. subdir is '' for the whole of folder, else as check_subdir allows.

    ValueError when subdir isn't allowed, folder/subdir isn't a folder, goes through a link, holds no
    regular file, or holds more than MAX_REPO_FILES."""
    try:
        sub = check_subdir(subdir)
    except ValueError as exc:
        raise ValueError(f"subdir {exc}") from None
    base = Path(folder)
    for part in (sub.split("/") if sub else []):
        base = base / part
        if base.is_symlink():
            raise ValueError(f"{sub!r} goes through a link")
    if not base.is_dir():
        raise ValueError(f"there's no folder {sub!r} in that copy" if sub else "there's no copy to hash")
    found: list[tuple[bytes, Path]] = []
    for here, _folders, names in os.walk(base, followlinks=False):
        for name in names:
            path = Path(here) / name
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    continue
            except OSError:
                continue
            found.append((path.relative_to(base).as_posix().encode("utf-8", "surrogateescape"), path))
            if len(found) > MAX_REPO_FILES:
                raise ValueError(f"more than {MAX_REPO_FILES} files to hash")
    if not found:
        raise ValueError("there's nothing to hash: no regular files there")
    found.sort(key=lambda item: item[0])
    whole = hashlib.sha256()
    for rel, path in found:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(chunk)
        whole.update(digest.hexdigest().encode("ascii") + b"  " + rel + b"\n")
    return whole.hexdigest(), len(found)


# ── Fetching ─────────────────────────────────────────────────────────────────

@dataclass
class FetchResult:
    """A repo fetched into the agent's scratch folder. folder is scratch/<name>/; top_level lists its first
    level (folders end in '/'); readme is the first README_LINES lines of its top-level README, or '';
    skipped lists what wasn't copied (links, devices …); tree_sha256 and files are for the whole copy."""
    name: str
    owner: str
    repo: str
    repo_url: str
    commit: str
    folder: Path
    top_level: list[str]
    readme: str
    tree_sha256: str
    files: int
    skipped: list[str] = field(default_factory=list)

    def text(self) -> str:
        """What the step's result tells the model."""
        shown = self.top_level[:MAX_LISTED]
        more = len(self.top_level) - len(shown)
        top = ", ".join(shown) if shown else "(nothing)"
        if more > 0:
            top += f", … and {more} more"
        lines = [f"Fetched {self.repo_url} at commit {self.commit}.",
                 f"It is in scratch:{self.name}; read it with list_dir, read_file, search and view_file "
                 f"(paths like scratch:{self.name}/README.md).",
                 f"Top level: {top}"]
        if self.readme:
            lines += [f"README (first {README_LINES} lines):", self.readme]
        if self.skipped:
            left = ", ".join(self.skipped[:MAX_SKIPPED_LISTED])
            if len(self.skipped) > MAX_SKIPPED_LISTED:
                left += f", … and {len(self.skipped) - MAX_SKIPPED_LISTED} more"
            lines.append(f"Not copied (links, devices and other special entries): {left}")
        lines.append(f'To fill in a card\'s [source], call tree_hash with repo "{self.name}" and the subdir the '
                     "card installs ('' for the whole repo): it gives tree_sha256 and files. "
                     f"For the whole copy that is {self.files} files, tree_sha256 {self.tree_sha256}.")
        return "\n".join(lines)


async def _download(url: str, target: Path) -> None:
    """Save url to target, stopping at MAX_REPO_BYTES. ValueError for anything but a plain 200."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT)) as session:
            async with session.get(url, headers=_HEADERS, allow_redirects=False) as resp:
                _check_status(resp.status, "the download")
                if resp.content_length is not None and resp.content_length > MAX_REPO_BYTES:
                    raise ValueError(f"the repo is over {_size_text(MAX_REPO_BYTES)}")
                total = 0
                with open(target, "wb") as handle:
                    async for chunk in resp.content.iter_chunked(_CHUNK):
                        total += len(chunk)
                        if total > MAX_REPO_BYTES:
                            raise ValueError(f"the repo is over {_size_text(MAX_REPO_BYTES)}")
                        handle.write(chunk)
    except (asyncio.TimeoutError, TimeoutError):
        raise ValueError("GitHub didn't send the repo in time") from None
    except aiohttp.ClientError as exc:
        raise ValueError(f"couldn't download the repo from GitHub ({type(exc).__name__})") from None


def _describe(folder: Path) -> tuple[list[str], str]:
    """A copy's top-level entries, and the first README_LINES lines of its README ('' when it has none)."""
    names: list[str] = []
    readmes: list[Path] = []
    for entry in sorted(folder.iterdir(), key=lambda path: path.name):
        plain = not entry.is_symlink()
        is_folder = plain and entry.is_dir()
        names.append(entry.name + ("/" if is_folder else ""))
        if plain and entry.is_file() and entry.name.lower().startswith("readme"):
            readmes.append(entry)
    readme = ""
    if readmes:
        best = min(readmes, key=lambda path: (path.name.lower() != "readme.md", path.name))
        with suppress(OSError):
            with open(best, "rb") as handle:
                data = handle.read(MAX_README_BYTES)
            if b"\x00" not in data:
                text = data.decode("utf-8", errors="replace")
                readme = "\n".join(line[:MAX_README_LINE_CHARS] for line in text.splitlines()[:README_LINES])
    return names, readme


def _unpack(archive: Path, staging: Path, final: Path, cancel: threading.Event) -> tuple:
    """The blocking half of fetch_repo, run in a worker: unpack, hash, look, and only then put the copy in
    place of the old one."""
    skipped = extract_tarball(archive, staging, cancel)
    tree, files = tree_hash(staging)
    top_level, readme = _describe(staging)
    if cancel.is_set():
        raise ValueError("stopped")
    old: Path | None = None
    if os.path.lexists(final):  # the old copy goes aside first, and is deleted once the new one is there
        old = final.with_name(f".old-{uuid.uuid4().hex}")
        os.replace(final, old)
    try:
        os.replace(staging, final)
    except BaseException:
        if old is not None:
            with suppress(OSError):
                os.replace(old, final)
        raise
    if old is not None:
        _remove(old)
    return skipped, tree, files, top_level, readme


async def fetch_repo(url: str, commit: str | None, scratch_root: Path) -> FetchResult:
    """Download a GitHub repo at a commit (HEAD when none is given) and keep its regular files in
    scratch_root/<owner>-<repo>/, replacing an older copy only once the new one is complete.

    The commit is resolved to a full sha first (resolve_commit), then {CODELOAD}/<owner>/<repo>/tar.gz/<sha>
    is downloaded (at most MAX_REPO_BYTES, FETCH_TIMEOUT seconds) into a temp file under scratch_root and
    unpacked (extract_tarball) into a temp folder there. Both are removed on any failure, and when the run is
    stopped. scratch_root is made 0700 when it's missing. ValueError for a bad link or commit, a network or
    GitHub problem, or an archive that's refused."""
    owner, repo = parse_repo_url(url)
    sha = await resolve_commit(owner, repo, commit)
    name = scratch_name(owner, repo)
    scratch_root = Path(scratch_root)
    _make_private(scratch_root)
    archive: Path | None = None
    staging: Path | None = None
    try:
        handle, temp = tempfile.mkstemp(dir=scratch_root, prefix=".fetch-", suffix=".tar.gz")
        os.close(handle)
        archive = Path(temp)
        await _download(f"{CODELOAD}/{owner}/{repo}/tar.gz/{sha}", archive)
        staging = Path(tempfile.mkdtemp(dir=scratch_root, prefix=".copy-"))
        final = scratch_root / name
        cancel = threading.Event()
        work = asyncio.get_running_loop().run_in_executor(None, _unpack, archive, staging, final, cancel)
        try:
            skipped, tree, files, top_level, readme = await asyncio.shield(work)
        except asyncio.CancelledError:
            # The worker can't be interrupted: tell it to stop and wait, so the temp files are gone for good
            cancel.set()
            with suppress(Exception):
                await work
            raise
    finally:
        if archive is not None:
            _remove(archive)
        if staging is not None:
            _remove(staging)  # nothing left when the copy was put in place
    return FetchResult(name=name, owner=owner, repo=repo, repo_url=repo_url(owner, repo), commit=sha,
                       folder=final, top_level=top_level, readme=readme, tree_sha256=tree, files=files,
                       skipped=skipped)


# ── The card ─────────────────────────────────────────────────────────────────

def _parse(text: Any) -> dict[str, Any]:
    if not isinstance(text, str):
        raise CardError("", "the card must be TOML text")
    if len(text.encode("utf-8", "replace")) > MAX_CARD_BYTES:
        raise CardError("", f"the card is over {MAX_CARD_BYTES // 1024} KB; a card is a short file")
    if tomllib is None:
        raise CardError("", "no TOML parser is available: install tomli (pip install tomli) or use "
                            "Python 3.11+")
    try:
        return tomllib.loads(text)
    except (ValueError, RecursionError) as exc:
        raise CardError("", f"the card isn't valid TOML ({str(exc)[:200]})") from None


def _only_keys(table: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    """CardError for the first key a table has that it may not."""
    for key in table:
        dotted = (f"{where}.{key}" if where else str(key))[:80]
        if key in _TURNS_ON:
            raise CardError(dotted, "a card can't turn itself on; the person reviews and approves it in "
                                    "Settings › Add-ons")
        if key not in allowed:
            raise CardError(dotted, f"isn't a key a card has here (the keys are: {', '.join(sorted(allowed))})")


def _need(table: dict[str, Any], key: str, dotted: str) -> Any:
    if key not in table:
        raise CardError(dotted, "is required")
    return table[key]


def _table(value: Any, dotted: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CardError(dotted, "must be a table ([" + dotted + "])")
    return value


def _check_commands(value: Any, kind: str, files: str) -> list[list[str]]:
    if not isinstance(value, list):
        raise CardError("commands", "must be a list of commands, each a list of words")
    if kind == "skill" and value:
        raise CardError("commands", "a skill has no commands")
    if kind == "mcp" and len(value) != 1:
        raise CardError("commands", f"an mcp add-on has exactly one command (the server), not {len(value)}")
    if kind == "cli" and not value:
        raise CardError("commands", "a cli add-on needs at least one command")
    if len(value) > MAX_COMMANDS:
        raise CardError("commands", f"at most {MAX_COMMANDS} commands")
    checked: list[list[str]] = []
    for number, argv in enumerate(value, 1):
        where = f"command {number}"
        if not isinstance(argv, list) or not argv or len(argv) > MAX_WORDS:
            raise CardError("commands", f"{where} must be a list of 1 to {MAX_WORDS} words")
        for word in argv:
            if not isinstance(word, str) or not word or len(word) > MAX_WORD_CHARS or _has_control(word):
                raise CardError("commands", f"{where} has a word that isn't plain text (1 to "
                                            f"{MAX_WORD_CHARS} characters, no control characters)")
            for placeholder in _PLACEHOLDER.findall(word):
                if placeholder not in _PLACEHOLDERS:
                    raise CardError("commands", f"{where} uses {placeholder}; the only placeholders are "
                                                "{addon} and {output}")
                if placeholder == "{output}" and files == "none":
                    raise CardError("commands", f'{where} uses {{output}}, but permissions.files is "none": '
                                                "there is no output folder")
        if is_shell_or_interpreter(argv):
            raise CardError("commands", f"{where} runs a shell or an interpreter given code; a command must "
                                        "name the tool itself")
        checked.append(list(argv))
    return checked


def validate_card(text: str, card_id: str) -> dict[str, Any]:
    """Check a card's TOML against every rule (see the top of this file) and return it as a dict with the
    optional parts filled in: id, name, description, kind, commands, source {repo, commit, subdir,
    tree_sha256, files} and permissions {network, files}. CardError, with the key at fault, otherwise.
    card_id is the id write_addon_card was given; the card's own id must be the same.

    This checks the card on its own. That its [source] is what this run fetched is check_source's job."""
    data = _parse(text)
    _only_keys(data, _CARD_KEYS, "")

    value = _need(data, "id", "id")
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise CardError("id", "must be lowercase letters, digits and '-', 1 to 63 characters, starting with "
                              "a letter or digit")
    if not isinstance(card_id, str) or not _ID.fullmatch(card_id):
        raise CardError("id", "the id given to write_addon_card must be lowercase letters, digits and '-', "
                              "1 to 63 characters, starting with a letter or digit")
    if value != card_id:
        raise CardError("id", f"is {value!r} but the id given to write_addon_card is {card_id!r}; they must "
                              "be the same")

    name = _need(data, "name", "name")
    if (not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_CHARS
            or _has_control(name)):
        raise CardError("name", f"must be 1 to {MAX_NAME_CHARS} characters of text, no control characters")

    description = data.get("description", "")
    if (not isinstance(description, str) or len(description) > MAX_DESCRIPTION_CHARS
            or _has_control(description, "\n\t")):
        raise CardError("description", f"must be text of at most {MAX_DESCRIPTION_CHARS} characters")

    kind = _need(data, "kind", "kind")
    if kind not in KINDS:
        raise CardError("kind", f"must be one of {', '.join(repr(k) for k in KINDS)}")

    source = _table(_need(data, "source", "source"), "source")
    _only_keys(source, _SOURCE_KEYS, "source")
    repo = _need(source, "repo", "source.repo")
    try:
        owner, repo_name = parse_repo_url(repo)
    except ValueError as exc:
        raise CardError("source.repo", str(exc)) from None
    if repo != repo_url(owner, repo_name):
        raise CardError("source.repo", f"must be written exactly as {repo_url(owner, repo_name)}")
    commit = _need(source, "commit", "source.commit")
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise CardError("source.commit", "must be the full commit: 40 lowercase hex characters")
    try:
        subdir = check_subdir(source.get("subdir", ""))
    except ValueError as exc:
        raise CardError("source.subdir", str(exc)) from None
    tree = _need(source, "tree_sha256", "source.tree_sha256")
    if not isinstance(tree, str) or not _TREE_SHA.fullmatch(tree):
        raise CardError("source.tree_sha256", "must be 64 lowercase hex characters (tree_hash gives it)")
    files = _need(source, "files", "source.files")
    if isinstance(files, bool) or not isinstance(files, int) or not 1 <= files <= MAX_REPO_FILES:
        raise CardError("source.files", f"must be a whole number from 1 to {MAX_REPO_FILES} (tree_hash "
                                        "gives it)")

    permissions = _table(data.get("permissions", {}), "permissions")
    _only_keys(permissions, _PERMISSION_KEYS, "permissions")
    network = permissions.get("network", False)
    if not isinstance(network, bool):
        raise CardError("permissions.network", "must be true or false")
    file_access = permissions.get("files", "none")
    if file_access not in FILE_MODES:
        raise CardError("permissions.files", f"must be one of {', '.join(repr(m) for m in FILE_MODES)}")

    commands = _check_commands(data.get("commands", []), kind, file_access)
    return {"id": value, "name": name, "description": description, "kind": kind, "commands": commands,
            "source": {"repo": repo, "commit": commit, "subdir": subdir, "tree_sha256": tree, "files": files},
            "permissions": {"network": network, "files": file_access}}


def check_source(card: dict[str, Any], fetched: dict[str, dict[str, Any]]) -> None:
    """CardError unless the card's [source] is what this run fetched and hashed. card is validate_card's
    answer; fetched is the run's record, by scratch name:

        {"example-owner-example-tool": {"repo_url": "https://github.com/…", "commit": "<40 hex>",
                                        "hashes": {"": (tree_sha256, files), "tools/x": (…, …)}}}

    The keys at fault: source.repo (not fetched in this run), source.commit, source.subdir (no tree_hash for
    that folder yet), source.tree_sha256 and source.files."""
    source = card["source"]
    owner, repo = parse_repo_url(source["repo"])
    wanted = repo_url(owner, repo)
    entry = fetched.get(scratch_name(owner, repo))
    if entry is None:
        entry = next((item for item in fetched.values()
                      if str(item.get("repo_url", "")).lower() == wanted.lower()), None)
    if entry is None:
        raise CardError("source.repo", f"this run hasn't fetched {wanted}; use fetch_repo on it first")
    if source["commit"] != entry.get("commit"):
        raise CardError("source.commit", f"is {source['commit']}, but this run fetched {entry.get('commit')}")
    subdir = source["subdir"]
    found = (entry.get("hashes") or {}).get(subdir)
    if found is None:
        raise CardError("source.subdir", f"this run hasn't hashed {subdir!r}; call tree_hash with that subdir "
                                         "and use its answer")
    tree, files = found
    if source["tree_sha256"] != tree:
        raise CardError("source.tree_sha256", f"is {source['tree_sha256']}, but tree_hash computed {tree} "
                                              f"for {subdir!r}")
    if source["files"] != files:
        raise CardError("source.files", f"is {source['files']}, but tree_hash counted {files} for {subdir!r}")


def default_addons_dir() -> Path:
    """Where cards are kept, ~/.ixel/addons. Read each time, so tests can move it."""
    return Path.home() / ".ixel" / "addons"


def builtin_addons_dir() -> Path:
    """The cards that ship with Ixel (ixel_mat/addons), if the folder is there."""
    return Path(__file__).resolve().parent.parent / "addons"


def _is_builtin(card_id: str, folder: Path) -> bool:
    try:
        return folder.is_dir() and ((folder / card_id).exists() or (folder / f"{card_id}.toml").is_file())
    except OSError:
        return False


def _shown(path: Path) -> str:
    """A path as the person reads it: ~/… under the home folder."""
    with suppress(ValueError, RuntimeError):
        return "~/" + path.relative_to(Path.home()).as_posix()
    return str(path)


@dataclass
class PendingCard:
    """A card that's been checked and is waiting for the person's approval. text is exactly what will be
    written to path; overwrites says a card with this id is already there; summary is what the approval card
    shows (the whole card, and what happens to it)."""
    id: str
    text: str
    card: dict[str, Any]
    path: Path
    overwrites: bool
    summary: str
    builtin_dir: Path


def prepare_card(card_id: str, text: str, fetched: dict[str, dict[str, Any]], addons_dir: Path | None = None,
                 builtin_dir: Path | None = None) -> PendingCard:
    """Check a card in full (validate_card, then that its id isn't a built-in card's, then check_source
    against this run's fetched) and return it ready for approval; nothing is written. CardError otherwise.
    addons_dir defaults to ~/.ixel/addons and builtin_dir to ixel_mat/addons (None, or a missing folder,
    means no built-in cards)."""
    card = validate_card(text, card_id)
    builtin = Path(builtin_dir) if builtin_dir is not None else builtin_addons_dir()
    if _is_builtin(card_id, builtin):
        raise CardError("id", f"{card_id!r} is a built-in add-on's id; pick another id")
    check_source(card, fetched)
    folder = Path(addons_dir) if addons_dir is not None else default_addons_dir()
    path = folder / card_id / "addon.toml"
    overwrites = os.path.lexists(path)
    where = _shown(path)
    if overwrites:
        note = (f"A card with this id is already at {where}. Writing this replaces it, and that add-on "
                "must be approved again in Settings › Add-ons.")
    else:
        note = (f"This writes a new card to {where}. It stays off until you review and approve it in "
                "Settings › Add-ons.")
    summary = f"Add-on card {card['name']} ({card_id}), kind {card['kind']}.\n{note}\n\n{text}"
    return PendingCard(id=card_id, text=text, card=card, path=path, overwrites=overwrites, summary=summary,
                       builtin_dir=builtin)


def write_card(pending: PendingCard) -> str:
    """Write an approved card to <addons>/<id>/addon.toml (private, atomically) and return what to tell the
    model. The card is checked again first, and nothing is written if an add-on with this id appeared
    since it was prepared. The only file made is addon.toml; the card stays off until the person approves
    it in Settings › Add-ons."""
    validate_card(pending.text, pending.id)
    if _is_builtin(pending.id, pending.builtin_dir):
        raise CardError("id", f"{pending.id!r} is a built-in add-on's id; pick another id")
    folder = pending.path.parent
    if folder.is_symlink():
        raise ValueError(f"{_shown(folder)} is a link, not a folder")
    exists = os.path.lexists(pending.path)
    if exists and not pending.overwrites:
        raise ValueError(f"a card with the id {pending.id!r} appeared while this waited; ask again")
    _make_private(folder.parent)
    write_private_file(pending.path, pending.text.encode("utf-8"))
    return (f"Wrote {_shown(pending.path)}" + (" (replacing the old card)" if exists else "") + ". It is not "
            "turned on: tell the person to open Settings › Add-ons to review and approve it.")
