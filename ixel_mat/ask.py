"""
`ixel ask --agent NAME`: one of your models answers one question, with no panel and no review.

It's how other tools (Handoff's dispatch, the app's /handoff) give a job to one model you set
up in Ixel: "gemini, make me a list of projects to check out", or "codex, review my changes".
`--agent codex,claude,local` names a few, in the order you want them: when one is out of usage,
the next one answers (limits.py says what counts).
The model gets the same answer-only prompt a panel member gets, and code or files you attach
are fenced as material, so instructions inside them are something to read, not to follow.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from urllib.parse import urlparse

from ixel_mat.agents import create_agent
from ixel_mat.agents.base import AgentConfig, needs_api_key
from ixel_mat.limits import USAGE_LIMIT, UsageLimit
from ixel_mat.material import MAX_MATERIAL_CHARS, Material, mark_hidden_characters, mask_secrets
from ixel_mat.modes.review import (_UNTRUSTED_NOTE, ANSWER_PROMPT, MATERIAL_ANSWER_PROMPT, _clip, _fenced,
                                   _unfenced)
from ixel_mat.runtime import CONNECT_TIMEOUT
from ixel_mat.usage import Usage, billing_for, call_usage, configured_model


class AskError(ValueError):
    """Something to tell the person; nothing was asked. kind is USAGE_LIMIT when the model is out of usage;
    skipped, the models before it that were (see ask_in_order)."""

    def __init__(self, message: str, kind: str = "", skipped: list[dict] | None = None):
        super().__init__(message)
        self.kind = kind
        self.skipped = list(skipped or [])


@dataclass
class AskResult:
    agent: str
    label: str
    model: str
    answer: str
    ms: int
    usage: dict | None = None
    material: dict | None = None
    notes: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)  # models asked first that were out of usage

    def to_dict(self) -> dict:
        data = {"agent": self.agent, "label": self.label, "model": self.model, "answer": self.answer, "ms": self.ms}
        if self.usage is not None:
            data["usage"] = self.usage
        if self.material is not None:
            data["material"] = self.material
        if self.notes:
            data["notes"] = list(self.notes)
        if self.skipped:
            data["skipped"] = list(self.skipped)
        return data


def _key(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def find_agent(configs: dict[str, AgentConfig], wanted: str) -> AgentConfig:
    """The agent called `wanted`: its name, else its label ("Grok"), else the one name or label it starts."""
    wanted = wanted.strip()
    if not configs:
        raise AskError("No models are set up yet. Run: ixel setup")
    if wanted in configs:
        return configs[wanted]
    key = _key(wanted)
    if key:
        for match in ([c for c in configs.values() if _key(c.name) == key or _key(c.label) == key],
                      [c for c in configs.values() if _key(c.name).startswith(key) or _key(c.label).startswith(key)]):
            if len(match) == 1:
                return match[0]
            if len(match) > 1:
                names = ", ".join(c.name for c in match)
                raise AskError(f"“{wanted}” could be {names}. Name one of them.")
    names = ", ".join(f"{c.name} ({c.label})" if _key(c.label) != _key(c.name) else c.name for c in configs.values())
    raise AskError(f"There's no model called “{wanted}” in your Ixel setup. You have: {names}. "
                   "ixel setup adds more.")


def is_ready(cfg: AgentConfig) -> bool:
    """Set up enough to try: a key where one's needed (whether it works is `ixel agents`' job)."""
    return bool(cfg.token) or not needs_api_key(cfg)


def image_provider_of(cfg: AgentConfig) -> str | None:
    """The `ixel image` provider at the same company as this model (Grok: xai), if there is one."""
    from ixel_mat.images import PROVIDERS
    try:
        host = urlparse(cfg.url).hostname if cfg.type == "http" else None
    except ValueError:
        return None
    return next((p.name for p in PROVIDERS.values() if host and host == p.chat_host), None)


def agent_list(configs: dict[str, AgentConfig]) -> list[dict]:
    """Every agent, for `ixel ask --list` (no calls are made). "images" names the `ixel image` provider
    at the same company, for tools that let "grok, make pictures" mean xAI's image model."""
    out = []
    for cfg in configs.values():
        out.append({"name": cfg.name, "label": cfg.label, "type": cfg.type, "model": cfg.model,
                    "billing": billing_for(SimpleNamespace(config=cfg)), "ready": is_ready(cfg),
                    "images": image_provider_of(cfg)})
    return out


