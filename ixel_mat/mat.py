#!/usr/bin/env python3
"""
Ixel MAT — Multi-Agent Terminal
IxelOS-branded CLI for parallel agent comparison.

Drawn inline, not full screen: your terminal stays your terminal (scrollback, selection, copy-paste).
The prompt (history, completion, status rule) is prompt_ui.py; what it draws and completes is tui.py;
the palette is theme.py.
"""
from __future__ import annotations

import asyncio
import select
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, TypeVar

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich.markdown import Markdown

from ixel_mat import __version__, review_ui, stats, tui
from ixel_mat.agents.base import BaseAgent
from ixel_mat.asking import Confirm, LinePrompt, Prompt
from ixel_mat.config.secrets import load_env
from ixel_mat.config.loader import load_config, build_agent_configs, validate_config, print_config_status
from ixel_mat.commands import build_help_groups, resolve_command_name
from ixel_mat.hyperlinks import hyperlink_text
from ixel_mat.sanitize import safe_markup, sanitize_terminal_text
from ixel_mat.theme import C, PROMPT_ARROW, moon
from ixel_mat.update import start_background_notice

# Load the saved keys FIRST (before config reads token_env)
_loaded_secrets = load_env()
from ixel_mat.modes.full import FullModeDispatcher
from ixel_mat.conversation import continued
from ixel_mat.modes.review import EarlierTurn, ReviewMode, ReviewResult, run_review
from ixel_mat.triage import parse_triage_settings
from ixel_mat.usage import parse_pricing
from ixel_mat.material import Material, MaterialError, code_for_review
from ixel_mat.runtime import (
    AUTO, ReviewSettings, Settings, auto_warnings, choose_mode, connect_agents as _connect_agents,
    local_agent_names, parse_review_settings, parse_saver_settings,
)

console = Console()

# ── Config-driven agents ──────────────────────────────────────────────────────
_CONFIG = load_config()
_AGENT_CONFIGS, _CONFIG_WARNINGS = build_agent_configs(_CONFIG)
_REVIEW, _REVIEW_WARNINGS = parse_review_settings(_CONFIG, set(_AGENT_CONFIGS))
_SAVER, _SAVER_WARNINGS = parse_saver_settings(_CONFIG, set(_AGENT_CONFIGS), _REVIEW)
_TRIAGE, _TRIAGE_WARNINGS = parse_triage_settings(_CONFIG, _AGENT_CONFIGS)
_CONFIG_WARNINGS += (_REVIEW_WARNINGS + _SAVER_WARNINGS + _TRIAGE_WARNINGS + auto_warnings(_REVIEW, _TRIAGE)
                     + parse_pricing(_CONFIG)[1])
_PASTE_STATE = {"count": 0}
_LAST_REVIEW: dict[str, ReviewResult] = {}
# This session's conversation: each review sees the last few questions and verdicts (/new clears it)
_CONVERSATION: list[EarlierTurn] = []


# ── Splash ────────────────────────────────────────────────────────────────────

def plain_mode_text() -> str:
    """What a question typed without a command does, for the welcome card."""
    plain = _REVIEW.plain
    if plain == "compare":
        return "compare · every model answers side by side"
    if plain == AUTO:
        return "auto · Triage picks how much checking a question needs"
    return f"{plain} · " + " → ".join(ReviewMode(plain).rounds)


def print_splash():
    """The welcome card: the IxelOS moon, the name, what a plain question does, and what to try."""
    rows = [("mode", plain_mode_text())]
    try:
        rows.append(("folder", tui.tilde(Path.cwd())))
    except OSError:  # the folder was deleted from under us
        pass
    console.print(tui.splash(console.width, __version__, rows))


# ── Agent management ──────────────────────────────────────────────────────────

async def connect_agents() -> dict[str, BaseAgent]:
    """Connect all configured agents in parallel (with Private on, only the ones on your own computers, so
    nothing here can ask another). Returns the connected ones."""

    def report(config, error):
        if error is None:
            console.print(f"  [{C['green']}]✓[/] [{C['blue']}]{safe_markup(config.label)}[/]  [{C['dim']}]connected[/]")
        else:
            console.print(f"  [{C['red']}]✗[/] [{C['blue']}]{safe_markup(config.label)}[/]  [{C['red']}]{safe_markup(error)}[/]")

    settings = _settings()
    configs = {name: cfg for name, cfg in _AGENT_CONFIGS.items() if settings.yours(name)}
    agents = await _connect_agents(configs, on_result=report)
    console.print()
    return agents


async def disconnect_all(agents: dict[str, BaseAgent]):
    for agent in agents.values():
        try:
            await agent.disconnect()
        except Exception:
            pass


# ── Commands ──────────────────────────────────────────────────────────────────

