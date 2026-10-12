"""Anthropic prompt caching: the fixed start of a message gets a cache breakpoint (a message with no such start
gets none), and cached tokens are counted at cache prices."""
import asyncio
import json

import pytest

from fake_providers import PNG, FakeProvider, anthropic_reply, openai_reply
from ixel_mat.agents import http as http_mod
from ixel_mat.agents.base import AgentConfig
from ixel_mat.agents.http import MIN_CACHEABLE_CHARS, HttpAgent
from ixel_mat.pictures import Picture
from ixel_mat.usage import cost_at, price_for

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
CACHE = {"type": "ephemeral"}
LONG = "Fixed rules, persona and skills. " * 300
assert len(LONG) > MIN_CACHEABLE_CHARS  # if this fails, LONG is too short to exercise the breakpoint
CACHED_USAGE = {"input_tokens": 5, "output_tokens": 5, "cache_read_input_tokens": 4000,
                "cache_creation_input_tokens": 1200}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def anthropic_to_fake(monkeypatch):
    """The SDK path is only taken for anthropic.com URLs; point it at the fake server."""
    holder = {}
    monkeypatch.setattr(http_mod, "_anthropic_base_url", lambda url: f"http://127.0.0.1:{holder['port']}")
    return holder


async def _ask(fake, url, message="hello", *, model="claude-sonnet-5-5", accepts=None, pictures=(), **kwargs):
    agent = HttpAgent(AgentConfig(name="a", label="A", type="http", url=url, token="sk-test", model=model,
                                  accepts=accepts))
    await agent.connect()
    seen = []
    try:
        answer = await agent.send_and_receive(message, on_usage=seen.append, pictures=pictures, **kwargs)
        return answer, seen, fake.requests
    finally:
        await agent.disconnect()


def _cached_reply(_request):
    status, body, headers = anthropic_reply("ok", model="claude-sonnet-5-5")
    body["usage"] = dict(CACHED_USAGE)
    return status, body, headers


def test_a_long_message_to_claude_with_no_prefix_gets_no_cache_breakpoint(anthropic_to_fake):
    # Marking the whole message would write it to the cache on every call and never read it back
    async def go():
        async with FakeProvider() as fake:
            anthropic_to_fake["port"] = fake.port
            return await _ask(fake, ANTHROPIC_URL, LONG)

    _, _, requests = _run(go())
    body = requests[0].body
    assert body["messages"][0]["content"] == LONG
    assert "cache_control" not in json.dumps(body)


def test_a_long_message_to_claude_gets_its_cache_breakpoint_at_the_end_of_the_prefix(anthropic_to_fake):
    prefix = LONG[:5000]

    async def go():
        async with FakeProvider() as fake:
            anthropic_to_fake["port"] = fake.port
            return await _ask(fake, ANTHROPIC_URL, LONG, cache_prefix=prefix)

    _, _, requests = _run(go())
    content = requests[0].body["messages"][0]["content"]
    assert content == [{"type": "text", "text": prefix, "cache_control": CACHE},
                       {"type": "text", "text": LONG[5000:]}]


def test_a_short_question_to_claude_is_sent_as_before(anthropic_to_fake):
    async def go():
        async with FakeProvider() as fake:
            anthropic_to_fake["port"] = fake.port
            return await _ask(fake, ANTHROPIC_URL, "hello")

    _, _, requests = _run(go())
    body = requests[0].body
    assert body["messages"][0]["content"] == "hello"
    assert "cache_control" not in json.dumps(body)


def test_the_fixed_start_is_cached_and_the_changing_end_is_not(anthropic_to_fake):
    prefix = LONG[:5000]
    first_message = LONG + " Step 3 result: 391."
    second_message = LONG + " Step 3 result: 381."

    async def go():
        async with FakeProvider() as fake:
            anthropic_to_fake["port"] = fake.port
            await _ask(fake, ANTHROPIC_URL, first_message, cache_prefix=prefix)
            return await _ask(fake, ANTHROPIC_URL, second_message, cache_prefix=prefix)

    _, _, requests = _run(go())
    first, second = (r.body["messages"][0]["content"] for r in requests)
    assert len(first) == 2
    assert first[0] == {"type": "text", "text": prefix, "cache_control": CACHE}
    assert first[1] == {"type": "text", "text": first_message[5000:]} and "cache_control" not in first[1]
    assert "".join(block["text"] for block in first) == first_message
    # The same start on both calls is what lets the second one read it back from the cache
    assert second[0] == first[0] and second[1] != first[1]


