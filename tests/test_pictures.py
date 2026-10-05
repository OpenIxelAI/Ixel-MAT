"""Pictures for the panel: only PNG and JPEG, metadata taken out, kept in memory for a while."""
import struct
import zlib

import pytest

from ixel_mat import pictures
from ixel_mat.pictures import Picture, PictureError, PictureStore, read_picture


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png(width=2, height=2, extra=b"") -> bytes:
    return (pictures.PNG_SIGNATURE + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + extra + png_chunk(b"IDAT", zlib.compress(b"\x00\xe8\xb6\x4c\xe8\xb6\x4c" * height))
            + png_chunk(b"IEND", b""))


def segment(marker: int, body: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(body) + 2) + body


def jpeg(width=640, height=480, extra=b"") -> bytes:
    return (b"\xff\xd8" + segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00") + extra
            + segment(0xDB, b"\x00" + bytes(64))
            + segment(0xC0, b"\x08" + struct.pack(">HH", height, width) + b"\x01\x01\x11\x00")
            + segment(0xC4, b"\x00" + bytes(16) + b"\x00")
            + segment(0xDA, b"\x01\x01\x00\x00\x3f\x00") + b"\x12\xff\x00\x34\xff\xd0\x56" + b"\xff\xd9")


EXIF = segment(0xE1, b"Exif\x00\x00" + b"GPS 40.7128 N 74.0060 W")
XMP = segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta>Jane's phone</x:xmpmeta>")
COMMENT = segment(0xFE, b"taken at home")
ICC = segment(0xE2, b"ICC_PROFILE\x00\x01\x01" + bytes(20))
ADOBE = segment(0xEE, b"Adobe\x00\x64\x00\x00\x00\x00\x01")


def test_a_png_keeps_its_picture_and_loses_its_text():
    clean = png(3, 5)
    picture = read_picture(png(3, 5, extra=png_chunk(b"tEXt", b"Author\x00Jane") + png_chunk(b"eXIf", b"MM\x00*GPS")
                                     + png_chunk(b"tIME", b"\x07\xea\x0a\x03\x05\x00\x00"))
                           + b"PK\x03\x04 a zip after the end")
    assert picture == Picture("image/png", clean, 3, 5)


def test_a_jpeg_keeps_only_the_headers_of_the_segments_it_needs():
    thumbnail = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x02\x01" + b"face" * 3  # a 2×1 thumbnail
    picture = read_picture(b"\xff\xd8" + segment(0xE0, thumbnail) + segment(0xEE, b"Adobe\x00\x64\x00\x00\x00\x00\x01"
                           + b"payload") + segment(0xF0, b"JPEG extension: anything") + segment(0xE0, b"JFXX\x00\x10face")
                           + jpeg()[2:])
    assert b"face" not in picture.data and b"payload" not in picture.data and b"anything" not in picture.data
    assert segment(0xE0, thumbnail[:12] + b"\x00\x00") in picture.data and ADOBE in picture.data


def test_the_store_stays_fast_and_bounded_with_many_small_pictures():
    store = PictureStore(max_count=4)
    ids = [store.add(PICTURE) for _ in range(6)]
    assert len(store._items) == 4 and store.size == 4 * len(PICTURE.data)
    with pytest.raises(PictureError, match="no longer here"):
        store.take(ids[:1])
    assert len(store.take(ids[2:])) == 4
    store.clear()
    assert store.size == 0


def test_a_jpeg_loses_exif_xmp_and_comments_and_keeps_what_decoding_needs():
    picture = read_picture(jpeg(800, 600, extra=EXIF + XMP + COMMENT + ICC + ADOBE) + b"trailing bytes")
    assert (picture.media_type, picture.width, picture.height) == ("image/jpeg", 800, 600)
    assert b"GPS" not in picture.data and b"Jane" not in picture.data and b"taken at home" not in picture.data
    assert ICC in picture.data and ADOBE in picture.data
    assert picture.data == jpeg(800, 600, extra=ICC + ADOBE)
    assert picture.data.endswith(b"\xff\xd0\x56\xff\xd9")  # the image data, restart marker and all


def test_metadata_between_the_scans_of_a_progressive_jpeg_goes_too():
    scan = segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    first, second = b"\x12\xff\x00\x34\xff\xd0\x56", b"\x78\xff\x00\x9a"
    head = (b"\xff\xd8" + segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
            + segment(0xC2, b"\x08" + struct.pack(">HH", 480, 640) + b"\x01\x01\x11\x00"))
    huffman = segment(0xC4, b"\x10" + bytes(16) + b"\x00")
    picture = read_picture(head + scan + first + EXIF + COMMENT + huffman + b"\xff\xff" + scan + second
                           + XMP + b"\xff\xd9" + EXIF)
    assert b"GPS" not in picture.data and b"taken at home" not in picture.data and b"Jane" not in picture.data
    assert picture.data == head + scan + first + huffman + scan + second + b"\xff\xd9"


