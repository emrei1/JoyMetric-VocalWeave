$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$CFG = Get-Content (Join-Path $ROOT "config.json") -Raw | ConvertFrom-Json
$CONDA = $CFG.conda_exe

$envJson = & $CONDA env list --json
if ($LASTEXITCODE -ne 0) { throw "Could not read conda environments from $CONDA" }
$envs = $envJson | ConvertFrom-Json
$RO = $envs.envs | Where-Object { (Split-Path $_ -Leaf) -eq $CFG.roformer_env } | Select-Object -First 1
if (-not $RO) { throw "roformer env not found: $($CFG.roformer_env)" }
$PY = Join-Path $RO "python.exe"
$GPUENV = $envs.envs | Where-Object { (Split-Path $_ -Leaf) -eq $CFG.sa3_gpu_env } | Select-Object -First 1
$GPUPY = if ($GPUENV) { Join-Path $GPUENV "python.exe" } else { $null }
if (-not (Test-Path $PY)) { throw "Python was not found: $PY" }

# Persistent JoyMetric locations shared across app versions.
$JOYROOT = Join-Path $env:LOCALAPPDATA "JoyMetric"
$SEMROOT = Join-Path $JOYROOT "models\semantic_clap"
$SEMENV = Join-Path $JOYROOT "envs\semantic_clap_cpu"
$SEMPY = Join-Path $SEMENV "Scripts\python.exe"
$SEMGPUENV = Join-Path $JOYROOT "envs\semantic_clap_gpu"
$SEMGPU_PY = Join-Path $SEMGPUENV "python.exe"
New-Item -ItemType Directory -Force -Path $SEMROOT | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path $SEMENV -Parent) | Out-Null
$env:HF_HOME = $SEMROOT
$env:JOY_SEMANTIC_HOST = [string]$CFG.realtime_model_host
$env:JOY_SEMANTIC_PORT = [string]$CFG.realtime_model_port
if (-not $env:JOY_SEMANTIC_MODEL) { $env:JOY_SEMANTIC_MODEL = "laion/clap-htsat-unfused" }
if (-not $env:JOY_SEMANTIC_DETAIL_MODEL) { $env:JOY_SEMANTIC_DETAIL_MODEL = "laion/larger_clap_music" }
if (-not $env:JOY_SEMANTIC_DETAIL_PLANNER) { $env:JOY_SEMANTIC_DETAIL_PLANNER = "1" }
if (-not $env:JOY_SAMPLE_DECLICK) { $env:JOY_SAMPLE_DECLICK = "0" }
if (-not $env:JOY_DSP_AUTHORITY) { $env:JOY_DSP_AUTHORITY = "1.00" }
if (-not $env:JOY_DJ_DSP_AUTHORITY) { $env:JOY_DJ_DSP_AUTHORITY = "1.00" }
if (-not $env:JOY_DSP_EFFECT_MULT) { $env:JOY_DSP_EFFECT_MULT = "1.0" }
# v30.4.4 continuity defaults: Auto DSP/50D product control is removed.
# Continuous DJ owns only the visible 15 relative features; decisions are
# timestamp-aligned to the delayed source timeline and then slew through the
# native C2-like parameter trajectory.
if (-not $env:JOY_CONTINUITY_LIVE_BAND_FAST) { $env:JOY_CONTINUITY_LIVE_BAND_FAST = "0.042" }
if (-not $env:JOY_CONTINUITY_LIVE_BAND_SLOW) { $env:JOY_CONTINUITY_LIVE_BAND_SLOW = "0.010" }
if (-not $env:JOY_CONTINUITY_DEADBAND_FAST) { $env:JOY_CONTINUITY_DEADBAND_FAST = "0.0028" }
if (-not $env:JOY_CONTINUITY_DEADBAND_SLOW) { $env:JOY_CONTINUITY_DEADBAND_SLOW = "0.0024" }
if (-not $env:JOY_CONTINUITY_LOOKAHEAD_SEC) { $env:JOY_CONTINUITY_LOOKAHEAD_SEC = "2.0" }
if (-not $env:JOY_CONTINUITY_ANALYSIS_CENTER_FRAC) { $env:JOY_CONTINUITY_ANALYSIS_CENTER_FRAC = "0.50" }
if (-not $env:JOY_CONTINUITY_PARAM_PREROLL_SEC) { $env:JOY_CONTINUITY_PARAM_PREROLL_SEC = "0.46" }
if (-not $env:JOY_CONTINUITY_MIN_SCHEDULE_AHEAD_SEC) { $env:JOY_CONTINUITY_MIN_SCHEDULE_AHEAD_SEC = "0.10" }
if (-not $env:JOY_DJ_FEATURE_LIMIT) { $env:JOY_DJ_FEATURE_LIMIT = "0.68" }
if (-not $env:JOY_DJ_FEATURE_STEP) { $env:JOY_DJ_FEATURE_STEP = "0.075" }
if (-not $env:JOY_DJ_FEATURE_DEADBAND) { $env:JOY_DJ_FEATURE_DEADBAND = "0.012" }
if (-not $env:JOY_DJ_FEATURE_SLEW_PER_SEC) { $env:JOY_DJ_FEATURE_SLEW_PER_SEC = "0.48" }
if (-not $env:JOY_DJ_FEATURE_SMOOTH_SEC) { $env:JOY_DJ_FEATURE_SMOOTH_SEC = "1.10" }
if (-not $env:JOY_AGENTIC_AUDIO_FRAMES) { $env:JOY_AGENTIC_AUDIO_FRAMES = "2048" }
if (-not $env:JOY_DJ_FEATURE_PREROLL_SEC) { $env:JOY_DJ_FEATURE_PREROLL_SEC = "0.34" }
$env:JOY_SEMANTIC_DEVICE = "cpu"
$env:JOY_SEMANTIC_ALLOW_CUDA = "0"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"

