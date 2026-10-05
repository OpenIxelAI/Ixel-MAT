"""
A fake model API for checking that CLI agent presets are really answer-only.

It speaks just enough of the Anthropic Messages, Gemini, OpenAI Chat Completions
and OpenAI Responses streaming APIs for the official CLIs to finish a turn. Until
a conversation contains tool results, it answers with the tool calls it was given
(run a shell command, write a file, read a secret); after that it answers with
text. Every request body is kept, so a test can see which tools the CLI offered
the model and what came back from the attempted calls.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANSWER = "17 x 23 = 391"

_USAGE = {"input_tokens": 1, "output_tokens": 5, "total_tokens": 6,
          "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}


def _sse(events) -> bytes:
    out = []
    for name, data in events:
        if name:
            out.append(f"event: {name}\n")
        out.append(f"data: {data if isinstance(data, str) else json.dumps(data)}\n\n")
    return "".join(out).encode()


def has_tool_results(body: dict) -> bool:
    for message in body.get("messages") or []:  # Anthropic, OpenAI chat
        if message.get("role") == "tool":
            return True
        content = message.get("content")
        if isinstance(content, list) and any(isinstance(p, dict) and p.get("type") == "tool_result" for p in content):
            return True
    for content in body.get("contents") or []:  # Gemini
        if any("functionResponse" in part for part in content.get("parts") or []):
            return True
    items = body.get("input")  # Responses
    return isinstance(items, list) and any(str(i.get("type", "")).endswith("_output") for i in items)


def offered_tools(body: dict) -> list[str]:
    names = []
    for tool in body.get("tools") or []:
        if "functionDeclarations" in tool:
            names += [f.get("name") for f in tool["functionDeclarations"]]
        elif "function" in tool:
            names.append(tool["function"].get("name"))
        else:
            names.append(tool.get("name") or tool.get("type"))
    return names


class CaptureServer:
    """Threaded fake model API on 127.0.0.1; use as a context manager."""

    def __init__(self, tool_calls: list[dict] | None = None):
        self.tool_calls = tool_calls or []   # [{"name": ..., "args": {...}}]
        self.requests: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, body: bytes, ctype: str = "application/json"):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._send(json.dumps({"data": [{"id": "m", "object": "model"}], "models": []}).encode())

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                server.requests.append({"path": self.path, "body": body, "headers": str(self.headers),
                                        "raw": raw.decode("utf-8", "replace")})
                call = bool(server.tool_calls) and not has_tool_results(body)
                self._send(*server._reply(self.path, body, call))

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()

    # ── replies per API ───────────────────────────────────────────────────────

    def _reply(self, path: str, body: dict, call: bool) -> tuple[bytes, str]:
        model = body.get("model", "m")
        calls = self.tool_calls if call else []
        if "/messages" in path and "count_tokens" in path:
            return b'{"input_tokens": 10}', "application/json"
        if "/messages" in path:
            return self._anthropic(model, calls, stream=bool(body.get("stream")))
        if ":countTokens" in path:
            return b'{"totalTokens": 10}', "application/json"
        if ":streamGenerateContent" in path or ":generateContent" in path:
            parts = [{"functionCall": c} for c in calls] or [{"text": ANSWER}]
            chunk = {"candidates": [{"content": {"parts": parts, "role": "model"}, "finishReason": "STOP",
                                     "index": 0}],
                     "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 5, "totalTokenCount": 6}}
            if ":streamGenerateContent" in path:
                return _sse([(None, chunk)]), "text/event-stream"
            return json.dumps(chunk).encode(), "application/json"
        if path.endswith("/chat/completions"):
            return self._chat(model, calls, stream=bool(body.get("stream")))
        if path.endswith("/responses"):
            return self._responses(model, calls)
        return b"{}", "application/json"

    @staticmethod
    def _anthropic(model, calls, stream):
        msg = {"id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
               "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}
        blocks = [{"type": "tool_use", "id": f"toolu_{i}", "name": c["name"], "input": c["args"]}
                  for i, c in enumerate(calls)] or [{"type": "text", "text": ANSWER}]
        stop = "tool_use" if calls else "end_turn"
        if not stream:
            return json.dumps({**msg, "content": blocks, "stop_reason": stop}).encode(), "application/json"
        events = [("message_start", {"type": "message_start", "message": msg})]
        for i, block in enumerate(blocks):
            if block["type"] == "tool_use":
                start = {**block, "input": {}}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            else:
                start = {"type": "text", "text": ""}
                delta = {"type": "text_delta", "text": block["text"]}
            events += [("content_block_start", {"type": "content_block_start", "index": i, "content_block": start}),
                       ("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta}),
                       ("content_block_stop", {"type": "content_block_stop", "index": i})]
        events += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                      "usage": {"output_tokens": 5}}),
                   ("message_stop", {"type": "message_stop"})]
        return _sse(events), "text/event-stream"

    @staticmethod
    def _chat(model, calls, stream):
        base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": model}
        usage = {"prompt_tokens": 1, "completion_tokens": 5, "total_tokens": 6}
        tool_calls = [{"index": i, "id": f"call_{i}", "type": "function",
                       "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                      for i, c in enumerate(calls)]
        finish = "tool_calls" if calls else "stop"
        if not stream:
            message = {"role": "assistant", "content": None if calls else ANSWER}
            if calls:
                message["tool_calls"] = [{k: v for k, v in t.items() if k != "index"} for t in tool_calls]
            return json.dumps({**base, "object": "chat.completion", "usage": usage, "choices": [
                {"index": 0, "message": message, "finish_reason": finish}]}).encode(), "application/json"
        delta = {"role": "assistant", "content": None, "tool_calls": tool_calls} if calls else \
            {"role": "assistant", "content": ANSWER}
        return _sse([
            (None, {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}),
            (None, {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": usage}),
            (None, "[DONE]"),
        ]), "text/event-stream"

    @staticmethod
    def _responses(model, calls):
        if calls:
            items = [{"type": "function_call", "id": f"fc_{i}", "call_id": f"call_{i}", "name": c["name"],
                      "arguments": json.dumps(c["args"]), "status": "completed"} for i, c in enumerate(calls)]
        else:
            items = [{"type": "message", "id": "m1", "status": "completed", "role": "assistant",
                      "content": [{"type": "output_text", "text": ANSWER, "annotations": []}]}]
        resp = {"id": "r1", "object": "response", "status": "completed", "model": model, "output": items,
                "usage": _USAGE}
        events = [("response.created", {"type": "response.created",
                                        "response": {**resp, "status": "in_progress", "output": []}})]
        events += [("response.output_item.done", {"type": "response.output_item.done", "output_index": i,
                                                  "item": item}) for i, item in enumerate(items)]
        events.append(("response.completed", {"type": "response.completed", "response": resp}))
        return _sse(events), "text/event-stream"
