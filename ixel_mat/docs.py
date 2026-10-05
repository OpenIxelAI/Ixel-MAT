"""Ixel's docs, on ixelai.com: `ixel docs` in a terminal, and the Docs button on the app's Settings page.

The address is fixed here, so nothing a page or a settings file says can send the browser elsewhere.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Callable

DOCS_URL = "https://ixelai.com/docs/"
# How long the browser may take to say it's opening (a browser that's starting returns as soon as it's started)
OPEN_SECONDS = 30


def can_open_browser() -> bool:
    """Whether a browser window can open here. On Linux (and the BSDs) that needs a desktop: over plain
    SSH, Python would start a text browser in the terminal instead, and it would sit on the session."""
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def open_in_browser(url: str) -> bool:
    """webbrowser.open(url), in a Python of its own that has none of the keys Ixel saved: a browser that isn't
    running yet starts with that Python's environment, as every program Ixel starts does (child_env). True if
    the browser said it's opening."""
    from ixel_mat.agents.launch import NO_WINDOW_FLAGS
    from ixel_mat.config.secrets import child_env
    script = "import sys, webbrowser; sys.exit(0 if webbrowser.open(sys.argv[1]) else 1)"
    try:
        done = subprocess.run([sys.executable, "-I", "-c", script, url], env=child_env(nested=False),
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=OPEN_SECONDS, creationflags=NO_WINDOW_FLAGS)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def open_docs(opener: Callable[[str], bool] | None = None) -> bool:
    """Opens the docs in your own browser. False when there's no browser to open here, or it didn't start."""
    if not can_open_browser():
        return False
    try:
        return bool((opener or open_in_browser)(DOCS_URL))
    except Exception:  # noqa: BLE001 — a browser that won't start is the same as none: the address is shown
        return False
