"""
The Models list in Settings: what each model on your panel can be set to, so nobody has to type a name.

Each list comes from the company whenever Ixel can ask it. A model Ixel calls itself shows its API's own
list, with the key it already uses. A program shows its company's list when Ixel has that company's key
(Claude Code: Anthropic's, Codex: OpenAI's, Gemini CLI: Google's), OpenCode shows the models it already
knows (`opencode models`, kept from fetching OpenCode's catalog), and a model server on your own computer
shows what it has. Otherwise the list built in below is
shown, and the page says so: it's a fallback that goes stale, while the live one doesn't.

Only model names reach the page. A key goes only to its own company (as with every question), never over
plain http to another computer, and nothing here changes the settings file.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import secrets
import shutil
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from ixel_mat import effort
from ixel_mat.local_models import is_chat_model
from ixel_mat.models import ALIASES, models_url, pick_latest, provider_for_url, valid_model_id
from ixel_mat.presets import OPENCODE_ONLY_FREE, opencode_only_free

# Built in, newest first (as of October 2026). Shown only when the company can't be asked.
BUILT_IN: dict[str, list[str]] = {
    "anthropic": ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"],
    "openai": ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-5.5", "gpt-5.5-mini"],
    "gemini": ["gemini-3.1-pro-preview", "gemini-3.8-flash", "gemini-3.7-flash"],
    "xai": ["grok-4.7", "grok-4.7-fast", "grok-4.6", "grok-4.5"],
}
COMPANIES = {"anthropic": "Anthropic", "openai": "OpenAI", "gemini": "Google", "xai": "xAI"}
KEYS = {"anthropic": ("ANTHROPIC_API_KEY",), "openai": ("OPENAI_API_KEY",),
        "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"), "xai": ("XAI_API_KEY",)}
ALIAS_WORDS = {"latest": "the newest top model", "latest-fast": "the newest fast, cheaper model"}

# Short names a program takes and keeps up to date by itself
PROGRAM_NAMES: dict[str, list[tuple[str, str]]] = {
    "claude_code": [("fable", "the newest Fable"), ("opus", "the newest Opus"), ("sonnet", "the newest Sonnet"),
                    ("haiku", "the newest Haiku")],
    "gemini_cli": [("auto", "Gemini CLI picks for each question"), ("pro", "the newest Pro"),
                   ("flash", "the newest Flash"), ("flash-lite", "the newest Flash-Lite")],
    "copilot": [("auto", "Copilot picks")],
}
# The company whose model list a program can use
PROGRAM_COMPANY = {"claude_code": "anthropic", "codex": "openai", "gemini_cli": "gemini"}
# Programs that list their own models: `opencode models` prints provider/model, one a line. Only run when
# the agent's command is that program itself (a command such as npx would read "models" as a package to fetch).
# It's run as for a question, so OpenCode lists what it already knows and never fetches its model catalog
# (locked_env), and OpenCode 2 is asked through a server started just for that (_opencode_models_privately).
PROGRAM_LISTS = {"opencode": ["models"]}

MAX_MODELS = 300
PROGRAM_TIMEOUT = 20
SETTLE = 10        # seconds OpenCode 2's private server is given to load its list (it starts out empty)...
STABLE = 1.5       # ...which is taken once it has stayed the same this long (your own providers come in last)
LIVE_FOR = 600     # seconds a list is kept before the company is asked again
RETRY_AFTER = 60   # ...and after asking failed
_SNAPSHOT_DATE = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{4}|\d{6,8})$")  # gpt-4o-2024-08-06, grok-4-0709
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


# ── Asking ────────────────────────────────────────────────────────────────────

def company_models(provider: str, key: str) -> list[dict]:
    """A company's chat models for this key, as its API lists them ({"id", "created"?}). Raises if it can't say."""
    from ixel_mat.config.setup import _CHAT_MODEL_PREFIXES, _NOT_CHAT, _get_json
    if provider == "anthropic":
        data = _get_json(urllib.request.Request("https://api.anthropic.com/v1/models?limit=100",
                                                headers={"x-api-key": key, "anthropic-version": "2023-06-01"}))
        entries = [{"id": m.get("id")} for m in data.get("data", []) if isinstance(m, dict)]
    elif provider == "gemini":
        data = _get_json(urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000", headers={"x-goog-api-key": key}))
        entries = [{"id": str(m.get("name", "")).removeprefix("models/")} for m in data.get("models", [])
                   if isinstance(m, dict) and "generateContent" in (m.get("supportedGenerationMethods") or [])]
    else:
        url = {"openai": "https://api.openai.com/v1/models", "xai": "https://api.x.ai/v1/models"}[provider]
        data = _get_json(urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"}))
        entries = [{"id": m.get("id"), "created": m.get("created")} for m in data.get("data", [])
                   if isinstance(m, dict)]
    prefixes = _CHAT_MODEL_PREFIXES.get(provider, ())
    return [e for e in entries if isinstance(e["id"], str) and e["id"].startswith(prefixes)
            and not any(x in e["id"] for x in _NOT_CHAT)]


def server_models(cfg) -> list[dict]:
    """What a model server that isn't one of the big companies' has (Ollama, LM Studio…). Raises if it can't say."""
    from ixel_mat.agents.base import sends_key_in_cleartext
    from ixel_mat.config.setup import _get_json
    if sends_key_in_cleartext(cfg.url, cfg.token):
        raise RuntimeError("it would mean sending its key over plain http")
    headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
    data = _get_json(urllib.request.Request(models_url(cfg.url), headers=headers), timeout=5)
    # Only models that can answer: a server lists its embedding (and speech) models beside them
    return [{"id": m.get("id")} for m in data.get("data", []) if isinstance(m, dict) and isinstance(m.get("id"), str)
            and is_chat_model(m["id"])]


