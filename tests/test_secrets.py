"""Saved keys: encrypted in keys.enc with a key kept in the system's keychain, or in .env where there's none.

Every test has a keychain of its own, in memory (tests/conftest.py): none of them reads or changes yours.
"""
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from ixel_mat.config import secrets


def env_file():
    return secrets.get_env_file_path()


def keys_file():
    return secrets.get_keys_file_path()


def write_env(text):
    env_file().parent.mkdir(parents=True, exist_ok=True)
    env_file().write_text(text, encoding="utf-8")


def wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "it didn't happen in time"
        time.sleep(0.01)


@pytest.fixture(autouse=True)
def _no_keys_from_the_shell(monkeypatch):
    for name in ("API_KEY", "OTHER", "SOME_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_load_env_reads_dotenv_and_populates_environment(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("# comment\nAPI_KEY='abc123'\nOTHER=value\n")
    monkeypatch.setattr(secrets, "_ENV_FILE", env_path)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("OTHER", raising=False)

    loaded = secrets.load_env()

    assert loaded == {"API_KEY": "abc123", "OTHER": "value"}
    assert secrets.os.environ["API_KEY"] == "abc123"
    assert secrets.os.environ["OTHER"] == "value"


def test_load_env_does_not_override_existing_environment_variable(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("API_KEY=from_file\n")
    monkeypatch.setattr(secrets, "_ENV_FILE", env_path)
    monkeypatch.setenv("API_KEY", "from_env")

    loaded = secrets.load_env()

    assert loaded == {"API_KEY": "from_file"}
    assert secrets.os.environ["API_KEY"] == "from_env"


# ── Moving keys out of .env ───────────────────────────────────────────────────

def test_keys_in_env_move_into_the_encrypted_file_and_env_goes(keychain):
    write_env('# Ixel MAT — Secrets\n# Permissions: 600\n\nOPENAI_API_KEY="sk-one"\nXAI_API_KEY=\'xai-two\'\n')
    assert secrets.load_env() == {"OPENAI_API_KEY": "sk-one", "XAI_API_KEY": "xai-two"}
    assert os.environ["OPENAI_API_KEY"] == "sk-one"
    assert not env_file().exists()  # nothing but comments was left
    data = keys_file().read_bytes()
    assert b"sk-one" not in data and b"xai-two" not in data and b"OPENAI" not in data
    assert list(keychain.items) == [("Ixel", "keys")]  # one item: the key that opens the file
    assert json.loads(Fernet(keychain.items[("Ixel", "keys")].encode()).decrypt(data)) == {
        "OPENAI_API_KEY": "sk-one", "XAI_API_KEY": "xai-two"}
    if os.name == "posix":
        assert stat.S_IMODE(keys_file().stat().st_mode) == 0o600
    assert sorted(p.name for p in keys_file().parent.iterdir()) == ["keys.enc"]  # no lock or temp file left

    keychain.restart()  # the next run of Ixel reads them back
    assert secrets.load_env() == {"OPENAI_API_KEY": "sk-one", "XAI_API_KEY": "xai-two"}


def test_a_line_added_to_env_by_hand_moves_on_the_next_load_and_wins(keychain):
    secrets.save_secret("OPENAI_API_KEY", "sk-old")
    write_env("# mine\nOPENAI_API_KEY=sk-new\nnot a key line\n")
    assert secrets.load_env() == {"OPENAI_API_KEY": "sk-new"}
    assert env_file().read_text(encoding="utf-8") == "# mine\nnot a key line\n"  # what isn't a key stays
    keychain.restart()
    assert secrets.load_env() == {"OPENAI_API_KEY": "sk-new"}


def test_nothing_touches_the_keychain_when_there_are_no_keys(keychain):
    assert secrets.load_env() == {} and secrets.saved_names() == set()
    assert keychain.calls == 0 and not keychain.items and not keys_file().exists()


# ── Saving and removing ───────────────────────────────────────────────────────

def test_save_and_remove_round_trip(keychain):
    secrets.save_secret("OPENAI_API_KEY", ' "sk-one"\n')
    secrets.save_secret("XAI_API_KEY", "xai-two")
    assert secrets.saved_names() == {"OPENAI_API_KEY", "XAI_API_KEY"}
    assert not env_file().exists()
    keychain.restart()
    assert secrets.load_env() == {"OPENAI_API_KEY": "sk-one", "XAI_API_KEY": "xai-two"}
    assert secrets.key_state("XAI_API_KEY") == "file"
    assert secrets.remove_secret("XAI_API_KEY") and not secrets.remove_secret("XAI_API_KEY")
    assert secrets.load_env() == {"OPENAI_API_KEY": "sk-one"} and "XAI_API_KEY" not in os.environ
    assert secrets.remove_secret("OPENAI_API_KEY")
    assert not keys_file().exists() and secrets.key_state("OPENAI_API_KEY") == "none"  # no keys, no file
    assert not secrets.remove_secret("ANTHROPIC_API_KEY")


def test_set_live_and_remove_live_change_this_process_too():
    assert secrets.set_live("OPENAI_API_KEY", "sk-live") == "saved"
    assert os.environ["OPENAI_API_KEY"] == "sk-live" and secrets.ixels_own("OPENAI_API_KEY") == "sk-live"
    assert "OPENAI_API_KEY" not in secrets.child_env()  # the programs Ixel starts don't get it
    assert secrets.child_env(["OPENAI_API_KEY"])["OPENAI_API_KEY"] == "sk-live"  # unless pass_env names it
    assert secrets.remove_live("OPENAI_API_KEY") and "OPENAI_API_KEY" not in os.environ


def test_a_key_set_in_your_shell_still_wins(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "from-the-shell")
    secrets.save_secret("OPENAI_API_KEY", "saved-in-ixel")
    assert secrets.load_env() == {"OPENAI_API_KEY": "saved-in-ixel"}
    assert os.environ["OPENAI_API_KEY"] == "from-the-shell"
    assert secrets.key_state("OPENAI_API_KEY") == "system" and secrets.ixels_own("OPENAI_API_KEY") == ""
    assert secrets.set_live("OPENAI_API_KEY", "another") == "system"
    assert secrets.child_env()["OPENAI_API_KEY"] == "from-the-shell"  # yours, not Ixel's: it isn't withheld
    monkeypatch.delenv("OPENAI_API_KEY")
    secrets.load_env()
    assert secrets.ixels_own("OPENAI_API_KEY") == "another" and "OPENAI_API_KEY" not in secrets.child_env()


# ── No keychain ───────────────────────────────────────────────────────────────

def test_without_a_keychain_keys_stay_in_a_plain_text_file(keychain):
    keychain.restart(present=False)
    write_env("OPENAI_API_KEY=sk-one\n")
    assert secrets.load_env() == {"OPENAI_API_KEY": "sk-one"}
    assert env_file().read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-one\n"  # not moved: there's nowhere to
    secrets.save_secret("XAI_API_KEY", "xai-two")
    text = env_file().read_text(encoding="utf-8")
    assert 'OPENAI_API_KEY="sk-one"' in text and 'XAI_API_KEY="xai-two"' in text and not keys_file().exists()
    store = secrets.where_keys_are()
    assert store.kind == "file" and store.path == env_file() and store.keychain == "" and not store.problem
    assert "plain-text file" in store.summary
    assert store.summary.endswith("because this computer has no keychain Ixel can use.")
    assert secrets.remove_secret("XAI_API_KEY") and not secrets.remove_secret("XAI_API_KEY")


def test_only_real_keychains_count(monkeypatch):
    import keyring
    from keyring.backends import SecretService, Windows, chainer, fail, kwallet, libsecret, macOS, null
    monkeypatch.setattr(sys, "platform", "linux")
    plaintext = type("PlaintextKeyring", (), {"__module__": "keyrings.alt.file"})()  # keyrings.alt's
    for found, label in [(null.Keyring(), ""), (fail.Keyring(), ""), (plaintext, ""),
                         (macOS.Keyring(), "your Mac's Keychain"),
                         (Windows.WinVaultKeyring(), "Windows Credential Manager"),
                         (SecretService.Keyring(), "your Linux keyring"), (libsecret.Keyring(), "your Linux keyring"),
                         (kwallet.DBusKeyring(), "your Linux keyring")]:
        monkeypatch.setattr(keyring, "get_keyring", lambda found=found: found)
        backend, said = secrets._system_keychain()
        assert said == label and (backend is found if label else backend is None), found
    # keyring's chainer: the first real keychain in it, else none
    monkeypatch.setattr(keyring, "get_keyring", lambda: chainer.ChainerBackend())
    real = SecretService.Keyring()
    monkeypatch.setattr(chainer.ChainerBackend, "backends", [plaintext, real])
    assert secrets._system_keychain() == (real, "your Linux keyring")
    monkeypatch.setattr(chainer.ChainerBackend, "backends", [plaintext, null.Keyring()])
    assert secrets._system_keychain() == (None, "")


def test_on_windows_the_item_goes_with_a_profile_that_roams(monkeypatch):
    """keyring's own setting is kept: the item roams as keys.enc does, so the keys open on each computer you
    sign in to (one kept on this computer only would leave keys.enc unreadable on the others)."""
    import keyring
    monkeypatch.setattr(sys, "platform", "win32")
    vault = type("WinVaultKeyring", (), {"__module__": "keyring.backends.Windows", "persist": "enterprise"})()
    monkeypatch.setattr(keyring, "get_keyring", lambda: vault)
    assert secrets._system_keychain() == (vault, "Windows Credential Manager")
    assert "persist" not in vars(vault)


def test_turning_keyring_off_turns_it_off_here(monkeypatch):
    import keyring.core
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    monkeypatch.setattr(keyring.core, "_keyring_backend", None)  # found again, as in a new run
    assert secrets._system_keychain() == (None, "")


def test_without_keyring_installed_keys_stay_in_env(monkeypatch):
    monkeypatch.delenv(secrets.TEST_KEYCHAIN_VAR)
    monkeypatch.setitem(sys.modules, "keyring", None)  # import keyring fails
    assert secrets._find_keychain() == (None, "")


def test_the_suites_keychain_is_in_memory():
    assert os.environ[secrets.TEST_KEYCHAIN_VAR] == "memory"  # the programs the suite starts get it too
    backend, _ = secrets._find_keychain()
    assert isinstance(backend, secrets.MemoryKeychain)


def test_the_tests_keychain_is_never_used_outside_them(tmp_path):
    """IXEL_TEST_KEYCHAIN left set in a shell: Ixel then uses no keychain at all, so the keys in .env stay
    there (a keychain in memory would take them into keys.enc and forget its key when Ixel exits)."""
    folder = tmp_path / ".config" / "ixel-mat"
    folder.mkdir(parents=True)
    (folder / ".env").write_text("OPENAI_API_KEY=sk-one\n", encoding="utf-8")
    root = str(Path(secrets.__file__).resolve().parents[2])
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    env.update({"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "PYTHONPATH": root,
                secrets.TEST_KEYCHAIN_VAR: "memory"})
    script = ("from ixel_mat.config import secrets; print(sorted(secrets.load_env())); "
              "print(secrets.where_keys_are().kind)")
    out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["['OPENAI_API_KEY']", "file"]
    assert sorted(p.name for p in folder.iterdir()) == [".env"]
    assert (folder / ".env").read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-one\n"


@pytest.mark.parametrize("label", ["your Mac's Keychain", "Windows Credential Manager", "your Linux keyring"])
def test_where_keys_are_names_the_keychain(keychain, label):
    keychain.restart(label=label)
    store = secrets.where_keys_are()
    assert store.kind == "keychain" and store.keychain == label and store.path == keys_file()
    assert store.summary == f"Keys saved in Ixel are encrypted, and the key that opens them is kept in {label}."


# ── A keychain that can't be opened ───────────────────────────────────────────

def test_a_keychain_that_fails_is_never_traded_for_plain_text(keychain):
    secrets.save_secret("OPENAI_API_KEY", "sk-one")
    before = keys_file().read_bytes()
    keychain.restart()
    keychain.error = RuntimeError("the keyring is locked")
    write_env("XAI_API_KEY=xai-two\n")  # added by hand: used, and left where it is
    assert secrets.load_env() == {"XAI_API_KEY": "xai-two"}
    calls = keychain.calls
    assert secrets.load_env() == {"XAI_API_KEY": "xai-two"} and keychain.calls == calls  # loading doesn't ask again
    with pytest.raises(secrets.KeyStoreError) as refused:
        secrets.save_secret("ANTHROPIC_API_KEY", "sk-ant")
    assert str(refused.value) == ("Ixel couldn't open your Mac's Keychain, so nothing was saved. Unlock it and "
                                  "try again.")
    assert keychain.calls > calls  # saving does ask again: someone is there to unlock it
    with pytest.raises(secrets.KeyStoreError):
        secrets.remove_secret("OPENAI_API_KEY")
    assert keys_file().read_bytes() == before and env_file().read_text(encoding="utf-8") == "XAI_API_KEY=xai-two\n"
    store = secrets.where_keys_are()
    assert store.kind == "unavailable" and store.path == keys_file()
    assert store.problem == ("Ixel couldn't open your Mac's Keychain, so the keys saved in Ixel aren't in use. "
                             "Unlock it, then restart Ixel.")
    keychain.error = None  # unlocked
    secrets.save_secret("ANTHROPIC_API_KEY", "sk-ant")
    assert secrets.load_env() == {"OPENAI_API_KEY": "sk-one", "XAI_API_KEY": "xai-two", "ANTHROPIC_API_KEY": "sk-ant"}
    assert not env_file().exists()


def test_before_the_first_save_a_failing_keychain_still_keeps_keys_out_of_plain_text(keychain):
    write_env("OPENAI_API_KEY=sk-one\n")
    keychain.error = RuntimeError("denied")
    assert secrets.load_env() == {"OPENAI_API_KEY": "sk-one"}  # still used
    with pytest.raises(secrets.KeyStoreError):
        secrets.save_secret("XAI_API_KEY", "xai-two")
    assert env_file().read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-one\n" and not keys_file().exists()
    store = secrets.where_keys_are()
    assert store.kind == "unavailable"
    assert store.problem == ("Ixel couldn't open your Mac's Keychain, so it can't save keys right now. Unlock it, "
                             f"then try again. Until then, the keys in {env_file()} stay there as plain text.")
    assert store.summary == ("Keys saved in Ixel are encrypted, with the key that opens them kept in your Mac's "
                             "Keychain, once Ixel can open it.")  # none are yet


def test_a_keychain_that_refuses_to_keep_a_key_leaves_keys_in_plain_text(keychain):
    """A keychain that opens but won't keep anything (a company policy against saved passwords, say):
    keys are saved as they were before there was a keychain, and move once it keeps one."""
    keychain.refuse = RuntimeError("not allowed")
    secrets.save_secret("OPENAI_API_KEY", "sk-one")
    assert not keys_file().exists() and 'OPENAI_API_KEY="sk-one"' in env_file().read_text(encoding="utf-8")
    store = secrets.where_keys_are()
    assert store.kind == "refused" and store.plain and store.path == env_file() and not store.problem
    who = "that only you can read" if os.name == "posix" else "in your user folder"
    assert store.summary == (f"Keys saved in Ixel are in a plain-text file {who}, because your Mac's Keychain "
                             "refused to keep the key that would encrypt them. Ixel tries again when you next "
                             "save a key or start it.")
    calls = keychain.calls
    secrets.save_secret("XAI_API_KEY", "xai-two")  # asked again, and refused again
    assert keychain.calls > calls and secrets.saved_names() == {"OPENAI_API_KEY", "XAI_API_KEY"}
    assert secrets.remove_secret("XAI_API_KEY") and not secrets.remove_secret("XAI_API_KEY")
    keychain.restart()  # the next run: still refused, and the key is still used
    assert secrets.load_env() == {"OPENAI_API_KEY": "sk-one"} and secrets.where_keys_are().kind == "refused"
    keychain.refuse = None
    keychain.restart()  # a run where it keeps one: the keys move
    assert secrets.load_env() == {"OPENAI_API_KEY": "sk-one"}
    assert keys_file().exists() and not env_file().exists() and secrets.where_keys_are().kind == "keychain"


def test_a_keychain_locked_when_asked_to_keep_a_key_is_not_a_refusal(keychain):
    """A Mac whose password prompt was turned down, or a Linux keyring that stayed locked: someone can unlock
    it, so the key isn't written in plain text."""
    from keyring.errors import KeyringLocked
    keychain.refuse = KeyringLocked("Can't store password on keychain")
    with pytest.raises(secrets.KeyStoreError) as refused:
        secrets.save_secret("OPENAI_API_KEY", "sk-one")
    assert str(refused.value) == ("Ixel couldn't open your Mac's Keychain, so nothing was saved. Unlock it and "
                                  "try again.")
    assert not env_file().exists() and not keys_file().exists()
    assert secrets.where_keys_are().kind == "unavailable"
    keychain.refuse = None  # unlocked
    secrets.save_secret("OPENAI_API_KEY", "sk-one")
    assert keys_file().exists() and secrets.where_keys_are().kind == "keychain"


def test_a_mac_keychain_that_cant_show_its_prompt_is_locked_not_a_refusal(keychain):
    """Over SSH, a Mac's keychain can't ask for its password: keyring calls that a failure to save, with the
    Security framework's errSecInteractionNotAllowed behind it."""
    from keyring.errors import PasswordSetError

    def refused(status, chained=True):
        try:
            raise Exception(status, "User interaction is not allowed.")  # keyring's macOS api.Error
        except Exception as cause:
            try:
                if chained:  # keyring 25: raise ... from e
                    raise PasswordSetError("Can't store password on keychain") from cause
                raise PasswordSetError("Can't store password on keychain")  # keyring 24: only the context
            except PasswordSetError as error:
                return error

    for chained in (True, False):
        keychain.refuse = refused(-25308, chained)
        with pytest.raises(secrets.KeyStoreError) as failed:
            secrets.save_secret("OPENAI_API_KEY", "sk-one")
        assert "Unlock it and try again" in str(failed.value)
        assert not env_file().exists() and not keys_file().exists()
        keychain.restart()
    keychain.refuse = refused(-61)  # any other failure to save is a refusal: plain text, as before
    keychain.restart()
    secrets.save_secret("OPENAI_API_KEY", "sk-one")
    assert env_file().exists() and secrets.where_keys_are().kind == "refused"


def test_a_refusal_never_means_plain_text_beside_keys_enc(keychain):
    secrets.save_secret("OPENAI_API_KEY", "sk-one")
    old = keys_file().read_bytes()
    keychain.items.clear()  # the key that opens it is gone, and no new one is kept
    keychain.refuse = RuntimeError("not allowed")
    keychain.restart()
    with pytest.raises(secrets.KeyStoreError) as refused:
        secrets.save_secret("XAI_API_KEY", "xai-two")
    assert str(refused.value) == ("your Mac's Keychain refused to keep the key that opens the keys saved in Ixel, "
                                  "so nothing was saved.")
    assert keys_file().read_bytes() == old and not env_file().exists()
    assert sorted(p.name for p in keys_file().parent.iterdir()) == ["keys.enc"]  # nothing set aside either


def test_a_key_the_keychain_keeps_after_ixel_stopped_waiting_holds_the_lock(keychain, monkeypatch):
    """A save that ran out of time while the keychain asked for a password: until that call ends, no other
    Ixel makes a key of its own (this one's could replace it, leaving keys.enc that opens with neither)."""
    monkeypatch.setattr(secrets, "KEYCHAIN_TIMEOUT", 0.2)
    monkeypatch.setattr(secrets, "LOCK_WAIT", 0.2)
    answered = threading.Event()
    keep = keychain.set_password

    def prompt(service, username, password):
        answered.wait()
        keep(service, username, password)

    monkeypatch.setattr(keychain, "set_password", prompt)
    with pytest.raises(secrets.KeyStoreError, match="didn't|couldn't open"):
        secrets.save_secret("OPENAI_API_KEY", "sk-one")
    lock = keys_file().with_name("keys.enc.lock")
    assert lock.exists() and not keys_file().exists() and not env_file().exists()
    with pytest.raises(secrets._Busy):  # another Ixel waits
        with secrets._file_lock(0.2):
            pass
    answered.set()
    wait_for(lambda: not lock.exists())
    secrets.save_secret("OPENAI_API_KEY", "sk-one")
    keychain.restart()
    assert secrets.load_env() == {"OPENAI_API_KEY": "sk-one"}


def test_a_lock_taken_over_meanwhile_is_left_to_the_ixel_that_took_it(keychain, monkeypatch):
    monkeypatch.setattr(secrets, "KEYCHAIN_TIMEOUT", 0.2)
    answered = threading.Event()
    monkeypatch.setattr(keychain, "set_password", lambda *args: answered.wait())
    with pytest.raises(secrets.KeyStoreError):
        secrets.save_secret("OPENAI_API_KEY", "sk-one")
    lock = keys_file().with_name("keys.enc.lock")
    lock.write_bytes(b"another Ixel's")  # it took this one over once it was stale
    answered.set()
    for thread in threading.enumerate():
        if thread.name == "ixel-keys-lock":
            thread.join(5)
    assert lock.read_bytes() == b"another Ixel's"


def test_a_keychain_that_doesnt_answer_is_given_up_on(keychain, monkeypatch):
    secrets.save_secret("OPENAI_API_KEY", "sk-one")
    before = keys_file().read_bytes()
    keychain.restart()
    monkeypatch.setattr(secrets, "KEYCHAIN_TIMEOUT", 0.2)
    keychain.hold = threading.Event()  # a password prompt nobody answers
    started = time.monotonic()
    assert secrets.load_env() == {}
    assert time.monotonic() - started < 3
    calls = keychain.calls
    with pytest.raises(secrets.KeyStoreError, match="couldn't open your Mac's Keychain"):
        secrets.save_secret("XAI_API_KEY", "xai-two")  # at once: the call that ran out of time is still waiting
    assert keychain.calls == calls and keys_file().read_bytes() == before and not env_file().exists()
    store = secrets.where_keys_are()
    assert store.kind == "unavailable" and "(it didn't answer in time)" in store.problem
    keychain.hold.set()  # answered, late
    wait_for(lambda: secrets._keychain._waiting.is_set())
    secrets.save_secret("XAI_API_KEY", "xai-two")
    assert secrets.load_env() == {"OPENAI_API_KEY": "sk-one", "XAI_API_KEY": "xai-two"}


def test_a_keychain_search_that_hangs_isnt_taken_for_no_keychain(monkeypatch):
    monkeypatch.setattr(secrets, "KEYCHAIN_TIMEOUT", 0.2)
    stuck = threading.Event()
    monkeypatch.setattr(secrets, "_keychain", secrets._Keychain(lambda: (stuck.wait(), None)))
    write_env("OPENAI_API_KEY=sk-one\n")
    assert secrets.load_env() == {"OPENAI_API_KEY": "sk-one"}
    with pytest.raises(secrets.KeyStoreError, match="couldn't open your keychain"):
        secrets.save_secret("XAI_API_KEY", "xai-two")
    assert env_file().read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-one\n"
    assert secrets.where_keys_are().kind == "unavailable"
    stuck.set()


def test_keys_enc_with_no_keychain_now_isnt_overwritten(keychain):
    secrets.save_secret("OPENAI_API_KEY", "sk-one")
    before = keys_file().read_bytes()
    keychain.restart(present=False)  # turned off, say with PYTHON_KEYRING_BACKEND
    assert secrets.load_env() == {}
    with pytest.raises(secrets.KeyStoreError):
        secrets.save_secret("XAI_API_KEY", "xai-two")
    assert keys_file().read_bytes() == before and not env_file().exists()
    store = secrets.where_keys_are()
    assert store.kind == "unavailable" and "If you turned your keychain off, turn it back on" in store.problem


def test_loading_never_raises(monkeypatch):
    write_env("OPENAI_API_KEY=sk-one\n")

    def broken():
        raise OSError("disk on fire")

    monkeypatch.setattr(secrets, "_env_file", broken)
    assert secrets.load_env() == {}


# ── keys.enc that can't be opened ─────────────────────────────────────────────

@pytest.mark.parametrize("gone", [True, False])
def test_keys_that_cant_be_opened_are_left_alone_until_a_key_is_saved(keychain, gone):
    secrets.save_secret("OPENAI_API_KEY", "sk-old")
    old = keys_file().read_bytes()
    other = Fernet.generate_key().decode()
    if gone:  # the keychain's item was deleted
        keychain.items.clear()
    else:  # a different key: the file came from another computer, say
        keychain.items[("Ixel", "keys")] = other
    keychain.restart()
    write_env('XAI_API_KEY="xai-one"\n')
    assert secrets.load_env() == {"XAI_API_KEY": "xai-one"}
    assert keys_file().read_bytes() == old and env_file().exists()  # nothing moved into it
    store = secrets.where_keys_are()
    assert store.kind == "unreadable"
    assert store.problem == ("The keys saved in Ixel can't be opened: the key they were encrypted with is gone from "
                             "your Mac's Keychain, or isn't the same one. Add them again in Settings or with ixel "
                             "setup. The old file is then kept as keys.enc.unreadable.")
    assert not secrets.remove_secret("OPENAI_API_KEY") and keys_file().read_bytes() == old

    aside = keys_file().with_name("keys.enc.unreadable")
    aside.write_bytes(b"an older one")
    secrets.save_secret("ANTHROPIC_API_KEY", "sk-ant")
    assert aside.read_bytes() == old  # kept, and the older one too: neither is overwritten
    assert keys_file().with_name("keys.enc.unreadable-2").read_bytes() == b"an older one"
    keychain.restart()
    assert secrets.load_env() == {"ANTHROPIC_API_KEY": "sk-ant", "XAI_API_KEY": "xai-one"}
    assert not env_file().exists() and secrets.where_keys_are().kind == "keychain"
    if not gone:  # the keychain's key was kept, not replaced
        assert keychain.items[("Ixel", "keys")] == other


def test_keys_from_two_computers_sharing_a_folder_are_never_lost(keychain):
    """A folder that roams or is synced between two computers, each with a keychain of its own: keys.enc
    written on one doesn't open on the other, and what each saved comes back when it saves again."""
    here, there = keychain, type(keychain)()
    secrets.save_secret("OPENAI_API_KEY", "sk-here")
    secrets.save_secret("XAI_API_KEY", "xai-here")
    there.restart(label="your Mac's Keychain")
    assert secrets.load_env() == {} and secrets.where_keys_are().kind == "unreadable"
    secrets.save_secret("ANTHROPIC_API_KEY", "sk-ant-there")
    assert secrets.load_env() == {"ANTHROPIC_API_KEY": "sk-ant-there"}
    here.restart()
    assert secrets.load_env() == {}
    secrets.save_secret("OPENAI_API_KEY", "sk-here-2")
    assert secrets.load_env() == {"OPENAI_API_KEY": "sk-here-2", "XAI_API_KEY": "xai-here"}
    folder = sorted(p.name for p in keys_file().parent.iterdir())
    assert folder == ["keys.enc", "keys.enc.unreadable"]  # the other computer's, kept (and only that)
    there.restart()
    secrets.save_secret("OPENAI_API_KEY", "sk-there")
    assert secrets.load_env() == {"ANTHROPIC_API_KEY": "sk-ant-there", "OPENAI_API_KEY": "sk-there"}
    here.restart()
    secrets.save_secret("SOME_KEY", "some")
    assert secrets.load_env() == {"OPENAI_API_KEY": "sk-here-2", "XAI_API_KEY": "xai-here", "SOME_KEY": "some"}


def test_files_that_cant_be_opened_are_kept_and_come_back_with_their_key(keychain):
    secrets.save_secret("OPENAI_API_KEY", "sk-one")
    first, first_key = keys_file().read_bytes(), keychain.items[("Ixel", "keys")]
    keychain.items.clear()  # the item is deleted
    keychain.restart()
    secrets.save_secret("XAI_API_KEY", "xai-two")
    second = keys_file().read_bytes()
    keychain.items.clear()  # and again
    keychain.restart()
    secrets.save_secret("ANTHROPIC_API_KEY", "sk-ant")
    aside = keys_file().with_name("keys.enc.unreadable")
    assert aside.read_bytes() == second and aside.with_name("keys.enc.unreadable-2").read_bytes() == first
    assert secrets.load_env() == {"ANTHROPIC_API_KEY": "sk-ant"}
    third = keys_file().read_bytes()
    keychain.items[("Ixel", "keys")] = first_key  # the first item is back (from a backup, say)
    keychain.restart()
    secrets.save_secret("SOME_KEY", "some")
    assert secrets.load_env() == {"OPENAI_API_KEY": "sk-one", "SOME_KEY": "some"}  # the first file's keys are back
    left = {p.name: p.read_bytes() for p in keys_file().parent.iterdir() if p.name != "keys.enc"}
    assert left == {"keys.enc.unreadable": third, "keys.enc.unreadable-3": second}  # and that file is gone


def test_removing_a_key_from_env_works_beside_keys_that_cant_be_opened(keychain):
    secrets.save_secret("OPENAI_API_KEY", "sk-old")
    keychain.items.clear()
    keychain.restart()
    write_env("XAI_API_KEY=xai-one\nANTHROPIC_API_KEY=sk-ant\n")
    old = keys_file().read_bytes()
    assert secrets.remove_secret("XAI_API_KEY")
    assert env_file().read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=sk-ant\n" and keys_file().read_bytes() == old


def test_a_key_changed_by_another_ixel_is_read_again(keychain):
    secrets.save_secret("OPENAI_API_KEY", "sk-one")  # this run remembers the key
    theirs = Fernet.generate_key()
    keychain.items[("Ixel", "keys")] = theirs.decode()
    keys_file().write_bytes(Fernet(theirs).encrypt(json.dumps({"XAI_API_KEY": "xai-two"}).encode()))
    assert secrets.load_env() == {"XAI_API_KEY": "xai-two"}


# ── Two Ixels at once ─────────────────────────────────────────────────────────

def test_a_new_key_is_the_one_the_keychain_kept(keychain, monkeypatch):
    theirs = Fernet.generate_key().decode()
    keep = keychain.set_password

    def raced(service, username, password):  # another Ixel saved its own key just after this one
        keep(service, username, password)
        keychain.items[(service, username)] = theirs

    monkeypatch.setattr(keychain, "set_password", raced)
    secrets.save_secret("OPENAI_API_KEY", "sk-one")
    assert json.loads(Fernet(theirs.encode()).decrypt(keys_file().read_bytes())) == {"OPENAI_API_KEY": "sk-one"}


def test_a_save_waits_for_another_ixel_and_takes_over_a_lock_left_behind(monkeypatch):
    monkeypatch.setattr(secrets, "LOCK_WAIT", 0.3)
    secrets.save_secret("OPENAI_API_KEY", "sk-one")
    lock = keys_file().with_name("keys.enc.lock")
    lock.write_bytes(b"")  # another Ixel is saving
    with pytest.raises(secrets.KeyStoreError) as refused:
        secrets.save_secret("XAI_API_KEY", "xai-two")
    assert str(refused.value) == "Another Ixel is saving keys right now, so nothing was saved. Try again in a moment."
    write_env("ANTHROPIC_API_KEY=sk-ant\n")
    assert secrets.load_env()["ANTHROPIC_API_KEY"] == "sk-ant" and env_file().exists()  # moved next time
    old = time.time() - secrets.LOCK_STALE - 5
    os.utime(lock, (old, old))  # it stopped mid-save, long ago
    secrets.save_secret("XAI_API_KEY", "xai-two")
    assert not lock.exists() and not env_file().exists()
    assert secrets.saved_names() == {"OPENAI_API_KEY", "XAI_API_KEY", "ANTHROPIC_API_KEY"}


WRITER = r'''
import json, os, sys, time
from pathlib import Path
from ixel_mat.config import secrets

folder, start, prefix = Path(sys.argv[1]), float(sys.argv[2]), sys.argv[3]


class FileKeychain:
    """The keychain both Ixels share: one JSON file."""
    path = folder / "keychain.json"

    def get_password(self, service, username):
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get(f"{service}/{username}")
        except FileNotFoundError:
            return None

    def set_password(self, service, username, password):
        tmp = self.path.with_name(f"keychain.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({f"{service}/{username}": password}), encoding="utf-8")
        secrets._replace(tmp, self.path)


secrets._ENV_DIR, secrets._ENV_FILE, secrets._KEYS_FILE = folder, folder / ".env", folder / "keys.enc"
secrets._keychain = secrets._Keychain(lambda: (FileKeychain(), "your keychain"))
while time.time() < start:
    time.sleep(0.005)
for i in range(15):
    secrets.save_secret(f"{prefix}_{i}_KEY", f"{prefix}-{i}")
'''


def test_two_ixels_saving_at_once_lose_nothing(tmp_path):
    folder = tmp_path / "ixel-mat"
    folder.mkdir()
    script = tmp_path / "writer.py"
    script.write_text(WRITER, encoding="utf-8")
    # This copy of Ixel, whichever one the environment has installed
    root = str(Path(secrets.__file__).resolve().parents[2])
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "PYTHONPATH": root}
    start = time.time() + 1.0
    writers = [subprocess.Popen([sys.executable, str(script), str(folder), str(start), prefix], env=env,
                                stderr=subprocess.PIPE) for prefix in ("A", "B")]
    for writer in writers:
        assert writer.wait(timeout=60) == 0, writer.stderr.read().decode()
        writer.stderr.close()
    key = json.loads((folder / "keychain.json").read_text(encoding="utf-8"))["Ixel/keys"]
    saved = json.loads(Fernet(key.encode()).decrypt((folder / "keys.enc").read_bytes()))
    assert saved == {f"{p}_{i}_KEY": f"{p}-{i}" for p in "AB" for i in range(15)}
    assert sorted(p.name for p in folder.iterdir()) == ["keychain.json", "keys.enc"]


# ── Owner-only access on Windows ──────────────────────────────────────────────

SID = "S-1-5-21-1-2-3-1001"
WHO = ("desktop-abc\\me", SID)  # what whoami says: the account, in lower case, and its SID
WHOAMI = f'"{WHO[0]}","{SID}"\r\n'
DONE = "Successfully processed 1 files; Failed processing 0 files\r\n"
# How a program goes wrong: it exits with an error, isn't there, or doesn't answer
FAILURES = [
    pytest.param((5, "Access is denied.\r\n"), id="exits-with-an-error"),
    pytest.param(OSError(2, "The system cannot find the file specified"), id="isnt-there"),
    pytest.param(subprocess.TimeoutExpired(["icacls"], 10), id="doesnt-answer"),
]


def is_tool(argv, name):
    return argv[0].lower().endswith(name)


class Tools:
    """whoami and icacls as secrets runs them (_run_tool): each answers with (exit code, what it printed), or
    raises what it's given when that's an exception. Every call is kept, and whether the path icacls was given
    was there at the time."""

    def __init__(self, whoami=(0, WHOAMI), icacls=(0, DONE)):
        self.whoami, self.icacls = whoami, icacls
        self.calls, self.present = [], []

    def __call__(self, argv):
        self.calls.append(list(argv))
        answer = self.whoami
        if not is_tool(argv, "whoami.exe"):
            self.present.append(Path(argv[1]).exists())
            answer = self.icacls
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def grants(self):
        return [argv for argv in self.calls if is_tool(argv, "icacls.exe")]


def on_windows(monkeypatch, **answers):
    """This run as Windows sees it: sys.platform says so, nobody has asked whoami yet, and whoami and icacls are
    Tools (answers: whoami=, icacls=). No real program runs."""
    tools = Tools(**answers)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(secrets, "_USER", None)
    monkeypatch.setattr(secrets, "_run_tool", tools)
    return tools


def icacls_text(path, *entries, summary=DONE):
    """`icacls <path>`'s output: the path and the first entry, the others indented under it, a blank line, and
    a summary in the person's language."""
    pad = " " * (len(str(path)) + 1)
    lines = [f"{path} {entries[0]}", *(pad + entry for entry in entries[1:])]
    return "\r\n".join(lines) + "\r\n\r\n" + summary + "\r\n"


def owner_only_acl(monkeypatch):
    """whoami and icacls answer at once, and say that only you can open anything (on Windows Health and every
    save run them): for the tests that time the keychain, and nothing else. Nothing is run or changed."""
    def tool(argv):
        if is_tool(argv, "whoami.exe"):
            return 0, WHOAMI
        if len(argv) == 2:  # icacls <path>: who can open it
            return 0, icacls_text(argv[1], f"{WHO[0]}:(F)")
        return 0, DONE
    monkeypatch.setattr(secrets, "_USER", None)
    monkeypatch.setattr(secrets, "_run_tool", tool)


def test_whoami_gives_the_account_and_its_sid():
    assert secrets.parse_whoami(WHOAMI) == WHO
    assert secrets.parse_whoami(WHOAMI.strip()) == WHO


def test_a_name_with_a_comma_or_quotes_still_parses():
    assert secrets.parse_whoami(f'"desk,top\\me ""the one""","{SID}"\r\n') == ('desk,top\\me "the one"', SID)


@pytest.mark.parametrize("text", [
    "",
    "\r\n",
    "ERROR: Access is denied.\r\n",
    f'"{SID}"\r\n',
    f'"desktop-abc\\me","{SID}","extra"\r\n',
    '"desktop-abc\\me","not-a-sid"\r\n',
])
def test_whoami_output_that_isnt_an_account_and_a_sid_gives_none(text):
    assert secrets.parse_whoami(text) is None


def test_the_icacls_call_for_a_file_is_an_argv_list_with_the_path_as_one_item(monkeypatch, tmp_path):
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Win"))
    path = tmp_path / "Some One" / "keys.enc"
    argv = secrets.owner_only_argv(path, SID)
    assert isinstance(argv, list) and len(argv) == 5
    assert Path(argv[0]) == tmp_path / "Win" / "System32" / "icacls.exe"
    assert argv[1] == str(path)
    assert argv[2:] == ["/inheritance:r", "/grant:r", f"*{SID}:F"]


def test_the_icacls_call_for_a_folder_grants_what_is_made_in_it_too(tmp_path):
    argv = secrets.owner_only_argv(tmp_path, SID, folder=True)
    assert argv[1:] == [str(tmp_path), "/inheritance:r", "/grant:r", f"*{SID}:(OI)(CI)F"]


def test_the_programs_are_found_in_windows_own_folder_even_without_systemroot(monkeypatch, tmp_path):
    monkeypatch.delenv("SystemRoot", raising=False)
    assert secrets.owner_only_argv(tmp_path, SID)[0].startswith("C:\\Windows")
    assert secrets.owner_only_argv(tmp_path, SID)[0].endswith("icacls.exe")


def test_a_file_is_limited_to_the_users_account_on_windows(monkeypatch, tmp_path):
    tools = on_windows(monkeypatch)
    path = tmp_path / "keys.enc"
    assert secrets.secure_owner_only(path) is True
    assert [Path(argv[0]).name for argv in tools.calls] == ["whoami.exe", "icacls.exe"]
    assert tools.calls[0][1:] == ["/user", "/fo", "csv", "/nh"]
    assert tools.calls[1][1:] == [str(path), "/inheritance:r", "/grant:r", f"*{SID}:F"]


def test_a_folder_is_limited_with_inheritance_for_what_is_made_in_it(monkeypatch, tmp_path):
    tools = on_windows(monkeypatch)
    assert secrets.secure_owner_only(tmp_path, folder=True) is True
    assert tools.calls[-1][1:] == [str(tmp_path), "/inheritance:r", "/grant:r", f"*{SID}:(OI)(CI)F"]


def test_whoami_is_asked_once_and_its_answer_kept(monkeypatch, tmp_path):
    tools = on_windows(monkeypatch)
    assert secrets.secure_owner_only(tmp_path / "keys.enc") and secrets.secure_owner_only(tmp_path, folder=True)
    assert secrets.user_sid() == WHO
    assert len([argv for argv in tools.calls if is_tool(argv, "whoami.exe")]) == 1
    assert len(tools.grants()) == 2


def test_a_whoami_that_failed_is_asked_again(monkeypatch):
    tools = on_windows(monkeypatch, whoami=(1, "ERROR: Access is denied.\r\n"))
    assert secrets.user_sid() is None
    tools.whoami = (0, WHOAMI)
    assert secrets.user_sid() == WHO
    assert len(tools.calls) == 2


def test_the_programs_get_no_window_no_input_ten_seconds_and_none_of_the_saved_keys(monkeypatch, tmp_path):
    secrets.set_live("OPENAI_API_KEY", "sk-one")
    seen = []

    def run(*args, **kwargs):
        argv = args[0] if args else kwargs["args"]
        seen.append((argv, kwargs))
        # Printed in the console's code page: bytes that aren't UTF-8 must not stop it
        out = WHOAMI.encode("ascii") if is_tool(argv, "whoami.exe") else b"Successfully processed \xe9\xff\r\n"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr=b"")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(secrets, "_USER", None)
    monkeypatch.setattr(secrets.subprocess, "run", run)
    assert secrets.secure_owner_only(tmp_path / "keys.enc") is True
    assert [Path(argv[0]).name for argv, _ in seen] == ["whoami.exe", "icacls.exe"]
    for argv, kwargs in seen:
        assert isinstance(argv, list) and not kwargs.get("shell")
        assert kwargs["creationflags"] == 0x08000000
        assert kwargs["timeout"] == 10
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert "OPENAI_API_KEY" not in kwargs["env"]


