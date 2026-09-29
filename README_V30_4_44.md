# JoyMetric Workstation v30.4.44 — Auto Spotify Route + Continuous Audio

This release fixes the two runtime faults found after the JoyMetric render endpoint was successfully installed.

## 1. LET'S GO now applies the Spotify route using the exact JoyMetric render ID

JoyMetric no longer relies on the PyAudio display label alone. At Live start it asks SoundVolumeCommandLine for the active sound-item table, resolves the exact JoyMetric `Command-Line Friendly ID` ending in `\Device\Speakers\Render`, and applies Windows' per-app route with `/SetAppDefault` for the selected Spotify process. The durable process-name preference is attempted before PID fallbacks.

The user still selects only **Spotify** and the real **Audio Out**. `Speakers (JoyMetric Virtual Audio Driver)` remains internal plumbing and is not exposed as an input choice in the JoyMetric UI.

## 2. Periodic 3-second audio dropouts are removed

v30.4.40–v30.4.43 called the Windows routing helper from `_ManagedVirtualChannelInputStream.read()` every ~2 seconds. That was on the realtime capture thread. Launching one or more `svcl.exe` processes there could stop capture long enough for the output FIFO to empty, producing the observed repeating sound / silence / sound cycle.

v30.4.44 routes only at stream start. The per-block read path performs only the WASAPI loopback read. No process enumeration, subprocess, SVCL command or Windows mixer COM scan is allowed on the managed virtual-channel audio path.

The old self-session Windows volume guard is also disabled for the managed virtual channel, removing another periodic COM operation from the playback realtime thread.

## Runtime path

`Spotify -> Speakers (JoyMetric Virtual Audio Driver) -> private WASAPI loopback -> unchanged Clean6x/MusicCLAP 15D DSP -> exact user-selected physical Audio Out`

## Driver

No driver rebuild is required when the v30.4.42/v30.4.43 render driver is already installed and `Speakers (JoyMetric Virtual Audio Driver)` is `OK` in Windows. v30.4.44 changes only user-mode routing/realtime plumbing.
