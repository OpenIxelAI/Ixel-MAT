"""
A log of what Machines did, on this computer only: ~/.config/ixel-mat/machines.log, readable by you alone.
One line per connection, key pinned or forgotten, import, new key and command run: which machine and
address, how it went, and the key fingerprints that show a server's key changed. Never a command's text.
It's never sent anywhere.

It stays small and short-lived. Lines older than KEEP_DAYS days are dropped, and at MAX_BYTES the oldest
lines go, in that one file. Both happen as a line is written and as each ixel command starts (tidy): the
first two lines and the last 64 KB say whether anything is due, so the file is rewritten only when
something is. A log from an Ixel before this one (commands in it, and a machines.log.1 beside it) is
missing the first line, HEADER, so it's rewritten the first time, and machines.log.1 deleted. An app
window opened before an update can still be running that Ixel and adding commands: those are at the end,
so they're found there and go too. `ixel forget` deletes it (delete).
"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

LOG_FILE = Path.home() / ".config" / "ixel-mat" / "machines.log"
KEEP_DAYS = 30
MAX_BYTES = 1024 * 1024
HEADER = "# Ixel's Machines log, on this computer only. It never holds a command's text, and lines go after 30 days.\n"
# A line dated further ahead than this is from a clock that was wrong: it would keep itself, and the lines
# after it, past KEEP_DAYS
AHEAD_DAYS = 1
_PEEK = 64 * 1024  # how much of each end of the file the check reads
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_LINE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ")
# The command, as an older Ixel wrote it: in quotes, with \" and \\ escaped (a value never holds a bare ")
_COMMAND = re.compile(r' command="(?:[^"\\]|\\.)*"')
_lock = threading.Lock()


def _quote(value: object) -> str:
    """value in double quotes, at most 2000 characters of it (cut before escaping, so an escape is never
    cut in half and the closing quote is always the real one)."""
    text = str(value)[:2000].replace("\\", "\\\\").replace('"', '\\"')
    return '"' + "".join(ch if ch.isprintable() else f"\\x{ord(ch):02x}" for ch in text) + '"'


def _stamp(when: float) -> str:
    return time.strftime(_STAMP, time.gmtime(when))


def write(action: str, /, **fields: object) -> None:
    """One line: the time (UTC), the action, then name="value" pairs. Never raises."""
    path = LOG_FILE
    now = time.time()
    line = (" ".join([_stamp(now), action, *(f"{k}={_quote(v)}" for k, v in fields.items())]) + "\n").encode("utf-8")
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                there = _tidy(path, now, len(line))
            except OSError:  # another program has it open, say: it's tidied next time
                there = True
            # Bytes, so every line ends in \n alone, on Windows too
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "ab") as fh:
                fh.write(line if there else HEADER.encode("utf-8") + line)
    except (OSError, ValueError):
        pass


def tidy(now: float | None = None) -> None:
    """The check each ixel command makes as it starts: drops what's due, if anything is. Never raises."""
    try:
        with _lock:
            _tidy(LOG_FILE, time.time() if now is None else now)
    except (OSError, ValueError):
        pass


def delete() -> list[tuple[Path, str]]:
    """`ixel forget`: deletes the log, and an older Ixel's machines.log.1. Each one that was there, with ""
    once it's gone, or why it couldn't be deleted."""
    found = []
    with _lock:
        for path in (LOG_FILE, LOG_FILE.with_name(LOG_FILE.name + ".1")):
            try:
                path.unlink()
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as exc:
                found.append((path, exc.strerror or str(exc)))
                continue
            found.append((path, ""))
    return found


def _tidy(path: Path, now: float, adding: int = 0) -> bool:
    """Rewrites the log if anything in it is due, or an older Ixel wrote some of it, and deletes that
    Ixel's machines.log.1. adding: the bytes about to be written. True if the log is there."""
    # Apart from the rest: a machines.log.1 that won't go doesn't make every line written rewrite the log
    _unlink(path.with_name(path.name + ".1"))
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    with open(path, "rb") as fh:
        first, oldest = fh.readline(), fh.readline(_PEEK)
        fh.seek(max(size - _PEEK, 0))
        newest = fh.read(_PEEK)
    oldest_text = oldest.decode("utf-8", "replace")
    since, ahead = _window(now, KEEP_DAYS)
    due = (first.rstrip(b"\r\n") != HEADER.rstrip("\n").encode("utf-8")
           or size + adding > MAX_BYTES
           or (oldest_text and (not _LINE.match(oldest_text) or not since <= oldest_text[:20] <= ahead))
           # An older Ixel still running (an app window opened before an update) adds its lines at the end
           or b' command="' in newest)
    if due:
        _rewrite(path, now)
    return True


def _window(now: float, days: float) -> tuple[str, str]:
    """The stamps a line kept at `now` lies between: from `days` ago to AHEAD_DAYS ahead."""
    return _stamp(now - days * 86400), _stamp(now + AHEAD_DAYS * 86400)


def _rewrite(path: Path, now: float) -> None:
    """The log with HEADER, then its newest lines, without an older Ixel's commands. It keeps a little less
    than it must, so the next rewrite is a while away: lines from the last KEEP_DAYS - 1 days, filling three
    quarters of MAX_BYTES at most. A line that isn't one of the log's goes too, and so does one dated more
    than AHEAD_DAYS ahead."""
    from ixel_mat.config.secrets import write_private_file

    since, ahead = _window(now, KEEP_DAYS - 1)
    kept: list[str] = []
    size = len(HEADER)
    for line in reversed(path.read_bytes().decode("utf-8", "replace").splitlines()):
        if not _LINE.match(line) or not since <= line[:20] <= ahead:
            continue
        line = _COMMAND.sub("", line) + "\n"
        size += len(line.encode("utf-8"))
        if size > MAX_BYTES * 3 // 4:
            break
        kept.append(line)
    write_private_file(path, (HEADER + "".join(reversed(kept))).encode("utf-8"))


def _unlink(path: Path) -> None:
    """Deletes it if it's there and can go. One that can't (a folder, or in use) is tried the next time."""
    try:
        path.unlink()
    except OSError:
        pass
