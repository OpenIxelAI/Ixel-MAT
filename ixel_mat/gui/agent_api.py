"""The app's Agent page: configured models, runs and the agent's plain files.

Live runs stay in this server's memory and save themselves as sessions as they go. A saved session can
still be read after Ixel restarts; an approval can only resume a run that is live in this server.

A file attached in the composer is uploaded before the run starts, so it's kept under a pending id of its own
(uploads/<pending id>/) until a start names it; the run then takes it into its own uploads/<run id>/. Pending
files live as long as this server: they're removed when it stops.
"""
from __future__ import annotations

import asyncio
import copy
import re
import secrets
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from ixel_mat import effort as effort_levels
from ixel_mat.agent.protocol import MAX_GOAL_CHARS
from ixel_mat.agent.store import MAX_UPLOAD_BYTES as MAX_UPLOAD_BYTES  # the server reads it from here
from ixel_mat.agent.store import MAX_UPLOADS_PER_RUN, AgentStore
from ixel_mat.ask import is_ready

if TYPE_CHECKING:
    from ixel_mat.agent.runner import AgentRun
    from ixel_mat.runtime import Settings

MAX_RESULT_BYTES = 4 * 1024
MAX_RUNS = 50
FAST_MAX_STEPS = 10   # Fast: at most this many steps, at the model's lowest effort
FOLLOW_STEPS = 3      # a follow-up's prompt holds the earlier run's last steps, this many
# MAX_UPLOAD_BYTES (50 MB) is the upload route's limit, read when a file comes in (a test can lower it)
MAX_PENDING_UPLOADS = 5 * MAX_UPLOADS_PER_RUN  # files attached and not yet sent; the oldest go past this
_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
_FINISHED = ("done", "stopped", "failed")
COMPUTER_WARNING = ("Computer access needs its warning confirmed first: it lets the agent work anywhere in your "
                    "home folder, run commands without the sandbox and with the network, take screenshots and "
                    "open apps, files and links. Confirm the warning, then send again.")
# A stand-in for the model gets the prompt, and pictures= and effort= when a call has them
Ask = Callable[..., Awaitable[tuple[str, dict | None]]]


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
        for name in ("ms", "batch"):  # a step's time, and the first step of the reads it was run with
            value = step.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                entry[name] = value
        if isinstance(step.get("tokens"), dict):
            entry["tokens"] = copy.deepcopy(step["tokens"])
        out["steps"].append(entry)
    uploads = data.get("uploads", [])
    out["uploads"] = copy.deepcopy(uploads) if isinstance(uploads, list) else []
    out["computer"] = data.get("computer") is True  # a run saved before Computer access existed had it off
    follows = data.get("follows")
    out["follows"] = follows if isinstance(follows, str) and _ID.fullmatch(follows) else None
    out["pending"] = copy.deepcopy(data.get("pending")) if out["status"] == "waiting" else None
    return out


