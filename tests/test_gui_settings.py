"""The app's Settings page: checked edits to config.toml, and keys that can be saved but never read back."""
import json
import os
import sys

import pytest

from ixel_mat.config import edit, loader, secrets
from ixel_mat.gui.server import GuiServer
from ixel_mat.runtime import load_settings
from test_gui_server import AUTH, JSON_AUTH, TOKEN, run_with_client

CONFIG = """\
# My Ixel settings
[agents.claude]
preset = "claude_code"
model = "latest"   # the newest one

[agents.gpt]
type = "http"
url = "https://api.openai.com/v1/chat/completions"
token_env = "OPENAI_API_KEY"
label = "GPT"
model = "gpt-5"

[agents.local]
type = "http"
url = "http://127.0.0.1:11434/v1/chat/completions"
label = "Llama"
model = "llama3"

[review]
mode = "review"   # what runs
agents = [
  "claude",
  "gpt",  # not the local one
]

# Pictures
[images]
xai_model = "grok-imagine-image-2.0"
"""

SECRET = "sk-test-0123456789abcdefSECRET"


@pytest.fixture
def config():
    path = loader._GLOBAL_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _keys_put_back(monkeypatch):
    """set_live changes this process's environment: each test gets it back as it was."""
    injected = set(secrets._INJECTED)
    names = ("OPENAI_API_KEY", "XAI_API_KEY", "ANTHROPIC_API_KEY", "TYPESAFE_API_KEY", "GOOGLE_API_KEY",
             "GROQ_API_KEY")
    saved = {n: os.environ.get(n) for n in names}
    for name in names:
        monkeypatch.delenv(name, raising=False)
    yield
    secrets._INJECTED.clear()
    secrets._INJECTED.update(injected)
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def gui():
    return GuiServer(token=TOKEN, settings_loader=load_settings)


def call(*steps):
    """Each step is ("GET", path) or ("POST", path, body); returns [(status, json)]."""
    async def scenario(client):
        out = []
        for method, path, *body in steps:
            if method == "GET":
                resp = await client.get(path, headers=AUTH)
            else:
                resp = await client.post(path, headers=JSON_AUTH, data=json.dumps(body[0]))
            out.append((resp.status, await resp.json()))
        return out
    return run_with_client(gui(), scenario)


def snapshot():
    [(status, data)] = call(("GET", "/api/settings"))
    assert status == 200
    return data


def change(section, values, agent=None, version=None):
    body = {"section": section, "values": values, "version": version or snapshot()["version"]}
    if agent:
        body["agent"] = agent
    [(status, data)] = call(("POST", "/api/settings", body))
    return status, data


# ── The edits themselves ─────────────────────────────────────────────────────

def test_an_edit_changes_only_its_own_lines():
    text = edit.set_values(CONFIG, ("review",), {"mode": "deep"})
    assert text == CONFIG.replace('mode = "review"   # what runs', 'mode = "deep"   # what runs')
    text = edit.set_values('[review]\nmode = "quick"#x\ntimeout = 60\n', ("review",), {"mode": "deep", "timeout": 90})
    assert text == '[review]\nmode = "deep"#x\ntimeout = 90\n'
    text = edit.set_values(CONFIG, ("agents", "claude"), {"model": edit.REMOVE, "effort": "high"})
    assert 'model = "latest"' not in text and '# My Ixel settings' in text
    assert '[agents.claude]\npreset = "claude_code"\neffort = "high"\n\n[agents.gpt]' in text


def test_an_array_over_several_lines_is_replaced_whole():
    text = edit.set_values(CONFIG, ("review",), {"agents": ["gpt"]})
    assert 'agents = ["gpt"]\n\n# Pictures' in text and '"claude",' not in text
    assert loader.tomllib.loads(text)["review"] == {"mode": "review", "agents": ["gpt"]}


def test_a_missing_table_is_added_at_the_end():
    text = edit.set_values(CONFIG, ("saver",), {"verifier": "gpt", "escalate": "always"})
    assert text.startswith(CONFIG) and text.endswith('\n[saver]\nverifier = "gpt"\nescalate = "always"\n')


@pytest.mark.parametrize("text", [
    'review = { mode = "quick" }\n',                       # an inline table
    'review.mode = "quick"\n',                             # dotted keys
    '[review]\nmode = """\nquick"""\n',                    # a string over several lines
])
def test_a_file_written_another_way_is_refused_not_mangled(text):
    with pytest.raises(edit.EditError):
        edit.set_values(text, ("review",), {"mode": "deep"})


def test_a_bracket_in_a_string_isnt_an_array():
    text = '[review]\nmoderator = "a]b" # [x]\nmode = "quick"\n'
    assert loader.tomllib.loads(edit.set_values(text, ("review",), {"moderator": "gpt"}))["review"] == \
        {"moderator": "gpt", "mode": "quick"}


