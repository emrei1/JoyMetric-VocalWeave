"""BeatClock v2 (JoyMetric v31.17/v31.18) - song-level tempo / beat-phase tracker
plus groove clarity, downbeat and source-pattern analysis.

Why: the v31.16 agent estimated tempo from a 20 ms-hop autocorrelation peak.
The lag grid quantised BPM (60/(26*0.02)=115.38, 60/(36*0.02)=83.33), octave
flips (83 <-> 167) and stray peaks (125) re-anchored the beat grid every second,
so every added layer wobbled around the beat of the record and grid confidence
never cleared the legality gate.

v31.18 GrooveTruth adds what a DJ does before touching a record:
  * groove clarity - how much of the record's own transients actually sit on a
    16th grid (a dreamy, beat-less passage must not receive a kick pattern)
  * downbeat estimate - kick weight, snare penalty and spectral change per
    beat class, so bar-level patterns and fills start on the "1"
  * source patterns - the record's own kick / snare / hat 16-step accents so
    added layers can follow instead of contradict

Design (online beat tracking literature: comb-filter tempograms, Ellis 2007
dynamic-programming beat tracking, Boeck/Krebs tempo memory, Krebs/Boeck
bar-pointer downbeat models):
  * log-compressed spectral-flux onset envelope at 5 ms hop (vectorised)
  * comb-enhanced autocorrelation tempo profile, parabolic sub-lag refinement
  * song-level tempo memory: decayed profile on a fixed BPM grid, octave-aware
    lock with hysteresis (relock only after persistent disagreement)
  * fractional-period phase comb weighted towards the window end
  * calibrated confidence in [0,1] from peak prominence, periodicity strength,
    lock agreement and lock age
"""
from __future__ import annotations
import math
import numpy as np
try:
    from scipy import fft as _sfft          # float32-native, ~2x numpy.fft here
except Exception:                            # pragma: no cover
    _sfft = None

SR = 6000            # analysis rate (matches agentic_live_dj.SR_ANALYSIS)
HOP = 30             # 5 ms
FRAME = 240          # 40 ms
HOP_S = HOP / SR
ONSET_LAT_FRAMES = 1.8   # flux peak lags the physical onset by ~9 ms (calibrated on a synthetic kick track)
STRENGTH_GATE = (0.06, 0.18)   # periodicity strength below/above which confidence is 0 / ungated
BPM_LO, BPM_HI, BPM_STEP = 55.0, 210.0, 0.25
DJ_LO, DJ_HI = 68.0, 158.0
_BPM_GRID = np.arange(BPM_LO, BPM_HI + 1e-9, BPM_STEP)
_WIN = np.hanning(FRAME).astype(np.float32)
_FREQS = np.fft.rfftfreq(FRAME, 1.0 / SR)
_BIN_W = np.where(_FREQS < 200.0, 2.0, 1.0).astype(np.float32)   # kick emphasis
_BIN_W /= float(np.mean(_BIN_W))
_LOW = _FREQS < 130.0          # kick fundamentals; keeps most of the snare body (150-250 Hz) out
HF_LAT_FRAMES = -2.0            # rectangular 5 ms HF envelope, after STFT_SHIFT: peak 2 frames before the STFT time base (measured)
LOW_LAT_FRAMES = 1.6            # linear low-band STFT flux peak position (measured on a 160->55 Hz kick: median +1.6 frames)
MID_LAT_FRAMES = 1.2            # linear mid-band STFT flux peak position (measured on a snare: median +1.2 frames)
STFT_SHIFT = FRAME // HOP       # STFT frames index time by their window END (5i+40 ms); rectangular HF frames by 5i
_MID = (_FREQS >= 200.0) & (_FREQS < 2000.0)


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def _norm_env(on: np.ndarray) -> np.ndarray:
    """Median-floored, robustly scaled envelope in [0,1].  The scale is the 97th
    percentile of the non-zero flux, not the maximum: one huge transient (a chord
    stab, a vocal plosive) must not push every hat below the peak-picking threshold."""
    on = np.maximum(on - float(np.median(on)), 0.0)
    m = float(np.max(on)) if on.size else 0.0
    if m > 0:
        on /= m
    return on


