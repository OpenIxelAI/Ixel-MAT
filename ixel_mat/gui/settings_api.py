"""
The app's Settings page: what it shows, and the few things it may change.

It changes choices among things already set up: which models sit on the panel and which model and
effort each uses, the default mode and moderator, Saver, Triage and the picture service. Beyond that it
manages only model servers on your own computers (Ollama, LM Studio…): it can add one of their models,
point one at another of your computers, or take one off. Such a model is never given a key, and its address
must be this computer or one on your own network, checked when it's saved. Nothing else about what runs or
where things are sent (an agent's command, arguments, environment or which key it uses, or any other
address) can change here: those stay in `ixel setup` and the file itself, so a page that went wrong still
couldn't run a program or send a key somewhere new.

Keys are write-only: the page can save, replace or remove one (only names Ixel knows), never read one.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from typing import Callable
from urllib.parse import urlparse

from ixel_mat import effort, local_models, sound
from ixel_mat.agents.base import EFFORT_LEVELS, is_loopback_host, needs_api_key
from ixel_mat.config import edit, secrets
from ixel_mat.models import ALIASES, provider_for_url, valid_model_id
from ixel_mat.modes.review import ESCALATE_POLICIES as ESCALATE
from ixel_mat.modes.review import ON_WRONG_POLICIES as ON_WRONG
from ixel_mat.presets import preset_for
from ixel_mat.runtime import MODE_CHOICES as MODES
from ixel_mat.runtime import PLAIN_CHOICES as PLAIN

TRIAGE_PROVIDERS = ("typesafe", "model")
KEY_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
KEY_ENDINGS = ("_KEY", "_TOKEN")
MAX_KEY_CHARS = 400
MIN_TIMEOUT, MAX_TIMEOUT = 10, 3600


class SettingsError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ── Reading ───────────────────────────────────────────────────────────────────

def _path(settings) -> Path | None:
    source = settings.config.get("_source", "none")
    return Path(source) if source not in ("none", "", None) and Path(source).is_file() else None


def _raw_agents(settings) -> dict[str, dict]:
    agents = settings.config.get("agents", {})
    return {n: a for n, a in agents.items() if isinstance(a, dict)} if isinstance(agents, dict) else {}


def _kind(cfg) -> str:
    if cfg.type == "oneshot" or cfg.type == "subprocess":
        return "cli"
    if cfg.type == "websocket":
        return "gateway"
    return "api"


def known_keys(settings) -> list[dict]:
    """The keys the page may set: the providers `ixel setup` knows, the picture and sound services, Triage's,
    and the ones your agents name (only names that look like a key or token)."""
    from ixel_mat import images
    from ixel_mat.config.setup import PROVIDERS
    found: dict[str, dict] = {}

    def add(name: str, label: str, user: str = "") -> None:
        if not (isinstance(name, str) and KEY_NAME.match(name) and name.endswith(KEY_ENDINGS)):
            return
        entry = found.setdefault(name, {"name": name, "label": label, "used_by": []})
        if user and user not in entry["used_by"]:
            entry["used_by"].append(user)

    for provider in PROVIDERS:
        add(provider["env_name"], provider["name"])
    for provider in images.PROVIDERS.values():
        add(provider.env, provider.label)
    for provider in sound.PROVIDERS.values():
        add(provider.env, provider.label)
    add(settings.triage.token_env, "TypeSafe (Triage)")
    for name, raw in _raw_agents(settings).items():
        cfg = settings.agent_configs.get(name)
        add(raw.get("token_env", ""), raw.get("label", name), cfg.label if cfg else name)
        if cfg and preset_for(cfg.command).get("id") == "gemini_cli":  # given it while it has no sign-in of its own
            add("GOOGLE_API_KEY", "Google (Gemini)", cfg.label)
    for names, use in ((("XAI_API_KEY", "OPENAI_API_KEY"), "Pictures"), (("OPENAI_API_KEY", "GROQ_API_KEY"), "Sound")):
        for name in names:
            if name in found and use not in found[name]["used_by"]:
                found[name]["used_by"].append(use)
    saved = secrets.saved_names()
    return [{**entry, "state": secrets.key_state(entry["name"]), "saved": entry["name"] in saved}
            for entry in found.values()]


def _holds_secrets(config: dict) -> bool:
    """A key written into config.toml itself (an agent's token or env, Triage's token): the backup copy
    would keep it after it's taken out, so there's no backup then."""
    agents = config.get("agents") if isinstance(config.get("agents"), dict) else {}
    if any(isinstance(a, dict) and (a.get("token") or a.get("env")) for a in agents.values()):
        return True
    triage = config.get("triage")
    return isinstance(triage, dict) and bool(triage.get("token"))


def settings_path(settings) -> Path | None:
    return _path(settings)


def snapshot(settings, version: str | None = None) -> dict:
    """Everything the page shows. `version` is the file's, read before `settings` were (so a change in
    between makes the page's next edit be refused, never applied to settings it didn't see)."""
    from ixel_mat import images
    path = _path(settings)
    if path is not None and version is None:
        version = edit.file_version(path)
    problem = ""
    if settings.config.get("_error"):
        problem = f"Ixel can't read its settings file ({settings.config['_error']}). Fix the file, or write a new " \
                  "one with ixel setup."
    elif path is None:
        problem = "There's no settings file yet. Run ixel setup to add your models; keys can be saved here already."
    raw = _raw_agents(settings)
    panel = set(settings.review.agents) if settings.review.agents else None
    agents = []
    for name, cfg in settings.agent_configs.items():
        levels = effort.agent_levels(cfg)
        agents.append({
            "name": name, "label": cfg.label, "kind": _kind(cfg), "model": raw.get(name, {}).get("model", "") or "",
            # Only the levels this model (or program) takes; one set before that it doesn't is sent as the nearest
            "effort": cfg.effort or "", "has_effort": bool(levels), "efforts": list(levels),
            "effort_sent": effort.to_send(levels, cfg.effort) or "", "effort_note": _effort_note(cfg, levels),
            # A model server of your own has no default to fall back on: a model has to be named
            "needs_model": cfg.type == "http" and not _follows_newest(cfg),
            "on_panel": panel is None or name in panel, "in_file": name in raw,
            # Pictures go only to models Ixel calls itself (never to a program such as Claude Code)
            "can_see": cfg.type == "http", "pictures": cfg.sees_pictures,
            # A model server on your own computers: its address can change here, and it can be taken off
            "server": _server_info(cfg) if own_server(cfg, raw.get(name, {})) else None,
            # Answers with Private on: a question to it stays on your computers
            "yours": local_models.stays_on_your_computers(cfg),
        })
    section = settings.config.get("images") if isinstance(settings.config.get("images"), dict) else {}
    image_provider = section.get("provider", "")
    image_provider = image_provider.strip().lower() if isinstance(image_provider, str) else ""
    return {
        "source": str(path) if path else "",
        "version": version if path else "",
        "editable": path is not None and not settings.config.get("_error"),
        # A model on your own computers can be added even before there's a file (it makes one)
        "can_add": not settings.config.get("_error"),
        "backup": not _holds_secrets(settings.config),
        "problem": problem,
        "warnings": list(settings.warnings),
        "agents": agents,
        "review": {"mode": settings.default_mode, "moderator": settings.review.moderator or "",
                   "plain_questions": settings.review.plain, "timeout": settings.review.timeout,
                   "private": settings.private},
        "saver": {"verifier": settings.saver.verifier or "", "verifier_set": _raw_verifier(settings),
                  "drafters": settings.saver.drafters or [],
                  "escalate": settings.saver.escalate, "on_wrong": settings.saver.on_wrong},
        "triage": {"enabled": settings.triage.enabled, "provider": settings.triage.provider,
                   "agent": settings.triage.agent, "auto_mode": settings.triage.auto_mode,
                   "skip_review": settings.triage.skip_review, "ready": settings.triage.ready,
                   "key": settings.triage.token_env, "host": settings.triage.host,
                   "official": settings.triage.official},
        "images": {"provider": image_provider if image_provider in images.PROVIDERS else ""},
        "sound": _sound(settings),
        "keys": known_keys(settings),
        "choices": {"modes": list(MODES), "plain": list(PLAIN), "escalate": list(ESCALATE),
                    "on_wrong": list(ON_WRONG), "efforts": list(EFFORT_LEVELS),
                    "triage_providers": list(TRIAGE_PROVIDERS),
                    "image_providers": [{"name": p.name, "label": p.label} for p in images.PROVIDERS.values()],
                    "sound_providers": [{"name": p.name, "label": p.label} for p in sound.PROVIDERS.values()]},
    }


# ── Model servers of your own ─────────────────────────────────────────────────

def own_server(cfg, raw: dict) -> bool:
    """A model server on your own computers that's given no key: the kind of model Settings adds, can point
    at another of your computers, and can take off."""
    if cfg.type != "http" or not isinstance(raw, dict) or cfg.token or provider_for_url(cfg.url):
        return False
    if any(raw.get(k) for k in ("token_env", "token", "env", "preset")):
        return False
    return not needs_api_key(cfg)


def _where(url: str) -> str:
    """What a server's computer is called: "this computer", or its name or address."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return url
    return "this computer" if is_loopback_host(host) else host


