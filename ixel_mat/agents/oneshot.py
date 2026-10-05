"""One-shot subprocess agent — runs a fresh command per message."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from typing import Awaitable, Callable

from ixel_mat.agents.base import AgentConfig, BaseAgent, in_folder, prepare_workdir, remove_workdir
from ixel_mat.agents import launch, leftovers
from ixel_mat.agents.launch import LaunchError, find_on_path, resolve_argv
from ixel_mat.agents.process_tree import SPAWN_OPTIONS, create_process_tree
from ixel_mat.config.secrets import child_env, ixels_own
from ixel_mat.effort import agent_levels, to_send
from ixel_mat.limits import UsageLimit, out_of_usage
from ixel_mat.models import valid_model_id
from ixel_mat.presets import gemini_key_env, locked_env, plain_error, preset_for
from ixel_mat.sanitize import ESCAPE_SEQUENCE_RE
from ixel_mat.usage import OnUsage, claude_code_usage

logger = logging.getLogger("ixel_mat.agents.oneshot")

# Strip ANSI escape sequences
# CLI metadata lines, matched as whole lines only (see _split_footer)
SESSION_LINE_RE = re.compile(r"(?:session[ _]?id|session)\s*[:=]\s*([A-Za-z0-9_\-]+)", re.IGNORECASE)
STATS_LINE_RE = re.compile(r"(?:Duration|Messages):.*")


def _split_footer(text: str) -> tuple[str, str | None]:
    """
    Separate the answer from a trailing block of CLI metadata lines.

    Only whole lines at the very end count as metadata, so nothing inside the
    answer is dropped and an answer that mentions "session = x" can't change
    which session gets resumed.
    """
    lines = text.split("\n")
    session_id = None
    while lines:
        line = lines[-1].strip()
        if line:
            match = SESSION_LINE_RE.fullmatch(line)
            if match:
                session_id = session_id or match.group(1)
            elif not STATS_LINE_RE.fullmatch(line):
                break
        lines.pop()
    return "\n".join(lines).strip(), session_id


def _session_id_in_log(text: str) -> str | None:
    """Last whole-line session marker in stderr (never shown as the answer)."""
    for line in reversed(text.split("\n")):
        match = SESSION_LINE_RE.fullmatch(line.strip())
        if match:
            return match.group(1)
    return None


def _visible_text(raw: bytes) -> str:
    """Decode output and apply carriage returns the way a terminal would."""
    text = ESCAPE_SEQUENCE_RE.sub("", raw.decode("utf-8", errors="replace")).replace("\r\n", "\n")
    return "\n".join(
        next((seg for seg in reversed(line.split("\r")) if seg.strip()), "")
        for line in text.split("\n")
    )


# Claude Code's stream-json puts a whole answer on one line in its final event
_LINE_LIMIT = 32 * 1024 * 1024
# The longest prompt passed as a command-line argument: Windows caps a whole command line
# at 32,767 characters, and Linux caps one argument at 128 KiB
ARG_LIMIT = 30_000 if os.name == "nt" else 120_000


def cli_env(config: AgentConfig) -> dict[str, str]:
    """The environment a command-line agent runs with: child_env's, and for Gemini CLI without a sign-in
    of its own, your Gemini API key (gemini_key_env): GEMINI_API_KEY, or the Google (Gemini) key saved in
    Settings. A GOOGLE_API_KEY from your shell isn't used: under that name it's often a billed Cloud key.
    What Ixel always sets for the program (locked_env: OpenCode's lockdown, no catalog fetch) goes on top."""
    env = child_env(config.pass_env, {**(config.env or {}), **locked_env(config.command)}, config.drop_env)
    if preset_for(config.command).get("id") == "gemini_cli":
        keys = {"GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY", ""), "GOOGLE_API_KEY": ixels_own("GOOGLE_API_KEY")}
        env.update(gemini_key_env(env, keys))
    return env


def arg_size(text: str) -> int:
    """How much of the limit a prompt takes as an argument: on Windows, quoted as Python passes it
    (every " gains a backslash) and counted in UTF-16 units; elsewhere, in bytes."""
    if launch.WINDOWS:
        return len(subprocess.list2cmdline([text]).encode("utf-16-le")) // 2
    return len(text.encode("utf-8"))


# The major version each program says it is, by its file: an update replaces the file, so it's asked again
_MAJOR_VERSIONS: dict[tuple[str, int, int], str] = {}
_VERSION_RE = re.compile(r"(\d+)\.\d+")


