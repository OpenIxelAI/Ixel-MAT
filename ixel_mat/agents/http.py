"""
HTTP adapter for direct API agents — no gateway needed.

- Anthropic (api.anthropic.com): the official `anthropic` SDK.
- Everything else: OpenAI-compatible chat completions (OpenAI, xAI, Gemini's
  OpenAI endpoint, and local servers such as Ollama or LM Studio).

Pictures attached to a question go along as image parts when the agent sees pictures
(AgentConfig.sees_pictures). If the API refuses them (a model that only reads text), the call
is made again without them, and the model is told it can't see them.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Awaitable, Callable
from urllib.parse import urlparse

import aiohttp
import anthropic

from ixel_mat.agents.base import (AgentConfig, BaseAgent, cleartext_refusal, needs_api_key,
                                  sends_key_in_cleartext)
from ixel_mat.effort import model_levels, to_send
from ixel_mat.limits import UsageLimit, out_of_usage, used_up
from ixel_mat.models import ALIASES, models_url, pick_latest, provider_for_url
from ixel_mat.usage import OnUsage, Usage, anthropic_usages, openai_usage

logger = logging.getLogger("ixel_mat.agents.http")


def _masked(text: str) -> str:
    """A server's error with any key it quoted back blanked out."""
    from ixel_mat.material import mask_secrets  # material imports the agents package
    return mask_secrets(text)


# Room for the model's (adaptive) thinking plus a full answer
ANTHROPIC_MAX_TOKENS = 16000
# Server-side refusal fallback: if the model declines, the API re-runs the
# request on another Claude model instead of returning an empty turn.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_MODEL_PREFIXES = ("claude-opus-5", "claude-fable-5", "claude-mythos-5")

RETRY_STATUSES = {429, 500, 502, 503, 504, 529}

# (url, key fingerprint, alias) -> (when, model id). Per key, since two accounts on one API can
# see different models; and only for a while, so a long-running `ixel gui` or `ixel mcp` picks
# up a new release.
_RESOLVED: dict[tuple[str, str, str], tuple[float, str]] = {}
RESOLVED_FOR = 3600.0  # seconds

OnText = Callable[[str], Awaitable[None]]

PICTURES_REFUSED = ("[The user attached {n} to the question, but this model's API wouldn't take pictures, so you "
                    "can't see them. If the question depends on them, say so.]\n\n")


# What an API answers a call it won't take pictures in with: too big or a kind it doesn't take (413, 415), or
# a 400/422 whose message is about the pictures (a model that only reads text). Any other error is the call's.
PICTURE_REFUSAL_STATUSES = (400, 413, 415, 422)
_ABOUT_PICTURES = re.compile(r"image|vision|multimodal|picture|media.?type|content.?type", re.IGNORECASE)


def _about_pictures(status: int, text: str) -> bool:
    return status in (413, 415) or (status in PICTURE_REFUSAL_STATUSES and bool(_ABOUT_PICTURES.search(text)))


class PicturesRefused(RuntimeError):
    """The API refused a call with pictures in it: the model may only read text."""


def _refuses_pictures(exc: Exception) -> bool:
    return isinstance(exc, anthropic.APIStatusError) and _about_pictures(exc.status_code, str(exc))


def _pictures_word(n: int) -> str:
    return "a picture" if n == 1 else f"{n} pictures"


def _openai_content(message: str, pictures) -> str | list[dict]:
    if not pictures:
        return message
    return [{"type": "text", "text": message},
            *({"type": "image_url", "image_url": {"url": p.data_url()}} for p in pictures)]


def _anthropic_content(message: str, pictures) -> str | list[dict]:
    if not pictures:
        return message
    return [*({"type": "image", "source": {"type": "base64", "media_type": p.media_type, "data": p.base64()}}
              for p in pictures),
            {"type": "text", "text": message}]


async def _read_chat_stream(resp: aiohttp.ClientResponse, on_text: OnText,
                            model: str = "") -> tuple[str, Usage | None]:
    """
    An OpenAI-style streamed reply (server-sent events), passed to on_text as it arrives; and
    its usage, from the chunk that carries it (the last one, when asked for with include_usage).
    """
    parts: list[str] = []
    usage: Usage | None = None
    async for raw in resp.content:  # one line at a time
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        if not isinstance(chunk, dict):
            continue
        if chunk.get("error"):
            said = json.dumps(chunk["error"])
            raise (UsageLimit if out_of_usage(said) else RuntimeError)(f"API error: {_masked(said)[:200]}")
        usage = openai_usage(chunk, model) or usage
        for choice in chunk.get("choices") or []:
            text = (choice.get("delta") or {}).get("content") if isinstance(choice, dict) else None
            if isinstance(text, str) and text:
                parts.append(text)
                await on_text(text)
    return "".join(parts).strip(), usage


