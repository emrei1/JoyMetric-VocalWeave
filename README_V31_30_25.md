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
