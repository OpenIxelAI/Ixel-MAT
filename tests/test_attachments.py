"""
Documents and pictures attached to a question on the command line (ixel ask / ixel review --file), and
pictures going to the subscription programs (Claude Code, Codex, Gemini CLI, Copilot, OpenCode) the way each
takes them, with no API key.
"""
import asyncio
import json
import os
import sys

import pytest

import doc_samples as samples
from ixel_mat import ask as ask_mod
from ixel_mat.agents.base import AgentConfig
from ixel_mat.agents.oneshot import OneShotAgent
from ixel_mat.config import loader
from ixel_mat.material import (DEFAULT_DOCUMENT_QUESTION, DEFAULT_FILES_QUESTION, MaterialError, check,
                               code_for_review, read_files)
from ixel_mat.modes.review import run_review
from ixel_mat.pictures import read_picture
from ixel_mat.presets import PRESETS_BY_ID


def _run(coro):
    return asyncio.run(coro)


# ── Files named on the command line ───────────────────────────────────────────

def test_documents_and_pictures_are_read_with_their_names(tmp_path):
    (tmp_path / "lab.docx").write_bytes(samples.docx())
    (tmp_path / "photo.png").write_bytes(samples.png())
    (tmp_path / "report.pdf").write_bytes(samples.pdf(picture=samples.jpeg()))
    found = read_files(["lab.docx", "photo.png", "report.pdf"], cwd=tmp_path)
    assert found.files == ["lab.docx", "photo.png", "report.pdf"]
    assert "=== lab.docx (Word document) ===\n# Lab 3: Titration" in found.text
    assert "=== photo.png: Picture 1 ===" in found.text
    assert "=== report.pdf (PDF) ===" in found.text and "[Picture 2]" in found.text  # numbered as they're sent
    assert len(found.pictures) == 2 and found.documents
    assert b"GPS" not in found.pictures[1].data
    assert "2 pictures" in found.summary()


def test_documents_get_a_question_about_documents_and_code_one_about_code(tmp_path):
    (tmp_path / "lab.docx").write_bytes(samples.docx())
    (tmp_path / "app.py").write_text("print('hi')\n")
    _, question = code_for_review("", files=[str(tmp_path / "lab.docx")])
    assert question == DEFAULT_DOCUMENT_QUESTION
    _, question = code_for_review("", files=[str(tmp_path / "lab.docx"), str(tmp_path / "app.py")])
    assert question == DEFAULT_FILES_QUESTION


def test_a_picture_alone_is_something_to_ask_about(tmp_path):
    (tmp_path / "photo.jpg").write_bytes(samples.jpeg())
    found = check(read_files([str(tmp_path / "photo.jpg")]))
    assert len(found.pictures) == 1 and found.pictures[0].media_type == "image/jpeg"


def test_a_scan_with_no_text_says_which_pictures_are_its(tmp_path):
    (tmp_path / "scan.docx").write_bytes(samples.docx(f'<w:p>{samples.word_picture("r1")}</w:p>',
                                                      pictures={"r1": samples.png()}))
    found = read_files(["scan.docx"], cwd=tmp_path)
    assert "=== scan.docx (Word document) ===\n[Picture 1]" in found.text and len(found.pictures) == 1


@pytest.mark.parametrize("name, data, says", [
    ("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 600, "Save it as .docx"),
    ("locked.pdf", None, "password"),
    ("bad.png", b"\x89PNG\r\n\x1a\n not really", "bad.png"),
    ("drawing.gif", b"GIF89a" + b"\0" * 20, "PNG or JPEG"),
])
def test_what_can_t_be_sent_says_why(tmp_path, name, data, says):
    (tmp_path / name).write_bytes(samples.pdf(password="x") if data is None else data)
    with pytest.raises(MaterialError, match=says):
        read_files([str(tmp_path / name)])


def test_no_more_than_a_question_takes(tmp_path, monkeypatch):
    from ixel_mat import material
    names = []
    for i in range(10):
        (tmp_path / f"p{i}.png").write_bytes(samples.png(rgb=(i, 0, 0)))
        names.append(str(tmp_path / f"p{i}.png"))
    found = read_files(names)
    assert len(found.pictures) == 8 and any("at most 8" in note for note in found.notes)
    # and no more than a model takes in one question, in bytes
    monkeypatch.setattr(material, "MAX_PER_QUESTION_BYTES", 2 * len(samples.png()) + 1, raising=False)
    from ixel_mat import pictures
    monkeypatch.setattr(pictures, "MAX_PER_QUESTION_BYTES", 2 * len(read_picture(samples.png()).data) + 1)
    found = read_files(names[:4])
    assert len(found.pictures) == 2 and any("p3.png left out" in note for note in found.notes)
    assert "Picture 3" not in found.text  # numbered only when it's sent


def test_documents_share_the_panels_room(tmp_path):
    big = ("line of text " * 20 + "\n") * 400
    for name in ("a.docx", "b.docx", "c.docx"):
        body = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in big.splitlines())
        (tmp_path / name).write_bytes(samples.docx(body))
    found = check(read_files([str(tmp_path / n) for n in ("a.docx", "b.docx", "c.docx")]))
    assert len(found.text) <= 60_000
    assert any("left out" in note for note in found.notes)


