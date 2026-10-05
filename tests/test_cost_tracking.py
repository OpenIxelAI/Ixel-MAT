"""What reviews cost: token counts from each transport, prices, per-run totals, savings and stats."""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from fake_providers import FakeProvider, anthropic_reply, openai_reply
from ixel_mat import review_ui, stats
from ixel_mat.agents import http as http_mod
from ixel_mat.agents.base import AgentConfig
from ixel_mat.agents.http import HttpAgent
from ixel_mat.agents.oneshot import OneShotAgent
from ixel_mat.modes.review import ReviewMode, ReviewResult, run_review
from ixel_mat.runtime import Settings, local_agent_names
from ixel_mat.usage import (PRICES, CallUsage, Price, Saving, Usage, anthropic_usages, billing_for, call_usage,
                            claude_code_usage, cost_at, cost_line, money, openai_usage, parse_pricing, price_for,
                            totals)
from test_review import FENCED_ANSWER, PanelAgent, Verifier


def _run(coro):
    return asyncio.run(coro)


# ── Prices ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model, expected", [
    ("claude-opus-5-5", PRICES["claude-opus-5-5"]),
    ("claude-opus-5", PRICES["claude-opus-5"]),               # not taken for 5-5, nor 5-5 for it
    ("claude-haiku-4-5-20251001", PRICES["claude-haiku-4-5"]),  # a snapshot date
    ("anthropic/claude-sonnet-5", PRICES["claude-sonnet-5"]),   # OpenRouter-style prefix
    ("claude-opus-5-5[1m]", PRICES["claude-opus-5-5"]),        # Claude Code's context tag
    ("claude-sonnet-4-6@20260101", PRICES["claude-sonnet-4-6"]),  # Vertex
    ("claude-opus-5-6", None),                                 # a newer model isn't priced as an older one
    ("gpt-5.5", None),
    ("", None),
], ids=["opus-5-5", "opus-5", "dated", "prefixed", "context-tag", "vertex", "newer", "other", "blank"])
def test_price_for(model, expected):
    assert price_for(model) == expected


def test_your_prices_come_first_and_bad_ones_are_reported():
    prices, warnings = parse_pricing({"pricing": {
        "gpt-5.5": {"input": 1.25, "output": 10},
        "claude-opus-5-5": {"input": 3, "output": 15, "cache_read": 0.3},
        "broken": {"input": "cheap"},
        "extra": {"input": 1, "output": 2, "per_request": 0.01},
    }})
    assert price_for("gpt-5.5", prices) == price_for("gpt-5.5-2026-04-23", prices) == Price(1.25, 10.0)
    assert price_for("gpt-5.5-mini", prices) is None  # another model, not a snapshot of this one
    assert price_for("claude-opus-5-5", prices).read == 0.3
    assert price_for("claude-opus-5", prices) == PRICES["claude-opus-5"]  # the rest of the list still applies
    assert sorted(w.split("'")[1] for w in warnings) == ["broken", "extra"]
    assert parse_pricing({"pricing": "cheap"})[1] and parse_pricing({})[1] == []


def test_settings_pass_your_prices_to_every_run():
    settings = Settings({"pricing": {"gpt-5.5": {"input": 1, "output": 2}}}, {})
    assert settings.run_options(ReviewMode.REVIEW)["pricing"] == {"gpt-5.5": Price(1.0, 2.0)}


@pytest.mark.parametrize("cfg, billing", [
    (dict(type="http", url="https://api.openai.com/v1/chat/completions"), "api"),
    (dict(type="http", url="http://127.0.0.1:11434/v1/chat/completions"), "local"),
    (dict(type="oneshot", command="claude"), "plan"),
    (dict(type="oneshot", command=r"C:\Users\me\AppData\Roaming\npm\codex.cmd"), "plan"),
    (dict(type="oneshot", command="hermes"), "unknown"),
    (dict(type="websocket", url="wss://gateway"), "unknown"),
    (dict(type="oneshot", command="claude", billing="api"), "api"),  # a Console login pays per token
], ids=["api", "local", "claude", "codex-shim", "other-cli", "gateway", "override"])
def test_billing(cfg, billing):
    agent = SimpleNamespace(config=AgentConfig(name="a", label="A", **cfg))
    assert billing_for(agent) == billing
    assert billing_for(object()) == "unknown"


