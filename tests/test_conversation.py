"""The saved conversation `ixel review --continue` picks up."""
import json
import os
import stat
import time

import pytest

from ixel_mat import conversation
from ixel_mat.conversation import asked_in_private, continued, expire, load_conversation, save_conversation
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


# ── kept for a day ────────────────────────────────────────────────────────────

HOUR = 60 * 60
NOW = 1_790_000_000  # 2026-09-21T14:13:20Z
real_read = conversation._read


def saved(path):
    """What the file holds: each question with the time it was saved."""
    return [(t["question"], t["saved"]) for t in json.loads(path.read_text(encoding="utf-8"))["turns"]]


def test_each_exchange_is_saved_with_the_time_it_was_saved_in_utc(tmp_path):
    path = tmp_path / "conversation.json"
    save_conversation([EarlierTurn("q", "a")], path, now=NOW)
    assert saved(path) == [("q", "2026-09-21T14:13:20Z")]
    # The file's own time is its oldest exchange's: that's all the check as ixel starts looks at
    assert path.stat().st_mtime == NOW


def test_continued_within_a_day_and_deleted_after_one(tmp_path):
    path = tmp_path / "conversation.json"
    save_conversation([EarlierTurn("q", "a")], path, now=NOW)
    assert load_conversation(path, now=NOW + 23 * HOUR) == [EarlierTurn("q", "a")]
    assert path.exists()
    assert load_conversation(path, now=NOW + 25 * HOUR) == []
    assert not path.exists()  # deleted, not just left out


def test_continuing_keeps_each_exchange_a_day_from_when_it_was_saved_not_from_the_last_save(tmp_path):
    # Asked at NOW, continued 23 hours later and again 46 hours later: the first question goes after a day,
    # and doesn't ride along with the later saves
    path = tmp_path / "conversation.json"
    save_conversation([EarlierTurn("Q1 my salary is 90k", "a1")], path, now=NOW)
    earlier = load_conversation(path, now=NOW + 23 * HOUR)
    assert [t.question for t in earlier] == ["Q1 my salary is 90k"]
    save_conversation(continued(earlier, result("Q2", "a2")), path, now=NOW + 23 * HOUR)
    assert saved(path) == [("Q1 my salary is 90k", "2026-09-21T14:13:20Z"), ("Q2", "2026-09-22T13:13:20Z")]
    assert path.stat().st_mtime == NOW  # the oldest one's time
    assert expire(path, now=NOW + 25 * HOUR) is True  # as the next ixel command starts
    assert "salary" not in path.read_text(encoding="utf-8") and [q for q, _ in saved(path)] == ["Q2"]
    assert path.stat().st_mtime == NOW + 23 * HOUR
    earlier = load_conversation(path, now=NOW + 46 * HOUR)
    save_conversation(continued(earlier, result("Q3", "a3")), path, now=NOW + 46 * HOUR)
    assert [q for q, _ in saved(path)] == ["Q2", "Q3"]
    assert [t.question for t in load_conversation(path, now=NOW + 48 * HOUR)] == ["Q3"]
    assert [q for q, _ in saved(path)] == ["Q3"]  # dropped from the file, not just left out


def test_an_exchange_past_its_day_isnt_saved_again(tmp_path):
    # Loaded 23 hours in, by a review that took two hours
    path = tmp_path / "conversation.json"
    save_conversation([EarlierTurn("old", "a")], path, now=NOW)
    earlier = load_conversation(path, now=NOW + 23 * HOUR)
    save_conversation(continued(earlier, result("new", "b")), path, now=NOW + 25 * HOUR)
    assert [q for q, _ in saved(path)] == ["new"]


def test_the_saved_time_counts_even_if_the_file_was_touched_since(tmp_path):
    path = tmp_path / "conversation.json"
    save_conversation([EarlierTurn("q", "a")], path, now=NOW - 30 * HOUR)
    os.utime(path, (NOW, NOW))  # a backup put it back, say
    assert load_conversation(path, now=NOW) == [] and not path.exists()


