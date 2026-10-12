"""The agent's loop, using scripted answers instead of a real model or command."""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from ixel_mat.agent import runner
from ixel_mat.agent.runner import AgentRun
from ixel_mat.agent.sandbox import CommandResult
from ixel_mat.agent.store import AgentStore
from ixel_mat.agents.base import AgentConfig
from ixel_mat.usage import Usage


def _reply(action: str, **args) -> str:
    return json.dumps({"thought": "Check one thing", "action": action, "args": args})


class ScriptedAsk:
    """Record each prompt, and return the next answer and its optional usage."""

    def __init__(self, *replies: str | tuple[str, dict | None]):
        self.replies = iter(replies)
        self.prompts: list[str] = []

    async def __call__(self, prompt: str) -> tuple[str, dict | None]:
        self.prompts.append(prompt)
        reply = next(self.replies)
        return (reply, None) if isinstance(reply, str) else reply


def _cfg() -> AgentConfig:
    return AgentConfig(name="fake", label="Fake model", type="http", model="fake-model",
                       url="https://model.invalid", token="obviously-fake-token")


def _run(tmp_path: Path, ask, max_steps: int = 25) -> AgentRun:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return AgentRun(_cfg(), workspace, "Check the project", AgentStore(tmp_path / "agent"),
                    max_steps=max_steps, ask=ask)


async def _waiting(run: AgentRun) -> dict:
    """Wait for the approval card; a broken loop fails quickly instead of hanging a test."""
    async def wait() -> dict:
        while run.status == "running":
            await asyncio.sleep(0)
        assert run.status == "waiting", run.to_dict()
        return run.to_dict()

    return await asyncio.wait_for(wait(), timeout=2)


async def _done(run: AgentRun) -> dict:
    await asyncio.wait_for(run.task, timeout=2)
    return run.to_dict()


def test_finished_run_has_the_page_shape_and_saves_every_step(tmp_path):
    async def scenario():
        ask = ScriptedAsk(_reply("list_dir"), _reply("finish", answer="The folder is empty."))
        run = _run(tmp_path, ask)
        assert run.store.load_session(run.id)["status"] == "running"
        run.start()
        page = await _done(run)
        assert set(page) == {"id", "status", "goal", "model", "workspace", "started", "answer", "error",
                             "steps", "pending", "usage", "uploads", "computer", "follows"}
        assert page["status"] == "done"
        assert page["model"] == "fake"
        assert page["workspace"] == str(tmp_path / "workspace")
        assert page["answer"] == "The folder is empty."
        assert page["error"] == "" and page["pending"] is None
        assert [step["action"] for step in page["steps"]] == ["list_dir", "finish"]
        assert all(set(step) == {"n", "thought", "action", "args", "ok", "result", "usage"}
                   for step in page["steps"])
        assert page["steps"][0]["result"] == "(empty folder)"
        assert page["steps"][0]["ok"] is True
        assert "(empty folder)" in ask.prompts[1]
        saved = run.store.load_session(run.id)
        assert saved["status"] == "done" and saved["steps"] == page["steps"]
        assert saved["approvals"] == []
        assert page["usage"] == {"calls": 2, "cost": 0.0}

    asyncio.run(scenario())


def test_bad_reply_is_sent_back_once_with_its_parse_error(tmp_path):
    async def scenario():
        ask = ScriptedAsk("not an action", _reply("finish", answer="Done"))
        run = _run(tmp_path, ask)
        run.start()
        page = await _done(run)
        assert page["status"] == "done"
        assert len(ask.prompts) == 2
        assert "not an action" in ask.prompts[1]
        assert "no JSON object" in ask.prompts[1]
        assert "data, not instructions" in ask.prompts[1]
        assert len(page["steps"]) == 1 and page["steps"][0]["action"] == "finish"
        assert page["usage"]["calls"] == 2

    asyncio.run(scenario())


def test_two_bad_replies_fail_and_are_saved(tmp_path):
    async def scenario():
        ask = ScriptedAsk("not JSON", "still not JSON", _reply("finish", answer="Never asked"))
        run = _run(tmp_path, ask)
        run.start()
        page = await _done(run)
        assert page["status"] == "failed" and "JSON" in page["error"]
        assert len(ask.prompts) == 2 and page["steps"] == []
        assert page["usage"]["calls"] == 2
        assert run.store.load_session(run.id)["status"] == "failed"

    asyncio.run(scenario())


