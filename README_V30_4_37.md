# JoyMetric v30.4.37 — Driver Source Encoding Fix

This release fixes the concrete v30.4.36 failure shown by Windows:
`UnicodeDecodeError: utf-8 codec can\'t decode byte 0xff` while patching `SimpleAudioSample.inx`.

Microsoft currently stores `SimpleAudioSample.inx` as a BOM-marked UTF-16 text file. The JoyMetric patcher now detects UTF-16 / UTF-8-BOM / UTF-8 per source file and writes every modified file back using its original encoding.

No DSP, routing, virtual-cable architecture, or Audio Out behavior changed. The installed VS/SDK/WDK toolchain is reused; only the JoyMetric derivative build folder is recreated from the untouched Microsoft source cache.
