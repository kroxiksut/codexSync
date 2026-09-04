# Decisions

## D-001: Sync strategy
We use cold sync only.
Sync happens only when Codex is fully closed.

## D-002: Legal/safety boundary
The project only works with user-local files.
No token handling, no network interception, no reverse engineering.

## D-003: Scope
The project is a utility, not a Codex plugin.

## D-004: Storage
Cloud folder can be OneDrive, Dropbox, Syncthing, Google Drive mirror, etc, or a network folder.

## D-005: Conflict policy
Single-writer assumption.
User should not actively work in Codex on two machines at the same time.

## D-006: Initial platform
Windows first.

## D-007: Operational handoff contract
The expected workflow is strict and manual:
close Codex on machine A, wait for cloud propagation, then sync on machine B.

## D-008: Responsibility boundary for cloud environment
The utility does not verify cloud client process status or free space in cloud/network storage.
These checks are out of scope and owned by the user.

## D-009: CI target matrix for MVP
CI runs on Windows and macOS runners (`windows-latest`, `macos-latest`).
Linux CI is intentionally disabled for MVP until Linux runtime support is explicitly in scope.

## D-010: Preflight diagnostics mode
The CLI provides `doctor` and `preflight` commands (equivalent behavior).
These checks are read-only and validate runtime readiness before sync:
- config/runtime path readiness
- local/cloud/backup/temp readability and directory shape
- Codex process precondition
- manifest data-version compatibility
- session catalog audit (invalid/ambiguous sessions, graph codes)
- read-only SQLite audit
- orphan temp file detection

If any preflight check fails, the command exits with code `5` (`fail-safe`).

Amendment (0.2): the original list promised write-access probes and a
local/cloud mtime-drift probe. Both wrote probe files, one of them inside the
Codex state directory. `OperationKind.DOCTOR` is declared `side_effect_free`,
so the drift probe was dropped and the path checks were reduced to readability
checks. Drift detection may return later, but only in a form that writes
nothing into state.

## D-011: Bounded retry on a locked destination
`os.replace` is the commit step of every mutation. Destinations live in
directories a cloud client, search indexer or antivirus may open at any moment,
which on Windows surfaces as `WinError 5`/`32`/`33` for as long as that handle
lives — usually milliseconds.

Treating this as a hard failure aborted the run and left a `RECOVERY_REQUIRED`
journal for a condition that had already cleared, which is a worse outcome than
waiting. The engine therefore retries a *transient lock* up to five times with
exponential backoff (~1.5s total) before giving up.

This does not weaken `fail-safe`:
- every attempt is the same atomic `os.replace`, so a destination is never
  partially written;
- the destination hash is still verified after the replace;
- the process-safety check is re-proved before each further attempt, so Codex
  starting during the wait still stops the commit;
- errors that are not a transient lock (for example `EXDEV`) are raised on the
  first attempt, unretried.

Retry counts are constants, not configuration: they are a property of the
filesystem behaviour, not a user preference.
