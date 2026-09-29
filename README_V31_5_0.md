# JoyMetric v31.5.0 — Performance FX Rack

v31.5 upgrades the Agentic DJ from a collection of individual FX gestures into a controller-style realtime FX rack while preserving the v31.4 15-second lookahead, delayed audible timeline, Spotify route, and v30.4.47 LowEndIntegrity master chain.

## What changed

### 1. Professional controller-style FX topology
The realtime DJ mixer now contains a dedicated `ProfessionalFXRack` before the existing JoyMetric master DSP.

Signal concept:

`Delayed Deck A / Loop Deck B -> deck EQ/filter -> source-derived performance layers -> Performance FX Rack -> one-shot impact -> JoyMetric RealtimeDSP -> LowEndIntegrity / safety / true-peak chain -> Audio Out`

The rack is native to JoyMetric. It does not embed Mixxx or another DJ application, so the proven Windows routing path remains under JoyMetric's control.

### 2. Tempo-synchronised SEND / RETURN beat echo
The rack has its own preallocated stereo delay bus:
- BPM-synchronised delay time
- prompt/controller-selectable musical beat division
- filtered return
- bounded feedback
- ping-pong stereo return
- wet-bus width
- program-dependent return ducking
- SEND and RETURN are separate

This allows a professional post-fader-style transition gesture: build sends audio into the delay, then SEND can fall to zero on the release while the feedback tail continues naturally.

### 3. Low-end-safe TRANS / GATE
A tempo-synchronised rhythmic gate is applied only to content above roughly 220 Hz. The sub and kick body stay continuous, avoiding the cheap full-band chopping that can destroy bass-heavy hip-hop/trap material.

The agent can move between 2x and 4x subdivisions during a build, which is useful for perceived DnB acceleration without time-stretching the whole Spotify signal.

### 4. Prompt-conditioned controller actions
The neural action bank now includes:
- `FX_BEAT_ECHO`
- `FX_TRANS_GATE`

Alongside the existing:
- `FX_RISER`
- `FX_REVERSE_SWELL`
- `FX_SNARE_RUSH`
- `FX_IMPACT`
- `FX_ECHO_BLOOM`

Direct prompt language such as `post-fader echo out`, `ping-pong echo`, `gated build`, `chopped rhythm`, `wide transition`, or `stereo FX` changes the compiled FX profile. CLAP still evaluates the live musical context before creative actions are allowed.

### 5. DnB morph now uses a macro FX rack
For a prompt such as:

`drum n bass like speed and drums a banger, smooth professional, strong sound fx, post-fader echo out and gated build`

The staged morph is approximately:

1. **FOUNDATION** — bass tighten + source-derived drum punch; FX rack mostly dry.
2. **PROPULSION** — double-time articulation enters; low-level trans-gate and echo bus begin.
3. **BUILD** — riser + reverse swell + snare rush + increasing trans-gate + beat-echo SEND + wider wet bus.
4. **RELEASE** — build layers clear; gate clears; echo SEND closes while the return tail remains; one bounded cinematic impact receives momentary program ducking for headroom.
5. **DNB FEEL LOCK** — source-derived fast drum feel stays; FX rack retreats instead of masking the song.

The goal is to morph an unrelated 80–95 BPM trap/hip-hop source toward DnB energy without simply forcing the full song to 174 BPM.

## Realtime safety

The Performance FX Rack:
- allocates its audio buffers at initialization
- performs no model calls in the audio callback
- performs no file/network/plugin discovery in the audio callback
- uses bounded feedback saturation
- limits trans-gate authority
- preserves low-end continuity in the gate
- ducks echo returns against program energy
- applies impact headroom by briefly ducking the program rather than only summing a louder hit

The existing `RealtimeDSP`, `MasterSafetyCompressor`, `SourceFidelityGuard`, and `LookaheadPeakLimiter` are unchanged from v31.4 at AST level.

## Important validation boundary

Linux validation can exercise the numeric realtime DSP graph but cannot execute the actual Windows WASAPI / JoyMetric Virtual Driver / Spotify route. Final listening validation must therefore happen on the target Windows machine.
