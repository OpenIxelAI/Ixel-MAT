"""The agent's speed and loop guards: reads together, forgiven slips, repeats, steps left, time and tokens."""
from __future__ import annotations

import asyncio
import json
import re
import threading
from pathlib import Path

import pytest

from ixel_mat.agent.protocol import MAX_READS, Action, ProtocolError, Reads, build_prompt, parse_reply
from ixel_mat.agent.runner import AgentRun
from ixel_mat.agent.store import AgentStore
from ixel_mat.agent.tools import Workspace
from ixel_mat.agents.base import AgentConfig

FENCE = "IXEL-abcdef123456"
GOAL = "Check the project"
USAGE = {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 0, "cache_write_tokens": 0,
         "cost_usd": 0.0}


def _reply(action: str, **args) -> str:
    return json.dumps({"thought": "Check one thing", "action": action, "args": args})


def _reads(*items: tuple[str, dict]) -> str:
    """A reads reply: each (action, args) pair is one read, and they are all sent together."""
    return json.dumps({"thought": "Look at several things",
                       "reads": [{"action": name, "args": args} for name, args in items]})


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
    return AgentRun(_cfg(), workspace, GOAL, AgentStore(tmp_path / "agent"), max_steps=max_steps, ask=ask)


async def _done(run: AgentRun) -> dict:
    await asyncio.wait_for(run.task, timeout=2)
    return run.to_dict()


# ── Reading a reply with several reads ──────────────────────────────────────

def test_a_reads_reply_parses_to_each_read_with_the_thought():
    parsed = parse_reply(_reads(("read_file", {"path": "a.txt"}), ("list_dir", {}),
                                ("search", {"pattern": "keep"})))
    assert isinstance(parsed, Reads)
    assert parsed.thought == "Look at several things"
    assert parsed.actions == (
        Action("Look at several things", "read_file", {"path": "a.txt"}),
        Action("Look at several things", "list_dir", {}),
        Action("Look at several things", "search", {"pattern": "keep"}),
    )


@pytest.mark.parametrize("name,args", [("write_file", {"path": "a.txt", "content": "x"}),
                                       ("run_command", {"argv": ["pytest", "-q"]})])
def test_a_write_or_command_in_reads_is_refused_by_name(name, args):
    with pytest.raises(ProtocolError, match=f'"{name}"'):
        parse_reply(_reads((name, args)))


def test_more_than_five_reads_is_refused():
    with pytest.raises(ProtocolError, match=f"has {MAX_READS + 1} actions"):
        parse_reply(_reads(*[("list_dir", {})] * (MAX_READS + 1)))


def test_an_empty_reads_list_is_refused():
    with pytest.raises(ProtocolError, match="is empty"):
        parse_reply(json.dumps({"thought": "t", "reads": []}))


def test_action_and_reads_together_are_refused():
    text = json.dumps({"thought": "t", "action": "list_dir", "args": {},
                       "reads": [{"action": "list_dir", "args": {}}]})
    with pytest.raises(ProtocolError, match='both "action" and "reads"'):
        parse_reply(text)


# ── Repairs before a reply counts as bad ────────────────────────────────────

def test_arguments_beside_action_are_moved_under_args():
    parsed = parse_reply('{"thought": "t", "action": "read_file", "path": "a.txt"}')
    assert parsed == Action("t", "read_file", {"path": "a.txt"})


def test_a_trailing_comma_is_removed():
    parsed = parse_reply('{"thought": "t", "action": "list_dir", "args": {},}')
    assert parsed == Action("t", "list_dir", {})


def test_a_missing_thought_is_empty():
    assert parse_reply('{"action": "list_dir", "args": {}}').thought == ""


def test_a_comma_inside_a_string_is_left_alone():
    text = '{"thought": "keep ,} here", "action": "finish", "args": {"answer": "x,}"},}'
    assert parse_reply(text) == Action("keep ,} here", "finish", {"answer": "x,}"})


# ── The prompt: fixed first, steps left at the end ─────────────────────────

FILES = {"soul": "SOUL-MARKER: be brief", "user": "", "memory": ""}
SKILLS = [("notes", "Plain notes")]
UPLOADS = [{"name": "upload-marker.txt", "kind": "other", "size": 3}]
STEP = {"n": 1, "thought": "Look first", "action": "list_dir", "args": {}, "ok": True,
        "result": "RESULT-MARKER"}


def _prompt(steps: list[dict], **kwargs) -> str:
    return build_prompt(GOAL, FILES, SKILLS, steps, fence=FENCE, uploads=UPLOADS, **kwargs)


