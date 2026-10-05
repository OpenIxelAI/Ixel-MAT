"""
Connections: the open pull requests (GitHub, Gitea, Forgejo) or merge requests (GitLab) of a project's
`origin`, for the Board. Read-only: Ixel lists them, and fetches one into the project with your own git
login for a review. Nothing is posted back, and nothing in your project's files changes.

Where a project's code is hosted comes from its `origin` remote. github.com and gitlab.com are known; any
other host is set once, as Gitea (Forgejo too) or GitLab, with the address its web pages are at:

    [connections."git.example.com"]
    kind = "gitea"
    url = "https://git.example.com"

A token is optional for a public repository and needed for a private one. It's saved for one host and port
only, with the keys saved in Ixel, as IXEL_CONN_<HOST>_<PORT>_<CHECK>_TOKEN (secrets.set_live: the programs
Ixel starts never get it; CHECK is a hash of the exact host and port, so two names that only look alike
never share one), and only ever sent to that host's API: over https, or over plain http only to this computer or a
Tailscale address (WireGuard already encrypts those), connecting to the address that was checked and never
through a proxy. A public repository needs no token, so a server on your own network over plain http works
too. Host names are compared in the form DNS uses (an international name as xn--…). Redirects
aren't followed, so a token can't be bounced elsewhere. A read-only token is enough: Ixel only reads.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urljoin, urlparse

from ixel_mat.agents.launch import NO_WINDOW_FLAGS
from ixel_mat.sanitize import sanitize_terminal_text

KINDS = {"github": "GitHub", "gitlab": "GitLab", "gitea": "Gitea"}
KNOWN_HOSTS = {"github.com": "github", "gitlab.com": "gitlab", "codeberg.org": "gitea"}
TIMEOUT = 20.0
FETCH_TIMEOUT = 180.0
MAX_REPLY_BYTES = 4 * 1024 * 1024
MAX_LISTED = 50
_TAILSCALE = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
_PATH_PART = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_LABEL = r"[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?"
_HOST = re.compile(rf"^{_LABEL}(\.{_LABEL})*$")
# What git doesn't allow in a branch name (git check-ref-format), and what a refspec would read as syntax
_REF_BAD = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//|/\.|\.lock(/|$)")


def clean_line(text: object) -> str:
    """One line of text from a host or from git, safe to show."""
    return " ".join(sanitize_terminal_text(text).split())


class ConnectionError_(Exception):
    """Something to tell the person, as it is. `code`: no_origin, unknown_host, needs_token, refused,
    unreachable, not_found, invalid."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ── Where a project's code is ────────────────────────────────────────────────

@dataclass(frozen=True)
class Origin:
    host: str          # lower case, no port
    port: int | None   # the web port when the remote said one (an ssh port isn't)
    scheme: str        # "https" or "http": how the remote reached the host ("https" for ssh)
    path: str          # "owner/repo", or "group/sub/repo" on GitLab

    @property
    def name(self) -> str:
        return self.path


def parse_remote(url: str) -> Origin | None:
    """A git remote URL → Origin; None for a local path or anything that isn't a hosted repository."""
    url = (url or "").strip()
    scp = re.fullmatch(r"(?:[A-Za-z0-9._-]+@)?([A-Za-z0-9.-]+):(?!/)(.+)", url)
    if scp and "://" not in url:
        host, path, port, scheme = scp.group(1), scp.group(2), None, "https"
    else:
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError:
            return None
        if parsed.scheme not in ("https", "http", "ssh", "git+ssh", "ssh+git") or not parsed.hostname:
            return None
        host, path = parsed.hostname, parsed.path
        if parsed.scheme in ("https", "http"):
            scheme = parsed.scheme
        else:
            scheme, port = "https", None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) < 2 or not all(_PATH_PART.fullmatch(p) and p not in (".", "..") for p in parts):
        return None
    host = ascii_host(host)
    return Origin(host, port, scheme, "/".join(parts)) if host else None


def ascii_host(host: str) -> str | None:
    """A host name the way DNS has it: lower-case ASCII, an international name in its xn-- form (so a name
    that only looks like another, gıthub.com say, is never taken for it), an IPv6 address written one way.
    None when it isn't a host name."""
    host = (host or "").strip().rstrip(".")
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        pass
    try:
        name = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    return name if len(name) <= 253 and _HOST.fullmatch(name) else None


