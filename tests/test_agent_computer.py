"""Computer access stays off unless chosen, keeps secrets out, and asks before using the computer."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import doc_samples as samples
from ixel_mat.agent import runner, sandbox, tools
from ixel_mat.agent.protocol import ProtocolError, build_prompt, parse_reply
from ixel_mat.agent.runner import AgentRun
from ixel_mat.agent.sandbox import CommandResult
from ixel_mat.agent.store import AgentStore
from ixel_mat.agent.tools import Workspace, WorkspaceError
from ixel_mat.agents.base import AgentConfig
from ixel_mat.config import secrets as config_secrets
from ixel_mat.pictures import Picture


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake home in the test's temporary folder; no action reaches the person's home."""
    folder = tmp_path / "home" / "x"
    (folder / "workspace").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: folder))
    monkeypatch.setenv("HOME", str(folder))
    monkeypatch.setenv("USERPROFILE", str(folder))
    monkeypatch.setattr(config_secrets, "_ENV_DIR", folder / ".config" / "ixel-mat")
    return folder


def _reply(action: str, **args: Any) -> str:
    return json.dumps({"thought": "Check one thing", "action": action, "args": args})


class Asked:
    """Scripted model replies, recording each prompt and the pictures sent with it."""

    def __init__(self, *replies: str):
        self.replies = iter(replies)
        self.prompts: list[str] = []
        self.pictures: list[list[Picture]] = []

    async def __call__(self, prompt: str, pictures: list[Picture] | None = None) -> tuple[str, None]:
        self.prompts.append(prompt)
        self.pictures.append(list(pictures or []))
        return next(self.replies), None


def _run(home: Path, ask: Asked, computer: bool = False, sees: bool = True) -> AgentRun:
    cfg = AgentConfig(name="fake", label="Fake model", type="http", model="fake-model",
                      url="https://model.invalid", token="obviously-fake-token",
                      accepts=["image"] if sees else [])
    return AgentRun(cfg, home / "workspace", "Check the computer", AgentStore(home / ".config/ixel-mat/agent"),
                    ask=ask, computer=computer)


async def _waiting(run: AgentRun, n: int = 1) -> dict:
    async def wait() -> dict:
        while run.status == "running" or (run.status == "waiting" and run.pending["n"] != n):
            await asyncio.sleep(0)
        assert run.status == "waiting", run.to_dict()
        return run.to_dict()

    return await asyncio.wait_for(wait(), timeout=3)


async def _done(run: AgentRun) -> dict:
    await asyncio.wait_for(run.task, timeout=3)
    return run.to_dict()


