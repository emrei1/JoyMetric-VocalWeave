# JoyMetric v31.18.0 — GrooveTruth

Brief: "much better, but it has to be even better — there are still sequences that do not fit; the added hats and additions are sometimes also mismatched in level."

## 0. What the live system showed this time (measured on the running v31.17)

A 40 s capture of the raw Spotify feed and of the headphone endpoint was taken simultaneously, the 15.84 s input→output delay was recovered by envelope cross-correlation (corr 0.97), and every low-band / high-band transient of the OUTPUT was classified as "the record's own" or "added by JoyMetric":

| Finding | Value |
|---|---|
| Record's own low-band transients on ANY 16th grid (phase-optimised, all candidate tempos) | 31–42 % — chance level is 36 % |
| Added low-band hits (195 in 40 s) on the record's grid | 30–35 % — chance |
| Added hats on the record's grid | 42–56 % |
| Tempo estimates for the same record over 40 s | 129.9, 135.3, 139.97, 141.1 BPM |
| Added hats vs the record's own hats | −7.4 dB |
| Added low hits vs the record's own kicks | −3.6 dB |

So the "dreamy hypnotic" record has **no grid of its own** (its transients sit at chance on every tempo), yet the agent imposed a full kick pattern and 16th hats on it, at a level unrelated to the record. That is exactly what "sequences that do not fit" sounds like. On top of that the mixer's beat grid was anchored in **wall-clock time** (render-thread scheduling jitter of up to ~120 ms flowed straight into where layers landed) and the bar phase (which beat is "1") was never estimated at all.

## 1. Groove clarity — a DJ listens before adding drums

`beat_clock.py` now measures, per analysis window, how much of the record's own transient content actually sits on the 16th grid of the locked tempo: peak-picked kick-band (< 130 Hz), snare-band (200–2000 Hz) and hat-band (> 5 kHz) envelopes are scored against the grid with a 15 ms tolerance, chance is subtracted, periodicity strength and lock maturity weigh in. Measured: synthetic club record **0.87**, the dreamy capture **0.00**, white noise **0.00**.

The engine multiplies every rhythmic layer (re-drum, hats, key-bass pattern, stabs) by `0.10 + 0.90·clarity^1.3` on its parameter and `0.20 + 0.80·clarity` on the rendered layer; the arranger weights its rhythmic sections (GROOVE+, DROP+, STAB_GROOVE, PERC, ROLL, BASSLINE) by clarity, so beat-less material gets STRIP/HALF textures instead. Beat-less material now receives 1.7 % of the kick energy it received before.

## 2. Frame-accurate grid — no wall clock anywhere

The audible ring reports the absolute render-frame index of the last sample it holds (`snapshot_indexed`, taken under one lock); the agent expresses the last downbeat as an absolute frame (`grid_anchor_frame`); the run loop hands the mixer the ring's frame index for each block (`grid_frame_end`). The mixer's grid target is `(frame_start − anchor)/sr·bps`, pulled on the bar phase (mod 4 beats) and snapped when the error exceeds half a beat. Verified: grid error **0.00 milli-beats** after convergence, rendered hats within a **0.1–0.7 ms spread** of the anchored 16th grid (the remaining constant ~5 ms in the tests is the onset detector's own lag), bar phase of the mixer within **0.001 beats** of the record's true downbeats in a 30 s real-time simulation.

## 3. Downbeat and source patterns

Per beat class (0..3 back from the last beat): kick weight + accent − snare penalty + beat-to-beat spectral change (chord changes land on the "1"). Votes accumulate per absolute beat index; the anchor is placed on a downbeat, so re-drum bars, fills and the arranger's bar counts share the record's bar. Correct in all four bar rotations of the synthetic record. The record's own 16-step kick / snare / hat accents are extracted (downbeat-aligned, kick peaks exactly at steps 0/4/8/12, hats at the 8ths) and the re-drum pattern is chosen by cosine similarity to the record's kick pattern when the section leaves it to "auto".

## 4. Level matching

Added kick/bass and hats are gained per block toward `(0.15 + 0.70·amount)` × the record's own band peak (slow 4 s peak-hold reference, absolute floor and ceiling, 0.4 s smoothing, per-sample ramps), identical on the native and Python paths (nominal design levels, so Rust/Python parity holds). A quiet record now gets a proportionally quieter layer (gain 0.62 vs 2.50 on a loud one), and the amount the agent or the beat confidence chooses still scales the target itself.

## 5. Tempo lock hysteresis

A neighbouring hump 2.5–8 % away (135 ↔ 140 seen live on the dreamy record) needs 8–14 consistent windows before a relock; a record change (unrelated tempo) still relocks after 3.

## 6. Validation — 203/203

GrooveTruth suite 21 (clarity, downbeat, patterns, frame grid, clarity gate, level matcher, agent wiring, 30 s real-time end-to-end alignment), BeatClock 14, GridTruth agent 18, regressions v31.16 (24), v31.15 (11), v31.14 (21), v31.13 (12), v31.12 (21), v31.11 (29), v31.10 (32).

## 7. Semantic worker memory guard (field incident, fixed)

While packaging this version the running CLAP worker of the previous session was found at **60 GB of committed memory** (6.5 GB resident) after about an hour; the machine hit its commit limit and the launcher itself died of OutOfMemory. The worker served every request on a fresh thread (`ThreadingHTTPServer`). It now funnels all model calls through **one persistent inference thread**, rejects requests when three are already queued, logs its commit size every 20 inferences (`[semantic] commit=... MB`), reports it in `/health` (`commit_mb`, `inferences`), and exits cleanly above 9 GB. A detached **worker watchdog** (`worker_watchdog.ps1`, started by the launcher) restarts a missing worker, kills one above 12 GB and removes duplicates, protecting the listener's whole ancestor chain (the venv stub is the parent of the real process).
