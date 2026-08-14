# Checks reachability and login to the FortiGate.
# Reads host/user/password from ..\.env if present.

$ErrorActionPreference = "Stop"

$envFile = Join-Path $PSScriptRoot "..\.env"
$cfg = @{ FGT_HOST = "172.21.0.1"; FGT_USER = "admin"; FGT_PASSWORD = "" }
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#=]+)=(.*)$') { $cfg[$matches[1].Trim()] = $matches[2].Trim() }
    }
}

$fgtHost = $cfg.FGT_HOST
Write-Host "Testing $fgtHost:443 ..." -ForegroundColor Cyan
$tcp = Test-NetConnection -ComputerName $fgtHost -Port 443 -WarningAction SilentlyContinue
if (-not $tcp.TcpTestSucceeded) { Write-Host "UNREACHABLE" -ForegroundColor Red; exit 1 }
Write-Host "Reachable." -ForegroundColor Green

if ([string]::IsNullOrWhiteSpace($cfg.FGT_PASSWORD)) {
    Write-Host "No password set in .env - skipping login test." -ForegroundColor Yellow
    exit 0
}

Write-Host "Testing login as $($cfg.FGT_USER) ..." -ForegroundColor Cyan
$body = "username=$($cfg.FGT_USER)&secretkey=$($cfg.FGT_PASSWORD)&ajax=1"
$resp = curl.exe -sk -c "$env:TEMP\fgt_cookies.txt" `
    -d $body "https://$fgtHost/logincheck"
if ($resp -match '^\s*1') {
    Write-Host "Login OK (credentials accepted)." -ForegroundColor Green
} else {
    Write-Host "Login FAILED: $resp" -ForegroundColor Red
    exit 1
}
