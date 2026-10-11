"""
Which thinking levels ("effort") each model really takes, so Settings offers only those and a question never
sends one its API would turn down.

Ixel's scale is EFFORT_LEVELS (minimal … ultra). Each model takes some of them, as its company documents: the
rules below were written from Anthropic's, OpenAI's, Google's and xAI's docs in October 2026, and each company's
last rule covers the models that come out after that. A level a model doesn't take goes to the nearest one it
does (minimal on GPT-6 is sent as low), and a model that takes none (Claude Haiku 4.5) gets none sent.

Programs are offered their provider's own ladder for the model they run (program_levels): Codex up to ultra on
the models that take it, Grok Build never max, Claude Code none on Haiku, Antigravity none when the model id
carries its own effort, Gemini CLI none at all; OpenCode gets the level as the model's #variant, for the
providers whose models Ixel knows. The Settings page and `ixel effort` both read them through native_levels.
"""
from __future__ import annotations

import os
import re

from ixel_mat.models import ALIASES, provider_for_url
from ixel_mat.presets import PRESETS_BY_ID, preset_for

UP_TO_ULTRA = ("low", "medium", "high", "xhigh", "max", "ultra")
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

# Per program, the first rule whose pattern fits the model id wins; a model none fits goes by its company's RULES
_PROGRAM_RULES: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    # Codex 0.162.1's own list (~/.codex/models_cache.json, models[].supported_reasoning_levels), 2026-10-11
    "codex": [
        (r"^gpt-(6(\.\d+)?|5\.6)-(sol|astra|terra)", UP_TO_ULTRA),
        (r"^gpt-(6(\.\d+)?|5\.6)-luna", UP_TO_MAX),
        (r"^gpt-5\.5", UP_TO_XHIGH),
    ],
    # Grok Build 1.0.50's own list (~/.grok/models_cache.json, models[].info.reasoning_efforts), 2026-10-11: no max
    "grok_build": [
        (r"^grok-4\.7-build-fast$", UP_TO_XHIGH),
        (r"^grok-4\.5", LOW_TO_HIGH),
    ],
}
PROGRAM_RULES = {program: [(re.compile(pattern), levels) for pattern, levels in rules]
                 for program, rules in _PROGRAM_RULES.items()}

# Claude Code 2.1.296's --model aliases, as the model each means
CLAUDE_ALIASES = {"fable": "claude-fable-5-1", "opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
                  "haiku": "claude-haiku-4-5"}

# Antigravity (agy 1.2.16) model ids that carry their own effort (gemini-3.8-flash-high): no --effort with those
EFFORT_SUFFIXES = ("-low", "-medium", "-high", "-xhigh", "-max")

# What a model takes on a server that isn't one of the big companies' (Ollama, LM Studio, OpenRouter…):
# the OpenAI scale as it was before xhigh, which such servers pass on or ignore
OTHER_SERVERS = MINIMAL_TO_HIGH

# A program of your own with an effort flag and no list of levels: Ixel's scale up to max (ultra is Codex's own)
OWN_PROGRAM = ("minimal", *UP_TO_MAX)

# The model each company's "latest" and "latest-fast" mean when Ixel can't ask yet; only its levels are used
NEWEST = {"latest": {"anthropic": "claude-opus-5-5", "openai": "gpt-6-astra", "gemini": "gemini-3.1-pro-preview",
                     "xai": "grok-4.7"},
          "latest-fast": {"anthropic": "claude-haiku-4-5", "openai": "gpt-6.1-sol", "gemini": "gemini-3.8-flash",
                          "xai": "grok-4.7-fast"}}

# Programs that hand the level straight to a company's API, and the model they use when given none. Ixel itself
# now reads program_levels; this stays for code that still asks it (the Board's /api/board/levels)
PASSES_ON = {"codex": ("openai", "gpt-6"), "grok": ("xai", "grok-4.7")}

# Programs that take minutes over a review at their own default level, and the level a review sends them when
# the agent has none set: Grok Build answered an 11.6k-character diff in 189 s at low, 604 s at high, and never
# within 300 s at its default (measured 2026-10-11, grok 1.0.50)
SLOW_REVIEWERS = {"grok": "low"}

# OpenCode providers whose models Ixel knows, and the company whose rules apply to them
OPENCODE_PROVIDERS = {"anthropic": "anthropic", "openai": "openai", "xai": "xai", "google": "gemini"}

# Each level as people read it
LABELS = {"minimal": "Minimal", "low": "Low", "medium": "Medium", "high": "High", "xhigh": "Extra high",
          "max": "Max", "ultra": "Ultra", "none": "None"}


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


