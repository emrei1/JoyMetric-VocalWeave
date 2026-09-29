# JoyMetric v31.30.38 - raise the Windows commit limit with a FIXED 32 GB pagefile (was system-managed, ~24 GB).
#
# Why: the Stable Audio worker needs a large burst of commit charge (RAM + pagefile) while it (re)loads the
# 9.2 GB fp32 checkpoint.  With 23.4 GB RAM and a ~24 GB system-managed pagefile the commit limit is ~46.8 GB;
# measured on 2026-09-19 the machine sat at ~33 GB committed with the DJ running, so a worker reload hit the
# limit and died with "The paging file is too small for this operation to complete" (error 1455) - heard as
# "the model stopped".  v31.30.38 also cut the worker's load peak (~22 GB -> ~10 GB), but a fixed, larger
# pagefile removes the remaining margin risk (Home generation + realtime + browser at the same time).
#
# Run ONCE from an elevated PowerShell (right-click -> Run as administrator), then REBOOT Windows:
#   powershell -ExecutionPolicy Bypass -File .\SET_PAGEFILE_32GB.ps1
# Disk: C: needs ~8 GB more than today (pagefile 24 -> 32 GB).  Undo: Settings -> System -> About -> Advanced
# system settings -> Performance -> Advanced -> Virtual memory -> "Automatically manage".
#Requires -RunAsAdministrator
$size = 32768
$cs = Get-CimInstance Win32_ComputerSystem
if ($cs.AutomaticManagedPagefile) {
    $cs | Set-CimInstance -Property @{ AutomaticManagedPagefile = $false }
    Write-Host "Automatic pagefile management: OFF"
}
$pf = Get-CimInstance Win32_PageFileSetting | Where-Object { $_.Name -like 'C:\pagefile.sys' }
if ($pf) {
    $pf | Set-CimInstance -Property @{ InitialSize = $size; MaximumSize = $size }
} else {
    New-CimInstance -ClassName Win32_PageFileSetting -Property @{ Name = 'C:\pagefile.sys'; InitialSize = $size; MaximumSize = $size } | Out-Null
}
Get-CimInstance Win32_PageFileSetting | Format-Table Name, InitialSize, MaximumSize -AutoSize
Write-Host "Pagefile set to a fixed $size MB. REBOOT Windows for it to take effect."
