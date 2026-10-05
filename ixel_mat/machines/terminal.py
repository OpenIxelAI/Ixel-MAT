"""
Connect opens a terminal window running ssh, so the session (and any password prompt) is yours.

Every terminal runs a small Python helper that runs ssh's argv exactly and then keeps the window open
until you press Enter, so what ssh said last (a refused connection, a changed key, "Key added") stays
readable. The argv goes as separate arguments, never one string a shell would read again:
- Windows: Windows Terminal (wt.exe) re-parses its command line (it splits at every ';', even inside an
  argument), so it gets Python and a one-shot script holding the argv; without it, Python in a console
  window of its own.
- Mac: kitty or Alacritty when installed, else Terminal.app, through a private one-shot .command file: a
  /bin/sh script with each argument quoted (opening a file needs no Automation permission, unlike
  scripting Terminal).
- Linux: konsole, gnome-terminal, kitty, alacritty or xterm.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from ixel_mat.agents.launch import find_on_path
from ixel_mat.config.secrets import child_env
from ixel_mat.machines.ssh import SSHError

WINDOWS = sys.platform == "win32"
MAC = sys.platform == "darwin"
MACOS_TERMINAL = "Terminal.app"
NAMES = {"wt.exe": "Windows Terminal", "cmd.exe": "a console window", MACOS_TERMINAL: "Terminal",
         "konsole": "Konsole", "gnome-terminal": "GNOME Terminal", "kitty": "kitty", "alacritty": "Alacritty",
         "xterm": "xterm"}


def no_screen() -> bool:
    """Linux with no desktop to open a window on (over ssh, or a server): a terminal would start and show
    nothing."""
    return not (WINDOWS or MAC or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def find() -> str | None:
    """The terminal Connect opens: its full path (or Terminal.app on a Mac), None if there's none."""
    if no_screen():
        return None
    if WINDOWS:
        candidates: tuple[str, ...] = ("wt.exe", "cmd.exe")
    elif MAC:
        candidates = ("kitty", "alacritty")
    else:
        candidates = ("konsole", "gnome-terminal", "kitty", "alacritty", "xterm")
    for name in candidates:
        path = find_on_path(name)
        if path:
            return path
    return MACOS_TERMINAL if MAC else None


def label(terminal: str | None) -> str:
    if not terminal:
        return ""
    name = Path(terminal.replace("\\", "/")).name.lower()
    return NAMES.get(terminal) or NAMES.get(name) or NAMES.get(name.removesuffix(".exe")) or name


# Runs argv[1:], then waits for Enter so the last words stay readable. Ctrl+C belongs to ssh.
_HOLD_OPEN = """\
import subprocess, sys
proc = subprocess.Popen(sys.argv[1:])
while True:
    try:
        code = proc.wait()
        break
    except KeyboardInterrupt:
        pass
try:
    input(f"\\nssh ended (code {code}). Press Enter to close this window.")
except (EOFError, KeyboardInterrupt):
    pass
"""


def _console_python() -> str:
    """A console-mode Python: pythonw.exe has no console to hold open."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return sys.executable


def _one_shot(argv: list[str], suffix: str, body: str) -> str:
    fd, path = tempfile.mkstemp(prefix="ixel-machines-", suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(path, 0o700)
    return path


def _windows_script(argv: list[str]) -> str:
    """A private Python script that deletes itself, runs argv exactly, then waits for Enter."""
    return _one_shot(argv, ".py", "import os, sys\ntry:\n    os.remove(__file__)\nexcept OSError:\n    pass\n"
                                  f"sys.argv[1:] = {list(argv)!r}\n" + _HOLD_OPEN)


def _held(argv: list[str]) -> list[str]:
    """argv, run by the helper that keeps the window open after it ends."""
    return [_console_python(), "-c", _HOLD_OPEN, *argv]


def _mac_command_file(argv: list[str]) -> str:
    """A private .command script for Terminal.app that deletes itself as it starts."""
    return _one_shot(argv, ".command", '#!/bin/sh\nrm -f -- "$0"\n' + shlex.join(_held(argv)) + "\n")


def _is(terminal: str, *names: str) -> bool:
    return Path(terminal.replace("\\", "/")).name.lower() in names


def argv_for(terminal: str, argv: list[str]) -> list[str]:
    """The line that opens terminal running argv."""
    if _is(terminal, "wt.exe", "wt"):
        launch = (_console_python(), _windows_script(argv))
        return [terminal, "new-tab", "--", *(a.replace(";", "\\;") for a in launch)]  # \; is wt's literal ;
    if _is(terminal, "cmd.exe", "cmd"):
        # cmd.exe re-parses its one command string (>, %, ^), so it's left out: Python, in a console of its
        # own (creationflags), runs the exact argv
        return _held(argv)
    if terminal == MACOS_TERMINAL:
        return ["/usr/bin/open", "-a", "Terminal", _mac_command_file(argv)]
    if _is(terminal, "gnome-terminal", "kitty"):
        return [terminal, "--", *_held(argv)]
    # konsole, alacritty (which refuses --) and xterm: what follows -e is run as it is, no shell
    return [terminal, "-e", *_held(argv)]


def creationflags(terminal: str) -> int:
    if _is(terminal, "cmd.exe", "cmd"):
        return getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
    return 0


def open_window(argv: list[str], terminal: str | None = None) -> str:
    """Open a terminal running argv. → which terminal it was. SSHError when there's none."""
    terminal = terminal or find()
    if not terminal:
        raise SSHError("There's no desktop here to open a terminal window on. Run the line below in your terminal."
                       if no_screen() else
                       "There's no terminal program Ixel knows here (Konsole, GNOME Terminal, kitty, Alacritty or "
                       "xterm). Install one, or run the line below in yours.", "no_terminal")
    line = argv_for(terminal, argv)
    try:
        proc = subprocess.Popen(line, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=child_env(nested=False),
                                creationflags=creationflags(terminal),
                                **({} if WINDOWS else {"start_new_session": True}))
    except OSError as exc:
        raise SSHError(f"Couldn't open {label(terminal)}: {exc.strerror or exc}", "no_terminal") from None
    # Reaped when it ends (a terminal that stays in the foreground lives as long as its window)
    threading.Thread(target=proc.wait, name="ixel-terminal", daemon=True).start()
    return label(terminal)
