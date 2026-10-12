"""
The Windows low-integrity sandbox for the agent's commands (ixel_mat/agent/winsandbox.py, and how sandbox.py and
the runner use it).

On every system, with the Win32 functions replaced by a fake that models the files' labels and the handles it
hands out and keeps each call: the helpers (command line, environment block, label strings), the calls start()
makes and their order, that a folder that was already Low is never touched, that runs in one folder share its
label, that every failure undoes what was done, how Windows error numbers become Python's, the sandbox's test
run, what run_command does with it (its kind, a failed setup, Stop, the time limit, the output limit), and what
the card and the result say. On Windows only: a command at low integrity really writes in the workspace, can't
write in a Medium folder of the profile, can read one, and the workspace's label is back afterwards.
"""
from __future__ import annotations

import asyncio
import ctypes
import errno
import functools
import io
import itertools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ixel_mat.agent import runner, sandbox, winsandbox
from ixel_mat.agent.runner import AgentRun
from ixel_mat.agent.sandbox import CommandResult
from ixel_mat.agent.store import AgentStore
from ixel_mat.agents.base import AgentConfig
from ixel_mat.gui import server as gui_server

windows_only = pytest.mark.skipif(os.name != "nt", reason="needs Windows: it runs a command at low integrity")

STATIC = Path(gui_server.__file__).parent / "static"
LOW = winsandbox.LOW_LABEL_SDDL
MEDIUM = "S:(ML;;NW;;;ME)"  # a label of a folder's own that isn't ours: it has to come back exactly
INHERITED_MEDIUM = "S:AI(ML;OICIID;NW;;;ME)"
INVALID_HANDLE = ctypes.c_void_p(-1).value
WAIT_TIMEOUT = 0x102
INFINITE = WAIT_FAILED = 0xFFFFFFFF
_CODE = object()  # what a failing function returns when that is its error number (the security functions)
# The cached functions: a test replaces the names in winsandbox, and these are what get cleared
_PROBE, _API = winsandbox._probe, winsandbox._api

# The Win32 calls start() makes, in order, without CloseHandle and LocalFree
_START_CALLS = [
    # the workspace's label is read, then it and the private temp folder get the Low label
    "GetNamedSecurityInfoW", "ConvertSecurityDescriptorToStringSecurityDescriptorW",
    "ConvertStringSecurityDescriptorToSecurityDescriptorW", "GetSecurityDescriptorSacl", "SetNamedSecurityInfoW",
    "ConvertStringSecurityDescriptorToSecurityDescriptorW", "GetSecurityDescriptorSacl", "SetNamedSecurityInfoW",
    # a copy of Ixel's token at Low integrity
    "GetCurrentProcess", "OpenProcessToken", "DuplicateTokenEx", "ConvertStringSidToSidW", "GetLengthSid",
    "SetTokenInformation",
    # the job, the output pipe and the input
    "CreateJobObjectW", "SetInformationJobObject", "CreatePipe", "SetHandleInformation", "open_osfhandle",
    "CreateFileW",
    # started suspended with only those two handles, put in the job, then let go
    "InitializeProcThreadAttributeList", "InitializeProcThreadAttributeList", "UpdateProcThreadAttribute",
    "CreateProcessAsUserW", "DeleteProcThreadAttributeList", "AssignProcessToJobObject", "ResumeThread",
]


# ── A fake of the Win32 functions ────────────────────────────────────────────

def _win32(failure=0):
    """Makes a FakeWin32 method stand for the Win32 function of its name: the call is kept, and when the test
    made that call fail it returns failure (_CODE: the error number itself, as the security functions do)."""
    def decorate(function):
        name = function.__name__

        @functools.wraps(function)
        def call(self, *args):
            self.calls.append((name, args))
            code = self.failing(name)
            if code is None:
                return function(self, *args)
            if name in self.partial:  # it got as far as changing something before it failed
                function(self, *args)
            return code if failure is _CODE else failure
        return call
    return decorate


class _Stream(io.BytesIO):
    """What winsandbox._reader gives in these tests: the command's output, as a file with read1. Closing it lets
    go of the pipe's read handle, as closing the real one does."""

    def __init__(self, data: bytes, release):
        super().__init__(data)
        self._release = release

    def close(self) -> None:
        if not self.closed:
            self._release()
        super().close()


def _parse_environment(block: str | None) -> dict[str, str]:
    """The NAME=value entries of an environment block (the empty one that ends it, and the hidden =C: ones, aside)."""
    entries: dict[str, str] = {}
    for entry in (block or "").split("\x00"):
        name, separator, value = entry.partition("=")
        if separator and name:
            entries[name] = value
    return entries


