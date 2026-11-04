# Stop only the recorded ephy-discord process. A reused PID is left alone.
$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0
$state = Join-Path $env:LOCALAPPDATA "ephy-discord"
$file = Join-Path $state "service.json"
if (-not (Test-Path -LiteralPath $file)) { Write-Output "Running=False"; exit 0 }
$record = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
if ($record.processStartId -notmatch '^windows-ticks:[0-9]{1,20}$') {
    Write-Output "Running=False"
    exit 0
}
$process = Get-Process -Id ([int]$record.pid) -ErrorAction SilentlyContinue
if (-not $process) { Write-Output "Running=False"; exit 0 }
try {
    $actual = "windows-ticks:" + $process.StartTime.ToUniversalTime().Ticks.ToString([Globalization.CultureInfo]::InvariantCulture)
    $samePath = $process.Path -ieq [string]$record.executable
    if ($actual -cne $record.processStartId -or -not $samePath) {
        Write-Output "Running=False"
        exit 0
    }
    $process.Kill()
    [void]$process.WaitForExit(10000)
    Write-Output "Stopped=True"
}
finally { $process.Dispose() }
