import asyncio, json, os, sys
sys.path.insert(0, "/home/user/Ixel-MAT")
os.environ["IXEL_TEST_KEYCHAIN"] = "memory"
from pathlib import Path
from websockets.asyncio.server import serve
from ixel_mat.agents import websocket as ws_agent
from ixel_mat.agents.base import AgentConfig
ws_agent._KEY_FILE = Path(__file__).parent / "device_key"

async def gateway(ws):
    await ws.send(json.dumps({"type": "event", "event": "connect.challenge", "payload": {"nonce": "n"}}))
    hello = json.loads(await ws.recv())
    await ws.send(json.dumps({"type": "res", "id": hello["id"], "ok": True, "payload": {"type": "hello-ok"}}))
    await ws.send("[]")            # a frame that is JSON but not an object (or: "null", "1")
    await asyncio.sleep(0.2)
    async for raw in ws:
        req = json.loads(raw)
        if req["method"] == "chat.send":
            await ws.send(json.dumps({"type": "res", "id": req["id"], "ok": True, "payload": {"runId": "r1"}}))
            await ws.send(json.dumps({"type": "event", "event": "chat", "payload": {"state": "final", "runId": "r1"}}))
        elif req["method"] == "chat.history":
            await ws.send(json.dumps({"type": "res", "id": req["id"], "ok": True,
                                      "payload": {"messages": [{"role": "assistant", "content": "answer"}]}}))

async def main():
    async with serve(gateway, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        agent = ws_agent.WebSocketAgent(AgentConfig(name="gw", label="GW", type="websocket",
                                                    url=f"ws://127.0.0.1:{port}", token="t"), response_timeout=3)
        await agent.connect()
        await asyncio.sleep(0.5)
        try:
            print("answer:", await agent.send_and_receive("q1"))
        except Exception as exc:
            print(f"{type(exc).__name__}: {exc}")
        finally:
            await agent.disconnect()
asyncio.run(main())
