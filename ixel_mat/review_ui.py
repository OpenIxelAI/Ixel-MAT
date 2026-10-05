"""Terminal rendering for /review: a live progress view and the final report."""
from __future__ import annotations

import time
from typing import Callable

from rich import box
from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ixel_mat.modes.review import ReviewEvent, ReviewMode, ReviewResult, Verdict
from ixel_mat.stats import StatsUpdate, cost_lines
from ixel_mat.usage import cost_line, money, saving_line, tokens_text, totals
from ixel_mat.sanitize import sanitize_terminal_text
from ixel_mat.theme import C, spinner

ROUND_TITLES = {
    "answer": "answering independently",
    "review": "anonymous peer review",
    "revise": "revising after critique",
    "verdict": "moderator's verdict",
    "verify": "big-model verification",
    "fix": "drafters fix their answers",
}

VERIFIER_OUTCOMES = {
    "confirmed": "confirmed a draft without rewriting it",
    "corrected": "corrected the drafts",
    "skipped": "not needed: every draft was rated correct",
    "answered": "answered directly (no drafts came back)",
    "failed": "failed; showing the best draft, unverified",
}

VERDICT_MARKS = {
    Verdict.CORRECT: ("✓", C["green"]),
    Verdict.PARTIAL: ("~", C["gold"]),
    Verdict.INCORRECT: ("✗", C["red"]),
    Verdict.UNSURE: ("?", C["dim"]),
}

PREVIEW_LINES = 8
PREVIEW_CHARS = 700


def _s(text) -> str:
    return sanitize_terminal_text(text)


def _secs(ms: int) -> str:
    return f"{ms / 1000:.1f}s"


def _has_markdown(text: str) -> bool:
    return any(m in text for m in ("```", "**", "## ", "- ", "1. ", "| "))


def _body(text: str) -> RenderableType:
    # hyperlinks=False: a model could label a link "https://your-bank.com" and point
    # it elsewhere; with it off the real address is printed next to the text.
    text = _s(text)
    return Markdown(text, code_theme="monokai", hyperlinks=False) if _has_markdown(text) else Text(text, style=C["moon"])


def _hanging(label: Text, body: Text, indent: int = 4) -> Table:
    """label, then body; when body wraps, its lines stay aligned after the label."""
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=indent - 1 if indent > 1 else None)
    grid.add_column(no_wrap=True)
    grid.add_column(ratio=1)
    grid.add_row("", label, body)
    return grid


def mode_line(mode: ReviewMode, agent_count: int) -> Text:
    flow = " → ".join(mode.rounds)
    return Text.assemble(
        ("  ", ""), (f"{mode.value} mode", C["violet"]), (" · ", C["dim"]),
        (f"{agent_count} agents", C["dim"]), (" · ", C["dim"]), (flow, C["dim"]),
    )


