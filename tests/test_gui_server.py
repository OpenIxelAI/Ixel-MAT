"""`ixel gui` server: access control, security headers, and the review stream."""
import asyncio
import json
import re
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from fake_providers import ThreadedFakeProvider, typesafe_handler, panel_text
from ixel_mat.agents.base import AgentConfig
from ixel_mat.gui import server as gui_server
from ixel_mat.gui.server import CSP, GuiServer
from ixel_mat import stats
from ixel_mat.triage import TriageSettings
from ixel_mat.runtime import SaverSettings, Settings

ANSWERS = {"m-gpt": "It's 391.", "m-claude": "17 × 23 = 391", "m-wrong": "The answer is 381."}
TOKEN = "test-token-abc123"


class Agent:
    def __init__(self, name, model, *, delay=0.0, answer=None):
        self.name, self.label, self.model, self.is_connected = name, name.title(), model, True
        self.delay, self.answer = delay, answer
        self.cancelled = 0

    async def send_and_receive(self, message, **kwargs):
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.answer is not None and "You are" not in message:
            return self.answer
        return panel_text(ANSWERS, self.model, message)

    async def disconnect(self):
        self.is_connected = False


def make_gui(agents=None, saver=None, triage=None):
    agents = agents if agents is not None else [
        Agent("gpt", "m-gpt"), Agent("claude", "m-claude"), Agent("gemini", "m-wrong")]
    configs = {a.name: AgentConfig(name=a.name, label=a.label, type="http", url="https://api.example.com",
                                   token="sk-never-shown", model=a.model) for a in agents}

    async def connect(cfgs, on_result=None):
        for cfg in cfgs.values():
            if on_result:
                await on_result(cfg, None)
        return {a.name: a for a in agents}

    async def disconnect(connected):
        for a in connected.values():
            await a.disconnect()

    settings = Settings({}, configs, saver=saver or SaverSettings(), triage=triage or TriageSettings())
    gui = GuiServer(token=TOKEN, settings_loader=lambda: settings, connect=connect, disconnect=disconnect)
    return gui, agents


def run_with_client(gui, scenario):
    async def go():
        server = TestServer(gui.app(), host="127.0.0.1")
        async with TestClient(server) as client:
            gui.port = server.port
            return await scenario(client)
    return asyncio.run(go())


AUTH = {"Authorization": f"Bearer {TOKEN}"}
JSON_AUTH = {**AUTH, "Content-Type": "application/json"}


async def read_events(resp):
    return [json.loads(line) for line in (await resp.text()).splitlines() if line.strip()]


# ── Static page + headers ─────────────────────────────────────────────────────

def test_page_is_served_with_strict_security_headers():
    async def scenario(client):
        page = await client.get("/")
        missing = await client.get("/server.py")
        return page.status, dict(page.headers), await page.text(), missing.status, dict(missing.headers)

    status, headers, body, missing_status, missing_headers = run_with_client(make_gui()[0], scenario)
    assert status == 200 and '<script type="module" src="/app.js"></script>' in body
    assert headers["Content-Security-Policy"] == CSP
    assert "'unsafe-inline'" not in CSP and "script-src 'self'" in CSP
    assert headers["X-Content-Type-Options"] == "nosniff" and headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer" and headers["Cache-Control"] == "no-store"
    assert missing_status == 404 and missing_headers["Content-Security-Policy"] == CSP


def test_only_listed_static_files_exist():
    async def scenario(client):
        return [(await client.get(p)).status for p in ("/app.js", "/common.js", "/ask.js", "/health.js", "/board.js",
                                                        "/settings.js", "/markdown.js", "/style.css", "/mark.svg",
                                                        "/static/app.js", "/%2e%2e/server.py", "/app.js/../server.py",
                                                        "/health.py", "/settings_api.py")]
    assert run_with_client(make_gui()[0], scenario) == [200] * 9 + [404] * 5


def test_every_script_the_page_has_is_served():
    """A module the page imports but the server doesn't list would break the whole window."""
    static = Path(gui_server.__file__).parent / "static"
    served = {name for name, _ in gui_server.STATIC_FILES.values()}
    assert {p.name for p in static.glob("*.js")} <= served
    for script in static.glob("*.js"):
        for imported in re.findall(r'from "\./([\w.-]+)"|import "\./([\w.-]+)"', script.read_text(encoding="utf-8")):
            assert (imported[0] or imported[1]) in served, (script.name, imported)


def test_scripts_can_only_come_from_this_server():
    """The confirmations, Settings and Board all trust the page: the CSP is what keeps other code out."""
    script_src = next(part for part in CSP.split(";") if part.strip().startswith("script-src")).split()[1:]
    assert script_src == ["'self'"]
    assert "default-src 'none'" in CSP and "unsafe" not in CSP


