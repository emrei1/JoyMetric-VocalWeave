# JoyMetric Workstation UI v31.30.17 — "VocalWeave Live FX"

"Take the 16 s buffer; separate the vocal / melody with the model the Home tab uses; then, according to
the agentic prompt, transform that melody with the Home tab's Stable Audio model; play it. Every 16 s,
continuous and real-time. Buffer → process → play. The DJ controller parts stay."

## v31.30.17 — the latency profiles, after listening

The user tried them in turn: 4 s ("not good"), 8 s ("not good"), 10 s, 14 s, then 16 s, then settled on **20 s**. At 14–20 s the
budget (lag − window − pad) admits **8 s windows** — the default profile's own hop geometry (one 8 s hop after
8 s of known output), so the seams are the same as the 48 s profile's — with 10.5 s of budget for ~2.7 s of
work. That is `LAUNCH_MID.ps1` (`JOY_WEAVE_PROFILE=mid`, 20 s). Live: audible 20.4 s after LET'S GO, first woven
clip 14 s later, 2.7 s per window, 0 late, 0 xruns.

| launcher | window | lag | work / window | seams |
|---|---|---|---|---|
| `LAUNCH_SONGMIND.ps1` (normal) | 16 s (2 × 8 s hops) | 48 s | 3.4–4 s | smoothest; battery-safe |
| `LAUNCH_MID.ps1` | 8 s | 20 s | 2.7 s | same hop geometry as normal |
| `LAUNCH_LOWLAT.ps1` | 4 s | 10 s | 1.3 s | a step rougher |
| `LAUNCH_ULTRALOW.ps1` | 2 s | 4 s | 1.1 s | rough (the architecture's floor) |

## v31.30.16 — 4 s render lag, live (and why 2 s is the wall)

`LAUNCH_ULTRALOW.ps1` = `JOY_WEAVE_PROFILE=ultralow`: 2 s windows, 0.5 s pads, 1.5 s context (known prefix
2 s), one hop per window, start lead 1.5 s, render lag **4 s** (engine floor lowered to 4 s). Live on AC with
the Baseus output: audible 4.1 s after LET'S GO, first woven clip 2 s later, 1.1 s of work per 2 s window
(separation 0.42 s + Stable Audio 0.66 s at 4 steps, FX 1.0), 63 of 65 windows on time (the first window
is late once: the cold Stable Audio call takes ~6.7 s), 0 xruns, bridge cushion 1.28 s.

**2 s is not reachable.** About 1.1 s of the work is fixed cost per call regardless of window length
(the separator processes a full chunk, the diffusion model has a per-call floor), and the lag is
window + pad + work. A 2 s lag would need windows of ~0.6 s processed in ~0.6 s — 1.7× slower than real
time on this GPU — and a seam every 0.6 s. Even 2 diffusion steps and 1 s windows would land at ~2.3–2.5 s
with clearly worse audio. The 4 s profile is the floor of this architecture; the 10 s (`LAUNCH_LOWLAT.ps1`)
and 48 s (normal) profiles trade lag for smoother seams.

Measured seams (2 s windows, noise 0.51): excess level step at the boundaries median 2.4 dB (max 7.7 dB) at
FX 1.0, median 4.1 dB (max 10.9 dB) at FX 4.0; effect 25 / 45.

## v31.30.15 — "try a 2 s lag" (measured; shipped as an optional low-latency profile)

The real pipeline (separation worker + Stable Audio worker + hop continuation) was run window by window
over 24 s of the song while the live session shared the GPU:

| window | work per window | real time? | seams (excess level step vs the record's own) | effect | harmony |
|---|---|---|---|---|---|
| 2 s | 1.5 s (max 2.3 s) | **no** | median 3.4 dB, max 16.6 dB | 42 | 0.82 |
| 4 s | 1.3 s (max 1.35 s) | yes | close to the record's own | 51 | 0.85 |
| 16 s (default, 2 × 8 s hops) | 3.4–4 s | yes | measured earlier: 1.00 / 0.95 | 52 | 0.87 |

The lag can never be smaller than window + pad + work; with 2 s windows that is ≥ 4 s, the work is not
real time on this GPU, and every 2 s seam is audible. Stable Audio 3 is not a streaming model (whole-clip
latent diffusion), and the separator needs several seconds of context — 2 s is not reachable with the
same quality. What is: **`LAUNCH_LOWLAT.ps1`** — the low-latency profile (`JOY_WEAVE_PROFILE=lowlat`: 4 s
windows, 1 s pads, 3 s context, one hop per window, start lead 3 s) with a **10 s render lag** and the same
models; not for battery, and seams are a step below the 16 s profile. The normal launcher is unchanged.

## v31.30.14 — "the UI lags": performance mode while the session is live

The server was not the problem (status endpoints 16–18 ms, ~2 requests/s). The page's *live* colour theme
re-applies the CSS variables of the whole page ~36 times a second (`liveThemeTick`), the page carries 14
`backdrop-filter`s and 15 `blur`s, so every frame is a full-page repaint on the GPU — the same GPU that
Stable Audio and the separation model pin for seconds on every 16 s window. The browser's compositor
stalled during those jobs.

While an agentic session is live the page now switches itself into **performance mode** (`body.perf`):
backdrop-filter / blur / keyframe animations / transitions off, the live theme at 10 fps, the spectrum
canvas at 20 fps; it switches back when the session stops. Static files are served with `no-cache`, so a
page reload (F5) picks it up without relaunching.

## v31.30.13 — first woven audio 16 s after LET'S GO, and what the two sliders do

The ring snapshot was clamped to 30 s, so window 1 (starting 16 s into the record) was silently skipped at
start-up and the first woven audio came at 32 s; the clamp is 58 s now (the ring holds 60 s) → **~16 s**.

Measured on the full song (3 windows of 8 s; effect = MFCC distance of the render from the original
instrumental, "originals" = the song's own consecutive sections differ by 18):

| TEMP (noise) | FX (cfg) | effect | harmony | seams |
|---|---|---|---|---|
| 0.46 | 3.5 | 40 (37–44) | 0.93 | 1.00 / 0.96 |
| 0.65 | 1.0 (Home) | 26 (25–28) | 0.79 | 1.00 / 0.89 |
| **0.65** | **4.0** | **56 (49–68)** | 0.87 | 1.00 / 0.91 |
| 0.75 | 4.0 | 54 (46–64) | 0.79 | 0.99 / 0.83 |

**STABLE TEMP** is how far the model may move away from the record; **STABLE FX** is how hard the DJ prompt
pushes what it becomes. For an obviously audible transformation: TEMP 0.65 + FX 4.0. TEMP 0.39 / FX 1.9
(where the sliders were during the last session) is measurably subtle. Note that the vocals are untouched
by design — on vocal-heavy passages the change is in the instrumental underneath.

## v31.30.12 — "right now, for example, Stable is not coming": the 96 s dead zone after LET'S GO

The weave used to start on the *next* window to be captured after the 48 s lookahead had filled — and that
window is heard 48 s after its capture: about **96 s of untouched record after every LET'S GO**, during which
the effect is simply absent (the user restarts often, so this was heard again and again). Now the weave
starts on the audio the lookahead ring already holds: the first window that begins at least 8 s ahead of
the render position (window 1, at 16 s) is requested the moment the session becomes audible, and the ring
snapshot reaches back to it. The first woven window is heard ~16–32 s after LET'S GO. A session restart
also clears the previous session's hops, pending windows and continuity context (their frame numbers
belong to the old clock), keeping the slider settings.

## v31.30.11 — "Stable is sometimes audible, sometimes not" → prompt guidance is now a live setting

Checked the worker first: 128 of 128 windows scheduled on time (0 late, 0 failed, 0 skipped), separation
1.4 s, Stable Audio 2.4 s per window, only harmless health-connection aborts in its log. The worker was
fine; the *effect* varied. With Home's `cfg_scale 1.0` the text prompt has **no influence at all** — the
transformation is pure init-noise re-synthesis, and how far that lands from the original depends on the
passage. Measured per 8 s window on the full song at noise 0.61 (MFCC distance render vs original):

| cfg | effect mean | min – max | harmony vs record | seams (chroma / corr) |
|---|---|---|---|---|
| 1.0 (Home) | 20.7 | 16.7 – 30.6 | 0.85 | 0.99 / 0.90 |
| 2.5 | 35.1 | 25.6 – 45.2 | 0.85 | 1.00 / 0.95 |
| 4.0 | 52.5 | 45.5 – 59.7 | 0.87 | 1.00 / 0.95 |

At 4.0 the weakest window is stronger than the strongest window at 1.0 — the effect is always there —
while the harmony and the seams stay. So the woven instrumental now runs with **cfg 4.0** by default, and
a **STABLE FX** slider (1.0–7.0) next to STABLE TEMP sets it live (`weave.json {"cfg": …}`, API `cfg`);
a change re-renders the pending hops exactly like a temperature change. Cost: one extra model pass per
hop (~+1–2 s per window at 4 steps), well inside the 32 s budget.

## v31.30.10 — running on battery, and honest diagnostics

The last crackle sessions were on **battery** (11 % → 3 %): Windows parked cores, held the CPU at its 5 %
minimum state and the GPU at P4 / 20 W — separations took 10 s instead of 2.7 s, Stable Audio hops 8 s
instead of 2.5 s, and the audio thread woke late. Changes so the DJ can run unplugged:

- The launcher switches the power mode to **Best performance**, sets the CPU minimum state to 100 %,
  disables core parking and the battery-saver auto-on threshold (AC and DC alike; this drains the battery
  faster while the DJ runs).
- **Render lag 48 s** (was 40): the processing budget per 16 s window is now 32 s.
- **Separation with chunk overlap 1** instead of 2: 1.8 s per 16 s window instead of 4.6–7.6 s on battery;
  output correlation with overlap 2 is 0.999 (4 % relative difference).
- The **AMT / SongMind worker is not started** when the generative composer is off (it was idle but held
  0.9 GB; with Stable Audio 4.7 GB + separation 2 GB peak the 8 GB card paged). Stable Audio's memory
  fraction is capped at 0.70.
- The engine reports **`dsp_max_ms`** (longest DSP block over the last second), **`capture_gap_max_ms`**
  (longest wait for capture) and **`bridge_target_ms`** next to `bridge_fill_ms` / `xruns`, so the next
  dropout can be attributed instead of guessed: DSP too slow, capture starving, or the output clock.
- Measured on the app process while it ran: none of the weave's per-window CPU steps holds the interpreter
  lock longer than the 15–19 ms Windows timer quantum (resampling 32 ms total, file I/O 8–14 ms, array
  copies < 10 ms); kernel DPC / interrupt time stayed under 1 % / 2.2 %.

Below ~10 % battery Windows overrides all of this; the measurements of this build were cut short at 3 %.

## v31.30.9 — crackles and window seams (user: "the transitions must sound more continuous, and fix the crackles")

**Crackles = dropouts of the output bridge.** Measured after the process clean-up: the bridge cushion only ever
went *down* (a stall cost it permanently; the servo gave back one sample per ~3 blocks, i.e. 20 minutes for
0.4 s), so after a few stalls every further stall was an audible dropout. Two fixes:

- **Deficit reclaim** in the playback bridge: below target the cushion is refilled at up to 16 samples per
  block through the existing whole-block linear resampler (0.39 %, under 7 cents — the same rate the latency
  reclaim already used in the other direction). 0.4 s of cushion now comes back in ~50 s instead of 20 min.
- **UI polling** 260 / 360 ms → 1 s: the two status endpoints cost 27–30 ms of interpreter time each and were
  called ~7 times a second (≈ 200 ms/s of lock time on the audio process).

**Seams.** A window boundary was a 1.5 s raised-cosine cross-fade between two *different* renders of the
same bars (the previous window's tail and the new window's reconstructed head) — a phasey, "not quite
continuous" moment. Every boundary — hop or window — is now the same thing: one 100 ms blend from our own
previous output into the continuation (the last hop's clip ends exactly at the window end; the trailing 1.5 s
is model context only). The cancel clips follow the same 100 ms scheme, so cancel + transformed always sum
to exactly one instrumental across a boundary.

Also: the launcher now kills the app first by its port owner / name and waits for it (the old folder filter
never matched `python.exe app.py`, so the workers died first and the still-running app re-spawned a Stable
Audio worker — the source of every duplicate worker of the day).

## v31.30.8 — the real dropout root causes (found with the GPU check)

- **The audio process had never actually been AboveNormal.** `_raise_audio_process_priority` and the audio
  thread's `SetThreadPriority` used untyped ctypes handles; on 64-bit Windows the pseudo-handle is truncated
  and the calls fail silently (error 6). Typed now and verified in-process: process class 0x8000, thread
  raised, MMCSS "Pro Audio" registered. The same bug had defeated the workers' self-priority in v31.30.6.
- **20+ watchdogs from earlier deploys were still running**, each respawning its own AMT worker (13 listeners
  on :8769, GPU at 7.7 of 8 GB, 0.37 GB free) and raising the Stable Audio worker to AboveNormal every
  15 s — behind every "duplicate worker" and most dropouts of the day. The watchdog is now single-instance
  (pid marker; an older one exits when superseded) and never raises worker priority; the launcher retires
  every old watchdog first, kills the app before the workers, and waits until old GPU workers are gone.
- **The launcher's "kill the app first" had never matched** (the app's command line is just `python.exe app.py`), so
  the workers died first and the still-running app re-spawned a Stable Audio worker within a second — every
  duplicate worker of the day. The app is now killed first by its port owner / name and awaited.
- GPU sanity check passed (no driver resets, no throttling; fp16 matmul 13.5 TFLOPS / 19 GB/s only because
  the card was full at the time).

## v31.30.7 — "the Stable Audio output sometimes does not come"

Three findings, all fixed:

1. The in-worker priority call of v31.30.6 had **failed silently** (an untyped `GetCurrentProcess()` handle,
   Windows error 6), so the workers were still AboveNormal. The call is typed now and verified to land
   (`GetPriorityClass` reads 0x4000).
2. A **terminated Stable Audio process lingers** for seconds while the driver releases its memory; the
   launcher started the new worker meanwhile, so the GPU was shared with a ghost (the first window of a
   session took 48 s). The launcher now waits until every old GPU worker is really gone (up to 30 s).
3. When the worker was unreachable for a moment the weave disabled itself and re-probed only every 20 s,
   skipping the windows in between — heard as "the output comes only now and then". A failed call is now
   retried once after 1 s, the health re-probe runs every 5 s, and every failed / late window is kept in an
   error history that the status and `/api/agentic/weave` expose (`errors`).

## v31.30.6 — priorities enforced from inside the workers

After v31.30.5 the single Stable Audio worker still came up AboveNormal (the app process itself runs
AboveNormal and the creation flag did not stick), and the dropout storm returned the moment the weave
started: 39 xruns in 95 s. Setting that one process to BelowNormal live gave 0 xruns in the next 100 s.
Every GPU worker (Stable Audio, separation, AMT) now lowers its **own** process priority to BelowNormal at
start-up, independent of how it was launched. The launcher's bridge cushion is 14 blocks (the adaptive
logic had reached 14–18 safely during the storms; 28 had broken playback).

