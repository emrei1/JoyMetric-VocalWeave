# JoyMetric v31.8.9 — Qualified A/B Shift Cadence

This release keeps the v31.8.8 Musical Deck-B Match selector, Intelligent Live Slicer, Source-Aware Transition FX, DJ Director V3, Clean Headroom, CrackleGuard, startup routing and headphone fixes, while increasing **A↔B performance cadence at high creativity**.

## What changed

At normal creativity, Deck-B behavior stays close to v31.8.8. As creativity rises above the concert threshold, a dedicated Deck-B shift multiplier ramps from **1.0x to 3.5x**.

At creativity = 100%, the Director can add up to four short phrase-safe A→B micro-shifts, each followed by a B→A return. These are not full-deck replacements: the crossfader pulse is intentionally small and brief, while the already matched Deck-B loop remains resident between pulses. This produces substantially more mixer movement without making Deck B dominant.

## Musical quality remains mandatory

Frequency does **not** weaken the v31.8.8 matching rules. A new fixed Deck-B Musical Gate runs after loop capture and before any audible crossfade/layer. It evaluates the captured loop using the existing:

- harmonic fit to the upcoming phrase
- rhythmic 16-step accent fit
- role-aware salience/significance
- energy fit

The gate is role-aware:

- percussive Deck B requires strong rhythm + salience and, when the upcoming phrase is tonal, a minimum harmonic fit;
- harmonic Deck B requires harmonic fit + salience and a minimum rhythm floor;
- balanced Deck B requires both rhythmic and harmonic agreement.

If the loop fails these conditions, crossfader and layer authority are multiplied to zero even at creativity 100%.

## High-creativity pulse design

A typical qualified high-creativity phrase can now behave like:

```
Deck-B matched slice capture
→ short A→B micro shift
→ return A
→ musical gesture / fill
→ short A→B micro shift
→ return A
→ drop / structural landing
→ short A→B micro shift
→ return A
→ short A→B micro shift
→ return A
```

The short pulses reuse the already selected loop, so they do not repeatedly run loop-candidate analysis inside the realtime path.

## UI

Agentic status adds:

- **B SHIFT RATE** — 1.0x … 3.5x based on creativity
- **B QUALITY GATE** — final 0–100% audible qualification after harmonic/rhythmic/salience checks

The existing B HARM FIT / B RHYTHM FIT / B SALIENCE / B ROLE metrics remain visible.

## Safety / performance

Synthetic validation at 48 kHz stereo / 2048 frames with a qualified live-slicer Deck-B loop and repeated A↔B pulses:

- mean ~1.76 ms
- p95 ~3.14 ms
- p99 ~4.76 ms
- max observed sample peak ~0.281
- realtime budget = 42.67 ms

Deck-B-off neutral output remains sample-identical to v31.8.8 in the regression test.
