"""JoyMetric v31.23 RackMind - SynthRack.

A real subtractive synthesizer and a sampler, in numpy DSP, that plays the record's UPCOMING
chords (no diffusion model - the Stable Audio composer of v31.21/22 is retired by default).

per block (2 s of the 16 s lookahead, beat-aligned through the frame anchor):
  chords   tonal chroma per beat -> triad templates + bass-note bonus + key prior -> Viterbi (bar-line aware)
  voicing  chord tones around a register with voice leading across blocks
  notes    kind pattern on the ABSOLUTE beat grid (block boundaries are transparent):
             pad    sustained voicing (legato continuation across blocks, no re-attack)
             arp    16ths (8ths above 140 BPM) up / up-down / random walk over two octaves
             pluck  chord stabs on a genre pattern (offbeat / syncopated / trap / four / dnb)
             lead   monophonic motif on chord tones with scale passing notes, portamento, vibrato
  voices   PolyBLEP saw / pulse, unison detune spread, sub oscillator, ADSR, per-voice pan
  filter   paraphonic resonant low-pass, ADSR + LFO + "build" (opening across the block) modulated
  fx       chorus, tempo-synced ping-pong delay, plate-style reverb, beat-locked sidechain pump
  level    RMS to the target the planner asked for (relative to the record), soft ceiling
sampler:
  freeze   the drop's first hit rolled with a tightening pattern (1/2 1/2 | 1/4 x4 | 1/8 x8 | 1/12 x9 + gap),
           rising high-pass, 3 ms grain fades, a silent gap before the release
"""
from __future__ import annotations

import math
import threading
import time

import numpy as np
from scipy.signal import butter, fftconvolve, lfilter, sosfilt

from synth_weave import allowed_pitch_classes, chroma_agreement, style_words, tonal_chroma

NAMES = "C C# D D# E F F# G G# A A# B".split()
MAJOR_SCALE = (0, 2, 4, 5, 7, 9, 11)
MINOR_SCALE = (0, 2, 3, 5, 7, 8, 10)
FLATS = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#", "Cb": "B", "Fb": "E"}


# ---------------------------------------------------------------------------
# harmony
# ---------------------------------------------------------------------------
def parse_key(key):
    k = (key or "").strip().replace("♯", "#").replace("♭", "b")
    if not k:
        return None
    minor = k.endswith("m") and not k.endswith("dim")
    root = k[:-1] if minor else k
    root = FLATS.get(root, root)
    if root not in NAMES:
        return None
    return NAMES.index(root), minor


def key_pcs(key):
    p = parse_key(key)
    if p is None:
        return None
    r, minor = p
    return set((r + s) % 12 for s in (MINOR_SCALE if minor else MAJOR_SCALE))


def diatonic_triads(key):
    p = parse_key(key)
    if p is None:
        return []
    r, minor = p
    scale = MINOR_SCALE if minor else MAJOR_SCALE
    out = []
    for i in range(7):
        root = (r + scale[i]) % 12
        third = (r + scale[(i + 2) % 7]) % 12
        fifth = (r + scale[(i + 4) % 7]) % 12
        if (fifth - root) % 12 != 7:
            continue                                        # diminished: no triad template
        out.append((root, "maj" if (third - root) % 12 == 4 else "min"))
    return out


def chord_pcs(root, quality):
    return [root % 12, (root + (4 if quality == "maj" else 3)) % 12, (root + 7) % 12]


def chord_label(root, quality):
    return NAMES[root % 12] + ("m" if quality == "min" else "")


TEMPLATES = []
for _r in range(12):
    for _q in ("maj", "min"):
        _w = np.zeros(12)
        _p = chord_pcs(_r, _q)
        _w[_p[0]] = 1.0; _w[_p[1]] = 0.8; _w[_p[2]] = 0.7
        TEMPLATES.append((_r, _q, _w))


def _bass_pc(seg, sr):
    """pitch class of the strongest spectral peak between 45 and 260 Hz (None if there is no clear one)."""
    n = int(len(seg))
    if n < 2048:
        return None
    win = np.hanning(n).astype(np.float32)
    spec = np.abs(np.fft.rfft(seg * win))
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    m = (freqs >= 45.0) & (freqs <= 260.0)
    if not m.any():
        return None
    band = spec[m]
    i = int(np.argmax(band))
    if band[i] < 4.0 * (np.median(band) + 1e-9):
        return None
    f = float(freqs[m][i])
    return int(round(69 + 12 * math.log2(f / 440.0))) % 12


def estimate_chords(mono, sr, bounds, key="", bar_starts=None):
    """One chord per beat window.  `bounds` = [(a, b), ...] sample ranges of consecutive beats in
    `mono`; `bar_starts` = indices of windows that begin a bar (chord changes are cheaper there).
    Returns [{"root","quality","pcs","label","conf","weak"}] aligned with `bounds`."""
    mono = np.asarray(mono, dtype=np.float32)
    if mono.ndim == 2:
        mono = mono.mean(axis=1)
    bar_starts = set(bar_starts or ())
    prior = set(diatonic_triads(key))
    obs, weak = [], []
    for (a, b) in bounds:
        a = max(0, int(a)); b = min(len(mono), int(b))
        seg = mono[a:b]
        if len(seg) < 2048:
            seg = np.pad(seg, (0, 2048 - len(seg)))
        c, ton = tonal_chroma(seg, sr)
        c = np.asarray(c, dtype=np.float64)
        s = float(c.sum())
        c = c / s if s > 1e-9 else np.full(12, 1.0 / 12.0)
        bass = _bass_pc(seg, sr)
        sc = np.zeros(len(TEMPLATES))
        for t, (r, q, w) in enumerate(TEMPLATES):
            inside = float((c * w).sum())
            outside = float(c.sum() - c[chord_pcs(r, q)].sum())
            sc[t] = inside - 0.35 * outside + (0.12 if bass == r else 0.0) + (0.06 if (r, q) in prior else 0.0)
        obs.append(sc); weak.append(bool(ton < 0.05))
    if not obs:
        return []
    obs = np.asarray(obs)
    n, T = obs.shape
    cost = np.zeros((n, T)); back = np.zeros((n, T), dtype=np.int64)
    cost[0] = obs[0]
    for i in range(1, n):
        pen = 0.02 if i in bar_starts else 0.07
        prev = cost[i - 1]
        bp = int(np.argmax(prev)); bv = float(prev[bp])
        move = bv - pen
        use_move = move > prev
        cost[i] = np.where(use_move, move, prev) + obs[i]
        back[i] = np.where(use_move, bp, np.arange(T))
    path = [int(np.argmax(cost[-1]))]
    for i in range(n - 1, 0, -1):
        path.append(int(back[i, path[-1]]))
    path = path[::-1]
    tonic = None
    p = parse_key(key)
    if p is not None:
        tonic = (p[0], "min" if p[1] else "maj")
    out = []
    for i, t in enumerate(path):
        r, q, _ = TEMPLATES[t]
        if weak[i]:
            if out:
                r, q = out[-1]["root"], out[-1]["quality"]
            elif tonic is not None:
                r, q = tonic
        srt = np.sort(obs[i])
        conf = float(srt[-1] - srt[-2]) if T > 1 else 1.0
        out.append({"root": int(r), "quality": q, "pcs": chord_pcs(r, q), "label": chord_label(r, q),
                    "conf": round(conf, 3), "weak": bool(weak[i])})
    return out


