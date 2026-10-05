"""`ixel gui` in a real browser: full flow, hostile model output, and CSP compliance.

Skipped when Playwright (and its Chromium) isn't installed.
"""
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from fake_providers import ThreadedFakeProvider, typesafe_handler, panel_handler  # noqa: E402

XSS = ('391, as it happens. <img src=x onerror="window.__pwned=1"> <script>window.__pwned=2</script> '
       '[click me](javascript:window.__pwned=3) **bold** and `code`\n\n```js\nwindow.__pwned = 4\n```')
ANSWERS = {"m-gpt": "It's **391**.\n\n- 17 × 20 = 340\n- 17 × 3 = 51\n- 340 + 51 = 391",
           "m-xss": XSS, "m-wrong": "The answer is 381."}
URL_RE = re.compile(r"http://127\.0\.0\.1:\d+/#token=[A-Za-z0-9_-]+")
LAST = ".thread:not([hidden]) .turn:last-child"  # the newest question in the conversation on screen


def open_app(page, url):
    page.goto(url)
    page.wait_for_selector(".model", state="attached")  # (in a closed drawer on a phone)


def injected(page):
    """Anything a model's text could have added to the page."""
    return {"pwned": page.evaluate("window.__pwned"),
            "images": page.locator('img:not([src="/mark.svg"])').count(),  # the logo is the only picture
            "scripts": page.locator("script").count() - 2,                  # theme.js and app.js are the only scripts
            "links": page.locator("a").count()}                             # nothing a model writes is clickable


CLEAN = {"pwned": None, "images": 0, "scripts": 0, "links": 0}


@pytest.fixture(scope="module")
def gui_url(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    answer = panel_handler(ANSWERS)

    def handler(recorded):
        if "take your time" in recorded.body["messages"][0]["content"]:
            time.sleep(1.5)  # a slow model, so the page can be used while it works
        return answer(recorded)

    with ThreadedFakeProvider(handler) as fake:
        fake.stream_delay = 0.25  # the verdict arrives in visible pieces
        # Triage: picks quick for auto; says answers agree only for questions marked [agree]
        fake.typesafe_handler = typesafe_handler(agree=lambda r: 0.97 if "[agree]" in r.body["state"]["question"] else 0.2,
                                       depth=0.2)
        cfg = home / ".config" / "ixel-mat"
        cfg.mkdir(parents=True)
        cfg.joinpath("config.toml").write_text("".join(
            f'[agents.{aid}]\ntype = "http"\nurl = "{fake.openai_url}"\ntoken_env = "IXEL_TEST_PANEL_KEY"\n'
            f'model = "{model}"\nlabel = "{label}"\n\n'
            for aid, model, label in [("gpt", "m-gpt", "GPT-5"), ("claude", "m-xss", "Claude"),
                                      ("gemini", "m-wrong", "Gemini")]) + '[saver]\nverifier = "claude"\n'
            + f'[triage]\nenabled = true\nskip_review = true\nurl = "{fake.typesafe_url}"\n')
        # A computer without Handoff, as the Health and Board tests expect: not one installed where they run
        no_handoff = {"HANDOFF_INSTALL_ROOT": "", "LOCALAPPDATA": str(home / "AppData" / "Local"),
                      "PATH": os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                                              if p and not shutil.which("handoff", path=p))}
        with launch_gui(home, {"IXEL_TEST_PANEL_KEY": "sk-test", "TYPESAFE_API_KEY": "ts-test",
                               "OPENAI_API_KEY": "", "GROQ_API_KEY": "", **no_handoff}) as url:  # no sound service
            yield url


@contextlib.contextmanager
def launch_gui(home, env):
    """`ixel gui` with `home` as its home folder; yields the address it prints."""
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "PYTHONIOENCODING": "utf-8",
           "PYTHONUNBUFFERED": "1", **env}
    proc = subprocess.Popen([sys.executable, "-m", "ixel_mat", "gui", "--no-browser"], env=env, cwd=home,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    found = {}

    def read():
        for line in proc.stdout:
            match = URL_RE.search(line)
            if match and "url" not in found:
                found["url"] = match.group(0)

    threading.Thread(target=read, daemon=True).start()
    deadline = time.monotonic() + 30
    while "url" not in found and time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.1)
    try:
        assert "url" in found, "ixel gui did not print its URL"
        yield found["url"]
    finally:
        proc.terminate()
        proc.wait(10)


@pytest.fixture
def browser():
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium not available: {exc}")
        yield b
        b.close()


