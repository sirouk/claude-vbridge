"""Portable launcher/admin checks. No live service or macOS actions."""

from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "scripts/bridge_admin.py"
spec = importlib.util.spec_from_file_location("bridge_admin", PATH)
assert spec and spec.loader
admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin)


class AdminTests(unittest.TestCase):
    def test_settings_uses_server_contract(self):
        with patch(
            "vbridge.config.settings", return_value={"public_url": "https://bridge.example", "port": 9999}
        ):
            self.assertEqual(admin.settings(), ("https://bridge.example", 9999))

    def test_nonstandard_remote_port_rejected(self):
        with patch(
            "vbridge.config.settings",
            return_value={"public_url": "https://bridge.example:10000", "port": 8799},
        ):
            with self.assertRaises(RuntimeError):
                admin.settings()

    def test_install_writes_private_portable_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            home = admin.ensure_private_dir(Path(directory) / "state")
            agents = admin.ensure_private_dir(Path(directory) / "agents")
            plist = agents / "bridge.plist"
            with (
                patch.object(admin, "HOME", home),
                patch.object(admin, "PLIST", plist),
                patch.object(admin.sys, "platform", "darwin"),
            ):
                admin.install("https://bridge.example")
                values = json.loads((home / "settings.json").read_text())
                self.assertEqual(values["public_url"], "https://bridge.example")
                self.assertFalse(values["ui_enabled"])
                if os.name != "nt":
                    self.assertEqual((home / "settings.json").stat().st_mode & 0o777, 0o600)
                installed = plistlib.loads(plist.read_bytes())
                self.assertEqual(installed["WorkingDirectory"], str(admin.ROOT))
                self.assertEqual(installed["EnvironmentVariables"]["VBRIDGE_HOME"], str(home))
                before = plist.read_bytes()
                with self.assertRaises(RuntimeError):
                    admin.install("https://another.example")
                self.assertEqual(plist.read_bytes(), before)
                self.assertEqual(
                    json.loads((home / "settings.json").read_text())["public_url"], "https://bridge.example"
                )

    def test_route_conflict_is_fail_closed(self):
        state = {
            "Web": {"bridge.example:443": {"Handlers": {"/mcp": {"Proxy": "http://127.0.0.1:9998/mcp"}}}}
        }
        with (
            patch.object(admin, "settings", return_value=("https://bridge.example", 8799)),
            patch.object(admin.subprocess, "check_output", return_value=json.dumps(state)),
            patch.object(admin.subprocess, "run") as run,
        ):
            with self.assertRaises(RuntimeError):
                admin.configure_routes()
            run.assert_not_called()

    def test_windows_task_is_interactive_least_privilege_and_user_logon_only(self):
        xml = ET.fromstring(admin.windows_task_xml())
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(
            xml.findtext("t:Principals/t:Principal/t:LogonType", namespaces=ns), "InteractiveToken"
        )
        self.assertEqual(xml.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=ns), "LeastPrivilege")
        self.assertEqual(
            xml.findtext("t:Triggers/t:LogonTrigger/t:UserId", namespaces=ns), "CURRENT_USER_SID"
        )
        self.assertNotIn("Password", admin.windows_task_xml())
        self.assertIn("--state-home", xml.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns))
        self.assertIn("python.exe", xml.findtext("t:Actions/t:Exec/t:Command", namespaces=ns))

    def test_windows_install_refuses_existing_task_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            home = admin.ensure_private_dir(Path(directory) / "state")
            with (
                patch.object(admin, "HOME", home),
                patch.object(admin.sys, "platform", "win32"),
                patch.object(admin, "windows_task_exists", return_value=True),
                patch.object(admin, "private_write") as write,
                patch.object(admin, "_powershell") as powershell,
            ):
                with self.assertRaises(RuntimeError):
                    admin.install("https://bridge.example")
                write.assert_not_called()
                powershell.assert_not_called()

    def test_windows_install_registers_only_current_sid(self):
        with tempfile.TemporaryDirectory() as directory:
            home = admin.ensure_private_dir(Path(directory) / "state")
            with (
                patch.object(admin, "HOME", home),
                patch.object(admin.sys, "platform", "win32"),
                patch.object(admin, "windows_task_exists", return_value=False),
                patch.object(admin, "_powershell") as powershell,
            ):
                admin.install("https://bridge.example")
                script = powershell.call_args.args[0]
                self.assertIn("$Identity.User.Value", script)
                self.assertIn("Register-ScheduledTask", script)
                self.assertIn("InteractiveToken", script)
                self.assertNotIn("-Password", script)
                values = json.loads((home / "settings.json").read_text())
                self.assertFalse(values["ui_enabled"])

    def test_windows_replace_rejects_running_task_without_changing_settings(self):
        from subprocess import CompletedProcess

        with tempfile.TemporaryDirectory() as directory:
            home = admin.ensure_private_dir(Path(directory) / "state")
            admin.private_write(home / "settings.json", '{"public_url": "https://bridge.example"}')
            before = (home / "settings.json").read_bytes()
            with (
                patch.object(admin, "HOME", home),
                patch.object(admin.sys, "platform", "win32"),
                patch.object(admin, "windows_task_exists", return_value=True),
                patch.object(admin, "_powershell", return_value=CompletedProcess([], 0, "Running\n")),
            ):
                with self.assertRaisesRegex(RuntimeError, "Stop"):
                    admin.install("https://another.example", replace_agent=True)
                self.assertEqual((home / "settings.json").read_bytes(), before)

    def test_windows_stop_sets_persistent_switch_and_preserves_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            home = admin.ensure_private_dir(Path(directory) / "state")
            with (
                patch.object(admin, "HOME", home),
                patch.object(admin.sys, "platform", "win32"),
                patch.object(admin, "_powershell") as powershell,
                patch.object(admin, "configure_routes") as routes,
            ):
                admin.windows_lifecycle("stop")
                self.assertTrue((home / "DISABLED").exists())
                self.assertIn("Stop-ScheduledTask", powershell.call_args.args[0])
                routes.assert_not_called()

    def test_routes_sequential_preserve_unrelated_paths(self):
        initial = {"Web": {"bridge.example:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9998"}}}}}
        current = json.loads(json.dumps(initial))
        calls = []

        def read(*args, **kwargs):
            return json.dumps(current)

        def run(argv, **kwargs):
            calls.append(argv)
            path = next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--set-path="))
            current["Web"]["bridge.example:443"]["Handlers"][path] = {"Proxy": argv[-1]}
            current["AllowFunnel"] = {"bridge.example:443": True}

        with tempfile.TemporaryDirectory() as directory:
            home = admin.ensure_private_dir(Path(directory) / "state")
            with (
                patch.object(admin, "HOME", home),
                patch.object(admin, "settings", return_value=("https://bridge.example", 8799)),
                patch.object(admin.subprocess, "check_output", side_effect=read),
                patch.object(admin.subprocess, "run", side_effect=run),
            ):
                admin.configure_routes()
                self.assertEqual(len(calls), len(admin.ROUTES))
                self.assertTrue(all("--https=443" in argv for argv in calls))
                self.assertFalse(any("reset" in argv for argv in calls))
                self.assertEqual(
                    current["Web"]["bridge.example:443"]["Handlers"]["/"],
                    initial["Web"]["bridge.example:443"]["Handlers"]["/"],
                )
                self.assertEqual(len(list((home / "backups").glob("routing-*.json"))), 1)

    def test_concurrent_route_conflict_stops_before_write(self):
        before = {"Web": {"bridge.example:443": {"Handlers": {}}}}
        changed = {"Web": {"bridge.example:443": {"Handlers": {"/mcp": {"Proxy": "http://127.0.0.1:9998"}}}}}
        with tempfile.TemporaryDirectory() as directory:
            home = admin.ensure_private_dir(Path(directory) / "state")
            with (
                patch.object(admin, "HOME", home),
                patch.object(admin, "settings", return_value=("https://bridge.example", 8799)),
                patch.object(
                    admin.subprocess, "check_output", side_effect=[json.dumps(before), json.dumps(changed)]
                ),
                patch.object(admin.subprocess, "run") as run,
            ):
                with self.assertRaises(RuntimeError):
                    admin.configure_routes()
                run.assert_not_called()

    def test_operator_client_cache_avoids_duplicate_registration(self):
        class Response:
            def __init__(self, data=None, status_code=200, location=None):
                self.data = data
                self.status_code = status_code
                self.headers = {"location": location} if location else {}

            def raise_for_status(self):
                pass

            def json(self):
                return self.data

        class Client:
            def __init__(self):
                self.registrations = 0
                self.state = None

            def get(self, url, params):
                self.state = params["state"]
                return Response(status_code=302, location="https://bridge.example/consent?nonce=test")

            def post(self, url, **kwargs):
                if url.endswith("/register"):
                    self.registrations += 1
                    return Response({"client_id": "test-client"})
                if "/consent?" in url:
                    return Response(
                        status_code=303,
                        location="http://127.0.0.1:8799/operator-check?code=test&state=" + self.state,
                    )
                if url.endswith("/token"):
                    return Response({"access_token": "test-access", "refresh_token": "test-refresh"})
                raise AssertionError("Unexpected request")

        with tempfile.TemporaryDirectory() as directory:
            home = admin.ensure_private_dir(Path(directory) / "state")
            admin.private_write(home / "passphrase", "local-test-passphrase")
            client = Client()
            with (
                patch.object(admin, "HOME", home),
                patch.object(admin, "settings", return_value=("https://bridge.example", 8799)),
                patch.object(admin.httpx, "Client", return_value=client),
                patch.object(admin.BridgeClient, "rpc"),
            ):
                first = admin.BridgeClient()
                first.login()
                second = admin.BridgeClient()
                second.login()
                self.assertEqual(client.registrations, 1)
                self.assertEqual(second.cid, "test-client")
                if os.name != "nt":
                    self.assertEqual((home / "operator-client.json").stat().st_mode & 0o777, 0o600)

    def test_healthy_routes_do_not_write(self):
        state = {
            "Web": {
                "bridge.example:443": {
                    "Handlers": {p: {"Proxy": "http://127.0.0.1:8799" + p} for p in admin.ROUTES}
                }
            },
            "AllowFunnel": {"bridge.example:443": True},
        }
        state["Web"]["bridge.example:443"]["Handlers"]["/"] = {"Proxy": "http://127.0.0.1:9998"}
        with (
            patch.object(admin, "settings", return_value=("https://bridge.example", 8799)),
            patch.object(admin.subprocess, "check_output", return_value=json.dumps(state)),
            patch.object(admin.subprocess, "run") as run,
        ):
            admin.configure_routes()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
