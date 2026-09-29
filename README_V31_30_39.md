# JoyMetric Workstation UI v31.30.39 — VocalWeave: level-normalised model input + harmony guard

Applied in place to the `v31_30_37` folder on 2026-09-19 (on top of v31.30.38). Changed file: `vocal_weave.py`
(original in the session scratchpad `v31_30_37_orig/`). User report: "especially when the prompt does not match the
original melody there are distortions (bozukluk)".

## What was measured (offline, through the production `VocalWeave._hop_generate`, 4 consecutive 24 s windows of the
## user's own Home upload "All Of The Lights", vocals kept exact, instrumental transformed; metrics against the record)

| condition (TEMP / FX / steps) | chroma kept | rhythm kept | note |
|---|---|---|---|
| record at full scale (RMS 0.47), 0.55 / 2.0 / 4 — any prompt | 0.91–0.92 | 0.78–0.79 | the prompt's content barely changes structure at FX 2 |
| record at the **live capture level** (RMS 0.12), 0.55 / 2.0 / 4 | **0.76** | **0.57–0.63** | same settings, far less of the melody survives |
| live level, 0.55 / **4.0** / 4 | 0.71–0.72 | 0.45–0.50 | harmony and rhythm fall apart; prompt adherence (CLAP) drops too |
| live level, 0.55 / 2.0 / **6 steps** | 0.86–0.87 (full scale) | 0.63–0.72 | more steps make it worse, not better |
| live level, **0.40** / 2.0 / 4 | 0.90–0.92 | 0.76–0.78 | lower TEMP restores structure |
| live level, 0.55 / 2.0 / 4 **+ input normalisation (this build)** | **0.88–0.90** | **0.77–0.78** | one generation, no extra cost |
| live level, 0.55 / 4.0 / 4 + input normalisation **+ harmony guard** | **0.92–0.93** | **0.83** | guard retried 4/4 windows (+~4 s each) |
| live level, 0.55 / 2.0 / 4 + both | 0.88–0.90 | 0.77–0.78 | guard did not trigger (0/8) |

Why the level matters: Stable Audio's audio-to-audio start is `init·(1−TEMP) + noise·TEMP` in latent space, so a
quieter input is anchored more weakly at the same TEMP. Home works on full-scale files; the live weave receives the
Spotify capture at about −18 dBFS RMS — 12 dB quieter — so live windows were transformed far more aggressively than Home
ever was, and a prompt that pulls away from the song (mismatch) had much more room to break harmony and rhythm.

## Changes

1. **Level-normalised model input** (`_input_gain`, default on): each hop (and its 8 s continuation context) is boosted
   to RMS 0.35 before Stable Audio (boost only, peak kept ≤ 0.98, at most ×8) and the output is divided by the same
   factor, so the level logic after it is unchanged. `JOY_WEAVE_INPUT_RMS` (env) or `weave.json {"input_rms": …}`;
   `0` = off. 0.47 gave the same result as 0.35.
2. **Harmony guard** (`guarded_hop_generate`, used by `_run`, default on): after each hop, chroma cosine and onset-pattern
   correlation against the record's hop are measured (librosa, ~0.5 s CPU in the weave thread). If chroma < 0.86 or
   rhythm < 0.60 (`JOY_WEAVE_GUARD_CHROMA` / `JOY_WEAVE_GUARD_ONSET`) the hop is rendered once more with TEMP −0.12 and
   FX ×0.6 (`JOY_WEAVE_GUARD_NOISE_DROP`, `JOY_WEAVE_GUARD_CFG_MULT`) and the rendition that follows the record better is
   kept. Only for windows ≥ 16 s (`JOY_WEAVE_GUARD_MIN_S`); `weave.json {"guard": 0}` or `JOY_WEAVE_GUARD=0` disables it.
   The guard's numbers travel in the window record (`status → synth_ai.weave.last.guard`) and in `monitor_sa3.log`
   (`guard[ch=… on=…]`, `EVENT harmony guard retried window …`).

Budget: a 24 s window costs ~4–5 s to generate; a guard retry adds one more generation, well inside the 48 s lag.

## Not changed / caveats

- STABLE TEMP / FX sliders keep their meaning; the FX floor 2.0 stays. High FX (≥ 4) still degrades structure — the guard
  softens it, it does not make FX 8 sound like FX 2.
- Measured on one song (hip-hop, loud master). The thresholds are tunable; the guard only ever swaps in a rendition that
  measured closer to the record.
- The Home tab is untouched (it already works on full-scale files).