def _fake_program(home: Path, monkeypatch: pytest.MonkeyPatch, name: str, body: str = "") -> Path:
    """A Python stand-in on PATH records argv; the short env shebang works with long interpreter paths."""
    folder = home / "bin"
    folder.mkdir(exist_ok=True)
    python = folder / "python3"
    if not python.exists():
        python.symlink_to(sys.executable)
    log = home / f"{name}.log"
    program = folder / name
    program.write_text(
        "#!/usr/bin/env python3\n"
        "import base64, json, os, stat, sys\n"
        "from pathlib import Path\n"
        f"LOG = Path({str(log)!r})\n"
        "with LOG.open('a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        + body, encoding="utf-8")
    program.chmod(0o755)
    monkeypatch.setenv("PATH", str(folder))
    return log


@pytest.mark.parametrize("path", ["/home/x/note.txt", "~/note.txt", "../note.txt"])
def test_workspace_access_is_off_by_default(home: Path, path: str) -> None:
    workspace = Workspace(home / "workspace")
    assert workspace.computer is False
    with pytest.raises(WorkspaceError):
        workspace.resolve(path)
    with pytest.raises(WorkspaceError):
        workspace.resolve(str(workspace.root / "note.txt"))


@pytest.mark.parametrize("action,args", [("screenshot", {}), ("open", {"target": "https://example.invalid"})])
def test_computer_actions_are_unknown_by_default(home: Path, action: str, args: dict) -> None:
    with pytest.raises(ProtocolError, match="unknown action"):
        parse_reply(_reply(action, **args))
    with pytest.raises(WorkspaceError, match="unknown action"):
        getattr(Workspace(home / "workspace"), action)(**args)

    async def scenario() -> None:
        ask = Asked(_reply(action, **args), _reply(action, **args))
        run = _run(home, ask)
        run.start()
        page = await _done(run)
        assert page["computer"] is False and page["status"] == "failed"
        assert "unknown action" in page["error"]
        assert page["pending"] is None and page["steps"] == []
        assert "unknown action" in ask.prompts[1]
        assert run.store.load_session(run.id)["computer"] is False

    asyncio.run(scenario())


def test_a_workspace_does_not_turn_computer_access_on_for_a_run(home: Path) -> None:
    original = Workspace(home / "workspace", computer=True)
    run = _run(home, Asked(_reply("finish", answer="Done")))
    plain = AgentRun(run.cfg, original, run.goal, run.store, ask=run._ask)
    assert plain.computer is False and plain.workspace.computer is False
    with pytest.raises(WorkspaceError):
        plain.workspace.resolve("~/note.txt")
    ordinary = Workspace(home / "workspace")
    assert AgentRun(run.cfg, ordinary, run.goal, run.store).workspace is ordinary


@pytest.mark.parametrize("path", ["~/Documents/note.txt", "../Documents/note.txt", "absolute"])
def test_computer_access_reads_and_changes_files_elsewhere_in_home(home: Path, path: str) -> None:
    note = home / "Documents/note.txt"
    note.parent.mkdir()
    # Exactly these bytes: read_file keeps a file's line endings, and write_text would write \r\n on Windows
    note.write_bytes(b"before\n")
    if path == "absolute":
        path = str(note)
    workspace = Workspace(home / "workspace", computer=True)
    assert workspace.resolve(path) == note
    assert workspace.resolve("~") == home
    assert workspace.read_file(path) == "before\n"
    pending = workspace.prepare_edit(path, "before", "after")
    assert str(note) in pending.diff
    assert note.read_text(encoding="utf-8") == "before\n"
    workspace.apply(pending)
    assert note.read_text(encoding="utf-8") == "after\n"
    new = workspace.prepare_write("~/Documents/new.txt", "new text")
    workspace.apply(new)
    assert (home / "Documents/new.txt").read_text(encoding="utf-8") == "new text"


@pytest.mark.parametrize("path", [
    "~/.ssh/config", "~/.aws/credentials", "~/.gnupg/settings", "~/Library/Keychains/login.db",
    "~/Library/Safari/history.db", "~/Library/Containers/com.apple.Safari/settings",
    "~/Library/Application Support/Google/Chrome/Default/Cookies",
    "~/Library/Application Support/Firefox/Profiles/x/cookies.sqlite",
    "~/.mozilla/firefox/x/cookies.sqlite", "~/.config/chromium/Default/Cookies",
    "~/.config/BraveSoftware/Brave-Browser/Default/Cookies", "~/.config/microsoft-edge/Default/Cookies",
    "~/.var/app/org.mozilla.firefox/settings", "~/snap/chromium/settings",
    "~/AppData/Local/Google/Chrome/User Data/Default/Cookies",
    "~/AppData/Roaming/Mozilla/Firefox/Profiles/x/cookies.sqlite",
    "~/.SSH/config", "~/Library/KEYCHAINS/login.db", "~/.config/ixel-mat/settings.txt",
    "~/Documents/.env.fake", "~/Documents/example.pem", "~/Documents/example.key",
    "~/Documents/id_rsa_fake", "~/.netrc", "~/Documents/.git/config",
])
def test_computer_access_still_refuses_secrets(home: Path, path: str) -> None:
    workspace = Workspace(home / "workspace", computer=True)
    with pytest.raises(WorkspaceError):
        workspace.read_file(path)
    with pytest.raises(WorkspaceError):
        workspace.prepare_write(path, "fake text")
    with pytest.raises(WorkspaceError):
        workspace.open_argv(path)


@pytest.mark.parametrize("path", ["/home/other/note.txt", "../../outside.txt", "~other/note.txt"])
def test_computer_access_cannot_reach_outside_home(home: Path, path: str) -> None:
    with pytest.raises(WorkspaceError):
        Workspace(home / "workspace", computer=True).resolve(path)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need extra rights on Windows")
@pytest.mark.parametrize("folder", [".ssh", ".config/chromium", ".config/ixel-mat", "outside"])
def test_symlinks_cannot_disguise_protected_folders(home: Path, folder: str) -> None:
    target = home.parent / folder if folder == "outside" else home / folder
    target.mkdir(parents=True)
    (target / "note.txt").write_text("fake private text", encoding="utf-8")
    (home / "workspace/alias").symlink_to(target, target_is_directory=True)
    workspace = Workspace(home / "workspace", computer=True)
    with pytest.raises(WorkspaceError):
        workspace.read_file("alias/note.txt")
    with pytest.raises(WorkspaceError):
        workspace.prepare_write("alias/note.txt", "replacement")


def test_config_folder_alias_is_refused_with_computer_access(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = home / ".config/ixel-mat"
    alias = home / "alias"
    alias.mkdir()
    samefile = Path.samefile

    def same_folder(path: Path, other: Path | str) -> bool:
        return True if path == alias and Path(other) == config else samefile(path, other)

    monkeypatch.setattr(Path, "samefile", same_folder)
    with pytest.raises(WorkspaceError, match="config folder"):
        Workspace(home / "workspace", computer=True).resolve("~/alias/note.txt")


def test_search_skips_secret_folders_under_home(home: Path) -> None:
    for folder in ("Documents", ".ssh", ".config/chromium", ".config/ixel-mat"):
        target = home / folder
        target.mkdir(parents=True)
        (target / "note.txt").write_text("fake match", encoding="utf-8")
    found = Workspace(home / "workspace", computer=True).search("fake match", "~")
    # A file outside the workspace is shown by its own full path: with \ between folders on Windows
    assert found == f"{home / 'Documents' / 'note.txt'}:1: fake match"


@pytest.mark.parametrize("action,args", [("screenshot", {"path": "~/note.png"}), ("open", {}),
                                         ("open", {"target": 7}), ("open", {"target": "x", "shell": True})])
def test_computer_action_arguments_are_checked(action: str, args: dict) -> None:
    with pytest.raises(ProtocolError):
        parse_reply(_reply(action, **args), computer=True)


def test_prompt_explains_the_chosen_access() -> None:
    off = build_prompt("Check files", {}, [], [])
    on = build_prompt("Check files", {}, [], [], computer=True)
    assert "- screenshot:" not in off and "- open:" not in off
    assert "Paths are relative to the workspace" in off
    assert "- screenshot:" in on and "- open:" in on
    assert "anywhere under the home folder" in on
    assert "without a sandbox and with the network" in on
    assert "command allowlist does not apply" in on
    assert parse_reply(_reply("screenshot"), computer=True).args == {}
    assert parse_reply(_reply("open", target="Fake App"), computer=True).args == {"target": "Fake App"}


def test_commands_skip_the_sandbox_but_keep_child_env_and_output_limits(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "can_sandbox", lambda: True)

    def no_profile(folder: Path) -> str:
        raise AssertionError("Computer access must not build a sandbox profile")

    monkeypatch.setattr(sandbox, "sandbox_profile", no_profile)
    monkeypatch.setenv("IXEL_FAKE_KEY", "obviously-fake-value")
    monkeypatch.setenv("IXEL_FAKE_KEEP", "kept")
    monkeypatch.setattr(config_secrets, "_INJECTED", {"IXEL_FAKE_KEY"})
    code = ("import os; from pathlib import Path; "
            "print('key present:', 'IXEL_FAKE_KEY' in os.environ); "
            "print('kept:', os.environ.get('IXEL_FAKE_KEEP')); "
            "Path('../command.txt').write_text('fake text'); print('x' * 70000)")
    result = sandbox.run_command([sys.executable, "-c", code], home / "workspace", computer=True)
    assert result.returncode == 0 and result.sandboxed is False
    assert "key present: False" in result.output and "kept: kept" in result.output
    assert "output cut at 64 KB" in result.output
    assert len(result.output.encode("utf-8")) < sandbox.MAX_OUTPUT_BYTES + 100
    assert (home / "command.txt").read_text(encoding="utf-8") == "fake text"


@pytest.mark.skipif(os.name != "posix", reason="this child-process stand-in uses a POSIX process group")
def test_unsandboxed_timeout_stops_the_process_tree(home: Path) -> None:
    child = ("import time; from pathlib import Path; Path('child-started').write_text('fake'); "
             "time.sleep(1); Path('child-survived').write_text('fake')")
    code = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
    result = sandbox.run_command([sys.executable, "-c", code], home / "workspace", timeout=0.5, computer=True)
    assert result.timed_out and result.returncode is None and not result.sandboxed
    assert (home / "workspace/child-started").exists()
    time.sleep(1.1)
    assert not (home / "workspace/child-survived").exists()


def test_stopped_computer_command_does_not_start(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cancel = threading.Event()
    cancel.set()

    def no_start(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a stopped command must not start")

    monkeypatch.setattr(sandbox.subprocess, "Popen", no_start)
    result = sandbox.run_command(["fake-program"], home / "workspace", cancel=cancel, computer=True)
    assert result.cancelled and not result.sandboxed


def test_computer_commands_ignore_the_allowlist_and_always_wait(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], bool]] = []

    def command(argv: list[str], workspace: Path, **options: Any) -> CommandResult:
        calls.append((argv, options["computer"]))
        return CommandResult(argv, 0, "fake result", False, False)

    monkeypatch.setattr(runner, "run_command", command)
    monkeypatch.setattr(runner, "can_sandbox", lambda: True)

    async def scenario() -> None:
        argv = ["fake-check", "--quick"]
        ask = Asked(_reply("run_command", argv=argv), _reply("run_command", argv=argv), _reply("finish", answer="Done"))
        run = _run(home, ask, computer=True)
        run.store.allow(run.workspace.root, argv)
        assert run.store.is_allowed(run.workspace.root, argv)

        def no_allowlist(*args: Any) -> bool:
            raise AssertionError("the allowlist must not be consulted")

        monkeypatch.setattr(run.store, "is_allowed", no_allowlist)
        run.start()
        page = await _waiting(run)
        assert page["computer"] is True and page["pending"]["sandboxed"] is False
        assert page["pending"]["argv"] == argv and calls == []
        with pytest.raises(ValueError, match="approval every time"):
            await run.decide(True, always=True)
        assert run.status == "waiting"
        await run.decide(True)
        await _waiting(run, n=2)
        assert calls == [(argv, True)]
        await run.decide(False)
        page = await _done(run)
        assert page["status"] == "done" and calls == [(argv, True)]
        saved = run.store.load_session(run.id)
        assert saved["computer"] is True
        assert [item["approve"] for item in saved["approvals"]] == [True, False]

    asyncio.run(scenario())


@pytest.mark.skipif(os.name == "nt", reason="a Python program on PATH is a POSIX stand-in")
def test_screenshot_waits_then_sends_a_clean_picture_once(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.sys, "platform", "darwin")
    data = base64.b64encode(samples.png()).decode("ascii")
    details = home / "capture.json"
    body = ("capture = Path(sys.argv[-1])\n"
            f"Path({str(details)!r}).write_text(json.dumps({{'path': str(capture), "
            "'file_mode': stat.S_IMODE(capture.stat().st_mode), "
            "'folder_mode': stat.S_IMODE(capture.parent.stat().st_mode)}))\n"
            f"capture.write_bytes(base64.b64decode({data!r}))\n")
    log = _fake_program(home, monkeypatch, "screencapture", body)

    async def scenario() -> None:
        ask = Asked(_reply("screenshot"), _reply("list_dir"), _reply("finish", answer="Looked at the screen"))
        run = _run(home, ask, computer=True)
        run.start()
        page = await _waiting(run)
        assert page["pending"]["action"] == "screenshot"
        assert "screen" in page["pending"]["diff"] and "model" in page["pending"]["diff"]
        assert not log.exists()
        await run.decide(True)
        page = await _done(run)
        assert page["status"] == "done"
        assert [len(sent) for sent in ask.pictures] == [0, 1, 0]
        picture = ask.pictures[1][0]
        assert picture.media_type == "image/png" and (picture.width, picture.height) == (64, 48)
        assert page["steps"][0]["pictures"] == 1 and page["steps"][0]["ok"] is True
        assert "screenshot" in ask.prompts[1]
        args = json.loads(log.read_text(encoding="utf-8"))
        captured = json.loads(details.read_text(encoding="utf-8"))
        assert args == ["-x", captured["path"]]
        assert captured["file_mode"] == 0o600 and captured["folder_mode"] == 0o700
        assert not Path(captured["path"]).exists() and not Path(captured["path"]).parent.exists()
        assert run.store.load_session(run.id)["steps"][0]["pictures"] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("action,args", [("screenshot", {}), ("open", {"target": "https://example.invalid"})])
@pytest.mark.parametrize("stop", [False, True])
def test_deny_or_stop_never_runs_a_computer_action(
        home: Path, monkeypatch: pytest.MonkeyPatch, action: str, args: dict, stop: bool) -> None:
    monkeypatch.setattr(tools.sys, "platform", "darwin")

    def no_action(*args: Any, **options: Any) -> None:
        raise AssertionError("the action needs approval")

    monkeypatch.setattr(Workspace, action, no_action)

    async def scenario() -> None:
        ask = Asked(_reply(action, **args), _reply("finish", answer="Done"))
        run = _run(home, ask, computer=True)
        run.start()
        await _waiting(run)
        if stop:
            await run.stop()
        else:
            await run.decide(False)
        page = await _done(run)
        assert page["status"] == ("stopped" if stop else "done")
        assert all(not sent for sent in ask.pictures)
        if not stop:
            assert page["steps"][0]["ok"] is False and "denied" in page["steps"][0]["result"]
            if action == "screenshot":
                assert page["steps"][0]["pictures"] == 0

    asyncio.run(scenario())


def test_a_model_without_vision_still_waits_but_does_not_capture(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_capture(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a model without vision should not get a screenshot")

    monkeypatch.setattr(Workspace, "screenshot", no_capture)

    async def scenario() -> None:
        ask = Asked(_reply("screenshot"), _reply("finish", answer="Could not look"))
        run = _run(home, ask, computer=True, sees=False)
        run.start()
        await _waiting(run)
        await run.decide(True)
        page = await _done(run)
        assert page["steps"][0]["pictures"] == 0 and "can't see pictures" in page["steps"][0]["result"]
        assert ask.pictures == [[], []]

    asyncio.run(scenario())


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_screenshot_is_unsupported_elsewhere(home: Path, monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(tools.sys, "platform", platform)
    viewed = Workspace(home / "workspace", computer=True).screenshot()
    assert "isn't supported" in viewed.text and viewed.pictures == []


@pytest.mark.skipif(os.name == "nt", reason="a Python program on PATH is a POSIX stand-in")
@pytest.mark.parametrize("platform,program,target", [
    ("darwin", "open", "Fake App $(touch shell-ran)"),
    ("darwin", "open", "https://example.invalid/$(touch${IFS}shell-ran)"),
    ("darwin", "open", "~/Documents/a;$(touch shell-ran).txt"),
    ("linux", "xdg-open", "https://example.invalid/$(touch${IFS}shell-ran)"),
    ("linux", "xdg-open", "~/Documents/a;$(touch shell-ran).txt"),
])
def test_open_waits_then_passes_literal_argv_without_a_shell(
        home: Path, monkeypatch: pytest.MonkeyPatch, platform: str, program: str, target: str) -> None:
    monkeypatch.setattr(tools.sys, "platform", platform)
    log = _fake_program(home, monkeypatch, program)
    if target.startswith("~/"):
        path = home / target[2:]
        path.parent.mkdir()
        path.write_text("fake file", encoding="utf-8")
    popen = sandbox.subprocess.Popen
    commands: list[list[str]] = []

    def start(argv: list[str], **options: Any) -> Any:
        assert isinstance(argv, list) and options.get("shell", False) is False
        commands.append(argv)
        return popen(argv, **options)

    monkeypatch.setattr(sandbox.subprocess, "Popen", start)

    async def scenario() -> None:
        ask = Asked(_reply("open", target=target), _reply("finish", answer="Opened"))
        run = _run(home, ask, computer=True)
        expected = run.workspace.open_argv(target)
        run.start()
        page = await _waiting(run)
        assert page["pending"]["argv"] == expected and target in page["pending"]["diff"]
        assert not log.exists() and commands == []
        await run.decide(True)
        page = await _done(run)
        assert page["status"] == "done" and page["steps"][0]["ok"] is True
        assert commands == [expected]
        assert json.loads(log.read_text(encoding="utf-8")) == expected[1:]
        assert not (home / "workspace/shell-ran").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("target", ["http://example.invalid", "file:///home/x/note.txt", "https://", "-a",
                                    "https://example.invalid/a b", "https://[invalid", "fake\x00app"])
def test_open_refuses_invalid_targets(home: Path, monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    monkeypatch.setattr(tools.sys, "platform", "darwin")
    with pytest.raises(WorkspaceError):
        Workspace(home / "workspace", computer=True).open_argv(target)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need extra rights on Windows")
def test_open_rechecks_the_path_after_approval(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.sys, "platform", "darwin")
    workspace = Workspace(home / "workspace", computer=True)
    for name in ("a.txt", "b.txt"):
        (home / name).write_text("fake text", encoding="utf-8")
    alias = workspace.root / "alias.txt"
    alias.symlink_to(home / "a.txt")
    shown = workspace.open_argv("alias.txt")
    alias.unlink()
    alias.symlink_to(home / "b.txt")
    with pytest.raises(WorkspaceError, match="target changed"):
        workspace.open("alias.txt", expected_argv=shown)


def test_windows_open_uses_the_direct_start_equivalent_without_a_shell(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.sys, "platform", "win32")
    opened: list[str] = []
    monkeypatch.setattr(tools.os, "startfile", opened.append, raising=False)

    def no_process(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("start must not run through cmd.exe")

    monkeypatch.setattr(sandbox.subprocess, "Popen", no_process)
    workspace = Workspace(home / "workspace", computer=True)
    target = "https://example.invalid/fake&literal"
    assert workspace.open_argv(target) == ["start", target]
    result = workspace.open(target)
    assert result.returncode == 0 and opened == [target]
