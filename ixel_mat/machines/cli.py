"""
`ixel machines`: your machines from the terminal.

    ixel machines                       list them, and whether each one's key is pinned
    ixel machines connect NAME          ssh into one here (checking its key first, the first time)
    ixel machines import ssh|console    bring in ~/.ssh/config's hosts, or Ixel Console's machines

Adding, editing and "Run on machines" are on the Machines page of the Ixel window (ixel app).
"""
from __future__ import annotations

import subprocess
import sys

from ixel_mat.config.secrets import child_env
from ixel_mat.machines import log, ssh, store
from ixel_mat.machines.ssh import SSHError
from ixel_mat.machines.store import MachineError
from ixel_mat.sanitize import sanitize_terminal_text

USAGE = __doc__.split("\n\n", 1)[1].rsplit("\n\n", 1)[0]


def _say(text: str = "", err: bool = False) -> None:
    print(sanitize_terminal_text(text), file=sys.stderr if err else sys.stdout)


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help", "help"):
        _say(USAGE)
        return 0
    try:
        if not argv or argv[0] in ("list", "ls"):
            return list_machines()
        if argv[0] == "connect" and len(argv) == 2:
            return connect(argv[1])
        if argv[0] == "import" and len(argv) == 2 and argv[1] in ("ssh", "console"):
            return bring_in(argv[1])
    except (MachineError, SSHError) as exc:
        _say(str(exc), err=True)
        return 1
    _say(USAGE, err=True)
    return 2


def list_machines() -> int:
    machines = store.Store().load()
    if not machines:
        _say("No machines yet. Add them on the Machines page (ixel app), or bring in your ~/.ssh/config hosts "
             "with: ixel machines import ssh")
        return 0
    program = ssh.ssh_program()
    width = max(len(m.name) for m in machines)
    for machine in machines:
        where = ssh.resolve(machine, program) if program else ssh.Where(machine.host, machine.port or 22,
                                                                        machine.user)
        pinned = "key pinned" if ssh.pinned(where.name) else "key not checked"
        runs = "a shell" if machine.agent == "shell" else machine.command
        _say(f"  {machine.name.ljust(width)}  {ssh.address(where)}  runs {runs}  ({pinned})")
    if not program:
        _say("\nssh isn't installed, or isn't on PATH, so none of them can connect yet.", err=True)
    return 0


def _ask(question: str) -> bool:
    try:
        return input(question).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        _say()
        return False


def connect(name: str) -> int:
    machine = store.Store().find(name)
    if machine is None:
        _say(f"There's no machine called {name!r}. `ixel machines` lists them.", err=True)
        return 1
    where = ssh.resolve(machine)
    if not ssh.pinned(where.name):
        _say(f"Checking {machine.name}'s key ({ssh.address(where)})…")
        learned = ssh.learn(machine)
        seen = learned.to_dict()
        if learned.state == "changed":
            log.write("HOSTKEY_CHANGED", machine=machine.name, name=learned.name, seen=seen["fingerprint"])
            _say(f"{machine.name}'s key has changed. It shows {seen['fingerprint']}, not the key pinned for it. "
                 "That's what someone listening in between looks like (or a reinstalled server). Ixel won't "
                 "connect: if you know why it changed, forget the old key on the Machines page.", err=True)
            return 1
        if learned.state == "new":
            _say(f"This is the first time Ixel connects to {seen['address']}. Its key says:\n\n"
                 f"    {seen['fingerprint']}  ({seen['key_type']})\n\n"
                 "To be sure, compare it with the server's own (on the server: ssh-keygen -lf "
                 "/etc/ssh/ssh_host_ed25519_key.pub). Once pinned, ssh only connects to a server with this key.")
            if not sys.stdin.isatty():
                _say("Run this in a terminal to say yes, or pin it on the Machines page.", err=True)
                return 1
            if not _ask("Trust and pin it? [y/N] "):
                return 1
            ssh.pin(learned.name, learned.keys)
            log.write("HOSTKEY_PINNED", machine=machine.name, name=learned.name, fingerprint=seen["fingerprint"])
    argv = ssh.connect_argv(machine)
    log.write("CONNECT", machine=machine.name, host=machine.host, command=machine.command, terminal="here")
    try:
        return subprocess.run(argv, env=child_env(nested=False)).returncode
    except KeyboardInterrupt:
        return 130


def bring_in(source: str) -> int:
    machines = store.Store()
    if source == "ssh":
        added, skipped = machines.add_many(store.from_ssh_config())
        log.write("IMPORT", source="ssh_config", added=len(added), skipped=skipped)
        _say(f"Added {len(added)} from ~/.ssh/config" + (f" ({skipped} already here)." if skipped else ".")
             if added or skipped else "There are no Host entries in ~/.ssh/config to add.")
        return 0
    found = store.from_console()
    if found is None:
        _say("Ixel Console's profiles aren't on this computer (~/.config/ixel-console).", err=True)
        return 1
    added, skipped = machines.add_many(found.machines)
    program = ssh.ssh_program()
    names = [((ssh.resolve(m, program).name if program else ssh.known_hosts_name(m.host, m.port or 22)),
              ssh.known_hosts_name(*found.names[m.id])) for m in added]
    keys, refused = ssh.adopt_console_pins(names)
    log.write("IMPORT", source="ixel-console", added=len(added), skipped=skipped, keys=keys, refused=refused)
    _say(f"Added {len(added)} from Ixel Console, with {keys} pinned key{'' if keys == 1 else 's'}"
         + (f" ({skipped} already here)." if skipped else "."))
    if refused:
        _say(f"For {refused} machine(s), Ixel Console had pinned a different key from the one already pinned "
             "here for the same address, so Ixel kept the one here. If you're not sure which is right, check "
             "its key again on the Machines page (in its settings).")
    if found.gateways:
        _say(f"{found.gateways} gateway chat profile(s) stayed behind: Machines connects over SSH only.")
    _say("When you're done with Ixel Console, remove it with: ixel-console uninstall")
    return 0
