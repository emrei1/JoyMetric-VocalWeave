# JoyMetric Workstation UI v31.30.25 — "VocalWeave · no DJ controller"

The AI DJ gesture controller is removed (agent + UI + API) to free the CPU that stalled the audio thread on battery.

Removed: the neural DJ brain, planner, AI drummer, synth rack, takeovers, 22-gesture families, performance queue and
automation lanes (agent), and their UI — the CONCERT CONTROLLER card + performance FX rack, the ghost DECK B, the
transition matrix, the director / choreography cards, and (at the user's request) DECK A, the STEM ACTIVITY card and
the ENGINE HEALTH card. The controller API endpoints (/api/agentic/controls|macro|loop|take-control|regenerate|
approve|lock) answer 410.

Kept: VocalWeave (the vocal stays exact, the instrumental is transformed by Stable Audio) and the engine's FX — the
"MusicCLAP -> 15D StudioDSP" prompt-shaping that colours the processed sound. The UI is now just AGENT PERFORMANCE
(prompt + STABLE TEMP / STABLE FX sliders + LET'S GO / STOP) and the UNIFIED REALTIME FX card. Set
JOY_DJ_CONTROLLER=1 to bring the full controller back.

Why: measured on AC, the audio DSP loop shares Python's GIL with the controller; with the controller ON, app.py ran
94 % of one core and the DSP loop stalled up to 711 ms on busy passages (40 xruns/min). With it removed: app.py 60 %,
peak stall 220 ms, 0 xruns/min. On battery this is the fix for the busy-passage dropouts/crackle. The one remaining
constant load is the FX critic (the CLAP DSP-gradient, ~96 % of a separate core) - kept because it IS the FX; if
battery still glitches it can be throttled (slower prompt-adaptation, unnoticeable for VocalWeave) without removing it.


## Battery crackle: FX critic auto-throttled on battery (v31.30.26)

Unplugging still crackled because the FX critic (the CLAP semantic gradient that shapes the 15D StudioDSP toward the
prompt) pins ~96 % of a core constantly (~1.2 Hz), and on battery that constant draw shrinks the package-power budget
the audio thread needs. Now on battery the engine detects DC (GetSystemPowerStatus) and stretches the gradient
interval x20 (~0.8 s -> ~16 s, validated: dj_update_hz 1.2 -> 0.25 at x5), so the critic idles most of the time -
the DSP prompt-shaping updates every ~16 s instead of continuously, imperceptible for VocalWeave (the instrumental is
already transformed by Stable Audio). AC is unchanged. Tune with JOY_FX_GRADIENT_BATT_MULT (bigger = lighter/slower);
JOY_FORCE_BATTERY=1 applies the battery profile on AC too. Honest note: the laptop's real battery power cap cannot be
reproduced on AC, so this is validated by the rate drop and handed to you to confirm by ear on battery.


## Battery crackle, deeper (v31.30.27): the two real CPU consumers, throttled

py-spy on the running app showed the one hot Python thread was the AGENT loop in numpy percentile (the MIR analysis,
_analysis_features), and the biggest process was the FX critic (CLAP) at ~95 % of a core. Both stall / starve the
audio thread, worst on battery. Two throttles, keeping VocalWeave + the FX sound:
- MIR analysis: with the DJ controller removed it only feeds VocalWeave's key, so it now runs every ~3 s (AC) / ~6 s
  (battery) instead of ~0.85-1.6 s. app.py dropped 60 % -> 34 % of a core.
- FX critic: the CLAP semantic gradient is slowed on AC too (x4 -> ~3.3 s, 95 % -> ~52 %) and much more on battery
  (x20). The FX SOUND is the 15D StudioDSP per-block processing (unchanged); the critic only nudges its targets, so a
  slower rate is inaudible for a static prompt.
Tunable: JOY_MIR_INTERVAL / JOY_MIR_BATT_MULT, JOY_FX_GRADIENT_MULT / JOY_FX_GRADIENT_BATT_MULT. JOY_FORCE_BATTERY=1
applies the battery profile on AC. Measured on AC: 0 xruns. On battery this is a large cumulative reduction over the
earlier build; confirm by ear. If it STILL crackles the remaining path is the audio thread's own 15D StudioDSP per
block being too slow for the battery CPU - then the fix is a battery mode that bypasses the heavy DSP (raw VocalWeave
only), which I can add on request.
