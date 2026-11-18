# Guardian

**English** · [Русский](../ru/GUARDIAN.md) · [中文](../zh/GUARDIAN.md)

[← Documentation](README.md)

Codex keeps its projects and chat bindings in `.codex-global-state.json`. If
Codex crashes, the machine loses power or blue-screens while that file is being
written, it can come back truncated or empty — and every project with it.

Guardian is the one part of codexSync that runs **while Codex is open**. It only
reads the file and writes verified snapshots into its own directory, which the
configuration must place outside `.codex`, the mirror, backups and temp. In the
window this is the [Snapshot guardian](GUI.md#snapshot-guardian) screen.

## Commands

```powershell
codexsync -c config.toml guardian snapshot --once   # take one snapshot now
codexsync -c config.toml guardian watch             # keep taking snapshots while Codex runs
codexsync -c config.toml guardian list              # snapshots and quarantine; writes nothing
```

To take snapshots on a schedule, set `[scheduler] mode = "guardian_snapshot"`
and run `automation apply` (see [Automation](CONFIGURATION.md#automation)).

## How a snapshot is decided

- **Stable reads.** The file must read the same several times in a row
  (`stable_reads`, 3 by default), so a state caught mid-write never becomes a
  snapshot. `watch` polls every 3 s, waits 2 s after a change, and rescans every
  60 s in case a change was missed.
- **Validation.** Bytes, JSON and referential integrity are checked: NUL bytes, a
  BOM other than one UTF-8 mark at the very start, duplicate keys, bindings that
  point to a project that does not exist.
- **The schema is recognised, not assumed.** Each known shape of the file has its
  own adapter; a state no adapter recognises is not trusted.
- **Suspicious shrink.** A state that lost many projects or bindings compared with
  the last good snapshot goes to **quarantine** instead of replacing it
  (`shrink_min_count`, `shrink_ratio`). While Codex keeps rewriting such a state,
  quarantine keeps two events per drop — the first and the newest, which counts
  the rewrites in between — rather than a copy of every rewrite.
- **Order and visibility.** Snapshots are ordered by a monotonic generation, not
  by the clock; a new one is numbered past every snapshot the store holds, and a
  snapshot file that cannot be read at that moment stops the commit until it can.
  A snapshot is visible only once it is `COMMITTED`, and nothing suspicious can
  ever become `latest-good`. A directory a crash left without `COMMITTED` is
  removed after `staging_retention_hours`.
- **A store error is not the end of `watch`.** A snapshot held by another program
  or a full disk costs that one attempt; `watch` logs it, waits longer each time,
  and tries again.

An honest limit: a scheduled `snapshot --once` does not guarantee the snapshot is
fresh at the moment of a BSOD. It guarantees that the snapshot that exists is
intact and restorable.

## Restoring a snapshot

Putting a snapshot back is a preview first. The preview says how many projects
and bindings the state has now and would have afterwards. The restore needs
Codex closed and the exact plan id, and writes through the same envelope as every
other edit of the global state: operation lock, journal, a verified backup of the
file it replaces, a final process check, and a verified rollback if anything
after the replace fails.

```powershell
codexsync -c config.toml guardian restore --snapshot <snapshot-id>
codexsync -c config.toml guardian restore --snapshot <snapshot-id> --confirm <plan-id> --dry-run
codexsync -c config.toml guardian restore --snapshot <snapshot-id> --confirm <plan-id>
```

Refused: a snapshot that no longer passes validation, one that belongs to another
machine, one in another schema than the file Codex writes today, a live file whose
project state is in a shape codexSync does not recognise yet (`LIVE_SCHEMA_UNKNOWN`
— a newer Codex, not damage), and a restore when the live file is missing.

Restoring an older snapshot does not rewind Guardian: the next snapshot of the
restored file is a new generation and becomes `latest-good`.

## Accepting a new baseline

Guardian compares every state with `latest-good`. When a drop in projects or
bindings is **real** — Codex re-created its projects under new ids, say — every
later state is still compared with the baseline from before the drop, so
`latest-good` would never move again. `doctor` warns when that has happened.

`guardian accept` explains the drop in counts only: projects re-created under a
new id, removed or added, and which of those the lost bindings pointed at. With
`--confirm` it commits the current state as `latest-good`.

```powershell
codexsync -c config.toml guardian accept
codexsync -c config.toml guardian accept --confirm <plan-id>
```

- Only a shrink can be accepted, never a state that fails validation — or a
  **schema change**: when the state is in another known schema than the baseline
  (a new Codex build, a baseline older than schema tracking), the watcher cannot
  compare the two and holds every state back. Such a state can be accepted when it
  passes every check under its own schema (`BASELINE_SCHEMA_CHANGED`).
- A state with no project and no binding has no schema to compare: once every
  project is gone the file looks like the oldest shape, simply because nothing
  of the newer one is left in it. So losing every project is compared by counts
  and held back as a drop, never offered as a schema change, and `guardian
  restore` may put the projects back from a snapshot taken before.
- The plan id pins the drop, not the file's bytes, so it can be confirmed while
  Codex keeps rewriting the file.
- The snapshot is marked `SHRINK_ACCEPTED` (or `SCHEMA_CHANGE_ACCEPTED`), and
  retention keeps both it and the baseline it overrode ([D-014](../dev/DECISIONS.md)).

## Settings

```toml
[guardian]
root_dir = "${workspace_root}/guardian"   # outside .codex, the mirror, backups and temp
max_state_bytes = 67108864                # the largest state accepted (1 MiB – 1 GiB)
shrink_min_count = 2                      # a shrink is suspicious from this many lost items…
shrink_ratio = 0.25                       # …and this share of them
retention_days = 30                       # 0 disables the limit
max_snapshots = 100                       # 0 disables the limit
quarantine_retention_days = 30
staging_retention_hours = 24
poll_interval_seconds = 3
debounce_seconds = 2
stable_reads = 3
stable_read_interval_seconds = 0.5
fallback_scan_seconds = 60
once_timeout_seconds = 120
```
