"""
Config loader for Ixel MAT.

Precedence: explicit path > global > defaults
Secrets: never in config files — always env var references via token_env field.

Config location:
  1. --config /path/to/file.toml (explicit)
  2. ~/.config/ixel-mat/config.toml (global)

A project-local ./.ixel-mat.toml is deliberately NOT loaded. Agent definitions
can launch commands and choose which env vars get sent to which URL, so picking
them up from the current directory would let any cloned repo run code or
exfiltrate API keys the moment you start ixel inside it.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ixel_mat.agents.base import (EFFORT_LEVELS, MEDIA, PROMPT_MODES, STDOUT_FORMATS, AgentConfig,
                                   cleartext_refusal, is_loopback_host, needs_api_key, sends_key_in_cleartext)
from ixel_mat.usage import BILLING
from ixel_mat.config.secrets import read_text_file, write_private_file
from ixel_mat.local_models import chat_url
from ixel_mat.models import ALIASES, provider_for_url, valid_model_id
from ixel_mat.presets import PRESET_FIELDS, PRESETS_BY_ID, locked_env

# Use tomllib (3.11+) or fallback to tomli
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore


_GLOBAL_CONFIG = Path.home() / ".config" / "ixel-mat" / "config.toml"
_LOCAL_CONFIG  = Path(".ixel-mat.toml")

def find_config(explicit_path: str | None = None) -> Path | None:
    """Find the config file by precedence (never the current directory)."""
    if explicit_path:
        p = Path(explicit_path)
        return p if p.exists() else None

    if _GLOBAL_CONFIG.exists():
        return _GLOBAL_CONFIG

    return None


def load_config(explicit_path: str | None = None) -> dict[str, Any]:
    """
    Load and return the full config dict: no agents when there's no config yet,
    and no agents plus "_error" when it can't be read (never stand-in agents).
    """
    if not explicit_path and _LOCAL_CONFIG.exists():
        print(
            f"Warning: ignoring ./{_LOCAL_CONFIG} — project-local configs are "
            f"not loaded for security. Agents are read from {_GLOBAL_CONFIG}.",
            file=sys.stderr,
        )

    path = find_config(explicit_path)

    if path is None:
        return {"agents": {}, "_source": "none"}

    if tomllib is None:
        error = "no TOML parser available: install tomli (pip install tomli) or use Python 3.11+"
        print(f"Warning: {error}", file=sys.stderr)
        return {"agents": {}, "_source": str(path), "_error": error}

    try:
        data = tomllib.loads(read_text_file(path))
    except Exception as e:  # noqa: BLE001 — unreadable or not valid TOML
        error = f"couldn't read {path}: {e}"
        print(f"Warning: {error}. Fix it, or run `ixel setup` to write a new one.", file=sys.stderr)
        return {"agents": {}, "_source": str(path), "_error": error}
    data["_source"] = str(path)
    return data


def _resolve_token(agent_data: dict) -> str:
    """Resolve token from env var reference or direct value."""
    # Prefer token_env (env var name)
    env_name = agent_data.get("token_env", "")
    if env_name:
        val = os.getenv(env_name, "")
        if val:
            return val

    # Fallback: direct token (not recommended, will warn)
    direct = agent_data.get("token", "")
    if direct and not direct.startswith("${"):
        return direct

    # Handle ${ENV_VAR} interpolation
    if direct.startswith("${") and direct.endswith("}"):
        env_name = direct[2:-1]
        return os.getenv(env_name, "")

    return ""


def build_agent_configs(config: dict[str, Any]) -> tuple[dict[str, AgentConfig], list[str]]:
    """
    Build AgentConfig objects from loaded config.
    Returns (configs, warnings) where configs is dict of name → AgentConfig
    and warnings is a list of non-fatal issues (e.g. missing tokens).
    """
    agents_data = config.get("agents", {})
    configs: dict[str, AgentConfig] = {}
    warnings: list[str] = []

    for name, data in agents_data.items():
        if not isinstance(data, dict):
            continue

        data = _with_preset(name, data, warnings)
        if data is None:
            continue

        token = _resolve_token(data)
        agent_type = data.get("type", "websocket")

        if agent_type == "websocket" and not token:
            env_name = data.get("token_env", "IXELMAT_GATEWAY_TOKEN")
            warnings.append(
                f"Agent '{name}': no token found. Run `ixel setup`, or set the {env_name} environment variable"
            )

        timeout = data.get("timeout", 0)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout < 0:
            warnings.append(f"Agent '{name}': ignoring invalid timeout {timeout!r}")
            timeout = 0
        prompt_via = data.get("prompt_via", "flag")
        if prompt_via not in PROMPT_MODES:
            warnings.append(f"Agent '{name}': ignoring unknown prompt_via {prompt_via!r}")
            prompt_via = "flag"
        stdout_format = data.get("stdout_format", "text")
        if stdout_format not in STDOUT_FORMATS:
            warnings.append(f"Agent '{name}': ignoring unknown stdout_format {stdout_format!r}")
            stdout_format = "text"
        workdir = data.get("workdir", "temp")
        if not isinstance(workdir, str) or not workdir:
            warnings.append(f"Agent '{name}': ignoring invalid workdir {workdir!r}")
            workdir = "temp"
        effort = data.get("effort", "")
        if effort and effort not in EFFORT_LEVELS:
            warnings.append(f"Agent '{name}': effort must be one of {', '.join(EFFORT_LEVELS)}")
            effort = ""
        pass_env = _str_list(data, "pass_env", name, warnings)
        drop_env = _str_list(data, "drop_env", name, warnings)
        effort_args = _str_list(data, "effort_args", name, warnings)
        effort_levels = _str_list(data, "effort_levels", name, warnings)
        model_args = _str_list(data, "model_args", name, warnings)
        accepts = _str_list(data, "accepts", name, warnings)
        if accepts and any(kind not in MEDIA for kind in accepts):
            warnings.append(f"Agent '{name}': accepts can list {', '.join(MEDIA)}; ignoring "
                            f"{', '.join(repr(k) for k in accepts if k not in MEDIA)}")
            accepts = [kind for kind in accepts if kind in MEDIA]
        model = data.get("model", "")
        if model and not (isinstance(model, str) and (model in ALIASES or valid_model_id(model))):
            warnings.append(f"Agent '{name}': ignoring model {model!r}, which isn't a valid model name")
            model = ""
        billing = data.get("billing", "")
        if billing and billing not in BILLING:
            warnings.append(f"Agent '{name}': billing must be one of {', '.join(BILLING)}")
            billing = ""
        env = data.get("env")
        if env is not None and not (isinstance(env, dict) and all(
                isinstance(k, str) and _ENV_NAME.match(k) and isinstance(v, str) for k, v in env.items())):
            warnings.append(f"Agent '{name}': env must be a table of NAME = \"value\" strings")
            env = None
        command = data.get("command", "")
        kept = locked_env(command if isinstance(command, str) else "")
        overridden = sorted(k for k, v in (env or {}).items() if k in kept and v != kept[k])
        if overridden:  # Ixel sets its own whenever it runs the program
            warnings.append(f"Agent '{name}': Ixel always sets {', '.join(overridden)} for this program, so its env "
                            f"can't change {'it' if len(overridden) == 1 else 'them'}")
        args_by_version, required_args = data.get("args_by_version"), data.get("required_args")
        if args_by_version is not None and not _args_table(args_by_version):
            warnings.append(f"Agent '{name}': args_by_version must be a table of \"version\" = [\"arg\", …] lists")
            args_by_version = None
        if required_args is not None and not _args_table(required_args):
            warnings.append(f"Agent '{name}': required_args must be a table of \"version\" = [\"arg\", …] lists")
            required_args = None

        url = data.get("url", "")
        if agent_type == "http" and isinstance(url, str):
            url = chat_url(url)  # a model server's address as its app shows it (…:1234/v1) takes questions too

        configs[name] = AgentConfig(
            name=name,
            label=data.get("label", name),
            type=agent_type,
            url=url,
            token=token,
            model=model,
            session_key=data.get("session_key", f"agent:{name}:main"),
            color=data.get("color", "cyan"),
            command=data.get("command", ""),
            args=data.get("args"),
            args_by_version=args_by_version,
            required_args=required_args,
            auto_resume=data.get("auto_resume", True),
            timeout=float(timeout),
            prompt_via=prompt_via,
            workdir=workdir,
            pass_env=pass_env,
            output_flag=data.get("output_flag", "") if isinstance(data.get("output_flag", ""), str) else "",
            stdout_format=stdout_format,
            effort=effort,
            env=env,
            drop_env=drop_env,
            effort_args=effort_args,
            effort_levels=effort_levels,
            model_args=model_args,
            billing=billing,
            accepts=accepts,
        )

    return configs, warnings


def _with_preset(name: str, data: dict, problems: list[str]) -> dict | None:
    """An agent that names a CLI preset gets the preset's settings, with its own on top."""
    if "preset" not in data:
        return data
    preset = PRESETS_BY_ID.get(data["preset"]) if isinstance(data["preset"], str) else None
    if preset is None:
        problems.append(f"Agent '{name}': unknown preset {data['preset']!r} (known: {', '.join(PRESETS_BY_ID)})")
        return None
    merged = {"type": "oneshot", "workdir": "temp", "label": preset["label"],
              **{f: preset[f] for f in PRESET_FIELDS if f in preset}, **data}
    if isinstance(preset.get("env"), dict) and isinstance(data.get("env"), dict):
        merged["env"] = {**preset["env"], **data["env"]}  # your own settings join the preset's, not replace them
    if "args" in data and "args_by_version" not in data:
        merged.pop("args_by_version", None)  # your own args are used whatever the version
    if "required_args" in preset:
        merged["required_args"] = preset["required_args"]  # but they still need these: they're the lockdown
    return merged


