"""Direct OAuth-protected Mac capabilities for claude.ai, including voice if supported.

The caller is the model. Tool execution makes no inference calls. Installed
Desktop MCP servers are launched as separate long-lived stdio clients.
"""

from __future__ import annotations

import asyncio
import hmac
import html
import json
import os
import sys
import time

import uvicorn
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ImageContent, TextContent
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from . import desktop as desktop_api
from . import macos, native
from .config import settings
from .job_store import check_private_file, ensure_private_dir, private_write
from .jobs import JobRunner
from .local_tools import DesktopProxy
from .oauth_store import OwnerOAuthProvider
from .queue import TaskQueue

SETTINGS = settings()
HOME = SETTINGS["home"]
ensure_private_dir(HOME)
PUBLIC_URL = SETTINGS["public_url"]
PORT = SETTINGS["port"]
PASS_FILE = HOME / "passphrase"


def _passphrase() -> str:
    if not PASS_FILE.exists():
        import secrets

        private_write(PASS_FILE, secrets.token_urlsafe(18))
    check_private_file(PASS_FILE)
    return PASS_FILE.read_text().strip()


provider = OwnerOAuthProvider(HOME / "oauth.json", PUBLIC_URL)
queue = TaskQueue(HOME / "queue")
host = PUBLIC_URL.split("://", 1)[1].split("/")[0]

desktop = DesktopProxy(None if SETTINGS["desktop_enabled"] else {})
jobs = JobRunner(HOME)


mcp = FastMCP(
    "vbridge",
    instructions=(
        "Direct access to the owner's computer. Use desktop tools and run_command/read_file/write_file "
        "directly; no additional model or API key is needed. run_command can use osascript for apps. "
        "These tools have the owner's full user access, not a sandbox. Confirm destructive or "
        "externally visible actions with the user. For long commands use start_job, then job_status; "
        "cancel_job stops a process but cannot undo effects. Screenshots and clipboard contents "
        "are private data sent to this conversation: only access when requested. Use screenshot "
        "to observe before/after UI actions; use screenshot coordinate mapping, not resized image pixels. "
        "Avoid claiming success from merely posting input. Keep spoken replies short."
    ),
    auth_server_provider=provider,
    auth=AuthSettings(
        issuer_url=PUBLIC_URL,
        resource_server_url=f"{PUBLIC_URL}/mcp",
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=["bridge"], default_scopes=["bridge"]
        ),
        required_scopes=["bridge"],
        validate_token_resource=True,
        revocation_options=RevocationOptions(enabled=True),
    ),
    host="127.0.0.1",
    port=PORT,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, host.split(":")[0], f"127.0.0.1:{PORT}", f"localhost:{PORT}"],
        allowed_origins=[PUBLIC_URL, "https://claude.ai", "https://claude.com"],
    ),
)


@mcp.tool()
def ping() -> str:
    """Check the bridge is alive. Returns server time."""
    return f"pong {time.strftime('%H:%M:%S')}"


async def _wait(task_id: str, wait_s: int) -> dict:
    deadline = time.monotonic() + max(0, min(wait_s, 40))
    while True:
        rec = queue.get(task_id)
        if rec is None:
            return {"error": "unknown task id"}
        if rec["state"] in ("done", "failed") or time.monotonic() >= deadline:
            return {"id": rec["id"], "state": rec["state"], "result": rec.get("result")}
        await asyncio.sleep(0.25)


@mcp.tool()
async def queue_task(prompt: str, project: str | None = None, wait_s: int = 25) -> dict:
    """Hand a task to Claude Code on the owner's computer (full access: shell, files, apps).
    Waits up to wait_s seconds (max 40) and returns the result if finished; otherwise
    returns state 'pending'/'running' and an id to pass to task_status."""
    rec = queue.add(prompt, project)
    return await _wait(rec["id"], wait_s)


@mcp.tool()
async def task_status(task_id: str, wait_s: int = 25) -> dict:
    """Get state and result of a task. Waits up to wait_s seconds (max 40) for it to finish."""
    return await _wait(task_id, wait_s)


@mcp.tool()
def recent_tasks(limit: int = 5) -> list[dict]:
    """List the most recent tasks, newest first."""
    return queue.list(max(1, min(limit, 20)))


@mcp.tool()
async def run_command(command: str, cwd: str | None = None, timeout_s: int = 30) -> dict:
    """Run a shell command on this computer with full user access: zsh on Mac, PowerShell on Windows.
    No inference call. Max duration 40 seconds. Use platform-appropriate commands.
    Confirm destructive actions and messages with the owner before calling."""
    return await native.run_command(command, cwd, timeout_s)


@mcp.tool()
async def read_file(path: str, max_chars: int = 32000) -> dict:
    """Read a UTF-8 file directly from the computer. No model call. Full user access."""
    return await asyncio.to_thread(native.read_file, path, max_chars)


@mcp.tool()
async def write_file(path: str, content: str, overwrite: bool = False) -> dict:
    """Write a local file. Existing files require overwrite=true. Full user access."""
    return await asyncio.to_thread(native.write_file, path, content, overwrite)


