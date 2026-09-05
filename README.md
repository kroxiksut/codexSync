# codexSync

Open-source utility for syncing local Codex state between personal machines using a cloud-synced folder.

Russian version: [README.ru.md](./README.ru.md).

> [!IMPORTANT]
> Current real-world validation is Windows-to-Windows only.
> macOS support exists in code/CI, but end-to-end handoff on real macOS machines is not yet validated.

## Why

Developers may want to continue working with Codex on another machine without losing local session state.

## What this does

* **Syncs** the local Codex state directory through any cloud-synced folder
  (Dropbox, OneDrive, Yandex.Disk, Syncthing…), backup-first, only while Codex
  is closed.
* **Guards the global state.** `guardian` takes immutable, verified snapshots
  of `.codex-global-state.json` while Codex is running, writing only outside
  `.codex`, so a crash or a BSOD leaves a restorable `latest-good`.
* **Recovers from an interrupted mutation.** Every write is wrapped in a lock, a
  durable journal and a verified backup, and `recover inspect|resume|rollback`
  is the sanctioned way out of one that stopped halfway.
* **Repairs a machine handoff.** `repair-projects` rebuilds project bindings
  after paths moved, from what the sessions actually say, as an exact plan you
  confirm by its id.
* **Moves session history between machines.** `sessions` classifies every branch
  semantically — identical, fast-forward, archive transition, divergence — and
  never merges, sorts or picks a winner by timestamp.
* **Finds a chat and puts it under a project.** `chats` lists what exists, says
  *why* each chat sits where it does, and moves one with the same confirmation
  protocol as everything else.

## What this does NOT do

* No integration with Codex internals
* No API usage
* No token extraction
* No network interception
* No real-time sync
* No checks for cloud client process/health
* No checks for free space in cloud/network storage

## Design principles

* **Backup-first, and fail closed.** Uncertainty is never resolved optimistically.
* **One authority, one envelope.** Exactly one place decides whether state may
  change, and exactly one path performs the change.
* **Say why, do not guess.** A branch that cannot be classified, a project that
  matches two candidates, a runtime behaviour that has not been observed — each
  is reported with a code, not approximated.
* **No integration with Codex internals.** codexSync never starts or stops
  Codex, reads no tokens and writes no SQLite.
* **Offline-friendly, zero runtime dependencies** in the core and the CLI.
* Windows-first; macOS supported in code and CI.

## How it works

1. Decide whether Codex is running. An *undetermined* answer counts as running:
   nothing that mutates state is optimistic.
2. Read-only commands (`doctor`, `plan`, every `scan`, `chats`, `guardian`) run
   either way. A result taken while Codex is open is marked `volatile` and
   cannot be reused by a mutation.
3. A mutation runs only with Codex closed, and always through the same
   envelope: a non-stealable lock, a durable journal, a verified backup of
   everything it will replace, a final process check immediately before the
   commit, staging on the same volume, an atomic replace, then `COMMITTED`.
4. Anything ambiguous stops instead of guessing, with an exit code that says
   which kind of stop it was.

## Conflict policy

`conflict.policy` supports:

- `manual_abort`: report conflict and stop (default)
- `prefer_cloud`: auto-resolve conflict by taking cloud version
- `prefer_local`: auto-resolve conflict by taking local version
- `prefer_newer_mtime`: auto-resolve conflict by taking side with newer mtime

## Sync options

`sync.compare` controls file comparison strategy:

- `mtime` (default): compare by `size + mtime`
- `mtime_hash_fallback`: fast `size + mtime` path, but when values are equal/close (within tolerance), compare file content hash (SHA-256)

`sync.equal_mtime_action` controls behavior when file mtimes are equal (within `sync.time_tolerance_seconds`) but files differ:

- `skip`: do not copy (default)
- `prefer_local`: copy local version to cloud
- `prefer_cloud`: copy cloud version to local
- `manual_abort`: mark as conflict and stop in `manual_abort` conflict mode

`sessions`, `archived_sessions`, `session_index.jsonl`, global project state, and SQLite are semantic-owned paths in 0.2. They are excluded from generic mtime copying. `sync.session_mode=last_date_only` is rejected because it can discard branches.

## Backups

- `backup.compression` supports:
  - `none` (default): backup snapshot is stored as directory tree
  - `zip`: backup snapshot is stored as a single `.zip` file
