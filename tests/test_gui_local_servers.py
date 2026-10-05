"""Settings and model servers on your own computers: looking for them, adding a model, moving one, taking one off."""
import sys

import pytest

from ixel_mat import local_models
from ixel_mat.config import edit, loader
from ixel_mat.gui import settings_api
from ixel_mat.runtime import load_settings
from test_gui_settings import CONFIG, call, change, snapshot

MAC = "http://mac-mini:1234/v1"


@pytest.fixture
def config():
    path = loader._GLOBAL_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIG, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def servers(monkeypatch):
    """The Mac's LM Studio and this computer's Ollama, as asked; example.com isn't yours."""
    have = {MAC: local_models.Server("LM Studio", MAC, ["qwen3:14b", "gemma3:12b"], ["text-embedding-nomic"]),
            "http://127.0.0.1:11434/v1": local_models.Server("Ollama", "http://127.0.0.1:11434/v1",
                                                             ["llama3:latest", "qwen3:8b"], [], ollama=True)}
    asked = []

    def ask_server(name, base, timeout=1.5):
        asked.append(base)
        return have.get(base)

    def where_is(host, resolve=None):
        return {"mac-mini": "yours", "127.0.0.1": "yours", "mac-mini.fritz.box": "yours",
                "example.com": "elsewhere"}.get(host, "unknown")

    monkeypatch.setattr(local_models, "ask_server", ask_server)
    monkeypatch.setattr(local_models, "where_is", where_is)
    monkeypatch.setattr(local_models, "on_this_computer", lambda timeout=1.5: [have["http://127.0.0.1:11434/v1"]])
    return asked


def add(base, model, version=None):
    body = {"section": "add_model", "values": {"base": base, "model": model},
            "version": snapshot()["version"] if version is None else version}
    [(status, data)] = call(("POST", "/api/settings", body))
    return status, data


def remove(agent):
    [(status, data)] = call(("POST", "/api/settings", {"section": "remove_model", "agent": agent,
                                                       "version": snapshot()["version"]}))
    return status, data


def look(body):
    [(status, data)] = call(("POST", "/api/settings/servers", body))
    return status, data


# ── Taking a whole table out of the file ──────────────────────────────────────

def test_a_table_and_the_tables_inside_it_come_out_and_nothing_else():
    text = ('# mine\n[agents.a]\ntype = "http"\n\n[agents.a.env]\nX = "1"\n\n# about b\n[agents.b]\nlabel = "B"\n')
    assert edit.remove_table(text, ("agents", "a")) == '# mine\n\n# about b\n[agents.b]\nlabel = "B"\n'
    assert edit.remove_table(text, ("agents", "zzz")) == text
    last = '[agents.a]\ntype = "http"\n\n[agents.b]\nlabel = "B"\n\n# Ollama on the Mac: ixel setup finds it\n'
    assert edit.remove_table(last, ("agents", "b")) == \
        '[agents.a]\ntype = "http"\n\n\n# Ollama on the Mac: ixel setup finds it\n'  # a note at the end stays
    with pytest.raises(edit.EditError):
        edit.remove_table('agents = { a = { type = "http" } }\n', ("agents", "a"))


# ── What the page shows ───────────────────────────────────────────────────────

def test_only_a_keyless_model_on_your_own_computers_shows_its_server(config):
    agents = {a["name"]: a for a in snapshot()["agents"]}
    assert agents["local"]["server"] == {"url": "http://127.0.0.1:11434/v1", "where": "this computer"}
    assert agents["gpt"]["server"] is None and agents["claude"]["server"] is None
    config.write_text(CONFIG.replace('label = "Llama"', 'label = "Llama"\ntoken_env = "OPENAI_API_KEY"'),
                      encoding="utf-8")
    assert {a["name"]: a for a in snapshot()["agents"]}["local"]["server"] is None  # given a key: not here


