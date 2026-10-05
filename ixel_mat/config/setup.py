"""
Interactive setup wizard for Ixel MAT.
Guided onboarding experience for configuring providers and agents.

Run with: ixel setup  (or ixel configure)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import box

from ixel_mat import __version__, local_models
from ixel_mat.agents.launch import find_on_path
from ixel_mat.asking import Prompt, Confirm
from ixel_mat.config.secrets import (KeyStoreError, load_env, normalize_secret_input, save_secret, where_keys_are,
                                     write_private_file)
from ixel_mat.models import ALIASES, pick_latest, valid_model_id
from ixel_mat.presets import CLI_PRESETS  # noqa: F401 — the wizard offers these
from ixel_mat.sanitize import safe_markup

console = Console()

_CONFIG_DIR = Path.home() / ".config" / "ixel-mat"
_CONFIG_FILE = _CONFIG_DIR / "config.toml"
VERSION = __version__

# IxelAI color palette
C = {
    "bg":      "#070b14",
    "navy":    "#0d1b2a",
    "moon":    "#c8d8e8",
    "blue":    "#7eb8d4",
    "violet":  "#9b7fc7",
    "gold":    "#d4af37",
    "dim":     "#6b7d94",
    "green":   "#4ade80",
    "red":     "#e05252",
}


# ── Provider definitions ───────────────────────────────────────────────────────

PROVIDERS = [
    {
        "id":            "openclaw",
        "blurb":         "Only if you run an OpenClaw gateway; it routes to the models you set up there.",
        "name":          "OpenClaw Gateway",
        "env_name":      "IXELMAT_GATEWAY_TOKEN",
        "type":          "websocket",
        "url":           "ws://127.0.0.1:18789",
        "probe_url":     "http://127.0.0.1:18789/api/sessions",
        "probe_type":    "openclaw",
    },
    {
        "id":            "openai",
        "name":          "OpenAI",
        "env_name":      "OPENAI_API_KEY",
        "type":          "http",
        "url":           "https://api.openai.com/v1/chat/completions",
        "probe_url":     "https://api.openai.com/v1/models",
        "probe_type":    "openai",
    },
    {
        "id":            "anthropic",
        "name":          "Anthropic (Claude)",
        "env_name":      "ANTHROPIC_API_KEY",
        "type":          "http",
        "url":           "https://api.anthropic.com/v1/messages",
        "probe_url":     "https://api.anthropic.com/v1/models",
        "probe_type":    "anthropic",
    },
    {
        "id":            "xai",
        "name":          "xAI (Grok)",
        "env_name":      "XAI_API_KEY",
        "type":          "http",
        "url":           "https://api.x.ai/v1/chat/completions",
        "probe_url":     "https://api.x.ai/v1/models",
        "probe_type":    "openai",
    },
    {
        "id":            "gemini",
        "name":          "Google (Gemini)",
        "env_name":      "GOOGLE_API_KEY",
        "type":          "http",
        "url":           "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "probe_url":     "https://generativelanguage.googleapis.com/v1beta/models",
        "probe_type":    "google",
    },
]


# ── Validation probes ──────────────────────────────────────────────────────────

def _probe_openclaw(token: str, url: str = "http://127.0.0.1:18789/api/sessions") -> tuple[bool, str, list]:
    """Probe OpenClaw gateway. Returns (ok, message, sessions_list)."""
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with _urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            sessions = data if isinstance(data, list) else data.get("sessions", [])
            return True, f"connected ({len(sessions)} session(s))", sessions
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "invalid token (401 Unauthorized)", []
        return False, f"HTTP {e.code}", []
    except Exception as e:
        return False, f"could not connect — is OpenClaw running? ({e})", []


def _probe_openai_style(token: str, url: str) -> tuple[bool, str]:
    """Probe OpenAI-compatible API via the models endpoint."""
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with _urlopen(req, timeout=8):
            return True, "key valid"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "invalid key (401 Unauthorized)"
        if e.code == 403:
            return False, "forbidden (403) — check key permissions"
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"connection error: {e}"


def _probe_anthropic(token: str) -> tuple[bool, str]:
    """Probe Anthropic API."""
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": token, "anthropic-version": "2023-06-01"},
        )
        with _urlopen(req, timeout=8):
            return True, "key valid"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "invalid key (401 Unauthorized)"
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"connection error: {e}"


def _probe_google(token: str) -> tuple[bool, str]:
    """Probe Google Gemini API."""
    try:
        # Header, not ?key= — query strings end up in proxy logs and error messages
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": token},
        )
        with _urlopen(req, timeout=8):
            return True, "key valid"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "invalid key (auth failed)"
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"connection error: {e}"


def _validate_key(provider: dict, key: str) -> tuple[bool, str]:
    """Validate a key for a given provider. Returns (ok, message)."""
    pid = provider["id"]
    probe_type = provider.get("probe_type", "openai")

    if pid == "openclaw":
        ok, msg, _ = _probe_openclaw(key)
        return ok, msg
    elif probe_type == "anthropic":
        return _probe_anthropic(key)
    elif probe_type == "google":
        return _probe_google(key)
    else:
        return _probe_openai_style(key, provider.get("probe_url", ""))


def _mask_key(key: str) -> str:
    if not key:
        return "(empty)"
    if len(key) <= 8:
        return key[:4] + "..."
    return key[:8] + "..."


# ── Live model lists ───────────────────────────────────────────────────────────

_CHAT_MODEL_PREFIXES = {
    "openai":    ("gpt-", "o1", "o3", "o4", "chatgpt-"),
    "anthropic": ("claude-",),
    "xai":       ("grok",),
    "gemini":    ("gemini",),
}
_NOT_CHAT = ("embed", "tts", "audio", "realtime", "transcribe", "whisper", "image", "moderation",
             "search", "live", "aqa", "imagen", "veo", "computer-use", "codex")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """urllib re-sends every header on a redirect, keys included and to any host,
    even plain http://. Provider APIs don't redirect, so treat one as an error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)
# Plain http:// (a server on this computer or your network) never goes through a proxy: a key sent
# to it would reach the proxy unencrypted, and urllib proxies even 127.0.0.1 unless no_proxy says not to
_DIRECT_OPENER = urllib.request.build_opener(_NoRedirects, urllib.request.ProxyHandler({}))


def _urlopen(req: urllib.request.Request, timeout: float):
    return (_DIRECT_OPENER if req.type == "http" else _OPENER).open(req, timeout=timeout)


