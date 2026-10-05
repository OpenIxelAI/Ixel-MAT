import pytest

from ixel_mat import conversation, stats, update
from ixel_mat.agents import websocket
from ixel_mat.config import loader, secrets
from ixel_mat.config import setup as wizard
from ixel_mat.gui import appearance
from ixel_mat.machines import log as machines_log, ssh as machines_ssh, store as machines_store


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
    # Which keys Ixel put into the environment itself: each test starts from what the suite started with
    from ixel_mat.config import secrets
    monkeypatch.setattr(secrets, "_INJECTED", set(secrets._INJECTED))
    # Machines reads ~/.ssh/config and Ixel Console's files, and ssh reads its config: none of yours (in a
    # folder of their own, so a test listing its tmp_path doesn't see them)
    ssh_dir = tmp_path_factory.mktemp("ssh-home")
    (ssh_dir / "config").write_text("")
    monkeypatch.setattr(machines_store, "SSH_CONFIG", ssh_dir / "config")
    monkeypatch.setattr(machines_ssh, "CONFIG_FILE", ssh_dir / "config")
    monkeypatch.setattr(machines_store, "CONSOLE_PROFILES", tmp_path / "ixel-console" / "profiles.json")
    monkeypatch.setattr(machines_ssh, "CONSOLE_PINS", tmp_path / "ixel-console" / "ssh_known_hosts")


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
