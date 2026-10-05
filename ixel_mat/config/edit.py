"""
Small, checked edits to config.toml: set or remove a few keys in one table, leaving every other line as
written (comments, order, spacing). After the edit the file is parsed again and compared with what the
edit should have made; anything else (a table written inline, a dotted key, a value over several lines
this can't follow, a file that isn't UTF-8) is refused, so a person's file is never quietly mangled.
They change it by hand then.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Callable

from ixel_mat.config.secrets import write_private_file

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib  # type: ignore

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
REMOVE = None  # a value of None takes the key out (back to Ixel's default)


class EditError(ValueError):
    """The edit can't be made safely; the file is left as it was."""


class Changed(EditError):
    """The file changed since the page read it (`ixel setup`, or an edit by hand)."""


def version(data: bytes) -> str:
    """A short fingerprint of the file's bytes, so a page can tell the file changed since it read it."""
    return hashlib.sha256(data).hexdigest()[:16]


def file_version(path: Path) -> str:
    return version(path.read_bytes())


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EditError("a number must be finite")
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return "[" + ", ".join(_string(v) for v in value) + "]"
    raise EditError(f"can't write {type(value).__name__} values")


def _string(value: str) -> str:
    return json.dumps(value).replace("\x7f", "\\u007f")  # JSON's string escapes are TOML's too, but DEL


def _key_text(key: str) -> str:
    return key if _BARE_KEY.match(key) else _string(key)


def _header(table: tuple[str, ...]) -> re.Pattern:
    parts = r"\s*\.\s*".join(rf'(?:{re.escape(p)}|"{re.escape(p)}")' for p in table)
    return re.compile(rf"^\s*\[\s*{parts}\s*\]\s*(#.*)?$")


def _value_end(lines: list[str], start: int, column: int) -> tuple[int, str]:
    """The last line of the value that starts at lines[start][column:] (one line, unless it's an array
    whose brackets close further down), and the comment after it on that line. Strings are skipped, so
    a bracket or # inside one doesn't count."""
    depth, i = 0, start
    pos = column
    while i < len(lines):
        line = lines[i]
        comment = ""
        while pos < len(line):
            c = line[pos]
            if c in "\"'":
                if line.startswith(c * 3, pos):
                    raise EditError("a value there is a string over several lines")
                end = pos + 1
                while end < len(line) and line[end] != c:
                    end += 2 if c == '"' and line[end] == "\\" else 1
                pos = end + 1
                continue
            if c == "#":
                comment = line[len(line[:pos].rstrip()):].rstrip()  # with the spaces before it
                break
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
            pos += 1
        if depth <= 0:
            return i, comment
        i, pos = i + 1, 0
    raise EditError("an array there never closes")


def set_values(text: str, table: tuple[str, ...], values: dict[str, Any]) -> str:
    """`text` with each key in `values` set in [table] (None removes it), checked by parsing."""
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise EditError(f"the file isn't valid TOML ({exc})") from None
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += newline
    header = _header(table)
    start = next((i for i, line in enumerate(lines) if header.match(line.rstrip("\r\n"))), None)
    if start is None:
        added = {k: v for k, v in values.items() if v is not REMOVE}
        if not added and _table(before, table) is None:
            return text  # taking keys out of a table that isn't there: nothing to do
        if added:
            if lines and lines[-1].strip():
                lines.append(newline)
            lines.append("[" + ".".join(_key_text(p) for p in table) + "]" + newline)
            lines += [f"{_key_text(k)} = {toml_value(v)}{newline}" for k, v in added.items()]
    else:
        for key, value in values.items():
            end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
            pattern = re.compile(rf'^\s*(?:{re.escape(key)}|"{re.escape(key)}")\s*=\s*')
            found = next((i for i in range(start + 1, end) if pattern.match(lines[i])), None)
            new = [] if value is REMOVE else [f"{_key_text(key)} = {toml_value(value)}{newline}"]
            if found is not None:
                last, comment = _value_end(lines, found, pattern.match(lines[found]).end())
                if new and comment:
                    new = [f"{new[0].rstrip()}{comment}{newline}"]  # a note beside the setting stays with it
                lines[found:last + 1] = new
            elif new:
                at = end
                while at > start + 1 and (not lines[at - 1].strip() or lines[at - 1].lstrip().startswith("#")):
                    at -= 1  # after the table's last setting: blank lines and the next table's comment stay below
                lines[at:at] = new
    updated = "".join(lines)

    expected = copy.deepcopy(before)
    target = expected
    for part in table:
        target = target.setdefault(part, {})
        if not isinstance(target, dict):
            raise EditError(f"[{'.'.join(table)}] isn't a table")
    for key, value in values.items():
        if value is REMOVE:
            target.pop(key, None)
        else:
            target[key] = value
    try:
        after = tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise EditError(f"the edit wouldn't have been valid TOML ({exc})") from None
    if after != expected:
        raise EditError(f"[{'.'.join(table)}] is written in a way this can't edit safely")
    return updated


