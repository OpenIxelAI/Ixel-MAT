"""
Ending a command-line agent and everything it started.

Killing only the process Ixel launched isn't enough: on Windows an npm-installed
CLI is `cmd.exe` running a shim that starts node, and any CLI may start helpers.
On POSIX the command gets its own process group (start it with SPAWN_OPTIONS);
on Windows it goes into a job object, which also catches descendants whose
parent has already exited.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
from contextlib import suppress

from ixel_mat.agents.launch import NO_WINDOW_FLAGS

logger = logging.getLogger("ixel_mat.agents.process_tree")

# Pass to create_subprocess_exec: the command leads its own process group on
# POSIX.  On Windows, CREATE_SUSPENDED lets us assign the process to its job
# before an npm/Python shim can start children outside that job.
# NO_WINDOW_FLAGS: no console window per call when Ixel itself runs without one.
_CREATE_SUSPENDED = 0x00000004
SPAWN_OPTIONS = ({"start_new_session": True} if os.name == "posix"
                 else {"creationflags": _CREATE_SUSPENDED | NO_WINDOW_FLAGS})


async def _finish_despite_cancellation(task: asyncio.Task):
    """Finish a spawn or cleanup even if its caller is cancelled again."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def create_process_tree(*cmd, **options) -> tuple[asyncio.subprocess.Process, "ProcessTree"]:
    """Start a command and contain it before a cancelled caller can leave it suspended."""
    if os.name != "nt":
        proc = await asyncio.create_subprocess_exec(*cmd, **options)
        return proc, ProcessTree(proc)

    spawning = asyncio.create_task(asyncio.create_subprocess_exec(*cmd, **options))
    try:
        proc = await asyncio.shield(spawning)
    except asyncio.CancelledError:
        # Cancelling the proactor while it connects pipes can strand a suspended
        # Windows child. Let the spawn finish, resume it inside ProcessTree, then
        # kill the whole tree before propagating cancellation.
        proc = await _finish_despite_cancellation(spawning)
        tree = ProcessTree(proc)
        try:
            await _finish_despite_cancellation(asyncio.create_task(tree.kill()))
        finally:
            tree.close()
        raise
    return proc, ProcessTree(proc)