# v30.4.44 JoyMetric-owned render-endpoint routing. Spotify is moved to the private virtual
# render endpoint; JoyMetric captures that endpoint's own WASAPI loopback and
# returns only processed audio to the selected physical output. SoundVolumeCommandLine is used
# only to set/reset Spotify's per-app Windows output preference.
$TOOLROOT = Join-Path $JOYROOT "tools\svcl"
$SVCL = Join-Path $TOOLROOT "svcl.exe"
New-Item -ItemType Directory -Force -Path $TOOLROOT | Out-Null
if (-not (Test-Path $SVCL)) {
    $svZip = Join-Path $env:TEMP "joymetric_svcl_x64.zip"
    try {
        Write-Host "Preparing Windows per-app output router (one-time download from nirsoft.net)..." -ForegroundColor Yellow
        Invoke-WebRequest -UseBasicParsing -Uri "https://www.nirsoft.net/utils/svcl-x64.zip" -OutFile $svZip
        Expand-Archive -Path $svZip -DestinationPath $TOOLROOT -Force
        Remove-Item $svZip -Force -ErrorAction SilentlyContinue
    } catch {
        Write-Warning "Per-app output router could not be prepared: $($_.Exception.Message)"
        Write-Warning "Automatic app routing is unavailable until the helper can be downloaded."
    }
}
if (Test-Path $SVCL) {
    $env:JOY_SVCL_EXE = $SVCL
    # Crash recovery: if a previous JoyMetric run ended before restoring Spotify,
    # put Spotify back on the current Windows default before the new engine arms.
    try { & $SVCL /SetAppDefault DefaultRenderDevice all spotify.exe *> $null } catch {}
} else {
    Remove-Item Env:JOY_SVCL_EXE -ErrorAction SilentlyContinue
}

# JoyMetric-owned WDM/WaveRT virtual audio driver. Internal endpoints are not
# user choices; LET'S GO routes Spotify to JoyMetric Virtual Input automatically.
# v30.4.44 uses the stock WaveRT speaker endpoint and captures its WASAPI loopback; the paired mic is not used.
$channelPresent = $false
$renderEndpoint = $null
try {
    $renderEndpoint = Get-PnpDevice -Class AudioEndpoint -PresentOnly -ErrorAction SilentlyContinue |
        Where-Object { $_.FriendlyName -match 'JoyMetric' -and $_.InstanceId -match '\{0\.0\.0\.' } |
        Select-Object -First 1
    $channelPresent = [bool]$renderEndpoint
} catch { $channelPresent = $false }
if (-not $channelPresent) {
    throw "JoyMetric render/output endpoint is missing. Run SETUP_AND_START.ps1 so the JoyMetric render driver can replace the old capture-only development driver."
} else {
    Write-Host ("JoyMetric render/output endpoint: READY - " + $renderEndpoint.FriendlyName) -ForegroundColor Green
}