class FakeWin32:
    """Stands in for winsandbox._api(): the Win32 functions it uses, answering from a small model of the files'
    labels and of the handles and memory it hands out, and keeping every call in order (calls).

    A test makes a call fail with fail[name] = (error number, which call of that name: 0 for the first, None
    for each). The command it "starts" ends at once with exit_on_start (None: it runs until something ends
    it, or the test sets exit) and its output is output. open and memory are what hasn't been closed or freed
    yet, and bad is every handle or pointer used, closed or freed that wasn't there."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.counts: dict[str, int] = {}
        self.fail: dict[str, tuple[int, int | None]] = {}
        self.partial: set[str] = set()
        self.error = 0
        self.labels: dict[str, str] = {}      # path -> its label string; a path without one isn't here
        self.sets: list[tuple[str, str]] = []  # (path, label) each time a label was set, '' for taking it off
        self.empty_acls: list[bytes] = []
        self.no_sacl = False
        self.open: set[int] = set()
        self.roles: dict[int, str] = {}
        self.memory: set[int] = set()
        self.bad: list[str] = []
        self.levels: dict[int, tuple] = {}
        self.job_limits: dict[int, tuple[int, int]] = {}
        self.handle_flags: dict[int, tuple[int, int]] = {}
        self.pipe_inheritable = False
        self.handle_list: list[int] | None = None
        self.attribute: int | None = None
        self.output = b""
        self.exit_on_start: int | None = 0
        self.exit: int | None = None
        self.suspended = False
        self.process_job: int | None = None
        self.started: SimpleNamespace | None = None
        self.on_create = None
        self.on_wait = None
        self.streams: list[_Stream] = []
        self._ids = itertools.count(0x1000, 8)
        self._descriptors: dict[int, str] = {}
        self._sacls: dict[int, str] = {}
        self._sids: dict[int, str] = {}
        self._buffers: dict[int, ctypes.Array] = {}

    # What the tests look at
    def last_error(self) -> int:
        return self.error

    def names(self, *skip: str) -> list[str]:
        return [name for name, _ in self.calls if name not in skip]

    def calls_to(self, name: str) -> list[tuple]:
        return [args for call, args in self.calls if call == name]

    def handle_of(self, role: str) -> int:
        return next(handle for handle, found in self.roles.items() if found == role)

    def failing(self, name: str) -> int | None:
        """The error number this call of name is to fail with, or None. It counts the call."""
        index = self.counts.get(name, 0)
        self.counts[name] = index + 1
        wanted = self.fail.get(name)
        if wanted is None or wanted[1] not in (None, index):
            return None
        self.error = wanted[0]
        return wanted[0]

    # Handles and memory
    def _handle(self, role: str) -> int:
        handle = next(self._ids)
        self.open.add(handle)
        self.roles[handle] = role
        return handle

    def _alloc(self) -> int:
        pointer = next(self._ids)
        self.memory.add(pointer)
        return pointer

    def _use(self, handle, call: str) -> None:
        if handle not in self.open:
            self.bad.append(f"{call} was given a handle that isn't open: {handle!r}")

    @_win32()
    def CloseHandle(self, handle):
        if handle not in self.open:
            self.bad.append(f"CloseHandle was given a handle that isn't open: {handle!r}")
            return 0
        self.open.discard(handle)
        killing = self.job_limits.get(handle, (0, 0))[1] & winsandbox._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if killing and handle == self.process_job and self.exit is None:
            self.exit = 1  # the last handle of a job with KILL_ON_JOB_CLOSE ends what is in it
        return 1

    @_win32()
    def LocalFree(self, pointer):
        self._buffers.pop(pointer, None)
        if pointer not in self.memory:
            self.bad.append(f"LocalFree was given memory that isn't allocated: {pointer!r}")
            return pointer
        self.memory.discard(pointer)
        return None

    # Labels
    @_win32(_CODE)
    def GetNamedSecurityInfoW(self, path, kind, info, owner, group, dacl, sacl, descriptor):
        pointer = self._alloc()
        self._descriptors[pointer] = self.labels.get(path, "")
        descriptor._obj.value = pointer
        return 0

    @_win32()
    def ConvertSecurityDescriptorToStringSecurityDescriptorW(self, descriptor, revision, info, string, length):
        buffer = ctypes.create_unicode_buffer(self._descriptors[descriptor])
        address = ctypes.addressof(buffer)
        self._buffers[address] = buffer  # ctypes.wstring_at reads it from there
        self.memory.add(address)
        string._obj.value = address
        return 1

    @_win32()
    def ConvertStringSecurityDescriptorToSecurityDescriptorW(self, text, revision, descriptor, size):
        pointer = self._alloc()
        self._descriptors[pointer] = text
        descriptor._obj.value = pointer
        return 1

    @_win32()
    def GetSecurityDescriptorSacl(self, descriptor, present, sacl, defaulted):
        pointer = descriptor + 4
        self._sacls[pointer] = self._descriptors[descriptor]
        present._obj.value = 0 if self.no_sacl else 1
        sacl._obj.value = pointer
        defaulted._obj.value = 0
        return 1

    @_win32(_CODE)
    def SetNamedSecurityInfoW(self, name, kind, info, owner, group, dacl, sacl):
        path = name.value
        if isinstance(sacl, ctypes.Array):  # the empty ACL: the folder is left with no label of its own
            self.empty_acls.append(bytes(sacl))
            self.labels.pop(path, None)
            self.sets.append((path, ""))
        else:
            self.labels[path] = self._sacls[sacl]
            self.sets.append((path, self._sacls[sacl]))
        return 0

    # The token
    @_win32()
    def GetCurrentProcess(self):
        return -1

    @_win32()
    def OpenProcessToken(self, process, access, token):
        token._obj.value = self._handle("own token")
        return 1

    @_win32()
    def DuplicateTokenEx(self, token, access, attributes, level, kind, new):
        self._use(token, "DuplicateTokenEx")
        new._obj.value = self._handle("low token")
        return 1

    @_win32()
    def ConvertStringSidToSidW(self, text, sid):
        pointer = self._alloc()
        self._sids[pointer] = text
        sid._obj.value = pointer
        return 1

    @_win32()
    def GetLengthSid(self, sid):
        return 12

    @_win32()
    def SetTokenInformation(self, token, kind, info, size):
        self._use(token, "SetTokenInformation")
        label = info._obj
        self.levels[token] = (kind, self._sids.get(label.Label.Sid), label.Label.Attributes)
        return 1

    # The job, the pipe and the input
    @_win32()
    def CreateJobObjectW(self, attributes, name):
        return self._handle("job")

    @_win32()
    def SetInformationJobObject(self, job, kind, info, size):
        self._use(job, "SetInformationJobObject")
        self.job_limits[job] = (kind, info._obj.BasicLimitInformation.LimitFlags)
        return 1

    @_win32()
    def CreatePipe(self, read, write, attributes, size):
        read._obj.value = self._handle("pipe read")
        write._obj.value = self._handle("pipe write")
        self.pipe_inheritable = bool(attributes._obj.bInheritHandle)
        return 1

    @_win32()
    def SetHandleInformation(self, handle, mask, flags):
        self._use(handle, "SetHandleInformation")
        self.handle_flags[handle] = (mask, flags)
        return 1

    @_win32(INVALID_HANDLE)
    def CreateFileW(self, name, access, share, attributes, disposition, flags, template):
        return self._handle("nul")

    def reader(self, handle: int) -> _Stream:
        """Stands in for winsandbox._reader (which needs msvcrt): the output as a file."""
        self.calls.append(("open_osfhandle", (handle,)))
        if self.failing("open_osfhandle") is not None:
            self.open.discard(handle)  # the real one closes the handle when it fails
            raise winsandbox.SetupError("open_osfhandle failed (made to fail by the test)")
        stream = _Stream(self.output, lambda: self.open.discard(handle))
        self.streams.append(stream)
        return stream

    # Starting the command
    @_win32()
    def InitializeProcThreadAttributeList(self, attributes, count, flags, size):
        if attributes is None:  # asked for the size: it says how much, and fails, as the real one does
            size._obj.value = 48
            self.error = 122
            return 0
        return 1

    @_win32()
    def UpdateProcThreadAttribute(self, attributes, flags, attribute, value, size, previous, returned):
        self.attribute = attribute
        self.handle_list = list(value)
        return 1

    @_win32()
    def DeleteProcThreadAttributeList(self, attributes):
        return None

    @_win32()
    def CreateProcessAsUserW(self, token, application, command, process_attributes, thread_attributes, inherit,
                             flags, environment, directory, startup, info):
        self._use(token, "CreateProcessAsUserW")
        basic = startup._obj.StartupInfo
        block = environment.raw.decode("utf-16-le", "surrogatepass") if environment is not None else None
        process, thread = self._handle("process"), self._handle("thread")
        self.suspended = True
        self.exit = None
        info._obj.hProcess, info._obj.hThread, info._obj.dwProcessId = process, thread, 4242
        self.started = SimpleNamespace(
            token=token, application=application, command=command.value, inherit=inherit, flags=flags,
            block=block, env=_parse_environment(block), directory=directory, startup_flags=basic.dwFlags,
            stdin=basic.hStdInput, stdout=basic.hStdOutput, stderr=basic.hStdError, process=process,
            thread=thread)
        if self.on_create is not None:
            self.on_create(self.started)
        return 1

    @_win32()
    def AssignProcessToJobObject(self, job, process):
        self._use(job, "AssignProcessToJobObject")
        self._use(process, "AssignProcessToJobObject")
        self.process_job = job
        return 1

    @_win32(WAIT_FAILED)
    def ResumeThread(self, thread):
        self._use(thread, "ResumeThread")
        self.suspended = False
        if self.exit_on_start is not None:
            self.exit = self.exit_on_start
        return 1

    # Watching and ending it
    @_win32(WAIT_FAILED)
    def WaitForSingleObject(self, handle, millis):
        self._use(handle, "WaitForSingleObject")
        if self.exit is not None:
            return 0
        if self.on_wait is not None:
            self.on_wait()
        if millis == INFINITE:  # no test waits for ever for a command that nothing ends
            self.bad.append("waited without a limit for a command that was still running")
        time.sleep(min(millis, 50) / 1000)
        return WAIT_TIMEOUT

    @_win32()
    def GetExitCodeProcess(self, process, code):
        self._use(process, "GetExitCodeProcess")
        code._obj.value = 259 if self.exit is None else self.exit  # 259: STILL_ACTIVE
        return 1

    @_win32()
    def TerminateJobObject(self, job, code):
        self._use(job, "TerminateJobObject")
        if job == self.process_job and self.exit is None:
            self.exit = code
        return 1

    @_win32()
    def TerminateProcess(self, process, code):
        self._use(process, "TerminateProcess")
        if self.exit is None:
            self.exit = code
        return 1


# ── Fixtures and helpers ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch, tmp_path):
    """The sandbox remembers the labels it holds, whether it broke and its test run: each test starts clean and
    leaves none for the next, and its private temp folders are made in a folder of the test's own."""
    monkeypatch.setattr(winsandbox, "_labels", {})
    monkeypatch.setattr(winsandbox, "_broken", None)
    _PROBE.cache_clear()
    _API.cache_clear()
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    yield
    _PROBE.cache_clear()
    _API.cache_clear()


@pytest.fixture
def api(monkeypatch):
    """winsandbox's Win32 functions replaced by a FakeWin32."""
    fake = FakeWin32()
    monkeypatch.setattr(winsandbox, "_api", lambda: fake)
    return fake


@pytest.fixture
def win32(api, monkeypatch):
    """The same, and the output pipe as a file in memory (msvcrt exists on Windows only)."""
    monkeypatch.setattr(winsandbox, "_reader", api.reader)
    return api


