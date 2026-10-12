"""
Diagram Design in Ask.

Ask with the Diagram toggle on: the skill's own files go to the model as fenced material
(instructions to read, not to follow), its HTML is saved in ~/.ixel/diagrams, the add-on's
scripts/self_check.py checks it in a separate isolated Python, and one failed check earns
one retry with the problems listed.
"""
from __future__ import annotations

import asyncio
import inspect
import re
import secrets
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import ixel_mat.ask
from ixel_mat.addons import paths
from ixel_mat.addons.installer import ADDONS, Addon, AddonError, folder, installed
from ixel_mat.agents.launch import NO_WINDOW_FLAGS, find_on_path
from ixel_mat.ask import AskResult
from ixel_mat.config.secrets import child_env
from ixel_mat.material import MAX_MATERIAL_CHARS, Material

__all__ = ["is_on", "status", "DIAGRAM_TYPES", "TYPE_WORDS", "available_types", "pick_type", "diagram_material",
           "diagram_question", "extract_html", "slug", "save_diagram", "CheckResult", "self_check", "DiagramResult",
           "make_diagram", "show_in_folder", "DiagramStore"]


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
    where = folder or paths.DIAGRAMS_DIR
    where.mkdir(parents=True, exist_ok=True, mode=0o700)
    stem = f"{(now or datetime.now()):%Y-%m-%d}-{slug(question)}"
    number = 1
    while True:
        path = where / (f"{stem}.html" if number == 1 else f"{stem}-{number}.html")
        try:
            with open(path, "x", encoding="utf-8", newline="") as out:  # the model's bytes, untranslated
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
    path.write_text(html2, encoding="utf-8", newline="")
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
