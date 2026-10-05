"""Machines without a server: the saved list, ~/.ssh/config and Ixel Console imports, pinned keys, the ssh
lines, terminals, the log, runs (with a stand-in for ssh), Health and `ixel machines`. test_machines_ssh.py
does it against a real sshd."""
import asyncio
import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from ixel_mat import health
from ixel_mat.machines import cli, log, runs, ssh, store, terminal
from ixel_mat.machines.ssh import SSHError
from ixel_mat.machines.store import MachineError

ED = "AAAAC3NzaC1lZDI1NTE5AAAAIHN0dWZmIHRoYXQgbG9va3MgbGlrZSBhIGtleSBidXQgaXNudA"
OTHER = "AAAAC3NzaC1lZDI1NTE5AAAAIGFub3RoZXIga2V5IHRoYXQgaXNudCB0aGUgc2FtZSBvbmUh"
POSIX = sys.platform != "win32"


def machine(**changes) -> store.Machine:
    return store.check({"name": "web", "host": "web.example.com", "user": "robin", **changes})


@pytest.fixture
def fake_ssh(monkeypatch, tmp_path):
    """ssh is "found", and `ssh -G` says each host goes where it's named (no config to apply)."""
    monkeypatch.setattr(ssh, "need", lambda name="ssh": f"/usr/bin/{name}")
    monkeypatch.setattr(ssh, "ssh_program", lambda name="ssh": f"/usr/bin/{name}")
    monkeypatch.setattr(ssh, "resolve", lambda m, program=None, fresh=False: ssh.Where(m.host, m.port or 22, m.user))


# ── what can be saved ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("host", ["web", "web.example.com", "web.example.com.", "192.168.1.20", "::1",
                                  "100.64.0.7", "my_alias", "a-b"])
def test_names_and_addresses_are_taken(host):
    assert store.check({"host": host}).host == host


@pytest.mark.parametrize("host", ["-oProxyCommand=touch /tmp/x", "-p", "web;id", "$(id)", "web name", "[::1]",
                                  "web:22", "fe80::1%eth0", "a" * 254, "we\nb", "user@web", "", "-"])
def test_anything_ssh_could_read_as_an_option_or_a_shell_could_run_is_refused(host):
    with pytest.raises(MachineError):
        store.check({"host": host})


@pytest.mark.parametrize("user", ["-oProxyCommand=x", "-l", "a b", "a;b", "$(id)", "a\nb", "é"])
def test_only_plain_login_names_are_taken(user):
    with pytest.raises(MachineError):
        store.check({"host": "web", "user": user})
    assert store.check({"host": "web", "user": "deploy.bot@corp"}).user == "deploy.bot@corp"


@pytest.mark.parametrize("given, kept", [(None, None), ("", None), (0, None), ("2222", 2222), (22, 22)])
def test_ports(given, kept):
    assert store.check({"host": "web", "port": given}).port == kept


@pytest.mark.parametrize("port", [70000, -1, True, "22a", 1.5])
def test_ports_that_arent(port):
    with pytest.raises(MachineError, match="port"):
        store.check({"host": "web", "port": port})


def test_what_runs_on_connecting_is_an_agents_own_command_a_shell_or_one_you_type():
    assert store.check({"host": "w", "agent": "openclaw", "command": "openclaw tui"}).command == "openclaw tui"
    with pytest.raises(MachineError, match="openclaw tui"):
        store.check({"host": "w", "agent": "openclaw", "command": "rm -rf ~"})
    assert store.check({"host": "w", "agent": "shell", "command": "ignored"}).command == ""
    with pytest.raises(MachineError, match="Type the command"):
        store.check({"host": "w", "agent": "custom", "command": " "})
    assert store.check({"host": "w", "agent": "custom", "command": "tmux attach"}).command == "tmux attach"
    with pytest.raises(MachineError, match="line break"):
        store.check({"host": "w", "agent": "custom", "command": "ls\nid"})
    with pytest.raises(MachineError):
        store.check({"host": "w", "agent": "something"})
    assert store.check({"host": "w"}).name == "w"
    assert store.check({"host": "w", "notes": "line one\nline two"}).notes == "line one\nline two"


def test_saved_machines_are_private_and_come_back_the_same():
    saved = store.Store().save({"name": "Pi", "host": "pi.local", "user": "pi", "port": "2200", "group": "Home"})
    again = store.Store().load()
    assert again == [saved] and saved.port == 2200 and saved.id
    if POSIX:
        assert stat.S_IMODE(store.MACHINES_FILE.stat().st_mode) == 0o600
    changed = store.Store().save({**saved.to_dict(), "name": "Pi 5"})
    assert changed.id == saved.id and [m.name for m in store.Store().load()] == ["Pi 5"]
    assert store.Store().delete(saved.id).name == "Pi 5" and store.Store().load() == []
    with pytest.raises(MachineError, match="isn't saved"):
        store.Store().save({**saved.to_dict(), "name": "gone"})


def test_entries_this_version_cant_read_are_kept_as_they_are():
    store.MACHINES_FILE.parent.mkdir(parents=True, exist_ok=True)
    odd = {"id": "x1", "host": "-oProxyCommand=evil", "future_field": [1, 2]}
    store.MACHINES_FILE.write_text(json.dumps({"version": 1, "machines": [odd]}))
    machines = store.Store()
    assert machines.load() == [] and machines.unreadable() == 1
    machines.save({"host": "web"})
    data = json.loads(store.MACHINES_FILE.read_text())
    assert odd in data["machines"] and len(data["machines"]) == 2


def test_a_file_ixel_cant_read_is_never_written_over():
    store.MACHINES_FILE.parent.mkdir(parents=True, exist_ok=True)
    store.MACHINES_FILE.write_text("{ not json")
    with pytest.raises(MachineError, match="won't write over it"):
        store.Store().save({"host": "web"})
    assert store.MACHINES_FILE.read_text() == "{ not json"


