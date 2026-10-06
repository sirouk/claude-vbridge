"""Operator helpers for macOS/Windows launchers. No extra inference or app reads."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from vbridge.job_store import check_private_file, ensure_private_dir, private_write

HOME = Path(os.environ.get("VBRIDGE_HOME", Path.home() / ".vbridge")).expanduser()
ROOT = Path(__file__).resolve().parents[1]
PLIST = Path.home() / "Library/LaunchAgents/com.vbridge.server.plist"
ROUTES = ["/mcp", "/.well-known", "/authorize", "/token", "/register", "/revoke", "/consent", "/healthz"]


def settings() -> tuple[str, int]:
    """Use the same private settings contract as the server."""
    from vbridge.config import settings as server_settings

    value = server_settings()
    base, port = value["public_url"], value["port"]
    parsed = urlparse(base)
    if parsed.scheme != "https" or parsed.port not in (None, 443):
        raise RuntimeError("Launcher requires an HTTPS issuer on standard port 443")
    if not 1024 <= port <= 65535:
        raise RuntimeError("VBRIDGE_PORT must be an unprivileged TCP port")
    return base, port


def _powershell(script: str, *, capture: bool = True) -> subprocess.CompletedProcess:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        check=True,
        capture_output=capture,
        text=True,
    )


def _ps(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


TASK_CONTEXT = """
$ErrorActionPreference = 'Stop'
$Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$TaskName = 'VBridge-' + $Identity.User.Value
"""


def windows_task_exists() -> bool:
    result = _powershell(
        TASK_CONTEXT
        + """
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $Task) { Write-Output 'absent' } else { Write-Output 'present' }
"""
    )
    return result.stdout.strip() == "present"


def windows_task_xml() -> str:
    """Return a task template; Windows resolves the current SID at registration."""
    import xml.etree.ElementTree as ET

    ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", ns)
    task = ET.Element(f"{{{ns}}}Task", version="1.2")

    def node(parent, name, text=None, **attributes):
        child = ET.SubElement(parent, f"{{{ns}}}{name}", attributes)
        if text is not None:
            child.text = text
        return child

    triggers = node(task, "Triggers")
    trigger = node(triggers, "LogonTrigger")
    node(trigger, "Enabled", "true")
    node(trigger, "UserId", "CURRENT_USER_SID")
    principal = node(node(task, "Principals"), "Principal", id="User")
    node(principal, "UserId", "CURRENT_USER_SID")
    node(principal, "LogonType", "InteractiveToken")
    node(principal, "RunLevel", "LeastPrivilege")
    options = node(task, "Settings")
    node(options, "MultipleInstancesPolicy", "IgnoreNew")
    node(options, "DisallowStartIfOnBatteries", "false")
    node(options, "StopIfGoingOnBatteries", "false")
    node(options, "StartWhenAvailable", "true")
    node(options, "ExecutionTimeLimit", "PT0S")
    restart = node(options, "RestartOnFailure")
    node(restart, "Interval", "PT1M")
    node(restart, "Count", "3")
    actions = node(task, "Actions", Context="User")
    execute = node(actions, "Exec")
    node(execute, "Command", str(ROOT / ".venv/Scripts/python.exe"))
    node(
        execute,
        "Arguments",
        subprocess.list2cmdline(
            [str(ROOT / "scripts/bridge_admin.py"), "run-server", "--state-home", str(HOME)]
        ),
    )
    node(execute, "WorkingDirectory", str(ROOT))
    return ET.tostring(task, encoding="unicode")


def install(public_url: str | None = None, replace_agent: bool = False) -> None:
    import plistlib

    if sys.platform not in ("darwin", "win32"):
        raise RuntimeError("Install is supported on macOS and Windows only")
    config = HOME / "settings.json"
    if config.exists():
        check_private_file(config)
    previous_config = config.read_bytes() if config.exists() else None
    config_data = json.loads(previous_config) if previous_config is not None else {}
    env = dict(config_data.get("runtime_env", config_data.get("launchagent_env", {})))
    if config_data.get("public_url"):
        env.setdefault("VBRIDGE_PUBLIC_URL", str(config_data["public_url"]))
    env.setdefault("VBRIDGE_PORT", str(config_data.get("port", 8799)))
    if public_url:
        env["VBRIDGE_PUBLIC_URL"] = public_url.rstrip("/")
    if not env.get("VBRIDGE_PUBLIC_URL"):
        raise RuntimeError("Install requires --public-url https://YOUR-HOSTNAME")
    parsed = urlparse(env["VBRIDGE_PUBLIC_URL"])
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.port not in (None, 443)
    ):
        raise RuntimeError("--public-url must be an HTTPS origin on standard port 443")
    port = int(env["VBRIDGE_PORT"])
    if not 1024 <= port <= 65535:
        raise RuntimeError("VBRIDGE_PORT must be an unprivileged TCP port")
    windows = sys.platform == "win32"
    exists = windows_task_exists() if windows else PLIST.exists()
    if exists and not replace_agent:
        raise RuntimeError(
            "Existing launcher preserved. Stop it first; use explicit --replace-agent/--replace-task"
        )
    ensure_private_dir(HOME)
    if exists:
        if windows:
            result = _powershell(TASK_CONTEXT + "(Get-ScheduledTask -TaskName $TaskName).State")
            if result.stdout.strip().lower() == "running":
                raise RuntimeError("Stop the running task before replacing it")
        else:
            loaded = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/com.vbridge.server"],
                capture_output=True,
                text=True,
            )
            if loaded.returncode == 0:
                raise RuntimeError("Stop the loaded LaunchAgent before replacing it")
        ensure_private_dir(HOME / "backups")
        suffix = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        if config.exists():
            private_write(HOME / "backups" / ("settings-" + suffix + ".json"), config.read_bytes())
        if windows:
            old = _powershell(TASK_CONTEXT + "Export-ScheduledTask -TaskName $TaskName").stdout
            private_write(HOME / "backups" / ("task-" + suffix + ".xml"), old)
        else:
            private_write(HOME / "backups" / ("launchagent-" + suffix + ".plist"), PLIST.read_bytes())
    config_data.update(public_url=env["VBRIDGE_PUBLIC_URL"], port=port)
    config_data.setdefault("desktop_enabled", True)
    config_data.setdefault("ui_enabled", False)
    config_data["runtime_env" if windows else "launchagent_env"] = env
    private_write(config, json.dumps(config_data, indent=2) + "\n")
    if windows:
        template = windows_task_xml()
        script = TASK_CONTEXT + "$Xml = " + _ps(template) + "\n"
        script += "$Xml = $Xml.Replace('CURRENT_USER_SID', $Identity.User.Value)\n"
        script += "Register-ScheduledTask -TaskName $TaskName -Xml $Xml -Force | Out-Null\n"
        try:
            _powershell(script)
        except Exception:
            if previous_config is not None:
                private_write(config, previous_config)
            raise
        print("Installed per-user interactive logon task. Run .\\windows-bridge.ps1 start")
        return
    runtime_env = {k: v for k, v in env.items() if k not in {"VBRIDGE_PUBLIC_URL", "VBRIDGE_PORT"}}
    runtime_env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"))
    runtime_env.update({"HOME": str(Path.home()), "VBRIDGE_HOME": str(HOME)})
    data = {
        "Label": "com.vbridge.server",
        "ProgramArguments": [str(ROOT / ".venv/bin/python"), "-m", "vbridge.server"],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": runtime_env,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "StandardOutPath": str(HOME / "server.log"),
        "StandardErrorPath": str(HOME / "server.log"),
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    try:
        private_write(PLIST, plistlib.dumps(data))
    except Exception:
        if previous_config is not None:
            private_write(config, previous_config)
        raise
    print("Installed private configuration and LaunchAgent. Run ./mac-bridge.sh start")


def windows_lifecycle(action: str) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Use mac-bridge.sh for macOS service operations")
    if action == "status":
        _powershell(
            TASK_CONTEXT
            + """
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $Task) { Write-Output 'Logon task: not installed' }
else { Write-Output ('Logon task: ' + $Task.State) }
""",
            capture=False,
        )
        if (HOME / "DISABLED").exists():
            print("Bridge: DISABLED")
        with httpx.Client(timeout=5) as client:
            response = client.get(f"http://127.0.0.1:{settings()[1]}/healthz")
            if response.status_code != 200 or response.text != "ok":
                raise RuntimeError("Local bridge is not ready")
        print("Local bridge: ready")
        return
    if action == "stop":
        ensure_private_dir(HOME)
        private_write(HOME / "DISABLED", "")
        _powershell(
            TASK_CONTEXT
            + """
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $Task) { Stop-ScheduledTask -TaskName $TaskName }
"""
        )
        print("Bridge disabled and stopped. Shared Funnel routes preserved.")
        return
    if not windows_task_exists():
        raise RuntimeError("Install the per-user logon task first")
    # This is the operator-owned kill switch, not shared recovery/evidence.
    (HOME / "DISABLED").unlink(missing_ok=True)
    script = TASK_CONTEXT
    if action == "restart":
        script += "Stop-ScheduledTask -TaskName $TaskName\n"
    script += "Start-ScheduledTask -TaskName $TaskName\n"
    _powershell(script)
    ready()
    configure_routes()
    print("Connector URL:", settings()[0] + "/mcp")


def run_windows_server() -> None:
    if sys.platform != "win32":
        raise RuntimeError("run-server is the Windows task entry point")
    ensure_private_dir(HOME)
    check_private_file(HOME / "settings.json")
    config = json.loads((HOME / "settings.json").read_text())
    os.environ.update(
        {
            str(k): str(v)
            for k, v in config.get("runtime_env", {}).items()
            if k not in {"VBRIDGE_PUBLIC_URL", "VBRIDGE_PORT"}
        }
    )
    os.environ["VBRIDGE_HOME"] = str(HOME)
    if (HOME / "server.log").exists():
        check_private_file(HOME / "server.log")
    else:
        private_write(HOME / "server.log", b"")
    with (HOME / "server.log").open("a", encoding="utf-8") as log:
        os.dup2(log.fileno(), sys.stdout.fileno())
        os.dup2(log.fileno(), sys.stderr.fileno())
        from vbridge.server import main as server_main

        server_main()


def ready(timeout: int = 35) -> None:
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=2) as client:
        while True:
            try:
                r = client.get(f"http://127.0.0.1:{settings()[1]}/healthz")
                if r.status_code == 200 and r.text == "ok":
                    print("Local bridge: ready")
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Bridge did not become ready. Inspect private logs locally")
            time.sleep(0.25)


def configure_routes() -> None:
    base, port = settings()
    origin = f"http://127.0.0.1:{port}"
    raw = subprocess.check_output(["tailscale", "serve", "status", "--json"], text=True)
    state = json.loads(raw)
    key = urlparse(base).hostname + ":443"
    handlers = state.get("Web", {}).get(key, {}).get("Handlers", {})
    for path in ROUTES:
        existing = handlers.get(path)
        expected = {"Proxy": origin + path}
        if existing and existing != expected:
            raise RuntimeError(f"Refusing to replace another service at {path}")
    missing = [p for p in ROUTES if handlers.get(p) != {"Proxy": origin + p}]
    public = state.get("AllowFunnel", {}).get(key, False)
    if missing or not public:
        backups = HOME / "backups"
        ensure_private_dir(backups)
        snapshot = backups / ("routing-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
        private_write(snapshot, raw)
        print("Routing backup:", snapshot)
        # All writes share a config etag. Never run these in parallel.
        for path in missing or ["/mcp"]:
            # Recheck immediately before each sequential write. Tailscale has no
            # route-scoped transaction; operators must not edit routes concurrently.
            current = json.loads(
                subprocess.check_output(["tailscale", "serve", "status", "--json"], text=True)
            )
            current_handler = current.get("Web", {}).get(key, {}).get("Handlers", {}).get(path)
            if current_handler and current_handler != {"Proxy": origin + path}:
                raise RuntimeError(f"Concurrent routing conflict at {path}; stopped without replacing it")
            subprocess.run(
                ["tailscale", "funnel", "--bg", "--https=443", "--set-path=" + path, origin + path],
                check=True,
                capture_output=True,
                text=True,
            )
        latest = json.loads(subprocess.check_output(["tailscale", "serve", "status", "--json"], text=True))
        actual = latest.get("Web", {}).get(key, {}).get("Handlers", {})
        if not all(actual.get(p) == {"Proxy": origin + p} for p in ROUTES):
            raise RuntimeError("Incomplete bridge routes; inspect private routing backup")
        if not latest.get("AllowFunnel", {}).get(key):
            raise RuntimeError("Funnel is not public")
        if any(actual.get(p) != value for p, value in handlers.items() if p not in ROUTES):
            raise RuntimeError(
                "Unrelated routes changed; inspect private backup. No automatic restore attempted"
            )
    print("Funnel: named bridge routes ready; other routes preserved")


class BridgeClient:
    def __init__(self):
        self.base, self.port = settings()
        self.client = httpx.Client(timeout=60, follow_redirects=False)
        self.cid = None
        self.token = None
        self.refresh = None

    def login(self):
        cache = HOME / "operator-client.json"
        redirect_uri = f"http://127.0.0.1:{self.port}/operator-check"
        if cache.exists():
            check_private_file(cache)
        cached = json.loads(cache.read_text()) if cache.exists() else {}
        if cached.get("issuer") == self.base and cached.get("redirect_uri") == redirect_uri:
            self.cid = cached.get("client_id")
        if not self.cid:
            r = self.client.post(
                self.base + "/register",
                json={
                    "client_name": "VBridge local runbook check",
                    "redirect_uris": [redirect_uri],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                },
            )
            r.raise_for_status()
            self.cid = r.json()["client_id"]
            ensure_private_dir(HOME)
            private_write(
                cache,
                json.dumps({"issuer": self.base, "redirect_uri": redirect_uri, "client_id": self.cid}) + "\n",
            )
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {
            "response_type": "code",
            "client_id": self.cid,
            "redirect_uri": f"http://127.0.0.1:{self.port}/operator-check",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "bridge",
            "resource": self.base + "/mcp",
            "state": secrets.token_urlsafe(16),
        }
        r = self.client.get(self.base + "/authorize", params=params)
        if r.status_code not in (302, 303, 307):
            r.raise_for_status()
            raise RuntimeError("Expected OAuth consent redirect")
        consent = r.headers["location"]
        if not consent.startswith(self.base + "/consent?"):
            raise RuntimeError("Unexpected consent URL")
        check_private_file(HOME / "passphrase")
        r = self.client.post(consent, data={"pw": (HOME / "passphrase").read_text().strip(), "a": "allow"})
        if r.status_code not in (302, 303, 307):
            r.raise_for_status()
            raise RuntimeError("Expected approved callback redirect")
        callback = urlparse(r.headers["location"])
        query = parse_qs(callback.query)
        if query.get("state") != [params["state"]]:
            raise RuntimeError("OAuth state mismatch")
        r = self.client.post(
            self.base + "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": self.cid,
                "code": query["code"][0],
                "code_verifier": verifier,
                "redirect_uri": params["redirect_uri"],
            },
        )
        r.raise_for_status()
        self.token = r.json()["access_token"]
        self.refresh = r.json()["refresh_token"]
        self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "runbook-check", "version": "1"},
            },
        )

    def rpc(self, method, params):
        r = self.client.post(
            self.base + "/mcp",
            headers={
                "authorization": "Bearer " + self.token,
                "accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        r.raise_for_status()
        value = r.json()
        if "error" in value:
            raise RuntimeError(str(value["error"]))
        return value["result"]

    def call(self, name, args):
        result = self.rpc("tools/call", {"name": name, "arguments": args})
        if result.get("isError"):
            raise RuntimeError(str(result.get("content")))
        text = result["content"][0]["text"]
        try:
            return json.loads(text)
        except ValueError:
            return text

    def close(self):
        try:
            if self.refresh:
                r = self.client.post(
                    self.base + "/revoke",
                    data={"token": self.refresh, "client_id": self.cid, "token_type_hint": "refresh_token"},
                )
                r.raise_for_status()
        finally:
            self.client.close()


PERMISSION_CODE = """
import ctypes,json,os
from pathlib import Path
result={}
try:
 fd=os.open(str(Path.home()/"Library/Messages/chat.db"),os.O_RDONLY)
 os.close(fd)
 result["Messages database access"]=True
