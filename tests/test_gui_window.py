"""`ixel app`: finding a browser with an app mode, opening the window, and stopping once it's closed."""
import asyncio
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import aiohttp
import pytest

from ixel_mat.gui import window
from ixel_mat.gui.server import Presence, serve_window, until_closed

WIN_ENV = {"ProgramFiles(x86)": r"C:\Program Files (x86)", "ProgramFiles": r"C:\Program Files",
           "LOCALAPPDATA": r"C:\Users\Ann\AppData\Local"}


# ── Finding a browser ─────────────────────────────────────────────────────────

def test_windows_prefers_edge_then_chrome_and_asks_the_registry_first():
    registered = {"msedge.exe": [r"D:\Edge\msedge.exe"], "chrome.exe": []}
    found = window.browser_candidates("win32", WIN_ENV, Path("C:/Users/Ann"), registered=registered.get)
    assert found[0] == r"D:\Edge\msedge.exe"
    edge = [i for i, p in enumerate(found) if p.endswith("msedge.exe")]
    chrome = [i for i, p in enumerate(found) if p.endswith("chrome.exe")]
    assert edge and chrome and max(edge) < min(chrome)
    assert str(Path(r"C:\Program Files (x86)", "Microsoft", "Edge", "Application", "msedge.exe")) in found


def test_a_chosen_browser_comes_first_and_nothing_is_listed_twice():
    env = {**WIN_ENV, "IXEL_APP_BROWSER": r"E:\Brave\brave.exe"}
    registered = {"msedge.exe": [str(Path(r"C:\Program Files", "Microsoft", "Edge", "Application", "msedge.exe"))],
                  "chrome.exe": []}
    found = window.browser_candidates("win32", env, Path("C:/Users/Ann"), registered=registered.get)
    assert found[0] == r"E:\Brave\brave.exe" and len(found) == len(set(found))