def test_full_review_in_the_browser(gui_url, browser):
    page = browser.new_page(viewport={"width": 1100, "height": 900})
    violations, errors = [], []
    page.on("console", lambda m: violations.append(m.text) if "Content Security Policy" in m.text else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    open_app(page, gui_url)
    assert page.evaluate("location.hash") == ""  # key removed from the address bar
    assert page.locator(".model:not(.triage) .name").all_inner_texts() == ["GPT-5", "Claude", "Gemini"]
    assert "about 7 model calls" in page.inner_text("#mode-hint")
    assert "grade each other anonymously" in page.inner_text("#empty")

    page.fill("#question", "What is 17 × 23?")
    page.click("#ask")
    page.wait_for_selector(f"{LAST} th.step.active", timeout=30_000)  # the live grid, one row per model
    assert page.locator(f"{LAST} .grid tbody tr").count() == 3
    page.wait_for_selector(".verdict", timeout=30_000)

    assert "17 × 23 = 391" in page.inner_text(".verdict")
    assert "One answer said 381." in page.inner_text(".verdict")
    assert page.locator("table.scores tbody tr").count() == 3
    assert "Gemini conceded" in page.inner_text(".concessions")
    assert "Agreement: strong" in page.inner_text(".agreement")
    assert page.locator(f"{LAST} th.step.done").count() == 3
    assert page.inner_text("#convo-title") == "What is 17 × 23?"
    assert page.locator(".convos li").all_inner_texts() == ["What is 17 × 23?"]

    # Hostile answer: shown as text, nothing executed, no elements injected
    page.click(f"{LAST} .tab[data-tab=answers]")
    answers = page.inner_text(".answers")
    assert '<img src=x onerror="window.__pwned=1">' in answers
    assert "<script>window.__pwned=2</script>" in answers
    assert injected(page) == CLEAN
    assert page.locator(".answers pre code").first.inner_text() == "window.__pwned = 4"
    assert page.locator(".answers strong").count() >= 1

    # This tab keeps the conversation across a reload, and it's still only text. Ixel keeps it, in memory: the
    # browser's storage, which Edge and Chrome may write into a profile folder, has only the key and the tab's id
    page.reload()
    page.wait_for_selector(".verdict")
    assert "17 × 23 = 391" in page.inner_text(".verdict")
    stored = page.evaluate("JSON.stringify([Object.entries(sessionStorage), Object.entries(localStorage)])")
    assert "17 × 23" not in stored and "ixel-conversations" not in stored
    assert sorted(page.evaluate("Object.keys(sessionStorage)")) == ["ixel-session", "ixel-tab"]
    assert page.locator(f"{LAST} th.step.done").count() == 3
    page.click(f"{LAST} .tab[data-tab=answers]")
    assert "<script>window.__pwned=2</script>" in page.inner_text(".answers")
    assert injected(page) == CLEAN

    assert not violations, violations
    assert not errors, errors

    shots = os.environ.get("IXEL_SCREENSHOT_DIR")
    if shots:
        page.screenshot(path=os.path.join(shots, "ixel-gui-review.png"), full_page=True)


def test_a_duplicated_tab_keeps_its_own_conversations(gui_url, browser):
    """Duplicate tab copies the tab's sessionStorage, and with it the id Ixel keeps its conversations under: each
    page takes a new id when it loads, so the two start the same and then don't overwrite each other's."""
    errors = []

    def open_page(url, copied=None):
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        if copied:  # what Duplicate does, once: a reload of the copy keeps its own storage
            page.add_init_script(f'if (window.name !== "duplicate") {{ window.name = "duplicate"; '
                                 f'for (const [k, v] of {copied}) sessionStorage.setItem(k, v); }}')
        open_app(page, url)
        return page

    def ask(page, question):
        asked = page.locator(".thread:not([hidden]) .turn").count()
        page.fill("#question", question)
        page.click("#ask")
        page.locator(".thread:not([hidden]) .turn").nth(asked).locator(".verdict").wait_for(timeout=30_000)
        page.wait_for_timeout(300)  # the save after it

    def after_a_reload(page):
        page.reload()
        page.locator(".thread:not([hidden]) .turn .verdict").first.wait_for(timeout=10_000)
        page.wait_for_timeout(300)
        return page.inner_text(".thread:not([hidden])")

    first = open_page(gui_url)
    first.click('.modes button[data-mode="quick"]')
    ask(first, "What is 17 × 23? Asked in the first tab")
    copied = json.dumps(first.evaluate("Object.entries(sessionStorage)"))
    second = open_page(gui_url.split("#")[0], copied)
    second.locator(".thread:not([hidden]) .turn .verdict").first.wait_for(timeout=10_000)  # a copy of the first's
    second.click('.modes button[data-mode="quick"]')
    ask(first, "Asked in the first tab again")
    ask(second, "Asked in the duplicate")

    shown = after_a_reload(first)
    assert "Asked in the first tab again" in shown and "Asked in the duplicate" not in shown
    shown = after_a_reload(second)
    assert "What is 17 × 23? Asked in the first tab" in shown and "Asked in the duplicate" in shown
    assert "Asked in the first tab again" not in shown
    assert "Asked in the duplicate" in after_a_reload(second)  # and again, under the id it took last time
    assert not errors, errors


def test_follow_up_questions_in_the_browser(gui_url, browser):
    page = browser.new_page(viewport={"width": 1100, "height": 900})
    errors, sent = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("request", lambda r: sent.append(json.loads(r.post_data)) if r.url.endswith("/api/review") else None)
    open_app(page, gui_url)
    assert page.is_hidden("#followup")

    page.click('.modes button[data-mode="quick"]')
    page.fill("#question", "What is 17 × 23?")
    page.click("#ask")
    page.wait_for_selector(".verdict", timeout=30_000)
    # Answered: the box is cleared for a follow-up, and the page says what the panel will see
    assert page.input_value("#question") == ""
    assert "last question and its answer" in page.inner_text("#followup")
    assert sent[-1]["earlier"] == []

    page.fill("#question", "And doubled?")
    page.click("#ask")
    page.wait_for_selector(f"{LAST} .verdict .followup-badge", timeout=30_000)  # (the page's CSP forbids eval)
    assert sent[-1]["earlier"] == [{"question": "What is 17 × 23?", "answer": "17 × 23 = 391"}]
    assert "last 2 questions" in page.inner_text("#followup")
    assert page.locator(".thread:not([hidden]) .turn").count() == 2  # one conversation, both questions
    shots = os.environ.get("IXEL_SCREENSHOT_DIR")
    if shots:
        page.screenshot(path=os.path.join(shots, "ixel-gui-follow-up.png"))

    # New conversation while a follow-up is still running: its answer mustn't join the new one
    page.fill("#question", "take your time: and tripled?")
    page.click("#ask")
    page.wait_for_selector("#stop:not([hidden])")
    page.click("#new-question")
    assert page.is_visible("#empty") and page.is_hidden("#followup")
    page.wait_for_selector("#ask:not([hidden])", timeout=30_000)  # it finished, out of sight
    assert page.is_hidden("#followup")  # the answer that finished didn't bring the old topic back
    assert page.locator(".convos .n").all_inner_texts() == ["3"]  # it joined the conversation it was asked in

    page.fill("#question", "Something else")
    page.click("#ask")
    page.wait_for_selector(f"{LAST} .verdict", timeout=30_000)
    assert page.locator(f"{LAST} .verdict .followup-badge").count() == 0
    assert sent[-1]["earlier"] == []
    assert page.locator(".convos li").count() == 2

    # Back to the first conversation: a follow-up there carries its questions, not the other one's
    page.click(".convos li:last-child button")
    assert page.inner_text("#convo-title") == "What is 17 × 23?"
    assert "last 3 questions" in page.inner_text("#followup")
    page.fill("#question", "And halved?")
    page.click("#ask")
    page.wait_for_selector(f"{LAST} .verdict .followup-badge", timeout=30_000)
    assert [t["question"] for t in sent[-1]["earlier"]] == [
        "What is 17 × 23?", "And doubled?", "take your time: and tripled?"]
    assert not errors, errors


def test_triage_picks_the_mode_and_skips_reviews_it_isnt_needed_for(gui_url, browser):
    page = browser.new_page(viewport={"width": 1100, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    open_app(page, gui_url)
    assert page.locator("#panel .model.triage").inner_text().startswith("Triage")
    page.click('.modes button[data-mode="auto"]')
    assert "picks how much checking" in page.inner_text("#empty")

    page.fill("#question", "What is 17 × 23?")
    page.click("#ask")
    page.wait_for_selector(f"{LAST} .verdict", timeout=30_000)
    assert page.inner_text(f"{LAST} .mode-tag").lower() == "auto · quick"
    assert "Triage picked quick mode" in page.inner_text(f"{LAST} .triage-notes")
    assert "1 triage call" in page.inner_text(f"{LAST} .verdict")

    # The answers agree: peer review is skipped, and the grid says so
    page.click('.modes button[data-mode="review"]')
    page.fill("#question", "[agree] And 17 × 24?")
    page.click("#ask")
    page.wait_for_selector(f"{LAST} .verdict", timeout=30_000)
    assert page.locator(f"{LAST} th.step.skipped").all_inner_texts() == ["Peer review"]
    assert "peer review was skipped" in page.inner_text(f"{LAST} .triage-notes")
    assert page.locator(f"{LAST} table.scores").count() == 0
    assert injected(page) == CLEAN
    shots = os.environ.get("IXEL_SCREENSHOT_DIR")
    if shots:
        page.screenshot(path=os.path.join(shots, "ixel-gui-triage.png"))
    # …and it's all still there after a reload
    page.reload()
    page.wait_for_selector(f"{LAST} .verdict")
    assert "peer review was skipped" in page.inner_text(f"{LAST} .triage-notes")
    assert not errors, errors


def test_stop_cancels_and_keeps_the_question(gui_url, browser):
    page = browser.new_page(viewport={"width": 1100, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    open_app(page, gui_url)
    page.click('.modes button[data-mode="quick"]')
    page.fill("#question", "take your time: what is 17 × 23?")
    page.click("#ask")
    assert page.input_value("#question") == ""  # sent: the box is ready for the next one
    page.wait_for_selector(f"{LAST} .cell.working")
    page.click("#stop")
    page.wait_for_selector("#ask:not([hidden])")
    assert "Stopped" in page.inner_text(f"{LAST} .status")
    assert "calls that were still running were cancelled" in page.inner_text(LAST)
    assert page.input_value("#question") == "take your time: what is 17 × 23?"  # back, to ask again
    assert page.is_hidden("#followup")  # nothing was answered, so there's nothing to follow up
    assert not errors, errors


def test_the_verdict_shows_while_it_is_written(gui_url, browser):
    page = browser.new_page(viewport={"width": 1100, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    open_app(page, gui_url)
    page.click('.modes button[data-mode="quick"]')
    page.fill("#question", "What is 17 × 23?")
    page.click("#ask")
    page.wait_for_selector("#draft .draft-text p", timeout=30_000)
    partial = page.inner_text("#draft .draft-text")
    assert partial and "17 × 23 = 391".startswith(partial.strip()[:5])
    assert "being written" in page.inner_text("#draft")
    shots = os.environ.get("IXEL_SCREENSHOT_DIR")
    if shots:
        page.screenshot(path=os.path.join(shots, "ixel-gui-verdict-streaming.png"))
    page.wait_for_selector(".verdict", timeout=30_000)
    assert page.locator("#draft").count() == 0  # replaced by the finished verdict
    assert "17 × 23 = 391" in page.inner_text(".verdict")
    assert not errors, errors


def test_handoff_alone_shows_how_to_use_it_or_how_to_add_it(gui_url, browser):
    page = browser.new_page(viewport={"width": 1100, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    line = "curl -fsSL https://ixelai.com/handoff/install.sh | sh"
    info = {"installed": True, "project": ""}  # what the server says, set below
    page.route("**/api/handoff", lambda route: route.fulfill(content_type="application/json", body=json.dumps(info)))
    open_app(page, gui_url)

    def handoff():
        page.fill("#question", "/handoff")
        page.click("#ask")
        card = page.locator(".handoff-turn").last
        card.locator(".handoff-body p").first.wait_for()
        return card

    card = handoff()
    assert "Start each part with who does it" in card.inner_text() and line not in card.inner_text()

    # Ixel MAT installed on its own: just /handoff says how to add Handoff
    info = {"installed": False, "project": "", "install": {"where": "a terminal", "command": line}}
    card = handoff()
    assert "Handoff isn't installed on this computer." in card.inner_text()
    assert card.locator(".handoff-example").inner_text() == line
    assert "Start each part" not in card.inner_text() and card.locator(".handoff-btn").is_hidden()
    assert not errors, errors


def test_page_without_the_key_explains_itself(gui_url, browser):
    page = browser.new_page()
    page.goto(gui_url.split("#")[0])
    page.wait_for_selector(".notice.error")
    assert "one-time key" in page.inner_text(".notice")
    assert page.is_disabled("#ask")


def test_light_mode_and_phone_width_render(gui_url, browser):
    context = browser.new_context(color_scheme="light", viewport={"width": 390, "height": 844})
    page = context.new_page()
    open_app(page, gui_url)
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= 390  # no sideways scrolling on a phone
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg == "rgb(255, 255, 255)"
    # The sidebar is a drawer here
    assert page.is_hidden("#sidebar .models")
    page.click("#open-sidebar")
    page.wait_for_selector("#sidebar .model")
    page.click("#close-sidebar")
    page.wait_for_selector("#sidebar .model", state="hidden")
    page.fill("#question", "What is 17 × 23?")
    page.click("#ask")
    page.wait_for_selector(".verdict", timeout=30_000)
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    shots = os.environ.get("IXEL_SCREENSHOT_DIR")
    if shots:
        page.screenshot(path=os.path.join(shots, "ixel-gui-phone-light.png"), full_page=True)
    context.close()


def test_saver_mode_and_saves_counter_in_the_browser(gui_url, browser):
    page = browser.new_page(viewport={"width": 1100, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    open_app(page, gui_url)
    page.click(".modes button[data-mode=saver]")
    assert "Claude only verifies" in page.inner_text("#mode-hint")

    page.fill("#question", "What is 17 × 23?")
    page.click("#ask")
    page.wait_for_selector(".verdict", timeout=30_000)
    verdict = page.inner_text(".verdict")
    assert "verified by Claude" in verdict and "4 cheap calls · 1 big-model" in verdict
    assert page.locator(f"{LAST} th.step .step-title").all_inner_texts() == [
        "Answer", "Peer review", "Big-model check"]
    assert page.locator(f"{LAST} th.step.done").count() == 3

    page.wait_for_selector('#saves:has-text("1 save")')
    assert page.inner_text("#saves") == "1 save"
    page.wait_for_selector(".toast")
    toasts = " ".join(page.locator(".toast").all_inner_texts())
    assert "Saved one" in toasts and "First save" in toasts
    assert not errors, errors

    shots = os.environ.get("IXEL_SCREENSHOT_DIR")
    if shots:
        # Let the toasts finish sliding in; they're pinned to the window's corner, so
        # capture the window as a user sees it rather than the whole page.
        page.evaluate("Promise.all(document.getAnimations().map(a => a.finished))")
        page.screenshot(path=os.path.join(shots, "ixel-gui-saver.png"))


def test_private_launch_page_opens_the_app(gui_url, browser):
    from pathlib import Path

    from ixel_mat.gui.server import write_launch_page
    launch = write_launch_page(gui_url)
    try:
        page = browser.new_page()
        page.goto(launch.as_uri())
        page.wait_for_selector(".model", timeout=15_000)
        assert page.url.startswith("http://127.0.0.1:") and page.evaluate("location.hash") == ""
    finally:
        Path(launch).unlink(missing_ok=True)


def test_handoff_runs_the_plan_it_showed(gui_url, browser):
    page = browser.new_page(viewport={"width": 1100, "height": 900})
    errors, runs = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    step = {"agent": "codex", "label": "Codex", "kind": "review", "text": "review my changes",
            "title": "Review my changes", "detail": "", "note": "", "problem": ""}

    def plan(route):
        asked = json.loads(route.request.post_data)["project"]
        route.fulfill(content_type="application/json",
                      body=json.dumps({"project": "/repos/" + asked.strip("/"), "steps": [step], "problems": []}))

    def run(route):
        runs.append(json.loads(route.request.post_data)["project"])
        route.fulfill(content_type="application/json", body=json.dumps(
            {"project": runs[-1], "results": [{"task": "T-1", "agent": "codex", "ok": True, "message": "reviewed"}]}))

    page.route("**/api/handoff", lambda route: route.fulfill(
        content_type="application/json", body=json.dumps({"installed": True, "project": "shop"})))
    page.route("**/api/handoff/plan", plan)
    page.route("**/api/handoff/run", run)
    open_app(page, gui_url)
    page.fill("#question", "/handoff codex review my changes")
    page.click("#ask")
    card = page.locator(".handoff-turn").last
    card.locator(".handoff-run").wait_for()

    # Changing the folder after the plan takes Run away until there's a plan for the new folder
    card.locator(".handoff-project").fill("/somewhere/else")
    assert card.locator(".handoff-run").count() == 0
    assert "The folder changed" in card.inner_text()
    card.locator(".handoff-btn").click()
    card.locator(".handoff-run").click()
    card.locator(".handoff-results").wait_for()
    assert runs == ["/repos/somewhere/else"]  # the folder the plan was made for, as Handoff named it
    assert not errors, errors


def test_handoff_keeps_the_plan_you_asked_for_while_it_starts(gui_url, browser):
    """Handoff answers slowly, and you type a folder and press Plan first: that plan stays, and no second
    plan for the remembered folder replaces it."""
    page = browser.new_page(viewport={"width": 1100, "height": 900})
    errors, held, plans = [], [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    step = {"agent": "codex", "label": "Codex", "kind": "review", "text": "review my changes",
            "title": "Review my changes", "detail": "", "note": "", "problem": ""}

    def plan(route):
        plans.append(json.loads(route.request.post_data)["project"])
        route.fulfill(content_type="application/json",
                      body=json.dumps({"project": "/repos/" + plans[-1].strip("/"), "steps": [step], "problems": []}))

    page.route("**/api/handoff", lambda route: held.append(route))  # answered once the plan is on screen
    page.route("**/api/handoff/plan", plan)
    open_app(page, gui_url)
    page.fill("#question", "/handoff codex review my changes")
    page.click("#ask")
    card = page.locator(".handoff-turn").last
    for _ in range(100):
        if held:
            break
        page.wait_for_timeout(50)
    card.locator(".handoff-project").fill("/typed")
    card.locator(".handoff-btn").click()
    card.locator(".handoff-run").wait_for()
    held[0].fulfill(content_type="application/json", body=json.dumps({"installed": True, "project": "/shop"}))
    page.wait_for_timeout(300)
    assert plans == ["/typed"]
    assert card.locator(".handoff-project").input_value() == "/repos/typed"
    assert card.locator(".handoff-run").count() == 1
    assert not errors, errors


# ── The rail and Health ───────────────────────────────────────────────────────

def test_rail_moves_between_views_and_a_question_keeps_going(gui_url, browser):
    page = browser.new_page(viewport={"width": 1280, "height": 860})
    errors, violations = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: violations.append(m.text) if "Content Security Policy" in m.text else None)
    open_app(page, gui_url)
    assert page.get_attribute(".rail-item[data-view=ask]", "aria-current") == "page"

    # Ask, then look at Health while the panel works: the answer is there on the way back
    page.fill("#question", "What is 17 × 23? take your time")
    page.click("#ask")
    page.click(".rail-item[data-view=health]")
    page.wait_for_selector("#view-health .check")
    assert page.is_hidden("#view-ask") and page.title() == "Health · Ixel"
    assert page.evaluate("document.activeElement.id") == "health-title"
    assert page.get_attribute(".rail-item[data-view=health]", "aria-current") == "page"
    labels = page.locator("#view-health .check-label").all_inner_texts()
    assert {"GPT-5", "Claude", "Gemini", "Handoff", "Git"} <= set(labels)
    assert page.locator("#view-health .check.unchecked").count() >= 3  # the models wait for Check now
    page.keyboard.press("/")  # Ask's shortcut does nothing while Ask isn't on screen
    assert page.evaluate("document.activeElement.id") != "question"

    page.click(".rail-item[data-view=ask]")
    wait_until(page, "document.activeElement.id === 'question'")
    page.wait_for_selector(f"{LAST} .verdict", timeout=30_000)

    # Back and forward work like pages; a reload stays on the view
    page.go_back()
    page.wait_for_selector("#view-health:not([hidden])")
    page.reload()
    page.wait_for_selector("#view-health .check")
    assert page.is_hidden("#view-ask")
    assert not errors and not violations, (errors, violations)


def test_check_now_tests_each_model(gui_url, browser):
    page = browser.new_page(viewport={"width": 1280, "height": 860})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(gui_url)
    page.click(".rail-item[data-view=health]")
    page.wait_for_selector("#view-health .check")
    assert "Check now" in page.inner_text("#health-summary")
    page.click("#health-check")
    page.wait_for_selector("#health-when:has-text('Checked')", timeout=40_000)
    rows = {r.locator(".check-label").inner_text(): r.get_attribute("class")
            for r in page.locator("#view-health .check").all()}
    assert all("ok" in rows[name] for name in ("GPT-5", "Claude", "Gemini")), rows  # the fake server answers
    handoff = page.locator("#view-health .check", has=page.locator(".check-label", has_text="Handoff"))
    assert "off" in handoff.get_attribute("class")  # it isn't installed here
    assert "ixelai.com/handoff/install" in handoff.locator(".check-fix code").inner_text()
    assert not errors, errors
    shots = os.environ.get("IXEL_SCREENSHOT_DIR")
    if shots:
        page.screenshot(path=os.path.join(shots, "ixel-health.png"), full_page=True)


def test_health_text_stays_text_and_problems_show_on_the_rail(gui_url, browser):
    page = browser.new_page(viewport={"width": 1280, "height": 860})
    hostile = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>'
    report = {"schema": 1, "checked_at": "2026-10-03T05:00:00Z", "probed": False, "groups": [
        {"id": "models", "title": "Models", "checks": [
            {"id": "a", "label": hostile, "state": "fail", "detail": hostile, "fix": hostile},
            {"id": "b", "label": "B", "state": "warn", "detail": "", "fix": ""}]}]}
    page.route("**/api/health*", lambda route: route.fulfill(content_type="application/json", body=json.dumps(report)))
    page.goto(gui_url)
    page.wait_for_selector(".rail-dot.fail:not([hidden])")
    assert page.get_attribute(".rail-item[data-view=health] .rail-dot", "title") == "1 thing to fix"
    page.click(".rail-item[data-view=health]")
    page.wait_for_selector("#view-health .check.fail")
    assert page.inner_text("#view-health .check.fail .check-label") == hostile
    assert "1 thing to fix, 1 thing to look at." in page.inner_text("#health-summary")
    assert injected(page) == CLEAN


def test_rail_is_a_bottom_bar_on_a_phone(gui_url, browser):
    context = browser.new_context(color_scheme="light", viewport={"width": 390, "height": 844})
    page = context.new_page()
    open_app(page, gui_url)
    box = page.locator(".rail").bounding_box()
    assert box["y"] > 700 and box["width"] >= 380
    page.click(".rail-item[data-view=health]")
    page.wait_for_selector("#view-health .check")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    shots = os.environ.get("IXEL_SCREENSHOT_DIR")
    if shots:
        page.screenshot(path=os.path.join(shots, "ixel-health-phone-light.png"))
    context.close()


# ── The Board ─────────────────────────────────────────────────────────────────

def tiny_png():
    import struct
    import zlib

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xe8\xb6\x4c\xe8\xb6\x4c" * 2)) + chunk(b"IEND", b""))


HOSTILE = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>'


class FakeBoard:
    """The app server's /api/board routes, played by the test: a board that changes when the page acts."""

    def __init__(self, hello=None):
        self.hello = hello or {"ok": True, "version": "0.9.0", "update": "", "project": "/repos/shop",
                               "install": {"where": "PowerShell", "command": "irm https://ixelai.com/handoff/install.ps1 | iex"}}
        self.revision = 1
        self.actions = []
        self.refuse = {}   # op → the error Handoff gives for it
        self.slow = 0.0    # seconds each action takes, as a real `handoff api` start does
        self.tasks = {
            "T-1": self.task(1, "Pick the launch date " + HOSTILE, "open", "human"),
            "T-2": self.task(2, "Write release notes", "in_progress", "claude",
                             run={"state": "running", "agent": "claude", "kind": "answer"}),
            "T-3": self.task(3, "Make the store pictures", "open", "grok"),
            "T-4": self.task(4, "Fix the login page", "done", "codex"),
            "T-5": self.task(5, "Ship it", "blocked", None),
        }

    @staticmethod
    def task(n, title, status, assignee, run=None):
        return {"ref": f"T-{n}", "id": n, "title": title, "status": status, "assignee": assignee, "waiting_on": None,
                "branch": None, "parent": None, "created_by": "human", "created_at": "2026-10-03T04:00:00Z",
                "updated_at": f"2026-10-03T04:0{n}:00Z", "run": run, "overlaps": 0,
                "last_event": {"at": "2026-10-03T04:00:00Z", "actor": "human", "kind": "created",
                               "summary": f"created it{f', assigned to {assignee}' if assignee else ''}"}}

    def detail(self, ref):
        t = self.tasks[ref]
        if t["status"] in ("done", "cancelled"):
            actions = [{"op": "status", "label": "Reopen", "primary": True, "args": {"to": "open"}},
                       {"op": "delete", "label": "Delete", "primary": False, "confirm": True}]
        elif t["assignee"] not in (None, "human") and not t["run"]:
            actions = [{"op": "approve", "label": "Run it now", "primary": True, "needs": "kind", "confirm": True,
                        "args": {"agent": t["assignee"], "kind": "answer"}, "kinds": ["answer", "review", "image"]},
                       {"op": "note", "label": "Add a note", "primary": False, "needs": "text"},
                       {"op": "delete", "label": "Delete", "primary": False, "confirm": True}]
        else:
            actions = [{"op": "status", "label": "Mark done", "primary": True, "args": {"to": "done"}},
                       {"op": "assign", "label": "Assign", "primary": False, "needs": "agent"},
                       {"op": "note", "label": "Add a note", "primary": False, "needs": "text"},
                       {"op": "delete", "label": "Delete", "primary": False, "confirm": True}]
        events = [{"id": 1, "at": "2026-10-03T04:00:00Z", "actor": "human", "kind": "created",
                   "summary": t["last_event"]["summary"], "text": "", "detail": "", "panel": ""},
                  {"id": 2, "at": "2026-10-03T04:05:00Z", "actor": "codex", "kind": "note", "summary": "added a note",
                   "text": HOSTILE + "\nsecond line", "detail": "", "panel": ""}]
        outputs = [{"name": "answer.md", "size": 60}, {"name": "picture-1.png", "size": 80}] if ref == "T-1" else []
        return {"task": {**t, "body": "Details " + HOSTILE, "acceptance": ["Works on " + HOSTILE],
                         "content": f"seen-{ref}-{t['updated_at']}"}, "events": events,
                "claims": [], "overlaps": [], "children": [], "outputs": outputs, "actions": actions}

    def handle(self, route):
        from urllib.parse import parse_qs, urlparse
        url = urlparse(route.request.url)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        reply = lambda data, status=200: route.fulfill(status=status, content_type="application/json",  # noqa: E731
                                                       body=json.dumps(data))
        if url.path == "/api/board/hello":
            return reply(self.hello)
        if url.path == "/api/board":
            if query.get("since") == str(self.revision):
                return reply({"unchanged": True, "revision": query["since"]})
            return reply({"exists": True, "revision": str(self.revision), "project": "/repos/shop", "counts": {},
                          "tasks": list(self.tasks.values())})
        if url.path == "/api/board/task":
            return reply(self.detail(query["task"]))
        if url.path == "/api/board/agents":
            return reply({"agents": [{"name": "claude"}, {"name": "codex"}, {"name": "grok", "label": "Grok"}],
                          "problems": []})
        if url.path == "/api/board/output":
            if query["name"].endswith(".png"):
                return route.fulfill(status=200, content_type="image/png", body=tiny_png())
            return route.fulfill(status=200, content_type="text/plain; charset=utf-8",
                                 body="# The date\n\nPick **Oct 20**. " + HOSTILE + " [x](javascript:window.__pwned=3)")
        if url.path == "/api/board/action":
            body = json.loads(route.request.post_data)
            self.actions.append((body["op"], body["args"]))
            time.sleep(self.slow)
            if body["op"] in self.refuse:
                error, code = (self.refuse[body["op"]], "invalid") if isinstance(self.refuse[body["op"]], str) \
                    else self.refuse[body["op"]]
                return reply({"error": error, "code": code}, 409 if code == "changed" else 400)
            self.revision += 1
            args = body["args"]
            if body["op"] == "add":
                n = len(self.tasks) + 1
                self.tasks[f"T-{n}"] = self.task(n, args["title"], "open", args.get("assignee"))
                return reply({"task": self.tasks[f"T-{n}"]})
            if body["op"] == "delete":
                self.tasks.pop(args["task"])
                return reply({"deleted": args["task"]})
            if body["op"] == "approve":
                self.tasks[args["task"]]["run"] = {"state": "approved", "agent": args["agent"], "kind": args["kind"]}
            if body["op"] == "run.start":
                self.tasks[args["task"]]["run"]["state"] = "running"
            if body["op"] == "status":
                self.tasks[args["task"]]["status"] = args["to"]
            return reply({"task": self.tasks.get(args.get("task"))})
        return route.fulfill(status=404, body="{}")


def wait_until(page, script, timeout=15):
    """page.wait_for_function compiles its test with eval, which this page's CSP refuses."""
    deadline = time.monotonic() + timeout
    while not page.evaluate(script):
        assert time.monotonic() < deadline, f"timed out waiting for {script}"
        page.wait_for_timeout(50)


def open_board(browser, fake, viewport=None):
    page = browser.new_page(viewport=viewport or {"width": 1440, "height": 900})
    errors, violations = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: violations.append(m.text) if "Content Security Policy" in m.text else None)
    page.route(re.compile(r"/api/board(\?|/|$)"), fake.handle)
    page.route(re.compile(r"/api/connections(\?|/|$)"), lambda route: route.fulfill(  # (a project not hosted anywhere)
        status=200, content_type="application/json", body='{"problem": {"code": "no_origin", "message": "No origin."}}'))
    return page, errors, violations


def test_board_shows_who_has_what_and_its_text_stays_text(gui_url, browser):
    fake = FakeBoard()
    page, errors, violations = open_board(browser, fake)
    page.goto(gui_url)
    page.wait_for_selector(".rail-item[data-view=board] .rail-dot.you:not([hidden])")  # without opening it
    assert page.get_attribute(".rail-item[data-view=board] .rail-dot", "title") == "1 task waiting on you"
    page.click(".rail-item[data-view=board]")
    page.wait_for_selector(".task-card")
    columns = {c.locator("h2 span").first.text_content(): c.locator(".task-card .ref").all_inner_texts()
               for c in page.locator(".column").all()}
    assert columns == {"Waiting on you": ["T-1"], "With agents": ["T-3", "T-2"], "Not assigned": [],
                       "Blocked": ["T-5"], "Done": ["T-4"]}
    assert "claude is on it" in page.inner_text(".task-card[data-ref=T-2]")
    assert page.inner_text("#board-where") == "shop"
    assert page.is_hidden("#board-prs")  # a project not hosted anywhere has no pull requests to show

    page.click(".task-card[data-ref=T-1]")
    page.wait_for_selector("#task-title")
    wait_until(page, "document.activeElement.id === 'task-title'")
    assert page.inner_text("#task-title") == "Pick the launch date " + HOSTILE
    assert HOSTILE in page.inner_text(".task-body") and HOSTILE in page.inner_text(".history")
    page.wait_for_selector(".output-body .md h3")  # the answer, as Markdown
    assert "Oct 20" in page.inner_text(".output-body .md")
    wait_until(page, "!!document.querySelector('.output-picture') && "
                     "document.querySelector('.output-picture').naturalWidth === 2")
    assert page.get_attribute(".output-picture", "src").startswith("blob:")
    assert page.evaluate("window.__pwned") is None and page.locator("a").count() == 0
    assert page.locator("script").count() == 2  # theme.js and app.js
    assert page.locator('img:not([src="/mark.svg"]):not(.output-picture)').count() == 0
    shots = os.environ.get("IXEL_SCREENSHOT_DIR")
    if shots:
        page.screenshot(path=os.path.join(shots, "ixel-board.png"))
    page.keyboard.press("Escape")
    assert page.is_hidden("#task-panel")
    assert page.evaluate("document.activeElement.dataset.ref") == "T-1"
    assert not errors and not violations, (errors, violations)


def test_board_actions_ask_first_and_go_to_handoff(gui_url, browser):
    fake = FakeBoard()
    page, errors, _ = open_board(browser, fake)
    page.goto(gui_url + "")
    page.click(".rail-item[data-view=board]")
    page.click(".task-card[data-ref=T-3]")

    # Run it now: pick what it does, then it's approved and started
    page.click("#task-panel .actions .btn.primary")
    page.check("input[name=needs-kind][value=image]")
    assert "makes pictures" in page.inner_text(".needs")
    page.click(".needs button[type=submit]")
    page.wait_for_selector(".task-card[data-ref=T-3] .run.running")
    # (with the task as the panel showed it, so an agent's change in between isn't approved unseen)
    assert fake.actions[:2] == [("approve", {"agent": "grok", "kind": "image", "shown": "seen-T-3-2026-10-03T04:03:00Z",
                                             "task": "T-3"}),
                                ("run.start", {"task": "T-3"})]

    # A note, typed in the panel
    page.click(".task-card[data-ref=T-1]")
    page.click("#task-panel .actions button:has-text('Add a note')")
    page.fill("#needs-text", "Ask the shop first")
    page.click(".needs button[type=submit]")
    wait_until(page, "!document.querySelector('.needs')")
    assert fake.actions[-1] == ("note", {"text": "Ask the shop first", "task": "T-1"})

    # Delete asks first: no, then yes
    count = len(fake.actions)
    page.click("#task-panel .actions button:has-text('Delete')")
    page.wait_for_selector("#board-dialog[open]")
    assert "can't be undone" in page.inner_text("#board-dialog")
    page.keyboard.press("Escape")
    assert len(fake.actions) == count and page.is_visible("#task-panel")
    page.click("#task-panel .actions button:has-text('Delete')")
    page.click("#board-dialog button[value=yes]")
    page.wait_for_selector(".task-card[data-ref=T-1]", state="detached")
    assert fake.actions[-1] == ("delete", {"task": "T-1"}) and page.is_hidden("#task-panel")

    # A new task, with its checks one per line, for an agent the roster names
    page.click("#board-new")
    page.fill("#new-title", "Translate the app")
    page.fill("#new-checks", "Spanish\n\nFrench\n")
    page.wait_for_selector("#new-assignee option[value=grok]", state="attached")
    page.select_option("#new-assignee", "grok")
    page.click("#board-dialog button[value=yes]")
    page.wait_for_selector("#task-title:has-text('Translate the app')")
    assert fake.actions[-1] == ("add", {"title": "Translate the app", "body": "", "acceptance": ["Spanish", "French"],
                                        "assignee": "grok"})
    assert not errors, errors


def test_board_a_refused_action_says_why_and_keeps_what_was_typed(gui_url, browser):
    fake = FakeBoard()
    fake.refuse["note"] = "Refused: that looks like an API key."
    page, errors, _ = open_board(browser, fake)
    page.goto(gui_url)
    page.click(".rail-item[data-view=board]")
    page.click(".task-card[data-ref=T-1]")
    page.click("#task-panel .actions button:has-text('Add a note')")
    page.fill("#needs-text", "my key is sk-ant-xxxx")
    page.click(".needs button[type=submit]")
    page.wait_for_selector("#task-panel .board-notice.error")
    assert "looks like an API key" in page.inner_text("#task-panel .board-notice.error")
    assert page.input_value("#needs-text") == "my key is sk-ant-xxxx"  # to fix, not to type again
    assert not page.evaluate("[...document.querySelectorAll('#task-panel button')].some(b => b.disabled)")
    del fake.refuse["note"]
    page.fill("#needs-text", "fixed")
    page.click(".needs button[type=submit]")
    wait_until(page, "!document.querySelector('#task-panel .needs')")
    assert fake.actions[-1] == ("note", {"text": "fixed", "task": "T-1"}) and not errors


def test_board_a_task_that_changed_while_open_is_shown_again_before_it_runs(gui_url, browser):
    fake = FakeBoard()
    fake.refuse["approve"] = ("T-3 changed while you were reading it. Look at it again, then approve it.", "changed")
    page, errors, _ = open_board(browser, fake)
    page.goto(gui_url)
    page.click(".rail-item[data-view=board]")
    page.click(".task-card[data-ref=T-3]")
    page.click("#task-panel .actions .btn.primary")
    page.check("input[name=needs-kind][value=answer]")
    page.click(".needs button[type=submit]")
    page.wait_for_selector("#task-panel .board-notice.error")
    assert "changed while you were reading it" in page.inner_text("#task-panel .board-notice.error")
    assert not page.query_selector("#task-panel .needs")  # the form closed: the task as it is now, to look at
    assert [op for op, _ in fake.actions] == ["approve"] and not errors  # and nothing started


def test_board_a_double_click_does_it_once(gui_url, browser):
    fake = FakeBoard()
    fake.slow = 0.4
    page, _, _ = open_board(browser, fake)
    page.goto(gui_url)
    page.click(".rail-item[data-view=board]")
    page.click(".task-card[data-ref=T-1]")
    page.click("#task-panel .actions button:has-text('Assign')")
    page.wait_for_selector("#needs-agent option[value=codex]", state="attached")
    page.select_option("#needs-agent", "codex")
    page.dblclick(".needs button[type=submit]")
    page.click(".task-card[data-ref=T-3]")
    page.click("#task-panel .actions .btn.primary")  # Run it now
    page.dblclick(".needs button[type=submit]")
    wait_until(page, "!document.querySelector('#task-panel .needs')")
    page.wait_for_timeout(1000)
    assert [op for op, _ in fake.actions] == ["assign", "approve", "run.start"]
    assert fake.actions[0] == ("assign", {"to": "codex", "task": "T-1"})


def test_board_a_change_elsewhere_leaves_the_open_task_where_it_was(gui_url, browser):
    fake = FakeBoard()
    page, _, _ = open_board(browser, fake, viewport={"width": 1440, "height": 600})
    page.goto(gui_url)
    page.click(".rail-item[data-view=board]")
    page.click(".task-card[data-ref=T-1]")
    page.wait_for_selector(".output-body .md")
    page.evaluate("document.querySelector('.panel-scroll').scrollTop = 400")
    page.evaluate("document.querySelector('#task-panel [data-key=close]').focus({ preventScroll: true })")
    top = page.evaluate("document.querySelector('.panel-scroll').scrollTop")
    assert top > 0
    fake.revision += 1  # an agent changes another task
    fake.tasks["T-2"]["updated_at"] = "2026-10-03T05:00:00Z"
    page.wait_for_timeout(4000)
    assert page.evaluate("document.querySelector('.panel-scroll').scrollTop") == top
    assert page.evaluate("document.activeElement.dataset.key") == "close"
    page.click("#task-panel .actions button:has-text('Mark done')")  # the button goes: focus stays in the panel
    wait_until(page, "document.querySelector('#task-panel .pill').textContent === 'Done'")
    assert page.evaluate("document.activeElement.id") == "task-title"


def test_board_a_refused_new_task_keeps_the_dialog_and_its_text(gui_url, browser):
    fake = FakeBoard()
    fake.refuse["add"] = "Refused: that looks like an API key."
    page, _, _ = open_board(browser, fake)
    page.goto(gui_url)
    page.click(".rail-item[data-view=board]")
    page.wait_for_selector(".task-card")
    page.click("#board-new")
    page.fill("#new-title", "Deploy")
    page.fill("#new-body", "Long details, and sk-ant-xxxx")
    page.click("#board-dialog button[value=yes]")
    page.wait_for_selector("#board-dialog .board-notice.error")
    page.wait_for_timeout(3500)  # a look at the board meanwhile doesn't take it away
    assert page.evaluate("document.querySelector('#board-dialog').open")
    assert "looks like an API key" in page.inner_text("#board-dialog .board-notice.error")
    assert page.input_value("#new-body") == "Long details, and sk-ant-xxxx"
    del fake.refuse["add"]
    page.fill("#new-body", "Long details")
    page.click("#board-dialog button[value=yes]")
    page.wait_for_selector("#task-panel #task-title")
    assert not page.evaluate("document.querySelector('#board-dialog').open")
    assert page.inner_text("#task-title") == "Deploy"


def test_board_a_board_that_wont_start_says_so_until_dismissed(gui_url, browser):
    fake = FakeBoard()
    fake.refuse["init"] = "There's a file called .handoff in the way."
    page, _, _ = open_board(browser, fake)
    original = fake.handle

    def no_board(route):
        if route.request.url.split("?")[0].endswith("/api/board") and not fake.actions:
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"exists": False, "revision": "none", "project": "/repos/shop", "tasks": []}))
        if route.request.url.split("?")[0].endswith("/api/board"):
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"exists": False, "revision": "none", "project": "/repos/shop", "tasks": []}))
        return original(route)
    page.unroute(re.compile(r"/api/board(\?|/|$)"))
    page.route(re.compile(r"/api/board(\?|/|$)"), no_board)
    page.goto(gui_url)
    page.click(".rail-item[data-view=board]")
    page.click("button:has-text('Start a board here')")
    page.wait_for_selector("#board-main .board-notice.error")
    page.wait_for_timeout(3500)
    assert "file called .handoff" in page.inner_text("#board-main .board-notice.error")
    page.click("#board-main .board-notice.error button[aria-label=Dismiss]")
    assert page.locator("#board-main .board-notice.error").count() == 0


def test_board_without_handoff_says_how_to_add_it(gui_url, browser):
    fake = FakeBoard(hello={"ok": False, "problem": "not_installed", "version": "", "update": "", "project": "",
                            "install": {"where": "PowerShell", "command": "irm https://ixelai.com/handoff/install.ps1 | iex"}})
    page, errors, _ = open_board(browser, fake)
    page.goto(gui_url)
    page.click(".rail-item[data-view=board]")
    page.wait_for_selector(".board-message")
    assert "Handoff isn't installed" in page.inner_text(".board-message")
    assert page.inner_text(".board-message code") == "irm https://ixelai.com/handoff/install.ps1 | iex"
    assert page.is_hidden("#board-project") and page.is_disabled("#board-new")
    assert not errors, errors


def test_board_on_a_phone(gui_url, browser):
    context = browser.new_context(viewport={"width": 390, "height": 844})
    fake = FakeBoard()
    page = context.new_page()
    page.route(re.compile(r"/api/board(\?|/|$)"), fake.handle)
    page.goto(gui_url)
    page.click(".rail-item[data-view=board]")
    page.wait_for_selector(".task-card")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    page.click(".task-card[data-ref=T-1]")
    page.wait_for_selector("#task-title")
    box = page.locator("#task-panel").bounding_box()
    assert box["x"] == 0 and box["width"] == 390
    shots = os.environ.get("IXEL_SCREENSHOT_DIR")
    if shots:
        page.screenshot(path=os.path.join(shots, "ixel-board-phone.png"))
    context.close()


class FakeConnections:
    """The app server's /api/connections routes: a Gitea Ixel doesn't know yet, then one that wants a token."""

    def __init__(self, board):
        self.board = board
        self.posts = []
        self.looks = 0
        self.state = "unknown"
        self.origin = {"host": "macmini", "path": "robin/shop", "web": "http://macmini:3000"}

    def host(self):
        return {"kind": "gitea", "label": "macmini", "web": "http://macmini:3000", "set": True,
                "token": "file" if self.state == "listed" else "none"}

    def handle(self, route):
        reply = lambda data, status=200: route.fulfill(status=status, content_type="application/json",  # noqa: E731
                                                       body=json.dumps(data))
        if route.request.method == "GET":
            self.looks += 1
            if self.state == "unknown":
                return reply({"origin": self.origin, "problem": {
                    "code": "unknown_host", "message": "Ixel doesn't know what kind of host macmini is."}})
            if self.state == "token":
                return reply({"origin": self.origin, "host": self.host(), "problem": {
                    "code": "needs_token", "message": "macmini wants a token for robin/shop."}})
            return reply({"origin": self.origin, "host": self.host(), "prs": [
                {"number": 7, "title": "Pay by card " + HOSTILE, "author": "ada", "head": "card", "base": "main",
                 "head_sha": "a" * 40, "draft": False, "url": "", "updated": "2026-10-03T09:00:00Z"},
                {"number": 9, "title": "Dark mode", "author": "bo", "head": "dark", "base": "main",
                 "head_sha": "b" * 40, "draft": True, "url": "", "updated": "2026-10-03T08:00:00Z"}]})
        body = json.loads(route.request.post_data)
        path = route.request.url.split("/api/connections", 1)[1]
        self.posts.append((path, body))
        if path == "/host":
            self.state = "token"
            return reply({"ok": True})
        if path == "/token":
            self.state = "listed"
            return reply({"state": "file", "message": "Saved. Only macmini gets it."})
        n = len(self.board.tasks) + 1
        self.board.revision += 1
        what, kind = ("Review", "review") if path == "/review" else ("Fix", "edit")
        self.board.tasks[f"T-{n}"] = self.board.task(n, f"{what} pull request #7 (card into main): Pay by card", "open",
                                                     body["agent"], run={"state": "running", "agent": body["agent"],
                                                                         "kind": kind})
        self.board.tasks[f"T-{n}"]["updated_at"] = f"2026-10-03T05:0{n}:00Z"
        out = {"task": f"T-{n}", "label": "pull request #7 (card into main)"}
        return reply(out if path == "/review" else {**out, "push": f"git push origin handoff/T-{n}:refs/heads/card"})


def test_board_pull_requests_are_set_up_listed_and_reviewed(gui_url, browser):
    fake = FakeBoard()
    conn = FakeConnections(fake)
    page, errors, violations = open_board(browser, fake)
    page.route(re.compile(r"/api/connections(\?|/|$)"), conn.handle)
    page.goto(gui_url)
    page.click(".rail-item[data-view=board]")

    # A host Ixel doesn't know: which kind, and where, offered with what the origin says
    page.wait_for_selector("#board-prs #prs-url")
    assert "robin/shop on macmini" in page.inner_text("#board-prs .prs-head")
    assert page.input_value("#prs-url") == "http://macmini:3000"
    page.select_option("#prs-kind", "gitea")
    page.click("#board-prs button:has-text('Save')")

    # It wants a token: what's typed survives the board looking again in the background
    page.wait_for_selector("#board-prs #prs-token")
    assert "wants a token" in page.inner_text("#board-prs")
    page.fill("#prs-token", "gitea-secret-123")
    looks = conn.looks
    fake.revision += 1
    fake.tasks["T-1"]["title"] = "Pick the launch date (changed)"
    page.wait_for_selector(".task-card[data-ref=T-1]:has-text('(changed)')", timeout=20000)
    assert page.input_value("#prs-token") == "gitea-secret-123" and conn.looks == looks  # not asked again, not redrawn
    page.click("#board-prs button:has-text('Save token')")

    # The list: its text stays text, and the token is never shown
    page.wait_for_selector("#board-prs .pr")
    assert page.locator("#board-prs .pr .pr-number").all_inner_texts() == ["#7", "#9"]
    assert page.inner_text("#board-prs .pr:first-child .pr-title") == "Pay by card " + HOSTILE
    assert "card → main" in page.inner_text("#board-prs .pr >> nth=0") and "draft" in page.inner_text("#board-prs .pr >> nth=1")
    assert "Only macmini gets it." in page.inner_text("#board-prs")
    assert "gitea-secret-123" not in page.content()
    assert "A token for macmini is saved" in page.inner_text("#board-prs .prs-settings summary")
    assert [p for p, _ in conn.posts] == ["/host", "/token"]
    assert conn.posts[0][1] == {"project": "/repos/shop", "kind": "gitea", "url": "http://macmini:3000"}
    assert conn.posts[1][1] == {"project": "/repos/shop", "value": "gitea-secret-123"}

    # Review: asks first and who, codex by default; the review lands on the board, opened
    page.click("#board-prs .pr >> nth=0 >> button:has-text('Review')")
    page.wait_for_selector("#board-dialog[open] #pr-agent")
    assert "Review pull request #7?" in page.inner_text("#board-dialog")
    assert page.input_value("#pr-agent") == "codex"
    page.keyboard.press("Escape")
    assert len(conn.posts) == 2
    page.click("#board-prs .pr >> nth=0 >> button:has-text('Review')")
    page.select_option("#pr-agent", "claude")
    page.click("#board-dialog button[value=yes]")
    page.wait_for_selector("#task-title:has-text('Review pull request #7')")
    assert conn.posts[-1] == ("/review", {"project": "/repos/shop", "number": 7, "agent": "claude"})
    assert "T-6: claude is reviewing pull request #7 (card into main)." in page.inner_text("#board-prs")

    # Fix: Claude by default, starting from the review's answer, which stays text; nothing sent without words
    page.click("#board-prs .pr >> nth=0 >> button:has-text('Fix')")
    page.wait_for_selector("#board-dialog[open] #pr-fix-text")
    assert page.input_value("#pr-fixer") == "claude"
    wait_until(page, "document.querySelector('#pr-fix-text').value.length > 0")
    prefilled = page.input_value("#pr-fix-text")
    assert prefilled.startswith("Fix what this review found:\n\n# The date") and HOSTILE in prefilled
    assert "From T-6's review" in page.inner_text("#board-dialog")
    page.fill("#pr-fix-text", "  ")
    page.click("#board-dialog button[value=yes]")
    assert "Say what to change." in page.inner_text("#board-dialog") and conn.posts[-1][0] == "/review"
    page.fill("#pr-fix-text", "Handle a declined card.")
    page.click("#board-dialog button[value=yes]")
    page.wait_for_selector("#task-title:has-text('Fix pull request #7')")
    assert conn.posts[-1] == ("/fix", {"project": "/repos/shop", "number": 7, "agent": "claude",
                                       "text": "Handle a declined card."})
    assert page.inner_text("#board-prs .check-fix code") == "git push origin handoff/T-7:refs/heads/card"
    assert page.evaluate("window.__pwned") is None
    assert page.evaluate("window.__pwned") is None and page.locator("a").count() == 0
    shots = os.environ.get("IXEL_SCREENSHOT_DIR")
    if shots:
        page.screenshot(path=os.path.join(shots, "ixel-board-prs.png"))
    assert not errors and not violations, (errors, violations)


def test_ask_catches_up_after_another_view(gui_url, browser):
    """A run that finishes while another view is on screen still shows its verdict when you come back."""
    page = browser.new_page(viewport={"width": 1100, "height": 700})
    open_app(page, gui_url)
    page.fill("#question", "What is 17 × 23? take your time\n\nwith\nseveral\nlines")
    page.click("#ask")
    page.click(".rail-item[data-view=health]")
    page.wait_for_timeout(200)
    wait_until(page, "!!document.querySelector('.verdict')", 30)
    page.click(".rail-item[data-view=ask]")
    page.wait_for_timeout(300)
    top = page.evaluate("""() => {
        const s = document.querySelector('#scroller').getBoundingClientRect();
        return document.querySelector('.verdict').getBoundingClientRect().top - s.top; }""")
    assert 0 <= top < 200, top  # brought into view


def test_a_fix_still_fetching_cant_start_something_else(gui_url, browser):
    # Escape closes the dialog even while the fix is fetched (which can take minutes): nothing else on the list
    # starts meanwhile, the late answer closes no dialog opened since, and it still lands on the strip
    fake = FakeBoard()
    conn = FakeConnections(fake)
    conn.state = "listed"
    held = []

    def handle(route):
        if route.request.method == "POST" and route.request.url.endswith("/api/connections/fix"):
            held.append(route)
            return None
        return conn.handle(route)

    page, errors, violations = open_board(browser, fake)
    page.route(re.compile(r"/api/connections(\?|/|$)"), handle)
    page.goto(gui_url)
    page.click(".rail-item[data-view=board]")
    page.wait_for_selector("#board-prs .pr")
    page.click("#board-prs .pr >> nth=0 >> button:has-text('Fix')")
    page.wait_for_selector("#board-dialog[open] #pr-fix-text")
    page.fill("#pr-fix-text", "Handle a declined card.")
    page.click("#board-dialog button[value=yes]")
    wait_until(page, "document.querySelector('#board-dialog').textContent.includes('Fetching')")
    page.keyboard.press("Escape")
    wait_until(page, "!document.querySelector('#board-dialog').open")
    assert page.locator("#board-prs .pr >> nth=1 >> button:has-text('Review')").is_disabled()
    ref = f"T-{len(fake.tasks) + 1}"
    conn.handle(held[0])  # the fix's answer comes now
    wait_until(page, "document.querySelector('#board-prs .prs-line.said')")
    assert ref in page.inner_text("#board-prs .prs-line.said")
    assert page.inner_text("#board-prs .check-fix code") == f"git push origin handoff/{ref}:refs/heads/card"
    assert not page.locator("#board-prs .pr >> nth=1 >> button:has-text('Review')").is_disabled()
    assert not [p for p, _ in conn.posts if p == "/review"]
    assert not errors and not violations
    page.close()


def test_the_phone_drawer_closes_when_the_view_changes(gui_url, browser):
    page = browser.new_page(viewport={"width": 800, "height": 900})
    open_app(page, gui_url)
    page.click("#open-sidebar")
    assert page.evaluate("document.body.classList.contains('drawer-open')")
    page.evaluate("location.hash = '#/health'")  # the drawer covers the rail, but Back or a link moves on
    page.wait_for_selector("#view-health", state="visible")
    page.evaluate("location.hash = '#/ask'")
    page.wait_for_selector("#view-ask", state="visible")
    assert not page.evaluate("document.body.classList.contains('drawer-open')")


REAL_HANDOFF = os.environ.get("IXEL_TEST_HANDOFF_PYTHON", "")


@pytest.mark.skipif(not REAL_HANDOFF, reason="IXEL_TEST_HANDOFF_PYTHON isn't set")
def test_board_with_the_real_handoff(tmp_path, browser):
    """The whole way: the page, the app's server, `handoff api`, and a board in a real repository."""
    home = tmp_path / "home"
    home.mkdir()
    install = tmp_path / "handoff-install"
    python = install / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec "{REAL_HANDOFF}" "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    repo = tmp_path / "shop"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "HANDOFF_INSTALL_ROOT": str(install),
           "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen([sys.executable, "-m", "ixel_mat", "gui", "--no-browser"], env=env, cwd=home,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    try:
        url = None
        deadline = time.monotonic() + 30
        while url is None and time.monotonic() < deadline:
            match = URL_RE.search(proc.stdout.readline())
            url = match.group(0) if match else None
        assert url, "ixel gui did not print its URL"
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url + "")
        page.click(".rail-item[data-view=board]")
        page.fill("#board-project-input", str(repo))
        page.click("#board-project button[type=submit]")
        page.click(".board-message button:has-text('Start a board here')")
        page.wait_for_selector(".columns")
        page.click("#board-new")
        page.fill("#new-title", "Ñandú " + HOSTILE)
        page.fill("#new-body", "Line one\nLine two")
        page.click("#board-dialog button[value=yes]")
        page.wait_for_selector("#task-title")
        assert page.inner_text("#task-title") == "Ñandú " + HOSTILE
        page.click("#task-panel .actions button:has-text('Add a note')")
        page.fill("#needs-text", "From the browser")
        page.click(".needs button[type=submit]")
        page.wait_for_selector(".history .event-text:has-text('From the browser')")
        page.click("#task-panel .actions button:has-text('Mark done')")
        page.wait_for_selector(".col-done .task-card[data-ref=T-1]")
        assert page.evaluate("window.__pwned") is None
        assert not errors, errors
        shots = os.environ.get("IXEL_SCREENSHOT_DIR")
        if shots:
            page.screenshot(path=os.path.join(shots, "ixel-board-real.png"))
    finally:
        proc.terminate()
        proc.wait(10)


# ── Settings ──────────────────────────────────────────────────────────────────

SETTINGS_TOML = "".join(
    f'[agents.{aid}]\ntype = "http"\nurl = "http://127.0.0.1:9/v1/chat/completions"\n'
    f'token_env = "IXEL_TEST_PANEL_KEY"\nmodel = "{model}"\nlabel = {label}\n\n'
    for aid, model, label in [("gpt", "m-gpt", '"GPT-5"'), ("claude", "m-xss", f"'{HOSTILE}'"),
                              ("gemini", "m-wrong", '"Gemini"')])
SETTINGS_TOML = "# My models (this line has to stay)\n" + SETTINGS_TOML + \
    '[review]\nmode = "review"   # how much checking\ntimeout = 120\n'


@pytest.fixture
def settings_gui(tmp_path):
    home = tmp_path / "home"
    path = home / ".config" / "ixel-mat" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(SETTINGS_TOML, encoding="utf-8")
    with launch_gui(home, {"IXEL_TEST_PANEL_KEY": "sk-test"}) as url:
        yield url, path


def open_settings(browser, url, **options):
    page = browser.new_page(**(options or {"viewport": {"width": 1280, "height": 900}}))
    errors, violations = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: violations.append(m.text) if "Content Security Policy" in m.text else None)
    page.goto(url)
    page.click(".rail-item[data-view=settings]")
    page.wait_for_selector("#settings-body .set-group")
    return page, errors, violations


def setting(page, key):
    return page.locator(f'#settings-body [data-key="{key}"]')


def wait_for_file(path, text, timeout=10):
    deadline = time.monotonic() + timeout
    while text not in path.read_text(encoding="utf-8"):
        assert time.monotonic() < deadline, f"{text!r} never reached {path}:\n{path.read_text(encoding='utf-8')}"
        time.sleep(0.05)


def test_settings_are_saved_as_they_change_and_ask_follows(settings_gui, browser):
    url, path = settings_gui
    page, errors, violations = open_settings(browser, url)
    assert page.title() == "Settings · Ixel" and page.evaluate("document.activeElement.id") == "settings-title"
    assert page.locator(".set-agent .set-label").all_inner_texts() == ["GPT-5", HOSTILE, "Gemini"]
    assert injected(page) == CLEAN
    assert str(path) in page.inner_text(".set-intro")

    # Gemini off the panel: the file says so, the rest of it stays as written, and Ask shows two models
    setting(page, "agent:gemini:on").uncheck()
    wait_for_file(path, 'agents = ["gpt", "claude"]')
    page.wait_for_selector("[data-note=models].ok")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# My models (this line has to stay)\n[agents.gpt]")
    assert path.with_suffix(".toml.bak").read_text(encoding="utf-8") == SETTINGS_TOML

    # A new default mode: Ask moves to it, since no mode was picked there
    setting(page, "review:mode").select_option("deep")
    wait_for_file(path, 'mode = "deep"   # how much checking')
    page.click(".rail-item[data-view=ask]")
    wait_until(page, "document.querySelector('.modes button.on').dataset.mode === 'deep'")
    assert page.locator(".model:not(.triage) .name").all_inner_texts() == ["GPT-5", HOSTILE]

    # A mode picked in Ask stays picked when the default changes again
    page.click(".modes button[data-mode=quick]")
    page.click(".rail-item[data-view=settings]")
    page.wait_for_selector("[data-note=review]:empty", state="attached")  # an old "Saved" is gone on the way back
    setting(page, "review:mode").select_option("review")
    wait_for_file(path, 'mode = "review"')
    page.wait_for_selector("[data-note=review].ok")
    page.click(".rail-item[data-view=ask]")
    assert page.get_attribute(".modes button.on", "data-mode") == "quick"

    # Other… takes a name that isn't in the list: saved on Enter; one that isn't a model name is refused
    # and kept to fix
    page.click(".rail-item[data-view=settings]")
    assert setting(page, "agent:gpt:model").input_value() == "m-gpt"
    setting(page, "agent:gpt:model").select_option(label="Other…")
    name = setting(page, "agent:gpt:model-name")
    name.wait_for()
    assert page.evaluate("document.activeElement.dataset.key") == "agent:gpt:model-name"
    name.fill("m-new")
    name.press("Enter")
    wait_for_file(path, 'model = "m-new"')
    page.wait_for_selector("[data-note=models].ok")
    assert setting(page, "agent:gpt:model").input_value() == "m-new" and name.count() == 0
    setting(page, "agent:gpt:model").select_option(label="Other…")
    name.fill("--model x; rm -rf ~")
    name.press("Enter")
    page.wait_for_selector("[data-note=models].fail")
    assert "isn't a model name" in page.inner_text("[data-note=models]")
    assert name.input_value() == "--model x; rm -rf ~"
    assert "rm -rf" not in path.read_text(encoding="utf-8")

    setting(page, "review:timeout").fill("45")
    setting(page, "review:timeout").press("Tab")
    wait_for_file(path, "timeout = 45")
    assert not errors and not violations, (errors, violations)
    shots = os.environ.get("IXEL_SCREENSHOT_DIR")
    if shots:
        page.screenshot(path=os.path.join(shots, "ixel-settings.png"), full_page=True)


def test_settings_models_are_picked_from_a_list(tmp_path, browser):
    home = tmp_path / "home"
    path = home / ".config" / "ixel-mat" / "config.toml"
    path.parent.mkdir(parents=True)
    with ThreadedFakeProvider() as fake:
        fake.models = ["qwen3", "llama3.3:latest", HOSTILE]
        path.write_text(f'[agents.local]\ntype = "http"\nurl = "{fake.openai_url}"\nlabel = "LM Studio"\n'
                        'model = "qwen3"\n\n[agents.claude]\npreset = "claude_code"\n', encoding="utf-8")
        with launch_gui(home, {"ANTHROPIC_API_KEY": ""}) as url:
            page, errors, violations = open_settings(browser, url)
            # A model server of your own: what it has, and no Default (it needs a name)
            page.wait_for_selector('[data-key="agent:local:model"] option[value="llama3.3:latest"]', state="attached")
            local = setting(page, "agent:local:model")
            assert local.input_value() == "qwen3"
            assert local.locator("option").all_inner_texts() == ["qwen3", "llama3.3:latest", "Other…"]
            assert "Models from your model server." in page.inner_text(".set-agent:has([data-key='agent:local:model'])")
            local.select_option("llama3.3:latest")
            wait_for_file(path, 'model = "llama3.3:latest"')
            page.wait_for_selector("[data-note=models].ok")

            # Claude Code: its own short names first, then the models (built in, with no Anthropic key)
            claude = setting(page, "agent:claude:model")
            texts = claude.locator("option").all_inner_texts()
            assert texts[:5] == ["Default (Claude Code's own)", "fable (the newest Fable)", "opus (the newest Opus)",
                                 "sonnet (the newest Sonnet)", "haiku (the newest Haiku)"]
            assert "claude-opus-5-5" in texts and texts[-1] == "Other…"
            assert "Save an Anthropic key under Keys" in page.inner_text(
                ".set-agent:has([data-key='agent:claude:model'])")
            claude.select_option("opus")
            wait_for_file(path, 'model = "opus"')
            assert injected(page) == CLEAN
            assert not errors and not violations, (errors, violations)
            shots = os.environ.get("IXEL_SCREENSHOT_DIR")
            if shots:
                page.locator("#settings-body .set-group").first.screenshot(
                    path=os.path.join(shots, "ixel-settings-models.png"))


def test_settings_effort_follows_the_model_latest_is_now(tmp_path, browser):
    home = tmp_path / "home"
    path = home / ".config" / "ixel-mat" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('[agents.gpt]\ntype = "http"\nurl = "https://api.openai.com/v1/chat/completions"\n'
                    'token_env = "OPENAI_API_KEY"\nlabel = "GPT"\nmodel = "latest"\neffort = "minimal"\n',
                    encoding="utf-8")
    # OpenAI's list, answered here: the app is started with this module loaded first
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "sitecustomize.py").write_text(
        "from ixel_mat.gui import model_choices\n"
        "model_choices.company_models = lambda provider, key: [\n"
        "    {'id': 'gpt-6-astra', 'created': 8}, {'id': 'gpt-5', 'created': 5}, {'id': 'gpt-6.1-sol', 'created': 9}]\n",
        encoding="utf-8")
    with launch_gui(home, {"PYTHONPATH": str(hooks), "OPENAI_API_KEY": "sk-test-0123456789abcdef"}) as url:
        page, errors, violations = open_settings(browser, url)
        effort = page.get_by_role("combobox", name="GPT effort")

        def levels(expected):
            wait_until(page, "JSON.stringify([...document.querySelector('[data-key=\"agent:gpt:effort\"]')"
                             f".options].map((o) => o.text)) === {json.dumps(json.dumps(expected, separators=(',', ':')))}")

        # Latest is GPT-6 Astra now: low to max, and the minimal saved before is sent as low
        page.wait_for_selector('[data-key="agent:gpt:model"] option:text("Latest (gpt-6-astra)")', state="attached")
        levels(["Default", "Low", "Medium", "High", "Xhigh", "Max", "Minimal (sent as Low)"])
        assert effort.input_value() == "minimal"
        # GPT-5 takes minimal to high
        setting(page, "agent:gpt:model").select_option("gpt-5")
        wait_for_file(path, 'model = "gpt-5"')
        levels(["Default", "Minimal", "Low", "Medium", "High"])
        # Latest fast is GPT-6.1 Sol now, which takes low to max again
        setting(page, "agent:gpt:model").select_option("latest-fast")
        wait_for_file(path, 'model = "latest-fast"')
        levels(["Default", "Low", "Medium", "High", "Xhigh", "Max", "Minimal (sent as Low)"])
        assert not errors and not violations, (errors, violations)


def test_settings_add_and_take_off_a_model_from_a_server_of_yours(tmp_path, browser):
    home = tmp_path / "home"
    path = home / ".config" / "ixel-mat" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(SETTINGS_TOML, encoding="utf-8")
    with ThreadedFakeProvider() as fake:
        fake.models = ["qwen3:14b", "nomic-embed-text", HOSTILE]
        base = f"http://127.0.0.1:{fake.port}/v1"
        with launch_gui(home, {"IXEL_TEST_PANEL_KEY": "sk-test"}) as url:
            page, errors, violations = open_settings(browser, url)
            # Letting other computers in lets anyone on that network in, and the card says so
            assert "anyone on that network can then use it" in page.inner_text(".set-group:has(#set-h-servers)")
            # Ixel looks only where it's told: here, the address typed
            setting(page, "servers:address").fill(f"127.0.0.1:{fake.port}")
            setting(page, "servers:address").press("Enter")
            page.wait_for_selector("#settings-body .set-model-name")
            group = page.locator(".set-group:has(#set-h-servers)")
            assert "Model server on this computer" in group.inner_text()
            assert "Left out, since they can't answer questions: nomic-embed-text." in group.inner_text()
            assert group.locator(".set-model-name").all_inner_texts() == ["qwen3:14b", HOSTILE]

            setting(page, f"servers:add:{base}:qwen3:14b").click()
            wait_for_file(path, "[agents.qwen3_14b]")
            page.wait_for_selector("[data-note=servers].ok")
            assert "Added qwen3:14b (local)" in page.inner_text("[data-note=servers]")
            assert "On your list" in group.inner_text()
            text = path.read_text(encoding="utf-8")
            added = text.split("[agents.qwen3_14b]")[1]
            assert text.startswith("# My models (this line has to stay)") and "token_env" not in added
            assert f'url = "{base}/chat/completions"' in added

            # The new model shows where its server is, and comes off again after a second press
            assert setting(page, "agent:qwen3_14b:url").input_value() == base
            setting(page, "agent:qwen3_14b:remove").click()
            setting(page, "agent:qwen3_14b:confirm").click()
            page.wait_for_selector('[data-key="agent:qwen3_14b:url"]', state="detached")
            assert "[agents.qwen3_14b]" not in path.read_text(encoding="utf-8")
            assert setting(page, "agent:gpt:remove").count() == 0  # a model with a key isn't taken off here
            assert injected(page) == CLEAN
            assert not errors and not violations, (errors, violations)
            shots = os.environ.get("IXEL_SCREENSHOT_DIR")
            if shots:
                group.screenshot(path=os.path.join(shots, "ixel-settings-servers.png"))


def test_settings_keys_go_in_and_never_come_back(settings_gui, browser):
    url, path = settings_gui
    page, errors, violations = open_settings(browser, url)
    secret = "sk-proj-SettingsPageTest-0123456789abcdef"
    row = page.locator(".set-key", has=page.locator("code", has_text="OPENAI_API_KEY"))
    assert row.locator(".set-state").inner_text() == "Not set"
    outside = page.locator(".set-key", has=page.locator("code", has_text="IXEL_TEST_PANEL_KEY"))
    assert outside.locator(".set-state").inner_text() == "Set outside Ixel"

    row.locator("input[type=password]").fill(secret)
    row.locator("input[type=password]").press("Enter")
    page.wait_for_selector('[data-note="key:OPENAI_API_KEY"].ok')
    assert "Saved OpenAI's key" in row.inner_text()
    assert row.locator(".set-state").inner_text() == "Saved in Ixel"
    keys_file = path.parent / "keys.enc"  # encrypted, with the key in the suite's keychain (in memory)
    assert keys_file.exists() and secret.encode() not in keys_file.read_bytes()
    assert not (path.parent / ".env").exists()
    assert page.locator("section[aria-labelledby=set-h-keys] .set-hint").inner_text().startswith(
        "Keys saved in Ixel are encrypted, and the key that opens them is kept in your keychain.")

    # Nowhere in the page, nor in anything the page can ask for
    assert row.locator("input[type=password]").input_value() == ""
    assert secret not in page.content()
    served = page.evaluate("""fetch('/api/settings', {headers: {Authorization: 'Bearer '
                              + sessionStorage.getItem('ixel-session')}}).then((r) => r.text())""")
    assert '"OPENAI_API_KEY"' in served and secret not in served

    # Remove asks first; Keep it keeps it
    row.locator("button:has-text('Remove')").click()
    assert "Remove OpenAI's key from Ixel?" in row.inner_text()
    assert page.evaluate("document.activeElement.textContent") == "Remove"
    row.locator("button:has-text('Keep it')").click()
    assert page.evaluate("document.activeElement.textContent") == "Remove"
    assert keys_file.exists()
    row.locator("button:has-text('Remove')").click()
    row.locator("button:has-text('Remove')").click()
    page.wait_for_selector('[data-note="key:OPENAI_API_KEY"]:has-text("Removed")')
    assert row.locator(".set-state").inner_text() == "Not set"
    assert not keys_file.exists()  # its only key is gone

    # Something that isn't a key is refused before it's saved
    row.locator("input[type=password]").fill("my key has spaces")
    row.locator("button:has-text('Save')").click()
    page.wait_for_selector('[data-note="key:OPENAI_API_KEY"].fail')
    assert "doesn't look like a key" in row.inner_text()
    assert not errors and not violations, (errors, violations)


def test_settings_a_file_changed_elsewhere_is_shown_again_before_a_change(settings_gui, browser):
    url, path = settings_gui
    page, errors, _ = open_settings(browser, url)
    path.write_text(path.read_text(encoding="utf-8").replace("timeout = 120", "timeout = 300"), encoding="utf-8")
    setting(page, "review:mode").select_option("quick")
    page.wait_for_selector("[data-note=review].fail")
    assert "changed since this page read it" in page.inner_text("[data-note=review]")
    assert setting(page, "review:timeout").input_value() == "300"   # what the file says now
    assert setting(page, "review:mode").input_value() == "review"   # the change didn't land
    assert 'mode = "review"' in path.read_text(encoding="utf-8")
    setting(page, "review:mode").select_option("quick")              # once more, on the file as it is
    wait_for_file(path, 'mode = "quick"')
    assert "timeout = 300" in path.read_text(encoding="utf-8")
    assert not errors, errors


def test_settings_without_a_file_say_so_and_still_take_keys(tmp_path, browser):
    home = tmp_path / "home"
    home.mkdir()
    with launch_gui(home, {}) as url:
        page, errors, _ = open_settings(browser, url)
        assert "There's no settings file yet" in page.inner_text("#settings-body .notice")
        assert setting(page, "review:mode").is_disabled()
        assert "No models yet" in page.inner_text("#settings-body")
        row = page.locator(".set-key", has=page.locator("code", has_text="ANTHROPIC_API_KEY"))
        row.locator("input[type=password]").fill("sk-ant-test-0123456789")
        row.locator("button:has-text('Save')").click()
        page.wait_for_selector('[data-note="key:ANTHROPIC_API_KEY"].ok')
        keys_file = home / ".config" / "ixel-mat" / "keys.enc"
        assert keys_file.exists() and b"sk-ant-test-0123456789" not in keys_file.read_bytes()
        assert not errors, errors


def test_settings_on_a_phone(settings_gui, browser):
    url, _ = settings_gui
    context = browser.new_context(color_scheme="light", viewport={"width": 390, "height": 844})
    page = context.new_page()
    page.goto(url)
    page.click(".rail-item[data-view=settings]")
    page.wait_for_selector(".set-key")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390
    assert page.evaluate("document.querySelector('#view-settings .scroller').scrollWidth") <= 390
    shots = os.environ.get("IXEL_SCREENSHOT_DIR")
    if shots:
        page.screenshot(path=os.path.join(shots, "ixel-settings-phone-light.png"), full_page=True)
    context.close()


def hold_settings_saves(page):
    """Saves to the settings file wait until the test lets each one go (keys aren't held)."""
    held = []

    def handle(route):
        if route.request.method == "POST":
            held.append(route)
        else:
            route.continue_()
    page.route(re.compile(r"/api/settings$"), handle)
    return held


def wait_for_held(page, held, n=1):
    deadline = time.monotonic() + 10
    while len(held) < n:
        assert time.monotonic() < deadline, "the save never started"
        page.wait_for_timeout(50)


def test_settings_a_refused_change_drops_only_file_changes_and_shows_the_file(settings_gui, browser):
    url, path = settings_gui
    page, errors, _ = open_settings(browser, url)
    held = hold_settings_saves(page)
    path.write_text(path.read_text(encoding="utf-8").replace('model = "m-gpt"', 'model = "m-gpt-2"'), encoding="utf-8")
    setting(page, "agent:gpt:model").select_option(label="Other…")
    name = setting(page, "agent:gpt:model-name")
    name.fill("m-typed")
    name.press("Enter")                                       # refused once it's let go: the file changed
    wait_for_held(page, held)
    setting(page, "review:timeout").fill("60")
    setting(page, "review:timeout").press("Enter")            # queued behind it: dropped with it
    key = setting(page, "key:OPENAI_API_KEY:value")
    key.fill("sk-proj-QueuedBehindARefusal-0123456789")
    key.press("Enter")                                        # also queued, but a key doesn't depend on the file
    held[0].continue_()
    page.wait_for_selector('[data-note="key:OPENAI_API_KEY"].ok')
    assert "changed since this page read it" in page.inner_text("[data-note=models]")
    assert "Not saved, since the file changed" in page.inner_text("[data-note=review]")
    assert setting(page, "agent:gpt:model").input_value() == "m-gpt-2"   # the file, not what was typed over it
    assert name.count() == 0
    assert "sk-proj-QueuedBehindARefusal" in (path.parent / ".env").read_text(encoding="utf-8")
    assert "timeout = 120" in path.read_text(encoding="utf-8")

    # Remove asks at once, even while a save is under way, and focus follows
    page.unroute(re.compile(r"/api/settings$"))
    held = hold_settings_saves(page)
    setting(page, "review:mode").select_option("deep")
    wait_for_held(page, held)
    page.get_by_role("button", name="Remove OpenAI's key").click()
    assert page.evaluate("document.activeElement.dataset.key") == "key:OPENAI_API_KEY:confirm"
    setting(page, "key:OPENAI_API_KEY:keep").click()
    assert page.evaluate("document.activeElement.dataset.key") == "key:OPENAI_API_KEY:remove"
    setting(page, "key:OPENAI_API_KEY:remove").click()
    setting(page, "key:OPENAI_API_KEY:confirm").click()
    held[0].continue_()
    page.wait_for_selector('[data-note="key:OPENAI_API_KEY"]:has-text("Removed")')
    wait_until(page, "document.activeElement.dataset.key === 'key:OPENAI_API_KEY:value'")
    assert not errors, errors


def test_settings_saver_drafters_labels_time_limit_and_asks_warning(tmp_path, browser):
    home = tmp_path / "home"
    path = home / ".config" / "ixel-mat" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(SETTINGS_TOML.replace('mode = "review"', 'mode = "auto"')
                    + '\n[saver]\nverifier = "claude"\n', encoding="utf-8")
    with launch_gui(home, {"IXEL_TEST_PANEL_KEY": "sk-test"}) as url:
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url)
        page.wait_for_selector("#notice:not([hidden])")
        assert "auto" in page.inner_text("#notice")              # Auto without Triage: Ask says what runs instead
        page.click(".rail-item[data-view=settings]")
        page.wait_for_selector(".set-agent")

        # Every control says which model or key it's for
        assert page.get_by_role("checkbox", name="Gemini on the panel").is_checked()
        assert page.get_by_role("combobox", name="GPT-5 model").input_value() == "m-gpt"
        assert page.get_by_role("combobox", name="Gemini effort").count() == 1
        # Only the levels a server like this takes (it would only pass anything higher on as high)
        assert page.get_by_role("combobox", name="Gemini effort").locator("option").all_inner_texts() == [
            "Default", "Minimal", "Low", "Medium", "High"]
        assert page.get_by_role("button", name="Save OpenAI's key").count() == 1

        # Drafters: the page shows what runs
        setting(page, "saver:draft:gemini").uncheck()
        wait_for_file(path, 'drafters = ["gpt"]')
        setting(page, "saver:verifier").select_option("gpt")   # gpt verifies: its draft is gone, so all others draft
        wait_for_file(path, 'verifier = "gpt"')
        page.wait_for_selector("[data-note=saver].ok")
        assert setting(page, "saver:draft:claude").is_checked() and setting(page, "saver:draft:gemini").is_checked()

        # The time limit is a plain number of seconds
        setting(page, "review:timeout").fill("0x1f4")
        setting(page, "review:timeout").press("Enter")
        page.wait_for_selector("[data-note=review].fail")
        assert "timeout = 120" in path.read_text(encoding="utf-8")

        # A fixed warning leaves Ask too
        setting(page, "review:mode").select_option("review")
        wait_for_file(path, 'mode = "review"')
        page.click(".rail-item[data-view=ask]")
        wait_until(page, "document.querySelector('#notice').hidden")


