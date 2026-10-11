"""E6: a call cancelled by timeout or by the slowest-wait cut-off, after the provider generated tokens,
leaves no usage record: cost line and month totals omit it, no notice. Real HttpAgent against local servers."""
import asyncio, io, json, random, sys
sys.path.insert(0, "/home/user/Ixel-MAT/tests"); sys.path.insert(0, __file__.rsplit("/", 1)[0])
from aiohttp import web
from fake_providers import FakeProvider, verdict_reply
from ixel_mat.agents.base import AgentConfig
from ixel_mat.agents.http import HttpAgent
from ixel_mat.modes import review as engine
from ixel_mat.modes.review import run_review
from ixel_mat.usage import totals, cost_line
from common import render

Q = "What is 17 × 23?"
SERVED = []   # what the slow server actually generated before the client went away

def fast_handler(r):
    prompt = r.body["messages"][0]["content"]
    text = verdict_reply(prompt, "17 × 23 = 391", confidence="high") if "You are the moderator" in prompt else "It's 391."
    return 200, {"model": "m", "choices": [{"message": {"content": text}}],
                 "usage": {"prompt_tokens": 100, "completion_tokens": 10}}, {}

async def slow_route(request):
    body = await request.json()
    if body.get("stream"):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for piece in ["The answer ", "is 391, ", "because "]:
            SERVED.append(piece)
            await resp.write(("data: " + json.dumps({"choices": [{"delta": {"content": piece}}]}) + "\n\n").encode())
            await asyncio.sleep(0.1)
        await asyncio.sleep(20)  # the provider keeps generating; the client gives up first
        return resp
    SERVED.append("(non-streamed reply being generated)")
    await asyncio.sleep(20)
    return web.json_response({"model": "m", "choices": [{"message": {"content": "17 × 23 = 391"}}],
                              "usage": {"prompt_tokens": 100, "completion_tokens": 500}})

async def main():
    app = web.Application(); app.router.add_post("/v1/chat/completions", slow_route)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
    slow_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1/chat/completions"
    async with FakeProvider(handler=fast_handler) as fake:
        def agent(name, url):
            return HttpAgent(AgentConfig(name=name, label=name.title(), type="http", url=url, token="k", model="m"))

        async def scenario(title, agents, **kw):
            print(f"\n== {title} ==")
            SERVED.clear()
            for a in agents: await a.connect()
            try:
                r = await run_review(Q, agents, rng=random.Random(1), **kw)
            finally:
                for a in agents: await a.disconnect()
            print("failures:", [(f.agent, f.round, f.error) for f in r.failures])
            print("calls:", r.calls, "| usage records:", [(u.agent, u.round, u.input_tokens, u.output_tokens, u.estimated) for u in r.usage])
            print("server generated for the cancelled call:", SERVED)
            print("cost line:", cost_line(totals(r.usage)))
            print("to_dict usage.summary:", r.to_dict()["usage"]["summary"])
            out = render(r)
            print("terminal lines:", "\n  ".join(l.strip() for l in out.splitlines() if "Cost" in l or "model calls" in l or "failed" in l.lower() or "timed out" in l or "left out" in l))
            return r

        # A: a non-streamed answer call times out (per-call limit 1.5s) while the server is still generating
        await scenario("A: answer-round timeout", [agent("fast1", fake.openai_url), agent("fast2", fake.openai_url),
                                                   agent("slow", slow_url)], mode="quick", timeout=1.5, moderator="fast1")
        # B: the moderator streams three pieces, then the call times out
        await scenario("B: verdict streamed then timed out", [agent("fast1", fake.openai_url), agent("fast2", fake.openai_url),
                                                              agent("slow", slow_url)], mode="quick", timeout=1.5, moderator="slow")
        # C: cut off by the slowest-wait (no timeout reached)
        engine.SLOWEST_WAIT_MIN = 0.3
        await scenario("C: left out by slowest-wait", [agent("fast1", fake.openai_url), agent("fast2", fake.openai_url),
                                                       agent("slow", slow_url)], mode="quick", timeout=30, moderator="fast1")
    await runner.cleanup()

asyncio.run(main())