def _get_json(req: urllib.request.Request, timeout: float = 8.0) -> dict:
    with _urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(2_000_000).decode("utf-8"))


def list_models(provider: dict, key: str) -> list[str]:
    """
    Chat models this key can use, straight from the provider (newest first,
    roughly). Hardcoded lists go stale; this doesn't. [] if the call fails.
    """
    pid = provider["id"]
    try:
        if pid == "anthropic":
            data = _get_json(urllib.request.Request(
                "https://api.anthropic.com/v1/models?limit=100",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"}))
            ids = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
        elif pid == "gemini":
            data = _get_json(urllib.request.Request(
                "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
                headers={"x-goog-api-key": key}))
            ids = [m.get("name", "").removeprefix("models/") for m in data.get("models", [])
                   if isinstance(m, dict) and "generateContent" in (m.get("supportedGenerationMethods") or [])]
        elif provider.get("probe_type") == "openai" and provider.get("probe_url"):
            data = _get_json(urllib.request.Request(provider["probe_url"],
                                                    headers={"Authorization": f"Bearer {key}"}))
            ids = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
        else:
            return []
    except Exception:  # noqa: BLE001 — offline or unexpected shape: fall back to defaults
        return []
    prefixes = _CHAT_MODEL_PREFIXES.get(pid, ())
    ids = [i for i in ids if isinstance(i, str) and i.startswith(prefixes) and not any(x in i for x in _NOT_CHAT)]
    if pid != "anthropic":  # Anthropic already lists newest first
        ids.sort(reverse=True)
    return list(dict.fromkeys(ids))


def _explain_aliases(pid: str, models: list[str]) -> None:
    now = {alias: pick_latest(pid, models, alias) for alias in ALIASES} if models else {}
    parts = [f"{alias} = {now[alias]} today" if now.get(alias) else f"{alias} = the newest {kind}"
             for alias, kind in zip(ALIASES, ("top model", "fast, cheaper model"))]
    console.print(f"  [{C['dim']}]  {' · '.join(parts)}. Ixel re-checks every time it starts.[/]")


def _ask_model(models: list[str], default: str = "latest") -> str:
    while True:
        model = Prompt.ask(f"  [{C['moon']}]  Model[/] [{C['dim']}](latest, latest-fast, or a name)[/]",
                           default=default).strip() or default
        if model in ALIASES or valid_model_id(model):
            if models and model not in ALIASES and model not in models:
                console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(model)} isn't in your account's "
                              f"model list. Check the spelling.[/]")
            return model
        console.print(f"  [{C['red']}]✗[/] [{C['dim']}]That doesn't look like a model name.[/]")


# ── Command-line agents (use subscriptions you already pay for) ────────────────

# Flags verified against `claude --help` (Claude Code 2.1) and `codex exec --help`.
# Both run answer-only: no tools / read-only sandbox, in a fresh empty folder,
# with no MCP servers loaded (so a panel member can't call Ixel back and loop).
def _configure_cli_agents(taken_ids: set[str]) -> list[dict]:
    found = [p for p in CLI_PRESETS if find_on_path(p["command"])]
    if not found:
        return []
    console.print(f"  [{C['blue']}]━━ Command-line agents ━━[/]\n")
    console.print(f"  [{C['dim']}]These run the official CLIs you're already signed in to, answer-only:[/]")
    console.print(f"  [{C['dim']}]no tools, an empty temporary folder, and none of Ixel's API keys.[/]\n")
    agents = []
    for preset in found:
        if preset["id"] in taken_ids:
            continue
        if Confirm.ask(f"  [{C['moon']}]Add {preset['label']} to the panel?[/] "
                       f"[{C['dim']}]({preset['why']})[/]", default=True):
            # A reference, not a copy: when the preset is tightened, this agent gets it too
            agent = {"id": preset["id"], "preset": preset["id"], "type": "oneshot",
                     "label": preset["label"], "color": "white", "_asked": True}
            model = Prompt.ask(f"  [{C['moon']}]  Model[/] [{C['dim']}](Enter = {preset['label']}'s default, "
                               f"which it keeps current; or {preset['model_hint']})[/]", default="",
                               show_default=False).strip()
            while model and not valid_model_id(model):
                model = Prompt.ask(f"  [{C['red']}]  That doesn't look like a model name.[/] [{C['moon']}]Model[/]",
                                   default="", show_default=False).strip()
            if model:
                agent["model"] = model
            agents.append(agent)
    console.print()
    return agents


def _suggest_more_members(agents: list[dict]) -> None:
    """A panel of two has no majority when it disagrees; say how to add a third for free."""
    if len(agents) >= 3:
        return
    console.print(f"  [{C['blue']}]━━ Tip: a third panel member ━━[/]\n")
    if len(agents) == 1:
        console.print(f"  [{C['dim']}]With one model there's nobody to review its answer. Two can check each other,[/]")
        console.print(f"  [{C['dim']}]and three are best: when two disagree, the third settles it.[/]\n")
    else:
        console.print(f"  [{C['dim']}]With two models, a disagreement is a tie. A third member gives a majority.[/]\n")
    taken = {a.get("preset") or a.get("id") for a in agents}
    missing = [p for p in CLI_PRESETS if p["id"] not in taken and not find_on_path(p["command"])]
    free = [(p["label"], f"{p['free'][0].upper()}{p['free'][1:]}. Install: {p['install']}, "
                         f"{p.get('free_then') or 'then run ' + p['command'] + ' once to sign in'}.")
            for p in missing if p.get("free")]
    if not find_on_path("ollama"):
        free.append(("Ollama", "Free open models that run on this computer: https://ollama.com"))
    elif not any(a.get("url", "").startswith(LOCAL_SERVERS[0][1]) for a in agents):
        free.append(("Ollama", "Installed, but no model is running. Pull one (ollama pull <model>, "
                               "see https://ollama.com/library) and start it."))
    paid = [(p["label"], p["install"]) for p in missing if not p.get("free")]
    for heading, rows in (("These cost nothing:", free), ("Paying for one of these already? It can join too:", paid)):
        if not rows:
            continue
        console.print(f"  [{C['dim']}]{heading}[/]")
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style=C["moon"], no_wrap=True)
        grid.add_column(style=C["dim"])
        for label, how in rows:
            grid.add_row(f"    {label}", safe_markup(how))
        console.print(grid)
        console.print()
    console.print(f"  [{C['dim']}]Then run[/] [{C['blue']}]ixel setup[/] [{C['dim']}]again to add it.[/]\n")