def test_bringing_in_leaves_out_what_is_already_there():
    machines = store.Store()
    machines.save({"host": "web", "user": "robin"})
    added, skipped = machines.add_many([machine(host="WEB", user="robin", port=22), machine(host="db"),
                                        machine(host="db")])
    assert [m.host for m in added] == ["db"] and skipped == 2


def test_finding_by_name_ignores_case_then_tries_the_address():
    machines = store.Store()
    machines.save({"name": "Build box", "host": "10.0.0.5"})
    assert machines.find("build BOX").host == "10.0.0.5"
    assert machines.find("10.0.0.5").name == "Build box"
    assert machines.find("nope") is None


# ── ~/.ssh/config and Ixel Console ────────────────────────────────────────────

def test_ssh_config_hosts_include_the_files_it_includes_and_skip_patterns(tmp_path):
    home = tmp_path / ".ssh"
    (home / "conf.d").mkdir(parents=True)
    (home / "config").write_text(
        "# mine\nHost web db\n  HostName 10.0.0.1\nHost *.corp !bad * ?x\nHost=pi\n"
        'Host "quoted"\nInclude conf.d/*.conf\nInclude config\nMatch host foo\n  User x\n')
    (home / "conf.d" / "a.conf").write_text("Host gpu-box\n")
    (home / "conf.d" / "b.conf").write_text("Host -oProxyCommand=x\nHost web\n")
    assert store.ssh_config_hosts(home / "config") == ["web", "db", "pi", "quoted", "gpu-box"]
    assert [m.host for m in store.from_ssh_config(home / "config")] == ["web", "db", "pi", "quoted", "gpu-box"]


def test_ixel_consoles_ssh_profiles_come_over_and_its_gateways_are_counted(tmp_path):
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps([
        {"name": "Mac mini", "host": "Mini.local", "user": "robin", "port": 22, "identity_file": "~/.ssh/id_ed25519",
         "agent": "openclaw", "remote_command": "openclaw tui", "group": "Default", "notes": "gitea"},
        {"name": "GPU", "host": "100.64.0.9", "port": 2222, "agent": "custom", "remote_command": ""},
        {"name": "Hermes", "host": "h.example.com", "agent": "hermes", "remote_command": "hermes", "group": "Lab"},
        {"name": "Gateway", "connection_type": "websocket", "url": "wss://x"},
        {"name": "Bad", "host": "-oProxyCommand=x"},
        "junk",
    ]))
    found = store.from_console(profiles)
    mini, gpu, hermes = found.machines
    assert (mini.host, mini.user, mini.port, mini.key, mini.agent, mini.command, mini.group, mini.notes) == \
        ("Mini.local", "robin", None, "~/.ssh/id_ed25519", "openclaw", "openclaw tui", "", "gitea")
    assert (gpu.port, gpu.agent, gpu.command) == (2222, "shell", "")
    assert (hermes.agent, hermes.group) == ("hermes", "Lab")
    assert found.gateways == 1 and found.unreadable == 2
    assert found.names[mini.id] == ("mini.local", 22) and found.names[gpu.id] == ("100.64.0.9", 2222)
    assert store.from_console(tmp_path / "missing.json") is None


# ── pinned keys ──────────────────────────────────────────────────────────────

def test_keys_are_pinned_privately_under_ssh_s_own_name():
    assert ssh.known_hosts_name("Web.Example.com", 22) == "web.example.com"
    assert ssh.known_hosts_name("10.0.0.1", 2222) == "[10.0.0.1]:2222"
    assert ssh.pin("web.example.com", {("ssh-ed25519", ED)}) == 1
    assert ssh.pin("web.example.com", [("ssh-ed25519", ED)]) == 0
    assert ssh.pinned("WEB.example.com") == {("ssh-ed25519", ED)}
    assert ssh.pinned("db") == set()
    assert ssh.PINS_FILE.read_text() == f"web.example.com ssh-ed25519 {ED}\n"
    if POSIX:
        assert stat.S_IMODE(ssh.PINS_FILE.stat().st_mode) == 0o600
    ssh.pin("[10.0.0.1]:2222", {("ssh-ed25519", OTHER)})
    assert ssh.forget("web.example.com") == 1
    assert ssh.pinned("web.example.com") == set() and ssh.pinned("[10.0.0.1]:2222")


@pytest.mark.parametrize("name, kind, b64", [
    ("web\n@cert-authority * ssh-ed25519", "ssh-ed25519", ED),
    ("web *", "ssh-ed25519", ED),
    ("web,*", "ssh-ed25519", ED),
    ("web", "ssh-ed25519\nevil", ED),
    ("web", "ssh-ed25519", ED + " evil"),
    ("[web]:99999", "ssh-ed25519", ED),
])
def test_nothing_but_one_name_and_one_key_gets_into_the_pins_file(name, kind, b64):
    with pytest.raises(SSHError):
        ssh.pin(name, {(kind, b64)})
    assert not ssh.PINS_FILE.exists()


def test_ixel_consoles_pins_come_over_for_the_machines_brought_in():
    ssh.CONSOLE_PINS.parent.mkdir(parents=True)
    ssh.CONSOLE_PINS.write_text(f"mini.local ssh-ed25519 {ED}\n[100.64.0.9]:2222 ssh-ed25519 {OTHER}\n"
                                "@revoked * ssh-ed25519 AAAA\n|1|hashed ssh-ed25519 AAAA\n")
    assert ssh.adopt_console_pins([("mini.local", "mini.local"), ("gpu", "[100.64.0.9]:2222"),
                                   ("nothing", "not-there")]) == (2, 0)
    assert ssh.pinned("gpu") == {("ssh-ed25519", OTHER)} and ssh.pinned("mini.local") == {("ssh-ed25519", ED)}