def program_models(cfg, args: list[str]) -> list[dict]:
    """What a program says it can use (`opencode models`), started as it is for a question. Raises if it can't say."""
    from ixel_mat.material import mask_secrets
    returncode, out, err = asyncio.run(_list(cfg, args))
    if returncode != 0:
        said = mask_secrets(_ANSI.sub("", err.decode("utf-8", "replace"))).strip().splitlines()
        raise RuntimeError(f"{cfg.command} exited with code {returncode}" + (f": {said[0][:200]}" if said else ""))
    ids = _program_ids(out)
    if not ids:  # said so, and asked again after RETRY_AFTER rather than kept
        raise RuntimeError("it listed none")
    return [{"id": i} for i in ids]


def _program_ids(out: bytes) -> list[str]:
    lines = (_ANSI.sub("", line).strip() for line in out.decode("utf-8", "replace").splitlines())
    return [line for line in lines if "/" in line and "://" not in line]


async def _list(cfg, args: list[str]) -> tuple[int | None, bytes, bytes]:
    from ixel_mat.agents.base import prepare_workdir
    from ixel_mat.agents.oneshot import cli_env
    env = cli_env(cfg)
    cwd, remove = prepare_workdir("temp")
    try:
        if effort.program_name(cfg.command) == "opencode" and await _opencode_lists_through_a_server(cfg, env, cwd):
            return await _opencode_models_privately(cfg, args, env, cwd)
        return await _run(cfg, args, env, cwd)
    finally:
        if remove:
            shutil.rmtree(cwd, ignore_errors=True)


async def _opencode_lists_through_a_server(cfg, env: dict[str, str], cwd: str) -> bool:
    """OpenCode 2 and on list through a server: your background service, unless told which. OpenCode 1 lists itself,
    and is the only one asked to: when Ixel can't tell, it's a private server (which an OpenCode 1 turns down)."""
    from ixel_mat.agents.oneshot import major_version
    version = await major_version(cfg.command, env)
    if version.isdigit() and version != "0":
        return int(version) >= 2
    # A build that doesn't say which it is (a dev build calls itself 0.0.0-dev-…): its help says
    code, out, err = await _run(cfg, ["models", "--help"], env, cwd)
    return code != 0 or b"--server" in out + err


# Only 127.0.0.1 (where it's told to listen), and the address built from the port alone, so nothing it prints can
# send the password anywhere else
_LISTENING = re.compile(r"server listening on http://127\.0\.0\.1:(\d{1,5})\b")


