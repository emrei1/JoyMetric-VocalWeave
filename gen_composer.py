"""JoyMetric v31.27 - GenComposer v3: the song, in full resolution, -> SongMind -> a part that belongs to it.

For a 4-bar window of the record (the 2 bars just before the block's bars + the 2 bars that will play):
  1. LINES     LineFinder transcribes the 4 bars (Basic Pitch) into bass MIDI, top-line MIDI, inner voices,
               onsets, loudness per sixteenth; GrooveNet adds the drum grid = the 135-number cell context
               SongMind was trained on (Lakh MIDI, melodic targets only);
  2. WRITE     the SongMind Transformer (GPU worker, /songmind) writes K parts over the 4 bars with OUR
               previous part forced as the first 2 bars (prefix) when the windows are contiguous, so the
               new bars continue a phrase; masks: chord / key pitch classes per cell, consonance against
               the record's top line at onsets, register per role, repetition penalty, minimum note length;
               conditions: family (synth lead / pad), genre, density bucket (creativity), register, relation;
  3. CHOOSE    SongMind's log-likelihood + GrooveNet placement fit + a melodic post-filter (repeated-pitch
               ratio, distinct pitches) rank the K parts; the last 2 bars of the winner are the block's notes;
  4. RENDER    quantised with the record's micro-timing, velocities from GrooveNet, the CLAP-selected synth,
               GrooveNet level / tilt; in-key and play-time gates still apply;
  5. REPLACE   when the planner asks, the record's top-line notes in the span are handed back for the line duck.
Fallback: the numpy MelodyNet (GRU, 2 bars) when the worker is down.  Nothing melodic reaches the
speakers that was not written for these bars.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

import numpy as np

from synth_rack import chord_pcs, key_pcs, render_block, rms_hp
from synth_weave import allowed_pitch_classes, chroma_agreement, tonal_chroma

GM_PART = {"pad": 89, "pluck": 81}
FAMILY = {"pad": 8, "pluck": 7}
V = 39; PITCH_LO = 48


def tokens_to_notes(tokens, cells_per_beat: int = 4):
    notes = []; cur = None
    for c, tk in enumerate(tokens):
        tk = int(tk)
        if tk >= 2:
            if cur is not None:
                notes.append(cur)
            cur = [c / cells_per_beat, 1.0 / cells_per_beat, PITCH_LO + tk - 2]
        elif tk == 1 and cur is not None:
            cur[1] += 1.0 / cells_per_beat
        else:
            if cur is not None:
                notes.append(cur); cur = None
    if cur is not None:
        notes.append(cur)
    return [tuple(n) for n in notes]


def notes_to_tokens(notes, cells: int = 64, cells_per_beat: int = 4):
    toks = np.zeros(cells, dtype=np.int64)
    for (t, d, p) in notes:
        c0 = int(round(float(t) * cells_per_beat)); c1 = c0 + max(1, int(round(float(d) * cells_per_beat))); p = int(p)
        while p < PITCH_LO: p += 12
        while p > PITCH_LO + V - 3: p -= 12
        if 0 <= c0 < cells:
            toks[c0] = 2 + (p - PITCH_LO)
            for c in range(c0 + 1, min(cells, c1)):
                toks[c] = 1
    return toks


def melodic_stats(notes):
    ps = [int(p) for t, d, p in notes]
    if len(ps) < 2:
        return {"n": len(ps), "distinct": len(set(ps)), "repeat": 0.0, "mean_iv": 0.0}
    rep = sum(1 for i in range(1, len(ps)) if ps[i] == ps[i - 1]) / (len(ps) - 1)
    ivs = [abs(ps[i] - ps[i - 1]) for i in range(1, len(ps))]
    return {"n": len(ps), "distinct": len(set(ps)), "repeat": rep, "mean_iv": float(np.mean(ivs))}


class GenComposer:
    def __init__(self, sr: int = 48000, url: str = "http://127.0.0.1:8769", candidates: int = 1, timeout: float = 25.0, model_path: str | None = None):
        self.sr = int(sr); self.url = url.rstrip("/"); self.candidates = int(candidates); self.timeout = float(timeout)
        self.enabled = False; self.busy = False; self.count = 0; self.last_ms = 0; self._last_health = 0.0; self.error = ""
        self._lock = threading.Lock(); self.prev_parts = {}; self.last = {}
        self.window_cache = {}; self.level_ema = {}
        self.amt = False; self.songmind = False
        try:
            from melody_net import MelodyNet
            self.net = MelodyNet(model_path); self.net_ready = True
        except Exception as exc:
            self.net = None; self.net_ready = False; self.error = "melodynet: " + str(exc)[:80]
        try:
            from line_finder import LineFinder
            self.lines = LineFinder(self.sr)
        except Exception:
            self.lines = None
        self.genre_id = 13
        self.health()

    # ------------------------------------------------------------------ availability
    def health(self) -> bool:
        self._last_health = time.time()
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=3) as r:
                h = json.loads(r.read().decode("utf-8"))
            self.amt = bool(h.get("ok")) and h.get("state") == "ready"; self.songmind = bool(h.get("songmind"))
        except Exception:
            self.amt = False; self.songmind = False
        self.enabled = bool(self.songmind or self.net_ready)
        return self.enabled

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def request(self, spec: dict, seq: int, on_done) -> bool:
        if not self.songmind and time.time() - self._last_health > 20.0:
            self.health()
        with self._lock:
            if self.busy or not self.enabled:
                return False
            self.busy = True
        threading.Thread(target=self._run, args=(dict(spec), int(seq), on_done), daemon=True, name="joymetric-gen-composer").start()
        return True

    @staticmethod
    def _below_normal():
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -1)
        except Exception:
            pass

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def allowed_per_cell(chords_beats, scale, offset_beats: float = 0.0, cells: int = 64, cells_per_beat: int = 4):
        """allowed pitch classes per cell: chord tones on the beats, chord + key in between; key where no chord is known."""
        out = []
        for c in range(cells):
            b = c / cells_per_beat - offset_beats
            pcs = None
            for (b0, b1, root, quality) in chords_beats:
                if b0 - 1e-6 <= b < b1:
                    pcs = set(chord_pcs(int(root), str(quality))); break
            if pcs is None:
                out.append(set(scale) if scale else None); continue
            out.append(set(pcs) if (c % 4 == 0) else (set(pcs) | set(scale)))
        return out

    @staticmethod
    def quantise(notes, mt=None, cells_per_beat=4, max_dur_beats=4.0):
        out = []
        for t, d, p in notes:
            c = int(round(float(t) * cells_per_beat))
            if c < 0 or c >= 32:
                continue
            off = float(mt[c]) / cells_per_beat if mt is not None and len(mt) > c else 0.0
            out.append((c / cells_per_beat + off, min(max_dur_beats, max(1.0 / cells_per_beat, float(d))), int(p), c))
        return out

    @staticmethod
    def cond_vector(role, genre_id, cr, relation=0):
        c = np.zeros(35, np.float32)
        c[FAMILY.get(role, 7)] = 1.0; c[10 + int(genre_id) % 14] = 1.0
        dens = 1 if cr < 0.35 else (2 if cr < 0.75 else 3)
        c[24 + dens] = 1.0
        c[29 + (1 if role == "pad" else 2)] = 1.0
        c[32 + int(relation)] = 1.0
        return c

    # ------------------------------------------------------------------ the request
    def _run(self, spec, seq, on_done):
        self._below_normal()
        t0 = time.time(); role = str(spec.get("role", "pad")); meta = {"seq": seq, "kind": "gen", "role": role, "bpm": spec.get("bpm")}
        buf = None
        try:
            bpm = float(spec["bpm"]); beat_s = 60.0 / max(40.0, bpm); cr = float(spec.get("creativity", 0.5))
            scale = set(spec.get("scale") or key_pcs(str(spec.get("key") or "")) or range(12))
            chords = list(spec.get("chords_beats") or []) or [(0.0, 8.0, 9, "min")]          # beats relative to the next-2-bar window start
            on_prob = spec.get("on_prob"); drums2 = None
            if spec.get("src_kick") is not None:
                drums2 = np.stack([np.asarray(spec["src_kick"]), np.asarray(spec["src_snare"]), np.asarray(spec["src_hat"])], axis=1)
            m0 = float(spec.get("window_beat0", 0.0)); b0 = float(spec.get("b0", 0.0)); n = int(spec["n"]); beat_n = beat_s * self.sr
            key_ = (role, int(round(m0 * 4)))
            cached = self.window_cache.get(key_)
            lines = None
            if cached is not None and time.time() - cached["t"] < 40.0:
                notes2 = cached["notes"]; lines = cached["lines"]; meta.update(cached["meta"]); meta["source"] = cached["source"] + "(cached)"
            else:
                cached = None
                from line_finder import LineFinder
                win4 = spec.get("src_window4"); win2 = spec.get("src_window")
                X = None
                if self.lines is not None and self.lines.ready and win4 is not None:
                    try:
                        drums4 = np.concatenate([np.zeros((32, 3), np.float32), drums2], axis=0) if drums2 is not None else None
                        lines = self.lines.analyse_v3(win4, self.sr, bpm, drums=drums4, key_pcs=sorted(scale)); X = lines["X"]
                    except Exception as exc:
                        meta["lines_error"] = str(exc)[:80]
                genre_id = int(spec.get("genre_id", self.genre_id))
                allowed = self.allowed_per_cell(chords, scale, offset_beats=8.0)
                top4 = lines["voices"]["top"] if lines is not None else np.full(64, -1)
                bass4 = lines["voices"]["bass"] if lines is not None else np.full(64, -1)
                for c in range(64):
                    if int(bass4[c]) >= 0 and allowed[c] is not None:
                        allowed[c] = set(allowed[c]) | {int(bass4[c]) % 12}
                lo, hi = (52, 74) if role == "pad" else (57, 79)
                min_hold = 2 if role == "pad" else 1
                cands = []; scores = []
                prev = self.prev_parts.get("any") or self.prev_parts.get(role); prefix = None
                if prev is not None and abs(float(prev["window_beat0"]) + 8.0 - m0) < 1e-6:
                    prefix = [int(q) for q in notes_to_tokens(prev["notes"], cells=32)]           # the phrase continues, whichever role plays it
                # 2. SongMind (GPU worker) writes K parts for the 4 bars
                if self.songmind and X is not None:
                    try:
                        cond = self.cond_vector(role, genre_id, cr, relation=0)
                        body = {"x": X.tolist(), "cond": cond.tolist(), "prefix": prefix, "allowed": [sorted(a) if a is not None else None for a in allowed],
                                "top_line": [int(q) for q in top4], "pitch_lo": lo, "pitch_hi": hi, "temperature": 0.9 + 0.2 * cr, "top_p": 0.92,
                                "rep_penalty": 1.6, "min_hold": min_hold, "K": 4, "seed": int(seq), "consonance": bool(spec.get("consonance", True))}
                        out = self._post("/songmind", body)
                        for toks, lp in zip(out.get("tokens") or [], out.get("logp") or []):
                            notes64 = tokens_to_notes(np.asarray(toks, dtype=np.int64))
                            notes_new = [(t - 8.0, d, p) for (t, d, p) in notes64 if t >= 8.0 - 1e-6]
                            cands.append((notes_new, "songmind", float(lp)))
                        meta["songmind_ms"] = out.get("ms")
                    except Exception as exc:
                        meta["songmind_error"] = str(exc)[:80]; self.songmind = False
                # fallback: the numpy GRU on the next 2 bars
                if not cands and self.net_ready and win2 is not None and self.lines is not None and self.lines.ready:
                    try:
                        from melody_net import MelodyNet
                        l2 = self.lines.analyse(win2, self.sr, bpm, drums=drums2, key_pcs=sorted(scale))
                        cond2 = MelodyNet.cond_vector(FAMILY.get(role, 7), genre_id)
                        for k in range(3):
                            toks = self.net.sample(l2["X"], cond2, allowed_pcs=allowed[32:], top_line=l2["voices"]["top"], pitch_lo=lo, pitch_hi=hi, temperature=0.9, top_p=0.92, seed=int(seq) * 13 + k)
                            nts = self.net.tokens_to_notes(toks)
                            if nts:
                                cands.append((nts, "melodynet", float(self.net.score(l2["X"], cond2, toks))))
                        if lines is None:
                            lines = {"voices": {"top": np.concatenate([np.full(32, -1), l2["voices"]["top"]]), "bass": np.concatenate([np.full(32, -1), l2["voices"]["bass"]])}, "notes": l2["notes"],
                                     "top_notes": [(t + 8.0, d, m) for (t, d, m) in l2["voices"]["top_notes"]]}
                    except Exception as exc:
                        meta["melodynet_error"] = str(exc)[:80]
                if not cands:
                    raise RuntimeError("no generator wrote a part")
                # 3. choose
                target_n = int(round(((3 + 4 * cr) if role == "pad" else (4 + 6 * cr)) * (1.0 + 0.3 * max(0.0, (cr - 0.8) / 0.2))))
                for notes_new, src_, lp in cands:
                    st = melodic_stats(notes_new)
                    fit = float(np.mean([float(on_prob[min(31, max(0, int(round(t * 4))))]) for t, d, p in notes_new])) if (on_prob is not None and len(on_prob) >= 32 and notes_new) else 0.0
                    pen = 0.0
                    if st["n"] == 0:
                        pen -= 5.0
                    if st["repeat"] > 0.5:
                        pen -= 8.0 * (st["repeat"] - 0.5)
                    if st["n"] >= 3 and st["distinct"] < 3:
                        pen -= 1.5
                    scores.append(1.5 * lp + 1.0 * fit - abs(st["n"] - target_n) / max(1.0, target_n) + pen)
                best = int(np.argmax(scores)); notes2, src, lp_best = cands[best]
                self.prev_parts[role] = {"window_beat0": m0, "notes": list(notes2)}; self.prev_parts["any"] = {"window_beat0": m0, "notes": list(notes2)}
                meta.update({"source": src, "candidates": [(len(c[0]), c[1]) for c in cands], "scores": [round(float(v), 3) for v in scores], "chosen": best,
                             "prefixed": bool(prefix), "notes": len(notes2), "stats": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in melodic_stats(notes2).items()},
                             "lines": ({"bass": int((bass4 >= 0).sum()), "top": int((top4 >= 0).sum()), "notes": len(lines.get("notes", []))} if lines is not None else None), "genre_id": genre_id})
                self.window_cache[key_] = {"t": time.time(), "notes": notes2, "lines": lines, "source": src,
                                           "meta": {k: meta[k] for k in ("candidates", "scores", "chosen", "prefixed", "notes", "stats", "lines", "genre_id") if k in meta}}
                if len(self.window_cache) > 8:
                    for kk in sorted(self.window_cache, key=lambda q: self.window_cache[q]["t"])[:-8]:
                        self.window_cache.pop(kk, None)
            # 4. this block's notes, quantised with the record's micro-timing; full note lengths
            q = self.quantise(notes2, spec.get("mt"), max_dur_beats=4.0 if role == "pad" else 2.0)
            rack_events = []; last_end = 0
            for (t_rel, dur, pitch, cell) in q:
                pos = int(round((m0 + t_rel - b0) * beat_n))
                if pos < 0 or pos >= n:
                    continue
                vel = 0.6 + 0.35 * float(on_prob[cell]) if (on_prob is not None and 0 <= cell < len(on_prob)) else 0.8
                gate = int(dur * beat_n); rack_events.append((pos, gate, int(pitch), float(vel), None)); last_end = max(last_end, pos + gate)
            n_clip = int(max(n, min(last_end + int(0.3 * self.sr), n + int(8.0 * beat_n))))
            replace = []
            if spec.get("replace_line") and lines is not None:
                for (t, d, m) in lines.get("top_notes", []):
                    tt = t - 8.0
                    if tt >= -1e-6 and b0 - 1e-6 <= m0 + tt < b0 + n / beat_n:
                        replace.append((m0 + tt, d, int(m)))
            meta.update({"in_block": len(rack_events), "replace": replace, "gen_ms": int((time.time() - t0) * 1000)})
            if not rack_events:
                meta["ok"] = True; meta["empty"] = True; buf = None
                raise StopIteration
            # 5. render on the selected synth, level eased between blocks
            lm = float(spec.get("level_mult", 1.0) or 1.0); prev_lm = self.level_ema.get(role)
            if prev_lm is not None:
                lm = 0.5 * prev_lm + 0.5 * lm; lm = float(np.clip(lm, prev_lm / 1.41, prev_lm * 1.41))
            self.level_ema[role] = lm
            rspec = dict(spec); rspec.update({"kind": "score", "events": rack_events, "sr": self.sr, "n": n_clip, "level_mult": lm})
            buf, info = render_block(rspec)
            meta["rms"] = round(rms_hp(buf, self.sr), 4); meta["variant"] = info.get("variant"); meta["chords"] = " | ".join("%d%s" % (r, q_) for _a, _b, r, q_ in chords)[:40]
            hist = np.zeros(12)
            for (_s, _g, _m, _v, _o) in rack_events:
                hist[int(_m) % 12] += float(_g) * float(_v)
            hist = hist / hist.sum() if hist.sum() > 0 else hist
            meta["note_hist"] = [round(float(v), 4) for v in hist]
            src_clip = spec.get("src_clip")
            if src_clip is not None:
                c_src, ton = tonal_chroma(np.asarray(src_clip, dtype=np.float32), self.sr)
                meta["tonal_src"] = round(float(ton), 3); meta["inkey"] = round(float(hist[allowed_pitch_classes(c_src)].sum()), 3)
                c_out, _ = tonal_chroma(buf, self.sr); meta["harm"] = round(chroma_agreement(c_src, c_out), 3)
            meta["ok"] = True
        except StopIteration:
            pass
        except Exception as exc:
            meta["ok"] = False; meta["error"] = str(exc)[:160]; buf = None
        meta["ms"] = int((time.time() - t0) * 1000); self.last_ms = meta["ms"]; self.last = {k: v for k, v in meta.items() if k not in ("note_hist", "replace")}
        with self._lock:
            self.busy = False; self.count += 1
        try:
            on_done(seq, buf, meta)
        except Exception:
            pass
