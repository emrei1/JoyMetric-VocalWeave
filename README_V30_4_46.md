# JoyMetric v30.4.46 — CrackleGuard

Built directly on the working v30.4.45 Fast Control Response release. Spotify auto-routing, JoyMetric Virtual Audio Driver handling, exact physical Audio Out selection, MusicCLAP, Clean6x mappings and v45 control-response speed are preserved.

## What changed

- **De-zippered RBJ biquads:** moving EQ/filter coefficients are morphed inside each audio block in short sub-blocks instead of swapping the whole coefficient set at a 512-frame boundary. This targets clicks/zipper noise caused by fast Air/Presence/body/HP/LP, side-tilt, bass-mono and nonlinear-return filter motion.
- **Master Safety Compressor v2:** broad 5 dB soft knee, modest ~1.5:1 ratio, ~5.5–8 ms program-dependent attack and ~260–340 ms release. It shaves only DSP-created peak build-up before the limiter instead of acting as an audible loudness compressor. Maximum requested reduction is capped at 2.8 dB.
- **Smoother true-peak safety:** final ceiling remains -2.0 dBTP; lookahead is 5.0 ms and release 220 ms so the limiter has more time to catch transients without gritty gain modulation.
- **Conservative isolated-sample de-click guard:** enabled by default. It only repairs unmistakable one-sample digital impulses whose two neighbors agree closely. Disable for diagnostics with `JOY_SAMPLE_DECLICK=0`.
- **No slower controls:** v30.4.45 `CONTROL_RESPONSE_SPEED=2.35` is unchanged.

## Runtime architecture unchanged

Spotify -> JoyMetric Virtual Audio Driver render endpoint -> private WASAPI loopback -> StudioDSP -> exact selected physical Audio Out.

No VoiceMeeter/VB-CABLE/ProcTap path was reintroduced.