def _server_info(cfg) -> dict:
    return {"url": local_models.base_url(cfg.url), "where": _where(cfg.url)}


def _same_server(a: str, b: str) -> bool:
    def norm(url: str) -> str:
        url = local_models.base_url(url).lower().rstrip("/")
        return url.replace("://localhost:", "://127.0.0.1:")
    return norm(a) == norm(b)


def _added(settings, base: str, model: str) -> str:
    """The model already on your list that is `model` at `base`, or ""."""
    for name, cfg in settings.agent_configs.items():
        if cfg.type == "http" and _same_server(cfg.url, base) and (cfg.model == model or (
                ":" not in cfg.model and model == f"{cfg.model}:latest")):  # Ollama's llama3.2 is llama3.2:latest
            return name
    return ""


def look_for_servers(settings, body: Any) -> dict:
    """The model servers on this computer (no address), or at the address typed, with which of their models
    are already on your list. Looks nowhere else."""
    address = body.get("address") if isinstance(body, dict) else None
    if address not in (None, "") and not isinstance(address, str):
        raise SettingsError("Type the computer's name or address.")
    if address:
        try:
            servers = local_models.at_address(address)
        except local_models.AddressError as exc:
            raise SettingsError(str(exc), 404) from None
    else:
        servers = local_models.on_this_computer()
        if not servers:
            raise SettingsError("No model server is running on this computer. Start Ollama, or LM Studio's "
                                "server, then look again.", 404)
    return {"servers": [{**server.to_dict(), "where": _where(server.base),
                         "added": {m: _added(settings, server.base, m) for m in server.models}}
                        for server in servers]}


