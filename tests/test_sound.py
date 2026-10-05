"""Sound written out as text by a transcription service (sound.py)."""
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ixel_mat import sound
from ixel_mat.sound import SoundError, pick_provider, providers, sound_kind, transcribe

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 60
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 40


class Transcriber:
    """A transcription service on 127.0.0.1 that records what it's sent."""

    def __init__(self, status=200, reply=None):
        self.requests = []
        self.delay = 0.0  # seconds before it answers
        self.queue = []   # (status, reply) to answer with in turn, before the usual one
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append({"headers": dict(self.headers), "body": body, "path": self.path})
                time.sleep(outer.delay)
                code, said = outer.queue.pop(0) if outer.queue else (status, reply)
                data = json.dumps(said if said is not None else {"text": " Hello there. "}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/audio/transcriptions"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    for provider in sound.PROVIDERS.values():
        monkeypatch.delenv(provider.env, raising=False)


def provider_at(url, monkeypatch, name="openai"):
    monkeypatch.setenv(sound.PROVIDERS[name].env, "sk-test-key")
    return pick_provider({"sound": {"provider": name, f"{name}_url": url}})


@pytest.mark.parametrize("data, kind", [
    (WEBM, "webm"), (b"OggS" + b"\x00" * 20, "ogg"), (WAV, "wav"), (b"fLaC" + b"\x00" * 20, "flac"),
    (b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 20, "m4a"), (b"ID3\x04" + b"\x00" * 20, "mp3"),
    (b"\xff\xfb\x90\x00" + b"\x00" * 20, "mp3"),
])
def test_the_kinds_of_sound_it_sends_on(data, kind):
    assert sound_kind(data)[0] == kind


@pytest.mark.parametrize("data", [b"", b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, b"<html>" + b"\x00" * 20, b"OggS"])
def test_anything_else_is_refused(data):
    assert sound_kind(data) is None


def test_the_sound_goes_to_the_service_with_its_key_and_the_words_come_back(monkeypatch):
    with Transcriber() as service:
        text = transcribe(provider_at(service.url, monkeypatch), WEBM)
    assert text == "Hello there."
    [sent] = service.requests
    assert sent["headers"]["Authorization"] == "Bearer sk-test-key"
    assert sent["headers"]["Content-Type"].startswith("multipart/form-data; boundary=ixel-")
    assert b'name="model"\r\n\r\ngpt-4o-mini-transcribe\r\n' in sent["body"]
    assert b'filename="sound.webm"\r\nContent-Type: audio/webm\r\n\r\n' + WEBM in sent["body"]


@pytest.mark.parametrize("status, reply, says", [
    (401, {"error": {"message": "Incorrect API key"}}, "Incorrect API key"),
    (200, {"text": "   "}, "No words were heard"),
    (200, {"nope": 1}, "isn't a transcript"),
    (200, {"text": "x" * 50_001}, "more than a question takes"),
])
def test_what_went_wrong_is_said(monkeypatch, status, reply, says):
    with Transcriber(status, reply) as service:
        with pytest.raises(SoundError, match=says):
            transcribe(provider_at(service.url, monkeypatch), WAV)


def test_too_much_or_the_wrong_kind_is_never_sent(monkeypatch):
    monkeypatch.setattr(sound, "MAX_BYTES", 100)
    with Transcriber() as service:
        provider = provider_at(service.url, monkeypatch)
        with pytest.raises(SoundError, match="over 0 MB"):
            transcribe(provider, WEBM + b"\x00" * 100)
        with pytest.raises(SoundError, match="isn't sound"):
            transcribe(provider, b"%PDF-1.7" + b"\x00" * 40)
    assert not service.requests


def test_which_service_is_used(monkeypatch):
    with pytest.raises(SoundError, match="OpenAI or Groq key"):
        pick_provider({})
    assert sound.ready({}) == (None, "Writing out sound needs an OpenAI or Groq key. Add one in Settings, under Keys.")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    assert pick_provider({}).name == "groq"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert pick_provider({}).name == "openai"
    assert pick_provider({"sound": {"provider": " Groq ", "groq_model": "whisper-large-v3"}}).model == "whisper-large-v3"


def test_the_service_you_picked_is_the_only_one_sound_goes_to(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    with pytest.raises(SoundError, match="set to go to OpenAI, which needs a key \\(OPENAI_API_KEY\\)"):
        pick_provider({"sound": {"provider": "openai"}})  # not Groq instead, though it has a key
    with pytest.raises(SoundError, match="isn't a sound service Ixel knows"):
        pick_provider({"sound": {"provider": "whisper.cpp"}})
    assert pick_provider({"sound": {"provider": ["groq"]}}).name == "groq"  # not a name: left to Ixel


def test_a_key_never_goes_over_plain_http_to_another_computer(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    plain = {"sound": {"openai_url": "http://transcribe.example.com/v1/audio/transcriptions"}}
    with pytest.raises(SoundError, match="isn't an https address.*openai_url"):
        pick_provider(plain)
    provider = sound.providers(plain)[0]
    with pytest.raises(ValueError, match="https"):
        transcribe(provider, WEBM)


def test_an_address_set_for_the_other_service_doesnt_stop_this_one(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert pick_provider({"sound": {"groq_url": "http://transcribe.example.com/v1"}}).name == "openai"


@pytest.mark.parametrize("data", [
    b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20,      # an iPhone photo
    b"\x00\x00\x00\x1cftypavif" + b"\x00" * 20,
    b"\xff\xfeh\x00e\x00l\x00l\x00o\x00" + b"\x00" * 20,  # UTF-16 text
    b"\xff\xf1\x50\x80\x02\x1f\xfc" + b"\x00" * 20,      # AAC in ADTS, which the services don't take
])
def test_pictures_text_and_other_lookalikes_arent_sound(data):
    assert sound_kind(data) is None


@pytest.mark.parametrize("reply", [
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 500\r\n\r\n{\"text\"",
    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n40\r\n{\"text\"",
    b"HTTP/1.1 OK-ISH\r\n\r\n",
])
def test_a_reply_cut_off_partway_is_said_plainly(monkeypatch, reply):
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve():
        conn, _ = listener.accept()
        request = b""
        while b"\r\n\r\n" not in request:  # all of it, so closing doesn't reset the connection instead
            request += conn.recv(65536)
        head, body = request.split(b"\r\n\r\n", 1)
        length = int(re.search(rb"Content-Length: (\d+)", head).group(1))
        while len(body) < length:
            body += conn.recv(65536)
        conn.sendall(reply)
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    url = f"http://127.0.0.1:{listener.getsockname()[1]}/v1/audio/transcriptions"
    with pytest.raises(SoundError, match="broke off"):
        transcribe(provider_at(url, monkeypatch), WEBM)
    listener.close()
