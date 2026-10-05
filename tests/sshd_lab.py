"""
A real OpenSSH server on 127.0.0.1 for the Machines tests: a host key of its own, one client key that signs
in, and whoever runs the tests as its only user. Skipped where there's no sshd to start (Windows, most Macs,
or as root without /run/sshd).
"""
import getpass
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


def keygen(path: Path, kind: str = "ed25519") -> Path:
    subprocess.run([shutil.which("ssh-keygen"), "-q", "-t", kind, "-N", "", "-C", "lab", "-f", str(path)],
                   check=True, stdin=subprocess.DEVNULL)
    return path


def fingerprint_of(pub: Path) -> str:
    out = subprocess.run([shutil.which("ssh-keygen"), "-lf", str(pub)], capture_output=True, text=True, check=True)
    return out.stdout.split()[1]


class Lab:
    def __init__(self, folder: Path, ecdsa: bool = False):
        """ecdsa: an ECDSA host key too (a server with two keys)."""
        self.folder = folder
        self.user = getpass.getuser()
        self.host_key = keygen(folder / "host_ed25519")
        self.ecdsa_key = keygen(folder / "host_ecdsa", "ecdsa") if ecdsa else None
        self.client = keygen(folder / "client")
        self.stranger = keygen(folder / "stranger")  # a key the server doesn't know
        (folder / "authorized_keys").write_text((folder / "client.pub").read_text())
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        (folder / "sshd_config").write_text("\n".join([
            f"Port {self.port}", "ListenAddress 127.0.0.1", f"HostKey {self.host_key}",
            *([f"HostKey {self.ecdsa_key}"] if self.ecdsa_key else []),
            f"PidFile {folder / 'sshd.pid'}", f"AuthorizedKeysFile {folder / 'authorized_keys'}",
            "UsePAM no", "StrictModes no", "PasswordAuthentication no", "KbdInteractiveAuthentication no",
            "PermitRootLogin prohibit-password", "LogLevel ERROR", ""]))
        self.proc = subprocess.Popen([sshd_program(), "-D", "-e", "-f", str(folder / "sshd_config")],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                said = self.proc.stdout.read().decode("utf-8", "replace").strip()
                pytest.skip(f"sshd didn't start: {said}")
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                return
            except OSError:
                time.sleep(0.05)
        self.stop()
        pytest.skip("sshd didn't start listening")

    @property
    def fingerprint(self) -> str:
        return fingerprint_of(self.host_key.with_name("host_ed25519.pub"))

    @property
    def name(self) -> str:
        """What its key is pinned under."""
        return f"[127.0.0.1]:{self.port}"

    def host_keys(self) -> set[tuple[str, str]]:
        kind, b64 = self.host_key.with_name("host_ed25519.pub").read_text().split()[:2]
        return {(kind, b64)}

    def machine(self, **changes) -> dict:
        return {"name": "lab", "host": "127.0.0.1", "port": self.port, "user": self.user,
                "key": str(self.client), **changes}

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def sshd_program() -> str | None:
    return shutil.which("sshd") or next((p for p in ("/usr/sbin/sshd", "/usr/local/sbin/sshd") if os.path.isfile(p)),
                                        None)


def need_sshd() -> None:
    if sys.platform == "win32" or not sshd_program() or not shutil.which("ssh") or not shutil.which("ssh-keygen"):
        pytest.skip("needs OpenSSH's sshd, ssh and ssh-keygen")


@pytest.fixture
def sshd(tmp_path_factory):
    need_sshd()
    lab = Lab(tmp_path_factory.mktemp("sshd"))
    yield lab
    lab.stop()