def test_the_file_keeps_a_copy_and_refuses_a_stale_page(config):
    old = edit.version(config.read_bytes())  # as written: with \r\n on Windows
    edit.edit_file(config, ("review",), {"mode": "quick"}, old)
    assert config.with_suffix(".toml.bak").read_text(encoding="utf-8") == CONFIG
    with pytest.raises(edit.Changed):
        edit.edit_file(config, ("review",), {"mode": "deep"}, old)


# ── What the page reads ──────────────────────────────────────────────────────

def test_the_page_sees_choices_and_where_keys_come_from_but_never_a_key(config, monkeypatch):
    secrets.save_secret("OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("XAI_API_KEY", "xai-from-the-shell-" + SECRET)
    [(status, data)] = call(("GET", "/api/settings"))
    assert status == 200 and data["editable"] and data["source"] == str(config)
    assert [(a["name"], a["on_panel"], a["model"], a["in_file"]) for a in data["agents"]] == \
        [("claude", True, "latest", True), ("gpt", True, "gpt-5", True), ("local", False, "llama3", True)]
    assert data["triage"]["host"] == "api.typesafe.ai" and data["triage"]["official"]
    assert data["review"]["mode"] == "review" and data["review"]["moderator"] == ""
    keys = {k["name"]: k for k in data["keys"]}
    assert keys["OPENAI_API_KEY"]["state"] == "file" and "GPT" in keys["OPENAI_API_KEY"]["used_by"]
    assert keys["XAI_API_KEY"]["state"] == "system" and keys["ANTHROPIC_API_KEY"]["state"] == "none"
    assert SECRET not in json.dumps(data)



def test_the_google_key_says_gemini_cli_uses_it(config):
    config.write_text(CONFIG + '\n[agents.gemini]\npreset = "gemini_cli"\n', encoding="utf-8")
    keys = {k["name"]: k for k in snapshot()["keys"]}
    assert keys["GOOGLE_API_KEY"]["label"] == "Google (Gemini)" and keys["GOOGLE_API_KEY"]["used_by"] == ["Gemini CLI"]
    assert "GEMINI_API_KEY" not in keys  # one key for both

def test_without_a_settings_file_the_page_says_to_run_setup():
    data = snapshot()
    assert not data["editable"] and "ixel setup" in data["problem"] and data["agents"] == []
    status, reply = change("review", {"mode": "deep"}, version="")
    assert status == 409 and "ixel setup" in reply["error"]


# ── What the page changes ────────────────────────────────────────────────────

def test_choices_are_saved_and_read_back(config):
    assert change("review", {"mode": "deep", "moderator": "gpt", "timeout": 240})[0] == 200
    assert change("agent", {"model": "gpt-5-mini", "effort": "low"}, agent="gpt")[0] == 200
    assert change("saver", {"verifier": "claude", "escalate": "disagreement"})[0] == 200
    assert change("images", {"provider": "openai"})[0] == 200
    settings = load_settings()
    assert settings.review.mode.value == "deep" and settings.review.moderator == "gpt"
    assert settings.review.timeout == 240 and settings.agent_configs["gpt"].model == "gpt-5-mini"
    assert settings.agent_configs["gpt"].effort == "low" and settings.saver.verifier == "claude"
    text = config.read_text(encoding="utf-8")
    assert "# My Ixel settings" in text and "# Pictures" in text and 'provider = "openai"' in text


def test_the_panel_needs_one_model_and_all_of_them_means_every_one(config):
    status, data = change("panel", {"on": []})
    assert status == 400 and "at least one" in data["error"]
    assert change("panel", {"on": ["claude", "gpt", "local"]})[0] == 200
    assert "agents" not in loader.tomllib.loads(config.read_text(encoding="utf-8"))["review"]  # new ones join too
    assert change("panel", {"on": ["local"]})[0] == 200
    assert load_settings().review.agents == ["local"]


@pytest.mark.parametrize("section, values, agent", [
    ("agent", {"command": "calc.exe"}, "claude"),
    ("agent", {"url": "https://evil.example/v1"}, "gpt"),
    ("agent", {"token_env": "AWS_SECRET_ACCESS_KEY"}, "gpt"),
    ("agent", {"args": ["--dangerously-skip-permissions"]}, "claude"),
    ("agent", {"model": "x; rm -rf ~"}, "gpt"),
    ("agent", {"model": "gpt-5"}, "nobody"),
    ("review", {"mode": "yolo"}, None),
    ("review", {"moderator": "nobody"}, None),
    ("review", {"timeout": 0}, None),
    ("review", {"timeout": True}, None),
    ("triage", {"url": "https://evil.example"}, None),
    ("images", {"xai_url": "https://evil.example"}, None),
    ("agents", {"gpt": {}}, None),
    ("review", {}, None),
])
def test_what_runs_and_where_things_go_cant_be_changed_here(config, section, values, agent):
    status, data = change(section, values, agent=agent)
    assert status == 400 and data["error"]
    assert config.read_text(encoding="utf-8") == CONFIG


def test_a_page_that_read_an_older_file_is_told_and_given_the_new_one(config):
    old = snapshot()["version"]
    config.write_text(CONFIG.replace('mode = "review"', 'mode = "quick"'), encoding="utf-8")  # ixel setup ran
    status, data = change("review", {"mode": "deep"}, version=old)
    assert status == 409 and "changed" in data["error"] and data["settings"]["review"]["mode"] == "quick"
    assert 'mode = "quick"' in config.read_text(encoding="utf-8")


def test_a_file_written_in_a_way_it_cant_edit_is_left_alone(config):
    config.write_text('saver.verifier = "gpt"\n' + CONFIG, encoding="utf-8")  # [saver] made by a dotted key
    before = config.read_text(encoding="utf-8")
    status, data = change("saver", {"escalate": "always"})
    assert status == 422 and "by hand" in data["error"] and config.read_text(encoding="utf-8") == before


# ── Keys ─────────────────────────────────────────────────────────────────────

def key(body):
    [(status, data)] = call(("POST", "/api/settings/key", body))
    return status, data


def test_a_saved_key_is_used_at_once_and_still_kept_from_the_programs_ixel_starts(config):
    status, data = key({"name": "OPENAI_API_KEY", "value": f'  "{SECRET}"\n'})
    assert status == 200 and data["state"] == "file" and SECRET not in json.dumps(data)
    assert f'OPENAI_API_KEY="{SECRET}"' in secrets.get_env_file_path().read_text(encoding="utf-8")
    assert os.environ["OPENAI_API_KEY"] == SECRET and load_settings().agent_configs["gpt"].token == SECRET
    assert "OPENAI_API_KEY" not in secrets.child_env()  # Claude Code and Codex don't get it
    status, data = key({"name": "OPENAI_API_KEY", "remove": True})
    assert status == 200 and data["state"] == "none" and "OPENAI_API_KEY" not in os.environ
    assert "OPENAI_API_KEY" not in secrets.get_env_file_path().read_text(encoding="utf-8")


def test_a_key_set_outside_ixel_wins_and_the_page_says_so(config, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "from-the-shell")
    status, data = key({"name": "XAI_API_KEY", "value": SECRET})
    assert status == 200 and data["state"] == "system" and "wins" in data["message"]
    assert os.environ["XAI_API_KEY"] == "from-the-shell"
    status, data = key({"name": "XAI_API_KEY", "remove": True})
    assert data["state"] == "system" and "still used" in data["message"]


@pytest.mark.parametrize("body", [
    {"name": "PATH", "value": "/tmp/evil"},
    {"name": "NODE_OPTIONS", "value": "--require=/tmp/x.js"},
    {"name": "ANTHROPIC_BASE_URL", "value": "https://evil.example"},
    {"name": "OPENAI_API_KEY", "value": "two words"},
    {"name": "OPENAI_API_KEY", "value": 'sk-"quoted'},
    {"name": "OPENAI_API_KEY", "value": "sk-line\nBREAK=1"},
    {"name": "OPENAI_API_KEY", "value": ""},
    {"name": "OPENAI_API_KEY", "value": "x" * 401},
    {"name": "OPENAI_API_KEY"},
    {"name": ["OPENAI_API_KEY"], "value": SECRET},
    {"name": None, "value": SECRET},
])
def test_only_keys_ixel_uses_and_only_key_shaped_values(config, body):
    status, data = key(body)
    assert status == 400 and data["error"]
    assert not secrets.get_env_file_path().exists()



def test_a_saved_copy_of_a_key_set_outside_ixel_can_still_be_removed(config, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "from-the-shell")
    key({"name": "XAI_API_KEY", "value": SECRET})
    keys = {k["name"]: k for k in snapshot()["keys"]}
    assert keys["XAI_API_KEY"]["state"] == "system" and keys["XAI_API_KEY"]["saved"]
    assert not keys["OPENAI_API_KEY"]["saved"]
    key({"name": "XAI_API_KEY", "remove": True})
    assert not {k["name"]: k for k in snapshot()["keys"]}["XAI_API_KEY"]["saved"]


def test_an_empty_variable_isnt_a_key_set_outside_ixel(config, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    status, data = key({"name": "OPENAI_API_KEY", "value": SECRET})
    assert data["state"] == "file" and os.environ["OPENAI_API_KEY"] == SECRET
    assert secrets.key_state("OPENAI_API_KEY") == "file" and "OPENAI_API_KEY" not in secrets.child_env()


def test_keys_ixel_set_follow_the_file_and_go_when_taken_out_of_it(config):
    secrets.save_secret("OPENAI_API_KEY", SECRET)
    secrets.load_env()
    assert os.environ["OPENAI_API_KEY"] == SECRET
    secrets.save_secret("OPENAI_API_KEY", SECRET + "2")       # `ixel setup` changed it
    secrets.load_env()
    assert os.environ["OPENAI_API_KEY"] == SECRET + "2"
    secrets.remove_secret("OPENAI_API_KEY")                   # and took it out
    secrets.load_env()
    assert "OPENAI_API_KEY" not in os.environ and "OPENAI_API_KEY" not in secrets._INJECTED


def test_programs_started_while_a_key_is_saved_never_get_it(config):
    import threading
    leaks, errors, done = [], [], threading.Event()

    def start_programs():
        while not done.is_set():
            try:
                if "OPENAI_API_KEY" in secrets.child_env():
                    leaks.append(1)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=start_programs) for _ in range(2)]
    for t in threads:
        t.start()
    try:
        for _ in range(300):
            secrets.set_live("OPENAI_API_KEY", SECRET)
            secrets.remove_live("OPENAI_API_KEY")
            secrets.load_env()
    finally:
        done.set()
        for t in threads:
            t.join()
    assert not leaks and not errors


