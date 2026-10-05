"""The app's Board: Handoff's board, read and changed through `handoff api` (one JSON request on stdin)."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from ixel_mat.config import secrets
from ixel_mat.gui import handoff_api
from ixel_mat.gui.server import GuiServer
from test_gui_server import AUTH, JSON_AUTH, TOKEN, run_with_client

BOARD = {"exists": True, "revision": "r1", "counts": {"open": 1}, "done_rule": "", "tasks": [
    {"ref": "T-1", "id": 1, "title": "Fix the login\x1b[31m page", "status": "open", "assignee": "codex"}]}


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "shop"
    (root / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / ".handoff").mkdir()
    (root / ".handoff" / "board.db").write_bytes(b"SQLite format 3\x00")
    return root.resolve()


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A stand-in for `python -m handoff`: logs each request and answers from a table of replies."""
    log = tmp_path / "api.jsonl"
    replies = tmp_path / "replies.json"
    script = tmp_path / "fake_handoff_api.py"
    script.write_text(textwrap.dedent(f'''
        import json, os, sys
        raw = sys.stdin.buffer.read().decode("utf-8")
        request = json.loads(raw)
        with open({str(log)!r}, "a", encoding="utf-8") as f:
            f.write(json.dumps({{"argv": sys.argv[1:], "request": request, "cwd": os.getcwd(),
                                "key": os.environ.get("OPENAI_API_KEY"),
                                "depth": os.environ.get("IXEL_PANEL_DEPTH")}}) + "\\n")
        table = json.load(open({str(replies)!r}, encoding="utf-8"))
        reply = table.get(request["op"], {{"data": {{}}}})
        if "stderr" in reply:
            sys.stderr.write(reply["stderr"])
            sys.exit(reply.get("exit", 1))
        if "raw" in reply:
            sys.stdout.write(reply["raw"])
            sys.exit(0)
        if "error" in reply:
            print(json.dumps({{"schema": 1, "ok": False, "op": request["op"], "error": reply["error"]}}))
            sys.exit(1)
        print(json.dumps({{"schema": reply.get("schema", 1), "ok": True, "op": request["op"],
                          "data": reply["data"]}}, ensure_ascii=True))
    '''), encoding="utf-8")

    class Fake:
        command = [sys.executable, str(script)]

        def __init__(self):
            self.table = {"hello": {"data": {"handoff_version": "0.9.0", "schema": 1, "ops": ["board"]}},
                          "board": {"data": BOARD}}
            self.save()

        def answer(self, op, reply):
            self.table[op] = reply
            self.save()

        def save(self):
            replies.write_text(json.dumps(self.table), encoding="utf-8")

        def calls(self):
            return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    return Fake()


def gui_with(command):
    return GuiServer(token=TOKEN, settings_loader=lambda: None, handoff_command=lambda: command)


def get(gui, *paths):
    async def scenario(client):
        out = []
        for path in paths:
            resp = await client.get(path, headers=AUTH)
            out.append((resp.status, await resp.json()))
        return out
    return run_with_client(gui, scenario)


def post(gui, body):
    async def scenario(client):
        resp = await client.post("/api/board/action", headers=JSON_AUTH, data=json.dumps(body))
        return resp.status, await resp.json()
    return run_with_client(gui, scenario)


def q(path):
    from urllib.parse import quote
    return quote(str(path), safe="")


def test_the_board_comes_from_handoff_api_on_a_fixed_command_line(project, fake):
    [(status, board)] = get(gui_with(fake.command), f"/api/board?project={q(project / 'src')}")
    assert status == 200 and board["tasks"][0]["title"] == "Fix the login page"  # control codes cleaned
    [call] = fake.calls()
    assert call["argv"] == ["api"]  # everything else comes on stdin
    assert call["request"] == {"schema": 1, "op": "board", "args": {}, "project": str(project)}
    assert os.path.samefile(call["cwd"], tempfile.gettempdir())


