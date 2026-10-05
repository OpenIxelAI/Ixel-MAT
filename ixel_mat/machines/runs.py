"""
One command you typed, run on several machines at once ("Run on machines"): eight at a time, each with a
time limit and an output limit. Nothing is asked along the way (a key, or ssh-agent, signs in), each
machine's key must already be pinned, and every run is logged (log.py). Runs still going stop when Ixel
closes. On Linux and macOS, if Ixel is killed outright, an ssh it started can go on until its command ends
on the server.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass

from ixel_mat.agents.process_tree import SPAWN_OPTIONS, create_process_tree
from ixel_mat.machines import log
from ixel_mat.machines.ssh import Where, explain, quiet_env
from ixel_mat.machines.store import Machine

logger = logging.getLogger("ixel_mat.machines")

MAX_AT_ONCE = 8
OUTPUT_CAP = 64 * 1024          # per machine: more is counted, not kept
TIMEOUTS = (30, 120, 600, 1800)  # seconds a machine may take
MAX_COMMAND = 2000
KEEP_RUNS = 5


class RunError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Target:
    """A machine to run on, with its ssh line, or why it can't run: (code, message)."""
    machine: Machine
    argv: list[str] | None = None
    problem: tuple[str, str] | None = None
    address: str = ""
    where: Where | None = None


class Result:
    def __init__(self, target: Target):
        self.target = target
        self.machine = target.machine
        self.address = target.address
        self.state = "waiting"    # waiting | running | ok | failed | error | timeout | stopped
        self.code: int | None = None
        self.hint = ""
        self.hint_code = ""
        self.output = bytearray()
        self.total = 0
        self.started: float | None = None
        self.ended: float | None = None
        self._stderr = bytearray()

    def add(self, chunk: bytes, stderr: bool) -> None:
        self.total += len(chunk)
        room = OUTPUT_CAP - len(self.output)
        if room > 0:
            self.output += chunk[:room]
        if stderr:
            self._stderr = (self._stderr + chunk)[-8192:]

    @property
    def stderr(self) -> str:
        return self._stderr.decode("utf-8", "replace")

    def to_dict(self, full: bool) -> dict:
        text = self.output.decode("utf-8", "replace")
        lines = [line for line in text.splitlines() if line.strip()]
        seconds = (self.ended or time.monotonic()) - self.started if self.started else None
        out = {"id": self.machine.id, "name": self.machine.name, "address": self.address, "state": self.state,
               "code": self.code, "hint": self.hint, "hint_code": self.hint_code, "bytes": self.total,
               "cut": self.total > len(self.output), "preview": lines[-1][:200] if lines else "",
               "seconds": round(seconds, 1) if seconds is not None else None}
        if full:
            out["output"] = text
        return out


class Run:
    def __init__(self, command: str, timeout: int, targets: list[Target]):
        self.id = uuid.uuid4().hex[:12]
        self.command = command
        self.timeout = timeout
        self.results = [Result(t) for t in targets]
        self.created = time.time()
        self.stopping = False
        self.task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def to_dict(self, show: set[str] | None = None) -> dict:
        counts: dict[str, int] = {}
        for r in self.results:
            counts[r.state] = counts.get(r.state, 0) + 1
        return {"id": self.id, "command": self.command, "timeout": self.timeout, "running": self.running,
                "created": self.created, "counts": counts,
                "results": [r.to_dict(show is not None and r.machine.id in show) for r in self.results]}