def remove_table(text: str, table: tuple[str, ...]) -> str:
    """`text` without [table] and any table inside it, checked by parsing. A table that isn't there: unchanged."""
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise EditError(f"the file isn't valid TOML ({exc})") from None
    if _table(before, table) is None:
        return text
    lines = text.splitlines(keepends=True)
    header = _header(table)
    start = next((i for i, line in enumerate(lines) if header.match(line.rstrip("\r\n"))), None)
    if start is None:
        raise EditError(f"[{'.'.join(table)}] is written in a way this can't edit safely")
    inside = re.compile(r"^\s*\[\s*" + r"\s*\.\s*".join(rf'(?:{re.escape(p)}|"{re.escape(p)}")' for p in table)
                        + r"\s*\.")
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.lstrip().startswith("[") and not inside.match(line):
            break
        end += 1
    while start + 1 < end and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
        end -= 1  # a comment after the table's settings belongs to what follows (the next table, or the end)
    del lines[start:end]
    updated = "".join(lines)
    expected = copy.deepcopy(before)
    parent = _table(expected, table[:-1]) if len(table) > 1 else expected
    parent.pop(table[-1], None)
    try:
        after = tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise EditError(f"the edit wouldn't have been valid TOML ({exc})") from None
    if after != expected:
        raise EditError(f"[{'.'.join(table)}] is written in a way this can't edit safely")
    return updated


def _table(data: dict, table: tuple[str, ...]) -> Any:
    for part in table:
        data = data.get(part) if isinstance(data, dict) else None
    return data


def edit_file(path: Path, table: tuple[str, ...], values: dict[str, Any], expected_version: str | None = None,
              *, backup: bool = True, check: Callable[[dict], None] | None = None) -> str:
    """
    Make the edit in the file itself (the file a link points to, when it's a link). The old one is kept as
    config.toml.bak unless backup is False. check(new settings) may refuse the result by raising.
    Returns the new text.
    """
    return change_file(path, lambda text: set_values(text, table, values), expected_version, backup=backup,
                       check=check)


def change_file(path: Path, change: Callable[[str], str], expected_version: str | None = None,
                *, backup: bool = True, check: Callable[[dict], None] | None = None) -> str:
    """edit_file for any change(text) -> text made of the checked edits above (several at once, say)."""
    target = path.resolve() if path.is_symlink() else path
    data = target.read_bytes()
    if expected_version is not None and version(data) != expected_version:
        raise Changed("The settings file changed since this page read it.")
    bom = data.startswith(b"\xef\xbb\xbf")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise EditError("the file isn't saved as UTF-8 (open it in an editor and save it as UTF-8)") from None
    updated = change(text)
    if check is not None:
        check(tomllib.loads(updated))
    if updated != text:
        if backup:
            write_private_file(target.with_name(target.name + ".bak"), data)
        write_private_file(target, (b"\xef\xbb\xbf" if bom else b"") + updated.encode("utf-8"))
    return updated
