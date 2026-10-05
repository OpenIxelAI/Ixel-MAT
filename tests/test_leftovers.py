"""
Ixel takes away what Gemini CLI, Copilot and OpenCode keep of a question it asked them in its temp folder
(ixel_mat/agents/leftovers.py), and nothing else. Each CLI's folders are built here, in a home of the test's own,
the way the CLI leaves them (as checked against Gemini CLI 0.62.0, Copilot 1.0.91, OpenCode 1.18.34 and 2.0.22).
"""
import asyncio
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from ixel_mat.agents import leftovers
from ixel_mat.agents.base import AgentConfig
from ixel_mat.agents.oneshot import OneShotAgent

QUESTION = "What is our acquisition price for Zanzibarco"  # what must not be left anywhere
YOURS = "Your own question about Quetzalcoatlus"            # a session of yours, which must stay


def _env(home: Path, **more) -> dict[str, str]:
    return {"HOME": str(home), "USERPROFILE": str(home), "PATH": os.environ.get("PATH", ""), **more}


def _run_folder(tmp_path: Path) -> str:
    folder = tmp_path / "temp" / "ixel-agent-ab12cd34"
    folder.mkdir(parents=True)
    return str(folder)


def _everything(root: Path) -> bytes:
    """Every file's bytes under root, to look for text the way someone reading the disk would."""
    return b"".join(p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file())


def _has_fts5() -> bool:
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False


# ── Where each CLI keeps things ───────────────────────────────────────────────

def test_each_clis_folders_are_found_the_way_the_cli_finds_them(tmp_path):
    home = tmp_path / "home"
    where = leftovers.places(_env(home))
    assert where.gemini == home / ".gemini" and where.copilot == home / ".copilot" and where.opencode == ()
    data = home / ".local" / "share" / "opencode"
    data.mkdir(parents=True)
    for name in ("opencode.db", "opencode-beta.db", "notes.db"):
        (data / name).write_bytes(b"")
    assert leftovers.places(_env(home)).opencode == (data / "opencode-beta.db", data / "opencode.db")
    moved = leftovers.places(_env(home, GEMINI_CLI_HOME=str(tmp_path / "g"), COPILOT_HOME=str(tmp_path / "c"),
                                  XDG_DATA_HOME=str(tmp_path / "x"), OPENCODE_DB="mine.db"))
    assert moved.gemini == tmp_path / "g" / ".gemini" and moved.copilot == tmp_path / "c"
    assert moved.opencode == (tmp_path / "x" / "opencode" / "mine.db",)
    assert leftovers.places(_env(home, OPENCODE_DB=str(tmp_path / "full.db"))).opencode == (tmp_path / "full.db",)
    assert leftovers.places(_env(home, OPENCODE_DB=":memory:")).opencode == ()  # nothing on disk to clean
    assert leftovers.places(_env(home, SANDBOX="sandbox-exec")).gemini == home / ".cache" / ".gemini"


def test_the_tests_never_look_in_your_own_folders(tmp_path_factory):
    # tests/conftest.py: a stand-in CLI run with the suite's own environment is cleaned up after in a test folder
    base = tmp_path_factory.getbasetemp()
    for env in (dict(os.environ), {}):
        where = leftovers.places(env)
        assert all(base in place.parents for place in (where.gemini, where.copilot, *where.opencode))


@pytest.mark.parametrize("command", ["claude", "codex", "hermes", sys.executable, "/opt/bin/ixel-own-cli"])
def test_only_the_clis_that_keep_questions_are_cleaned_up_after(command, tmp_path):
    assert leftovers.prepare(command, [], _run_folder(tmp_path), _env(tmp_path)) is None


def test_a_folder_of_yours_is_left_alone(tmp_path):
    # An agent's own workdir holds your own sessions too: only Ixel's temp folder is cleaned up after
    for command in ("gemini", "copilot", "opencode"):
        assert leftovers.prepare(command, [], None, _env(tmp_path)) is None


