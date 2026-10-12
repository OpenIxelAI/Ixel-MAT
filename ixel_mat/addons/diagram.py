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
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import ixel_mat.ask
from ixel_mat.addons import paths, registry
from ixel_mat.addons.installer import AddonError
from ixel_mat.agents.launch import NO_WINDOW_FLAGS, find_on_path
from ixel_mat.ask import AskError, AskResult
from ixel_mat.config.secrets import child_env
from ixel_mat.material import MAX_MATERIAL_CHARS, Material
from ixel_mat.models import provider_for_url
from ixel_mat.usage import CHARS_PER_TOKEN, billing_for, normalize_model, price_for

if TYPE_CHECKING:
    from ixel_mat.addons.cards import Card

__all__ = ["DIAGRAM_ID", "diagram_card", "HAIKU_MODEL", "haiku_model_for", "DiagramChoice", "diagram_choices",
           "default_choice", "find_choice", "LOCAL_DIAGRAM_TIMEOUT", "CONTEXT_TOKENS", "is_local",
           "diagram_timeout", "context_note", "DIAGRAM_TYPES", "TYPE_WORDS", "available_types", "pick_type",
           "diagram_material", "diagram_question", "extract_html", "slug", "save_diagram", "CheckResult",
           "CHECK_ENV", "check_env", "self_check", "DiagramResult", "make_diagram", "show_in_folder", "DiagramStore"]

DIAGRAM_ID = "diagram-design"


def diagram_card() -> Card | None:
    """The Diagram Design card when it's on, approved and installed; None otherwise."""
    card = registry.card_by_id(DIAGRAM_ID)
    return card if card is not None and card.enabled else None


HAIKU_MODEL = "claude-haiku-4-5"  # the name Haiku is priced under (effort.CLAUDE_ALIASES['haiku'])


def _command_name(command: str | None) -> str:
    name = os.path.basename((command or "").replace("\\", "/")).lower()
    return re.sub(r"\.(exe|cmd|bat)$", "", name)


def haiku_model_for(cfg) -> str | None:
    """The model to send so cfg runs Claude Haiku: '' when it already does, 'latest-fast' for the
    Anthropic API, 'haiku' for Claude Code; None when it can't."""
    model = str(cfg.model or "")
    is_api = cfg.type == "http" and provider_for_url(cfg.url or "") == "anthropic"
    is_claude_code = cfg.type == "oneshot" and _command_name(cfg.command) == "claude"
    if normalize_model(model).startswith("claude-haiku-") or (is_claude_code and model.lower() == "haiku") \
            or (is_api and model == "latest-fast"):
        return ""
    if is_api:
        return "latest-fast"  # the API agent resolves this alias to the newest Haiku when it connects
    if is_claude_code and cfg.model_args:
        return "haiku"
    return None


@dataclass(frozen=True)
class DiagramChoice:
    """One model the Diagram picker offers: a panel member, or Haiku through one."""
    id: str
    agent: str
    model: str  # sent in place of the agent's own; '' = its own
    label: str
    detail: str
    price: tuple[float, float] | None
    haiku: bool
    local: bool

    def to_dict(self) -> dict:
        return {"id": self.id, "agent": self.agent, "model": self.model, "label": self.label,
                "detail": self.detail, "price": list(self.price) if self.price is not None else None,
                "haiku": self.haiku, "local": self.local}


def _price(model: str, pricing: dict | None) -> tuple[float, float] | None:
    price = price_for(model, pricing)
    return (price.input, price.output) if price is not None else None


