# JoyMetric v31.7.0 — Concert Controller V2 / Continuity Engine

This release focuses on two things: removing intermittent audible holes and making the Agentic DJ controller behave more like a professional concert-performance surface instead of a small macro layer.

## Cut / interruption diagnosis and fixes

### 1. Loop-release hole fixed
v31.6 cleared Deck B immediately when a loop-release sequence arrived. The crossfader itself is intentionally smoothed, so for a short period it could still request Deck B after Deck B had already been erased. That created a real level dip on some ROLL -> DROP / RELEASE transitions.

v31.7 uses graceful release:
- request release
- crossfade smoothly back to Deck A
- keep Deck B alive during that fade
- retire the loop only after the smoothed crossfader is effectively at A

Synthetic regression: minimum RMS during release remained 99.3% of the pre-release mean instead of creating a short hole.

### 2. Non-silent underrun concealment
The output bridge no longer fills a short queue miss primarily with silence. It reuses a tiny recent waveform grain, crossfades into it, and applies a shallow decay. This is only an emergency concealment path; normal playback remains untouched.

### 3. Larger continuity bridge
Agentic mode already has a 15-second intentional musical lookahead, so v31.7 spends more of that latency budget on reliability:
- playback FIFO capacity: ~1.8 seconds
- normal target fill: 7 output quanta
- startup fill: 8 output quanta
- adaptive recovery ceiling: 14 output quanta

The physical output remains decoupled from capture/DSP timing.

### 4. Temporal agent CPU spikes reduced
v31.6 ran the full BPM/key/STFT analysis separately on every bar-ish chunk of the 15-second future window. That was out-of-band but still caused periodic CPU pressure in the same process as realtime audio.

v31.7 keeps one global musical analysis and uses lightweight per-segment descriptors for:
- energy
- transient/drum density
- bass/mid/high ratios
- vocal/melody activity proxies
- adjacent-segment novelty
- energy/drum direction

No extra CLAP call is made for every chunk. In the local synthetic benchmark, a six-segment 15-second scan averaged about 17.7 ms and p95 about 19.9 ms.

## Concert Controller V2

Discrete controller vocabulary:
- 20 neural atomic DJ actions
- 16 concert macros
- 36 total discrete actions

The 16 macros are:
1. TENSION
2. ECHO OUT
3. ROLL ACCEL
4. DROP
5. SPACE BREAK
6. GROOVE LIFT
7. FILTER SWEEP
8. TRANS BUILD
9. 1/2 ROLL
10. 1/4 ROLL
11. 1/2 ECHO
12. 1-BEAT ECHO
13. WASH OUT
14. PUNCH IN
15. DRUM FILL
16. CLEAN RESET

These sit above the continuous controller lanes (Deck A/B EQ, filter, gain, crossfader, reverb, echo, performance FX rack, echo send/feedback/time, transformer gate, wet width, ducking, drum drive, double-time articulation, riser/reverse/snare/impact, punch/energy/clarity/air, low-end tightening).

## Scene-aware gesture grammar

The rolling-horizon planner no longer throws independent actions at the same boundary. It builds short performance chains and guarantees one primary command per beat.

Typical upward boundary:
TENSION -> FILTER SWEEP -> ROLL ACCEL / DRUM FILL -> DROP

Typical downward boundary:
1/2 ECHO -> WASH OUT -> SPACE BREAK

Neutral structural change:
TENSION -> restrained TRANS BUILD -> PUNCH IN

The next committed beats are never rewritten. Farther-future actions can be replanned as new audio arrives.

## High-BPM scheduler

At 170+ BPM, v31.6's ~180 ms main planner cadence could miss the early beat-phase execution window. v31.7 adds a cheap scheduler tick every ~55 ms between expensive analysis passes. It:
- copies no 15-second audio
- runs no FFT
- runs no neural inference
- predicts phase from the already locked beat anchor
- executes queued controller actions on time

## Professional gate behavior

The transformer/gate no longer pushes the high-frequency bus as close to silence. Its envelope floor was raised so it behaves like rhythmic articulation rather than random dropouts, while the sub/kick body stays continuous.

## Audio-quality regression boundary

The following classes are AST-identical to v31.6:
- RealtimeDSP
- MasterSafetyCompressor
- SourceFidelityGuard
- LookaheadPeakLimiter

The underlying LowEndIntegrity/master path was not rewritten.

## Local validation

- Python compile: PASS
- JavaScript syntax: PASS
- neural action count: 20
- concert macro count: 16
- total discrete action vocabulary: 36
- all 16 concert macros execute against the controller state: PASS
- one-primary-action-per-beat queue: PASS
- committed-horizon protection: PASS
- graceful loop-release minimum RMS / pre-release mean: 0.993
- heavy RealtimeDJMixer p99 at 48 kHz / 2048 frames: ~5.28 ms
- heavy mixer + full existing RealtimeDSP p99: ~22.94 ms
- 2048-frame budget at 48 kHz: 42.67 ms
- finite PCM / no NaN / no Inf: PASS

## Important Windows validation boundary

This build environment cannot run the user's physical Windows WASAPI endpoints or Spotify route. The code paths are static/synthetic tested here; final hardware validation still needs the real Windows machine. Watch XRUNS while listening. The goal is 0 during normal playback.