# ── What the file is left like ───────────────────────────────────────────────

def test_a_page_without_the_files_version_is_told_to_look_again(config):
    config.write_text(CONFIG.replace('mode = "review"', 'mode = "quick"'), encoding="utf-8")
    for version in (None, 123, ["x"], ""):
        [(status, data)] = call(("POST", "/api/settings", {"section": "review", "values": {"mode": "deep"},
                                                           **({} if version is None else {"version": version})}))
        assert status == 409 and data["settings"]["review"]["mode"] == "quick"
    assert 'mode = "quick"' in config.read_text(encoding="utf-8")


def test_a_file_that_isnt_utf8_is_left_alone(config):
    config.write_bytes(CONFIG.replace("# My Ixel settings", "# configuraci\xf3n").encode("latin-1"))
    before = config.read_bytes()
    status, data = change("review", {"mode": "deep"})
    assert status == 422 and "UTF-8" in data["error"] and config.read_bytes() == before


def test_a_bom_and_crlf_lines_stay(config):
    config.write_bytes(b"\xef\xbb\xbf" + CONFIG.replace("\n", "\r\n").encode("utf-8"))
    assert change("review", {"mode": "deep"})[0] == 200
    data = config.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf") and b'mode = "deep"   # what runs\r\n' in data and b"\n\n" not in data


