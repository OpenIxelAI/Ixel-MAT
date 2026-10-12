"""The Agent page's routes: live runs, approvals, saved sessions and the plain files it edits."""
import asyncio
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from ixel_mat.agent import runner
from ixel_mat.agent.sandbox import CommandResult
from ixel_mat.agent.store import AgentStore
from ixel_mat.agents.base import AgentConfig
from ixel_mat.config import secrets
from ixel_mat.gui import agent_api
from ixel_mat.gui.server import GuiServer
from ixel_mat.runtime import Settings

TOKEN = "obviously-fake-agent-session-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
RUN_KEYS = {"id", "status", "goal", "model", "workspace", "started", "answer", "error", "steps",
            "pending", "usage", "uploads", "computer", "follows"}
STEP_KEYS = {"n", "thought", "action", "args", "ok", "result", "usage", "ms", "tokens"}
ROW_KEYS = {"id", "goal", "status", "started", "model", "workspace", "computer", "follows"}


def action(name, **args):
    return json.dumps({"thought": "A fake model's short reason.", "action": name, "args": args})


class ScriptedAsk:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    async def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0), {"cost_usd": 0.0}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = home / "shop"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(secrets, "_ENV_DIR", home / ".config" / "ixel-mat")
    # Commands here are fake: a card shouldn't depend on this computer's sandbox
    monkeypatch.setattr(runner, "sandbox_kind", lambda: "none")
    store = AgentStore(home / ".config" / "ixel-mat" / "agent")
    configs = {
        "fake-local": AgentConfig(name="fake-local", label="Fake local", type="http",
                                  url="http://127.0.0.1:9999", model="fake-local-model"),
        "fake-cloud": AgentConfig(name="fake-cloud", label="Fake cloud", type="http",
                                  url="https://api.example.invalid", token="obviously-fake-agent-key",
                                  model="fake-cloud-model"),
        "fake-missing": AgentConfig(name="fake-missing", label="Fake missing key", type="http",
                                    url="https://api.example.invalid", model="fake-missing-model"),
    }
    return home, workspace, store, Settings({}, configs)


def gui_for(environment, ask=None):
    _, _, store, settings = environment
    return GuiServer(token=TOKEN, settings_loader=lambda: settings, agent_store=store, agent_ask=ask)


def run_with_client(gui, scenario):
    async def go():
        server = TestServer(gui.app(), host="127.0.0.1")
        async with TestClient(server) as client:
            gui.port = server.port
            return await scenario(client)
    return asyncio.run(go())


async def start(client, workspace, **extra):
    response = await client.post("/api/agent/start", headers=AUTH,
                                 json={"model": "fake-local", "workspace": str(workspace),
                                       "goal": "Look around the shop.", **extra})
    assert response.status == 200, await response.text()
    body = await response.json()
    assert set(body) == {"id"}
    return body["id"]


async def wait_status(client, run_id, wanted):
    for _ in range(1000):
        response = await client.get("/api/agent/run", headers=AUTH, params={"id": run_id})
        assert response.status == 200, await response.text()
        data = await response.json()
        if data["status"] == wanted:
            return data
        assert data["status"] not in ("done", "stopped", "failed"), data
        await asyncio.sleep(0.002)
    raise AssertionError(f"The run never became {wanted}.")


