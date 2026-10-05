"""The terminal's look and feel, apart from the prompt itself (prompt_ui.py): the welcome card, what
completes while you type, and the status line under the prompt.

It's drawn inline, not full screen: your scrollback, selection and copy-paste keep working. Everything
here is plain data in, text out, so it's tested without a terminal.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from rich.cells import cell_len
from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from ixel_mat.commands import COMMANDS
from ixel_mat.sanitize import sanitize_terminal_text
from ixel_mat.theme import C

LOGO_PATH = Path(__file__).parent / "assets" / "ixel-mat-logo.txt"
TAGLINE = "Multi-Agent Terminal"

# What the welcome card suggests: (what to type, what it does)
TIPS = (
    ("a question", "the panel answers; you get its verdict"),
    ("/review --diff", "the panel reviews your uncommitted changes"),
    ("/saver <question>", "cheap models draft, your big model verifies"),
    ("/help", "every command (Tab completes them)"),
)

# The keys at the prompt, for /help
KEYS = (
    ("Enter", "send"),
    ("Ctrl+J  or  \\ then Enter", "a new line instead (Alt+Enter too, where your terminal passes it on)"),
    ("Tab", "complete a /command, an --option, or an agent name"),
    ("Up / Down", "earlier questions from this session; Ctrl+R searches them"),
    ("Ctrl+C", "clear the line · cancel a review that's running · twice on an empty line to exit"),
    ("Ctrl+D", "exit"),
    ("Ctrl+L", "clear the screen"),
)

# --options of /review, /saver and /auto, with what each does. mat.py's flag sets must all appear here
# (tests/test_tui.py checks), so a new flag can't be left out of completion.
FLAG_HELP = {
    "--quick": "answers, then a verdict (no peer review)",
    "--review": "answers, peer review, verdict",
    "--deep": "adds a round where each model revises its answer",
    "--saver": "cheap models draft; your big model only verifies",
    "--auto": "Triage picks quick, review or deep",
    "--mode": "quick, review, deep, saver or auto",
    "--moderator": "AGENT that writes the verdict",
    "--timeout": "SECONDS per model call",
    "--diff": "review your uncommitted changes",
    "--new-files": "with --diff or --base, new files git doesn't track yet too",
    "--staged": "review only what you've staged",
    "--base": "REF: review everything since this branch left REF",
    "--file": "PATH of a file to review (repeat for more; Tab completes it)",
    "--allow-secrets": "send code even if it looks like it holds a key",
}
FLAG_VALUES = {"--mode": ("quick", "review", "deep", "saver", "auto")}
VALUE_FLAGS = frozenset({"--mode", "--moderator", "--timeout", "--base", "--file"})
FLAG_COMMANDS = ("/review", "/saver", "/auto")  # the commands that take them


# ── Welcome card ──────────────────────────────────────────────────────────────

def logo_lines() -> list[Text]:
    """The moon logo, one Text per line. Going through Rich (rather than writing the file's raw color
    codes to the terminal) lets it fall back to the colors a terminal has, and print nothing odd to a pipe."""
    try:
        raw = LOGO_PATH.read_text(encoding="utf-8")
    except OSError:
        return []
    lines = []
    for line in raw.split("\n"):
        text = Text.from_ansi(line)
        text.rstrip()
        lines.append(text)
    while lines and not lines[0].plain.strip():
        lines.pop(0)
    while lines and not lines[-1].plain.strip():
        lines.pop()
    return lines


def tilde(path: Path) -> str:
    """A folder as you'd type it: under your home folder it starts with ~."""
    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text


def _details(rows: Sequence[tuple[str, str]], tips: bool) -> list[RenderableType]:
    """The facts and the suggestions: what sits under the name."""
    out: list[RenderableType] = []
    if rows:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style=C["dim"], no_wrap=True)
        grid.add_column(style=C["moon"], ratio=1)
        for label, value in rows:
            grid.add_row(label, sanitize_terminal_text(value))
        out += [grid, Text("")]
    if tips:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style=C["blue"], no_wrap=True)
        grid.add_column(style=C["dim"], ratio=1)
        for what, does in TIPS:
            grid.add_row(what, does)
        out += [Text("Try", style=f"bold {C['violet']}"), grid]
    return out


