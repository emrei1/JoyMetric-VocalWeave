# JoyMetric v31.13.0 — GridLock

Targeted fix for the reported bug: **the ghost hi-hats (and other rhythmic FX) could be rhythmically off** — right tempo, wrong phase.

## Root cause

Every rhythmic FX layer (source-aware hats, snare rush, redrum, pump fallback) derived its pattern position from `_fx_sample_clock` — a free-running counter that starts at engine start. Two consequences:

1. **Arbitrary phase**: the 16-step accent grid had no relationship to the song's actual beats — accents could sit anywhere inside the beat, including dead offbeats ("ritmik açıdan uyumsuz").
2. **BPM re-estimate scrambling**: pattern position was recomputed as `clock × rate` from zero, so every small BPM update (119.8 → 120.3) jumped the pattern by multiple steps — the hats would suddenly "turn around" mid-phrase.

## Fix — the beat-locked FX grid

One shared **beat-phase accumulator** (`_grid_beats`) now drives hats, rush, redrum and the pump:

- **Advances by BPM per block** (an accumulator, not a from-zero recompute) → BPM re-estimates can no longer scramble the pattern. Verified: accent alignment ratio stays 6.0 under ±0.6 BPM per-block jitter.
- **PLL-locked to the agent's audible beat anchor**: the agent posts its onset-locked `_beat_anchor_t` (`grid_anchor_t`) plus `bpm_confidence` (`grid_conf`); the engine gently pulls grid phase toward `(now − anchor) × bps` (6–22 %/block, confidence-weighted, wrap-to-nearest-beat → jump-free). Verified with a deterministic clock hook: accents land on the *anchor's* beats (energy ratio 5.7), not on the old engine-start grid.
- **Confidence gating**: when the tracker is unsure, the grid gets quiet instead of confidently wrong — hats scale by `0.20+0.80·conf^1.35` (5.7× quieter at conf 0.05), redrum by `0.35+0.65·conf` (0.795 → 0.325 effective).
- **Pump** breathes directly on the locked grid when the anchor is present (duck lands exactly on the beat; sync-seq fallback kept for manual/unlocked use).
- **Swing** (`fx_swing`, new SWING fader, prompt keywords *swing/shuffle/swung*, hard variants): odd 16ths are delayed by the swing amount across hats and redrum — straight-16th ghost hats no longer fight a shuffled song.
- Manual/unlocked mode (no agent running) keeps the legacy free-running behavior; neutral-controls output remains **sample-identical**.

UI: FX Rack grew to 24 faders (+ SWING); the DJ Director grid shows **FX GRID · BEAT-LOCKED · conf %** (or FREE).

## Validation — 94/94 across four suites

**GridLock suite (12)**: anchor-locked hat accents (5.74 ratio over 7 beats) and proof they do *not* follow the old grid; jitter stability (6.04); confidence mute (hats 5.7×, redrum 0.795→0.325 effective); redrum kicks on anchor beats (2.51 low-band ratio); pump duck on the locked beat (0.050 vs 0.096); swing delays odd 16ths (0 → 0.00008); neutral sample-identical; legacy unlocked mode intact; agent posts anchor/conf/swing.
**Regression**: v31.12 (21), StageCraft (29), v31.10 (32) — all green.
