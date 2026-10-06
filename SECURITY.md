# Security and privacy

## Trust model

This is an operator-controlled personal computer connector, not a multi-tenant execution
service. OAuth controls which clients can connect. Authorized clients have broad
local authority: unrestricted shell and files, installed MCP tools, and optional
visual/clipboard interaction. It is not a sandbox or an enforced approval system.

Treat remote prompts and tool results as untrusted instructions. A malicious
webpage, document or message returned to Claude may contain prompt injection that
tries to make it use other powerful tools. Conversational confirmation is useful
but is not a security boundary. Do not deploy for untrusted users or clients.

## Credential handling

Credentials and machine metadata live under the private state directory, normally
`~/.vbridge`, outside the repository. Do not publish that directory, its logs,
backups, job output, OAuth state, passphrase, local test evidence or launcher registration.
OAuth protects access but does not prevent disclosure of data returned to a model
provider or retained in conversations. URL-embedded bearer credentials are not
supported. Keep the public HTTPS origin in private settings, not source code.

The repo privacy audit checks publication candidates, not live runtime data or
all possible secret formats. It is a guardrail, not a guarantee. Review the staged
files and full Git history before publication. `.gitignore` does not remove a file
already tracked or scrub history.

## Screenshots and clipboard

Screenshots can contain third-party data, messages, credentials and notifications.
A screenshot tool result sends visible data to the requesting remote client.
Temporary files are deleted after image processing, but the client/model/chat
may retain the returned image. Clipboard reads can similarly disclose secrets.
Use these tools only deliberately. UI tools are off by default for new installs.
This catalog switch does not contain a client that already has shell authority.

## Process jobs and emergency stop

Job timeouts, cancellation and clean shutdown kill the owned platform process tree. They
cannot undo actions or reliably stop a process that deliberately creates a new
session. Crash recovery marks unfinished jobs interrupted; it never replays
commands or signals stale persisted PIDs. The emergency switch blocks new access,
not completed effects. State persists outside the repo and can contain commands
and outputs that should be treated as private.

## Permission grants

Privacy grants to Python or Node can authorize other scripts using that executable.
A dedicated signed wrapper application would provide a clearer identity than
shared runtimes; it is not currently part of this project. Do not disable SIP,
change TCC databases or bypass macOS approval prompts. Recheck after runtime updates.

## Publication checklist

1. Run `uv run pytest -q` and `uv run python scripts/privacy_audit.py`.
2. Inspect all staged files with `git diff --cached` before committing.
3. Check `.gitignore` covers local state, images, logs and credentials.
4. Do not paste live connector URLs or permission-test transcripts into issues.
5. If a credential was ever committed, rotate it and scrub the full history before
   publishing; merely removing it from the latest file is insufficient.
6. Do not enable a repository's public visibility until its owner approves the
   name, license and reviewed contents.

No comprehensive third-party security audit is claimed. See CAPABILITIES.md for
implemented features and unverified client behavior.

## Windows boundary

The Windows launcher registers a task under the current user SID with an
interactive token and least privilege. It does not install a service or request
administrator access. GUI tools require the user's unlocked desktop. UAC secure
desktops, Session 0, and higher-integrity input targets are not supported. Input
preflight is not a promise that a specific app accepts input.

The private storage implementation uses a protected user DACL on Windows and
POSIX 0700/0600 on macOS. A Windows chmod call alone is not confidentiality.
ACL protection failures must stop startup/writes; do not silence them or make
credentials world-readable. Keep state on a local filesystem that supports the
required ACL. A local administrator or malware already running as the user can
still access user secrets. This does not protect against a compromised account.

Tailscale named routes share a listener and configuration revision. Updates are
sequential, conflicts fail closed, and unrelated routes are preserved. Do not
run concurrent route editors; a check/write race cannot be eliminated by the
launcher alone. Never reset or overwrite the shared listener to repair the bridge.

Report suspected vulnerabilities privately through the repository's private
reporting feature once a repository exists. Do not post tokens, passphrases,
private hostnames, screenshots, or personal files in a public issue. No private
reporting destination or security response SLA is claimed before publication.
