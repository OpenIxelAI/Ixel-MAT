"""
Finding the program to run for a command-line agent, on Windows in particular.

Windows starts a bare name like `codex` only if there's a codex.exe: it doesn't look for the
codex.cmd that `npm install -g` makes. And a .cmd runs through cmd.exe, which reads & | < > ^ %
and quotes in its arguments as its own syntax, so a prompt (which may quote a model's answer)
passed as an argument could run commands.

So on Windows a command is looked up the way a shell would (PATHEXT; but only on PATH, never
in the current folder), and an npm shim is replaced by what it runs: `node <script>` or the
program it points to. cmd.exe is only used for a batch file Ixel can't read through, and then
only with arguments that hold nothing cmd.exe treats as syntax. A command that isn't on PATH
isn't started at all (CreateProcess would look for it in the current folder first).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable

# npm's cmd-shim .cmd files, in the forms Ixel runs directly: a script run by plain node (no
# interpreter arguments), or a program. Anything else (bun, sh, node with flags) isn't
# understood, and goes through the checked batch-file path.
#   current:  SET "_prog=%dp0%\node.exe" / SET "_prog=node" … "%_prog%"  "%dp0%\…\cli.js" %*
#   older:    "%~dp0\node.exe"  "%~dp0\…\cli.js" %*   and   node  "%~dp0\…\cli.js" %*
#   program:  "%dp0%\…\claude.exe"   %*
_CURRENT_NODE = re.compile(r'"%_prog%"[ \t]+"%dp0%\\([^"%\r\n]+)"[ \t]+%\*', re.IGNORECASE)
_CURRENT_PROG = re.compile(r'SET[ \t]+"_prog=([^"\r\n]*)"', re.IGNORECASE)
_OLD_NODE = re.compile(r'(?:"%~dp0\\node\.exe"|(?<![^\s(@])node)[ \t]+"%~dp0\\([^"%\r\n]+)"[ \t]+%\*', re.IGNORECASE)
_PROGRAM = re.compile(r'^[ \t]*@?"%~?dp0%?\\([^"%\r\n]+)"[ \t]+%\*[ \t]*$', re.IGNORECASE | re.MULTILINE)
_NODE_PROGS = {"%dp0%\\node.exe", "node"}
_SCRIPT_EXTS = (".js", ".mjs", ".cjs")
_PROGRAM_EXTS = (".exe", ".com")
_BATCH_EXTS = (".cmd", ".bat")
# What CreateProcess can start (anything else in PATHEXT, like .py or .js, it can't)
_LAUNCHABLE = _PROGRAM_EXTS + _BATCH_EXTS
# What cmd.exe reads as syntax in a command line (plus line breaks, which end it)
_CMD_SPECIAL = re.compile(r'[&|<>^%!()"\r\n]')

WINDOWS = os.name == "nt"


def _has_console() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except (AttributeError, ImportError, OSError):
        return True


# pythonw.exe (how the Start Menu's Ixel runs) has no console, so each console program started
# from it (a model's CLI, git) would open a console window of its own: CREATE_NO_WINDOW gives it
# a hidden one. From a terminal, programs share its console as usual.
NO_WINDOW_FLAGS = 0x08000000 if WINDOWS and not _has_console() else 0


class LaunchError(RuntimeError):
    pass


def _npm_shim(path: Path, which: Callable[[str], str | None]) -> list[str] | None:
    """What an npm .cmd shim runs, as an argv prefix; None if this isn't one Ixel understands."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    script = None
    current = _CURRENT_NODE.findall(text)
    if current:
        progs = {p.lower() for p in _CURRENT_PROG.findall(text)}
        if progs and progs <= _NODE_PROGS and len(set(current)) == 1:
            script = current[0]
    else:
        old = _OLD_NODE.findall(text)
        if old and len(set(old)) == 1:
            script = old[0]
    if script is not None:
        target = (path.parent / script.replace("\\", os.sep)).resolve()
        if not target.is_file() or target.suffix.lower() not in (*_SCRIPT_EXTS, ""):
            return None
        # The shim prefers a node.exe next to it, as npm's own does; otherwise node.exe on
        # PATH (never a node.cmd or node.bat, which would put cmd.exe back in between)
        local_node = path.parent / "node.exe"
        node = str(local_node) if local_node.is_file() else which("node.exe")
        return [node, str(target)] if node else None
    programs = _PROGRAM.findall(text)
    if len(programs) == 1:
        target = (path.parent / programs[0].replace("\\", os.sep)).resolve()
        if target.is_file() and target.suffix.lower() in _PROGRAM_EXTS:
            return [str(target)]
    return None


def windows_argv(argv: list[str], which: Callable[[str], str | None] | None = None) -> list[str]:
    """argv with its program resolved the way Windows needs (see the module docstring)."""
    which = which or find_on_path
    if not argv:
        return argv
    found = which(argv[0])
    if not found:
        # Never hand CreateProcess a bare name: it would look in the current folder first
        raise FileNotFoundError(f"{argv[0]} isn't on PATH")
    suffix = Path(found).suffix.lower()
    if suffix not in _BATCH_EXTS:
        return [found, *argv[1:]]
    shim = _npm_shim(Path(found), which)
    if shim is not None:
        return [*shim, *argv[1:]]
    unsafe = next((a for a in argv[1:] if _CMD_SPECIAL.search(a)), None)
    if unsafe is not None:
        raise LaunchError(
            f"{argv[0]} is a batch file ({Path(found).name}), which runs through cmd.exe, and this call's "
            "arguments contain characters cmd.exe would read as commands. Set the agent's command to the "
            'program the batch file starts, or set prompt_via = "stdin" if the program reads the prompt there.')
    return [found, *argv[1:]]


def resolve_argv(argv: list[str]) -> list[str]:
    """The argv to start: unchanged except on Windows."""
    return windows_argv(argv) if WINDOWS else argv


def find_on_path(name: str) -> str | None:
    """
    A program's full path, searching only PATH. (On Windows, shutil.which and CreateProcess both
    look in the current folder first, and the current folder may be a repository you just
    cloned: a claude.bat or git.exe there must not be what runs.)
    """
    if not name:
        return None
    exts = [""]
    if WINDOWS:
        # PATHEXT's order, but only what CreateProcess can start: a claude.py earlier on PATH
        # mustn't shadow claude.exe
        pathext = [e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
        exts = [e for e in pathext if e in _LAUNCHABLE] or list(_LAUNCHABLE)
        if os.path.splitext(name)[1].lower() in _LAUNCHABLE:
            exts = [""]
    if os.path.dirname(name):  # a path was given: that file, nothing else
        return next((name + ext for ext in exts if os.path.isfile(name + ext)), None)
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        folder = folder.strip().strip('"')
        if not folder or folder == os.curdir or not os.path.isabs(folder):
            continue
        for ext in exts:
            candidate = os.path.join(folder, name + ext)
            if os.path.isfile(candidate) and (WINDOWS or os.access(candidate, os.X_OK)):
                return candidate
    return None
