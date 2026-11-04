# Report the recorded ephy-discord process. A reused PID is not reported as ours.
$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0
$file = Join-Path $env:LOCALAPPDATA "ephy-discord\service.json"
if (-not (Test-Path -LiteralPath $file)) { Write-Output "Running=False"; exit 1 }
$record = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
if ($record.processStartId -notmatch '^windows-ticks:[0-9]{1,20}$') {
    Write-Output "Running=False"
    exit 1
}
$process = Get-Process -Id ([int]$record.pid) -ErrorAction SilentlyContinue
if (-not $process) { Write-Output "Running=False"; exit 1 }
try {
    $actual = "windows-ticks:" + $process.StartTime.ToUniversalTime().Ticks.ToString([Globalization.CultureInfo]::InvariantCulture)
    if ($actual -cne $record.processStartId -or $process.Path -ine [string]$record.executable) {
        Write-Output "Running=False"
        exit 1
    }
    Write-Output ("Running=True; PID=" + $process.Id + "; Port=" + $record.port)
    exit 0
}
finally { $process.Dispose() }
