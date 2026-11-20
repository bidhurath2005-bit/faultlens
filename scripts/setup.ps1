# Creates .env from .env.example, replacing every __GENERATE__ with a cryptographically random value.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 [-Force]
param([switch]$Force)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root ".env"
$example = Join-Path $root ".env.example"

if ((Test-Path $envFile) -and -not $Force) {
    Write-Host ".env already exists (use -Force to regenerate; this rotates all local secrets)." -ForegroundColor Yellow
    exit 0
}

function New-Secret([int]$bytes = 24) {
    $buf = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buf)
    return (($buf | ForEach-Object { $_.ToString("x2") }) -join "")
}

$lines = Get-Content $example | ForEach-Object {
    if ($_ -match "__GENERATE__") { $_ -replace "__GENERATE__", (New-Secret) } else { $_ }
}
# UTF-8 without BOM so docker compose parses the first key correctly.
[System.IO.File]::WriteAllLines($envFile, $lines, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "Created .env with freshly generated local secrets." -ForegroundColor Green
$admin = ($lines | Where-Object { $_ -like "BOOTSTRAP_ADMIN_PASSWORD=*" }) -replace "BOOTSTRAP_ADMIN_PASSWORD=", ""
Write-Host "Initial admin login -> username: admin  password: $admin" -ForegroundColor Cyan
Write-Host "(stored only in your local .env file)"
