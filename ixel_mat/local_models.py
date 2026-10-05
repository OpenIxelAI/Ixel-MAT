"""
Model servers of your own: Ollama, LM Studio and any other server that speaks OpenAI's chat API, on this
computer or another of yours.

Ixel doesn't run models itself; these servers do, and Ixel asks them questions like any other model. It looks
for them only when you ask (`ixel setup`, or Look for servers in Settings): on this computer, at the ports
each server uses out of the box, and on another computer only at the address you type. It never scans your
network. Asking a server what it has sends nothing of yours, and no key goes to one.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlparse

# The port each server listens on out of the box. llama.cpp's llama-server and LocalAI share 8080.
KNOWN_SERVERS: tuple[tuple[str, int], ...] = (
    ("Ollama", 11434),
    ("LM Studio", 1234),
    ("llama.cpp", 8080),
    ("vLLM", 8000),
    ("Jan", 1337),
    ("GPT4All", 4891),
    ("KoboldCpp", 5001),
)
# What a server's model list says about who serves it, when the port alone can't
_OWNED_BY = {"llamacpp": "llama.cpp", "vllm": "vLLM", "localai": "LocalAI"}
LOOK_TIMEOUT = 1.5      # this computer answers at once, or isn't running that server
REMOTE_TIMEOUT = 4.0    # another computer, maybe over Wi-Fi or Tailscale
MAX_MODELS = 300

# Names of models that can't answer a question: embeddings, rerankers, speech and image models.
# A server lists them beside its chat models (Ollama's nomic-embed-text, LM Studio's text-embedding-…).
_NOT_CHAT = re.compile(
    r"embed|rerank|whisper|(^|[/_.:-])(tts|bge|e5|gte|clip|minilm)([/_.:-]|\d|$)|text-to-speech|transcri"
    r"|stable-?diffusion|moderation", re.IGNORECASE)


def is_chat_model(name: str) -> bool:
    return bool(name) and not _NOT_CHAT.search(name)


# ── Addresses ─────────────────────────────────────────────────────────────────

def chat_url(url: str) -> str:
    """
    Where questions go. A model server's address as its app shows it (http://localhost:1234/v1, or just
    http://localhost:11434) gets the rest of the chat address added; a full address is kept as written.
    """
    from ixel_mat.models import provider_for_url
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if parsed.scheme not in ("http", "https") or not parsed.netloc or provider_for_url(url):
        return url
    path = parsed.path.rstrip("/")
    if not path:
        from ixel_mat.agents.base import is_local_network_host
        try:
            if parsed.scheme != "http" or not is_local_network_host(parsed.hostname):
                return url  # only a model server of yours is guessed to be at /v1
        except ValueError:
            return url
        path = "/v1"
    if not re.search(r"/v\d+(beta\d*)?$", path):
        return url
    return parsed._replace(path=path + "/chat/completions").geturl()


def base_url(url: str) -> str:
    """http://127.0.0.1:11434/v1/chat/completions -> http://127.0.0.1:11434/v1"""
    url = chat_url(url).rstrip("/")
    return url[: -len("/chat/completions")] if url.endswith("/chat/completions") else url


def server_root(url: str) -> str:
    """http://127.0.0.1:11434/v1/chat/completions -> http://127.0.0.1:11434"""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _private_ip(text: str) -> bool:
    from ixel_mat.usage import _PRIVATE_NETWORKS
    try:
        address = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback or any(address in network for network in _PRIVATE_NETWORKS)