@pytest.fixture
def windows(monkeypatch):
    """sandbox_kind and run_command as on a Windows computer whose sandbox passed its test (faked)."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(winsandbox, "_probe", lambda: (True, None))


@pytest.fixture
def work(tmp_path):
    folder = tmp_path / "work"
    folder.mkdir()
    return folder


def _assert_clean(win32: FakeWin32, folder: str | None = None, label: str = "") -> None:
    """Nothing of a run is left: every handle closed and every allocation freed, nothing used that wasn't
    there, the workspace's label as it was (label), no label still held, and no private temp folder."""
    assert win32.bad == []
    assert win32.open == set()
    assert win32.memory == set()
    if folder is not None:
        assert win32.labels.get(folder, "") == label
    assert winsandbox._labels == {}
    assert list(Path(tempfile.gettempdir()).iterdir()) == []


def _finish(proc: winsandbox.LowProcess) -> None:
    """What run_command does at the end of a run: the output is let go, then the command."""
    proc.stdout.close()
    proc.close()


# ── The helpers that need no Windows ─────────────────────────────────────────

def test_command_line_is_quoted_the_way_popen_does_it():
    argv = ["C:\\Program Files\\tool.exe", "a b", 'say "hi"', "", "plain", "back\\slash\\"]
    assert winsandbox.command_line(argv) == subprocess.list2cmdline(argv)
    assert winsandbox.command_line(["tool", "a b"]) == 'tool "a b"'
    assert winsandbox.command_line(["tool", 'say "hi"']) == 'tool "say \\"hi\\""'
    assert winsandbox.command_line(["tool", ""]) == 'tool ""'


def test_environment_block_replaces_temp_and_sorts_by_upper_cased_name():
    env = {"b": "2", "Temp": "C:\\old", "A": "1", "tmp": "C:\\older", "PATH": "x"}
    block = winsandbox.environment_block(env, "C:\\private")
    assert block == "A=1\x00b=2\x00PATH=x\x00TEMP=C:\\private\x00TMP=C:\\private\x00\x00"
    assert env["Temp"] == "C:\\old"  # the caller's environment isn't changed


def test_environment_block_takes_a_path_and_has_a_temp_even_when_the_environment_is_empty(tmp_path):
    block = winsandbox.environment_block({}, tmp_path)
    assert block == f"TEMP={tmp_path}\x00TMP={tmp_path}\x00\x00"


@pytest.mark.parametrize("env, temp", [
    ({"A": "b\x00c"}, "C:\\t"), ({"A\x00": "b"}, "C:\\t"), ({"A": "b"}, "C:\\t\x00x"),
])
def test_environment_block_refuses_a_nul(env, temp):
    with pytest.raises(ValueError):
        winsandbox.environment_block(env, temp)


@pytest.mark.parametrize("sddl", [
    "S:(ML;OICI;NW;;;LW)", "S:(ML;CIOI;NW;;;LW)", "S:(ML;OICI;NW;;;S-1-16-4096)", "S:AI(ML;OICIID;NW;;;LW)",
    "S:(ML;OICI;0x1;;;LW)", "S:(ML;OICI;0x5;;;LW)", "S:(ML;OICI;NWNR;;;LW)", "S:(ML;oici;nw;;;lw)",
])
def test_is_low_label_knows_the_label_we_would_set(sddl):
    assert winsandbox.is_low_label(sddl) is True


@pytest.mark.parametrize("sddl", [
    "", "S:(ML;OICI;NW;;;ME)", "S:(ML;OICI;NW;;;HI)", "S:(ML;;NW;;;LW)", "S:(ML;OI;NW;;;LW)", "S:(ML;CI;NW;;;LW)",
    "S:(ML;OICI;NR;;;LW)", "S:(ML;OICI;0x2;;;LW)", "S:(ML;OICI;;;;LW)", "D:(A;;FA;;;WD)", "S:AI",
])
def test_is_low_label_is_false_for_any_other_label(sddl):
    assert winsandbox.is_low_label(sddl) is False


@pytest.mark.parametrize("saved, restored", [
    ("", ""),
    (MEDIUM, MEDIUM),
    (LOW, LOW),
    (INHERITED_MEDIUM, ""),  # it came from the parent: no label of its own, so the parent's applies again
    ("S:AI(ML;ID;NW;;;ME)", ""),
])
def test_restorable_is_the_label_to_put_back(saved, restored):
    assert winsandbox._restorable(saved) == restored


# ── Reading and setting a label ──────────────────────────────────────────────

def test_label_sddl_reads_the_label_or_nothing(api, tmp_path):
    api.labels[os.fspath(tmp_path)] = LOW
    assert winsandbox.label_sddl(tmp_path) == LOW
    assert winsandbox.label_sddl(tmp_path / "other") == ""
    assert api.bad == [] and api.memory == set()  # what was allocated was freed


def test_label_sddl_counts_an_empty_label_list_as_none(api, tmp_path):
    # What taking our label off leaves behind reads as 'S:', and that's the same as no label at all
    api.labels[os.fspath(tmp_path)] = "S:"
    assert winsandbox.label_sddl(tmp_path) == ""
    assert api.bad == [] and api.memory == set()


def test_label_sddl_says_which_call_failed(api, tmp_path):
    api.fail["GetNamedSecurityInfoW"] = (5, None)
    with pytest.raises(winsandbox.SetupError) as raised:
        winsandbox.label_sddl(tmp_path)
    assert raised.value.code == 5
    assert "GetNamedSecurityInfoW failed (Windows error 5" in str(raised.value)
    api.fail.clear()
    api.fail["ConvertSecurityDescriptorToStringSecurityDescriptorW"] = (87, None)
    with pytest.raises(winsandbox.SetupError, match="ConvertSecurityDescriptorToStringSecurityDescriptorW"):
        winsandbox.label_sddl(tmp_path)
    assert api.bad == [] and api.memory == set()


def test_set_label_sets_it_and_an_empty_label_takes_it_off(api, tmp_path):
    path = os.fspath(tmp_path)
    winsandbox._set_label(tmp_path, LOW)
    assert api.labels[path] == LOW
    assert api.calls_to("SetNamedSecurityInfoW")[0][1:3] == (1, 0x10)  # a file object, the label only
    winsandbox._set_label(tmp_path, "")
    assert path not in api.labels
    assert api.empty_acls == [bytes([2, 0, 8, 0, 0, 0, 0, 0])]  # an ACL with no entries
    assert api.sets == [(path, LOW), (path, "")]
    assert api.bad == [] and api.memory == set()


@pytest.mark.parametrize("call", [
    "ConvertStringSecurityDescriptorToSecurityDescriptorW", "GetSecurityDescriptorSacl", "SetNamedSecurityInfoW",
])
def test_set_label_failures_are_a_setup_error_and_free_what_they_allocated(api, tmp_path, call):
    api.fail[call] = (5, None)
    with pytest.raises(winsandbox.SetupError, match=call) as raised:
        winsandbox._set_label(tmp_path, LOW)
    assert raised.value.code == 5
    assert api.labels == {}
    assert api.bad == [] and api.memory == set()


def test_set_label_without_a_label_in_the_descriptor_is_a_setup_error(api, tmp_path):
    api.no_sacl = True
    with pytest.raises(winsandbox.SetupError, match="no label"):
        winsandbox._set_label(tmp_path, LOW)
    assert api.labels == {} and api.memory == set()


# ── start(): the calls it makes ──────────────────────────────────────────────

