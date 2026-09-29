$ErrorActionPreference='Continue'
Write-Host '=== JoyMetric Driver Diagnostic v30.4.42 ===' -ForegroundColor Cyan
try { Write-Host ('Secure Boot: ' + [bool](Confirm-SecureBootUEFI)) } catch { Write-Host ('Secure Boot: unavailable (' + $_.Exception.Message + ')') }
try { (& bcdedit /enum '{current}' | Select-String -Pattern 'testsigning').Line | Write-Host } catch {}
Write-Host ''
Write-Host 'Media controller:' -ForegroundColor Yellow
Get-PnpDevice -Class Media -PresentOnly -ErrorAction SilentlyContinue |
    Where-Object {$_.FriendlyName -match 'JoyMetric'} |
    Select-Object FriendlyName,Status,InstanceId | Format-Table -AutoSize
Write-Host 'Audio endpoints:' -ForegroundColor Yellow
$eps=Get-PnpDevice -Class AudioEndpoint -PresentOnly -ErrorAction SilentlyContinue |
    Where-Object {$_.FriendlyName -match 'JoyMetric'}
$eps | Select-Object @{N='Direction';E={if($_.InstanceId -match '\{0\.0\.0\.'){ 'RENDER/OUTPUT' }elseif($_.InstanceId -match '\{0\.0\.1\.'){ 'CAPTURE/INPUT' }else{'UNKNOWN'}}},FriendlyName,Status,InstanceId | Format-Table -AutoSize
$render=$eps | Where-Object {$_.InstanceId -match '\{0\.0\.0\.'}
if($render){Write-Host 'RENDER ENDPOINT: PASS' -ForegroundColor Green}else{Write-Host 'RENDER ENDPOINT: MISSING' -ForegroundColor Red}
$log=Join-Path $env:LOCALAPPDATA 'JoyMetric\logs\driver-install-v30.4.42.log'
Write-Host ('Installer log: '+$log)
