#!/usr/bin/env python3
"""
Ixel MAT — CLI entry point.

Usage:
  ixel                    Launch multi-agent terminal
  ixel setup              Interactive setup wizard
  ixel status             Show single-screen status dashboard
  ixel config             Show resolved config + validation
  ixel agents             List configured agents + status
  ixel help               Show all commands
  ixel version            Show version
  ixel doctor             Check that each part of Ixel is ready (--check tests models too)
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import os
import signal
import stat
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.text import Text

from ixel_mat import __version__
from ixel_mat.commands import COMMANDS, build_help_rows, resolve_command_name
from ixel_mat.hyperlinks import hyperlink_text
from ixel_mat.sanitize import safe_markup, sanitize_terminal_text
from ixel_mat.theme import C


def classify_probe_status(ok: bool, message: str) -> tuple[str, str]:
    msg = (message or "").lower()
    if ok:
        return "ok", "✓ ok"
    if "429" in msg or "rate limit" in msg:
        return "rate_limited", "⚠ rate limited"
    if any(token in msg for token in ("401", "403", "invalid key", "invalid token", "auth failed", "forbidden")):
        return "auth_failed", "✗ auth failed"
    return "unreachable", "✗ unreachable"


def get_secret_file_status(path: Path) -> dict[str, str | bool]:
    if not path.exists():
        return {
            "exists": False,
            "permissions_octal": "—",
            "last_modified": "—",
        }

    st = path.stat()
    perms = stat.S_IMODE(st.st_mode)
    last_modified = dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "exists": True,
        "permissions_octal": f"{perms:o}",
        "last_modified": last_modified,
    }


def _status_color(status_key: str) -> str:
    return {
        "ok": C["green"],
        "rate_limited": C["gold"],
        "auth_failed": C["red"],
        "unreachable": C["red"],
    }.get(status_key, C["dim"])


def _probe_provider(provider: dict, key: str) -> tuple[str, str, int | None]:
    from ixel_mat.config.setup import _probe_anthropic, _probe_google, _probe_openai_style, _probe_openclaw

    started = time.perf_counter()
    pid = provider.get("id", "")
    probe_type = provider.get("probe_type", "openai")

    try:
        if pid == "openclaw":
            ok, msg, _ = _probe_openclaw(key)
        elif probe_type == "anthropic":
            ok, msg = _probe_anthropic(key)
        elif probe_type == "google":
            ok, msg = _probe_google(key)
        else:
            ok, msg = _probe_openai_style(key, provider.get("probe_url", ""))
    except Exception as exc:
        ok, msg = False, f"connection error: {exc}"

    latency_ms = int((time.perf_counter() - started) * 1000)
    status_key, status_label = classify_probe_status(ok, msg)
    return status_key, status_label, latency_ms


def remediation_hint(status_key: str, message: str, provider_id: str | None = None) -> str:
    provider_label = provider_id or "provider"
    if status_key == "auth_failed":
        return f"Run ixel setup and replace the {provider_label} key."
    if status_key == "rate_limited":
        return f"{provider_label} is rate limited — retry shortly or switch models."
    if status_key == "model_missing":
        return "Set this agent's model to one the server lists (for Ollama: ollama pull <model>)."
    if status_key == "not_installed":
        return "Install it (ixel doctor says how), or set this agent's command to where it is."
    return f"Check network reachability and verify {provider_label} endpoint/settings."


def summarize_agent_probe(cfg, status_key: str, detail: str, latency_ms: int | None = None) -> tuple[str, str]:
    if cfg.type == "http":
        if status_key == "ok" and not cfg.token:  # a local server: nothing to authenticate
            return f"[{C['green']}]✓ reachable[/]", f"ready — {latency_ms}ms"
        if status_key == "ok":
            return f"[{C['green']}]✓ auth ok[/]", f"ready — {latency_ms}ms auth probe"
        if status_key == "model_missing":
            return f"[{C['red']}]✗ model not found[/]", detail
        if status_key == "rate_limited":
            return f"[{C['gold']}]⚠ rate limited[/]", detail
        if status_key == "auth_failed":
            return f"[{C['red']}]✗ auth failed[/]", detail
        return f"[{C['red']}]✗ unreachable[/]", detail
    if status_key == "ok" and cfg.type == "oneshot":  # started for each question, so nothing to connect to
        return f"[{C['green']}]✓ installed[/]", detail
    if status_key == "ok":
        return f"[{C['green']}]✓ connected[/]", "transport ready"
    if status_key == "not_installed":
        return f"[{C['red']}]✗ not installed[/]", detail
    return f"[{C['red']}]✗ failed[/]", detail


def _models_url(chat_url: str) -> str:
    """http://127.0.0.1:11434/v1/chat/completions -> http://127.0.0.1:11434/v1/models"""
    base = chat_url.rstrip("/")
    for suffix in ("/chat/completions", "/messages"):
        if base.endswith(suffix):
            return base[: -len(suffix)] + "/models"
    return base + "/models"


def _probe_other_http(cfg) -> tuple[str, str, int | None]:
    """Ask an OpenAI-compatible server for its models, and check ours is one of them."""
    import urllib.error
    import urllib.request

    from ixel_mat.agents.base import cleartext_refusal, needs_api_key, sends_key_in_cleartext
    from ixel_mat.config.setup import _get_json

    if needs_api_key(cfg) and not cfg.token:
        return "auth_failed", "no API key set", None
    if sends_key_in_cleartext(cfg.url, cfg.token):  # the same rule a real call follows
        return "unreachable", cleartext_refusal(cfg.name, cfg.url).split(": ", 1)[1], None
    headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
    started = time.perf_counter()
    try:
        data = _get_json(urllib.request.Request(_models_url(cfg.url), headers=headers), timeout=5)
    except urllib.error.HTTPError as exc:
        return (*classify_probe_status(False, f"HTTP {exc.code}"), None)
    except Exception as exc:  # noqa: BLE001 — refused, timed out, not JSON…
        reason = getattr(exc, "reason", None) or exc
        return "unreachable", f"no answer: {reason}", None
    latency_ms = int((time.perf_counter() - started) * 1000)
    models = [m.get("id", "") for m in (data.get("data") or []) if isinstance(m, dict)]
    # Ollama lists "llama3.3:latest" for a model you can call as "llama3.3"
    if cfg.model and models and not any(m == cfg.model or m.startswith(cfg.model + ":") for m in models):
        shown = ", ".join(models[:6]) + ("…" if len(models) > 6 else "")
        return "model_missing", f"the server has no model {cfg.model!r} (it has: {shown})", latency_ms
    return "ok", "reachable", latency_ms


def _provider_for_agent_cfg(cfg):
    from ixel_mat.config.setup import PROVIDERS

    for provider in PROVIDERS:
        if cfg.url and provider.get("url") == cfg.url:
            return provider
    return None


async def _probe_agent_connection(cfg) -> tuple[str, str, int | None, str | None]:
    from ixel_mat.agents import create_agent

    if cfg.type == "http":
        provider = _provider_for_agent_cfg(cfg)
        if provider is None:  # a local model server, or an endpoint without a preset
            status_key, status_label, latency_ms = await asyncio.to_thread(_probe_other_http, cfg)
            return status_key, status_label, latency_ms, None
        status_key, status_label, latency_ms = _probe_provider(provider, cfg.token)
        return status_key, status_label, latency_ms, provider.get("id")

    if cfg.type in ("oneshot", "subprocess") and cfg.command:
        from ixel_mat.agents.launch import find_on_path
        found = find_on_path(cfg.command)
        if found is None:
            return "not_installed", f"{cfg.command} isn't installed, or isn't on PATH", None, None
        if cfg.type == "oneshot":
            return "ok", f"{found} (not asked anything)", None, None

    try:
        agent = create_agent(cfg)
    except Exception as exc:
        return "unreachable", str(exc), None, None

    try:
        await agent.connect()
        await agent.disconnect()
        return "ok", "connected", None, None
    except Exception as exc:
        return "unreachable", str(exc), None, None

