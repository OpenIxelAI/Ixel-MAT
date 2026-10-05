"""
Secret storage for Ixel MAT.

Stores API keys in ~/.config/ixel-mat/.env (chmod 600).
Loads automatically on startup — no manual export needed.

Flow:
  1. `ixel-mat setup` → interactive wizard, saves to .env
  2. On startup, loader reads .env and injects into os.environ
  3. Config TOML references keys by env var name (token_env)
  4. User never types `export` again
"""
from __future__ import annotations

import logging
import os
import stat
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path

logger = logging.getLogger("ixel_mat.config.secrets")

_ENV_DIR  = Path.home() / ".config" / "ixel-mat"
_ENV_FILE = _ENV_DIR / ".env"

# Names load_env() put into os.environ (not ones the user exported themselves)
_INJECTED: set[str] = set()
# os.environ and _INJECTED change together, and are read together: the app's Settings page saves
# a key from one thread while programs are started from others, and one of those must never see a
# key from Ixel's .env that it isn't meant to (Claude Code and Codex would bill it).
_LOCK = threading.RLock()


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
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows: a virus scanner, an editor or another read has the file open for a moment
                if os.name != "nt" or attempt == 5:
                    raise
                time.sleep(0.05 * 2 ** attempt)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


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


def load_env() -> dict[str, str]:
    """
    Load secrets from .env file into os.environ.
    Returns dict of loaded key names → values.
    Does NOT override already-set env vars (explicit export wins).
    """
    loaded: dict[str, str] = {}
    with _LOCK:
        try:
            if _ENV_FILE.exists():
                _tighten_permissions(_ENV_FILE)
                text = read_text_file(_ENV_FILE)
            else:
                text = ""
        except Exception as exc:
            logger.warning("Failed to read secrets from %s: %s", _ENV_FILE, exc)
            return loaded
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = normalize_secret_input(value)
            if key and value:
                # A key set outside Ixel (and not empty) wins; one Ixel put there follows the file
                if key in _INJECTED or not os.environ.get(key):
                    _INJECTED.add(key)
                    os.environ[key] = value
                loaded[key] = value
        for key in list(_INJECTED - set(loaded)):  # taken out of the file since: stop using it
            os.environ.pop(key, None)
            _INJECTED.discard(key)
    return loaded


def _saved() -> dict[str, str]:
    existing: dict[str, str] = {}
    if _ENV_FILE.exists():
        for line in read_text_file(_ENV_FILE).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" in stripped:
                k, _, v = stripped.partition("=")
                existing[k.strip()] = normalize_secret_input(v)
    return existing


def save_secret(key: str, value: str) -> None:
    """Save or update a single secret in the .env file."""
    existing = _saved()
    existing[key] = normalize_secret_input(value)
    _write_secrets(existing)


def remove_secret(key: str) -> bool:
    """Take one secret out of the .env file; False if it wasn't there."""
    existing = _saved()
    if key not in existing:
        return False
    del existing[key]
    _write_secrets(existing)
    return True


def _set_outside(key: str) -> bool:
    return bool(os.environ.get(key)) and key not in _INJECTED


def key_state(key: str) -> str:
    """Where a key comes from: "system" (set outside Ixel, and that one wins), "file" (Ixel's .env),
    or "none"."""
    with _LOCK:
        if _set_outside(key):
            return "system"
        return "file" if _saved().get(key) else "none"


def ixels_own(key: str) -> str:
    """A key's value when it's the one saved in Ixel's .env and in use, else "" (unset, or set outside Ixel)."""
    with _LOCK:
        return os.environ.get(key, "") if key in _INJECTED else ""


def saved_names() -> set[str]:
    """The keys in Ixel's .env (never their values)."""
    with _LOCK:
        return {k for k, v in _saved().items() if v}


def set_live(key: str, value: str) -> str:
    """Save a key and use it from now on, in this process too, still withheld from the programs Ixel starts
    (child_env). A key set outside Ixel wins, as at startup: "system" says so; else "saved"."""
    with _LOCK:
        save_secret(key, value)
        if _set_outside(key):
            return "system"
        _INJECTED.add(key)  # first, so no program started meanwhile is given it
        os.environ[key] = normalize_secret_input(value)
        return "saved"


def remove_live(key: str) -> bool:
    """Take a key out of Ixel's .env, and out of this process if it came from there."""
    with _LOCK:
        removed = remove_secret(key)
        if key in _INJECTED:
            os.environ.pop(key, None)
            _INJECTED.discard(key)
        return removed


def _write_secrets(existing: dict[str, str]) -> None:
    lines = [
        "# Ixel MAT — Secrets",
        "# This file is auto-generated by `ixel-mat setup`",
        "# Permissions: 600 (owner read/write only)",
        "",
    ]
    for k, v in sorted(existing.items()):
        lines.append(f'{k}="{v}"')

    write_private_file(_ENV_FILE, ("\n".join(lines) + "\n").encode("utf-8"))


def child_env(pass_env: list[str] | None = None, set_env: dict[str, str] | None = None,
              drop_env: list[str] | None = None, nested: bool = True) -> dict[str, str]:
    """
    Environment for programs ixel launches (CLI agents).

    Keys ixel loaded from its own .env are withheld unless listed in pass_env:
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


PANEL_DEPTH_VAR = "IXEL_PANEL_DEPTH"


def panel_depth() -> int:
    try:
        return max(int(os.environ.get(PANEL_DEPTH_VAR, "0")), 0)
    except ValueError:
        return 1  # set but garbled: assume we're inside a panel


def get_env_file_path() -> Path:
    return _ENV_FILE


def secrets_exist() -> bool:
    return _ENV_FILE.exists() and _ENV_FILE.stat().st_size > 50
