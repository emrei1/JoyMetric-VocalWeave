# JoyMetric Workstation UI v31.27.0 — "SongMind"

"The melodies are bad (dak dak dak). We need a far stronger model; rebuild the architecture; the model
must be very aware of the song." — rebuilt from the ground up.

## What was wrong

MelodyNet (v31.26) saw the record only as pitch-class summaries per sixteenth and was trained on any
track with 4–24 onsets — comping tracks (repeated chord stabs) included. With the harmony masks on top,
it fell into repeated single pitches on short plucks: "dak dak dak".

## The new architecture

### 1. The song, in full resolution — `line_finder.py` (`analyse_v3`)

Four bars of the record (the two bars just before the block and the two that will play) are transcribed
(Basic Pitch) and turned into 64 cells × 135 numbers: **bass MIDI pitch** (rest + 40 pitches from 28),
**top-line MIDI pitch** (rest + 40 from 48), **inner voices** as pitch classes + count, **onset flags**
for bass / top / inner, kick / snare / hat strengths (GrooveNet), cell-in-bar (16), bar-in-window (4),
key pitch classes, tempo and per-cell **loudness**. The model sees what the bass does, what the lead
sings, what the chords are, where the drums hit, how loud each moment is, and where in the phrase it is.

### 2. Melodic targets only — `melody_corpus3.py`

Lakh MIDI (`lmd_matched`, all ~31 000 matched songs), 4-bar windows with a 2-bar hop. A track is a
target only when it behaves like a melody: monophonic (< 15 % of cells with two notes), 3–24 onsets,
≥ 4 distinct pitches, at most half of the onsets repeating the previous pitch, mean interval 1–7
semitones, mean pitch ≥ 55, not a bass or drum program. Condition: instrument family (10), tagtraum genre
(14), **density bucket** (5), **register bucket** (3), **relation to the top line** (counter / harmony /
response, measured from the data).

### 3. The model — `songmind_model.py`

Transformer encoder (4 layers, d 256, 8 heads) over the 64 song cells + one condition token; Transformer
decoder (4 layers) writing the part's 64 tokens (rest / hold / pitch 48–84) with causal self-attention and
**cross-attention to the song**. 5.4 M parameters, trained on the GPU (`songmind_train.py`, mixed
precision), served on the GPU inside the AMT worker (`/songmind`, `/songmind_score`).

Live sampling (`write`): allowed pitch classes per cell (chord tones on the beats, chord + key between,
the record's bass note always legal), a register per role, consonance against the record's top line at
onsets, hold only inside a note, a **repetition penalty** on the previous onset pitch, a **minimum note
length** per role, nucleus sampling with a creativity-driven temperature, K parts per call. **Our previous
two bars are forced as the prefix**, so the new bars continue the phrase instead of restarting it.

### 4. Writing and choosing — `gen_composer.py` v3

K = 4 parts → ranked by SongMind's log-likelihood + GrooveNet placement fit + a melodic post-filter
(repeated-pitch ratio, distinct pitches, density target from creativity). The winner's last two bars are
the block's notes: quantised with the record's micro-timing, GrooveNet velocities, full note lengths
(overlap-added across blocks), the CLAP-selected synth, GrooveNet level and tilt, the in-key and play-time
gates. Fallback: the v31.26 numpy GRU when the worker is down.

## Measured

(see `V31_27_0_TESTS.txt` — held-out NLL vs unigram / bigram, exact-pitch accuracy, sample repeat ratio
vs real parts, in-key and consonance rates, onsets per 4 bars, worker latency, live in-key rate)
