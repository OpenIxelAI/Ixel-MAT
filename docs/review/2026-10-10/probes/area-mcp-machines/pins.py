import sys, os
sys.path.insert(0, "/home/user/Ixel-MAT")
from pathlib import Path
from ixel_mat.machines import ssh, store
d = Path("/tmp/claude-0/-home-user-Ixel-MAT/c585261b-b9d6-5506-884a-19d4ae6ba125/scratchpad/area-mcp-machines/pins"); d.mkdir(exist_ok=True)
p = d / "known"; p.unlink(missing_ok=True)
K1 = ("ssh-ed25519", "AAAAC3NzaC1lZDI1NTE5AAAAIGxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx1")
K2 = ("ssh-rsa", "AAAAB3NzaC1yc2EAAAADAQABAAABAQ" + "B"*40)
K3 = ("ssh-ed25519", "AAAAC3NzaC1lZDI1NTE5AAAAIGyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy2")
print("pin a:", ssh.pin("a.example", [K1], p))
print("pin a again w/ K2 (different type, server w/ 2 keys):", end=" ")
try: print(ssh.pin("a.example", [K2], p))
except ssh.SSHError as e: print("REFUSED", e.code)
print("pin a w/ {K1,K2} (both):", ssh.pin("a.example", {K1, K2}, p))
print("pin A.EXAMPLE uppercase K3:", end=" ")
try: print(ssh.pin("A.EXAMPLE", [K3], p))
except ssh.SSHError as e: print("REFUSED", e.code)
# hand-edited multi-name line
p.write_text(p.read_text() + f"b.example,c.example {K3[0]} {K3[1]}\n")
print("pinned c before forget b:", len(ssh.pinned("c.example", p)))
print("forget b ->", ssh.forget("b.example", p), "lines; pinned c after:", len(ssh.pinned("c.example", p)))
print("names:", [ssh._valid_name(n) for n in ("a.b", "[a.b]:2222", "[::1]:2222", "::1", "a.b~0123456789", "a.b~zz", "[a.b]:70000", "-x", "a b")])
w = ssh.Where("Host.Example", 2222, "me", jump="bastion")
print("where.name:", w.name, "| address:", ssh.address(w))
print("explain changed:", ssh.explain("@@@ WARNING ...\nREMOTE HOST IDENTIFICATION HAS CHANGED!\nHost key verification failed.", store.Machine("i","n","h"), w)[0])
print("explain denied:", ssh.explain("me@h: Permission denied (publickey).", store.Machine("i","n","h"))[0])
print("explain nothing:", ssh.explain("", store.Machine("i","n","h")))
print("connect_line:", ssh.connect_line(store.Machine("i", "my box; rm -rf ~", "h")))
# --- store lock: a stale lock file left by a crashed process
s = store.Store(d / "machines.json")
(d / "machines.json.lock").write_text("stale")
m = s.save({"name": "x", "host": "x.example"})
print("saved despite stale lock file:", m.name)
# lock held by another live process -> timeout message
import subprocess, time
holder = subprocess.Popen([sys.executable, "-c", f"import fcntl,os,time; fd=os.open({str(d/'machines.json.lock')!r}, os.O_RDWR|os.O_CREAT); fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); time.sleep(30)"], stdout=subprocess.PIPE)
holder.stdout.readline()
t = time.monotonic()
try:
    import ixel_mat.machines.store as st
    with st.locked(d / "machines.json", timeout=1): pass
except store.MachineError as e: print(f"busy after {time.monotonic()-t:.1f}s:", e)
holder.kill(); holder.wait()
# leftovers from a crash mid-write
print("tmp leftovers now:", [f.name for f in d.iterdir() if ".tmp" in f.name])
