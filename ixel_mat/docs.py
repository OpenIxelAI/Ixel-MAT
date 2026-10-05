"""Ixel's docs, on ixelai.com: `ixel docs` in a terminal, and the Docs button on the app's Settings page.

The address is fixed here, so nothing a page or a settings file says can send the browser elsewhere.
"""
from __future__ import annotations

import os
import sys
import webbrowser
from typing import Callable

DOCS_URL = "https://ixelai.com/docs/"


def can_open_browser() -> bool:
    """Whether a browser window can open here. On Linux (and the BSDs) that needs a desktop: over plain
    SSH, Python would start a text browser in the terminal instead, and it would sit on the session."""
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def open_docs(opener: Callable[[str], bool] | None = None) -> bool:
    """Opens the docs in your own browser. False when there's no browser to open here, or it didn't start."""
    if not can_open_browser():
        return False
    try:
        return bool((opener or webbrowser.open)(DOCS_URL))
    except Exception:  # noqa: BLE001 — a browser that won't start is the same as none: the address is shown
        return False
