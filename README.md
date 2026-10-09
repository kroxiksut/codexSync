<p align="center">
  <img src="assets/brand/brand-mark.png" width="96" alt="CodexSync">
</p>

<h1 align="center">codexSync</h1>

<p align="center">
  Move your Codex work safely between personal machines — chats, project lists,
  configuration and local state — with guarded handoff, conflict handling,
  verified backups and recovery.
</p>

<p align="center">
  <a href="https://github.com/kroxiksut/codexSync/actions/workflows/ci.yml"><img src="https://github.com/kroxiksut/codexSync/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://github.com/kroxiksut/codexSync/releases"><img src="https://img.shields.io/github/v/release/kroxiksut/codexSync?include_prereleases&sort=semver" alt="Release"></a>
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-3776AB?logo=python&logoColor=white" alt="Python 3.11 | 3.12 | 3.13 | 3.14">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey" alt="Platform: Windows | macOS | Linux">
  <img src="https://img.shields.io/badge/runtime%20dependencies-0-brightgreen" alt="Runtime dependencies: 0">
  <img src="https://img.shields.io/badge/GUI-PySide6%2C%20optional-41CD52?logo=qt&logoColor=white" alt="GUI: PySide6, optional">
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0--or--later-blue" alt="License: GPL-3.0-or-later"></a>
</p>

<p align="center">
  <b>English</b> · <a href="./README.ru.md">Русский</a> · <a href="./README.zh.md">中文</a>
</p>

> [!IMPORTANT]
> Validated in practice Windows → Windows only. macOS and Linux are
> experimental: reads, diagnostics and plans work, and writes stay closed until
> process detection has been proven against a live Codex there (Codex's own
> Linux app is a preview too).

<p align="center">
  <a href="docs/en/GUI.md"><img src="docs/screenshots/en/02-overview.png" width="85%" alt="The CodexSync window: overview"></a>
</p>

Codex keeps important working state on your machine. Putting `.codex` into
Dropbox, OneDrive or Syncthing is not enough: Codex may still be writing to it,
the same chat may be continued differently on two machines, and an interrupted
overwrite can destroy the very copy you needed. codexSync treats moving that
state as a guarded handoff, not as ordinary file synchronisation.

## What it does

