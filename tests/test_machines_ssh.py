"""Machines against a real OpenSSH server (tests/sshd_lab.py), and the Machines page's API end to end."""
import asyncio
import json
import shutil
import subprocess
import sys

import pytest

from ixel_mat.gui import machines_api
from ixel_mat.gui.server import GuiServer
from ixel_mat.machines import cli, log, ssh, store, terminal
from ixel_mat.machines.ssh import SSHError
from ixel_mat.runtime import load_settings
from sshd_lab import Lab, fingerprint_of, keygen, need_sshd, sshd  # noqa: F401 (a fixture)
from test_gui_server import JSON_AUTH, AUTH, TOKEN, run_with_client


def saved(lab, **changes) -> store.Machine:
    return store.Store().save(lab.machine(**changes))


def run_here(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
                          env=ssh.quiet_env())


def test_a_new_machine_shows_its_key_and_runs_only_once_its_pinned(sshd):
    lab = saved(sshd)
    with pytest.raises(SSHError) as caught:
        ssh.run_argv(lab, "echo hi")
    assert caught.value.code == "not_pinned"
    learned = ssh.learn(lab)
    assert learned.state == "new" and learned.name == sshd.name
    assert learned.to_dict()["fingerprint"] == sshd.fingerprint and learned.keys == sshd.host_keys()
    assert not ssh.PINS_FILE.exists()  # nothing pinned until you say yes
    ssh.pin(learned.name, learned.keys)
    assert ssh.learn(lab).state == "pinned"
    done = run_here(ssh.run_argv(lab, "echo \"hello from $(whoami)\"; exit 3"))
    assert done.returncode == 3 and done.stdout == f"hello from {sshd.user}\n"


def test_a_server_whose_key_changed_is_refused_by_ssh_itself(sshd, tmp_path):
    lab = saved(sshd)
    impostor = keygen(tmp_path / "impostor")
    kind, b64 = impostor.with_name("impostor.pub").read_text().split()[:2]
    ssh.pin(sshd.name, {(kind, b64)})
    learned = ssh.learn(lab)
    assert learned.state == "changed" and learned.to_dict()["pinned"] == [fingerprint_of(tmp_path / "impostor.pub")]
    done = run_here(ssh.run_argv(lab, "echo should not run"))
    assert done.returncode == 255 and "should not run" not in done.stdout
    assert ssh.explain(done.stderr, lab)[0] == "hostkey"


def test_a_host_from_ssh_config_goes_where_the_config_says(sshd):
    ssh.CONFIG_FILE.write_text(f"Host lab-alias\n  HostName 127.0.0.1\n  Port {sshd.port}\n  User {sshd.user}\n"
                               f"  IdentityFile {sshd.client}\n  IdentitiesOnly yes\n")
    assert cli.main(["import", "ssh"]) == 0
    [alias] = store.Store().load()
    assert (alias.host, alias.port, alias.user, alias.key) == ("lab-alias", None, "", "")
    where = ssh.resolve(alias)
    assert (where.hostname, where.port, where.user, where.name) == ("127.0.0.1", sshd.port, sshd.user, sshd.name)
    learned = ssh.learn(alias)
    assert learned.state == "new" and learned.to_dict()["address"] == f"{sshd.user}@127.0.0.1:{sshd.port}"
    ssh.pin(learned.name, learned.keys)
    assert run_here(ssh.run_argv(alias, "echo via alias")).stdout == "via alias\n"


def test_a_remote_command_in_ssh_config_doesnt_stop_a_check_or_a_run(sshd):
    # A common setup: every login attaches to tmux. ssh refuses that together with a command of its own
    ssh.CONFIG_FILE.write_text(f"Host tmuxed\n  HostName 127.0.0.1\n  Port {sshd.port}\n  User {sshd.user}\n"
                               f"  IdentityFile {sshd.client}\n  RemoteCommand echo from-the-config\n")
    tmuxed = store.Store().save({"name": "tmuxed", "host": "tmuxed"})
    learned = ssh.learn(tmuxed)
    assert learned.state == "new"
    ssh.pin(learned.name, learned.keys)
    assert run_here(ssh.run_argv(tmuxed, "echo ours")).stdout == "ours\n"


