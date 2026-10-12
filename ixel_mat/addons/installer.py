"""
Installing an add-on: the files its card pins, fetched from GitHub and unpacked safely.

Every add-on is a card (docs/addon-cards.md), and the card holds the pin: a GitHub repo,
one commit, the folder in it to install, and the tree hash and file count of that folder.
Switching an add-on on downloads the GitHub tarball of that commit and unpacks only that
folder into ~/.ixel/addons/<id>/files/. Nothing else in the archive is ever written, and an
archive with anything odd in it (an absolute path, '..', a link, a device, an oversized
file) is refused whole, before a single file is written. The extracted files are checked
against the card's tree hash (the tarball's own bytes aren't stable enough to pin) before
they replace what was there. Switching it off removes files/.
"""
from __future__ import annotations

import hashlib
import io
import os
import posixpath
import shutil
import stat
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING

from ixel_mat.addons import paths

if TYPE_CHECKING:
    from ixel_mat.addons.cards import Card

__all__ = ["AddonError", "MAX_TARBALL_BYTES", "MAX_FILE_BYTES", "DOWNLOAD_TIMEOUT", "tarball_url", "download",
           "extract", "tree_hash", "installed", "install", "ensure_installed", "remove", "remove_legacy"]

MAX_TARBALL_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
DOWNLOAD_TIMEOUT = 120

_CHUNK = 64 * 1024
# A small gzip can unpack to a great deal: caps on what one archive may hold and write
_MAX_MEMBERS = 20_000
_MAX_FILES = 5_000
_MAX_EXTRACTED_BYTES = 200 * 1024 * 1024
# What install() keeps in a card's folder; the rest of a built-in card's folder is the old layout
_KEEP = ("files", "files.part", "files.old")