- New backups include a verified `codexsync-backup-v1` sidecar manifest. Automatic restore selects only committed, manifested backups. A legacy directory/zip requires an explicit `--from` plus `--allow-legacy-snapshot` and cannot restore semantic-owned state.

## Logging

- Levels: `DEBUG|INFO|WARNING|ERROR`
- Formats (configurable): `text|json|logfmt`
- Rotation/size/retention/archive rules apply equally to all formats (`text`, `json`, `logfmt`)
- UTF-8 for all log files
- Daily log files with machine id (`<stem>-<machine>-YYYY-MM-DD[.N].log`)
- Daily/time rotation + size rotation (`logging.max_file_size_mb`, default `10`)
- Retention cleanup (`logging.retention_days`, default `7`)
- Old log storage mode (`logging.archive_mode`):
  - `zip` (default): archive rotated/old logs into `.zip`
  - `text`: keep rotated logs as plain text files

## Platform and CI

- Runtime support is currently Windows-first.
- macOS support is allowed in current project scope (Apple Silicon target).
- Linux runtime support is intentionally out of MVP scope for now.
- CI runs `pytest` on `windows-latest` and `macos-latest`, for Python 3.11,
  3.12 and 3.13. There is no linter or type-checker; two tests carry that
  weight instead — a static guard against names used but never imported, and a
  guard that fails the moment a write targets the Codex state directory.

## CLI commands

Run from project root:

```powershell
python -m codexsync -c config.toml <command>
```

Every command at a glance. "Cold" means it refuses to run unless Codex is
closed; everything else reads only and may run at any time.

| Command | Cold? | What it does |
|---|---|---|
| `init-config` | — | Write a `config.toml` from the bundled template |
| `validate` | no | Load and check the configuration, nothing else |
| `doctor` / `preflight` | no | Environment diagnostics; identical, and side-effect free |
| `plan` | no | Show what a sync would copy (marked `volatile` if Codex is open) |
| `sync` | **yes** | Copy state both ways, backup-first |
| `restore` | **yes** | Restore files from a verified backup snapshot |
| `guardian watch` | no | Keep taking snapshots of the global state while Codex runs |
| `guardian snapshot --once` | no | Take one snapshot now |
| `guardian scheduler` | no | Render user-level scheduler templates |
| `repair-projects scan` | no | Build an immutable, hashed repair plan |
| `repair-projects apply` | **yes** | Apply one exact plan, quoted by its id |
| `sessions scan` | no | Classify every session branch on both sides |
| `sessions index` | no | Report what each `session_index.jsonl` holds |
| `sessions resolve` | no | Record one decision about a divergence |
| `sessions apply` | **yes** | Transfer whole branches under one confirmed plan |
| `chats list` / `chats tree` | no | Find chats and see which project each is in |
| `chats move` | **yes** | Put chosen chats under one project |
| `recover inspect` | no | Read one mutation journal without side effects |
| `recover resume` / `rollback` | **yes** | Close an interrupted mutation |

Two rules apply to every mutating command and are not configurable: it refuses
while Codex is open *or* undetermined, and it takes a verified backup before it
replaces anything.

Generate `config.toml` from bundled template:

```powershell
python -m codexsync init-config
```

Generate to a custom location:

```powershell
python -m codexsync init-config --output D:\codexSync\config.toml
```

Overwrite existing config file:

```powershell
python -m codexsync init-config --output D:\codexSync\config.toml --force
```

Validation:

```powershell
python -m codexsync -c config.toml validate
```

Preflight diagnostics (same behavior for `doctor` and `preflight`):

```powershell
python -m codexsync -c config.toml doctor
python -m codexsync -c config.toml preflight
```

Guardian (allowed while Codex is running; writes only to the external Guardian root):

```powershell
python -m codexsync -c config.toml guardian watch
python -m codexsync -c config.toml guardian snapshot --once
python -m codexsync -c config.toml guardian scheduler --platform windows --output-dir D:\codexSync\scheduler --log-dir D:\codexSync\logs
```

Read-only project analysis and exact-plan apply:

```powershell
python -m codexsync -c config.toml repair-projects scan --source-machine desktop --target-machine laptop --save-plan repair-plan.json
python -m codexsync -c config.toml repair-projects apply --plan repair-plan.json --confirm-plan <exact-plan-id>
```