console = Console()
VERSION = __version__


def print_banner():
    console.print(f"\n  [{C['gold']}]Ixel MAT[/] [{C['dim']}]v{VERSION}[/]  [{C['dim']}]— Multi-Agent Terminal[/]")
    console.print(f"  [{C['dim']}]{'─' * 40}[/]\n")


def cmd_help():
    print_banner()
    rows = build_help_rows(mode='cli')
    width = max(len(cmd) for cmd, _ in rows)
    for cmd, desc in rows:  # escaped: "[flags]" would otherwise be read as a Rich style tag
        console.print(f"    [{C['blue']}]{safe_markup(cmd.ljust(width))}[/]  [{C['dim']}]{safe_markup(desc)}[/]")
    console.print()


def cmd_version():
    console.print(f"  [{C['gold']}]Ixel MAT[/] [{C['moon']}]v{VERSION}[/]")


def cmd_config():
    from ixel_mat.config.secrets import load_env
    load_env()
    from ixel_mat.config.loader import load_config, print_config_status
    config = load_config()
    print_banner()
    print_config_status(config)


def cmd_setup():
    from ixel_mat.config.setup import run_setup
    # (Not on Windows: Git Bash's window gives Python a pipe even though someone is typing in it. With no
    # one there, the questions end at once with EOFError, below.)
    if not sys.stdin.isatty() and os.name != "nt":
        console.print(f"  [{C['red']}]✗[/] [{C['dim']}]ixel setup asks you questions, so it needs a terminal. "
                      f"Run it in a terminal window.[/]")
        sys.exit(1)
    try:
        run_setup()
    except (EOFError, KeyboardInterrupt):
        console.print(f"\n  [{C['dim']}]Setup stopped before the end. Run[/] [{C['blue']}]ixel setup[/] "
                      f"[{C['dim']}]again to finish.[/]")
        sys.exit(1)


def _live_models(cfg) -> tuple[list[str | dict], str | None]:
    """(models an HTTP agent's server offers right now, provider id or None); [] if it can't say. A big
    provider's come with their release dates, so `latest` here means what it does when the agent runs."""
    import urllib.request

    from ixel_mat.config.setup import _get_json
    from ixel_mat.gui.model_choices import company_models
    from ixel_mat.models import models_url, provider_for_url

    from ixel_mat.agents.base import sends_key_in_cleartext

    provider = provider_for_url(cfg.url)
    if provider is not None:
        if not cfg.token:
            return [], provider
        try:
            return company_models(provider, cfg.token), provider
        except Exception:  # noqa: BLE001 — offline, or a key it refused
            return [], provider
    if sends_key_in_cleartext(cfg.url, cfg.token):  # never put the key on the wire to ask
        return [], None
    headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
    try:
        data = _get_json(urllib.request.Request(models_url(cfg.url), headers=headers), timeout=5)
    except Exception:  # noqa: BLE001 — not running, or no model list
        return [], None
    from ixel_mat.local_models import is_chat_model
    return [m["id"] for m in data.get("data", []) if isinstance(m, dict) and isinstance(m.get("id"), str)
            and is_chat_model(m["id"])], None


def _model_now(cfg, models: list[str], provider: str | None) -> str:
    """What an agent's model setting means today."""
    from ixel_mat.models import ALIASES, pick_latest

    if cfg.type == "oneshot":
        return cfg.model or "the CLI's own default"
    if cfg.type != "http":
        return cfg.model or cfg.session_key or "—"
    alias = cfg.model if cfg.model in ALIASES else ("latest" if not cfg.model and provider else None)
    if alias:
        picked = pick_latest(provider, models, alias) if provider and models else None
        return picked or f"{alias} (add the API key to see which)"
    return cfg.model or "— (set one)"


def cmd_model(argv: list[str] | None = None) -> int:
    """`ixel model`: every agent's model; `ixel model AGENT`: its choices; `ixel model AGENT NAME`: change it."""
    from concurrent.futures import ThreadPoolExecutor

    from rich import box as rbox
    from rich.table import Table

    from ixel_mat.config.loader import build_agent_configs, find_config, load_config, set_agent_model
    from ixel_mat.config.secrets import load_env
    from ixel_mat.config.setup import CLI_PRESETS
    from ixel_mat.models import ALIASES, pick_latest, provider_for_url

    argv = list(sys.argv[2:] if argv is None else argv)
    if argv[:1] in (["-h"], ["--help"]):
        console.print("usage: ixel model [AGENT [MODEL]]\n\n"
                      "  ixel model               every agent's model, and what it uses now\n"
                      "  ixel model AGENT         that agent's choices\n"
                      "  ixel model AGENT MODEL   change it: a model name, latest, latest-fast, or default",
                      markup=False, highlight=False)
        return 0
    load_env()
    config = load_config()
    configs, _ = build_agent_configs(config)
    if not configs:
        console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]No agents configured yet. Run:[/] [{C['blue']}]ixel setup[/]")
        return 1

    if len(argv) >= 2:  # change one
        agent, model = argv[0], argv[1]
        path = find_config()
        if agent not in configs or path is None:
            console.print(f"  [{C['red']}]✗[/] No agent named {safe_markup(agent)}. "
                          f"Agents: {safe_markup(', '.join(configs))}")
            return 1
        cfg = configs[agent]
        newest = cfg.type == "http" and provider_for_url(cfg.url) is not None
        if (model in ALIASES or (model == "default" and cfg.type == "http")) and not newest:
            # Only the big providers' model names say which is newest; a local server's don't
            console.print(f"  [{C['red']}]✗[/] {safe_markup(cfg.label)} has no list of newest models to pick from, "
                          f"so {safe_markup(model)} can't work there. Name the model instead "
                          f"([{C['blue']}]ixel model {safe_markup(repr(agent) if ' ' in agent else agent)}[/] shows what it has).")
            return 1
        try:
            set_agent_model(path, agent, model)
        except (ValueError, KeyError) as exc:
            console.print(f"  [{C['red']}]✗[/] {safe_markup(exc)}")
            return 1
        shown = "its default" if model == "default" else model
        console.print(f"  [{C['green']}]✓[/] {safe_markup(configs[agent].label)} now uses "
                      f"[{C['moon']}]{safe_markup(shown)}[/] [{C['dim']}](saved to {safe_markup(path)})[/]")
        if model in ALIASES or (model == "default" and configs[agent].type == "http"):
            console.print(f"  [{C['dim']}]Ixel looks up the newest matching model every time it starts.[/]")
        return 0

    names = [argv[0]] if argv else list(configs)
    unknown = [n for n in names if n not in configs]
    if unknown:
        console.print(f"  [{C['red']}]✗[/] No agent named {safe_markup(unknown[0])}. "
                      f"Agents: {safe_markup(', '.join(configs))}")
        return 1

    http = [n for n in names if configs[n].type == "http"]
    with ThreadPoolExecutor(max_workers=8) as pool:  # each provider answers in parallel
        live = dict(zip(http, pool.map(lambda n: _live_models(configs[n]), http)))

    table = Table(box=rbox.SIMPLE, header_style=f"bold {C['moon']}", border_style=C["dim"], padding=(0, 1))
    table.add_column("Agent", style=C["blue"])
    table.add_column("Label", style=C["moon"])
    table.add_column("Setting", style=C["dim"])
    table.add_column("Uses now", style=C["green"])
    for n in names:
        cfg = configs[n]
        models, provider = live.get(n, ([], None))
        setting = cfg.model or ("latest" if cfg.type == "http" and provider else "default")
        table.add_row(safe_markup(n), safe_markup(cfg.label), safe_markup(setting),
                      safe_markup(_model_now(cfg, models, provider)))
    console.print()
    console.print(table)

    if argv:  # one agent: show its choices
        cfg = configs[names[0]]
        models, provider = live.get(names[0], ([], None))
        if cfg.type == "http" and models:
            if provider:
                picks = [f"{a} = {pick_latest(provider, models, a)}" for a in ALIASES if pick_latest(provider, models, a)]
                console.print(f"  [{C['dim']}]{safe_markup(' · '.join(picks))}[/]")
            from ixel_mat.gui.model_choices import listed
            ids = listed(provider, [m if isinstance(m, dict) else {"id": m} for m in models])  # newest first
            console.print(f"  [{C['dim']}]Available: {safe_markup(', '.join(ids[:30]))}"
                          f"{' …' if len(ids) > 30 else ''}[/]")
        elif cfg.type == "oneshot":
            preset = next((p for p in CLI_PRESETS if p["command"] == cfg.command), None)
            if preset:
                console.print(f"  [{C['dim']}]{safe_markup(preset['label'])} accepts "
                              f"{safe_markup(preset['model_hint'])}.[/]")
    console.print(f"  [{C['dim']}]Change one:[/] [{C['blue']}]ixel model <agent> <model>[/]"
                  f"[{C['dim']}]  (latest, latest-fast, a model name, or default)[/]")
    console.print()
    return 0