def _args_table(value: object) -> bool:
    return isinstance(value, dict) and all(isinstance(v, list) and all(isinstance(a, str) for a in v)
                                           for v in value.values())


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _str_list(data: dict, key: str, agent: str, warnings: list[str]) -> list[str] | None:
    value = data.get(key)
    if value is None or (isinstance(value, list) and all(isinstance(v, str) for v in value)):
        return value
    warnings.append(f"Agent '{agent}': {key} must be a list of strings")
    return None


def _is_cleartext_remote(url: str, scheme: str) -> bool:
    """True if url uses the unencrypted scheme for a host other than this machine."""
    if not url.startswith(f"{scheme}://"):
        return False
    try:
        host = urlparse(url).hostname
    except ValueError:  # malformed, e.g. "http://[oops" — report it, don't crash
        return True
    return not is_loopback_host(host)


def validate_config(config: dict[str, Any]) -> list[str]:
    """
    Validate config and return list of issues.
    Empty list = valid.
    """
    issues: list[str] = []
    source = config.get("_source", "unknown")

    agents = config.get("agents", {})
    if not agents:
        issues.append("No agents configured")
        return issues

    for name, data in agents.items():
        if not isinstance(data, dict):
            issues.append(f"Agent '{name}': invalid config (not a dict)")
            continue
        data = _with_preset(name, data, issues)
        if data is None:
            continue

        agent_type = data.get("type", "")
        if agent_type not in ("websocket", "http", "subprocess", "oneshot"):
            issues.append(f"Agent '{name}': unknown type '{agent_type}'")

        if agent_type == "websocket":
            url = data.get("url", "")
            if not url:
                issues.append(f"Agent '{name}': missing url")
            elif not url.startswith(("ws://", "wss://")):
                issues.append(f"Agent '{name}': url must start with ws:// or wss://")
            elif _is_cleartext_remote(url, "ws"):
                issues.append(f"Agent '{name}': use wss:// for non-local hosts (ws:// sends the token in cleartext)")

            token = _resolve_token(data)
            if not token:
                env_name = data.get("token_env", "IXELMAT_GATEWAY_TOKEN")
                issues.append(f"Agent '{name}': token not set (run `ixel setup`, or set {env_name})")

        if agent_type == "http":
            url = data.get("url", "")
            if not url:
                issues.append(f"Agent '{name}': missing url")
            elif not url.startswith(("http://", "https://")):
                issues.append(f"Agent '{name}': url must start with http:// or https://")

            token = _resolve_token(data)
            if url and sends_key_in_cleartext(url, token):
                issues.append(cleartext_refusal(name, url))
            if not token and needs_api_key(AgentConfig(name=name, label=name, type="http", url=url)):
                env_name = data.get("token_env", "")
                issues.append(f"Agent '{name}': API key not set (need: {env_name})")

            # Blank means "latest" on the big providers; a local or custom server needs a name
            if not data.get("model", "") and not provider_for_url(url):
                issues.append(f"Agent '{name}': missing model (ixel model {name} lists the server's models)")

        if agent_type == "subprocess":
            command = data.get("command", "")
            if not command:
                issues.append(f"Agent '{name}': missing command")

        if agent_type == "oneshot":
            command = data.get("command", "")
            if not command:
                issues.append(f"Agent '{name}': missing command")
            if data.get("prompt_via", "flag") not in PROMPT_MODES:
                issues.append(f"Agent '{name}': prompt_via must be one of {', '.join(PROMPT_MODES)}")

        if not data.get("label"):
            issues.append(f"Agent '{name}': missing label")

    return issues


