# JoyMetric v31.8.7 — Smooth Deck-B Loop Selection

This build keeps the v31.8.6 Intelligent Live Slicer and high-authority Deck-B Remix FX, but makes A/B movement less dominant and captured loops more musical.

## 1. Less dominant A/B shifts
- Director-generated roll crossfades are reduced by roughly one third.
- DECKB_SWAP is now a short accent, not the default foreground takeover.
- Deck-B slicer/ghost/drum gestures prefer layer mode over full crossfade.
- Agent-side Deck-B layer authority is capped lower while B-only FX can still remain strong.
- The critic adds an explicit swap penalty so plans prefer layered remix gestures when both choices are musically plausible.
- High-creativity plans are de-collided to one primary gesture per beat, preventing a hidden second Deck-B move from firing late.

## 2. Musical Loop Selector
A loop capture no longer blindly copies the newest N beats.

The engine evaluates a small set of whole-beat-aligned recent candidates using:
- loop-seam continuity,
- endpoint amplitude/slope continuity,
- energy stability across eight subregions,
- source-role compatibility (percussive vs harmonic),
- recency.

Only whole-beat offsets are considered, so beat-grid phase is preserved. An older phrase is selected only when it is materially cleaner than the newest candidate.

## 3. Smoother loop wrap
- Loop seam guard increased from 4 ms to 6 ms.
- Equal-power seam blending replaces the previous linear seam blend.
- Existing 2 ms slicer micro-crossfades are retained.

## 4. Deck-B character is still strong
The B-only Remix FX Bus remains intentionally more dramatic than Deck A. The change is about reducing foreground A/B replacement, not making Deck B inaudible.

## Validation summary
Synthetic 48 kHz / 2048-frame tests in the artifact environment:
- deliberately damaged newest 2-beat loop seam error: 2.6035
- selected beat-aligned loop seam error: 0.9744
- selected offset: 1 beat
- heavy Deck-B slicer/FX stress: p99 5.25 ms / 42.67 ms budget
- stress peak: 0.2321
- finite PCM: PASS
- high-authority Director plan: unique primary beats PASS
- Deck-B-off sample parity vs v31.8.6: max abs diff 0.0

Windows Spotify/WASAPI hardware behavior still requires validation on the target Windows machine.