def test_start_makes_the_calls_in_order_with_a_low_token_in_a_job(win32, work):
    folder = os.path.realpath(work)
    win32.output = b"out\r\n"
    env = {"PATH": "x", "Temp": "C:\\old"}
    argv = ["tool", "a b"]
    proc = winsandbox.start(argv, work, env)
    try:
        assert win32.names("LocalFree", "CloseHandle") == _START_CALLS
        own, low = win32.handle_of("own token"), win32.handle_of("low token")
        job, nul = win32.handle_of("job"), win32.handle_of("nul")
        read, write = win32.handle_of("pipe read"), win32.handle_of("pipe write")
        process, thread = win32.handle_of("process"), win32.handle_of("thread")
        # A copy of the token (impersonation level, made a primary token) at Low integrity
        duplicate = win32.calls_to("DuplicateTokenEx")[0]
        assert duplicate[0] == own and duplicate[1:5] == (0, None, 2, 1)
        assert win32.levels == {low: (25, "S-1-16-4096", 0x20)}  # TokenIntegrityLevel, Low, SE_GROUP_INTEGRITY
        # In a job that ends what is in it when its handle closes
        assert win32.job_limits == {job: (9, 0x2000)}
        assert win32.process_job == job
        # The pipe's read end stays with Ixel, and only the two handles it needs go to the command
        assert win32.pipe_inheritable is True
        assert win32.handle_flags == {read: (1, 0)}
        assert win32.attribute == 0x20002
        assert win32.handle_list == [nul, write]
        started = win32.started
        assert started.token == low
        assert started.application is None
        assert started.command == 'tool "a b"'
        assert started.inherit == 1
        assert started.flags == 0x00000004 | 0x08000000 | 0x00000400 | 0x00080000  # suspended, no window, ...
        assert started.directory == folder
        assert started.startup_flags == 0x100  # STARTF_USESTDHANDLES
        assert (started.stdin, started.stdout, started.stderr) == (nul, write, write)
        # Its environment is the one given, with TEMP and TMP at the private folder
        assert started.env["PATH"] == "x"
        assert Path(started.env["TEMP"]) == proc.temp == Path(started.env["TMP"])
        assert "Temp" not in started.env
        assert started.block == winsandbox.environment_block(env, proc.temp)
        assert proc.temp.parent == Path(tempfile.gettempdir())
        assert proc.temp.name.startswith("ixel-agent-") and proc.temp.is_dir()
        # What was closed on the way, in order, and what is held
        assert [args[0] for args in win32.calls_to("CloseHandle")] == [own, low, write, nul, thread]
        assert win32.open == {job, process, read}
        # The labels, and the command as Popen's
        assert win32.labels[folder] == LOW
        assert win32.labels[os.fspath(proc.temp)] == LOW
        assert winsandbox._labels == {folder: [1, ""]}
        assert proc.pid == 4242 and proc.args == argv and proc.args is not argv
        assert proc.poll() == 0 and proc.returncode == 0
        assert proc.stdout.read() == b"out\r\n"
    finally:
        _finish(proc)
    assert not proc.temp.exists()
    assert win32.sets[-1] == (folder, "")
    _assert_clean(win32, folder)


def test_start_resumes_the_command_only_after_it_is_in_the_job(win32, work):
    proc = winsandbox.start(["tool"], work, {})
    _finish(proc)
    names = win32.names()
    assert names.index("CreateProcessAsUserW") < names.index("AssignProcessToJobObject") < names.index("ResumeThread")
    assert win32.suspended is False


@pytest.mark.parametrize("argv", [[], ["tool\x00x"], ["tool", "a\x00"]])
def test_start_refuses_an_empty_command_or_one_with_a_nul(win32, work, argv):
    with pytest.raises(ValueError):
        winsandbox.start(argv, work, {})
    assert win32.calls == []
    _assert_clean(win32)


def test_start_refuses_a_nul_in_the_environment_and_leaves_no_temp_folder(win32, work):
    with pytest.raises(ValueError):
        winsandbox.start(["tool"], work, {"A": "b\x00c"})
    assert win32.calls == []
    _assert_clean(win32)


def test_start_refuses_a_command_line_that_is_too_long_before_anything_is_set_up(win32, work):
    with pytest.raises(OSError) as raised:
        winsandbox.start(["tool", "x" * 40000], work, {})
    assert type(raised.value) is OSError and raised.value.errno == errno.E2BIG
    assert win32.calls == []
    _assert_clean(win32)


def test_start_refuses_a_whole_drive_as_the_workspace(win32):
    root = Path(os.path.abspath(os.sep))
    with pytest.raises(winsandbox.SetupError, match="drive"):
        winsandbox.start(["tool"], root, {})
    assert win32.calls == []
    _assert_clean(win32)


# ── The workspace's label ────────────────────────────────────────────────────

@pytest.mark.parametrize("before, after", [("", ""), (MEDIUM, MEDIUM), (INHERITED_MEDIUM, "")])
def test_the_workspace_is_low_during_the_run_and_has_its_label_back_after(win32, work, before, after):
    folder = os.path.realpath(work)
    if before:
        win32.labels[folder] = before
    proc = winsandbox.start(["tool"], work, {})
    assert win32.labels[folder] == LOW
    assert winsandbox._labels[folder] == [1, before]
    _finish(proc)
    assert win32.labels.get(folder, "") == after
    assert [text for path, text in win32.sets if path == folder] == [LOW, after]
    _assert_clean(win32, folder, after)


@pytest.mark.parametrize("label", [LOW, "S:AI(ML;OICIID;NW;;;LW)"])
def test_a_folder_that_is_already_low_is_never_relabelled_or_put_back(win32, work, label):
    folder = os.path.realpath(work)
    win32.labels[folder] = label
    proc = winsandbox.start(["tool"], work, {})
    assert winsandbox._labels[folder] == [1, None]  # None: not ours, so never taken off
    assert [path for path, _ in win32.sets] == [os.fspath(proc.temp)]  # only the private temp folder
    _finish(proc)
    assert [path for path, _ in win32.sets] == [os.fspath(proc.temp)]
    assert win32.labels[folder] == label
    _assert_clean(win32, folder, label)


def test_a_label_that_failed_halfway_is_put_back(win32, work):
    folder = os.path.realpath(work)
    win32.labels[folder] = MEDIUM
    win32.fail["SetNamedSecurityInfoW"] = (5, 0)
    win32.partial.add("SetNamedSecurityInfoW")  # it changed the folder, then failed
    with pytest.raises(winsandbox.SetupError, match="SetNamedSecurityInfoW"):
        winsandbox.start(["tool"], work, {})
    assert [text for path, text in win32.sets if path == folder] == [LOW, MEDIUM]
    _assert_clean(win32, folder, MEDIUM)


def test_overlapping_runs_share_one_label_and_the_last_one_puts_it_back(win32, work):
    folder = os.path.realpath(work)
    first = winsandbox.start(["one"], work, {})
    second = winsandbox.start(["two"], work, {})
    assert winsandbox._labels[folder] == [2, ""]
    assert [args[0] for args in win32.calls_to("GetNamedSecurityInfoW")] == [folder]  # read once
    assert [text for path, text in win32.sets if path == folder] == [LOW]  # set once
    _finish(first)
    assert win32.labels[folder] == LOW  # the second still needs it
    assert winsandbox._labels[folder] == [1, ""]
    assert not first.temp.exists() and second.temp.exists()
    _finish(second)
    assert [text for path, text in win32.sets if path == folder] == [LOW, ""]
    _assert_clean(win32, folder)


