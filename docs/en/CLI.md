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
| `validate` | no | Load and check the configuration, nothing else | [below](#getting-started) |
| `config check` | no | Report what this version would change in `config.toml` | [Configuration](CONFIGURATION.md#upgrading-a-config-from-an-earlier-version) |
| `config upgrade` | no | Apply that, in one confirmed write | [Configuration](CONFIGURATION.md#upgrading-a-config-from-an-earlier-version) |
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
| `automation apply` / `remove` | no | Make the OS task match `[scheduler]`, or remove it | [Configuration](CONFIGURATION.md#automation) |
| `sessions scan` | no | Classify every session branch on both sides | [Sessions](SESSIONS.md) |
| `sessions resolve` | no | Record one decision about a divergence | [Sessions](SESSIONS.md#divergences) |
| `sessions apply` | **yes** | Transfer whole branches under one confirmed plan | [Sessions](SESSIONS.md#applying-a-plan) |
| `sessions index` | no | Report what each `session_index.jsonl` holds | [Sessions](SESSIONS.md#the-session-index) |
| `chats list` / `chats tree` | no | Find chats and see which project each is in | [Projects](PROJECTS.md#chats) |
| `chats move` | **yes** | Put chosen chats under one project | [Projects](PROJECTS.md#moving-chats-to-a-project) |
| `repair-projects scan` | no | Build an immutable, hashed repair plan | [Projects](PROJECTS.md#repair-after-a-machine-handoff) |
| `repair-projects apply` | **yes** | Apply one exact plan, quoted by its id | [Projects](PROJECTS.md#repair-after-a-machine-handoff) |
| `project-move scan` | no | Hash a project and plan copying it to a new folder | [Projects](PROJECTS.md#moving-a-projects-files) |
| `project-move apply` | **yes** | Copy, verify, then point Codex at the new folder | [Projects](PROJECTS.md#moving-a-projects-files) |
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
create it there. Without `-c` the search is the same one the window does:
`config.toml` in the current folder, then beside the executable, then the
per-user location — `%APPDATA%\CodexSync\config.toml` on Windows,
`~/Library/Application Support/CodexSync/` on macOS,
`$XDG_CONFIG_HOME/codexsync/` on Linux. A file found anywhere but the current
folder is announced, so a command never works quietly on a file you are not
looking at.

The window remembers the config it last opened and starts with it; the command
line does not remember anything, which is why `-c` stays the way to be precise
in a script or a scheduled task.

Check the configuration, then the environment:

```powershell
codexsync -c config.toml validate
codexsync -c config.toml doctor
```

`doctor` (and its twin `preflight`) reads only and creates nothing — least of all
inside `.codex`. It checks the configuration and directories, whether Codex is
running, the global-state schema and the latest restorable snapshot, session
files and the session index, the SQLite thread catalogue, what a sync is allowed
to do, the sync manifest and leftover temporary files.

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

## Process safety

- codexSync never starts or terminates Codex. The old termination flags are
  rejected with exit code `4`, as is `allow_terminate_if_running = true` for
  commands that write.
- A write requires Codex to have been stopped continuously for two seconds, plus
  direct checks before and during the commit.
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
