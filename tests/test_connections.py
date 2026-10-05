"""Connections: a project's open pull requests, read from where its origin is hosted (connections.py)."""
import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from ixel_mat import connections
from ixel_mat.connections import ConnectionError_, Origin, check_web, host_for, parse_remote, pull_requests


@pytest.mark.parametrize("url, origin", [
    ("https://github.com/OpenIxelAI/ixel-mat.git", Origin("github.com", None, "https", "OpenIxelAI/ixel-mat")),
    ("git@github.com:OpenIxelAI/ixel-mat.git", Origin("github.com", None, "https", "OpenIxelAI/ixel-mat")),
    ("ssh://git@macmini:2222/robin/store.git", Origin("macmini", None, "https", "robin/store")),
    ("http://macmini:3000/robin/store.git", Origin("macmini", 3000, "http", "robin/store")),
    ("https://gitlab.com/group/sub/repo", Origin("gitlab.com", None, "https", "group/sub/repo")),
    ("https://token@GitHub.com/a/b/", Origin("github.com", None, "https", "a/b")),
])
def test_where_a_project_is_hosted(url, origin):
    assert parse_remote(url) == origin


@pytest.mark.parametrize("url", ["/srv/git/shop.git", "file:///srv/git/shop.git", "C:/code/shop", "https://github.com/a",
                                 "https://github.com/a/../b", "git@github.com:a/b c.git", "ftp://example.com/a/b"])
def test_what_isnt_a_hosted_repository(url):
    assert parse_remote(url) is None


def test_known_hosts_and_set_ones():
    github = host_for(Origin("github.com", None, "https", "a/b"), {})
    assert (github.kind, github.api) == ("github", "https://api.github.com")
    assert re.fullmatch(r"IXEL_CONN_GITHUB_COM_443_[0-9A-F]{16}_TOKEN", github.token_env)
    gitlab = host_for(Origin("gitlab.com", None, "https", "g/s/r"), {})
    assert gitlab.api == "https://gitlab.com/api/v4"
    with pytest.raises(ConnectionError_) as unknown:
        host_for(Origin("git.example.com", None, "https", "a/b"), {})
    assert unknown.value.code == "unknown_host"
    config = {"connections": {"git.example.com": {"kind": "forgejo", "url": "https://git.example.com:8443"}}}
    forgejo = host_for(Origin("git.example.com", None, "https", "a/b"), config)
    assert (forgejo.kind, forgejo.api) == ("gitea", "https://git.example.com:8443/api/v1")
    assert forgejo.token_env.startswith("IXEL_CONN_GIT_EXAMPLE_COM_8443_")


def test_a_token_only_goes_where_it_was_saved_for():
    # Its name comes from the host and port, so pointing the host's address elsewhere leaves it behind
    elsewhere = {"connections": {"git.example.com": {"kind": "gitea", "url": "https://evil.example"}}}
    with pytest.raises(ConnectionError_, match="somewhere else"):
        host_for(Origin("git.example.com", None, "https", "a/b"), elsewhere)
    assert connections.token_env("https://git.example.com") != connections.token_env("https://git.example.com:8443")
    for bad in ("ftp://git.example.com", "https://user:pw@git.example.com", "https://git.example.com/sub",
                "https://git.example.com?x=1", "javascript:alert(1)", ""):
        with pytest.raises(ConnectionError_):
            check_web(bad)


def test_a_token_goes_over_plain_http_only_to_this_computer_or_tailscale(monkeypatch):
    for web in ("http://127.0.0.1:3000", "http://100.101.102.103:3000", "http://[fd7a:115c:a1e0::5]:3000"):
        assert check_web(web) == web and connections.token_allowed(web)
    monkeypatch.setattr(connections.socket, "getaddrinfo", lambda host, port: [(0, 0, 0, "", ("100.70.1.2", 0))])
    assert connections.token_allowed("http://macmini:3000")  # a MagicDNS name
    assert connections.token_allowed("http://macmini.tail1234.ts.net:3000")
    monkeypatch.setattr(connections.socket, "getaddrinfo", lambda host, port: [(0, 0, 0, "", ("192.168.1.20", 0))])
    # A server on your Wi-Fi: listed without a token (a public repository needs none), but no token goes there,
    # since your Wi-Fi isn't encrypted the way a tailnet is
    assert check_web("http://nas:3000") == "http://nas:3000" and not connections.token_allowed("http://nas:3000")
    assert connections._opener("http://nas:3000/api/v1/x", "")
    with pytest.raises(ConnectionError_, match="Tailscale"):
        connections._opener("http://nas:3000/api/v1/x", "tok")
    monkeypatch.setattr(connections.socket, "getaddrinfo", lambda host, port: [(0, 0, 0, "", ("203.0.113.9", 0))])
    with pytest.raises(ConnectionError_, match="Tailscale"):
        connections._opener("http://macmini.tail1234.ts.net:3000/api/v1/x", "tok")  # Tailscale off: its public address
    assert connections.token_allowed("https://git.example.com")


