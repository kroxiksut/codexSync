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
exclude_globs = ["**/*.lock", "**/*.tmp", "**/*.temp", "**/tmp/**", "**/cache/**", "**/.cache/**", "**/__pycache__/**", "**/*.log", "skills/.system/**", "plugins/.plugin-appserver/**", "plugins/.remote-plugin-install-staging/**"]
```

- `include_roots` are paths relative to `.codex` (and to the mirror). The
  template lists `sessions` and `session_index.jsonl`, but `sync` skips them (see
  below); `sessions` carries them.
- `include_roots` must name something inside `.codex`: an empty list, `""` or
  `"."` is refused with exit 4, because it would mean the whole directory. A
  config without the key loads (for Guardian alone, say), and `sync` refuses to
  run with it.
- In `exclude_globs`, `**` means any number of path segments, `*` and `?` match
  within one segment, and a pattern without `/` matches that file name at any
  depth.
- **Semantic-owned paths are never copied by `sync`:** `sessions/`,
  `archived_sessions/`, `session_index.jsonl`, the global project state and
  SQLite. Copying them by modification time can lose a history that grew on both
  machines, so they are handled by [`sessions`](SESSIONS.md), [Guardian](GUARDIAN.md)
  and [`repair-projects`](PROJECTS.md) instead.
- `skills/.system/**` is excluded because the Codex runtime installs and
  removes those skills itself. With `delete_policy = "never"` a file it deleted
  would be restored from the mirror on the next run, and deleted again — the
  tree is left to the runtime entirely.
- **A compiled program is never synced**, whatever `include_roots` and
  `exclude_globs` say: a Windows `.exe`/`.dll`, a Linux ELF file, a macOS
  Mach-O file, recognised by its header since on macOS and Linux a program
  usually has no extension. It is built for one platform and one version, it
  goes out of date, and an installer restores it; a copy from another machine is
  useless there or replaces a newer version with an older one.
- `plugins/.plugin-appserver/**` and `plugins/.remote-plugin-install-staging/**`
  are excluded as well, because Codex keeps its own programs there — `codex.exe`, the
  command runner, the sandbox setup — built for its own version and platform.
  On a Mac they are useless, and on another Windows machine they would overwrite
  the version installed there. Plugins themselves are in `plugins/cache/`, which
  `**/cache/**` already excludes: Codex installs them again from its own config.
- A credential file (`auth.json`, `cap_sid`, `.sandbox-secrets` and the like)
  is never copied, at any depth under any root, and the Settings screen never
  offers one for inclusion.
- `sync.session_mode = "last_date_only"` is refused: it can discard branches.

## How files are compared

The previous run's result is kept in a manifest (`state.manifest_file`) that
records both sides. That is how a change on one side is told apart from a
conflict.

The manifest sits in the shared workspace, so it keeps one record **per
machine** (`identity.machine_id`, or the host name without one): what this
machine saw on both sides when it last synced. A single shared record made a
machine take the other machine's last sync for its own, see its older file as a
local edit and copy it over the newer one in the cloud. A manifest written by an
earlier version has no per-machine records; its entries are not attributed to
anyone, so the first run after the upgrade behaves like a first sync — the newer
file wins, a file on one side only is copied, nothing is deleted — and records
this machine's own baseline.

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

A file or a chat changed on both sides since the last run is a conflict. One
rule decides both, in every run — the window, the console and the task at
sign-in ([D-027](../dev/DECISIONS.md)). `conflict.policy`:

| Policy | What happens |
|---|---|
| `prefer_newer_mtime` (default) | Keep the newer copy: a file by its modification time, a chat by the time of its last message |
| `prefer_local` | Keep this machine's copy |
| `prefer_cloud` | Keep the cloud's copy |
| `manual_abort` | Report the conflict and stop before anything is written (exit code `2`) |

The copy not kept is never lost: a file goes into the verified backup before it
is overwritten, and a chat goes whole into the conflict bundle under
`semantic.root_dir` (see [Sessions](SESSIONS.md#applying-a-plan)). A chat is
never merged: one of the two copies is kept. Two chats that end at the same
moment cannot be ordered by time, so `prefer_newer_mtime` still asks about
them, and a choice you recorded on the Sessions page for a chat always outranks
the rule.

For one run, `--conflict-policy` on `sync`, `handoff sync` and `sessions scan`
replaces the setting:

```powershell
codexsync -c config.toml handoff sync --conflict-policy prefer_local
```

In the window, a sync that stopped on a conflict offers the same choice where
it stopped — *Keep the newer copies*, *Keep this machine's*, *Keep the cloud's*
— and *Always decide this way* saves it as `conflict.policy`.

A conflict the policy does not decide — equal times with
`equal_mtime_action = "manual_abort"`, a disputed deletion — stops the run
before any write with exit code `2` under every policy, and so do two paths that
differ only in letter case (`Rules/a.md` and `rules/a.md`), which a
case-insensitive volume stores as one file.

## Direction

`sync.direction` decides which side may be written ([D-012](../dev/DECISIONS.md)):

- `bidirectional` (default) — both sides;
- `to_cloud` — only the cloud folder is written; local changes are left alone;
- `to_local` — the mirror image.

A one-way run does **not** record the side it skipped as synchronised: the
manifest keeps the previous entry for every path it did not act on, so the next
bidirectional run still sees the difference instead of concluding that the two
sides agreed. A file conflict stays a conflict and is decided by
`conflict.policy`. For chats a one-way direction is the decision as well:
`to_cloud` keeps this machine's copy of a chat changed on both machines and
writes nothing into `.codex`, `to_local` keeps the cloud's and writes nothing
into the cloud copy. `validate`, `doctor` and the plan report state the
direction.

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
- a deletion out of an include root that holds no file at all on the side it
  went missing from is a conflict, not a deletion: an empty or missing folder
  there (a cloud folder being downloaded again, a disconnected drive) must not
  empty the same folder on the other side;
- semantic-owned paths are never deleted this way.

## Handing work over

The protocol codexSync is built for is: close Codex on the machine you worked
on, let the cloud deliver, sync on the next machine, and only then start Codex
there. A **handoff** does the two syncs and checks the delivery in between:

```toml
[handoff]
root_dir = "${workspace_root}/handoff"   # optional: empty means "handoff" beside state.manifest_file
enabled = true                           # the watcher task below
delivery_wait_minutes = 15
notify = true
```

```powershell
codexsync -c config.toml handoff status                   # who is working, what was handed off, what arrived
codexsync -c config.toml handoff status --check-delivery  # also hash the cloud copy
codexsync -c config.toml handoff sync                     # load, then hand off; Codex must be closed
codexsync -c config.toml handoff watch                    # what the task at sign-in runs
```

A handoff is one full sync with nobody deciding anything: settings as
`sync --apply --unattended` does them, then chats as `sessions apply` does
them, with the chat plan checked first. Conflicts — of files and of chats —
are decided by [`conflict.policy`](#conflicts); **a conflict the policy leaves
open stops it before the first write**, and a chat is never merged. Only when
both halves finished does the machine record the handoff.

Each machine keeps one small file in `root_dir`: whether it is *working*
(Codex was seen running) or has *handed off*, the id of its last handoff, a
fingerprint of the cloud copy as that handoff left it (a size and SHA-256 per
file, keyed by a hash of the path — no path or chat id is written), and which
handoff of every other machine it has already loaded. Whether a handoff was
loaded is decided by its id, never by comparing two machines' clocks; the
times are there for you to read.

**The watcher** (`enabled = true`) is a task that starts at sign-in and runs
until you sign out:

- at start, with Codex closed, it loads what another machine handed off. If
  the cloud client has not brought all of it yet, it waits up to
  `delivery_wait_minutes`, then gives up and tells you — nothing half-arrived
  is loaded;
- when Codex starts, it marks this machine as working and warns you if another
  machine is still working or its handoff has not been loaded here;
- when Codex closes, it hands off.

It replaces the sync after sign-in: `handoff.enabled` and
`scheduler.sync_at_login` together are a configuration error. What it does is
reported with a system notification (`notify`) and always in the log. A
machine that is shut down with Codex still open hands off at its next sign-in.

`--accept-undelivered` loads a handoff that never fully arrives — use it only
when you know why, for example when a file it named was changed by hand in the
cloud copy since.

**Chats.** A chat both machines have — one you continued on the other machine —
is loaded into `.codex` over its own file, at the path Codex's own catalogue
names for it. A chat started on the other machine is loaded too, at the path it
has there (`[semantic] new_chats = "same_path"`, the default); with
`keep_in_cloud` it stays in the cloud copy (see [Sessions](SESSIONS.md#writing-into-codex)). A handoff says how many new
chats it wrote into Codex — `doctor` then says whether Codex lists them — and
how many stayed in the cloud copy only, rather than calling the work loaded.

**Projects.** Last, the project list is merged: projects the other machine has
are added here, nothing is removed, and the other machine's pins and order are
taken for the projects both have. See
[Projects → Projects between machines](PROJECTS.md#projects-between-machines).
**Synchronise** in the window runs this same full sync.

## History

Every real sync leaves an operation journal, and the history is those journals
read newest first — there is no separate log to keep:

```powershell
codexsync -c config.toml history                  # the last 20 runs of every kind
codexsync -c config.toml history --family sync    # one kind only: sync, sessions, project-sync, chats, restore…
codexsync -c config.toml history --json --limit 0
```

One full sync is three runs, one per part: `sync` (settings files), `sessions`
(chats) and `project-sync` (projects). Each run shows when it started, its
result (and, for a failure, the kind of error — never its message, which may
name files), who started it (`window`, `cli`, `unattended` for the task at
sign-in, or `handoff` for the handoff watcher), what it carried — files or
chats to the cloud, to the local side and deleted, or projects added — and the
backup it made. A journal written before these fields were recorded shows only
its total.

Not listed: a dry run, which writes nothing, and a run that stopped before its
first write — Codex open, or a conflict the policy left open. The window shows
the same list on the **History** tab of *Synchronisation*, and the last run on
*Overview*.
