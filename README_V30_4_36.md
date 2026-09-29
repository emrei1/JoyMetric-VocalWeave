# JoyMetric v30.4.37 — Driver Bootstrap / Install Diagnostics

This revision keeps the v30.4.35 JoyMetric virtual-audio architecture and DSP unchanged.

Installer fixes:
- saves the elevated driver build/install transcript to `%LOCALAPPDATA%\JoyMetric\logs\driver-install-v30.4.37.log`
- prints the last log lines back into the original PowerShell window on failure
- checks Secure Boot / TESTSIGNING first
- can bootstrap Microsoft's current Visual Studio + SDK + WDK toolchain with Microsoft's official WinGet configuration when tools are missing
- no longer assumes a preinstalled WDK DevCon binary; builds Microsoft's DevCon sample locally from source
- preserves the one-reboot TESTSIGNING flow

Important: a locally test-signed kernel driver still cannot load with Secure Boot enabled. JoyMetric does not disable Secure Boot or BitLocker automatically.