@mcp.tool()
def bridge_status() -> dict:
    """Show direct local backends, tool counts and connection errors (no private app data)."""
    return {
        "inference_required": False,
        "desktop_backends": list(desktop.specs),
        "desktop_tools": len(desktop.tools),
        "backend_errors": dict(desktop.errors),
        "ui_enabled": SETTINGS["ui_enabled"],
        "jobs_enabled": True,
        "platform": sys.platform,
        "voice_routing": "depends on the Claude client; not guaranteed",
    }


@mcp.tool()
async def start_job(command: str, cwd: str | None = None, timeout_s: int = 3600) -> dict:
    """Start a long local shell job and return an id immediately. No model inference.
    Full user authority; confirm destructive actions. Use job_status for bounded output."""
    return await jobs.start(command, cwd, timeout_s)


@mcp.tool()
async def job_status(job_id: str, wait_s: int = 0, tail_bytes: int = 8192) -> dict:
    """Get a local job's state and recent output; optionally wait up to25 seconds."""
    return await jobs.status(job_id, wait_s, tail_bytes)


@mcp.tool()
async def list_jobs(limit: int = 10) -> list[dict]:
    """List recent direct local jobs (not Claude Code delegation)."""
    return await jobs.list_jobs(limit)


@mcp.tool()
async def cancel_job(job_id: str) -> dict:
    """Stop a local job and its process group. This cannot undo completed side effects."""
    return await jobs.cancel(job_id)


@mcp.tool()
async def run_applescript(script: str) -> dict:
    """Run AppleScript directly for Mac apps. Full user authority; confirm messages and changes."""
    result = await macos.applescript(script)
    return {"output": result[:32000], "truncated": len(result) > 32000}


@mcp.tool()
async def permission_status() -> dict:
    """Check platform UI permission/session status without prompting or capturing anything."""
    return await asyncio.to_thread(desktop_api.permission_status)


@mcp.tool()
async def screenshot(display: int = 1, max_width: int = 1600):
    """Capture a display and return a resized JPEG image to this conversation.
    Only use at the owner's request: visible private data leaves the Mac.
    No screenshot history is retained. Image pixels are not global click coordinates."""
    image = await desktop_api.screenshot(display, max_width)
    return [
        TextContent(type="text", text=json.dumps(image["metadata"])),
        ImageContent(type="image", data=image["data"], mimeType=image["mimeType"]),
    ]


@mcp.tool()
async def list_displays() -> list[dict]:
    """Get display bounds in global points and physical pixels for screenshot/click mapping."""
    return await asyncio.to_thread(desktop_api.displays)


@mcp.tool()
async def list_apps() -> list[str]:
    """List running foreground/visible applications (names only, no window contents)."""
    return await desktop_api.list_apps()


@mcp.tool()
async def focus_app(name: str) -> dict:
    """Activate an application. Windows activates matching running executable names.
    Observe UI before entering text; posting activation does not guarantee focus."""
    return await desktop_api.focus_app(name)


@mcp.tool()
async def click(x: float, y: float, button: str = "left", clicks: int = 1) -> dict:
    """Post a click in global desktop coordinates; use screenshot metadata to map image pixels.
    Confirm destructive controls before clicking. Posting is not proof the action succeeded."""
    return await asyncio.to_thread(desktop_api.click, x, y, button, clicks)


@mcp.tool()
async def type_text(text: str) -> dict:
    """Type into the focused UI element. Observe focus first; confirm outgoing text with owner."""
    return await desktop_api.type_text(text)


@mcp.tool()
async def press_key(key: str, modifiers: list[str] | None = None) -> dict:
    """Post a named key or one letter/digit with platform-specific modifiers.
    Mac: command/shift/option/control. Windows: windows/shift/alt/control.
    Confirm shortcuts with irreversible or external effects before calling."""
    return await desktop_api.press_key(key, modifiers)


@mcp.tool()
async def read_clipboard() -> dict:
    """Read clipboard text only when requested. Clipboard can contain secrets sent to this chat."""
    return await desktop_api.clipboard_read()


@mcp.tool()
async def write_clipboard(text: str) -> dict:
    """Replace clipboard text. No paste is performed automatically."""
    return await desktop_api.clipboard_write(text)


UI_TOOLS = {
    "screenshot",
    "list_displays",
    "list_apps",
    "focus_app",
    "click",
    "type_text",
    "press_key",
    "read_clipboard",
    "write_clipboard",
}
WORKER_TOOLS = {"queue_task", "task_status", "recent_tasks"}


def enabled_tool(name: str) -> bool:
    if name == "run_applescript" and sys.platform != "darwin":
        return False
    if name in UI_TOOLS and sys.platform not in ("darwin", "win32"):
        return False
    if name in UI_TOOLS and not SETTINGS["ui_enabled"]:
        return False
    if name in WORKER_TOOLS and os.environ.get("VBRIDGE_ENABLE_WORKER") != "1":
        return False
    return True