# ── Local model servers (Ollama, LM Studio, …) ─────────────────────────────────

# Each server at its own port on this computer (Ollama first: the tip below names it)
LOCAL_SERVERS = [(name, f"http://127.0.0.1:{port}/v1") for name, port in local_models.KNOWN_SERVERS]


def detect_local_servers(timeout: float = 1.5) -> list[tuple[str, str, list[str]]]:
    """(name, base_url, models) for each local OpenAI-compatible server that answers with a chat model."""
    return [(s.name, s.base, s.models) for s in local_models.look(LOCAL_SERVERS, timeout) if s.models]


def _agent_id(text: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "local"
    agent_id, n = base, 2
    while agent_id in taken:
        agent_id, n = f"{base}_{n}", n + 1
    taken.add(agent_id)
    return agent_id


def _add_server_models(name: str, base: str, models: list[str], where: str, agents: list[dict],
                       taken: set[str]) -> None:
    shown = ", ".join(models[:10]) + (f" … ({len(models)} in total)" if len(models) > 10 else "")
    console.print(f"  [{C['green']}]✓[/] [{C['moon']}]{safe_markup(name)}[/] [{C['dim']}]is running: "
                  f"{safe_markup(shown)}[/]")
    while Confirm.ask(f"  [{C['moon']}]Add a{'nother' if any(a['url'].startswith(base) for a in agents) else ''} "
                      f"{safe_markup(name)} model to the panel?[/]", default=not agents):
        model = Prompt.ask(f"  [{C['moon']}]  Model[/]", choices=models, default=models[0], show_choices=False)
        agents.append({
            "id": _agent_id(model, taken), "type": "http", "url": f"{base}/chat/completions",
            "model": model, "label": f"{model} ({where})", "color": "yellow", "_asked": True,
        })


def _configure_local_agents(taken_ids: set[str]) -> list[dict]:
    console.print(f"  [{C['blue']}]━━ Local models ━━[/]\n")
    console.print(f"  [{C['dim']}]These run on your own computers: no API key, no usage limits, and questions[/]")
    console.print(f"  [{C['dim']}]stay with you. Ollama, LM Studio and other OpenAI-compatible servers work.[/]\n")
    agents: list[dict] = []
    taken = set(taken_ids)
    servers = detect_local_servers()
    if not servers:
        console.print(f"  [{C['dim']}]No model server with a chat model is running on this computer.[/]")
    for name, base, models in servers:
        _add_server_models(name, base, models, "local", agents, taken)
    while Confirm.ask(f"  [{C['moon']}]Use models on another of your computers?[/] "
                      f"[{C['dim']}](like LM Studio on your Mac)[/]", default=False):
        address = Prompt.ask(f"  [{C['moon']}]  Its name or address[/] [{C['dim']}](like mac-mini or "
                             f"192.168.1.20:1234)[/]", default="", show_default=False).strip()
        if not address:
            break
        console.print(f"  [{C['dim']}]Looking...[/]", end="")
        try:
            found = local_models.at_address(address)
        except local_models.AddressError as exc:
            console.print(f"\r  [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(str(exc))}[/]")
            continue
        console.print("\r", end="")
        for server in found:
            host = urlparse(server.base).hostname or address
            if server.models:
                _add_server_models(f"{server.name} on {host}", server.base, server.models, host, agents, taken)
            elif server.elsewhere:
                console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(server.name)} on {safe_markup(host)} "
                              f"has only models that run on ollama.com ({safe_markup(', '.join(server.elsewhere))}), "
                              "and Ixel leaves those out so questions stay on your computers.[/]")
            else:
                console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(server.name)} on "
                              f"{safe_markup(host)} has no model that answers questions yet.[/]")
    console.print()
    return agents


def _configure_saver(agents: list[dict]) -> dict | None:
    if len(agents) < 2:
        return None  # not asked: running setup again keeps what the file says
    console.print(f"  [{C['blue']}]━━ Usage saver ━━[/]\n")
    console.print(f"  [{C['dim']}]/saver lets your cheaper models do the work and has one big model check it,[/]")
    console.print(f"  [{C['dim']}]so your expensive usage lasts longer. Pick the big model; the rest draft.[/]")
    ids = [a["id"] for a in agents]
    verifier = Prompt.ask(f"  [{C['moon']}]Big model that verifies[/] [{C['dim']}](or skip)[/]",
                          choices=["skip", *ids], default="skip")
    if verifier == "skip":
        console.print()
        return {}
    escalate = Prompt.ask(
        f"  [{C['moon']}]Call it even when every draft is rated correct?[/] "
        f"[{C['dim']}](always = safest; disagreement = saves the most)[/]",
        choices=["always", "disagreement"], default="always")
    effort = Prompt.ask(f"  [{C['moon']}]How hard should it think when verifying?[/]",
                        choices=["low", "medium", "high"], default="low")
    console.print()
    return {"verifier": verifier, "escalate": escalate, "verifier_effort": effort}


