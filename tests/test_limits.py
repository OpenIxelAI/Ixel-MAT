"""Out of usage: each tool's words for it, and the failures that aren't it."""
import pytest

from ixel_mat.limits import UsageLimit, out_of_usage, used_up


@pytest.mark.parametrize("error", [
    "Claude AI usage limit reached|1791140400",                                       # Claude Code, older
    "You've hit your session limit · resets 12pm (America/Los_Angeles)",             # Claude Code
    "5-hour limit reached ∙ resets 3pm",
    "You're out of extra usage",
    "'codex' exited with code 1: ERROR: You've hit your usage limit. Try again in 2 days.",  # Codex
    'API 429: {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}',               # Gemini
    "You exceeded your current quota, please check your plan and billing details.",  # OpenAI
    'API 429: {"error": {"type": "insufficient_quota"}}',
    "API 402: quota_exceeded: You have exceeded your premium request allowance",     # Copilot
    "API 403: You've reached your 5-hour usage limit. Upgrade your plan or wait.",   # Kimi Code
    'API 429: {"error": {"type": "exceeded_current_quota_error"}}',                  # Moonshot platform
    "API 400: Your credit balance is too low to access the Anthropic API.",          # Anthropic API
    "You've hit your Opus limit · resets Oct 9, 10am",                               # Claude Code, per model
    "You've hit your Sonnet limit · resets Oct 9, 10am",
    "You've reached your Fable limit.",
    "You've hit your limit · resets 3pm",
    "You've hit your org's monthly spend limit",
    "You've hit your team's shared budget.",
    "ERROR: Your workspace is out of credits. Add credits to continue.",             # Codex workspaces
    "ERROR: You hit your spend cap set in your workspace. Increase your spend cap to continue.",
    'API 402: {"error": {"message": "This request requires more credits, or fewer max_tokens."}}',  # OpenRouter
])
def test_each_tools_words_for_out_of_usage_are_recognised(error):
    assert out_of_usage(error)


@pytest.mark.parametrize("error", [
    "API 401: Incorrect API key provided",
    "API 400: This model's maximum context length is 128000 tokens",
    "API 403: Kimi For Coding is currently only available for Coding Agents",  # turned away, not used up
    "free tier can only be used from within OpenCode",
    "API 429: slow down",          # rate-limited for a moment: the transport retries it
    "'gemini' exited with code 41: Please set an Auth method",
    "Codex didn't answer within 300 seconds.",
    "API 400: prompt is too long: 250000 tokens > 200000 maximum",
    "You've reached your context limit for this conversation",
    "You have hit your rate limit. Please retry in 20s",
    "You've reached your concurrency limit",
    "You've hit your max turns limit",
    "You've reached your file size limit",
])
def test_other_failures_are_not_out_of_usage(error):
    assert not out_of_usage(error)


def test_a_usage_limit_is_still_a_runtime_error():
    assert isinstance(UsageLimit("x"), RuntimeError)  # callers that catch RuntimeError still do


def test_googles_quota_words_are_out_of_usage_but_worth_a_retry_first():
    # Google says the same for a used-up daily quota and for too many requests this minute
    error = 'API 429: {"error": {"status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota"}}'
    assert out_of_usage(error) and not used_up(error)
    assert used_up('API 429: {"error": {"type": "insufficient_quota"}}')  # OpenAI: the plan is used up
