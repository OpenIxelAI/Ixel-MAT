"""
The security review fixes for the agent (ixel_mat/agent/sandbox.py, addons.py and tools.py): the keychain and Apple
Events out of reach, the ssh-agent socket never shared with a command, more secret files, an add-on's reads under
files="none", a secret across the output cut masked, read_file refusing a file that isn't text past line 400, and
the heavy build folders skipped only at the top of the workspace.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ixel_mat.agent import sandbox
from ixel_mat.agent.sandbox import run_command
from ixel_mat.agent.tools import Workspace, WorkspaceError
from ixel_mat.config import secrets as config_secrets

darwin = pytest.mark.skipif(sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"),
                            reason="sandbox-exec is macOS only")
MARKER = re.compile(r"\[output cut at 64 KB: (\d+) bytes left out here, of (\d+) in all\]")
FAKE_KEY = "sk-" + "FAKE" * 7


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


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(root)


def _write(ws: Workspace, rel: str, text: str) -> None:
    path = ws.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exactly these bytes: read_file keeps a file's line endings, and write_text would write \r\n on Windows
    path.write_bytes(text.encode("utf-8"))


def _kept(data: bytes) -> str:
    """The text a command's output comes back as, once all of data has been read through _Output."""
    output = sandbox._Output(io.BufferedReader(io.BytesIO(data)))
    output.thread.join(timeout=5)
    assert not output.thread.is_alive()
    return output.text()


# ── Keychain, Apple Events and the ssh-agent socket in the profile ───────────

@pytest.mark.parametrize("temporary", [True, False])
@pytest.mark.parametrize("network", [False, True])
@pytest.mark.parametrize("files", ["none", "output-folder", "chosen-folder"])
def test_every_profile_denies_the_keychain_services_and_apple_events(
        home: Path, monkeypatch: pytest.MonkeyPatch, files: str, network: bool, temporary: bool) -> None:
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    profile = sandbox.sandbox_profile(home / "workspace", network=network, files=files, temporary=temporary)
    assert "(deny mach-lookup " in profile
    for name in sandbox.KEYCHAIN_MACH_SERVICES:
        assert f"(global-name {sandbox._quoted(name)})" in profile
    assert "com.apple.SecurityServer" in sandbox.KEYCHAIN_MACH_SERVICES
    assert "com.apple.securityd" in sandbox.KEYCHAIN_MACH_SERVICES
    assert "com.apple.trustd" not in profile
    assert profile.endswith("(deny appleevent-send)")


@pytest.mark.parametrize("network", [False, True])
def test_the_ssh_agent_socket_is_denied_when_one_is_set(
        home: Path, monkeypatch: pytest.MonkeyPatch, network: bool) -> None:
    agent = home / "agent.sock"
    monkeypatch.setenv("SSH_AUTH_SOCK", str(agent))
    profile = sandbox.sandbox_profile(home / "workspace", network=network)
    quoted = sandbox._quoted(os.path.realpath(agent))
    deny = f"(deny file-read* file-write* (literal {quoted}))"
    remote = f"(deny network-outbound (remote unix-socket (path-literal {quoted})))"
    assert deny in profile and remote in profile
    if "(allow file-read*" in profile:
        assert profile.index(deny) > profile.rindex("(allow file-read*")


