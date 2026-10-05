"""
The machines you saved, in ~/.config/ixel-mat/machines.json (only you can read it), and bringing them
in from ~/.ssh/config or from Ixel Console. Nothing secret is kept here: no passwords, passphrases or
tokens. A key is a path to the key file, which ssh reads itself.
"""
from __future__ import annotations

import contextlib
import glob
import ipaddress
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ixel_mat.config.secrets import write_private_file

logger = logging.getLogger("ixel_mat.machines")

MACHINES_FILE = Path.home() / ".config" / "ixel-mat" / "machines.json"
SSH_CONFIG = Path.home() / ".ssh" / "config"
CONSOLE_PROFILES = Path.home() / ".config" / "ixel-console" / "profiles.json"

# What runs when you connect: the agent's own commands, a shell, or a command you type
AGENTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "openclaw": ("OpenClaw", ("openclaw tui", "openclaw status", "openclaw sessions", "openclaw logs")),
    "hermes": ("Hermes", ("hermes", "hermes status", "hermes sessions", "hermes logs")),
    "shell": ("A shell", ("",)),
    "custom": ("A command you choose", ()),
}
MAX_MACHINES = 500
MAX_COMMAND = 1024
MAX_NAME, MAX_GROUP, MAX_NOTES, MAX_PATH = 80, 60, 2000, 1024

# host and user go into ssh's argv: one starting with "-" would be read as an option ("-oProxyCommand=…"
# runs a program here), so only plain host and login names are taken
_USER_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._@-]{0,127}")
_LABEL_RE = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?")
_CONTROL = re.compile(r"[\x00-\x1f\x7f  ]")


class MachineError(ValueError):
    """What's wrong with a machine, in words for the person."""


def valid_host(host: str) -> bool:
    """A DNS name, an IP address (no brackets, no zone) or a Host name from ~/.ssh/config."""
    if not host or len(host) > 253:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return not getattr(ip, "scope_id", None)
    labels = (host[:-1] if host.endswith(".") else host).split(".")
    return all(_LABEL_RE.fullmatch(label) for label in labels)


def valid_user(user: str) -> bool:
    return bool(_USER_RE.fullmatch(user or ""))


def one_line(value: str) -> bool:
    return not _CONTROL.search(value)


@dataclass
class Machine:
    id: str
    name: str
    host: str
    user: str = ""            # "" = whatever ssh's config says (or your login name)
    port: int | None = None   # None = whatever ssh's config says (or 22)
    key: str = ""             # the private key file ssh signs in with; "" = ssh's own choice
    agent: str = "shell"
    command: str = ""         # what runs on connecting; "" = a shell
    group: str = ""
    notes: str = ""

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def to_dict(self) -> dict:
        return asdict(self)


def _text(data: dict, key: str, limit: int, what: str, *, lines: bool = False) -> str:
    value = data.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise MachineError(f"{what} must be text.")
    value = value.replace("\r\n", "\n").strip() if lines else value.strip()
    if len(value) > limit:
        raise MachineError(f"{what} is longer than {limit:,} characters.")
    if (_CONTROL.search(value.replace("\n", "")) if lines else _CONTROL.search(value)):
        raise MachineError(f"{what} has characters that can't be shown{'' if lines else ' (or a line break)'}.")
    return value


def check(data: Any, *, machine_id: str | None = None) -> Machine:
    """A Machine from what the page (or a file) gave, or MachineError saying what's wrong."""
    if not isinstance(data, dict):
        raise MachineError("Expected a machine.")
    host = _text(data, "host", 253, "The address")
    if not host:
        raise MachineError("Say where it is: its name or address, or a Host name from your ~/.ssh/config.")
    if not valid_host(host):
        raise MachineError(f"{host!r} isn't a name or address ssh can use: letters, digits, dots, - and _, "
                           "or an IP address (no brackets, and no port: that goes in Port).")
    user = _text(data, "user", 128, "The user")
    if user and not valid_user(user):
        raise MachineError("The user can only have letters, digits, '.', '_', '-' and '@', and can't start with "
                           "'-'.")
    port = data.get("port")
    if port in (None, "", 0):
        port = None
    else:
        if isinstance(port, str) and port.strip().isdigit():
            port = int(port.strip())
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise MachineError("The port is a number from 1 to 65535, or empty for ssh's own.")
    agent = data.get("agent") or "shell"
    if not isinstance(agent, str) or agent not in AGENTS:
        raise MachineError("Pick what runs when you connect: OpenClaw, Hermes, a shell, or a command.")
    command = _text(data, "command", MAX_COMMAND, "The command")
    presets = AGENTS[agent][1]
    if agent == "custom":
        if not command:
            raise MachineError("Type the command to run when you connect, or pick a shell.")
    elif command not in presets:
        if agent == "shell":
            command = ""
        else:
            raise MachineError(f"For {AGENTS[agent][0]}, pick one of: {', '.join(presets)}.")
    name = _text(data, "name", MAX_NAME, "The name") or host
    return Machine(
        id=machine_id or (data.get("id") if isinstance(data.get("id"), str) and data.get("id") else uuid.uuid4().hex),
        name=name, host=host, user=user, port=port,
        key=_text(data, "key", MAX_PATH, "The key file"),
        agent=agent, command=command,
        group=_text(data, "group", MAX_GROUP, "The group"),
        notes=_text(data, "notes", MAX_NOTES, "The notes", lines=True),
    )


