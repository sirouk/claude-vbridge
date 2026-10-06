# What this gives Claude Voice

## The problem

A local stdio MCP tool installed in Claude Desktop is not automatically a remote
connector available to a web or mobile conversation. That can leave a voice chat
able to talk about work but unable to carry out the local actions you want.

This project puts an authenticated remote MCP interface in front of the machine's
local capabilities. **Claude's current conversation does the reasoning; the machine
executes tool calls.** It does not insert a second language model, replace Claude's
voice engine, change model effort, or connect to an active Desktop conversation.

## What you can ask for

These are example requests, not promises of a successful voice session. Claude
must have the connector enabled and must support tool calls in the chosen mode.

| Request | How it works | Requirements / limits |
| --- | --- | --- |
| “Make a note with this idea.” | Installed Notes MCP extension | Extension installed; Automation grant; writes have side effects |
| “Read my reminders and calendar.” | Installed EventKit MCP server | Read grants; returns personal data to the conversation |
| “Draft a message, then ask me before sending.” | Installed Messages tools | Contacts/Automation/File access; confirmation is conversational, not enforced |
| “Find and update that local document.” | File tools or shell | Local user permissions; no sandbox |
| “Run my project tests and tell me when they finish.” | `start_job` and `job_status` | Concrete command; bounded output; no automatic completion push into chat |
| “Stop that job.” | `cancel_job` | Stops owned process group; cannot undo actions or catch detached sessions |
| “Look at my screen.” | `screenshot` returns a JPEG MCP image | UI opt-in and platform permission/session access; screen content leaves machine; client must accept images |
| “Open the app and click that button.” | App focus, screenshot, `click` | UI opt-in; Accessibility; coordinate mapping; observe result after input |
| “Type this into the focused field.” | `type_text`, `press_key` | Confirm focus; may cause irreversible/external actions |
| “Put this text on my clipboard.” | Clipboard tools | UI opt-in; reading may disclose passwords or other sensitive text |
| “Use the other local tool I installed.” | Namespaced Desktop MCP proxy | Enabled stdio config/extension; backend credentials and permissions |

## Direct tool groups

### Commands and files

`run_command`, `read_file`, `write_file`; macOS also has `run_applescript`. Direct execution,
no additional model call. Commands have a short timeout and output bound. File
writes require explicit overwrite for existing files. Unrestricted shell can bypass
these convenience-tool bounds or launch detached processes: it is full authority.

### Background jobs

`start_job`, `job_status`, `list_jobs`, `cancel_job`. A job returns an opaque id,
so a voice request need not wait for a long command within one MCP HTTP request.
Up to four run concurrently; the waiting queue is bounded. Status can wait briefly
and return a recent output tail. Records are private local state, not repo content.
Timeouts/cancellation/shutdown terminate the owned platform process tree. Commands
are not replayed after restart; unfinished records become interrupted. Side effects
already performed are not reversible, and intentionally detached sessions escape
a process group. Job status persists; no server can promise delivery of a spoken
notification when Claude is not polling or the voice session has ended.

### Optional visual interaction

`screenshot`, `list_displays`, `list_apps`, `focus_app`, `click`, `type_text`,
`press_key`, `read_clipboard`, `write_clipboard`. Disabled in new installs until
`ui_enabled=true` in private settings. `permission_status` is always available.

Screenshots are resized JPEGs, returned to the caller and not retained as a local
screen history. Metadata maps resized image coordinates to global display points.
Click coordinates use points; Retina pixels and resized image pixels differ.
Posting a click/key does not prove the intended element received it. Observe the
result and avoid repeated clicks if the outcome is uncertain.

The UI switch is an accidental-use/catalog control, **not a security sandbox**:
an authorized client with shell access can invoke local OS tools independently.

### Desktop tool proxy

Enabled stdio server configuration and enabled extension manifests are discovered
at startup. Tools retain their original schemas under backend namespaces. The
bridge opens its own persistent connections; it does not copy the Desktop login,
chat history, cookies or private Anthropic account credentials. MCP servers can
still have their own local credentials, and macOS grants may not transfer.

## What is verified vs unknown

- Direct execution, OAuth, persistent stdio dispatch, file operations and permission
  preflights have been exercised. Job lifecycle and mocked visual-tool contracts
  have automated regression coverage.
- Image encoding, capture cleanup and denied-permission behavior are tested without
  capturing a real personal screen. UI typing/click delivery needs a controlled
  owner-approved live test.
- **Custom connector routing in Claude Voice remains user-side unverified.** Text
  connector setup is not evidence of voice tool support. This project cannot patch
  Anthropic's voice pipeline or force unsupported tools to be registered.
- There is no enforced human-approval policy around shell/files/installed tools.
  Always review outgoing messages, destructive work and private data disclosure.

## Performance choices

Long-lived stdio clients avoid restarting local servers for each call. Calls to a
single backend are serialized; independent backends can run concurrently. Background
jobs avoid long HTTP waits. Output/image sizes and queues are bounded. There is no
extra inference latency for direct tools. Actual end-to-end voice latency includes
Anthropic reasoning, connector routing, network and application response time; a
same-Mac HTTP benchmark does not establish mobile performance.

## Platform verification boundary

| Area | macOS | Windows |
| --- | --- | --- |
| Launcher | User LaunchAgent; prior local use | Per-user interactive scheduled task; mocked only so far |
| UI coordinates | Global points / Retina mapping | Physical pixels / virtual desktop |
| UI permission gate | Accessibility / Screen Recording TCC | Active input desktop; UIPI still applies |
| AppleScript / Apple app integrations | macOS only, installed grants required | Unavailable; use installed Windows tools |
| Offline CI Python 3.12/3.13 | Workflow supplied; pending actual run | Workflow supplied; pending actual run |
| Interactive delivery and public OAuth | Earlier local checks are not a fresh release proof | Not verified on a real Windows host |

No universal cross-platform app automation is promised. Windows screenshot/input
code and platform dispatch have automated mocked contracts. Only real Windows
execution can establish API/runtime compatibility, Task Scheduler installation,
filesystem ACL behavior, and live GUI delivery. See the release ledger.
