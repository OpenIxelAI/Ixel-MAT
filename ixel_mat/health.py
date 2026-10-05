"""
What `ixel doctor` and the app's Health page check: whether each part of Ixel is ready, and if not,
the one line that fixes it.

Each check is ok, warn (works, but something's off), fail (won't work), off (optional and not set up)
or unchecked (needs a network call or a program run: the page's "Check now", `ixel doctor --check`).
Without probe nothing goes over the network and no program runs, so the page opens at once. Nothing
here ever returns a key: only whether one is set.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import stat
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

from ixel_mat import __version__
from ixel_mat.agents.base import (AgentConfig, cleartext_refusal, is_loopback_host, needs_api_key,
                                  sends_key_in_cleartext)
from ixel_mat.agents.launch import LaunchError, find_on_path, resolve_argv

SCHEMA = 1
STATES = ("ok", "warn", "fail", "off", "unchecked")
PROGRAM_TIMEOUT = 8.0   # `codex --version` on a cold Windows start, with the antivirus scanning it
PROBE_TIMEOUT = 25.0    # all of "Check now" together
MAX_DETAIL = 300
# What Ixel MAT needs installed (pyproject.toml's dependencies, by the name Python imports)
PACKAGES = ("rich", "prompt_toolkit", "websockets", "cryptography", "aiohttp", "anthropic", "mcp")
BROWSER_NAMES = {"msedge": "Microsoft Edge", "chrome": "Google Chrome", "microsoft-edge": "Microsoft Edge",
                 "microsoft-edge-stable": "Microsoft Edge", "google-chrome": "Google Chrome",
                 "google-chrome-stable": "Google Chrome", "chromium": "Chromium", "chromium-browser": "Chromium",
                 "brave-browser": "Brave"}
GIT_INSTALL = {"win32": "winget install --id Git.Git -e", "darwin": "xcode-select --install"}


@dataclass
class Check:
    id: str
    label: str
    state: str
    detail: str = ""
    fix: str = ""  # one line to copy and run, or ""

    def __post_init__(self) -> None:
        assert self.state in STATES, self.state
        self.detail = " ".join(str(self.detail).split())[:MAX_DETAIL]


@dataclass
class Group:
    id: str
    title: str
    checks: list[Check]


def _first_line(text: str) -> str:
    return next((line.strip() for line in (text or "").splitlines() if line.strip()), "")


async def run_program(argv: list[str], timeout: float = PROGRAM_TIMEOUT,
                      env: dict[str, str] | None = None) -> tuple[int | None, str]:
    """Run a program the way Ixel runs agents' CLIs (found on PATH only, keys withheld, no console
    window), in a scratch folder. (None, why) when it couldn't start or didn't finish; one that doesn't
    finish is ended with everything it started (an npm CLI is node under cmd.exe on Windows)."""
    from ixel_mat.agents.process_tree import SPAWN_OPTIONS, create_process_tree
    from ixel_mat.config.secrets import child_env
    try:
        proc, tree = await create_process_tree(
            *resolve_argv(argv), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=tempfile.gettempdir(), env=child_env() if env is None else env,
            **SPAWN_OPTIONS)
    except (OSError, LaunchError, RuntimeError) as exc:
        return None, str(exc)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        return None, f"no answer in {timeout:.0f} s"
    finally:  # finished, timed out, or the whole check was cancelled: nothing it started keeps running
        await tree.kill()
        tree.close()
    output = out.decode("utf-8", "replace") or err.decode("utf-8", "replace")
    return proc.returncode, _first_line(output)


async def off_the_loop(fn, *args):
    """fn(*args) on a thread of its own that nobody waits for: a check that never ends can't hold up
    `ixel doctor` as it exits, or fill the app server's shared threads."""
    from ixel_mat.gui.handoff import in_daemon_thread
    return await in_daemon_thread(fn, *args)


# ── Ixel MAT itself ───────────────────────────────────────────────────────────

