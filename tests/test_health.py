"""The checks behind `ixel doctor` and the app's Health page."""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ixel_mat import health
from ixel_mat.agents.base import AgentConfig
from ixel_mat.runtime import Settings
from ixel_mat.triage import TriageSettings

KEY = "sk-test-never-shown-1234567890"


def settings_with(*configs, config=None, triage=None):
    return Settings(config or {}, {c.name: c for c in configs}, triage=triage or TriageSettings())


CLOUD = AgentConfig(name="gpt", label="GPT", type="http", url="https://api.openai.com/v1/chat/completions",
                    token=KEY, model="gpt-5")
NO_KEY = AgentConfig(name="grok", label="Grok", type="http", url="https://api.x.ai/v1/chat/completions", model="grok-4")
LOCAL = AgentConfig(name="llama", label="Llama", type="http", url="http://127.0.0.1:11434/v1/chat/completions",
                    model="llama3.3")
CODEX = AgentConfig(name="codex", label="Codex", type="oneshot", command="codex")
CLAUDE = AgentConfig(name="claude", label="Claude Code", type="oneshot", command="claude")


class Calls:
    """Fake program runner and model probe that remember what they were asked."""

    def __init__(self, programs=None, probes=None):
        self.ran, self.probed, self.envs = [], [], []
        self.programs = programs or {}
        self.probes = probes or {}

    async def run(self, argv, env=None):
        self.ran.append(argv)
        self.envs.append(env)
        return self.programs.get(" ".join(argv), (0, f"{argv[0]} 1.2.3"))

    async def probe(self, cfg):
        self.probed.append(cfg.name)
        return self.probes.get(cfg.name, ("ok", "reachable", 42, None))


def which_finding(*names):
    return lambda name: f"/usr/bin/{name}" if name in names else None


def run_report(settings, probe, calls, found=("git", "codex", "handoff"), system="linux"):
    return asyncio.run(health.report(probe, settings_loader=lambda: settings, which=which_finding(*found),
                                     run=calls.run, probe_agent=calls.probe, system=system))


def checks_of(report):
    return {c["id"]: c for g in report["groups"] for c in g["checks"]}


def test_opening_the_page_runs_nothing_and_sends_nothing(monkeypatch):
    from ixel_mat.gui import window

    def refuse(*args, **kwargs):
        raise AssertionError(f"a program was started: {args}")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)
    monkeypatch.setattr(window, "linux_window_command", refuse)
    calls = Calls()
    report = run_report(settings_with(CLOUD, NO_KEY, LOCAL, CODEX, CLAUDE), False, calls)
    assert calls.ran == [] and calls.probed == []
    assert report["schema"] == 1 and report["probed"] is False
    assert [g["id"] for g in report["groups"]] == ["ixel", "models", "handoff", "window", "machines"]
    checks = checks_of(report)
    assert checks["agent-gpt"]["state"] == "unchecked" and "Key set" in checks["agent-gpt"]["detail"]
    assert checks["agent-grok"]["state"] == "fail" and checks["agent-grok"]["fix"] == "ixel setup"
    assert checks["agent-llama"]["state"] == "unchecked" and "127.0.0.1:11434" in checks["agent-llama"]["detail"]
    assert checks["agent-codex"]["state"] == "unchecked"
    # A CLI that isn't installed says how to install it
    assert checks["agent-claude"]["state"] == "fail"
    assert checks["agent-claude"]["fix"] == "npm install -g @anthropic-ai/claude-code"
    assert KEY not in json.dumps(report)


def test_a_missing_key_names_the_variable_it_goes_in():
    settings = settings_with(NO_KEY, config={"agents": {"grok": {"token_env": "XAI_API_KEY"}}})
    check = checks_of(run_report(settings, False, Calls()))["agent-grok"]
    assert check["detail"] == "No API key (XAI_API_KEY)"


def test_check_now_asks_each_model_and_program():
    calls = Calls(programs={"codex login status": (1, "Not logged in")},
                  probes={"llama": ("unreachable", "no answer: refused", None, None),
                          "gpt": ("auth_failed", f"401 Incorrect API key provided: {KEY}", 80, "openai")})
    report = run_report(settings_with(CLOUD, LOCAL, CODEX), True, calls)
    assert sorted(calls.probed) == ["gpt", "llama"]
    assert ["codex", "--version"] in calls.ran and ["codex", "login", "status"] in calls.ran
    assert ["/usr/bin/handoff", "version"] in calls.ran
    checks = checks_of(report)
    assert checks["agent-gpt"]["state"] == "fail" and checks["agent-gpt"]["fix"] == "ixel setup"
    assert KEY not in json.dumps(report)  # quoted back by the service, never shown
    assert checks["agent-llama"]["state"] == "fail" and "Is the model server running?" in checks["agent-llama"]["detail"]
    assert checks["agent-codex"] == {"id": "agent-codex", "label": "Codex", "state": "ok", "detail": "codex 1.2.3",
                                     "fix": ""}
    assert checks["agent-codex-login"]["state"] == "warn" and checks["agent-codex-login"]["fix"] == "codex login"
    assert checks["handoff"]["detail"] == "/usr/bin/handoff 1.2.3"