def print_config_status(config: dict[str, Any]):
    """`ixel config` and /config: each agent as Ixel will run it (presets filled in), and what's wrong."""
    from rich.console import Console

    from ixel_mat.runtime import settings_from
    from ixel_mat.sanitize import safe_markup
    console = Console()

    source = config.get("_source", "unknown")
    console.print(f"\n  [bold]Config source:[/] {safe_markup(source)}")

    settings = settings_from(config)
    console.print(f"  [bold]Agents:[/] {len(settings.agent_configs)}\n")

    for name, cfg in settings.agent_configs.items():
        console.print(f"    [cyan]{safe_markup(name)}[/] — {safe_markup(cfg.label)}")
        console.print(f"      type: {cfg.type}")
        if cfg.url:
            console.print(f"      url: {safe_markup(cfg.url)}")
        if cfg.command:
            console.print(f"      command: {safe_markup(cfg.command)}")
        if cfg.type in ("oneshot", "subprocess"):
            key = "not needed (the program signs in itself)"
        elif not needs_api_key(cfg):
            key = "not needed (a model server on your own computer or network)"
        else:
            key = f"✓ set ({len(cfg.token)} chars)" if cfg.token else "✗ missing"
            raw = config.get("agents", {}).get(name)
            if isinstance(raw, dict) and isinstance(raw.get("token_env"), str):
                key += f" · from {safe_markup(raw['token_env'])}"
        console.print(f"      key: {key}")
        console.print()

    issues = [*settings.warnings, *validate_config(config)]
    if issues:
        console.print("  [yellow]Issues:[/]")
        for issue in dict.fromkeys(issues):  # in order, once each
            console.print(f"    [yellow]⚠[/] {safe_markup(issue)}")
    else:
        console.print("  [green]✓ Config valid[/]")
    console.print()


