# macOS Scheduler Scripts

This folder contains scripts that install and run a `codexSync` job via **launchd** (`LaunchAgent`).

## Files

- `launchd.config.sh`: user-editable settings.
- `install-launchd.sh`: validates config, writes plist, loads/enables LaunchAgent.
- `run-codexsync.sh`: runner executed by launchd.
- `uninstall-launchd.sh`: unloads and removes plist.

## What The Code Does

`install-launchd.sh`:

- Loads `launchd.config.sh` (or custom config path).
- Validates required values (`LABEL`, `PYTHON_BIN`, `PROJECT_DIR`, `CONFIG_FILE`, `MODE`, `INTERVAL_SECONDS`, log paths).
- Validates:
  - `MODE` is `dry-run` or `apply`
  - `INTERVAL_SECONDS >= 60`
- Ensures `run-codexsync.sh` is executable.
- Generates plist at:
  - `~/Library/LaunchAgents/<LABEL>.plist`
- Configures plist:
  - `ProgramArguments`: runner + parameters from config
  - `WorkingDirectory`: `PROJECT_DIR`
  - `StartInterval`: `INTERVAL_SECONDS`
  - `RunAtLoad`: from `RUN_AT_LOAD`
  - stdout/stderr log files
- Reloads agent (`bootout` if exists, then `bootstrap` and `enable`).

`run-codexsync.sh`:

- Switches to `PROJECT_DIR`.
- Runs:
  - `python -m codexsync -c <CONFIG_FILE> sync --dry-run` if `MODE=dry-run`
  - `python -m codexsync -c <CONFIG_FILE> sync --apply` if `MODE=apply`
- Exits with the same exit code as `codexsync`.

`uninstall-launchd.sh`:

- Loads `LABEL` from config.
- Unloads the agent and removes plist file.

## Where Schedule Parameters Come From

All runtime and schedule parameters come from `launchd.config.sh`.

- `INTERVAL_SECONDS` controls repeat period.
- `RUN_AT_LOAD=true|false` controls immediate run after loading.
- `MODE` controls `--dry-run` vs `--apply`.

Examples:

- Every 15 minutes: `INTERVAL_SECONDS=900`
- Every 1 hour: `INTERVAL_SECONDS=3600`
- Every 2 hours: `INTERVAL_SECONDS=7200`

## Install

Before scheduler setup, create a config file from packaged template:

```bash
python3 -m codexsync init-config --output /Users/<you>/codexSync/config.toml
```

```bash
cd scripts/scheduler/macos
chmod +x install-launchd.sh uninstall-launchd.sh run-codexsync.sh
# edit launchd.config.sh
./install-launchd.sh
```

Optional custom config file:

```bash
./install-launchd.sh "/path/to/my-launchd.config.sh"
```

## Uninstall

```bash
cd scripts/scheduler/macos
./uninstall-launchd.sh
```

## Manual Run (Desktop Launcher Script)

If you want manual launch from Desktop (outside launchd), create a file like `~/Desktop/run-codexsync-now.command`:

```bash
#!/usr/bin/env bash
set -euo pipefail

"/Users/<you>/codexSync/scripts/scheduler/macos/run-codexsync.sh" \
  "/usr/bin/python3" \
  "/Users/<you>/codexSync" \
  "/Users/<you>/codexSync/config.toml" \
  "dry-run"
```

Then make it executable:

```bash
chmod +x ~/Desktop/run-codexsync-now.command
```

Double-clicking this `.command` file in Finder runs the sync once.

You can also keep your edited config on Desktop and install from it:

```bash
"/Users/<you>/codexSync/scripts/scheduler/macos/install-launchd.sh" \
  "$HOME/Desktop/launchd.config.sh"
```

## Notes

- These scripts create a LaunchAgent schedule, not a macOS service/daemon.
- Keep `MODE="dry-run"` until behavior is validated.
- Follow cold sync protocol: Codex must be closed when sync runs.