@pytest.mark.parametrize("failure", FAILURES)
def test_a_grant_that_fails_is_logged_and_reported_not_raised(monkeypatch, tmp_path, caplog, failure):
    tools = on_windows(monkeypatch, icacls=failure)
    path = tmp_path / "keys.enc"
    with caplog.at_level("WARNING", logger="ixel_mat.config.secrets"):
        assert secrets.secure_owner_only(path) is False
    assert str(path) in caplog.text
    assert len(tools.grants()) == 1


@pytest.mark.parametrize("failure", [
    *FAILURES,
    pytest.param((0, "Something else entirely\r\n"), id="says-something-else"),
])
def test_an_account_that_cant_be_found_leaves_the_file_as_it_is(monkeypatch, tmp_path, failure):
    tools = on_windows(monkeypatch, whoami=failure)
    assert secrets.secure_owner_only(tmp_path / "keys.enc") is False
    assert tools.grants() == []  # no grant without a SID


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_off_windows_nothing_is_run(monkeypatch, tmp_path, system):
    tools = Tools()
    monkeypatch.setattr(sys, "platform", system)
    monkeypatch.setattr(secrets, "_USER", None)
    monkeypatch.setattr(secrets, "_run_tool", tools)
    assert secrets.secure_owner_only(tmp_path / "keys.enc") is True
    assert secrets.secure_owner_only(tmp_path, folder=True) is True
    assert tools.calls == []


