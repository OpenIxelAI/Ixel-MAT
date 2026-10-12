"""Approved MCP add-ons, connected over stdio for the lifetime of one agent run.

Only the optional add-ons loader supplies cards. Each connection owns its MCP contexts in one task:
the SDK's task groups must be entered and exited there, including when the person stops the run.
"""
from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from ixel_mat import pictures
from ixel_mat.agent import sandbox
from ixel_mat.agent.tools import Viewed
from ixel_mat.config.secrets import child_env
from ixel_mat.material import mask_secrets

DEFAULT_TIMEOUT = 120.0
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


class Permissions(Protocol):
    files: str
    network: bool
    commands: list[list[str]]


class Card(Protocol):
    id: str
    name: str
    kind: str
    permissions: Permissions
    dir: Path


def addon_module() -> ModuleType | None:
    """Imported only when a run needs it; older Ixels have no add-ons module."""
    try:
        return importlib.import_module("ixel_mat.addons")
    except ImportError:
        return None


def enabled_cards() -> list[Card]:
    module = addon_module()
    loader = getattr(module, "enabled_cards", None)
    if not callable(loader):
        return []
    return [card for card in loader("mcp")
            if card.kind == "mcp" and getattr(card, "enabled", True)]


def check_installed(card: Card) -> None:
    """The add-ons module's own check that an enabled card's installed files still match its tree hash and
    file count (edited files after approval); it raises when they don't. No module, or an older one without
    the check, checks nothing more than enabled_cards did."""
    check = getattr(addon_module(), "check_installed", None)
    if callable(check):
        check(card)