## v31.30.5 — the duplicate Stable Audio worker, for good

Live, the largest remaining cause of dropouts and slow windows was a **second Stable Audio worker**: the
worker answers no health probe while it loads (~30 s), so a second `start()` in that window spawned
another 4.5 GB process; two of them page the GPU (edits of 30 s instead of 4 s) and both ran AboveNormal.
Removing the duplicate live took the dropout rate from ~17 per 2 min to 1. The manager now keeps a pid
file for a freshly started worker and waits for it instead of spawning again, and creates the worker
BelowNormal. (The launcher kills the app before the workers since v31.29, so a stale worker from the
previous folder cannot be re-spawned either.)

## v31.30.4 — "fix the crackles" (second pass)

The transformed audio itself is clean (click metric 13–15/s at 4, 6 and 8 steps vs 15/s for the original
instrumental; no NaN; peaks ≤ 0.74). The crackles are **dropouts**: the xrun counter still rose about once
every 7 s, every time the elastic output bridge (128 ms) ran dry — the DSP loop had been preempted.

- The GPU workers (Stable Audio, separation, AMT) had been launched **AboveNormal** with torch free to use
  all 16 cores for its CPU-side work; they preempted the audio process. They now run **BelowNormal** with
  `torch.set_num_threads(4)` and `OMP_NUM_THREADS=4`. Applied live: 0 xruns in the following 90 s.