def test_models_start_run_runs_and_files_routes(environment):
    _, workspace, store, _ = environment
    (workspace / "note.txt").write_bytes(b"A fake project note.\n")  # write_text would end it with \r\n on Windows
    skill = store.root / "skills" / "fake-style" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: fake-style\ndescription: Short fake writing rules.\n---\nText.\n",
                     encoding="utf-8")
    ask = ScriptedAsk(action("read_file", path="note.txt"), action("finish", answer="I read the note."))
    gui = gui_for(environment, ask)

    async def scenario(client):
        models = await client.get("/api/agent/models", headers=AUTH)
        assert models.status == 200
        assert await models.json() == {"models": [
            {"name": "fake-local", "label": "Fake local", "ready": True},
            {"name": "fake-cloud", "label": "Fake cloud", "ready": True},
            {"name": "fake-missing", "label": "Fake missing key", "ready": False},
        ]}
        run_id = await start(client, workspace, max_steps=3)
        assert run_id in gui.agent_runs
        data = await wait_status(client, run_id, "done")
        assert set(data) == RUN_KEYS and all(set(step) == STEP_KEYS for step in data["steps"])
        assert data["model"] == "fake-local" and data["workspace"] == str(workspace.resolve())
        assert data["answer"] == "I read the note." and data["pending"] is None
        assert data["steps"][0]["result"] == "A fake project note.\n"
        listed = await client.get("/api/agent/runs", headers=AUTH)
        assert listed.status == 200
        [row] = (await listed.json())["runs"]
        assert set(row) == ROW_KEYS and row["id"] == run_id
        assert row["model"] == data["model"] and row["workspace"] == data["workspace"]
        files = await client.get("/api/agent/files", headers=AUTH)
        assert files.status == 200
        before = await files.json()
        assert set(before) == {"soul", "user", "memory", "skills"}
        assert before["skills"] == [{"name": "fake-style", "description": "Short fake writing rules."}]
        for name in ("soul", "user", "memory"):
            text = f"A fake {name} note for /home/x.\n"
            changed = await client.put("/api/agent/files", headers=AUTH, json={"name": name, "text": text})
            assert changed.status == 200 and (await changed.json())[name] == text
            assert store.file_text(name) == text
        return run_id, data

    run_id, data = run_with_client(gui, scenario)
    assert store.load_session(run_id)["status"] == "done"
    assert store.load_session(run_id)["steps"] == data["steps"]


def test_decide_approves_a_write_and_resumes_the_run(environment):
    _, workspace, store, _ = environment
    ask = ScriptedAsk(action("write_file", path="answer.txt", content="A fake answer.\n"),
                      action("finish", answer="I wrote the answer."))
    gui = gui_for(environment, ask)

    async def scenario(client):
        run_id = await start(client, workspace)
        waiting = await wait_status(client, run_id, "waiting")
        assert waiting["pending"]["action"] == "write_file"
        assert "+A fake answer." in waiting["pending"]["diff"]
        assert not (workspace / "answer.txt").exists()
        assert store.load_session(run_id)["status"] == "waiting"
        approved = await client.post("/api/agent/decide", headers=AUTH,
                                     json={"id": run_id, "approve": True, "always": False})
        assert approved.status == 200
        data = await approved.json()
        assert set(data) == RUN_KEYS and data["status"] == "running" and data["pending"] is None
        done = await wait_status(client, run_id, "done")
        assert done["steps"][0]["ok"] is True
        assert (workspace / "answer.txt").read_text(encoding="utf-8") == "A fake answer.\n"

    run_with_client(gui, scenario)


def test_decide_denies_an_action_and_the_model_hears_about_it(environment):
    _, workspace, _, _ = environment
    ask = ScriptedAsk(action("write_file", path="answer.txt", content="A fake answer."),
                      action("finish", answer="I left the file alone."))
    gui = gui_for(environment, ask)

    async def scenario(client):
        run_id = await start(client, workspace)
        await wait_status(client, run_id, "waiting")
        denied = await client.post("/api/agent/decide", headers=AUTH,
                                   json={"id": run_id, "approve": False, "always": False})
        assert denied.status == 200
        done = await wait_status(client, run_id, "done")
        assert done["steps"][0]["ok"] is False and "denied" in done["steps"][0]["result"]
        assert not (workspace / "answer.txt").exists()
        assert "denied" in ask.prompts[1]

    run_with_client(gui, scenario)