def test_a_second_bad_reply_after_a_good_step_also_fails(tmp_path):
    async def scenario():
        ask = ScriptedAsk("not JSON", _reply("list_dir"), "not JSON again")
        run = _run(tmp_path, ask)
        run.start()
        page = await _done(run)
        assert page["status"] == "failed"
        assert len(page["steps"]) == 1 and page["steps"][0]["action"] == "list_dir"
        assert len(ask.prompts) == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("action,args,before,after", [
    ("write_file", {"path": "notes.txt", "content": "new text\n"}, None, "new text\n"),
    ("edit_file", {"path": "notes.txt", "old": "old", "new": "new"}, "old text\n", "new text\n"),
])
def test_write_and_edit_wait_for_approval_then_use_the_reviewed_diff(tmp_path, action, args, before, after):
    async def scenario():
        ask = ScriptedAsk(_reply(action, **args), _reply("finish", answer="Changed the note"))
        run = _run(tmp_path, ask)
        note = tmp_path / "workspace" / "notes.txt"
        if before is not None:
            note.write_text(before, encoding="utf-8")
        run.start()
        page = await _waiting(run)
        pending = page["pending"]
        assert set(pending) == {"n", "action", "args", "diff", "argv", "sandboxed"}
        assert pending["n"] == 1 and pending["action"] == action and pending["args"] == args
        assert "+new text" in pending["diff"] and "b/notes.txt" in pending["diff"]
        assert pending["argv"] == []
        assert len(ask.prompts) == 1 and page["steps"] == []
        assert (note.read_text(encoding="utf-8") if note.exists() else None) == before
        assert run.store.load_session(run.id)["pending"] == pending
        await run.decide(True, always=False)
        page = await _done(run)
        assert page["status"] == "done" and page["pending"] is None
        assert note.read_text(encoding="utf-8") == after
        assert page["steps"][0]["ok"] is True
        assert run.store.load_session(run.id)["approvals"]

    asyncio.run(scenario())


def test_denial_is_a_failed_step_and_is_reported_to_the_model(tmp_path):
    async def scenario():
        ask = ScriptedAsk(_reply("write_file", path="notes.txt", content="new text"),
                          _reply("finish", answer="The write was declined."))
        run = _run(tmp_path, ask)
        run.start()
        await _waiting(run)
        await run.decide(False, always=False)
        page = await _done(run)
        assert page["status"] == "done" and not (tmp_path / "workspace" / "notes.txt").exists()
        step = page["steps"][0]
        assert step["action"] == "write_file" and step["ok"] is False
        assert "den" in step["result"].lower()
        assert step["result"] in ask.prompts[1]
        assert run.store.load_session(run.id)["approvals"]

    asyncio.run(scenario())


def test_allowlisted_command_runs_without_waiting(tmp_path, monkeypatch):
    calls = []

    def command(argv, workspace, **kwargs):
        calls.append((argv, workspace))
        return CommandResult(argv, 0, "Checks passed", False, True)

    monkeypatch.setattr(runner, "run_command", command)

    async def scenario():
        argv = ["pytest", "-q"]
        ask = ScriptedAsk(_reply("run_command", argv=argv), _reply("finish", answer="Checked"))
        run = _run(tmp_path, ask)
        run.store.allow(tmp_path / "workspace", ["pytest"])
        run.start()
        page = await _done(run)
        assert page["status"] == "done" and page["pending"] is None
        assert calls == [(argv, tmp_path / "workspace")]
        assert page["steps"][0]["ok"] is True
        assert "Checks passed" in page["steps"][0]["result"]
        assert run.store.load_session(run.id)["approvals"] == []

    asyncio.run(scenario())