def test_no_backup_is_kept_when_the_file_holds_a_key(config):
    config.write_text(CONFIG.replace('label = "Llama"', f'label = "Llama"\ntoken = "{SECRET}"'), encoding="utf-8")
    assert not snapshot()["backup"]
    assert change("review", {"mode": "deep"})[0] == 200
    assert not config.with_suffix(".toml.bak").exists()


def test_a_linked_settings_file_stays_a_link(config, tmp_path):
    real = tmp_path / "dotfiles" / "ixel.toml"
    real.parent.mkdir()
    real.write_text(CONFIG, encoding="utf-8")
    config.unlink()
    try:
        config.symlink_to(real)
    except OSError:  # Windows lets only administrators, or Developer Mode, make links
        pytest.skip("this account can't make symbolic links")
    assert change("review", {"mode": "deep"})[0] == 200
    assert config.is_symlink() and 'mode = "deep"' in real.read_text(encoding="utf-8")


def test_a_new_setting_goes_above_the_next_tables_comment(config):
    assert change("review", {"moderator": "gpt"})[0] == 200
    assert ']\nmoderator = "gpt"\n\n# Pictures\n[images]' in config.read_text(encoding="utf-8")


def test_taking_a_setting_out_of_a_table_that_isnt_there_is_fine(config):
    assert change("saver", {"verifier": ""})[0] == 200
    assert config.read_text(encoding="utf-8") == CONFIG


# ── Choices that can't work together ─────────────────────────────────────────

def test_saver_cant_be_where_questions_start_without_a_verifier(config):
    status, data = change("review", {"mode": "saver"})
    assert status == 400 and "Saver needs a big model" in data["error"]
    assert change("review", {"plain_questions": "saver"})[0] == 400
    assert change("saver", {"verifier": "gpt"})[0] == 200
    assert change("review", {"mode": "saver"})[0] == 200
    status, data = change("saver", {"verifier": ""})        # it would leave Saver without one
    assert status == 400 and "Saver needs a big model" in data["error"]
    assert change("review", {"moderator": "claude"})[0] == 200
    assert change("saver", {"verifier": ""})[0] == 200       # the moderator verifies now
    assert load_settings().saver.verifier == "claude"


def test_who_writes_the_verdict_stays_on_the_panel(config):
    status, data = change("review", {"moderator": "local"})   # not on the panel
    assert status == 400 and "has to be on the panel" in data["error"]
    assert change("review", {"moderator": "gpt"})[0] == 200
    status, data = change("panel", {"on": ["claude", "local"]})
    assert status == 400 and "GPT writes the verdict" in data["error"]


def test_a_problem_the_file_already_has_doesnt_block_other_changes(config):
    config.write_text(CONFIG.replace('mode = "review"', 'mode = "saver"'), encoding="utf-8")  # by hand
    assert change("agent", {"effort": "high"}, agent="gpt")[0] == 200