def test_a_different_key_is_never_pinned_beside_one_already_pinned():
    ssh.pin("web", {("ssh-ed25519", ED)})
    with pytest.raises(SSHError) as caught:
        ssh.pin("web", {("ssh-ed25519", OTHER)})
    assert caught.value.code == "changed" and ssh.pinned("web") == {("ssh-ed25519", ED)}
    # The same server's other key types, alongside one already pinned, are fine
    assert ssh.pin("web", {("ssh-ed25519", ED), ("ssh-rsa", OTHER)}) == 1
    # Ixel Console's key for a machine that has another one pinned here is left out, and counted
    ssh.CONSOLE_PINS.parent.mkdir(parents=True)
    ssh.CONSOLE_PINS.write_text(f"db ssh-ed25519 {OTHER}\n")
    ssh.pin("db", {("ssh-ed25519", ED)})
    assert ssh.adopt_console_pins([("db", "db")]) == (0, 1) and ssh.pinned("db") == {("ssh-ed25519", ED)}


def test_a_pins_file_that_isnt_utf8_is_kept_byte_for_byte():
    ssh.PINS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ssh.PINS_FILE.write_bytes(b"old\xff ssh-ed25519 " + ED.encode() + b"\n")
    assert ssh.pinned("web") == set()
    ssh.pin("web", {("ssh-ed25519", ED)})
    assert ssh.PINS_FILE.read_bytes().startswith(b"old\xff ssh-ed25519 ") and ssh.pinned("web")
    assert ssh.forget("web") == 1 and ssh.PINS_FILE.read_bytes() == b"old\xff ssh-ed25519 " + ED.encode() + b"\n"


def test_the_way_there_is_part_of_the_name_a_key_is_pinned_under():
    direct = ssh.Where("10.0.0.5", 22, "")
    office, datacenter = ssh.Where("10.0.0.5", 22, "", jump="office-bastion"), ssh.Where("10.0.0.5", 22, "", jump="dc")
    proxied = ssh.Where("10.0.0.5", 2222, "", proxy="nc dc 22")
    names = {direct.name, office.name, datacenter.name, proxied.name}
    assert len(names) == 4 and direct.name == "10.0.0.5" and office.name.startswith("10.0.0.5~")
    assert proxied.name.startswith("[10.0.0.5]:2222~")
    for name in names:
        ssh.pin(name, {("ssh-ed25519", ED if name == office.name else OTHER)})
    assert ssh.pinned(office.name) == {("ssh-ed25519", ED)} and ssh.pinned(datacenter.name) == {("ssh-ed25519", OTHER)}
    for bad in ("10.0.0.5~", "10.0.0.5~xyz", "10.0.0.5~0123456789a", "10.0.0.5~ABCDEF0123"):
        with pytest.raises(SSHError):
            ssh.pin(bad, {("ssh-ed25519", ED)})


@pytest.mark.skipif(not ssh.ssh_program(), reason="needs OpenSSH's ssh for ssh -G")
def test_ssh_g_says_the_jump_host_and_the_proxy():
    ssh.CONFIG_FILE.write_text("Host office\n  HostName 10.0.0.5\n  ProxyJump bastion\n"
                               "Host dc\n  HostName 10.0.0.5\n  ProxyCommand nc dc-gw %p\n"
                               "Host plain\n  HostName 10.0.0.5\n")
    office, dc, plain = (ssh.resolve(store.check({"host": h})) for h in ("office", "dc", "plain"))
    assert office.jump == "bastion" and dc.proxy == "nc dc-gw %p" and not plain.jump and not plain.proxy
    assert len({office.name, dc.name, plain.name}) == 3 and plain.name == "10.0.0.5"


def test_known_hosts_command_is_switched_off_whenever_ssh_has_it(monkeypatch):
    # Not only when the config sets one: a file it Includes can add one at any moment
    ssh.pin("web.example.com", {("ssh-ed25519", ED)})
    monkeypatch.setattr(ssh, "need", lambda name="ssh": "/usr/bin/ssh")
    monkeypatch.setattr(ssh, "resolve", lambda m, program=None, fresh=False: ssh.Where(m.host, m.port or 22, m.user))
    for has in (True, False):
        monkeypatch.setattr(ssh, "stops_known_hosts_command", lambda program: has)
        for line in (ssh.run_argv(machine(), "uptime"), ssh.connect_argv(machine())):
            assert ("KnownHostsCommand=none" in line) is has


@pytest.mark.skipif(not ssh.ssh_program(), reason="needs OpenSSH's ssh")
def test_asking_ssh_whether_it_has_an_option():
    program = ssh.ssh_program()
    assert ssh._takes_option(program, "BatchMode=yes", ()) is True
    assert ssh._takes_option(program, "NoSuchOptionAnywhere=1", ()) is False


