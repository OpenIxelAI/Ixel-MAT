"""E5: punctuation in a grade or label. Unit edges, then an end-to-end run with a reviewer that writes them."""
import asyncio, json, random, sys
sys.path.insert(0, "/home/user/Ixel-MAT/tests"); sys.path.insert(0, __file__.rsplit("/", 1)[0])
from ixel_mat.modes.review import Verdict, _parse_label, run_review
from test_review import PanelAgent, FENCED_ANSWER, QUESTION, classify
from common import render

for raw in ["incorrect.", "Correct!", "correct ", "\"correct\"", "Incorrect", "partially correct.", "wrong."]:
    print(f"Verdict.parse({raw!r}) -> {Verdict.parse(raw).value}")
for raw in ["A.", "(A)", "A)", "Answer A", "answer A.", "a", "A:"]:
    print(f"_parse_label({raw!r}) -> {_parse_label(raw, {'A','B','C'})!r}")

class Punctuating(PanelAgent):
    """Grades like a careful reviewer but writes 'A.' labels and 'correct.' verdicts."""
    async def send_and_receive(self, message, **kwargs):
        if classify(message) != "review":
            return await super().send_and_receive(message, **kwargs)
        reviews, best = [], None
        for _, label, text in FENCED_ANSWER.findall(message):
            right = "391" in text
            reviews.append({"answer": label + ".", "verdict": ("correct." if right else "incorrect."),
                            "errors": [] if right else ["17 × 23 is 391"], "strengths": []})
            if right and best is None: best = f"({label})"
        return json.dumps({"reviews": reviews, "best": best, "summary": "Checked it."})

agents = [PanelAgent("gpt", "It's 391."), PanelAgent("claude", "17 × 23 = 391"),
          Punctuating("gemini", "The answer is 381.")]
result = asyncio.run(run_review(QUESTION, agents, mode="review", moderator="gpt", rng=random.Random(3)))
print("\ngemini's reviews kept:", [(r.target, r.verdict.value) for r in result.reviews if r.reviewer == "gemini"])
print("gemini's ballot best:", [b.best for b in result.ballots if b.reviewer == "gemini"])
print("failures:", [(f.agent, f.round, f.error) for f in result.failures])
print("standings:", [(s.answer.label, s.answer.agent, s.score, len(s.reviews)) for s in result.standings()])

class VerdictOnly(PanelAgent):
    """Labels fine, verdicts with a trailing period: every grade becomes 'unsure'."""
    async def send_and_receive(self, message, **kwargs):
        if classify(message) != "review":
            return await super().send_and_receive(message, **kwargs)
        reviews = [{"answer": label, "verdict": ("Correct." if "391" in text else "Incorrect."),
                    "errors": [] if "391" in text else ["17 × 23 is 391"], "strengths": []}
                   for _, label, text in FENCED_ANSWER.findall(message)]
        return json.dumps({"reviews": reviews, "best": None, "summary": "Checked it."})

agents = [VerdictOnly("gpt", "It's 391."), VerdictOnly("claude", "17 × 23 = 391"), VerdictOnly("gemini", "The answer is 381.")]
result = asyncio.run(run_review(QUESTION, agents, mode="review", moderator="gpt", rng=random.Random(3)))
print("\nall-'Correct.'/'Incorrect.' panel -> grades:", sorted({r.verdict.value for r in result.reviews}),
      "| scores:", [(s.answer.agent, s.score) for s in result.standings()], "| agreement:", result.agreement())
out = render(result)
print("\n".join(l for l in out.splitlines() if "Agreement" in l or "unsure" in l.lower() or "—" in l)[:1500])
