# Synchronisation

**English** · [Русский](../ru/SYNC.md) · [中文](../zh/SYNC.md)

[← Documentation](README.md)

`sync` copies files between the local `.codex` and its mirror in the cloud
folder. It runs only while Codex is closed, and a verified backup of every file
it will replace is written before the first replace. In the window this is the
[Synchronisation](GUI.md#synchronisation) screen.

## Commands

```powershell
codexsync -c config.toml plan              # what a sync would copy; works while Codex is open
codexsync -c config.toml -v plan           # the same, plus the Codex processes that were seen
codexsync -c config.toml sync --dry-run    # every check a sync makes, writes nothing
codexsync -c config.toml sync --apply      # the real sync
```

`sync` without a flag follows `sync.dry_run_default` (`true` in the template),
so a real sync always needs `--apply`.

A plan built while Codex is open is marked `volatile` and is only a preview:
`sync --apply` builds its plan again at the moment it writes.

## What is synced

```toml
[targets]
include_roots = ["sessions", "session_index.jsonl", "skills", "plugins"]

[filters]
exclude_globs = ["**/*.lock", "**/*.tmp", "**/*.temp", "**/tmp/**", "**/cache/**", "**/.cache/**", "**/__pycache__/**", "**/*.log"]
```

- `include_roots` are paths relative to `.codex` (and to the mirror). The
  template lists `sessions` and `session_index.jsonl`, but `sync` skips them (see
  below); `sessions` carries them.
- In `exclude_globs`, `**` means any number of path segments, `*` and `?` match
  within one segment, and a pattern without `/` matches that file name at any
  depth.
- **Semantic-owned paths are never copied by `sync`:** `sessions/`,
  `archived_sessions/`, `session_index.jsonl`, the global project state and
  SQLite. Copying them by modification time can lose a history that grew on both
  machines, so they are handled by [`sessions`](SESSIONS.md), [Guardian](GUARDIAN.md)
  and [`repair-projects`](PROJECTS.md) instead.
- The Settings screen never offers a credential file (`auth.json` and the like)
  for inclusion.
- `sync.session_mode = "last_date_only"` is refused: it can discard branches.

## How files are compared

The previous run's result is kept in a manifest (`state.manifest_file`) that
records both sides. That is how a change on one side is told apart from a
conflict.

`sync.compare`:

- `mtime` (default) — compare size and modification time.
- `mtime_hash_fallback` — the same fast path, but when the times are equal or
  close (within `sync.time_tolerance_seconds`), compare SHA-256 of the content.

`sync.equal_mtime_action` — what to do when times are equal but the files
differ:

- `skip` (default) — copy nothing;
- `prefer_local` — copy the local file to the cloud;
- `prefer_cloud` — copy the cloud file to local;
- `manual_abort` — treat it as a conflict.

## Conflicts

A file changed on both sides since the last run is a conflict. `conflict.policy`:

| Policy | What happens |
|---|---|
| `manual_abort` (default) | Report the conflict and stop before anything is written (exit code `2`) |
| `prefer_cloud` | Take the cloud version |
| `prefer_local` | Take the local version |
| `prefer_newer_mtime` | Take the side with the newer modification time |

## Direction

`sync.direction` decides which side may be written ([D-012](../dev/DECISIONS.md)):

- `bidirectional` (default) — both sides;
- `to_cloud` — only the cloud folder is written; local changes are left alone;
- `to_local` — the mirror image.

A one-way run does **not** record the side it skipped as synchronised: the
manifest keeps the previous entry for every path it did not act on, so the next
bidirectional run still sees the difference instead of concluding that the two
sides agreed. A conflict stays a conflict and is decided by `conflict.policy`.
`validate`, `doctor` and the plan report state the direction.

## Deletions

`sync.delete_policy` decides whether a deletion travels ([D-013](../dev/DECISIONS.md)):

- `never` (default) — a file missing on one side is copied back from the other.
- `propagate` — it is deleted on the other side too, but only when the previous
  manifest proves both sides held it and the surviving side has not changed
  since. Anything else is a conflict.

With `propagate`:

- a verified backup is written before the removal, and the removal is logged
  separately;
- it runs inside the same journalled envelope as every other write, so
  [`recover`](RECOVERY.md#interrupted-mutations) can undo it;
- a dry run deletes nothing;
- the first run after switching it on deletes nothing, because there is no proof
  yet;
- semantic-owned paths are never deleted this way.
