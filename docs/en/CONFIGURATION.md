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
  during a cold operation, and never created.
- **`cloud_root_dir`** is the mirror of the state in the cloud folder.
- **`backup_dir`** holds the backups taken before anything is replaced.
- **`temp_dir`** holds the operation lock and the mutation journal, and is
  where `restore` unpacks a snapshot. A copy itself is staged and verified in
  the folder of its destination: an atomic replace only works within one volume,
  and `.codex` is often on another drive than the cloud folder.

A relative path is resolved against `workspace_root_dir`, or against the folder
`config.toml` is in when there is no workspace.

## Sections

| Section | What it controls | See |
|---|---|---|
| `[sync]` | Comparison, direction, deletions, dry run by default | [Synchronisation](SYNC.md) |
| `[targets]` | `include_roots`: what under `.codex` takes part in `sync` | [Synchronisation](SYNC.md#what-is-synced) |
| `[filters]` | `exclude_globs`: what is never copied | [Synchronisation](SYNC.md#what-is-synced) |
| `[conflict]` | `policy` for a file changed on both sides | [Synchronisation](SYNC.md#conflicts) |
| `[backup]` | Retention and format of backups | [Recovery](RECOVERY.md#backups) |
| `[guardian]` | Snapshot store, polling, shrink thresholds, retention | [Guardian](GUARDIAN.md#settings) |
| `[semantic]` | Conflict bundles, mirror compression | [Sessions](SESSIONS.md#the-cloud-mirror) |
| `[[path_mappings]]` | How a path on one machine maps onto another | [below](#path_mappings) |
| `[process_detection]` | Which processes mean "Codex is running" | [below](#process_detection) |
| `[scheduler]` | The scheduled safe job | [below](#automation) |
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
from 0.1. codexSync never terminates Codex, and `allow_terminate_if_running =
true` is refused.

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
| `TERMINATE_FLAG_SET` | blocks writes | `allow_terminate_if_running = true`; codexSync never stops Codex |
| `SESSION_MODE_LAST_DATE` | blocks writes | `session_mode = "last_date_only"` can drop branches |
| `BACKUP_DISABLED` | blocks writes | `backup_before_overwrite = false` |
| `DETECTION_LIST_OUTDATED` | safety | Codex processes this version knows are missing from your lists |
| `MISSING_EXCLUDE_SKILLS_SYSTEM` | correctness | `skills/.system/**` is not excluded |
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
window's *Settings → Automation* tab, never in the OS scheduler directly.

```toml
[scheduler]
enabled = true
mode = "guardian_snapshot"   # guardian_snapshot | preflight | sync_dry_run
interval_seconds = 300       # at least 60
run_at_login = true
startup_delay_seconds = 0
jitter_seconds = 0
sync_at_login = false        # a separate task: sync settings once after sign-in
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
- **Sync after sign-in** (`sync_at_login = true`, the *Sync settings after
  signing in* checkbox) is the one exception and is off by default. It installs
  a second task that runs `sync --apply --unattended` once after you sign in,
  after `startup_delay_seconds`, and never repeats. It goes through the same
  checks as a manual sync: it is refused while Codex is open (exit 3), and
  `--unattended` turns every conflict into a stop before any write (exit 2),
  whatever `conflict.policy` says. It syncs the settings tree only — sessions
  are never transferred by a task. If Codex starts with Windows, it will be open
  by the time the task runs and the sync will simply be skipped; take Codex out
  of autostart if you want this to work. The window shows the task's last run
  and what its result meant.
- It is a user-level task — Task Scheduler on Windows, a LaunchAgent on macOS,
  `systemd --user` on Linux — never a service.
- `automation run` exits like the job would: `0` on success or when another
  Guardian is already running, `2` on quarantine, `3` if Codex is running during
  `sync_dry_run`, `5` on failure.

**Deprecated:** `guardian scheduler` and `scripts/scheduler/{windows,macos}` keep
scheduler settings outside `config.toml` and will be removed. If you installed a
task with those scripts, uninstall it with them first so that two tasks do not
run.

**One task per account, and an executable that moved.** The Windows task is
registered as `CodexSync Job (<user>)` in the `\CodexSync\` folder: before
0.2 every account shared one name, so enabling automation as one user replaced
another user's task and disabling it deleted theirs. A task belonging to another
account is now shown and never changed or removed. `automation status` also
names what the installed task actually runs, so a renamed or moved executable —
what an upgraded frozen install leaves behind — is reported as
`EXECUTABLE_MISSING` or `EXECUTABLE_MOVED` instead of a vague "does not match";
`automation apply` re-registers it for this installation.

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
