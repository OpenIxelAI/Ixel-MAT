"""A screenshot picks the capture program for this computer, and says what to install when there isn't one.

The platform, the environment and the programs on PATH are all patched, so every case runs on every OS and
nothing here starts a program.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import doc_samples as samples
from ixel_mat.agent import tools
from ixel_mat.agent.sandbox import CommandResult
from ixel_mat.agent.tools import Workspace, WorkspaceError

PNG = "/tmp/ixel-agent-screen-x/screen.png"
WINDOWS_PNG = "C:\\Users\\me\\AppData\\Local\\Temp\\ixel-agent-screen-x\\screen.png"
FIXED_START = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]


def _on(monkeypatch: pytest.MonkeyPatch, platform: str, wayland: str | None = None, x11: str | None = None,
        installed: tuple[str, ...] = ()) -> None:
    """Pretend to be this platform, with these display variables and only these programs on PATH."""
    monkeypatch.setattr(tools.sys, "platform", platform)
    for name, value in (("WAYLAND_DISPLAY", wayland), ("DISPLAY", x11)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.setattr(tools.shutil, "which", lambda name, *args, **kwargs: name if name in installed else None)


def _literal(path: str) -> str:
    """How the script holds a path: one single-quoted PowerShell string, its own quotes doubled."""
    return "'" + path.replace("'", "''") + "'"


# ── The words for each platform ──────────────────────────────────────────────

def test_macos_uses_screencapture_without_looking_for_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "darwin")
    assert Workspace._screenshot_argv(PNG) == ["screencapture", "-x", PNG]


def test_windows_runs_a_fixed_powershell_script(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "win32", installed=("powershell.exe",))
    argv = Workspace._screenshot_argv(WINDOWS_PNG)
    assert isinstance(argv, list) and argv[:4] == FIXED_START and len(argv) == 5
    script = argv[4]
    for part in ("System.Windows.Forms", "System.Drawing", "VirtualScreen", "CopyFromScreen",
                 "ImageFormat]::Png"):
        assert part in script
    assert f"$bmp.Save({_literal(WINDOWS_PNG)}, " in script


def test_windows_script_is_the_same_whatever_the_path_is(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "win32", installed=("powershell.exe",))
    other = "D:\\Temp\\ixel-agent-screen-y\\screen.png"
    first = Workspace._screenshot_argv(WINDOWS_PNG)[4]
    second = Workspace._screenshot_argv(other)[4]
    assert first.replace(_literal(WINDOWS_PNG), "PATH") == second.replace(_literal(other), "PATH")
    assert "PATH" in first.replace(_literal(WINDOWS_PNG), "PATH")


@pytest.mark.parametrize("path", [
    "C:\\Users\\O'Brien\\Temp\\screen.png",
    "C:\\Users\\a\u2019b\\Temp\\screen.png",                  # PowerShell reads a curly quote as a quote too
    "C:\\x'; Remove-Item -Recurse C:\\; '\\screen.png",       # an attempt to end the string and run more
    "C:\\Users\\$env:USERNAME\\`n\\$(calc)\\screen.png",      # nothing inside single quotes is expanded
])
def test_windows_path_stays_inside_one_quoted_literal(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    _on(monkeypatch, "win32", installed=("powershell.exe",))
    argv = Workspace._screenshot_argv(path)
    assert isinstance(argv, list)
    doubled = "".join(char * 2 if char in "'\u2018\u2019\u201a\u201b" else char for char in path)
    assert f"$bmp.Save('{doubled}', [System.Drawing.Imaging.ImageFormat]::Png)" in argv[4]


@pytest.mark.parametrize("path", ['C:\\a"b\\screen.png', "C:\\a\nb\\screen.png", "C:\\a\rb\\screen.png"])
def test_windows_refuses_a_path_with_a_double_quote_or_line_break(monkeypatch: pytest.MonkeyPatch,
                                                                  path: str) -> None:
    _on(monkeypatch, "win32", installed=("powershell.exe",))
    message = Workspace._screenshot_argv(path)
    assert isinstance(message, str) and "path" in message


def test_windows_without_powershell_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "win32", installed=("grim", "scrot"))
    message = Workspace._screenshot_argv(WINDOWS_PNG)
    assert isinstance(message, str) and "powershell.exe" in message


def test_wayland_uses_grim(monkeypatch: pytest.MonkeyPatch) -> None:
    # XWayland sets DISPLAY too, and its X11 tools would be tried only on a real X11 session
    _on(monkeypatch, "linux", wayland="wayland-0", x11=":0", installed=("scrot", "gnome-screenshot", "grim"))
    assert Workspace._screenshot_argv(PNG) == ["grim", PNG]


def test_wayland_without_grim_names_grim_and_does_not_try_x11_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "linux", wayland="wayland-0", x11=":0", installed=("scrot", "gnome-screenshot", "import"))
    message = Workspace._screenshot_argv(PNG)
    assert isinstance(message, str) and "grim" in message and "install" in message


@pytest.mark.parametrize("installed,expected", [
    (("grim", "gnome-screenshot", "scrot", "import"), ["gnome-screenshot", "-f", PNG]),
    (("grim", "scrot", "import"), ["scrot", "-o", PNG]),
    (("grim", "import"), ["import", "-window", "root", PNG]),
])
def test_x11_tries_gnome_screenshot_then_scrot_then_import(monkeypatch: pytest.MonkeyPatch,
                                                           installed: tuple[str, ...],
                                                           expected: list[str]) -> None:
    _on(monkeypatch, "linux", x11=":0", installed=installed)
    assert Workspace._screenshot_argv(PNG) == expected


def test_x11_with_nothing_installed_names_what_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "linux", x11=":0", installed=("grim",))
    message = Workspace._screenshot_argv(PNG)
    assert isinstance(message, str) and "install" in message
    for name in ("scrot", "gnome-screenshot", "ImageMagick"):
        assert name in message


@pytest.mark.parametrize("wayland,x11", [(None, None), ("", ""), (None, ""), ("", None)])
def test_no_display_is_a_clear_message_even_with_every_program_installed(
        monkeypatch: pytest.MonkeyPatch, wayland: str | None, x11: str | None) -> None:
    _on(monkeypatch, "linux", wayland=wayland, x11=x11,
        installed=("grim", "gnome-screenshot", "scrot", "import"))
    message = Workspace._screenshot_argv(PNG)
    assert isinstance(message, str) and "display" in message


# ── screenshot() itself ──────────────────────────────────────────────────────

@pytest.fixture
def seen_paths(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The capture path screenshot() gave _screenshot_argv, one per call."""
    paths: list[str] = []
    real = Workspace._screenshot_argv

    def spy(path: str) -> list[str] | str:
        paths.append(path)
        return real(path)

    monkeypatch.setattr(Workspace, "_screenshot_argv", staticmethod(spy))
    return paths