def where_is(host: str | None, resolve=socket.getaddrinfo) -> str:
    """
    "yours" if host is this computer or one on your own network: a loopback or private address, a Tailscale
    one, or a name that leads only to such addresses (mac-mini, nas.local, mac-mini.tail1234.ts.net). A
    home network's name (mac-mini, nas.local) can also have a public IPv6 address beside its private IPv4
    one; it's still yours. "elsewhere" if it isn't, "unknown" if the name leads nowhere.
    """
    from ixel_mat.usage import is_private_host
    host = (host or "").strip().strip("[]").rstrip(".").lower()
    if not host:
        return "unknown"
    if host == "localhost" or _private_ip(host):
        return "yours"
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return "elsewhere"  # an address, and not one of yours
    except ValueError:
        pass
    try:
        found = {info[4][0] for info in resolve(host, None, type=socket.SOCK_STREAM)}
    except (OSError, UnicodeError):
        return "unknown"
    if not found:
        return "unknown"
    if all(_private_ip(str(a)) for a in found):
        return "yours"
    # A home name with a public IPv6 address besides: every IPv4 one, including any carried inside an IPv6
    # address, has to be yours
    v4 = [a for a in (_ipv4_in(str(a)) for a in found) if a is not None]
    if is_private_host(host) and v4 and all(_private_ip(str(a)) for a in v4):
        return "yours"
    return "elsewhere"


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _ipv4_in(text: str) -> ipaddress.IPv4Address | None:
    """The IPv4 address text is, or that an IPv6 one carries (mapped, 6to4 or NAT64); None for plain IPv6."""
    try:
        address = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv4Address):
        return address
    if address.ipv4_mapped or address.sixtofour:
        return address.ipv4_mapped or address.sixtofour
    if address in _NAT64:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return None


class AddressError(ValueError):
    pass


# Said wherever Ixel tells you to let other computers in to a model server
OPEN_TO_NETWORK = ("Out of the box neither asks for a password, so anyone on that network can then use it: only do "
                   "this on a computer that stays on your home network.")


def usable_name(host: str) -> None:
    """
    AddressError unless the rest of Ixel also takes host for your own network. That's told from the name
    alone, as a question goes out: an address, a name with no dots (mac-mini), or one ending in .local, .lan,
    .internal, .home.arpa or .ts.net. A router's own names (mac-mini.fritz.box) lead home too, but can't be
    told from a company's by their look.
    """
    from ixel_mat.agents.base import is_local_network_host
    if not is_local_network_host(host):
        raise AddressError(f"{host} is on your network, but Ixel can't tell that from its name each time it asks "
                           "a question. Use its address (like 192.168.1.20), or its short name (like mac-mini) "
                           "or Tailscale name.")


def addresses_to_try(text: str) -> list[tuple[str, str]]:
    """
    (name, base url) to try for an address someone typed: mac-mini, mac-mini:1234, 192.168.1.20,
    http://mac-mini:1234/v1… With no port, each server's own port is tried.
    """
    text = (text or "").strip()
    if not text or len(text) > 300 or any(c.isspace() for c in text):
        raise AddressError("Type the computer's name or address, like mac-mini or 192.168.1.20:1234.")
    if "://" not in text:
        try:
            ipaddress.ip_address(text.split("%", 1)[0])
            text = f"[{text}]" if ":" in text else text  # an IPv6 address typed without brackets
        except ValueError:
            pass
        text = "http://" + text
    try:
        parsed = urlparse(text)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise AddressError("That isn't an address Ixel can use. Try mac-mini or 192.168.1.20:1234.") from None
    if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password \
            or parsed.query or parsed.fragment:
        raise AddressError("That isn't an address Ixel can use. Try mac-mini or 192.168.1.20:1234.")
    shown = f"[{host}]" if ":" in host else host
    path = parsed.path.rstrip("/") or "/v1"  # at_address checks the computer is yours before anything's asked
    if port is not None:
        name = next((n for n, p in KNOWN_SERVERS if p == port), "Model server")
        return [(name, base_url(f"{parsed.scheme}://{shown}:{port}{path}"))]
    if parsed.path.rstrip("/") or parsed.scheme == "https":
        return [("Model server", base_url(f"{parsed.scheme}://{shown}{path}"))]
    return [(name, f"http://{shown}:{p}/v1") for name, p in KNOWN_SERVERS]


