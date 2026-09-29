# JoyMetric v31.5.1 — CrackleGuard + Headphone Output Fix

v31.5.1 keeps the v31.5 Performance FX Rack and 15-second Agentic lookahead, but hardens the Windows realtime route after real-hardware crackle and missing-headphone reports.

## What was wrong

The v31.5 heavy-FX validation used 1024-frame processing blocks (21.33 ms at 48 kHz). The measured DSP path fit inside that budget on the Linux validation machine, but the remaining Windows scheduler margin was too small for a real Spotify -> JoyMetric Virtual Driver -> Python DSP -> physical WASAPI output chain. A single scheduling spike could starve Audio Out and be heard as severe crackle.

Separately, the device picker was based primarily on PyAudioWPatch's WASAPI table. Some Bluetooth/USB headphone endpoints can be visible through Windows WASAPI/sounddevice while absent from that particular PyAudio enumeration, making them impossible to select.

## v31.5.1 fixes

### 1. Safer Agentic realtime quantum
- Agentic capture/DSP quantum defaults to **2048 frames** instead of 1024.
- At 48 kHz, the processing deadline becomes **42.67 ms** instead of 21.33 ms.
- The physical Audio Out worker uses its own **4096-frame write quantum** behind the existing elastic FIFO.
- This adds only a small fixed I/O latency compared with the intentional 15-second musical lookahead, while giving Windows substantially more scheduler headroom.

The musical samples are not resampled merely to gain this safety margin, and FX remain attached to the same delayed source samples.

### 2. Runtime FX CrackleGuard
If the playback FIFO actually underruns or an output write fails:
- the dry/core DJ signal remains live,
- the most expensive additive/send FX are temporarily reduced,
- the queue is allowed to recover,
- FX authority glides back over the next stable seconds.

This prevents one scheduler spike from cascading into repeated crackle while preserving impact/drum presence.

### 3. Professional FX rack safety tuning
The v31.5 controller topology is retained, including beat echo, TRANS/GATE, width, ducking, riser, reverse swell, snare rush and impact. v31.5.1 adds:
- true zero-work rack bypass when no rack/tail is active,
- lower maximum feedback authority,
- smoother/lower TRANS gate authority,
- safer echo send/return gain,
- slightly lower synthesized-noise layer gain so an intentional riser is less likely to be mistaken for technical crackle.

The effect is still prominent; the change is about headroom and stability, not reverting to weak FX.

### 4. Headphone / Bluetooth / USB output discovery
The output scan now merges missing **sounddevice WASAPI** outputs into the existing PyAudioWPatch list.

- Existing PyAudio endpoints keep their exact PyAudio IDs.
- A headphone visible only through sounddevice receives an `sd:<index>` ID.
- Selecting that device opens the exact sounddevice WASAPI index rather than fuzzy-matching by name.
- sounddevice output uses a safer high-latency WASAPI mode.
- The app still hides JoyMetric's private virtual transport endpoints.

For Bluetooth devices, choose the **stereo/music/headphones** endpoint. A mono `Hands-Free` / `Headset` telephony endpoint is intentionally rejected for Agentic DJ because the pipeline requires stereo output.

### 5. Output-rate compatibility
For the managed Spotify route, the engine now considers the selected physical output's native sample rate and also tries 48 kHz / 44.1 kHz compatible capture rates. This improves compatibility with headphones whose Windows mix format differs from the previous fixed assumption.

## Protected audio chain
The following classes remain AST-identical to v31.5.0:
- `RealtimeDSP`
- `MasterSafetyCompressor`
- `SourceFidelityGuard`
- `LookaheadPeakLimiter`

So the existing LowEndIntegrity / master-safety architecture was not rewritten to solve the crackle. The fix is primarily realtime scheduling, output compatibility, and FX-bus safety.

## Validation boundary
The numeric DSP and device-enumeration logic can be tested in this artifact environment. Actual Windows WASAPI, Bluetooth drivers, JoyMetric Virtual Driver routing and Spotify playback still require the target Windows machine for final integration validation.