def test_copilot_gets_a_session_and_a_log_folder_of_its_own(tmp_path):
    run = leftovers.prepare("/usr/local/bin/copilot", ["-s"], _run_folder(tmp_path), _env(tmp_path))
    session = run.args[run.args.index("--session-id") + 1]
    assert str(uuid.UUID(session)) == session == run.session_id
    log_dir = run.args[run.args.index("--log-dir") + 1]
    assert log_dir == run.log_dir and os.path.isdir(log_dir) and not log_dir.startswith(str(tmp_path / "temp"))
    run.drop()
    assert not os.path.exists(log_dir)
    for named in ("gemini", "opencode"):
        assert leftovers.prepare(named, [], _run_folder(tmp_path / named), _env(tmp_path)).args == []


@pytest.mark.parametrize("args", [["--resume"], ["-r", "abc"], ["--continue"], ["--session-id", "x"],
                                  ["--resume=abc"], ["--session-id=x"]])
def test_a_copilot_run_that_picks_its_own_session_keeps_it(args, tmp_path):
    run = leftovers.prepare("copilot", args, _run_folder(tmp_path), _env(tmp_path))
    assert run.session_id == "" and "--session-id" not in run.args
    run.drop()
    run = leftovers.prepare("copilot", ["--log-dir", "/x"], _run_folder(tmp_path / "b"), _env(tmp_path))
    assert "--log-dir" not in run.args and run.log_dir == "" and "--session-id" in run.args


# ── Gemini CLI ────────────────────────────────────────────────────────────────

def _gemini_project(root: Path, slug: str, owner: str, chat: str) -> None:
    for base in ("tmp", "history"):
        (root / base / slug).mkdir(parents=True, exist_ok=True)
        (root / base / slug / ".project_root").write_text(owner, encoding="utf-8")
    chats = root / "tmp" / slug / "chats"
    chats.mkdir()
    (chats / "session-2026-10-05T00-27-04216832.jsonl").write_text(json.dumps({"text": chat}), encoding="utf-8")


def _gemini_home(tmp_path: Path, folder: str, slug: str = "ixel-agent-ab12cd34") -> Path:
    root = tmp_path / "home" / ".gemini"
    root.mkdir(parents=True)
    (root / "settings.json").write_text('{"security": {"auth": {"selectedType": "gemini-api-key"}}}')
    (root / "installation_id").write_text("5d3c0f9e-0000-4000-8000-000000000000")
    yours = str(tmp_path / "your-project")
    (root / "projects.json").write_text(json.dumps({"projects": {yours: "your-project", folder: slug}}, indent=2))
    _gemini_project(root, slug, folder, QUESTION)
    _gemini_project(root, "your-project", yours, YOURS)
    return root


def test_gemini_clis_chat_of_the_run_is_removed_and_nothing_of_yours(tmp_path):
    folder = _run_folder(tmp_path)
    root = _gemini_home(tmp_path, folder)
    run = leftovers.prepare("gemini", [], folder, _env(tmp_path / "home"))
    run.clean()
    assert not (root / "tmp" / "ixel-agent-ab12cd34").exists() and not (root / "history" / "ixel-agent-ab12cd34").exists()
    assert json.loads((root / "projects.json").read_text())["projects"] == {str(tmp_path / "your-project"): "your-project"}
    assert QUESTION.encode() not in _everything(root)
    # Yours, your settings and Gemini CLI's id are untouched
    assert YOURS.encode() in (root / "tmp" / "your-project" / "chats").joinpath(
        "session-2026-10-05T00-27-04216832.jsonl").read_bytes()
    assert (root / "history" / "your-project" / ".project_root").is_file()
    assert (root / "settings.json").is_file() and (root / "installation_id").is_file()
    assert not (root / "projects.json.lock").exists() and not list(root.glob("projects.json.*.tmp"))