def server_parameters(card: Card, workspace: Path) -> StdioServerParameters:
    """The card's single argv, with only installed files and output substituted; never a shell."""
    commands = card.permissions.commands
    if not isinstance(commands, (list, tuple)) or len(commands) != 1:
        raise ValueError("an MCP add-on needs exactly one command")
    argv = commands[0]
    if (not isinstance(argv, (list, tuple)) or not argv
            or not all(isinstance(word, str) and word and "\x00" not in word for word in argv)):
        raise ValueError("an MCP command must be a non-empty argv list of text")
    values = {"addon": str(Path(card.dir) / "files"), "output": str(workspace)}

    def fill(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise ValueError(f"MCP commands can't fill {{{key}}}; only {{addon}} and {{output}} are allowed")
        return values[key]

    command = [_PLACEHOLDER.sub(fill, word) for word in argv]
    if sandbox.is_shell_or_interpreter(command):
        raise ValueError("an MCP add-on must start a program, never a shell or inline interpreter code")
    if card.permissions.files not in ("none", "output-folder", "chosen-folder"):
        raise ValueError("unknown files permission")
    if not isinstance(card.permissions.network, bool):
        raise ValueError("network permission must be true or false")
    if sandbox.can_sandbox():
        profile = sandbox.sandbox_profile(workspace, network=card.permissions.network,
                                          files=card.permissions.files, temporary=False)
        command = [sandbox.SANDBOX_EXEC, "-p", profile, *command]
    # stdio_client merges its default environment into this one. Explicit empty values prevent it
    # restoring a saved key in any of those default names after child_env withheld it.
    env = {key: "" for key in get_default_environment()}
    env.update(child_env(nested=False))
    return StdioServerParameters(command=command[0], args=command[1:], env=env, cwd=str(workspace))


@dataclass
class _Request:
    tool: str
    arguments: dict[str, Any]
    result: asyncio.Future[Any]


class _Server:
    def __init__(self, card: Card, workspace: Path, timeout: float) -> None:
        self.card = card
        self.parameters = server_parameters(card, workspace)
        self.timeout = timeout
        self.tools: list[dict] = []
        self.ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.requests: asyncio.Queue[_Request] = asyncio.Queue()
        self.task = asyncio.create_task(self._serve())

    async def _serve(self) -> None:
        request = None
        try:
            with open(os.devnull, "w", encoding="utf-8") as errlog:
                async with stdio_client(self.parameters, errlog=errlog) as streams:
                    async with ClientSession(*streams, read_timeout_seconds=self.timeout) as session:
                        await asyncio.wait_for(session.initialize(), self.timeout)
                        listed = await asyncio.wait_for(session.list_tools(), self.timeout)
                        self.tools = [{"name": tool.name, "description": tool.description or "",
                                       "inputSchema": tool.input_schema} for tool in listed.tools]
                        self.ready.set_result(None)
                        while True:
                            request = await self.requests.get()
                            try:
                                result = await asyncio.wait_for(session.call_tool(request.tool,
                                                                                  request.arguments), self.timeout)
                            except Exception as exc:  # noqa: BLE001 — report this call, keep the session usable
                                if not request.result.done():
                                    request.result.set_exception(ValueError(self._error(exc)))
                            else:
                                if not request.result.done():
                                    request.result.set_result(result)
                            request = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a broken server doesn't fail other add-ons or the run
            error = ValueError(self._error(exc))
            if not self.ready.done():
                self.ready.set_exception(error)
            if request is not None and not request.result.done():
                request.result.set_exception(error)
        finally:
            if not self.ready.done():
                self.ready.cancel()
            if request is not None and not request.result.done():
                request.result.set_exception(ValueError("the MCP server stopped"))
            while not self.requests.empty():
                pending = self.requests.get_nowait()
                if not pending.result.done():
                    pending.result.set_exception(ValueError("the MCP server stopped"))

    def _error(self, exc: Exception) -> str:
        nested = getattr(exc, "exceptions", ())
        if nested:
            return "; ".join(self._error(item) for item in nested)
        if isinstance(exc, asyncio.TimeoutError):
            return f"{self.card.name} didn't answer within {self.timeout:g} seconds"
        return mask_secrets(f"{self.card.name}: {str(exc) or type(exc).__name__}")

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        if self.task.done():
            raise ValueError("the MCP server stopped")
        result = asyncio.get_running_loop().create_future()
        await self.requests.put(_Request(tool, arguments, result))
        return await result


class AddonServers:
    """One set of enabled servers and one tools listing per server, scoped to a single run."""

    def __init__(self, workspace: Path, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.workspace = workspace
        self.timeout = timeout
        self.servers: dict[str, _Server] = {}
        self.tools: list[dict] = []

    async def start(self) -> None:
        for card in enabled_cards():
            if card.id in self.servers:
                continue
            try:
                check_installed(card)
            except Exception as exc:  # noqa: BLE001 — the add-ons module's AddonError, or anything it raises
                self.tools.append({"addon": card.id, "name": card.name, "tools": [],
                                   "error": "its installed files don't match what was approved, so it wasn't "
                                            f"started; approve it again in Settings › Add-ons "
                                            f"({mask_secrets(str(exc))})"})
                continue
            try:
                server = _Server(card, self.workspace, self.timeout)
                self.servers[card.id] = server
                await asyncio.shield(server.ready)
                self.tools.append({"addon": card.id, "name": card.name, "tools": server.tools})
            except (ValueError, OSError) as exc:
                self.tools.append({"addon": card.id, "name": card.name,
                                   "error": mask_secrets(str(exc)), "tools": []})

    def card_for_tool(self, addon: str, tool: str) -> Card:
        server = self.servers.get(addon)
        if server is None or server.task.done():
            raise ValueError(f"MCP add-on {addon!r} isn't available in this run")
        if not any(item["name"] == tool for item in server.tools):
            raise ValueError(f"{tool!r} isn't a listed tool of {addon!r}")
        return server.card

    async def call(self, addon: str, tool: str, arguments: dict[str, Any]) -> tuple[bool, Viewed]:
        self.card_for_tool(addon, tool)
        result = await self.servers[addon].call(tool, arguments)
        lines, found = [], []
        total = 0
        for content in result.content:
            if content.type == "text":
                lines.append(content.text)
            elif content.type == "image":
                try:
                    if len(content.data) > 4 * ((pictures.MAX_BYTES + 2) // 3):
                        raise ValueError("picture exceeds the size limit")
                    picture = pictures.read_picture(base64.b64decode(content.data, validate=True))
                    if (len(found) >= pictures.MAX_PER_QUESTION
                            or total + len(picture.data) > pictures.MAX_PER_QUESTION_BYTES):
                        raise ValueError("picture exceeds the model call's picture limit")
                    found.append(picture)
                    total += len(picture.data)
                except ValueError as exc:
                    lines.append(f"[picture left out: {exc}]")
        if result.structured_content is not None:
            lines.append(json.dumps(result.structured_content, ensure_ascii=False))
        if found:
            lines.append(f"[{len(found)} picture(s); they go to you with your next turn]")
        return not result.is_error, Viewed(mask_secrets("\n".join(lines)), found)

    async def close(self) -> None:
        tasks = [server.task for server in self.servers.values()]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
