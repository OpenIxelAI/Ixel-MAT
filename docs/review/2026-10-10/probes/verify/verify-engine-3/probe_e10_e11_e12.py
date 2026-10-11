import asyncio, json, tempfile, pathlib
from ixel_mat.modes.review import ReviewResult, ReviewMode, PanelAnswer
from ixel_mat import stats
from ixel_mat.usage import CallUsage, call_usage, totals, cost_line
from ixel_mat.agents.base import AgentConfig
from ixel_mat.modes.full import FullModeDispatcher

d = pathlib.Path(tempfile.mkdtemp())

print("=== E10: realistic estimated API call (streamed non-OpenAI server, no usage reported) ===")
class FakeAgent:
    name, label = "gem", "Gemini"
    config = AgentConfig(name="gem", label="Gemini", type="http", url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                         model="gemini-2.5-pro", token="x")
cu = call_usage(FakeAgent(), "verdict", "panel", "prompt " * 500, "reply " * 500, reported=[],
                pricing={"gemini-2.5-pro": __import__("ixel_mat.usage", fromlist=["Price"]).Price(1.25, 10.0)})
print("CallUsage:", cu.billing, "estimated=", cu.estimated, "cost=", cu.cost_usd)
t = totals([cu]); print("per-run cost_line:", cost_line(t))
r = ReviewResult("q", ReviewMode.QUICK); r.usage = [cu]
p = d / "s10.json"; stats.record_run(r, path=p)
print("month lines:", stats.cost_lines(stats.cost_summary(stats.load_stats(p))))
print("stored keys in month entry:", json.loads(p.read_text())["months"])

print("=== E11: hand-edited by_model int ===")
p = d / "s11.json"
p.write_text(json.dumps({"version": 1, "by_model": {"Small": 5}}))
r = ReviewResult("q", ReviewMode.SAVER)
r.answers = [PanelAnswer("A", "small", "Small", "391", 10)]
r.verifier_outcome, r.accepted = "confirmed", "A"
r.usage = [CallUsage("small", "Small", "answer", "panel", "api", "m", 10, 10, cost_usd=0.001)]
try:
    print("record_run:", stats.record_run(r, path=p))
except Exception as exc:
    print("record_run RAISED:", type(exc).__name__, exc)
try:
    print("summary:", stats.summary(stats.load_stats(p))["leaderboard"])
except Exception as exc:
    print("summary RAISED:", type(exc).__name__, exc)
# a saver run whose winner is another model: does record_run survive, and summary afterwards?
r2 = ReviewResult("q", ReviewMode.SAVER)
r2.answers = [PanelAnswer("A", "other", "Other", "391", 10)]
r2.verifier_outcome, r2.accepted = "confirmed", "A"
try:
    print("record_run other winner:", stats.record_run(r2, path=p).counted)
    print("summary after:", stats.summary(stats.load_stats(p))["leaderboard"])
except Exception as exc:
    print("other winner path RAISED:", type(exc).__name__, exc)

print("=== E12: FullModeDispatcher never passes on_usage ===")
calls = []
class T:
    name, label, is_connected = "a", "A", True
    async def send_and_receive(self, message, **kw):
        calls.append(kw); return '{"answer": "42", "confidence": "high"}'
res = asyncio.run(FullModeDispatcher([T()]).dispatch("q"))
print("kwargs passed to send_and_receive:", calls, "| responses:", len(res.responses), "| has usage attr:", hasattr(res, "usage"))