Preview the apply without writing. The dry run performs every refusal the real
apply performs, including the process gate, so it is blocked by a running or
undetectable Codex exactly as the mutation is:

```powershell
python -m codexsync -c config.toml repair-projects apply --plan repair-plan.json --confirm-plan <exact-plan-id> --dry-run
```

### Moving a project to a new path

When a project directory moves — renamed, put on another drive, or opened on a
second machine under a different root — Codex loses it: the recorded root no
longer exists, so the chats that belong to it stop appearing under it.

Declare where the old prefix lives now with a `[[path_mappings]]` rule, then
scan. If an existing project's recorded root maps onto the directory your
sessions actually point at, the plan proposes `REMAP_ROOT` for it instead of
creating a second project. Applying it rewrites that one path value and nothing
else, so the project keeps its id.

A remap never travels alone. Chats created before the move recorded the old
directory, and moving the project's root away from it is what makes them
disappear from the sidebar — so every session still living under the old root is
also pinned to the project with an explicit `thread-project-assignments` entry,
in the shape the detected schema uses. If any such chat would be left uncovered
the plan reports `REMAP_ORPHANS_SESSIONS` and the apply refuses.

Nothing inside a session file is ever edited. A record's raw bytes are its
identity for branch comparison, so rewriting a `cwd` there would make the same
history on two machines permanently divergent — which is exactly the transfer
feature the obvious find-and-replace would destroy.

Two existing projects that both map onto the same new root are reported as
`AMBIGUOUS_PROJECT` and nothing is applied.

Build sync plan (no changes):

```powershell
python -m codexsync -c config.toml plan
```

Build plan with process snapshot (`--verbose`):

```powershell
python -m codexsync -c config.toml -v plan
```

Sync simulation (safe test):

```powershell
python -m codexsync -c config.toml sync --dry-run
```

Sync simulation with process snapshot (`--verbose`):

```powershell
python -m codexsync -c config.toml -v sync --dry-run
```

Real sync (writes files):

```powershell
python -m codexsync -c config.toml sync --apply
```

Typical handoff to another machine:

```powershell
python -m codexsync -c config.toml sync --apply
```

Restore from latest backup snapshot to local state:

```powershell
python -m codexsync -c config.toml restore --apply
```

Restore from specific backup snapshot:

```powershell
python -m codexsync -c config.toml restore --from <snapshot_dir_name> --apply
```

Restore from specific zip snapshot:

```powershell
python -m codexsync -c config.toml restore --from <snapshot_name.zip> --apply
```

Restore to cloud target instead of local:

```powershell
python -m codexsync -c config.toml restore --target cloud --apply
```

Preview restore without writing:

```powershell
python -m codexsync -c config.toml restore --dry-run
```

## Session branches across machines

`sessions scan` compares every session branch on this machine against the copy
in the cloud folder and classifies each one: identical, a fast-forward in either
direction, an active/archive transition, or a divergence. It writes nothing and
runs while Codex is open, though the result is then marked volatile.

```powershell
python -m codexsync -c config.toml sessions scan --source-machine desktop --target-machine laptop --save-plan sessions-plan.json
```

The report names no session ids, thread names or record contents: a conflict is
addressed by its id alone.

A divergence is never resolved automatically — no interleaving, no sorting by
timestamp, no newer-mtime. Both branches stay where they are and the plan blocks
until a decision is recorded:

```powershell
python -m codexsync -c config.toml sessions resolve --plan sessions-plan.json --conflict <conflict-id> --choice KEEP_LOCAL --output resolutions.json
python -m codexsync -c config.toml sessions scan --source-machine desktop --target-machine laptop --resolutions resolutions.json
```

A decision is pinned to the exact bytes it was made about. If either branch
changes afterwards it is refused as `STALE_RESOLUTION` rather than applied to a
history you never saw.

Applying a plan requires Codex to be closed and quotes the exact plan id. The
plan is rebuilt from the current state first and its id must still match, so any
change since the scan — a branch that grew, a new conflict, a decision gone
stale — refuses the apply rather than acting on a stale picture:

```powershell
python -m codexsync -c config.toml sessions apply --plan sessions-plan.json --confirm-plan <exact-plan-id> --dry-run
python -m codexsync -c config.toml sessions apply --plan sessions-plan.json --confirm-plan <exact-plan-id> --resolutions resolutions.json
```

