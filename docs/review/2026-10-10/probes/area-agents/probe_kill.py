import asyncio, os, sys, time, subprocess
sys.path.insert(0, "/home/user/Ixel-MAT")
os.environ["IXEL_TEST_KEYCHAIN"] = "memory"
os.environ["PATH"] = os.path.dirname(__file__) + "/bin:" + os.environ["PATH"]
pids = "/tmp/claude-0/-home-user-Ixel-MAT/c585261b-b9d6-5506-884a-19d4ae6ba125/scratchpad/area-agents/pids.txt"
os.environ["IXEL_SCRATCH_PIDS"] = pids
open(pids, "w").close()
from ixel_mat.agents.base import AgentConfig
from ixel_mat.agents.oneshot import OneShotAgent

async def go():
    agent = OneShotAgent(AgentConfig(name="s", label="S", type="oneshot", command="stubborn", prompt_via="arg", timeout=1.5))
    await agent.connect()
    try:
        await agent.send_and_receive("q")
    except TimeoutError as exc:
        print("raised:", exc)

asyncio.run(go())
time.sleep(0.3)
ids = open(pids).read().split()
print("bash, sleep =", ids)
print(subprocess.run(["ps", "-o", "pid,ppid,pgid,stat,cmd", "-p", ",".join(ids)], capture_output=True, text=True).stdout)
print("my pid", os.getpid(), "ppid", os.getppid())
print(open(f"/proc/{os.getpid()}/status").read().split("PPid")[0][-200:])
