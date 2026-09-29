from __future__ import annotations

import argparse
import os
import json
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

# v31.30.38: c10's CPU allocator on Windows is mimalloc, which returns freed blocks to the OS lazily - the fp32
# storages released while streaming the checkpoint stayed in the process commit until seconds after READY
# (peak 22 GB).  Ask it to purge freed memory immediately.  Must be set before torch (c10.dll) is imported.
os.environ.setdefault("MIMALLOC_PURGE_DELAY", "0")
os.environ.setdefault("MIMALLOC_RESET_DELAY", "0")
os.environ.setdefault("MIMALLOC_ARENA_EAGER_COMMIT", "0")

import torch
import ctypes
try:
    import ctypes
    _k = ctypes.windll.kernel32; _k.GetCurrentProcess.restype = ctypes.c_void_p; _k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _k.SetPriorityClass(ctypes.c_void_p(_k.GetCurrentProcess()), 0x4000)   # BELOW_NORMAL_PRIORITY_CLASS (v31.30.7: typed handle - the untyped call failed with error 6)
except Exception:
    pass
try:
    torch.set_num_threads(4)             # v31.30.4: CPU-side torch work must not saturate the cores the audio loop needs
except Exception:
    pass
from stable_audio_3 import StableAudioModel

import sa3_fullsong_worker as core


def configure_torch():
    torch.set_grad_enabled(False)
    # v31.22.3: keep the whole working set in dedicated VRAM.  On an 8 GB laptop card the caching
    # allocator grew to 7.7 GB + 1.9 GB *shared* (system-RAM backed) memory after long full-song jobs;
    # WDDM then pages allocations in and out on every diffusion step and a 5 s clip takes 8-19 s
    # instead of ~1.5 s (GPU shows 100 % 'utilization' while stalled on paging).
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    torch.backends.cudnn.benchmark = True
    try:
        torch.set_float32_matmul_precision("high")
    except Exception:
        pass


def _commit_gb():
    """(available commit GB, commit limit GB) via GlobalMemoryStatusEx - (None, None) when unavailable."""
    try:
        class _MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                        ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64)]
        ms = _MS()
        ms.dwLength = ctypes.sizeof(_MS)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            return None, None
        return ms.ullAvailPageFile / 2 ** 30, ms.ullTotalPageFile / 2 ** 30
    except Exception:
        return None, None


def _print_checksums(m, label: str, loaded_keys=None):
    """Weight checksums for verification: [all] = every floating tensor of the model; [ckpt] = only the tensors that
    came from the checkpoint (comparable with scratchpad verify_sa3_checksum.py, which streams the file itself)."""
    try:
        with torch.no_grad():
            tot = {"all": [0.0, 0.0, 0], "ckpt": [0.0, 0.0, 0]}
            for k, t in m.model.state_dict().items():
                if not t.is_floating_point():
                    continue
                tt = t.to("cuda", torch.float64) if not t.is_cuda else t.to(torch.float64)
                sm = float(tt.sum()); ab = float(tt.abs().sum()); n = int(tt.numel())
                del tt
                tot["all"][0] += sm; tot["all"][1] += ab; tot["all"][2] += n
                if loaded_keys is not None and k in loaded_keys:
                    tot["ckpt"][0] += sm; tot["ckpt"][1] += ab; tot["ckpt"][2] += n
            print("ENGINE weights checksum[%s] all: n=%d sum=%.6e abs=%.6e" % (label, tot["all"][2], tot["all"][0], tot["all"][1]), flush=True)
            if loaded_keys is not None:
                print("ENGINE weights checksum[%s] ckpt: n=%d sum=%.6e abs=%.6e" % (label, tot["ckpt"][2], tot["ckpt"][0], tot["ckpt"][1]), flush=True)
    except Exception as exc:
        print("ENGINE checksum failed: %s" % exc, flush=True)


_SAFETENSORS_DTYPES = {"F32": torch.float32, "F16": torch.float16, "BF16": torch.bfloat16, "F64": torch.float64,
                       "I64": torch.int64, "I32": torch.int32, "I16": torch.int16, "I8": torch.int8, "U8": torch.uint8, "BOOL": torch.bool}