def test_a_public_repository_on_your_network_is_listed_over_plain_http(monkeypatch):
    with FakeHost(reply=[GITEA_PR]) as fake:
        port = fake.server.server_port
        monkeypatch.setattr(connections, "tunnel_addresses", lambda host: [])  # as if 127.0.0.1 were your Wi-Fi
        origin = Origin("127.0.0.1", port, "http", "robin/shop")
        host = host_for(origin, {"connections": {f"127.0.0.1:{port}": {"kind": "gitea"}}})
        assert [pr["number"] for pr in pull_requests(origin, host, "")] == [7]
        with pytest.raises(ConnectionError_, match="won't send one"):
            pull_requests(origin, host, "tok-1")
    assert "Authorization" not in fake.requests[0]["headers"] and len(fake.requests) == 1


def test_redirects_are_said_plainly(monkeypatch):
    for location, code, said in (("/api/v1/repos/robin/shop-new/pulls", 301, "moved"),
                                 ("https://git.example.com/api/v1/repos/robin/shop/pulls", 302, "invalid"),
                                 ("/login", 302, "refused")):
        with FakeHost(reply=[], status=code, headers={"Location": location}) as fake:
            origin, host = _host(fake, "gitea")
            with pytest.raises(ConnectionError_) as error:
                pull_requests(origin, host, "")
        assert error.value.code == said, (location, str(error.value))
        if said == "moved":
            assert "renamed" in str(error.value) and "git remote set-url origin" in str(error.value)


def test_a_broken_host_cant_crash_the_listing():
    deep = [{"number": 7, "title": "x", "html_url": "https://[x", "head": {}, "base": {}}]
    with FakeHost(reply=deep) as fake:
        origin, host = _host(fake, "gitea")
        [found] = pull_requests(origin, host, "")
    assert found["url"] == "" and found["fork"] is True  # where its branch is wasn't said: no push line
    with FakeHost(raw=b"[" * 100_000 + b"]" * 100_000) as fake:
        origin, host = _host(fake, "gitea")
        with pytest.raises(ConnectionError_, match="isn't JSON"):
            pull_requests(origin, host, "")


def test_a_reply_nested_past_the_depth_limit_isnt_read():
    """Deep enough to stop at MAX_JSON_DEPTH on every Python, not only where json runs out of stack."""
    deep = connections.MAX_JSON_DEPTH + 1
    with FakeHost(raw=b"[" * deep + b"]" * deep) as fake:
        origin, host = _host(fake, "gitea")
        with pytest.raises(ConnectionError_, match="isn't JSON"):
            pull_requests(origin, host, "")
    assert not connections._too_deep(json.loads('{"a":' * (connections.MAX_JSON_DEPTH - 1) + "1"
                                                + "}" * (connections.MAX_JSON_DEPTH - 1)))  # just under still reads


def test_names_that_only_look_alike_never_share_a_token():
    real = connections.token_env("https://github.com")
    assert parse_remote("https://gıthub.com/me/x").host == "xn--gthub-n4a.com"  # shown as it is, too
    for other in ("https://xn--gthub-n4a.com", "https://github.com:8443", "https://github.co"):
        assert connections.token_env(other) != real
    assert connections.token_env("https://gitlab.corp.com") != connections.token_env("https://gitlab-corp.com")
    assert connections.token_env("https://GitHub.com.") == real  # the same name, written differently
    assert parse_remote("http://[fd7a:115c:a1e0::5]:3000/robin/shop.git") == \
        Origin("fd7a:115c:a1e0::5", 3000, "http", "robin/shop")
    assert parse_remote("https://exa mple.com/a/b") is None


