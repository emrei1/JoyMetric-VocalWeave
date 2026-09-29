"""JoyMetric v31.24 GrooveNet features - shared by TRAINING (offline corpus) and LIVE (the lookahead).

A 2-bar window is 32 sixteenth cells.  For every cell: the strongest drum onset per band (kick
30-150 Hz, snare 150-2500 Hz, hat 5-11 kHz of the percussive component) with its micro-timing
(offset of that onset from the cell's grid line, in cells), the strongest HARMONIC onset (chroma flux
of the harmonic component, weighted by tonal energy) with its micro-timing, and the cell position in
the bar.  Per window: the harmonic layer's level relative to the drums (dB) and its 3-band tilt (dB).

Everything is computed at 22.05 kHz with the same STFT (1024 / 256) on both sides, so the model sees
the same numbers live as in training.
"""
from __future__ import annotations

import math

import numpy as np

SR = 22050
HOP = 256
NFFT = 1024
N_FEAT = 7           # kick, snare, hat strength; kick, snare, hat micro-timing; position in bar


def _stft_power(y, sr):
    import librosa
    S = np.abs(librosa.stft(y, n_fft=NFFT, hop_length=HOP)) ** 2
    f = librosa.fft_frequencies(sr=sr, n_fft=NFFT)
    return S, f


def _band(S, f, lo, hi):
    m = (f >= lo) & (f < hi)
    e = S[m].sum(axis=0)
    flux = np.maximum(0.0, np.diff(np.log1p(e * 1e3), prepend=0.0))
    return e, flux


def analyse(y, sr=SR):
    """band envelopes / fluxes of a mono clip (any length >= 1 s).  Returns a dict of frame series."""
    import librosa
    y = np.asarray(y, dtype=np.float32)
    y = y / (float(np.abs(y).max()) + 1e-6)
    H, P = librosa.effects.hpss(y, margin=2.0)
    Sp, f = _stft_power(P, sr); Sh, _ = _stft_power(H, sr)
    e_k, f_k = _band(Sp, f, 30, 150); e_s, f_s = _band(Sp, f, 150, 2500); e_h, f_h = _band(Sp, f, 5000, 11000)
    e_t, _ = _band(Sh, f, 80, 5000)
    chroma = librosa.feature.chroma_stft(S=Sh, sr=sr)
    f_t = np.maximum(0.0, np.diff(chroma, axis=1, prepend=0.0)).sum(axis=0) * np.sqrt(e_t / (float(e_t.max()) + 1e-9))
    e_tl, _ = _band(Sh, f, 80, 300); e_tm, _ = _band(Sh, f, 300, 2000); e_th, _ = _band(Sh, f, 2000, 8000)
    return {"f_k": f_k, "f_s": f_s, "f_h": f_h, "f_t": f_t, "e_k": e_k, "e_s": e_s, "e_h": e_h, "e_t": e_t, "e_tl": e_tl, "e_tm": e_tm, "e_th": e_th,
            "n_frames": int(f_k.shape[0]), "frames_per_s": sr / HOP}


def cells_from_beats(beat_frames):
    """32 sixteenth cells (start, end in STFT frames) from 9 consecutive beat frame positions."""
    cells = []
    for b in range(8):
        fa, fb = float(beat_frames[b]), float(beat_frames[b + 1])
        for q in range(4):
            cells.append((fa + (fb - fa) * q / 4.0, fa + (fb - fa) * (q + 1) / 4.0))
    return cells


def window_features(an, cells):
    """(X (32, 7), on (32,), mt (32,)) for one window, or None if a cell is too short.
    Strengths are RAW here (normalise per clip / per session with `normalise`)."""
    n_frames = an["n_frames"]
    feat = np.zeros((32, N_FEAT), np.float32); on = np.zeros(32, np.float32); mt = np.zeros(32, np.float32)
    for ci, (ca, cb) in enumerate(cells):
        w = cb - ca
        a = int(max(0, math.floor(ca - w * 0.5))); b = int(min(n_frames, math.ceil(cb - w * 0.5)))   # centred on the grid line
        if b - a < 2:
            return None
        for j, fl in enumerate((an["f_k"], an["f_s"], an["f_h"], an["f_t"])):
            seg = fl[a:b]; k = int(np.argmax(seg)); off = float((a + k - ca) / max(1.0, w))
            if j < 3:
                feat[ci, j] = float(seg[k]); feat[ci, 3 + j] = off
            else:
                on[ci] = float(seg[k]); mt[ci] = off
        feat[ci, 6] = float(ci % 16) / 16.0
    return feat, on, mt


def window_sound(an, fa, fb):
    """(gain dB of the harmonic layer vs the drums, 3-band tilt dB) over frames [fa, fb)."""
    fa = int(max(0, fa)); fb = int(min(an["n_frames"], fb))
    drums = float(np.mean(an["e_k"][fa:fb] + an["e_s"][fa:fb] + an["e_h"][fa:fb]) + 1e-9); ton = float(np.mean(an["e_t"][fa:fb]) + 1e-9)
    gain = 10.0 * math.log10(ton / drums)
    tl, tm, th = float(np.mean(an["e_tl"][fa:fb]) + 1e-9), float(np.mean(an["e_tm"][fa:fb]) + 1e-9), float(np.mean(an["e_th"][fa:fb]) + 1e-9)
    tot = tl + tm + th
    return float(gain), np.array([10 * math.log10(tl / tot), 10 * math.log10(tm / tot), 10 * math.log10(th / tot)], np.float32)


def normalise(X, on, ref=None):
    """strengths to 0..1 by the 98th percentile per channel (per clip in training, per session live).
    `ref` = (p_k, p_s, p_h, p_t) to reuse a reference scale; returns (X, on, ref)."""
    X = np.array(X, dtype=np.float32, copy=True); on = np.array(on, dtype=np.float32, copy=True)
    if ref is None:
        ref = tuple(float(np.percentile(X[..., j], 98)) + 1e-6 for j in range(3)) + (float(np.percentile(on, 98)) + 1e-6,)
    for j in range(3):
        X[..., j] = np.minimum(1.0, X[..., j] / ref[j])
    on = np.minimum(1.0, on / ref[3])
    return X, on, ref
