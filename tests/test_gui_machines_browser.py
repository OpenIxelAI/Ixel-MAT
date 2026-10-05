"""The Machines page in a real browser, against a real sshd: add a machine, check and pin its key, Connect
(with no terminal here, it gives the line to run), and run a command on it. Skipped without Playwright or
sshd."""
import json
import os

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from sshd_lab import sshd  # noqa: E402,F401 (a fixture)
from test_gui_browser import browser, launch_gui  # noqa: E402,F401 (a fixture)


def open_machines(browser, url, width=1100, height=900):
    page = browser.new_page(viewport={"width": width, "height": height})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if "Content Security Policy" in m.text else None)
    page.goto(url)
    page.click(".rail-item[data-view=machines]")
    page.wait_for_selector("#machines-body .machines-empty, #machines-body .machine")
    return page, errors


def shot(page, name, **options):
    shots = os.environ.get("IXEL_SCREENSHOT_DIR")
    if shots:
        page.screenshot(path=os.path.join(shots, name), **options)


def test_add_a_machine_pin_its_key_and_run_a_command(sshd, tmp_path, browser):
    home = tmp_path / "home"
    home.mkdir()
    with launch_gui(home, {"PATH": os.environ["PATH"]}) as url:
        page, errors = open_machines(browser, url)
        assert "Add a machine" in page.inner_text(".machines-empty")
        assert not page.is_visible("#machines-import") and not page.is_visible("#machines-run-open")
        shot(page, "ixel-machines-empty.png")

        page.click("#machines-add")
        page.fill("#m-name", "Lab")
        page.fill("#m-host", "-oProxyCommand=touch /tmp/pwned")
        page.click("#machines-dialog button[type=submit]")
        page.wait_for_selector("#machines-dialog .dialog-problem .board-notice.error")
        assert "isn't a name or address" in page.inner_text("#machines-dialog .dialog-problem")
        page.fill("#m-host", "127.0.0.1")
        page.fill("#m-port", str(sshd.port))
        page.fill("#m-user", sshd.user)
        page.fill("#m-key", str(sshd.client))
        page.fill("#m-group", "Home lab")
        shot(page, "ixel-machines-add.png")
        page.click("#machines-dialog button[type=submit]")
        page.wait_for_selector(".machine .machine-name:has-text('Lab')")
        assert page.inner_text(".machine .pill") == "Key not checked"
        assert f"{sshd.user}@127.0.0.1:{sshd.port}" in page.inner_text(".machine .machine-sub")

        page.click(".machine button:has-text('Check key')")
        page.wait_for_selector("#machines-dialog .fingerprint")
        assert page.inner_text("#machines-dialog .fingerprint code") == sshd.fingerprint
        assert "Is this Lab?" in page.inner_text("#machines-dialog h2")
        shot(page, "ixel-machines-trust.png")
        page.click("#machines-dialog button[type=submit]")
        page.wait_for_selector(".machine button:has-text('Connect')")
        pins = (home / ".config" / "ixel-mat" / "machines_known_hosts").read_text()
        assert pins.startswith(f"{sshd.name} ssh-ed25519 ")

        page.click(".machine button:has-text('Connect')")  # no terminal on this computer: the line to run
        page.wait_for_selector("#machines-dialog .check-fix code")
        assert page.inner_text("#machines-dialog .check-fix code") == "ixel machines connect Lab"
        page.click("#machines-dialog button[type=submit]")

        page.click("#machines-run-open")
        page.fill("#run-command", "echo hello from $(whoami); echo second line")
        assert page.locator(".run-pick input").is_checked()
        assert page.inner_text(".run-form button[type=submit]").strip() == "Run on 1 machine"
        page.click(".run-form button[type=submit]")
        page.wait_for_selector(".run-item.s-ok", timeout=30_000)
        assert page.inner_text(".run-item .run-preview") == "second line"
        page.click(".run-item .run-row")
        page.wait_for_selector(".run-output pre")
        assert page.inner_text(".run-output pre").strip() == f"hello from {sshd.user}\nsecond line"
        assert "1 done" in page.inner_text(".run-head")
        shot(page, "ixel-machines-run.png", full_page=True)

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(200)
        assert page.evaluate("document.documentElement.scrollWidth") <= 390
        shot(page, "ixel-machines-phone.png", full_page=True)
        assert not errors, errors

    log = (home / ".config" / "ixel-mat" / "machines.log").read_text()
    for action in ("HOSTKEY_PINNED", "RUN_START", "RUN "):
        assert action in log