# ── Pictures in Ask ───────────────────────────────────────────────────────────

@pytest.fixture
def pictures_gui(tmp_path):
    home = tmp_path / "home"
    path = home / ".config" / "ixel-mat" / "config.toml"
    path.parent.mkdir(parents=True)
    with ThreadedFakeProvider(panel_handler(ANSWERS)) as fake:
        path.write_text("".join(
            f'[agents.{aid}]\ntype = "http"\nurl = "{fake.openai_url}"\ntoken_env = "IXEL_TEST_PANEL_KEY"\n'
            f'model = "{model}"\nlabel = "{label}"\naccepts = {accepts}\n\n'
            for aid, model, label, accepts in [("vision", "m-gpt", "Vision", '["image"]'),
                                               ("texty", "m-wrong", "Texty", "[]")]), encoding="utf-8")
        with launch_gui(home, {"IXEL_TEST_PANEL_KEY": "sk-test"}) as url:
            yield url, fake, path


# A picture made in the page, then pasted into the question or dropped on the page
MAKE = """([how, type, width, height]) => new Promise((done) => {
  const canvas = document.createElement("canvas");
  canvas.width = width; canvas.height = height;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#c33"; ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = "#3c3"; ctx.fillRect(0, 0, width / 2, height / 2);
  canvas.toBlob((blob) => {
    const data = new DataTransfer();
    data.items.add(new File([blob], "made", { type }));
    const target = how === "paste" ? document.querySelector("#question") : document.querySelector(".composer");
    const event = how === "paste" ? new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true })
      : new DragEvent("drop", { dataTransfer: data, bubbles: true, cancelable: true });
    target.dispatchEvent(event);
    done();
  }, type);
})"""
READY = "document.querySelectorAll('#pic-list .pic').length === {n} && !document.querySelector('#pic-list .working')"


