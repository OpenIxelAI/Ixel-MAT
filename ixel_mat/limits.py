"""
Out of usage: a model that would answer, but has used up its plan's limit, its quota or its credits for now.

`ixel ask --agent codex,claude,local` hands the question to the next model you listed when one is out of
usage, and only then: any other failure (no key, a crash, a timeout) is something to fix, and the next
model answering would hide it.

Each tool says it its own way, and the wording drifts between versions, so the patterns are loose. They're
only ever matched against a tool's own error, never against the prompt or an answer.
"""
from __future__ import annotations

import re

USAGE_LIMIT = "usage_limit"  # the error_kind `ixel ask --json` reports

# Words that say the plan, the quota or the credits are used up: asking again in a few seconds won't help
_USED_UP = re.compile("|".join((
    r"usage limit",                                   # Claude Code, Codex ("You've hit your usage limit"), Kimi Code
    # Claude Code ("You've hit your Opus limit", "…your limit", "…your team's shared budget"), Codex ("spend cap");
    # not a rate, concurrency, turns or size limit, which a retry or a smaller request gets past
    r"(?:hit|reached) your (?!(?:[\w'’-]+ ){0,3}(?:rate|concurren\w*|requests?|turns?|files?|size|tokens?)\b)"
    r"(?:[\w'’-]+ ){0,3}(?:limit|budget|cap)\b",
    r"\b(?:session|weekly|daily|monthly|5-hour|five-hour) limit\b",
    r"\bout of (?:extra )?(?:usage|credits)\b",       # Claude Code's extra usage, Codex ("workspace is out of credits")
    r"\bspend(?:ing)? (?:cap|limit)\b",
    r"insufficient_quota|exceeded_current_quota",     # OpenAI, Moonshot
    r"premium request (?:allowance|limit)|quota_exceeded",  # GitHub Copilot
    r"credit balance is too low|insufficient (?:credits|balance)|requires more credits",  # Anthropic, OpenRouter
)), re.IGNORECASE)
# Google says these for a used-up daily quota and for too many requests this minute alike, so the transport
# retries them first
_PERHAPS_FOR_A_MINUTE = re.compile(r"resource_exhausted|exceeded your current quota|quota exceeded", re.IGNORECASE)
# A prompt too long for the model "reaches its limit" too, but a shorter one would be answered
_NOT_USAGE = re.compile(r"context (?:length|window|limit)|prompt is too long", re.IGNORECASE)


class UsageLimit(RuntimeError):
    """The model is out of usage for now: asking it again soon won't help."""


def used_up(error: str) -> bool:
    """Whether a tool's error says its plan, quota or credits are used up, so retrying soon is pointless."""
    return bool(_USED_UP.search(error)) and not _NOT_USAGE.search(error)


def out_of_usage(error: str) -> bool:
    """Whether a tool's error says it's out of usage for now: used up, or Google's quota words. Give it only
    the tool's own error, never a prompt it repeats or a (partial) answer, which can say anything."""
    return used_up(error) or (bool(_PERHAPS_FOR_A_MINUTE.search(error)) and not _NOT_USAGE.search(error))