def test_mac_looks_in_applications_folders():
    found = window.browser_candidates("darwin", {}, Path("/Users/ann"))
    # str(Path(...)): the Mac's paths, written with \ when the tests run on Windows
    assert found[0] == str(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    assert str(Path("/Users/ann/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")) in found


def test_linux_looks_on_path_only():
    on_path = {"chromium": "/usr/bin/chromium", "google-chrome": "/opt/google/chrome/google-chrome"}.get
    found = window.browser_candidates("linux", {}, Path("/home/ann"), on_path=on_path)
    assert found == ["/opt/google/chrome/google-chrome", "/usr/bin/chromium"]


def test_find_app_browser_takes_the_first_that_exists():
    present = {str(Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"))}
    assert window.find_app_browser("darwin", {}, Path("/Users/ann"), exists=present.__contains__) == next(iter(present))
    assert window.find_app_browser("darwin", {}, Path("/Users/ann"), exists=lambda p: False) is None


def test_the_window_keeps_its_own_profile_outside_the_config_folder():
    assert window.data_dir("win32", WIN_ENV) == Path(WIN_ENV["LOCALAPPDATA"], "IxelMAT")
    assert window.data_dir("darwin", {}, Path("/Users/ann")) == Path("/Users/ann/Library/Application Support/IxelMAT")
    assert window.data_dir("linux", {}, Path("/home/ann")) == Path("/home/ann/.local/state/ixel-mat")
    assert window.data_dir("linux", {"XDG_STATE_HOME": "/x"}, Path("/home/ann")) == Path("/x/ixel-mat")


def test_window_command_is_an_app_window_with_a_private_profile():
    cmd = window.window_command("msedge", "file:///tmp/ixel-gui-1.html", Path("/p/window"))
    assert cmd[:3] == ["msedge", "--app=file:///tmp/ixel-gui-1.html", f"--user-data-dir={Path('/p/window')}"]
    assert "--no-first-run" in cmd and "--no-default-browser-check" in cmd
    assert "--disable-sync" in cmd  # Edge would sign the profile in to your Microsoft account and sync it


def test_the_window_turns_off_the_browsers_background_services():
    cmd = window.window_command("msedge", "file:///tmp/ixel-gui-1.html", Path("/p/window"), "win32")
    for switch in ("--disable-background-networking", "--disable-component-update", "--disable-domain-reliability",
                   "--disable-breakpad", "--metrics-recording-only", "--no-pings", "--disable-default-apps",
                   "--disable-component-extensions-with-background-pages", "--disable-field-trial-config"):
        assert switch in cmd
    # One --disable-features: a browser reads only the last one it's given
    features = [arg for arg in cmd if arg.startswith("--disable-features=")]
    assert len(features) == 1
    off = features[0].split("=", 1)[1].split(",")
    assert {"AutofillServerCommunication", "PreconnectToSearch", "AimServerRequestOnStartupEnabled",
            "NetworkTimeServiceQuerying", "OptimizationHints", "Translate"} <= set(off)
    assert all(name.isalnum() for name in off)  # no spaces or stray commas, which would end the list early
    assert cmd[-1] == f"--window-size={window.WINDOW_SIZE}"
    assert "--class=Ixel" not in cmd  # Linux only
    assert window.window_command("chrome", "u", Path("/p"), "linux")[-1] == "--class=Ixel"


# ── Opening it ────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_browser(tmp_path):
    """A 'browser' that records its arguments and exits with the code (or sleeps) it's told to."""
    if os.name != "posix":
        pytest.skip("a shell-script browser")
    log = tmp_path / "argv.txt"

    def make(behaviour: str) -> str:
        script = tmp_path / f"browser-{behaviour.replace(' ', '-')}"
        script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{log}"\n{behaviour}\n')
        script.chmod(0o755)
        return str(script)
    make.log = log
    return make


def test_open_window_runs_the_browser_in_app_mode(fake_browser, tmp_path):
    fallback = []
    where = asyncio.run(window.open_window("file:///x.html", fake_browser("exit 0"), tmp_path / "profile",
                                           fallback=fallback.append))
    assert where == "its own window" and not fallback
    assert fake_browser.log.read_text().splitlines()[0] == "--app=file:///x.html"
    assert (tmp_path / "profile").is_dir()


def test_a_browser_that_keeps_running_is_a_window(fake_browser, tmp_path, monkeypatch):
    monkeypatch.setattr(window, "QUICK_FAILURE_SECONDS", 0.2)
    fallback = []
    where = asyncio.run(window.open_window("file:///x.html", fake_browser("sleep 3"), tmp_path / "p",
                                           fallback=fallback.append))
    assert where == "its own window" and not fallback


def test_a_browser_that_fails_at_once_falls_back_to_a_tab(fake_browser, tmp_path):
    fallback = []
    where = asyncio.run(window.open_window("file:///x.html", fake_browser("exit 3"), tmp_path / "p",
                                           fallback=lambda url: fallback.append(url) or True))
    assert where == "your browser" and fallback == ["file:///x.html"]
    missing = []
    where = asyncio.run(window.open_window("file:///y.html", str(tmp_path / "no-such-browser"), tmp_path / "p",
                                           fallback=lambda url: missing.append(url) or True))
    assert where == "your browser" and missing == ["file:///y.html"]


def test_no_browser_at_all_is_not_called_open(fake_browser, tmp_path):
    where = asyncio.run(window.open_window("file:///x.html", fake_browser("exit 3"), tmp_path / "p",
                                           fallback=lambda url: False))
    assert where == ""


# ── Stopping once the window is closed ────────────────────────────────────────

def test_presence_counts_open_pages_and_time_since_the_last():
    now = [100.0]
    presence = Presence(clock=lambda: now[0])
    now[0] = 103.0
    assert presence.empty_for() == 3.0 and not presence.seen
    presence.arrive()
    presence.arrive()
    presence.leave()
    now[0] = 110.0
    assert presence.empty_for() == 0.0  # one still open
    presence.leave()
    now[0] = 112.5
    assert presence.empty_for() == 2.5 and presence.seen


def test_until_closed_waits_out_a_reload_and_gives_up_on_no_page():
    async def go():
        presence = Presence()
        # Nobody ever comes: it stops after first_wait
        await asyncio.wait_for(until_closed(presence, first_wait=0.1, grace=5, tick=0.01), 2)

        presence = Presence()
        presence.arrive()
        waiting = asyncio.create_task(until_closed(presence, first_wait=5, grace=0.3, tick=0.01))
        presence.leave()          # a reload...
        await asyncio.sleep(0.1)
        presence.arrive()         # ...comes back within the grace period
        await asyncio.sleep(0.4)
        assert not waiting.done()
        presence.leave()          # closed for good
        await asyncio.wait_for(waiting, 2)
    asyncio.run(go())


def _launch_target(launch_uri: str) -> tuple[str, str]:
    page = Path(url2pathname(urlparse(launch_uri).path)).read_text(encoding="utf-8")
    url = re.search(r'content="0;url=([^"]+)"', page).group(1)
    base, token = url.split("/#token=")
    return base, token


def test_serve_window_stops_when_the_window_closes():
    events = []

    async def go():
        held = asyncio.Event()

        async def open_window(launch_uri):
            base, token = _launch_target(launch_uri)
            assert token not in launch_uri  # the key isn't on the browser's command line
            events.append(launch_uri)

            async def page():
                headers = {"Authorization": f"Bearer {token}"}
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{base}/api/presence", headers=headers) as resp:
                        assert resp.status == 200
                        held.set()
                        await asyncio.sleep(0.6)  # the window is open for a while, then closed
            events.append(asyncio.create_task(page()))
            return "its own window"

        serving = asyncio.create_task(serve_window(open_window, announce=events.append, first_wait=5, grace=0.2))
        await asyncio.wait_for(held.wait(), 5)
        launch = Path(url2pathname(urlparse(events[0]).path))
        await asyncio.sleep(0.4)
        assert not serving.done() and not launch.exists()  # the key's file is gone once the page has it
        return await asyncio.wait_for(serving, 5)

    assert asyncio.run(go()) is True
    assert events[2] == "its own window"


def test_a_launch_file_windows_wont_delete_yet_doesnt_stop_the_app(monkeypatch):
    """A virus scanner can hold the file open: the app goes on, and deletes it at exit."""
    from pathlib import Path as RealPath
    refusals = []

    def busy(self, missing_ok=False):
        refusals.append(self)
        raise PermissionError("in use")
    monkeypatch.setattr(RealPath, "unlink", busy)

    async def go():
        async def open_window(launch_uri):
            base, token = _launch_target(launch_uri)

            async def page():
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{base}/api/presence", headers={"Authorization": f"Bearer {token}"}):
                        await asyncio.sleep(0.5)
            asyncio.get_running_loop().create_task(page())
            return "its own window"
        return await asyncio.wait_for(serve_window(open_window, announce=lambda w: None, first_wait=5, grace=0.2), 10)
    assert asyncio.run(go()) is True and len(refusals) == 2  # once for the page, once at exit
    monkeypatch.undo()
    refusals[0].unlink()


@pytest.mark.parametrize("where", ["its own window", ""])
def test_serve_window_gives_up_when_no_window_arrives(where):
    """Nothing is said to be open until the page is: a browser that was started may still show nothing."""
    announced = []

    async def go():
        async def open_window(launch_uri):
            return where
        return await asyncio.wait_for(serve_window(open_window, announce=announced.append, first_wait=0.2,
                                                   grace=0.1), 5)
    assert asyncio.run(go()) is False and announced == []


def test_presence_needs_the_token_and_ends_on_ctrl_c():
    from ixel_mat.gui import server as gui_server

    async def go():
        announced = []
        serving = asyncio.create_task(gui_server.serve(open_browser=False, announce=announced.append))
        while not announced:
            await asyncio.sleep(0.01)
        base, token = announced[0].split("/#token=")
        async with aiohttp.ClientSession() as session:
            refused = await session.get(f"{base}/api/presence")
            resp = await session.get(f"{base}/api/presence", headers={"Authorization": f"Bearer {token}"})
            await asyncio.sleep(0.1)
            loop = asyncio.get_running_loop()
            started = loop.time()
            serving.cancel()  # Ctrl+C: an open page mustn't hold the server up
            with pytest.raises(asyncio.CancelledError):
                await serving
            took = loop.time() - started
            resp.release()
        return refused.status, resp.status, took

    refused, ok, took = asyncio.run(go())
    assert (refused, ok) == (401, 200)
    assert took < 3


def test_page_holds_presence_open():
    static = Path(window.__file__).parent / "static"
    assert 'api("/api/presence")' in (static / "common.js").read_text(encoding="utf-8")
    assert "stayPresent();" in (static / "app.js").read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_registry_lookup_runs_on_windows():
    assert all(isinstance(p, str) for p in window._registered("msedge.exe"))


def test_programs_the_app_starts_open_no_console_window():
    # The Start Menu's Ixel runs in pythonw.exe: without CREATE_NO_WINDOW, every model CLI and git it
    # started would flash a console window
    import inspect

    from ixel_mat import material
    from ixel_mat.agents import launch, process_tree
    assert "creationflags=NO_WINDOW_FLAGS" in inspect.getsource(material._run_git)
    if os.name == "nt":
        assert process_tree.SPAWN_OPTIONS["creationflags"] & launch.NO_WINDOW_FLAGS == launch.NO_WINDOW_FLAGS
    else:
        assert launch.NO_WINDOW_FLAGS == 0 and process_tree.SPAWN_OPTIONS == {"start_new_session": True}


# ── Native windows: Ixel.app on a Mac, the GTK window on Linux ────────────────

ROOT = Path(__file__).resolve().parent.parent


def test_host_mode_hands_the_address_to_the_window_and_stops_when_it_closes(tmp_path):
    import json
    import subprocess
    import time
    import urllib.request

    from ixel_mat.gui.server import HOST_PREFIX
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "IXEL_NO_UPDATE_CHECK": "1"}
    server = subprocess.Popen([sys.executable, "-m", "ixel_mat", "app", "--host"], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=tmp_path)
    try:
        line = server.stdout.readline().decode()
        assert line.startswith(HOST_PREFIX) and line.endswith("\n")
        base, token = line[len(HOST_PREFIX):].strip().split("/#token=")
        assert base.startswith("http://127.0.0.1:")
        request = urllib.request.Request(f"{base}/api/panel", headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=10) as resp:
            assert resp.status == 200 and "agents" in json.load(resp)
        started = time.monotonic()
        server.stdin.close()  # the window quit
        assert server.wait(timeout=10) == 0 and time.monotonic() - started < 5
        assert server.stdout.read() == b""  # nothing else on stdout, which the window reads
    finally:
        if server.poll() is None:
            server.kill()


def test_every_window_reads_the_same_address_line():
    from ixel_mat.gui import linux_window
    from ixel_mat.gui.server import HOST_PREFIX
    swift = (ROOT / "macos" / "Ixel" / "main.swift").read_text(encoding="utf-8")
    assert f'let hostPrefix = "{HOST_PREFIX}"' in swift and linux_window.HOST_PREFIX == HOST_PREFIX
    assert 'exec \\"$0\\" -I -m ixel_mat app --host' in swift


def test_linux_window_takes_only_ixels_own_address():
    import io

    from ixel_mat.gui.linux_window import read_address
    noise = (b"Welcome back!\nIXEL-URL https://evil.example/#token=x\nIXEL-URL http://127.0.0.1:1@evil.example/\n"
             b"IXEL-URL http://127.0.0.1:5123/#token=abc\n")
    assert read_address(io.BytesIO(noise)) == "http://127.0.0.1:5123/#token=abc"
    assert read_address(io.BytesIO(b"no address\n")) is None


def _app(folder: Path, name: str, bundle: str | None) -> Path:
    import plistlib
    contents = folder / name / "Contents"
    contents.mkdir(parents=True)
    if bundle is not None:
        (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle}))
    return folder / name