A branch is transferred whole: nothing is appended to a destination and no
history is interleaved. The source branch is only ever read, the destination is
in a verified backup before it is replaced, and a branch that loses a resolution
is also copied into an immutable conflict bundle under `semantic.root_dir` —
backups are pruned by retention, so the bundle is what guarantees a divergent
history is never the sole copy in something that expires.

The layout gate applies to one direction only. Writing *into* your `.codex`
directory needs a proven target layout, because where the Codex runtime looks
for a session file is a property of that runtime: put the file somewhere else
and the session is silently invisible, with no error at all. Until a controlled
run records that layout, such an item is reported as `BLOCKED_UNPROVEN_LAYOUT`.
See [docs/experiments/session-layout-adapter.md](./docs/experiments/session-layout-adapter.md).

Writing *towards the cloud folder* is not gated. That copy is codexSync's own
mirror — no Codex reads it — so a branch simply keeps the relative path it has
locally (`mirror_layout_id` in the report records which mirror layout was used).
This is what lets a stale or missing cloud copy be rebuilt, since sessions are
semantic-owned and never copied by plain `sync`.

Because no runtime reads the mirror, a branch may be stored there compressed:
`semantic.mirror_compression` takes `none`, `gzip` or `xz` (default `xz`, which
measured about a fifth of the original size on real session data). Only the
mirror is affected — a branch written back into `.codex` is always plain JSONL.

The setting names the container for a branch the mirror does not hold yet. A
branch already there keeps the container it is stored in, reported as
`MIRROR_CONTAINER_KEPT`: the container is part of the file name, nothing deletes
the old name because `delete_policy` is `never`, and two names for one session
id would make the catalogue treat both as ambiguous and drop the session from
every later plan. Converting an existing mirror therefore needs a delete, and is
refused for the same reason an archive transition is.
Compression is a property of the container and never of the history: branch
hashes, record counts and every branch comparison are taken from the
decompressed stream, so a compressed mirror copy is `IDENTICAL` to the plain
local branch rather than a divergence. The container is named in the mirror
layout id and hashed into the plan id, so changing it invalidates an existing
plan instead of silently renaming every destination underneath a confirmation
you already gave.

An apply is therefore partial by design: a conflict or a target collision stops
it, because each names a decision only you can make, while items blocked on an
unproven layout or a SQLite-held binding are reported and left exactly where
they are. Active/archive transitions are also reported but not applied in 0.2:
moving a branch between `sessions/` and `archived_sessions/` requires a delete,
and `delete_policy` is `never`.

### The session index

`session_index.jsonl` is an append/update journal, not a list of the sessions
that exist: one id may appear on several lines, a session may have no line at
all, and a line may name a file that is gone. None of that is an error, and
codexSync never "cleans it up".

`sessions index` reports what each side's index holds and where the two
disagree. It reads only, runs while Codex is open, and names no session ids or
thread names — a divergent record is addressed by a hashed id, exactly like a
divergent branch.

```powershell
python -m codexsync -c config.toml sessions index
```

Two things are worth knowing about, and both are reported rather than acted on.
A repeated id has two plausible readings — the last line wins, or the greatest
`updated_at` wins — which differ exactly when a clock ran backwards;
disagreement is reported as `REDUCTION_AMBIGUOUS`, and `doctor` carries the same
check. And the two sides may hold a different record for one session, which is
a rename divergence and a decision rather than a merge.

No index is ever rewritten while the consumer contract is unproven, which the
report says as `UNPROVEN_CONSUMER_CONTRACT`. See
[docs/experiments/session-index-contract.md](./docs/experiments/session-index-contract.md).

## The GUI is not part of this release

0.2 is a command-line release. An optional extra exists as groundwork —
`codexsync[gui]`, a Qt-free controller, a launcher and one read-only screen —
but it is **not a finished interface**, and installing it will not let you drive
codexSync from a window. Everything below and above is the CLI.

The groundwork is kept because two properties are cheaper to establish than to
retrofit, and both are enforced by tests rather than by intent: the core and the
CLI keep zero runtime dependencies, so `pip install codexsync` never pulls Qt;
and nothing in the GUI package may reach the sync engine, the backup manager,
the operation lock, the journal or the safety gate. A second shell that decided
for itself when a write is allowed would leave two safety stories for one
operation, with only one of them written down.

## Finding a chat and putting it under a project

