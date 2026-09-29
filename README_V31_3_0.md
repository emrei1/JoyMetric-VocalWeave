# JoyMetric v31.3.0 — Professional Lookahead Agentic DJ

v31.3.0 keeps the stable Spotify virtual-driver / exact Audio Out route and the v30.4.47 LowEndIntegrity master, but upgrades the Agentic DJ from isolated one-shot actions into a lookahead transition-performance engine.

## What changed

### 1. Real 15-second Agentic lookahead

Agentic mode now intentionally delays the heard Spotify waveform by 15 seconds (configurable with `JOY_AGENTIC_LOOKAHEAD_SEC`, bounded to 8–20 s).

The realtime engine maintains two independent musical timelines:

- **Future/raw PCM:** the newest captured Spotify audio, up to 15 seconds ahead of what the listener hears. Used for CLAP planning, genre/mood mismatch estimation, and rolling-horizon decisions.
- **Delayed/source-time PCM:** the exact waveform currently entering the DJ mixer / about to be heard. Used for beat, transient, vocal, low-end and execution safety.

This prevents a common lookahead bug where an AI sees future audio but fires its effect immediately on the wrong part of the song.

### 2. Multi-bar transition choreography

For large prompt/source mismatches, especially Drum & Bass requests, the agent no longer tries to jump directly to the destination. It uses a staged scene:

1. **FOUNDATION** — tighten low end, increase source-derived drum punch and clarity.
2. **PROPULSION** — gradually add double-time articulation and energy.
3. **BUILD** — phrase-safe filtered hiss/riser, restrained filter and echo, optional source-derived half-beat roll when vocals are quiet.
4. **RELEASE** — clear the riser/filter and fire one bounded impact.
5. **DNB FEEL LOCK** — sustain the faster, harder drum feel without destructive whole-song time-stretching.

Example prompt:

`drum n bass like speed and drums a banger, smooth professional genre morph, strong drums, clean low end, tasteful riser and impact`

A trap/hip-hop song around 80–95 BPM is treated as a potential ~160–190 BPM perceived double-time source rather than being brutally resampled to 174 BPM.

### 3. Performance FX layer

The Agentic DJ now has bounded realtime performance controls in addition to normal Deck A/B EQ/filter/loop controls:

- filtered noise / hiss riser
- one-shot downbeat impact
- source-derived drum drive
- double-time top-percussion articulation
- punch / energy / clarity / air production overlays
- bass-tightening overlay

These controls do not call models, files, HTTP, subprocesses or Spotify APIs in the audio callback. They are vectorized DSP / synthesis only, and still feed the existing LowEndIntegrity, mastering compressor, fidelity guard and true-peak limiter.

### 4. Vocal and transient restraint

The morph scene continuously reduces riser, filter, echo and double-time density when vocal activity is high. The neural action critic remains active during a morph as a safety/context layer rather than competing with choreography by firing unrelated creative moves.

### 5. UI

The Agentic DJ policy panel now shows:

- active transition scene
- lookahead duration
- buffer completion
- neural state / decision / critic
- normal low-end and BPM-confidence information

A **DNB MORPH** prompt chip is included for direct testing.

## Audio-path invariants

The following v31.2.0 / v30.4.47 safety components are intentionally unchanged at AST level:

- `RealtimeDSP`
- `MasterSafetyCompressor`
- `SourceFidelityGuard`
- `LookaheadPeakLimiter`

The new work is upstream in Agentic scheduling, Deck mixer performance FX, dual-timeline buffering, and bounded parameter overlays.

## Important latency tradeoff

Agentic DJ mode now starts with approximately 15 seconds of intentional silence/delay while future PCM is collected. This is deliberate. It gives the agent enough musical context to make stronger edits with substantially better planning. Normal non-Agentic realtime modes keep their existing short continuity lookahead.

## Validation boundary

Python/JavaScript validation, synthetic DSP stress tests and timing tests are included in `V31_3_0_TESTS.txt`.

This build environment cannot execute the Windows WASAPI/Spotify route or the persisted Windows CLAP model cache. The first run on the target Windows machine remains the required live integration test for device routing and audible transition quality.
