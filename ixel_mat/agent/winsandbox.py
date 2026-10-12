"""
Running an approved command at low integrity on Windows, for the agent's run_command (sandbox.py).

The command gets a copy of Ixel's token with its integrity level lowered to Low (S-1-16-4096) and runs in a
job object that ends it, and everything it started, when the job closes. A Low process can't write to what
is labelled Medium (nearly all of the user profile), so for the run the workspace and a private temp folder
carry a Low label, and its writes can land only there.

That is all it does. The network stays on (blocking it needs administrator rights), and reads aren't
limited: a Low token can still read every file the account can, so the SSH, cloud and browser folders
(secret_folders) can't be hidden here the way they are on Linux. available() and unavailable_reason() say
whether it works on this computer, found out once by running a tiny command through it.

Only ctypes is used, and the module imports on every system: the Win32 functions are loaded by _api() when
they're first needed (tests replace it with a fake).
"""
from __future__ import annotations

import atexit
import ctypes
import errno
import functools
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

logger = logging.getLogger(__name__)

LOW_LABEL_SDDL = "S:(ML;OICI;NW;;;LW)"
LOW_INTEGRITY_SID = "S-1-16-4096"
# What the approval card and the result say about this sandbox, in one place
WRITES_ONLY_NOTE = ("Commands are only partly sandboxed on Windows: they can write only in the workspace and a "
                    "private temp folder, but the network is on, and reads aren't limited: they can read any "
                    "file your account can, including your SSH, cloud and browser folders.")
_NO_SANDBOX = "Commands run without a sandbox because "

_SE_FILE_OBJECT = 1
_LABEL_SECURITY_INFORMATION = 0x10
_SDDL_REVISION_1 = 1
_TOKEN_ASSIGN_PRIMARY, _TOKEN_DUPLICATE, _TOKEN_QUERY, _TOKEN_ADJUST_DEFAULT = 0x1, 0x2, 0x8, 0x80
_SECURITY_IMPERSONATION = 2
_TOKEN_PRIMARY = 1
_TOKEN_INTEGRITY_LEVEL = 25
_SE_GROUP_INTEGRITY = 0x20
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_CREATE_SUSPENDED = 0x00000004
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_NO_WINDOW = 0x08000000
_STARTF_USESTDHANDLES = 0x00000100
_HANDLE_FLAG_INHERIT = 1
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ_WRITE = 0x00000003
_OPEN_EXISTING = 3
_WAIT_OBJECT_0, _WAIT_TIMEOUT, _FAILED, _INFINITE = 0, 0x102, 0xFFFFFFFF, 0xFFFFFFFF
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_MAX_COMMAND_LINE = 32766  # characters CreateProcess accepts, not counting the ending NUL
_TEST_SECONDS = 20.0
# The test command: it prints, appends to a file that was there, makes a new one, and writes into its temp
# folder and (which must be refused) outside the workspace. Run by cmd.exe, so quotes need no care.
_TEST_SCRIPT = (b"@echo off\r\necho ixel-sandbox-ok\r\necho changed>>existing.txt\r\necho new>new.txt\r\n"
                b"echo no>..\\outside\\denied.txt\r\necho temp>\"%TEMP%\\inside.txt\"\r\nexit /b 0\r\n")

# folder -> [runs holding its Low label, its label before ours (None: it was already Low, so never touched)].
# A run that couldn't take the label off again leaves its entry at 0, and the next run to end there retries.
_labels: dict[str, list] = {}
_labels_lock = threading.Lock()
_broken: str | None = None


