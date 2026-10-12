"""
One agent run: ask for an action, do it, and show its result on the next turn.

Reads happen at once, and a reply may hold up to 5 of them (list_dir, read_file, search, load_skill): they run
together and each is recorded as its own step, with the same 'batch' (the number of the first). Writes and
commands wait for the person, unless a command is on this workspace's allowlist. The run stays in memory
while it works, and saves its full steps and approvals once after each change, written in a thread.
With Computer access on for this run, every command asks, and screenshots and opening apps, files or links
ask too. Paths can reach the home folder and commands run without the sandbox.
The page gets the same run with long results cut to 4 KB; the model and the saved session keep more.

The run keeps itself out of ruts. One bad reply is sent back with its error (cut to 2 KB), and the count
starts again after a good one. From 3 steps left the prompt says to finish. The same reply 3 times in a row, or
two replies alternating, isn't run: the model is told once, and the run ends with an error if it goes on.
Each step records 'ms' (the model call and running the action, not the wait for an approval) and 'tokens',
and the run's usage totals them with the number of calls and the cost.

Pictures from view_file travel with the next model call only, and only to a model that sees pictures; the
step's result tells any other model that it can't look at them. The files the person attached to the run are
listed in its prompt. A follow-up's prompt also holds the earlier run's goal, answer and last steps.

Adding a tool from a GitHub link (ixel_mat/agent/cards.py does the work): fetch_repo downloads a repo into
the agent's scratch folder after approval, tree_hash works out the hash a card's [source] needs and only
what this run fetched and hashed can go in a card, and write_addon_card shows the whole checked card and
writes ~/.ixel/addons/<id>/addon.toml after approval. A command with cwd scratch:NAME always asks. The run
never turns a card on: the person does that in Settings › Add-ons.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from copy import deepcopy
from pathlib import Path

from ixel_mat.agent import addons, cards
from ixel_mat.agent.protocol import Action, NEEDS_APPROVAL, ProtocolError, Reads, build_prompt, parse_reply
from ixel_mat.agent.sandbox import can_sandbox, run_command
from ixel_mat.agent.store import AgentStore
from ixel_mat.agent.tools import (MAX_PICTURE_BYTES_PER_CALL, MAX_PICTURES_PER_CALL, PendingWrite, Workspace,
                                  Viewed, WorkspaceError)
from ixel_mat.agents import create_agent
from ixel_mat.agents.base import AgentConfig
from ixel_mat.ask import AskError, is_ready
from ixel_mat.limits import USAGE_LIMIT, UsageLimit
from ixel_mat.material import mask_secrets
from ixel_mat.pictures import megabytes
from ixel_mat.runtime import CONNECT_TIMEOUT
from ixel_mat.usage import Usage, call_usage

# A stand-in for the model gets the prompt, pictures= as well on a turn that has pictures to send, and
# effort= on every call of a run that has one
Ask = Callable[..., Awaitable[tuple[str, dict | None]]]
MAX_PAGE_RESULT_BYTES = 4 * 1024
MAX_BAD_REPLY_BYTES = 2 * 1024  # how much of a bad reply is sent back to the model with its error
REPEATS = 3                     # the same reply this many times in a row is a loop
_TERMINAL = ("done", "stopped", "failed")
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")


def clip_result(text: str) -> str:
    """A tool result for the page, at most 4 KB of UTF-8 without splitting a character."""
    return text.encode("utf-8")[:MAX_PAGE_RESULT_BYTES].decode("utf-8", errors="ignore")


def clip_bad_reply(text: str) -> str:
    """A bad reply as it's sent back to the model: at most 2 KB of UTF-8 without splitting a character, with
    a note saying so when it was cut."""
    raw = text.encode("utf-8")
    if len(raw) <= MAX_BAD_REPLY_BYTES:
        return text
    shown = raw[:MAX_BAD_REPLY_BYTES].decode("utf-8", errors="ignore")
    return f"{shown}\n[… cut: the reply was {len(raw):,} bytes and only the first {MAX_BAD_REPLY_BYTES:,} are shown]"


def _count(value: object) -> int:
    """A token count from a usage dict: a whole number of at least 0, or 0 for anything else."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _tokens(usage: dict | None) -> dict:
    """{"input", "output"} for one model call's usage dict: input includes cached reads and writes. Zeros when
    the call reported nothing."""
    usage = usage if isinstance(usage, dict) else {}
    return {"input": sum(_count(usage.get(key)) for key in ("input_tokens", "cache_read_tokens",
                                                            "cache_write_tokens")),
            "output": _count(usage.get("output_tokens"))}