def test_runs_in_threads_at_once_set_the_label_once_and_the_last_to_end_puts_it_back(win32, work):
    folder = os.path.realpath(work)
    count = 4
    everyone_started = threading.Barrier(count)
    errors: list[BaseException] = []

    def one_run() -> None:
        try:
            proc = winsandbox.start(["tool"], work, {})
            everyone_started.wait(timeout=20)
            _finish(proc)
        except BaseException as exc:  # reported by the test, not lost in the thread
            errors.append(exc)

    threads = [threading.Thread(target=one_run) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    assert [text for path, text in win32.sets if path == folder] == [LOW, ""]
    _assert_clean(win32, folder)


def test_a_label_that_cannot_be_put_back_is_kept_for_the_next_run_to_try_again(win32, work, caplog):
    folder = os.path.realpath(work)
    win32.labels[folder] = MEDIUM
    first = winsandbox.start(["one"], work, {})
    win32.fail["SetNamedSecurityInfoW"] = (5, win32.counts["SetNamedSecurityInfoW"])  # the next call: the restore
    first.stdout.close()
    with caplog.at_level(logging.WARNING, logger=winsandbox.__name__):
        first.close()  # it never raises
    assert "Could not take the Low label off" in caplog.text
    assert win32.labels[folder] == LOW  # still on the folder, so still ours to take off
    assert winsandbox._labels[folder] == [0, MEDIUM]
    win32.fail.clear()
    second = winsandbox.start(["two"], work, {})
    assert winsandbox._labels[folder] == [1, MEDIUM]
    _finish(second)
    assert win32.labels[folder] == MEDIUM
    _assert_clean(win32, folder, MEDIUM)


# ── start(): when a call fails ───────────────────────────────────────────────

# (the call that fails, which of its calls (0: the first), the Windows error number)
FAILURES = [
    ("GetNamedSecurityInfoW", 0, 5),
    ("ConvertSecurityDescriptorToStringSecurityDescriptorW", 0, 5),
    ("ConvertStringSecurityDescriptorToSecurityDescriptorW", 0, 87),  # the workspace's label
    ("GetSecurityDescriptorSacl", 0, 87),
    ("SetNamedSecurityInfoW", 0, 5),
    ("ConvertStringSecurityDescriptorToSecurityDescriptorW", 1, 87),  # the private temp folder's label
    ("SetNamedSecurityInfoW", 1, 5),
    ("OpenProcessToken", 0, 5),
    ("DuplicateTokenEx", 0, 5),
    ("ConvertStringSidToSidW", 0, 87),
    ("SetTokenInformation", 0, 5),
    ("CreateJobObjectW", 0, 5),
    ("SetInformationJobObject", 0, 5),
    ("CreatePipe", 0, 8),
    ("SetHandleInformation", 0, 6),
    ("open_osfhandle", 0, 0),
    ("CreateFileW", 0, 5),
    ("InitializeProcThreadAttributeList", 0, 8),  # asking for the size
    ("InitializeProcThreadAttributeList", 1, 8),
    ("UpdateProcThreadAttribute", 0, 87),
    ("CreateProcessAsUserW", 0, 1314),
    ("AssignProcessToJobObject", 0, 5),
    ("ResumeThread", 0, 5),
]


@pytest.mark.parametrize("call, nth, code", FAILURES, ids=[f"{call}-{nth}" for call, nth, _ in FAILURES])
def test_a_failed_setup_call_undoes_everything_and_the_command_does_not_run(win32, work, call, nth, code):
    folder = os.path.realpath(work)
    win32.labels[folder] = MEDIUM  # a label of the workspace's own: it has to be there afterwards
    win32.fail[call] = (code, nth)
    with pytest.raises(winsandbox.SetupError) as raised:
        winsandbox.start(["tool"], work, {})
    assert call in str(raised.value)
    if call != "open_osfhandle":
        assert f"Windows error {code}" in str(raised.value)
        assert raised.value.code == code
    created = win32.started is not None
    assert created == (call in ("AssignProcessToJobObject", "ResumeThread"))
    if created:  # it was made suspended: it is ended before it ever ran
        assert "TerminateProcess" in win32.names()
        assert win32.exit is not None
    _assert_clean(win32, folder, MEDIUM)


@pytest.mark.parametrize("code, error, number", [
    (2, FileNotFoundError, errno.ENOENT), (3, FileNotFoundError, errno.ENOENT),
    (5, PermissionError, errno.EACCES),
    (193, OSError, errno.ENOEXEC), (216, OSError, errno.ENOEXEC),
])
def test_a_program_that_cannot_be_started_gives_the_error_popen_gives(win32, work, code, error, number):
    win32.fail["CreateProcessAsUserW"] = (code, None)
    with pytest.raises(OSError) as raised:
        winsandbox.start(["tool"], work, {})
    assert type(raised.value) is error
    assert raised.value.errno == number
    _assert_clean(win32, os.path.realpath(work))


@pytest.mark.parametrize("code", [87, 740, 1314])
def test_any_other_launch_error_is_a_setup_error_naming_the_call(win32, work, code):
    win32.fail["CreateProcessAsUserW"] = (code, None)
    with pytest.raises(winsandbox.SetupError) as raised:
        winsandbox.start(["tool"], work, {})
    assert not isinstance(raised.value, OSError)
    assert raised.value.code == code
    assert f"CreateProcessAsUserW failed (Windows error {code}" in str(raised.value)
    _assert_clean(win32, os.path.realpath(work))


# ── The output pipe, with msvcrt faked ───────────────────────────────────────

def test_reader_gives_a_binary_file_with_read1(api, monkeypatch):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"output\r\n")
    os.close(write_fd)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=lambda handle, flags: read_fd))
    stream = winsandbox._reader(1234)
    try:
        assert stream.read1(100) == b"output\r\n"
        assert stream.read1(100) == b""
    finally:
        stream.close()
    assert api.calls == []  # the handle belongs to the file now


def test_reader_closes_the_handle_when_it_cannot_make_a_file(api, monkeypatch):
    def refuse(handle, flags):
        raise OSError(9, "bad handle")

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=refuse))
    with pytest.raises(winsandbox.SetupError, match="open_osfhandle failed"):
        winsandbox._reader(1234)
    assert api.calls == [("CloseHandle", (1234,))]


def test_reader_closes_the_handle_when_there_is_no_msvcrt(api, monkeypatch):
    monkeypatch.setitem(sys.modules, "msvcrt", None)  # importing it fails
    with pytest.raises(winsandbox.SetupError, match="open_osfhandle failed"):
        winsandbox._reader(1234)
    assert api.calls == [("CloseHandle", (1234,))]


def test_reader_closes_the_descriptor_when_it_cannot_open_the_file(api, monkeypatch):
    fd = os.open(os.devnull, os.O_RDONLY)

    def refuse(file, mode):
        raise OSError(24, "too many open files")

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=lambda handle, flags: fd))
    monkeypatch.setattr(winsandbox, "open", refuse, raising=False)
    with pytest.raises(winsandbox.SetupError, match="output pipe"):
        winsandbox._reader(1234)
    with pytest.raises(OSError):
        os.fstat(fd)  # it was closed


# ── A started command ────────────────────────────────────────────────────────

def test_poll_and_wait_follow_the_process(win32, work):
    win32.exit_on_start = None
    proc = winsandbox.start(["tool"], work, {})
    try:
        assert proc.poll() is None and proc.returncode is None
        for seconds, millis in [(0.25, 250), (0.0001, 1), (-5, 0), (3, 3000)]:
            with pytest.raises(subprocess.TimeoutExpired):
                proc.wait(timeout=seconds)
            assert win32.calls_to("WaitForSingleObject")[-1][1] == millis
        win32.exit = 7  # it ended by itself
        assert proc.wait(timeout=1) == 7
        assert proc.poll() == 7 and proc.returncode == 7
    finally:
        _finish(proc)
    _assert_clean(win32, os.path.realpath(work))


def test_poll_reports_a_failure_to_ask_as_an_oserror(win32, work):
    proc = winsandbox.start(["tool"], work, {})
    try:
        win32.fail["GetExitCodeProcess"] = (5, None)
        with pytest.raises(OSError):
            proc.poll()
        win32.fail.clear()
        win32.fail["WaitForSingleObject"] = (6, None)
        with pytest.raises(OSError):
            proc.wait(timeout=1)
    finally:
        win32.fail.clear()
        _finish(proc)


def test_kill_ends_the_job_and_not_only_the_process(win32, work):
    win32.exit_on_start = None
    proc = winsandbox.start(["tool"], work, {})
    try:
        proc.kill()
        assert win32.calls_to("TerminateJobObject") == [(win32.handle_of("job"), 1)]
        assert "TerminateProcess" not in win32.names()
        assert proc.poll() == 1
    finally:
        _finish(proc)
    count = len(win32.calls)
    proc.kill()  # after close() there is nothing left to end
    assert len(win32.calls) == count


def test_kill_raises_only_when_the_command_is_still_running(win32, work):
    win32.exit_on_start = None
    proc = winsandbox.start(["tool"], work, {})
    try:
        win32.fail["TerminateJobObject"] = (5, None)
        with pytest.raises(OSError):
            proc.kill()
        win32.exit = 0  # it ended on its own
        proc.kill()  # not an error now
    finally:
        win32.fail.clear()
        _finish(proc)


def test_close_ends_what_still_runs_lets_go_of_everything_and_can_be_repeated(win32, work):
    win32.exit_on_start = None
    folder = os.path.realpath(work)
    proc = winsandbox.start(["tool"], work, {})
    proc.stdout.close()
    proc.close()
    assert len(win32.calls_to("TerminateJobObject")) == 1
    assert proc.returncode == 1
    _assert_clean(win32, folder)
    proc.close()  # nothing is done twice
    _assert_clean(win32, folder)
    assert len(win32.calls_to("TerminateJobObject")) == 1


def test_close_never_raises_and_still_gives_the_label_back(win32, work):
    folder = os.path.realpath(work)
    win32.exit_on_start = None
    proc = winsandbox.start(["tool"], work, {})
    win32.fail["TerminateJobObject"] = (5, None)
    win32.fail["CloseHandle"] = (6, None)
    win32.fail["WaitForSingleObject"] = (6, None)
    proc.stdout.close()
    proc.close()
    assert win32.labels.get(folder, "") == ""
    assert winsandbox._labels == {}
    assert not proc.temp.exists()


