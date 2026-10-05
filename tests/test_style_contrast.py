"""The browser app's text colours stay readable: WCAG AA (4.5:1) on every surface text sits on."""
import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parents[1] / "ixel_mat" / "gui" / "static" / "style.css").read_text(encoding="utf-8")
TEXT = ("--text", "--text-2", "--muted")
COLOURED = ("--accent", "--green", "--red", "--amber", "--blue", "--violet")  # status words, links, "Has a board"
SURFACES = ("--bg", "--side", "--raised", "--sunken")


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", block))


def _themes() -> dict[str, dict[str, str]]:
    dark = _tokens(CSS[CSS.index(":root {"):CSS.index(':root[data-theme="light"] {')])
    light_block = CSS[CSS.index(':root[data-theme="light"] {'):]
    light = {**dark, **_tokens(light_block[:light_block.index("\n}\n")])}
    return {"dark": dark, "light": light}


def _contrast(a: str, b: str) -> float:
    def lum(hex_colour: str) -> float:
        rgb = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_text_colours_reach_aa_on_every_surface(theme):
    tokens = _themes()[theme]
    for text in TEXT:
        for surface in SURFACES:
            ratio = _contrast(tokens[text], tokens[surface])
            assert ratio >= 4.5, f"{theme}: {text} on {surface} is {ratio:.2f}:1"


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_coloured_words_reach_aa_on_every_surface(theme):
    tokens = _themes()[theme]
    for colour in COLOURED:
        for surface in SURFACES:
            ratio = _contrast(tokens[colour], tokens[surface])
            assert ratio >= 4.5, f"{theme}: {colour} on {surface} is {ratio:.2f}:1"


def test_faint_is_never_a_text_colour():
    # --faint is for lines and icons (about 2.5:1); words in it are hard to read
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS):
        if re.search(r"(?<![-\w])color:\s*var\(--faint\)", body):
            assert all(s.strip().endswith(".icon") for s in selectors.split(",")), selectors.strip()
