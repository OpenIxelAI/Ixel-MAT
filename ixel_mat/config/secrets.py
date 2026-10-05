"""
Where Ixel MAT keeps the API keys you save, and how they reach os.environ and the programs it starts.

Keys are kept in ~/.config/ixel-mat/keys.enc, encrypted (Fernet), and the key that opens that file is
kept in the system's keychain: a Mac's Keychain, Windows Credential Manager, or the Secret Service or
KWallet on Linux. That's one keychain item (service "Ixel", account "keys"), the way Chrome keeps its
saved passwords, so a Mac asks at most once and no key comes near Windows' size limit for an item. On a
computer with no keychain Ixel can use, or one that won't keep that item (before there's a keys.enc),
keys stay in ~/.config/ixel-mat/.env as plain text (0600).

Flow:
  1. `ixel setup` or the app's Settings saves a key (save_secret, set_live)
  2. On startup, load_env() reads them (moving any it finds in .env into keys.enc) into os.environ
  3. Config TOML references keys by env var name (token_env)
  4. User never types `export` again
"""
from __future__ import annotations

import json
import logging
import os
import stat
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

logger = logging.getLogger("ixel_mat.config.secrets")

_ENV_DIR  = Path.home() / ".config" / "ixel-mat"
_ENV_FILE = _ENV_DIR / ".env"
_KEYS_FILE = _ENV_DIR / "keys.enc"

# Names load_env() put into os.environ (not ones the user exported themselves)
_INJECTED: set[str] = set()
# os.environ and _INJECTED change together, and are read together: the app's Settings page saves
# a key from one thread while programs are started from others, and one of those must never see a
# key saved in Ixel that it isn't meant to (Claude Code and Codex would bill it).
_LOCK = threading.RLock()
# The saved keys are changed by one thread at a time, and read under this lock whenever the keychain may
# have to be asked (or os.environ follows what's read). A separate lock, taken before _LOCK and never while
# holding it: the keychain can keep a thread waiting on a password prompt, and starting a program
# (child_env) mustn't wait for that. Reads that needn't ask go without it, so a page never waits for a save.
_STORE_LOCK = threading.RLock()

KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT = "Ixel", "keys"
# A keychain can wait for a person: a Mac asking for your password, a locked Linux keyring asking to be
# unlocked. Long enough to answer, and Ixel never waits forever.
KEYCHAIN_TIMEOUT = 30.0
# Another Ixel saving keys holds keys.enc.lock, at most for one keychain answer plus a moment. One that
# stopped mid-save leaves its lock behind, which is taken over once it's this old.
LOCK_WAIT = KEYCHAIN_TIMEOUT + 10
LOCK_STALE = 120.0
# The keychains that count. keyring's other backends keep nothing (fail, null), or keep the key in a
# plain file of their own (keyrings.alt), next to the file it opens.
_KEYCHAINS = {
    "keyring.backends.macOS.Keyring": "your Mac's Keychain",
    "keyring.backends.Windows.WinVaultKeyring": "Windows Credential Manager",
    "keyring.backends.SecretService.Keyring": "your Linux keyring",
    "keyring.backends.libsecret.Keyring": "your Linux keyring",
    "keyring.backends.kwallet.DBusKeyring": "your Linux keyring",
    "keyring.backends.kwallet.DBusKeyringKWallet4": "your Linux keyring",
}
# The tests set this (tests/conftest.py, before Ixel is imported, so the programs the suite starts get it
# too): a keychain that lives in memory, so no test reads or changes yours. Only under the tests (_under_test)
TEST_KEYCHAIN_VAR = "IXEL_TEST_KEYCHAIN"


def write_private_file(path: Path, data: bytes) -> None:
    """
    Atomically write data to path, readable by the current user only.

    mkstemp creates the file 0600 from the start, so the secret is never
    briefly world-readable (write_text + chmod leaves a umask-sized window),
    and os.replace means a crash can't leave a truncated secrets file.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        with suppress(OSError):
            os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def _replace(source, target: Path) -> None:
    for attempt in range(6):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            # Windows: a virus scanner, an editor or another read has the file open for a moment
            if os.name != "nt" or attempt == 5:
                raise
            time.sleep(0.05 * 2 ** attempt)


def read_text_file(path: Path) -> str:
    """
    Read a config or secrets file: UTF-8 (with or without the BOM Notepad adds),
    else the Windows ANSI code page. Before encodings were explicit, Python on
    Windows wrote these files that way (`ixel setup` did), so they still load.
    """
    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _tighten_permissions(path: Path) -> None:
    """Reset a secrets file to 0600 if group/other can read it."""
    if os.name != "posix":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        os.chmod(path, 0o600)
        logger.warning("%s was mode %o — reset to 600", path, mode)


def normalize_secret_input(value: str) -> str:
    """Normalize pasted secrets by removing line breaks and non-Latin1 artifacts."""
    if not value:
        return ""
    cleaned = value.replace("\r", "").replace("\n", "").strip().strip('"').strip("'")
    return "".join(ch for ch in cleaned if ord(ch) <= 255)


# ── The keychain ──────────────────────────────────────────────────────────────

class KeyStoreError(Exception):
    """A key wasn't saved or removed. The message says why and what to do, in plain words."""