def test_a_known_hosts_command_in_ssh_config_cant_vouch_for_another_key(sshd, tmp_path):
    lab = saved(sshd)
    impostor = keygen(tmp_path / "impostor")
    ssh.pin(sshd.name, {tuple(impostor.with_name("impostor.pub").read_text().split()[:2])})
    kind, b64 = sorted(sshd.host_keys())[0]
    vouch = tmp_path / "vouch.sh"
    vouch.write_text(f"#!/bin/sh\necho '{sshd.name} {kind} {b64}'\n")
    vouch.chmod(0o755)
    if not ssh.stops_known_hosts_command(ssh.need()):
        pytest.skip("this ssh has no KnownHostsCommand (OpenSSH 8.5 and later)")
    # In a file the config Includes, as well as in the config itself
    (ssh.CONFIG_FILE.parent / "vouching").write_text(f"KnownHostsCommand {vouch}\n")
    ssh.CONFIG_FILE.write_text(f"Include {ssh.CONFIG_FILE.parent / 'vouching'}\n")
    done = run_here(ssh.run_argv(lab, "echo should not run"))
    assert done.returncode == 255 and "should not run" not in done.stdout
    assert ssh.learn(lab).state == "changed"


@pytest.mark.skipif(not shutil.which("nc"), reason="needs nc for a ProxyCommand")
def test_one_address_on_two_networks_is_two_servers(sshd, tmp_path_factory):
    # The same 127.0.0.1:port, reached directly and through a proxy that goes somewhere else: like 10.0.0.5
    # at the office and at the data centre
    other = Lab(tmp_path_factory.mktemp("other"))
    try:
        ssh.CONFIG_FILE.write_text(f"Host office\n  HostName 127.0.0.1\n  Port {sshd.port}\n  User {sshd.user}\n"
                                   f"  IdentityFile {sshd.client}\n"
                                   f"Host datacenter\n  HostName 127.0.0.1\n  Port {sshd.port}\n  User {sshd.user}\n"
                                   f"  IdentityFile {other.client}\n  ProxyCommand nc 127.0.0.1 {other.port}\n")
        api = machines_api.Machines()
        office = api.save({"machine": {"name": "office", "host": "office"}})["machine"]["id"]
        datacenter = api.save({"machine": {"name": "datacenter", "host": "datacenter"}})["machine"]["id"]
        seen = {i: api.check_key({"id": i}) for i in (office, datacenter)}
        assert seen[office]["name"] != seen[datacenter]["name"] and {s["state"] for s in seen.values()} == {"new"}
        assert seen[office]["fingerprint"] == sshd.fingerprint and seen[datacenter]["fingerprint"] == other.fingerprint
        for i in (datacenter, office):
            api.trust({"id": i, "fingerprint": seen[i]["fingerprint"]})
        assert ssh.pinned(seen[office]["name"]) == sshd.host_keys()
        assert ssh.pinned(seen[datacenter]["name"]) == other.host_keys()
        for i, folder in ((office, sshd.folder), (datacenter, other.folder)):
            argv = ssh.run_argv(api.store.get(i), f"test -f {folder / 'sshd_config'} && echo right server")
            assert run_here(argv).stdout == "right server\n"
    finally:
        other.stop()


def test_a_key_pinned_since_the_check_isnt_joined_by_another(sshd, tmp_path):
    api = machines_api.Machines()
    lab = api.save({"machine": sshd.machine()})["machine"]["id"]
    seen = api.check_key({"id": lab})
    impostor = keygen(tmp_path / "impostor")  # pinned meanwhile (another window, an import)
    ssh.pin(sshd.name, {tuple(impostor.with_name("impostor.pub").read_text().split()[:2])})
    with pytest.raises(machines_api.MachinesApiError) as caught:
        api.trust({"id": lab, "fingerprint": seen["fingerprint"]})
    assert caught.value.status == 409 and caught.value.code == "changed"
    assert ssh.pinned(sshd.name) == {tuple(impostor.with_name("impostor.pub").read_text().split()[:2])}


def test_a_server_with_two_keys_matches_whichever_one_is_pinned(tmp_path_factory):
    need_sshd()
    lab = Lab(tmp_path_factory.mktemp("two-keys"), ecdsa=True)
    try:
        machine = saved(lab)
        kind, b64 = lab.ecdsa_key.with_name("host_ecdsa.pub").read_text().split()[:2]
        ssh.pin(lab.name, {(kind, b64)})  # pinned by an older ssh that preferred ECDSA
        assert run_here(ssh.run_argv(machine, "echo ok")).stdout == "ok\n"
        assert ssh.learn(machine).state == "pinned"  # not a false "changed"
        ssh.forget(lab.name)
        assert ssh.learn(machine).keys == lab.host_keys()  # nothing pinned: ssh's own first choice
    finally:
        lab.stop()


def test_no_key_that_signs_in_is_said_plainly(sshd):
    ssh.CONFIG_FILE.write_text("IdentitiesOnly yes\nIdentityAgent none\n")
    stranger = saved(sshd, key=str(sshd.stranger))
    ssh.pin(sshd.name, sshd.host_keys())
    done = run_here(ssh.run_argv(stranger, "true"))
    assert done.returncode == 255 and ssh.explain(done.stderr, stranger)[0] == "no_key"