def program_levels(program: str, model: str | None) -> tuple[str, ...]:
    """The levels a preset's program (its id) takes for model, lowest first, in its provider's own names; () when
    it takes none. No model: the program's own default."""
    if program == "claude_code":
        # Claude Code 2.1.296: --effort low..max, as Anthropic's API takes them for the model; Haiku takes none
        model = CLAUDE_ALIASES.get(model or "", model)
        if not model:
            return UP_TO_MAX
        if re.match(r"^claude-haiku", model):
            return NONE
        return tuple(level for level in model_levels("anthropic", model) if level in UP_TO_MAX)
    if program == "codex":
        if not model:  # its default model isn't known to Ixel: the levels every current Codex model takes
            return UP_TO_XHIGH
        found = next((levels for pattern, levels in PROGRAM_RULES["codex"] if pattern.search(model)), None)
        return model_levels("openai", model) if found is None else found
    if program == "grok_build":
        if not model:  # Grok 4.7, its newest
            return UP_TO_XHIGH
        found = next((levels for pattern, levels in PROGRAM_RULES["grok_build"] if pattern.search(model)), None)
        if found is None:  # grok-4.7-fast, an API-only model, stays none
            found = tuple(level for level in model_levels("xai", model) if level in UP_TO_XHIGH)
        return found
    if program == "antigravity":
        # agy 1.2.16: --effort low..max, except for a model id that carries its own
        return NONE if (model or "").endswith(EFFORT_SUFFIXES) else UP_TO_MAX
    if program == "gemini_cli":  # Gemini CLI 0.62.0 has no effort flag: thinking is set in its settings.json
        return NONE
    if program == "opencode":
        # OpenCode 2.0.26: the level is the model's #variant, offered only for providers whose models Ixel knows
        if not model or "#" in model:  # no model to put it in, or the model names its own variant
            return NONE
        provider, sep, bare = model.partition("/")
        company = OPENCODE_PROVIDERS.get(provider)
        if not sep or company is None:
            return NONE
        return tuple(level for level in model_levels(company, bare) if level in LOW_TO_HIGH)
    preset = PRESETS_BY_ID.get(program)
    if preset and (preset.get("effort_args") or preset.get("effort_in_model")):
        return tuple(preset.get("effort_levels") or ())
    return NONE


def slow_on_reviews(cfg) -> bool:
    """Whether an agent is a program that takes minutes over a review (Grok Build), so its wait says so."""
    # getattr: test fakes carry configs with only some of AgentConfig's fields
    return (cfg is not None and getattr(cfg, "type", None) == "oneshot"
            and program_name(getattr(cfg, "command", "")) in SLOW_REVIEWERS)


def review_effort(cfg, wanted: str | None = "") -> str:
    """The level to ask for in a review, or in any question with a diff or files attached: wanted when given
    (a verifier's level, say), else the agent's own setting (a person's always wins), else low for Grok Build
    so a review finishes; "" for none."""
    if wanted:
        return wanted
    if cfg is None:
        return ""
    own = getattr(cfg, "effort", "")
    if own:
        return own
    if slow_on_reviews(cfg):
        return SLOW_REVIEWERS[program_name(cfg.command)]
    return ""


def agent_levels(cfg, model: str | None = None) -> tuple[str, ...]:
    """The levels an agent can be given: its model's, for a model Ixel calls itself (model= the one it resolved
    to, when known); what the program takes for its model, for a program. () when it takes none, so none is
    sent."""
    model = cfg.model if model is None else model
    if cfg.type == "http":
        return model_levels(provider_for_url(cfg.url), model)
    if cfg.type != "oneshot" or not (cfg.effort_args or cfg.effort_in_model):
        return NONE
    program = preset_for(cfg.command).get("id", "")
    if program:
        theirs = program_levels(program, model or None)
        return tuple(level for level in theirs if level in (cfg.effort_levels or theirs))  # a config can narrow it
    from ixel_mat.agents.base import EFFORT_LEVELS  # the agents package imports this module
    if cfg.effort_in_model and (not model or "#" in model):  # no model to put it in, or it names its own
        return NONE
    return tuple(level for level in EFFORT_LEVELS if level in (cfg.effort_levels or OWN_PROGRAM))


def _labelled(levels) -> list[dict]:
    return [{"value": v, "label": LABELS.get(v, v.capitalize())} for v in levels]


def native_levels(program: str, model: str | None, company: str | None = None) -> list[dict]:
    """The levels a program (a preset id) or an API agent ("http", with its provider as company) can be given
    for model, lowest first, in the provider's own names: [{"value": "xhigh", "label": "Extra high"}, ...];
    [] when none."""
    if program == "http":
        return _labelled(model_levels(company, model or ""))
    if program in PRESETS_BY_ID:
        return _labelled(program_levels(program, model))
    return []


def agent_choices(cfg, model: str | None = None) -> list[dict]:
    """native_levels for an agent (model= another model than its own): [{"value", "label"}, …]; [] for none."""
    model = cfg.model if model is None else model
    program = preset_for(cfg.command).get("id", "") if cfg.type == "oneshot" else ""
    native = globals().get("native_levels")  # looked up when called, so it can be swapped
    if program and native is not None and (cfg.effort_args or cfg.effort_in_model):
        return [c for c in native(program, model or None) if not cfg.effort_levels or c["value"] in cfg.effort_levels]
    return _labelled(agent_levels(cfg, model))


def why_none(cfg) -> str:
    """Why an agent has no effort levels to pick from (no closing full stop)."""
    program = preset_for(cfg.command).get("id", "") if cfg.type == "oneshot" else ""
    if program == "gemini_cli":
        return f"{cfg.label} doesn't take an effort level from Ixel: set thinking in Gemini's own settings.json"
    if program == "opencode":
        return "OpenCode's effort is the #variant in the model name: ixel model <agent> provider/model#variant"
    if program == "antigravity" and (cfg.model or "").endswith(EFFORT_SUFFIXES):
        return f"{cfg.model} carries its own effort in its name, so Ixel sends none"
    if program == "claude_code" and cfg.model:
        return f"{cfg.label} takes no effort level with {cfg.model}, so Ixel sends none"
    if cfg.type == "http":
        return f"{cfg.model or cfg.label} has no effort setting, so Ixel sends none"
    return f"{cfg.label} doesn't take an effort level from Ixel"


def to_send(levels: tuple[str, ...], wanted: str) -> str | None:
    """The level to send for the one picked: itself, or the nearest the model takes. None when it takes none."""
    from ixel_mat.agents.base import nearest_effort
    if not wanted or not levels:
        return None
    return nearest_effort(wanted, list(levels))
