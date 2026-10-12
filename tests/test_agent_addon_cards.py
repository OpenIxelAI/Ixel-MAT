"""
Adding a tool from a GitHub link (ixel_mat/agent/cards.py, runner.py and tools.py): unpacking a repo's tarball,
the tree hash, a card's rules, its approval and the scratch: paths. GitHub is a local server here; nothing
reaches the network, and every add-on file goes to a tmp_path folder, never to ~/.ixel or ixel_mat/addons.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import stat
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from ixel_mat.agent import cards
from ixel_mat.agent.runner import AgentRun
from ixel_mat.agent.store import AgentStore
from ixel_mat.agent.tools import Workspace, WorkspaceError
from ixel_mat.agents.base import AgentConfig

OWNER = "example-owner"
REPO = "example-tool"
SCRATCH = f"{OWNER}-{REPO}"
REPO_URL = f"https://github.com/{OWNER}/{REPO}"
COMMIT = "ab" * 20                     # a fake full commit
OTHER_COMMIT = "cd" * 20
TOP = f"{REPO}-{COMMIT}"               # codeload's top folder
REPO_FILES = {
    "README.md": b"# Example tool\n\nRuns an example.\n",
    "tools/run.py": b"print('example')\n",
    "tools/data/list.txt": b"one\ntwo\n",
}
FILE_COUNT = len(REPO_FILES)


def _hand_hash(files: dict[str, bytes]) -> str:
    """The tree hash, worked out from the algorithm in cards.tree_hash's docstring and not from cards: one line
    per file, '<sha256 of its bytes>  <path with />' and a newline, the lines in order of the paths' UTF-8
    bytes, then the sha256 of all the lines together."""
    text = b""
    for rel in sorted(files, key=lambda name: name.encode("utf-8")):
        line = hashlib.sha256(files[rel]).hexdigest() + "  " + rel + "\n"
        text += line.encode("utf-8")
    return hashlib.sha256(text).hexdigest()


TREE = _hand_hash(REPO_FILES)
OTHER_TREE = "0" * 64

# ── Tarballs and the fake GitHub ─────────────────────────────────────────────


def _targz(add: Callable[[tarfile.TarFile], None]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        add(tar)
    return buffer.getvalue()


def _archive(tmp_path: Path, add: Callable[[tarfile.TarFile], None]) -> Path:
    path = tmp_path / "repo.tar.gz"
    path.write_bytes(_targz(add))
    return path


def _file(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def _special(tar: tarfile.TarFile, name: str, kind: bytes, linkname: str = "", devmajor: int = 0,
             devminor: int = 0) -> None:
    """A link, a device or another entry that isn't a regular file (so no data)."""
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    info.devmajor = devmajor
    info.devminor = devminor
    tar.addfile(info)


def _repo_tarball() -> bytes:
    def add(tar: tarfile.TarFile) -> None:
        for rel, data in REPO_FILES.items():
            _file(tar, f"{TOP}/{rel}", data)

    return _targz(add)


async def _fake_github(monkeypatch: pytest.MonkeyPatch, tarball: bytes) -> TestServer:
    """A local server standing in for GitHub's commits API and codeload, with HEAD at COMMIT. cards reads both
    addresses at call time, so they're pointed at it here. The caller closes the server."""

    async def answer(request: web.Request) -> web.Response:
        if request.path == f"/repos/{OWNER}/{REPO}/commits/HEAD":
            return web.json_response({"sha": COMMIT})
        if request.path == f"/{OWNER}/{REPO}/tar.gz/{COMMIT}":
            return web.Response(body=tarball, content_type="application/gzip")
        return web.Response(status=404)

    app = web.Application()
    app.router.add_get("/{tail:.*}", answer)
    server = TestServer(app)  # 127.0.0.1, on a free port
    await server.start_server()
    base = str(server.make_url("/")).rstrip("/")
    monkeypatch.setattr(cards, "GITHUB_API", base)
    monkeypatch.setattr(cards, "CODELOAD", base)
    return server


# ── Extraction ───────────────────────────────────────────────────────────────


def test_a_link_and_a_device_are_skipped_and_the_file_is_copied(tmp_path):
    def add(tar: tarfile.TarFile) -> None:
        _file(tar, f"{TOP}/README.md", b"hello\n")
        _special(tar, f"{TOP}/link.txt", tarfile.SYMTYPE, linkname="README.md")
        _special(tar, f"{TOP}/hard.txt", tarfile.LNKTYPE, linkname=f"{TOP}/README.md")
        _special(tar, f"{TOP}/dev", tarfile.CHRTYPE, devmajor=1, devminor=3)

    dest = tmp_path / "copy"
    skipped = cards.extract_tarball(_archive(tmp_path, add), dest)
    assert (dest / "README.md").read_bytes() == b"hello\n"
    assert set(skipped) == {"link.txt", "hard.txt", "dev"}
    for name in ("link.txt", "hard.txt", "dev"):
        assert not os.path.lexists(dest / name)
    assert sorted(os.listdir(dest)) == ["README.md"]