def test_the_page_never_parses_html():
    """Model text reaches the page only as text nodes: none of the ways to turn a string into markup are used."""
    static = Path(gui_server.__file__).parent / "static"
    scripts = sorted(static.glob("*.js"))
    assert len(scripts) >= 4
    for script in scripts:
        code = script.read_text(encoding="utf-8")
        # The one link a script makes: Save, for a recording the page made itself (a blob: address), never shown
        code = code.replace("{ href: url, download: `Ixel recording.${ext}` }", "")
        for api in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                    "DOMParser", "createContextualFragment", "srcdoc", "setAttribute(\"on", "href", "setTimeout(\"",
                    "import(", "javascript:"):
            assert api not in code, (script.name, api)
    page = (static / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in page and "<style" not in page and " style=" not in page
    assert not re.search(r"\son[a-z]+=", page)  # no inline event handlers


# ── Access control ────────────────────────────────────────────────────────────

def test_api_needs_the_session_token():
    async def scenario(client):
        none = await client.get("/api/panel")
        wrong = await client.get("/api/panel", headers={"Authorization": "Bearer nope"})
        basic = await client.get("/api/panel", headers={"Authorization": f"Basic {TOKEN}"})
        ok = await client.get("/api/panel", headers=AUTH)
        return none.status, wrong.status, basic.status, ok.status, await ok.text()

    none, wrong, basic, ok, body = run_with_client(make_gui()[0], scenario)
    assert (none, wrong, basic, ok) == (401, 401, 401, 200)
    data = json.loads(body)
    assert [a["label"] for a in data["agents"]] == ["Gpt", "Claude", "Gemini"]
    assert all(a["ready"] for a in data["agents"])
    assert "sk-never-shown" not in body


def test_foreign_host_header_is_refused():
    # DNS rebinding: a website that resolves its name to 127.0.0.1 still sends its own Host
    async def scenario(client):
        page = await client.get("/", headers={"Host": "evil.example"})
        api = await client.get("/api/panel", headers={**AUTH, "Host": f"evil.example:{client.port}"})
        return page.status, api.status

    assert run_with_client(make_gui()[0], scenario) == (403, 403)


def test_cross_site_posts_are_refused():
    async def scenario(client):
        body = json.dumps({"question": "q"})
        foreign = await client.post("/api/review", data=body, headers={**JSON_AUTH, "Origin": "https://evil.example"})
        form = await client.post("/api/review", data="question=q",
                                 headers={**AUTH, "Content-Type": "application/x-www-form-urlencoded"})
        return foreign.status, form.status

    assert run_with_client(make_gui()[0], scenario) == (403, 415)


@pytest.mark.parametrize("payload,status,message", [
    ("not json", 400, "JSON"),
    (json.dumps(["q"]), 400, "JSON object"),
    (json.dumps({"question": "  "}), 400, "Ask a question"),
    (json.dumps({"question": "x" * 50_001}), 400, "longer than"),
    (json.dumps({"question": "q", "mode": "loud"}), 400, "mode"),
    (json.dumps({"question": "q", "mode": 0}), 400, "mode"),
    (json.dumps({"question": "q", "mode": ["quick"]}), 400, "mode"),
    (json.dumps({"question": "q", "earlier": "x"}), 400, "earlier"),
    (json.dumps({"question": "q", "earlier": [{"question": "a", "answer": "b"}] * 4}), 400, "up to 3"),
    (json.dumps({"question": "q", "earlier": [{"question": "a", "answer": 5}]}), 400, "earlier"),
    (json.dumps({"question": "q", "earlier": [{"question": "a", "answer": "b" * 20_001}]}), 400, "earlier"),
], ids=["not-json", "json-array", "blank-question", "question-too-long", "bad-mode", "mode-a-number", "mode-a-list", "earlier-not-a-list",
        "too-many-earlier", "earlier-not-text", "earlier-too-long"])
def test_review_input_is_validated(payload, status, message):
    gui, agents = make_gui()

    async def scenario(client):
        resp = await client.post("/api/review", data=payload, headers=JSON_AUTH)
        return resp.status, (await resp.json())["error"]

    got_status, error = run_with_client(gui, scenario)
    assert got_status == status and message in error


# ── Review stream ─────────────────────────────────────────────────────────────

def test_a_follow_up_reaches_the_panel():
    gui, agents = make_gui()
    prompts = []
    for agent in agents:
        original = agent.send_and_receive

        async def recording(message, _original=original, **kwargs):
            prompts.append(message)
            return await _original(message, **kwargs)

        agent.send_and_receive = recording

    async def scenario(client):
        body = {"question": "And doubled?", "mode": "quick",
                "earlier": [{"question": "What is 17 × 23?", "answer": "17 × 23 = 391"}]}
        resp = await client.post("/api/review", data=json.dumps(body), headers=JSON_AUTH)
        return await read_events(resp)

    events = run_with_client(gui, scenario)
    final = next(e for e in events if e["kind"] == "final")["data"]["result"]
    assert final["earlier_turns"] == 1
    assert all("earlier in this conversation" in p and "17 × 23 = 391" in p for p in prompts)


def test_review_streams_every_round_and_the_verdict():
    async def scenario(client):
        resp = await client.post("/api/review", data=json.dumps({"question": "What is 17 × 23?"}), headers=JSON_AUTH)
        return resp.status, resp.headers["Content-Type"], await read_events(resp)

    status, content_type, events = run_with_client(make_gui()[0], scenario)
    assert status == 200 and content_type.startswith("application/x-ndjson")
    kinds = [e["kind"] for e in events]
    assert kinds.count("connect") == 3 and kinds.count("answer") == 3 and kinds.count("review") == 3
    assert kinds.index("start") < kinds.index("labels") < kinds.index("review") < kinds.index("final")
    start = next(e for e in events if e["kind"] == "start")["data"]
    assert start["rounds"] == ["answer", "review", "verdict"]
    result = events[-1]["data"]["result"]
    assert result["final"]["answer"] == "17 × 23 = 391"
    assert [c["reviewer_label"] for c in result["concessions"]] == ["Gemini"]


def test_mode_can_be_chosen_per_request():
    async def scenario(client):
        resp = await client.post("/api/review", data=json.dumps({"question": "What is 17 × 23?", "mode": "deep"}),
                                 headers=JSON_AUTH)
        return await read_events(resp)

    events = run_with_client(make_gui()[0], scenario)
    assert events[-1]["data"]["result"]["mode"] == "deep"
    assert any(e["kind"] == "revision" for e in events)


def triage_at(url, **extra):
    return TriageSettings(enabled=True, url=url, token="ts-test", timeout=2.0, **extra)


def test_panel_says_whether_triage_is_set_up():
    async def scenario(client):
        return await (await client.get("/api/panel", headers=AUTH)).json()

    off = run_with_client(make_gui()[0], scenario)["triage"]
    assert off == {"ready": False, "auto": False, "skip_review": False, "saver_gate": False,
                   "provider": "typesafe", "via": "", "host": "", "official": False}
    on = run_with_client(make_gui(triage=TriageSettings(enabled=True, token="ts-test"))[0], scenario)
    assert on["triage"]["ready"] and on["triage"]["auto"] and on["triage"]["official"]
    assert on["triage"]["host"] == "api.typesafe.ai" and "ts-test" not in json.dumps(on)
    own = TriageSettings(enabled=True, provider="model", agent="claude",
                         agent_config=AgentConfig(name="claude", label="Claude Haiku", type="http",
                                                  url="https://api.example.com", token="sk-never-shown"))
    mine = run_with_client(make_gui(triage=own)[0], scenario)["triage"]
    assert mine["ready"] and mine["provider"] == "model" and mine["via"] == "Claude Haiku"
    assert mine["host"] == "" and not mine["official"]


def test_auto_mode_lets_triage_pick_and_reports_it():
    with ThreadedFakeProvider() as fake:
        fake.typesafe_handler = typesafe_handler(depth=0.2)

        async def scenario(client):
            resp = await client.post("/api/review", data=json.dumps({"question": "What is 17 × 23?", "mode": "auto"}),
                                     headers=JSON_AUTH)
            return await read_events(resp)

        events = run_with_client(make_gui(triage=triage_at(fake.typesafe_url))[0], scenario)
    start = next(e for e in events if e["kind"] == "start")["data"]
    assert start["mode"] == "quick" and start["auto"] and start["rounds"] == ["answer", "verdict"]
    triage = [e["data"] for e in events if e["kind"] == "triage"]
    assert triage[0]["about"] == "mode" and triage[0]["value"] == "quick" and triage[0]["acted"]
    result = events[-1]["data"]["result"]
    assert result["mode"] == "quick" and result["triage_calls"] == 1


def test_auto_mode_without_triage_still_runs_and_says_why():
    async def scenario(client):
        resp = await client.post("/api/review", data=json.dumps({"question": "What is 17 × 23?", "mode": "auto"}),
                                 headers=JSON_AUTH)
        return await read_events(resp)

    events = run_with_client(make_gui()[0], scenario)
    result = events[-1]["data"]["result"]
    assert result["mode"] == "review" and result["triage_calls"] == 0
    assert "Auto mode needs triage" in result["triage"][0]["note"]


def test_answers_that_agree_skip_peer_review_in_the_stream():
    agree = [Agent("gpt", "m-gpt", answer="391"), Agent("claude", "m-claude", answer="17 × 23 = 391"),
             Agent("gemini", "m-wrong", answer="It's 391.")]
    with ThreadedFakeProvider() as fake:
        fake.typesafe_handler = typesafe_handler(agree=0.97)

        async def scenario(client):
            resp = await client.post("/api/review", data=json.dumps({"question": "What is 17 × 23?"}),
                                     headers=JSON_AUTH)
            return await read_events(resp)

        events = run_with_client(make_gui(agree, triage=triage_at(fake.typesafe_url, skip_review=True))[0], scenario)
    kinds = [e["kind"] for e in events]
    assert "review" not in kinds and kinds.count("answer") == 3
    triage = next(e for e in events if e["kind"] == "triage")["data"]
    assert triage["skipped"] == ["review"] and "peer review was skipped" in triage["note"]
    assert events[-1]["data"]["result"]["skipped_rounds"] == ["review"]


def test_stream_strips_control_codes_and_bidi_overrides():
    hostile = "391 \x1b]52;c;ZXZpbA==\x07 ‮gnirts‬ done"
    gui, _ = make_gui([Agent("gpt", "m-gpt", answer=hostile), Agent("claude", "m-claude")])

    async def scenario(client):
        resp = await client.post("/api/review", data=json.dumps({"question": "What is 17 × 23?"}), headers=JSON_AUTH)
        return await resp.text()

    raw = run_with_client(gui, scenario)  # JSON lines: a leaked control code would show up escaped
    assert "\\u001b" not in raw and "\\u202e" not in raw and "\\u0007" not in raw
    assert "391  gnirts done" in raw


def test_one_review_at_a_time():
    gui, _ = make_gui([Agent("gpt", "m-gpt", delay=0.5), Agent("claude", "m-claude", delay=0.5)])

    async def scenario(client):
        first = asyncio.create_task(client.post("/api/review", data=json.dumps({"question": "What is 17 × 23?"}),
                                                headers=JSON_AUTH))
        await asyncio.sleep(0.15)
        second = await client.post("/api/review", data=json.dumps({"question": "q2"}), headers=JSON_AUTH)
        first_resp = await first
        await first_resp.text()
        return second.status, (await second.json())["error"]

    assert run_with_client(gui, scenario) == (409, "A review is already running.")


def test_closing_the_page_cancels_model_calls():
    slow = [Agent("gpt", "m-gpt", delay=30), Agent("claude", "m-claude", delay=30)]
    gui, _ = make_gui(slow)

    async def scenario(client):
        resp = await client.post("/api/review", data=json.dumps({"question": "q"}), headers=JSON_AUTH)
        # The models are answering… (closing any earlier stops the connecting instead; see below)
        while json.loads(await resp.content.readline())["kind"] != "round":
            pass
        resp.close()                           # …then the tab was closed
        started = time.monotonic()
        while sum(a.cancelled for a in slow) < 2 and time.monotonic() - started < 5:
            await asyncio.sleep(0.05)
        elapsed = time.monotonic() - started
        # the lock was released, so a new review can start
        again = await client.post("/api/review", data=json.dumps({"question": "q"}), headers=JSON_AUTH)
        status = again.status
        again.close()
        return elapsed, status

    elapsed, status = run_with_client(gui, scenario)
    assert all(a.cancelled == 1 for a in slow)
    assert elapsed < 2 and status == 200
    assert all(not a.is_connected for a in slow)


class SlowToConnect:
    """An agent whose connect() takes `delay` seconds; records being disconnected."""

    def __init__(self, cfg, delay):
        self.name, self.label, self.delay = cfg.name, cfg.label, delay
        self.connected = self.disconnected = False

    async def connect(self):
        await asyncio.sleep(self.delay)
        self.connected = True

    async def disconnect(self):
        self.disconnected = True


def test_closing_the_page_while_models_connect_disconnects_them(monkeypatch):
    # The page closes while one model is still connecting: the one that already connected
    # used to stay connected, and the slow one kept going.
    from ixel_mat import runtime
    from ixel_mat.gui import server as gui_server

    made = {}

    def create(cfg):
        made[cfg.name] = SlowToConnect(cfg, 0 if cfg.name == "gpt" else 30)
        return made[cfg.name]

    monkeypatch.setattr(runtime, "create_agent", create)
    configs = {n: AgentConfig(name=n, label=n.title(), type="http", url="https://api.example.com",
                              token="sk", model="m") for n in ("gpt", "claude")}
    settings = Settings({}, configs, saver=SaverSettings())
    gui = GuiServer(token=TOKEN, settings_loader=lambda: settings,
                    connect=gui_server.connect_agents, disconnect=gui_server.disconnect_agents)

    async def scenario(client):
        resp = await client.post("/api/review", data=json.dumps({"question": "q"}), headers=JSON_AUTH)
        first = json.loads(await resp.content.readline())   # gpt connected; claude still connecting
        resp.close()
        started = time.monotonic()
        while not all(a.disconnected for a in made.values()) and time.monotonic() - started < 5:
            await asyncio.sleep(0.05)
        again = await client.post("/api/review", data=json.dumps({"question": "q"}), headers=JSON_AUTH)
        status = again.status
        again.close()
        return first, time.monotonic() - started, status

    first, elapsed, status = run_with_client(gui, scenario)
    assert first["kind"] == "connect" and first["data"]["agent"] == "gpt"
    assert made["gpt"].connected and made["gpt"].disconnected
    assert not made["claude"].connected and made["claude"].disconnected
    assert elapsed < 2 and status == 200


def test_connect_agents_cleans_up_when_cancelled(monkeypatch):
    from ixel_mat import runtime

    made = {}

    def create(cfg):
        made[cfg.name] = SlowToConnect(cfg, 0 if cfg.name == "fast" else 30)
        return made[cfg.name]

    monkeypatch.setattr(runtime, "create_agent", create)
    configs = {n: AgentConfig(name=n, label=n, type="http", url="https://x", token="t", model="m")
               for n in ("fast", "slow")}

    async def go():
        task = asyncio.create_task(runtime.connect_agents(configs))
        while not (made.get("fast") and made["fast"].connected):
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert made["fast"].disconnected and made["slow"].disconnected


def test_no_connected_agents_is_explained():
    gui, _ = make_gui([])

    async def scenario(client):
        resp = await client.post("/api/review", data=json.dumps({"question": "q"}), headers=JSON_AUTH)
        return await read_events(resp)

    events = run_with_client(gui, scenario)
    assert events == [{"kind": "error", "data": {"message": "No panel agents connected. Run `ixel setup`, "
                                                            "then `ixel agents` to check them."}}]


# ── Saver mode + saves counter ────────────────────────────────────────────────

def test_saver_mode_streams_a_saves_event(monkeypatch, tmp_path):
    monkeypatch.setattr(stats, "STATS_FILE", tmp_path / "stats.json")
    gui, _ = make_gui(saver=SaverSettings(verifier="claude"))

    async def scenario(client):
        panel = await (await client.get("/api/panel", headers=AUTH)).json()
        resp = await client.post("/api/review", data=json.dumps({"question": "What is 17 × 23?", "mode": "saver"}),
                                 headers=JSON_AUTH)
        events = await read_events(resp)
        saves = await (await client.get("/api/saves", headers=AUTH)).json()
        return panel, events, saves

    panel, events, saves = run_with_client(gui, scenario)
    assert panel["saver"] == {"verifier": "Claude", "drafters": ["Gpt", "Gemini"], "escalate": "always"}
    result = next(e for e in events if e["kind"] == "final")["data"]["result"]
    assert result["mode"] == "saver" and result["verifier_outcome"] == "confirmed"
    assert result["tier_calls"] == {"panel": 4, "verifier": 1}
    update = next(e for e in events if e["kind"] == "saves")["data"]
    assert update["saved"] and update["saves"] == 1 and update["unlocked"][0]["title"] == "First save"
    assert saves["saves"] == 1 and saves["leaderboard"][0]["model"] == "Gpt"


def test_saves_endpoint_needs_the_token():
    async def scenario(client):
        return (await client.get("/api/saves")).status
    assert run_with_client(make_gui()[0], scenario) == 401


def test_browser_is_opened_through_a_private_page_not_a_url_with_the_key(monkeypatch):
    # A URL on the browser launcher's command line is readable by other users of this computer
    import os
    import stat
    from pathlib import Path
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    from ixel_mat.config import secrets
    from ixel_mat.gui import server as gui_server

    opened, announced, keys = [], [], []
    monkeypatch.setenv("XAI_API_KEY", "xai-saved-in-ixel")
    monkeypatch.setattr(secrets, "_INJECTED", {"XAI_API_KEY"})  # loaded from what's saved
    monkeypatch.setattr(gui_server, "load_env", lambda: {"XAI_API_KEY": "xai-saved-in-ixel"})
    monkeypatch.setattr(gui_server.webbrowser, "open",
                        lambda target: keys.append(os.environ.get("XAI_API_KEY")) or opened.append(target))

    async def go():
        task = asyncio.create_task(gui_server.serve(announce=announced.append))
        for _ in range(100):
            if opened:
                break
            await asyncio.sleep(0.05)
        page = Path(url2pathname(urlparse(opened[0]).path))
        content = page.read_text(encoding="utf-8")
        mode = stat.S_IMODE(os.stat(page).st_mode)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return page, content, mode

    page, content, mode = asyncio.run(go())
    token = announced[0].split("#token=")[1]
    assert opened[0].startswith("file:") and token not in opened[0]
    assert keys == [None] and os.environ["XAI_API_KEY"] == "xai-saved-in-ixel"  # the browser gets none of them
    assert f"#token={token}" in content and 'http-equiv="refresh"' in content
    if os.name == "posix":
        assert mode == 0o600
    assert not page.exists()  # removed when the server stops


# ── Health ────────────────────────────────────────────────────────────────────

def test_health_needs_the_token_and_passes_check_now_on():
    asked = []

    async def fake_report(probe):
        asked.append(probe)
        return {"schema": 1, "groups": [{"id": "x", "title": "X", "checks": [
            {"id": "a", "label": "A\x1b[31m", "state": "ok", "detail": "‮fine", "fix": ""}]}]}

    gui = GuiServer(token=TOKEN, health_report=fake_report)

    async def scenario(client):
        none = await client.get("/api/health")
        plain = await client.get("/api/health", headers=AUTH)
        probed = await client.get("/api/health?probe=1", headers=AUTH)
        return none.status, plain.status, await plain.json(), probed.status

    none, plain, body, probed = run_with_client(gui, scenario)
    assert (none, plain, probed) == (401, 200, 200)
    assert asked == [False, True]
    check = body["groups"][0]["checks"][0]
    assert "\x1b" not in check["label"] and "‮" not in check["detail"]  # cleaned like everything else


def test_opening_health_sends_nothing_and_shows_no_key(monkeypatch):
    from ixel_mat import health

    async def no_programs(argv, timeout=0, env=None):
        raise AssertionError(f"ran {argv}")

    async def no_probes(cfg):
        raise AssertionError(f"probed {cfg.name}")

    monkeypatch.setattr(health, "run_program", no_programs)
    monkeypatch.setattr(health, "_default_probe_agent", no_probes)

    async def scenario(client):
        resp = await client.get("/api/health?probe=0", headers=AUTH)
        return resp.status, await resp.text()

    status, text = run_with_client(make_gui()[0], scenario)
    assert status == 200 and "sk-never-shown" not in text
    states = {c["id"]: c["state"] for g in json.loads(text)["groups"] for c in g["checks"]}
    assert states["agent-gpt"] == "unchecked"


def test_the_launch_page_goes_as_soon_as_the_page_has_loaded(monkeypatch):
    # It holds the session key: it shouldn't wait for Ixel to stop (a `kill` may not let it clean up)
    import aiohttp
    from pathlib import Path
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    from ixel_mat.gui import server as gui_server

    opened, announced = [], []
    monkeypatch.setattr(gui_server.webbrowser, "open", lambda target: opened.append(target))

    async def go():
        task = asyncio.create_task(gui_server.serve(announce=announced.append))
        for _ in range(100):
            if opened:
                break
            await asyncio.sleep(0.05)
        page = Path(url2pathname(urlparse(opened[0]).path))
        assert page.exists()
        base, token = announced[0].split("/#token=")
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base}/api/presence", headers={"Authorization": f"Bearer {token}"}):
                for _ in range(40):
                    if not page.exists():
                        break
                    await asyncio.sleep(0.05)
                gone = not page.exists()
                running = not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return gone, running

    gone, running = asyncio.run(go())
    assert gone and running


