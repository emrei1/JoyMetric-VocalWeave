# JoyMetric v30.4.39 — WDK Tool Discovery Fix

Fixes the v30.4.38 bootstrap failure on WDK 28000 installations where `Inf2Cat.exe` is present only in the x86 host-tools directory.

- Inf2Cat / SignTool are now discovered independently and may use x64 or x86 host binaries.
- InfVerif prefers x64, then x86, then any installed WDK copy.
- x64 ApiValidator is used only when a complete x64 validation set is actually installed.
- If WDK 28000 lacks the x64 ApiValidator set, development installation continues after `InfVerif /w`, catalog generation and test signing, with an explicit warning.
- No DSP, routing, kernel PCM bridge, INF patch, or endpoint-name changes from v30.4.38.