@pytest.mark.parametrize("name", ["../evil.txt", "/home/x/evil.txt"])
def test_a_path_that_leaves_the_copy_refuses_the_whole_archive(tmp_path, name):
    def add(tar: tarfile.TarFile) -> None:
        _file(tar, f"{TOP}/README.md", b"hello\n")  # written before the bad entry, so it must be undone
        _file(tar, name, b"evil\n")

    dest = tmp_path / "copy"
    with pytest.raises(ValueError, match=re.escape(name)):
        cards.extract_tarball(_archive(tmp_path, add), dest)
    assert not dest.exists()
    assert not (tmp_path / "evil.txt").exists()


# ── Limits ───────────────────────────────────────────────────────────────────


def test_more_files_than_the_limit_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "MAX_REPO_FILES", 2)

    def add(tar: tarfile.TarFile) -> None:
        for name in ("a.txt", "b.txt", "c.txt"):
            _file(tar, f"{TOP}/{name}", b"x")

    dest = tmp_path / "copy"
    with pytest.raises(ValueError, match="more than 2 files"):
        cards.extract_tarball(_archive(tmp_path, add), dest)
    assert not dest.exists()


def test_more_bytes_than_the_limit_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "MAX_REPO_BYTES", 10)

    def add(tar: tarfile.TarFile) -> None:
        _file(tar, f"{TOP}/a.txt", b"123456")
        _file(tar, f"{TOP}/b.txt", b"123456")  # 12 bytes in all

    dest = tmp_path / "copy"
    with pytest.raises(ValueError, match="over 10 bytes"):
        cards.extract_tarball(_archive(tmp_path, add), dest)
    assert not dest.exists()


# ── The tree hash ────────────────────────────────────────────────────────────


def test_tree_hash_matches_one_worked_out_by_hand(tmp_path):
    folder = tmp_path / "copy"
    for rel, data in REPO_FILES.items():
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    under_tools = {"run.py": REPO_FILES["tools/run.py"], "data/list.txt": REPO_FILES["tools/data/list.txt"]}
    assert cards.tree_hash(folder, "") == (TREE, FILE_COUNT)
    assert cards.tree_hash(folder, "tools") == (_hand_hash(under_tools), len(under_tools))


# ── Fetching through the local server ────────────────────────────────────────


def test_fetch_without_a_commit_resolves_head_and_copies_the_files(tmp_path, monkeypatch):
    async def scenario():
        server = await _fake_github(monkeypatch, _repo_tarball())
        try:
            return await cards.fetch_repo(REPO_URL, None, tmp_path / "scratch")
        finally:
            await server.close()

    found = asyncio.run(scenario())
    folder = tmp_path / "scratch" / SCRATCH
    assert found.name == SCRATCH and found.commit == COMMIT
    assert COMMIT in found.text() and f"scratch:{SCRATCH}" in found.text()
    for rel, data in REPO_FILES.items():
        assert (folder / rel).read_bytes() == data
    assert found.tree_sha256 == TREE and found.files == FILE_COUNT
    assert sorted(os.listdir(tmp_path / "scratch")) == [SCRATCH]  # no temp files left beside it


# ── Cards: a valid one, and every rule ───────────────────────────────────────


def _card(*, commit: str = COMMIT, tree: str = TREE, files: int = FILE_COUNT, repo: str = REPO_URL,
          subdir: str = "") -> str:
    """A valid card for the example tool, its [source] as given (what fetch and tree_hash said)."""
    return (
        'id = "example-tool"\n'
        'name = "Example tool"\n'
        'description = "Runs the example tool on a file."\n'
        'kind = "cli"\n'
        'commands = [["{addon}/run", "--out", "{output}"]]\n'
        "\n"
        "[source]\n"
        f'repo = "{repo}"\n'
        f'commit = "{commit}"\n'
        f'subdir = "{subdir}"\n'
        f'tree_sha256 = "{tree}"\n'
        f"files = {files}\n"
        "\n"
        "[permissions]\n"
        "network = false\n"
        'files = "output-folder"\n'
    )