# ── Ask's conversations, kept for a reload ────────────────────────────────────

TAB = "a-tab-of-its-own-123456"
KEPT = json.dumps([{"title": "What is 17 x 23?", "turns": [{"question": "What is 17 x 23?", "mode": "quick"}]}])


def test_a_reload_gets_its_conversations_back_from_ixel_not_the_browser():
    gui, _ = make_gui()

    async def scenario(client):
        before = await client.get(f"/api/conversations?tab={TAB}", headers=AUTH)
        put = await client.put(f"/api/conversations?tab={TAB}", data=KEPT, headers=JSON_AUTH)
        after = await client.get(f"/api/conversations?tab={TAB}", headers=AUTH)
        other = await client.get("/api/conversations?tab=another-tab-entirely-7890", headers=AUTH)
        cleared = await client.put(f"/api/conversations?tab={TAB}", data="[]", headers=JSON_AUTH)
        gone = await client.get(f"/api/conversations?tab={TAB}", headers=AUTH)
        return (await before.json(), put.status, await put.json(), await after.text(), await other.json(),
                cleared.status, await gone.json(), dict(after.headers))

    before, put, said, after, other, cleared, gone, headers = run_with_client(gui, scenario)
    assert before == [] and put == 200 and said == {"kept": 1}
    assert json.loads(after) == json.loads(KEPT) and other == []  # each tab has only its own
    assert headers["Cache-Control"] == "no-store"
    assert cleared == 200 and gone == [] and len(gui.conversations) == 0