def test_one_change_at_a_time_across_programs(tmp_path):
    import subprocess
    target = tmp_path / "machines.json"
    holder = subprocess.Popen([sys.executable, "-c", (
        "import sys, time; sys.path.insert(0, sys.argv[2]); from pathlib import Path; "
        "from ixel_mat.machines.store import locked\n"
        "with locked(Path(sys.argv[1])):\n    print('held', flush=True); time.sleep(3)"),
        str(target), str(Path(store.__file__).parents[2])], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(MachineError, match="Another Ixel"):
            with store.locked(target, timeout=0.3):
                pass
    finally:
        holder.wait(10)
    with store.locked(target, timeout=1):  # free again once the other one is done
        pass


def test_ssh_only_accepts_the_pinned_key_from_ixel_s_own_file(tmp_path):
    options = ssh.pin_options("[10.0.0.1]:2222", tmp_path / "100%" / "known hosts")
    pairs = dict(o.split("=", 1) for o in options[1::2])
    assert options[::2] == ["-o"] * len(pairs)
    assert pairs["StrictHostKeyChecking"] == "yes" and pairs["HostKeyAlias"] == "[10.0.0.1]:2222"
    assert pairs["UserKnownHostsFile"] == '"' + str(tmp_path / "100%%" / "known hosts") + '"'
    assert pairs["GlobalKnownHostsFile"] == os.devnull
    assert pairs["UpdateHostKeys"] == "no" and pairs["ControlMaster"] == "no" and pairs["ControlPath"] == "none"
    assert "KnownHostsCommand" not in pairs
    assert "KnownHostsCommand=none" in ssh.pin_options("web", known_hosts_command=True)


def test_checking_a_key_signs_in_with_nothing_and_sends_nothing_of_yours(fake_ssh, monkeypatch):
    ran = []

    def options(argv):
        return dict(argv[i + 1].split("=", 1) for i, arg in enumerate(argv) if arg == "-o")

    def fake_run(argv, timeout, env=None):  # ssh writes the key it was shown into the temporary file
        ran.append(argv)
        known = options(argv)["UserKnownHostsFile"].strip('"').replace("%%", "%")
        Path(known).write_text(f"web.example.com ssh-ed25519 {ED}\n")
        return 255, "Permission denied"

    monkeypatch.setattr(ssh, "_run_bounded", fake_run)
    assert ssh.learn(machine()).state == "new"
    options = options(ran[0])
    for key, value in {"BatchMode": "yes", "PubkeyAuthentication": "no", "PasswordAuthentication": "no",
                       "KbdInteractiveAuthentication": "no", "GSSAPIAuthentication": "no",
                       "HostbasedAuthentication": "no", "IdentityAgent": "none", "ForwardAgent": "no",
                       "ForwardX11": "no", "ForwardX11Trusted": "no", "Tunnel": "no", "ClearAllForwardings": "yes",
                       "PermitLocalCommand": "no", "RemoteCommand": "none", "RequestTTY": "no",
                       "ControlMaster": "no", "ControlPath": "none"}.items():
        assert options[key] == value, key
    assert ran[0][-2:] == ["robin@web.example.com", "exit"]


def test_checking_a_key_sends_none_of_your_environment(fake_ssh, monkeypatch):
    seen = []
    monkeypatch.setattr(ssh, "resolve", lambda m, program=None, fresh=False: ssh.Where(
        m.host, 22, m.user, send_env=("MY_*", "LANG", "-LC_*"), set_env=True))
    monkeypatch.setattr(ssh, "_run_bounded", lambda argv, timeout, env=None: seen.append((argv, env)) or (255, ""))
    monkeypatch.setenv("MY_DEPLOY_TOKEN", "secret")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_TIME", "C")
    with pytest.raises(SSHError):
        ssh.learn(machine())
    argv, env = seen[0]
    assert "MY_DEPLOY_TOKEN" not in env and "LANG" not in env and env["LC_TIME"] == "C"  # "-LC_*" sends nothing
    assert "SetEnv=IXEL_CHECK=1" in argv and argv.index("SetEnv=IXEL_CHECK=1") < argv.index("--")


@pytest.mark.skipif(not ssh.ssh_program(), reason="needs OpenSSH's ssh for ssh -G")
def test_ssh_g_says_what_ssh_would_send_from_the_environment():
    ssh.CONFIG_FILE.write_text("Host sends\n  SendEnv MY_* LANG\n  SetEnv VAULT_TOKEN=x\nHost quiet\n")
    sends, quiet = (ssh.resolve(store.check({"host": h}), fresh=True) for h in ("sends", "quiet"))
    assert {"MY_*", "LANG"} <= set(sends.send_env) and sends.set_env
    assert not quiet.set_env and "MY_*" not in quiet.send_env


def test_a_pinned_machine_is_asked_for_the_pinned_key_type_first(fake_ssh, monkeypatch):
    ssh.pin("web.example.com", {("ecdsa-sha2-nistp256", OTHER), ("ssh-rsa", ED)})
    asked = []

    def fake_run(argv, timeout, env=None):
        algorithms = next((a for a in argv if a.startswith("HostKeyAlgorithms=")), "")
        asked.append(algorithms)
        return 255, "Unable to negotiate with 1.2.3.4 port 22: no matching host key type found." if algorithms else \
            "Permission denied"

    monkeypatch.setattr(ssh, "_run_bounded", fake_run)
    with pytest.raises(SSHError):
        ssh.learn(machine())
    # The server no longer has those types: asked again, for whatever it has
    assert asked == ["HostKeyAlgorithms=ecdsa-sha2-nistp256,rsa-sha2-512,rsa-sha2-256", ""]
    asked.clear()
    monkeypatch.setattr(ssh, "_run_bounded",
                        lambda argv, timeout, env=None: asked.append(1) or (255, "Connection refused"))
    with pytest.raises(SSHError) as caught:
        ssh.learn(machine())
    assert caught.value.code == "refused" and len(asked) == 1  # not asked twice when it isn't the key type


@pytest.mark.parametrize("jump, line", [
    ("bastion", "(ssh bastion)"), ("root@localhost:41161", "(ssh ssh://root@localhost:41161)"),
    ("ssh://a@b:2", "(ssh ssh://a@b:2)"), ("a,b", "(ssh a) and say yes there, then do the same for each hop"),
])
def test_the_jump_host_hint_gives_a_line_ssh_takes(jump, line):
    assert line in ssh._jump_hint(jump)


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="reads /proc")
def test_a_check_that_hangs_ends_with_everything_it_started(tmp_path):
    import time
    started = time.monotonic()
    # A child that leaves a grandchild holding stderr: a pipe would never close
    with pytest.raises(__import__("subprocess").TimeoutExpired):
        ssh._run_bounded(["sh", "-c", f"sleep 60 & echo $! > {tmp_path / 'pid'}; wait"], 1)
    assert time.monotonic() - started < 10
    pid = int((tmp_path / "pid").read_text())
    time.sleep(0.2)
    try:  # gone, or a zombie waiting for init to reap it
        assert Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        pass
    assert ssh._run_bounded(["sh", "-c", "echo said >&2; exit 4"], 10) == (4, "said\n")
    # A chatty ssh (LogLevel DEBUG3): what's kept is the end, where it says why it stopped
    chatty = "import sys; sys.stderr.write('x' * 200000 + '\\nthe end\\n')"
    code, said = ssh._run_bounded([sys.executable, "-c", chatty], 10)
    assert said.endswith("the end\n") and len(said) <= 65536


