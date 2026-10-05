"""
What a review cost: tokens and dollars for each model call, and what saver mode saved.

Tokens come from the model when it reports them (every API, and Claude Code). Other CLIs
don't, so their tokens are counted from the text, at about 4 characters a token, and marked
as an estimate.

Dollars are at per-token API prices: the tool's own figure when it gives one (Claude Code),
otherwise PRICES below or the [pricing] table in your config. Which calls are money you
spent depends on how the agent is billed:
  api      an API key (billed per token): these add up to what a review cost you
  plan     a subscription login (Claude Code, Codex, Gemini CLI, Copilot): the call uses your
           plan's limits, not money
  local    a model on this machine or your own network (Ollama, LM Studio…): free
  unknown  anything else (a gateway, or a CLI Ixel doesn't know)
An agent's `billing` setting overrides the guess, e.g. billing = "api" for a Claude Code that's
signed in to a Console (pay-as-you-go) account, or for a paid proxy on your network.

Calls that failed or were cut off are left out unless the model reported their tokens.
"""
from __future__ import annotations

import ipaddress
import math
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable
from urllib.parse import urlparse

BILLING = ("api", "plan", "local", "unknown")
# The vendors' CLIs, signed in with the user's subscription (see presets.py)
PLAN_COMMANDS = frozenset({"claude", "codex", "gemini", "copilot"})
# OpenCode's names for model servers of your own (its docs' provider ids), as in model = "ollama/qwen3:8b"
LOCAL_OPENCODE_PROVIDERS = ("ollama/", "lmstudio/", "llama.cpp/", "llamacpp/")
# Ollama's cloud models (gpt-oss:120b-cloud, qwen3-coder:480b-cloud…) are listed beside the local ones but
# run on ollama.com, under your Ollama account
_OLLAMA_CLOUD = re.compile(r"[:-]cloud$", re.IGNORECASE)


def runs_elsewhere(model: str | None) -> bool:
    """True for a model a local server only passes on to the cloud (Ollama's -cloud models)."""
    return bool(_OLLAMA_CLOUD.search(str(model or "").strip()))
