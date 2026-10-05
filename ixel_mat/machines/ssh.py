"""
The ssh lines Machines runs, and each server's host key.

Keys are pinned in Ixel's own file (~/.config/ixel-mat/machines_known_hosts), not ~/.ssh/known_hosts.
Every connection runs ssh with StrictHostKeyChecking=yes against that file alone (no KnownHostsCommand
either), so ssh itself refuses a server whose key has changed, and it never adds a key on its own. A
server's first key is learned by ssh too, into a temporary file, with every way of signing in switched off
and nothing of yours sent along, so a Host from ~/.ssh/config works with its HostName, User, Port and
ProxyJump. It's pinned only once you've seen its fingerprint and said yes: trust on first use. A key is
never pinned beside a different one already pinned for the same name.

A jump host (ProxyJump) is ssh's own hop: ssh checks the jump host's key against your ~/.ssh/known_hosts,
as it always does, and Ixel's options reach only the machine at the end.

Nothing here goes through a shell on this computer: every program gets its argv as a list, a machine's
host and user can't be read as options (store.py), and the destination follows "--".
"""
from __future__ import annotations

import base64
import contextlib
import fnmatch
import functools
import hashlib
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ixel_mat.agents.launch import NO_WINDOW_FLAGS, find_on_path
from ixel_mat.config.secrets import child_env, write_private_file
from ixel_mat.machines.store import Machine, MachineError, locked, valid_host

PINS_FILE = Path.home() / ".config" / "ixel-mat" / "machines_known_hosts"
CONSOLE_PINS = Path.home() / ".local" / "share" / "ixel-console" / "ssh_known_hosts"
WINDOWS = sys.platform == "win32"
MAC = sys.platform == "darwin"
# ssh reads ~/.ssh/config from your account's home folder, whatever $HOME says; a file set here is read
# instead (with -F), for tests
CONFIG_FILE: Path | None = None

_KEY_TYPE_RE = re.compile(r"(?:ssh|ecdsa|sk|rsa)-[a-z0-9@.-]{1,60}")
_KEY_B64_RE = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
_NAME_RE = re.compile(r"\[([^\]\s]+)\]:(\d{1,5})")
_ROUTE_RE = re.compile(r"[0-9a-f]{10}")
_KEY_COMMENT_RE = re.compile(r"[A-Za-z0-9@._+-]{1,100}")
_pins_lock = threading.Lock()  # one change to the pins file at a time (and locked(), across programs)


class SSHError(Exception):
    """Why ssh couldn't do it, in words for the person. code is a short fixed word for the page."""

    def __init__(self, message: str, code: str = "ssh"):
        super().__init__(message)
        self.code = code


# ── finding ssh ──────────────────────────────────────────────────────────────

def ssh_program(name: str = "ssh") -> str | None:
    """
    ssh or ssh-keygen's full path, from PATH only (never the current folder).

    On a Mac, an ssh on PATH that can't read ~/.ssh/config gives way to Apple's /usr/bin/ssh: Homebrew's
    OpenSSH refuses the whole file over the Mac-only UseKeychain option (GitHub's Mac guide adds it),
    ending every connection before it starts. One that reads the config stays first.
    """
    found = find_on_path(name)
    apple = f"/usr/bin/{name}"
    if (MAC and name == "ssh" and found and found != apple and os.path.isfile(apple)
            and not _reads_ssh_config(found, _config_stamp())):
        return apple
    return found


def need(name: str = "ssh") -> str:
    found = ssh_program(name)
    if not found:
        raise SSHError(f"{name} isn't installed, or isn't on PATH. " + (
            "On Windows, add it in Settings > System > Optional features > OpenSSH Client." if WINDOWS else
            "Install OpenSSH's client (openssh-client, or openssh)."), "no_ssh")
    return found


def _config_args() -> list[str]:
    return ["-F", str(CONFIG_FILE)] if CONFIG_FILE else []


def _account_home() -> str:
    """The home folder ssh reads ~/.ssh from: the account's own, whatever $HOME says."""
    if not WINDOWS:
        try:
            import pwd
            return pwd.getpwuid(os.getuid()).pw_dir
        except (ImportError, KeyError, OSError):
            pass
    return os.path.expanduser("~")


