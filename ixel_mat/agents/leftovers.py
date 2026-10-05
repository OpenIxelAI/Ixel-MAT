"""
What Gemini CLI, GitHub Copilot and OpenCode keep of a question Ixel asks them, and taking it away again.

Each of them saves every question and its answer under your home folder, and none has a switch that stops it
when it's run the way Ixel runs it (Claude Code and Codex have one, and their presets use it). So after a run
in Ixel's own empty temp folder, once the CLI has exited, Ixel removes what that run left, and only that:
never a login, a setting, or a session of yours.

- Gemini CLI: the run's folders in ~/.gemini/tmp and ~/.gemini/history (its chat, which has the question and
  the answer), each only if its .project_root names the run's folder, and the run's line in projects.json.
  The same in ~/.cache/.gemini, where it keeps them when its own sandbox is on, on a Mac.
- Copilot: Ixel gives the run a session id and a log folder of its own, then removes that session's folder
  in ~/.copilot/session-state, its lock, and its rows in ~/.copilot/session-store.db.
- OpenCode: the sessions whose folder is the run's folder, in each of its databases
  (~/.local/share/opencode/opencode*.db), with everything kept for them.

Rows are deleted with SQLite's secure_delete on, and the database's write-ahead log is emptied afterwards, so
the text doesn't stay behind in free space. A run in a folder of yours (an agent's own workdir) is left alone:
your own sessions are kept there too. SECURITY.md lists what each CLI still keeps.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Callable, Mapping
from urllib.parse import quote

from ixel_mat.presets import preset_for

logger = logging.getLogger("ixel_mat.agents.leftovers")

# How long to wait for one of your own CLIs that's using the same database or file at that moment
BUSY_SECONDS = 2.0
# Gemini CLI's own rule for the names of its project folders: never a path
GEMINI_SLUG = re.compile(r"[a-z0-9-]+")
# Copilot arguments that pick the session already: a run with one of them is left alone
COPILOT_SESSION_FLAGS = ("--session-id", "--resume", "-r", "--continue")
# OpenCode keeps a session's instructions as blobs named by their SHA-256
BLOB_NAME = re.compile(r"\b[0-9a-f]{64}\b")


@dataclass(frozen=True)
class Places:
    """Where each CLI keeps what it saves, for the environment it ran with."""
    gemini: tuple[Path, ...]     # Gemini CLI's ~/.gemini, and ~/.cache/.gemini
    copilot: Path                # Copilot's ~/.copilot
    opencode: tuple[Path, ...]   # OpenCode's databases


def places(env: Mapping[str, str]) -> Places:
    """Each CLI's folders, found the way the CLI finds them: from the home folder it was given (USERPROFILE on
    Windows, HOME elsewhere) and the variables that move them."""
    home = Path(env.get("USERPROFILE" if os.name == "nt" else "HOME") or Path.home())
    gemini_home = Path(env.get("GEMINI_CLI_HOME") or home)
    # With its own sandbox on, on a Mac, Gemini CLI runs again inside it and keeps its state in ~/.cache: it sets
    # SANDBOX for that copy, not in the environment Ixel gives it, so both are looked in
    gemini = (gemini_home / ".gemini", gemini_home / ".cache" / ".gemini")
    copilot = Path(env.get("COPILOT_HOME") or home / ".copilot")
    data = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "opencode"
    chosen = env.get("OPENCODE_DB")
    if chosen == ":memory:":
        databases: tuple[Path, ...] = ()
    elif chosen:
        databases = (data / chosen,)  # a full path replaces the folder
    else:  # opencode.db, or one for a test release (opencode-beta.db…)
        databases = tuple(sorted(data.glob("opencode*.db")))
    return Places(gemini, copilot, databases)


@dataclass
class Run:
    """One question to a CLI in Ixel's temp folder, and what's added to it and taken away after it."""
    program: str                 # the preset's id: "gemini_cli", "copilot" or "opencode"
    folder: str                  # the temp folder it runs in
    env: Mapping[str, str]       # the environment it runs with
    forms: frozenset[str]        # the folder as the CLI may write it: as given, and with links resolved
    args: list[str] = field(default_factory=list)  # arguments added for this run
    session_id: str = ""         # Copilot's, picked by Ixel
    log_dir: str = ""            # Copilot's log folder for this run

    def clean(self) -> None:
        """Take away what the run left, once the CLI has exited. Never fails: what can't be removed stays, and is
        logged (never its text)."""
        try:
            CLEANERS[self.program](self, places(self.env))
        except Exception as exc:  # noqa: BLE001 — tidying up must never cost you the answer
            logger.warning("Couldn't remove everything %s kept of a question: %s", self.program, exc)
        finally:
            self.drop()

    def drop(self) -> None:
        """Remove what Ixel made for the run (Copilot's log folder): all there is when the CLI never started."""
        if self.log_dir:
            _remove_folder(Path(self.log_dir))

    def same_folder(self, path: object) -> bool:
        return isinstance(path, str) and bool(path) and _normal(path) in self.forms


def _remove_folder(path: Path) -> None:
    """Remove a folder the run left, as much of it as can go. What can't (a file open in another program, on
    Windows) is said, by its folder's path alone, and stays."""
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        logger.warning("Couldn't remove all of %s", path)


def _normal(path: str) -> str:
    """A path as Node's path.resolve gives it, with Windows' case ignored (Gemini CLI lowercases it there)."""
    return os.path.normcase(os.path.abspath(path))


def prepare(command: str, args: list[str], folder: str | None, env: Mapping[str, str]) -> Run | None:
    """What to add to a run of command, and to take away after it; None when Ixel doesn't clean up after it:
    another program, or a folder that isn't Ixel's own temp folder."""
    program = preset_for(command).get("id", "")
    if not folder or program not in CLEANERS:
        return None
    forms = frozenset({_normal(folder), _normal(os.path.realpath(folder))})
    run = Run(program, folder, dict(env), forms)
    if program == "copilot":
        if not any(a in COPILOT_SESSION_FLAGS or a.startswith(("--session-id=", "--resume=")) for a in args):
            run.session_id = str(uuid.uuid4())
            run.args += ["--session-id", run.session_id]
        if not any(a == "--log-dir" or a.startswith("--log-dir=") for a in args):
            # Its log names the folders it ran in and its session: kept for this run only
            run.log_dir = tempfile.mkdtemp(prefix="ixel-copilot-log-")
            run.args += ["--log-dir", run.log_dir]
    return run


# ── Gemini CLI ────────────────────────────────────────────────────────────────

def _gemini_slug(name: str) -> str:
    """The folder name Gemini CLI gives a project, from its folder's name."""
    return re.sub(r"^-|-$", "", re.sub(r"-+", "-", re.sub(r"[^a-z0-9]", "-", name.lower()))) or "project"


def _clean_gemini(run: Run, where: Places) -> None:
    for root in where.gemini:
        if root.is_dir():
            _clean_gemini_in(run, root)


def _clean_gemini_in(run: Run, root: Path) -> None:
    registry = root / "projects.json"
    slugs = {_gemini_slug(os.path.basename(run.folder))}
    try:
        projects = json.loads(registry.read_text(encoding="utf-8")).get("projects")
    except (OSError, ValueError, AttributeError):
        projects = None
    if isinstance(projects, dict):
        slugs |= {slug for path, slug in projects.items() if run.same_folder(path) and isinstance(slug, str)}
    for slug in slugs:
        if not GEMINI_SLUG.fullmatch(slug):
            continue
        for base in (root / "tmp", root / "history"):
            # Gemini CLI marks each project folder with the folder it belongs to: one that isn't this run's stays
            try:
                owner = (base / slug / ".project_root").read_text(encoding="utf-8").strip()
            except (OSError, ValueError):
                continue
            if run.same_folder(owner):
                _remove_folder(base / slug)
    _forget_gemini_project(registry, run)


def _forget_gemini_project(registry: Path, run: Run) -> None:
    """Take the run's folder out of Gemini CLI's list of projects, under the lock Gemini CLI uses for it."""
    if not registry.is_file():
        return
    lock = registry.with_name(registry.name + ".lock")  # proper-lockfile's: a folder, made only if it isn't there
    deadline = time.monotonic() + BUSY_SECONDS
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                return  # a Gemini CLI of yours is using it: the line stays
            time.sleep(0.1)
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
        projects = data.get("projects") if isinstance(data, dict) else None
        if not isinstance(projects, dict):
            return
        kept = {path: slug for path, slug in projects.items() if not run.same_folder(path)}
        if len(kept) == len(projects):
            return
        data["projects"] = kept
        temp = registry.with_name(f"{registry.name}.{uuid.uuid4()}.tmp")
        try:
            temp.write_bytes(json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))
            os.replace(temp, registry)
        finally:
            temp.unlink(missing_ok=True)
    finally:
        lock.rmdir()