def test_conversations_need_the_token_and_a_same_origin_json_put():
    gui, _ = make_gui()

    async def scenario(client):
        url = f"/api/conversations?tab={TAB}"
        no_token = await client.get(url)
        put_no_token = await client.put(url, data=KEPT, headers={"Content-Type": "application/json"})
        foreign = await client.put(url, data=KEPT, headers={**JSON_AUTH, "Origin": "https://evil.example"})
        form = await client.put(url, data="a=b", headers={**AUTH, "Content-Type": "application/x-www-form-urlencoded"})
        return no_token.status, put_no_token.status, foreign.status, form.status

    assert run_with_client(gui, scenario) == (401, 401, 403, 415)
    assert len(gui.conversations) == 0


@pytest.mark.parametrize("tab, body, status", [
    ("short", KEPT, 400),
    ("../../etc/passwd-and-more", KEPT, 400),
    ("", KEPT, 400),
    (TAB, "not json", 400),
    (TAB, json.dumps({"title": "x"}), 400),
], ids=["tab-too-short", "tab-not-an-id", "no-tab", "not-json", "not-a-list"])
def test_conversations_are_checked(tab, body, status):
    gui, _ = make_gui()

    async def scenario(client):
        resp = await client.put(f"/api/conversations?tab={tab}", data=body, headers=JSON_AUTH)
        got = await client.get(f"/api/conversations?tab={tab}", headers=AUTH)
        return resp.status, got.status

    put, get = run_with_client(gui, scenario)
    assert put == status and len(gui.conversations) == 0
    assert get == (200 if gui_server.TAB_ID.fullmatch(tab) else 400)


