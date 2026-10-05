"""Subprocess agent transport with PTY support for interactive CLIs."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
from contextlib import suppress
from typing import Awaitable, Callable

from ixel_mat.agents.base import AgentConfig, BaseAgent, prepare_workdir, remove_workdir
from ixel_mat.agents.launch import find_on_path, resolve_argv
from ixel_mat.agents.oneshot import version_args
from ixel_mat.agents.process_tree import SPAWN_OPTIONS, ProcessTree, create_process_tree
from ixel_mat.config.secrets import child_env
from ixel_mat.presets import locked_env

try:
    import pty
except ImportError:  # Windows has no pty/termios; fall back to plain pipes
    pty = None

logger = logging.getLogger("ixel_mat.agents.subprocess")


def _reply_started(output: str, message: str, echoes: bool) -> bool:
    """Has the reply begun? On a PTY the prompt is echoed back first, which doesn't count."""
    seen = output.replace("\r", "").strip()
    return bool(seen) and not (echoes and message.replace("\r", "").strip().startswith(seen))


class SubprocessAgent(BaseAgent):
    """
    Interactive subprocess transport.

    Uses a PTY by default where the OS has one (not Windows) so tools like Hermes
    behave as if attached to a terminal (colors, prompt behavior, interactive output).
    """

    def __init__(
        self,
        config: AgentConfig,
        *,
        use_pty: bool | None = None,
        response_idle_timeout: float = 1.2,
        startup_timeout: float = 10.0,
        shutdown_timeout: float = 4.0,
    ):
        super().__init__(config)
        self.use_pty = (pty is not None) if use_pty is None else use_pty
        self.response_idle_timeout = response_idle_timeout
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout

        self.process: asyncio.subprocess.Process | None = None
        self._tree: ProcessTree | None = None
        self.master_fd: int | None = None
        self.slave_fd: int | None = None
        self._read_task: asyncio.Task | None = None
        self._listen_callback: Callable[[str], Awaitable[None]] | None = None
        self._output_queue: asyncio.Queue[str] = asyncio.Queue()
        self._lock: asyncio.Lock = asyncio.Lock()
        self.binary_path: str = ""
        self.last_error: str = ""
        self.last_exit_code: int | None = None
        self._workdir: str | None = None
        self._remove_workdir = False

    async def connect(self) -> None:
        if self._connected:
            return

        if not self.config.command:
            raise ValueError(f"Agent '{self.name}' missing command")
        if self.use_pty and pty is None:
            raise RuntimeError(f"Agent '{self.name}': PTY mode is not available on this OS")

        binary = find_on_path(self.config.command)  # PATH only: never the current folder
        if not binary:
            raise FileNotFoundError(
                f"Command '{self.config.command}' not found in PATH for agent '{self.name}'"
            )
        self.binary_path = binary
        self.last_error = ""
        self.last_exit_code = None

        env = self._build_env()
        # As for one-shot agents: what its version can't run without (OpenCode 2's --standalone: without it, the
        # terminal interface starts your background service with Ixel's settings, or uses yours with your own)
        cmd = [binary] + await version_args(self.config, env)
        if self.config.last_session_id and "--resume" not in cmd:
            cmd.extend(["--resume", self.config.last_session_id])
        # On Windows, as for one-shot agents: an npm .cmd runs what it points to, not cmd.exe
        cmd = resolve_argv(cmd)
        logger.info("Starting subprocess agent '%s': %s", self.name, " ".join(cmd))

        self._workdir, self._remove_workdir = prepare_workdir(self.config.workdir)
        try:
            if self.use_pty:
                self.master_fd, self.slave_fd = pty.openpty()
                self.process = await asyncio.wait_for(
                    asyncio.create_subprocess_exec(
                        *cmd,
                        stdin=self.slave_fd,
                        stdout=self.slave_fd,
                        stderr=self.slave_fd,
                        preexec_fn=os.setsid,
                        close_fds=True,
                        cwd=self._workdir,
                        env=env,
                    ),
                    timeout=self.startup_timeout,
                )
                os.close(self.slave_fd)
                self.slave_fd = None
            else:
                self.process, self._tree = await asyncio.wait_for(
                    create_process_tree(
                        *cmd,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        cwd=self._workdir,
                        env=env,
                        **SPAWN_OPTIONS,
                    ),
                    timeout=self.startup_timeout,
                )
            if self.use_pty:
                self._tree = ProcessTree(self.process)

            self._read_task = asyncio.create_task(self._read_loop())
            # Give process a short moment to fail fast before marking connected.
            await asyncio.sleep(0.2)
            if self.process and self.process.returncode is not None:
                self.last_exit_code = self.process.returncode
                startup_output = await self._collect_startup_output()
                self.last_error = (
                    f"Process exited immediately with code {self.process.returncode}"
                )
                details = f"{self.last_error}. Output: {startup_output}" if startup_output else self.last_error
                await self.disconnect()
                raise RuntimeError(details)
            self._connected = True
        except BaseException:
            if self._read_task is not None:
                self._read_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._read_task
                self._read_task = None
            if self.process is not None:
                with suppress(Exception):
                    await self._terminate_process()
                self.process = None
            if self._tree is not None:
                with suppress(Exception):
                    self._tree.close()
                self._tree = None
            await self._close_fds()
            remove_workdir(self._workdir, self._remove_workdir)
            self._workdir = None
            raise

    async def disconnect(self) -> None:
        if self._read_task:
            self._read_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._read_task
            self._read_task = None

        if self.process:
            await self._terminate_process()
            self.process = None

        await self._close_fds()
        remove_workdir(self._workdir, self._remove_workdir)
        self._workdir = None
        self._connected = False

    async def send(self, message: str) -> None:
        if not self.process or self.process.returncode is not None:
            raise RuntimeError(f"Agent '{self.name}' is not connected")

        payload = (message + "\n").encode("utf-8", errors="replace")
        if self.use_pty:
            if self.master_fd is None:
                raise RuntimeError("PTY master fd missing")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, os.write, self.master_fd, payload)
            return

        if not self.process.stdin:
            raise RuntimeError("Process stdin is unavailable")
        self.process.stdin.write(payload)
        await self.process.stdin.drain()

    async def send_and_receive(self, message: str, **kwargs) -> str:
        """
        Sends the prompt, waits for the reply to start (up to the call timeout),
        then collects output until it goes quiet for response_idle_timeout.

        The idle timer only starts once the reply has: a model that thinks for a
        while before its first word would otherwise get an empty answer here, and
        its reply would be read as the answer to the next prompt.
        kwargs accepted and ignored for transport-agnostic compatibility.
        """
        async with self._lock:
            self._drain_queue()
            await self.send(message)

            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.config.transport_timeout
            last_output = loop.time()
            chunks: list[str] = []
            while (now := loop.time()) < deadline:
                if _reply_started("".join(chunks), message, self.use_pty):
                    wait = self.response_idle_timeout - (now - last_output)
                    if wait <= 0:
                        break  # the reply has gone quiet: done
                else:
                    wait = deadline - now
                try:
                    # In slices, so a process that exits mid-wait is noticed
                    chunks.append(await asyncio.wait_for(self._output_queue.get(), timeout=min(wait, 0.5)))
                    last_output = loop.time()
                except asyncio.TimeoutError:
                    if not self._connected and self._output_queue.empty():
                        break

            text = "".join(chunks).strip()
            if not text or (self.use_pty and text.replace("\r", "") == message.replace("\r", "").strip()):
                if not self._connected:
                    raise RuntimeError(f"{self.label} exited before replying")
                raise TimeoutError(f"{self.label} didn't reply within {self.config.transport_timeout:.0f}s")
            return text

    async def listen(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """Register a stream callback and block while connected."""
        self._listen_callback = callback
        while self._connected:
            await asyncio.sleep(0.1)

    async def cancel(self) -> None:
        """Interrupt current generation (SIGINT on process group when possible)."""
        if not self.process or self.process.returncode is not None:
            return
        if self.use_pty:
            with suppress(ProcessLookupError):
                os.killpg(os.getpgid(self.process.pid), signal.SIGINT)
        else:
            self.process.send_signal(signal.SIGINT)

    async def _read_loop(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while self.process and self.process.returncode is None:
                if self.use_pty:
                    if self.master_fd is None:
                        break
                    try:
                        data = await loop.run_in_executor(None, os.read, self.master_fd, 4096)
                    except OSError as exc:
                        # EIO is expected once PTY closes.
                        if exc.errno != 5:
                            logger.warning("PTY read error (%s): %s", self.name, exc)
                        break
                else:
                    if not self.process.stdout:
                        break
                    data = await self.process.stdout.read(4096)
                    if not data:
                        break

                if not data:
                    break

                text = data.decode("utf-8", errors="replace")
                await self._output_queue.put(text)
                if self._listen_callback:
                    await self._listen_callback(text)
        finally:
            self._connected = False
            if self.process and self.process.returncode is not None:
                self.last_exit_code = self.process.returncode

    async def _terminate_process(self) -> None:
        """Ask the CLI to exit, then end it and everything it started (its process group, or job)."""
        assert self.process is not None
        if self.process.returncode is None:
            with suppress(Exception):
                if os.name == "posix":
                    with suppress(ProcessLookupError, PermissionError):
                        os.killpg(self.process.pid, signal.SIGTERM)  # its own group: setsid / new session
                else:
                    self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=self.shutdown_timeout)
        if self._tree is not None:
            await self._tree.kill()  # anything that ignored SIGTERM, or outlived the CLI
            self._tree.close()
            self._tree = None

    async def _close_fds(self) -> None:
        if self.slave_fd is not None:
            with suppress(OSError):
                os.close(self.slave_fd)
            self.slave_fd = None
        if self.master_fd is not None:
            with suppress(OSError):
                os.close(self.master_fd)
            self.master_fd = None

    def _drain_queue(self) -> None:
        while True:
            try:
                self._output_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def _collect_startup_output(self) -> str:
        parts: list[str] = []
        while True:
            try:
                item = self._output_queue.get_nowait()
                parts.append(item)
            except asyncio.QueueEmpty:
                break
        return "".join(parts).strip()

    def _build_env(self) -> dict[str, str]:
        env = child_env(self.config.pass_env, {**(self.config.env or {}), **locked_env(self.config.command)},
                        self.config.drop_env)
        env.setdefault("TERM", "xterm-256color")
        return env