CHARS_PER_TOKEN = 4
# Addresses that are yours, not a provider's: private (RFC 1918), link-local, IPv6 unique-local, and
# carrier-grade NAT, which Tailscale uses. A model server there (an Ollama on another PC) is free.
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10", "fe80::/10", "fc00::/7"))
# (and Tailscale's own names, mac-mini.tail1234.ts.net)
_PRIVATE_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".ts.net")


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens."""
    input: float
    output: float
    cache_read: float | None = None   # default: a tenth of input
    cache_write: float | None = None  # default: 1.25 × input

    @property
    def read(self) -> float:
        return self.input * 0.1 if self.cache_read is None else self.cache_read

    @property
    def write(self) -> float:
        return self.input * 1.25 if self.cache_write is None else self.cache_write


# Anthropic's first-party API prices (also Microsoft Foundry), as of PRICES_AS_OF. Other
# providers change prices often and aren't listed: add yours under [pricing] in your config.
PRICES_AS_OF = "June 2026"
PRICES: dict[str, Price] = {
    "claude-fable-5-1": Price(10.0, 50.0, cache_read=0.25),
    "claude-fable-5": Price(10.0, 50.0),
    "claude-opus-5-5": Price(4.0, 20.0, cache_read=0.20),
    "claude-opus-5": Price(5.0, 25.0),
    "claude-opus-4-8": Price(5.0, 25.0),
    "claude-opus-4-7": Price(5.0, 25.0),
    "claude-opus-4-6": Price(5.0, 25.0),
    "claude-sonnet-5": Price(2.0, 10.0),
    "claude-sonnet-4-6": Price(3.0, 15.0),
    "claude-haiku-4-5": Price(1.0, 5.0),
}

# What can follow a model's name and still be the same model: a snapshot date
# (-20251001, Vertex's @20251001, OpenAI's -2026-01-01), a Bedrock version (-v1:0),
# a context-size tag ([1m])
_SNAPSHOT = re.compile(r"(?:[-@](?:\d{8}|\d{4}-\d{2}-\d{2}))?(?:-v\d+(?::\d+)?)?(?:\[[^\]]*\])?")


def normalize_model(model: str) -> str:
    """claude-haiku-4-5-20251001 and anthropic/claude-haiku-4-5 both name claude-haiku-4-5."""
    name = (model or "").strip().lower()
    for prefix in ("anthropic/", "anthropic.", "us.anthropic.", "eu.anthropic.", "global.anthropic."):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name


def price_for(model: str, pricing: dict[str, Price] | None = None) -> Price | None:
    """The price of a model: yours ([pricing]) first, then the built-in list. None if unknown."""
    name = normalize_model(model)
    if not name:
        return None
    for table in (pricing or {}, PRICES):
        # Longest name first, so claude-opus-5-5 isn't taken for claude-opus-5
        for key in sorted(table, key=len, reverse=True):
            if name.startswith(key) and _SNAPSHOT.fullmatch(name[len(key):]):
                return table[key]
    return None


def parse_pricing(config: dict[str, Any]) -> tuple[dict[str, Price], list[str]]:
    """
    The optional [pricing] table, in US dollars per million tokens:
        [pricing]
        "gpt-5.5" = { input = 1.25, output = 10.0 }
        "claude-opus-5-5" = { input = 4.0, output = 20.0, cache_read = 0.2 }
    """
    section = config.get("pricing", {})
    if not isinstance(section, dict):
        return {}, ["[pricing] must be a table of model = { input = …, output = … }"]
    prices, warnings = {}, []
    for model, entry in section.items():
        values = entry if isinstance(entry, dict) else {}
        numbers = {k: values.get(k) for k in ("input", "output", "cache_read", "cache_write") if k in values}
        ok = (isinstance(entry, dict) and "input" in numbers and "output" in numbers
              and set(values) <= set(numbers)
              and all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 10_000
                      for v in numbers.values()))
        if not ok:
            warnings.append(f"[pricing] {model!r}: needs input and output, in dollars per million tokens "
                            "(optionally cache_read and cache_write)")
            continue
        prices[normalize_model(model)] = Price(**{k: float(v) for k, v in numbers.items()})
    return prices, warnings


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text or "") / CHARS_PER_TOKEN)


def is_private_host(host: str | None) -> bool:
    """True if host is on your own network: a private address, or a name like nas.local or a bare `gpu-box`."""
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host or host.endswith(_PRIVATE_SUFFIXES)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address in network for network in _PRIVATE_NETWORKS)


def billing_for(agent: Any) -> str:
    """How an agent's calls are paid for (see the module docstring)."""
    cfg = getattr(agent, "config", None)
    if cfg is None:
        return "unknown"
    override = getattr(cfg, "billing", "")
    if override in BILLING:
        return override
    if cfg.type == "http":
        from ixel_mat.agents.base import is_loopback_host  # here: the agents import this module
        try:
            host = urlparse(cfg.url).hostname
        except ValueError:
            return "unknown"
        if is_loopback_host(host) or is_private_host(host):
            return "unknown" if runs_elsewhere(cfg.model) else "local"
        return "api"
    if cfg.type == "oneshot":
        name = os.path.basename((cfg.command or "").replace("\\", "/")).lower()
        name = re.sub(r"\.(exe|cmd|bat)$", "", name)
        if name == "opencode" and str(cfg.model or "").lower().startswith(LOCAL_OPENCODE_PROVIDERS) \
                and not runs_elsewhere(cfg.model):
            return "local"  # OpenCode asking your Ollama or LM Studio
        return "plan" if name in PLAN_COMMANDS else "unknown"
    return "unknown"


def configured_model(agent: Any) -> str:
    """The model an agent was set up with (an API agent's resolved one, once connected)."""
    return getattr(agent, "model", "") or getattr(getattr(agent, "config", None), "model", "") or ""


# ── What a call reported, and what it cost ────────────────────────────────────

@dataclass
class Usage:
    """What one model call reported about itself (transports pass this to on_usage)."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None  # the tool's own figure, at API prices (Claude Code gives one)
    model: str = ""                # the model that answered, when it said


OnUsage = Callable[[Usage], None]


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _dollars(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 1_000_000:
        return float(value)
    return None


def anthropic_usages(response: Any, model: str = "") -> list[Usage]:
    """
    A Messages API response's usage: one entry per model that worked on it. When a model
    declines and a fallback model answers, both attempts are billed, each at its own model's
    rates, and usage.iterations lists them (the top-level figures cover only the answer).
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return []
    answered = str(getattr(response, "model", "") or model)
    iterations = getattr(usage, "iterations", None) or []
    billed = [i for i in iterations if getattr(i, "type", "") in ("message", "fallback_message", "advisor_message")]
    return [Usage(input_tokens=_count(getattr(u, "input_tokens", 0)),
                  output_tokens=_count(getattr(u, "output_tokens", 0)),
                  cache_read_tokens=_count(getattr(u, "cache_read_input_tokens", 0)),
                  cache_write_tokens=_count(getattr(u, "cache_creation_input_tokens", 0)),
                  model=str(getattr(u, "model", "") or answered))
            for u in (billed or [usage])]


