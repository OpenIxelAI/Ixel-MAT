"""verify-engine-1: E1 with the REAL SLOWEST_WAIT_MIN (30s) and default timeout; E2 stats streak reset."""
import asyncio
import json
import pathlib
import random
import sys
import tempfile
import time

sys.path.insert(0, "/home/user/Ixel-MAT/tests")

from ixel_mat.modes import review as engine  # noqa: E402
from ixel_mat.modes.review import run_review  # noqa: E402
from ixel_mat import stats  # noqa: E402
from test_review import PanelAgent, QUESTION  # noqa: E402


def run(agents, **kw):
    kw.setdefault("rng", random.Random(1))
    return asyncio.run(run_review(QUESTION, agents, **kw))


# E1, real constant: two instant failures + one healthy model that needs 33s, with the default timeout.
def test_e1_real_constant_two_instant_failures_cut_off_healthy_third():
    assert engine.SLOWEST_WAIT_MIN == 30.0
    events = []
    agents = [
        PanelAgent("a", "x", fail_on={"answer"}),
        PanelAgent("b", "x", fail_on={"answer"}),
        PanelAgent("c", "17 × 23 = 391", delay=33),
    ]
    t0 = time.perf_counter()
    result = asyncio.run(run_review(QUESTION, agents, mode="quick", rng=random.Random(1),
                                    on_event=lambda e: events.append((e.kind, dict(e.data)))))
    took = time.perf_counter() - t0
    print(f"\nrun took {took:.1f}s; default timeout = {engine.DEFAULT_TIMEOUT}")
    print("failures:", [(f.agent, f.error) for f in result.failures])
    print("error:", result.error, "| final:", result.final)
    assert 29 < took < 33, took  # cut at 30s, not after the model's 33s, not at the 300s timeout
    assert result.final is None and result.error == "No agent produced an answer."
    assert [f.error for f in result.failures if f.agent == "c"] == \
        ["left out: still working 30s in, when the others had finished in 0s"]


# E1 control: same healthy model with only ONE instant failure beside it (2 agents: no cut-off) → it answers.
def test_e1_control_two_agents_one_failure_waits():
    agents = [PanelAgent("a", "x", fail_on={"answer"}), PanelAgent("c", "17 × 23 = 391", delay=1)]
    result = run(agents, mode="quick")
    assert result.final is not None and "391" in result.final.answer


# E2: the confirmed-without-use reply is counted as a big-model answer and resets the streak.
def test_e2_confirmed_without_use_resets_streak_in_stats():
    path = pathlib.Path(tempfile.mkdtemp()) / "stats.json"
    # first, two genuine saves to build a streak
    for _ in range(2):
        r = run([PanelAgent("small", "17 × 23 = 391"),
                 PanelAgent("big", "", replies={"answer": '{"status": "confirmed", "use": "A"}'})],
                mode="saver", verifier="big")
        assert r.verifier_outcome == "confirmed"
        upd = stats.record_run(r, path=path)
    print("\nafter 2 real saves:", upd.saves, "streak", upd.streak)
    assert upd.saves == 2 and upd.streak == 2
    r = run([PanelAgent("small", "17 × 23 = 391"),
             PanelAgent("big", "", replies={"answer": '{"status": "confirmed"}'})],
            mode="saver", verifier="big")
    assert r.verifier_outcome == "corrected" and r.final.answer == '{"status": "confirmed"}'
    upd = stats.record_run(r, path=path)
    data = json.loads(path.read_text())
    print("after confirmed-without-use:", "saves", data["saves"], "streak", data["streak"],
          "big_model_answers", data["big_model_answers"])
    assert data["streak"] == 0 and data["big_model_answers"] == 1 and data["saves"] == 2


# E2 variant: "use" written as "Draft A" (not a bare label) → same fall-through.
def test_e2_use_draft_a_also_falls_through():
    r = run([PanelAgent("s1", "It's 391."), PanelAgent("s2", "It's 371."),
             PanelAgent("big", "", replies={"answer": '{"status": "confirmed", "use": "Draft A"}'})],
            mode="saver", verifier="big")
    print("\noutcome:", r.verifier_outcome, "| answer:", repr(r.final.answer))
    assert r.verifier_outcome == "corrected" and r.final.answer.startswith('{"status": "confirmed"')