except OSError:
 result["Messages database access"]=False
for label,framework,function in [
 ("Accessibility","ApplicationServices","AXIsProcessTrusted"),
 ("Screen Recording","CoreGraphics","CGPreflightScreenCaptureAccess")]:
 lib=ctypes.CDLL("/System/Library/Frameworks/"+framework+".framework/"+framework)
 f=getattr(lib,function); f.argtypes=[]; f.restype=ctypes.c_bool
 result[label]=bool(f())
print(json.dumps(result))
"""


def check(permissions: bool = False):
    import shlex

    bridge = BridgeClient()
    try:
        bridge.login()
        status = bridge.call("bridge_status", {})
        print("Public OAuth + MCP: working")
        print("Desktop tools:", status["desktop_tools"])
        if status["backend_errors"]:
            raise RuntimeError("Desktop backend errors present; inspect private server logs")
        print("Desktop backends connected:", len(status["desktop_backends"]))
        r = bridge.call(
            "run_command",
            {"command": "echo BRIDGE-OK" if sys.platform == "win32" else "printf BRIDGE-OK", "timeout_s": 5},
        )
        if r["exit_code"] != 0 or r["output"].strip() != "BRIDGE-OK":
            raise RuntimeError("Direct execution failed")
        print("Direct shell: working (no inference)")
        if permissions and sys.platform == "win32":
            states = bridge.call("permission_status", {})
            print(json.dumps(states, indent=2))
            print("Windows checks do not grant rights or test screenshot/input delivery.")
        elif permissions:
            # Explicit sys.executable means the bridge's selected Python binary.
            cmd = shlex.quote(sys.executable) + " -c " + shlex.quote(PERMISSION_CODE)
            r = bridge.call("run_command", {"command": cmd, "timeout_s": 10})
            if r["exit_code"] != 0:
                raise RuntimeError(r["output"])
            states = json.loads(r["output"])
            for name, allowed in states.items():
                print(name + ": " + ("GRANTED" if allowed else "NOT GRANTED"))
            print("No messages were read, screenshots taken, or app data changed.")
            if not all(states.values()):
                raise RuntimeError("Some privacy grants are missing. See RUNBOOK.md")
    finally:
        bridge.close()


def main():
    global HOME
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "install",
            "start",
            "restart",
            "status",
            "stop",
            "run-server",
            "settings",
            "connector",
            "ready",
            "routes",
            "check",
            "permissions",
            "url",
            "local-url",
            "runtime",
            "enable",
            "disable",
        ],
    )
    parser.add_argument("--public-url")
    parser.add_argument("--replace-agent", "--replace-task", action="store_true")
    parser.add_argument("--state-home", type=Path)
    args = parser.parse_args()
    if args.state_home:
        HOME = args.state_home.expanduser().resolve()
        os.environ["VBRIDGE_HOME"] = str(HOME)
    try:
        if args.action in {"start", "restart", "status", "stop"}:
            windows_lifecycle(args.action)
        elif args.action == "run-server":
            run_windows_server()
        elif args.action == "settings":
            if sys.platform == "win32":
                _powershell("Start-Process 'ms-settings:privacy'")
                print(
                    "Windows GUI tools need your active unlocked desktop session, not administrator rights."
                )
                print("Review UI opt-in in private settings; see RUNBOOK.md.")
            else:
                subprocess.run(
                    ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
                    check=True,
                )
        elif args.action == "connector":
            print("Name: VBridge\nURL: " + settings()[0] + "/mcp\nAuthentication: Sign in now")
            print("OAuth client: Register automatically\nRequest headers: empty")
            print("Read the private passphrase locally; never paste into shared logs.")
        elif args.action == "install":
            install(args.public_url, args.replace_agent)
        elif args.action == "ready":
            ready()
        elif args.action == "routes":
            configure_routes()
        elif args.action == "url":
            print(settings()[0] + "/mcp")
        elif args.action == "local-url":
            print(f"http://127.0.0.1:{settings()[1]}/healthz")
        elif args.action == "runtime":
            print(Path(sys.executable).resolve())
        elif args.action == "enable":
            # Only the operator's known kill switch; no state/evidence cleanup.
            (HOME / "DISABLED").unlink(missing_ok=True)
        elif args.action == "disable":
            ensure_private_dir(HOME)
            private_write(HOME / "DISABLED", "")
        else:
            check(args.action == "permissions")
    except Exception as e:
        # HTTP errors can embed credentials, callback codes or a private hostname.
        if isinstance(e, subprocess.CalledProcessError):
            print("ERROR: platform command failed; inspect private logs locally", file=sys.stderr)
        elif isinstance(e, httpx.HTTPError):
            print("ERROR: HTTP request failed; inspect private logs locally", file=sys.stderr)
        else:
            print("ERROR:", e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