def _check_server(base: Any) -> local_models.Server:
    """The server at base, asked now; SettingsError unless it's on your own computers and answers."""
    if not isinstance(base, str) or not base:
        raise SettingsError("Which server? Look for it first.")
    try:
        candidates = local_models.addresses_to_try(base)
    except local_models.AddressError as exc:
        raise SettingsError(str(exc)) from None
    if len(candidates) != 1:
        raise SettingsError("Say which port too, like mac-mini:1234.")
    name, url = candidates[0]
    host = urlparse(url).hostname
    if local_models.where_is(host) != "yours":
        raise SettingsError(f"{host} isn't on this computer or your own network, so Settings won't send "
                            "questions there. A server elsewhere goes in your settings file by hand.")
    try:
        local_models.usable_name(host)
    except local_models.AddressError as exc:
        raise SettingsError(str(exc)) from None
    server = local_models.ask_server(name, url, local_models.REMOTE_TIMEOUT)
    if server is None:
        if _where(url) == "this computer":
            raise SettingsError(f"No model server answered at {url}. Is it running?", 404)
        raise SettingsError(f"No model server answered at {url}. Is it running, and does it let other computers "
                            f"in? {local_models.OPEN_TO_NETWORK}", 404)
    return server


def _auto_label(model: str, url: str) -> str:
    """The name Ixel gives a model it adds: qwen3:14b (local), qwen3:14b (mac-mini)."""
    where = _where(url)
    return f"{model} ({'local' if where == 'this computer' else where})"


