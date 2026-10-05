"""
Saves counter for saver mode: how often your cheaper (or local) models got the
job done without the big model having to do it.

A *save* is a saver run whose final answer came from the drafters: the big
model confirmed a draft (possibly after sending the drafts back once), or
wasn't needed at all because every draft was rated correct. Runs where the big
model had to write the answer count, but aren't saves. Failed runs aren't
counted. Stored locally in ~/.config/ixel-mat/stats.json.

Every review (not just saver runs) also adds what it cost: dollars spent on calls billed to an
API key, per month and in all, and, for saves, the answer the big model didn't have to write
(tokens, and dollars when it's billed per token). See usage.py for how those are counted.

It also keeps score of the big model's first look at the drafts, when it had
peer reviews to go on: did it confirm the draft the reviewers rated highest,
pick another, or find them all wrong? If it nearly always takes the top-rated
draft, the cheaper models' reviews are doing most of the work.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ixel_mat.config.secrets import write_private_file
from ixel_mat.modes.review import ReviewMode, ReviewResult
from ixel_mat.usage import money, tokens_text, totals

logger = logging.getLogger("ixel_mat.stats")

STATS_FILE = Path.home() / ".config" / "ixel-mat" / "stats.json"

SAVE_OUTCOMES = {"confirmed", "skipped"}
COUNTED_OUTCOMES = {"confirmed", "skipped", "corrected", "answered", "unresolved"}

# id → (title, description); checked in this order
ACHIEVEMENTS = {
    "first_save": ("First save", "Your cheaper models handled a question on their own."),
    "saves_10": ("10 saves", "Ten answers without the big model doing the work."),
    "saves_25": ("25 saves", ""),
    "saves_50": ("50 saves", ""),
    "saves_100": ("100 saves", "Triple digits."),
    "saves_250": ("250 saves", ""),
    "saves_500": ("500 saves", ""),
    "saves_1000": ("1,000 saves", "The big model has barely broken a sweat."),
    "streak_5": ("On a roll", "5 saves in a row."),
    "streak_10": ("Hot streak", "10 saves in a row."),
    "streak_25": ("Unstoppable", "25 saves in a row."),
    "full_save": ("Didn't even need the big guy", "Every draft was rated correct, so the big model was never called."),
    "fixed_it": ("Fixed it themselves", "Sent back once, fixed, then accepted."),
    "local_hero": ("Local hero", "A model running on your own machine got the save."),
    "local_10": ("Home team", "10 saves by local models."),
}
_SAVE_MILESTONES = [(1, "first_save"), (10, "saves_10"), (25, "saves_25"), (50, "saves_50"), (100, "saves_100"),
                    (250, "saves_250"), (500, "saves_500"), (1000, "saves_1000")]
_STREAKS = [(5, "streak_5"), (10, "streak_10"), (25, "streak_25")]


def _empty() -> dict:
    return {
        "version": 1, "runs": 0, "saves": 0, "full_saves": 0, "saves_after_fix": 0,
        "big_model_answers": 0, "local_saves": 0, "streak": 0, "best_streak": 0,
        "checks_top_pick": 0, "checks_other_pick": 0, "checks_all_wrong": 0,
        "by_model": {}, "achievements": {}, "first_run": None, "last_run": None,
        # What reviews cost (every mode), and what saver mode saved
        "reviews": 0, "spent_usd": 0.0, "unpriced_calls": 0, "saved_usd": 0.0, "saved_tokens": 0, "months": {},
    }


MONTHS_KEPT = 24


def _month(stats: dict, key: str) -> dict:
    """This month's totals, repaired if the file was edited by hand."""
    entry = stats["months"].get(key)
    clean = {"reviews": 0, "spent_usd": 0.0, "unpriced_calls": 0, "saved_usd": 0.0, "saved_tokens": 0}
    if isinstance(entry, dict):
        for field_name, default in clean.items():
            value = entry.get(field_name, default)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                clean[field_name] = type(default)(value)
    stats["months"][key] = clean
    for old in sorted(stats["months"])[:-MONTHS_KEPT]:
        del stats["months"][old]
    return clean


def _read(path: Path) -> dict:
    # utf-8-sig: Notepad and PowerShell 5.1 may have added a BOM to a hand-edited file
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def load_stats(path: Path | None = None) -> dict:
    path = path or STATS_FILE
    stats = _empty()
    try:
        data = _read(path)
    except FileNotFoundError:
        return stats
    except (OSError, ValueError) as exc:
        logger.warning("Ignoring unreadable stats file %s: %s", path, exc)
        return stats
    for key, default in stats.items():
        value = data.get(key, default)
        if isinstance(value, type(default)) or default is None:
            stats[key] = value
        elif isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool):
            stats[key] = float(value)  # JSON wrote 3.0 as 3
    return stats