class SetupError(Exception):
    """The sandbox couldn't be set up for a command, and nothing of the command has started. The message names
    the call that failed and the Windows error number."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_uint32)]


class _TokenMandatoryLabel(ctypes.Structure):
    _fields_ = [("Label", _SidAndAttributes)]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", ctypes.c_uint32), ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", ctypes.c_int)]


class _StartupInfo(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32), ("lpReserved", ctypes.c_void_p), ("lpDesktop", ctypes.c_void_p),
                ("lpTitle", ctypes.c_void_p), ("dwX", ctypes.c_uint32), ("dwY", ctypes.c_uint32),
                ("dwXSize", ctypes.c_uint32), ("dwYSize", ctypes.c_uint32),
                ("dwXCountChars", ctypes.c_uint32), ("dwYCountChars", ctypes.c_uint32),
                ("dwFillAttribute", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                ("wShowWindow", ctypes.c_uint16), ("cbReserved2", ctypes.c_uint16),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", ctypes.c_void_p),
                ("hStdOutput", ctypes.c_void_p), ("hStdError", ctypes.c_void_p)]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p),
                ("dwProcessId", ctypes.c_uint32), ("dwThreadId", ctypes.c_uint32)]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _bind(library, name: str, restype, *argtypes):
    function = getattr(library, name)
    function.restype = restype
    function.argtypes = list(argtypes)
    return function


@functools.cache
def _api() -> SimpleNamespace:
    """The Win32 functions used here (names as in Windows), with their argument and result types set, and
    last_error() for the error of the call that just failed. Handles are plain integers, None when null.
    Windows only: WinDLL is missing elsewhere."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    ptr, dword, number, text, size = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p,
                                      ctypes.c_size_t)

    def k(name, restype, *argtypes):
        return _bind(kernel32, name, restype, *argtypes)

    def a(name, restype, *argtypes):
        return _bind(advapi32, name, restype, *argtypes)

    return SimpleNamespace(
        last_error=ctypes.get_last_error,
        GetCurrentProcess=k("GetCurrentProcess", ptr),
        CloseHandle=k("CloseHandle", number, ptr),
        LocalFree=k("LocalFree", ptr, ptr),
        CreateJobObjectW=k("CreateJobObjectW", ptr, ptr, text),
        SetInformationJobObject=k("SetInformationJobObject", number, ptr, number, ptr, dword),
        AssignProcessToJobObject=k("AssignProcessToJobObject", number, ptr, ptr),
        TerminateJobObject=k("TerminateJobObject", number, ptr, dword),
        TerminateProcess=k("TerminateProcess", number, ptr, dword),
        ResumeThread=k("ResumeThread", dword, ptr),
        CreatePipe=k("CreatePipe", number, ptr, ptr, ptr, dword),
        SetHandleInformation=k("SetHandleInformation", number, ptr, dword, dword),
        CreateFileW=k("CreateFileW", ptr, text, dword, dword, ptr, dword, dword, ptr),
        WaitForSingleObject=k("WaitForSingleObject", dword, ptr, dword),
        GetExitCodeProcess=k("GetExitCodeProcess", number, ptr, ptr),
        InitializeProcThreadAttributeList=k("InitializeProcThreadAttributeList", number, ptr, dword, dword, ptr),
        UpdateProcThreadAttribute=k("UpdateProcThreadAttribute", number, ptr, dword, size, ptr, size, ptr, ptr),
        DeleteProcThreadAttributeList=k("DeleteProcThreadAttributeList", None, ptr),
        OpenProcessToken=a("OpenProcessToken", number, ptr, dword, ptr),
        DuplicateTokenEx=a("DuplicateTokenEx", number, ptr, dword, ptr, number, number, ptr),
        SetTokenInformation=a("SetTokenInformation", number, ptr, number, ptr, dword),
        ConvertStringSidToSidW=a("ConvertStringSidToSidW", number, text, ptr),
        GetLengthSid=a("GetLengthSid", dword, ptr),
        CreateProcessAsUserW=a("CreateProcessAsUserW", number, ptr, text, ptr, ptr, ptr, number, dword, ptr,
                               text, ptr, ptr),
        GetNamedSecurityInfoW=a("GetNamedSecurityInfoW", dword, text, number, dword, ptr, ptr, ptr, ptr, ptr),
        SetNamedSecurityInfoW=a("SetNamedSecurityInfoW", dword, ptr, number, dword, ptr, ptr, ptr, ptr),
        ConvertSecurityDescriptorToStringSecurityDescriptorW=a(
            "ConvertSecurityDescriptorToStringSecurityDescriptorW", number, ptr, dword, dword, ptr, ptr),
        ConvertStringSecurityDescriptorToSecurityDescriptorW=a(
            "ConvertStringSecurityDescriptorToSecurityDescriptorW", number, text, dword, ptr, ptr),
        GetSecurityDescriptorSacl=a("GetSecurityDescriptorSacl", number, ptr, ptr, ptr, ptr),
    )