def test_a_file_from_an_older_ixel_goes_by_when_it_was_changed(tmp_path):
    path = tmp_path / "conversation.json"
    path.write_text(json.dumps({"turns": [{"question": "q", "answer": "a"}]}), encoding="utf-8")
    os.utime(path, (NOW - HOUR, NOW - HOUR))
    assert load_conversation(path, now=NOW) == [EarlierTurn("q", "a")]
    os.utime(path, (NOW - 25 * HOUR, NOW - 25 * HOUR))
    assert load_conversation(path, now=NOW) == [] and not path.exists()
    path.write_text(json.dumps({"turns": [{"question": "q", "answer": "a"}]}), encoding="utf-8")
    os.utime(path, (NOW - 25 * HOUR, NOW - 25 * HOUR))
    assert expire(path, now=NOW) is True and not path.exists()


def test_the_check_as_ixel_starts_deletes_only_what_is_past_its_day(tmp_path, monkeypatch):
    path = tmp_path / "conversation.json"
    assert expire(path, now=NOW) is False  # nothing there
    save_conversation([EarlierTurn("q", "a")], path, now=NOW - 23 * HOUR)
    reads = []
    monkeypatch.setattr(conversation, "_read", lambda p: reads.append(p) or real_read(p))
    assert expire(path, now=NOW) is False and path.exists()
    assert not reads  # a stat, and nothing more, while nothing is due
    assert expire(path, now=NOW + 2 * HOUR) is True and not path.exists()


def test_a_damaged_file_goes_when_a_conversation_would(tmp_path):
    path = tmp_path / "conversation.json"
    path.write_text("not json", encoding="utf-8")
    assert expire(path, now=time.time()) is False and path.exists()
    os.utime(path, (NOW - 25 * HOUR, NOW - 25 * HOUR))
    assert expire(path, now=NOW) is True and not path.exists()


def test_a_time_from_a_clock_that_was_far_ahead_doesnt_keep_an_exchange(tmp_path):
    path = tmp_path / "conversation.json"
    save_conversation([EarlierTurn("ahead", "a")], path, now=NOW + 365 * 24 * HOUR)
    assert expire(path, now=NOW) is True and not path.exists()
    save_conversation([EarlierTurn("ahead", "a")], path, now=NOW + 365 * 24 * HOUR)
    assert load_conversation(path, now=NOW) == [] and not path.exists()
    save_conversation([EarlierTurn("a little", "a")], path, now=NOW + HOUR)  # a clock a little ahead is fine
    assert load_conversation(path, now=NOW) == [EarlierTurn("a little", "a")]


def test_what_is_kept_of_a_private_conversation_stays_marked(tmp_path):
    path = tmp_path / "conversation.json"
    save_conversation([EarlierTurn("q1", "a1")], path, private=True, now=NOW)
    save_conversation([*load_conversation(path, now=NOW + 20 * HOUR), EarlierTurn("q2", "a2")], path,
                      private=True, now=NOW + 20 * HOUR)
    assert expire(path, now=NOW + 25 * HOUR) is True
    assert [q for q, _ in saved(path)] == ["q2"] and asked_in_private(path)


def test_asked_in_private_is_still_marked(tmp_path):
    path = tmp_path / "conversation.json"
    save_conversation([EarlierTurn("q", "a")], path, private=True)
    assert json.loads(path.read_text(encoding="utf-8"))["private"] is True and asked_in_private(path)
    save_conversation([EarlierTurn("q", "a")], path)
    assert not asked_in_private(path)


def test_checking_its_age_doesnt_load_the_review_engine():
    # Every ixel command checks it as it starts: `ixel version` mustn't wait a second for the engine
    import subprocess
    import sys
    code = ("import sys, ixel_mat.forget; ixel_mat.forget.tidy(); "
            "print(sorted(m for m in sys.modules if m.startswith(('ixel_mat.agents', 'ixel_mat.modes', 'anthropic'))))")
    env = {**os.environ, "HOME": os.devnull, "USERPROFILE": os.devnull}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"