def cmd_status():
    """Single-screen status dashboard for providers, agents, secrets, and config."""
    from rich.table import Table
    from rich import box as rbox
    from ixel_mat.config.secrets import get_env_file_path, load_env, where_keys_are
    from ixel_mat.config.loader import load_config, build_agent_configs, validate_config, find_config
    from ixel_mat.config.setup import PROVIDERS, _mask_key

    load_env()
    config = load_config()
    configs, warnings = build_agent_configs(config)
    issues = validate_config(config)
    config_path = find_config() or config.get("_source", "defaults")
    store = where_keys_are()
    secret_paths = [store.path]
    if get_env_file_path() != store.path and get_env_file_path().exists():
        secret_paths.append(get_env_file_path())  # still holding keys in plain text, beside keys.enc

    print_banner()
    console.print(f"  [{C['gold']}]Status Dashboard[/]\n")
    console.print(f"  [{C['blue']}]Version[/] [{C['moon']}]v{VERSION}[/]  [{C['dim']}]Python {sys.version.split()[0]}[/]")
    console.print(f"  [{C['blue']}]Config source[/]", end=" ")
    console.print(hyperlink_text(str(config_path)))
    console.print()

    ptable = Table(
        title=f"[{C['gold']}]Providers[/]",
        box=rbox.SIMPLE,
        show_header=True,
        header_style=f"bold {C['moon']}",
        border_style=C["dim"],
        padding=(0, 1),
    )
    ptable.add_column("Provider", style=C["blue"], min_width=20)
    ptable.add_column("Auth", min_width=12)
    ptable.add_column("Probe", min_width=18)
    ptable.add_column("Latency", style=C["dim"], min_width=10)
    ptable.add_column("Key", style=C["dim"], min_width=14)

    for p in PROVIDERS:
        key = os.getenv(p["env_name"], "")
        auth_str = f"[{C['green']}]✓ set[/]" if key else f"[{C['red']}]✗ not set[/]"
        key_str = _mask_key(key) if key else "—"
        if key:
            status_key, status_label, latency_ms = _probe_provider(p, key)
            probe_str = f"[{_status_color(status_key)}]{status_label}[/]"
            latency_str = f"{latency_ms}ms"
        else:
            probe_str = f"[{C['dim']}]—[/]"
            latency_str = "—"
        ptable.add_row(p["name"], auth_str, probe_str, latency_str, key_str)

    console.print(ptable)
    console.print()

    atable = Table(
        title=f"[{C['gold']}]Agents[/]",
        box=rbox.SIMPLE,
        show_header=True,
        header_style=f"bold {C['moon']}",
        border_style=C["dim"],
        padding=(0, 1),
    )
    atable.add_column("Agent", style=C["blue"], min_width=14)
    atable.add_column("Label", style=C["moon"], min_width=22)
    atable.add_column("Type", style=C["dim"], min_width=10)
    atable.add_column("Status", min_width=16)
    atable.add_column("Details", style=C["dim"], min_width=32)

    async def collect_agent_rows():
        rows = []
        for name, cfg in configs.items():
            status_key, detail, latency_ms, provider_id = await _probe_agent_connection(cfg)
            status_str, detail_str = summarize_agent_probe(cfg, status_key, detail, latency_ms)
            if status_key != 'ok':
                detail_str = f"{detail_str} — {remediation_hint(status_key, detail, provider_id)}"
            rows.append((safe_markup(name), safe_markup(cfg.label), cfg.type, status_str, safe_markup(detail_str[:120])))
        return rows

    rows = asyncio.run(collect_agent_rows()) if configs else []
    for row in rows:
        atable.add_row(*row)
    if not rows:
        atable.add_row("—", "No agents configured", "—", f"[{C['gold']}]⚠[/]", "Run ixel setup")

    console.print(atable)
    console.print()

    stable = Table(
        title=f"[{C['gold']}]Saved keys[/]",
        box=rbox.SIMPLE,
        show_header=True,
        header_style=f"bold {C['moon']}",
        border_style=C["dim"],
        padding=(0, 1),
    )
    stable.add_column("Path", style=C["blue"], min_width=32)
    stable.add_column("Exists", min_width=10)
    stable.add_column("Perms", style=C["dim"], min_width=8)
    stable.add_column("Last Modified", style=C["dim"], min_width=20)
    for secret_path in secret_paths:
        secret_status = get_secret_file_status(secret_path)
        stable.add_row(
            hyperlink_text(str(secret_path)),
            f"[{C['green']}]✓[/]" if secret_status["exists"] else f"[{C['red']}]✗[/]",
            str(secret_status["permissions_octal"]),
            str(secret_status["last_modified"]),
        )
    console.print(stable)
    mark = f"[{C['green']}]✓[/]" if store.kind == "keychain" else f"[{C['gold']}]⚠[/]"
    console.print(f"  {mark} [{C['dim']}]{safe_markup(store.summary)}[/]")
    if store.problem:
        console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(store.problem)}[/]")
    console.print()

    console.print(f"  [{C['gold']}]Warnings[/]")
    if warnings or issues:
        for item in [*warnings, *issues]:
            console.print(f"    [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(item)}[/]")
    else:
        console.print(f"    [{C['green']}]✓[/] [{C['dim']}]No config warnings[/]")
    console.print()


def cmd_agents():
    """List agents and test connectivity."""
    from ixel_mat.config.secrets import load_env
    load_env()
    from ixel_mat.config.loader import load_config, build_agent_configs

    config = load_config()
    configs, warnings = build_agent_configs(config)

    print_banner()
    console.print(f"  [{C['moon']}]Agents ({len(configs)})[/]\n")

    for warn in warnings:
        console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(warn)}[/]")
    if warnings:
        console.print()

    async def test_agents():
        for name, cfg in configs.items():
            agent_type = cfg.type
            token_set = bool(cfg.token)
            url = cfg.url or "(none)"

            console.print(f"    [{C['blue']}]{safe_markup(name)}[/] [{C['dim']}]— {safe_markup(cfg.label)}[/]")
            console.print(f"      [{C['dim']}]type: {safe_markup(agent_type)}  url: {safe_markup(url)}  token: {'✓' if token_set else '✗'}[/]")

            status_key, detail, latency_ms, provider_id = await _probe_agent_connection(cfg)
            status_str, detail_str = summarize_agent_probe(cfg, status_key, detail, latency_ms)
            console.print(f"      {status_str} [{C['dim']}]{safe_markup(detail_str)}[/]")
            if status_key != 'ok':
                console.print(f"      [{C['gold']}]⚠[/] [{C['dim']}]{remediation_hint(status_key, detail, provider_id)}[/]")

            console.print()

    asyncio.run(test_agents())


