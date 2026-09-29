# JoyMetric v31.8.6 — Intelligent 8-Slice Live Remix Deck

This build turns Deck B from a source-derived loop/remix layer into an 8-slice live remix instrument while preserving the v31.8.5 clean path when the slicer is off.

## Intelligent Live Slicer
- Every captured Deck B loop is divided into 8 beat-synchronous cells.
- Downbeat/phrase anchors 0 and 4 are never moved.
- Five slicer modes: GROOVE, FILL, CALL, ACCEL, GHOST.
- Slice selection is not random. Each source slice gets lightweight energy, transient-density and brightness descriptors.
- Percussive scenes prefer local slices with stronger source transients; harmonic scenes prefer smoother sustained cells.
- Only local/safe substitutions are considered, so rearrangement stays recognizable.
- 2 ms micro-crossfades are applied at rearranged boundaries to avoid clicks.
- Deck A keeps playing underneath, preserving slip-style continuity.

## Larger Deck B Effect Field
Deck B now has a dedicated Remix FX Bus before it enters the A/B mixer:
- beat-synchronous 1/8–1/4 beat short taps,
- restrained ping-pong wet return,
- source-derived transient accent,
- mid/side bloom/width,
- bounded wet saturation,
- Deck-B-relative RMS cap.

The B-only FX authority is deliberately higher than the ordinary garnish layer. This makes the remixed/sliced material feel like a second performance deck without driving the shared master into distortion.

## Director V3 additions
28 parameterized gesture families total. New families:
- DECKB_SLICE_GROOVE
- DECKB_SLICE_FILL
- DECKB_SLICE_CALL
- DECKB_SLICE_ACCEL

A typical high-energy phrase can now become:
DECKB_SLICE_GROOVE -> DECKB_SLICE_FILL -> DECKB_SLICE_ACCEL -> DROP -> DECKB_RELEASE.

The critic still penalizes destructive/vocal-risky actions, and the realtime executor reduces Deck B layer level during strong vocals.

## Manual controls
Deck B now exposes SLICER, SLICE MODE and B REMIX FX controls in the Agentic DJ page.

## Safety / stability
- No model call, filesystem access or network request was added to the audio callback.
- RealtimeDSP, MasterSafetyCompressor, SourceFidelityGuard and LookaheadPeakLimiter are AST-identical to v31.8.5.
- With b_slicer_mix=0 and b_fx=0, synthetic parity against v31.8.5 is sample-identical.