@pytest.mark.parametrize("data, says", [
    (b"GIF89a" + bytes(20), "PNG or JPEG"),
    (b"<svg onload=alert(1)>", "PNG or JPEG"),
    (png()[:30], "cut short"),
    (png()[:-12], "cut short"),
    (jpeg()[:40], "cut short"),
    (b"\xff\xd8\xff\xd9", "isn.t a JPEG"),
    (jpeg()[:-2], "cut short"),
    (png(9000, 10), "at most 8000 pixels"),
    (jpeg(10, 0), "at most 8000 pixels"),
    (pictures.PNG_SIGNATURE + png_chunk(b"tEXt", b"x") + png_chunk(b"IEND", b""), "isn't a PNG"),
])
def test_anything_else_is_refused_with_a_reason(data, says):
    with pytest.raises(PictureError, match=says):
        read_picture(data)


def test_a_picture_over_the_limit_is_refused_before_reading(monkeypatch):
    monkeypatch.setattr(pictures, "MAX_BYTES", 100)
    with pytest.raises(PictureError, match="over 0 MB"):
        read_picture(png() + bytes(100))


def test_the_store_keeps_pictures_for_a_while_and_within_its_size():
    now = [0.0]
    store = PictureStore(keep_for=60, max_bytes=3 * len(png()), clock=lambda: now[0])
    a, b, c = (store.add(read_picture(png())) for _ in range(3))
    assert len(store.take([a, b, c])) == 3
    d = store.add(read_picture(png()))  # over the size: the oldest goes
    with pytest.raises(PictureError, match="no longer here"):
        store.take([a])
    assert len(store.take([b, c, d])) == 3
    now[0] = 61
    with pytest.raises(PictureError, match="no longer here"):
        store.take([d])
    assert store.size == 0


def test_a_question_takes_at_most_eight_pictures_and_only_its_own_ids():
    store = PictureStore()
    key = store.add(read_picture(png()))
    with pytest.raises(PictureError, match="at most 8"):
        store.take([key] * 9)
    with pytest.raises(PictureError):
        store.take(["../../etc/passwd"])
    with pytest.raises(PictureError):
        store.take([{"id": key}])
    assert store.take([key, key]) == [read_picture(png())] * 2


# ── Sending them ──────────────────────────────────────────────────────────────

import asyncio  # noqa: E402

from fake_providers import FakeProvider, error_reply, openai_reply  # noqa: E402
from ixel_mat.agents import http as http_mod  # noqa: E402
from ixel_mat.agents.base import AgentConfig  # noqa: E402
from ixel_mat.agents.http import HttpAgent  # noqa: E402
from ixel_mat.config import loader  # noqa: E402
from ixel_mat.modes.review import run_review  # noqa: E402

PICTURE = read_picture(png())


def ask(url, *, accepts=None, message="What's in it?", pictures=(PICTURE,), model="m"):
    async def go():
        agent = HttpAgent(AgentConfig(name="a", label="A", type="http", url=url, token="sk-test", model=model,
                                      accepts=accepts))
        await agent.connect()
        try:
            return await agent.send_and_receive(message, pictures=pictures)
        finally:
            await agent.disconnect()
    return go()


def test_a_model_that_sees_pictures_gets_them_as_image_parts():
    async def go():
        async with FakeProvider() as fake:
            await ask(fake.openai_url, accepts=["image"])
            await ask(fake.openai_url)  # a local server, not marked: text only
            return [r.body["messages"][0]["content"] for r in fake.requests]

    seeing, local = asyncio.run(go())
    assert seeing == [{"type": "text", "text": "What's in it?"},
                      {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PICTURE.base64()}}]
    assert local == "What's in it?"


def test_a_model_that_refuses_pictures_is_asked_again_without_them():
    replies = iter([error_reply(400, "this model does not support image input"), openai_reply("Can't see it.")])

    async def go():
        async with FakeProvider(handler=lambda r: next(replies)) as fake:
            answer = await ask(fake.openai_url, accepts=["image"], pictures=(PICTURE, PICTURE))
            return answer, [r.body["messages"][0]["content"] for r in fake.requests]

    answer, (first, second) = asyncio.run(go())
    assert answer == "Can't see it." and isinstance(first, list) and len(first) == 3
    assert second.startswith("[The user attached 2 pictures to the question, but this model's API wouldn't take")
    assert second.endswith("What's in it?")


