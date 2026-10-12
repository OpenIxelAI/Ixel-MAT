"""The Agent page's run options (AGENT_BETA.md, "Beta 2" > "Run options"): files attached to a run, the
Computer access warning, Fast, the effort level and follow-ups, through the app's own aiohttp test client."""
import asyncio
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from ixel_mat.agent.store import MAX_UPLOAD_BYTES as BIGGEST_UPLOAD
from ixel_mat.agent.store import MAX_UPLOADS_PER_RUN, AgentStore
from ixel_mat.agents.base import AgentConfig
from ixel_mat.config import secrets
from ixel_mat.gui import agent_api
from ixel_mat.gui.agent_api import COMPUTER_WARNING
from ixel_mat.gui.server import GuiServer
from ixel_mat.pictures import megabytes
from ixel_mat.runtime import Settings

TOKEN = "obviously-fake-agent-session-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOTE = b"A fake note for /home/x.\n"
CAP = 2_000_000  # the route's own limit, lowered here so a test needn't send 50 MB


def action(name, **args):
    return json.dumps({"thought": "A fake model's short reason.", "action": name, "args": args})


class ScriptedAsk:
    """Record every call's prompt, pictures and effort, and answer with the next scripted reply."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []
        self.calls = []

    async def __call__(self, prompt, pictures=(), effort=""):
        self.prompts.append(prompt)
        self.calls.append({"pictures": list(pictures), "effort": effort})
        return self.replies.pop(0), {"cost_usd": 0.0}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = home / "shop"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(secrets, "_ENV_DIR", home / ".config" / "ixel-mat")
    store = AgentStore(home / ".config" / "ixel-mat" / "agent")
    configs = {
        # a model on your own computer: Ixel calls it itself, and it takes the OpenAI scale, minimal to high
        "fake-local": AgentConfig(name="fake-local", label="Fake local", type="http",
                                  url="http://127.0.0.1:9999", model="fake-local-model"),
        # a model with effort levels, low to max
        "fake-thinking": AgentConfig(name="fake-thinking", label="Fake thinking", type="http",
                                     url="https://api.anthropic.com", token="obviously-fake-agent-key",
                                     model="claude-opus-4-6"),
        # a model that takes no effort level at all
        "fake-plain": AgentConfig(name="fake-plain", label="Fake plain", type="http",
                                  url="https://api.anthropic.com", token="obviously-fake-agent-key",
                                  model="claude-haiku-4-5"),
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


async def rows(client):
    """The runs list as {run id: its row}."""
    response = await client.get("/api/agent/runs", headers=AUTH)
    assert response.status == 200
    return {row["id"]: row for row in (await response.json())["runs"]}


BOUNDARY = b"ixel-fake-attachment-boundary"


def part_of(name, data, field="file", last=True, filename=True):
    """One part of a multipart body holding a file, written out by hand so the file name reaches the server
    exactly as a browser sends it (aiohttp's FormData quotes the names a traversal test needs)."""
    shown = f'filename="{name}"' if filename else ""
    head = (f"--{BOUNDARY.decode()}\r\nContent-Disposition: form-data; name=\"{field}\""
            + (f"; {shown}" if shown else "") + "\r\n\r\n").encode("utf-8")
    return b"".join([head, data, f"\r\n--{BOUNDARY.decode()}{'--' if last else ''}\r\n".encode("utf-8")])


def multipart(name, data, field="file", filename=True):
    """A body with one file in the field the page uses."""
    return part_of(name, data, field, filename=filename)


async def send(client, body):
    return await client.post(
        "/api/agent/upload",
        headers={**AUTH, "Content-Type": f"multipart/form-data; boundary={BOUNDARY.decode()}"},
        data=body)


async def attach(client, name="notes.txt", data=NOTE, field="file", filename=True):
    """One file attached in the composer, in the multipart field the page uses."""
    return await send(client, multipart(name, data, field, filename=filename))


async def attached(client, name="notes.txt", data=NOTE, field="file"):
    """attach, for a file that is taken: the {id, name, kind, size} the page got."""
    response = await attach(client, name, data, field)
    assert response.status == 200, await response.text()
    return await response.json()


# ── Files attached to a run ───────────────────────────────────────────────────

def test_an_attached_file_is_kept_until_a_start_names_it(environment):
    _, workspace, store, _ = environment
    ask = ScriptedAsk(action("read_file", path="upload:notes.txt"), action("finish", answer="I read the note."))
    gui = gui_for(environment, ask)

    async def scenario(client):
        info = await attached(client)
        assert info["name"] == "notes.txt" and info["kind"] == "text" and info["size"] == len(NOTE)
        # it waits under a pending id of its own: not in the workspace, and not in any run yet
        assert list(gui.agent.pending_uploads) == [info["id"]]
        assert (store.uploads_dir(info["id"]) / "notes.txt").read_bytes() == NOTE
        assert not (workspace / "notes.txt").exists()

        run_id = await start(client, workspace, uploads=[info["id"]])
        data = await wait_status(client, run_id, "done")
        assert data["uploads"] == [{"id": "notes.txt", "name": "notes.txt", "kind": "text"}]
        assert f"- notes.txt (text, {len(NOTE)} bytes)" in ask.prompts[0]  # the run's first prompt lists it
        assert "upload:NAME" in ask.prompts[0]
        assert data["steps"][0]["ok"] is True and "A fake note" in data["steps"][0]["result"]
        # the file moved into the run's own folder, and the pending copy is gone
        assert (store.uploads_dir(run_id) / "notes.txt").read_bytes() == NOTE
        assert store.list_uploads(info["id"]) == []
        assert not gui.agent.pending_uploads

    run_with_client(gui, scenario)


def test_only_one_start_can_claim_an_attached_file(environment):
    _, workspace, store, _ = environment
    gui = gui_for(environment, ScriptedAsk(action("finish", answer="Attached and done.")))

    async def scenario(client):
        info = await attached(client)
        run_id = await start(client, workspace, uploads=[info["id"]])
        await wait_status(client, run_id, "done")
        assert list(gui.agent.pending_uploads) == []
        again = await client.post("/api/agent/start", headers=AUTH,
                                  json={"model": "fake-local", "workspace": str(workspace),
                                        "goal": "The same file again.", "uploads": [info["id"]]})
        assert again.status == 400
        error = (await again.json())["error"]
        assert "isn't there any more" in error and "Attach it again" in error
        assert set(gui.agent_runs) == {run_id}

    run_with_client(gui, scenario)


def test_a_run_takes_at_most_ten_attached_files(environment):
    _, workspace, store, _ = environment
    gui = gui_for(environment, ScriptedAsk(action("finish", answer="Every file read.")))

    async def scenario(client):
        infos = [await attached(client, f"note-{n}.txt", f"A fake note {n} for /home/x.\n".encode())
                 for n in range(MAX_UPLOADS_PER_RUN + 1)]
        ids = [info["id"] for info in infos]
        too_many = await client.post("/api/agent/start", headers=AUTH,
                                     json={"model": "fake-local", "workspace": str(workspace),
                                           "goal": "Look at every file.", "uploads": ids})
        assert too_many.status == 400
        assert f"at most {MAX_UPLOADS_PER_RUN} different ids" in (await too_many.json())["error"]
        assert list(gui.agent.pending_uploads) == ids  # none of them was taken
        run_id = await start(client, workspace, uploads=ids[:MAX_UPLOADS_PER_RUN])
        data = await wait_status(client, run_id, "done")
        assert [item["name"] for item in data["uploads"]] == [f"note-{n}.txt" for n in range(MAX_UPLOADS_PER_RUN)]
        assert len(store.list_uploads(run_id)) == MAX_UPLOADS_PER_RUN
        assert list(gui.agent.pending_uploads) == [ids[-1]]  # the eleventh waits for a run of its own

    run_with_client(gui, scenario)


def test_a_file_over_the_limit_is_refused(environment, monkeypatch):
    _, workspace, store, _ = environment
    assert agent_api.MAX_UPLOAD_BYTES == BIGGEST_UPLOAD  # the route's cap is the store's 50 MB
    monkeypatch.setattr(agent_api, "MAX_UPLOAD_BYTES", CAP)  # read here, so a test can lower it
    gui = gui_for(environment)
    too_big = f"That file is over {megabytes(CAP)}, more than the agent takes."

    async def scenario(client):
        # more than the Content-Length check leaves room for
        long = await attach(client, "long.txt", b"x" * (CAP + 64 * 1024 + 1000))
        assert long.status == 413 and (await long.json())["error"] == too_big
        # and one that only shows how big it is once it is read in pieces
        chunked = await attach(client, "big.txt", b"x" * (CAP + 1000))
        assert chunked.status == 413 and (await chunked.json())["error"] == too_big
        assert not gui.agent.pending_uploads
        kept = store.root / "uploads"  # nothing was kept at all
        assert not kept.exists() or not list(kept.iterdir())

    run_with_client(gui, scenario)


@pytest.mark.parametrize("name, field, kept, kind, shown", [
    ("../../../etc/passwd", "file", "passwd", "text", True),
    ("/etc/passwd", "file", "passwd", "text", True),
    ("C:\\photo.png", "file", "photo.png", "picture", True),
    ("..", "file", "file", "text", True),
    ("anything at all", "file", "file", "text", False),  # the page sends no file name of its own
    ("notes.txt", "attachment", None, "text", True),
])
def test_an_attached_name_is_cleaned_or_refused(environment, name, field, kept, kind, shown):
    _, workspace, store, _ = environment
    gui = gui_for(environment)

    async def scenario(client):
        response = await attach(client, name, field=field, filename=shown)
        if kept is None:
            assert response.status == 400 and "file" in (await response.json())["error"]
            return
        assert response.status == 200, await response.text()
        info = await response.json()
        assert info["name"] == kept and info["kind"] == kind
        assert list((store.uploads_dir(info["id"])).iterdir()) == [store.uploads_dir(info["id"]) / kept]

    run_with_client(gui, scenario)


def test_a_file_that_looks_like_a_secret_is_refused(environment):
    _, workspace, store, _ = environment
    gui = gui_for(environment)

    async def scenario(client):
        for name in (".env", "server.pem"):
            response = await attach(client, name)
            assert response.status == 400 and "secret" in (await response.json())["error"]
            assert not gui.agent.pending_uploads
        empty = await attach(client, "empty.txt", b"")
        assert empty.status == 400 and "empty" in (await empty.json())["error"]
        assert not gui.agent.pending_uploads

    run_with_client(gui, scenario)


@pytest.mark.parametrize("name, data, second", [
    ("a.txt", b"one\n", part_of("b.txt", b"two\n")),
    ("a.txt", b"one\n", part_of("note", b"hi\n", filename=False)),
])
def test_only_one_file_per_request(environment, name, data, second):
    _, workspace, _, _ = environment
    gui = gui_for(environment)

    async def scenario(client):
        two = await send(client, part_of(name, data, last=False) + second)
        assert two.status == 400 and "one file per request" in (await two.json())["error"]
        assert not gui.agent.pending_uploads
        # a body that isn't a multipart one at all is refused the same way
        broken = await client.post("/api/agent/upload", headers={**AUTH, "Content-Type":
                                 f"multipart/form-data; boundary={BOUNDARY.decode()}"}, data=b"nothing here")
        assert broken.status == 400 and set(await broken.json()) == {"error"}
        assert not gui.agent.pending_uploads

    run_with_client(gui, scenario)


def test_the_upload_route_needs_the_token_and_a_multipart_body(environment):
    gui = gui_for(environment)

    async def scenario(client):
        for headers in ({}, {"Authorization": "Bearer fake-wrong-token"}):
            response = await client.post("/api/agent/upload", headers=headers, data=b"not multipart at all")
            assert response.status == 401 and set(await response.json()) == {"error"}
        wrong_kind = await client.post("/api/agent/upload", headers={**AUTH, "Content-Type": "application/json"},
                                       data=b"{}")
        assert wrong_kind.status == 415 and set(await wrong_kind.json()) == {"error"}
        assert not gui.agent.pending_uploads

    run_with_client(gui, scenario)


# ── Computer access ───────────────────────────────────────────────────────────

def test_computer_access_needs_the_warning_confirmed(environment):
    home, workspace, store, _ = environment
    (home / "notes.txt").write_text("A fake note outside the workspace, in /home/x.\n", encoding="utf-8")
    ask = ScriptedAsk(action("read_file", path="~/notes.txt"), action("finish", answer="I read it."),
                      action("read_file", path="~/notes.txt"), action("finish", answer="I left it alone."))
    gui = gui_for(environment, ask)

    async def scenario(client):
        for body in ({"computer": True}, {"computer": True, "confirmed": False},
                     {"computer": True, "confirmed": "yes"}):
            response = await client.post("/api/agent/start", headers=AUTH,
                                         json={"model": "fake-local", "workspace": str(workspace),
                                               "goal": "Read ~/notes.txt.", **body})
            assert response.status == 400, await response.text()
            assert (await response.json())["error"] == COMPUTER_WARNING
        assert not gui.agent_runs

        on = await start(client, workspace, computer=True, confirmed=True)
        data = await wait_status(client, on, "done")
        assert data["computer"] is True
        assert data["steps"][0]["ok"] is True and "outside the workspace" in data["steps"][0]["result"]
        assert store.load_session(on)["computer"] is True
        assert (await rows(client))[on]["computer"] is True

        # the same run without the switch and without the warning reads nothing outside the workspace
        off = await start(client, workspace)
        plainly = await wait_status(client, off, "done")
        assert plainly["computer"] is False
        assert plainly["steps"][0]["ok"] is False and "isn't relative to the workspace" in plainly["steps"][0]["result"]
        assert (await rows(client))[off]["computer"] is False

    run_with_client(gui, scenario)


# ── Fast and the effort level ─────────────────────────────────────────────────

def test_fast_caps_a_run_at_ten_steps_and_the_models_lowest_effort(environment):
    _, workspace, store, _ = environment
    (workspace / "note.txt").write_text("A fake note.\n", encoding="utf-8")
    # a different last line each time, so the run isn't taken for repeating itself
    ask = ScriptedAsk(*[action("read_file", path="note.txt", end=n) for n in range(1, 21)])
    gui = gui_for(environment, ask)

    async def scenario(client):
        run_id = await start(client, workspace, model="fake-thinking", fast=True, max_steps=25)
        data = await wait_status(client, run_id, "failed")
        assert data["status"] == "failed" and "step limit (10)" in data["error"]
        assert len(data["steps"]) == 10 and data["usage"]["calls"] == 10
        assert [call["effort"] for call in ask.calls] == ["low"] * 10  # the lowest this model takes
        assert [call["pictures"] for call in ask.calls] == [[]] * 10
        saved = store.load_session(run_id)
        assert saved["max_steps"] == 10 and saved["effort"] == "low"

    run_with_client(gui, scenario)


def test_the_effort_a_person_picks_is_sent_with_every_call(environment):
    _, workspace, store, _ = environment
    (workspace / "note.txt").write_text("A fake note.\n", encoding="utf-8")
    ask = ScriptedAsk(action("read_file", path="note.txt"), action("finish", answer="I read the note."),
                      action("read_file", path="note.txt"), action("finish", answer="I read it again."),
                      action("finish", answer="The nearest level."), action("finish", answer="No effort."))
    gui = gui_for(environment, ask)

    async def scenario(client):
        # nothing picked: no effort goes with the calls
        await wait_status(client, await start(client, workspace, model="fake-thinking"), "done")
        assert [call["effort"] for call in ask.calls] == ["", ""]
        # the level picked is sent on every call, and it wins over fast's lowest one
        fast = await start(client, workspace, model="fake-thinking", fast=True, effort="max")
        data = await wait_status(client, fast, "done")
        assert [call["effort"] for call in ask.calls[2:]] == ["max", "max"]
        assert store.load_session(fast)["effort"] == "max"
        assert data["computer"] is False and data["follows"] is None
        # a level the model doesn't take goes as the nearest one it does
        nearest = await start(client, workspace, model="fake-thinking", effort="minimal")
        await wait_status(client, nearest, "done")
        assert ask.calls[-1]["effort"] == "low"
        assert store.load_session(nearest)["effort"] == "low"
        # a model that takes no effort level is asked for none, whatever was picked
        plain = await start(client, workspace, model="fake-plain", effort="ultra")
        await wait_status(client, plain, "done")
        assert ask.calls[-1]["effort"] == "" and store.load_session(plain)["effort"] == ""

    run_with_client(gui, scenario)


@pytest.mark.parametrize("changes", [
    {"computer": 1}, {"computer": True, "confirmed": False}, {"computer": True, "confirmed": "yes"},
    {"fast": 1}, {"fast": "yes"}, {"effort": "turbo"}, {"effort": 5}, {"effort": None},
    {"uploads": "abc"}, {"uploads": {"a": 1}}, {"uploads": ["abc", "abc"]}, {"uploads": [1]},
    {"uploads": ["../x"]}, {"uploads": ["a" * 101]}, {"uploads": ["a"] * (MAX_UPLOADS_PER_RUN + 1)},
    {"follows": 5}, {"follows": ["a"]}, {"follows": "not an id!"}, {"follows": "a" * 101},
    {"follows": "fake-unknown-run-id"},
])
def test_the_new_run_options_are_checked(environment, changes):
    _, workspace, _, _ = environment
    gui = gui_for(environment)

    async def scenario(client):
        response = await client.post("/api/agent/start", headers=AUTH,
                                     json={"model": "fake-local", "workspace": str(workspace),
                                           "goal": "Look around the shop.", **changes})
        assert response.status == 400 and set(await response.json()) == {"error"}

    run_with_client(gui, scenario)
    assert not gui.agent_runs


# ── Follow-ups ────────────────────────────────────────────────────────────────

def test_a_follow_up_starts_from_the_earlier_goal_and_answer(environment):
    _, workspace, store, _ = environment
    (workspace / "note.txt").write_text("A fake note for /home/x.\n", encoding="utf-8")
    ask = ScriptedAsk(action("read_file", path="note.txt"), action("finish", answer="I read the note."),
                      action("finish", answer="I summarised the note."))
    gui = gui_for(environment, ask)

    async def scenario(client):
        first = await start(client, workspace, goal="Look around the shop.")
        await wait_status(client, first, "done")
        follow = await start(client, workspace, goal="Now summarise the note.", follows=first)
        data = await wait_status(client, follow, "done")
        assert data["follows"] == first and data["answer"] == "I summarised the note."

        prompt = ask.prompts[2]  # the follow-up's first turn
        assert "This run continues an earlier one" in prompt
        assert "data: the earlier run" in prompt  # fenced, like everything else that isn't its own words
        assert "Look around the shop." in prompt and "I read the note." in prompt
        assert "Step 1: read_file" in prompt and "Step 2: finish" in prompt
        assert "Now summarise the note." in prompt  # its own goal, sent after the earlier one's

        saved = store.load_session(follow)
        earlier = saved["earlier"]
        assert earlier["id"] == first
        assert earlier["goal"] == "Look around the shop." and earlier["answer"] == "I read the note."
        assert [step["action"] for step in earlier["steps"]] == ["read_file", "finish"]
        assert saved["follows"] == first and saved["goal"] == "Now summarise the note."
        listed = await rows(client)
        assert listed[follow]["follows"] == first and listed[first]["follows"] is None
        assert set(listed[follow]) == {"id", "goal", "status", "started", "model", "workspace",
                                       "computer", "follows"}

    run_with_client(gui, scenario)


def test_a_follow_up_needs_the_earlier_run_to_have_finished(environment):
    _, workspace, store, _ = environment
    ask = ScriptedAsk(action("write_file", path="answer.txt", content="A fake answer.\n"),
                      action("finish", answer="I wrote it."))
    gui = gui_for(environment, ask)

    async def scenario(client):
        first = await start(client, workspace)
        await wait_status(client, first, "waiting")
        busy = await client.post("/api/agent/start", headers=AUTH,
                                 json={"model": "fake-local", "workspace": str(workspace),
                                       "goal": "And now?", "follows": first})
        assert busy.status == 409 and "still working" in (await busy.json())["error"]
        assert not (workspace / "answer.txt").exists()

        stopped = await client.post("/api/agent/stop", headers=AUTH, json={"id": first})
        assert stopped.status == 200
        follow = await start(client, workspace, goal="And now?", follows=first)
        data = await wait_status(client, follow, "done")
        assert data["follows"] == first
        assert "(no answer: it ended without one)" in ask.prompts[-1]
        assert "Look around the shop." in ask.prompts[-1]  # the stopped run's goal

    run_with_client(gui, scenario)


def test_a_pending_file_goes_when_the_server_stops(environment):
    _, workspace, store, _ = environment
    gui = gui_for(environment, ScriptedAsk(action("finish", answer="No file needed.")))

    async def scenario(client):
        info = await attached(client)
        assert list(gui.agent.pending_uploads) == [info["id"]]
        return info["id"]

    pending = run_with_client(gui, scenario)
    assert not gui.agent.pending_uploads
    assert store.list_uploads(pending) == []
    assert not (store.uploads_dir(pending) / "notes.txt").exists()


def test_two_starts_cannot_both_claim_one_file(environment):
    _, workspace, store, _ = environment
    ask = ScriptedAsk(action("finish", answer="The file is mine."))
    gui = gui_for(environment, ask)

    async def scenario(client):
        info = await attached(client)
        body = {"model": "fake-local", "workspace": str(workspace),
                "goal": "Look at the attached file.", "uploads": [info["id"]]}
        answers = await asyncio.gather(*(client.post("/api/agent/start", headers=AUTH, json=body)
                                         for _ in range(2)))
        assert sorted(answer.status for answer in answers) == [200, 400]
        [taken] = [answer for answer in answers if answer.status == 200]
        run_id = (await taken.json())["id"]
        data = await wait_status(client, run_id, "done")
        assert [item["name"] for item in data["uploads"]] == ["notes.txt"]
        assert len(gui.agent_runs) == 1
        assert not gui.agent.pending_uploads

    run_with_client(gui, scenario)


def test_a_file_waiting_for_a_run_is_let_go_past_the_limit(environment, monkeypatch):
    _, workspace, store, _ = environment
    monkeypatch.setattr(agent_api, "MAX_PENDING_UPLOADS", 3)
    gui = gui_for(environment, ScriptedAsk(action("finish", answer="Three files read.")))

    async def scenario(client):
        infos = [await attached(client, f"note-{n}.txt", f"A fake note {n} for /home/x.\n".encode())
                 for n in range(4)]
        assert list(gui.agent.pending_uploads) == [info["id"] for info in infos[1:]]  # the oldest went
        assert store.list_uploads(infos[0]["id"]) == []
        run_id = await start(client, workspace, uploads=[info["id"] for info in infos[1:]])
        data = await wait_status(client, run_id, "done")
        assert [item["name"] for item in data["uploads"]] == [f"note-{n}.txt" for n in (1, 2, 3)]
        gone = await client.post("/api/agent/start", headers=AUTH,
                                 json={"model": "fake-local", "workspace": str(workspace),
                                       "goal": "And the first one?", "uploads": [infos[0]["id"]]})
        assert gone.status == 400 and "isn't there any more" in (await gone.json())["error"]

    run_with_client(gui, scenario)


def test_the_follow_up_prompt_holds_only_the_earlier_runs_last_steps(environment):
    _, workspace, store, _ = environment
    ask = ScriptedAsk(action("list_dir", path="."), action("list_dir", path="dir-a"),
                      action("list_dir", path="dir-b"), action("list_dir", path="dir-c"),
                      action("finish", answer="I looked in three folders."),
                      action("finish", answer="I carried on from there."))
    gui = gui_for(environment, ask)

    async def scenario(client):
        first = await start(client, workspace, goal="Look around the shop.")
        await wait_status(client, first, "done")
        follow = await start(client, workspace, goal="And the other folders?", follows=first)
        await wait_status(client, follow, "done")
        prompt = ask.prompts[5]  # the follow-up's first turn
        assert "Its last 3 steps" in prompt
        assert "dir-b" in prompt and "dir-c" in prompt
        assert "dir-a" not in prompt and "Step 1:" not in prompt
        assert store.load_session(follow)["earlier"]["steps"][0]["n"] == 3

    run_with_client(gui, scenario)


def test_a_follow_up_of_a_saved_run_works_after_a_restart(environment):
    _, workspace, store, _ = environment
    (workspace / "note.txt").write_text("A fake note for /home/x.\n", encoding="utf-8")
    before = gui_for(environment, ScriptedAsk(action("read_file", path="note.txt"),
                                              action("finish", answer="I read the note.")))

    async def opening(client):
        run_id = await start(client, workspace, goal="Look around the shop.")
        await wait_status(client, run_id, "done")  # the run finishes before Ixel stops, as a real one would
        return run_id

    first = run_with_client(before, opening)
    assert before.agent_runs  # live only until Ixel stops; the session is what is left

    ask = ScriptedAsk(action("finish", answer="I summarised the saved run."))
    gui = gui_for(environment, ask)

    async def again(client):
        follow = await start(client, workspace, goal="Now summarise it.", follows=first)
        await wait_status(client, follow, "done")
        assert ask.prompts  # the follow-up did ask the model
        assert "data: the earlier run" in ask.prompts[0]
        assert "Look around the shop." in ask.prompts[0] and "I read the note." in ask.prompts[0]
        assert store.load_session(follow)["earlier"]["id"] == first

    run_with_client(gui, again)
