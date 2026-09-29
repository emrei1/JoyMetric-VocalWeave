# JoyMetric v31.10.0 — Musical Mind

This release fixes the "dead deck" bug, gives the Agentic DJ a literature-grounded musical-awareness core, adds the requested **contrary-motion transition layer** (one voice rises while the other falls), and turns the full FX automation surface into real, live faders in the UI.

## 1. BUG FIX — deck parameters and sliders never moved

Three independent causes were found and all are fixed:

1. **The agent barely wrote the visible keys.** Deck A EQ/FILTER/GAIN were touched only by rare micro-gestures; `a_gain`/`b_gain` were never automated at all. Most of the performance went to FX-rack keys that had **no faders in the UI**. The deck looked dead because, for the visible keys, it mostly was.
2. **One slider touch permanently killed the agent.** `manual_controls()` latched `_manual=True` forever, so the first drag froze the entire controller until the next prompt update.
3. **Before LET'S GO every slider snapped back.** `/api/agentic/controls` returned 409 while stopped and the 360 ms status poll immediately overwrote the user's value with the neutral controller.

### Fixes
- **Live mirrored faders.** The audio callback now publishes the *effective smoothed* deck values (`RealtimeDJMixer.status` → `a_low_db … b_gain`, reverb/echo). The UI binds every fader to what the DSP is actually applying, so faders dance with the real performance. Pure telemetry — a neutral-controls A/B run is sample-identical.
- **Phrase-locked deck breathing.** A new `_musical_motion_layer` keeps constant low-amplitude, vocal-aware EQ/filter motion running between discrete gestures (EQ-first motion is the dominant real-DJ primitive per the mix literature below). The deck is alive at all times, audibly subtle, always bounded.
- **Touch = 10 s override, agent keeps playing.** In autopilot/copilot a slider touch now creates a per-parameter bounded override (`MANUAL_OVERRIDE_HOLD_SEC = 10 s`) instead of permanent manual mode. Full manual is still available (autonomy selector or TAKE CONTROL, which now *toggles*).
- **Staged controls.** Slider input before LET'S GO is stored (`staged: true`) instead of rejected; no more snap-back. The UI additionally guards the dragged fader against poll overwrites and uses per-fader debounce (moving two faders quickly no longer drops the first value).

## 2. Contrary-motion transition layer (the rise/fall duet)

New gesture families rendered as **opposing control-rate automation lanes** over one beat window, so the two voices audibly cross:

- `AB_CONTRARY_RISE` — Deck B rises (HPF opens downward into full body, layer/gain/lows glide in, riser swells) while Deck A falls (LPF closes, gain dips, highs shelve down, lows glide out). One low-end owner at every moment.
- `AB_CONTRARY_FALL` — the mirror: A rises home while B folds up and away (reverse swell + space tail).
- `BASS_SWAP_GLIDE` — classic low-end handoff A→B with an automatically scheduled return glide (future-start lanes; no second event needed).
- `AB_BREATH` — both decks inhale together (width + space + small filter dip) and exhale into the beat.

New Director archetypes built from them: **ab_contrary_reveal** (tease → 6-beat contrary cross → pickup → DROP exactly where the arcs meet → mirrored resolve), **ab_seesaw_dialogue**, **bass_swap_glide**, **contrary_fakeout**. Landing macros (DROP/PUNCH/CLEAN RESET) retire in-flight contrary lanes so a stale ramp can never re-raise Deck B after the release.

## 3. Musical Intelligence Core (`musical_intelligence.py`)

A model-free, O(small)-per-tick awareness layer feeding the Director's critic:

| Component | What it does | Grounding |
|---|---|---|
| **PhraseClock** | Locks the 8/16/32-beat phrase grid from future novelty boundaries; landings are quantized to phrase ends when confidence ≥ 0.45 | Real DJ transitions are phrase-aligned (Kim et al., ISMIR 2020; Vande Veire & De Bie 2018) |
| **TensionField** | Scalar tension distinct from energy, charged by risers/gates/filter displacement/loudness slope, discharged by drops, with hunger + refractory | Tension ≠ energy; peaks in build, released at the drop (Yadati et al., ISMIR 2014) |
| **EnergyArc** | RISE/HOLD/FALL story with hysteresis from prompt + measured trend + future direction; biases archetype selection | Set-level energy management practice |
| **HarmonicField** | Vote-stabilized key, Camelot label, modulation evidence, Deck-B role hint via circle-of-fifths distance | Harmonic mixing / Camelot practice |
| **GestureGovernor** | Big-release pacing: 24-beat spacing (16 at max authority) as a soft critic penalty | "A drop every phrase is a drop nowhere" |