def test_conversations_over_the_limit_are_refused_while_read(monkeypatch):
    # They take more than the 512 KB other requests may (a long conversation), but not without limit
    monkeypatch.setattr(gui_server, "MAX_CONVERSATION_BYTES", 600 * 1024)
    gui, _ = make_gui()
    big = json.dumps([{"title": "t", "turns": [{"question": "x" * (550 * 1024)}]}])
    too_big = json.dumps([{"title": "t", "turns": [{"question": "x" * (700 * 1024)}]}])

    async def scenario(client):
        ok = await client.put(f"/api/conversations?tab={TAB}", data=big, headers=JSON_AUTH)
        refused = await client.put(f"/api/conversations?tab={TAB}", data=too_big, headers=JSON_AUTH)
        return ok.status, refused.status, (await refused.json())["error"], gui.conversations.get(TAB)

    ok, refused, error, kept = run_with_client(gui, scenario)
    assert ok == 200 and refused == 413 and "MB" in error
    assert kept == big.encode()  # the last one that fitted


def test_only_so_many_tabs_are_kept_the_one_unused_longest_goes():
    kept = gui_server.Conversations(max_tabs=2)
    kept.put("tab-one", b"[1]")
    kept.put("tab-two", b"[2]")
    assert kept.get("tab-one") == b"[1]"  # used: now tab-two is the one unused longest
    kept.put("tab-three", b"[3]")
    assert (kept.get("tab-one"), kept.get("tab-two"), kept.get("tab-three")) == (b"[1]", None, b"[3]")
    assert gui_server.MAX_TABS * gui_server.MAX_CONVERSATION_BYTES <= 64 * 1024 * 1024  # at most 64 MB in all