def ixel_checks(settings, which: Callable[[str], str | None] | None = None) -> list[Check]:
    from ixel_mat.config.loader import find_config, validate_config
    from ixel_mat.config.secrets import get_env_file_path
    which = which or find_on_path

    checks = [Check("version", "Ixel MAT", "ok", f"Version {__version__}, on Python {sys.version.split()[0]}")]
    missing = [name for name in PACKAGES if importlib.util.find_spec(name) is None]
    if sys.version_info < (3, 11) and importlib.util.find_spec("tomli") is None:
        missing.append("tomli")
    checks.append(Check("packages", "Python packages", "fail" if missing else "ok",
                        f"Missing: {', '.join(missing)}" if missing else "Everything Ixel MAT needs is installed",
                        "ixel update" if missing else ""))

    path = find_config()
    error = settings.config.get("_error") if isinstance(settings.config, dict) else None
    if error:
        checks.append(Check("config", "Settings", "fail", f"{error[:1].upper()}{error[1:]}. Fix the file, or write "
                            "a new one with ixel setup", "ixel setup"))
    elif path is None:
        checks.append(Check("config", "Settings", "warn", "No settings yet, so there are no models to ask",
                            "ixel setup"))
    else:
        problems = [*validate_config(settings.config), *settings.warnings]
        checks.append(Check("config", "Settings", "warn" if problems else "ok",
                            "; ".join(dict.fromkeys(problems)) if problems else f"Read from {path}",
                            "ixel config" if problems else ""))

    keys = get_env_file_path()
    if keys.exists() and os.name == "posix":
        mode = stat.S_IMODE(keys.stat().st_mode)
        if mode & 0o077:
            checks.append(Check("keys-file", "Keys file", "warn",
                                f"{keys} can be read by other people on this computer ({mode:o})",
                                f"chmod 600 '{keys}'"))
        else:
            checks.append(Check("keys-file", "Keys file", "ok", f"Only you can read {keys}"))
    elif keys.exists():
        checks.append(Check("keys-file", "Keys file", "ok", f"Kept in {keys}, in your user folder"))

    git = which("git")
    if git:
        checks.append(Check("git", "Git", "ok", f"Found at {git}"))
    else:
        fix = GIT_INSTALL.get(sys.platform, "")
        checks.append(Check("git", "Git", "fail", "Not found. Code reviews and Handoff need it"
                            + ("" if fix else ": install it with your package manager"), fix))
    return checks


# ── Models ────────────────────────────────────────────────────────────────────

def _token_env(settings, name: str) -> str:
    agents = settings.config.get("agents") if isinstance(settings.config, dict) else None
    data = agents.get(name) if isinstance(agents, dict) else None
    env = data.get("token_env") if isinstance(data, dict) else None
    return env if isinstance(env, str) else ""


def _where(cfg: AgentConfig) -> str:
    try:
        parsed = urlparse(cfg.url)
        return parsed.netloc or cfg.url
    except ValueError:
        return cfg.url


def _place(cfg: AgentConfig) -> str:
    try:
        return "this computer" if is_loopback_host(urlparse(cfg.url).hostname) else "your own network"
    except ValueError:
        return "this computer"


ProbeAgent = Callable[[AgentConfig], Awaitable[tuple[str, str, "int | None", "str | None"]]]


async def _default_probe_agent(cfg: AgentConfig):
    from ixel_mat.cli import _probe_agent_connection
    # In a thread of its own: some provider probes block, and the app's server must keep answering
    return await off_the_loop(asyncio.run, _probe_agent_connection(cfg))


