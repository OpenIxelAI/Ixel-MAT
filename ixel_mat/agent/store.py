"""
What the agent keeps between runs, in a folder of plain files you can read and edit.

Under ~/.config/ixel-mat/agent/ (the folder Ixel's config uses, on Windows too):

  SOUL.md, USER.md, MEMORY.md   its persona, who you are, what it has learned (made with short defaults
                                the first time, each clipped to 16 KB in the prompt)
  skills/<name>/SKILL.md        skills: front matter with name: and description:, and the text, which the
                                agent reads only when it loads the skill
  sessions/<id>.json            each run: its goal, model, workspace, steps, approvals and usage
  allow.json                    the commands you always allow, by workspace

Every file is written the way Ixel writes its config (private to you, and atomically). The allowlist never
matches a shell or an interpreter given code (sandbox.is_shell_or_interpreter), even if allow.json is
edited by hand to hold one: that's checked again each time a command is matched.
"""
from __future__ import annotations

import json
import os
import re
import threading
from contextlib import suppress
from datetime import date
from pathlib import Path
from typing import Any

from ixel_mat.agent.sandbox import is_shell_or_interpreter
from ixel_mat.agent.tools import is_secret_name
from ixel_mat.config import secrets as _secrets
from ixel_mat.config.secrets import write_private_file

MAX_PROMPT_FILE_CHARS = 16 * 1024
FILE_CLIP_NOTE = "[clipped to 16 KB for the prompt; the rest is still in the file]"
MAX_MEMORY_LINE_CHARS = 2000
MAX_SKILL_CHARS = 64 * 1024
MAX_SKILL_DESCRIPTION_CHARS = 500
MAX_SESSIONS_LISTED = 50

# The starter files a new agent folder begins with (ixel_mat/agent/starter/, written by a small local model
# and checked by hand): SOUL.md, USER.md, MEMORY.md, and the example skills under skills/
STARTER = Path(__file__).parent / "starter"


def _starter(name: str) -> str:
    try:
        return (STARTER / name).read_text(encoding="utf-8")
    except OSError:  # an install without the package data still starts, with a header only
        return f"# {name.removesuffix('.md').title()}\n"


SOUL_DEFAULT = _starter("SOUL.md")
USER_DEFAULT = _starter("USER.md")
MEMORY_DEFAULT = _starter("MEMORY.md")

_FILES = {"soul": ("SOUL.md", SOUL_DEFAULT), "user": ("USER.md", USER_DEFAULT),
          "memory": ("MEMORY.md", MEMORY_DEFAULT)}
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
# One change at a time to MEMORY.md and allow.json (the runner and the app's pages share a process)
_LOCK = threading.RLock()


def default_root() -> Path:
    """The agent's folder, inside Ixel's own config folder. Read each time, so tests can move it."""
    return Path(_secrets._ENV_DIR) / "agent"


def _safe_component(name: Any) -> bool:
    """Whether name is one plain file or folder name: no slashes, not '..', not hidden."""
    return (isinstance(name, str) and bool(name) and name == name.strip() and "/" not in name
            and "\\" not in name and "\x00" not in name and not name.startswith(".") and ":" not in name)


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


def _check_argv(argv: Any) -> list[str]:
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(word, str) for word in argv):
        raise ValueError("a command is a non-empty list of text")
    return list(argv)


def _workspace_key(workspace: Path | str) -> str:
    return os.path.realpath(os.fspath(workspace))