- The output bridge cushion is now configurable (`JOY_BRIDGE_BLOCKS`, default 7 ≈ 128 ms). A first try with
  28 blocks broke playback on this machine (the bridge never reached its start-up fill: continuous xruns), so
  the launcher keeps **7**; the worker-priority fix is the measured win.

## v31.30.3 — "the temp slider does not work"

It did reach the server (requests logged, re-renders counted), but its effect arrived far too late: every
slider move invalidated all pending hops (up to 6), a drag posted several values per second, and the Stable
Audio worker ran at 99 % GPU, queueing every call — a single hop call took 10–19 s instead of 2 s, so the
regular windows and the re-renders starved each other.

- **Horizon**: only hops that play within the next 20 s are re-rendered; farther hops are re-rendered as they
  come into the horizon (by then the slider may have moved again — that work is no longer wasted).
- **Dead band**: a hop is re-rendered only when the slider differs from its rendered temperature by ≥ 0.02.
- **Visible state**: next to the slider, "playing 0.52 · 2 queued" (polled every 2 s) — what is audible now
  and how many hops still wait for the new value.
- Front-end cache busting (`app.js?v=31-30-3`, `app.css?v=31-30-3`) so a browser cannot keep the old JS.

## v31.30.2 — "sometimes there are crackles"

