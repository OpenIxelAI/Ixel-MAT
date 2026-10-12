"""Show in folder for a diagram page: Finder, Explorer, and on Linux FileManager1 with the xdg-open fallback."""
import subprocess
import sys
from pathlib import Path

import pytest

from ixel_mat.addons import diagram

EVERY_PROGRAM = {"gdbus", "dbus-send", "xdg-open"}
# A comma, a single quote and a backslash as they are in a URI that was not percent-encoded
RAW_URI = "file:///tmp/a,b'c\\d.html"


class _Run:
    """Stands in for subprocess.run: keeps each call, then answers with the exit code chosen for the program
    (0 unless said) or raises the error chosen for it."""

    def __init__(self, codes: dict[str, int], errors: dict[str, Exception]):
        self.codes = codes
        self.errors = errors
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        name = Path(argv[0]).name
        if name in self.errors:
            raise self.errors[name]
        return subprocess.CompletedProcess(argv, self.codes.get(name, 0))


def _system(monkeypatch, platform: str, found=(), codes=None, errors=None) -> _Run:
    """Be this platform with only the programs in found on PATH; what gets started is kept in the result."""
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(diagram, "find_on_path", lambda name: f"/usr/bin/{name}" if name in found else None)
    run = _Run(codes or {}, errors or {})
    monkeypatch.setattr(diagram.subprocess, "run", run)
    return run


def _started(run: _Run) -> list[list[str]]:
    """Each argv that was started, after checking it is a list of strings and no shell was asked for."""
    for argv, kwargs in run.calls:
        assert isinstance(argv, list) and all(isinstance(part, str) for part in argv), argv
        assert not kwargs.get("shell")
    return [argv for argv, _ in run.calls]


def _timeouts(run: _Run) -> list[float]:
    return [kwargs["timeout"] for _, kwargs in run.calls]


def _gdbus(items: str) -> list[str]:
    return ["/usr/bin/gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1",
            "--object-path", "/org/freedesktop/FileManager1",
            "--method", "org.freedesktop.FileManager1.ShowItems", items, "''"]


def _dbus_send(items: str) -> list[str]:
    return ["/usr/bin/dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.FileManager1",
            "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1.ShowItems", items, "string:"]


@pytest.fixture
def page(tmp_path):
    folder = tmp_path / "made"
    folder.mkdir()
    made = folder / "diagram.html"
    made.write_bytes(b"<html></html>")
    return made


# ── Linux: the file manager's FileManager1 API ───────────────────────────────

def test_linux_selects_the_file_through_gdbus_and_starts_nothing_else(page, monkeypatch):
    run = _system(monkeypatch, "linux", EVERY_PROGRAM)
    assert diagram.show_in_folder(page) is True
    assert _started(run) == [_gdbus(f"['{page.absolute().as_uri()}']")]
    assert _timeouts(run) == [5]


def test_linux_without_gdbus_selects_the_file_through_dbus_send(page, monkeypatch):
    run = _system(monkeypatch, "linux", {"dbus-send", "xdg-open"})
    assert diagram.show_in_folder(page) is True
    assert _started(run) == [_dbus_send(f"array:string:{page.absolute().as_uri()}")]
    assert _timeouts(run) == [5]


def test_linux_without_either_opens_the_folder_with_xdg_open(page, monkeypatch):
    run = _system(monkeypatch, "linux", {"xdg-open"})
    assert diagram.show_in_folder(page) is True
    assert _started(run) == [["/usr/bin/xdg-open", str(page.parent)]]
    assert _timeouts(run) == [15]


# ── Linux: when the call fails ───────────────────────────────────────────────

@pytest.mark.parametrize("found, codes, errors, tried", [
    (EVERY_PROGRAM, {"gdbus": 1}, {}, "gdbus"),
    (EVERY_PROGRAM, {}, {"gdbus": subprocess.TimeoutExpired("gdbus", 5)}, "gdbus"),
    (EVERY_PROGRAM, {}, {"gdbus": OSError("Exec format error")}, "gdbus"),
    ({"dbus-send", "xdg-open"}, {"dbus-send": 1}, {}, "dbus-send"),
    ({"dbus-send", "xdg-open"}, {}, {"dbus-send": subprocess.TimeoutExpired("dbus-send", 5)}, "dbus-send"),
], ids=["gdbus exits 1", "gdbus times out", "gdbus cannot start", "dbus-send exits 1", "dbus-send times out"])
@pytest.mark.parametrize("opener_code, shown", [(0, True), (3, False)])
def test_linux_tries_one_dbus_call_then_opens_the_folder(page, monkeypatch, found, codes, errors, tried,
                                                         opener_code, shown):
    run = _system(monkeypatch, "linux", found, {**codes, "xdg-open": opener_code}, errors)
    assert diagram.show_in_folder(page) is shown
    started = _started(run)
    assert [argv[0] for argv in started] == [f"/usr/bin/{tried}", "/usr/bin/xdg-open"]
    assert started[1] == ["/usr/bin/xdg-open", str(page.parent)]
    assert _timeouts(run) == [5, 15]


