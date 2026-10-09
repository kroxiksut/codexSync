# AGENTS.md

## Project

codexSync hands Codex work between a person's own machines through a folder
they already sync (Dropbox, OneDrive, Yandex.Disk, Syncthing...): settings,
chats, chat names and the project list, with backups, recovery and a window on
top of the command line. Version line: 0.2 (alpha `0.2.0a1`).

Binding rules are in `AI_RULES.md`; decisions with their reasons in
`docs/dev/DECISIONS.md` (`D-001`...`D-030`); architecture in `CLAUDE.md`. When
this file disagrees with them, they win.

## Goal

Let a developer close Codex on one machine and carry on on another, with every
chat, its name and its project where Codex looks for them, and nothing lost on
the way.

## Constraints

* Only operate on local files; no Codex APIs, no network interception
* Do not extract credentials or tokens (`auth.json` is never copied, at any depth)
* Do not modify Codex binaries
* Do not check cloud client process state or free space on cloud storage
* Never terminate Codex by force. With `[sync] close_codex = true` a sync may
  *ask* the desktop app to quit (`D-029`) and refuses if it does not
* Codex's SQLite is written in exactly two narrow places, with Codex closed,
  inside the mutation envelope: `backfill_state` reset so Codex rebuilds its
  chat list (`D-024`) and `threads.name` where it is unset (`D-025`). Never a
  `threads` row, never project records, never `session_index.jsonl`

## Sync model

* Cold sync only: writes happen only while Codex is not running
* Single active machine at a time; handoff: close Codex on A, wait for the
  cloud, sync on B, then start Codex on B
* `sync` is a full sync by default (`D-028`): settings, chats, projects, chat
  names, the catalogue request. `scope = "settings"` is files only
* A conflict is decided by `[conflict] policy` (`D-027`, default
  `prefer_newer_mtime`); the copy not kept is saved, a tie asks. Two histories
  are never merged

## Delivered

* 0.1 MVP: process detection, state directory, timestamp-then-hash comparison,
  backup before overwrite, temp/lock/cache exclusion
* One mutation authority (`safety_gate`) and one envelope: lock, journal,
  verified backup, final process check, atomic replace; `recover` for an
  interrupted write
* Guardian snapshots of the global state while Codex runs (outside `.codex`)
* Chat transfer by branch classification, in place (`D-019`) and for chats a
  machine never held (`new_chats = "same_path"`, `D-020`); archive moves
  (`D-023`); paged chats (`D-027`)
* Project list merge between machines (`D-022`), project folder comparison
  (`D-026`), `repair-projects`, `chats move`, project move
* Machine handoff record and watcher (`D-018`), sign-in sync (`D-016`), copies
  of `.codex` (`D-017`), OS tasks for each
* The window: optional `codexsync[gui]` / `codexsync-gui.exe`, a second shell
  over the same core; every window action also exists in the CLI

## Still gated (empty `PROVEN_*`, filled only from an experiment)

* `PROVEN_LAYOUTS`: a general placement rule for new sessions (today:
  `same_path`)
* `PROVEN_CONTRACTS`: rewriting `session_index.jsonl`
* `PROVEN_DETECTORS`: macOS/Linux process detection; writes refused there
* `PROVEN_PROJECT_REGISTRY`: project delete/merge, SQLite project roots

## Expected output

* CLI first (Python 3.11+, zero runtime dependencies in core and CLI)
* Config file, logging, dry runs, `doctor`/`preflight`
* Windows and macOS CI; Windows `.exe` builds (x64, ARM64 — what Codex ships for; no 32-bit)