def build_prompt(question: str, material: Material | None = None, fence: str | None = None) -> str:
    """The panel's answer prompt, for one model: the material fenced, the question after it."""
    if material is None:
        return ANSWER_PROMPT.format(question=question)
    fence = fence or f"IXEL-{secrets.token_hex(6)}"
    text = _clip(_unfenced(mark_hidden_characters(material.text), fence), MAX_MATERIAL_CHARS)
    title = _unfenced(material.title, fence).replace("\n", " ")
    block = _fenced(fence, f"material: {title} (the question below is about this)", text)
    return MATERIAL_ANSWER_PROMPT.format(title=title, untrusted=_UNTRUSTED_NOTE.format(fence=fence), earlier="",
                                         material=block, question=question)


async def ask(cfg: AgentConfig, question: str, material: Material | None = None,
              timeout: float | None = None) -> AskResult:
    """Ask one agent. AskError if it can't connect or doesn't answer."""
    if not is_ready(cfg):
        raise AskError(f"{cfg.label} has no API key yet. Run: ixel setup")
    prompt = build_prompt(question, material)
    timeout = timeout or cfg.call_timeout
    agent = create_agent(cfg)
    reported: list[Usage] = []
    reply = None
    started = time.perf_counter()
    try:
        try:
            await asyncio.wait_for(agent.connect(), timeout=CONNECT_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise AskError(f"{cfg.label} didn't connect within {int(CONNECT_TIMEOUT)} seconds.") from exc
        except AskError:
            raise
        except Exception as exc:  # noqa: BLE001 — the transport's own words, for the person
            raise AskError(f"Couldn't connect to {cfg.label}: "
                           f"{mask_secrets(str(exc), [cfg.token]) or type(exc).__name__}") from exc
        try:
            reply = await asyncio.wait_for(agent.send_and_receive(prompt, use_full_session=True,
                                                                  on_usage=reported.append), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise AskError(f"{cfg.label} didn't answer within {int(timeout)} seconds.") from exc
        except Exception as exc:  # noqa: BLE001
            said = mask_secrets(str(exc), [cfg.token]) or type(exc).__name__
            if isinstance(exc, UsageLimit):  # the transport read the tool's own error; str(exc) can hold more
                raise AskError(f"{cfg.label} is out of usage: {said}", kind=USAGE_LIMIT) from exc
            raise AskError(f"{cfg.label} failed: {said}") from exc
    finally:
        try:
            await agent.disconnect()
        except Exception:  # noqa: BLE001
            pass
    answer = (reply or "").strip()
    if not answer:
        raise AskError(f"{cfg.label} sent back an empty answer.")
    record = call_usage(agent, "ask", "panel", prompt, answer, reported)
    return AskResult(agent=cfg.name, label=cfg.label, model=configured_model(agent), answer=answer,
                     ms=int((time.perf_counter() - started) * 1000),
                     usage=record.to_dict() if record is not None else None,
                     material=material.to_dict() if material is not None else None,
                     notes=list(material.notes) if material is not None else [])


async def ask_in_order(cfgs: list[AgentConfig], question: str, material: Material | None = None,
                       timeout: float | None = None,
                       on_skip: Callable[[AgentConfig, AgentConfig, AskError], None] | None = None) -> AskResult:
    """Ask the first model; when it's out of usage, the next, and so on. Any other failure stops there, since
    it needs fixing and the next model answering would hide it. on_skip(skipped, next, why) is told each move."""
    skipped: list[dict] = []
    for i, cfg in enumerate(cfgs):
        try:
            result = await ask(cfg, question, material, timeout=timeout)
        except AskError as exc:
            last = i + 1 == len(cfgs)
            if exc.kind != USAGE_LIMIT or (last and not skipped):  # another failure, or the one model named
                raise AskError(str(exc), kind=exc.kind, skipped=skipped) from exc
            skipped.append({"agent": cfg.name, "label": cfg.label, "error_kind": USAGE_LIMIT, "error": str(exc)})
            if last:
                raise AskError("Every model you listed is out of usage for now: "
                               f"{', '.join(c.label for c in cfgs)}.", kind=USAGE_LIMIT, skipped=skipped) from exc
            if on_skip is not None:
                on_skip(cfg, cfgs[i + 1], exc)
            continue
        result.skipped = skipped
        return result
    raise AskError("No model to ask.")
