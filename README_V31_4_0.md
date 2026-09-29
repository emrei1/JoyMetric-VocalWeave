# JoyMetric v31.4.0 — Pro FX Director

v31.4.0 keeps the v31.3 15-second lookahead / delayed audible timeline and upgrades Sound FX into a first-class prompt-conditioned DJ performance system.

## What changed

### Prompt-conditioned FX Director
The prompt now compiles a dedicated FX palette instead of using one generic riser/impact recipe. The policy exposes:

- `fx_profile`
- `fx_presence`
- `fx_riser`
- `fx_impact`
- `fx_reverse`
- `fx_snare_rush`
- `fx_space`
- `fx_echo`
- `fx_phrase_accents`

Examples:

- DnB / jungle / banger → strong riser, reverse swell, accelerating snare-rush, cinematic impact, dry/punchy low-end-safe release.
- Dreamy / hypnotic → reverse swell, reverb bloom and echo-tail authority, restrained impact.
- Hip-hop / trap → shorter impact/echo gestures and less constant wash.
- `very restrained fx`, `minimal fx`, `no fx` → explicit low-FX mode.

`professional`, `smooth`, or `tasteful` no longer means "almost inaudible". It means longer arcs, fewer simultaneous layers and stronger safety guards.

### Neural FX action bank
The CLAP prompt-conditioned DJ action bank now includes:

- `FX_RISER`
- `FX_REVERSE_SWELL`
- `FX_SNARE_RUSH`
- `FX_IMPACT`
- `FX_ECHO_BLOOM`

This means semantic phrasing can select an FX gesture even when the user does not type the exact effect name. The existing musical critic still applies phrase, buildup/drop, vocal, transient, bass and repetition penalties.

### New realtime-safe FX DSP
The Agentic mixer now contains independent stateful DSP for:

- stronger band-limited hiss/noise riser
- wide reverse-swell / reverse-cymbal illusion
- BPM-synchronous snare-rush that accelerates from 8th-note toward 16th-note density
- prompt-strength cinematic impact: sub thump + crack + short wide crash tail
- source-derived drum excitation and double-time top-percussion from v31.3

No sample file, filesystem read, network request, model call or subprocess is added to the realtime audio callback.

### DnB morph choreography
The 15-second lookahead DnB morph now behaves as:

1. FOUNDATION — clean/tighten source and add drum punch.
2. PROPULSION — increase double-time perception and introduce low-level reverse/fill motion.
3. BUILD — riser + reverse swell + snare rush become foreground transition FX while vocal/transient guards remain active.
4. RELEASE — build stack hard-clears on the audible downbeat, then one prompt-scaled cinematic impact lands.
5. DNB FEEL LOCK — fast drum feel remains while transition FX fall away.

The impact strength is one-shot and automatically clears after triggering, so headroom is not permanently reduced.

## Safety / regression boundary

Unchanged from v31.3 / v30.4.47:

- `RealtimeDSP` AST: identical
- `MasterSafetyCompressor` AST: identical
- `SourceFidelityGuard` AST: identical
- `LookaheadPeakLimiter` AST: identical
- JoyMetric driver patch: byte-identical
- driver installer: byte-identical
- setup/start path: byte-identical
- crash-safe device scan worker: byte-identical

The new FX run before the existing LowEndIntegrity / CrackleGuard / true-peak safety path.

## Validation

48 kHz stereo, 1024-frame synthetic stress with riser 92%, reverse swell 88%, snare rush 92%, impact strength 100%, drum drive 78%, double-time 78% plus the existing RealtimeDSP:

- finite output: PASS
- mean processing: 8.855 ms/block
- p95: 13.043 ms/block
- p99: 15.300 ms/block
- block budget: 21.333 ms
- max sample peak in this synthetic run: 0.5374

Windows Spotify/WASAPI hardware routing cannot be executed in this Linux packaging environment. The route/driver files are intentionally unchanged; the first Windows run remains the real integration/listening test.