CARD = _card()
COMMANDS_LINE = 'commands = [["{addon}/run", "--out", "{output}"]]'
TWO_COMMANDS = 'commands = [["{addon}/run"], ["{addon}/other"]]'
FETCHED = {SCRATCH: {"repo_url": REPO_URL, "commit": COMMIT, "hashes": {"": (TREE, FILE_COUNT)}}}


def _swap(old: str, new: str, base: str = CARD) -> str:
    """The valid card with one piece changed. old must be in it, so a test can't quietly change nothing."""
    assert old in base, old
    return base.replace(old, new, 1)


VIOLATIONS = [
    pytest.param('surprise = "x"\n' + CARD, "surprise", id="unknown-top-level-key"),
    pytest.param(_swap('subdir = ""', 'subdir = ""\nbranch = "main"'), "source.branch", id="unknown-source-key"),
    pytest.param(_swap('id = "example-tool"', 'id = "Example_Tool"'), "id", id="bad-id"),
    pytest.param(_swap('id = "example-tool"', 'id = "other-tool"'), "id", id="id-differs-from-the-action"),
    pytest.param(_swap('kind = "cli"', 'kind = "python"'), "kind", id="bad-kind"),
    pytest.param(_swap(f'commit = "{COMMIT}"', f'commit = "{COMMIT[:39]}"'), "source.commit", id="short-commit"),
    pytest.param(_swap(f'tree_sha256 = "{TREE}"', f'tree_sha256 = "{TREE[:63]}"'), "source.tree_sha256",
                 id="short-tree-hash"),
    pytest.param(_swap(f'tree_sha256 = "{TREE}"', f'tree_sha256 = "{TREE.upper()}"'), "source.tree_sha256",
                 id="uppercase-tree-hash"),
    pytest.param(_swap(f'repo = "{REPO_URL}"', 'repo = "https://gitlab.com/example-owner/example-tool"'),
                 "source.repo", id="not-github"),
    pytest.param(_swap('subdir = ""', 'subdir = "../x"'), "source.subdir", id="subdir-with-dotdot"),
    pytest.param(_swap(f"files = {FILE_COUNT}", "files = 0"), "source.files", id="files-zero"),
    pytest.param(_swap(f"files = {FILE_COUNT}", "files = true"), "source.files", id="files-true"),
    pytest.param(_swap('files = "output-folder"', 'files = "anywhere"'), "permissions.files",
                 id="bad-permissions-files"),
    pytest.param(_swap("network = false", 'network = "no"'), "permissions.network", id="network-not-a-bool"),
    pytest.param(_swap('kind = "cli"', 'kind = "mcp"', _swap(COMMANDS_LINE, TWO_COMMANDS)),
                 "commands", id="mcp-with-two-commands"),
    pytest.param(_swap(COMMANDS_LINE, 'commands = [["sh", "-c", "x"]]'), "commands", id="shell-command"),
    pytest.param(_swap(COMMANDS_LINE, 'commands = [["{addon}/run", "{home}"]]'), "commands",
                 id="unknown-placeholder"),
    pytest.param(_swap('files = "output-folder"', 'files = "none"'), "commands", id="output-without-files"),
]


@pytest.mark.parametrize("text, key", VIOLATIONS)
def test_a_rule_violation_is_refused_with_its_key_named(text, key):
    with pytest.raises(cards.CardError) as info:
        cards.validate_card(text, "example-tool")
    assert info.value.key == key
    assert str(info.value).startswith(f"{key}: ")


def test_a_valid_card_passes_the_checks_by_itself():
    card = cards.validate_card(CARD, "example-tool")
    assert card["id"] == "example-tool" and card["kind"] == "cli"
    assert card["source"] == {"repo": REPO_URL, "commit": COMMIT, "subdir": "", "tree_sha256": TREE,
                              "files": FILE_COUNT}


MISMATCHES = [
    pytest.param(_card(commit=OTHER_COMMIT), "source.commit", id="commit-not-fetched"),
    pytest.param(_card(tree=OTHER_TREE), "source.tree_sha256", id="tree-hash-not-computed"),
    pytest.param(_card(files=FILE_COUNT + 1), "source.files", id="file-count-differs"),
    pytest.param(_card(subdir="tools"), "source.subdir", id="folder-never-hashed"),
    pytest.param(_card(repo="https://github.com/example-owner/other-tool"), "source.repo",
                 id="repo-not-fetched"),
]


@pytest.mark.parametrize("text, key", MISMATCHES)
def test_a_card_that_does_not_match_the_fetch_is_refused(tmp_path, text, key):
    with pytest.raises(cards.CardError) as info:
        cards.prepare_card("example-tool", text, FETCHED, tmp_path / "addons", tmp_path / "builtin")
    assert info.value.key == key
    assert not (tmp_path / "addons").exists()