Director V5 additions: `tension_fit`, `arc_fit` and `pacing_penalty` critic components; release-heavy plans require accumulated tension, build-heavy plans require headroom, refractory suppresses instant double-drops.

## 4. Controller + UI expansion (Agentic DJ view)

- **Performance FX Rack panel — 18 new live faders**: RISER, REVERSE, SNARE RUSH, DRUM DRIVE, DOUBLETIME, TRANS GATE, GATE DIV, ECHO SEND, ECHO FEEDBACK, ECHO BEATS, RACK WET, WET WIDTH, DUCK, PUNCH, ENERGY, AIR, CLARITY, BASS TIGHT. Every lane the agent automates is now a real fader you can also grab (10 s override rule applies). All values stay inside the engine's existing bounded control surface.
- **Transition Matrix card**: DECK A / DECK B role indicators (RISE ↗ / FALL ↘ with live level meters), active transition name + progress + depth, and the Musical Mind row: phrase position bar, tension meter + hunger, release readiness, energy arc, stable key, Camelot + modulation, Deck-B role hint, drop spacing.
- **Alive faders everywhere**: custom gradient-fill tracks driven by the live value, instant value labels while dragging, double-click resets any fader to neutral.
- DJ Director grid now also reports PHRASE, TENSION, ENERGY ARC, KEY·CAMELOT, TRANSITION and OVERRIDES.

## 5. Literature review (what shaped the design)

- **Automatic DJ Transitions with Differentiable Audio Effects and GANs** — learned transitions converge on slow EQ/fader contrary motion between outgoing/incoming tracks: [arxiv.org/pdf/2110.06525](https://arxiv.org/pdf/2110.06525)
- **A Computational Analysis of Real-World DJ Mixes using Mix-To-Track Subsequence Alignment** (ISMIR 2020) — real transitions are long, phrase-aligned and EQ-dominated: [arxiv.org/pdf/2008.10267](https://arxiv.org/pdf/2008.10267)
- **Automatic Detection of Cue Points for DJ Mixing** — switch points live on structural boundaries: [arxiv.org/pdf/2007.08411](https://arxiv.org/pdf/2007.08411)
- **Detecting Drops in EDM** (ISMIR 2014) — the build→drop tension grammar the TensionField implements: [archives.ismir.net/ismir2014/paper/000297.pdf](https://archives.ismir.net/ismir2014/paper/000297.pdf)
- **Full-automatic DJ mixing with optimal tempo adjustment** (ISMIR 2009) — beat/tempo alignment and listener discomfort: [archives.ismir.net/ismir2009/paper/000043.pdf](https://archives.ismir.net/ismir2009/paper/000043.pdf)
- **Temporal Considerations in DJ Mix IR and Generation** (2025 survey of the field): [drops.dagstuhl.de — LIPIcs.TIME.2025.20](https://drops.dagstuhl.de/storage/00lipics/lipics-vol355-time2025/LIPIcs.TIME.2025.20/LIPIcs.TIME.2025.20.pdf)
- Also consulted: Raveform metrical/functional EDM structure dataset ([transactions.ismir.net](https://transactions.ismir.net/articles/288/files/69e5eecf0612b.pdf)), novelty-based structure segmentation ([arxiv 2309.02243](https://arxiv.org/pdf/2309.02243)), graph-cut crossfades ([arxiv 2301.13380](https://arxiv.org/pdf/2301.13380)), DJ StructFreak (ISMIR 2023 LBD).

## 6. Validation (all 32 checks pass)

- Deck keys breathe continuously (a_high span ≈ 0.75 dB, filter micro-motion) while staying bounded; agent never enters manual from a touch; override applies, holds, expires, agent resumes.
- `AB_CONTRARY_RISE` schedules opposing lanes (A-gain ↓ / B-layer ↑ / A-filter → LP / B-lows ↑), the scheduler audibly crosses them (a_gain 1.00→0.95 while b_layer 0.00→0.24 over 8 beats), DROP retires the lanes and restores Deck A.
- Director offers contrary/bass-swap archetypes on both rising and falling arcs, quantizes the landing to the phrase boundary (target beat 42 for a 16-beat grid), and reports `tension_fit`/`arc_fit`.
- TensionField charges under a riser and discharges into refractory on release; PhraseClock locks 16-beat grids; Camelot mapping and circle-of-fifths compatibility verified (Am↔C = 0.90, C↔F# = 0.05).
- Engine mixer publishes effective deck values; telemetry is sample-identical to the previous mixer output for the same input/controls; effective values track control targets through the smoothers.

Windows Spotify routing, physical WASAPI output and hardware xrun behavior still require validation on the target machine.