# ── Pictures reach the models that see them ───────────────────────────────────

class _Seen:
    def __init__(self, sees):
        self.calls = []
        self.config = AgentConfig(name="m", label="Model", type="http", url="https://api.example.com", token="k",
                                  model="m", accepts=["image"] if sees else [])

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def send_and_receive(self, message, **kwargs):
        self.calls.append((message, kwargs.get("pictures")))
        return "It's a red square."


@pytest.mark.parametrize("sees", [True, False])
def test_ixel_ask_sends_the_pictures_or_says_they_re_there(tmp_path, monkeypatch, sees):
    (tmp_path / "photo.png").write_bytes(samples.png())
    material, question = code_for_review("What colour?", files=[str(tmp_path / "photo.png")])
    agent = _Seen(sees)
    monkeypatch.setattr(ask_mod, "create_agent", lambda cfg: agent)
    result = _run(ask_mod.ask(agent.config, question, material))
    [(message, sent)] = agent.calls
    if sees:
        assert sent == tuple(material.pictures) and "can't see" not in message and not result.notes
    else:
        assert sent is None and "which you can't see" in message
        assert any("can't see pictures" in note for note in result.notes)


def test_a_review_sends_the_files_pictures_first(tmp_path):
    (tmp_path / "photo.png").write_bytes(samples.png())
    material, question = code_for_review("What colour?", files=[str(tmp_path / "photo.png")])
    typed = read_picture(samples.png(rgb=(0, 0, 255)))
    seen = []

    class Agent:
        name, label, is_connected = "a", "A", True
        config = AgentConfig(name="a", label="A", type="http", url="https://api.example.com", accepts=["image"])

        async def send_and_receive(self, message, **kwargs):
            seen.append(kwargs.get("pictures"))
            return "Red. VERDICT: correct"

    result = _run(run_review(question, [Agent()], mode="quick", material=material, pictures=[typed]))
    assert result.pictures == 2 and seen and all(p == (material.pictures[0], typed) for p in seen)


# ── The subscription programs ─────────────────────────────────────────────────

def _preset_agent(preset_id, **override):
    fields = {k: v for k, v in PRESETS_BY_ID[preset_id].items() if k not in ("id", "why", "install", "free",
                                                                             "free_then", "model_hint")}
    return OneShotAgent(AgentConfig(name=preset_id, type="oneshot", **{"workdir": "temp", **fields, **override}))


# Grok Build has no way to be given a picture yet: an @name in the question is left as text, and image blocks in
# --prompt-file or --prompt-json reach the model as text too (Grok Build 1.0.46). Like any model that doesn't
# see pictures, it's told there are some it can't see.
NO_PICTURES = {"grok_build"}


def test_every_subscription_program_sees_pictures():
    for preset_id in PRESETS_BY_ID:
        assert _preset_agent(preset_id).config.sees_pictures is (preset_id not in NO_PICTURES), preset_id
    assert not _preset_agent("codex", accepts=[]).config.sees_pictures  # turned off in Settings
    assert not _preset_agent("codex", workdir="/some/folder").config.sees_pictures  # only into a folder of its own


def test_each_program_gets_the_pictures_its_own_way(tmp_path):
    picture = read_picture(samples.png())
    written = [(str(tmp_path / "ixel-picture-1.png"), picture)]
    cmd, stdin = _preset_agent("claude_code")._build_command("What colour?", pictures=written)
    assert cmd[-2:] == ["--input-format", "stream-json"]
    line = json.loads(stdin)
    assert line["type"] == "user" and line["message"]["content"][-1] == {"type": "text", "text": "What colour?"}
    assert line["message"]["content"][0]["source"] == {"type": "base64", "media_type": "image/png",
                                                       "data": picture.base64()}
    cmd, stdin = _preset_agent("codex")._build_command("q", pictures=written)
    assert cmd[-2:] == ["--image", "ixel-picture-1.png"] and stdin == b"q"  # by name: --image splits at commas
    cmd, stdin = _preset_agent("gemini_cli")._build_command("q", pictures=written)
    assert stdin == b"@ixel-picture-1.png \n\nq"
    cmd, _ = _preset_agent("copilot")._build_command("q", pictures=written)
    assert cmd[-2:] == ["--attachment", written[0][0]]
    cmd, _ = _preset_agent("opencode")._build_command("q", pictures=written)
    assert cmd[-2:] == ["-f", written[0][0]] and "--title" in cmd


def test_claude_code_can_t_read_a_file_an_at_path_names():
    args = PRESETS_BY_ID["claude_code"]["args"]
    settings = json.loads(args[args.index("--settings") + 1])
    assert settings == {"disableAllHooks": True, "permissions": {"deny": ["Read"]}}


