"""Scratch experiments for the agents review (read-only review: nothing here touches the repo)."""
import asyncio, os, signal, sys, time, json
from pathlib import Path
import pytest

HERE = Path(__file__).parent
sys.path.insert(0, "/home/user/Ixel-MAT")
os.environ.setdefault("IXEL_TEST_KEYCHAIN", "memory")

from ixel_mat.agents.base import AgentConfig
from ixel_mat.agents.oneshot import OneShotAgent, _split_footer
from ixel_mat.agents import launch


def _alive(pid: int) -> bool:
    try:
        state = open(f"/proc/{pid}/status").read().split("State:")[1].split()[0]
    except FileNotFoundError:
        return False
    return state != "Z"  # a killed orphan not yet reaped by init isn't a leak


def _pids(path: Path) -> list[int]:
    for _ in range(50):
        if path.exists() and path.read_text().strip():
            return [int(p) for p in path.read_text().split()]
        time.sleep(0.05)
    raise AssertionError("the fake CLI never wrote its pids")


@pytest.fixture
def on_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", f"{HERE / 'bin'}{os.pathsep}{os.environ['PATH']}")
    pids = tmp_path / "pids"
    monkeypatch.setenv("IXEL_SCRATCH_PIDS", str(pids))
    return pids


@pytest.mark.parametrize("command,fmt", [("stubborn", "text"), ("stubborn-json", "claude-stream-json")])
def test_timeout_kills_the_cli_and_its_helper(on_path, command, fmt):
    agent = OneShotAgent(AgentConfig(name="s", label="S", type="oneshot", command=command, prompt_via="arg",
                                     stdout_format=fmt, timeout=1.5))

    async def go():
        await agent.connect()
        return await agent.send_and_receive("q")

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="timed out after 1.5s"):
        asyncio.run(go())
    took = time.monotonic() - started
    pids = _pids(on_path)
    time.sleep(0.2)
    assert not any(_alive(p) for p in pids), f"still alive: {[p for p in pids if _alive(p)]}"
    assert took < 6, took


def test_stream_json_cli_that_prints_no_events_gives_an_empty_answer_and_loses_what_it_said(on_path):
    agent = OneShotAgent(AgentConfig(name="j", label="J", type="oneshot", command="junk-json", prompt_via="arg",
                                     stdout_format="claude-stream-json", timeout=10))

    async def go():
        await agent.connect()
        return await agent.send_and_receive("q")

    assert asyncio.run(go()) == ""   # exit 0, so no error; the review then says "returned an empty answer"


def test_session_id_from_the_clis_output_can_start_with_a_dash():
    answer, session = _split_footer("fine.\nsession: --dangerously-skip-permissions")
    assert session == "--dangerously-skip-permissions"
    agent = OneShotAgent(AgentConfig(name="h", label="H", type="oneshot", command="hermes", prompt_via="flag",
                                     last_session_id=session))
    cmd, _ = agent._build_command("next question")
    assert cmd[-2:] == ["--resume", "--dangerously-skip-permissions"]


def test_a_trailing_duration_line_of_an_answer_is_dropped():
    assert _split_footer("A marathon is 42.195 km.\nDuration: about 4 hours for most runners")[0] == \
        "A marathon is 42.195 km."


def test_cancel_during_tree_kill_leaks_workdir_and_leftovers(on_path, tmp_path, monkeypatch):
    """A CLI left a detached helper holding its stdout (setsid), so the call times out and tree.kill()'s
    communicate() waits its 2 s; the user presses Stop / ctrl+c in that window."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    agent = OneShotAgent(AgentConfig(name="g", label="Gemini", type="oneshot", command="gemini", prompt_via="stdin",
                                     timeout=1.0))

    async def go():
        await agent.connect()
        task = asyncio.ensure_future(agent.send_and_receive("my secret question"))
        await asyncio.sleep(1.8)   # the 1 s timeout has fired; kill() is waiting on the pipe the helper holds
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(go())
        time.sleep(0.3)
        workdirs = list((tmp_path / "tmp").glob("ixel-agent-*"))
        chats = list(home.glob(".gemini/tmp/*/chats/*.jsonl"))
        registry = json.loads((home / ".gemini" / "projects.json").read_text())["projects"]
        print("workdirs left:", workdirs, "\nchats left:", chats, "\nregistry:", registry)
        assert workdirs == [] and chats == [] and registry == {}
    finally:
        for p in _pids(on_path):
            if _alive(p):
                os.kill(p, signal.SIGKILL)


def test_install_folders_with_an_empty_home(monkeypatch):
    monkeypatch.setenv("HOME", "")
    env = {"PATH": "/usr/bin"}
    print(launch.add_install_folders(env), env)