def _iter_safetensors(path: str):
    """Yield (name, dtype, shape, tensor_view) for every tensor of a .safetensors file using plain sequential file
    reads into ONE reusable buffer - no memory map.  safetensors' safe_open() maps the file copy-on-write, and on
    Windows that charges the whole file (9.2 GB for the medium checkpoint) to the process commit the moment it is
    opened: that was the +10 GB step in the load and the place where 'The paging file is too small' (error 1455)
    was raised.  The yielded tensor is a view of the shared buffer: consume it (copy to CUDA) before the next one."""
    import struct
    with open(path, "rb") as fh:
        hl = struct.unpack("<Q", fh.read(8))[0]
        header = json.loads(fh.read(hl).decode("utf-8"))
        base = 8 + hl
        items = [(k, v) for k, v in header.items() if k != "__metadata__"]
        items.sort(key=lambda kv: kv[1]["data_offsets"][0])
        maxn = max((v["data_offsets"][1] - v["data_offsets"][0] for _, v in items), default=0)
        buf = bytearray(max(maxn, 1))
        for k, v in items:
            o0, o1 = v["data_offsets"]; n = int(o1 - o0)
            dt = _SAFETENSORS_DTYPES[v["dtype"]]
            shape = [int(x) for x in v["shape"]]
            if n == 0:
                yield k, dt, shape, torch.empty(shape, dtype=dt)
                continue
            fh.seek(base + o0)
            mv = memoryview(buf)[:n]
            got = fh.readinto(mv)
            if got != n:
                raise IOError("short read for %s: %d of %d bytes" % (k, got, n))
            yield k, dt, shape, torch.frombuffer(mv, dtype=dt).reshape(shape)


