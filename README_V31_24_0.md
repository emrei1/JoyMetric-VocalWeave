# JoyMetric Workstation UI v31.24.0 — "GrooveNet"

"It sounds out of rhythm; where and how the synth / drums enter must be decided by a neural network,
and so must their gain and EQ." — done as a self-supervised model trained on real produced music.

## What was wrong

The rack's stabs used canned genre patterns (off-beat / trap / syncopated) and straight sixteenths;
the drummer's hats had DrumMind's own micro-timing but not the record's swing; layer level and tilt
were fixed ratios. None of that was learned from the record in front of it.

## GrooveNet

**Data (self-supervised, no annotation).** From the local corpus of Spotify previews (30 s, real
produced music; 6000 clips drawn at random), each clip is beat-tracked (kept only when the tempo is
stable, downbeat = the beat rotation with the most low-band energy on 1 and mid-band transients on
2 / 4) and cut into 2-bar windows of 32 sixteenths. Per cell: the strongest **kick / snare / hat**
onset of the percussive component (HPSS) with its **micro-timing** (offset from the grid line, in
cells), and the strongest **harmonic onset** (chroma flux of the harmonic component, weighted by tonal
energy) with its micro-timing. Per window: the harmonic layer's **level relative to the drums** (dB)
and its **3-band tilt** (dB). `groove_features.py` is shared by training and the live path, so the
model sees the same numbers in both.

**Model.** per-step (7 features + tempo) → Linear 64 → BiGRU 96 × 2 → per-step heads (harmonic
onset probability, micro-timing) + pooled sound head (gain dB, tilt dB × 3). 0.3 M parameters,
trained on the GPU in seconds, exported to npz; the runtime is numpy (`groove_net.py`, < 5 ms per
window on the agent thread).

**Held-out evaluation (final model, 33.2 k training windows / 3.5 k validation, 2800 clips):**

| task | GrooveNet | baselines |
|---|---|---|
| where the harmonic layer hits (F1 per sixteenth) | **0.578** | off-beat stabs 0.347 · copy the kick 0.471 · copy the snare 0.401 · corpus-average grid 0.120 |
| micro-timing of those hits (MAE, cells) | 0.254 | predict "straight" 0.263 |
| level vs drums (MAE dB) | 3.23 | corpus mean 4.36 |
| 3-band tilt (MAE dB) | 2.04 / 2.13 / 4.35 | 2.20 / 2.22 / 4.88 |

Live cost: 160-290 ms per 2-bar window on the agent thread (HPSS + STFT), never the audio thread; 0 xruns over the verification session. (During the corpus extraction itself - 8 idle-priority processes - the live session did drop frames; the extraction is an offline step and must not run while the DJ plays.)

## In the DJ (`groove_live.py`, agent `_groove_plan`)

For every stab / arp layer the planner schedules, the 2-bar window that starts at the last downbeat
before the layer is cut from the **16 s lookahead** (~10 s before it plays), resampled to 22.05 kHz,
featured exactly like the training data (per-session normalisation reference), and the net answers:

- **placement**: the stab pattern = the most probable cells at the creativity-set density (10–25 % of
  the 32 cells, never two neighbours) — the rack plays chord stabs exactly there, sample-exact;
- **micro-timing**: each hit carries the predicted offset; the record's **swing** (weighted mean of
  the off-beat sixteenth offsets) shifts the rack's arps and, blended 50/50, the AI drummer's hats;
- **level**: the predicted level-vs-drums relative to the corpus mean scales the layer (×0.6–1.5);
- **tilt**: the predicted 3-band tilt relative to the corpus mean is applied as a ±6 dB 3-band EQ.

Status: `synth_ai.groove` (swing, level_mult, tilt_db, chosen steps, hits in the block, ms).

## Tests (validate_v31_24.py)

numpy runtime == torch (parity < 1e-3); model beats every baseline; features on a synthetic groove
(kicks / snares / hats in the right cells, swung hats show the offset); pattern / swing helpers; the
rack hits the model's beat positions sample-exact, tilt EQ, level multiplier, swung arps; GrooveLive
window geometry on 48 kHz audio; the agent fills the layer spec and the drummer's swing.