def test_saving_a_key_on_windows_limits_the_folder_then_the_file(keychain, monkeypatch):
    tools = on_windows(monkeypatch)
    secrets.save_secret("OPENAI_API_KEY", "sk-one")
    grants = tools.grants()
    assert [argv[1] for argv in grants] == [str(keys_file().parent), str(keys_file())]
    assert grants[0][-1] == f"*{SID}:(OI)(CI)F" and grants[1][-1] == f"*{SID}:F"
    assert tools.present == [True, True]  # keys.enc was there when it was limited
    assert secrets.saved_names() == {"OPENAI_API_KEY"}


@pytest.mark.parametrize("failure", FAILURES)
def test_keys_are_saved_even_when_every_grant_fails(keychain, monkeypatch, failure):
    tools = on_windows(monkeypatch, icacls=failure)
    secrets.save_secret("OPENAI_API_KEY", "sk-one")
    secrets.save_secret("XAI_API_KEY", "xai-two")
    assert len(tools.grants()) == 4  # the folder and the file, twice
    assert secrets.saved_names() == {"OPENAI_API_KEY", "XAI_API_KEY"}
    keychain.restart()  # the next run reads them back
    assert secrets.load_env() == {"OPENAI_API_KEY": "sk-one", "XAI_API_KEY": "xai-two"}
    assert sorted(p.name for p in keys_file().parent.iterdir()) == ["keys.enc"]  # no lock or temp file left


