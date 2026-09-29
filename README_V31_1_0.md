# JoyMetric v31.1.0 — Realtime Agentic DJ / Spotify Prototype

This branch replaces the v31.0 offline two-file transition demo with a live Spotify DJ agent.

## Audio architecture

Spotify -> JoyMetric Virtual Input -> WASAPI loopback -> Deck A LIVE -> Realtime DJ Mixer -> v30.4.47 LowEndIntegrity / CrackleGuard / 15D StudioDSP -> exact physical Audio Out.

Deck B is a bounded internal Loop/Roll deck captured from Deck A's own PCM history. It is not a fake second Spotify stream.

The realtime callback performs no model calls, subprocesses, media-session calls, file I/O, or planning.

## Agent architecture

- Fast observer: 1.5 s live PCM
- Musical-context observer: 15 s live PCM
- Existing MusicCLAP continuous DJ semantic layer: out-of-band
- Beat/BPM, energy, key and content-activity analysis: out-of-band
- Rolling beat/bar planner: out-of-band
- Controller executor: bounded numeric targets only
- Windows GSMTC Spotify transport: optional out-of-band play/pause/next/previous/seek/beatjump

## DJ controls exposed to both agent and manual user

Deck A: Low / Mid / High / bipolar Filter / Gain
Deck B Loop: Low / Mid / High / bipolar Filter / Gain
Mixer: correlation-aware crossfader
Loop/Roll: 1/2, 1, 2, 4 beat manual capture plus agent gestures
Master FX: beat-synced Echo + restrained Reverb
JoyMetric: existing MusicCLAP + 15D semantic DSP remains active

## Quality rules retained

- RealtimeDSP, MasterSafetyCompressor and SourceFidelityGuard are AST-identical to v31.0/v30.4.47.
- JoyMetric custom virtual driver installer/patch is unchanged.
- Loop seam gets a short overlap crossfade.
- A/B equal-power mixing is correlation-normalized to avoid correlated +3 dB overs.
- Existing -2 dBTP-class LowEndIntegrity master remains the final safety stage.

## Prototype limitation

Spotify does not expose future raw PCM to JoyMetric. The live agent therefore cannot truthfully build a full future phrase map of the currently playing Spotify track. It uses past/current multi-timescale context plus Windows media-session timeline controls. A future licensed/local-track mode can add full-track lookahead without changing the controller/action API.
