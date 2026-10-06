# VBridge Runbook — macOS and Windows

## 1. Understand the boundary

The conversation in Claude is the model. Direct tools run on your Mac without
another model or inference key. A remote connector is different from a local
Claude Desktop stdio server. The bridge starts its own MCP server clients; Desktop's
session and macOS grants do not automatically transfer.

An authorized connector has your Mac user's shell and file access. This is not a
sandbox. UI catalog switches and prompts to confirm actions are not a security
boundary against unrestricted shell access. Screen, clipboard, file, app, and shell
results can reach the requesting client and its model service. A screenshot can
expose every visible private window and notification. Do not capture a screen
without first reviewing it. Do not use this with an untrusted client.

Voice routing has not been verified. The bridge cannot change Claude account or
client feature availability. Text success does not establish voice support.

## 2. Install on a new Mac

Install uv and Tailscale using their official instructions. Sign in to Tailscale
and enable Funnel according to your tailnet policy. Use your existing checkout:

```bash
uv sync --locked
./mac-bridge.sh install --public-url https://YOUR-HOSTNAME
```

Use the actual Funnel hostname locally. The placeholder is not a working address.
The launcher creates a private `~/.vbridge/settings.json` (0600), and a user
LaunchAgent at `~/Library/LaunchAgents/com.vbridge.server.plist` (0600). Its paths
are generated from the checkout and current user, never hardcoded in source.
The plist is necessarily outside `~/.vbridge/` because launchd discovers it there.
The private settings contain the issuer and local port:

```json
{
  "public_url": "https://YOUR-HOSTNAME",
  "port": 8799,
  "desktop_enabled": true,
  "ui_enabled": false
}
```

Optional `launchagent_env` is a private map of runtime environment strings. Set
runtime PATH there or in the launcher's environment if your Desktop servers need
particular installed executables. A service does not inherit an interactive shell's
login setup. The generated LaunchAgent reads private metadata via `VBRIDGE_HOME`. If you
set `VBRIDGE_PUBLIC_URL`/`VBRIDGE_PORT` as environment overrides, keep them in sync
with private metadata. Keep all account identifiers and credentials outside the checkout.

Installation refuses to replace an existing LaunchAgent. For an intentional
migration, stop the service first, then use:

```bash
./mac-bridge.sh stop
./mac-bridge.sh install --replace-agent --public-url https://YOUR-HOSTNAME
./mac-bridge.sh start
```

Replacement saves the previous plist under `~/.vbridge/backups/`. It does not delete
OAuth state, private logs, routing backups, or credentials. An existing service can
continue using its installed environment; private settings must retain its
issuer and port. The server and launcher use the same settings contract. Moving a checkout requires explicit
agent replacement because launchd keeps absolute executable paths.

## 3. Start and connect

```bash
./mac-bridge.sh start
./mac-bridge.sh check
./mac-bridge.sh connector
```

Start clears only the known emergency kill switch, loads the LaunchAgent if needed,
waits for loopback readiness, and checks/adds the named OAuth/MCP Funnel routes.
It does not restart a healthy service or start inference workers. The LaunchAgent
starts at login and restarts after a crash. The Mac must be awake and online.

Add a custom connector in Claude settings:

| Field | Value |
| --- | --- |
| Name | VBridge |
| Server URL | The URL printed by `connector` |
| Authentication | Sign in now |
| OAuth client | Register automatically |
| Request headers | Empty |

Read the passphrase from `~/.vbridge/passphrase` privately. Do not display it in a
recorded/shared terminal or paste it into the name field. Confirm that you initiated
the sign-in and approve the bridge consent page. This is bridge authorization,
not a new Anthropic login. Published Claude identity/CIMD is not implemented; use
automatic client registration. URL-embedded bearer tokens are not supported.

Enable the connector in the conversation if Claude offers a tools toggle. Test in
text: “Use Mac Bridge to call ping and bridge_status.” Then try that request in
voice. If voice fails, record the error without private data. An inference key will
not repair Claude Voice's connector routing.

## 4. Everyday operations

```bash
./mac-bridge.sh             # Start or re-enable; no inference worker
./mac-bridge.sh status      # LaunchAgent and local readiness
./mac-bridge.sh check       # Live public OAuth, backends, harmless shell
./mac-bridge.sh permissions # Live permission preflight; no screen capture
./mac-bridge.sh restart     # Restart after runtime/config/grant changes
./mac-bridge.sh settings    # Open Accessibility and select Python in Finder
./mac-bridge.sh connector   # URL and setup fields, not the passphrase
./mac-bridge.sh logs        # Private logs; Ctrl-C leaves service running
./mac-bridge.sh stop        # Emergency disable and stop
```