def test_saver_shows_the_verifier_as_written_and_as_used(config):
    change("review", {"moderator": "gpt"})
    saver = snapshot()["saver"]
    assert saver["verifier"] == "gpt" and saver["verifier_set"] == "" and saver["drafters"] == []


def test_without_its_model_triage_still_doesnt_go_to_typesafe(config):
    with config.open("a", encoding="utf-8") as f:
        f.write('\n[triage]\nenabled = true\nagent = "gpt"\n')
    assert load_settings().triage.provider == "model"
    assert change("triage", {"agent": ""})[0] == 200
    assert load_settings().triage.provider == "model"


def test_which_models_see_pictures_can_be_switched_for_models_ixel_calls(config):
    agents = {a["name"]: a for a in snapshot()["agents"]}
    assert (agents["gpt"]["can_see"], agents["gpt"]["pictures"]) == (True, True)       # OpenAI: on unless turned off
    assert (agents["local"]["can_see"], agents["local"]["pictures"]) == (True, False)  # a local server: off
    assert (agents["claude"]["can_see"], agents["claude"]["pictures"]) == (False, False)  # Claude Code: never

    assert change("agent", {"pictures": True}, agent="local")[0] == 200
    assert loader.load_config()["agents"]["local"]["accepts"] == ["image"]
    assert change("agent", {"pictures": False}, agent="gpt")[0] == 200
    assert loader.load_config()["agents"]["gpt"]["accepts"] == []
    agents = {a["name"]: a for a in snapshot()["agents"]}
    assert agents["local"]["pictures"] and not agents["gpt"]["pictures"]


@pytest.mark.parametrize("agent, value", [("claude", True), ("gpt", "yes"), ("gpt", 1)])
def test_programs_never_see_pictures_and_the_switch_is_on_or_off(config, agent, value):
    status, data = change("agent", {"pictures": value}, agent=agent)
    assert status == 400 and data["error"]
    assert config.read_text(encoding="utf-8") == CONFIG


def test_the_sound_service_is_picked_and_its_keys_are_listed(config, monkeypatch):
    data = snapshot()
    keys = {k["name"]: k for k in data["keys"]}
    assert "Sound" in keys["GROQ_API_KEY"]["used_by"] and "Sound" in keys["OPENAI_API_KEY"]["used_by"]
    assert data["sound"]["using"] == "" and "OpenAI or Groq key" in data["sound"]["problem"]  # no key for either
    assert [p["name"] for p in data["choices"]["sound_providers"]] == ["openai", "groq"]

    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-the-shell")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-from-the-shell")
    assert snapshot()["sound"] == {"provider": "", "using": "OpenAI", "problem": ""}
    assert change("sound", {"provider": "groq"})[0] == 200
    assert loader.load_config()["sound"] == {"provider": "groq"}
    assert snapshot()["sound"] == {"provider": "groq", "using": "Groq", "problem": ""}
    assert change("sound", {"provider": ""})[0] == 200
    assert "provider" not in loader.load_config().get("sound", {})


@pytest.mark.parametrize("values", [{"provider": "whisper.cpp"}, {"groq_url": "http://evil.example"}])
def test_only_a_known_sound_service_can_be_picked(config, values):
    status, data = change("sound", values)
    assert status == 400 and data["error"]
    assert config.read_text(encoding="utf-8") == CONFIG


def test_a_service_named_oddly_in_the_file_still_reads(config):
    with config.open("a", encoding="utf-8") as f:
        f.write('\n[sound]\nprovider = ["groq"]\n\n[images]\nprovider = ["xai"]\n')
    data = snapshot()  # not a 500
    assert data["sound"]["provider"] == "" and data["images"]["provider"] == ""



# ── Models to pick from ───────────────────────────────────────────────────────

@pytest.fixture
def asked(monkeypatch):
    """The companies and model servers, answering from lists here; each call is recorded."""
    import urllib.error

    from ixel_mat.gui import model_choices
    model_choices._cache.clear()
    calls = []
    lists = {"openai": [{"id": "gpt-5.5", "created": 5}, {"id": "gpt-6-astra", "created": 8},
                        {"id": "gpt-4o-2024-08-06", "created": 1}, {"id": "gpt-6.1-sol", "created": 9}],
             "anthropic": [{"id": "claude-fable-5-1"}, {"id": "claude-opus-5-5"}]}

    def company(provider, key):
        calls.append((provider, key))
        if key == "refused":
            raise urllib.error.HTTPError("https://api.openai.com/v1/models", 401, "Unauthorized", {}, None)
        return lists[provider]

    def server(cfg):
        calls.append(("server", cfg.url))
        return [{"id": "llama3.3:latest"}, {"id": "qwen3"}, {"id": "--model x"}]

    monkeypatch.setattr(model_choices, "company_models", company)
    monkeypatch.setattr(model_choices, "server_models", server)
    yield calls
    model_choices._cache.clear()