def diagram_choices(configs: Iterable, pricing: dict | None = None) -> list[DiagramChoice]:
    """What the picker offers for the ready panel members, in panel order: each one, then
    Haiku through it when it can run that."""
    choices = []
    for cfg in configs:
        h = haiku_model_for(cfg)
        local = is_local(cfg)
        if h == "":
            choices.append(DiagramChoice(cfg.name, cfg.name, "", cfg.label, cfg.model,
                                         _price(cfg.model, pricing), True, local))
            continue
        choices.append(DiagramChoice(cfg.name, cfg.name, "", cfg.label, cfg.model or "its own default model",
                                     _price(cfg.model, pricing), False, local))
        if h:
            choices.append(DiagramChoice(f"{cfg.name}+haiku", cfg.name, h, f"Haiku, via {cfg.label}", HAIKU_MODEL,
                                         _price(HAIKU_MODEL, pricing), True, local))
    return choices


def default_choice(choices: list[DiagramChoice]) -> DiagramChoice | None:
    """The first Haiku; else the cheapest with a known price (the earlier on a tie); else the first."""
    if not choices:
        return None
    for choice in choices:
        if choice.haiku:
            return choice
    priced = [c for c in choices if c.price is not None]
    if priced:
        return min(priced, key=lambda c: c.price[0] + c.price[1])
    return choices[0]


def find_choice(choices: Iterable[DiagramChoice], agent: str, model: str | None) -> DiagramChoice | None:
    """The choice for agent with model (None or '' = the agent's own)."""
    for choice in choices:
        if choice.agent == agent and choice.model == (model or ""):
            return choice
    return None


# A local model reads about 16k tokens of skill material before it writes a word, which
# takes far longer than the usual 180 s on most computers.
LOCAL_DIAGRAM_TIMEOUT = 600.0
CONTEXT_TOKENS = 16384


def is_local(cfg) -> bool:
    """A model server on this computer or your network, or OpenCode asking one."""
    return billing_for(SimpleNamespace(config=cfg)) == "local"


def diagram_timeout(cfg) -> float | None:
    """At least LOCAL_DIAGRAM_TIMEOUT for a local model; None (cfg's own) otherwise."""
    return max(cfg.call_timeout, LOCAL_DIAGRAM_TIMEOUT) if is_local(cfg) else None


def context_note(cfg, chars: int) -> str:
    """Why a local model may have failed: its context window is too small for the material."""
    k = max(1, round(chars / CHARS_PER_TOKEN / 1000))
    return (f"{cfg.label} runs on your own computer, and the Diagram Design material is about {k}k tokens, "
            f"so it needs a context window of at least 16k tokens ({CONTEXT_TOKENS}). With a smaller one a "
            "local model stalls or loses the instructions. In Ollama, raise the context length to 16384 or "
            "more (OLLAMA_CONTEXT_LENGTH=16384, or num_ctx) and try again.")


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


# All the check gets from the environment: what Python needs to start, and no keys from your shell
CHECK_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")


def check_env() -> dict[str, str]:
    return {name: value for name, value in os.environ.items() if name.upper() in CHECK_ENV}


def self_check(folder: Path, file: Path, timeout: float = 60) -> CheckResult:
    """Run the add-on's scripts/self_check.py on file, in its own isolated Python (-I: no
    PYTHON* variables, user site or script folder on its path) with a bare environment
    (check_env); never imported here. The route checked the files against the card's tree
    hash before the diagram began (check_installed)."""
    script = folder / "scripts" / "self_check.py"
    if not script.is_file():
        return CheckResult(True, ["The add-on has no self_check.py, so the diagram wasn't checked."], False)
    try:
        done = subprocess.run([sys.executable, "-I", str(script), str(file)], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", cwd=str(file.parent), timeout=timeout,
                              stdin=subprocess.DEVNULL, env=check_env(), creationflags=NO_WINDOW_FLAGS)
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


async def _ask(ask, cfg, question: str, material: Material, timeout, note_chars: int) -> AskResult:
    """ask(), with a local model's failure explained by its context window."""
    try:
        return await ask(cfg, question, material, timeout=timeout)
    except AskError as exc:
        if not is_local(cfg):
            raise
        raise AddonError(f"{exc} {context_note(cfg, note_chars)}", 502, "local_model") from exc


