import sys, time; sys.argv=["x"]
from pty_harness import *
with ThreadedFakeProvider(slow_handler(1.0)) as fake:
    write_config(fake.openai_url)
    log = S / "review_stdout.txt"
    s = Session(["review", "What is 17 × 23?"], stdout_to_file=str(log))
    code, took = s.wait(60)
    print("exit:", code, f"{took:.1f}s")
    print("what the terminal (stderr) showed during/after the run:", repr(s.plain()[-300:]))
    txt = log.read_text(errors="replace")
    print("stdout file lines:", len(txt.splitlines()), "| has transcript rounds:", "Round 1/3" in txt, "| has verdict:", "IXEL VERDICT" in txt)
    s.kill()