# ── Copilot ───────────────────────────────────────────────────────────────────

def _clean_copilot(run: Run, where: Places) -> None:
    if not run.session_id:
        return
    state = where.copilot / "session-state"
    _remove_folder(state / run.session_id)
    (state / ".session-operation-locks" / f"{run.session_id}.lock").unlink(missing_ok=True)
    database = where.copilot / "session-store.db"
    if database.is_file():
        _delete_rows(database, lambda db: _copilot_rows(db, run), _delete_copilot_rows, foreign_keys=False)


def _copilot_rows(db: sqlite3.Connection, run: Run) -> tuple[str, list[str]] | None:
    """The run's session and the tables with rows of it; None if there are none, or it isn't the run's."""
    tables = _tables(db)
    found = db.execute("SELECT cwd FROM sessions WHERE id = ?", (run.session_id,)).fetchone() \
        if {"id", "cwd"} <= tables.get("sessions", set()) else None
    if found and not run.same_folder(found[0]):
        return None
    holding = [table for table, columns in tables.items() if "session_id" in columns and db.execute(
        f'SELECT 1 FROM "{table}" WHERE session_id = ? LIMIT 1', (run.session_id,)).fetchone()]
    return (run.session_id, holding) if found or holding else None


def _delete_copilot_rows(db: sqlite3.Connection, found: tuple[str, list[str]]) -> None:
    session, holding = found
    for table in holding:
        db.execute(f'DELETE FROM "{table}" WHERE session_id = ?', (session,))
        if _is_full_text_index(db, table):
            # Its words stay in the index until it's rebuilt
            db.execute(f'INSERT INTO "{table}"("{table}") VALUES (\'optimize\')')
    db.execute("DELETE FROM sessions WHERE id = ?", (session,))


