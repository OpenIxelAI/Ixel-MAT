"""Agent card writes go only through the optional add-ons API, after approval."""
from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from ixel_mat.agent import cards
from ixel_mat.agent.runner import AgentRun
from ixel_mat.agent.store import AgentStore
from ixel_mat.agents.base import AgentConfig

COMMIT = "ab" * 20
TREE = "cd" * 32
TEXT = f'''id = "example-tool"
name = "Example tool"
summary = "A fake add-on"
by = "Example author"
license = "MIT"
homepage = "https://github.com/example-owner/example-tool"
kind = "mcp"
[source]
repo = "https://github.com/example-owner/example-tool"
commit = "{COMMIT}"
subdir = ""
tree_sha256 = "{TREE}"
files = 1
[permissions]
files = "output-folder"
network = false
commands = [["python", "{{addon}}/server.py", "{{output}}"]]
[use]
where = "agent"
output = "text"
'''
FETCHED = {"example-owner-example-tool": {"repo_url": "https://github.com/example-owner/example-tool",
                                         "commit": COMMIT, "hashes": {"": (TREE, 1)}}}


@pytest.fixture
def api(tmp_path, monkeypatch):
    module = ModuleType("ixel_mat.addons")
    checked = SimpleNamespace(id="example-tool", name="Example tool", kind="mcp", builtin=False,
                              source=SimpleNamespace(repo="https://github.com/example-owner/example-tool",
                                                     commit=COMMIT, subdir="", tree_sha256=TREE, files=1))
    events = []
    known = []

    def validate(text):
        events.append(("validate", text))
        return checked

    def write(text):
        events.append(("write", text))
        return checked

    module.validate_card_text = validate
    module.write_agent_card = write
    module.all_cards = lambda: known
    monkeypatch.setitem(sys.modules, "ixel_mat.addons", module)
    # The canonical format must reach its own validator, rather than the earlier agent-only schema.
    monkeypatch.setattr(cards, "validate_card", lambda *args: pytest.fail("used the old validator"))
    return SimpleNamespace(module=module, checked=checked, known=known, events=events)


def _run(tmp_path) -> AgentRun:
    folder = tmp_path / "workspace"
    folder.mkdir()
    cfg = AgentConfig(name="fake", label="Fake model", type="http", model="fake",
                      url="https://model.invalid", token="obviously-fake-token")
    answers = iter([{"action": "write_addon_card", "args": {"id": "example-tool", "card": TEXT}},
                    {"action": "finish", "args": {"answer": "Open Settings › Add-ons to review it."}}])
    async def ask(prompt):
        return json.dumps(dict(thought="A fake card", **next(answers))), None
    run = AgentRun(cfg, folder, "Add a fake tool", AgentStore(tmp_path / "agent"), ask=ask,
                   addons_dir=tmp_path / "addons", builtin_dir=tmp_path / "builtin")
    run._fetched = FETCHED
    return run


@pytest.mark.parametrize("approve", [False, True])
def test_canonical_card_waits_then_only_calls_the_api_writer(tmp_path, api, approve):
    async def scenario():
        run = _run(tmp_path)
        run.start()
        async def wait():
            while run.status == "running":
                await asyncio.sleep(0.01)
            assert run.status == "waiting", run.to_dict()
        await asyncio.wait_for(wait(), 2)
        assert TEXT in run.pending["diff"]
        assert [kind for kind, _ in api.events] == ["validate"]
        await run.decide(approve)
        page = await asyncio.wait_for(run.task, 2)
        assert page["status"] == "done"
        assert page["steps"][0]["ok"] is approve
        assert [kind for kind, _ in api.events] == (["validate", "validate", "write"] if approve else ["validate"])
        assert all(text == TEXT for _, text in api.events)
        assert not (tmp_path / "addons").exists()  # the fake API recorded it; the agent wrote no files
    asyncio.run(scenario())


def test_canonical_validator_error_is_reported_before_approval(tmp_path, api):
    def invalid(text):
        raise ValueError("permissions.commands: fake invalid card")
    api.module.validate_card_text = invalid
    async def scenario():
        run = _run(tmp_path)
        page = await run.run()
        assert not page["steps"][0]["ok"]
        assert "permissions.commands" in page["steps"][0]["result"]
        assert run.store.load_session(run.id)["approvals"] == []
        assert api.events == []
    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["builtin", "source", "id"])
def test_builtin_id_and_source_provenance_are_checked(tmp_path, api, fault):
    if fault == "builtin":
        api.known.append(SimpleNamespace(id="example-tool", builtin=True))
    elif fault == "source":
        api.checked.source.commit = "ef" * 20
    else:
        api.checked.id = "other-tool"
    async def scenario():
        run = _run(tmp_path)
        page = await run.run()
        assert not page["steps"][0]["ok"]
        assert run.store.load_session(run.id)["approvals"] == []
        assert not any(kind == "write" for kind, _ in api.events)
    asyncio.run(scenario())


@pytest.mark.parametrize("missing", ["module", "validate_card_text", "write_agent_card"])
def test_missing_api_reports_unavailable_and_never_writes(tmp_path, monkeypatch, missing):
    if missing == "module":
        monkeypatch.setitem(sys.modules, "ixel_mat.addons", None)
    else:
        module = ModuleType("ixel_mat.addons")
        module.validate_card_text = lambda text: pytest.fail("should report unavailable first")
        module.write_agent_card = lambda text: pytest.fail("must not write")
        module.all_cards = lambda: []
        delattr(module, missing)
        monkeypatch.setitem(sys.modules, "ixel_mat.addons", module)
    async def scenario():
        run = _run(tmp_path)
        page = await run.run()
        assert page["status"] == "done" and not page["steps"][0]["ok"]
        assert "aren't available yet" in page["steps"][0]["result"]
        assert run.store.load_session(run.id)["approvals"] == []
        assert not (tmp_path / "addons").exists()
    asyncio.run(scenario())
