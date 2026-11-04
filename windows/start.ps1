# Decrypt DPAPI secrets into this process, start ephy-discord, then restore the parent environment.
param([string]$Config = $env:EPHY_DISCORD_CONFIG)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0
$repo = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
if (-not $Config) { $Config = Join-Path $repo "configs\ephy-discord.local.toml" }
if (-not (Test-Path -LiteralPath $Config)) {
    Write-Error "Set EPHY_DISCORD_CONFIG or create configs\ephy-discord.local.toml"
    exit 2
}
$state = Join-Path $env:LOCALAPPDATA "ephy-discord"
New-Item -ItemType Directory -Force -Path $state | Out-Null
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { Write-Error "python was not found on PATH"; exit 2 }
$names = @("DISCORD_BOT_TOKEN", "EPHY_MCP_BEARER_TOKEN")
$previous = @{}
$plain = $null
$secure = $null
try {
    foreach ($name in $names) {
        $previous[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    }
    $secretPath = Join-Path $state "secrets.dpapi"
    if (Test-Path -LiteralPath $secretPath) {
        $secure = ConvertTo-SecureString -String ([IO.File]::ReadAllText($secretPath))
        $plain = [Net.NetworkCredential]::new("", $secure).Password
        $obj = $plain | ConvertFrom-Json
        foreach ($property in $obj.PSObject.Properties) {
            if ($names -notcontains $property.Name) { throw "SECRET_FILE_INVALID" }
            [Environment]::SetEnvironmentVariable($property.Name, [string]$property.Value, "Process")
        }
    }
    $info = & $python.Source -c "import sys; from ephy_discord.config import load_config; c=load_config(sys.argv[1]); print('1' if c.mcp.enabled else '0'); print(c.mcp.port)" $Config
    $enabled, $port = @($info -split "`r?`n" | Where-Object { $_ -ne "" })
    if ($port -eq "18640") { throw "PORT_COLLIDES_WITH_ORCHESTRATOR" }
    $process = Start-Process -FilePath $python.Source -WorkingDirectory $repo -WindowStyle Hidden -PassThru -ArgumentList @(
        "-m", "ephy_discord", "run", "--config", $Config
    )
    $identity = "windows-ticks:" + $process.StartTime.ToUniversalTime().Ticks.ToString([Globalization.CultureInfo]::InvariantCulture)
    $record = @{
        pid = $process.Id
        processStartId = $identity
        executable = $python.Source
        port = [int]$port
    }
    [IO.File]::WriteAllText((Join-Path $state "service.json"), ($record | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
    if ($enabled -eq "1") {
        $healthy = $false
        for ($i = 0; $i -lt 50; $i++) {
            Start-Sleep -Milliseconds 100
            $process.Refresh()
            if ($process.HasExited) { throw "SERVER_START_FAILED" }
            try {
                $health = Invoke-RestMethod -Uri ("http://127.0.0.1:" + $port + "/health") -TimeoutSec 1
                if ($health.ok -and -not $health.live_connectors) { $healthy = $true; break }
            }
            catch {}
        }
        if (-not $healthy) { throw "START_HEALTH_TIMEOUT" }
    }
    Write-Output ("Started=True; Bind=127.0.0.1; Port=" + $port)
}
finally {
    if ($secure) { $secure.Dispose() }
    $plain = $null
    foreach ($name in $names) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name], "Process")
    }
}