def test_kill_tree_ends_a_low_process_through_its_job_and_ignores_a_failure(win32, work):
    win32.exit_on_start = None
    proc = winsandbox.start(["tool"], work, {})
    try:
        win32.fail["TerminateJobObject"] = (5, None)
        sandbox._kill_tree(proc)  # a failure to end it isn't raised
        win32.fail.clear()
        sandbox._kill_tree(proc)
        assert proc.poll() == 1
        assert "TerminateProcess" not in win32.names()
    finally:
        _finish(proc)


# ── The sandbox's test run ───────────────────────────────────────────────────

def _test_script(started, workspace: bool = True, temp: bool = True, outside: bool = False) -> None:
    """What the test's test.cmd does in the folder the command was started in: the writes the sandbox allows
    (workspace, temp), and the one it must refuse (outside), as a test turns them on."""
    folder = Path(started.directory)
    if workspace:
        with (folder / "existing.txt").open("ab") as existing:
            existing.write(b"changed\r\n")
        (folder / "new.txt").write_bytes(b"new\r\n")
    if temp:
        (Path(started.env["TEMP"]) / "inside.txt").write_bytes(b"temp\r\n")
    if outside:
        (folder.parent / "outside" / "denied.txt").write_bytes(b"no\r\n")


@pytest.fixture
def probing(win32, monkeypatch):
    """A Windows computer for winsandbox._probe: the platform and ctypes.WinDLL are there, and the test command
    does what test.cmd should be able to do under the sandbox, and prints what it prints."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", object, raising=False)
    win32.output = b"ixel-sandbox-ok\r\n"
    win32.on_create = _test_script
    return win32


def test_the_test_run_passes_when_the_command_does_what_the_sandbox_promises(probing):
    assert _PROBE() == (True, None)
    assert winsandbox.available() is True
    assert winsandbox.unavailable_reason() is None
    assert probing.started.command.endswith("/d /c test.cmd")
    _assert_clean(probing)


def test_the_test_run_is_made_once(probing):
    _PROBE()
    count = len(probing.calls)
    assert count
    _PROBE()
    winsandbox.available()
    winsandbox.unavailable_reason()
    assert len(probing.calls) == count


@pytest.mark.parametrize("output, script, said", [
    (b"", _test_script, "output didn't come back"),
    (b"ixel-sandbox-ok\r\n", functools.partial(_test_script, workspace=False), "couldn't write in the workspace"),
    (b"ixel-sandbox-ok\r\n", functools.partial(_test_script, temp=False), "couldn't write in its temp folder"),
    (b"ixel-sandbox-ok\r\n", functools.partial(_test_script, outside=True), "could write outside the workspace"),
])
def test_the_test_run_fails_when_the_sandbox_does_not_do_what_it_promises(probing, output, script, said):
    probing.output = output
    probing.on_create = script
    passed, reason = _PROBE()
    assert passed is False
    assert reason.startswith("Commands run without a sandbox because the Windows sandbox failed its test run: ")
    assert said in reason
    assert winsandbox.available() is False
    assert winsandbox.unavailable_reason() == reason
    _assert_clean(probing)


def test_the_test_run_fails_when_the_label_is_not_put_back(probing):
    probing.fail["SetNamedSecurityInfoW"] = (5, 2)  # the workspace, the temp folder, then taking it off again
    passed, reason = _PROBE()
    assert passed is False
    assert "label wasn't put back" in reason


def test_the_test_run_fails_when_the_command_does_not_finish(probing, monkeypatch):
    monkeypatch.setattr(winsandbox, "_TEST_SECONDS", 0.2)
    probing.exit_on_start = None
    passed, reason = _PROBE()
    assert passed is False
    assert "didn't finish within 0.2 seconds" in reason
    assert probing.calls_to("TerminateJobObject")  # and it was ended
    _assert_clean(probing)


def test_the_test_run_fails_when_the_sandbox_cannot_be_set_up(probing):
    probing.fail["CreateJobObjectW"] = (5, None)
    passed, reason = _PROBE()
    assert passed is False
    assert "CreateJobObjectW failed (Windows error 5" in reason
    _assert_clean(probing)


def test_off_windows_there_is_no_sandbox_and_the_reason_says_so(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert _PROBE() == (False, "Commands run without a sandbox because this isn't Windows")
    assert winsandbox.available() is False


def test_mark_broken_takes_the_sandbox_away_for_good(windows):
    assert winsandbox.available() is True
    winsandbox.mark_broken(winsandbox.SetupError("boom"))
    assert winsandbox.available() is False
    assert winsandbox.unavailable_reason() == ("Commands run without a sandbox because the Windows sandbox "
                                               "couldn't be set up: boom")


# ── sandbox.py: the kind of sandbox, and what a result says ──────────────────

def test_a_result_says_how_it_was_sandboxed():
    full = CommandResult(["x"], 0, "", False, True)
    assert (full.sandbox, full.sandboxed, full.started) == ("full", True, True)
    none = CommandResult(["x"], 0, "", False, False)
    assert (none.sandbox, none.sandboxed) == ("none", False)
    partial = CommandResult(["x"], 0, "", False, False, sandbox="writes-only")
    assert (partial.sandbox, partial.sandboxed) == ("writes-only", False)
    # sandboxed is True only for "full", whatever else was given
    assert CommandResult(["x"], 0, "", False, True, sandbox="writes-only").sandboxed is False
    assert CommandResult(["x"], 0, "", False, True, sandbox="none").sandboxed is False
    assert CommandResult(["x"], 0, "", False, False, sandbox="full").sandboxed is True
    with pytest.raises(ValueError):
        CommandResult(["x"], 0, "", False, False, sandbox="partly")


def test_windows_with_a_working_sandbox_is_sandboxed_for_writes_only(windows):
    assert sandbox.sandbox_kind() == "writes-only"
    assert sandbox.can_sandbox() is False  # "sandboxed" stays the full kind: no network, as on Mac and Linux
    assert sandbox.sandbox_unavailable_reason() == winsandbox.WRITES_ONLY_NOTE
    assert sandbox.sandboxed_argv(["tool", "-q"], Path("."), network=True) == ["tool", "-q"]  # add-ons aren't


def test_the_note_on_windows_says_the_network_is_on_and_reads_are_not_limited():
    note = winsandbox.WRITES_ONLY_NOTE
    for part in ("only partly sandboxed", "write only in the workspace and a private temp folder",
                 "the network is on", "reads aren't limited", "any file your account can",
                 "SSH, cloud and browser folders"):
        assert part in note, part


def test_windows_without_a_working_sandbox_says_why_not(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(winsandbox, "_probe", lambda: (False, "Commands run without a sandbox because of a test"))
    assert sandbox.sandbox_kind() == "none"
    assert sandbox.sandbox_unavailable_reason() == "Commands run without a sandbox because of a test"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_other_systems_never_ask_the_windows_sandbox(monkeypatch, platform):
    def asked() -> bool:
        raise AssertionError("the Windows sandbox was asked")

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sandbox, "can_sandbox", lambda: False)
    monkeypatch.setattr(winsandbox, "available", asked)
    assert sandbox.sandbox_kind() == "none"
    monkeypatch.setattr(sandbox, "can_sandbox", lambda: True)
    assert sandbox.sandbox_kind() == "full"


# ── run_command with the Windows sandbox ─────────────────────────────────────

def test_run_command_on_windows_runs_at_low_integrity_and_reports_writes_only(win32, windows, work):
    folder = os.path.realpath(work)
    win32.output = b"hello\r\n"
    result = sandbox.run_command(["tool", "-q"], work)
    assert result.sandbox == "writes-only"
    assert result.sandboxed is False
    assert result.started is True
    assert result.returncode == 0
    assert result.output == "hello\r\n"
    assert (result.timed_out, result.cancelled) == (False, False)
    assert win32.started.command == "tool -q"
    assert win32.started.directory == folder
    assert win32.levels  # a token whose level was set
    _assert_clean(win32, folder)


def test_run_command_gives_the_commands_own_exit_code(win32, windows, work):
    win32.exit_on_start = 3
    result = sandbox.run_command(["tool"], work)
    assert result.returncode == 3 and result.sandbox == "writes-only"
    _assert_clean(win32, os.path.realpath(work))


def test_run_command_keeps_64_kb_of_the_output_and_counts_the_rest(win32, windows, work):
    win32.output = b"0123456789 abcdef\n" * 4000  # 72000 bytes
    result = sandbox.run_command(["tool"], work)
    marker = re.search(r"\[output cut at 64 KB: (\d+) bytes left out here, of 72000 in all\]", result.output)
    assert marker is not None and int(marker[1]) == 72000 - 64 * 1024
    assert result.output.startswith("0123456789 abcdef\n")
    assert result.output.endswith("0123456789 abcdef\n")
    assert result.sandbox == "writes-only"
    _assert_clean(win32, os.path.realpath(work))


def test_run_command_stops_a_command_at_the_time_limit_with_its_job(win32, windows, work):
    win32.exit_on_start = None
    started = time.monotonic()
    result = sandbox.run_command(["tool"], work, timeout=0.3)
    assert time.monotonic() - started < 10
    assert result.timed_out is True and result.returncode is None
    assert "the time limit" in result.output
    assert result.sandbox == "writes-only"
    assert len(win32.calls_to("TerminateJobObject")) >= 1
    _assert_clean(win32, os.path.realpath(work))


def test_run_command_stops_a_command_when_the_person_presses_stop(win32, windows, work):
    win32.exit_on_start = None
    cancel = threading.Event()
    win32.on_wait = cancel.set  # Stop is pressed while the command runs
    result = sandbox.run_command(["tool"], work, timeout=60, cancel=cancel)
    assert result.cancelled is True and result.timed_out is False
    assert "[stopped by the person]" in result.output
    assert result.sandbox == "writes-only"
    assert len(win32.calls_to("TerminateJobObject")) >= 1
    _assert_clean(win32, os.path.realpath(work))


def test_run_command_after_stop_was_pressed_starts_nothing(win32, windows, work):
    cancel = threading.Event()
    cancel.set()
    result = sandbox.run_command(["tool"], work, cancel=cancel)
    assert result.cancelled is True
    assert result.sandbox == "writes-only"  # what the card said it would be
    assert win32.calls == []


@pytest.mark.parametrize("code, returncode, words", [
    (2, 127, "there's no program 'tool'"),
    (5, 126, "can't be run"),
    (193, 126, "couldn't start"),
])
def test_a_program_that_cannot_run_is_its_own_failure_not_a_broken_sandbox(win32, windows, work, code, returncode,
                                                                           words):
    win32.fail["CreateProcessAsUserW"] = (code, None)
    result = sandbox.run_command(["tool"], work)
    assert result.returncode == returncode
    assert words in result.output
    assert result.sandbox == "writes-only"
    assert winsandbox.available() is True
    _assert_clean(win32, os.path.realpath(work))


def test_a_command_line_that_is_too_long_is_its_own_failure_not_a_broken_sandbox(win32, windows, work):
    result = sandbox.run_command(["tool", "x" * 40000], work)
    assert result.returncode == 126
    assert "too long" in result.output
    assert winsandbox.available() is True
    assert win32.calls == []


def test_a_failed_setup_runs_nothing_and_the_next_command_says_it_is_not_sandboxed(win32, windows, work):
    folder = os.path.realpath(work)
    win32.fail["SetInformationJobObject"] = (5, None)
    result = sandbox.run_command(["tool"], work)
    assert result.started is False
    assert result.returncode == 126
    assert (result.sandbox, result.sandboxed) == ("none", False)
    assert result.output.startswith("[not run: the Windows sandbox couldn't be set up: "
                                    "SetInformationJobObject failed (Windows error 5")
    assert "Nothing was started" in result.output
    assert win32.started is None
    _assert_clean(win32, folder)
    # From now on the card says it isn't sandboxed, and why
    assert winsandbox.available() is False
    assert sandbox.sandbox_kind() == "none"
    reason = sandbox.sandbox_unavailable_reason()
    assert reason == winsandbox.unavailable_reason()
    assert reason.startswith("Commands run without a sandbox because the Windows sandbox couldn't be set up: ")
    assert "SetInformationJobObject" in reason
    # and a command that is approved after that runs as it did before the sandbox, and its result says so
    calls = len(win32.calls)
    result = sandbox.run_command([sys.executable, "-c", "print('ran')"], work)
    assert result.sandbox == "none" and result.sandboxed is False and result.started is True
    assert result.returncode == 0 and "ran" in result.output
    assert len(win32.calls) == calls  # no Win32 function was used for it


def test_computer_access_never_uses_the_windows_sandbox(win32, windows, work):
    result = sandbox.run_command([sys.executable, "-c", "print('ran')"], work, computer=True)
    assert result.sandbox == "none" and result.sandboxed is False
    assert result.returncode == 0 and "ran" in result.output
    assert win32.calls == []


def test_a_sandbox_that_failed_its_test_never_runs_a_command_at_low_integrity(win32, monkeypatch, work):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(winsandbox, "_probe", lambda: (False, "Commands run without a sandbox because of a test"))
    result = sandbox.run_command([sys.executable, "-c", "print('ran')"], work)
    assert result.sandbox == "none" and result.sandboxed is False
    assert result.returncode == 0 and "ran" in result.output
    assert win32.calls == []


# ── The runner: the card's data and the step's result ────────────────────────

def _reply(action: str, **args) -> str:
    return json.dumps({"thought": "Check one thing", "action": action, "args": args})


class ScriptedAsk:
    """Records each prompt and gives the next answer."""

    def __init__(self, *replies: str):
        self.replies = iter(replies)
        self.prompts: list[str] = []

    async def __call__(self, prompt: str) -> tuple[str, dict | None]:
        self.prompts.append(prompt)
        return next(self.replies), None


def _run(tmp_path: Path, ask, computer: bool = False) -> AgentRun:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    cfg = AgentConfig(name="fake", label="Fake model", type="http", model="fake-model",
                      url="https://model.invalid", token="obviously-fake-token")
    return AgentRun(cfg, workspace, "Check the project", AgentStore(tmp_path / "agent"), max_steps=5, ask=ask,
                    computer=computer)


async def _status(run: AgentRun, wanted: str) -> dict:
    async def wait() -> dict:
        while run.status == "running":
            await asyncio.sleep(0)
        assert run.status == wanted, run.to_dict()
        return run.to_dict()

    return await asyncio.wait_for(wait(), timeout=5)


async def _finished(run: AgentRun) -> dict:
    await asyncio.wait_for(run.task, timeout=5)
    return run.to_dict()


def _one_command(tmp_path: Path, monkeypatch, kind: str, result: CommandResult) -> tuple[dict, dict]:
    """A run whose model asks for one command on a computer whose sandbox is `kind`, and which the person
    approves: the page while it waits, and the page after it."""
    monkeypatch.setattr(runner, "sandbox_kind", lambda: kind)
    monkeypatch.setattr(runner, "run_command", lambda argv, folder, **kwargs: result)

    async def scenario():
        ask = ScriptedAsk(_reply("run_command", argv=["tool", "-q"]), _reply("finish", answer="Done"))
        run = _run(tmp_path, ask)
        run.start()
        waiting = await _status(run, "waiting")
        await run.decide(True, always=False)
        return waiting, await _finished(run)

    return asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["full", "writes-only", "none"])
def test_the_card_has_the_kind_of_sandbox_the_command_will_have(tmp_path, monkeypatch, kind):
    result = CommandResult(["tool", "-q"], 0, "listed", False, False, sandbox=kind)
    waiting, _ = _one_command(tmp_path, monkeypatch, kind, result)
    pending = waiting["pending"]
    assert pending["action"] == "run_command"
    assert pending["argv"] == ["tool", "-q"]
    assert pending["sandbox"] == kind
    assert pending["sandboxed"] is (kind == "full")


def test_the_result_of_a_windows_command_says_the_network_was_on_and_reads_were_not_limited(tmp_path, monkeypatch):
    result = CommandResult(["tool", "-q"], 0, "listed", False, False, sandbox="writes-only")
    _, page = _one_command(tmp_path, monkeypatch, "writes-only", result)
    step = page["steps"][0]
    first = step["result"].splitlines()[0]
    assert step["ok"] is True
    assert first.startswith("exit code 0; sandboxed for writes only (Windows)")
    for part in ("write only in the workspace and a private temp folder", "the network was on",
                 "reads weren't limited", "any file your account can", "SSH, cloud and browser folders"):
        assert part in first, part
    assert step["result"].endswith("\nlisted")


@pytest.mark.parametrize("kind, words", [("full", "sandboxed"), ("none", "ran without a sandbox")])
def test_the_result_of_a_command_on_the_other_systems_says_what_it_did(tmp_path, monkeypatch, kind, words):
    result = CommandResult(["tool", "-q"], 0, "listed", False, False, sandbox=kind)
    _, page = _one_command(tmp_path, monkeypatch, kind, result)
    assert page["steps"][0]["result"] == f"exit code 0; {words}\nlisted"


def test_the_result_of_a_command_that_was_never_started_says_not_run(tmp_path, monkeypatch):
    text = "[not run: the Windows sandbox couldn't be set up: boom. Nothing was started.]"
    result = CommandResult(["tool", "-q"], 126, text, False, False, sandbox="none", started=False)
    _, page = _one_command(tmp_path, monkeypatch, "writes-only", result)
    step = page["steps"][0]
    assert step["ok"] is False
    assert step["result"] == f"exit code 126; not run\n{text}"
    assert "ran without a sandbox" not in step["result"]


def test_the_card_of_a_command_with_computer_access_is_never_the_windows_one(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "sandbox_kind", lambda: "writes-only")
    monkeypatch.setattr(runner, "run_command",
                        lambda argv, folder, **kwargs: CommandResult(argv, 0, "", False, False, sandbox="none"))

    async def scenario():
        ask = ScriptedAsk(_reply("run_command", argv=["tool"]), _reply("finish", answer="Done"))
        run = _run(tmp_path, ask, computer=True)
        run.start()
        waiting = await _status(run, "waiting")
        await run.decide(False, always=False)
        await _finished(run)
        return waiting

    pending = asyncio.run(scenario())["pending"]
    assert pending["sandbox"] == "none" and pending["sandboxed"] is False


# ── The approval card (agent.js) ─────────────────────────────────────────────

def test_the_windows_card_says_the_network_is_on_and_reads_are_not_limited():
    code = (STATIC / "agent.js").read_text(encoding="utf-8")
    start = code.index('kind === "writes-only"')
    card = code[start:code.index('class: "agent-sandbox no"', start)]
    assert 'class: "agent-sandbox partial"' in card
    for part in ("Partly sandboxed (Windows)", "it can write only in", "but it has the network",
                 "it can read any file your account can", "including your SSH, cloud and browser folders"):
        assert part in card, part
    # A page saved before the kinds existed still has the true/false "sandboxed"
    assert 'pending.sandboxed ? "full" : "none"' in code
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".agent-sandbox.partial" in css


# ── On Windows: the real sandbox ─────────────────────────────────────────────

def _cmd() -> str:
    return os.environ.get("ComSpec") or str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
                                              / "cmd.exe")


def _script(folder: Path, *lines: str) -> list[str]:
    """A batch file in the folder (its bytes written as they are, with Windows line endings) and the command that
    runs it. cmd.exe reads it in the console's code page, so a path that isn't ASCII skips the test."""
    text = "\r\n".join(["@echo off", *lines, "exit /b 0", ""])
    if not text.isascii():
        pytest.skip("cmd.exe reads batch files in the console's code page: this test keeps to ASCII paths")
    (folder / "run.cmd").write_bytes(text.encode("ascii"))
    return [_cmd(), "/d", "/c", "run.cmd"]


@pytest.fixture
def medium_folder():
    """A new folder in the user's profile, which has the Medium level as the profile's folders do, and that is
    neither the workspace nor the run's temp folder."""
    try:
        folder = Path(tempfile.mkdtemp(prefix="ixel-medium-", dir=str(Path.home())))
    except OSError as exc:
        pytest.skip(f"can't make a folder in the home folder: {exc}")
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@windows_only
def test_windows_the_sandbox_passes_its_own_test():
    assert winsandbox.available(), winsandbox.unavailable_reason()
    assert winsandbox.unavailable_reason() is None
    assert sandbox.sandbox_kind() == "writes-only"
    assert sandbox.sandbox_unavailable_reason() == winsandbox.WRITES_ONLY_NOTE