def cmd_doctor(argv: list[str] | None = None) -> int:
    """`ixel doctor`: what the app's Health page shows. --check also tests each model and program."""
    import argparse
    import json

    from ixel_mat import health

    parser = argparse.ArgumentParser(prog="ixel doctor", description="Check that each part of Ixel is ready. "
                                     "Exits 1 when something needs fixing.")
    parser.add_argument("--check", action="store_true",
                        help="also ask each model and program whether it answers (uses the network)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args([] if argv is None else argv)

    report = asyncio.run(health.report(probe=args.check))
    code = 1 if health.failing(report) else 0
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return code

    marks = {"ok": (C["green"], "✓"), "warn": (C["gold"], "⚠"), "fail": (C["red"], "✗"),
             "off": (C["dim"], "○"), "unchecked": (C["dim"], "·")}
    print_banner()
    for group in report["groups"]:
        console.print(f"  [{C['moon']}]{safe_markup(group['title'])}[/]")
        for check in group["checks"]:
            color, mark = marks[check["state"]]
            console.print(f"    [{color}]{mark}[/] {safe_markup(check['label'])}  "
                          f"[{C['dim']}]{safe_markup(check['detail'])}[/]")
            if check["fix"]:
                console.print(f"      [{C['dim']}]Fix:[/] [{C['blue']}]{safe_markup(check['fix'])}[/]")
        console.print()
    if not args.check:
        console.print(f"  [{C['dim']}]To test each model too (this uses the network):[/] "
                      f"[{C['blue']}]ixel doctor --check[/]\n")
    return code


def json_usage_errors(parser, argv: list[str]) -> None:
    """With --json, a bad argument is a problem like any other: one {"error": …} line and exit 1."""
    if "--json" in argv:
        import json

        def error(message: str):
            print(json.dumps({"error": f"{parser.prog}: {message}"}))
            sys.exit(1)
        parser.error = error


def cmd_review(argv: list[str]) -> int:
    """`ixel review "question"` — run a panel review without the interactive terminal."""
    import argparse
    import json

    from rich.live import Live

    from ixel_mat import review_ui, stats
    from ixel_mat.conversation import asked_in_private, continued, load_conversation, save_conversation
    from ixel_mat.material import MaterialError, code_for_review
    from ixel_mat.modes.review import run_review
    from ixel_mat.runtime import (MODE_CHOICES, choose_mode, connect_agents, disconnect_agents, load_settings,
                                  local_agent_names)

    parser = argparse.ArgumentParser(
        prog="ixel review",
        description="Every configured model answers, they review each other anonymously, "
                    "and a moderator writes the verdict.",
    )
    parser.add_argument("question", nargs="*", help='the question; "-" (or piped input) reads it from stdin. '
                                                    "With code to review it's optional")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mode", choices=MODE_CHOICES, help="default: [review] mode, else review")
    group.add_argument("--quick", dest="mode", action="store_const", const="quick", help="answers + verdict, no peer review")
    group.add_argument("--deep", dest="mode", action="store_const", const="deep", help="adds a revision round")
    group.add_argument("--saver", dest="mode", action="store_const", const="saver",
                       help="cheaper models draft and check; your big model only verifies")
    group.add_argument("--auto", dest="mode", action="store_const", const="auto",
                       help="Triage ([triage] in your config) picks quick, review or deep for this question")
    parser.add_argument("--moderator", help="agent that writes the verdict (default: best-rated author)")
    parser.add_argument("--timeout", type=float,
                        help="seconds per model call, for models with no timeout of their own "
                             "(default: [review] timeout, else 180)")
    parser.add_argument("-c", "--continue", dest="follow_up", action="store_true",
                        help="a follow-up: the panel also sees your last few questions and its answers to them "
                             "(each kept for a day)")
    code = parser.add_argument_group("code to review (read-only: nothing runs, nothing changes)")
    diff = code.add_mutually_exclusive_group()
    diff.add_argument("--diff", action="store_true", help="your changes since the last commit (git diff HEAD)")
    diff.add_argument("--staged", action="store_true", help="only what you've staged (git diff --cached)")
    diff.add_argument("--base", metavar="REF", help="everything on this branch since it left REF, e.g. main")
    code.add_argument("--new-files", action="store_true",
                      help="with --diff or --base, new files git doesn't track yet too (the ones safe to send)")
    code.add_argument("--head", metavar="REF",
                      help="with --base: what's committed on REF since it left the base (a pull request fetched "
                           "but not checked out); your own files and changes aren't read")
    code.add_argument("-f", "--file", dest="files", action="append", default=[], metavar="PATH",
                      help="a file to review (repeat for more)")
    code.add_argument("--allow-secrets", action="store_true",
                      help="send it even if it looks like it holds a key (only if you're sure it doesn't)")
    parser.add_argument("--answers", action="store_true", help="show every answer in full")
    parser.add_argument("--json", action="store_true", help="print the result as JSON (progress goes to stderr)")
    args = parser.parse_args(argv)
    if args.new_files and not (args.diff or args.base):
        parser.error("--new-files goes with --diff or --base")
    if args.head and not args.base:
        parser.error("--head goes with --base")
    if args.head and args.new_files:
        parser.error("--head and --new-files don't go together: new files are yours, not the branch's")

    question = " ".join(args.question)
    wants_code = bool(args.diff or args.staged or args.base or args.files)
    # With code to review, piped input is never read: in a git hook it's git's, not a question
    if question == "-" or (not question and not wants_code and not sys.stdin.isatty()):
        question = read_piped_text()
    material = None
    if wants_code:
        try:
            which = "staged" if args.staged else "base" if args.base else "uncommitted" if args.diff else None
            material, question = code_for_review(question, which, args.base, args.files,
                                                 allow_secrets=args.allow_secrets, new_files=args.new_files,
                                                 head=args.head)
        except MaterialError as exc:
            (Console(stderr=True) if args.json else console).print(
                f"  [{C['red']}]✗[/] [{C['dim']}]{safe_markup(exc)}[/]")
            return 1
    if not question.strip():
        parser.error("a question is required")
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout must be > 0")

    settings = load_settings()
    if args.moderator and args.moderator not in settings.agent_configs:
        parser.error(f"--moderator: there's no agent named {args.moderator!r} "
                     f"(agents: {', '.join(settings.agent_configs) or 'none yet'})")
    out = Console(stderr=True) if args.json else console
    for warning in settings.warnings:
        out.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(warning)}[/]")
    earlier = load_conversation() if args.follow_up else []
    if earlier and not settings.private and asked_in_private():
        earlier = []
        out.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]The last conversation was asked with Private on, so it stays "
                  "with your own models. This is a new question.[/]")
    elif args.follow_up and not earlier:
        out.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]Nothing to continue (Ixel keeps each question for a day), "
                  "so this is a new question.[/]")
    # Auto mode asks Triage first (well under a second); the decision is reported with the run
    mode, decision = asyncio.run(choose_mode(settings, args.mode, question, earlier))
    problem = settings.private_problem(mode)
    if not problem and args.moderator and not settings.yours(args.moderator):
        problem = f"Private is on, and {args.moderator} isn't on your computers, so it can't write the verdict."
    if problem:
        out.print(f"  [{C['red']}]✗[/] [{C['dim']}]{safe_markup(problem)}[/]")
        return 1
    if settings.private:
        out.print(f"  [{C['violet']}]↳[/] [{C['dim']}]Private: only models on your own computers answer"
                  + (f" ({safe_markup(', '.join(settings.sitting_out()))} sit out)" if settings.sitting_out() else "")
                  + ".[/]")
    options = settings.run_options(mode)
    if args.moderator:
        options["moderator"] = args.moderator
    if args.timeout:
        options["timeout"] = args.timeout
    options.update(earlier=earlier, auto=decision, material=material)

    if material is not None:
        out.print(f"  [{C['violet']}]↳[/] [{C['dim']}]Reviewing {safe_markup(material.title)} · "
                  f"{safe_markup(material.summary())}[/]")
        for note in material.notes:
            out.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(note)}[/]")

    def connected(cfg, error):
        if error is not None:
            out.print(f"  [{C['red']}]✗[/] [{C['blue']}]{safe_markup(cfg.label)}[/]  [{C['dim']}]{safe_markup(error)}[/]")

    async def go():
        agents = await connect_agents(settings.configs_for(mode), on_result=connected)
        try:
            if not agents:
                return None
            # Rounds that are over scroll up into the terminal's history; the live view holds the one running
            progress = review_ui.ReviewProgress(question, mode, len(agents), echo=None if args.json else out.print)
            if not args.json:
                out.print(progress.header)
            with Live(progress, console=out, refresh_per_second=10, transient=True):
                try:
                    return await run_review(question, list(agents.values()), on_event=progress.on_event, **options)
                finally:
                    progress.flush()
        finally:
            await disconnect_agents(agents)

    result = asyncio.run(go())
    if result is None:
        out.print(f"  [{C['red']}]No agents connected — run: ixel setup, then ixel agents[/]")
        return 1
    if result.final is not None:
        # for the next `ixel review --continue`
        save_conversation(continued(earlier, result), private=settings.private)
    update = stats.record_run(result, local_agent_names(settings.agent_configs))
    if args.json:
        data = result.to_dict()
        if update.counted:
            data["stats"] = update.to_dict()
        print(json.dumps(data, indent=2))  # ASCII-escaped: safe in any encoding
    else:
        for renderable in review_ui.report(result, full_answers=args.answers) + review_ui.stats_lines(update):
            console.print(renderable)
    return 0 if result.final is not None else 1