@mcp._mcp_server.list_tools()
async def bridged_list_tools():
    return [*[t for t in await mcp.list_tools() if enabled_tool(t.name)], *desktop.tools.values()]


@mcp._mcp_server.call_tool(validate_input=True)
async def bridged_call_tool(name: str, arguments: dict):
    if not enabled_tool(name):
        raise ValueError("Tool disabled in local configuration")
    if name in desktop.routes:
        return await desktop.call(name, arguments)
    return await mcp.call_tool(name, arguments)


_fails: dict[str, list[float]] = {}


def _throttled(ip: str) -> bool:
    now = time.time()
    xs = [t for t in _fails.get(ip, []) if now - t < 600]
    _fails[ip] = xs
    return len(xs) >= 5


@mcp.custom_route("/consent", methods=["GET", "POST"])
async def consent(request: Request):
    pid = request.query_params.get("p", "")
    item = provider.pending(pid)
    if not item:
        return PlainTextResponse("Expired. Start again from Claude.", status_code=400)
    client = item[1]
    if request.method == "GET":
        name = html.escape(client.client_name or client.client_id)
        callback = html.escape(str(item[2].redirect_uri))
        return HTMLResponse(
            f"<meta name=viewport content='width=device-width'><body style='font:16px system-ui;max-width:420px;margin:12vh auto;padding:0 16px'>"
            f"<h3>Allow <b>{name}</b> to use vbridge?</h3>"
            f"<p>Callback: <code>{callback}</code>. Only approve the Claude connector you just started.</p>"
            f"<p>This grants full local user access: shell, files, and installed Desktop tools. It can read private data, change files, and send messages.</p>"
            f"<form method=post><input type=password name=pw placeholder=passphrase autofocus style='width:100%;padding:10px;font-size:16px'>"
            f"<p><button name=a value=allow style='padding:10px 18px'>Allow</button> "
            f"<button name=a value=deny style='padding:10px 18px'>Deny</button></p></form>"
        )
    ip = request.client.host if request.client else "?"
    form = await request.form()
    if form.get("a") == "deny":
        return RedirectResponse(provider.deny(pid) or "/", status_code=303)
    if _throttled(ip):
        return PlainTextResponse("Too many tries. Wait 10 minutes.", status_code=429)
    if not hmac.compare_digest(str(form.get("pw", "")).encode(), _passphrase().encode()):
        _fails.setdefault(ip, []).append(time.time())
        return PlainTextResponse("Wrong passphrase.", status_code=403)
    url = provider.approve(pid)
    return RedirectResponse(url or "/", status_code=303)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request):
    return PlainTextResponse("ok")


class BridgeGate:
    """OAuth-only middleware, privacy headers, request bounds and emergency switch."""

    def __init__(self, app):
        self.app = app
        self._auth_hits: list[float] = []

    async def _reply(self, send, status: int, body: bytes = b""):
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await desktop.start()
            try:
                await self.app(scope, receive, send)
            finally:
                await jobs.close()
                await desktop.close()
            return
        if scope["type"] == "http":
            original_send = send

            async def private_send(message):
                if message["type"] == "http.response.start":
                    message = dict(
                        message,
                        headers=[
                            *message.get("headers", []),
                            (b"cache-control", b"no-store"),
                            (b"referrer-policy", b"no-referrer"),
                            (b"x-frame-options", b"DENY"),
                            (b"content-security-policy", b"frame-ancestors 'none'"),
                        ],
                    )
                await original_send(message)

            send = private_send
            if scope["path"] == "/revoke" and scope["method"] == "POST":
                # SDK 1.30 requires a nullable client_secret field even for public
                # clients. Supply an empty field only when absent; retain all
                # authentication and grant ownership checks in the SDK handler.
                body = bytearray()
                while True:
                    part = await receive()
                    if part["type"] == "http.disconnect":
                        return
                    body.extend(part.get("body", b""))
                    if len(body) > 8192:
                        return await self._reply(send, 413)
                    if not part.get("more_body", False):
                        break
                from urllib.parse import parse_qs

                if "client_secret" not in parse_qs(body.decode(errors="replace"), keep_blank_values=True):
                    body.extend(b"&client_secret=")
                first = True
                original_receive = receive

                async def revoke_receive():
                    nonlocal first
                    if first:
                        first = False
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await original_receive()

                receive = revoke_receive
                scope = dict(scope, headers=[(k, v) for k, v in scope["headers"] if k != b"content-length"])
            if scope["path"] in ("/register", "/authorize"):
                now = time.monotonic()
                self._auth_hits = [t for t in self._auth_hits if now - t < 60]
                if len(self._auth_hits) >= 30:
                    return await self._reply(send, 429, b"retry later")
                self._auth_hits.append(now)
            if (HOME / "DISABLED").exists():
                return await self._reply(send, 503, b"disabled")
        await self.app(scope, receive, send)


def main() -> None:
    _passphrase()
    app = BridgeGate(mcp.streamable_http_app())
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=PORT,
        log_level="warning",
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
