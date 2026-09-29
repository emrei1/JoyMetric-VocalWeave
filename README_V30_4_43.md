# JoyMetric v30.4.43 — Launcher Wait Fix

This release does not change the audio driver, DSP, Spotify routing, or virtual-render capture path from v30.4.42.

It fixes a launcher deadlock observed after a completely successful elevated driver install. `Start-Process -Wait` can remain blocked by descendants of the elevated PowerShell process even after the installer transcript ends and the JoyMetric MMDevice render endpoint is already `OK`.

v30.4.43 launches the elevated installer without `-Wait` and polls the actual JoyMetric render endpoint plus the v30.4.42 success marker. As soon as both are present, JoyMetric starts immediately. Existing v30.4.42 driver installs are accepted and are not rebuilt.