def test_the_prompt_is_fixed_first_and_ordered_rules_persona_skills_uploads_goal_steps():
    empty = _prompt([])
    later = _prompt([STEP])
    cut = empty.index("The goal:")
    assert later[:cut] == empty[:cut]
    markers = ["You are Ixel Agent.", "SOUL-MARKER", "Skills you can load with load_skill",
               "upload-marker.txt", "The goal:", "Steps so far"]
    positions = [later.index(marker) for marker in markers]
    assert positions == sorted(positions) and len(set(positions)) == len(positions)


def test_steps_left_is_added_after_the_steps_only_from_three_down():
    line = "2 steps left: finish with what you have"
    assert "steps left" not in _prompt([STEP])
    assert "steps left" not in _prompt([STEP], steps_left=5)
    two = _prompt([STEP], steps_left=2)
    assert two.count(line) == 1 and two.index(line) > two.index("RESULT-MARKER")


# ── A run: reads together, forgiving replies, loops, steps left, time, saving ─

def test_reads_in_one_reply_run_together_and_are_recorded_as_a_batch(tmp_path, monkeypatch):
    original = Workspace.read_file
    # Both read_file calls must be in flight at once; if they ran one after the other, this would time out
    barrier = threading.Barrier(2, timeout=2)

    def read_file(self, *args, **kwargs):
        barrier.wait()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Workspace, "read_file", read_file)

    async def scenario():
        ask = ScriptedAsk(_reads(("read_file", {"path": "a.txt"}), ("read_file", {"path": "b.txt"}),
                                 ("list_dir", {})),
                          _reply("finish", answer="Read both"))
        run = _run(tmp_path, ask)
        (tmp_path / "workspace" / "a.txt").write_text("alpha\n", encoding="utf-8")
        (tmp_path / "workspace" / "b.txt").write_text("beta\n", encoding="utf-8")
        run.start()
        page = await _done(run)
        assert page["status"] == "done" and len(ask.prompts) == 2
        steps = page["steps"]
        assert [step["n"] for step in steps] == [1, 2, 3, 4]
        assert [step["action"] for step in steps] == ["read_file", "read_file", "list_dir", "finish"]
        assert [step.get("batch") for step in steps[:3]] == [1, 1, 1] and "batch" not in steps[3]
        assert all(step["ok"] for step in steps[:3])
        assert "alpha" in steps[0]["result"] and "beta" in steps[1]["result"]

    asyncio.run(scenario())


def test_the_fence_is_the_same_on_every_call_of_a_run(tmp_path):
    async def scenario():
        ask = ScriptedAsk(_reply("list_dir"), _reply("finish", answer="Checked"))
        run = _run(tmp_path, ask)
        run.start()
        await _done(run)
        markers = [set(re.findall(r"IXEL-[0-9a-f]{12}", prompt)) for prompt in ask.prompts]
        assert len(markers[0]) == 1 and markers[0] == markers[1]

    asyncio.run(scenario())


def test_a_good_reply_resets_the_bad_reply_count(tmp_path):
    async def scenario():
        ask = ScriptedAsk("not an action", _reply("list_dir"), "not an action again",
                          _reply("finish", answer="Done"))
        run = _run(tmp_path, ask)
        run.start()
        page = await _done(run)
        assert page["status"] == "done" and len(ask.prompts) == 4

    asyncio.run(scenario())


def test_two_bad_replies_in_a_row_still_fail(tmp_path):
    async def scenario():
        ask = ScriptedAsk("not JSON", "still not JSON", _reply("finish", answer="Never asked"))
        run = _run(tmp_path, ask)
        run.start()
        page = await _done(run)
        assert page["status"] == "failed" and "second bad reply" in page["error"]
        assert len(ask.prompts) == 2

    asyncio.run(scenario())


def test_a_long_bad_reply_is_echoed_only_in_part(tmp_path):
    async def scenario():
        bad = "Q" * 10_000
        ask = ScriptedAsk(bad, _reply("finish", answer="Done"))
        run = _run(tmp_path, ask)
        run.start()
        page = await _done(run)
        assert page["status"] == "done"
        assert "Q" * 10_000 not in ask.prompts[1]
        assert "Q" * 2_000 in ask.prompts[1] and "Q" * 2_049 not in ask.prompts[1]

    asyncio.run(scenario())


def test_the_same_read_three_times_is_warned_then_stopped_on_the_fourth(tmp_path):
    async def scenario():
        same = _reply("read_file", path="a.txt")
        ask = ScriptedAsk(same, same, same, same)
        run = _run(tmp_path, ask)
        (tmp_path / "workspace" / "a.txt").write_text("alpha\n", encoding="utf-8")
        run.start()
        page = await _done(run)
        assert "asked for the same thing (read_file) 3 times in a row" in ask.prompts[3]
        assert page["status"] == "failed" and "kept repeating" in page["error"]
        assert len(page["steps"]) == 2 and len(ask.prompts) == 4

    asyncio.run(scenario())


