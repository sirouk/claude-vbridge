"""Native tools intentionally run with the logged-in user's access, not a sandbox."""

from __future__ import annotations

import asyncio
from pathlib import Path

from .processes import close_process, spawn_shell, terminate_tree, wait_exited

MAX_OUTPUT = 64000


async def run_command(command: str, cwd: str | None = None, timeout_s: int = 30) -> dict:
    if not isinstance(command, str) or not command.strip() or len(command) > 32000 or "\x00" in command:
        raise ValueError("command must be non-empty and at most 32000 characters")
    directory = Path(cwd or Path.home()).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError("cwd must be an existing directory")
    spawn = asyncio.create_task(spawn_shell(command, directory))
    proc = None
    reader = None
    chunks = bytearray()
    truncated = False
    timed_out = False

    async def drain():
        nonlocal truncated
        while data := await proc.stdout.read(8192):
            room = MAX_OUTPUT - len(chunks)
            chunks.extend(data[:room])
            truncated = truncated or len(data) > room

    try:
        # Cancellation must not orphan a process created before spawn returns.
        proc = await asyncio.shield(spawn)
        reader = asyncio.create_task(drain())
        try:
            async with asyncio.timeout(max(1, min(timeout_s, 40))):
                await wait_exited(proc)
        except TimeoutError:
            timed_out = True
    finally:
        if proc is None:
            try:
                proc = await asyncio.shield(spawn)
            except (OSError, RuntimeError):
                pass
        if proc is not None:
            if reader is None:
                reader = asyncio.create_task(drain())
            terminate_tree(proc)
            await wait_exited(proc)
        if reader is not None:
            try:
                await asyncio.wait_for(asyncio.shield(reader), 1)
            except (TimeoutError, asyncio.CancelledError):
                reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if proc is not None:
            close_process(proc)
            await proc.wait()
    return {
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "output": chunks.decode(errors="replace"),
        "truncated": truncated,
    }


def read_file(path: str, max_chars: int = 32000) -> dict:
    p = Path(path).expanduser()
    n = max(1, min(max_chars, MAX_OUTPUT))
    with p.open(encoding="utf-8", errors="replace") as f:
        text = f.read(n + 1)
    return {"path": str(p), "text": text[:n], "truncated": len(text) > n}


def write_file(path: str, content: str, overwrite: bool = False) -> dict:
    if len(content.encode()) > 1024 * 1024:
        raise ValueError("content exceeds 1 MiB")
    p = Path(path).expanduser()
    with p.open("w" if overwrite else "x", encoding="utf-8") as f:
        f.write(content)
    return {"path": str(p), "written_chars": len(content)}
