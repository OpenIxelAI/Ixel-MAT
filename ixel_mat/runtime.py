"""
Shared startup for every front end (terminal, `ixel review`, plugin, GUI):
load secrets + config, pick the review panel, connect and disconnect agents.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from ixel_mat.agents import create_agent
from ixel_mat.agents.base import DEFAULT_TIMEOUT, EFFORT_LEVELS, AgentConfig, BaseAgent
from ixel_mat.config.loader import build_agent_configs, load_config
from ixel_mat.config.secrets import load_env
from ixel_mat.local_models import stays_on_your_computers
from ixel_mat.material import mask_secrets
from ixel_mat.triage import DEPTH_WHY, TriageDecision, TriageSettings, make_triage, parse_triage_settings
from ixel_mat.modes.review import ESCALATE_POLICIES, ON_WRONG_POLICIES, ReviewMode
from ixel_mat.usage import Price, billing_for, parse_pricing

CONNECT_TIMEOUT = 30.0


# "Auto": Triage (see triage.py) picks quick, review or deep for each question
AUTO = "auto"
MODE_CHOICES = (*(m.value for m in ReviewMode), AUTO)

# What a question typed without a command runs: a review mode, auto, or "compare"
# (every answer side by side, no review)
PLAIN_CHOICES = ("quick", "review", "deep", "saver", AUTO, "compare")


@dataclass
class ReviewSettings:
    mode: ReviewMode = ReviewMode.REVIEW
    moderator: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    agents: list[str] | None = None     # panel subset; None = every agent
    plain: str = "quick"                # what a question typed without a command runs
    slowest_wait: float | str = "auto"  # "auto", "always", or seconds: how long a round waits for its last model
    auto: bool = False                  # mode = "auto": Triage picks; `mode` is what runs when it can't
    private: bool = False               # only models on your own computers answer (see Settings.private)


@dataclass
class SaverSettings:
    verifier: str | None = None          # the big model that checks the drafts
    drafters: list[str] | None = None    # None = every other agent
    escalate: str = "always"             # or "disagreement": skip the verifier when drafts all agree
    verifier_effort: str | None = None   # e.g. "low": verify quickly
    on_wrong: str = "send_back"          # or "correct": the verifier fixes wrong drafts itself


@dataclass
class Settings:
    config: dict[str, Any]
    agent_configs: dict[str, AgentConfig]
    warnings: list[str] = field(default_factory=list)
    review: ReviewSettings = field(default_factory=ReviewSettings)
    saver: SaverSettings = field(default_factory=SaverSettings)
    triage: TriageSettings = field(default_factory=TriageSettings)

    @property
    def default_mode(self) -> str:
        """What a run without an explicit mode asks for: a mode name, or "auto"."""
        return AUTO if self.review.auto else self.review.mode.value

    # Private ([review] private = true): only models on your own computers answer, so a question and its
    # answers never leave them. Every other model sits out, and so does whatever would send the question
    # elsewhere: a moderator, verifier or Triage that isn't one of those models, and TypeSafe's Triage.

    @property
    def private(self) -> bool:
        return self.review.private

    def yours(self, name: str | None) -> bool:
        """Whether this agent may take part now: always, unless Private is on and it isn't on your computers."""
        if not self.private:
            return True
        cfg = self.agent_configs.get(name or "")
        return cfg is not None and stays_on_your_computers(cfg)

    def sitting_out(self) -> list[str]:
        """The panel's models that Private leaves out (their labels)."""
        if not self.private:
            return []
        return [c.label for n, c in self._panel().items() if not self.yours(n)]

    def _panel(self) -> dict[str, AgentConfig]:
        if not self.review.agents:
            return dict(self.agent_configs)
        return {n: c for n, c in self.agent_configs.items() if n in self.review.agents}

    def panel_configs(self) -> dict[str, AgentConfig]:
        """Agents that take part in /review (the [review] agents list, if set; with Private, only yours)."""
        return {n: c for n, c in self._panel().items() if self.yours(n)}

    @property
    def moderator(self) -> str | None:
        """Who writes the verdict now (None: the best-rated answer's author)."""
        return self.review.moderator if self.yours(self.review.moderator) else None

    @property
    def verifier(self) -> str | None:
        """Saver's verifier now (None: Saver can't run)."""
        return self.saver.verifier if self.yours(self.saver.verifier) else None

    @property
    def active_triage(self) -> TriageSettings:
        """Triage as it runs now: off under Private unless one of your own models makes its decisions."""
        if self.private and not (self.triage.provider == "model" and self.yours(self.triage.agent)):
            return replace(self.triage, enabled=False)
        return self.triage

    def private_problem(self, mode: ReviewMode) -> str:
        """Why Private stops a question in this mode from running ("" when it doesn't)."""
        if not self.private:
            return ""
        if not self.configs_for(mode):
            if any(self.yours(name) for name in self.agent_configs):
                where = "drafting for Saver" if mode is ReviewMode.SAVER else "on your panel"
                return (f"Private is on, and none of the models {where} runs on your own computers. Put one of "
                        "yours there in Settings, or turn Private off.")
            return ("Private is on, and none of your models runs on your own computers. Add one in Settings, "
                    "under Models on your computers, or turn Private off.")
        if mode is ReviewMode.SAVER and self.saver.verifier and not self.verifier:
            label = self.agent_configs[self.saver.verifier].label
            return (f"Private is on, and Saver's verifier ({label}) isn't on your computers. Pick one of yours to "
                    "verify in Settings, or ask in another mode.")
        return ""

    def saver_configs(self) -> dict[str, AgentConfig]:
        """Drafters plus the verifier, for saver mode."""
        verifier = self.verifier
        drafters = self.saver.drafters or [n for n in self.agent_configs if n != self.saver.verifier]
        names = [n for n in drafters if n != verifier and self.yours(n)] + ([verifier] if verifier else [])
        return {n: self.agent_configs[n] for n in names if n in self.agent_configs}

    @property
    def pricing(self) -> dict[str, Price]:
        """Your [pricing] table (dollars per million tokens), on top of the built-in prices."""
        return parse_pricing(self.config)[0]

    def configs_for(self, mode: ReviewMode) -> dict[str, AgentConfig]:
        return self.saver_configs() if mode is ReviewMode.SAVER else self.panel_configs()

    def run_options(self, mode: ReviewMode) -> dict[str, Any]:
        """Keyword arguments for run_review() in this mode."""
        options: dict[str, Any] = {"mode": mode, "moderator": self.moderator, "timeout": self.review.timeout,
                                   "slowest_wait": self.review.slowest_wait, "pricing": self.pricing}
        if mode is ReviewMode.SAVER:
            options.update(verifier=self.verifier, escalate=self.saver.escalate,
                           verifier_effort=self.saver.verifier_effort, on_wrong=self.saver.on_wrong)
        if self.active_triage.ready:
            options["triage"] = make_triage(self.active_triage, self.pricing)
        return options