# At xhigh and max Claude thinks at length: room for that plus the answer, so it isn't cut off (such a long
# reply is always streamed, which the SDK requires of one this size)
ANTHROPIC_DEEP_MAX_TOKENS = 64000
MAX_RETRIES = 2
# Providers known to accept stream_options (a streamed reply then ends with its token counts);
# a local or custom server might reject a field it doesn't know
_STREAM_USAGE_PROVIDERS = ("openai", "xai")


def _is_anthropic(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "anthropic.com" or host.endswith(".anthropic.com")


def _anthropic_base_url(url: str) -> str:
    """https://api.anthropic.com/v1/messages -> https://api.anthropic.com"""
    base = url.rstrip("/")
    for suffix in ("/v1/messages", "/v1"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _retry_delay(retry_after: str | None, attempt: int) -> float:
    try:
        return min(max(float(retry_after), 0.0), 20.0)
    except (TypeError, ValueError):
        return float(2 ** attempt)


class HttpAgent(BaseAgent):
    """Direct HTTP API agent — no gateway, no WebSocket."""

    def __init__(self, config: AgentConfig, response_timeout: float | None = None):
        super().__init__(config)
        self.response_timeout = response_timeout or config.transport_timeout
        self._session: aiohttp.ClientSession | None = None
        self._anthropic: anthropic.AsyncAnthropic | None = None
        # A blank model on a known provider means its latest one, never a stale built-in name
        self._alias = config.model if config.model in ALIASES else (
            "latest" if not config.model and provider_for_url(config.url) else None)
        self.model = "" if self._alias else config.model
        self._use_fallbacks = self.model.startswith(_FALLBACK_MODEL_PREFIXES)
        self._pictures_refused = False  # its API turned pictures down once: later calls go without

    async def connect(self) -> None:
        if self._connected:
            return
        if not self.config.url:
            raise ValueError(f"Agent '{self.name}' missing API url")
        if not self.config.token and needs_api_key(self.config):
            raise ValueError(f"Agent '{self.name}' missing API token")

        # Security: the API key rides in a header, so never send it in cleartext. A keyless model server
        # on your own network (an Ollama on another PC) may use plain http://.
        parsed = urlparse(self.config.url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Agent '{self.name}': url must start with http:// or https://")
        if sends_key_in_cleartext(self.config.url, self.config.token):
            raise ValueError(cleartext_refusal(self.name, self.config.url))

        if _is_anthropic(self.config.url):
            self._anthropic = anthropic.AsyncAnthropic(
                api_key=self.config.token,
                base_url=_anthropic_base_url(self.config.url),
                timeout=self.response_timeout,
                max_retries=MAX_RETRIES,
                # The SDK follows redirects by default, and the key rides in x-api-key,
                # which (unlike Authorization) survives a redirect to another host.
                http_client=anthropic.DefaultAsyncHttpxClient(follow_redirects=False),
            )
        else:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.response_timeout),
            )
        if not self._alias and not self.model:
            await self.disconnect()
            raise ValueError(f"Agent '{self.name}': no model set (ixel model {self.name} shows the choices)")
        if self._alias:
            try:
                self.model = await self._resolve_alias(self._alias)
            except Exception:
                await self.disconnect()
                raise
            self._use_fallbacks = self.model.startswith(_FALLBACK_MODEL_PREFIXES)
        self._connected = True

    async def _resolve_alias(self, alias: str) -> str:
        """Turn "latest" / "latest-fast" into today's model id, from the provider's own list."""
        key = (self.config.url, hashlib.sha256((self.config.token or "").encode()).hexdigest(), alias)
        cached = _RESOLVED.get(key)
        if cached is not None and time.monotonic() - cached[0] < RESOLVED_FOR:
            return cached[1]
        provider = provider_for_url(self.config.url)
        if provider is None:
            raise ValueError(f"Agent '{self.name}': model = \"{alias}\" works with OpenAI, Anthropic, Gemini "
                             "and xAI; for this server, set a model by name (ixel model shows the choices)")
        try:
            models = await self._list_models()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Agent '{self.name}': couldn't look up the {alias} model: {exc}") from exc
        model = pick_latest(provider, models, alias)
        if not model:
            raise RuntimeError(f"Agent '{self.name}': the account lists no model that fits \"{alias}\"; "
                               "set one by name with: ixel model " + self.name + " <model>")
        _RESOLVED[key] = (time.monotonic(), model)
        logger.info("'%s': %s is %s", self.name, alias, model)
        return model

    async def _list_models(self) -> list[dict]:
        if self._anthropic is not None:
            page = await self._anthropic.models.list(limit=100)
            return [{"id": m.id} for m in page.data]  # newest first
        headers = {"Authorization": f"Bearer {self.config.token}"} if self.config.token else {}
        async with self._session.get(models_url(self.config.url), headers=headers, allow_redirects=False) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            data = await resp.json(content_type=None)
        return [m for m in (data.get("data") or []) if isinstance(m, dict)] if isinstance(data, dict) else []

    async def disconnect(self) -> None:
        self._connected = False
        if self._session:
            await self._session.close()
            self._session = None
        if self._anthropic:
            await self._anthropic.close()
            self._anthropic = None

    async def send(self, message: str) -> None:
        await self.send_and_receive(message)

    async def send_and_receive(self, message: str, use_full_session: bool = True, **kwargs) -> str:
        """Send message to API and return response text."""
        if not self._connected:
            raise RuntimeError(f"Agent '{self.name}' not connected")

        model = self.model
        # Only a level this model takes: the nearest one to what was picked, or none at all
        effort = to_send(model_levels(provider_for_url(self.config.url), model),
                         kwargs.get("effort") or self.config.effort or "")
        on_text = kwargs.get("on_text")  # streams the reply to it as it's written
        on_usage = kwargs.get("on_usage")  # gets the call's token counts
        pictures = tuple(kwargs.get("pictures") or ()) if self.config.sees_pictures else ()
        call = self._call_anthropic if self._anthropic is not None else self._call_openai_compat
        try:
            if pictures and not self._pictures_refused:
                try:
                    return await call(message, model, effort, on_text, on_usage, pictures)
                except PicturesRefused as exc:
                    logger.info("'%s' wouldn't take pictures (%s); asking without them", self.name, exc)
                    self._pictures_refused = True
            if pictures:
                message = PICTURES_REFUSED.format(n=_pictures_word(len(pictures))) + message
            return await call(message, model, effort, on_text, on_usage)
        except anthropic.APIStatusError as exc:
            # The SDK has already retried a 429; credits that ran out come back as a 400
            if isinstance(exc, anthropic.RateLimitError) or out_of_usage(str(exc)):
                raise UsageLimit(f"API {exc.status_code}: {str(exc)[:200]}") from exc
            raise

    async def _call_openai_compat(self, message: str, model: str, effort: str | None = None,
                                  on_text: OnText | None = None, on_usage: OnUsage | None = None,
                                  pictures=()) -> str:
        """OpenAI-compatible chat completions (OpenAI, xAI, Gemini, local servers)."""
        if self._session is None:
            raise RuntimeError(f"Agent '{self.name}' not connected")
        headers = {"Content-Type": "application/json"}
        if self.config.token:  # local model servers don't take one
            headers["Authorization"] = f"Bearer {self.config.token}"
        # No temperature: reasoning models (o-series, GPT-5, ...) reject any
        # value but their default, and the default is fine for everyone else.
        body = {
            "model": model,
            "messages": [{"role": "user", "content": _openai_content(message, pictures)}],
        }
        if effort:
            body["reasoning_effort"] = effort
        if on_text is not None:
            body["stream"] = True
            if provider_for_url(self.config.url) in _STREAM_USAGE_PROVIDERS:
                body["stream_options"] = {"include_usage": True}

        for attempt in range(MAX_RETRIES + 1):
            # No redirects: a 307/308 would re-send the prompt to wherever it points,
            # plain http:// included, bypassing the cleartext check in connect().
            async with self._session.post(self.config.url, headers=headers, json=body,
                                          allow_redirects=False) as resp:
                if resp.status == 200:
                    if on_text is not None and resp.content_type == "text/event-stream":
                        text, usage = await _read_chat_stream(resp, on_text, model)
                        if usage is not None and on_usage is not None:
                            on_usage(usage)
                        return text
                    data = await resp.json(content_type=None)  # also a server that ignored "stream"
                    usage = openai_usage(data, model)
                    if usage is not None and on_usage is not None:
                        on_usage(usage)
                    choices = (data.get("choices") or []) if isinstance(data, dict) else []
                    if not choices:
                        raise RuntimeError(f"API returned no choices: {_masked(json.dumps(data))[:200]}")
                    content = (choices[0].get("message") or {}).get("content") or ""
                    if isinstance(content, list):  # content-part arrays
                        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
                    return str(content).strip()

                if 300 <= resp.status < 400:
                    raise RuntimeError(
                        f"API redirected to {resp.headers.get('Location', '(no location)')[:200]}; "
                        "Ixel doesn't follow redirects. Set this agent's url to the final address.")
                text = await resp.text()
                if pictures and _about_pictures(resp.status, text):
                    raise PicturesRefused(f"API {resp.status}: {_masked(text)[:200]}")
                said = f"API {resp.status}: {_masked(text)[:200]}"
                if used_up(text) or resp.status == 402:  # asking again in a few seconds won't help
                    raise UsageLimit(said)
                if resp.status in RETRY_STATUSES and attempt < MAX_RETRIES:
                    delay = _retry_delay(resp.headers.get("retry-after"), attempt)
                    logger.info("'%s' got HTTP %s, retrying in %.1fs", self.name, resp.status, delay)
                    await asyncio.sleep(delay)
                    continue
                # Still turned away for too many requests after the retries: it can't answer for now
                raise (UsageLimit if resp.status == 429 or out_of_usage(text) else RuntimeError)(said)
        raise RuntimeError("unreachable")  # pragma: no cover

    async def _call_anthropic(self, message: str, model: str, effort: str | None = None,
                              on_text: OnText | None = None, on_usage: OnUsage | None = None,
                              pictures=()) -> str:
        """Anthropic Messages API through the official SDK."""
        assert self._anthropic is not None
        params = {
            "model": model,
            "max_tokens": ANTHROPIC_DEEP_MAX_TOKENS if effort in ("xhigh", "max") else ANTHROPIC_MAX_TOKENS,
            "messages": [{"role": "user", "content": _anthropic_content(message, pictures)}],
        }
        if effort:
            params["output_config"] = {"effort": effort}

        response = None
        if self._use_fallbacks:
            try:
                response = await self._anthropic_request(params, on_text, fallbacks=True)
            except anthropic.APIStatusError as exc:
                if not isinstance(exc, anthropic.BadRequestError) or "fallback" not in str(exc).lower():
                    if pictures and _refuses_pictures(exc):
                        raise PicturesRefused(str(exc)[:200]) from exc
                    raise
                # This model/account doesn't take the parameter: stop sending it
                logger.info("'%s': refusal fallbacks unavailable, continuing without", self.name)
                self._use_fallbacks = False
        if response is None:
            try:
                response = await self._anthropic_request(params, on_text, fallbacks=False)
            except anthropic.APIStatusError as exc:
                if pictures and _refuses_pictures(exc):
                    raise PicturesRefused(str(exc)[:200]) from exc
                raise
        if on_usage is not None:  # billed even when it declines
            for usage in anthropic_usages(response, model):
                on_usage(usage)

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            raise RuntimeError("Claude declined this request" + (f" ({category})" if category else ""))

        text = "\n".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()
        if response.stop_reason == "max_tokens":
            text += "\n\n[Answer cut off at the output limit.]"
        return text

    async def _anthropic_request(self, params: dict, on_text: OnText | None, *, fallbacks: bool):
        """One Messages call. With on_text it's streamed, and on_text gets the text as it's written."""
        assert self._anthropic is not None
        api = self._anthropic.beta.messages if fallbacks else self._anthropic.messages
        extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"} if fallbacks else {}
        if on_text is None and params["max_tokens"] <= ANTHROPIC_MAX_TOKENS:
            return await api.create(**params, **extra)
        async with api.stream(**params, **extra) as stream:
            async for text in stream.text_stream:
                if on_text is not None:
                    await on_text(text)
            return await stream.get_final_message()

    async def listen(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """HTTP agents don't have persistent connections — listen is a no-op."""
        while self._connected:
            await asyncio.sleep(1)
