"""
The add-on card format in docs/addon-cards.md: reading and checking a card, filling in a
command's placeholders, and working out a card's on/off and approval state.

A card with any problem is refused whole, with a CardError that names the key it is about
(`source.commit`, `permissions.commands[0][2]`), so a typo never silently loosens anything.
Commands only ever run as argv lists, never through a shell, so ';', '|', '$()' and the
like are plain characters here, and nothing is quoted or escaped.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import os
import re
import stat
import unicodedata
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from ixel_mat.addons import installer, paths

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib  # type: ignore

__all__ = ["KINDS", "FILE_ACCESS", "WHERE", "OUTPUTS", "PLACEHOLDERS", "MAX_CARD_BYTES", "CardError", "Source",
           "Permissions", "Use", "Card", "validate_card_text", "check_command", "fill_command", "load_card",
           "card_state"]

KINDS = ("skill", "mcp", "cli")
FILE_ACCESS = ("none", "output-folder", "chosen-folder")
WHERE = ("ask", "board", "agent")
OUTPUTS = ("text", "html", "file", "image")
PLACEHOLDERS = ("addon", "input", "output")
MAX_CARD_BYTES = 64 * 1024
MAX_COMMANDS = 8
MAX_ARGS = 32
MAX_ARG_CHARS = 500

# The keys a card may have, in each table; anything else is refused
_TOP = ("id", "name", "summary", "by", "license", "homepage", "kind", "terms", "source", "permissions", "use")
_TABLES = {"source": ("repo", "commit", "subdir", "tree_sha256", "files"),
           "permissions": ("files", "network", "commands"),
           "use": ("where", "trigger", "output")}
_LIMITS = {"name": 60, "summary": 200, "by": 100, "license": 64, "homepage": 300, "terms": 500,
           "source.repo": 200, "source.subdir": 200}

_ID = re.compile(r"[a-z0-9-]{2,40}")
_LICENSE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]*(?: (?:AND|OR|WITH) [A-Za-z0-9][A-Za-z0-9.+-]*)*")
_REPO = re.compile(r"https://github[.]com/[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_TREE = re.compile(r"[0-9a-f]{64}")
_TRIGGER = re.compile(r"/[a-z0-9][a-z0-9-]{0,29}")
_PROGRAM = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._+-]*")
_BRACED = re.compile(r"\{([^{}]*)\}")
_FILL = re.compile(r"\{(addon|input|output)\}")
_ADDON_PREFIX = "{addon}/"


class CardError(ValueError):
    """Why a card was refused, said for a person, naming the key it is about."""


class _Section:
    """Read-only item access and to_dict() for a card's sections, so card.source.repo and
    card.source['repo'] are the same."""

    def __getitem__(self, key):
        if isinstance(key, str) and key in self.keys():
            return getattr(self, key)
        raise KeyError(key)

    def keys(self) -> list[str]:
        return [field.name for field in dataclasses.fields(self)]

    def to_dict(self) -> dict:
        return {key: copy.deepcopy(getattr(self, key)) for key in self.keys()}


@dataclass(frozen=True)
class Source(_Section):
    repo: str
    commit: str
    subdir: str
    tree_sha256: str
    files: int


@dataclass(frozen=True)
class Permissions(_Section):
    files: str
    network: bool
    commands: list[list[str]]  # placeholders unfilled


@dataclass(frozen=True)
class Use(_Section):
    where: str
    trigger: str  # '' when the card has none
    output: str


@dataclass(frozen=True)
class Card:
    id: str
    name: str
    summary: str
    by: str
    license: str
    homepage: str
    kind: str
    terms: str
    builtin: bool
    card_path: Path
    dir: Path
    files_dir: Path
    source: Source
    permissions: Permissions
    use: Use
    card_sha256: str
    on: bool = False
    approved: bool = False
    changed_since_approval: bool = False
    installed: bool = False
    enabled: bool = False

    def to_dict(self) -> dict:
        """Every field, ready for JSON: Paths as text, sections as dicts."""
        out = {}
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if isinstance(value, Path):
                value = str(value)
            elif isinstance(value, _Section):
                value = value.to_dict()
            out[field.name] = value
        return out


def _unknown(path: str) -> CardError:
    return CardError(f"Unknown key `{path}` in the card. The format has no such key, so the card was refused.")


def _check_keys(data: dict) -> None:
    """Refuse the first key the format doesn't have: the top level's keys first, then each
    table's, in the order the card has them."""
    for key in data:
        if key not in _TOP:
            raise _unknown(key)
    for key, table in data.items():
        if key in _TABLES and isinstance(table, dict):
            for inner in table:
                if inner not in _TABLES[key]:
                    raise _unknown(f"{key}.{inner}")