def print_help(keys: bool = True):
    console.print(f"\n  [{C['gold']}]Ixel MAT[/] [{C['dim']}]— Commands[/]")
    groups = build_help_groups(mode='mat')
    width = max(len(cmd) for _, rows in groups for cmd, _ in rows)  # one column of commands across the groups
    for title, rows in groups:
        if title:
            console.print(f"\n  [{C['violet']}]{safe_markup(title)}[/]")
        console.print(tui.two_columns(rows, C['blue'], C['dim'], left_width=width))
    if keys:
        console.print(f"\n  [{C['violet']}]Keys[/]")
        console.print(tui.two_columns(tui.KEYS, C['blue'], C['dim']))
    console.print()


def print_agents(agents: dict[str, BaseAgent]):
    console.print(f"\n  [{C['gold']}]Agents[/]\n")
    for name, agent in agents.items():
        if agent.is_connected:
            console.print(f"    [{C['green']}]●[/] [{C['blue']}]{safe_markup(name)}[/]  [{C['dim']}]{safe_markup(agent.label)}[/]  [{C['green']}]connected[/]")
        else:
            console.print(f"    [{C['dim']}]○[/] [{C['blue']}]{safe_markup(name)}[/]  [{C['dim']}]{safe_markup(agent.label)}[/]  [{C['red']}]disconnected[/]")
    console.print()


def _has_markdown(text: str) -> bool:
    return any(marker in text for marker in (
        "| ", "---|", "## ", "### ", "**", "```", "- [ ]", "1. ",
    ))


def _print_answer(text: str):
    """Render an agent answer — uses Rich Markdown if it contains markdown syntax."""
    text = sanitize_terminal_text(text)
    if _has_markdown(text):
        md = Markdown(text, code_theme="monokai", hyperlinks=False)
        console.print()
        console.print(Panel(md, border_style=C["dim"], padding=(1, 2), width=min(console.width - 4, 100)))
        console.print()
    else:
        # Wrap plain text nicely
        for line in text.split("\n"):
            if line.strip():
                linked = hyperlink_text(f"    {line}")
                linked.stylize(C["moon"], 0, len(linked.plain))
                console.print(linked, overflow="fold")


def _response_attr(response, key: str, default=None):
    if response is None:
        return default
    if isinstance(response, dict):
        return response.get(key, default)
    return getattr(response, key, default)


def _format_elapsed(seconds: float) -> str:
    return f"{max(seconds, 0):.1f}s"


def describe_large_paste(text: str) -> str:
    stripped = text.strip()
    return f"{len(stripped)} chars / {len(stripped.splitlines()) or 1} lines"


def should_confirm_large_paste(text: str, char_threshold: int = 2000, line_threshold: int = 12) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return len(stripped) >= char_threshold or len(stripped.splitlines()) >= line_threshold


def normalize_interactive_command(text: str) -> str:
    stripped = text.strip()
    lowered = stripped.lower()
    mapping = {
        "ixel help": "/help",
        "ixel agents": "/agents",
        "ixel status": "/agents",
        "ixel config": "/config",
        "ixel version": "/help",
    }
    return mapping.get(lowered, stripped)


def format_prompt_preview(text: str, paste_state: dict[str, int] | None = None) -> str:
    stripped = text.strip()
    lines = stripped.splitlines()
    if len(lines) <= 1 and len(stripped) <= 240:
        return stripped
    state = paste_state if paste_state is not None else _PASTE_STATE
    state["count"] = state.get("count", 0) + 1
    extra_lines = max(len(lines) - 1, 0)
    if extra_lines > 0:
        return f"[paste #{state['count']} +{extra_lines} lines]"
    return f"[paste #{state['count']} +{max(len(stripped) - 1, 0)} chars]"


async def _prompt_async(label: str) -> str:
    return await asyncio.to_thread(LinePrompt.ask, label)


async def _confirm_async(label: str, default: bool = True) -> bool:
    return await asyncio.to_thread(Confirm.ask, label, default=default)


async def read_burst_submission(
    prompt_fn,
    main_prompt: str,
    continuation_prompt: str | None = None,
    burst_window: float = 0.05,
    stdin=None,
    select_fn=None,
) -> str:
    first = await prompt_fn(main_prompt)
    parts = [first]
    if "\n" in first:
        return first
    if stdin is None and not sys.stdin.isatty():
        return first  # piped in: each line is a question (or a command) of its own, not part of a paste

    stream = stdin or sys.stdin
    selector = select_fn or select.select
    if not hasattr(stream, "readline"):
        return first

    while True:
        try:
            ready, _, _ = selector([stream], [], [], burst_window)
        except Exception:
            break
        if not ready:
            break
        nxt = stream.readline()
        if nxt == "":
            break
        parts.append(str(nxt).rstrip("\r\n"))
    return "\n".join(parts)


