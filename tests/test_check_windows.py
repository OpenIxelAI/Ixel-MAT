"""scripts/check_windows.py: the report says when Windows won't start the ixel you use, and what fixes it."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_windows.py"
# What the owner's Windows 11 PC said, with Smart App Control on
BLOCKED_TEXT = (r"C:\Users\ana\AppData\Local\IxelMAT\.venv\Scripts\ixel.exe was blocked by your organization's "
                "Device Guard policy")


@pytest.fixture
def check():
    spec = importlib.util.spec_from_file_location("check_windows", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _your_ixel(check, monkeypatch, program, result):
    ran = []
    monkeypatch.setattr(check, "shutil", SimpleNamespace(which=lambda name: str(program) if name == "ixel" else None))
    monkeypatch.setattr(check, "run", lambda argv, **kw: ran.append(argv) or result)
    check.your_ixel()
    return ran, "\n".join(check.REPORT)


def test_windows_blocking_ixel_is_recognized(check):
    assert check.blocked(4551, "")  # cmd.exe's exit code when it can't start ixel.exe
    assert check.blocked(1, BLOCKED_TEXT)
    assert check.blocked(None, "[WinError 4551] An Application Control policy has blocked this file")
    assert not check.blocked(0, "  Ixel MAT v0.3.0") and not check.blocked(1, "Unknown command: nope")


def test_an_old_ixel_cmd_that_windows_blocks_is_reported_with_the_fix(check, tmp_path, monkeypatch):
    wrapper = tmp_path / "ixel.cmd"
    wrapper.write_text('@echo off\r\n"C:\\Users\\ana\\AppData\\Local\\IxelMAT\\.venv\\Scripts\\ixel.exe" %*',
                       encoding="ascii")
    ran, report = _your_ixel(check, monkeypatch, wrapper, (4551, BLOCKED_TEXT))
    assert ran == [[str(wrapper), "version"]]  # the ixel.cmd you run; doctor isn't tried
    assert f"{wrapper} runs: " in report and "ixel.exe" in report
    assert f"FAILED: Windows blocked {wrapper} from starting" in report
    # An install.ps1 with the fix: the one you installed from once it's pulled, or this checkout's, exactly
    assert "git pull in the folder you installed Ixel from, then run its install.ps1 again" in report
    assert f'powershell -ExecutionPolicy Bypass -File "{SCRIPT.parent.parent / "install.ps1"}"' in report
    assert "run ixel mcp --setup again" in report  # an app's plugin still runs ixel.exe
    assert "python -I -m ixel_mat with that copy's Python" in report  # for a pipx or uv copy


def test_ixel_exe_itself_blocked_is_reported_with_the_fix(check, monkeypatch):
    exe = r"C:\Users\ana\pipx\bin\ixel.exe"
    ran, report = _your_ixel(check, monkeypatch, exe, (None, "[WinError 4551] An Application Control policy "
                                                             "has blocked this file"))
    assert ran == [[exe, "version"]] and "FAILED: Windows blocked" in report and "install.ps1 again" in report


def test_an_ixel_that_starts_is_not_failed(check, tmp_path, monkeypatch):
    wrapper = tmp_path / "ixel.cmd"
    wrapper.write_text('@echo off\r\n"C:\\IxelMAT\\.venv\\Scripts\\python.exe" -I -m ixel_mat %*', encoding="ascii")
    ran, report = _your_ixel(check, monkeypatch, wrapper, (0, "Ixel MAT v0.3.0"))
    assert [argv[-1] for argv in ran] == ["version", "doctor"]
    assert "-I -m ixel_mat %*" in report and "FAILED" not in report
    # If Windows blocked even python.exe, reinstalling wouldn't help: the report doesn't say it would
    check.REPORT.clear()
    _, report = _your_ixel(check, monkeypatch, wrapper, (4551, "blocked by your organization's Device Guard policy"))
    assert "FAILED: Windows blocked" in report and "install.ps1" not in report
