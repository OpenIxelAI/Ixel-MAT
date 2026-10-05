"""What Ixel keeps of what you asked and ran: deleted on time as each command starts, and at once by
`ixel forget` and Settings' Forget button."""
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from ixel_mat import cli, conversation, forget, stats, update
from ixel_mat.commands import build_help_rows, resolve_command_name
from ixel_mat.config import loader, secrets
from ixel_mat.forget import window_folders  # the real one: conftest.py points forget's at the test's folder
from ixel_mat.machines import log, store
from ixel_mat.modes.review import EarlierTurn
from test_gui_server import AUTH, JSON_AUTH, make_gui, run_with_client

DAY = 24 * 60 * 60


def keep_everything():
    """A conversation, a Machines log (and an older Ixel's machines.log.1), the window's storage, and the
    files forget must leave alone."""
    conversation.save_conversation([EarlierTurn("What is my salary?", "90k")])
    log.write("CONNECT", machine="web", host="10.0.0.5", terminal="here")
    log.LOG_FILE.with_name("machines.log.1").write_text("old", encoding="utf-8")
    [window] = forget.window_folders()
    (window / "Default" / "Local Storage").mkdir(parents=True)
    (window / "Default" / "Local Storage" / "000003.log").write_text("ixel.handoff.project", encoding="utf-8")
    others = [stats.STATS_FILE, secrets._ENV_FILE, loader._GLOBAL_CONFIG, store.MACHINES_FILE, update.CHECK_FILE]
    for path in others:
        path.write_text("kept", encoding="utf-8")
    return window, others


def test_forget_deletes_the_conversation_the_log_and_the_window_storage_and_nothing_else():
    window, others = keep_everything()
    found = forget.forget()
    assert [(item.what, item.path, item.error) for item in found] == [
        (forget.CONVERSATION, conversation.CONVERSATION_FILE, ""),
        ("the Machines log", log.LOG_FILE, ""),
        ("the Machines log", log.LOG_FILE.with_name("machines.log.1"), ""),
        (forget.WINDOW, window, "")]
    assert not conversation.CONVERSATION_FILE.exists() and not log.LOG_FILE.exists() and not window.exists()
    assert not window.with_name("window.forgotten").exists()
    assert all(path.read_text(encoding="utf-8") == "kept" for path in others)
    assert forget.forget() == []  # nothing left to forget


def test_the_settings_button_leaves_the_window_storage_it_is_using():
    window, _ = keep_everything()
    found = forget.forget(window=False)
    assert {item.what for item in found} == {forget.CONVERSATION, "the Machines log"}
    assert (window / "Default" / "Local Storage" / "000003.log").exists()


def test_a_window_folder_in_use_is_left_whole(monkeypatch):
    window, _ = keep_everything()

    def refused(self, target):  # what Windows says while a window has files in it open
        raise PermissionError(13, "The process cannot access the file because it is being used by another process")
    monkeypatch.setattr(Path, "rename", refused)
    [item] = [item for item in forget.forget() if item.what == forget.WINDOW]
    assert item.error.startswith("The process cannot access the file")
    assert (window / "Default" / "Local Storage" / "000003.log").exists()  # all of it, not half


def test_what_couldnt_all_be_deleted_goes_the_next_time():
    [window] = forget.window_folders()
    aside = window.with_name("window.forgotten")
    (aside / "Default").mkdir(parents=True)
    (aside / "Default" / "History").write_text("x", encoding="utf-8")
    assert [(item.what, item.error) for item in forget.forget()] == [(forget.WINDOW, "")]
    assert not aside.exists()


def link(path, target):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, path, target_is_directory=True)
    except (OSError, NotImplementedError):  # Windows, without the right to make one
        pytest.skip("this computer won't make a symbolic link")


def someone_elses_folder(tmp_path):
    target = tmp_path / "someone-elses"
    (target / "inside").mkdir(parents=True)
    (target / "inside" / "notes.txt").write_text("kept", encoding="utf-8")
    return target, stat.S_IMODE(target.stat().st_mode)