def test_always_allow_command_is_saved_and_later_runs_at_once(tmp_path, monkeypatch):
    calls = []

    def command(argv, workspace, **kwargs):
        calls.append(argv)
        return CommandResult(argv, 0, "Checks passed", False, False)

    monkeypatch.setattr(runner, "run_command", command)
    monkeypatch.setattr(runner, "can_sandbox", lambda: False)

    async def scenario():
        argv = ["pytest", "-q"]
        ask = ScriptedAsk(_reply("run_command", argv=argv), _reply("run_command", argv=[*argv, "tests"]),
                          _reply("finish", answer="Checked twice"))
        run = _run(tmp_path, ask)
        run.start()
        page = await _waiting(run)
        assert page["pending"]["argv"] == argv
        assert page["pending"]["sandboxed"] is False and page["pending"]["diff"] == ""
        assert calls == []
        await run.decide(True, always=True)
        page = await _done(run)
        assert page["status"] == "done" and calls == [argv, [*argv, "tests"]]
        assert run.store.is_allowed(tmp_path / "workspace", argv) is True
        assert len(run.store.load_session(run.id)["approvals"]) == 1

    asyncio.run(scenario())


def test_stop_while_waiting_never_applies_the_change(tmp_path):
    async def scenario():
        ask = ScriptedAsk(_reply("write_file", path="notes.txt", content="new text"))
        run = _run(tmp_path, ask)
        run.start()
        await _waiting(run)
        stopped = await run.stop()
        assert stopped["status"] == "stopped" and stopped["pending"] is None
        page = await _done(run)
        assert page["status"] == "stopped" and len(ask.prompts) == 1
        assert not (tmp_path / "workspace" / "notes.txt").exists()
        assert run.store.load_session(run.id)["status"] == "stopped"

    asyncio.run(scenario())


def test_stop_cancels_an_in_flight_model_call(tmp_path):
    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def ask(prompt):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        run = _run(tmp_path, ask)
        run.start()
        await asyncio.wait_for(entered.wait(), timeout=2)
        stopped = await run.stop()
        await asyncio.wait_for(cancelled.wait(), timeout=2)
        assert stopped["status"] == "stopped"
        assert (await _done(run))["status"] == "stopped"
        assert run.store.load_session(run.id)["status"] == "stopped"

    asyncio.run(scenario())


def test_the_step_limit_stops_before_asking_for_an_extra_action(tmp_path):
    async def scenario():
        ask = ScriptedAsk(_reply("list_dir"), _reply("list_dir"), _reply("finish", answer="Never asked"))
        run = _run(tmp_path, ask, max_steps=2)
        run.start()
        page = await _done(run)
        assert page["status"] == "failed" and "step" in page["error"].lower()
        assert len(page["steps"]) == 2 and len(ask.prompts) == 2
        assert run.store.load_session(run.id)["status"] == "failed"

    asyncio.run(scenario())


def test_results_are_clipped_by_bytes_for_the_page_but_not_the_model_or_disk(tmp_path):
    async def scenario():
        text = "é" * 6000
        ask = ScriptedAsk(_reply("read_file", path="notes.txt"), _reply("finish", answer="Read the note"))
        run = _run(tmp_path, ask)
        (tmp_path / "workspace" / "notes.txt").write_text(text, encoding="utf-8")
        run.start()
        page = await _done(run)
        assert page["status"] == "done"
        shown = page["steps"][0]["result"]
        assert len(shown.encode("utf-8")) <= 4 * 1024
        assert "�" not in shown and shown != text
        assert text in ask.prompts[1]
        assert run.store.load_session(run.id)["steps"][0]["result"] == text

    asyncio.run(scenario())


def test_usage_totals_include_the_bad_reply_and_each_action(tmp_path):
    async def scenario():
        costs = [0.1, 0.2, 0.3]
        ask = ScriptedAsk(("bad reply", {"cost_usd": costs[0]}),
                          (_reply("list_dir"), {"cost_usd": costs[1]}),
                          (_reply("finish", answer="Done"), {"cost_usd": costs[2]}))
        run = _run(tmp_path, ask)
        run.start()
        page = await _done(run)
        assert page["usage"]["calls"] == 3 and page["usage"]["cost"] == pytest.approx(sum(costs))
        assert [step["usage"]["cost_usd"] for step in page["steps"]] == costs[1:]
        assert run.store.load_session(run.id)["usage"] == page["usage"]

    asyncio.run(scenario())