Measured live: the xrun counter climbed in bursts of 2–4 exactly every 16 s, at the moment a new window is
cut from the ring — also when no weave job was running. That is the separation exchange: 12 MB of audio
base64-encoded into a 16 MB JSON request and a 16 MB JSON response parsed back, in the app process, whose
audio thread shares the interpreter lock; ~200 ms of lock-holding per window = several missed 43 ms blocks.

- The separation worker now has `/separate_raw`: raw float32 bodies both ways, stats in headers; the app
  never encodes or parses megabytes of text any more.
- A soft ceiling per hop (`tanh` above 0.9): the transformed instrumental plus the untouched vocal could
  produce hot peaks that the output limiter did not fully catch.
- The re-render worker thread runs at below-normal priority like the window thread.

## v31.30.1 — "re-rendering takes too long" (and: "check whether it is broken")

Checked live: the slider requests reached the server and re-renders did happen, but a re-render was a whole
16 s window (two hops, ≈ 5–12 s) queued behind the regular window job (≈ 10 s) and heard only at the next
16 s window boundary. Three changes:

1. **Hop-level clips.** Every 8 s hop is now its own synth clip (100 ms linear blends baked into the hop
   boundaries, raised-cosine window edges; the vocal-cancel clip stays window-level). A slider move
   re-renders **one hop (~2–3 s)** and swaps just that clip; the following hops are re-rendered one by one,
   each continuing the freshly rendered one, so continuity survives the change.
