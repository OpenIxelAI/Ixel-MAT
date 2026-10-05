"""
BaseAgent — interface all agent transports implement.

Every agent (WebSocket, subprocess, ACP, API) must implement this.
This is the contract that /agent, /full, and future /max depend on.
"""

import ipaddress
import os
from urllib.parse import urlparse
import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Awaitable, Callable

from ixel_mat.usage import is_private_host


DEFAULT_TIMEOUT = 180.0  # seconds per model call; reasoning models can take minutes
# How long a transport itself lets a call run when the agent sets no timeout: every caller (a review,
# `ixel ask`, triage) waits with its own limit, so this only stops a call that nothing else limits
UNSET_TRANSPORT_TIMEOUT = 3600.0
# "auto": an argument when it fits on a command line, else stdin (for CLIs that read either)
PROMPT_MODES = ("flag", "arg", "stdin", "auto")
# How a CLI prints its answer: plain text, or Claude Code's stream-json events (which let the
# answer be shown as it's written)
STDOUT_FORMATS = ("text", "claude-stream-json")
EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")
# What an agent can take besides text (`accepts = ["image"]`)
MEDIA = ("image",)
# APIs whose current models all take pictures; an agent on one of them sees pictures unless its
# `accepts` says otherwise. A local or custom server only does when its `accepts` says so.
PICTURE_PROVIDERS = ("anthropic", "openai", "xai", "gemini")


def is_loopback_host(host: str | None) -> bool:
    """True if host is this machine (localhost or a loopback IP)."""
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_local_network_host(host: str | None) -> bool:
    """True if host is this machine or on your own network (a private or Tailscale address, nas.local,
    a bare gpu-box)."""
    return is_loopback_host(host) or is_private_host(host)


def needs_api_key(cfg: "AgentConfig") -> bool:
    """Gateway and cloud agents need a key; a model server on this machine or your own network (Ollama…)
    doesn't."""
    if cfg.type == "websocket":
        return True
    if cfg.type != "http":
        return False
    try:
        return not is_local_network_host(urlparse(cfg.url).hostname)
    except ValueError:
        return True


def sends_key_in_cleartext(url: str, token: str | None) -> bool:
    """True if a request to url carrying token would cross the network unencrypted: a key goes over plain
    http:// only to this machine."""
    if not token:
        return False
    try:
        parsed = urlparse(url)
        return parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback_host(parsed.hostname))
    except ValueError:
        return True


def cleartext_refusal(name: str, url: str) -> str:
    """What to say when an agent's key would go over plain http:// to another computer."""
    try:
        host = urlparse(url).hostname or url
    except ValueError:
        host = url
    return (f"Agent '{name}': Ixel won't send its API key over plain http:// to {host}, where anyone on the "
            "way could read it. Use https://, or take the key out if that server doesn't need one.")


def nearest_effort(effort: str, supported: list[str] | None) -> str:
    """The supported level closest to effort (ties go to the lower one)."""
    order = {level: i for i, level in enumerate(EFFORT_LEVELS)}
    known = [level for level in supported or () if level in order]
    if not known or effort in (supported or ()) or effort not in order:
        return effort
    return min(known, key=lambda level: (abs(order[level] - order[effort]), order[level]))


def prepare_workdir(spec: str) -> tuple[str | None, bool]:
    """
    Resolve an agent's workdir setting to (cwd, remove_when_done).

    "temp" gives each run a fresh empty directory, so a CLI agent with file
    tools can't read or change whatever project ixel happened to start in.
    """
    if not spec or spec == "temp":
        return tempfile.mkdtemp(prefix="ixel-agent-"), True
    if spec == "inherit":
        return None, False
    path = os.path.expanduser(spec)
    if not os.path.isdir(path):
        raise ValueError(f"workdir does not exist: {spec}")
    return path, False


def in_folder(env: dict[str, str], folder: str) -> dict[str, str]:
    """env for a program started in folder, with PWD naming it as a shell would: OpenCode 2 works in the folder
    PWD names, so the folder Ixel was started in (its AGENTS.md, its config, its plugins) would reach it."""
    env["PWD"] = os.path.abspath(folder)
    return env


def remove_workdir(path: str | None, remove: bool) -> None:
    if remove and path:
        shutil.rmtree(path, ignore_errors=True)