def openai_usage(data: Any, model: str = "") -> Usage | None:
    """The "usage" of an OpenAI-style chat completion (or of the last chunk of a streamed one)."""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None
    details = usage.get("prompt_tokens_details")
    cached = _count(details.get("cached_tokens")) if isinstance(details, dict) else 0
    prompt = _count(usage.get("prompt_tokens"))
    # Some providers (xAI; Gemini's OpenAI-style API) count reasoning outside completion_tokens
    # but bill it as output: the total says how much there was
    output = max(_count(usage.get("completion_tokens")), _count(usage.get("total_tokens")) - prompt)
    served = data.get("model")
    return Usage(input_tokens=max(prompt - cached, 0), output_tokens=output,
                 cache_read_tokens=min(cached, prompt), model=served if isinstance(served, str) and served else model)


def claude_code_usage(event: dict) -> Usage | None:
    """Claude Code's final "result" event: its token counts and its own cost figure."""
    usage = event.get("usage")
    by_model = event.get("modelUsage")
    if not isinstance(usage, dict) and "total_cost_usd" not in event:
        return None
    usage = usage if isinstance(usage, dict) else {}
    model = ""
    if isinstance(by_model, dict) and by_model:
        # The model that did the work: the one that cost the most
        def weight(item):
            info = item[1] if isinstance(item[1], dict) else {}
            return (_dollars(info.get("costUSD")) or 0.0, _count(info.get("outputTokens")))
        model = str(max(by_model.items(), key=weight)[0])
    return Usage(input_tokens=_count(usage.get("input_tokens")), output_tokens=_count(usage.get("output_tokens")),
                 cache_read_tokens=_count(usage.get("cache_read_input_tokens")),
                 cache_write_tokens=_count(usage.get("cache_creation_input_tokens")),
                 cost_usd=_dollars(event.get("total_cost_usd")), model=model)