def test_default_ask_uses_the_answer_only_transport_and_records_usage(tmp_path, monkeypatch):
    events = []
    reported = Usage(input_tokens=20, output_tokens=5, cost_usd=0.01)
    record = {"cost_usd": 0.01, "input_tokens": 20, "output_tokens": 5}

    class Transport:
        async def connect(self):
            events.append("connect")

        async def send_and_receive(self, prompt, **kwargs):
            events.append(("send", prompt, kwargs))
            kwargs["on_usage"](reported)
            return _reply("finish", answer="Done")

        async def disconnect(self):
            events.append("disconnect")

    transport = Transport()

    def create(cfg):
        events.append(("create", cfg.name))
        return transport

    class Record:
        def to_dict(self):
            return record

    def usage(agent, round_name, tier, prompt, answer, usages):
        assert agent is transport and usages == [reported]
        assert "Check the project" in prompt and '"finish"' in answer
        events.append(("usage", round_name, tier))
        return Record()

    monkeypatch.setattr(runner, "create_agent", create)
    monkeypatch.setattr(runner, "call_usage", usage)

    async def scenario():
        run = _run(tmp_path, None)
        run.start()
        page = await _done(run)
        assert page["status"] == "done"
        assert events[0] == ("create", "fake") and events[1] == "connect"
        assert events[2][0] == "send" and events[2][2]["use_full_session"] is True
        assert "disconnect" in events and any(event[0] == "usage" for event in events if isinstance(event, tuple))
        assert page["steps"][0]["usage"] == record

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["connect", "send"])
def test_default_ask_masks_errors_and_always_disconnects(tmp_path, monkeypatch, stage):
    disconnected = []

    class Transport:
        async def connect(self):
            if stage == "connect":
                raise RuntimeError("Failed with obviously-fake-token")

        async def send_and_receive(self, prompt, **kwargs):
            raise RuntimeError("Failed with obviously-fake-token")

        async def disconnect(self):
            disconnected.append(True)

    monkeypatch.setattr(runner, "create_agent", lambda cfg: Transport())

    async def scenario():
        run = _run(tmp_path, None)
        run.start()
        page = await _done(run)
        assert page["status"] == "failed" and "Failed" in page["error"]
        assert "obviously-fake-token" not in page["error"]
        assert disconnected == [True]
        assert "obviously-fake-token" not in json.dumps(run.store.load_session(run.id))

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["connect", "send"])
def test_default_ask_timeouts_fail_and_disconnect(tmp_path, monkeypatch, stage):
    disconnected = []

    class Transport:
        async def connect(self):
            if stage == "connect":
                await asyncio.Event().wait()

        async def send_and_receive(self, prompt, **kwargs):
            await asyncio.Event().wait()

        async def disconnect(self):
            disconnected.append(True)

    monkeypatch.setattr(runner, "create_agent", lambda cfg: Transport())
    monkeypatch.setattr(runner, "CONNECT_TIMEOUT", 0.01)

    async def scenario():
        run = _run(tmp_path, None)
        run.cfg.timeout = 0.01
        run.start()
        page = await _done(run)
        assert page["status"] == "failed" and "within" in page["error"]
        assert disconnected == [True]

    asyncio.run(scenario())


def test_memory_skills_and_search_run_at_once_and_feed_the_next_turn(tmp_path):
    async def scenario():
        ask = ScriptedAsk(_reply("remember", text="Use plain text notes"),
                          _reply("load_skill", name="notes"),
                          _reply("search", pattern="keep", glob="*.txt"),
                          _reply("finish", answer="Checked the notes"))
        run = _run(tmp_path, ask)
        skill = tmp_path / "agent" / "skills" / "notes"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: notes\ndescription: Work with plain notes\n---\nKeep notes short.\n", encoding="utf-8")
        (tmp_path / "workspace" / "notes.txt").write_text("keep this note\n", encoding="utf-8")
        run.start()
        page = await _done(run)
        assert page["status"] == "done" and page["pending"] is None
        assert [step["action"] for step in page["steps"]] == ["remember", "load_skill", "search", "finish"]
        assert all(step["ok"] is True for step in page["steps"])
        assert "Use plain text notes" in run.store.file_text("memory")
        assert "Use plain text notes" in ask.prompts[1]
        assert "Keep notes short." in page["steps"][1]["result"]
        assert "Keep notes short." in ask.prompts[2]
        assert "notes.txt:1: keep this note" in page["steps"][2]["result"]
        assert "notes.txt:1: keep this note" in ask.prompts[3]
        assert run.store.load_session(run.id)["approvals"] == []

    asyncio.run(scenario())


