"""The saved conversation `ixel review --continue` picks up."""
import json
import os
import stat

import pytest

from ixel_mat.conversation import continued, load_conversation, save_conversation
from ixel_mat.modes.review import MAX_EARLIER_TURNS, EarlierTurn, FinalAnswer, ReviewMode, ReviewResult


def result(question, answer):
    return ReviewResult(question, ReviewMode.QUICK, final=FinalAnswer(answer=answer) if answer else None)


def test_round_trip_keeps_only_the_last_turns(tmp_path):
    path = tmp_path / "conversation.json"
    turns = []
    for n in range(MAX_EARLIER_TURNS + 2):
        turns = continued(turns, result(f"q{n}", f"a{n}"))
    save_conversation(turns, path)
    loaded = load_conversation(path)
    assert [t.question for t in loaded] == [f"q{n}" for n in range(2, MAX_EARLIER_TURNS + 2)]
    assert loaded[-1] == EarlierTurn(f"q{MAX_EARLIER_TURNS + 1}", f"a{MAX_EARLIER_TURNS + 1}")


def test_a_failed_review_leaves_the_conversation_as_it_was():
    turns = [EarlierTurn("q", "a")]
    assert continued(turns, result("next", "")) == turns


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_saved_owner_only(tmp_path):
    path = tmp_path / "conversation.json"
    save_conversation([EarlierTurn("private question", "answer")], path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("content", ["not json", "[]", '{"turns": "x"}', '{"turns": [{"question": 1}]}'])
def test_a_damaged_file_is_an_empty_conversation(tmp_path, content):
    path = tmp_path / "conversation.json"
    path.write_text(content, encoding="utf-8")
    assert load_conversation(path) == []
    assert load_conversation(tmp_path / "missing.json") == []


def test_saved_as_utf8(tmp_path):
    path = tmp_path / "conversation.json"
    save_conversation([EarlierTurn("17 × 23?", "391 ✓")], path)
    assert json.loads(path.read_bytes().decode("utf-8"))["turns"][0]["answer"] == "391 ✓"
