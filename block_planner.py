"""JoyMetric v31.23 RackMind - BlockPlanner.

The 16 s lookahead is divided into 8 blocks of 2 s (absolute stream frames, block k = [k*B, (k+1)*B)).
A block is DESCRIBED as soon as it is completely inside the ring (cheap spectral descriptors + per-beat
chords), and it is PLANNED when the block after it is described too: the decision for block k looks at
the plan of k-1 (continuity) and the descriptors of k-1, k, k+1 (anticipation) and picks a DJ character:

  TRANSITION  a new record starts inside the block: our pad swells first, the record returns under it
  DROP        the record's energy jumps at a detected downbeat: the drop's first hit is FROZEN (rolled)
              for 2-4 beats while the pad and the drummer keep the rhythm, then the record is released
  BUILD       the block(s) before a drop / a riser: pad swell + arp opening its filter, riser, snare rush
  BREAK       energy falls: the programme's melody is taken over by the pad (mid/high centre duck)
  FLOW        a lead vocal: a soft pad underneath, nothing takes the front
  GROOVE      steady material: pad / arp / pluck / lead rotation, prominence scaled by creativity

Every layer is rendered by the SynthRack on the absolute beat grid and scheduled at absolute render
frames, so block boundaries are inaudible; the takeover / freeze curves are frame-scheduled too.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.signal import butter, sosfilt

from synth_rack import estimate_chords
from synth_weave import tonal_chroma

CHARACTERS = ("TRANSITION", "DROP", "BUILD", "BREAK", "FLOW", "PEAK", "COLOR", "GROOVE")


def _band_rms(spec_pow, freqs, lo, hi, n):
    m = (freqs >= lo) & (freqs < hi)
    return float(math.sqrt(max(0.0, float(spec_pow[m].sum())) / max(1, n)))


def describe_block(x, sr):
    """cheap descriptors of one 2 s block (stereo float32)."""
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 1:
        x = np.stack([x, x], axis=1)
    n = int(x.shape[0])
    mono = x.mean(axis=1)
    rms = float(np.sqrt(np.mean(mono * mono) + 1e-12))
    win = np.hanning(n).astype(np.float32)
    spec = np.fft.rfft(mono * win)
    pw = (np.abs(spec) ** 2) * (8.0 / 3.0) / n            # Hann power correction
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    low = _band_rms(pw, freqs, 20.0, 150.0, n); mid = _band_rms(pw, freqs, 150.0, 2500.0, n); high = _band_rms(pw, freqs, 2500.0, 16000.0, n)
    tot = float(pw.sum()) + 1e-12
    centroid = float((pw * freqs).sum() / tot)
    # centre ("vocal-ish") energy in 300-3500 Hz: mid channel vs side channel
    m_ch = 0.5 * (x[:, 0] + x[:, 1]); s_ch = 0.5 * (x[:, 0] - x[:, 1])
    sos = butter(2, [300.0 / (sr / 2.0), min(0.45, 3500.0 / (sr / 2.0))], btype="band", output="sos")
    mb = sosfilt(sos, m_ch); sb = sosfilt(sos, s_ch)
    em = float(np.mean(mb * mb) + 1e-12); es = float(np.mean(sb * sb) + 1e-12)
    centre = float(em / (em + es))
    # onset density: half-wave rectified spectral flux peaks (1024 hop), per second
    hop = 1024; nfft = 2048
    frames = max(1, (n - nfft) // hop)
    mags = np.empty((frames, nfft // 2 + 1), np.float32)
    w2 = np.hanning(nfft).astype(np.float32)
    for i in range(frames):
        mags[i] = np.abs(np.fft.rfft(mono[i * hop:i * hop + nfft] * w2))
    flux = np.maximum(0.0, np.diff(mags, axis=0)).sum(axis=1) if frames > 1 else np.zeros(1)
    thr = 2.0 * float(np.median(flux)) + 1e-6
    peaks = 0
    for i in range(1, len(flux) - 1):
        if flux[i] > thr and flux[i] >= flux[i - 1] and flux[i] >= flux[i + 1]:
            peaks += 1
    onset_density = float(peaks) / (n / sr)
    # the strongest transient inside the first 1.2 s (drop-onset location, 20 ms grid)
    hit = 0; hit_frame = 0
    seg = int(0.02 * sr); lim = min(n, int(1.2 * sr))
    env = np.array([float(np.sqrt(np.mean(mono[a:a + seg] ** 2) + 1e-12)) for a in range(0, lim - seg, seg)]) if lim > 2 * seg else np.zeros(1)
    if env.size > 2:
        d = np.diff(env); j = int(np.argmax(d)); hit = float(max(d[j], env[0] - env[1:].mean()) / (rms + 1e-6)); hit_frame = int((j + 1) * seg)
    chroma, tonal = tonal_chroma(mono, sr)
    return {"rms": rms, "low": low, "mid": mid, "high": high, "centroid": centroid, "centre": centre,
            "onsets": onset_density, "tonal": float(tonal), "chroma": np.asarray(chroma, dtype=np.float64),
            "hit": float(hit), "hit_frame": int(hit_frame), "env20": env.astype(np.float32),
            "hp_rms": float(np.sqrt(max(1e-12, mid * mid + high * high)))}


class BlockPlanner:
    def __init__(self, sr: int, block_s: float = 2.0, n_blocks: int = 8):
        self.sr = int(sr); self.B = int(round(block_s * sr)); self.n_blocks = int(n_blocks)
        self.desc = {}          # k -> descriptors
        self.plans = {}         # k -> plan
        self.last_takeover_k = -100
        self.last_drop_k = -100
        self.riser_from = None
        self.seed = 0

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def window_len(cr: float) -> int:
        """blocks per 16 s in which the rack may speak at a high: 2 (4 s) normally, 3 from creativity 0.75, 4 (8 s) from 0.9."""
        cr = float(cr)
        return 2 + (2 if cr >= 0.9 else (1 if cr >= 0.75 else 0))

    def block_of(self, frame: int) -> int:
        return int(frame // self.B)

    def plan_at(self, frame: int):
        return self.plans.get(self.block_of(frame))

    def _grid(self, ctx):
        bf = float(ctx.get("beat_frames") or 0.0); anchor = float(ctx.get("anchor_frame") or 0.0)
        ok = bf > 0 and anchor > 0 and float(ctx.get("grid_conf") or 0.0) >= 0.30
        return ok, bf, anchor

    def _beat_bounds(self, k, ctx, snap_start, n_ring):
        """beat windows (ring-relative) covering block k with one beat of context on each side."""
        ok, bf, anchor = self._grid(ctx)
        a = k * self.B; b = (k + 1) * self.B
        if not ok:
            return [(a - snap_start, b - snap_start)], set(), [(a, b)]
        m0 = int(math.floor((a - anchor) / bf)) - 1; m1 = int(math.ceil((b - anchor) / bf)) + 1
        bounds, bars, absb = [], set(), []
        for i, m in enumerate(range(m0, m1)):
            s = int(round(anchor + m * bf)); e = int(round(anchor + (m + 1) * bf))
            if e - snap_start <= 0 or s - snap_start >= n_ring:
                continue
            bounds.append((max(0, s - snap_start), min(n_ring, e - snap_start))); absb.append((s, e))
            if m % 4 == 0:
                bars.add(len(bounds) - 1)
        return bounds, bars, absb

    def _chords_for(self, k, ctx, ring, snap_start):
        bounds, bars, absb = self._beat_bounds(k, ctx, snap_start, len(ring))
        est = estimate_chords(ring, self.sr, bounds, key=str(ctx.get("key") or ""), bar_starts=bars)
        a = k * self.B; b = (k + 1) * self.B
        out = []
        for ch, (s, e) in zip(est, absb):
            s2 = max(a, s); e2 = min(b, e)
            if e2 <= s2:
                continue
            if out and out[-1]["root"] == ch["root"] and out[-1]["quality"] == ch["quality"] and out[-1]["b"] == s2 - a:
                out[-1]["b"] = e2 - a
            else:
                out.append({"a": s2 - a, "b": e2 - a, "root": ch["root"], "quality": ch["quality"], "label": ch["label"], "conf": ch["conf"]})
        if not out:
            out = [{"a": 0, "b": b - a, "root": 9, "quality": "min", "label": "Am", "conf": 0.0}]
        # a pad must not re-harmonise the record beat by beat: segments shorter than two beats are absorbed by
        # the neighbour with the higher confidence (block-edge segments by their only neighbour)
        ok, bf, _anchor = self._grid(ctx)
        if ok and len(out) > 1:
            min_len = int(2.0 * bf) - 2
            changed = True
            while changed and len(out) > 1:
                changed = False
                for i, ch in enumerate(out):
                    if ch["b"] - ch["a"] >= min_len:
                        continue
                    left = out[i - 1] if i > 0 else None; right = out[i + 1] if i + 1 < len(out) else None
                    host = left if (right is None or (left is not None and left["conf"] >= right["conf"])) else right
                    if host is None:
                        break
                    host["a"] = min(host["a"], ch["a"]); host["b"] = max(host["b"], ch["b"])
                    out.pop(i); changed = True
                    break
        return out

    # ------------------------------------------------------------------ update
    def update(self, ring, end_capture: int, render_index: int, ctx: dict):
        """ring: stereo PCM ending at absolute frame `end_capture`.  Returns {"new_plans": [...], "now": plan|None}."""
        ring = np.asarray(ring, dtype=np.float32)
        n_ring = int(ring.shape[0]); snap_start = int(end_capture) - n_ring
        if n_ring < 2 * self.B or end_capture <= 0:
            return {"new_plans": [], "now": self.plan_at(render_index)}
        k_first = self.block_of(snap_start) + 1                        # first complete block in the ring
        k_last = self.block_of(end_capture) - 1                        # last complete block
        change = ctx.get("track_change_frame")
        # 1. describe complete blocks not seen yet
        for k in range(max(k_first, self.block_of(render_index)), k_last + 1):
            if k in self.desc:
                continue
            a = k * self.B - snap_start; b = a + self.B
            if a < 0 or b > n_ring:
                continue
            d = describe_block(ring[a:b], self.sr)
            d["chords"] = self._chords_for(k, ctx, ring, snap_start)
            d["k"] = k
            if change is not None and k * self.B <= int(change) < (k + 1) * self.B:
                d["track_change"] = int(change)
            self.desc[k] = d
        # 2. plan every block whose successor is described and that is still >= 3 s ahead of the render position
        new_plans = []
        min_start = int(render_index) + int(3.0 * self.sr)
        for k in sorted(self.desc):
            if k in self.plans or (k + 1) not in self.desc or (k - 1) not in self.desc:
                continue
            if k * self.B < min_start:
                continue
            plan = self._decide(k, ctx, ring, snap_start)
            self.plans[k] = plan
            new_plans.append(plan)
        # 3. forget the past
        old = self.block_of(render_index) - 4
        for k in [k for k in self.desc if k < old]:
            self.desc.pop(k, None)
        for k in [k for k in self.plans if k < old]:
            self.plans.pop(k, None)
        return {"new_plans": new_plans, "now": self.plan_at(render_index)}

    # ------------------------------------------------------------------ analysis of the neighbourhood
    def _events(self, k, ctx):
        d0 = self.desc.get(k - 1); d1 = self.desc[k]; d2 = self.desc.get(k + 1)
        ev = {"drop": False, "drop_next": False, "riser": False, "breakdown": False, "transition": None, "vocal": False, "drumless": False}
        eps = 1e-6
        if "track_change" in d1:
            ev["transition"] = int(d1["track_change"])
        if d0 is not None:
            r = d1["rms"] / (d0["rms"] + eps); l = d1["low"] / (d0["low"] + eps)
            # a drop is a RHYTHMIC energy jump: the floor (kick / bass) must come in too - a vocal or a pad entering
            # raises the level without touching the low band and is not a drop
            dm = self.desc.get(k - 2)
            sustained = dm is None or dm["rms"] <= 0.85 * d1["rms"]
            loudest = d1["rms"] >= 0.9 * max(q["rms"] for q in self.desc.values())
            drop_like = (l >= 1.6 or (r >= 1.4 and l >= 1.15)) and d1["onsets"] >= 2.0 and d1["hit"] >= 0.15 and sustained and loudest and (k - self.last_drop_k) >= 16
            ev["drop"] = bool(drop_like and ev["transition"] is None)
            ev["breakdown"] = bool(r <= 0.72 and l <= 0.65)
        if d2 is not None:
            r2 = d2["rms"] / (d1["rms"] + eps); l2 = d2["low"] / (d1["low"] + eps)
            loudest2 = d2["rms"] >= 0.9 * max(q["rms"] for q in self.desc.values())
            sustained2 = d0 is None or d0["rms"] <= 0.85 * d2["rms"]
            ev["drop_next"] = bool((l2 >= 1.6 or (r2 >= 1.4 and l2 >= 1.15)) and d2["onsets"] >= 2.0 and d2["hit"] >= 0.15 and "track_change" not in d2 and loudest2 and sustained2 and (k + 1 - self.last_drop_k) >= 16)
        # riser: brightness rising over two blocks while the floor thins
        dm = self.desc.get(k - 2)
        if d0 is not None and dm is not None:
            rising = d1["centroid"] > 1.12 * d0["centroid"] and d1["high"] > 1.15 * d0["high"] and d1["low"] <= 1.05 * d0["low"]
            rising0 = d0["centroid"] > 1.08 * dm["centroid"] and d0["high"] > 1.10 * dm["high"]
            ev["riser"] = bool(rising and rising0 and (d2 is None or d2["centroid"] >= d1["centroid"] or ev["drop_next"]))
        vocal_now = float(ctx.get("vocal") or 0.0)
        ev["vocal"] = bool(d1["centre"] >= 0.62 and d1["tonal"] >= 0.08 and vocal_now >= 0.35)
        ev["drumless"] = bool(d1["onsets"] < 1.2)
        return ev

    def _drop_frame(self, k, ctx):
        """the drop's first hit, snapped to the grid when the grid agrees with the transient."""
        d1 = self.desc[k]; d0 = self.desc.get(k - 1)
        env = np.asarray(d1.get("env20", []), dtype=np.float64); seg = int(0.02 * self.sr)
        raw = k * self.B + int(d1["hit_frame"])
        if env.size > 2:
            floor = float(d0["rms"]) if d0 is not None else float(env.min())
            loud = np.nonzero(env >= max(2.0 * floor, 0.5 * float(env.max())))[0]
            if loud.size:
                raw = k * self.B + int(loud[0]) * seg          # the first loud window IS the drop hit
        ok, bf, anchor = self._grid(ctx)
        if not ok:
            return raw, False
        m = round((raw - anchor) / (4.0 * bf))
        down = int(round(anchor + m * 4.0 * bf))
        if abs(down - raw) <= int(0.12 * self.sr):
            return down, True
        m = round((raw - anchor) / bf)
        beat = int(round(anchor + m * bf))
        if abs(beat - raw) <= int(0.06 * self.sr):
            return beat, True
        return raw, False

    # ------------------------------------------------------------------ decision
    def _decide(self, k, ctx, ring, snap_start):
        d = self.desc[k]; prev = self.plans.get(k - 1)
        ev = self._events(k, ctx)
        cr = float(min(1.0, max(0.0, float(ctx.get("creativity") or 0.0))))
        ok_grid, bf, anchor = self._grid(ctx)
        a = k * self.B; b = (k + 1) * self.B
        style = str(ctx.get("style") or ""); key = str(ctx.get("key") or "")
        bpm = float(ctx.get("bpm") or 120.0)
        ref = float(d["hp_rms"]) if d["hp_rms"] > 1e-4 else max(1e-3, float(d["rms"]) * 0.5)
        b0 = ((a - anchor) / bf) if ok_grid else 0.0
        prev_pad = bool(prev and any(l["kind"] == "pad" and not l.get("fade") for l in prev["layers"]))   # a fading pad does not chain
        common = dict(bpm=bpm, b0=b0, style=style, key=key, energy=float(ctx.get("energy") or 0.5), vocal=float(ctx.get("vocal") or 0.0),
                      drums=0.0 if ev["drumless"] else 1.0, grid=ok_grid, ref_rms=ref, chords=d["chords"], seed=self.seed + k, block_start=a)
        layers = []; takeover = None; bias = {"drum_presence": 1.0, "pump": 0.0, "riser": 0.0, "rush": 0.0, "impact": False}
        char = "GROOVE"

        def layer(kind, level, start=a, n=None, **kw):
            item = dict(common); item.update(kind=kind, level=float(level), start=int(start), n=int(n if n is not None else (b - start)))
            item.update(kw); layers.append(item)

        if ev["transition"] is not None:
            char = "TRANSITION"
            t0 = int(ev["transition"])
            layer("gen", 0.4 + 0.2 * cr, start=t0, n=b - t0, role="pad", swell=True, continuation=False, pump=0.0)   # AI-ONLY: the swell is generated too
            takeover = {"start": t0, "end": t0 + int(1.6 * self.sr), "amount": 0.6 + 0.2 * cr, "ramp_in_s": 0.25, "ramp_out_s": 2.2, "mode": "takeover"}
            bias["drum_presence"] = 0.8
        elif ev["drop"] and ok_grid and cr >= 0.45:
            char = "DROP"
            f0, snapped = self._drop_frame(k, ctx)
            beats = 4 if (cr >= 0.7 and snapped) else 2
            n_freeze = int(round(beats * bf))
            if snapped and f0 + n_freeze <= b + self.B // 2:
                # the record is silenced for the roll; the pad and the drummer carry the rhythm; the record returns
                # exactly on the beat after the gap
                layer("freeze", 1.0, start=f0, n=n_freeze, beats=beats, src_start=f0, src_n=int(round(1.0 * bf)) + int(0.05 * self.sr))
                takeover = {"start": f0, "end": f0 + n_freeze, "amount": 1.0, "ramp_in_s": 0.02, "ramp_out_s": 0.012, "mode": "freeze"}
                if cr >= 0.6 and f0 + n_freeze < b - int(0.3 * self.sr):
                    layer("gen", 0.2 * cr, start=f0 + n_freeze, n=b - (f0 + n_freeze), role="pluck")
                bias.update({"drum_presence": 1.05, "pump": 0.3 * cr, "impact": True})
                self.last_drop_k = k
            else:
                layer("gen", 0.2 * cr, start=a, n=b - a, role="pluck")
                bias.update({"drum_presence": 1.0, "pump": 0.25 * cr, "impact": True})
                self.last_drop_k = k
        elif ev["drop_next"] or ev["riser"]:
            char = "BUILD"
            if ok_grid:
                layer("gen", 0.24 + 0.2 * cr, start=a, n=b - a, role=("pluck" if cr >= 0.55 else "pad"), build=1.0)      # the model writes the build figure
            bias.update({"drum_presence": 1.0, "riser": 0.35 + 0.45 * cr, "rush": 0.4 + 0.5 * cr if ev["drop_next"] else 0.0})
        elif ev["breakdown"] and cr >= 0.5 and not ev["vocal"]:
            char = "BREAK"
            if ok_grid:
                layer("gen", 0.25 + 0.2 * cr, start=a, n=b - a, role="pad", replace_line=True)  # the takeover voice is generated, the record's line steps aside
            amt = 0.3 + 0.4 * cr
            takeover = {"start": a, "end": b, "amount": amt, "ramp_in_s": 0.6, "ramp_out_s": 1.8, "mode": "takeover"}
            bias["drum_presence"] = 0.6
            self.last_takeover_k = k
        elif ev["vocal"]:
            char = "FLOW"
            # AI-ONLY: nothing starts under a vocal
            bias["drum_presence"] = 0.85
        elif ev["drumless"] and d["tonal"] < 0.06:
            char = "GROOVE"
        else:
            # the default is SILENCE: the rack only speaks at the record's highs, and only in short windows -
            # the first two blocks (4 s) of every 16 s while the energy is in the top band
            char = "GROOVE"
            peak_ref = max(q["rms"] for q in self.desc.values())
            d0 = self.desc.get(k - 1)
            is_peak = d["rms"] >= 0.9 * peak_ref and d0 is not None and d0["rms"] >= 0.8 * peak_ref
            window = (k % 8) < self.window_len(cr)
            maxgen = 0.12 * max(0.0, (cr - 0.8) / 0.2)               # MAXGEN: at creativity 1.0 the generated voice is 0.12 louder and appears on two more blocks
            if is_peak and window and cr >= 0.35:
                char = "PEAK"
                if ok_grid:
                    # generative main voice; on the third block of a window at high creativity the record's own line
                    # steps aside (harmonic notches) so ours takes its place
                    layer("gen", 0.22 + 0.2 * cr + maxgen, start=a, n=b - a, role=("pluck" if (k % 8) in (1, 3) else "pad"), replace_line=bool(cr >= 0.75 and ((k % 8) == 2 or (cr >= 0.9 and (k % 8) == 3))))
                # AI-ONLY: no rack bed; without a grid nothing is added
                # the melody takeover: at most once per 32 s, only at a high, only at high creativity, one bar
                if cr >= 0.75 and (k - self.last_takeover_k) >= 16 and ok_grid and (k % 8) == 0:
                    m = math.ceil((a - anchor) / (4.0 * bf)); f0 = int(round(anchor + m * 4.0 * bf))
                    if a <= f0 < b - int(0.5 * self.sr):
                        takeover = {"start": f0, "end": min(b + self.B // 2, f0 + int(round(4.0 * bf))), "amount": 0.3 + 0.3 * cr,
                                    "ramp_in_s": 0.4, "ramp_out_s": 1.4, "mode": "takeover"}
                        self.last_takeover_k = k
            elif cr >= 0.9 and (k % 8) in (5, 6) and not ev["drumless"] and d["tonal"] >= 0.06:
                # creativity max: one short colour touch per 16 s outside the highs (instrument from the CLAP ranking)
                char = "COLOR"
                if ok_grid:
                    layer("gen", 0.16 + 0.12 * cr + 0.5 * maxgen, start=a, n=b - a, role=("pad" if (k % 8) == 5 else "pluck"))
            # AI-ONLY: no rack fade pads
            bias["pump"] = 0.15 * cr if not ev["drumless"] else 0.0
        return {"k": k, "start": a, "end": b, "character": char, "layers": layers, "takeover": takeover, "bias": bias,
                "chords": " | ".join(c["label"] for c in d["chords"]), "events": {kk: (v if not isinstance(v, np.generic) else v.item()) for kk, v in ev.items()},
                "desc": {"rms": round(d["rms"], 4), "low": round(d["low"], 4), "centroid": int(d["centroid"]), "centre": round(d["centre"], 2),
                         "onsets": round(d["onsets"], 1), "tonal": round(d["tonal"], 2)}}
