"""
`ixel image`: pictures from xAI (Grok Imagine) or OpenAI, from a description or from your work.

With files or a diff attached ("make pictures of my app from the README"), one of your chat models
first reads them and writes the picture's description; only that description goes to the image
model. Without, your words go to the image model as they are. The pictures are saved as files
(PNG, JPEG or WebP, whatever the service sends), never opened or run.

Keys are the ones `ixel setup` saves (XAI_API_KEY, OPENAI_API_KEY). Settings, all optional:

    [images]
    provider = "xai"                 # which one to use when you don't say
    xai_model = "grok-imagine-image-2.0"
    openai_model = "gpt-image-1"
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlparse

from ixel_mat.agents.base import AgentConfig, is_loopback_host
from ixel_mat.material import Material

MAX_COUNT = 10
MAX_PROMPT_CHARS = 4_000
MAX_IMAGE_BYTES = 30 * 1024 * 1024
TIMEOUT_SEC = 300.0


class ImageError(ValueError):
    """Something to tell the person; no picture was made."""


@dataclass(frozen=True)
class ImageProvider:
    name: str
    label: str
    env: str               # where its key is
    url: str               # the images/generations endpoint
    model: str
    chat_host: str         # its chat API's host: a model there writes the description, if you have one
    asks_for_base64: bool  # takes response_format="b64_json" (OpenAI's image models always send base64)


PROVIDERS = {
    "xai": ImageProvider("xai", "xAI (Grok)", "XAI_API_KEY", "https://api.x.ai/v1/images/generations",
                         "grok-imagine-image-2.0", "api.x.ai", True),
    "openai": ImageProvider("openai", "OpenAI", "OPENAI_API_KEY", "https://api.openai.com/v1/images/generations",
                            "gpt-image-1", "api.openai.com", False),
}
ALIASES = {"grok": "xai", "x": "xai", "gpt": "openai", "chatgpt": "openai", "dalle": "openai", "dall-e": "openai"}

DESCRIBE_PROMPT = """Write the description an image-generation model will turn into a picture.

What the person asked for: {request}

The material above is their work. Read it, then describe one picture that fits their request and their \
work: the subject, the setting, the style, the colours and the mood, concretely, in under 150 words. \
Don't put words, logos or interface text in the picture unless they asked for them. Reply with the \
description only."""


def _section(config: dict) -> dict:
    section = config.get("images") if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


def _check_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme == "https" or (parsed.scheme == "http" and is_loopback_host(parsed.hostname)):
        return url
    raise ImageError(f"{url} isn't an https address, so Ixel won't send your key there.")


def providers(config: dict) -> list[ImageProvider]:
    """Both providers, with your [images] settings applied."""
    section = _section(config)
    out = []
    for provider in PROVIDERS.values():
        model = section.get(f"{provider.name}_model")
        url = section.get(f"{provider.name}_url")  # a proxy or compatible server (and the tests)
        out.append(replace(provider, model=model if isinstance(model, str) and model.strip() else provider.model,
                           url=_check_url(url) if isinstance(url, str) and url.strip() else provider.url))
    return out


def status(config: dict) -> list[dict]:
    """For `ixel image --list`: each provider, and whether its key is set."""
    return [{"name": p.name, "label": p.label, "model": p.model, "ready": bool(os.environ.get(p.env)),
             **({} if os.environ.get(p.env) else {"why": f"no {p.env} (ixel setup adds it)"})}
            for p in providers(config)]


def pick_provider(config: dict, wanted: str | None = None) -> ImageProvider:
    """The provider asked for (a name, or grok/gpt…), else [images] provider, else the first with a key."""
    found = {p.name: p for p in providers(config)}
    if wanted:
        key = wanted.strip().lower()
        name = ALIASES.get(key, key)
        if name not in found:
            raise ImageError(f"Ixel makes pictures with {' or '.join(p.label for p in found.values())}, "
                             f"not “{wanted}”.")
        provider = found[name]
    else:
        default = _section(config).get("provider")
        default = ALIASES.get(str(default).lower(), str(default).lower()) if default else None
        ready = [p for p in found.values() if os.environ.get(p.env)]
        provider = found.get(default) if default in found else (ready[0] if ready else None)
        if provider is None:
            raise ImageError("Making pictures needs an xAI or OpenAI key. Add one with: ixel setup")
    if not os.environ.get(provider.env):
        raise ImageError(f"{provider.label} needs a key ({provider.env}). Add it with: ixel setup")
    return provider


def pick_writer(configs: dict[str, AgentConfig], provider: ImageProvider, wanted: str | None = None,
                private: bool = False) -> AgentConfig:
    """Who reads your work and describes the picture: the one asked for, else a model at the same
    company as the pictures (Grok for xAI), else your first model with a key. With Private on, configs
    holds only your own models."""
    from ixel_mat.ask import AskError, find_agent, is_ready
    if private and not configs:
        raise ImageError("Private is on, so only a model on your own computers may read your files, and none "
                         "is set up. Describe the picture yourself, or turn Private off in Settings, under Asking.")
    if wanted:
        try:
            return find_agent(configs, wanted)
        except AskError as exc:
            if private:
                raise ImageError(f"Private is on, so only a model on your own computers may read your files. "
                                 f"{exc}") from exc
            raise ImageError(str(exc)) from exc
    ready = [c for c in configs.values() if is_ready(c)]
    for cfg in ready:
        try:
            if cfg.type == "http" and urlparse(cfg.url).hostname == provider.chat_host:
                return cfg
        except ValueError:
            continue
    if ready:
        return ready[0]
    raise ImageError("Making a picture from your files needs a chat model to read them first. Add one with: "
                     "ixel setup")


