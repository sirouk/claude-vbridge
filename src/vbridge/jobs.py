"""Persistent, inference-free local shell jobs.

JobRunner owns one ``home/jobs`` store and must be closed during application
shutdown. All methods run on one asyncio event loop. Jobs have full local user
access; they are not sandboxed. stdout and stderr share a bounded 64 KiB tail.
Cancellation never replays a command. Windows Job Objects kill descendants;
launch is suspended until assignment. POSIX groups are killed even when
the shell exits first. Children that deliberately create a new session are not
members of that group. A crash marks unfinished records interrupted on restart;
it does not replay commands or signal persisted PIDs (which could be reused).
No commands or output are sent to a logger.
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .job_store import JobStore
from .processes import close_process, spawn_shell, terminate_tree, wait_exited

MAX_CONCURRENCY = 4
MAX_QUEUE = 32
MAX_OUTPUT = 64 * 1024
MAX_TIMEOUT = 3600
MAX_WAIT = 25
TERMINAL_STATES = frozenset({"done", "failed", "cancelled", "timed_out", "interrupted"})


def _valid_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid job id")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValueError("invalid job id") from None
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("invalid job id")
    return value


def _number(value: float, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return float(value)


def _text_tail(data: bytes, limit: int) -> str:
    # Replacement characters can expand invalid UTF-8; bound the *returned*
    # UTF-8 length too, without exposing surrogate characters in JSON.
    if not limit:
        return ""
    return data[-limit:].decode(errors="replace").encode()[-limit:].decode(errors="ignore")


@dataclass
class _Job:
    record: dict
    chunks: deque[bytes] = field(default_factory=deque)
    size: int = 0
    total: int = 0
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    process: asyncio.subprocess.Process | None = None
    stop_state: str = "cancelled"

    def append(self, data: bytes) -> None:
        self.total += len(data)
        self.chunks.append(data)
        self.size += len(data)
        while self.size > MAX_OUTPUT:
            overflow = self.size - MAX_OUTPUT
            first = self.chunks.popleft()
            if len(first) > overflow:
                self.chunks.appendleft(first[overflow:])
                self.size -= overflow
            else:
                self.size -= len(first)

    def output(self, limit: int) -> str:
        return _text_tail(b"".join(self.chunks), limit)


class JobRunner:
    """One persistent local runner; construct with the bridge's state HOME.

    Async API: start(command, cwd=None, timeout_s=3600),
    status(id, wait_s=0, tail_bytes=8192), list_jobs(limit=20), cancel(id),
    shutdown() (also named close()). States: queued, running, done, failed,
    cancelled, timed_out, interrupted. Unknown IDs raise KeyError; invalid
    arguments raise ValueError; full queue or closed runner raises RuntimeError.
    List results omit command, cwd and output. Status returns their contents only
    for the explicitly requested ID. Terminal status includes persisted=True
    unless saving the result failed. Call shutdown before closing the event loop.
    """

    def __init__(self, home: Path | str):
        self._store = JobStore(home)
        self.home = self._store.home
        self.base = self._store.base
        self._jobs: dict[str, _Job] = {}
        self._queue: deque[_Job] = deque()
        self._active: dict[str, asyncio.Task] = {}
        self._closed = False
        self._shutdown_task: asyncio.Task | None = None
        self._storage_failed = False
        try:
            self._load()
        except BaseException:
            self._release()
            raise

    def _release(self) -> None:
        self._store.close()

    def _persist(self, record: dict) -> None:
        self._store.write(record)

    def _load(self) -> None:
        for record in self._store.records():
            job_id = _valid_id(record["id"])
            if record.get("id") != job_id or record.get("state") not in TERMINAL_STATES | {
                "queued",
                "running",
            }:
                raise ValueError("invalid job record")
            job = _Job(record)
            data = record.get("output", "").encode()
            job.append(data[-MAX_OUTPUT:])
            job.total = max(job.size, int(record.get("output_bytes", job.size)))
            if record["state"] not in TERMINAL_STATES:
                record.update(
                    state="interrupted",
                    finished=time.time(),
                    exit_code=None,
                    error="Bridge restarted; command was not replayed.",
                    persisted=True,
                )
                self._persist(record)
            job.done.set()
            self._jobs[job_id] = job

    def _snapshot(self, job: _Job, tail_bytes: int = 8192) -> dict:
        record = dict(job.record)
        record["output"] = job.output(tail_bytes)
        record["output_bytes"] = job.total
        rendered_size = len(b"".join(job.chunks).decode(errors="replace").encode())
        record["output_truncated"] = (
            bool(job.record.get("output_truncated")) or job.total > job.size or rendered_size > MAX_OUTPUT
        )
        record["tail_truncated"] = job.size > tail_bytes or rendered_size > tail_bytes
        return record

    def _find(self, job_id: str) -> _Job:
        job_id = _valid_id(job_id)
        if job_id not in self._jobs:
            raise KeyError("unknown job id")
        return self._jobs[job_id]

    async def start(self, command: str, cwd: str | None = None, timeout_s: float = 3600) -> dict:
        """Persist admission before scheduling; return immediately, never replay."""
        if self._closed or self._storage_failed:
            raise RuntimeError("job runner is closed or storage is unavailable")
        if not isinstance(command, str) or not command.strip() or len(command) > 32000 or "\x00" in command:
            raise ValueError("command must be non-empty, NUL-free and at most 32000 characters")
        timeout_s = _number(timeout_s, "timeout_s", 0.001, MAX_TIMEOUT)
        directory = Path(cwd or Path.home()).expanduser().resolve()
        if not directory.is_dir():
            raise ValueError("cwd must be an existing directory")
        if len(self._active) >= MAX_CONCURRENCY and len(self._queue) >= MAX_QUEUE:
            raise RuntimeError("job queue is full")
        record = {
            "id": str(uuid.uuid4()),
            "state": "queued",
            "command": command,
            "cwd": str(directory),
            "timeout_s": timeout_s,
            "created": time.time(),
            "started": None,
            "finished": None,
            "exit_code": None,
            "error": None,
            "output": "",
            "output_bytes": 0,
            "output_truncated": False,
            "persisted": True,
        }
        self._persist(record)
        job = _Job(record)
        self._jobs[record["id"]] = job
        self._queue.append(job)
        self._dispatch()
        return self._snapshot(job)

    def _dispatch(self) -> None:
        while not self._closed and self._queue and len(self._active) < MAX_CONCURRENCY:
            job = self._queue.popleft()
            task = asyncio.create_task(self._run(job), name=f"vbridge-job-{job.record['id']}")
            self._active[job.record["id"]] = task
            # Consume unexpected errors without any command/output logging.
            task.add_done_callback(lambda task: None if task.cancelled() else task.exception())

    async def status(self, job_id: str, wait_s: float = 0, tail_bytes: int = 8192) -> dict:
        """Wait at most 25s without cancelling execution; return a bounded tail."""
        wait_s = _number(wait_s, "wait_s", 0, MAX_WAIT)
        if (
            isinstance(tail_bytes, bool)
            or not isinstance(tail_bytes, int)
            or not 0 <= tail_bytes <= MAX_OUTPUT
        ):
            raise ValueError(f"tail_bytes must be between 0 and {MAX_OUTPUT}")
        job = self._find(job_id)
        if wait_s and not job.done.is_set():
            try:
                await asyncio.wait_for(job.done.wait(), wait_s)
            except TimeoutError:
                pass
        return self._snapshot(job, tail_bytes)

    async def list_jobs(self, limit: int = 20) -> list[dict]:
        """Newest records first; omit command, directory and captured output."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        keys = ("id", "state", "created", "started", "finished", "exit_code", "timeout_s", "persisted")
        jobs = sorted(self._jobs.values(), key=lambda job: job.record["created"], reverse=True)[:limit]
        return [{key: job.record.get(key) for key in keys} for job in jobs]

    async def cancel(self, job_id: str) -> dict:
        """Stop once and await cleanup. Already-terminal jobs are not mutated."""
        job = self._find(job_id)
        if job.done.is_set():
            return dict(self._snapshot(job), cancellation_requested=False)
        if job in self._queue:
            self._queue.remove(job)
            self._finish(job, "cancelled")
        else:
            # Never cancel the spawn await: a child may already exist before the
            # subprocess handle reaches Python. The worker owns that whole race.
            job.stop.set()
            if job.process is not None:
                self._kill_group(job.process)
            await asyncio.shield(job.done.wait())
        return dict(self._snapshot(job), cancellation_requested=True)

    @staticmethod
    def _kill_group(process) -> None:
        terminate_tree(process)

    async def _drain(self, job: _Job) -> None:
        assert job.process is not None and job.process.stdout is not None
        while data := await job.process.stdout.read(8192):
            job.append(data)

    @staticmethod
    async def _exited(process: asyncio.subprocess.Process) -> None:
        await wait_exited(process)

    def _finish(self, job: _Job, state: str, error: str | None = None) -> None:
        job.record.update(
            state=state,
            finished=time.time(),
            error=error,
            exit_code=job.process.returncode if job.process else None,
            output=job.output(MAX_OUTPUT),
            output_bytes=job.total,
            output_truncated=job.total > job.size
            or len(b"".join(job.chunks).decode(errors="replace").encode()) > MAX_OUTPUT,
            persisted=True,
        )
        try:
            self._persist(job.record)
        except OSError:
            self._storage_failed = True
            job.record.update(
                persisted=False, error="Could not persist job result; command will not be replayed."
            )
        finally:
            job.done.set()

    async def _run(self, job: _Job) -> None:
        reader = exit_wait = stop_wait = spawn = None
        state = "failed"
        error = None
        try:
            if job.stop.is_set() or self._closed:
                state = "interrupted" if self._closed else job.stop_state
                return
            job.record.update(state="running", started=time.time())
            self._persist(job.record)
            deadline = asyncio.get_running_loop().time() + job.record["timeout_s"]
            spawn = asyncio.create_task(spawn_shell(job.record["command"], job.record["cwd"]))
            job.process = await asyncio.shield(spawn)
            reader = asyncio.create_task(self._drain(job))
            exit_wait = asyncio.create_task(self._exited(job.process))
            stop_wait = asyncio.create_task(job.stop.wait())
            await asyncio.wait(
                (exit_wait, stop_wait),
                timeout=max(0, deadline - asyncio.get_running_loop().time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if job.stop.is_set():
                state = job.stop_state
            elif job.process.returncode is None:
                state = "timed_out"
            else:
                state = "done" if job.process.returncode == 0 else "failed"
        except asyncio.CancelledError:
            state = "interrupted"
            error = "Job worker was interrupted; command was not replayed."
            # The owner normally shuts down without task cancellation. Preserve
            # ownership even if this worker is externally cancelled during spawn.
            if spawn is not None and job.process is None:
                try:
                    job.process = await asyncio.shield(spawn)
                except (OSError, asyncio.CancelledError):
                    pass
        except Exception:
            error = "Local process could not be started or monitored."
        finally:
            try:
                if job.process is not None:
                    self._kill_group(job.process)
                    if reader is None:
                        reader = asyncio.create_task(self._drain(job))
                    # Process.wait may also wait for inherited pipes. Observe
                    # shell exit independently, then bound reader cleanup.
                    await self._exited(job.process)
                if reader is not None:
                    try:
                        await asyncio.wait_for(asyncio.shield(reader), 1)
                    except (TimeoutError, asyncio.CancelledError):
                        reader.cancel()
                for task in (reader, exit_wait, stop_wait):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(
                    *(task for task in (reader, exit_wait, stop_wait) if task is not None),
                    return_exceptions=True,
                )
                if job.process is not None:
                    close_process(job.process)
                    await job.process.wait()
            finally:
                self._finish(job, state, error)
                self._active.pop(job.record["id"], None)
                self._dispatch()

    async def shutdown(self) -> None:
        """Cancel queued work, kill all running groups and persist results."""
        if self._shutdown_task is None:
            self._closed = True
            self._shutdown_task = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self) -> None:
        try:
            while self._queue:
                self._finish(self._queue.popleft(), "interrupted", "Bridge shut down before execution.")
            for job_id in list(self._active):
                job = self._jobs[job_id]
                job.stop_state = "interrupted"
                job.stop.set()
                if job.process is not None:
                    self._kill_group(job.process)
            await asyncio.gather(*list(self._active.values()), return_exceptions=True)
        finally:
            self._release()

    async def close(self) -> None:
        await self.shutdown()