def test_gemini_clis_project_folder_is_found_by_its_list_and_must_name_the_run(tmp_path):
    folder = _run_folder(tmp_path)
    # Another folder named like the run's got the plain name first, so the run's has "-1" (Gemini CLI's way)
    root = _gemini_home(tmp_path, folder, slug="ixel-agent-ab12cd34-1")
    _gemini_project(root, "ixel-agent-ab12cd34", str(tmp_path / "elsewhere" / "ixel-agent-ab12cd34"), YOURS)
    leftovers.prepare("gemini", [], folder, _env(tmp_path / "home")).clean()
    assert not (root / "tmp" / "ixel-agent-ab12cd34-1").exists()
    assert (root / "tmp" / "ixel-agent-ab12cd34" / "chats").is_dir()  # its .project_root names another folder


def test_a_gemini_folder_with_no_mark_of_the_run_stays(tmp_path):
    folder = _run_folder(tmp_path)
    root = _gemini_home(tmp_path, folder)
    (root / "history" / "ixel-agent-ab12cd34" / ".project_root").unlink()
    (root / "tmp" / "ixel-agent-ab12cd34" / ".project_root").write_text(str(tmp_path / "someone-else"))
    leftovers.prepare("gemini", [], folder, _env(tmp_path / "home")).clean()
    assert (root / "tmp" / "ixel-agent-ab12cd34" / "chats").is_dir()
    assert (root / "history" / "ixel-agent-ab12cd34").is_dir()


@pytest.mark.skipif(os.name == "nt", reason="a link to the temp folder, as /var is to /private/var on a Mac")
def test_gemini_cli_writing_the_folders_real_path_still_counts(tmp_path):
    real = Path(_run_folder(tmp_path))
    (tmp_path / "link").symlink_to(real.parent)
    folder = str(tmp_path / "link" / real.name)  # what Ixel was given; Node's process.cwd() resolves the link
    root = _gemini_home(tmp_path, str(real))
    leftovers.prepare("gemini", [], folder, _env(tmp_path / "home")).clean()
    assert not (root / "tmp" / "ixel-agent-ab12cd34").exists()


def test_while_gemini_cli_holds_its_project_list_the_line_stays(tmp_path, monkeypatch):
    folder = _run_folder(tmp_path)
    root = _gemini_home(tmp_path, folder)
    (root / "projects.json.lock").mkdir()  # a Gemini CLI of yours is updating it
    monkeypatch.setattr(leftovers, "BUSY_SECONDS", 0.2)
    leftovers.prepare("gemini", [], folder, _env(tmp_path / "home")).clean()
    assert not (root / "tmp" / "ixel-agent-ab12cd34").exists()  # the chat goes all the same
    assert folder in json.loads((root / "projects.json").read_text())["projects"]
    assert (root / "projects.json.lock").is_dir()  # not Ixel's to remove


# ── Copilot ───────────────────────────────────────────────────────────────────

