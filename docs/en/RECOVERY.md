# Backups and recovery

**English** · [Русский](../ru/RECOVERY.md) · [中文](../zh/RECOVERY.md)

[← Documentation](README.md)

Every command that writes takes a verified backup of what it will replace, and
keeps a journal while it works. This page covers both, and the way out of a write
that stopped halfway. In the window these are the [Backups](GUI.md#backups) and
[Recovery](GUI.md#recovery) screens.

## Backups

```toml
[backup]
backup_before_overwrite = true   # a write with false is refused
retention_days = 30
max_backups = 0                  # 0 = unlimited
compression = "none"             # none | zip
```

- Backups go to `paths.backup_dir`, as a directory tree (`none`) or a single
  `.zip` file (`zip`).
- Every new backup carries a verified `codexsync-backup-v1` manifest. An automatic
  restore picks only committed backups with a manifest.
- Backups are pruned by `retention_days` and `max_backups`. The one exception is a
  **conflict bundle** — the losing branch of a session divergence — which lives
  under `semantic.root_dir` and is never pruned, so a divergent history is never
  the only copy in something that expires.

## Restoring a backup

```powershell
codexsync -c config.toml restore --dry-run                              # preview, writes nothing
codexsync -c config.toml restore --apply                                # the latest backup into the local .codex
codexsync -c config.toml restore --from <snapshot-name> --apply         # a specific backup (directory or .zip)
codexsync -c config.toml restore --target cloud --apply                 # into the cloud mirror instead
```

- A restore needs Codex closed, and backs up whatever it replaces first.
- The dry run verifies every file against the backup's manifest.
- A legacy backup without a manifest needs an explicit `--from` plus
  `--allow-legacy-snapshot`, and cannot restore semantic-owned state (sessions,
  the session index, the global state).

To put back `.codex-global-state.json` from a Guardian snapshot, see
[Guardian → Restoring a snapshot](GUARDIAN.md#restoring-a-snapshot).

## Interrupted mutations

`sync`, `restore`, `sessions apply`, `chats move`, `repair-projects apply`,
`project-move apply` and the Guardian restore all write a durable journal. If one
is interrupted — a power cut, a forced shutdown — the journal stays open, and
every later write refuses to start until it is closed. That block is what keeps a
half-applied state from being changed further. Two commands close it.

**Read the evidence first** (no side effects):

```powershell
codexsync -c config.toml recover inspect <operation-id>
```

**Resume** — retry the interrupted command:

```powershell
codexsync -c config.toml recover resume <operation-id>          # report only
codexsync -c config.toml recover resume <operation-id> --apply
```

`resume` does not replay the lost plan. Every destination is replaced atomically,
so after a crash each file is either fully old or fully new, and running the
original command again re-plans against what is actually on disk. `resume`
verifies that the operation's backup is still intact, then closes the journal so
the command can run again.

**Roll back** — restore the backup that operation created before it wrote anything:

```powershell
codexsync -c config.toml recover rollback <operation-id> --target cloud
codexsync -c config.toml recover rollback <operation-id> --target cloud --apply
```

- `--target` (`local` or `cloud`) is required and never inferred: one sync can
  back up files from both sides, and the backup manifest records only relative
  paths, so the side cannot be proven from the backup alone.
- Both commands default to a dry run and require Codex to be closed.
- `rollback` releases the journal only after the backup has been verified against
  its committed manifest, so a rollback that cannot run leaves the block in place.
- A backup with no committed manifest proves the commit phase was never entered —
  the backup set is stamped before the first replace — so there is nothing to undo
  and the journal is simply closed.
