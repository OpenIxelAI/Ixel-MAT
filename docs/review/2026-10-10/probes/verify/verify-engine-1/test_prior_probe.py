"""Scratch probes for the review engine (read-only review). Each test asserts the CURRENT behaviour
so a pass means 'confirmed', and prints what the user would see."""
import asyncio
import json
import random
import sys

sys.path.insert(0, "/home/user/Ixel-MAT/tests")

import pytest  # noqa: E402

from ixel_mat.modes import review as engine  # noqa: E402
from ixel_mat.modes.review import (  # noqa: E402
    Ballot, PanelAnswer, PeerReview, ReviewResult, ReviewMode, Verdict, _parse_label, _parse_verdict,
    run_review,
)
from ixel_mat.schema.response import Confidence  # noqa: E402
from test_review import PanelAgent, QUESTION, classify  # noqa: E402


def run(agents, **kw):
    kw.setdefault("rng", random.Random(1))
    return asyncio.run(run_review(QUESTION, agents, **kw))


# 1. slowest-wait: two instant FAILURES count as "the others finished", so a healthy third model
#    is cut off after SLOWEST_WAIT_MIN even though nobody has answered yet.
def test_two_instant_failures_cut_off_the_only_healthy_model(monkeypatch):
    monkeypatch.setattr(engine, "SLOWEST_WAIT_MIN", 0.2)
    agents = [
        PanelAgent("a", "x", fail_on={"answer"}),
        PanelAgent("b", "x", fail_on={"answer"}),
        PanelAgent("c", "17 × 23 = 391", delay=0.8),  # healthy, just needs 0.8s
    ]
    result = run(agents, mode="quick", timeout=30)
    print("\nfailures:", [(f.agent, f.error) for f in result.failures])
    print("error:", result.error, "| final:", result.final)
    assert result.final is None and result.error == "No agent produced an answer."
    assert any(f.agent == "c" and f.error.startswith("left out") for f in result.failures)


# 2. saver: verifier confirms but leaves out "use" (natural with a single draft) -> the raw JSON
#    becomes the final answer and the run is counted as "corrected" (a big-model answer).
def test_confirmed_without_use_shows_json_as_the_answer():
    drafter = PanelAgent("small", "17 × 23 = 391")
    verifier = PanelAgent("big", "", replies={"answer": '{"status": "confirmed"}'})
    # the verify prompt is classified as "answer" by classify() (no other keyword), so replies["answer"] serves it
    result = run([drafter, verifier], mode="saver", verifier="big")
    print("\noutcome:", result.verifier_outcome, "| answer:", repr(result.final.answer), "| note:", result.final.note)
    assert result.verifier_outcome == "corrected"
    assert result.final.answer == '{"status": "confirmed"}'


