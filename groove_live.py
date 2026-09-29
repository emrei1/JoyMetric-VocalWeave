"""JoyMetric v31.24 - GrooveLive: GrooveNet on the lookahead (agent side, never the audio thread).

For a 2-bar window of the record that is still ~10 s away from the speakers, the same features the net
was trained on are computed from the lookahead ring (resampled to 22.05 kHz, same STFT), normalised
with a per-session reference, and the net answers: where the harmonic layer hits (per sixteenth), its
micro-timing, the record's swing, the layer's level relative to the drums and its 3-band tilt.
"""
from __future__ import annotations

import time

import numpy as np
from scipy.signal import resample_poly

import groove_features as GF
from groove_net import GrooveNet


class GrooveLive:
    def __init__(self, sr: int = 48000, model_path: str | None = None):
        self.sr = int(sr); self.ref = None; self.last = {}; self.count = 0; self.last_ms = 0
        try:
            self.net = GrooveNet(model_path); self.ready = True; self.error = ""
        except Exception as exc:
            self.net = None; self.ready = False; self.error = str(exc)[:120]

    def window(self, ring, snap_start: int, beats_abs, tempo: float):
        """ring: stereo PCM (48 kHz) whose first sample is absolute frame `snap_start`; beats_abs: 9 consecutive
        beat positions (absolute frames, the first a downbeat).  Returns the net's answer for that window or None."""
        if not self.ready or ring is None:
            return None
        t0 = time.time()
        beats_abs = [int(round(b)) for b in beats_abs]
        try:
            import ctypes; ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -1)
        except Exception:
            pass
        if len(beats_abs) != 9:
            return None
        pad = int(0.25 * self.sr)
        a = max(0, beats_abs[0] - snap_start - pad); b = min(int(ring.shape[0]), beats_abs[8] - snap_start + pad)
        if beats_abs[0] - snap_start < 0 or beats_abs[8] - snap_start > int(ring.shape[0]) or b - a < self.sr:
            return None
        seg = np.asarray(ring[a:b], dtype=np.float32)
        mono = seg.mean(axis=1) if seg.ndim == 2 else seg
        y22 = resample_poly(mono, 147, 320).astype(np.float32)               # 48000 -> 22050
        an = GF.analyse(y22, GF.SR)
        scale = (GF.SR / float(self.sr)) / GF.HOP
        beat_frames = [(bf - snap_start - a) * scale for bf in beats_abs]
        res = GF.window_features(an, GF.cells_from_beats(beat_frames))
        if res is None:
            return None
        X, on, mt = res
        Xn, onn, ref = GF.normalise(X[None], on[None], None)
        if self.ref is None:
            self.ref = ref
        else:
            self.ref = tuple(0.75 * float(o) + 0.25 * float(n) for o, n in zip(self.ref, ref))
        Xn, onn, _ = GF.normalise(X[None], on[None], self.ref)
        onp, mtp, sound = self.net.forward(Xn[0], float(tempo))
        gain_ref = float(self.net.w.get("gain_mean", 0.0)); tilt_ref = np.asarray(self.net.w.get("tilt_mean", np.zeros(3)), dtype=np.float32)
        out = {"on": onp, "mt": mtp, "sound": sound, "swing": GrooveNet.swing(mtp, onp),
               "level_mult": float(np.clip(10.0 ** ((float(sound[0]) - gain_ref) / 20.0), 0.6, 1.5)),
               "tilt_db": np.clip(sound[1:] - tilt_ref, -6.0, 6.0).astype(np.float32),
               "src_kick": Xn[0][:, 0], "src_snare": Xn[0][:, 1], "src_hat": Xn[0][:, 2], "src_on": onn[0], "tempo": float(tempo)}
        self.last = {"swing": round(out["swing"], 3), "level_mult": round(out["level_mult"], 2), "tilt_db": [round(float(v), 1) for v in out["tilt_db"]],
                     "gain_db": round(float(sound[0]), 1), "on_max": round(float(onp.max()), 2), "steps": GrooveNet.pattern(onp, 0.2)}
        self.count += 1; self.last_ms = int((time.time() - t0) * 1000)
        return out
