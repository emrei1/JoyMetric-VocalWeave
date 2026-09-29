# JoyMetric v31.8.8 — Musical Deck-B Match

This release keeps the v31.8.7 smooth Deck-B dominance policy, Intelligent Live Slicer, Source-Aware Transition FX, DJ Director V3, lookahead route, CrackleGuard and headphone fixes, but upgrades **what material Deck B captures**.

## Why v31.8.7 could still sound musically unrelated

v31.8.7 scored loop candidates mostly from seam continuity, local energy stability and a rough source role. A loop could therefore be technically clean but still contain an unimportant ambience fragment, the wrong accent pattern, or melodic material that clashes with the phrase heard immediately after the Deck-B gesture.

## v31.8.8 selector

Deck B now receives a compact description of the next audible phrase from the existing 15-second lookahead:

- 12-bin local chroma / pitch-class profile
- tonal-strength confidence
- 16-step rhythmic accent signature
- future energy, drums and vocal context

At a quantized Deck-B capture, recent whole-beat-aligned candidates are scored by:

1. **Harmonic fit** — local chroma similarity to the upcoming phrase, with limited fourth/fifth tolerance.
2. **Rhythmic fit** — weighted 16th-grid accent agreement; beat anchors carry extra weight.
3. **Salience** — role-aware significance. Percussive Deck B rewards meaningful accent/transient motifs; harmonic Deck B rewards tonal concentration and sustained energy. Very quiet ambience cannot win merely because its relative contrast looks high.
4. **Energy fit** — avoids a loop whose perceived weight is unrelated to the section it is meant to support.
5. **Seam quality** — click-free loopability remains important.
6. **Recency** — still preferred, but an older phrase may win when it is materially more musical and future-compatible.

Search stays bounded to recent whole-beat offsets (0/1/2/4/6/8 beats), so the engine does not dig arbitrarily far into history.

## Significant slice selection

The 8-slice Live Slicer now also tracks slice salience. Percussive patterns prefer meaningful transient slices rather than quiet filler; harmonic patterns prefer sustained, energetic slices. Downbeat anchors 0 and 4 remain fixed and all prior micro-crossfade guards remain enabled.

## UI

Agentic DJ now reports:

- B HARM FIT
- B RHYTHM FIT
- B SALIENCE
- B ROLE

These metrics describe the currently captured Deck-B phrase.

## Realtime safety

No model inference, STFT, file I/O or new allocation-heavy planning was added to the normal audio block path. Chroma/rhythm candidate analysis runs only on quantized loop-capture events. Future phrase descriptors reuse the already existing out-of-band lookahead analysis.

48 kHz / 2048-frame synthetic Deck-B stress: p99 ~2.82 ms in the local validation run, with a 42.67 ms audio quantum.
