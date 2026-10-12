"""
The agent's folder of files (ixel_mat/agent/store.py): the persona, user and memory files, skills, saved runs
and the per-workspace allowlist. Each test gives the store a folder of its own under tmp_path.
"""
import os
import re
import stat

import pytest

from ixel_mat.agent.store import FILE_CLIP_NOTE, SOUL_DEFAULT, AgentStore


def _store(tmp_path) -> AgentStore:
    return AgentStore(tmp_path / "agent")


# ── SOUL.md, USER.md, MEMORY.md ──────────────────────────────────────────────

def test_files_are_made_with_defaults_on_first_read(tmp_path):
    store = _store(tmp_path)
    for name in ("soul", "user", "memory"):
        assert store.file_text(name).strip()
    assert store.file_text("soul") == SOUL_DEFAULT


def test_set_then_read_round_trips(tmp_path):
    store = _store(tmp_path)
    store.set_file_text("user", "I work on /home/x projects.\n")
    assert store.file_text("user") == "I work on /home/x projects.\n"


def test_prompt_files_clip_a_long_memory(tmp_path):
    store = _store(tmp_path)
    store.set_file_text("memory", "a" * 40_000)
    memory = store.prompt_files()["memory"]
    assert len(memory) < 17_000
    assert FILE_CLIP_NOTE in memory


def test_remember_appends_a_dated_line_and_returns_it(tmp_path):
    store = _store(tmp_path)
    line = store.remember("Likes short answers\nand tables")
    assert re.fullmatch(r"- \d{4}-\d{2}-\d{2}: Likes short answers and tables", line)
    assert "\n" not in line
    assert line in store.file_text("memory")


# ── Skills ───────────────────────────────────────────────────────────────────

def test_skills_index_lists_only_folders_with_a_skill_file(tmp_path):
    store = _store(tmp_path)
    one = tmp_path / "agent" / "skills" / "one"
    one.mkdir(parents=True)
    (one / "SKILL.md").write_text(
        "---\nname: one\ndescription: How to do one thing\n---\n\nBody text here.\n", encoding="utf-8")
    (tmp_path / "agent" / "skills" / "junk").mkdir()
    assert store.skills() == [{"name": "one", "description": "How to do one thing"}]


def test_load_skill_returns_its_text(tmp_path):
    store = _store(tmp_path)
    one = tmp_path / "agent" / "skills" / "one"
    one.mkdir(parents=True)
    (one / "SKILL.md").write_text("---\nname: one\ndescription: d\n---\n\nBody text here.\n", encoding="utf-8")
    assert "Body text here." in store.load_skill("one")


@pytest.mark.parametrize("name", ["../x", "nope"])
def test_load_skill_refuses_unsafe_or_missing_names(tmp_path, name):
    with pytest.raises(KeyError):
        _store(tmp_path).load_skill(name)


# ── Sessions ─────────────────────────────────────────────────────────────────

def test_save_then_load_a_session(tmp_path):
    store = _store(tmp_path)
    store.save_session({"id": "r1", "goal": "tidy", "status": "done", "started": 100})
    assert store.load_session("r1")["goal"] == "tidy"


def test_sessions_are_listed_newest_first(tmp_path):
    store = _store(tmp_path)
    store.save_session({"id": "r1", "goal": "first", "status": "done", "started": 100})
    store.save_session({"id": "r2", "goal": "second", "status": "done", "started": 200})
    runs = store.list_sessions()
    assert [run["id"] for run in runs] == ["r2", "r1"]
    assert runs[0] == {"id": "r2", "goal": "second", "status": "done", "started": 200}


def test_session_id_with_path_parts_is_refused(tmp_path):
    with pytest.raises(ValueError):
        _store(tmp_path).save_session({"id": "../x", "goal": "g"})


# ── The allowlist ────────────────────────────────────────────────────────────

def test_allowlist_matches_argv_prefixes_in_one_workspace(tmp_path):
    store = _store(tmp_path)
    ws = tmp_path / "ws"
    other = tmp_path / "other"
    ws.mkdir()
    other.mkdir()
    store.allow(ws, ["pytest", "-q"])
    assert store.is_allowed(ws, ["pytest", "-q", "tests"]) is True
    assert store.is_allowed(ws, ["pytest"]) is False
    assert store.is_allowed(other, ["pytest", "-q", "tests"]) is False


def test_shells_and_interpreters_with_code_cannot_be_allowed(tmp_path):
    store = _store(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(ValueError):
        store.allow(ws, ["bash", "-c", "x"])
    with pytest.raises(ValueError):
        store.allow(ws, ["python", "-c", "x"])


@pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX only")
def test_agent_files_are_private(tmp_path):
    store = _store(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    store.file_text("soul")
    store.allow(ws, ["pytest", "-q"])
    for path in (tmp_path / "agent" / "SOUL.md", tmp_path / "agent" / "allow.json"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