def splash(width: int, version: str, rows: Sequence[tuple[str, str]] = (), logo: list[Text] | None = None,
           tips: bool = True) -> RenderableType:
    """The welcome card: the moon on the left, and on the right the name, a few facts (rows) and what to try.
    On a terminal too narrow for both side by side, the text alone."""
    logo = logo_lines() if logo is None else logo
    details = _details(rows, tips)
    logo_width = max((cell_len(line.plain) for line in logo), default=0)
    if not logo or width < logo_width + 3 + 46:
        head = Text.assemble(("✦ ", C["violet"]), ("Ixel MAT", f"bold {C['gold']}"), ("  v" + version, C["dim"]),
                             ("  " + TAGLINE, C["dim"]))
        return Padding(Group(head, Text(""), *details), (1, 0, 1, 2))
    head = [Text.assemble(("Ixel MAT", f"bold {C['gold']}"), ("  v" + version, C["dim"])),
            Text(TAGLINE, style=C["moon"]), Text("")]
    grid = Table.grid(padding=(0, 3))
    grid.add_column(width=logo_width)
    grid.add_column(vertical="middle")
    grid.add_row(Group(*logo), Group(*head, *details))
    return Padding(grid, (1, 0, 1, 1))


def two_columns(rows: Sequence[tuple[str, str]], left: str, right: str, indent: int = 4,
                left_width: int = 0) -> RenderableType:
    """Rows of (what, explanation), the explanation wrapping under itself instead of back to the margin.
    Both are shown as typed: nothing in them is read as Rich markup (usage like "[--quick|--deep]").
    `left_width` lines the first column up with other tables of the same kind."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=left, no_wrap=True, min_width=left_width)
    grid.add_column(style=right, ratio=1)
    for what, explanation in rows:
        grid.add_row(Text(sanitize_terminal_text(what)), Text(sanitize_terminal_text(explanation)))
    return Padding(grid, (0, 0, 0, indent))


# ── Prompt: what completes ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Suggestion:
    text: str       # replaces the word being typed
    back: int       # how many characters back from the cursor that word starts
    meta: str = ""  # a few words on what it is, shown beside it


def _brief(description: str, limit: int = 60) -> str:
    text = description.split(" (")[0]
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def command_names() -> list[tuple[str, str]]:
    """(/command, what it does) for the terminal, as /help lists them."""
    return [(cmd.usage.split()[0], _brief(cmd.description)) for cmd in COMMANDS if cmd.mode in ("mat", "both")]


def _words(text: str) -> list[str]:
    """text split at spaces, except inside quotes (a path with a space): the last word is what's being typed,
    with its opening quote if it hasn't been closed."""
    words, current, quote = [], "", ""
    for ch in text:
        if quote:
            current += ch
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote, current = ch, current + ch
        elif ch == " ":
            words.append(current)
            current = ""
        else:
            current += ch
    return words + [current]


PATH_LIMIT = 50  # entries offered; a huge folder shouldn't make every keystroke slow