class Runs:
    """The runs since Ixel started (the last few), one going at a time."""

    def __init__(self):
        self._runs: dict[str, Run] = {}
        self._halting: set[asyncio.Task] = set()  # ending a stopped machine's ssh, whatever else happens

    def start(self, command: str, timeout: int, targets: list[Target]) -> Run:
        if any(run.running for run in self._runs.values()):
            raise RunError("A run is still going. Stop it, or wait for it to finish.", 409)
        run = Run(command, timeout, targets)
        log.write("RUN_START", command=command, machines=", ".join(t.machine.name for t in targets),
                  timeout=timeout)
        run.task = asyncio.get_running_loop().create_task(self._all(run))
        self._runs[run.id] = run
        for old in list(self._runs)[:-KEEP_RUNS]:
            if not self._runs[old].running:
                del self._runs[old]
        return run

    def get(self, run_id: str | None) -> Run:
        run = self._runs.get(run_id or "")
        if run is None:
            raise RunError("That run is gone: Ixel keeps the last few, until it stops.", 404)
        return run

    def latest(self) -> list[Run]:
        return list(self._runs.values())[::-1]

    async def stop(self, run_id: str | None) -> Run:
        run = self.get(run_id)
        if run.running and run.task is not None:
            if not run.stopping:  # once: a second cancel could land while the first is ending ssh
                run.stopping = True
                run.task.cancel()
            await asyncio.wait({run.task})  # (never passes on a cancel of its own to the run)
        if self._halting:
            await asyncio.wait(set(self._halting))
        return run

    async def close(self) -> None:
        """Stop every run still going (Ixel is closing)."""
        for run in list(self._runs.values()):
            if run.running:
                await self.stop(run.id)

    async def _all(self, run: Run) -> None:
        gate = asyncio.Semaphore(MAX_AT_ONCE)
        await asyncio.gather(*(self._one(run, r, r.target, gate) for r in run.results), return_exceptions=True)

    async def _one(self, run: Run, result: Result, target: Target, gate: asyncio.Semaphore) -> None:
        if target.problem or not target.argv:
            result.state = "error"
            result.hint_code, result.hint = target.problem or ("ssh", "It can't run.")
            log.write("RUN", machine=result.machine.name, result="error", reason=result.hint_code)
            return
        tree = readers = None
        try:
            async with gate:
                result.state, result.started = "running", time.monotonic()
                try:
                    proc, tree = await create_process_tree(
                        *target.argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, env=quiet_env(), **SPAWN_OPTIONS)
                except (OSError, RuntimeError) as exc:
                    result.state, result.hint_code, result.hint = "error", "no_ssh", f"Couldn't start ssh: {exc}"
                    return
                readers = asyncio.gather(self._read(proc.stdout, result, False),
                                         self._read(proc.stderr, result, True))
                try:
                    await asyncio.wait_for(proc.wait(), run.timeout)
                except asyncio.TimeoutError:
                    result.state = "timeout"
                    result.hint = (f"It was still going after {_duration(run.timeout)}, so Ixel closed the "
                                   "connection (which ends most commands on the server).")
                    await tree.kill()
                try:
                    await asyncio.wait_for(asyncio.shield(readers), 5)
                except asyncio.TimeoutError:  # something it started still holds the output open
                    pass
                if result.state == "running":
                    self._finish(result, proc.returncode)
        except asyncio.CancelledError:
            result.state = "stopped"
            # In a task of its own, so nothing can cut it short (a second cancel only stops the waiting)
            halt = asyncio.ensure_future(self._halt(readers, tree))
            self._halting.add(halt)
            halt.add_done_callback(self._halting.discard)
            await asyncio.shield(halt)
            raise
        finally:
            if readers is not None and not readers.done():
                readers.cancel()
            if readers is not None:
                await asyncio.gather(readers, return_exceptions=True)
            if tree is not None:
                tree.close()
            if result.started is not None:
                result.ended = time.monotonic()
                log.write("RUN", machine=result.machine.name, address=result.address, command=run.command,
                          result=result.state, code="" if result.code is None else result.code,
                          reason=result.hint_code, seconds=round(result.ended - result.started, 1))

    @staticmethod
    async def _halt(readers, tree) -> None:
        """Stop reading first (kill() reads what's left, so the pipes close), then end ssh and what it started."""
        if readers is not None:
            readers.cancel()
            await asyncio.gather(readers, return_exceptions=True)
        if tree is not None:
            await tree.kill()

    @staticmethod
    async def _read(stream, result: Result, stderr: bool) -> None:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            result.add(chunk, stderr)

    @staticmethod
    def _finish(result: Result, code: int | None) -> None:
        result.code = code
        if code == 0:
            result.state = "ok"
            return
        if code == 255:
            hint_code, hint = explain(result.stderr, result.machine, result.target.where)
            if hint_code != "ssh":  # ssh's own failure, not the command's
                result.state, result.hint_code, result.hint = "error", hint_code, hint
                return
        result.state = "failed"
        result.hint = f"It ended with code {code}."


def _duration(seconds: int) -> str:
    return f"{seconds // 60} minutes" if seconds >= 120 else f"{seconds} seconds"


def check_command(command: object) -> str:
    if not isinstance(command, str) or not command.strip():
        raise RunError("Type the command to run.")
    command = command.strip()
    if len(command) > MAX_COMMAND:
        raise RunError(f"The command is longer than {MAX_COMMAND:,} characters.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in command):
        raise RunError("The command is one line, with no control characters.")
    return command
