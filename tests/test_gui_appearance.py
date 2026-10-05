"""Settings > Appearance: System, Light or Dark, saved for every window and applied before the page is drawn."""
import json
import re
from pathlib import Path

import pytest

from ixel_mat.gui import appearance
from test_gui_server import AUTH, JSON_AUTH, make_gui, run_with_client

STATIC = Path(appearance.__file__).parent / "static"


@pytest.fixture
def app_file(tmp_path, monkeypatch):
    path = tmp_path / ".config" / "ixel-mat" / "app.json"
    monkeypatch.setattr(appearance, "APP_FILE", path)
    return path


# ── The saved choice ──────────────────────────────────────────────────────────

def test_system_until_something_else_is_picked(app_file):
    assert appearance.load() == "system"
    assert appearance.save("light") == "light"
    assert appearance.load() == "light"
    assert json.loads(app_file.read_text(encoding="utf-8")) == {"appearance": "light"}


def test_a_broken_or_hand_edited_file_means_system(app_file):
    app_file.parent.mkdir(parents=True)
    for text in ("{not json", '["dark"]', '{"appearance": "purple"}', '{"appearance": 1}'):
        app_file.write_text(text, encoding="utf-8")
        assert appearance.load() == "system", text
    app_file.write_bytes('﻿{"appearance": "dark"}'.encode("utf-8"))  # Notepad's BOM
    assert appearance.load() == "dark"


def test_only_the_three_choices_are_saved_and_other_settings_in_the_file_stay(app_file):
    app_file.parent.mkdir(parents=True)
    app_file.write_text('{"later": true}', encoding="utf-8")
    for bad in ("purple", "", None, 1, ["dark"]):
        with pytest.raises(ValueError):
            appearance.save(bad)
    appearance.save("dark")
    assert json.loads(app_file.read_text(encoding="utf-8")) == {"later": True, "appearance": "dark"}


def test_the_choice_goes_on_the_page_as_it_is_served():
    page = (STATIC / "index.html").read_bytes()
    assert b'<html lang="en" data-appearance="dark">' in appearance.into_page(page, "dark")
    assert b'data-appearance="system"' in appearance.into_page(page, '"><script>')  # never anything else


# ── The server ────────────────────────────────────────────────────────────────

def test_page_opens_in_the_saved_colors_and_settings_change_them(app_file):
    appearance.save("light")

    async def scenario(client):
        first = await client.get("/")
        asked = await client.get("/api/appearance", headers=AUTH)
        changed = await client.post("/api/appearance", headers=JSON_AUTH, json={"appearance": "dark"})
        again = await client.get("/")
        refused = [await client.post("/api/appearance", headers=JSON_AUTH, json=body)
                   for body in ({"appearance": "sepia"}, {}, ["dark"])]
        no_key = await client.post("/api/appearance", headers={"Content-Type": "application/json"},
                                   json={"appearance": "light"})
        form = await client.post("/api/appearance", headers=AUTH, data="appearance=light")
        return (await first.text(), await asked.json(), changed.status, await changed.json(), await again.text(),
                [r.status for r in refused], no_key.status, form.status)

    first, asked, status, reply, again, refused, no_key, form = run_with_client(make_gui()[0], scenario)
    assert '<html lang="en" data-appearance="light">' in first
    assert asked == {"appearance": "light"}
    assert (status, reply) == (200, {"appearance": "dark"})
    assert '<html lang="en" data-appearance="dark">' in again
    assert refused == [400, 400, 400]
    assert (no_key, form) == (401, 415)
    assert appearance.load() == "dark"


def test_theme_js_runs_before_the_page_is_drawn():
    """A plain script in <head>, ahead of the app's module: a module runs only after the page is drawn."""
    head = (STATIC / "index.html").read_text(encoding="utf-8").split("</head>")[0]
    assert '<script src="/theme.js"></script>' in head
    assert head.index('href="/style.css"') < head.index("/theme.js") < head.index('src="/app.js"')
    assert "import " not in (STATIC / "theme.js").read_text(encoding="utf-8")