def envelopes(x6k: np.ndarray) -> dict:
    """5 ms-hop log-flux envelopes from one STFT: all (kick-weighted), low, mid,
    plus the per-frame log spectrum (for beat-level spectral change)."""
    x = np.asarray(x6k, dtype=np.float32)
    if x.ndim == 2:
        x = x.mean(axis=1)
    n = 1 + (len(x) - FRAME) // HOP
    if n < 8:
        z = np.zeros(max(0, n), dtype=np.float64)
        return {"all": z, "low": z.copy(), "mid": z.copy(), "spec": np.zeros((max(0, n), len(_FREQS)), dtype=np.float32)}
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n)[:, None]
    frames = x[idx] * _WIN[None, :]
    if _sfft is not None:
        spec = _sfft.rfft(frames, axis=1, overwrite_x=True)
    else:
        spec = np.fft.rfft(frames, axis=1)
    mag = np.abs(spec).astype(np.float32, copy=False)
    lg = np.log1p(20.0 * mag)
    d = np.maximum(lg[1:] - lg[:-1], 0.0)
    # kick / snare bands use LINEAR flux: the log domain turns the noise floor of a
    # pad or a room into hundreds of tiny "onsets" in the low band, and the peak
    # position of a log onset depends on loudness (window-entry effect)
    dl = np.maximum(mag[1:] - mag[:-1], 0.0)
    out = {}
    out["all"] = _norm_env(np.concatenate(([0.0], (d * _BIN_W[None, :]).mean(axis=1))).astype(np.float64))
    out["low"] = _norm_env(np.concatenate(([0.0], dl[:, _LOW].mean(axis=1))).astype(np.float64))
    out["mid"] = _norm_env(np.concatenate(([0.0], dl[:, _MID].mean(axis=1))).astype(np.float64))
    out["spec"] = lg
    return out


def onset_envelope(x6k: np.ndarray) -> np.ndarray:
    """Log-flux onset strength at 5 ms hop, median-floored, peak-normalised."""
    return envelopes(x6k)["all"]


def hf_flux(x: np.ndarray, sr: int) -> np.ndarray:
    """5 ms-hop flux of the high band (>~6 kHz via first difference) from the
    full-rate signal - hats live above the 6 kHz analysis rate."""
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 2:
        x = x.mean(axis=1)
    hop = max(1, int(round(sr * HOP_S)))
    # a real high-pass: the first difference used before was reacting to every
    # sharp low-frequency onset (kick attack, chord start) as if it were a hat
    try:
        from scipy.signal import butter as _butter, sosfilt as _sosfilt
        sos = _butter(2, min(5000.0, 0.45 * sr), btype="high", fs=sr, output="sos")
        h = np.abs(_sosfilt(sos, x).astype(np.float32))
    except Exception:
        h = np.abs(np.diff(x, prepend=x[:1]))
    n = len(h) // hop
    if n < 8:
        return np.zeros(max(0, n), dtype=np.float64)
    env = np.log1p(40.0 * h[:n * hop].reshape(n, hop).mean(axis=1).astype(np.float64))
    flux = np.maximum(np.diff(env, prepend=env[:1]), 0.0)
    return _norm_env(flux)


def _autocorr(z: np.ndarray) -> np.ndarray:
    n = len(z)
    nfft = 1 << int(math.ceil(math.log2(2 * n)))
    f = np.fft.rfft(z, nfft)
    ac = np.fft.irfft(f * np.conj(f), nfft)[:n]
    return ac / (ac[0] + 1e-12)


def tempo_profile(onset: np.ndarray):
    """Comb-enhanced autocorrelation on the BPM grid (0..1)."""
    z = onset - float(np.mean(onset))
    if len(z) < 64 or float(np.std(z)) < 1e-9:
        return np.zeros_like(_BPM_GRID), np.zeros(1), 0.0
    ac = _autocorr(z)
    n = len(ac)
    lag = 60.0 / (_BPM_GRID * HOP_S)                   # fractional lag per grid bpm

    def interp(l):
        l = np.asarray(l, dtype=np.float64)
        i0 = np.floor(l).astype(int)
        fr = l - i0
        ok = (i0 >= 1) & (i0 + 1 < n)
        v = np.zeros_like(l)
        v[ok] = ac[i0[ok]] * (1 - fr[ok]) + ac[i0[ok] + 1] * fr[ok]
        return v

    comb = interp(lag) + 0.55 * interp(2 * lag) + 0.35 * interp(3 * lag) + 0.20 * interp(4 * lag)
    comb = np.maximum(comb, 0.0)
    strength = float(np.max(comb))
    if strength > 0:
        comb = comb / strength
    return comb, ac, strength


