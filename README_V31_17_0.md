# JoyMetric v31.17.0 — GridTruth

Brief: "there is crackle and it lags — this has to be fixed."

## 0. What the live system actually showed (measured, not assumed)

The running v31.16 session was instrumented in place (py-spy GIL profile, per-thread CPU, status telemetry, and a 60 s loopback capture of the headphone endpoint plus the raw Spotify feed):

| Signal | Value | Meaning |
|---|---|---|
| Output FIFO underruns / concealments | 0 / 0 over 12 min | the audience path never starved |
| Render vs capture rate | 0.9994× both, gap constant 15.0 s | no backlog, no growing lag |
| Loopback capture of the headphone endpoint, 60 s | 0 clicks (3 at the most sensitive setting), 0 gaps | the rendered signal is clean where Windows hands it to the device |
| Output vs input spectrum | −2 … −5 dB in every band, HF crest lower than the source | the added layers do not sizzle |
| Output device | Bluetooth headphones (A2DP), io latency 256 ms | anything heard as crackle after this point is born on the radio link |
| Beat tempo estimate | 83.33 / 115.38 / 166.67 / 125.00 (quantised, flipping) vs 82.65 true | **the beat grid was wrong** |
| Grid confidence | 0.38 – 0.46, gate is 0.45 | every beat-critical gesture deferred ("grid confidence low") |
| Beat anchor update | stale phase (up to 0.85 s old) re-applied every 75 ms tick with gain 0.18 | **the grid wobbled by up to a beat between analyses** |
| Neural action brain | connecting to :8768 — nothing listens there | "neural brain fallback" since the detached launcher |
| Bridge (playback FIFO) fill | 709 ms, target 299 ms | +0.4 s fixed latency left from start-up |
| 15 s + 8 s PCM snapshot copies | every 75 ms (~9 MB per tick) | pointless GIL and memory-bandwidth load |

So: no dropout could be found in software, but the **beat grid itself was measurably lagging and wobbling** — the added kick/hat layers sat tens of milliseconds off the record's beat and drifted between analyses. That is what a listener hears as "lag" (and a flamming kick reads as crackle in a dense mix). Everything below fixes root causes that were proven above.

## 1. BeatClock v2 (`beat_clock.py`) — song-level tempo and phase

- log-flux onset envelope at **5 ms hop** (vectorised, kick-weighted), comb-enhanced autocorrelation on a 0.25-BPM grid with parabolic sub-lag refinement → tempo precision **0.07 %** (was ±4 % quantisation);
- **song-level tempo memory** (30 s decayed profile), octave-aware harmonic support, lock hysteresis: octave/triple estimates never relock, an unrelated tempo relocks after 3 consistent windows, and a run of contradicting windows fades the old memory fast (audio-only relock 6 s after a record change; the Spotify title hook resets instantly);
- **fractional-period phase comb** weighted to the window end, flux latency calibrated (**phase error 0.0 ± 0.4 ms** on a synthetic kick track, −2.4 ms through the agent);
- **calibrated confidence** gated by absolute periodicity strength: white noise 0.04–0.08, a sparse 83-BPM record 0.46–0.69, a steady club track 0.85–0.88 — the legality gate (0.45) now opens on real music;
- cost 11 ms per 15 s window.

## 2. Beat anchor: measurements applied once, at their own time

`_update_beat_clock` now identifies every phase measurement by its analysis time stamp (`_t`), compares it with the prediction **for that time**, applies it exactly once with a confidence-weighted gain, keeps the beat phase continuous across tempo refinements, ignores a single outlier and snaps once after two consistent large disagreements (record change). Reference from the old code path: stale ticks would have dragged the anchor by 279 ms per analysis interval.

## 3. Runtime relief

- 15 s window snapshot refreshed at 0.4 s, the 8 s audible window exactly when its analysis is due (4 copies in 1.6 s instead of ~21);
- agentic status: realtime block trimmed to the fields the agentic panel reads, performance memory 32 → 12 entries (60 KB → ~35 KB per 360 ms poll);
- neural brain client points at the semantic worker port (`JOY_SEMANTIC_PORT`, 8767) where `/agent-state` is actually served; cadence 6 s so the CPU critic keeps its DJ-gradient duty;
- playback bridge: the capture start-up burst is drained before the first audible block, and anything above target+1 block is reclaimed proportionally (4 samples per excess block, capped at 16 = 0.39 %) instead of +2 above 4 blocks → the bridge settles near 680 ms instead of 937 ms (the 597 ms base target with 4096-frame output blocks is deliberate Bluetooth/scheduler headroom).

## 4. Bluetooth note (the part software cannot fix)

The 60 s capture proves the rendered stream is clean at the endpoint. If crackle is still audible on the Baseus Inspire XH1, switch Audio Out in the app to a wired output (Speaker Realtek / wired headphones) for one song: if it disappears, the crackle is the A2DP link (2.4 GHz interference, USB-3 ports, distance), not JoyMetric. Keep the PC's Bluetooth adapter away from USB-3 devices and Wi-Fi on 5 GHz.

## 5. Validation — 182/182

BeatClock suite 14 (synthetic 127.3 BPM, tempo ramp, live 60 s capture with a record change, noise, sparse kick-only), GridTruth agent suite 18, regressions v31.16 (24), v31.15 (11), v31.14 (21), v31.13 (12), v31.12 (21), v31.11 (29), v31.10 (32).