def test_no_ssh_agent_rules_without_a_socket(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for value in (None, ""):
        if value is None:
            monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
        else:
            monkeypatch.setenv("SSH_AUTH_SOCK", value)
        assert "unix-socket" not in sandbox.sandbox_profile(home / "workspace")


# ── The child environment never gets the ssh-agent socket ────────────────────

def test_computer_commands_never_get_the_ssh_agent_socket(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", str(home / "agent.sock"))
    code = "import os; print('sock:', os.environ.get('SSH_AUTH_SOCK'))"
    result = run_command([sys.executable, "-c", code], home / "workspace", computer=True)
    assert result.sandboxed is False
    assert "sock: None" in result.output


@darwin
def test_sandboxed_commands_never_get_the_ssh_agent_socket(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", str(home / "agent.sock"))
    code = "import os; print('sock:', os.environ.get('SSH_AUTH_SOCK'))"
    result = run_command([sys.executable, "-c", code], home / "workspace")
    assert result.sandboxed is True
    assert "sock: None" in result.output


def test_addon_environment_never_gets_the_ssh_agent_socket(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mcp")
    from ixel_mat.agent import addons

    monkeypatch.setattr(sandbox, "can_sandbox", lambda: False)
    monkeypatch.setenv("SSH_AUTH_SOCK", str(home / "agent.sock"))
    card = SimpleNamespace(id="fake-mcp", name="Fake MCP", kind="mcp", dir=home / "addon", enabled=True,
                           permissions=SimpleNamespace(files="output-folder", network=False,
                                                       commands=[["fake-server"]]))
    params = addons.server_parameters(card, home / "workspace")
    assert "SSH_AUTH_SOCK" not in params.env


# ── More secret files and folders ────────────────────────────────────────────

@pytest.mark.parametrize("entry", [".netrc", ".npmrc", ".pypirc", ".git-credentials", ".config/gh", ".kube",
                                   ".docker/config.json", ".config/gcloud", ".azure"])
def test_more_secret_files_and_folders_are_denied(home: Path, monkeypatch: pytest.MonkeyPatch, entry: str) -> None:
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    assert entry in sandbox.SECRET_HOME_FOLDERS
    profile = sandbox.sandbox_profile(home / "workspace")
    assert f"(subpath {sandbox._quoted(os.path.realpath(home / entry))})" in profile


def test_top_level_env_files_are_secret_but_not_deeper_ones(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    for name in (".env", ".env.local", ".ENV.prod"):
        (home / name).write_text("FAKE=fake", encoding="utf-8")
    (home / "workspace" / ".env").write_text("FAKE=fake", encoding="utf-8")
    secret = sandbox.secret_folders()
    for name in (".env", ".env.local", ".ENV.prod"):
        assert os.path.realpath(home / name) in secret
        assert f"(subpath {sandbox._quoted(os.path.realpath(home / name))})" in sandbox.sandbox_profile(
            home / "workspace")
    assert os.path.realpath(home / "workspace" / ".env") not in secret


def test_computer_access_refuses_the_new_secret_files(home: Path) -> None:
    (home / ".git-credentials").write_text("fake credentials", encoding="utf-8")
    (home / ".docker").mkdir()
    (home / ".docker" / "config.json").write_text("{\"fake\": true}", encoding="utf-8")
    (home / ".kube").mkdir()
    (home / ".kube" / "config").write_text("fake kube config", encoding="utf-8")
    workspace = Workspace(home / "workspace", computer=True)
    for path in ("~/.git-credentials", "~/.docker/config.json", "~/.kube/config"):
        with pytest.raises(WorkspaceError):
            workspace.read_file(path)


# ── files="none" reads ───────────────────────────────────────────────────────

def test_files_none_denies_the_home_folder_then_allows_only_the_addon_folder(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    addon = home / ".ixel" / "addons" / "fake" / "files"
    addon.mkdir(parents=True)
    profile = sandbox.sandbox_profile(home / "workspace", files="none", addon_files=addon)
    home_deny = f"(deny file-read* (subpath {sandbox._quoted(os.path.realpath(home))}))"
    allow = f"(allow file-read* (subpath {sandbox._quoted(os.path.realpath(addon))}))"
    secret = f"(subpath {sandbox._quoted(os.path.realpath(home / '.ssh'))})"
    assert home_deny in profile and allow in profile and secret in profile
    assert profile.index(home_deny) < profile.index(allow) < profile.index(secret)
    for system in ("/usr", "/System", "/etc"):
        assert f"(subpath {sandbox._quoted(system)})" not in profile


@pytest.mark.parametrize("files", ["chosen-folder", "output-folder"])
def test_other_file_modes_have_no_home_folder_deny(home: Path, monkeypatch: pytest.MonkeyPatch, files: str) -> None:
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    addon = home / ".ixel" / "addons" / "fake" / "files"
    addon.mkdir(parents=True)
    home_deny = f"(deny file-read* (subpath {sandbox._quoted(os.path.realpath(home))}))"
    assert home_deny not in sandbox.sandbox_profile(home / "workspace", files=files, addon_files=addon)


def test_files_none_without_an_addon_folder_allows_no_reads(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    assert "(allow file-read*" not in sandbox.sandbox_profile(home / "workspace", files="none")


@darwin
@pytest.mark.parametrize("files", ["none", "output-folder", "chosen-folder"])
def test_profiles_are_accepted_by_sandbox_exec(home: Path, monkeypatch: pytest.MonkeyPatch, files: str) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", str(home / "agent.sock"))
    profile = sandbox.sandbox_profile(home / "workspace", files=files)
    done = subprocess.run([sandbox.SANDBOX_EXEC, "-p", profile, "/usr/bin/true"], cwd="/",
                          capture_output=True, timeout=30)
    assert done.returncode == 0, done.stderr


@darwin
def test_files_none_reads_the_addon_folder_and_system_folders_only(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", str(home / "agent.sock"))
    addon = home / ".ixel" / "addons" / "fake" / "files"
    addon.mkdir(parents=True)
    (addon / "data.txt").write_text("fake addon text", encoding="utf-8")
    note = home / "Documents" / "note.txt"
    note.parent.mkdir()
    note.write_text("fake note text", encoding="utf-8")
    profile = sandbox.sandbox_profile(home / "workspace", files="none", addon_files=addon)

    def cat(path: Path | str) -> subprocess.CompletedProcess:
        return subprocess.run([sandbox.SANDBOX_EXEC, "-p", profile, "/bin/cat", str(path)], cwd="/",
                              capture_output=True, text=True, timeout=30)

    inside = cat(addon / "data.txt")
    assert inside.returncode == 0 and "fake addon text" in inside.stdout
    outside = cat(note)
    assert outside.returncode != 0 and "fake note text" not in outside.stdout + outside.stderr
    system = cat("/etc/hosts")
    assert system.returncode == 0


# ── Masking before the output cut ────────────────────────────────────────────

# A key is only masked after a separator, so each one here has a space before it. _Output keeps 2 * (32 KB + a 4 KB
# margin) in all: 66_000 bytes is within that (its head and tail overlap), 140_000 is over it.
@pytest.mark.parametrize("total", [66_000, 140_000])
def test_a_secret_across_the_head_cut_is_masked_whole(total: int) -> None:
    half = sandbox.MAX_OUTPUT_BYTES // 2
    # The key starts 20 bytes before the head's cut, so without masking first its start would be kept
    data = b"a" * (half - 21) + b" " + FAKE_KEY.encode() + b" " + b"b" * (total - half - 12)
    assert len(data) == total
    text = _kept(data)
    assert FAKE_KEY[:12] not in text and FAKE_KEY[-12:] not in text
    assert "[API key hidden]" in text
    assert MARKER.search(text) is not None


@pytest.mark.parametrize("total", [66_000, 140_000])
def test_a_secret_across_the_tail_cut_is_masked_whole(total: int) -> None:
    tail = sandbox.MAX_OUTPUT_BYTES - sandbox.MAX_OUTPUT_BYTES // 2
    # The key starts 15 bytes before the tail's cut, so without masking first its end would be kept
    data = b"a" * (total - tail - 16) + b" " + FAKE_KEY.encode() + b" " + b"b" * (tail - 17)
    assert len(data) == total
    text = _kept(data)
    assert FAKE_KEY[:12] not in text and FAKE_KEY[-12:] not in text
    assert "[API key hidden]" in text
    assert MARKER.search(text) is not None


def test_a_short_output_with_a_whole_secret_comes_back_masked() -> None:
    text = _kept(b"token " + FAKE_KEY.encode() + b"\n")
    assert "token" in text
    assert FAKE_KEY not in text


def test_output_just_over_the_limit_keeps_its_head_and_tail_around_the_cut() -> None:
    half = sandbox.MAX_OUTPUT_BYTES // 2
    tail = sandbox.MAX_OUTPUT_BYTES - half
    data = b"".join(b"line %06d\n" % i for i in range(6600))[:66_000]
    assert len(data) == 66_000
    text = _kept(data)
    match = MARKER.search(text)
    assert match is not None
    assert int(match.group(1)) == 66_000 - sandbox.MAX_OUTPUT_BYTES
    assert int(match.group(2)) == 66_000
    assert text == f"{data[:half].decode()}\n{match.group(0)}\n{data[-tail:].decode()}"


# ── read_file and files that aren't text ─────────────────────────────────────

def test_read_file_refuses_a_file_that_is_not_text_past_line_400(ws: Workspace) -> None:
    path = ws.root / "late.txt"
    path.write_bytes((b"a" * 20 + b"\n") * 500 + b"\xff" * 2000 + b"\n")
    with pytest.raises(WorkspaceError, match="isn't a text file"):
        ws.read_file("late.txt")


def test_read_file_of_a_long_text_file_still_gives_400_lines_and_a_note(ws: Workspace) -> None:
    _write(ws, "long.txt", "".join(f"line {n}\n" for n in range(1, 1001)))
    text = ws.read_file("long.txt")
    lines = text.splitlines()
    assert "line 400" in lines and "line 401" not in lines
    assert "1000 lines in all" in text


# ── Heavy folders only at the top of the workspace ───────────────────────────

def test_search_skips_build_folders_only_at_the_top(ws: Workspace) -> None:
    for rel in ("build/out.js", "dist/x.js", "target/x.rs", ".next/x.js",
                "src/build/gen.py", "pkg/target/lib.rs", "web/dist/app.js", "app/.next/page.js",
                "src/node_modules/m.js", "src/__pycache__/c.py"):
        _write(ws, rel, "needle\n")
    found = ws.search("needle")
    hits = {Path(line.split(":", 1)[0]) for line in found.splitlines() if ": needle" in line}
    assert hits == {Path("src/build/gen.py"), Path("pkg/target/lib.rs"), Path("web/dist/app.js"),
                    Path("app/.next/page.js")}
    note = found.splitlines()[-1]
    for name in ("build/", "dist/", ".next/", "node_modules/", "__pycache__/", "target/"):
        assert name in note


def test_list_dir_leaves_out_the_top_level_build_folders(ws: Workspace) -> None:
    for rel in ("build/out.js", "dist/x.js", "target/x.rs", ".next/x.js",
                "src/build/gen.py", "pkg/target/lib.rs", "web/dist/app.js", "app/.next/page.js",
                "src/node_modules/m.js", "src/__pycache__/c.py"):
        _write(ws, rel, "needle\n")
    listing = ws.list_dir(".").splitlines()
    for name in ("build", "dist", "target", ".next"):
        assert f"d {name}/" not in listing
    for name in ("app", "pkg", "src", "web"):
        assert f"d {name}/" in listing
    assert ".next/" in listing[-1] and "target/" in listing[-1]


def test_list_dir_of_a_nested_dist_folder_shows_it(ws: Workspace) -> None:
    _write(ws, "web/dist/app.js", "needle\n")
    assert ws.list_dir("web") == "d dist/"


def test_list_dir_of_src_leaves_out_node_modules_and_pycache_but_keeps_build(ws: Workspace) -> None:
    for rel in ("src/build/gen.py", "src/node_modules/m.js", "src/__pycache__/c.py"):
        _write(ws, rel, "needle\n")
    listing = ws.list_dir("src").splitlines()
    assert "d build/" in listing
    assert "d node_modules/" not in listing and "d __pycache__/" not in listing
    assert listing[-1] == "[skipped __pycache__/, node_modules/: heavy folders; list one by naming it as path]"