def test_nothing_listening_is_said_plainly(sshd):
    closed = saved(sshd, port=sshd.port + 1 if sshd.port < 65535 else sshd.port - 1)
    with pytest.raises(SSHError) as caught:
        ssh.learn(closed)
    assert caught.value.code in ("refused", "unreachable")


def test_ixel_machines_connect_asks_once_then_signs_in(sshd, monkeypatch, capfd):
    saved(sshd, name="Lab box", agent="custom", command="echo connected to $(hostname)")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    answers = []
    monkeypatch.setattr("builtins.input", lambda prompt: answers.append(prompt) or "yes")
    assert cli.main(["connect", "lab BOX"]) == 0
    out = capfd.readouterr().out
    assert sshd.fingerprint in out and "connected to" in out and len(answers) == 1
    assert ssh.pinned(sshd.name) == sshd.host_keys()
    assert cli.main(["connect", "Lab box"]) == 0 and len(answers) == 1  # pinned: no question the second time
    actions = [line.split()[1] for line in log.LOG_FILE.read_text(encoding="utf-8").splitlines()[1:]]  # [0]: HEADER
    assert actions == ["HOSTKEY_PINNED", "CONNECT", "CONNECT"]
    assert "echo connected" not in log.LOG_FILE.read_text(encoding="utf-8")  # what ran on connecting isn't kept


# ── the page's API ──────────────────────────────────────────────────────────

def gui():
    return GuiServer(token=TOKEN, settings_loader=load_settings)


async def post(client, action, **body):
    resp = await client.post(f"/api/machines/{action}", headers=JSON_AUTH, data=json.dumps(body))
    return resp.status, await resp.json()


async def overview(client):
    resp = await client.get("/api/machines", headers=AUTH)
    assert resp.status == 200
    return await resp.json()


async def finished(client, run_id, show=""):
    for _ in range(300):
        resp = await client.get(f"/api/machines/run?id={run_id}&show={show}", headers=AUTH)
        state = await resp.json()
        if not state["running"]:
            return state
        await asyncio.sleep(0.1)
    raise AssertionError("the run didn't finish")


def test_the_page_checks_a_key_pins_only_what_was_shown_and_runs_on_every_machine(sshd, monkeypatch):
    opened = []
    monkeypatch.setattr(terminal, "open_window", lambda argv: opened.append(argv) or "Konsole")

    async def scenario(client):
        status, made = await post(client, "save", machine=sshd.machine(name="lab"))
        assert status == 200
        lab = made["machine"]["id"]
        _, other = await post(client, "save", machine=sshd.machine(name="unchecked", user="nobody-here"))
        before = await overview(client)
        refused = await post(client, "connect", id=lab)
        early = await post(client, "trust", id=lab, fingerprint=sshd.fingerprint)
        _, key = await post(client, "key", id=lab)
        wrong = (*await post(client, "trust", id=lab, fingerprint="SHA256:not-what-was-shown"),
                 ssh.pinned(sshd.name))
        trusted = await post(client, "trust", id=lab, fingerprint=key["fingerprint"])
        after = await overview(client)
        connected = await post(client, "connect", id=lab)
        resp = await client.post("/api/machines/run", headers=JSON_AUTH, data=json.dumps(
            {"command": "echo ran on $(whoami)", "timeout": 30, "ids": [lab, other["machine"]["id"]]}))
        started = await resp.json()
        done = await finished(client, started["id"], show=lab)
        return before, refused, early, key, wrong, trusted, after, connected, done

    before, refused, early, key, wrong, trusted, after, connected, done = run_with_client(gui(), scenario)
    assert before["ssh"] and [m["pin"]["state"] for m in before["machines"]] == ["none", "none"]
    assert before["machines"][0]["address"] == f"{sshd.user}@127.0.0.1:{sshd.port}"
    assert refused[0] == 409 and refused[1]["code"] == "not_pinned"
    assert early[0] == 409 and "Check its key again" in early[1]["error"]
    assert key["state"] == "new" and key["fingerprint"] == sshd.fingerprint
    assert wrong[0] == 409 and "isn't the fingerprint" in wrong[1]["error"] and wrong[2] == set()
    assert trusted[0] == 200 and ssh.pinned(sshd.name) == sshd.host_keys()
    assert after["machines"][0]["pin"] == {"state": "pinned", "fingerprints": [sshd.fingerprint]}
    assert connected == (200, {"terminal": "Konsole", "line": "ixel machines connect lab"})
    assert opened[0][opened[0].index("--") + 1:] == [f"{sshd.user}@127.0.0.1"]
    lab, unchecked = done["results"]
    assert lab["state"] == "ok" and lab["output"] == f"ran on {sshd.user}\n"
    # The other machine is the same server under the same name, so its key is pinned too: ssh refuses
    # the user it doesn't know
    assert unchecked["state"] == "error" and unchecked["hint_code"] == "no_key" and "output" not in unchecked


