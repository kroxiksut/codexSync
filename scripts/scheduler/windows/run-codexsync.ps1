param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,
    [Parameter(Mandatory = $true)]
    [string]$ProjectDir,
    [Parameter(Mandatory = $true)]
    [string]$ConfigFile,
    [Parameter(Mandatory = $true)]
    [ValidateSet("dry-run", "apply")]
    [string]$Mode,
    [Parameter(Mandatory = $true)]
    [string]$LogDir
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$LogFile = Join-Path $LogDir "codexsync-task.log"

$cmdArgs = @("-m", "codexsync", "-c", $ConfigFile, "sync")
if ($Mode -eq "apply") {
    $cmdArgs += "--apply"
}
else {
    $cmdArgs += "--dry-run"
}

Push-Location $ProjectDir
try {
    Add-Content -Path $LogFile -Value ("[{0}] Starting task mode={1}" -f (Get-Date -Format "s"), $Mode)
    # Under "Stop" the first stderr line of a native program is a terminating
    # NativeCommandError that ends the run mid-write; take stderr as text.
    $ErrorActionPreference = "Continue"
    & $PythonExe @cmdArgs 2>&1 | ForEach-Object { "$_" } | Out-File -FilePath $LogFile -Append -Encoding utf8
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    Add-Content -Path $LogFile -Value ("[{0}] Finished task exit_code={1}" -f (Get-Date -Format "s"), $exitCode)
    exit $exitCode
}
finally {
    Pop-Location
}