def test_the_id_a_page_moved_away_from_goes_first_but_isnt_forgotten():
    kept = gui_server.Conversations(max_tabs=3)
    kept.put("another-tab", b"[0]")
    kept.put("before-reload", b"[1]")
    kept.put("after-reload", b"[1]", moved_from="before-reload")
    assert len(kept) == 3  # a duplicated tab may still be using its original's id
    kept.put("one-more-tab", b"[2]")
    assert kept.get("before-reload") is None  # it went before the tab unused longest
    assert (kept.get("another-tab"), kept.get("after-reload")) == (b"[0]", b"[1]")
    # The original of a duplicated tab saves again under its id: that makes it the newest again
    kept.put("duplicate", b"[3]", moved_from="original")  # an id that's gone already changes nothing
    kept.put("original", b"[4]")
    kept.put("duplicate-2", b"[4]", moved_from="original")
    kept.put("original", b"[5]")
    kept.put("last-tab", b"[6]")
    assert kept.get("original") == b"[5]" and kept.get("duplicate-2") == b"[4]"
    kept.put("same", b"[7]", moved_from="same")
    assert kept.get("same") == b"[7]"


def test_a_page_moving_its_conversations_names_the_id_it_had():
    gui, _ = make_gui()
    before, after = "id-before-the-reload-1", "id-after-the-reload-22"

    async def scenario(client):
        await client.put(f"/api/conversations?tab={before}", data=KEPT, headers=JSON_AUTH)
        bad = await client.put(f"/api/conversations?tab={after}&was=../x", data=KEPT, headers=JSON_AUTH)
        empty = await client.put(f"/api/conversations?tab={after}&was=", data=KEPT, headers=JSON_AUTH)
        moved = await client.put(f"/api/conversations?tab={after}&was={before}", data=KEPT, headers=JSON_AUTH)
        old = await client.get(f"/api/conversations?tab={before}", headers=AUTH)
        return bad.status, empty.status, moved.status, await old.json(), len(gui.conversations)

    bad, empty, moved, old, ids = run_with_client(gui, scenario)
    assert (bad, empty, moved) == (400, 400, 200)
    assert old == json.loads(KEPT) and ids == 2  # kept until it's the one to go