async def agent_check(settings, cfg: AgentConfig, probe: bool, which=None, run=None,
                      probe_agent: ProbeAgent | None = None) -> list[Check]:
    which, run, probe_agent = which or find_on_path, run or run_program, probe_agent or _default_probe_agent
    label, cid = cfg.label or cfg.name, f"agent-{cfg.name}"
    if cfg.type in ("oneshot", "subprocess"):
        from ixel_mat.agents.oneshot import cli_env
        from ixel_mat.presets import preset_for
        preset = preset_for(cfg.command)
        path = which(cfg.command) if cfg.command else None
        if not path:
            return [Check(cid, label, "fail", f"{cfg.command or 'Its program'} isn't installed, or isn't on PATH",
                          preset.get("install", ""))]
        # What the agent itself runs with (a subprocess one gets no Gemini API key)
        if cfg.type == "oneshot":
            env = cli_env(cfg)
        else:
            from ixel_mat.config.secrets import child_env
            from ixel_mat.presets import locked_env
            env = child_env(cfg.pass_env, {**(cfg.env or {}), **locked_env(cfg.command)}, cfg.drop_env)
        # Read from its settings and environment, so it's there before Check now too
        sign_in = [gemini_sign_in(cid, label, env)] if preset.get("id") == "gemini_cli" and cfg.type == "oneshot" \
            else []
        if not probe:
            return [Check(cid, label, "unchecked", f"{cfg.command} is installed. Check now asks it for its version"),
                    *sign_in]
        code, out = await run([cfg.command, "--version"], env=env)
        checks = [Check(cid, label, "ok" if code == 0 else "warn",
                        out if code == 0 and out else f"{cfg.command} --version didn't work: {out or f'exit {code}'}")]
        if preset.get("id") == "codex":
            code, out = await run([cfg.command, "login", "status"], env=env)
            checks.append(Check(f"{cid}-login", f"{label} sign-in", "ok" if code == 0 else "warn",
                                out or ("Signed in" if code == 0 else "Not signed in"),
                                "" if code == 0 else f"{cfg.command} login"))
        return checks + sign_in

    remote = needs_api_key(cfg)
    if remote and not cfg.token:
        env = _token_env(settings, cfg.name)
        return [Check(cid, label, "fail", f"No API key{f' ({env})' if env else ''}", "ixel setup")]
    if sends_key_in_cleartext(cfg.url, cfg.token):  # a real call refuses this too
        return [Check(cid, label, "fail", cleartext_refusal(cfg.name, cfg.url).split(": ", 1)[1],
                      f"Use https:// for [agents.{cfg.name}] url in your settings file")]
    if not probe:
        what = "Key set" if remote else f"Runs on {_place(cfg)} at {_where(cfg)}"
        return [Check(cid, label, "unchecked", f"{what}. Check now sees if it answers")]
    try:
        status, detail, latency, _ = await probe_agent(cfg)
    except Exception as exc:  # noqa: BLE001 — a probe that breaks is a result, not a crash
        status, detail, latency = "unreachable", str(exc), None
    if cfg.token and len(cfg.token) >= 8:  # a service that quotes the key back in its error
        detail = str(detail).replace(cfg.token, "[your key]")
    took = f" in {latency} ms" if latency is not None else ""
    if status == "ok":
        return [Check(cid, label, "ok", f"Answers{took}" + (f", with {cfg.model}" if cfg.model else ""))]
    if status == "rate_limited":
        return [Check(cid, label, "warn", f"Rate limited for now: {detail}")]
    if status == "model_missing":
        return [Check(cid, label, "fail", detail, f"ixel model {cfg.name}")]
    if status == "auth_failed":
        return [Check(cid, label, "fail", f"Refused the key: {detail}", "ixel setup")]
    if not remote:
        return [Check(cid, label, "fail", f"Nothing answers at {_where(cfg)}. Is the model server running? {detail}")]
    return [Check(cid, label, "fail", f"Couldn't reach it: {detail}")]


def gemini_sign_in(cid: str, label: str, env: dict[str, str]) -> Check:
    """Whether Gemini CLI, run with env, can sign in: without a way to, every question it's asked fails."""
    from ixel_mat.presets import GEMINI_SIGN_IN, gemini_has_sign_in
    cid, label = f"{cid}-login", f"{label} sign-in"
    if gemini_has_sign_in(env):
        return Check(cid, label, "ok", "Set up to sign in, in Gemini CLI itself")
    if env.get("GEMINI_API_KEY"):
        return Check(cid, label, "ok", "Uses your Gemini API key")
    return Check(cid, label, "warn", GEMINI_SIGN_IN, "gemini")


