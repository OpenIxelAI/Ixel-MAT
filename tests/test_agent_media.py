"""
Looking at files (ixel_mat/agent/tools.py and runner.py): list_dir says what each file is, read_file reads a
document as text, view_file returns pictures (a PNG or JPEG, a document's pictures, a video's frames through
ffmpeg), and the run sends them with the model's next call only, and only to a model that can see them.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import pytest

import doc_samples as samples
from ixel_mat.agent.runner import AgentRun
from ixel_mat.agent.store import AgentStore
from ixel_mat.agent.tools import Viewed, Workspace, WorkspaceError
from ixel_mat.agents.base import AgentConfig
from ixel_mat.pictures import Picture

MP4 = b"\x00\x00\x00\x18ftypmp42" + bytes(20)  # not a real video: only its name and a fake ffmpeg matter here


@pytest.fixture
def ws(tmp_path) -> Workspace:
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(root)


def _put(ws: Workspace, name: str, data: bytes) -> None:
    (ws.root / name).write_bytes(data)


# ── list_dir ─────────────────────────────────────────────────────────────────

def test_list_dir_says_what_each_file_is(ws):
    png, docx = samples.png(), samples.docx()
    _put(ws, "a.txt", b"hello")
    _put(ws, "blob.bin", b"ab\x00cd")
    _put(ws, "clip.mp4", MP4)
    _put(ws, "photo.png", png)
    _put(ws, "report.docx", docx)
    assert ws.list_dir().splitlines() == [
        "f a.txt 5",  # plain text keeps the line it always had
        "f blob.bin 5 other",
        f"f clip.mp4 {len(MP4)} video",
        f"f photo.png {len(png)} picture",
        f"f report.docx {len(docx)} document",
    ]


# ── read_file ────────────────────────────────────────────────────────────────

def test_read_file_reads_a_document_as_text(ws):
    _put(ws, "report.docx", samples.docx())
    text = ws.read_file("report.docx")
    assert "Lab 3: Titration" in text
    assert "no pictures" in text


def test_read_file_counts_a_documents_pictures(ws):
    body = f'<w:p><w:r><w:t>See the burette</w:t></w:r></w:p><w:p>{samples.word_picture("rPic")}</w:p>'
    _put(ws, "lab.docx", samples.docx(body, pictures={"rPic": samples.png()}))
    text = ws.read_file("lab.docx")
    assert "See the burette" in text
    assert "1 picture inside" in text and "view_file" in text


@pytest.mark.parametrize("name, data", [("photo.png", samples.png()), ("clip.mp4", MP4)])
def test_read_file_sends_pictures_and_videos_to_view_file(ws, name, data):
    _put(ws, name, data)
    with pytest.raises(WorkspaceError, match="view_file"):
        ws.read_file(name)


# ── view_file ────────────────────────────────────────────────────────────────

def test_view_file_returns_a_png(ws):
    data = samples.png()
    _put(ws, "photo.png", data)
    viewed = ws.view_file("photo.png")
    assert isinstance(viewed, Viewed) and len(viewed.pictures) == 1
    picture = viewed.pictures[0]
    assert isinstance(picture, Picture) and picture.media_type == "image/png"
    assert (picture.width, picture.height) == (64, 48)
    assert "photo.png" in viewed.text and f"{len(data)} bytes" in viewed.text


def test_view_file_returns_a_jpeg_without_where_it_was_taken(ws):
    data = samples.jpeg()
    assert b"GPS-48.8584N" in data
    _put(ws, "photo.jpg", data)
    viewed = ws.view_file("photo.jpg")
    assert len(viewed.pictures) == 1
    picture = viewed.pictures[0]
    assert picture.media_type == "image/jpeg"
    assert b"GPS" not in picture.data
    assert "photo.jpg" in viewed.text and f"{len(data)} bytes" in viewed.text


@pytest.mark.parametrize("name, data", [("anim.gif", b"GIF89a" + bytes(20)), ("bad.png", samples.png()[:30])])
def test_view_file_refuses_a_picture_it_cannot_send(ws, name, data):
    _put(ws, name, data)
    with pytest.raises(WorkspaceError):
        ws.view_file(name)


def test_view_file_leaves_text_to_read_file(ws):
    _put(ws, "a.txt", b"hello")
    with pytest.raises(WorkspaceError, match="read_file"):
        ws.view_file("a.txt")


def test_view_file_shows_a_documents_pictures(ws):
    body = f'<w:p><w:r><w:t>See the burette</w:t></w:r></w:p><w:p>{samples.word_picture("rPic")}</w:p>'
    _put(ws, "lab.docx", samples.docx(body, pictures={"rPic": samples.png()}))
    viewed = ws.view_file("lab.docx")
    assert len(viewed.pictures) == 1 and viewed.pictures[0].media_type == "image/png"
    assert "lab.docx" in viewed.text


def test_a_pdf_is_read_and_its_picture_shown(ws):
    pytest.importorskip("pypdf")
    _put(ws, "report.pdf", samples.pdf(picture=samples.jpeg()))
    text = ws.read_file("report.pdf")
    assert "Lab report page one" in text and "a PDF" in text
    viewed = ws.view_file("report.pdf")
    assert len(viewed.pictures) == 1 and viewed.pictures[0].media_type == "image/jpeg"


# ── video, with ffmpeg faked by a script ─────────────────────────────────────

def _fake_ffmpeg(tmp_path: Path) -> Path:
    """An 'ffmpeg' in tmp_path/bin that is a small Python script. It records each call (its words and the
    folder it ran in) as a line of JSON in calls.log, answers a call whose last word names a .png with a tiny
    picture, and any other call with a 12-second Duration line and exit code 1, as the real one does when it's
    only asked to describe a file. Returns the log."""
    log = tmp_path / "calls.log"
    frame = base64.b64encode(samples.png(16, 16)).decode("ascii")
    folder = tmp_path / "bin"
    folder.mkdir()
    script = folder / "ffmpeg"
    script.write_text(
        f"#!{sys.executable}\n"
        "import base64, json, os, sys\n"
        f"LOG = {str(log)!r}\n"
        f"FRAME = {frame!r}\n"
        "args = sys.argv[1:]\n"
        "with open(LOG, 'a') as log:\n"
        "    log.write(json.dumps({'argv': args, 'cwd': os.getcwd()}) + '\\n')\n"
        "if args and args[-1].endswith('.png'):\n"
        "    with open(args[-1], 'wb') as out:\n"
        "        out.write(base64.b64decode(FRAME))\n"
        "    sys.exit(0)\n"
        "sys.stderr.write('Duration: 00:00:12.00, start: 0.000000, bitrate: 20 kb/s\\n')\n"
        "sys.exit(1)\n", encoding="utf-8")
    script.chmod(0o755)
    return log


# The stand-in is started through its #! line, which can't hold a path with a space or a very long one
NO_SCRIPT = sys.platform == "win32" or " " in sys.executable or len(sys.executable) > 120


@pytest.mark.skipif(NO_SCRIPT, reason="a script stand-in for ffmpeg can't be started here")
def test_view_file_takes_frames_spread_over_a_video(ws, tmp_path, monkeypatch):
    log = _fake_ffmpeg(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    _put(ws, "clip.mp4", MP4)
    viewed = ws.view_file("clip.mp4")
    assert len(viewed.pictures) == 6
    assert all(picture.media_type == "image/png" for picture in viewed.pictures)
    assert "12.0 s" in viewed.text and "6 frames" in viewed.text
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert calls[0]["argv"][-1].endswith("clip.mp4") and "-ss" not in calls[0]["argv"]  # asked its length first
    frames = [call["argv"] for call in calls if "-ss" in call["argv"]]
    assert [float(argv[argv.index("-ss") + 1]) for argv in frames] == [1.0, 3.0, 5.0, 7.0, 9.0, 11.0]
    # Every word reached ffmpeg as it was given, so no shell read the filter's backslash or brackets
    assert all("scale=min(1280\\,iw):-2" in argv for argv in frames)
    assert all(argv[-1].endswith(".png") for argv in frames)
    # It ran in a private temp folder of its own, which is gone again
    assert all(Path(call["cwd"]).name.startswith("ixel-agent-") for call in calls)
    assert not Path(calls[0]["cwd"]).exists()


def test_a_video_without_ffmpeg_says_so(ws, tmp_path, monkeypatch):
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    _put(ws, "clip.mp4", MP4)
    viewed = ws.view_file("clip.mp4")
    assert viewed.pictures == []
    assert "ffmpeg isn't installed" in viewed.text and "clip.mp4" in viewed.text


# ── the run: pictures go with the next call ──────────────────────────────────

def _reply(action: str, **args) -> str:
    return json.dumps({"thought": "Look at it", "action": action, "args": args})


class Asked:
    """A stand-in for the model that takes pictures= as the runner gives it, and records the prompt and the
    pictures of each call."""

    def __init__(self, *replies: str):
        self.replies = iter(replies)
        self.prompts: list[str] = []
        self.pictures: list[list[Picture]] = []

    async def __call__(self, prompt: str, pictures=None) -> tuple[str, dict | None]:
        self.prompts.append(prompt)
        self.pictures.append(list(pictures or []))
        return next(self.replies), None


def _run(tmp_path: Path, ask: Asked, accepts: list[str]) -> AgentRun:
    cfg = AgentConfig(name="fake", label="Fake model", type="http", model="fake-model",
                      url="https://model.invalid", token="obviously-fake-token", accepts=accepts)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "photo.png").write_bytes(samples.png())
    return AgentRun(cfg, workspace, "Look at the photo", AgentStore(tmp_path / "agent"), ask=ask)


async def _done(run: AgentRun) -> dict:
    await asyncio.wait_for(run.task, timeout=2)
    return run.to_dict()


def test_a_model_that_cannot_see_pictures_is_told_so(tmp_path):
    async def scenario():
        ask = Asked(_reply("view_file", path="photo.png"), _reply("finish", answer="I could not look at it."))
        run = _run(tmp_path, ask, accepts=[])
        assert run.cfg.sees_pictures is False
        run.start()
        page = await _done(run)
        assert page["status"] == "done"
        step = page["steps"][0]
        assert step["action"] == "view_file" and step["ok"] is False and step["pictures"] == 0
        assert "can't see pictures" in step["result"]
        assert "can't see pictures" in ask.prompts[1]
        assert ask.pictures == [[], []]

    asyncio.run(scenario())


def test_pictures_go_with_the_call_after_view_file(tmp_path):
    async def scenario():
        ask = Asked(_reply("view_file", path="photo.png"), _reply("finish", answer="A red picture."))
        run = _run(tmp_path, ask, accepts=["image"])
        assert run.cfg.sees_pictures is True
        run.start()
        page = await _done(run)
        assert page["status"] == "done" and len(ask.prompts) == 2  # nothing is asked after finish
        assert [len(sent) for sent in ask.pictures] == [0, 1]
        assert ask.pictures[1][0].media_type == "image/png"
        assert "photo.png" in ask.prompts[1]
        step = page["steps"][0]
        assert step["action"] == "view_file" and step["ok"] is True and step["pictures"] == 1
        assert "pictures" not in page["steps"][1]  # only a view_file step has the key
        assert run.to_dict()["steps"][0]["pictures"] == 1
        assert run.store.load_session(run.id)["steps"][0]["pictures"] == 1

    asyncio.run(scenario())


def test_pictures_are_sent_once_and_not_again(tmp_path):
    async def scenario():
        ask = Asked(_reply("view_file", path="photo.png"), _reply("list_dir"), _reply("finish", answer="Done."))
        run = _run(tmp_path, ask, accepts=["image"])
        run.start()
        await _done(run)
        assert [len(sent) for sent in ask.pictures] == [0, 1, 0]

    asyncio.run(scenario())


def test_a_picture_that_cannot_be_read_sends_nothing(tmp_path):
    async def scenario():
        ask = Asked(_reply("view_file", path="missing.png"), _reply("finish", answer="There is no such file."))
        run = _run(tmp_path, ask, accepts=["image"])
        run.start()
        page = await _done(run)
        step = page["steps"][0]
        assert step["ok"] is False and step["pictures"] == 0
        assert ask.pictures == [[], []]

    asyncio.run(scenario())
