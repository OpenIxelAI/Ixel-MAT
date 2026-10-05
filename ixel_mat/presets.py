"""
Command-line agent presets: the vendors' own CLIs, run with the user's login,
locked down to answer-only.

A config refers to one by name (`preset = "claude_code"`) rather than copying
its flags, so when a preset is tightened, every install picks that up.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Mapping

# Each preset runs the vendor's CLI with its own login, answer-only. Every
# lock-down below was checked by pointing the CLI at a fake model server that
# answers with shell, file-write and file-read tool calls: none may run
# (tests/test_cli_presets_live.py repeats this wherever the CLIs are installed).
_OPENCODE_LOCKDOWN = json.dumps({
    # Our own agent, defined here where it outranks the user's config, so a
    # config that re-enables tools for the built-in agents can't reach it.
    "agent": {"ixel": {"mode": "primary", "description": "Answer-only panel member for Ixel MAT",
                       "tools": {"*": False}, "permission": {"*": "deny"}}},
    "tools": {"*": False}, "permission": {"*": "deny"}, "share": "disabled", "autoupdate": False,
}, separators=(",", ":"))

PANEL_MEMBER = ("You're answering as one member of a panel of AI models. Answer the question directly and "
                "completely, whatever its topic, without remarking on whether it relates to coding.")

# Model providers' keys a subscription CLI has no use for. Exported in your shell, they would reach
# every CLI (Ixel withholds the ones it loads itself): each preset drops the ones that aren't its own, and
# its own too where the CLI would bill one instead of your login. OpenCode keeps them: it may be set up to
# use them. An agent's pass_env lets one through.
OTHER_KEYS = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY", "GEMINI_API_KEY",
              "GOOGLE_API_KEY", "XAI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY",
              "DEEPSEEK_API_KEY", "TYPESAFE_API_KEY"]

CLI_PRESETS = [
    {
        "id": "claude_code", "label": "Claude Code", "command": "claude",
        "why": "uses your Claude subscription login",
        "install": "npm install -g @anthropic-ai/claude-code",
        # --append-system-prompt adds to Claude Code's own prompt (replacing it could break
        # subscription logins): it's a coding assistant by default and says so otherwise.
        # stream-json (with partial messages) prints the answer as it's written, so a verdict
        # shows up word by word; plain text would only arrive at the end.
        # disableAllHooks: your own hooks and plugins' hooks (a session-start hook that adds
        # notes or a style to every session) would otherwise reach the panel member's prompt.
        "args": ["-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
                 "--tools", "", "--strict-mcp-config", "--settings", '{"disableAllHooks": true}',
                 "--no-session-persistence", "--append-system-prompt", PANEL_MEMBER],
        # The prompt on stdin (`claude -p` reads it there): an argument would show in `ps` to other
        # people on this computer, and could be too long for a command line
        "prompt_via": "stdin", "timeout": 300, "stdout_format": "claude-stream-json",
        # An API key in the environment would be billed instead of the subscription
        # (CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token` still gets through).
        "drop_env": [*OTHER_KEYS],
        "effort_args": ["--effort", "{effort}"], "effort_levels": ["low", "medium", "high", "xhigh", "max"],
        "model_args": ["--model", "{model}"], "model_hint": "opus, sonnet, haiku or a full model name",
    },
    {
        "id": "codex", "label": "Codex", "command": "codex",
        "why": "uses your ChatGPT subscription login",
        "install": "npm install -g @openai/codex",
        # --ignore-user-config skips ~/.codex/config.toml (MCP servers, plugins, hooks);
        # the login still works. Read-only sandboxing alone would still let the
        # model run commands that read your files, so the shell is switched off too.
        "args": ["exec", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral", "--color", "never",
                 "--ignore-user-config", "--ignore-rules",
                 "--disable", "shell_tool", "--disable", "unified_exec", "--disable", "view_image",
                 "--disable", "multi_agent", "--disable", "goals", "-c", 'web_search="disabled"'],
        "prompt_via": "stdin", "output_flag": "-o", "timeout": 300,
        "drop_env": [*OTHER_KEYS],
        "effort_args": ["-c", 'model_reasoning_effort="{effort}"'],
        "effort_levels": ["minimal", "low", "medium", "high", "xhigh"],
        "model_args": ["-m", "{model}"], "model_hint": "a model name such as gpt-5.5",
    },
    {
        "id": "gemini_cli", "label": "Gemini CLI", "command": "gemini",
        "why": "uses your Gemini API key, or a work Google account",
        "install": "npm install -g @google/gemini-cli",
        "free": "free with a Gemini API key from aistudio.google.com (Google trains on its free tier, except in "
                "the EEA, Switzerland and the UK)",
        # Personal Google accounts can no longer sign in to Gemini CLI (since 2026-06-18), so the free way in is a key
        "free_then": "then paste the key when ixel setup asks for Google (Gemini)",
        # Plan mode is read-only and needs a trusted folder (otherwise it silently
        # falls back to default mode); the folder is the empty temp dir. No
        # extensions, and only an MCP server named "ixel-none" (there isn't one).
        "args": ["--skip-trust", "--approval-mode", "plan", "-e", "none",
                 "--allowed-mcp-server-names", "ixel-none", "-o", "text"],
        "prompt_via": "stdin", "timeout": 300,
        "drop_env": [*OTHER_KEYS],  # your Gemini API key goes only while it has no sign-in (gemini_key_env)
        "model_args": ["-m", "{model}"], "model_hint": "a model name such as gemini-2.5-pro",
    },
    {
        "id": "copilot", "label": "GitHub Copilot", "command": "copilot",
        "why": "uses your GitHub Copilot subscription",
        "install": "npm install -g @github/copilot",
        # --available-tools with a name that doesn't exist leaves the model no tools. --no-remote-export: where
        # GitHub has turned session indexing on for your account (and your organization allows it), Copilot
        # otherwise copies the session, question and answer, to GitHub's cloud session storage. Ixel adds
        # --session-id and --log-dir to each run, to remove what it keeps afterwards (agents/leftovers.py).
        "args": ["-s", "--no-custom-instructions", "--disable-builtin-mcps", "--no-auto-update",
                 "--stream", "off", "--available-tools=ixel_none", "--no-remote-export"],
        "prompt_via": "stdin", "timeout": 300,
        "drop_env": [*OTHER_KEYS, "COPILOT_ALLOW_ALL"],  # COPILOT_ALLOW_ALL: auto-approve tools, trust the folder
        "effort_args": ["--reasoning-effort", "{effort}"],
        "effort_levels": ["minimal", "low", "medium", "high", "xhigh", "max"],
        "model_args": ["--model", "{model}"], "model_hint": "a model name, or auto",
    },
    {
        "id": "opencode", "label": "OpenCode", "command": "opencode",
        "why": "uses the providers you've signed in to in OpenCode",
        "install": "npm install -g opencode-ai",
        # --standalone: OpenCode 2 otherwise hands the question to its background service, which
        # runs with your own settings, so the locked-down agent below wouldn't exist there.
        "args": ["run", "--standalone", "--agent", "ixel"],
        # OpenCode 1 starts a server of its own every time and has no --standalone; its --pure leaves
        # out plugins you installed
        "args_by_version": {"1": ["run", "--pure", "--agent", "ixel"]},
        # Whatever args you give it yourself: without --standalone, OpenCode 2's background service has no
        # locked-down agent, and would answer with its own one and your tools. Each version's flag is one the
        # other refuses, so args picked for a version Ixel only thinks is installed fail instead of running
        "required_args": {"1": ["--pure"], "*": ["--standalone"]},
        # Like Claude Code's: on stdin, out of `ps` and never too long. `opencode run` reads stdin
        # whenever it isn't a terminal, and uses it as the message.
        "prompt_via": "stdin", "timeout": 300,
        # OPENCODE_DISABLE_MODELS_FETCH: never fetch OpenCode's model catalog (models.opencode.ai, models.dev
        # on older versions); it uses the copy it already has, or the one built into it
        "env": {"OPENCODE_CONFIG_CONTENT": _OPENCODE_LOCKDOWN, "OPENCODE_DISABLE_AUTOUPDATE": "1",
                "OPENCODE_DISABLE_MODELS_FETCH": "1"},
        "model_args": ["-m", "{model}"], "model_hint": "provider/model, e.g. anthropic/claude-opus-5-5",
    },
]


# Fields a preset supplies; an agent's own config overrides any of them.
PRESET_FIELDS = ("command", "args", "args_by_version", "prompt_via", "output_flag", "stdout_format", "timeout",
                 "env", "drop_env", "effort_args", "effort_levels", "model_args")
PRESETS_BY_ID = {p["id"]: p for p in CLI_PRESETS}
# Fields that describe a preset for people (setup, `ixel model`) and never reach an agent's config.
PRESET_ABOUT = ("id", "why", "install", "free", "free_then", "model_hint")


def preset_for(command: str) -> dict:
    """The preset whose program command runs (by its name, wherever it's installed), or {}."""
    name = Path(command).stem.lower() if command else ""
    return next((p for p in CLI_PRESETS if p["command"] == name), {})


# Settings of a preset's env that Ixel gives its program whenever it runs it, whatever an agent's own env says and
# whether or not the agent names the preset: OpenCode's locked-down agent, and no fetching of its model catalog
# (checked against OpenCode 1.18.18, 1.18.34 and 2.0.22: with it, none of them asked for the catalog)
LOCKED_ENV = {"opencode": ("OPENCODE_CONFIG_CONTENT", "OPENCODE_DISABLE_MODELS_FETCH")}


def locked_env(command: str) -> dict[str, str]:
    """What Ixel always sets for the program command runs (see LOCKED_ENV), or {}."""
    preset = preset_for(command)
    return {name: preset["env"][name] for name in LOCKED_ENV.get(preset.get("id", ""), ())}


# ── Gemini CLI's sign-in ──────────────────────────────────────────────────────

# What to do when Gemini CLI has no way to sign in: its own message names only settings and variables.
# At most 160 characters, so the terminal shows all of it.
GEMINI_SIGN_IN = ("Gemini CLI isn't signed in: add a free Gemini API key (aistudio.google.com) in Settings under "
                  "Keys, or run gemini once to sign in, or take Gemini off the panel.")
# OpenCode's free tier refuses an agent of anyone else's, like Ixel's locked-down one
OPENCODE_FREE_TIER = ("OpenCode's free tier only works inside OpenCode: sign in to a provider (opencode auth login) "
                      "and pick its model in Settings, or take OpenCode off the panel.")
# OpenCode listing only OpenCode Zen's free models: no provider you've connected, and no Zen credit (which would
# list Zen's paid ones too), so what it would answer Ixel with turns Ixel down
OPENCODE_ONLY_FREE = ("OpenCode has only its free models, which answer only inside OpenCode. To use it in Ixel, "
                      "connect a provider in OpenCode (opencode auth login), then pick one of its models.")
# OpenCode 1 started on the data OpenCode 2 has moved on
OPENCODE_OLD_COPY = ("This OpenCode is older than the data a newer one left: put the newer OpenCode first on PATH, "
                     "or set its full path as OpenCode's command.")
# OpenCode 2 refusing a model: its provider isn't signed in, or it's newer than OpenCode's list, which Ixel keeps
# it from fetching
OPENCODE_UNKNOWN_MODEL = ("OpenCode can't use this model: sign in to its provider in OpenCode, or pick one from its "
                          "list in Settings (Ixel keeps OpenCode from fetching new ones).")
# A Copilot from before the settings Ixel runs it with, which keep its sessions and logs where Ixel removes them
# (--session-id) and off GitHub (--no-remote-export)
COPILOT_TOO_OLD = ("This Copilot is older than Ixel needs (1.0.52, May 2026): run npm install -g @github/copilot, "
                   "or take Copilot off the panel.")
# What a preset's CLI says on stderr when it can't start on a question at all, and what to tell you instead
PLAIN_ERRORS = {"gemini_cli": {"Please set an Auth method": GEMINI_SIGN_IN},
                "copilot": {"unknown option '--no-remote-export'": COPILOT_TOO_OLD,
                            "unknown option '--session-id'": COPILOT_TOO_OLD},
                "opencode": {"free tier can only be used from within OpenCode": OPENCODE_FREE_TIER,
                             "Database is not empty and has no session table": OPENCODE_OLD_COPY,
                             "Model unavailable:": OPENCODE_UNKNOWN_MODEL}}


def opencode_only_free(models: list[str]) -> bool:
    """Whether OpenCode's model list (provider/model names, as `opencode models` prints them) has only OpenCode Zen's
    free models (OPENCODE_ONLY_FREE): named …-free, and Big Pickle. A model it doesn't know to be free counts as one
    that may answer, so a new free one only means no warning."""
    return bool(models) and all(m.startswith("opencode/") and (m.endswith("-free") or m == "opencode/big-pickle")
                                for m in models)


# Where your Gemini API key can be: Gemini CLI's own name for it, or the Google (Gemini) key in Ixel's Keys
GEMINI_KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
# Variables that choose a sign-in for Gemini CLI when set to "true", and GOOGLE_GEMINI_BASE_URL (a gateway,
# which would be sent the key) when set at all
_GEMINI_SIGN_IN_FLAGS = ("GOOGLE_GENAI_USE_GCA", "GOOGLE_GENAI_USE_VERTEXAI", "CLOUD_SHELL",
                         "GEMINI_CLI_USE_COMPUTE_ADC")
# All Gemini CLI takes from ~/.env when it doesn't trust the folder it runs in, as it doesn't Ixel's empty one
# (it reads .env files before --skip-trust counts), and then it reads no ~/.gemini/.env at all
_GEMINI_UNTRUSTED_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION")


def plain_error(preset_id: str, stderr: str) -> str | None:
    """What to tell you, for an error from a preset's CLI that Ixel knows a plainer way to say."""
    return next((tell for says, tell in PLAIN_ERRORS.get(preset_id, {}).items() if says in stderr), None)


def _gemini_home(env: Mapping[str, str]) -> Path:
    return Path(env.get("GEMINI_CLI_HOME") or Path.home())


def _gemini_settings_files(env: Mapping[str, str]) -> list[Path]:
    """The settings files Gemini CLI reads its sign-in from: the system's defaults, yours, the system's."""
    system = env.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH") or {
        "darwin": "/Library/Application Support/GeminiCli/settings.json",
        "win32": "C:\\ProgramData\\gemini-cli\\settings.json"}.get(sys.platform, "/etc/gemini-cli/settings.json")
    defaults = env.get("GEMINI_CLI_SYSTEM_DEFAULTS_PATH") or os.path.join(os.path.dirname(system),
                                                                        "system-defaults.json")
    return [Path(defaults), _gemini_home(env) / ".gemini" / "settings.json", Path(system)]