def plan_pull(body: Any) -> tuple[str, str]:
    """(Ollama's address, the model's name) for getting a model onto an Ollama of yours; SettingsError if not."""
    if not isinstance(body, dict) or set(body) != {"base", "model"}:
        raise SettingsError("Which server, and which model?")
    try:
        name = local_models.model_to_get(body["model"])
    except local_models.PullError as exc:
        raise SettingsError(str(exc)) from None
    server = _check_server(body["base"])
    if not server.ollama:
        raise SettingsError(f"{server.name} at {_where(server.base)} isn't Ollama, so Ixel can't get models for it by name. "
                            "Download them in the app itself.")
    return local_models.server_root(server.base), name


def _plan_add(settings, values: Any) -> tuple[Callable[[str], str], str]:
    """The edit that adds a model from a server of yours (and puts it on the panel), and what to say."""
    from ixel_mat.config.setup import _agent_id
    if not isinstance(values, dict) or set(values) != {"base", "model"}:
        raise SettingsError("Nothing to add.")
    model = values["model"]
    if not (isinstance(model, str) and valid_model_id(model)):
        raise SettingsError("That isn't a model name.")
    server = _check_server(values["base"])
    if model not in server.models:
        raise SettingsError(f"{server.name} at {_where(server.base)} has no model called {model} that answers "
                            "questions. Look again for what it has now.", 404)
    if _added(settings, server.base, model):
        raise SettingsError(f"{model} on {_where(server.base)} is already on your list.")
    raw = _raw_agents(settings)
    agent = _agent_id(model, set(raw) | set(settings.agent_configs))
    label = _auto_label(model, server.base)
    table = {"type": "http", "url": f"{server.base}/chat/completions", "model": model, "label": label,
             "color": "yellow"}
    panel = settings.config.get("review", {}).get("agents") if isinstance(settings.config.get("review"), dict) else None

    def change(text: str) -> str:
        text = edit.set_values(text, ("agents", agent), table)
        if isinstance(panel, list) and panel:  # the panel is a list in the file: the new model joins it (an
            # empty one means every model, so it stays as it is)
            text = edit.set_values(text, ("review",), {"agents": [*panel, agent]})
        return text
    return change, f"Added {label}. It's on the panel."


