"""Local process tests. Real portable commands; no inference or GUI access."""

import asyncio
import base64
import ctypes
import os
import shlex
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from vbridge import processes


def python_command(code):
    if os.name == "nt":

        def quote(value):
            return "'" + value.replace("'", "''") + "'"

        return f"& {quote(sys.executable)} -c {quote(code)}; exit $LASTEXITCODE"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_shell_output(self):
        proc = await processes.spawn_shell(python_command("print('portable')"), Path.cwd())
        try:
            output = await proc.stdout.read(8192)
            await processes.wait_exited(proc)
            self.assertIn(b"portable", output)
            self.assertEqual(proc.returncode, 0)
        finally:
            processes.terminate_tree(proc)
            await proc.wait()
            processes.close_process(proc)

    async def test_shell_selection(self):
        with (
            patch.object(processes.os, "name", "nt"),
            patch.object(processes.shutil, "which", side_effect=[None, "powershell.exe"]),
        ):
            self.assertEqual(
                processes.shell_argv("hello"),
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-EncodedCommand",
                    base64.b64encode("hello".encode("utf-16-le")).decode("ascii"),
                ],
            )
        with (
            patch.object(processes.os, "name", "nt"),
            patch.object(processes.shutil, "which", return_value=None),
        ):
            with self.assertRaises(RuntimeError):
                processes.shell_argv("hello")
        with patch.object(processes.os, "name", "posix"), patch.object(processes.sys, "platform", "darwin"):
            self.assertEqual(processes.shell_argv("hello"), ["/bin/zsh", "-c", "hello"])

    async def test_real_terminate_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "pid"
            proc = await processes.spawn_shell(
                python_command(
                    f"import os,time; open({str(marker)!r},'w').write(str(os.getpid())); time.sleep(30)"
                ),
                directory,
            )
            try:
                async with asyncio.timeout(5):
                    while not marker.exists():
                        await asyncio.sleep(0.01)
                processes.terminate_tree(proc)
                await asyncio.wait_for(proc.wait(), 3)
                self.assertIsNotNone(proc.returncode)
            finally:
                processes.terminate_tree(proc)
                await proc.wait()
                processes.close_process(proc)

    @unittest.skipUnless(os.name == "nt", "requires real Windows Job Objects")
    async def test_windows_exit_code_259_is_terminal(self):
        proc = processes._spawn_windows([sys.executable, "-c", "import sys; sys.exit(259)"], str(Path.cwd()))
        try:
            await asyncio.wait_for(proc.wait(), 5)
            self.assertEqual(proc.returncode, 259)
        finally:
            processes.terminate_tree(proc)
            processes.close_process(proc)

    @unittest.skipUnless(os.name == "nt", "requires real Windows Job Objects")
    async def test_windows_owner_death_kills_job_descendants(self):
        import _winapi
        import subprocess
        from ctypes import wintypes

        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "pids"
            child_code = (
                "import os,subprocess,sys,time; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                f"open({str(marker)!r},'w').write(str(os.getpid())+' '+str(child.pid)); "
                "time.sleep(60)"
            )
            owner_code = (
                "import sys,time; from vbridge.processes import _spawn_windows; "
                f"proc=_spawn_windows([sys.executable,'-c',{child_code!r}],{directory!r}); "
                "time.sleep(60)"
            )
            owner = subprocess.Popen(
                [sys.executable, "-c", owner_code], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
            try:
                async with asyncio.timeout(10):
                    while not marker.exists() or not marker.stat().st_size:
                        if owner.poll() is not None:
                            self.fail(owner.stderr.read().decode(errors="replace"))
                        await asyncio.sleep(0.02)
                pids = [int(pid) for pid in marker.read_text().split()]
                _, kernel, _ = processes._windows_api()
                kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                kernel.OpenProcess.restype = wintypes.HANDLE
                handles = [kernel.OpenProcess(0x100000, False, pid) for pid in pids]
                self.assertTrue(all(handles))
                try:
                    owner.kill()  # abrupt death closes the sole Job handle
                    await asyncio.to_thread(owner.wait, 5)
                    for handle in handles:
                        self.assertEqual(
                            await asyncio.to_thread(_winapi.WaitForSingleObject, int(handle), 5000), 0
                        )
                finally:
                    for handle in handles:
                        if handle:
                            kernel.CloseHandle(handle)
            finally:
                if owner.poll() is None:
                    owner.kill()
                await asyncio.to_thread(owner.wait, 5)
                owner.stderr.close()

    @unittest.skipUnless(os.name == "nt", "requires real Windows suspended processes")
    async def test_windows_assignment_rejection_cleans_real_suspended_process(self):
        import _winapi
        from ctypes import wintypes

        ctypes_api, kernel, limit_type = processes._windows_api()
        kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessHandleCount.restype = wintypes.BOOL
        count = wintypes.DWORD()
        self.assertTrue(kernel.GetProcessHandleCount(_winapi.GetCurrentProcess(), ctypes.byref(count)))
        baseline = count.value
        captured = []

        class RejectAssignment:
            def __getattr__(self, name):
                return getattr(kernel, name)

            def AssignProcessToJobObject(self, job, process):
                captured.append(process)
                ctypes.set_last_error(5)
                return False

        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "must_not_execute"
            with patch.object(
                processes, "_windows_api", return_value=(ctypes_api, RejectAssignment(), limit_type)
            ):
                with self.assertRaises(OSError):
                    processes._spawn_windows(
                        [sys.executable, "-c", f"open({str(marker)!r},'w').write('executed')"], directory
                    )
            self.assertFalse(marker.exists())
            self.assertEqual(len(captured), 1)
            # The owned process HANDLE was closed after terminated suspended
            # process cleanup. It cannot be waited on or signal user code.
            with self.assertRaises(OSError):
                _winapi.WaitForSingleObject(captured[0], 0)
        self.assertTrue(kernel.GetProcessHandleCount(_winapi.GetCurrentProcess(), ctypes.byref(count)))
        self.assertLessEqual(count.value, baseline + 1)


class WindowsLaunchContractTests(unittest.TestCase):
    """Contract tests on any OS; real Windows integration is still required."""

    def run_launch(self, assign=True):
        calls = []

        class Basic(ctypes.Structure):
            _fields_ = [("LimitFlags", ctypes.c_uint32)]

        class Limit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic)]

        class Kernel:
            def CreateJobObjectW(self, *args):
                calls.append("job")
                return 500

            def SetInformationJobObject(self, job, kind, pointer, size):
                self.flags = ctypes.cast(
                    pointer, ctypes.POINTER(Limit)
                ).contents.BasicLimitInformation.LimitFlags
                calls.append("limit")
                return True

            def AssignProcessToJobObject(self, job, process):
                calls.append("assign")
                return assign

            def ResumeThread(self, thread):
                calls.append("resume")
                return 1

            def CloseHandle(self, handle):
                calls.append("job-close")
                return True

        kernel = Kernel()

        def create(*args):
            calls.append("create")
            self.assertEqual(args[5] & 4, 4, "must create suspended")
            self.assertEqual(args[8].lpAttributeList["handle_list"][-1], pipe[1])
            return 100, 101, 102, 103

        pipe = os.pipe()

        def close(handle):
            if handle in pipe:
                os.close(handle)
            calls.append("close")

        api = types.SimpleNamespace(
            CreatePipe=lambda *args: pipe,
            CreateProcess=create,
            CloseHandle=close,
            TerminateProcess=lambda *args: calls.append("terminate"),
            WaitForSingleObject=lambda *args: 0,
        )
        msvcrt = types.SimpleNamespace(get_osfhandle=lambda fd: fd, open_osfhandle=lambda fd, flags: fd)
        with (
            patch.dict(sys.modules, {"_winapi": api, "msvcrt": msvcrt}),
            patch.object(processes, "_windows_api", return_value=(ctypes, kernel, Limit)),
            patch.object(processes.os, "set_handle_inheritable", create=True),
            patch.object(processes.os, "O_BINARY", 0, create=True),
            patch.object(processes.subprocess, "STARTUPINFO", types.SimpleNamespace, create=True),
            patch.object(processes.subprocess, "STARTF_USESTDHANDLES", 256, create=True),
            patch.object(ctypes, "WinError", lambda error: OSError("assignment failure"), create=True),
            patch.object(ctypes, "get_last_error", return_value=5, create=True),
        ):
            if assign:
                proc = processes._spawn_windows(["powershell.exe", "-Command", "pass"], ".")
                proc.stdout.close()
            else:
                with self.assertRaises(OSError):
                    processes._spawn_windows(["powershell.exe", "-Command", "pass"], ".")
        self.assertEqual(kernel.flags, 0x2000)
        return calls

    def test_suspended_assign_before_resume(self):
        calls = self.run_launch()
        self.assertLess(calls.index("create"), calls.index("assign"))
        self.assertLess(calls.index("assign"), calls.index("resume"))

    def test_assignment_failure_never_resumes(self):
        calls = self.run_launch(assign=False)
        self.assertNotIn("resume", calls)
        self.assertIn("terminate", calls)