async def _opencode_models_privately(cfg, args: list[str], env: dict[str, str],
                                     cwd: str) -> tuple[int | None, bytes, bytes]:
    """
    OpenCode 2's list, from a server of its own started just for this and ended after it: never your background
    service, which runs with your own settings (fetching the catalog included) and would otherwise be started with
    Ixel's. The server is reached on 127.0.0.1 with a password made up for it, given in the environment rather than
    on a command line. Its list is empty for its first second or so, and your own providers come in after its
    built-in ones, so it's asked until the list has stayed the same for STABLE seconds, or SETTLE have passed.
    """
    env = {**env, "OPENCODE_SERVER_PASSWORD": secrets.token_urlsafe(24)}
    deadline = time.monotonic() + PROGRAM_TIMEOUT
    proc, tree = await _start(cfg, ["serve", "--hostname", "127.0.0.1", "--port", "0"], env, cwd)
    found = asyncio.get_running_loop().create_future()
    said = {"out": b"", "err": b""}

    async def watch(stream, name: str) -> None:  # read all it says, so it never stalls on a full pipe
        while chunk := await stream.read(65536):
            said[name] = (said[name] + chunk)[-65536:]
            match = _LISTENING.search(_ANSI.sub("", said[name].decode("utf-8", "replace")))
            if match and not found.done():
                found.set_result(f"http://127.0.0.1:{match.group(1)}")

    watchers = [asyncio.ensure_future(watch(proc.stdout, "out")), asyncio.ensure_future(watch(proc.stderr, "err"))]
    try:
        ended = asyncio.ensure_future(asyncio.gather(*watchers, return_exceptions=True))
        await asyncio.wait({found, ended}, timeout=max(deadline - time.monotonic(), 0),
                           return_when=asyncio.FIRST_COMPLETED)
        if not found.done():
            if ended.done():  # it's done talking, most likely exited, with its reason on stderr
                with contextlib.suppress(asyncio.TimeoutError):
                    return await asyncio.wait_for(proc.wait(), max(deadline - time.monotonic(), 1)), b"", \
                        said["err"] or said["out"]
            raise RuntimeError(f"{cfg.command} didn't answer within {PROGRAM_TIMEOUT} seconds")
        url = found.result()
        settled, last, since = min(deadline, time.monotonic() + SETTLE), None, 0.0
        while True:
            code, out, err = await _run(cfg, [*args, "--server", url], env, cwd,
                                        timeout=max(deadline - time.monotonic(), 1))
            if code != 0:
                return code, out, err
            ids, now = _program_ids(out), time.monotonic()
            if ids != last:
                last, since = ids, now
            if (ids and now - since >= STABLE) or now >= settled:
                return code, out, err
            await asyncio.sleep(0.3)
    finally:
        for watcher in watchers:  # before the kill, which reads what's left itself
            watcher.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)
        await tree.kill()
        tree.close()
        with contextlib.suppress(asyncio.TimeoutError, OSError):
            await asyncio.wait_for(proc.wait(), 5)  # gone: reaped before the loop closes


async def _start(cfg, args: list[str], env: dict[str, str], cwd: str):
    """The program started the way a question starts it (found on PATH only, no console window), in cwd."""
    from ixel_mat.agents.base import in_folder
    from ixel_mat.agents.launch import LaunchError, resolve_argv
    from ixel_mat.agents.process_tree import SPAWN_OPTIONS, create_process_tree
    try:
        return await create_process_tree(
            *resolve_argv([cfg.command, *args]), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=cwd, env=in_folder(env, cwd), **SPAWN_OPTIONS)
    except FileNotFoundError:
        raise RuntimeError(f"{cfg.command} isn't installed") from None
    except (OSError, LaunchError) as exc:
        raise RuntimeError(f"{cfg.command} couldn't start: {exc}") from None


async def _run(cfg, args: list[str], env: dict[str, str], cwd: str,
               timeout: float | None = None) -> tuple[int | None, bytes, bytes]:
    """The program run once with env (as for a question: cli_env), in a scratch folder, and ended with everything
    it started however it ends (an npm CLI is node starting another program on Windows)."""
    proc, tree = await _start(cfg, args, env, cwd)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), PROGRAM_TIMEOUT if timeout is None else timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"{cfg.command} didn't answer within {PROGRAM_TIMEOUT} seconds") from None
    finally:
        await tree.kill()
        tree.close()
    return proc.returncode, out, err


# ── One model's choices ───────────────────────────────────────────────────────

