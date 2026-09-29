"""JoyMetric v31.24 GrooveNet runtime (numpy) - where, when and how the rack's stabs and the drummer's
hats sit inside THIS record's groove.

Trained self-supervised on real produced music (see scratchpad groove_extract.py / groove_train.py):
given the record's kick / snare / hat grid with micro-timing over 2 bars, the net predicts
  * per sixteenth: the probability that the record's own HARMONIC layer hits there (where keys /
    stabs live in music that grooves like this) and its micro-timing (swing / push / drag),
  * per window: the harmonic layer's level relative to the drums (dB) and its 3-band tilt (dB).
The rack turns the probabilities into a stab pattern, plays it with the predicted micro-timing,
at the predicted level, through the predicted tilt; the drummer's hats take the same micro-timing.
"""
from __future__ import annotations

import os

import numpy as np

from groove_features import N_FEAT


def _sig(x):
    return 1.0 / (1.0 + np.exp(-x))


class GrooveNet:
    def __init__(self, path: str | None = None):
        path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "groovenet.npz")
        z = np.load(path)
        self.w = {k: np.asarray(z[k], dtype=np.float32) for k in z.files}
        self.on_thr = float(self.w.get("on_thr", 0.35)); self.gain_scale = float(self.w.get("gain_scale", 10.0)); self.tilt_scale = float(self.w.get("tilt_scale", 10.0))
        self.ready = True

    # ------------------------------------------------------------------ GRU pieces (PyTorch gate order r, z, n)
    def _gru_dir(self, x, pre, reverse=False):
        Wih = self.w[pre + "weight_ih_l0" + ("_reverse" if reverse else "")]; Whh = self.w[pre + "weight_hh_l0" + ("_reverse" if reverse else "")]
        bih = self.w[pre + "bias_ih_l0" + ("_reverse" if reverse else "")]; bhh = self.w[pre + "bias_hh_l0" + ("_reverse" if reverse else "")]
        H = Whh.shape[1]; T = x.shape[0]
        h = np.zeros(H, np.float32); out = np.zeros((T, H), np.float32)
        gi_all = x @ Wih.T + bih
        order = range(T - 1, -1, -1) if reverse else range(T)
        for t in order:
            gi = gi_all[t]; gh = h @ Whh.T + bhh
            r = _sig(gi[:H] + gh[:H]); zg = _sig(gi[H:2 * H] + gh[H:2 * H]); n = np.tanh(gi[2 * H:] + r * gh[2 * H:])
            h = (1.0 - zg) * n + zg * h
            out[t] = h
        return out

    def _bigru(self, x, pre):
        return np.concatenate([self._gru_dir(x, pre, False), self._gru_dir(x, pre, True)], axis=1)

    def forward(self, X, tempo):
        """X (32, 7) normalised features, tempo BPM -> (on_prob (32,), micro-timing (32,) in cells, sound (4,) = gain dB, tilt dB x3)."""
        X = np.asarray(X, dtype=np.float32)
        t = np.full((X.shape[0], 1), (float(tempo) - 120.0) / 60.0, np.float32)
        x = np.concatenate([X, t], axis=1)
        z = np.tanh(x @ self.w["inp.weight"].T + self.w["inp.bias"])
        z = self._bigru(z, "gru1."); z = self._bigru(z, "gru2.")
        on = _sig(z @ self.w["on.weight"].T + self.w["on.bias"]).reshape(-1)
        mt = 0.5 * np.tanh(z @ self.w["mt.weight"].T + self.w["mt.bias"]).reshape(-1)
        pooled = z.mean(axis=0)
        s = np.tanh(pooled @ self.w["s1.weight"].T + self.w["s1.bias"]) @ self.w["s2.weight"].T + self.w["s2.bias"]
        sound = np.array([s[0] * self.gain_scale, s[1] * self.tilt_scale, s[2] * self.tilt_scale, s[3] * self.tilt_scale], np.float32)
        return on, mt, sound

    # ------------------------------------------------------------------ musical use
    @staticmethod
    def pattern(on_prob, density: float, min_gap: int = 1):
        """stab steps from probabilities: the `density` fraction of the 32 cells with the highest
        probability, never two neighbours (min_gap), always at least the top cell."""
        p = np.asarray(on_prob, dtype=np.float64)
        k = max(1, int(round(float(density) * 32)))
        order = np.argsort(-p)
        chosen = []
        for i in order:
            if len(chosen) >= k:
                break
            if all(abs(int(i) - c) > min_gap for c in chosen):
                chosen.append(int(i))
        return sorted(chosen)

    @staticmethod
    def swing(mt, on_prob):
        """the record's swing: mean micro-timing of the off-beat sixteenths (cells 2, 6, ...) weighted by onset probability, in cells."""
        mt = np.asarray(mt, dtype=np.float64); p = np.asarray(on_prob, dtype=np.float64)
        idx = np.arange(32); off = (idx % 4) == 2
        w = p[off] + 1e-6
        return float((mt[off] * w).sum() / w.sum())
