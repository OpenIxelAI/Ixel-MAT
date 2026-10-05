"""The app's picture uploads: a counted, checked upload route, kept in memory, sent only to models that see them."""
import json

import pytest

from ixel_mat import pictures
from ixel_mat.agents.base import AgentConfig
from ixel_mat.gui.server import GuiServer
from ixel_mat.runtime import Settings
from test_gui_server import AUTH, JSON_AUTH, TOKEN, read_events, run_with_client
from test_pictures import png, png_chunk

PNG = png(extra=png_chunk(b"tEXt", b"Author\x00Someone's full name"))
PNG_AUTH = {**AUTH, "Content-Type": "image/png"}


class Agent:
    def __init__(self, name, sees):
        self.name, self.label, self.is_connected = name, name.title(), True
        self.config = AgentConfig(name=name, label=self.label, type="http", url="https://api.example.com",
                                  token="sk-never-shown", model="m", accepts=["image"] if sees else [])
        self.calls = []

    async def send_and_receive(self, message, **kwargs):
        self.calls.append((message, kwargs.get("pictures")))
        return "A small orange square. VERDICT: correct"

    async def disconnect(self):
        self.is_connected = False


def make_gui():
    agents = [Agent("seeing", True), Agent("blind", False)]
    configs = {a.name: a.config for a in agents}

    async def connect(cfgs, on_result=None):
        return {a.name: a for a in agents}

    async def disconnect(connected):
        pass

    settings = Settings({}, configs)
    return GuiServer(token=TOKEN, settings_loader=lambda: settings, connect=connect, disconnect=disconnect), agents


async def upload(client, data=PNG, headers=PNG_AUTH):
    resp = await client.post("/api/pictures", headers=headers, data=data)
    return resp.status, await resp.json()


def test_a_picture_is_kept_without_its_metadata_and_sent_only_to_models_that_see_it():
    gui, (seeing, blind) = make_gui()

    async def scenario(client):
        status, data = await upload(client)
        assert status == 200 and (data["width"], data["height"]) == (2, 2)
        [kept] = gui.pictures.take([data["id"]])
        assert b"Someone" not in kept.data and kept.data.startswith(pictures.PNG_SIGNATURE)
        resp = await client.post("/api/review", headers=JSON_AUTH, data=json.dumps(
            {"question": "What colour is it?", "mode": "quick", "pictures": [data["id"]]}))
        assert resp.status == 200
        events = await read_events(resp)
        panel = await (await client.get("/api/panel", headers=AUTH)).json()
        return kept, events, panel

    kept, events, panel = run_with_client(gui, scenario)
    assert events[-1]["kind"] == "final" and events[-1]["data"]["result"]["pictures"] == 1
    assert seeing.calls and all(p == (kept,) for _, p in seeing.calls)
    assert blind.calls and all(p is None and "which you can't see" in m for m, p in blind.calls)
    assert {a["name"]: a["pictures"] for a in panel["agents"]} == {"seeing": True, "blind": False}


def test_pictures_alone_get_a_question_of_their_own():
    gui, (seeing, _) = make_gui()

    async def scenario(client):
        ids = [(await upload(client))[1]["id"] for _ in range(2)]
        resp = await client.post("/api/review", headers=JSON_AUTH,
                                 data=json.dumps({"question": "", "mode": "quick", "pictures": ids}))
        return resp.status, await read_events(resp)

    status, events = run_with_client(gui, scenario)
    assert status == 200 and events[-1]["data"]["result"]["question"] == "What do you make of the attached pictures?"


@pytest.mark.parametrize("value, says", [
    (["not-an-id"], "no longer here"),
    ("abc", "list of the ids"),
    ([1, 2], "list of the ids"),
    (["x"] * 9, "at most 8"),
])
def test_a_question_names_only_pictures_ixel_has(value, says):
    gui, (seeing, blind) = make_gui()

    async def scenario(client):
        resp = await client.post("/api/review", headers=JSON_AUTH,
                                 data=json.dumps({"question": "q", "pictures": value}))
        return resp.status, await resp.json()

    status, data = run_with_client(gui, scenario)
    assert status == 400 and says in data["error"]
    assert data.get("code") == ("pictures_gone" if says == "no longer here" else None)  # the page attaches it again
    assert not seeing.calls and not blind.calls


def test_a_picture_over_the_limit_is_refused_while_its_read(monkeypatch):
    monkeypatch.setattr(pictures, "MAX_BYTES", 1000)
    gui, _ = make_gui()

    async def chunks():  # no Content-Length: only counting while reading catches it
        for _ in range(20):
            yield b"\0" * 100

    async def scenario(client):
        said = await upload(client, data=b"\0" * 1001)
        streamed = await upload(client, data=chunks())
        fits = await upload(client, data=png())
        return said, streamed, fits, len(gui.pictures._items)

    said, streamed, fits, kept = run_with_client(gui, scenario)
    assert said[0] == 413 and streamed[0] == 413 and "MB" in said[1]["error"]
    assert fits[0] == 200
    assert kept == 1


@pytest.mark.parametrize("headers, data, status", [
    ({**AUTH, "Content-Type": "image/gif"}, b"GIF89a", 415),
    ({**AUTH, "Content-Type": "application/json"}, b"{}", 415),
    (PNG_AUTH, b"\x89PNG\r\n\x1a\n not really", 400),
    (PNG_AUTH, b"<svg onload=alert(1)>", 400),
    ({"Content-Type": "image/png"}, PNG, 401),
    ({**PNG_AUTH, "Origin": "https://evil.example"}, PNG, 403),
])
def test_only_a_png_or_jpeg_from_this_page_is_taken(headers, data, status):
    gui, _ = make_gui()

    async def scenario(client):
        resp = await client.post("/api/pictures", headers=headers, data=data)
        return resp.status

    assert run_with_client(gui, scenario) == status
    assert not gui.pictures._items


def test_other_requests_keep_their_size_limit():
    gui, (seeing, _) = make_gui()

    async def scenario(client):
        resp = await client.post("/api/review", headers=JSON_AUTH,
                                 data=json.dumps({"question": "x" * (600 * 1024)}))
        return resp.status

    assert run_with_client(gui, scenario) == 413 and not seeing.calls


def test_pictures_are_gone_when_ixel_stops():
    gui, _ = make_gui()

    async def scenario(client):
        await upload(client)
        return len(gui.pictures._items)

    assert run_with_client(gui, scenario) == 1
    assert not gui.pictures._items

