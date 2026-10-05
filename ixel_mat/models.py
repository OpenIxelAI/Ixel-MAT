"""
"latest" models: which model id a moving alias means right now.

A config can say `model = "latest"` (the provider's newest top model) or
`model = "latest-fast"` (its newest small, fast one, a good saver-mode
drafter) instead of pinning an id that goes stale. Agents resolve the alias
from the provider's live model list when they connect, so a new release is
picked up without updating Ixel or rerunning setup. Any other value is a
specific model and is used as-is.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

ALIASES = ("latest", "latest-fast")

# Model ids a tier may resolve to, per provider (dated snapshots and variants
# such as -lite, -image, -audio or -codex are left out on purpose).
_TIERS: dict[str, dict[str, re.Pattern]] = {
    # GPT-6 came in named tiers: Astra is the top one, Sol the fast, cheaper one (Luna is smaller still)
    "openai": {
        "latest": re.compile(r"^gpt-\d+(\.\d+)?(-astra)?$"),
        "latest-fast": re.compile(r"^gpt-\d+(\.\d+)?-(mini|sol)$"),
    },
    "xai": {
        "latest": re.compile(r"^grok-\d+(\.\d+)?$"),
        "latest-fast": re.compile(r"^grok-\d+(\.\d+)?-(fast|mini)$"),
    },
    "gemini": {
        "latest": re.compile(r"^gemini-\d+(\.\d+)?-pro(-preview(-[\w.-]+)?|-latest)?$"),
        "latest-fast": re.compile(r"^gemini-\d+(\.\d+)?-flash(-preview(-[\w.-]+)?|-latest)?$"),
    },
    "anthropic": {
        "latest": re.compile(r"^claude-(mythos|fable|opus)-"),
        "latest-fast": re.compile(r"^claude-haiku-"),
    },
}


def provider_for_url(url: str) -> str | None:
    """Which provider's model naming an API url follows (None for local or custom servers)."""
    host = (urlparse(url).hostname or "").lower()
    if host == "anthropic.com" or host.endswith(".anthropic.com"):
        return "anthropic"
    if host == "api.openai.com":
        return "openai"
    if host == "api.x.ai":
        return "xai"
    if host == "generativelanguage.googleapis.com":
        return "gemini"
    return None


def _version(model_id: str) -> tuple[int, ...]:
    # "gpt-5.5" -> (5, 5); "gemini-3-pro-preview-06-05" -> (3,): what follows -preview is its date,
    # not more version (that made a preview outrank the stable release). Dates like 20250805 don't count.
    base = re.split(r"-(?:preview|latest)\b", model_id, maxsplit=1)[0]
    return tuple(int(n) for n in re.findall(r"\d+", base) if len(n) <= 3)


def _preview_date(model_id: str) -> tuple[int, ...]:
    # "gemini-3-pro-preview-06-05" -> (6, 5): orders previews of the same version
    _, _, tail = model_id.partition("-preview")
    return tuple(int(n) for n in re.findall(r"\d+", tail))


def pick_latest(provider: str, models: list[dict | str], alias: str = "latest") -> str | None:
    """
    The model an alias means, from a provider's model list: the highest version
    in the tier, preferring a stable release over a preview of the same version
    and then the most recently created. Anthropic lists newest first, so its
    first match wins. xAI's numbers don't always count up (grok-4.20 came out
    before grok-4.7), so its list goes by release date when it gives one.
    None when nothing in the list fits the tier.
    """
    pattern = _TIERS.get(provider, {}).get(alias)
    if pattern is None:
        return None
    entries = [m if isinstance(m, dict) else {"id": m} for m in models]
    ids = [str(e.get("id", "")).removeprefix("models/") for e in entries]
    matches = [(i, e) for i, e in zip(ids, entries) if pattern.search(i)]
    if not matches:
        return None
    if provider == "anthropic":
        return matches[0][0]
    if provider == "xai" and all(isinstance(e.get("created"), (int, float)) for _, e in matches):
        return max(matches, key=lambda m: m[1]["created"])[0]
    return max(matches, key=lambda m: (_version(m[0]), "preview" not in m[0], _preview_date(m[0]),
                                       m[1].get("created") or 0))[0]


def models_url(chat_url: str) -> str:
    """https://api.openai.com/v1/chat/completions -> https://api.openai.com/v1/models"""
    base = chat_url.rstrip("/")
    for suffix in ("/chat/completions", "/messages"):
        if base.endswith(suffix):
            return base[: -len(suffix)] + "/models"
    return base + "/models"


_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")


def valid_model_id(value: str) -> bool:
    """A model name, never something a command line could read as a flag."""
    return bool(_MODEL_ID.match(value))