def _check_typesafe_key(key: str, url: str = "https://api.typesafe.ai/v1/models") -> tuple[bool, str]:
    """A key check that sends no questions: list the models the key can use."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json",
                                               "User-Agent": f"ixel-mat/{VERSION}"})
    try:
        data = _get_json(req)
    except urllib.error.HTTPError as e:
        return False, "TypeSafe refused the key (HTTP 401)" if e.code == 401 else f"TypeSafe answered HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 — offline, DNS, TLS…
        return False, f"couldn't reach TypeSafe ({type(e).__name__})"
    names = [m.get("name") for m in data.get("models", []) if isinstance(m, dict)] if isinstance(data, dict) else []
    return True, f"key works ({', '.join(n for n in names if isinstance(n, str))[:80] or 'decision models'})"


def _configure_triage(agents: list[dict], review: dict | None) -> dict | None:
    """Optional: triage picks modes and skips checks the panel doesn't need. Off by default."""
    if len(agents) < 2 or review is None:
        return None  # not asked
    console.print(f"  [{C['blue']}]━━ Triage (optional) ━━[/]\n")
    console.print(f"  [{C['dim']}]Triage makes quick decisions between rounds: it picks Quick, Review or Deep for[/]")
    console.print(f"  [{C['dim']}]each question, and can skip checks the panel doesn't need. One of your own models[/]")
    console.print(f"  [{C['dim']}]can make the decisions (a fast, cheap one is best), or TypeSafe AI's decision API[/]")
    console.print(f"  [{C['dim']}](calibrated, about a second; needs their key, and sends them your questions).[/]")
    if not Confirm.ask(f"  [{C['moon']}]Use triage?[/]", default=False):
        console.print()
        return {}
    provider = Prompt.ask(f"  [{C['moon']}]Who decides?[/] [{C['dim']}](model = one of yours, no extra account)[/]",
                          choices=["model", "typesafe"], default="model")
    triage: dict = {"enabled": True, "provider": provider}
    if provider == "model":
        ids = [a["id"] for a in agents]
        triage["agent"] = Prompt.ask(f"  [{C['moon']}]Which model decides?[/] [{C['dim']}](your fastest, cheapest one)[/]",
                                     choices=ids, default=ids[-1])
    elif not _typesafe_key():
        return {}
    if Confirm.ask(f"  [{C['moon']}]Let triage pick the mode for each question by default?[/]", default=True):
        review["mode"] = "auto"
    triage["skip_review"] = Confirm.ask(
        f"  [{C['moon']}]Skip peer review when every answer already agrees?[/] "
        f"[{C['dim']}](faster and cheaper; the verdict still sees every answer)[/]", default=False)
    console.print()
    return triage


def _typesafe_key() -> bool:
    """Save a TypeSafe key (checked without sending any question). False: triage stays off."""
    console.print(f"  [{C['dim']}]Get the key only from typesafe.ai: lookalike sites that resell it would see everything.[/]")
    key = os.environ.get("TYPESAFE_API_KEY", "")
    if key and Confirm.ask(f"  [{C['moon']}]Use the TYPESAFE_API_KEY you already have?[/]", default=True):
        return True
    console.print(f"  [{C['dim']}]Enter your TypeSafe key (input is hidden — paste, then press Enter):[/]")
    key = normalize_secret_input(Prompt.ask(f"  [{C['moon']}]TYPESAFE_API_KEY[/]", password=True))
    if not key:
        console.print(f"  [{C['dim']}]No key entered — triage stays off.[/]\n")
        return False
    console.print(f"  [{C['dim']}]Checking...[/]", end="")
    ok, msg = _check_typesafe_key(key)
    console.print(f"\r  [{C['green'] if ok else C['gold']}]{'✓' if ok else '⚠'}[/] [{C['dim']}]{safe_markup(msg)}[/]")
    if not ok and not Confirm.ask(f"  [{C['dim']}]Save it anyway?[/]", default=False):
        console.print(f"  [{C['dim']}]Triage stays off.[/]\n")
        return False
    _save_key("TYPESAFE_API_KEY", key)
    return True


def _save_key(env_name: str, key: str) -> None:
    """Save a key, and use it for the rest of setup. When the keychain can't be opened, setup says so
    and still uses the key until it ends (it's never written as plain text instead)."""
    try:
        save_secret(env_name, key)
    except KeyStoreError as exc:
        console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(exc)} Until then, setup uses the key "
                      f"without saving it.[/]")
    os.environ[env_name] = key


def _drop_saver_without_verifier(review: dict | None, saver: dict | None) -> None:
    """Saver mode needs a verifier: [saver] verifier, else the moderator. With neither, don't save
    plain_questions = "saver" (every typed question would fail)."""
    if review and review.get("plain_questions") == "saver" and not (saver or {}).get("verifier") \
            and not review.get("moderator"):
        del review["plain_questions"]
        console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]No verifier, so a typed question gets a quick review "
                      f"instead of saver mode. Run ixel setup again to pick one.[/]\n")


def _configure_review(agents: list[dict]) -> dict | None:
    if len(agents) < 2:
        return None  # not asked: running setup again keeps what the file says
    console.print(f"  [{C['blue']}]━━ Review defaults ━━[/]\n")
    console.print(f"  [{C['dim']}]quick = answers + verdict · review = + anonymous peer review · deep = + revisions[/]")
    mode = Prompt.ask(f"  [{C['moon']}]Default /review mode[/]", choices=["quick", "review", "deep"], default="review")
    plain = Prompt.ask(f"  [{C['moon']}]When you just type a question[/] "
                       f"[{C['dim']}](compare = every answer side by side, no review)[/]",
                       choices=["quick", "review", "deep", "saver", "compare"], default="quick")
    ids = [a["id"] for a in agents]
    moderator = Prompt.ask(f"  [{C['moon']}]Who writes the verdict?[/] [{C['dim']}](auto = best-rated answer's author)[/]",
                           choices=["auto", *ids], default="auto")
    console.print()
    review = {"mode": mode}
    if plain != "quick":
        review["plain_questions"] = plain
    if moderator != "auto":
        review["moderator"] = moderator
    return review


# ── Status detection ───────────────────────────────────────────────────────────

def _detect_status() -> dict[str, dict]:
    """Detect which providers are already configured (set in your environment, or saved in Ixel)."""
    load_env()
    result = {}
    for p in PROVIDERS:
        env_name = p["env_name"]
        key = os.getenv(env_name, "")
        result[p["id"]] = {
            "configured": bool(key),
            "key": key,
            "masked": _mask_key(key) if key else "",
        }
    return result


# ── Welcome screen ─────────────────────────────────────────────────────────────