@dataclass
class AgentConfig:
    """Configuration for a single agent."""
    name: str               # internal id: "claude", "hermes"
    label: str              # display name e.g. "Claude", "Grok"
    type: str               # transport: "websocket", "subprocess", "acp", "api"
    color: str = "cyan"     # TUI color

    # WebSocket
    url: str = ""
    token: str = ""

    # Subprocess
    command: str = ""
    args: list[str] | None = None
    args_by_version: dict[str, list[str]] | None = None  # in place of args for a major version: {"1": [...]}
    required_args: dict[str, list[str]] | None = None    # args a major version ("*": any other) can't run without

    # Model (for HTTP adapters)
    model: str = ""          # model id e.g. gpt-4o, grok-4

    # Session
    session_key: str = ""    # full gateway session key e.g. agent:main:main
    auto_resume: bool = True
    last_session_id: str = ""

    # Per-call timeout in seconds (0 = DEFAULT_TIMEOUT)
    timeout: float = 0.0

    # Thinking level: minimal | low | medium | high | xhigh | max
    effort: str = ""

    # Command-line agents
    prompt_via: str = "flag"         # "flag" (-q PROMPT), "arg" (-- PROMPT), "stdin", or "auto"
    output_flag: str = ""            # e.g. "-o": the CLI writes its final answer to a file we name
    stdout_format: str = "text"      # one of STDOUT_FORMATS
    workdir: str = "temp"            # "temp" (fresh empty dir), "inherit", or a path
    pass_env: list[str] | None = None  # ixel .env keys this process may see
    env: dict[str, str] | None = None  # fixed settings for the CLI (not secrets)
    drop_env: list[str] | None = None  # removed from its environment, e.g. an API key that would
                                       # otherwise be billed instead of the subscription login
    effort_args: list[str] | None = None    # how the CLI takes a thinking level: ["--effort", "{effort}"]
    effort_levels: list[str] | None = None  # levels it accepts; others map to the nearest one
    model_args: list[str] | None = None     # how the CLI takes a model: ["--model", "{model}"];
                                            # with no model set, the CLI uses its own (current) default

    # How its calls are paid for: "api", "plan", "local" or "unknown" (usage.py); "" = guess
    billing: str = ""

    # What it takes besides text: ["image"], [] for text only; None = what its API is known to take
    accepts: list[str] | None = None

    @property
    def own_timeout(self) -> float | None:
        """The timeout this agent sets for itself, if it sets one."""
        return self.timeout if self.timeout and self.timeout > 0 else None

    @property
    def call_timeout(self) -> float:
        return self.own_timeout or DEFAULT_TIMEOUT

    @property
    def transport_timeout(self) -> float:
        """The transport's own limit: the caller's limit (a review's, say) is the one that counts."""
        return self.own_timeout or UNSET_TRANSPORT_TIMEOUT

    @property
    def sees_pictures(self) -> bool:
        """Whether pictures attached to a question are sent to it. Only HTTP agents: a program
        (Claude Code, Codex…) never gets them."""
        if self.type != "http":
            return False
        if self.accepts is not None:
            return "image" in self.accepts
        from ixel_mat.models import provider_for_url
        return provider_for_url(self.url) in PICTURE_PROVIDERS


class BaseAgent(ABC):
    """Abstract base for all agent transports."""

    def __init__(self, config: AgentConfig):
        self.config = config
        self.name = config.name
        self.label = config.label
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the agent."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Clean up connection."""
        ...

    @abstractmethod
    async def send(self, message: str) -> None:
        """Send a message to the agent (fire and forget)."""
        ...

    @abstractmethod
    async def send_and_receive(self, message: str, **kwargs) -> str:
        """
        Send a message and wait for the full response. Keyword arguments a transport may use,
        and must otherwise ignore: effort= (thinking level for this call), on_text= (async; gets
        the reply as it's written), on_usage= (gets a usage.Usage with the call's tokens,
        when the model reports them) and pictures= (pictures.Picture objects to send along, given
        only to agents whose config sees_pictures).
        """
        ...

    @abstractmethod
    async def listen(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """
        Listen for incoming messages and call callback for each.
        Used by /agent single mode for streaming responses.
        Should run until disconnect.
        """
        ...

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.disconnect()

    def __repr__(self) -> str:
        status = "●" if self.is_connected else "○"
        return f"{status} {self.label} ({self.config.type})"
