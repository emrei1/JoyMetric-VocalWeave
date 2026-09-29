# JoyMetric v31.21.0 — SynthWeave (with v31.20 DrumFront)

Briefs: "the generated drums should show themselves more at creativity max; in the A/B loops the drums must not loop" → v31.20. "Add synths and melodies that fit, with Stable Audio" → v31.21.

## v31.20 DrumFront

- **Presence.** Creativity now buys the AI drummer three things: level (`drum_presence` raises the record-relative level target and its ceiling), density (the DrumMind density target grows with creativity) and adventurousness (sampling temperature 0.85 → 1.15). Measured on a club record: the drummer's own hat band is **4.2×** and its kick band **4.3×** louder at creativity 1 than at creativity 0, no clipping. The clarity gate is softened for the drummer on material that clearly has a grid.
- **Drum-free A/B loops.** While the AI drummer plays, Deck B runs through `DeDrum`: a two-band transient designer in cut mode (fast/slow envelope ratio below and above 4 kHz), a kick-body low cut and a top trim. On a drum loop: transient energy **−12 dB**, kick body **−18.7 dB**, hats **−13.4 dB**; a sustained pad changes by −0.7 dB; 0.34 ms per block. The loops keep the record's chords, bass line, vocals and textures; the drums come from the drummer alone.

## v31.21 SynthWeave — Stable Audio synths and melodies that fit

The local Stable Audio worker (RTX 5070) is an audio-to-audio editor. Instead of asking it for music from nothing (which would never sit in the record's key, tempo or phrase), SynthWeave sends it **the captured Deck-B loop itself as init audio** with a prompt for a melodic synth part — arpeggio, pad chords, lead melody or plucks, chosen from vocal presence, energy arc and rotation — in the record's key (from the stabilised key estimate) and tempo, with a negative prompt that pushes drums and vocals out. The result inherits the record's harmony and phrase length, comes back sample-aligned with the loop (post: high-pass 140 Hz, level matched to the loop, soft limit, seamless seam), and Deck B crossfades from the record loop to the synth loop on the grid (`b_ai_mix`) in sections that leave room for a melodic layer (HALF, STRIP, BASSLINE, GROOVE+, STAB_GROOVE, ROLL, PERC; softer under vocals), raising the deck and biasing its co-mix role toward a harmonic bed. Fully asynchronous — the audio thread never waits for the GPU. Measured live: a 2-bar loop → synth arpeggio in **9.9 s**, aligned length, sub band 0.08×, mids 2.2×, level matched.

## UI

Agentic panel: SYNTH AI (state · kind), LOOPS (DRUM-FREE · SYNTH %), DRUM AI, DRUM KIT chips; `synth_ai` and `drum_ai` in the status JSON.

## Validation

See `V31_21_0_TESTS.txt`.