@windows_only
def test_windows_a_write_in_the_workspace_or_its_private_temp_folder_works(work):
    (work / "existing.txt").write_bytes(b"before\r\n")
    command = _script(work, "echo visible-output", "echo hello>made.txt", "echo appended>>existing.txt",
                      "echo %TEMP% >seen_temp.txt", 'echo temp>"%TEMP%\\inside.txt"',
                      'if exist "%TEMP%\\inside.txt" echo ok>temp_ok.txt')
    result = sandbox.run_command(command, work, timeout=60)
    assert result.sandbox == "writes-only", result.output
    assert result.returncode == 0, result.output
    assert "visible-output" in result.output
    assert (work / "made.txt").read_bytes().strip() == b"hello"
    existing = (work / "existing.txt").read_bytes()
    assert b"before" in existing and b"appended" in existing
    assert (work / "temp_ok.txt").exists()
    seen = Path((work / "seen_temp.txt").read_bytes().decode("ascii").strip())
    assert seen.name.startswith("ixel-agent-")
    assert not seen.exists()  # the private temp folder is gone with the run


@windows_only
def test_windows_a_write_to_a_medium_folder_in_the_home_fails_but_a_read_works(work, medium_folder):
    kept = medium_folder / "kept.txt"
    kept.write_bytes(b"original\r\n")
    (medium_folder / "secret.txt").write_bytes(b"read-me-7f3a91\r\n")
    assert not winsandbox.is_low_label(winsandbox.label_sddl(medium_folder))  # the premise
    command = _script(work, f'echo nope>"{medium_folder}\\denied.txt"', f'echo nope>>"{kept}"',
                      f'type "{medium_folder}\\secret.txt"')
    result = sandbox.run_command(command, work, timeout=60)
    assert result.sandbox == "writes-only", result.output
    assert not (medium_folder / "denied.txt").exists()  # a new file can't be made there
    assert kept.read_bytes() == b"original\r\n"  # and one that is there can't be changed
    assert "read-me-7f3a91" in result.output  # but it can be read: the card says reads aren't limited


