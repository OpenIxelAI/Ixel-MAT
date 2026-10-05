"""
A log of what Machines did, on this computer only: ~/.config/ixel-mat/machines.log, readable by you alone.
One line per connection, key pinned or forgotten, import, new key and command run (with the command as
you typed it). It's never sent anywhere. Over 5 MB it moves to machines.log.1 and a new one starts.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path

LOG_FILE = Path.home() / ".config" / "ixel-mat" / "machines.log"
MAX_BYTES = 5 * 1024 * 1024
_lock = threading.Lock()


def _quote(value: object) -> str:
    """value in double quotes, at most 2000 characters of it (cut before escaping, so an escape is never
    cut in half and the closing quote is always the real one)."""
    text = str(value)[:2000].replace("\\", "\\\\").replace('"', '\\"')
    return '"' + "".join(ch if ch.isprintable() else f"\\x{ord(ch):02x}" for ch in text) + '"'


def write(action: str, /, **fields: object) -> None:
    """One line: the time (UTC), the action, then name="value" pairs. Never raises."""
    path = LOG_FILE
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = " ".join([stamp, action, *(f"{k}={_quote(v)}" for k, v in fields.items())]) + "\n"
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                if path.stat().st_size > MAX_BYTES:
                    os.replace(path, path.with_name(path.name + ".1"))
            except FileNotFoundError:
                pass
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        pass