def stays_on_your_computers(cfg) -> bool:
    """
    Whether a question to this agent stays on your own computers: a model server Ixel calls itself, at an
    address on this computer or your own network (a private or Tailscale address, nas.local, a bare name).
    Not a program such as OpenCode (Ixel can't tell where it sends things), not one of Ollama's cloud models
    (they run on ollama.com), and not a server that takes a key or is marked billing = "api": that's most
    likely a gateway (LiteLLM and such) that passes questions on to a company.
    """
    from ixel_mat.agents.base import is_local_network_host
    from ixel_mat.models import provider_for_url
    from ixel_mat.usage import runs_elsewhere
    if cfg.type != "http" or not isinstance(cfg.url, str) or getattr(cfg, "billing", "") == "api" or cfg.token \
            or runs_elsewhere(cfg.model if isinstance(cfg.model, str) else ""):
        return False
    try:  # a malformed address (http://[bad) is reported when connecting; it isn't yours meanwhile
        return not provider_for_url(cfg.url) and is_local_network_host(urlparse(cfg.url).hostname)
    except ValueError:
        return False


# ── Getting a model through Ollama ────────────────────────────────────────────

# Ollama's names: llama3.2, qwen3:14b, library/qwen3:8b, hf.co/user/repo:Q4_K_M
_OLLAMA_NAME = re.compile(r"^(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._/-]{0,150}(:[A-Za-z0-9._-]{1,80})?$")
# Ollama says how far along it is several times a second while it downloads, but says nothing while it checks
# the finished file, which for a big model on a slow disk can take many minutes
PULL_READ_TIMEOUT = 1800.0


class PullError(Exception):
    pass


def model_to_get(name) -> str:
    """The name, if it's one Ollama can download and run on your computer; PullError if not."""
    name = name.strip() if isinstance(name, str) else ""
    if not _OLLAMA_NAME.match(name):
        raise PullError("Type a model's name as ollama.com/library shows it, like qwen3:8b.")
    from ixel_mat.usage import runs_elsewhere
    if runs_elsewhere(name):
        raise PullError(f"{name} runs on ollama.com, not on your computer, so it isn't one to get here.")
    return name