Logs and `tailscale funnel status` can contain private hostnames, paths, and errors.
Review locally and redact before sharing. `check`/`permissions` reuse a client registration cached in private
`~/.vbridge/operator-client.json`, use fresh PKCE tokens, and revoke their grant
afterward. Existing older checks may have left registrations behind. If the
bounded cap is reached, use targeted expired-registration cleanup. Do not delete
`oauth.json` to clear the cap. If the cached client was revoked/deleted, archive
its cache privately and retry to register a replacement.

## 5. macOS permissions and UI

Open **System Settings → Privacy & Security**. Grant only the access you need.
Claude Desktop permissions do not necessarily cover the bridge's Python and MCP
backend processes. Shared runtime grants can also apply to unrelated scripts.

- **Full Disk Access:** add the bridge's actual Python runtime and, where needed,
  your configured backend runtime. `settings` selects Python in Finder. Discover
  backend executable paths from your private Desktop configuration; do not copy
  another machine's paths. Permission preflight opens the Messages database and
  closes it without reading content.
- **Accessibility:** enable the bridge runtime for UI interactions.
- **Screen Recording / Screen & System Audio Recording:** enable the runtime for
  screenshots. macOS may need a native request before the process appears.
- **Automation, Reminders, Calendar:** approve the relevant app prompts when the
  owner deliberately tests an action. Calendar access can differ for reads/writes.

Never disable SIP, modify TCC databases, or reset all privacy grants as a shortcut.
After changing permissions, restart and rerun `permissions`. Python/Node upgrades
can change process identity and require new grants. A preflight or schema check
is not proof that all actions work. Owner-approved read/write or UI tests must
verify the visible result, not just that a tool returned success.

UI tools are off by default on a new installation. Enable `ui_enabled: true` in
private settings and restart only after reviewing disclosure risks. `screenshot`
returns a resized JPEG image to the caller, without intentional local persistence.
The client may retain it. Display discovery reports global point coordinates;
use its mapping for clicks rather than assuming image pixels equal desktop points.
UI delivery and actual screenshot permissions still need owner-approved live tests.

## 6. Jobs and optional model delegation

`start_job` runs a background shell command (maximum timeout 3600 seconds).
`job_status` waits for at most 25 seconds and returns a bounded output tail.
`list_jobs` and `cancel_job` manage these jobs. They run with your full user
privileges. They are not an inference service. Review shell commands before use.
The emergency switch does not undo actions or guarantee termination of detached
processes. Cancel known jobs deliberately and inspect the machine locally.

Legacy `queue_task`, `task_status`, and `recent_tasks` delegate to Claude Code and
are hidden by default. Enabling `VBRIDGE_ENABLE_WORKER=1` requires a separate,
explicitly configured worker and can incur inference/subscription costs. This
launcher never starts it. Do not use legacy queue fixtures as harmless auth checks.

## 7. Troubleshooting and route safety

### Unreachable connector

Use the HTTPS origin printed by `connector`, on standard port 443, with `/mcp`.
Do not include a private passphrase, token, or a nonstandard port in the URL.
Check that the Mac is awake, Tailscale is connected, and Funnel is enabled. Run
`start`, then `check`. An unauthenticated `/mcp` response of 401 is expected;
it does not prove the authenticated tool path works.

### Server does not start or returns 502

Run `status`, inspect private `logs`, and check the installed executable paths.
A brief 502 can occur during restart. The launcher waits for readiness. If you
changed the issuer in settings, restart with matching LaunchAgent environment.
Changing the issuer may invalidate existing connector authorization expectations.
Back up state first and reconnect deliberately; do not delete OAuth state.

### Funnel route conflict

The launcher publishes only named bridge paths, preserves unrelated paths, and
refuses conflicts. It saves a private routing snapshot before changes. Tailscale
route updates share a single configuration: do not make concurrent route edits.
Never run `tailscale funnel reset`, turn off the whole shared 443 listener, or
replace another application's route as a troubleshooting shortcut. Remove only
explicitly reviewed bridge routes if unpublishing; consult your installed
Tailscale version's help. Standard HTTPS is required by this launcher.

### Permission denied or backend errors

Check private Desktop configuration and actual backend executables. Use permission
preflight, adjust the relevant setting, restart, and retest a harmless action.
Messages/Notes writes and message sending need separate owner-approved tests.
Do not paste private backend errors, command output, or app content into an issue.

## 8. Stop, recovery, and publication hygiene

`./mac-bridge.sh stop` creates `~/.vbridge/DISABLED` and stops the LaunchAgent.
The switch blocks access on later service startup. It does not revoke existing
OAuth grants, undo actions, or stop a tool already running. Existing Funnel routes
are left intact and can return 502 while the server is stopped. `start` explicitly
re-enables access, including existing grants. Revoke grants separately if needed.

