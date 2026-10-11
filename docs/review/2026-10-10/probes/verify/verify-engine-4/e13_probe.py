from ixel_mat.schema.response import Confidence, parse_structured_response
from ixel_mat.modes.review import _parse_verdict
for s in ["medium-high", "not high", "high", "medium", "low", "uncertain", "low-medium", "medium to high",
          "highly uncertain", "Medium (high on syntax)", "below average", "slow", "moderate", "HIGH", "", None]:
    print(repr(s), "->", Confidence.from_string(s))
print("--- JSON full-mode path")
r = parse_structured_response("x", '{"answer": "42", "confidence": "not high"}')
print(r.confidence, r.degraded)
print("--- verdict path")
raw = "The answer is 42.\n<ixel notes>\n{\"confidence\": \"medium-high\", \"disagreements\": [], \"corrections\": []}"
f = _parse_verdict(raw, "<ixel notes>")
print(f.confidence)