def test_mac_app_is_found_in_either_applications_folder(tmp_path):
    assert window.mac_app(tmp_path) is None or window.mac_app(tmp_path).parent == Path("/Applications")
    _app(tmp_path / "Applications", "Ixel.app", window.MAC_APP_ID)
    assert window.mac_app(tmp_path) == tmp_path / "Applications" / "Ixel.app"


def test_mac_app_is_only_ixels_own(tmp_path):
    """`ixel app` never opens an Ixel.app of yours: beside one, Ixel's is Ixel MAT.app."""
    import plistlib
    assert plistlib.loads((ROOT / "macos" / "Info.plist").read_bytes())["CFBundleIdentifier"] == window.MAC_APP_ID
    apps = tmp_path / "Applications"
    yours = _app(apps, "Ixel.app", "com.example.ixel")
    found = window.mac_app(tmp_path)
    assert found is None or found.parent == Path("/Applications")
    ixels = _app(apps, "Ixel MAT.app", window.MAC_APP_ID)
    assert window.mac_app(tmp_path) == ixels
    for broken in (b"<plist/>", b"<plist><array/></plist>", b"<plist><dict>", b"not a plist"):
        (yours / "Contents" / "Info.plist").write_bytes(broken)
        assert window.mac_app(tmp_path) == ixels