def _plan_remove(settings, agent: Any) -> tuple[Callable[[str], str], str]:
    raw = _raw_agents(settings)
    if not isinstance(agent, str) or agent not in raw or agent not in settings.agent_configs:
        raise SettingsError("That isn't one of your models.")
    cfg = settings.agent_configs[agent]
    if not own_server(cfg, raw[agent]):
        raise SettingsError(f"{cfg.label} isn't a model server on your own computers, so it's taken off with "
                            "ixel setup or in the settings file.")
    if len(settings.agent_configs) == 1:
        raise SettingsError(f"{cfg.label} is your only model.")
    deciding = settings.triage.agent if settings.triage.enabled and settings.triage.provider == "model" else ""
    uses = [what for what, name in (("writes the verdict", settings.review.moderator),
                                    ("verifies under Saver", _raw_verifier(settings)),
                                    ("makes Triage's decisions", deciding)) if name == agent]
    if uses:
        raise SettingsError(f"{cfg.label} {uses[0]}. Pick another model for that first.")
    lists = []
    for table, key, what in (("review", "agents", "on the panel"), ("saver", "drafters", "drafting for Saver")):
        section = settings.config.get(table)
        names = section.get(key) if isinstance(section, dict) else None
        if isinstance(names, list) and agent in names:
            rest = [n for n in names if n != agent]
            # Counted as Ixel reads the list: models you have, and a drafter isn't the one verifying
            counted = [n for n in rest if n in settings.agent_configs
                       and (table == "review" or n != settings.saver.verifier)]
            if not counted:  # an empty list would mean every model, the companies' too
                raise SettingsError(f"{cfg.label} is the only model {what}. Put another one there first.")
            lists.append(((table,), {key: rest}))
    triage = settings.config.get("triage") if isinstance(settings.config.get("triage"), dict) else {}
    if triage.get("agent") == agent:  # named, but not deciding (TypeSafe decides, or Triage is off)
        values = {"agent": edit.REMOVE}
        if "provider" not in triage:  # without an agent the file would mean TypeSafe: say "model" outright
            values["provider"] = "model"
        lists.append((("triage",), values))

    def change(text: str) -> str:
        text = edit.remove_table(text, ("agents", agent))
        for table, values in lists:
            text = edit.set_values(text, table, values)
        return text
    return change, f"Took {cfg.label} off your list."


def _effort_note(cfg, levels) -> str:
    """Why there's no Effort to pick, when there isn't."""
    if levels or cfg.type not in ("http", "oneshot"):
        return ""
    if cfg.type == "oneshot":
        return f"{cfg.label} doesn't take an effort level from Ixel; it thinks as it's set up to."
    return f"{cfg.model or cfg.label} has no effort setting, so Ixel sends none."


def _sound(settings) -> dict:
    """[sound] provider as the file says it, the service sound goes to now, and else what's needed first."""
    chosen = sound.chosen(settings.config)
    now, problem = sound.ready(settings.config)
    return {"provider": chosen if chosen in sound.PROVIDERS else "", "using": now.label if now else "",
            "problem": problem}


def _raw_verifier(settings) -> str:
    """[saver] verifier as the file says it ("" when it's left to be the moderator)."""
    section = settings.config.get("saver")
    value = section.get("verifier") if isinstance(section, dict) else None
    return value if isinstance(value, str) and value in settings.agent_configs else ""


# ── Changing ──────────────────────────────────────────────────────────────────

def _agent_name(value: Any, settings, allow_none: bool = True) -> str | None:
    if allow_none and value in ("", None):
        return edit.REMOVE
    if isinstance(value, str) and value in settings.agent_configs:
        return value
    raise SettingsError("That isn't one of your models.")


def _choice(value: Any, choices: tuple[str, ...], what: str) -> str:
    if value in choices:
        return value
    raise SettingsError(f"{what} must be one of: {', '.join(choices)}.")


def _flag(value: Any, what: str) -> bool:
    if isinstance(value, bool):
        return value
    raise SettingsError(f"{what} must be on or off.")


def _names(value: Any, settings, what: str) -> list[str]:
    if not (isinstance(value, list) and value and all(isinstance(v, str) for v in value)):
        raise SettingsError(f"Pick at least one model for {what}.")
    unknown = [v for v in value if v not in settings.agent_configs]
    if unknown:
        raise SettingsError(f"{', '.join(unknown)} isn't one of your models.")
    return list(dict.fromkeys(value))


def _follows_newest(cfg) -> bool:
    """latest and latest-fast only work on a big company's API, where the names say which model is newest."""
    return cfg.type == "http" and provider_for_url(cfg.url) is not None


