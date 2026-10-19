# codexSync documentation

**English** · [Русский](../ru/README.md) · [中文](../zh/README.md)

[← Project page](../../README.md)

| Page | What it covers |
|---|---|
| [The window](GUI.md) | All thirteen screens with screenshots |
| [Command line](CLI.md) | Every command, global options, exit codes |
| [Configuration](CONFIGURATION.md) | `config.toml` section by section, automation |
| [Synchronisation](SYNC.md) | `plan` and `sync`: comparison, conflicts, direction, deletions |
| [Guardian](GUARDIAN.md) | Snapshots of the global state, quarantine, restore, a new baseline |
| [Sessions](SESSIONS.md) | Session branches across machines, working set, mirror, index |
| [Projects and chats](PROJECTS.md) | Chat bindings, repair after a handoff, moving a project |
| [Backups and recovery](RECOVERY.md) | Backups, restore, interrupted mutations, when Codex does not start |

[![The CodexSync window](../screenshots/en/02-overview.png)](GUI.md)

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
extra. While 0.2 is an alpha, pip needs `--pre`; without it, it installs 0.1.
`--upgrade` makes the same command replace an installed 0.1.

```powershell
python -m pip install --upgrade --pre "codexsync[gui]"    # the command line and the window (PySide6)
python -m pip install --upgrade --pre codexsync           # the command line only
```