def cmd_ask(argv: list[str]) -> int:
    """`ixel ask --agent NAME "question"` — one model answers; no panel, no review. `--agent a,b,c`: the next
    one answers when one is out of usage."""
    import argparse
    import json

    from ixel_mat.ask import AskError, agent_list, ask_in_order, find_agent
    from ixel_mat.material import MaterialError, code_for_review
    from ixel_mat.runtime import load_settings

    parser = argparse.ArgumentParser(
        prog="ixel ask",
        description="One of your models answers, on its own: no panel, no review. "
                    "For a second opinion on the answer, use ixel review.")
    parser.add_argument("question", nargs="*", help='the question; "-" (or piped input) reads it from stdin. '
                                                    "With code attached it's optional")
    parser.add_argument("--agent", metavar="NAME", help="who answers: an agent's name or label, e.g. gemini or Grok. "
                                                        "Several, in order (codex,claude,local): when one is "
                                                        "out of usage, the next answers")
    parser.add_argument("--list", action="store_true", help="list the models you can ask, and stop")
    parser.add_argument("--timeout", type=float, help="seconds to wait for the answer")
    code = parser.add_argument_group("code to attach (read-only: nothing runs, nothing changes)")
    diff = code.add_mutually_exclusive_group()
    diff.add_argument("--diff", action="store_true", help="your changes since the last commit (git diff HEAD)")
    diff.add_argument("--staged", action="store_true", help="only what you've staged (git diff --cached)")
    diff.add_argument("--base", metavar="REF", help="everything on this branch since it left REF, e.g. main")
    code.add_argument("--new-files", action="store_true",
                      help="with --diff or --base, new files git doesn't track yet too (the ones safe to send)")
    code.add_argument("--head", metavar="REF",
                      help="with --base: what's committed on REF since it left the base (a pull request fetched "
                           "but not checked out); your own files and changes aren't read")
    code.add_argument("-f", "--file", dest="files", action="append", default=[], metavar="PATH",
                      help="a file to attach (repeat for more)")
    code.add_argument("--allow-secrets", action="store_true",
                      help="send it even if it looks like it holds a key (only if you're sure it doesn't)")
    parser.add_argument("--json", action="store_true",
                        help='print the result as JSON (a problem prints {"error": …} and exits 1)')
    json_usage_errors(parser, argv)
    args = parser.parse_args(argv)
    if args.new_files and not (args.diff or args.base):
        parser.error("--new-files goes with --diff or --base")
    if args.head and not args.base:
        parser.error("--head goes with --base")
    if args.head and args.new_files:
        parser.error("--head and --new-files don't go together: new files are yours, not the branch's")
    out = Console(stderr=True) if args.json else console

    def fail(message: str, kind: str = "", skipped: list[dict] | None = None) -> int:
        if args.json:  # error_kind "usage_limit": out of usage; skipped: the models before it that were too
            problem = {"error": str(message)}
            if kind:
                problem["error_kind"] = kind
            if skipped:
                problem["skipped"] = skipped
            print(json.dumps(problem))
        else:
            out.print(f"  [{C['red']}]✗[/] [{C['dim']}]{safe_markup(message)}[/]")
        return 1

    settings = load_settings()
    for warning in settings.warnings:
        out.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(warning)}[/]")
    if args.list:  # with Private on, only the models it lets answer
        agents = agent_list({name: cfg for name, cfg in settings.agent_configs.items() if settings.yours(name)})
        if args.json:
            # features: what this ixel ask can do, for programs that call it (Handoff)
            print(json.dumps({"agents": agents, "features": ["new-files", "head", "fallback", "error-kind"]},
                             indent=2))
            return 0
        if not agents and settings.private and settings.agent_configs:
            return fail("Private is on, and none of your models runs on your own computers. Add one in Settings, "
                        "under Models on your computers, or turn Private off.")
        if not agents:
            return fail("No models are set up yet. Run: ixel setup")
        for item in agents:
            ready = "" if item["ready"] else f"  [{C['gold']}](no key yet)[/]"
            model = f" · {safe_markup(item['model'])}" if item["model"] else ""
            console.print(f"  [{C['blue']}]{safe_markup(item['name'])}[/] [{C['dim']}]{safe_markup(item['label'])}"
                          f"{model}[/]{ready}")
        return 0
    if not args.agent:
        parser.error("--agent is required (who answers; --list shows the choices)")
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout must be > 0")

    question = " ".join(args.question)
    wants_code = bool(args.diff or args.staged or args.base or args.files)
    if question == "-" or (not question and not wants_code and not sys.stdin.isatty()):
        question = read_piped_text()
    material = None
    try:
        cfgs = []  # in the order given, each once
        for wanted in args.agent.split(","):
            if wanted.strip() and (cfg := find_agent(settings.agent_configs, wanted)) not in cfgs:
                cfgs.append(cfg)
        if not cfgs:
            parser.error("--agent needs a name (--list shows the choices)")
        for cfg in cfgs:
            if not settings.yours(cfg.name):
                return fail(f"Private is on, and {cfg.label} isn't on your computers. Ask one of your own models, or "
                            "turn Private off in Settings, under Asking.")
        if wants_code:
            which = "staged" if args.staged else "base" if args.base else "uncommitted" if args.diff else None
            material, question = code_for_review(question, which, args.base, args.files,
                                                 allow_secrets=args.allow_secrets, new_files=args.new_files,
                                                 head=args.head)
    except (AskError, MaterialError) as exc:
        return fail(str(exc))
    if not question.strip():
        parser.error("a question is required")
    if material is not None:
        out.print(f"  [{C['violet']}]↳[/] [{C['dim']}]Attached {safe_markup(material.title)} · "
                  f"{safe_markup(material.summary())}[/]")
        for note in material.notes:
            out.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(note)}[/]")
    if not args.json:
        out.print(f"  [{C['dim']}]Asking {safe_markup(cfgs[0].label)}…[/]")

    def moving_on(skipped, following, why: AskError) -> None:
        out.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(str(why))}[/]")
        out.print(f"  [{C['dim']}]Asking {safe_markup(following.label)} instead…[/]")

    try:
        result = asyncio.run(ask_in_order(cfgs, question, material, timeout=args.timeout, on_skip=moving_on))
    except AskError as exc:
        return fail(str(exc), exc.kind, exc.skipped)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))  # ASCII-escaped: safe in any encoding
        return 0
    console.print(f"\n  [{C['gold']}]{safe_markup(result.label)}[/] [{C['dim']}]· {result.ms / 1000:.1f}s[/]\n")
    console.print(Text(sanitize_terminal_text(result.answer)))
    console.print()
    return 0