def test_two_servers_on_one_computer_are_two_hosts():
    config = {"connections": {"macmini:3000": {"kind": "gitea", "url": "https://macmini:3000"}}}
    assert host_for(Origin("macmini", 3000, "https", "a/b"), config).api == "https://macmini:3000/api/v1"
    with pytest.raises(ConnectionError_) as other:
        host_for(Origin("macmini", 3001, "https", "a/b"), config)
    assert other.value.code == "unknown_host"
    with pytest.raises(ConnectionError_, match="somewhere else"):
        host_for(Origin("macmini", 3001, "https", "a/b"),
                 {"connections": {"macmini:3001": {"kind": "gitea", "url": "https://macmini:3000"}}})
    assert connections.setting_name(Origin("macmini", None, "https", "a/b")) == "macmini"  # an ssh remote


class FakeHost:
    """A GitHub/Gitea/GitLab API on 127.0.0.1 that records what it's asked."""

    def __init__(self, status=200, reply=None, headers=None, raw=None):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.requests.append({"path": self.path, "headers": dict(self.headers)})
                data = raw if raw is not None else json.dumps(reply if reply is not None else []).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.web = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _host(fake, kind):
    origin = Origin("127.0.0.1", fake.server.server_port, "http", "robin/shop")
    return origin, host_for(origin, {"connections": {f"127.0.0.1:{origin.port}": {"kind": kind}}})


GITEA_PR = {"number": 7, "title": "Pay\nby card", "user": {"login": "ada"}, "draft": False,
            "head": {"ref": "card", "sha": "a" * 40, "repo": {"full_name": "ada/shop"}},
            "base": {"ref": "main", "repo": {"full_name": "robin/shop"}}, "updated_at": "2026-10-03T09:00:00Z"}


def test_open_pull_requests_are_listed(monkeypatch):
    with FakeHost(reply=[GITEA_PR, {"number": "x"}, "junk"]) as fake:
        origin, host = _host(fake, "gitea")
        found = pull_requests(origin, host, "tok-1")
    assert found == [{"number": 7, "title": "Pay by card", "author": "ada", "head": "card", "head_sha": "a" * 40,
                      "base": "main", "draft": False, "fork": True, "url": "", "updated": "2026-10-03T09:00:00Z"}]
    [asked] = fake.requests
    assert asked["path"].startswith("/api/v1/repos/robin/shop/pulls?state=open")
    assert asked["headers"]["Authorization"] == "token tok-1"


def test_gitlab_merge_requests_are_listed():
    mr = {"iid": 3, "title": "Fix", "author": {"username": "bo"}, "source_branch": "fix", "sha": "b" * 40,
          "source_project_id": 12, "target_project_id": 12,
          "target_branch": "main", "draft": True, "web_url": "https://elsewhere.example/x"}
    with FakeHost(reply=[mr]) as fake:
        origin, host = _host(fake, "gitlab")
        [found] = pull_requests(origin, host, "glpat-1")
    assert (found["number"], found["head"], found["base"], found["draft"], found["fork"]) == (3, "fix", "main", True, False)
    assert found["url"] == ""  # a link only to the host's own site
    assert fake.requests[0]["path"].startswith("/api/v4/projects/robin%2Fshop/merge_requests?state=opened")
    assert fake.requests[0]["headers"]["Authorization"] == "Bearer glpat-1"


@pytest.mark.parametrize("status, token, code, words", [
    (404, "", "needs_token", "Add one under Connections"),
    (401, "", "needs_token", "wants a token"),
    (401, "t", "refused", "didn't accept the saved token"),
    (404, "t", "not_found", "no such repository"),
    (500, "t", "unreachable", "(500)"),
])
def test_what_the_host_says_is_told_plainly(status, token, code, words):
    with FakeHost(status=status, reply={"message": "nope"}) as fake:
        origin, host = _host(fake, "gitea")
        with pytest.raises(ConnectionError_) as caught:
            pull_requests(origin, host, token)
    assert caught.value.code == code and words in str(caught.value)


def test_plain_http_goes_straight_to_the_address_checked(monkeypatch):
    """Never through a proxy, and never to whatever the name says a moment later."""
    real = connections.socket.getaddrinfo
    asked = []

    def lookup(host, port, *args, **kwargs):
        if host == "macmini":
            asked.append(host)
            return [(0, 0, 0, "", ("127.0.0.1" if len(asked) == 1 else "203.0.113.9", 0))]
        return real(host, port, *args, **kwargs)
    monkeypatch.setattr(connections.socket, "getaddrinfo", lookup)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")  # nothing there: a request through it would fail
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    with FakeHost(reply=[GITEA_PR]) as fake:
        origin = Origin("macmini", fake.server.server_port, "http", "robin/shop")
        host = host_for(origin, {"connections": {f"macmini:{fake.server.server_port}": {"kind": "gitea"}}})
        asked.clear()
        assert [pr["number"] for pr in pull_requests(origin, host, "tok-1")] == [7]
    assert asked == ["macmini"] and fake.requests[0]["headers"]["Host"] == f"macmini:{fake.server.server_port}"


