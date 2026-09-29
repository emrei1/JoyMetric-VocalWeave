# JoyMetric v31.8.2 — Clean Headroom / Distortion Fix

This build keeps DJ Director V3, unified Realtime FX + Agentic DJ, the v31.8.1 start/route handshake, and the v31.7 continuity/headphone protections while fixing the excessive crackle/fizz/distortion heard during strong concert FX.

## Root cause
v31.8.1 let the Agentic performance bus and the shared Realtime StudioDSP obey the same strong prompt independently. During builds/drops the additive DJ layers could exceed 0 dBFS before StudioDSP; StudioDSP then added EQ, saturation, compression and time FX again. The final limiter prevented numeric clipping but could not prevent upstream nonlinear harshness and limiter pumping.

## v31.8.2 changes
- Shared Mix Authority coordinates Agentic DJ and Realtime StudioDSP.
- Strong performance FX automatically reduce duplicate StudioDSP drive/saturation, time-FX, compressor makeup and positive EQ authority.
- Performance residual bus is RMS-bounded against the clean song instead of being allowed to become a second full mix.
- Pre-StudioDSP peak guard keeps the concert bus around 0.82 peak without hard clipping.
- Echo feedback ceiling reduced and return RMS is bounded relative to dry program level.
- Riser/reverse/snare noise is lower, darker and more band-limited.
- Reverse/crash stereo anti-phase is reduced for cleaner headphone playback.
- Impact sub/click/crash levels are rebalanced and crash bandwidth is limited.
- DJ Director V3 and startup routing behavior remain intact.

## Stress comparison
Aggressive synthetic 174 BPM concert-FX case, 48 kHz stereo / 2048 frames:

v31.8.1:
- pre-StudioDSP mixer max peak: ~1.172
- worst limiter reduction: ~-2.99 dB

v31.8.2:
- pre-StudioDSP mixer max peak: ~0.820
- worst limiter reduction in the same test: ~-0.05 dB
- finite PCM / no NaN / no Inf

Windows Spotify/WASAPI hardware behavior still requires validation on the target Windows machine.