class ProcessTree:
    """A started process and its descendants. Call close() when done with it."""

    def __init__(self, proc: asyncio.subprocess.Process):
        self.proc = proc
        self._job = _job_for(proc.pid) if os.name == "nt" else None
        if os.name == "nt":
            if self._job is None:
                proc.kill()  # It is still suspended; never run without containment.
                raise RuntimeError(f"Could not contain process {proc.pid} in a Windows job object")
            self._descendants: dict[int, int] = {}
            self._watch_lock = threading.Lock()
            self._watch_stop = threading.Event()
            self._watch_ready = threading.Event()
            self._watch_error: Exception | None = None
            self._watcher = threading.Thread(target=self._watch_descendants, daemon=True)
            try:
                # The CPython venv redirector can put the real interpreter in a
                # separate job. Watch process ancestry before the launcher runs.
                self._watcher.start()
                if not self._watch_ready.wait(timeout=2):
                    raise RuntimeError("Could not start Windows process tree watcher")
                if self._watch_error is not None:
                    raise RuntimeError("Windows process tree watcher failed") from self._watch_error
                _resume_process(proc.pid)
            except Exception:
                _kernel32.TerminateJobObject(self._job, 1)
                self.close()
                raise

    def _capture_descendants(self) -> None:
        processes = _process_snapshot()
        with self._watch_lock:
            known = {self.proc.pid, *self._descendants}
            changed = True
            while changed:
                changed = False
                for pid, parent in processes.items():
                    if pid not in known and parent in known:
                        known.add(pid)
                        changed = True
            for pid in known - {self.proc.pid} - self._descendants.keys():
                handle = _kernel32.OpenProcess(_PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION,
                                               False, pid)
                if handle:
                    # Keeping a handle prevents PID reuse after an intermediate exits.
                    self._descendants[pid] = handle

    def _watch_descendants(self) -> None:
        try:
            while not self._watch_stop.is_set():
                try:
                    self._capture_descendants()
                except OSError:
                    logger.warning("Could not inspect Windows process descendants; retrying", exc_info=True)
                    self._watch_stop.wait(0.02)
                    continue
                self._watch_ready.set()
                self._watch_stop.wait(0.02)
        except Exception as exc:
            self._watch_error = exc
            logger.exception("Windows process tree watcher failed")
            self._watch_ready.set()

    def _terminate_descendants(self) -> None:
        with self._watch_lock:
            for handle in self._descendants.values():
                _kernel32.TerminateProcess(handle, 1)

    async def kill(self) -> None:
        if os.name == "posix":
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(self.proc.pid, signal.SIGKILL)
        elif self._job:
            try:
                self._capture_descendants()
            except OSError:
                logger.exception("Could not inspect Windows process descendants before termination")
            self._terminate_descendants()
            _kernel32.TerminateJobObject(self._job, 1)
            try:
                self._capture_descendants()
            except OSError:
                logger.exception("Could not inspect Windows process descendants after termination")
            self._terminate_descendants()
        with suppress(ProcessLookupError):
            if self.proc.returncode is None:
                self.proc.kill()
        with suppress(Exception):
            # Reads what's left too, so the pipes are closed before the caller's event loop is
            await asyncio.wait_for(self.proc.communicate(), timeout=2)

    def close(self) -> None:
        if self._job:
            self._watch_stop.set()
            self._watcher.join(timeout=1)
            try:
                self._capture_descendants()
            except OSError:
                logger.exception("Could not inspect Windows process descendants while closing")
            self._terminate_descendants()
            _kernel32.CloseHandle(self._job)  # KILL_ON_JOB_CLOSE: anything still in it ends
            self._job = None
            with self._watch_lock:
                for handle in self._descendants.values():
                    _kernel32.CloseHandle(handle)
                self._descendants.clear()


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.Thread32First.restype = wintypes.BOOL
    _kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _kernel32.Thread32Next.restype = wintypes.BOOL
    _kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _kernel32.OpenThread.restype = wintypes.HANDLE
    _kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.ResumeThread.restype = wintypes.DWORD
    _kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_SET_QUOTA, _PROCESS_TERMINATE = 0x0100, 0x0001
    _TH32CS_SNAPTHREAD = 0x00000004
    _TH32CS_SNAPPROCESS = 0x00000002
    _THREAD_SUSPEND_RESUME = 0x0002
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                    ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD)]

    class _ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _job_for(pid: int):
    """A job object holding `pid` (and, from now on, whatever it starts); None if that fails."""
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        logger.warning("CreateJobObject failed (error %s)", ctypes.get_last_error())
        return None
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    process = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    ok = bool(process) and _kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
    ) and _kernel32.AssignProcessToJobObject(job, process)
    if process:
        _kernel32.CloseHandle(process)
    if not ok:
        logger.warning("Couldn't put process %s in a job object (error %s)", pid, ctypes.get_last_error())
        _kernel32.CloseHandle(job)
        return None
    return job


def _resume_process(pid: int) -> None:
    """Resume the suspended initial thread after its process is in the job."""
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), "Could not enumerate Windows process threads")
    resumed = False
    try:
        entry = _ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = _kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32OwnerProcessID == pid:
                thread = _kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if not thread:
                    raise OSError(ctypes.get_last_error(), "Could not open suspended process thread")
                try:
                    if _kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                        raise OSError(ctypes.get_last_error(), "Could not resume process thread")
                    resumed = True
                finally:
                    _kernel32.CloseHandle(thread)
            entry.dwSize = ctypes.sizeof(entry)
            found = _kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snapshot)
    if not resumed:
        raise RuntimeError(f"Could not find the suspended thread for process {pid}")


def _process_snapshot() -> dict[int, int]:
    """Return live Windows process IDs and their parent IDs."""
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        raise OSError(ctypes.get_last_error(), "Could not enumerate Windows processes")
    result = {}
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            result[entry.th32ProcessID] = entry.th32ParentProcessID
            entry.dwSize = ctypes.sizeof(entry)
            found = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snapshot)
    return result
