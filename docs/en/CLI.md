# Command line

**English** · [Русский](../ru/CLI.md) · [中文](../zh/CLI.md)

[← Documentation](README.md)

```powershell
codexsync -c config.toml <command>            # installed
python -m codexsync -c config.toml <command>  # from a source checkout, without installing
codexsync-gui.exe -c config.toml <command>    # the Windows windowed build, without a console
```

Global options: `-c/--config` — the path to `config.toml`; `-v/--verbose` —
verbose logging, including which Codex processes were seen; `-V/--version` —
print the version and exit, which is how you ask a downloaded exe what it is.
`-h` on any command or subcommand prints its options.

## All commands

"Cold" means the command refuses to run unless Codex is closed. Everything else
only reads (or writes outside `.codex`) and may run at any time.

| Command | Cold? | What it does | Details |
|---|---|---|---|
| `init-config` | — | Write a `config.toml` from the bundled template | [below](#getting-started) |
| `validate` | no | Load and check the configuration, including that commands that write accept it | [below](#getting-started) |
| `config check` | no | Report what this version would change in `config.toml` | [Configuration](CONFIGURATION.md#upgrading-a-config-from-an-earlier-version) |
| `config upgrade` | no | Apply that, in one confirmed write | [Configuration](CONFIGURATION.md#upgrading-a-config-from-an-earlier-version) |
| `config set` / `unset` | no | Change or remove one value, as the Settings page does (`--dry-run` shows the change) | [below](#the-window-from-the-console) |
| `config mapping` | no | List, suggest, add or remove `[[path_mappings]]` rules | [below](#the-window-from-the-console) |
| `config roots` | no | One level of `.codex` and the cloud copy, to choose `targets.include_roots` | [below](#the-window-from-the-console) |
| `config history` | no | Saved versions of `config.toml` | [below](#the-window-from-the-console) |
| `doctor` / `preflight` | no | Environment diagnostics; identical and side-effect free | [below](#getting-started) |
| `plan` | no | Show what a sync would copy (marked `volatile` if Codex is open) | [Sync](SYNC.md) |
| `sync` | **yes** | Copy state both ways, backup first | [Sync](SYNC.md) |
| `restore` | **yes** | Restore files from a verified backup | [Recovery](RECOVERY.md#restoring-a-backup) |
| `guardian watch` | no | Keep taking snapshots of the global state while Codex runs | [Guardian](GUARDIAN.md) |
| `guardian snapshot --once` | no | Take one snapshot now | [Guardian](GUARDIAN.md) |
| `guardian list` | no | List this machine's snapshots and quarantine | [Guardian](GUARDIAN.md) |
| `guardian restore` | **yes** | Put a verified snapshot back (preview without `--confirm`) | [Guardian](GUARDIAN.md#restoring-a-snapshot) |
| `guardian accept` | no | Take the current state as the new baseline after a real drop | [Guardian](GUARDIAN.md#accepting-a-new-baseline) |
| `guardian scheduler` | no | Deprecated: use `automation apply` | [Configuration](CONFIGURATION.md#automation) |
| `automation status` / `run` | no | Show the scheduled task, or run its safe job now | [Configuration](CONFIGURATION.md#automation) |
| `automation apply` / `remove` | no | Make the OS tasks match `[scheduler]`, `[state_backup]` and `[handoff]`, or remove them | [Configuration](CONFIGURATION.md#automation) |
| `state-backup create` | **yes** | Take one verified copy of `.codex`; `--wait` waits for Codex to close | [Configuration](CONFIGURATION.md#copies-of-codex) |
| `state-backup list` | no | List the copies in `[state_backup] root_dir` | [Configuration](CONFIGURATION.md#copies-of-codex) |
| `backups list` | no | Backup snapshots a sync or restore took; the name is what `restore --from` takes | [Recovery](RECOVERY.md#restoring-a-backup) |
| `summary` | no | What the Home page shows; `--recount` counts chats now | [below](#the-window-from-the-console) |
| `handoff status` | no | Which machine is working, what each handed off, what arrived here | [Synchronisation](SYNC.md#handing-work-over) |
| `handoff sync` | **yes** | Load what other machines handed off, then hand off this one; conflicts decided by `conflict.policy` (`--conflict-policy` for one run) | [Synchronisation](SYNC.md#handing-work-over) |
| `handoff watch` | **yes** | The watcher the task at sign-in runs: loads at start, hands off when Codex closes | [Synchronisation](SYNC.md#handing-work-over) |
| `sessions scan` | no | Classify every session branch on both sides | [Sessions](SESSIONS.md) |
| `sessions resolve` | no | Record one decision about a divergence | [Sessions](SESSIONS.md#divergences) |
| `sessions apply` | **yes** | Transfer whole branches under one confirmed plan | [Sessions](SESSIONS.md#applying-a-plan) |
| `sessions index` | no | Report what each `session_index.jsonl` holds | [Sessions](SESSIONS.md#the-session-index) |
| `sessions scope` | no | Show the working set stored for a pair of machines | [Sessions](SESSIONS.md) |
| `sessions names` | **yes** | Chat names other machines show; with `--confirm-plan`, set them on chats unnamed here | [Sessions](SESSIONS.md#writing-into-codex) |
| `sessions catalogue` | **yes** | List chat files Codex does not show; with `--confirm-plan`, ask Codex to rebuild its chat list | [Sessions](SESSIONS.md#writing-into-codex) |
| `chats list` / `chats tree` | no | Find chats and see which project each is in | [Projects](PROJECTS.md#chats) |
| `chats move` | **yes** | Put chosen chats under one project | [Projects](PROJECTS.md#moving-chats-to-a-project) |
| `projects sync` | **yes** | Merge other machines' project lists into this one, then publish this one's (preview without `--confirm-plan`) | [Projects](PROJECTS.md#projects-between-machines) |
| `projects files` | no | Whether this machine's project folders hold what other machines last had | [Projects](PROJECTS.md#project-folders) |
| `repair-projects scan` | no | Build an immutable, hashed repair plan | [Projects](PROJECTS.md#repair-after-a-machine-handoff) |
| `repair-projects apply` | **yes** | Apply one exact plan, quoted by its id | [Projects](PROJECTS.md#repair-after-a-machine-handoff) |
| `project-move scan` | no | Hash a project and plan copying it to a new folder | [Projects](PROJECTS.md#moving-a-projects-files) |
| `project-move apply` | **yes** | Copy, verify, then point Codex at the new folder | [Projects](PROJECTS.md#moving-a-projects-files) |
| `history` | no | List past writes of every kind (or one kind) from their journals | [Sync](SYNC.md#history) |
| `recover list` | no | List open mutation journals and what closes each (`--all`, `--json`) | [Recovery](RECOVERY.md#interrupted-mutations) |
| `recover inspect` | no | Read one mutation journal without side effects | [Recovery](RECOVERY.md#interrupted-mutations) |
| `recover resume` / `rollback` | **yes** | Close an interrupted mutation | [Recovery](RECOVERY.md#interrupted-mutations) |

Two rules apply to every command that writes and are not configurable: it
refuses while Codex is open *or undetermined*, and it takes a verified backup
before it replaces anything.

## Getting started

Write the template to `config.toml` in the current folder, to another path, or
over an existing file:

```powershell
codexsync init-config
codexsync init-config --output D:\codexSync\config.toml
codexsync init-config --output D:\codexSync\config.toml --force
```

Or write a config already filled in for this machine. It is validated, every
template comment is kept, `--machine-id`, `--local-state-dir` and
`--workspace-root` go together (`--cloud-root` is optional), and an existing file
is never overwritten in this mode:

```powershell
codexsync init-config --output config.toml --machine-id laptop-1 --local-state-dir C:/Users/me/.codex --workspace-root D:/Cloud/codexSync
```

### Which config is opened

`-c` names the file, and naming one that does not exist yet is a request to
create it there. The config can live in any folder you like. Without `-c` the
search is the same one the window does: the config the window last opened,
then `config.toml` in the current folder, then beside the executable. If none
of them has a file, the command stops with exit code 4 and says how to name
one — no location is ever made up for you. A file found anywhere but the
current folder is announced, so a command never works quietly on a file you
are not looking at.

The window records the path of the config it opened or created in a small
pointer file — the path only, never the content — at
`%LOCALAPPDATA%\CodexSync\config-path.txt` on Windows,
`~/Library/Application Support/CodexSync/config-path.txt` on macOS and
`$XDG_CONFIG_HOME/codexsync/config-path.txt` on Linux. That is how a terminal
command without `-c` finds the same file as the window. A script or a scheduled
task should still pass `-c` to be precise.

Check the configuration, then the environment:

```powershell
codexsync -c config.toml validate
codexsync -c config.toml doctor
```

`doctor` (and its twin `preflight`) reads only and creates nothing — least of all
inside `.codex`. It checks the configuration and directories, whether Codex is
running, the global-state schema and the latest restorable snapshot, session
files and the session index, the SQLite thread catalogue, what a sync is allowed
to do, the sync manifest, leftover temporary files, and whether an unfinished
mutation still blocks every write (a failure until `recover` closes it).

## The window from the console

Everything the window does, the console does too: the window is a convenience,
and a script or a scheduled task has only the console. The pages that are not
commands of their own map like this:

| Window | Console |
|---|---|
| Home | `summary` (`--recount` to count chats and projects now) |
| Settings — any field | `config set SECTION.KEY VALUE`, `config unset SECTION.KEY` |
| Settings — path mappings | `config mapping list`, `suggest`, `add`, `remove` |
| Settings — what is synchronised | `config roots [PATH]` |
| Settings — history | `config history` |
| Backups | `backups list`, then `restore --from NAME` |
| Sessions — working set | `sessions scan --project/--chat --save-scope`; `sessions scope` shows it |
| *Always decide this way* | `config set conflict.policy prefer_newer_mtime` (or another rule) |

`config set` takes a TOML value (`true`, `600`, `["sessions", "skills"]`); text
needs no quotes. It edits the file the way Settings do: comments and every other
value stay, the result must pass the same checks as the loader, a file that
changed meanwhile is not overwritten, and the replaced version goes into
`config-history/`. What the window does not let you edit, the console refuses as
well — `safety.*`, the `assumptions`, the 0.1 termination switches and
`handoff.root_dir`.

```powershell
codexsync -c config.toml config set conflict.policy prefer_local --dry-run
codexsync -c config.toml config mapping add --id laptop --source-machine laptop `
  --target-machine desktop --from C:/Users/me/Projects --to D:/Projects
codexsync -c config.toml backups list
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | runtime error |
| `2` | conflict detected, a decision is needed |
| `3` | Codex is running (the cold precondition failed) |
| `4` | invalid configuration or arguments |
| `5` | safe abort (fail-safe) |

`doctor`/`preflight` return `0` when every check passed or there are only
warnings, and `5` when at least one check failed.

A malformed command line — an unknown option, a missing argument — is `4`, not
the `2` an argument parser usually returns: `2` means a conflict here. The same
holds for `codexsync-gui.exe` given a command; `codexsync-gui.exe --version` and
`--help` are answered by the command line too.

`guardian restore` and `chats move` are previews without `--confirm`; their
`--dry-run` checks everything a real write would, including the process gate,
only together with `--confirm`.

## Process safety

- codexSync never starts or terminates Codex. The old termination flags are
  rejected with exit code `4`, as is `allow_terminate_if_running = true` for
  commands that write.
- A write requires Codex to have been stopped continuously for two seconds, plus
  direct checks before and during the commit.
- Only one writing command works on one Codex folder at a time, whatever its
  kind; a second one stops with exit code `5`.
- `RUNNING` and `UNKNOWN` both block a write. On macOS and Linux writes stay
  blocked until the process detector has been proven on a live machine (see
  [what is not proven yet](README.md#what-is-not-proven-yet)).
- If a destination is momentarily held open by another process (a cloud client,
  a search indexer, an antivirus), the atomic replace is retried with bounded
  backoff. The process check is repeated before each attempt, and any other
  error is not retried ([D-011](../dev/DECISIONS.md)).
- Background processes that count as "Codex is still running" are listed per OS
  in [`process_detection.background_process_names`](CONFIGURATION.md#process_detection).
- With `-v`, `plan`, `sync` and `restore` log the tracked processes: whether
  Codex and its sandbox are running, and the subprocesses under Codex (PID, name,
  parent PID). Full command lines are never collected.