@pytest.mark.parametrize("status, state, fix", [
    (("ok", "reachable", 30, None), "ok", ""),
    (("rate_limited", "429", 30, None), "warn", ""),
    (("model_missing", "the server has no model 'x'", 30, None), "fail", "ixel model gpt"),
])
def test_what_each_probe_result_means(status, state, fix):
    check = checks_of(run_report(settings_with(CLOUD), True, Calls(probes={"gpt": status})))["agent-gpt"]
    assert (check["state"], check["fix"]) == (state, fix)


def test_a_probe_that_breaks_is_a_result():
    calls = Calls()

    async def broken(cfg):
        raise RuntimeError("socket closed")

    report = asyncio.run(health.report(True, settings_loader=lambda: settings_with(CLOUD), which=which_finding("git"),
                                       run=calls.run, probe_agent=broken, system="linux"))
    assert checks_of(report)["agent-gpt"]["state"] == "fail"


def test_no_models_and_no_handoff_say_what_to_run():
    report = run_report(settings_with(), False, Calls(), found=("git",), system="win32")
    checks = checks_of(report)
    assert checks["agents"]["fix"] == "ixel setup"
    assert checks["handoff"]["state"] == "off"
    assert checks["handoff"]["fix"] == "irm https://ixelai.com/handoff/install.ps1 | iex"
    assert checks["pictures"]["state"] == "off"


