# JoyMetric v31.14.0 — TimeWeaver

The brief: creativity/variance ×10-20, "terrifyingly good engineering", full-remix level, rethink the 15-second lookahead ("first 7.5 s / next 7.5 s… with more interesting transitions"), full architectural freedom — *surprise me*.

The surprise: **the 15-second lookahead stops being just analysis and becomes MATERIAL.** The DJ now plays with time itself.

## 1. TimeWeave — gestures on the time axis

The mixer archives its own output (the past the listener heard) and reads the engine's lookahead ring directly (the future the listener has NOT heard yet). One beat-quantized state machine renders five time gestures on the program bus while the real stream marches on untouched underneath — so the return to live is always sample-exact on the landing beat:

| Mode | Gesture |
|---|---|
| BRAKE / BACKSPIN | (v31.11) turntable stop / reverse rip |
| **JUMPBACK** | beat-exact replay of the previous phrase — a live arrangement edit ("hook extend") |
| **FLASHFORWARD** | a 2-3 beat *vision* of audio from N beats in the future — the hook is foreshadowed before the song reaches it; when the phrase arrives for real, the listener already knows it |
| **INTERLEAVE** | call-and-response between NOW and the FUTURE in alternating beat cells (the user's 7.5 s/7.5 s idea, generalized: offset and cell size are free parameters) |

Verified with amplitude-encoded carrier signals: JUMPBACK reproduces the input from exactly 4 beats earlier; FLASHFORWARD reproduces material 16 beats ahead of the render position; INTERLEAVE alternates between both streams; live returns exactly after each effect. Zero contention risk: future reads are non-blocking single-slice copies (`StereoAudioRing.read_at`, absolute-frame anchored via a new `total` counter), with graceful no-ops when material is missing.

New Director archetypes: **future_echo_reveal** (flash the future hook → build toward it → the real phrase lands as the payoff), **time_interleave_break**, **hook_extend_edit**. New pads: FLASH FWD / JUMPBACK / TIME WEAVE. The gesture-collapse logic now re-slots (instead of dropping) early foreshadow gestures on short runways.

## 2. KEYBASS + STAB — the remix owns the key, not just the beat

- **KEYBASS** (`fx_bass_synth/root/pattern/conf`): a synthesized bassline in the **detected key** (stabilized root from HarmonicField, Camelot pipeline), four patterns (PULSE offbeat-house / DRIVE syncopated / SUB long roots / OCT octaves), per-bar hash DNA (fifth/octave substitutions ≈ 20 % of bars), sidechain-ducked from the redrum kick, gated by key confidence. Combined with the build bass-strip, the remix literally replaces the source's low end. Verified: root A → 54.1 Hz peak, root D retunes to 74 Hz, PULSE sits on the offbeats, bounded.
- **STAB** (`fx_stab/pattern`): band-passed power-chord stabs (root+fifth+octave — no third, so major and minor are both safe), offbeat or dub patterns with ~20 % hash-dropped hits, kick-ducked.
- The FX protection chain now grants the replacement layers dry-independent headroom (they are the new floor, not garnish).

## 3. Redrum Pattern DNA — the groove never loops identically

- `fx_redrum_var`: per-bar deterministic hash mutation — velocity humanization ±20 %, ghost-kick injection, clap displacement. Verified: bar-to-bar fingerprint difference 0.163 vs 0.018 static.
- `fx_redrum_fill`: every 4th bar ends in one of three auto-fills (roll / buildup / dropout), every 8th bar a longer one. Verified: 4-periodic tail contrast 0.59.

## 4. Arranger 2.0 — variance at every scale

- **8 sections** (+BASSLINE spotlight, +PERC breather, +STAB_GROOVE) with weighted-grammar transitions.
- **Per-instance DNA**: every section pass samples its targets ±25 % and re-rolls patterns (redrum FLOOR↔UPBEAT swaps, bass pattern per section, stab pattern) — consecutive instances of the same section are forced to differ. Verified: 20 distinct instance signatures in a 4-minute run.
- **Macro-arc**: a ~170 s set-level intensity wave (±25 % · remix intensity) so the performance breathes at the arrangement scale.
- Agent streams the stabilized key root + confidence to the engine every tick.

## 5. UI

FX Rack → **29 live faders** (+BASSLINE, B-PATTERN, STAB, R-VAR, FILLS); pad bank → 24 pads (+FLASH FWD, JUMPBACK, TIME WEAVE, RESET — teal TimeWeave styling); Musical Mind grid gained **BASS KEY** (root · confidence · pattern) and **TIME WEAVE** (active mode + winding %) cells.

## 6. Validation — 115/115 across five suites

TimeWeaver (21): pattern DNA mutation + auto-fills; keybass tuning/retuning/offbeat placement; encoded-signal proofs for JUMPBACK / FLASHFORWARD / INTERLEAVE + clean return; neutral-controls output **sample-identical** to legacy; arranger vocabulary/instance-DNA/keybass-hold/root-posting; TimeWeave pads and Director archetypes.
Regression: v31.13 GridLock (12), v31.12 FullRemix (21), v31.11 StageCraft (29), v31.10 (32) — all green.
