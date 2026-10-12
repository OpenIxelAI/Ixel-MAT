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
- Linux: $TERMINAL first (split only to find its program, never run through a shell), then the first of
  x-terminal-emulator, xdg-terminal-exec, Ptyxis, GNOME Console (kgx), GNOME Terminal, Konsole, Xfce Terminal,
  Tilix, foot, WezTerm, Alacritty, kitty and xterm that's installed. Each gets the command as separate
  arguments in the form it takes; Tilix's -e takes one string, so it gets the command quoted with
  shlex.join, which Tilix parses itself, not a shell.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from ixel_mat.agents.launch import find_on_path
from ixel_mat.config.secrets import child_env
from ixel_mat.machines.ssh import SSHError

WINDOWS = sys.platform == "win32"
MAC = sys.platform == "darwin"
MACOS_TERMINAL = "Terminal.app"
NAMES = {"wt.exe": "Windows Terminal", "cmd.exe": "a console window", MACOS_TERMINAL: "Terminal",
         "x-terminal-emulator": "your default terminal", "xdg-terminal-exec": "your default terminal",
         "ptyxis": "Ptyxis", "kgx": "GNOME Console", "konsole": "Konsole", "gnome-terminal": "GNOME Terminal",
         "xfce4-terminal": "Xfce Terminal", "tilix": "Tilix", "foot": "foot", "wezterm": "WezTerm",
         "kitty": "kitty", "alacritty": "Alacritty", "xterm": "xterm"}
INSTALL_HINT = ("Install a terminal such as GNOME Console (gnome-console), Konsole, Xfce Terminal (xfce4-terminal) "
                "or xterm from your package manager, or set $TERMINAL to the one you use.")

# The Linux terminals, in the order they're looked for: each builds what follows its program from held, the
# helper's argv, which they all run as it is, with no shell. "--" keeps the terminal's own option parser off
# our arguments (foot would take the -c as its --config); Alacritty refuses it. Tilix's -e takes one string
# that Tilix splits itself, so it gets held quoted with shlex.join.
LINUX_TERMINALS: dict[str, Callable[[list[str]], list[str]]] = {
    "x-terminal-emulator": lambda held: ["-e", *held],
    "xdg-terminal-exec": lambda held: list(held),
    "ptyxis": lambda held: ["--new-window", "--", *held],
    "kgx": lambda held: ["--", *held],
    "gnome-terminal": lambda held: ["--", *held],
    "konsole": lambda held: ["-e", *held],
    "xfce4-terminal": lambda held: ["-x", *held],
    "tilix": lambda held: ["-e", shlex.join(held)],
    "foot": lambda held: ["--", *held],
    "wezterm": lambda held: ["start", "--", *held],
    "alacritty": lambda held: ["-e", *held],
    "kitty": lambda held: ["--", *held],
    "xterm": lambda held: ["-e", *held],
}


def no_screen() -> bool:
    """Linux with no desktop to open a window on (over ssh, or a server): a terminal would start and show
    nothing."""
    return not (WINDOWS or MAC or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _chosen() -> str | None:
    """The program $TERMINAL names, when it's there. Only its first word is looked for (the rest are its own
    options, and none of it goes through a shell)."""
    try:
        words = shlex.split(os.environ.get("TERMINAL", ""))
    except ValueError:
        return None
    return find_on_path(words[0]) if words else None


def find() -> str | None:
    """The terminal Connect opens: its full path (or Terminal.app on a Mac), None if there's none."""
    if no_screen():
        return None
    if WINDOWS:
        candidates: tuple[str, ...] = ("wt.exe", "cmd.exe")
    elif MAC:
        candidates = ("kitty", "alacritty")
    else:
        chosen = _chosen()
        if chosen:
            return chosen
        candidates = tuple(LINUX_TERMINALS)
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


def _name(terminal: str) -> str:
    return Path(terminal.replace("\\", "/")).name.lower()


def _is(terminal: str, *names: str) -> bool:
    return _name(terminal) in names


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
    held = _held(argv)
    build = LINUX_TERMINALS.get(_name(terminal))
    # A program that's not in the table (a $TERMINAL of its own) gets -e, as x-terminal-emulator takes it
    return [terminal, *(build(held) if build else ["-e", *held])]


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
                       f"There's no terminal program Ixel knows here. {INSTALL_HINT} Or run the line below in yours.",
                       "no_terminal")
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
