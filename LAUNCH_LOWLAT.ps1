# v31.30.15 LOW-LATENCY profile: 4 s weave windows, 10 s render lag (the normal launcher: 16 s windows, 48 s lag).  Same models,
# measured 1.3 s of work per 4 s window on AC; not for battery.  Seams are close to the record's own but not as smooth as 16 s.
# JoyMetric v31.16 SongMind launcher (detached, duplicate-safe).
#   1. stop any previous JoyMetric app / workers
#   2. start the Semantic AI worker (isolated CPU env) and WAIT for /health
#   3. only then start app.py  (prevents the app from spawning a second worker)
#   4. watchdog: kill any duplicate realtime_semantic_server.py not owning :8767
param([string]$AppDir = (Split-Path -Parent $MyInvocation.MyCommand.Path))
$ErrorActionPreference = 'Continue'
$joy = Join-Path $env:LOCALAPPDATA 'JoyMetric'
$logdir = Join-Path $joy 'logs'
New-Item -ItemType Directory -Force -Path $logdir | Out-Null

# v31.30.11: on battery Windows parks cores and caps the GPU (P4, 20 W): separations took 10 s instead of 2.7 s and the
# audio thread woke late -> dropouts.  Best-performance power mode + CPU min state 100 % + no core parking + no battery-saver
# auto-on, for AC and DC alike (the user wants the DJ to work unplugged; this drains the battery faster while it runs).
try {
    powercfg /overlaysetactive OVERLAY_SCHEME_MAX | Out-Null
    powercfg /setdcvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMIN 100 | Out-Null; powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMIN 100 | Out-Null
    powercfg /setdcvalueindex SCHEME_CURRENT SUB_PROCESSOR CPMINCORES 100 | Out-Null; powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR CPMINCORES 100 | Out-Null
    powercfg /setdcvalueindex SCHEME_CURRENT SUB_ENERGYSAVER ESBATTTHRESHOLD 0 | Out-Null
    powercfg /setactive SCHEME_CURRENT | Out-Null
    Write-Output 'power: best performance, cpu min 100 %, core parking off, battery saver auto-on disabled'
} catch { Write-Output ('power settings failed: ' + $_.Exception.Message) }
# v31.30.9: kill the app FIRST, by its port and by name (its command line is just 'python.exe app.py' - the old folder
# filter never matched, so the workers died first and the still-running app re-spawned a Stable Audio worker within a
# second: the source of every duplicate worker), and WAIT until it is gone before touching the workers
$appOwners = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
$appProcs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessId -ne $PID -and $_.Name -like 'python*.exe' -and ($_.CommandLine -like '* app.py*' -or $appOwners -contains $_.ProcessId) })
$appProcs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue }
for ($w = 0; $w -lt 20; $w++) {
    $alive = @($appProcs | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
    if ($alive.Count -eq 0) { break }
    Start-Sleep -Milliseconds 500
}
# v31.30.8: retire every watchdog of earlier launches FIRST (they respawn their own AMT workers and raise SA3 priority)
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessId -ne $PID -and $_.Name -like 'powershell*.exe' -and $_.CommandLine -like '*worker_watchdog.ps1*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue }
# only python processes: the command-line match must never hit a shell whose
# command line merely mentions the script (e.g. the shell that launched us)
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessId -ne $PID -and $_.Name -like 'python*.exe' -and ($_.CommandLine -like '*JoyMetric_Workstation_UI_v31_*app.py*' -or
    $_.CommandLine -like '*realtime_semantic_server.py*' -or $_.CommandLine -like '*sa3_persistent_server.py*' -or $_.CommandLine -like '*amt_persistent_server.py*' -or $_.CommandLine -like '*sep_persistent_server.py*')
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue }
# v31.30.7: wait for the GPU workers to be gone (a terminated CUDA process can linger for seconds while the driver
# releases its memory; a new worker started meanwhile shares the GPU with the ghost -> paging, dropouts)
for ($w = 0; $w -lt 30; $w++) {
    $left = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'python*.exe' -and ($_.CommandLine -like '*sa3_persistent_server.py*' -or $_.CommandLine -like '*sep_persistent_server.py*' -or $_.CommandLine -like '*amt_persistent_server.py*') })
    if ($left.Count -eq 0) { break }
    $left | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
}
@(8765, 8766, 8767, 8768, 8769, 8770) | ForEach-Object {
    Get-NetTCPConnection -LocalPort $_ -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique |
        ForEach-Object { if ($_ -ne $PID) { Stop-Process -Id $_ -Force -Confirm:$false -ErrorAction SilentlyContinue } }
}
Start-Sleep -Milliseconds 900

