# JoyMetric v31.8.3 — Source-Aware Transition FX

This release targets FX *quality*, not agent timing or planning. DJ Director V3, shared prompt, lookahead, route/start fixes and Clean Headroom remain intact.

## What changed

- Replaced white-noise-only double-time hats with source-derived 4–13 kHz transient grains plus a small coherent air layer.
- Added four 16-step velocity/decay groove fingerprints; a new fingerprint is chosen per activation, not per audio block.
- Fixed rhythmic FX pattern continuity with one absolute sample clock.
- Rebuilt risers as four stable event-level spectral fingerprints blending coherent filtered air with live-song upper-band texture.
- Rebuilt reverse swell as live-song body + restrained air instead of a synthetic noise wash.
- Rebuilt snare/drum rush as source-derived mid/high body + a quiet stick/noise layer, with four event-level 16-step patterns.
- Stereo width is mid/side/coherent rather than hard anti-phase noise.
- No sample/file/model work occurs in the realtime callback.
- v31.8.2 Clean Headroom residual cap and pre-StudioDSP gain budget remain downstream.

## Intent

The agent already knew *when* to build or accent. This version gives it a better instrument: transition percussion and sweeps now inherit the currently playing record's timbre and vary from event to event, reducing stock-FX repetition.
