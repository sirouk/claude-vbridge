"""Direct local capabilities and long-lived clients for Desktop stdio servers.

No model calls. Each subprocess MCP session lives in one owner task so AnyIO
cancel scopes enter and exit in the same task. Calls are serialized per backend.
Failed calls are never replayed automatically (mutations may have succeeded).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent, Tool

log = logging.getLogger(__name__)


def desktop_servers() -> dict[str, StdioServerParameters]:
    if os.environ.get("VBRIDGE_DESKTOP_DIR"):
        base = Path(os.environ["VBRIDGE_DESKTOP_DIR"]).expanduser()
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Claude"
    else:
        base = Path.home() / "Library/Application Support/Claude"
    found: dict[str, StdioServerParameters] = {}
    config = base / "claude_desktop_config.json"
    if config.exists():
        for name, spec in json.loads(config.read_text()).get("mcpServers", {}).items():
            if spec.get("command"):
                found[name] = StdioServerParameters(
                    command=spec["command"],
                    args=spec.get("args", []),
                    env=spec.get("env"),
                )
    # Use the installed Reminders executable, avoiding npx/network on restarts.
    cached = sorted((Path.home() / ".npm/_npx").glob("*/node_modules/mcp-server-apple-events/dist/index.js"))
    if "apple-reminders" in found and cached:
        spec = found["apple-reminders"]
        if spec.args == ["-y", "mcp-server-apple-events"]:
            found["apple-reminders"] = StdioServerParameters(
                command=shutil.which("node") or "node",
                args=[str(cached[0])],
                env=spec.env,
            )
    extensions = base / "Claude Extensions"
    settings = base / "Claude Extensions Settings"
    for manifest in sorted(extensions.glob("*/manifest.json")):
        enabled_file = settings / (manifest.parent.name + ".json")
        if not enabled_file.exists() or not json.loads(enabled_file.read_text()).get("isEnabled"):
            continue
        data = json.loads(manifest.read_text())
        compatible = data.get("compatibility", {}).get("platforms", [])
        if compatible and sys.platform not in compatible:
            continue
        spec = data.get("server", {}).get("mcp_config", {})
        if not spec.get("command"):
            continue

        def expand(value: str) -> str:
            expanded = value.replace("${__dirname}", str(manifest.parent)).replace(
                "${HOME}", str(Path.home())
            )
            for key in ("APPDATA", "LOCALAPPDATA", "USERPROFILE"):
                expanded = expanded.replace("${" + key + "}", os.environ.get(key, ""))
            if "${" in expanded:
                raise ValueError(
                    "Extension requires unsupported configuration variables; configure it explicitly"
                )
            return expanded

        name = data["name"].lower().replace(" ", "-")
        found[name] = StdioServerParameters(
            command=shutil.which(spec["command"]) or spec["command"],
            args=[expand(v) for v in spec.get("args", [])],
            env={k: expand(v) for k, v in spec.get("env", {}).items()},
            cwd=manifest.parent,
        )
    return found


class DesktopProxy:
    def __init__(self, specs: dict[str, StdioServerParameters] | None = None):
        self.specs = desktop_servers() if specs is None else specs
        self.tools: dict[str, Tool] = {}
        self.routes: dict[str, tuple[str, str]] = {}
        self.errors: dict[str, str] = {}
        self._queues: dict[str, asyncio.Queue] = {}
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        ready = []
        for name, spec in self.specs.items():
            event = asyncio.Event()
            self._queues[name] = asyncio.Queue(maxsize=32)
            self._tasks.append(asyncio.create_task(self._serve(name, spec, event)))
            ready.append(event.wait())
        if ready:
            await asyncio.gather(*ready)

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _serve(self, name, spec, ready) -> None:
        current = None
        try:
            async with AsyncExitStack() as stack:
                # wait_for would enter AnyIO scopes in another task; use timeout instead.
                async with asyncio.timeout(20):
                    rd, wr = await stack.enter_async_context(stdio_client(spec))
                    session = await stack.enter_async_context(ClientSession(rd, wr))
                    await session.initialize()
                    page = await session.list_tools()
                    items = list(page.tools)
                    while page.nextCursor:
                        page = await session.list_tools(cursor=page.nextCursor)
                        items.extend(page.tools)
                prefix = "".join(c if c.isalnum() else "_" for c in name)
                for tool in items:
                    public = f"{prefix}__{tool.name}"
                    self.tools[public] = tool.model_copy(update={"name": public})
                    self.routes[public] = (name, tool.name)
                log.warning("Desktop backend %s ready: %d tools", name, len(items))
                ready.set()
                while True:
                    tool_name, arguments, current = await self._queues[name].get()
                    if current.cancelled():
                        current = None
                        continue
                    try:
                        result = await session.call_tool(
                            tool_name, arguments, read_timeout_seconds=timedelta(seconds=45)
                        )
                        if not current.done():
                            current.set_result(result)
                    except Exception as e:
                        if not current.done():
                            current.set_exception(e)
                    finally:
                        current = None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.errors[name] = f"{type(e).__name__}: {e}"
            log.error("Desktop backend %s unavailable: %s", name, self.errors[name])
        finally:
            ready.set()
            err = RuntimeError(f"{name} disconnected; restart bridge. Calls are not replayed.")
            if current is not None and not current.done():
                current.set_exception(err)
            q = self._queues[name]
            while not q.empty():
                _, _, fut = q.get_nowait()
                if not fut.done():
                    fut.set_exception(err)
            self.errors.setdefault(name, "backend stopped")

    async def call(self, public: str, arguments: dict[str, Any]) -> CallToolResult:
        backend, actual = self.routes[public]
        if backend in self.errors:
            return CallToolResult(isError=True, content=[TextContent(type="text", text=self.errors[backend])])
        fut = asyncio.get_running_loop().create_future()
        try:
            self._queues[backend].put_nowait((actual, arguments, fut))
        except asyncio.QueueFull:
            return CallToolResult(
                isError=True, content=[TextContent(type="text", text="Backend busy; queue full.")]
            )
        try:
            return await fut
        except Exception as e:
            return CallToolResult(
                isError=True,
                content=[
                    TextContent(type="text", text=f"Local tool failed: {e}. Not automatically retried.")
                ],
            )
