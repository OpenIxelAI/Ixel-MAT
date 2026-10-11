"""Drive the REPL (and setup) under a real pty, against a fake panel whose models take a while."""
import fcntl, os, pty, re, select, signal, struct, subprocess, sys, termios, time
from pathlib import Path
sys.path.insert(0, "/home/user/Ixel-MAT/tests"); sys.path.insert(0, "/home/user/Ixel-MAT")
from fake_providers import ThreadedFakeProvider, panel_handler, prompt_kind

S = Path(__file__).parent
HOME = S / "home3"
ANSWERS = {"m-gpt": "It's 391.", "m-claude": "17 × 23 = 391", "m-wrong": "The answer is 381."}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[=>]|\x1b\[\?[0-9;]*[hl]")
SGR_COLOR = re.compile(r"\x1b\[[0-9;]*[34]8;|\x1b\[[0-9;]*(3[0-7]|9[0-7]|4[0-7]|10[0-7])m")

def slow_handler(delay):
    inner = panel_handler(ANSWERS)
    def handler(r):
        time.sleep(delay)
        return inner(r)
    return handler

def write_config(url):
    d = HOME / ".config" / "ixel-mat"; d.mkdir(parents=True, exist_ok=True)
    txt = ""
    for aid, (model, label) in {"gpt": ("m-gpt", "GPT"), "claude": ("m-claude", "Claude"), "gemini": ("m-wrong", "Gemini")}.items():
        txt += f'[agents.{aid}]\ntype = "http"\nurl = "{url}"\ntoken_env = "IXEL_TEST_PANEL_KEY"\nmodel = "{model}"\nlabel = "{label}"\n\n'
    (d / "config.toml").write_text(txt)

def env(**over):
    e = {"PATH": os.environ["PATH"], "HOME": str(HOME), "XDG_CONFIG_HOME": str(HOME / ".config"),
         "IXEL_TEST_KEYCHAIN": "memory", "IXEL_NO_UPDATE_CHECK": "1", "PYTHONIOENCODING": "utf-8",
         "PYTHONPATH": "/home/user/Ixel-MAT", "TERM": "xterm-256color", "IXEL_TEST_PANEL_KEY": "sk-test"}
    e.update(over); return e

class Session:
    def __init__(self, args, stdout_to_file=None, **over):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 100, 0, 0))
        out = open(stdout_to_file, "wb") if stdout_to_file else slave
        def ctty():
            os.setsid(); fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        self.proc = subprocess.Popen([sys.executable, "-m", "ixel_mat", *args], stdin=slave, stdout=out, stderr=slave,
                                     cwd=HOME, env=env(**over), preexec_fn=ctty, close_fds=True)
        os.close(slave)
        self.raw = b""
    def read_until(self, pattern, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            r, _, _ = select.select([self.master], [], [], 0.1)
            if r:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    break
                if not chunk: break
                self.raw += chunk
                if b"\x1b[6n" in chunk:  # cursor position request: answer like a terminal would
                    os.write(self.master, b"\x1b[40;1R" * chunk.count(b"\x1b[6n"))
            if re.search(pattern, self.plain()):
                return True
            if self.proc.poll() is not None and not r:
                break
        return False
    def plain(self):
        return ANSI.sub("", self.raw.decode("utf-8", "replace")).replace("\r", "")
    def send(self, data):
        os.write(self.master, data if isinstance(data, bytes) else data.encode())
    def wait(self, timeout=10):
        t = time.time()
        try:
            code = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            code = None
        return code, time.time() - t
    def kill(self):
        try: os.killpg(self.proc.pid, signal.SIGKILL)
        except Exception: pass

def tail(s, n=1800):
    p = s.plain(); return p[-n:]

def scenario_ctrl_c(fake):
    print("\n##### ctrl+c mid-review, then ctrl+c twice to exit")
    s = Session([])
    ok = s.read_until(r"agents ready", 30); print("ready:", ok)
    ok = s.read_until(r"❯", 10); print("prompt:", ok)
    s.send("What is 17 × 23?\r")
    ok = s.read_until(r"working…", 15); print("working shown:", ok)
    time.sleep(0.5); s.send("\x03")
    ok = s.read_until(r"Cancelled", 15); print("cancelled:", ok)
    ok = s.read_until(r"❯[^\n]*$", 10); print("prompt back:", ok)
    before = len(s.plain())
    s.send("\x03"); time.sleep(0.4)
    armed = s.read_until(r"again to exit", 3); print("armed hint:", armed)
    s.send("\x03")
    ok = s.read_until(r"Goodbye", 10); print("goodbye:", ok)
    code, took = s.wait(10); print("exit code:", code, f"in {took:.1f}s")
    print(tail(s, 1200)); s.kill()

def scenario_commands(fake):
    print("\n##### typed commands: 'ixel help', '/rev', '/forget', '/quit'")
    s = Session([])
    s.read_until(r"❯", 30)
    s.send("ixel help\r"); s.read_until(r"Interpreting as", 10)
    s.read_until(r"This session", 10)
    s.send("/rev\r"); s.read_until(r"Usage: /review", 10)
    s.send("/forget\r"); s.read_until(r"Unknown command", 10)
    s.send("/quit\r"); s.read_until(r"Goodbye", 10)
    code, took = s.wait(10); print("exit code:", code)
    p = s.plain()
    for needle in ("Interpreting as /help inside MAT", "Usage: /review", "Unknown command. Try /help", "Goodbye"):
        print(f"  {needle!r}: {needle in p}")
    s.kill()

def scenario_no_color(fake):
    print("\n##### NO_COLOR=1: does the prompt/rule still carry color codes?")
    s = Session([], NO_COLOR="1")
    s.read_until(r"❯", 30); time.sleep(0.5)
    raw = s.raw.decode("utf-8", "replace")
    splash_part = raw[:raw.find("agents ready")] if "agents ready" in raw else raw
    prompt_part = raw[raw.find("agents ready"):] if "agents ready" in raw else ""
    print("color SGR in Rich output (splash/connect):", bool(SGR_COLOR.search(splash_part)))
    print("color SGR in prompt_toolkit prompt/rule:", bool(SGR_COLOR.search(prompt_part)))
    m = SGR_COLOR.findall(prompt_part); print("  sample codes:", m[:5])
    s.send("\x04"); s.wait(10); s.kill()

def scenario_dumb(fake):
    print("\n##### TERM=dumb")
    s = Session([], TERM="dumb")
    ok = s.read_until(r"❯", 30); print("prompt drawn:", ok)
    time.sleep(0.5)
    raw = s.raw.decode("utf-8", "replace")
    print("escape sequences present:", raw.count("\x1b"), "| color SGR:", bool(SGR_COLOR.search(raw)))
    print("plain-lines fallback message:", "Reading plain lines" in s.plain())
    s.send("/quit\r"); s.read_until(r"Goodbye", 10)
    code, _ = s.wait(10); print("exit:", code)
    print(repr(raw[-400:])); s.kill()

def scenario_setup_ctrl_c(fake):
    print("\n##### ixel setup under a pty, ctrl+c at the first question")
    s = Session(["setup"])
    ok = s.read_until(r"[?:›❯]\s*$|\?", 30); print("first question shown:", ok)
    time.sleep(0.5)
    s.send("\x03")
    ok = s.read_until(r"Setup stopped|Interrupted|Traceback", 10)
    code, took = s.wait(10); print("exit code:", code, f"({took:.1f}s)")
    print(tail(s, 1500)); s.kill()

def scenario_review_cli_ctrl_c(fake):
    print("\n##### ixel review (non-interactive) under a pty: ctrl+c mid-run")
    s = Session(["review", "What is 17 × 23?"])
    ok = s.read_until(r"working…", 30); print("working shown:", ok)
    time.sleep(0.5); s.send("\x03")
    code, took = s.wait(15); print("exit code:", code, f"({took:.1f}s)")
    p = s.plain(); print("traceback:", "Traceback" in p, "| verdict printed:", "IXEL VERDICT" in p)
    print(repr(p[-600:])); s.kill()

def scenario_plain_mode_hang(fake):
    print("\n##### plain-line mode (stdout -> file, stdin tty): ctrl+c at the prompt; does it exit?")
    log = S / "plain_stdout.txt"
    s = Session([], stdout_to_file=str(log))
    # stdout is a file, so wait for the prompt in the file instead
    deadline = time.time() + 30
    while time.time() < deadline:
        txt = log.read_text(errors="replace") if log.exists() else ""
        if "agents ready" in txt: break
        time.sleep(0.2)
    time.sleep(1.0)
    s.send("\x03")
    code, took = s.wait(6)
    print("exit code after ctrl+c:", code, f"(waited {took:.1f}s; None = still running)")
    if code is None:
        print("still running -> pressing Enter to see if that frees it")
        s.send("\r"); code, took = s.wait(6); print("exit code after Enter:", code, f"({took:.1f}s)")
    print("stdout tail:", repr(log.read_text(errors="replace")[-300:]))
    print("stderr/pty tail:", repr(s.plain()[-300:])); s.kill()

if __name__ == "__main__":
    which = sys.argv[1:] or ["ctrlc", "clictrlc", "commands", "nocolor", "dumb", "setup", "plainhang"]
    with ThreadedFakeProvider(slow_handler(2.0 if ("ctrlc" in which or "clictrlc" in which) else 0.0)) as fake:
        write_config(fake.openai_url)
        for name in which:
            {"ctrlc": scenario_ctrl_c, "clictrlc": scenario_review_cli_ctrl_c, "commands": scenario_commands, "nocolor": scenario_no_color,
             "dumb": scenario_dumb, "setup": scenario_setup_ctrl_c, "plainhang": scenario_plain_mode_hang}[name](fake)