def command_line(argv: list[str]) -> str:
    """argv as the one command line CreateProcess takes, quoted the way Popen does it."""
    return subprocess.list2cmdline(argv)


def environment_block(env: dict[str, str], temp: str | os.PathLike) -> str:
    """The environment for CREATE_UNICODE_ENVIRONMENT: env with TEMP and TMP (any case) replaced by temp, as
    KEY=value entries sorted by upper-cased key, each ended by a NUL, and one more NUL at the end. ValueError
    when a name or a value holds a NUL, which would cut the block short."""
    temp = os.fspath(temp)
    entries: dict[str, str] = {}
    for key, value in env.items():
        if "\x00" in key or "\x00" in value:
            raise ValueError("an environment variable can't contain a NUL character")
        if key.upper() not in ("TEMP", "TMP"):
            entries[key] = value
    if "\x00" in temp:
        raise ValueError("the temp folder can't contain a NUL character")
    entries["TEMP"] = entries["TMP"] = temp
    ordered = sorted(entries.items(), key=lambda item: item[0].upper())
    return "".join(f"{key}={value}\x00" for key, value in ordered) + "\x00"


def _error_text(code: int) -> str:
    """Windows' words for an error number ('' where they aren't there, like off Windows)."""
    format_error = getattr(ctypes, "FormatError", None)
    if format_error is None:
        return ""
    try:
        return str(format_error(code)).strip().rstrip(".")
    except Exception:
        return ""


def _failure(call: str, code: int) -> SetupError:
    text = _error_text(code)
    return SetupError(f"{call} failed (Windows error {code}{': ' + text if text else ''})", code)


def _failed(api, call: str) -> SetupError:
    """The SetupError for a call that just returned failure; its error is read here, before anything else runs."""
    return _failure(call, api.last_error())


_LABEL_ACE = re.compile(r"\(ML;([A-Za-z]*);([0-9A-Za-z]*);[^;)]*;[^;)]*;([^;)]*)\)")


def _pairs(letters: str) -> set[str]:
    return {letters[i:i + 2] for i in range(0, len(letters), 2)}


def is_low_label(sddl: str) -> bool:
    """Whether an SDDL label string (label_sddl's) is already what we'd set: a Low label with no-write-up that
    new and existing files and folders below get (OICI), set on the folder or inherited."""
    for flags, rights, sid in _LABEL_ACE.findall(sddl):
        if sid.upper() not in ("LW", LOW_INTEGRITY_SID) or not {"OI", "CI"} <= _pairs(flags.upper()):
            continue
        if rights.lower().startswith("0x"):
            no_write_up = int(rights, 16) & 1
        else:
            no_write_up = "NW" in _pairs(rights.upper())
        if no_write_up:
            return True
    return False


def _restorable(saved: str) -> str:
    """The label string to put back: '' (no label of its own) when the saved one was inherited, so what the
    parent has applies again."""
    inherited = [flags for flags, _, _ in _LABEL_ACE.findall(saved) if "ID" in _pairs(flags.upper())]
    return "" if inherited else saved


