"""
Add-ons: other people's skills that Ixel can fetch and use, off until you switch them on.

There is one so far, Diagram Design (Cathryn Lavery's MIT-licensed skill for making a
diagram as one HTML file, in your brand). Switching it on downloads the GitHub tarball of
one pinned commit and unpacks only skills/diagram-design/ into ~/.ixel/addons/diagram-design.
Nothing else in the archive is ever written, and an archive with anything odd in it (an
absolute path, '..', a link, a device, an oversized file) is refused whole, before a single
file is written. The extracted files are checked against the pinned tree hash this Ixel
ships with (the tarball's own bytes aren't stable enough to pin) before they replace what
was there. Switching it off removes the folder. Diagrams it makes are saved in ~/.ixel/diagrams.
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
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

ADDONS_DIR = Path.home() / ".ixel" / "addons"
DIAGRAMS_DIR = Path.home() / ".ixel" / "diagrams"
MAX_TARBALL_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
DOWNLOAD_TIMEOUT = 120
# Filled in by a person (the recipe is in the plan): while either is empty install() refuses.
# The tree hash is tree_hash() of the extracted subdir, not of the tarball bytes
PINNED_COMMIT = "7a2e22170ee838e859d8142ba825d83f8b6375e6"
PINNED_TREE_SHA256 = "e4adecc695f6367ce202995133e6e1fe5d18b6e4e142a06bd30926d72bfbb4a1"
PINNED_TREE_FILES = 279

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
    tree_sha256: str
    files: int
    subdir: str
    setting: str


DIAGRAM_DESIGN = Addon("diagram-design", "Diagram Design", "diagrams as one HTML file, in your brand",
                       "Cathryn Lavery", "MIT", "cathrynlavery/diagram-design", PINNED_COMMIT, PINNED_TREE_SHA256,
                       PINNED_TREE_FILES, "skills/diagram-design", "diagram_design")
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
    return how many files were written. Every member is checked before anything is written:
    one unsafe member anywhere refuses the whole archive. Files are copied out one by one
    (never TarFile.extract/extractall) into a .part folder that then takes dest's place;
    the archive's own modes are ignored (files 0o644, folders 0o755). With expect, a
    (file count, tree_hash hex) pair, the .part folder must match it or it is removed and
    dest is left as it was."""
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
            if expect is not None:
                count, digest = tree_hash(part)
                if count != expect[0] or digest != expect[1].strip().lower():
                    raise AddonError("The download isn't the release this Ixel was made for (its files don't "
                                     "match the pinned ones), so nothing was installed.", 409, "tree_mismatch")
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
    The unpacked files must match addon's pinned tree hash and file count, or nothing is
    installed. fetch(url) -> bytes stands in for download in tests."""
    if not addon.commit or not addon.tree_sha256:
        raise AddonError(f"This Ixel has no pinned release of {addon.label} yet.", 409, "unpinned")
    data = (fetch or download)(tarball_url(addon))
    ADDONS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    extract(data, addon.subdir, folder(addon), expect=(addon.files, addon.tree_sha256))
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
# Ask with the Diagram toggle on: the skill's own files go to the model as fenced material
# (instructions to read, not to follow), its HTML is saved in ~/.ixel/diagrams, the add-on's
# scripts/self_check.py checks it in a separate isolated Python, and one failed check earns
# one retry with the problems listed.

import asyncio  # noqa: E402
import inspect  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from collections.abc import Iterable  # noqa: E402
from datetime import datetime  # noqa: E402
from dataclasses import field  # noqa: E402

import ixel_mat.ask  # noqa: E402
from ixel_mat.agents.launch import NO_WINDOW_FLAGS, find_on_path  # noqa: E402
from ixel_mat.ask import AskResult  # noqa: E402
from ixel_mat.config.secrets import child_env  # noqa: E402
from ixel_mat.material import MAX_MATERIAL_CHARS, Material  # noqa: E402


def is_on(config: dict, addon: Addon) -> bool:
    chosen = config.get("addons") if isinstance(config, dict) else None
    return isinstance(chosen, dict) and chosen.get(addon.setting) is True and installed(addon)


def status(config: dict) -> list[dict]:
    """What the Settings page shows for each add-on."""
    rows = []
    for addon in ADDONS.values():
        here = installed(addon)
        rows.append({"name": addon.name, "label": addon.label, "about": addon.about, "author": addon.author,
                     "license": addon.license, "on": is_on(config, addon), "installed": here,
                     "pinned": bool(addon.commit and addon.tree_sha256), "version": addon.commit[:12] or "",
                     "path": str(folder(addon)) if here else ""})
    return rows


DIAGRAM_TYPES = ("architecture", "flowchart", "sequence", "timeline", "quadrant", "comparison", "hierarchy",
                 "cycle", "funnel", "mindmap")
TYPE_WORDS: dict[str, tuple[str, ...]] = {
    "architecture": ("architecture", "system", "components", "services", "infrastructure"),
    "flowchart": ("flow", "flowchart", "steps", "process", "decision"),
    "sequence": ("sequence", "interaction", "messages", "request", "handshake"),
    "timeline": ("timeline", "roadmap", "history", "milestones", "over time"),
    "quadrant": ("quadrant", "2x2", "matrix", "versus", "axes"),
    "comparison": ("compare", "comparison", "vs", "pros and cons"),
    "hierarchy": ("hierarchy", "org chart", "tree", "taxonomy"),
    "cycle": ("cycle", "loop", "lifecycle"),
    "funnel": ("funnel", "pipeline stages", "conversion"),
    "mindmap": ("mind map", "mindmap", "brainstorm"),
}
_TYPE_FILE = re.compile(r"^type-(.+)\.md$")
_CUT = "\n[... cut to fit]"


def available_types(folder: Path) -> list[str]:
    """The diagram types the installed skill has a references/type-<t>.md for: the known
    ones in DIAGRAM_TYPES order, then any others alphabetically."""
    refs = folder / "references"
    if not refs.is_dir():
        return []
    found = {m.group(1) for p in refs.iterdir() if p.is_file() and (m := _TYPE_FILE.match(p.name))}
    return [t for t in DIAGRAM_TYPES if t in found] + sorted(found - set(DIAGRAM_TYPES))


def _hits(text: str, word: str) -> int:
    return len(re.findall(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", text))


def pick_type(question: str, available: Iterable[str] | None = None) -> str:
    """The diagram type the question most sounds like (keyword hits; ties go to the earlier
    type in DIAGRAM_TYPES), among available when that's given; architecture when nothing hits."""
    offered = list(available or ())
    choices = [t for t in DIAGRAM_TYPES if t in offered] if offered else list(DIAGRAM_TYPES)
    text = question.lower()
    best, best_hits = "", 0
    for kind in choices:
        hits = sum(_hits(text, word) for word in TYPE_WORDS.get(kind, ()))
        if hits > best_hits:
            best, best_hits = kind, hits
    if best:
        return best
    if not offered or "architecture" in offered:
        return "architecture"
    return offered[0]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _join(sections: list[list[str]]) -> str:
    return "\n\n".join(f"=== {name} ===\n{body}" for name, body in sections)


