"""
`ixel gui` — the review panel in your browser, served only to this machine.

Security model
- Listens on 127.0.0.1 only, on a random port by default.
- Every launch makes a fresh secret token. The browser gets it in the URL
  fragment (#token=…), which browsers never send over the network (opened
  through a private redirect file, so it isn't on a command line), and the
  page sends it back as a Bearer header on every API call. Other websites
  and other local programs can't drive the panel or spend API credits.
- Host header must be this server (blocks DNS rebinding); POSTs must be
  same-origin JSON (blocks cross-site form posts).
- Strict Content-Security-Policy: only this server's own script and
  styles, no inline code, no third-party anything. Model output is
  rendered with textContent only, never as HTML.
- Only a fixed list of static files is served. One review at a time.
- Ask's conversations come back after a reload from this server's memory
  (/api/conversations), never from the browser's storage, which Edge and
  Chrome may write into a profile folder. They're gone when Ixel stops.

`ixel app` serves the same page to a window of its own (see window.py) and
stops once no page has been open for a few seconds: each open page holds
a request to /api/presence open.
"""
from __future__ import annotations

import asyncio
import hmac
import html
import json
import logging
import math
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import webbrowser
from collections import OrderedDict
from contextlib import aclosing
from importlib import resources
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

import aiohttp
from aiohttp import web

from ixel_mat import __version__, pictures, sound, stats
from ixel_mat.agents.base import needs_api_key
from ixel_mat.modes.review import MAX_EARLIER_TURNS, MAX_PANEL, EarlierTurn, run_review
from ixel_mat.runtime import (MODE_CHOICES, choose_mode, connect_agents, disconnect_agents, load_settings,
                              local_agent_names)
from ixel_mat.config.secrets import keys_withheld, load_env, where_keys_are
from ixel_mat.material import Material, MaterialError, code_for_review
from ixel_mat.sanitize import sanitize_terminal_text

logger = logging.getLogger("ixel_mat.gui")

MAX_BODY_BYTES = 512 * 1024  # a question, earlier turns and attached code, JSON-escaped
MAX_QUESTION_CHARS = 50_000
MAX_EARLIER_CHARS = 20_000  # per earlier question or answer (the engine clips them further)
FIRST_PAGE_WAIT = 120.0  # `ixel app`: a browser's first start on a new profile can be slow
RELOAD_GRACE = 8.0       # a reloaded page is back well within this
HOST_PREFIX = "IXEL-URL "  # `ixel app --host`: the line a native window reads the address from

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/appearance.js": ("appearance.js", "text/javascript; charset=utf-8"),
    "/common.js": ("common.js", "text/javascript; charset=utf-8"),
    "/ask.js": ("ask.js", "text/javascript; charset=utf-8"),
    "/health.js": ("health.js", "text/javascript; charset=utf-8"),
    "/board.js": ("board.js", "text/javascript; charset=utf-8"),
    "/settings.js": ("settings.js", "text/javascript; charset=utf-8"),
    "/pictures.js": ("pictures.js", "text/javascript; charset=utf-8"),
    "/sound.js": ("sound.js", "text/javascript; charset=utf-8"),
    "/video.js": ("video.js", "text/javascript; charset=utf-8"),
    "/machines.js": ("machines.js", "text/javascript; charset=utf-8"),
    "/markdown.js": ("markdown.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/mark.svg": ("mark.svg", "image/svg+xml"),
}

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src blob:; "
       "connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}


