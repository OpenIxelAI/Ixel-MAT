"""A real review in the browser against the repo's fake providers (tests/fake_providers.py)."""
import json, os, re, subprocess, sys, threading, time
from pathlib import Path
sys.path.insert(0, "/home/user/Ixel-MAT/tests")
from fake_providers import ThreadedFakeProvider, typesafe_handler, panel_text  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

S = Path(__file__).parent
OUT = S / "shots2"; OUT.mkdir(exist_ok=True)
HOME = S / "home2"
LOG = []
def log(*a):
    line = " ".join(str(x) for x in a); print(line, flush=True); LOG.append(line)

XSS = ('391, as it happens. <img src=x onerror="window.__pwned=1"> <script>window.__pwned=2</script> '
       '[click me](javascript:window.__pwned=3) **bold** and `code`\n\n```js\nwindow.__pwned = 4\n```')
ANSWERS = {"m-gpt": "It's **391**.\n\n- 17 × 20 = 340\n- 17 × 3 = 51\n- 340 + 51 = 391",
           "m-xss": XSS, "m-wrong": "The answer is 381."}
BIG = {**ANSWERS, "m-wrong": "391 " + "**a** " * 80000}   # one line, 80k inline tokens

def handler(r):
    prompt = r.body["messages"][0]["content"]
    if isinstance(prompt, list): prompt = "".join(p.get("text", "") for p in prompt if isinstance(p, dict))
    if "[slow]" in prompt: time.sleep(3.0)
    answers = BIG if "[big]" in prompt else ANSWERS
    text = panel_text(answers, r.body["model"], prompt)
    from fake_providers import anthropic_reply, openai_reply
    return anthropic_reply(text) if r.api == "anthropic" else openai_reply(text)

URL_RE = re.compile(r"http://127\.0\.0\.1:\d+/#token=[A-Za-z0-9_-]+")

