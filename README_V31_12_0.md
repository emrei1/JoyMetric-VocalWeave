# JoyMetric v31.12.0 — FullRemix

The brief: at creativity max the app must sound like a **real remix**, not a song with FX seasoning. This release implements the remix-production formula from the pro documentation as a persistent arrangement layer.

## What the remix literature says (researched for this release)

- A remix is defined by a **new drum groove played under/against the original** — "play your drum pattern against it and tweak until it feels natural" ([Crossfader — Remixing for DJs](https://wearecrossfader.co.uk/blog/remixing-for-djs/), [DJ TechTools — live remix performance](https://djtechtools.com/amp/2015/09/28/turn-a-song-into-a-live-remix-performance-in-ableton-maschine/))
- **Sidechain glue keyed from the kick** — the bass/music ducks slightly on every kick, "giving your track that pumping effect" ([iZotope — sidechain techniques](https://www.izotope.com/en/learn/11-creative-sidechain-compression-techniques.html), [Sonarworks](https://www.sonarworks.com/blog/learn/sidechain-compression))
- **Gate remixing / loop resequencing** — the source keeps running while fragments are selectively opened and re-ordered ([Liveschool — Gate Remixing](https://liveschool.net/blog/ableton-live-tutorial-chopping-drums-with-gates/), [SOS — Recording & Remixing in Live](https://www.soundonsound.com/techniques/recording-remixing-ableton-live))
- **Arrangement**: duplicate/mute sections into an energy arc with short breaks and one strong drop ([Loopmasters — Remixing Tips](https://www.loopmasters.com/articles/3499-Remixing-Tips-for-Live-Part-1-))

## 1. Engine: the Redrum layer (`fx_redrum`, `fx_redrum_pattern`)

A synthesized kick/clap groove played under the live song:
- **Kick**: closed-form pitch-swept sine (139→47 Hz) + HP click transient, following one of four 16-step grids: **FLOOR** (four-on-the-floor + pickup ghost), **HALF** (halftime), **BREAK** (breakbeat), **UPBEAT** (offbeat bounce). Clap/snare band-noise on the backbeats.
- **Kick-keyed program duck**: every kick momentarily ducks the whole program (≤26 %) — the sidechain-from-the-kick production staple — so the new groove *pushes* the song instead of stacking on it.
- Beat-locked via the mixer's absolute sample clock; fully vectorized; included in the FX headroom budget; **sample-identical to legacy output at level 0** (verified).

## 2. Agent: the RemixArranger (persistent, phrase-aligned sections)

Above ~0.55 creativity (scaling to full at 1.0) — or whenever the prompt says *full remix / redrum / new beat* — the arranger runs a section cycle the way a producer arranges a remix, holding each state for a whole phrase-aligned section instead of firing decaying two-beat gestures:

| Section | Hold | Character |
|---|---|---|
| **GROOVE+** | 16 beats | full redrum + pump 0.30 + resident Deck-B motif (layer ~0.30) + light slicer |
| **ROLL** | 8 | gate-remix rearrangement: slicer 0.58 FILL mode, B fx up |
| **STRIP** | 8 | floor removed (bass-cut 0.78), riser lean, everything held back |
| **DROP+** | 16 | full groove + impact one-shot + pump 0.40, bass restored |
| **HALF** | 8 | halftime pattern, harmonic Deck-B bed, space |

Transitions follow MI grammar: STRIP→DROP+ always; readiness + drop-pacing gate the STRIP entry; high vocals steer to HALF/GROOVE+; arc FALL favors HALF after a drop. Section entries re-lock the pump phase, capture the Deck-B motif if the deck is empty, and are logged to the performance history (`REMIX_GROOVE_PLUS`, …). Discrete Director events (builds, spinbacks, drops) still fire on top; automation lanes and manual overrides always win their keys. `CLEAN_RESET` parks the arranger for a genuinely clean bar. Prompt opt-out: *"no remix / original drums"*.

- Policy: `remix_intensity` (0 below 0.55 creativity → 0.85 at max; keyword-forced ≥0.78; opt-out 0) and genre-mapped `redrum_pattern` (club→FLOOR, hip-hop→HALF, dnb→BREAK, + halftime/breakbeat/offbeat keywords).

## 3. UI

- FX Rack grew to **23 live faders** (+ REDRUM, R-PATTERN with FLOOR/HALF/BREAK/UPBEAT readout).
- Musical Mind grid: **REMIX MODE** (on/off · intensity · pattern) and **REMIX SECTION** (name + beats left) cells; DJ Director grid gained a REMIX cell.

## 4. Validation — 82/82 checks green across three suites

- **FullRemix suite (21)**: four-floor kicks land exactly on the beat (silence-input peak/trough → ∞), bounded, status published, neutral regression sample-identical; policy scaling (max→0.85, 0.35→0, keyword force, opt-out, genre patterns); arranger enables at max, cycles sections (GROOVE+→ROLL…), redrum persistently audible (100 % of ticks above threshold), resident B layer 0.28 + pump 0.28 held, history entries, status block, silent at low creativity, keyword override works, CLEAN_RESET clean bar.
- **StageCraft suite (29)** and **v31.10 suite (32)**: all still green.

Ear-validation of the Windows Spotify → WASAPI route remains on the target machine.