@pytest.mark.parametrize("host, billing", [
    ("10.1.2.3", "local"), ("172.16.0.9", "local"), ("172.31.255.1", "local"), ("192.168.1.20", "local"),
    ("169.254.10.10", "local"), ("100.101.102.103", "local"), ("[fe80::1]", "local"), ("[fd12:3456::7]", "local"),
    ("[::ffff:192.168.1.20]", "local"), ("nas.local", "local"), ("gpu.lan", "local"), ("llm.internal", "local"),
    ("box.home.arpa", "local"), ("ollama", "local"), ("gpu-box.LOCAL.", "local"),
    ("172.32.0.1", "api"), ("100.128.0.1", "api"), ("8.8.8.8", "api"), ("[2001:db8::1]", "api"),
    ("api.x.ai", "api"), ("openrouter.ai", "api"), ("local.example.com", "api"), ("example.lan.com", "api"),
], ids=lambda value: value)
def test_models_on_your_own_network_are_free(host, billing):
    agent = SimpleNamespace(config=AgentConfig(name="a", label="A", type="http", url=f"http://{host}:11434/v1"))
    assert billing_for(agent) == billing


def test_your_billing_setting_still_wins_on_your_network():
    """A paid proxy on your network is billed per token when you say so."""
    agent = SimpleNamespace(config=AgentConfig(name="a", label="A", type="http", url="http://192.168.1.5:4000/v1",
                                               billing="api"))
    assert billing_for(agent) == "api"


def test_the_saves_counter_agrees_on_which_models_are_local():
    """What the cost line counts as free is what "Local hero" counts as local."""
    configs = {name: AgentConfig(name=name, label=name, type="http", url=url, **extra) for name, url, extra in (
        ("here", "http://localhost:11434/v1", {}), ("lan", "http://192.168.1.20:11434/v1", {}),
        ("paid", "http://192.168.1.5:4000/v1", {"billing": "api"}), ("cloud", "https://api.x.ai/v1", {}))}
    configs["cli"] = AgentConfig(name="cli", label="cli", type="oneshot", command="ollama", billing="local")
    assert local_agent_names(configs) == {"here", "lan", "cli"}


def test_money_reads_naturally():
    assert [money(x) for x in (0, 0.0004, 0.042, 0.05, 0.1, 0.4567, 12.5)] == \
        ["$0", "<$0.001", "$0.042", "$0.05", "$0.10", "$0.46", "$12.50"]


# ── What each transport reports ───────────────────────────────────────────────

