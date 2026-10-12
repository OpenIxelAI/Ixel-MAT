"""
Every add-on card and its on/off and approval state.

Cards come from two places: the built-in ones, <id>.toml beside this package, and the ones
the agent (or a person) wrote, ~/.ixel/addons/<id>/addon.toml. Whether each is switched on,
and which exact card file the person approved, lives in ~/.ixel/addons/state.json. A card is
enabled only when it is on, its file is byte for byte the one approved, its tree hash is the
one approved, and its files are installed. Only the Settings approval route writes
state.json; the agent can write cards but never switch one on.
"""
from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

from ixel_mat.addons import paths
from ixel_mat.addons.cards import KINDS, Card, CardError, load_card, validate_card_text
from ixel_mat.config.secrets import write_private_file

__all__ = ["read_state", "all_cards", "card_problems", "enabled_cards", "card_by_id", "builtin_ids",
           "write_agent_card", "approve_and_switch_on", "switch_off", "describe_permissions"]

MAX_STATE_BYTES = 1024 * 1024
_ID = re.compile(r"^[a-z0-9-]{2,40}$")
_LINK_PROBLEM = "Its folder or card is a link, which Ixel doesn't follow."


def read_state() -> dict[str, dict]:
    """
    state.json as a mapping of card id to its entry. Anything odd (no file, a link, over
    1 MB, unreadable, not JSON, not an object) reads as no state at all, so every card is off.
    Entries with a key that isn't a card id, or a value that isn't an object, are dropped.
    """
    path = paths.state_path()
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_BYTES:
            return {}
        data = path.read_bytes()
        if len(data) > MAX_STATE_BYTES:
            return {}
        state = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(state, dict):
        return {}
    return {key: value for key, value in state.items()
            if isinstance(key, str) and _ID.match(key) and isinstance(value, dict)}


def _write_state(state: dict) -> None:
    paths.ADDONS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
    write_private_file(paths.state_path(), data)