def test_a_built_in_add_on_id_is_refused(tmp_path):
    (tmp_path / "builtin" / "example-tool").mkdir(parents=True)
    with pytest.raises(cards.CardError, match="built-in") as info:
        cards.prepare_card("example-tool", CARD, FETCHED, tmp_path / "addons", tmp_path / "builtin")
    assert info.value.key == "id"


TURN_ON = [
    pytest.param("enabled = true\n" + CARD, "enabled", id="enabled-at-the-top"),
    pytest.param("approved = true\n" + CARD, "approved", id="approved-at-the-top"),
    pytest.param(_swap("network = false", "network = false\napproved = true"), "permissions.approved",
                 id="approved-in-permissions"),
]


@pytest.mark.parametrize("text, key", TURN_ON)
def test_a_card_cannot_turn_itself_on(tmp_path, text, key):
    with pytest.raises(cards.CardError) as info:
        cards.prepare_card("example-tool", text, FETCHED, tmp_path / "addons", tmp_path / "builtin")
    assert info.value.key == key
    assert not (tmp_path / "addons").exists()


# ── The run: fetch, hash, write after approval ───────────────────────────────


def _reply(action: str, **args) -> str:
    return json.dumps({"thought": "Check one thing", "action": action, "args": args})


class Asked:
    """A stand-in for the model: returns the scripted replies in turn."""

    def __init__(self, *replies: str):
        self.replies = iter(replies)

    async def __call__(self, prompt: str) -> tuple[str, dict | None]:
        return next(self.replies), None


def _cfg() -> AgentConfig:
    return AgentConfig(name="fake", label="Fake model", type="http", model="fake-model",
                       url="https://model.invalid", token="obviously-fake-token")


@pytest.fixture
def store(tmp_path) -> AgentStore:
    return AgentStore(tmp_path / "agent")


def _run(tmp_path: Path, store: AgentStore, ask: Asked) -> AgentRun:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return AgentRun(_cfg(), workspace, "Add the example tool", store, ask=ask, addons_dir=tmp_path / "addons",
                    builtin_dir=tmp_path / "builtin")


async def _until_waiting(run: AgentRun) -> dict:
    """Wait until the run asks for the person's approval, and return its page."""
    for _ in range(1000):
        if run.status == "waiting":
            return run.to_dict()
        if run.task is not None and run.task.done():
            break
        await asyncio.sleep(0.01)
    raise AssertionError(f"the run didn't wait for approval: status {run.status!r}, error {run.error!r}")


async def _done(run: AgentRun) -> dict:
    await asyncio.wait_for(run.task, timeout=10)
    return run.to_dict()


async def _fetch_then_wait_for_the_card(run: AgentRun) -> dict:
    """Start the run, approve its fetch_repo, and return the page once it waits on write_addon_card."""
    run.start()
    fetch = await _until_waiting(run)
    assert fetch["pending"]["action"] == "fetch_repo"
    await run.decide(True)
    return await _until_waiting(run)


@pytest.fixture
def canonical(tmp_path, monkeypatch):
    """A stand-in for ixel_mat.addons, the one writer of cards: it checks a card with the agent's own rules and
    writes it privately under tmp_path/addons, the way the real module writes ~/.ixel/addons."""
    import sys
    from types import ModuleType, SimpleNamespace
    from ixel_mat.config.secrets import write_private_file
    module = ModuleType("ixel_mat.addons")

    def validate(text):
        card = cards.validate_card(text, cards.tomllib.loads(text).get("id", ""))
        return SimpleNamespace(id=card["id"], name=card["name"], kind=card["kind"], builtin=False,
                               source=SimpleNamespace(**card["source"]))

    def write(text):
        checked = validate(text)
        folder = tmp_path / "addons" / checked.id
        folder.mkdir(parents=True, exist_ok=True)
        write_private_file(folder / "addon.toml", text.encode("utf-8"))
        return checked

    module.validate_card_text = validate
    module.write_agent_card = write
    module.all_cards = lambda: []
    monkeypatch.setitem(sys.modules, "ixel_mat.addons", module)
    return module