# 3. saver second pass: verifier says send_back again with no issues -> raw JSON as the answer.
def test_second_send_back_without_issues_shows_json():
    calls = {"n": 0}

    class Verifier(PanelAgent):
        async def send_and_receive(self, message, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return json.dumps({"status": "send_back", "issues": ["wrong number"]})
            return json.dumps({"status": "send_back", "issues": []})

    drafters = [PanelAgent("s1", "It's 381."), PanelAgent("s2", "It's 371.")]
    result = run([*drafters, Verifier("big", "")], mode="saver", verifier="big")
    print("\noutcome:", result.verifier_outcome, "| answer:", repr(result.final.answer), "| note:", result.final.note)
    assert result.final.answer.startswith('{"status": "send_back"')


# 4. _parse_verdict: notes block first (or the answer written inside it) -> fence tags shown to the user,
#    confidence not parsed.
def test_parse_verdict_notes_first_leaks_fence_tags():
    fence = "IXEL-abcdef012345"
    marker = f"<{fence} notes>"
    raw = f'{marker}\n{{"confidence": "high", "disagreements": []}}\n</{fence}>\nThe answer is 391.'
    final = _parse_verdict(raw, marker)
    print("\nanswer shown:", repr(final.answer), "| confidence:", final.confidence)
    assert marker in final.answer and final.confidence is Confidence.UNCERTAIN


def test_parse_verdict_answer_inside_notes_json():
    fence = "IXEL-abcdef012345"
    marker = f"<{fence} notes>"
    raw = f'{marker}\n{{"answer": "391", "confidence": "high"}}\n</{fence}>'
    final = _parse_verdict(raw, marker)
    print("\nanswer shown:", repr(final.answer), "| confidence:", final.confidence)
    assert marker in final.answer


# 5. Grades/labels with trailing punctuation are dropped or become "unsure".
def test_verdict_and_label_parsing_edges():
    print("\n", Verdict.parse("incorrect."), Verdict.parse("Correct!"), Verdict.parse("not correct"),
          _parse_label("A.", {"A"}), _parse_label("(A)", {"A"}), _parse_label("Answer A", {"A"}))
    assert Verdict.parse("incorrect.") is Verdict.UNSURE
    assert _parse_label("A.", {"A"}) is None


# 6. agreement(): every reviewer "unsure" -> "strong".
def test_all_unsure_is_reported_as_strong_agreement():
    r = ReviewResult(QUESTION, ReviewMode.REVIEW)
    r.answers = [PanelAnswer("A", "a", "A", "x", 10), PanelAnswer("B", "b", "B", "y", 20)]
    r.reviews = [PeerReview("a", "A", "B", Verdict.UNSURE, [], [], False),
                 PeerReview("b", "B", "A", Verdict.UNSURE, [], [], False)]
    print("\nagreement:", r.agreement(), "| scores:", [(s.answer.label, s.score) for s in r.standings()])
    assert r.agreement() == "strong"


# 7. standings(): an answer nobody could grade (all unsure, score None) sorts BELOW one everyone called
#    incorrect (score 0.0), so the moderator/verifier fallback "best" is the one rated wrong.
def test_ungraded_answer_sorts_below_all_incorrect():
    r = ReviewResult(QUESTION, ReviewMode.REVIEW)
    r.answers = [PanelAnswer("A", "a", "A", "wrong", 10), PanelAnswer("B", "b", "B", "unknown", 20),
                 PanelAnswer("C", "c", "C", "also wrong", 30)]
    r.reviews = [PeerReview("b", "B", "A", Verdict.INCORRECT, [], [], False),
                 PeerReview("c", "C", "A", Verdict.INCORRECT, [], [], False),
                 PeerReview("a", "A", "B", Verdict.UNSURE, [], [], False),
                 PeerReview("c", "C", "B", Verdict.UNSURE, [], [], False),
                 PeerReview("a", "A", "C", Verdict.INCORRECT, [], [], False),
                 PeerReview("b", "B", "C", Verdict.INCORRECT, [], [], False)]
    order = [(s.answer.label, s.score) for s in r.standings()]
    print("\nstandings:", order)
    assert order[0][0] in ("A", "C") and order[-1][0] == "B"


# 8. deep mode: the moderator's prompt tags REVISED text with peer scores and flagged errors of the ORIGINAL.
def test_deep_verdict_prompt_pairs_old_scores_with_revised_text():
    agents = [PanelAgent("gpt", "It's 391."), PanelAgent("claude", "17 × 23 = 391"),
              PanelAgent("gemini", "The answer is 381.")]
    result = run(agents, mode="deep", moderator="gpt")
    verdict_prompt = agents[0].prompts_of("verdict")[0]
    gem = next(a for a in result.answers if a.agent == "gemini")
    print("\ngemini revised:", gem.was_revised, "| text now:", repr(gem.text))
    import re
    tag = re.search(rf"<IXEL-[0-9a-f]+ answer {gem.label} \(peer score ([0-9.]+)\)>\n(.*?)\n</IXEL", verdict_prompt, re.S)
    print("tag in verdict prompt:", tag.group(1), repr(tag.group(2)))
    print("flagged lines:", [l for l in verdict_prompt.splitlines() if "flagged" in l])
    assert gem.was_revised and tag.group(1) == "0.00" and "391" in tag.group(2)
    assert any("17 × 23 is 391" in l for l in verdict_prompt.splitlines() if "flagged" in l)


# 9. a reviewer whose JSON parses but names no valid answers contributes nothing and is NOT a failure.
def test_reviewer_with_empty_reviews_is_silently_ignored():
    agents = [PanelAgent("gpt", "It's 391."), PanelAgent("claude", "17 × 23 = 391"),
              PanelAgent("gemini", "The answer is 381.", replies={"review": '{"reviews": [], "best": "Z"}'})]
    result = run(agents, mode="review")
    print("\nfailures:", [(f.agent, f.round) for f in result.failures],
          "| ballots:", [(b.reviewer, b.best) for b in result.ballots],
          "| reviews by gemini:", [r for r in result.reviews if r.reviewer == "gemini"])
    assert not any(f.agent == "gemini" for f in result.failures)
    assert not [r for r in result.reviews if r.reviewer == "gemini"]


# 10. round numbering seen by the front end in saver send_back: verify 3/3, then fix 4/5, verify 5/5.
def test_saver_round_numbers_after_send_back():
    events = []
    calls = {"n": 0}

    class Verifier(PanelAgent):
        async def send_and_receive(self, message, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return json.dumps({"status": "send_back", "issues": ["wrong number"]})
            return json.dumps({"status": "confirmed", "use": "A"})

    drafters = [PanelAgent("s1", "It's 381."), PanelAgent("s2", "It's 371.")]
    asyncio.run(run_review(QUESTION, [*drafters, Verifier("big", "")], mode="saver", verifier="big",
                           on_event=lambda e: events.append(e) if e.kind == "round" else None))
    seq = [(e.data["round"], e.data["number"], e.data["total"]) for e in events]
    print("\nround events:", seq)
    assert seq == [("answer", 1, 3), ("review", 2, 3), ("verify", 3, 3), ("fix", 4, 5), ("verify", 5, 5)]


# 11. a call cut off by the slowest-wait that reported nothing is not in usage -> cost line omits it.
def test_cut_off_call_is_missing_from_usage(monkeypatch):
    monkeypatch.setattr(engine, "SLOWEST_WAIT_MIN", 0.2)
    agents = [PanelAgent("gpt", "It's 391."), PanelAgent("claude", "17 × 23 = 391"),
              PanelAgent("gemini", "The answer is 381.", delay=3)]
    result = run(agents, mode="quick")
    print("\ncalls:", result.calls, "| usage records:", len(result.usage),
          "| agents in usage:", sorted({u.agent for u in result.usage}))
    assert result.calls == 4 and "gemini" not in {u.agent for u in result.usage}
