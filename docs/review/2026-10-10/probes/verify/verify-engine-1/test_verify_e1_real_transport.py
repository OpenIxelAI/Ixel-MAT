"""verify-engine-1 / E1 reachability: a OneShotAgent whose CLI isn't installed passes connect() (runtime's
connect_agents keeps it on the panel) and fails instantly in the answer round, so with two of them the one
healthy model is cut off after SLOWEST_WAIT_MIN."""
import asyncio
import random
import sys

sys.path.insert(0, "/home/user/Ixel-MAT/tests")

from ixel_mat.agents.base import AgentConfig  # noqa: E402
from ixel_mat.agents.oneshot import OneShotAgent  # noqa: E402
from ixel_mat.modes import review as engine  # noqa: E402
from ixel_mat.modes.review import run_review  # noqa: E402
from ixel_mat.runtime import connect_agents  # noqa: E402
from test_review import PanelAgent, QUESTION  # noqa: E402


def _missing_cli(name):
    return AgentConfig(name=name, label=name.title(), type="oneshot",
                       command="/nonexistent/ixel-no-such-cli", workdir="temp", prompt_via="stdin")


def test_missing_cli_passes_connect_and_fails_instantly(monkeypatch):
    monkeypatch.setattr(engine, "SLOWEST_WAIT_MIN", 0.5)  # only to keep the test short; same logic at 30s

    async def go():
        broken = {"x": _missing_cli("x"), "y": _missing_cli("y")}
        connected = await connect_agents(broken, on_result=lambda *a: print("connect result:", a))
        print("\nconnected after connect_agents:", sorted(connected), [type(a).__name__ for a in connected.values()])
        assert sorted(connected) == ["x", "y"]  # connect() doesn't look for the program
        healthy = PanelAgent("c", "17 × 23 = 391", delay=2)
        return await run_review(QUESTION, [connected["x"], connected["y"], healthy], mode="quick",
                                rng=random.Random(1))

    result = asyncio.run(go())
    print("failures:", [(f.agent, f.round, f.error) for f in result.failures])
    print("error:", result.error, "| final:", result.final)
    errors = {f.agent: f.error for f in result.failures}
    assert errors["x"].startswith("Command not found") and errors["y"].startswith("Command not found")
    assert errors["c"].startswith("left out: still working")
    assert result.final is None and result.error == "No agent produced an answer."
