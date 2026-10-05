"""The app's /handoff: the plan and the run come from `handoff dispatch`, with the request on stdin."""
import json
import os
import subprocess
import sys
import textwrap

import pytest

from ixel_mat.gui import handoff
from test_gui_server import AUTH, JSON_AUTH, make_gui, run_with_client

PLAN = {"context": "", "problems": [], "steps": [
    {"agent": "gemini", "label": "Gemini", "kind": "answer", "text": "list ideas", "title": "List ideas",
     "detail": "", "note": "", "problem": ""}]}


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "shop"
    (root / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


@pytest.fixture
def fake_handoff(tmp_path, monkeypatch):
    """A stand-in `handoff` on PATH that logs its argv, stdin and folder, and answers like dispatch --json."""
    from ixel_mat.gui import handoff_api
    monkeypatch.delenv("HANDOFF_INSTALL_ROOT", raising=False)  # one exported where the tests run is a real one too
    real = handoff_api.install_roots  # HANDOFF_INSTALL_ROOT a test sets, but not a Handoff installed on this computer
    monkeypatch.setattr(handoff_api, "install_roots", lambda env=os.environ, home=None: real(
        {k: v for k, v in env.items() if k != "LOCALAPPDATA"}, tmp_path / "no-home"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "handoff.jsonl"
    script = bin_dir / "fake_handoff.py"
    script.write_text(textwrap.dedent(f'''
        import json, os, sys
        stdin = sys.stdin.buffer.read().decode("utf-8")
        with open({str(log)!r}, "a", encoding="utf-8") as f:
            f.write(json.dumps({{"argv": sys.argv[1:], "stdin": stdin, "cwd": os.getcwd(),
                                "key": os.environ.get("OPENAI_API_KEY"), "path": os.environ.get("PATH"),
                                "depth": os.environ.get("IXEL_PANEL_DEPTH")}}) + "\\n")
        if os.environ.get("FAKE_HANDOFF") == "broken":
            print("Traceback: no", file=sys.stderr)
            print("  The board is locked.", file=sys.stderr)
            sys.exit(1)
        plan = {PLAN!r}
        if "--plan" in sys.argv:
            print(json.dumps(plan))
        else:
            print(json.dumps({{**plan, "results": [{{"task": "T-1", "agent": "gemini", "ok": True,
                                                    "message": "handed to human\\x1b[31m"}}]}}))
    '''), encoding="utf-8")
    if sys.platform == "win32":
        (bin_dir / "handoff.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        wrapper = bin_dir / "handoff"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))

    def calls():
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return calls


def post(path, body):
    async def scenario(client):
        resp = await client.post(path, headers=JSON_AUTH, data=json.dumps(body))
        return resp.status, await resp.json()
    return run_with_client(make_gui()[0], scenario)


def test_plan_then_run(project, fake_handoff):
    request = "gemini list ideas for the café"  # not ASCII: arrives whole
    status, plan = post("/api/handoff/plan", {"project": str(project / "src"), "request": request})
    assert status == 200 and plan["steps"][0]["agent"] == "gemini" and plan["project"] == str(project.resolve())
    status, ran = post("/api/handoff/run", {"project": str(project), "request": request})
    assert status == 200 and ran["results"][0]["message"] == "handed to human"  # control codes stripped
    first, second = fake_handoff()
    assert first["argv"] == ["dispatch", "--json", "--project", str(project.resolve()), "--plan", "-"]
    assert second["argv"] == ["dispatch", "--json", "--project", str(project.resolve()), "--yes", "-"]
    assert first["stdin"] == request and os.path.samefile(first["cwd"], project)


def test_ixels_keys_dont_go_to_handoff(project, fake_handoff, monkeypatch):
    """Handoff runs Claude and Codex, which would bill a key from Ixel's .env instead of their subscriptions."""
    from ixel_mat.config import secrets
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-ixels-env-file")
    monkeypatch.setattr(secrets, "_INJECTED", {"OPENAI_API_KEY"})
    status, _ = post("/api/handoff/plan", {"project": str(project), "request": "gemini list ideas"})
    [call] = fake_handoff()
    assert status == 200 and call["key"] is None
    assert call["depth"] in (None, "0")  # the agents it starts may still ask an ixel panel


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in Python here is a shell script")
def test_handoff_in_its_own_folder_is_found_without_path(project, fake_handoff, tmp_path, monkeypatch):
    """An app opened from the Dock or Start menu may not have Handoff's folder on PATH: /handoff finds it
    where the installer puts it, like the Board does, and Handoff can still find `ixel` for its panel."""
    root = tmp_path / "handoff-install"
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    fake = tmp_path / "bin" / "fake_handoff.py"
    # Stands in for the install's Python: drops "-I -m handoff" and runs the fake instead
    python.write_text(f'#!/bin/sh\nshift 3\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.setenv("HANDOFF_INSTALL_ROOT", str(root))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    async def scenario(client):
        resp = await client.get("/api/handoff", headers=AUTH)
        return await resp.json()
    assert run_with_client(make_gui()[0], scenario)["installed"] is True
    status, plan = post("/api/handoff/plan", {"project": str(project), "request": "gemini list ideas"})
    [call] = fake_handoff()
    assert status == 200 and plan["steps"][0]["agent"] == "gemini"
    assert call["path"].split(os.pathsep)[-1] == os.path.dirname(sys.executable)


@pytest.mark.parametrize("body,message", [
    ({"project": "", "request": "gemini list ideas"}, "Say which project"),
    ({"project": "relative/path", "request": "gemini list ideas"}, "full path"),
    ({"project": "MISSING", "request": "gemini list ideas"}, "There's no folder"),
    ({"project": "PROJECT", "request": ""}, "Say what to hand out"),
    ({"project": "PROJECT", "request": "x" * 5000}, "longer than"),
])
def test_plan_input_is_checked(project, fake_handoff, body, message):
    # MISSING is a full path on this system (/no/such isn't one on Windows) to a folder that isn't there
    named = {"PROJECT": str(project), "MISSING": str(project.parent / "no" / "such" / "folder")}
    body = {k: named.get(v, v) for k, v in body.items()}
    status, data = post("/api/handoff/plan", body)
    assert status == 400 and message in data["error"]
    assert fake_handoff() == []


def test_a_folder_outside_git_or_with_cmd_characters_is_refused(tmp_path, fake_handoff):
    plain = tmp_path / "notes"
    plain.mkdir()
    status, data = post("/api/handoff/plan", {"project": str(plain), "request": "gemini hi"})
    assert status == 400 and "isn't in a git repository" in data["error"]
    odd = tmp_path / "a&b"
    odd.mkdir()
    status, data = post("/api/handoff/plan", {"project": str(odd), "request": "gemini hi"})
    assert status == 400 and "can't be started on a folder" in data["error"]
    assert fake_handoff() == []


def test_handoff_errors_are_passed_on(project, fake_handoff, monkeypatch):
    monkeypatch.setenv("FAKE_HANDOFF", "broken")
    status, data = post("/api/handoff/plan", {"project": str(project), "request": "gemini hi"})
    assert status == 400 and data["error"] == "The board is locked."


def test_without_handoff_installed(project, monkeypatch):
    monkeypatch.setattr(handoff, "find_handoff", lambda: None)
    # The exact line that installs Handoff on its own here: PowerShell's on Windows, the shell's elsewhere
    line = ("irm https://ixelai.com/handoff/install.ps1 | iex" if sys.platform == "win32"
            else "curl -fsSL https://ixelai.com/handoff/install.sh | sh")
    status, data = post("/api/handoff/plan", {"project": str(project), "request": "gemini hi"})
    assert status == 400 and "Handoff isn't installed" in data["error"] and data["error"].endswith(line)

    async def scenario(client):
        resp = await client.get("/api/handoff", headers=AUTH)
        return await resp.json()
    info = run_with_client(make_gui()[0], scenario)
    assert info["installed"] is False and info["install"]["command"] == line


@pytest.mark.parametrize("platform,where,line", [
    ("win32", "PowerShell", "irm https://ixelai.com/handoff/install.ps1 | iex"),
    ("darwin", "a terminal", "curl -fsSL https://ixelai.com/handoff/install.sh | sh"),
    ("linux", "a terminal", "curl -fsSL https://ixelai.com/handoff/install.sh | sh"),
])
def test_the_install_line_fits_the_system(monkeypatch, platform, where, line):
    monkeypatch.setattr(handoff.sys, "platform", platform)
    assert handoff.how_to_install() == {"where": where, "command": line}
    assert handoff.not_installed().endswith(f"run this in {where}, then open Ixel again:  {line}")


def test_handoff_needs_the_session_token(project, fake_handoff):
    async def scenario(client):
        resp = await client.post("/api/handoff/run", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"project": str(project), "request": "gemini hi"}))
        return resp.status
    assert run_with_client(make_gui()[0], scenario) == 401
    assert fake_handoff() == []