def extra_model_checks(settings) -> list[Check]:
    """Triage and pictures: optional, so off rather than failing when they aren't set up."""
    from ixel_mat import images
    checks = []
    triage = settings.triage
    if triage.enabled:
        checks.append(Check("triage", "Triage", "ok" if triage.ready else "warn",
                            "Picks the mode and skips reviews that aren't needed" if triage.ready
                            else f"Turned on, but it has no {'model' if triage.provider == 'model' else 'key'}",
                            "" if triage.ready else "ixel triage"))
    ready = [p for p in images.status(settings.config) if p["ready"]]
    checks.append(Check("pictures", "Pictures", "ok" if ready else "off",
                        f"Made with {ready[0]['label']} ({ready[0]['model']})" if ready
                        else "Add an xAI or OpenAI key to make pictures", "" if ready else "ixel setup"))
    return checks


# ── Handoff and the window ────────────────────────────────────────────────────

async def handoff_checks(probe: bool, which=None, run=None, system: str | None = None) -> list[Check]:
    from ixel_mat.gui.handoff import INSTALL_MAC_LINUX, INSTALL_WINDOWS
    from ixel_mat.gui.handoff_api import handoff_command
    run = run or run_program
    if which is None:  # as the Board finds it: also where its installer put it, before PATH has it
        command = handoff_command()
    else:
        command = [which("handoff")] if which("handoff") else None
    path = command[0] if command else None
    if not path:
        where, line = ("PowerShell", INSTALL_WINDOWS) if (system or sys.platform) == "win32" else \
            ("a terminal", INSTALL_MAC_LINUX)
        return [Check("handoff", "Handoff", "off", "Not installed. It gives your agents one task board to share, "
                      f"and /handoff in Ask needs it. Run this in {where}", line)]
    if not probe:
        return [Check("handoff", "Handoff", "ok", f"Installed at {path}")]
    code, out = await run([*command, "version"])
    return [Check("handoff", "Handoff", "ok" if code == 0 else "warn",
                  out if code == 0 and out else f"Found at {path}, but `handoff version` didn't work: {out}",
                  "" if code == 0 else "handoff doctor")]


def window_checks(probe: bool, system: str | None = None) -> list[Check]:
    from ixel_mat.gui import window
    system = system or sys.platform
    browser = window.find_app_browser(system)
    if system == "darwin":
        app = window.mac_app()
        if app is not None:
            return [Check("window", "The Ixel window", "ok", f"Opens as {app}")]
    elif system.startswith("linux") and probe and window.linux_window_command() is not None:
        return [Check("window", "The Ixel window", "ok", "Opens in its own window (GTK)")]
    if browser:
        name = BROWSER_NAMES.get(Path(browser).stem.lower(), Path(browser).stem)
        return [Check("window", "The Ixel window", "ok", f"Opens in a window of its own, through {name}")]
    if system.startswith("linux") and not probe:
        return [Check("window", "The Ixel window", "unchecked", "Check now looks for GTK and WebKit")]
    return [Check("window", "The Ixel window", "warn", "Opens in a browser tab: there's no Edge or Chrome for "
                  "a window of its own", "winget install Microsoft.Edge" if system == "win32" else "")]


# ── Machines ──────────────────────────────────────────────────────────────────

SSH_INSTALL = {"win32": "Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0"}


def machine_checks(probe: bool, which=None, system: str | None = None) -> list[Check]:
    """Only once there are machines: whether ssh and a terminal are there, and (Check now) how many keys
    are pinned. Finding where an ssh alias goes runs `ssh -G`, so that waits for Check now. Never raises:
    whatever goes wrong here is one failed check, not a Health page (or `ixel doctor`) that won't load."""
    try:
        return _machine_checks(probe, which, system)
    except Exception as exc:  # noqa: BLE001 (one group must not take the report down)
        logging.getLogger("ixel_mat.machines").warning("Machines checks failed", exc_info=True)
        return [Check("machines", "Machines", "fail", f"Couldn't check your machines: {exc}")]


