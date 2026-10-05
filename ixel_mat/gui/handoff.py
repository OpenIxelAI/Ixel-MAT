"""
The app's /handoff: split one request across your agents with Handoff (`handoff dispatch`).

    /handoff codex review my changes, grok make pictures of my app, claude finish the next step,
             and gemini make me a list of projects to check out

The page asks for the plan first (`handoff dispatch --plan --json`), shows who gets what, and runs it
only when you press Run (`handoff dispatch --yes --json`). Handoff does the rest: it adds a task per
part to the project's board, approves each for exactly that run, and runs them side by side (edits
by claude and codex in their own branch, answers, reviews and pictures through `ixel ask` and
`ixel image`).

The request goes to Handoff on stdin, never on its command line. The project folder is one you
typed, checked here first: an absolute path to a folder in a git repository, without the characters
Windows' command interpreter treats specially. Handoff is found on PATH only; without it, the page
shows the one line that installs it on this system. Handoff is found where its installer put it, or on
PATH (as the Board finds it). A run keeps going if the window closes; its
results land on the board either way (`handoff board`).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from ixel_mat.agents.launch import NO_WINDOW_FLAGS, LaunchError, resolve_argv

MAX_REQUEST_CHARS = 4_000
PLAN_TIMEOUT = 120.0
RUN_TIMEOUT = 2 * 60 * 60.0
# A .cmd launcher's arguments pass through cmd.exe, which gives these meaning even inside quotes
_UNSAFE = re.compile(r'["%&|<>^!\r\n\x00]')
# Handoff on its own, for someone who installed Ixel MAT alone (the full Ixel installs both)
INSTALL_WINDOWS = "irm https://ixelai.com/handoff/install.ps1 | iex"
INSTALL_MAC_LINUX = "curl -fsSL https://ixelai.com/handoff/install.sh | sh"


class HandoffError(ValueError):
    """Something to tell the person; nothing ran."""


def find_handoff() -> list[str] | None:
    """How to start Handoff (its install's Python, or `handoff` on PATH), the way the Board does; None if neither."""
    from ixel_mat.gui.handoff_api import handoff_command
    return handoff_command()


def how_to_install() -> dict:
    """Where to run the line that installs Handoff on this system, and the line."""
    if sys.platform == "win32":
        return {"where": "PowerShell", "command": INSTALL_WINDOWS}
    return {"where": "a terminal", "command": INSTALL_MAC_LINUX}


def not_installed() -> str:
    how = how_to_install()
    return (f"Handoff isn't installed on this computer. To add it, run this in {how['where']}, then open Ixel "
            f"again:  {how['command']}")


def project_root(start: Path) -> Path | None:
    """The git repository a folder is in (the nearest folder above it with a .git)."""
    try:
        current = start.resolve()
    except OSError:
        return None
    for folder in (current, *current.parents):
        if (folder / ".git").exists():
            return folder
    return None


def default_project() -> str:
    """The repository `ixel app` was started in, if any (from the Start Menu it's your home folder: none)."""
    root = project_root(Path.cwd())
    return str(root) if root is not None and not _UNSAFE.search(str(root)) else ""


def check_project(value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise HandoffError("Say which project: the folder of a git repository on this computer.")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise HandoffError("Give the project's full path, like C:\\Users\\you\\code\\shop or ~/code/shop.")
    if _UNSAFE.search(str(path)):
        raise HandoffError("Handoff can't be started on a folder whose path has \" % & | < > ^ or ! in it.")
    if not path.is_dir():
        raise HandoffError(f"There's no folder at {path}.")
    root = project_root(path)
    if root is None:
        raise HandoffError(f"{path} isn't in a git repository, and Handoff keeps its board in one.")
    return root


def check_request(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HandoffError("Say what to hand out, starting each part with who does it: "
                           "/handoff codex review my changes, gemini make me a list of …")
    if len(value) > MAX_REQUEST_CHARS:
        raise HandoffError(f"That's longer than {MAX_REQUEST_CHARS:,} characters; split it up.")
    return value.strip()


def _command(handoff: list[str], project: Path, plan: bool) -> list[str]:
    try:
        return resolve_argv([*handoff, "dispatch", "--json", "--project", str(project),
                             "--plan" if plan else "--yes", "-"])
    except LaunchError as exc:
        raise HandoffError(str(exc)) from None


def _run(argv: list[str], request: str, cwd: Path, timeout: float, stop_on_timeout: bool) -> tuple[int | None, str, str]:
    """Run Handoff with the request on stdin. A run that outlives `timeout` is left running unless asked."""
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW_FLAGS} if sys.platform == "win32"
             else {"start_new_session": True})  # closing the window or Ctrl+C here doesn't stop the agents
    from ixel_mat.gui.handoff_api import _env
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=str(cwd), env=_env(), **group)
    try:
        out, err = proc.communicate(request.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        if stop_on_timeout:
            proc.kill()
            proc.communicate()
        return None, "", ""
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def in_daemon_thread(fn, *args):
    """fn(*args) on a daemon thread: a long run mustn't keep Ixel from exiting when its window closes
    (asyncio's own worker threads are waited for at exit)."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def settle(outcome: tuple) -> None:
        if not future.done():
            (future.set_exception if outcome[0] else future.set_result)(outcome[1])

    def work() -> None:
        try:
            outcome = (False, fn(*args))
        except BaseException as exc:  # noqa: BLE001 — handed to the awaiting coroutine
            outcome = (True, exc)
        try:
            loop.call_soon_threadsafe(settle, outcome)
        except RuntimeError:
            pass  # Ixel has stopped; the board has the results

    threading.Thread(target=work, name="ixel-handoff", daemon=True).start()
    return await future


async def dispatch(project: object, request: object, *, plan: bool) -> dict:
    """Handoff's plan for the request (plan=True), or the results of running it. HandoffError if it can't."""
    handoff = find_handoff()
    if handoff is None:
        raise HandoffError(not_installed())
    root = check_project(project)
    text = check_request(request)
    timeout = PLAN_TIMEOUT if plan else RUN_TIMEOUT
    code, out, err = await in_daemon_thread(_run, _command(handoff, root, plan), text, root, timeout, plan)
    if code is None:
        if plan:
            raise HandoffError("Handoff took too long to make a plan.")
        raise HandoffError("The tasks are still running. Their results land on the board: `handoff board`.")
    try:
        data = json.loads(out)
    except ValueError:
        lines = [line.strip() for line in err.splitlines() if line.strip()]
        raise HandoffError(lines[-1][:500] if lines else f"Handoff stopped (exit code {code}).") from None
    if not isinstance(data, dict):
        raise HandoffError("Handoff sent back something Ixel doesn't understand.")
    data["project"] = str(root)
    return data
