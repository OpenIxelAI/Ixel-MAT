"""`ixel docs`, /docs and Settings' Docs button: the docs on ixelai.com, opened in your own browser."""
import sys

from ixel_mat import cli, docs
from ixel_mat.commands import resolve_command_name
from test_gui_server import AUTH, JSON_AUTH, make_gui, run_with_client


def test_the_address_is_fixed_and_on_https():
    assert docs.DOCS_URL == "https://ixelai.com/docs/"


def test_opens_the_docs_where_there_is_a_desktop(monkeypatch):
    opened = []
    monkeypatch.setattr(docs, "can_open_browser", lambda: True)
    assert docs.open_docs(lambda url: opened.append(url) or True) is True
    assert opened == [docs.DOCS_URL]


def test_over_ssh_on_linux_no_text_browser_is_started(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    opened = []
    assert docs.open_docs(lambda url: opened.append(url) or True) is False
    assert opened == []
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert docs.can_open_browser()


def test_a_browser_that_wont_start_just_means_the_address_is_shown(monkeypatch):
    monkeypatch.setattr(docs, "can_open_browser", lambda: True)

    def broken(url):
        raise OSError("no browser")
    assert docs.open_docs(broken) is False
    assert docs.open_docs(lambda url: False) is False


def test_docs_is_a_command_and_doc_no_longer_means_doctor():
    assert resolve_command_name("docs", mode="cli") == "docs"
    assert resolve_command_name("doctor", mode="cli") == "doctor"
    assert resolve_command_name("doc", mode="cli") == ("ambiguous", ["docs", "doctor"])
    assert resolve_command_name("docs", mode="mat") == "docs"


def test_ixel_docs_prints_the_address_and_opens_it(monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(docs, "open_docs", lambda: opened.append(True) or True)
    assert cli.cmd_docs([]) == 0
    out = capsys.readouterr().out
    assert docs.DOCS_URL in out and "Opened in your browser" in out and opened == [True]
    assert cli.cmd_docs(["--no-browser"]) == 0
    out = capsys.readouterr().out
    assert docs.DOCS_URL in out and "Opened" not in out and opened == [True]


def test_settings_docs_button_has_the_server_open_the_fixed_address(monkeypatch):
    opened = []
    monkeypatch.setattr(docs, "open_docs", lambda: opened.append(True) or True)

    async def scenario(client):
        ok = await client.post("/api/docs", headers=JSON_AUTH, json={})
        no_key = await client.post("/api/docs", headers={"Content-Type": "application/json"}, json={})
        cross = await client.post("/api/docs", headers={**JSON_AUTH, "Origin": "https://evil.example"}, json={})
        read = await client.get("/api/docs", headers=AUTH)
        return ok.status, await ok.json(), no_key.status, cross.status, read.status

    status, reply, no_key, cross, read = run_with_client(make_gui()[0], scenario)
    assert (status, reply) == (200, {"opened": True, "url": docs.DOCS_URL})
    assert (no_key, cross, read) == (401, 403, 405)
    assert opened == [True]  # only the request with the session key opened anything
