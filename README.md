# Claude Voice Bridge — macOS and Windows

Use a remote MCP connector in Claude to reach tools on your own Mac or Windows PC.
The bridge provides OAuth authorization and direct local execution. It does not
run a second model for direct tools and needs no inference API key.

## Why this exists

Claude Desktop can use local stdio MCP servers. A Claude web or mobile conversation
needs a reachable remote connector instead. This bridge exposes direct local tools
and can start separate clients for MCP servers configured in Claude Desktop.
It does not attach to Desktop's conversation or reuse Desktop's active app session.

**Voice routing is unverified.** This project cannot enable custom connector tools
in Claude Voice. Availability depends on Claude's client and your account. Test in
text first, then voice. A working OAuth connector does not prove voice support.

## Security and privacy

**This is full-machine access, not a sandbox.** An authorized client can run shell
commands with your logged-in user's authority, read and change files, control apps,
use the clipboard, and send messages through installed tools. Natural-language
confirmation requests are not enforced authorization boundaries.

- Screen captures are sent to the requesting MCP client and can disclose anything
  visible: passwords, messages, documents, notifications, and other people's data.
  Capture images are returned as resized JPEGs. The bridge does not intentionally
  save them, but the client, model service, or conversation may retain them.
- Shell output, file content, clipboard content, and app data also leave the machine
  when returned to a remote client. OAuth protects access, not the sensitivity of
  the returned data. Public Funnel routing exposes the authorization surface.
- macOS permissions granted to shared Python/Node runtimes can affect other scripts.
- Treat the bridge passphrase and OAuth state as credentials. Do not put machine
  settings, private test evidence, or real connector URLs into this repository.
- UI enable/disable settings change the catalog to reduce accidental use. They do
  not contain a client that can already run unrestricted shell commands.

Only deploy this for a trusted personal account. Keep the machine updated. Review the
risks before exposing it publicly, and stop the service when it is not needed.

## Install and connect

Choose the instructions for your OS. **Windows implementation and mocked tests
are present; real Windows CI and interactive desktop validation have not run
yet.** See [RELEASE_READINESS.md](RELEASE_READINESS.md).

Python 3.12+, [uv](https://docs.astral.sh/uv/), and Tailscale with Funnel
permission are required on either OS. Start from your checkout:

### macOS

```bash
uv sync --locked
./mac-bridge.sh install --public-url https://YOUR-HOSTNAME
./mac-bridge.sh start
./mac-bridge.sh check
```

### Windows (PowerShell, normal logged-in user)

```powershell
uv sync --locked
.\windows-bridge.ps1 install -PublicUrl https://YOUR-HOSTNAME
.\windows-bridge.ps1 start
.\windows-bridge.ps1 check
```

Windows installs a scheduled task for **only the current user's interactive
session**, at login. It is not a Windows service. It does not store a Windows
password or request administrator privileges. GUI tools require the same active,
unlocked desktop; they do not work through Session 0, the UAC secure desktop, or
an unavailable/locked session. Do not elevate the launcher as a blanket fix.
If local policy blocks unsigned scripts, review it with your administrator; this
project does not disable policy or supply an execution-policy bypass.

Use your real HTTPS hostname only in the private installation command/configuration.
The launcher stores settings in `~/.vbridge/settings.json` and installs the user
LaunchAgent on macOS or per-user logon task on Windows. It refuses to replace an existing launcher without explicit permission.
The server binds to loopback (default port 8799); the launcher publishes named
OAuth/MCP paths on standard HTTPS port 443. It preserves unrelated Funnel routes
and refuses conflicting paths. This is not an arbitrary reverse-proxy installer.

Run `./mac-bridge.sh connector` or `.\windows-bridge.ps1 connector` for your
local connector URL. In Claude settings,
add **VBridge**, select **Sign in now** and automatic OAuth registration, and
leave request headers empty. Read `~/.vbridge/passphrase` privately and approve
the authorization page. The command does not print the passphrase.
OAuth uses authorization-code PKCE and refresh grants. URL-embedded bearer
credentials are not supported. Do not enter an inference provider key.

See [RUNBOOK.md](RUNBOOK.md) for permissions, recovery, and safe checks.
See [CAPABILITIES.md](CAPABILITIES.md) for tools, limits, performance, and the
current verification boundary.

## Tools

- `ping`, `bridge_status`: service and backend checks.
- `run_command`, `read_file`, `write_file`: direct local actions.
  `run_applescript` is macOS-only.
- `start_job`, `job_status`, `list_jobs`, `cancel_job`: bounded background shell jobs.
  These are process jobs, not model delegation.
- Optional platform UI tools: display/app discovery, screenshot, focus, click, typing,
  key presses, and clipboard. `permission_status` preflight is always available.
  UI action tools are disabled by default;
  enable `ui_enabled` in private settings only after reviewing the risks.
- Configured Desktop stdio servers and enabled Desktop extensions are exported with
  backend namespaces. Catalog size and permissions depend on your installation.

The optional `queue_task`/`task_status`/`recent_tasks` path delegates to Claude Code.
It is hidden unless `VBRIDGE_ENABLE_WORKER=1` is set. It requires a separately
configured worker and may use paid inference or a subscription. Installing or
starting this launcher does **not** start that worker.

## Verify before publishing or using

```bash
uv run --locked python tests/direct.py
uv run --locked pytest -q
uv run --locked python scripts/privacy_audit.py
./mac-bridge.sh check        # Explicit live OAuth/direct-shell check
./mac-bridge.sh permissions  # Explicit live permission preflight
```

`tests/public_direct.py --live` is an explicit opt-in wrapper for the live check.
Live checks reuse a private operator client registration, get fresh PKCE tokens,
and revoke the grant afterward.
They do not call paid inference, send messages, or take screenshots. On macOS, permission
preflight opens the Messages database for access only; it does not read contents.
On Windows, permission preflight checks session/desktop availability only.
Screenshots and UI action delivery require separate, owner-approved live tests.
App schema discovery alone does not prove app actions work.

Emergency stop: `./mac-bridge.sh stop` or `.\windows-bridge.ps1 stop`. This sets `~/.vbridge/DISABLED` and stops the
platform launcher. It cannot undo actions or stop an already-running detached process.
Private state, logs, credentials, backups, and archived live tests belong outside
the checkout under `~/.vbridge/`. The privacy audit is a guardrail, not a guarantee;
review all candidate files and repository history before any publication.

## Release checks and license

The CI workflow covers macOS and Windows with Python 3.12 and 3.13. It installs
locked dependencies, runs offline tests/lint/privacy checks, and builds packages.
A workflow file is not proof that those jobs passed. Interactive screenshot/input,
Task Scheduler permissions, live Funnel/OAuth, and Voice checks are separate
owner-approved validation. No CI job signs in, starts a public service, or reads
personal Desktop state. See [RELEASE_READINESS.md](RELEASE_READINESS.md).

Licensed under [MIT](LICENSE). See [THIRD_PARTY.md](THIRD_PARTY.md) for external
packages, installed extensions, and redistribution limits.
