# v30.4.35 changes

- Replaced the v30.4.34 managed third-party virtual channel with JoyMetric-owned WDM/WaveRT driver source/build tooling.
- Added a real render→capture PCM shared ring in the JoyMetric driver patch.
- Driver wire format is fixed at 48 kHz / stereo / signed PCM32 on render and capture.
- Spotify Live route no longer uses ProcTap, source mute/duck, or a second physical/HDMI parking endpoint.
- Internal JoyMetric render/capture endpoints are hidden from the UI.
- LET'S GO performs app-specific routing to JoyMetric Virtual Input automatically.
- Existing v30.4.31 exact physical Audio Out path is preserved.
- Clean6x/MusicCLAP feature mapping and DSP classes are unchanged from v30.4.34.