async def choose_mode(settings: Settings, requested: str | None, question: str,
                      earlier=()) -> tuple[ReviewMode, TriageDecision | None]:
    """
    The mode a run uses. requested is a mode name, "auto", or None for the configured
    default. "auto" asks Triage to pick quick, review or deep; when it can't, the configured
    mode runs. The decision (None unless auto) goes to run_review(auto=…) so every
    front end shows it.
    """
    requested = requested or settings.default_mode
    if requested != AUTO:
        return ReviewMode(requested), None
    # Auto picks among quick / review / deep, so it never falls back to saver (other models)
    fallback = settings.review.mode if settings.review.mode is not ReviewMode.SAVER else ReviewMode.REVIEW
    triage = settings.active_triage
    if not triage.can_pick_mode:
        why = "[triage] auto_mode is off" if triage.ready else \
            "Private is on, and Triage isn't one of your own models" if settings.triage.ready else "it isn't set up"
        return fallback, TriageDecision("mode", ok=False, value=fallback.value, asked=False, error=why,
                                     note=f"Auto mode needs triage ({why}), so this ran in {fallback.value} mode.")
    decision = await make_triage(triage, settings.pricing).pick_mode(question, earlier)
    if not decision.ok:
        decision.value = fallback.value
        decision.note = f"Triage couldn't pick a mode ({decision.error}), so this ran in {fallback.value} mode."
        return fallback, decision
    decision.acted = True
    decision.note = f"Triage picked {decision.value} mode: {DEPTH_WHY[decision.value]}."
    return ReviewMode(decision.value), decision