def _netloc(host: str, port: int | None) -> str:
    shown = f"[{host}]" if ":" in host else host  # an IPv6 address goes in brackets
    return shown + (f":{port}" if port else "")


def project_origin(root: Path) -> Origin:
    try:
        out = _run(["git", "-C", str(root), "remote", "get-url", "origin"], 10)
    except (OSError, subprocess.TimeoutExpired):
        raise ConnectionError_("no_origin", "git isn't working here, so Ixel can't tell where this project "
                                            "is hosted.") from None
    if out.returncode != 0:
        raise ConnectionError_("no_origin", "This project has no `origin` remote, so there are no pull "
                                            "requests to list.")
    origin = parse_remote(out.stdout.strip())
    if origin is None:
        raise ConnectionError_("no_origin", "This project's `origin` isn't a hosted repository Ixel can read "
                                            "(a folder on this computer, say).")
    return origin


# ── Which kind of host, and where its API is ─────────────────────────────────

@dataclass(frozen=True)
class Host:
    kind: str          # github, gitlab, gitea
    web: str           # "https://git.example.com" (no trailing slash)
    api: str
    token_env: str

    @property
    def label(self) -> str:
        return KINDS[self.kind]


# The start of every name a host's token is saved under (token_env)
TOKEN_PREFIX = "IXEL_CONN_"


def token_env(web: str) -> str:
    """Where a host's token is kept: named for its host and port, so it's never sent anywhere else. The
    readable part can be the same for two hosts (git.example.com, git-example.com); the hash of the exact
    host and port can't."""
    parsed = urlparse(web)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    host = ascii_host(parsed.hostname or "") or ""
    readable = re.sub(r"[^A-Z0-9]", "_", f"{host}_{port}".upper())[:30]
    check = hashlib.sha256(f"{host}:{port}".encode("ascii")).hexdigest()[:16].upper()
    return f"{TOKEN_PREFIX}{readable}_{check}_TOKEN"


def check_web(url: str) -> str:
    """A host's web address as given in settings → its normal form; ConnectionError_ when it isn't one.
    (Plain http is fine for reading; a token only goes over it to this computer or a Tailscale address:
    see _opener.)"""
    try:
        parsed = urlparse((url or "").strip())
        port = parsed.port
    except ValueError:
        parsed, port = None, None
    host = ascii_host(parsed.hostname or "") if parsed is not None else None
    if parsed is None or parsed.scheme not in ("https", "http") or not host or parsed.username \
            or parsed.password or parsed.query or parsed.fragment or parsed.path.strip("/"):
        raise ConnectionError_("invalid", f"{clean_line(url)!r} isn't a web address like "
                                          "https://git.example.com.")
    return f"{parsed.scheme}://{_netloc(host, port)}"


def token_allowed(web: str) -> bool:
    """Whether a token may go to this web address: https, or plain http to this computer or a Tailscale
    address (WireGuard encrypts that)."""
    parsed = urlparse(web)
    return parsed.scheme == "https" or bool(tunnel_addresses(ascii_host(parsed.hostname or "")))


PLAIN_HTTP = ("Ixel sends a token over plain http only to this computer or a Tailscale address, so it won't "
              "send one to {host}. Use its https address, or reach it over Tailscale.")


def tunnel_addresses(host: str | None) -> list[str]:
    """Where plain http to `host` may go: its addresses when every one is this computer or on your Tailscale
    network (WireGuard encrypts what goes there), else none. A name is looked up, *.ts.net too: with
    Tailscale off, those can answer with a public address."""
    if not host:
        return []
    try:
        literal = ipaddress.ip_address(host)
        return [literal.compressed] if literal.is_loopback or _tailscale(literal) else []
    except ValueError:
        pass
    try:
        found = list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, None)))
    except (OSError, UnicodeError):
        return []
    try:
        addresses = [ipaddress.ip_address(a.split("%")[0]) for a in found]
    except ValueError:
        return []
    ok = bool(addresses) and all(a.is_loopback or _tailscale(a) for a in addresses)
    return [a.compressed for a in addresses] if ok else []


