"""`kill` (SIGTERM) and closing the terminal (SIGHUP) stop Ixel the way Ctrl+C does: the model programs it
started stop too, and the app's launch file (which holds its key) goes."""
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

from test_security_hardening import _HANG, _wait_dead

pytestmark = pytest.mark.skipif(os.name != "posix", reason="SIGTERM and SIGHUP are POSIX signals")


def slow_panel(home, pid_file):
    """A config whose one agent starts a helper, writes both pids to pid_file, then hangs."""
    cfg_dir = home / ".config" / "ixel-mat"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text(
        f'[agents.slow]\ntype = "oneshot"\ncommand = {json.dumps(sys.executable)}\n'
        f'args = ["-c", {json.dumps(_HANG)}, {json.dumps(str(pid_file))}]\nprompt_via = "stdin"\n'
        'label = "Slow"\ntimeout = 120\n')


def start_ixel(home, *args):
    tmp = home / "tmp"
    tmp.mkdir(exist_ok=True)
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "TMPDIR": str(tmp), "BROWSER": "/bin/true",
           "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    return subprocess.Popen([sys.executable, "-m", "ixel_mat", *args], cwd=home, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def pids_from(pid_file, seconds=30.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if pid_file.exists() and len(pid_file.read_text().split()) == 2:
            return [int(p) for p in pid_file.read_text().split()]
        time.sleep(0.05)
    raise AssertionError("the agent never started")


# By name: Windows has no SIGHUP, and collecting this file there mustn't fail (it's skipped there)
@pytest.mark.parametrize("sig", ["SIGTERM", "SIGHUP"])
def test_a_killed_review_ends_the_model_programs_it_started(tmp_path, sig):
    pid_file = tmp_path / "pids"
    slow_panel(tmp_path, pid_file)
    ixel = start_ixel(tmp_path, "review", "--quick", "--json", "What is 17 x 23?")
    try:
        pids = pids_from(pid_file)
        ixel.send_signal(getattr(signal, sig))
        ixel.wait(timeout=15)
    finally:
        if ixel.poll() is None:
            ixel.kill()
    assert ixel.returncode == 130 and "Traceback" not in ixel.stdout.read()
    assert all(_wait_dead(pid, 5) for pid in pids), "a model program was left running"


def test_a_killed_app_stops_its_review_and_removes_its_launch_file(tmp_path):
    pid_file = tmp_path / "pids"
    slow_panel(tmp_path, pid_file)
    ixel = start_ixel(tmp_path, "gui")
    try:
        url = None
        deadline = time.monotonic() + 30
        while url is None and time.monotonic() < deadline:
            line = ixel.stdout.readline()
            found = re.search(r"(http://127\.0\.0\.1:\d+)/#token=(\S+)", line)
            if found:
                url = found.groups()
        assert url, "ixel gui printed no address"
        base, token = url
        deadline = time.monotonic() + 10
        while not list((tmp_path / "tmp").glob("ixel-gui-*.html")) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert list((tmp_path / "tmp").glob("ixel-gui-*.html"))  # the browser was handed one

        def review():
            request = urllib.request.Request(f"{base}/api/review", data=json.dumps({"question": "q"}).encode(),
                                             headers={"Authorization": f"Bearer {token}",
                                                      "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=60) as answer:
                    answer.read()
            except OSError:
                pass

        threading.Thread(target=review, daemon=True).start()
        pids = pids_from(pid_file)
        started = time.monotonic()
        ixel.send_signal(signal.SIGTERM)
        ixel.wait(timeout=15)
        assert time.monotonic() - started < 10, "the server waited for the review to finish"
    finally:
        if ixel.poll() is None:
            ixel.kill()
    assert all(_wait_dead(pid, 5) for pid in pids), "a model program was left running"
    assert not list((tmp_path / "tmp").glob("ixel-gui-*.html")), "the launch file, with its key, was left behind"


def test_a_killed_plugin_server_exits_even_with_its_stdin_open(tmp_path):
    """The MCP SDK reads stdin in a thread that can't be stopped: a host that kills the server without
    closing its stdin mustn't leave it running."""
    env = {**os.environ, "HOME": str(tmp_path), "PYTHONUNBUFFERED": "1", "IXEL_NO_UPDATE_CHECK": "1"}
    server = subprocess.Popen([sys.executable, "-m", "ixel_mat", "mcp"], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=tmp_path)
    try:
        hello = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        server.stdin.write((json.dumps(hello) + "\n").encode())
        server.stdin.flush()
        assert b'"id":1' in server.stdout.readline().replace(b" ", b"")
        server.send_signal(signal.SIGTERM)
        server.wait(timeout=15)
    finally:
        if server.poll() is None:
            server.kill()
    assert server.returncode == 130


def test_nohup_keeps_working(monkeypatch):
    from ixel_mat import cli

    installed = {}
    monkeypatch.setattr(cli.signal, "getsignal",
                        lambda sig: cli.signal.SIG_IGN if sig == cli.signal.SIGHUP else cli.signal.SIG_DFL)
    monkeypatch.setattr(cli.signal, "signal", lambda sig, handler: installed.setdefault(sig, handler))
    cli.stop_like_ctrl_c()
    assert list(installed) == [signal.SIGTERM]  # an ignored SIGHUP stays ignored