def parse_review_settings(config: dict[str, Any], agent_names: set[str]) -> tuple[ReviewSettings, list[str]]:
    """Read the optional [review] table; bad values are reported and ignored."""
    section = config.get("review", {})
    settings, warnings = ReviewSettings(), []
    if not isinstance(section, dict):
        return settings, ["[review] must be a table"]

    mode = section.get("mode", settings.mode.value)
    if mode == AUTO:
        settings.auto = True  # the mode itself stays review: what runs when Triage can't pick
    else:
        try:
            settings.mode = ReviewMode(mode)
        except ValueError:
            warnings.append(f"[review] mode must be quick, review, deep, saver or auto (got {mode!r})")

    moderator = section.get("moderator")
    if moderator is not None:
        if isinstance(moderator, str) and moderator in agent_names:
            settings.moderator = moderator
        else:
            warnings.append(f"[review] moderator {moderator!r} is not a configured agent")

    timeout = section.get("timeout")
    if timeout is not None:
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
            settings.timeout = float(timeout)
        else:
            warnings.append(f"[review] timeout must be a positive number (got {timeout!r})")

    slowest = section.get("slowest_wait")
    if slowest is not None:
        if slowest in ("auto", "always") or (isinstance(slowest, (int, float)) and not isinstance(slowest, bool)
                                             and slowest > 0):
            settings.slowest_wait = slowest if isinstance(slowest, str) else float(slowest)
        else:
            warnings.append(f'[review] slowest_wait must be "auto", "always" or a number of seconds (got {slowest!r})')

    plain = section.get("plain_questions")
    if plain is not None:
        if plain in PLAIN_CHOICES:
            settings.plain = plain
        else:
            warnings.append(f"[review] plain_questions must be one of {', '.join(PLAIN_CHOICES)} (got {plain!r})")

    private = section.get("private")
    if private is not None:
        if isinstance(private, bool):
            settings.private = private
        else:
            warnings.append("[review] private must be true or false")

    agents = section.get("agents")
    if agents is not None:
        if isinstance(agents, list) and all(isinstance(a, str) for a in agents):
            unknown = [a for a in agents if a not in agent_names]
            if unknown:
                warnings.append(f"[review] agents not configured: {', '.join(unknown)}")
            settings.agents = [a for a in agents if a in agent_names]
        else:
            warnings.append("[review] agents must be a list of agent names")
    return settings, warnings


def parse_saver_settings(config: dict[str, Any], agent_names: set[str],
                         review: ReviewSettings | None = None) -> tuple[SaverSettings, list[str]]:
    """
    Read the optional [saver] table (verifier defaults to the [review] moderator). Without a
    verifier, a `plain_questions = "saver"` in `review` falls back to quick, with a warning.
    """
    section = config.get("saver", {})
    settings, warnings = SaverSettings(), []
    if not isinstance(section, dict):
        return settings, ["[saver] must be a table"]

    verifier = section.get("verifier", review.moderator if review else None)
    if verifier is not None:
        if isinstance(verifier, str) and verifier in agent_names:
            settings.verifier = verifier
        else:
            warnings.append(f"[saver] verifier {verifier!r} is not a configured agent")

    drafters = section.get("drafters")
    if drafters is not None:
        if isinstance(drafters, list) and all(isinstance(d, str) for d in drafters):
            unknown = [d for d in drafters if d not in agent_names]
            if unknown:
                warnings.append(f"[saver] drafters not configured: {', '.join(unknown)}")
            settings.drafters = [d for d in drafters if d in agent_names and d != settings.verifier]
        else:
            warnings.append("[saver] drafters must be a list of agent names")

    escalate = section.get("escalate", "always")
    if escalate in ESCALATE_POLICIES:
        settings.escalate = escalate
    else:
        warnings.append(f"[saver] escalate must be one of {', '.join(ESCALATE_POLICIES)}")

    on_wrong = section.get("on_wrong", "send_back")
    if on_wrong in ON_WRONG_POLICIES:
        settings.on_wrong = on_wrong
    else:
        warnings.append(f"[saver] on_wrong must be one of {', '.join(ON_WRONG_POLICIES)}")

    effort = section.get("verifier_effort")
    if effort is not None:
        if effort in EFFORT_LEVELS:
            settings.verifier_effort = effort
        else:
            warnings.append(f"[saver] verifier_effort must be one of {', '.join(EFFORT_LEVELS)}")

    if review is not None and review.plain == "saver" and settings.verifier is None:
        warnings.append('[review] plain_questions = "saver" needs a verifier (set [saver] verifier); '
                        'questions get a quick review instead')
        review.plain = "quick"
    return settings, warnings


