"""Make untrusted text (model output, server errors, pasted prompts) safe to print."""
from __future__ import annotations

import re

from rich.markup import escape

# ESC-introduced sequences: CSI (colors, cursor movement), OSC (window title,
# OSC 52 clipboard writes, hyperlinks), DCS/SOS/PM/APC strings, and the short
# escapes. Unterminated strings stop at a newline so the rest of an answer
# is not swallowed, and every branch runs in linear time on hostile input.
ESCAPE_SEQUENCE_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-?]*[ -/]*[@-~]"                  # CSI
    r"|\][^\x07\x1b\n]*(?:\x07|\x1b\\)?"    # OSC ... BEL or ST
    r"|[PX^_][^\x1b\n]*(?:\x1b\\)?"         # DCS / SOS / PM / APC ... ST
    r"|[ -/]+[0-~]"                         # nF (e.g. charset selection)
    r"|[@-_]"                               # other two-byte escapes
    r")"
)
# Leftover C0/C1 controls (keeping \t and \n), DEL, and bidi overrides that
# make text display differently from what it is.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")


def sanitize_terminal_text(text: object) -> str:
    """Strip terminal control sequences so text can only render as text."""
    if text is None:
        return ""
    cleaned = str(text).replace("\r\n", "\n")
    cleaned = ESCAPE_SEQUENCE_RE.sub("", cleaned)
    return _CONTROL_RE.sub("", cleaned)


def safe_markup(text: object) -> str:
    """Sanitize text and escape it for use inside a Rich markup string."""
    return escape(sanitize_terminal_text(text))