def cmd_image(argv: list[str]) -> int:
    """`ixel image "description"` — pictures from xAI or OpenAI, saved as files."""
    import argparse
    import json

    from ixel_mat import images
    from ixel_mat.material import MaterialError, code_for_review
    from ixel_mat.runtime import load_settings

    parser = argparse.ArgumentParser(
        prog="ixel image",
        description="Make pictures with xAI (Grok) or OpenAI and save them as files. Attach files or a diff and "
                    "one of your chat models reads them first and describes the picture; only that description "
                    "goes to the image model.")
    parser.add_argument("description", nargs="*", help='what to make; "-" (or piped input) reads it from stdin')
    parser.add_argument("--provider", metavar="NAME", help="xai (or grok) or openai (default: [images] provider, "
                                                           "else the first with a key)")
    parser.add_argument("--model", help="the image model (default: [images] xai_model or openai_model)")
    parser.add_argument("-n", "--count", type=int, default=1, help=f"how many pictures, 1 to {images.MAX_COUNT}")
    parser.add_argument("--size", help="OpenAI only: e.g. 1024x1024, 1536x1024 or 1024x1536")
    parser.add_argument("--out", metavar="FOLDER", help="where to save them (default: Pictures/Ixel in your home)")
    parser.add_argument("--name", default="image", help="file names start with this (default: image)")
    parser.add_argument("--list", action="store_true", help="show which services are ready, and stop")
    work = parser.add_argument_group("your work, for a picture based on it (read-only)")
    diff = work.add_mutually_exclusive_group()
    diff.add_argument("--diff", action="store_true", help="your changes since the last commit")
    diff.add_argument("--base", metavar="REF", help="everything on this branch since it left REF")
    work.add_argument("-f", "--file", dest="files", action="append", default=[], metavar="PATH",
                      help="a file to base it on, such as README.md (repeat for more)")
    work.add_argument("--writer", metavar="AGENT", help="the chat model that reads them (default: Grok for xAI, "
                                                        "GPT for OpenAI, else your first model)")
    work.add_argument("--allow-secrets", action="store_true",
                      help="send them even if they look like they hold a key (only if you're sure they don't)")
    parser.add_argument("--timeout", type=float, default=images.TIMEOUT_SEC, help="seconds to wait for the pictures")
    parser.add_argument("--json", action="store_true",
                        help='print the result as JSON (a problem prints {"error": …} and exits 1)')
    json_usage_errors(parser, argv)
    args = parser.parse_args(argv)
    out = Console(stderr=True) if args.json else console

    def fail(message: str) -> int:
        if args.json:
            print(json.dumps({"error": str(message)}))
        else:
            out.print(f"  [{C['red']}]✗[/] [{C['dim']}]{safe_markup(message)}[/]")
        return 1

    if not 1 <= args.count <= images.MAX_COUNT:
        parser.error(f"--count must be 1 to {images.MAX_COUNT}")
    if args.timeout <= 0:
        parser.error("--timeout must be > 0")
    settings = load_settings()
    for warning in settings.warnings:
        out.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(warning)}[/]")
    try:
        if args.list:
            ready = images.status(settings.config)
            if args.json:
                print(json.dumps({"providers": ready}, indent=2))
            else:
                for item in ready:
                    mark = f"[{C['green']}]✓[/]" if item["ready"] else f"[{C['gold']}]⚠[/]"
                    why = f" · {safe_markup(item['why'])}" if not item["ready"] else ""
                    console.print(f"  {mark} [{C['blue']}]{safe_markup(item['name'])}[/] [{C['dim']}]"
                                  f"{safe_markup(item['label'])} · {safe_markup(item['model'])}{why}[/]")
            return 0

        description = " ".join(args.description)
        wants_work = bool(args.diff or args.base or args.files)
        if description == "-" or (not description and not wants_work and not sys.stdin.isatty()):
            description = read_piped_text()
        material = None
        if wants_work:
            which = "base" if args.base else "uncommitted" if args.diff else None
            material, description = code_for_review(description or "A picture that shows what this work is about.",
                                                    which, args.base, args.files, allow_secrets=args.allow_secrets)
        if not description.strip():
            parser.error("say what to make")
        provider = images.pick_provider(settings.config, args.provider)
        if args.model:
            provider = dataclasses.replace(provider, model=args.model)
        size = args.size
        if size and provider.name != "openai":  # xAI picks its own size and refuses the setting
            out.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]--size is for OpenAI only; "
                      f"{safe_markup(provider.label)} picks its own size.[/]")
            size = None
        prompt, writer = description.strip(), None
        if material is not None:
            # With Private on, only a model of yours reads your files (the description still goes out)
            writer = images.pick_writer({name: cfg for name, cfg in settings.agent_configs.items()
                                         if settings.yours(name)}, provider, args.writer, private=settings.private)
            out.print(f"  [{C['dim']}]{safe_markup(writer.label)} is reading {safe_markup(material.title)} "
                      f"({safe_markup(material.summary())}) to describe the picture…[/]")
            prompt = asyncio.run(images.describe(writer, description, material))
        out.print(f"  [{C['dim']}]Asking {safe_markup(provider.label)} ({safe_markup(provider.model)}) for "
                  f"{args.count} picture{'s' if args.count != 1 else ''}…[/]")
        pictures, rewritten = images.generate(provider, prompt, args.count, size, timeout=args.timeout)
        folder = Path(args.out).expanduser() if args.out else images.default_folder()
        saved = images.save(pictures, folder, args.name)
    except (images.ImageError, MaterialError) as exc:
        return fail(str(exc))
    except OSError as exc:
        return fail(f"Couldn't save the pictures: {exc}")
    result = {"provider": provider.name, "label": provider.label, "model": provider.model, "prompt": prompt,
              "files": [str(p) for p in saved]}
    if rewritten:
        result["revised_prompts"] = rewritten
    if writer is not None:
        result["writer"] = {"name": writer.name, "label": writer.label}
    if material is not None:
        result["material"] = material.to_dict()
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    if writer is not None:
        console.print(f"\n  [{C['dim']}]{safe_markup(writer.label)} described it as:[/]")
        console.print(Text("  " + sanitize_terminal_text(prompt)))
    console.print()
    for path in saved:
        console.print(f"  [{C['green']}]✓[/] {safe_markup(str(path))}")
    console.print()
    return 0


def cmd_saves() -> None:
    """`ixel saves` — the usage saver scoreboard."""
    from ixel_mat import review_ui, stats

    print_banner()
    for renderable in review_ui.stats_view(stats.summary(stats.load_stats())):
        console.print(renderable)