@dataclass
class StatsUpdate:
    counted: bool                       # False when the run didn't count (not saver / failed)
    saved: bool = False
    saves: int = 0
    runs: int = 0
    streak: int = 0
    unlocked: list[tuple[str, str, str]] = field(default_factory=list)  # (id, title, description)
    saved_usd: float = 0.0              # saved by saver mode in all, where it's in dollars
    saved_tokens: int = 0

    def to_dict(self) -> dict:
        return {"counted": self.counted, "saved": self.saved, "saves": self.saves, "runs": self.runs,
                "streak": self.streak, "saved_usd": round(self.saved_usd, 6), "saved_tokens": self.saved_tokens,
                "unlocked": [{"id": i, "title": t, "description": d} for i, t, d in self.unlocked]}


def _first_check(result: ReviewResult) -> str | None:
    """
    How the big model's first look went: "top_pick" (it confirmed a draft no other was
    rated above), "other_pick" or "all_wrong". None when it wasn't asked, or had no peer
    reviews to compare with.
    """
    if result.verifier_outcome not in ("confirmed", "corrected", "unresolved"):
        return None
    scores = {s.answer.label: s.score for s in result.standings()}
    rated = [score for score in scores.values() if score is not None]
    if not rated:
        return None
    if result.sent_back or result.verifier_outcome != "confirmed":
        return "all_wrong"
    accepted = scores.get(result.accepted)
    if accepted is None:
        return None
    return "top_pick" if accepted >= max(rated) else "other_pick"


def _record_cost(stats: dict, result: ReviewResult) -> None:
    month = _month(stats, datetime.now().strftime("%Y-%m"))
    total = totals(result.usage)
    for entry in (stats, month):
        entry["reviews"] += 1
        entry["spent_usd"] += total.api_usd
        entry["unpriced_calls"] += total.unpriced_api_calls  # billed, at a price Ixel doesn't know
    if result.saving is not None:
        stats["saved_tokens"] += result.saving.tokens
        month["saved_tokens"] += result.saving.tokens
        if result.saving.usd:
            stats["saved_usd"] += result.saving.usd
            month["saved_usd"] += result.saving.usd


def record_run(result: ReviewResult, local_agents: set[str] | frozenset[str] = frozenset(),
               path: Path | None = None) -> StatsUpdate:
    """
    Count one finished run and return what changed (never raises). Any review that made model
    calls adds its cost; saver runs also count toward saves (`counted`).
    """
    counted = result.mode is ReviewMode.SAVER and result.verifier_outcome in COUNTED_OUTCOMES
    if not counted and not result.usage:
        return StatsUpdate(counted=False)
    path = path or STATS_FILE
    stats = load_stats(path)
    if result.usage:
        _record_cost(stats, result)
    if not counted:
        _save(stats, path)
        return StatsUpdate(counted=False, saved_usd=stats["saved_usd"], saved_tokens=stats["saved_tokens"])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stats["runs"] += 1
    stats["first_run"] = stats["first_run"] or now
    stats["last_run"] = now

    saved = result.verifier_outcome in SAVE_OUTCOMES and bool(result.accepted)
    winner = result.answer(result.accepted) if saved else None
    earned: list[str] = []
    if saved and winner is not None:
        stats["saves"] += 1
        stats["streak"] += 1
        stats["best_streak"] = max(stats["best_streak"], stats["streak"])
        entry = stats["by_model"].setdefault(winner.agent_label, {"saves": 0, "local": False})
        entry["saves"] += 1
        if winner.agent in local_agents:
            entry["local"] = True
            stats["local_saves"] += 1
            earned.append("local_hero")
            if stats["local_saves"] >= 10:
                earned.append("local_10")
        if result.verifier_outcome == "skipped":
            stats["full_saves"] += 1
            earned.append("full_save")
        if result.sent_back:
            stats["saves_after_fix"] += 1
            earned.append("fixed_it")
        earned += [a for n, a in _SAVE_MILESTONES if stats["saves"] >= n]
        earned += [a for n, a in _STREAKS if stats["streak"] >= n]
    elif result.verifier_outcome in ("corrected", "answered", "unresolved"):
        stats["big_model_answers"] += 1
        stats["streak"] = 0
    check = _first_check(result)
    if check:
        stats[f"checks_{check}"] += 1

    unlocked = []
    for achievement in dict.fromkeys(earned):
        if achievement not in stats["achievements"]:
            stats["achievements"][achievement] = now
            title, description = ACHIEVEMENTS[achievement]
            unlocked.append((achievement, title, description))

    _save(stats, path)
    return StatsUpdate(counted=True, saved=saved, saves=stats["saves"], runs=stats["runs"],
                       streak=stats["streak"], unlocked=unlocked, saved_usd=stats["saved_usd"],
                       saved_tokens=stats["saved_tokens"])


