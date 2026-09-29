$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$CFG = Get-Content (Join-Path $ROOT "config.json") -Raw | ConvertFrom-Json
$CONDA = $CFG.conda_exe

if (-not (Test-Path $CONDA)) { throw "conda.exe not found: $CONDA" }

$envs = (& $CONDA env list --json) | ConvertFrom-Json
$RO = $envs.envs | Where-Object { (Split-Path $_ -Leaf) -eq $CFG.roformer_env } | Select-Object -First 1
if (-not $RO) { throw "roformer env not found: $($CFG.roformer_env)" }
$PY = Join-Path $RO "python.exe"

Write-Host "Installing/confirming web dependencies in $($CFG.roformer_env)..." -ForegroundColor Cyan
& $PY -m pip install --upgrade flask werkzeug imageio-ffmpeg numpy soundfile scipy PyAudioWPatch "pedalboard>=0.9.23,<0.10"
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
Write-Host "App dependencies ready." -ForegroundColor Green
