"""Claude Code's "haiku" is Haiku 5.5 now: it takes effort, and it has its own price."""
from ixel_mat.effort import CLAUDE_ALIASES, NEWEST, program_levels
from ixel_mat.usage import price_for


def test_claude_codes_haiku_is_haiku_5_5():
    assert CLAUDE_ALIASES["haiku"] == "claude-haiku-5-5"
    assert NEWEST["latest-fast"]["anthropic"] == "claude-haiku-5-5"


def test_haiku_5_5_takes_effort_and_haiku_4_5_still_takes_none():
    assert program_levels("claude_code", "haiku") == ("low", "medium", "high", "xhigh", "max")
    assert program_levels("claude_code", "claude-haiku-5-5") == ("low", "medium", "high", "xhigh", "max")
    assert program_levels("claude_code", "claude-haiku-4-5") == ()


def test_haiku_5_5_has_a_price():
    assert price_for("claude-haiku-5-5") is not None
