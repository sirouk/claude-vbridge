# Release readiness ledger

This ledger records evidence, not marketing claims. No repository publication,
remote commit, release upload, or live machine configuration change is implied.

| Gate | Evidence / status |
| --- | --- |
| MIT license and generic 2026 contributors copyright | Included in checkout |
| Third-party and external extension boundary | THIRD_PARTY.md; not a full legal audit |
| macOS/Windows implementations | Source present; implementation is not runtime verification |
| macOS mocked/admin offline checks | Run locally during preparation; exact final results must be recorded below |
| Windows mocked tests on macOS | Contract checks only; do not prove Windows APIs work |
| CI matrix macOS/Windows Python 3.12/3.13 | First candidate run completed: macOS jobs passed, Windows fixtures failed; correction runs pending |
| Wheel and sdist/privacy gates | Commands supplied; record actual final local results below |
| Windows normal-user Task Scheduler / logon | **Not verified on Windows** |
| Windows filesystem DACL and process-tree lifecycle | **Not verified on Windows** |
| Windows screenshot, focus, text/key/mouse, clipboard | **Not verified in an interactive Windows session** |
| Windows live public OAuth / Funnel shared routes | **Not verified** |
| Fresh macOS interactive/public regression | Requires explicit owner-approved run; prior checks do not prove this release |
| Claude Voice connector routing | **User-side unverified**; text/OAuth success does not establish voice support |
| Publication review | Pending owner approval of repository target and reviewed contents/history |

## Required evidence before a cross-platform verified release

1. Run all four real CI matrix jobs successfully. Record workflow/run links and
   commit identifier here. Never call a workflow definition a passing result.
2. On a normal-user Windows desktop, test launcher install/start/status/restart,
   logon trigger, explicit replacement/backup, stop/kill switch, ACL fail-closed
   behavior, and jobs with a synthetic child process. Confirm no admin/service is
   required. Keep real usernames/SIDs and private paths outside this document.
3. On each OS, test UI with an empty synthetic app and reviewed blank screen.
   Confirm coordinate mapping and visible result. Do not capture personal data.
4. Run owner-approved public OAuth/direct checks and verify a shared unrelated
   Tailscale route survives. Do not publish real hostnames, tokens or passphrases.
5. Test Claude text connector then Voice from the intended client/account.
   Record limitations without asserting unsupported client features.
6. Build wheel/sdist, run privacy checks and manually review candidate files and
   history. Approve the public repo name/license before any publication.

## Local preparation results

Recorded during local preparation on macOS:

- Admin and release tests: **19 passed** (`uv run --locked pytest -q
  tests/test_admin.py tests/test_release.py`). Windows task behavior is mocked.
- Scoped Ruff lint/format: passed for the operator and release Python files.
- macOS launcher syntax: `bash -n mac-bridge.sh` passed.
- Privacy guard: **43 candidate files, 0 findings** at this checkpoint. Candidate
  count can grow as parallel implementation completes; rerun before publication.
- `uv build`: built the 0.2.0 wheel and source distribution successfully.
- `scripts/release_check.py`: 1 wheel and 1 sdist passed content checks.

These are local snapshots, not an assertion that later source changes are green.
Windows runtime and GitHub CI remain unverified even when all tests pass on the
development Mac.

## Private candidate CI checkpoint

The first real cross-platform matrix exposed Windows-only fixture failures.
Both macOS/Python jobs passed. Windows created processes and ran many core tests,
but the complete Windows suite did not pass. Corrections preserve strict
foreign-owner rejection, stop reopening an exclusive lock, and allow for
PowerShell startup in timeout tests. This is not yet a passing Windows release.
Additional native Windows ACL/reparse/process-owner tests are being added.