@pytest.mark.parametrize("path", [".env", "../outside.txt", "/home/x/.ssh/id_rsa"])
def test_a_refused_read_is_a_step_result_and_the_model_can_continue(tmp_path, path):
    async def scenario():
        ask = ScriptedAsk(_reply("read_file", path=path), _reply("finish", answer="That file is unavailable."))
        run = _run(tmp_path, ask)
        (tmp_path / "workspace" / ".env").write_text("obviously-fake-secret-value", encoding="utf-8")
        run.start()
        page = await _done(run)
        assert page["status"] == "done" and page["error"] == ""
        assert page["steps"][0]["ok"] is False
        assert page["steps"][0]["result"] in ask.prompts[1]
        assert "obviously-fake-secret-value" not in ask.prompts[1]
        assert "obviously-fake-secret-value" not in json.dumps(run.store.load_session(run.id))

    asyncio.run(scenario())


@pytest.mark.parametrize("argv", [["bash", "-c", "echo fake"], ["python", "-c", "print('fake')"]])
def test_always_allow_refuses_a_shell_or_code_and_keeps_its_approval_waiting(tmp_path, argv):
    async def scenario():
        ask = ScriptedAsk(_reply("run_command", argv=argv), _reply("finish", answer="Declined the command"))
        run = _run(tmp_path, ask)
        run.start()
        page = await _waiting(run)
        pending = page["pending"]
        with pytest.raises(ValueError):
            await run.decide(True, always=True)
        assert run.to_dict()["status"] == "waiting" and run.to_dict()["pending"] == pending
        assert run.store.is_allowed(tmp_path / "workspace", argv) is False
        assert run.store.load_session(run.id)["approvals"] == []
        await run.decide(False)
        assert (await _done(run))["status"] == "done"

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["connect", "send"])
def test_stop_during_the_default_model_call_disconnects_the_transport(tmp_path, monkeypatch, stage):
    async def scenario():
        entered, cancelled, disconnected = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def wait():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        class Transport:
            async def connect(self):
                if stage == "connect":
                    await wait()

            async def send_and_receive(self, prompt, **kwargs):
                await wait()

            async def disconnect(self):
                disconnected.set()

        monkeypatch.setattr(runner, "create_agent", lambda cfg: Transport())
        run = _run(tmp_path, None)
        run.start()
        await asyncio.wait_for(entered.wait(), timeout=2)
        page = await run.stop()
        assert cancelled.is_set() and disconnected.is_set()
        assert page["status"] == "stopped" and page["pending"] is None
        assert (await _done(run))["status"] == "stopped"
        assert run.store.load_session(run.id)["status"] == "stopped"

    asyncio.run(scenario())


def test_stop_signals_the_approved_command_to_end(tmp_path, monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        ended = threading.Event()
        loop = asyncio.get_running_loop()

        def command(argv, workspace, *, cancel, computer=False):
            loop.call_soon_threadsafe(entered.set)
            was_cancelled = cancel.wait(timeout=2)
            ended.set()
            assert was_cancelled
            return CommandResult(argv, None, "Stopped", False, False, cancelled=True)

        monkeypatch.setattr(runner, "run_command", command)
        ask = ScriptedAsk(_reply("run_command", argv=["pytest", "-q"]))
        run = _run(tmp_path, ask)
        run.start()
        await _waiting(run)
        await run.decide(True)
        await asyncio.wait_for(entered.wait(), timeout=2)
        page = await run.stop()
        assert await asyncio.to_thread(ended.wait, 2)
        assert page["status"] == "stopped" and len(ask.prompts) == 1
        assert (await _done(run))["status"] == "stopped"
        assert run.store.load_session(run.id)["status"] == "stopped"

    asyncio.run(scenario())


def test_a_run_can_be_awaited_directly(tmp_path):
    async def scenario():
        ask = ScriptedAsk(_reply("list_dir"), _reply("finish", answer="Checked"))
        run = _run(tmp_path, ask)
        page = await asyncio.wait_for(run.run(), timeout=2)
        assert page == run.to_dict() and page["status"] == "done"
        assert page["answer"] == "Checked" and len(ask.prompts) == 2
        assert run.store.load_session(run.id)["status"] == "done"

    asyncio.run(scenario())