def test_git_missing_is_a_failure_with_the_line_that_installs_it(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    git = checks_of(run_report(settings_with(CLOUD), False, Calls(), found=()))["git"]
    assert git["state"] == "fail" and git["fix"] == "winget install --id Git.Git -e"


def test_triage_turned_on_without_a_key_is_flagged():
    report = run_report(settings_with(CLOUD, triage=TriageSettings(enabled=True)), False, Calls())
    assert checks_of(report)["triage"]["state"] == "warn"


@pytest.mark.skipif(os.name != "posix", reason="file modes")
def test_a_keys_file_others_can_read_is_flagged(tmp_path, monkeypatch):
    from ixel_mat.config import secrets
    env = tmp_path / ".env"
    env.write_text('X="1"\n')
    env.chmod(0o644)
    monkeypatch.setattr(secrets, "_ENV_FILE", env)
    check = checks_of(run_report(settings_with(CLOUD), False, Calls()))["keys-file"]
    assert check["state"] == "warn" and check["fix"] == f"chmod 600 '{env}'"
    env.chmod(0o600)
    assert checks_of(run_report(settings_with(CLOUD), False, Calls()))["keys-file"]["state"] == "ok"


def test_saved_keys_say_where_they_are(keychain):
    from ixel_mat.config import secrets
    check = checks_of(run_report(settings_with(CLOUD), False, Calls()))["keys-file"]
    assert check["state"] == "ok" and check["label"] == "Saved keys"
    assert check["detail"] == (f"None yet. Keys you save are encrypted in {secrets.get_keys_file_path()}, and the "
                               "key that opens them is kept in your Mac's Keychain")
    secrets.save_secret("OPENAI_API_KEY", KEY)
    report = run_report(settings_with(CLOUD), False, Calls())
    assert checks_of(report)["keys-file"]["detail"] == (f"Encrypted in {secrets.get_keys_file_path()}, and the key "
                                                        "that opens them is kept in your Mac's Keychain")
    assert KEY not in json.dumps(report)


def test_keys_in_plain_text_are_flagged_and_say_why(keychain):
    from ixel_mat.config import secrets
    keychain.restart(present=False)
    check = checks_of(run_report(settings_with(CLOUD), False, Calls()))["keys-file"]
    assert check["state"] == "ok" and check["detail"].startswith("None yet. Keys you save go in")
    secrets.save_secret("OPENAI_API_KEY", KEY)
    check = checks_of(run_report(settings_with(CLOUD), False, Calls()))["keys-file"]
    who = "only you can read" if os.name == "posix" else "in your user folder"
    assert check["state"] == "warn" and check["detail"] == (
        f"In {secrets.get_env_file_path()}, a plain-text file {who}, because this computer has no keychain Ixel "
        "can use")


def test_keys_in_plain_text_because_the_keychain_refused_say_so(keychain):
    from ixel_mat.config import secrets
    keychain.refuse = RuntimeError("not allowed")
    secrets.save_secret("OPENAI_API_KEY", KEY)
    check = checks_of(run_report(settings_with(CLOUD), False, Calls()))["keys-file"]
    who = "only you can read" if os.name == "posix" else "in your user folder"
    said = (f"In {secrets.get_env_file_path()}, a plain-text file {who}, because your Mac's Keychain refused to keep "
            "the key that would encrypt them. Ixel tries again when you next save a key or start it")
    assert check["state"] == "warn" and check["detail"] == said[:health.MAX_DETAIL]  # a Mac's temp folder is long


def test_the_ixel_checks_run_off_the_event_loop(monkeypatch):
    """Where the saved keys are may mean asking the keychain, which can wait for a password."""
    import threading
    ran_on = []
    checks = health.ixel_checks

    def recording(*args):
        ran_on.append(threading.current_thread())
        return checks(*args)

    monkeypatch.setattr(health, "ixel_checks", recording)
    report = run_report(settings_with(CLOUD), False, Calls())
    assert ran_on and ran_on[0] is not threading.main_thread()
    assert report["groups"][0]["id"] == "ixel" and checks_of(report)["version"]["state"] == "ok"


def test_saved_keys_that_cant_be_used_say_what_to_do(keychain):
    from ixel_mat.config import secrets
    secrets.save_secret("OPENAI_API_KEY", KEY)
    keychain.restart()
    keychain.error = RuntimeError("locked")
    secrets.load_env()
    check = checks_of(run_report(settings_with(CLOUD), False, Calls()))["keys-file"]
    assert check["state"] == "warn" and "Unlock it, then restart Ixel" in check["detail"]
    keychain.error = None
    keychain.items.clear()  # the key that opens them is gone
    keychain.restart()
    check = checks_of(run_report(settings_with(CLOUD), False, Calls()))["keys-file"]
    assert check["state"] == "fail" and check["fix"] == "ixel setup"
    assert "Add them again in Settings or with ixel setup" in check["detail"]


def test_saved_keys_that_cant_be_read_are_one_failed_check(monkeypatch):
    from ixel_mat.config import secrets

    def unreadable(wait=True):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(secrets, "where_keys_are", unreadable)
    check = checks_of(run_report(settings_with(CLOUD), False, Calls()))["keys-file"]
    assert check["state"] == "fail" and "Permission denied" in check["detail"]


def test_every_package_ixel_needs_is_checked():
    from ixel_mat.config.loader import tomllib
    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    needed = {re.split(r"[<>=;\s\[]", dep, maxsplit=1)[0].lower() for dep in pyproject["project"]["dependencies"]}
    assert set(health.PACKAGES) | {"tomli"} == needed  # tomli only before Python 3.11, checked on its own


def test_failing_is_only_what_needs_fixing():
    report = {"groups": [{"checks": [{"state": "ok"}, {"state": "off"}, {"state": "unchecked"}, {"state": "warn"}]}]}
    assert not health.failing(report)
    report["groups"][0]["checks"].append({"state": "fail"})
    assert health.failing(report)


def test_a_settings_file_that_cant_be_read_says_so(tmp_path, monkeypatch):
    from ixel_mat.config import loader
    broken = tmp_path / "config.toml"
    broken.write_text("[agents.gpt\n", encoding="utf-8")
    monkeypatch.setattr(loader, "find_config", lambda explicit=None: broken)
    config = loader.load_config()
    assert config["_error"]
    checks = checks_of(run_report(settings_with(config=config), False, Calls()))
    assert checks["config"]["state"] == "fail" and checks["config"]["detail"].startswith("Couldn't read")
    assert str(broken) in checks["config"]["detail"] and checks["config"]["fix"] == "ixel setup"
    assert checks["agents"]["state"] == "fail" and "can't read its settings" in checks["agents"]["detail"]
    assert checks["agents"]["fix"] == ""  # not "set up a model": the file is the problem


def test_a_cli_is_checked_with_the_environment_its_agent_gets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-the-shell")
    codex = AgentConfig(name="codex", label="Codex", type="oneshot", command="codex", drop_env=["OPENAI_API_KEY"],
                        env={"CODEX_HOME": "/tmp/codex-home"})
    calls = Calls()
    run_report(settings_with(codex), True, calls)
    for argv, env in zip(calls.ran, calls.envs):
        if argv[0] == "codex":
            assert "OPENAI_API_KEY" not in env and env["CODEX_HOME"] == "/tmp/codex-home"



def test_gemini_cli_without_a_sign_in_says_what_to_do_even_before_check_now(gemini_home, monkeypatch):
    from ixel_mat.presets import GEMINI_SIGN_IN, PRESETS_BY_ID
    gemini = AgentConfig(name="gemini", label="Gemini CLI", type="oneshot", command="gemini",
                         drop_env=PRESETS_BY_ID["gemini_cli"]["drop_env"])
    found = ("git", "gemini")
    check = checks_of(run_report(settings_with(gemini), False, Calls(), found=found))["agent-gemini-login"]
    assert check == {"id": "agent-gemini-login", "label": "Gemini CLI sign-in", "state": "warn",
                     "detail": GEMINI_SIGN_IN, "fix": "gemini"}
    from ixel_mat.config import secrets
    monkeypatch.setenv("GOOGLE_API_KEY", KEY)  # saved under Keys
    monkeypatch.setattr(secrets, "_INJECTED", {"GOOGLE_API_KEY"})
    calls = Calls()
    report = run_report(settings_with(gemini), True, calls, found=found)
    check = checks_of(report)["agent-gemini-login"]
    assert (check["state"], check["detail"]) == ("ok", "Uses your Gemini API key") and KEY not in json.dumps(report)
    assert next(env for argv, env in zip(calls.ran, calls.envs) if argv[0] == "gemini")["GEMINI_API_KEY"] == KEY
    (gemini_home / ".gemini" / "settings.json").write_text(
        json.dumps({"security": {"auth": {"selectedType": "oauth-personal"}}}), encoding="utf-8")
    check = checks_of(run_report(settings_with(gemini), False, Calls(), found=found))["agent-gemini-login"]
    assert (check["state"], check["detail"]) == ("ok", "Set up to sign in, in Gemini CLI itself")

@pytest.mark.skipif(os.name != "posix", reason="process groups")
def test_a_program_that_hangs_is_ended_with_what_it_started(tmp_path):
    marker = tmp_path / "child.pid"
    script = ("import subprocess, sys, time\n"
              "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
              f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
              "time.sleep(60)\n")
    started = time.monotonic()
    code, why = asyncio.run(health.run_program([sys.executable, "-c", script], timeout=2))
    assert code is None and why == "no answer in 2 s" and time.monotonic() - started < 10
    pid = int(marker.read_text())
    for _ in range(50):
        if not _running(pid):
            break
        time.sleep(0.1)
    else:
        pytest.fail("the program's child is still running")


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # ended but not yet collected: in a container whose first process collects nothing, it stays that way
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):  # no /proc (a Mac), or it went in between
        return True