def build_full_status_lines(agent_states: dict[str, dict], now: float | None = None) -> list[str]:
    now = time.perf_counter() if now is None else now
    total = len(agent_states)
    responded = sum(1 for state in agent_states.values() if state.get("status") == "done")
    lines = [f"{responded}/{total} agents responded"]

    for name, state in agent_states.items():
        label = state.get("label", name)
        status = state.get("status", "pending")
        if status == "running":
            started_at = state.get("started_at", now)
            lines.append(f"{label} — processing... {_format_elapsed(now - started_at)}")
            continue
        response = state.get("response")
        if response is None:
            lines.append(f"{label} — waiting")
            continue
        answer = _response_attr(response, "answer", "") or "(no answer)"
        latency_ms = _response_attr(response, "latency_ms", 0)
        marker = "failed" if _response_attr(response, "failed", False) else f"{latency_ms}ms"
        lines.append(f"{label} — {marker}")
        lines.append(answer.split("\n")[0][:120])
    return lines


def _build_full_renderable(prompt: str, agent_states: dict[str, dict]):
    now = time.perf_counter()
    total = len(agent_states)
    responded = sum(1 for state in agent_states.values() if state.get("status") == "done")
    progress = Text()
    progress.append(f"{responded}/{total}", style=C["green"])
    progress.append(" agents responded", style=C["dim"])

    panels = []
    for name, state in agent_states.items():
        label = state.get("label", name)
        status = state.get("status", "pending")
        if status == "running":
            started_at = state.get("started_at", now)
            body = Text()
            body.append("processing... ", style=C["dim"])
            body.append(_format_elapsed(now - started_at), style=C["gold"])
            border_style = C["violet"]
        elif status == "done":
            response = state.get("response")
            answer = sanitize_terminal_text(_response_attr(response, "answer", "")) or "(no answer)"
            failed = _response_attr(response, "failed", False)
            # A plain answer has no confidence of its own: only a structured one says it
            confidence = None if _response_attr(response, "degraded", False) else _response_attr(response, "confidence", None)
            evidence = [sanitize_terminal_text(e) for e in _response_attr(response, "evidence", []) or []]
            followup = sanitize_terminal_text(_response_attr(response, "followup", ""))
            extra = []
            if confidence and hasattr(confidence, "value"):
                extra.append(Text.assemble(("Confidence: ", C["dim"]), (confidence.value, C["blue"])))
            if evidence:
                extra.append(Text.assemble(("Evidence: ", C["dim"]), (", ".join(evidence[:5]), C["violet"])))
            if followup:
                extra.append(Text.assemble(("Next: ", C["dim"]), (followup, C["moon"])))
            answer_renderable = Markdown(answer, code_theme="monokai", hyperlinks=False) if _has_markdown(answer) else Text(answer, style=C["moon"])
            body = Group(answer_renderable, *extra) if extra else answer_renderable
            border_style = C["gold"] if failed else C["green"]
        else:
            body = Text("queued...", style=C["dim"])
            border_style = C["dim"]

        panels.append(
            Panel(
                body,
                title=f"[{C['blue']}]{safe_markup(label)}[/]",
                border_style=border_style,
                padding=(1, 2),
            )
        )

    header = Group(
        Text.assemble(("▸ ", C["gold"]), (sanitize_terminal_text(prompt), C["moon"])),
        progress,
        Text("", style=C["dim"]),
    )
    return Group(header, Columns(panels, equal=True, expand=True))


def _flag_value(text: str, name: str) -> tuple[str, str]:
    """A flag's value from the start of text, and what follows. A value in quotes can hold
    spaces (C:\\Users\\Jane Doe\\app.py); backslashes are kept as they are."""
    text = text.lstrip()
    if text[:1] in ('"', "'"):
        end = text.find(text[0], 1)
        if end == -1:
            raise ValueError(f"Missing closing quote in the value for {name}")
        return text[1:end], text[end + 1:]
    parts = text.split(None, 1)
    if not parts:
        raise ValueError(f"Missing value for {name}")
    return parts[0], (parts[1] if len(parts) > 1 else "")


def take_flags(raw: str, value_flags: set[str], bool_flags: frozenset[str] = frozenset(),
               list_flags: frozenset[str] = frozenset()) -> tuple[dict, str]:
    """
    Consume leading --flags from a command line and return (flags, rest).
    The rest is returned verbatim, so quotes and apostrophes in a question
    survive. A bare `--` ends the flags. A list flag can be given more than once.
    """
    flags: dict[str, str | bool] = {}
    rest = raw.strip()
    while rest.startswith("--"):
        parts = rest.split(None, 1)
        token, remainder = parts[0], (parts[1] if len(parts) > 1 else "")
        if token == "--":
            rest = remainder
            break
        name, has_value, _ = token.partition("=")
        if name in bool_flags and not has_value:
            flags[name] = True
            rest = remainder
        elif name in value_flags or name in list_flags:
            value, rest = _flag_value(rest[len(name) + 1:] if has_value else remainder, name)
            if name in list_flags:
                flags.setdefault(name, []).append(value)
            else:
                flags[name] = value
            rest = rest.strip()
        else:
            raise ValueError(f"Unknown option {name}")
    return flags, rest.strip()


