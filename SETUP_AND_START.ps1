$ErrorActionPreference = 'Stop'
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $env:LOCALAPPDATA 'JoyMetric\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'driver-install-v30.4.42.log'
# v30.4.43 is a launcher-only fix. It intentionally accepts the already-built
# v30.4.42 render driver instead of forcing a needless kernel-driver reinstall.
$driverMarker = Join-Path $env:LOCALAPPDATA 'JoyMetric\virtual-driver\installed-v30442.marker'

function Get-JoyMetricRenderEndpoint {
    try {
        return Get-PnpDevice -Class AudioEndpoint -PresentOnly -ErrorAction SilentlyContinue |
            Where-Object { $_.FriendlyName -match 'JoyMetric' -and $_.InstanceId -match '\{0\.0\.0\.' -and $_.Status -eq 'OK' } |
            Select-Object -First 1
    } catch { return $null }
}

function Test-JoyMetricDriver {
    try {
        $controller = Get-PnpDevice -Class Media -PresentOnly -ErrorAction SilentlyContinue |
            Where-Object { $_.FriendlyName -match '^JoyMetric Virtual Audio Driver$|JoyMetric Virtual Audio' -and $_.Status -eq 'OK' } |
            Select-Object -First 1
        $render = Get-JoyMetricRenderEndpoint
        return [bool]($controller -and $render -and (Test-Path $driverMarker))
    } catch { return $false }
}

if (-not (Test-JoyMetricDriver)) {
    Write-Host 'JoyMetric render/output endpoint is missing or the render-driver marker is absent.' -ForegroundColor Yellow
    Write-Host 'Opening the elevated build/install bootstrap...' -ForegroundColor Yellow
    $installer = Join-Path $ROOT 'INSTALL_JOYMETRIC_DRIVER.ps1'
    $argLine = "-NoProfile -ExecutionPolicy Bypass -File `"$installer`" -BootstrapTools -ForceRebuild -ForceReinstall -LogPath `"$log`""

    # DO NOT use Start-Process -Wait here. On some Windows builds -Wait can remain
    # blocked by descendants of the elevated PowerShell process even after the
    # installer transcript has ended and the audio endpoint is already live.
    $proc = Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argLine -PassThru

    $deadline = (Get-Date).AddMinutes(8)
    $lastMessage = Get-Date
    while ((Get-Date) -lt $deadline) {
        if (Test-JoyMetricDriver) {
            Write-Host 'JoyMetric render endpoint detected; continuing without waiting for the elevated process tree.' -ForegroundColor Green
            break
        }

        if ($proc.HasExited) {
            if ($proc.ExitCode -eq 3010) {
                Write-Host ''
                Write-Host 'REBOOT REQUIRED. Restart Windows once, then run the SAME JoyMetric PowerShell command again.' -ForegroundColor Yellow
                Write-Host "Driver log: $log" -ForegroundColor DarkGray
                return
            }
            if ($proc.ExitCode -ne 0) {
                Write-Host ''
                Write-Host "JoyMetric driver installer exited with code $($proc.ExitCode)." -ForegroundColor Red
                if (Test-Path $log) { Get-Content $log -Tail 100 | Out-Host }
                throw "JoyMetric driver setup failed. Full log: $log"
            }
            # Exit code 0 can race MMDevice enumeration by a moment. Give the
            # Audio Endpoint Builder a short grace period before failing.
            Start-Sleep -Seconds 2
            if (Test-JoyMetricDriver) { break }
        }

        if (((Get-Date) - $lastMessage).TotalSeconds -ge 20) {
            $ep = Get-JoyMetricRenderEndpoint
            if ($ep) {
                Write-Host ("Waiting for install marker; render endpoint already present: " + $ep.FriendlyName) -ForegroundColor DarkGray
            } else {
                Write-Host 'Waiting for JoyMetric render endpoint...' -ForegroundColor DarkGray
            }
            $lastMessage = Get-Date
        }
        Start-Sleep -Milliseconds 500
    }

    if (-not (Test-JoyMetricDriver)) {
        Write-Host ''
        Write-Host 'JoyMetric driver setup did not become ready before the launcher timeout.' -ForegroundColor Red
        if (Test-Path $log) { Get-Content $log -Tail 100 | Out-Host }
        throw "JoyMetric driver setup did not complete. Full log: $log"
    }
}

$renderReady = Get-JoyMetricRenderEndpoint
Write-Host ('JoyMetric Virtual Audio render endpoint: READY' + $(if($renderReady){' · ' + $renderReady.FriendlyName}else{''})) -ForegroundColor Green
& (Join-Path $ROOT 'start.ps1')
