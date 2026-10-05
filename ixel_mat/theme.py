"""The IxelOS look in one place: the palette and glyphs every terminal screen draws from.

The colors are the moon logo's (assets/ixel-logo.svg): moonlight blue, violet and gold on deep navy.
"""
from __future__ import annotations

C = {
    "bg":      "#070b14",
    "navy":    "#0d1b2a",
    "moon":    "#c8d8e8",
    "blue":    "#7eb8d4",
    "violet":  "#9b7fc7",
    "gold":    "#d4af37",
    "dim":     "#6b7d94",
    "red":     "#e05252",
    "green":   "#4ade80",
}

PROMPT_GLYPH = "☾"  # the moon, small
PROMPT_ARROW = "❯"

# The prompt's moon grows with the conversation: one phase for each earlier exchange the next question
# will carry along (the terminal keeps three), and /new starts it over as a crescent
MOON_PHASES = ("☾", "◗", "◕", "●")


def moon(follow_ups: int) -> tuple[str, str]:
    """(glyph, color) of the prompt's moon after this many earlier exchanges; gold once it's full."""
    phase = max(0, min(follow_ups, len(MOON_PHASES) - 1))
    return MOON_PHASES[phase], C["gold"] if phase == len(MOON_PHASES) - 1 else C["violet"]


# The moon turning through its phases while something works; one phase every 1/6 s
SPINNER_FRAMES = "◐◓◑◒"


def spinner(now: float) -> str:
    """The spinner frame to show at this time (time.perf_counter() or any clock that only goes forward)."""
    return SPINNER_FRAMES[int(now * 6) % len(SPINNER_FRAMES)]
