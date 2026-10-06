"""Local process ownership, not a sandbox.

POSIX uses a fresh process group (deliberate setsid escapes are outside it).
Windows creates the process suspended, assigns a kill-on-close Job Object, then
resumes its thread. Assignment failures abort without running user code. No
breakaway permission is granted. Closing the Job kills descendants even after
the original shell exits. Windows requires the standard CPython Win32 APIs.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path


def shell_argv(command: str) -> list[str]:
    if os.name == "nt":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            raise RuntimeError("PowerShell (pwsh or powershell.exe) is required")
        # -Command strings are PowerShell programs, not cmd.exe programs.
        return [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
    return ["/bin/zsh" if sys.platform == "darwin" else "/bin/sh", "-c", command]


async def spawn_shell(command: str, cwd: str | Path):
    argv = shell_argv(command)
    if os.name == "nt":
        # Native creation is synchronous and short: no cancellation await occurs
        # between CreateProcess, assignment and resume.
        return _spawn_windows(argv, str(cwd))
    return await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        limit=16384,
    )


def terminate_tree(process) -> None:
    if isinstance(process, _WindowsProcess):
        process.terminate_tree()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def close_process(process) -> None:
    if isinstance(process, _WindowsProcess):
        process.close()
    else:
        # Close inherited PIPEs after the bounded drain. A deliberate POSIX
        # setsid escape must not keep shutdown waiting on the pipe forever.
        transport = getattr(process, "_transport", None)
        if transport is not None:
            transport.close()


async def wait_exited(process) -> None:
    # asyncio's wait() may wait for inherited output PIPEs too. Poll only the
    # root exit; the owner subsequently kills descendants and drains the pipe.
    while process.returncode is None:
        await asyncio.sleep(0.025)


def _windows_api():
    import ctypes
    from ctypes import wintypes as w

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", w.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", w.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", w.DWORD),
            ("SchedulingClass", w.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    specs = {
        "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
        "ResumeThread": ([w.HANDLE], w.DWORD),
        "CloseHandle": ([w.HANDLE], w.BOOL),
    }
    for name, (args, result) in specs.items():
        fn = getattr(kernel, name)
        fn.argtypes, fn.restype = args, result
    return ctypes, kernel, EXTENDED_LIMIT


class _WindowsPipe:
    def __init__(self, fd: int):
        self.fd = fd

    async def read(self, size: int) -> bytes:
        # Ordinary anonymous Win32 pipes are synchronous; keep blocking reads
        # off the event loop. Terminating the Job releases all inherited writers.
        return await asyncio.to_thread(os.read, self.fd, size)

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class _WindowsProcess:
    def __init__(self, handle, pid, job, fd, kernel, winapi):
        self._handle, self.pid, self._job = handle, pid, job
        self.stdout = _WindowsPipe(fd)
        self._kernel, self._winapi = kernel, winapi
        self._returncode = None

    @property
    def returncode(self):
        if self._returncode is None and self._handle is not None:
            # STILL_ACTIVE (259) is also a possible actual exit code. Query the
            # signaled state first so an exit of 259 is not mistaken for alive.
            if self._winapi.WaitForSingleObject(self._handle, 0) == 0:
                self._returncode = self._winapi.GetExitCodeProcess(self._handle)
        return self._returncode

    async def wait(self):
        await wait_exited(self)
        return self.returncode

    def terminate_tree(self):
        if self._job is not None:
            if not self._kernel.CloseHandle(self._job):
                import ctypes

                raise ctypes.WinError(ctypes.get_last_error())
            self._job = None

    def close(self):
        self.terminate_tree()
        self.stdout.close()
        if self._handle is not None:
            self._winapi.CloseHandle(self._handle)
            self._handle = None


def _spawn_windows(argv: list[str], cwd: str):
    import _winapi
    import msvcrt

    ctypes, kernel, limit_type = _windows_api()
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limit = limit_type()
    limit.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    handles = []
    process = thread = read_fd = None
    try:
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limit), ctypes.sizeof(limit)):
            raise ctypes.WinError(ctypes.get_last_error())
        read_handle, write_handle = _winapi.CreatePipe(None, 0)
        handles.extend((read_handle, write_handle))
        stdin_fd = os.open(os.devnull, os.O_RDONLY)
        try:
            stdin_handle = msvcrt.get_osfhandle(stdin_fd)
            os.set_handle_inheritable(write_handle, True)
            os.set_handle_inheritable(stdin_handle, True)
            startup = subprocess.STARTUPINFO()
            startup.dwFlags = subprocess.STARTF_USESTDHANDLES
            startup.hStdInput = stdin_handle
            startup.hStdOutput = startup.hStdError = write_handle
            startup.lpAttributeList = {"handle_list": [stdin_handle, write_handle]}
            process, thread, pid, _ = _winapi.CreateProcess(
                argv[0],
                subprocess.list2cmdline(argv),
                None,
                None,
                True,
                0x00000004 | 0x08000000,  # CREATE_SUSPENDED | CREATE_NO_WINDOW
                None,
                cwd,
                startup,
            )
        finally:
            os.close(stdin_fd)
        if not kernel.AssignProcessToJobObject(job, process):
            raise ctypes.WinError(ctypes.get_last_error())
        read_fd = msvcrt.open_osfhandle(read_handle, os.O_RDONLY | os.O_BINARY)
        handles.remove(read_handle)  # ownership transferred to fd
        if kernel.ResumeThread(thread) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        owner = _WindowsProcess(process, pid, job, read_fd, kernel, _winapi)
        process = read_fd = job = None  # ownership transferred to returned object
        return owner
    except BaseException:
        if process is not None:
            _winapi.TerminateProcess(process, 1)
            _winapi.WaitForSingleObject(process, 5000)
        raise
    finally:
        if thread is not None:
            _winapi.CloseHandle(thread)
        if process is not None:
            _winapi.CloseHandle(process)
        if read_fd is not None:
            os.close(read_fd)
        for handle in handles:
            _winapi.CloseHandle(handle)
        if job is not None:
            kernel.CloseHandle(job)
