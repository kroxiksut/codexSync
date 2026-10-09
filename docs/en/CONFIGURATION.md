# Configuration

**English** · [Русский](../ru/CONFIGURATION.md) · [中文](../zh/CONFIGURATION.md)

[← Documentation](README.md)

Everything codexSync does is decided by one `config.toml`. Create it with
`codexsync init-config` or on the window's *First run* screen, and edit it by
hand or on the *Settings* screen. The full template, with a comment on every
key, is [config.example.toml](../../config.example.toml).

Every command that writes also checks that the config does not ask for anything
the safety rules forbid; such a config is refused with exit code `4`.

## Paths and the workspace

```toml
[identity]
machine_id = "desktop"

[paths]
workspace_root_dir = "D:/Cloud/codexSync"
local_state_dir = "C:/Users/me/.codex"
cloud_root_dir = "${workspace_root}/sync"
backup_dir = "${workspace_root}/backups"
temp_dir = "${workspace_root}/.tmp"
```

- **`machine_id`** is unique and permanent. Backups, snapshots, plans and mapping
  rules on the other machine refer to it; two machines under one name mix their
  data.
- **`workspace_root_dir`** is a folder your cloud client syncs between machines.
  `${workspace_root}` in any other path stands for it.
- **`local_state_dir`** is Codex's own state directory. It is read, written only
  during a cold operation, and never created. If it does not exist, codexSync
  uses `CODEX_HOME` or `~/.codex` instead, and every command first checks that
  none of its own folders below (nor Guardian's, the semantic store's or the
  copies') lies inside the folder it actually uses — otherwise it stops with
  exit 4 before reading or writing anything.
- **`cloud_root_dir`** is the mirror of the state in the cloud folder.
- **`backup_dir`** holds the backups taken before anything is replaced.
- **`temp_dir`** holds the operation lock and the mutation journal, and is
  where `restore` unpacks a snapshot. A copy itself is staged and verified in
  the folder of its destination: an atomic replace only works within one volume,
  and `.codex` is often on another drive than the cloud folder.

A relative path is resolved against `workspace_root_dir`, or against the folder
`config.toml` is in when there is no workspace. `cloud_root_dir`, `backup_dir`,
`temp_dir` and `state.manifest_file` may not lie inside one another: each has an
owner that removes files from it on its own schedule. A path written with the
`\\?\` prefix is compared without it.

## Sections

| Section | What it controls | See |
|---|---|---|
| `[sync]` | Comparison, direction, deletions, dry run by default | [Synchronisation](SYNC.md) |
| `[targets]` | `include_roots`: what under `.codex` takes part in `sync` | [Synchronisation](SYNC.md#what-is-synced) |
| `[filters]` | `exclude_globs`: what is never copied | [Synchronisation](SYNC.md#what-is-synced) |
| `[conflict]` | `policy` for a file or chat changed on both sides (default: keep the newer) | [Synchronisation](SYNC.md#conflicts) |
| `[backup]` | Retention and format of backups | [Recovery](RECOVERY.md#backups) |
| `[guardian]` | Snapshot store, polling, shrink thresholds, retention | [Guardian](GUARDIAN.md#settings) |
| `[semantic]` | Conflict bundles, mirror compression | [Sessions](SESSIONS.md#the-cloud-mirror) |
| `[[path_mappings]]` | How a path on one machine maps onto another | [below](#path_mappings) |
| `[process_detection]` | Which processes mean "Codex is running" | [below](#process_detection) |
| `[scheduler]` | The scheduled safe job | [below](#automation) |
| `[state_backup]` | Copies of `.codex` after sign-in and/or every N hours | [below](#copies-of-codex) |
| `[handoff]` | Handing work between machines, and the watcher task | [Synchronisation](SYNC.md#handing-work-over) |
| `[logging]` | Level, format, rotation, retention | [below](#logging) |
| `[safety]` | Fixed: Codex must be stopped, uncertainty aborts | not editable |
| `[state]` | Where the sync manifest lives | — |

## `[[path_mappings]]`

A machine handoff usually changes paths: a project lives in `D:/Projects/atlas`
on the desktop and in `C:/Work/atlas` on the laptop. A rule says so:

```toml
[[path_mappings]]
rule_id = "desktop-projects-to-laptop"
source_machine = "desktop"
target_machine = "laptop"
from = "D:/Projects"
to = "C:/Work"
# case_sensitive = false   # optional
```

`rule_id` must be unique. Rules are used by `chats`, `repair-projects` and the
working set of `sessions`; they are never written into Codex's files, and Codex
does not read them. See [Projects and chats](PROJECTS.md).

## `[process_detection]`

```toml
[process_detection]
process_names = ["codex.exe", "codex", "codex-app-server"]
grace_period_seconds = 2

[process_detection.background_process_names]
windows = ["codex-windows-sandbox", "codex-windows-sandbox-setup", "codex-command-runner"]
macos = ["ChatGPT.app/Contents/MacOS/", "codex-app-server", "codex-execve-wrapper", "codex-code-mode-host"]
linux = ["/usr/lib/chatgpt/", "codex-app-server", "codex-linux-sandbox", "codex-execve-wrapper", "codex-code-mode-host"]
```

A name belongs in `background_process_names` only if it disappears when Codex
is closed. A process that is always running reports the same thing in both
states, so it does not make detection stricter -- it makes the safety gate say
"running" forever and closes every mutating command permanently. This is why
`codex-windows-sandbox-service` is not listed: on Windows it is the service
`CodexSandboxService.OpenAI.Codex`, started automatically at boot.

Names are matched as whole process names, never as substrings. An entry
containing `/` is a path marker matched against the process path: the macOS
desktop build is called `ChatGPT`, and a bare `ChatGPT` would also match the
ordinary ChatGPT app.

The keys `allow_terminate_if_running` and the other `terminate_*` keys are left
from 0.1. codexSync never terminates Codex by force, and
`allow_terminate_if_running = true` is refused. To have a sync ask Codex to
quit, use `[sync] close_codex` (below).

## Upgrading a config from an earlier version

The template 0.1 shipped set `allow_terminate_if_running = true` and
`session_mode = "last_date_only"`, and this version refuses both for every
command that writes. So a `config.toml` written by 0.1 makes `sync`, `restore`,
`repair-projects apply` and `recover` exit 4 until it is brought up to date —
and the Settings screen cannot do it by hand, because it has no field for the
first key and refuses to save any text that still carries it.

```powershell
codexsync -c config.toml config check     # what this version would change; writes nothing
codexsync -c config.toml config upgrade --confirm-plan <id>
```

`config check` prints every finding with its code, the exact edits and a diff,
then the plan id. `config upgrade` needs that id, and the id covers the file's
bytes, so a config edited in between stops the upgrade instead of being written
over. In the window the same thing appears on *Settings* as **Config from an
earlier version**, with the same list, the same diff and the same id.

Your file stays yours. Comments are kept, arrays grow and shrink line by line
rather than being rewritten, values the plan does not name are not touched, and
the replaced version is copied into `config-history/` next to your workspace.
Everything is written in one step: a config with only the first blocker fixed is
still refused, so there is no half-migrated state to be left in.

| Code | Level | What it means |
|---|---|---|
| `TERMINATE_FLAG_SET` | blocks writes | `allow_terminate_if_running = true`; codexSync never terminates Codex by force (see `[sync] close_codex`) |
| `SESSION_MODE_LAST_DATE` | blocks writes | `session_mode = "last_date_only"` can drop branches |
| `BACKUP_DISABLED` | blocks writes | `backup_before_overwrite = false` |
| `DETECTION_LIST_OUTDATED` | safety | Codex processes this version knows are missing from your lists |
| `MISSING_EXCLUDE_SKILLS_SYSTEM` | correctness | `skills/.system/**` is not excluded |
| `MISSING_EXCLUDE_CODEX_BINARIES` | correctness | Codex's own programs under `plugins/` are not excluded |
| `OBSOLETE_INCLUDE_ROOT` | correctness | include roots that are never copied anyway |
| `SCHEDULER_INTERVAL_MIGRATED` | correctness | `interval_minutes` carried over to `interval_seconds` |
| `LEGACY_SCHEDULER_KEYS` | correctness | scheduler keys this version ignores |

A blocker has to be settled; everything else may be declined — `--skip CODE` on
the command line, or the tick next to it in the window. `DETECTION_LIST_OUTDATED`
is the one worth reading before declining: names are matched whole, so a Codex
process your config does not list is never seen at all, and "the window is
closed but its background processes are still running" is exactly the state the
safety check exists for. `doctor` reports all of this as `config_compat`.

## Automation

A scheduled task is the applied form of `[scheduler]`. Set it here or on the
window's *Automation* page, never in the OS scheduler directly.

```toml
[scheduler]
enabled = true
mode = "guardian_snapshot"   # guardian_snapshot | preflight | sync_dry_run
interval_seconds = 1800      # 300 (5 min) to 2678400 (31 days); default 1800 (30 min)
run_at_login = true
startup_delay_seconds = 0
jitter_seconds = 0
sync_at_login = false        # a separate task: one sync after sign-in ([sync] scope decides what)
```

```powershell
codexsync -c config.toml automation status   # config, exact command, OS task state; changes nothing
codexsync -c config.toml automation apply    # install or update the task; removes it when enabled = false
codexsync -c config.toml automation remove   # remove the task; config.toml is untouched
codexsync -c config.toml automation run      # run the configured job once, now
```

- The periodic task can run only a safe job: `guardian_snapshot`, `preflight` or
  `sync_dry_run`. A repair, a transfer, a restore or a rollback cannot be
  scheduled, and a scheduled dry run is still refused while Codex is open.
- **Sync after sign-in** (`sync_at_login = true`, the *Sync after signing in*
  checkbox) is the one exception and is off by default. It installs a second
  task that runs `sync --apply --unattended` once after you sign in, after
  `startup_delay_seconds`, and never repeats. It carries what `[sync] scope`
  says — everything (settings, chats, projects) by default, or settings files
  only — and goes through the same checks as a manual sync: a conflict is
  decided by `conflict.policy` as in any run, and one the policy leaves open
  stops it before any write (exit 2). If Codex is open it is refused (exit 3),
  unless Codex may be closed for a sync (next point). The window shows the
  task's last run and what its result meant.
- **Closing Codex for a sync** (`[sync] close_codex = true`, the *Close Codex
  when a sync starts while it is open* checkbox on the Automation page; off by
  default). Any sync that finds Codex — the ChatGPT app — open asks it to quit
  first, the way Windows closes an app for an update, and waits up to a minute
  for it to be gone. It is never forced: if Codex declines or stays, nothing is
  written and the sync says why. A running agent turn is interrupted, so leave
  Codex idle; a Codex CLI running in a terminal is never closed; the handoff
  watcher and dry runs never ask. Proven on Windows; on macOS it stays off until
  it has been seen to work on a Mac.
- It is a user-level task — Task Scheduler on Windows, a LaunchAgent on macOS,
  `systemd --user` on Linux — never a service.
- `automation run` exits like the job would: `0` on success or when another
  Guardian is already running, `2` on quarantine, `3` if Codex is running during
  `sync_dry_run`, `5` on failure.

**Deprecated:** `guardian scheduler` and `scripts/scheduler/{windows,macos}` keep
scheduler settings outside `config.toml` and will be removed. If you installed a
task with those scripts, uninstall it with them first so that two tasks do not
run. `automation status` and the Automation page name such a task (the default
`codexSyncSync`, the LaunchAgent `com.codexsync.sync`, or any task that runs
`run-codexsync`) with the command that removes it: it runs `codexsync sync`,
which in 0.2 carries settings only, never chats. It is never removed for you.

**One task per account, and an executable that moved.** The Windows task is
registered as `CodexSync Job (<user>)` in the `\CodexSync\` folder: before
0.2 every account shared one name, so enabling automation as one user replaced
another user's task and disabling it deleted theirs. A task belonging to another
account is now shown and never changed or removed. `automation status` also
names what the installed task actually runs, so a renamed or moved executable —
what an upgraded frozen install leaves behind — is reported as
`EXECUTABLE_MISSING` or `EXECUTABLE_MOVED` instead of a vague "does not match";
`automation apply` re-registers it for this installation.

## Copies of `.codex`

A copy of the valuable part of the Codex state directory, taken only while Codex
is closed, into a folder you choose. It stays off until that folder is set:
nothing proposes one, because a folder a cloud client syncs uploads every copy.

```toml
[state_backup]
root_dir = "E:/Backups/codex"   # empty: no copies, the task stays off
at_login = true                 # a copy after signing in
interval_hours = 24             # and/or every N hours, at most 744; 0 = off
keep = 5                        # copies of this machine to keep
```

```powershell
codexsync -c config.toml state-backup create          # one copy now; exit 3 while Codex is open
codexsync -c config.toml state-backup create --wait   # wait for Codex to close first (up to 23 h)
codexsync -c config.toml state-backup list            # the copies in the folder
codexsync -c config.toml automation apply             # install the task that takes them
```

- **What is copied:** sessions and the archive, the global state and its
  `.bak`, the SQLite catalogues (`state_*`, `thread_history_*`, `memories_*`,
  `goals_*`, with their `-wal`/`-shm`), `session_index.jsonl`, `config.toml`,
  `AGENTS.md`, `rules`, `skills`, `memories` and `automations`. **Never:**
  `auth.json`, `cap_sid`, `.sandbox-secrets` or anything else holding a token,
  nor caches, logs, the sandbox, temporary files or any compiled program
  (an installer restores those). About 1.2 GB before
  compression on the machine this was built on. Codex's own `config.toml` is
  copied whole, and it can hold MCP server tokens (`env` tables, bearer
  headers): keep `root_dir` somewhere only you can read, and not in a shared
  folder.
- **Links are not followed.** A symlink or a Windows junction inside `.codex`
  (`skills` often holds junctions into other folders) is skipped with everything
  behind it; a cloud placeholder is an ordinary file and is copied. A folder that
  cannot be listed fails the copy (exit 5) instead of leaving a hole in it.
- **Only while Codex is closed.** A copy of a database or a session that is being
  written is a copy of no particular moment, so the copy goes through the same
  process check as a write: Codex continuously closed before the first file, and
  checked again before the copy counts. The scheduled task waits for Codex to
  close (checking every 30 seconds, for up to 23 hours); `create` without
  `--wait` and the window's *Make a copy now* refuse instead (exit 3). A file
  that changes while it is being read fails the copy.
- **One zip per copy**, `codex-<machine>-<UTC time>.zip`, with a manifest of
  every file and its SHA-256 inside. It is written as `.partial`, read back and
  compared, and only then renamed. After that the oldest copies of this machine
  beyond `keep` are removed; copies another machine left in the same folder are
  listed and never removed.
- `root_dir` must be outside `.codex`, `backup_dir`, `temp_dir`, the cloud
  mirror, `guardian.root_dir` and `semantic.root_dir`: each of those has an
  owner that prunes, sweeps or mirrors it.
- It is the third user-level task, `CodexSync Codex backup (<user>)`; at
  sign-in it also waits `startup_delay_seconds`. A copy only reads `.codex`, and
  putting one back is by hand: close Codex and unzip what you need.
- Installing the task never takes a copy: with `at_login` the first copy comes
  at the next sign-in, on every platform. The task needs the process check,
  which is proven on Windows only, so on macOS and Linux `automation apply`
  refuses to install it (and `sync_at_login`) rather than install a task whose
  every run would stop with exit 5.

## Logging

```toml
[logging]
level = "INFO"            # DEBUG | INFO | WARNING | ERROR
file = "${workspace_root}/logs/codexsync.log"
format = "text"           # text | json | logfmt
retention_days = 7
archive_mode = "zip"      # zip | text
max_file_size_mb = 10
```

- Log files are daily and carry the machine id:
  `<stem>-<machine>-YYYY-MM-DD[.N].log`, UTF-8.
- A file is rotated by day and by size; old files are archived into `.zip`
  (`archive_mode = "zip"`) or kept as text, and removed after `retention_days`.
- Every dangerous action is logged separately: backup created, overwrite, skip.
- `file` must be outside `.codex`, and `level` must be one of the listed values.
- `plan`, `sync`, `restore` and the commands a scheduled task runs (`guardian`,
  `preflight`, `state-backup`, `handoff`) write to this file.