def label_sddl(path: str | os.PathLike) -> str:
    """A file's or folder's mandatory integrity label as an SDDL string, like 'S:(ML;OICI;NW;;;LW)' ('' when
    it has none, which counts as Medium). SetupError when it can't be read."""
    api = _api()
    descriptor = ctypes.c_void_p()
    code = api.GetNamedSecurityInfoW(os.fspath(path), _SE_FILE_OBJECT, _LABEL_SECURITY_INFORMATION,
                                     None, None, None, None, ctypes.byref(descriptor))
    if code:
        raise _failure("GetNamedSecurityInfoW", code)
    try:
        if not descriptor.value:
            return ""
        string = ctypes.c_void_p()
        if not api.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor.value, _SDDL_REVISION_1, _LABEL_SECURITY_INFORMATION, ctypes.byref(string), None):
            raise _failed(api, "ConvertSecurityDescriptorToStringSecurityDescriptorW")
        try:
            text = ctypes.wstring_at(string.value) if string.value else ""
            # A label list with no entries ('S:', what taking our label off leaves) is no label, like ''
            return text if _LABEL_ACE.search(text) else ""
        finally:
            api.LocalFree(string.value)
    finally:
        api.LocalFree(descriptor.value)


def _set_label(path: str | os.PathLike, sddl: str) -> None:
    """Gives a folder (and what's in it, now and later) the label in sddl; '' takes its own label off."""
    api = _api()
    name = ctypes.create_unicode_buffer(os.fspath(path))
    if not sddl:
        # An ACL with no entries (revision 2, 8 bytes): setting it as the label leaves the folder with none
        empty = (ctypes.c_ubyte * 8)(2, 0, 8, 0, 0, 0, 0, 0)
        code = api.SetNamedSecurityInfoW(name, _SE_FILE_OBJECT, _LABEL_SECURITY_INFORMATION,
                                         None, None, None, empty)
        if code:
            raise _failure("SetNamedSecurityInfoW", code)
        return
    descriptor = ctypes.c_void_p()
    if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, _SDDL_REVISION_1,
                                                                    ctypes.byref(descriptor), None):
        raise _failed(api, "ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        present, sacl, defaulted = ctypes.c_int(), ctypes.c_void_p(), ctypes.c_int()
        if not api.GetSecurityDescriptorSacl(descriptor.value, ctypes.byref(present), ctypes.byref(sacl),
                                             ctypes.byref(defaulted)):
            raise _failed(api, "GetSecurityDescriptorSacl")
        if not present.value or not sacl.value:
            raise SetupError(f"GetSecurityDescriptorSacl found no label in {sddl!r}")
        code = api.SetNamedSecurityInfoW(name, _SE_FILE_OBJECT, _LABEL_SECURITY_INFORMATION,
                                         None, None, None, sacl.value)
        if code:
            raise _failure("SetNamedSecurityInfoW", code)
    finally:
        api.LocalFree(descriptor.value)


def _hold_folder(folder: str) -> None:
    """Gives the workspace the Low label for one more run. The first run labels it (unless it's Low already),
    and the ones that overlap it share the label."""
    with _labels_lock:
        entry = _labels.get(folder)
        if entry is not None:
            entry[0] += 1
            return
        current = label_sddl(folder)
        if is_low_label(current):
            _labels[folder] = [1, None]
            return
        try:
            _set_label(folder, LOW_LABEL_SDDL)
        except BaseException:
            with suppress(Exception):  # it may have reached some of what's inside before it failed
                _set_label(folder, _restorable(current))
            raise
        _labels[folder] = [1, current]


def _release_folder(folder: str) -> None:
    """One run less holding the label; the last one puts it back, if it was us who set it. SetupError when
    that fails (the folder is then still ours to put back at the next release)."""
    with _labels_lock:
        entry = _labels.get(folder)
        if entry is None:
            return
        entry[0] = max(entry[0] - 1, 0)
        if entry[0] > 0:
            return
        if entry[1] is not None:
            _set_label(folder, _restorable(entry[1]))
        del _labels[folder]


@atexit.register
def _release_all() -> None:
    """Ixel quitting during a run: the workspaces we labelled get theirs back (the commands end with Ixel,
    because the job's handle closes with it)."""
    with suppress(Exception):
        for folder in list(_labels):
            with suppress(Exception):
                entry = _labels.pop(folder)
                if entry[1] is not None:
                    _set_label(folder, _restorable(entry[1]))


def _low_token() -> int:
    """A primary token copied from this process's, with its integrity level set to Low. The caller closes it."""
    api = _api()
    access = _TOKEN_DUPLICATE | _TOKEN_ADJUST_DEFAULT | _TOKEN_QUERY | _TOKEN_ASSIGN_PRIMARY
    current = ctypes.c_void_p()
    if not api.OpenProcessToken(api.GetCurrentProcess(), access, ctypes.byref(current)):
        raise _failed(api, "OpenProcessToken")
    try:
        low = ctypes.c_void_p()
        if not api.DuplicateTokenEx(current.value, 0, None, _SECURITY_IMPERSONATION, _TOKEN_PRIMARY,
                                    ctypes.byref(low)):
            raise _failed(api, "DuplicateTokenEx")
    finally:
        api.CloseHandle(current.value)
    try:
        sid = ctypes.c_void_p()
        if not api.ConvertStringSidToSidW(LOW_INTEGRITY_SID, ctypes.byref(sid)):
            raise _failed(api, "ConvertStringSidToSidW")
        try:
            label = _TokenMandatoryLabel()
            label.Label.Sid = sid.value
            label.Label.Attributes = _SE_GROUP_INTEGRITY
            size = ctypes.sizeof(label) + api.GetLengthSid(sid.value)
            if not api.SetTokenInformation(low.value, _TOKEN_INTEGRITY_LEVEL, ctypes.byref(label), size):
                raise _failed(api, "SetTokenInformation")
        finally:
            api.LocalFree(sid.value)
    except BaseException:
        api.CloseHandle(low.value)
        raise
    return low.value


def _new_job() -> int:
    """A job object that ends everything in it when its handle closes. The caller closes it."""
    api = _api()
    job = api.CreateJobObjectW(None, None)
    if not job:
        raise _failed(api, "CreateJobObjectW")
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not api.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits),
                                       ctypes.sizeof(limits)):
        error = _failed(api, "SetInformationJobObject")
        api.CloseHandle(job)
        raise error
    return job