def test_conversations_are_gone_when_ixel_stops():
    gui, _ = make_gui()

    async def go():
        server = TestServer(gui.app(), host="127.0.0.1")
        async with TestClient(server) as client:
            gui.port = server.port
            await client.put(f"/api/conversations?tab={TAB}", data=KEPT, headers=JSON_AUTH)
            assert len(gui.conversations) == 1
    asyncio.run(go())
    assert len(gui.conversations) == 0


STATIC = Path(gui_server.__file__).parent / "static"


def test_the_page_keeps_no_question_or_answer_in_browser_storage():
    """Edge and Chrome may write a page's storage into a profile folder (in `ixel gui`, your own browser's): only
    the session key, this tab's id and the Board's folders may go there. Conversations live in Ixel's memory."""
    allowed = {"TOKEN_KEY", "TAB_KEY", "OLD_STORE_KEY", "RECENT_KEY", "HANDOFF_PROJECT_KEY"}
    for script in sorted(STATIC.glob("*.js")):
        code = script.read_text(encoding="utf-8")
        for api in ("indexedDB", "caches.", "document.cookie", "serviceWorker", "openDatabase"):
            assert api not in code, (script.name, api)
        for store, call, key in re.findall(r"\b(sessionStorage|localStorage)\.(\w+)\(([^,)]*)", code):
            assert key.strip() in allowed, (script.name, store, call, key)
            if store == "sessionStorage" and call == "setItem":
                assert key.strip() in ("TOKEN_KEY", "TAB_KEY"), (script.name, key)
    ask = (STATIC / "ask.js").read_text(encoding="utf-8")
    assert 'const OLD_STORE_KEY = "ixel-conversations"' in ask and "sessionStorage.removeItem(OLD_STORE_KEY)" in ask
    assert f"const MAX_KEPT_BYTES = {gui_server.MAX_CONVERSATION_BYTES // (1024 * 1024)} * 1024 * 1024;" in ask