`chats tree` prints the projects with their chats underneath, and the chats that
belong to no project at the end. `chats list` is the same information filtered.
Both read only and run while Codex is open.

```powershell
python -m codexsync -c config.toml chats tree
python -m codexsync -c config.toml chats list --text "parser" --limit 20
python -m codexsync -c config.toml chats list --project none
```

Each row says *why* the chat sits where it does, and the three reasons behave
differently:

| Marker | Meaning |
|---|---|
| `pinned` | An explicit `thread-project-assignments` entry. Follows the project if its path changes. |
| `by path` | No entry; the chat is under the project only because its recorded directory falls under the project root. Moving the project leaves it behind. |
| `by rule!` | Its directory names *another machine's* path and only a `[[path_mappings]]` rule connects it to a project here. Codex does not read those rules, so it shows this chat under no project at all. |

A chat's own directory is not the only thing that has to agree. Codex keeps its
own catalogue of where each session's file lives, so a branch copied into
`.codex` at a path that catalogue does not name is simply never shown — no error
appears. Transfers into the state directory are therefore refused unless the
catalogue already points at exactly that file; a session it has never heard of
would need a new row, which codexSync does not write and reports as
`UNSUPPORTED_STATE_BACKEND`. Copies into the cloud folder are unaffected: nothing
has to find them afterwards.

That last one is what a machine handoff produces: the project lives under `C:` on
the laptop while every chat that came from the desktop still records `D:`. List
exactly those and pin them:

```powershell
python -m codexsync -c config.toml chats list --source-machine desktop --target-machine laptop --association DERIVED_VIA_MAPPING
```

Moving is a preview first. `chats move` writes nothing until you repeat it with
the plan id it printed, and Codex must be closed for the write:

```powershell
python -m codexsync -c config.toml chats move --chat 01a00ab4 --to LabTakt
python -m codexsync -c config.toml chats move --chat 01a00ab4 --to LabTakt --confirm <exact-plan-id>
```

There is no plan file to keep: the id covers the decisions *and* the exact bytes
of the state they were read from, so it stops matching the moment anything
changes and you are asked to look again. The write is one binding per chat, in
the shape the detected schema uses, behind the same envelope as every other
mutation — verified full backup first, process re-checked immediately before the
replace, and a verified rollback if anything afterwards fails. `--dry-run` runs
every check and writes nothing.

## Recovering an interrupted mutation

`sync`, `restore` and `repair-projects apply` write a durable journal. If one is
interrupted (power cut, forced shutdown) the journal stays open, and every later
mutation refuses to start until it is closed — that block is what keeps a
half-applied state from being mutated further. Two commands close it.

Read the evidence first (no side effects):

```powershell
python -m codexsync -c config.toml recover inspect <operation_id>
```

Retry the interrupted command. `resume` does not replay the lost plan: every
destination is replaced atomically, so after a crash each file is either fully
old or fully new, and re-running the original command re-plans against what is
actually on disk. `resume` verifies that the operation's backup is still intact,
then closes the journal so the command can be run again:

```powershell
python -m codexsync -c config.toml recover resume <operation_id>          # report only
python -m codexsync -c config.toml recover resume <operation_id> --apply
```

Undo instead, restoring the snapshot that operation created before it wrote
anything:

```powershell
python -m codexsync -c config.toml recover rollback <operation_id> --target cloud
python -m codexsync -c config.toml recover rollback <operation_id> --target cloud --apply
```

`--target` is required and never inferred: one `sync` run can back up files from
both sides and the backup manifest records only relative paths, so the side
cannot be proven from the snapshot alone.

Both commands default to a dry run and require Codex to be stopped. `rollback`
releases the journal only after the snapshot has been verified against its
committed manifest, so a rollback that cannot run leaves the block in place. A
snapshot with no committed manifest proves the commit phase was never entered —
the backup set is stamped before the first replace — so there is nothing to undo
and the journal is simply closed.

List CLI help:

```powershell
python -m codexsync -h
```

Process safety behavior:

