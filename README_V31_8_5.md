# JoyMetric v31.8.5 — Deck B Remix Engine

This build makes Deck B a first-class live remix/ghost deck rather than a rarely-used full-band loop.

## New Deck B behaviors
- Source-derived PERCUSSIVE focus: low/body removal + attack emphasis for ghost drums without claiming stem separation.
- Source-derived HARMONIC bed: restrained mid-band loop layer for breakdown/space scenes.
- Remix layer mode: Deck A stays present while a correlation-aware Deck B layer is added underneath.
- Quantized retrigger: Director gestures can re-arm Deck B at a musical boundary without stopping Deck A.
- Brief call-response swap: bounded crossfader handoffs, automatically released after the structural landing.
- Correlation-aware gain normalization and low-end cuts prevent A/B self-summing from becoming a loud duplicate.

## Director V3 additions
24 parameterized gesture families total. New families:
DECKB_GHOST, DECKB_DRUM_LAYER, DECKB_HARMONIC_BED, DECKB_TEASE, DECKB_SWAP, DECKB_RELEASE.

Candidate plans now explicitly include Deck B choreography. The critic rewards controlled Deck B use but penalizes vocal-risky swaps.

## Stability
No model inference, file I/O, or new FFT is added to the realtime callback. Deck B processing uses the existing PCM loop plus a few stateful filters and bounded mixing math. Existing v31.8.4 source-aware transition FX, clean-headroom logic, lookahead route, and CrackleGuard remain.
