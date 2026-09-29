# JoyMetric v31.0.0 — Agentic DJ Prototype 01

This branch keeps the proven v30.4.47 Windows Live path intact and adds a separate local-file two-deck Agentic DJ laboratory.

## What is real in this prototype

- Two local decks (A outgoing / B incoming).
- Whole-track decode before planning; the agent is not limited to 3-second current-moment windows.
- Beat/BPM map from spectral-flux onset tracking + autocorrelation.
- Downbeat/bar map and 8-bar phrase grid with structural novelty snapping.
- Key estimate, normalized bar-energy map, cue candidates and waveform overview.
- Prompt → explicit DJ policy (transition length, low-end policy, FX authority, overlap policy, energy direction).
- Candidate search over phrase-aligned cue combinations.
- Critic score combining phrase alignment, energy trajectory, harmonic relation and tempo-stretch risk.
- Deterministic action timeline: prepare, downbeat start, equal-power crossfade, bass swap, outgoing HPF, optional JoyMetric semantic arc, handoff.
- Deck B tempo sync is prepared outside playback. Resampling speed is pitch-compensated offline with Pedalboard PitchShift.
- Render uses v30.4.47 RealtimeDSP / LowEndIntegrity as the master chain.
- Playback thread performs no model inference, file reads, track analysis, subprocess calls or planning. It streams pre-rendered 48 kHz stereo float PCM only.
- Physical Audio Out uses the same exact-WASAPI-first failover philosophy as JoyMetric Live.

## Deliberate prototype limits

This is not presented as a finished professional DJ replacement. It proves the architecture with real audio. It does not fake features that are not implemented yet.

Not yet implemented: true stems, vocal-aware cue scoring, PFL/headphone audition, MIDI/HID controller mapping, arbitrary hot-cue editing, scratch/jog behavior, multi-song set planning, online RL/imitation policy, multi-transition rolling replanning during playback.

## Recommended test

1. Open **Agentic DJ** from the left navigation.
2. Load Deck A and wait for `MAP READY`.
3. Load Deck B and wait for `MAP READY`.
4. Enter a DJ directive or choose a prompt chip.
5. Click **AGENT PLAN**.
6. Inspect Critic Score and Action Timeline.
7. Click **RENDER PLAN**.
8. Choose the same physical Audio Out you normally use with JoyMetric.
9. Click **PLAY**.

For hip-hop, use tracks whose BPM difference is within roughly ±8% for the highest-quality prototype tempo preparation.
