"""
`ixel app` — Ixel in a window of its own, served only to this computer.

The window is the browser app (`ixel gui`) shown by Edge or Chrome in app mode: no tabs or
address bar, its own taskbar button. No new program is installed, so there's no unsigned .exe
for Windows' Smart App Control to block. It runs with its own browser profile, so it never
touches your normal browsing, and the server stops once its window is closed (the page holds a
connection open while it's open; see GuiServer._presence).

The session key stays off the browser's command line, as with `ixel gui`: the browser opens a
private file that forwards it to the page.

On a Mac and on Linux, Ixel can be a native app instead: Ixel.app (macos/, built by install.sh) and
the GTK window in linux_window.py. Each starts `ixel app --host` itself and reads the address from a
pipe. `ixel app` opens them when they're there.
"""
from __future__ import annotations

import asyncio
import os
import plistlib
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping

from ixel_mat.agents.launch import find_on_path
from ixel_mat.config.secrets import child_env, keys_withheld

WINDOW_SIZE = "1280,840"
# A browser that fails this soon (a profile it can't use, a missing library) never opened a window
QUICK_FAILURE_SECONDS = 2.0

_EDGE = ("Microsoft", "Edge", "Application", "msedge.exe")
_CHROME = ("Google", "Chrome", "Application", "chrome.exe")
_MAC_APPS = ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium")
_LINUX_NAMES = ("microsoft-edge", "microsoft-edge-stable", "google-chrome", "google-chrome-stable",
                "chromium", "chromium-browser", "brave-browser")


def _registered(exe: str) -> Iterator[str]:
    """Where Windows' App Paths says a program is (how Run and Start find msedge.exe)."""
    try:
        import winreg
    except ImportError:
        return
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as key:
                value, _ = winreg.QueryValueEx(key, None)
        except OSError:
            continue
        if isinstance(value, str) and value.strip():
            yield os.path.expandvars(value.strip().strip('"'))


