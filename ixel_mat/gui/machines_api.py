"""
The Machines page (see ixel_mat/machines): your saved machines and whether each one's key is pinned,
adding and importing them, checking and pinning a key, Connect (a terminal window), a new key and
copying it to a server, and one command run on several machines.

Everything that runs ssh or touches a file blocks: the server runs those in a thread. Runs themselves
are asyncio (runs.py) and live in the server's loop.
"""
from __future__ import annotations

import time
from typing import Any

from ixel_mat.machines import log, runs, ssh, store, terminal
from ixel_mat.machines.ssh import SSHError
from ixel_mat.machines.store import Machine, MachineError

LEARNED_FOR = 15 * 60  # a key seen this long ago can still be pinned; after that, check it again


class MachinesApiError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "", **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.extra = extra


class Machines:
    """What the page works with: the saved machines, the keys just seen, and the runs."""

    def __init__(self, machine_store: store.Store | None = None):
        self.store = machine_store or store.Store()
        self.runs = runs.Runs()
        self._learned: dict[str, tuple[float, ssh.Learned]] = {}

    # ── reading ──────────────────────────────────────────────────────────────

    def _get(self, body: Any) -> Machine:
        if not isinstance(body, dict) or not isinstance(body.get("id"), str):
            raise MachinesApiError("Say which machine.")
        try:
            return self.store.get(body["id"])
        except MachineError as exc:
            raise MachinesApiError(str(exc), 404) from None

    def overview(self) -> dict:
        """The page's picture: every machine with where ssh takes it and whether its key is pinned."""
        try:
            machines = self.store.load()
            unreadable = self.store.unreadable()
            problem = ""
        except MachineError as exc:
            machines, unreadable, problem = [], 0, str(exc)
        program = ssh.ssh_program()
        listed = []
        for machine in machines:
            where = ssh.resolve(machine, program) if program else ssh.Where(machine.host, machine.port or 22,
                                                                            machine.user)
            keys = ssh.pinned(where.name)
            listed.append({**machine.to_dict(), "agent_label": store.AGENTS[machine.agent][0],
                           "address": ssh.address(where), "jump": where.jump,
                           "pin": {"state": "pinned" if keys else "none",
                                   "fingerprints": sorted(ssh.fingerprint(b) for _, b in keys)}})
        console = store.from_console()
        have = {store._same(m) for m in machines}
        return {
            "machines": listed, "problem": problem, "unreadable": unreadable,
            "ssh": bool(program), "terminal": terminal.label(terminal.find()),
            "imports": {
                "ssh_config": sum(1 for m in store.from_ssh_config() if store._same(m) not in have),
                "console": None if console is None else {
                    "machines": sum(1 for m in console.machines if store._same(m) not in have),
                    "gateways": console.gateways},
            },
            "agents": {name: {"label": label, "commands": list(commands)}
                       for name, (label, commands) in store.AGENTS.items()},
            "timeouts": list(runs.TIMEOUTS),
            "runs": [run.to_dict() for run in self.runs.latest()],
        }

    # ── changing the list ────────────────────────────────────────────────────

    def save(self, body: Any) -> dict:
        try:
            machine = self.store.save(body.get("machine") if isinstance(body, dict) else None)
        except MachineError as exc:
            raise MachinesApiError(str(exc)) from None
        return {"machine": machine.to_dict()}

    def delete(self, body: Any) -> dict:
        machine = self._get(body)
        try:
            self.store.delete(machine.id)
        except MachineError as exc:
            raise MachinesApiError(str(exc), 404) from None
        self._learned.pop(machine.id, None)
        return {"deleted": machine.name}

    def bring_in(self, body: Any) -> dict:
        """Import from ~/.ssh/config or from Ixel Console."""
        source = body.get("from") if isinstance(body, dict) else None
        if source == "ssh_config":
            added, skipped = self.store.add_many(store.from_ssh_config())
            log.write("IMPORT", source="ssh_config", added=len(added), skipped=skipped)
            message = (f"Added {_count(len(added), 'machine')} from ~/.ssh/config. ssh reads each one's address, "
                       "user, port, key and any jump host from that file every time." if added else
                       "Every Host in ~/.ssh/config is already here.")
            return {"added": len(added), "skipped": skipped, "message": message}
        if source != "console":
            raise MachinesApiError("Import from ~/.ssh/config or from Ixel Console.")
        found = store.from_console()
        if found is None:
            raise MachinesApiError("Ixel Console's profiles aren't on this computer (~/.config/ixel-console).", 404)
        added, skipped = self.store.add_many(found.machines)
        names = []
        program = ssh.ssh_program()
        for machine in added:
            host, port = found.names[machine.id]
            ours = ssh.resolve(machine, program).name if program else ssh.known_hosts_name(machine.host,
                                                                                          machine.port or 22)
            names.append((ours, ssh.known_hosts_name(host, port)))
        keys, refused = ssh.adopt_console_pins(names)
        log.write("IMPORT", source="ixel-console", added=len(added), skipped=skipped, keys=keys, refused=refused)
        said = [f"Added {_count(len(added), 'machine')} from Ixel Console"
                + (f", with {_count(keys, 'pinned key')}." if keys else ".") if added else
                "Ixel Console's machines are already here."]
        if refused:
            said.append(f"For {_count(refused, 'machine')}, Ixel Console had pinned a different key from the one "
                        "already pinned here for the same address, so Ixel kept the one here. If you're not sure "
                        "which is right, check its key again in its settings.")
        if found.gateways:
            said.append(f"{_count(found.gateways, 'gateway chat profile')} stayed behind: Machines connects "
                        "over SSH and doesn't chat with gateways.")
        if found.unreadable:
            said.append(f"{_count(found.unreadable, 'profile')} couldn't be read.")
        said.append("Key passphrases and gateway tokens Ixel Console kept in your keychain stay there; Ixel "
                    "doesn't need them. When you're done with Ixel Console, remove it with: ixel-console uninstall")
        return {"added": len(added), "skipped": skipped, "keys": keys, "message": " ".join(said)}

    # ── keys ────────────────────────────────────────────────────────────────

    def check_key(self, body: Any) -> dict:
        """Ask the server for its key (without signing in) and say how it compares with the pinned one."""
        machine = self._get(body)
        try:
            learned = ssh.learn(machine)
        except SSHError as exc:
            raise MachinesApiError(str(exc), 502 if exc.code in ("unreachable", "refused", "not_found") else 400,
                                   exc.code) from None
        self._learned[machine.id] = (time.monotonic(), learned)
        if learned.state == "changed":
            log.write("HOSTKEY_CHANGED", machine=machine.name, name=learned.name,
                      seen=learned.to_dict()["fingerprint"], pinned=", ".join(learned.to_dict()["pinned"]))
        return learned.to_dict()

    def trust(self, body: Any) -> dict:
        """Pin the key the person was just shown, and said yes to."""
        machine = self._get(body)
        seen = self._learned.get(machine.id)
        if seen is None or time.monotonic() - seen[0] > LEARNED_FOR:
            raise MachinesApiError("Check its key again first: Ixel pins only a key it has just seen.", 409)
        learned = seen[1]
        shown = body.get("fingerprint")
        if learned.state == "changed":
            raise MachinesApiError("This key isn't the one pinned. Ixel won't pin it over the old one: if the "
                                   "server really changed (reinstalled, a new key), forget the old key first.", 409)
        if shown != learned.to_dict()["fingerprint"]:
            raise MachinesApiError("That isn't the fingerprint Ixel showed. Check its key again.", 409)
        try:
            ssh.pin(learned.name, learned.keys)  # refused if another key was pinned for it since
        except SSHError as exc:
            self._learned.pop(machine.id, None)
            raise MachinesApiError(str(exc), 409 if exc.code == "changed" else 500, exc.code) from None
        except OSError as exc:
            raise MachinesApiError(f"Couldn't pin it: {exc}", 500) from None
        self._learned.pop(machine.id, None)
        log.write("HOSTKEY_PINNED", machine=machine.name, name=learned.name, fingerprint=shown)
        return {"pinned": True, "message": f"Pinned. ssh now accepts only this key for {machine.name}."}

    def forget(self, body: Any) -> dict:
        machine = self._get(body)
        program = ssh.ssh_program()
        name = ssh.resolve(machine, program).name if program else ssh.known_hosts_name(machine.host,
                                                                                      machine.port or 22)
        try:
            gone = ssh.forget(name)
        except OSError as exc:
            raise MachinesApiError(f"Couldn't change the pinned keys: {exc}", 500) from None
        self._learned.pop(machine.id, None)
        log.write("HOSTKEY_FORGOTTEN", machine=machine.name, name=name, keys=gone)
        others = [m.name for m in self.store.load() if m.id != machine.id
                  and (ssh.resolve(m, program).name if program else "") == name]
        return {"forgot": gone, "message": (f"Forgot the key pinned for {machine.name}" if gone else
                                            f"No key was pinned for {machine.name}")
                + (f" (and for {', '.join(others)}, the same server)" if others else "")
                + ". Check its key again before connecting."}

    def new_key(self, body: Any) -> dict:
        """A new key in ~/.ssh. The form puts its path in the Key file box: it's the machine's key once
        you save."""
        try:
            made = ssh.make_key()
        except SSHError as exc:
            raise MachinesApiError(str(exc), 500, exc.code) from None
        log.write("KEYGEN", path=made.path, fingerprint=made.fingerprint)
        return {"path": str(made.path), "fingerprint": made.fingerprint, "public": made.public,
                "message": f"Made {made.path} (no passphrase, so runs need no prompt). Save to use it for this "
                           "machine, then copy it to the server. To add a passphrase later, run in a terminal: "
                           f"ssh-keygen -p -f \"{made.path}\""}

    # ── connecting ─────────────────────────────────────────────────────────

    def _open(self, machine: Machine, argv: list[str], action: str) -> dict:
        line = ssh.connect_line(machine)
        try:
            used = terminal.open_window(argv)
        except SSHError as exc:
            raise MachinesApiError(str(exc), 409, exc.code, line=line) from None
        log.write(action, machine=machine.name, host=machine.host, terminal=used)
        return {"terminal": used, "line": line}

    def connect(self, body: Any) -> dict:
        machine = self._get(body)
        try:
            argv = ssh.connect_argv(machine)
        except SSHError as exc:
            log.write("BLOCKED", machine=machine.name, reason=exc.code)
            raise MachinesApiError(str(exc), 409, exc.code) from None
        return self._open(machine, argv, "CONNECT")

    def copy_key(self, body: Any) -> dict:
        machine = self._get(body)
        try:
            argv = ssh.copy_key_argv(machine)
        except SSHError as exc:
            raise MachinesApiError(str(exc), 409, exc.code) from None
        return self._open(machine, argv, "COPY_KEY")

    # ── runs ─────────────────────────────────────────────────────────────

    def plan_run(self, body: Any) -> tuple[str, int, list[runs.Target]]:
        """What a run will do, machine by machine (blocking: it reads ssh's config and the pins)."""
        if not isinstance(body, dict):
            raise MachinesApiError("Expected a JSON object.")
        try:
            command = runs.check_command(body.get("command"))
        except runs.RunError as exc:
            raise MachinesApiError(str(exc), exc.status) from None
        timeout = body.get("timeout", 120)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout not in runs.TIMEOUTS:
            raise MachinesApiError("Pick one of the time limits.")
        ids = body.get("ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            raise MachinesApiError("Pick the machines to run it on.")
        saved = {m.id: m for m in self.store.load()}
        missing = [i for i in ids if i not in saved]
        if missing:
            raise MachinesApiError("Some of those machines aren't saved any more. Pick them again.", 409)
        targets = []
        for machine_id in dict.fromkeys(ids):
            machine = saved[machine_id]
            try:
                argv = ssh.run_argv(machine, command)
                where = ssh.resolve(machine)
                targets.append(runs.Target(machine, argv, None, ssh.address(where), where))
            except SSHError as exc:
                targets.append(runs.Target(machine, None, (exc.code, str(exc)), machine.destination))
        return command, timeout, targets

    def start_run(self, planned: tuple[str, int, list[runs.Target]]) -> dict:
        try:
            run = self.runs.start(*planned)
        except runs.RunError as exc:
            raise MachinesApiError(str(exc), exc.status) from None
        return run.to_dict()

    def run_state(self, run_id: str | None, show: str | None) -> dict:
        try:
            run = self.runs.get(run_id)
        except runs.RunError as exc:
            raise MachinesApiError(str(exc), exc.status) from None
        return run.to_dict({s for s in (show or "").split(",") if s})

    async def stop_run(self, body: Any) -> dict:
        try:
            run = await self.runs.stop(body.get("id") if isinstance(body, dict) else None)
        except runs.RunError as exc:
            raise MachinesApiError(str(exc), exc.status) from None
        return run.to_dict()


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"
