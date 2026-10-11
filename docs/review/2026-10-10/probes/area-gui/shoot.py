"""Screenshots and interaction probes of the Ixel browser app, as a brand-new user."""
import json
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

S = Path(__file__).parent
OUT = S / "shots"
OUT.mkdir(exist_ok=True)
url_line = (S / "home" / "app.out").read_text().strip().splitlines()
URL = next(l for l in url_line if l.startswith("IXEL-URL ")).split(" ", 1)[1]
print("URL", URL)

LOG = []


def log(*a):
    line = " ".join(str(x) for x in a)
    print(line)
    LOG.append(line)


def launch(p):
    try:
        return p.chromium.launch(executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    except Exception as e:  # noqa: BLE001
        log("default launch failed:", e)
        return p.chromium.launch(executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome")


def wire(page, tag):
    page.on("console", lambda m: LOG.append(f"[{tag}] console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: LOG.append(f"[{tag}] PAGEERROR: {e}"))
    page.on("requestfailed", lambda r: LOG.append(f"[{tag}] requestfailed: {r.url} {r.failure}"))


VIEWS = ["ask", "board", "machines", "health", "settings"]


def inner_height(page, view):
    return page.evaluate(
        """(view) => {
          const v = document.querySelector('#view-' + view);
          const s = v.querySelector('.scroller') || v.querySelector('.board-main');
          const main = s ? s.scrollHeight : 0;
          return Math.max(main, 0);
        }""", view)


def shoot_views(page, tag, width, base_h):
    for view in VIEWS:
        page.click(f'.rail-item[data-view="{view}"]')
        if view == "health":
            try:
                page.wait_for_function("() => !document.querySelector('#health-summary').textContent.includes('Looking')", timeout=20000)
            except Exception:  # noqa: BLE001
                log(f"[{tag}] health still looking after 20s")
        elif view == "settings":
            try:
                page.wait_for_function("() => !document.querySelector('#settings-body').textContent.includes('Reading your settings')", timeout=10000)
            except Exception:  # noqa: BLE001
                log(f"[{tag}] settings still reading after 10s")
        page.wait_for_timeout(1200)
        # Viewport-sized shot (what the person actually sees)
        page.screenshot(path=str(OUT / f"{tag}-{view}-viewport.png"))
        # A tall shot so inner scrollers show in full
        h = inner_height(page, view)
        total = max(base_h, h + 140)
        if total > base_h:
            page.set_viewport_size({"width": width, "height": min(total, 6000)})
            page.wait_for_timeout(300)
            page.screenshot(path=str(OUT / f"{tag}-{view}-full.png"))
            page.set_viewport_size({"width": width, "height": base_h})
            page.wait_for_timeout(200)
        # Text dump of the view for reading
        txt = page.evaluate("(v) => document.querySelector('#view-' + v).innerText", view)
        (OUT / f"{tag}-{view}.txt").write_text(txt)


def main():
    with sync_playwright() as p:
        browser = launch(p)
        for width, height, phone in ((1280, 800, False), (390, 844, True)):
            for scheme in ("light", "dark"):
                tag = f"{'phone' if phone else 'desk'}-{scheme}"
                ctx = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme,
                                          device_scale_factor=1)
                page = ctx.new_page()
                wire(page, tag)
                page.goto(URL)
                page.wait_for_function("() => document.querySelector('#version').textContent.length > 0", timeout=15000)
                page.wait_for_timeout(800)
                shoot_views(page, tag, width, height)
                if phone:
                    page.click('.rail-item[data-view="ask"]')
                    page.wait_for_timeout(300)
                    page.click("#open-sidebar")
                    page.wait_for_timeout(500)
                    page.screenshot(path=str(OUT / f"{tag}-ask-drawer.png"))
                    page.keyboard.press("Escape")
                ctx.close()

        # ── Interactions, desktop light ────────────────────────────────────
        ctx = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme="light")
        page = ctx.new_page()
        wire(page, "inter")
        page.goto(URL)
        page.wait_for_function("() => document.querySelector('#version').textContent.length > 0", timeout=15000)
        page.wait_for_timeout(800)

        # 1. Ask with no panel configured
        page.fill("#question", "What is 2 + 2? Please answer briefly.")
        page.click("#ask")
        page.wait_for_timeout(2500)
        page.screenshot(path=str(OUT / "inter-ask-nopanel.png"))
        log("ASK no panel, notice:", page.evaluate("() => document.querySelector('#notice').innerText"))
        log("ASK no panel, thread:", page.evaluate("() => document.querySelector('#threads').innerText")[:800])
        log("ASK no panel, question box now:", repr(page.evaluate("() => document.querySelector('#question').value")))

        # 2. Keyboard: tab order from the question box
        page.click("#question")
        order = []
        for _ in range(14):
            page.keyboard.press("Tab")
            order.append(page.evaluate("() => { const a = document.activeElement; return a.tagName + '#' + (a.id||'') + '.' + (a.className||'').toString().split(' ')[0] + ' ' + (a.getAttribute('aria-label')||a.textContent||'').trim().slice(0,30); }"))
        log("TAB ORDER:", json.dumps(order, indent=0))
        page.focus("#ask")
        page.screenshot(path=str(OUT / "inter-focus-ask.png"), clip={"x": 240, "y": 560, "width": 1040, "height": 240})

        # 3. Settings: save a bogus Anthropic key
        page.click('.rail-item[data-view="settings"]')
        page.wait_for_function("() => !document.querySelector('#settings-body').textContent.includes('Reading your settings')", timeout=10000)
        page.wait_for_timeout(800)
        keys = page.evaluate("() => [...document.querySelectorAll('input[type=password][data-key]')].map(i => i.dataset.key)")
        log("KEY INPUTS:", keys)
        sel = 'input[data-key="key:ANTHROPIC_API_KEY:value"]'
        if page.query_selector(sel):
            page.fill(sel, "sk-ant-api03-this-is-a-bogus-key-0123456789")
            page.click('[data-key="key:ANTHROPIC_API_KEY:save"]')
            page.wait_for_timeout(2500)
            row = page.query_selector('.set-key[data-row="ANTHROPIC_API_KEY"]')
            row.screenshot(path=str(OUT / "inter-settings-bogus-key.png"))
            log("BOGUS KEY row text:", row.inner_text())
            # the whole settings page after
            page.screenshot(path=str(OUT / "inter-settings-after-key.png"))
            log("SETTINGS problem line:", page.evaluate("() => (document.querySelector('#settings-body .notice') || {}).innerText || ''"))
            log("MODELS card:", page.evaluate("() => document.querySelector('[aria-labelledby=set-h-models]').innerText"))
        # key with spaces → validation message
        if page.query_selector('input[data-key="key:OPENAI_API_KEY:value"]'):
            page.fill('input[data-key="key:OPENAI_API_KEY:value"]', "not a key at all")
            page.click('[data-key="key:OPENAI_API_KEY:save"]')
            page.wait_for_timeout(1500)
            log("BAD KEY (spaces) note:", page.evaluate("() => document.querySelector('.set-key[data-row=OPENAI_API_KEY]').innerText"))
        # Settings: try the "Look" on this computer
        if page.query_selector('[data-key="servers:here"]'):
            page.click('[data-key="servers:here"]')
            page.wait_for_timeout(4000)
            log("LOOK here result:", page.evaluate("() => document.querySelector('[aria-labelledby=set-h-servers]').innerText")[-600:])
            page.screenshot(path=str(OUT / "inter-settings-look.png"))
        # Settings: try to change a disabled select? list the disabled controls
        log("DISABLED CONTROLS:", page.evaluate("() => [...document.querySelectorAll('#settings-body select:disabled, #settings-body input:disabled, #settings-body button:disabled')].map(n => n.dataset.key || n.textContent.trim()).slice(0, 40)"))

        # 4. Back to Ask: did the sidebar panel change after the key save?
        page.click('.rail-item[data-view="ask"]')
        page.wait_for_timeout(1200)
        log("PANEL after key:", page.evaluate("() => document.querySelector('#panel').innerText"))
        log("NOTICE after key:", page.evaluate("() => document.querySelector('#notice').innerText"))
        page.screenshot(path=str(OUT / "inter-ask-after-key.png"))

        # 5. Machines: open Add a machine, submit empty, then with a host
        page.click('.rail-item[data-view="machines"]')
        page.wait_for_timeout(1000)
        page.click("#machines-add")
        page.wait_for_timeout(500)
        page.screenshot(path=str(OUT / "inter-machines-add.png"))
        page.click('#machines-dialog button[type=submit]')
        page.wait_for_timeout(1200)
        log("MACHINE empty submit problem:", page.evaluate("() => (document.querySelector('#machines-dialog .dialog-problem')||{}).innerText || '' "), "| open:", page.evaluate("() => document.querySelector('#machines-dialog').open"))
        page.screenshot(path=str(OUT / "inter-machines-add-empty.png"))
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

        # 6. Board for a new user
        page.click('.rail-item[data-view="board"]')
        page.wait_for_timeout(2500)
        log("BOARD text:", page.evaluate("() => document.querySelector('#view-board').innerText")[:600])

        # 7. Hostile markdown through the page's own renderer
        page.click('.rail-item[data-view="ask"]')
        hostile = [
            "<img src=x onerror=alert(1)>",
            "[click](javascript:alert(1))",
            "**<script>alert(1)</script>**",
            "```html\n<script>alert(2)</script>\n```",
            "| a | b |\n|---|---|\n| <b>x</b> | `</td><script>` |",
            "> " * 50 + "deep",
            "‮RTL override text‬ and \x1b[31mANSI\x1b[0m",
        ]
        res = page.evaluate("""async (items) => {
          const m = await import('/markdown.js');
          const out = [];
          for (const s of items) {
            const frag = m.renderMarkdown(s);
            const div = document.createElement('div');
            div.append(frag);
            out.push({ src: s.slice(0, 40), html: div.innerHTML.slice(0, 200), scripts: div.querySelectorAll('script,img,a,iframe').length });
          }
          return out;
        }""", hostile)
        for r in res:
            log("MD:", json.dumps(r))
        # Pathological inputs: timing
        timing = page.evaluate("""async () => {
          const m = await import('/markdown.js');
          const out = {};
          const cases = {
            stars: '*'.repeat(100000),
            nested_lists: Array.from({length: 3000}, (_, i) => ' '.repeat(i) + '- x').join('\\n'),
            many_lines: 'line\\n'.repeat(60000),
            backticks: '`'.repeat(100000),
            quotes: '>'.repeat(100000) + 'x',
            table: ('|' + 'a|'.repeat(2000) + '\\n').repeat(300),
            long_para_inline: ('**a** *b* `c` ~~d~~ ').repeat(20000),
          };
          for (const [k, v] of Object.entries(cases)) {
            const t = performance.now();
            try { m.renderMarkdown(v); out[k] = Math.round(performance.now() - t) + 'ms'; }
            catch (e) { out[k] = 'THREW ' + e.message.slice(0, 80); }
          }
          return out;
        }""")
        log("MD TIMING:", json.dumps(timing))

        # 8. Reload: does state survive? ask a question (fails: no panel) → reload → is the failed turn kept?
        page.reload()
        page.wait_for_function("() => document.querySelector('#version').textContent.length > 0", timeout=15000)
        page.wait_for_timeout(1000)
        log("AFTER RELOAD convos:", page.evaluate("() => document.querySelector('#convos').innerText"))
        log("AFTER RELOAD hash:", page.evaluate("() => location.hash"))
        ctx.close()

        # ── Phone interactions ─────────────────────────────────────────────
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark")
        page = ctx.new_page()
        wire(page, "phone-inter")
        page.goto(URL)
        page.wait_for_function("() => document.querySelector('#version').textContent.length > 0", timeout=15000)
        page.wait_for_timeout(800)
        page.fill("#question", "What is 2 + 2?")
        page.click("#ask")
        page.wait_for_timeout(2000)
        page.screenshot(path=str(OUT / "phone-inter-ask-nopanel.png"))
        # overflow check on each view
        for view in VIEWS:
            page.click(f'.rail-item[data-view="{view}"]')
            page.wait_for_timeout(1200)
            over = page.evaluate("""(view) => {
              const v = document.querySelector('#view-' + view);
              const wide = [];
              for (const n of v.querySelectorAll('*')) {
                const r = n.getBoundingClientRect();
                if (r.width > 0 && r.right > window.innerWidth + 1 && getComputedStyle(n).position !== 'fixed') {
                  wide.push(n.tagName + '.' + (n.className||'').toString().split(' ')[0] + ' right=' + Math.round(r.right));
                }
              }
              return { docScrollW: document.documentElement.scrollWidth, wide: wide.slice(0, 8) };
            }""", view)
            log(f"PHONE OVERFLOW {view}:", json.dumps(over))
        ctx.close()
        browser.close()
    (OUT / "log.txt").write_text("\n".join(LOG))


main()