- **Hands your work over between machines** — one click, or on its own when
  Codex closes: settings, chats and the project list go to the cloud folder, the next
  machine loads them, and each knows what the other handed off.
  → [Synchronisation](docs/en/SYNC.md#handing-work-over)
- **Carries chats and projects** — chat history, chat names and project
  bindings arrive where Codex looks for them, even when a project folder has
  another path on the other machine. A project's own files are not copied: a
  full sync compares the folders and says what is missing.
  → [Projects and chats](docs/en/PROJECTS.md)
- **Never merges two histories** — every session branch is classified; a chat
  continued on both machines keeps one whole copy, by your rule or your choice,
  and the other is saved. → [Sessions](docs/en/SESSIONS.md)
- **Backs up before every overwrite** and recovers from an interrupted write —
  lock, journal, verified backup. → [Backups and recovery](docs/en/RECOVERY.md)
- **Guards the global state** with verified snapshots taken *while Codex runs*,
  written only outside `.codex`. → [Guardian](docs/en/GUARDIAN.md)
- **Can run on its own** — the handoff watcher, copies of `.codex` and periodic
  snapshots are ordinary tasks of your OS. → [Automation](docs/en/CONFIGURATION.md#automation)

Not a Codex client and not a real-time sync. It never terminates Codex by
force, and edits Codex's databases only in two narrow, backed-up ways:
[what it does not do](docs/en/README.md#what-it-does-not-do).

## Privacy and safety

- **Local-first.** codexSync has no service of its own: shared state goes only
  through the folder you choose.
- **Your sign-in never travels.** `auth.json` and other credential files are
  never copied, at any depth.
- **Writes only while Codex is closed.** When that cannot be determined, nothing
  is written. If you allow it, a sync asks Codex to quit first — never forcibly.
- **Nothing is replaced without a verified backup**, and every write is
  journaled, so an interrupted one can be resumed or rolled back.

Found a security or data-safety problem? See [SECURITY.md](./SECURITY.md).

## Install

**Windows, no Python needed:** download `codexsync-gui-…-windows-amd64.zip`
(or `…-arm64.zip`) from [Releases](https://github.com/kroxiksut/codexSync/releases),
unpack it and run `codexsync-gui.exe`. The same executable can also run CLI
subcommands. Windows 10 (1809) or later.

**With Python 3.11+**, on Windows, macOS or Linux — `--pre` while 0.2 is an
alpha, or pip installs 0.1, and `--upgrade` so that an installed 0.1 is
replaced too. On macOS and Linux every read and plan works, but writes are
refused until the process detector is proven there
([platforms](docs/en/README.md#platforms)). On Linux the system Python refuses
`pip install` (PEP 668), so install into a virtual environment:
`python3 -m venv ~/.venvs/codexsync && ~/.venvs/codexsync/bin/pip install --pre "codexsync[gui]"`
(on Ubuntu and Debian `sudo apt install python3-venv` first).

```powershell
python -m pip install --upgrade --pre "codexsync[gui]"    # the window and the command line
codexsync-gui                                             # on first launch, the window helps you create or open config.toml

python -m pip install --upgrade --pre codexsync           # the command line only, no dependencies
codexsync init-config --output config.toml
codexsync -c config.toml doctor
```

From source, and the first run step by step:
[installing and getting started](docs/en/README.md#install).

## Documentation

| | |
|---|---|
| [Overview](docs/en/README.md) | How it works, install, first run, design principles, platforms |
| [The window](docs/en/GUI.md) | All thirteen screens with screenshots |
| [Command line](docs/en/CLI.md) | Every command, global options, exit codes |
| [Configuration](docs/en/CONFIGURATION.md) | `config.toml` section by section, automation |
| [Synchronisation](docs/en/SYNC.md) | `plan` and `sync`: comparison, conflicts, direction, deletions |
| [Guardian](docs/en/GUARDIAN.md) | Snapshots of the global state, quarantine, restore, a new baseline |
| [Sessions](docs/en/SESSIONS.md) | Session branches across machines, working set, mirror, index |
| [Projects and chats](docs/en/PROJECTS.md) | Chat bindings, repair after a handoff, moving a project |
| [Backups and recovery](docs/en/RECOVERY.md) | Backups, restore, interrupted mutations |

Every page in one place: [docs/](docs/README.md). For contributors:
[developer documents](docs/dev/README.md). Release notes:
[CHANGELOG.md](./CHANGELOG.md).

## Status

**0.2.0 alpha 1** — the first release with the window (`codexsync[gui]` or
`codexsync-gui.exe`) next to the command line. It is an alpha because 0.2
understands and changes far more of Codex's state than 0.1: it is in daily use,
and every write still goes through the lock, journal and verified backup, but it
needs more machines than ours before 0.2.0. If you use Codex on two or more
machines, please try it and [tell us](https://github.com/kroxiksut/codexSync/issues/new/choose)
what happened. Data an alpha writes stays readable by every later version.

**Upgrading from 0.1.** codexSync 0.2 recognises configurations created by 0.1
and upgrades them in place, from the window or with `config check` and
`config upgrade`: a few 0.1 values are refused by every command that writes,
and the upgrade shows each change before making it. Existing state is kept, and
the previous configuration is preserved in `config-history/`.
→ [Upgrading a config](docs/en/CONFIGURATION.md#upgrading-a-config-from-an-earlier-version)

A few runtime behaviours are deliberately left unused
until a controlled experiment records them — see
[what is not proven yet](docs/en/README.md#what-is-not-proven-yet).

## License

Dual-licensed: open source under `GPL-3.0-or-later` ([LICENSE](./LICENSE)), with
a commercial path described in [COMMERCIAL_LICENSE.md](./COMMERCIAL_LICENSE.md).
Contributions are accepted under [CONTRIBUTING.md](./CONTRIBUTING.md) and
[CLA.md](./CLA.md).