def png_size(data_url):
    import base64
    import struct
    data = base64.b64decode(data_url.split(",", 1)[1])
    return struct.unpack(">II", data[16:24])


def test_pictures_are_attached_and_go_only_to_the_models_that_see_them(pictures_gui, browser):
    url, fake, path = pictures_gui
    page = browser.new_page(viewport={"width": 1100, "height": 900})
    errors, violations = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: violations.append(m.text) if "Content Security Policy" in m.text else None)
    open_app(page, url)
    assert page.locator(".model .sees").count() == 1 and "Vision" in page.inner_text(".model:has(.sees)")

    # Picked, pasted and dropped; the wide one is made 2048 pixels across; one is taken out again
    page.set_input_files("#picture-file", files=[{"name": "a.png", "mimeType": "image/png", "buffer": tiny_png()}])
    page.evaluate(MAKE, ["paste", "image/png", 3000, 1000])
    page.evaluate(MAKE, ["drop", "image/jpeg", 640, 480])
    wait_until(page, READY.format(n=3))
    sizes = page.eval_on_selector_all("#pic-list img", "(imgs) => imgs.map((i) => [i.width, i.height].join('x'))")
    assert page.eval_on_selector_all("#pic-list img", "(imgs) => imgs.map((i) => i.getAttribute('width'))") \
        == ["2", "2048", "640"], sizes
    assert "Only Vision sees them" in page.inner_text("#pic-note")
    page.locator(".pic-remove").nth(0).click()
    wait_until(page, READY.format(n=2))
    shots = os.environ.get("IXEL_SCREENSHOT_DIR")
    if shots:
        page.screenshot(path=os.path.join(shots, "ixel-pictures-tray.png"))

    page.click(".modes button[data-mode=quick]")
    page.fill("#question", "What is 17 × 23?")
    page.click("#ask")
    page.wait_for_selector(".verdict", timeout=30_000)
    assert page.locator("#pic-tray").is_hidden()
    assert page.locator(f"{LAST} .shots img").count() == 2

    calls = {}
    for r in fake.requests:
        calls.setdefault(r.body["model"], []).append(r.body["messages"][0]["content"])
    seen = [c for c in calls["m-gpt"] if isinstance(c, list)]
    assert seen and all([p["type"] for p in c] == ["text", "image_url", "image_url"] for c in seen)
    wide, photo = (p["image_url"]["url"] for p in seen[0][1:])
    assert png_size(wide) == (2048, 683) and photo.startswith("data:image/jpeg;base64,/9j/")  # Chrome's JPEG, read
    assert all(isinstance(c, str) and "which you can't see" in c for c in calls["m-wrong"])

    if shots:
        page.screenshot(path=os.path.join(shots, "ixel-pictures-sent.png"))
    page.click(f"{LAST} .shots")
    assert page.get_attribute(f"{LAST} .shots", "aria-pressed") == "true"

    # After a reload the question says it had pictures (they aren't kept in the page)
    page.reload()
    page.wait_for_selector(".verdict")
    assert "2 pictures (not kept after a reload)" in page.inner_text(f"{LAST} .shots")

    # At most 8 for a question
    page.set_input_files("#picture-file", files=[
        {"name": f"{i}.png", "mimeType": "image/png", "buffer": tiny_png()} for i in range(9)])
    wait_until(page, READY.format(n=8))
    assert "at most 8 pictures" in page.inner_text("#notice")

    # Settings: the other model is switched on, and Ask says so at once
    page.click(".rail-item[data-view=settings]")
    page.locator('#settings-body [data-key="agent:texty:pictures"]').check()
    wait_for_file(path, '[agents.texty]\ntype = "http"')
    deadline = time.monotonic() + 10
    while 'accepts = ["image"]' not in path.read_text(encoding="utf-8").split("[agents.texty]")[1]:
        assert time.monotonic() < deadline, path.read_text(encoding="utf-8")
        time.sleep(0.05)
    page.click(".rail-item[data-view=ask]")
    wait_until(page, "document.querySelector('#pic-note').textContent.includes('Every model on the panel sees them')")
    assert page.locator(".model .sees").count() == 2

    assert not errors, errors
    assert not violations, violations


