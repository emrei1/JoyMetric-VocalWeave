# JoyMetric Workstation v30.4.41 — Render Endpoint Fix

This release fixes the root cause discovered on Windows: the installed JoyMetric development driver exposed the capture/microphone endpoint, but the Spotify route needs a real render/output endpoint.

## Driver change

v30.4.35–v30.4.40 modified Microsoft's SimpleAudioSample speaker transport from its stock 48 kHz stereo PCM16 format to PCM32 and inserted a custom render→capture kernel ring. v30.4.40 no longer uses that paired capture ring; it captures the private render endpoint through WASAPI loopback instead. v30.4.41 therefore removes both unnecessary driver mutations and restores Microsoft's stock WaveRT speaker transport.

The INF continues to register `WaveSpeaker` under `KSCATEGORY_RENDER` and rebrands the controller as `JoyMetric Virtual Audio Driver`. Windows may display the endpoint as `Speakers (JoyMetric Virtual Audio Driver)` rather than the internal miniport name `JoyMetric Virtual Input`; the app resolves it by actual WASAPI render capability, not by one exact display string.

## Startup/installer safety

`SETUP_AND_START.ps1` now requires BOTH the JoyMetric Media controller and a JoyMetric MMDevice render endpoint (`{0.0.0...}`). If only `Microphone Array (JoyMetric Virtual Audio Driver)` exists, v30.4.41 automatically opens the elevated installer, rebuilds from a fresh Microsoft SimpleAudioSample copy, removes the old root device, and installs the render-safe driver.

The elevated installer also refuses to report success unless Windows enumerates a JoyMetric render endpoint after installation.

## Runtime capture

Spotify path remains:

`Spotify → JoyMetric render/output endpoint → private WASAPI loopback → existing JoyMetric DSP → exact selected physical Audio Out`

The paired microphone endpoint is not used by the Spotify live path.

The loopback opener now supports float32, native PCM16, and PCM32. PCM16 is converted to float32 before the unchanged DSP graph.

## DSP

No Clean6x / MusicCLAP / 15D processing changes were made.