def _without_flags(raw: str, drop: set[str], value_flags: set[str]) -> tuple[str, list[str]]:
    """raw with the value flags named in drop (and their values) taken out of its leading flags, and
    which ones were. The leading flags are read as take_flags reads them; the rest is kept as typed."""
    kept, dropped, rest = [], [], raw.strip()
    while rest.startswith("--"):
        token = rest.split(None, 1)[0]
        if token == "--":
            break
        name, has_value, _ = token.partition("=")
        if name in drop or name in value_flags:
            try:
                _, after = _flag_value(rest[len(name) + 1:] if has_value else rest[len(token):], name)
            except ValueError:
                break  # /review says what's wrong
            if name in drop:
                dropped.append(name)
            else:
                kept.append(rest[:len(rest) - len(after)].strip())
            rest = after.strip()
        else:
            kept.append(token)
            rest = rest[len(token):].strip()
    return " ".join(kept + [rest]).strip(), dropped


REVIEW_VALUE_FLAGS = {"--mode", "--moderator", "--timeout", "--base"}
REVIEW_BOOL_FLAGS = frozenset({"--quick", "--deep", "--review", "--saver", "--auto", "--diff", "--staged",
                               "--new-files", "--allow-secrets"})
REVIEW_LIST_FLAGS = frozenset({"--file"})
# Old /consensus options /review has no use for: a review goes on with the answers that come back.
# (Its --timeout is /review's.)
CONSENSUS_ONLY_FLAGS = {"--min-responses"}


def renamed_command(name: str, remainder: str) -> tuple[str, str, str] | None:
    """A command that was folded into another: the (command, arguments) to run instead, and a line saying so."""
    if name in ("consensus", "cons"):  # answers, then one verdict: what /review --quick does
        args, dropped = _without_flags(remainder, CONSENSUS_ONLY_FLAGS, REVIEW_VALUE_FLAGS | REVIEW_LIST_FLAGS)
        note = f"/{name} is now /review --quick."
        if dropped:
            note += f" {', '.join(dropped)} isn't needed any more (a review goes on with the answers it gets)."
        return "review", f"--quick {args}", note
    return None


REVIEW_USAGE = ("Usage: /review [--quick | --deep | --saver | --auto | --mode M] [--moderator AGENT] "
                "[--timeout SECONDS] [--diff | --staged | --base REF] [--new-files] [--file PATH]… [--allow-secrets] "
                "<question>")


@dataclass
class ReviewOptions:
    question: str
    mode: ReviewMode
    moderator: str | None
    timeout: float
    auto: bool = False  # Triage picks quick / review / deep; mode runs if it can't
    diff: str | None = None   # code to review: "uncommitted", "staged" or "base" (see material.git_diff)
    base: str | None = None
    files: list[str] = field(default_factory=list)
    allow_secrets: bool = False  # send code even if it looks like it holds a key
    new_files: bool = False  # with a diff, the new files git doesn't track yet too


def parse_review_args(raw: str, defaults: ReviewSettings | None = None) -> ReviewOptions:
    defaults = defaults or ReviewSettings()
    flags, question = take_flags(raw, REVIEW_VALUE_FLAGS, REVIEW_BOOL_FLAGS, REVIEW_LIST_FLAGS)
    mode, auto = defaults.mode, defaults.auto
    for shortcut in ("--quick", "--review", "--deep", "--saver"):
        if flags.get(shortcut):
            mode, auto = ReviewMode(shortcut[2:]), False
    if flags.get("--auto"):
        auto = True
    if "--mode" in flags:
        if flags["--mode"] == AUTO:
            auto = True
        else:
            try:
                mode, auto = ReviewMode(flags["--mode"]), False
            except ValueError:
                raise ValueError("--mode must be quick, review, deep, saver or auto") from None
    timeout = defaults.timeout
    if "--timeout" in flags:
        try:
            timeout = float(flags["--timeout"])
        except ValueError:
            raise ValueError("--timeout must be a number of seconds") from None
        if timeout <= 0:
            raise ValueError("--timeout must be > 0")
    diffs = [d for d, on in (("uncommitted", flags.get("--diff")), ("staged", flags.get("--staged")),
                             ("base", "--base" in flags)) if on]
    if len(diffs) > 1:
        raise ValueError("Pick one of --diff, --staged and --base")
    if flags.get("--new-files") and diffs[:1] not in (["uncommitted"], ["base"]):
        raise ValueError("--new-files goes with --diff or --base")
    files = list(flags.get("--file") or [])
    if not question and not diffs and not files:
        raise ValueError(REVIEW_USAGE)
    return ReviewOptions(question, mode, flags.get("--moderator") or defaults.moderator, timeout, auto,
                         diffs[0] if diffs else None, flags.get("--base"), files, bool(flags.get("--allow-secrets")),
                         bool(flags.get("--new-files")))