def test_keys_are_saved_even_when_whoami_cant_say_who_you_are(keychain, monkeypatch):
    tools = on_windows(monkeypatch, whoami=OSError(2, "The system cannot find the file specified"))
    secrets.save_secret("OPENAI_API_KEY", "sk-one")
    assert tools.grants() == [] and secrets.saved_names() == {"OPENAI_API_KEY"}
    keychain.restart()
    assert secrets.load_env() == {"OPENAI_API_KEY": "sk-one"}


def test_icacls_output_with_only_the_user_has_nothing_wrong(tmp_path):
    path = tmp_path / "keys.enc"
    entries = secrets.parse_icacls(icacls_text(path, "DESKTOP-ABC\\Me:(F)"), path)
    assert entries == [("DESKTOP-ABC\\Me", "(F)")]
    assert secrets.acl_problems(entries, *WHO) == (False, [])  # the account's case doesn't matter


def test_an_entry_shown_as_the_raw_sid_is_the_user_too(tmp_path):
    path = tmp_path / "keys.enc"
    entries = secrets.parse_icacls(icacls_text(path, f"{SID}:(F)"), path)
    assert entries == [(SID, "(F)")]
    assert secrets.acl_problems(entries, *WHO) == (False, [])


def test_inherited_entries_are_found(tmp_path):
    path = tmp_path / "keys.enc"
    text = icacls_text(path, "NT AUTHORITY\\SYSTEM:(I)(F)", "BUILTIN\\Administrators:(I)(F)", "DESKTOP-ABC\\Me:(I)(F)")
    entries = secrets.parse_icacls(text, path)
    assert entries == [("NT AUTHORITY\\SYSTEM", "(I)(F)"), ("BUILTIN\\Administrators", "(I)(F)"),
                       ("DESKTOP-ABC\\Me", "(I)(F)")]
    inherited, _ = secrets.acl_problems(entries, *WHO)
    assert inherited