def main():
    with ThreadedFakeProvider(handler) as fake:
        fake.stream_delay = 0.15
        fake.typesafe_handler = typesafe_handler(agree=0.2, depth=0.2)
        cfg = HOME / ".config" / "ixel-mat"; cfg.mkdir(parents=True, exist_ok=True)
        cfg.joinpath("config.toml").write_text("".join(
            f'[agents.{aid}]\ntype = "http"\nurl = "{fake.openai_url}"\ntoken_env = "IXEL_TEST_PANEL_KEY"\n'
            f'model = "{model}"\nlabel = "{label}"\n\n'
            for aid, model, label in [("gpt", "m-gpt", "GPT-5"), ("claude", "m-xss", "Claude"), ("gemini", "m-wrong", "Gemini")])
            + '[saver]\nverifier = "claude"\n' + f'[triage]\nenabled = true\nskip_review = true\nurl = "{fake.typesafe_url}"\n')
        env = {**os.environ, "HOME": str(HOME), "XDG_CONFIG_HOME": str(HOME / ".config"), "PYTHONIOENCODING": "utf-8",
               "PYTHONUNBUFFERED": "1", "IXEL_TEST_KEYCHAIN": "memory", "IXEL_NO_UPDATE_CHECK": "1",
               "IXEL_TEST_PANEL_KEY": "sk-test", "TYPESAFE_API_KEY": "ts-test", "OPENAI_API_KEY": "", "GROQ_API_KEY": ""}
        proc = subprocess.Popen([sys.executable, "-m", "ixel_mat", "gui", "--no-browser"], env=env, cwd=HOME,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        found = {}
        def read():
            for line in proc.stdout:
                m = URL_RE.search(line)
                if m and "url" not in found: found["url"] = m.group(0)
                LOG.append("[server] " + line.rstrip())
        threading.Thread(target=read, daemon=True).start()
        for _ in range(300):
            if "url" in found or proc.poll() is not None: break
            time.sleep(0.1)
        url = found["url"]; log("URL", url)
        try:
            run(url)
        finally:
            proc.terminate(); proc.wait(10)
    (OUT / "log.txt").write_text("\n".join(LOG))

def wire(page, tag):
    page.on("pageerror", lambda e: LOG.append(f"[{tag}] PAGEERROR: {e}"))
    page.on("console", lambda m: LOG.append(f"[{tag}] console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)

def open_app(page, url):
    page.goto(url); page.wait_for_selector(".model", state="attached"); page.wait_for_timeout(600)

def injected(page):
    return {"pwned": page.evaluate("window.__pwned"), "images": page.locator('img:not([src="/mark.svg"])').count(),
            "scripts": page.locator("script").count() - 2, "links": page.locator("a").count()}

LAST = ".thread:not([hidden]) .turn:last-child"

def ask(page, text, wait_verdict=True, timeout=60000):
    page.fill("#question", text); page.click("#ask")
    if wait_verdict:
        page.wait_for_selector(f"{LAST} .verdict", timeout=timeout)
    page.wait_for_function(f"() => !document.querySelector('{LAST}').classList.contains('running')", timeout=timeout)
    page.wait_for_timeout(500)

def tall(page, path, width):
    h = page.evaluate("() => document.querySelector('#scroller').scrollHeight")
    page.set_viewport_size({"width": width, "height": min(max(800, h + 160), 6000)}); page.wait_for_timeout(300)
    page.screenshot(path=str(path)); page.set_viewport_size({"width": width, "height": 800 if width > 500 else 844})

def run(url):
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
        # ── desk light: a full review, tabs, follow-up ──
        ctx = b.new_context(viewport={"width": 1280, "height": 800}, color_scheme="light"); page = ctx.new_page(); wire(page, "light")
        open_app(page, url)
        page.screenshot(path=str(OUT / "light-ask-empty-with-panel.png"))
        log("PANEL sidebar:", page.evaluate("() => document.querySelector('#sidebar').innerText").replace("\n", " | "))
        log("MODE HINT:", page.evaluate("() => document.querySelector('#mode-hint').innerText"))
        page.fill("#question", "What is 17 × 23?"); page.click("#ask"); page.wait_for_timeout(700)
        page.screenshot(path=str(OUT / "light-ask-running.png"))
        page.wait_for_selector(f"{LAST} .verdict", timeout=60000)
        page.wait_for_function(f"() => !document.querySelector('{LAST}').classList.contains('running')", timeout=60000)
        page.wait_for_timeout(600)
        page.screenshot(path=str(OUT / "light-ask-verdict.png"))
        tall(page, OUT / "light-ask-verdict-full.png", 1280)
        log("INJECTED after review:", injected(page))
        log("TURN text:", page.evaluate(f"() => document.querySelector('{LAST}').innerText")[:1500].replace("\n", " | "))
        for tab in ("answers", "review", "problems"):
            if page.query_selector(f'{LAST} .tab[data-tab="{tab}"]'):
                page.click(f'{LAST} .tab[data-tab="{tab}"]'); page.wait_for_timeout(300)
                tall(page, OUT / f"light-ask-tab-{tab}.png", 1280)
        log("INJECTED after tabs:", injected(page))
        # follow-up
        ask(page, "And 17 × 24?")
        page.screenshot(path=str(OUT / "light-ask-followup.png"))
        log("FOLLOWUP bar:", page.evaluate("() => document.querySelector('#followup').innerText"), "| hidden:", page.evaluate("() => document.querySelector('#followup').hidden"))
        # Stop flow
        page.fill("#question", "[slow] What is 17 × 23?"); page.click("#ask"); page.wait_for_timeout(1200)
        page.screenshot(path=str(OUT / "light-ask-stop-before.png"))
        page.click("#stop"); page.wait_for_timeout(800)
        log("STOPPED turn:", page.evaluate(f"() => document.querySelector('{LAST}').innerText")[:400].replace("\n", " | "))
        log("STOPPED question box:", repr(page.evaluate("() => document.querySelector('#question').value")))
        page.screenshot(path=str(OUT / "light-ask-stopped.png"))
        # Saver
        page.fill("#question", ""); page.click('.modes button[data-mode="saver"]'); page.wait_for_timeout(200)
        log("SAVER hint:", page.evaluate("() => document.querySelector('#mode-hint').innerText"))
        ask(page, "What is 17 × 23? (saver)")
        page.wait_for_timeout(800)
        page.screenshot(path=str(OUT / "light-ask-saver.png"))
        log("SAVES box:", page.evaluate("() => document.querySelector('#saver-box').innerText").replace("\n", " | "), "| toasts:", page.evaluate("() => document.querySelector('#toasts').innerText").replace("\n", " | "))
        page.click('.modes button[data-mode="review"]')
        # Settings with a config file
        page.click('.rail-item[data-view="settings"]')
        page.wait_for_function("() => !document.querySelector('#settings-body').textContent.includes('Reading your settings')", timeout=10000)
        page.wait_for_timeout(2500)  # lists
        page.screenshot(path=str(OUT / "light-settings-viewport.png"))
        h = page.evaluate("() => document.querySelector('#view-settings .scroller').scrollHeight")
        page.set_viewport_size({"width": 1280, "height": min(h + 160, 6000)}); page.wait_for_timeout(300)
        page.screenshot(path=str(OUT / "light-settings-full.png")); page.set_viewport_size({"width": 1280, "height": 800})
        log("MODELS card:", page.evaluate("() => document.querySelector('[aria-labelledby=set-h-models]').innerText")[:1200].replace("\n", " | "))
        # change a setting and see Ask follow
        page.select_option('select[data-key="review:mode"]', "quick"); page.wait_for_timeout(1500)
        log("REVIEW note:", page.evaluate("() => document.querySelector('[data-note=review]').innerText"))
        page.click('.rail-item[data-view="ask"]'); page.wait_for_timeout(500)
        log("ASK mode after settings change (modePicked was true, so should stay review):", page.evaluate("() => document.querySelector('.modes button.on').dataset.mode"))
        ctx.close()

        # ── race: a review under way while Settings changes the panel ──
        ctx = b.new_context(viewport={"width": 1280, "height": 800}, color_scheme="light"); page = ctx.new_page(); wire(page, "race")
        open_app(page, url)
        page.fill("#question", "[slow] What is 17 × 23?"); page.click("#ask"); page.wait_for_timeout(400)
        page.click('.rail-item[data-view="settings"]')
        page.wait_for_function("() => !document.querySelector('#settings-body').textContent.includes('Reading your settings')", timeout=10000)
        page.wait_for_timeout(400)
        page.uncheck('input[data-key="agent:gemini:on"]'); page.wait_for_timeout(1500)
        log("RACE models note:", page.evaluate("() => document.querySelector('[data-note=models]').innerText"))
        page.click('.rail-item[data-view="ask"]')
        page.wait_for_function(f"() => !document.querySelector('{LAST}').classList.contains('running')", timeout=90000)
        page.wait_for_timeout(1200)
        log("RACE turn status:", page.evaluate(f"() => document.querySelector('{LAST} .status').innerText"))
        log("RACE panel after run:", page.evaluate("() => document.querySelector('#panel').innerText").replace("\n", " | "))
        log("RACE grid rows:", page.evaluate(f"() => [...document.querySelectorAll('{LAST} .grid tbody th .name')].map(n => n.innerText)"))
        page.screenshot(path=str(OUT / "race-after.png"))
        # put gemini back
        page.click('.rail-item[data-view="settings"]'); page.wait_for_timeout(800)
        page.check('input[data-key="agent:gemini:on"]'); page.wait_for_timeout(1500)
        ctx.close()

        # ── the big answer: does the result render? ──
        ctx = b.new_context(viewport={"width": 1280, "height": 800}, color_scheme="light"); page = ctx.new_page(); wire(page, "big")
        open_app(page, url)
        page.fill("#question", "[big] What is 17 × 23?"); page.click("#ask")
        page.wait_for_function(f"() => !document.querySelector('{LAST}').classList.contains('running')", timeout=120000)
        page.wait_for_timeout(1000)
        log("BIG status:", page.evaluate(f"() => document.querySelector('{LAST} .status').innerText"))
        log("BIG has verdict:", page.locator(f"{LAST} .verdict").count(), "| result slot text:", page.evaluate(f"() => document.querySelector('{LAST} .result').innerText")[:300].replace("\n", " | "))
        page.screenshot(path=str(OUT / "big-after.png"))
        page.reload(); page.wait_for_selector(".model", state="attached"); page.wait_for_timeout(1500)
        log("BIG after reload convos:", page.evaluate("() => document.querySelector('#convos').innerText").replace("\n", " | "))
        log("BIG after reload threads:", page.evaluate("() => document.querySelector('#threads').innerText")[:300].replace("\n", " | "))
        ctx.close()

        # ── dark desk + phone dark: the verdict ──
        for tag, vp, scheme in (("dark", {"width": 1280, "height": 800}, "dark"), ("phone", {"width": 390, "height": 844}, "dark")):
            ctx = b.new_context(viewport=vp, color_scheme=scheme); page = ctx.new_page(); wire(page, tag)
            open_app(page, url)
            page.fill("#question", "What is 17 × 23?"); page.click("#ask"); page.wait_for_timeout(700)
            page.screenshot(path=str(OUT / f"{tag}-ask-running.png"))
            page.wait_for_selector(f"{LAST} .verdict", timeout=60000)
            page.wait_for_function(f"() => !document.querySelector('{LAST}').classList.contains('running')", timeout=60000)
            page.wait_for_timeout(600)
            page.screenshot(path=str(OUT / f"{tag}-ask-verdict.png"))
            tall(page, OUT / f"{tag}-ask-verdict-full.png", vp["width"])
            if page.query_selector(f'{LAST} .tab[data-tab="answers"]'):
                page.click(f'{LAST} .tab[data-tab="answers"]'); page.wait_for_timeout(300)
                tall(page, OUT / f"{tag}-ask-tab-answers.png", vp["width"])
            if tag == "phone":
                page.click('.rail-item[data-view="settings"]'); page.wait_for_timeout(2500)
                h = page.evaluate("() => document.querySelector('#view-settings .scroller').scrollHeight")
                page.set_viewport_size({"width": 390, "height": min(h + 160, 6000)}); page.wait_for_timeout(300)
                page.screenshot(path=str(OUT / "phone-settings-full.png"))
            ctx.close()
        b.close()

main()