def _fit(sections: list[list[str]], order: list[int], limit: int) -> str:
    """The sections joined, trimming the bodies at the indexes in order (first one first)
    until the whole fits in limit."""
    for i in order:
        over = len(_join(sections)) - limit
        if over <= 0:
            break
        body = sections[i][1]
        shorter = body[:max(0, len(body) - over - len(_CUT))] + _CUT
        if len(shorter) < len(body):
            sections[i][1] = shorter
    # Only a SKILL.md longer than the whole limit gets here still too long
    return _join(sections)[:limit]


def diagram_material(folder: Path, kind: str) -> Material:
    """The skill's instructions, style guide, the type's reference and its example, as
    material for the model."""
    skill = folder / "SKILL.md"
    if not skill.is_file():
        raise AddonError("Diagram Design isn't installed", 409, "addon_off")
    sections = [["SKILL.md", _read(skill)]]
    for rel in ("references/style-guide.md", f"references/type-{kind}.md", f"assets/example-{kind}.html"):
        path = folder.joinpath(*rel.split("/"))
        if path.is_file():
            sections.append([rel, _read(path)])
    names = [name for name, _ in sections]
    order = [names.index(rel) for rel in (f"assets/example-{kind}.html", f"references/type-{kind}.md",
                                          "references/style-guide.md") if rel in names]
    return Material(f"the Diagram Design skill (type: {kind})", _fit(sections, order, MAX_MATERIAL_CHARS))


