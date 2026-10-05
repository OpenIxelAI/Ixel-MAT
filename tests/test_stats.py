"""Saver-mode saves counter and achievements."""
import json
import os
import stat

import pytest

from ixel_mat import stats
from ixel_mat.modes.review import FinalAnswer, PanelAnswer, PeerReview, ReviewMode, ReviewResult, Verdict


def saver_result(outcome, accepted="A", sent_back=False, winner="haiku", winner_label="Haiku"):
    result = ReviewResult("q", ReviewMode.SAVER, answers=[
        PanelAnswer("A", winner, winner_label, "391", 10), PanelAnswer("B", "flash", "Flash", "381", 12)])
    result.verifier_outcome = outcome
    result.accepted = accepted if outcome in ("confirmed", "skipped") else ""
    result.sent_back = sent_back
    result.final = FinalAnswer("391", moderator="opus", moderator_label="Opus")
    return result


def reviewed(result, a="correct", b="incorrect"):
    """Each drafter graded the other's draft: A (haiku's) by flash, B (flash's) by haiku."""
    result.reviews = [PeerReview("flash", "Flash", "A", Verdict(a), [], [], False),
                      PeerReview("haiku", "Haiku", "B", Verdict(b), [], [], False)]
    return result


@pytest.fixture
def stats_path(tmp_path):
    return tmp_path / "ixel-mat" / "stats.json"


def test_confirmed_draft_is_a_save(stats_path):
    update = stats.record_run(saver_result("confirmed"), path=stats_path)
    assert update.counted and update.saved and update.saves == 1 and update.runs == 1
    assert [u[0] for u in update.unlocked] == ["first_save"]
    data = stats.load_stats(stats_path)
    assert data["by_model"] == {"Haiku": {"saves": 1, "local": False}}


def test_big_model_answering_counts_but_breaks_the_streak(stats_path):
    stats.record_run(saver_result("confirmed"), path=stats_path)
    stats.record_run(saver_result("skipped"), path=stats_path)
    update = stats.record_run(saver_result("corrected"), path=stats_path)
    assert update.counted and not update.saved and update.saves == 2 and update.runs == 3 and update.streak == 0
    data = stats.load_stats(stats_path)
    assert data["big_model_answers"] == 1 and data["best_streak"] == 2 and data["full_saves"] == 1


def test_failures_and_other_modes_are_not_counted(stats_path):
    assert not stats.record_run(saver_result("failed"), path=stats_path).counted
    review = saver_result("confirmed")
    review.mode = ReviewMode.REVIEW
    assert not stats.record_run(review, path=stats_path).counted
    assert not stats_path.exists()


def test_special_achievements(stats_path):
    full = stats.record_run(saver_result("skipped"), path=stats_path)
    assert {"first_save", "full_save"} == {u[0] for u in full.unlocked}
    fixed = stats.record_run(saver_result("confirmed", sent_back=True), path=stats_path)
    assert [u[0] for u in fixed.unlocked] == ["fixed_it"]
    local = stats.record_run(saver_result("confirmed", winner="llama", winner_label="Llama (local)"),
                             local_agents={"llama"}, path=stats_path)
    assert [u[0] for u in local.unlocked] == ["local_hero"]
    assert stats.load_stats(stats_path)["local_saves"] == 1


def test_milestones_and_streaks_unlock_once(stats_path):
    unlocked = []
    for _ in range(10):
        unlocked += [u[0] for u in stats.record_run(saver_result("confirmed"), path=stats_path).unlocked]
    assert unlocked == ["first_save", "streak_5", "saves_10", "streak_10"]
    summary = stats.summary(stats.load_stats(stats_path))
    assert summary["saves"] == 10 and summary["save_rate"] == 1.0 and summary["next_milestone"] == 25
    assert summary["leaderboard"] == [{"model": "Haiku", "saves": 10, "local": False}]
    assert [a["id"] for a in summary["achievements"]] == ["first_save", "streak_5", "saves_10", "streak_10"]


