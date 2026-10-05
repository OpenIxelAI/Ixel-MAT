"""Check Ixel MAT on this Windows machine, and write a report to send back.

Run it from a checkout, in its virtual environment (CONTRIBUTING.md, "Run the tests"):

    .\\.venv\\Scripts\\python scripts\\check_windows.py

Nothing is sent to a model provider and nothing is billed: the tests use fake models on this machine.
The installer check installs into a temporary folder, so your own install and settings aren't touched.
It writes windows-check-report.txt next to where you run it.

1. This machine: Windows, Python, and which ixel and subscription CLIs are on PATH.
2. Ixel's test suite, including the live checks that the Claude Code, Codex, Gemini, Copilot and
   OpenCode you have installed stay answer-only (they run against a fake model).
3. install.ps1, into a temporary folder, then the ixel it installed (through its ixel.cmd).
4. The ixel you use every day: its version and `ixel doctor`, and whether Windows blocks it from starting.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT: list[str] = []
# Key shapes the report must never carry, even if a tool prints one
SECRET = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|xai-[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_-]{30,}|gh[pousr]_[A-Za-z0-9]{30,}"
                    r"|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})")
# Windows refusing to start a program: Smart App Control, or an App Control (Device Guard) policy.
# 4551 is ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION, which cmd.exe passes on as its exit code.
BLOCKED_EXIT = 4551
BLOCKED = re.compile(r"Device Guard|Application Control|Smart App Control|WinError 4551", re.IGNORECASE)


def say(line: str = "") -> None:
    line = SECRET.sub("[hidden]", line)
    print(line, flush=True)
    REPORT.append(line)


def section(title: str) -> None:
    say()
    say(f"==================== {title} ====================")


def run(argv: list[str], **kw) -> tuple[int | None, str]:
    # Python programs write UTF-8 to the pipe, not the ANSI code page, so ✓ and × survive. Not through an
    # ixel.cmd, though: it runs Python with -I, which ignores these, so its ✓ and ✗ may come through as ?
    kw["env"] = {**kw.get("env", os.environ), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    return p.returncode, (p.stdout + p.stderr).strip()


def wrapper_command(program: str) -> str:
    """The line an ixel.cmd runs ('' for anything else)."""
    if not program.lower().endswith((".cmd", ".bat")):
        return ""
    try:
        lines = Path(program).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return next((line.strip() for line in reversed(lines) if line.strip()), "")


def blocked(code: int | None, out: str) -> bool:
    """Whether Windows refused to start the program (or one it ran, for a .cmd)."""
    return code == BLOCKED_EXIT or bool(BLOCKED.search(out))


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


# ── 1. This machine ──────────────────────────────────────────────────────────

def machine() -> None:
    section("1. This machine")
    say(f"System: {platform.platform()}  (release {platform.release()}, version {platform.version()})")
    if sys.platform != "win32":
        say("NOTE: this isn't Windows; the installer check is skipped.")
    say(f"Running as administrator: {is_admin()}")
    say(f"Python: {sys.version.split()[0]} at {sys.executable}")
    for name in ("ixel", "claude", "codex", "gemini", "copilot", "opencode", "ollama", "git", "node", "uv", "pipx"):
        found = shutil.which(name)
        version = run([found, "--version"], timeout=60)[1] if found and name != "ixel" else ""
        say(f"{name}: {found or 'not found'}  {version.splitlines()[0] if version else ''}")
    if sys.platform == "win32":
        code, out = run(["where.exe", "ixel"], timeout=30)
        if code == 0 and out:
            say("every ixel on PATH: " + " | ".join(out.splitlines()))
            for path in out.splitlines():
                if path.lower().endswith(".bat") and _is_console_launcher(Path(path)):
                    say(f"NOTE: {path} is Ixel Console's old launcher, not Ixel MAT. Re-run Ixel Console's "
                        "installer: its command is ixel-console now, and it removes this file.")


def _is_console_launcher(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "ixel_console.py" in text


# ── 2. Tests ─────────────────────────────────────────────────────────────────

def pytest() -> None:
    section("2. Test suite (fake models; the live CLI checks run for each CLI you have)")
    code, out = run([sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider"], cwd=ROOT,
                    env={**os.environ, "IXEL_NO_UPDATE_CHECK": "1"}, timeout=3600)
    lines = out.splitlines()
    keep = [ln for ln in lines if ln.startswith(("FAILED", "ERROR", "SKIPPED")) or " passed" in ln
            or " failed" in ln]
    for line in (keep or lines[-15:])[-60:]:
        say(line)
    if code not in (0, 5):  # 5: nothing collected
        say("(details of failures)")
        for line in lines[-150:]:
            say("  " + line)


# ── 3. The installer ─────────────────────────────────────────────────────────

def installer() -> None:
    section("3. install.ps1 into a temporary folder")
    if sys.platform != "win32":
        say("skipped: not Windows")
        return
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        say("skipped: PowerShell not found")
        return
    # A file Windows still holds (Defender, a process not yet gone) mustn't stop the report
    with tempfile.TemporaryDirectory(prefix="ixel-check-", ignore_cleanup_errors=True) as tmp:
        env = {**os.environ, "IXEL_INSTALL_ROOT": str(Path(tmp) / "IxelMAT"), "IXEL_BIN_DIR": str(Path(tmp) / "bin"),
               "IXEL_SKIP_PATH_UPDATE": "1", "IXEL_SKIP_APP_ENTRY": "1", "IXEL_NO_UPDATE_CHECK": "1", "CI": "1"}
        code, out = run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "install.ps1")],
                        env=env, timeout=1800)
        say(f"install.ps1 exit code: {code}")
        for line in out.splitlines()[-25:]:
            say("  " + line)
        wrapper = Path(tmp) / "bin" / "ixel.cmd"
        if code != 0 or not wrapper.exists():
            say("FAILED: the installer didn't finish" if code != 0 else f"FAILED: {wrapper} wasn't created")
            return
        say(f"ixel.cmd runs: {wrapper_command(str(wrapper))}")
        for args in (["version"], ["help"]):
            code, out = run(["cmd", "/c", str(wrapper), *args], env=env, timeout=120)
            first = out.splitlines()[0] if out else ""
            say(f"ixel {' '.join(args)} (installed copy): exit {code}  {first}")
            if blocked(code, out):
                say("FAILED: Windows blocked the installed ixel from starting (Smart App Control, or a Device Guard / "
                    "App Control policy), even though ixel.cmd runs python.exe")
            elif code != 0:
                say("FAILED: the installed ixel didn't run")


# ── 4. Your ixel ─────────────────────────────────────────────────────────────

def your_ixel() -> None:
    section("4. The ixel on your PATH")
    ixel = shutil.which("ixel")
    if not ixel:
        say("not installed (or not on PATH in this window)")
        return
    command = wrapper_command(ixel)
    if command:
        say(f"{ixel} runs: {command}")
    for args in (["version"], ["doctor"]):
        code, out = run([ixel, *args], env={**os.environ, "IXEL_NO_UPDATE_CHECK": "1", "NO_COLOR": "1"},
                        timeout=120)
        say(f"ixel {' '.join(args)}: exit {code}")
        for line in out.splitlines()[:40]:
            say("  " + line)
        if blocked(code, out):
            say(f"FAILED: Windows blocked {ixel} from starting (Smart App Control, or a Device Guard / App Control "
                "policy).")
            if "ixel.exe" in (command or ixel).lower():
                say("It rejects the unsigned ixel.exe that pip writes. The fix is an install.ps1 whose ixel.cmd runs "
                    "the environment's python.exe (-I -m ixel_mat) instead:")
                say("  git pull in the folder you installed Ixel from, then run its install.ps1 again; or run this "
                    "checkout's (ixel update then updates from here):")
                say(f'  powershell -ExecutionPolicy Bypass -File "{ROOT / "install.ps1"}"')
                say("Then, if you set up the Ixel plugin in an app, run ixel mcp --setup again and use its new command "
                    "there.")
                say("A pipx or uv copy only has ixel.exe: run python -I -m ixel_mat with that copy's Python instead.")
            return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-tests", action="store_true", help="skip the test suite")
    parser.add_argument("--no-installer", action="store_true", help="skip the install.ps1 check")
    parser.add_argument("--report", default="windows-check-report.txt", help="where to write the report")
    args = parser.parse_args()

    try:
        machine()
        if not args.no_tests:
            pytest()
        if not args.no_installer:
            installer()
        your_ixel()
    except Exception as exc:
        say(f"FAILED: the check stopped early: {type(exc).__name__}: {exc}")
        raise
    finally:  # whatever happened, there's a report to send back
        Path(args.report).write_text("\n".join(REPORT) + "\n", encoding="utf-8")
        print(f"\nReport written to {Path(args.report).resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