def test_check_now_doesnt_wait_for_a_probe_that_never_ends(monkeypatch):
    monkeypatch.setattr(health, "PROBE_TIMEOUT", 1)

    async def stuck(cfg):
        return await health.off_the_loop(time.sleep, 8)

    started = time.monotonic()
    report = asyncio.run(health.report(True, settings_loader=lambda: settings_with(CLOUD), which=which_finding("git"),
                                       run=Calls().run, probe_agent=stuck, system="linux"))
    assert time.monotonic() - started < 5  # asyncio.run doesn't wait for the stuck thread as it exits
    assert checks_of(report)["models"]["detail"] == "The checks took too long; try again"


def test_doctor_json_has_the_same_report(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, "-m", "ixel_mat", "doctor", "--json"], env=env, cwd=tmp_path,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 1, proc.stderr  # no models yet: something needs fixing
    report = json.loads(proc.stdout)
    assert report["schema"] == 1 and report["probed"] is False
    assert {c["state"] for g in report["groups"] for c in g["checks"]} <= set(health.STATES)
    assert checks_of(report)["config"]["fix"] == "ixel setup"


def test_doctor_text_says_how_to_test_models(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "PYTHONIOENCODING": "utf-8",
           "COLUMNS": "200"}
    proc = subprocess.run([sys.executable, "-m", "ixel_mat", "doctor"], env=env, cwd=tmp_path,
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 1, proc.stderr
    assert "Fix: ixel setup" in proc.stdout and "ixel doctor --check" in proc.stdout


def test_a_model_server_on_your_network_and_a_key_that_would_go_in_cleartext():
    lan = AgentConfig(name="nas", label="NAS", type="http", url="http://192.168.1.20:11434/v1/chat/completions",
                      model="llama3.3")
    leaky = AgentConfig(name="leaky", label="Leaky", type="http", url="http://192.168.1.20:8000/v1/chat/completions",
                        token=KEY, model="m")
    calls = Calls()
    checks = checks_of(run_report(settings_with(lan, leaky, LOCAL), True, calls))
    assert calls.probed == ["nas", "llama"]  # the key is never put on the wire to check it
    assert checks["agent-leaky"]["state"] == "fail" and "plain http://" in checks["agent-leaky"]["detail"]
    assert "https://" in checks["agent-leaky"]["fix"]
    unchecked = checks_of(run_report(settings_with(lan, LOCAL), False, Calls()))
    assert "Runs on your own network at 192.168.1.20:11434" in unchecked["agent-nas"]["detail"]
    assert "Runs on this computer at 127.0.0.1:11434" in unchecked["agent-llama"]["detail"]
