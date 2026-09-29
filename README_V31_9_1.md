# JoyMetric v31.9.1 — Musical Co-Mix + Wow Scene Composer

This release targets the two remaining weak points in the Agentic DJ path: Deck-B sounded like a second copy laid on top of Deck A, and high-creativity choreography was active but not consistently surprising.

## 1. Musical Co-Mix engine

Deck B is no longer treated as one broadband secondary signal.

### Future-qualified capture remains mandatory
A candidate must still pass the v31.8.8/v31.8.9 harmonic, rhythm, salience and energy checks. Creativity never lowers the quality gate.

### New sub-beat micro alignment
After a phrase is selected, the engine searches only a tiny ±14–30 ms circular neighborhood (role dependent) for a better transient/seam/rhythm alignment. It preserves the same phrase and beat grid; it does not jump to a different musical cell.

### Automatic musical role resolver
The requested Deck-B texture is now advisory, not absolute:
- weak harmony + strong rhythm -> PERCUSSIVE role
- strong tonal agreement + sparse vocal -> HARMONIC role
- strong combined fit -> BALANCED role

This prevents a rhythmically useful but tonally incompatible loop from being heard as a second melody.

### Complementary spectral slotting
Persistent IIR crossovers split A and B into low / body / air zones. Deck B is fitted into spectral space left by Deck A:
- sub/low remains owned by Deck A
- Deck-B body opens only when harmonic fit permits it
- Deck-B center-mid is reduced when a foreground vocal is expected
- percussive B gets more air/transient space and less tonal body
- harmonic B gets more body but remains low-end constrained

No FFT, neural inference, filesystem operation or model call is added to the realtime callback.

## 2. DJ Director V4 — bounded wow scenes

The compatibility filename remains `dj_director_v3.py`, but the planner now exposes DJ DIRECTOR V4 behavior.

New performance gesture families:
- DECKB_MOTIF_TEASE
- DECKB_COUNTER_GROOVE
- DECKB_ECHO_FREEZE
- DECKB_PICKUP_STUTTER
- DECKB_HOOK_CALL
- DECKB_DROP_FLASH

New phrase-level scene candidates:
- Wow Motif Reveal
- Wow Deck Dialogue
- Wow Fakeout Reveal

At maximum authority the Director may prefer one of these when its musical critic score is within 0.12 of the safest candidate. A wow scene therefore cannot override a substantially better structural/vocal-safe plan.

The same future-qualified Deck-B capture is reused through the scene. The engine does not inject unrelated samples to create surprise.

## Typical max-creativity arc

DECK B COUNTER GROOVE -> ECHO FREEZE -> PICKUP STUTTER -> DROP -> PUNCH IN -> short qualified A/B pulses -> clean return

The exact scene still depends on structure, prompt, recent performance memory, vocals and the fixed Deck-B quality gate.

## Realtime safety

48 kHz stereo / 2048 frame synthetic Deck-B stress run:
- median: ~1.75 ms
- p95: ~2.27 ms
- p99: ~2.33 ms
- audio block budget: 42.67 ms
- peak: ~0.342
- finite PCM / no NaN / no Inf

The single ~26.9 ms max observed in the Linux synthetic harness remained below the 42.67 ms block budget.

## Regression

With Deck B inactive, the v31.9.1 mixer is sample-identical to v31.8.9 in the neutral regression run (`max difference = 0.0`).

Windows Spotify routing, physical WASAPI output and hardware-specific xrun behavior still require validation on the target Windows machine.
