"""
The conversation `ixel review --continue` picks up: the last few questions asked
with `ixel review` and the panel's final answers to them.

It's saved owner-only (0600) in ~/.config/ixel-mat, overwritten by every
`ixel review`, and never holds more than MAX_EARLIER_TURNS exchanges. The
terminal app and the browser app keep their conversations in memory instead.
A conversation asked with Private on says so, and isn't continued with it off.
"""
from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from typing import Sequence

from ixel_mat.config.secrets import write_private_file
from ixel_mat.modes.review import MAX_EARLIER_TURNS, EarlierTurn, ReviewResult

CONVERSATION_FILE = Path.home() / ".config" / "ixel-mat" / "conversation.json"


def load_conversation(path: Path | None = None) -> list[EarlierTurn]:
    try:
        data = json.loads((path or CONVERSATION_FILE).read_text(encoding="utf-8"))
        turns = data["turns"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    if not isinstance(turns, list):
        return []
    return [EarlierTurn(t["question"], t["answer"]) for t in turns[-MAX_EARLIER_TURNS:]
            if isinstance(t, dict) and isinstance(t.get("question"), str) and isinstance(t.get("answer"), str)]


def asked_in_private(path: Path | None = None) -> bool:
    """Whether the saved conversation was asked with Private on (then it stays with your own models)."""
    try:
        data = json.loads((path or CONVERSATION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("private") is True


def continued(turns: Sequence[EarlierTurn], result: ReviewResult) -> list[EarlierTurn]:
    """The conversation after `result`: its question and final answer added, the oldest dropped."""
    turn = EarlierTurn.from_result(result)
    return (list(turns) + [turn])[-MAX_EARLIER_TURNS:] if turn else list(turns)


def save_conversation(turns: Sequence[EarlierTurn], path: Path | None = None, private: bool = False) -> None:
    data = {"turns": [{"question": t.question, "answer": t.answer} for t in turns[-MAX_EARLIER_TURNS:]]}
    if private:
        data["private"] = True
    with suppress(OSError):
        write_private_file(path or CONVERSATION_FILE, json.dumps(data, indent=1).encode("utf-8"))