def test_pictures_are_written_for_the_run_alone_and_gone_after(tmp_path):
    script = ("import os, stat, sys; sys.stdin.read(); "
              "print(sorted((n, oct(stat.S_IMODE(os.stat(n).st_mode)), os.path.getsize(n)) for n in os.listdir('.')))")
    agent = OneShotAgent(AgentConfig(name="cli", label="CLI", type="oneshot", command=sys.executable,
                                     args=["-c", script], picture_args=["--image={name}"]))
    seen = {}
    real = agent._build_command

    def build(*args, **kwargs):
        cmd, stdin = real(*args, **kwargs)
        seen["cmd"] = cmd
        return cmd, stdin

    agent._build_command = build
    picture = read_picture(samples.jpeg())

    async def go():
        await agent.connect()
        return await agent.send_and_receive("q", pictures=[picture])

    answer = _run(go())
    mode = "0o600" if os.name != "nt" else answer.split("'")[3]
    assert answer == f"[('ixel-picture-1.jpg', '{mode}', {len(picture.data)})]"
    assert seen["cmd"][-1] == "--image=ixel-picture-1.jpg"
    folder = os.path.dirname(os.path.abspath(seen["cmd"][0]))  # (the run's folder is gone: nothing to look in)
    assert folder


def test_a_program_given_the_question_in_a_file_finds_the_pictures_named_there():
    # prompt_via "file": what picture_prompt puts before the question is in the file too
    script = ("import os, sys; path = sys.argv[sys.argv.index('--prompt-file') + 1]; "
              "print(repr(open(path, encoding='utf-8').read()), sorted(os.listdir('.'))); print(path)")
    agent = OneShotAgent(AgentConfig(name="cli", label="CLI", type="oneshot", command=sys.executable,
                                     args=["-c", script], prompt_via="file", picture_prompt="@{name} "))
    assert agent.config.sees_pictures

    async def go():
        await agent.connect()
        return await agent.send_and_receive("What colour?", pictures=[read_picture(samples.png())])

    said, path = _run(go()).splitlines()
    assert said == "'@ixel-picture-1.png \\n\\nWhat colour?' ['ixel-picture-1.png']"
    assert not os.path.exists(path)  # the question's file is gone after the run


def test_no_pictures_go_to_a_program_that_doesn_t_take_them():
    agent = OneShotAgent(AgentConfig(name="cli", label="CLI", type="oneshot", command=sys.executable,
                                     args=["-c", "import os, sys; sys.stdin.read(); print(os.listdir('.'))"]))
    assert not agent.config.sees_pictures

    async def go():
        await agent.connect()
        return await agent.send_and_receive("q", pictures=[read_picture(samples.png())])

    assert _run(go()) == "[]"


def test_a_preset_brings_its_picture_settings_and_your_config_is_checked():
    configs, warnings = loader.build_agent_configs({"agents": {
        "claude": {"preset": "claude_code"},
        "mine": {"type": "oneshot", "label": "Mine", "command": "mycli", "picture_args": "--image",
                 "picture_prompt": 3, "picture_stdin": "carrier-pigeon"},
    }})
    assert configs["claude"].picture_stdin == "claude-stream-json" and configs["claude"].sees_pictures
    mine = configs["mine"]
    assert (mine.picture_args, mine.picture_prompt, mine.picture_stdin) == (None, "", "")
    assert not mine.sees_pictures
    assert len([w for w in warnings if "picture_" in w]) == 3


def test_a_web_page_named_with_file_is_code_to_review_as_it_is(tmp_path):
    (tmp_path / "index.html").write_text("<p>Hello</p><script>document.write(location.hash)</script>\n")
    found = read_files(["index.html"], cwd=tmp_path)
    assert "<script>document.write(location.hash)</script>" in found.text and not found.documents


def test_code_named_after_a_document_still_fits(tmp_path):
    lines = "".join(f"<w:p><w:r><w:t>{'line of text ' * 20}</w:t></w:r></w:p>" for _ in range(400))
    (tmp_path / "long.docx").write_bytes(samples.docx(lines))
    (tmp_path / "main.py").write_text("print('hi')\n" * 400)
    material = check(read_files(["long.docx", "main.py"], cwd=tmp_path))  # (over the limit, before)
    assert material.text.index("long.docx") < material.text.index("main.py")  # in the order named
    assert "print('hi')\n" * 400 in material.text


def test_pasted_code_leaves_the_documents_less_room(tmp_path, monkeypatch):
    lines = "".join(f"<w:p><w:r><w:t>{'line of text ' * 20}</w:t></w:r></w:p>" for _ in range(400))
    (tmp_path / "long.docx").write_bytes(samples.docx(lines))
    monkeypatch.chdir(tmp_path)
    material, _ = code_for_review("q", files=["long.docx"], code="x = 1\n" * 5000)
    assert len(material.text) <= 60_000 and "x = 1\n" * 5000 in material.text


def test_a_secrets_file_is_refused_whatever_ixel_adds_to_its_name():
    from ixel_mat.material import pasted
    for header in ("=== .env (text file) ===", "=== id_rsa: Picture 1 ===", "=== keys/.env ==="):
        with pytest.raises(MaterialError, match="looks like a secrets file"):
            check(pasted(f"{header}\nDB_PASSWORD=hunter2\n"))
    check(pasted("=== report (final).txt ===\nfine\n"))
