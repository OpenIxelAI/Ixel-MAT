"""A local stand-in for provider APIs (OpenAI-compatible + Anthropic) for tests."""
from __future__ import annotations

import asyncio
import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from aiohttp import web

Reply = tuple[int, Any, dict[str, str]]  # status, JSON body, extra headers


@dataclass
class Recorded:
    api: str                 # "openai" | "anthropic"
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: dict


def openai_reply(text: str) -> Reply:
    return 200, {"choices": [{"message": {"role": "assistant", "content": text}}]}, {}


def anthropic_reply(text: str = "", *, stop_reason: str = "end_turn", model: str = "claude-opus-5",
                    extra_blocks: list[dict] | None = None, stop_details: dict | None = None) -> Reply:
    content = list(extra_blocks or [])
    if text:
        content.append({"type": "text", "text": text})
    body = {
        "id": "msg_test", "type": "message", "role": "assistant", "model": model,
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 5},
    }
    if stop_details is not None:
        body["stop_details"] = stop_details
    return 200, body, {}


def error_reply(status: int, message: str, headers: dict[str, str] | None = None) -> Reply:
    return status, {"type": "error", "error": {"type": "invalid_request_error", "message": message}}, headers or {}


@dataclass
class FakeProvider:
    """
    handler(recorded) -> Reply. Defaults to echoing a canned answer, so tests
    only override what they care about.
    """
    handler: Callable[[Recorded], Reply] | None = None
    requests: list[Recorded] = field(default_factory=list)
    models: list[str] = field(default_factory=list)   # served at GET /v1/models
    stream_delay: float = 0.0   # between pieces, when a request asks for a streamed reply
    streams: bool = True        # False: answer every request with plain JSON, like a server without streaming
    typesafe_handler: Callable[[Recorded], Reply] | None = None  # TypeSafe's Triage at /v1/systemone
    image_handler: Callable[[Recorded], Reply] | None = None     # /v1/images/generations (default: one PNG)
    files: dict[str, tuple[bytes, str]] = field(default_factory=dict)  # GET /files/<name>: (bytes, content type)
    ollama: bool = False        # answers /api/version, and /api/pull adds the model asked for (as Ollama does)
    pulls: list[dict] = field(default_factory=list)
    port: int = 0
    _runner: web.AppRunner | None = None

    @property
    def openai_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1/chat/completions"

    @property
    def anthropic_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1/messages"

    @property
    def images_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1/images/generations"

    def file_url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/files/{name}"

    @property
    def typesafe_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1/systemone"

    async def __aenter__(self) -> "FakeProvider":
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._make_route("openai"))
        app.router.add_post("/v1/messages", self._make_route("anthropic"))
        app.router.add_post("/v1/systemone", self._make_route("triage"))
        app.router.add_get("/v1/models", self._models)
        app.router.add_post("/v1/images/generations", self._make_route("images"))
        app.router.add_get("/files/{name}", self._file)
        app.router.add_get("/api/version", self._ollama_version)
        app.router.add_post("/api/pull", self._ollama_pull)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def _file(self, request: web.Request) -> web.Response:
        body, ctype = self.files.get(request.match_info["name"], (b"", ""))
        return web.Response(body=body, content_type=ctype) if ctype else web.Response(status=404)

    async def __aexit__(self, *exc) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def _ollama_version(self, request: web.Request) -> web.Response:
        return web.json_response({"version": "0.12.3"}) if self.ollama else web.Response(status=404)

    async def _ollama_pull(self, request: web.Request) -> web.StreamResponse:
        if not self.ollama:
            return web.Response(status=404)
        body = await request.json()
        self.pulls.append(body)
        resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(request)
        for step in ({"status": "pulling manifest"}, {"status": "pulling a1b2", "total": 5_200_000_000,
                                                      "completed": 2_600_000_000}, {"status": "success"}):
            await resp.write((json.dumps(step) + "\n").encode())
            await asyncio.sleep(0.3)
        self.models.append(body["model"])
        return resp

    async def _models(self, request: web.Request) -> web.Response:
        return web.json_response({"object": "list", "data": [{"id": m, "object": "model"} for m in self.models]})

    def _make_route(self, api: str):
        async def route(request: web.Request) -> web.Response:
            recorded = Recorded(
                api=api, path=request.path, query=dict(request.query),
                headers={k.lower(): v for k, v in request.headers.items()},
                body=await request.json(),
            )
            self.requests.append(recorded)
            if api == "triage":
                status, body, headers = (self.typesafe_handler or typesafe_handler())(recorded)
                return web.json_response(body, status=status, headers=headers)
            if api == "images":
                status, body, headers = (self.image_handler or image_reply())(recorded)
                return web.json_response(body, status=status, headers=headers)
            if self.handler is not None:
                status, body, headers = self.handler(recorded)
            elif api == "anthropic":
                status, body, headers = anthropic_reply("fake answer", model=recorded.body.get("model", ""))
            else:
                status, body, headers = openai_reply("fake answer")
            if self.streams and status == 200 and recorded.body.get("stream"):
                return await self._stream(request, api, body)
            return web.json_response(body, status=status, headers=headers)
        return route

    async def _stream(self, request: web.Request, api: str, body: dict) -> web.StreamResponse:
        """The same reply as server-sent events, in a few pieces."""
        if api == "anthropic":
            text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
        else:
            text = body["choices"][0]["message"]["content"] or ""
        size = max(1, len(text) // 4 + 1)
        pieces = [text[i:i + size] for i in range(0, len(text), size)] or [""]
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)

        async def send(name, data):
            head = f"event: {name}\n" if name else ""
            await resp.write(f"{head}data: {data if isinstance(data, str) else json.dumps(data)}\n\n".encode())

        if api == "anthropic":
            msg = {**body, "content": [], "stop_reason": None}
            await send("message_start", {"type": "message_start", "message": msg})
            await send("content_block_start", {"type": "content_block_start", "index": 0,
                                               "content_block": {"type": "text", "text": ""}})
            for piece in pieces:
                await asyncio.sleep(self.stream_delay)
                await send("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                   "delta": {"type": "text_delta", "text": piece}})
            await send("content_block_stop", {"type": "content_block_stop", "index": 0})
            await send("message_delta", {"type": "message_delta", "usage": {"output_tokens": 5},
                                         "delta": {"stop_reason": body.get("stop_reason") or "end_turn",
                                                   "stop_sequence": None}})
            await send("message_stop", {"type": "message_stop"})
        else:
            base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": body.get("model", "")}
            for piece in pieces:
                await asyncio.sleep(self.stream_delay)
                await send(None, {**base, "choices": [{"index": 0, "delta": {"content": piece},
                                                       "finish_reason": None}]})
            await send(None, {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            await send(None, "[DONE]")
        await resp.write_eof()
        return resp


# ── Pictures ──────────────────────────────────────────────────────────────────

# The smallest PNG there is: one transparent pixel
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def image_reply(picture: bytes = PNG, revised: str | None = None):
    """An images/generations reply: n copies of `picture` in base64."""
    import base64

    def handler(r: Recorded) -> Reply:
        item = {"b64_json": base64.b64encode(picture).decode()}
        if revised:
            item["revised_prompt"] = revised
        return 200, {"created": 0, "data": [dict(item) for _ in range(int(r.body.get("n") or 1))]}, {}
    return handler


# ── TypeSafe's Triage ────────────────────────────────────────────────────────────

def typesafe_handler(agree: float | Callable[[Recorded], float] = 0.2, depth: float = 1.0, confidence: float = 0.9):
    """A Triage reply: every noul question gets `agree`, every score question `depth`."""
    def handler(r: Recorded) -> Reply:
        answers = {}
        for name, q in r.body["questions"].items():
            if q["type"] == "noul":
                answers[name] = {"type": "noul", "noul": agree(r) if callable(agree) else agree}
            elif q["type"] == "score":
                levels = len(q["criteria"])
                answers[name] = {"type": "score", "score": depth, "confidence": confidence,
                                 "legend": {str(i): c for i, c in enumerate(q["criteria"])},
                                 "probabilities": {str(i): 1.0 if i == round(depth) else 0.0 for i in range(levels)}}
        return 200, {"model": "ts-decision-1", "answers": answers, "usage": {"input_tokens": 120, "output_tokens": 12}}, {}
    return handler


# ── A simulated panel (answer / review / verdict) ─────────────────────────────

FENCED_ANSWER = re.compile(r"<(IXEL-[0-9a-f]+) answer ([A-Z])[^>]*>\n(.*?)\n</\1>", re.DOTALL)


def prompt_kind(prompt: str) -> str:
    if "You are the senior reviewer on a panel" in prompt:
        return "verify"
    if "sent them back: none was right yet" in prompt:
        return "fix"
    if "You are one reviewer on a panel" in prompt:
        return "review"
    if "You answered the question below." in prompt:
        return "revise"
    if "You are the moderator" in prompt:
        return "verdict"
    return "answer"


def panel_text(answers: dict[str, str], model: str, prompt: str, correct: str = "391") -> str:
    """How a panel model replies: answers[model] as its answer; honest reviews."""
    kind = prompt_kind(prompt)
    if kind == "answer":
        return answers[model]
    if kind == "review":
        reviews, best = [], None
        for _, label, text in FENCED_ANSWER.findall(prompt):
            right = correct in text
            reviews.append({"answer": label, "verdict": "correct" if right else "incorrect",
                            "errors": [] if right else [f"The correct result is {correct}."], "strengths": []})
            if right and best is None:
                best = label
        return json.dumps({"reviews": reviews, "best": best, "summary": "Checked the arithmetic."})
    if kind == "revise":
        return f"{correct} (revised)"
    if kind == "fix":
        return f"{correct} (fixed)"
    if kind == "verify":
        good = [label for _, label, text in FENCED_ANSWER.findall(prompt) if correct in text]
        if good:
            return json.dumps({"status": "confirmed", "use": good[0]})
        return json.dumps({"status": "send_back", "issues": [f"The correct result is {correct}."]})
    return verdict_reply(prompt, f"17 × 23 = {correct}", confidence="high", corrections=["One answer said 381."])


def verdict_reply(prompt: str, answer: str, **notes) -> str:
    """A moderator's reply: the answer, then its notes in the run's fence."""
    fence = re.search(r"<(IXEL-[0-9a-f]+) question>", prompt).group(1)
    notes = {"confidence": "medium", "disagreements": [], "corrections": [], **notes}
    return f"{answer}\n\n<{fence} notes>\n{json.dumps(notes)}\n</{fence}>"


def panel_handler(answers: dict[str, str]):
    def handler(r: Recorded) -> Reply:
        prompt = r.body["messages"][0]["content"]
        if isinstance(prompt, list):  # with pictures: the text part
            prompt = "".join(p.get("text", "") for p in prompt if isinstance(p, dict))
        text = panel_text(answers, r.body["model"], prompt)
        return anthropic_reply(text) if r.api == "anthropic" else openai_reply(text)
    return handler


class ThreadedFakeProvider:
    """FakeProvider on its own thread, for tests that run ixel in a subprocess."""

    def __init__(self, handler=None, image_handler=None):
        self.provider = FakeProvider(handler=handler, image_handler=image_handler)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)

    def __enter__(self) -> FakeProvider:
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self.provider.__aenter__(), self._loop).result(10)
        return self.provider

    def __exit__(self, *exc) -> None:
        asyncio.run_coroutine_threadsafe(self.provider.__aexit__(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)


# ── A slowly streaming API (the verdict as it's written) ──────────────────────

class StreamingProvider:
    """
    Streams `pieces` as server-sent events, `delay` seconds apart, in the Anthropic
    Messages or OpenAI chat format (whichever path is asked). A request without
    "stream" gets the whole text as ordinary JSON. Use with `async with`.
    """

    def __init__(self, pieces: list[str], delay: float = 0.2):
        self.pieces, self.delay = pieces, delay
        self.requests: list[dict] = []
        self._runner: web.AppRunner | None = None
        self.port = 0

    async def __aenter__(self) -> "StreamingProvider":
        app = web.Application()
        app.router.add_post("/v1/messages", self._anthropic)
        app.router.add_post("/v1/chat/completions", self._chat)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def _events(self, request, events) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for name, data, pause in events:
            if pause:
                await asyncio.sleep(self.delay)
            head = f"event: {name}\n" if name else ""
            await resp.write(f"{head}data: {data if isinstance(data, str) else json.dumps(data)}\n\n".encode())
        await resp.write_eof()
        return resp

    async def _anthropic(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.requests.append(body)
        text = "".join(self.pieces)
        if not body.get("stream"):
            status, reply, _ = anthropic_reply(text, model=body.get("model", ""))
            return web.json_response(reply, status=status)
        msg = {"id": "msg_s", "type": "message", "role": "assistant", "model": body.get("model", ""), "content": [],
               "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}
        events = [("message_start", {"type": "message_start", "message": msg}, False),
                  ("content_block_start", {"type": "content_block_start", "index": 0,
                                           "content_block": {"type": "text", "text": ""}}, False)]
        events += [("content_block_delta", {"type": "content_block_delta", "index": 0,
                                            "delta": {"type": "text_delta", "text": p}}, True) for p in self.pieces]
        events += [("content_block_stop", {"type": "content_block_stop", "index": 0}, False),
                   ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn",
                                                                         "stop_sequence": None},
                                      "usage": {"output_tokens": 5}}, False),
                   ("message_stop", {"type": "message_stop"}, False)]
        return await self._events(request, events)

    async def _chat(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.requests.append(body)
        if not body.get("stream"):
            status, reply, _ = openai_reply("".join(self.pieces))
            return web.json_response(reply, status=status)
        base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": body.get("model", "")}
        events = [(None, {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": p},
                                               "finish_reason": None}]}, True) for p in self.pieces]
        events += [(None, {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}, False),
                   (None, "[DONE]", False)]
        return await self._events(request, events)