async def major_version(command: str, env: dict[str, str] | None = None) -> str:
    """The major version `command --version` reports ("2" for "opencode v2.0.22"), or "" if it doesn't say."""
    path = find_on_path(command)
    if not path:
        return ""
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    key = (path, stat.st_mtime_ns, stat.st_size)
    if key not in _MAJOR_VERSIONS:
        from ixel_mat.health import run_program  # health imports the agents package
        code, out = await run_program([path, "--version"], env=env)
        if code != 0:  # didn't start, didn't finish or failed: ask again next time
            return ""
        match = _VERSION_RE.search(out)
        _MAJOR_VERSIONS[key] = match.group(1) if match else ""
    return _MAJOR_VERSIONS[key]


def forget_major_version(command: str) -> None:
    """Ask again next time: a run with the arguments picked for it failed, and a wrapper (a version manager's
    shim, a script) stays the same file when the program behind it is updated."""
    path = find_on_path(command)
    for key in [key for key in _MAJOR_VERSIONS if key[0] == path]:
        del _MAJOR_VERSIONS[key]


async def version_args(config: AgentConfig, env: dict[str, str]) -> list[str]:
    """A command-line agent's arguments (one-shot or terminal), for the version installed (a preset may need others
    for an older one). Arguments a version can't run without (its lockdown) must be there, your own args or not, and whether or not
    the agent names the preset: OpenCode 2 without --standalone answers through your background service."""
    by_version = config.args_by_version or {}
    required = config.required_args or preset_for(config.command).get("required_args") or {}
    args = list(config.args or [])
    if by_version or required:
        version = await major_version(config.command, env)
        if version in by_version:
            args = list(by_version[version])
        missing = [a for a in required.get(version, required.get("*", [])) if a not in args]
        name = config.label or config.command
        # Without the preset there are no args of its own to fall back on
        instead = "take args out of its config" if config.required_args else \
            f'use preset = "{preset_for(config.command).get("id")}" in its config'
        if missing and not version:
            raise RuntimeError(f"'{config.name}': Ixel couldn't tell which version of {name} this is, and its "
                               f"args don't have what the newest needs ({' '.join(missing)}). "
                               f"{instead[0].upper()}{instead[1:]}, or try again.")
        if missing:
            raise RuntimeError(f"'{config.name}': {name} {version} needs {' '.join(missing)} in its args, or "
                               f"Ixel's locked-down agent isn't used. Add it, or {instead}.")
    return args


async def _read_claude_stream(proc: asyncio.subprocess.Process, stdin_data: bytes | None,
                              on_text: Callable[[str], Awaitable[None]] | None,
                              on_usage: OnUsage | None = None) -> tuple[bytes, bytes]:
    """
    Claude Code's `--output-format stream-json --include-partial-messages`: each line is an
    event. Text pieces go to on_text as they arrive; the answer is the final "result" event
    (or, without one, the pieces put together), which also carries the tokens and cost that go
    to on_usage. Returns (answer, stderr) like communicate().
    """
    async def feed() -> None:
        if proc.stdin is None:
            return
        try:
            if stdin_data:
                proc.stdin.write(stdin_data)
                await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

    stderr_task = asyncio.ensure_future(proc.stderr.read())
    feed_task = asyncio.ensure_future(feed())
    pieces: list[str] = []
    result: str | None = None
    errors: list = []
    failed = False
    try:
        async for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "stream_event":
                inner = event.get("event") if isinstance(event.get("event"), dict) else {}
                delta = inner.get("delta") if isinstance(inner.get("delta"), dict) else {}
                text = delta.get("text")
                if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta" \
                        and isinstance(text, str) and text:
                    pieces.append(text)
                    if on_text is not None:
                        await on_text(text)
            elif event.get("type") == "result":
                result = event.get("result") if isinstance(event.get("result"), str) else None
                errors = event.get("errors") if isinstance(event.get("errors"), list) else []
                failed = bool(event.get("is_error"))
                usage = claude_code_usage(event)
                if usage is not None and on_usage is not None:
                    on_usage(usage)
        await proc.wait()
        stderr = await stderr_task
    finally:
        for task in (feed_task, stderr_task):
            if not task.done():
                task.cancel()
    answer = result if result is not None else "".join(pieces)
    if failed:
        from ixel_mat.material import mask_secrets  # material imports the agents package
        # Out of usage only by Claude Code's own words, never by the pieces of an answer it was writing
        own = result if result is not None else " ".join(str(e) for e in errors)
        said = mask_secrets(answer.strip())[:300] or 'no details'
        raise (UsageLimit if out_of_usage(own) else RuntimeError)(f"Claude Code reported an error: {said}")
    return answer.encode("utf-8"), stderr


