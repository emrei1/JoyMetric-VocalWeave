# JoyMetric Workstation UI v31.30.37 — "VocalWeave · rollback to the pre-Home-GPU build"

Created 2026-09-19 at the user's request: "go back to the version of the app from before the Home-page GPU/OOM
changes" and "the current desktop icon must start that version".

## What this build is

- The **v31.30.31 app** = the state of the `v31_30_25` folder on 2026-09-10 (no DJ controller, no realtime FX,
  16 colour themes with the live agentic colour pulse, VocalWeave 48 s lag / 24 s single-generation windows,
  4096-frame audio buffer, battery power block, STABLE FX floor 2.0).
- PLUS the **v31.30.36 desktop launcher** of 2026-09-15 (`LAUNCH_DESKTOP_APP.vbs` / `LAUNCH_DESKTOP_APP.ps1` /
  `desktop_loading.html`): the desktop icon opens the same UI as a chromeless Edge app window and starts the
  backend at most once per boot.
- The user's Home library (`runtime/jobs`, `runtime/cache`) was copied from `v31_30_25`, so nothing is lost.

## What was rolled back (the 2026-09-12 changes v31.30.32 / v31.30.33 / v31.30.34)

| file | 12 Sept change that is NOT in this build | source of the restored file |
|---|---|---|
| `LAUNCH_SONGMIND.ps1`, `LAUNCH_MID.ps1`, `LAUNCH_LOWLAT.ps1`, `LAUNCH_ULTRALOW.ps1` | `PYTORCH_CUDA_ALLOC_CONF=garbage_collection_threshold:0.8,max_split_size_mb:512` | `v31_30_23` parent (content identical to the pre-12-Sept files) |
| `config.json` | `gpu_worker_offload_for_roformer` false, `gpu_home_mem_fraction` 0.85 | `v31_30_23` parent (back to `true`, key absent) |
| `processor.py` | per-job `mem_fraction` for Home generations, v31.30.33 comment | `v31_30_23` parent |
| `workers/sa3_persistent_server.py` | per-job `torch.cuda.set_per_process_memory_fraction` | `v31_30_23` parent |
| `vocal_weave.py` | `mem_fraction` 0.70 in the `/run` body | `v31_30_23` parent |
| `app.py` | separation-worker lifecycle (`_stop_sep_worker` / `_ensure_sep_worker`), `run_job` stopping realtime before a Home job, `agentic_start` restarting the separation worker | rebuilt from the current file by removing exactly the four v31.30.34 additions; verified to differ from the `v31_30_23` parent only by the seven v31.30.25 "410" lines |

Every other file is byte-identical to the `v31_30_25` folder. The `v31_30_25` folder itself is untouched and remains the
reference of the OOM-fix build (v31.30.34 + desktop launcher).

## Known behaviour of this (pre-fix) build

A Home generation right after a realtime session can hit CUDA out-of-memory on the 8 GB card — that was the reason
for the 12 Sept changes. If it happens: STOP the realtime session, close the app and start it again from the desktop
icon (fresh SA3 worker), then run the Home generation.

## Launch

Desktop shortcut "JoyMetric VocalWeave" → `LAUNCH_DESKTOP_APP.vbs` in this folder → `LAUNCH_SONGMIND.ps1`
(sa3 :8766, semantic :8767, separation :8770, app :8765) + Edge app window. Manual: `LAUNCH_SONGMIND.ps1`.