function Invoke-NativeLogged([string]$Exe, [string[]]$ArgList, [string]$LogPath) {
    $oldEap = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $Exe @ArgList 2>&1 | Tee-Object -FilePath $LogPath -Append | Out-Host
        return [int]$LASTEXITCODE
    } catch {
        return 999
    } finally {
        $ErrorActionPreference = $oldEap
    }
}

function Test-PythonImports([string]$PythonExe, [string]$Code) {
    if (-not (Test-Path $PythonExe)) { return $false }
    $oldEap = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $PythonExe -c $Code *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $oldEap
    }
}

# Native Windows realtime route dependency stays in the existing audio env.
$nativeProbe = Test-PythonImports $PY "import numpy, pyaudiowpatch, pedalboard, pycaw, psutil, soundcard, sounddevice"
if (-not $nativeProbe) {
    Write-Host "Preparing exact WASAPI Audio Out + JoyMetric internal-driver routing + C++ Studio DSP core (one-time install)..." -ForegroundColor Yellow
    try {
        & $PY -m pip install --disable-pip-version-check --no-input PyAudioWPatch "pedalboard>=0.9.23,<0.10" pycaw psutil soundcard sounddevice
        if ($LASTEXITCODE -ne 0) { throw "pip exited with code $LASTEXITCODE" }
    } catch {
        Write-Warning "Native realtime/audio-routing dependency could not be installed: $($_.Exception.Message)"
        Write-Warning "JoyMetric will use its built-in fallback feature renderer until the dependency is available."
    }
}

# v31.1 optional Windows media-session bridge. This gives the Agentic DJ real
# Spotify play/pause/next/previous/seek/beatjump tools without touching audio
# capture. Failure is non-fatal: the live DJ mixer still works normally.
$mediaProbe = Test-PythonImports $PY "from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager"
if (-not $mediaProbe) {
    Write-Host "Preparing Spotify media-session controls for Agentic DJ (one-time install)..." -ForegroundColor Yellow
    try {
        & $PY -m pip install --disable-pip-version-check --no-input "winrt-Windows.Media.Control==3.2.1"
        if ($LASTEXITCODE -ne 0) { throw "pip exited with code $LASTEXITCODE" }
    } catch {
        Write-Warning "Spotify transport controls are unavailable: $($_.Exception.Message)"
        Write-Warning "Agentic EQ/filter/FX/loop processing will still work normally."
    }
}

# Semantic AI stays in a clean isolated CPU environment. Continuous-DJ inference is
# out-of-band and never runs inside the native audio callback, so the audio route
# keeps deterministic MMCSS/Pro Audio scheduling.
if (-not (Test-Path $SEMPY)) {
    Write-Host "Creating isolated Semantic AI CPU environment (one-time setup)..." -ForegroundColor Yellow
    $created = $false
    try {
        & $PY -m venv $SEMENV
        $created = (($LASTEXITCODE -eq 0) -and (Test-Path $SEMPY))
    } catch { $created = $false }
    if (-not $created) {
        try {
            if (Test-Path $SEMENV) { Remove-Item $SEMENV -Recurse -Force -ErrorAction SilentlyContinue }
            & $CONDA create -y -p $SEMENV python=3.11 pip
            $created = (($LASTEXITCODE -eq 0) -and (Test-Path $SEMPY))
        } catch { $created = $false }
    }
    if (-not $created) {
        # Conda prefix environments place python.exe at the prefix root rather than Scripts\.
        $altSemPy = Join-Path $SEMENV "python.exe"
        if (Test-Path $altSemPy) { $SEMPY = $altSemPy; $created = $true }
    }
    if (-not $created) {
        Write-Warning "Could not create isolated Semantic AI environment. Realtime DSP will still work normally."
    }
}
if (-not (Test-Path $SEMPY)) {
    $altSemPy = Join-Path $SEMENV "python.exe"
    if (Test-Path $altSemPy) { $SEMPY = $altSemPy }
}