def _clean(value: Any) -> Any:
    """Strip control codes and bidi overrides from every string going to the page."""
    if isinstance(value, str):
        return sanitize_terminal_text(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _sound_now(config: dict) -> dict:
    """For the page: the service sound goes to (name and label), or what's needed first."""
    provider, problem = sound.ready(config)
    return _clean({"service": provider.label, "name": provider.name, "problem": ""} if provider
                  else {"service": "", "name": "", "problem": problem})


class _Earlier(list):
    """A follow-up's earlier turns, and whether any was asked with Private on."""
    private = False


def _parse_earlier(value: Any) -> _Earlier | None:
    """A follow-up's earlier questions and answers, as the page sends them; None if malformed."""
    turns = _Earlier()
    if value is None:
        return turns
    if not isinstance(value, list) or len(value) > MAX_EARLIER_TURNS:
        return None
    for item in value:
        if not isinstance(item, dict):
            return None
        question, answer, private = item.get("question"), item.get("answer"), item.get("private", False)
        if not (isinstance(question, str) and isinstance(answer, str) and isinstance(private, bool)
                and len(question) <= MAX_EARLIER_CHARS and len(answer) <= MAX_EARLIER_CHARS):
            return None
        turns.append(EarlierTurn(question, answer))
        turns.private = turns.private or private
    return turns


class Presence:
    """How many pages are open now, and for how long there have been none."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.pages = 0
        self.seen = False
        self._empty_since = clock()

    def arrive(self) -> None:
        self.pages += 1
        self.seen = True

    def leave(self) -> None:
        self.pages -= 1
        if not self.pages:
            self._empty_since = self._clock()

    def empty_for(self) -> float:
        return 0.0 if self.pages else self._clock() - self._empty_since


# Ask's conversations, kept for a reload of the page (see Conversations)
CONVERSATIONS_PATH = "/api/conversations"
MAX_CONVERSATION_BYTES = 4 * 1024 * 1024  # one tab's, as JSON (ask.js drops its oldest conversations to fit)
MAX_TABS = 16                             # ids whose conversations are kept (each page load has its own)
TAB_ID = re.compile(r"[A-Za-z0-9_-]{16,64}")


class Conversations:
    """
    Each tab's conversations in Ask, by a random id the page made up for itself, so a reload brings them back.
    Only in this server's memory: the page used to keep them in the browser's sessionStorage, which Edge and
    Chrome may write into a profile folder (and in `ixel gui` that's your own browser's). Gone when Ixel stops.

    A page takes a new id each time it loads and moves its conversations to it (a duplicated tab has a copy of
    its original's id). When more than max_tabs ids are kept, the ids pages moved away from go first, then the
    one unused longest.
    """

    def __init__(self, max_tabs: int = MAX_TABS):
        self.max_tabs = max_tabs
        self._tabs: OrderedDict[str, bytes] = OrderedDict()  # the next to go first

    def get(self, tab: str) -> bytes | None:
        data = self._tabs.get(tab)
        if data is not None:
            self._tabs.move_to_end(tab)
        return data

    def put(self, tab: str, data: bytes, moved_from: str = "") -> None:
        self._tabs[tab] = data
        self._tabs.move_to_end(tab)
        # Not forgotten now: a duplicated tab may still be using it, and its next save makes it the newest again
        if moved_from != tab and moved_from in self._tabs:
            self._tabs.move_to_end(moved_from, last=False)
        while len(self._tabs) > self.max_tabs:
            self._tabs.popitem(last=False)

    def forget(self, tab: str) -> None:
        self._tabs.pop(tab, None)

    def clear(self) -> None:
        self._tabs.clear()

    def __len__(self) -> int:
        return len(self._tabs)


async def until_closed(presence: Presence, *, first_wait: float = FIRST_PAGE_WAIT, grace: float = RELOAD_GRACE,
                       on_first_page: Callable[[], None] = lambda: None, tick: float = 0.25) -> None:
    """Return once no page has been open for grace seconds (or first_wait, if none ever was)."""
    told = False
    while True:
        await asyncio.sleep(tick)
        if presence.seen and not told:
            told = True
            on_first_page()
        if presence.empty_for() >= (grace if presence.seen else first_wait):
            return


def _json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


# A picture is sent as itself (the page has already made it a PNG or JPEG); everything else is JSON
PICTURE_TYPES = ("image/png", "image/jpeg")
PICTURES_PATH = "/api/pictures"
SOUND_PATH = "/api/sound"  # sound to write out: sent as it is, whatever kind (sound.py checks)
TOO_BIG = f"That picture is over {pictures.megabytes(pictures.MAX_BYTES)}, even made smaller."


# POST /api/machines/<action> → the method of machines_api.Machines that does it (all blocking)
MACHINE_ACTIONS = {"save": "save", "delete": "delete", "import": "bring_in", "key": "check_key", "trust": "trust",
                   "forget": "forget", "connect": "connect", "copy-key": "copy_key", "new-key": "new_key"}


class GuiServer:
    def __init__(
        self,
        *,
        port: int = 0,
        token: str | None = None,
        # Requests never wait for the keychain or another save (secrets.load_env)
        settings_loader: Callable = lambda: load_settings(wait=False),
        connect: Callable[..., Awaitable[dict]] = connect_agents,
        disconnect: Callable[..., Awaitable[None]] = disconnect_agents,
        health_report: Callable[[bool], Awaitable[dict]] | None = None,
        handoff_command: Callable[[], list[str] | None] | None = None,
    ):
        self.port = port
        self.token = token or secrets.token_urlsafe(32)
        self._load_settings = settings_loader
        self._connect = connect
        self._disconnect = disconnect
        self._health_report = health_report or self._default_health
        self._health_lock = asyncio.Lock()  # "Check now" twice runs the checks once at a time
        self._lock = asyncio.Lock()
        self._handoff_lock = asyncio.Lock()  # one dispatch at a time (its runs go side by side)
        self._pull_lock = asyncio.Lock()     # one model downloading at a time
        self._board_lock = asyncio.Lock()    # the Board's changes go to Handoff one at a time
        self._settings_lock = asyncio.Lock()  # Settings changes the file one edit at a time
        self._machines_lock = asyncio.Lock()  # Machines changes its files one at a time
        from ixel_mat.gui import machines_api
        self.machines = machines_api.Machines()
        self.pictures = pictures.PictureStore()  # attached to questions: in memory only, gone when Ixel stops
        self.conversations = Conversations()     # Ask's, for a reload: in memory only, too
        from ixel_mat.gui import handoff_api
        self._handoff_command = handoff_command or handoff_api.handoff_command
        self._board_watch = handoff_api.BoardWatch()
        self._board_hello: dict | None = None
        self._static = resources.files("ixel_mat.gui") / "static"
        self.presence = Presence()
        self._stopping = False
        self._reviews: set[Callable[[], None]] = set()  # how to stop each review that's running

    # ── plumbing ──────────────────────────────────────────────────────────────

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/#token={self.token}"

    def app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_BODY_BYTES, middlewares=[self._guard])
        app.on_response_prepare.append(self._add_security_headers)
        app.on_shutdown.append(self._end_presence)
        app.on_shutdown.append(self._stop_runs)
        app.on_shutdown.append(self._stop_reviews)
        app.on_cleanup.append(self._forget_what_was_asked)
        for path in STATIC_FILES:
            app.router.add_get(path, self._serve_static)
        app.router.add_get("/api/panel", self._panel)
        app.router.add_get(CONVERSATIONS_PATH, self._conversations)
        app.router.add_put(CONVERSATIONS_PATH, self._keep_conversations)
        app.router.add_get("/api/saves", self._saves)
        app.router.add_get("/api/health", self._health)
        app.router.add_get("/api/presence", self._presence)
        app.router.add_post("/api/review", self._review)
        app.router.add_post(PICTURES_PATH, self._add_picture)
        app.router.add_post(SOUND_PATH, self._sound)
        app.router.add_get("/api/handoff", self._handoff_info)
        app.router.add_post("/api/handoff/plan", self._handoff_plan)
        app.router.add_post("/api/handoff/run", self._handoff_run)
        app.router.add_get("/api/board/hello", self._board_hello_route)
        app.router.add_get("/api/board", self._board)
        app.router.add_get("/api/board/projects", self._board_projects)
        app.router.add_get("/api/board/task", self._board_task)
        app.router.add_get("/api/board/output", self._board_output)
        app.router.add_get("/api/board/agents", self._board_agents)
        app.router.add_post("/api/board/action", self._board_action)
        app.router.add_get("/api/connections", self._connections)
        app.router.add_post("/api/connections/host", self._connections_host)
        app.router.add_post("/api/connections/token", self._connections_token)
        app.router.add_post("/api/connections/review", self._connections_review)
        app.router.add_post("/api/connections/fix", self._connections_fix)
        app.router.add_get("/api/machines", self._machines)
        app.router.add_get("/api/machines/run", self._machines_run_state)
        app.router.add_post("/api/machines/run", self._machines_run)
        app.router.add_post("/api/machines/run/stop", self._machines_run_stop)
        for action in MACHINE_ACTIONS:
            app.router.add_post(f"/api/machines/{action}", self._machines_action)
        app.router.add_get("/api/settings", self._settings)
        app.router.add_get("/api/settings/models", self._settings_models)
        app.router.add_post("/api/settings", self._settings_change)
        app.router.add_post("/api/settings/key", self._settings_key)
        app.router.add_post("/api/settings/servers", self._settings_servers)
        app.router.add_post("/api/settings/servers/pull", self._settings_pull)
        app.router.add_get("/api/appearance", self._appearance)
        app.router.add_post("/api/appearance", self._appearance_change)
        app.router.add_post("/api/docs", self._docs)
        app.router.add_post("/api/forget", self._forget)
        return app

    def _allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def _allowed_origins(self) -> set[str]:
        return {f"http://{h}" for h in self._allowed_hosts()}

    def _token_ok(self, header: str) -> bool:
        scheme, _, supplied = header.partition(" ")
        return scheme == "Bearer" and hmac.compare_digest(supplied.encode(), self.token.encode())

    @web.middleware
    async def _guard(self, request: web.Request, handler):
        if request.host not in self._allowed_hosts():
            return web.Response(status=403, text="Forbidden host")
        if request.path.startswith("/api/"):
            if not self._token_ok(request.headers.get("Authorization", "")):
                return _json_error(401, "Missing or wrong session token. Reopen the link `ixel gui` printed.")
            if request.method != "GET":
                origin = request.headers.get("Origin")
                if origin is not None and origin not in self._allowed_origins():
                    return _json_error(403, "Cross-origin request refused.")
                if request.path == PICTURES_PATH:
                    if request.content_type not in PICTURE_TYPES:
                        return _json_error(415, "Expected a PNG or JPEG picture.")
                elif request.path == SOUND_PATH:
                    # (aiohttp calls a request with no Content-Type octet-stream too: the page always says)
                    if "Content-Type" not in request.headers or request.content_type != "application/octet-stream":
                        return _json_error(415, "Expected the sound as application/octet-stream.")
                elif request.content_type != "application/json":
                    return _json_error(415, "Expected application/json.")
        return await handler(request)

    @staticmethod
    async def _add_security_headers(request: web.Request, response: web.StreamResponse) -> None:
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)

    async def _serve_static(self, request: web.Request) -> web.Response:
        name, content_type = STATIC_FILES[request.path]
        body = (self._static / name).read_bytes()
        if name == "index.html":
            from ixel_mat.gui import appearance
            body = appearance.into_page(body, await asyncio.to_thread(appearance.load))
        return web.Response(body=body, headers={"Content-Type": content_type})

    # ── API ───────────────────────────────────────────────────────────────────

    async def _presence(self, request: web.Request) -> web.StreamResponse:
        """Held open by each page for as long as it's open: `ixel app` stops once none is."""
        response = web.StreamResponse(headers={"Content-Type": "text/plain; charset=utf-8"})
        await response.prepare(request)
        self.presence.arrive()
        try:
            beat = 0
            while True:
                await asyncio.sleep(0.25)
                transport = request.transport
                if self._stopping or transport is None or transport.is_closing():
                    break
                beat += 1
                if beat % 60 == 0:  # every 15 s: a connection that died quietly fails the write
                    await response.write(b"\n")
        except (ConnectionError, RuntimeError):
            pass
        finally:
            self.presence.leave()
        return response

    async def _end_presence(self, app: web.Application) -> None:
        # On Ctrl+C, open pages mustn't keep the server waiting for them
        self._stopping = True

    async def _forget_what_was_asked(self, app: web.Application) -> None:
        # Pictures and conversations are only ever in memory: they go with the server
        self.pictures.clear()
        self.conversations.clear()

    async def _stop_runs(self, app: web.Application) -> None:
        # Commands still running on your machines stop with Ixel
        await self.machines.runs.close()

    async def _stop_reviews(self, app: web.Application) -> None:
        # A review still running stops with Ixel, and so do the model programs it started, at once:
        # the server would otherwise wait for it to finish before stopping
        for stop in list(self._reviews):
            stop()

    async def _panel(self, request: web.Request) -> web.Response:
        settings = self._load_settings()
        agents = []
        for cfg in settings.panel_configs().values():
            agents.append({
                "name": cfg.name, "label": cfg.label, "type": cfg.type,
                "model": cfg.model or cfg.command or "",
                "ready": not (needs_api_key(cfg) and not cfg.token),
                "pictures": cfg.sees_pictures,
            })
        saver_configs = settings.saver_configs()
        verifier = settings.verifier
        triage = settings.active_triage
        return web.json_response(_clean({
            "version": __version__,
            "agents": agents[:MAX_PANEL],
            "review": {"mode": settings.default_mode, "moderator": settings.moderator},
            # Private: only models on your own computers answer; these are the panel's others
            "private": {"on": settings.private, "sitting_out": settings.sitting_out()[:MAX_PANEL]},
            "saver": {
                "verifier": settings.agent_configs[verifier].label if verifier in settings.agent_configs else None,
                "drafters": [c.label for n, c in saver_configs.items() if n != verifier],
                "escalate": settings.saver.escalate,
            },
            "triage": {
                "ready": triage.ready, "auto": triage.can_pick_mode,
                "skip_review": triage.ready and triage.skip_review,
                "saver_gate": triage.ready and triage.saver_gate,
                "provider": triage.provider,
                "via": triage.via if triage.enabled else "",
                "host": triage.host if triage.enabled and triage.provider == "typesafe" else "",
                "official": triage.enabled and triage.official,
            },
            "sound": _sound_now(settings.config),
            "warnings": settings.warnings,
        }))

    async def _saves(self, request: web.Request) -> web.Response:
        return web.json_response(_clean(stats.summary(stats.load_stats())))

    async def _default_health(self, probe: bool) -> dict:
        from ixel_mat import health
        return await health.report(probe, settings_loader=self._load_settings)

    async def _health(self, request: web.Request) -> web.Response:
        """The Health page: probe=1 also asks each model and program whether it answers."""
        probe = request.query.get("probe") == "1"
        if not probe:  # quick: never waits behind a Check now that's under way
            return web.json_response(_clean(await self._health_report(False)))
        async with self._health_lock:  # one Check now at a time
            return web.json_response(_clean(await self._health_report(True)))

    # Settings: choices among what's set up, and write-only keys (see settings_api.py)

    async def _settings_snapshot(self) -> dict:
        from ixel_mat.config import edit
        from ixel_mat.gui import settings_api

        def read() -> dict:
            # The file's version first, then the settings: a change in between makes the page's next edit
            # be refused (and shown), rather than applied to settings the page never saw
            path = settings_api.settings_path(self._load_settings())
            try:
                version = edit.file_version(path) if path else None
            except OSError:
                version = None
            return settings_api.snapshot(self._load_settings(), version)
        return _clean(await asyncio.to_thread(read))

    async def _settings(self, request: web.Request) -> web.Response:
        return web.json_response(await self._settings_snapshot())

    async def _settings_models(self, request: web.Request) -> web.Response:
        """Each model's list to pick from, from its company when Ixel can ask (see model_choices.py)."""
        from ixel_mat.gui import model_choices
        fresh = request.query.get("fresh") == "1"
        found = await asyncio.to_thread(lambda: model_choices.choices(self._load_settings(), fresh))
        return web.json_response(_clean({"agents": found}))

    async def _settings_write(self, request: web.Request, write) -> web.Response:
        from ixel_mat.gui import settings_api
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _json_error(400, "Body must be JSON.")
        async with self._settings_lock:
            try:
                settings = await asyncio.to_thread(self._load_settings)
                result = await asyncio.to_thread(write, settings, body)
            except settings_api.SettingsError as exc:
                return web.json_response({"error": _clean(str(exc)), "settings": await self._settings_snapshot()},
                                         status=exc.status)
            except OSError as exc:
                return _json_error(500, f"Couldn't save: {exc.strerror or exc}")
            return web.json_response({**_clean(result or {}), "settings": await self._settings_snapshot()})

    async def _settings_change(self, request: web.Request) -> web.Response:
        from ixel_mat.gui import settings_api
        return await self._settings_write(request, settings_api.change)

    async def _settings_key(self, request: web.Request) -> web.Response:
        from ixel_mat.gui import settings_api
        return await self._settings_write(request, settings_api.set_key)

    async def _settings_servers(self, request: web.Request) -> web.Response:
        """Model servers on this computer, or at the address typed (nothing is saved)."""
        from ixel_mat.gui import settings_api
        body = await self._handoff_body(request)
        if isinstance(body, web.Response):
            return body
        try:
            found = await asyncio.to_thread(lambda: settings_api.look_for_servers(self._load_settings(), body))
        except settings_api.SettingsError as exc:
            return _json_error(exc.status, str(exc))
        return web.json_response(_clean(found))

    async def _settings_pull(self, request: web.Request) -> web.StreamResponse:
        """Has an Ollama of yours download a model from ollama.com; streams how far along it is (NDJSON:
        progress, then done or error). Closing the page stops the download."""
        from ixel_mat.gui import settings_api
        from ixel_mat import local_models
        body = await self._handoff_body(request)
        if isinstance(body, web.Response):
            return body
        if self._pull_lock.locked():
            return _json_error(409, "Ixel is already getting a model. Wait for it to finish, then try again.")
        async with self._pull_lock:
            try:
                root, name = await asyncio.to_thread(settings_api.plan_pull, body)
            except settings_api.SettingsError as exc:
                return _json_error(exc.status, str(exc))
            response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson; charset=utf-8"})
            await response.prepare(request)
            gone = False

            async def send(kind: str, data: dict) -> bool:
                nonlocal gone
                if not gone:
                    try:
                        await response.write((json.dumps({"kind": kind, "data": _clean(data)}) + "\n").encode())
                    except (ConnectionError, RuntimeError):
                        gone = True  # the page closed
                return not gone

            async def relay() -> None:
                shown, said = 0.0, None
                async with aclosing(local_models.pull(root, name)) as steps:
                    async for step in steps:
                        # A few a second is plenty for a progress line, but each new step shows (verifying…)
                        if time.monotonic() - shown >= 0.25 or step["status"] != said:
                            shown, said = time.monotonic(), step["status"]
                            if not await send("progress", step):
                                return  # closing the connection to Ollama stops its download too
                await send("done", {"model": name})

            async def page_closed() -> None:
                # Ollama says nothing for minutes while it checks a big download: notice a closed page anyway
                while not gone:
                    await asyncio.sleep(0.25)
                    if request.transport is None or request.transport.is_closing():
                        return

            work, watch = asyncio.ensure_future(relay()), asyncio.ensure_future(page_closed())
            try:
                await asyncio.wait({work, watch}, return_when=asyncio.FIRST_COMPLETED)
                if not work.done():
                    return response  # the page closed (finally stops the pull)
                work.result()
            except local_models.PullError as exc:
                await send("error", {"message": str(exc)})
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                await send("error", {"message": f"Lost touch with Ollama while it got {name} "
                                                f"({str(exc) or type(exc).__name__}). Get it again to carry on "
                                                "from where it stopped."})
            except Exception as exc:  # anything else Ollama's answer could cause (a line too long to read)
                await send("error", {"message": f"Ollama's answer couldn't be read while it got {name} "
                                                f"({type(exc).__name__}). Get it again to carry on."})
            finally:
                watch.cancel()
                if not work.done():
                    work.cancel()  # closes the connection to Ollama, which stops its download
                await asyncio.gather(work, watch, return_exceptions=True)
            return response

    # Appearance: System, Light or Dark (see appearance.py)

    async def _appearance(self, request: web.Request) -> web.Response:
        from ixel_mat.gui import appearance
        return web.json_response({"appearance": await asyncio.to_thread(appearance.load)})

    async def _appearance_change(self, request: web.Request) -> web.Response:
        from ixel_mat.gui import appearance
        body = await self._handoff_body(request)
        if isinstance(body, web.Response):
            return body
        try:
            choice = await asyncio.to_thread(appearance.save, body.get("appearance"))
        except ValueError as exc:
            return _json_error(400, str(exc))
        except OSError as exc:
            return _json_error(500, f"Couldn't save it: {exc}")
        return web.json_response({"appearance": choice})

    async def _docs(self, request: web.Request) -> web.Response:
        """Settings' Docs button. The server opens them, in your own browser: a link in Ixel's window would
        open the website in the window itself (the Mac app's) or in its private browser profile (Windows')."""
        from ixel_mat import docs
        return web.json_response({"opened": await asyncio.to_thread(docs.open_docs), "url": docs.DOCS_URL})

    async def _forget(self, request: web.Request) -> web.Response:
        """Settings' Forget button: deletes the review conversation and the Machines log. Not the window's
        storage, which this window is using (`ixel forget` does that, with Ixel closed)."""
        from ixel_mat.forget import forget
        found = await asyncio.to_thread(forget, window=False)
        return web.json_response(_clean({"forgotten": [item.to_dict() for item in found]}))

    # /handoff: one request split across agents, through Handoff (see handoff.py)

    async def _handoff_info(self, request: web.Request) -> web.Response:
        from ixel_mat.gui import handoff
        return web.json_response(_clean({"installed": handoff.find_handoff() is not None,
                                         "project": handoff.default_project(), "install": handoff.how_to_install()}))

    async def _handoff_body(self, request: web.Request) -> dict | web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _json_error(400, "Body must be JSON.")
        return body if isinstance(body, dict) else _json_error(400, "Body must be a JSON object.")

    def _private_handoff(self) -> web.Response | None:
        """Handoff's agents (Claude Code, Codex…) send the work to their companies: not under Private."""
        if self._load_settings().private:
            return _json_error(403, "Private is on, and /handoff gives your request to coding agents that send it to "
                                    "their companies. Turn Private off in Settings, under Asking, to hand it over.")
        return None

    async def _handoff_plan(self, request: web.Request) -> web.Response:
        from ixel_mat.gui import handoff
        body = await self._handoff_body(request)
        if isinstance(body, web.Response):
            return body
        if refused := await asyncio.to_thread(self._private_handoff):
            return refused
        try:
            return web.json_response(_clean(await handoff.dispatch(body.get("project"), body.get("request"),
                                                                   plan=True)))
        except handoff.HandoffError as exc:
            return _json_error(400, str(exc))

    async def _handoff_run(self, request: web.Request) -> web.Response:
        from ixel_mat.gui import handoff
        body = await self._handoff_body(request)
        if isinstance(body, web.Response):
            return body
        if refused := await asyncio.to_thread(self._private_handoff):
            return refused
        if self._handoff_lock.locked():
            return _json_error(409, "A handoff is already running; its results land on the board.")
        async with self._handoff_lock:
            try:
                # Shielded: a closed page doesn't stop agents that are already working
                return web.json_response(_clean(await asyncio.shield(
                    handoff.dispatch(body.get("project"), body.get("request"), plan=False))))
            except handoff.HandoffError as exc:
                return _json_error(400, str(exc))

    # The Board: Handoff's board for a project, through `handoff api` (see handoff_api.py)

    BOARD_STATUS = {"usage": 400, "no_project": 400, "invalid": 400, "not_installed": 404, "outdated": 404,
                    "no_board": 404, "not_found": 404, "forbidden": 403, "busy": 409, "changed": 409, "timeout": 504}

    def _board_error(self, exc) -> web.Response:
        return web.json_response(_clean({"error": str(exc), "code": exc.code}),
                                 status=self.BOARD_STATUS.get(exc.code, 502))

    async def _handoff_call(self, op: str, root: Path | None = None, args: dict | None = None) -> dict:
        from ixel_mat.gui import handoff_api
        command = await asyncio.to_thread(self._handoff_command)
        if command is None:
            raise handoff_api.HandoffApiError("not_installed", "Handoff isn't installed on this computer.")
        return await asyncio.to_thread(handoff_api.call, op, root, args, command)

    async def _board_hello_route(self, request: web.Request) -> web.Response:
        """Is there a Handoff that has the Board's door? Asked once, then remembered."""
        from ixel_mat.gui import handoff, handoff_api
        if self._board_hello is None or not self._board_hello.get("ok"):
            try:
                hello = await self._handoff_call("hello")
                self._board_hello = {"ok": handoff_api.version_ok(hello), "version": hello.get("handoff_version", "")}
                if not self._board_hello["ok"]:
                    self._board_hello["problem"] = "outdated"
            except handoff_api.HandoffApiError as exc:
                self._board_hello = {"ok": False, "version": "", "problem": exc.code, "message": str(exc)}
        update = "handoff update" if self._board_hello.get("problem") == "outdated" else ""
        return web.json_response(_clean({**self._board_hello, "install": handoff.how_to_install(), "update": update,
                                         "project": handoff.default_project()}))

    async def _board_projects(self, request: web.Request) -> web.Response:
        """Git repositories on this computer, for the Board to offer before a project is picked."""
        from ixel_mat.gui import handoff_api
        return web.json_response(_clean({"projects": await asyncio.to_thread(handoff_api.find_projects)}))

    async def _board(self, request: web.Request) -> web.Response:
        from ixel_mat.gui import handoff_api
        since = request.query.get("since", "")
        try:
            root = handoff_api.check_project(request.query.get("project"))
            if since and self._board_watch.unchanged(root, since):
                return web.json_response({"unchanged": True, "revision": since})
            signature = handoff_api.board_signature(root)  # before reading: a write meanwhile shows next time
            data = await self._handoff_call("board", root)
        except handoff_api.HandoffApiError as exc:
            return self._board_error(exc)
        if not data.get("exists"):
            data["revision"] = handoff_api.NO_BOARD
        self._board_watch.remember(root, signature, str(data.get("revision", "")))
        return web.json_response(_clean(data))

    async def _board_task(self, request: web.Request) -> web.Response:
        from ixel_mat.gui import handoff_api
        try:
            root = handoff_api.check_project(request.query.get("project"))
            return web.json_response(_clean(await self._handoff_call("task", root,
                                                                     {"task": request.query.get("task", "")})))
        except handoff_api.HandoffApiError as exc:
            return self._board_error(exc)

    async def _board_agents(self, request: web.Request) -> web.Response:
        from ixel_mat.gui import handoff_api
        try:
            root = handoff_api.check_project(request.query.get("project"))
            return web.json_response(_clean(await self._handoff_call("agents", root)))
        except handoff_api.HandoffApiError as exc:
            return self._board_error(exc)

    async def _board_output(self, request: web.Request) -> web.Response:
        """A picture or answer a run left in .handoff/outputs/T-N, for the page to show."""
        from ixel_mat.gui import handoff_api
        try:
            root = handoff_api.check_project(request.query.get("project"))
            data, kind = await asyncio.to_thread(handoff_api.read_output, root, request.query.get("task"),
                                                 request.query.get("name"))
        except handoff_api.HandoffApiError as exc:
            return self._board_error(exc)
        return web.Response(body=data, headers={"Content-Type": kind, "Content-Disposition": "attachment"})

    async def _board_action(self, request: web.Request) -> web.Response:
        """A change the person made on the Board: Handoff acts as them, under the board's rules."""
        from ixel_mat.gui import handoff_api
        body = await self._handoff_body(request)
        if isinstance(body, web.Response):
            return body
        op, args = body.get("op"), body.get("args", {})
        if op not in handoff_api.WRITE_OPS or not isinstance(args, dict):
            return _json_error(400, "That isn't something the Board can do.")
        try:
            root = handoff_api.check_project(body.get("project"))
        except handoff_api.HandoffApiError as exc:
            return self._board_error(exc)
        try:
            async with self._board_lock:
                data = await self._handoff_call(op, root, args)
        except handoff_api.HandoffApiError as exc:
            return self._board_error(exc)
        finally:
            self._board_watch.forget(root)  # the next look reads the board again
        return web.json_response(_clean(data))

    # The Board's pull requests: the project's origin, read through its host's API (see connections_api.py)

    async def _connections(self, request: web.Request) -> web.Response:
        from ixel_mat.gui import connections_api, handoff_api
        try:
            settings = await asyncio.to_thread(self._load_settings)
            return web.json_response(_clean(await asyncio.to_thread(connections_api.look, settings,
                                                                     request.query.get("project"))))
        except handoff_api.HandoffApiError as exc:
            return self._board_error(exc)

    async def _connections_write(self, request: web.Request, write) -> web.Response:
        from ixel_mat.gui import connections_api, handoff_api
        body = await self._handoff_body(request)
        if isinstance(body, web.Response):
            return body
        try:
            async with self._settings_lock:
                settings = await asyncio.to_thread(self._load_settings)
                return web.json_response(_clean(await asyncio.to_thread(write, settings, body)))
        except handoff_api.HandoffApiError as exc:
            return self._board_error(exc)
        except connections_api.ConnectionsApiError as exc:
            return _json_error(exc.status, _clean(str(exc)))
        except OSError as exc:
            return _json_error(500, f"Couldn't save: {exc.strerror or exc}")

    async def _connections_host(self, request: web.Request) -> web.Response:
        from ixel_mat.gui import connections_api
        return await self._connections_write(request, connections_api.set_host)

    async def _connections_token(self, request: web.Request) -> web.Response:
        from ixel_mat.gui import connections_api
        return await self._connections_write(request, connections_api.set_token)

    async def _connections_review(self, request: web.Request) -> web.Response:
        """Fetch a pull request into the project, and have an agent review exactly its commits now."""
        from ixel_mat.gui import connections_api
        return await self._connections_run(request, connections_api.plan_review)

    async def _connections_fix(self, request: web.Request) -> web.Response:
        """Fetch a pull request into the project, and have Claude or Codex change it, from its last commit."""
        from ixel_mat.gui import connections_api
        return await self._connections_run(request, connections_api.plan_fix)

    async def _connections_run(self, request: web.Request, plan) -> web.Response:
        from ixel_mat.gui import connections_api, handoff_api
        body = await self._handoff_body(request)
        if isinstance(body, web.Response):
            return body
        command = await asyncio.to_thread(self._handoff_command)
        if command is None:
            return self._board_error(handoff_api.HandoffApiError("not_installed", "Handoff isn't installed on this "
                                                                                  "computer."))

        def handoff(op: str, root: Path | None, args: dict) -> dict:
            return handoff_api.call(op, root, args, command)
        try:
            # Listing and fetching can take a while (a slow host, a big fetch): the board stays usable meanwhile
            settings = await asyncio.to_thread(self._load_settings)
            planned = await asyncio.to_thread(plan, settings, body)
            async with self._board_lock:
                return web.json_response(_clean(await asyncio.to_thread(connections_api.start, planned, handoff)))
        except handoff_api.HandoffApiError as exc:
            return self._board_error(exc)
        except connections_api.ConnectionsApiError as exc:
            return _json_error(exc.status, _clean(str(exc)))
        finally:
            self._board_watch.forget_all()

    # ── Machines ──────────────────────────────────────────────────────────────

    @staticmethod
    def _machines_error(exc) -> web.Response:
        said = {"error": _clean(str(exc))}
        if exc.code:
            said["code"] = exc.code
        said.update(_clean(exc.extra))
        return web.json_response(said, status=exc.status)

    async def _machines_body(self, request: web.Request) -> Any:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _json_error(400, "Body must be JSON.")
        if not isinstance(body, dict):
            return _json_error(400, "Body must be a JSON object.")
        return body

    async def _machines(self, request: web.Request) -> web.Response:
        """Your machines, where ssh takes each one, and whether its key is pinned."""
        return web.json_response(_clean(await asyncio.to_thread(self.machines.overview)))

    async def _machines_action(self, request: web.Request) -> web.Response:
        from ixel_mat.gui import machines_api
        body = await self._machines_body(request)
        if isinstance(body, web.Response):
            return body
        action = getattr(self.machines, MACHINE_ACTIONS[request.path.rsplit("/", 1)[1]])
        try:
            if action.__name__ in ("check_key", "connect", "copy_key"):  # ssh may take a while: nothing to lock
                return web.json_response(_clean(await asyncio.to_thread(action, body)))
            async with self._machines_lock:
                return web.json_response(_clean(await asyncio.to_thread(action, body)))
        except machines_api.MachinesApiError as exc:
            return self._machines_error(exc)
        except OSError as exc:
            return _json_error(500, f"Couldn't save: {exc.strerror or exc}")

    async def _machines_run(self, request: web.Request) -> web.Response:
        """Run one command you typed on the machines you picked (it goes on after this answers)."""
        from ixel_mat.gui import machines_api
        body = await self._machines_body(request)
        if isinstance(body, web.Response):
            return body
        try:
            planned = await asyncio.to_thread(self.machines.plan_run, body)
            return web.json_response(_clean(self.machines.start_run(planned)))
        except machines_api.MachinesApiError as exc:
            return self._machines_error(exc)

    async def _machines_run_state(self, request: web.Request) -> web.Response:
        """A run as it stands; the full output of the machines named in ?show= (the rest, a line each)."""
        from ixel_mat.gui import machines_api
        try:
            return web.json_response(_clean(self.machines.run_state(request.query.get("id"),
                                                                    request.query.get("show"))))
        except machines_api.MachinesApiError as exc:
            return self._machines_error(exc)

    async def _machines_run_stop(self, request: web.Request) -> web.Response:
        from ixel_mat.gui import machines_api
        body = await self._machines_body(request)
        if isinstance(body, web.Response):
            return body
        try:
            return web.json_response(_clean(await self.machines.stop_run(body)))
        except machines_api.MachinesApiError as exc:
            return self._machines_error(exc)

    @staticmethod
    async def _read_capped(request: web.Request, cap: int) -> bytes | None:
        """The request's body, read here and counted (the 512 KB limit on the rest doesn't fit it); None if
        it's over cap."""
        if request.content_length is not None and request.content_length > cap:
            return None
        data = bytearray()
        async for chunk in request.content.iter_chunked(64 * 1024):
            data += chunk
            if len(data) > cap:
                return None
        return bytes(data)

    async def _sound(self, request: web.Request) -> web.Response:
        """Sound recorded or attached in Ask, written out by the service set up for it → {text, service}.
        The sound goes there and nowhere else, and isn't kept. ?expect= names the service the page said it
        goes to: when the settings have changed since, nothing is sent and the page is told."""
        settings = self._load_settings()
        provider, problem = sound.ready(settings.config)
        if provider is None:
            return _json_error(409, problem)
        expect = request.query.get("expect", "")
        if expect and expect != provider.name:
            return web.json_response(_clean({
                "error": f"Sound goes to {provider.label} now, since the settings changed. Nothing was sent: "
                         f"send it again to have {provider.label} write it out.",
                "code": "sound_service_changed", "service": provider.label}), status=409)
        data = await self._read_capped(request, sound.MAX_BYTES)
        if data is None:
            return _json_error(413, f"That's over {sound.MAX_BYTES // (1024 * 1024)} MB of sound, more than the "
                                    "service takes at once. Send a shorter piece.")
        try:
            text = await asyncio.to_thread(sound.transcribe, provider, data)
        except sound.SoundError as exc:  # may quote the service
            said = {"error": _clean(str(exc))}
            if exc.code:
                said["code"] = exc.code
            return web.json_response(said, status=400)
        return web.json_response(_clean({"text": text, "service": provider.label}))

    @staticmethod
    def _tab(request: web.Request, name: str = "tab") -> str | None:
        tab = request.query.get(name, "")
        return tab if TAB_ID.fullmatch(tab) else None

    async def _conversations(self, request: web.Request) -> web.Response:
        """This tab's conversations, as it last sent them ([] if none): after a reload, the page shows them again."""
        tab = self._tab(request)
        if tab is None:
            return _json_error(400, "tab must be the id this tab made up for itself.")
        return web.Response(body=self.conversations.get(tab) or b"[]",
                            headers={"Content-Type": "application/json; charset=utf-8"})

    async def _keep_conversations(self, request: web.Request) -> web.Response:
        """Keep this tab's conversations (a JSON list; an empty one forgets them), in memory only. was: the id they
        were under before the page loaded, which goes first when too many are kept."""
        tab = self._tab(request)
        if tab is None:
            return _json_error(400, "tab must be the id this tab made up for itself.")
        was = self._tab(request, "was") if "was" in request.query else ""
        if was is None:
            return _json_error(400, "was must be the id this tab had before.")
        data = await self._read_capped(request, MAX_CONVERSATION_BYTES)
        if data is None:
            return _json_error(413, f"Conversations over {MAX_CONVERSATION_BYTES // (1024 * 1024)} MB aren't kept.")
        try:
            kept = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _json_error(400, "Body must be JSON.")
        if not isinstance(kept, list):
            return _json_error(400, "Body must be a JSON list.")
        if kept:
            self.conversations.put(tab, data, moved_from=was)
        else:
            self.conversations.forget(tab)
        return web.json_response({"kept": len(kept)})

    async def _add_picture(self, request: web.Request) -> web.Response:
        """One picture for a question to come: checked, its metadata taken out, kept in memory. → its id"""
        data = await self._read_capped(request, pictures.MAX_BYTES)
        if data is None:
            return _json_error(413, TOO_BIG)
        try:
            picture = await asyncio.to_thread(pictures.read_picture, data)
        except pictures.PictureError as exc:
            return _json_error(400, str(exc))
        return web.json_response({"id": self.pictures.add(picture), "width": picture.width,
                                  "height": picture.height, "bytes": len(picture.data)})

    def _pictures_for(self, value: Any) -> list:
        """The pictures a question names (PictureError says why not)."""
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise pictures.PictureError("pictures must be a list of the ids Ixel gave them.")
        return self.pictures.take(value)

    async def _review(self, request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _json_error(400, "Body must be JSON.")
        if not isinstance(body, dict):
            return _json_error(400, "Body must be a JSON object.")
        question = body.get("question")
        mode = body.get("mode")
        if mode == "":
            mode = None
        code = body.get("material") or ""
        if not isinstance(code, str):
            return _json_error(400, "material must be text.")
        try:
            material, asked = code_for_review(question if isinstance(question, str) else "", code=code,
                                              code_title="code the user attached")
        except MaterialError as exc:
            return _json_error(400, str(exc))
        try:
            attached = self._pictures_for(body.get("pictures"))
        except pictures.PictureGone as exc:  # the page sends them again
            return web.json_response({"error": str(exc), "code": "pictures_gone"}, status=400)
        except pictures.PictureError as exc:
            return _json_error(400, str(exc))
        if isinstance(question, str):
            question = asked  # the default question for the code, if there's code and no question
            if not question.strip() and attached:
                question = "What do you make of the attached picture?" if len(attached) == 1 else \
                    "What do you make of the attached pictures?"
        if not isinstance(question, str) or not question.strip():
            return _json_error(400, "Ask a question first.")
        if len(question) > MAX_QUESTION_CHARS:
            return _json_error(400, f"The question is longer than {MAX_QUESTION_CHARS:,} characters.")
        if mode is not None and (not isinstance(mode, str) or mode not in MODE_CHOICES):
            return _json_error(400, "mode must be quick, review, deep, saver or auto.")
        earlier = _parse_earlier(body.get("earlier"))
        if earlier is None:
            return _json_error(400, f"earlier must be a list of up to {MAX_EARLIER_TURNS} "
                                    f"{{question, answer}} objects of text.")
        if self._lock.locked():
            return _json_error(409, "A review is already running.")

        async with self._lock:
            response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson; charset=utf-8"})
            await response.prepare(request)
            await self._stream_review(request, response, question.strip(), mode, earlier, material, attached)
            return response

    async def _stream_review(self, request: web.Request, response: web.StreamResponse,
                             question: str, mode: str | None, earlier: list[EarlierTurn],
                             material: Material | None = None, attached: Sequence = ()) -> None:
        connect_task: asyncio.Task | None = None
        review_task: asyncio.Task | None = None
        watchdog: asyncio.Task | None = None
        gone = False

        def page_gone() -> None:
            # The page closed or pressed Stop: stop paying for model calls
            nonlocal gone
            gone = True
            for task in (connect_task, review_task):
                if task is not None and not task.done():
                    task.cancel()

        async def watch_connection() -> None:
            # Writes only happen between events, and a slow model can take
            # minutes; notice a closed tab without waiting for the next one.
            while True:
                await asyncio.sleep(0.25)
                transport = request.transport
                if transport is None or transport.is_closing():
                    page_gone()
                    return

        async def send(kind: str, data: dict) -> None:
            if gone:
                return
            try:
                await response.write((json.dumps({"kind": kind, "data": _clean(data)}) + "\n").encode())
            except (ConnectionError, RuntimeError):
                page_gone()

        settings = self._load_settings()

        async def on_connect(cfg, error):
            await send("connect", {"agent": cfg.name, "agent_label": cfg.label,
                                   "ok": error is None, "error": "" if error is None else str(error)})

        agents: dict = {}
        decision = None
        # Watched from the start: a page closed while the models connect stops that too
        watchdog = asyncio.create_task(watch_connection())
        self._reviews.add(page_gone)
        try:
            # Auto mode asks Triage first (well under a second); the engine reports the decision
            if getattr(earlier, "private", False) and not settings.private:  # this page hadn't heard yet
                await send("error", {"code": "private_off", "message": (
                    "This conversation was asked with Private on, and Private is off now, so its earlier questions "
                    "stay with your own models. Ask again to start a new conversation.")})
                return
            review_mode, decision = await choose_mode(settings, mode, question, earlier)
            if gone:
                return
            problem = settings.private_problem(review_mode)
            if problem:
                await send("error", {"message": problem})
                return
            connect_task = asyncio.create_task(self._connect(settings.configs_for(review_mode),
                                                             on_result=on_connect))
            try:
                agents = await connect_task
            except asyncio.CancelledError:
                if not gone:  # we were cancelled, not the page
                    raise
                return  # connect_agents disconnected whatever it had connected
            if gone:
                return
            if not agents:
                await send("error", {"message": "No panel agents connected. Run `ixel setup`, "
                                                "then `ixel agents` to check them."})
                return
            await send("start", {"mode": review_mode.value, "rounds": review_mode.rounds,
                                 "auto": decision is not None, "private": settings.private,
                                 "agents": [a.label for a in agents.values()][:MAX_PANEL]})

            async def on_event(event):
                await send(event.kind, event.data)

            review_task = asyncio.create_task(run_review(
                question, list(agents.values()), on_event=on_event, earlier=earlier, auto=decision,
                material=material, pictures=attached, **settings.run_options(review_mode),
            ))
            try:
                result = await review_task
            except asyncio.CancelledError:
                if not gone:  # we were cancelled, not the page
                    raise
            else:
                update = stats.record_run(result, local_agent_names(settings.agent_configs))
                if update.counted:
                    await send("saves", update.to_dict())
        except Exception as exc:  # noqa: BLE001 — report, don't leave the page hanging
            logger.exception("review failed")
            await send("error", {"message": f"Something went wrong: {exc}"})
        finally:
            self._reviews.discard(page_gone)
            for task in (watchdog, connect_task, review_task):
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            await self._disconnect(agents)


async def _start(port: int) -> tuple[GuiServer, web.AppRunner]:
    # The saved keys first, on a thread: the keychain may wait for a password prompt, and the event loop
    # mustn't stop while it does. After this, what the keychain said is remembered, and requests read the
    # keys without waiting (load_settings(wait=False)); anything more is asked on a thread of its own.
    await asyncio.to_thread(lambda: (load_env(), where_keys_are()))
    gui = GuiServer(port=port)
    runner = web.AppRunner(gui.app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    gui.port = site._server.sockets[0].getsockname()[1]
    return gui, runner


async def serve(port: int = 0, open_browser: bool = True, announce: Callable[[str], None] = print) -> None:
    gui, runner = await _start(port)
    announce(gui.url)
    launch_page = None
    if open_browser:
        try:
            launch_page = write_launch_page(gui.url)
            with keys_withheld():  # the browser it starts gets none of the keys Ixel saved
                webbrowser.open(launch_page.as_uri())
        except Exception:  # noqa: BLE001 — the printed link still works
            pass
    try:
        # The launch file holds the session key: it's deleted as soon as the page has it
        await until_closed(gui.presence, first_wait=math.inf, grace=math.inf,
                           on_first_page=lambda: forget_launch_page(launch_page))
    finally:
        forget_launch_page(launch_page)
        await runner.cleanup()


async def serve_window(open_window: Callable[[str], Awaitable[str]], announce: Callable[[str], None] = print,
                       port: int = 0, first_wait: float = FIRST_PAGE_WAIT, grace: float = RELOAD_GRACE) -> bool:
    """
    `ixel app`: open the page with open_window and serve it until it's closed. announce is told
    where it opened once the page has arrived. False if it never did (the window didn't open).
    """
    gui, runner = await _start(port)
    launch_page = None
    try:
        launch_page = write_launch_page(gui.url)
        where = await open_window(launch_page.as_uri())
        if not where:  # no browser could be started at all
            return False

        def first_page() -> None:
            forget_launch_page(launch_page)  # it holds the session key: gone as soon as the page has it
            announce(where)
        await until_closed(gui.presence, first_wait=first_wait, grace=grace, on_first_page=first_page)
        return gui.presence.seen
    finally:
        forget_launch_page(launch_page)
        await runner.cleanup()


async def serve_hosted(port: int = 0, out=None, stdin=None) -> None:
    """
    `ixel app --host`, for a native window (Ixel.app on a Mac, the GTK window on Linux) that started this
    with pipes: print the page's address, with the session key, on stdout, which only that window reads,
    then serve until it closes stdin (it quit, or crashed). Nothing else is printed on stdout.
    """
    out = out or sys.stdout
    stdin = stdin or sys.stdin
    gui, runner = await _start(port)
    loop = asyncio.get_running_loop()
    closed = asyncio.Event()

    def wait_for_eof() -> None:
        try:
            while stdin.buffer.read(65536):
                pass
        except (OSError, ValueError):
            pass
        loop.call_soon_threadsafe(closed.set)

    try:
        out.write(f"{HOST_PREFIX}{gui.url}\n")
        out.flush()
        # A daemon thread: a blocking read mustn't keep Python from exiting
        threading.Thread(target=wait_for_eof, name="ixel-host-stdin", daemon=True).start()
        await closed.wait()
    finally:
        await runner.cleanup()


def forget_launch_page(page: Path | None) -> None:
    """Delete the launch page. If Windows won't yet (a virus scanner has it open), the next try, at exit, will."""
    if page is not None:
        try:
            page.unlink(missing_ok=True)
        except OSError:
            pass


def write_launch_page(url: str) -> Path:
    """
    A private (0600) page that forwards the browser to url. Opening this file
    instead of the URL keeps the session key out of the browser launcher's
    command line, which other users on the same computer can read.
    """
    fd, path = tempfile.mkstemp(prefix="ixel-gui-", suffix=".html")
    target = html.escape(url, quote=True)
    with os.fdopen(fd, "w", encoding="utf-8") as page:
        page.write('<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
                   f'<meta http-equiv="refresh" content="0;url={target}"><title>Ixel</title>'
                   f'<p><a href="{target}">Open Ixel</a></p>')
    return Path(path)