def test_a_valid_card_waits_for_approval_and_is_written_only_then(tmp_path, store, monkeypatch, canonical):
    addons = tmp_path / "addons"

    async def scenario() -> dict:
        server = await _fake_github(monkeypatch, _repo_tarball())
        try:
            ask = Asked(_reply("fetch_repo", url=REPO_URL),
                        _reply("tree_hash", repo=SCRATCH, subdir=""),
                        _reply("write_addon_card", id="example-tool", card=CARD),
                        _reply("finish", answer="Added."))
            run = _run(tmp_path, store, ask)
            writing = await _fetch_then_wait_for_the_card(run)
            assert writing["status"] == "waiting" and writing["pending"]["action"] == "write_addon_card"
            assert CARD in writing["pending"]["diff"]
            assert not (addons / "example-tool" / "addon.toml").exists()  # nothing written before approval
            await run.decide(True)
            return await _done(run)
        finally:
            await server.close()

    page = asyncio.run(scenario())
    fetch, tree, write, finish = page["steps"]
    assert page["status"] == "done" and finish["action"] == "finish"
    assert fetch["ok"] is True and COMMIT in fetch["result"]
    assert f'tree_sha256 = "{TREE}"' in tree["result"] and f"files = {FILE_COUNT}" in tree["result"]
    assert write["ok"] is True and "not turned on" in write["result"]

    path = addons / "example-tool" / "addon.toml"
    assert path.read_bytes() == CARD.encode("utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # The only thing made under the addons folder is the card, and it's still off: nothing else was written
    found = sorted(item.relative_to(addons).as_posix() for item in addons.rglob("*"))
    assert found == ["example-tool", "example-tool/addon.toml"]


def test_an_overwritten_card_needs_approval_again_before_it_is_replaced(tmp_path, store, monkeypatch, canonical):
    addons = tmp_path / "addons"
    (addons / "example-tool").mkdir(parents=True)
    (addons / "example-tool" / "addon.toml").write_bytes(b"old card\n")

    async def scenario() -> dict:
        server = await _fake_github(monkeypatch, _repo_tarball())
        try:
            ask = Asked(_reply("fetch_repo", url=REPO_URL),
                        _reply("tree_hash", repo=SCRATCH),
                        _reply("write_addon_card", id="example-tool", card=CARD),
                        _reply("finish", answer="Replaced."))
            run = _run(tmp_path, store, ask)
            writing = await _fetch_then_wait_for_the_card(run)
            assert "must be approved again in Settings › Add-ons" in writing["pending"]["diff"]
            assert (addons / "example-tool" / "addon.toml").read_bytes() == b"old card\n"
            await run.decide(True)
            return await _done(run)
        finally:
            await server.close()

    page = asyncio.run(scenario())
    assert page["status"] == "done" and "replacing the old card" in page["steps"][2]["result"]
    assert (addons / "example-tool" / "addon.toml").read_bytes() == CARD.encode("utf-8")


# ── Paths into a fetched copy: scratch:NAME ──────────────────────────────────


def test_a_fetched_repo_can_be_read_but_never_written(tmp_path, store):
    copy = store.scratch_dir() / SCRATCH
    copy.mkdir(parents=True)
    (copy / "README.md").write_bytes(b"# Example tool\n")
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = Workspace(root, scratch=store.scratch_dir())
    assert workspace.read_file(f"scratch:{SCRATCH}/README.md") == "# Example tool\n"
    with pytest.raises(WorkspaceError):
        workspace.read_file(f"scratch:{SCRATCH}/../README.md")
    with pytest.raises(WorkspaceError):
        workspace.prepare_write(f"scratch:{SCRATCH}/README.md", "changed\n")
    assert (copy / "README.md").read_text(encoding="utf-8") == "# Example tool\n"


def test_a_command_in_a_fetched_repo_always_asks_even_when_allowed(tmp_path, store):
    copy = store.scratch_dir() / SCRATCH
    copy.mkdir(parents=True)
    (copy / "README.md").write_bytes(b"# Example tool\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store.allow(workspace, ["ls"])
    assert store.is_allowed(workspace, ["ls"])  # it would run at once in the workspace

    async def scenario() -> dict:
        ask = Asked(_reply("run_command", argv=["ls"], cwd=f"scratch:{SCRATCH}"),
                    _reply("finish", answer="Looked."))
        run = AgentRun(_cfg(), workspace, "Look at the repo", store, ask=ask, addons_dir=tmp_path / "addons",
                       builtin_dir=tmp_path / "builtin")
        run.start()
        waiting = await _until_waiting(run)
        assert waiting["pending"]["action"] == "run_command"
        assert waiting["pending"]["args"]["cwd"] == f"scratch:{SCRATCH}"
        with pytest.raises(ValueError, match="asks every time"):
            await run.decide(True, always=True)
        assert run.status == "waiting"  # refused, and still waiting for a plain answer
        await run.decide(False)
        return await _done(run)

    page = asyncio.run(scenario())
    assert page["steps"][0]["ok"] is False and "denied" in page["steps"][0]["result"]