def test_a_prefix_the_message_does_not_start_with_is_ignored(anthropic_to_fake):
    async def go():
        async with FakeProvider() as fake:
            anthropic_to_fake["port"] = fake.port
            return await _ask(fake, ANTHROPIC_URL, LONG, cache_prefix="Something else. " + LONG[:5000])

    _, _, requests = _run(go())
    body = requests[0].body
    assert body["messages"][0]["content"] == LONG  # nothing is marked, and never the whole message
    assert "cache_control" not in json.dumps(body)


def test_a_prefix_under_the_minimum_is_not_marked(anthropic_to_fake):
    async def go():
        async with FakeProvider() as fake:
            anthropic_to_fake["port"] = fake.port
            return await _ask(fake, ANTHROPIC_URL, LONG, cache_prefix=LONG[:100])

    _, _, requests = _run(go())
    body = requests[0].body
    assert body["messages"][0]["content"] == LONG
    assert "cache_control" not in json.dumps(body)


def test_other_providers_get_the_plain_message_whatever_the_prefix():
    async def go():
        async with FakeProvider(handler=lambda r: openai_reply("ok")) as fake:
            return await _ask(fake, fake.openai_url, LONG, model="gpt-x", cache_prefix=LONG[:5000])

    _, _, requests = _run(go())
    body = requests[0].body
    assert body["messages"][0]["content"] == LONG
    assert "cache_control" not in json.dumps(body)


def test_cached_tokens_are_counted_at_the_cache_prices(anthropic_to_fake):
    async def go():
        async with FakeProvider(handler=_cached_reply) as fake:
            anthropic_to_fake["port"] = fake.port
            return await _ask(fake, ANTHROPIC_URL, LONG)

    answer, seen, _ = _run(go())
    assert answer == "ok" and len(seen) == 1
    usage = seen[0]
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == \
        (5, 5, 4000, 1200)
    assert usage.model == "claude-sonnet-5-5"
    cost = cost_at(price_for(usage.model), usage.input_tokens, usage.output_tokens, usage.cache_read_tokens,
                   usage.cache_write_tokens)
    assert cost == pytest.approx((5 * 2.0 + 5 * 10.0 + 4000 * 0.20 + 1200 * 2.5) / 1_000_000)


def test_cached_tokens_are_counted_on_the_streamed_path_too(anthropic_to_fake):
    streamed = []

    async def on_text(text):
        streamed.append(text)

    async def go():
        async with FakeProvider(handler=_cached_reply) as fake:
            anthropic_to_fake["port"] = fake.port
            return await _ask(fake, ANTHROPIC_URL, LONG, on_text=on_text, cache_prefix=LONG[:5000])

    answer, seen, requests = _run(go())
    assert answer == "ok" and "".join(streamed) == "ok"
    assert len(seen) == 1 and (seen[0].cache_read_tokens, seen[0].cache_write_tokens) == (4000, 1200)
    body = requests[0].body
    assert body["stream"] is True
    assert body["messages"][0]["content"] == [{"type": "text", "text": LONG[:5000], "cache_control": CACHE},
                                              {"type": "text", "text": LONG[5000:]}]


def test_claude_gets_the_picture_then_the_marked_text(anthropic_to_fake):
    picture = Picture("image/png", PNG, 1, 1)

    async def go():
        async with FakeProvider() as fake:
            anthropic_to_fake["port"] = fake.port
            return await _ask(fake, ANTHROPIC_URL, LONG, accepts=["image"], pictures=(picture,),
                              cache_prefix=LONG[:5000])

    _, _, requests = _run(go())
    assert requests[0].body["messages"][0]["content"] == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": picture.base64()}},
        {"type": "text", "text": LONG[:5000], "cache_control": CACHE},
        {"type": "text", "text": LONG[5000:]}]