def diagram_question(question: str, kind: str, problems: list[str] | None = None) -> str:
    lines = [f"Make a {kind} diagram for this request, following the Diagram Design skill's instructions "
             f"and style guide in the material:", "", question.strip(), "",
             "Answer with ONE complete HTML document inside a single ```html block, and nothing else."]
    if problems:
        lines += ["", "The last attempt failed these checks; fix them:"]
        lines += [f"- {problem}" for problem in problems]
    return "\n".join(lines)


_HTML_BLOCK = re.compile(r"```html\b(.*?)```", re.IGNORECASE | re.DOTALL)
_HTML_START = re.compile(r"(<!doctype\s+html|<html)", re.IGNORECASE)


def extract_html(reply: str) -> str | None:
    """The body of the reply's first ```html block, or the whole reply when it's a bare HTML
    document; None when it's neither."""
    block = _HTML_BLOCK.search(reply or "")
    if block and block.group(1).strip():
        return block.group(1).strip()
    bare = (reply or "").strip()
    if _HTML_START.match(bare):
        return bare
    return None


def slug(question: str, limit: int = 40) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")
    return words[:limit].strip("-") or "diagram"


def save_diagram(html: str, question: str, *, folder: Path | None = None, now: datetime | None = None) -> Path:
    """Save html as <date>-<slug>.html in folder (~/.ixel/diagrams), adding -2, -3... when
    the name is taken; never overwrites."""
    where = folder or DIAGRAMS_DIR
    where.mkdir(parents=True, exist_ok=True, mode=0o700)
    stem = f"{(now or datetime.now()):%Y-%m-%d}-{slug(question)}"
    number = 1
    while True:
        path = where / (f"{stem}.html" if number == 1 else f"{stem}-{number}.html")
        try:
            with open(path, "x", encoding="utf-8") as out:
                out.write(html)
            return path
        except FileExistsError:
            number += 1


@dataclass
class CheckResult:
    ok: bool
    messages: list[str]
    ran: bool

    def to_dict(self) -> dict:
        return {"ok": self.ok, "messages": list(self.messages), "ran": self.ran}


def self_check(folder: Path, file: Path, timeout: float = 60) -> CheckResult:
    """Run the add-on's scripts/self_check.py on file, in its own isolated Python (-I: no
    environment variables, user site or script folder on its path); never imported here."""
    script = folder / "scripts" / "self_check.py"
    if not script.is_file():
        return CheckResult(True, ["The add-on has no self_check.py, so the diagram wasn't checked."], False)
    try:
        done = subprocess.run([sys.executable, "-I", str(script), str(file)], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", cwd=str(file.parent), timeout=timeout,
                              stdin=subprocess.DEVNULL, env=child_env(nested=False),
                              creationflags=NO_WINDOW_FLAGS)
    except subprocess.TimeoutExpired:
        return CheckResult(False, [f"The check took more than {timeout:g} seconds."], True)
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult(False, [f"The check couldn't start: {exc}"], False)
    lines = [line.strip()[:500] for line in f"{done.stdout or ''}\n{done.stderr or ''}".splitlines()]
    return CheckResult(done.returncode == 0, [line for line in lines if line][:40], True)


