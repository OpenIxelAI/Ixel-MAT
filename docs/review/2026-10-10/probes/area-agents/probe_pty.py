import asyncio, os, sys
sys.path.insert(0, "/home/user/Ixel-MAT")
os.environ["IXEL_TEST_KEYCHAIN"] = "memory"
from ixel_mat.agents.base import AgentConfig
from ixel_mat.agents.subprocess import SubprocessAgent
chat = "import sys\nfor line in sys.stdin:\n    print('reply to', line.strip(), flush=True)\n"
async def main():
    for pty in (False, True):
        agent = SubprocessAgent(AgentConfig(name="c", label="Chat", type="subprocess", command=sys.executable,
                                            args=["-u", "-c", chat], timeout=5), use_pty=pty)
        await agent.connect()
        try:
            print(f"pty={pty}:", repr(await agent.send_and_receive("what is 2+2?")))
        finally:
            await agent.disconnect()
asyncio.run(main())