class ReviewProgress:
    """Live view of a running review; feed it engine events via on_event.

    With `echo` (the console's print), a round that's over is printed into the terminal's history as
    soon as the next one starts, and the live view holds only the round still moving. That keeps it a
    few lines tall however many models are on the panel, and leaves a plain transcript behind. Print
    `header` once before starting the live view, and call `flush()` when the run ends.
    """

    def __init__(self, question: str, mode: ReviewMode, agent_count: int,
                 echo: Callable[[RenderableType], None] | None = None):
        self.header = Group(
            Text.assemble(("  ▸ ", C["gold"]), (_s(question).splitlines()[0][:300] if question.strip() else "", C["moon"])),
            mode_line(mode, agent_count),
            Text(""),
        )
        self.echo = echo
        self.started = time.perf_counter()
        self.lines: list[Text] = []
        self.pending: dict[str, tuple[str, float]] = {}
        self.round_total = 0  # calls in this round, and how many are back
        self.round_done = 0
        self.draft = ""  # the verdict so far, while it's being written
        self.verify_skipped = "every draft was rated correct"  # why a saver run needn't call the verifier

    def _done(self, agent: str) -> None:
        if self.pending.pop(agent, None) is not None:
            self.round_done += 1

    def flush(self) -> None:
        """Move the finished lines into the terminal's history (a no-op without `echo`)."""
        if self.echo is not None and self.lines:
            lines, self.lines = self.lines, []
            self.echo(Group(*lines))

    def on_event(self, event: ReviewEvent) -> None:
        d = event.data
        if event.kind == "round":
            self.flush()
            self.round_total, self.round_done = len(d["agents"]), 0
            title = f"  Round {d['number']}/{d['total']} · {ROUND_TITLES[d['round']]}"
            if d["round"] == "verify" and not d["agents"]:
                title += f" — not needed, {self.verify_skipped}"
            self.lines.append(Text(title, style=f"bold {C['gold']}"))
        elif event.kind == "agent_started":
            self.pending[d["agent"]] = (_s(d["agent_label"]), time.perf_counter())
        elif event.kind == "answer":
            self._done(d["agent"])
            self.lines.append(Text.assemble(("    ✓ ", C["green"]), (_s(d["agent_label"]), C["blue"]),
                                            (f"  answered in {_secs(d['latency_ms'])}", C["dim"])))
        elif event.kind == "labels":
            count = len(d["labels"])
            if count > 1:
                self.lines.append(Text(f"    answers labeled A–{chr(ord('A') + count - 1)} in random order; "
                                       "reviewers won't know who wrote which", style=C["dim"]))
        elif event.kind == "agent_failed":
            self._done(d["agent"])
            self.lines.append(Text.assemble(("    ✗ ", C["red"]), (_s(d["agent_label"]), C["blue"]),
                                            (f"  {_s(d['error'])[:160]}", C["dim"])))
        elif event.kind == "review":
            self._done(d["reviewer"])
            pick = d.get("best")
            detail = f"  picked {pick} as most accurate" if pick else "  reviewed"
            if pick and pick == d.get("own_label"):
                detail += " (its own)"
            self.lines.append(Text.assemble(("    ✓ ", C["green"]), (_s(d["reviewer_label"]), C["blue"]),
                                            (detail, C["dim"])))
        elif event.kind == "revision":
            self._done(d["agent"])
            self.lines.append(Text.assemble(("    ✓ ", C["green"]), (_s(d["agent_label"]), C["blue"]),
                                            (f"  revised answer {d['label']}", C["dim"])))
        elif event.kind == "sent_back":
            self.lines.append(Text.assemble(("    ↩ ", C["violet"]), (_s(d["verifier_label"]), C["blue"]),
                                            ("  sent the drafts back to be fixed: ", C["dim"]),
                                            ("; ".join(_s(i) for i in d["issues"])[:200], C["moon"])))
        elif event.kind == "verified":
            self.pending.clear()
            self.round_done = self.round_total
            if d["outcome"] in ("confirmed", "corrected"):
                self.lines.append(Text.assemble(("    ✓ ", C["green"]), (_s(d["verifier_label"]), C["blue"]),
                                                (f"  {VERIFIER_OUTCOMES[d['outcome']]}", C["dim"])))
        elif event.kind == "triage":
            if "verify" in d.get("skipped", []):
                self.verify_skipped = "Triage is sure the drafts agree and no reviewer found a problem"
            self.lines.append(Text.assemble(("    ⚡ ", C["gold"] if d.get("acted") else C["violet"]),
                                            (_s(d.get("note", "")), C["dim"])))
        elif event.kind == "verdict_text":
            self.draft += d["text"]
        elif event.kind == "final":
            self.pending.clear()
            self.draft = ""  # the report shows the finished verdict

    def __rich__(self) -> RenderableType:
        now = time.perf_counter()
        frame = spinner(now)
        waiting = [
            Text.assemble((f"    {frame} ", C["violet"]), (label, C["blue"]),
                          (f"  working… {now - started:.1f}s", C["gold"]))
            for label, started in self.pending.values()
        ]
        parts: list[RenderableType] = [*([] if self.echo else [self.header]), *self.lines, *waiting]
        if self.draft.strip():
            parts.append(Panel(Text(_draft_tail(self.draft), style=C["moon"]),
                               title=Text(" verdict, being written ", style=C["dim"]), title_align="left",
                               border_style=C["dim"], padding=(0, 1)))
        parts.append(self._status(now, frame))
        return Group(*parts)

    def _status(self, now: float, frame: str) -> Text:
        """The line under the view: where the round is, how long it's been, and how to stop."""
        elapsed = f"{now - self.started:.1f}s"
        counts = f"{self.round_done}/{self.round_total} done · " if self.round_total else "starting · "
        return Text.assemble(("  ", ""), (frame, C["violet"]), (f" {counts}", C["dim"]), (elapsed, C["gold"]),
                             ("  ·  ctrl+c cancels", C["dim"]))