def _front_matter(text: str) -> dict[str, str]:
    """name: and description: from a SKILL.md's leading '---' block, read by hand (a value may be quoted,
    or folded over the indented lines after a lone > or |)."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    found: dict[str, str] = {}
    key = ""
    for line in lines[1:]:
        if line.strip() in ("---", "..."):
            break
        if key and line[:1] in (" ", "\t"):  # more of a folded value
            found[key] = (found[key] + " " + line.strip()).strip()
            continue
        key = ""
        name, sep, value = line.partition(":")
        name = name.strip().lower()
        if not sep or name not in ("name", "description"):
            continue
        value = value.strip()
        if value in (">", "|", ">-", "|-", ""):
            key, value = name, ""
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        found[name] = value
    return found


class AgentStore:
    """The agent's files: persona, user and memory, skills, sessions and the allowlist. root defaults to
    ~/.config/ixel-mat/agent; tests give a folder of their own."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else default_root()

    # ── Folders ──────────────────────────────────────────────────────────────

    def _ensure(self) -> None:
        """The agent's folder and its skills/ and sessions/, made on first use, private where the system
        allows (0700)."""
        for folder in (self.root, self.root / "skills", self.root / "sessions"):
            if not folder.is_dir():
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                if os.name == "posix":
                    with suppress(OSError):
                        os.chmod(folder, 0o700)
                if folder.name == "skills":
                    self._copy_starter_skills(folder)

    @staticmethod
    def _copy_starter_skills(skills: Path) -> None:
        """The example skills, into a skills/ folder made just now (never over one you have)."""
        with suppress(OSError):
            for skill in sorted((STARTER / "skills").iterdir()):
                text = skill / "SKILL.md"
                if skill.is_dir() and text.is_file() and not (skills / skill.name).exists():
                    (skills / skill.name).mkdir(mode=0o700)
                    write_private_file(skills / skill.name / "SKILL.md", text.read_bytes())

    # ── SOUL.md, USER.md, MEMORY.md ─────────────────────────────────────────

    def _file(self, name: str) -> tuple[Path, str]:
        if name not in _FILES:
            raise ValueError(f"no file {name!r}; one of: {', '.join(_FILES)}")
        file_name, default = _FILES[name]
        return self.root / file_name, default

    def file_text(self, name: str) -> str:
        """The text of 'soul', 'user' or 'memory', made with its short default the first time."""
        path, default = self._file(name)
        with _LOCK:
            self._ensure()
            try:
                return _read(path)
            except FileNotFoundError:
                write_private_file(path, default.encode("utf-8"))
                return default

    def set_file_text(self, name: str, text: str) -> None:
        """Replace 'soul', 'user' or 'memory' with text (from the app, where you edit them)."""
        path, _ = self._file(name)
        if not isinstance(text, str):
            raise ValueError("the text must be text")
        with _LOCK:
            self._ensure()
            write_private_file(path, text.encode("utf-8"))

    def prompt_files(self) -> dict[str, str]:
        """soul, user and memory for the prompt, each clipped to 16 KB (with a note saying so)."""
        files = {}
        for name in _FILES:
            text = self.file_text(name)
            if len(text) > MAX_PROMPT_FILE_CHARS:
                text = text[:MAX_PROMPT_FILE_CHARS] + "\n" + FILE_CLIP_NOTE
            files[name] = text
        return files

    def remember(self, text: str) -> str:
        """Add one dated line to MEMORY.md ('- YYYY-MM-DD: text', on one line, at most 2000 characters of
        text) and return it."""
        if not isinstance(text, str):
            raise ValueError("what to remember must be text")
        text = " ".join(text.split())
        if not text:
            raise ValueError("there's nothing to remember: the text is empty")
        if len(text) > MAX_MEMORY_LINE_CHARS:
            text = text[:MAX_MEMORY_LINE_CHARS] + " […]"
        line = f"- {date.today().isoformat()}: {text}"
        path, _ = self._file("memory")
        with _LOCK:
            current = self.file_text("memory")
            if current and not current.endswith("\n"):
                current += "\n"
            write_private_file(path, (current + line + "\n").encode("utf-8"))
        return line

    # ── Skills ───────────────────────────────────────────────────────────────

    def _skill_path(self, name: str) -> Path:
        """A skill's real SKILL.md, confined to skills/ even when a downloaded skill contains symlinks."""
        if not _safe_component(name):
            raise KeyError(f"no skill named {str(name)[:60]!r}; use a name from the skill index")
        try:
            base = self.root.resolve() / "skills"
            skill = (base / name / "SKILL.md").resolve()
            parts = skill.relative_to(base).parts
        except (OSError, RuntimeError, ValueError):
            raise KeyError(f"the skill {name!r} leads outside the skills folder") from None
        if not parts or skill.name != "SKILL.md" or any(part.lower() == ".git" or is_secret_name(part)
                                                       for part in parts):
            raise KeyError(f"the skill {name!r} isn't a plain SKILL.md inside the skills folder")
        return skill

    def skills(self) -> list[dict[str, str]]:
        """Each skill's name and description, sorted by name, from skills/*/SKILL.md (folders without one
        are skipped). The name is the folder's, which is what load_skill takes (the AgentSkills format has
        the front matter's name match it); the description comes from the front matter."""
        self._ensure()
        found = []
        try:
            folders = [entry for entry in (self.root / "skills").iterdir() if entry.is_dir()]
        except OSError:
            return []
        for folder in folders:
            try:
                skill = self._skill_path(folder.name)
                if not skill.is_file():
                    continue
                with skill.open("rb") as handle:
                    head = handle.read(MAX_SKILL_CHARS).decode("utf-8", errors="replace")
            except (KeyError, OSError):
                continue
            description = " ".join(_front_matter(head).get("description", "").split())
            if len(description) > MAX_SKILL_DESCRIPTION_CHARS:
                description = description[:MAX_SKILL_DESCRIPTION_CHARS] + " […]"
            found.append({"name": folder.name, "description": description})
        return sorted(found, key=lambda skill: skill["name"])

    def load_skill(self, name: str) -> str:
        """A skill's whole SKILL.md (at most 64 KB), by its folder's exact name. KeyError when there's none."""
        skill = self._skill_path(name)
        try:
            with skill.open("rb") as handle:
                data = handle.read(MAX_SKILL_CHARS * 4 + 1)
        except (FileNotFoundError, NotADirectoryError):
            raise KeyError(f"no skill named {name!r}; use a name from the skill index") from None
        except OSError as exc:
            raise KeyError(f"the skill {name!r} can't be read: {exc.strerror or type(exc).__name__}") from None
        text = data.decode("utf-8", errors="replace")
        if len(text) > MAX_SKILL_CHARS:
            text = text[:MAX_SKILL_CHARS] + "\n[… the rest of this skill was cut at 64 KB]"
        return text

    # ── Sessions ─────────────────────────────────────────────────────────────

    def _session_path(self, session_id: Any) -> Path:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError("a session id is letters, digits, - and _ only")
        return self.root / "sessions" / f"{session_id}.json"

    def save_session(self, data: dict) -> None:
        """Save a run as sessions/<id>.json; data['id'] is letters, digits, - and _ only."""
        if not isinstance(data, dict):
            raise ValueError("a session is a dict")
        path = self._session_path(data.get("id"))
        self._ensure()
        write_private_file(path, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))

    def load_session(self, session_id: str) -> dict:
        """A saved run. KeyError when there's none with that id, or it can't be read."""
        try:
            path = self._session_path(session_id)
        except ValueError:
            raise KeyError(f"no session {str(session_id)[:60]!r}") from None
        try:
            data = json.loads(_read(path))
        except FileNotFoundError:
            raise KeyError(f"no session {session_id!r}") from None
        except (OSError, ValueError):
            raise KeyError(f"the session {session_id!r} can't be read") from None
        if not isinstance(data, dict):
            raise KeyError(f"the session {session_id!r} can't be read")
        return data

    def list_sessions(self) -> list[dict]:
        """The saved runs as {id, goal, status, started}, newest first, at most 50. Files that can't be read
        are left out."""
        self._ensure()
        runs = []
        for path in (self.root / "sessions").glob("*.json"):
            try:
                data = json.loads(_read(path))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or data.get("id") != path.stem:
                continue
            started = data.get("started")
            if isinstance(started, bool) or not isinstance(started, (int, float)):
                started = 0
            runs.append({"id": path.stem, "goal": str(data.get("goal") or ""),
                         "status": str(data.get("status") or ""), "started": started})
        runs.sort(key=lambda run: run["started"], reverse=True)
        return runs[:MAX_SESSIONS_LISTED]

    # ── The allowlist ────────────────────────────────────────────────────────

    def _allow_file(self) -> dict[str, list[list[str]]]:
        """allow.json as {workspace real path: [argv prefix, …]}, leaving out anything malformed."""
        try:
            data = json.loads(_read(self.root / "allow.json"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        clean: dict[str, list[list[str]]] = {}
        for key, entries in data.items():
            if not isinstance(key, str) or not isinstance(entries, list):
                continue
            clean[key] = [list(entry) for entry in entries
                          if isinstance(entry, list) and entry and all(isinstance(word, str) for word in entry)]
        return clean

    def allow(self, workspace: Path | str, argv: list[str]) -> None:
        """Always allow commands that start with argv in this workspace ("Always allow" on an approval).
        ValueError for a shell or an interpreter given code: those always ask."""
        argv = _check_argv(argv)
        if is_shell_or_interpreter(argv):
            raise ValueError("shells and interpreters running code (sh, bash, python -c, node -e …) always ask; "
                             "they can't be always allowed")
        key = _workspace_key(workspace)
        with _LOCK:
            self._ensure()
            data = self._allow_file()
            entries = data.setdefault(key, [])
            if argv not in entries:
                entries.append(argv)
                write_private_file(self.root / "allow.json",
                                   json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))

    def is_allowed(self, workspace: Path | str, argv: list[str]) -> bool:
        """Whether argv may run in this workspace without asking: some allowed entry is exactly its first
        words, and it isn't a shell or an interpreter given code (checked here too, whatever allow.json says)."""
        try:
            argv = _check_argv(argv)
        except ValueError:
            return False
        if is_shell_or_interpreter(argv):
            return False
        entries = self._allow_file().get(_workspace_key(workspace), [])
        return any(argv[:len(entry)] == entry and not is_shell_or_interpreter(entry) for entry in entries)

    def allowed(self, workspace: Path | str) -> list[list[str]]:
        """The commands always allowed in this workspace, as argv prefixes."""
        return self._allow_file().get(_workspace_key(workspace), [])
