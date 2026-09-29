# JoyMetric Workstation UI v31.23.0 — "RackMind"

The Stable Audio composer of v31.21/22 is retired (opt-in with `JOY_SYNTH_AI=1`). Synth parts now come
from a **real DSP synthesizer rack** that plays the record's own upcoming chords, driven by a
**16 s / 8-block planner** that decides the DJ character of every 2 s block from its neighbours.

## The algorithm (the user's design, engineered)

1. **16 s lookahead, 8 blocks of 2 s.** `JOY_AGENTIC_LOOKAHEAD_SEC=16`. Blocks are absolute stream
   frames (block k = [k·2 s, (k+1)·2 s)); the capture and render counters index the same samples, so a
   plan made on captured audio lands frame-exactly when those samples play ~16 s later.
2. **Describe.** As soon as a block is completely captured: RMS, low/mid/high band energy, spectral
   centroid, centre ("vocal-ish") energy 300–3500 Hz, onset density (spectral-flux peaks), tonalness,
   the 20 ms envelope of its first 1.2 s (drop-onset location), and **per-beat chords** (tonal chroma →
   24 triad templates + bass-note bonus + key prior → bar-line-aware Viterbi).
3. **Decide with both neighbours.** Block k is planned when block k+1 is described too; the decision
   sees the plan of k−1 (continuity) and the descriptors of k−1, k, k+1 (anticipation):

   | character | when | what |
   |---|---|---|
   | TRANSITION | a new record starts inside the block | our pad swells first from the change frame, the record returns under it over ~2 s |
   | DROP | low band ≥ 1.5× (or level ≥ 1.35× with the floor rising), rhythmic, a clear hit | the drop's first hit is **frozen**: rolled for 4 beats (2 below creativity 0.7) from the snapped downbeat with a tightening pattern, rising high-pass and a silent gap; the record steps aside (full-band frame-scheduled duck) and returns exactly on the beat after the gap; pad + drummer carry the rhythm |
   | BUILD | the next block is a drop, or a riser (centroid and highs rising, floor thinning) | pad swell with an opening filter, arp opening its filter, noise-riser and snare-rush bias into the drop, drum presence up |
   | BREAK | energy falls (level ≤ 0.72×, floor ≤ 0.65×) | the record's melody is taken over: mid/high centre duck (floor kept) while the pad plays at 0.6–1.0× the record |
   | FLOW | a lead vocal (centre energy + the analyser's vocal estimate) | a soft pad underneath, nothing takes the front |
   | GROOVE | steady material | pad (legato across blocks) + arp / pluck / lead rotation; one **melody takeover** per 8 blocks at creativity ≥ 0.6 |

   Creativity scales everything: below 0.35 one soft pad every 4th block and no takeovers; at 1.0 a
   continuous pad at 0.9× the record's mid/high level, a rotating second layer, freezes and takeovers.
4. **Render.** Every layer is rendered by the rack on the **absolute beat grid** (block boundaries are
   inaudible: a 16th arp keeps its step count across blocks, a pad continues without re-attack) and
   scheduled at an absolute render frame in the SynthDeck; takeover / freeze curves are scheduled in the
   mixer's `TakeoverLane` (trapezoids: full amount at the start, held to the end, ramps outside).
5. **Bias.** While a block plays, its character shapes the live lanes: drum presence ×0.7–1.25, pump
   floor, riser rising across a BUILD block, snare rush in its last beat.

## SynthRack (`synth_rack.py`)

PolyBLEP saw / pulse oscillators with unison detune spread and a sine sub, ADSR, a paraphonic resonant
low-pass modulated by envelope + LFO + "build", chorus, tempo-synced ping-pong delay, plate-style
reverb, beat-locked sidechain pump; genre presets from the DJ prompt (house/techno: offbeat stabs, more
pump, brighter; hip-hop/r&b: trap stabs, warmer; ambient: long attacks, no pump). Level is set to a
target RMS relative to the record's own mid/high RMS with a soft ceiling. Per 2 s layer: 100–210 ms of
CPU in a background thread. Chord estimation: 8 beats in ~30 ms.

The rack's parts join the programme **after the residual cap** (which pinned the v31.22 synths to
~20 % of the record) and before the bus preconditioner and limiter. Harmonic gates use the histogram of
the notes actually played, not a smeared synth spectrum.

## Freeze (`render_freeze`)

