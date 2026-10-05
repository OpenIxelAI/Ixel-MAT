"""
AgentResponse schema — structured output from every agent in /full mode.

Every agent response in /full gets parsed into this format.
This is what makes /max buildable later — without structure, 
consensus is just vibes.
"""

from dataclasses import dataclass, field
from enum import Enum
from time import time
from typing import Optional
import json
import re


class Confidence(Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNCERTAIN = "uncertain"

    @classmethod
    def from_string(cls, s: str) -> "Confidence":
        s = str(s or "").lower().strip()
        for member in cls:
            if member.value in s:
                return member
        return cls.UNCERTAIN


@dataclass
class AgentResponse:
    """Structured response from a single agent."""

    agent: str                          # agent id: "claude", "hermes"
    answer: str                         # the actual answer text
    confidence: Confidence = Confidence.UNCERTAIN
    evidence: list[str] = field(default_factory=list)       # citations, RFCs, docs, URLs
    uncertainties: list[str] = field(default_factory=list)   # what the agent isn't sure about
    commands: list[str] = field(default_factory=list)        # any commands/code suggested
    followup: str = ""                  # recommended next step
    raw: str = ""                       # original unstructured response
    latency_ms: int = 0                 # how long the agent took
    degraded: bool = False              # True if parsing failed, showing raw only
    degraded_reason: str = ""           # why parsing failed
    failed: bool = False                # no answer at all: an error or a timeout

    @property
    def confidence_score(self) -> float:
        """Numeric confidence for comparison: high=1.0, medium=0.7, low=0.4, uncertain=0.1"""
        return {
            Confidence.HIGH: 1.0,
            Confidence.MEDIUM: 0.7,
            Confidence.LOW: 0.4,
            Confidence.UNCERTAIN: 0.1,
        }[self.confidence]

    @property
    def has_evidence(self) -> bool:
        return len(self.evidence) > 0

    @property
    def is_complete(self) -> bool:
        """A response is complete if it has an answer and at least medium confidence."""
        return bool(self.answer) and self.confidence != Confidence.UNCERTAIN

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "answer": self.answer,
            "confidence": self.confidence.value,
            "confidence_score": self.confidence_score,
            "evidence": self.evidence,
            "uncertainties": self.uncertainties,
            "commands": self.commands,
            "followup": self.followup,
            "latency_ms": self.latency_ms,
            "degraded": self.degraded,
            "failed": self.failed,
            "has_evidence": self.has_evidence,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


# ── Prompt injection for /full mode ──────────────────────────────────────────
# This gets appended to the user's prompt when sent in /full mode.
# It asks the agent to structure their response so we can parse it.

# JSON schema is preferred — machine-parseable, no regex fragility.
# Markdown format kept as fallback instructions for models that struggle with JSON.

FULL_MODE_SUFFIX_JSON = """

Respond with a JSON object (and nothing else before or after it) using this exact schema:

```json
{
  "answer": "your answer here",
  "confidence": "high|medium|low|uncertain",
  "evidence": ["citation 1", "citation 2"],
  "uncertainties": ["uncertainty 1"],
  "commands": ["command 1"],
  "next_step": "what to do next"
}
```

Use empty arrays [] for fields with no items. Do not wrap the JSON in any other text.
"""

FULL_MODE_SUFFIX_MARKDOWN = """

IMPORTANT: Structure your response in this exact format:

**Answer:** [your answer here]

**Confidence:** [high/medium/low/uncertain]

**Evidence:** [citations, RFC numbers, documentation, URLs — one per line, or "none"]

**Uncertainties:** [what you're not sure about — one per line, or "none"]

**Commands:** [any commands or code you'd suggest — one per line, or "none"]

**Next Step:** [what you'd recommend doing next]
"""

# Default: try JSON first
FULL_MODE_SUFFIX = FULL_MODE_SUFFIX_JSON


# ── Response parser ──────────────────────────────────────────────────────────

def parse_structured_response(agent: str, raw: str, latency_ms: int = 0) -> AgentResponse:
    """
    Parse a structured agent response into AgentResponse.
    Strategy: try JSON first → markdown fallback → degraded.

    Never raises: models routinely send null, numbers, lists or objects where
    the schema asks for strings, and one odd field must not crash /compare.
    """
    if not isinstance(raw, str):
        raw = "" if raw is None else str(raw)

    # ── Attempt 1: JSON parse ─────────────────────────────────────────────
    json_result = _try_parse_json(raw)
    answer = _coerce_text(json_result.get("answer")) if json_result is not None else ""
    if answer:
        return AgentResponse(
            agent=agent,
            answer=answer,
            confidence=Confidence.from_string(_coerce_text(json_result.get("confidence"))),
            evidence=_ensure_list(json_result.get("evidence")),
            uncertainties=_ensure_list(json_result.get("uncertainties")),
            commands=_ensure_list(json_result.get("commands")),
            followup=_coerce_text(json_result.get("next_step", json_result.get("next step"))),
            raw=raw,
            latency_ms=latency_ms,
            degraded=False,
        )

    # ── Attempt 2: Markdown parse ─────────────────────────────────────────
    try:
        sections = _extract_sections(raw)
        answer = sections.get("answer", "").strip()
        if answer:
            return AgentResponse(
                agent=agent,
                answer=answer,
                confidence=Confidence.from_string(sections.get("confidence", "uncertain")),
                evidence=_parse_list(sections.get("evidence", "")),
                uncertainties=_parse_list(sections.get("uncertainties", "")),
                commands=_parse_list(sections.get("commands", "")),
                followup=sections.get("next step", "").strip(),
                raw=raw,
                latency_ms=latency_ms,
                degraded=False,
            )
    except Exception:
        pass

    # ── Attempt 3: Degraded ───────────────────────────────────────────────
    return AgentResponse(
        agent=agent,
        answer=raw.strip(),
        raw=raw,
        latency_ms=latency_ms,
        degraded=True,
        degraded_reason="No structured format detected (tried JSON + markdown)",
    )


def extract_json_object(raw: str) -> dict | None:
    """Find the JSON object in a model reply (bare, fenced, or embedded in prose)."""
    text = (raw or "").strip()

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # ```json ... ``` code blocks. Scanned with find(): a regex here backtracks
    # quadratically on a reply full of unclosed fences, and reviewers' replies
    # are untrusted.
    pos = 0
    while (start := text.find("```", pos)) != -1 and (end := text.find("```", start + 3)) != -1:
        block = text[start + 3:end].strip()
        if block[:4].lower() == "json":
            block = block[4:].strip()
        if block.startswith("{"):
            try:
                obj = json.loads(block)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
        pos = end + 3

    # First { ... last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    return None


def _try_parse_json(raw: str) -> dict | None:
    """Try to extract and parse an answer-shaped JSON object from the response."""
    obj = extract_json_object(raw)
    return obj if obj is not None and "answer" in obj else None


def _coerce_text(val) -> str:
    """Turn any JSON value into display text (null → "")."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, list):
        return "\n".join(t for t in (_coerce_text(v) for v in val) if t)
    if isinstance(val, dict):
        return json.dumps(val, ensure_ascii=False)
    return str(val).strip()


def _ensure_list(val) -> list[str]:
    """Ensure a value is a list of strings."""
    if val is None:
        return []
    if isinstance(val, list):
        return [t for t in (_coerce_text(v) for v in val) if t]
    if isinstance(val, str):
        return _parse_list(val)
    text = _coerce_text(val)
    return [text] if text else []


def _extract_sections(text: str) -> dict[str, str]:
    """
    Extract **Section:** content blocks from structured response.
    Returns dict with lowercase section names as keys.
    """
    sections = {}
    # Match **Label:** or **Label :** patterns
    # Labels are short and on one line; unbounded, the pattern backtracks
    # quadratically on hostile text (e.g. "**" followed by a long run of spaces).
    pattern = r'\*\*([^*\n]{1,80}?)\s{0,8}:\*\*\s*(.*?)(?=\*\*[^*\n]{1,80}?\s{0,8}:\*\*|$)'
    matches = re.findall(pattern, text, re.DOTALL | re.IGNORECASE)

    for label, content in matches:
        key = label.strip().lower()
        sections[key] = content.strip()

    return sections


def _parse_list(text: str) -> list[str]:
    """Parse a section that might be a list (newline or bullet separated)."""
    if not text or text.lower().strip() in ("none", "n/a", "-", ""):
        return []

    items = []
    for line in text.split("\n"):
        line = line.strip()
        # Strip bullet points, dashes, numbers
        line = re.sub(r'^[\-\*•]\s*', '', line)
        line = re.sub(r'^\d+[\.\)]\s*', '', line)
        if line and line.lower() not in ("none", "n/a"):
            items.append(line)

    return items


# ── Comparison utilities (future /max support) ───────────────────────────────

def compare_responses(responses: list[AgentResponse]) -> dict:
    """
    Compare multiple agent responses. Returns agreement summary.
    This is a building block for future /max consensus.
    """
    if len(responses) < 2:
        return {"status": "need_multiple", "responses": len(responses)}

    # Basic agreement: do confidence levels align?
    confidences = [r.confidence for r in responses]
    all_agree_confidence = len(set(confidences)) == 1

    # Evidence overlap
    all_evidence = [set(r.evidence) for r in responses if r.evidence]
    shared_evidence = set.intersection(*all_evidence) if all_evidence else set()

    # Count non-degraded
    valid = [r for r in responses if not r.degraded]
    degraded = [r for r in responses if r.degraded]

    return {
        "total": len(responses),
        "valid": len(valid),
        "degraded": len(degraded),
        "confidence_agreement": all_agree_confidence,
        "confidence_spread": [r.confidence.value for r in responses],
        "shared_evidence": list(shared_evidence),
        "avg_confidence": sum(r.confidence_score for r in responses) / len(responses),
        "avg_latency_ms": sum(r.latency_ms for r in responses) / len(responses),
        "all_have_evidence": all(r.has_evidence for r in valid),
    }