2. **Priority.** The earliest due hop is re-rendered first, and re-renders are serviced *between the hops of
   a running window job*, so a slider move no longer waits for a whole window. A hop starting in less than
   4.5 s keeps its old render.
3. **Half the model time per call.** The Home model pads every request to +6 s; the weave now asks for 1 s of
   padding (`duration_padding_sec` task field, `JOY_WEAVE_PADDING`). Measured on the full song: 4 hops in 11.5 s
   instead of 23.7 s, continuity unchanged (renders 16.1 vs the song's own 18.1; seam 1.00 / 0.91; harmony 0.90).

Expected latency of a slider move now: next hop boundary (0–8 s) + one hop render (2–3 s) → typically ~6 s.

## v31.30 — the Stable Audio temperature is a slider, and it changes in real time

**STABLE TEMP** slider in the Agent Performance console (0.20–0.70). It posts to `/api/agentic/weave`
(debounced), which persists the value to `weave.json` and hands it to the running weave.

Real time within what the pipeline allows: every 8 s hop reads the slider when it starts, and the windows that
are already rendered but not yet playing are **re-rendered at the new value and their clips swapped** (the
vocal cancellation stays; only the transformed instrumental clip is replaced; a window starting in less than
9 s keeps its old render). The change is therefore heard at the next window boundary — at most 16 s away,
typically about 8 s — instead of after the full 40 s render lag. The re-render uses the previous window's
output as its known prefix, so continuity is kept across the change; a following window that had used the
old render as context is re-rendered too.

`GET /api/agentic/weave` → `{noise, pending, rerenders, playing}`; status `synth_ai.weave.noise / pending /
rerenders`.

## v31.29.3 — "there is still a slight break and unrelatedness between windows"

Two causes found and fixed:

1. **Level steps at the seams.** Each window was gain-matched to its own instrumental's RMS, so the *same
   continuing texture* could step by up to 6 dB across a seam (gains 0.9 … 1.9 were logged). Now the kept
   cross-fade region of the new render is compared with the previous window's already-levelled tail and the
   gain that makes them equal is used, pulled only 25 % toward the RMS target per window (`g_cont`).
2. **Drift within a 16 s generation.** Measured on a full song (64 s, noise 0.52, 4 steps), consecutive-render
   timbre distance vs the song's own consecutive sections: 16 s windows with a 3 s known prefix 45.2 vs 43.3;
   8 s hops with a 3 s prefix 30.0 vs 25.9; **8 s hops with an 8 s known prefix 25.9 vs 26.1** — no added
   difference at all, seam chroma 1.00 / waveform correlation 0.92. So every 16 s window is now generated in
   **two 8 s continuation hops**, each preceded by 8 s of our own output as the known (inpaint-kept) prefix:
   the previous window's tail for the first hop, the first hop for the second; a 100 ms blend at the hop
   boundary covers the codec round trip. Two Stable Audio calls per window (≈ 3–5 s each at 4 steps).

Also (user): **DJ controller back on at 40 %** without the drum and instrument-adding parts —
`agentic.json {"controller_scale": 0.4, "dj_off": false, "no_drums": true, "no_instruments": true}`: the AI
drummer / re-drum / hat fills / snare rushes and the synth stabs / bass synth / deck-B layers are silenced,
the planner's generated and rack layers are dropped (freeze rolls, the record itself, stay), everything else
runs at 40 % of its amount. Live, no relaunch.

## v31.29.2 — the user's session notes

- **DJ controller off** (`agentic.json {"controller_scale": 0.0, "dj_off": true}`, live, no relaunch): the
  amount-like controls are scaled to 0 and the block tick cancels the planner's takeovers / freezes, empties
  the rack queue and stops generated layers; only the weave (protected vocal + transformed instrumental)
  keeps running. `controller_scale` alone (0.75 …) is the "a bit less" setting.
- **Temperature 0.52, 4-step (turbo) diffusion** by default (`weave.json {"noise": 0.52, "speed": "turbo", "amount": 1.0}`;
  code default 0.52, Home turbo). The user tried 0.6 ("very bad"), 0.5, 0.53, 0.47, 0.52. The weave is now
  **independent of the creativity slider**: the temperature is the user's number and the whole instrumental is
  always replaced (amount 1.0) — the live session had creativity 0, which used to leave 40 % of the original
  instrumental in place.
- **Measured on a full song (64 s, 4 consecutive windows, noise 0.53, 4 steps)** to see how much of the
  window-to-window difference is ours: consecutive-window timbre distance of the renders 46.2 vs 43.3 for
  the song's own consecutive sections; seam chroma 1.00, waveform correlation 0.92; harmony vs the record
  0.92. A fixed 3 s style anchor did not help (46.8); 8 s windows scale both numbers down together (31.2 vs
  25.9). So the design stays 16 s windows + 3 s known prefix: the seam is continuous and the remaining
  variation is close to the song's own. (The earlier 40 s test capture had truncated the second window,
  which made some of the v31.29.0/1 body-distance numbers unreliable; the seam numbers were valid.)