async def pull(root: str, name: str, read_timeout: float = PULL_READ_TIMEOUT):
    """
    Has the Ollama at root (http://127.0.0.1:11434) download a model from ollama.com, yielding how far along
    it is: {"status", "completed", "total"}. Ixel talks only to your Ollama, straight (no proxy, no
    redirect); Ollama does the download. Stopping early (the page closed) closes the connection, which
    stops Ollama too, and a later pull carries on from where it stopped.
    """
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=REMOTE_TIMEOUT, sock_read=read_timeout)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
        async with session.post(f"{root}/api/pull", json={"model": name, "name": name, "stream": True},
                                allow_redirects=False) as resp:
            if resp.status != 200:
                said = _ollama_error(await resp.content.read(4000))
                raise PullError(f"Ollama couldn't get {name}" + (f": {said}" if said else f" ({resp.status})."))
            status = ""
            async for line in resp.content:
                if not line.strip():
                    continue
                try:
                    step = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(step, dict):
                    continue
                if step.get("error"):
                    raise PullError(f"Ollama couldn't get {name}: {str(step['error'])[:300]}")
                status = str(step.get("status", ""))[:200]
                yield {"status": status, "completed": _count(step.get("completed")), "total": _count(step.get("total"))}
            if status != "success":  # it stopped without saying the model is there
                raise PullError(f"Ollama stopped before {name} was ready. Get it again to carry on.")


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _ollama_error(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return ""
    return str(data.get("error", ""))[:300] if isinstance(data, dict) else ""


# ── Asking a server what it has ───────────────────────────────────────────────

@dataclass
class Server:
    name: str
    base: str                                         # http://127.0.0.1:11434/v1
    models: list[str] = field(default_factory=list)   # the ones that can answer questions
    hidden: list[str] = field(default_factory=list)   # embeddings and such, which can't
    ollama: bool = False                              # can get more models (ollama pull)
    elsewhere: list[str] = field(default_factory=list)  # Ollama's cloud models: listed, but run on ollama.com

    def has(self, model: str) -> bool:
        """Whether it serves this model (Ollama's llama3.2 is llama3.2:latest)."""
        return model in self.models or (":" not in model and f"{model}:latest" in self.models)

    def to_dict(self) -> dict:
        return {"name": self.name, "base": self.base, "models": self.models, "hidden": self.hidden,
                "elsewhere": self.elsewhere, "ollama": self.ollama, "here": _here(self.base)}


def _here(base: str) -> bool:
    from ixel_mat.agents.base import is_loopback_host
    try:
        return is_loopback_host(urlparse(base).hostname)
    except ValueError:
        return False


def _get(url: str, timeout: float):
    """Straight to the server (never through a proxy, which would see where your servers are), and a
    redirect is an error: a model server doesn't send you elsewhere."""
    from ixel_mat.config.setup import _DIRECT_OPENER
    with _DIRECT_OPENER.open(urllib.request.Request(url, headers={"Accept": "application/json"}),
                             timeout=timeout) as resp:
        return json.loads(resp.read(2_000_000).decode("utf-8"))


def ask_server(name: str, base: str, timeout: float = LOOK_TIMEOUT) -> Server | None:
    """What the server at base has, or None when nothing that lists models answers there."""
    try:
        data = _get(f"{base}/models", timeout)
    except Exception:  # noqa: BLE001 — nothing there, or not a model server
        return None
    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return None
    ids = [e.get("id") for e in entries if isinstance(e, dict)]
    ids = list(dict.fromkeys(i.strip() for i in ids if isinstance(i, str) and i.strip()))[:MAX_MODELS]
    owners = {str(e.get("owned_by", "")).lower() for e in entries if isinstance(e, dict)}
    name = next((_OWNED_BY[o] for o in owners if o in _OWNED_BY), name)
    ollama = _is_ollama(server_root(base), timeout)
    if ollama:
        name = "Ollama"
    from ixel_mat.usage import runs_elsewhere
    chat = [i for i in ids if is_chat_model(i)]
    return Server(name, base, [i for i in chat if not runs_elsewhere(i)], [i for i in ids if not is_chat_model(i)],
                  ollama, [i for i in chat if runs_elsewhere(i)])


def _is_ollama(root: str, timeout: float) -> bool:
    try:
        data = _get(f"{root}/api/version", timeout)
    except Exception:  # noqa: BLE001
        return False
    return isinstance(data, dict) and isinstance(data.get("version"), str)


def look(candidates: list[tuple[str, str]], timeout: float) -> list[Server]:
    """Every candidate that answers, asked side by side, in the order given."""
    if not candidates:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
        found = list(pool.map(lambda c: ask_server(c[0], c[1], timeout), candidates))
    servers, seen = [], set()
    for server in found:
        if server is not None and server.base not in seen:
            seen.add(server.base)
            servers.append(server)
    return servers


def on_this_computer(timeout: float = LOOK_TIMEOUT) -> list[Server]:
    """The servers running on this computer, at each one's own port."""
    return look([(name, f"http://127.0.0.1:{port}/v1") for name, port in KNOWN_SERVERS], timeout)


def at_address(text: str, timeout: float = REMOTE_TIMEOUT, resolve=socket.getaddrinfo) -> list[Server]:
    """The servers at an address someone typed. AddressError when it isn't one of theirs, or nothing answers
    (saying what to turn on)."""
    candidates = addresses_to_try(text)
    host = urlparse(candidates[0][1]).hostname
    place = where_is(host, resolve)
    if place == "unknown":
        raise AddressError(f"Ixel can't find a computer called {host}. Check the name, or use its address "
                           "(like 192.168.1.20, or its Tailscale address).")
    if place != "yours":
        raise AddressError(f"{host} isn't on this computer or your own network, so Ixel won't look there. "
                           "A server elsewhere goes in your settings file by hand.")
    usable_name(host)
    servers = look(candidates, timeout)
    if servers:
        return servers
    if _here(candidates[0][1]):
        raise AddressError("No model server answered on this computer. Start Ollama or LM Studio's server, "
                           "then look again.")
    where = "at that port" if len(candidates) == 1 else "on the usual ports"
    raise AddressError(f"Nothing answered at {host} {where}. On that computer, let other computers in: in "
                       "LM Studio, turn on Serve on Local Network (Developer tab); for Ollama, set "
                       "OLLAMA_HOST=0.0.0.0 and restart it. Its firewall has to allow the port too. " + OPEN_TO_NETWORK)