def test_pictures_that_fail_expire_or_come_with_text_are_handled(pictures_gui, browser):
    url, fake, path = pictures_gui
    page = browser.new_page(viewport={"width": 1100, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    uploads = []
    page.on("request", lambda r: uploads.append(r.url) if r.url.endswith("/api/pictures") else None)
    open_app(page, url)
    page.click(".modes button[data-mode=quick]")

    # A picture that can't be read, with Ask pressed while it's read: nothing is asked, and the reason stays
    page.fill("#question", "What's wrong in this screenshot?")
    page.evaluate("""() => {
      const data = new DataTransfer();
      data.items.add(new File([new Uint8Array([1, 2, 3, 4])], "shot.png", { type: "image/png" }));
      document.querySelector("#question").dispatchEvent(
        new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
      document.querySelector("#ask").click();
    }""")
    wait_until(page, "!document.querySelector('#notice').hidden")
    page.wait_for_timeout(300)
    assert "can't be opened here" in page.inner_text("#notice")
    assert page.locator(".turn").count() == 0 and page.input_value("#question") == "What's wrong in this screenshot?"

    # Text copied from Office comes with a picture of itself: the text is pasted, no picture attached
    prevented = page.evaluate("""() => new Promise((done) => {
      const c = document.createElement("canvas"); c.width = 50; c.height = 20;
      c.toBlob((b) => {
        const data = new DataTransfer();
        data.setData("text/plain", "A1\\tB1");
        data.items.add(new File([b], "image.png", { type: "image/png" }));
        const event = new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true });
        document.querySelector("#question").dispatchEvent(event);
        done(event.defaultPrevented);
      }, "image/png");
    })""")
    page.wait_for_timeout(300)
    assert prevented is False and page.locator("#pic-list .pic").count() == 0

    # Focus stays on a picture's Remove while another one finishes
    page.set_input_files("#picture-file", files=[{"name": "a.png", "mimeType": "image/png", "buffer": tiny_png()}])
    wait_until(page, READY.format(n=1))
    page.focus("#pic-list .pic-remove")
    page.evaluate(MAKE, ["drop", "image/png", 300, 200])
    wait_until(page, READY.format(n=2))
    assert page.evaluate("document.activeElement.getAttribute('aria-label')") == "Remove picture 1"

    # Ixel no longer has them (kept 30 minutes): they're sent again, and asking again works
    held = []
    page.route("**/api/review", lambda route: held.append(route) if not held else route.continue_())
    page.fill("#question", "What is 17 × 23?")
    page.click("#ask")
    wait_until(page, "document.querySelector('.turn') !== null")
    deadline = time.monotonic() + 10
    while not held:
        assert time.monotonic() < deadline
        page.wait_for_timeout(50)
    page.set_input_files("#picture-file", files=[{"name": "b.png", "mimeType": "image/png", "buffer": tiny_png()}])
    wait_until(page, READY.format(n=1))
    before = len(uploads)
    held[0].fulfill(status=400, content_type="application/json",
                    body='{"error": "A picture you attached is no longer here.", "code": "pictures_gone"}')
    wait_until(page, READY.format(n=3))
    assert len(uploads) == before + 2  # the two it had sent, attached again; the new one stays after them
    assert page.input_value("#question") == "What is 17 × 23?"
    widths = page.eval_on_selector_all("#pic-list img", "(imgs) => imgs.map((i) => i.getAttribute('width'))")
    assert widths == ["2", "300", "2"], widths
    page.click("#ask")
    page.wait_for_selector(f"{LAST} .verdict", timeout=30_000)
    assert page.locator(f"{LAST} .shots img").count() == 3
    assert not errors, errors


# ── Sound in Ask ──────────────────────────────────────────────────────────────

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 60
SOUND_SAID = "document.querySelector('#sound-state').textContent.includes({text!r})"


@pytest.fixture
def sound_gui(tmp_path):
    from test_sound import Transcriber
    home = tmp_path / "home"
    path = home / ".config" / "ixel-mat" / "config.toml"
    path.parent.mkdir(parents=True)
    with ThreadedFakeProvider(panel_handler(ANSWERS)) as fake, \
            Transcriber(reply={"text": "What does this error mean?"}) as service:
        path.write_text(f'[agents.gpt]\ntype = "http"\nurl = "{fake.openai_url}"\ntoken_env = "IXEL_TEST_PANEL_KEY"\n'
                        f'model = "m-gpt"\nlabel = "GPT-5"\n\n[sound]\nopenai_url = "{service.url}"\n', encoding="utf-8")
        with launch_gui(home, {"IXEL_TEST_PANEL_KEY": "sk-test", "OPENAI_API_KEY": "sk-sound", "GROQ_API_KEY": ""}) as url:
            yield url, service


@pytest.fixture
def mic_browser():
    """Chromium with a pretend microphone that's allowed without asking."""
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch(args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                                        "--autoplay-policy=no-user-gesture-required"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium not available: {exc}")
        yield b
        b.close()


def test_sound_is_written_out_into_the_question(sound_gui, mic_browser):
    url, service = sound_gui
    page = mic_browser.new_page(viewport={"width": 1100, "height": 900})
    errors, violations = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: violations.append(m.text) if "Content Security Policy" in m.text else None)
    open_app(page, url)
    page.click("#sound-add")
    assert page.get_attribute("#sound-add", "aria-expanded") == "true"
    assert "goes to OpenAI" in page.inner_text("#sound-state")

    # A file picked: its words go where the cursor is
    page.fill("#question", "Explain this:")
    page.set_input_files("#sound-file", files=[{"name": "note.webm", "mimeType": "audio/webm", "buffer": WEBM}])
    wait_until(page, SOUND_SAID.format(text="OpenAI wrote it out"))
    assert page.input_value("#question") == "Explain this: What does this error mean?"
    [sent] = service.requests
    assert sent["headers"]["Authorization"] == "Bearer sk-sound" and WEBM in sent["body"]

    # Recorded from the microphone
    page.fill("#question", "")
    page.click("#sound-record")
    wait_until(page, "!document.querySelector('#sound-stop').disabled")
    assert page.is_visible("#sound-clock") and page.is_hidden("#sound-pick")
    shots = os.environ.get("IXEL_SCREENSHOT_DIR")
    if shots:
        page.screenshot(path=os.path.join(shots, "ixel-sound-recording.png"))
    page.wait_for_timeout(1500)
    page.click("#sound-stop")
    wait_until(page, "document.querySelector('#question').value === 'What does this error mean?'")
    assert len(service.requests) == 2
    recorded = service.requests[1]["body"]
    assert b'filename="sound.webm"' in recorded and len(recorded) > 1000

    # Cancelled while it's written out: nothing lands, and Ask waits for it meanwhile
    service.delay = 1.5
    page.fill("#question", "Keep me")
    page.evaluate("""() => {
      const data = new DataTransfer();
      data.items.add(new File([new Uint8Array([0x1a, 0x45, 0xdf, 0xa3, ...new Array(60).fill(0)])], "memo.webm",
                              { type: "audio/webm" }));
      document.querySelector(".composer").dispatchEvent(new DragEvent("drop", { dataTransfer: data, bubbles: true, cancelable: true }));
    }""")
    wait_until(page, SOUND_SAID.format(text="writing it out"))
    page.click("#ask")
    assert "still being written out" in page.inner_text("#notice") and page.locator(".turn").count() == 0
    page.click("#sound-cancel")
    assert "Cancelled" in page.inner_text("#sound-state")
    page.wait_for_timeout(2000)
    assert page.input_value("#question") == "Keep me"

    # What the service says reaches the page; closing the bar puts you back in the question
    service.delay = 0
    page.set_input_files("#sound-file", files=[{"name": "x.webm", "mimeType": "audio/webm", "buffer": b"not sound at all"}])
    wait_until(page, SOUND_SAID.format(text="isn't sound Ixel can send"))
    page.click("#sound-close")
    assert page.is_hidden("#sound-bar") and page.evaluate("document.activeElement.id") == "question"

    # The settings changed after the page said where sound goes: nothing is sent, and it can be sent again
    page.fill("#question", "")
    page.click("#sound-add")
    turned = []
    page.route("**/api/sound?*", lambda route: turned.append(route.request.url) or route.fulfill(
        status=409, content_type="application/json",
        body='{"error": "Sound goes to Groq now, since the settings changed. Nothing was sent: send it again to '
             'have Groq write it out.", "code": "sound_service_changed", "service": "Groq"}'))
    page.set_input_files("#sound-file", files=[{"name": "note.webm", "mimeType": "audio/webm", "buffer": WEBM}])
    wait_until(page, SOUND_SAID.format(text="Nothing was sent"))
    assert turned[0].endswith("/api/sound?expect=openai")
    assert page.is_visible("#sound-retry") and page.is_hidden("#sound-save")  # a file you have: nothing to save
    page.unroute("**/api/sound?*")
    page.click("#sound-retry")
    wait_until(page, "document.querySelector('#question').value === 'What does this error mean?'")

    # A recording that reaches the limit stops by itself, and doesn't take you from where you're typing
    page.fill("#question", "")
    page.click("#sound-record")
    wait_until(page, "!document.querySelector('#sound-stop').disabled")
    assert page.is_visible("#rec-dot")
    page.focus("#material") if page.is_visible("#material") else page.focus("#question")
    typing_in = page.evaluate("document.activeElement.id")
    page.evaluate("() => { const real = performance.now.bind(performance); performance.now = () => real() + 20 * 60 * 1000; }")
    wait_until(page, SOUND_SAID.format(text="stopped at 20 minutes"))
    assert page.evaluate("document.activeElement.id") == typing_in and page.is_hidden("#rec-dot")
    assert page.input_value("#question") == "What does this error mean?"

    # A recording that couldn't be written out is kept: sent again, or saved
    page.evaluate("() => { performance.now = () => document.timeline.currentTime; }")
    page.route("**/api/sound?*", lambda route: route.fulfill(
        status=400, content_type="application/json", body='{"error": "OpenAI couldn\'t write it out: too long."}'))
    page.click("#sound-record")
    wait_until(page, "!document.querySelector('#sound-stop').disabled")
    page.wait_for_timeout(600)
    page.click("#sound-stop")
    wait_until(page, SOUND_SAID.format(text="too long"))
    assert page.is_visible("#sound-retry") and page.evaluate("document.activeElement.id") == "sound-retry"
    with page.expect_download() as saved:
        page.click("#sound-save")
    assert saved.value.suggested_filename == "Ixel recording.webm"
    page.unroute("**/api/sound?*")
    assert not errors, errors
    assert not violations, violations


# A video made in the page (colours changing, and a tone when `sound`), then dropped or picked
MAKE_VIDEO = """([seconds, sound, how]) => new Promise((done) => {
  const canvas = document.createElement("canvas");
  canvas.width = 320; canvas.height = 240;
  const ctx = canvas.getContext("2d");
  const stream = canvas.captureStream(30);
  let audio = null;
  if (sound) {
    audio = new AudioContext();
    const tone = audio.createOscillator();
    const out = audio.createMediaStreamDestination();
    tone.connect(out);
    tone.start();
    stream.addTrack(out.stream.getAudioTracks()[0]);
  }
  const recorder = new MediaRecorder(stream, { mimeType: sound ? "video/webm;codecs=vp8,opus" : "video/webm;codecs=vp8" });
  const chunks = [];
  recorder.ondataavailable = (e) => chunks.push(e.data);
  recorder.onstop = () => {
    if (audio) audio.close();
    const file = new File(chunks, "clip.webm", { type: "video/webm" });
    if (how === "keep") { window.__video = file; done(); return; }
    const data = new DataTransfer();
    data.items.add(file);
    document.querySelector(".composer").dispatchEvent(new DragEvent("drop", { dataTransfer: data, bubbles: true, cancelable: true }));
    done();
  };
  const start = performance.now();
  const draw = () => {
    const t = (performance.now() - start) / 1000;
    ctx.fillStyle = `hsl(${(t * 120) % 360} 80% 50%)`;
    ctx.fillRect(0, 0, 320, 240);
    if (t < seconds) requestAnimationFrame(draw); else recorder.stop();
  };
  recorder.start(200);
  draw();
})"""


def test_a_video_gives_frames_and_its_sound_is_written_out(sound_gui, mic_browser):
    url, service = sound_gui
    page = mic_browser.new_page(viewport={"width": 1100, "height": 900})
    errors, violations = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: violations.append(m.text) if "Content Security Policy" in m.text else None)
    open_app(page, url)

    # Dropped: six frames in the tray, and what's said in it in the question
    page.evaluate(MAKE_VIDEO, [3, True, "drop"])
    wait_until(page, SOUND_SAID.format(text="OpenAI wrote it out"), timeout=30)
    wait_until(page, READY.format(n=6))
    assert "6 frames from it are attached" in page.inner_text("#sound-state")
    assert page.input_value("#question") == "What's said in clip.webm: What does this error mean?"
    [sent] = service.requests
    assert b'filename="sound.wav"' in sent["body"] and b"WAVEfmt " in sent["body"]
    assert len(sent["body"]) < 200_000  # its sound only, made small: not the video

    # Taken from through it, not all from one spot (its colour changes as it goes)
    colours = page.evaluate("""() => [...document.querySelectorAll('#pic-list img')].map((img) => {
      const c = document.createElement('canvas'); c.width = c.height = 1;
      const ctx = c.getContext('2d'); ctx.drawImage(img, 0, 0, 1, 1);
      return [...ctx.getImageData(0, 0, 1, 1).data.slice(0, 3)].join(',');
    })""")
    assert len(colours) == 6 and len(set(colours)) == 6, colours

    # One with no sound: the frames, and the bar says so; room is left for only one more picture
    page.set_input_files("#picture-file", files=[{"name": "a.png", "mimeType": "image/png", "buffer": tiny_png()}])
    wait_until(page, READY.format(n=7))
    page.evaluate(MAKE_VIDEO, [1, False, "drop"])
    wait_until(page, SOUND_SAID.format(text="has no sound to write out"), timeout=30)
    wait_until(page, READY.format(n=8))
    assert "1 frame from it is attached" in page.inner_text("#sound-state")
    assert len(service.requests) == 1

    # A full tray: no frames, but its sound is still written out
    page.fill("#question", "")
    page.evaluate(MAKE_VIDEO, [1, True, "drop"])
    wait_until(page, SOUND_SAID.format(text="No frames were taken"), timeout=30)
    wait_until(page, SOUND_SAID.format(text="OpenAI wrote it out"), timeout=30)
    assert page.locator("#pic-list .pic").count() == 8

    # Picked under Sound: only its sound
    for _ in range(8):
        page.locator(".pic-remove").first.click()
    page.fill("#question", "")
    page.evaluate(MAKE_VIDEO, [1, True, "keep"])
    page.evaluate("""() => {
      const data = new DataTransfer();
      data.items.add(window.__video);
      const input = document.querySelector("#sound-file");
      input.files = data.files;
      input.dispatchEvent(new Event("change"));
    }""")
    wait_until(page, "document.querySelector('#question').value.startsWith(\"What's said in clip.webm\")", timeout=30)
    assert page.locator("#pic-list .pic").count() == 0
    assert not errors, errors
    assert not violations, violations


def tone_wav(seconds=1, rate=16000):
    """A WAV of a tone: 16-bit for a short one, 8-bit at 4 kHz (small) for minutes of it."""
    import math
    import struct
    if seconds > 60:
        rate, width = 4000, 1
        cycle = bytes(int(128 + 60 * math.sin(i * 2 * math.pi / 10)) for i in range(10))  # 400 Hz
        samples = cycle * int(seconds * rate // 10)
    else:
        width = 2
        samples = b"".join(struct.pack("<h", int(8000 * math.sin(i * 2 * math.pi * 440 / rate)))
                           for i in range(int(seconds * rate)))
    return (b"RIFF" + struct.pack("<I", 36 + len(samples)) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * width, width, 8 * width) + b"data" +
            struct.pack("<I", len(samples)) + samples)


def drop_file(page, name, mime, data):
    page.evaluate("""([name, type, bytes]) => {
      const file = new File([new Uint8Array(bytes)], name, { type });
      const data = new DataTransfer();
      data.items.add(file);
      document.querySelector(".composer").dispatchEvent(new DragEvent("drop", { dataTransfer: data, bubbles: true, cancelable: true }));
    }""", [name, mime, list(data)])


def test_a_video_the_browser_cant_show_still_has_its_sound_tried(sound_gui, mic_browser):
    url, service = sound_gui
    page = mic_browser.new_page(viewport={"width": 1100, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    open_app(page, url)

    # Not a video it can read at all: no frames, and its sound is tried and said not to be there
    drop_file(page, "broken.mp4", "video/mp4", b"\0\1 not a video" * 64)
    wait_until(page, SOUND_SAID.format(text="couldn't be taken out either"), timeout=30)
    assert "No frames were taken from broken.mp4: this browser can't play that kind of video." in \
        page.inner_text("#sound-state")
    assert not service.requests

    # Plays, but with no picture it can show (sound saved as a video, or a picture of a kind it can't
    # draw): no frames, and the sound is still written out
    drop_file(page, "talk.mp4", "video/mp4", tone_wav())
    wait_until(page, SOUND_SAID.format(text="OpenAI wrote it out"), timeout=30)
    assert "No frames were taken: it has no picture this browser can show." in page.inner_text("#sound-state")
    assert page.input_value("#question") == "What's said in talk.mp4: What does this error mean?"
    assert page.locator("#pic-list .pic").count() == 0
    assert len(service.requests) == 1
    assert not errors, errors


def pick_video(page, name, data, mime="video/mp4"):
    page.set_input_files("#picture-file", files=[{"name": name, "mimeType": mime, "buffer": data}])


def test_a_long_videos_sound_goes_in_parts(sound_gui, mic_browser):
    url, service = sound_gui
    page = mic_browser.new_page(viewport={"width": 1100, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    open_app(page, url)

    # Ten and a half minutes: two equal parts. The second fails, and sending it again carries on from there,
    # where nothing is said: the first part's words are kept
    service.queue = [(200, {"text": "First part."}), (500, {"error": {"message": "Busy, try later."}}),
                     (200, {"text": ""})]
    pick_video(page, "long.mp4", tone_wav(630))
    wait_until(page, SOUND_SAID.format(text="Busy, try later."), timeout=60)
    assert page.input_value("#question") == "" and page.is_visible("#sound-retry")
    first, second = (len(r["body"]) for r in service.requests)
    assert abs(first - second) < 100 and second > 9_000_000  # equal halves, at 16 kHz
    page.click("#sound-retry")
    wait_until(page, SOUND_SAID.format(text="OpenAI wrote it out"), timeout=60)
    assert page.input_value("#question") == "What's said in long.mp4: First part."
    assert len(service.requests) == 3

    # A little over 20 minutes: only the first 20 are written out
    page.fill("#question", "")
    pick_video(page, "longer.mp4", tone_wav(20 * 60 + 30))
    wait_until(page, SOUND_SAID.format(text="Only its first 20 minutes were written out."), timeout=90)
    assert page.input_value("#question").startswith("What's said in longer.mp4: What does this error mean?")
    assert len(service.requests) == 5

    # More than that: not taken apart at all
    pick_video(page, "longest.mp4", tone_wav(22 * 60))
    wait_until(page, SOUND_SAID.format(text="longest.mp4 is over 20 minutes long"), timeout=60)
    assert len(service.requests) == 5
    assert not errors, errors


RECORD_SOUND_WEBM = """() => new Promise((done) => {
  const audio = new AudioContext();
  const tone = audio.createOscillator();
  const out = audio.createMediaStreamDestination();
  tone.connect(out);
  tone.start();
  const recorder = new MediaRecorder(out.stream, { mimeType: "audio/webm;codecs=opus" });
  const chunks = [];
  recorder.ondataavailable = (e) => chunks.push(e.data);
  recorder.onstop = () => {
    audio.close();
    // As the browser hands over a .webm picked from disk: called a video
    const file = new File(chunks, "note.webm", { type: "video/webm" });
    const data = new DataTransfer();
    data.items.add(file);
    document.querySelector(".composer").dispatchEvent(new DragEvent("drop", { dataTransfer: data, bubbles: true, cancelable: true }));
    done();
  };
  recorder.start(200);
  setTimeout(() => recorder.stop(), 1000);
})"""


def test_a_webm_with_only_sound_is_sent_as_it_is(sound_gui, mic_browser):
    url, service = sound_gui
    page = mic_browser.new_page(viewport={"width": 1100, "height": 900})
    open_app(page, url)
    page.evaluate(RECORD_SOUND_WEBM)
    wait_until(page, SOUND_SAID.format(text="OpenAI wrote it out"), timeout=30)
    assert "frame" not in page.inner_text("#sound-state")
    assert page.input_value("#question") == "What does this error mean?"
    [sent] = service.requests
    assert b"\x1a\x45\xdf\xa3" in sent["body"] and b"WAVEfmt " not in sent["body"]
    assert page.locator("#pic-list .pic").count() == 0


def _ebml(element_id, payload, unknown=False):
    """One EBML element (what WebM is written in): its ID, its size (8 bytes; all ones: unknown), its data."""
    size = b"\x01\xff\xff\xff\xff\xff\xff\xff" if unknown else b"\x01" + len(payload).to_bytes(7, "big")
    return element_id + size + payload


def _webm(*tracks, before_tracks=b"", inside_tracks=b"", unknown_segment=False):
    """A WebM's start: its header, then a Segment with Info, Tracks (each (type, codec)) and a Cluster."""
    entries = [_ebml(b"\xae", _ebml(b"\x83", bytes([kind])) + _ebml(b"\x86", codec)) for kind, codec in tracks]
    listed = entries[0] + inside_tracks + b"".join(entries[1:]) if entries else inside_tracks
    segment = (before_tracks + _ebml(b"\x15\x49\xa9\x66", b"") + _ebml(b"\x16\x54\xae\x6b", listed)
               + _ebml(b"\x1f\x43\xb6\x75", b"\0" * 64))
    return _ebml(b"\x1a\x45\xdf\xa3", _ebml(b"\x42\x82", b"webm")) + _ebml(b"\x18\x53\x80\x67", segment, unknown_segment)


SOUND, PICTURE = (2, b"A_OPUS"), (1, b"V_VP8")


def test_only_a_webm_with_nothing_but_sound_is_sent_as_it_is(gui_url, browser):
    """The rest are videos, whose frames and sound are taken out in the page: a video never goes whole."""
    page = browser.new_page()
    open_app(page, gui_url)
    check = """async (bytes) => (await import('/video.js')).soundOnly(
      new File([new Uint8Array(bytes)], 'x.webm', { type: 'video/webm' }))"""
    void = _ebml(b"\xec", b"\0" * (1024 * 1024 + 100))  # (a legal filler) pushes the rest past the first megabyte
    cases = {
        "sound": (_webm(SOUND), True),
        "a recording, its length unknown": (_webm(SOUND, unknown_segment=True), True),
        "sound and subtitles": (_webm(SOUND, (0x11, b"S_TEXT/UTF8")), True),
        "sound and a picture": (_webm(SOUND, PICTURE), False),
        "a picture first": (_webm(PICTURE, SOUND), False),
        "a picture hidden past the first megabyte": (_webm(SOUND, PICTURE, inside_tracks=void), False),
        "tracks past the first megabyte": (_webm(SOUND, before_tracks=void), False),
        "no tracks": (_webm(), False),
        "a track of no kind": (_webm(SOUND, (0, b"")), False),
        "not WebM": (b"RIFF" + bytes(60), False),
    }
    for name, (data, sound_only) in cases.items():
        assert page.evaluate(check, list(data)) is sound_only, name
    page.close()


def test_a_video_has_one_frame_taken_per_moment(gui_url, browser):
    page = browser.new_page()
    open_app(page, gui_url)
    times = page.evaluate("""async () => { const v = await import('/video.js');
      return [v.frameTimes(0, 6), v.frameTimes(0.08, 6), v.frameTimes(60, 6)]; }""")
    assert times[0] == [0]
    assert len(times[1]) == 2 and times[1][1] >= 0.04  # a two-frame video: both frames, each once
    assert times[2] == [5, 15, 25, 35, 45, 55]
    # Two at once: the second waits for the first, rather than both taking memory together
    both = page.evaluate("""async (bytes) => { const v = await import('/video.js');
      const file = new File([new Uint8Array(bytes)], 'a.wav', { type: 'audio/wav' });
      const found = await Promise.all([v.soundOf(file, 1), v.soundOf(file, 1)]);
      return found.map((f) => f && f.pieces.length); }""", list(tone_wav()))
    assert both == [1, 1]
    page.close()


def test_without_a_sound_service_the_bar_says_what_to_add(gui_url, browser):
    page = browser.new_page(viewport={"width": 390, "height": 844})
    open_app(page, gui_url)
    page.click("#sound-add")
    assert "needs an OpenAI or Groq key" in page.inner_text("#sound-state")
    assert page.is_disabled("#sound-pick")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390

    # A video still gives its frames, and the bar says its sound wasn't written out and what that needs
    page.evaluate(MAKE_VIDEO, [1, True, "drop"])
    wait_until(page, SOUND_SAID.format(text="Its sound wasn't written out."), timeout=30)
    wait_until(page, READY.format(n=6))
    said = page.inner_text("#sound-state")
    assert said.startswith("6 frames from it are attached. Its sound wasn't written out.") and "OpenAI or Groq key" in said

    page.click("#sound-state .linkish")
    wait_until(page, "!document.querySelector('#view-settings').hidden")
    wait_until(page, "document.activeElement.classList.contains('page-title')")
    page.close()



def test_settings_private_has_only_models_on_your_computers_answer(tmp_path, browser):
    home = tmp_path / "home"
    path = home / ".config" / "ixel-mat" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(SETTINGS_TOML.replace("[review]", '[agents.llama]\ntype = "http"\n'
                                          'url = "http://127.0.0.1:9/v1/chat/completions"\nmodel = "llama3.2"\n'
                                          'label = "Llama"\n\n[review]'), encoding="utf-8")
    with launch_gui(home, {"IXEL_TEST_PANEL_KEY": "sk-test"}) as url:
        page, errors, violations = open_settings(browser, url)
        group = page.locator(".set-group:has(#set-h-review)")
        # The panel's models with a key (taken for gateways to a company) would sit out
        assert f"Would sit out: GPT-5, {HOSTILE}, Gemini." in group.inner_text()
        setting(page, "review:private").check()
        wait_for_file(path, "private = true")
        page.wait_for_selector("[data-note=review].ok")
        assert f"Sitting out: GPT-5, {HOSTILE}, Gemini." in group.inner_text()

        # Ask shows it, and lists only the model that answers
        page.click(".rail-item[data-view=ask]")
        page.wait_for_selector("#panel .model.private")
        assert page.locator("#panel .model.private").inner_text().replace("\n", " ").split() == [
            "Private", "3", "sit", "out"]
        assert page.locator("#panel .model:not(.private) .name").all_inner_texts() == ["Llama"]
        assert injected(page) == CLEAN
        assert not errors and not violations, (errors, violations)
        shots = os.environ.get("IXEL_SCREENSHOT_DIR")
        if shots:
            page.locator("#panel").screenshot(path=os.path.join(shots, "ixel-ask-private.png"))


def test_a_question_private_turns_away_doesnt_say_the_models_are_starting(tmp_path, browser):
    home = tmp_path / "home"
    path = home / ".config" / "ixel-mat" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(SETTINGS_TOML.replace("[review]\n", "[review]\nprivate = true\n"), encoding="utf-8")
    with launch_gui(home, {"IXEL_TEST_PANEL_KEY": "sk-test"}) as url:
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        open_app(page, url)
        page.fill("#question", "What is 2 + 2?")
        page.click("#ask")
        page.wait_for_selector(f"{LAST} .notice.error")
        assert "Private is on" in page.inner_text(f"{LAST} .notice.error")
        wait_until(page, f"!document.querySelector({LAST!r}).classList.contains('running')")
        assert "Starting the models" not in page.inner_text(LAST) and "No model started." in page.inner_text(LAST)
        assert not errors, errors


def test_settings_gets_a_model_through_an_ollama_of_yours(tmp_path, browser):
    home = tmp_path / "home"
    path = home / ".config" / "ixel-mat" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(SETTINGS_TOML, encoding="utf-8")
    with ThreadedFakeProvider() as fake:
        fake.ollama, fake.models = True, ["llama3.2"]
        base = f"http://127.0.0.1:{fake.port}/v1"
        with launch_gui(home, {"IXEL_TEST_PANEL_KEY": "sk-test"}) as url:
            page, errors, violations = open_settings(browser, url)
            setting(page, "servers:address").fill(f"127.0.0.1:{fake.port}")
            setting(page, "servers:address").press("Enter")
            page.wait_for_selector("#settings-body .set-get")
            group = page.locator(".set-group:has(#set-h-servers)")
            assert "Ollama on this computer" in group.inner_text()

            setting(page, f"servers:get:{base}").fill("qwen3:8b")
            # The fake's progress line shows for a moment only: note every text the status line is given
            page.evaluate("""() => { window.__said = []; new MutationObserver(() => {
                for (const s of document.querySelectorAll('.set-get small')) if (s.checkVisibility()) window.__said.push(s.textContent);
            }).observe(document.getElementById('settings-body'), {subtree: true, childList: true,
                                                                characterData: true}); }""")
            setting(page, f"servers:get-go:{base}").click()
            wait_until(page, "window.__said.some(t => t.includes('50% of 5.2 GB'))")
            page.wait_for_selector("[data-note=servers].ok")
            assert "qwen3:8b is on this computer now" in page.inner_text("[data-note=servers]")
            page.wait_for_selector(f'[data-key="servers:add:{base}:qwen3:8b"]')
            assert fake.pulls == [{"model": "qwen3:8b", "name": "qwen3:8b", "stream": True}]
            assert "[agents." + "qwen3_8b]" not in path.read_text(encoding="utf-8")  # got, not added
            assert not errors and not violations, (errors, violations)
            shots = os.environ.get("IXEL_SCREENSHOT_DIR")
            if shots:
                group.screenshot(path=os.path.join(shots, "ixel-settings-get-model.png"))