T = TypeVar("T")


async def _interruptible(work: Awaitable[T]) -> T | None:
    """Await work so that ctrl+c cancels it and nothing else: the models are stopped, you're back at the
    prompt and the session goes on (at the prompt itself ctrl+c is a key; see prompt_ui). None if cancelled."""
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(work)
    hit = False

    def stop() -> None:
        nonlocal hit
        if not task.done():  # a ctrl+c as the run finishes isn't a cancel
            hit = True
            task.cancel()

    previous = None
    restored = False

    def restore() -> None:
        nonlocal restored
        if previous is not None and not restored:
            restored = True
            signal.signal(signal.SIGINT, previous)

    def on_sigint(*_) -> None:
        if hit:  # a second ctrl+c: it's slow to stop, so stop waiting for it
            restore()  # before unwinding past this function's own finally, which may only run much later
            raise KeyboardInterrupt
        loop.call_soon_threadsafe(stop)

    try:
        previous = signal.signal(signal.SIGINT, on_sigint)
    except ValueError:  # not the main thread, so there's no ctrl+c to catch
        pass
    try:
        return await task
    except asyncio.CancelledError:
        if not hit:
            raise
        console.print(f"\n  [{C['gold']}]⏹[/] [{C['moon']}]Cancelled.[/] "
                      f"[{C['dim']}]The models were stopped, and your conversation is unchanged.[/]\n")
        return None
    finally:
        restore()


async def run_full(prompt: str, agents: dict[str, BaseAgent]):
    """/compare: every agent answers side by side. ctrl+c cancels it."""
    await _interruptible(_compare(prompt, agents))


async def _compare(prompt: str, agents: dict[str, BaseAgent]):
    """Run /full via FullModeDispatcher — single orchestration path."""
    settings = _settings()
    connected = [a for name, a in agents.items() if a.is_connected and settings.yours(name)]
    if not connected:
        console.print(f"  [{C['red']}]No connected agents.[/]")
        return

    dispatcher = FullModeDispatcher(connected, timeout=60.0)
    agent_states = {
        agent.name: {
            "label": agent.label,
            "status": "pending",
            "started_at": None,
            "response": None,
        }
        for agent in connected
    }

    async def on_start(name: str):
        state = agent_states[name]
        state["status"] = "running"
        state["started_at"] = time.perf_counter()

    async def on_done(resp):
        state = agent_states[resp.agent]
        state["status"] = "done"
        state["response"] = resp

    dispatch_task = asyncio.create_task(
        dispatcher.dispatch(prompt, on_agent_start=on_start, on_agent_done=on_done)
    )
    prompt_preview = format_prompt_preview(prompt)

    try:
        with Live(_build_full_renderable(prompt_preview, agent_states), console=console, refresh_per_second=10) as live:
            while not dispatch_task.done():
                live.update(_build_full_renderable(prompt_preview, agent_states))
                await asyncio.sleep(0.1)
            result = await dispatch_task
            live.update(_build_full_renderable(prompt_preview, agent_states))
    except BaseException:
        # Cancelled (ctrl+c): the dispatch is a task of its own, so stop it too, or the models keep answering
        dispatch_task.cancel()
        await asyncio.gather(dispatch_task, return_exceptions=True)
        raise

    # Summary
    answered = sum(1 for r in result.responses if not r.failed)
    avg_ms = sum(r.latency_ms for r in result.responses) // len(result.responses) if result.responses else 0
    console.print(
        f"  [{C['dim']}]── /compare ── {len(result.responses)} agents ── "
        f"{answered} answered ── avg {avg_ms}ms ──[/]\n"
    )


# ── Main loop ─────────────────────────────────────────────────────────────────

def _settings() -> Settings:
    return Settings(_CONFIG, _AGENT_CONFIGS, [], _REVIEW, _SAVER, _TRIAGE)


def review_panel(agents: dict[str, BaseAgent], mode: ReviewMode = ReviewMode.REVIEW) -> list[BaseAgent]:
    """Connected agents for this mode: the review panel, or drafters + verifier for saver."""
    settings = _settings()
    connected = {name: a for name, a in agents.items() if a.is_connected and settings.yours(name)}
    if mode is ReviewMode.SAVER:
        verifier = settings.verifier
        drafters = _SAVER.drafters or [n for n in connected if n != _SAVER.verifier]
        names = [n for n in drafters if n != verifier] + ([verifier] if verifier else [])
        return [connected[n] for n in names if n in connected]
    return [a for name, a in connected.items() if not _REVIEW.agents or name in _REVIEW.agents]