def plan_change(settings, section: str, values: Any, agent: Any = None) -> tuple[tuple[str, ...], dict]:
    """The table and the values a change from the page writes, each one checked; SettingsError if not."""
    if not isinstance(values, dict) or not values:
        raise SettingsError("Nothing to change.")
    out: dict[str, Any] = {}
    if section == "agent":
        if not isinstance(agent, str) or agent not in _raw_agents(settings) or agent not in settings.agent_configs:
            raise SettingsError("That isn't one of your models.")
        for key, value in values.items():
            if key == "model":
                cfg = settings.agent_configs[agent]
                if value in ("", None, "default"):
                    if cfg.type == "http" and not _follows_newest(cfg):
                        raise SettingsError(f"{cfg.label} needs a model name, since its server has no default "
                                            "for Ixel to use. Pick one.")
                    out["model"] = edit.REMOVE
                elif value in ALIASES and not _follows_newest(cfg):
                    raise SettingsError(f"{cfg.label} has no list of newest models to pick from, so {value} "
                                        "can't work there. Pick a model by name.")
                elif isinstance(value, str) and len(value) <= 200 and (value in ALIASES or valid_model_id(value)):
                    out["model"] = value
                else:
                    raise SettingsError(f"{value!r} isn't a model name.")
            elif key == "effort":
                out["effort"] = edit.REMOVE if value in ("", None) else _choice(value, EFFORT_LEVELS, "Effort")
            elif key == "url":
                cfg = settings.agent_configs[agent]
                if not own_server(cfg, _raw_agents(settings)[agent]):
                    raise SettingsError(f"{cfg.label}'s address can't be changed here, since it isn't a model "
                                        "server on your own computers. Change it in the settings file.")
                server = _check_server(value)
                if cfg.model and not server.has(cfg.model):
                    raise SettingsError(f"{server.name} at {_where(server.base)} has no model called {cfg.model}. "
                                        "Get it there first, or add one of the models it has.", 404)
                out["url"] = f"{server.base}/chat/completions"
                label = _raw_agents(settings)[agent].get("label")
                if label == _auto_label(cfg.model, cfg.url):  # the name Ixel gave it says where it is
                    out["label"] = _auto_label(cfg.model, server.base)
            elif key == "pictures":
                cfg = settings.agent_configs[agent]
                if not isinstance(value, bool):
                    raise SettingsError("Seeing pictures is on or off.")
                if cfg.type != "http":
                    raise SettingsError(f"{cfg.label} is a program Ixel starts; pictures only go to models it "
                                        "calls itself.")
                others = [kind for kind in (cfg.accepts or []) if kind != "image"]
                out["accepts"] = (["image"] if value else []) + others
            else:
                raise SettingsError(f"{key} can't be changed here.")
        return ("agents", agent), out
    if section == "panel":
        if set(values) != {"on"}:
            raise SettingsError("Nothing to change.")
        on = _names(values["on"], settings, "the panel")
        out["agents"] = edit.REMOVE if set(on) == set(settings.agent_configs) else on
        return ("review",), out
    if section == "review":
        for key, value in values.items():
            if key == "mode":
                out["mode"] = _choice(value, MODES, "The mode")
            elif key == "moderator":
                out["moderator"] = _agent_name(value, settings)
            elif key == "plain_questions":
                out["plain_questions"] = _choice(value, PLAIN, "Plain questions")
            elif key == "private":
                out["private"] = _flag(value, "Private") or edit.REMOVE
            elif key == "timeout":
                if isinstance(value, bool) or not isinstance(value, (int, float)) \
                        or not MIN_TIMEOUT <= value <= MAX_TIMEOUT:
                    raise SettingsError(f"The time limit must be between {MIN_TIMEOUT} and {MAX_TIMEOUT} seconds.")
                out["timeout"] = int(value) if float(value).is_integer() else float(value)
            else:
                raise SettingsError(f"{key} can't be changed here.")
        return ("review",), out
    if section == "saver":
        for key, value in values.items():
            if key == "verifier":
                out["verifier"] = _agent_name(value, settings)
            elif key == "drafters":
                out["drafters"] = edit.REMOVE if value is None else _names(value, settings, "the drafts")
            elif key == "escalate":
                out["escalate"] = _choice(value, ESCALATE, "When to check")
            elif key == "on_wrong":
                out["on_wrong"] = _choice(value, ON_WRONG, "What to do with a wrong draft")
            else:
                raise SettingsError(f"{key} can't be changed here.")
        return ("saver",), out
    if section == "triage":
        for key, value in values.items():
            if key in ("enabled", "auto_mode", "skip_review"):
                out[key] = _flag(value, key)
            elif key == "provider":
                out["provider"] = _choice(value, TRIAGE_PROVIDERS, "Triage's provider")
            elif key == "agent":
                out["agent"] = _agent_name(value, settings)
                if out["agent"] is edit.REMOVE and "provider" not in values and settings.triage.provider == "model":
                    out["provider"] = "model"  # without an agent the file would mean TypeSafe: say "model" outright
            else:
                raise SettingsError(f"{key} can't be changed here.")
        return ("triage",), out
    if section == "images":
        from ixel_mat import images
        for key, value in values.items():
            if key != "provider":
                raise SettingsError(f"{key} can't be changed here.")
            out["provider"] = edit.REMOVE if value in ("", None) else _choice(value, tuple(images.PROVIDERS),
                                                                               "The picture service")
        return ("images",), out
    if section == "sound":
        for key, value in values.items():
            if key != "provider":
                raise SettingsError(f"{key} can't be changed here.")
            out["provider"] = edit.REMOVE if value in ("", None) else _choice(value, tuple(sound.PROVIDERS),
                                                                               "The sound service")
        return ("sound",), out
    raise SettingsError("That isn't a setting the page can change.")