## v31.29 — continuity (the user: "the melody Stable Audio makes in one window can be unrelated to the next")

Every 16 s window had been an independent diffusion render: new seed, no memory. Measured on two consecutive
windows of the record (noise 0.45): the 1.5 s the two renders share had chroma similarity 0.48 and waveform
correlation 0.08; the two windows' timbre (MFCC) distance was 36.6 while the *original* windows differ by 21.9.
One seed + the previous output as plain input context barely helped (0.52 / 0.13 / 36.0).

The fix uses the model's **inpainting conditioning** (`stable_audio_3.model.generate`: `inpaint_audio` +
`inpaint_mask_*`, mask 1 = known). For window w the input is
`[previous window's OWN output over (start − 1.5 s, start + 1.5 s)] + [the record's instrumental from the window
start]`, the first 3 s are marked known, and the init audio (img2img, noise 0.45) still anchors the generated
part to the record. The model therefore *continues its own rendition* instead of inventing a new one.
Measured: shared region chroma 1.00, waveform correlation 1.00, timbre distance between the windows 16.6 —
consecutive renders are now more alike than the original windows are. The optional task field
`inpaint_keep_seconds` was added to `workers/sa3_fullsong_worker.py`; the Home tab's path is unchanged.

Also: one session seed for every window; pads 1.5 s with **raised-cosine** ramps (sum to exactly 1, zero
slope at both ends); temperature **0.60 at max creativity** (0.45 + 0.15 × creativity, v31.29.1 on the user's request;
measured at 0.60 with continuity: shared region chroma 0.99 / waveform 0.99, harmony vs the record 0.78, timbre
distance between windows 36.8 vs 16.6 at 0.45 — the seam stays continuous, the character drifts more within a
window; a longer known prefix (6 s, 9 s) did not reduce that drift, so the prefix stays 3 s); the DJ controller's overall
effect can be scaled live — `%LOCALAPPDATA%\JoyMetricgentic.json {"controller_scale": 0.75}` (amount-like
keys, neutral 0; sequences / modes / lengths untouched), set to 0.75 on the user's request.