def _ms(seconds: float) -> int:
    """A time taken in whole milliseconds, never below 0."""
    return max(0, round(seconds * 1000))


def _signature(action: Action) -> tuple[str, str]:
    """What makes two actions the same one: its name and its arguments, in a fixed order."""
    return action.action, json.dumps(action.args, sort_keys=True, ensure_ascii=False)


def _label(signature: tuple) -> str:
    """A reply's name in a message to the model: the action, or the actions of a batch of reads."""
    if signature and isinstance(signature[0], tuple):
        return "reads of " + ", ".join(dict.fromkeys(name for name, _ in signature))
    return signature[0]


def _repeating(signatures: list[tuple]) -> str:
    """How the newest replies repeat, in words that follow 'has' ('' when they don't): the same reply
    REPEATS times in a row, or two different replies going back and forth (A, B, A, B)."""
    if len(signatures) >= REPEATS and len(set(signatures[-REPEATS:])) == 1:
        return f"asked for the same thing ({_label(signatures[-1])}) {REPEATS} times in a row"
    if len(signatures) >= 4:
        first, second, third, fourth = signatures[-4:]
        if first == third and second == fourth and first != second:
            one, two = _label(first), _label(second)
            names = f"{one} and {two}" if one != two else f"{one}, with two sets of arguments"
            return f"gone back and forth between two actions ({names})"
    return ""


def _in_workspace(cwd: object) -> bool:
    """Whether run_command's cwd means the workspace: none given, or '.'. Anything else has to be a fetched
    copy, scratch:NAME."""
    return cwd is None or cwd == "."


def _fetch_note(args: dict) -> str:
    """What the approval card says a fetch_repo will do. ValueError for a link or commit that isn't allowed,
    so the step fails without asking."""
    owner, repo = cards.parse_repo_url(args["url"])
    ref = cards.check_ref(args.get("commit"))
    limit = cards.MAX_REPO_BYTES
    size = f"{limit // (1024 * 1024)} MB" if limit >= 1024 * 1024 else f"{limit} bytes"
    return (f"Download {cards.CODELOAD}/{owner}/{repo}/tar.gz/{ref} into the agent's scratch folder as "
            f"scratch:{cards.scratch_name(owner, repo)} (regular files only, at most {size} and "
            f"{cards.MAX_REPO_FILES:,} files). Nothing is run.")