def test_looking_finds_this_computers_servers_and_what_is_already_on_your_list(config):
    status, data = look({})
    assert status == 200
    [server] = data["servers"]
    assert (server["name"], server["where"], server["ollama"]) == ("Ollama", "this computer", True)
    assert server["added"] == {"llama3:latest": "local", "qwen3:8b": ""}  # llama3 is llama3:latest


def test_looking_at_an_address_says_why_when_it_cant(config, monkeypatch):
    def at_address(text):
        raise local_models.AddressError("Nothing answered at mac-mini on the usual ports.")
    monkeypatch.setattr(local_models, "at_address", at_address)
    assert look({"address": "mac-mini"}) == (404, {"error": "Nothing answered at mac-mini on the usual ports."})
    assert look({"address": 5})[0] == 400
    monkeypatch.setattr(local_models, "on_this_computer", lambda timeout=1.5: [])
    status, data = look({})
    assert status == 404 and "No model server is running on this computer" in data["error"]


# ── Adding ────────────────────────────────────────────────────────────────────

def test_a_model_on_another_computer_is_added_with_no_key_and_joins_the_panel(config, servers):
    status, data = add(MAC, "qwen3:14b")
    assert status == 200 and data["message"] == "Added qwen3:14b (mac-mini). It's on the panel."
    assert servers[-1] == MAC  # asked again when saving, not taken from the page
    settings = load_settings()
    cfg = settings.agent_configs["qwen3_14b"]
    assert (cfg.type, cfg.url, cfg.model, cfg.label, cfg.token) == (
        "http", "http://mac-mini:1234/v1/chat/completions", "qwen3:14b", "qwen3:14b (mac-mini)", "")
    assert settings.review.agents == ["claude", "gpt", "qwen3_14b"]
    raw = settings.config["agents"]["qwen3_14b"]
    assert not any(k in raw for k in ("token_env", "token", "env", "command"))
    text = config.read_text(encoding="utf-8")
    assert "# My Ixel settings" in text and "# Pictures" in text  # the rest as written
    assert add(MAC, "qwen3:14b")[1]["error"] == "qwen3:14b on mac-mini is already on your list."


@pytest.mark.parametrize("base, model, status, words", [
    ("http://example.com:1234/v1", "qwen3:14b", 400, "isn't on this computer or your own network"),
    ("http://mac-mini.fritz.box:1234/v1", "qwen3:14b", 400, "Use its address"),
    ("http://mac-mini:9999/v1", "qwen3:14b", 404, "No model server answered"),
    (MAC, "text-embedding-nomic", 404, "has no model called text-embedding-nomic"),
    (MAC, "--flag", 400, "isn't a model name"),
    ("mac-mini", "qwen3:14b", 400, "Say which port"),
    (5, "qwen3:14b", 400, "Look for it first"),
])
def test_a_model_is_added_only_from_a_server_of_yours_that_has_it(config, base, model, status, words):
    before = config.read_text(encoding="utf-8")
    got, data = add(base, model)
    assert got == status and words in data["error"]
    assert config.read_text(encoding="utf-8") == before


def test_a_server_that_doesnt_answer_says_what_letting_others_in_means(config):
    got, data = add("http://mac-mini:9999/v1", "qwen3:14b")
    assert got == 404 and data["error"].endswith(local_models.OPEN_TO_NETWORK)
    # On this computer, letting other computers in has nothing to do with it
    got, data = add("http://127.0.0.1:9/v1", "qwen3:14b")
    assert got == 404 and data["error"].endswith("Is it running?")


def test_a_panel_left_to_every_model_stays_that_way(config):
    config.write_text(CONFIG.replace('agents = [\n  "claude",\n  "gpt",  # not the local one\n]', "agents = []"),
                      encoding="utf-8")
    assert load_settings().config["review"]["agents"] == []
    assert add(MAC, "qwen3:14b")[0] == 200
    settings = load_settings()
    assert settings.config["review"]["agents"] == [] and "qwen3_14b" in settings.panel_configs()