def _inheritable() -> _SecurityAttributes:
    attributes = _SecurityAttributes()
    attributes.nLength = ctypes.sizeof(attributes)
    attributes.bInheritHandle = 1
    return attributes


def _new_pipe() -> tuple[int, int]:
    """(read end, write end) of a pipe: the write end can be inherited by the command, the read end can't."""
    api = _api()
    read, write = ctypes.c_void_p(), ctypes.c_void_p()
    if not api.CreatePipe(ctypes.byref(read), ctypes.byref(write), ctypes.byref(_inheritable()), 0):
        raise _failed(api, "CreatePipe")
    if not api.SetHandleInformation(read.value, _HANDLE_FLAG_INHERIT, 0):
        error = _failed(api, "SetHandleInformation")
        api.CloseHandle(read.value)
        api.CloseHandle(write.value)
        raise error
    return read.value, write.value


def _open_nul() -> int:
    """An inheritable handle to NUL, for the command's input."""
    api = _api()
    handle = api.CreateFileW("NUL", _GENERIC_READ, _FILE_SHARE_READ_WRITE, ctypes.byref(_inheritable()),
                             _OPEN_EXISTING, 0, None)
    if not handle or handle == _INVALID_HANDLE:
        raise _failed(api, "CreateFileW")
    return handle