@contextlib.contextmanager
def locked(path: Path, timeout: float = 10):
    """
    path to read, change and write back, by one program at a time: the app and `ixel machines` in a
    terminal can both change it, and the second would otherwise write over the first's change. The lock is
    path + ".lock" next to it. MachineError when another one holds it for longer than timeout seconds.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(f"{path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() > deadline:
                raise MachineError(f"Another Ixel is changing {path.name}. Try again in a moment.")
            time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _try_lock(fd: int) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    with contextlib.suppress(OSError):
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)


class Store:
    """machines.json, read and written whole, one change at a time (across programs too: locked()).
    Entries this version can't read are kept in the file as they are, so nothing you saved is lost."""

    def __init__(self, path: Path | None = None):
        self.path = path or MACHINES_FILE
        self._lock = threading.RLock()

    def _read(self) -> tuple[list[Machine], list]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return [], []
        except OSError as exc:
            raise MachineError(f"Couldn't read {self.path}: {exc.strerror or exc}") from None
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MachineError(f"{self.path} isn't valid JSON ({exc}). Fix it or move it away; Ixel won't "
                               "write over it.") from None
        entries = data.get("machines") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise MachineError(f"{self.path} doesn't hold a list of machines. Fix it or move it away; Ixel "
                               "won't write over it.")
        machines, kept, seen = [], [], set()
        for entry in entries:
            try:
                machine = check(entry)
                if not (isinstance(entry, dict) and isinstance(entry.get("id"), str)) or machine.id in seen:
                    raise MachineError("no id, or the same id twice")
            except MachineError as exc:
                logger.warning("left a machine in %s as it is: %s", self.path, exc)
                kept.append(entry)
                continue
            seen.add(machine.id)
            machines.append(machine)
        return machines, kept

    def _write(self, machines: list[Machine], kept: list) -> None:
        data = {"version": 1, "machines": [m.to_dict() for m in machines] + kept}
        write_private_file(self.path, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))

    def load(self) -> list[Machine]:
        with self._lock:
            return self._read()[0]

    def unreadable(self) -> int:
        """How many entries in the file this version left as they are."""
        with self._lock:
            return len(self._read()[1])

    def get(self, machine_id: Any) -> Machine:
        for machine in self.load():
            if machine.id == machine_id:
                return machine
        raise MachineError("That machine isn't saved any more.")

    def find(self, name: str) -> Machine | None:
        """By name, ignoring case (for `ixel machines connect NAME`); then by address."""
        wanted = name.strip().lower()
        machines = self.load()
        return (next((m for m in machines if m.name.lower() == wanted), None)
                or next((m for m in machines if m.host.lower() == wanted), None))

    def save(self, data: Any) -> Machine:
        """Add a machine, or change the one with data's id."""
        with self._lock, locked(self.path):
            machines, kept = self._read()
            wanted = data.get("id") if isinstance(data, dict) else None
            known = {m.id for m in machines}
            if wanted and wanted not in known:
                raise MachineError("That machine isn't saved any more.")
            machine = check(data, machine_id=wanted or uuid.uuid4().hex)
            if not wanted and len(machines) >= MAX_MACHINES:
                raise MachineError(f"That's {MAX_MACHINES} machines, as many as Ixel keeps.")
            machines = [machine if m.id == machine.id else m for m in machines]
            if not wanted:
                machines.append(machine)
            self._write(machines, kept)
            return machine

    def delete(self, machine_id: Any) -> Machine:
        with self._lock, locked(self.path):
            machines, kept = self._read()
            gone = next((m for m in machines if m.id == machine_id), None)
            if gone is None:
                raise MachineError("That machine isn't saved any more.")
            self._write([m for m in machines if m.id != machine_id], kept)
            return gone

    def add_many(self, found: list[Machine]) -> tuple[list[Machine], int]:
        """Add machines found elsewhere, leaving out any already saved (same address, user and port).
        → (added, how many were already here)."""
        with self._lock, locked(self.path):
            machines, kept = self._read()
            have = {_same(m) for m in machines}
            added = []
            for machine in found:
                if _same(machine) in have:
                    continue
                if len(machines) + len(added) >= MAX_MACHINES:
                    break
                have.add(_same(machine))
                added.append(machine)
            if added:
                self._write(machines + added, kept)
            return added, len(found) - len(added)