def test_the_first_model_added_in_the_app_starts_the_settings_file():
    assert not loader._GLOBAL_CONFIG.exists()
    status, data = add("http://127.0.0.1:11434/v1", "qwen3:8b", version="")
    assert status == 200 and data["settings"]["editable"]
    settings = load_settings()
    assert list(settings.agent_configs) == ["qwen3_8b"] and settings.agent_configs["qwen3_8b"].label == "qwen3:8b (local)"
    assert loader._GLOBAL_CONFIG.read_text(encoding="utf-8").startswith(settings_api.NEW_FILE)
    assert not loader._GLOBAL_CONFIG.with_name("config.toml.bak").exists()


def test_a_first_model_that_cant_be_added_leaves_no_file_behind():
    assert add("http://127.0.0.1:11434/v1", "nope", version="")[0] == 404
    assert not loader._GLOBAL_CONFIG.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="making a link needs Developer Mode on Windows")
def test_a_link_to_a_settings_file_that_isnt_there_is_left_alone():
    loader._GLOBAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    loader._GLOBAL_CONFIG.symlink_to(loader._GLOBAL_CONFIG.with_name("elsewhere.toml"))
    status, data = add("http://127.0.0.1:11434/v1", "qwen3:8b", version="")
    assert status == 400 and "a link to a file that isn't there" in data["error"]
    assert not loader._GLOBAL_CONFIG.with_name("elsewhere.toml").exists()


def test_a_settings_file_written_meanwhile_isnt_written_over(monkeypatch):
    """ixel setup saving at the same moment wins; the page is told to read it again."""
    real = settings_api.os.path.lexists

    def setup_writes_now(path):
        seen = real(path)
        loader._GLOBAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        loader._GLOBAL_CONFIG.write_text(CONFIG, encoding="utf-8")
        return seen

    monkeypatch.setattr(settings_api.os.path, "lexists", setup_writes_now)
    status, data = add("http://127.0.0.1:11434/v1", "qwen3:8b", version="")
    assert status == 409 and "changed since this page read it" in data["error"]
    assert loader._GLOBAL_CONFIG.read_text(encoding="utf-8") == CONFIG


# ── Moving and taking off ─────────────────────────────────────────────────────

def test_a_model_server_can_move_to_another_of_your_computers(config):
    # Only to one that has its model
    status, data = change("agent", {"url": "mac-mini:1234"}, agent="local")
    assert status == 404 and "LM Studio at mac-mini has no model called llama3" in data["error"]
    assert load_settings().agent_configs["local"].url == "http://127.0.0.1:11434/v1/chat/completions"
    config.write_text(CONFIG.replace('model = "llama3"', 'model = "qwen3:14b"'), encoding="utf-8")
    status, _ = change("agent", {"url": "mac-mini:1234"}, agent="local")
    assert status == 200
    assert load_settings().agent_configs["local"].url == "http://mac-mini:1234/v1/chat/completions"
    assert load_settings().agent_configs["local"].label == "Llama"  # a name of yours stays
    status, data = change("agent", {"url": "example.com:1234"}, agent="local")
    assert status == 400 and "own network" in data["error"]
    status, data = change("agent", {"url": "http://127.0.0.1:11434/v1"}, agent="gpt")
    assert status == 400 and "can't be changed here" in data["error"]
    assert load_settings().agent_configs["gpt"].url == "https://api.openai.com/v1/chat/completions"


def test_the_name_ixel_gave_a_model_follows_it_to_another_computer(config, monkeypatch):
    assert add("http://127.0.0.1:11434/v1", "qwen3:8b")[0] == 200
    mac = local_models.Server("LM Studio", MAC, ["qwen3:8b"], [])
    monkeypatch.setattr(local_models, "ask_server", lambda name, base, timeout=1.5: mac if base == MAC else None)
    assert change("agent", {"url": "mac-mini:1234"}, agent="qwen3_8b")[0] == 200
    cfg = load_settings().agent_configs["qwen3_8b"]
    assert (cfg.url, cfg.label) == ("http://mac-mini:1234/v1/chat/completions", "qwen3:8b (mac-mini)")