def test_after_one_refusal_the_next_rounds_go_without_pictures():
    replies = iter([error_reply(413, "request too large"), openai_reply("First."), openai_reply("Second.")])

    async def go():
        async with FakeProvider(handler=lambda r: next(replies)) as fake:
            agent = HttpAgent(AgentConfig(name="a", label="A", type="http", url=fake.openai_url, token="sk-test",
                                          model="m", accepts=["image"]))
            await agent.connect()
            try:
                answers = [await agent.send_and_receive(q, pictures=(PICTURE,)) for q in ("one", "two")]
            finally:
                await agent.disconnect()
            return answers, [r.body["messages"][0]["content"] for r in fake.requests]

    answers, sent = asyncio.run(go())
    assert answers == ["First.", "Second."] and len(sent) == 3  # one refusal, then text only
    assert isinstance(sent[0], list) and all(isinstance(c, str) and c.startswith("[The user attached a picture")
                                             for c in sent[1:])


@pytest.mark.parametrize("status, message", [
    (401, "bad key"),
    (400, "prompt is too long: 210000 tokens > 200000 maximum"),
    (400, "Unsupported value: 'reasoning_effort'"),
])
def test_an_error_that_isnt_about_the_pictures_isnt_taken_for_a_refusal(status, message):
    replies = iter([error_reply(status, message)])

    async def go():
        async with FakeProvider(handler=lambda r: next(replies)) as fake:
            with pytest.raises(RuntimeError, match=str(status)):
                await ask(fake.openai_url, accepts=["image"])
            return len(fake.requests)

    assert asyncio.run(go()) == 1  # asked once: never again without the pictures, never told it can't see them


def test_claude_gets_image_blocks_before_the_text(monkeypatch):
    async def go():
        async with FakeProvider() as fake:
            monkeypatch.setattr(http_mod, "_anthropic_base_url", lambda url: f"http://127.0.0.1:{fake.port}")
            await ask("https://api.anthropic.com/v1/messages", model="claude-sonnet-5")  # known provider: on
            return fake.requests[0].body["messages"][0]["content"]

    assert asyncio.run(go()) == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PICTURE.base64()}},
        {"type": "text", "text": "What's in it?"}]


def test_which_agents_see_pictures():
    configs, warnings = loader.build_agent_configs({"agents": {
        "gpt": {"type": "http", "url": "https://api.openai.com/v1/chat/completions", "token_env": "X_KEY"},
        "text_only": {"type": "http", "url": "https://api.x.ai/v1/chat/completions", "accepts": []},
        "llava": {"type": "http", "url": "http://127.0.0.1:11434/v1/chat/completions", "accepts": ["image", "smell"]},
        "llama": {"type": "http", "url": "http://127.0.0.1:11434/v1/chat/completions"},
        "codex": {"preset": "codex", "accepts": ["image"]},
    }})
    assert {n: c.sees_pictures for n, c in configs.items()} == \
        {"gpt": True, "text_only": False, "llava": True, "llama": False, "codex": False}
    assert configs["llava"].accepts == ["image"] and any("'smell'" in w for w in warnings)


class FakeAgent:
    def __init__(self, name, sees):
        self.name = self.label = name
        self.config = type("Config", (), {"sees_pictures": sees})()
        self.is_connected = True
        self.calls = []

    async def send_and_receive(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs.get("pictures")))
        return "It's a small orange square. VERDICT: correct"


def test_every_round_sends_the_pictures_to_models_that_see_them_and_tells_the_others():
    seeing, blind = FakeAgent("Seeing", True), FakeAgent("Blind", False)
    result = asyncio.run(run_review("What colour is it?", [seeing, blind], mode="quick", pictures=[PICTURE]))
    assert result.pictures == 1 and result.to_dict()["pictures"] == 1
    assert seeing.calls and all(p == (PICTURE,) for _, p in seeing.calls)
    assert not any(prompt.startswith("[The user attached") for prompt, _ in seeing.calls)  # it sees them
    assert blind.calls and all(p is None for _, p in blind.calls)
    assert all("which you can't see" in prompt for prompt, _ in blind.calls)


def test_without_pictures_nothing_changes():
    agent = FakeAgent("A", True)
    asyncio.run(run_review("q", [agent, FakeAgent("B", True)], mode="quick"))
    assert all(p is None and not prompt.startswith("[The user attached") for prompt, p in agent.calls)


def test_a_questions_pictures_together_stay_under_what_an_api_takes(monkeypatch):
    monkeypatch.setattr(pictures, "MAX_PER_QUESTION_BYTES", 2 * len(PICTURE.data))
    store = PictureStore()
    ids = [store.add(PICTURE) for _ in range(3)]
    assert len(store.take(ids[:2])) == 2
    with pytest.raises(PictureError, match="add up to more than"):
        store.take(ids)