@pytest.fixture
def mac_tools(tmp_path):
    """What make-app.sh needs from a Mac, on any Unix: plutil (-replace only), and xcode-select saying there
    are no Command Line Tools, so it makes the browser-mode app."""
    if os.name != "posix":
        pytest.skip("runs bash")
    tools = tmp_path / "mac-tools"
    tools.mkdir()
    (tools / "plutil").write_text(f"""#!{sys.executable}
import plistlib, sys
_, verb, key, kind, value, path = sys.argv
assert verb == "-replace" and kind == "-string"
with open(path, "rb") as f:
    info = plistlib.load(f)
info[key] = value
with open(path, "wb") as f:
    plistlib.dump(info, f)
""", encoding="utf-8")
    (tools / "xcode-select").write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    for tool in tools.iterdir():
        tool.chmod(0o755)
    return f"{tools}:/usr/bin:/bin"


def _make_app(dest: Path, path: str):
    import subprocess
    return subprocess.run(["bash", str(ROOT / "macos" / "make-app.sh"), sys.executable, str(dest), "/opt/x/bin"],
                          capture_output=True, text=True, env={"PATH": path, "HOME": str(dest.parent)})


def test_make_app_never_replaces_an_app_that_isnt_ixels(tmp_path, mac_tools):
    import plistlib
    apps = tmp_path / "Applications"

    def made(name):
        info = plistlib.loads((apps / name / "Contents" / "Info.plist").read_bytes())
        return info["CFBundleIdentifier"], info["CFBundleName"], info["IxelPython"]

    run = _make_app(apps, mac_tools)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["browser", str(apps / "Ixel.app")]
    assert made("Ixel.app") == (window.MAC_APP_ID, "Ixel", sys.executable)
    assert _make_app(apps, mac_tools).returncode == 0 and made("Ixel.app")[0] == window.MAC_APP_ID  # its own: replaced

    # An Ixel.app of yours stays as it is, and Ixel's is Ixel MAT.app beside it, again on each update
    (apps / "Ixel.app").rename(tmp_path / "old")
    yours = _app(apps, "Ixel.app", "com.example.ixel")
    (yours / "Contents" / "yours.txt").write_text("mine", encoding="utf-8")
    for _ in range(2):
        run = _make_app(apps, mac_tools)
        assert run.returncode == 0, run.stderr
        assert run.stdout.splitlines() == ["browser", str(apps / "Ixel MAT.app")]
        assert made("Ixel MAT.app") == (window.MAC_APP_ID, "Ixel MAT", sys.executable)
        assert (yours / "Contents" / "yours.txt").read_text(encoding="utf-8") == "mine"
        assert plistlib.loads((yours / "Contents" / "Info.plist").read_bytes()) == {"CFBundleIdentifier": "com.example.ixel"}

    # Once yours is gone, it's Ixel.app again, and the Ixel MAT.app it made goes
    yours.rename(tmp_path / "yours")
    run = _make_app(apps, mac_tools)
    assert run.stdout.splitlines() == ["browser", str(apps / "Ixel.app")] and not (apps / "Ixel MAT.app").exists()

    # Both names taken by apps that aren't Ixel's (one with no Info.plist at all): it leaves both alone
    for name in ("Ixel.app", "Ixel MAT.app"):
        if (apps / name).exists():
            (apps / name).rename(tmp_path / f"gone-{name}")
    _app(apps, "Ixel.app", "com.example.ixel")
    _app(apps, "Ixel MAT.app", None)
    run = _make_app(apps, mac_tools)
    assert run.returncode == 3 and "left them as they are" in run.stderr and run.stdout == ""
    assert not (apps / "Ixel MAT.app" / "Contents" / "Info.plist").exists()
    assert plistlib.loads((apps / "Ixel.app" / "Contents" / "Info.plist").read_bytes())["CFBundleIdentifier"] == "com.example.ixel"