def _reader(handle: int):
    """The read end of the output pipe as a binary file (it has read1, as Popen's stdout does). It takes the
    handle over, and closes it when that fails."""
    try:
        import msvcrt
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except (ImportError, OSError) as exc:
        _api().CloseHandle(handle)
        raise SetupError(f"open_osfhandle failed ({exc})") from exc
    try:
        return open(fd, "rb")
    except OSError as exc:
        os.close(fd)
        raise SetupError(f"opening the output pipe failed ({exc})") from exc


def _launch_error(api) -> Exception:
    """What a failed CreateProcessAsUserW is: the same errors Popen gives for a program that isn't there or
    can't be run, otherwise a SetupError."""
    code = api.last_error()
    text = _error_text(code) or f"Windows error {code}"
    if code in (2, 3):
        return FileNotFoundError(errno.ENOENT, text)
    if code == 5:
        return PermissionError(errno.EACCES, text)
    if code in (193, 216):
        return OSError(errno.ENOEXEC, text)
    return _failure("CreateProcessAsUserW", code)


def _create_suspended(api, token: int, line: str, block: str, folder: str, stdin: int, stdout: int):
    """CreateProcessAsUserW with the Low token, suspended, with no window, getting only the two handles it is
    given. Without the handle list it would inherit every inheritable handle Ixel holds, and some of those
    can write where a Low process must not. Returns (process, thread, pid)."""
    handles = (ctypes.c_void_p * 2)(stdin, stdout)
    needed = ctypes.c_size_t(0)
    api.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(needed))  # fails on purpose: gives the size
    if not needed.value:
        raise _failed(api, "InitializeProcThreadAttributeList")
    attributes = ctypes.create_string_buffer(needed.value)
    if not api.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(needed)):
        raise _failed(api, "InitializeProcThreadAttributeList")
    try:
        if not api.UpdateProcThreadAttribute(attributes, 0, _PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles,
                                             ctypes.sizeof(handles), None, None):
            raise _failed(api, "UpdateProcThreadAttribute")
        startup = _StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = stdin
        startup.StartupInfo.hStdOutput = stdout
        startup.StartupInfo.hStdError = stdout
        startup.lpAttributeList = ctypes.addressof(attributes)
        data = block.encode("utf-16-le", "surrogatepass")
        environment = (ctypes.c_char * len(data)).from_buffer_copy(data)
        command = ctypes.create_unicode_buffer(line)  # CreateProcess may write into it
        info = _ProcessInformation()
        flags = (_CREATE_SUSPENDED | _CREATE_NO_WINDOW | _CREATE_UNICODE_ENVIRONMENT
                 | _EXTENDED_STARTUPINFO_PRESENT)
        if not api.CreateProcessAsUserW(token, None, command, None, None, 1, flags, environment, folder,
                                        ctypes.byref(startup), ctypes.byref(info)):
            raise _launch_error(api)
    finally:
        api.DeleteProcThreadAttributeList(attributes)
    return info.hProcess, info.hThread, info.dwProcessId


def _end_process(api, process: int) -> None:
    api.TerminateProcess(process, 1)
    api.CloseHandle(process)