def _peaks(env: np.ndarray, thr: float = 0.22, min_gap: int = 3) -> np.ndarray:
    """Local maxima above thr*max with a minimum spacing (frames)."""
    if len(env) < 3:
        return np.zeros(0, dtype=int)
    m = float(np.max(env))
    if m <= 0:
        return np.zeros(0, dtype=int)
    c = env[1:-1]
    cand = np.where((c >= env[:-2]) & (c >= env[2:]) & (c > thr * m))[0] + 1
    if len(cand) == 0:
        return cand
    keep = [int(cand[0])]
    for i in cand[1:]:
        if i - keep[-1] >= min_gap:
            keep.append(int(i))
        elif env[i] > env[keep[-1]]:
            keep[-1] = int(i)
    return np.asarray(keep, dtype=int)


def _grid_fraction(peaks: np.ndarray, last_beat: float, per: float, sub: int = 4, tol: float = 3.0):
    """Fraction of peak frames within tol frames of the sub-grid anchored at last_beat."""
    if len(peaks) == 0:
        return 0.0, 0.0
    g = per / sub
    dev = np.mod(peaks - last_beat, g)
    dev = np.minimum(dev, g - dev)
    frac = float(np.mean(dev <= tol))
    chance = min(1.0, 2.0 * tol / g)
    return frac, chance


def groove_clarity(envs: dict, hf: np.ndarray, bpm: float, frac_end: float, strength: float, lock_count: int) -> dict:
    """How much of the record's own transient content sits on the 16th grid."""
    if bpm <= 0:
        return {"clarity": 0.0, "align": 0.0, "kick_on": 0.0, "hat_on": 0.0, "mid_on": 0.0}
    per = 60.0 / bpm / HOP_S
    n = len(envs["all"])
    last_beat = (n - 1 + ONSET_LAT_FRAMES) - frac_end * per
    res = {}
    tot = 0.0; wsum = 0.0
    for name, env, w, lat in (("kick_on", envs["low"], 1.2, LOW_LAT_FRAMES), ("mid_on", envs["mid"], 0.8, MID_LAT_FRAMES), ("hat_on", hf, 1.0, HF_LAT_FRAMES)):
        if env is None or len(env) < 16:
            res[name] = 0.0
            continue
        pk = _peaks(env)
        frac, chance = _grid_fraction(pk - lat, last_beat, per)
        ex = max(0.0, (frac - chance) / max(1e-6, 1.0 - chance))
        res[name] = round(frac, 3)
        cnt = float(len(pk))
        tot += ex * cnt * w; wsum += cnt * w
    align = tot / wsum if wsum > 0 else 0.0
    align_n = _clamp((align - 0.05) / 0.40, 0.0, 1.0)
    strength_n = _clamp((strength - 0.25) / 0.90, 0.0, 1.0)
    stability = _clamp(lock_count / 6.0, 0.0, 1.0)
    clarity = _clamp(0.65 * align_n + 0.35 * strength_n, 0.0, 1.0) * (0.6 + 0.4 * stability)
    res.update({"clarity": float(clarity), "align": float(align)})
    return res


def _sample(env: np.ndarray, pos: np.ndarray, half: int = 2) -> np.ndarray:
    """Max of env over pos +- half frames (pos may be fractional / out of range)."""
    out = np.zeros(len(pos))
    n = len(env)
    for i, p in enumerate(pos):
        a = int(math.floor(p)) - half; b = int(math.ceil(p)) + half
        if b < 0 or a >= n:
            out[i] = -1.0
            continue
        out[i] = float(np.max(env[max(0, a):min(n, b + 1)]))
    return out