def _is_full_text_index(db: sqlite3.Connection, table: str) -> bool:
    found = db.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return bool(found and re.search(r"\bUSING\s+fts5\b", found[0] or "", re.IGNORECASE))


# ── OpenCode ──────────────────────────────────────────────────────────────────

SESSION_TABLES = ("session", "session_v2")  # OpenCode 1's and 2's


def _clean_opencode(run: Run, where: Places) -> None:
    for database in where.opencode:
        if database.is_file():
            _delete_rows(database, lambda db: _opencode_sessions(db, run),
                         lambda db, ids: _delete_opencode_rows(db, run, ids), foreign_keys=True)


def _session_tables(tables: dict[str, set[str]]) -> set[str]:
    return {table for table in SESSION_TABLES if {"id", "directory"} <= tables.get(table, set())}


def _opencode_sessions(db: sqlite3.Connection, run: Run) -> set[str] | None:
    """The sessions OpenCode ran in the run's folder, and the ones those started (a sub-agent's); None if none."""
    tables = _tables(db)
    sessions = _session_tables(tables)
    name = os.path.basename(run.folder)
    ids: set[str] = set()
    for table in sessions:
        ids |= {row[0] for row in db.execute(f'SELECT id, directory FROM "{table}" WHERE instr(directory, ?) > 0',
                                             (name,)) if run.same_folder(row[1])}
    started = set(ids)
    while started:
        keys = tuple(started)
        started = {row[0] for table in sessions if "parent_id" in tables[table] for row in db.execute(
            f'SELECT id FROM "{table}" WHERE parent_id IN ({_marks(keys)})', keys)} - ids
        ids |= started
    return ids or None


