import os
import tempfile
import threading
from pathlib import Path

# Before anything of Ixel's is imported, since mat.py loads the saved keys as it's imported: a keychain
# in memory instead of yours (secrets.py honours this, and the programs the suite starts inherit it),
# and a folder that's never there instead of yours, until each test gets its own (below).
os.environ["IXEL_TEST_KEYCHAIN"] = "memory"

from ixel_mat.config import secrets  # noqa: E402  (first, so nothing reads your keys before this)

_NOWHERE = Path(tempfile.gettempdir()) / f"ixel-tests-{os.getpid()}-no-keys-here"
secrets._ENV_DIR, secrets._ENV_FILE, secrets._KEYS_FILE = _NOWHERE, _NOWHERE / ".env", _NOWHERE / "keys.enc"

import pytest  # noqa: E402

from ixel_mat import conversation, forget, stats, update  # noqa: E402
from ixel_mat.agents import leftovers, websocket  # noqa: E402
from ixel_mat.config import loader  # noqa: E402
from ixel_mat.config import setup as wizard  # noqa: E402
from ixel_mat.gui import appearance  # noqa: E402
from ixel_mat.machines import log as machines_log, ssh as machines_ssh, store as machines_store  # noqa: E402


@pytest.fixture(autouse=True)
def _no_update_checks(monkeypatch):
    # Tests that start the terminal app must not ask GitHub for updates when the suite runs
    # from an installed copy (tests/test_update.py turns the check back on where it's tested).
    monkeypatch.setenv("IXEL_NO_UPDATE_CHECK", "1")


# Every file Ixel keeps in ~/.config/ixel-mat, and the installer's record next to its virtualenv
# (install.lock is found beside install.json), as (module, attribute, name in a test's folder; "" for the folder)
USER_FILES = [
    (stats, "STATS_FILE", "stats.json"),
    (conversation, "CONVERSATION_FILE", "conversation.json"),
    (update, "CHECK_FILE", "update_check.json"),
    (update, "INSTALL_INFO", "install.json"),
    (loader, "_GLOBAL_CONFIG", "config.toml"),
    (wizard, "_CONFIG_DIR", ""),
    (wizard, "_CONFIG_FILE", "config.toml"),
    (secrets, "_ENV_DIR", ""),
    (secrets, "_ENV_FILE", ".env"),
    (secrets, "_KEYS_FILE", "keys.enc"),
    (websocket, "_KEY_DIR", ""),
    (websocket, "_KEY_FILE", "device_key"),
    (machines_store, "MACHINES_FILE", "machines.json"),
    (machines_ssh, "PINS_FILE", "machines_known_hosts"),
    (machines_log, "LOG_FILE", "machines.log"),
    (appearance, "APP_FILE", "app.json"),
]


@pytest.fixture(autouse=True)
def _no_real_user_files(tmp_path, tmp_path_factory, monkeypatch):
    # Reviews run in-process count toward stats.json, the setup wizard writes config.toml, `ixel update`
    # deletes its last check: in a test, all of that happens in the test's own folder, never in the files
    # of whoever runs the suite. A test that points one somewhere itself still can (its monkeypatch is later).
    folder = tmp_path / "ixel-user-files"
    for module, name, file in USER_FILES:
        monkeypatch.setattr(module, name, folder / file)
    # Which keys Ixel put into the environment itself: each test starts from what the suite started with,
    # and the ones a test loaded are gone after it (they'd make the next test's keys look set)
    before = set(secrets._INJECTED)
    monkeypatch.setattr(secrets, "_INJECTED", set(before))
    # Machines reads ~/.ssh/config and Ixel Console's files, and ssh reads its config: none of yours (in a
    # folder of their own, so a test listing its tmp_path doesn't see them)
    ssh_dir = tmp_path_factory.mktemp("ssh-home")
    (ssh_dir / "config").write_text("")
    monkeypatch.setattr(machines_store, "SSH_CONFIG", ssh_dir / "config")
    monkeypatch.setattr(machines_ssh, "CONFIG_FILE", ssh_dir / "config")
    monkeypatch.setattr(machines_store, "CONSOLE_PROFILES", tmp_path / "ixel-console" / "profiles.json")
    monkeypatch.setattr(machines_ssh, "CONSOLE_PINS", tmp_path / "ixel-console" / "ssh_known_hosts")
    # `ixel forget` deletes the app window's browser storage: a folder in the test's own, not your window's
    # (a test of where those folders are imports window_folders itself, before this runs)
    monkeypatch.setattr(forget, "window_folders", lambda: [tmp_path / "ixel-window" / "window"])
    yield
    # A load the app left to a thread of its own (secrets._load_later) finishes before the next test's files
    if secrets._LATER.acquire(timeout=10):
        secrets._LATER.release()
    for name in secrets._INJECTED - before:
        os.environ.pop(name, None)


