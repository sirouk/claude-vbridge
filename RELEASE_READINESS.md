# Release readiness: 0.2.0 preview

This is an MIT-licensed cross-platform **preview**, not a claim that every desktop
workflow or Claude Voice account has been validated.

## Real CI evidence

Source candidate: `312b9d4d77b79261fafa0161827aaf86ccbb2e2b`.
Run: https://github.com/sirouk/claude-vbridge/actions/runs/37537563760

| Runner | Python | Offline tests | Full job |
| --- | --- | --- | --- |
| macOS | 3.12 | 107 passed, 12 skipped | Passed |
| macOS | 3.13 | 107 passed, 12 skipped | Passed |
| Windows | 3.12 | 115 passed, 4 skipped | Passed |
| Windows | 3.13 | 115 passed, 4 skipped | Passed |

All four jobs passed locked dependency installation, lint, formatting, privacy
checks, wheel/source builds and package-content checks. Windows jobs also parsed
the PowerShell launcher. Privacy guard: 44 candidate files, zero findings.
Platform-specific skips are intentional; they are not passes. The tests include
actual Windows process creation, jobs, private storage and native failure paths.
GUI tests mock actions and never capture a runner's screen or type into an app.

### Native Windows security/process coverage

The passing candidate includes owner-only storage checks, rejected extra-ACE and
unprotected ACLs, foreign-owner rejection (subject to test-token privilege),
junction/reparse rejection, hardlink rejection without external target mutation,
exclusive lock recovery after owner death, Job Object child cleanup after owner
death, exit-code259 handling and suspended-process assignment failure cleanup.
This is targeted regression evidence, not a comprehensive security certification.

Early CI correctly exposed fixture mistakes: administrator-owned temporary
folders, reopening an exclusively held lock, and insufficient PowerShell startup
budget. Fixes create private test directories and preserve strict ownership
checks. The storage boundary no longer resolves away symlinks/junctions upstream.

## Live macOS baseline

On the existing operator installation, authenticated public OAuth/direct-shell
checks, Desktop MCP backend connectivity and privacy preflights passed after
cross-platform integration. No personal screen/clipboard capture, clicks, typing,
or outgoing messages were used in that regression. Real machine details and
credentials are kept outside the repo.

## Still unverified manually

- Windows installation/logon task as a normal non-admin user, restart and stop.
  The installer/task contract is unit-tested, not end-to-end desktop-tested.
- Windows screenshot, focus, typing, keys, mouse, clipboard and coordinate mapping
  in a connected, unlocked interactive desktop.
- Windows live public Funnel/OAuth with an unrelated shared route preserved.
- Controlled visual interaction on each OS. Permission preflight is not action
  delivery proof.
- Claude Voice custom-connector routing. Text connector discovery and OAuth
  success do not prove Voice support.

For these tests, use a blank/synthetic desktop and disposable files; do not return
personal screen contents or credentials to a test transcript. Record results
without usernames, SIDs, real hostnames, tokens or private paths.

## Publication boundary

License and third-party notices are included; no installed third-party Desktop
extension code is bundled. Candidate source and full Git history were checked for
known private values before publication. Private settings, logs, screenshots,
OAuth state and machine-specific test evidence are excluded. Repository publication
must not be described as proof of full Windows GUI or Claude Voice support.
