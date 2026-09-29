"""JoyMetric v31.25 - persistent Anticipatory Music Transformer worker (symbolic generation, GPU).

Stanford CRFM's AMT (Thickstun et al. 2023, "Anticipatory Music Transformer"), music-small-800k,
trained on 180 k Lakh MIDI files.  The DJ hands it the record's upcoming MELODY and CHORDS as
anticipated controls (plus our own previous part as the prompt) and asks for a NEW part on one
instrument.  Sampling is the library's nucleus loop with two extra masks that make the result
robust for a live DJ:
  * the note token is restricted to ONE instrument (the part we asked for),
  * the pitch is restricted to the pitch classes allowed at that moment (chord tones on the beats,
    the key in between) and to a register.
POST /compose  {start_s, end_s, prompt:[[t,d,instr,pitch]], controls:[[t,d,instr,pitch]], instr, top_p,
               max_events, allowed:[[t0,t1,[pcs]]], pitch_lo, pitch_hi, seed}
GET  /health
"""
from __future__ import annotations

import argparse
import json
import math
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

import sys

import numpy as np
import torch
try:
    import ctypes
    _k = ctypes.windll.kernel32; _k.GetCurrentProcess.restype = ctypes.c_void_p; _k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _k.SetPriorityClass(ctypes.c_void_p(_k.GetCurrentProcess()), 0x4000)   # BELOW_NORMAL_PRIORITY_CLASS (v31.30.7: typed handle - the untyped call failed with error 6)
except Exception:
    pass
import torch.nn.functional as F

from anticipation import ops
from anticipation.sample import future_logits, nucleus, safe_logits
from anticipation.vocab import (ANTICIPATE, ATIME_OFFSET, AUTOREGRESS, CONTROL_OFFSET, DUR_OFFSET, MAX_DUR, MAX_PITCH, MAX_TIME,
                                NOTE_OFFSET, TIME_OFFSET)
from anticipation.config import DELTA, TIME_RESOLUTION

