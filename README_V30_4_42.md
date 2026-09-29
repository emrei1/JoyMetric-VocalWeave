# JoyMetric Workstation v30.4.42 — Pristine Render Endpoint Reset

This release fixes the v30.4.41 driver patch failure and makes the render/output rebuild deterministic.

## Root cause fixed

v30.4.41 required an exact source line (`#define SPEAKER_HOST_MIN_BITS_PER_SAMPLE 16`) before it would build. The user's cached Microsoft SimpleAudioSample checkout did not match that exact textual shape, so the patcher aborted before compiling the driver. The check was unnecessary because JoyMetric no longer modifies `speakerwavtable.h` at all.

v30.4.42 removes that brittle exact-line requirement. It only performs a tolerant structural sanity check that Microsoft's stock WaveRT speaker still has a render sink and the 48 kHz / stereo / PCM16 format.

## Pristine upstream guarantee

When `SETUP_AND_START.ps1` invokes a forced driver rebuild, the installer now deletes the cached `windows-driver-samples` sparse checkout and clones a fresh Microsoft `main` sample tree before patching. This prevents any development mutation from v30.4.35–v30.4.40 from contaminating the render-safe build.

## Driver identity

- Hardware ID: `ROOT\JoyMetricVirtualAudio`
- Development DriverVer: `0.3.0.0`
- Expected Windows output endpoint: `Speakers (JoyMetric Virtual Audio Driver)` / JoyMetric render MMDevice
- Internal miniport name: `JoyMetric Virtual Input`
- Microphone/capture endpoint may also remain visible, but Spotify does not use it.

## Runtime route

Spotify → JoyMetric render/output endpoint → private WASAPI loopback → unchanged JoyMetric DSP → selected physical Audio Out.

No VoiceMeeter, VB-CABLE, ProcTap, source mute, source duck, or second-output parking is used for this route.