class _Unavailable(Exception):
    """The keychain couldn't be asked: it failed, didn't answer in time, or isn't there any more."""


class _Unreadable(Exception):
    """keys.enc doesn't open with the keychain's key (key: what the keychain holds, None for nothing)."""

    def __init__(self, key: bytes | None):
        super().__init__("keys.enc can't be opened")
        self.key = key


class _Busy(Exception):
    """Another Ixel holds keys.enc.lock."""


class _Refused(Exception):
    """The keychain answered, but wouldn't keep a new key for keys.enc (a company policy that forbids it, say)."""


class _NotNow(Exception):
    """Reading the saved keys would mean asking the keychain, and the caller mustn't wait (load_env(wait=False))."""


class MemoryKeychain:
    """A keychain that lasts as long as the process: the tests' (IXEL_TEST_KEYCHAIN=memory)."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password


def _system_keychain() -> tuple[Any, str]:
    """The keychain keyring would use, when it's one that counts: (backend, what to call it), or (None, "").
    keyring follows PYTHON_KEYRING_BACKEND and its own settings file, so turning it off there turns it off here.
    On Windows, keyring's own setting for the item is kept: it goes with a profile that roams, as keys.enc in
    it does, so the keys open on each computer you sign in to (an item kept on one computer only would leave
    keys.enc unreadable on all the others)."""
    import keyring
    from keyring.backends import chainer
    found = keyring.get_keyring()
    for backend in found.backends if isinstance(found, chainer.ChainerBackend) else [found]:
        label = _KEYCHAINS.get(f"{type(backend).__module__}.{type(backend).__name__}")
        if label and "Linux" in label and not sys.platform.startswith("linux"):
            label = "your keyring"  # the Secret Service on a BSD, say
        if label:
            return backend, label
    return None, ""


def _under_test() -> bool:
    """Whether this is Ixel's test suite, or a program it started (pytest sets PYTEST_CURRENT_TEST for those)."""
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _find_keychain() -> tuple[Any, str]:
    if os.environ.get(TEST_KEYCHAIN_VAR):
        if os.environ[TEST_KEYCHAIN_VAR] == "memory" and _under_test():
            return MemoryKeychain(), "your keychain"
        # Set outside the tests: no keychain at all. Not the one in memory (keys moved into keys.enc would be
        # gone when Ixel exits, with no key left to open them), and not yours (whoever set it meant to keep
        # Ixel away from it)
        logger.warning("%s is set outside Ixel's tests, so this run uses no keychain", TEST_KEYCHAIN_VAR)
        return None, ""
    try:
        return _system_keychain()
    except ImportError:  # keyring isn't installed (Health says so): keys stay in .env, as before it
        return None, ""


# A Mac's keychain that would have to ask for its password and can't show the prompt (over SSH, say):
# keyring calls it a failure to save, but it's locked, and must never send keys to plain text
_MAC_CANT_ASK = -25308  # errSecInteractionNotAllowed


def _is_locked(error: BaseException) -> bool:
    """keyring's word for a keychain that's locked, or whose password prompt was turned down (a Mac's too),
    and a Mac's that can't show its prompt."""
    try:
        from keyring.errors import KeyringLocked
    except ImportError:
        return False
    cause = getattr(error.__cause__, "args", ())
    return isinstance(error, KeyringLocked) or bool(cause) and cause[0] == _MAC_CANT_ASK