## v31.28.2 — the user's two notes

"Vocals must not be touched by Stable Audio; they are heard exactly as they are." "The melody should be
affected more: raise the temperature." So the weave is now the Home tab's default flow, live: **protected
vocals + Stable-Audio-edited instrumental**, every 16 s.

## The pipeline (`vocal_weave.py`, `workers/sep_persistent_server.py`, `realtime_native_engine.py`)

Window **w** = frames `[w·16 s, (w+1)·16 s)` of the record, cut with 0.5 s of context on both sides.
The render lag is **40 s** (`JOY_AGENTIC_LOOKAHEAD_SEC=40`): a window is fully captured 24 s before it
plays; that is the processing budget. One window at a time, in a background thread:

1. **Separation** — the Home tab's Mel-Band RoFormer ("kim vocals"), resident on the GPU in its own
   worker (`:8770`, 16 s in ≈ 2.7 s, 2 GB peak, cache released per job). Vocal stem; instrumental =
   mix − vocals. **The vocal stem is never sent anywhere** — it stays in the record untouched.
2. **Stable Audio 3 medium, exactly as the Home tab uses it** (`processor.py` → `:8766`): the
   instrumental is the audio-to-audio input; prompt = the agentic DJ prompt + Home's suffix "Instrumental
   only. Preserve original melody, rhythm, tempo, harmony and musical structure."; Home's negative prompt;
   **cfg_scale 1.0**; steps = Home's balanced mode (6, `JOY_WEAVE_SPEED`); init noise is the temperature:
   measured on the record's instrumental, 0.45 keeps harmony 0.97 / rhythm 0.51, 0.55 → 0.88 / 0, 0.65
   (Home's default) → 0.72 / 0 with a new timbre. v31.28.2 used 0.50 + 0.15 × creativity (0.65 at max);
   the user then asked for half of it → **v31.28.3: 0.25 + 0.075 × creativity, 0.325 at max**. 17 s in 3–10 s.
   **Live tuning without a relaunch**: `%LOCALAPPDATA%\JoyMetric\weave.json`, read per window:
   `{"noise": 0.4, "speed": "turbo|balanced|quality", "amount": 1.0}` (env `JOY_WEAVE_NOISE` / `JOY_WEAVE_SPEED`
   still work as defaults).
3. **Post** — level matched to the record's instrumental, 20 Hz DC guard, soft ceiling, and the
   **cross-fade envelope**: linear ramps over the 0.5 s context pads, so consecutive windows sum to
   exactly 1 in their overlap (no gap, no bump at the 16 s seams).
4. **Playback** — two frame-exact clips at `w·16 s − 0.5 s`: the record's instrumental (under the same
   envelope) is **subtracted** by the engine's cancel deck, applied to the dry reference and the programme
   before the residual cap, so what remains of the record inside the window is exactly the vocal; the
   transformed instrumental is added on the synth deck. Amount 1.0 at creativity ≥ 0.6, partial below.
   Every DJ loop / FX / drummer layer stays.