class AgentApi:
    """Runs this server knows about, and the files and sessions the Agent page can read and change."""

    def __init__(self, store: AgentStore | None = None, ask: Ask | None = None):
        self.store = store if store is not None else AgentStore()
        self.ask = ask
        self.runs: dict[str, AgentRun] = {}
        # Files attached for a run that hasn't started yet: pending id -> the {id, name, kind, size} the page got
        self.pending_uploads: dict[str, dict] = {}
        self._pending_lock = threading.Lock()  # uploads are saved off the event loop

    def save_upload(self, name: str, data: bytes) -> dict:
        """Keep one attached file under a new pending id until a start names it → {id, name, kind, size}.
        The id is the pending one; the run lists the file by its stored name once it has taken it. Blocking:
        the server calls it in a thread. Past MAX_PENDING_UPLOADS, the oldest pending file is let go."""
        pending = secrets.token_hex(16)
        try:
            info = self.store.save_upload(pending, name, data)
        except ValueError as exc:
            raise AgentApiError(f"Couldn't attach that file: {exc}.") from None
        reply = {"id": pending, "name": info["name"], "kind": info["kind"], "size": info["size"]}
        with self._pending_lock:
            self.pending_uploads[pending] = reply
            dropped = list(self.pending_uploads)[:-MAX_PENDING_UPLOADS]
            for old in dropped:
                del self.pending_uploads[old]
        for old in dropped:
            self.store.delete_uploads(old)
        return dict(reply)

    def _claim(self, claimed: dict[str, dict], run_id: str) -> None:
        """Move attached files from their pending folders into the run's own, by their stored names. On a
        failure the run's folder goes too, so a run never starts with only some of them; the pending folders
        go either way. Blocking: called in a thread."""
        try:
            for pending, info in claimed.items():
                data = (self.store.uploads_dir(pending) / info["name"]).read_bytes()
                self.store.save_upload(run_id, info["name"], data)
        except ValueError as exc:
            self.store.delete_uploads(run_id)
            raise AgentApiError(f"Couldn't attach a file to the run: {exc}. Attach it again.") from None
        except OSError:
            self.store.delete_uploads(run_id)
            raise
        finally:
            for pending in claimed:
                self.store.delete_uploads(pending)

    async def _earlier(self, value: object) -> dict:
        """The finished run a follow-up continues: {"id", "goal", "answer", "steps"} with its last steps."""
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise AgentApiError("follows must be the id of the run to follow up.")
        try:
            data = self.run(value) if value in self.runs else await asyncio.to_thread(self.run, value)
        except AgentApiError as exc:
            if exc.status == 404:
                raise AgentApiError("There's no earlier run with that id to follow up.") from None
            raise
        if data["status"] not in _FINISHED:
            raise AgentApiError("The run you're following up is still working. Wait for it to finish, or stop it, "
                                "then send again.", 409)
        return {"id": value, "goal": data["goal"], "answer": data["answer"],
                "steps": copy.deepcopy(data["steps"][-FOLLOW_STEPS:])}

    @staticmethod
    def models(settings: Settings) -> dict:
        """Every configured model, ready when its key is set and Private permits it."""
        return {"models": [{"name": cfg.name, "label": cfg.label,
                            "ready": is_ready(cfg) and settings.yours(cfg.name)}
                           for cfg in settings.agent_configs.values()]}

    async def start(self, settings: Settings, body: dict) -> dict:
        """Start one run; the page gets its id at once and polls for its steps.

        Besides model, goal, workspace and max_steps: computer (with confirmed, once the person has confirmed
        the warning), fast (at most FAST_MAX_STEPS steps at the model's lowest effort), effort (one of Ixel's
        levels, sent as the nearest the model takes; it wins over fast's), follows (a finished run to continue)
        and uploads (pending ids from save_upload)."""
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

        computer = body.get("computer", False)
        if not isinstance(computer, bool):
            raise AgentApiError("computer must be true or false.")
        if computer and body.get("confirmed") is not True:
            raise AgentApiError(COMPUTER_WARNING)
        fast = body.get("fast", False)
        if not isinstance(fast, bool):
            raise AgentApiError("fast must be true or false.")
        choices = [level for level in effort_levels.LABELS if level != "none"]
        wanted = body.get("effort", "")
        if not isinstance(wanted, str) or (wanted.strip() and wanted.strip().lower() not in choices):
            raise AgentApiError(f"effort must be one of {', '.join(choices)}, or empty for the model's own.")
        wanted = wanted.strip().lower()
        levels = effort_levels.agent_levels(cfg)
        if fast:
            max_steps = min(max_steps, FAST_MAX_STEPS)
            if not wanted and levels:
                wanted = levels[0]  # the lowest the model takes; an effort the person picked wins
        sent = effort_levels.to_send(levels, wanted) or ""
        uploads = body.get("uploads", [])
        if uploads is None:
            uploads = []
        if (not isinstance(uploads, list) or len(uploads) > MAX_UPLOADS_PER_RUN
                or not all(isinstance(item, str) and _ID.fullmatch(item) for item in uploads)
                or len(set(uploads)) != len(uploads)):
            raise AgentApiError(f"uploads must be a list of at most {MAX_UPLOADS_PER_RUN} different ids "
                                "of attached files.")

        follows = body.get("follows")
        context = await self._earlier(follows) if follows not in (None, "") else None

        # Taken out of the pending list at once, so two starts can't both claim a file
        with self._pending_lock:
            if any(item not in self.pending_uploads for item in uploads):
                raise AgentApiError("A file you attached isn't there any more (Ixel restarted, or it was sent "
                                    "already). Attach it again.")
            claimed = {item: self.pending_uploads.pop(item) for item in uploads}
        run_id = secrets.token_hex(16)
        if claimed:
            await asyncio.to_thread(self._claim, claimed, run_id)
        try:
            run = AgentRun(cfg, workspace, goal.strip(), self.store, max_steps=max_steps, ask=self.ask,
                           run_id=run_id, computer=computer, effort=sent, follows=context)
        except (ValueError, OSError) as exc:
            await asyncio.to_thread(self.store.delete_uploads, run_id)  # a run that never started keeps none
            if isinstance(exc, OSError):
                raise
            raise AgentApiError(str(exc)) from None
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
        """Newest first, with the model and workspace the page uses to pre-fill its form, whether Computer
        access was on, and the run each one follows up (History shows a conversation once, by its last run)."""
        found = dict(saved) if saved is not None else self.saved_runs()
        found.update({run_id: run.to_dict() for run_id, run in self.runs.items()})
        rows = []
        for data in found.values():
            started = data.get("started", 0)
            if isinstance(started, bool) or not isinstance(started, (int, float)):
                started = 0
            follows = data.get("follows")
            rows.append({"id": data["id"], "goal": str(data.get("goal") or ""),
                         "status": str(data.get("status") or ""), "started": started,
                         "model": str(data.get("model") or ""), "workspace": str(data.get("workspace") or ""),
                         "computer": data.get("computer") is True,
                         "follows": follows if isinstance(follows, str) and _ID.fullmatch(follows) else None})
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
        """Model calls and commands still running stop when this Ixel server stops, and files attached for a
        run that never started are removed."""
        await asyncio.gather(*(run.stop() for run in self.runs.values()
                               if run.to_dict()["status"] in ("running", "waiting")))
        with self._pending_lock:
            pending = list(self.pending_uploads)
            self.pending_uploads.clear()
        for item in pending:
            await asyncio.to_thread(self.store.delete_uploads, item)