def test_always_allow_adds_the_exact_command_and_shells_are_refused(environment, monkeypatch):
    _, workspace, store, _ = environment
    calls = []

    def command(argv, folder, **kwargs):
        calls.append((argv, folder))
        return CommandResult(argv, 0, "A fake command's output.", False, False)

    monkeypatch.setattr(runner, "run_command", command)
    ask = ScriptedAsk(action("run_command", argv=["fake-check", "--quick"]),
                      action("run_command", argv=["fake-check", "--quick", "src"]),
                      action("run_command", argv=["bash", "-c", "a fake command"]),
                      action("finish", answer="Checked."))
    gui = gui_for(environment, ask)

    async def scenario(client):
        run_id = await start(client, workspace)
        await wait_status(client, run_id, "waiting")
        approved = await client.post("/api/agent/decide", headers=AUTH,
                                     json={"id": run_id, "approve": True, "always": True})
        assert approved.status == 200
        waiting = await wait_status(client, run_id, "waiting")
        assert waiting["pending"]["argv"] == ["bash", "-c", "a fake command"]
        assert len(calls) == 2
        assert store.allowed(workspace) == [["fake-check", "--quick"]]
        refused = await client.post("/api/agent/decide", headers=AUTH,
                                    json={"id": run_id, "approve": True, "always": True})
        assert refused.status == 400 and "error" in await refused.json()
        denied = await client.post("/api/agent/decide", headers=AUTH, json={"id": run_id, "approve": False})
        assert denied.status == 200
        await wait_status(client, run_id, "done")

    run_with_client(gui, scenario)


def test_stop_a_waiting_run_clears_pending_and_saves_it(environment):
    _, workspace, store, _ = environment
    gui = gui_for(environment, ScriptedAsk(action("write_file", path="answer.txt", content="Fake.")))

    async def scenario(client):
        run_id = await start(client, workspace)
        await wait_status(client, run_id, "waiting")
        stopped = await client.post("/api/agent/stop", headers=AUTH, json={"id": run_id})
        assert stopped.status == 200
        data = await stopped.json()
        assert set(data) == RUN_KEYS and data["status"] == "stopped" and data["pending"] is None
        assert not (workspace / "answer.txt").exists()
        assert store.load_session(run_id)["status"] == "stopped"
        after = await client.post("/api/agent/decide", headers=AUTH, json={"id": run_id, "approve": True})
        assert after.status == 409 and "error" in await after.json()

    run_with_client(gui, scenario)


def test_saved_sessions_are_available_after_a_restart_and_results_are_clipped(environment):
    _, _, store, _ = environment
    for n in range(55):
        store.save_session({"id": f"archived-{n}", "goal": f"Fake goal {n}", "model": "fake-local",
                            "workspace": "/home/x/shop", "status": "done", "started": n,
                            "answer": "A fake saved answer.", "steps": [
                                {"n": 1, "thought": "Fake.", "action": "read_file", "args": {},
                                 "ok": True, "result": "é" * 5000, "usage": {}}],
                            "approvals": [], "max_steps": 25})
    gui = gui_for(environment)

    async def scenario(client):
        listed = await client.get("/api/agent/runs", headers=AUTH)
        assert listed.status == 200
        rows = (await listed.json())["runs"]
        assert len(rows) == 50 and [row["started"] for row in rows] == list(range(54, 4, -1))
        assert all(set(row) == ROW_KEYS for row in rows)
        assert all(row["model"] == "fake-local" and row["workspace"] == "/home/x/shop" for row in rows)
        archived = await client.get("/api/agent/run?id=archived-54", headers=AUTH)
        assert archived.status == 200
        data = await archived.json()
        assert set(data) == RUN_KEYS and data["pending"] is None
        assert len(data["steps"][0]["result"].encode("utf-8")) == 4096
        assert len(store.load_session("archived-54")["steps"][0]["result"]) == 5000
        refused = await client.post("/api/agent/decide", headers=AUTH,
                                    json={"id": "archived-54", "approve": True})
        assert refused.status == 409

    run_with_client(gui, scenario)