def test_other_accounts_with_an_entry_of_their_own_are_found(tmp_path):
    path = tmp_path / "keys.enc"
    text = icacls_text(path, "DESKTOP-ABC\\Me:(F)", "DESKTOP-ABC\\Other:(R)", "BUILTIN\\Users:(RX)")
    entries = secrets.parse_icacls(text, path)
    assert secrets.acl_problems(entries, *WHO) == (False, ["DESKTOP-ABC\\Other", "BUILTIN\\Users"])


def test_a_mandatory_label_and_a_denial_let_no_one_in(tmp_path):
    path = tmp_path / "keys.enc"
    text = icacls_text(path, "DESKTOP-ABC\\Me:(F)", "Mandatory Label\\Low Mandatory Level:(I)(NW)",
                       "DESKTOP-ABC\\Other:(DENY)(R)")
    entries = secrets.parse_icacls(text, path)
    assert len(entries) == 3
    assert secrets.acl_problems(entries, *WHO) == (False, [])


def test_an_inherited_entry_and_an_extra_account_are_both_found(tmp_path):
    path = tmp_path / "keys.enc"
    entries = secrets.parse_icacls(icacls_text(path, "DESKTOP-ABC\\Me:(F)", "BUILTIN\\Users:(I)(RX)",
                                               "DESKTOP-ABC\\Other:(R)"), path)
    assert secrets.acl_problems(entries, *WHO) == (True, ["DESKTOP-ABC\\Other"])