def _print_welcome(status: dict[str, dict]) -> None:
    """Print branded welcome screen with current provider status table."""
    store = where_keys_are()
    console.print()

    # ASCII brand block
    console.print(
        f"[{C['gold']}]"
        "  ██╗██╗  ██╗███████╗██╗      \n"
        "  ██║╚██╗██╔╝██╔════╝██║      \n"
        "  ██║ ╚███╔╝ █████╗  ██║      \n"
        "  ██║ ██╔██╗ ██╔══╝  ██║      \n"
        "  ██║██╔╝ ██╗███████╗███████╗ \n"
        f"  ╚═╝╚═╝  ╚═╝╚══════╝╚══════╝[/] "
        f"[{C['moon']}]M A T[/]  [{C['dim']}]v{VERSION}[/]"
    )
    console.print()

    console.print(Panel(
        f"[{C['moon']}]Welcome to the Ixel MAT Setup Wizard[/]\n"
        f"[{C['dim']}]Configure your AI providers and agents for multi-agent comparison.[/]\n"
        f"[{C['dim']}]{safe_markup(store.summary)}[/]",
        border_style=C["violet"],
        padding=(0, 2),
    ))
    if store.problem:
        console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(store.problem)}[/]")
    console.print()

    # Status table
    table = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style=f"bold {C['moon']}",
        border_style=C["dim"],
        padding=(0, 1),
    )
    table.add_column("Provider",    style=C["blue"],   min_width=22)
    table.add_column("Status",                         min_width=16)
    table.add_column("Auth",        style=C["dim"],    min_width=14)

    for p in PROVIDERS:
        s = status[p["id"]]
        if s["configured"]:
            status_str = f"[{C['green']}]✓ configured[/]"
            auth_str   = s["masked"]
        else:
            status_str = f"[{C['dim']}]○ not set[/]"
            auth_str   = "—"
        table.add_row(p["name"], status_str, auth_str)

    console.print("  [bold]Current Status[/]")
    console.print(table)
    console.print(f"  [{C['dim']}]Models come live from each provider once you add a key, so new ones show up without")
    console.print(f"  [{C['dim']}]updating Ixel. Choose [/][{C['moon']}]latest[/][{C['dim']}] to always use the newest.[/]")
    console.print()


# ── Provider setup ─────────────────────────────────────────────────────────────

def _setup_provider(provider: dict, existing_status: dict) -> Optional[str]:
    """
    Interactive setup for one provider.
    Returns the saved key/token string, or None if skipped.
    """
    pid      = provider["id"]
    name     = provider["name"]
    env_name = provider["env_name"]
    s        = existing_status[pid]

    # Section header
    console.print(f"  [{C['blue']}]━━ {name} ━━[/]")
    if provider.get("blurb"):
        console.print(f"  [{C['dim']}]{provider['blurb']}[/]")
    console.print()

    if s["configured"]:
        console.print(f"  [{C['green']}]✓[/] [{C['dim']}]Key already set:[/] [{C['moon']}]{s['masked']}[/]")
        keep = Confirm.ask(f"  [{C['moon']}]Keep existing key?[/]", default=True)
        if keep:
            console.print(f"  [{C['dim']}]Keeping existing key.[/]\n")
            return s["key"]
        # Replace flow
        console.print(f"  [{C['dim']}]Enter replacement key (input is hidden — paste, then press Enter):[/]")
        new_key = Prompt.ask(f"  [{C['moon']}]{env_name}[/]", password=True)
        if not new_key.strip():
            console.print(f"  [{C['dim']}]No key entered — keeping existing.[/]\n")
            return s["key"]
        key = normalize_secret_input(new_key)
    else:
        if pid == "openclaw":
            console.print(
                f"  [{C['dim']}]Find your token at:[/] "
                f"[{C['blue']}]http://127.0.0.1:18789[/] "
                f"[{C['dim']}]→ Settings → Auth Token[/]"
            )
        want = Confirm.ask(f"  [{C['moon']}]Configure {name}?[/]", default=False)
        if not want:
            console.print(f"  [{C['dim']}]Skipped.[/]\n")
            return None
        console.print(f"  [{C['dim']}]Enter key (input is hidden — paste, then press Enter):[/]")
        key = Prompt.ask(f"  [{C['moon']}]{env_name}[/]", password=True)
        if not key.strip():
            tip = " (In Windows PowerShell, right-click pastes.)" if os.name == "nt" else ""
            console.print(f"  [{C['dim']}]No key entered — skipped.{tip}[/]\n")
            return None
        key = normalize_secret_input(key)

    # Validate key live
    console.print(f"  [{C['dim']}]Validating...[/]", end="")
    ok, msg = _validate_key(provider, key)
    if ok:
        console.print(f"\r  [{C['green']}]✓[/] [{C['dim']}]{msg}[/]")
        _save_key(env_name, key)
    else:
        console.print(f"\r  [{C['gold']}]⚠[/] [{C['dim']}]{msg}[/]")
        save_anyway = Confirm.ask(f"  [{C['dim']}]Save key anyway?[/]", default=False)
        if save_anyway:
            _save_key(env_name, key)
        else:
            console.print(f"  [{C['dim']}]Key not saved.[/]\n")
            return None

    console.print()
    return key


# ── OpenClaw agent auto-detection ──────────────────────────────────────────────

def _detect_openclaw_sessions(token: str) -> list[str]:
    """Return list of session keys discovered on the gateway."""
    ok, _, sessions = _probe_openclaw(token)
    if not ok or not sessions:
        return []
    keys = []
    for s in sessions:
        if isinstance(s, dict):
            key = s.get("key") or s.get("session_key") or s.get("id", "")
        else:
            key = str(s)
        if key and key.startswith("agent:"):
            keys.append(key)
    return keys


# ── Agent configuration ────────────────────────────────────────────────────────

_HTTP_COLORS = {
    "openai":    "green",
    "anthropic": "blue",
    "xai":       "magenta",
    "gemini":    "red",
}


