"""The Board's answer and review runs carry the effort picked for the worker, and nothing when Default is picked."""
import pytest
from test_gui_board import fake, gui_with, post, project  # noqa: F401


@pytest.mark.parametrize("kind", ["answer", "review"])
def test_an_answer_or_review_sends_the_effort_picked(project, fake, kind):  # noqa: F811
    fake.answer("approve", {"data": {"task": {"ref": "T-1"}}})
    args = {"task": "T-1", "agent": "claude", "kind": kind, "efforts": {"": "high"}}
    status, data = post(gui_with(fake.command), {"project": str(project), "op": "approve", "args": args})
    assert status == 200 and data == {"task": {"ref": "T-1"}}
    call = fake.calls()[-1]["request"]
    assert call["op"] == "approve" and call["args"] == args


@pytest.mark.parametrize("kind", ["answer", "review"])
def test_an_answer_or_review_on_default_sends_no_effort(project, fake, kind):  # noqa: F811
    fake.answer("approve", {"data": {"task": {"ref": "T-1"}}})
    args = {"task": "T-1", "agent": "claude", "kind": kind}
    status, _ = post(gui_with(fake.command), {"project": str(project), "op": "approve", "args": args})
    assert status == 200
    sent = fake.calls()[-1]["request"]["args"]
    assert sent == args and "efforts" not in sent
