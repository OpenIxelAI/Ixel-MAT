"""Scratch probes for the review (not part of the repo). Every path Ixel keeps is pointed into tmp_path."""
import os
import threading

import pytest

from ixel_mat.config import secrets, setup as wizard


class FakeKeychain(secrets.MemoryKeychain):
    def __init__(self):
        super().__init__()
        self.error = None
        self.refuse = None
        self.hold = None
        self.calls = 0

    def _call(self):
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


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    folder = tmp_path / "ixel-mat"
    monkeypatch.setattr(secrets, "_ENV_DIR", folder)
    monkeypatch.setattr(secrets, "_ENV_FILE", folder / ".env")
    monkeypatch.setattr(secrets, "_KEYS_FILE", folder / "keys.enc")
    monkeypatch.setattr(wizard, "_CONFIG_DIR", folder)
    monkeypatch.setattr(wizard, "_CONFIG_FILE", folder / "config.toml")
    before = set(secrets._INJECTED)
    monkeypatch.setattr(secrets, "_INJECTED", set())
    fake = FakeKeychain()
    monkeypatch.setattr(secrets, "_keychain", secrets._Keychain(lambda: (fake, "your Mac's Keychain")))
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY", "GOOGLE_API_KEY", "ZZ_TEST_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield fake
    for name in secrets._INJECTED - before:
        os.environ.pop(name, None)


def opencode_cfg():
    from ixel_mat.config.loader import build_agent_configs
    configs, _ = build_agent_configs({"agents": {"opencode": {"preset": "opencode"}}})
    return configs["opencode"]


def test_wizard_saved_key_reaches_the_opencode_list_run_but_not_after_a_restart(isolated):
    """_save_key puts the key in os.environ without marking it as Ixel's, so child_env doesn't withhold it."""
    from ixel_mat.agents.oneshot import cli_env
    wizard._save_key("OPENAI_API_KEY", "sk-typed-in-the-wizard")
    env_now = cli_env(opencode_cfg())
    # ...whereas the same key, loaded on the next run, is withheld
    secrets._INJECTED.clear()
    os.environ.pop("OPENAI_API_KEY", None)
    secrets.load_env()
    assert os.environ.get("OPENAI_API_KEY") == "sk-typed-in-the-wizard"
    env_next_run = cli_env(opencode_cfg())
    print("\nin the wizard run, opencode models sees OPENAI_API_KEY:", "OPENAI_API_KEY" in env_now)
    print("after a restart, opencode sees OPENAI_API_KEY:", "OPENAI_API_KEY" in env_next_run)
    assert "OPENAI_API_KEY" in env_now and "OPENAI_API_KEY" not in env_next_run


def test_save_secret_raises_a_bare_oserror_when_keys_enc_cannot_be_read(isolated):
    """keys.enc that can't be read (root-owned after `sudo ixel setup`, say): save_secret doesn't turn the
    OSError into KeyStoreError, so callers that only catch KeyStoreError crash."""
    secrets._KEYS_FILE.mkdir(parents=True)  # stands in for a file read_bytes() can't open (an OSError either way)
    with pytest.raises(OSError) as info:
        secrets.save_secret("ZZ_TEST_KEY", "value")
    print("\nsave_secret raised:", type(info.value).__name__, "- a KeyStoreError?", isinstance(info.value, secrets.KeyStoreError))
    assert not isinstance(info.value, secrets.KeyStoreError)


def test_a_refusing_keychain_rewrites_env_and_drops_your_own_lines(isolated):
    """save_secret falling back to .env rewrites it from a dict: comments and blank-valued lines you wrote go."""
    from keyring.errors import PasswordSetError
    isolated.refuse = PasswordSetError("policy")
    secrets._ENV_DIR.mkdir(parents=True)
    secrets._ENV_FILE.write_text("# my note: this key is the work account\nOPENAI_API_KEY=sk-old\nMY_OTHER_TOOL_VAR=keepme\n")
    secrets.save_secret("ZZ_TEST_KEY", "value")
    text = secrets._ENV_FILE.read_text()
    print("\n.env after save:\n" + text)
    assert "my note" not in text
    assert 'MY_OTHER_TOOL_VAR="keepme"' in text  # other keys survive, as quoted values


def test_env_wins_over_keys_enc_for_the_same_name_on_load(isolated):
    """A line added to .env by hand replaces the value saved in keys.enc for that name (documented; just checking
    nothing else is lost)."""
    secrets.save_secret("ZZ_TEST_KEY", "from-keychain")
    secrets.save_secret("OPENAI_API_KEY", "enc-openai")
    secrets._ENV_FILE.write_text("ZZ_TEST_KEY=from-env\n")
    loaded = secrets.load_env()
    assert loaded == {"ZZ_TEST_KEY": "from-env", "OPENAI_API_KEY": "enc-openai"}
    assert not secrets._ENV_FILE.exists()


def test_keychain_timeout_on_first_save_leaves_no_plain_text_and_says_so(isolated):
    isolated.hold = threading.Event()
    secrets.KEYCHAIN_TIMEOUT = 0.3
    secrets.LOCK_WAIT = 0.5
    try:
        with pytest.raises(secrets.KeyStoreError) as info:
            secrets.save_secret("ZZ_TEST_KEY", "value")
        print("\n", info.value)
        assert not secrets._ENV_FILE.exists() and not secrets._KEYS_FILE.exists()
    finally:
        isolated.hold.set()
        secrets.KEYCHAIN_TIMEOUT = 30.0
        secrets.LOCK_WAIT = 40.0


def test_set_agent_model_backup_and_edit_backup_land_on_the_same_name(tmp_path):
    from ixel_mat.config import edit, loader
    path = tmp_path / "config.toml"
    path.write_text('[agents.gpt]\ntype = "http"\nurl = "https://api.openai.com/v1/chat/completions"\nmodel = "latest"\n')
    loader.set_agent_model(path, "gpt", "latest-fast")
    a = sorted(p.name for p in tmp_path.iterdir())
    edit.edit_file(path, ("review",), {"mode": "deep"})
    b = sorted(p.name for p in tmp_path.iterdir())
    print("\nafter set_agent_model:", a, "\nafter edit_file:", b)
    assert a == b == ["config.toml", "config.toml.bak"]