def test_openai_usage_counts_cached_tokens_apart():
    usage = openai_usage({"model": "gpt-5.5-2026", "usage": {
        "prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 600}}})
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens, usage.model) == \
        (400, 600, 200, "gpt-5.5-2026")
    assert openai_usage({"choices": []}) is None and openai_usage("nonsense") is None


def test_reasoning_counted_outside_completion_tokens_is_still_output():
    """xAI counts reasoning apart from completion_tokens, and bills it as output."""
    usage = openai_usage({"usage": {"prompt_tokens": 2000, "completion_tokens": 300, "total_tokens": 6300,
                                    "completion_tokens_details": {"reasoning_tokens": 4000}}})
    assert usage.output_tokens == 4300
    # OpenAI already counts it inside completion_tokens: no double count
    usage = openai_usage({"usage": {"prompt_tokens": 2000, "completion_tokens": 4300, "total_tokens": 6300,
                                    "completion_tokens_details": {"reasoning_tokens": 4000}}})
    assert usage.output_tokens == 4300


def test_a_declined_attempt_and_its_fallback_are_both_billed():
    iterations = [SimpleNamespace(type="message", model="claude-opus-5", input_tokens=20000, output_tokens=3000,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
                  SimpleNamespace(type="fallback_message", model="claude-opus-4-8", input_tokens=20000,
                                  output_tokens=1500, cache_read_input_tokens=0, cache_creation_input_tokens=0),
                  SimpleNamespace(type="compaction", model="claude-opus-5", input_tokens=1, output_tokens=1)]
    response = SimpleNamespace(model="claude-opus-4-8", usage=SimpleNamespace(
        input_tokens=20000, output_tokens=1500, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        iterations=iterations))
    parts = anthropic_usages(response, "claude-opus-5")
    assert [(u.model, u.output_tokens) for u in parts] == [("claude-opus-5", 3000), ("claude-opus-4-8", 1500)]
    agent = SimpleNamespace(name="opus", label="Opus", model="claude-opus-5", config=AgentConfig(
        name="opus", label="Opus", type="http", url="https://api.anthropic.com/v1/messages"))
    call = call_usage(agent, "answer", "panel", "q", "a", parts)
    assert call.model == "claude-opus-4-8" and call.output_tokens == 4500
    assert call.cost_usd == pytest.approx(cost_at(PRICES["claude-opus-5"], 20000, 3000)
                                          + cost_at(PRICES["claude-opus-4-8"], 20000, 1500))
    plain = SimpleNamespace(model="claude-sonnet-5", usage=SimpleNamespace(input_tokens=7, output_tokens=3))
    assert [(u.input_tokens, u.model) for u in anthropic_usages(plain)] == [(7, "claude-sonnet-5")]
    assert anthropic_usages(SimpleNamespace()) == []


def test_claude_code_result_event():
    event = {"type": "result", "total_cost_usd": 0.0259,
             "usage": {"input_tokens": 3, "cache_read_input_tokens": 14000, "cache_creation_input_tokens": 900,
                       "output_tokens": 120, "output_tokens_details": {"thinking_tokens": 40}},
             "modelUsage": {"claude-haiku-4-5": {"costUSD": 0.0001, "outputTokens": 5},
                            "claude-opus-5-5": {"costUSD": 0.0258, "outputTokens": 115}}}
    usage = claude_code_usage(event)
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens, usage.output_tokens) == \
        (3, 14000, 900, 120)
    assert usage.cost_usd == 0.0259 and usage.model == "claude-opus-5-5"
    assert claude_code_usage({"type": "result", "result": "hi"}) is None
    assert claude_code_usage({"type": "result", "total_cost_usd": True, "usage": {"input_tokens": -5}}) == Usage()


def _recording(fake_reply):
    """A FakeProvider handler that answers every request with fake_reply(request)."""
    return FakeProvider(handler=fake_reply)


def test_openai_transport_reports_usage():
    body = {"model": "m", "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3}}

    async def go():
        async with _recording(lambda r: (200, body, {})) as fake:
            agent = HttpAgent(AgentConfig(name="a", label="A", type="http", url=fake.openai_url, token="k", model="m"))
            await agent.connect()
            seen = []
            try:
                return await agent.send_and_receive("q", on_usage=seen.append), seen, fake.requests
            finally:
                await agent.disconnect()

    answer, seen, requests = _run(go())
    assert answer == "hi" and [(u.input_tokens, u.output_tokens) for u in seen] == [(12, 3)]
    assert "stream_options" not in requests[0].body  # not streamed, and not a known provider


def test_anthropic_transport_reports_usage_and_the_model_that_answered(monkeypatch):
    holder = {}
    monkeypatch.setattr(http_mod, "_anthropic_base_url", lambda url: holder["base"])

    async def go():
        async with FakeProvider(handler=lambda r: anthropic_reply("hi", model="claude-opus-4-8")) as fake:
            holder["base"] = f"http://127.0.0.1:{fake.port}"
            agent = HttpAgent(AgentConfig(name="a", label="A", type="http", url="https://api.anthropic.com/v1/messages",
                                          token="k", model="claude-sonnet-5"))
            await agent.connect()
            seen = []
            try:
                await agent.send_and_receive("q", on_usage=seen.append)
                return seen
            finally:
                await agent.disconnect()

    (usage,) = _run(go())
    assert (usage.input_tokens, usage.output_tokens, usage.model) == (5, 5, "claude-opus-4-8")


def test_openai_stream_asks_known_providers_for_usage(monkeypatch):
    monkeypatch.setattr(http_mod, "provider_for_url", lambda url: "openai")

    async def go():
        async with FakeProvider(handler=lambda r: openai_reply("streamed")) as fake:
            agent = HttpAgent(AgentConfig(name="a", label="A", type="http", url=fake.openai_url, token="k", model="m"))
            await agent.connect()

            async def on_text(_):
                pass

            try:
                await agent.send_and_receive("q", on_text=on_text)
                return fake.requests[0].body
            finally:
                await agent.disconnect()

    assert _run(go())["stream_options"] == {"include_usage": True}


def test_a_streamed_reply_s_usage_comes_from_its_last_chunk():
    lines = [b'data: {"choices": [{"delta": {"content": "39"}}]}', b"",
             b'data: {"choices": [{"delta": {"content": "1"}}]}',
             b'data: {"model": "gpt-5.5", "choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 2}}',
             b"data: [DONE]"]

    class Resp:
        async def _lines(self):
            for line in lines:
                yield line

        @property
        def content(self):
            return self._lines()

    async def on_text(_):
        pass

    text, usage = _run(http_mod._read_chat_stream(Resp(), on_text, "m"))
    assert text == "391" and (usage.input_tokens, usage.output_tokens, usage.model) == (20, 2, "gpt-5.5")


_CLAUDE = """
import json
print(json.dumps({"type": "result", "is_error": %s, "result": "391", "total_cost_usd": 0.012,
                  "usage": {"input_tokens": 10, "output_tokens": 4},
                  "modelUsage": {"claude-opus-5-5": {"costUSD": 0.012}}}), flush=True)
"""


@pytest.mark.parametrize("failed", [False, True], ids=["ok", "error"])
def test_claude_code_reports_its_cost_even_when_it_fails(failed):
    agent = OneShotAgent(AgentConfig(name="cc", label="CC", type="oneshot", command=sys.executable,
                                     args=["-c", _CLAUDE % failed], stdout_format="claude-stream-json",
                                     prompt_via="arg"))

    async def go():
        seen = []
        await agent.connect()
        try:
            return await agent.send_and_receive("q", on_usage=seen.append), seen
        except RuntimeError:
            return None, seen

    answer, seen = _run(go())
    assert answer == (None if failed else "391")
    assert [(u.cost_usd, u.model, u.output_tokens) for u in seen] == [(0.012, "claude-opus-5-5", 4)]


# ── A review's cost ───────────────────────────────────────────────────────────

class ReportingAgent(PanelAgent):
    """A PanelAgent billed to an API key, that reports its tokens."""

    def __init__(self, name, answer, model="claude-sonnet-5", **kw):
        super().__init__(name, answer, **kw)
        self.config = AgentConfig(name=name, label=self.label, type="http", url="https://api.example/v1",
                                  model=model)
        self.model = model

    async def send_and_receive(self, message, **kwargs):
        reply = await super().send_and_receive(message, **kwargs)
        kwargs["on_usage"](Usage(input_tokens=1000, output_tokens=100, model=self.model))
        return reply


ONE_CALL = cost_at(PRICES["claude-sonnet-5"], 1000, 100)


def test_every_call_is_recorded_and_priced():
    agents = [ReportingAgent("gpt", "It's 391."), ReportingAgent("claude", "17 × 23 = 391"),
              PanelAgent("gemini", "The answer is 381.")]  # reports nothing: counted from its text

    result = _run(run_review("What is 17 × 23?", agents, mode="review"))
    assert result.calls == len(result.usage) >= 7
    reported = [c for c in result.usage if not c.estimated]
    assert {c.agent for c in reported} == {"gpt", "claude"}
    assert all(c.cost_usd == pytest.approx(ONE_CALL) and c.billing == "api" for c in reported)
    guessed = [c for c in result.usage if c.agent == "gemini"]
    assert guessed and all(c.estimated and c.billing == "unknown" and c.input_tokens > 0 for c in guessed)
    total = totals(result.usage)
    assert total.api_usd == pytest.approx(ONE_CALL * len(reported)) and total.unknown_calls == len(guessed)
    data = result.to_dict()
    assert data["usage"]["total"]["api_calls"] == len(reported) and len(data["usage"]["calls"]) == result.calls
    summary = data["usage"]["summary"]
    # the dollars were reported, so they're exact; gemini's tokens were counted from its text
    assert summary.startswith("$") and f"{len(guessed)} not priced" in summary and "about " in summary
    json.dumps(data)


def test_a_failed_call_counts_only_if_it_reported_its_tokens():
    class Refuses(ReportingAgent):
        async def send_and_receive(self, message, **kwargs):
            kwargs["on_usage"](Usage(input_tokens=50, output_tokens=0, model=self.model))
            raise RuntimeError("Claude declined this request")

    agents = [ReportingAgent("gpt", "It's 391."), ReportingAgent("claude", "17 × 23 = 391"),
              PanelAgent("gemini", "", fail_on={"answer"}), Refuses("grok", "")]
    result = _run(run_review("What is 17 × 23?", agents, mode="quick", moderator="gpt"))
    counted = sorted(c.agent for c in result.usage if c.round == "answer")
    assert counted == ["claude", "gpt", "grok"]  # gemini failed before it used anything


class ReportingVerifier(Verifier):
    def __init__(self, reply, model="claude-opus-5-5"):
        super().__init__(reply)
        self.config = AgentConfig(name="opus", label="Opus", type="http", url="https://api.anthropic.com/v1/messages",
                                  model=model)
        self.model = model

    async def send_and_receive(self, message, **kwargs):
        reply = await super().send_and_receive(message, **kwargs)
        kwargs["on_usage"](Usage(input_tokens=5000, output_tokens=100, model=self.model))
        return reply


def _confirm(marker):
    def reply(prompt):
        label = next(label for _, label, text in FENCED_ANSWER.findall(prompt) if marker in text)
        return json.dumps({"status": "confirmed", "use": label})
    return reply


def _saver(agents, **kwargs):
    return _run(run_review("What is 17 × 23?", agents, mode="saver", verifier="opus", **kwargs))


def test_saver_saving_is_the_answer_the_big_model_did_not_write():
    long_answer = ("391, because " + "17 × 20 is 340 and 17 × 3 is 51. " * 40).strip()
    result = _saver([ReportingAgent("gpt", long_answer), ReportingAgent("claude", "17 × 23 = 391"),
                     ReportingVerifier(_confirm("because"))])
    assert result.verifier_outcome == "confirmed" and result.answer(result.accepted).text == long_answer
    saving = result.saving
    expected = -(-len(long_answer) // 4) - 100  # its answer, less the 100 tokens the verifier wrote
    assert (saving.tokens, saving.billing, saving.model) == (expected, "api", "claude-opus-5-5")
    assert saving.usd == pytest.approx(expected * PRICES["claude-opus-5-5"].output / 1_000_000)
    data = result.to_dict()["saving"]
    assert data["tokens"] == expected and data["summary"].startswith("Saved about")
    assert [c.tier for c in result.usage].count("verifier") == 1


def test_a_short_draft_saves_nothing():
    result = _saver([ReportingAgent("gpt", "It's 391."), ReportingAgent("claude", "17 × 23 = 391"),
                     ReportingVerifier(_confirm("391"))])
    assert result.saving.tokens == 0 and result.saving.usd == 0
    assert "about as much" in result.to_dict()["saving"]["summary"]


def test_saving_on_a_subscription_is_counted_in_tokens_only():
    verifier = ReportingVerifier(_confirm("because"))
    verifier.config = AgentConfig(name="opus", label="Opus", type="oneshot", command="claude")
    result = _saver([ReportingAgent("gpt", "391, because " + "x" * 800), ReportingAgent("claude", "391"),
                     verifier])
    assert result.saving.billing == "plan" and result.saving.usd is None and result.saving.tokens > 0
    assert "plan's limits" in result.to_dict()["saving"]["summary"]


def test_no_saving_when_the_big_model_wrote_the_answer():
    fix = json.dumps({"status": "corrected", "answer": "391", "issues": ["both wrong"]})
    result = _saver([PanelAgent("gpt", "380"), PanelAgent("claude", "381"), ReportingVerifier(fix)],
                    on_wrong="correct")
    assert result.verifier_outcome == "corrected" and result.saving is None
    assert result.to_dict()["saving"] is None


def _plain(renderables):
    return "".join(str(getattr(r, "plain", "")) for r in renderables)


def test_report_shows_cost_and_saving():
    result = _saver([ReportingAgent("gpt", "391, because " + "x" * 800), ReportingAgent("claude", "391"),
                     ReportingVerifier(_confirm("because"))])
    text = _plain(review_ui.report(result))
    assert "Cost: $" in text and "on API keys" in text
    assert "Saved about" in text
    assert cost_line(totals(result.usage)).startswith("$")


# ── Stats: spent and saved ────────────────────────────────────────────────────

def _costed(spent=0.02, saving=None):
    call = CallUsage("a", "A", "answer", "panel", "api", "m", 10, 10, cost_usd=spent)
    return ReviewResult("q", ReviewMode.REVIEW, usage=[call], saving=saving)


def test_every_review_adds_its_cost_to_stats(tmp_path):
    path = tmp_path / "stats.json"
    update = stats.record_run(_costed(0.02), path=path)
    assert not update.counted  # not a saver run: no saves
    stats.record_run(_costed(0.03, Saving(1200, 0.024, "api", "Opus", "claude-opus-5-5")), path=path)
    cost = stats.summary(stats.load_stats(path))["cost"]
    assert cost["reviews"] == cost["month_reviews"] == 2
    assert cost["spent_usd"] == pytest.approx(0.05) == cost["month_spent_usd"]
    assert (cost["saved_tokens"], cost["saved_usd"]) == (1200, pytest.approx(0.024))
    assert stats.cost_lines(cost) == [
        "This month: $0.05 on API keys over 2 reviews · saver mode saved about 1,200 big-model tokens (≈ $0.024)"]


def test_calls_with_no_known_price_aren_t_shown_as_free(tmp_path):
    path = tmp_path / "stats.json"
    unpriced = CallUsage("gpt", "GPT", "answer", "panel", "api", "gpt-x", 10, 10, cost_usd=None)
    stats.record_run(ReviewResult("q", ReviewMode.REVIEW, usage=[unpriced, unpriced]), path=path)
    cost = stats.summary(stats.load_stats(path))["cost"]
    assert stats.cost_lines(cost) == ["This month: 2 unpriced calls on API keys (add prices under [pricing]) "
                                      "over 1 review"]
    stats.record_run(_costed(0.5), path=path)
    cost = stats.summary(stats.load_stats(path))["cost"]
    assert stats.cost_lines(cost)[0].startswith("This month: $0.50 + 2 unpriced calls on API keys over 2 reviews")


def test_only_estimated_api_calls_make_the_dollars_approximate():
    exact = CallUsage("a", "A", "answer", "panel", "api", "m", 10, 10, cost_usd=0.01)
    guessed_cli = CallUsage("b", "B", "answer", "panel", "plan", "", 10, 10, estimated=True)
    assert cost_line(totals([exact, guessed_cli])).startswith("$0.01 on API keys")
    guessed_api = CallUsage("c", "C", "answer", "panel", "api", "m", 10, 10, estimated=True, cost_usd=0.01)
    assert cost_line(totals([exact, guessed_api])).startswith("≈ $0.02 on API keys")


def test_a_stats_file_with_a_bom_loads_and_a_broken_one_is_kept(tmp_path):
    path = tmp_path / "stats.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"version": 1, "saves": 5}).encode())  # Notepad's UTF-8
    assert stats.load_stats(path)["saves"] == 5
    path.write_text('{"saves": 5, oops')
    stats.record_run(_costed(), path=path)
    assert (tmp_path / "stats.json.bad").read_text() == '{"saves": 5, oops'
    assert stats.load_stats(path)["reviews"] == 1


def test_a_review_that_made_no_calls_is_not_written(tmp_path):
    path = tmp_path / "stats.json"
    assert not stats.record_run(ReviewResult("q", ReviewMode.REVIEW), path=path).counted
    assert not path.exists()


def test_stats_from_before_cost_tracking_still_load(tmp_path):
    path = tmp_path / "stats.json"
    path.write_text(json.dumps({"version": 1, "runs": 3, "saves": 2, "spent_usd": 1, "months": "junk"}))
    loaded = stats.load_stats(path)
    assert loaded["spent_usd"] == 1.0 and isinstance(loaded["spent_usd"], float)  # JSON wrote 1.0 as 1
    assert loaded["months"] == {} and loaded["saves"] == 2
    stats.record_run(_costed(0.5), path=path)
    assert stats.load_stats(path)["spent_usd"] == pytest.approx(1.5)


def test_hand_edited_months_are_repaired_and_old_ones_dropped(tmp_path):
    path = tmp_path / "stats.json"
    months = {f"{year}-{m:02d}": {"reviews": 1, "spent_usd": 1.0} for year in (2020, 2021) for m in range(1, 13)}
    months["2022-01"] = {"reviews": "many", "spent_usd": -3, "saved_tokens": 7}
    path.write_text(json.dumps({"version": 1, "months": months}))
    stats.record_run(_costed(), path=path)
    kept = stats.load_stats(path)["months"]
    assert len(kept) == stats.MONTHS_KEPT and "2020-01" not in kept and "2020-02" not in kept
    assert stats._month({"months": kept}, "2022-01") == {"reviews": 0, "spent_usd": 0.0, "unpriced_calls": 0,
                                                         "saved_usd": 0.0, "saved_tokens": 7}


def test_stats_view_shows_cost_before_any_saver_run():
    summary = stats.summary({**stats._empty(), "reviews": 1, "spent_usd": 0.5})
    assert stats.cost_lines(summary["cost"]) == ["In all: $0.50 on API keys over 1 review"]
    assert "In all: $0.50 on API keys over 1 review" in _plain(review_ui.stats_view(summary))
    assert stats.cost_lines(stats.summary(stats._empty())["cost"]) == []
