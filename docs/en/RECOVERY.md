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
codexsync -c config.toml restore --apply                                # this machine's latest backup of the local .codex
codexsync -c config.toml restore --from <snapshot-name> --apply         # a specific backup (directory or .zip)
codexsync -c config.toml restore --target cloud --apply                 # into the cloud mirror instead
```

- A restore needs Codex closed, and backs up whatever it replaces first.
- Without `--from`, only this machine's own backups count, and only one taken
  of the side being restored (`--target`): the backup folder is shared, and the
  newest backup in it may be another machine's, or a backup of the other side.
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

**Another machine's open journal holds back only what it concerns.** Sync,
chat transfer and restore wait for it, since they write the shared copy. A
change to this machine's own Codex state — moving a chat, the project list,
`repair-projects`, a Guardian restore, a project move, chat names, the chat
catalogue and `codex repair` — goes ahead with a warning in the log: a laptop
that stopped half-way through a sync no longer keeps this computer from
repairing a Codex that does not start. A journal whose machine cannot be told,
or that cannot be read, still holds back everything.

**See what is open** (no side effects):

```powershell
codexsync -c config.toml recover list           # open journals, and what closes each
codexsync -c config.toml recover list --all     # finished ones too; --json for scripts
```

A cloud client's copy of a journal file (such as `<id> (1).json`) blocks nothing
while the journal it copies is readable, and is not listed. A copy whose original
is gone still blocks; the list names it so you can check it and remove it.

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

- Only the machine that ran the operation can roll it back: its backup holds
  that machine's files, and the local side here is another `.codex`. Another
  machine's journal can only be closed here with `resume`, once that run is
  certainly over.
- Every file the backup holds goes back, even one that today's `include_roots`
  or `exclude_globs` would leave out: the rollback undoes what the operation
  replaced under the settings it ran with.
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
  committed manifest, the sides, the global state's validity — before it writes
  anything, and closes the journal only after every file is back. A rollback
  that cannot run, or that Codex starting interrupts, leaves the block in place;
  run it again once Codex is closed.
- If the backup the journal names is gone (removed by retention, possibly by
  another machine sharing the backup folder), the rollback is refused: what was
  replaced can no longer be proven. `resume` still closes the journal.
- A journal that never reached its commit phase, or a backup with no committed
  manifest, proves nothing was replaced — both are recorded before the first
  replace — so there is nothing to undo and the journal is simply closed.
- Only one writing command works on one Codex folder at a time, whatever its
  kind; a second one stops with exit code `5` instead of interleaving its writes.

## When Codex does not start

```powershell
codexsync -c config.toml codex check
codexsync -c config.toml codex repair
codexsync -c config.toml codex repair --confirm-plan <plan-id>
```

`codex check` reads Codex's state and lists what is wrong, whoever caused it —
in the window, *Recovery → Codex state → Check Codex*. It only reads, and may
run while Codex is open. Each finding names its repair:

| Finding | What it means | Repair |
|---|---|---|
| `CATALOGUE_REBUILD_STUCK` | Codex will not start: a rebuild of its chat list was ended part-way and nobody is doing it. Codex shows "could not load your organization's settings". | `codex repair` |
| `CATALOGUE_REBUILD_PENDING` | Codex rebuilds its chat list on its next start, walking every chat file; leave it open until the chats appear. | `codex repair` skips the rebuild |
| `CATALOGUE_REBUILDING` | Codex is rebuilding right now. | Leave it open |
| `CATALOGUE_MISSES_CHATS` | Chat files are in place that Codex does not show. | `sessions catalogue` |
| `CATALOGUE_STATUS_UNKNOWN` | Codex's chat list reports a rebuild status this version does not know; nothing about it is judged. | Nothing: `codex repair` leaves it alone |
| `CATALOGUE_STATUS_MISSING` | Codex's chat list holds no rebuild status; Codex writes it itself. | Nothing: `codex repair` has nothing to do |
| `GLOBAL_STATE_MISSING`, `GLOBAL_STATE_INVALID` | Projects, pins or which chat is in which project are gone or torn apart. | `guardian restore` |
| `GLOBAL_STATE_UNRECOGNISED` | The global state is in a shape this version does not know (a Codex with no project yet writes one). Codex is not affected; projects are not carried until it is known. | Nothing to repair |
| `OPEN_JOURNAL` | A codexSync operation stopped part-way and blocks every write. | [Interrupted mutations](#interrupted-mutations) |

How a rebuild gets stuck: Codex builds its chat list from the chat files while
it starts, and opens only when it is done. If that start is ended part-way —
the window gave up, or Codex was closed — the rebuild stays marked as running,
and every later start waits for it and exits. `codex repair` sets it back to
complete: one row, with Codex closed, after a verified backup of the database,
journalled like every write (family `codex-repair`). Where codexSync's own
request started the rebuild, the exact row from the backup it took then is put
back. Codex then starts with the chats it already listed; ask for a rebuild
again with `sessions catalogue` when you can leave Codex open until it
finishes. `doctor` reports the same state as `codex_startup`.

## When Codex refuses every command

On Windows, Codex runs an agent's commands in a sandbox. Before each command it
puts a deny entry on the folders of the project that the agent must not touch —
`.codex`, `.agents`, `.git` — and to do that you have to be able to change
their permissions. A folder created by a command *inside* the sandbox belongs
to the sandbox account (`CodexSandboxOffline`), and one created from a shell
run as administrator belongs to Administrators; then you may not change its
permissions, the setup fails, and after a while Codex refuses every command in
every chat of that project (`setup refresh had errors`).

`doctor` checks each project's `.codex`, `.agents` and `.git` and reports any
it cannot protect as `codex_sandbox`, with the owner and, when Codex's own
sandbox log says the same, that too. In the window the Overview names them and
*Copy commands* puts the fix on the clipboard. codexSync changes no permission
itself: the folders are yours. Run the commands in PowerShell as
administrator; each changes only the owner, and `/L` works on a link itself
instead of what it points to. The names are in single quotes, where PowerShell expands
nothing, so a `$` or a backtick in a folder name stays part of the name:

```powershell
icacls 'D:\Projects\app\.codex' /setowner 'PC\you' /C /L
icacls 'D:\Projects\app\.git' /setowner 'PC\you' /T /C /L
```

Only `.git` is changed recursively: `.agents` and `.codex` may hold junctions
into folders other agents use (Claude Code reads `.agents/skills`). Give such a
folder back by changing its owner — never delete or recreate it. The deny
entries Codex leaves on these folders after a successful setup are its
protection working: they deny the sandbox, not you.
