# JoyMetric v30.4.45 — Fast Control Response

Based directly on the known-good v30.4.44 Auto Spotify Route + Continuous Audio build.

Changes in v30.4.45:
- Spotify auto-routing, JoyMetric virtual driver, exact physical Audio Out and continuous-audio fix are unchanged.
- Manual feature/control POST debounce: 42 ms -> 18 ms.
- Native two-stage DSP automation keeps the same jerk-limited/C2 topology but defaults to 2.35x response speed.
- Continuous DJ feature deck target/slew smoothing is approximately 1.8–2x faster.
- Reverb/delay/chorus wet crossfades react faster while delay-time movement retains the conservative anti-click slew limit.
- Final feature mapping, FEATURE_RENDER_BOOST 19.50, limiter ceiling and DSP target values are unchanged.
- Set JOY_CONTROL_RESPONSE_SPEED=1.0 to reproduce the v30.4.44 native parameter response speed.
