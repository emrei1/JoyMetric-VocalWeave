# JoyMetric v31.2.0 — Neural Agentic DJ

This branch replaces the v31.1 rule-heavy DJ brain with a neural audio-informed decision loop while preserving the proven Windows audio path and v30.4.47 LowEndIntegrity master.

## Live architecture

Spotify PCM -> JoyMetric Virtual Driver -> realtime ring buffer

- 250 ms transient guard
- 1.5 s fast/groove observer
- 15 s bounded DSP/MIR context
- newest 9.75 s raw PCM -> isolated LAION CLAP neural worker
- ~60 s rolling CLAP embedding/state memory (24 observations)
- prompt-conditioned neural DJ-action affinities
- candidate critic: musical state + low-end/transient safety + repetition penalty + NO_ACTION
- beat/bar/phrase quantized deterministic scheduler
- normal DJ controller actions -> Live Deck A / Loop Deck B mixer
- unchanged v30.4.47 LowEndIntegrity / CrackleGuard master
- exact physical Audio Out

No neural inference, HTTP request, subprocess, file I/O, or Spotify transport call runs inside the realtime audio callback.

## Neural action space

NO_ACTION, LOW_END_PROTECT, EQ_TIGHTEN, FILTER_BUILD, FILTER_RELEASE, ROLL_1, ROLL_2, ECHO_THROW, REVERB_SPACE, ENERGY_LIFT, LOOP_RELEASE, BEATJUMP_FWD, BEATJUMP_BACK.

The prompt conditions the CLAP text-action bank. For example, a restrained clean hip-hop prompt changes the action embeddings the live audio is compared against; it is not only parsed by keyword rules.

## Why CLAP rather than MERT in the product core

MERT-v1-95M is useful research technology but its published checkpoint is CC-BY-NC-4.0. This prototype keeps the core on LAION CLAP (Apache-2.0), which JoyMetric already downloads in its isolated semantic environment. The architecture remains modular enough to plug in another commercially suitable music encoder later.

## Failure isolation

If the neural worker is loading, unavailable, or crashes, the realtime audio route remains live. The agent falls back to bounded safety behavior and reports NEURAL BRAIN: FALLBACK instead of killing Flask or the WASAPI callback.

The v31.1.2 crash-safe device scan is retained: native audio enumeration remains isolated from the Flask server.

## Important prototype limit

Spotify is still one live source. Deck B is a real JoyMetric rolling Loop/Roll deck, not a fake second Spotify deck. The agent may use guarded Spotify beatjump when the prompt explicitly asks for skip/crop/cut/repeat behavior, but it does not pretend to know future Spotify PCM.