- codexSync never starts or terminates Codex. Legacy termination CLI flags are rejected with exit code `4`, and `allow_terminate_if_running=true` is rejected for mutation commands.
- `sync`, `restore`, `repair-projects apply`, and recovery mutations require a continuously stopped two-second process window plus direct checks before and during commit.
- `RUNNING` and `UNKNOWN` both block mutation; macOS/Linux mutation remains blocked until a tested adapter is available.
- If a destination is momentarily held open by another process (cloud client, search indexer, antivirus), the atomic replace is retried with bounded backoff instead of failing the run. Process safety is re-checked before each attempt, and errors that are not a transient lock are not retried. See [D-011](./docs/DECISIONS.md).
- Background process tracking is configured by OS in `process_detection.background_process_names`:
  - `windows = ["codex-windows-sandbox"]`
  - `macos = []`
  - `linux = []`
- `--verbose` works for `plan`, `sync --dry-run`, `sync --apply`, `restore --dry-run`, and `restore --apply`; it logs tracked processes with PID/name.
- In verbose mode, codexSync logs only:
  - whether `codex.exe` is running,
  - whether `codex-windows-sandbox` is detected,
  - subprocesses under `codex.exe` (PID/name/parent PID). Full command lines are not collected.

## CLI exit codes

- `0` success
- `1` runtime error
- `2` conflict detected (manual resolution required)
- `3` Codex is running (cold sync precondition failed)
- `4` invalid config or CLI arguments
- `5` safe abort (`fail-safe`)

`doctor`/`preflight` return:

- `0` when all checks passed or warnings only
- `5` when at least one preflight check failed

## Required operation protocol

This tool assumes a strict handoff flow between machines:

1. Close Codex on machine A.
2. Wait until cloud sync fully propagates machine A changes.
3. Run codexSync on machine B.
4. Start Codex on machine B only after sync completes.
5. Sign in to Codex again on machine B after file sync.

Important: per OpenAI licensing constraints, authentication tokens are not transferred by codexSync.

The project intentionally does not verify cloud-provider sync status, cloud client process state, or free space on cloud/network storage. These are user responsibilities.

## Scheduler setup scripts

This repository includes editable scheduler setup templates:

- Windows Task Scheduler:
  - `scripts/scheduler/windows/task.config.ps1`
  - `scripts/scheduler/windows/install-task.ps1`
  - `scripts/scheduler/windows/remove-task.ps1`
  - detailed guide: [scripts/scheduler/windows/README.md](./scripts/scheduler/windows/README.md)
- macOS launchd (LaunchAgent):
  - `scripts/scheduler/macos/launchd.config.sh`
  - `scripts/scheduler/macos/install-launchd.sh`
  - `scripts/scheduler/macos/uninstall-launchd.sh`
  - detailed guide: [scripts/scheduler/macos/README.md](./scripts/scheduler/macos/README.md)

Windows install:

```powershell
cd scripts/scheduler/windows
# 1) Edit task.config.ps1
.\install-task.ps1
```

Windows remove:

```powershell
cd scripts/scheduler/windows
.\remove-task.ps1
```

macOS install:

```bash
cd scripts/scheduler/macos
# 1) Edit launchd.config.sh
chmod +x install-launchd.sh uninstall-launchd.sh run-codexsync.sh
./install-launchd.sh
```

macOS remove:

```bash
cd scripts/scheduler/macos
./uninstall-launchd.sh
```

Important:

- These scripts only register scheduled jobs, not OS services.
- Keep `MODE="dry-run"` while validating behavior; switch to `apply` only when ready.
- Cold sync protocol still applies: codexSync must run only when Codex is not running.

## Status

0.2 — command-line release. Guardian, a safety spine for every mutation, machine
handoff repair, semantic session transfer and chats.

Practically exercised Windows-to-Windows. macOS is supported in code and CI but
the end-to-end handoff has not been validated on real macOS machines. Two
capabilities are deliberately inert until a controlled experiment on disposable
state records what the Codex runtime actually does — writing a transferred
branch *into* `.codex`, and rewriting `session_index.jsonl`. Both are reported
rather than guessed; see [docs/experiments](./docs/experiments).

## Publishing

See release checklist: [docs/PUBLISHING.md](./docs/PUBLISHING.md)
Release notes: [CHANGELOG.md](./CHANGELOG.md)

## Licensing

This project uses dual licensing:

- Open-source license: `GPL-3.0-or-later` (see [LICENSE](./LICENSE))
- Commercial licensing path: see [COMMERCIAL_LICENSE.md](./COMMERCIAL_LICENSE.md)

Contributions are accepted under project contribution terms in:

- [CONTRIBUTING.md](./CONTRIBUTING.md)
- [CLA.md](./CLA.md)
