$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$CFG = Get-Content (Join-Path $ROOT "config.json") -Raw | ConvertFrom-Json
$CONDA = $CFG.conda_exe
$envs = (& $CONDA env list --json) | ConvertFrom-Json
$GPUENV = $envs.envs | Where-Object { (Split-Path $_ -Leaf) -eq $CFG.sa3_gpu_env } | Select-Object -First 1
if (-not $GPUENV) { throw "GPU env not found: $($CFG.sa3_gpu_env)" }
$PY = Join-Path $GPUENV "python.exe"
& $PY (Join-Path $ROOT "workers\sa3_gpu_probe.py")
if ($LASTEXITCODE -ne 0) { throw "CUDA backend probe failed." }