def test_an_unchanged_board_isnt_read_again(project, fake):
    gui = gui_with(fake.command)
    url = f"/api/board?project={q(project)}"
    first, second = get(gui, url, f"{url}&since=r1")
    assert first[1]["revision"] == "r1" and second == (200, {"unchanged": True, "revision": "r1"})
    assert len(fake.calls()) == 1
    with open(project / ".handoff" / "board.db-wal", "ab") as wal:  # a write lands in the WAL
        wal.write(b"x" * 64)
    [(status, again)] = get(gui, f"{url}&since=r1")
    assert status == 200 and again["revision"] == "r1" and len(fake.calls()) == 2
    [(status, wrong)] = get(gui, f"{url}&since=r0")  # the page has another revision: read it
    assert "tasks" in wrong and len(fake.calls()) == 3


def test_a_project_with_no_board_isnt_asked_again_until_one_appears(project, fake):
    (project / ".handoff" / "board.db").unlink()
    fake.answer("board", {"data": {"exists": False, "project": str(project), "revision": ""}})
    gui = gui_with(fake.command)
    url = f"/api/board?project={q(project)}"
    [(_, first)] = get(gui, url)
    assert first["exists"] is False and first["revision"] == "none"
    [(_, second)] = get(gui, f"{url}&since=none")
    assert second == {"unchanged": True, "revision": "none"} and len(fake.calls()) == 1
    (project / ".handoff" / "board.db").write_bytes(b"SQLite format 3\x00")  # someone started one
    fake.answer("board", {"data": BOARD})
    [(_, third)] = get(gui, f"{url}&since=none")
    assert third["revision"] == "r1" and len(fake.calls()) == 2


def test_after_a_change_the_board_is_read_again(project, fake):
    gui = gui_with(fake.command)

    async def scenario(client):
        url = f"/api/board?project={q(project)}"
        await client.get(url, headers=AUTH)
        changed = await client.post("/api/board/action", headers=JSON_AUTH, data=json.dumps(
            {"project": str(project), "op": "note", "args": {"task": "T-1", "text": "hi"}}))
        again = await client.get(f"{url}&since=r1", headers=AUTH)
        return changed.status, await again.json()

    status, again = run_with_client(gui, scenario)
    assert status == 200 and "tasks" in again
    assert [c["request"]["op"] for c in fake.calls()] == ["board", "note", "board"]