class FakeKeychain(secrets.MemoryKeychain):
    """The keychain every test has, in memory. A test can make it fail (error), refuse to keep anything
    while still giving what it has (refuse: set_password raises it), or wait like a password prompt nobody
    answers (hold: every call waits for that event)."""

    def __init__(self) -> None:
        super().__init__()
        self.error: Exception | None = None
        self.refuse: Exception | None = None
        self.hold: threading.Event | None = None
        self.calls = 0

    def _call(self) -> None:
        self.calls += 1
        if self.hold is not None:
            self.hold.wait()
        if self.error is not None:
            raise self.error

    def get_password(self, service, username):
        self._call()
        return super().get_password(service, username)

    def set_password(self, service, username, password):
        self._call()
        if self.refuse is not None:
            raise self.refuse
        super().set_password(service, username, password)

    def restart(self, label: str = "your Mac's Keychain", present: bool = True) -> None:
        """As the next run of Ixel finds it: nothing remembered. present=False: this computer has no keychain."""
        secrets._keychain = secrets._Keychain(lambda: (self if present else None, label if present else ""))


@pytest.fixture(autouse=True)
def keychain(monkeypatch):
    """No test ever reads or changes your keychain: each gets a fresh one in memory (a Mac's, by its name)."""
    fake = FakeKeychain()
    monkeypatch.setattr(secrets, "_keychain", secrets._Keychain(lambda: (fake, "your Mac's Keychain")))
    return fake


@pytest.fixture
def gemini_home(tmp_path, monkeypatch):
    """Gemini CLI's settings (yours and the system's) in the test's own folder, with no sign-in set up, and
    none of the variables that choose one or hold a key."""
    home = tmp_path / "gemini-home"
    (home / ".gemini").mkdir(parents=True)
    monkeypatch.setenv("GEMINI_CLI_HOME", str(home))
    monkeypatch.setenv("GEMINI_CLI_SYSTEM_SETTINGS_PATH", str(tmp_path / "gemini-system" / "settings.json"))
    for name in ("GEMINI_CLI_SYSTEM_DEFAULTS_PATH", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_GCA",
                 "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GEMINI_BASE_URL", "CLOUD_SHELL", "GEMINI_CLI_USE_COMPUTE_ADC",
                 "GEMINI_CLI_TRUST_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)
    return home


# The environment the suite started with, before any test changes it
SUITE_ENV = dict(os.environ)
# What tells Ixel where Gemini CLI, Copilot and OpenCode keep their sessions (agents/leftovers.py)
CLI_PLACES = ("HOME", "USERPROFILE", "GEMINI_CLI_HOME", "COPILOT_HOME", "XDG_DATA_HOME", "OPENCODE_DB")


@pytest.fixture(autouse=True)
def _no_real_cli_sessions(tmp_path_factory, monkeypatch):
    # After a run in its temp folder, Ixel removes what those CLIs kept of it, in the folders their environment
    # names. A test that runs a stand-in CLI with the suite's environment would have it look in yours: whatever
    # of yours the environment still names is swapped for a folder of the test's own. A home a test gives the
    # CLI itself (the live checks, the stand-ins in test_leftovers.py) is kept.
    real = leftovers.places
    stand_in = []

    def places(env):
        yours = {name for name in CLI_PLACES if env.get(name) and env.get(name) == SUITE_ENV.get(name)}
        if not (env.get("HOME") or env.get("USERPROFILE")):
            yours |= {"HOME", "USERPROFILE"}  # it would be your home folder
        if not yours:
            return real(env)
        if not stand_in:
            stand_in.append(str(tmp_path_factory.mktemp("cli-home")))
        env = {name: value for name, value in env.items() if name not in yours}
        if yours & {"HOME", "USERPROFILE"}:
            env.update(HOME=stand_in[0], USERPROFILE=stand_in[0])
        return real(env)
    monkeypatch.setattr(leftovers, "places", places)
