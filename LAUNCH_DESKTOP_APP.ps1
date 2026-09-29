# JoyMetric VocalWeave - desktop launcher (v31.30.36).
# No separate app is built: this makes the SAME UI (served by app.py, exactly what has been
# opening in the browser) open as a chromeless Edge "app" window instead of a browser tab.
#
# v31.30.36 fixes a real problem from v31.30.35: with no visible feedback while the backend
# boots (30-180 s), repeated clicks each started ANOTHER LAUNCH_SONGMIND.ps1, piling up
# duplicate app.py/GPU-worker processes and causing a crash loop. Now:
#   1. Edge opens INSTANTLY, pointed at a local loading page (desktop_loading.html) - every
#      click shows a window right away, no more "nothing happened" confusion.
#   2. That page's own JS polls the backend and navigates itself once it answers - this
#      script does not block/wait at all, so it returns immediately.
#   3. A short-lived lock file (runtime\desktop_launch.lock) means only the FIRST click
#      actually starts LAUNCH_SONGMIND.ps1; extra clicks while it is booting just open
#      another loading-page window onto the SAME in-flight boot instead of starting a
#      second one.
$ErrorActionPreference = 'Continue'
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Url = 'http://127.0.0.1:8765/'
$LoadingPage = 'file:///' + ((Join-Path $AppDir 'desktop_loading.html') -replace '\\', '/')
$LockPath = Join-Path $AppDir 'runtime\desktop_launch.lock'
$EdgeCandidates = @(
    'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    'C:\Program Files\Microsoft\Edge\Application\msedge.exe'
)
$Edge = $EdgeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

function Test-Backend {
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
        return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 300)
    } catch { return $false }
}

function Test-LaunchInFlight {
    # Time-based, not process-liveness-based: LAUNCH_SONGMIND.ps1 itself returns well before
    # app.py/the GPU workers are actually responsive (it hands off and exits early), so a PID
    # check raced and let a second click re-trigger a duplicate boot. A cooldown window is
    # simple and covers the real (30-90 s) boot time with margin.
    if (-not (Test-Path $LockPath)) { return $false }
    try {
        $lockTime = [double](Get-Content $LockPath -Raw -EA Stop).Trim()
        $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $lockTime
        return ($age -lt 150)
    } catch { return $false }
}

# Start the real backend at most once per boot, regardless of how many times the icon is
# clicked while it is starting.
if ((-not (Test-Backend)) -and (-not (Test-LaunchInFlight))) {
    New-Item -ItemType Directory -Force -Path (Split-Path $LockPath) | Out-Null
    [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() | Out-File -FilePath $LockPath -Encoding ascii -Force
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $AppDir 'LAUNCH_SONGMIND.ps1')) `
        -WorkingDirectory $AppDir -WindowStyle Hidden
}

if (-not $Edge) {
    $logDir = Join-Path $env:LOCALAPPDATA 'JoyMetric\logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    "Microsoft Edge not found; open $Url manually." | Out-File -FilePath (Join-Path $logDir 'desktop_launch.log') -Append -Encoding utf8
    exit 1
}

# A dedicated --user-data-dir guarantees a real chromeless app window even if the user's
# normal Edge is already open (otherwise --app can just reuse that running instance).
$ProfileDir = Join-Path $env:LOCALAPPDATA 'JoyMetric\EdgeAppProfile'
New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null
Start-Process -FilePath $Edge -ArgumentList @(
    "--app=$LoadingPage",
    '--window-size=1480,920',
    "--user-data-dir=$ProfileDir",
    '--no-first-run',
    '--no-default-browser-check'
)
