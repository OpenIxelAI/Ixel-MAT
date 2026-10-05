"""
Sound attached in the app, written out as text: a voice note, a recording of a meeting, the sound of a
video. No model on the panel hears it; a transcription service writes it out and the text goes into your
question, where you read it (and can change it) before you ask.

The sound goes to the service you pick, with your key, and nowhere else; Ixel doesn't keep it. Keys are
the ones `ixel setup` or the app's Settings save (OPENAI_API_KEY, GROQ_API_KEY). Settings, all optional:

    [sound]
    provider = "openai"               # only this one, even when you have keys for both
    openai_model = "gpt-4o-mini-transcribe"
    groq_model = "whisper-large-v3-turbo"
"""
from __future__ import annotations

import http.client
import json
import os
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from urllib.parse import urlparse

from ixel_mat.agents.base import is_loopback_host
from ixel_mat.images import _check_url, _error_text, _open

MAX_BYTES = 25 * 1024 * 1024   # what OpenAI's and Groq's transcription take in one file
MAX_TEXT_CHARS = 50_000        # a question's limit in the app
TIMEOUT_SEC = 300.0


class SoundError(ValueError):
    """Something to tell the person; nothing was written out. `code` names what the page acts on itself
    ("no_words": one part of a long video with nothing said in it doesn't stop the rest)."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SoundProvider:
    name: str
    label: str
    env: str     # where its key is
    url: str     # the audio/transcriptions endpoint
    model: str


PROVIDERS = {
    "openai": SoundProvider("openai", "OpenAI", "OPENAI_API_KEY", "https://api.openai.com/v1/audio/transcriptions",
                            "gpt-4o-mini-transcribe"),
    "groq": SoundProvider("groq", "Groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1/audio/transcriptions",
                          "whisper-large-v3-turbo"),
}

# What MP4 and M4A sound says it is (HEIC and AVIF pictures are MP4 boxes too, and aren't sent)
_MP4_BRANDS = {b"M4A ", b"M4B ", b"M4P ", b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"dash",
               b"3gp4", b"3gp5", b"3gp6", b"3g2a", b"qt  "}


def _mpeg_audio(d: bytes) -> bool:
    """An MP3 (MPEG audio layer III or II) frame header: not just any two bytes that start FF Ex."""
    return (d[0] == 0xFF and d[1] & 0xE0 == 0xE0 and (d[1] >> 3) & 3 != 1 and (d[1] >> 1) & 3 in (1, 2)
            and d[2] >> 4 not in (0, 15) and (d[2] >> 2) & 3 != 3)


# What a recording or a sound file starts with → the name the service is told (it goes by the extension)
_KINDS = (
    (lambda d: d[:4] == b"\x1a\x45\xdf\xa3", "webm", "audio/webm"),
    (lambda d: d[:4] == b"OggS", "ogg", "audio/ogg"),
    (lambda d: d[:4] == b"RIFF" and d[8:12] == b"WAVE", "wav", "audio/wav"),
    (lambda d: d[:4] == b"fLaC", "flac", "audio/flac"),
    (lambda d: d[4:8] == b"ftyp" and d[8:12] in _MP4_BRANDS, "m4a", "audio/mp4"),
    (lambda d: d[:3] == b"ID3" or _mpeg_audio(d), "mp3", "audio/mpeg"),
)


def sound_kind(data: bytes) -> tuple[str, str] | None:
    """(extension, media type) for sound Ixel sends on, else None."""
    if len(data) < 12:
        return None
    return next(((ext, media) for test, ext, media in _KINDS if test(data)), None)


def _section(config: dict) -> dict:
    section = config.get("sound") if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


def providers(config: dict) -> list[SoundProvider]:
    """Both services, with your [sound] settings applied (an address is checked once it's the one used)."""
    section = _section(config)
    out = []
    for provider in PROVIDERS.values():
        model = section.get(f"{provider.name}_model")
        url = section.get(f"{provider.name}_url")  # a proxy or compatible server (and the tests)
        out.append(replace(provider, model=model.strip() if isinstance(model, str) and model.strip() else provider.model,
                           url=url.strip() if isinstance(url, str) and url.strip() else provider.url))
    return out


def chosen(config: dict) -> str:
    """[sound] provider as you set it ("" when you left it to Ixel)."""
    value = _section(config).get("provider")
    return value.strip().lower() if isinstance(value, str) else ""


def pick_provider(config: dict) -> SoundProvider:
    """The service you picked in [sound] (and only that one: its key must be set), else the first with a key."""
    found = {p.name: p for p in providers(config)}
    name = chosen(config)
    if name:
        provider = found.get(name)
        if provider is None:
            raise SoundError(f"[sound] provider = \"{name}\" isn't a sound service Ixel knows. Pick OpenAI or Groq "
                             "in Settings.")
        if not os.environ.get(provider.env):
            raise SoundError(f"Sound is set to go to {provider.label}, which needs a key ({provider.env}). Add it in "
                             "Settings, under Keys, or pick another sound service there.")
    else:
        provider = next((p for p in found.values() if os.environ.get(p.env)), None)
        if provider is None:
            raise SoundError("Writing out sound needs an OpenAI or Groq key. Add one in Settings, under Keys.")
    try:
        _check_url(provider.url)
    except ValueError as exc:
        raise SoundError(f"{exc} Change {provider.name}_url under [sound] in your settings file.") from None
    return provider


def _private(config: dict) -> bool:
    review = config.get("review") if isinstance(config, dict) else None
    return isinstance(review, dict) and review.get("private") is True


def _on_this_computer(url: str) -> bool:
    try:
        return is_loopback_host(urlparse(url).hostname)
    except ValueError:
        return False


def ready(config: dict) -> tuple[SoundProvider | None, str]:
    """(the service sound would go to now, "") or (None, what's needed first). With Private on ([review]
    private), sound goes nowhere but a server on this computer set as the service's address."""
    try:
        provider = pick_provider(config)
    except SoundError as exc:
        if _private(config):
            return None, PRIVATE
        return None, str(exc)
    if _private(config) and not _on_this_computer(provider.url):
        return None, (f"Private is on, and writing out sound would send it to {provider.label}, so Ixel won't. "
                      "Type the question, or turn Private off in Settings, under Asking.")
    return provider, ""


PRIVATE = ("Private is on, so sound isn't sent to OpenAI or Groq to be written out. Type the question, or "
           "turn Private off in Settings, under Asking.")


def _form(fields: dict[str, str], name: str, data: bytes, media: str) -> tuple[bytes, str]:
    boundary = "ixel-" + secrets.token_hex(16)
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in fields.items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                 f"Content-Type: {media}\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def transcribe(provider: SoundProvider, data: bytes, timeout: float = TIMEOUT_SEC) -> str:
    """The words in `data`, as text. SoundError says why not."""
    if len(data) > MAX_BYTES:
        raise SoundError(f"That's over {MAX_BYTES // (1024 * 1024)} MB of sound, more than the service takes at "
                         "once. Send a shorter piece.")
    kind = sound_kind(data)
    if kind is None:
        raise SoundError("That isn't sound Ixel can send: WebM, Ogg, WAV, FLAC, MP3 and M4A work.")
    key = os.environ.get(provider.env)
    if not key:
        raise SoundError(f"{provider.label} needs a key ({provider.env}). Add it in Settings, under Keys.")
    body, content_type = _form({"model": provider.model, "response_format": "json"}, f"sound.{kind[0]}", data,
                               kind[1])
    request = urllib.request.Request(_check_url(provider.url), data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": content_type, "User-Agent": "ixel-mat"})
    try:
        with _open(request, timeout) as resp:
            raw = resp.read(5_000_000)
            if resp.length:  # it said how long its reply was, and stopped short
                raise http.client.IncompleteRead(raw, resp.length)
    except urllib.error.HTTPError as exc:
        raise SoundError(f"{provider.label} couldn't write it out: {_error_text(exc)}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SoundError(f"Couldn't reach {provider.label}: {getattr(exc, 'reason', exc)}") from None
    except http.client.HTTPException:
        raise SoundError(f"The connection to {provider.label} broke off. Send it again.") from None
    except UnicodeError:  # in the key, which goes in a header
        raise SoundError(f"The {provider.label} key ({provider.env}) has characters a key can't have. Paste it "
                         "again in Settings, under Keys.") from None
    try:
        reply = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        reply = None
    text = reply.get("text") if isinstance(reply, dict) else None
    if not isinstance(text, str):
        raise SoundError(f"{provider.label} sent back something that isn't a transcript.")
    text = text.strip()
    if not text:
        raise SoundError("No words were heard in it.", code="no_words")
    if len(text) > MAX_TEXT_CHARS:
        raise SoundError(f"That's {len(text):,} characters written out, more than a question takes. "
                         "Send a shorter piece.")
    return text
