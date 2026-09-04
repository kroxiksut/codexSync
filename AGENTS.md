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
