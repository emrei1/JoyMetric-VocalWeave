# JoyMetric v31.6.0 — Unified Concert Agent

v31.6.0 unifies the Realtime 15D FX engine and Agentic DJ into one live performance surface and one prompt. It also fixes the main reason DJ actions felt nearly absent in v31.5.x.

## What was actually wrong in v31.5.1

There were three independent suppression mechanisms:

1. During a non-native genre morph (including DnB), `_apply_agent_logic()` explicitly allowed only `LOW_END_PROTECT` and `LOOP_RELEASE`; all creative neural actions were vetoed while the fixed morph choreography owned the mix.
2. In native mode, creative actions were restricted to 4/8-bar phrase boundaries plus a narrow phase window, so a good neural decision could wait many seconds or be replaced before it was ever executable.
3. The bar clock was anchored to the first audible callback instead of the measured beat phase (`to_next_beat_sec`) of the delayed PCM.

v31.6 removes the morph-vs-agent veto and replaces the wall-clock phrase gate with a beat-phase-locked rolling-horizon scheduler.

## One page, one prompt, two coordinated engines

Agentic DJ now mounts the existing Realtime 15D feature deck directly inside the Agentic page. There is only one DOM/control surface, not a duplicated fake panel.

The Agentic prompt is copied to the Realtime continuous-DJ prompt on LET'S GO. While live, prompt edits call `/api/agentic/prompt`, which updates both:

- NativeRealtimeEngine continuous Realtime 15D / DJ prompt
- RealtimeAgenticDJ concert policy / temporal planner

The Realtime feature controls remain the same controls used by the original Realtime page. In DJ-owned mode they continue to visualize the live feature state while the prompt-driven engine owns the targets.

## Temporal Concert Agent

The existing 15 s intentional lookahead is now used as a temporal planning horizon rather than one undifferentiated context window.

Pipeline:

1. Capture the next ~15 s of raw Spotify PCM.
2. Run one global CLAP semantic observation out-of-band (same isolated worker as before).
3. Split the future PCM into approximately bar-length chunks (bounded to 1.35–3.10 s).
4. Extract lightweight energy / drums / bass / vocal / melody descriptors per chunk.
5. Calculate adjacent structural novelty and energy/drum direction.
6. Maintain a receding-horizon action queue. The next two beats are committed; farther actions may be replaced as new future audio arrives.
7. Execute at the measured beat phase of the delayed/audible PCM.

This is inspired by barwise Music Structure Analysis / self-similarity boundary detection, but intentionally does not run a separate expensive foundation-model inference for every chunk. That avoids returning the realtime route to the scheduler-pressure problems fixed in v31.5.1.

## Concert controller vocabulary

The AI and the user now share the same beat-quantized macro bus:

- `MACRO_TENSION` — filter + riser + reverse + width + light echo-send build
- `MACRO_ECHO_OUT` — post-fader-style tempo echo send/return gesture
- `MACRO_ROLL_ACCEL` — source-derived half/one-beat roll + drum propulsion + guarded gate
- `MACRO_DROP` — clears build FX, releases loop deck, lands cinematic impact + punch/energy
- `MACRO_SPACE_BREAK` — reverse / reverb / width / echo space transition
- `MACRO_GROOVE_LIFT` — low-authority phrase-grid drum/energy lift so concert mode does not become inert in flat passages

Manual TENSION / ECHO OUT / ROLL ACCEL / DROP / SPACE pads arm the same macros for the next beat. They do not bypass quantization.

## Action-density fix

`action_density` is now separate from `creativity`.

- Creativity = variety / adventurousness of gestures.
- Action density = how often the scheduler is allowed to commit a justified performance move.

“concert set”, “festival set”, “active DJ”, “more actions” and related prompt language raises density. Minimal/leave-it-alone language lowers it. Structural novelty thresholds adapt to density.

The neural critic is also less over-conservative:

- lower NO_ACTION prior
- smaller repetition penalty
- smaller margin required to beat NO_ACTION
- action density contributes a bounded positive prior to creative candidates

Safety rules remain: low-end protection, transient pressure penalties, vocal guards and loop-release logic.

## Realtime / crackle safety

`realtime_native_engine.py` is byte-identical to v31.5.1. Therefore the v31.5.1 hardware fixes remain intact:

- 2048-frame Agentic processing quantum
- >=4096-frame physical Audio Out safety write quantum
- runtime FX CrackleGuard
- sounddevice/PyAudioWPatch merged headphone discovery
- exact sounddevice index selection
- high-latency WASAPI safety mode for fallback/wireless paths

No neural inference, network request, file I/O, temporal segmentation or planning runs in the realtime audio callback.

## Research basis

Architecture was informed by:

- Mixxx 2.6 engine/player design: beat tracking, quantize, read-ahead, pre/post-fader processing and separate mix/effects stages.
  - https://github.com/mixxxdj/mixxx/wiki/Developer-Guide-Engine
  - https://github.com/mixxxdj/mixxx/wiki/Developer-Guide-Engine-Player
  - https://manual.mixxx.org/2.6/en/chapters/appendix/mixxx_controls
- Barwise structure analysis using self-similarity / correlation block matching:
  - https://arxiv.org/abs/2311.18604
- 2026 evaluation of generic deep audio embeddings + barwise unsupervised boundary segmentation:
  - https://arxiv.org/abs/2603.27218

JoyMetric does not embed Mixxx itself in v31.6. The concert-control topology is implemented natively so the proven JoyMetric virtual-driver/WASAPI path remains stable.

## Validation boundary

Linux artifact tests can validate Python/JS, scheduler logic, temporal segmentation and DSP-file parity, but cannot execute the real Windows JoyMetric Virtual Driver + Spotify + Bluetooth/USB WASAPI hardware route. The first Windows run remains the final hardware validation.