def _install_app(tmp_path: Path, python: Path):
    """install.sh's install_app on its own (Linux), as the installer runs it."""
    import subprocess
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    functions = script[script.index("linux_window_hint() {"):script.index("\nPYTHON_BIN=")]
    venv = python.parent.parent
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + functions + '\nAPP_NOTE=""\nAPP_PATH=""\ninstall_app\nprintf "%s\\n%s" "$APP_PATH" "$APP_NOTE"'],
                          capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "VENV_DIR": str(venv),
                               "XDG_DATA_HOME": str(tmp_path / "share"), "SOURCE_DIR": str(ROOT),
                               "XDG_DATA_DIRS": f"{tmp_path / 'usr-local'}:{tmp_path / 'usr'}"})


def test_install_sh_never_replaces_a_menu_entry_that_isnt_ixels(tmp_path):
    if os.name != "posix" or sys.platform == "darwin":
        pytest.skip("the Linux app menu")
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\ncase \"$3\" in *ixel.png*) echo /icons/ixel.png ;; *) exit 1 ;; esac\n",
                      encoding="utf-8")
    python.chmod(0o755)
    apps = tmp_path / "share" / "applications"

    run = _install_app(tmp_path, python)
    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines()[:2] == [str(apps / "ixel.desktop"), "Ixel in your app menu (or: ixel app), "
                                           "in Chrome's app mode or your browser for now"]
    assert "Name=Ixel\n" in (apps / "ixel.desktop").read_text(encoding="utf-8")

    yours = "[Desktop Entry]\nName=Ixel\nExec=/opt/ixel/run\n"
    (apps / "ixel.desktop").write_text(yours, encoding="utf-8")
    for _ in range(2):
        run = _install_app(tmp_path, python)
        assert run.stdout.splitlines()[0] == str(apps / "ixel-mat.desktop")
        assert (apps / "ixel.desktop").read_text(encoding="utf-8") == yours
        assert "Name=Ixel MAT\n" in (apps / "ixel-mat.desktop").read_text(encoding="utf-8")

    (apps / "ixel.desktop").unlink()
    run = _install_app(tmp_path, python)
    assert run.stdout.splitlines()[0] == str(apps / "ixel.desktop") and not (apps / "ixel-mat.desktop").exists()

    # One of the system's (in a folder of $XDG_DATA_DIRS), which an ixel.desktop of Ixel's would hide from the menu
    system = tmp_path / "usr" / "applications"
    system.mkdir(parents=True)
    (system / "ixel.desktop").write_text(yours, encoding="utf-8")
    run = _install_app(tmp_path, python)
    assert run.stdout.splitlines()[0] == str(apps / "ixel-mat.desktop")
    assert not (apps / "ixel.desktop").exists() and (system / "ixel.desktop").read_text(encoding="utf-8") == yours
    (system / "ixel.desktop").unlink()

    (apps / "ixel.desktop").write_text(yours, encoding="utf-8")
    (apps / "ixel-mat.desktop").write_text(yours, encoding="utf-8")
    run = _install_app(tmp_path, python)
    assert run.returncode == 0 and run.stdout == "\n" and "Ixel isn't added to it" in run.stderr
    assert (apps / "ixel.desktop").read_text(encoding="utf-8") == (apps / "ixel-mat.desktop").read_text(encoding="utf-8") == yours