def cost_at(price: Price | None, input_tokens: int, output_tokens: int,
            cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float | None:
    if price is None:
        return None
    return (input_tokens * price.input + output_tokens * price.output
            + cache_read_tokens * price.read + cache_write_tokens * price.write) / 1_000_000


@dataclass
class CallUsage:
    """One model call in a review: its tokens, and what it cost at API prices."""
    agent: str
    agent_label: str
    round: str
    tier: str                      # "panel" or "verifier"
    billing: str                   # one of BILLING
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated: bool = False        # counted from the text: the model didn't report its tokens
    cost_usd: float | None = None  # at API prices; None when no price is known

    def to_dict(self) -> dict:
        return asdict(self)


def call_usage(agent: Any, round_name: str, tier: str, prompt: str, reply: str | None,
               reported: list[Usage], pricing: dict[str, Price] | None = None) -> CallUsage | None:
    """What one call used: as reported, else counted from its text. None if it reported nothing and failed."""
    fields = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    configured = configured_model(agent)
    if reported:
        est = False
        tokens = [sum(getattr(u, f) for u in reported) for f in fields]
        # Each part at its own model's price (a declined attempt and the fallback that answered)
        costs = [u.cost_usd if u.cost_usd is not None
                 else cost_at(price_for(u.model or configured, pricing), *(getattr(u, f) for f in fields))
                 for u in reported]
        model = next((u.model for u in reversed(reported) if u.model), "") or configured
    elif reply is not None:
        est = True
        tokens = [estimate_tokens(prompt), estimate_tokens(reply), 0, 0]
        model = configured
        costs = [cost_at(price_for(model, pricing), *tokens)]
    else:
        return None
    billing = billing_for(agent)
    cost = 0.0 if billing == "local" else (None if any(c is None for c in costs) else sum(costs))
    return CallUsage(getattr(agent, "name", ""), getattr(agent, "label", ""), round_name, tier, billing, model,
                     *tokens, estimated=est, cost_usd=cost)


@dataclass
class UsageTotals:
    calls: int = 0
    input_tokens: int = 0            # everything the models read, cached or not
    output_tokens: int = 0
    estimated: bool = False          # some of the tokens were counted from the text
    api_usd: float = 0.0             # what the calls billed to an API key cost (where the price is known)
    api_calls: int = 0
    api_estimated: bool = False      # some of those were counted from the text, so api_usd is approximate
    unpriced_api_calls: int = 0      # billed to an API key, but no price known ([pricing] fixes that)
    plan_calls: int = 0
    local_calls: int = 0
    unknown_calls: int = 0

    def to_dict(self) -> dict:
        return {**asdict(self), "api_usd": round(self.api_usd, 6)}


def totals(calls: list[CallUsage]) -> UsageTotals:
    t = UsageTotals()
    for c in calls:
        t.calls += 1
        t.input_tokens += c.input_tokens + c.cache_read_tokens + c.cache_write_tokens
        t.output_tokens += c.output_tokens
        t.estimated = t.estimated or c.estimated
        if c.billing == "api":
            t.api_calls += 1
            t.api_estimated = t.api_estimated or c.estimated
            if c.cost_usd is None:
                t.unpriced_api_calls += 1
            else:
                t.api_usd += c.cost_usd
        else:
            setattr(t, f"{c.billing}_calls", getattr(t, f"{c.billing}_calls") + 1)
    return t


@dataclass
class Saving:
    """
    Saver mode: the answer the big model didn't have to write. When it confirmed a draft (or
    wasn't needed at all), it wrote a few words instead of that answer. Counted conservatively:
    everything the verifier wrote (its thinking too, where the API counts it) is subtracted,
    and the drafts it read to check them aren't credited back.
    """
    tokens: int                # output tokens the big model didn't write (about)
    usd: float | None          # those at its API price, when it's billed per token and the price is known
    billing: str
    verifier_label: str
    model: str = ""

    def to_dict(self) -> dict:
        return {**asdict(self), "usd": None if self.usd is None else round(self.usd, 6)}


def saver_saving(verifier: Any, accepted_text: str, calls: list[CallUsage],
                 pricing: dict[str, Price] | None = None) -> Saving:
    mine = [c for c in calls if c.tier == "verifier"]
    tokens = max(estimate_tokens(accepted_text) - sum(c.output_tokens for c in mine), 0)
    billing = billing_for(verifier)
    model = next((c.model for c in mine if c.model), "") or configured_model(verifier)
    price = price_for(model, pricing)
    usd = tokens * price.output / 1_000_000 if billing == "api" and price is not None else None
    return Saving(tokens, usd, billing, getattr(verifier, "label", ""), model)


# ── Words ─────────────────────────────────────────────────────────────────────

def money(usd: float) -> str:
    """$1.25, $0.42, $0.042, <$0.001"""
    if usd <= 0:
        return "$0"
    if usd < 0.001:
        return "<$0.001"
    if usd >= 0.1:
        return f"${usd:,.2f}"
    text = f"{usd:.3f}".rstrip("0")
    return "$" + (text if len(text.split(".")[1]) >= 2 else f"{usd:.2f}")


def tokens_text(count: int) -> str:
    return f"{count / 1000:.1f}k" if count >= 10_000 else f"{count:,}"


def cost_line(t: UsageTotals) -> str:
    """One line on what a review cost, e.g. "$0.042 on API keys · 3 calls on your subscriptions"."""
    parts = []
    if t.api_calls:
        priced = t.api_calls - t.unpriced_api_calls
        if not priced:
            parts.append(f"{t.api_calls} call{'s' if t.api_calls != 1 else ''} on API keys (price unknown)")
        else:
            unpriced = f" + {t.unpriced_api_calls} unpriced" if t.unpriced_api_calls else ""
            parts.append(f"{'≈ ' if t.api_estimated else ''}{money(t.api_usd)}{unpriced} on API keys")
    if t.plan_calls:
        parts.append(f"{t.plan_calls} call{'s' if t.plan_calls != 1 else ''} on your subscriptions")
    if t.local_calls:
        parts.append(f"{t.local_calls} free on your own hardware")
    if t.unknown_calls:
        parts.append(f"{t.unknown_calls} not priced")
    tokens = f"{tokens_text(t.input_tokens)} tokens in, {tokens_text(t.output_tokens)} out"
    return " · ".join(parts + [("about " if t.estimated else "") + tokens])


def saving_line(s: Saving) -> str:
    if s.tokens <= 0:
        return f"{s.verifier_label} wrote about as much checking the drafts as the answer itself would have taken."
    what = f"about {tokens_text(s.tokens)} tokens of {s.verifier_label}'s writing"
    if s.usd is not None:
        return f"Saved {what} (≈ {money(s.usd)})."
    if s.billing == "plan":
        return f"Saved {what}, out of your plan's limits."
    return f"Saved {what}."
