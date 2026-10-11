"""E8: every grade 'unsure' -> agreement() says 'strong'; what the terminal/MCP/GUI say."""
import asyncio, json, random, sys
sys.path.insert(0, "/home/user/Ixel-MAT/tests"); sys.path.insert(0, __file__.rsplit("/", 1)[0])
from ixel_mat.modes.review import run_review
from test_review import PanelAgent, FENCED_ANSWER, QUESTION, classify
from common import render

class Unsure(PanelAgent):
    async def send_and_receive(self, message, **kwargs):
        if classify(message) != "review":
            return await super().send_and_receive(message, **kwargs)
        reviews = [{"answer": label, "verdict": "unsure", "errors": [], "strengths": []}
                   for _, label, text in FENCED_ANSWER.findall(message)]
        return json.dumps({"reviews": reviews, "best": None, "summary": "Can't check any of these."})

agents = [Unsure("gpt", "It's 391."), Unsure("claude", "17 × 23 = 391"), Unsure("gemini", "The answer is 381.")]
r = asyncio.run(run_review("What is 17 × 23?", agents, mode="review", moderator="gpt", rng=random.Random(2)))
print("grades:", sorted({x.verdict.value for x in r.reviews}), "| agreement():", r.agreement(), "| disputed:", r.disputed())
print("to_dict agreement (GUI):", r.to_dict()["agreement"])
print("confidence shown:", r.final.confidence.value, "| note:", r.final.note)
print("\n".join(l for l in render(r).splitlines() if "Agreement" in l or "unsure" in l))
from ixel_mat import mcp_server
fn = [n for n in dir(mcp_server) if "format" in n.lower() or "render" in n.lower() or "text" in n.lower()]
print("mcp formatters:", fn)