def test_the_mac_app_reaches_the_local_server_and_names_its_python():
    import plistlib
    info = plistlib.loads((ROOT / "macos" / "Info.plist").read_bytes())
    assert info["NSAppTransportSecurity"] == {"NSAllowsLocalNetworking": True}  # http://127.0.0.1
    assert info["CFBundleExecutable"] == "Ixel" and info["CFBundleIconFile"] == "ixel"
    assert {"IxelPython", "IxelPath"} <= info.keys()
    script = (ROOT / "macos" / "make-app.sh").read_text(encoding="utf-8")
    for key in ("IxelPython", "IxelPath", "CFBundleShortVersionString"):
        assert f"plutil -replace {key} -string" in script
    assert "xcode-select -p" in script and "app --browser" in script  # without Swift: Chrome's app mode
    assert (ROOT / "ixel_mat" / "assets" / "ixel.icns").read_bytes()[:4] == b"icns"


@pytest.fixture
def system_python(tmp_path):
    """A stand-in for /usr/bin/python3 whose --probe says whether GTK and WebKit are there."""
    if os.name != "posix":
        pytest.skip("a shell-script python")

    def make(has_gtk: bool) -> str:
        script = tmp_path / f"python3-{has_gtk}"
        script.write_text(f"#!/bin/sh\nexit {0 if has_gtk else 1}\n", encoding="utf-8")
        script.chmod(0o755)
        return str(script)
    return make


