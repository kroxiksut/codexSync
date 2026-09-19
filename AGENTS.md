# AGENTS.md

## Project

codexSync is a local-first utility for syncing Codex state between two personal machines.

## Goal

Enable a developer to continue work on another machine with preserved Codex local state.

## Constraints

* Only operate on local files
* Do not interact with Codex APIs
* Do not extract credentials or tokens
* Do not intercept network traffic
* Do not modify Codex binaries or runtime
* Do not check cloud client process state
* Do not check free space on cloud/network storage

## Sync model

* Cold sync only
* Sync happens only when Codex is NOT running
* Single active machine at a time
* Handoff protocol is mandatory: close Codex on source machine, wait for cloud propagation, then sync on target machine

## MVP requirements

Implement:

1. Detect Codex process (Windows)
2. Detect Codex state directory
3. Compare timestamps (local vs cloud)
4. Sync changes
5. Backup before overwrite
6. Exclude temp/lock/cache files

## 0.2 capabilities

The MVP list above is delivered. 0.2 adds, on the same safety model:

7. Guardian: immutable, verified snapshots of `.codex-global-state.json` taken
   while Codex is running, written only outside `.codex`
8. One authority over every mutation (`safety_gate`) and one envelope around it:
   lock, durable journal, verified backup, final process check, atomic replace
9. `recover inspect|resume|rollback` as the only way out of an interrupted
   mutation
10. `repair-projects`: rebuild project bindings after a machine handoff, as an
    exact plan confirmed by its id
11. `sessions`: semantic classification and transfer of session branches; a
    divergence is reported, never merged
12. `chats`: find a chat, see why it sits where it does, move it under a project

Not delivered and deliberately inert until a controlled experiment records the
runtime's real behaviour: writing a transferred branch into `.codex`, and
rewriting `session_index.jsonl`.

The window ships as the optional `codexsync[gui]` extra (and the windowed
`CodexSync.exe`): a second shell over the same core, which keeps zero runtime
dependencies.

## Safety rules

* Never write into state while Codex is running
* Always create backup before overwrite
* Fail safely if uncertain
* Cloud sync readiness and cloud storage capacity are user responsibilities

## Expected output

* CLI tool (Python preferred)
* Config file support
* Logging
* Dry-run mode
* Doctor/preflight diagnostics mode before sync