async def run_review_cmd(raw: str, agents: dict[str, BaseAgent]):
    """Run /review — answer, anonymous peer review, (revise), verdict."""
    try:
        opts = parse_review_args(raw, _REVIEW)
    except ValueError as e:
        console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(e)}[/]")
        return

    await run_review_opts(opts, agents)


def _review_material(opts: ReviewOptions) -> Material | None:
    """The code a /review asked for, checked; the question gets a default if there wasn't one."""
    material, opts.question = code_for_review(opts.question, opts.diff, opts.base, opts.files,
                                              allow_secrets=opts.allow_secrets, new_files=opts.new_files)
    return material


async def run_review_opts(opts: ReviewOptions, agents: dict[str, BaseAgent]):
    """A review, start to report. ctrl+c cancels it."""
    await _interruptible(_review(opts, agents))


async def _review(opts: ReviewOptions, agents: dict[str, BaseAgent]):
    try:
        # git can take a while on a big repository: off the event loop, so the terminal stays responsive
        material = await asyncio.to_thread(_review_material, opts)
    except MaterialError as e:
        console.print(f"  [{C['red']}]✗[/] [{C['dim']}]{safe_markup(e)}[/]")
        return
    settings = _settings()
    if opts.moderator and opts.moderator not in settings.agent_configs:
        console.print(f"  [{C['red']}]✗[/] [{C['dim']}]--moderator: there's no agent named "
                      f"{safe_markup(repr(opts.moderator))} (agents: {safe_markup(', '.join(settings.agent_configs))})[/]")
        return
    # Auto mode asks Triage first (well under a second); the decision shows with the run
    mode, decision = await choose_mode(settings, AUTO if opts.auto else opts.mode.value, opts.question,
                                       list(_CONVERSATION))
    moderator = opts.moderator
    if moderator and moderator == _REVIEW.moderator:
        moderator = settings.moderator  # the one in your settings, which sits out under Private
    problem = settings.private_problem(mode)
    if not problem and moderator and not settings.yours(moderator):
        problem = f"Private is on, and {moderator} isn't on your computers, so it can't write the verdict."
    if problem:
        console.print(f"  [{C['red']}]✗[/] [{C['dim']}]{safe_markup(problem)}[/]")
        return
    panel = review_panel(agents, mode)
    if not panel:
        console.print(f"  [{C['red']}]No connected agents on the review panel.[/]")
        return

    options = settings.run_options(mode)
    options.update(moderator=moderator, timeout=opts.timeout, earlier=list(_CONVERSATION), auto=decision,
                   material=material)
    progress = review_ui.ReviewProgress(opts.question, mode, len(panel), echo=console.print)
    console.print()
    if material is not None:
        console.print(f"  [{C['violet']}]↳[/] [{C['dim']}]Reviewing {safe_markup(material.title)} · "
                      f"{safe_markup(material.summary())}[/]")
        for note in material.notes:
            console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(note)}[/]")
    if _CONVERSATION:
        earlier = ("your last question and its answer" if len(_CONVERSATION) == 1
                   else f"your last {len(_CONVERSATION)} questions and their answers")
        console.print(f"  [{C['violet']}]↳[/] [{C['dim']}]Follow-up: the panel also sees {earlier}. "
                      f"[/][{C['blue']}]/new[/][{C['dim']}] starts fresh.[/]")
    console.print(progress.header)
    # Each round that's over scrolls up into the terminal's history; only the one running is live
    with Live(progress, console=console, refresh_per_second=10, transient=True):
        try:
            result = await run_review(opts.question, panel, on_event=progress.on_event, **options)
        finally:
            progress.flush()
    _LAST_REVIEW["result"] = result
    _CONVERSATION[:] = continued(_CONVERSATION, result)
    for renderable in review_ui.report(result):
        console.print(renderable)
    for renderable in review_ui.stats_lines(stats.record_run(result, local_agent_names(_AGENT_CONFIGS))):
        console.print(renderable)


async def run_plain_question(text: str, agents: dict[str, BaseAgent]) -> None:
    """A question typed without a command: [review] plain_questions says what runs (a quick review by default)."""
    if _REVIEW.plain == "compare":
        await run_full(text, agents)
    else:
        # No flag parsing, so a question that starts with "--" stays a question
        auto = _REVIEW.plain == AUTO
        mode = _REVIEW.mode if auto else ReviewMode(_REVIEW.plain)
        await run_review_opts(ReviewOptions(text, mode, _REVIEW.moderator, _REVIEW.timeout, auto), agents)


def print_last_answers():
    result = _LAST_REVIEW.get("result")
    if result is None or not result.answers:
        console.print(f"  [{C['dim']}]No review yet. Try /review <question>.[/]")
        return
    console.print()
    for panel in review_ui.answer_panels(result, full=True):
        console.print(panel)
    console.print()