def test_linux_window_runs_on_a_system_python_with_gtk(system_python):
    with_gtk, without = system_python(True), system_python(False)
    command = window.linux_window_command("/venv/bin/python", [without, "/no/such/python", with_gtk])
    assert command == [with_gtk, "-I", str(window.LINUX_WINDOW), "/venv/bin/python"]
    assert window.linux_window_command("/venv/bin/python", [without]) is None


def test_chrome_windows_on_linux_carry_ixels_window_class():
    assert "--class=Ixel" in window.window_command("chromium", "file:///x", Path("/p"), system="linux")
    assert not any(a.startswith("--class") for a in window.window_command("msedge", "file:///x", Path("/p"),
                                                                          system="win32"))


def test_ixel_app_opens_the_native_window_unless_told_not_to(monkeypatch, tmp_path):
    from ixel_mat import cli
    calls = []
    monkeypatch.setattr("subprocess.call", lambda cmd: calls.append(cmd) or 0)
    monkeypatch.setattr(window, "mac_app", lambda: tmp_path / "Ixel.app")
    monkeypatch.setattr(window, "linux_window_command", lambda: ["/usr/bin/python3", "linux_window.py", "py"])
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    assert cli.cmd_app([]) == 0 and calls == [["/usr/bin/open", str(tmp_path / "Ixel.app")]]
    monkeypatch.setattr(cli.sys, "platform", "linux")
    assert cli.cmd_app([]) == 0 and calls[-1] == ["/usr/bin/python3", "linux_window.py", "py"]

    async def no_window(open_window, announce, port):
        calls.append("browser window")
        return True
    monkeypatch.setattr("ixel_mat.gui.server.serve_window", no_window)
    assert cli.cmd_app(["--browser"]) == 0 and calls[-1] == "browser window"


def test_ixel_app_opens_a_browser_window_when_the_mac_app_wont_open(monkeypatch, tmp_path):
    from ixel_mat import cli
    calls = []
    monkeypatch.setattr("subprocess.call", lambda cmd: calls.append(cmd) or 1)  # open: error -1712
    monkeypatch.setattr(window, "mac_app", lambda: tmp_path / "Ixel.app")
    monkeypatch.setattr(cli.sys, "platform", "darwin")

    async def browser_window(open_window, announce, port):
        calls.append("browser window")
        return True
    monkeypatch.setattr("ixel_mat.gui.server.serve_window", browser_window)
    assert cli.cmd_app([]) == 0
    assert calls == [["/usr/bin/open", str(tmp_path / "Ixel.app")], "browser window"]


def test_install_sh_adds_ixel_to_the_app_menu_or_applications():
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'Exec="$python" -I -m ixel_mat app' in script and "StartupWMClass=Ixel" in script
    assert 'bash "$SOURCE_DIR/macos/make-app.sh" "$python" "$HOME/Applications" "$PATH"' in script
    assert 'if [[ "${IXEL_SKIP_APP_ENTRY:-0}" == "1" ]]; then' in script
    # Before the install is recorded, so `ixel update` (which runs this again) keeps it current
    assert script.index("\ninstall_app\n") < script.index('"$INSTALL_ROOT/install.json"')


def test_linux_window_opens_and_stops_its_server(tmp_path):
    """The real GTK window, where a system Python has GTK and WebKit and there's a display."""
    import subprocess
    import time
    if not os.environ.get("DISPLAY"):
        pytest.skip("no display (run under xvfb-run)")
    pythons = [p for p in ("/usr/bin/python3", "/usr/bin/python3.12", "/usr/bin/python3.13") if os.path.exists(p)]
    command = window.linux_window_command(sys.executable, pythons)
    if command is None:
        pytest.skip("no system Python with GTK and WebKit")
    env = {**os.environ, "HOME": str(tmp_path), "IXEL_NO_UPDATE_CHECK": "1"}
    win = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30
        host = None
        while time.monotonic() < deadline and not host:
            ps = subprocess.run(["ps", "-ww", "-eo", "pid,ppid,args"], capture_output=True, text=True).stdout
            host = [ln for ln in ps.splitlines() if "ixel_mat app --host" in ln and f" {win.pid} " in f" {ln} "]
            time.sleep(0.5)
        assert host, "the window didn't start its server"
        time.sleep(3)
        assert win.poll() is None  # the window is up
    finally:
        win.terminate()  # closing the window ends the server's stdin
        win.wait(timeout=10)
    time.sleep(3)
    ps = subprocess.run(["ps", "-ww", "-eo", "ppid,args"], capture_output=True, text=True).stdout
    assert not [ln for ln in ps.splitlines() if "ixel_mat app --host" in ln and ln.split()[0] == str(win.pid)]