def _copilot_home(tmp_path: Path, folder: str, session: str, cwd: str | None = None) -> Path:
    root = tmp_path / "home" / ".copilot"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text('{"firstLaunchAt": "2026-10-05T00:00:00Z"}')
    other = str(uuid.uuid4())
    for sid, text in ((session, QUESTION), (other, YOURS)):
        state = root / "session-state" / sid
        (state / "checkpoints").mkdir(parents=True)
        (state / "events.jsonl").write_text(json.dumps({"data": {"content": text}}))
        locks = root / "session-state" / ".session-operation-locks"
        locks.mkdir(exist_ok=True)
        (locks / f"{sid}.lock").write_text("")
    db = sqlite3.connect(root / "session-store.db")
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, summary TEXT);
        CREATE TABLE turns (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id),
                            turn_index INTEGER NOT NULL, user_message TEXT, assistant_response TEXT);
        CREATE TABLE checkpoints (id INTEGER PRIMARY KEY AUTOINCREMENT,
                                  session_id TEXT NOT NULL REFERENCES sessions(id), title TEXT);
    """)
    if _has_fts5():
        db.execute("CREATE VIRTUAL TABLE search_index USING fts5(content, session_id UNINDEXED, "
                   "source_type UNINDEXED, source_id UNINDEXED)")
    for sid, where, text in ((session, cwd or folder, QUESTION), (other, str(tmp_path / "your-repo"), YOURS)):
        db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (sid, where, text))
        db.execute("INSERT INTO turns (session_id, turn_index, user_message, assistant_response) VALUES (?, 0, ?, ?)",
                   (sid, text, "391"))
        db.execute("INSERT INTO checkpoints (session_id, title) VALUES (?, ?)", (sid, text))
        if _has_fts5():
            db.execute("INSERT INTO search_index VALUES (?, ?, 'turn', '0')", (text, sid))
    db.commit()
    db.close()
    return root


def _rows(db_path: Path, sql: str) -> list:
    db = sqlite3.connect(db_path)
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def test_copilots_session_of_the_run_is_removed_and_nothing_of_yours(tmp_path):
    folder = _run_folder(tmp_path)
    run = leftovers.prepare("copilot", [], folder, _env(tmp_path / "home"))
    root = _copilot_home(tmp_path, folder, run.session_id)
    log_dir = run.log_dir
    run.clean()
    store = root / "session-store.db"
    assert not (root / "session-state" / run.session_id).exists()
    assert not (root / "session-state" / ".session-operation-locks" / f"{run.session_id}.lock").exists()
    assert _rows(store, "SELECT summary FROM sessions") == [(YOURS,)]
    assert _rows(store, "SELECT user_message FROM turns") == [(YOURS,)]
    assert _rows(store, "SELECT title FROM checkpoints") == [(YOURS,)]
    if _has_fts5():
        assert _rows(store, "SELECT content FROM search_index") == [(YOURS,)]
        assert _rows(store, "SELECT count(*) FROM search_index WHERE search_index MATCH 'Zanzibarco'") == [(0,)]
    # Not in the file's free space, its write-ahead log, or its full-text index (which keeps words in lower case)
    everything = _everything(root)
    assert b"Zanzibarco" not in everything and b"zanzibarco" not in everything and YOURS.encode() in everything
    assert (root / "config.json").is_file() and len(list((root / "session-state").iterdir())) == 2
    assert not os.path.exists(log_dir)


def test_while_a_cli_of_yours_has_the_database_open_its_log_is_emptied_all_the_same(tmp_path):
    # SQLite empties the write-ahead log, which holds the question as it was written, when the last program using
    # the database closes it: with one of yours still open, that's not Ixel's own connection
    folder = _run_folder(tmp_path)
    run = leftovers.prepare("copilot", [], folder, _env(tmp_path / "home"))
    store = tmp_path / "home" / ".copilot" / "session-store.db"
    store.parent.mkdir(parents=True)
    yours = sqlite3.connect(store)
    yours.execute("PRAGMA journal_mode = WAL")
    yours.execute("SELECT * FROM sqlite_master").fetchall()  # open in WAL mode, as a running CLI has it
    try:
        root = _copilot_home(tmp_path, folder, run.session_id)
        assert QUESTION.encode() in (root / "session-store.db-wal").read_bytes()
        run.clean()
        assert b"Zanzibarco" not in _everything(root)
        assert yours.execute("SELECT summary FROM sessions").fetchall() == [(YOURS,)]
    finally:
        yours.close()


def test_a_copilot_session_from_another_folder_stays(tmp_path):
    folder = _run_folder(tmp_path)
    run = leftovers.prepare("copilot", [], folder, _env(tmp_path / "home"))
    root = _copilot_home(tmp_path, folder, run.session_id, cwd=str(tmp_path / "somewhere-else"))
    run.clean()
    assert _rows(root / "session-store.db", "SELECT count(*) FROM turns") == [(2,)]


def test_copilot_run_that_resumed_a_session_of_yours_leaves_it(tmp_path):
    folder = _run_folder(tmp_path)
    run = leftovers.prepare("copilot", ["--resume", "abc"], folder, _env(tmp_path / "home"))
    root = _copilot_home(tmp_path, folder, str(uuid.uuid4()))
    before = _everything(root)
    run.clean()
    assert _everything(root) == before


# ── OpenCode ──────────────────────────────────────────────────────────────────

OPENCODE_1 = """
    CREATE TABLE project (id text PRIMARY KEY, worktree text NOT NULL);
    CREATE TABLE session (id text PRIMARY KEY, project_id text NOT NULL, parent_id text, directory text NOT NULL,
                          title text NOT NULL);
    CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL, data text NOT NULL,
        CONSTRAINT fk FOREIGN KEY (session_id) REFERENCES session(id) ON DELETE CASCADE);
    CREATE TABLE part (id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL, data text NOT NULL,
        CONSTRAINT fk FOREIGN KEY (message_id) REFERENCES message(id) ON DELETE CASCADE);
    CREATE TABLE event_sequence (aggregate_id text PRIMARY KEY, seq integer NOT NULL);
    CREATE TABLE event (id text PRIMARY KEY, aggregate_id text NOT NULL, data text NOT NULL,
        CONSTRAINT fk FOREIGN KEY (aggregate_id) REFERENCES event_sequence(aggregate_id) ON DELETE CASCADE);
    CREATE TABLE credential (id text PRIMARY KEY, value text NOT NULL);
