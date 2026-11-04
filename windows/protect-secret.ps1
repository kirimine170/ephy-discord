# Encrypt one ephy-discord secret with Windows DPAPI (CurrentUser).
# The value is read from the console, never from an argument or a file in git.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("DISCORD_BOT_TOKEN", "EPHY_MCP_BEARER_TOKEN", "EPHY_MCP_OAUTH_SECRET")]
    [string]$Name
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Security
$directory = Join-Path $env:LOCALAPPDATA "ephy-discord\secrets"
New-Item -ItemType Directory -Force -Path $directory | Out-Null
$secure = Read-Host -AsSecureString -Prompt $Name
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    $bytes = [Text.Encoding]::UTF8.GetBytes($plain)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    $plain = $null
}
$protected = [Security.Cryptography.ProtectedData]::Protect(
    $bytes,
    $null,
    [Security.Cryptography.DataProtectionScope]::CurrentUser
)
[Array]::Clear($bytes, 0, $bytes.Length)
$destination = Join-Path $directory ($Name + ".dpapi")
[IO.File]::WriteAllBytes($destination, $protected)
Write-Output ("protected " + $Name)