def model_lists():
    [(status, data)] = call(("GET", "/api/settings/models"))
    assert status == 200
    return data["agents"]


def test_each_model_has_a_list_from_its_company_or_a_built_in_one(config, asked, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    lists = model_lists()
    gpt = lists["gpt"]
    assert (gpt["source"], gpt["where"], gpt["default"]) == ("company", "OpenAI", "gpt-6-astra")
    assert gpt["models"] == ["gpt-6.1-sol", "gpt-6-astra", "gpt-5.5"]  # newest first, no dated snapshot
    assert [(n["id"], n["now"]) for n in gpt["names"]] == [("latest", "gpt-6-astra"), ("latest-fast", "gpt-6.1-sol")]
    # Claude Code without an Anthropic key: the built-in list, and the short names it keeps up to date itself
    claude = lists["claude"]
    assert claude["source"] == "built_in" and "claude-opus-5-5" in claude["models"]
    assert [n["id"] for n in claude["names"]] == ["fable", "opus", "sonnet", "haiku"]
    assert "Save an Anthropic key under Keys" in claude["note"]
    # A model server of your own: what it has (never a name that could be a flag), and no Default
    local = lists["local"]
    assert (local["source"], local["default"], local["models"]) == ("server", None, ["llama3.3:latest", "qwen3"])
    assert SECRET not in json.dumps(lists) and ("openai", SECRET) in asked

    # Kept for a while; a new key is asked about at once
    before = len(asked)
    model_lists()
    assert len(asked) == before
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    claude = model_lists()["claude"]
    assert asked[before:] == [("anthropic", "sk-ant-test")]
    assert (claude["source"], claude["where"], claude["models"]) == ("company", "Anthropic",
                                                                    ["claude-fable-5-1", "claude-opus-5-5"])


def test_a_model_that_follows_the_newest_offers_the_levels_of_the_one_it_is_now(config, asked, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    assert "efforts" not in model_lists()["gpt"]  # a model named outright: the page's own levels hold
    asked_before = len(asked)
    assert change("agent", {"model": "latest"}, agent="gpt")[0] == 200
    gpt = model_lists()["gpt"]
    assert (gpt["follows"], gpt["resolved"]) == ("latest", "gpt-6-astra")
    assert gpt["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert change("agent", {"model": "latest-fast"}, agent="gpt")[0] == 200
    assert model_lists()["gpt"]["resolved"] == "gpt-6.1-sol"
    assert len(asked) == asked_before  # worked out from the list kept: picking a model asks OpenAI nothing more
    monkeypatch.delenv("OPENAI_API_KEY")  # can't ask: no levels claimed for a model nobody named
    assert "efforts" not in model_lists()["gpt"]


def test_a_key_the_company_refuses_shows_the_built_in_list_and_why(config, asked, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "refused")
    gpt = model_lists()["gpt"]
    assert (gpt["source"], gpt["default"]) == ("built_in", "") and "gpt-6-astra" in gpt["models"]
    assert "Ixel couldn't ask OpenAI for its models (it didn't accept the key)." == gpt["note"]
    monkeypatch.delenv("OPENAI_API_KEY")
    assert "There's no key for GPT yet" in model_lists()["gpt"]["note"]


def test_latest_is_only_for_a_company_api(config):
    for agent in ("claude", "local"):  # a program, and a server whose names say nothing about which is newest
        status, data = change("agent", {"model": "latest"}, agent=agent)
        assert status == 400 and "no list of newest models" in data["error"]
    assert change("agent", {"model": "latest-fast"}, agent="gpt")[0] == 200
    assert change("agent", {"model": "opus"}, agent="claude")[0] == 200
    assert loader.tomllib.loads(config.read_text(encoding="utf-8"))["agents"]["claude"]["model"] == "opus"
    # ...and a model server of your own has no default to go back to
    assert [a["needs_model"] for a in snapshot()["agents"]] == [False, False, True]
    status, data = change("agent", {"model": ""}, agent="local")
    assert status == 400 and "needs a model name" in data["error"]
    assert change("agent", {"model": ""}, agent="gpt")[0] == 200


def test_one_odd_model_never_takes_the_others_lists_with_it(config, asked, monkeypatch):
    config.write_text(CONFIG + '\n[agents.odd]\ntype = "oneshot"\ncommand = 5\nlabel = "Odd"\n', encoding="utf-8")
    from ixel_mat.gui import model_choices
    real = model_choices.agent_choices
    monkeypatch.setattr(model_choices, "agent_choices",
                        lambda cfg, raw: 1 / 0 if cfg.name == "claude" else real(cfg, raw))
    lists = model_lists()
    assert lists["local"]["source"] == "server"
    assert lists["claude"]["source"] == "none" and "couldn't work out this model's list" in lists["claude"]["note"]


@pytest.mark.skipif(os.name != "posix", reason="a shell script stands in for opencode")
def test_opencode_lists_models_for_its_scratch_folder_not_ixels(tmp_path, monkeypatch):
    # OpenCode 2 works in the folder PWD names: Ixel's own (its opencode.json, its plugins) stays out of it
    from ixel_mat.agents.base import AgentConfig
    from ixel_mat.gui import model_choices
    monkeypatch.setenv("PWD", str(tmp_path))
    fake = tmp_path / "opencode"
    fake.write_text(f"#!{sys.executable}\nimport os, sys\n"  # (a shell would set PWD itself)
                    "sys.exit(4) if os.path.realpath(os.environ['PWD']) != os.path.realpath('.') else None\n"
                    "print('anthropic/claude-opus-5-5')\n")
    fake.chmod(0o755)
    cfg = AgentConfig(name="opencode", label="OpenCode", type="oneshot", command=str(fake),
                      model_args=["-m", "{model}"])
    assert model_choices.agent_choices(cfg, {"preset": "opencode"})["models"] == ["anthropic/claude-opus-5-5"]


@pytest.mark.skipif(os.name != "posix", reason="a shell script stands in for opencode")
def test_opencode_lists_its_own_models(tmp_path, monkeypatch):
    import time

    from ixel_mat.agents.base import AgentConfig
    from ixel_mat.gui import model_choices
    fake = tmp_path / "opencode"
    # ...from what it knows: it isn't sent off to fetch the models.dev catalog first
    fake.write_text('#!/bin/sh\n[ "$1" = models ] || exit 3\n[ "$OPENCODE_DISABLE_MODELS_FETCH" = 1 ] || exit 4\n'
                    'printf "anthropic/claude-opus-5-5\\n\\033[2mlmstudio/qwen3\\033[0m\\nnot a model\\n"\n')
    fake.chmod(0o755)
    cfg = AgentConfig(name="opencode", label="OpenCode", type="oneshot", command=str(fake),
                      model_args=["-m", "{model}"])
    out = model_choices.agent_choices(cfg, {"preset": "opencode"})
    assert (out["source"], out["models"]) == ("program", ["anthropic/claude-opus-5-5", "lmstudio/qwen3"])
    # One that fails says why (never with a key in it), and Other… is left for a name
    fake.write_text('#!/bin/sh\necho "ERROR Unrecognized command for sk-proj-abcdefghijklmnopqrstuvwxyz0123" >&2\nexit 1\n')
    out = model_choices.agent_choices(cfg, {"preset": "opencode"})
    assert out["source"] == "none" and "exited with code 1: ERROR Unrecognized command" in out["note"]
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz0123" not in out["note"]
    # One that hangs is stopped, with what it started
    fake.write_text("#!/bin/sh\nsleep 30 &\nsleep 30\n")
    monkeypatch.setattr(model_choices, "PROGRAM_TIMEOUT", 1)
    started = time.monotonic()
    out = model_choices.agent_choices(cfg, {"preset": "opencode"})
    assert time.monotonic() - started < 10 and "didn't answer within 1 seconds" in out["note"]
    # A program that can't take a model from Ixel isn't asked
    cfg.model_args = None
    assert "isn't set up to take a model" in model_choices.agent_choices(cfg, {"preset": "opencode"})["note"]
    # Nor is a command that only starts OpenCode: npx would read "models" as a package to fetch and run
    npx = tmp_path / "npx"
    npx.write_text(f"#!/bin/sh\ntouch {tmp_path / 'ran'}\n")
    npx.chmod(0o755)
    cfg.command, cfg.model_args = str(npx), ["-m", "{model}"]
    out = model_choices.agent_choices(cfg, {"preset": "opencode"})
    assert out["source"] == "none" and not (tmp_path / "ran").exists()


@pytest.mark.skipif(os.name != "posix", reason="a script stands in for opencode")
def test_opencode_2_lists_through_a_server_of_its_own_never_your_background_service(tmp_path, monkeypatch):
    # OpenCode 2's `models` would ask your background service, which runs with your settings (and fetches OpenCode's
    # catalog): Ixel starts a private one with a made-up password, kept off command lines, and ends it after
    from ixel_mat.agents.base import AgentConfig
    from ixel_mat.gui import model_choices
    log, pid, count = tmp_path / "calls", tmp_path / "pid", tmp_path / "count"
    fake = tmp_path / "opencode"
    fake.write_text(f"""#!{sys.executable}
import os, sys, time
args, env = sys.argv[1:], os.environ
password = env.get("OPENCODE_SERVER_PASSWORD", "")
with open({str(log)!r}, "a") as f:
    f.write(repr((args, env.get("OPENCODE_DISABLE_MODELS_FETCH"), bool(password))) + "\\n")
if args == ["--version"]:
    sys.exit(print("opencode v2.0.22"))
if password and any(password in a for a in args):
    sys.exit(7)
if args[:1] == ["serve"]:
    open({str(pid)!r}, "w").write(str(os.getpid()))
    print("a plugin: server listening on http://10.255.255.1\\\\@127.0.0.1:9", flush=True)  # never the password's way
    print("INFO  server listening on http://127.0.0.1:4096", flush=True)
    time.sleep(60)
if args[:1] == ["models"]:
    if "--server" not in args:
        sys.exit(9)
    n = int(open({str(count)!r}).read()) if os.path.exists({str(count)!r}) else 0
    open({str(count)!r}, "w").write(str(n + 1))
    if n and os.environ.get("NEVER_LISTS") != "1":
        print("anthropic/claude-opus-5-5\\nollama/qwen3")
""")
    fake.chmod(0o755)
    cfg = AgentConfig(name="opencode", label="OpenCode", type="oneshot", command=str(fake),
                      model_args=["-m", "{model}"], env={"OPENCODE_DISABLE_MODELS_FETCH": "0"})
    out = model_choices.agent_choices(cfg, {"preset": "opencode"})
    assert (out["source"], out["models"]) == ("program", ["anthropic/claude-opus-5-5", "ollama/qwen3"]), out["note"]
    calls = [eval(line) for line in log.read_text().splitlines()]  # noqa: S307 — written by the script above
    lists = [args for args, _, _ in calls if args[:1] == ["models"]]
    assert set(map(tuple, lists)) == {("models", "--server", "http://127.0.0.1:4096")}
    assert len(lists) >= 3  # empty while it loads, then the same list for STABLE seconds
    assert ["serve", "--hostname", "127.0.0.1", "--port", "0"] in [args for args, _, _ in calls]
    assert all(flag == "1" for _, flag, _ in calls)  # and never the catalog, whatever the agent's env said
    assert all(password for args, _, password in calls if args != ["--version"])
    assert not any(a in ("--refresh", "auth", "login", "providers") for args, _, _ in calls for a in args)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)  # the private server is gone
    # One that never lists anything is given SETTLE seconds, and says so
    monkeypatch.setenv("NEVER_LISTS", "1")
    monkeypatch.setattr(model_choices, "SETTLE", 1)
    out = model_choices.agent_choices(cfg, {"preset": "opencode"})
    assert out["source"] == "none" and "(it listed none)" in out["note"]


def test_each_model_offers_only_the_effort_levels_it_takes(config):
    config.write_text(CONFIG + """
[agents.haiku]
type = "http"
url = "https://api.anthropic.com/v1/messages"
label = "Haiku"
model = "claude-haiku-4-5"
effort = "high"

[agents.gem]
preset = "gemini_cli"

[agents.codex]
preset = "codex"
effort = "minimal"
""", encoding="utf-8")
    agents = {a["name"]: a for a in snapshot()["agents"]}
    assert agents["claude"]["efforts"] == ["low", "medium", "high", "xhigh", "max"]   # what Claude Code takes
    assert agents["gpt"]["efforts"] == ["minimal", "low", "medium", "high"]           # GPT-5
    assert agents["local"]["efforts"] == ["minimal", "low", "medium", "high"]
    # Nothing to pick where nothing would be sent, and the page says why
    assert not agents["haiku"]["has_effort"] and agents["haiku"]["effort_note"] == \
        "claude-haiku-4-5 has no effort setting, so Ixel sends none."
    assert not agents["gem"]["has_effort"] and "Gemini CLI doesn't take an effort level" in agents["gem"]["effort_note"]
    # Codex passes it to a GPT-6 model, which has no minimal: what's sent instead is shown
    codex = agents["codex"]
    assert (codex["efforts"], codex["effort"], codex["effort_sent"]) == (["low", "medium", "high", "xhigh"],
                                                                        "minimal", "low")
    # A new model brings its own levels with the page's next look
    status, data = change("agent", {"model": "gpt-6.1-sol"}, agent="gpt")
    assert status == 200
    gpt = next(a for a in data["settings"]["agents"] if a["name"] == "gpt")
    assert gpt["efforts"] == ["low", "medium", "high", "xhigh", "max"]


def test_a_server_that_isnt_on_your_network_is_named_by_its_address(config, asked, monkeypatch):
    from ixel_mat.gui import model_choices
    monkeypatch.setattr(model_choices, "server_models", lambda cfg: [{"id": "deepseek/deepseek-r2"}])
    config.write_text(CONFIG + """
[agents.router]
type = "http"
url = "https://openrouter.ai/api/v1/chat/completions"
label = "Router"
model = "deepseek/deepseek-r2"
""", encoding="utf-8")
    lists = model_lists()
    assert (lists["router"]["where"], lists["router"]["models"]) == ("openrouter.ai", ["deepseek/deepseek-r2"])
    assert lists["local"]["where"] == "your model server"
    assert model_choices._server_name("http://100.101.1.2:8080/v1/chat/completions") == "your model server"  # Tailscale
    assert model_choices._server_name("http://gpu-box:11434/v1/chat/completions") == "your model server"