MODEL_ID = os.environ.get("JOY_AMT_MODEL", "stanford-crfm/music-small-800k")
SONGMIND_PATH = os.environ.get("JOY_SONGMIND", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", "songmind.pt"))


def events_from_json(items):
    toks = []
    for t, d, instr, pitch in items:
        tt = int(max(0, min(MAX_TIME - 1, round(float(t) * TIME_RESOLUTION))))
        dd = int(max(1, min(MAX_DUR - 1, round(float(d) * TIME_RESOLUTION))))
        nn = int(max(0, min(128, int(instr)))) * MAX_PITCH + int(max(0, min(127, int(pitch))))
        toks.extend([TIME_OFFSET + tt, DUR_OFFSET + dd, NOTE_OFFSET + nn])
    return toks


def allowed_pcs_at(allowed, t_s):
    for t0, t1, pcs in allowed:
        if float(t0) <= t_s < float(t1):
            return set(int(p) % 12 for p in pcs)
    return None


def add_token_masked(model, z, tokens, top_p, current_time, instr, allowed, pitch_lo, pitch_hi):
    """the library's add_token with the instrument / pitch-class / register masks."""
    assert len(tokens) % 3 == 0
    history = tokens.copy()
    lookback = max(len(tokens) - 1017, 0)
    history = history[lookback:]
    offset = ops.min_time(history, seconds=False)
    history[::3] = [tok - offset for tok in history[::3]]
    new_token = []
    with torch.no_grad():
        for i in range(3):
            input_tokens = torch.tensor(z + history + new_token).unsqueeze(0).to(model.device)
            logits = model(input_tokens).logits[0, -1].float()
            idx = input_tokens.shape[1] - 1
            logits = safe_logits(logits, idx)
            if i == 0:
                logits = future_logits(logits, current_time - offset)
            elif i == 2:
                t_abs = (new_token[0] + offset - TIME_OFFSET) / TIME_RESOLUTION
                mask = torch.full_like(logits, -float("inf"))
                base = NOTE_OFFSET + int(instr) * MAX_PITCH
                pcs = allowed_pcs_at(allowed, t_abs) if allowed else None
                for p in range(int(pitch_lo), int(pitch_hi) + 1):
                    if pcs is None or (p % 12) in pcs:
                        mask[base + p] = 0.0
                logits = logits + mask
            logits = nucleus(logits, top_p)
            probs = F.softmax(logits, dim=-1)
            if not torch.isfinite(probs).all() or float(probs.sum()) <= 0:
                probs = torch.nan_to_num(probs, nan=0.0); probs[0] = 1.0 if float(probs.sum()) <= 0 else probs[0]
            token = torch.multinomial(probs, 1)
            new_token.append(int(token))
    new_token[0] += offset
    return new_token


def compose(model, req):
    start_s = float(req.get("start_s", 0.0)); end_s = float(req.get("end_s", 4.0))
    prompt = events_from_json(req.get("prompt") or []); controls_in = events_from_json(req.get("controls") or [])
    instr = int(req.get("instr", 81)); top_p = float(req.get("top_p", 0.95)); max_events = int(req.get("max_events", 48))
    allowed = req.get("allowed") or []; pitch_lo = int(req.get("pitch_lo", 48)); pitch_hi = int(req.get("pitch_hi", 84))
    delta = int(DELTA * TIME_RESOLUTION)
    start_time = int(TIME_RESOLUTION * start_s); end_time = int(TIME_RESOLUTION * end_s)
    inputs = ops.sort(prompt + controls_in)
    seed = req.get("seed")
    if seed is not None:
        torch.manual_seed(int(seed))
    prompt_toks = ops.pad(ops.clip(inputs, 0, start_time, clip_duration=False, seconds=False), start_time)
    future = ops.clip(inputs, start_time + 1, ops.max_time(inputs, seconds=False), clip_duration=False, seconds=False)
    z = [ANTICIPATE] if len(future) > 0 else [AUTOREGRESS]
    tokens, controls = ops.anticipate(prompt_toks, ops.sort([CONTROL_OFFSET + tok for tok in future]))
    current_time = ops.max_time(prompt_toks, seconds=False)
    if controls:
        atime, adur, anote = controls[0:3]; anticipated_tokens = controls[3:]; anticipated_time = atime - ATIME_OFFSET
    else:
        anticipated_time = math.inf
    out = []
    while len(out) < max_events:
        while current_time >= anticipated_time - delta:
            tokens.extend([atime, adur, anote])
            if len(anticipated_tokens) > 0:
                atime, adur, anote = anticipated_tokens[0:3]; anticipated_tokens = anticipated_tokens[3:]; anticipated_time = atime - ATIME_OFFSET
            else:
                anticipated_time = math.inf
        new_token = add_token_masked(model, z, tokens, top_p, max(start_time, current_time), instr, allowed, pitch_lo, pitch_hi)
        new_time = new_token[0] - TIME_OFFSET
        if new_time >= end_time:
            break
        tokens.extend(new_token)
        dt = new_time - current_time
        if dt < 0:
            break
        current_time = new_time
        note = new_token[2] - NOTE_OFFSET
        out.append([new_time / TIME_RESOLUTION, (new_token[1] - DUR_OFFSET) / TIME_RESOLUTION, note % MAX_PITCH])
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=8769)
    args = ap.parse_args()
    state = {"state": "loading", "error": None}
    print(f"AMT loading {MODEL_ID}...", flush=True)
    t0 = time.time()
    from transformers import AutoModelForCausalLM
    model = AutoModelForCausalLM.from_pretrained(MODEL_ID)
    model = model.to("cuda" if torch.cuda.is_available() else "cpu").half().eval() if torch.cuda.is_available() else model.eval()
    state["state"] = "ready"; load_s = time.time() - t0
    print(f"AMT READY load={load_s:.1f}s device={model.device}", flush=True)
    lock = threading.Lock()
    # v31.27 SongMind: OUR part-writing Transformer (trained on the Lakh corpus), served on the same GPU
    songmind = None; sm_error = ""
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from songmind_model import SongMind
        if os.path.exists(SONGMIND_PATH):
            ck = torch.load(SONGMIND_PATH, map_location="cpu")
            songmind = SongMind(**(ck.get("config") or {})); songmind.load_state_dict(ck["state_dict"]); songmind = songmind.to(model.device).eval()
            print(f"SONGMIND READY {SONGMIND_PATH}", flush=True)
        else:
            sm_error = "no songmind.pt"
    except Exception as exc:
        sm_error = str(exc)[:120]; print("SONGMIND unavailable:", sm_error, flush=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

        def do_GET(self):
            self._json(200, {"ok": True, "state": state["state"], "model": MODEL_ID, "cuda": torch.cuda.is_available(), "load_seconds": round(load_s, 1), "busy": lock.locked(),
                             "songmind": songmind is not None, "songmind_error": sm_error})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            except Exception as exc:
                self._json(400, {"ok": False, "error": str(exc)}); return
            if self.path in ("/songmind", "/songmind_score"):
                if songmind is None:
                    self._json(503, {"ok": False, "error": "songmind not loaded: " + sm_error}); return
                t1 = time.time()
                with lock:
                    try:
                        x = np.asarray(req["x"], dtype=np.float32); c = np.asarray(req["cond"], dtype=np.float32)
                        if self.path == "/songmind_score":
                            sc = songmind.score(x, c, np.asarray(req["tokens"], dtype=np.int64))
                            self._json(200, {"ok": True, "scores": [float(v) for v in sc], "ms": int((time.time() - t1) * 1000)}); return
                        allowed = req.get("allowed")
                        allowed = [set(int(q) for q in a) if a is not None else None for a in allowed] if allowed is not None else None
                        top = np.asarray(req.get("top_line") or [-1] * 64, dtype=np.int64)
                        toks, lp = songmind.write(x, c, prefix=req.get("prefix"), allowed=allowed, top_line=top, pitch_lo=int(req.get("pitch_lo", 52)), pitch_hi=int(req.get("pitch_hi", 79)),
                                                  temperature=float(req.get("temperature", 1.0)), top_p=float(req.get("top_p", 0.92)), rep_penalty=float(req.get("rep_penalty", 1.6)),
                                                  min_hold=int(req.get("min_hold", 1)), K=int(req.get("K", 4)), seed=int(req.get("seed", 0)), consonance=bool(req.get("consonance", True)))
                        self._json(200, {"ok": True, "tokens": toks.tolist(), "logp": [float(v) for v in lp], "ms": int((time.time() - t1) * 1000)})
                    except Exception as exc:
                        traceback.print_exc(); self._json(500, {"ok": False, "error": str(exc)[:200]})
                return
            if self.path != "/compose":
                self._json(404, {"ok": False, "error": "unknown path"}); return
            t1 = time.time()
            with lock:
                try:
                    events = compose(model, req)
                    self._json(200, {"ok": True, "events": events, "n": len(events), "ms": int((time.time() - t1) * 1000)})
                except Exception as exc:
                    traceback.print_exc()
                    self._json(500, {"ok": False, "error": str(exc)[:200]})

    class Server(HTTPServer):
        allow_reuse_address = True

    srv = Server((args.host, args.port), Handler)
    print(f"AMT listening on http://{args.host}:{args.port}", flush=True)
    srv.serve_forever(poll_interval=0.25)


if __name__ == "__main__":
    main()