def change(settings, body: Any) -> dict | None:
    """Make one change from the page in the settings file (the old file is kept as config.toml.bak)."""
    if not isinstance(body, dict):
        raise SettingsError("Expected a JSON object.")
    section = body.get("section")
    path = _path(settings)
    if settings.config.get("_error") or (path is None and section != "add_model"):
        raise SettingsError("There's no settings file this page can change. Run ixel setup first.", 409)
    version = body.get("version")
    if not isinstance(version, str) or (not version and path is not None):
        raise SettingsError("The page didn't say which version of the file it read. Here it is again; make the "
                            "change once more.", 409)
    said = None
    if section == "add_model":
        change_text, said = _plan_add(settings, body.get("values"))
    elif section == "remove_model":
        change_text, said = _plan_remove(settings, body.get("agent"))
    else:
        table, values = plan_change(settings, section, body.get("values"), body.get("agent"))

        def change_text(text: str) -> str:
            return edit.set_values(text, table, values)
    made = path is None
    if made:
        path, version = _new_file(version)
    try:
        edit.change_file(path, change_text, version, backup=not (made or _holds_secrets(settings.config)),
                         check=_new_problems(settings))
    except edit.Changed as exc:
        raise SettingsError(f"{exc} Here it is again; make the change once more.", 409) from None
    except edit.EditError as exc:
        raise SettingsError(f"Ixel didn't change the file: {exc}. Change it by hand in {path}.", 422) from None
    finally:
        if made and path.read_text(encoding="utf-8") == NEW_FILE:
            path.unlink()  # the first change didn't go in: no empty file left behind
    return {"message": said} if said else None


NEW_FILE = ("# Ixel MAT — Agent Configuration\n"
            "# Started in the app's Settings. ixel setup can add more; secrets live in .env, never here.\n")


