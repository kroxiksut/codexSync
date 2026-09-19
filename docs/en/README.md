# codexSync documentation

**English** · [Русский](../ru/README.md) · [中文](../zh/README.md)

[← Project page](../../README.md)

| Page | What it covers |
|---|---|
| [The window](GUI.md) | All eleven screens with screenshots |
| [Command line](CLI.md) | Every command, global options, exit codes |
| [Configuration](CONFIGURATION.md) | `config.toml` section by section, automation |
| [Synchronisation](SYNC.md) | `plan` and `sync`: comparison, conflicts, direction, deletions |
| [Guardian](GUARDIAN.md) | Snapshots of the global state, quarantine, restore, a new baseline |
| [Sessions](SESSIONS.md) | Session branches across machines, working set, mirror, index |
| [Projects and chats](PROJECTS.md) | Chat bindings, repair after a handoff, moving a project |
| [Backups and recovery](RECOVERY.md) | Backups, restore, interrupted mutations |

[![The CodexSync window](../screenshots/en/01-overview.png)](GUI.md)

*The window over the same core as the command line — [every screen, with screenshots](GUI.md).*

## Why

A developer wants to continue working with Codex on another machine without
losing its local state: the projects, which chat belongs to which project, and
the history of every session. Codex keeps all of that in a local directory
(`.codex`), and a cloud folder alone cannot carry it safely — a file copied
while Codex writes it, or a history that grew on both machines, is lost without
an error.

## How it works

1. **Is Codex running?** An *undetermined* answer counts as running: nothing
   that changes state is ever optimistic.
2. **Reading is always allowed.** `doctor`, `plan`, every `scan`, `chats` and
   Guardian run whether Codex is open or not. A result taken while Codex is open
   is marked `volatile` and cannot be reused by a write.
3. **A write runs only with Codex closed, and always the same way:** a lock
   nobody else can take, a durable journal, a verified backup of everything that
   will be replaced, a final process check right before the commit, staging on
   the same volume, an atomic replace, then `COMMITTED`.
4. **Anything ambiguous stops** instead of guessing, with an
   [exit code](CLI.md#exit-codes) that says which kind of stop it was.

## Install

The core and the command line have no dependencies. The window is an optional
extra.

```powershell
git clone https://github.com/kroxiksut/codexSync
cd codexSync
pip install .            # command line only
pip install ".[gui]"     # command line and the window (PySide6)
```

On Windows you can instead download a build from
[Releases](https://github.com/kroxiksut/codexSync/releases), neither of which
needs Python installed: `CodexSync-gui-<tag>-windows-amd64.zip` (the window,
which is also the command line) or `codexsync-<tag>-windows-amd64.zip` (command
line only, without Qt).

## First run

**The window:**

```powershell
codexsync-gui
```

The *First run* screen writes `config.toml`: the machine name, the local
`.codex` and the synced workspace folder. Every screen is shown in
[the window](GUI.md).

**The command line:**

```powershell
codexsync init-config --output config.toml --machine-id desktop --local-state-dir C:/Users/me/.codex --workspace-root D:/Cloud/codexSync
codexsync -c config.toml doctor           # read-only diagnostics
codexsync -c config.toml sync --dry-run   # what a sync would do, writes nothing
codexsync -c config.toml sync --apply     # Codex must be closed
```

Every command and its options are in [the command line](CLI.md); the file
`init-config` writes is explained section by section in
[configuration](CONFIGURATION.md).

## Design principles

- **Backup first, and fail closed.** Uncertainty is never resolved
  optimistically.
- **One authority, one envelope.** Exactly one place decides whether state may
  change, and exactly one path performs the change.
- **Say why, do not guess.** A branch that cannot be classified, a project that
  matches two candidates, a runtime behaviour nobody has observed — each is
  reported with a code, not approximated.
- **Plan, then confirm by id.** Every command that writes shows a plan or a dry
  run first and applies only the exact plan id you quote back. If anything
  changed in between, the id no longer matches and nothing is written.
- **No integration with Codex internals.** codexSync never starts or stops
  Codex, reads no tokens and writes no SQLite.
- **Zero runtime dependencies** in the core and the command line.

## Handoff protocol

codexSync assumes a strict order between machines:

1. Close Codex on machine A.
2. Wait until the cloud client has fully uploaded machine A's changes.
3. Run codexSync on machine B.
4. Start Codex on machine B only after the sync has finished.
5. Sign in to Codex again on machine B.

Authentication tokens are never transferred. codexSync deliberately does not
check the cloud provider's sync status, the cloud client process or free space
in the cloud folder: those are the user's responsibility.

## What it does not do

- No integration with Codex internals, no API usage, no network interception.
- No token extraction: after a handoff you sign in to Codex again.
- Never starts or stops Codex, and never writes Codex's SQLite databases.
- No real-time sync: one machine works at a time.
- No checks of the cloud client or of free space in the cloud folder.

## Platforms

- **Windows** is the tested platform.
- **macOS** (Apple Silicon) is supported in code and CI. The process detector
  for macOS and Linux is written and tested against recorded `ps` output, but
  until it has been run against a live Codex, the platform reports itself as
  unsupported: the process state reads as undetermined and every command that
  writes refuses.
- **Linux** runtime support is out of scope for now.
- CI runs the test suite on `windows-latest` and `macos-latest` with Python
  3.11, 3.12 and 3.13.

## What is not proven yet

Some behaviour of the Codex runtime cannot be learned from its files, only
observed. Until a controlled experiment on disposable state records it,
codexSync reports the case instead of guessing:

| What | How it shows | Experiment |
|---|---|---|
| Writing a transferred session branch *into* `.codex` | `BLOCKED_UNPROVEN_LAYOUT` | [session-layout-adapter](../dev/experiments/session-layout-adapter.md) |
| Rewriting `session_index.jsonl` | `UNPROVEN_CONSUMER_CONTRACT` | [session-index-contract](../dev/experiments/session-index-contract.md) |
| Detecting Codex on macOS and Linux | platform unsupported, writes refused | [process-detector-macos](../dev/experiments/process-detector-macos.md) |
| Projects stored in `state_*.sqlite` | moving a project rewrites the JSON only; deleting or merging projects is not offered | [project-registry-contract](../dev/experiments/project-registry-contract.md) |

`doctor` reports the last one on every run.