class MediaRequest:
    def __init__(self, audio=True, video=False):
        self.props = {"is-for-audio-device": audio, "is-for-video-device": video}
        self.answer = None

    def get_property(self, name):
        return self.props[name]

    def allow(self):
        self.answer = "allow"

    def deny(self):
        self.answer = "deny"


@pytest.mark.parametrize("uri, request_kind, answer", [
    ("http://127.0.0.1:5123/#token=abc", MediaRequest(), "allow"),
    ("http://127.0.0.1:5123/#token=abc", MediaRequest(video=True), "deny"),               # the camera too
    ("http://127.0.0.1:5123/#token=abc", MediaRequest(audio=False, video=True), "deny"),
    ("https://evil.example/", MediaRequest(), "deny"),
    ("http://127.0.0.1:5123@evil.example/", MediaRequest(), "deny"),     # evil.example, with a user name
    ("http://127.0.0.1:80@evil.example:5123/", MediaRequest(), "deny"),
    ("http://127.0.0.1:6000/", MediaRequest(), "deny"),                  # another program on this computer
    ("http://127.0.0.1.evil.example:5123/", MediaRequest(), "deny"),
    (None, MediaRequest(), "deny"),
])
def test_linux_window_lets_only_ixels_page_use_the_microphone(uri, request_kind, answer):
    from ixel_mat.gui.linux_window import answer_permission
    assert answer_permission(uri, "http://127.0.0.1:5123/#token=abc", request_kind, MediaRequest) is True
    assert request_kind.answer == answer


def test_linux_window_leaves_other_permissions_to_webkit():
    from ixel_mat.gui.linux_window import answer_permission
    assert answer_permission("http://127.0.0.1:5123/", "http://127.0.0.1:5123/", object(), MediaRequest) is False


@pytest.mark.parametrize("uri, inside", [
    ("http://127.0.0.1:5123/", True),
    ("http://127.0.0.1:5123/api/board?x=1#y", True),
    ("http://127.0.0.1:5123@evil.example/", False),
    ("http://user:pw@127.0.0.1:5123/", False),
    ("http://127.0.0.1:5124/", False),
    ("https://127.0.0.1:5123/", False),
    ("http://localhost:5123/", False),
    ("http://127.0.0.1:99999/", False),
    ("file:///etc/passwd", False),
    ("", False),
])
def test_linux_window_keeps_only_ixels_own_page_inside(uri, inside):
    from ixel_mat.gui.linux_window import is_ixel_page
    assert is_ixel_page(uri, "http://127.0.0.1:5123/#token=abc") is inside


def test_the_mac_app_shows_a_file_chooser_for_the_page():
    swift = (ROOT / "macos" / "Ixel" / "main.swift").read_text(encoding="utf-8")
    assert "webView.uiDelegate = self" in swift and "runOpenPanelWith parameters: WKOpenPanelParameters" in swift


def test_the_mac_app_says_why_it_wants_the_microphone():
    import plistlib
    info = plistlib.loads((ROOT / "macos" / "Info.plist").read_bytes())
    assert "voice note" in info["NSMicrophoneUsageDescription"]



@pytest.mark.parametrize("uri, opens", [("https://ixelai.com/", True), ("mailto:a@b.example", True),
                                        ("file:///etc/passwd", False), ("steam://run/1", False),
                                        ("http://[oops/", False)])
def test_linux_window_opens_only_web_links_in_the_browser(uri, opens):
    from ixel_mat.gui.linux_window import opens_in_browser
    assert opens_in_browser(uri) is opens