def _tailscale(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(address in network for network in _TAILSCALE)


def setting_name(origin: Origin) -> str:
    """The [connections."…"] an origin's host is set up under: its host, and its port when the remote names
    one (two servers on one computer are two hosts)."""
    return origin.host if origin.port is None else _netloc(origin.host, origin.port)


def host_for(origin: Origin, config: Mapping[str, Any]) -> Host:
    """The host an origin is on, from [connections."host"] or the hosts Ixel knows; ConnectionError_
    ("unknown_host") when it has to be set first."""
    section = (config.get("connections") or {}) if isinstance(config.get("connections"), dict) else {}
    found = section.get(setting_name(origin))
    saved = found if isinstance(found, dict) else {}
    kind = str(saved.get("kind") or KNOWN_HOSTS.get(origin.host) or "").strip().lower()
    if kind == "forgejo":
        kind = "gitea"
    if kind not in KINDS:
        raise ConnectionError_("unknown_host", f"Ixel doesn't know what {origin.host} is. Say whether it's Gitea "
                                               "(or Forgejo) or GitLab, and its web address, under Connections.")
    if saved.get("url"):
        web = check_web(str(saved["url"]))
        same_port = origin.port is None or (urlparse(web).port or (443 if web.startswith("https:") else 80)) == origin.port
        if ascii_host(urlparse(web).hostname or "") != origin.host or not same_port:
            raise ConnectionError_("invalid", f"The web address set for {setting_name(origin)} ({web}) is somewhere "
                                              "else. Set it again on the Board.")
    else:
        web = check_web(f"{origin.scheme}://{_netloc(origin.host, origin.port)}")
    if kind == "github":
        api = "https://api.github.com" if origin.host == "github.com" else f"{web}/api/v3"
    elif kind == "gitlab":
        api = f"{web}/api/v4"
    else:
        api = f"{web}/api/v1"
    return Host(kind, web, api, token_env(web))


# ── Reading the host's API ───────────────────────────────────────────────────

class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, PLR0913
        return None


class _PinnedHTTP(urllib.request.HTTPHandler):
    """Plain http to the address that was checked, not to whatever the name resolves to a moment later."""

    def __init__(self, address: str):
        super().__init__()
        self.address = address

    def http_open(self, req):
        address = self.address

        class Connection(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.create_connection((address, self.port), self.timeout)
        return self.do_open(Connection, req)


_HTTPS_OPENER = urllib.request.build_opener(_NoRedirects)  # (through your https proxy, if you have one)


def _opener(url: str, token: str) -> urllib.request.OpenerDirector:
    """https as usual. Plain http goes straight there, never through a proxy; with a token, only to this
    computer or a Tailscale address, pinned to the address that was checked."""
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return _HTTPS_OPENER
    if parsed.scheme != "http":
        raise ConnectionError_("invalid", f"{clean_line(url)[:100]!r} isn't a web address Ixel reads.")
    if not token:
        return urllib.request.build_opener(_NoRedirects, urllib.request.ProxyHandler({}))
    addresses = tunnel_addresses(ascii_host(parsed.hostname or ""))
    if not addresses:
        raise ConnectionError_("invalid", PLAIN_HTTP.format(host=parsed.hostname))
    return urllib.request.build_opener(_NoRedirects, urllib.request.ProxyHandler({}), _PinnedHTTP(addresses[0]))


def _get(host: Host, url: str, token: str) -> Any:
    headers = {"Accept": "application/json", "User-Agent": "Ixel"}
    if host.kind == "github":
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    if token:
        headers["Authorization"] = f"token {token}" if host.kind == "gitea" else f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    opener = _opener(url, token)
    try:
        with opener.open(request, timeout=TIMEOUT) as resp:
            data = resp.read(MAX_REPLY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        if status in (301, 302, 303, 307, 308):
            raise _redirected(host, url, exc.headers.get("Location") or "", status) from None
        if status == 429 or (status == 403 and exc.headers.get("X-RateLimit-Remaining") == "0"):
            raise ConnectionError_("needs_token" if not token else "refused",
                                   f"{host.label} says Ixel has asked too often for now. Try again later"
                                   + (" (GitHub allows 60 an hour without a token; one raises that)." if not token
                                      else ".")) from None
        if status in (401, 403) and not token:
            raise ConnectionError_("needs_token", f"{host.label} wants a token for this repository. Add one "
                                                  "under Connections (read-only access is enough).") from None
        if status == 401:
            raise ConnectionError_("refused", f"{host.label} didn't accept the saved token. Replace it under "
                                              "Connections.") from None
        if status == 403:
            raise ConnectionError_("refused", f"{host.label} refused: the token can't read this repository's "
                                              "pull requests, or you've hit its rate limit.") from None
        if status == 404:
            raise ConnectionError_("not_found" if token else "needs_token",
                                   f"{host.label} says there's no such repository"
                                   + ("." if token else ", which is what it says about a private one without "
                                                        "a token. Add one under Connections.")) from None
        raise ConnectionError_("unreachable", f"{host.label} answered with an error ({status}).") from None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        raise ConnectionError_("unreachable", f"Couldn't reach {urlparse(host.api).hostname}: "
                                              f"{clean_line(str(reason))[:200]}") from None
    if len(data) > MAX_REPLY_BYTES:
        raise ConnectionError_("unreachable", f"{host.label} sent back more than Ixel reads.")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ConnectionError_("unreachable", f"{host.label} sent back something that isn't JSON. Check the web "
                                              "address set for it.") from None


def _redirected(host: Host, url: str, location: str, status: int) -> ConnectionError_:
    """Ixel doesn't follow a redirect (a token mustn't go where it points): say what it most likely means."""
    try:
        asked, there = urlparse(url), urlparse(urljoin(url, location))
        same_place = (there.scheme, there.hostname, there.port) == (asked.scheme, asked.hostname, asked.port)
        where = f"{there.scheme}://{_netloc(ascii_host(there.hostname or '') or '', there.port)}"
    except ValueError:
        same_place, where = False, ""
    if same_place and status in (301, 308):
        return ConnectionError_("moved", f"{host.label} says this repository has moved: it was renamed, or moved to "
                                         "another owner. Point origin at its new address with: git remote set-url "
                                         "origin <its new address>")
    if same_place or not where:
        return ConnectionError_("refused", f"{host.label} sent Ixel somewhere else ({status}), which Ixel doesn't "
                                           "follow.")
    return ConnectionError_("invalid", f"{host.label} sent Ixel to {where}, which Ixel doesn't follow. If that's "
                                       "where it is, set that as its web address.")


def _text(value: object, limit: int = 300) -> str:
    return clean_line(value if isinstance(value, str) else "")[:limit]


def _number(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 10**9 else None


def _ref(value: object) -> str:
    """A branch name as git allows one (any language), else ""."""
    if not isinstance(value, str) or not 0 < len(value) <= 200 or not value.isprintable() or _REF_BAD.search(value):
        return ""
    return "" if value.startswith(("-", "/", ".")) or value.endswith(("/", ".")) or value == "@" else value


def _sha(value: object) -> str:
    return value.lower() if isinstance(value, str) and _SHA.fullmatch(value.lower()) else ""


def _link(value: object, host: Host) -> str:
    """A link to the PR on its host's own site only."""
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlparse(value)
        web = urlparse(host.web)
    except ValueError:
        return ""
    return value if parsed.scheme == web.scheme and parsed.netloc == web.netloc else ""


def pull_requests(origin: Origin, host: Host, token: str = "") -> list[dict]:
    """The origin's open pull/merge requests, newest first, each as
    {number, title, author, head, head_sha, base, draft, fork, url, updated}. `fork`: its branch is in
    another repository (a fork), so pushing to origin doesn't update it."""
    if host.kind == "gitlab":
        url = f"{host.api}/projects/{quote(origin.path, safe='')}/merge_requests?state=opened&per_page={MAX_LISTED}" \
              "&order_by=updated_at"
    elif host.kind == "gitea":
        url = f"{host.api}/repos/{origin.path}/pulls?state=open&limit={MAX_LISTED}&sort=recentupdate"
    else:
        url = f"{host.api}/repos/{origin.path}/pulls?state=open&per_page={MAX_LISTED}&sort=updated&direction=desc"
    data = _get(host, url, token)
    if not isinstance(data, list):
        raise ConnectionError_("unreachable", f"{host.label} sent back something that isn't a list of pull "
                                              "requests.")
    found = []
    for item in data[:MAX_LISTED]:
        if not isinstance(item, dict):
            continue
        if host.kind == "gitlab":
            number, head, sha, base = item.get("iid"), item.get("source_branch"), item.get("sha"), \
                item.get("target_branch")
            author = (item.get("author") or {}).get("username") if isinstance(item.get("author"), dict) else ""
            link, draft = item.get("web_url"), bool(item.get("draft") or item.get("work_in_progress"))
            source, target = item.get("source_project_id"), item.get("target_project_id")
            fork = source is None or target is None or source != target
        else:
            head_info = item.get("head") if isinstance(item.get("head"), dict) else {}
            base_info = item.get("base") if isinstance(item.get("base"), dict) else {}
            number, head, sha, base = item.get("number"), head_info.get("ref"), head_info.get("sha"), \
                base_info.get("ref")
            author = (item.get("user") or {}).get("login") if isinstance(item.get("user"), dict) else ""
            link, draft = item.get("html_url"), bool(item.get("draft"))
            # (a deleted fork's repo is null: not knowing where the branch is counts as a fork)
            head_repo, base_repo = _repo_name(head_info), _repo_name(base_info)
            fork = head_repo is None or base_repo is None or head_repo != base_repo
        if _number(number) is None:
            continue
        found.append({"number": number, "title": _text(item.get("title")), "author": _text(author, 100),
                      "head": _ref(head), "head_sha": _sha(sha), "base": _ref(base), "draft": draft, "fork": fork,
                      "url": _link(link, host), "updated": _text(item.get("updated_at"), 40)})
    return found


def _repo_name(side: dict) -> str | None:
    repo = side.get("repo")
    name = repo.get("full_name") if isinstance(repo, dict) else None
    return name.lower() if isinstance(name, str) else None


# ── Fetching one into the project ────────────────────────────────────────────

def _git_env() -> dict[str, str]:
    """git with no prompt of any kind: the window has nowhere to show one."""
    from ixel_mat.config.secrets import child_env
    env = child_env(nested=False)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_ASKPASS": "",
                "SSH_ASKPASS": "", "SSH_ASKPASS_REQUIRE": "never", "LC_ALL": "C", "GIT_NO_REPLACE_OBJECTS": "1"})
    return env


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    """git without a terminal: in a session of its own (POSIX), so ssh can't ask for a passphrase or a host
    key on the terminal Ixel was started from and wait there; with no console window on Windows."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=_git_env(),
                          stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace",
                          creationflags=NO_WINDOW_FLAGS, start_new_session=os.name != "nt")


def pr_refs(kind: str, number: int) -> tuple[str, str]:
    """The host's ref for a PR's head, and where Ixel keeps it in the project."""
    theirs = f"refs/merge-requests/{number}/head" if kind == "gitlab" else f"refs/pull/{number}/head"
    return theirs, f"refs/ixel/pr/{number}/head"


def fetch(root: Path, kind: str, number: int, base: str) -> dict:
    """The PR's head and its base branch fetched from origin into refs/ixel/pr/N/ (your branches, files and
    remote-tracking refs aren't touched) → {"head": sha, "base": sha, "head_ref", "base_ref"}."""
    if _number(number) is None or not _ref(base) or base.startswith(("-", "refs/")):
        raise ConnectionError_("invalid", "That isn't a pull request Ixel can fetch.")
    theirs, head_ref = pr_refs(kind, number)
    base_ref = f"refs/ixel/pr/{number}/base"
    argv = ["git", "-C", str(root), "-c", "credential.interactive=false", "-c", "core.askPass=",
            "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules", "--refmap=", "origin",
            f"+{theirs}:{head_ref}", f"+refs/heads/{base}:{base_ref}"]
    try:
        out = _run(argv, FETCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ConnectionError_("unreachable", "Fetching it from origin took too long.") from None
    except OSError as exc:
        raise ConnectionError_("unreachable", f"git couldn't start: {exc}") from None
    if out.returncode != 0:
        said = clean_line(out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "")[:300]
        login = any(w in out.stderr.lower() for w in ("authentication", "permission denied", "could not read",
                                                      "terminal prompts disabled", "askpass"))
        raise ConnectionError_("refused" if login else "unreachable",
                               ("git couldn't log in to origin without asking you. Run `git fetch origin` in a "
                                "terminal once (or start your ssh agent), then try again. " if login else
                                "git couldn't fetch it from origin. ") + (f"git said: {said}" if said else ""))
    shas = {}
    for name, ref in (("head", head_ref), ("base", base_ref)):
        try:
            got = _run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], 10)
        except (OSError, subprocess.TimeoutExpired):
            raise ConnectionError_("unreachable", "git fetched it, but Ixel can't read what it fetched.") from None
        shas[name] = got.stdout.strip() if got.returncode == 0 else ""
        if not _sha(shas[name]):
            raise ConnectionError_("unreachable", "git fetched it, but Ixel can't read what it fetched.")
    return {"head": shas["head"], "base": shas["base"], "head_ref": head_ref, "base_ref": base_ref}


# ── Settings ─────────────────────────────────────────────────────────────────

def token_for(host: Host, env: Mapping[str, str] = os.environ) -> str:
    return (env.get(host.token_env) or "").strip()