$env:HF_HOME = Join-Path $joy 'models\semantic_clap'
$env:JOY_SEMANTIC_HOST = '127.0.0.1'; $env:JOY_SEMANTIC_PORT = '8767'
$env:JOY_SEMANTIC_MODEL = 'laion/clap-htsat-unfused'; $env:JOY_SEMANTIC_DETAIL_MODEL = 'laion/larger_clap_music'
$env:JOY_SEMANTIC_DETAIL_PLANNER = '1'; $env:JOY_SAMPLE_DECLICK = '1'
$env:JOY_DSP_AUTHORITY = '1.00'; $env:JOY_DJ_DSP_AUTHORITY = '1.00'; $env:JOY_DSP_EFFECT_MULT = '1.0'
$env:JOY_CONTINUITY_LIVE_BAND_FAST = '0.042'; $env:JOY_CONTINUITY_LIVE_BAND_SLOW = '0.010'
$env:JOY_CONTINUITY_DEADBAND_FAST = '0.0028'; $env:JOY_CONTINUITY_DEADBAND_SLOW = '0.0024'
$env:JOY_CONTINUITY_LOOKAHEAD_SEC = '2.0'; $env:JOY_CONTINUITY_ANALYSIS_CENTER_FRAC = '0.50'
$env:JOY_CONTINUITY_PARAM_PREROLL_SEC = '0.46'; $env:JOY_CONTINUITY_MIN_SCHEDULE_AHEAD_SEC = '0.10'
$env:JOY_DJ_FEATURE_LIMIT = '0.68'; $env:JOY_DJ_FEATURE_STEP = '0.075'; $env:JOY_DJ_FEATURE_DEADBAND = '0.012'
$env:JOY_DJ_FEATURE_SLEW_PER_SEC = '0.48'; $env:JOY_DJ_FEATURE_SMOOTH_SEC = '1.10'; $env:JOY_DJ_FEATURE_PREROLL_SEC = '0.34'
$env:JOY_SA3_MEM_FRACTION = '0.70'; $env:JOY_GEN_AI = '0'; $env:JOY_AGENTIC_AUDIO_FRAMES = '2048'; $env:JOY_BRIDGE_BLOCKS = '14'; $env:OMP_NUM_THREADS = '4'; $env:MKL_NUM_THREADS = '4'; $env:JOY_AGENTIC_LOOKAHEAD_SEC = '10'; $env:JOY_WEAVE_PROFILE = 'lowlat'; $env:JOY_VOCAL_WEAVE = '1'; $env:JOY_RUST_DSP = '1'
$env:JOY_SEMANTIC_DEVICE = 'cpu'; $env:JOY_SEMANTIC_ALLOW_CUDA = '0'; $env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
$svcl = Join-Path $joy 'tools\svcl\svcl.exe'
if (Test-Path $svcl) { $env:JOY_SVCL_EXE = $svcl; try { & $svcl /SetAppDefault DefaultRenderDevice all spotify.exe *> $null } catch {} }

$sempy = Join-Path $joy 'envs\semantic_clap_cpu\Scripts\python.exe'
if (-not (Test-Path $sempy)) { $sempy = Join-Path $joy 'envs\semantic_clap_cpu\python.exe' }
$sp = Start-Process -FilePath $sempy -ArgumentList @((Join-Path $AppDir 'realtime_semantic_server.py')) -WorkingDirectory $AppDir -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $AppDir 'semantic_ai.log') -RedirectStandardError (Join-Path $AppDir 'semantic_ai_error.log') -PassThru
try { $sp.PriorityClass = 'BelowNormal' } catch {}
$semok = $false
for ($i = 0; $i -lt 60; $i++) {
    try { $null = Invoke-RestMethod -Uri 'http://127.0.0.1:8767/health' -TimeoutSec 3; $semok = $true; break } catch { Start-Sleep -Seconds 2 }
}
Write-Output ("semantic worker pid={0} health={1}" -f $sp.Id, $(if ($semok) { 'OK' } else { 'warming (app starts anyway)' }))