async def describe(writer: AgentConfig, request: str, material: Material, timeout: float | None = None) -> str:
    """The picture's description, written by `writer` from your request and your files."""
    from ixel_mat.ask import AskError, ask
    try:
        result = await ask(writer, DESCRIBE_PROMPT.format(request=request.strip()), material, timeout=timeout)
    except AskError as exc:
        raise ImageError(f"Couldn't describe the picture: {exc}") from exc
    return result.answer.strip().strip('"“”').strip()[:MAX_PROMPT_CHARS]


# ── The image API ─────────────────────────────────────────────────────────────

class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # the key must never follow a redirect elsewhere
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


def _open(request: urllib.request.Request, timeout: float):
    return _OPENER.open(request, timeout=timeout)


def _error_text(exc: urllib.error.HTTPError) -> str:
    try:
        data = json.loads(exc.read(100_000).decode("utf-8", "replace"))
        error = data.get("error") if isinstance(data, dict) else None
        message = error.get("message") if isinstance(error, dict) else error
        if isinstance(message, str) and message.strip():
            return message.strip()[:300]
    except (OSError, ValueError):
        pass
    return exc.reason if isinstance(exc.reason, str) else f"HTTP {exc.code}"


def image_type(data: bytes) -> str | None:
    """The file extension for these bytes, if they're a picture Ixel saves."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _download(url: str, timeout: float) -> bytes:
    _check_url(url)
    with _open(urllib.request.Request(url, headers={"User-Agent": "ixel-mat"}), timeout) as resp:
        data = resp.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError("The picture the service sent is too big to save.")
    return data


def generate(provider: ImageProvider, prompt: str, count: int = 1, size: str | None = None,
             timeout: float = TIMEOUT_SEC) -> tuple[list[bytes], list[str]]:
    """Ask for `count` pictures. Returns their bytes and any descriptions the service rewrote them from."""
    key = os.environ.get(provider.env, "")
    if not key:
        raise ImageError(f"{provider.label} needs a key ({provider.env}). Add it with: ixel setup")
    body: dict = {"model": provider.model, "prompt": prompt[:MAX_PROMPT_CHARS], "n": max(1, min(count, MAX_COUNT))}
    if provider.asks_for_base64:
        body["response_format"] = "b64_json"
    if size:
        body["size"] = size
    request = urllib.request.Request(
        _check_url(provider.url), data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "ixel-mat"})
    try:
        with _open(request, timeout) as resp:
            data = json.loads(resp.read(200 * 1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ImageError(f"{provider.label} refused the key ({_error_text(exc)}). Check it with: ixel setup") from exc
        raise ImageError(f"{provider.label} couldn't make the picture: {_error_text(exc)}") from exc
    except (OSError, ValueError) as exc:
        raise ImageError(f"Couldn't reach {provider.label}: {exc}") from exc
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise ImageError(f"{provider.label} sent back no pictures.")
    pictures, rewritten = [], []
    too_big = f"it's bigger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB"
    for item in items:
        if len(pictures) == body["n"]:  # a service that sends more than asked for doesn't fill the disk
            break
        if not isinstance(item, dict):
            continue
        try:
            if isinstance(item.get("b64_json"), str):
                # 4 base64 characters carry 3 bytes: refuse an oversized picture before decoding it
                if len(item["b64_json"]) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
                    raise ValueError(too_big)
                picture = base64.b64decode(item["b64_json"], validate=True)
                if len(picture) > MAX_IMAGE_BYTES:  # the line above rounds up to whole base64 groups
                    raise ValueError(too_big)
            elif isinstance(item.get("url"), str):
                picture = _download(item["url"], timeout)
            else:
                continue
        except (OSError, ValueError) as exc:
            raise ImageError(f"Couldn't read a picture {provider.label} sent: {exc}") from exc
        if image_type(picture) is None:
            raise ImageError(f"{provider.label} sent something that isn't a PNG, JPEG or WebP picture.")
        pictures.append(picture)
        if isinstance(item.get("revised_prompt"), str) and item["revised_prompt"].strip():
            rewritten.append(item["revised_prompt"].strip())
    if not pictures:
        raise ImageError(f"{provider.label} sent back no pictures.")
    return pictures, rewritten


def save(pictures: list[bytes], folder: Path, stem: str = "image") -> list[Path]:
    """Write the pictures as stem-1.png, stem-2.jpg…, never over a file that's already there."""
    folder.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-") or "image"
    saved, n = [], 0
    for picture in pictures:
        while True:
            n += 1
            path = folder / f"{stem}-{n}.{image_type(picture)}"
            if os.path.lexists(path):  # a link counts as taken, even to nothing: Windows' "x" open would follow it
                continue
            try:
                with open(path, "xb") as handle:
                    handle.write(picture)
                break
            except FileExistsError:
                continue
        saved.append(path.resolve())
    return saved


def default_folder() -> Path:
    pictures = Path.home() / "Pictures"
    return (pictures if pictures.is_dir() else Path.home()) / "Ixel"
