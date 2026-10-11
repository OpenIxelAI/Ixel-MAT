import json, os, re, subprocess, sys, threading, time
from pathlib import Path
sys.path.insert(0, "/home/user/Ixel-MAT/tests")
from fake_providers import ThreadedFakeProvider, typesafe_handler, panel_text, anthropic_reply, openai_reply
from playwright.sync_api import sync_playwright
S = Path(__file__).parent; OUT = S / "shots2"; HOME = S / "home3"
ANSWERS = {"m-gpt": "It's **391**.", "m-xss": "391, as it happens.", "m-wrong": "The answer is 381."}
def handler(r):
    prompt = r.body["messages"][0]["content"]
    if "[slow]" in prompt: time.sleep(4.0)
    text = panel_text(ANSWERS, r.body["model"], prompt)
    return anthropic_reply(text) if r.api == "anthropic" else openai_reply(text)
URL_RE = re.compile(r"http://127\.0\.0\.1:\d+/#token=[A-Za-z0-9_-]+")
with ThreadedFakeProvider(handler) as fake:
    cfg = HOME / ".config" / "ixel-mat"; cfg.mkdir(parents=True, exist_ok=True)
    cfg.joinpath("config.toml").write_text("".join(
        f'[agents.{aid}]\ntype = "http"\nurl = "{fake.openai_url}"\ntoken_env = "IXEL_TEST_PANEL_KEY"\nmodel = "{model}"\nlabel = "{label}"\n\n'
        for aid, model, label in [("gpt", "m-gpt", "GPT-5"), ("claude", "m-xss", "Claude"), ("gemini", "m-wrong", "Gemini")]))
    env = {**os.environ, "HOME": str(HOME), "XDG_CONFIG_HOME": str(HOME / ".config"), "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
           "IXEL_TEST_KEYCHAIN": "memory", "IXEL_NO_UPDATE_CHECK": "1", "IXEL_TEST_PANEL_KEY": "sk-test", "OPENAI_API_KEY": "", "GROQ_API_KEY": ""}
    proc = subprocess.Popen([sys.executable, "-m", "ixel_mat", "gui", "--no-browser"], env=env, cwd=HOME, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    found = {}
    def read():
        for line in proc.stdout:
            m = URL_RE.search(line)
            if m: found.setdefault("url", m.group(0))
    threading.Thread(target=read, daemon=True).start()
    while "url" not in found: time.sleep(0.1)
    url = found["url"]
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
            # (a) /api/panel answers 500
            ctx = b.new_context(viewport={"width": 1280, "height": 800}); page = ctx.new_page()
            errs = []; page.on("pageerror", lambda e: errs.append(str(e)))
            page.route("**/api/panel", lambda route: route.fulfill(status=500, content_type="text/plain", body="Internal Server Error"))
            page.goto(url); page.wait_for_timeout(2000)
            print("PANEL-500: pageerrors =", errs)
            print("PANEL-500: notice hidden =", page.evaluate("() => document.querySelector('#notice').hidden"), "| notice =", repr(page.evaluate("() => document.querySelector('#notice').innerText")))
            print("PANEL-500: ask disabled =", page.evaluate("() => document.querySelector('#ask').disabled"), "| version =", repr(page.evaluate("() => document.querySelector('#version').textContent")), "| panel list =", repr(page.evaluate("() => document.querySelector('#panel').innerText")), "| empty lede =", repr(page.evaluate("() => document.querySelector('#empty-lede').innerText")))
            page.screenshot(path=str(OUT / "panel-500.png"))
            ctx.close()
            # (b) reload mid-review
            ctx = b.new_context(viewport={"width": 1280, "height": 800}); page = ctx.new_page()
            page.goto(url); page.wait_for_selector(".model", state="attached"); page.wait_for_timeout(600)
            page.fill("#question", "[slow] What is 17 × 23?"); page.click("#ask"); page.wait_for_timeout(1500)
            page.reload(); page.wait_for_selector(".model", state="attached"); page.wait_for_timeout(1500)
            print("RELOAD-MID: question box =", repr(page.evaluate("() => document.querySelector('#question').value")), "| convos =", repr(page.evaluate("() => document.querySelector('#convos').innerText")), "| threads =", repr(page.evaluate("() => document.querySelector('#threads').innerText")[:200]))
            # does a new question work right away (server lock released)?
            page.fill("#question", "What is 17 × 23?"); page.click("#ask")
            try:
                page.wait_for_selector(".thread:not([hidden]) .turn:last-child .verdict", timeout=30000); print("RELOAD-MID: next question answered OK")
            except Exception as e:
                print("RELOAD-MID: next question did NOT finish:", page.evaluate("() => document.querySelector('#threads').innerText")[:300])
            # (c) two tabs: ask in both at once → the second gets 409
            page2 = ctx.new_page(); page2.goto(url); page2.wait_for_selector(".model", state="attached"); page2.wait_for_timeout(600)
            page.fill("#question", "[slow] A"); page.click("#ask"); page.wait_for_timeout(500)
            page2.fill("#question", "B"); page2.click("#ask"); page2.wait_for_timeout(1500)
            print("TWO-TABS: second tab turn =", repr(page2.evaluate("() => document.querySelector('.thread:not([hidden]) .turn:last-child').innerText")[:300]))
            page2.screenshot(path=str(OUT / "two-tabs-409.png"))
            page.wait_for_function("() => !document.querySelector('.thread:not([hidden]) .turn:last-child').classList.contains('running')", timeout=60000)
            # (d) toasts on a phone after a saver-like event? skip. (e) focus after clicking Ask
            page.fill("#question", "[slow] focus"); page.click("#ask"); page.wait_for_timeout(300)
            print("FOCUS after clicking Ask (busy):", page.evaluate("() => document.activeElement.tagName + '#' + document.activeElement.id"))
            page.click("#stop"); page.wait_for_timeout(500)
            print("FOCUS after clicking Stop:", page.evaluate("() => document.activeElement.tagName + '#' + document.activeElement.id"))
            ctx.close(); b.close()
    finally:
        proc.terminate(); proc.wait(10)