def test_a_changed_key_is_never_pinned_over_the_old_one(sshd, tmp_path):
    impostor = keygen(tmp_path / "impostor")
    ssh.pin(sshd.name, {tuple(impostor.with_name("impostor.pub").read_text().split()[:2])})

    async def scenario(client):
        _, made = await post(client, "save", machine=sshd.machine())
        lab = made["machine"]["id"]
        _, key = await post(client, "key", id=lab)
        refused = await post(client, "trust", id=lab, fingerprint=key["fingerprint"])
        forgot = await post(client, "forget", id=lab)
        _, again = await post(client, "key", id=lab)
        return key, refused, forgot, again

    key, refused, forgot, again = run_with_client(gui(), scenario)
    assert key["state"] == "changed" and key["fingerprint"] == sshd.fingerprint
    assert refused[0] == 409 and "won't pin it over the old one" in refused[1]["error"]
    assert forgot[0] == 200 and forgot[1]["forgot"] == 1 and again["state"] == "new"
    assert "HOSTKEY_CHANGED" in log.LOG_FILE.read_text() and "HOSTKEY_FORGOTTEN" in log.LOG_FILE.read_text()


def test_the_page_saves_imports_and_says_what_is_wrong(monkeypatch, tmp_path):
    monkeypatch.setattr(ssh, "make_key", lambda: ssh.NewKey(ssh.Path("/home/x/.ssh/ixel_ed25519"),
                                                            "ssh-ed25519 AAAA", "SHA256:x"))
    store.SSH_CONFIG.write_text("Host web db\n")
    store.CONSOLE_PROFILES.parent.mkdir(parents=True)
    store.CONSOLE_PROFILES.write_text(json.dumps([{"name": "Mini", "host": "mini.local", "port": 22},
                                                  {"name": "GW", "connection_type": "websocket"}]))
    ssh.CONSOLE_PINS.write_text("mini.local ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHN0dWZm\n")

    async def scenario(client):
        bad = await post(client, "save", machine={"host": "-oProxyCommand=touch /tmp/pwned"})
        notjson = await client.post("/api/machines/save", headers=JSON_AUTH, data="[1]")
        unknown = await client.post("/api/machines/explode", headers=JSON_AUTH, data="{}")
        noauth = await client.get("/api/machines")
        from_ssh = await post(client, "import", **{"from": "ssh_config"})
        from_console = await post(client, "import", **{"from": "console"})
        listing = await overview(client)
        gone = await post(client, "delete", id="not-an-id")
        norun = await client.post("/api/machines/run", headers=JSON_AUTH, data=json.dumps(
            {"command": "ls\nid", "timeout": 30, "ids": [listing["machines"][0]["id"]]}))
        floats = await client.post("/api/machines/run", headers=JSON_AUTH, data=json.dumps(
            {"command": "ls", "timeout": 120.0, "ids": [listing["machines"][0]["id"]]}))
        weird = await post(client, "save", machine={"host": "web", "agent": []})
        made = await post(client, "new-key")
        after = await overview(client)
        return bad, notjson.status, unknown.status, noauth.status, from_ssh, from_console, listing, gone, \
            (norun.status, await norun.json()), floats.status, weird, made, after

    bad, notjson, unknown, noauth, from_ssh, from_console, listing, gone, norun, floats, weird, made, after = \
        run_with_client(gui(), scenario)
    assert bad[0] == 400 and "isn't a name or address" in bad[1]["error"]
    assert notjson == 400 and unknown == 404 and noauth == 401
    assert from_ssh[1]["added"] == 2
    assert from_console[1]["added"] == 1 and "1 gateway chat profile stayed behind" in from_console[1]["message"]
    assert "ixel-console uninstall" in from_console[1]["message"]
    assert [m["name"] for m in listing["machines"]] == ["web", "db", "Mini"]
    assert listing["imports"] == {"ssh_config": 0, "console": {"machines": 0, "gateways": 1}}
    assert gone[0] == 404
    assert norun[0] == 400 and "one line" in norun[1]["error"] and floats == 400
    assert weird[0] == 200 and weird[1]["machine"]["agent"] == "shell"
    # A new key is only made: it becomes a machine's key when the form is saved
    assert made[0] == 200 and made[1]["path"] == str(ssh.Path("/home/x/.ssh/ixel_ed25519"))
    assert "Save" in made[1]["message"]
    assert all(m["key"] == "" for m in after["machines"])