def _paste_placeholder(text: str) -> str | None:
    """What the prompt shows in place of a big paste (the paste itself is what gets sent); None for a small one."""
    return format_prompt_preview(text, _PASTE_STATE) if should_confirm_large_paste(text) else None


def _make_prompt_ui(agents: dict[str, BaseAgent]):
    """The interactive prompt (history, completion, status line), or None where there's no terminal to draw
    on or prompt_toolkit can't use it: lines are then read the plain way."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        from ixel_mat.prompt_ui import PromptUI
        return PromptUI(
            status=lambda: tui.Status(_REVIEW.plain, sum(1 for a in agents.values() if a.is_connected),
                                      len(_CONVERSATION)),
            agents=lambda: list(agents),
            placeholder=_paste_placeholder,
        )
    except Exception as exc:  # not installed, or a terminal it can't drive (e.g. mintty on Windows)
        console.print(f"  [{C['dim']}]Reading plain lines, without history or completion "
                      f"({safe_markup(f'{type(exc).__name__}: {exc}')}).[/]")
        return None


def _show_update_notice(notice: str | None) -> None:
    if notice:
        console.print(f"  [{C['gold']}]↑[/] [{C['dim']}]{safe_markup(notice)}[/]\n")


async def main():
    issues = validate_config(_CONFIG)
    if any("token not set" in i for i in issues):
        for issue in issues:
            console.print(f"  [{C['red']}]⚠ {safe_markup(issue)}[/]")
        console.print(f"  [{C['dim']}]Run: /config to see full status[/]\n")

    print_splash()

    # First run: nothing configured yet (local models and CLI logins need no secrets)
    if _CONFIG.get("_error"):
        console.print(f"  [{C['red']}]Your config file couldn't be read:[/] [{C['dim']}]{safe_markup(_CONFIG['_error'])}[/]")
        console.print(f"  [{C['dim']}]Fix it, or write a new one with:[/] [{C['blue']}]ixel setup[/]\n")
    elif not _AGENT_CONFIGS:
        console.print(f"  [{C['gold']}]First time?[/] [{C['dim']}]Run setup to configure agents:[/]")
        console.print(f"    [{C['blue']}]ixel setup[/]\n")

    # Show config warnings
    for warn in _CONFIG_WARNINGS:
        console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]{safe_markup(warn)}[/]")
    if _CONFIG_WARNINGS:
        console.print()

    update_ready = start_background_notice(_CONFIG)
    agents = await connect_agents()

    if not agents and not _AGENT_CONFIGS:
        return  # the first-run (or unreadable config) message above says what to do
    settings = _settings()
    if not agents and settings.private and not any(settings.yours(n) for n in _AGENT_CONFIGS):
        console.print(f"  [{C['red']}]✗[/] [{C['dim']}]{safe_markup(settings.private_problem(ReviewMode.REVIEW))}[/]")
        return
    if not agents:
        console.print(f"  [{C['red']}]No agents connected.[/] [{C['dim']}]Run [/][{C['blue']}]ixel status[/]"
                      f"[{C['dim']}] to see why, or [/][{C['blue']}]ixel setup[/][{C['dim']}] to add some.[/]")
        return

    ready = [getattr(a, "label", name) for name, a in agents.items() if a.is_connected]
    console.print(f"  [{C['green']}]✦[/] [{C['moon']}]{len(ready)} {'agent' if len(ready) == 1 else 'agents'} ready[/]"
                  f"  [{C['dim']}]{safe_markup(' · '.join(ready))}[/]")
    if settings.private:
        out = settings.sitting_out()
        console.print(f"  [{C['violet']}]↳[/] [{C['dim']}]Private: only models on your own computers answer"
                      + (f" ({safe_markup(', '.join(out))} sit out)" if out else "") + ".[/]")
    prompt_ui = _make_prompt_ui(agents)
    plain = ("see every answer side by side" if _REVIEW.plain == "compare"
             else "get the panel's verdict (Triage picks the mode)" if _REVIEW.plain == AUTO
             else f"get the panel's verdict ({_REVIEW.plain} mode)")
    console.print(f"  [{C['dim']}]Type a question to {plain}. [/][{C['blue']}]/help[/][{C['dim']}] lists the rest.[/]")

    try:
        while True:
            _show_update_notice(update_ready())  # never waits: shown at the first prompt after it's ready
            try:
                if prompt_ui is not None:
                    try:
                        user_input = await prompt_ui.read()
                    except (EOFError, KeyboardInterrupt):
                        raise
                    except Exception as exc:  # the terminal wouldn't take it: carry on with plain lines
                        console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]The line editor stopped "
                                      f"({safe_markup(f'{type(exc).__name__}: {exc}')}); reading plain lines.[/]")
                        prompt_ui = None
                        continue
                else:
                    glyph, color = moon(len(_CONVERSATION))
                    user_input = await read_burst_submission(
                        _prompt_async,
                        main_prompt=f"  [{color}]{glyph}[/] [{C['dim']}]{PROMPT_ARROW}[/]",
                        continuation_prompt=f"  [{color}]{glyph}[/] [{C['dim']}]…[/]",
                        burst_window=0.05,
                    )
            except (EOFError, KeyboardInterrupt):
                break

            try:
                text = normalize_interactive_command(user_input)
                if not text:
                    continue
                if text != user_input.strip():
                    console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]Interpreting as {text} inside MAT.[/]\n")

                # At the interactive prompt a big paste already showed as a placeholder you chose to send
                if prompt_ui is None and should_confirm_large_paste(text):
                    paste_info = describe_large_paste(text)
                    ok = await _confirm_async(
                        f"  [{C['gold']}]Large paste detected:[/] [{C['dim']}]{paste_info}[/]  [{C['moon']}]Send as one prompt?[/]",
                        default=True,
                    )
                    if not ok:
                        console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]Paste cancelled.[/]\n")
                        continue

                if text.startswith('/'):
                    body = text[1:]
                    raw_name, _, remainder = body.partition(' ')
                    resolved = resolve_command_name(raw_name, mode='mat')
                    if isinstance(resolved, tuple) and resolved[0] == 'ambiguous':
                        console.print(f"  [{C['gold']}]⚠[/] [{C['dim']}]Ambiguous command: /{safe_markup(raw_name)}[/]")
                        console.print(f"  [{C['dim']}]Matches: {', '.join('/' + m for m in resolved[1])}[/]\n")
                        continue
                    if resolved is None and (renamed := renamed_command(raw_name, remainder)):
                        resolved, remainder, note = renamed
                        console.print(f"  [{C['dim']}]{safe_markup(note)}[/]")
                    if resolved is None:
                        console.print(f"  [{C['dim']}]Unknown command. Try /help[/]")
                        continue
                    args = remainder.strip()
                    if resolved == 'quit':
                        break
                    elif resolved == 'help':
                        print_help(keys=prompt_ui is not None)
                    elif resolved == 'agents':
                        print_agents(agents)
                    elif resolved == 'full':
                        if args:
                            await run_full(args, agents)
                        else:
                            console.print(f"  [{C['dim']}]Usage: /compare <prompt>[/]")
                    elif resolved == 'config':
                        print_config_status(_CONFIG)
                    elif resolved == 'docs':
                        from ixel_mat.docs import DOCS_URL, open_docs
                        console.print(f"  [{C['dim']}]Ixel's docs are at[/] {DOCS_URL}")
                        if open_docs():
                            console.print(f"  [{C['dim']}]Opened in your browser.[/]")
                        console.print()
                    elif resolved == 'review':
                        if args:
                            await run_review_cmd(args, agents)
                        else:
                            console.print(f"  [{C['dim']}]{safe_markup(REVIEW_USAGE)}[/]")
                    elif resolved == 'saver':
                        if args:
                            await run_review_cmd("--saver " + args, agents)
                        else:
                            console.print(f"  [{C['dim']}]Usage: /saver <question>  (cheaper models draft, your big model verifies)[/]")
                    elif resolved == 'auto':
                        if args:
                            await run_review_cmd("--auto " + args, agents)
                        else:
                            console.print(f"  [{C['dim']}]Usage: /auto <question>  (Triage picks quick, review or deep)[/]")
                    elif resolved == 'answers':
                        print_last_answers()
                    elif resolved == 'new':
                        _CONVERSATION.clear()
                        console.print(f"  [{C['dim']}]Fresh start: the next question won't see the earlier ones.[/]\n")
                    elif resolved == 'saves':
                        for renderable in review_ui.stats_view(stats.summary(stats.load_stats())):
                            console.print(renderable)
                    else:
                        console.print(f"  [{C['dim']}]Unknown command. Try /help[/]")
                else:
                    await run_plain_question(text, agents)
            except EOFError:
                break
            except Exception as exc:
                # One bad response or command must not end the whole session
                console.print(
                    f"  [{C['red']}]✗ Command failed:[/] "
                    f"[{C['dim']}]{safe_markup(f'{type(exc).__name__}: {exc}')}[/]\n"
                )

    except KeyboardInterrupt:
        pass

    console.print(f"\n  [{C['dim']}]Disconnecting...[/]")
    _show_update_notice(update_ready())
    await disconnect_all(agents)
    console.print(f"  [{C['violet']}]✦[/] [{C['dim']}]Goodbye.[/]\n")


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "setup":
        from ixel_mat.config.setup import run_setup
        run_setup()
    else:
        asyncio.run(main())