def _delete_opencode_rows(db: sqlite3.Connection, run: Run, ids: set[str]) -> None:
    tables = _tables(db)
    sessions = _session_tables(tables)
    name = os.path.basename(run.folder)
    keys = tuple(ids)
    instructions = [table for table, columns in tables.items()
                    if table.startswith("instruction_") and "session_id" in columns]
    blobs = {blob for table in instructions
             for row in db.execute(f'SELECT * FROM "{table}" WHERE session_id IN ({_marks(keys)})', keys)
             for blob in BLOB_NAME.findall(str(row))}
    for table, columns in tables.items():
        if table in sessions:
            continue
        for column in ("session_id", "aggregate_id"):  # aggregate_id: the session's events
            if column in columns:
                db.execute(f'DELETE FROM "{table}" WHERE "{column}" IN ({_marks(keys)})', keys)
    for table in sessions:
        db.execute(f'DELETE FROM "{table}" WHERE id IN ({_marks(keys)})', keys)
    # OpenCode 2 makes a project of each folder it's run in: the run's goes if nothing else is in it
    if {"id", "worktree"} <= tables.get("project", set()):
        for (project, worktree) in db.execute("SELECT id, worktree FROM project WHERE instr(worktree, ?) > 0",
                                              (name,)).fetchall():
            if not run.same_folder(worktree) or any(db.execute(
                    f'SELECT 1 FROM "{table}" WHERE project_id = ? LIMIT 1', (project,)).fetchone()
                    for table in sessions if "project_id" in tables[table]):
                continue
            for table, columns in tables.items():
                if "project_id" in columns and table not in sessions:
                    db.execute(f'DELETE FROM "{table}" WHERE project_id = ?', (project,))
            db.execute("DELETE FROM project WHERE id = ?", (project,))
    # The run's instructions (its folder, the date), unless another session uses them too
    if blobs and "hash" in tables.get("instruction_blob", set()):
        for blob in blobs:
            if not any(_mentions(db, table, tables[table], blob) for table in instructions):
                db.execute("DELETE FROM instruction_blob WHERE hash = ?", (blob,))


def _mentions(db: sqlite3.Connection, table: str, columns: set[str], text: str) -> bool:
    return any(db.execute(f'SELECT 1 FROM "{table}" WHERE instr(CAST("{column}" AS TEXT), ?) > 0 LIMIT 1',
                          (text,)).fetchone() for column in columns)


# ── SQLite ────────────────────────────────────────────────────────────────────

def _marks(values) -> str:
    return ",".join("?" * len(values))


def _sqlite_uri(path: PurePath) -> str:
    """A file: URI for SQLite to open an absolute path with. Not as_uri(): for a home folder on a network share
    (redirected or roaming Windows profiles) it puts the server's name where SQLite wants none, and SQLite refuses
    the URI."""
    where = path.as_posix()  # /home/you/..., C:/Users/you/..., or //server/share/Users/you/...
    if not where.startswith("/"):
        where = "/" + where  # file:///C:/... is how SQLite takes a drive letter
    # file:// with no server after it, then the path, which for a network share starts with //
    return "file://" + quote(where, safe="/:")


def _tables(db: sqlite3.Connection) -> dict[str, set[str]]:
    """Each table's columns. One this SQLite can't read (a full-text index without its module) is left out."""
    tables = {}
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        try:
            tables[name] = {row[1] for row in db.execute(f'PRAGMA table_info("{name}")')}
        except sqlite3.OperationalError:
            continue
    return tables


def _delete_rows(path: Path, find: Callable[[sqlite3.Connection], object],
                 delete: Callable[[sqlite3.Connection, object], None], foreign_keys: bool) -> None:
    """Look for the run's rows in the database at path with find, and if there are any, delete them in one
    transaction (all of them or none), with what's deleted overwritten. Then empty the write-ahead log, which
    still holds the text as it was written."""
    # mode=rw: never make a database that isn't there
    db = sqlite3.connect(f"{_sqlite_uri(path.absolute())}?mode=rw", uri=True, timeout=BUSY_SECONDS,
                         isolation_level=None)
    try:
        found = find(db)
        if not found:
            return
        db.execute("PRAGMA secure_delete = ON")
        # OpenCode's own setting, so a session's rows in tables Ixel doesn't know go with it
        db.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
        db.execute("BEGIN IMMEDIATE")
        try:
            delete(db, found)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        # While a CLI of yours is reading the database, the log can't be emptied: it is the next time it can be
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    finally:
        db.close()


CLEANERS: dict[str, Callable[[Run, Places], None]] = {
    "gemini_cli": _clean_gemini, "copilot": _clean_copilot, "opencode": _clean_opencode}