def _configure_agents(provider_keys: dict[str, Optional[str]]) -> list[dict]:
    """
    Walk through configured providers and build agent config entries.
    Returns list of dicts ready for TOML serialisation.
    """
    agents: list[dict] = []

    console.print(f"  [{C['blue']}]━━ Agent Configuration ━━[/]\n")
    console.print(
        f"  [{C['dim']}]We'll create one agent per configured provider.[/]\n"
        f"  [{C['dim']}]Press Enter to accept defaults.[/]\n"
    )

    # ── OpenClaw WebSocket agents ──────────────────────────────────────────
    gw_token = provider_keys.get("openclaw")
    if gw_token:
        console.print(f"  [{C['dim']}]Detecting active sessions on OpenClaw gateway...[/]")
        session_keys = _detect_openclaw_sessions(gw_token)

        if session_keys:
            console.print(
                f"  [{C['green']}]✓[/] [{C['dim']}]Found {len(session_keys)} session(s)[/]\n"
            )
            used_ids: set[str] = set()
            for sk in session_keys:
                parts   = sk.split(":")           # e.g. ["agent", "main", "main"]
                name_part = parts[1] if len(parts) > 1 else "agent"
                default_label = f"{name_part.title()} (OpenClaw)"
                agent_id = name_part.lower().replace("-", "_")
                # Deduplicate IDs
                if agent_id in used_ids:
                    agent_id = f"{agent_id}_{len(used_ids)}"
                used_ids.add(agent_id)

                console.print(f"  [{C['blue']}]Session:[/] [{C['moon']}]{sk}[/]")
                label = Prompt.ask(f"  [{C['moon']}]  Label[/]", default=default_label)
                agents.append({
                    "id":          agent_id,
                    "type":        "websocket",
                    "url":         "ws://127.0.0.1:18789",
                    "token_env":   "IXELMAT_GATEWAY_TOKEN",
                    "session_key": sk,
                    "label":       label.strip() or default_label,
                    "color":       "cyan",
                })
                console.print()
        else:
            console.print(
                f"  [{C['gold']}]⚠[/] [{C['dim']}]No active sessions found — configuring a default agent.[/]\n"
            )
            label = Prompt.ask(
                f"  [{C['moon']}]OpenClaw agent label[/]",
                default="OpenClaw Agent",
            )
            session_key = Prompt.ask(
                f"  [{C['moon']}]Session key[/]",
                default="agent:main:main",
            )
            agents.append({
                "id":          "openclaw",
                "type":        "websocket",
                "url":         "ws://127.0.0.1:18789",
                "token_env":   "IXELMAT_GATEWAY_TOKEN",
                "session_key": session_key,
                "label":       label.strip() or "OpenClaw Agent",
                "color":       "cyan",
            })
            console.print()

    # ── HTTP agents (one per API provider) ────────────────────────────────
    for p in PROVIDERS:
        if p["type"] != "http":
            continue
        key = provider_keys.get(p["id"])
        if not key:
            continue

        pid           = p["id"]
        default_label = p["name"]
        models        = list_models(p, key)

        console.print(f"  [{C['blue']}]Provider:[/] [{C['moon']}]{p['name']}[/]")
        if models:
            shown = ", ".join(models[:10]) + (f" … ({len(models)} in total)" if len(models) > 10 else "")
            console.print(f"  [{C['dim']}]  Models on your account: {safe_markup(shown)}[/]")
        _explain_aliases(pid, models)
        label = Prompt.ask(f"  [{C['moon']}]  Agent label[/]", default=default_label)
        model = _ask_model(models)
        agents.append({
            "id":        pid,
            "type":      "http",
            "url":       p["url"],
            "token_env": p["env_name"],
            "model":     model,
            "label":     label.strip() or default_label,
            "color":     _HTTP_COLORS.get(pid, "white"),
        })
        console.print()

    return agents


# ── TOML builder ───────────────────────────────────────────────────────────────

