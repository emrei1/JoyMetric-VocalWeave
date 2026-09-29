$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$CFG = Get-Content (Join-Path $ROOT "config.json") -Raw | ConvertFrom-Json
$URL = "http://$($CFG.gpu_worker_host):$($CFG.gpu_worker_port)/health"
Write-Host "Persistent SA3 engine:" -ForegroundColor Cyan
Invoke-RestMethod $URL | Format-List
Write-Host "`nRecent engine log:" -ForegroundColor Cyan
Get-Content (Join-Path $ROOT "runtime\sa3_engine.log") -Tail 30 -ErrorAction SilentlyContinue