# ── the ssh lines ────────────────────────────────────────────────────────────

def test_nothing_connects_before_its_key_is_pinned(fake_ssh):
    for build in (ssh.connect_argv, lambda m: ssh.run_argv(m, "uptime")):
        with pytest.raises(SSHError) as caught:
            build(machine())
        assert caught.value.code == "not_pinned" and ssh.connect_line(machine()) in str(caught.value)
    assert ssh.connect_line(machine()) == ('ixel machines connect "web"' if ssh.WINDOWS else "ixel machines connect web")


@pytest.mark.parametrize("name, posix, windows", [
    ("web", "ixel machines connect web", 'ixel machines connect "web"'),
    ("$5 VPS", "ixel machines connect '$5 VPS'", "ixel machines connect '$5 VPS'"),
    ("Dev \"blue\"", "ixel machines connect 'Dev \"blue\"'", "ixel machines connect 'Dev \"blue\"'"),
    ("box $(id -un)", "ixel machines connect 'box $(id -un)'", "ixel machines connect 'box $(id -un)'"),
    ("Robin's box", "ixel machines connect 'Robin'\"'\"'s box'", "ixel machines connect 'Robin''s box'"),
    ("100% up", "ixel machines connect '100% up'", "ixel machines connect '100% up'"),
    ("Robin\u2019s box", "ixel machines connect 'Robin\u2019s box'", "ixel machines connect 'Robin\u2019\u2019s box'"),
])
def test_the_line_to_paste_is_quoted_for_the_shell(name, posix, windows, monkeypatch):
    monkeypatch.setattr(ssh, "WINDOWS", False)
    assert ssh.connect_line(machine(name=name)) == posix
    monkeypatch.setattr(ssh, "WINDOWS", True)
    assert ssh.connect_line(machine(name=name)) == windows


def test_the_destination_comes_after_the_end_of_options(fake_ssh):
    ssh.pin("web.example.com", {("ssh-ed25519", ED)})
    web = machine(agent="openclaw", command="openclaw tui", port=2200)
    ssh.pin("[web.example.com]:2200", {("ssh-ed25519", ED)})
    line = ssh.connect_argv(web)
    assert line[:4] == ["/usr/bin/ssh", "-F", str(ssh.CONFIG_FILE), "-t"]
    assert line[line.index("--") + 1:] == ["robin@web.example.com", "openclaw tui"]
    assert "BatchMode=no" in line and "HostKeyAlias=[web.example.com]:2200" in line and "-p" in line
    run = ssh.run_argv(web, "df -h | tail -1")
    assert run[run.index("--") + 1:] == ["robin@web.example.com", "df -h | tail -1"]
    assert "BatchMode=yes" in run and "-T" in run and "ConnectTimeout=15" in run
    # A command given here replaces a RemoteCommand from the config (ssh refuses both); a shell keeps it
    assert "RemoteCommand=none" in run and "RemoteCommand=none" in line
    shell = ssh.connect_argv(machine())
    assert shell[-1] == "robin@web.example.com" and "RemoteCommand=none" not in shell  # a shell: no command


def test_a_key_file_thats_gone_is_said_before_ssh_runs(fake_ssh, tmp_path):
    ssh.pin("web.example.com", {("ssh-ed25519", ED)})
    with pytest.raises(SSHError) as caught:
        ssh.run_argv(machine(key=str(tmp_path / "missing")), "uptime")
    assert caught.value.code == "key_missing"
    (tmp_path / "id").write_text("key")
    line = ssh.run_argv(machine(key=str(tmp_path / "id")), "uptime")
    assert line[line.index("-i") + 1] == str(tmp_path / "id")


def test_copying_a_key_sends_only_key_characters(fake_ssh, tmp_path):
    ssh.pin("web.example.com", {("ssh-ed25519", ED)})
    (tmp_path / "id.pub").write_text(f"ssh-ed25519 {ED} robin@pc'; rm -rf ~; echo '\n")
    line = ssh.copy_key_argv(machine(key=str(tmp_path / "id")))
    assert line[line.index("--") + 1] == "robin@web.example.com"
    assert f"'ssh-ed25519 {ED}'" in line[-1] and "rm -rf" not in line[-1] and "RemoteCommand=none" in line
    (tmp_path / "id.pub").write_text(f"ssh-ed25519 {ED} robin@pc\n")
    assert f"'ssh-ed25519 {ED} robin@pc'" in ssh.copy_key_argv(machine(key=str(tmp_path / "id")))[-1]
    for junk in ("", "not a key", f"ssh-ed25519 {ED}'x"):
        (tmp_path / "id.pub").write_text(junk)
        with pytest.raises(SSHError, match="public key"):
            ssh.copy_key_argv(machine(key=str(tmp_path / "id")))
    with pytest.raises(SSHError, match="key file first"):
        ssh.copy_key_argv(machine())


@pytest.mark.skipif(not POSIX, reason="the server side runs in sh")
def test_the_copied_key_lands_once_on_its_own_line_in_a_private_file(fake_ssh, tmp_path):
    import subprocess
    ssh.pin("web.example.com", {("ssh-ed25519", ED)})
    (tmp_path / "id.pub").write_text(f"ssh-ed25519 {ED} robin@pc\n")
    remote = ssh.copy_key_argv(machine(key=str(tmp_path / "id")))[-1]
    server = tmp_path / "server-home"
    server.mkdir()

    def on_server():
        return subprocess.run(["sh", "-c", remote], cwd=server, capture_output=True, text=True).stdout

    assert "Key added" in on_server() and "already" in on_server()
    keys = server / ".ssh" / "authorized_keys"
    assert keys.read_text() == f"ssh-ed25519 {ED} robin@pc\n"
    assert stat.S_IMODE(keys.stat().st_mode) == 0o600 and stat.S_IMODE(keys.parent.stat().st_mode) == 0o700
    keys.write_text(f"ssh-ed25519 {OTHER} other")  # no newline at the end
    on_server()
    assert keys.read_text().splitlines() == [f"ssh-ed25519 {OTHER} other", f"ssh-ed25519 {ED} robin@pc"]


