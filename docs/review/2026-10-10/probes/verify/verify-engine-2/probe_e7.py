"""E7: an answer graded only 'unsure' sorts below one every reviewer graded incorrect; the fallback hands it over."""
import asyncio, json, random, sys
sys.path.insert(0, "/home/user/Ixel-MAT/tests"); sys.path.insert(0, __file__.rsplit("/", 1)[0])
from ixel_mat.modes.review import run_review
from test_review import PanelAgent, FENCED_ANSWER, QUESTION, classify
from common import render

Q = "What is the capital of Australia?"
WRONG1, UNJUDGED, WRONG2 = "The capital is Sydney.", "It depends on census tables I can't access.", "The capital is Melbourne."
GRADES = {WRONG1: "incorrect", WRONG2: "incorrect", UNJUDGED: "unsure"}

class Grader(PanelAgent):
    async def send_and_receive(self, message, **kwargs):
        if classify(message) != "review":
            return await super().send_and_receive(message, **kwargs)
        reviews = []
        for _, label, text in FENCED_ANSWER.findall(message):
            v = GRADES.get(text.strip(), "unsure")
            reviews.append({"answer": label, "verdict": v, "errors": ["Canberra is the capital"] if v == "incorrect" else [],
                            "strengths": []})
        return json.dumps({"reviews": reviews, "best": None, "summary": "Checked."})

def run(agents, **kw):
    kw.setdefault("rng", random.Random(5))
    return asyncio.run(run_review(Q, agents, **kw))

print("== 3 answers, moderator fails ==")
agents = [Grader("a", WRONG1, fail_on={"verdict"}), Grader("b", UNJUDGED), Grader("c", WRONG2)]
r = run(agents, mode="review", moderator="a")
print("standings:", [(s.answer.label, s.answer.agent, s.score) for s in r.standings()])
print("final.answer:", repr(r.final.answer)); print("note:", r.final.note)
print("\n".join(l for l in render(r).splitlines() if "moderator" in l.lower() or "Score" in l or "│" in l or "unsure" in l)[:2000])

print("\n== 2 answers (one 'unsure' grade is enough), moderator fails ==")
agents = [Grader("a", WRONG1, fail_on={"verdict"}), Grader("b", UNJUDGED)]
r = run(agents, mode="review", moderator="a")
print("reviews:", [(x.reviewer, x.target, x.verdict.value) for x in r.reviews])
print("standings:", [(s.answer.label, s.answer.agent, s.score) for s in r.standings()])
print("final.answer:", repr(r.final.answer)); print("note:", r.final.note)

print("\n== no moderator configured: who gets picked (standings[0] author) ==")
agents = [Grader("a", WRONG1), Grader("b", UNJUDGED), Grader("c", WRONG2)]
r = run(agents, mode="review")
print("moderator picked:", r.final.moderator, "| standings:", [(s.answer.agent, s.score) for s in r.standings()])

print("\n== saver: verifier fails -> 'best-rated draft' ==")
class Verifier(PanelAgent):
    async def send_and_receive(self, message, **kwargs):
        raise RuntimeError("verifier down")
agents = [Grader("a", WRONG1), Grader("b", UNJUDGED), Grader("c", WRONG2), Verifier("big", "")]
r = run(agents, mode="saver", verifier="big")
print("outcome:", r.verifier_outcome, "| final.answer:", repr(r.final.answer)); print("note:", r.final.note)