@dataclass
class DiagramResult:
    path: Path
    kind: str
    check: CheckResult
    retried: bool
    calls: list[AskResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"file": str(self.path), "name": self.path.name, "kind": self.kind, "check": self.check.to_dict(),
                "retried": self.retried,
                "calls": [{k: v for k, v in call.to_dict().items() if k != "answer"} for call in self.calls]}


async def _emit(on_event, kind: str, data: dict) -> None:
    if on_event is None:
        return
    out = on_event(kind, data)
    if inspect.isawaitable(out):
        await out


async def make_diagram(cfg, question: str, folder: Path, *, ask=ixel_mat.ask.ask, on_event=None, timeout=None,
                       diagrams_dir: Path | None = None) -> DiagramResult:
    """Ask cfg for a diagram with the skill as material, save it, check it, and, only when
    the check ran and failed, ask once more with the problems and the first attempt."""
    kind = pick_type(question, available_types(folder))
    await _emit(on_event, "start", {"kind": kind, "agent": cfg.name, "label": cfg.label})
    material = diagram_material(folder, kind)
    first = await ask(cfg, diagram_question(question, kind), material, timeout=timeout)
    calls = [first]
    await _emit(on_event, "model", {"attempt": 1, "ms": first.ms, "usage": first.usage, "model": first.model})
    html = extract_html(first.answer)
    if html is None:
        raise AddonError(f"{cfg.label} didn't answer with an HTML document.", 502, "no_html")
    path = save_diagram(html, question, folder=diagrams_dir)
    check = await asyncio.to_thread(self_check, folder, path)
    await _emit(on_event, "check", {"attempt": 1, **check.to_dict()})
    if not (check.ran and not check.ok):
        return DiagramResult(path, kind, check, False, calls)

    await _emit(on_event, "retry", {})
    head = material.text + "\n\n=== your previous attempt ===\n"
    again = Material(material.title, (head + html)[:MAX_MATERIAL_CHARS])
    second = await ask(cfg, diagram_question(question, kind, check.messages), again, timeout=timeout)
    calls.append(second)
    await _emit(on_event, "model", {"attempt": 2, "ms": second.ms, "usage": second.usage, "model": second.model})
    html2 = extract_html(second.answer)
    if html2 is None:
        check = CheckResult(check.ok, check.messages + ["The retry didn't answer with an HTML document."], check.ran)
        return DiagramResult(path, kind, check, True, calls)
    path.write_text(html2, encoding="utf-8")
    check = await asyncio.to_thread(self_check, folder, path)
    await _emit(on_event, "check", {"attempt": 2, **check.to_dict()})
    return DiagramResult(path, kind, check, True, calls)


def show_in_folder(path: Path) -> bool:
    """Show path in the file manager (selected where the system can); True when that started."""
    if sys.platform == "darwin":
        argv = ["/usr/bin/open", "-R", str(path)]
    elif sys.platform == "win32":
        explorer = find_on_path("explorer")
        if not explorer:
            return False
        argv = [explorer, f"/select,{path}"]
    else:
        opener = find_on_path("xdg-open")
        if not opener:
            return False
        argv = [opener, str(path.parent)]
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              env=child_env(nested=False), timeout=15, creationflags=NO_WINDOW_FLAGS)
    except (OSError, subprocess.SubprocessError):
        return False
    # explorer /select returns 1 even when it worked
    return sys.platform == "win32" or done.returncode == 0


class DiagramStore:
    """The diagrams this app run has made, by an unguessable id the page can ask for."""

    def __init__(self) -> None:
        self._paths: dict[str, Path] = {}

    def add(self, path: Path) -> str:
        key = secrets.token_hex(16)
        self._paths[key] = path
        return key

    def get(self, key: str) -> Path | None:
        return self._paths.get(key)

    def clear(self) -> None:
        self._paths.clear()
