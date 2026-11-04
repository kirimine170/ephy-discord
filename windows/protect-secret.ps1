# Merge one secret into %LOCALAPPDATA%\ephy-discord\secrets.dpapi.
# Current-user DPAPI only. The value is read from the console, never from argv.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("DISCORD_BOT_TOKEN", "EPHY_MCP_BEARER_TOKEN")]
    [string]$Name
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0
$directory = Join-Path $env:LOCALAPPDATA "ephy-discord"
New-Item -ItemType Directory -Force -Path $directory | Out-Null
$path = Join-Path $directory "secrets.dpapi"
$values = @{}
if (Test-Path -LiteralPath $path) {
    $secure = ConvertTo-SecureString -String ([IO.File]::ReadAllText($path))
    try {
        $plain = [Net.NetworkCredential]::new("", $secure).Password
        $obj = $plain | ConvertFrom-Json
        foreach ($property in $obj.PSObject.Properties) {
            if ($property.Name -notin @("DISCORD_BOT_TOKEN", "EPHY_MCP_BEARER_TOKEN")) {
                throw "SECRET_FILE_INVALID"
            }
            $values[$property.Name] = [string]$property.Value
        }
    }
    finally {
        $secure.Dispose()
        $plain = $null
    }
}
$entered = Read-Host -AsSecureString -Prompt $Name
try {
    $values[$Name] = [Net.NetworkCredential]::new("", $entered).Password
    $encoded = ConvertTo-SecureString -String ($values | ConvertTo-Json -Compress) -AsPlainText -Force | ConvertFrom-SecureString
    [IO.File]::WriteAllText($path, $encoded, [Text.UTF8Encoding]::new($false))
}
finally {
    $entered.Dispose()
    $values.Clear()
}
Write-Output ("protected " + $Name)