def test_ixels_keys_never_reach_handoff(project, fake, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-ixels-env-file")
    monkeypatch.setattr(secrets, "_INJECTED", {"OPENAI_API_KEY"})
    get(gui_with(fake.command), f"/api/board?project={q(project)}")
    [call] = fake.calls()
    assert call["key"] is None  # Claude and Codex would bill it instead of their subscriptions
    assert call["depth"] in (None, "0")  # not a panel member: agents it starts can still use ixel


def test_changes_are_only_the_boards_ops(project, fake):
    gui = gui_with(fake.command)
    assert post(gui, {"project": str(project), "op": "rm", "args": {}})[0] == 400
    assert post(gui, {"project": str(project), "op": "board", "args": {}})[0] == 400  # reading isn't a change
    assert post(gui, {"project": str(project), "op": "add", "args": []})[0] == 400
    assert fake.calls() == []
    fake.answer("add", {"data": {"task": {"ref": "T-2"}}})
    status, data = post(gui, {"project": str(project), "op": "add", "args": {"title": "Ñandú \"50%\" & <b>"}})
    assert status == 200 and data == {"task": {"ref": "T-2"}}
    assert fake.calls()[-1]["request"]["args"] == {"title": "Ñandú \"50%\" & <b>"}


@pytest.mark.parametrize("code, status", [("invalid", 400), ("not_found", 404), ("forbidden", 403), ("busy", 409),
                                          ("internal", 502)])
def test_handoffs_refusals_keep_their_code(project, fake, code, status):
    fake.answer("status", {"error": {"code": code, "message": "No: T-1 is done"}})
    got = post(gui_with(fake.command), {"project": str(project), "op": "status", "args": {"task": "T-1", "to": "open"}})
    assert got == (status, {"error": "No: T-1 is done", "code": code})


def test_a_path_pasted_with_quotes_works(project, fake):
    # Windows' "Copy as path" puts quotes round it
    [(status, _)] = get(gui_with(fake.command), f"/api/board?project={q(chr(34) + str(project) + chr(34) + ' ')}")
    assert status == 200 and fake.calls()[-1]["request"]["project"] == str(project)


def test_a_project_must_be_a_folder_in_git(tmp_path, fake):
    outside = tmp_path / "loose"
    outside.mkdir()
    gui = gui_with(fake.command)
    results = get(gui, "/api/board?project=relative%2Fpath", f"/api/board?project={q(outside)}",
                  f"/api/board?project={q(tmp_path / 'nope')}", "/api/board")
    assert [status for status, _ in results] == [400] * 4
    assert {body["code"] for _, body in results} == {"no_project"}
    assert fake.calls() == []


def repo(folder, board=False):
    (folder / ".git").mkdir(parents=True)
    if board:
        (folder / ".handoff").mkdir()
    return folder


def test_the_board_offers_the_projects_on_this_computer_with_boards_first(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo(home / "zebra")
    repo(home / "code" / "shop", board=True)
    repo(home / "code" / "blog")
    (home / "code" / "notes").mkdir()                       # a folder that isn't a repository
    repo(home / "code" / "shop" / "vendor" / "lib")         # inside a repository: part of it, not a project
    repo(home / "code" / "deep" / "er")                     # too far down to look for
    repo(home / ".hidden" / "x")
    repo(home / "Library" / "thing")
    repo(home / "source" / "repos" / "game")                # Visual Studio's place
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    [(status, data)] = get(gui_with(None), "/api/board/projects")  # (no Handoff needed to list them)
    assert status == 200
    assert [(p["name"], p["board"]) for p in data["projects"]] == [
        ("shop", True), ("blog", False), ("game", False), ("zebra", False)]
    assert data["projects"][0]["path"] == str((home / "code" / "shop").resolve())


def test_on_a_mac_the_folders_it_asks_about_first_arent_looked_in(tmp_path):
    repo(tmp_path / "Documents" / "GitHub" / "site")
    repo(tmp_path / "Desktop" / "app")
    repo(tmp_path / "code" / "shop")
    names = lambda system: [p["name"] for p in handoff_api.find_projects(tmp_path, system=system)]  # noqa: E731
    assert names("darwin") == ["shop"]
    assert names("win32") == ["app", "shop", "site"]


def test_a_home_full_of_folders_is_looked_through_only_so_far(tmp_path, monkeypatch):
    monkeypatch.setattr(handoff_api, "MAX_FOLDERS_LOOKED_AT", 5)
    for n in range(8):
        repo(tmp_path / f"p{n}")
    assert [p["name"] for p in handoff_api.find_projects(tmp_path)] == ["p0", "p1", "p2", "p3", "p4"]


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs a folder this user can't open")
def test_a_folder_it_cant_open_doesnt_hide_the_others(tmp_path):
    repo(tmp_path / "code" / "shop")
    locked = tmp_path / "docker" / "locked"
    locked.mkdir(parents=True)
    locked.chmod(0)
    try:
        assert [p["name"] for p in handoff_api.find_projects(tmp_path)] == ["shop"]
    finally:
        locked.chmod(0o755)


def test_one_huge_folder_doesnt_use_up_the_looking(tmp_path, monkeypatch):
    monkeypatch.setattr(handoff_api, "MAX_ENTRIES_READ", 10)
    monkeypatch.setattr(handoff_api, "MAX_FOLDERS_LOOKED_AT", 20)
    for n in range(30):
        (tmp_path / "aaa" / f"photos-{n:02}").mkdir(parents=True)
    repo(tmp_path / "zzz" / "app")
    assert [p["name"] for p in handoff_api.find_projects(tmp_path)] == ["app"]


def test_a_slow_disk_answers_with_what_it_found_in_time(tmp_path, monkeypatch):
    for name in ("a", "b", "c"):
        repo(tmp_path / name)
    monkeypatch.setattr(handoff_api, "FIND_SECONDS", 3.5)
    ticks = iter(range(100))  # each look takes a second: at ~/source/repos, at the home folder, at a, at b…
    found = handoff_api.find_projects(tmp_path, system="darwin", clock=lambda: next(ticks))
    assert [p["name"] for p in found] == ["a"]


def test_folders_with_nothing_in_them_arent_read_after_times_up(tmp_path, monkeypatch):
    for name in ("e0", "e1", "e2"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(handoff_api, "FIND_SECONDS", 5.5)  # time's up after looking at e0, e1 and e2
    read, scandir = [], os.scandir
    monkeypatch.setattr(handoff_api.os, "scandir", lambda path: read.append(Path(path).name) or scandir(path))
    ticks = iter(range(100))
    assert handoff_api.find_projects(tmp_path, system="darwin", clock=lambda: next(ticks)) == []
    assert read == ["repos", tmp_path.name]  # not e0, e1 or e2


def test_a_name_that_could_pass_for_another_isnt_offered(tmp_path):
    repo(tmp_path / "shop")
    repo(tmp_path / "pohs\u202eshop")  # shows as "shop" + "pohs" reversed
    assert [p["name"] for p in handoff_api.find_projects(tmp_path)] == ["shop"]


def test_without_handoff_the_page_says_how_to_add_it(project):
    gui = gui_with(None)
    (hs, hello), (bs, board) = get(gui, "/api/board/hello", f"/api/board?project={q(project)}")
    assert hs == 200 and hello["ok"] is False and hello["problem"] == "not_installed"
    assert "handoff/install" in hello["install"]["command"]
    assert (bs, board["code"]) == (404, "not_installed")


def test_a_handoff_too_old_for_the_board_says_update(project, fake):
    fake.answer("hello", {"stderr": "  Unknown command: api. Did you mean add?\n", "exit": 2})
    [(status, hello)] = get(gui_with(fake.command), "/api/board/hello")
    assert status == 200 and hello["ok"] is False and hello["problem"] == "outdated"
    assert hello["update"] == "handoff update"
    fake.answer("hello", {"data": {}, "schema": 2})  # newer than this Ixel: also not one it can use
    [(status, hello)] = get(gui_with(fake.command), "/api/board/hello")
    assert hello["problem"] == "outdated"


def test_a_reply_that_isnt_json_is_an_error_not_a_crash(project, fake):
    fake.answer("board", {"raw": "Traceback (most recent call last):\n"})
    [(status, body)] = get(gui_with(fake.command), f"/api/board?project={q(project)}")
    assert status == 502 and body["code"] == "internal"


# ── Files a run left ──────────────────────────────────────────────────────────

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
def outputs(project):
    folder = project / ".handoff" / "outputs" / "T-1"
    folder.mkdir(parents=True)
    (folder / "picture-1.png").write_bytes(PNG)
    (folder / "answer.md").write_text("# Ideas\n\n- one", encoding="utf-8")
    (folder / "page.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    (folder / "fake.png").write_text("<svg onload=alert(1)>", encoding="utf-8")
    return folder


def output(gui, project, task, name):
    async def scenario(client):
        resp = await client.get(f"/api/board/output?project={q(project)}&task={q(task)}&name={q(name)}", headers=AUTH)
        return resp.status, dict(resp.headers), await resp.read()
    return run_with_client(gui, scenario)


def test_pictures_and_answers_a_run_left_are_served_as_what_they_are(project, outputs, fake):
    gui = gui_with(fake.command)
    status, headers, body = output(gui, project, "T-1", "picture-1.png")
    assert status == 200 and body == PNG and headers["Content-Type"] == "image/png"
    assert headers["X-Content-Type-Options"] == "nosniff" and headers["Content-Disposition"] == "attachment"
    status, headers, body = output(gui, project, "T-1", "answer.md")
    assert status == 200 and headers["Content-Type"].startswith("text/plain") and body.startswith(b"# Ideas")
    assert output(gui, project, "T-1", "page.html")[0] == 403  # never served as a page
    assert output(gui, project, "T-1", "fake.png")[0] == 403   # a name isn't a picture: its bytes are
    assert fake.calls() == []  # read straight from the folder


@pytest.mark.parametrize("task, name", [("T-1", "../../board.db"), ("T-1/..", "board.db"), ("../T-1", "answer.md"),
                                        ("T-0", "answer.md"), ("T-1", ".."), ("T-1", "a b.md"), ("T-1", "")])
def test_nothing_outside_a_tasks_outputs_can_be_asked_for(project, outputs, fake, task, name):
    assert output(gui_with(fake.command), project, task, name)[0] == 400


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="links")
def test_a_link_among_the_outputs_isnt_followed(project, outputs, tmp_path, fake):
    secret = tmp_path / "secret.md"
    secret.write_text("the ssh key", encoding="utf-8")
    try:
        (outputs / "link.md").symlink_to(secret)
        (project / ".handoff" / "outputs" / "T-2").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("can't make links here")
    gui = gui_with(fake.command)
    assert output(gui, project, "T-1", "link.md")[0] == 403
    assert output(gui, project, "T-2", "secret.md")[0] == 403


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="links")
def test_a_folder_swapped_for_a_link_after_the_check_still_isnt_followed(project, outputs, tmp_path, monkeypatch):
    # what a run racing the read could do: the path checks out, then T-1 becomes a link before the open
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "answer.md").write_text("from outside", encoding="utf-8")
    real = outputs.with_name("T-1-real")
    outputs.rename(real)
    try:
        outputs.symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("can't make links here")
    monkeypatch.setattr(handoff_api, "_no_links", lambda path, stop: True)
    with pytest.raises(handoff_api.HandoffApiError) as caught:
        handoff_api.read_output(project, "T-1", "answer.md")
    assert caught.value.code == "forbidden"


def test_a_second_name_for_a_file_elsewhere_isnt_served(project, outputs, tmp_path, fake):
    keys = tmp_path / ".env"
    keys.write_text("OPENAI_API_KEY=sk-secret", encoding="utf-8")
    try:
        os.link(keys, outputs / "notes.txt")
    except OSError:
        pytest.skip("can't make hard links here")
    status, _, body = output(gui_with(fake.command), project, "T-1", "notes.txt")
    assert status == 403 and b"sk-secret" not in body


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="pipes")
def test_a_pipe_among_the_outputs_is_refused_without_waiting(project, outputs, fake):
    os.mkfifo(outputs / "answer.txt")
    assert output(gui_with(fake.command), project, "T-1", "answer.txt")[0] == 403  # (it would hang if opened plainly)


