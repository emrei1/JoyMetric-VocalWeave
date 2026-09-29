# JoyMetric v31.18 semantic-worker watchdog (started detached by LAUNCH_SONGMIND.ps1).
#   every 15 s while the app (:8765) is alive:
#     - no :8767 listener for two checks   -> start a fresh semantic worker
#     - the :8767 owner's commit > 12 GB    -> kill it (the worker also self-exits at 9 GB)
#     - duplicate worker processes          -> kill them (owner + ancestor chain protected)
#   exits 90 s after the app disappears.
param([string]$AppDir = (Split-Path -Parent $MyInvocation.MyCommand.Path))
$ErrorActionPreference = 'Continue'
$joy = Join-Path $env:LOCALAPPDATA 'JoyMetric'
$logdir = Join-Path $joy 'logs'
New-Item -ItemType Directory -Force -Path $logdir | Out-Null
$log = Join-Path $logdir 'worker-watchdog.log'
function Log($m) { Add-Content -Path $log -Value ("{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) }
$sempy = Join-Path $joy 'envs\semantic_clap_cpu\Scripts\python.exe'
if (-not (Test-Path $sempy)) { $sempy = Join-Path $joy 'envs\semantic_clap_cpu\python.exe' }
$missing = 0; $appGone = 0
Log ("watchdog start appdir={0}" -f $AppDir)
# v31.30.8: one watchdog at a time - a newer launcher writes its watchdog pid here; an old watchdog that sees another pid exits.
# (20+ watchdogs from earlier deploys had kept respawning their own AMT workers and raising SA3 to AboveNormal: GPU full, dropouts.)
$markerDir = Join-Path $env:LOCALAPPDATA 'JoyMetric'; New-Item -ItemType Directory -Force -Path $markerDir | Out-Null
$marker = Join-Path $markerDir 'watchdog.pid'; Set-Content -Path $marker -Value ([string]$PID) -Encoding ascii
while ($true) {
    Start-Sleep -Seconds 15
    try { if ((Get-Content -Path $marker -ErrorAction Stop | Select-Object -First 1).Trim() -ne [string]$PID) { Log 'superseded by a newer watchdog - exiting'; break } } catch {}
    $app = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $app) { $appGone++; if ($appGone -ge 6) { Log 'app gone - watchdog exit'; break }; continue } else { $appGone = 0 }
    $owner = (Get-NetTCPConnection -LocalPort 8767 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess)
    $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
    # keep the Stable Audio worker above the CPU critic (synth parts must come back in seconds)
    $procs | Where-Object { $_.Name -like 'python*.exe' -and $_.CommandLine -like '*sa3_persistent_server.py*' } |
        ForEach-Object { try { $pp = Get-Process -Id $_.ProcessId -ErrorAction Stop; if ($pp.PriorityClass -ne 'BelowNormal') { $pp.PriorityClass = 'BelowNormal'; Log ('sa3 worker pid={0} -> BelowNormal' -f $_.ProcessId) } } catch {} }
    # v31.25: keep the generative composer alive (restart when :8769 has no listener)
    $amtOwner = (Get-NetTCPConnection -LocalPort 8769 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess)
    if ((-not $amtOwner) -and ($env:JOY_GEN_AI -ne '0')) {
        $script:amtMissing = [int]$script:amtMissing + 1
        if ($script:amtMissing -ge 2) {
            $script:amtMissing = 0
            Log 'no :8769 listener - starting the AMT composer'
            try {
                $env:HF_HOME = Join-Path $joy 'models\amt'
                $ap2 = Start-Process -FilePath 'C:\Users\inceo\miniconda3\envs\stableaudio3_gpu\python.exe' -ArgumentList @((Join-Path $AppDir 'workers\amt_persistent_server.py'), '--port', '8769') -WorkingDirectory $AppDir -WindowStyle Hidden -PassThru
                try { $ap2.PriorityClass = 'BelowNormal' } catch {}
                Log ('started amt pid=' + $ap2.Id)
            } catch { Log ('amt start failed: ' + $_.Exception.Message) }
        }
    } else { $script:amtMissing = 0 }
    if ($owner) {
        $missing = 0
        $ppid = @{}; $commit = @{}
        foreach ($q in $procs) { $ppid[[int]$q.ProcessId] = [int]$q.ParentProcessId; $commit[[int]$q.ProcessId] = [double]$q.PageFileUsage }
        $keep = @([int]$owner); $cur = [int]$owner
        for ($d = 0; $d -lt 6; $d++) { if (-not $ppid.ContainsKey($cur)) { break }; $cur = $ppid[$cur]; if ($cur -le 4) { break }; $keep += $cur }
        $mb = 0; if ($commit.ContainsKey([int]$owner)) { $mb = $commit[[int]$owner] / 1MB }
        if ($mb -gt 12288) {
            Log ("owner pid={0} commit={1:N0} MB > 12 GB - killing for a clean restart" -f $owner, $mb)
            Stop-Process -Id $owner -Force -Confirm:$false -ErrorAction SilentlyContinue
            continue
        }
        $procs | Where-Object { $_.Name -like 'python*.exe' -and $_.CommandLine -like '*realtime_semantic_server.py*' -and ($keep -notcontains [int]$_.ProcessId) } |
            ForEach-Object { Log ("duplicate worker pid={0} killed" -f $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue }
    } else {
        $missing++
        if ($missing -ge 2) {
            $missing = 0
            Log 'no :8767 listener - starting a fresh semantic worker'
            try {
                $sp = Start-Process -FilePath $sempy -ArgumentList @((Join-Path $AppDir 'realtime_semantic_server.py')) -WorkingDirectory $AppDir -WindowStyle Hidden `
                    -RedirectStandardOutput (Join-Path $AppDir 'semantic_ai.log') -RedirectStandardError (Join-Path $AppDir 'semantic_ai_error.log') -PassThru
                try { $sp.PriorityClass = 'BelowNormal' } catch {}
                Log ("started worker pid={0}" -f $sp.Id)
            } catch { Log ("start failed: {0}" -f $_.Exception.Message) }
        }
    }
}
