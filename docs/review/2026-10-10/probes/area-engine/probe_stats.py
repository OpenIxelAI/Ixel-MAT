import json, sys, tempfile, pathlib
from ixel_mat.modes.review import ReviewResult, ReviewMode, PanelAnswer
from ixel_mat import stats
from ixel_mat.usage import CallUsage

d = pathlib.Path(tempfile.mkdtemp())
p = d / "stats.json"
p.write_text(json.dumps({"version": 1, "by_model": {"Small": 5}}))  # hand-edited: a number, not a table
r = ReviewResult("q", ReviewMode.SAVER)
r.answers = [PanelAnswer("A", "small", "Small", "391", 10)]
r.verifier_outcome, r.accepted = "confirmed", "A"
r.usage = [CallUsage("small", "Small", "answer", "panel", "api", "m", 10, 10, cost_usd=0.001)]
try:
    print("update:", stats.record_run(r, path=p))
except Exception as exc:
    print("record_run RAISED:", type(exc).__name__, exc)

# month totals mix exact and estimated spend with no flag
p2 = d / "s2.json"
r2 = ReviewResult("q", ReviewMode.QUICK)
r2.usage = [CallUsage("x", "X", "answer", "panel", "api", "m", 1000, 1000, estimated=True, cost_usd=0.5)]
stats.record_run(r2, path=p2)
print("cost lines:", stats.cost_lines(stats.cost_summary(stats.load_stats(p2))))