def test_a_model_server_can_be_taken_off_and_leaves_the_lists(config):
    config.write_text(CONFIG.replace('"gpt",  # not the local one', '"gpt", "local",') +
                      '\n[saver]\nverifier = "gpt"\ndrafters = ["local", "claude"]\n'
                      '\n[triage]\nenabled = true\nprovider = "typesafe"\nagent = "local"\n', encoding="utf-8")
    status, data = remove("local")
    assert status == 200 and data["message"] == "Took Llama off your list."
    settings = load_settings()
    assert "local" not in settings.agent_configs and settings.review.agents == ["claude", "gpt"]
    assert settings.config["saver"]["drafters"] == ["claude"] and settings.saver.verifier == "gpt"
    assert "agent" not in settings.config["triage"] and settings.triage.enabled  # named only; TypeSafe decides


def test_a_triage_model_named_but_not_deciding_goes_without_turning_on_typesafe(config):
    config.write_text(CONFIG + '\n[triage]\nenabled = false\nagent = "local"\n', encoding="utf-8")
    assert remove("local")[0] == 200
    settings = load_settings()
    assert settings.config["triage"] == {"enabled": False, "provider": "model"} and settings.triage.provider == "model"


@pytest.mark.parametrize("panel, tables, words", [
    ('agents = ["local"]\n', "", "Llama is the only model on the panel."),
    ('agents = ["local", "gone"]\n', "", "Llama is the only model on the panel."),  # gone isn't a model of yours
    ("", '[saver]\nverifier = "gpt"\ndrafters = ["local"]\n', "Llama is the only model drafting for Saver."),
    ("", '[saver]\nverifier = "gpt"\ndrafters = ["local", "gpt"]\n', "Llama is the only model drafting"),
    ("", '[triage]\nenabled = true\nprovider = "model"\nagent = "local"\n', "Llama makes Triage's decisions."),
])
def test_the_last_model_on_a_list_isnt_taken_off_since_an_empty_list_means_every_model(config, panel, tables, words):
    """Taking the only local model off a panel would hand every question to the companies' models."""
    text = CONFIG.replace('agents = [\n  "claude",\n  "gpt",  # not the local one\n]\n', panel) + "\n" + tables
    config.write_text(text, encoding="utf-8")
    status, data = remove("local")
    assert status == 400 and words in data["error"]
    assert config.read_text(encoding="utf-8") == text


def test_only_a_model_server_of_yours_is_taken_off_here_and_not_while_it_has_a_job(config):
    assert "taken off with ixel setup" in remove("gpt")[1]["error"]
    config.write_text(CONFIG.replace('"gpt",  # not the local one', '"gpt", "local",'), encoding="utf-8")
    assert change("review", {"moderator": "local"})[0] == 200
    status, data = remove("local")
    assert status == 400 and data["error"] == "Llama writes the verdict. Pick another model for that first."
    assert "local" in load_settings().agent_configs


# ── Getting a model through Ollama ────────────────────────────────────────────

@pytest.mark.parametrize("name", ["qwen3:8b", "llama3.2", "library/qwen3:14b", "hf.co/bartowski/Qwen3-8B-GGUF:Q4_K_M"])
def test_a_model_name_ollama_knows_can_be_got(name):
    assert local_models.model_to_get(f" {name} ") == name


@pytest.mark.parametrize("name, words", [
    ("", "like qwen3:8b"), ("qwen3 8b", "like qwen3:8b"), ("--insecure", "like qwen3:8b"), ("../x", "like qwen3:8b"),
    ("a/../b", "like qwen3:8b"), (5, "like qwen3:8b"), ("gpt-oss:120b-cloud", "runs on ollama.com"),
])
def test_a_name_that_isnt_one_or_runs_elsewhere_isnt_asked_for(name, words):
    with pytest.raises(local_models.PullError, match=words):
        local_models.model_to_get(name)


