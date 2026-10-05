from __future__ import annotations

import re
from pathlib import Path

from rich.text import Text

_URL_RE = re.compile(r"https?://[^\s)\]>]+")
_PATH_RE = re.compile(r"(?:(?<=\s)|^)(~/[^\s]+|/[A-Za-z0-9._~\-/]+)")


def _link_target(token: str) -> str:
    if token.startswith('http://') or token.startswith('https://'):
        return token
    path = Path.home() / token[2:] if token.startswith('~/') else Path(token)
    try:
        return path.as_uri()  # file:///C:/... on Windows, percent-encoded
    except ValueError:  # e.g. "/tmp/x" on Windows has no drive, so isn't absolute
        return f"file://{path.as_posix()}"


def hyperlink_text(text: str) -> Text:
    result = Text(text)
    matches: list[tuple[int, int, str]] = []

    for match in _URL_RE.finditer(text):
        matches.append((match.start(), match.end(), _link_target(match.group(0))))
    for match in _PATH_RE.finditer(text):
        token = match.group(1)
        start = match.start(1)
        end = match.end(1)
        matches.append((start, end, _link_target(token)))

    seen = set()
    for start, end, target in sorted(matches, key=lambda item: (item[0], item[1])):
        key = (start, end)
        if key in seen:
            continue
        seen.add(key)
        result.stylize(f"link {target}", start, end)
    return result