_BARE_TOML_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _toml_str(value: str) -> str:
    """Quote a value as a TOML basic string (JSON escapes are valid TOML; DEL, which JSON leaves as it is,
    isn't allowed raw)."""
    return json.dumps(str(value), ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml_key(value: str) -> str:
    return value if _BARE_TOML_KEY.fullmatch(value) else _toml_str(value)


def _toml_any(value: Any) -> str:
    """Any value a settings file can hold, as TOML (tables inline)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _toml_str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_any(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(str(k))} = {_toml_any(v)}" for k, v in value.items()) + " }" \
            if value else "{}"
    if hasattr(value, "isoformat"):  # a date or time
        return value.isoformat()
    raise ValueError(f"can't write {type(value).__name__} values")


def _table_lines(path: tuple[str, ...], table: dict) -> list[str]:
    """[path] with its values, and each table in it under a header of its own (so Settings can edit it)."""
    plain = [(k, v) for k, v in table.items() if not isinstance(v, dict)]
    inner = [(k, v) for k, v in table.items() if isinstance(v, dict)]
    lines = []
    if plain or not inner:
        lines.append("[" + ".".join(_toml_key(p) for p in path) + "]")
        lines += [f"{_toml_key(k)} = {_toml_any(v)}" for k, v in plain]
        lines.append("")
    for k, v in inner:
        lines += _table_lines((*path, k), v)
    return lines


# The order an agent's settings are written in; any others follow
_AGENT_ORDER = ("preset", "type", "url", "token_env", "command", "args", "args_by_version", "prompt_via",
                "output_flag", "workdir", "drop_env", "effort_args", "effort_levels", "model_args", "env", "timeout",
                "session_key", "model", "effort", "label", "color")
# What the wizard asks in each table. The rest of a table (set in Settings, or by hand) is kept.
_ASKED = {"review": ("mode", "plain_questions", "moderator"),
          "saver": ("verifier", "escalate", "verifier_effort"),
          "triage": ("enabled", "provider", "agent", "skip_review")}
# Lists of agents in a table: an agent that's gone is taken out of them
_AGENT_LISTS = {"review": "agents", "saver": "drafters"}


def _agent_lines(a: dict) -> list[str]:
    # Labels are typed by the user and session keys come from the gateway,
    # so nothing is interpolated raw: a stray quote must not break the file
    # or smuggle in extra keys (e.g. a subprocess command).
    lines = [f"[agents.{_toml_key(a['id'])}]"]
    kept = a.get("_kept", set())  # as the file had them: an empty one too (accepts = [] is pictures off)
    for key in [*(k for k in _AGENT_ORDER if k in a), *(k for k in a if k not in _AGENT_ORDER)]:
        if key in ("id", "_asked", "_kept"):
            continue
        value = a[key]
        if value is None or (key not in kept and ((key != "args" and (value == "" or value == [] or value == {}))
                                                  or (key == "timeout" and not value))):
            continue
        if key == "timeout" and isinstance(value, float) and value.is_integer():
            value = int(value)
        lines.append(f"{_toml_key(key)} = {_toml_any(value)}")
    lines.append("")
    return lines


def _section(name: str, asked: dict | None, existing: dict | None, ids: list[str], new_ids: set[str]) -> dict:
    """[name] as this run leaves it: the wizard's answers (None when it didn't ask, as with one model), and
    what else the table had (agents that are gone taken out of it, and the agents set up for the first time
    put on the panel)."""
    old = existing.get(name) if existing and isinstance(existing.get(name), dict) else {}
    if asked is None:  # not asked: as the file had it, less any model that's gone
        out = {k: v for k, v in old.items() if not (k in ("moderator", "verifier", "agent") and v not in ids)}
        if name == "triage" and "agent" in old and "agent" not in out:
            out["enabled"] = False  # the model that decided is gone
    else:
        out = dict(asked)
        if name == "triage" and old and not asked:
            out["enabled"] = False  # you said no to triage this time
        out.update((k, v) for k, v in old.items() if k not in _ASKED.get(name, ()))
    listed = _AGENT_LISTS.get(name)
    if listed and isinstance(out.get(listed), list):
        kept = [n for n in out[listed] if n in ids]
        if name == "review":
            kept += [n for n in ids if n in new_ids and n not in kept]
        if kept:
            out[listed] = kept
        else:
            del out[listed]
    return out


def _build_toml(agents: list[dict], review: dict | None = None, saver: dict | None = None,
                triage: dict | None = None, existing: dict | None = None) -> str:
    """
    The settings file for this run's answers. existing is the file it replaces: whatever in it the wizard
    doesn't ask about (other tables, and settings made in the app or by hand) is written back too.
    """
    lines = [
        "# Ixel MAT — Agent Configuration",
        "# Generated by `ixel setup` wizard",
        "# Keys never go in this file: ixel setup and the app's Settings save them for you,",
        "# encrypted with a key kept in your system's keychain. Without a keychain Ixel can use (or",
        "# with one that won't keep that key), they go in .env, as plain text.",
        "",
    ]
    existing = existing or {}
    others = {k: v for k, v in existing.items() if k not in ("agents", "review", "saver", "triage", "_source", "_error")}
    for key, value in others.items():  # plain settings at the top of the file come before any table
        if not isinstance(value, dict):
            lines.append(f"{_toml_key(key)} = {_toml_any(value)}")
    if any(not isinstance(v, dict) for v in others.values()):
        lines.append("")
    for a in agents:
        lines += _agent_lines(a)
    old_agents = existing.get("agents") if isinstance(existing.get("agents"), dict) else {}
    ids = [a["id"] for a in agents]
    # New to the file, or added "to the panel" in this run: on the panel
    new_ids = {a["id"] for a in agents if a["id"] not in old_agents or a.get("_asked")}
    review = _section("review", review, existing, ids, new_ids)
    saver = _section("saver", saver, existing, ids, new_ids)
    triage = _section("triage", triage, existing, ids, new_ids)
    if review:
        lines += _table_lines(("review",), review)
    if saver:
        lines += _table_lines(("saver",), saver)
    if triage:
        if triage.get("enabled") and triage.get("provider", "typesafe") == "typesafe":
            lines += ["# Triage: quick decisions between rounds, by TypeSafe AI's decision API. Your questions",
                      "# and the models' answers go to TypeSafe too. The key is TYPESAFE_API_KEY, saved with the others."]
        elif triage.get("enabled"):
            lines.append("# Triage: quick decisions between rounds, made by one of your own models.")
        triage.setdefault("enabled", False)
        if triage.get("enabled"):
            triage.setdefault("provider", "typesafe")
            triage.setdefault("skip_review", False)
        order = ("enabled", "provider", "agent", "skip_review")
        lines += _table_lines(("triage",), {**{k: triage[k] for k in order if k in triage},
                                            **{k: v for k, v in triage.items() if k not in order}})
    for key, value in others.items():
        if isinstance(value, dict):
            lines += _table_lines((key,), value)
    return "\n".join(lines)


def _existing_config() -> dict | None:
    """The settings file this run replaces, or None when there's none. One that can't be read is copied
    aside first (config.toml.bak is replaced on the next run), and setup says so."""
    import time
    from ixel_mat.config.loader import tomllib
    from ixel_mat.config.secrets import read_text_file
    if not _CONFIG_FILE.exists() or tomllib is None:
        return None
    try:
        data = tomllib.loads(read_text_file(_CONFIG_FILE))
    except Exception as exc:  # noqa: BLE001 — not valid TOML
        aside = _CONFIG_FILE.with_name(f"config.toml.unreadable-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            shutil.copy2(_CONFIG_FILE, aside)
            copied = f"It's copied to {aside}."
        except OSError:
            copied = "Ixel couldn't copy it either, so check it before saying yes below."
        console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]Your settings file can't be read "
                      f"({safe_markup(str(exc))[:200]}), so nothing in it is kept. {safe_markup(copied)}[/]")
        return None
    return data if isinstance(data, dict) else None


def _where(agent: dict) -> str:
    """An agent's address as setup compares them (localhost is 127.0.0.1)."""
    url = local_models.chat_url(str(agent.get("url", ""))).lower()
    return url.replace("://localhost:", "://127.0.0.1:").replace("://localhost/", "://127.0.0.1/")


def _kind(agent: dict) -> tuple:
    """What kind of agent, and where: the same program, or a server at the same address."""
    agent_type = agent.get("type", "oneshot" if "preset" in agent else "websocket")
    return agent_type, agent.get("preset"), _where(agent)


def _carry_over(agents: list[dict], existing: dict | None) -> None:
    """
    An agent set up again keeps its name and what it had that the wizard doesn't ask (effort, pictures,
    billing…, and the label and color when the wizard made them up): the same model at the same address, else
    the same kind of agent at the same address under the same name (its model changed). One that only shares
    a name with an agent in the file is given a new name, so neither replaces the other.
    """
    old_agents = (existing or {}).get("agents")
    if not isinstance(old_agents, dict):
        return
    old = {name: before for name, before in old_agents.items() if isinstance(before, dict)}
    claimed: dict[int, str] = {}

    def same_model(name: str, a: dict) -> bool:
        model = str(a.get("model") or "")
        return bool(model) and _kind(old[name]) == _kind(a) and str(old[name].get("model") or "") == model

    # The same model at the same address: under the same name first, then whatever this run called it
    for i, a in enumerate(agents):
        if a["id"] in old and same_model(a["id"], a):
            claimed[i] = a["id"]
    for i, a in enumerate(agents):
        if i not in claimed:
            name = next((n for n in old if n not in claimed.values() and same_model(n, a)), None)
            if name is not None:
                claimed[i] = name
    used = set(claimed.values())
    for i, a in enumerate(agents):
        if i in claimed:
            a["id"] = claimed[i]
        elif a["id"] in old and a["id"] not in used and _kind(old[a["id"]]) == _kind(a):
            claimed[i] = a["id"]
        elif a["id"] in old or a["id"] in used:
            a["id"] = _agent_id(a["id"], set(old_agents) | used | {b["id"] for b in agents})
        used.add(a["id"])
    for i, name in claimed.items():
        a, kept = agents[i], set()
        for key, value in old[name].items():
            # The wizard asked for the model (no answer means the default); a label only for API models
            if key != "model" and (key not in a or (key in ("label", "color") and (a.get("_asked") or key == "color"))):
                a[key] = value
                kept.add(key)
        a["_kept"] = kept


