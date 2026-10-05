"""WebSocket transport for OpenClaw gateway agents.

Protocol (proven via headless probe 2026-04-08):
  1. Gateway sends connect.challenge {nonce}
  2. Client sends signed connect req (ed25519 v2 scheme)
     CRITICAL: client.id MUST be "webchat" — gateway validates as constant
  3. Gateway replies hello-ok
  4. Send:  chat.send RPC → RES {runId, status:'started'}
  5. Recv:  EVENT 'chat' {state:'final', runId, sessionKey}
  6. Fetch: chat.history RPC {sessionKey, limit} → text in messages

Session isolation (v0.2):
  Each send_and_receive() call generates a unique session key suffix
  so /full calls never share context with each other or with live chat.
  RunId from chat.send response is correlated through the full cycle.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Awaitable, Callable

import websockets
import websockets.exceptions
from websockets.asyncio.client import connect as _ws_connect

from ixel_mat import __version__
from ixel_mat.agents.base import AgentConfig, BaseAgent, is_loopback_host
from ixel_mat.config.secrets import write_private_file

_KEY_DIR  = Path.home() / ".config" / "ixel-mat"
_KEY_FILE = _KEY_DIR / "device_key"


def _load_or_gen_key():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, NoEncryption, PrivateFormat, PublicFormat,
        )
    except ImportError:
        raise RuntimeError("pip install cryptography")

    if _KEY_FILE.exists():
        priv = Ed25519PrivateKey.from_private_bytes(_KEY_FILE.read_bytes())
    else:
        priv = Ed25519PrivateKey.generate()
        raw = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        write_private_file(_KEY_FILE, raw)

    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    pub_raw   = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    device_id = hashlib.sha256(pub_raw).hexdigest()
    pub_b64   = base64.urlsafe_b64encode(pub_raw).rstrip(b"=").decode()
    return priv, device_id, pub_b64


def _sign(priv, payload: str) -> str:
    return base64.urlsafe_b64encode(
        priv.sign(payload.encode("utf-8"))
    ).rstrip(b"=").decode()



class _connect_without_redirects(_ws_connect):
    """
    websockets follows redirects, cross-origin and ws:// ones included, and the
    gateway token is sent right after connecting: a redirect could hand it to
    another host, past the wss:// check in connect(). Gateways don't redirect.
    """

    def process_redirect(self, exc: Exception) -> Exception:
        return exc

class WebSocketAgent(BaseAgent):

    def __init__(self, config: AgentConfig, response_timeout: float = 60.0):
        """Create a WebSocketAgent.

        Args:
            response_timeout: How long (seconds) send_and_receive() waits for
                a state=final event before raising TimeoutError.  Defaults to
                60 s to match FullModeDispatcher.timeout so the two layers
                fire at the same threshold.  When FullModeDispatcher wraps
                send_and_receive() in its own asyncio.wait_for, the outer
                timeout governs /full mode; this inner timeout applies when
                the agent is used standalone (e.g. mat.py single-query mode).
        """
        super().__init__(config)
        self.response_timeout = response_timeout
        self._ws = None
        self._listen_callback: Callable[[str], Awaitable[None]] | None = None
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

        # Run tracking — keyed by runId for proper correlation
        self._run_completions: dict[str, asyncio.Event] = {}
        self._pending: dict[str, asyncio.Future] = {}
        # chat.send request id → the Event its run will set. The reader moves it to
        # _run_completions the moment the reply names the runId, before it reads the
        # next frame: a fast gateway sends state=final right behind that reply.
        self._send_completions: dict[str, asyncio.Event] = {}

    @property
    def _session_key(self) -> str:
        return self.config.session_key or f"agent:{self.config.name}:main"

    # ── connect ───────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        if self._connected:
            return
        if not self.config.url:
            raise ValueError(f"Agent '{self.name}' missing ws URL")

        from urllib.parse import urlparse
        _p     = urlparse(self.config.url)
        origin = f"{'https' if _p.scheme == 'wss' else 'http'}://{_p.netloc}"

        # Security: enforce wss:// for non-loopback connections
        if _p.scheme == "ws":
            host = _p.hostname or ""
            if not is_loopback_host(host):
                raise ValueError(
                    f"Agent '{self.name}': ws:// refused for remote host '{host}'. "
                    "Use wss:// for non-local connections."
                )

        ws = await _connect_without_redirects(
            self.config.url,
            open_timeout=10,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=5,
            additional_headers={"Origin": origin},
        )

        try:
            raw   = await asyncio.wait_for(ws.recv(), timeout=10)
            nonce = json.loads(raw)["payload"]["nonce"]

            priv, device_id, pub_b64 = _load_or_gen_key()
            signed_at = int(time.time() * 1000)
            scopes    = ["operator.read", "operator.write"]
            sig_str   = "|".join(["v2", device_id, "webchat", "webchat", "operator",
                                   ",".join(scopes), str(signed_at),
                                   self.config.token or "", nonce])
            signature = _sign(priv, sig_str)

            await ws.send(json.dumps({
                "type": "req", "id": str(uuid.uuid4()), "method": "connect",
                "params": {
                    "minProtocol": 3, "maxProtocol": 3,
                    "client": {"id": "webchat", "version": "3.0.0",
                               "platform": "linux", "mode": "webchat"},
                    "role": "operator", "scopes": scopes,
                    "caps": [], "commands": [], "permissions": {},
                    "auth": {"token": self.config.token or ""},
                    "locale": "en-US", "userAgent": f"ixel-mat/{__version__}",
                    "device": {
                        "id": device_id, "publicKey": pub_b64,
                        "signature": signature, "signedAt": signed_at, "nonce": nonce,
                    },
                },
            }))

            raw2 = await asyncio.wait_for(ws.recv(), timeout=10)
            resp = json.loads(raw2)
            if not resp.get("ok"):
                raise ValueError(f"Rejected: {resp.get('error', {}).get('message', '?')}")
            if resp.get("payload", {}).get("type") != "hello-ok":
                raise ValueError(f"Expected hello-ok, got {resp.get('payload',{}).get('type')}")

        except Exception as exc:
            await ws.close()
            raise ValueError(f"Handshake failed for '{self.name}': {exc}")

        self._ws = ws
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._connected = True

    # ── disconnect ────────────────────────────────────────────────────────────

    async def disconnect(self) -> None:
        self._connected = False
        # Unblock any waiting runs
        for evt in self._run_completions.values():
            evt.set()
        self._run_completions.clear()
        self._send_completions.clear()
        # Cancel any pending RPC futures
        for fid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(RuntimeError(f"Agent '{self.name}' disconnected"))
        self._pending.clear()
        if self._reader_task:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        if self._ws:
            with suppress(Exception):
                await self._ws.close()
            self._ws = None

    # ── send (fire-and-forget) ────────────────────────────────────────────────

    async def send(self, message: str) -> None:
        if not self._ws:
            raise RuntimeError(f"Agent '{self.name}' not connected")
        await self._ws.send(json.dumps({
            "type": "req", "id": str(uuid.uuid4()),
            "method": "chat.send",
            "params": {
                "sessionKey":     self._session_key,
                "message":        message,
                "deliver":        False,
                "idempotencyKey": str(uuid.uuid4()),
            },
        }))

    # ── send_and_receive (with full isolation) ────────────────────────────────

    async def send_and_receive(self, message: str, use_full_session: bool = True, **kwargs) -> str:
        """
        Send message and return reply text.
        
        Session isolation (v0.2):
          - Each call gets a unique ephemeral session key (uuid suffix)
          - RunId from chat.send response is tracked
          - Only accepts state=final for OUR runId (not any random final)
          - History fetch uses the ephemeral session key
        """
        async with self._lock:
            if not self._connected or not self._ws:
                raise RuntimeError(f"Agent '{self.name}' not connected")

            # Generate ephemeral session key for this run
            if use_full_session:
                run_suffix = uuid.uuid4().hex[:8]
                sk = f"{self._session_key}:full:{run_suffix}"
            else:
                sk = self._session_key

            # Send chat.send and capture runId from response
            send_id = str(uuid.uuid4())
            send_fut: asyncio.Future = asyncio.get_event_loop().create_future()
            self._pending[send_id] = send_fut
            completion = asyncio.Event()
            self._send_completions[send_id] = completion

            await self._ws.send(json.dumps({
                "type": "req", "id": send_id,
                "method": "chat.send",
                "params": {
                    "sessionKey":     sk,
                    "message":        message,
                    "deliver":        False,
                    "idempotencyKey": str(uuid.uuid4()),
                },
            }))

            # Wait for chat.send RES to get runId
            try:
                send_resp = await asyncio.wait_for(send_fut, timeout=15)
            except asyncio.TimeoutError:
                self._pending.pop(send_id, None)
                raise TimeoutError(f"'{self.name}' chat.send timed out")
            finally:
                self._send_completions.pop(send_id, None)

            if not send_resp.get("ok"):
                err = send_resp.get("error", {}).get("message", "unknown")
                raise RuntimeError(f"'{self.name}' chat.send failed: {err}")

            run_id = send_resp.get("payload", {}).get("runId", "")
            if not run_id:
                raise RuntimeError(f"'{self.name}' chat.send returned no runId")

            # The reader registered `completion` under this runId when the reply came in
            # Wait for state=final with OUR runId
            try:
                await asyncio.wait_for(completion.wait(),
                                       timeout=self.response_timeout)
            except asyncio.TimeoutError:
                self._run_completions.pop(run_id, None)
                raise TimeoutError(
                    f"'{self.name}' run {run_id[:8]} did not complete within {self.response_timeout}s"
                )
            finally:
                self._run_completions.pop(run_id, None)

            if not self._connected:
                raise RuntimeError(f"Agent '{self.name}' disconnected while waiting")

            # Fetch reply from the ephemeral session
            return await self._fetch_reply(session_key=sk)

    # ── chat.history fetch ────────────────────────────────────────────────────

    async def _fetch_reply(self, limit: int = 5, session_key: str = "") -> str:
        if not self._ws:
            return ""

        sk = session_key or self._session_key
        hist_id = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[hist_id] = fut

        await self._ws.send(json.dumps({
            "type": "req", "id": hist_id,
            "method": "chat.history",
            "params": {"sessionKey": sk, "limit": limit},
        }))

        try:
            resp = await asyncio.wait_for(fut, timeout=10)
        except asyncio.TimeoutError:
            self._pending.pop(hist_id, None)
            return ""

        if not resp.get("ok"):
            return ""

        for msg in reversed(resp.get("payload", {}).get("messages", [])):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") not in ("assistant", "ai", "model"):
                continue
            text = self._extract_text(msg.get("content", ""))
            if text:
                return text
        return ""

    # ── listen ────────────────────────────────────────────────────────────────

    async def listen(self, callback: Callable[[str], Awaitable[None]]) -> None:
        self._listen_callback = callback
        while self._connected:
            await asyncio.sleep(0.1)

    # ── reader loop ───────────────────────────────────────────────────────────

    async def _reader_loop(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                ftype   = frame.get("type", "")
                fevent  = frame.get("event", "")
                payload = frame.get("payload", {}) or {}

                # RES frames → resolve pending futures by request id
                if ftype == "res":
                    fid = frame.get("id", "")
                    completion = self._send_completions.pop(fid, None)
                    run_id = (frame.get("payload") or {}).get("runId") if frame.get("ok") else None
                    if completion is not None and isinstance(run_id, str) and run_id:
                        self._run_completions[run_id] = completion
                    fut = self._pending.pop(fid, None)
                    if fut and not fut.done():
                        fut.set_result(frame)

                # state=final → signal the SPECIFIC run that completed
                elif ftype == "event" and fevent == "chat":
                    if payload.get("state") == "final":
                        completed_run_id = payload.get("runId", "")
                        evt = self._run_completions.get(completed_run_id)
                        if evt:
                            # send_and_receive() is waiting — signal it
                            evt.set()
                        elif self._listen_callback:
                            # fire-and-forget send() path — forward text to listener
                            asyncio.create_task(
                                self._forward_to_listener(payload)
                            )

                # ping → pong
                elif ftype == "ping":
                    if self._ws:
                        with suppress(Exception):
                            await self._ws.send(json.dumps({"type": "pong"}))

        except websockets.exceptions.ConnectionClosed as exc:
            import logging
            logging.getLogger("ixel-mat.ws").debug(
                "WS closed for '%s': code=%s reason=%s", self.name, exc.code, exc.reason)
        except Exception as exc:
            import logging
            logging.getLogger("ixel-mat.ws").warning(
                "WS reader error for '%s': %s: %s", self.name, type(exc).__name__, exc)
        finally:
            self._connected = False
            # Unblock any still-waiting runs
            for evt in self._run_completions.values():
                evt.set()
            # Cancel pending futures
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError(f"Agent '{self.name}' connection lost"))
            self._pending.clear()

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _forward_to_listener(self, payload: dict) -> None:
        """Fetch the latest reply and forward it to the listen() callback.

        Called from _reader_loop when a chat state=final event arrives for a
        session that no send_and_receive() is waiting on (i.e. a plain send()
        fire-and-forget message).
        """
        if not self._listen_callback:
            return
        try:
            # Gateway includes sessionKey in the chat event payload
            session_key = payload.get("sessionKey", "") or self._session_key
            text = await self._fetch_reply(session_key=session_key)
            if text:
                await self._listen_callback(text)
        except Exception as exc:
            import logging
            logging.getLogger("ixel-mat.ws").debug(
                "listen callback error for '%s': %s", self.name, exc)

    def _extract_text(self, content) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            return "\n".join(p for p in parts if p).strip()
        if isinstance(content, dict):
            return content.get("text", content.get("content", "")).strip()
        return ""