$semanticProbe = Test-PythonImports $SEMPY "import torch, numpy, scipy, pedalboard; from transformers import ClapModel, ClapTextModelWithProjection, AutoFeatureExtractor, AutoTokenizer"
if ((Test-Path $SEMPY) -and (-not $semanticProbe)) {
    Write-Host "Preparing stable CPU Semantic AI packages (one-time setup)..." -ForegroundColor Yellow
    $SETUPLOG = Join-Path $ROOT "semantic_setup.log"
    try {
        $code = Invoke-NativeLogged $SEMPY @("-m","pip","install","--disable-pip-version-check","--no-input","--upgrade","pip","wheel","setuptools") $SETUPLOG
        if ($code -ne 0) { throw "pip bootstrap exited with code $code" }
        $code = Invoke-NativeLogged $SEMPY @("-m","pip","install","--disable-pip-version-check","--no-input","--index-url","https://download.pytorch.org/whl/cpu","torch==2.5.1") $SETUPLOG
        if ($code -ne 0) { throw "CPU torch install exited with code $code" }
        $code = Invoke-NativeLogged $SEMPY @("-m","pip","install","--disable-pip-version-check","--no-input","numpy==1.26.4","scipy==1.13.1","transformers==4.46.3","tokenizers>=0.20,<0.21","safetensors>=0.4.3","huggingface-hub>=0.23","pedalboard>=0.9.23,<0.10") $SETUPLOG
        if ($code -ne 0) { throw "Semantic package install exited with code $code" }
    } catch {
        Write-Warning "Semantic AI package setup is incomplete: $($_.Exception.Message)"
        Write-Warning "JoyMetric will open normally; the Continuous DJ worker retries on later starts."
    }
}

# v30.4.23: GPU restoration removed. No CUDA/A2SB environment, checkpoint,
# worker, model download, or VRAM reservation is used by realtime audio.

# Realtime semantic control deliberately stays on the isolated CPU environment.
# The native audio route gets MMCSS/Pro Audio priority; keeping CLAP off CUDA
# avoids VRAM/device handoffs and GPU scheduling spikes while a song is live.
# Stable Audio may still use the GPU for offline generation as before.
$SEMUSEPY = $SEMPY
if ($SEMUSEPY -and (Test-Path $SEMUSEPY)) {
    Write-Host "Realtime Semantic AI: fast CPU audio critic + text-only detailed prompt planner · no GPU handoff · audio-priority mode." -ForegroundColor Cyan
}

# Stop stale realtime workers/routes from earlier JoyMetric versions.
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ProcessId -ne $PID -and ($_.CommandLine -like "*realtime_model_server.py*" -or $_.CommandLine -like "*realtime_semantic_server.py*") } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

@($CFG.port, $CFG.realtime_model_port, 8768) | ForEach-Object {
    $port = $_
    if ($null -ne $port) {
        Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
    }
}
Start-Sleep -Milliseconds 300

# Start Semantic AI only if its isolated interpreter exists. The worker itself
# retries model/cache/network setup in the background and never exposes a hard
# error state to the realtime UI.
$SEMOUT = Join-Path $ROOT "semantic_ai.log"
$SEMERR = Join-Path $ROOT "semantic_ai_error.log"
if ($SEMUSEPY -and (Test-Path $SEMUSEPY)) {
    try {
        $semProc = Start-Process -FilePath $SEMUSEPY -ArgumentList @((Join-Path $ROOT "realtime_semantic_server.py")) -WorkingDirectory $ROOT -WindowStyle Hidden -RedirectStandardOutput $SEMOUT -RedirectStandardError $SEMERR -PassThru
        try { $semProc.PriorityClass = "BelowNormal" } catch {}
        Write-Host "Semantic AI: CPU critic + detailed text planner starting · realtime audio keeps priority." -ForegroundColor Cyan
    } catch {
        Write-Warning "Semantic AI worker could not start. Manual 15D realtime remains available."
    }
} else {
    Write-Warning "Semantic AI environment is not ready yet. Manual 15D realtime remains available."
}

Write-Host "JoyMetric v31.2.0 Neural Agentic DJ · Spotify raw PCM → CLAP neural action brain → 60 s temporal memory → DJ critic → quantized controller scheduler · crash-safe WASAPI scan · v30.4.47 LowEndIntegrity master preserved." -ForegroundColor Cyan
Write-Host "Opening http://$($CFG.host):$($CFG.port)" -ForegroundColor Green
Start-Process "http://$($CFG.host):$($CFG.port)"
Set-Location $ROOT
& $PY "app.py"
