# Windows Scheduler Scripts

This folder contains scripts that install and run a `codexSync` job in **Windows Task Scheduler**.

## Files

- `task.config.ps1`: user-editable settings.
- `install-task.ps1`: validates config and registers/updates the scheduled task.
- `run-codexsync.ps1`: actual runner executed by Task Scheduler.
- `remove-task.ps1`: unregisters the task by name from config.

## What The Code Does

`install-task.ps1`:

- Loads `task.config.ps1` (or custom config path).
- Validates required values (`TaskName`, `PythonExe`, `ProjectDir`, `ConfigFile`, `Mode`, `LogDir`).
- Validates schedule (`IntervalMinutes >= 1`, `StartTime` format `HH:mm`).
- Builds scheduler action:
  - Program: `powershell.exe`
  - Arguments: run `run-codexsync.ps1` with values from config
- Creates a repeating trigger:
  - first run at next local `StartTime`
  - repeat every `IntervalMinutes`
  - long duration (about 10 years)
- Registers task with `-Force` (reinstall/update behavior).

`run-codexsync.ps1`:

- Creates log directory if missing.
- Runs:
  - `python -m codexsync -c <ConfigFile> sync --dry-run` if `Mode=dry-run`
  - `python -m codexsync -c <ConfigFile> sync --apply` if `Mode=apply`
- Writes start/finish markers and process output to `codexsync-task.log`.
- Returns the same exit code as `codexsync`.

`remove-task.ps1`:

- Reads `TaskName` from config.
- Removes scheduled task if it exists.

## Where Schedule Parameters Come From

All runtime and schedule parameters come from `task.config.ps1`.

- `IntervalMinutes` controls repeat period.
- `StartTime` controls first launch time (`HH:mm`, local timezone).
- `Mode` controls `--dry-run` vs `--apply`.

Examples:

- Every 15 minutes: `IntervalMinutes = 15`
- Every 1 hour: `IntervalMinutes = 60`
- Every 2 hours: `IntervalMinutes = 120`

## Install

Before scheduler setup, create a config file from packaged template:

```powershell
python -m codexsync init-config --output D:\codexSync\config.toml
```

```powershell
cd scripts/scheduler/windows
# edit task.config.ps1
.\install-task.ps1
```

Optional custom config file:

```powershell
.\install-task.ps1 -ConfigPath "D:\path\to\my-task.config.ps1"
```

## Uninstall

```powershell
cd scripts/scheduler/windows
.\remove-task.ps1
```

## Manual Run (Desktop Shortcut / Script)

If you want manual launch from Desktop (outside scheduler), create a small script on Desktop, for example `Run-codexSync-Now.ps1`:

```powershell
& "D:\YcodexSync\scripts\scheduler\windows\run-codexsync.ps1" `
  -PythonExe "python" `
  -ProjectDir "D:\codexSync" `
  -ConfigFile "D:\codexSync\config.toml" `
  -Mode "dry-run" `
  -LogDir "D:\codexSync\logs"
```

You can also copy `task.config.ps1` to Desktop and pass that config into `install-task.ps1`:

```powershell
& "D:\codexSync\scripts\scheduler\windows\install-task.ps1" `
  -ConfigPath "$env:USERPROFILE\Desktop\task.config.ps1"
```

## Notes

- These scripts create a scheduled task, not a Windows service.
- Keep `Mode = "dry-run"` until behavior is validated.
- Follow cold sync protocol: Codex must be closed when sync runs.