async def make_diagram(cfg, question: str, folder: Path, *, ask=ixel_mat.ask.ask, on_event=None, timeout=None,
                       diagrams_dir: Path | None = None) -> DiagramResult:
    """Ask cfg for a diagram with the skill as material, save it, check it, and, only when
    the check ran and failed, ask once more with the problems and the first attempt."""
    timeout = timeout or diagram_timeout(cfg)
    kind = pick_type(question, available_types(folder))
    await _emit(on_event, "start", {"kind": kind, "agent": cfg.name, "label": cfg.label})
    material = diagram_material(folder, kind)
    chars = len(material.text)
    first = await _ask(ask, cfg, diagram_question(question, kind), material, timeout, chars)
    calls = [first]
    await _emit(on_event, "model", {"attempt": 1, "ms": first.ms, "usage": first.usage, "model": first.model})
    html = extract_html(first.answer)
    if html is None:
        message = f"{cfg.label} didn't answer with an HTML document."
        if is_local(cfg):
            message += " " + context_note(cfg, chars)
        raise AddonError(message, 502, "no_html")
    path = save_diagram(html, question, folder=diagrams_dir)
    check = await asyncio.to_thread(self_check, folder, path)
    await _emit(on_event, "check", {"attempt": 1, **check.to_dict()})
    if not (check.ran and not check.ok):
        return DiagramResult(path, kind, check, False, calls)

    await _emit(on_event, "retry", {})
    head = material.text + "\n\n=== your previous attempt ===\n"
    again = Material(material.title, (head + html)[:MAX_MATERIAL_CHARS])
    second = await _ask(ask, cfg, diagram_question(question, kind, check.messages), again, timeout, chars)
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


def _run_quietly(argv: list[str], timeout: float) -> int | None:
    """Run argv with no input or output shown; its exit code, or None when it couldn't start or ran too long."""
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              env=child_env(nested=False), timeout=timeout, creationflags=NO_WINDOW_FLAGS)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.returncode


def _file_uri(path: Path) -> str | None:
    """The percent-encoded file: URI of path; None when it can't have one."""
    try:
        return path.absolute().as_uri()
    except (OSError, ValueError):
        return None


def _gvariant_string(text: str) -> str:
    """text as one single-quoted GVariant string, with backslashes and quotes escaped."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _show_items_argv(uri: str) -> list[str] | None:
    """The D-Bus call that selects uri in the file manager: gdbus if there is one, else dbus-send;
    None when neither can make it."""
    gdbus = find_on_path("gdbus")
    if gdbus:
        return [gdbus, "call", "--session", "--dest", "org.freedesktop.FileManager1",
                "--object-path", "/org/freedesktop/FileManager1",
                "--method", "org.freedesktop.FileManager1.ShowItems", f"[{_gvariant_string(uri)}]", "''"]
    dbus_send = find_on_path("dbus-send")
    # dbus-send's array:string: splits on commas, so a URI with one can't be passed as a single item
    if dbus_send and "," not in uri:
        return [dbus_send, "--session", "--print-reply", "--dest=org.freedesktop.FileManager1",
                "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1.ShowItems",
                f"array:string:{uri}", "string:"]
    return None


def show_in_folder(path: Path) -> bool:
    """Show path in the file manager (selected where the system can); True when that started.
    On Linux the file is selected through the file manager's FileManager1 D-Bus API, else its folder
    is opened with xdg-open."""
    if sys.platform == "darwin":
        return _run_quietly(["/usr/bin/open", "-R", str(path)], 15) == 0
    if sys.platform == "win32":
        explorer = find_on_path("explorer")
        if not explorer:
            return False
        # explorer /select returns 1 even when it worked
        return _run_quietly([explorer, f"/select,{path}"], 15) is not None
    uri = _file_uri(path)
    call = _show_items_argv(uri) if uri else None
    if call is not None and _run_quietly(call, 5) == 0:
        return True
    opener = find_on_path("xdg-open")
    if not opener:
        return False
    return _run_quietly([opener, str(path.parent)], 15) == 0


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
