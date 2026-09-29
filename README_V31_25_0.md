# JoyMetric Workstation UI v31.25.0 — "GenMind"

"Let's generate the synths ourselves with generative AI, from the song and the melody that is
playing right now — very solidly. Drums stay as they are; the rack can stay but smaller; the dominant
part is the generated one." — done, symbolically, so the harmony is guaranteed and the sound is ours.

## Why symbolic, not audio diffusion

The v31.21/22 experiment (Stable Audio, audio-to-audio) produced material that was only loosely
related to the record. A symbolic generator writes NOTES: every note can be checked against the
record's chords and key before it exists, it can be timed to the beat grid sample-exactly, and it is
rendered by instruments we already choose for the record and the prompt (CLAP) with GrooveNet's
level, tilt and micro-timing. Nothing unrelated can reach the speakers.

## The generator: Anticipatory Music Transformer (Stanford CRFM, Thickstun et al. 2023)

`workers/amt_persistent_server.py` — `music-small-800k` (128 M parameters, trained on 180 k Lakh MIDI
files), fp16 on the GPU next to the Stable Audio worker, port 8769, launched and watched like the other
workers. Its native ability is *anticipation*: it conditions on events that will happen in the future.
The DJ uses exactly that:

- **controls** = the record's next two bars: the **melody** (yin on the harmonic component of the
  lookahead, one pitch per sixteenth, merged into notes) and the **chords** (the planner's per-beat
  chords, as chord tones);
- **prompt** = our own previous generated part, so the new part continues it (register, style);
- **one instrument**, and — the robustness core — the note token is masked to **the pitch classes
  allowed at that moment** (chord tones on the beats, the key in between) and to a register. The model
  keeps rhythm, contour and phrasing; it cannot write a wrong note.

Measured: a 2-bar part in 0.5–1.2 s, 100 % of notes allowed by construction.

## The composer (`gen_composer.py`)

For every generated layer the planner schedules (PEAK main voice, COLOR touch, BUILD figure, BREAK
takeover voice): cut the 2-bar window from the 16 s lookahead ~10 s before it plays → melody + chords →
two candidates from the worker → the one whose note count is closest to the creativity target and whose
rhythm agrees best with **GrooveNet's placement probabilities** wins → starts quantised to the sixteenth
grid with the record's **micro-timing**, velocities from GrooveNet, durations bounded → rendered by the
rack as a *score* on the CLAP-selected instrument of the role (pad or transient) with GrooveNet's level
and tilt → scheduled like every other layer; the in-key gate (note histogram vs the record's pitch
classes) and the play-time gate still apply. If the worker is down or slow, the same layer falls back to
the rack part: never silence, never an unrelated sound.

## Policy (v31.25)

Drums: unchanged (DrumMind + GrooveNet swing). Rack: only a soft bed on the first PEAK block of each
window, the BUILD swell, the DROP pad, the TRANSITION swell, and fades. Everything else melodic is
generated. Levels stay lowkey (generated 0.28–0.55× the record's mid/high RMS at creativity 1.0).

## Tests (validate_v31_25.py, 14)

worker health / harmony-locked generation / prompt continuity; melody extraction (8-note line, < 3 s);
composer with a stub worker (controls, candidate choice, rendering, quantisation + micro-timing,
continuity); rack score rendering; planner policy; agent routing and fallback.