@pytest.mark.parametrize("said, code", [
    ("@@@@\nWARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\nHost key verification failed.", "hostkey"),
    ("Host key verification failed.", "hostkey"),
    ("robin@web: Permission denied (publickey).", "no_key"),
    ("ssh: Could not resolve hostname web: Name or service not known", "not_found"),
    ("ssh: connect to host web port 22: Connection refused", "refused"),
    ("ssh: connect to host web port 22: Connection timed out", "unreachable"),
    ("ssh: connect to host web port 22: No route to host", "unreachable"),
    ("/home/a/.ssh/config: line 3: Bad configuration option: usekeychain\nterminating, 1 bad configuration options",
     "config"),
    ("WARNING: UNPROTECTED PRIVATE KEY FILE!\nbad permissions", "key_perms"),
    ("Warning: Identity file /x not accessible: No such file or directory.", "key_missing"),
    ("kex_exchange_identification: read: Connection reset by peer", "ssh"),
])
def test_what_ssh_said_becomes_one_plain_reason(said, code):
    got, message = ssh.explain(said, machine())
    assert got == code and message


def test_through_a_jump_host_a_key_problem_names_the_jump_host_too():
    code, message = ssh.explain("Host key verification failed.", machine(),
                                ssh.Where("10.0.0.5", 22, "robin", jump="bastion.example.com"))
    assert code == "hostkey" and "jump host" in message and "ssh bastion.example.com" in message


# ── terminals ───────────────────────────────────────────────────────────────

LINE = ["/usr/bin/ssh", "-t", "--", "robin@web", "echo 'a b'; ls > out.txt & %PATH% ^"]


@pytest.mark.parametrize("program, flag", [("/usr/bin/konsole", "-e"), ("/usr/bin/xterm", "-e"),
                                           ("/usr/bin/alacritty", "-e"), ("/usr/bin/gnome-terminal", "--"),
                                           ("/usr/bin/kitty", "--")])
def test_each_terminal_gets_the_ssh_line_as_separate_arguments_and_stays_open(program, flag):
    assert terminal.argv_for(program, LINE) == [program, flag, sys.executable, "-c", terminal._HOLD_OPEN, *LINE]


def test_windows_terminal_only_gets_python_and_a_script_that_runs_the_exact_line(tmp_path):
    line = terminal.argv_for(r"C:\Users\a\AppData\Local\Microsoft\WindowsApps\wt.exe", [*LINE, "a;b"])
    assert line[1:3] == ["new-tab", "--"] and len(line) == 5
    script = Path(line[4].replace("\\;", ";"))
    try:
        assert repr([*LINE, "a;b"]) in script.read_text() and "os.remove(__file__)" in script.read_text()
    finally:
        script.unlink()
    console = terminal.argv_for("C:\\Windows\\System32\\cmd.exe", LINE)
    assert console[1] == "-c" and console[3:] == LINE and "cmd" not in console[0].lower()


@pytest.mark.skipif(not POSIX, reason="runs a shell script")
def test_the_mac_command_file_runs_the_exact_line_and_removes_itself(tmp_path):
    import subprocess
    out = tmp_path / "args"
    probe = [sys.executable, "-c", "import sys, json; open(sys.argv[1], 'w').write(json.dumps(sys.argv[2:]))",
             str(out), "a b", "'quoted'", "$HOME", "`id`", "; rm -rf /"]
    line = terminal.argv_for(terminal.MACOS_TERMINAL, probe)
    assert line[:3] == ["/usr/bin/open", "-a", "Terminal"]
    command_file = Path(line[3])
    assert stat.S_IMODE(command_file.stat().st_mode) == 0o700
    held = subprocess.run(["sh", str(command_file)], input="\n", capture_output=True, text=True, check=True,
                          timeout=30)
    assert json.loads(out.read_text()) == probe[4:] and not command_file.exists()
    assert "Press Enter to close this window" in held.stdout


def test_the_window_stays_open_until_enter_and_the_line_arrives_exactly(tmp_path):
    import subprocess
    out = tmp_path / "args"
    probe = [sys.executable, "-c", "import sys, json; open(sys.argv[1], 'w').write(json.dumps(sys.argv[2:])); "
             "sys.exit(3)", str(out), "a;b", "%PATH%", "x > y"]
    held = subprocess.run([sys.executable, "-c", terminal._HOLD_OPEN, *probe], input="\n", capture_output=True,
                          text=True, timeout=30)
    assert json.loads(out.read_text()) == probe[4:] and "ssh ended (code 3)" in held.stdout


def test_no_terminal_says_so_with_the_line_to_run_instead(monkeypatch):
    monkeypatch.setattr(terminal, "find", lambda: None)
    with pytest.raises(SSHError) as caught:
        terminal.open_window(["ssh", "web"])
    assert caught.value.code == "no_terminal"


def test_with_no_desktop_on_linux_no_window_is_opened(monkeypatch):
    monkeypatch.setattr(terminal, "WINDOWS", False)
    monkeypatch.setattr(terminal, "MAC", False)
    monkeypatch.setattr(terminal, "find_on_path", lambda name: f"/usr/bin/{name}")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert terminal.find() is None
    with pytest.raises(SSHError, match="no desktop") as caught:
        terminal.open_window(["ssh", "web"])
    assert caught.value.code == "no_terminal"
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert terminal.find() == "/usr/bin/konsole"