def _fernet_key(value: Any) -> bytes | None:
    """The keychain's item as a key for keys.enc, or None when it's missing or isn't one."""
    from cryptography.fernet import Fernet
    if not isinstance(value, str) or not value:
        return None
    try:
        Fernet(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return None
    return value.encode("ascii")


class _Keychain:
    """
    The keychain, as this run of Ixel found it. Every call runs on a thread of its own and is given
    KEYCHAIN_TIMEOUT to answer. One that fails or runs out of time leaves the keychain unavailable for
    the run: reading keys doesn't ask again (a locked keyring would put up its password prompt each
    time), but saving or removing one does, since someone is there to unlock it. Never two calls at
    once: while one that ran out of time is still waiting, the keychain stays unavailable.
    """

    def __init__(self, find: Callable[[], tuple[Any, str]] | None = None):
        self._find = find or _find_keychain
        self._found: tuple[Any, str] | None = None
        self.key: bytes | None = None   # what opens keys.enc, once the keychain gave it
        self.problem = ""               # "timeout" or "error" once a call didn't work, in this run
        self.refused = False            # it wouldn't keep a key for keys.enc, the last time it was asked to
        self.locked = False             # the last call that failed did because it's locked (or unlocking was
                                        # turned down)
        self._waiting: threading.Event | None = None
        # A key it was asked to keep that ran out of time, and may still be kept: set once that call ends
        self.unsettled: threading.Event | None = None

    @property
    def label(self) -> str:
        return self._found[1] if self._found and self._found[1] else "your keychain"

    def _ask(self, retry: bool, call: Callable, *args, ask: bool = True):
        if self._waiting is not None and not self._waiting.is_set():
            raise _Unavailable(self.problem)
        if self.problem and not retry:
            raise _Unavailable(self.problem)
        if not ask:  # the answer isn't known yet, and this caller mustn't wait for it
            raise _NotNow()
        self.problem = ""
        answer: dict[str, Any] = {}
        done = threading.Event()

        def run() -> None:
            try:
                answer["value"] = call(*args)
            except BaseException as exc:  # noqa: BLE001 — handed to the thread that asked
                answer["error"] = exc
            finally:
                done.set()

        threading.Thread(target=run, name="ixel-keychain", daemon=True).start()
        if not done.wait(KEYCHAIN_TIMEOUT):
            self._waiting, self.problem = done, "timeout"
            logger.warning("The keychain didn't answer within %.0f seconds", KEYCHAIN_TIMEOUT)
            raise _Unavailable("timeout")
        if "error" in answer:
            self.problem, self.locked = "error", _is_locked(answer["error"])
            # The kind of error only: nothing a keychain says back is worth the risk of logging
            logger.warning("The keychain couldn't be used (%s)", type(answer["error"]).__name__)
            raise _Unavailable("error") from None
        return answer.get("value")

    def backend(self, retry: bool = False, ask: bool = True) -> Any:
        """The keychain, or None when this computer has none Ixel can use. ask=False: raises _NotNow
        rather than asking (every other method takes it too)."""
        if self._found is None:
            self._found = self._ask(retry, self._find, ask=ask)
        return self._found[0]

    def get(self, retry: bool = False, fresh: bool = False, ask: bool = True) -> bytes | None:
        """The key that opens keys.enc, or None when the keychain has none."""
        if self.key is not None and not fresh:
            return self.key
        backend = self.backend(retry, ask)
        if backend is None:  # keys.enc was made with a keychain Ixel can't find now (or it was turned off)
            raise _Unavailable("none")
        self.key = _fernet_key(self._ask(retry, backend.get_password, KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, ask=ask))
        return self.key

    def make(self) -> bytes:
        """A new key for keys.enc, saved in the keychain. What's used is what the keychain gives back, so
        two Ixels that make one at once both use the one it kept. _Refused when the keychain answers but
        won't keep it."""
        from cryptography.fernet import Fernet
        backend = self.backend(True)
        if backend is None:
            raise _Unavailable("none")
        before = self._waiting
        try:
            self._ask(True, backend.set_password, KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT,
                      Fernet.generate_key().decode("ascii"))
        except _Unavailable:
            if self.problem == "error" and not self.locked:  # (locked: someone can unlock it and try again)
                self.refused = True
                raise _Refused() from None
            if self._waiting is not before:  # this one ran out of time: keys.enc.lock stays until it ends
                self.unsettled = self._waiting
            raise
        key = self.get(True, fresh=True)
        if key is None:  # it said yes, and kept nothing
            self.refused = True
            raise _Refused()
        self.refused = False
        return key


_keychain = _Keychain()


@dataclass(frozen=True)
class KeyStore:
    """Where the keys saved in Ixel are, for Health, ixel doctor, ixel status, ixel setup and Settings."""
    kind: str          # "keychain"; "file" (plain text: no keychain here), "refused" (plain text: the keychain
                       # wouldn't keep the key to keys.enc); "unavailable" or "unreadable"
    keychain: str      # what holds the key to keys.enc ("your Mac's Keychain"…); "" for "file"
    path: Path         # the file they're in: keys.enc, or .env
    problem: str = ""  # what's wrong and what to do; "" when nothing is

    @property
    def plain(self) -> bool:
        """Whether keys saved now go in a plain-text file."""
        return self.kind in ("file", "refused")

    @property
    def why_plain(self) -> str:
        if self.kind == "refused":
            return f"{self.keychain} refused to keep the key that would encrypt them"
        return "this computer has no keychain Ixel can use"

    @property
    def summary(self) -> str:
        if self.plain:
            where = "that only you can read" if os.name == "posix" else "in your user folder"
            again = " Ixel tries again when you next save a key or start it." if self.kind == "refused" else ""
            return f"Keys saved in Ixel are in a plain-text file {where}, because {self.why_plain}.{again}"
        if self.kind == "unavailable" and self.path.name != _KEYS_FILE.name:  # none encrypted yet
            return (f"Keys saved in Ixel are encrypted, with the key that opens them kept in {self.keychain}, once "
                    "Ixel can open it.")
        return f"Keys saved in Ixel are encrypted, and the key that opens them is kept in {self.keychain}."


def where_keys_are() -> KeyStore:
    """Where saved keys are kept, and what's wrong if they can't be used. Asks the keychain only what
    this run hasn't asked yet, and not again once it failed. When this run knows already, it answers
    at once, even while a key is being saved (a page asks then)."""
    try:
        return _where(ask=False)
    except _NotNow:
        with _STORE_LOCK:
            return _where()


def _where(ask: bool = True) -> KeyStore:
    made = _KEYS_FILE.exists()
    try:
        if made:
            _open_keys(ask=ask)
        elif _keychain.backend(ask=ask) is None:
            return KeyStore("file", "", _ENV_FILE)
        elif _keychain.refused:
            return KeyStore("refused", _keychain.label, _ENV_FILE)
        elif _keychain.problem:  # a call failed in this run: the first save would ask again
            raise _Unavailable(_keychain.problem)
    except _Unavailable as exc:
        return KeyStore("unavailable", _keychain.label, _KEYS_FILE if made else _ENV_FILE,
                        _unavailable_problem(str(exc), made))
    except _Unreadable:
        name = _KEYS_FILE.name
        return KeyStore("unreadable", _keychain.label, _KEYS_FILE,
                        f"The keys saved in Ixel can't be opened: the key they were encrypted with is gone from "
                        f"{_keychain.label}, or isn't the same one. Add them again in Settings or with ixel setup. "
                        f"The old file is then kept as {name}.unreadable.")
    except OSError as exc:
        return KeyStore("unreadable", _keychain.label, _KEYS_FILE,
                        f"Ixel can't read {_KEYS_FILE} ({exc.strerror or type(exc).__name__}).")
    return KeyStore("keychain", _keychain.label, _KEYS_FILE)


def _unavailable_problem(why: str, made: bool) -> str:
    label = _keychain.label
    if why == "none":
        return ("The keys saved in Ixel are encrypted with a key kept in a keychain, and Ixel can't find one it "
                "can use now, so they aren't in use. If you turned your keychain off, turn it back on, then "
                "restart Ixel.")
    late = " (it didn't answer in time)" if why == "timeout" else ""
    if made:
        return (f"Ixel couldn't open {label}{late}, so the keys saved in Ixel aren't in use. Unlock it, then "
                "restart Ixel.")
    try:
        waiting = bool(_env_file()[1])
    except OSError:
        waiting = False
    return (f"Ixel couldn't open {label}{late}, so it can't save keys right now. Unlock it, then try again."
            + (f" Until then, the keys in {_ENV_FILE} stay there as plain text." if waiting else ""))


# ── The files ─────────────────────────────────────────────────────────────────

def _env_line(line: str) -> tuple[str, str]:
    """A .env line's name and value; ("", "") for a comment, a blank line or anything else."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return "", ""
    name, _, value = stripped.partition("=")
    return name.strip(), normalize_secret_input(value)


def _env_file() -> tuple[list[str], dict[str, str]]:
    """.env's lines, and the keys in them."""
    if not _ENV_FILE.exists():
        return [], {}
    _tighten_permissions(_ENV_FILE)
    lines = read_text_file(_ENV_FILE).splitlines()
    keys = {}
    for line in lines:
        name, value = _env_line(line)
        if name and value:
            keys[name] = value
    return lines, keys


def _strip_env(lines: list[str], names: set[str] | None = None) -> None:
    """.env without its keys (names: only these), once they're kept in keys.enc. Removed when nothing but
    comments is left."""
    kept = [line for line in lines if not (_env_line(line)[0] and (names is None or _env_line(line)[0] in names))]
    if all(not line.strip() or line.strip().startswith("#") for line in kept):
        with suppress(FileNotFoundError):
            _ENV_FILE.unlink()
    elif kept != lines:
        write_private_file(_ENV_FILE, ("\n".join(kept) + "\n").encode("utf-8"))


def _write_secrets(existing: dict[str, str]) -> None:
    """.env, on a computer with no keychain Ixel can use (or one that won't keep a key for keys.enc)."""
    lines = [
        "# Ixel MAT — Secrets",
        "# This file is auto-generated by `ixel-mat setup`",
        "# Permissions: 600 (owner read/write only)",
        "",
    ]
    for k, v in sorted(existing.items()):
        lines.append(f'{k}="{v}"')

    write_private_file(_ENV_FILE, ("\n".join(lines) + "\n").encode("utf-8"))


def _decrypt(key: bytes, token: bytes) -> dict[str, str]:
    """The keys in a keys.enc. _Unreadable when key doesn't open it, or it isn't what Ixel writes."""
    from cryptography.fernet import Fernet, InvalidToken
    try:
        data = json.loads(Fernet(key).decrypt(token.strip()))
    except (InvalidToken, ValueError):  # ValueError: it opened, but isn't what Ixel writes
        raise _Unreadable(key) from None
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise _Unreadable(key)
    return {k: v for k, v in data.items() if k and v}


def _open_keys(retry: bool = False, ask: bool = True) -> tuple[bytes, dict[str, str]]:
    """keys.enc's keys, and the key that opened them. Raises _Unavailable, or _Unreadable when the keychain
    has no key or a different one."""
    _tighten_permissions(_KEYS_FILE)
    token = _KEYS_FILE.read_bytes()
    remembered = _keychain.key is not None
    key = _keychain.get(retry, ask=ask)
    if key is None:
        raise _Unreadable(None)
    try:
        return key, _decrypt(key, token)
    except _Unreadable:
        if not remembered:
            raise
    # The keychain's key changed since this run read it (it was taken out, and another Ixel made one)
    key = _keychain.get(retry, fresh=True, ask=ask)
    if key is None:
        raise _Unreadable(None)
    return key, _decrypt(key, token)


def _aside() -> Path:
    return _KEYS_FILE.with_name(_KEYS_FILE.name + ".unreadable")


def _asides() -> list[Path]:
    """The keys.enc files set aside, because they couldn't be opened: keys.enc.unreadable, -2, -3…"""
    first = _aside()
    numbered = [p for p in first.parent.glob(first.name + "-*") if p.name.rpartition("-")[2].isdigit()]
    return ([first] if first.exists() else []) + sorted(numbered, key=lambda p: int(p.name.rpartition("-")[2]))


def _free_aside() -> Path:
    n = 2
    while (path := _aside().with_name(f"{_aside().name}-{n}")).exists():
        n += 1
    return path


def _keys_set_aside(key: bytes) -> tuple[dict[str, str], list[Path]]:
    """The keys in files set aside earlier that open with key, and those files. They're this computer's own:
    another computer's keychain wrote keys.enc since (a folder that's synced, or a profile that roams), or
    keys.enc was set aside before the keychain's item came back."""
    keys: dict[str, str] = {}
    opened = []
    for path in _asides():
        try:
            keys.update(_decrypt(key, path.read_bytes()))
        except (_Unreadable, OSError):
            continue
        opened.append(path)
    return keys, opened


def _write_keys(token: bytes, unreadable: bool, opened: list[Path]) -> None:
    """
    token as keys.enc. unreadable: the keys.enc there now can't be opened, and becomes keys.enc.unreadable.
    Files set aside earlier whose keys are in token (opened) go. One that didn't open is never overwritten,
    but kept as keys.enc.unreadable-2 (-3…): another computer's keychain, or yours once its item is back,
    may open it. In an order that loses no file if Ixel stops halfway.
    """
    aside = _aside()
    target = aside
    if unreadable:
        if aside.exists() and aside not in opened:
            _replace(aside, _free_aside())
        if aside.exists():  # one whose keys are in token: replaced only once token is written
            target = _free_aside()
        _replace(_KEYS_FILE, target)
        logger.warning("%s couldn't be opened with the keychain's key; kept as %s", _KEYS_FILE.name, aside.name)
    write_private_file(_KEYS_FILE, token)
    # The keys are saved now: a file Windows won't let go of yet is only left a little longer
    for path in opened:
        with suppress(OSError):
            path.unlink()
    if target != aside:
        with suppress(OSError):
            _replace(target, aside)


@contextmanager
def _file_lock(wait: float):
    """One Ixel at a time changes keys.enc: whichever creates keys.enc.lock (O_EXCL, which Windows has too).
    A lock older than LOCK_STALE was left by an Ixel that stopped mid-save, and is taken over."""
    lock = _KEYS_FILE.with_name(_KEYS_FILE.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    mine = f"{os.getpid()} {os.urandom(8).hex()}".encode("ascii")  # so only this one is ever removed
    deadline = time.monotonic() + wait
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except (FileExistsError, PermissionError) as exc:
            if isinstance(exc, PermissionError) and os.name != "nt":
                raise  # a folder Ixel can't write in (on Windows, also a lock that's being deleted)
            with suppress(OSError):
                if time.time() - lock.stat().st_mtime > LOCK_STALE:
                    lock.unlink()
                    continue
            if time.monotonic() >= deadline:
                raise _Busy() from None
            time.sleep(0.05)
        else:
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(mine)
            except OSError:
                with suppress(OSError):
                    lock.unlink()
                raise
            break
    try:
        yield
    finally:
        pending = _keychain.unsettled
        if pending is not None and not pending.is_set():
            # The keychain may yet keep the key this Ixel asked it to, after it stopped waiting. Until it
            # has, no other Ixel makes one (keys.enc would then open with neither); a lock left this way is
            # still taken over once it's LOCK_STALE old
            threading.Thread(target=lambda: (pending.wait(), _unlock(lock, mine)), name="ixel-keys-lock",
                             daemon=True).start()
        else:
            _unlock(lock, mine)


def _unlock(lock: Path, mine: bytes) -> None:
    with suppress(OSError):
        if lock.read_bytes() == mine:  # not one another Ixel made after taking this one over
            lock.unlink()


def _change(update: Callable[[dict[str, str]], Any], retry: bool, set_aside: bool, wait: float | None = None):
    """
    Read every saved key (keys.enc's, and .env's on top), change them with update(keys), and write them
    to keys.enc, taking the keys out of .env. Returns what update returned.

    keys.enc is never written unless it opened (or there was none), so a keychain that can't be asked
    never costs a key. One that the keychain answered for but can't open (its key is gone or different)
    is set aside as keys.enc.unreadable when set_aside (see _write_keys), else _Unreadable is raised.
    When keys.enc is made anew, the keys in files set aside earlier that open with its key go back in.
    No file changes before the keychain holds a key for keys.enc: _Refused when it won't keep one.
    """
    from cryptography.fernet import Fernet
    _keychain.get(retry)  # before the lock: it may wait for a person to answer a password prompt
    with _file_lock(LOCK_WAIT if wait is None else wait):
        key, keys, unreadable = None, {}, False
        if _KEYS_FILE.exists():
            try:
                key, keys = _open_keys(retry)
            except _Unreadable as exc:
                if not set_aside:
                    raise
                key, unreadable = exc.key, True
        else:
            key = _keychain.get(retry, fresh=True)  # another Ixel may have made it while this one waited
        opened: list[Path] = []
        if key is not None and (unreadable or not _KEYS_FILE.exists()):
            keys, opened = _keys_set_aside(key)
        lines, plain = _env_file()
        keys.update(plain)
        result = update(keys)
        if keys:
            key = key or _keychain.make()
            _write_keys(Fernet(key).encrypt(json.dumps(keys, sort_keys=True).encode("utf-8")), unreadable, opened)
        else:
            for path in ([] if unreadable else [_KEYS_FILE]) + opened:
                with suppress(FileNotFoundError):
                    path.unlink()
        if any(_env_line(line)[0] for line in lines):
            _strip_env(lines)
        return result


@contextmanager
def _saving():
    """What the person who asked to save or remove a key is told when it couldn't be done."""
    try:
        yield
    except _Unavailable:
        raise KeyStoreError(f"Ixel couldn't open {_keychain.label}, so nothing was saved. Unlock it and "
                            "try again.") from None
    except _Refused:
        raise KeyStoreError(f"{_keychain.label} refused to keep the key that opens the keys saved in Ixel, so "
                            "nothing was saved.") from None
    except _Busy:
        raise KeyStoreError("Another Ixel is saving keys right now, so nothing was saved. Try again in a "
                            "moment.") from None


def _uses_keychain(retry: bool) -> bool:
    """Whether saved keys go in keys.enc: it's there already, or this computer has a keychain."""
    return _KEYS_FILE.exists() or _keychain.backend(retry) is not None


def _stored(move: bool = False, ask: bool = True) -> tuple[dict[str, str], bool]:
    """
    Every saved key, and whether that's all of them (not when keys.enc couldn't be opened: its keys
    are still saved, just not readable now). move: keys found in .env go into keys.enc, when the
    keychain can be used (someone may add lines to .env by hand, then they move on the next load).
    ask=False: from what this run already has, raising _NotNow when the keychain would have to be
    asked, and a move is left to _load_later().
    """
    plain = _env_file()[1]
    saved: dict[str, str] = {}
    if _KEYS_FILE.exists():
        try:
            _, saved = _open_keys(ask=ask)
        except (_Unavailable, _Unreadable, OSError):  # left untouched: it's still yours, and nothing moves into it
            return plain, False
    elif not (plain and move):
        return plain, True
    else:
        try:
            if _keychain.backend(ask=ask) is None:
                return plain, True
        except _Unavailable:
            return plain, True
    if plain and move and not ask:
        if not _keychain.problem:  # (else it would fail at once: the next save moves them)
            _load_later()
    elif plain and move:
        try:
            _change(lambda keys: None, retry=False, set_aside=False, wait=1.0)
            logger.info("Moved %d key(s) from %s into %s", len(plain), _ENV_FILE.name, _KEYS_FILE.name)
        except (_Unavailable, _Unreadable, _Busy, _Refused):
            pass  # they stay in .env this time, and are still used
        except OSError as exc:
            logger.warning("Couldn't move the keys in %s (%s)", _ENV_FILE.name, exc.strerror or type(exc).__name__)
    return {**saved, **plain}, True


# A load_env() on a thread of its own is under way (one at a time)
_LATER = threading.Lock()


def _load_later() -> None:
    """load_env() on a thread of its own, for what one that mustn't wait left undone: asking the keychain
    (keys.enc made since this run last asked, by ixel setup in a terminal say), or moving keys out of .env."""
    if not _LATER.acquire(blocking=False):
        return

    def run() -> None:
        try:
            load_env()
        finally:
            _LATER.release()

    threading.Thread(target=run, name="ixel-keys", daemon=True).start()


# ── What the rest of Ixel calls ───────────────────────────────────────────────

def load_env(wait: bool = True) -> dict[str, str]:
    """
    Load the saved keys into os.environ, moving any found in .env into keys.enc first where there's a
    keychain. Returns dict of loaded key names → values.
    Does NOT override already-set env vars (explicit export wins).
    Never raises: mat.py calls it as it's imported.

    wait=False is for the app, where a request mustn't wait for a password prompt or another save: it
    uses only what this run already has (the key the keychain gave it, and the files), and changes
    nothing while a key is being saved. What that leaves undone, a load_env() on a thread of its own
    does (_load_later), and the next request has what it found.
    """
    try:
        if wait:
            _STORE_LOCK.acquire()
        elif not _STORE_LOCK.acquire(blocking=False):  # another thread saves or reads them: as they are, this time
            return _in_use()
        try:
            loaded, complete = _stored(move=True, ask=wait)
            # Still holding _STORE_LOCK, so a key saved meanwhile isn't undone by what was read before it
            with _LOCK:
                for key, value in loaded.items():
                    # A key set outside Ixel (and not empty) wins; one Ixel put there follows what's saved
                    if key in _INJECTED or not os.environ.get(key):
                        _INJECTED.add(key)
                        os.environ[key] = value
                if complete:
                    for key in list(_INJECTED - set(loaded)):  # taken out of what's saved since: stop using it
                        os.environ.pop(key, None)
                        _INJECTED.discard(key)
            return loaded
        finally:
            _STORE_LOCK.release()
    except _NotNow:
        _load_later()
        return _in_use()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read the saved keys (%s)", type(exc).__name__)
        return {}


def _in_use() -> dict[str, str]:
    """The saved keys this process uses now."""
    with _LOCK:
        return {key: os.environ[key] for key in _INJECTED if key in os.environ}


def _saved() -> dict[str, str]:
    """Every saved key, from what this run already has, so a page that asks never waits for the keychain or
    a key being saved. When keys.enc would have to be opened first (made by another Ixel since this one last
    asked, say), the keys in use and .env's for now: a load_env() on a thread of its own opens it, and the
    next ask has them."""
    try:
        return _stored(ask=False)[0]
    except _NotNow:
        _load_later()
        try:
            plain = _env_file()[1]
        except OSError:
            plain = {}
        return {**_in_use(), **plain}


def save_secret(key: str, value: str) -> None:
    """Save or update one key: in keys.enc where there's a keychain, else in .env. Raises KeyStoreError
    when the keychain can't be opened (a key is then never written as plain text). Where the keychain
    refuses to keep a key for keys.enc and there's no keys.enc yet, the key goes in .env, as it would
    with no keychain, and moves on a later run once the keychain keeps one."""
    value = normalize_secret_input(value)
    with _STORE_LOCK, _saving():
        if _uses_keychain(retry=True):
            try:
                _change(lambda keys: keys.__setitem__(key, value), retry=True, set_aside=True)
                return
            except _Refused:
                if _KEYS_FILE.exists():
                    raise
                logger.warning("%s refused to keep a key for %s, so keys stay in %s", _keychain.label,
                               _KEYS_FILE.name, _ENV_FILE.name)
        existing = _env_file()[1]
        existing[key] = value
        _write_secrets(existing)


def remove_secret(key: str) -> bool:
    """Take one saved key out; False if it wasn't there. Raises KeyStoreError when the keychain can't be
    opened."""
    with _STORE_LOCK, _saving():
        lines, plain = _env_file()
        if _uses_keychain(retry=True):
            if not _KEYS_FILE.exists() and key not in plain:
                return False
            try:
                return _change(lambda keys: keys.pop(key, None) is not None, retry=True, set_aside=False)
            except _Unreadable:  # keys.enc stays as it is (it's no use, but it's yours): only .env can change
                if key not in plain:
                    return False
                _strip_env(lines, {key})
                return True
            except _Refused:  # the rest of .env would have moved: it stays, as in save_secret
                if _KEYS_FILE.exists():
                    raise
        if key not in plain:
            return False
        del plain[key]
        _write_secrets(plain)
        return True


def _set_outside(key: str) -> bool:
    return bool(os.environ.get(key)) and key not in _INJECTED


def key_state(key: str) -> str:
    """Where a key comes from: "system" (set outside Ixel, and that one wins), "file" (saved in Ixel),
    or "none"."""
    with _LOCK:
        if _set_outside(key):
            return "system"
    return "file" if _saved().get(key) else "none"


def ixels_own(key: str) -> str:
    """A key's value when it's the one saved in Ixel and in use, else "" (unset, or set outside Ixel)."""
    with _LOCK:
        return os.environ.get(key, "") if key in _INJECTED else ""


def saved_names() -> set[str]:
    """The keys saved in Ixel (never their values)."""
    return {k for k, v in _saved().items() if v}


def set_live(key: str, value: str) -> str:
    """Save a key and use it from now on, in this process too, still withheld from the programs Ixel starts
    (child_env). A key set outside Ixel wins, as at startup: "system" says so; else "saved". Raises
    KeyStoreError when it couldn't be saved."""
    with _STORE_LOCK:
        save_secret(key, value)
        with _LOCK:
            if _set_outside(key):
                return "system"
            _INJECTED.add(key)  # first, so no program started meanwhile is given it
            os.environ[key] = normalize_secret_input(value)
            return "saved"


def remove_live(key: str) -> bool:
    """Take a saved key out, and out of this process if it came from there. Raises KeyStoreError when it
    couldn't be removed."""
    with _STORE_LOCK:
        removed = remove_secret(key)
        with _LOCK:
            if key in _INJECTED:
                os.environ.pop(key, None)
                _INJECTED.discard(key)
        return removed


def child_env(pass_env: list[str] | None = None, set_env: dict[str, str] | None = None,
              drop_env: list[str] | None = None, nested: bool = True) -> dict[str, str]:
    """
    Environment for programs ixel launches (CLI agents).

    Keys saved in Ixel are withheld unless listed in pass_env:
    a third-party CLI has no business seeing every provider key, and some
    (Claude Code, Codex) would silently bill an API key instead of the user's
    subscription login if one were present. drop_env removes more names (the
    same keys when they come from the user's shell), except any pass_env lists; set_env adds fixed
    settings, such as a CLI's locked-down config. nested=False is for a program the
    person asked for that isn't a panel member (Handoff, from the app's Board).
    """
    allowed = set(pass_env or ())
    dropped = set(drop_env or ()) - allowed  # pass_env names a key on purpose: it goes
    with _LOCK:
        current, injected = dict(os.environ), set(_INJECTED)
    env = {k: v for k, v in current.items()
           if (k not in injected or k in allowed) and k not in dropped}
    env.update(set_env or {})
    # Marks everything ixel launches, so an ixel started *by* a panel member
    # (e.g. Claude Code with the ixel plugin) refuses to start another panel.
    if nested:
        env[PANEL_DEPTH_VAR] = str(panel_depth() + 1)
    return env


@contextmanager
def keys_withheld() -> Iterator[None]:
    """For starting a program that can't be given an environment of its own (the browser webbrowser.open
    starts): the keys Ixel saved are out of os.environ meanwhile, then back. Only while nothing else is
    using them, as the app opens its page."""
    with _LOCK:
        taken = {key: os.environ.pop(key) for key in list(_INJECTED) if key in os.environ}
        try:
            yield
        finally:
            os.environ.update(taken)


PANEL_DEPTH_VAR = "IXEL_PANEL_DEPTH"


def panel_depth() -> int:
    try:
        return max(int(os.environ.get(PANEL_DEPTH_VAR, "0")), 0)
    except ValueError:
        return 1  # set but garbled: assume we're inside a panel


def get_env_file_path() -> Path:
    """.env: where keys are kept on a computer with no keychain Ixel can use (plain text, 0600)."""
    return _ENV_FILE


def get_keys_file_path() -> Path:
    """keys.enc: where keys are kept, encrypted, where there's a keychain."""
    return _KEYS_FILE


def secrets_exist() -> bool:
    return _KEYS_FILE.exists() or (_ENV_FILE.exists() and _ENV_FILE.stat().st_size > 50)
