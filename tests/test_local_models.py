"""Model servers of your own: addresses, finding them, and which of their models can answer."""
import asyncio
import socket
from types import SimpleNamespace

import pytest
from aiohttp import web

from fake_providers import FakeProvider
from ixel_mat import local_models
from ixel_mat.agents.base import AgentConfig
from ixel_mat.config.loader import build_agent_configs
from ixel_mat.usage import billing_for


# ── Addresses ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("given, used", [
    ("http://localhost:1234/v1", "http://localhost:1234/v1/chat/completions"),
    ("http://localhost:1234/v1/", "http://localhost:1234/v1/chat/completions"),
    ("http://127.0.0.1:11434", "http://127.0.0.1:11434/v1/chat/completions"),
    ("http://mac-mini:1234/", "http://mac-mini:1234/v1/chat/completions"),
    ("http://127.0.0.1:11434/v1/chat/completions", "http://127.0.0.1:11434/v1/chat/completions"),
    ("http://gpu:8000/openai/v1", "http://gpu:8000/openai/v1/chat/completions"),
    ("http://gpu:8000/api/generate", "http://gpu:8000/api/generate"),      # not an address Ixel can complete
    ("https://api.openai.com/v1", "https://api.openai.com/v1"),           # a company's API is left as written
    ("https://api.anthropic.com/v1/messages", "https://api.anthropic.com/v1/messages"),
    ("http://192.168.1.20:1234", "http://192.168.1.20:1234/v1/chat/completions"),
    ("https://mac-mini.tail1.ts.net/v1", "https://mac-mini.tail1.ts.net/v1/chat/completions"),
    ("https://llm.example.com", "https://llm.example.com"),               # only a server of yours is guessed at
    ("http://example.com", "http://example.com"),
    ("not a url", "not a url"),
])
def test_a_model_server_address_as_its_app_shows_it_takes_questions(given, used):
    assert local_models.chat_url(given) == used


def test_settings_with_a_bare_v1_address_ask_the_chat_address():
    configs, warnings = build_agent_configs({"agents": {"lm": {
        "type": "http", "url": "http://127.0.0.1:1234/v1", "model": "qwen3", "label": "LM"}}})
    assert configs["lm"].url == "http://127.0.0.1:1234/v1/chat/completions" and not warnings


@pytest.mark.parametrize("name", ["nomic-embed-text:latest", "text-embedding-nomic-embed-text-v1.5", "mxbai-embed-large",
                                  "bge-m3", "all-minilm:l6-v2", "whisper-1", "kokoro-tts", "jina-reranker-v2",
                                  "snowflake-arctic-embed2", "granite-embedding:278m", "intfloat/e5-large"])
def test_models_that_cant_answer_are_left_out(name):
    assert not local_models.is_chat_model(name)


@pytest.mark.parametrize("name", ["llama3.2", "qwen3:14b", "qwen2.5-coder:7b", "gemma3n:e4b", "gpt-oss:20b",
                                  "deepseek-r1:8b", "mistral-small3.2", "qwen/qwen3-vl-8b", "phi4-mini"])
def test_chat_models_stay(name):
    assert local_models.is_chat_model(name)


@pytest.mark.parametrize("typed, tried", [
    ("mac-mini:1234", [("LM Studio", "http://mac-mini:1234/v1")]),
    ("http://192.168.1.20:11434", [("Ollama", "http://192.168.1.20:11434/v1")]),
    ("http://mac-mini:1234/v1/chat/completions", [("LM Studio", "http://mac-mini:1234/v1")]),
    ("100.101.102.103:9999", [("Model server", "http://100.101.102.103:9999/v1")]),
    ("[fd7a::1]:1234", [("LM Studio", "http://[fd7a::1]:1234/v1")]),
    ("mac-mini.tail1.ts.net:11434", [("Ollama", "http://mac-mini.tail1.ts.net:11434/v1")]),
    ("nas.fritz.box:1234", [("LM Studio", "http://nas.fritz.box:1234/v1")]),
    ("https://mac-mini", [("Model server", "https://mac-mini/v1")]),
    ("https://gpu.tail1.ts.net/openai/v1", [("Model server", "https://gpu.tail1.ts.net/openai/v1")]),
])
def test_an_address_with_a_port_is_tried_there(typed, tried):
    assert local_models.addresses_to_try(typed) == tried


def test_an_ipv6_address_can_be_typed_without_brackets():
    tried = local_models.addresses_to_try("fd7a::1")
    assert ("LM Studio", "http://[fd7a::1]:1234/v1") in tried and len(tried) == len(local_models.KNOWN_SERVERS)
    assert ("Ollama", "http://[::1]:11434/v1") in local_models.addresses_to_try("::1")