def _same(machine: Machine) -> tuple:
    return machine.host.lower().rstrip("."), machine.user, machine.port or 22


# ── ~/.ssh/config ─────────────────────────────────────────────────────────────

def ssh_config_hosts(path: Path | None = None) -> list[str]:
    """The Host names in ~/.ssh/config (and the files it Includes) that name one machine: no wildcards
    or negations. ssh reads the rest (the address, user, port, key and any jump host) itself."""
    path = path or SSH_CONFIG
    found: list[str] = []
    _read_ssh_config(path, path.parent, found, set(), 0)
    return list(dict.fromkeys(found))


def _read_ssh_config(path: Path, ssh_dir: Path, found: list[str], seen: set, depth: int) -> None:
    if depth > 8 or path in seen:
        return
    seen.add(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\s*=\s*|\s+", line, maxsplit=1)
        if len(parts) < 2:
            continue
        keyword, value = parts[0].lower(), parts[1].strip()
        if keyword == "host":
            for pattern in _words(value):
                if not any(ch in pattern for ch in "*?!") and valid_host(pattern):
                    found.append(pattern)
        elif keyword == "include":
            for pattern in _words(value):
                pattern = os.path.expanduser(pattern)
                if not os.path.isabs(pattern):
                    pattern = str(ssh_dir / pattern)
                for match in sorted(glob.glob(pattern)):
                    _read_ssh_config(Path(match), ssh_dir, found, seen, depth + 1)


def _words(value: str) -> list[str]:
    """ssh_config's words: split on spaces, with "double quotes" holding one together."""
    return [a or b for a, b in re.findall(r'"([^"]*)"|(\S+)', value)]


def from_ssh_config(path: Path | None = None) -> list[Machine]:
    return [Machine(id=uuid.uuid4().hex, name=host, host=host) for host in ssh_config_hosts(path)]


# ── Ixel Console ──────────────────────────────────────────────────────────────

@dataclass
class ConsoleImport:
    machines: list[Machine]
    names: dict[str, tuple[str, int]]  # machine id → the host and port Ixel Console pinned its key under
    gateways: int                      # gateway (WebSocket) profiles, which Machines doesn't bring over
    unreadable: int


def from_console(path: Path | None = None) -> ConsoleImport | None:
    """Ixel Console's SSH profiles as machines, or None if it has none here."""
    path = path or CONSOLE_PROFILES
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, list):
        return None
    machines, names, gateways, unreadable = [], {}, 0, 0
    for profile in data:
        if not isinstance(profile, dict):
            unreadable += 1
            continue
        if profile.get("connection_type", "ssh") != "ssh":
            gateways += 1
            continue
        agent = profile.get("agent") if profile.get("agent") in ("openclaw", "hermes", "custom") else "custom"
        command = profile.get("remote_command") if isinstance(profile.get("remote_command"), str) else ""
        if agent == "custom" and not command.strip():
            agent = "shell"
        port = profile.get("port", 22)
        group = profile.get("group") if isinstance(profile.get("group"), str) else ""
        try:
            machine = check({
                "name": profile.get("name"), "host": profile.get("host"), "user": profile.get("user"),
                "port": None if port in (22, "22", None) else port, "key": profile.get("identity_file"),
                "agent": agent, "command": command, "group": "" if group == "Default" else group,
                "notes": profile.get("notes"),
            })
            pinned_port = int(port or 22)
        except (MachineError, TypeError, ValueError):
            unreadable += 1
            continue
        machines.append(machine)
        names[machine.id] = (machine.host.lower(), pinned_port)
    if not (machines or gateways or unreadable):
        return None
    return ConsoleImport(machines, names, gateways, unreadable)