# ── the log ───────────────────────────────────────────────────────────────

def test_the_log_is_private_one_line_each_and_cant_be_forged():
    log.write("RUN", machine='web" result="ok', command="ls\nFAKE LINE", path="/home/a/.ssh/id")
    text = log.LOG_FILE.read_text()
    assert text.count("\n") == 1 and 'machine="web\\" result=\\"ok"' in text and "\\x0a" in text
    assert 'path="/home/a/.ssh/id"' in text
    if POSIX:
        assert stat.S_IMODE(log.LOG_FILE.stat().st_mode) == 0o600


def test_a_long_value_is_cut_before_it_is_escaped():
    # Cut after escaping, the last \" would lose its quote and the next field would read as part of this one
    assert log._quote("a" * 1999 + '"' + "more") == '"' + "a" * 1999 + '\\""'
    assert log._quote("a" * 1999 + "\n") == '"' + "a" * 1999 + '\\x0a"'


def test_a_big_log_moves_aside(monkeypatch):
    monkeypatch.setattr(log, "MAX_BYTES", 10)
    log.write("ONE")
    log.write("TWO")
    assert "ONE" in log.LOG_FILE.with_name("machines.log.1").read_text() and "ONE" not in log.LOG_FILE.read_text()


# ── runs (a stand-in for ssh) ────────────────────────────────────────────────

def python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def target(name: str, argv=None, problem=None) -> runs.Target:
    return runs.Target(machine(name=name, host=name), argv, problem, name)


def run_all(targets, timeout=30, during=None):
    async def go():
        machines = runs.Runs()
        run = machines.start("the command", timeout, targets)
        if during:
            await during(machines, run)
        await asyncio.gather(run.task, return_exceptions=True)
        return run
    return asyncio.run(go())


def test_each_machine_ends_its_own_way():
    run = run_all([
        target("ok", python("import sys; sys.stdout.buffer.write(b'up 3 days\\n')")),  # \n, as from a server
        target("failed", python("import sys; print('partial'); sys.exit(3)")),
        target("refused", python("import sys; sys.stderr.write('robin@x: Permission denied (publickey).\\n'); "
                                 "sys.exit(255)")),
        target("unpinned", problem=("not_pinned", "Check its key first")),
    ])
    ok, failed, refused, unpinned = (r.to_dict(True) for r in run.results)
    assert ok["state"] == "ok" and ok["output"] == "up 3 days\n" and ok["preview"] == "up 3 days"
    assert failed["state"] == "failed" and failed["code"] == 3 and "partial" in failed["output"]
    assert refused["state"] == "error" and refused["hint_code"] == "no_key"
    assert unpinned["state"] == "error" and unpinned["hint"] == "Check its key first"
    assert run.to_dict()["counts"] == {"ok": 1, "failed": 1, "error": 2}
    assert "output" not in run.to_dict()["results"][0]
    assert "output" in run.to_dict({run.results[0].machine.id})["results"][0]
    lines = log.LOG_FILE.read_text().splitlines()
    assert lines[0].split()[1] == "RUN_START" and sum(" RUN " in line for line in lines) == 4


def test_output_past_the_limit_is_counted_not_kept(monkeypatch):
    monkeypatch.setattr(runs, "OUTPUT_CAP", 1000)
    [big] = run_all([target("big", python("import sys; sys.stdout.write('x' * 50000)"))]).results
    assert len(big.output) == 1000 and big.to_dict(False)["bytes"] == 50000 and big.to_dict(False)["cut"]


def test_a_machine_that_takes_too_long_is_stopped_with_what_it_started():
    [slow] = run_all([target("slow", python("import subprocess, sys, time; "
                                            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                                            "print('started', flush=True); time.sleep(60)"))], timeout=1).results
    assert slow.state == "timeout" and "1 seconds" in slow.hint and "started" in slow.output.decode()
    assert slow.to_dict(False)["seconds"] < 15


def test_only_eight_run_at_once(tmp_path):
    marks = tmp_path / "marks"
    marks.mkdir()
    code = (f"import os, time, pathlib; d = pathlib.Path({str(marks)!r}); p = d / str(os.getpid()); p.touch(); "
            "time.sleep(0.4); n = len(list(d.iterdir())); p.unlink(); print(n)")
    run = run_all([target(f"m{i}", python(code)) for i in range(12)])
    seen = [int(r.output) for r in run.results]
    assert all(r.state == "ok" for r in run.results) and max(seen) <= runs.MAX_AT_ONCE


def test_stopping_ends_every_machine_and_one_run_goes_at_a_time():
    async def during(machines, run):
        await asyncio.sleep(0.5)
        with pytest.raises(runs.RunError) as caught:
            machines.start("another", 30, [target("x", python("pass"))])
        assert caught.value.status == 409
        await machines.stop(run.id)

    run = run_all([target(f"m{i}", python("import time; time.sleep(60)")) for i in range(3)], during=during)
    assert [r.state for r in run.results] == ["stopped"] * 3 and not run.running