class AddonError(Exception):
    """Something an add-on couldn't do, said for a person; status is the HTTP status the
    page gets, code a short word it can act on."""

    def __init__(self, message: str, status: int = 400, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def tarball_url(card: Card) -> str:
    owner, repo = urllib.parse.urlsplit(card.source.repo).path.strip("/").split("/")
    return f"https://codeload.github.com/{owner}/{repo}/tar.gz/{card.source.commit}"


def installed(card: Card) -> bool:
    """True when the card's files/ is a real folder (not a link, nor reached through a
    linked card folder) with something in it."""
    files = card.files_dir
    if card.dir.is_symlink() or files.is_symlink() or not files.is_dir():
        return False
    try:
        return any(files.iterdir())
    except OSError:
        return False


def _https(url: str) -> bool:
    return urllib.parse.urlsplit(url).scheme.lower() == "https"


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _https(newurl):
            raise urllib.error.HTTPError(newurl, code, "a redirect away from https was refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_https(url: str, timeout: float):
    return urllib.request.build_opener(_HttpsOnlyRedirects).open(url, timeout=timeout)


def download(url: str, *, opener=None, max_bytes: int = MAX_TARBALL_BYTES) -> bytes:
    """The bytes at url (https only), at most max_bytes. Nothing is written to disk, and
    nothing is checked here: extract() checks the unpacked files against the pin.
    opener(url, timeout) returns something with .read(n); the default is urllib's, which
    refuses redirects away from https."""
    if not _https(url):
        raise AddonError("Add-ons are only downloaded over https.", 400, "not_https")
    try:
        response = (opener or _open_https)(url, DOWNLOAD_TIMEOUT)
    except (OSError, ValueError) as exc:  # URLError, HTTPError and timeouts are OSErrors
        raise AddonError(f"Couldn't download {url}: {exc}", 502, "download") from exc
    chunks: list[bytes] = []
    size = 0
    try:
        final = getattr(response, "geturl", None)
        if callable(final) and isinstance(final(), str) and not _https(final()):
            raise AddonError("The download was sent away from https.", 502, "not_https")
        while True:
            chunk = response.read(_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise AddonError(f"The download is bigger than {max_bytes // (1024 * 1024)} MB.", 502, "too_big")
            chunks.append(chunk)
    except (OSError, ValueError) as exc:
        raise AddonError(f"Couldn't download {url}: {exc}", 502, "download") from exc
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    return b"".join(chunks)


def _unsafe(name: str, why: str) -> AddonError:
    return AddonError(f"The archive was refused: {name!r} {why}. Nothing was written.", 502, "unsafe")


def _member_parts(member: tarfile.TarInfo, max_file_bytes: int) -> list[str]:
    """The member's normalised path as parts, or AddonError(code="unsafe")."""
    name = member.name
    if "\x00" in name:
        raise _unsafe(name, "has a NUL in its name")
    if "\\" in name:
        raise _unsafe(name, "has a backslash in its name")
    if name.startswith("/") or posixpath.isabs(name) or PureWindowsPath(name).drive:
        raise _unsafe(name, "is an absolute path")
    if ".." in name.split("/"):
        raise _unsafe(name, "goes up a folder ('..')")
    if not (member.isfile() or member.isdir()):
        raise _unsafe(name, "is a link, device or fifo, not a file or folder")
    if member.isfile() and member.size > max_file_bytes:
        raise _unsafe(name, f"is bigger than {max_file_bytes // (1024 * 1024)} MB")
    normal = posixpath.normpath(name)
    return [] if normal == "." else normal.split("/")


def _clear(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def tree_hash(root: Path) -> tuple[int, str]:
    """(how many regular files, sha256 hex) of the tree under root: the sha256 of the
    UTF-8 of f"{rel}\\0{sha}\\n" for every regular file, sorted by rel, where rel is its
    path under root with '/' separators and sha its own sha256 hex. Links are never
    followed, and only regular files count."""
    entries: list[tuple[str, str]] = []
    for here, _dirs, names in os.walk(root, followlinks=False):
        for each in names:
            path = os.path.join(here, each)
            if not stat.S_ISREG(os.lstat(path).st_mode):
                continue
            rel = Path(path).relative_to(root).as_posix()
            entries.append((rel, _file_sha256(path)))
    entries.sort()
    joined = "".join(f"{rel}\0{sha}\n" for rel, sha in entries)
    return len(entries), hashlib.sha256(joined.encode("utf-8")).hexdigest()


def extract(data: bytes, subdir: str, dest: Path, *, max_file_bytes: int = MAX_FILE_BYTES,
            expect: tuple[int, str] | None = None) -> int:
    """Unpack <topfolder>/<subdir>/... of a .tar.gz into dest, replacing what was there, and
    return how many files were written; subdir '' unpacks everything under the top folder.
    Every member is checked before anything is written: one unsafe member anywhere refuses
    the whole archive. Files are copied out one by one (never TarFile.extract/extractall)
    into a .part folder beside dest that then takes dest's place; the archive's own modes
    are ignored (files 0o644, folders 0o755). With expect, a (file count, tree_hash hex)
    pair, the .part folder must match it or it is removed and dest is left as it was."""
    if subdir == "":
        sub: list[str] = []
    else:
        sub = [p for p in subdir.strip("/").split("/") if p and p != "."]
        if not sub or ".." in sub:
            raise AddonError(f"{subdir!r} isn't a folder inside the archive.", 400, "unsafe")
    what = subdir or "the archive"
    depth = len(sub) + 1
    wanted: list[tuple[tarfile.TarInfo, list[str]]] = []
    files = total = 0
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise AddonError(f"The download isn't a readable .tar.gz: {exc}", 502, "bad_archive") from exc
    with archive:
        try:
            for count, member in enumerate(archive, 1):
                if count > _MAX_MEMBERS:
                    raise AddonError(f"The archive has more than {_MAX_MEMBERS} entries.", 502, "too_big")
                parts = _member_parts(member, max_file_bytes)
                if len(parts) <= depth or parts[1:depth] != sub:
                    continue
                wanted.append((member, parts[depth:]))
                if member.isfile():
                    files += 1
                    total += member.size
        except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
            raise AddonError(f"The download isn't a readable .tar.gz: {exc}", 502, "bad_archive") from exc
        if not files:
            raise AddonError(f"the archive has no {subdir or 'files'}", 502, "no_subdir")
        if files > _MAX_FILES or total > _MAX_EXTRACTED_BYTES:
            too_big = f"{subdir} in the archive is too big to install." if subdir else \
                "The archive is too big to install."
            raise AddonError(too_big, 502, "too_big")

        part = dest.with_name(dest.name + ".part")
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _clear(part)
        part.mkdir(mode=0o755)
        try:
            for member, rel in wanted:
                target = part.joinpath(*rel)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise _unsafe(member.name, "has no contents to read")
                written = 0
                with source, open(target, "wb") as out:
                    while chunk := source.read(_CHUNK):
                        written += len(chunk)
                        if written > max_file_bytes:
                            raise _unsafe(member.name, f"is bigger than {max_file_bytes // (1024 * 1024)} MB")
                        out.write(chunk)
            # Only folders and regular files were made in part, so walking it follows no links
            for root, dirs, names in os.walk(part):
                os.chmod(root, 0o755)
                for each in names:
                    os.chmod(os.path.join(root, each), 0o644)
            if expect is not None:
                count, digest = tree_hash(part)
                if count != expect[0] or digest != expect[1].strip().lower():
                    raise AddonError("The download isn't the release the card pins (its files don't match "
                                     "the card's pinned files), so nothing was installed.", 409, "tree_mismatch")
        except AddonError:
            _clear(part)
            raise
        except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
            _clear(part)
            raise AddonError(f"Couldn't unpack {what}: {exc}", 500, "write") from exc

    old = dest.with_name(dest.name + ".old")
    _clear(old)
    if dest.is_symlink() or dest.exists():
        os.replace(dest, old)
    try:
        os.replace(part, dest)
    except OSError as exc:
        if old.exists() and not dest.exists():
            os.replace(old, dest)
        _clear(part)
        raise AddonError(f"Couldn't put {dest.name} in place: {exc}", 500, "write") from exc
    _clear(old)
    return files


def _delete(path: Path) -> None:
    """Remove path whatever it is: a real folder with everything in it, anything else
    (a link included, never followed) unlinked."""
    if stat.S_ISDIR(os.lstat(path).st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def install(card: Card, *, fetch=None) -> Path:
    """Download the commit the card pins and unpack its subdir into card.files_dir. The
    unpacked files must match the card's tree hash and file count, or nothing is
    installed and the old files/ stays as it was. Then the old layout, if any, is cleared.
    fetch(url) -> bytes stands in for download in tests."""
    if card.dir.is_symlink():
        raise AddonError(f"~/.ixel/addons/{card.id} is a link; Ixel won't install through it. Remove the link and "
                         "switch it on again.", 409, "unsafe")
    data = (fetch or download)(tarball_url(card))
    paths.ADDONS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    fresh = not card.dir.exists()
    try:
        extract(data, card.source.subdir, card.files_dir, expect=(card.source.files, card.source.tree_sha256))
    except AddonError:
        # A refused install leaves nothing behind, not even the folder it made for files/
        if fresh:
            try:
                card.dir.rmdir()
            except OSError:
                pass
        raise
    remove_legacy(card)
    return card.files_dir


def ensure_installed(card: Card, *, fetch=None) -> bool:
    """Make sure the card's pinned files are installed, as the switch is turned on: files
    already there whose tree hash matches the card are kept, anything else is installed
    again. True when it downloaded."""
    if installed(card) and tree_hash(card.files_dir) == (card.source.files, card.source.tree_sha256):
        remove_legacy(card)
        return False
    install(card, fetch=fetch)
    return True


def remove(card: Card) -> bool:
    """Remove the card's installed files (a link is unlinked, never followed); an agent
    card's addon.toml always stays. True if something was there."""
    if card.dir.is_symlink():
        if not card.builtin:
            raise AddonError(f"~/.ixel/addons/{card.id} is a link; Ixel won't remove anything through it.",
                             409, "unsafe")
        card.dir.unlink()
        return True
    for leftover in (card.dir / "files.part", card.dir / "files.old"):
        _clear(leftover)
    removed = False
    if card.files_dir.is_symlink() or card.files_dir.exists():
        _delete(card.files_dir)
        removed = True
    if card.builtin:
        removed = remove_legacy(card) or removed
        try:
            card.dir.rmdir()  # only when empty
        except OSError:
            pass
    return removed


def remove_legacy(card: Card) -> bool:
    """Clear a built-in card's old install, from before cards: the files unpacked straight
    into ~/.ixel/addons/<id>/ (SKILL.md at its root), and the <id>.part and <id>.old
    folders beside it. True if it removed something.

    Such an install isn't carried over: only the Settings approval route writes state.json,
    so treating it as switched on would be an approval nobody gave. Its switch shows off,
    and turning it on again downloads into files/ and clears the old files here."""
    if not card.builtin:
        return False
    removed = False
    root = card.dir
    if not root.is_symlink() and root.is_dir():
        try:
            legacy = stat.S_ISREG(os.lstat(root / "SKILL.md").st_mode)
        except OSError:
            legacy = False
        if legacy:
            for entry in root.iterdir():
                if entry.name not in _KEEP:
                    _delete(entry)
                    removed = True
    for name in (f"{card.id}.part", f"{card.id}.old"):
        sibling = paths.ADDONS_DIR / name
        if sibling.is_symlink() or sibling.exists():
            _delete(sibling)
            removed = True
    return removed
