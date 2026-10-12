"""The app's Agent page: configured models, runs and the agent's plain files.

Live runs stay in this server's memory and save themselves as sessions as they go. A saved session can
still be read after Ixel restarts; an approval can only resume a run that is live in this server.
"""
from __future__ import annotations

import asyncio
import copy
import re
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from ixel_mat.agent.protocol import MAX_GOAL_CHARS
from ixel_mat.agent.store import AgentStore
from ixel_mat.ask import is_ready

if TYPE_CHECKING:
    from ixel_mat.agent.runner import AgentRun
    from ixel_mat.runtime import Settings

MAX_RESULT_BYTES = 4 * 1024
MAX_RUNS = 50
_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
Ask = Callable[[str], Awaitable[tuple[str, dict | None]]]


class AgentApiError(ValueError):
    """Something the Agent page can tell the person, with an HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def check_workspace(value: object) -> Path:
    """An existing folder below the person's home, with links resolved before checking where it is."""
    if not isinstance(value, str) or not value.strip():
        raise AgentApiError("Pick a workspace: a folder inside your home folder.")
    value = value.strip()
    if len(value) > 1000 or any(c in value for c in "\x00\r\n"):
        raise AgentApiError("That isn't a folder's path.")
    try:
        home = Path.home().resolve(strict=True)
        path = home if value == "~" else home / value[2:] if value.startswith("~/") else Path(value).expanduser()
        if not path.is_absolute():
            raise AgentApiError("Give the workspace's full path, like ~/code/shop.")
        path = path.resolve(strict=True)
        if not path.is_dir():
            raise AgentApiError("The workspace must be an existing folder.")
        if path == home or home not in path.parents:
            raise AgentApiError("Pick a folder inside your home folder, rather than the home folder itself.")
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, AgentApiError):
            raise
        raise AgentApiError("The workspace must be an existing folder inside your home folder.") from None
    return path