def cmd_triage(argv: list[str]) -> int:
    """`ixel triage` — the optional triage step: who answers its questions, and a test call."""
    from ixel_mat.triage import DEFAULT_MODEL, OFFICIAL_HOST, TriageError, make_triage
    from ixel_mat.runtime import load_settings

    if argv:
        print("usage: ixel triage", file=sys.stderr)
        return 2
    settings = load_settings()
    triage = settings.triage
    print_banner()
    console.print(f"  [{C['moon']}]Triage[/] [{C['dim']}]· quick decisions between rounds: how much checking a "
                  f"question needs, and whether answers agree[/]\n")
    for warning in (w for w in settings.warnings if w.startswith("[triage]") or "triage" in w.lower()):
        console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(warning)}[/]")
    if not triage.enabled:
        example = next(iter(settings.agent_configs), "your-fastest-model")
        console.print(f"  [{C['dim']}]Off. To have one of your own models decide, add this to your config[/]")
        console.print(f"  [{C['dim']}](ixel config shows where it is), naming the agent that should decide:[/]\n")
        console.print(f'    [triage]\n    enabled = true\n    agent = "{example}"\n', markup=False, highlight=False)
        console.print(f"  [{C['dim']}]Or use TypeSafe AI's decision API instead (faster, calibrated; needs a key from "
                      f"{OFFICIAL_HOST.removeprefix('api.')}, and sends them your questions): ixel setup.[/]\n")
        return 0
    on = lambda flag: f"[{C['green']}]on[/]" if flag else f"[{C['dim']}]off[/]"  # noqa: E731
    if triage.provider == "model":
        who = (f"[{C['green']}]{safe_markup(triage.via)}[/] [{C['dim']}](your agent {safe_markup(triage.agent)}; "
               f"nothing goes anywhere new)[/]" if triage.agent_config
               else f"[{C['red']}]no agent set[/]")
        console.print(f"    [{C['dim']}]Decided by[/]    {who}")
    else:
        where = (f"[{C['green']}]{OFFICIAL_HOST}[/] [{C['dim']}](TypeSafe's own API)[/]" if triage.official
                 else f"[{C['gold']}]{safe_markup(triage.host)}[/] [{C['dim']}](not TypeSafe's own API)[/]")
        console.print(f"    [{C['dim']}]Sends to[/]      {where}")
        console.print(f"    [{C['dim']}]Key[/]           "
                      + (f"[{C['green']}]set[/] [{C['dim']}]({safe_markup(triage.token_env)})[/]" if triage.token
                         else f"[{C['red']}]missing[/] [{C['dim']}]({safe_markup(triage.token_env)})[/]"))
        model = "TypeSafe's current one" if triage.model == DEFAULT_MODEL else safe_markup(triage.model)
        console.print(f"    [{C['dim']}]Model[/]         {model}")
    console.print(f"    [{C['dim']}]Auto mode[/]     {on(triage.auto_mode)}  [{C['dim']}]picks quick / review / deep "
                  f"when you ask for auto[/]")
    console.print(f"    [{C['dim']}]Skip review[/]   {on(triage.skip_review)}  [{C['dim']}]when every first answer agrees "
                  f"(at least {triage.threshold:.0%} sure)[/]")
    console.print(f"    [{C['dim']}]Saver gate[/]    {on(triage.saver_gate)}  [{C['dim']}]with \\[saver] escalate = "
                  f'"disagreement"[/]\n')
    if not triage.ready:
        return 1
    console.print(f"  [{C['dim']}]Test question…[/]", end="")
    try:
        who, ms = asyncio.run(make_triage(triage).check())
    except TriageError as exc:
        console.print(f"\r  [{C['red']}]✗[/] [{C['dim']}]{safe_markup(exc)}[/]\n")
        return 1
    console.print(f"\r  [{C['green']}]✓[/] [{C['dim']}]{safe_markup(who)} answered ({ms} ms)[/]\n")
    return 0


def cmd_mcp(argv: list[str]) -> int:
    """`ixel mcp` serves the plugin over stdio; `ixel mcp --setup` shows how to connect apps."""
    from ixel_mat.mcp_server import host_snippets, run_stdio

    if argv in (["--setup"], ["setup"]):
        print(host_snippets())
        return 0
    if argv:
        print("usage: ixel mcp [--setup]", file=sys.stderr)
        return 2
    run_stdio()
    return 0


def cmd_gui(argv: list[str]) -> int:
    """`ixel gui` — open the review panel in a browser, served only to this computer."""
    import argparse

    from ixel_mat.gui.server import serve
    from ixel_mat.runtime import load_settings

    parser = argparse.ArgumentParser(prog="ixel gui",
                                     description="Open the Ixel panel in your browser (served only to this computer).")
    parser.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: a random free port)")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser; just print the link")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    def announce(url: str) -> None:
        console.print(f"\n  [{C['gold']}]Ixel MAT[/] [{C['dim']}]is open at:[/]")
        console.print(f"  {url}", markup=False, highlight=False, soft_wrap=True)
        try:
            warnings = load_settings().warnings
        except Exception:  # noqa: BLE001 — the page reports a settings file it can't read
            warnings = []
        if warnings:
            console.print()
        for warning in warnings:
            console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(warning)}[/]")
        console.print(f"\n  [{C['dim']}]The link carries a one-time key for this session; don't share it.[/]")
        console.print(f"  [{C['dim']}]Press Ctrl+C to stop.[/]\n")

    try:
        asyncio.run(serve(port=args.port, open_browser=not args.no_browser, announce=announce))
    except KeyboardInterrupt:
        console.print(f"  [{C['dim']}]Stopped.[/]")
    except OSError as exc:
        console.print(f"  [{C['red']}]Couldn't start: {safe_markup(exc)}[/]")
        return 1
    return 0


def read_piped_text(stdin=None) -> str:
    """
    Text piped in, read as UTF-8 (what Handoff and most programs send). ixel.cmd runs Python with -I,
    which ignores PYTHONIOENCODING, so on Windows a plain read would decode it in the ANSI code page and
    turn a curly quote into mojibake. Text that isn't UTF-8 is read in this computer's own encoding.
    """
    stdin = stdin or sys.stdin
    data = stdin.buffer.read() if hasattr(stdin, "buffer") else None
    if data is None:
        return stdin.read()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        import locale
        return data.decode(locale.getpreferredencoding(False), errors="replace")


def cmd_app(argv: list[str]) -> int:
    """`ixel app` — Ixel in a window of its own; it stops when the window is closed."""
    import argparse
    import subprocess

    from ixel_mat.gui.server import serve_hosted, serve_window
    from ixel_mat.gui.window import alert, linux_window_command, mac_app, open_window

    parser = argparse.ArgumentParser(prog="ixel app",
                                     description="Open Ixel in a window of its own (served only to this computer). "
                                                 "It stops when you close the window.")
    parser.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: a random free port)")
    parser.add_argument("--browser", action="store_true",
                        help="use an Edge or Chrome app window even where Ixel's native window is installed")
    parser.add_argument("--host", action="store_true",
                        help="for a native window: print the address on stdout and serve until stdin closes")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    if args.host:
        try:
            asyncio.run(serve_hosted(port=args.port))
        except KeyboardInterrupt:
            pass
        return 0
    if not args.browser:
        # The native window starts its own server (`ixel app --host`)
        if sys.platform == "darwin" and (app := mac_app()) is not None:
            if subprocess.call(["/usr/bin/open", str(app)]) == 0:
                return 0
            # macOS wouldn't start it (an older app still running from where it was installed does that)
            console.print(f"\n  [{C['dim']}]{safe_markup(app.name)} didn't open, so Ixel opens in a browser window "
                          f"instead.[/]")
        if sys.platform.startswith("linux") and (command := linux_window_command()) is not None:
            from ixel_mat.config.secrets import child_env
            try:
                # Without the keys Ixel saved: its `ixel app --host` loads them itself, and a link the window
                # opens starts your browser with this environment
                return subprocess.call(command, env=child_env(nested=False))
            except KeyboardInterrupt:
                return 0

    def announce(where: str) -> None:
        console.print(f"  [{C['gold']}]Ixel[/] [{C['dim']}]is open in {safe_markup(where)}. "
                      f"It stops when you close it (or press Ctrl+C).[/]\n")

    console.print(f"\n  [{C['dim']}]Opening Ixel…[/]")
    try:
        opened = asyncio.run(serve_window(open_window, announce=announce, port=args.port))
    except KeyboardInterrupt:
        console.print(f"  [{C['dim']}]Stopped.[/]")
        return 0
    except Exception as exc:  # noqa: BLE001 — from the Start Menu there's no console for a traceback
        alert(f"Ixel couldn't start: {exc}")
        return 1
    if not opened:
        alert("Ixel's window didn't open. Try `ixel gui`, which prints a link to open in any browser.")
        return 1
    return 0


def cmd_docs(argv: list[str]) -> int:
    """`ixel docs` — the docs on ixelai.com, in your browser."""
    import argparse

    from ixel_mat.docs import DOCS_URL, open_docs

    parser = argparse.ArgumentParser(prog="ixel docs", description="Open Ixel's docs in your browser.")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser; just print the address")
    args = parser.parse_args(argv)
    console.print(f"\n  [{C['gold']}]Ixel's docs[/] [{C['dim']}]are at:[/]")
    console.print(f"  {DOCS_URL}", markup=False, highlight=False, soft_wrap=True)
    if not args.no_browser and open_docs():
        console.print(f"  [{C['dim']}]Opened in your browser.[/]")
    console.print()
    return 0