def test_an_address_without_a_port_tries_each_servers_own_port():
    tried = local_models.addresses_to_try("mac-mini")
    assert ("Ollama", "http://mac-mini:11434/v1") in tried and ("LM Studio", "http://mac-mini:1234/v1") in tried
    assert len(tried) == len(local_models.KNOWN_SERVERS)


@pytest.mark.parametrize("typed", ["", "mac mini", "http://user:pw@mac-mini:1234", "ftp://mac-mini",
                                   "http://mac-mini:1234/?x=1", "http://[oops", "x" * 400])
def test_addresses_that_arent_one_are_refused(typed):
    with pytest.raises(local_models.AddressError):
        local_models.addresses_to_try(typed)


def answers(payload):
    async def handler(request):
        return web.json_response(payload)
    return handler


def resolver(table):
    def resolve(host, port, type=0):
        if host not in table:
            raise socket.gaierror("not found")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in table[host]]
    return resolve


def test_only_your_own_computers_count_as_yours():
    resolve = resolver({"mac-mini": ["192.168.1.20"], "mac-mini.tail1.ts.net": ["100.90.1.2"],
                        "example.com": ["93.184.215.14"], "mixed": ["10.0.0.2", "93.184.215.14"]})
    for host in ("localhost", "127.0.0.1", "::1", "10.1.2.3", "192.168.0.9", "100.64.0.1", "fd7a::1", "mac-mini",
                 "mac-mini.tail1.ts.net"):
        assert local_models.where_is(host, resolve) == "yours", host
    for host in ("8.8.8.8", "example.com", "mixed"):  # every address a name leads to has to be yours
        assert local_models.where_is(host, resolve) == "elsewhere", host
    assert local_models.where_is("nowhere", resolve) == "unknown"


def test_a_home_computer_with_a_public_ipv6_address_too_is_still_yours():
    resolve = resolver({"mac-mini": ["192.168.1.20", "2601:600:a::20"], "nas.local": ["10.0.0.5", "2601:600:a::5"],
                        "llm.example.com": ["192.168.1.20", "2601:600:a::20"], "v6only": ["2601:600:a::20"]})
    assert local_models.where_is("mac-mini", resolve) == local_models.where_is("nas.local", resolve) == "yours"
    # A public name, or one with no private address at all, isn't
    assert local_models.where_is("llm.example.com", resolve) == local_models.where_is("v6only", resolve) == "elsewhere"
    # A public IPv4 address carried inside an IPv6 one counts as what it is
    for extra in ("::ffff:8.8.8.8", "2002:808:808::1", "64:ff9b::808:808"):
        assert local_models.where_is("mac-mini", resolver({"mac-mini": ["192.168.1.2", extra]})) == "elsewhere", extra


def test_asking_a_server_goes_straight_to_it(monkeypatch):
    """Not through a proxy (which would learn where your servers are), and a redirect isn't followed."""
    from ixel_mat.config.setup import _DIRECT_OPENER
    handlers = {type(h).__name__ for h in _DIRECT_OPENER.handlers}
    assert "_NoRedirects" in handlers and not {"HTTPRedirectHandler", "ProxyHandler"} & handlers
    opened = []
    monkeypatch.setattr(_DIRECT_OPENER, "open", lambda req, timeout: opened.append(req.full_url) or (_ for _ in ()).throw(OSError()))
    assert local_models.ask_server("LM Studio", "https://mac-mini.tail1.ts.net/v1") is None
    assert opened == ["https://mac-mini.tail1.ts.net/v1/models"]


def test_ixel_wont_look_at_a_computer_that_isnt_yours():
    resolve = resolver({"example.com": ["93.184.215.14"]})
    with pytest.raises(local_models.AddressError, match="isn't on this computer or your own network"):
        local_models.at_address("example.com:1234", resolve=resolve)
    with pytest.raises(local_models.AddressError, match="can't find a computer called"):
        local_models.at_address("nowhere", resolve=resolve)


def test_a_name_only_your_router_knows_is_refused_for_one_ixel_can_tell_by_its_look(monkeypatch):
    """mac-mini.fritz.box leads home, but each question would take it for a company and ask for a key."""
    monkeypatch.setattr(local_models, "look", lambda candidates, timeout: pytest.fail("looked"))
    with pytest.raises(local_models.AddressError, match="Use its address .* or its short name"):
        local_models.at_address("mac-mini.fritz.box:1234", resolve=resolver({"mac-mini.fritz.box": ["192.168.1.20"]}))
    for name in ("mac-mini", "mac-mini.local", "nas.lan", "mac-mini.tail1.ts.net", "192.168.1.20", "localhost"):
        local_models.usable_name(name)