def path_suggestions(word: str, limit: int = PATH_LIMIT) -> list[Suggestion]:
    """Files and folders that complete the path being typed (relative to the current folder, or ~ for home).
    Folders come first and end in a separator, so you can keep going; hidden ones only once you type the dot.
    A name with a space is offered in quotes. Nothing if the folder can't be read."""
    quote = word[:1] if word[:1] in ("'", '"') else ""
    typed = word[len(quote):]
    cut = max(typed.rfind("/"), typed.rfind(os.sep))
    folder, prefix = typed[:cut + 1], typed[cut + 1:]
    sep = "/" if "/" in typed or os.sep == "/" else os.sep
    try:
        entries = sorted(os.scandir(os.path.expanduser(folder) or "."),
                         key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError:
        return []
    out = []
    for entry in entries:
        name = entry.name
        if not name.startswith(prefix) or (name.startswith(".") and not prefix.startswith(".")):
            continue
        is_dir = entry.is_dir()
        shown = folder + name + (sep if is_dir else "")
        if " " in name or quote:
            q = quote or '"'
            shown = q + shown + ("" if is_dir else q)  # a folder's quote stays open: you're not done
        out.append(Suggestion(shown, -len(word), "folder" if is_dir else "file"))
        if len(out) >= limit:
            break
    return out


def complete(text: str, agents: Sequence[str] = (), explicit: bool = False) -> list[Suggestion]:
    """What to offer for the line typed so far (up to the cursor): a /command, then its --options, then
    a value for one that takes one (--moderator an agent, --mode a mode). Nothing for a plain question, so a
    path like /tmp/x in the middle of one doesn't open a menu. `explicit`: Tab was pressed, so list the
    options even before a `--` is typed."""
    if not text.startswith("/") or "\n" in text:
        return []
    head, space, rest = text.partition(" ")
    if not space:
        return [Suggestion(name, -len(head), meta) for name, meta in command_names() if name.startswith(head.lower())]
    if head.lower() not in FLAG_COMMANDS:
        return []
    tokens = _words(rest)
    word, before = tokens[-1], [t for t in tokens[:-1] if t]
    expecting = None  # the flag whose value comes next
    for token in before:
        if expecting:
            expecting = None
        elif token in VALUE_FLAGS:
            expecting = token
        elif not token.startswith("--"):
            return []  # the question has started: no more options
    if expecting == "--file":
        return path_suggestions(word)
    if expecting:
        choices = FLAG_VALUES.get(expecting) or (tuple(agents) if expecting == "--moderator" else ())
        return [Suggestion(c, -len(word)) for c in choices if c.startswith(word)]
    if word.startswith("--") or (explicit and not word):
        return [Suggestion(flag + " ", -len(word), meta) for flag, meta in FLAG_HELP.items() if flag.startswith(word)]
    return []


# ── Prompt: the rule above it ─────────────────────────────────────────────────

@dataclass
class Status:
    mode: str            # what a plain question runs: quick, review, auto, compare…
    agents: int          # connected
    follow_ups: int = 0  # earlier questions the next one will carry along


HINTS = ("/ commands", "tab completes", "ctrl+j new line", "ctrl+c exit")


def _clip(parts: list[tuple[str, str]], width: int) -> list[tuple[str, str]]:
    out, used = [], 0
    for style, text in parts:
        text = text[:max(width - used, 0)]
        if text:
            out.append((style, text))
        used += len(text)
    return out


def rule_fragments(status: Status, width: int, exit_armed: bool = False, done: bool = False) -> list[tuple[str, str]]:
    """The rule across the top of the prompt, as (style class, text) pairs: the mode and panel on its left, the
    keys on its right (fewer of them on a narrow terminal). Once the line is sent (`done`) the keys go,
    so what stays in the terminal's history is a plain divider that says how the question was asked."""
    if exit_armed:
        left = [("class:rule", "── "), ("class:rule.warn", "Press ctrl+c again to exit ")]
    else:
        left = [("class:rule", "── "), ("class:rule.mark", "✦ "), ("class:rule.mode", status.mode),
                ("class:rule.text", f" · {status.agents} {'agent' if status.agents == 1 else 'agents'}")]
        if status.follow_ups:
            left.append(("class:rule.text", f" · follow-up ×{status.follow_ups}"))
        left.append(("class:rule", " "))
    left = _clip(left, width)  # never longer than the terminal is wide: that would wrap and break the layout
    used = sum(len(text) for _, text in left)
    hints = [] if exit_armed or done else list(HINTS)
    while hints and used + len(" · ".join(hints)) + 5 > width:
        hints.pop()
    right = f" {' · '.join(hints)} ──" if hints else ""
    return left + [("class:rule", "─" * max(width - used - len(right), 0))] + ([("class:rule.text", right)] if right else [])
