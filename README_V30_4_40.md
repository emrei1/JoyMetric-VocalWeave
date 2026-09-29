# JoyMetric Workstation v30.4.40 — Virtual Input Loopback Fix

This build fixes the first live-routing issue after the JoyMetric-owned virtual driver was successfully installed.

## Live flow

Spotify → **JoyMetric Virtual Input** (JoyMetric's own WDM/WaveRT driver) → **private WASAPI loopback of that exact endpoint** → existing Clean6x/MusicCLAP DSP → exact selected physical Audio Out.

The paired `JoyMetric Internal Capture` microphone endpoint/kernel ring is no longer used by the Spotify live path. It remains installed with the development driver, but v30.4.40 bypasses it. This removes a second kernel transport stage and captures exactly what Windows renders into JoyMetric Virtual Input.

The internal JoyMetric endpoints remain hidden from the UI. The user still selects only Spotify and the physical Audio Out.

## Routing fix

v30.4.39 treated a zero exit code from SoundVolumeCommandLine as proof that Spotify had been routed. The helper can return success even when a set command affects no item. v30.4.40 uses `/Stdout` and requires a matched item, tries the stable endpoint name `JoyMetric Virtual Input`, and arms the private loopback before applying the per-app route.

## Driver

No driver rebuild/reinstall is required when upgrading from v30.4.39 if `JoyMetric Virtual Audio Driver` is already installed. The kernel driver source/patch is unchanged.