def test_linux_with_nothing_to_start_shows_nothing_and_starts_nothing(page, monkeypatch):
    run = _system(monkeypatch, "linux")
    assert diagram.show_in_folder(page) is False
    assert _started(run) == []


@pytest.mark.parametrize("codes, errors", [
    ({"xdg-open": 4}, {}),
    ({}, {"xdg-open": OSError("Exec format error")}),
    ({}, {"xdg-open": subprocess.TimeoutExpired("xdg-open", 15)}),
], ids=["exits 4", "cannot start", "times out"])
def test_linux_where_xdg_open_does_not_work_says_nothing_was_shown(page, monkeypatch, codes, errors):
    run = _system(monkeypatch, "linux", {"xdg-open"}, codes, errors)
    assert diagram.show_in_folder(page) is False
    assert _started(run) == [["/usr/bin/xdg-open", str(page.parent)]]


def test_linux_with_no_uri_for_the_path_goes_straight_to_xdg_open(page, monkeypatch):
    monkeypatch.setattr(diagram, "_file_uri", lambda path: None)
    run = _system(monkeypatch, "linux", EVERY_PROGRAM)
    assert diagram.show_in_folder(page) is True
    assert _started(run) == [["/usr/bin/xdg-open", str(page.parent)]]


# ── Linux: commas, quotes and backslashes ────────────────────────────────────

def test_a_path_with_a_comma_and_a_quote_is_one_gdbus_item(tmp_path, monkeypatch):
    page = tmp_path / "a,b" / "it's,here.html"
    page.parent.mkdir()
    page.write_bytes(b"<html></html>")
    run = _system(monkeypatch, "linux", EVERY_PROGRAM)
    assert diagram.show_in_folder(page) is True
    (argv,) = _started(run)
    items = argv[-2]
    assert len(argv) == 11 and argv[-1] == "''"
    assert items.startswith("['") and items.endswith("']")
    assert items[2:-2] == page.absolute().as_uri()


def test_a_path_with_a_comma_is_one_dbus_send_item_once_percent_encoded(tmp_path, monkeypatch):
    page = tmp_path / "a,b" / "it's,here.html"
    page.parent.mkdir()
    page.write_bytes(b"<html></html>")
    run = _system(monkeypatch, "linux", {"dbus-send", "xdg-open"})
    assert diagram.show_in_folder(page) is True
    (argv,) = _started(run)
    assert argv == _dbus_send(f"array:string:{page.absolute().as_uri()}")
    assert "," not in argv[-2].removeprefix("array:string:")


def test_a_raw_comma_quote_and_backslash_stay_one_gdbus_item(page, monkeypatch):
    monkeypatch.setattr(diagram, "_file_uri", lambda path: RAW_URI)
    run = _system(monkeypatch, "linux", EVERY_PROGRAM)
    assert diagram.show_in_folder(page) is True
    assert _started(run) == [_gdbus(r"['file:///tmp/a,b\'c\\d.html']")]


def test_a_raw_comma_keeps_dbus_send_out_and_opens_the_folder(page, monkeypatch):
    monkeypatch.setattr(diagram, "_file_uri", lambda path: RAW_URI)
    run = _system(monkeypatch, "linux", {"dbus-send", "xdg-open"})
    assert diagram.show_in_folder(page) is True
    assert _started(run) == [["/usr/bin/xdg-open", str(page.parent)]]
    assert _timeouts(run) == [15]


# ── macOS and Windows keep what they did ─────────────────────────────────────

@pytest.mark.parametrize("code, shown", [(0, True), (1, False)])
def test_a_mac_reveals_the_file_in_finder(page, monkeypatch, code, shown):
    run = _system(monkeypatch, "darwin", EVERY_PROGRAM, {"open": code})
    assert diagram.show_in_folder(page) is shown
    assert _started(run) == [["/usr/bin/open", "-R", str(page)]]
    assert _timeouts(run) == [15]


@pytest.mark.parametrize("code", [0, 1])
def test_windows_selects_the_file_in_explorer_whatever_the_exit_code(page, monkeypatch, code):
    run = _system(monkeypatch, "win32", {"explorer"} | EVERY_PROGRAM, {"explorer": code})
    assert diagram.show_in_folder(page) is True
    assert _started(run) == [["/usr/bin/explorer", f"/select,{page}"]]
    assert _timeouts(run) == [15]


def test_windows_without_explorer_shows_nothing_and_starts_nothing(page, monkeypatch):
    run = _system(monkeypatch, "win32", EVERY_PROGRAM)
    assert diagram.show_in_folder(page) is False
    assert _started(run) == []
