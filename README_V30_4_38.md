# JoyMetric v30.4.38 — WDK x64 Verification Fix

This revision fixes the Windows WDK 28000 command-line verification failure seen after the JoyMetric driver had already compiled and linked successfully.

Observed v30.4.37 failure:
- SimpleAudioSample.sys was produced successfully.
- WDK MSBuild then tried to load `x86\InfVerif.dll` and failed with HRESULT 0x8007007E.
- WDK ApiValidation invoked the x86 ApiValidator/Aitstatic path for an x64 driver and returned Win32 error 193.

v30.4.38 behavior:
- Keeps Release x64 driver compile/link.
- Disables only the broken command-line in-build package verification and ApiValidator tasks.
- Immediately runs standalone **x64 InfVerif /w** on the generated INF.
- Immediately runs standalone **x64 ApiValidator + x64 Aitstatic** against the x64 driver and x64 UniversalDDI XML files.
- Uses version-matched x64 Inf2Cat and SignTool for catalog/signing.
- Does not reinstall Visual Studio / SDK / WDK if they are already present.
- Keeps the JoyMetric kernel PCM ring, endpoint names, app routing, Clean6x/MusicCLAP DSP, and exact physical Audio Out unchanged.

The workaround is development-build plumbing, not a relaxation of driver validation: the same validation is still performed explicitly using the correct architecture.