def test_a_path_with_spaces_parses(tmp_path):
    path = tmp_path / "Some One" / "ixel mat" / "keys.enc"
    entries = secrets.parse_icacls(icacls_text(path, "DESKTOP-ABC\\Me:(F)", "NT AUTHORITY\\SYSTEM:(F)"), path)
    assert entries == [("DESKTOP-ABC\\Me", "(F)"), ("NT AUTHORITY\\SYSTEM", "(F)")]


def test_the_path_is_compared_without_regard_to_case(tmp_path):
    path = tmp_path / "keys.enc"
    shouted = Path(str(path).upper())
    assert secrets.parse_icacls(icacls_text(shouted, "DESKTOP-ABC\\Me:(F)"), path) == [("DESKTOP-ABC\\Me", "(F)")]


def test_the_summary_after_the_blank_line_is_not_an_entry(tmp_path):
    path = tmp_path / "keys.enc"
    text = icacls_text(path, "DESKTOP-ABC\\Me:(F)", summary="Dateien: (1) verarbeitet, (0) fehlgeschlagen")
    assert secrets.parse_icacls(text, path) == [("DESKTOP-ABC\\Me", "(F)")]


def test_output_that_isnt_about_this_path_gives_none(tmp_path):
    path = tmp_path / "keys.enc"
    assert secrets.parse_icacls(icacls_text(tmp_path / "other.enc", "DESKTOP-ABC\\Me:(F)"), path) is None
    assert secrets.parse_icacls("", path) is None
    assert secrets.parse_icacls("Access is denied.\r\n", path) is None
    assert secrets.parse_icacls(f"{path} \r\n\r\n{DONE}", path) is None  # no entry