# ── Finding Handoff ───────────────────────────────────────────────────────────

def test_handoff_runs_as_its_installs_python(tmp_path, monkeypatch):
    root = tmp_path / "handoff-install"
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(handoff_api, "find_on_path", lambda name: None)
    assert handoff_api.handoff_command({"HANDOFF_INSTALL_ROOT": str(root)}, tmp_path) == [
        str(python), "-I", "-m", "handoff"]
    assert handoff_api.handoff_command({}, tmp_path / "empty") is None


def test_the_python_behind_a_launcher_is_found(tmp_path):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("", encoding="utf-8")
    sh = tmp_path / "handoff"
    sh.write_text(f'#!/bin/sh\nexec "{venv}/bin/handoff" "$@"\n', encoding="utf-8")
    assert handoff_api.python_from_launcher(sh) == venv / "bin" / "python"
    cmd = tmp_path / "handoff.cmd"
    exe = tmp_path / "win" / "Handoff" / "python.exe"  # (beside the file called handoff, on a disk that ignores case)
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    cmd.write_text(f'@echo off\r\n"{exe}" -I -m handoff %*\r\n', encoding="utf-8")
    assert handoff_api.python_from_launcher(cmd) == exe
    script = tmp_path / "pip-script"
    script.write_text(f"#!{venv}/bin/python\nfrom handoff.cli import main\n", encoding="utf-8")
    assert handoff_api.python_from_launcher(script) == venv / "bin" / "python"
    other = tmp_path / "other"
    other.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    assert handoff_api.python_from_launcher(other) is None


