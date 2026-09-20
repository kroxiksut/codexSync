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

## D-012: One-way sync directions are configurable
`sync.direction` accepts `bidirectional` (the default), `to_cloud` and
`to_local`. The one-way modes exist because a machine is often only a source or
only a destination during a handoff, and copying the other way at that moment
is exactly what a person wants to prevent.

Nothing about the cold-sync model changes: a one-way run still needs Codex
stopped, still backs up before overwriting and still refuses on uncertainty.
Two properties keep it honest:

- **A skipped action is never recorded as synchronised.** The manifest holds a
  two-sided fingerprint so that a one-sided change can be told from a conflict.
  Writing the skipped side's current fingerprint would make the next
  bidirectional run believe both sides had agreed, and it would then take the
  older file for the newer one. For every path the direction skipped, the
  previous manifest entry is carried over unchanged.
- **A conflict stays a conflict.** When both sides changed, a one-way run does
  not quietly overwrite the other side; `conflict.policy` decides, exactly as
  in a bidirectional run.

`validate`, `doctor` and the run report state the direction, so a run that
copied nothing in one direction says why.

## D-013: Deletions may be propagated, but only against proof
`sync.delete_policy` accepts `never` (the default) and `propagate`.

Under `propagate`, a file missing on one side is deleted on the other **only
when the previous manifest proves that both sides held it and the surviving
side has not changed since**. Anything else — no manifest, an unknown path, a
side that changed after the recorded fingerprint — is a conflict, not a
deletion. The first run after enabling it therefore deletes nothing, because
there is no proof yet.

The safety rules are unchanged and apply in full (`AI_RULES` 3 and 6):

- a verified backup of the file is created before it is removed, and the
  removal is logged as its own dangerous action;
- the deletion runs inside the same mutation envelope as every other write —
  operation lock, journal, gate re-checked before each step — so `recover` can
  roll it back from that backup;
- a dry run deletes nothing;
- semantic-owned paths (`sessions/`, `archived_sessions/`, the session index,
  the global state, SQLite) are never deleted by `sync`. Moving a session to
  the archive is a separate question (`ARCHIVE_TRANSITION`) and this setting
  does not open it.

## D-014: A person may accept a suspicious shrink as Guardian's new baseline
Guardian quarantines a state whose project or binding count fell sharply
(`guardian_shrink`), and nothing suspicious becomes `latest-good` on its own.
That rule stays. What it did not cover is a drop that is real: the comparison is
always against `latest-good`, so after one every later state is suspicious too
and `latest-good` never moves again. Observed on 2026-09-13, when the desktop
build re-created all 16 projects under new ids and the 6 bindings naming the old
ids disappeared; the store stayed on the 2026-09-05 snapshot.

`guardian accept` is the sanctioned way out, and its limits are the decision:

- Only `SUSPICIOUS` with a shrink code can be accepted. `INVALID` and
  `INDETERMINATE` cannot, whatever is confirmed: acceptance overrides a
  judgement about counts, never an integrity check.
- The preview explains the drop in counts only (projects replaced — gone while
  a project with the same roots exists under a new id — removed and added; lost
  bindings attributed to each). Names, roots and thread ids stay in core, as
  everywhere else in Guardian.
- The plan id covers the baseline (id and hash), the counts, the shrink codes
  and a digest of which bindings and projects went and why, but not the state's
  own hash. Codex rewrites the file every few minutes; an id that followed the
  bytes could not be confirmed while Codex runs, which is when Guardian works. A
  different drop is a different id.
- The acceptance runs the watcher's pipeline under the runner lock (stable
  reads, validation, shrink assessment) and commits through the ordinary store
  path. The manifest is `PASS_WITH_WARNING` with the shrink codes plus
  `SHRINK_ACCEPTED`, and names the overridden baseline as its predecessor.
- Retention never prunes an accepted snapshot or the baseline it overrode.
- It writes only into the Guardian root, so it is allowed while Codex is open.
  `doctor` warns when shrink quarantines are newer than `latest-good`.

## D-015: A record-format rewrite by Codex is a conflict decided in bulk
The session model assumed a history only grows: a branch changes at its end or
not at all, which is what makes a prefix a fast-forward and anything else a
divergence. The September 2026 desktop build broke that itself. It rewrote every
existing session file into numbered records (`ordinal` on each record, messages
moved into `item` payloads, `session_meta` carrying `session_id` and
`history_mode`), kept each file's mtime, and dropped records on the way: turns
the person had rolled back, repeated `session_meta` records, injected
instructions and most of the guardian sub-agent reviews. Observed on 2026-09-19:
all 277 local sessions rewritten, the mirror written on 2026-09-05 still holding
242 of them in the old format, and every one of those a
`DIVERGED_NO_COMMON_RECORDS` that refused `sessions apply` as a whole.

What was decided:

- It stays a conflict. The two copies are not the same history, and proving
  "same history, re-encoded" would mean trusting a projection of one format onto
  the other that the dropped records already contradict. The catalogue records
  each branch's record format (`legacy`, `ordinal`, `mixed`) and the latest
  record time; a conflict between two formats is labelled `FORMAT_MIGRATION`
  with the newer side, and nothing else about it changes — not its conflict id,
  not the fact that it blocks.
- One explicit decision covers all of them. `sessions resolve
  --format-migrations` writes an ordinary pinned resolution keeping the newer
  side for each. It never decides a conflict whose older copy has a record later
  than anything in the newer one (`OLDER_FORMAT_HAS_LATER_RECORDS`): that copy
  may hold work done elsewhere before the upgrade.
- The loser is kept, alone and compressed. A conflict bundle stores both raw
  branches; here the winner is what the destination is about to hold, and both
  copies of 242 sessions would have been about two gigabytes written into a
  folder the config allows to be in the cloud. `superseded/<branch sha256>/`
  holds the old branch in the mirror's container, verified by decompressing it
  before the directory is committed. It is the only surviving copy of what the
  rewrite dropped, and nothing prunes it.
- `doctor` reports both sides' formats from each file's first record
  (`session_format`), so a rewrite that reached one side is a known step rather
  than two hundred unexplained conflicts.