def set_agent_model(path: Path, agent_id: str, model: str) -> None:
    """
    Change one agent's model in the config file, leaving everything else as
    written. "default" removes the setting (the provider's latest model, or the
    CLI's own default). The result is re-read to confirm the change before it
    replaces the file, and the previous version is kept as config.toml.bak.
    """
    if model != "default" and not (model in ALIASES or valid_model_id(model)):
        raise ValueError(f"{model!r} isn't a valid model name")
    text = read_text_file(path)
    agents = tomllib.loads(text).get("agents", {})
    if agent_id not in agents:
        raise KeyError(agent_id)
    lines = text.splitlines(keepends=True)
    header = re.compile(r'^\s*\[\s*agents\s*\.\s*("?)' + re.escape(agent_id) + r'\1\s*\](\s*#.*)?\s*$')
    start = next((i for i, line in enumerate(lines) if header.match(line)), None)
    if start is None:
        raise ValueError(f"couldn't find the [agents.{agent_id}] table to edit; change its model by hand")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    model_line = next((i for i in range(start + 1, end) if re.match(r"^\s*model\s*=", lines[i])), None)
    newline = "\r\n" if lines[start].endswith("\r\n") else "\n"
    new_line = f"model = {json.dumps(model)}{newline}"
    if model == "default":
        if model_line is not None:
            del lines[model_line]
    elif model_line is not None:
        # Keep the line's indent and any comment after its value
        kept = re.match(r"""^(\s*)model\s*=\s*(?:"(?:[^"\\]|\\.)*"|'[^']*')(\s*#.*?)?\s*$""", lines[model_line])
        if kept:
            new_line = f"{kept.group(1)}model = {json.dumps(model)}{kept.group(2) or ''}{newline}"
        lines[model_line] = new_line
    else:
        lines.insert(start + 1, new_line)
    updated = "".join(lines)
    check = tomllib.loads(updated)["agents"][agent_id]
    if check.get("model") != (None if model == "default" else model):
        raise ValueError("the edit didn't come out right; change the model by hand")
    write_private_file(path.with_suffix(".toml.bak"), text.encode("utf-8"))
    write_private_file(path, updated.encode("utf-8"))
