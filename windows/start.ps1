# Start ephy-discord. DPAPI blobs in %LOCALAPPDATA%\ephy-discord\secrets
# are decrypted into this process only and are not printed.
param(
    [string]$Config = $env:EPHY_DISCORD_CONFIG
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
if (-not $Config) {
    $Config = Join-Path $repo "configs\ephy-discord.local.toml"
}
if (-not (Test-Path -LiteralPath $Config)) {
    Write-Error "Set EPHY_DISCORD_CONFIG or copy the example to configs\ephy-discord.local.toml"
    exit 2
}

Add-Type -AssemblyName System.Security
$secretDir = Join-Path $env:LOCALAPPDATA "ephy-discord\secrets"
foreach ($name in @("DISCORD_BOT_TOKEN", "EPHY_MCP_BEARER_TOKEN", "EPHY_MCP_OAUTH_SECRET")) {
    $blob = Join-Path $secretDir ($name + ".dpapi")
    if (-not (Test-Path -LiteralPath $blob)) {
        continue
    }
    $protected = [IO.File]::ReadAllBytes($blob)
    $bytes = [Security.Cryptography.ProtectedData]::Unprotect(
        $protected,
        $null,
        [Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    Set-Item -Path ("Env:" + $name) -Value ([Text.Encoding]::UTF8.GetString($bytes))
    [Array]::Clear($bytes, 0, $bytes.Length)
}

$state = Join-Path $env:LOCALAPPDATA "ephy-discord"
New-Item -ItemType Directory -Force -Path $state | Out-Null
$python = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $python) {
    Write-Error "python was not found on PATH"
    exit 2
}
$process = Start-Process -FilePath $python.Source -WorkingDirectory $repo -WindowStyle Hidden -PassThru -ArgumentList @(
    "-m", "ephy_discord", "run", "--config", $Config
)
Set-Content -LiteralPath (Join-Path $state "ephy-discord.pid") -Value $process.Id -Encoding ascii
Write-Output ("started pid=" + $process.Id)
