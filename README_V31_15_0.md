# JoyMetric v31.15.0 — RustCore

The brief: "do the agentic AI part in Rust — let's improve latency and real-time."

The honest engineering split: the agentic **planner** never runs inside the audio callback (~170 ms cadence, zero latency impact) — rewriting it in Rust buys nothing. What determines latency and real-time safety is the **per-block DSP hot path inside the Windows audio callback**. That is exactly what moved to Rust.

## 1. `native/joymetric_rt` — the realtime hot path as a native cdylib

A dependency-free Rust crate (C ABI, loaded with ctypes — no PyO3, no Python headers; MSVC toolchain, LTO, zero heap allocation per call, f64 internal / f32 interleaved I/O):

- **`jm_deck`** — the full deck channel strip in one call: 3 RBJ EQ biquads (low-shelf 120 Hz, peaking 1.15 kHz, high-shelf 7.6 kHz) + the bipolar DJ filter pair + smoothed gain, including the exact coefficient-morphing behavior (128-frame sub-block walk) and the exact `_smooth` tau/slew dynamics. Smoothed effective values are returned so UI telemetry stays truthful.
- **`jm_bass_cut`** — the program floor-strip highpass with its smoothing.
- **`jm_groove`** — the entire FullRemix groove in ONE call: redrum kick/clap with Pattern-DNA hashes and auto-fills, the key-following bassline, the power-chord stabs, the kick-keyed sidechain duck and swing — all per-sample in native code.

Python integration is a drop-in with graceful fallback: if the DLL is missing or fails its load-time self-test, the engine keeps the pure-numpy path untouched (`JOY_RUST_DSP=0` also forces it). Crate sources ship in the zip (`native/`), the prebuilt DLL loads from `native/joymetric_rt.dll`.

## 2. Measured results (everything-on stress blocks, 2048 frames)

| | Python | **Rust** |
|---|---|---|
| median | 5.13 ms | **2.38 ms** (2.15×) |
| p99 | 8.26 ms | **4.07 ms** |
| max | 8.84 ms | **4.66 ms** |

**Parity is essentially bit-exact**: deck strip max sample difference 1.9e-09, bass-cut 1.5e-08, groove low-band envelope correlation 1.0000 with level ratio 1.000 (deterministic layers identical; only the noise beds use a different RNG). Smoothed telemetry state matches to 0.

## 3. Block size: 2048 retained (field result)

The mixer benchmark suggested a 1024-frame block was safe, but the first live run at 1024 produced audible crackles — the mixer is only part of the callback (StudioDSP, the capture bridge and the per-app Spotify tap all share the block budget, and the tap's packet cadence does not follow the block size down). The launcher therefore stays at `JOY_AGENTIC_AUDIO_FRAMES=2048`. The RustCore win stands regardless: the p99/worst-case tail inside the unchanged budget is cut in half, which is precisely the headroom that prevents xrun crackle under load. 1024 remains available as an experiment via the env var.

## 4. Observability

Mixer status reports `rust_core`; the DJ Director grid shows **DSP CORE · RUST · NATIVE** (or PYTHON when running the fallback).

## 5. Validation — 126/126 across six suites

- **RustCore suite (11)**: DLL load + self-test; deck/bass-cut sample parity; groove envelope corr 1.0000 & level 1.000; everything-on full chain finite/bounded; benchmark gates (faster median, tighter p99, median inside half-budget).
- **All five prior suites re-run with the Rust path ACTIVE**: v31.14 TimeWeaver 21/21, v31.13 GridLock 12/12, v31.12 FullRemix 21/21, v31.11 StageCraft 29/29, v31.10 32/32 — proving the native core is behaviorally invisible except for speed.