def _config_stamp() -> tuple:
    """ssh's config's change time and size, and the minute: after an edit, things read from it are read
    again (an edit to a file it Includes, within a minute)."""
    minute = int(time.monotonic() // 60)
    try:
        st = os.stat(CONFIG_FILE or os.path.join(_account_home(), ".ssh", "config"))
        return str(CONFIG_FILE or ""), st.st_mtime_ns, st.st_size, minute
    except OSError:
        return str(CONFIG_FILE or ""), minute


@contextlib.contextmanager
def _pins_locked(path: Path):
    try:
        with _pins_lock, locked(path):
            yield
    except MachineError as exc:
        raise SSHError(str(exc), "busy") from None


def _program_stamp(ssh: str) -> tuple:
    try:
        st = os.stat(ssh)
        return st.st_mtime_ns, st.st_size
    except OSError:
        return ()


def stops_known_hosts_command(ssh: str) -> bool:
    """Whether this ssh takes KnownHostsCommand=none (OpenSSH 8.5 and later). Asked of ssh itself, not of
    your config: a KnownHostsCommand could appear in a file the config Includes at any moment."""
    return _takes_option(ssh, "KnownHostsCommand=none", _program_stamp(ssh))


@functools.lru_cache(maxsize=8)
def _takes_option(ssh: str, option: str, stamp: tuple) -> bool:
    try:
        result = subprocess.run([ssh, "-F", os.devnull, "-o", option, "-G", "localhost"], stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=5, env=quiet_env(), **_quiet())
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


@functools.lru_cache(maxsize=8)
def _reads_ssh_config(ssh: str, stamp: tuple = ()) -> bool:
    """False only when this ssh stops on the config with "Bad configuration option"."""
    try:
        result = subprocess.run([ssh, *_config_args(), "-o", "CanonicalizeHostname=no", "-G", "localhost"],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=5, **_quiet())
    except (OSError, subprocess.SubprocessError):
        return True
    return not (result.returncode != 0 and b"bad configuration option" in result.stderr.lower())


def _quiet() -> dict:
    """For an ssh that mustn't ask anything: no terminal to ask on, no window, nothing on stdin."""
    if WINDOWS:
        return {"creationflags": NO_WINDOW_FLAGS}
    return {"start_new_session": True}


def quiet_env() -> dict[str, str]:
    """Ixel's API keys withheld (as for every program it starts), and no password window popping up."""
    return {**child_env(nested=False), "SSH_ASKPASS_REQUIRE": "never"}


# ── where a machine is ──────────────────────────────────────────────────────

@dataclass
class Where:
    """What ssh makes of a machine's host, with ~/.ssh/config applied."""
    hostname: str
    port: int
    user: str
    jump: str = ""
    proxy: str = ""                     # a ProxyCommand
    send_env: tuple[str, ...] = ()      # SendEnv patterns: environment variables ssh sends the server
    set_env: bool = False               # the config has SetEnv (values ssh sends the server)

    @property
    def name(self) -> str:
        """
        The name its key is pinned under. Through a jump host or a proxy, the same address can be another
        server (10.0.0.5 at the office and at the data centre), so the way there is part of the name.
        """
        base = known_hosts_name(self.hostname, self.port)
        route = f"jump {self.jump}" if self.jump else f"proxy {self.proxy}" if self.proxy else ""
        return f"{base}~{hashlib.sha256(route.encode()).hexdigest()[:10]}" if route else base


def known_hosts_name(host: str, port: int) -> str:
    host = host.lower()
    return host if port == 22 else f"[{host}]:{port}"


def _port_args(machine: Machine) -> list[str]:
    return ["-p", str(machine.port)] if machine.port else []


def resolve(machine: Machine, ssh: str | None = None, *, fresh: bool = False) -> Where:
    """Where ssh will connect for this machine (`ssh -G`, which reads the config and connects nowhere).
    fresh: read just now, not what was read in the last minute."""
    resolver = _resolve.__wrapped__ if fresh else _resolve
    return resolver(ssh or need(), machine.host, machine.user, machine.port, _config_stamp())


@functools.lru_cache(maxsize=256)
def _resolve(ssh: str, host: str, user: str, port: int | None, stamp) -> Where:
    destination = f"{user}@{host}" if user else host
    try:
        result = subprocess.run([ssh, *_config_args(), *(["-p", str(port)] if port else []), "-G", "--",
                                 destination],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=5, env=quiet_env(),
                                **_quiet())
    except (OSError, subprocess.SubprocessError):
        result = None
    seen: dict[str, str] = {}
    send_env: list[str] = []
    if result is not None and result.returncode == 0:
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            key, _, value = line.partition(" ")
            seen.setdefault(key.lower(), value.strip())
            if key.lower() == "sendenv":
                send_env += value.split()
    hostname = seen.get("hostname", "")
    if not valid_host(hostname):
        hostname = host
    try:
        resolved_port = int(seen.get("port", port or 22))
    except ValueError:
        resolved_port = port or 22
    if not 1 <= resolved_port <= 65535:
        resolved_port = 22
    jump, proxy = seen.get("proxyjump", ""), seen.get("proxycommand", "")
    return Where(hostname, resolved_port, seen.get("user", user), "" if jump == "none" else jump,
                 "" if proxy == "none" else proxy, tuple(send_env), "setenv" in seen)


# ── pinned keys ──────────────────────────────────────────────────────────────

def fingerprint(b64: str) -> str:
    try:
        raw = base64.b64decode(b64, validate=True)
    except ValueError:
        return "(unreadable key)"
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def _valid_name(name: str) -> bool:
    name, tagged, route = name.partition("~")
    if tagged and not _ROUTE_RE.fullmatch(route):
        return False
    match = _NAME_RE.fullmatch(name)
    if match:
        return valid_host(match[1]) and 1 <= int(match[2]) <= 65535
    return valid_host(name)


def _read_text(path: Path) -> str:
    """A known_hosts file as it is (bytes that aren't UTF-8 kept, to be written back unchanged)."""
    try:
        return path.read_text(encoding="utf-8", errors="surrogateescape")
    except FileNotFoundError:
        return ""


def _write_text(path: Path, text: str) -> None:
    write_private_file(path, text.encode("utf-8", "surrogateescape"))


def _read_lines(path: Path) -> list[tuple[list[str], str, str]]:
    """(names, key type, base64 key) for each line of a known_hosts file; markers and hashed names skipped."""
    try:
        text = _read_text(path)
    except OSError:
        return []  # unreadable: nothing is pinned, so nothing connects
    return _lines_of(text)


def _lines_of(text: str) -> list[tuple[list[str], str, str]]:
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and not parts[0].startswith(("#", "@", "|")):
            out.append(([n.lower() for n in parts[0].split(",")], parts[1], parts[2]))
    return out


def pinned(name: str, path: Path | None = None) -> set[tuple[str, str]]:
    """The (key type, base64 key) pairs pinned for name."""
    name = name.lower()
    return {(kind, b64) for names, kind, b64 in _read_lines(path or PINS_FILE) if name in names}


def pin(name: str, keys: set[tuple[str, str]] | list[tuple[str, str]], path: Path | None = None) -> int:
    """
    Pin keys for name (ssh will accept only these). → how many were new. When other keys are already
    pinned for name and these aren't among them, SSHError ("changed"): ssh would accept either one, so a
    second server's key never goes in beside the first's.
    """
    path = path or PINS_FILE
    if not _valid_name(name):
        raise SSHError(f"Ixel won't pin a key for {name!r}.", "invalid")
    keys = list(dict.fromkeys(keys))
    for kind, b64 in keys:
        if not (_KEY_TYPE_RE.fullmatch(kind) and _KEY_B64_RE.fullmatch(b64)):
            raise SSHError(f"That isn't a key Ixel can pin for {name}.", "invalid")
    with _pins_locked(path):
        text = _read_text(path)  # what's checked is what's written back
        have = {(kind, b64) for names, kind, b64 in _lines_of(text) if name.lower() in names}
        if have and not have & set(keys):
            raise SSHError("A different key is already pinned for this address. Ixel won't pin a second one "
                           "beside it: if the server really changed, forget the old key first.", "changed")
        new = [key for key in keys if key not in have]
        if new:
            if text and not text.endswith("\n"):
                text += "\n"
            text += "".join(f"{name.lower()} {kind} {b64}\n" for kind, b64 in new)
            _write_text(path, text)
    return len(new)


def forget(name: str, path: Path | None = None) -> int:
    """Remove every key pinned for name. → how many lines went."""
    path = path or PINS_FILE
    name = name.lower()
    with _pins_locked(path):
        lines = _read_text(path).splitlines()
        keep = [line for line in lines
                if not (line.split() and name in [n.lower() for n in line.split()[0].split(",")])]
        if len(keep) != len(lines):
            _write_text(path, "\n".join(keep) + ("\n" if keep else ""))
    return len(lines) - len(keep)


def adopt_console_pins(names: list[tuple[str, str]], source: Path | None = None,
                       path: Path | None = None) -> tuple[int, int]:
    """
    Copy the keys Ixel Console pinned: for each (Ixel's name, Console's name). → (keys pinned, machines
    left out because a different key is already pinned for them here).
    """
    lines = _read_lines(source or CONSOLE_PINS)
    count = refused = 0
    for ours, theirs in names:
        keys = {(kind, b64) for found, kind, b64 in lines if theirs.lower() in found
                and _KEY_TYPE_RE.fullmatch(kind) and _KEY_B64_RE.fullmatch(b64)}
        if keys:
            try:
                count += pin(ours, keys, path)
            except SSHError as exc:
                refused += exc.code == "changed"
    return count, refused


def _ssh_path(path: Path) -> str:
    """A path inside an -o option: quoted for ssh's own parser, its % tokens kept literal."""
    return '"' + str(path).replace("%", "%%") + '"'


def pin_options(name: str, path: Path | None = None, *, known_hosts_command: bool = False) -> list[str]:
    """
    ssh options that accept only the keys pinned for name, in Ixel's file alone. known_hosts_command: this
    ssh can switch KnownHostsCommand off (OpenSSH 8.5 and later; one set in your config would vouch for
    keys too). Older ones don't have it, so there it can't be set either.
    """
    return [
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={_ssh_path(path or PINS_FILE)}",
        "-o", f"GlobalKnownHostsFile={os.devnull}",
        *(["-o", "KnownHostsCommand=none"] if known_hosts_command else []),
        "-o", f"HostKeyAlias={name}",
        "-o", "CheckHostIP=no",
        "-o", "UpdateHostKeys=no",
        "-o", "VerifyHostKeyDNS=no",
        # A connection your own ssh already has open (ControlMaster) was checked against your known_hosts,
        # not this: never share one
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none",
    ]


@dataclass
class Learned:
    """A server's key as ssh saw it just now, against what's pinned for it."""
    where: Where
    keys: set[tuple[str, str]]
    state: str                    # pinned (it matches) | new (nothing pinned) | changed
    pinned: set[tuple[str, str]] = field(default_factory=set)

    @property
    def name(self) -> str:
        return self.where.name

    def to_dict(self) -> dict:
        kind, b64 = sorted(self.keys)[0]
        return {"state": self.state, "name": self.name, "key_type": kind, "fingerprint": fingerprint(b64),
                "pinned": sorted(fingerprint(b) for _, b in self.pinned), "address": address(self.where),
                "jump": self.where.jump}


def address(where: Where) -> str:
    host = f"[{where.hostname}]" if ":" in where.hostname else where.hostname
    return f"{where.user + '@' if where.user else ''}{host}{'' if where.port == 22 else f':{where.port}'}"


def learn(machine: Machine, *, timeout: float = 30) -> Learned:
    """Ask the server for its key with ssh (into a temporary file), without signing in."""
    ssh = need()
    where = resolve(machine, ssh, fresh=True)
    have = pinned(where.name)
    keys: set[tuple[str, str]] = set()
    said = ""
    if have:
        # Asked for the pinned key's type: ssh would otherwise ask for its own favourite, and a server
        # with several keys would show another one, which looks like a changed key. If the server no
        # longer has that type at all, it's asked again for whatever it has
        keys, said = _ask_for_key(ssh, machine, where, timeout, _host_key_algorithms(have))
        if not keys and "no matching host key type" not in said.lower():
            code, message = explain(said, machine, where)
            raise SSHError(message, code)
    if not keys:
        keys, said = _ask_for_key(ssh, machine, where, timeout)
    if not keys:
        code, message = explain(said, machine, where)
        raise SSHError(message, code)
    state = "new" if not have else "pinned" if keys & have else "changed"
    return Learned(where, keys, state, have)


def _host_key_algorithms(keys: set[tuple[str, str]]) -> str:
    """The host key algorithms that show these keys (an RSA key, with SHA-2 signatures)."""
    out: list[str] = []
    for kind, _ in sorted(keys):
        out += ["rsa-sha2-512", "rsa-sha2-256"] if kind == "ssh-rsa" else [kind]
    return ",".join(dict.fromkeys(out))


def _ask_for_key(ssh: str, machine: Machine, where: Where, timeout: float,
                 algorithms: str = "") -> tuple[set[tuple[str, str]], str]:
    """The keys the server showed ssh (none if it didn't get that far), and what ssh said."""
    with tempfile.TemporaryDirectory(prefix="ixel-hostkey-") as folder:
        known = Path(folder) / "known_hosts"
        argv = [ssh, *_config_args(), *_port_args(machine),
                *(["-o", f"HostKeyAlgorithms={algorithms}"] if algorithms else []),
                "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"UserKnownHostsFile={_ssh_path(known)}", "-o", f"GlobalKnownHostsFile={os.devnull}",
                *(["-o", "KnownHostsCommand=none"] if stops_known_hosts_command(ssh) else []),
                "-o", f"HostKeyAlias={where.name}", "-o", "HashKnownHosts=no", "-o", "CheckHostIP=no",
                "-o", "UpdateHostKeys=no", "-o", "VerifyHostKeyDNS=no", "-o", "ConnectTimeout=10",
                "-o", "ControlMaster=no", "-o", "ControlPath=none",
                # No way to sign in, so nothing is sent but the hello: ssh stops at "Permission denied"
                "-o", "PubkeyAuthentication=no", "-o", "PasswordAuthentication=no",
                "-o", "KbdInteractiveAuthentication=no", "-o", "GSSAPIAuthentication=no",
                "-o", "HostbasedAuthentication=no",
                # A server can still let anyone in with no credentials at all (so can whoever sits in between,
                # before the key is checked), so nothing of yours goes along: no agent, no X11, no tunnel, no
                # forwards, and no command from your config
                "-o", "IdentityAgent=none", "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
                "-o", "ForwardX11Trusted=no", "-o", "Tunnel=no", "-o", "ClearAllForwardings=yes",
                "-o", "PermitLocalCommand=no", "-o", "RemoteCommand=none", "-o", "RequestTTY=no",
                # Nor environment variables: SetEnv's values give way to a harmless one (the first value
                # wins; an ssh with SetEnv in its config has the option), and SendEnv finds none to send
                *(["-o", "SetEnv=IXEL_CHECK=1"] if where.set_env else []),
                "--", machine.destination, "exit"]
        env = {name: value for name, value in quiet_env().items()
               if not any(fnmatch.fnmatchcase(name, pattern) for pattern in where.send_env
                          if not pattern.startswith("-"))}
        try:
            _, said = _run_bounded(argv, timeout, env)
        except subprocess.TimeoutExpired:
            raise SSHError(f"{machine.name} didn't answer within {int(timeout)} seconds."
                           + (_jump_hint(where.jump) if where.jump else ""), "unreachable") from None
        except OSError as exc:
            raise SSHError(f"Couldn't start ssh: {exc.strerror or exc}", "no_ssh") from None
        return {(kind, b64) for _, kind, b64 in _read_lines(known)
                if _KEY_TYPE_RE.fullmatch(kind) and _KEY_B64_RE.fullmatch(b64)}, said


def _run_bounded(argv: list[str], timeout: float, env: dict[str, str] | None = None) -> tuple[int, str]:
    """
    Run argv with nothing on stdin and for at most timeout seconds. → (exit code, what it wrote to stderr).
    stderr goes to a file, not a pipe: a program ssh starts (a jump host's ssh) can hold a pipe open after
    ssh has gone, and waiting for that would never end. On a timeout, everything it started is ended too.
    """
    with tempfile.TemporaryFile() as said:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=said,
                                env=quiet_env() if env is None else env, **_quiet())
        try:
            code = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            _end_tree(proc)
            raise
        size = said.seek(0, os.SEEK_END)
        said.seek(max(0, size - 65536))  # the last of it, where ssh says why it stopped
        return code, said.read().decode("utf-8", "replace")