def test_a_window_folder_that_links_elsewhere_loses_the_link_and_nothing_it_points_to(tmp_path):
    target, mode = someone_elses_folder(tmp_path)
    [window] = forget.window_folders()
    link(window, target)
    [item] = forget.forget()
    assert (item.what, item.path) == (forget.WINDOW, window)
    assert item.error == forget.LINKED.format(os.path.realpath(target))  # not reported as deleted
    assert not os.path.lexists(window) and not os.path.lexists(window.with_name("window.forgotten"))
    assert (target / "inside" / "notes.txt").read_text(encoding="utf-8") == "kept"
    assert stat.S_IMODE(target.stat().st_mode) == mode
    assert forget.forget() == []


def test_a_link_left_where_a_folder_goes_on_its_way_out_goes_and_nothing_it_points_to(tmp_path):
    target, mode = someone_elses_folder(tmp_path)
    [window] = forget.window_folders()
    link(window.with_name("window.forgotten"), target)
    assert [(item.what, item.error) for item in forget.forget()] == [(forget.WINDOW, "")]
    assert not os.path.lexists(window.with_name("window.forgotten"))
    assert (target / "inside" / "notes.txt").exists() and stat.S_IMODE(target.stat().st_mode) == mode


def test_a_link_inside_the_window_folder_is_never_followed(tmp_path, monkeypatch):
    target, mode = someone_elses_folder(tmp_path)
    [window] = forget.window_folders()
    link(window / "Default" / "linked", target)
    real, refused = os.unlink, []

    def unlink(path, *args, **kwargs):
        if Path(path).name == "linked":  # one that won't go: Ixel mustn't make it writable through the link
            refused.append(path)
            raise PermissionError(13, "Access is denied")
        return real(path, *args, **kwargs)
    monkeypatch.setattr(os, "unlink", unlink)
    [item] = forget.forget()
    assert item.error == "Access is denied" and refused
    assert (target / "inside" / "notes.txt").exists() and stat.S_IMODE(target.stat().st_mode) == mode
    monkeypatch.setattr(os, "unlink", real)
    assert [(item.what, item.error) for item in forget.forget()] == [(forget.WINDOW, "")]  # the next time
    assert (target / "inside" / "notes.txt").exists() and stat.S_IMODE(target.stat().st_mode) == mode


@pytest.mark.parametrize("system, env, expected", [
    ("win32", {"LOCALAPPDATA": "C:/Users/angel/AppData/Local"}, ["C:/Users/angel/AppData/Local/IxelMAT/window"]),
    ("darwin", {}, ["HOME/Library/Application Support/IxelMAT/window", "HOME/Library/WebKit/com.ixelai.ixel"]),
    ("linux", {}, ["HOME/.local/state/ixel-mat/window"]),
    ("linux", {"XDG_STATE_HOME": "/state"}, ["/state/ixel-mat/window"]),
])
def test_where_the_window_keeps_its_storage(system, env, expected, tmp_path):
    home = tmp_path / "home"
    assert window_folders(system, env, home) == [Path(e.replace("HOME", str(home))) for e in expected]


# ── ixel forget ───────────────────────────────────────────────────────────────

def test_ixel_forget_says_what_it_deleted(capsys):
    keep_everything()
    assert cli.cmd_forget([]) == 0
    out = capsys.readouterr().out
    assert "Deleted your last ixel review conversation" in out and "Deleted the Machines log" in out
    assert "Deleted the app window's storage" in out and "keys, settings, machines and usage stats are kept" in out
    assert "machines.log" in out.replace("\n", "")
    assert "salary" not in out
    assert cli.cmd_forget([]) == 0
    assert "Nothing to forget" in capsys.readouterr().out


def test_ixel_forget_says_what_it_couldnt_delete_and_what_to_do(monkeypatch, capsys):
    keep_everything()

    def refused(self, target):
        raise PermissionError(13, "Access is denied")
    monkeypatch.setattr(Path, "rename", refused)
    assert cli.cmd_forget([]) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "Couldn't delete the app window's storage" in out and "Access is denied" in out
    assert "If an Ixel window is open, close it, then run ixel forget again." in out
    assert "Deleted your last ixel review conversation" in out  # the rest still went


def test_ixel_forget_says_a_window_folder_was_only_a_link(tmp_path, capsys):
    target, _ = someone_elses_folder(tmp_path)
    [window] = forget.window_folders()
    link(window, target)
    assert cli.cmd_forget([]) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "Couldn't delete the app window's storage" in out and "it was a link to" in out
    assert "Ixel removed the link and left that folder as it was" in out and "close it" not in out


