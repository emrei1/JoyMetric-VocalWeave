# JoyMetric v30.4.47 — LowEndIntegrity

Targeted realtime cleanup for residual crackle on kick/sub-heavy hip-hop.

## What changed

- Managed Spotify virtual-driver DSP quantum: 512 -> 1024 frames at 48 kHz.
- Deep sub below roughly 92 Hz stays linear in the nonlinear branch; 92-220 Hz mid-bass still receives saturation/harmonic drive.
- True-peak limiter attack is planned progressively across the existing lookahead instead of using a one-sample gain jump when a future transient enters the window.
- Realtime true-peak detector uses quarter-sample cubic points (4x-style) instead of eighth-sample points to recover realtime CPU margin.
- Internal limiter detector ceiling is -2.04 dBTP; dense 8x verification remains at or below approximately -2.0 dBTP in stress testing.

## Intentionally unchanged

- Spotify auto-route / SoundVolumeView routing logic.
- JoyMetric WDM/WaveRT virtual audio driver.
- Physical Audio Out selection/failover.
- MusicCLAP semantic controller.
- FEATURE_RENDER_BOOST = 19.50.
- CONTROL_RESPONSE_SPEED = 2.35.
- v30.4.46 coefficient morphing and isolated-sample de-click guard.