Private files:

- `~/.vbridge/settings.json`: machine settings, 0600.
- `~/.vbridge/passphrase`, OAuth state, internal credentials: never share.
- `~/.vbridge/server.log`, job/queue state: potentially sensitive output.
- `~/.vbridge/backups/`: routing and LaunchAgent recovery snapshots.
- `~/.vbridge/private-tests/`: locally archived legacy live fixtures and hashes.
- User LaunchAgent under `~/Library/LaunchAgents/`: private runtime paths/settings.

Do not delete state, evidence, or unrelated routes during recovery. Before any
publication, run the offline tests and `scripts/privacy_audit.py`, then manually
review candidate files and any history. The audit reports locations/categories,
not matched secret values. It cannot guarantee that arbitrary private content is
absent. Do not publish screenshots, private fixtures, credentials, or local logs.

## 9. Windows installation and operations

Windows 10/11 support is implemented but not yet validated on a real Windows
runner or interactive host. Follow the release ledger before claiming readiness.
Use PowerShell as your normal logged-in user:

```powershell
uv sync --locked
.\windows-bridge.ps1 install -PublicUrl https://YOUR-HOSTNAME
.\windows-bridge.ps1 start
.\windows-bridge.ps1 status
.\windows-bridge.ps1 check
.\windows-bridge.ps1 permissions
.\windows-bridge.ps1 connector
.\windows-bridge.ps1 settings
.\windows-bridge.ps1 restart
.\windows-bridge.ps1 logs
.\windows-bridge.ps1 stop
```

`install` registers `VBridge-<current-user-SID>` in Task Scheduler. The trigger
matches that user at logon. `InteractiveToken` and `LeastPrivilege` restrict it
to an existing logged-in user session; there is no service, stored password,
`SYSTEM` principal, or elevated run level. `start`/`restart` wait for loopback
readiness before publishing only named 443 paths. `stop` sets the persistent
kill switch and stops that task. It preserves shared Funnel routes and state.
The task retries crashes a bounded number of times; inspect private logs after
repeated failures. User logoff ends the interactive session. Existing grants are
not revoked by stopping.

`-ReplaceTask` is explicit opt-in. Stop first, then reinstall after moving the
checkout. The old task XML is saved privately under `~/.vbridge/backups/`.
The task action uses the checkout's `.venv\Scripts\python.exe` and passes its
state-directory path explicitly. The private settings `runtime_env` map can set
PATH and backend environment strings. It does not import shell profile settings.
`VBRIDGE_HOME` overrides the state directory on either OS.

Private Windows state requires a protected current-user DACL, not merely a
POSIX-looking chmod mode. The storage layer applies/checks that ACL and fails
closed if protection fails. Do not claim that `chmod(0600)` protects Windows
credentials. Use local NTFS storage and review ACL errors; do not loosen them as
a shortcut. POSIX state directories/files use 0700/0600. Windows task XML contains
runtime paths and identity metadata, not the bridge passphrase.

### Windows UI and permissions

`settings` opens Windows Privacy settings. There is no macOS TCC grant workflow
on Windows. `permissions` checks active desktop availability through the bridge;
it does not grant rights, take screenshots, send input, or prove delivery to an
app. Enable `ui_enabled` in private settings only after reviewing the risks.

- Keep the target desktop active and unlocked. Locked/disconnected sessions,
  Session 0 and the UAC secure desktop are unsupported.
- Windows input restrictions (UIPI) may block input to elevated applications.
  Run both the bridge and target apps at ordinary user privilege. Do not elevate
  the bridge as a blanket workaround.
- `list_apps` reports process executable names for visible windows;
  `focus_app` activates a running match, not an installed app launcher.
- Windows coordinates are physical desktop pixels, including negative origins
  for monitors left/above the primary one. Compatibility fields ending in
  `_points` contain those same physical values on Windows. macOS uses points.
- `run_command` uses the platform shell; use Windows-compatible commands and
  quoting. `run_applescript` is unavailable on Windows.
- A reported input post/typing success is not proof of target delivery. Observe
  the intended change. Stop if focus or outcome is uncertain.

Windows Claude Desktop configuration and enabled extension paths depend on its
installed version and user profile. The bridge starts separate stdio clients;
it does not reuse logged-in chats or private Anthropic account credentials.
Installed macOS-only extensions do not become Windows tools.

Offline CI does not replace this interactive checklist: install/start/status,
logon-only launch, normal-user task replacement/backup, permissions preflight,
a controlled screenshot, harmless text input in an empty app, job cancellation,
live OAuth/Funnel, and stop/re-enable. Use only synthetic data and keep evidence
outside the checkout. These live checks have not yet been completed on Windows.
