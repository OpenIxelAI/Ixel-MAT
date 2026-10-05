"""
The conversation `ixel review --continue` picks up: the last few questions asked
with `ixel review` and the panel's final answers to them.

It's saved owner-only (0600) in ~/.config/ixel-mat, overwritten by every
`ixel review`, and never holds more than MAX_EARLIER_TURNS exchanges. Each
exchange is kept for a day: it's saved with the time it was saved (UTC), and
once that's more than MAX_AGE_HOURS ago it's dropped from the file, which is
deleted when none is left. That's checked as every ixel command starts
(expire), and by `--continue` and the next save (load_conversation,
save_conversation). `ixel forget` deletes it at once. The terminal app and the
browser app keep their conversations in memory instead. A conversation asked
with Private on says so, and isn't continued with it off.
"""
from __future__ import annotations

import calendar
import json
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

# The review engine is imported only where it's needed: every ixel command checks this file's age as it
# starts (expire), and loading the engine takes about a second
if TYPE_CHECKING:
    from ixel_mat.modes.review import EarlierTurn, ReviewResult

CONVERSATION_FILE = Path.home() / ".config" / "ixel-mat" / "conversation.json"
MAX_AGE_HOURS = 24
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def _parse(stamp: object) -> float | None:
    """A time saved as _STAMP (UTC), in seconds since 1970, or None if it isn't one."""
    if isinstance(stamp, str):
        with suppress(ValueError):
            return float(calendar.timegm(time.strptime(stamp, _STAMP)))
    return None


def _changed(path: Path) -> float | None:
    """When the file was last changed, or None if it isn't there."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _in_its_day(saved: float | None, now: float) -> bool:
    """Whether what was saved at `saved` is still kept at `now`: saved at most MAX_AGE_HOURS ago. A time
    further ahead of now than that is from a clock that was wrong, and doesn't keep it longer either."""
    return saved is None or abs(now - saved) <= MAX_AGE_HOURS * 3600


def _delete(path: Path) -> bool:
    try:
        path.unlink()
    except OSError:  # gone already, or in use (Windows): the next check tries again
        return False
    return True


def _read(path: Path) -> dict | None:
    """The saved conversation as the file has it, or None if there's none (or it's damaged)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("turns"), list) else None


def _dated(data: dict, path: Path, now: float) -> list[tuple[float, str, str]]:
    """Its exchanges as (saved, question, answer). An exchange from an Ixel that didn't save its time goes
    by the time the file was last changed."""
    changed = _changed(path)
    fallback = now if changed is None else changed
    dated = []
    for turn in data["turns"]:
        if isinstance(turn, dict) and isinstance(turn.get("question"), str) and isinstance(turn.get("answer"), str):
            saved = _parse(turn.get("saved"))
            dated.append((fallback if saved is None else saved, turn["question"], turn["answer"]))
    return dated


def _write(path: Path, turns: Sequence[tuple[float, str, str]], private: bool) -> None:
    """Saves (saved, question, answer) exchanges, or deletes the file when there are none. The file's own
    time is set to its oldest exchange's, so a stat is all expire needs to know when one is due."""
    from ixel_mat.config.secrets import write_private_file

    if not turns:
        _delete(path)
        return
    data: dict = {"turns": [{"question": question, "answer": answer,
                             "saved": time.strftime(_STAMP, time.gmtime(saved))} for saved, question, answer in turns]}
    if private:
        data["private"] = True
    with suppress(OSError):
        write_private_file(path, json.dumps(data, indent=1).encode("utf-8"))
        oldest = min(saved for saved, _, _ in turns)
        os.utime(path, (oldest, oldest))


def expire(path: Path | None = None, now: float | None = None) -> bool:
    """
    Drops each exchange that's past its day. This runs as every ixel command starts, so it's a stat: the
    file's time is its oldest exchange's. Only once that's past is the file read, then saved again without
    what's past, or deleted when nothing is left. True if it dropped anything.
    """
    path = path or CONVERSATION_FILE
    now = time.time() if now is None else now
    if _in_its_day(_changed(path), now):
        return False
    data = _read(path)
    if data is None:  # damaged: it goes when a conversation would
        return _delete(path)
    dated = _dated(data, path, now)
    kept = [turn for turn in dated if _in_its_day(turn[0], now)]
    _write(path, kept, data.get("private") is True)
    return len(kept) < len(data["turns"])


def load_conversation(path: Path | None = None, now: float | None = None) -> list[EarlierTurn]:
    """The saved exchanges still in their day, or [] when there are none. One that's past it is dropped
    from the file, not just left out."""
    from ixel_mat.modes.review import MAX_EARLIER_TURNS, EarlierTurn

    path = path or CONVERSATION_FILE
    now = time.time() if now is None else now
    data = _read(path)
    if data is None:
        return []
    dated = _dated(data, path, now)
    kept = [turn for turn in dated if _in_its_day(turn[0], now)]
    if len(kept) < len(dated):
        _write(path, kept, data.get("private") is True)
    return [EarlierTurn(question, answer, saved=saved) for saved, question, answer in kept[-MAX_EARLIER_TURNS:]]


def asked_in_private(path: Path | None = None) -> bool:
    """Whether the saved conversation was asked with Private on (then it stays with your own models)."""
    try:
        data = json.loads((path or CONVERSATION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("private") is True


def continued(turns: Sequence[EarlierTurn], result: ReviewResult) -> list[EarlierTurn]:
    """The conversation after `result`: its question and final answer added, the oldest dropped."""
    from ixel_mat.modes.review import MAX_EARLIER_TURNS, EarlierTurn

    turn = EarlierTurn.from_result(result)
    return (list(turns) + [turn])[-MAX_EARLIER_TURNS:] if turn else list(turns)


def save_conversation(turns: Sequence[EarlierTurn], path: Path | None = None, private: bool = False,
                      now: float | None = None) -> None:
    """Saves the last MAX_EARLIER_TURNS exchanges, each with the time it was first saved (a new one's is
    now, in UTC), and none that's past its day."""
    from ixel_mat.modes.review import MAX_EARLIER_TURNS

    now = time.time() if now is None else now
    dated = [(now if t.saved is None else t.saved, t.question, t.answer) for t in turns[-MAX_EARLIER_TURNS:]]
    _write(path or CONVERSATION_FILE, [turn for turn in dated if _in_its_day(turn[0], now)], private)