def _keep_others(agents: list[dict], existing: dict | None) -> list[dict]:
    """Models in the settings file that this run didn't set up (added by hand or in the app, or skipped this
    time). Kept unless you say not to."""
    old_agents = (existing or {}).get("agents")
    if not isinstance(old_agents, dict):
        return []
    ids = {a["id"] for a in agents}
    others = [{**data, "id": name, "_kept": set(data)} for name, data in old_agents.items()
              if name not in ids and isinstance(data, dict)]
    if not others:
        return []
    names = ", ".join(str(a.get("label") or a["id"]) for a in others)
    console.print(f"  [{C['dim']}]Your settings also have models this run didn't set up: {safe_markup(names)}[/]")
    if Confirm.ask(f"  [{C['moon']}]Keep {'it' if len(others) == 1 else 'them'}?[/]", default=True):
        return others
    console.print(f"  [{C['dim']}]They'll be left out (the old file is kept as config.toml.bak).[/]")
    return []


# ── Summary + write ────────────────────────────────────────────────────────────

def with_existing(agents: list[dict]) -> tuple[list[dict], dict | None]:
    """This run's agents with what the settings file it replaces had (see _carry_over and _keep_others), and
    that file's settings."""
    existing = _existing_config()
    _carry_over(agents, existing)
    return agents + _keep_others(agents, existing), existing


def _print_summary_and_write(agents: list[dict], review: dict | None = None, saver: dict | None = None,
                             triage: dict | None = None, existing: dict | None = None) -> None:
    """Show final summary table, confirm, then write config.toml (with what in `existing`, the file it
    replaces, this run didn't ask about)."""
    console.print(f"\n  [{C['blue']}]━━ Configuration Summary ━━[/]\n")

    if not agents:
        console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]No agents configured — nothing to write.[/]\n")
        return

    table = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style=f"bold {C['moon']}",
        border_style=C["dim"],
        padding=(0, 1),
    )
    table.add_column("ID",              style=C["blue"],   min_width=14)
    table.add_column("Label",           style=C["moon"],   min_width=24)
    table.add_column("Type",            style=C["dim"],    min_width=10)
    table.add_column("Model / Session", style=C["dim"],    min_width=30)

    for a in agents:
        extra = a.get("model") or a.get("session_key") or a.get("command") or a.get("preset") or "—"
        table.add_row(safe_markup(a["id"]), safe_markup(str(a.get("label") or a["id"])),
                      safe_markup(str(a.get("type") or ("oneshot" if a.get("preset") else "—"))),
                      safe_markup(str(extra)))

    console.print(table)
    console.print(f"  [{C['dim']}]Config  →[/] [{C['blue']}]{_CONFIG_FILE}[/]")
    console.print(f"  [{C['dim']}]Keys    →[/] [{C['blue']}]{where_keys_are().path}[/]")
    console.print()

    do_write = Confirm.ask(f"  [{C['moon']}]Write configuration now?[/]", default=True)
    if not do_write:
        console.print(f"  [{C['dim']}]Aborted — no files written.[/]\n")
        return

    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if _CONFIG_FILE.exists():
        backup = _CONFIG_FILE.with_suffix(".toml.bak")
        shutil.copy2(_CONFIG_FILE, backup)
        console.print(f"  [{C['dim']}]Backed up → {backup}[/]")

    # Explicit UTF-8: Windows' default encoding can't round-trip the "—" in the header
    write_private_file(_CONFIG_FILE, _build_toml(agents, review, saver, triage, existing).encode("utf-8"))
    console.print(f"  [{C['green']}]✓[/] [{C['dim']}]Config written → {_CONFIG_FILE}[/]")
    console.print()


# ── Public entry point ─────────────────────────────────────────────────────────

def run_setup() -> None:
    """
    Interactive setup wizard for configuring providers and agents.

    Steps:
      1. Welcome screen with current status table
      2. Provider setup (key detection → validation → save)
      3. Agent configuration (auto-detect OpenClaw sessions, HTTP defaults)
      4. Summary table → write config.toml
    """
    # Load the saved keys before anything else
    load_env()
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1. Welcome ─────────────────────────────────────────────────────────
    status = _detect_status()
    _print_welcome(status)

    console.print(f"  [{C['dim']}]Walk through each provider — press Enter to keep defaults.[/]")
    console.print(f"  [{C['dim']}]You can skip any provider by answering 'n'.[/]\n")

    # ── 2. Providers ───────────────────────────────────────────────────────
    provider_keys: dict[str, Optional[str]] = {}
    for provider in PROVIDERS:
        provider_keys[provider["id"]] = _setup_provider(provider, status)

    # ── 3. Agent configuration ─────────────────────────────────────────────
    agents = _configure_agents(provider_keys) if any(provider_keys.values()) else []
    agents += _configure_cli_agents({a["id"] for a in agents})
    agents += _configure_local_agents({a["id"] for a in agents})
    agents, existing = with_existing(agents)

    if not agents:
        console.print(
            f"  [{C['gold']}]⚠[/] [{C['dim']}]No agents configured.[/]  "
            f"[{C['dim']}]Run '[/][{C['blue']}]ixel setup[/][{C['dim']}]' again when ready.[/]\n"
        )
        return

    _suggest_more_members(agents)

    # ── 4. Review defaults, summary + write ────────────────────────────────
    review = _configure_review(agents)
    saver = _configure_saver(agents)
    _drop_saver_without_verifier(review, saver)
    triage = _configure_triage(agents, review)
    _print_summary_and_write(agents, review, saver, triage, existing)

    # ── Done ───────────────────────────────────────────────────────────────
    console.print(Panel(
        f"[{C['green']}]Setup complete![/]\n\n"
        f"[{C['dim']}]Start Ixel MAT:[/]         [{C['blue']}]ixel[/]\n"
        f"[{C['dim']}]Check agents:[/]           [{C['blue']}]ixel status[/]\n"
        f"[{C['dim']}]See or change models:[/]   [{C['blue']}]ixel model[/]\n\n"
        f"[{C['dim']}]Your keys load automatically; there's nothing to set in your shell.[/]",
        border_style=C["green"],
        padding=(0, 2),
    ))
    console.print()
