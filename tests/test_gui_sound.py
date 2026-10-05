"""The app's sound route: sound recorded or attached in Ask, sent on to be written out, never kept."""
import pytest

from ixel_mat import sound
from ixel_mat.gui.server import GuiServer
from ixel_mat.runtime import Settings
from test_gui_server import AUTH, TOKEN, run_with_client
from test_sound import WEBM, Transcriber

SOUND_AUTH = {**AUTH, "Content-Type": "application/octet-stream"}


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    for provider in sound.PROVIDERS.values():
        monkeypatch.delenv(provider.env, raising=False)


def make_gui(config=None):
    settings = Settings(config or {}, {})

    async def connect(cfgs, on_result=None):
        return {}

    async def disconnect(connected):
        pass

    return GuiServer(token=TOKEN, settings_loader=lambda: settings, connect=connect, disconnect=disconnect)


async def post(client, data=WEBM, headers=SOUND_AUTH):
    resp = await client.post("/api/sound", headers=headers, data=data)
    return resp.status, await resp.json()


def test_sound_is_written_out_by_the_service_and_the_panel_names_it(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    with Transcriber(reply={"text": "What does this error mean?"}) as service:
        gui = make_gui({"sound": {"groq_url": service.url}})

        async def scenario(client):
            panel = await (await client.get("/api/panel", headers=AUTH)).json()
            return panel, await post(client)

        panel, (status, data) = run_with_client(gui, scenario)
    assert panel["sound"] == {"service": "Groq", "name": "groq", "problem": ""}
    assert status == 200 and data == {"text": "What does this error mean?", "service": "Groq"}
    [sent] = service.requests
    assert sent["headers"]["Authorization"] == "Bearer gsk-test" and WEBM in sent["body"]


def test_with_no_key_the_page_is_told_what_to_add():
    gui = make_gui()

    async def scenario(client):
        panel = await (await client.get("/api/panel", headers=AUTH)).json()
        return panel, await post(client)

    panel, (status, data) = run_with_client(gui, scenario)
    assert panel["sound"]["service"] == "" and "OpenAI or Groq key" in panel["sound"]["problem"]
    assert status == 409 and "OpenAI or Groq key" in data["error"]


def test_a_key_isnt_sent_to_a_plain_http_address(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    gui = make_gui({"sound": {"provider": "openai", "openai_url": "http://transcribe.example.com/v1"}})

    async def scenario(client):
        panel = await (await client.get("/api/panel", headers=AUTH)).json()
        return panel, await post(client)

    panel, (status, data) = run_with_client(gui, scenario)
    assert panel["sound"]["service"] == "" and "isn't an https address" in panel["sound"]["problem"]
    assert status == 409 and "isn't an https address" in data["error"]


def test_sound_over_the_limit_is_refused_while_its_read(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(sound, "MAX_BYTES", 1000)
    with Transcriber() as service:
        gui = make_gui({"sound": {"openai_url": service.url}})

        async def chunks():  # no Content-Length: only counting while reading catches it
            for _ in range(20):
                yield b"\0" * 100

        async def scenario(client):
            return await post(client, data=WEBM + b"\0" * 1000), await post(client, data=chunks())

        said, streamed = run_with_client(gui, scenario)
    assert said[0] == 413 and streamed[0] == 413 and "shorter piece" in said[1]["error"]
    assert not service.requests


def test_what_the_service_says_reaches_the_page(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with Transcriber(status=400, reply={"error": {"message": "Audio file is too short."}}) as service:
        gui = make_gui({"sound": {"openai_url": service.url}})

        async def scenario(client):
            return await post(client), await post(client, data=b"<html><body>not sound</body></html>")

        (status, data), (other, refused) = run_with_client(gui, scenario)
    assert status == 400 and data["error"] == "OpenAI couldn't write it out: Audio file is too short."
    assert other == 400 and "isn't sound" in refused["error"]
    assert len(service.requests) == 1  # what isn't sound never leaves


def test_nothing_said_is_told_apart_from_a_failure(monkeypatch):
    # (a long video's part with no words in it doesn't stop the others)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with Transcriber(reply={"text": "  "}) as service:
        gui = make_gui({"sound": {"openai_url": service.url}})
        status, data = run_with_client(gui, post)
    assert status == 400 and data == {"error": "No words were heard in it.", "code": "no_words"}


def test_sound_goes_only_to_the_service_the_page_named(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    with Transcriber() as service:
        gui = make_gui({"sound": {"provider": "groq", "groq_url": service.url}})

        async def scenario(client):
            changed = await client.post("/api/sound?expect=openai", headers=SOUND_AUTH, data=WEBM)
            same = await client.post("/api/sound?expect=groq", headers=SOUND_AUTH, data=WEBM)
            return (changed.status, await changed.json()), (same.status, await same.json())

        (status, data), (ok, words) = run_with_client(gui, scenario)
    assert status == 409 and data["code"] == "sound_service_changed" and data["service"] == "Groq"
    assert "Nothing was sent" in data["error"]
    assert ok == 200 and words["service"] == "Groq"
    assert len(service.requests) == 1


@pytest.mark.parametrize("headers, status", [
    ({**AUTH, "Content-Type": "audio/webm"}, 415),
    ({**AUTH, "Content-Type": "application/json"}, 415),
    ({"Content-Type": "application/octet-stream"}, 401),
    ({**AUTH, "Content-Type": None}, 415),  # no Content-Type at all
    ({**SOUND_AUTH, "Origin": "https://evil.example"}, 403),
])
def test_only_this_page_sends_sound(monkeypatch, headers, status):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with Transcriber() as service:
        gui = make_gui({"sound": {"openai_url": service.url}})

        async def scenario(client):
            given = {k: v for k, v in headers.items() if v is not None}
            skip = [k for k, v in headers.items() if v is None]  # (else the client adds one)
            return (await client.post("/api/sound", headers=given, data=WEBM, skip_auto_headers=skip)).status

        assert run_with_client(gui, scenario) == status
    assert not service.requests