def _without_comments(text: str) -> str:
    """Settings with comments, which Gemini CLI allows, as plain JSON: // and /* */ outside strings taken out."""
    out, i = [], 0
    while i < len(text):
        if text[i] == '"':
            end = i + 1
            while end < len(text) and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            out.append(text[i:end + 1])
            i = end + 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _gemini_settings(env: Mapping[str, str]) -> list[dict | None]:
    """Each settings file Gemini CLI reads that's there, as read: None for one that can't be read."""
    found = []
    for path in _gemini_settings_files(env):
        try:
            data = json.loads(_without_comments(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            data = None
        found.append(data if isinstance(data, dict) else None)
    return found


def _setting(data: dict, *keys: str) -> object:
    for key in keys:
        data = data.get(key) if isinstance(data, dict) else None
    return data


def _gemini_env_file(env: Mapping[str, str], trusted: bool) -> dict[str, str]:
    """The variables Gemini CLI takes from your ~/.gemini/.env, or else ~/.env (the empty folder it runs in has
    none), reading only the first that's there; in a folder it doesn't trust, only a few from ~/.env."""
    home = _gemini_home(env)
    for path in (home / ".gemini" / ".env", home / ".env") if trusted else (home / ".env",):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return {}
        found = {}
        for line in text.splitlines():
            name, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and not name.startswith("#") and (trusted or name.strip() in _GEMINI_UNTRUSTED_ENV):
                found[name.strip()] = value.strip().strip("'\"")
        return found
    return {}


def gemini_has_sign_in(env: Mapping[str, str]) -> bool:
    """
    Whether Gemini CLI, run with env, can sign in on its own. Decided as `gemini -p` decides: the sign-in its
    settings name (security.auth.selectedType, saved when you sign in by running gemini), else one its
    variables choose, or a key in its own .env file. The files a Google login leaves don't count: without
    that setting the CLI doesn't use them, and stops with "Please set an Auth method".
    """
    settings = _gemini_settings(env)
    # One it can't read: Gemini CLI won't start with it either, and taken as signed in, the key stays out
    if any(data is None or _setting(data, "security", "auth", "selectedType") for data in settings):
        return True
    trusted = env.get("GEMINI_CLI_TRUST_WORKSPACE") == "true" \
        or any(_setting(data, "security", "folderTrust", "enabled") is False for data in settings)
    own = _gemini_env_file(env, trusted)
    variables = {**own, **env}  # the file's don't replace the ones it's started with
    # A gateway named in ~/.gemini/.env counts even where Ixel judges the folder untrusted (a trustedFolders
    # entry could make the CLI read it): it would be sent the key
    gateway = variables.get("GOOGLE_GEMINI_BASE_URL") or _gemini_env_file(env, True).get("GOOGLE_GEMINI_BASE_URL")
    return any(variables.get(name) == "true" for name in _GEMINI_SIGN_IN_FLAGS) or bool(gateway) \
        or bool(own.get("GEMINI_API_KEY"))


def gemini_key_env(env: Mapping[str, str], keys: Mapping[str, str]) -> dict[str, str]:
    """
    What to add to Gemini CLI's environment env: your Gemini API key as GEMINI_API_KEY (the first of
    GEMINI_KEYS in keys, Ixel's own environment, which has the keys saved in Settings), but only while the
    CLI has no sign-in of its own. drop_env keeps it out otherwise, as it keeps keys from the other
    subscription CLIs; with no sign-in, Gemini CLI stops before it asks anything, so the key can't be
    billed in place of a login.
    """
    if env.get("GEMINI_API_KEY") or gemini_has_sign_in(env):
        return {}
    key = next((keys[name] for name in GEMINI_KEYS if keys.get(name)), "")
    return {"GEMINI_API_KEY": key} if key else {}
