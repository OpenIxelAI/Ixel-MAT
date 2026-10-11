import sys, subprocess as sp
sys.path.insert(0, "/home/user/Ixel-MAT")
from ixel_mat.machines import terminal
planted = "/tmp/claude-0/-home-user-Ixel-MAT/c585261b-b9d6-5506-884a-19d4ae6ba125/scratchpad/area-mcp-machines/planted"
line = terminal._held(["/bin/echo", "hello from ssh stand-in"])
print("helper argv head:", line[:2])
# konsole/gnome-terminal/xterm inherit Ixel's cwd; simulate Ixel started inside a cloned repo holding subprocess.py
r = sp.run(line, input=b"\n", capture_output=True, timeout=20, cwd=planted)
print("exit:", r.returncode)
print("stdout:", r.stdout.decode()[:300])
print("stderr:", r.stderr.decode()[:600])
print("--- with -I added:")
line2 = [line[0], "-I", *line[1:]]
r = sp.run(line2, input=b"\n", capture_output=True, timeout=20, cwd=planted)
print("exit:", r.returncode, "stdout:", r.stdout.decode()[:120], "stderr:", r.stderr.decode()[:200])