def _machine_checks(probe: bool, which=None, system: str | None = None) -> list[Check]:
    from ixel_mat.machines import ssh, store, terminal
    machines_store = store.Store()
    try:
        machines = machines_store.load()
    except store.MachineError as exc:
        return [Check("machines", "Machines", "fail", str(exc))]
    if not machines:
        return [Check("machines", "Machines", "off", "None yet. Add them on the Machines page, or bring in the "
                      "hosts in your ~/.ssh/config with", "ixel machines import ssh")]
    system = system or sys.platform
    program = which("ssh") if which else (ssh.ssh_program() if probe else find_on_path("ssh"))
    count = f"{len(machines)} machine{'' if len(machines) == 1 else 's'}"
    checks = [Check("machines", "Machines", "warn" if machines_store.unreadable() else "ok",
                    count + (f", and {machines_store.unreadable()} entries Ixel can't read (left as they are "
                             f"in {store.MACHINES_FILE})" if machines_store.unreadable() else ""))]
    if program:
        checks.append(Check("ssh", "ssh", "ok", f"Found at {program}"))
    else:
        checks.append(Check("ssh", "ssh", "fail", "Not installed, or not on PATH, so no machine can connect. " + (
            "Run this in PowerShell as administrator" if system == "win32" else
            "Install OpenSSH's client (openssh-client, or openssh)"), SSH_INSTALL.get(system, "")))
    window = terminal.find()
    checks.append(Check("terminal", "Terminal for Connect", "ok", f"Connect opens {terminal.label(window)}")
                  if window else
                  Check("terminal", "Terminal for Connect", "warn", (
                      "There's no desktop here to open a terminal window on" if terminal.no_screen() else
                      "There's no terminal Ixel knows how to open (konsole, gnome-terminal, kitty, alacritty or "
                      "xterm)") + ", so Connect gives the line to run instead: `ixel machines connect NAME` "
                      "connects in the terminal you're in."))
    if not probe or not program:
        checks.append(Check("pinned", "Pinned keys", "unchecked", "Check now counts the machines whose key is "
                            "pinned"))
        return checks
    have = sum(1 for m in machines if ssh.pinned(ssh.resolve(m, program).name))
    checks.append(Check("pinned", "Pinned keys", "ok", f"{have} of {len(machines)} pinned" + (
        "" if have == len(machines) else ". The others are checked the first time you connect to them")))
    return checks


# ── All of it ─────────────────────────────────────────────────────────────────

async def report(probe: bool = False, settings_loader: Callable | None = None, which=None, run=None,
                 probe_agent: ProbeAgent | None = None, system: str | None = None) -> dict:
    """The Health page's JSON: {schema, checked_at, probed, groups: [{id, title, checks: [...]}]}."""
    if settings_loader is None:
        from ixel_mat.runtime import load_settings as settings_loader
    settings = settings_loader()

    async def models() -> list[Check]:
        rows = await asyncio.gather(*(agent_check(settings, cfg, probe, which, run, probe_agent)
                                      for cfg in settings.agent_configs.values()))
        checks = [check for row in rows for check in row]
        if not checks and isinstance(settings.config, dict) and settings.config.get("_error"):
            checks.append(Check("agents", "Models", "fail", "None, because Ixel can't read its settings file "
                                "(see Settings)"))
        elif not checks:
            checks.append(Check("agents", "Models", "fail", "No models set up yet", "ixel setup"))
        return checks + extra_model_checks(settings)

    async def bounded(coro, group_id: str, title: str) -> Group:
        try:
            return Group(group_id, title, await asyncio.wait_for(coro, PROBE_TIMEOUT) if probe else await coro)
        except asyncio.TimeoutError:
            return Group(group_id, title, [Check(group_id, title, "warn", "The checks took too long; try again")])

    groups = await asyncio.gather(
        bounded(models(), "models", "Models"),
        bounded(handoff_checks(probe, which, run, system), "handoff", "Handoff"),
        bounded(off_the_loop(window_checks, probe, system), "window", "Window"),
        bounded(off_the_loop(machine_checks, probe, which, system), "machines", "Machines"),
    )
    return {"schema": SCHEMA, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "probed": probe,
            "groups": [asdict(Group("ixel", "Ixel", ixel_checks(settings, which))),
                       *(asdict(group) for group in groups)]}


def failing(health: dict) -> bool:
    """Whether anything in a report needs fixing (`ixel doctor` then exits 1)."""
    return any(c["state"] == "fail" for g in health.get("groups", []) for c in g.get("checks", []))