def fake_ollama(lines, status=200):
    """An Ollama whose /api/pull answers with these lines; returns (start, what it was asked)."""
    import asyncio as aio
    from aiohttp import web
    asked = []

    async def pulled(request):
        asked.append(await request.json())
        if status != 200:
            return web.json_response({"error": "pull model manifest: file does not exist"}, status=status)
        resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(request)
        for line in lines:
            await resp.write((line + "\n").encode())
            await aio.sleep(0)
        return resp

    async def start():
        app = web.Application()
        app.router.add_post("/api/pull", pulled)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    return start, asked


def pull_all(lines, status=200):
    import asyncio as aio
    start, asked = fake_ollama(lines, status)

    async def go():
        runner, root = await start()
        try:
            return [step async for step in local_models.pull(root, "qwen3:8b")]
        finally:
            await runner.cleanup()
    return aio.run(go()), asked


def test_ollama_says_how_far_along_it_is():
    steps, asked = pull_all(['{"status":"pulling manifest"}',
                             '{"status":"pulling a1b2","digest":"sha256:a1b2","total":5200000000,"completed":2600000000}',
                             'not json', '{"status":"success"}'])
    assert asked == [{"model": "qwen3:8b", "name": "qwen3:8b", "stream": True}]
    assert steps == [{"status": "pulling manifest", "completed": 0, "total": 0},
                     {"status": "pulling a1b2", "completed": 2600000000, "total": 5200000000},
                     {"status": "success", "completed": 0, "total": 0}]


def test_ollama_saying_no_is_passed_on():
    with pytest.raises(local_models.PullError, match="file does not exist"):
        pull_all([], status=500)
    with pytest.raises(local_models.PullError, match="Ollama couldn't get qwen3:8b: out of disk"):
        pull_all(['{"status":"pulling manifest"}', '{"error":"out of disk"}'])
    with pytest.raises(local_models.PullError, match="stopped before qwen3:8b was ready"):  # never said success
        pull_all(['{"status":"pulling manifest"}'])


def get(base, model):
    [(status, data)] = call(("POST", "/api/settings/servers/pull", {"base": base, "model": model}))
    return status, data


@pytest.mark.parametrize("base, model, status, words", [
    (MAC, "qwen3:8b", 400, "isn't Ollama, so Ixel can't get models for it by name"),
    ("http://example.com:11434/v1", "qwen3:8b", 400, "isn't on this computer or your own network"),
    ("http://127.0.0.1:11434/v1", "gpt-oss:120b-cloud", 400, "runs on ollama.com"),
    ("http://127.0.0.1:11434/v1", "--flag", 400, "like qwen3:8b"),
])
def test_a_model_is_got_only_by_an_ollama_of_yours(config, monkeypatch, base, model, status, words):
    async def never(root, name):
        raise AssertionError("asked Ollama anyway")
        yield
    monkeypatch.setattr(local_models, "pull", never)
    got, data = get(base, model)
    assert got == status and words in data["error"]


def test_getting_a_model_streams_how_far_along_it_is(config, monkeypatch):
    import asyncio as aio
    import json as js
    from test_gui_server import JSON_AUTH, run_with_client
    from test_gui_settings import gui
    pulled = []

    async def pull(root, name):
        pulled.append((root, name))
        yield {"status": "pulling a1b2", "completed": 1, "total": 2}
        await aio.sleep(0.3)
        yield {"status": "success", "completed": 0, "total": 0}

    monkeypatch.setattr(local_models, "pull", pull)

    async def scenario(client):
        resp = await client.post("/api/settings/servers/pull", headers=JSON_AUTH,
                                 data=js.dumps({"base": "http://127.0.0.1:11434/v1", "model": "qwen3:8b"}))
        return [js.loads(line) for line in (await resp.text()).splitlines() if line.strip()]

    events = run_with_client(gui(), scenario)
    assert pulled == [("http://127.0.0.1:11434", "qwen3:8b")]
    assert [e["kind"] for e in events] == ["progress", "progress", "done"]
    assert events[0]["data"] == {"status": "pulling a1b2", "completed": 1, "total": 2}
    assert events[-1]["data"] == {"model": "qwen3:8b"}