# v31.25: the generative composer (Anticipatory Music Transformer, GPU) - waits for /health like the semantic worker
# v31.30.10: the AMT / SongMind worker is NOT started when the generative composer is off (JOY_GEN_AI=0): with the vocal weave
# its 0.9 GB pushed SA3 (4.7 GB) + separation (2 GB peak) over the 8 GB card -> paging, 10 s separations, 100 % GPU, dropouts
if ($env:JOY_GEN_AI -ne '0') {
    $amtpy = 'C:\Users\inceo\miniconda3\envs\stableaudio3_gpu\python.exe'
    $env:HF_HOME = Join-Path $joy 'models\amt'; $env:JOY_AMT_PORT = '8769'
    $amt = Start-Process -FilePath $amtpy -ArgumentList @((Join-Path $AppDir 'workers\amt_persistent_server.py'), '--port', '8769') -WorkingDirectory $AppDir -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logdir 'amt-worker-out.log') -RedirectStandardError (Join-Path $logdir 'amt-worker-err.log') -PassThru
    try { $amt.PriorityClass = 'BelowNormal' } catch {}     # v31.30.4: GPU workers must not preempt the audio process
    $amtok = $false
    for ($i = 0; $i -lt 40; $i++) {
        try { $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8769/health' -TimeoutSec 3; if ($h.state -eq 'ready') { $amtok = $true; break } } catch {}
        Start-Sleep -Seconds 2
    }
    Write-Output ("amt worker pid={0} ready={1}" -f $amt.Id, $amtok)
} else { Write-Output 'amt worker skipped (JOY_GEN_AI=0)' }

# v31.28: the persistent vocal separator (Mel-Band RoFormer, the Home tab's model) for VocalWeave
$seppy = 'C:\Users\inceo\miniconda3\envs\roformer_sep\python.exe'
$sep = Start-Process -FilePath $seppy -ArgumentList @((Join-Path $AppDir 'workers\sep_persistent_server.py'), '--port', '8770') -WorkingDirectory $AppDir -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logdir 'sep-worker-out.log') -RedirectStandardError (Join-Path $logdir 'sep-worker-err.log') -PassThru
try { $sep.PriorityClass = 'BelowNormal' } catch {}
$sepok = $false
for ($i = 0; $i -lt 40; $i++) {
    try { $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8770/health' -TimeoutSec 3; if ($h.state -eq 'ready') { $sepok = $true; break } } catch {}
    Start-Sleep -Seconds 2
}
Write-Output ("sep worker pid={0} ready={1}" -f $sep.Id, $sepok)
$env:HF_HOME = Join-Path $joy 'models\semantic_clap'
$py = 'C:\Users\inceo\miniconda3\envs\roformer_sep\python.exe'
$ap = Start-Process -FilePath $py -ArgumentList 'app.py' -WorkingDirectory $AppDir -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logdir 'songmind-app-out.log') -RedirectStandardError (Join-Path $logdir 'songmind-app-err.log') -PassThru
Write-Output ("app pid={0}" -f $ap.Id)

# v31.18: persistent semantic-worker watchdog (restart on death, kill on >12 GB commit, kill duplicates)
$wd = Join-Path $AppDir 'worker_watchdog.ps1'
if (Test-Path $wd) {
    $wp = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $wd, '-AppDir', $AppDir) -WorkingDirectory $AppDir -WindowStyle Hidden -PassThru
    Write-Output ("worker watchdog pid={0}" -f $wp.Id)
}

# duplicate-worker watchdog (one pass after startup)
Start-Sleep -Seconds 25
# The venv 'Scripts\python.exe' is a launcher stub: the process that actually
# owns :8767 is its CHILD.  Protect the owner AND its whole ancestor chain;
# only kill workers that are neither (an app-spawned second worker).
$owner = (Get-NetTCPConnection -LocalPort 8767 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess)
if ($owner) {
    $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
    $ppid = @{}; foreach ($q in $procs) { $ppid[[int]$q.ProcessId] = [int]$q.ParentProcessId }
    $keep = @([int]$owner); $cur = [int]$owner
    for ($d = 0; $d -lt 6; $d++) { if (-not $ppid.ContainsKey($cur)) { break }; $cur = $ppid[$cur]; if ($cur -le 4) { break }; $keep += $cur }
    $procs | Where-Object {
        $_.Name -like 'python*.exe' -and $_.CommandLine -like '*realtime_semantic_server.py*' -and ($keep -notcontains [int]$_.ProcessId)
    } | ForEach-Object { Write-Output ("watchdog: stopping duplicate semantic worker pid={0}" -f $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue }
    Write-Output ("watchdog: :8767 owner pid={0} (protected chain: {1})" -f $owner, ($keep -join ','))
} else {
    Write-Output 'watchdog: no :8767 listener found - nothing killed (worker may still be warming)'
}
Write-Output 'launch complete'

# v31.22.2: the Stable Audio worker is mostly GPU, but its CPU-side stages lose to the CLAP critic
# under a live session (synth parts took 10-19 s instead of 1.5 s): give it scheduling priority
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'python*.exe' -and $_.CommandLine -like '*sa3_persistent_server.py*' } |
    ForEach-Object { try { (Get-Process -Id $_.ProcessId).PriorityClass = 'BelowNormal'; Write-Output ('sa3 worker pid={0} -> BelowNormal' -f $_.ProcessId) } catch {} }