async def _ask_model(cfg: AgentConfig, prompt: str, pictures: tuple = (),
                     effort: str = "") -> tuple[str, dict | None]:
    """Ask a model with the agent's whole prompt, using the same lifecycle and usage as ixel ask. pictures go
    along only to a model that sees them (the runner says so in the step's result for any other); effort, when
    given, is the thinking level for the call, one the model takes."""
    if not is_ready(cfg):
        raise AskError(f"{cfg.label} has no API key yet. Run: ixel setup")
    agent = create_agent(cfg)
    reported: list[Usage] = []
    reply = None
    try:
        try:
            await asyncio.wait_for(agent.connect(), timeout=CONNECT_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise AskError(f"{cfg.label} didn't connect within {int(CONNECT_TIMEOUT)} seconds.") from exc
        except AskError:
            raise
        except Exception as exc:  # noqa: BLE001 — the transport's words, with its key removed
            raise AskError(f"Couldn't connect to {cfg.label}: "
                           f"{mask_secrets(str(exc), [cfg.token]) or type(exc).__name__}") from exc
        options: dict = {}
        if pictures and cfg.sees_pictures:
            options["pictures"] = list(pictures)
        if effort:
            options["effort"] = effort
        try:
            reply = await asyncio.wait_for(agent.send_and_receive(prompt, use_full_session=True,
                                                                  on_usage=reported.append, **options),
                                           timeout=cfg.call_timeout)
        except asyncio.TimeoutError as exc:
            raise AskError(f"{cfg.label} didn't answer within {int(cfg.call_timeout)} seconds.") from exc
        except Exception as exc:  # noqa: BLE001
            said = mask_secrets(str(exc), [cfg.token]) or type(exc).__name__
            if isinstance(exc, UsageLimit):
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
    record = call_usage(agent, "agent", "panel", prompt, answer, reported)
    return answer, record.to_dict() if record is not None else None


class AgentRun:
    """Work on one goal in one workspace. ask can be an async stand-in for the model in tests.

    run_id lets the app name the run before it starts, so the files the person attaches can be kept under
    that id (uploads/<run id>/); without it the run gets a fresh one. effort is the thinking level sent with
    every model call ("" sends none). follows is the earlier run a follow-up continues, {"id", "goal",
    "answer", "steps"}: its prompt starts with them, and the run records the earlier one's id.

    addons_dir is where write_addon_card puts cards (None: ~/.ixel/addons) and builtin_dir the folder of
    Ixel's own add-ons whose ids a card can't take (None: ixel_mat/addons, if it's there); tests give both."""

    def __init__(self, cfg: AgentConfig, workspace: Path | str | Workspace, goal: str, store: AgentStore,
                 max_steps: int = 25, ask: Ask | None = None, run_id: str | None = None, computer: bool = False,
                 effort: str = "", follows: dict | None = None, addons_dir: Path | str | None = None,
                 builtin_dir: Path | str | None = None):
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1:
            raise ValueError("max_steps must be a positive whole number")
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("Say what the agent should do.")
        if run_id is not None and (not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id)):
            raise ValueError("a run id is letters, digits, - and _ only")
        if not isinstance(computer, bool):
            raise ValueError("computer must be true or false")
        if not isinstance(effort, str):
            raise ValueError("effort must be text")
        if follows is not None and (not isinstance(follows, dict) or not isinstance(follows.get("id"), str)
                                    or not _RUN_ID.fullmatch(follows["id"])):
            raise ValueError("follows must be the earlier run, with its id")
        for folder in (addons_dir, builtin_dir):
            if folder is not None and not isinstance(folder, (str, Path)):
                raise ValueError("addons_dir and builtin_dir must be folders")
        self.cfg = cfg
        self.computer = computer
        self.effort = effort
        self.follows = deepcopy(follows)
        self.addons_dir = Path(addons_dir) if addons_dir is not None else None
        self.builtin_dir = Path(builtin_dir) if builtin_dir is not None else None
        if isinstance(workspace, Workspace):
            self.workspace = (workspace if workspace.computer == computer else
                              Workspace(workspace.root, uploads=workspace.uploads, computer=computer,
                                        scratch=workspace.scratch))
        else:
            self.workspace = Workspace(Path(workspace), computer=computer)
        self.goal = goal
        self.store = store
        self.max_steps = max_steps
        self.id = run_id if run_id is not None else secrets.token_hex(16)
        if self.workspace.uploads is None:
            self.workspace.uploads = store.uploads_dir(self.id)
        if self.workspace.scratch is None:
            self.workspace.scratch = store.scratch_dir()  # not made here: fetch_repo makes it, private
        self.started = time.time()
        self.status = "running"
        self.answer = ""
        self.error = ""
        self.steps: list[dict] = []
        self.pending: dict | None = None
        self.usage: dict = {"calls": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0, "ms": 0}
        self.task: asyncio.Task | None = None
        self._ask = ask or self._default_ask
        self._fence = f"IXEL-{secrets.token_hex(6)}"  # one marker for the whole run, so the prompt's front is stable
        self._bad_replies = 0
        self._retry: dict | None = None
        self._signatures: list[tuple] = []  # what each good reply asked for, to spot a loop
        self._repeat_warned = False
        self._save_lock = asyncio.Lock()    # saves are written one at a time, in the order they were asked for
        self._approvals: list[dict] = []
        self._calls: list[dict | None] = []
        self._decision: bool | None = None
        self._decision_event = asyncio.Event()
        self._command_cancel = threading.Event()
        self._pictures: list = []  # from view_file: they go with the next model call, and then they're gone
        self._saw = 0              # how many pictures the last view_file kept, for its step
        # What this run fetched and hashed, by scratch name, in the shape cards.check_source takes:
        # {"repo_url", "commit", "hashes": {subdir: (tree_sha256, files)}}. A card's [source] must match it.
        self._fetched: dict[str, dict] = {}
        self._addon_servers = addons.AddonServers(self.workspace.root)
        self._allowed_addons: set[str] = set()
        self._save()

    async def _default_ask(self, prompt: str, pictures: tuple | list = (),
                           effort: str = "") -> tuple[str, dict | None]:
        return await _ask_model(self.cfg, prompt, tuple(pictures), effort)

    def _uploads(self) -> list[dict]:
        """The run's attached files as the page shows them: {id, name, kind}."""
        try:
            return [{"id": item["id"], "name": item["name"], "kind": item["kind"]}
                    for item in self.store.list_uploads(self.id)]
        except OSError:
            return []

    def _snapshot(self, page: bool, status: str | None = None) -> dict:
        """The run as it is now; status says what to record as its status when that isn't self.status yet (a
        run that is about to wait is saved as waiting before it says so)."""
        status = status or self.status
        steps = deepcopy(self.steps)
        if page:
            for step in steps:
                step["result"] = clip_result(step["result"])
        return {"id": self.id, "status": status, "goal": self.goal, "model": self.cfg.name,
                "workspace": str(self.workspace.root), "started": self.started, "answer": self.answer,
                "error": self.error, "steps": steps,
                "pending": deepcopy(self.pending) if status == "waiting" else None,
                "usage": dict(self.usage), "uploads": self._uploads(), "computer": self.computer,
                "follows": self.follows["id"] if self.follows else None}

    def to_dict(self) -> dict:
        """The run for the app, with the shape in AGENT_BETA.md and results clipped to 4 KB."""
        return self._snapshot(page=True)

    def _session(self, status: str | None = None) -> dict:
        """What is saved: the run, its approvals and the model calls' usage, copied as they are now."""
        data = self._snapshot(page=False, status=status)
        data.update(approvals=deepcopy(self._approvals), calls=deepcopy(self._calls), max_steps=self.max_steps,
                    effort=self.effort, earlier=deepcopy(self.follows))
        return data

    def _save(self) -> None:
        """Save now, and wait for the disk. Only for __init__, where there may be no running loop."""
        self.store.save_session(self._session())

    async def _save_async(self, status: str | None = None) -> None:
        """Save the run once, in a thread, so the disk never holds up the loop. The copy is made here, on the
        loop; the lock makes the writes land in the order they were asked for, and a write that is already
        under way is let finish if this task is cancelled (a thread can't be, and an older copy must not
        land after a newer one)."""
        data = self._session(status)
        async with self._save_lock:
            write = asyncio.ensure_future(asyncio.to_thread(self.store.save_session, data))
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                await asyncio.gather(write, return_exceptions=True)
                raise

    def start(self) -> asyncio.Task:
        """Start the loop in the background, or return the task already working on this run."""
        if self.task is None:
            self.task = asyncio.create_task(self.run())
        return self.task

    def _record_usage(self, usage: dict | None) -> dict:
        self._calls.append(deepcopy(usage))
        if usage:
            cost = usage.get("cost_usd", usage.get("cost"))
            if (isinstance(cost, (float, int)) and not isinstance(cost, bool)
                    and math.isfinite(cost) and cost >= 0):
                self.usage["cost"] += cost
        return deepcopy(usage) if usage is not None else {}

    async def run(self) -> dict:
        """Keep asking until finished, stopped or failed, waiting for decide when approval is needed."""
        if self.status in _TERMINAL:
            return self.to_dict()
        current = asyncio.current_task()
        if self.task is not None and self.task is not current:
            return await asyncio.shield(self.task)
        self.task = current
        try:
            await self._addon_servers.start()
            while self.status not in _TERMINAL:
                if len(self.steps) >= self.max_steps:
                    self.status = "failed"
                    self.error = f"Reached the step limit ({self.max_steps}). Start another run to continue."
                    await self._save_async()
                    break
                history = self.steps + ([self._retry] if self._retry is not None else [])
                try:
                    attached = self.store.list_uploads(self.id)
                except OSError:
                    attached = []
                prompt = build_prompt(self.goal, self.store.prompt_files(), self.store.skills(), history,
                                      fence=self._fence, uploads=attached, computer=self.computer,
                                      follows=self.follows, addon_tools=self._addon_servers.tools,
                                      steps_left=self.max_steps - len(self.steps))
                self.usage["calls"] += 1
                sent = list(self._pictures)
                # Only what this call has: a stand-in that takes the prompt alone still works without either
                options: dict = {}
                if sent:
                    options["pictures"] = sent
                if self.effort:
                    options["effort"] = self.effort
                asked = time.monotonic()
                try:
                    reply, usage = await self._ask(prompt, **options)
                finally:
                    self._pictures = []  # they go with one call only, whether or not it worked
                if self.status == "stopped":
                    break
                model_ms = _ms(time.monotonic() - asked)
                step_usage = self._record_usage(usage)
                tokens = _tokens(usage)
                self.usage["input_tokens"] += tokens["input"]
                self.usage["output_tokens"] += tokens["output"]
                self.usage["ms"] += model_ms
                try:
                    parsed = parse_reply(reply, computer=self.computer)
                except ProtocolError as exc:
                    self._bad_replies += 1
                    if self._bad_replies > 1:
                        self.status = "failed"
                        self.error = mask_secrets(f"The model sent a second bad reply: {exc}", [self.cfg.token])
                        await self._save_async()
                        break
                    shown = clip_bad_reply(reply if isinstance(reply, str) else str(reply))
                    self._retry = {"n": len(self.steps) + 1, "action": "invalid_reply", "args": {},
                                   "thought": "", "ok": False,
                                   "result": f"Parse error: {exc}\nSend one corrected JSON action.\n"
                                             f"Your bad reply was:\n{shown}"}
                    self._pictures = sent  # the corrected reply is the next call, and it still needs to see them
                    continue
                self._bad_replies = 0  # one slip is forgiven at a time, wherever in the run it comes
                self._retry = None
                signature = (tuple(_signature(each) for each in parsed.actions) if isinstance(parsed, Reads)
                             else _signature(parsed))
                self._signatures.append(signature)
                loop = _repeating(self._signatures)
                if loop:
                    if self._repeat_warned:
                        self.status = "failed"
                        self.error = mask_secrets(f"The run was stopped because the model kept repeating "
                                                  f"itself: it has {loop}, even after being told.",
                                                  [self.cfg.token])
                        await self._save_async()
                        break
                    self._repeat_warned = True
                    self._retry = {"n": len(self.steps) + 1, "action": "repeating", "args": {},
                                   "thought": "", "ok": False,
                                   "result": f"You have {loop}, so this reply was not run. Do something "
                                             "different, or use finish with what you have."}
                    self._pictures = sent  # the next call is this turn again, and it still needs to see them
                    continue
                if isinstance(parsed, Reads):
                    await self._read_together(parsed, model_ms, tokens, step_usage)
                    continue
                action = parsed
                step = {"n": len(self.steps) + 1, "thought": action.thought, "action": action.action,
                        "args": deepcopy(action.args), "ok": True, "result": "", "usage": step_usage,
                        "ms": model_ms, "tokens": tokens}
                if action.action == "finish":
                    self.answer = action.args["answer"]
                    step["result"] = self.answer
                    self.steps.append(step)
                    self.status = "done"
                    await self._save_async()
                    break
                await self._act(action, step)
        except asyncio.CancelledError:
            self._command_cancel.set()
            self.status = "stopped"
            self.pending = None
            await self._save_async()
        except Exception as exc:  # noqa: BLE001 — leave a failed session the page can still inspect
            self.status = "failed"
            self.pending = None
            self.error = mask_secrets(str(exc), [self.cfg.token]) or type(exc).__name__
            with suppress(OSError):
                await self._save_async()
        finally:
            await self._addon_servers.close()
        return self.to_dict()

    def _add_time(self, step: dict, began: float, waited: float = 0.0) -> None:
        """Add the time an action took, since began and less the wait for the person, to its step and to the
        run's total. The step already holds the model call's time."""
        took = _ms(time.monotonic() - began - waited)
        step["ms"] += took
        self.usage["ms"] += took

    def _read_one(self, action: Action) -> tuple[bool, str, int]:
        """One read-only action, run in a thread: whether it worked, what it gave back (or why it didn't, with
        any secret masked) and how long it took in ms. A refusal is a failed read, as it is for a single
        action; anything else is raised."""
        tool = getattr(self.store if action.action == "load_skill" else self.workspace, action.action)
        began = time.monotonic()
        try:
            ok, text = True, tool(**action.args)
        except (WorkspaceError, ValueError, KeyError, OSError) as exc:
            ok, text = False, str(exc)
        return ok, mask_secrets(text, [self.cfg.token]), _ms(time.monotonic() - began)

    async def _read_together(self, reads: Reads, model_ms: int, tokens: dict, usage: dict) -> None:
        """Run a reply's reads at the same time and record each as its own step, all with the 'batch' of the
        first one's number. The model call's time, tokens and usage go on the first step; each step has its own
        read time. Only as many reads as there are steps left run, and the last step says how many didn't."""
        first = len(self.steps) + 1
        actions = reads.actions[:self.max_steps - len(self.steps)]
        outcomes = await asyncio.gather(*(asyncio.to_thread(self._read_one, action) for action in actions),
                                        return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome  # not a refusal: the run fails with it, as it would for a single read
        recorded = []
        for index, (action, (ok, result, took)) in enumerate(zip(actions, outcomes)):
            lead = index == 0
            recorded.append({"n": first + index, "thought": action.thought, "action": action.action,
                             "args": deepcopy(action.args), "ok": ok, "result": result,
                             "usage": usage if lead else {}, "ms": took + (model_ms if lead else 0),
                             "tokens": tokens if lead else {"input": 0, "output": 0}, "batch": first})
            self.usage["ms"] += took
        left = len(reads.actions) - len(actions)
        if left:
            recorded[-1]["result"] += (f"\n[{left} more read{'' if left == 1 else 's'} not run: the run has no "
                                       f"steps left for {'it' if left == 1 else 'them'}]")
        if self.status != "stopped":
            self.steps.extend(recorded)
            await self._save_async()

    async def _act(self, action: Action, step: dict) -> None:
        """Check an action, wait if needed, then record what happened for the next model turn."""
        began = time.monotonic()
        waited = 0.0  # time spent waiting for the person: not the action's time
        if action.action == "view_file" and not self.cfg.sees_pictures:
            step.update(ok=False, pictures=0,
                        result=f"{self.cfg.label} can't see pictures, so view_file shows nothing. Use read_file "
                               "for a document's text, and finish by saying the picture couldn't be looked at.")
            self.steps.append(step)
            await self._save_async()
            return
        prepared = None
        opened = None
        card = None   # a checked add-on card, waiting to be written
        note = ""     # what the approval card says when the action has no diff
        self._saw = 0
        try:
            needs_approval = action.action in NEEDS_APPROVAL
            if action.action == "write_file":
                prepared = await asyncio.to_thread(self.workspace.prepare_write, **action.args)
            elif action.action == "edit_file":
                prepared = await asyncio.to_thread(self.workspace.prepare_edit, **action.args)
            elif action.action == "run_command":
                cwd = action.args.get("cwd")
                in_copy = not _in_workspace(cwd)
                if in_copy:  # a fetched repo: it always asks, and the allowlist isn't looked at
                    _, name = self.workspace.scratch_cwd(cwd)
                    note = (f"Run in scratch:{name}, the copy of a GitHub repo fetched into Ixel's scratch "
                            "folder, not in the workspace.")
                needs_approval = in_copy or self.computer or not self.store.is_allowed(
                    self.workspace.root, action.args["argv"])
            elif action.action == "fetch_repo":
                note = _fetch_note(action.args)  # a bad link or commit fails here, without asking
            elif action.action == "use_tool":
                addon = self._addon_servers.card_for_tool(action.args["addon"], action.args["tool"])
                needs_approval = addon.id not in self._allowed_addons
                note = (f"Use {addon.name} ({addon.id}), tool {action.args['tool']}. "
                        f"Files: {addon.permissions.files}; network: {addon.permissions.network}. "
                        "Always allow covers this add-on for this run only.")
            elif action.action == "write_addon_card":
                card = await asyncio.to_thread(cards.prepare_agent_card, action.args["id"], action.args["card"],
                                               self._fetched, self.addons_dir, self.builtin_dir)
                note = card.summary  # the whole card
            elif action.action == "open":
                opened = await asyncio.to_thread(self.workspace.open_argv, **action.args)
            if needs_approval:
                self._decision = None
                self._decision_event.clear()
                self.pending = {"n": step["n"], "action": action.action, "args": deepcopy(action.args),
                                "diff": prepared.diff if prepared is not None else note,
                                "argv": list(action.args["argv"]) if action.action == "run_command" else [],
                                "sandboxed": (not self.computer and can_sandbox()
                                              if action.action == "run_command" else False)}
                if action.action == "screenshot":
                    self.pending["diff"] = "Take a picture of the screen (macOS only) and send it to the model."
                elif opened is not None:
                    self.pending.update(argv=opened, diff=f"Open {action.args['target']!r} using {opened!r}.")
                elif action.action == "use_tool":
                    self.pending["sandboxed"] = can_sandbox()
                paused = time.monotonic()
                # Saved as waiting before the run says it is, so whoever sees the status finds the card on disk
                await self._save_async(status="waiting")
                if self.status == "stopped":
                    return  # stop() came while the save was under way
                self.status = "waiting"
                await self._decision_event.wait()
                waited = time.monotonic() - paused
                if not self._decision:
                    step["ok"] = False
                    step["result"] = "The person denied this action. It was not run."
                    if action.action == "screenshot":
                        step["pictures"] = 0
                    self._add_time(step, began, waited)
                    self.steps.append(step)
                    await self._save_async()
                    return
            ok, result = await self._execute(action, prepared, opened, card)
            step.update(ok=ok, result=mask_secrets(result, [self.cfg.token]))
        except (WorkspaceError, ValueError, KeyError, OSError) as exc:
            step.update(ok=False, result=mask_secrets(str(exc), [self.cfg.token]))
        if action.action in ("view_file", "screenshot", "use_tool"):
            step["pictures"] = self._saw  # what goes with the next call: 0 when it showed nothing
        self._add_time(step, began, waited)
        if self.status != "stopped":
            self.steps.append(step)
            await self._save_async()

    async def _execute(self, action: Action, prepared: PendingWrite | None,
                       opened: list[str] | None = None, card: cards.PendingCard | None = None) -> tuple[bool, str]:
        if prepared is not None:
            return True, await asyncio.to_thread(self.workspace.apply, prepared)
        if action.action == "fetch_repo":
            return True, await self._fetch(action.args)
        if action.action == "tree_hash":
            return True, await self._tree_hash(**action.args)
        if action.action == "use_tool":
            ok, viewed = await self._addon_servers.call(**action.args)
            return ok, self._keep_pictures(viewed)
        if action.action == "write_addon_card":
            if card is None:
                raise ValueError("the card wasn't checked; propose write_addon_card again")
            return True, await asyncio.to_thread(cards.write_card, card)
        if action.action in ("run_command", "open"):
            where = ""
            if action.action == "run_command":
                folder = self.workspace.root
                if not _in_workspace(action.args.get("cwd")):
                    # Looked at again: the folder is the copy as it is now, and sandboxed on a Mac unless
                    # Computer access is on (which is also the only time it has the network)
                    folder, name = self.workspace.scratch_cwd(action.args["cwd"])
                    where = f"; ran in scratch:{name}"
                result = await asyncio.to_thread(run_command, action.args["argv"], folder,
                                                 cancel=self._command_cancel, computer=self.computer)
            else:
                result = await asyncio.to_thread(self.workspace.open, **action.args, expected_argv=opened,
                                                 cancel=self._command_cancel)
            status = "stopped" if result.cancelled else ("timed out" if result.timed_out
                                                          else f"exit code {result.returncode}")
            sandbox = "sandboxed" if result.sandboxed else "ran without a sandbox"
            return (result.returncode == 0 and not result.cancelled,
                    f"{status}; {sandbox}{where}\n{result.output}")
        if action.action in ("view_file", "screenshot"):
            if action.action == "screenshot" and not self.cfg.sees_pictures:
                return False, f"{self.cfg.label} can't see pictures, so screenshot shows nothing."
            return True, await self._look(action)
        if action.action in ("load_skill", "remember"):
            tool = getattr(self.store, action.action)
        else:
            tool = getattr(self.workspace, action.action)
        return True, await asyncio.to_thread(tool, **action.args)

    async def _fetch(self, args: dict) -> str:
        """An approved fetch_repo. It runs in this task, so Stop cancels it, and cards.fetch_repo removes its
        temp files then. What came of it is remembered for this run: write_addon_card's [source] has to match."""
        found = await cards.fetch_repo(args["url"], args.get("commit"), self.store.scratch_dir())
        self._fetched[found.name] = {"repo_url": found.repo_url, "commit": found.commit,
                                     "hashes": {"": (found.tree_sha256, found.files)}}
        return found.text()

    async def _tree_hash(self, repo: str, subdir: str = "") -> str:
        """tree_hash: the card's tree_sha256 and files for a fetched copy, or a folder in it, worked out here.

        A hash is only remembered for a repo this run fetched, and only while the copy is still the files
        that were downloaded (a command run in it may have changed them): then it'd no longer be what the
        commit holds, and a card couldn't say it was."""
        if not cards.is_scratch_name(repo):
            raise ValueError(f"{repo!r} isn't a fetched repo's name; use the OWNER-REPO name fetch_repo gave")
        folder = self.store.scratch_dir() / repo
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError(f"there's no fetched repo {repo!r}; use fetch_repo first")
        entry = self._fetched.get(repo)
        fetched_hash = entry["hashes"][""][0] if entry is not None else None

        def work() -> tuple[str, int]:
            whole = cards.tree_hash(folder, "")
            if fetched_hash is not None and whole[0] != fetched_hash:
                raise ValueError(f"scratch:{repo} isn't the files fetch_repo downloaded any more (a command "
                                 "changed it?), so its hash wouldn't match the commit; use fetch_repo again "
                                 "for a fresh copy, and tree_hash before you run anything in it")
            return whole if not subdir else cards.tree_hash(folder, subdir)

        tree, files = await asyncio.to_thread(work)
        where = f"scratch:{repo}/{subdir}" if subdir else f"scratch:{repo} (the whole repo)"
        lines = [f"tree_hash of {where}:", f'tree_sha256 = "{tree}"', f"files = {files}"]
        if entry is None:
            lines.append("This run hasn't fetched this repo, so a card can't use this hash; use fetch_repo "
                         "on it first.")
        else:
            entry["hashes"][subdir] = (tree, files)
            lines.append(f'Copy both into the card\'s [source], with repo = "{entry["repo_url"]}", commit = '
                         f'"{entry["commit"]}"' + (f', subdir = "{subdir}"' if subdir else "") + ".")
        return "\n".join(lines)

    async def _look(self, action: Action) -> str:
        """Run view_file or an approved screenshot. Its pictures wait for the next model call, at most
        MAX_PICTURES_PER_CALL of them and MAX_PICTURE_BYTES_PER_CALL in all; the rest are left out, and the
        text says how many and why."""
        tool = getattr(self.workspace, action.action)
        options = {"cancel": self._command_cancel} if action.action == "screenshot" else {}
        viewed = await asyncio.to_thread(tool, **action.args, **options)
        return self._keep_pictures(viewed)

    def _keep_pictures(self, viewed: Viewed) -> str:
        """Pictures from files or MCP tools go with one model call, within the same limits."""
        if viewed.pictures and not self.cfg.sees_pictures:
            return viewed.text + f"\n[{self.cfg.label} can't see pictures; none were sent.]"
        kept, total, why = [], 0, ""
        for picture in viewed.pictures:
            if len(kept) >= MAX_PICTURES_PER_CALL:
                why = f"a model call takes at most {MAX_PICTURES_PER_CALL} pictures"
                break
            if total + len(picture.data) > MAX_PICTURE_BYTES_PER_CALL:
                why = f"a model call takes at most {megabytes(MAX_PICTURE_BYTES_PER_CALL)} of pictures"
                break
            kept.append(picture)
            total += len(picture.data)
        self._pictures = kept
        self._saw = len(kept)
        text = viewed.text
        left = len(viewed.pictures) - len(kept)
        if left:
            text += f"\n[{left} picture{'' if left == 1 else 's'} left out: {why}]"
        return text

    async def decide(self, approve: bool, always: bool = False) -> dict:
        """Approve or deny the pending action and let the background loop continue."""
        if not isinstance(approve, bool) or not isinstance(always, bool):
            raise ValueError("approve and always must be true or false")
        if self.status != "waiting" or self.pending is None:
            raise ValueError("This run isn't waiting for an approval.")
        if always:
            if approve and self.pending["action"] == "use_tool":
                self._allowed_addons.add(self.pending["args"]["addon"])
            elif self.computer:
                raise ValueError("Computer access requires approval every time; Always allow isn't available.")
            elif not approve or self.pending["action"] != "run_command":
                raise ValueError("Always allow needs an approved command.")
            elif not _in_workspace(self.pending["args"].get("cwd")):
                raise ValueError("A command in a fetched repo asks every time; Always allow isn't available "
                                 "for it.")
            else:
                self.store.allow(self.workspace.root, self.pending["argv"])
        self._approvals.append({"n": self.pending["n"], "action": self.pending["action"],
                                "args": deepcopy(self.pending["args"]), "approve": approve,
                                "always": always, "decided": time.time()})
        self._decision = approve
        self.pending = None
        self.status = "running"
        await self._save_async()
        self._decision_event.set()
        return self.to_dict()

    async def stop(self) -> dict:
        """Stop this run, cancelling an outstanding model call or a command and clearing its approval. The
        task is cancelled in the same breath as the status changes, so it can't carry on to its clean-up
        while the stop is being saved; the stopped run is saved before this returns."""
        if self.status in _TERMINAL:
            return self.to_dict()
        self.status = "stopped"
        self.pending = None
        self._command_cancel.set()
        if self.task is not None and not self.task.done():
            self.task.cancel()
            if self.task is not asyncio.current_task():
                with suppress(asyncio.CancelledError):
                    await self.task
        await self._save_async()
        return self.to_dict()