class LowProcess:
    """A command started by start(): the part of subprocess.Popen that run_command uses (pid, args, stdout,
    returncode, poll, wait, kill), plus temp, its private temp folder, and close()."""

    def __init__(self, args: list[str], pid: int, process: int, job: int, stdout, temp: Path, folder: str, api):
        self.args = args
        self.pid = pid
        self.stdout = stdout
        self.returncode: int | None = None
        self.temp = temp
        self._process: int | None = process
        self._job: int | None = job
        self._folder = folder
        self._win = api
        self._closed = False
        self._lock = threading.Lock()

    def poll(self) -> int | None:
        """The exit code once the command has ended, None while it runs."""
        if self.returncode is None and self._process is not None:
            state = self._win.WaitForSingleObject(self._process, 0)
            if state == _WAIT_OBJECT_0:
                code = ctypes.c_uint32()
                if not self._win.GetExitCodeProcess(self._process, ctypes.byref(code)):
                    raise OSError(self._win.last_error(), "GetExitCodeProcess failed")
                self.returncode = code.value
            elif state != _WAIT_TIMEOUT:
                raise OSError(self._win.last_error(), "WaitForSingleObject failed")
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        """Waits for the command to end, at most timeout seconds (None: as long as it takes).
        subprocess.TimeoutExpired when it hasn't ended by then."""
        if self.returncode is None and self._process is not None:
            millis = _INFINITE if timeout is None else min(max(math.ceil(timeout * 1000), 0), _INFINITE - 1)
            state = self._win.WaitForSingleObject(self._process, millis)
            if state == _WAIT_TIMEOUT:
                raise subprocess.TimeoutExpired(self.args, timeout)
            if state != _WAIT_OBJECT_0:
                raise OSError(self._win.last_error(), "WaitForSingleObject failed")
            self.poll()
        return self.returncode

    def kill(self) -> None:
        """Ends the command and everything it started (TerminateJobObject)."""
        if self._job is None:
            return
        if not self._win.TerminateJobObject(self._job, 1) and self.poll() is None:
            raise OSError(self._win.last_error(), "TerminateJobObject failed")

    def close(self) -> None:
        """Lets go of the command: anything still running is ended, the temp folder is removed and the
        workspace's label goes back if no other run holds it. Safe to call again, and never raises."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        with suppress(Exception):
            if self.poll() is None:
                self.kill()
                self.wait(timeout=2)
        for handle in (self._process, self._job):  # closing the job ends whatever is still in it
            if handle is not None:
                with suppress(Exception):
                    self._win.CloseHandle(handle)
        self._process = self._job = None
        for _ in range(10):  # programs it started can take a moment to let go of their files
            shutil.rmtree(self.temp, ignore_errors=True)
            with suppress(Exception):
                if not self.temp.exists():
                    break
            time.sleep(0.1)
        with suppress(Exception):
            if self.temp.exists():
                _set_label(self.temp, "")
        try:
            _release_folder(self._folder)
        except Exception as exc:
            logger.warning("Could not take the Low label off %s: %s", self._folder, exc)


def start(argv: list[str], workspace: Path, env: dict[str, str]) -> LowProcess:
    """Starts argv at low integrity in the workspace with env (but TEMP and TMP, which point to a new private
    folder), in a job object, with its input from NUL and its output and errors in one pipe (stdout).

    FileNotFoundError, PermissionError or OSError when the program isn't there or can't be run, as Popen
    gives them. SetupError when the sandbox itself can't be set up: whatever was done by then is undone and
    the command hasn't started. ValueError for a NUL in argv or env."""
    if not argv or any("\x00" in word for word in argv):
        raise ValueError("a command is a non-empty list of text without NUL characters")
    line = command_line(argv)
    if len(line) > _MAX_COMMAND_LINE:
        raise OSError(errno.E2BIG, "the command line is too long")
    api = _api()
    folder = os.path.realpath(workspace)
    if os.path.dirname(folder) == folder:
        raise SetupError("a whole drive can't be the workspace of the Windows sandbox: it would have to label "
                         "every file on it")
    undo: list[tuple] = []  # (function, arguments) taken back, last first, if setting up fails
    scratch: list[int] = []  # handles only this function needs: closed whatever happens
    try:
        temp = tempfile.mkdtemp(prefix="ixel-agent-")
        undo.append((shutil.rmtree, (temp, True)))
        block = environment_block(env, temp)
        _hold_folder(folder)
        undo.append((_release_folder, (folder,)))
        _set_label(temp, LOW_LABEL_SDDL)
        token = _low_token()
        scratch.append(token)
        job = _new_job()
        undo.append((api.CloseHandle, (job,)))
        read, write = _new_pipe()
        scratch.append(write)
        stdout = _reader(read)
        undo.append((stdout.close, ()))
        nul = _open_nul()
        scratch.append(nul)
        process, thread, pid = _create_suspended(api, token, line, block, folder, nul, write)
        scratch.append(thread)
        undo.append((_end_process, (api, process)))
        if not api.AssignProcessToJobObject(job, process):
            raise _failed(api, "AssignProcessToJobObject")
        if api.ResumeThread(thread) == _FAILED:
            raise _failed(api, "ResumeThread")
        return LowProcess(list(argv), pid, process, job, stdout, Path(temp), folder, api)
    except BaseException:
        for function, arguments in reversed(undo):
            with suppress(Exception):
                function(*arguments)
        raise
    finally:
        for handle in scratch:
            with suppress(Exception):
                api.CloseHandle(handle)