def approve_and_switch_on(card: Card, *, now: datetime | None = None) -> dict:
    """
    Record that the person approved this card and switch it on. Returns the new entry.

    It records the sha256 of the Card it was given, the one the person saw in the approval
    dialog, and never re-reads the file: a card swapped in after the dialog stays unapproved.
    Only the Settings approval route (settings_api.set_addon) calls this; nothing else
    writes state.json.
    """
    when = now or datetime.now(timezone.utc)
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc)
    entry = {
        "on": True,
        "approved_card_sha256": card.card_sha256,
        "approved_tree_sha256": card.source.tree_sha256,
        "approved_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    state = read_state()
    state[card.id] = entry
    _write_state(state)
    return dict(entry)


def switch_off(card_id: str) -> None:
    """
    Switch a card off, keeping what was approved. A card with no entry is already off, so
    nothing is written. Only the Settings approval route (settings_api.set_addon) calls
    this; nothing else writes state.json.
    """
    state = read_state()
    entry = state.get(card_id)
    if entry is None:
        return
    entry["on"] = False
    _write_state(state)


def _is_link_or_odd(path: Path, want_dir: bool) -> bool:
    """True when path is a link, or isn't a plain folder (want_dir) or a plain file."""
    try:
        mode = path.lstat().st_mode
    except OSError:
        return True
    if stat.S_ISLNK(mode):
        return True
    return not (stat.S_ISDIR(mode) if want_dir else stat.S_ISREG(mode))


def _scan() -> tuple[list[Card], list[dict]]:
    """Every card that loads, and a problem for each one that doesn't."""
    state = read_state()
    problems: list[dict] = []

    def problem(path: Path, error: str) -> None:
        problems.append({"path": str(path), "error": error})

    builtin: list[Card] = []
    reserved = builtin_ids()
    try:
        builtin_files = sorted(paths.BUILTIN_DIR.glob("*.toml"), key=lambda p: p.name)
    except OSError:
        builtin_files = []
    for path in builtin_files:
        if _is_link_or_odd(path, want_dir=False):
            continue
        try:
            builtin.append(load_card(path, builtin=True, state=state))
        except (CardError, OSError) as exc:
            problem(path, str(exc))

    agent: list[Card] = []
    root = paths.ADDONS_DIR
    if not _is_link_or_odd(root, want_dir=True):
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            problem(root, str(exc))
            children = []
        for folder in children:
            card_file = folder / "addon.toml"
            if not os.path.lexists(card_file):
                continue  # not a card: a built-in card's installed files, or leftovers
            if _is_link_or_odd(folder, want_dir=True) or _is_link_or_odd(card_file, want_dir=False):
                problem(card_file, _LINK_PROBLEM)
                continue
            if folder.name in reserved:
                problem(card_file, f"{folder.name} is a built-in add-on's id, so this card was ignored.")
                continue
            try:
                agent.append(load_card(card_file, builtin=False, state=state))
            except (CardError, OSError) as exc:
                problem(card_file, str(exc))

    cards = sorted(builtin, key=lambda c: c.id) + sorted(agent, key=lambda c: c.id)
    return cards, problems


def all_cards() -> list[Card]:
    """Every card, built-in ones first, then the agent's, each sorted by id, with its state."""
    return _scan()[0]


def card_problems() -> list[dict]:
    """The cards that couldn't be loaded, each {'path', 'error'}."""
    return _scan()[1]


def enabled_cards(kind: str | None = None) -> list[Card]:
    """
    The cards that are on, approved as they are now, and installed; only those of one kind
    when kind is given.
    """
    if kind is not None and kind not in KINDS:
        raise ValueError("kind must be one of: " + ", ".join(KINDS))
    return [card for card in all_cards() if card.enabled and (kind is None or card.kind == kind)]


def card_by_id(card_id: str) -> Card | None:
    """The card with this id, or None."""
    for card in all_cards():
        if card.id == card_id:
            return card
    return None


def builtin_ids() -> set[str]:
    """The ids of the built-in cards, from their file names, so a broken one still keeps its id."""
    try:
        return {path.stem for path in paths.BUILTIN_DIR.glob("*.toml")}
    except OSError:
        return set()


def write_agent_card(text: str) -> Card:
    """
    Check a card and write it to ~/.ixel/addons/<id>/addon.toml. This is the agent's only way
    to add or change a card.

    It refuses a built-in card's id, and a folder or card file that is a link. It never writes
    state.json: a new card starts off, and overwriting a card with different bytes changes its
    sha256, so it needs approving again.
    """
    card = validate_card_text(text)
    if card.id in builtin_ids():
        raise CardError(f"{card.id} is a built-in add-on's id. Built-in cards can't be changed; "
                        "give this card another id.")
    folder = paths.ADDONS_DIR / card.id
    target = folder / "addon.toml"
    if os.path.lexists(folder) and _is_link_or_odd(folder, want_dir=True):
        raise CardError(f"{folder} is a link or not a plain folder, so Ixel won't write the card there.")
    if os.path.lexists(target) and _is_link_or_odd(target, want_dir=False):
        raise CardError(f"{target} is a link or not a plain file, so Ixel won't write the card there.")
    paths.ADDONS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.mkdir(exist_ok=True, mode=0o700)
    write_private_file(target, text.encode("utf-8"))
    return load_card(target, builtin=False, state=read_state())


_SHOWN = {"{addon}": "<its files>", "{input}": "<input>", "{output}": "<output file>"}
_WHERE_WORDS = {"ask": "Ask", "board": "the Board", "agent": "the agent"}
_FILES_WORDS = {
    "none": ("It doesn't read or write your files.", False),
    "output-folder": ("It writes only into the output folder Ixel gives it.", False),
    "chosen-folder": ("It can read and change files in a folder you choose.", True),
}


def _show_command(argv: list[str]) -> str:
    shown = []
    for item in argv:
        text = item
        for placeholder, words in _SHOWN.items():
            text = text.replace(placeholder, words)
        shown.append(f'"{text}"' if " " in item else text)
    return " ".join(shown)


def describe_permissions(card: Card) -> list[dict]:
    """
    What the card may do, in plain words, for the approval dialog: a list of
    {'text', 'warn'}, where warn marks what deserves a second look.
    """
    lines: list[tuple[str, bool]] = []
    if not card.builtin:
        lines.append(("Written by the agent, not reviewed. Read what it may do before you approve it.", True))

    source = card.source
    owner_repo = source.repo.removeprefix("https://github.com/").strip("/")
    lines.append((f"Installing downloads {owner_repo} at commit {source.commit[:12]} from GitHub "
                  f"({source.files} {'file' if source.files == 1 else 'files'}, checked against the card's hash) "
                  f"into {card.files_dir}.", False))

    permissions = card.permissions
    lines.append(_FILES_WORDS[permissions.files])
    if permissions.network:
        lines.append(("It may use the internet when it runs.", True))
    else:
        lines.append(("It doesn't use the internet when it runs.", False))
    if not permissions.commands:
        lines.append(("It runs no programs.", False))
    for argv in permissions.commands:
        lines.append((f"Ixel may run this program for it: {_show_command(argv)}", True))

    use = card.use
    where = "It's used in " + _WHERE_WORDS[use.where]
    if use.trigger:
        where += ", when a question starts with " + use.trigger
    lines.append((where + ".", False))

    if card.kind in ("mcp", "cli"):
        lines.append(("Ixel doesn't run this kind of add-on yet: switching it on only installs it.", False))
    return [{"text": text, "warn": warn} for text, warn in lines]