ERROR_LINES = 15  # a failed CLI says why at the end


def _own_error(stderr: str, *prompts: str | bytes | None) -> str:
    """What a failed CLI said itself: the end of its stderr, from its last ERROR line if it has one (Codex
    writes the model's reasoning there first), and without the prompt it may repeat there (Codex does), since
    the question or attached code can mention limits. Its stdout is the model's answer."""
    text = stderr
    for prompt in prompts:
        if isinstance(prompt, str):
            prompt = prompt.encode("utf-8")
        echoed = _visible_text(prompt).strip() if prompt else ""  # as stderr was read
        if echoed:
            text = text.replace(echoed, "")
    lines = text.splitlines()
    last_error = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].lstrip().startswith("ERROR")), 0)
    return "\n".join(lines[last_error:][-ERROR_LINES:])


class OneShotAgent(BaseAgent):
    """
    Runs a fresh subprocess per message, with the question on its stdin unless its config's prompt_via
    says otherwise (e.g. "flag" for hermes chat -q "prompt").

    No persistent process, no PTY, no TUI rendering issues.
    Each send() spawns a new process, captures stdout, returns the result.
    """

    def __init__(self, config: AgentConfig, *, timeout: float | None = None):
        super().__init__(config)
        self.timeout = timeout or config.transport_timeout
        self._listen_callback: Callable[[str], Awaitable[None]] | None = None

    async def connect(self) -> None:
        """One-shot doesn't maintain a connection — just mark as ready."""
        if not self.config.command:
            raise ValueError(f"Agent '{self.name}' missing command")
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def send(self, message: str) -> None:
        """Send a message and stream the response via listener callback."""
        response = await self._run_command(message, effort=self.config.effort)
        if self._listen_callback and response:
            await self._listen_callback(response)

    async def send_and_receive(self, message: str, **kwargs) -> str:
        """Send and return full response. Used by /full mode.
        effort= overrides the agent's thinking level for this call. on_text= gets the answer as
        it's written, and on_usage= its tokens and cost, from CLIs that print those
        (stdout_format "claude-stream-json"); other kwargs are ignored.
        """
        return await self._run_command(message, effort=kwargs.get("effort") or self.config.effort,
                                       on_text=kwargs.get("on_text"), on_usage=kwargs.get("on_usage"))

    async def listen(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """Register callback and block while 'connected'."""
        self._listen_callback = callback
        while self._connected:
            await asyncio.sleep(0.2)

    def _stdin_hint(self) -> str:
        """For an agent whose config doesn't say how it takes the question: that used to be -q PROMPT, and
        a program that still wants it there fails now. It goes right after the agent's name, before what
        the program said: `ixel review` shows only an error's first 160 or 200 characters."""
        if not self.config.prompt_via_default:
            return ""
        return ' (it got the question on stdin; if it takes it as an argument, set prompt_via = "arg" or "flag")'

    async def _args(self, env: dict[str, str]) -> list[str]:
        return await version_args(self.config, env)

    def _build_command(self, message: str, output_file: str | None = None, effort: str = "",
                       args: list[str] | None = None) -> tuple[list[str], bytes | None]:
        cmd = [self.config.command] + list((self.config.args or []) if args is None else args)
        if self.config.model and self.config.model_args:
            if not valid_model_id(self.config.model):  # never something the CLI could read as a flag
                raise ValueError(f"'{self.name}': {self.config.model!r} isn't a valid model name")
            cmd += [a.replace("{model}", self.config.model) for a in self.config.model_args]
        # The nearest level the program (and, for Codex, its model) takes, or none when it takes none
        level = to_send(agent_levels(self.config), effort) if self.config.effort_args else None
        if level:
            cmd += [a.replace("{effort}", level) for a in self.config.effort_args]
        if output_file and self.config.output_flag:
            cmd += [self.config.output_flag, output_file]
        mode = self.config.prompt_via
        too_long = arg_size(message) > ARG_LIMIT
        if mode == "stdin" or (mode == "auto" and too_long):
            return cmd, message.encode("utf-8")
        if too_long:
            raise RuntimeError(
                f"'{self.name}': this prompt is too long to pass as a command-line argument. "
                'Set prompt_via = "stdin" for this agent if its CLI can read the prompt from stdin.')
        if mode in ("arg", "auto"):
            # "--" ends option parsing, so a prompt starting with "-" (or one a
            # model wrote, in a review round) can't turn into a CLI flag.
            return cmd + ["--", message], None
        # "flag": Hermes-style `-q PROMPT [--resume ID]`
        cmd += ["-q", message]
        if self.config.last_session_id:
            cmd += ["--resume", self.config.last_session_id]
        return cmd, None

    async def _run_command(self, message: str, effort: str = "",
                           on_text: Callable[[str], Awaitable[None]] | None = None,
                           on_usage: OnUsage | None = None) -> str:
        """Run the command once for this message and return its answer."""
        env = cli_env(self.config)
        args = await self._args(env)
        cwd, remove_cwd = prepare_workdir(self.config.workdir)
        # Gemini CLI, Copilot and OpenCode save the question and answer under your home folder: what a run in
        # Ixel's own temp folder left is taken away after it (leftovers.py)
        run = leftovers.prepare(self.config.command, args, cwd if remove_cwd else None, env)
        if run:
            args = args + run.args
        output_file = None
        if self.config.output_flag:
            output_dir = cwd if remove_cwd else tempfile.mkdtemp(prefix="ixel-out-")
            output_file = os.path.join(output_dir, "answer.txt")
        try:
            cmd, stdin_data = self._build_command(message, output_file, effort, args)
            try:
                cmd = resolve_argv(cmd)
            except FileNotFoundError:
                raise RuntimeError(f"Command not found: {self.config.command}") from None
        except (LaunchError, RuntimeError, ValueError):
            if run:
                run.drop()
            remove_workdir(cwd, remove_cwd)
            if output_file and not remove_cwd:
                remove_workdir(os.path.dirname(output_file), True)
            raise
        logger.info("Running one-shot agent '%s': %s (prompt via %s)",
                    self.name, self.config.command, self.config.prompt_via)

        if cwd:
            in_folder(env, cwd)
        tree = None
        try:
            try:
                proc, tree = await create_process_tree(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    limit=_LINE_LIMIT,
                    **SPAWN_OPTIONS,
                )
            except FileNotFoundError:
                raise RuntimeError(f"Command not found: {self.config.command}") from None
            try:
                if self.config.stdout_format == "claude-stream-json":
                    reading = _read_claude_stream(proc, stdin_data, on_text, on_usage)
                else:
                    reading = proc.communicate(stdin_data)
                stdout, stderr = await asyncio.wait_for(reading, timeout=self.timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(f"'{self.name}' timed out after {self.timeout:g}s{self._stdin_hint()}") from None

            if output_file and os.path.isfile(output_file):
                with open(output_file, "rb") as handle:
                    written = handle.read()
                if written.strip():
                    stdout = written  # the CLI's final answer, without its progress chatter
            result, session_id = _split_footer(_visible_text(stdout))
            if session_id is None:
                session_id = _session_id_in_log(_visible_text(stderr))
            if session_id:
                self.config.last_session_id = session_id

            if proc.returncode != 0:
                if self.config.args_by_version or self.config.required_args or \
                        preset_for(self.config.command).get("required_args"):
                    forget_major_version(self.config.command)
                # A crashed CLI's partial output is not an answer (a review
                # round would otherwise grade it as one)
                from ixel_mat.material import mask_secrets  # material imports the agents package
                said = _visible_text(stderr).strip()
                # One Ixel can say what to do about, like Gemini CLI that isn't signed in
                plain = plain_error(preset_for(self.config.command).get("id", ""), said)
                if plain:
                    raise RuntimeError(plain)
                # Masked before it's cut, so a key's start isn't cut off and the rest left showing
                detail = mask_secrets(said or result)[-500:]
                failed = UsageLimit if out_of_usage(_own_error(said, message, stdin_data)) else RuntimeError
                raise failed(f"'{self.name}' exited with code {proc.returncode}{self._stdin_hint()}: "
                             f"{detail or 'no output'}")

            return result
        finally:
            # Timeout, error, or cancellation (a review stops waiting for a slow agent): never leave
            # the command running in the background, or anything it started. That includes
            # a helper still holding the output pipe after the command itself exited, and one
            # left running after a clean exit.
            if tree is not None:
                await tree.kill()
                tree.close()
            remove_workdir(cwd, remove_cwd)
            if output_file and not remove_cwd:
                remove_workdir(os.path.dirname(output_file), True)
            if run and tree is not None:
                # Once the CLI and everything it started have exited, so nothing is still writing. In a thread: a
                # database a CLI of yours is using at that moment can take a moment to be free (and if this call
                # is cancelled meanwhile, the thread still finishes)
                await asyncio.to_thread(run.clean)
            elif run:
                run.drop()