def _program_id(cfg, raw: dict) -> str:
    from ixel_mat.presets import CLI_PRESETS, PRESETS_BY_ID
    if isinstance(raw.get("preset"), str) and raw["preset"] in PRESETS_BY_ID:
        return raw["preset"]
    return next((p["id"] for p in CLI_PRESETS if _runs_itself(cfg, p["command"])), "")


def _company_key(provider: str) -> str:
    return next((value for value in (os.environ.get(k) for k in KEYS[provider]) if value), "")


def _runs_itself(cfg, program: str) -> bool:
    """The agent's command is the program (opencode, opencode.exe…), not something that starts it, such as npx."""
    name = os.path.basename(cfg.command).lower()
    for ending in (".exe", ".cmd", ".bat"):
        name = name.removesuffix(ending)
    return name == program


def listed(provider: str | None, entries: list[dict]) -> list[str]:
    """The names to list: valid ones only, dated snapshots of the big companies' models left out, newest first."""
    from ixel_mat.models import _version
    ids = list(dict.fromkeys(str(e["id"]).strip() for e in entries))
    ids = [i for i in ids if valid_model_id(i)]
    if provider in ("openai", "xai", "gemini"):
        ids = [i for i in ids if not _SNAPSHOT_DATE.search(i)]
        ids.sort(key=lambda i: (_version(i), "preview" not in i, i), reverse=True)
    return ids[:MAX_MODELS]


def _key_hint(provider: str) -> str:
    return f"Save {'an' if COMPANIES[provider][0] in 'AEIOU' else 'a'} {COMPANIES[provider]} key under Keys " \
           "to see your account's own list."


def agent_choices(cfg, raw: dict) -> dict:
    """
    {"source": "company" | "program" | "server" | "built_in" | "none", "where": who the list is from,
     "newest": whether Default means the newest model, "default": the model that is now when Ixel knows
     ("" otherwise, None when there's no default: a model has to be named), and for a model that follows
     the newest, "follows" (its setting), "resolved" (the model it is now) and "efforts" (that model's levels),
     "names": [{"id", "about", "now"}] (names that follow the newest), "models": [names], "note": ...}
    """
    out = {"source": "none", "where": "", "newest": False, "default": "", "names": [], "models": [], "note": ""}
    if cfg.type == "http":
        provider = provider_for_url(cfg.url)
        if provider is None:  # a model server of your own, or another company's: a model has to be named
            out.update(default=None, where=_server_name(cfg.url))
            try:
                out.update(source="server", models=listed(None, server_models(cfg)))
            except Exception as exc:  # noqa: BLE001 — not running, or no model list
                out["note"] = f"Ixel couldn't ask {out['where']} for its models ({_why(exc)})."
            return out
        entries = None
        out["where"] = COMPANIES[provider]
        if cfg.token:
            try:
                entries = company_models(provider, cfg.token)
            except Exception as exc:  # noqa: BLE001 — offline, or a key it refused
                out["note"] = f"Ixel couldn't ask {COMPANIES[provider]} for its models ({_why(exc)})."
        else:
            out["note"] = f"There's no key for {cfg.label} yet. Save one under Keys to see your account's own list."
        now = {a: (pick_latest(provider, entries, a) or "") if entries else "" for a in ALIASES}
        out.update(newest=True, default=now["latest"],  # an empty model on a big company's API means its newest
                   names=[{"id": a, "about": ALIAS_WORDS[a], "now": now[a]} for a in ALIASES])
        if entries:
            out.update(source="company", models=listed(provider, entries))
        else:
            out.update(source="built_in", models=list(BUILT_IN[provider]))
        return out
    if cfg.type != "oneshot":
        return out
    if not cfg.model_args:
        out["note"] = f"{cfg.label} isn't set up to take a model from Ixel, so it uses its own."
        return out
    program = _program_id(cfg, raw)
    out["names"] = [{"id": i, "about": about, "now": ""} for i, about in PROGRAM_NAMES.get(program, [])]
    if program in PROGRAM_LISTS and _runs_itself(cfg, program):
        out["where"] = cfg.label
        try:
            out.update(source="program", models=listed(None, program_models(cfg, PROGRAM_LISTS[program])))
        except Exception as exc:  # noqa: BLE001 — not installed, signed in nowhere, an old version…
            out["note"] = f"Ixel couldn't ask {cfg.label} for its models ({_why(exc)})."
            return out
        if program == "opencode" and opencode_only_free(out["models"]):
            out["note"] = OPENCODE_ONLY_FREE  # before you pick one of its free models, which turn Ixel down
        return out
    provider = PROGRAM_COMPANY.get(program)
    if provider is None:
        return out
    out["where"] = COMPANIES[provider]
    key = _company_key(provider)
    if key:
        try:
            out.update(source="company", models=listed(provider, company_models(provider, key)))
            return out
        except Exception as exc:  # noqa: BLE001
            out["note"] = f"Ixel couldn't ask {COMPANIES[provider]} for its models ({_why(exc)})."
    else:
        out["note"] = _key_hint(provider)
    out.update(source="built_in", models=list(BUILT_IN[provider]))
    return out