def test_read_acl_gives_the_entries_icacls_printed(monkeypatch, tmp_path):
    path = tmp_path / "keys.enc"
    tools = on_windows(monkeypatch, icacls=(0, icacls_text(path, "DESKTOP-ABC\\Me:(F)")))
    assert secrets.read_acl(path) == ([("DESKTOP-ABC\\Me", "(F)")], "")
    assert is_tool(tools.calls[0], "icacls.exe") and tools.calls[0][1:] == [str(path)]


@pytest.mark.parametrize("answer, why", [
    ((5, "Access is denied.\r\n"), "icacls failed"),
    (OSError(2, "The system cannot find the file specified"), "The system cannot find the file specified"),
    (subprocess.TimeoutExpired(["icacls"], 10), "no answer in 10 s"),
])
def test_read_acl_says_why_it_couldnt(monkeypatch, tmp_path, answer, why):
    on_windows(monkeypatch, icacls=answer)
    assert secrets.read_acl(tmp_path / "keys.enc") == (None, why)


def test_read_acl_says_when_the_answer_isnt_one_it_understands(monkeypatch, tmp_path):
    on_windows(monkeypatch, icacls=(0, "Something else entirely\r\n"))
    entries, why = secrets.read_acl(tmp_path / "keys.enc")
    assert entries is None and why


def test_the_fix_is_one_icacls_line_that_is_safe_to_paste(tmp_path):
    path = tmp_path / "Some One" / "keys.enc"
    assert secrets.owner_only_fix(path, SID, False, []) == f'icacls "{path}" /inheritance:r /grant:r "*{SID}:F"'
    assert secrets.owner_only_fix(path.parent, SID, True, []) == (
        f'icacls "{path.parent}" /inheritance:r /grant:r "*{SID}:(OI)(CI)F"')


def test_the_fix_takes_the_extra_accounts_off_and_writes_a_raw_sid_with_a_star(tmp_path):
    path = tmp_path / "keys.enc"
    fix = secrets.owner_only_fix(path, SID, False, ["DESKTOP-ABC\\Other", "S-1-5-32-545"])
    assert fix == (f'icacls "{path}" /inheritance:r /grant:r "*{SID}:F" /remove:g "DESKTOP-ABC\\Other" '
                   '/remove:g "*S-1-5-32-545"')
    assert "\n" not in fix


# ── The app: requests never wait ──────────────────────────────────────────────

def _saving_in_the_background(name, value):
    errors = []

    def save():
        try:
            secrets.set_live(name, value)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=save, daemon=True)
    thread.start()
    return thread, errors


def quickly(call, seconds=0.5):
    started = time.monotonic()
    result = call()
    assert time.monotonic() - started < seconds, "it waited"
    return result


def test_reading_keys_never_waits_for_a_first_save_waiting_on_the_keychain(keychain, monkeypatch):
    monkeypatch.setattr(secrets, "KEYCHAIN_TIMEOUT", 5.0)
    assert secrets.where_keys_are().kind == "keychain"  # as the app does when it starts
    keychain.hold = threading.Event()  # the first save asks the keychain, which waits for a password
    before = keychain.calls
    thread, errors = _saving_in_the_background("OPENAI_API_KEY", "sk-one")
    wait_for(lambda: keychain.calls > before)
    assert quickly(lambda: secrets.load_env(wait=False)) == {}
    assert quickly(lambda: secrets.key_state("OPENAI_API_KEY")) == "none"
    assert quickly(secrets.saved_names) == set()
    assert quickly(secrets.where_keys_are).kind == "keychain"
    from ixel_mat.gui.server import GuiServer
    quickly(GuiServer(token="t")._load_settings)  # the app's requests load settings this way
    keychain.hold.set()
    thread.join(5)
    assert not errors and os.environ["OPENAI_API_KEY"] == "sk-one"


