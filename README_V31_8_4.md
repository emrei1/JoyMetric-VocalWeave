# JoyMetric v31.8.4 — Agentic Authority Boost

This build keeps the v31.8.3 source-aware transition FX timbre and v31.8.2 clean-headroom protection, but makes the Agentic DJ much more dominant when Creativity is near 100%.

## What changed

- Added `agentic_authority` to the compiled live policy.
- Creativity 35% keeps ~1.0x normal authority.
- Creativity 100% reaches 3.6x aggregate Agentic authority.
- The multiplier is **not raw audio gain**. It is distributed across:
  - higher action density / lower NO_ACTION bias,
  - stronger bounded Director V3 gesture intensity,
  - longer parameterized gesture durations,
  - longer FX hold / tail persistence,
  - fuller high-creativity choreographies (typically 5–6 gestures instead of 3–4),
  - one-beat rolling-horizon commit at high creativity,
  - slightly more permissive vocal guard for creative gestures while safety remains active.
- Shared headroom, source-aware FX, limiter, CrackleGuard and physical output behavior are retained.

## Why not multiply the wet gain by 4?

Doing that would reproduce the distortion/limiter problems fixed in v31.8.2. v31.8.4 instead increases the amount of *musical intervention* while leaving each DSP control inside the existing bounded range.

## Expected max-creativity behavior

At Creativity = 100%, a strong structural rise can now create a fuller arc such as:

`GROOVE LIFT → TENSION → FILTER SWEEP → ROLL ACCEL → DROP → PUNCH IN`

rather than only a restrained 3–4 action phrase.

The UI exposes `AGENT AUTHORITY` beside Action Density.