On Windows you can instead download a build from
[Releases](https://github.com/kroxiksut/codexSync/releases), none of which
needs Python installed:

| Build | Windows |
|---|---|
| `codexsync-gui-<tag>-windows-amd64.zip`, `…-arm64.zip` — the window, which also runs every command | x64, ARM64 |
| `codexsync-<tag>-windows-amd64.zip`, `…-arm64.zip` — the command line only, without Qt | x64, ARM64 |

There is no 32-bit build: Codex itself is published for x64 and ARM64 only.

From source, for development or an unreleased change:

```powershell
git clone https://github.com/kroxiksut/codexSync
cd codexSync
pip install -e ".[gui]"
```

### On Linux

Every way below works and none is preferred; choose what suits the machine.
With root and `apt` or `dnf`, a package; without root, the AppImage or the
`tar.gz`; with Python 3.11 or later, `pipx` or a virtual environment. The
packages, the AppImage and the `tar.gz` are on the
[release page](https://github.com/kroxiksut/codexSync/releases) from the next
release on, for x64 (glibc 2.35 or later: Ubuntu 22.04, Debian 12, Fedora 36
and newer) and ARM64 (glibc 2.39 or later: Ubuntu 24.04, Debian 13 and newer).
Each holds both programs, `codexsync` and `codexsync-gui`.

**A .deb package** (Ubuntu, Debian and their relatives) installs into
`/opt/codexsync` with both commands on `PATH` and a menu entry:

```sh
sudo apt install ./codexsync-<version>-linux-amd64.deb   # install; the same with a newer file updates
sudo apt remove codexsync                                # remove
```

**An .rpm package** (Fedora and its relatives), the same way:

```sh
sudo dnf install ./codexsync-<version>-linux-amd64.rpm   # install; the same with a newer file updates
sudo dnf remove codexsync                                # remove
```

**The AppImage** is one file to keep wherever you like; it is the window and,
given a command, the command line:

```sh
chmod +x codexsync-<version>-linux-amd64.AppImage
./codexsync-<version>-linux-amd64.AppImage                          # the window
./codexsync-<version>-linux-amd64.AppImage -c config.toml doctor    # the command line
```

To update, put the new file at the same path under the same name (scheduled
tasks name that file); to remove it, delete it. It needs no `libfuse2`; where
FUSE is not available at all, add `--appimage-extract-and-run`.

**The tar.gz** is the same folder for any place you can write to; unpack it
into a folder of its own, so that an update keeps the path scheduled tasks
name:

```sh
mkdir -p ~/.local/opt/codexsync
tar -xzf codexsync-<version>-linux-amd64.tar.gz -C ~/.local/opt/codexsync --strip-components=1   # install
rm -rf ~/.local/opt/codexsync/* && tar -xzf <newer file> -C ~/.local/opt/codexsync --strip-components=1   # update
rm -rf ~/.local/opt/codexsync                                                                     # remove
```

The programs are `~/.local/opt/codexsync/codexsync` and `codexsync-gui`. To
call them by name, link both into `~/.local/bin`
(`ln -s ~/.local/opt/codexsync/codexsync ~/.local/opt/codexsync/codexsync-gui ~/.local/bin/`).

**pipx** makes and manages the virtual environment itself (`sudo apt install
pipx` or `sudo dnf install pipx`, then `pipx ensurepath`):

```sh
pipx install --pip-args=--pre "codexsync[gui]"   # the window and the command line; without [gui], the command line only
pipx upgrade --pip-args=--pre codexsync          # update
pipx uninstall codexsync                         # remove
```

**A virtual environment by hand** — the system Python refuses a plain
`pip install` (PEP 668); on Ubuntu and Debian `sudo apt install python3-venv`
first:

```sh
python3 -m venv ~/.venvs/codexsync
~/.venvs/codexsync/bin/pip install --pre "codexsync[gui]"            # install
~/.venvs/codexsync/bin/pip install --upgrade --pre "codexsync[gui]"  # update
rm -rf ~/.venvs/codexsync                                            # remove
```

Put `~/.venvs/codexsync/bin` on `PATH`, or call the commands by that path.
`--pre` is needed while 0.2 is a pre-release.

**The applications menu.** A `.deb` or `.rpm` puts CodexSync in it; pipx, a
virtual environment, the AppImage and the `tar.gz` do not. For those,
`codexsync shortcut install` (or *About → Applications menu* in the window)
writes a menu entry and an icon into `~/.local/share` that start the very
program you ran it with — the AppImage file, the unpacked folder, the virtual
environment. Run it again after moving that program; `codexsync shortcut
status` says what the entry starts, and `codexsync shortcut remove` takes away
the two files it wrote and nothing else. With a package installed it writes
nothing.

**Before removing codexSync**, take its scheduled tasks away —
`codexsync -c config.toml automation remove` (or *Automation* in the window),
and its menu entry if you made one (`codexsync shortcut remove`):
removing a program never touches your tasks, your config, the workspace or
`.codex`, so a task left behind would keep trying to start a program that is
gone. Your config and workspace stay where they are for a later install.

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
- **Codex's own files, and a narrow boundary in them.** codexSync uses no Codex
  API, reads no tokens, never terminates Codex by force, and its only edits to
  Codex's SQLite are the three listed under
  [what it does not do](#what-it-does-not-do).
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

- No Codex API, no network interception, no change to Codex itself.
- No token extraction: after a handoff you sign in to Codex again.
- Never terminates Codex by force. With `[sync] close_codex = true` a sync asks
  the desktop app to quit the normal way and writes only after the process
  check confirms it has stopped; if it does not quit, nothing is written.
- Nothing in Codex's SQLite databases beyond three narrow edits, each made with
  Codex closed, after a verified backup and only when needed: Codex's chat-list
  rebuild status (`backfill_state`) — set to rebuild when you ask for it, and
  back to complete when a rebuild is stuck and Codex cannot start (`D-032`) —
  for a chat with no name here, the name the other machine shows; and, for a
  chat archived or brought back on the other machine, its place and archive
  mark in Codex's list (`D-035`). It never creates or
  deletes chat rows, never rewrites `session_index.jsonl` and never modifies
  project records.
- No real-time sync: one machine works at a time.
- No checks of the cloud client or of free space in the cloud folder.

## Platforms

- **Windows** is the tested platform.
- **macOS** (Apple Silicon) is supported in code and CI. The process detector
  for macOS is written and tested against recorded `ps` output, but until it
  has been run against a live Codex on a Mac, the platform reports itself as
  unsupported: the process state reads as undetermined and every command that
  writes refuses. `codexsync codex processes` shows what the check sees there
  — matches by process id and rule, no path — and what it would read if it
  were trusted; that report is what proves it ([Have a Mac?](../../README.md#have-a-mac)).
- **Linux** is experimental, as Codex's own desktop app for Linux is a preview
  (since August 2026). codexSync installs from PyPI and its code and CI run
  there. The process detector has been watched against a live Codex on Ubuntu
  26.04 (desktop app 26.1002, 2026-10-09), so commands that write are open; a
  full handoff between Linux and another machine has not been tried yet. A
  command Codex started in a chat can outlive the app, and until it ends Codex
  reads as running.
- **Minimum versions:** Windows 10 (1809) or later, Python 3.11 or later with
  pip; the window on macOS needs macOS 13 or later (what the current PySide6
  supports). Python 3.14 or later reads the chats Codex keeps compressed
  (`.jsonl.zst`); older versions leave them blocked
  ([sessions](SESSIONS.md#when-codex-compresses-its-chats)). The release
  programs are built with 3.14.
- CI runs the test suite on `windows-latest` and `macos-latest` with Python
  3.11, 3.12, 3.13 and 3.14, and on `ubuntu-latest` as an experimental job
  whose failure does not fail the run.

## What is not proven yet

Some behaviour of the Codex runtime cannot be learned from its files, only
observed. Until a controlled experiment on disposable state records it,
codexSync reports the case instead of guessing:

| What | How it shows | Experiment |
|---|---|---|
| A general rule for where Codex expects a session this machine has never had. Until then a chat it already has is written over its own file, and a new one at its path on the other machine (`[semantic] new_chats = "same_path"`, the default) | new chats are written by that one rule, and `doctor` counts chats Codex has not listed (`session_visibility`); with `new_chats = "keep_in_cloud"` they stay in the cloud copy (`BLOCKED_UNPROVEN_LAYOUT`) | [session-layout-adapter](../dev/experiments/session-layout-adapter.md) |
| Rewriting `session_index.jsonl` | `UNPROVEN_CONSUMER_CONTRACT` | [session-index-contract](../dev/experiments/session-index-contract.md) |
| Detecting Codex on macOS (observed on Linux) | platform unsupported, writes refused | [process-detector-macos](../dev/experiments/process-detector-macos.md) |
| Projects stored in `state_*.sqlite` | moving a project rewrites the JSON only; deleting or merging projects is not offered | [project-registry-contract](../dev/experiments/project-registry-contract.md) |

`doctor` reports the last one on every run.
