"""Native command tests use only disposable local Python programs."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_processes import python_command

from vbridge.native import MAX_OUTPUT, read_file, run_command, write_file
from vbridge.processes import spawn_shell


class NativeTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_failure_output_and_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            result = await run_command(
                python_command("import os; print('native'); print(os.getcwd())"), directory
            )
            self.assertEqual(result["exit_code"], 0)
            self.assertFalse(result["timed_out"])
            self.assertIn("native", result["output"])
            self.assertIn(str(Path(directory).resolve()), result["output"])
            result = await run_command(python_command("import sys; sys.exit(7)"), directory)
            self.assertEqual(result["exit_code"], 7)

    async def test_timeout_and_truncation(self):
        result = await run_command(python_command("import time; time.sleep(30)"), timeout_s=1)
        self.assertTrue(result["timed_out"])
        result = await run_command(python_command("print('x'*200000)"))
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["output"]), MAX_OUTPUT)

    async def test_cancel_during_spawn_owns_child(self):
        spawned = asyncio.Event()
        release = asyncio.Event()
        owners = []

        async def delayed(command, cwd):
            proc = await spawn_shell(command, cwd)
            owners.append(proc)
            spawned.set()
            await release.wait()
            return proc

        with patch("vbridge.native.spawn_shell", delayed):
            task = asyncio.create_task(run_command(python_command("import time; time.sleep(30)")))
            await asyncio.wait_for(spawned.wait(), 5)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        self.assertIsNotNone(owners[0].returncode)

    async def test_validation_and_files(self):
        for command in (None, "", " ", "x\x00y", "x" * 32001):
            with self.assertRaises(ValueError):
                await run_command(command)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample"
            self.assertEqual(write_file(str(path), "hello")["written_chars"], 5)
            with self.assertRaises(FileExistsError):
                write_file(str(path), "other")
            self.assertEqual(read_file(str(path), 3)["text"], "hel")
            self.assertTrue(read_file(str(path), 3)["truncated"])
