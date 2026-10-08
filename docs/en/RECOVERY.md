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
- Backups are pruned by `retention_days` and `max_backups` — only this machine's
  own snapshots, never another machine's or a folder that merely sits in
  `backup_dir`, and never a snapshot an unfinished operation of this machine
  still needs for [`recover`](#interrupted-mutations). The one exception is a
  **conflict bundle** — the losing branch of a session divergence — which lives
  under `semantic.root_dir` and is never pruned, so a divergent history is never
  the only copy in something that expires.

## Restoring a backup

```powershell
codexsync -c config.toml backups list                                  # the snapshots and their names
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
- Backups taken by 0.1 have no manifest, so they are such legacy backups:
  settings files only. If you want chats to be restorable too, take a new
  backup with this version — a sync takes one before every overwrite, and a
  copy of `.codex` ([Configuration](CONFIGURATION.md#copies-of-codex)) holds
  everything.

To put back `.codex-global-state.json` from a Guardian snapshot, see
[Guardian → Restoring a snapshot](GUARDIAN.md#restoring-a-snapshot).

## Interrupted mutations

`sync`, `restore`, `sessions apply`, `chats move`, `repair-projects apply`,
`project-move apply` and the Guardian restore all write a durable journal. If one
is interrupted — a power cut, a forced shutdown — the journal stays open, and
every later write refuses to start until it is closed. That block is what keeps a
half-applied state from being changed further. Two commands close it.

**A run that stopped before replacing anything closes by itself.** A journal
still at `PREPARED` or `BACKED_UP` proves no file was replaced — the usual cause
is a cloud client holding the journal file while uploading it, so the write that
would have closed it was refused too. The next write on the same machine closes
such a journal (history reason *Abandoned*) and goes ahead. Two kinds still wait
for you: a run that entered the commit phase (`COMMITTING`,
`RECOVERY_REQUIRED`), and another machine's run — the journals folder is in the
shared workspace, and that run may still be going.

**See what is open** (no side effects):

```powershell
codexsync -c config.toml recover list           # open journals, and what closes each
codexsync -c config.toml recover list --all     # finished ones too; --json for scripts
```

In the window the same list is the **Recovery** page; a synchronisation stopped
by an open journal offers **Open Recovery**, which lands on that journal.

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
codexsync -c config.toml recover rollback <operation-id>
codexsync -c config.toml recover rollback <operation-id> --apply
```

- Each file goes back to the side it was backed up from: one sync backs up files
  of both sides, and the backup records the side of each one. `--target`
  (`local` or `cloud`) is optional; given, it must be the only side the backup
  holds, or the rollback is refused rather than writing a file into the wrong
  root. It is needed only for a `restore` whose backup was written before sides
  were recorded; such a backup from a `sync` is refused — restore it by hand
  with `restore --from`.
- The global state (after `chats move`, `repair-projects apply`, `project-move
  apply` or a Guardian restore) goes back through the same careful write as the
  original: verified backup of the current file, validation, process check.
- A `sessions apply` is not rolled back: use `resume`, then scan and apply again.
  A transfer only ever extends a history or keeps the branch it replaced in a
  conflict bundle, so re-running it loses nothing.
- Both commands default to a dry run and require Codex to be closed.
- `rollback` checks everything that could refuse — the backup against its
  committed manifest, the sides, the global state's validity — before it releases
  the journal, so a rollback that cannot run leaves the block in place.
- If the backup the journal names is gone (removed by retention, possibly by
  another machine sharing the backup folder), the rollback is refused: what was
  replaced can no longer be proven. `resume` still closes the journal.
- A journal that never reached its commit phase, or a backup with no committed
  manifest, proves nothing was replaced — both are recorded before the first
  replace — so there is nothing to undo and the journal is simply closed.
- Only one writing command works on one Codex folder at a time, whatever its
  kind; a second one stops with exit code `5` instead of interleaving its writes.