def _draft_tail(text: str, lines: int = 10, width: int = 400) -> str:
    """The end of a long text, so a live view never grows taller than the window."""
    tail = text.rstrip().splitlines()[-lines:]
    return "\n".join(line if len(line) <= width else "…" + line[-width:] for line in tail)


def _shorten(text: str, limit: int) -> str:
    """Cut at a word boundary, drop trailing punctuation, and add an ellipsis."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] if " " in text[:limit] else text[:limit]
    return cut.rstrip(" ,;:—-") + "…"


def _close_markdown(line: str) -> str:
    """Close a **bold** or `code` span a cut left open, so no raw markers show."""
    if line.count("**") % 2:
        line += "**"
    if line.replace("**", "").count("`") % 2:
        line += "`"
    return line


def _preview(text: str) -> tuple[str, int]:
    """The first lines of an answer, cut between lines where possible, and how many lines are left out."""
    lines = _s(text).splitlines()
    shown: list[str] = []
    used = 0
    for line in lines[:PREVIEW_LINES]:
        if used + len(line) > PREVIEW_CHARS:
            if not shown:  # one long first line: cut it at a word
                shown.append(_close_markdown(_shorten(line, PREVIEW_CHARS)))
            break
        shown.append(line)
        used += len(line) + 1
    while shown and not shown[-1].strip():  # don't end on a blank line
        shown.pop()
    return "\n".join(shown), len(lines) - len(shown)


def answer_panels(result: ReviewResult, full: bool = False) -> list[RenderableType]:
    panels = []
    scores = {s.answer.label: s.score for s in result.standings()}
    for ans in result.answers:
        title = Text.assemble((f" {ans.label} ", f"bold {C['gold']}"), ("· ", C["dim"]),
                              (_s(ans.agent_label), C["blue"]), (f" · {_secs(ans.latency_ms)} ", C["dim"]))
        if ans.was_revised:
            title.append("· revised ", style=C["violet"])
        if full:
            body = _body(ans.text)
        else:
            clipped, hidden = _preview(ans.text)
            parts: list[RenderableType] = [_body(clipped)]
            if hidden:
                parts.append(Text(f"… {hidden} more lines (/answers shows everything)", style=C["dim"]))
            body = Group(*parts)
        score = scores.get(ans.label)
        border = C["dim"] if score is None else C["green"] if score >= 0.75 else C["gold"] if score >= 0.4 else C["red"]
        panels.append(Panel(body, title=title, title_align="left", border_style=border, padding=(0, 1)))
    return panels


def scoreboard(result: ReviewResult) -> Table | None:
    standings = result.standings()
    if not any(s.reviews for s in standings):
        return None
    table = Table(box=box.SIMPLE_HEAD, header_style=f"bold {C['moon']}", padding=(0, 1), expand=False)
    table.add_column("Answer", style=f"bold {C['gold']}", justify="center")
    table.add_column("Model", style=C["blue"])
    table.add_column("Peer score", justify="right")
    table.add_column("Verdicts")
    table.add_column("Picked best", justify="center", style=C["dim"])
    table.add_column("Issues flagged", justify="right")
    for s in standings:
        score = Text("—", style=C["dim"]) if s.score is None else Text(
            f"{s.score:.2f}", style=C["green"] if s.score >= 0.75 else C["gold"] if s.score >= 0.4 else C["red"])
        marks = Text()
        for r in s.reviews:
            glyph, color = VERDICT_MARKS[r.verdict]
            marks.append(glyph + " ", style=color)
        issues = len(s.flagged_errors)
        table.add_row(s.answer.label, Text(_s(s.answer.agent_label)), score, marks,
                      str(s.best_votes) if s.best_votes else "—",
                      Text(str(issues), style=C["red"]) if issues else Text("—", style=C["dim"]))
    return table


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def stats_lines(update: StatsUpdate) -> list[RenderableType]:
    """The saves counter shown after a saver run."""
    if not update.counted:
        return []
    icon, color = ("🏆", C["gold"]) if update.saved else ("·", C["dim"])
    line = Text.assemble(("  ", ""), (f"{icon} Saves: {update.saves}", f"bold {color}"),
                         (f"  ({_plural(update.runs, 'saver run')}", C["dim"]),
                         (f" · streak {update.streak})" if update.streak else ")", C["dim"]))
    out: list[RenderableType] = [line]
    if update.saved_tokens:
        out.append(Text(f"  Saved so far: about {tokens_text(update.saved_tokens)} big-model tokens"
                        + (f" (≈ {money(update.saved_usd)})" if update.saved_usd else ""), style=C["dim"]))
    for _, title, description in update.unlocked:
        out.append(Text.assemble(("  ★ New achievement: ", C["violet"]), (title, f"bold {C['gold']}"),
                                 (f" — {description}" if description else "", C["dim"])))
    return out + [Text("")]


def stats_view(summary: dict) -> list[RenderableType]:
    """`/saves` and `ixel saves`."""
    out: list[RenderableType] = [Text(""), Text("  Usage saver", style=f"bold {C['gold']}")]
    cost = [Text(f"  {_s(line)}", style=C["dim"]) for line in cost_lines(summary.get("cost") or {"reviews": 0})]
    if cost:
        cost.insert(0, Text("  What reviews cost", style=f"bold {C['moon']}"))
    if not summary["runs"]:
        out.append(Text("  No saver runs yet. Try /saver <question> (or ixel review --saver).", style=C["dim"]))
        return out + cost + [Text("")]
    rate = f"{summary['save_rate'] * 100:.0f}%"
    out.append(Text.assemble(
        ("  🏆 ", ""), (_plural(summary["saves"], "save"), f"bold {C['gold']}"),
        (f" in {_plural(summary['runs'], 'run')} ({rate})", C["dim"])))
    details = [f"{count} {text}" for count, text in (
        (summary["full_saves"], "without calling the big model at all"),
        (summary["saves_after_fix"], "fixed after a send-back"),
        (summary["big_model_answers"], "where the big model had to answer")) if count]
    if details:
        out.append(Text("  " + " · ".join(details), style=C["dim"]))
    checks = summary["big_model_checks"]
    if sum(checks.values()):
        parts = [f"{checks['top_pick']} of {sum(checks.values())} confirmed the reviewers' top-rated draft"]
        parts += [f"{count} {text}" for count, text in (
            (checks["other_pick"], "picked another draft"),
            (checks["all_wrong"], "found every draft wrong")) if count]
        out.append(Text("  Big-model checks: " + " · ".join(parts), style=C["dim"]))
    out.append(Text(f"  Streak {summary['streak']} (best {summary['best_streak']})"
                    + (f" · next milestone: {summary['next_milestone']} saves" if summary["next_milestone"] else ""),
                    style=C["dim"]))
    if summary["leaderboard"]:
        table = Table(box=box.SIMPLE_HEAD, header_style=f"bold {C['moon']}", padding=(0, 1))
        table.add_column("Model whose draft was accepted", style=C["blue"])
        table.add_column("Saves", justify="right")
        for row in summary["leaderboard"]:
            local = row["local"] and "local" not in row["model"].lower()  # setup already labels them "(local)"
            table.add_row(Text(_s(row["model"]) + ("  (local)" if local else "")), str(row["saves"]))
        out.append(table)
    if summary["achievements"]:
        out.append(Text("  Achievements", style=f"bold {C['moon']}"))
        for a in summary["achievements"]:
            out.append(Text.assemble(("  ★ ", C["violet"]), (a["title"], C["gold"]),
                                     (f" — {a['description']}" if a["description"] else "", C["dim"])))
    return out + cost + [Text("")]


def two_model_standoff(result: ReviewResult) -> bool:
    """A panel of two where each found fault with the other (or the verdict says they disagree)."""
    if len(result.answers) != 2 or result.mode is ReviewMode.SAVER or result.final is None:
        return False
    scores = [s.score for s in result.standings()]
    return all(score is not None and score < 1 for score in scores) or bool(result.final.disagreements)


def report(result: ReviewResult, *, full_answers: bool = False) -> list[RenderableType]:
    out: list[RenderableType] = [Text("")]
    if result.answers:
        out.append(Text("  Answers", style=f"bold {C['moon']}"))
        out += answer_panels(result, full=full_answers)

    table = scoreboard(result)
    if table is not None:
        out += [Text(""), Text("  Peer review", style=f"bold {C['moon']}"), table]
        flagged = [(s, who, err) for s in result.standings() for who, err in s.flagged_errors]
        if flagged:
            out.append(Text("  Issues reviewers flagged", style=f"bold {C['moon']}"))
            for s, who, err in flagged[:20]:
                out.append(_hanging(
                    Text.assemble((f"{s.answer.label} ", f"bold {C['gold']}"), (f"({_s(s.answer.agent_label)})", C["blue"])),
                    Text.assemble((f"{_s(who)}: ", C["dim"]), (_s(err), C["moon"]))))
            if len(flagged) > 20:
                out.append(Text(f"    … and {len(flagged) - 20} more", style=C["dim"]))
        for b in result.concessions():
            best = result.answer(b.best)
            out.append(_hanging(
                Text("↪", style=C["violet"]),
                Text.assemble(
                    (_s(b.reviewer_label), C["blue"]), (" conceded: rated ", C["dim"]),
                    (f"{b.best} ({_s(best.agent_label) if best else '?'})", C["gold"]),
                    (" above its own answer", C["dim"]),
                    *(((f" — “{_shorten(_s(b.summary), 200)}”", C["dim"]),) if b.summary else ())),
                indent=2))
        agreement = result.agreement()
        if agreement == "strong":
            out.append(Text("  Agreement: strong — reviewers were consistent about every answer", style=C["green"]))
        elif agreement == "split":
            out.append(Text(f"  Agreement: split — reviewers contradicted each other on answer "
                            f"{', '.join(result.disputed())}", style=C["gold"]))

    if result.failures:
        out.append(Text(""))
        for f in result.failures:
            out.append(Text.assemble(("  ⚠ ", C["gold"]), (_s(f.agent_label), C["blue"]),
                                     (f" ({f.round}): {_s(f.error)[:200]}", C["dim"])))

    out.append(Text(""))
    if result.final is None:
        out.append(Text(f"  ✗ {_s(result.error) or 'No verdict.'}", style=C["red"]))
    else:
        final = result.final
        parts: list[RenderableType] = [_body(final.answer)]
        if final.corrections:
            parts.append(Text("\nCorrections", style=f"bold {C['moon']}"))
            parts += [Text(f"• {_s(c)}", style=C["moon"]) for c in final.corrections]
        if final.disagreements:
            parts.append(Text("\nStill disputed", style=f"bold {C['gold']}"))
            parts += [Text(f"• {_s(d)}", style=C["gold"]) for d in final.disagreements]
        if final.note:
            parts.append(Text(f"\n{_s(final.note)}", style=C["dim"]))
        verb = "verified by" if result.mode is ReviewMode.SAVER and result.verifier_outcome in (
            "confirmed", "corrected") else "moderated by" if result.mode is not ReviewMode.SAVER else "from"
        subtitle = f" {verb} {_s(final.moderator_label)}"
        if final.confidence.value != "uncertain":
            subtitle += f" · confidence {final.confidence.value}"
        out.append(Panel(Group(*parts), title=Text(" IXEL VERDICT ", style=f"bold {C['gold']}"),
                         subtitle=Text(subtitle + " ", style=C["dim"]), subtitle_align="right",
                         border_style=C["gold"], padding=(1, 2)))
    if result.mode is ReviewMode.SAVER:
        panel_calls = result.tier_calls.get("panel", 0)
        big_calls = result.tier_calls.get("verifier", 0)
        verifier = result.final.moderator_label if result.final else "the verifier"
        out.append(Text.assemble(
            ("  Usage: ", C["dim"]), (f"{panel_calls} calls to your cheaper models", C["green"]),
            (" · ", C["dim"]), (f"{big_calls} to {_s(verifier)}", C["gold"] if big_calls else C["green"]),
            (f" ({VERIFIER_OUTCOMES.get(result.verifier_outcome, '')})" if result.verifier_outcome else "", C["dim"])))
    if result.saving is not None:
        out.append(Text(f"  {_s(saving_line(result.saving))}", style=C["green"] if result.saving.tokens else C["dim"]))
    if result.usage:
        out.append(Text(f"  Cost: {_s(cost_line(totals(result.usage)))}", style=C["dim"]))
    if two_model_standoff(result):
        out.append(Text("  Two models that disagree make a tie: there's no majority to settle it. A third "
                        "member fixes that, and Gemini CLI and Ollama are free (run ixel setup).", style=C["dim"]))
    follow_up = " · follow-up" if result.earlier_turns else ""
    triage = f" · {_plural(result.triage_calls, 'triage call')}" if result.triage_calls else ""
    out.append(Text(f"  ── {result.mode.value}{follow_up} · {result.calls} model calls{triage} · {_secs(result.elapsed_ms)} ──",
                    style=C["dim"]))
    out.append(Text(""))
    return out
