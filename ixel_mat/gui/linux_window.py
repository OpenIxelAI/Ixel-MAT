"""
Ixel's own window on Linux: GTK with WebKit around the browser app that `ixel app --host` serves.

It runs on the system's Python, which has GTK and WebKit where the desktop does (Ixel's virtual
environment can't see them), so it imports nothing from Ixel:

    /usr/bin/python3 -I linux_window.py <Ixel's python>      open the window
    /usr/bin/python3 -I linux_window.py --probe              exit 0 if GTK and WebKit are installed

It starts `<python> -I -m ixel_mat app --host`, reads the page's address (with this session's key) from
its stdout, a pipe only this window reads, and shows it. Closing the window closes that program's stdin,
which stops it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from urllib.parse import urlsplit

HOST_PREFIX = "IXEL-URL "
ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "assets", "ixel.png")


def load_gtk():
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    for version in ("4.1", "4.0"):
        try:
            gi.require_version("WebKit2", version)
            break
        except ValueError:
            continue
    else:
        raise ImportError("WebKit2GTK isn't installed")
    from gi.repository import Gdk, Gio, GLib, Gtk, WebKit2
    return Gdk, Gio, GLib, Gtk, WebKit2


def _origin(uri: str | None) -> tuple[str, str, int] | None:
    """(scheme, host, port) of an address Ixel's server could have: http to 127.0.0.1 on a port, with no
    name or password before the host (http://127.0.0.1:80@evil.example/ is evil.example). None otherwise."""
    try:
        parts = urlsplit(uri or "")
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "http" or parts.hostname != "127.0.0.1" or port is None or "@" in parts.netloc:
        return None
    return parts.scheme, parts.hostname, port


def is_ixel_page(uri: str | None, address: str | None) -> bool:
    """True if uri is on the server that address (the one `ixel app --host` printed) is on."""
    here = _origin(address)
    return here is not None and _origin(uri) == here


def opens_in_browser(uri: str) -> bool:
    """A link away from Ixel's page opens in your browser if it's a web page or an email address; anything
    else (file:, or a scheme that starts some other program) is ignored."""
    try:
        return urlsplit(uri).scheme in ("http", "https", "mailto")
    except ValueError:
        return False


def read_address(stream) -> str | None:
    """The page's address: the line that starts with HOST_PREFIX (anything before it is skipped)."""
    for raw in stream:
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith(HOST_PREFIX):
            address = line[len(HOST_PREFIX):]
            if _origin(address) is not None:
                return address
    return None


def answer_permission(uri: str | None, address: str | None, request, media_request_type) -> bool:
    """The microphone, for Ixel's own page (Ask's Record button); the camera and the screen are refused.
    Anything else is left to WebKit, which refuses it. True when it's answered here."""
    if not isinstance(request, media_request_type):
        return False
    if (is_ixel_page(uri, address) and request.get_property("is-for-audio-device")
            and not request.get_property("is-for-video-device")):
        request.allow()
    else:
        request.deny()
    return True


def main(argv: list[str]) -> int:
    if argv[1:] == ["--probe"]:
        try:
            load_gtk()
        except (ImportError, ValueError):
            return 1
        return 0
    if len(argv) != 2:
        print("usage: linux_window.py <Ixel's python> | --probe", file=sys.stderr)
        return 2
    Gdk, Gio, GLib, Gtk, WebKit2 = load_gtk()
    # Matches ixel.desktop (StartupWMClass=Ixel), so the dock shows Ixel's name and icon for this window
    GLib.set_prgname("ixel")
    GLib.set_application_name("Ixel")

    server = subprocess.Popen([argv[1], "-I", "-m", "ixel_mat", "app", "--host"], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, cwd=os.path.expanduser("~"))
    window = Gtk.Window(title="Ixel")
    if os.path.isfile(ICON):
        window.set_icon_from_file(ICON)
    monitor = Gdk.Display.get_default().get_primary_monitor() or Gdk.Display.get_default().get_monitor(0)
    area = monitor.get_workarea() if monitor else None
    width, height = (min(1280, int(area.width * 0.9)), min(840, int(area.height * 0.9))) if area else (1280, 840)
    window.set_default_size(width, height)
    window.set_position(Gtk.WindowPosition.CENTER)
    view = WebKit2.WebView()
    page = {"address": None}  # the server's address, once it's printed it

    def stay_inside(_view, decision, kind):
        # Anything that isn't Ixel's own page opens in your browser
        if kind == WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri() or ""
            if not (is_ixel_page(uri, page["address"]) or uri == "about:blank"):
                if opens_in_browser(uri):
                    Gio.AppInfo.launch_default_for_uri(uri, None)
                decision.ignore()
                return True
        return False

    view.connect("decide-policy", stay_inside)
    view.get_settings().set_enable_media_stream(True)  # Ask's Record button (off in WebKitGTK by default)
    view.connect("permission-request", lambda v, request: answer_permission(
        v.get_uri(), page["address"], request, WebKit2.UserMediaPermissionRequest))
    window.add(view)
    window.connect("destroy", lambda _w: Gtk.main_quit())
    window.show_all()

    def fail(message: str) -> bool:
        dialog = Gtk.MessageDialog(transient_for=window, modal=True, message_type=Gtk.MessageType.WARNING,
                                   buttons=Gtk.ButtonsType.CLOSE, text="Ixel")
        dialog.format_secondary_text(message)
        dialog.run()
        Gtk.main_quit()
        return False

    def watch_server() -> None:
        address = read_address(server.stdout)
        if address:
            page["address"] = address
            GLib.idle_add(view.load_uri, address)
        for _ in server.stdout:  # keep the pipe drained until the server exits
            pass
        code = server.wait()
        if not closing.is_set():
            GLib.idle_add(fail, f"Ixel stopped (exit code {code}). Open it again. If that keeps happening, run "
                                "`ixel gui` in a terminal to see why.")

    closing = threading.Event()
    threading.Thread(target=watch_server, name="ixel-server", daemon=True).start()
    try:
        Gtk.main()
    finally:
        closing.set()
        try:
            server.stdin.close()  # end of input: the server stops by itself
            server.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            server.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
