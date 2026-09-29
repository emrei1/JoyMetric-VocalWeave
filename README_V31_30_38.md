# JoyMetric Workstation UI v31.30.38 — "the model stopped" fixed at the root (SA3 worker reloads)

Applied in place to the `v31_30_37` folder on 2026-09-19 (the rollback build: v31.30.31 app + v31.30.36 desktop launcher).
Originals of the two changed files are kept in the session scratchpad (`v31_30_37_orig/`).

## Symptom

"Sometimes the model stops working": the woven instrumental disappears (vocals only) or a Home generation says
"Persistent SA3 engine did not become ready", and nothing recovers until a full relaunch.

## Root cause (measured, not guessed)

- When the Stable Audio worker (`workers/sa3_persistent_server.py`, :8766) dies for any reason, its **reload could not
  complete**: it crashed with `OSError: The paging file is too small for this operation to complete (error 1455)` inside
  `safetensors.safe_open`, or with an access violation in `c10.dll` / `torch_cpu.dll` (Windows Error Reporting,
  2026-09-18 12:14–12:26). The old `gpu_engine.py` monitor respawned it every ~4 s without backoff. In the old
  `v31_30_25` log: **900 worker starts, 672 died with 1455, 201 died silently, 27 reached READY.**
- Why the load fails: it needs a burst of *commit charge* (RAM + pagefile). Measured with 2 s samples of the worker's
  private bytes during a reload: **peak 21.9–22.1 GB** with the upstream `from_pretrained(device="cuda")`. Three parts:
  1. the fp32 model built on the CPU (~9 GB; the model has init-time buffers such as rotary `inv_freq`, so it cannot be
     built in fp16 or on the meta device),
  2. the whole fp32 state dict held at the same time (`safetensors.load_file`, 9.2 GB),
  3. **`safetensors.safe_open` memory-maps the 9.2 GB checkpoint copy-on-write; on Windows that charges the entire file
     to the process commit the moment it is opened** — that is where the 1455 errors were raised.
- The machine: 23.4 GB RAM, system-managed pagefile ~24 GB → commit limit 46.8 GB, ~33 GB already committed while the DJ
  runs (sa3 8 GB, separation 3.4 GB, semantic 1.8 GB, OneDrive 2 GB, browsers…). 33 + 22 > 46.8 → the reload dies,
  the monitor retries immediately, and the machine pages so hard that the audio thread drops out as well.

## Fix

1. **`gpu_engine.py` supervisor** — exponential backoff after a death within 180 s of the spawn (10/20/40/80/90 s), a
   commit gate before spawning (`JOY_SA3_MIN_COMMIT_GB`, default 12; waits up to `JOY_SA3_COMMIT_WAIT_S` = 300 s, then
   tries anyway), timestamped `[gpu_engine …]` lines in `runtime/sa3_engine.log` (the worker's own prints carry no time),
   the `=== starting persistent SA3 engine ===` marker now carries the time, and `status()["supervisor"]` reports
   `restarts / failed_loads / deferred / last_exit_code / commit_avail_gb / commit_limit_gb`.
   Offline test: `scratchpad/test_supervisor_v31_30_38.py` (commit gate, failed-load detection, backoff, respawn, log lines).
2. **`workers/sa3_persistent_server.py` streaming loader** (`_load_model_low_commit`, default; `JOY_SA3_LOAD_VIA_CPU=0`
   restores the upstream path): the fp32 model is built once on the CPU; the checkpoint is read with **plain sequential
   file reads into one reusable buffer (no memory map)**; every tensor is cast to fp16 on the GPU and swapped into its
   parameter immediately (the same fp32→fp16 cast upstream applies at the end; key remapping and the shape rule mirror
   `loading_utils.copy_state_dict`); `MIMALLOC_PURGE_DELAY=0` is set before `import torch` so freed CPU blocks return
   to the OS at once. A checksum over the loaded fp16 values is printed (`ENGINE weights checksum[streamed] ckpt`).
   **Measured: peak 13.2 GB (was 22.0), load 25 s (was 41–95 s), resident 7.3 GB, system commit peak 38 GB — under the
   46.8 GB limit with no pagefile growth. 997/997 tensors loaded; checksum identical to the independent verifier
   (`scratchpad/verify_sa3_checksum.py`: n=2305495793 sum=-1.574953e+04 abs=8.370240e+07).**
3. **`monitor_sa3.py`** (new, read-only): samples the app status, weave counters, sa3/sep health, worker memory, commit
   charge and GPU use every 10 s into `%LOCALAPPDATA%\JoyMetric\logs\monitor_sa3.log`, with `EVENT` lines for worker
   restarts, failed/late/stalled weave windows, session start/stop and low commit. Health timeouts while the worker's
   process is alive are `busy?` (its HTTP server is single-threaded and blocks during a generation), a fresh pid is
   `loading`. Stop it with `runtime\monitor_stop`.
4. **`SET_PAGEFILE_32GB.ps1`** (user step, admin + reboot): fixed 32 GB pagefile → commit limit ~55 GB, removes the
   remaining margin risk when Home + realtime + browser run together.

## Verify after a relaunch

`runtime/sa3_engine.log` shows `ENGINE streamed checkpoint: 997 tensors loaded, 0 skipped`, the checksum line above and
`ENGINE READY … load=~25s`; `[gpu_engine]` lines show at most one spawn; `monitor_sa3.log` shows `sa3=ready` with
~7.3 GB resident.