def test_nothing_answering_says_what_to_turn_on(monkeypatch):
    monkeypatch.setattr(local_models, "look", lambda candidates, timeout: [])
    with pytest.raises(local_models.AddressError, match="Serve on Local Network.*OLLAMA_HOST") as raised:
        local_models.at_address("mac-mini", resolve=resolver({"mac-mini": ["192.168.1.20"]}))
    # and that anyone on that network can then use it
    assert str(raised.value).endswith(local_models.OPEN_TO_NETWORK)


# ── Asking a server ───────────────────────────────────────────────────────────

def test_a_server_says_its_models_and_the_ones_that_cant_answer_are_set_aside():
    async def go():
        async with FakeProvider(models=["qwen3:14b", "nomic-embed-text:latest", "llama3.2"]) as fake:
            base = f"http://127.0.0.1:{fake.port}/v1"
            return await asyncio.to_thread(local_models.ask_server, "LM Studio", base), base

    server, base = asyncio.run(go())
    assert (server.name, server.base, server.models, server.hidden, server.ollama) == \
        ("LM Studio", base, ["qwen3:14b", "llama3.2"], ["nomic-embed-text:latest"], False)
    assert server.to_dict()["here"] is True


def test_ollamas_cloud_models_are_set_apart():
    async def go():
        app = web.Application()
        app.router.add_get("/v1/models", answers({"data": [{"id": "qwen3:8b"}, {"id": "gpt-oss:120b-cloud"},
                                                           {"id": "deepseek-v3.1:671b-cloud"}]}))
        app.router.add_get("/api/version", answers({"version": "0.12.3"}))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            return await asyncio.to_thread(local_models.ask_server, "Ollama", f"http://127.0.0.1:{port}/v1", 2)
        finally:
            await runner.cleanup()

    server = asyncio.run(go())
    assert server.models == ["qwen3:8b"] and server.elsewhere == ["gpt-oss:120b-cloud", "deepseek-v3.1:671b-cloud"]
    assert server.has("qwen3:8b") and not server.has("gpt-oss:120b-cloud")


def test_ollama_is_known_by_its_version_answer_whatever_its_port():
    async def go():
        app = web.Application()
        app.router.add_get("/v1/models", answers({"data": [{"id": "llama3.2:latest", "owned_by": "library"}]}))
        app.router.add_get("/api/version", answers({"version": "0.12.3"}))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            return await asyncio.to_thread(local_models.look, [("Model server", f"http://127.0.0.1:{port}/v1"),
                                                                ("Jan", "http://127.0.0.1:9/v1")], 2)
        finally:
            await runner.cleanup()

    [server] = asyncio.run(go())
    assert server.name == "Ollama" and server.ollama and server.models == ["llama3.2:latest"]


def test_a_page_that_isnt_a_model_list_isnt_a_server():
    async def go():
        app = web.Application()
        app.router.add_get("/v1/models", answers({"hello": "world"}))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            return await asyncio.to_thread(local_models.ask_server, "x", f"http://127.0.0.1:{port}/v1")
        finally:
            await runner.cleanup()

    assert asyncio.run(go()) is None


def test_the_settings_model_list_leaves_out_embedding_models(monkeypatch):
    from ixel_mat.gui import model_choices

    async def go():
        async with FakeProvider(models=["qwen3:14b", "text-embedding-nomic-embed-text-v1.5"]) as fake:
            cfg = AgentConfig(name="lm", label="LM", type="http", url=fake.openai_url, model="qwen3:14b")
            return await asyncio.to_thread(model_choices.agent_choices, cfg, {})

    assert asyncio.run(go())["models"] == ["qwen3:14b"]


# ── What it costs ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model, billing", [("ollama/qwen3:8b", "local"), ("lmstudio/qwen/qwen3-14b", "local"),
                                            ("anthropic/claude-sonnet-5-5", "unknown"), ("", "unknown")])
def test_opencode_asking_your_own_server_is_free(model, billing):
    cfg = AgentConfig(name="oc", label="OpenCode", type="oneshot", command="opencode", model=model)
    assert billing_for(SimpleNamespace(config=cfg)) == billing


def test_ollamas_cloud_models_arent_counted_as_free_or_local():
    for cfg in (AgentConfig(name="o", label="O", type="http", url="http://127.0.0.1:11434/v1/chat/completions",
                            model="gpt-oss:120b-cloud"),
                AgentConfig(name="oc", label="OpenCode", type="oneshot", command="opencode",
                            model="ollama/qwen3-coder:480b-cloud")):
        assert billing_for(SimpleNamespace(config=cfg)) == "unknown"
    cfg = AgentConfig(name="o", label="O", type="http", url="http://127.0.0.1:11434/v1/chat/completions",
                      model="qwen3:14b")
    assert billing_for(SimpleNamespace(config=cfg)) == "local"