def cmd_forget(argv: list[str]) -> int:
    """`ixel forget`: deletes what Ixel keeps of what you asked and ran."""
    import argparse

    from ixel_mat.forget import WINDOW, forget

    parser = argparse.ArgumentParser(
        prog="ixel forget",
        description="Delete what Ixel keeps of what you asked and ran: your last ixel review conversation, "
                    "the Machines log, and the app window's storage. Your keys, settings, machines and "
                    "usage stats stay. On Linux, Ixel's own GTK window keeps its storage where WebKitGTK "
                    "puts it, which this leaves alone.")
    parser.parse_args(argv)
    found = forget()
    console.print()
    for item in found:
        where = safe_markup(str(item.path))
        if not item.error:
            console.print(f"  [{C['green']}]✓[/] Deleted {safe_markup(item.what)} [{C['dim']}]({where})[/]")
            continue
        console.print(f"  [{C['red']}]✗[/] Couldn't delete {safe_markup(item.what)} [{C['dim']}]({where}): "
                      f"{safe_markup(item.error)}[/]")
        if item.what == WINDOW and os.path.lexists(item.path):  # on Windows, an open window holds it
            console.print(f"    [{C['dim']}]If an Ixel window is open, close it, then run[/] "
                          f"[{C['blue']}]ixel forget[/] [{C['dim']}]again.[/]")
    if not found:
        console.print(f"  [{C['dim']}]Nothing to forget: Ixel isn't keeping any of what you asked or ran.[/]")
    elif os.name == "posix" and any(item.what == WINDOW and not item.error for item in found):
        # On a Mac or Linux the folder goes even while a window has it open, and that window can write
        # what it holds back as it closes
        console.print(f"  [{C['dim']}]If an Ixel window was open, close it and run[/] [{C['blue']}]ixel forget[/] "
                      f"[{C['dim']}]again: a window can save what it holds as it closes.[/]")
    from ixel_mat.forget import gtk_window_folders
    gtk = [str(folder) for folder in gtk_window_folders() if folder.is_dir()]
    if gtk:
        console.print(f"  [{C['dim']}]Ixel's Linux window keeps its own storage in {safe_markup(' and '.join(gtk))}, "
                      "which this leaves alone: delete it with Ixel closed.[/]")
    console.print(f"  [{C['dim']}]Your keys, settings, machines and usage stats are kept.[/]\n")
    return 1 if any(item.error for item in found) else 0


def cmd_update(argv: list[str]) -> int:
    from ixel_mat.update import run_update
    return run_update(argv, say=lambda line: console.print(f"  {safe_markup(line)}"))


def cmd_run():
    """Launch the Rich CLI multi-agent terminal (mat.py)."""
    from ixel_mat.mat import main
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # ctrl+c twice while a review was stopping: leave without a traceback
        console.print(f"\n  [{C['dim']}]Interrupted.[/]\n")
        sys.exit(130)



def _tolerate_unencodable_output() -> None:
    """A ✓ piped to a cp1252 file on Windows should become '?', not a crash."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def stop_like_ctrl_c(give_up_after: float | None = None) -> None:
    """
    `kill` (SIGTERM) and closing the terminal (SIGHUP) stop a command the way Ctrl+C does, so the model
    programs it started stop with it and it cleans up after itself. By default they'd end Ixel at once and
    leave those programs running on their own. give_up_after: seconds after which Ixel exits even if
    something is still waiting (the plugin's stdin is read by a thread that can't be stopped).
    """
    if os.name != "posix":
        return

    def like_ctrl_c(signum, frame):
        if give_up_after is not None:
            import threading
            timer = threading.Timer(give_up_after, os._exit, [130])
            timer.daemon = True
            timer.start()
        handler = signal.getsignal(signal.SIGINT)  # asyncio's while a run is going: it cancels the run
        if callable(handler):
            handler(signal.SIGINT, frame)
        else:
            raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(sig) is signal.SIG_DFL:  # not one that's ignored on purpose (nohup)
            signal.signal(sig, like_ctrl_c)


# Commands that start model programs or serve the app, and stop like Ctrl+C on SIGTERM and SIGHUP
STOP_LIKE_CTRL_C = ("review", "ask", "image", "mcp", "gui", "app")


def main():
    _tolerate_unencodable_output()
    # The model programs where their installers put them, also when Ixel was opened from the app menu, whose
    # PATH has none of what your shell's startup file adds (OpenCode's ~/.opencode/bin)
    from ixel_mat.agents.launch import add_install_folders
    add_install_folders()
    # Whatever the command: each review question and answer goes once it's a day old, Machines log lines
    # once they're 30 days old (a stat or two, and a rewrite or a delete only when something's due)
    from ixel_mat.forget import tidy
    tidy()
    try:
        _main()
    except KeyboardInterrupt:  # Ctrl+C, kill or a closed terminal: stopped, no traceback
        sys.exit(130)


def _main():
    args = sys.argv[1:]
    raw_cmd = args[0] if args else ''
    if raw_cmd in ('--help', '-h'):
        resolved = 'help'
    elif raw_cmd in ('--version', '-v'):
        resolved = 'version'
    else:
        resolved = resolve_command_name(raw_cmd, mode='cli')

    commands = {
        'run': cmd_run,
        'setup': cmd_setup,
        'status': cmd_status,
        'config': cmd_config,
        'agents': cmd_agents,
        'saves': cmd_saves,
        'version': cmd_version,
        'help': cmd_help,
    }

    if resolved in STOP_LIKE_CTRL_C:
        # The plugin's stdin is read by a thread nothing can stop: past a few seconds of cleanup, just exit
        stop_like_ctrl_c(give_up_after=5.0 if resolved == "mcp" else None)
    if resolved == 'review':
        sys.exit(cmd_review(args[1:]))
    if resolved == 'ask':
        sys.exit(cmd_ask(args[1:]))
    if resolved == 'image':
        sys.exit(cmd_image(args[1:]))
    if resolved == 'model':
        sys.exit(cmd_model(args[1:]))
    if resolved == 'mcp':
        sys.exit(cmd_mcp(args[1:]))
    if resolved == 'gui':
        sys.exit(cmd_gui(args[1:]))
    if resolved == 'app':
        sys.exit(cmd_app(args[1:]))
    if resolved == 'update':
        sys.exit(cmd_update(args[1:]))
    if resolved == 'docs':
        sys.exit(cmd_docs(args[1:]))
    if resolved == 'forget':
        sys.exit(cmd_forget(args[1:]))
    if resolved == 'triage':
        sys.exit(cmd_triage(args[1:]))
    if resolved == 'machines':
        from ixel_mat.machines.cli import main as machines_main
        sys.exit(machines_main(args[1:]))
    if resolved == 'doctor':
        sys.exit(cmd_doctor(args[1:]))

    if isinstance(resolved, tuple) and resolved[0] == 'ambiguous':
        console.print(f"  [{C['red']}]Ambiguous command: {safe_markup(raw_cmd)}[/]")
        console.print(f"  [{C['dim']}]Matches: {', '.join(resolved[1])}[/]\n")
        sys.exit(1)

    handler = commands.get(resolved)
    if handler and resolved != 'help' and any(a in ('-h', '--help') for a in args[1:]):
        # These take no options: --help shows what the command does instead of running it
        cmd = next(c for c in COMMANDS if c.name == resolved and c.mode == 'cli')
        console.print(f"usage: {cmd.usage}\n\n  {cmd.description}", markup=False, highlight=False)
        return
    if handler:
        handler()
    else:
        console.print(f"  [{C['red']}]Unknown command: {safe_markup(raw_cmd)}[/]")
        console.print(f"  [{C['dim']}]Run 'ixel help' for available commands[/]\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