@windows_only
def test_windows_the_workspace_is_low_during_the_run_and_has_its_label_back_after(work):
    before = winsandbox.label_sddl(work)
    assert not winsandbox.is_low_label(before)
    command = _script(work, "icacls . >label.txt")  # the command looks at its own folder's label
    result = sandbox.run_command(command, work, timeout=60)
    assert result.returncode == 0, result.output
    during = (work / "label.txt").read_bytes().decode("ascii", errors="replace")
    assert "(NW)" in during, during  # a no-write-up label entry, which only the label has
    assert winsandbox.label_sddl(work) == before
    assert winsandbox._labels == {}


@windows_only
def test_windows_a_workspace_that_is_already_low_stays_low(work):
    winsandbox._set_label(work, winsandbox.LOW_LABEL_SDDL)
    before = winsandbox.label_sddl(work)
    assert winsandbox.is_low_label(before)
    result = sandbox.run_command(_script(work, "echo hi>made.txt"), work, timeout=60)
    assert result.returncode == 0, result.output
    assert (work / "made.txt").exists()
    assert winsandbox.label_sddl(work) == before


@windows_only
def test_windows_the_time_limit_ends_the_command(work):
    started = time.monotonic()
    result = sandbox.run_command([sys.executable, "-c", "import time; time.sleep(60)"], work, timeout=2)
    assert time.monotonic() - started < 30
    assert result.timed_out is True and result.returncode is None
    assert result.sandbox == "writes-only"


@windows_only
def test_windows_stop_ends_the_command(work):
    cancel = threading.Event()
    timer = threading.Timer(1.5, cancel.set)
    timer.start()
    try:
        started = time.monotonic()
        result = sandbox.run_command([sys.executable, "-c", "import time; time.sleep(60)"], work, timeout=60,
                                     cancel=cancel)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 30
    assert result.cancelled is True
    assert result.sandbox == "writes-only"


@windows_only
def test_windows_a_program_that_is_not_there_is_not_a_broken_sandbox(work):
    result = sandbox.run_command(["ixel-no-such-program-7f3a91"], work, timeout=60)
    assert result.returncode == 127
    assert "there's no program" in result.output
    assert result.sandbox == "writes-only"
    assert winsandbox.available() is True