"""
OPENCODE_2 = """
    CREATE TABLE project (id text PRIMARY KEY, worktree text NOT NULL);
    CREATE TABLE project_directory (project_id text NOT NULL, directory text NOT NULL,
        CONSTRAINT fk FOREIGN KEY (project_id) REFERENCES project(id) ON DELETE CASCADE);
    CREATE TABLE session_v2 (id text PRIMARY KEY, project_id text NOT NULL, parent_id text, directory text NOT NULL,
                             title text);
    CREATE TABLE session_message (id text PRIMARY KEY, session_id text NOT NULL, data text NOT NULL,
        CONSTRAINT fk FOREIGN KEY (session_id) REFERENCES session_v2(id) ON DELETE CASCADE);
    CREATE TABLE instruction_state (session_id text PRIMARY KEY, current_values text NOT NULL,
        CONSTRAINT fk FOREIGN KEY (session_id) REFERENCES session_v2(id) ON DELETE CASCADE);
    CREATE TABLE instruction_blob (hash text PRIMARY KEY, value text NOT NULL);
    CREATE TABLE event_sequence (aggregate_id text PRIMARY KEY, seq integer NOT NULL);
    CREATE TABLE credential (id text PRIMARY KEY, value text NOT NULL);
"""


def _blob(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


def _opencode_db(path: Path, version: int, folder: str, yours: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(OPENCODE_1 if version == 1 else OPENCODE_2)
    db.execute("INSERT INTO credential VALUES ('anthropic', 'sk-ant-your-login')")
    sessions = "session" if version == 1 else "session_v2"
    run_project = "global" if version == 1 else "f23c3ce6"
    db.execute("INSERT INTO project VALUES (?, ?)", (run_project, "/" if version == 1 else folder))
    if version == 2:
        db.execute("INSERT INTO project VALUES ('yourproj', ?)", (yours,))
        db.execute("INSERT INTO project_directory VALUES (?, ?)", (run_project, folder))
    for sid, parent, where, text in (("ses_run", None, folder, QUESTION), ("ses_sub", "ses_run", folder + "-x", QUESTION),
                                     ("ses_yours", None, yours, YOURS)):
        project = run_project if where != yours or version == 1 else "yourproj"
        db.execute(f"INSERT INTO {sessions} VALUES (?, ?, ?, ?, ?)", (sid, project, parent, where, text))
        db.execute("INSERT INTO event_sequence VALUES (?, 3)", (sid,))
        if version == 1:
            db.execute("INSERT INTO message VALUES (?, ?, ?)", ("msg_" + sid, sid, json.dumps({"role": "user"})))
            db.execute("INSERT INTO part VALUES (?, ?, ?, ?)", ("prt_" + sid, "msg_" + sid, sid,
                                                                json.dumps({"text": text})))
            db.execute("INSERT INTO event VALUES (?, ?, ?)", ("evt_" + sid, sid, json.dumps({"text": text})))
        else:
            db.execute("INSERT INTO session_message VALUES (?, ?, ?)", ("msg_" + sid, sid, json.dumps({"text": text})))
            own = _blob(f"Working directory: {where}")
            db.execute("INSERT OR IGNORE INTO instruction_blob VALUES (?, ?)", (own, json.dumps(f"<env> {where}")))
            db.execute("INSERT OR IGNORE INTO instruction_blob VALUES (?, ?)", (_blob("date"), '"Mon Oct 05 2026"'))
            db.execute("INSERT INTO instruction_state VALUES (?, ?)", (sid, json.dumps(
                {"core/date": _blob("date"), "core/environment": own})))
    db.commit()
    db.close()


@pytest.mark.parametrize("version", [1, 2])
def test_opencodes_sessions_of_the_run_are_removed_and_nothing_of_yours(version, tmp_path):
    folder = _run_folder(tmp_path)
    yours = str(tmp_path / "your-repo")
    data = tmp_path / "home" / ".local" / "share" / "opencode"
    _opencode_db(data / "opencode.db", version, folder, yours)
    _opencode_db(data / "opencode-beta.db", version, folder, yours)  # a test release's, cleaned too
    (data / "auth.json").write_text('{"anthropic": "sk-ant-your-login"}')
    leftovers.prepare("opencode", [], folder, _env(tmp_path / "home")).clean()
    sessions = "session" if version == 1 else "session_v2"
    for db in (data / "opencode.db", data / "opencode-beta.db"):
        # The sub-agent's session it started (in a folder of its own) goes with it
        assert _rows(db, f"SELECT id FROM {sessions}") == [("ses_yours",)]
        assert _rows(db, "SELECT aggregate_id FROM event_sequence") == [("ses_yours",)]
        assert _rows(db, "SELECT value FROM credential") == [("sk-ant-your-login",)]
        if version == 1:
            assert _rows(db, "SELECT session_id FROM part") == [("ses_yours",)]
            assert _rows(db, "SELECT aggregate_id FROM event") == [("ses_yours",)]
            assert _rows(db, "SELECT id FROM project") == [("global",)]  # everyone's
        else:
            assert _rows(db, "SELECT session_id FROM session_message") == [("ses_yours",)]
            assert _rows(db, "SELECT id FROM project") == [("yourproj",)]  # the run's folder's is gone
            assert _rows(db, "SELECT count(*) FROM project_directory") == [(0,)]
            # The run's own instructions go; the date, which yours uses too, stays
            assert sorted(h for (h,) in _rows(db, "SELECT hash FROM instruction_blob")) == sorted(
                [_blob("date"), _blob(f"Working directory: {yours}")])
    everything = _everything(data)
    assert b"Zanzibarco" not in everything and b"ixel-agent-ab12cd34" not in everything
    assert YOURS.encode() in everything and (data / "auth.json").is_file()


def test_opencode_told_which_database_to_use_has_only_that_one_cleaned(tmp_path):
    folder = _run_folder(tmp_path)
    mine = tmp_path / "elsewhere" / "chosen.db"
    _opencode_db(mine, 2, folder, str(tmp_path / "your-repo"))
    default = tmp_path / "home" / ".local" / "share" / "opencode" / "opencode.db"
    _opencode_db(default, 2, folder, str(tmp_path / "your-repo"))
    leftovers.prepare("opencode", [], folder, _env(tmp_path / "home", OPENCODE_DB=str(mine))).clean()
    assert _rows(mine, "SELECT count(*) FROM session_v2") == [(1,)]
    assert _rows(default, "SELECT count(*) FROM session_v2") == [(3,)]


def test_a_home_folder_on_a_network_share_still_opens_its_databases():
    """Redirected and roaming Windows profiles keep the home folder on \\\\server\\share. as_uri() puts the server
    where SQLite wants none (file://server/...), and SQLite refused it, so nothing was removed."""
    share = PureWindowsPath(r"\\server\share\Users\me\.copilot\session-store.db")
    assert leftovers._sqlite_uri(share) == "file:////server/share/Users/me/.copilot/session-store.db"
    with pytest.raises(sqlite3.OperationalError, match="authority"):
        sqlite3.connect(f"{share.as_uri()}?mode=rw", uri=True)
    drive = PureWindowsPath(r"C:\Users\José Núñez\.local\share\opencode\opencode #1 100%?.db")
    assert leftovers._sqlite_uri(drive) == ("file:///C:/Users/Jos%C3%A9%20N%C3%BA%C3%B1ez/.local/share/opencode/"
                                            "opencode%20%231%20100%25%3F.db")
    assert leftovers._sqlite_uri(PurePosixPath("/home/me/a b.db")) == "file:///home/me/a%20b.db"


@pytest.mark.skipif(os.name == "nt", reason="// at the start is a network path on Windows")
def test_sqlite_opens_a_path_that_starts_with_two_slashes(tmp_path):
    # A network share's path starts with //, and SQLite takes it with no server in the URI; on Linux and macOS,
    # //home/... is /home/..., so this is the one place it can be opened for real
    db = tmp_path / "x.db"
    sqlite3.connect(db).execute("CREATE TABLE t (x)").connection.close()
    uri = leftovers._sqlite_uri(PurePosixPath("/" + db.as_posix()))
    assert uri.startswith("file:////")
    sqlite3.connect(f"{uri}?mode=rw", uri=True).execute("SELECT * FROM t").connection.close()


def test_a_home_folder_with_marks_uris_use_is_cleaned_all_the_same(tmp_path):
    # #, ?, % and spaces mean something in a URI: in a folder's name they're only letters
    home = tmp_path / "José #1 100%? home"
    folder = _run_folder(tmp_path)
    db = home / ".local" / "share" / "opencode" / "opencode.db"
    _opencode_db(db, 2, folder, str(tmp_path / "your-repo"))
    leftovers.prepare("opencode", [], folder, _env(home)).clean()
    assert _rows(db, "SELECT id FROM session_v2") == [("ses_yours",)]
    assert not (home / "José #1 100%").exists()  # no database made beside it


def test_a_database_with_nothing_of_the_run_isnt_written_to(tmp_path):
    folder = _run_folder(tmp_path)
    db = tmp_path / "home" / ".local" / "share" / "opencode" / "opencode.db"
    _opencode_db(db, 2, str(tmp_path / "other-run"), str(tmp_path / "your-repo"))
    before = db.stat().st_mtime_ns, db.read_bytes()
    leftovers.prepare("opencode", [], folder, _env(tmp_path / "home")).clean()
    assert (db.stat().st_mtime_ns, db.read_bytes()) == before


def test_cleaning_up_never_fails_the_question(tmp_path, caplog):
    folder = _run_folder(tmp_path)
    data = tmp_path / "home" / ".local" / "share" / "opencode"
    data.mkdir(parents=True)
    (data / "opencode.db").write_bytes(b"not a database " * 300)
    run = leftovers.prepare("opencode", [], folder, _env(tmp_path / "home"))
    run.clean()  # doesn't raise
    assert "Couldn't remove everything opencode kept of a question" in caplog.text
    run = leftovers.prepare("copilot", [], folder, _env(tmp_path / "home"))
    copilot = tmp_path / "home" / ".copilot"
    copilot.mkdir()
    (copilot / "session-store.db").write_bytes(b"not a database " * 300)
    run.clean()
    assert not os.path.exists(run.log_dir)  # Ixel's own log folder goes all the same


# ── After a question ──────────────────────────────────────────────────────────

# Stand-ins for the CLIs that save a question the way each does, in the home folder they're given
STAND_INS = {
    "gemini": """
import json, os, pathlib, sys
question = sys.stdin.read()
root = pathlib.Path(os.environ["HOME"], ".gemini")
folder = os.getcwd()
for base in ("tmp", "history"):
    (root / base / "ixel-agent").mkdir(parents=True, exist_ok=True)
    (root / base / "ixel-agent" / ".project_root").write_text(folder)
(root / "tmp" / "ixel-agent" / "chats").mkdir()
(root / "tmp" / "ixel-agent" / "chats" / "session-1.jsonl").write_text(question)
(root / "projects.json").write_text(json.dumps({"projects": {folder: "ixel-agent"}}))
print("391")
""",
    "copilot": """
import os, pathlib, sys
question = sys.stdin.read()
args = sys.argv[1:]
session, logs = args[args.index("--session-id") + 1], args[args.index("--log-dir") + 1]
state = pathlib.Path(os.environ["HOME"], ".copilot", "session-state", session)
state.mkdir(parents=True)
(state / "events.jsonl").write_text(question)
pathlib.Path(logs, "process-1.log").write_text("session " + session)
pathlib.Path(os.environ["HOME"], "args.txt").write_text("\\n".join(args))
print("391")
""",
}


@pytest.fixture
def stand_in(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    folder = tmp_path / "bin"
    folder.mkdir()
    for name, script in STAND_INS.items():
        program = folder / name
        program.write_text(f"#!{sys.executable}\n{script}", encoding="utf-8")
        program.chmod(0o755)
        if os.name == "nt":  # Windows finds a program by name only as a .exe, .cmd or the like
            (folder / f"{name}.cmd").write_text(f'@"{sys.executable}" "{program}" %*\r\n', encoding="utf-8")
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def _ask(command: str, args: list[str] | None = None, workdir: str = "temp") -> str:
    agent = OneShotAgent(AgentConfig(name=command, label=command, type="oneshot", command=command,
                                     args=args or [], prompt_via="stdin", workdir=workdir))

    async def ask():
        await agent.connect()
        return await agent.send_and_receive(QUESTION)
    return asyncio.run(ask())


def test_after_a_question_nothing_gemini_cli_kept_of_it_is_left(stand_in):
    assert _ask("gemini") == "391"
    assert not (stand_in / ".gemini" / "tmp" / "ixel-agent").exists()
    assert not (stand_in / ".gemini" / "history" / "ixel-agent").exists()
    assert json.loads((stand_in / ".gemini" / "projects.json").read_text()) == {"projects": {}}


def test_after_a_question_nothing_copilot_kept_of_it_is_left(stand_in):
    assert _ask("copilot", ["-s"]) == "391"
    args = (stand_in / "args.txt").read_text().split("\n")
    assert args[0] == "-s" and "--session-id" in args and "--log-dir" in args
    assert list((stand_in / ".copilot" / "session-state").iterdir()) == []
    assert not os.path.exists(args[args.index("--log-dir") + 1])


def test_a_question_asked_in_a_folder_of_yours_leaves_its_sessions_alone(stand_in, tmp_path):
    yours = tmp_path / "your-project"
    yours.mkdir()
    assert _ask("gemini", workdir=str(yours)) == "391"
    assert (stand_in / ".gemini" / "tmp" / "ixel-agent" / "chats" / "session-1.jsonl").is_file()