def ended(pid: int) -> bool:
    """Gone, or a zombie waiting to be reaped."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="reads /proc")
@pytest.mark.parametrize("turns", [0, 2, 4, 6, 8])
def test_a_second_stop_never_cuts_the_first_short(tmp_path, turns):
    import time
    pidfile = tmp_path / "pid"
    code = f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(60)"

    async def during(machines, run):
        while not pidfile.exists() or not pidfile.read_text():
            await asyncio.sleep(0.05)
        # Two cancels a few turns apart (two Stops, or Stop and Ixel closing), then two Stops at once
        run.task.cancel()
        for _ in range(turns):
            await asyncio.sleep(0)
        run.task.cancel()
        await asyncio.gather(machines.stop(run.id), machines.stop(run.id))

    run = run_all([target("m", python(code))], during=during)
    time.sleep(0.2)
    assert ended(int(pidfile.read_text())) and run.results[0].state == "stopped"


@pytest.mark.parametrize("command, problem", [(None, "Type"), ("  ", "Type"), ("x" * 2001, "longer"),
                                              ("ls\nid", "one line"), ("ls\x1b[2J", "one line")])
def test_the_command_is_one_line_you_typed(command, problem):
    with pytest.raises(runs.RunError, match=problem):
        runs.check_command(command)


# ── Health ────────────────────────────────────────────────────────────────

def test_health_says_nothing_is_set_up_until_there_are_machines():
    [only] = health.machine_checks(False, which=lambda name: None)
    assert only.state == "off" and only.fix == "ixel machines import ssh"


def test_a_machine_entry_that_isnt_right_is_left_as_it_is_and_nothing_breaks(monkeypatch):
    store.MACHINES_FILE.parent.mkdir(parents=True, exist_ok=True)
    store.MACHINES_FILE.write_text(json.dumps({"version": 1, "machines": [
        {"id": "a", "host": "web", "agent": {"weird": 1}}, {"id": "b", "host": "db", "agent": ["shell"]},
        {"id": "c", "host": "pi"}]}))
    assert [m.host for m in store.Store().load()] == ["pi"] and store.Store().unreadable() == 2
    with pytest.raises(MachineError):
        store.check({"host": "web", "agent": ["shell"]})
    checks = {c.id: c for c in health.machine_checks(False, which=lambda name: None)}
    assert checks["machines"].state == "warn"

    def broken():
        raise RuntimeError("something odd")

    monkeypatch.setattr(store.Store, "load", lambda self: broken())
    [failed] = health.machine_checks(True, which=lambda name: None)
    assert failed.state == "fail" and "something odd" in failed.detail


def test_health_checks_ssh_and_the_terminal_then_counts_pinned_keys(fake_ssh, monkeypatch):
    store.Store().save({"host": "web"})
    store.Store().save({"host": "db", "port": 2200})
    ssh.pin("web", {("ssh-ed25519", ED)})
    monkeypatch.setattr(terminal, "find", lambda: "/usr/bin/konsole")
    checks = {c.id: c for c in health.machine_checks(False, which=lambda name: f"/usr/bin/{name}")}
    assert checks["machines"].detail == "2 machines" and checks["ssh"].state == "ok"
    assert checks["terminal"].detail == "Connect opens Konsole" and checks["pinned"].state == "unchecked"
    probed = {c.id: c for c in health.machine_checks(True, which=lambda name: f"/usr/bin/{name}")}
    assert probed["pinned"].detail.startswith("1 of 2 pinned")
    monkeypatch.setattr(terminal, "find", lambda: None)
    missing = {c.id: c for c in health.machine_checks(False, which=lambda name: None, system="win32")}
    assert missing["ssh"].state == "fail" and "OpenSSH.Client" in missing["ssh"].fix
    assert missing["terminal"].state == "warn"


# ── ixel machines ─────────────────────────────────────────────────────────────

def test_ixel_machines_lists_them_with_their_keys(fake_ssh, capsys):
    assert cli.main([]) == 0 and "No machines yet" in capsys.readouterr().out
    store.Store().save({"name": "web", "host": "web.example.com", "user": "robin"})
    store.Store().save({"name": "pi", "host": "pi", "agent": "hermes", "command": "hermes"})
    ssh.pin("web.example.com", {("ssh-ed25519", ED)})
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "robin@web.example.com  runs a shell  (key pinned)" in out and "runs hermes  (key not checked)" in out


def test_ixel_machines_import(capsys):
    store.SSH_CONFIG.write_text("Host web db\n")
    assert cli.main(["import", "ssh"]) == 0 and "Added 2" in capsys.readouterr().out
    assert cli.main(["import", "ssh"]) == 0 and "(2 already here)" in capsys.readouterr().out
    assert cli.main(["import", "console"]) == 1 and "aren't on this computer" in capsys.readouterr().err
    assert cli.main(["import", "nowhere"]) == 2


def test_ixel_machines_connect_wont_pin_a_key_without_a_terminal_to_ask_in(fake_ssh, monkeypatch, capsys):
    store.Store().save({"name": "web", "host": "web.example.com"})
    learned = ssh.Learned(ssh.Where("web.example.com", 22, ""), {("ssh-ed25519", ED)}, "new")
    monkeypatch.setattr(ssh, "learn", lambda m: learned)
    # Not os.devnull: Windows calls NUL a terminal, so there connect asks, reads no answer, and pins nothing
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    assert cli.main(["connect", "nope"]) == 1
    assert cli.main(["connect", "WEB"]) == 1
    said = capsys.readouterr()
    assert ssh.fingerprint(ED) in said.out and "Run this in a terminal" in said.err
    assert not ssh.pinned("web.example.com")
    changed = ssh.Learned(ssh.Where("web.example.com", 22, ""), {("ssh-ed25519", ED)}, "changed",
                          {("ssh-ed25519", OTHER)})
    monkeypatch.setattr(ssh, "learn", lambda m: changed)
    assert cli.main(["connect", "web"]) == 1 and "key has changed" in capsys.readouterr().err
    assert "HOSTKEY_CHANGED" in log.LOG_FILE.read_text()


def test_ixel_machines_connect_pins_what_you_said_yes_to_then_runs_ssh(fake_ssh, monkeypatch, capsys):
    import subprocess
    store.Store().save({"name": "web", "host": "web.example.com"})
    monkeypatch.setattr(ssh, "learn", lambda m: ssh.Learned(ssh.Where("web.example.com", 22, ""),
                                                            {("ssh-ed25519", ED)}, "new"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda argv, env: ran.append(argv) or subprocess.CompletedProcess(argv, 0))
    assert cli.main(["connect", "web"]) == 0
    assert ssh.pinned("web.example.com") == {("ssh-ed25519", ED)}
    assert ran[0][-1] == "web.example.com" and "StrictHostKeyChecking=yes" in ran[0]
