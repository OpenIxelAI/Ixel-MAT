"""Drive `ixel setup` in a pty inside an isolated home, following a script of (expect-regex, answer) steps.

usage: python drive_setup.py <scenario-name> <step>...   where a step is  REGEX=ANSWER  (ANSWER "" = Enter,
"^C" = ctrl-c, "EOF" = close stdin). Prints the whole transcript (ANSI stripped) and the files left behind.
"""
import os
import re
import shutil
import sys
import time

import pexpect

ROOT = "/tmp/claude-0/-home-user-Ixel-MAT/c585261b-b9d6-5506-884a-19d4ae6ba125/scratchpad/area-config"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r")


def main() -> int:
    name, steps = sys.argv[1], sys.argv[2:]
    home = os.path.join(ROOT, f"home-{name}")
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home)
    pre = os.path.join(ROOT, f"pre-{name}")  # files to seed the home with, if the scenario has them
    if os.path.isdir(pre):
        shutil.copytree(pre, home, dirs_exist_ok=True)
    env = {**os.environ, "HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config"),
           "IXEL_TEST_KEYCHAIN": "memory", "IXEL_NO_UPDATE_CHECK": "1", "PYTHONIOENCODING": "utf-8",
           "TERM": "xterm-256color", "COLUMNS": "110", "LINES": "50", "NO_COLOR": "1"}
    keep = set(os.environ.get("DRIVE_KEEP", "").split(","))
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY", "GOOGLE_API_KEY", "IXELMAT_GATEWAY_TOKEN",
              "TYPESAFE_API_KEY"):
        if k not in keep:
            env.pop(k, None)
    child = pexpect.spawn(sys.executable, ["-m", "ixel_mat", "setup"], cwd=home, env=env, encoding="utf-8",
                          timeout=120, dimensions=(50, 110))
    transcript = []
    child.logfile_read = type("L", (), {"write": lambda self, s: transcript.append(s), "flush": lambda self: None})()
    started = time.time()
    status = "ok"
    try:
        for step in steps:
            pattern, _, answer = step.partition("=")
            t0 = time.time()
            child.expect(pattern)
            dt = time.time() - t0
            if dt > 3:
                transcript.append(f"\n[[driver: waited {dt:.1f}s for {pattern!r}]]\n")
            if answer == "^C":
                child.sendcontrol("c")
            elif answer == "EOF":
                child.sendeof()
            else:
                child.sendline(answer)
        child.expect(pexpect.EOF)
    except pexpect.TIMEOUT:
        status = f"TIMEOUT waiting for {pattern!r} after {time.time() - started:.0f}s"
        child.terminate(force=True)
    except pexpect.EOF:
        status = f"EOF before {pattern!r}"
    child.close()
    text = ANSI.sub("", "".join(transcript))
    print(text)
    print(f"\n==== driver: {status}; exit status {child.exitstatus} signal {child.signalstatus}; "
          f"{time.time() - started:.1f}s")
    print("==== files left in", home)
    for dirpath, _, files in os.walk(home):
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            mode = oct(os.stat(p).st_mode & 0o777)
            print(f"  {mode} {os.path.relpath(p, home)} ({os.path.getsize(p)} bytes)")
            if f.endswith((".toml", ".env", ".bak")) or f == ".env":
                with open(p, encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
                print("    | " + body.replace("\n", "\n    | "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