def _attributes(source: str, start: int) -> str:
    """The attributes object of an el(...) call whose "{" is at or after start."""
    depth, i = 0, source.index("{", start)
    for j in range(i, len(source)):
        depth += {"{": 1, "}": -1}.get(source[j], 0)
        if depth == 0:
            return source[i:j + 1]
    raise AssertionError("unbalanced attributes")


def test_nothing_typed_is_spellchecked_suggested_or_remembered_by_the_browser():
    """In Edge, enhanced spell check and text predictions send what's typed to Microsoft, and autofill keeps it in the
    profile: every box that can hold a question, a key or a note says no to all three."""
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<body spellcheck="false" writingsuggestions="false">' in page  # and everything in it
    # Not on <html>: the server puts the saved Light or Dark there, by finding exactly this
    assert '<html lang="en">' in page
    boxes = re.findall(r"<(?:textarea|input)\b[^>]*>", page)
    typed = [b for b in boxes if 'type="file"' not in b]
    assert len(typed) >= 3
    for box in typed:
        assert 'spellcheck="false"' in box and 'autocomplete="off"' in box, box
    common = (STATIC / "common.js").read_text(encoding="utf-8")
    assert 'PRIVATE_TYPING = { spellcheck: "false", autocomplete: "off", writingsuggestions: "false" }' in common
    found = 0
    for script in sorted(STATIC.glob("*.js")):
        code = script.read_text(encoding="utf-8")
        for match in re.finditer(r'\bel\("(textarea|input)",', code):
            attrs = _attributes(code, match.end())
            kind = re.search(r'type: "(\w+)"', attrs)
            if match.group(1) == "input" and kind and kind.group(1) in ("checkbox", "radio", "file"):
                continue
            found += 1
            private = "...PRIVATE_TYPING" in attrs or ('spellcheck: "false"' in attrs and 'autocomplete: "off"' in attrs)
            assert private, (script.name, attrs)
    assert found >= 12