def test_stop_an_unfinished_saved_session(environment):
    _, _, store, _ = environment
    store.save_session({"id": "archived-waiting", "status": "waiting", "goal": "Fake.",
                        "model": "fake-local", "workspace": "/home/x/shop", "started": 10,
                        "pending": {"n": 1, "action": "run_command", "args": {}, "diff": "",
                                    "argv": ["fake-check"], "sandboxed": False}})

    async def scenario(client):
        refused = await client.post("/api/agent/decide", headers=AUTH,
                                    json={"id": "archived-waiting", "approve": True})
        assert refused.status == 409
        stopped = await client.post("/api/agent/stop", headers=AUTH, json={"id": "archived-waiting"})
        assert stopped.status == 200
        data = await stopped.json()
        assert data["status"] == "stopped" and data["pending"] is None
        assert store.load_session("archived-waiting")["status"] == "stopped"

    run_with_client(gui_for(environment), scenario)


@pytest.mark.parametrize("method, path", [
    ("get", "/api/agent/models"), ("post", "/api/agent/start"), ("get", "/api/agent/run?id=fake"),
    ("post", "/api/agent/decide"), ("post", "/api/agent/stop"), ("get", "/api/agent/runs"),
    ("get", "/api/agent/files"), ("put", "/api/agent/files"),
])
def test_every_agent_route_requires_the_token(environment, method, path):
    gui = gui_for(environment)

    async def scenario(client):
        response = await getattr(client, method)(path, json={} if method != "get" else None)
        assert response.status == 401 and set(await response.json()) == {"error"}
        wrong = await getattr(client, method)(path, headers={"Authorization": "Bearer fake-wrong-token"},
                                              json={} if method != "get" else None)
        assert wrong.status == 401

    run_with_client(gui, scenario)
    assert not gui.agent_runs


def test_agent_writes_require_same_origin_json(environment):
    async def scenario(client):
        foreign = await client.put("/api/agent/files", headers={**AUTH, "Origin": "https://example.invalid"},
                                   json={"name": "soul", "text": "Fake."})
        form = await client.put("/api/agent/files", headers=AUTH, data={"name": "soul", "text": "Fake."})
        assert foreign.status == 403 and form.status == 415
        assert "error" in await foreign.json() and "error" in await form.json()

    run_with_client(gui_for(environment), scenario)


@pytest.mark.parametrize("kind", ["home", "outside", "missing", "file", "relative", "symlink", "empty"])
def test_bad_workspaces_are_refused(environment, tmp_path, kind):
    home, workspace, _, _ = environment
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "note.txt").write_text("A fake note.", encoding="utf-8")
    (home / "escape").symlink_to(outside, target_is_directory=True)
    value = {"home": str(home), "outside": str(outside), "missing": str(home / "missing"),
             "file": str(home / "note.txt"), "relative": "shop", "symlink": str(home / "escape"),
             "empty": ""}[kind]
    gui = gui_for(environment)

    async def scenario(client):
        response = await client.post("/api/agent/start", headers=AUTH,
                                     json={"model": "fake-local", "workspace": value, "goal": "A fake goal."})
        assert response.status == 400 and set(await response.json()) == {"error"}

    run_with_client(gui, scenario)
    assert not gui.agent_runs


def test_workspace_links_and_tilde_are_resolved_inside_home(environment):
    home, workspace, _, _ = environment
    (home / "shop-link").symlink_to(workspace, target_is_directory=True)
    assert agent_api.check_workspace(str(home / "shop-link")) == workspace.resolve()
    assert agent_api.check_workspace("~/shop") == workspace.resolve()