def _new_file(version: str) -> tuple[Path, str]:
    """The settings file, made for a first model added in the app (when the page read no file)."""
    from ixel_mat.config import loader
    path = loader._GLOBAL_CONFIG
    changed = SettingsError("The settings file changed since this page read it. Here it is again; make the "
                            "change once more.", 409)
    if version or path.exists():
        raise changed
    if os.path.lexists(path):  # a link to a file that isn't there: left alone
        raise SettingsError(f"{path} is a link to a file that isn't there, so Ixel can't start your settings "
                            "file. Remove the link or put the file back, then add the model again.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:  # only if nothing appeared since (ixel setup writing it now)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    except FileExistsError:
        raise changed from None
    with os.fdopen(fd, "wb") as handle:
        handle.write(NEW_FILE.encode("utf-8"))
    return path, edit.file_version(path)


def _problems(config: dict, settings) -> set[str]:
    """Choices that can't work together (each one says how to get out of it)."""
    from ixel_mat.modes.review import ReviewMode
    from ixel_mat.runtime import parse_review_settings, parse_saver_settings
    names = set(settings.agent_configs)
    review, _ = parse_review_settings(config, names)
    saver, _ = parse_saver_settings(config, names, review)
    raw = config.get("review") if isinstance(config.get("review"), dict) else {}
    found = set()
    if ((review.mode is ReviewMode.SAVER and not review.auto) or raw.get("plain_questions") == "saver") \
            and not saver.verifier:
        found.add("Saver needs a big model to verify, and questions are set to use Saver. Pick the model that "
                  "verifies under Saver, or another mode first.")
    if review.moderator and review.agents and review.moderator not in review.agents:
        cfg = settings.agent_configs.get(review.moderator)
        found.add(f"{cfg.label if cfg else review.moderator} writes the verdict, so it has to be on the panel. "
                  "Pick who writes it first, or keep it on the panel.")
    return found


def _new_problems(settings):
    """Refuses a change that makes a problem the file didn't already have (one it had doesn't block others)."""
    before = _problems(settings.config, settings)

    def check(config: dict) -> None:
        new = _problems(config, settings) - before
        if new:
            raise SettingsError(sorted(new)[0])
    return check


# ── Keys ──────────────────────────────────────────────────────────────────────

def check_key_value(value: Any) -> str:
    if not isinstance(value, str):
        raise SettingsError("Paste the key.")
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'":
        cleaned = cleaned[1:-1].strip()
    if not cleaned:
        raise SettingsError("Paste the key.")
    if len(cleaned) > MAX_KEY_CHARS or any(not 33 <= ord(c) <= 126 or c in "\"'\\`" for c in cleaned):
        raise SettingsError("That doesn't look like a key: keys are one line of letters, digits and a few "
                            "symbols, with no spaces or quotes.")
    return cleaned


def set_key(settings, body: Any) -> dict:
    """Save, replace or remove one key. Returns {"state", "message"}."""
    if not isinstance(body, dict):
        raise SettingsError("Expected a JSON object.")
    name = body.get("name")
    allowed = {k["name"]: k for k in known_keys(settings)}
    if not isinstance(name, str) or name not in allowed:
        raise SettingsError("That isn't a key Ixel uses.")
    label = allowed[name]["label"]
    if body.get("remove") is True:
        removed = secrets.remove_live(name)
        state = secrets.key_state(name)
        if state == "system":
            return {"state": state, "message": f"{label}'s key is also set outside Ixel ({name}), and that one is "
                                               "still used. Remove it there to stop using it."}
        return {"state": state, "message": f"Removed {label}'s key." if removed else f"{label} had no key saved here."}
    value = check_key_value(body.get("value"))
    outcome = secrets.set_live(name, value)
    if outcome == "system":
        return {"state": "system", "message": f"Saved, but {name} is also set outside Ixel (in your system or "
                                              "shell), and that one wins. Change or remove it there."}
    return {"state": "file", "message": f"Saved {label}'s key. It's used from the next question on."}