def test_something_that_isnt_a_web_server_is_said_plainly():
    import socket as sockets
    listener = sockets.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def answer():
        conn, _ = listener.accept()
        conn.recv(1024)
        conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
        conn.close()
    threading.Thread(target=answer, daemon=True).start()
    origin = Origin("127.0.0.1", listener.getsockname()[1], "http", "robin/shop")
    host = host_for(origin, {"connections": {f"127.0.0.1:{origin.port}": {"kind": "gitea"}}})
    with pytest.raises(ConnectionError_) as caught:
        pull_requests(origin, host, "")
    listener.close()
    assert caught.value.code == "unreachable"


def test_branch_names_in_any_language_and_only_those_git_allows():
    assert connections._ref("ünïcode-zweig") == "ünïcode-zweig" and connections._ref("feature/x") == "feature/x"
    for bad in ("a b", "a..b", "-x", "x:y", "x^", "x~1", "x*", "refs@{0}", "x.lock", "a/.b", "x/", "", "\u202ex"):
        assert connections._ref(bad) == "", bad


def test_git_runs_without_a_terminal_or_a_window(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 1, "", "")
    monkeypatch.setattr(connections.subprocess, "run", run)
    with pytest.raises(ConnectionError_):
        connections.project_origin(Path("."))
    assert seen["stdin"] == subprocess.DEVNULL and seen["start_new_session"] is (os.name != "nt")
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0" and seen["env"]["SSH_ASKPASS_REQUIRE"] == "never"
    assert "creationflags" in seen


def test_a_redirect_isnt_followed():
    with FakeHost(status=302, headers={"Location": "https://evil.example/steal"}) as fake:
        origin, host = _host(fake, "gitea")
        with pytest.raises(ConnectionError_, match="doesn't follow"):
            pull_requests(origin, host, "tok-1")
    assert len(fake.requests) == 1


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=Ada", "-c", "user.email=ada@example.com", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


def test_a_pull_request_is_fetched_without_touching_your_work(tmp_path):
    hosted = tmp_path / "hosted"
    _git(tmp_path, "init", "-q", "-b", "main", str(hosted))
    (hosted / "a.txt").write_text("one\n", encoding="utf-8")
    _git(hosted, "add", "-A")
    _git(hosted, "commit", "-q", "-m", "one")
    base = _git(hosted, "rev-parse", "HEAD")
    _git(hosted, "switch", "-q", "-c", "card")
    (hosted / "a.txt").write_text("two\n", encoding="utf-8")
    _git(hosted, "commit", "-q", "-am", "two")
    head = _git(hosted, "rev-parse", "HEAD")
    _git(hosted, "update-ref", "refs/pull/7/head", head)  # how GitHub and Gitea publish a pull request
    _git(hosted, "switch", "-q", "main")
    mine = tmp_path / "mine"
    _git(tmp_path, "clone", "-q", str(hosted), str(mine))
    (mine / "a.txt").write_text("my own edit\n", encoding="utf-8")
    _git(hosted, "commit", "-q", "--allow-empty", "-m", "later on main")  # (fetching the base mustn't move origin/main)
    branches = _git(mine, "for-each-ref", "refs/heads", "refs/remotes", "refs/tags")

    found = connections.fetch(mine, "gitea", 7, "main")
    base = _git(hosted, "rev-parse", "main")
    assert found == {"head": head, "base": base, "head_ref": "refs/ixel/pr/7/head", "base_ref": "refs/ixel/pr/7/base"}
    assert (mine / "a.txt").read_text(encoding="utf-8") == "my own edit\n"
    assert _git(mine, "branch", "--show-current") == "main"
    assert _git(mine, "for-each-ref", "refs/heads", "refs/remotes", "refs/tags") == branches
    with pytest.raises(ConnectionError_) as missing:
        connections.fetch(mine, "gitea", 8, "main")
    assert missing.value.code == "unreachable" and "git said" in str(missing.value)
    for number, base_ref in ((0, "main"), (7, "--upload-pack=x"), (7, "refs/heads/main"), (7, "a..b")):
        with pytest.raises(ConnectionError_, match="isn't a pull request"):
            connections.fetch(mine, "gitea", number, base_ref)