def browser_candidates(system: str, env: Mapping[str, str], home: Path,
                       registered: Callable[[str], Iterable[str]] = _registered,
                       on_path: Callable[[str], str | None] = find_on_path) -> list[str]:
    """Browsers that have an app mode, best first: Edge on Windows (every Windows 10 and 11 has
    it), Chrome on a Mac. IXEL_APP_BROWSER names one to use instead."""
    found = [env["IXEL_APP_BROWSER"]] if env.get("IXEL_APP_BROWSER") else []
    if system == "win32":
        bases = [env.get(name) for name in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        for exe, parts in (("msedge.exe", _EDGE), ("chrome.exe", _CHROME)):
            found += registered(exe)
            found += [str(Path(base, *parts)) for base in bases if base]
    elif system == "darwin":
        for app in _MAC_APPS:
            for folder in (Path("/Applications"), home / "Applications"):
                found.append(str(folder / f"{app}.app" / "Contents" / "MacOS" / app))
    else:
        found += [path for path in map(on_path, _LINUX_NAMES) if path]
    return list(dict.fromkeys(found))


def find_app_browser(system: str = sys.platform, env: Mapping[str, str] = os.environ,
                     home: Path | None = None, exists: Callable[[str], bool] = os.path.isfile) -> str | None:
    for path in browser_candidates(system, env, home or Path.home()):
        if exists(path):
            return path
    return None


def data_dir(system: str = sys.platform, env: Mapping[str, str] = os.environ, home: Path | None = None) -> Path:
    """Where the window keeps its browser profile and launch file (not in your config folder:
    a browser profile is tens of megabytes of cache)."""
    home = home or Path.home()
    if system == "win32" and env.get("LOCALAPPDATA"):
        return Path(env["LOCALAPPDATA"], "IxelMAT")
    if system == "darwin":
        return home / "Library" / "Application Support" / "IxelMAT"
    return Path(env.get("XDG_STATE_HOME") or home / ".local" / "state", "ixel-mat")


# Browser features that call out to the browser's maker, which a window showing only Ixel's own page
# doesn't need. Checked against a Chromium 141 window (not headless) with a network log: with these off, it no
# longer asked the autofill server about Ixel's text box (by a fingerprint of the page's form), asked the search
# engine whether its AI mode is offered, asked a time server, checked for updates, or connected ahead to the
# search engine. Two requests are left, and no switch we found stops them: the push-message service checks in
# (version and system), and the sign-in service asks Google's account server which Google accounts the profile
# has (none). Where the computer's DNS server is a public one with a secure version (8.8.8.8, 1.1.1.1), the
# browser also tries that and looks names up there. SECURITY.md says what each sends.
WINDOW_FEATURES_OFF = (
    "AutofillServerCommunication",       # asks the autofill server what each page's form fields are
    "AimServerRequestOnStartupEnabled",  # asks the search engine, at start, whether its AI mode is offered
    "PreconnectToSearch",                # opens a connection to the search engine in case you search
    "NetworkTimeServiceQuerying",        # asks a time server for the time
    "OptimizationHints",                 # fetches page hints for the addresses you visit
    "Translate",                         # offers to translate pages, which downloads models
    "DialMediaRouteProvider",            # looks for TVs and speakers on your network to cast to
    "CastMediaRouteProvider",
)

# Switches that turn off background services in the window's own browser. Each is one Chromium has.
WINDOW_SWITCHES = (
    # On Windows, Edge signs a new profile in to the Microsoft account you use Windows with, says over Ixel's
    # page that it's syncing your browsing data, and syncs this profile's history (Ixel's addresses) to that
    # account. With this, the profile may still be signed in, but nothing syncs.
    "--disable-sync",
    "--disable-background-networking",  # most fetches the browser makes on its own
    "--disable-component-update",       # update checks for the browser's add-on parts
    "--disable-default-apps",           # no web apps installed into the new profile
    "--disable-component-extensions-with-background-pages",  # built-in extensions that run unseen
    "--disable-domain-reliability",     # reports of failed connections to Google's sites
    "--disable-breakpad",               # crash reports
    "--metrics-recording-only",         # usage statistics stay in the profile and aren't sent
    "--no-pings",                       # link-click pings to other sites
    # Chromium builds (not Chrome or Edge) otherwise turn on test features, one of which loads a Google page
    "--disable-field-trial-config",
    "--disable-features=" + ",".join(WINDOW_FEATURES_OFF),
)


def window_command(browser: str, url: str, profile: Path, system: str = sys.platform) -> list[str]:
    command = [browser, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
               "--no-default-browser-check", *WINDOW_SWITCHES, f"--window-size={WINDOW_SIZE}"]
    if system.startswith("linux"):
        command.append("--class=Ixel")  # the window class ixel.desktop names: the dock shows Ixel's icon
    return command


# ── Native windows (Mac, Linux) ───────────────────────────────────────────────

LINUX_WINDOW = Path(__file__).with_name("linux_window.py")
SYSTEM_PYTHONS = ("/usr/bin/python3", "/usr/local/bin/python3")


MAC_APP_ID = "com.ixelai.ixel"  # macos/Info.plist's CFBundleIdentifier


def _bundle_id(app: Path) -> str | None:
    try:
        with open(app / "Contents" / "Info.plist", "rb") as f:
            info = plistlib.load(f)
    except Exception:  # noqa: BLE001 — not there, unreadable, or not a property list
        return None
    return info.get("CFBundleIdentifier") if isinstance(info, dict) else None


def mac_app(home: Path | None = None) -> Path | None:
    """Ixel.app, the native window install.sh builds on a Mac, if it's installed (Ixel MAT.app beside an
    Ixel.app of your own). An app of the same name that isn't Ixel's, by its bundle identifier, isn't it."""
    for folder in ((home or Path.home()) / "Applications", Path("/Applications")):
        for app in (folder / "Ixel.app", folder / "Ixel MAT.app"):
            if _bundle_id(app) == MAC_APP_ID:
                return app
    return None


def linux_window_command(python: str = sys.executable,
                         system_pythons: Iterable[str] = SYSTEM_PYTHONS) -> list[str] | None:
    """The GTK window, run by a system Python that has GTK and WebKit; None if none has."""
    for system_python in system_pythons:
        if not os.access(system_python, os.X_OK):
            continue
        try:
            probe = subprocess.run([system_python, "-I", str(LINUX_WINDOW), "--probe"], stdin=subprocess.DEVNULL,
                                   capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return [system_python, "-I", str(LINUX_WINDOW), python]
    return None


async def open_window(url: str, browser: str | None = None, profile: Path | None = None,
                      fallback: Callable[[str], bool] = webbrowser.open) -> str:
    """Open url in an app window, or a normal browser tab if there's no browser with an app
    mode (or it fails at once). Returns where it opened, for a person to read, or "" if nothing could."""
    browser = browser or find_app_browser()
    if browser:
        profile = profile or data_dir() / "window"
        detach = {"start_new_session": True} if os.name == "posix" else {}
        try:
            profile.mkdir(parents=True, exist_ok=True)
            # Without the keys Ixel saved, as for every program it starts: the app has them loaded by now
            proc = subprocess.Popen(window_command(browser, url, profile), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    env=child_env(nested=False), **detach)
        except OSError:
            proc = None
        if proc is not None:
            try:
                # Edge and Chrome exit at once, with 0, when they hand the window to one already
                # running with this profile (a second `ixel app`): that's fine.
                code = await asyncio.to_thread(proc.wait, QUICK_FAILURE_SECONDS)
            except subprocess.TimeoutExpired:
                code = 0
            if code == 0:
                return "its own window"
    with keys_withheld():
        opened = fallback(url)
    return "your browser" if opened else ""


def alert(message: str) -> None:
    """Show an error to someone who started Ixel from the Start Menu (pythonw: no console)."""
    if sys.stderr is not None:
        print(message, file=sys.stderr)
        return
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "Ixel", 0x10)
        except (AttributeError, OSError):
            pass
