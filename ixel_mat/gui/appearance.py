"""
The app's Appearance setting: System (follow the computer as it switches), Light or Dark.

It's a choice about the window, not about models, so it lives in its own small file beside config.toml
and works before `ixel setup` has made one. The server writes it into the page it serves, so a window
opens in its colors at once rather than flashing the other ones first.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ixel_mat.config.secrets import write_private_file

logger = logging.getLogger("ixel_mat.gui")

APP_FILE = Path.home() / ".config" / "ixel-mat" / "app.json"
CHOICES = ("system", "light", "dark")
DEFAULT = "system"


def load(path: Path | None = None) -> str:
    """The saved choice, or System when there's none (or the file can't be read)."""
    path = path or APP_FILE
    try:
        # utf-8-sig: Notepad and PowerShell 5.1 may have added a BOM to a hand-edited file
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return DEFAULT
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable %s: %s", path, exc)
        return DEFAULT
    choice = data.get("appearance") if isinstance(data, dict) else None
    return choice if choice in CHOICES else DEFAULT


def save(choice: str, path: Path | None = None) -> str:
    """Keeps `choice` (one of CHOICES) and anything else the file holds. Raises ValueError for any other value
    and OSError when the file can't be written."""
    if choice not in CHOICES:
        raise ValueError("Pick System, Light or Dark.")
    path = path or APP_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["appearance"] = choice
    write_private_file(path, (json.dumps(data, indent=2) + "\n").encode("utf-8"))
    return choice


def into_page(html: bytes, choice: str) -> bytes:
    """index.html with the choice on its <html> element, where theme.js reads it before anything is drawn."""
    if choice not in CHOICES:
        choice = DEFAULT
    return html.replace(b'<html lang="en">', f'<html lang="en" data-appearance="{choice}">'.encode(), 1)