def test_a_run_survives_a_reload_and_typing_while_it_goes(sshd, tmp_path, browser):
    home = tmp_path / "home"
    (home / ".config" / "ixel-mat").mkdir(parents=True)
    (home / ".config" / "ixel-mat" / "machines.json").write_text(json.dumps(
        {"version": 1, "machines": [{"id": "a1", **sshd.machine(name="Lab")}]}))
    (home / ".config" / "ixel-mat" / "machines_known_hosts").write_text(
        "".join(f"{sshd.name} {kind} {b64}\n" for kind, b64 in sshd.host_keys()))
    with launch_gui(home, {"PATH": os.environ["PATH"]}) as url:
        page, errors = open_machines(browser, url)
        page.click("#machines-run-open")
        page.fill("#run-command", "for i in 1 2 3 4 5 6 7 8 9 10 11 12; do echo line $i; sleep 0.5; done")
        page.click(".run-form button[type=submit]")
        page.wait_for_selector(".run-item.s-running")
        page.click(".run-item .run-row")  # its output, open while it grows
        # Typing while the page polls: nothing is lost, and the box keeps the focus
        page.fill("#run-command", "")
        page.type("#run-command", "uptime -p", delay=150)
        assert page.input_value("#run-command") == "uptime -p"
        assert page.evaluate("document.activeElement.id") == "run-command"
        # A reload in the middle brings the run back, with Stop
        page.reload()
        page.click(".rail-item[data-view=machines]")
        page.wait_for_selector(".run-results button:has-text('Stop')")
        assert "Running on 1 machine" in page.inner_text(".run-head")
        page.wait_for_selector(".run-item.s-ok", timeout=30_000)
        assert not page.is_visible(".run-results button:has-text('Stop')")
        # A pinned machine's key can be checked again from its settings
        page.click(".machine button:has-text('Edit')")
        page.click("#machines-dialog button:has-text('Check it again')")
        page.wait_for_selector(".toast:has-text('key matches')")
        assert not errors, errors


def test_health_has_a_machines_group_once_there_are_machines(sshd, tmp_path, browser):
    home = tmp_path / "home"
    (home / ".config" / "ixel-mat").mkdir(parents=True)
    (home / ".config" / "ixel-mat" / "machines.json").write_text(
        '{"version": 1, "machines": [{"id": "a1", "name": "Lab", "host": "127.0.0.1"}]}')
    with launch_gui(home, {}) as url:
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        page.goto(url)
        page.click(".rail-item[data-view=health]")
        page.wait_for_selector(".health-group[aria-label=Machines]")
        text = page.inner_text(".health-group[aria-label=Machines]")
        assert "1 machine" in text and "Pinned keys" in text


def test_import_from_ssh_config_and_ixel_console_on_a_phone(tmp_path, browser):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_text("Host web db\n  User deploy\nHost *.corp\n")
    console = home / ".config" / "ixel-console"
    console.mkdir(parents=True)
    (console / "profiles.json").write_text(json.dumps([
        {"name": "Mac mini", "host": "mini.local", "agent": "openclaw", "remote_command": "openclaw tui"},
        {"name": "Gateway", "connection_type": "websocket"}]))
    with launch_gui(home, {}) as url:
        page, errors = open_machines(browser, url, 390, 844)
        page.click(".machines-empty button:has-text('~/.ssh/config')")
        page.wait_for_selector(".machine .machine-name:has-text('db')")
        assert page.locator(".machine").count() == 2 and "Added 2 machines" in page.inner_text("#machines-dialog")
        page.click("#machines-dialog button[type=submit]")
        page.click("#machines-import")
        page.click("#machines-dialog .import-option[value=console]")
        page.wait_for_selector(".machine .machine-name:has-text('Mac mini')")
        assert "runs openclaw tui" in page.inner_text(".machine:has-text('Mac mini')")
        said = page.inner_text("#machines-dialog")
        assert "1 gateway chat profile stayed behind" in said and "ixel-console uninstall" in said
        page.click("#machines-dialog button[type=submit]")
        assert not page.is_visible("#machines-import")  # nothing left to bring in
        assert page.evaluate("document.documentElement.scrollWidth") <= 390
        shot(page, "ixel-machines-imported-phone.png", full_page=True)
        assert not errors, errors