def _save(stats: dict, path: Path) -> None:
    try:
        _read(path)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        # Not replaced by a fresh file: kept as stats.json.bad, for you to fix or delete
        try:
            path.replace(path.with_name(path.name + ".bad"))
        except OSError as exc:
            logger.warning("Not saving stats: %s can't be read or moved aside (%s)", path, exc)
            return
    try:
        write_private_file(path, (json.dumps(stats, indent=2) + "\n").encode("utf-8"))
    except OSError as exc:  # stats are a nicety; never break a review over them
        logger.warning("Couldn't save stats to %s: %s", path, exc)


def summary(stats: dict) -> dict:
    """Display-ready numbers, with a leaderboard of which models' drafts got accepted."""
    runs, saves = stats["runs"], stats["saves"]
    leaderboard = sorted(stats["by_model"].items(), key=lambda kv: -kv[1].get("saves", 0))
    return {
        "runs": runs, "saves": saves, "save_rate": (saves / runs) if runs else None,
        "full_saves": stats["full_saves"], "saves_after_fix": stats["saves_after_fix"],
        "big_model_answers": stats["big_model_answers"], "local_saves": stats["local_saves"],
        "streak": stats["streak"], "best_streak": stats["best_streak"],
        "big_model_checks": {k: stats[f"checks_{k}"] for k in ("top_pick", "other_pick", "all_wrong")},
        "leaderboard": [{"model": m, "saves": v.get("saves", 0), "local": bool(v.get("local"))} for m, v in leaderboard],
        "achievements": [{"id": a, "title": ACHIEVEMENTS[a][0], "description": ACHIEVEMENTS[a][1], "when": when}
                         for a, when in stats["achievements"].items() if a in ACHIEVEMENTS],
        "next_milestone": next((n for n, _ in _SAVE_MILESTONES if n > saves), None),
        "cost": cost_summary(stats),
    }


def cost_summary(stats: dict, month: str | None = None) -> dict:
    """What reviews cost this month and in all, and what saver mode saved."""
    month = month or datetime.now().strftime("%Y-%m")
    this = _month({"months": dict(stats["months"])}, month)
    return {"month": month, "month_reviews": this["reviews"], "month_spent_usd": round(this["spent_usd"], 6),
            "month_unpriced_calls": this["unpriced_calls"],
            "month_saved_usd": round(this["saved_usd"], 6), "month_saved_tokens": this["saved_tokens"],
            "reviews": stats["reviews"], "spent_usd": round(stats["spent_usd"], 6),
            "unpriced_calls": stats["unpriced_calls"],
            "saved_usd": round(stats["saved_usd"], 6), "saved_tokens": stats["saved_tokens"]}


def cost_lines(cost: dict) -> list[str]:
    """cost_summary in words: one line for this month, one in all (none before the first review)."""
    if not cost["reviews"]:
        return []

    def line(reviews: int, spent: float, unpriced: int, saved_usd: float, saved_tokens: int) -> str:
        calls = f"{unpriced} unpriced call{'s' if unpriced != 1 else ''}"
        if unpriced and not spent:
            text = f"{calls} on API keys (add prices under [pricing])"
        else:
            text = f"{money(spent)}{f' + {calls}' if unpriced else ''} on API keys"
        text += f" over {reviews} review{'s' if reviews != 1 else ''}"
        if saved_tokens:
            text += (f" · saver mode saved about {tokens_text(saved_tokens)} big-model tokens"
                     + (f" (≈ {money(saved_usd)})" if saved_usd else ""))
        return text

    out = []
    if cost["month_reviews"]:
        out.append("This month: " + line(cost["month_reviews"], cost["month_spent_usd"],
                                         cost.get("month_unpriced_calls", 0), cost["month_saved_usd"],
                                         cost["month_saved_tokens"]))
    if cost["reviews"] != cost["month_reviews"] or not out:
        out.append("In all: " + line(cost["reviews"], cost["spent_usd"], cost.get("unpriced_calls", 0),
                                     cost["saved_usd"], cost["saved_tokens"]))
    return out
