# JoyMetric Workstation UI v31.26.0 — "LineMind"

The user's design, engineered: **find the lines that play in the record right now, one by one; let a
neural network we trained write an extra melody that fits them, the genre and the DJ prompt; play it
on a synth we generate ourselves; when needed, pull the record's own line back and put ours in its
place. Never bells, guitar or drum-like sounds.**

## 1. The lines, one by one — `line_finder.py`

Every 2-bar window of the 16 s lookahead (~10 s before it plays) is transcribed with Spotify's **Basic
Pitch** (polyphonic note transcription CNN, ONNX, CPU: 6 s of audio in ~1.3 s) and split on the beat
grid into **bass line** (lowest sounding note), **top line** (lead / vocal, the highest), **inner
voices** (pitch-class activations of the rest) and the **drum grid** (GrooveNet's kick / snare / hat).
The result is a symbolic picture of the record and the per-sixteenth context vector MelodyNet was
trained on (bass pc · inner pcs · top pc · drums · bar position · key · tempo = 58 numbers per cell).

## 2. Our own generative model — `melody_net.py` (numpy runtime), trained by `melody_train.py`

**Data:** the Lakh MIDI dataset (`lmd_matched`, real multi-track songs; 12 000 songs drawn at random,
one per track id), one example per melodic track and 2-bar window: the *context* of all the other
tracks (as above, computed from the MIDI exactly like LineFinder computes it from audio) → the *target*
part, that track's notes per sixteenth (rest / hold / pitch 48–84), conditioned on the target's
instrument family (10) and the song's genre (tagtraum CD2, 13 classes + unknown).

**Model:** context (58) + condition (24) → Linear 160 → BiGRU 160 × 2 → per-cell encoder state (320);
decoder GRU 256 over the 32 cells, input = encoder state ⊕ embedding of the previous token → 39-way
softmax. ~1 M parameters, trained on the GPU, exported to npz; the numpy runtime writes a 2-bar part in
< 150 ms and can also **score** any candidate part (mean log-likelihood).

**Live masks (the robustness core):** per cell only the chord tones (on the beats) or chord + key
(between), a register per role, **consonance against the record's top line** at every onset (unison /
3rds / 4th / 5th / 6ths / octave), "hold" only inside a note.

## 3. Genre — `genre_probe.py`

Once per record the CLAP critic scores the record's block against one prompt per genre (zero-shot);
the DJ prompt's genre wins when it names one. The genre conditions MelodyNet, the instrument palette
(hip-hop / r&b: warm pads, electric piano, soft plucks; house / techno: analog pads and tight plucks;
ambient: wide pads, strings, choir) and the density / register of the part.

## 4. Writing and choosing — `gen_composer.py` v2

MelodyNet samples three parts; the Anticipatory Music Transformer worker (when up) adds two, given the
same lines as anticipated controls and our previous part as its prompt. Candidates are ranked by
MelodyNet's likelihood (2×) + GrooveNet placement fit (1.2×) + closeness to the creativity density
target − large leaps. The winner is quantised to the sixteenth grid with the record's micro-timing,
velocities from GrooveNet, and rendered by the rack on the CLAP-selected **synth** (pads: warm / wide /
bright / hollow / strings / choir / e-piano; transients: soft / tight / bright plucks, e-piano) with
GrooveNet's level and tilt. The in-key and play-time gates still apply.

## 5. Taking the record's line out — `LineDuck` (engine)

When the planner marks a generated part `replace_line` (the third PEAK block of a window at creativity
≥ 0.75, every BREAK), the record's top-line notes inside that span are scheduled as frame-exact
segments: three deep narrow cuts (−30 dB, Q 12) follow the note's fundamental and its 2nd / 3rd
harmonics with 15 ms ramps, so only that line steps back and ours takes its place; the rest of the mix
is untouched (exact reconstruction outside the segments).

## Tests

`validate_v31_26.py` (14): LineFinder voices / context / audio; MelodyNet parity, held-out metrics vs
unigram + bigram, masked sampling, scoring; LineDuck depth and transparency; composer v2 end-to-end with
stubs; planner `replace_line`; agent line-duck scheduling; genre probe. `validate_v31_25.py` (14),
`validate_v31_23.py` (79), `validate_v31_24.py` (14) and the older suites stay green.

## v31.26.1 — AI-only melodies, max-creativity presence, smoother transitions (the user's listen)

- **Only generated melodic parts remain.** The rack's own pads / plucks / arps (beds, build swells, break
  pads, transition swells, drop pads, fades) are gone from the planner; every melodic layer is written by
  MelodyNet (the AMT worker adds candidates). The freeze (sampler) and the AI drummer stay. A generated
  layer that cannot be written (no window yet, no model) is dropped, not replaced by a rack part
  (`JOY_GEN_FALLBACK=1` re-enables the fallback).
- **Max creativity:** from creativity 0.8 the generated voice gains up to +0.12 level, appears on two extra
  blocks per 16 s (COLOR on the 6th and 7th block), replaces the record's line on two PEAK blocks, and
  the density target rises 30 %.
- **Smoother transitions:** a note that starts inside a block is rendered to its full length (the deck
  overlap-adds; no more 0.8 s truncation of long pad notes at block edges); the level multiplier eases
  between blocks (EMA, ≤ 3 dB per step); the window's part is written once and shared by both blocks
  (window cache: consistent, half the CPU); Basic Pitch runs on 2 ONNX threads and every analysis thread
  (transcription, GrooveNet, CLAP selection, genre probe, composer) runs below normal priority so the
  audio callback keeps its cores.
- **Final MelodyNet** (12 000 Lakh songs, 428 595 training examples, 8 epochs, 254 s on the GPU):
  held-out NLL 0.937 (bigram 1.73, unigram 2.04), token accuracy 0.739, exact-pitch accuracy 0.350;
  samples: in-key 0.94 (real parts 0.92), consonance against the top line 0.85 (real 0.83), 9.2 onsets
  per 2 bars (real 9.5).
- Known limit: heavy background jobs on the same machine (corpus extraction, training) do cause frame
  drops; they are offline steps and must not run while the DJ plays.
