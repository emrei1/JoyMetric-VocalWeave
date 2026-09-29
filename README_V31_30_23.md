# JoyMetric Workstation UI v31.30.23 — "VocalWeave · 48 s · max window"

The pre-shortening 48 s app (DJ controller present, 16 s windows as two 8 s hops) with two changes on top:

## 1. The 8 s hops are merged into ONE 24 s generation per window
`HOP_S = 24`, window 24 s. 24 s is the largest single generation that fits the 48 s render lag (measured
critical path ~9 s vs a ~22 s budget; 32 s does not fit and runs out of GPU memory). One generation per window
means there is NO internal hop seam any more, and there are fewer window boundaries overall (one every 24 s
instead of a hop every 8 s) — the transitions are smoother just by being fewer.

## 2. Smooth transitions use Stable Audio's own continuity feature (inpainting)
Each 24 s window is generated *continuing from the previous window's last 8 s*, marked as known audio via Stable
Audio's inpaint mask (`inpaint_keep_seconds`). The model conditions on that known tail and generates the rest to
follow it — that is the Stable-Audio feature that keeps the seam continuous. The worker also honours an explicit
`inpaint_mask_end_seconds` now, so a future bidirectional seam-bridge (keep both sides, regenerate the middle)
can be added without a worker change.

## GPU safety (the "very bad Stable sound" fix)
A 24 s generation peaks higher in VRAM than the old 8 s hops. The weave now forces the low-VRAM **chunked decode**
so sa3 stays under its per-process memory cap — measured peak 7.5/8.1 GB, no out-of-memory, so the empty-cache +
retry storm that used to starve the audio bridge (constant crackle) never starts. Plus the **STABLE FX floor of
2.0** from earlier: the slider and API cannot drop cfg below 2.0, where the prompt stops guiding and the output
turns monotone/garbled.

Live after relaunch: audible ~48 s after LET'S GO, single 24 s generations, 0 xruns, 0 out-of-memory, windows on
time, VRAM peak 7.5/8.1 GB. The DJ controller is unchanged from the pre-shortening build.

## Battery = plugged-in performance (CPU side), v31.30.24

The launcher now applies the SAME full-speed processor settings on battery (DC) as on AC — verified on this laptop:
CPU min/max state 100 %, turbo boost aggressive, no core parking, active (fan) cooling, PCIe link at full power, and
battery-saver auto-on disabled — plus a High-performance GPU preference for the worker pythons. So separation, the
15D DSP and the audio thread run at plugged-in speed unplugged (no core-parking dropouts).

Honest limit on the GPU: an RTX 5070 Laptop caps its own power on battery (~72 W on AC here, lower on DC) and that
cap is enforced by the driver/EC — a non-admin `nvidia-smi` cannot raise it (the launcher tries, and no-ops without
admin). It does not matter for the audio, because the 48 s lag gives each 24 s generation a ~22 s budget against a
~9 s AC critical path: even a 2x battery slowdown stays well inside the budget, so no late or dropped windows. For
maximum GPU clocks on battery you can set, once, in the NVIDIA Control Panel: Manage 3D settings → Power management
mode → Prefer maximum performance (that part needs the NVIDIA panel / admin, not reachable from this launcher).

## Busy-passage dropouts / crackle on battery (v31.30.24): the audio buffer

The user heard dropouts + crackle on battery, worse on fuller / louder passages ("patlamalar"). Measured cause:
the machine is not saturated (CPU ~24 % of 16 cores) but **app.py runs at ~94 % of one core** and the audio DSP
loop shares Python's GIL with the DJ controller (neural brain, planner, drum/rack ticks). On busy passages the
controller holds the GIL longer, so the audio loop stalls — up to 700 ms (= a dropout), with regular 50-90 ms
overruns (= crackle). On battery there is less CPU headroom, so it tips over.

Fix: the capture buffer is doubled, `JOY_AGENTIC_AUDIO_FRAMES` 2048 -> 4096 (42.7 -> 85.3 ms). That halves the
DSP loop's iteration rate (half the GIL churn) and gives each block more slack. Measured on AC: peak stall 711 ->
206 ms, xruns 0.67/s -> 0 over a minute. On battery this removes most of the crackle; a very heavy passage can
still glitch because the full DJ controller is CPU-heavy in Python. If that remains, the controller-less VocalWeave
build (v31.30.21) has no such load and is glitch-free on battery.
