import sys, time; sys.argv=["x"]
from pty_harness import *
with ThreadedFakeProvider(panel_handler(ANSWERS)) as fake:
    write_config(fake.openai_url)
    s = Session([])
    s.read_until(r"❯", 30); time.sleep(0.3)
    s.send("/rev"); time.sleep(0.8)  # let the completion menu appear
    s.send("\r"); time.sleep(1.0)
    s.read_until(r"NOPE", 2)
    p = s.plain(); i = p.rfind("Type a question")
    print("---- after typing /rev + Enter ----"); print(repr(p[i:i+1400]))
    s.send("/quit\r"); s.wait(10); s.kill()