def voice_chord(root, quality, center, prev=None, n_voices=3, seventh=False):
    """chord tones around `center` (MIDI); each voice moves as little as possible from `prev`."""
    pcs = chord_pcs(root, quality)
    if seventh:
        pcs = pcs + [(root + (11 if quality == "maj" else 10)) % 12]
    pcs = pcs[:max(3, int(n_voices))]
    notes = []
    for pc in pcs:
        cands = [m for m in range(center - 14, center + 15) if m % 12 == pc]
        if prev:
            best = min(cands, key=lambda m: min(abs(m - q) for q in prev) + 0.15 * abs(m - center))
        else:
            best = min(cands, key=lambda m: abs(m - center))
        notes.append(int(best))
    return sorted(set(notes))


# ---------------------------------------------------------------------------
# oscillators / envelopes / filter / fx
# ---------------------------------------------------------------------------
def _polyblep(t, dt):
    out = np.zeros_like(t)
    m = t < dt
    if m.any():
        tt = t[m] / dt[m]
        out[m] = tt + tt - tt * tt - 1.0
    m2 = t > 1.0 - dt
    if m2.any():
        tt = (t[m2] - 1.0) / dt[m2]
        out[m2] = tt * tt + tt + tt + 1.0
    return out


def osc_saw(phase, dt):
    t = phase - np.floor(phase)
    return (2.0 * t - 1.0) - _polyblep(t, dt)


def osc_pulse(phase, dt, width=0.5):
    t = phase - np.floor(phase)
    a = np.where(t < width, 1.0, -1.0) + _polyblep(t, dt)
    t2 = (t + 1.0 - width) % 1.0
    return a - _polyblep(t2, dt)


def osc_sine(phase, dt):
    return np.sin(2.0 * np.pi * phase)


def adsr_env(n, sr, a, d, s, r, gate_n):
    n = int(n); gate_n = int(max(1, min(n, gate_n)))
    a_n = max(1, int(a * sr)); d_n = max(1, int(d * sr)); r_n = max(1, int(r * sr))
    t = np.arange(n, dtype=np.float64)
    env = np.minimum(1.0, t / a_n)
    dec = s + (1.0 - s) * np.exp(-np.maximum(0.0, t - a_n) / (d_n / 4.0))
    env = np.where(t > a_n, dec, env)
    if gate_n < n:
        g_val = float(env[gate_n - 1])
        rel = g_val * np.exp(-(t[gate_n:] - gate_n) / (r_n / 4.0))
        env = np.concatenate([env[:gate_n], rel])
    return env.astype(np.float32)


def rbj_lowpass_sos(fc, sr, q):
    fc = float(min(max(fc, 30.0), 0.45 * sr))
    w0 = 2.0 * math.pi * fc / sr
    cw = math.cos(w0); sw = math.sin(w0); alpha = sw / (2.0 * max(0.2, q))
    b0 = (1.0 - cw) / 2.0; b1 = 1.0 - cw; b2 = b0
    a0 = 1.0 + alpha; a1 = -2.0 * cw; a2 = 1.0 - alpha
    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])