def load_settings(explicit_path: str | None = None, wait: bool = True) -> Settings:
    """Every setting, with the saved keys loaded first. wait=False: for the app, whose requests mustn't wait
    for a password prompt or another save (see secrets.load_env)."""
    load_env(wait=wait)
    return settings_from(load_config(explicit_path))


def settings_from(config: dict[str, Any]) -> Settings:
    """Every setting from a config that's been read, with what's wrong with it in warnings."""
    agent_configs, warnings = build_agent_configs(config)
    review, review_warnings = parse_review_settings(config, set(agent_configs))
    saver, saver_warnings = parse_saver_settings(config, set(agent_configs), review)
    triage, triage_warnings = parse_triage_settings(config, agent_configs)
    return Settings(config, agent_configs, warnings + review_warnings + saver_warnings + triage_warnings
                    + auto_warnings(review, triage) + parse_pricing(config)[1], review, saver, triage)


def auto_warnings(review: ReviewSettings, triage: TriageSettings) -> list[str]:
    """Auto mode configured, but Triage can't pick: say what runs instead."""
    if triage.can_pick_mode or not (review.auto or review.plain == AUTO):
        return []
    why = "turn on [triage] auto_mode" if triage.ready else "set up [triage]"
    return [f'[review] "auto" needs Triage to pick the mode ({why}); until then questions run in '
            f"{review.mode.value} mode"]


def local_agent_names(configs: dict[str, AgentConfig]) -> set[str]:
    """Models on this machine or your own network (Ollama, LM Studio, …): the 'local' models, the ones
    a review's cost counts as free (usage.billing_for, so a `billing` setting counts here too)."""
    return {name for name, cfg in configs.items() if billing_for(SimpleNamespace(config=cfg)) == "local"}


ConnectCallback = Callable[[AgentConfig, "Exception | None"], "Awaitable[None] | None"]


async def connect_agents(
    configs: dict[str, AgentConfig],
    on_result: ConnectCallback | None = None,
    timeout: float = CONNECT_TIMEOUT,
) -> dict[str, BaseAgent]:
    """
    Connect all agents in parallel; failures are reported via on_result and skipped.
    If this is cancelled (the browser page closed while models connect), every agent
    it connected, or was connecting, is disconnected before the cancellation goes on.
    """
    connected: dict[str, BaseAgent] = {}

    async def one(name: str, cfg: AgentConfig):
        error: Exception | None = None
        agent = None
        try:
            agent = create_agent(cfg)
            await asyncio.wait_for(agent.connect(), timeout=timeout)
            connected[name] = agent
        except asyncio.CancelledError:
            if agent is not None:
                with suppress(Exception):
                    await agent.disconnect()
            raise
        except Exception as exc:  # noqa: BLE001 — reported to the caller
            said = mask_secrets(str(exc), [cfg.token])  # a server's error can quote the key it was sent
            error = (exc if said == str(exc) else RuntimeError(said)) if said else \
                RuntimeError(f"{type(exc).__name__} while connecting")
            if agent is not None:
                try:
                    await agent.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            agent = None
        if on_result is not None:
            outcome = on_result(cfg, error)
            if asyncio.iscoroutine(outcome):
                await outcome
        return name, agent

    try:
        results = await asyncio.gather(*(one(n, c) for n, c in configs.items()))
    except BaseException:
        await disconnect_agents(connected)
        raise
    return {name: agent for name, agent in results if agent is not None}


async def disconnect_agents(agents: dict[str, BaseAgent]) -> None:
    async def one(agent: BaseAgent):
        try:
            await agent.disconnect()
        except Exception:  # noqa: BLE001 — shutting down anyway
            pass

    await asyncio.gather(*(one(a) for a in agents.values()))
