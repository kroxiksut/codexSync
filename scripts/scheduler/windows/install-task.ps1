param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot "task.config.ps1")
)

$ErrorActionPreference = "Stop"

# DEPRECATED: task.config.ps1 is a second source of scheduler settings next to
# config.toml. Kept working for existing installs; see docs/en/CONFIGURATION.md.
Write-Warning ("DEPRECATED: install-task.ps1 and task.config.ps1 will be removed in a later release. " +
    "Set [scheduler] in config.toml and run 'codexsync -c <config.toml> automation apply' instead. " +
    "If you continue, remove this task first (remove-task.ps1) before using automation apply, so two tasks do not run.")

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Config file not found: $ConfigPath"
}

. $ConfigPath

function Assert-RequiredString {
    param(
        [string]$Name,
        [string]$Value
    )
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "Required setting is empty: $Name"
    }
}

Assert-RequiredString -Name "TaskName" -Value $TaskName
Assert-RequiredString -Name "PythonExe" -Value $PythonExe
Assert-RequiredString -Name "ProjectDir" -Value $ProjectDir
Assert-RequiredString -Name "ConfigFile" -Value $ConfigFile
Assert-RequiredString -Name "Mode" -Value $Mode
Assert-RequiredString -Name "LogDir" -Value $LogDir

if ($Mode -notin @("dry-run", "apply")) {
    throw "Mode must be 'dry-run' or 'apply'"
}

if (-not ($IntervalMinutes -is [int]) -or $IntervalMinutes -lt 1) {
    throw "IntervalMinutes must be an integer >= 1"
}

$runnerScript = Join-Path $PSScriptRoot "run-codexsync.ps1"
if (-not (Test-Path -LiteralPath $runnerScript)) {
    throw "Runner script not found: $runnerScript"
}

if (-not (Test-Path -LiteralPath $ProjectDir)) {
    throw "ProjectDir not found: $ProjectDir"
}

if (-not (Test-Path -LiteralPath $ConfigFile)) {
    throw "ConfigFile not found: $ConfigFile"
}

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

try {
    $timeOfDay = [DateTime]::ParseExact($StartTime, "HH:mm", [System.Globalization.CultureInfo]::InvariantCulture)
}
catch {
    throw "StartTime must have HH:mm format, got: $StartTime"
}

$now = Get-Date
$firstRun = Get-Date -Hour $timeOfDay.Hour -Minute $timeOfDay.Minute -Second 0
if ($firstRun -le $now) {
    $firstRun = $firstRun.AddDays(1)
}

$escapedRunner = ('"{0}"' -f $runnerScript)
$escapedPython = ('"{0}"' -f $PythonExe)
$escapedProject = ('"{0}"' -f $ProjectDir)
$escapedConfig = ('"{0}"' -f $ConfigFile)
$escapedMode = ('"{0}"' -f $Mode)
$escapedLogDir = ('"{0}"' -f $LogDir)

$psArgs = "-NoProfile -ExecutionPolicy Bypass -File $escapedRunner -PythonExe $escapedPython -ProjectDir $escapedProject -ConfigFile $escapedConfig -Mode $escapedMode -LogDir $escapedLogDir"

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $psArgs -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -Once -At $firstRun -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "codexSync scheduled sync task" -Force | Out-Null

Write-Host "Task installed: $TaskName"
Write-Host "First run: $($firstRun.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Host "Interval (minutes): $IntervalMinutes"
Write-Host "Mode: $Mode"
Write-Host "To remove task: .\remove-task.ps1 -ConfigPath `"$ConfigPath`""