@pytest.mark.parametrize("changes", [
    {"model": "fake-unknown"}, {"model": "fake-missing"}, {"model": []}, {"goal": ""}, {"goal": 1},
    {"goal": "x" * (agent_api.MAX_GOAL_CHARS + 1)}, {"goal": "\ud800"}, {"max_steps": 0}, {"max_steps": True},
    {"max_steps": 1.5}, {"max_steps": "3"},
])
def test_start_checks_the_request(environment, changes):
    _, workspace, _, _ = environment
    gui = gui_for(environment)

    async def scenario(client):
        response = await client.post("/api/agent/start", headers=AUTH,
                                     json={"model": "fake-local", "workspace": str(workspace),
                                           "goal": "A fake goal.", **changes})
        assert response.status == 400 and set(await response.json()) == {"error"}

    run_with_client(gui, scenario)
    assert not gui.agent_runs


@pytest.mark.parametrize("method, path", [
    ("post", "/api/agent/start"), ("post", "/api/agent/decide"), ("post", "/api/agent/stop"),
    ("put", "/api/agent/files"),
])
def test_agent_mutations_require_a_json_object(environment, method, path):
    async def scenario(client):
        for body in ("not json", "[]"):
            response = await getattr(client, method)(path, headers={**AUTH, "Content-Type": "application/json"},
                                                     data=body)
            assert response.status == 400 and set(await response.json()) == {"error"}

    run_with_client(gui_for(environment), scenario)


def test_unknown_ids_and_invalid_decisions_are_json_errors(environment):
    async def scenario(client):
        for path in ("/api/agent/run", "/api/agent/run?id=../fake", "/api/agent/run?id=fake-unknown"):
            response = await client.get(path, headers=AUTH)
            assert response.status == (404 if path.endswith("fake-unknown") else 400)
            assert set(await response.json()) == {"error"}
        for path, body, status in (
            ("/api/agent/decide", {"id": "fake-unknown", "approve": True}, 404),
            ("/api/agent/stop", {"id": "fake-unknown"}, 404),
            ("/api/agent/stop", {}, 400),
            ("/api/agent/decide", {"id": "fake-unknown", "approve": "yes"}, 400),
            ("/api/agent/decide", {"id": "fake-unknown", "approve": True, "always": 1}, 400),
        ):
            response = await client.post(path, headers=AUTH, json=body)
            assert response.status == status and set(await response.json()) == {"error"}

    run_with_client(gui_for(environment), scenario)


@pytest.mark.parametrize("body", [{"name": "skills", "text": "Fake."}, {"name": "soul", "text": 1},
                                  {"name": "../fake", "text": "Fake."}, {"name": "soul", "text": "\ud800"}, {}])
def test_only_the_three_plain_files_can_be_edited(environment, body):
    async def scenario(client):
        response = await client.put("/api/agent/files", headers=AUTH, json=body)
        assert response.status == 400 and set(await response.json()) == {"error"}

    run_with_client(gui_for(environment), scenario)


def test_private_keeps_agent_requests_on_your_own_computers(environment):
    _, workspace, _, settings = environment
    settings.review.private = True
    gui = gui_for(environment)

    async def scenario(client):
        response = await client.get("/api/agent/models", headers=AUTH)
        ready = {row["name"]: row["ready"] for row in (await response.json())["models"]}
        assert ready == {"fake-local": True, "fake-cloud": False, "fake-missing": False}
        refused = await client.post("/api/agent/start", headers=AUTH,
                                    json={"model": "fake-cloud", "workspace": str(workspace), "goal": "Fake."})
        assert refused.status == 403 and "Private" in (await refused.json())["error"]

    run_with_client(gui, scenario)


def test_server_shutdown_stops_a_live_model_call_and_saves_the_run(environment):
    _, workspace, store, _ = environment
    called, cancelled = [], []

    async def ask(prompt):
        called.append(prompt)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    gui = gui_for(environment, ask)

    async def scenario(client):
        run_id = await start(client, workspace)
        for _ in range(100):
            if called:
                break
            await asyncio.sleep(0.002)
        assert called and gui.agent_runs[run_id].to_dict()["status"] == "running"
        return run_id

    run_id = run_with_client(gui, scenario)
    assert cancelled == [True] and gui.agent_runs[run_id].task.done()
    assert store.load_session(run_id)["status"] == "stopped"