def _run_id(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise AgentApiError("Give the run's id.")
    return value


def page_run(data: dict) -> dict:
    """A saved run in the page's shape, with results clipped to 4 KB, as live runs are."""
    out = {name: copy.deepcopy(data.get(name, default)) for name, default in (
        ("id", ""), ("status", "failed"), ("goal", ""), ("model", ""), ("workspace", ""),
        ("started", 0), ("answer", ""), ("error", ""), ("usage", {"calls": 0, "cost": 0.0}),
    )}
    out["steps"] = []
    for step in data.get("steps", []):
        if not isinstance(step, dict):
            continue
        entry = {name: copy.deepcopy(step.get(name, default)) for name, default in (
            ("n", 0), ("thought", ""), ("action", ""), ("args", {}), ("ok", False),
            ("result", ""), ("usage", {}),
        )}
        entry["result"] = str(entry["result"]).encode("utf-8")[:MAX_RESULT_BYTES].decode("utf-8", "ignore")
        entry["usage"] = entry["usage"] or {}
        pictures = step.get("pictures")
        if isinstance(pictures, int) and not isinstance(pictures, bool):
            entry["pictures"] = pictures
        out["steps"].append(entry)
    uploads = data.get("uploads", [])
    out["uploads"] = copy.deepcopy(uploads) if isinstance(uploads, list) else []
    out["pending"] = copy.deepcopy(data.get("pending")) if out["status"] == "waiting" else None
    return out


class AgentApi:
    """Runs this server knows about, and the files and sessions the Agent page can read and change."""

    def __init__(self, store: AgentStore | None = None, ask: Ask | None = None):
        self.store = store if store is not None else AgentStore()
        self.ask = ask
        self.runs: dict[str, AgentRun] = {}

    @staticmethod
    def models(settings: Settings) -> dict:
        """Every configured model, ready when its key is set and Private permits it."""
        return {"models": [{"name": cfg.name, "label": cfg.label,
                            "ready": is_ready(cfg) and settings.yours(cfg.name)}
                           for cfg in settings.agent_configs.values()]}

    def start(self, settings: Settings, body: dict) -> dict:
        """Start one run; the page gets its id at once and polls for its steps."""
        from ixel_mat.agent.runner import AgentRun

        model = body.get("model")
        if not isinstance(model, str) or model not in settings.agent_configs:
            raise AgentApiError("Pick one of your configured models.")
        cfg = settings.agent_configs[model]
        if not settings.yours(model):
            raise AgentApiError("Private is on, and this model sends requests outside your computers. "
                                "Pick one of your own models or turn Private off in Settings.", 403)
        if not is_ready(cfg):
            raise AgentApiError(f"{cfg.label} has no API key yet. Add it in Settings.")
        goal = body.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise AgentApiError("Give the agent a goal first.")
        if len(goal) > MAX_GOAL_CHARS:
            raise AgentApiError(f"The goal is longer than {MAX_GOAL_CHARS:,} characters.")
        try:
            goal.encode("utf-8")
        except UnicodeEncodeError:
            raise AgentApiError("The goal must be valid Unicode.") from None
        max_steps = body.get("max_steps", 25)
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1:
            raise AgentApiError("max_steps must be a positive whole number.")
        workspace = check_workspace(body.get("workspace"))
        run = AgentRun(cfg, workspace, goal.strip(), self.store, max_steps=max_steps, ask=self.ask)
        self.runs[run.id] = run
        run.start()
        return {"id": run.id}

    def run(self, value: object) -> dict:
        """One live run or saved session; saved sessions don't need a model connection."""
        run_id = _run_id(value)
        if run_id in self.runs:
            return self.runs[run_id].to_dict()
        try:
            return page_run(self.store.load_session(run_id))
        except KeyError:
            raise AgentApiError("There's no run with that id.", 404) from None

    async def decide(self, body: dict) -> dict:
        """Approve or deny a pending action in a live run."""
        run_id = _run_id(body.get("id"))
        approve, always = body.get("approve"), body.get("always", False)
        if not isinstance(approve, bool) or not isinstance(always, bool):
            raise AgentApiError("approve and always must be true or false.")
        data = self.run(run_id)
        if run_id not in self.runs or data["status"] != "waiting" or data["pending"] is None:
            raise AgentApiError("This run isn't waiting for an approval in this Ixel session.", 409)
        if always and (not approve or data["pending"]["action"] != "run_command"):
            raise AgentApiError("Always allow is only for a command you approve.")
        try:
            return await self.runs[run_id].decide(approve, always)
        except ValueError as exc:
            raise AgentApiError(str(exc)) from None

    async def stop(self, body: dict) -> dict:
        """Stop a live run, or mark an unfinished saved session stopped after a restart."""
        run_id = _run_id(body.get("id"))
        if run_id in self.runs:
            return await self.runs[run_id].stop()
        data = self.run(run_id)
        if data["status"] in ("running", "waiting"):
            saved = self.store.load_session(run_id)
            saved.update(status="stopped", pending=None)
            self.store.save_session(saved)
            data = page_run(saved)
        return data

    def saved_runs(self) -> dict[str, dict]:
        """Saved sessions for the list, read off the server's event loop."""
        found = {}
        for summary in self.store.list_sessions():
            try:
                found[summary["id"]] = self.store.load_session(summary["id"])
            except KeyError:
                continue
        return found

    def list_runs(self, saved: dict[str, dict] | None = None) -> dict:
        """Newest first, with the model and workspace the page uses to pre-fill its form."""
        found = dict(saved) if saved is not None else self.saved_runs()
        found.update({run_id: run.to_dict() for run_id, run in self.runs.items()})
        rows = []
        for data in found.values():
            started = data.get("started", 0)
            if isinstance(started, bool) or not isinstance(started, (int, float)):
                started = 0
            rows.append({"id": data["id"], "goal": str(data.get("goal") or ""),
                         "status": str(data.get("status") or ""), "started": started,
                         "model": str(data.get("model") or ""), "workspace": str(data.get("workspace") or "")})
        rows.sort(key=lambda row: row["started"], reverse=True)
        return {"runs": rows[:MAX_RUNS]}

    def files(self) -> dict:
        """The persona, user and memory files, and the skill names and descriptions."""
        return {**{name: self.store.file_text(name) for name in ("soul", "user", "memory")},
                "skills": self.store.skills()}

    def set_file(self, body: dict) -> dict:
        name, text = body.get("name"), body.get("text")
        if name not in ("soul", "user", "memory") or not isinstance(text, str):
            raise AgentApiError("name must be soul, user or memory, and text must be text.")
        try:
            self.store.set_file_text(name, text)
        except UnicodeEncodeError:
            raise AgentApiError("The file's text must be valid Unicode.") from None
        return self.files()

    async def close(self) -> None:
        """Model calls and commands still running stop when this Ixel server stops."""
        await asyncio.gather(*(run.stop() for run in self.runs.values()
                               if run.to_dict()["status"] in ("running", "waiting")))
