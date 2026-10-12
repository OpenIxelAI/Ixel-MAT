"""
Add-ons: other people's skills that Ixel can fetch and use, off until you switch them on.

There is one so far, Diagram Design (Cathryn Lavery's MIT-licensed skill for making a
diagram as one HTML file, in your brand). Switching it on downloads the GitHub tarball of
one pinned commit, checks its sha256 against the one this Ixel ships with, and unpacks
only skills/diagram-design/ into ~/.ixel/addons/diagram-design. Nothing else in the
archive is ever written, and an archive with anything odd in it (an absolute path, '..',
a link, a device, an oversized file) is refused whole, before a single file is written.
Switching it off removes the folder. Diagrams it makes are saved in ~/.ixel/diagrams.
"""
from __future__ import annotations

import hashlib
import io
import os
import posixpath
import shutil
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

ADDONS_DIR = Path.home() / ".ixel" / "addons"
DIAGRAMS_DIR = Path.home() / ".ixel" / "diagrams"
MAX_TARBALL_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
DOWNLOAD_TIMEOUT = 120
# Filled in by a person (the recipe is in the plan): until then install() refuses
PINNED_COMMIT = ""
PINNED_SHA256 = ""

_CHUNK = 64 * 1024
# A small gzip can unpack to a great deal: caps on what one archive may hold and write
_MAX_MEMBERS = 20_000
_MAX_FILES = 5_000
_MAX_EXTRACTED_BYTES = 200 * 1024 * 1024


@dataclass(frozen=True)
class Addon:
    name: str
    label: str
    about: str
    author: str
    license: str
    repo: str
    commit: str
    sha256: str
    subdir: str
    setting: str


DIAGRAM_DESIGN = Addon("diagram-design", "Diagram Design", "diagrams as one HTML file, in your brand",
                       "Cathryn Lavery", "MIT", "cathrynlavery/diagram-design", PINNED_COMMIT, PINNED_SHA256,
                       "skills/diagram-design", "diagram_design")
ADDONS = {DIAGRAM_DESIGN.name: DIAGRAM_DESIGN}


class AddonError(Exception):
    """Something an add-on couldn't do, said for a person; status is the HTTP status the
    page gets, code a short word it can act on."""

    def __init__(self, message: str, status: int = 400, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def tarball_url(addon: Addon) -> str:
    return f"https://codeload.github.com/{addon.repo}/tar.gz/{addon.commit}"


def folder(addon: Addon) -> Path:
    return ADDONS_DIR / addon.name


def installed(addon: Addon) -> bool:
    return (folder(addon) / "SKILL.md").is_file()


def _https(url: str) -> bool:
    return urllib.parse.urlsplit(url).scheme.lower() == "https"


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _https(newurl):
            raise urllib.error.HTTPError(newurl, code, "a redirect away from https was refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_https(url: str, timeout: float):
    return urllib.request.build_opener(_HttpsOnlyRedirects).open(url, timeout=timeout)


def download(url: str, expected_sha256: str, *, opener=None, max_bytes: int = MAX_TARBALL_BYTES) -> bytes:
    """The bytes at url (https only), at most max_bytes, and only when their sha256 is
    expected_sha256. Nothing is written to disk. opener(url, timeout) returns something
    with .read(n); the default is urllib's, which refuses redirects away from https."""
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
    data = b"".join(chunks)
    if hashlib.sha256(data).hexdigest() != expected_sha256.strip().lower():
        raise AddonError("The download isn't the release this Ixel was made for (its sha256 doesn't match), "
                         "so nothing was installed.", 409, "sha_mismatch")
    return data


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


def extract(data: bytes, subdir: str, dest: Path, *, max_file_bytes: int = MAX_FILE_BYTES) -> int:
    """Unpack <topfolder>/<subdir>/... of a .tar.gz into dest, replacing what was there, and
    return how many files were written. Every member is checked before anything is written:
    one unsafe member anywhere refuses the whole archive. Files are copied out one by one
    (never TarFile.extract/extractall) into a .part folder that then takes dest's place;
    the archive's own modes are ignored (files 0o644, folders 0o755)."""
    sub = [p for p in subdir.strip("/").split("/") if p and p != "."]
    if not sub or ".." in sub:
        raise AddonError(f"{subdir!r} isn't a folder inside the archive.", 400, "unsafe")
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
            raise AddonError(f"the archive has no {subdir}", 502, "no_subdir")
        if files > _MAX_FILES or total > _MAX_EXTRACTED_BYTES:
            raise AddonError(f"{subdir} in the archive is too big to install.", 502, "too_big")

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
        except AddonError:
            _clear(part)
            raise
        except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
            _clear(part)
            raise AddonError(f"Couldn't unpack {subdir}: {exc}", 500, "write") from exc

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


def install(addon: Addon, *, fetch=None) -> Path:
    """Download the pinned release of addon and unpack its subdir into folder(addon).
    fetch(url, sha256) -> bytes stands in for download in tests."""
    if not addon.commit or not addon.sha256:
        raise AddonError(f"This Ixel has no pinned release of {addon.label} yet.", 409, "unpinned")
    data = (fetch or download)(tarball_url(addon), addon.sha256)
    ADDONS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    extract(data, addon.subdir, folder(addon))
    return folder(addon)


def remove(addon: Addon) -> bool:
    """Remove addon's folder (a link to one is unlinked, never followed); True if
    something was there."""
    path = folder(addon)
    for leftover in (path.with_name(path.name + ".part"), path.with_name(path.name + ".old")):
        _clear(leftover)
    if path.is_symlink() or path.is_file():
        path.unlink()
        return True
    if path.is_dir():
        shutil.rmtree(path)
        return True
    return False


# ── Diagram Design in Ask ──