def _self_test() -> None:
    """Runs a tiny command through start() and checks what the sandbox promises: it can change a file that
    was in the workspace, make a new one and write in its temp folder; it can't write beside the workspace;
    its output comes back; and the workspace's label is as it was afterwards. SetupError when not."""
    base = Path(tempfile.mkdtemp(prefix="ixel-sandbox-test-"))
    try:
        work, outside = base / "work", base / "outside"
        work.mkdir()
        outside.mkdir()
        (work / "existing.txt").write_bytes(b"before\r\n")
        (work / "test.cmd").write_bytes(_TEST_SCRIPT)
        before = label_sddl(work)
        shell = os.environ.get("ComSpec") or str(Path(os.environ.get("SystemRoot", r"C:\Windows"))
                                                 / "System32" / "cmd.exe")
        proc = start([shell, "/d", "/c", "test.cmd"], work, dict(os.environ))
        try:
            try:
                proc.wait(timeout=_TEST_SECONDS)
            except subprocess.TimeoutExpired:
                with suppress(Exception):
                    proc.kill()
                raise SetupError(f"the test command didn't finish within {_TEST_SECONDS:g} seconds") from None
            output = proc.stdout.read().decode("utf-8", errors="replace")
            in_temp = (proc.temp / "inside.txt").exists()
        finally:
            with suppress(Exception):
                proc.stdout.close()
            proc.close()
        if "ixel-sandbox-ok" not in output:
            raise SetupError("the test command's output didn't come back")
        if b"changed" not in (work / "existing.txt").read_bytes() or not (work / "new.txt").exists():
            raise SetupError("the test command couldn't write in the workspace")
        if not in_temp:
            raise SetupError("the test command couldn't write in its temp folder")
        if (outside / "denied.txt").exists():
            raise SetupError("the test command could write outside the workspace")
        after = label_sddl(work)
        if after != before:
            raise SetupError(f"the workspace's label wasn't put back after the test command ({before!r} "
                             f"before, {after!r} after)")
    finally:
        shutil.rmtree(base, ignore_errors=True)


@functools.cache
def _probe() -> tuple[bool, str | None]:
    """(True, None) when commands can run in this sandbox here, otherwise (False, a sentence saying why).
    Found out once, by the test command."""
    if sys.platform != "win32" or not hasattr(ctypes, "WinDLL"):
        return False, _NO_SANDBOX + "this isn't Windows"
    try:
        _self_test()
    except Exception as exc:
        return False, _NO_SANDBOX + f"the Windows sandbox failed its test run: {exc}"
    return True, None


def available() -> bool:
    """Whether commands can run in this sandbox on this computer (and haven't failed to since)."""
    return _broken is None and _probe()[0]


def unavailable_reason() -> str | None:
    """None when available(), otherwise a sentence starting 'Commands run without a sandbox because'."""
    if _broken is not None:
        return _broken
    return _probe()[1]


def mark_broken(error: object) -> None:
    """Records that the sandbox couldn't be set up during a run (error: the exception or its text), so that
    available() is False from now on and unavailable_reason() says why: the next card says the command
    isn't sandboxed."""
    global _broken
    _broken = _NO_SANDBOX + f"the Windows sandbox couldn't be set up: {error}"
