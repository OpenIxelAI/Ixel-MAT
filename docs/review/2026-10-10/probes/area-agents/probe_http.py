import asyncio, os, sys, socket
sys.path.insert(0, "/home/user/Ixel-MAT")
os.environ["IXEL_TEST_KEYCHAIN"] = "memory"
from aiohttp import web
from ixel_mat.agents.base import AgentConfig
from ixel_mat.agents.http import HttpAgent

async def serve(handler):
    app = web.Application(); app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
    return runner, site._server.sockets[0].getsockname()[1]

async def ask(url, token="k", model="m", on_text=None):
    a = HttpAgent(AgentConfig(name="a", label="A", type="http", url=url, token=token, model=model, timeout=5))
    await a.connect()
    try:
        return await a.send_and_receive("q", on_text=on_text)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        await a.disconnect()

async def main():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    print("refused   ->", await ask(f"http://127.0.0.1:{port}/v1/chat/completions"))
    async def h401(req): return web.json_response({"error": {"message": "Incorrect API key provided: sk-abc. You can find your API key at https://platform.openai.com/account/api-keys.", "type": "invalid_request_error", "code": "invalid_api_key"}}, status=401)
    runner, port = await serve(h401)
    print("401       ->", await ask(f"http://127.0.0.1:{port}/v1/chat/completions"))
    await runner.cleanup()
    async def h404(req): return web.json_response({"error": {"message": "The model `gpt-9` does not exist or you do not have access to it.", "code": "model_not_found"}}, status=404)
    runner, port = await serve(h404)
    print("404 model ->", await ask(f"http://127.0.0.1:{port}/v1/chat/completions"))
    await runner.cleanup()
    async def hslow(req): await asyncio.sleep(10); return web.json_response({})
    runner, port = await serve(hslow)
    print("timeout   ->", repr(await ask(f"http://127.0.0.1:{port}/v1/chat/completions")))
    await runner.cleanup()
    async def hlen(req):
        return web.json_response({"choices": [{"message": {"content": "cut off mid"}, "finish_reason": "length"}], "usage": {"prompt_tokens": 1, "completion_tokens": 5}})
    runner, port = await serve(hlen)
    print("length    ->", repr(await ask(f"http://127.0.0.1:{port}/v1/chat/completions")))
    await runner.cleanup()
    async def hsse(req):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"}); await resp.prepare(req)
        await resp.write(b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n')
        await resp.write(b'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":"length"}]}\n\n')
        await resp.write(b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\ndata: [DONE]\n\n')
        return resp
    runner, port = await serve(hsse)
    seen = []
    async def on_text(t): seen.append(t)
    print("sse       ->", repr(await ask(f"http://127.0.0.1:{port}/v1/chat/completions", on_text=on_text)), seen)
    await runner.cleanup()
    async def hdrop(req):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"}); await resp.prepare(req)
        await resp.write(b'data: {"choices":[{"delta":{"content":"half an ans"}}]}\n\n')
        req.transport.close()
        return resp
    runner, port = await serve(hdrop)
    seen = []
    print("dropped   ->", repr(await ask(f"http://127.0.0.1:{port}/v1/chat/completions", on_text=on_text)), seen)
    await runner.cleanup()

asyncio.run(main())
