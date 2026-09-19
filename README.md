<p align="center">
  <img src="assets/brand/brand-mark.png" width="96" alt="CodexSync">
</p>

<h1 align="center">codexSync</h1>

<p align="center">
  Carry your local Codex state — projects, chat bindings and session history —
  between personal machines through a cloud-synced folder.
</p>

<p align="center">
  <a href="https://github.com/kroxiksut/codexSync/actions/workflows/ci.yml"><img src="https://github.com/kroxiksut/codexSync/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://github.com/kroxiksut/codexSync/releases"><img src="https://img.shields.io/github/v/release/kroxiksut/codexSync?include_prereleases&sort=semver" alt="Release"></a>
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-3776AB?logo=python&logoColor=white" alt="Python 3.11 | 3.12 | 3.13">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS-lightgrey" alt="Platform: Windows | macOS">
  <img src="https://img.shields.io/badge/runtime%20dependencies-0-brightgreen" alt="Runtime dependencies: 0">
  <img src="https://img.shields.io/badge/GUI-PySide6%2C%20optional-41CD52?logo=qt&logoColor=white" alt="GUI: PySide6, optional">
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-GPL--3.0--or--later-blue" alt="License: GPL-3.0-or-later"></a>
</p>

<p align="center">
  <b>English</b> · <a href="./README.ru.md">Русский</a> · <a href="./README.zh.md">中文</a>
</p>

> [!IMPORTANT]
> Validated in practice Windows → Windows only. macOS is supported in code and
> CI, but an end-to-end handoff on real macOS machines has not been validated.

<p align="center">
  <a href="docs/en/GUI.md"><img src="docs/screenshots/en/01-overview.png" width="85%" alt="The CodexSync window: overview"></a>
</p>

## What it does

- **Syncs** the Codex state directory through any cloud folder — backup first,
  and only while Codex is closed. → [Synchronisation](docs/en/SYNC.md)
- **Guards the global state** with verified snapshots taken *while Codex runs*,
  written only outside `.codex`. → [Guardian](docs/en/GUARDIAN.md)
- **Moves session history between machines**, classifying every branch; a
  divergence is never merged or decided by timestamp.
  → [Sessions](docs/en/SESSIONS.md)
- **Finds a chat and puts it under a project**, repairs bindings after a machine
  handoff, moves a project's files. → [Projects and chats](docs/en/PROJECTS.md)
- **Recovers from an interrupted write** — lock, journal, verified backup.
  → [Backups and recovery](docs/en/RECOVERY.md)

Neither an integration with Codex internals nor a real-time sync:
[what it does not do](docs/en/README.md#what-it-does-not-do).

## Install

```powershell
pip install ".[gui]"     # command line and the window; `pip install .` for the CLI only
codexsync-gui            # or: codexsync -c config.toml doctor
```

Windows builds without Python are in
[Releases](https://github.com/kroxiksut/codexSync/releases). Full instructions
and the first run: [installing and getting started](docs/en/README.md#install).

## Documentation

| | |
|---|---|
| [Overview](docs/en/README.md) | How it works, install, first run, design principles, platforms |
| [The window](docs/en/GUI.md) | All eleven screens with screenshots |
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

0.2 — the first release with the window (`codexsync[gui]` or `CodexSync.exe`)
next to the command line. A few runtime behaviours are deliberately left unused
until a controlled experiment records them — see
[what is not proven yet](docs/en/README.md#what-is-not-proven-yet).

## License

Dual-licensed: open source under `GPL-3.0-or-later` ([LICENSE](./LICENSE)), with
a commercial path described in [COMMERCIAL_LICENSE.md](./COMMERCIAL_LICENSE.md).
Contributions are accepted under [CONTRIBUTING.md](./CONTRIBUTING.md) and
[CLA.md](./CLA.md).
