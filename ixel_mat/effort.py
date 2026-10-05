"""
Which thinking levels ("effort") each model really takes, so Settings offers only those and a question never
sends one its API would turn down.

Ixel's scale is EFFORT_LEVELS (minimal … max). Each model takes some of them, as its company documents: the
rules below were written from Anthropic's, OpenAI's, Google's and xAI's docs in October 2026, and each company's
last rule covers the models that come out after that. A level a model doesn't take goes to the nearest one it
does (minimal on GPT-6 is sent as low), and a model that takes none (Claude Haiku 4.5) gets none sent.

Programs are told what the program itself takes (Claude Code: low to max). Codex hands the level straight to
OpenAI, so it gets the levels its model takes as well. Gemini CLI and OpenCode take none from Ixel.
"""
from __future__ import annotations

import os
import re

from ixel_mat.models import ALIASES, provider_for_url

UP_TO_MAX = ("low", "medium", "high", "xhigh", "max")
UP_TO_XHIGH = ("low", "medium", "high", "xhigh")
LOW_TO_HIGH = ("low", "medium", "high")
MINIMAL_TO_HIGH = ("minimal", "low", "medium", "high")
NONE: tuple[str, ...] = ()

# Per company, the first rule whose pattern fits the model id wins
_RULES: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "anthropic": [
        (r"^claude-opus-4-5(-\d{8})?$", LOW_TO_HIGH),
        (r"^claude-(opus|sonnet)-4-6", ("low", "medium", "high", "max")),
        # Haiku 4.5, Sonnet 4.5 and older take no effort at all
        (r"^claude-([23](\D|$)|instant|haiku-4(\D|$))|^claude-(opus|sonnet)-4(-[015])?(-\d{8})?$", NONE),
        (r"", UP_TO_MAX),  # Opus 4.7 on, Sonnet 5 on, Fable, Mythos
    ],
    "openai": [
        (r"chat|^gpt-[34](\D|$)|^o1-(mini|preview)|audio|realtime|search|transcribe|tts|image", NONE),
        (r"^gpt-5(-mini|-nano)?(-\d{4}-\d{2}-\d{2})?$", MINIMAL_TO_HIGH),
        (r"^gpt-5\.1-codex-max", UP_TO_XHIGH),
        (r"^gpt-5-codex|^gpt-5\.1(\D|$)", LOW_TO_HIGH),
        (r"^gpt-5\.\d", UP_TO_XHIGH),
        (r"^o\d", LOW_TO_HIGH),
        (r"^gpt-([6-9]|\d\d)", UP_TO_MAX),  # GPT-6 Astra, Sol and Luna take low to max
        (r"", LOW_TO_HIGH),
    ],
    "xai": [
        # Grok 4 itself and the fast models turn reasoning_effort down
        (r"fast|non-reasoning|^grok-[0-3](\D|$)|^grok-4(-\d{4})?$|^grok-4-", NONE),
        (r"^grok-4\.5", LOW_TO_HIGH),
        (r"^grok-(4\.([6-9]|\d\d)|[5-9])", UP_TO_XHIGH),  # 4.6, 4.7, 4.20 and on
        (r"", NONE),
    ],
    "gemini": [  # through Google's OpenAI-compatible endpoint, which maps these to thinking levels
        (r"^gemini-(1(\D|$)|2\.0)|^gemma|image|tts|live|audio", NONE),
        (r"^gemini-3-pro", ("low", "high")),
        (r"^gemini-3\.([7-9]|\d\d)|^gemini-3(\.\d+)?-pro|^gemini-([4-9]|\d\d)", LOW_TO_HIGH),
        (r"^gemini-3(\.\d+)?-flash|^gemini-2\.5", MINIMAL_TO_HIGH),
        (r"", LOW_TO_HIGH),
    ],
}
RULES = {company: [(re.compile(pattern), levels) for pattern, levels in rules] for company, rules in _RULES.items()}

# What a model takes on a server that isn't one of the big companies' (Ollama, LM Studio, OpenRouter…):
# the OpenAI scale as it was before xhigh, which such servers pass on or ignore
OTHER_SERVERS = MINIMAL_TO_HIGH

# The model each company's "latest" and "latest-fast" mean when Ixel can't ask yet; only its levels are used
NEWEST = {"latest": {"anthropic": "claude-opus-5-5", "openai": "gpt-6-astra", "gemini": "gemini-3.1-pro-preview",
                     "xai": "grok-4.7"},
          "latest-fast": {"anthropic": "claude-haiku-4-5", "openai": "gpt-6.1-sol", "gemini": "gemini-3.8-flash",
                          "xai": "grok-4.7-fast"}}

# Programs that hand the level straight to a company's API, and the model they use when given none
PASSES_ON = {"codex": ("openai", "gpt-6")}


def model_levels(company: str | None, model: str) -> tuple[str, ...]:
    """The levels a model takes on its API, in Ixel's names; () when it takes none."""
    if company not in RULES:
        return OTHER_SERVERS
    model = (model or "latest").removeprefix("models/")  # Gemini's own lists say models/gemini-…
    if model in ALIASES:
        model = NEWEST[model][company]
    return next(levels for pattern, levels in RULES[company] if pattern.search(model))


def program_name(command: str) -> str:
    name = os.path.basename(command or "").lower()
    for ending in (".exe", ".cmd", ".bat"):
        name = name.removesuffix(ending)
    return name


def agent_levels(cfg, model: str | None = None) -> tuple[str, ...]:
    """The levels an agent can be given: its model's, for a model Ixel calls itself (model= the one it resolved
    to, when known); what the program takes, for a program. () when it takes none, so none is sent."""
    model = cfg.model if model is None else model
    if cfg.type == "http":
        return model_levels(provider_for_url(cfg.url), model)
    if cfg.type != "oneshot" or not cfg.effort_args:
        return NONE
    from ixel_mat.agents.base import EFFORT_LEVELS  # the agents package imports this module
    takes = tuple(level for level in EFFORT_LEVELS if level in (cfg.effort_levels or EFFORT_LEVELS))
    passes_on = PASSES_ON.get(program_name(cfg.command))
    if passes_on:
        company, default = passes_on
        theirs = model_levels(company, model or default)
        takes = tuple(level for level in takes if level in theirs)
    return takes


def to_send(levels: tuple[str, ...], wanted: str) -> str | None:
    """The level to send for the one picked: itself, or the nearest the model takes. None when it takes none."""
    from ixel_mat.agents.base import nearest_effort
    if not wanted or not levels:
        return None
    return nearest_effort(wanted, list(levels))