def _stand_in_for_run_command(monkeypatch: pytest.MonkeyPatch, paths: list[str], returncode: int = 0,
                              output: str = "") -> list[tuple[list[str], dict[str, Any]]]:
    """Replace run_command: record each call, and on success write a PNG where the capture would go."""
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], workspace: Path, *args: Any, **options: Any) -> CommandResult:
        calls.append((list(argv), options))
        if returncode == 0:
            Path(paths[-1]).write_bytes(samples.png())
        return CommandResult(list(argv), returncode, output, False, False)

    monkeypatch.setattr(tools, "run_command", run)
    return calls


@pytest.mark.parametrize("platform,wayland,x11,installed,word", [
    ("linux", None, None, ("grim", "scrot"), "display"),
    ("linux", "wayland-0", None, ("scrot",), "grim"),
    ("linux", None, ":0", (), "scrot"),
    ("win32", None, None, (), "PowerShell"),
])
def test_without_a_way_to_capture_it_returns_a_message_and_runs_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seen_paths: list[str], platform: str,
        wayland: str | None, x11: str | None, installed: tuple[str, ...], word: str) -> None:
    _on(monkeypatch, platform, wayland=wayland, x11=x11, installed=installed)
    calls = _stand_in_for_run_command(monkeypatch, seen_paths)
    viewed = Workspace(tmp_path, computer=True).screenshot()
    assert word in viewed.text and viewed.pictures == []
    assert calls == []
    # The private temp folder made for the capture is gone again
    assert len(seen_paths) == 1 and not Path(seen_paths[0]).parent.exists()


def test_linux_capture_runs_the_chosen_program_with_computer_access(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seen_paths: list[str]) -> None:
    _on(monkeypatch, "linux", x11=":0", installed=("scrot",))
    calls = _stand_in_for_run_command(monkeypatch, seen_paths)
    viewed = Workspace(tmp_path, computer=True).screenshot()
    assert len(calls) == 1
    argv, options = calls[0]
    assert argv == ["scrot", "-o", seen_paths[0]] and options["computer"] is True
    assert len(viewed.pictures) == 1
    assert (viewed.pictures[0].width, viewed.pictures[0].height) == (64, 48)
    assert not Path(seen_paths[0]).exists() and not Path(seen_paths[0]).parent.exists()


def test_windows_capture_runs_powershell_with_computer_access(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seen_paths: list[str]) -> None:
    _on(monkeypatch, "win32", installed=("powershell.exe",))
    calls = _stand_in_for_run_command(monkeypatch, seen_paths)
    viewed = Workspace(tmp_path, computer=True).screenshot()
    argv, options = calls[0]
    assert argv[:4] == FIXED_START and _literal(seen_paths[0]) in argv[4]
    assert options["computer"] is True
    assert len(viewed.pictures) == 1


def test_a_failed_capture_is_an_error_with_the_programs_output(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seen_paths: list[str]) -> None:
    _on(monkeypatch, "linux", wayland="wayland-0", installed=("grim",))
    _stand_in_for_run_command(monkeypatch, seen_paths, returncode=1,
                              output="compositor doesn't support wlr-screencopy")
    with pytest.raises(WorkspaceError, match="screenshot failed: compositor doesn't support"):
        Workspace(tmp_path, computer=True).screenshot()
    assert not Path(seen_paths[0]).parent.exists()


def test_screenshot_needs_computer_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                          seen_paths: list[str]) -> None:
    _on(monkeypatch, "linux", x11=":0", installed=("scrot",))
    calls = _stand_in_for_run_command(monkeypatch, seen_paths)
    with pytest.raises(WorkspaceError, match="Computer access is off"):
        Workspace(tmp_path).screenshot()
    assert calls == [] and seen_paths == []