Slices retrigger the drop hit: 1/2 1/2 | 1/4 ×4 | 1/8 ×8 | 1/12 ×9, then a 1/4-beat silent gap. 3 ms
grain fades, amplitude 0.82→1.0, a rising high-pass (40 Hz → 1.4 kHz), level-matched to the hit. The
programme's full-band duck ramps in 20 ms before the hit and out 12 ms after the gap.

## Measured

- v31.23 suite: 42/42 (rack per kind: length, level ±8 %, all notes chord tones, 16th-grid onsets,
  continuation; freeze; chords Am F C G per beat; planner DROP/BUILD/GROOVE/TRANSITION/FLOW and
  creativity 0.2; lane ramps; duck −12 dB centre / bass within 1.5 dB / sides −5 dB; mixer keeps the
  rack level over a quiet record; frame-scheduled freeze; agent end-to-end with a 16 s ring).
- Older suites unchanged (v31.10–v31.22 and the beat clock).

## v31.23.1 — sparse and lowkey (the user's second listen)

"The synth plays through the whole song": it must not. Silence is now the default state of the rack.
It speaks only where a DJ would reach for a synth:

- **TRANSITION** (a new record), **BUILD** (into a drop), **DROP** (the freeze + a pad), **BREAK**
  (energy falls: melody takeover under a pad) — all at lower levels than v31.23.0;
- **PEAK**: the record's highs (top energy band, sustained over two blocks) get a lowkey pad — but only
  in the first 4 s of every 16 s, with a stab layer on the second block at high creativity, and the
  one-bar melody takeover at most once per 32 s (creativity ≥ 0.75);
- **GROOVE** (everything else) carries nothing; a pad that is already sounding gets one softer block
  to fade instead of being cut; a vocal block (FLOW) never starts a synth.

Levels: pads 0.18–0.5× the record's mid/high RMS (0.35–0.9 before), stabs ≤ 0.18×, no lead, arps only
inside a BUILD. Pads are triads, darker (600 Hz), 0.8 s swell and 1.2 s release; chords are held for
at least half a bar (no beat-by-beat re-harmonisation). Drops must be rare and unmistakable (low band
≥ 1.6×, sustained lower energy before, the loudest block in view, ≥ 32 s apart).

The AI drummer is lowkey too: presence × 0.62 (`JOY_DRUM_LOWKEY`), kick / hat / snare amounts × 0.8,
and the block bias never lifts it above 1.05 (1.25 before).

## v31.23.2 — the instrument follows the record AND the prompt; a real palette

"Synth" means any instrument now. The rack has instrument engines: analog subtractive (pads, plucks,
brass), 2-operator FM (electric piano, bells, marimba), Karplus–Strong plucked strings (guitar, harp),
additive drawbars (organ), and ensemble + formant voicing (strings, choir). Pad role: warm / wide /
bright / hollow / glass analog pads, strings, choir, organ, electric piano (9). Transient role: soft /
tight / bright synth plucks, electric piano, bells, marimba, guitar, harp, brass, organ (10).

**Which one plays is measured, not guessed.** Once per record / prompt (`timbre_select.py`, background
thread, throttled so the CPU critic never starves) every candidate is rendered for 2 s on the record's
current chords and embedded by the CLAP worker together with the DJ prompt. Score = 0.6·z(similarity to
the prompt text) + 0.4·z(cosine to the record's own block). The best instrument plays; at creativity
≥ 0.8 the top three rotate 3:2:1 between windows (variety that still fits). A busy or offline worker
leaves the defaults (warm pad / soft pluck).

**The prompt shapes the sound directly** (`prompt_profile`): dark / warm / vintage → darker filter;
bright / crisp / euphoric → brighter; dreamy / floating / space / wide → wider, more chorus and reverb;
dry / tight / punchy → less; aggressive / hard / raise energy → more pump, resonance, gentle drive,
+20 % level, faster envelopes; restrained / subtle / smooth / lowkey → −20 % level, slower envelopes;
"808" / "sub" / "keep the bass" → no sub oscillator and the pad high-passed at 180 Hz (the record's low
end stays alone); "vocal" → −30 % under vocals.

**Creativity max uses more**: PEAK windows grow from 2 blocks (4 s) per 16 s to 3 at creativity ≥ 0.75
and 4 (8 s) at ≥ 0.9, stabs on two of them, and one short COLOR touch per 16 s outside the highs.
Everything else is still silent.

Suite: 79/79 (instruments per role: in tune, in level, in time, a 163–5065 Hz centroid palette; profile
axes; 808-clean −68 dB low share; selector ranking / rotation / failure; planner windows).
