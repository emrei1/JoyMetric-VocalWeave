"""JoyMetric v31.28 - persistent vocal separation worker (Mel-Band RoFormer "kim vocals", the Home tab's model).

POST /separate  {audio_b64: float32 interleaved stereo, sample_rate}  ->  {vocals_b64 (instrumental = mix - vocals),
                 vocal_ratio, activity[] (per second, 0/1), ms}     (both stems at the input sample rate)
GET  /health
The model stays resident on the GPU (~2 GB peak for 16 s); one job at a time.
"""
from __future__ import annotations

import argparse
import os
import base64
import json
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np

MODEL_SR = 44100


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--model", default="melband-roformer-kim-vocals")
    args = ap.parse_args()
    state = {"state": "loading", "error": ""}
    print("SEP loading", args.model, flush=True)
    t0 = time.time()
    import torch
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
    from scipy.signal import resample_poly
    from mel_band_roformer.clean_api import MelBandRoformerSession
    sess = MelBandRoformerSession(model_name=args.model, device="cuda" if torch.cuda.is_available() else "cpu", backend="torch", progress=False).load()
    backend = sess._backend
    try:
        sess._config.inference.num_overlap = int(os.environ.get("JOY_SEP_OVERLAP", "1"))   # v31.30.11: 1 instead of 2 - 2.6-4x faster on battery, output correlation 0.999
    except Exception:
        pass
    load_s = time.time() - t0; state["state"] = "ready"
    print("SEP READY load=%.1fs device=%s" % (load_s, backend.resolved_device), flush=True)
    lock = threading.Lock()

    def separate(x48: np.ndarray, sr: int):
        """x48: (n, 2) float32 -> (vocals (n, 2), instrumental (n, 2)) at `sr`."""
        x = np.asarray(x48, dtype=np.float32)
        if x.ndim == 1:
            x = np.stack([x, x], axis=1)
        n = x.shape[0]
        if sr != MODEL_SR:
            xm = resample_poly(x, MODEL_SR, sr, axis=0).astype(np.float32)
        else:
            xm = x
        stems = backend.separate(np.ascontiguousarray(xm.T))
        voc = np.asarray(stems.get("vocals"), dtype=np.float32).T
        if sr != MODEL_SR:
            voc = resample_poly(voc, sr, MODEL_SR, axis=0).astype(np.float32)
        if voc.shape[0] < n:
            voc = np.concatenate([voc, np.zeros((n - voc.shape[0], voc.shape[1]), np.float32)], axis=0)
        voc = voc[:n]
        inst = (x - voc).astype(np.float32)
        try:
            torch.cuda.empty_cache()          # weights stay; the activations go back to the pool for Stable Audio
        except Exception:
            pass
        return voc, inst

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

        def do_GET(self):
            self._json(200, {"ok": True, "state": state["state"], "model": args.model, "cuda": torch.cuda.is_available(), "load_seconds": round(load_s, 1), "busy": lock.locked()})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            if self.path != "/separate_raw":
                try:
                    req = json.loads(raw.decode("utf-8")) if n else {}
                except Exception as exc:
                    self._json(400, {"ok": False, "error": str(exc)}); return
            if self.path == "/separate_raw":
                # v31.30.2: raw float32 interleaved stereo in, raw float32 vocals out; stats in headers.  No base64, no JSON:
                # the app's audio thread shares the GIL with the caller, and parsing 16 MB of JSON there caused xruns.
                t1 = time.time()
                with lock:
                    try:
                        sr = int(self.headers.get("X-Sample-Rate") or 48000)
                        x = np.frombuffer(raw, dtype=np.float32).reshape(-1, 2)
                        voc, inst = separate(x, sr)
                        def rms(a): return float(np.sqrt(np.mean(np.asarray(a, np.float64) ** 2) + 1e-12))
                        sec = max(1, sr); acts = []
                        for s0 in range(0, x.shape[0] - sec // 2, sec):
                            acts.append("1" if rms(voc[s0:s0 + sec]) > 0.25 * rms(x[s0:s0 + sec]) + 1e-4 else "0")
                        body = np.ascontiguousarray(voc, dtype=np.float32).tobytes()
                        self.send_response(200); self.send_header("Content-Type", "application/octet-stream"); self.send_header("Content-Length", str(len(body)))
                        self.send_header("X-Vocal-Ratio", "%.4f" % (rms(voc) / (rms(x) + 1e-9))); self.send_header("X-Activity", ",".join(acts)); self.send_header("X-Ms", str(int((time.time() - t1) * 1000)))
                        self.end_headers(); self.wfile.write(body)
                    except Exception as exc:
                        traceback.print_exc(); self._json(500, {"ok": False, "error": str(exc)[:200]})
                return
            if self.path != "/separate":
                self._json(404, {"ok": False, "error": "unknown path"}); return
            t1 = time.time()
            with lock:
                try:
                    sr = int(req.get("sample_rate", 48000))
                    x = np.frombuffer(base64.b64decode(req["audio_b64"]), dtype=np.float32).reshape(-1, 2)
                    voc, inst = separate(x, sr)
                    def rms(a): return float(np.sqrt(np.mean(np.asarray(a, np.float64) ** 2) + 1e-12))
                    sec = max(1, sr); acts = []
                    for s in range(0, x.shape[0] - sec // 2, sec):
                        acts.append(1 if rms(voc[s:s + sec]) > 0.25 * rms(x[s:s + sec]) + 1e-4 else 0)
                    self._json(200, {"ok": True, "vocals_b64": base64.b64encode(np.ascontiguousarray(voc, dtype=np.float32).tobytes()).decode("ascii"),
                                     "vocal_ratio": rms(voc) / (rms(x) + 1e-9), "activity": acts, "ms": int((time.time() - t1) * 1000)})
                except Exception as exc:
                    traceback.print_exc(); self._json(500, {"ok": False, "error": str(exc)[:200]})

    class Server(HTTPServer):
        allow_reuse_address = True

    srv = Server((args.host, args.port), Handler)
    print("SEP listening on http://%s:%d" % (args.host, args.port), flush=True)
    srv.serve_forever(poll_interval=0.25)


if __name__ == "__main__":
    main()