def _table(data: dict, key: str) -> dict:
    if key not in data:
        raise CardError(f"The card has no `{key}`.")
    if not isinstance(data[key], dict):
        raise CardError(f"`{key}` must be a table.")
    return data[key]


def _text(table: dict, key: str, path: str, *, required: bool = True, empty: bool = False) -> str:
    """table[key] checked as one line of text: no control characters, no spaces at either
    end, not empty unless empty is allowed, and within its length limit. '' when it isn't
    there and isn't required."""
    if key not in table:
        if required:
            raise CardError(f"The card has no `{path}`.")
        return ""
    value = table[key]
    if not isinstance(value, str):
        raise CardError(f"`{path}` must be text.")
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        raise CardError(f"`{path}` has a control character (like a newline or tab) in it.")
    if value != value.strip():
        raise CardError(f"`{path}` starts or ends with a space.")
    if not value and not empty:
        raise CardError(f"`{path}` is empty.")
    limit = _LIMITS.get(path)
    if limit is not None and len(value) > limit:
        raise CardError(f"`{path}` is longer than {limit} characters.")
    return value


def _one_of(value: str, path: str, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise CardError(f"`{path}` must be one of: {', '.join(choices)}.")
    return value


def _homepage(value: str) -> str:
    try:
        parts = urllib.parse.urlsplit(value)
        good = (parts.scheme == "https" and bool(parts.hostname) and parts.username is None
                and parts.password is None and not any(ch.isspace() for ch in value))
    except ValueError:
        good = False
    if not good:
        raise CardError("`homepage` must be an https:// address.")
    return value


def _repo(value: str) -> str:
    name = value.rsplit("/", 1)[-1]
    if not _REPO.fullmatch(value) or name in (".", "..") or name.lower().endswith(".git"):
        raise CardError("`source.repo` must be https://github.com/<owner>/<repo>.")
    return value


def _subdir(value: str) -> str:
    if value and (value.startswith("/") or "\\" in value or ":" in value
                  or any(part in ("", ".", "..") for part in value.split("/"))):
        raise CardError("`source.subdir` must be a folder inside the repository, like skills/name, or empty for "
                        "all of it.")
    return value


def _clean_relative(rel: str) -> bool:
    """True for a/b/c: parts that aren't empty, '.' or '..', with no backslash, colon or brace."""
    if not rel or any(ch in rel for ch in "\\:{}"):
        return False
    return all(part not in ("", ".", "..") for part in rel.split("/"))


def check_command(argv, where: str = "argv") -> list[str]:
    """One command template checked: a program and its arguments, as a list of strings,
    with only {addon}, {input} and {output} as placeholders. The first item is a bare
    program name or a file under {addon}/. Returns a new list."""
    if not isinstance(argv, list) or not 1 <= len(argv) <= MAX_ARGS or not all(isinstance(a, str) for a in argv):
        raise CardError(f"`{where}` must be a list of strings: a program and its arguments (at most {MAX_ARGS}).")
    for j, item in enumerate(argv):
        at = f"{where}[{j}]"
        if not item:
            raise CardError(f"`{at}` is empty.")
        if len(item) > MAX_ARG_CHARS:
            raise CardError(f"`{at}` is longer than {MAX_ARG_CHARS} characters.")
        if any(unicodedata.category(ch) == "Cc" for ch in item):
            raise CardError(f"`{at}` has a NUL or another control character in it.")
        for found in _BRACED.finditer(item):
            if found.group(1) not in PLACEHOLDERS:
                raise CardError(f"`{at}`: `{found.group(0)}` isn't a placeholder Ixel fills (only {{addon}}, "
                                "{input} and {output}).")
        if any(ch in _FILL.sub("", item) for ch in "{}"):
            raise CardError(f"`{at}` has a {{ or }} that isn't part of {{addon}}, {{input}} or {{output}}.")
        if item.startswith(_ADDON_PREFIX) and not _clean_relative(item[len(_ADDON_PREFIX):]):
            raise CardError(f"`{at}` must go on from {{addon}}/ with a path inside the add-on's files, like "
                            "{addon}/scripts/run.py.")
    if not (_PROGRAM.fullmatch(argv[0]) or argv[0].startswith(_ADDON_PREFIX)):
        raise CardError(f"`{where}[0]` must be a bare program name (like python3) or start with {{addon}}/.")
    return list(argv)


def fill_command(card: Card, argv: list[str], *, input: str | None = None,
                 output: str | None = None) -> list[str]:
    """argv with its placeholders filled: {addon} is the card's installed files, {input}
    and {output} the values given. Every item is filled in one pass, so a value that
    happens to hold '{output}' is never filled again. Returns a new list and runs nothing."""
    argv = check_command(argv, "argv")
    for name, value in (("input", input), ("output", output)):
        if value is not None and (not isinstance(value, str) or "\x00" in value):
            raise CardError(f"`{name}` must be text with no NUL in it.")
    values = {"addon": str(card.files_dir), "input": input, "output": output}
    filled = []
    for j, item in enumerate(argv):
        if item.startswith(_ADDON_PREFIX):
            # check_command made sure the rest is a clean relative path with no placeholders
            filled.append(str(card.files_dir.joinpath(*item[len(_ADDON_PREFIX):].split("/"))))
            continue

        def one(found: re.Match, j: int = j) -> str:
            name = found.group(1)
            value = values[name]
            if value is None:
                raise CardError(f"`argv[{j}]` uses {{{name}}}, but no {name} was given.")
            return value

        filled.append(_FILL.sub(one, item))
    return filled


def validate_card_text(text: str) -> Card:
    """The card in text, checked against the format, or CardError naming the first problem.
    It reads nothing from disk: the Card is placed as an agent card, at
    ~/.ixel/addons/<id>/addon.toml, with its state all off."""
    if not isinstance(text, str):
        raise CardError("The card must be text.")
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        raise CardError("The card isn't valid UTF-8 text.") from None
    if len(encoded) > MAX_CARD_BYTES:
        raise CardError(f"The card is bigger than {MAX_CARD_BYTES // 1024} KB.")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CardError(f"The card isn't valid TOML: {exc}") from None
    _check_keys(data)

    card_id = _text(data, "id", "id")
    if not _ID.fullmatch(card_id):
        raise CardError("`id` must be 2 to 40 characters of a-z, 0-9 and -.")
    name = _text(data, "name", "name")
    summary = _text(data, "summary", "summary")
    by = _text(data, "by", "by")
    license = _text(data, "license", "license")
    if not _LICENSE.fullmatch(license):
        raise CardError("`license` must be an SPDX licence id or expression, like MIT or Apache-2.0.")
    homepage = _homepage(_text(data, "homepage", "homepage"))
    kind = _one_of(_text(data, "kind", "kind"), "kind", KINDS)
    terms = _text(data, "terms", "terms", required=False)

    table = _table(data, "source")
    repo = _repo(_text(table, "repo", "source.repo"))
    commit = _text(table, "commit", "source.commit")
    if not _COMMIT.fullmatch(commit):
        raise CardError("`source.commit` must be the full 40-character commit sha, in lower-case hex.")
    subdir = _subdir(_text(table, "subdir", "source.subdir", empty=True))
    tree = _text(table, "tree_sha256", "source.tree_sha256")
    if not _TREE.fullmatch(tree):
        raise CardError("`source.tree_sha256` must be the 64-character sha256 of the files, in lower-case hex.")
    if "files" not in table:
        raise CardError("The card has no `source.files`.")
    files = table["files"]
    if isinstance(files, bool) or not isinstance(files, int):
        raise CardError("`source.files` must be a whole number.")
    if not 1 <= files <= installer._MAX_FILES:
        raise CardError(f"`source.files` must be from 1 to {installer._MAX_FILES}.")
    source = Source(repo, commit, subdir, tree, files)

    table = _table(data, "permissions")
    access = _one_of(_text(table, "files", "permissions.files"), "permissions.files", FILE_ACCESS)
    if "network" not in table:
        raise CardError("The card has no `permissions.network`.")
    if not isinstance(table["network"], bool):
        raise CardError("`permissions.network` must be true or false.")
    if "commands" not in table:
        raise CardError("The card has no `permissions.commands`.")
    if not isinstance(table["commands"], list):
        raise CardError("`permissions.commands` must be a list of commands.")
    if len(table["commands"]) > MAX_COMMANDS:
        raise CardError(f"`permissions.commands` has more than {MAX_COMMANDS} commands.")
    commands = [check_command(argv, f"permissions.commands[{i}]") for i, argv in enumerate(table["commands"])]
    permissions = Permissions(access, table["network"], commands)

    table = _table(data, "use")
    where = _one_of(_text(table, "where", "use.where"), "use.where", WHERE)
    trigger = _text(table, "trigger", "use.trigger", required=False)
    if "trigger" in table and not _TRIGGER.fullmatch(trigger):
        raise CardError("`use.trigger` must be one word starting with /, like /diagram.")
    output = _one_of(_text(table, "output", "use.output"), "use.output", OUTPUTS)
    use = Use(where, trigger, output)

    folder = paths.ADDONS_DIR / card_id
    return Card(id=card_id, name=name, summary=summary, by=by, license=license, homepage=homepage, kind=kind,
                terms=terms, builtin=False, card_path=folder / "addon.toml", dir=folder, files_dir=folder / "files",
                source=source, permissions=permissions, use=use,
                card_sha256=hashlib.sha256(encoded).hexdigest())


def load_card(path: Path, *, builtin: bool, state: dict | None = None) -> Card:
    """The card at path, checked, with its state from state (the whole state.json mapping).
    A built-in card's file is named <id>.toml; an agent card's folder is named <id>."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise CardError(f"The card couldn't be read: {exc}") from None
    if stat.S_ISLNK(info.st_mode):
        raise CardError("The card is a link, which Ixel doesn't follow.")
    if not stat.S_ISREG(info.st_mode):
        raise CardError("The card isn't a regular file.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as source:
            data = source.read(MAX_CARD_BYTES + 1)
    except OSError as exc:
        # O_NOFOLLOW: a link put there since the check above
        raise CardError(f"The card couldn't be read: {exc}") from None
    if len(data) > MAX_CARD_BYTES:
        raise CardError(f"The card is bigger than {MAX_CARD_BYTES // 1024} KB.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise CardError("The card isn't saved as UTF-8.") from None
    card = validate_card_text(text)
    named = path.stem if builtin else path.parent.name
    if card.id != named:
        raise CardError(f"The card's `id` is {card.id}, but its file is named for {named}.")
    card = dataclasses.replace(card, builtin=builtin, card_path=path, card_sha256=hashlib.sha256(data).hexdigest())
    return dataclasses.replace(card, **card_state(card, (state or {}).get(card.id)))


def card_state(card: Card, entry: dict | None) -> dict:
    """The card's on, approved, changed_since_approval, installed and enabled, from its
    state.json entry. Approval holds only for these exact card bytes and tree hash: any
    change to the card, its permissions for example, needs approving again."""
    if not isinstance(entry, dict):
        entry = {}
    on = entry.get("on") is True
    card_sha = entry.get("approved_card_sha256")
    tree_sha = entry.get("approved_tree_sha256")
    approved = (isinstance(card_sha, str) and card_sha == card.card_sha256
                and isinstance(tree_sha, str) and tree_sha == card.source.tree_sha256)
    here = installer.installed(card)
    return {"on": on, "approved": approved,
            "changed_since_approval": isinstance(card_sha, str) and card_sha != "" and not approved,
            "installed": here, "enabled": on and approved and here}