def downbeat_at_end(envs: dict, bpm: float, frac_end: float, n_beats: int = 32) -> tuple[int, float, list]:
    """Which of the last beats are downbeats.

    Returns (beats_since_downbeat_at_last_beat in 0..3, confidence, class scores).
    Score per beat class: kick weight + accent - snare penalty + spectral change
    (chord/section changes land on the "1")."""
    if bpm <= 0:
        return 0, 0.0, [0, 0, 0, 0]
    per = 60.0 / bpm / HOP_S
    n = len(envs["all"])
    last_beat = (n - 1 + ONSET_LAT_FRAMES) - frac_end * per
    k = np.arange(n_beats)
    pos = last_beat - k * per
    valid = pos >= 0
    if valid.sum() < 8:
        return 0, 0.0, [0, 0, 0, 0]
    # flux peaks sit ONSET_LAT_FRAMES after the physical onset: sample there
    L = _sample(envs["low"], pos + LOW_LAT_FRAMES); M = _sample(envs["mid"], pos + MID_LAT_FRAMES); A = _sample(envs["all"], pos + ONSET_LAT_FRAMES)
    spec = envs.get("spec")
    H = np.zeros(n_beats)
    if spec is not None and len(spec) > 8:
        def beat_spec(p):
            a = int(max(0, math.floor(p))); b = int(min(len(spec), math.floor(p + per)))
            if b - a < 2:
                return None
            v = spec[a:b].mean(axis=0)
            return v / (np.linalg.norm(v) + 1e-9)
        for i in range(n_beats - 1):
            if not (valid[i] and valid[i + 1]):
                continue
            s0 = beat_spec(pos[i]); s1 = beat_spec(pos[i + 1])      # this beat vs the beat before it
            if s0 is not None and s1 is not None:
                H[i] = 1.0 - float(np.dot(s0, s1))
        if H.max() > 0:
            H = H / H.max()
    score = np.zeros(4); cnt = np.zeros(4)
    for i in range(n_beats):
        if not valid[i] or L[i] < 0:
            continue
        c = i % 4
        w = 0.85 ** (i // 4)            # recent bars weigh more
        score[c] += w * (1.0 * L[i] + 0.5 * A[i] - 0.6 * M[i] + 0.8 * H[i])
        cnt[c] += w
    score = score / np.maximum(cnt, 1e-9)
    order = np.argsort(score)[::-1]
    best, second = float(score[order[0]]), float(score[order[1]])
    conf = _clamp((best - second) / (abs(best) + 1e-6), 0.0, 1.0) if best > 0 else 0.0
    return int(order[0]), float(conf), [round(float(s), 3) for s in score]


def source_patterns(envs: dict, hf: np.ndarray, bpm: float, frac_end: float, bar_pos_end: float, bars: int = 4) -> dict:
    """16-step accent patterns of the record itself (kick / snare / hat), step 0 = downbeat."""
    if bpm <= 0:
        z = [0.0] * 16
        return {"kick": z, "snare": list(z), "hat": list(z)}
    per = 60.0 / bpm / HOP_S
    n = len(envs["all"])
    end = n - 1 + ONSET_LAT_FRAMES
    last_down = end - bar_pos_end * per
    out = {}
    for name, env, lat in (("kick", envs["low"], LOW_LAT_FRAMES), ("snare", envs["mid"], MID_LAT_FRAMES), ("hat", hf, HF_LAT_FRAMES)):
        acc = np.zeros(16); c = 0
        if env is not None and len(env) >= 16:
            for b in range(bars):
                base = last_down - b * 4 * per
                if base < 0:
                    break
                pos = base + np.arange(16) * (per / 4.0) + lat
                v = _sample(env, pos, half=1)
                if np.any(v < 0):
                    continue
                acc += v; c += 1
        if c > 0:
            acc /= c
            m = float(acc.max())
            if m > 0:
                acc /= m
        out[name] = [round(float(v), 3) for v in acc]
    return out


class BeatClock:
    def __init__(self, memory_tau_s: float = 30.0):
        self.memory_tau_s = float(memory_tau_s)
        self.reset()

    def reset(self):
        self.profile = np.zeros_like(_BPM_GRID)
        self.locked_bpm = 0.0
        self.lock_count = 0
        self.disagree_streak = 0
        self.candidate_bpm = 0.0
        self.inst_disagree = 0
        self.last_t = None
        self.last_inst_bpm = 0.0
        self.last_conf = 0.0
        self.last_strength = 0.0
        self.relocks = 0

    # ---- tempo -----------------------------------------------------------
    def _peak_bpm(self, prof: np.ndarray, lo: float, hi: float):
        mask = (_BPM_GRID >= lo) & (_BPM_GRID <= hi)
        if not np.any(mask):
            return 0.0, 0.0, 0
        sub = np.where(mask, prof, -1.0)
        i = int(np.argmax(sub))
        v = float(prof[i])
        if 0 < i < len(prof) - 1:
            a, b, c = prof[i - 1], prof[i], prof[i + 1]
            den = a - 2 * b + c
            d = 0.5 * (a - c) / den if abs(den) > 1e-12 else 0.0
            d = _clamp(d, -0.5, 0.5)
        else:
            d = 0.0
        return float(_BPM_GRID[i] + d * BPM_STEP), v, i

    def _harmonic_score(self, prof: np.ndarray) -> np.ndarray:
        """Octave-aware support: a tempo is backed by its double and half."""
        def at(b):
            idx = np.clip(np.round((b - BPM_LO) / BPM_STEP).astype(int), 0, len(prof) - 1)
            v = prof[idx]
            v[(b < BPM_LO) | (b > BPM_HI)] = 0.0
            return v
        b = _BPM_GRID
        prior = 0.80 + 0.20 * np.exp(-0.5 * ((b - 118.0) / 34.0) ** 2)
        return (prof + 0.40 * at(2 * b) + 0.40 * at(b / 2) + 0.15 * at(3 * b) + 0.15 * at(b / 3)) * prior

    def _nearest_local_peak(self, prof: np.ndarray, bpm: float, band: float = 0.025) -> float:
        """Track our own peak: the local maximum nearest the lock inside +-band.

        A broad or double-humped tempo peak (dreamy, sparse records) makes the
        global argmax hop by 1-2 % for seconds at a time; a mature lock must
        stay on the hump it locked to as long as that hump exists.
        """
        lo = bpm * (1.0 - band); hi = bpm * (1.0 + band)
        i_lo = max(1, int(round((lo - BPM_LO) / BPM_STEP))); i_hi = min(len(prof) - 2, int(round((hi - BPM_LO) / BPM_STEP)))
        if i_hi <= i_lo:
            return bpm
        best = -1; bestv = -1.0
        for j in range(i_lo, i_hi + 1):
            if prof[j] >= prof[j - 1] and prof[j] >= prof[j + 1]:
                close = 1.0 - 0.5 * abs(float(_BPM_GRID[j]) - bpm) / (bpm * band)
                v = float(prof[j]) * close
                if v > bestv:
                    bestv = v; best = j
        if best < 0:
            return bpm
        a, b, c = prof[best - 1], prof[best], prof[best + 1]
        den = a - 2 * b + c
        d = 0.5 * (a - c) / den if abs(den) > 1e-12 else 0.0
        return float(_BPM_GRID[best] + _clamp(d, -0.5, 0.5) * BPM_STEP)

    def _second_peak(self, score: np.ndarray, bpm: float) -> float:
        rel = np.abs(_BPM_GRID - bpm) / max(bpm, 1.0)
        mask = rel > 0.06
        return float(np.max(score[mask])) if np.any(mask) else 0.0

    def observe(self, onset: np.ndarray, now: float, weight: float = 1.0) -> dict:
        comb, ac, strength = tempo_profile(onset)
        if self.last_t is not None and now > self.last_t:
            self.profile *= math.exp(-(now - self.last_t) / self.memory_tau_s)
        self.last_t = now
        # strong, unambiguous windows shape the memory; noise barely touches it
        self.profile += float(weight) * comb * _clamp(strength / 0.25, 0.15, 1.0)
        self.last_strength = strength
        hs_inst = self._harmonic_score(comb)
        inst_bpm, _, _ = self._peak_bpm(hs_inst, DJ_LO, DJ_HI)
        hs_mem = self._harmonic_score(self.profile / (float(np.max(self.profile)) + 1e-12))
        mem_bpm, mem_v, _ = self._peak_bpm(hs_mem, DJ_LO, DJ_HI)
        second = self._second_peak(hs_mem, mem_bpm)
        prominence = (mem_v - second) / (mem_v + 1e-9)          # 0..1
        self.last_inst_bpm = inst_bpm
        if self.locked_bpm <= 0.0:
            self.locked_bpm = mem_bpm
            self.lock_count = 1
        else:
            rel = abs(mem_bpm - self.locked_bpm) / self.locked_bpm
            if rel < 0.025:
                # refinement toward the memory estimate: quick while the lock is
                # young, then slow - a mature lock must not follow a few seconds of
                # argmax jitter inside a broad peak (seen live: 120.4 -> 122.3 -> 120.0)
                gain = 0.5 if self.lock_count < 3 else (0.25 if self.lock_count < 12 else 0.12)
                target = self._nearest_local_peak(hs_mem, self.locked_bpm) if self.lock_count >= 3 else mem_bpm
                self.locked_bpm += (target - self.locked_bpm) * gain
                self.lock_count += 1
                self.disagree_streak = 0
            else:
                # Metrically related estimate (octave, triple)?  A DJ keeps the lock
                # through such sections, so the disagreement must persist much longer
                # before a relock.  A NEAR tempo (2.5-8 % away: a neighbouring hump
                # of a broad peak, seen live as 135 <-> 140 on a dreamy record) is
                # almost never a new record either, so it needs a long streak too.
                # An unrelated tempo (new record) relocks after a short streak.
                ratio = mem_bpm / self.locked_bpm
                related = min(abs(ratio - r) for r in (2.0, 0.5, 3.0, 1.0 / 3.0)) < 0.03
                near = rel < 0.08
                if abs(mem_bpm - self.candidate_bpm) / max(mem_bpm, 1.0) < 0.02:
                    self.disagree_streak += 1
                else:
                    self.disagree_streak = 1
                self.candidate_bpm = mem_bpm
                if related:
                    need = max(8, min(16, self.lock_count // 2))
                elif near:
                    need = max(8, min(14, self.lock_count // 3))
                else:
                    need = 3
                if self.disagree_streak >= need:
                    self.locked_bpm = mem_bpm
                    self.lock_count = 1
                    self.disagree_streak = 0
                    self.relocks += 1
                elif self.disagree_streak >= 2 and not related and not near:
                    # a new record is taking over: let the old memory fade fast
                    self.profile *= 0.6
        rel_i = abs(inst_bpm - self.locked_bpm) / self.locked_bpm
        agree = math.exp(-(rel_i / 0.02) ** 2)
        # A run of windows whose own periodicity contradicts the lock (and is not
        # an octave of it) means the record changed: let the old memory fade fast
        # so the relock does not wait for 30 s of evidence to be outweighed.
        ratio_i = inst_bpm / self.locked_bpm
        related_i = min(abs(ratio_i - r) for r in (1.0, 2.0, 0.5, 3.0, 1.0 / 3.0)) < 0.03
        if strength >= STRENGTH_GATE[0] and not related_i and rel_i >= 0.08:
            self.inst_disagree += 1
        else:
            self.inst_disagree = 0
        if self.inst_disagree >= 3:
            self.profile *= 0.55
        # Periodicity gate: white noise (strength ~0.03) gets ~0 whatever the
        # lock history says; a sparse but steady record (~0.2) is ungated.
        gate = _clamp((strength - STRENGTH_GATE[0]) / (STRENGTH_GATE[1] - STRENGTH_GATE[0]), 0.0, 1.0)
        conf = gate * (0.25 * _clamp(prominence / 0.35, 0.0, 1.0)
                       + 0.20 * _clamp(strength / 0.25, 0.0, 1.0)
                       + 0.28 * agree
                       + 0.27 * _clamp(self.lock_count / 6.0, 0.0, 1.0))
        self.last_conf = _clamp(conf, 0.0, 1.0)
        return {"bpm": float(self.locked_bpm), "bpm_inst": float(inst_bpm), "bpm_mem": float(mem_bpm),
                "conf": float(self.last_conf), "prominence": float(prominence), "strength": float(strength),
                "lock_count": int(self.lock_count), "relocks": int(self.relocks), "ac": ac}

    # ---- phase -----------------------------------------------------------
    @staticmethod
    def phase_at_end(onset: np.ndarray, bpm: float, n_beats: int = 8, steps: int = 96) -> tuple[float, float]:
        """Return (fraction of a beat elapsed since the last beat at the window end, score)."""
        if bpm <= 0 or len(onset) < 16:
            return 0.0, 0.0
        per = 60.0 / bpm / HOP_S
        # the flux peak of an onset sits ONSET_LAT_FRAMES after the onset itself, so the
        # comb is anchored that far beyond the last frame (beats inside the latency
        # gap simply do not vote; the seven earlier beats carry the estimate)
        end = len(onset) - 1 + ONSET_LAT_FRAMES
        k = np.arange(n_beats)[:, None]
        w = (0.82 ** k)
        phi = (np.arange(steps) / steps)[None, :]
        pos = end - (phi + k) * per                     # (n_beats, steps)
        i0 = np.floor(pos).astype(int)
        fr = pos - i0
        ok = (i0 >= 0) & (i0 + 1 < len(onset))
        v = np.zeros_like(pos)
        v[ok] = onset[i0[ok]] * (1 - fr[ok]) + onset[i0[ok] + 1] * fr[ok]
        score = (v * w).sum(axis=0) / float(w.sum())
        i = int(np.argmax(score))
        a, b, c = score[(i - 1) % steps], score[i], score[(i + 1) % steps]
        den = a - 2 * b + c
        d = 0.5 * (a - c) / den if abs(den) > 1e-12 else 0.0
        d = _clamp(d, -0.5, 0.5)
        frac = ((i + d) / steps) % 1.0
        return float(frac), float(b)

    def analyze(self, x6k: np.ndarray, now: float, weight: float = 1.0, x_full=None, sr_full: int = 0, groove: bool = False) -> dict:
        envs = envelopes(x6k)
        onset = envs["all"]
        if len(onset) < int(3.0 / HOP_S):
            return {"bpm": float(self.locked_bpm or 100.0), "bpm_confidence": 0.0, "beat_sec": 60.0 / float(self.locked_bpm or 100.0),
                    "to_next_beat_sec": 0.0, "since_last_beat_sec": 0.0, "onset": onset}
        t = self.observe(onset, now, weight)
        bpm = t["bpm"]
        frac, pscore = self.phase_at_end(onset, bpm)
        beat_sec = 60.0 / bpm
        since = frac * beat_sec
        to_next = (beat_sec - since) % beat_sec
        conf = t["conf"] * (0.75 + 0.25 * _clamp(pscore / 0.30, 0.0, 1.0))
        res = {"bpm": bpm, "bpm_confidence": float(_clamp(conf, 0.0, 1.0)), "beat_sec": beat_sec,
               "to_next_beat_sec": float(to_next), "since_last_beat_sec": float(since),
               "bpm_inst": t["bpm_inst"], "bpm_mem": t["bpm_mem"], "prominence": t["prominence"],
               "strength": t["strength"], "lock_count": t["lock_count"], "relocks": t["relocks"],
               "phase_score": float(pscore), "onset": onset}
        if groove:
            hf = hf_flux(x_full, int(sr_full)) if (x_full is not None and sr_full) else None
            if hf is not None:
                # STFT frame i stands for the time 5i+40 ms (window end); HF frame i for 5i ms.
                # Shift the HF envelope by FRAME/HOP frames so both index the same instant.
                hf = hf[STFT_SHIFT:]
                if len(hf) > len(onset):
                    hf = hf[:len(onset)]
                elif len(hf) < len(onset):
                    hf = np.concatenate((hf, np.zeros(len(onset) - len(hf))))
            gc = groove_clarity(envs, hf if hf is not None else envs["mid"], bpm, frac, t["strength"], t["lock_count"])
            db_class, db_conf, db_scores = downbeat_at_end(envs, bpm, frac)
            bar_pos = float(db_class) + frac
            pats = source_patterns(envs, hf if hf is not None else envs["mid"], bpm, frac, bar_pos)
            res.update({"groove_clarity": float(gc["clarity"]), "groove_align": float(gc["align"]),
                        "kick_on_grid": float(gc["kick_on"]), "hat_on_grid": float(gc["hat_on"]), "mid_on_grid": float(gc["mid_on"]),
                        "downbeat_class": int(db_class), "downbeat_conf": float(db_conf), "downbeat_scores": db_scores,
                        "bar_pos": bar_pos, "src_kick": pats["kick"], "src_snare": pats["snare"], "src_hat": pats["hat"]})
        return res