def test_a_new_step_shows_at_once_and_more_of_the_same_a_few_times_a_second(config, monkeypatch):
    import json as js
    from test_gui_server import JSON_AUTH, run_with_client
    from test_gui_settings import gui

    async def pull(root, name):
        for n in range(50):
            yield {"status": "pulling a1b2", "completed": n, "total": 50}
        yield {"status": "verifying sha256 digest", "completed": 0, "total": 0}
        yield {"status": "success", "completed": 0, "total": 0}

    monkeypatch.setattr(local_models, "pull", pull)

    async def scenario(client):
        resp = await client.post("/api/settings/servers/pull", headers=JSON_AUTH,
                                 data=js.dumps({"base": "http://127.0.0.1:11434/v1", "model": "qwen3:8b"}))
        return [js.loads(line) for line in (await resp.text()).splitlines() if line.strip()]

    events = run_with_client(gui(), scenario)
    assert [e["data"].get("status") for e in events] == ["pulling a1b2", "verifying sha256 digest", "success", None]


@pytest.mark.parametrize("trouble, words", [
    (TimeoutError(), "Lost touch with Ollama while it got qwen3:8b (TimeoutError)"),
    (ValueError("Chunk too big"), "Ollama's answer couldn't be read while it got qwen3:8b (ValueError)"),
])
def test_trouble_reading_ollama_is_said_plainly(config, monkeypatch, trouble, words):
    import json as js
    from test_gui_server import JSON_AUTH, run_with_client
    from test_gui_settings import gui

    async def pull(root, name):
        yield {"status": "pulling a1b2", "completed": 1, "total": 2}
        raise trouble

    monkeypatch.setattr(local_models, "pull", pull)

    async def scenario(client):
        resp = await client.post("/api/settings/servers/pull", headers=JSON_AUTH,
                                 data=js.dumps({"base": "http://127.0.0.1:11434/v1", "model": "qwen3:8b"}))
        return [js.loads(line) for line in (await resp.text()).splitlines() if line.strip()]

    events = run_with_client(gui(), scenario)
    assert events[-1]["kind"] == "error" and words in events[-1]["data"]["message"]


def test_closing_the_page_stops_a_pull_even_while_ollama_says_nothing(config, monkeypatch):
    """As the app runs (a closed page doesn't cancel the handler): noticed within a moment, not at Ollama's
    next line, so the download stops and another can start."""
    import asyncio as aio
    import aiohttp
    from aiohttp import web
    from test_gui_server import JSON_AUTH
    from test_gui_settings import gui
    stopped = aio.Event()

    async def pull(root, name):
        try:
            yield {"status": "verifying sha256 digest", "completed": 0, "total": 0}
            await aio.sleep(3600)  # checking a big file: not a word
        finally:
            stopped.set()

    monkeypatch.setattr(local_models, "pull", pull)
    body = {"base": "http://127.0.0.1:11434/v1", "model": "qwen3:8b"}

    async def go():
        server = gui()
        runner = web.AppRunner(server.app(), access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        server.port = site._server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{server.port}/api/settings/servers/pull"
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(url, json=body, headers=JSON_AUTH)
                assert b"verifying" in await resp.content.readline()
                resp.close()  # the page closes
            await aio.wait_for(stopped.wait(), 3)
            stopped.clear()
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=body, headers=JSON_AUTH) as again:
                    assert again.status == 200  # not "already getting a model"
        finally:
            await runner.cleanup()

    aio.run(go())
