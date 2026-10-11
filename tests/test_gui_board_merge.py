"""The app's Board: merging a finished task into the checked-out branch, and what the tasks and the board cost."""
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from ixel_mat.gui import handoff_api
from ixel_mat.gui.server import GuiServer
from test_gui_server import JSON_AUTH, TOKEN, run_with_client

BOARD = {"exists": True, "revision": "r1", "counts": {"open": 1}, "done_rule": "", "tasks": [
    {"ref": "T-1", "id": 1, "title": "Fix the login page", "status": "open", "assignee": "codex"}]}
STATIC = Path(__file__).resolve().parent.parent / "ixel_mat" / "gui" / "static"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "shop"
    (root / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / ".handoff").mkdir()
    (root / ".handoff" / "board.db").write_bytes(b"SQLite format 3\x00")
    return root.resolve()


@pytest.fixture
def fake(tmp_path):
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


def post(gui, body):
    async def scenario(client):
        resp = await client.post("/api/board/action", headers=JSON_AUTH, data=json.dumps(body))
        return resp.status, await resp.json()
    return run_with_client(gui, scenario)


def test_merge_is_a_board_write_op():
    assert "merge" in handoff_api.WRITE_OPS


def test_merge_is_forwarded_with_its_args(project, fake):
    fake.answer("merge", {"data": {"merged": True}})
    args = {"task": "T-3", "shown": "abc123"}
    assert post(gui_with(fake.command), {"project": str(project), "op": "merge", "args": args}) == (
        200, {"merged": True})
    call = fake.calls()[-1]["request"]
    assert call["op"] == "merge" and call["args"] == args


def test_a_merge_refusal_keeps_handoffs_words(project, fake):
    message = "You have uncommitted changes in src/a.py"
    fake.answer("merge", {"error": {"code": "busy", "message": message}})
    got = post(gui_with(fake.command), {"project": str(project), "op": "merge", "args": {"task": "T-3"}})
    assert got == (409, {"error": message, "code": "busy"})


def test_board_js_shows_the_cost_chip_and_the_header_total():
    board = (STATIC / "board.js").read_text(encoding="utf-8")
    for text in ("cost-chip", "Spent: ", ".usage", "models",
                 "On a subscription this counts toward your plan's limits, not your bill"):
        assert text in board, text


def test_style_css_has_the_cost_chip():
    assert ".cost-chip" in (STATIC / "style.css").read_text(encoding="utf-8")
