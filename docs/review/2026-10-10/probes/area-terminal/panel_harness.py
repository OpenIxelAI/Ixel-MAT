"""Start a fake OpenAI-compatible panel, write an isolated-home config, run ixel commands as subprocesses."""
import json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, "/home/user/Ixel-MAT/tests")
sys.path.insert(0, "/home/user/Ixel-MAT")
from fake_providers import ThreadedFakeProvider, panel_handler

S = Path(__file__).parent
HOME = S / "home2"
ANSWERS = {"m-gpt": "It's 391.", "m-claude": "17 × 23 = 391", "m-wrong": "The answer is 381."}

def write_config(url, extra=""):
    cfg_dir = HOME / ".config" / "ixel-mat"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    agents = ""
    for agent_id, (model, label) in {"gpt": ("m-gpt", "GPT"), "claude": ("m-claude", "Claude"),
                                     "gemini": ("m-wrong", "Gemini")}.items():
        agents += (f'[agents.{agent_id}]\ntype = "http"\nurl = "{url}"\ntoken_env = "IXEL_TEST_PANEL_KEY"\n'
                   f'model = "{model}"\nlabel = "{label}"\n\n')
    (cfg_dir / "config.toml").write_text(agents + extra)

def env(**over):
    e = {"PATH": os.environ["PATH"], "HOME": str(HOME), "XDG_CONFIG_HOME": str(HOME / ".config"),
         "IXEL_TEST_KEYCHAIN": "memory", "IXEL_NO_UPDATE_CHECK": "1", "PYTHONIOENCODING": "utf-8",
         "PYTHONPATH": "/home/user/Ixel-MAT", "TERM": "xterm-256color", "IXEL_TEST_PANEL_KEY": "sk-test",
         "COLUMNS": "100"}
    e.update(over)
    return e

def run(label, *args, stdin=None, timeout=90, **over):
    print(f"\n=== [{label}] ixel {' '.join(args)} ===")
    t = time.perf_counter()
    p = subprocess.run([sys.executable, "-m", "ixel_mat", *args], cwd=HOME, env=env(**over),
                       input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL,
                       capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    print(f"exit={p.returncode}  ({time.perf_counter()-t:.1f}s)")
    print("--stdout--"); print(p.stdout.rstrip())
    print("--stderr--"); print(p.stderr.rstrip())
    return p

if __name__ == "__main__":
    which = sys.argv[1:] or ["all"]
    with ThreadedFakeProvider(panel_handler(ANSWERS)) as fake:
        write_config(fake.openai_url, extra="")
        if "all" in which or "review" in which:
            run("review", "review", "What is 17 × 23?")
            p = run("review-json", "review", "--json", "What is 17 × 23?")
            try:
                json.loads(p.stdout); print("stdout is valid JSON")
            except Exception as exc:
                print("stdout NOT valid JSON:", exc)
            run("review-continue", "review", "-c", "And times 2?")
            run("review-piped", "review", stdin="Piped question?\n")
            run("review-dash", "review", "-", stdin="Dash question?\n")
            run("review-badmod", "review", "--moderator", "nobody", "x")
            run("review-json-badmod", "review", "--json", "--moderator", "nobody", "x")
        if "all" in which or "ask" in which:
            run("ask", "ask", "--agent", "gpt", "What is 17 × 23?")
            run("ask-list", "ask", "--list")
            run("ask-prefix", "ask", "--agent", "g", "x")
            run("ask-json", "ask", "--json", "--agent", "GPT", "x")
        if "all" in which or "status" in which:
            run("agents", "agents")
            run("status", "status")
            run("model", "model")
            run("config", "config")
            run("doctor", "doctor")
            run("doctor-check", "doctor", "--check")
        if "all" in which or "pipe" in which:
            run("repl-piped", "", stdin="/help\n/agents\nWhat is 17 × 23?\n/quit\n")