def _load_model_low_commit(name: str):
    """v31.30.38: STREAMING load - plain file reads, each checkpoint tensor goes to CUDA as fp16 as it is read.

    Upstream from_pretrained() holds the fp32 model (~9 GB) and the whole 9.2 GB fp32 state dict at once (and the
    copy-on-write memory map of the checkpoint costs another 9.2 GB of commit on Windows), then moves fp32 to the
    GPU: the worker's commit charge peaked at ~22 GB (measured 2026-09-19), which hit this machine's 46.8 GB commit
    limit on every reload and killed the load ('The paging file is too small', error 1455, or an access violation
    in c10.dll - 672 of 900 starts in one log = 'the model stopped').  Here the fp32 model is built once on the CPU
    (the only large allocation); every checkpoint tensor is then read into one reusable buffer, cast to fp16 ON THE
    GPU (the same fp32 -> fp16 cast upstream applies later) and swapped into its parameter / buffer immediately, so
    the fp32 CPU storages are freed one by one.  Key remapping and the shape rule mirror
    loading_utils.copy_state_dict.  A checksum over the loaded fp16 values is accumulated while streaming
    (identical to scratchpad verify_sa3_checksum.py: n=2305495793 sum=-1.574953e+04 abs=8.370240e+07 for medium).
    JOY_SA3_LOAD_VIA_CPU=0 restores the upstream path.
    """
    if os.environ.get("JOY_SA3_LOAD_VIA_CPU", "1") != "1":
        m = StableAudioModel.from_pretrained(name, device="cuda")
        if os.environ.get("JOY_SA3_CHECKSUM_ALL", "0") == "1":
            _print_checksums(m, "upstream")
        return m
    a0, lim = _commit_gb()
    try:
        import gc
        from stable_audio_3.model_configs import all_models
        from stable_audio_3.factory import create_diffusion_cond_from_config
        model_cfg = all_models[name]
        local_config, local_ckpt = model_cfg.resolve()
        with open(local_config) as fh:
            model_config = json.load(fh)
        model = create_diffusion_cond_from_config(model_config)        # fp32 on the CPU - the only large allocation
        params = dict(model.named_parameters())
        buffers = dict(model.named_buffers())
        sd_keys = set(model.state_dict().keys())
        dev = torch.device("cuda")

        def target_key(key):                                           # = loading_utils.remap_state_dict_keys, per key
            if key in sd_keys:
                return key
            parts = key.split(".")
            for i in range(1, len(parts)):
                cand = ".".join(parts[:i]) + "." + ".".join(parts[i + 1:])
                if cand in sd_keys:
                    return cand
            return key

        want_cs = os.environ.get("JOY_SA3_CHECKSUM", "1") == "1"
        loaded = 0; skipped = []; cs_sum = 0.0; cs_abs = 0.0; cs_n = 0
        for k, dt, shape, t in _iter_safetensors(local_ckpt):
            tk = target_key(k)
            obj = params.get(tk)
            if obj is None:
                obj = buffers.get(tk)
            if obj is None:
                skipped.append(k); continue
            if tuple(t.shape) != tuple(obj.shape):
                skipped.append(k); continue
            with torch.no_grad():
                if t.is_floating_point():
                    h = t.to(dev, dtype=torch.float16)                 # fp32 -> fp16 on the GPU, as upstream's final cast
                    if want_cs:
                        d = h.float().double()                         # small fp64 temp on the GPU, freed at once
                        cs_sum += float(d.sum()); cs_abs += float(d.abs().sum()); cs_n += int(d.numel()); del d
                else:
                    h = t.to(dev)
                obj.data = h                                           # the parameter's fp32 CPU storage is freed here
            loaded += 1
        for k in skipped[:20]:
            print(f"Key {k} not found in target state_dict or shape mismatch. Skipping.", flush=True)
        print("ENGINE streamed checkpoint: %d tensors loaded, %d skipped" % (loaded, len(skipped)), flush=True)
        if want_cs:
            print("ENGINE weights checksum[streamed] ckpt: n=%d sum=%.6e abs=%.6e" % (cs_n, cs_sum, cs_abs), flush=True)
        gc.collect()
        a1, _ = _commit_gb()
        model.to(torch.float16)                                        # the remaining (non-checkpoint) fp32 buffers
        model.to(dev)                                                  # ... and anything still on the CPU
        model.eval().requires_grad_(False)
        model.use_lora = False
        model.lora_names = []
        torch.cuda.synchronize()
        gc.collect()
        torch.cuda.empty_cache()
        a2, _ = _commit_gb()
        print("ENGINE low-commit load: commit free before=%.1f GB, after build+stream=%.1f GB, after finalize=%.1f GB (limit %.1f GB)"
              % (a0 if a0 is not None else -1, a1 if a1 is not None else -1, a2 if a2 is not None else -1, lim if lim is not None else -1), flush=True)
        m = StableAudioModel(model, model_config, "cuda", True)
        if os.environ.get("JOY_SA3_CHECKSUM_ALL", "0") == "1":
            _print_checksums(m, "streamed")
        return m
    except Exception:
        traceback.print_exc()
        print("ENGINE low-commit load failed - falling back to the upstream CUDA load", flush=True)
        m = StableAudioModel.from_pretrained(name, device="cuda")
        return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--model", default="medium")
    args = ap.parse_args()

    configure_torch()
    print(f"ENGINE Loading Stable Audio 3 {args.model}...", flush=True)
    t0 = time.time()
    model = _load_model_low_commit(args.model)   # v31.30.38: CPU-first load, ~12 GB commit peak instead of ~22 GB
    load_seconds = time.time() - t0
    # the checkpoint is materialised in fp32 (~6.5 GB) and converted to fp16: without this the freed fp32
    # blocks stay cached (7.7 GB reserved) and every job's activations spill into shared memory
    try:
        torch.cuda.empty_cache()
    except Exception:
        pass
    try:
        frac = float(os.environ.get("JOY_SA3_MEM_FRACTION", "0.82") or 0.0)
        if torch.cuda.is_available() and 0.2 < frac < 1.0:
            torch.cuda.set_per_process_memory_fraction(frac, 0)   # spill -> OOM -> chunked decode, never paging
    except Exception:
        pass
    mem = ""
    try:
        if torch.cuda.is_available():
            mem = f" alloc={torch.cuda.memory_allocated() / 2 ** 20:.0f}MB reserved={torch.cuda.memory_reserved() / 2 ** 20:.0f}MB"
    except Exception:
        pass
    print(f"ENGINE READY model={args.model} load={load_seconds:.2f}s{mem}", flush=True)

    generation_lock = threading.Lock()
    state = {"device": "cuda"}

    def move_model(target: str):
        target = str(target)
        if state["device"] == target:
            return
        t0 = time.time()
        print(f"ENGINE moving model {state['device']} -> {target}", flush=True)
        model.model.to(target)
        model.device = target
        state["device"] = target
        if target == "cpu":
            torch.cuda.empty_cache()
        elif target == "cuda":
            torch.cuda.synchronize()
        print(f"ENGINE model now on {target} in {time.time()-t0:.2f}s", flush=True)

    class Handler(BaseHTTPRequestHandler):
        server_version = "SonicloomSA3/1.0"

        def log_message(self, fmt, *a):
            print("HTTP " + (fmt % a), flush=True)

        def _json(self, status: int, data: dict):
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == "/health":
                self._json(200, {
                    "ok": True,
                    "state": "ready" if not generation_lock.locked() else "busy",
                    "model": args.model,
                    "load_seconds": round(load_seconds, 2),
                    "cuda": torch.cuda.is_available(),
                    "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
                    "device": state["device"],
                })
            else:
                self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if self.path in {"/offload", "/activate"}:
                target = "cpu" if self.path == "/offload" else "cuda"
                try:
                    with generation_lock:
                        move_model(target)
                    self._json(200, {"ok": True, "state": "ready", "device": state["device"], "model": args.model})
                except Exception as e:
                    traceback.print_exc()
                    self._json(500, {"ok": False, "error": str(e), "device": state["device"]})
                return

            if self.path != "/run":
                self._json(404, {"ok": False, "error": "not found"})
                return

            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n <= 0 or n > 10 * 1024 * 1024:
                    raise ValueError("invalid manifest size")
                cfg = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception as e:
                self._json(400, {"ok": False, "error": str(e)})
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()

            def send_event(obj):
                try:
                    self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    # Keep processing the local job even if the web-side reader
                    # disconnects; output paths are already part of the manifest.
                    pass

            send_event({"type": "engine", "state": "ready", "message": "Persistent SA3 Medium already loaded"})
            with generation_lock:
                if state["device"] != "cuda":
                    move_model("cuda")
                old_emit = core.emit

                def stream_emit(stage: str, progress: float, message: str, **extra):
                    payload = {"type": "progress", "stage": stage, "progress": float(progress), "message": message, **extra}
                    print("PROGRESS " + json.dumps(payload, ensure_ascii=False), flush=True)
                    send_event(payload)

                core.emit = stream_emit
                try:
                    tasks = cfg.get("tasks", [])
                    if not tasks:
                        raise RuntimeError("No edit tasks in manifest")
                    for idx, task in enumerate(tasks):
                        core.edit_long(model, task, cfg, idx, len(tasks))
                    send_event({"type": "done", "ok": True, "message": "Stable Audio edits complete"})
                    # v31.22.3: release cached activations after every job so the resident set
                    # (weights + a few hundred MB) never spills into shared memory
                    try:
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
                except Exception as e:
                    traceback.print_exc()
                    # Free only failed transient allocations; model stays resident.
                    try:
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
                    send_event({"type": "error", "ok": False, "error": str(e), "error_type": type(e).__name__})
                finally:
                    core.emit = old_emit

    # HTTPServer intentionally serializes requests. One GPU/model means one
    # generation at a time, preventing concurrent jobs from fighting for VRAM.
    class ReusableHTTPServer(HTTPServer):
        allow_reuse_address = True

    server = ReusableHTTPServer((args.host, args.port), Handler)
    print(f"ENGINE Listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
