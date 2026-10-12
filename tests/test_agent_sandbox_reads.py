"""
The agent's commands can't read the secret folders under the home folder or Ixel's config folder (on a Mac, in
the sandbox profile), while other reads work so programs run; and a long output keeps its start and its end,
with a line saying how much was left out between (ixel_mat/agent/sandbox.py).
"""
import io
import os
import re
import sys
from pathlib import Path

import pytest

from ixel_mat.agent import sandbox, tools
from ixel_mat.agent.sandbox import run_command
from ixel_mat.config import secrets as config_secrets

darwin = pytest.mark.skipif(sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"),
                            reason="sandbox-exec is macOS only")
MARKER = re.compile(r"\[output cut at 64 KB: (\d+) bytes left out here, of (\d+) in all\]")


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


# ── The deny rules in the profile text (any platform) ────────────────────────

def test_profile_denies_reads_of_the_secret_folders(home: Path) -> None:
    profile = sandbox.sandbox_profile(home / "workspace")
    denial = profile[profile.index("(deny file-read*"):]
    for folder in sandbox.SECRET_HOME_FOLDERS:
        assert f"(subpath {sandbox._quoted(os.path.realpath(home / folder))})" in denial
    config = home / ".config" / "ixel-mat"
    assert f"(subpath {sandbox._quoted(os.path.realpath(config))})" in denial
    for folder in (".ssh", ".aws", ".gnupg", "Library/Keychains", "Library/Application Support/Google/Chrome"):
        assert folder in sandbox.SECRET_HOME_FOLDERS
    assert tools._COMPUTER_SECRET_FOLDERS is sandbox.SECRET_HOME_FOLDERS


def test_profile_still_allows_reads_elsewhere(home: Path) -> None:
    profile = sandbox.sandbox_profile(home / "workspace")
    denial = profile[profile.index("(deny file-read*"):]
    assert f"(subpath {sandbox._quoted(os.path.realpath(home))})" not in denial
    assert f"(subpath {sandbox._quoted(os.path.realpath(home / 'workspace'))})" not in denial
    assert "(deny file-read*)" not in profile
    assert "(deny file-write*)" in profile and "(deny network*)" in profile


@pytest.mark.parametrize("network", [False, True])
@pytest.mark.parametrize("files", ["none", "output-folder", "chosen-folder"])
def test_mcp_profiles_deny_the_secret_folders_too(home: Path, network: bool, files: str) -> None:
    profile = sandbox.sandbox_profile(home / "workspace", network=network, files=files, temporary=False)
    assert "(deny file-read*" in profile
    assert f"(subpath {sandbox._quoted(os.path.realpath(home / '.ssh'))})" in profile
    assert f"(subpath {sandbox._quoted(os.path.realpath(home / '.config' / 'ixel-mat'))})" in profile


def test_a_workspace_inside_a_secret_folder_is_readable_again(home: Path) -> None:
    workspace = home / ".config" / "ixel-mat" / "agent" / "scratch" / "x-y"
    workspace.mkdir(parents=True)
    profile = sandbox.sandbox_profile(workspace)
    allow = f"(allow file-read* (subpath {sandbox._quoted(os.path.realpath(workspace))}))"
    assert allow in profile
    assert profile.index(allow) > profile.index("(deny file-read*")


def test_a_workspace_is_not_readable_again_without_file_access_or_when_it_only_contains_secrets(home: Path) -> None:
    workspace = home / ".config" / "ixel-mat" / "agent" / "scratch" / "x-y"
    workspace.mkdir(parents=True)
    assert "(allow file-read*" not in sandbox.sandbox_profile(workspace, files="none")
    profile = sandbox.sandbox_profile(home)
    assert "(allow file-read*" not in profile
    assert f"(subpath {sandbox._quoted(os.path.realpath(home / '.ssh'))})" in profile


# ── Under sandbox-exec ───────────────────────────────────────────────────────

@darwin
@pytest.mark.parametrize("folder", [".ssh", ".aws", ".config/ixel-mat"])
def test_sandboxed_cat_cannot_read_a_secret_folder(home: Path, folder: str) -> None:
    secret = home / folder / "fake_secret.txt"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("fake secret text", encoding="utf-8")
    (home / "workspace" / "note.txt").write_text("fake note text", encoding="utf-8")
    other = home / "Documents" / "other.txt"
    other.parent.mkdir()
    other.write_text("fake other text", encoding="utf-8")

    blocked = run_command(["/bin/cat", str(secret)], home / "workspace")
    assert blocked.sandboxed is True and blocked.returncode != 0
    assert "fake secret text" not in blocked.output

    inside = run_command(["/bin/cat", "note.txt"], home / "workspace")
    assert inside.returncode == 0 and "fake note text" in inside.output

    elsewhere = run_command(["/bin/cat", str(other)], home / "workspace")
    assert elsewhere.returncode == 0 and "fake other text" in elsewhere.output


# ── Head and tail of the kept output ─────────────────────────────────────────

def _kept(data: bytes) -> str:
    """The text a command's output comes back as, once all of data has been read through _Output."""
    output = sandbox._Output(io.BufferedReader(io.BytesIO(data)))
    output.thread.join(timeout=5)
    assert not output.thread.is_alive()
    return output.text()


def test_long_output_keeps_its_head_and_tail_with_a_line_between() -> None:
    limit = sandbox.MAX_OUTPUT_BYTES
    half = limit // 2
    data = b"".join(b"line %06d\n" % i for i in range(9091))[:100_000]
    assert len(data) == 100_000
    text = _kept(data)
    match = MARKER.search(text)
    assert match is not None
    assert int(match.group(2)) == len(data)
    assert int(match.group(1)) == len(data) - limit
    assert text == f"{data[:half].decode()}\n{match.group(0)}\n{data[-(limit - half):].decode()}"


def test_output_of_exactly_the_limit_comes_back_whole() -> None:
    data = b"0123456789abcdef" * (sandbox.MAX_OUTPUT_BYTES // 16)
    assert len(data) == sandbox.MAX_OUTPUT_BYTES
    text = _kept(data)
    assert text == data.decode()
    assert "left out" not in text


def test_short_output_comes_back_unchanged() -> None:
    assert _kept(b"hello\n") == "hello\n"


def test_run_command_keeps_the_start_and_end_of_long_output(tmp_path: Path) -> None:
    code = "print('HEAD-LINE'); print('x' * 100000); print('TAIL-LINE')"
    result = run_command([sys.executable, "-c", code], tmp_path)
    assert result.returncode == 0
    assert result.output.startswith("HEAD-LINE")
    assert result.output.rstrip().endswith("TAIL-LINE")
    assert MARKER.search(result.output) is not None
    assert len(result.output.encode("utf-8")) < sandbox.MAX_OUTPUT_BYTES + 150
