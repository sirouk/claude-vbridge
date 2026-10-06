"""Local-only jobs regression tests. No bridge, Desktop access or model calls.

Run: .venv/bin/python -m unittest discover -s tests -p test_jobs.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import stat
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from vbridge.jobs import MAX_OUTPUT, JobRunner


def python_command(code: str) -> str:
    if os.name == "nt":

        def quote(value):
            return "'" + value.replace("'", "''") + "'"

        return f"& {quote(sys.executable)} -c {quote(code)}; exit $LASTEXITCODE"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


class JobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve() / "state"
        self.runner = JobRunner(self.home)

    async def asyncTearDown(self):
        await self.runner.close()
        self.temp.cleanup()

    async def wait_file(self, path: Path, timeout: float = 3) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_size > 0:
                return
            await asyncio.sleep(0.01)
        self.fail(f"file did not receive data: {path}")

    async def assert_dead(self, pid: int) -> None:
        if os.name == "nt":
            from vbridge.processes import _windows_api

            ctypes, kernel, _ = _windows_api()
            from ctypes import wintypes

            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE only
            if not handle:
                return
            import _winapi

            try:
                self.assertEqual(_winapi.WaitForSingleObject(int(handle), 3000), 0)
            finally:
                kernel.CloseHandle(handle)
            return
        # Orphan zombies can briefly remain under init; they cannot run.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            probe = await asyncio.create_subprocess_exec(
                "ps", "-o", "stat=", "-p", str(pid), stdout=asyncio.subprocess.PIPE
            )
            stdout, _ = await probe.communicate()
            if not stdout.strip() or stdout.strip().startswith(b"Z"):
                return
            await asyncio.sleep(0.025)
        self.fail(f"process {pid} is still live")

    async def test_success_failure_and_cwd(self):
        record = await self.runner.start(
            python_command("import os; os.write(1,b'hello'); os.write(2,b'error'); print(os.getcwd())"),
            cwd=str(self.home),
        )
        result = await self.runner.status(record["id"], wait_s=3)
        self.assertEqual(result["state"], "done")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("helloerror", result["output"])
        self.assertIn(str(self.home), result["output"])
        record = await self.runner.start(
            python_command("import sys; print('failure',end=''); sys.exit(7)"), cwd=str(self.home)
        )
        result = await self.runner.status(record["id"], wait_s=3)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["output"], "failure")
        stored = json.loads((self.home / "jobs" / f"{record['id']}.json").read_text())
        self.assertEqual(stored["state"], "failed")
        self.assertEqual(stored["output"], "failure")

    async def test_status_wait_is_not_execution_timeout(self):
        record = await self.runner.start(
            python_command("import time; time.sleep(.15); print('finished')"), cwd=str(self.home)
        )
        started = time.monotonic()
        result = await self.runner.status(record["id"], wait_s=0.02)
        self.assertLess(time.monotonic() - started, 0.12)
        self.assertIn(result["state"], ("queued", "running"))
        result = await self.runner.status(record["id"], wait_s=3)
        self.assertEqual(result["state"], "done")
        self.assertIn("finished", result["output"])

    async def test_output_tail_is_bounded_and_keeps_latest(self):
        command = python_command("import os; os.write(1,b'x'*200000); os.write(2,b'LATEST')")
        record = await self.runner.start(command, cwd=str(self.home))
        result = await self.runner.status(record["id"], wait_s=3, tail_bytes=MAX_OUTPUT)
        self.assertEqual(result["state"], "done")
        self.assertEqual(len(result["output"].encode()), MAX_OUTPUT)
        self.assertTrue(result["output"].endswith("LATEST"))
        self.assertTrue(result["output_truncated"])
        self.assertEqual(result["output_bytes"], 200006)
        small = await self.runner.status(record["id"], tail_bytes=10)
        self.assertEqual(len(small["output"].encode()), 10)
        self.assertTrue(small["tail_truncated"])
        self.assertEqual((await self.runner.status(record["id"], tail_bytes=0))["output"], "")
        stored = json.loads((self.home / "jobs" / f"{record['id']}.json").read_text())
        self.assertLessEqual(len(stored["output"].encode()), MAX_OUTPUT)
        self.assertTrue(stored["output"].endswith("LATEST"))

    @unittest.skipIf(os.name == "nt", "PowerShell native stdout encoding differs from POSIX byte streams")
    async def test_invalid_utf8_expansion_reports_truncation(self):
        record = await self.runner.start(
            python_command("import os; os.write(1,b'\\xff'*30000)"), cwd=str(self.home)
        )
        result = await self.runner.status(record["id"], wait_s=3, tail_bytes=MAX_OUTPUT)
        self.assertEqual(result["state"], "done")
        self.assertTrue(result["output_truncated"])
        self.assertTrue(result["tail_truncated"])
        self.assertLessEqual(len(result["output"].encode()), MAX_OUTPUT)

    @unittest.skipIf(os.name == "nt", "PowerShell native stdout encoding differs from POSIX byte streams")
    async def test_live_output_and_invalid_utf8_are_bounded(self):
        record = await self.runner.start(
            python_command("import os,time; os.write(1,b'\\xff'*90000+b'LIVE'); time.sleep(.3)"),
            cwd=str(self.home),
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = await self.runner.status(record["id"], tail_bytes=31)
            if result["output"].endswith("LIVE"):
                break
            await asyncio.sleep(0.01)
        self.assertLessEqual(len(result["output"].encode()), 31)
        self.assertTrue(result["output"].endswith("LIVE"))
        self.assertEqual(result["state"], "running")
        self.assertTrue(result["output_truncated"])
        result = await self.runner.status(record["id"], wait_s=3, tail_bytes=MAX_OUTPUT)
        self.assertLessEqual(len(result["output"].encode()), MAX_OUTPUT)

    async def test_capacity_queue_and_queued_cancel(self):
        records = [
            await self.runner.start(python_command("import time; time.sleep(30)"), cwd=str(self.home))
            for _ in range(36)
        ]
        self.assertEqual(len(self.runner._active), 4)
        self.assertEqual(len(self.runner._queue), 32)
        with self.assertRaises(RuntimeError):
            await self.runner.start(python_command("print('never')"), cwd=str(self.home))
        cancelled = await self.runner.cancel(records[-1]["id"])
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertIsNone(cancelled["started"])
        await self.runner.start(python_command("print('replacement')"), cwd=str(self.home))
        for record in records[:4]:
            await self.runner.cancel(record["id"])
        self.assertLessEqual(len(self.runner._active), 4)
        self.assertLessEqual(len(self.runner._queue), 32)

    async def test_running_cancel_and_retry_are_truthful(self):
        marker = self.home / "counter"
        command = python_command(f"import time; open({str(marker)!r},'a').write('x'); time.sleep(30)")
        record = await self.runner.start(command, cwd=str(self.home))
        await self.wait_file(marker)
        result = await self.runner.cancel(record["id"])
        self.assertEqual(result["state"], "cancelled")
        self.assertTrue(result["cancellation_requested"])
        path = self.home / "jobs" / f"{record['id']}.json"
        data, mtime = path.read_bytes(), path.stat().st_mtime_ns
        retried = await self.runner.cancel(record["id"])
        self.assertEqual(retried["state"], "cancelled")
        self.assertFalse(retried["cancellation_requested"])
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertEqual(marker.read_text(), "x")

    async def test_completed_cancel_does_not_mutate(self):
        record = await self.runner.start(python_command("print('done',end='')"), cwd=str(self.home))
        before = await self.runner.status(record["id"], wait_s=3)
        after = await self.runner.cancel(record["id"])
        self.assertEqual(after["state"], "done")
        self.assertFalse(after.pop("cancellation_requested"))
        self.assertEqual(before, after)

    async def test_timeout_kills_group_with_term_ignoring_child(self):
        pids = self.home / "pids"
        command = python_command(
            "import os,time,subprocess,sys,signal; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            f"open({str(pids)!r},'w').write(str(os.getpid())+' '+str(child.pid)); "
            "time.sleep(30)"
        )
        record = await self.runner.start(command, cwd=str(self.home), timeout_s=0.3)
        await self.wait_file(pids)
        parent, child = map(int, pids.read_text().split())
        result = await self.runner.status(record["id"], wait_s=3)
        self.assertEqual(result["state"], "timed_out")
        await self.assert_dead(parent)
        await self.assert_dead(child)

    async def test_cancel_and_shutdown_kill_groups(self):
        for action in ("cancel", "shutdown"):
            pids = self.home / action
            command = python_command(
                "import os,time,subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                f"open({str(pids)!r},'w').write(str(os.getpid())+' '+str(child.pid)); time.sleep(30)"
            )
            record = await self.runner.start(command, cwd=str(self.home))
            await self.wait_file(pids)
            parent, child = map(int, pids.read_text().split())
            if action == "cancel":
                result = await self.runner.cancel(record["id"])
                self.assertEqual(result["state"], "cancelled")
            else:
                await self.runner.close()
                result = await self.runner.status(record["id"])
                self.assertEqual(result["state"], "interrupted")
            await self.assert_dead(parent)
            await self.assert_dead(child)

    async def test_shell_exit_cleans_background_child_and_inherited_pipe(self):
        marker = self.home / "background_pid"
        command = python_command(
            "import subprocess,sys; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            f"open({str(marker)!r},'w').write(str(child.pid))"
        )
        record = await self.runner.start(command, cwd=str(self.home))
        await self.wait_file(marker)
        result = await self.runner.status(record["id"], wait_s=3)
        self.assertEqual(result["state"], "done")
        await self.assert_dead(int(marker.read_text()))

    async def test_cancel_during_spawn_cannot_orphan_process(self):
        from vbridge.processes import spawn_shell

        real_spawn = spawn_shell
        spawned = asyncio.Event()
        release = asyncio.Event()
        processes = []

        async def delayed_spawn(*args, **kwargs):
            process = await real_spawn(*args, **kwargs)
            processes.append(process)
            spawned.set()
            await release.wait()
            return process

        with patch("vbridge.jobs.spawn_shell", delayed_spawn):
            record = await self.runner.start(
                python_command("import time; time.sleep(30)"), cwd=str(self.home)
            )
            await asyncio.wait_for(spawned.wait(), 3)
            cancellation = asyncio.create_task(self.runner.cancel(record["id"]))
            await asyncio.sleep(0.02)
            self.assertFalse(cancellation.done())
            release.set()
            result = await asyncio.wait_for(cancellation, 3)
        self.assertEqual(result["state"], "cancelled")
        self.assertIsNotNone(processes[0].returncode)
        await self.assert_dead(processes[0].pid)

    async def test_shutdown_during_spawn_and_queued_work(self):
        from vbridge.processes import spawn_shell

        real_spawn = spawn_shell
        spawned = asyncio.Event()
        release = asyncio.Event()
        processes = []

        async def delayed_spawn(*args, **kwargs):
            process = await real_spawn(*args, **kwargs)
            processes.append(process)
            spawned.set()
            await release.wait()
            return process

        with patch("vbridge.jobs.spawn_shell", delayed_spawn):
            records = [
                await self.runner.start(python_command("import time; time.sleep(30)"), cwd=str(self.home))
                for _ in range(6)
            ]
            await asyncio.wait_for(spawned.wait(), 3)
            closure = asyncio.create_task(self.runner.close())
            await asyncio.sleep(0.02)
            release.set()
            await asyncio.wait_for(closure, 3)
        for record in records:
            self.assertEqual((await self.runner.status(record["id"]))["state"], "interrupted")
        for process in processes:
            await self.assert_dead(process.pid)
        with self.assertRaises(RuntimeError):
            await self.runner.start(python_command("print('cannot')"), cwd=str(self.home))
        await self.runner.close()

    async def test_permissions_ownership_and_restart_without_replay(self):
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((self.home / "jobs").stat().st_mode), 0o700)
        else:
            from vbridge.job_store import ensure_private_dir

            ensure_private_dir(self.home)
            ensure_private_dir(self.home / "jobs")
        with self.assertRaises(RuntimeError):
            JobRunner(self.home)
        marker = self.home / "once"
        record = await self.runner.start(
            python_command(f"open({str(marker)!r},'a').write('x')"), cwd=str(self.home)
        )
        completed = await self.runner.status(record["id"], wait_s=3)
        for path in (self.home / "jobs").iterdir():
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            else:
                from vbridge.job_store import check_private_file

                check_private_file(path)
        await self.runner.close()
        self.runner = JobRunner(self.home)
        restored = await self.runner.status(record["id"], tail_bytes=MAX_OUTPUT)
        self.assertEqual(restored["state"], completed["state"])
        self.assertEqual(marker.read_text(), "x")
        await self.runner.close()
        for state in ("queued", "running"):
            job_id = str(uuid.uuid4())
            fixture = dict(
                completed,
                id=job_id,
                state=state,
                command=python_command(f"open({str(marker)!r},'a').write('replay')"),
            )
            path = self.home / "jobs" / f"{job_id}.json"
            path.write_text(json.dumps(fixture))
        self.runner = JobRunner(self.home)
        listed = await self.runner.list_jobs()
        self.assertEqual(sum(row["state"] == "interrupted" for row in listed), 2)
        self.assertEqual(marker.read_text(), "x")
        self.assertEqual(len(self.runner._active), 0)
        for row in listed:
            self.assertNotIn("command", row)
            self.assertNotIn("output", row)
            self.assertNotIn("cwd", row)

    async def test_input_validation_and_unknown_ids(self):
        for command in ("", " ", "x" * 32001, "x\x00y", None):
            with self.assertRaises(ValueError):
                await self.runner.start(command, cwd=str(self.home))
        for timeout in (0, -1, 3601, float("inf"), float("nan"), True, "5"):
            with self.assertRaises(ValueError):
                await self.runner.start(python_command("pass"), cwd=str(self.home), timeout_s=timeout)
        with self.assertRaises(ValueError):
            await self.runner.start(python_command("pass"), cwd=str(self.home / "absent"))
        record = await self.runner.start(python_command("pass"), cwd=str(self.home), timeout_s=3600)
        for wait in (-1, 26, float("nan"), True):
            with self.assertRaises(ValueError):
                await self.runner.status(record["id"], wait_s=wait)
        for limit in (-1, MAX_OUTPUT + 1, True, 1.5):
            with self.assertRaises(ValueError):
                await self.runner.status(record["id"], tail_bytes=limit)
        for job_id in ("../secret", "", str(uuid.uuid1()), str(uuid.uuid4()).upper()):
            with self.assertRaises(ValueError):
                await self.runner.status(job_id)
            with self.assertRaises(ValueError):
                await self.runner.cancel(job_id)
        with self.assertRaises(KeyError):
            await self.runner.status(str(uuid.uuid4()))
        with self.assertRaises(KeyError):
            await self.runner.cancel(str(uuid.uuid4()))

    async def test_spawn_error_is_persisted_without_private_error(self):
        with patch("vbridge.jobs.spawn_shell", side_effect=OSError("PRIVATE")):
            record = await self.runner.start(python_command("pass"), cwd=str(self.home))
            result = await self.runner.status(record["id"], wait_s=3)
        self.assertEqual(result["state"], "failed")
        self.assertNotIn("PRIVATE", result["error"])
        stored = json.loads((self.home / "jobs" / f"{record['id']}.json").read_text())
        self.assertEqual(stored["state"], "failed")

    async def test_immediate_shutdown_marks_unstarted_workers_interrupted(self):
        records = [
            await self.runner.start(python_command("import time; time.sleep(30)"), cwd=str(self.home))
            for _ in range(6)
        ]
        await self.runner.close()
        for record in records:
            self.assertEqual((await self.runner.status(record["id"]))["state"], "interrupted")

    async def test_client_cancellation_does_not_cancel_job(self):
        record = await self.runner.start(
            python_command("import time; time.sleep(.15); print('safe')"), cwd=str(self.home)
        )
        waiter = asyncio.create_task(self.runner.status(record["id"], wait_s=3))
        await asyncio.sleep(0.02)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        result = await self.runner.status(record["id"], wait_s=3)
        self.assertEqual(result["state"], "done")
        self.assertIn("safe", result["output"])

    async def test_finish_persistence_error_is_truthful_and_blocks_admission(self):
        record = await self.runner.start(python_command("import time; time.sleep(.1)"), cwd=str(self.home))
        await asyncio.sleep(0.03)
        with patch.object(self.runner, "_persist", side_effect=OSError("disk full")):
            result = await self.runner.status(record["id"], wait_s=3)
        self.assertEqual(result["state"], "done")
        self.assertFalse(result["persisted"])
        with self.assertRaises(RuntimeError):
            await self.runner.start(python_command("pass"), cwd=str(self.home))

    @unittest.skipIf(
        os.name == "nt",
        "Windows symlink creation needs Developer Mode; native reparse checks covered separately",
    )
    async def test_symlink_state_paths_and_records_fail_closed(self):
        linked = Path(self.temp.name) / "linked"
        linked.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(ValueError):
            JobRunner(linked)
        await self.runner.close()
        record_id = str(uuid.uuid4())
        external = Path(self.temp.name) / "external"
        external.write_text("{}")
        (self.home / "jobs" / f"{record_id}.json").symlink_to(external)
        with self.assertRaises(OSError):
            JobRunner(self.home)
        self.assertEqual(external.read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