def test_forget_is_a_command_only_when_typed_in_full():
    assert resolve_command_name("forget", mode="cli") == "forget"
    for prefix in ("f", "fo", "for", "forge"):
        assert resolve_command_name(prefix, mode="cli") is None, prefix  # never by accident
    assert resolve_command_name("doc", mode="cli") == ("ambiguous", ["docs", "doctor"])
    assert resolve_command_name("forget", mode="mat") is None  # the terminal app keeps nothing on disk
    assert "ixel forget" in [usage for usage, _ in build_help_rows("cli")]


def test_ixel_forget_runs_from_the_command_line(monkeypatch, capsys):
    keep_everything()
    monkeypatch.setattr(sys, "argv", ["ixel", "forget"])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 0 and not conversation.CONVERSATION_FILE.exists()
    monkeypatch.setattr(sys, "argv", ["ixel", "forget", "--help"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "usage: ixel forget" in capsys.readouterr().out


# ── deleted on time ───────────────────────────────────────────────────────────

def test_every_ixel_command_first_deletes_what_is_past_its_time(monkeypatch):
    conversation.save_conversation([EarlierTurn("q", "a")], now=time.time() - DAY - 60)
    log.LOG_FILE.write_text(f'{log._stamp(time.time())} RUN_START command="cat notes" machines="web"\n',
                            encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["ixel", "version"])
    cli.main()
    assert not conversation.CONVERSATION_FILE.exists()
    assert "cat notes" not in log.LOG_FILE.read_text(encoding="utf-8")


def test_a_young_conversation_stays(monkeypatch):
    conversation.save_conversation([EarlierTurn("q", "a")])
    monkeypatch.setattr(sys, "argv", ["ixel", "version"])
    cli.main()
    assert conversation.load_conversation() == [EarlierTurn("q", "a")]


def test_the_check_never_stops_a_command(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(conversation, "expire", broken)
    forget.tidy()
    monkeypatch.setattr(sys, "argv", ["ixel", "version"])
    cli.main()


# ── Settings' Forget button ───────────────────────────────────────────────────

def test_settings_forget_button_deletes_the_conversation_and_the_log():
    window, others = keep_everything()

    async def scenario(client):
        ok = await client.post("/api/forget", headers=JSON_AUTH, json={})
        again = await client.post("/api/forget", headers=JSON_AUTH, json={})
        no_key = await client.post("/api/forget", headers={"Content-Type": "application/json"}, json={})
        cross = await client.post("/api/forget", headers={**JSON_AUTH, "Origin": "https://evil.example"}, json={})
        read = await client.get("/api/forget", headers=AUTH)
        return ok.status, await ok.json(), await again.json(), no_key.status, cross.status, read.status

    status, reply, again, no_key, cross, read = run_with_client(make_gui()[0], scenario)
    assert status == 200 and [item["what"] for item in reply["forgotten"]] == [
        forget.CONVERSATION, "the Machines log", "the Machines log"]
    assert all(item["error"] == "" for item in reply["forgotten"])
    assert again == {"forgotten": []} and (no_key, cross, read) == (401, 403, 405)
    assert window.exists() and all(path.exists() for path in others)


def test_settings_page_has_the_forget_button():
    script = (Path(cli.__file__).parent / "gui" / "static" / "settings.js").read_text(encoding="utf-8")
    card = script[script.index("function kept()"):]
    assert '"/api/forget"' in card and '"kept:confirm"' in card and "kept()," in script
    assert "\u2014" not in card  # no em-dash in what the page says


def test_a_read_only_file_in_the_window_folder_goes_too(monkeypatch):
    # Windows won't delete a read-only file until its flag is cleared
    [window] = forget.window_folders()
    (window / "Default").mkdir(parents=True)
    locked = window / "Default" / "Preferences"
    locked.write_text("{}", encoding="utf-8")
    os.chmod(locked, stat.S_IREAD)
    real, refused = os.unlink, []

    def unlink(path, *args, **kwargs):
        if Path(path).name == "Preferences" and not refused:  # as Windows does, once
            refused.append(path)
            raise PermissionError(13, "Access is denied")
        return real(path, *args, **kwargs)
    monkeypatch.setattr(os, "unlink", unlink)
    assert [(item.what, item.error) for item in forget.forget()] == [(forget.WINDOW, "")]
    assert refused and not window.exists() and not window.with_name("window.forgotten").exists()