def test_reading_keys_never_waits_for_a_save_waiting_on_another_ixel(keychain, monkeypatch):
    monkeypatch.setattr(secrets, "LOCK_WAIT", 5.0)
    secrets.save_secret("OPENAI_API_KEY", "sk-one")
    secrets.load_env()
    lock = keys_file().with_name("keys.enc.lock")
    lock.write_bytes(b"another Ixel's")
    thread, errors = _saving_in_the_background("XAI_API_KEY", "xai-two")
    time.sleep(0.2)
    assert thread.is_alive()
    assert quickly(lambda: secrets.load_env(wait=False)) == {"OPENAI_API_KEY": "sk-one"}
    assert quickly(lambda: secrets.key_state("OPENAI_API_KEY")) == "file"
    assert quickly(secrets.saved_names) == {"OPENAI_API_KEY"}
    assert quickly(secrets.where_keys_are).kind == "keychain"
    lock.unlink()
    thread.join(5)
    assert not errors and secrets.saved_names() == {"OPENAI_API_KEY", "XAI_API_KEY"}


def test_keys_saved_elsewhere_meanwhile_are_read_on_a_thread_of_their_own(keychain):
    """ixel setup in a terminal made keys.enc while the app was open with none: the app's next request
    doesn't wait for the keychain, and the one after it has the keys."""
    secrets.save_secret("OPENAI_API_KEY", "sk-one")
    keychain.restart()  # this run hasn't asked the keychain anything yet
    keychain.hold = threading.Event()  # and it waits for a password
    assert quickly(lambda: secrets.load_env(wait=False)) == {}
    wait_for(lambda: keychain.calls >= 1)  # asked, on a thread of its own
    assert "OPENAI_API_KEY" not in os.environ
    assert quickly(secrets.saved_names) == set()  # Settings, meanwhile: what's in use so far
    assert quickly(lambda: secrets.key_state("OPENAI_API_KEY")) == "none"
    keychain.hold.set()
    wait_for(lambda: os.environ.get("OPENAI_API_KEY") == "sk-one")
    assert secrets.load_env(wait=False) == {"OPENAI_API_KEY": "sk-one"}
    assert quickly(secrets.saved_names) == {"OPENAI_API_KEY"}


def test_the_apps_pages_dont_wait_to_open_keys_another_ixel_saved(keychain, monkeypatch):
    """The app asked the keychain as it started, when there was no keys.enc; ixel setup in a terminal made one
    since. Settings and Health say where keys are at once, and keys.enc is opened on a thread of its own."""
    owner_only_acl(monkeypatch)  # (on Windows Health would run icacls: this measures the keychain)
    assert secrets.where_keys_are().kind == "keychain"  # as the app does when it starts
    secrets.save_secret("OPENAI_API_KEY", "sk-one")
    secrets._keychain.key = None  # (this run never opened keys.enc: the other Ixel did)
    keychain.hold = threading.Event()  # and the keychain waits for a password
    store = quickly(lambda: secrets.where_keys_are(wait=False))
    assert store.kind == "keychain" and store.path == keys_file()
    from ixel_mat import health
    assert quickly(lambda: health.keys_check(wait=False)).state == "ok"
    keychain.hold.set()
    wait_for(lambda: os.environ.get("OPENAI_API_KEY") == "sk-one")


def test_the_apps_pages_dont_wait_while_a_save_asks_a_keychain_that_failed_before(keychain, monkeypatch):
    """The keychain failed as the app started; unlocked since, a save asks it again. Settings and Health give
    the answer they had meanwhile, rather than waiting for the save (and its password prompt)."""
    monkeypatch.setattr(secrets, "KEYCHAIN_TIMEOUT", 5.0)
    owner_only_acl(monkeypatch)  # (on Windows Health would run icacls: this measures the keychain)
    calls, answer = [], threading.Event()

    def find():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("locked")
        answer.wait(5)
        return keychain, "your Mac's Keychain"
    monkeypatch.setattr(secrets, "_keychain", secrets._Keychain(find))
    assert secrets.where_keys_are().kind == "unavailable"  # as the app does when it starts
    thread, errors = _saving_in_the_background("OPENAI_API_KEY", "sk-one")
    wait_for(lambda: len(calls) == 2)
    store = quickly(lambda: secrets.where_keys_are(wait=False))
    assert store.kind == "unavailable" and "Unlock it" in store.problem
    from ixel_mat import health
    assert quickly(lambda: health.keys_check(wait=False)).state == "warn"
    answer.set()
    thread.join(5)
    assert not errors and secrets.where_keys_are(wait=False).kind == "keychain"


def test_the_apps_pages_dont_wait_for_a_keychain_never_asked(keychain):
    keychain.restart()  # nothing asked it yet, and a save holds the keys meanwhile
    held, done = threading.Event(), threading.Event()

    def save():
        with secrets._STORE_LOCK:
            held.set()
            done.wait(5)
    thread = threading.Thread(target=save, daemon=True)
    thread.start()
    held.wait(5)
    store = quickly(lambda: secrets.where_keys_are(wait=False))
    assert store.kind == "unavailable" and "still waiting" in store.problem
    done.set()
    thread.join(5)
    wait_for(lambda: secrets.where_keys_are(wait=False).kind == "keychain")  # asked on a thread of its own


def test_a_key_saved_in_lower_case_is_withheld_on_windows_too(monkeypatch):
    """Windows keeps variable names in capitals: one saved by hand as my_tool_key is MY_TOOL_KEY there."""
    monkeypatch.setattr(secrets, "_IGNORE_CASE", True)
    for name in ("my_tool_key", "MY_TOOL_KEY"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    assert secrets.set_live("my_tool_key", "sk-one") == "saved"
    os.environ["MY_TOOL_KEY"] = os.environ.pop("my_tool_key")  # as Windows keeps it
    env = secrets.child_env()
    assert "MY_TOOL_KEY" not in env and "my_tool_key" not in env
    assert secrets.child_env(pass_env=["my_tool_key"])["MY_TOOL_KEY"] == "sk-one"
    assert secrets.key_state("MY_TOOL_KEY") != "system" and secrets.ixels_own("MY_TOOL_KEY") == "sk-one"


def test_keys_added_to_env_by_hand_are_used_at_once_and_moved_on_a_thread(keychain, monkeypatch):
    monkeypatch.setattr(secrets, "LOCK_WAIT", 5.0)
    secrets.save_secret("OPENAI_API_KEY", "sk-one")
    write_env("XAI_API_KEY=xai-two\n")
    lock = keys_file().with_name("keys.enc.lock")
    lock.write_bytes(b"another Ixel's")  # moving them waits for it, but not the request
    assert quickly(lambda: secrets.load_env(wait=False)) == {"OPENAI_API_KEY": "sk-one", "XAI_API_KEY": "xai-two"}
    assert os.environ["XAI_API_KEY"] == "xai-two" and env_file().exists()
    lock.unlink()
    wait_for(lambda: (secrets.load_env(wait=False), not env_file().exists())[1])
    assert secrets.saved_names() == {"OPENAI_API_KEY", "XAI_API_KEY"}


def test_a_key_saved_while_keys_are_read_stays_in_use(keychain, monkeypatch):
    """A load that read the files before a save mustn't put back what was there before it."""
    secrets.save_secret("OPENAI_API_KEY", "sk-one")
    secrets.load_env()
    saved = threading.Event()
    lock = secrets._LOCK

    class Slow:
        """os.environ's lock, which the loading thread waits a while to take (a busy computer)."""
        def __enter__(self):
            if threading.current_thread() is loading and not saved.is_set():
                saved.wait(1.0)
            lock.acquire()

        def __exit__(self, *exc):
            lock.release()

    loading = threading.Thread(target=secrets.load_env)
    monkeypatch.setattr(secrets, "_LOCK", Slow())
    loading.start()
    time.sleep(0.1)  # it has read the files
    secrets.set_live("XAI_API_KEY", "xai-two")
    saved.set()
    loading.join(5)
    assert os.environ["XAI_API_KEY"] == "xai-two" and secrets.ixels_own("XAI_API_KEY") == "xai-two"