Late results and failures leave the record untouched; a silent window is skipped.

## Arbitration with SongMind

Generated symbolic parts wait for the weave's verdict on their window: in a woven window they are dropped
(the transformed vocal *is* the melody there); a window the weave passed on ("no vocal", skipped) frees
them; a layer still undecided 4 s before it plays is dropped.

## Status

`synth_ai.weave`: windows, count, skipped, late, failed, busy, lead_s, last {vocal_ratio, vocal_active,
voiced, sep_ms, carrier_ms, sa_ms, sa_mode, gain, amount, level, ms}. Engine `agentic_mixer.vocal_cancel`
/ `cancel_clips`.

## v31.28.1 fix

The vocal stem arrives from the separation worker as a read-only buffer; the deck's in-place edge fades
raised `ValueError: output array is read-only` inside a silent `except`, so the cancel clip was dropped: the
original vocal stayed and the transformed melody was layered on top (heard as "broken melody"). The deck
now copies every buffer, scheduling failures are counted (`_cancel_sched_errors`), and the suite schedules
a read-only buffer on purpose. The launcher now also reaps port 8766 (a second Stable Audio worker from
the previous deploy folder had survived and pushed the GPU into paging: 46 s edits, late windows).

## Measured (`V31_30_17_TESTS.txt`)

73 v31.28/29/30 checks incl. live end-to-end and the continuity measurement, plus the v31.23–v31.27 regression battery.