def _end_tree(proc: subprocess.Popen) -> None:
    """End proc and whatever it started (it leads a process group of its own, or on Windows, a tree)."""
    try:
        if WINDOWS:
            taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
            subprocess.run([taskkill, "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=10, creationflags=NO_WINDOW_FLAGS)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass
    proc.wait()


def _jump_hint(jump: str) -> str:
    hops = [hop.strip() for hop in jump.split(",") if hop.strip()]
    first = hops[0] if hops else jump
    # ProxyJump's user@host:port isn't a destination ssh takes as it is; its ssh:// form is
    line = "ssh " + (first if first.startswith("ssh://") or not re.search(r":\d+$", first) else f"ssh://{first}")
    return (f" It goes through the jump host {first}, whose key ssh checks in your own known_hosts: if you've "
            f"never connected to it, connect once in a terminal ({line}) and say yes there"
            + (", then do the same for each hop after it." if len(hops) > 1 else "."))


# ── what ssh said ───────────────────────────────────────────────────────────

def explain(stderr: str, machine: Machine, where: Where | None = None) -> tuple[str, str]:
    """(code, message) for an ssh that failed, from what it wrote."""
    text = stderr.lower()
    host = where.hostname if where else machine.host
    port = where.port if where else (machine.port or 22)
    if "remote host identification has changed" in text or "host key verification failed" in text:
        if where and where.jump:
            return "hostkey", (f"ssh stopped at a host key: {machine.name}'s isn't the one pinned for it, or "
                               f"the jump host's isn't in your known_hosts." + _jump_hint(where.jump)
                               + f" Then check {machine.name}'s key on the Machines page.")
        return "hostkey", (f"{machine.name}'s key isn't the one pinned for it (or none is pinned), so ssh "
                           "stopped. Check its key on the Machines page.")
    if "unprotected private key file" in text or "bad permissions" in text:
        return "key_perms", (f"Others can read the key file, so ssh won't use it. Run: chmod 600 {machine.key}"
                             if machine.key and not WINDOWS else
                             "Others can read the key file, so ssh won't use it. Make it yours alone.")
    if "permission denied" in text or "too many authentication failures" in text:
        return "no_key", (f"No key could sign in to {machine.name}. Start ssh-agent and add your key "
                          "(ssh-add), set this machine's key file, or connect in a terminal, where a password "
                          "works.")
    if "could not resolve hostname" in text or "name or service not known" in text:
        return "not_found", (f"ssh can't find {host}. Check the name, and that you're on its network "
                             "(Tailscale or a VPN).")
    if "connection refused" in text:
        return "refused", f"{host} turned the connection away on port {port}. Is ssh running there, on that port?"
    if any(s in text for s in ("timed out", "no route to host", "network is unreachable", "host is down")):
        return "unreachable", f"{host} didn't answer on port {port}. Is it on, and on this network?"
    if "bad configuration option" in text or "bad owner or permissions" in text or "terminating" in text:
        return "config", f"ssh can't read its settings: {_last_line(stderr)}"
    if "no such identity" in text or ("identity file" in text and "not accessible" in text):
        return "key_missing", f"The key file {machine.key or ''} isn't there.".replace("  ", " ")
    return "ssh", _last_line(stderr) or "ssh stopped without saying why."


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()
             if line.strip() and not line.startswith(("Warning: Permanently added", "@@@"))]
    return lines[-1][:400] if lines else ""


# ── the lines that run ──────────────────────────────────────────────────────

def _key_args(machine: Machine) -> list[str]:
    if not machine.key:
        return []
    key = Path(machine.key).expanduser()
    if not key.is_file():
        raise SSHError(f"{machine.name}'s key file isn't there: {key}", "key_missing")
    return ["-i", str(key)]


def connect_line(machine: Machine) -> str:
    """`ixel machines connect NAME`, quoted for the shell it's pasted into (PowerShell, on Windows)."""
    if WINDOWS:
        name = machine.name if re.fullmatch(r"[A-Za-z0-9 ._@+-]+", machine.name) else None
        # In PowerShell's single quotes, each of its four single-quote characters is doubled
        return f'ixel machines connect "{name}"' if name else \
            "ixel machines connect '" + re.sub("(['\u2018\u2019\u201a\u201b])", r"\1\1", machine.name) + "'"
    return f"ixel machines connect {shlex.quote(machine.name)}"


def _checked(machine: Machine, where: Where) -> None:
    if not pinned(where.name):
        raise SSHError(f"{machine.name}'s key isn't pinned yet. Check its key first, on the Machines page or "
                       f"with: {connect_line(machine)}", "not_pinned")


def _pinned_to(where: Where, ssh: str) -> list[str]:
    return pin_options(where.name, known_hosts_command=stops_known_hosts_command(ssh))


# The command given here replaces any RemoteCommand in your config (which ssh would otherwise refuse to
# combine with it)
_OUR_COMMAND = ["-o", "RemoteCommand=none"]


def connect_argv(machine: Machine, *, command: str | None = None) -> list[str]:
    """ssh into the machine, in a terminal: its command (or a shell), its key pinned. Passwords work."""
    ssh = need()
    where = resolve(machine, ssh)
    _checked(machine, where)
    run = machine.command if command is None else command
    return [ssh, *_config_args(), "-t", *_port_args(machine), *_key_args(machine), *_pinned_to(where, ssh),
            "-o", "BatchMode=no", *(_OUR_COMMAND if run else []), "--", machine.destination, *([run] if run else [])]


def run_argv(machine: Machine, command: str) -> list[str]:
    """ssh that runs one command and answers nothing: no password, no prompt (a key, or ssh-agent)."""
    ssh = need()
    where = resolve(machine, ssh)
    _checked(machine, where)
    return [ssh, *_config_args(), "-T", *_port_args(machine), *_key_args(machine), *_pinned_to(where, ssh),
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3", "-o", "ClearAllForwardings=yes", "-o", "PermitLocalCommand=no",
            *_OUR_COMMAND, "--", machine.destination, command]


def public_key_line(pub: Path) -> str:
    """The public key in pub as "type base64 [comment]": only these characters reach the server's shell."""
    try:
        parts = pub.read_text(encoding="utf-8").split()
    except (OSError, UnicodeDecodeError):
        raise SSHError(f"There's no public key at {pub}.", "key_missing") from None
    if len(parts) < 2 or not _KEY_TYPE_RE.fullmatch(parts[0]) or not _KEY_B64_RE.fullmatch(parts[1]):
        raise SSHError(f"{pub} doesn't hold an SSH public key.", "key_missing")
    comment = " ".join(parts[2:])
    return " ".join(parts[:2] + ([comment] if _KEY_COMMENT_RE.fullmatch(comment) else []))


def copy_key_argv(machine: Machine) -> list[str]:
    """ssh that adds the machine's public key (its key file + ".pub") to the server's authorized_keys,
    once. You sign in with your password, in a terminal. Only needs ssh, so it works on Windows too."""
    if not machine.key:
        raise SSHError("Set this machine's key file first (or make a new key).", "key_missing")
    key = public_key_line(Path(machine.key + ".pub").expanduser())
    ssh = need()
    where = resolve(machine, ssh)
    _checked(machine, where)
    remote = (
        "umask 077; mkdir -p .ssh && touch .ssh/authorized_keys && "
        f"if grep -qxF '{key}' .ssh/authorized_keys; then echo 'This key was already on the server.'; "
        # Like ssh-copy-id: end a last line that has no newline first, or the key is glued onto it
        "else { [ -z \"$(tail -c1 .ssh/authorized_keys)\" ] || echo >> .ssh/authorized_keys; } && "
        f"echo '{key}' >> .ssh/authorized_keys && echo 'Key added. Next time, no password.'; fi"
    )
    return [ssh, *_config_args(), *_port_args(machine), *_pinned_to(where, ssh), "-o", "BatchMode=no",
            *_OUR_COMMAND, "--", machine.destination, remote]


@dataclass
class NewKey:
    path: Path
    public: str
    fingerprint: str


def make_key(path: Path | None = None) -> NewKey:
    """A new ed25519 key with no passphrase (so runs on many machines need no prompt), in ~/.ssh."""
    keygen = need("ssh-keygen")
    folder = Path.home() / ".ssh"
    if path is None:
        path = folder / "ixel_ed25519"
        n = 2
        while path.exists() or path.with_name(path.name + ".pub").exists():
            path = folder / f"ixel_ed25519_{n}"
            n += 1
    elif path.exists():
        raise SSHError(f"There's already a key at {path}.", "exists")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    host = re.sub(r"[^A-Za-z0-9._-]", "", socket.gethostname())[:60] or "computer"
    try:
        result = subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-C", f"ixel@{host}", "-f", str(path)],
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=30, env=quiet_env(),
                                **_quiet())
    except (OSError, subprocess.SubprocessError) as exc:
        raise SSHError(f"ssh-keygen didn't make the key: {exc}", "keygen") from None
    if result.returncode != 0 or not path.is_file():
        raise SSHError(f"ssh-keygen didn't make the key: {_last_line(result.stderr.decode('utf-8', 'replace'))}",
                       "keygen")
    if not WINDOWS:
        os.chmod(path, 0o600)
    public = public_key_line(Path(str(path) + ".pub"))
    return NewKey(path, public, fingerprint(public.split()[1]))