def test_two_reads_going_back_and_forth_are_warned_then_stopped(tmp_path):
    async def scenario():
        first = _reply("read_file", path="a.txt")
        second = _reply("read_file", path="b.txt")
        ask = ScriptedAsk(first, second, first, second, first)
        run = _run(tmp_path, ask)
        (tmp_path / "workspace" / "a.txt").write_text("alpha\n", encoding="utf-8")
        (tmp_path / "workspace" / "b.txt").write_text("beta\n", encoding="utf-8")
        run.start()
        page = await _done(run)
        assert "back and forth" in ask.prompts[4]
        assert page["status"] == "failed" and "kept repeating" in page["error"]
        assert len(page["steps"]) == 3 and len(ask.prompts) == 5

    asyncio.run(scenario())


def test_a_run_that_is_warned_and_then_does_something_else_is_done(tmp_path):
    async def scenario():
        same = _reply("read_file", path="a.txt")
        ask = ScriptedAsk(same, same, same, _reply("list_dir"), _reply("finish", answer="Checked"))
        run = _run(tmp_path, ask)
        (tmp_path / "workspace" / "a.txt").write_text("alpha\n", encoding="utf-8")
        run.start()
        page = await _done(run)
        assert page["status"] == "done"
        assert "asked for the same thing" in ask.prompts[3]
        assert [step["action"] for step in page["steps"]] == ["read_file", "read_file", "list_dir", "finish"]

    asyncio.run(scenario())


def test_the_prompt_says_how_many_steps_are_left_from_three_down(tmp_path):
    async def scenario():
        ask = ScriptedAsk(_reply("list_dir", path="one"), _reply("list_dir", path="two"),
                          _reply("list_dir", path="three"), _reply("finish", answer="Looked"))
        run = _run(tmp_path, ask, max_steps=4)
        for name in ("one", "two", "three"):
            (tmp_path / "workspace" / name).mkdir()
        run.start()
        page = await _done(run)
        assert page["status"] == "done" and len(page["steps"]) == 4
        assert "steps left" not in ask.prompts[0]
        assert "3 steps left: finish with what you have" in ask.prompts[1]
        assert "1 step left: finish with what you have" in ask.prompts[3]

    asyncio.run(scenario())


def test_each_step_has_its_time_and_tokens_and_the_run_totals_them(tmp_path):
    async def scenario():
        ask = ScriptedAsk((_reply("list_dir"), USAGE), (_reply("finish", answer="Looked"), USAGE))
        run = _run(tmp_path, ask)
        run.start()
        page = await _done(run)
        assert page["status"] == "done"
        for step in page["steps"]:
            assert isinstance(step["ms"], int) and step["ms"] >= 0
            assert step["tokens"] == {"input": 100, "output": 20}
        usage = page["usage"]
        assert usage["input_tokens"] == 200 and usage["output_tokens"] == 40
        assert isinstance(usage["ms"], int) and usage["ms"] >= 0
        assert usage["ms"] == sum(step["ms"] for step in page["steps"])
        assert run.store.load_session(run.id)["usage"] == usage

    asyncio.run(scenario())


def test_in_a_batch_only_the_first_read_step_carries_the_calls_tokens(tmp_path):
    async def scenario():
        ask = ScriptedAsk((_reads(("read_file", {"path": "a.txt"}), ("list_dir", {})), USAGE),
                          (_reply("finish", answer="Looked"), USAGE))
        run = _run(tmp_path, ask)
        (tmp_path / "workspace" / "a.txt").write_text("alpha\n", encoding="utf-8")
        run.start()
        page = await _done(run)
        steps = page["steps"]
        assert [step["tokens"] for step in steps] == [{"input": 100, "output": 20},
                                                      {"input": 0, "output": 0},
                                                      {"input": 100, "output": 20}]
        assert all(isinstance(step["ms"], int) and step["ms"] >= 0 for step in steps)
        assert page["usage"]["input_tokens"] == 200 and page["usage"]["output_tokens"] == 40

    asyncio.run(scenario())


def test_each_save_happens_off_the_main_thread_and_the_saved_run_is_complete(tmp_path, monkeypatch):
    async def scenario():
        ask = ScriptedAsk(_reply("list_dir"), _reply("finish", answer="Looked"))
        run = _run(tmp_path, ask)
        on_main: list[bool] = []
        original = run.store.save_session

        def recording(data: dict) -> None:
            on_main.append(threading.current_thread() is threading.main_thread())
            original(data)

        monkeypatch.setattr(run.store, "save_session", recording)
        run.start()
        page = await _done(run)
        assert page["status"] == "done"
        assert on_main == [False, False]  # once for the list_dir step, once for finish
        saved = run.store.load_session(run.id)
        assert saved["status"] == "done"
        assert [step["action"] for step in saved["steps"]] == ["list_dir", "finish"]

    asyncio.run(scenario())