def test_how_the_big_models_first_look_went(stats_path):
    for result in (
        reviewed(saver_result("confirmed", accepted="A")),                 # the reviewers' top pick
        reviewed(saver_result("confirmed", accepted="A"), b="correct"),    # a tie at the top is still the top
        reviewed(saver_result("confirmed", accepted="B")),                 # it disagreed with the reviewers
        reviewed(saver_result("corrected")),                               # every draft wrong
        reviewed(saver_result("confirmed", sent_back=True)),               # wrong at first, fixed later
        reviewed(saver_result("skipped"), b="correct"),                    # never asked
        saver_result("confirmed"),                                         # no reviews to compare with
    ):
        stats.record_run(result, path=stats_path)
    summary = stats.summary(stats.load_stats(stats_path))
    assert summary["big_model_checks"] == {"top_pick": 2, "other_pick": 1, "all_wrong": 2}
    assert summary["runs"] == 7


def test_stats_from_before_big_model_checks_still_load(stats_path):
    stats_path.parent.mkdir(parents=True)
    stats_path.write_text(json.dumps({"version": 1, "runs": 3, "saves": 2}))
    stats.record_run(reviewed(saver_result("confirmed")), path=stats_path)
    summary = stats.summary(stats.load_stats(stats_path))
    assert summary["runs"] == 4 and summary["big_model_checks"]["top_pick"] == 1


def test_corrupt_stats_file_is_ignored_not_fatal(stats_path):
    stats_path.parent.mkdir(parents=True)
    stats_path.write_text("{not json")
    update = stats.record_run(saver_result("confirmed"), path=stats_path)
    assert update.saves == 1
    assert json.loads(stats_path.read_text())["saves"] == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_stats_file_is_private(stats_path):
    stats.record_run(saver_result("confirmed"), path=stats_path)
    assert stat.S_IMODE(os.stat(stats_path).st_mode) == 0o600


def test_scoreboard_marks_local_models_once(stats_path):
    from rich.console import Console

    from ixel_mat.review_ui import stats_view
    stats.record_run(saver_result("confirmed", winner="llama", winner_label="llama3.3 (local)"),
                     local_agents={"llama"}, path=stats_path)
    stats.record_run(saver_result("confirmed", winner="qwen", winner_label="Qwen"), local_agents={"qwen"},
                     path=stats_path)
    console = Console(width=100, record=True)
    for part in stats_view(stats.summary(stats.load_stats(stats_path))):
        console.print(part)
    text = console.export_text()
    assert "llama3.3 (local)" in text and "(local)  (local)" not in text
    assert "Qwen  (local)" in text and "2 saves in 2 runs" in text


def test_scoreboard_counts_read_naturally(stats_path):
    from rich.console import Console

    from ixel_mat.review_ui import stats_lines, stats_view
    update = stats.record_run(saver_result("confirmed"), path=stats_path)
    console = Console(width=100, record=True)
    for part in stats_lines(update) + stats_view(stats.summary(stats.load_stats(stats_path))):
        console.print(part)
    text = console.export_text()
    assert "(1 saver run" in text and "1 save in 1 run" in text
    assert "0 without" not in text and "0 fixed" not in text  # zero counts aren't listed
    assert "Big-model checks" not in text  # that run had no reviews to compare with


def test_scoreboard_shows_how_the_big_models_checks_went(stats_path):
    from rich.console import Console

    from ixel_mat.review_ui import stats_view
    stats.record_run(reviewed(saver_result("confirmed", accepted="A")), path=stats_path)
    stats.record_run(reviewed(saver_result("corrected")), path=stats_path)
    console = Console(width=120, record=True)
    for part in stats_view(stats.summary(stats.load_stats(stats_path))):
        console.print(part)
    text = console.export_text()
    assert "Big-model checks: 1 of 2 confirmed the reviewers' top-rated draft · 1 found every draft wrong" in text
    assert "picked another" not in text


# ── Answer previews and long lines in the terminal report ────────────────────

def test_preview_cuts_between_lines_and_counts_what_it_left_out():
    from ixel_mat.review_ui import _preview
    text = "intro\n\n- **one.** detail\n- **two.** " + "long " * 200 + "\n- three"
    shown, hidden = _preview(text)
    assert shown == "intro\n\n- **one.** detail" and hidden == 2


def test_preview_of_one_long_line_ends_on_a_word_with_markers_closed():
    from ixel_mat.review_ui import _preview
    shown, hidden = _preview("**bold " + "word " * 300)
    assert shown.endswith("word…**") and shown.count("**") == 2 and hidden == 0


def test_shorten_stops_at_a_word():
    from ixel_mat.review_ui import _shorten
    assert _shorten("separating strong vs. mixed evidence, and more", 38) == "separating strong vs. mixed evidence…"
    assert _shorten("short", 38) == "short"