def _server_name(url: str) -> str:
    """What to call a server: "your model server" on this computer or your network, else its address."""
    from ixel_mat.agents.base import is_loopback_host
    from ixel_mat.usage import is_private_host
    host = (urlparse(url).hostname or "").lower()
    return "your model server" if not host or is_loopback_host(host) or is_private_host(host) else host


def _why(exc: Exception) -> str:
    from ixel_mat.material import mask_secrets
    code = getattr(exc, "code", None)
    if code in (401, 403):
        return "it didn't accept the key"
    if isinstance(code, int):
        return f"HTTP {code}"
    text = mask_secrets(str(getattr(exc, "reason", None) or exc)).strip()
    return text[:200] or type(exc).__name__


# ── Every model's ─────────────────────────────────────────────────────────────

def _fingerprint(cfg, raw: dict) -> str:
    """What a model's list depends on: where it's from and with which key (never the key itself)."""
    program = _program_id(cfg, raw) if cfg.type == "oneshot" else ""
    provider = PROGRAM_COMPANY.get(program)
    parts = [cfg.name, cfg.label, cfg.type, cfg.url, cfg.command, program, cfg.token,
             _company_key(provider) if provider else "", repr(cfg.model_args), repr(sorted((cfg.env or {}).items()))]
    return hashlib.sha256("\0".join(map(str, parts)).encode()).hexdigest()


def _safely(fn, *args):
    """One model's odd settings never take the others' lists with them."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001
        return exc


def choices(settings, fresh: bool = False) -> dict[str, dict]:
    """Each model's choices (see agent_choices), asked for side by side and kept for a while."""
    agents = settings.config.get("agents") if isinstance(settings.config.get("agents"), dict) else {}
    jobs = {name: (cfg, agents.get(name) if isinstance(agents.get(name), dict) else {})
            for name, cfg in settings.agent_configs.items()}
    now = time.monotonic()
    found: dict[str, dict] = {}
    todo: dict[str, str] = {}
    for name, (cfg, raw) in jobs.items():
        key = _safely(_fingerprint, cfg, raw)
        if isinstance(key, Exception):
            found[name] = _unreadable(key)
            continue
        with _cache_lock:
            kept = _cache.get(key)
        if kept and not fresh and now < kept[0]:
            found[name] = kept[1]
        else:
            todo[name] = key
    if todo:
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
            asked = dict(zip(todo, pool.map(lambda n: _safely(agent_choices, *jobs[n]), todo)))
        with _cache_lock:
            for name, result in asked.items():
                if isinstance(result, Exception):
                    asked[name] = _unreadable(result)
                    continue
                worked = result["source"] in ("company", "program", "server") or not result["note"]
                _cache[todo[name]] = (time.monotonic() + (LIVE_FOR if worked else RETRY_AFTER), result)
        found.update(asked)
    return {name: _with_levels(jobs[name][0], found[name]) for name in jobs}


def _with_levels(cfg, found: dict) -> dict:
    """For a model that follows the newest, the effort levels of the model it is now, not a guess at it (worked
    out from the list kept, so picking another model asks no one again)."""
    provider = provider_for_url(cfg.url) if cfg.type == "http" else None
    follows = cfg.model or "latest"
    now = next((n["now"] for n in found.get("names", ()) if n.get("id") == follows), "")
    if not provider or follows not in ALIASES or not now:
        return found
    return {**found, "follows": cfg.model, "resolved": now, "efforts": list(effort.model_levels(provider, now))}


def _unreadable(exc: Exception) -> dict:
    return {"source": "none", "where": "", "newest": False, "default": "", "names": [], "models": [],
            "note": f"Ixel couldn't work out this model's list ({_why(exc)})."}