# ── With the real Handoff (set IXEL_TEST_HANDOFF_PYTHON to a Python that has it) ─

REAL = os.environ.get("IXEL_TEST_HANDOFF_PYTHON", "")


@pytest.mark.skipif(not REAL, reason="IXEL_TEST_HANDOFF_PYTHON isn't set")
def test_with_the_real_handoff(tmp_path):
    root = tmp_path / "real"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    root = root.resolve()
    gui = gui_with([REAL, "-I", "-m", "handoff"])

    async def scenario(client):
        async def get_json(path):
            resp = await client.get(path, headers=AUTH)
            return resp.status, await resp.json()

        async def act(op, **args):
            resp = await client.post("/api/board/action", headers=JSON_AUTH,
                                     data=json.dumps({"project": str(root), "op": op, "args": args}))
            return resp.status, await resp.json()

        url = f"/api/board?project={q(root)}"
        out = {"hello": await get_json("/api/board/hello"), "empty": await get_json(url)}
        out["init"] = await act("init")
        out["add"] = await act("add", title="Ñandú <b>café</b>", body="línea", acceptance=["works"], assignee="grok")
        out["board"] = await get_json(url)
        out["same"] = await get_json(f"{url}&since={q(out['board'][1]['revision'])}")
        out["edit"] = await act("approve", task="T-1", agent="grok", kind="edit")
        seen = (await get_json(f"/api/board/task?project={q(root)}&task=T-1"))[1]["task"]["content"]
        out["note"] = await act("note", task="T-1", text="Only on Safari")
        out["task"] = await get_json(f"/api/board/task?project={q(root)}&task=T-1")
        # An approval covers the task as the panel showed it: a note since then means look again
        out["stale"] = await act("approve", task="T-1", agent="grok", kind="answer", shown=seen)
        now = out["task"][1]["task"]["content"]
        out["fresh"] = await act("approve", task="T-1", agent="grok", kind="answer", shown=now)
        return out

    out = run_with_client(gui, scenario)
    assert out["hello"][1]["ok"] is True
    assert out["empty"] == (200, {"exists": False, "revision": "none", "project": str(root), "counts": {}, "tasks": []})
    assert out["init"][0] == 200 and out["add"][1]["task"]["ref"] == "T-1"
    assert out["board"][1]["tasks"][0]["title"] == "Ñandú <b>café</b>"
    assert out["same"][1] == {"unchanged": True, "revision": out["board"][1]["revision"]}
    assert out["edit"][0] == 400 and out["edit"][1]["code"] == "invalid"  # grok can't change files
    task = out["task"][1]
    assert task["task"]["body"] == "línea" and task["events"][-1]["text"] == "Only on Safari"
    assert task["events"][0]["summary"] == "created it, assigned to grok" and task["events"][0]["actor"] == "human"
    assert any(a["op"] == "approve" for a in task["actions"])
    assert out["stale"][0] == 409 and out["stale"][1]["code"] == "changed"
    assert out["fresh"][0] == 200 and out["fresh"][1]["task"]["run"]["state"] == "approved"