def modulated_lowpass(y, sr, cutoff, q, chunk=128):
    """paraphonic resonant low-pass with a per-sample cutoff trajectory (coefficients per chunk)."""
    y = np.asarray(y, dtype=np.float64)
    out = np.empty_like(y)
    zi = np.zeros((1, 2, y.shape[1]))
    n = y.shape[0]
    for a in range(0, n, chunk):
        b = min(n, a + chunk)
        sos = rbj_lowpass_sos(float(cutoff[(a + b) // 2]), sr, q)
        out[a:b], zi = sosfilt(sos, y[a:b], axis=0, zi=zi)
    return out


def chorus(y, sr, rate=0.45, depth_ms=4.0, base_ms=11.0, wet=0.5):
    if wet <= 1e-3:
        return y
    n = y.shape[0]; t = np.arange(n, dtype=np.float64)
    out = np.empty_like(y)
    for ch in range(y.shape[1]):
        ph = 2.0 * np.pi * rate * t / sr + (0.0 if ch == 0 else np.pi / 2.0)
        d = (base_ms + depth_ms * np.sin(ph)) * sr / 1000.0
        out[:, ch] = np.interp(t - d, t, y[:, ch], left=0.0, right=0.0)
    return y * (1.0 - wet) + out * wet


def pingpong_delay(y, sr, bpm, division=0.75, feedback=0.38, wet=0.3, damp_hz=4200.0):
    if wet <= 1e-3:
        return y
    d = int(round(division * 60.0 / max(40.0, bpm) * sr))
    n = y.shape[0]
    if d < 32 or d >= n:
        return y
    out = np.zeros_like(y)
    sig = y.mean(axis=1)
    b, a = butter(1, min(0.45 * sr, damp_hz) / (sr / 2.0))
    g = 1.0
    for k in range(1, 14):
        sig = lfilter(b, a, sig)
        if k > 1:
            g *= feedback
        start = k * d
        if start >= n or g < 0.02:
            break
        chan = 0 if k % 2 == 1 else 1
        out[start:, chan] += g * sig[:n - start] * 0.9
        out[start:, 1 - chan] += g * sig[:n - start] * 0.28
    return y + wet * out


def plate_reverb(y, sr, rt60=1.5, wet=0.3, damp_hz=5200.0, seed=0):
    if wet <= 1e-3:
        return y
    rng = np.random.default_rng(seed)
    n_ir = int(rt60 * sr)
    t = np.arange(n_ir) / sr
    ir = rng.standard_normal((n_ir, 2)) * np.exp(-6.91 * t / rt60)[:, None]
    pre = int(0.012 * sr)
    ir[:pre] *= np.linspace(0.0, 1.0, pre)[:, None]
    b, a = butter(1, min(0.45 * sr, damp_hz) / (sr / 2.0))
    ir = lfilter(b, a, ir, axis=0)
    ir /= (np.sqrt((ir ** 2).sum(axis=0)).max() + 1e-9)
    n = y.shape[0]
    wetsig = np.stack([fftconvolve(y[:, 0], ir[:, 0])[:n], fftconvolve(y[:, 1], ir[:, 1])[:n]], axis=1)
    return y + wet * 0.5 * wetsig


def sidechain_pump(y, sr, bpm, depth, b0):
    """beat-locked duck: `b0` = absolute beat position of sample 0 (so blocks join without a phase jump)."""
    if depth <= 1e-3:
        return y
    n = y.shape[0]
    beat_n = 60.0 / max(40.0, bpm) * sr
    t = b0 + np.arange(n) / beat_n
    frac = t - np.floor(t)
    shape = np.minimum(frac / 0.02, 1.0) * np.exp(-frac * 6.0)
    return y * (1.0 - depth * shape)[:, None]


def tilt_eq(y, sr, tilt_db):
    """3-band tilt (low < 300 Hz, mid 300-2000 Hz, high > 2000 Hz) in dB, GrooveNet's sound head."""
    y = np.asarray(y, dtype=np.float64)
    g = [10.0 ** (float(min(6.0, max(-6.0, d))) / 20.0) for d in list(tilt_db)[:3]]
    lo = sosfilt(butter(2, 300.0 / (sr / 2.0), btype="low", output="sos"), y, axis=0)
    hi = sosfilt(butter(2, 2000.0 / (sr / 2.0), btype="high", output="sos"), y, axis=0)
    mid = y - lo - hi
    return lo * g[0] + mid * g[1] + hi * g[2]


def rms_hp(y, sr, hz=150.0):
    sos = butter(2, hz / (sr / 2.0), btype="high", output="sos")
    z = sosfilt(sos, np.asarray(y, dtype=np.float64), axis=0)
    return float(np.sqrt(np.mean(z * z) + 1e-12))


def level_to(y, sr, target_rms, ceiling=0.92):
    r = rms_hp(y, sr)
    g = min(60.0, float(target_rms) / max(r, 1e-6))
    y = y * g
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    if peak > ceiling:
        y = np.tanh(y / ceiling) * ceiling                  # soft ceiling, never a hard clip
    return y, g


# ---------------------------------------------------------------------------
# presets
# ---------------------------------------------------------------------------
PRESETS = {
    "pad":   dict(osc="saw", unison=5, detune=0.22, spread=0.85, sub=0.30, a=0.8, d=0.9, s=0.85, r=1.2,
                  fc=600.0, fenv=1.0, fq=0.8, lfo=0.09, lfo_amt=0.35, chorus=0.55, delay=0.0, reverb=0.4,
                  pump=0.22, register=60, voices=3, seventh=False),
    "arp":   dict(osc="saw", unison=2, detune=0.12, spread=0.55, sub=0.0, a=0.004, d=0.16, s=0.0, r=0.10,
                  fc=650.0, fenv=2.4, fq=2.6, lfo=0.0, lfo_amt=0.0, chorus=0.2, delay=0.32, reverb=0.15,
                  pump=0.32, register=67, voices=3, seventh=False),
    "pluck": dict(osc="pulse", unison=3, detune=0.15, spread=0.7, sub=0.22, a=0.003, d=0.25, s=0.45, r=0.25,
                  fc=600.0, fenv=2.8, fq=1.7, lfo=0.0, lfo_amt=0.0, chorus=0.3, delay=0.18, reverb=0.2,
                  pump=0.28, register=62, voices=3, seventh=False),
    "lead":  dict(osc="saw", unison=2, detune=0.10, spread=0.3, sub=0.0, a=0.01, d=0.25, s=0.6, r=0.2,
                  fc=1500.0, fenv=1.4, fq=1.2, lfo=5.5, lfo_amt=0.05, chorus=0.15, delay=0.35, reverb=0.25,
                  pump=0.12, register=72, voices=1, seventh=False, glide=0.035),
}
PLUCK_PATTERNS = {"offbeat": (2, 6, 10, 14), "syncopated": (0, 3, 6, 10, 12, 14), "trap": (0, 5, 8, 11),
                  "four": (0, 4, 8, 12), "dnb": (0, 2, 6, 10, 11)}



# ---------------------------------------------------------------------------
# v31.23.2: the DJ prompt shapes the sound (deterministic, instant) ...
# ---------------------------------------------------------------------------
PROFILE_WORDS = (
    (("dark", "deep", "underground", "moody", "shadow", "night", "warm", "analog", "vintage", "tape", "lofi", "lo-fi", "dusty", "karanlik", "karanlık", "koyu"), "brightness", -1.0),
    (("bright", "shiny", "crisp", "euphoric", "uplifting", "sparkle", "airy", "glass", "parlak", "digital", "futuristic"), "brightness", 1.0),
    (("dreamy", "floating", "space", "spacey", "wide", "width", "hypnotic", "ambient", "reverb", "ethereal", "lush", "cinematic", "genis", "geniş", "ruya", "rüya"), "space", 1.0),
    (("dry", "tight", "punchy", "minimal", "raw", "direct", "kuru", "siki", "sıkı"), "space", -1.0),
    (("aggressive", "hard", "intense", "driving", "raise energy", "banger", "energetic", "heavy", "sert", "agresif", "peak time", "rave", "massive"), "energy", 1.0),
    (("restrained", "subtle", "gentle", "soft", "smooth", "lowkey", "low-key", "laid back", "laid-back", "chill", "calm", "yumusak", "yumuşak", "sakin", "hafif"), "energy", -1.0),
    (("808", "sub", "bass clean", "keep the bass", "low end", "low-end", "clean bass"), "bass_clean", 1.0),
    (("vocal", "vocals", "voice", "vokal"), "vocal_care", 1.0),
)


def prompt_profile(text: str) -> dict:
    """axes in [-1, 1] from the DJ prompt (and the policy style) -> concrete rack modifiers."""
    t = " " + (text or "").lower().replace("_", " ") + " "
    axes = {"brightness": 0.0, "space": 0.0, "energy": 0.0, "bass_clean": 0.0, "vocal_care": 0.0}
    for words, axis, sign in PROFILE_WORDS:
        hits = sum(1 for w in words if w in t)
        if hits:
            axes[axis] += sign * min(1.0, 0.6 * hits)
    for k in ("brightness", "space", "energy"):
        axes[k] = float(max(-1.0, min(1.0, axes[k])))
    b, s, e = axes["brightness"], axes["space"], axes["energy"]
    bass_clean = axes["bass_clean"] > 0
    return {"axes": axes, "fc_mult": float(2.0 ** (0.75 * b)), "chorus_add": 0.15 * s, "reverb_add": 0.15 * s, "spread": float(min(1.0, max(0.5, 0.85 + 0.15 * s))),
            "pump_mult": float(1.0 + 0.4 * e), "level_scale": float(min(1.2, max(0.7, 1.0 + 0.2 * e))), "a_mult": float(1.0 - 0.3 * e), "r_mult": float(1.0 - 0.25 * e),
            "drive": float(max(0.0, 0.35 * e)), "fq_add": float(0.4 * e), "bass_clean": bool(bass_clean), "hp": 180.0 if bass_clean else 0.0,
            "vocal_care": bool(axes["vocal_care"] > 0), "text": (text or "")[:80]}


# ... and the CLAP-selected variant gives it its character (see timbre_select.py)
PAD_VARIANTS = {
    # analog synth pads
    "warm":    dict(osc="saw", unison=4, detune=0.15, fc=550.0, chorus=0.4, reverb=0.35, spread=0.8),
    "wide":    dict(osc="saw", unison=7, detune=0.30, fc=700.0, chorus=0.7, reverb=0.5, spread=1.0),
    "bright":  dict(osc="saw", unison=5, detune=0.2, fc=1400.0, chorus=0.4, reverb=0.3, fenv=1.4),
    "hollow":  dict(osc="pulse", unison=3, detune=0.12, fc=800.0, chorus=0.5, reverb=0.4),
    "glass":   dict(osc="saw", unison=3, detune=0.08, fc=2400.0, chorus=0.3, reverb=0.55, a=1.2, register=72),
    # other instruments in the pad role
    "strings": dict(engine="va", osc="saw", unison=6, detune=0.18, spread=1.0, sub=0.0, a=0.9, d=1.0, s=0.9, r=1.5, fc=2600.0, fenv=0.3, fq=0.6,
                    lfo=0.0, lfo_amt=0.0, chorus=0.35, reverb=0.5, vib_hz=5.0, vib_oct=0.008, register=64),
    "choir":   dict(engine="va", osc="saw", unison=4, detune=0.14, spread=0.9, sub=0.0, a=0.7, d=1.0, s=0.9, r=1.2, fc=3500.0, fenv=0.2, fq=0.6,
                    lfo=0.0, lfo_amt=0.0, chorus=0.3, reverb=0.5, formant=True, register=60),
    "organ":   dict(engine="additive", harmonics=((1, 1.0), (2, 0.8), (3, 0.5), (4, 0.4), (6, 0.25), (8, 0.2)), a=0.01, d=0.2, s=1.0, r=0.12,
                    filter=False, chorus=0.25, reverb=0.3, trem_hz=6.0, trem_depth=0.2, sub=0.0, register=60, pump=0.15, unison=1, spread=0.5),
    "epiano":  dict(engine="fm", ratio=1.0, index=1.8, fm_decay=0.9, a=0.005, d=1.6, s=0.5, r=0.9, filter=False, chorus=0.35, reverb=0.35,
                    trem_hz=4.5, trem_depth=0.25, sub=0.0, register=60, pump=0.15, unison=1, spread=0.6),
}
PLUCK_VARIANTS = {
    # analog synth plucks
    "soft":    dict(osc="saw", fc=500.0, fenv=2.0, d=0.3, s=0.5, r=0.3),
    "tight":   dict(osc="pulse", fc=700.0, fenv=3.0, d=0.2, s=0.35, r=0.2),
    "bright":  dict(osc="saw", fc=1200.0, fenv=3.2, d=0.25, s=0.45, r=0.25),
    # other instruments in the transient role (v31.27: they SUSTAIN while a note is held - the part's note lengths are audible)
    "epiano":  dict(engine="fm", ratio=1.0, index=2.6, fm_decay=0.6, a=0.003, d=0.9, s=0.45, r=0.4, filter=False, register=62, sub=0.0),
    "bells":   dict(engine="fm", ratio=3.5, index=3.0, fm_decay=0.5, a=0.002, d=0.6, s=0.0, r=0.5, filter=False, register=79, sub=0.0, reverb=0.4),
    "marimba": dict(engine="fm", ratio=4.0, index=1.2, fm_decay=0.12, a=0.002, d=0.25, s=0.0, r=0.15, filter=False, register=67, sub=0.0),
    "guitar":  dict(engine="ks", ks_decay=0.995, ks_bright=0.45, a=0.001, d=0.5, s=0.0, r=0.4, filter=False, register=55, sub=0.0, chorus=0.15, delay=0.15),
    "harp":    dict(engine="ks", ks_decay=0.998, ks_bright=0.7, a=0.001, d=0.9, s=0.0, r=0.8, filter=False, register=67, sub=0.0, reverb=0.45, delay=0.0),
    "brass":   dict(engine="va", osc="saw", unison=2, detune=0.1, fc=900.0, fenv=2.5, fq=1.0, a=0.02, d=0.25, s=0.4, r=0.2, drive=0.3, register=57, sub=0.0, chorus=0.1),
    "organ":   dict(engine="additive", harmonics=((1, 1.0), (2, 0.7), (3, 0.4), (4, 0.3)), a=0.005, d=0.15, s=0.8, r=0.08, filter=False, register=60, sub=0.0, chorus=0.2),
}
INSTRUMENT_LABEL = {"warm": "analog pad (warm)", "wide": "supersaw pad (wide)", "bright": "analog pad (bright)", "hollow": "pulse pad", "glass": "glass pad",
                    "strings": "string ensemble", "choir": "choir", "organ": "organ", "epiano": "electric piano", "soft": "synth pluck (soft)",
                    "tight": "synth pluck (tight)", "bells": "bells", "marimba": "marimba", "guitar": "guitar (plucked string)", "harp": "harp", "brass": "brass stabs"}


def make_preset(kind, style="", song=None, profile=None, variant=None):
    p = dict(PRESETS.get(kind, PRESETS["pad"]))
    table = PAD_VARIANTS if kind == "pad" else (PLUCK_VARIANTS if kind == "pluck" else {})
    if variant and variant in table:
        p.update(table[variant]); p["variant"] = variant
    genre, moods = style_words(style or "")
    p["genre"] = genre; p["moods"] = moods
    if genre in ("house", "techno", "trance", "disco"):
        p["pump"] += 0.15; p["fc"] *= 1.3; p["pattern"] = "offbeat"
    elif genre in ("drum and bass", "dubstep"):
        p["pump"] += 0.05; p["fc"] *= 1.2; p["pattern"] = "dnb"
    elif genre in ("hip-hop", "r&b"):
        p["pump"] = max(0.0, p["pump"] - 0.15); p["fc"] *= 0.8; p["reverb"] += 0.05; p["pattern"] = "trap"
    elif genre == "ambient":
        p["a"] *= 1.8; p["r"] *= 1.6; p["reverb"] = 0.5; p["pump"] = 0.0; p["pattern"] = "syncopated"
    else:
        p["pattern"] = "syncopated"
    if "dark" in moods or "deep" in moods:
        p["fc"] *= 0.7
    if "bright" in moods or "euphoric" in moods or "uplifting" in moods:
        p["fc"] *= 1.4
    if any(m in moods for m in ("dreamy", "floating", "spacey", "hypnotic")):
        p["chorus"] = min(0.8, p["chorus"] + 0.15); p["reverb"] = min(0.6, p["reverb"] + 0.1)
    song = dict(song or {})
    if float(song.get("drums") or 0.0) < 0.35:
        p["pump"] = 0.0                                      # nothing to pump against
    prof = dict(profile or {})
    if prof:
        p["fc"] *= float(prof.get("fc_mult", 1.0)); p["chorus"] = min(0.85, max(0.0, p["chorus"] + float(prof.get("chorus_add", 0.0))))
        p["reverb"] = min(0.65, max(0.0, p["reverb"] + float(prof.get("reverb_add", 0.0)))); p["spread"] = float(prof.get("spread", p["spread"]))
        p["pump"] = min(0.6, p["pump"] * float(prof.get("pump_mult", 1.0))); p["a"] *= float(prof.get("a_mult", 1.0)); p["r"] *= float(prof.get("r_mult", 1.0))
        p["fq"] = p["fq"] + float(prof.get("fq_add", 0.0)); p["drive"] = max(float(p.get("drive", 0.0) or 0.0), float(prof.get("drive", 0.0))); p["hp"] = float(prof.get("hp", 0.0))
        if prof.get("bass_clean"):
            p["sub"] = 0.0                                  # the record's 808 / sub stays alone below 180 Hz
    return p


# ---------------------------------------------------------------------------
# sequencing on the absolute beat grid
# ---------------------------------------------------------------------------
def chord_at(chords, rel_frame):
    for ch in chords:
        if ch["a"] <= rel_frame < ch["b"]:
            return ch
    return chords[-1] if rel_frame >= chords[-1]["b"] else chords[0]


def plan_events(kind, chords, spec, preset, rng, voicing_state):
    """events: (start_frame, gate_frames, midi, velocity, osc_override) relative to the block start."""
    sr = spec["sr"]; bpm = spec["bpm"]; n = spec["n"]
    beat_n = 60.0 / max(40.0, bpm) * sr
    b0 = float(spec["b0"])                                  # absolute beat position of sample 0
    block_beats = n / beat_n
    ev = []
    energy = float(spec.get("energy", 0.5))
    if kind == "score":
        return [tuple(e) for e in (spec.get("events") or [])], voicing_state          # v31.25: the generative model wrote the notes
    if kind == "pad":
        for ch in chords:
            notes = voice_chord(ch["root"], ch["quality"], preset["register"], voicing_state, preset["voices"], preset["seventh"])
            voicing_state = notes
            for m in notes:
                ev.append((ch["a"], ch["b"] - ch["a"], m, 0.9, None))
            if preset["sub"] > 0:
                ev.append((ch["a"], ch["b"] - ch["a"], notes[0] - 12, preset["sub"], "sine"))
    elif kind == "arp":
        step = 0.25 if bpm <= 140.0 else 0.5
        swing = float(spec.get("swing", 0.0) or 0.0) * 0.25          # cells -> beats, on the off-beat sixteenths
        mode = ("up", "updown", "random")[int(spec.get("seed", 0)) % 3]
        k0 = int(math.ceil(b0 / step - 1e-6)); k1 = int(math.floor((b0 + block_beats) / step + 1e-6))
        for k in range(k0, k1 + 1):
            off = swing if (step == 0.25 and k % 2 == 1) else 0.0
            pos = int(round((k * step + off - b0) * beat_n))
            if pos < 0 or pos >= n:
                continue
            ch = chord_at(chords, pos)
            notes = voice_chord(ch["root"], ch["quality"], preset["register"], None, 3, False)
            pool = sorted(notes + [m + 12 for m in notes])
            if mode == "up":
                m = pool[k % len(pool)]
            elif mode == "updown":
                seq = pool + pool[-2:0:-1]; m = seq[k % len(seq)]
            else:
                m = pool[int(rng.integers(len(pool)))]
            if rng.random() < 0.10 * (1.0 - energy):
                continue                                    # rests breathe at low energy
            ev.append((pos, int(0.6 * step * beat_n), m, 0.72 + 0.28 * (k % 4 == 0), None))
    elif kind == "pluck":
        patt = PLUCK_PATTERNS.get(preset.get("pattern", "syncopated"), PLUCK_PATTERNS["syncopated"])
        bar0 = int(math.floor(b0 / 4.0)); bar1 = int(math.floor((b0 + block_beats) / 4.0))
        steps = spec.get("pattern_steps")
        if steps:
            hits = [(float(bt), float(v)) for bt, v in steps]                    # GrooveNet: absolute beat positions incl. micro-timing
        else:
            hits = [(bar * 4.0 + st * 0.25, 0.85) for bar in range(bar0, bar1 + 1) for st in patt]
        for beat_abs, vel_hit in hits:
            if True:
                pos = int(round((beat_abs - b0) * beat_n))
                if pos < 0 or pos >= n:
                    continue
                ch = chord_at(chords, pos)
                notes = voice_chord(ch["root"], ch["quality"], preset["register"], voicing_state, 3, False)
                voicing_state = notes
                for m in notes:
                    ev.append((pos, int(0.35 * beat_n), m, float(vel_hit), None))
                if preset["sub"] > 0:
                    ev.append((pos, int(0.30 * beat_n), notes[0] - 12, preset["sub"] * float(vel_hit), "sine"))
    elif kind == "lead":
        scale = set(spec.get("scale") or ())
        step = 0.5
        k0 = int(math.ceil(b0 / step - 1e-6)); k1 = int(math.floor((b0 + block_beats) / step + 1e-6))
        cur = None
        for k in range(k0, k1 + 1):
            pos = int(round((k * step - b0) * beat_n))
            if pos < 0 or pos >= n:
                continue
            ch = chord_at(chords, pos)
            tones = voice_chord(ch["root"], ch["quality"], preset["register"], None, 3, False)
            pool = sorted(tones + [m + 12 for m in tones])
            on_beat = (k % 2 == 0)
            slot_rng = np.random.default_rng(int(spec.get("seed", 0)) * 131 + (k % 16))   # the motif repeats every 2 bars
            if not on_beat and slot_rng.random() < 0.35:
                continue                                    # rest
            if cur is None or on_beat or slot_rng.random() < 0.5:
                if cur is None:
                    target = pool[int(slot_rng.integers(len(pool)))]
                else:
                    target = min(pool, key=lambda m: abs(m - cur) + 0.01 * slot_rng.random())
                    if slot_rng.random() < 0.5:
                        target = pool[min(len(pool) - 1, max(0, pool.index(target) + int(slot_rng.choice([-1, 1]))))]
                cur = target
            else:
                cand = [cur + d for d in (-2, -1, 1, 2) if (scale and (cur + d) % 12 in scale)] or [cur]
                cur = int(slot_rng.choice(cand))
            ev.append((pos, int(0.92 * step * beat_n), cur, 0.85, None))
    return ev, voicing_state


# ---------------------------------------------------------------------------
# v31.23.2 instrument engines (the "synth" can be any instrument)
# ---------------------------------------------------------------------------
def fm_voice(freq, sr, length, ratio, index, fm_decay):
    """2-operator FM: carrier sine, modulator at `ratio`, index decaying with `fm_decay` (e-piano, bells, mallets)."""
    t = np.arange(length) / sr
    phase = np.cumsum(np.asarray(freq, dtype=np.float64) / sr)
    idx = float(index) * np.exp(-t / max(0.02, float(fm_decay)))
    return np.sin(2.0 * np.pi * phase + idx * np.sin(2.0 * np.pi * float(ratio) * phase))


def ks_voice(f0, sr, length, decay, bright, rng):
    """Karplus-Strong plucked string, block-vectorised (each period depends only on the previous one)."""
    L = max(2, int(round(sr / max(20.0, float(f0)))))
    out = np.zeros(length + 2 * L)
    b = min(0.98, max(0.15, float(bright)))
    out[:L] = lfilter([b], [1.0, -(1.0 - b)], rng.uniform(-1.0, 1.0, L))
    k = 1
    while k * L < length + L:
        prev = out[(k - 1) * L:k * L]
        first = out[(k - 1) * L - 1] if k > 1 else prev[-1]
        out[k * L:(k + 1) * L] = float(decay) * 0.5 * (prev + np.concatenate(([first], prev[:-1])))
        k += 1
    return out[:length]


def additive_voice(freq, sr, length, harmonics, rng):
    """drawbar-style additive tone (organ)."""
    phase = np.cumsum(np.asarray(freq, dtype=np.float64) / sr)
    y = np.zeros(length)
    for h, amp in harmonics:
        y += float(amp) * np.sin(2.0 * np.pi * float(h) * phase + float(rng.random()) * 2.0 * np.pi)
    return y / (sum(float(a) for _h, a in harmonics) + 1e-9)


def formant_bank(y, sr, formants=((660.0, 0.15), (1100.0, 0.15), (2400.0, 0.2)), mix=0.7):
    """three band-passes at vowel formants ("ah"): a choir out of a saw ensemble."""
    out = np.zeros_like(y)
    for f, bw in formants:
        lo = f * (1.0 - bw); hi = min(0.45 * sr, f * (1.0 + bw))
        sos = butter(2, [lo / (sr / 2.0), hi / (sr / 2.0)], btype="band", output="sos")
        out += sosfilt(sos, y, axis=0)
    return y * (1.0 - mix) + out * (mix * 2.5)


def render_events(events, n, sr, preset, rng, vibrato=(0.0, 0.0)):
    y = np.zeros((n, 2), np.float64)
    engine = str(preset.get("engine", "va"))
    unison = int(preset.get("unison", 1)); det = float(preset.get("detune", 0.0)); spread = float(preset.get("spread", 0.5))
    r_n = int(preset["r"] * sr)
    glide = float(preset.get("glide", 0.0))
    trem_hz = float(preset.get("trem_hz", 0.0) or 0.0); trem_depth = float(preset.get("trem_depth", 0.0) or 0.0)
    prev_end = None; prev_midi = None
    for (s, g, midi, vel, osc_override) in sorted(events, key=lambda e: e[0]):
        s = int(s); g = int(max(8, g))
        length = min(n - s, g + r_n + int(0.05 * sr))
        if s < 0 or length <= 8:
            continue
        f0 = 440.0 * 2.0 ** ((midi - 69) / 12.0)
        env = adsr_env(length, sr, preset["a"], preset["d"], preset["s"], preset["r"], g)
        t = np.arange(length, dtype=np.float64)
        if trem_hz > 0:
            env = env * (1.0 - trem_depth * 0.5 * (1.0 - np.cos(2.0 * np.pi * trem_hz * t / sr)))
        freq = np.full(length, f0)
        if glide > 0 and prev_midi is not None and prev_end is not None and abs(prev_end - s) <= int(0.02 * sr):
            fp = 440.0 * 2.0 ** ((prev_midi - 69) / 12.0); gn = min(length, int(glide * sr))
            if gn > 1:
                freq[:gn] = fp * (f0 / fp) ** (np.arange(gn) / gn)
        if vibrato[0] > 0:
            freq = freq * 2.0 ** (vibrato[1] * np.sin(2.0 * np.pi * vibrato[0] * t / sr) * np.minimum(1.0, t / (0.25 * sr)))
        sig = np.zeros((length, 2))
        eng = "va" if osc_override == "sine" else engine
        if eng == "fm":
            w = fm_voice(freq, sr, length, preset.get("ratio", 1.0), preset.get("index", 2.0), preset.get("fm_decay", 0.4))
            sig[:, 0] = w * 0.72; sig[:, 1] = w * 0.72
        elif eng == "ks":
            w = ks_voice(f0, sr, length, preset.get("ks_decay", 0.996), preset.get("ks_bright", 0.5), rng)
            sig[:, 0] = w * 0.72; sig[:, 1] = w * 0.72
        elif eng == "additive":
            w = additive_voice(freq, sr, length, preset.get("harmonics", ((1, 1.0), (2, 0.6), (3, 0.3))), rng)
            click = int(0.004 * sr)
            if click < length:
                w[:click] += rng.uniform(-0.3, 0.3, click) * np.linspace(1.0, 0.0, click)
            sig[:, 0] = w * 0.72; sig[:, 1] = w * 0.72
        else:
            osc = osc_override or preset.get("osc", "saw")
            uni = 1 if osc == "sine" else max(1, unison)
            for i in range(uni):
                off = det * ((i / (uni - 1)) - 0.5) * 2.0 if uni > 1 else 0.0
                fu = freq * 2.0 ** (off / 12.0)
                dt = fu / sr
                phase = np.cumsum(dt) + float(rng.random())
                if osc == "sine":
                    w = osc_sine(phase, dt)
                elif osc == "pulse":
                    w = osc_pulse(phase, dt, 0.5 - 0.14 * (i % 2))
                else:
                    w = osc_saw(phase, dt)
                pan = 0.5 + spread * ((i / (uni - 1)) - 0.5) if uni > 1 else 0.5
                sig[:, 0] += w * math.cos(pan * math.pi / 2.0); sig[:, 1] += w * math.sin(pan * math.pi / 2.0)
            sig /= math.sqrt(uni)
        y[s:s + length] += sig * (env[:, None] * vel)
        prev_end = s + g; prev_midi = midi
    return y



def filter_trajectory(events, n, sr, preset, spec, kind):
    t = np.arange(n, dtype=np.float64) / sr
    fc = float(preset["fc"])
    if kind == "pad":
        env = np.minimum(1.0, t / max(0.3, 0.9 * n / sr))       # slow opening across the block
    else:
        env = np.zeros(n)
        d_n = max(1, int(preset["d"] * sr))
        for (s, g, _m, _v, _o) in events:
            s = int(s)
            if 0 <= s < n:
                seg = np.exp(-np.arange(n - s) / (d_n / 2.5))
                env[s:] = np.maximum(env[s:], seg)
    lfo = np.sin(2.0 * np.pi * preset["lfo"] * t) if preset["lfo"] > 0 and kind != "lead" else 0.0
    build = float(spec.get("build", 0.0))
    cut = fc * 2.0 ** (preset["fenv"] * env + preset["lfo_amt"] * lfo + build * (t / max(t[-1], 1e-6)) * 2.2)
    return cut


def render_block(spec, voicing_state=None):
    """spec: kind, sr, bpm, n, b0, chords [{a,b,root,quality,label}], style, seed, level_rms, energy,
    vocal, drums, continuation, build, swell, tail_s, scale, pump.
    Returns (audio (n+tail, 2) float32, info)."""
    sr = int(spec["sr"]); n = int(spec["n"]); kind = str(spec.get("kind", "pad"))
    rng = np.random.default_rng(int(spec.get("seed", 0)) & 0xFFFFFFFF)
    role = str(spec.get("role", "pad")) if kind == "score" else kind
    preset = make_preset(role, spec.get("style", ""), spec, spec.get("profile"), spec.get("variant"))
    if spec.get("continuation") and role == "pad":
        preset["a"] = 0.02                                       # the pad is already sounding: no re-attack
    if role == "pad" and spec.get("swell"):
        preset["a"] = max(preset["a"], 0.35)
    chords = spec["chords"]
    tail = int(float(spec.get("tail_s", 0.8 if kind != "pad" else 1.3)) * sr)
    total = n + tail
    events, voicing_state = plan_events(kind, chords, spec, preset, rng, voicing_state)
    vib = (preset["lfo"], preset["lfo_amt"]) if kind == "lead" else (float(preset.get("vib_hz", 0.0) or 0.0), float(preset.get("vib_oct", 0.0) or 0.0))
    kind_fx = role
    y = render_events(events, total, sr, preset, rng, vibrato=vib)
    if preset.get("formant"):
        y = formant_bank(y, sr)
    if preset.get("filter", True):
        cut = filter_trajectory(events, total, sr, preset, spec, kind_fx)
        y = modulated_lowpass(y, sr, cut, preset["fq"])
    drive = float(preset.get("drive", 0.0) or 0.0)
    if drive > 1e-3:
        g = 1.0 + 2.5 * drive; y = np.tanh(y * g) / g                    # gentle analog-style saturation
    hp = float(preset.get("hp", 0.0) or 0.0)
    if hp > 0:
        y = sosfilt(butter(2, hp / (sr / 2.0), btype="high", output="sos"), y, axis=0)
    y = chorus(y, sr, wet=preset["chorus"])
    y = pingpong_delay(y, sr, spec["bpm"], wet=preset["delay"])
    y = plate_reverb(y, sr, wet=preset["reverb"], seed=int(spec.get("seed", 0)))
    y = sidechain_pump(y, sr, spec["bpm"], preset["pump"] * float(spec.get("pump", 1.0)), float(spec["b0"]))
    tilt = spec.get("tilt_db")
    if tilt is not None:
        y = tilt_eq(y, sr, tilt)
    y, g = level_to(y, sr, float(spec.get("level_rms", 0.05)) * float(spec.get("level_mult", 1.0)))
    hist = np.zeros(12)
    for (_s, _g, _m, _v, _o) in events:
        hist[int(_m) % 12] += float(_g) * float(_v)
    if hist.sum() > 0:
        hist /= hist.sum()
    info = {"kind": kind, "events": len(events), "chords": " | ".join(c["label"] for c in chords), "gain": round(float(g), 3),
            "genre": preset.get("genre"), "pattern": preset.get("pattern"), "voicing": voicing_state, "note_hist": hist, "variant": preset.get("variant")}
    return y.astype(np.float32), info


# ---------------------------------------------------------------------------
# sampler: the freeze / roll on the drop's first hit
# ---------------------------------------------------------------------------
def freeze_pattern(beats):
    """(start_beat, length_beat) slices for a `beats`-long roll that tightens toward the release
    and leaves a silent gap (1/4 beat) before it."""
    pat = []
    if beats >= 4:
        pat += [(0.0, 0.5), (0.5, 0.5)]; b = 1.0
        pat += [(b + i * 0.25, 0.25) for i in range(4)]; b += 1.0
        pat += [(b + i * 0.125, 0.125) for i in range(8)]; b += 1.0
        rest = beats - b - 0.25
        k = int(round(rest / (1.0 / 12.0)))
        pat += [(b + i / 12.0, 1.0 / 12.0) for i in range(k)]
    elif beats >= 2:
        pat += [(0.0, 0.5), (0.5, 0.5)]; b = 1.0
        rest = beats - b - 0.25
        k = int(round(rest / 0.25))
        pat += [(b + i * 0.25, 0.25) for i in range(k)]
    else:
        rest = beats - 0.25
        k = max(1, int(round(rest / 0.25)))
        pat += [(i * 0.25, 0.25) for i in range(k)]
    return pat


def render_freeze(src, sr, bpm, beats, level_rms, seed=0):
    """`src`: programme audio (stereo) starting at the drop hit, >= 1 beat.  Returns the roll clip
    (exactly `beats` long) whose slices retrigger the hit with 3 ms fades, a rising high-pass and a
    silent gap before the release."""
    src = np.asarray(src, dtype=np.float64)
    if src.ndim == 1:
        src = np.stack([src, src], axis=1)
    beat_n = 60.0 / max(40.0, bpm) * sr
    n = int(round(beats * beat_n))
    out = np.zeros((n, 2))
    fade = max(8, int(0.003 * sr))
    pat = freeze_pattern(beats)
    total_slices = max(1, len(pat))
    for i, (sb, lb) in enumerate(pat):
        a = int(round(sb * beat_n)); ln = int(round(lb * beat_n))
        if a >= n or ln < 2 * fade:
            continue
        ln = min(ln, n - a, src.shape[0])
        grain = src[:ln].copy()
        w = np.ones(ln)
        w[:fade] = np.linspace(0.0, 1.0, fade); w[-fade:] = np.linspace(1.0, 0.0, fade)
        amp = 0.82 + 0.18 * (i / total_slices)
        out[a:a + ln] += grain * (w * amp)[:, None]
    # rising high-pass: the roll loses its floor toward the release (tension), chunked 2nd-order
    t = np.arange(n) / max(1, n)
    hp = 40.0 * (1400.0 / 40.0) ** (t ** 1.6)
    res = np.empty_like(out); zi = np.zeros((1, 2, 2)); chunk = 256
    for a in range(0, n, chunk):
        b = min(n, a + chunk)
        sos = butter(2, min(0.45 * sr, float(hp[(a + b) // 2])) / (sr / 2.0), btype="high", output="sos")
        res[a:b], zi = sosfilt(sos, out[a:b], axis=0, zi=zi)
    y, g = level_to(res, sr, level_rms)
    return y.astype(np.float32), {"slices": total_slices, "gain": round(float(g), 3), "beats": beats}


# ---------------------------------------------------------------------------
# the rack (thread-per-request, same callback shape as the retired SynthWeave client)
# ---------------------------------------------------------------------------
class SynthRack:
    def __init__(self, sr: int = 48000):
        self.sr = int(sr); self.enabled = True; self.busy = False; self.count = 0
        self._lock = threading.Lock(); self._voicing = {}; self._last_health = time.time(); self.last_ms = 0

    def health(self):
        self._last_health = time.time(); self.enabled = True
        return True

    def request(self, spec: dict, seq: int, on_done) -> bool:
        with self._lock:
            if self.busy:
                return False
            self.busy = True
        threading.Thread(target=self._run, args=(dict(spec), int(seq), on_done), daemon=True, name="joymetric-synth-rack").start()
        return True

    def compose(self, spec: dict):
        kind = str(spec.get("kind", "pad"))
        if kind == "freeze":
            buf, info = render_freeze(spec["src"], self.sr, float(spec["bpm"]), float(spec["beats"]), float(spec.get("level_rms", 0.05)), int(spec.get("seed", 0)))
            info["kind"] = "freeze"; info["chords"] = ""
            return buf, info
        spec = dict(spec); spec["sr"] = self.sr
        self.last_spec = {k: v for k, v in spec.items() if k not in ("src_clip", "src")}
        buf, info = render_block(spec, self._voicing.get(kind))
        self._voicing[kind] = info.pop("voicing", None)
        return buf, info

    def _run(self, spec, seq, on_done):
        t0 = time.time(); meta = {"seq": seq, "kind": spec.get("kind"), "bpm": spec.get("bpm")}
        buf = None
        try:
            buf, info = self.compose(spec)
            meta.update(info); meta["ok"] = True
            meta["rms"] = round(rms_hp(buf, self.sr), 4)
            src = spec.get("src_clip")
            if src is not None and spec.get("kind") != "freeze":
                c_src, ton = tonal_chroma(np.asarray(src, dtype=np.float32), self.sr)
                c_out, _ = tonal_chroma(buf, self.sr)
                meta["tonal_src"] = round(float(ton), 3)
                hist = info.get("note_hist")
                if hist is not None and float(np.sum(hist)) > 0:
                    meta["inkey"] = round(float(np.asarray(hist)[allowed_pitch_classes(c_src)].sum()), 3)
                else:
                    meta["inkey"] = round(float(np.asarray(c_out)[allowed_pitch_classes(c_src)].sum()), 3)
                meta["harm"] = round(chroma_agreement(c_src, c_out), 3)
                meta["note_hist"] = [round(float(v), 4) for v in (hist if hist is not None else np.zeros(12))]
        except Exception as exc:
            meta["ok"] = False; meta["error"] = str(exc)[:160]; buf = None
        meta["ms"] = int((time.time() - t0) * 1000); self.last_ms = meta["ms"]
        with self._lock:
            self.busy = False; self.count += 1
        try:
            on_done(seq, buf, meta)
        except Exception:
            pass