def test_light_colors_are_their_own_not_the_dark_ones_inverted():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ':root[data-theme="light"] {' in css and "invert(" not in css
    assert "prefers-color-scheme" not in css  # theme.js decides, so a choice of Light or Dark always wins
    dark = css[css.index(":root {"):css.index(':root[data-theme="light"] {')]
    light = css[css.index(':root[data-theme="light"] {'):]
    light = light[:light.index("\n}\n")]
    colors = lambda block: set(re.findall(r"(--[\w-]+):\s*(?:#|rgb\()", block))
    assert colors(dark) - colors(light) == {"--accent-ink"}  # only sits on the gold, which both themes share


# ── In a browser ──────────────────────────────────────────────────────────────

def test_appearance_in_the_browser(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import expect
    from test_gui_browser import launch_gui

    bg = "getComputedStyle(document.body).backgroundColor"
    theme = "document.documentElement.dataset.theme"
    told_mac = "window.__told"  # what the Mac app's window would have been told
    fake_mac = "window.webkit = {messageHandlers: {ixelAppearance: {postMessage: (m) => (window.__told = window.__told || []).push(m)}}}"
    def saved(response):
        return response.url.endswith("/api/appearance") and response.request.method == "POST" and response.ok

    with launch_gui(tmp_path, {"OPENAI_API_KEY": "", "GROQ_API_KEY": ""}) as url:
        with playwright.sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:  # noqa: BLE001
                pytest.skip(f"Chromium not available: {exc}")
            context = browser.new_context(color_scheme="dark")
            context.add_init_script(fake_mac)
            page = context.new_page()
            problems = []
            page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: problems.append(str(e)))
            page.goto(url)
            page.wait_for_selector(".rail-item")

            # System: follows the computer, and switches while the window is open
            assert page.evaluate(theme) == "dark" and page.evaluate(bg) == "rgb(14, 15, 17)"
            page.emulate_media(color_scheme="light")
            expect(page.locator("html")).to_have_attribute("data-theme", "light")
            assert page.evaluate(bg) == "rgb(255, 255, 255)"
            assert page.get_attribute('meta[name="theme-color"]', "content") == "#ffffff"
            page.emulate_media(color_scheme="dark")
            expect(page.locator("html")).to_have_attribute("data-theme", "dark")

            # Picking Light in Settings switches at once, whatever the computer is in, and is kept
            page.click(".rail-item[data-view=settings]")
            assert page.locator('input[name="appearance"]:checked').get_attribute("value") == "system"
            other = context.new_page()  # a second window, open meanwhile
            other.goto(url)
            other.wait_for_selector(".rail-item")
            with page.expect_response(saved):
                page.click("label.look:has-text('Light')")
                assert page.evaluate(theme) == "light"  # before it's saved
            assert json.loads((tmp_path / ".config" / "ixel-mat" / "app.json").read_text()) == {"appearance": "light"}
            assert page.evaluate(told_mac) == ["system", "light"]

            # The other window takes it when it comes back to the front
            assert other.evaluate(theme) == "dark"
            other.evaluate("window.dispatchEvent(new Event('focus'))")
            expect(other.locator("html")).to_have_attribute("data-theme", "light")

            # Opened again: light from the start, before any script of the app's has run
            page.reload()
            assert page.evaluate("document.documentElement.dataset.appearance") == "light"
            assert page.evaluate(theme) == "light" and page.evaluate(told_mac) == ["light"]
            page.click(".rail-item[data-view=settings]")
            assert page.locator('input[name="appearance"]:checked').get_attribute("value") == "light"

            # Keyboard: arrow keys move between the three, like any set of choices
            page.focus('input[name="appearance"][value="light"]')
            with page.expect_response(saved):
                page.keyboard.press("ArrowRight")
            assert page.evaluate(theme) == "dark"
            page.keyboard.press("ArrowLeft")
            with page.expect_response(saved):
                page.keyboard.press("ArrowLeft")
            assert page.locator('input[name="appearance"]:checked').get_attribute("value") == "system"
            assert appearance.load(tmp_path / ".config" / "ixel-mat" / "app.json") == "system"
            assert not problems, problems
            browser.close()
