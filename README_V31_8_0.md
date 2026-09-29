# JoyMetric v31.8.0 — DJ Director V3 / Parameterized Concert Controller

This branch upgrades v31.7 from fixed gesture grammar into a no-fine-tune planning architecture designed to reduce repetitive DJ behavior while preserving the proven realtime audio path.

## Core architecture

Spotify PCM → 15 s future lookahead → lightweight bar-scale descriptors → global CLAP semantic observer → **DJ Director V3** → 5 candidate choreographies → multi-objective critic → selected parameterized action chunk → 55 ms control-rate automation → existing Concert/FX DSP → unchanged v31.7 continuity + LowEndIntegrity master.

### DJ Director V3

The director no longer picks one fixed macro chain. For each strong future boundary it generates five alternative 8–16-bar-ish performance ideas and scores them using:

- structural alignment and future novelty,
- prompt/style fit,
- action diversity,
- requested action density,
- smoothness / one-primary-gesture-per-beat,
- vocal/destructive-action risk,
- repetition against recent performance memory,
- low-authority support from the CLAP semantic action decision.

The recent performance history is part of every replan. Repeating the same family raises a repetition penalty, allowing a different transition vocabulary to win even on a similar musical boundary.

### Parameterized Concert Controller V3

There are 18 parameterized gesture families:

TENSION, FILTER_SWEEP, TRANS_GATE, DRUM_FILL, ROLL_ACCEL, ROLL_HALF, ROLL_QUARTER, ECHO_HALF, ECHO_ONE, ECHO_OUT, WASH_OUT, SPACE_BREAK, GROOVE_LIFT, PUNCH_IN, DROP, CLEAN_RESET, RISER, REVERSE_SWELL.

Each gesture carries continuous parameters such as depth, wet/send, feedback, width, ducking, loop size, acceleration, impact strength and duration. Therefore the effective controller space is continuous rather than a fixed list of 36 identical presets.

Examples:

- `FILTER_SWEEP(depth=.18, duration=3.1 beats, curve=smoothstep, width=.42)`
- `ECHO_OUT(send=.57, feedback=.43, width=.71, tail=2.0 beats)`
- `ROLL_ACCEL(start=.5 beat, end=.25 beat, xfade=.29, acceleration=.78)`

### Control-rate automation

Tonal and wet controls no longer need to jump to a preset value at an action boundary. The agent schedules beat-relative automation lanes for filter, riser, reverse swell, width, echo send/feedback, gate, drum drive, snare rush, punch, energy and related controls. These envelopes update at the existing ~55 ms cheap scheduler cadence and run no FFT, CLAP, file I/O or audio processing.

Discrete events such as loop capture/release and impact sequence remain handled by the realtime native engine.

## Realtime safety

`realtime_native_engine.py` is byte-identical to v31.7.0. Therefore the v31.7 fixes remain intact:

- 2048-frame agentic processing quantum,
- 4096-frame physical-output safety buffer,
- graceful Loop Deck release before buffer retirement,
- continuity FIFO and short dropout concealment,
- headphone/WASAPI discovery fixes inherited from v31.5.1,
- existing LowEndIntegrity / CrackleGuard / limiter chain.

The new Director runs outside the audio callback and adds no DSP work to the realtime mixer.

## Validation boundary

Python and JavaScript syntax, Director candidate generation, memory-driven plan variation, parameterized controller execution and a synthetic native mixer run were validated in the packaging environment. Windows Spotify/WASAPI hardware cannot be executed here; final device and audible musical validation must be performed on Windows.
