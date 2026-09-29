from __future__ import annotations

import base64
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any



DEFAULT_PARAMS = {
    # 50D Gradient Lab parameters. Neutral values are intentionally transparent.
    "low_cut_hz": 20.0,
    "low_cut_q": 0.707,
    "sub_db": 0.0,
    "sub_freq_hz": 58.0,
    "bass_db": 0.0,
    "bass_freq_hz": 120.0,
    "body_db": 0.0,
    "body_freq_hz": 850.0,
    "body_q": 0.72,
    "mid_db": 0.0,
    "mid_freq_hz": 1450.0,
    "mid_q": 0.82,
    "presence_db": 0.0,
    "presence_freq_hz": 3000.0,
    "presence_q": 0.85,
    "air_db": 0.0,
    "air_freq_hz": 8000.0,
    "high_cut_hz": 20000.0,
    "high_cut_q": 0.707,

    "compression": 0.0,
    "comp_threshold_db": -18.0,
    "comp_ratio": 3.0,
    "comp_attack_ms": 7.5,
    "comp_release_ms": 165.0,
    "comp_knee_db": 6.0,
    "comp_makeup_db": 0.0,
    "transient_attack": 0.0,
    "transient_sustain": 0.0,

    "drive": 0.0,
    "saturation_mix": 0.34,
    "saturation_asymmetry": 0.0,
    "low_drive": 0.0,
    "mid_drive": 0.0,
    "high_drive": 0.0,

    "width": 1.0,
    "bass_mono_hz": 20.0,
    "side_tilt_db": 0.0,
    "stereo_balance": 0.0,
    "hypnotic_motion_depth": 0.0,
    "hypnotic_rate_hz": 0.18,
    "motion_phase": 0.25,

    "reverb": 0.0,
    "reverb_decay": 1.8,
    "reverb_predelay_ms": 0.0,
    "reverb_diffusion": 0.62,
    "reverb_damping": 0.62,
    "reverb_tone_hz": 11800.0,

    "delay": 0.0,
    "delay_ms": 285.0,
    "delay_feedback": 0.08,

    # Hidden safety trim: not part of the 50 AI-owned dimensions.
    "output_db": 0.0,
}


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def _enter_windows_pro_audio_thread():
    """Best-effort MMCSS + thread priority boost for realtime audio threads.

    The semantic worker is deliberately BelowNormal; audio threads are marked as
    Pro Audio so a CLAP CPU inference cannot steal enough scheduler time to cause
    WASAPI starvation. Non-Windows/test environments simply no-op.
    """
    if os.name != "nt":
        return None
    try:
        k = _typed_kernel32()
        k.SetThreadPriority(ctypes_void_p(k.GetCurrentThread()), 2)  # THREAD_PRIORITY_HIGHEST (v31.30.8: typed handle)
    except Exception:
        pass
    try:
        import ctypes
        avrt = ctypes.windll.avrt
        avrt.AvSetMmThreadCharacteristicsW.restype = ctypes.c_void_p
        avrt.AvSetMmThreadCharacteristicsW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
        avrt.AvRevertMmThreadCharacteristics.argtypes = [ctypes.c_void_p]
        task_index = ctypes.c_ulong(0)
        handle = avrt.AvSetMmThreadCharacteristicsW("Pro Audio", ctypes.byref(task_index))
        return handle or None
    except Exception:
        return None


_BATT_CACHE = {"t": 0.0, "on": False}
def _on_battery():
    """v31.30.26: True when running on battery (AC unplugged). Cached ~2 s; never raises."""
    import time as _t, os as _os
    if _os.environ.get("JOY_FORCE_BATTERY", "0") == "1":
        return True
    now = _t.time()
    if now - _BATT_CACHE["t"] > 2.0:
        _BATT_CACHE["t"] = now
        try:
            import ctypes as _c
            class _SPS(_c.Structure):
                _fields_ = [("ACLineStatus", _c.c_ubyte), ("BatteryFlag", _c.c_ubyte), ("BatteryLifePercent", _c.c_ubyte), ("SystemStatusFlag", _c.c_ubyte), ("BatteryLifeTime", _c.c_ulong), ("BatteryFullLifeTime", _c.c_ulong)]
            sps = _SPS(); ok = _c.windll.kernel32.GetSystemPowerStatus(_c.byref(sps))
            _BATT_CACHE["on"] = bool(ok) and int(sps.ACLineStatus) == 0
        except Exception:
            _BATT_CACHE["on"] = False
    return _BATT_CACHE["on"]
def _fx_gradient_scale():
    """v31.30.27: how much to stretch the FX critic's interval.  The critic (CLAP semantic gradient) pins ~95 % of a core
    at the native ~0.82 s rate and that is the biggest single CPU consumer -> busy-passage / battery crackle.  For
    VocalWeave the FX SOUND is the 15D StudioDSP processing (a separate per-block path); the critic only nudges its
    targets toward the prompt, so it can run far slower with no audible change (a static prompt converges in a few
    passes).  So it is slowed on AC too (x4 -> ~3.3 s), and much more on battery."""
    import os as _o
    try:
        ac = max(1.0, float(_o.environ.get("JOY_FX_GRADIENT_MULT", "4.0")))
    except Exception:
        ac = 4.0
    if not _on_battery():
        return ac
    try:
        return ac * max(1.0, float(_o.environ.get("JOY_FX_GRADIENT_BATT_MULT", "5.0")))
    except Exception:
        return ac * 5.0


def _fx_enabled():
    """v31.30.28: is the 15D StudioDSP FX + CLAP critic on?  Default OFF (VocalWeave-only, lightest audio path)."""
    import os as _o
    return _o.environ.get("JOY_DSP_FX", "0") == "1"
def _fx_bypass_limit(x):
    """v31.30.28: FX bypassed - pass the VocalWeave mix through with only a cheap peak safety (soft-clip hot blocks)."""
    import numpy as _np
    try:
        peak = float(_np.abs(x).max()) if getattr(x, 'size', 0) else 0.0
        if peak > 0.98:
            return (_np.tanh(x / 0.98) * 0.98).astype(_np.float32)
    except Exception:
        pass
    return x


def _typed_kernel32():
    """kernel32 with 64-bit-safe signatures for the handle calls (pseudo-handles are -1 / -2 as 64-bit values)."""
    import ctypes
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = ctypes.c_void_p
    k.GetCurrentThread.restype = ctypes.c_void_p
    k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    k.GetPriorityClass.argtypes = [ctypes.c_void_p]
    k.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
    k.GetThreadPriority.argtypes = [ctypes.c_void_p]
    return k


def ctypes_void_p(v):
    import ctypes
    return ctypes.c_void_p(v)


def _leave_windows_pro_audio_thread(handle):
    if os.name != "nt" or not handle:
        return
    try:
        import ctypes
        ctypes.windll.avrt.AvRevertMmThreadCharacteristics(handle)
    except Exception:
        pass


def _raise_audio_process_priority():
    if os.name != "nt":
        return
    try:
        ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
        k = _typed_kernel32()
        k.SetPriorityClass(ctypes_void_p(k.GetCurrentProcess()), ABOVE_NORMAL_PRIORITY_CLASS)   # v31.30.8: typed handle - the
        # untyped call truncated the 64-bit pseudo-handle and failed silently (error 6): the audio process had run at
        # Normal while the GPU workers ran AboveNormal - the root of the dropouts
    except Exception:
        pass


class AudioFrameFifo:
    """Preallocated stereo FIFO that decouples input and output device clocks.

    A capture endpoint and a physical Audio Out can be driven by different
    hardware clocks. A synchronous read->DSP->write loop eventually
    has to drop/duplicate a block and that is heard as crackle. This FIFO lets the
    capture/DSP side follow the input clock while playback follows the output clock.
    """
    def __init__(self, capacity_frames: int, channels: int):
        import numpy as np
        self.capacity = max(2048, int(capacity_frames))
        self.channels = int(channels)
        self.buf = np.zeros((self.capacity, self.channels), dtype=np.float32)
        self.read_pos = 0
        self.write_pos = 0
        self.count = 0
        self.dropped_frames = 0
        self.lock = threading.Lock()

    def available(self) -> int:
        with self.lock:
            return int(self.count)

    def write(self, x) -> int:
        import numpy as np
        src = np.asarray(x, dtype=np.float32)
        if src.ndim != 2 or src.shape[1] != self.channels:
            src = src.reshape(-1, self.channels)
        n = int(src.shape[0])
        if n <= 0:
            return 0
        if n >= self.capacity:
            src = src[-self.capacity:]
            n = int(src.shape[0])
        with self.lock:
            overflow = max(0, self.count + n - self.capacity)
            if overflow:
                self.read_pos = (self.read_pos + overflow) % self.capacity
                self.count -= overflow
                self.dropped_frames += overflow
            first = min(n, self.capacity - self.write_pos)
            self.buf[self.write_pos:self.write_pos + first] = src[:first]
            remain = n - first
            if remain:
                self.buf[:remain] = src[first:]
            self.write_pos = (self.write_pos + n) % self.capacity
            self.count += n
            return overflow

    def read(self, n: int):
        import numpy as np
        n = int(n)
        if n <= 0:
            return np.zeros((0, self.channels), dtype=np.float32)
        with self.lock:
            if self.count < n:
                return None
            out = np.empty((n, self.channels), dtype=np.float32)
            first = min(n, self.capacity - self.read_pos)
            out[:first] = self.buf[self.read_pos:self.read_pos + first]
            remain = n - first
            if remain:
                out[first:] = self.buf[:remain]
            self.read_pos = (self.read_pos + n) % self.capacity
            self.count -= n
            return out

    def read_partial(self, n: int):
        """Consume up to *n* frames; preserve valid audio on a short queue."""
        import numpy as np
        n = int(n)
        if n <= 0:
            return np.zeros((0, self.channels), dtype=np.float32)
        with self.lock:
            take = min(n, int(self.count))
            if take <= 0:
                return np.zeros((0, self.channels), dtype=np.float32)
            out = np.empty((take, self.channels), dtype=np.float32)
            first = min(take, self.capacity - self.read_pos)
            out[:first] = self.buf[self.read_pos:self.read_pos + first]
            remain = take - first
            if remain:
                out[first:] = self.buf[:remain]
            self.read_pos = (self.read_pos + take) % self.capacity
            self.count -= take
            return out


def _elastic_resample(block, target_frames: int):
    """Tiny linear time correction used only for cross-device clock drift.

    Normally this consumes N frames and returns N frames unchanged. When the FIFO
    slowly drifts, it consumes N-1 or N+1 and interpolates back to N. A one-sample
    correction over a 256-frame block is far less audible than a dropped block.
    """
    import numpy as np
    n = int(block.shape[0])
    target_frames = int(target_frames)
    if n == target_frames:
        return block.astype(np.float32, copy=False)
    if n < 2:
        return np.repeat(block[:1], target_frames, axis=0).astype(np.float32, copy=False)
    pos = np.arange(target_frames, dtype=np.float64) * (float(n) / float(target_frames))
    idx0 = np.floor(pos).astype(np.int64)
    idx0 = np.minimum(idx0, n - 1)
    idx1 = np.minimum(idx0 + 1, n - 1)
    frac = (pos - idx0).astype(np.float32)[:, None]
    return (block[idx0] * (1.0 - frac) + block[idx1] * frac).astype(np.float32, copy=False)


def _elastic_micro_adjust(block, target_frames: int):
    """Localized phase-continuous one/two-frame drift correction.

    Instead of stretching a complete output block, a raised-cosine time warp is
    placed around a low-energy/low-slope region. Before the splice the mapping is
    exactly 1:1; after it the mapping is exactly one frame ahead/behind. Only a
    short ~0.7 ms neighborhood is interpolated, which is substantially less
    audible than whole-block resampling and avoids a hard sample insertion/drop.
    """
    import numpy as np
    x = np.asarray(block, dtype=np.float32)
    target_frames = int(target_frames)
    if x.ndim != 2:
        x = x.reshape(-1, 1)
    if x.shape[0] == target_frames:
        return x.astype(np.float32, copy=False)
    if x.shape[0] < 16 or abs(int(x.shape[0]) - target_frames) > 2:
        return _elastic_resample(x, target_frames)

    y = x
    while y.shape[0] != target_frames:
        n = int(y.shape[0])
        drop = n > target_frames
        out_n = n - 1 if drop else n + 1
        amp = np.mean(np.abs(y), axis=1)
        slope = np.mean(np.abs(np.diff(y, axis=0)), axis=1)
        score = amp[1:-1] + 2.2 * (slope[:-1] + slope[1:])
        margin = min(max(24, n // 10), max(24, n // 3))
        lo = max(1, margin - 1)
        hi = min(score.shape[0], n - margin - 1)
        if hi <= lo:
            center = n // 2
        else:
            center = int(np.argmin(score[lo:hi])) + lo + 1
        W = max(12, min(32, n // 24))
        a = max(2, center - W)
        b = min(out_n - 3, center + W)
        pos = np.arange(out_n, dtype=np.float64)
        shift = np.zeros((out_n,), dtype=np.float64)
        if b > a:
            t = np.linspace(0.0, 1.0, b - a + 1, endpoint=True, dtype=np.float64)
            shift[a:b + 1] = 0.5 - 0.5 * np.cos(np.pi * t)
            shift[b + 1:] = 1.0
        else:
            shift[center:] = 1.0
        pos = pos + shift if drop else pos - shift
        pos = np.clip(pos, 0.0, float(n - 1))
        i0 = np.floor(pos).astype(np.int64)
        i1 = np.minimum(i0 + 1, n - 1)
        frac = (pos - i0).astype(np.float32)[:, None]
        y = (y[i0] * (1.0 - frac) + y[i1] * frac).astype(np.float32, copy=False)
    return y.astype(np.float32, copy=False)


FEATURE_NAMES = (
    "air", "warmth", "brightness", "bass", "clarity",
    "hypnotic", "dreamy", "space", "width", "intimacy",
    "punch", "joy", "depth", "energy", "vintage",
)

# CLAP is excellent at production/timbre concepts and less direct for abstract
# mood concepts that DSP can only influence indirectly. These weights keep the
# adaptive controller assertive where the pretrained critic is trustworthy while
# preventing Joy/Energy from over-driving the chain trying to change composition.
FEATURE_AI_WEIGHTS = {
    "air": 1.00, "warmth": 1.00, "brightness": 1.00, "bass": 1.00,
    "clarity": 1.00, "hypnotic": 0.92, "dreamy": 1.00, "space": 1.00,
    "width": 0.96, "intimacy": 0.92, "punch": 1.00, "joy": 0.58,
    "depth": 1.00, "energy": 0.68, "vintage": 1.00,
}

# v30.2: semantic adaptation remains audible but no longer multiplies manual
# feature moves as aggressively.  The user's slider position is now the dominant
# authority; AI only trims it around the requested direction.
AI_AUTHORITY_MULT = 2.25
AI_EFFECTIVE_SCALE_MIN = 0.30
AI_EFFECTIVE_SCALE_MAX = 4.20

def _effective_ai_scale(raw_scale: float) -> float:
    raw = _clamp(float(raw_scale or 1.0), 0.55, 2.35)
    return _clamp(1.0 + AI_AUTHORITY_MULT * (raw - 1.0), AI_EFFECTIVE_SCALE_MIN, AI_EFFECTIVE_SCALE_MAX)

PARAM_BOUNDS = {
    # EQ / spectral
    "low_cut_hz": (20.0, 180.0), "low_cut_q": (0.50, 1.40),
    "sub_db": (-10.0, 10.0), "sub_freq_hz": (35.0, 95.0),
    "bass_db": (-15.0, 15.0), "bass_freq_hz": (75.0, 210.0),
    "body_db": (-14.0, 14.0), "body_freq_hz": (280.0, 1250.0), "body_q": (0.35, 1.80),
    "mid_db": (-11.0, 11.0), "mid_freq_hz": (700.0, 2600.0), "mid_q": (0.35, 2.20),
    "presence_db": (-12.0, 12.0), "presence_freq_hz": (1800.0, 5200.0), "presence_q": (0.35, 2.20),
    "air_db": (-15.0, 15.0), "air_freq_hz": (5200.0, 12500.0),
    "high_cut_hz": (6000.0, 20000.0), "high_cut_q": (0.50, 1.30),

    # Dynamics
    "compression": (0.0, 1.0), "comp_threshold_db": (-34.0, -6.0), "comp_ratio": (1.2, 8.0),
    "comp_attack_ms": (1.5, 45.0), "comp_release_ms": (45.0, 420.0), "comp_knee_db": (1.0, 14.0),
    "comp_makeup_db": (-2.0, 4.0), "transient_attack": (-1.0, 1.0), "transient_sustain": (-1.0, 1.0),

    # Harmonics
    "drive": (0.0, 1.0), "saturation_mix": (0.10, 0.82), "saturation_asymmetry": (-0.55, 0.55),
    "low_drive": (0.0, 1.0), "mid_drive": (0.0, 1.0), "high_drive": (0.0, 1.0),

    # Stereo / motion
    "width": (0.0, 1.90), "bass_mono_hz": (20.0, 260.0), "side_tilt_db": (-6.0, 6.0),
    "stereo_balance": (-0.45, 0.45), "hypnotic_motion_depth": (0.0, 1.0),
    "hypnotic_rate_hz": (0.05, 0.70), "motion_phase": (0.0, 1.0),

    # Reverb
    "reverb": (0.0, 0.82), "reverb_decay": (0.7, 4.5), "reverb_predelay_ms": (0.0, 115.0),
    "reverb_diffusion": (0.20, 0.95), "reverb_damping": (0.48, 0.84),
    "reverb_tone_hz": (4200.0, 16000.0),

    # Delay
    "delay": (0.0, 0.72), "delay_ms": (70.0, 520.0), "delay_feedback": (0.02, 0.70),

    # Hidden output protection.
    "output_db": (-12.0, 3.0),
}



def _project_musical_params(params: dict[str, float]) -> dict[str, float]:
    """Project independently-moving controls onto a musically coherent safe region.

    The optimizer is allowed to explore all 50 coordinates, but some combinations
    are objectively poor DSP engineering: crossing EQ centers, a shelf above a hard
    low-pass, extreme Q at extreme gain, or simultaneous maximum width + motion.
    This projection keeps the controls independent while preventing those pathological
    intersections from becoming audible harshness or phasey instability.
    """
    p = dict(params)
    # First hard-clamp everything we know.
    for name, (lo, hi) in PARAM_BOUNDS.items():
        if name in p:
            p[name] = _clamp(float(p[name]), lo, hi)

    # Keep the spectral centers ordered relative to the chosen high-cut.  We bias
    # lower centers downward rather than forcing the high-cut upward, so dark/vintage
    # targets remain dark.
    hc = float(p.get("high_cut_hz", DEFAULT_PARAMS["high_cut_hz"]))
    p["air_freq_hz"] = _clamp(min(float(p.get("air_freq_hz", 8000.0)), hc * 0.84), *PARAM_BOUNDS["air_freq_hz"])
    p["presence_freq_hz"] = _clamp(min(float(p.get("presence_freq_hz", 3000.0)), p["air_freq_hz"] * 0.72), *PARAM_BOUNDS["presence_freq_hz"])
    p["mid_freq_hz"] = _clamp(min(float(p.get("mid_freq_hz", 1450.0)), p["presence_freq_hz"] * 0.72), *PARAM_BOUNDS["mid_freq_hz"])
    p["body_freq_hz"] = _clamp(min(float(p.get("body_freq_hz", 850.0)), p["mid_freq_hz"] * 0.74), *PARAM_BOUNDS["body_freq_hz"])
    p["bass_freq_hz"] = _clamp(min(float(p.get("bass_freq_hz", 120.0)), p["body_freq_hz"] * 0.46), *PARAM_BOUNDS["bass_freq_hz"])
    p["sub_freq_hz"] = _clamp(min(float(p.get("sub_freq_hz", 58.0)), p["bass_freq_hz"] * 0.72), *PARAM_BOUNDS["sub_freq_hz"])
    p["low_cut_hz"] = _clamp(min(float(p.get("low_cut_hz", 20.0)), p["sub_freq_hz"] * 0.72), *PARAM_BOUNDS["low_cut_hz"])

    # Studio EQ projection: broad tonal moves, narrow corrective cuts. Large boosts
    # automatically widen so CLAP-driven brightness/presence/body moves do not
    # turn into whistling resonances. Cuts retain more Q for cleanup character.
    for gain_name, q_name in (("body_db","body_q"),("mid_db","mid_q"),("presence_db","presence_q")):
        signed_g = float(p.get(gain_name, 0.0))
        g = abs(signed_g)
        if signed_g > 2.5:
            q_cap = 1.45 - min(0.58, (signed_g - 2.5) * 0.085)
            p[q_name] = min(float(p.get(q_name, 0.82)), max(0.72, q_cap))
        elif signed_g < -5.0:
            p[q_name] = min(float(p.get(q_name, 0.82)), 1.72)

    # Extreme width and deep moving pan at the same time can collapse correlation.
    width = float(p.get("width", 1.0))
    motion = float(p.get("hypnotic_motion_depth", 0.0))
    if width > 1.38 and motion > 0.58:
        p["hypnotic_motion_depth"] = 0.58 + (motion - 0.58) * max(0.18, (1.60 - width) / 0.22)

    # v30.4.22 StudioDSP: delay is intentionally a background texture, never the
    # dominant spatial cue.  Clamp *after* every semantic/internal move so no
    # planner path can push echoes above the studio-safe ceiling.  Space/Dreamy
    # are carried primarily by the orthogonal FDN + width instead.
    DELAY_WET_CEILING = 0.12
    DELAY_FEEDBACK_CEILING = 0.22
    p["delay"] = min(float(p.get("delay", 0.0)), DELAY_WET_CEILING)
    p["delay_feedback"] = min(float(p.get("delay_feedback", 0.08)), DELAY_FEEDBACK_CEILING)

    # v30.4.26 Clean6x: keep the full 6x tonal/spatial render, but stop the
    # nonlinear branch from becoming the audible source of fizz.  At 6x, several
    # perceptual axes can request drive simultaneously and pin independent drive
    # controls at their hard maxima.  Professional processors normally compand
    # the nonlinear authority instead of letting every stage saturate at once.
    # These are *harmonic* ceilings only; EQ, reverb, width and dynamics targets
    # retain the v30.4.25 authority.
    positive_eq = sum(max(0.0, float(p.get(n, 0.0))) for n in ("sub_db","bass_db","body_db","mid_db","presence_db","air_db"))
    bright_eq = max(0.0, float(p.get("air_db", 0.0))) + 0.65 * max(0.0, float(p.get("presence_db", 0.0)))
    clean_drive_cap = _clamp(0.70 - 0.0065 * positive_eq, 0.46, 0.70)
    p["drive"] = min(float(p.get("drive", 0.0)), clean_drive_cap)
    p["low_drive"] = min(float(p.get("low_drive", 0.0)), 0.76)
    p["mid_drive"] = min(float(p.get("mid_drive", 0.0)), min(0.68, clean_drive_cap + 0.04))
    p["high_drive"] = min(float(p.get("high_drive", 0.0)), _clamp(0.48 - 0.010 * bright_eq, 0.24, 0.48))
    p["saturation_mix"] = min(float(p.get("saturation_mix", 0.34)), 0.52)
    # Keep transient enhancement strong, but avoid a maxed transient shaper
    # feeding maxed saturation on the same sample. This does not affect negative
    # attack (softening) requests.
    p["transient_attack"] = min(float(p.get("transient_attack", 0.0)), 0.88)

    # Re-clamp after dependent projections.
    for name, (lo, hi) in PARAM_BOUNDS.items():
        if name in p:
            p[name] = _clamp(float(p[name]), lo, hi)
    return p

DSP50_SPECS = {
    # Exactly 50 AI-owned controls. `step_frac` is for coordinate derivative probes.
    "low_cut_hz": {"label":"Low Cut","group":"EQ","unit":"Hz","step_frac":0.030},
    "low_cut_q": {"label":"Low Cut Q","group":"EQ","unit":"Q","step_frac":0.035},
    "sub_db": {"label":"Sub Gain","group":"EQ","unit":"dB","step_frac":0.040},
    "sub_freq_hz": {"label":"Sub Freq","group":"EQ","unit":"Hz","step_frac":0.035},
    "bass_db": {"label":"Bass Gain","group":"EQ","unit":"dB","step_frac":0.040},
    "bass_freq_hz": {"label":"Bass Freq","group":"EQ","unit":"Hz","step_frac":0.035},
    "body_db": {"label":"Body Gain","group":"EQ","unit":"dB","step_frac":0.040},
    "body_freq_hz": {"label":"Body Freq","group":"EQ","unit":"Hz","step_frac":0.035},
    "body_q": {"label":"Body Q","group":"EQ","unit":"Q","step_frac":0.035},
    "mid_db": {"label":"Mid Gain","group":"EQ","unit":"dB","step_frac":0.040},
    "mid_freq_hz": {"label":"Mid Freq","group":"EQ","unit":"Hz","step_frac":0.035},
    "mid_q": {"label":"Mid Q","group":"EQ","unit":"Q","step_frac":0.035},
    "presence_db": {"label":"Presence","group":"EQ","unit":"dB","step_frac":0.040},
    "presence_freq_hz": {"label":"Presence Freq","group":"EQ","unit":"Hz","step_frac":0.035},
    "presence_q": {"label":"Presence Q","group":"EQ","unit":"Q","step_frac":0.035},
    "air_db": {"label":"Air Gain","group":"EQ","unit":"dB","step_frac":0.040},
    "air_freq_hz": {"label":"Air Freq","group":"EQ","unit":"Hz","step_frac":0.035},
    "high_cut_hz": {"label":"High Cut","group":"EQ","unit":"Hz","step_frac":0.030},
    "high_cut_q": {"label":"High Cut Q","group":"EQ","unit":"Q","step_frac":0.035},

    "compression": {"label":"Compression","group":"DYNAMICS","unit":"%","step_frac":0.045},
    "comp_threshold_db": {"label":"Threshold","group":"DYNAMICS","unit":"dB","step_frac":0.040},
    "comp_ratio": {"label":"Ratio","group":"DYNAMICS","unit":"x","step_frac":0.040},
    "comp_attack_ms": {"label":"Comp Attack","group":"DYNAMICS","unit":"ms","step_frac":0.040},
    "comp_release_ms": {"label":"Comp Release","group":"DYNAMICS","unit":"ms","step_frac":0.040},
    "comp_knee_db": {"label":"Comp Knee","group":"DYNAMICS","unit":"dB","step_frac":0.040},
    "comp_makeup_db": {"label":"Makeup","group":"DYNAMICS","unit":"dB","step_frac":0.040},
    "transient_attack": {"label":"Transient Attack","group":"DYNAMICS","unit":"%","step_frac":0.045},
    "transient_sustain": {"label":"Transient Sustain","group":"DYNAMICS","unit":"%","step_frac":0.045},

    "drive": {"label":"Global Drive","group":"HARMONICS","unit":"%","step_frac":0.045},
    "saturation_mix": {"label":"Sat Mix","group":"HARMONICS","unit":"%","step_frac":0.040},
    "saturation_asymmetry": {"label":"Sat Asymmetry","group":"HARMONICS","unit":"%","step_frac":0.045},
    "low_drive": {"label":"Low Drive","group":"HARMONICS","unit":"%","step_frac":0.045},
    "mid_drive": {"label":"Mid Drive","group":"HARMONICS","unit":"%","step_frac":0.045},
    "high_drive": {"label":"High Drive","group":"HARMONICS","unit":"%","step_frac":0.045},

    "width": {"label":"Stereo Width","group":"STEREO","unit":"x","step_frac":0.040},
    "bass_mono_hz": {"label":"Bass Mono","group":"STEREO","unit":"Hz","step_frac":0.035},
    "side_tilt_db": {"label":"Side Tilt","group":"STEREO","unit":"dB","step_frac":0.040},
    "stereo_balance": {"label":"Balance","group":"STEREO","unit":"%","step_frac":0.045},
    "hypnotic_motion_depth": {"label":"Motion Depth","group":"STEREO","unit":"%","step_frac":0.045},
    "hypnotic_rate_hz": {"label":"Motion Rate","group":"STEREO","unit":"Hz","step_frac":0.040},
    "motion_phase": {"label":"Motion Phase","group":"STEREO","unit":"°","step_frac":0.040},

    "reverb": {"label":"Reverb Wet","group":"REVERB","unit":"%","step_frac":0.040},
    "reverb_decay": {"label":"Decay","group":"REVERB","unit":"s","step_frac":0.040},
    "reverb_predelay_ms": {"label":"Pre-delay","group":"REVERB","unit":"ms","step_frac":0.040},
    "reverb_diffusion": {"label":"Diffusion","group":"REVERB","unit":"%","step_frac":0.040},
    "reverb_damping": {"label":"Damping","group":"REVERB","unit":"%","step_frac":0.040},
    "reverb_tone_hz": {"label":"Reverb Tone","group":"REVERB","unit":"Hz","step_frac":0.035},

    "delay": {"label":"Delay Wet","group":"DELAY","unit":"%","step_frac":0.040},
    "delay_ms": {"label":"Delay Time","group":"DELAY","unit":"ms","step_frac":0.040},
    "delay_feedback": {"label":"Feedback","group":"DELAY","unit":"%","step_frac":0.040},
}
assert len(DSP50_SPECS) == 50

# v30 Auto DSP: topology/timing controls move on a slower timescale than the
# primary audible controls. This prevents a semantic optimizer from producing
# moving resonances, tempo-like delay wandering, or reverb-color zippering.
DSP50_SLOW_NODES = {
    "low_cut_q","sub_freq_hz","bass_freq_hz","body_freq_hz","body_q",
    "mid_freq_hz","mid_q","presence_freq_hz","presence_q","air_freq_hz","high_cut_q",
    "comp_threshold_db","comp_attack_ms","comp_release_ms","comp_knee_db","comp_makeup_db",
    "saturation_asymmetry","bass_mono_hz","stereo_balance","hypnotic_rate_hz","motion_phase",
    "reverb_predelay_ms","reverb_diffusion","reverb_damping","reverb_tone_hz","delay_ms",
}
DSP50_DIRECT_NODES = {
    "low_cut_hz","sub_db","bass_db","body_db","mid_db","presence_db","air_db","high_cut_hz",
    "compression","comp_ratio","transient_attack","transient_sustain","drive","saturation_mix",
    "low_drive","mid_drive","high_drive","width","side_tilt_db","hypnotic_motion_depth",
    "reverb","reverb_decay","delay","delay_feedback",
}
DSP50_COORDS_PER_CYCLE = 1
DSP50_SPSA_DIRECTIONS = 2
DSP50_SPSA_FRAC = 0.018

# v27 Studio DSP: optimizer motion remains deliberately slow, but the *rendered*
# change is expanded around the transparent neutral point. This makes the sound
# much more obvious without making the UI/optimizer twitchy. Frequency, Q and
# timing coordinates are expanded less than gain/wet/drive coordinates because
# rapid or extreme pole/time movement is where zippering and metallic artifacts
# tend to appear.
# v30.4: keep the v30.3 semantic/optimizer dynamics, then multiply only the
# audible render delta by 5x.  This avoids accidentally compounding a 5x request
# through prompt motion, optimizer steps and render authority simultaneously.
STUDIO_DSP_AUTHORITY = float(os.environ.get("JOY_DSP_AUTHORITY", "2.25"))
DJ_DSP_AUTHORITY = float(os.environ.get("JOY_DJ_DSP_AUTHORITY", "1.00"))
V30_4_AUDIBLE_MULT = float(os.environ.get("JOY_DSP_EFFECT_MULT", "5.0"))

# v30.4.45 Fast Control Response.  Keep the same final DSP targets and the
# same jerk-limited/C2 automation topology, but traverse those trajectories
# substantially faster so manual feature changes and Continuous DJ decisions
# become audible sooner.  1.0 reproduces the v30.4.44 response speed.
CONTROL_RESPONSE_SPEED = float(os.environ.get("JOY_CONTROL_RESPONSE_SPEED", "2.35"))
CONTROL_RESPONSE_SPEED = _clamp(CONTROL_RESPONSE_SPEED, 1.0, 3.20)
# v30.4.46: keep v45 fast destination timing, but de-zipper filter coefficient motion.
BIQUAD_MORPH_FRAMES = max(32, int(os.environ.get("JOY_BIQUAD_MORPH_FRAMES", "128")))

# v30.4.1 Continuity Engine.  Keep the x5 render strength unchanged, but decouple
# semantic target motion from the audio-rate parameter motion.  The semantic
# controller may update only a small trust band around the prompt-compiled base
# target; the audio graph then slews that target in two stages.
CONTINUITY_LIVE_BAND_FAST = float(os.environ.get("JOY_CONTINUITY_LIVE_BAND_FAST", "0.042"))
CONTINUITY_LIVE_BAND_SLOW = float(os.environ.get("JOY_CONTINUITY_LIVE_BAND_SLOW", "0.010"))
CONTINUITY_DEADBAND_FAST = float(os.environ.get("JOY_CONTINUITY_DEADBAND_FAST", "0.0028"))
CONTINUITY_DEADBAND_SLOW = float(os.environ.get("JOY_CONTINUITY_DEADBAND_SLOW", "0.0024"))
# v30.4.2: a true capture-side preview buffer.  Auto DSP listens to the newest
# dry input immediately, while the waveform heard at Audio Out is held back by
# ~2 s. This gives semantic decisions and parameter trajectories time to settle
# before the corresponding musical section is rendered, without reconstructing
# or resampling the song.
CONTINUITY_LOOKAHEAD_SEC = float(os.environ.get("JOY_CONTINUITY_LOOKAHEAD_SEC", "2.0"))
CONTINUITY_LOOKAHEAD_SEC = _clamp(CONTINUITY_LOOKAHEAD_SEC, 0.0, 3.0)
# Agentic DJ intentionally trades latency for musical foresight. The future raw
# stream is analyzed before the corresponding delayed waveform reaches the mixer.
AGENTIC_LOOKAHEAD_SEC = float(os.environ.get("JOY_AGENTIC_LOOKAHEAD_SEC", "15.0"))
AGENTIC_LOOKAHEAD_SEC = _clamp(AGENTIC_LOOKAHEAD_SEC, 4.0, 48.0)   # v31.30.16: 4 s floor for the ultra-low weave profile  # v31.28: 16 s window + processing budget
# v30.4.3: time-align semantic decisions with the delayed waveform instead of
# applying a current-audio decision immediately to audio that is ~2 s older.
# The 50D analyser uses a 1.30 s trailing window, so its perceptual target best
# represents roughly the centre of that window.  Start the DSP trajectory a little
# before that centre; the C2 trajectory then arrives musically instead of jumping.
CONTINUITY_ANALYSIS_CENTER_FRAC = float(os.environ.get("JOY_CONTINUITY_ANALYSIS_CENTER_FRAC", "0.50"))
CONTINUITY_PARAM_PREROLL_SEC = float(os.environ.get("JOY_CONTINUITY_PARAM_PREROLL_SEC", "0.46"))
CONTINUITY_MIN_SCHEDULE_AHEAD_SEC = float(os.environ.get("JOY_CONTINUITY_MIN_SCHEDULE_AHEAD_SEC", "0.10"))
CONTINUITY_PARAM_PREROLL_SEC = _clamp(CONTINUITY_PARAM_PREROLL_SEC, 0.15, 0.90)
CONTINUITY_MIN_SCHEDULE_AHEAD_SEC = _clamp(CONTINUITY_MIN_SCHEDULE_AHEAD_SEC, 0.03, 0.35)

# v30.4.4 15D DJ continuity.  The DJ controller can only move the public 15D deck,
# and those moves are timestamped onto the delayed source timeline.  The feature
# deck then slews toward each scheduled target before the native C2 parameter
# smoother, giving two independent continuity barriers without hiding the effect.
DJ_FEATURE_LIMIT = float(os.environ.get("JOY_DJ_FEATURE_LIMIT", "0.68"))
DJ_FEATURE_LIMIT = _clamp(DJ_FEATURE_LIMIT, 0.35, 0.82)
DJ_FEATURE_STEP = float(os.environ.get("JOY_DJ_FEATURE_STEP", "0.105"))
DJ_FEATURE_STEP = _clamp(DJ_FEATURE_STEP, 0.025, 0.12)
DJ_FEATURE_DEADBAND = float(os.environ.get("JOY_DJ_FEATURE_DEADBAND", "0.012"))
# v30.4.7 test: Dreamy + Space should become eligible earlier in Continuous DJ.
# 1.00 is the normal activation threshold. Dreamy uses 0.70 and Space uses 0.28,
# so Space becomes eligible much earlier while Dreamy remains moderately reluctant.
# This changes *when* they engage, not their audible mapping.
DJ_FEATURE_THRESHOLD_SCALE = {name: 1.0 for name in FEATURE_NAMES}
DJ_FEATURE_THRESHOLD_SCALE["dreamy"] = float(os.environ.get("JOY_DJ_DREAMY_THRESHOLD", "0.70"))
DJ_FEATURE_THRESHOLD_SCALE["space"] = float(os.environ.get("JOY_DJ_SPACE_THRESHOLD", "0.28"))
DJ_FEATURE_THRESHOLD_SCALE["dreamy"] = _clamp(DJ_FEATURE_THRESHOLD_SCALE["dreamy"], 0.40, 1.00)
DJ_FEATURE_THRESHOLD_SCALE["space"] = _clamp(DJ_FEATURE_THRESHOLD_SCALE["space"], 0.16, 1.00)
DJ_FEATURE_SLEW_PER_SEC = float(os.environ.get("JOY_DJ_FEATURE_SLEW_PER_SEC", "0.82"))
DJ_FEATURE_SMOOTH_SEC = float(os.environ.get("JOY_DJ_FEATURE_SMOOTH_SEC", "0.58"))
# v30.4.10+: Dreamy + Space keep their dedicated semantic eligibility thresholds, but
# their *movement* is deliberately more reluctant. They need sustained evidence
# to travel far, and the visible/audio-facing deck follows their targets more
# slowly than the other 13 axes. This does not change their DSP mapping.
DJ_RELUCTANT_FEATURES = {"dreamy", "space"}
DJ_RELUCTANT_TARGET_STEP_SCALE = float(os.environ.get("JOY_DJ_RELUCTANT_TARGET_STEP_SCALE", "0.55"))
DJ_RELUCTANT_GRADIENT_NEW_WEIGHT = float(os.environ.get("JOY_DJ_RELUCTANT_GRADIENT_NEW_WEIGHT", "0.16"))
DJ_RELUCTANT_SLEW_PER_SEC = float(os.environ.get("JOY_DJ_RELUCTANT_SLEW_PER_SEC", "0.52"))
DJ_RELUCTANT_SMOOTH_SEC = float(os.environ.get("JOY_DJ_RELUCTANT_SMOOTH_SEC", "0.92"))
DJ_RELUCTANT_TARGET_STEP_SCALE = _clamp(DJ_RELUCTANT_TARGET_STEP_SCALE, 0.25, 1.0)
DJ_RELUCTANT_GRADIENT_NEW_WEIGHT = _clamp(DJ_RELUCTANT_GRADIENT_NEW_WEIGHT, 0.08, 0.24)
DJ_RELUCTANT_SLEW_PER_SEC = _clamp(DJ_RELUCTANT_SLEW_PER_SEC, 0.12, DJ_FEATURE_SLEW_PER_SEC)
DJ_RELUCTANT_SMOOTH_SEC = _clamp(DJ_RELUCTANT_SMOOTH_SEC, DJ_FEATURE_SMOOTH_SEC, 3.0)
DJ_FEATURE_PREROLL_SEC = float(os.environ.get("JOY_DJ_FEATURE_PREROLL_SEC", "0.34"))
_SLOW_AUTHORITY_NAMES = {
    "low_cut_hz","low_cut_q","sub_freq_hz","bass_freq_hz","body_freq_hz","body_q",
    "mid_freq_hz","mid_q","presence_freq_hz","presence_q","air_freq_hz",
    "high_cut_hz","high_cut_q","comp_attack_ms","comp_release_ms","comp_ratio",
    "reverb_decay","reverb_predelay_ms","reverb_diffusion","reverb_damping",
    "reverb_tone_hz","delay_ms","delay_feedback","bass_mono_hz",
    "hypnotic_rate_hz","motion_phase",
}
_GROUP_AUTHORITY = {
    "EQ": 1.00, "DYNAMICS": 1.04, "HARMONICS": 1.20,
    "STEREO": 1.08, "REVERB": 1.22, "DELAY": 1.18,
}

def studio_dsp_render_params(params: dict[str, float] | None, authority: float | None = None, mode: str = "dsp50") -> dict[str, float]:
    """Expand AI-owned DSP coordinates around transparent neutral at render time.

    The optimizer keeps a conservative latent state; only the physical render is
    made more assertive. This decouples *how fast controls move* from *how audible
    the resulting mix change is*. The exact same transform is used by derivative
    probes, so the black-box gradient matches what the user actually hears.
    """
    base = dsp50_safety_params(params)
    a = float(STUDIO_DSP_AUTHORITY if authority is None else authority)
    if str(mode).lower() == "dj" and authority is None:
        a = DJ_DSP_AUTHORITY
    a = _clamp(a, 1.0, 2.60)
    out = dict(base)
    for name, spec in DSP50_SPECS.items():
        if name not in PARAM_BOUNDS:
            continue
        neutral = dsp50_normalized(DEFAULT_PARAMS[name], name)
        cur = dsp50_normalized(base.get(name, DEFAULT_PARAMS[name]), name)
        delta = cur - neutral
        if abs(delta) < 1e-8:
            continue
        group = str(spec.get("group", "EQ"))
        shape = float(_GROUP_AUTHORITY.get(group, 1.0))
        if name in _SLOW_AUTHORITY_NAMES:
            shape *= 0.58
        # Extra curvature only far from neutral; small movements remain precise.
        scale = 1.0 + (a - 1.0) * shape
        # v30.4 x5: multiply the already-shaped v30.3 audible delta exactly once.
        # Hard PARAM_BOUNDS remain the final ceiling, so extreme slider/prompt values
        # saturate musically instead of producing invalid filter/wet/drive settings.
        audible_mult = _clamp(V30_4_AUDIBLE_MULT, 1.0, 5.0)
        boosted = neutral + delta * scale * (1.0 + 0.16 * min(1.0, abs(delta) * 2.0)) * audible_mult
        out[name] = dsp50_from_normalized(_clamp(boosted, 0.0, 1.0), name)

    out = _project_musical_params(out)
    # Stronger DSP needs proactive headroom so the limiter is not the audible
    # "effect". This is reduction-only and therefore cannot win semantic scores
    # merely by turning the track up.
    positive_eq = sum(max(0.0, float(out.get(name, 0.0))) for name in ("sub_db","bass_db","body_db","mid_db","presence_db","air_db"))
    harmonic = max(float(out.get("drive", 0.0)), float(out.get("low_drive", 0.0)), float(out.get("mid_drive", 0.0)), float(out.get("high_drive", 0.0)))
    makeup = max(0.0, float(out.get("comp_makeup_db", 0.0))) * float(out.get("compression", 0.0))
    wet = 0.95 * float(out.get("reverb", 0.0)) + 0.70 * float(out.get("delay", 0.0))
    width_risk = max(0.0, float(out.get("width", 1.0)) - 1.15)
    headroom = min(6.2, 0.14 * positive_eq + 1.35 * harmonic + 0.58 * makeup + 0.92 * wet + 0.55 * width_risk)
    out["output_db"] = min(float(out.get("output_db", 0.0)), -headroom)
    return out


# DJ Gradient Mode measures the prompt derivative of these internal controls in
# the isolated semantic worker.  Each cycle uses central finite differences for
# only a rotating subset, so the realtime audio process never runs CLAP or offline
# render sweeps.  The retained EMA gradients make the full internal state evolve
# continuously between measurements.
DJ_NODE_SPECS = {
    "reverb": {"step_frac": 0.040, "group": "dreamy"},
    "reverb_decay": {"step_frac": 0.045, "group": "dreamy"},
    "reverb_damping": {"step_frac": 0.050, "group": "dreamy"},
    "reverb_tone_hz": {"step_frac": 0.045, "group": "dreamy"},
    "air_db": {"step_frac": 0.040, "group": "dreamy"},
    "high_cut_hz": {"step_frac": 0.040, "group": "dreamy"},
    "width": {"step_frac": 0.045, "group": "shared"},
    "delay": {"step_frac": 0.045, "group": "hypnotic"},
    "delay_ms": {"step_frac": 0.040, "group": "hypnotic"},
    "delay_feedback": {"step_frac": 0.050, "group": "hypnotic"},
    "hypnotic_motion_depth": {"step_frac": 0.050, "group": "hypnotic"},
    "hypnotic_rate_hz": {"step_frac": 0.050, "group": "hypnotic"},
    "presence_db": {"step_frac": 0.040, "group": "shared"},
    "body_db": {"step_frac": 0.040, "group": "shared"},
    "compression": {"step_frac": 0.045, "group": "shared"},
    "drive": {"step_frac": 0.045, "group": "shared"},
    "bass_db": {"step_frac": 0.040, "group": "shared"},
}
DJ_NODES_PER_CYCLE = 2


def feature_params(features: dict[str, float] | None, scales: dict[str, float] | None = None, intensity: float = 1.0) -> dict[str, float]:
    """Map the 15 perceptual axes to the realtime DSP graph.

    Frontend feature values arrive already shaped to roughly [-1, 1]. AI scales
    only their magnitude; it never flips the user's requested direction.
    """
    f = {name: float((features or {}).get(name, 0.0) or 0.0) for name in FEATURE_NAMES}
    scs = scales or {}
    # v30.4.25: 2x stronger than v30.4.24, ~6x the v30.4.23 public 15D render.
    # Semantic targets/thresholds are unchanged; only the physical render
    # authority is multiplied.  Delay remains independently studio-capped in
    # _project_musical_params so the low-delay character is preserved.
    FEATURE_RENDER_BOOST = 19.50
    G = FEATURE_RENDER_BOOST * _clamp(float(intensity), 0.0, 1.60)
    effective_scales = {name: _effective_ai_scale(float(scs.get(name, 1.0) or 1.0)) for name in FEATURE_NAMES}

    def sc(name: str) -> float:
        value = f[name] * effective_scales[name] * G
        return _clamp(value, -19.50, 19.50)

    a, w, br, ba, c = (sc(x) for x in ("air", "warmth", "brightness", "bass", "clarity"))
    h, dr, sp, wd, inti = (sc(x) for x in ("hypnotic", "dreamy", "space", "width", "intimacy"))
    pu, j, d, en, v = (sc(x) for x in ("punch", "joy", "depth", "energy", "vintage"))
    p = dict(DEFAULT_PARAMS)

    p["air_db"] += 8.1*a; p["presence_db"] += 1.9*a; p["high_cut_hz"] += 8800*a if a < 0 else 220*a
    p["bass_db"] += 5.25*w; p["body_db"] += 4.55*w; p["air_db"] -= 2.55*w; p["presence_db"] -= .8*w; p["drive"] += .24*max(0,w); p["high_cut_hz"] -= 2350*max(0,w)
    p["air_db"] += 6.05*br; p["presence_db"] += 3.0*br; p["body_db"] -= .9*br; p["high_cut_hz"] += 7200*br if br < 0 else 210*br
    p["bass_db"] += 7.45*ba; p["body_db"] += 1.75*ba; p["low_cut_hz"] += 68*max(0,-ba)-7*max(0,ba); p["output_db"] -= .42*max(0,ba)
    p["presence_db"] += 5.15*c; p["air_db"] += 2.65*c; p["body_db"] -= 1.9*c; p["bass_db"] -= .65*c; p["low_cut_hz"] += 27*max(0,c); p["high_cut_hz"] += 4350*c if c < 0 else 130*c

    p["body_db"] += 1.55*h; p["presence_db"] -= 1.45*h; p["width"] += .46*h; p["reverb"] += .39*h; p["reverb_decay"] += 2.15*h; p["reverb_damping"] += .085*max(0,h); p["reverb_tone_hz"] -= 1600*max(0,h); p["delay"] += .07*h; p["delay_ms"] += 165*h; p["delay_feedback"] += .035*max(0,h); p["hypnotic_motion_depth"] += .82*max(0,h); p["hypnotic_rate_hz"] += .16*max(0,h); p["high_cut_hz"] -= 650*max(0,h)
    p["air_db"] += 1.55*dr; p["presence_db"] -= 2.05*dr; p["body_db"] += .75*dr; p["width"] += .37*dr; p["reverb"] += .43*dr; p["reverb_decay"] += 2.35*dr; p["reverb_damping"] += .11*max(0,dr); p["reverb_tone_hz"] -= 3100*max(0,dr); p["delay"] += .018*dr; p["delay_feedback"] += .010*max(0,dr); p["high_cut_hz"] -= 1550*max(0,dr)
    p["reverb"] += .50*sp; p["reverb_decay"] += 2.70*sp; p["reverb_damping"] += .045*max(0,sp); p["width"] += .43*sp; p["delay"] += .015*max(0,sp); p["delay_ms"] += 92*sp; p["delay_feedback"] += .010*max(0,sp)
    p["width"] += .48*wd; p["reverb"] += .045*max(0,wd)
    p["presence_db"] += 2.35*inti; p["body_db"] += 1.25*inti; p["width"] -= .27*inti; p["reverb"] -= .31*inti; p["reverb_decay"] -= 1.25*inti; p["delay"] -= .10*inti; p["air_db"] += .45*inti

    p["bass_db"] += 3.05*pu; p["presence_db"] += 4.15*pu; p["compression"] += .59*pu; p["drive"] += .18*max(0,pu); p["reverb"] -= .065*max(0,pu); p["output_db"] -= .48*max(0,pu)
    p["air_db"] += 3.55*j; p["presence_db"] += 2.45*j; p["bass_db"] += 1.45*j; p["body_db"] += .75*j; p["width"] += .27*j; p["reverb"] += .085*j; p["compression"] += .16*j
    p["bass_db"] += 5.25*d; p["body_db"] += 3.65*d; p["presence_db"] -= .7*d; p["low_cut_hz"] += 78*max(0,-d)-6*max(0,d)
    p["compression"] += .45*en; p["presence_db"] += 2.85*en; p["air_db"] += 1.8*en; p["bass_db"] += 1.65*en; p["drive"] += .18*max(0,en); p["width"] += .12*en; p["output_db"] -= .34*max(0,en)
    p["air_db"] -= 4.05*v; p["presence_db"] -= .95*v; p["body_db"] += 2.35*v; p["drive"] += .33*max(0,v); p["high_cut_hz"] -= 6100*max(0,v); p["high_cut_hz"] += 180*max(0,-v); p["width"] -= .12*max(0,v); p["reverb"] += .055*max(0,v)

    # Stronger semantic corrections can legitimately produce much larger EQ/wet
    # moves. Reserve a little transparent headroom *before* the look-ahead limiter
    # so the stronger AI character does not turn into constant limiter pumping.
    active_excess = max((abs(f[name]) * max(0.0, effective_scales[name] - 1.0) for name in FEATURE_NAMES), default=0.0)
    p["output_db"] -= min(3.0, 0.55 * active_excess)

    return _project_musical_params(p)


def apply_dj_node_offsets(params: dict[str, float], offsets: dict[str, float] | None) -> dict[str, float]:
    out = dict(params)
    for name, off in (offsets or {}).items():
        if name not in PARAM_BOUNDS:
            continue
        lo, hi = PARAM_BOUNDS[name]
        out[name] = _clamp(float(out.get(name, DEFAULT_PARAMS.get(name, 0.0))) + float(off or 0.0), lo, hi)
    return _project_musical_params(out)



def dsp50_safety_params(params: dict[str, float] | None) -> dict[str, float]:
    """Clamp the 50D controller to musical bounds and reserve transparent headroom.

    `output_db` is deliberately outside the AI search space.  The optimizer can
    explore timbre and dynamics, but cannot win by simply turning the song up.
    """
    out = dict(DEFAULT_PARAMS)
    for name, value in (params or {}).items():
        if name in out:
            try:
                out[name] = float(value)
            except (TypeError, ValueError):
                pass
    out = _project_musical_params(out)

    positive_eq = sum(max(0.0, float(out.get(name, 0.0))) for name in ("sub_db","bass_db","body_db","mid_db","presence_db","air_db"))
    harmonic = max(float(out.get("drive", 0.0)), float(out.get("low_drive", 0.0)), float(out.get("mid_drive", 0.0)), float(out.get("high_drive", 0.0)))
    makeup = max(0.0, float(out.get("comp_makeup_db", 0.0))) * float(out.get("compression", 0.0))
    wet = 0.8 * float(out.get("reverb", 0.0)) + 0.55 * float(out.get("delay", 0.0))
    headroom = min(4.8, 0.12 * positive_eq + 1.15 * harmonic + 0.55 * makeup + 0.75 * wet)
    out["output_db"] = -headroom
    return out


def dsp50_normalized(value: float, name: str) -> float:
    lo, hi = PARAM_BOUNDS[name]
    return _clamp((float(value) - lo) / max(1e-9, hi - lo), 0.0, 1.0)


def dsp50_from_normalized(value: float, name: str) -> float:
    lo, hi = PARAM_BOUNDS[name]
    return lo + _clamp(float(value), 0.0, 1.0) * (hi - lo)

def feature_param_jacobian(features: dict[str, float], node_names: list[str] | tuple[str, ...], eps: float = 0.035) -> dict[str, dict[str, float]]:
    """Normalized d(DSP node)/d(feature) used by DJ Gradient chain rule.

    Node values are normalized by their full safe DSP span, so a 1.0 derivative
    means a one-unit feature move would move that node through its whole musical
    range. This keeps Hz, dB, wet percentages and LFO rates comparable.
    """
    base = {name: float(features.get(name, 0.0) or 0.0) for name in FEATURE_NAMES}
    out: dict[str, dict[str, float]] = {name: {} for name in FEATURE_NAMES}
    neutral_scales = {name: 1.0 for name in FEATURE_NAMES}
    for feat in FEATURE_NAMES:
        plus = dict(base); minus = dict(base)
        plus[feat] = _clamp(plus[feat] + eps, -1.25, 1.25)
        minus[feat] = _clamp(minus[feat] - eps, -1.25, 1.25)
        denom = max(1e-6, plus[feat] - minus[feat])
        p_plus = feature_params(plus, neutral_scales)
        p_minus = feature_params(minus, neutral_scales)
        for node in node_names:
            if node not in PARAM_BOUNDS:
                continue
            lo, hi = PARAM_BOUNDS[node]
            span = max(1e-9, hi - lo)
            out[feat][node] = ((float(p_plus[node]) - float(p_minus[node])) / span) / denom
    return out


class AudioRing:
    """Small lock-protected mono ring used only by the out-of-band AI critic."""
    def __init__(self, sample_rate: int, seconds: float = 5.0):
        import numpy as np
        self.capacity = max(1024, int(sample_rate * seconds))
        self.buf = np.zeros((self.capacity,), dtype=np.float32)
        self.pos = 0
        self.filled = 0
        self.lock = threading.Lock()

    def append(self, block):
        import numpy as np
        if block.ndim == 2:
            mono = block[:, 0] if block.shape[1] == 1 else np.mean(block[:, :2], axis=1)
        else:
            mono = block
        mono = np.asarray(mono, dtype=np.float32).reshape(-1)
        n = int(mono.size)
        if n <= 0:
            return
        if n >= self.capacity:
            mono = mono[-self.capacity:]
            n = mono.size
        # AI history is best-effort. Never let a long snapshot copy block the
        # realtime producer; dropping one 10 ms analysis block is inaudible to the
        # controller and much safer than stalling WASAPI.
        if not self.lock.acquire(False):
            return
        try:
            end = self.pos + n
            if end <= self.capacity:
                self.buf[self.pos:end] = mono
            else:
                first = self.capacity - self.pos
                self.buf[self.pos:] = mono[:first]
                self.buf[:n-first] = mono[first:]
            self.pos = (self.pos + n) % self.capacity
            self.filled = min(self.capacity, self.filled + n)
        finally:
            self.lock.release()

    def snapshot(self, n_samples: int):
        import numpy as np
        with self.lock:
            n = min(max(0, int(n_samples)), self.filled)
            if n <= 0:
                return np.zeros((0,), dtype=np.float32)
            start = (self.pos - n) % self.capacity
            if start < self.pos or self.filled < self.capacity:
                return self.buf[start:start+n].copy()
            return np.concatenate((self.buf[start:], self.buf[:self.pos])).astype(np.float32, copy=False)


class StereoAudioRing:
    """Short stereo ring used only by DJ Gradient analysis.

    The legacy semantic critic intentionally stores mono, but derivative control of
    width and stereo motion needs the original side channel. This ring is never
    touched by the model process directly; it only provides a copied analysis
    window to the out-of-band request.
    """
    def __init__(self, sample_rate: int, seconds: float = 3.0, channels: int = 2):
        import numpy as np
        self.capacity = max(1024, int(sample_rate * seconds))
        self.channels = max(1, int(channels))
        self.buf = np.zeros((self.capacity, self.channels), dtype=np.float32)
        self.pos = 0
        self.filled = 0
        self.total = 0            # absolute frames ever appended (v31.14 TimeWeave anchor)
        self.lock = threading.Lock()

    def append(self, block):
        import numpy as np
        x = np.asarray(block, dtype=np.float32)
        if x.ndim == 1:
            x = x[:, None]
        if x.shape[1] < self.channels:
            x = np.repeat(x[:, :1], self.channels, axis=1)
        else:
            x = x[:, :self.channels]
        n = int(x.shape[0])
        if n <= 0:
            return
        if n >= self.capacity:
            x = x[-self.capacity:]; n = int(x.shape[0])
        # v31.18: blocking acquire - the snapshot side holds the lock for ~0.3 ms at most,
        # and a dropped block would break the frame-accurate grid anchor (ring.total).
        self.lock.acquire()
        try:
            end = self.pos + n
            if end <= self.capacity:
                self.buf[self.pos:end] = x
            else:
                first = self.capacity - self.pos
                self.buf[self.pos:] = x[:first]
                self.buf[:n-first] = x[first:]
            self.pos = (self.pos + n) % self.capacity
            self.filled = min(self.capacity, self.filled + n)
            self.total += n
        finally:
            self.lock.release()

    def read_at(self, back_frames: int, n_frames: int):
        """v31.14 TimeWeave: non-blocking random-access read of n frames starting
        back_frames behind the write head.  Returns None on lock contention or
        when the region is not resident — callers keep the live program for that
        block, so worst case is one block of un-woven audio, never a stall."""
        import numpy as np
        n = max(0, int(n_frames))
        if n <= 0:
            return None
        if not self.lock.acquire(False):
            return None
        try:
            back = int(back_frames)
            if back < n or back > self.filled:
                return None
            start = (self.pos - back) % self.capacity
            if start + n <= self.capacity:
                return self.buf[start:start+n].copy()
            first = self.capacity - start
            return np.concatenate((self.buf[start:], self.buf[:n-first]), axis=0).astype(np.float32, copy=True)
        finally:
            self.lock.release()

    def snapshot(self, n_frames: int):
        import numpy as np
        with self.lock:
            n = min(max(0, int(n_frames)), self.filled)
            if n <= 0:
                return np.zeros((0, self.channels), dtype=np.float32)
            start = (self.pos - n) % self.capacity
            if start < self.pos or self.filled < self.capacity:
                return self.buf[start:start+n].copy()
            return np.concatenate((self.buf[start:], self.buf[:self.pos]), axis=0).astype(np.float32, copy=False)

    def snapshot_indexed(self, n_frames: int):
        """v31.18: newest n frames AND the absolute frame index of their end, read under one
        lock so the beat-phase measurement can be expressed in render frames (no wall clock)."""
        import numpy as np
        with self.lock:
            n = min(max(0, int(n_frames)), self.filled)
            total = int(self.total)
            if n <= 0:
                return np.zeros((0, self.channels), dtype=np.float32), total
            start = (self.pos - n) % self.capacity
            if start < self.pos or self.filled < self.capacity:
                return self.buf[start:start+n].copy(), total
            return np.concatenate((self.buf[start:], self.buf[:self.pos]), axis=0).astype(np.float32, copy=True), total


class SingleSampleDeClicker:
    """Vectorized, conservative isolated-sample repair for hard digital ticks."""

    def __init__(self, channels: int):
        import numpy as np
        self.channels = int(channels)
        self.prev = np.zeros((self.channels,), dtype=np.float32)
        self.repairs = 0

    def process(self, x):
        import numpy as np
        if x.size == 0 or x.shape[0] < 2:
            if x.size:
                self.prev = x[-1].astype(np.float32, copy=True)
            return x
        src = x.astype(np.float32, copy=False)
        prev = np.vstack((self.prev[None, :], src[:-1]))
        nxt = np.vstack((src[1:], src[-1:]))
        mid = 0.5 * (prev + nxt)
        spike = np.max(np.abs(src - mid), axis=1)
        neighbor_gap = np.max(np.abs(prev - nxt), axis=1)
        local = np.maximum(np.maximum(np.max(np.abs(prev), axis=1), np.max(np.abs(nxt), axis=1)), 0.05)
        # Only touch unmistakable one-sample digital impulses. Musical attacks can
        # be steep, so the detector requires both a very large interpolation error
        # and close agreement between the two neighboring samples.
        threshold = np.maximum(0.62, local * 3.10)
        mask = (spike > threshold) & (neighbor_gap < np.maximum(0.10, local * 0.34))
        # Last sample has no real right-hand neighbor; never alter it.
        mask[-1] = False
        if not np.any(mask):
            self.prev = src[-1].astype(np.float32, copy=True)
            return src
        strength = np.clip((spike - threshold) / np.maximum(0.18, 1.20 - threshold), 0.0, 0.72).astype(np.float32)
        y = src.copy()
        a = strength[mask, None]
        y[mask] = src[mask] * (1.0 - a) + mid[mask] * a
        self.repairs += int(np.count_nonzero(mask))
        self.prev = y[-1].astype(np.float32, copy=True)
        return y


class MasterSafetyCompressor:
    """Transparent post-DSP bus glue that keeps the limiter out of the audible path.

    v30.4.46 is intentionally *not* a loudness compressor.  It uses a broad soft
    knee, modest ratio and a faster downward envelope to shave DSP-created peaks
    by roughly 0..2.8 dB before the true-peak limiter.  Release remains slow enough
    to avoid buzz/pumping on bass and reverb tails.
    """

    def __init__(self, sample_rate: int, channels: int):
        self.sample_rate = float(sample_rate)
        self.channels = int(channels)
        self.gain = 1.0
        self.last_reduction_db = 0.0

    def process(self, x):
        import numpy as np
        if x.size == 0:
            return x
        n = int(x.shape[0])
        block_s = n / max(1.0, self.sample_rate)
        rms = float(np.sqrt(np.mean(x * x) + 1e-12))
        peak = float(np.max(np.abs(x)))
        crest = peak / max(rms, 1e-7)
        crest_n = _clamp((crest - 2.0) / 5.0, 0.0, 1.0)

        # Peak-weighted detector: mastered material gets only a touch of glue;
        # transient DSP overs are caught before the final limiter has to clamp them.
        detector = max(rms * 1.38, peak * (0.80 + 0.05 * crest_n), 1e-9)
        level_db = 20.0 * math.log10(detector)
        threshold = -4.0
        ratio = 1.48 + 0.12 * crest_n
        knee = 5.0
        lower = threshold - knee * 0.5
        upper = threshold + knee * 0.5
        if level_db <= lower:
            red = 0.0
        elif level_db >= upper:
            red = (level_db - threshold) * (1.0 - 1.0 / ratio)
        else:
            z = level_db - lower
            red = (1.0 - 1.0 / ratio) * (z * z) / (2.0 * knee)
        red = min(2.8, max(0.0, red))
        target = 10.0 ** (-red / 20.0)

        # 5..8 ms attack is fast enough to pre-condition the lookahead limiter but
        # not so fast that the compressor itself rectifies high-frequency content.
        tau_att = 0.0055 + 0.0025 * crest_n
        tau_rel = 0.260 + 0.080 * crest_n
        tau = tau_att if target < self.gain else tau_rel
        a = 1.0 - math.exp(-block_s / tau)
        end_gain = self.gain + (target - self.gain) * a
        gains = np.linspace(self.gain, end_gain, n, endpoint=True, dtype=np.float32)
        self.gain = float(end_gain)
        min_g = float(np.min(gains)) if gains.size else 1.0
        self.last_reduction_db = 20.0 * math.log10(max(min_g, 1e-9))
        return (x * gains[:, None]).astype(np.float32, copy=False)


class SourceFidelityGuard:
    """Dry-anchored residual guard for high-quality semantic DSP.

    JoyMetric never reconstructs or vocodes the live song.  All processing is
    rendered as a residual around the original full-rate waveform.  This guard
    only intervenes when the combined DSP residual becomes implausibly large,
    phase-destructive, overly rough, or transient-flattening for the requested
    effect strength.  It therefore preserves the source detail while still
    allowing obvious reverb, width, EQ and harmonic changes.
    """

    def __init__(self, sample_rate: int, channels: int):
        self.sample_rate = float(sample_rate)
        self.channels = int(channels)
        self.residual_gain = 1.0
        self.last_residual_gain = 1.0
        self.last_quality_score = 1.0
        self.prev_ref = None
        self.prev_candidate = None

    @staticmethod
    def _rms(x) -> float:
        import numpy as np
        return float(np.sqrt(np.mean(x * x) + 1e-12))

    def process(self, candidate, reference, p: dict[str, float]):
        import numpy as np
        y = np.nan_to_num(np.asarray(candidate, dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0)
        ref = np.asarray(reference, dtype=np.float32)
        if y.shape != ref.shape or y.size == 0:
            return y
        residual = y - ref
        dry_rms = self._rms(ref)
        res_rms = self._rms(residual)
        if dry_rms < 2e-5 or res_rms < 1e-7:
            self.residual_gain += (1.0 - self.residual_gain) * 0.04
            self.last_residual_gain = self.residual_gain
            self.last_quality_score = 1.0
            return y

        eq_strength = max(
            abs(float(p.get("sub_db", 0.0))), abs(float(p.get("bass_db", 0.0))),
            abs(float(p.get("body_db", 0.0))), abs(float(p.get("mid_db", 0.0))),
            abs(float(p.get("presence_db", 0.0))), abs(float(p.get("air_db", 0.0))),
        ) / 8.0
        width_strength = abs(float(p.get("width", 1.0)) - 1.0) / 0.60
        spatial = max(float(p.get("reverb", 0.0)) / 0.75, float(p.get("delay", 0.0)) / 0.60,
                      float(p.get("hypnotic_motion_depth", 0.0)), width_strength)
        harmonic = max(float(p.get("drive", 0.0)), float(p.get("saturation_mix", 0.0)))
        dynamics = max(float(p.get("compression", 0.0)), abs(float(p.get("transient_attack", 0.0))),
                       abs(float(p.get("transient_sustain", 0.0))))
        strength = _clamp(max(eq_strength, 0.92 * spatial, 0.86 * harmonic, 0.72 * dynamics), 0.0, 1.25)

        # Residual energy budget grows with intentional effect strength.  At strong
        # settings this still permits a residual larger than the dry RMS; it merely
        # prevents a pathological DSP combination from replacing the source.
        allowed_ratio = 0.38 + 1.42 * min(1.0, strength)
        target = min(1.0, allowed_ratio * dry_rms / max(res_rms, 1e-8))

        # Correlation guard: when the prompt is not asking for strong space/width,
        # a severe polarity/phase change is treated as reconstruction damage.
        den = self._rms(ref) * self._rms(y) + 1e-12
        corr = float(np.mean(ref * y) / den)
        if corr < 0.16 and spatial < 0.48:
            target *= _clamp((corr + 0.30) / 0.46, 0.38, 1.0)

        # First-difference energy is a cheap wideband roughness/alias proxy.  Air
        # and saturation are allowed to add some high-frequency detail, but not an
        # order-of-magnitude digital fizz component.
        prev_ref = ref[:1] if self.prev_ref is None else self.prev_ref[None, :]
        prev_y = y[:1] if self.prev_candidate is None else self.prev_candidate[None, :]
        dref = np.diff(np.vstack((prev_ref, ref)), axis=0)
        dy = np.diff(np.vstack((prev_y, y)), axis=0)
        rough_ref = self._rms(dref)
        rough_y = self._rms(dy)
        brightness_intent = _clamp(max(0.0, float(p.get("air_db", 0.0)) / 8.0) + 0.55 * harmonic, 0.0, 1.4)
        rough_allowed = max(rough_ref * (1.28 + 1.35 * brightness_intent), 0.012)
        if rough_y > rough_allowed:
            target *= _clamp((rough_allowed / max(rough_y, 1e-8)) ** 0.55, 0.42, 1.0)

        # If compression/transient shaping was not requested, protect source crest
        # factor so percussion and vocal consonants do not take on a smeared,
        # reconstructed texture.
        if float(p.get("compression", 0.0)) < 0.34 and abs(float(p.get("transient_attack", 0.0))) < 0.38:
            dry_crest = float(np.max(np.abs(ref))) / max(dry_rms, 1e-7)
            wet_rms = self._rms(y)
            wet_crest = float(np.max(np.abs(y))) / max(wet_rms, 1e-7)
            if dry_crest > 2.2 and wet_crest < 0.62 * dry_crest:
                target *= 0.86

        target = _clamp(target, 0.30, 1.0)
        n = int(y.shape[0]); block_s = n / max(1.0, self.sample_rate)
        old_gain = float(self.residual_gain)
        tau = 0.090 if target < old_gain else 0.620
        a = 1.0 - math.exp(-block_s / tau)
        new_gain = _clamp(old_gain + (target - old_gain) * a, 0.30, 1.0)
        # v30.4.3: apply fidelity protection as a sample-continuous residual ramp.
        # The previous block-scalar gain could create a tiny full-mix level step
        # even though all semantic DSP parameters themselves were smooth.
        gain_ramp = np.linspace(old_gain, new_gain, n, endpoint=True, dtype=np.float32)[:, None]
        self.residual_gain = float(new_gain)
        self.prev_ref = ref[-1].copy()
        self.prev_candidate = y[-1].copy()
        self.last_residual_gain = float(self.residual_gain)
        self.last_quality_score = float(_clamp(0.68 * self.residual_gain + 0.32 * max(0.0, corr), 0.0, 1.0))
        return (ref + residual * gain_ramp).astype(np.float32, copy=False)


class TransparentAutoGainStage:
    """DSP-relative auto gain that prevents loudness build-up from sounding like clipping.

    The stage only turns down; it never adds makeup gain. It compares the processed
    block with the original input block so strong EQ/saturation/reverb can remain
    obvious while accidental level build-up is removed before the final compressor
    and limiter. Reduction attacks quickly and releases slowly to avoid pumping.
    """

    def __init__(self, sample_rate: int, max_peak_lift_db: float = 2.15, max_rms_lift_db: float = 2.45):
        self.sample_rate = float(sample_rate)
        self.max_peak_lift = 10.0 ** (float(max_peak_lift_db) / 20.0)
        self.max_rms_lift = 10.0 ** (float(max_rms_lift_db) / 20.0)
        self.gain = 1.0
        self.last_reduction_db = 0.0

    def process(self, x, reference):
        import numpy as np
        if x.size == 0:
            return x
        n = int(x.shape[0])
        block_s = n / max(1.0, self.sample_rate)
        ref = reference.astype(np.float32, copy=False)
        src = x.astype(np.float32, copy=False)

        ref_peak = float(np.max(np.abs(ref))) if ref.size else 0.0
        src_peak = float(np.max(np.abs(src)))
        ref_rms = float(np.sqrt(np.mean(ref * ref) + 1e-12)) if ref.size else 0.0
        src_rms = float(np.sqrt(np.mean(src * src) + 1e-12))

        # Relative limits preserve already-mastered sources while removing the
        # extra level created by DSP. The absolute limit is intentionally loose;
        # the true-peak limiter remains the final safety net.
        allowed_peak = max(0.74, ref_peak * self.max_peak_lift)
        allowed_rms = max(0.055, ref_rms * self.max_rms_lift)
        target = 1.0
        if src_peak > allowed_peak + 1e-9:
            target = min(target, allowed_peak / src_peak)
        if src_rms > allowed_rms + 1e-9:
            target = min(target, allowed_rms / src_rms)
        target = float(_clamp(target, 0.38, 1.0))

        # v30.4.3: never hard-step gain at a block boundary. The final true-peak
        # lookahead limiter already protects the first transient samples, so this
        # stage can remain mathematically continuous and use a fast downward ramp.
        start_gain = float(self.gain)
        tau = 0.018 if target < start_gain else 0.880
        a = 1.0 - math.exp(-block_s / tau)
        end_gain = start_gain + (target - start_gain) * a
        gains = np.linspace(start_gain, end_gain, n, endpoint=True, dtype=np.float32)
        self.gain = float(end_gain)
        min_g = float(np.min(gains)) if gains.size else 1.0
        self.last_reduction_db = 20.0 * math.log10(max(min_g, 1e-9))
        return (src * gains[:, None]).astype(np.float32, copy=False)


class LookaheadPeakLimiter:
    """Stereo-linked look-ahead peak limiter for transparent clip protection.

    The old v23 limiter changed one gain value for an entire 64..256 frame block.
    That was safe, but a transient at the end of a block could pull the beginning of
    the same block down and make the output feel grainy/pumpy.  This limiter delays
    the signal by ~1.5 ms, looks ahead sample-by-sample, attacks before a transient,
    and releases smoothly.  It never hard-clips the waveform.
    """

    def __init__(self, sample_rate: int, channels: int, ceiling_db: float = -1.6, release_ms: float = 175.0, lookahead_ms: float = 2.5):
        import numpy as np

        self.sample_rate = float(sample_rate)
        self.channels = int(channels)
        self.ceiling_db = float(ceiling_db)
        self.ceiling = 10.0 ** (self.ceiling_db / 20.0)
        self.release_ms = float(release_ms)
        self.lookahead = max(8, int(round(self.sample_rate * float(lookahead_ms) / 1000.0)))
        self.pending = np.zeros((self.lookahead, self.channels), dtype=np.float32)
        self.gain = 1.0
        self.last_reduction_db = 0.0

    @staticmethod
    def _true_peak_frames(signal):
        """Vectorized dense cubic inter-sample true-peak estimate.

        Linear interpolation cannot reveal inter-sample overs, so we estimate
        quarter-sample positions with Catmull-Rom interpolation and
        use those values only for the limiter detector. The audio itself is never
        resampled in the realtime path.
        """
        import numpy as np
        x = signal.astype(np.float32, copy=False)
        m = int(x.shape[0])
        if m < 4:
            return np.max(np.abs(x), axis=1)
        pad = np.vstack((x[:1], x, x[-1:], x[-1:]))
        p0 = pad[:-3]
        p1 = pad[1:-2]
        p2 = pad[2:-1]
        p3 = pad[3:]
        interval_peak = np.maximum(np.max(np.abs(p1), axis=1), np.max(np.abs(p2), axis=1))
        for t in (0.250, 0.500, 0.750):
            t2 = t * t
            t3 = t2 * t
            q = 0.5 * ((2.0 * p1) + (-p0 + p2) * t + (2.0*p0 - 5.0*p1 + 4.0*p2 - p3) * t2 + (-p0 + 3.0*p1 - 3.0*p2 + p3) * t3)
            interval_peak = np.maximum(interval_peak, np.max(np.abs(q), axis=1))
        frame_peak = np.empty((m,), dtype=np.float32)
        frame_peak[:-1] = interval_peak[:m-1]
        frame_peak[-1] = float(np.max(np.abs(x[-1])))
        return frame_peak

    def process(self, x):
        import numpy as np

        if x.size == 0:
            return x
        n = int(x.shape[0])
        combined = np.concatenate((self.pending, x.astype(np.float32, copy=False)), axis=0)
        delayed = combined[:n].copy()
        self.pending = combined[n:].copy()

        frame_peak = self._true_peak_frames(combined)
        windows = np.lib.stride_tricks.sliding_window_view(frame_peak, self.lookahead + 1)
        future_peak = np.max(windows, axis=1)[:n]
        peak_distance = np.argmax(windows, axis=1)[:n].astype(np.int32, copy=False)
        needed = np.minimum(1.0, self.ceiling / np.maximum(future_peak, 1e-12))

        release_coeff = math.exp(-1.0 / max(1.0, self.sample_rate * (self.release_ms / 1000.0)))
        gains = np.empty((n,), dtype=np.float32)
        g = float(self.gain)
        min_g = 1.0
        for i in range(n):
            need = float(needed[i])
            if need < g:
                # Use the known future-peak distance to pre-ramp gain instead of
                # stepping it in one sample when a kick enters the lookahead.
                # This is much cleaner on a large 50..100 Hz waveform.
                d = int(peak_distance[i])
                if d <= 0 or g <= 1e-9:
                    g = need
                else:
                    ratio = max(1e-9, need / g)
                    g = max(need, g * (ratio ** (1.0 / float(d))))
            else:
                g = 1.0 - (1.0 - g) * release_coeff
                if g > need:
                    g = need
            gains[i] = g
            min_g = min(min_g, g)
        self.gain = g
        self.last_reduction_db = 20.0 * math.log10(max(min_g, 1e-9))
        return (delayed * gains[:, None]).astype(np.float32, copy=False)


class Biquad:
    """Small stereo RBJ biquad with persistent state and de-zippered coefficient morphing.

    v30.4.46 keeps the v45 fast semantic/control trajectory, but no longer swaps a
    complete RBJ coefficient set at a 512-frame boundary.  `set_coeffs()` stores a
    stable target; `process()` walks the current coefficients to that target in
    short sub-blocks (64 frames by default).  For RBJ biquads the denominator
    stability region is convex, so linear interpolation between two stable sets is
    safe, and the much smaller state perturbations materially reduce zipper/click
    artifacts from fast EQ/width automation.
    """

    def __init__(self, channels: int = 2):
        self.channels = channels
        self.z1 = [0.0] * channels
        self.z2 = [0.0] * channels
        self.b0, self.b1, self.b2, self.a1, self.a2 = 1.0, 0.0, 0.0, 0.0, 0.0
        self.tb0, self.tb1, self.tb2, self.ta1, self.ta2 = 1.0, 0.0, 0.0, 0.0, 0.0
        self._coeffs_initialized = False
        try:
            from scipy.signal import lfilter as _lf
            self._lfilter = _lf
        except Exception:
            self._lfilter = None

    def reset(self):
        self.z1 = [0.0] * self.channels
        self.z2 = [0.0] * self.channels

    def set_coeffs(self, kind: str, sample_rate: float, freq: float, q: float = 0.707, gain_db: float = 0.0):
        fs = max(8000.0, float(sample_rate))
        f = _clamp(freq, 10.0, fs * 0.48)
        w0 = 2.0 * math.pi * f / fs
        cw, sw = math.cos(w0), math.sin(w0)
        A = 10.0 ** (gain_db / 40.0)
        alpha = sw / (2.0 * max(0.05, q))

        if kind == "highpass":
            b0 = (1 + cw) / 2
            b1 = -(1 + cw)
            b2 = (1 + cw) / 2
            a0 = 1 + alpha
            a1 = -2 * cw
            a2 = 1 - alpha
        elif kind == "lowpass":
            b0 = (1 - cw) / 2
            b1 = 1 - cw
            b2 = (1 - cw) / 2
            a0 = 1 + alpha
            a1 = -2 * cw
            a2 = 1 - alpha
        elif kind == "peaking":
            b0 = 1 + alpha * A
            b1 = -2 * cw
            b2 = 1 - alpha * A
            a0 = 1 + alpha / A
            a1 = -2 * cw
            a2 = 1 - alpha / A
        elif kind in {"lowshelf", "highshelf"}:
            sqrtA = math.sqrt(A)
            shelf_alpha = sw / 2.0 * math.sqrt(2.0)
            beta = 2.0 * sqrtA * shelf_alpha
            if kind == "lowshelf":
                b0 = A * ((A + 1) - (A - 1) * cw + beta)
                b1 = 2 * A * ((A - 1) - (A + 1) * cw)
                b2 = A * ((A + 1) - (A - 1) * cw - beta)
                a0 = (A + 1) + (A - 1) * cw + beta
                a1 = -2 * ((A - 1) + (A + 1) * cw)
                a2 = (A + 1) + (A - 1) * cw - beta
            else:
                b0 = A * ((A + 1) + (A - 1) * cw + beta)
                b1 = -2 * A * ((A - 1) + (A + 1) * cw)
                b2 = A * ((A + 1) + (A - 1) * cw - beta)
                a0 = (A + 1) - (A - 1) * cw + beta
                a1 = 2 * ((A - 1) - (A + 1) * cw)
                a2 = (A + 1) - (A - 1) * cw - beta
        else:
            b0, b1, b2, a0, a1, a2 = 1.0, 0.0, 0.0, 1.0, 0.0, 0.0

        inv = 1.0 / a0
        target = (b0 * inv, b1 * inv, b2 * inv, a1 * inv, a2 * inv)
        self.tb0, self.tb1, self.tb2, self.ta1, self.ta2 = target
        if not self._coeffs_initialized:
            self.b0, self.b1, self.b2, self.a1, self.a2 = target
            self._coeffs_initialized = True

    def _process_fixed(self, x):
        import numpy as np
        y = np.empty_like(x)
        b0, b1, b2, a1, a2 = self.b0, self.b1, self.b2, self.a1, self.a2
        if self._lfilter is not None:
            b = np.asarray([b0, b1, b2], dtype=np.float64)
            a = np.asarray([1.0, a1, a2], dtype=np.float64)
            for ch in range(x.shape[1]):
                yi, zf = self._lfilter(b, a, x[:, ch], zi=np.asarray([self.z1[ch], self.z2[ch]], dtype=np.float64))
                y[:, ch] = yi.astype(x.dtype, copy=False)
                self.z1[ch], self.z2[ch] = float(zf[0]), float(zf[1])
            return y
        for ch in range(x.shape[1]):
            z1, z2 = self.z1[ch], self.z2[ch]
            src = x[:, ch]; dst = y[:, ch]
            for i in range(src.shape[0]):
                v = float(src[i])
                out = b0 * v + z1
                z1 = b1 * v - a1 * out + z2
                z2 = b2 * v - a2 * out
                dst[i] = out
            self.z1[ch], self.z2[ch] = z1, z2
        return y

    def process(self, x):
        import numpy as np
        if x.size == 0:
            return x
        target = np.asarray([self.tb0, self.tb1, self.tb2, self.ta1, self.ta2], dtype=np.float64)
        current = np.asarray([self.b0, self.b1, self.b2, self.a1, self.a2], dtype=np.float64)
        if float(np.max(np.abs(target - current))) < 2e-9:
            return self._process_fixed(x)

        n = int(x.shape[0])
        morph = max(16, int(BIQUAD_MORPH_FRAMES))
        pieces = []
        start_coeff = current.copy()
        for a in range(0, n, morph):
            b = min(n, a + morph)
            frac = float(b) / float(max(1, n))
            c = start_coeff + (target - start_coeff) * frac
            self.b0, self.b1, self.b2, self.a1, self.a2 = map(float, c)
            pieces.append(self._process_fixed(x[a:b]))
        self.b0, self.b1, self.b2, self.a1, self.a2 = map(float, target)
        return np.concatenate(pieces, axis=0).astype(x.dtype, copy=False)


class DelayBuffer:
    def __init__(self, sample_rate: int, channels: int, max_seconds: float = 0.75):
        import numpy as np

        self.sample_rate = int(sample_rate)
        self.channels = channels
        self.buf = np.zeros((max(16, int(sample_rate * max_seconds)), channels), dtype=np.float32)
        self.pos = 0
        self.current_delay_samples = max(1.0, sample_rate * 0.285)
        self.feedback_state = [0.0] * channels
        self.wet_smooth = 0.0
        try:
            from scipy.signal import lfilter as _lf
            self._lfilter = _lf
        except Exception:
            self._lfilter = None

    def process(self, x, delay_ms: float, wet: float, feedback: float):
        import numpy as np

        wet = _clamp(wet, 0.0, 0.8)
        # Continuity: never stop advancing the delay line just because its return
        # is currently inaudible.  A later wet increase must reveal a naturally
        # evolved tail, not a freshly-reset buffer edge.
        n = int(x.shape[0])
        nbuf = len(self.buf)
        target = _clamp(delay_ms / 1000.0 * self.sample_rate, n + 2.0, nbuf - 2.0)
        # Block-rate smoothing is sufficient because the parent DSP already smooths
        # delay_ms over ~140 ms. This keeps the fractional delay click-free without
        # a Python per-sample loop.
        alpha = 1.0 - math.exp(-(n / self.sample_rate) / 0.028)
        self.current_delay_samples += (target - self.current_delay_samples) * alpha
        d = float(self.current_delay_samples)
        d0 = int(math.floor(d))
        frac = d - d0
        base = np.arange(n, dtype=np.int64) + int(self.pos)
        read0 = (base - d0) % nbuf
        read1 = (base - (d0 + 1)) % nbuf
        delayed = self.buf[read0] * (1.0 - frac) + self.buf[read1] * frac
        old_wet = float(self.wet_smooth)
        block_s = n / max(1.0, float(self.sample_rate))
        tau_wet = 0.15 if wet > old_wet else 0.55
        a_wet = 1.0 - math.exp(-block_s / tau_wet)
        new_wet = old_wet + (wet - old_wet) * a_wet
        wet_ramp = np.linspace(old_wet, new_wet, n, endpoint=True, dtype=np.float32)[:, None]
        self.wet_smooth = float(new_wet)
        out = delayed * wet_ramp
        write = base % nbuf
        fb = _clamp(feedback, 0.0, 0.72)

        # A gently bandwidth-limited feedback path sounds far less brittle than
        # recirculating full-band samples. The cutoff falls a little as feedback
        # rises, which also keeps high-feedback optimizer moves stable.
        fc = 11200.0 - 3900.0 * fb
        pole = math.exp(-2.0 * math.pi * _clamp(fc, 5200.0, 12500.0) / float(self.sample_rate))
        filtered = np.empty_like(delayed)
        if self._lfilter is not None:
            for ch in range(self.channels):
                yi, zf = self._lfilter([1.0 - pole], [1.0, -pole], delayed[:, ch], zi=[float(self.feedback_state[ch])])
                filtered[:, ch] = yi.astype(np.float32, copy=False)
                self.feedback_state[ch] = float(zf[0])
        else:
            filtered[:] = delayed
        self.buf[write] = x + filtered * fb
        self.pos = int((self.pos + n) % nbuf)
        return out.astype(np.float32, copy=False)



class MultiTapReverb:
    """Eight-line feedback-delay-network reverb tuned for smooth music use.

    v27 replaces the older independent-comb return with a normalized 8x8
    Hadamard FDN.  Decay maps to a real RT60-style per-line feedback gain,
    diffusion changes the feedback matrix density rather than just wet level,
    and the return is decorrelated without random modulation.  All delay lines
    are longer than the realtime DSP quantum, so the block implementation is
    deterministic and does not create within-block feedback discontinuities.
    """

    def __init__(self, sample_rate: int, channels: int):
        import numpy as np
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        # Mutually incommensurate delays reduce metallic ringing. Keep the
        # shortest safely above the preferred 512-frame DSP block at 48 kHz.
        times = (0.0311, 0.0367, 0.0419, 0.0473, 0.0539, 0.0617, 0.0701, 0.0793)
        self.lengths = [max(1536, int(round(sample_rate * t))) for t in times]
        self.buffers = [np.zeros((n, channels), dtype=np.float32) for n in self.lengths]
        self.positions = [0] * 8
        self.damp_states = [np.zeros((channels,), dtype=np.float64) for _ in range(8)]

        # Sylvester Hadamard, energy normalized. This is orthogonal, so the
        # feedback network stays well behaved while still building dense tails.
        H = np.asarray([
            [1,1,1,1,1,1,1,1],
            [1,-1,1,-1,1,-1,1,-1],
            [1,1,-1,-1,1,1,-1,-1],
            [1,-1,-1,1,1,-1,-1,1],
            [1,1,1,1,-1,-1,-1,-1],
            [1,-1,1,-1,-1,1,-1,1],
            [1,1,-1,-1,-1,-1,1,1],
            [1,-1,-1,1,-1,1,1,-1],
        ], dtype=np.float32) / math.sqrt(8.0)
        self.hadamard = H
        self.in_sign = np.asarray((1,-1,1,1,-1,1,-1,-1), dtype=np.float32)
        self.out_sign_l = np.asarray((1,1,-1,1,-1,-1,1,-1), dtype=np.float32) / math.sqrt(8.0)
        self.out_sign_r = np.asarray((1,-1,1,-1,-1,1,1,-1), dtype=np.float32) / math.sqrt(8.0)

        self.wet_hp = Biquad(channels)
        self.wet_lp = Biquad(channels)
        self.wet_hp.set_coeffs("highpass", sample_rate, 95.0, 0.707, 0.0)
        self.wet_lp.set_coeffs("lowpass", sample_rate, min(11800.0, sample_rate * 0.235), 0.72, 0.0)
        self.pre_buf = np.zeros((max(2048, int(sample_rate * 0.16)), channels), dtype=np.float32)
        self.pre_pos = 0
        self.wet_gain_smooth = 0.0
        try:
            from scipy.signal import lfilter as _lf
            self._lfilter = _lf
        except Exception:
            self._lfilter = None
        # Four short Schroeder all-pass stages turn sparse transients into a dense
        # excitation before the long FDN. States are persistent across blocks.
        self.diffuser_delays = [max(8, int(round(sample_rate * t))) for t in (0.0047, 0.0063, 0.0089, 0.0127)]
        self.diffuser_states = [
            [np.zeros((d,), dtype=np.float64) for _ in range(channels)]
            for d in self.diffuser_delays
        ]

    def _diffuse_input(self, x, amount: float):
        import numpy as np
        if self._lfilter is None:
            return x
        y = x.astype(np.float32, copy=True)
        # Keep all-pass feedback fixed for stability; the user diffusion parameter
        # controls how much of the dense path enters the FDN.
        g = 0.52
        for stage, d in enumerate(self.diffuser_delays):
            b = np.zeros((d + 1,), dtype=np.float64); a = np.zeros((d + 1,), dtype=np.float64)
            b[0] = -g; b[d] = 1.0
            a[0] = 1.0; a[d] = -g
            nxt = np.empty_like(y)
            for ch in range(self.channels):
                yi, zf = self._lfilter(b, a, y[:, ch], zi=self.diffuser_states[stage][ch])
                nxt[:, ch] = yi.astype(np.float32, copy=False)
                self.diffuser_states[stage][ch] = zf.astype(np.float64, copy=False)
            y = nxt
        mix = _clamp((float(amount) - 0.20) / 0.75, 0.0, 1.0)
        # Equal-power blend avoids a dip as diffusion is increased.
        theta = 0.5 * math.pi * mix
        return (x * math.cos(theta) + y * math.sin(theta)).astype(np.float32, copy=False)

    def _predelay(self, x, predelay_ms: float):
        import numpy as np
        ds = int(round(_clamp(float(predelay_ms), 0.0, 115.0) * self.sample_rate / 1000.0))
        n = int(x.shape[0]); size = int(self.pre_buf.shape[0]); ds = min(max(1, ds), size - 2)
        if ds <= 1:
            # Keep the pre-delay memory advancing even when its audible delay is
            # effectively zero. A later increase therefore reveals history rather
            # than a cold buffer edge.
            base = np.arange(n, dtype=np.int64) + int(self.pre_pos)
            self.pre_buf[base % size] = x
            self.pre_pos = int((self.pre_pos + n) % size)
            return x
        out = np.empty_like(x)
        if ds >= n + 1:
            base = np.arange(n, dtype=np.int64) + int(self.pre_pos)
            out[:] = self.pre_buf[(base - ds) % size]
            self.pre_buf[base % size] = x
            self.pre_pos = int((self.pre_pos + n) % size)
            return out
        pos = int(self.pre_pos)
        for i in range(n):
            read = (pos - ds) % size
            out[i] = self.pre_buf[read]
            self.pre_buf[pos] = x[i]
            pos = (pos + 1) % size
        self.pre_pos = pos
        return out

    def process(
        self, x, wet: float, decay: float, damping: float = 0.62,
        tone_hz: float = 11800.0, predelay_ms: float = 0.0, diffusion: float = 0.62,
    ):
        import numpy as np
        wet = _clamp(wet, 0.0, 0.88)
        # Continuity: keep every FDN/all-pass/predelay state warm at zero wet.
        # Output gain can be zero while the internal acoustic state continues.
        decay = _clamp(decay, 0.7, 4.5)
        diffusion = _clamp(diffusion, 0.20, 0.95)
        damp = _clamp(float(damping), 0.48, 0.84)
        tone = _clamp(float(tone_hz), 4200.0, min(16000.0, self.sample_rate * 0.45))
        source = self._predelay(x, predelay_ms)
        source = self._diffuse_input(source, diffusion)
        n = int(source.shape[0])

        # Read each line before writing. Each line is longer than one DSP block,
        # therefore all reads represent already committed past audio.
        delayed_lines = []
        indices = []
        for k, buf in enumerate(self.buffers):
            pos = int(self.positions[k]); nbuf = len(buf)
            idx = (np.arange(n, dtype=np.int64) + pos) % nbuf
            indices.append(idx)
            delayed = buf[idx].copy()
            if self._lfilter is not None:
                smooth = np.empty_like(delayed)
                # Damping is a one-pole low-pass inside the loop. Higher damping
                # gives a darker, more stable tail without changing RT60 directly.
                pole = _clamp(0.38 + 0.58 * damp, 0.55, 0.92)
                for ch in range(self.channels):
                    yi, zf = self._lfilter([1.0 - pole], [1.0, -pole], delayed[:, ch], zi=[float(self.damp_states[k][ch])])
                    smooth[:, ch] = yi.astype(np.float32, copy=False)
                    self.damp_states[k][ch] = float(zf[0])
            else:
                smooth = delayed
            delayed_lines.append(smooth)

        stack = np.stack(delayed_lines, axis=1)  # [frames, 8, channels]
        mixed = np.einsum('ij,njc->nic', self.hadamard, stack, optimize=True)
        # Diffusion crossfades between direct line feedback and the orthogonal mix.
        feedback_field = stack * (1.0 - diffusion) + mixed * diffusion

        # RT60 mapping: after `decay` seconds each line has decayed by ~60 dB.
        for k, buf in enumerate(self.buffers):
            delay_sec = float(self.lengths[k]) / float(self.sample_rate)
            gain = 10.0 ** (-3.0 * delay_sec / max(0.25, decay))
            gain = _clamp(gain, 0.36, 0.965)
            inject = source * (0.145 + 0.045 * diffusion) * float(self.in_sign[k])
            # Alternate channel polarity for extra decorrelation while preserving
            # mono compatibility in the summed return.
            if self.channels >= 2 and (k & 1):
                inject = inject.copy(); inject[:, 1] *= -1.0
            buf[indices[k]] = inject + feedback_field[:, k, :] * gain
            self.positions[k] = int((self.positions[k] + n) % len(buf))

        if self.channels >= 2:
            left = np.einsum('nlc,l->nc', stack, self.out_sign_l, optimize=True)
            right = np.einsum('nlc,l->nc', stack, self.out_sign_r, optimize=True)
            # Use opposite decorrelation projections but retain each channel's
            # native energy. The small crossfeed avoids a hollow hard-panned tail.
            acc = np.empty_like(source)
            acc[:, 0] = 0.82 * left[:, 0] + 0.18 * right[:, 1]
            acc[:, 1] = 0.82 * right[:, 1] + 0.18 * left[:, 0]
        else:
            acc = np.mean(stack, axis=1)

        self.wet_hp.set_coeffs("highpass", self.sample_rate, 92.0, 0.707, 0.0)
        self.wet_lp.set_coeffs("lowpass", self.sample_rate, tone, 0.72, 0.0)
        acc = self.wet_hp.process(acc)
        acc = self.wet_lp.process(acc)
        # Slightly concave wet mapping gives useful low settings and prevents the
        # top of the range from becoming a washed-out limiter trigger.
        target_wet_gain = 0.92 * math.sin(0.5 * math.pi * min(1.0, wet))
        old_wet_gain = float(self.wet_gain_smooth)
        block_s = n / max(1.0, float(self.sample_rate))
        tau = 0.10 if target_wet_gain > old_wet_gain else 0.36
        a = 1.0 - math.exp(-block_s / tau)
        new_wet_gain = old_wet_gain + (target_wet_gain - old_wet_gain) * a
        wet_ramp = np.linspace(old_wet_gain, new_wet_gain, n, endpoint=True, dtype=np.float32)[:, None]
        self.wet_gain_smooth = float(new_wet_gain)
        return (acc * wet_ramp).astype(np.float32, copy=False)


class NativeStudioCore:
    """C++-backed studio dynamics and time-effects core via Spotify Pedalboard.

    The previous all-Python graph was musically capable, but its compressor and
    algorithmic time effects still did significant Python/Numpy work in the same
    process that services WASAPI.  Pedalboard executes the heavy plugin kernels in
    native code and releases the GIL, giving the audio scheduler much more margin.

    The core is deliberately parallel: compression, modulation, delay and reverb
    are blended with the dry signal under loudness-aware control.  This makes the
    effect unmistakable without relying on clipping or limiter gain reduction.
    """

    def __init__(self, sample_rate: int, channels: int):
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.available = False
        self.error = ""
        self.wet_gain = 1.0
        self.core_rms_ema = 1e-3
        self.wet_rms_ema = 1e-4
        # v30.4.3: time-FX and parallel dynamics gains are themselves persistent
        # automation states.  v30.4.2 kept the plugin delay/reverb state warm, but
        # the dry/wet weights were still one scalar per block.  Ramping these gains
        # sample-wise removes the last small block-edge amplitude discontinuities.
        self.comp_mix_smooth = 0.0
        self.comp_makeup_gain_smooth = 1.0
        self.chorus_weight_smooth = 0.0
        self.delay_weight_smooth = 0.0
        self.reverb_weight_smooth = 0.0
        self.direct_gain_smooth = 1.0
        try:
            from pedalboard import Compressor, Reverb, Delay, Chorus, LowpassFilter, HighpassFilter
            self.comp = Compressor(threshold_db=-18.0, ratio=2.0, attack_ms=8.0, release_ms=160.0)
            self.predelay = Delay(delay_seconds=0.001, feedback=0.0, mix=1.0)
            self.reverb = Reverb(room_size=0.55, damping=0.55, wet_level=1.0, dry_level=0.0, width=1.0)
            self.rev_hp = HighpassFilter(cutoff_frequency_hz=92.0)
            self.rev_lp = LowpassFilter(cutoff_frequency_hz=min(11800.0, self.sample_rate * 0.45))
            self.delay = Delay(delay_seconds=0.285, feedback=0.08, mix=1.0)
            self.delay_hp = HighpassFilter(cutoff_frequency_hz=72.0)
            self.delay_lp = LowpassFilter(cutoff_frequency_hz=min(13200.0, self.sample_rate * 0.45))
            self.chorus = Chorus(rate_hz=0.18, depth=0.10, centre_delay_ms=8.0, feedback=0.04, mix=1.0)
            self.delay_seconds_smooth = 0.285
            self.delay_feedback_smooth = 0.08
            self.predelay_seconds_smooth = 0.001
            self.rev_room_smooth = 0.55
            self.rev_damp_smooth = 0.55
            self.rev_tone_smooth = min(11800.0, self.sample_rate * 0.45)
            self.chorus_rate_smooth = 0.18
            self.chorus_depth_smooth = 0.10
            # v30.4.21 StudioDSP: use JoyMetric's orthogonal 8-line RT60 FDN as
            # the primary late reverb. Pedalboard reverb remains allocated/warm
            # for compatibility, but is not the audible late-field generator.
            self.studio_fdn = MultiTapReverb(sample_rate, channels)
            self.available = True
        except Exception as exc:
            self.error = str(exc)

    def _run(self, plugin, x):
        import numpy as np
        z = np.ascontiguousarray(x.T, dtype=np.float32)
        y = plugin(z, self.sample_rate, reset=False)
        y = np.asarray(y, dtype=np.float32)
        if y.ndim == 1:
            y = y[None, :]
        return np.ascontiguousarray(y.T, dtype=np.float32)

    def process_compressor(self, x, p: dict[str, float]):
        import numpy as np
        c = _clamp(p.get("compression", 0.0), 0.0, 1.0)
        if not self.available:
            return x
        # Keep the native compressor detector/envelope warm even at transparent
        # compression.  Returning early at c==0 made a later compressor fade-in
        # reveal a cold envelope state.
        self.comp.threshold_db = _clamp(p.get("comp_threshold_db", -18.0), -34.0, -6.0)
        requested_ratio = _clamp(p.get("comp_ratio", 3.0), 1.2, 8.0)
        self.comp.ratio = _clamp(1.0 + (requested_ratio - 1.0) * (0.35 + 0.65 * c), 1.0, 8.0)
        # Program-dependent ballistics: transient-rich material receives a little
        # more attack time and a longer release, while dense material can recover
        # faster. This reduces kick/vocal pumping without flattening punch.
        rms = float(np.sqrt(np.mean(x * x) + 1e-12))
        peak = float(np.max(np.abs(x)))
        crest = peak / max(rms, 1e-7)
        crest_n = _clamp((crest - 2.0) / 5.0, 0.0, 1.0)
        base_attack = _clamp(p.get("comp_attack_ms", 7.5), 1.5, 45.0)
        base_release = _clamp(p.get("comp_release_ms", 165.0), 45.0, 420.0)
        self.comp.attack_ms = _clamp(base_attack * (0.92 + 0.48 * crest_n), 1.5, 52.0)
        self.comp.release_ms = _clamp(base_release * (0.88 + 0.34 * crest_n), 50.0, 520.0)
        wet = self._run(self.comp, x)

        n = max(1, int(x.shape[0])); block_s = n / max(1.0, float(self.sample_rate))
        onset = _clamp(c / 0.030, 0.0, 1.0)
        target_mix = _clamp((0.12 + 0.74 * c) * onset, 0.0, 0.88)
        old_mix = float(self.comp_mix_smooth)
        tau_mix = 0.12 if target_mix > old_mix else 0.42
        a_mix = 1.0 - math.exp(-block_s / tau_mix)
        new_mix = old_mix + (target_mix - old_mix) * a_mix
        mix_ramp = np.linspace(old_mix, new_mix, n, endpoint=True, dtype=np.float32)[:, None]
        self.comp_mix_smooth = float(new_mix)
        y = x * (1.0 - mix_ramp) + wet * mix_ramp

        makeup_db = _clamp(p.get("comp_makeup_db", 0.0), -2.0, 4.0) * c
        target_makeup = 10.0 ** (makeup_db / 20.0)
        old_makeup = float(self.comp_makeup_gain_smooth)
        a_make = 1.0 - math.exp(-block_s / 0.24)
        new_makeup = old_makeup + (target_makeup - old_makeup) * a_make
        makeup_ramp = np.linspace(old_makeup, new_makeup, n, endpoint=True, dtype=np.float32)[:, None]
        self.comp_makeup_gain_smooth = float(new_makeup)
        y *= makeup_ramp
        return y.astype(np.float32, copy=False)

    def process_time_fx(self, x, p: dict[str, float], hypnotic: float = 0.0, clean_source=None):
        import numpy as np
        if not self.available:
            return None
        rev_wet = _clamp(p.get("reverb", 0.0), 0.0, 0.75)
        delay_wet = _clamp(p.get("delay", 0.0), 0.0, 0.12)
        motion = _clamp(max(float(hypnotic or 0.0), float(p.get("hypnotic_motion_depth", 0.0) or 0.0)), 0.0, 1.0)
        n = max(1, int(x.shape[0])); block_s = n / max(1.0, float(self.sample_rate))

        # Time effects are fed mainly from a cleaner pre-saturation source.  This
        # keeps reverb/delay tails detailed instead of repeatedly exciting any
        # nonlinear high-frequency residue.  A small amount of the colored core is
        # retained so the effect still belongs to the processed mix.
        if clean_source is not None and getattr(clean_source, "shape", None) == x.shape:
            send_source = (0.80 * clean_source + 0.20 * x).astype(np.float32, copy=False)
        else:
            send_source = x

        wet_parts = []
        wet_targets = []
        wet_attrs = []
        wet_taus = []

        # Pedalboard supports parameter automation, but smaller changes are much
        # smoother.  Add a second, native-core-local slew so plugin parameters do
        # not jump at block boundaries even when 50D targets move.
        a_motion = 1.0 - math.exp(-block_s / 0.20)
        target_rate = _clamp(p.get("hypnotic_rate_hz", 0.18), 0.05, 0.70)
        target_depth = _clamp(0.055 + 0.48 * motion, 0.04, 0.58)
        self.chorus_rate_smooth += (target_rate - self.chorus_rate_smooth) * a_motion
        self.chorus_depth_smooth += (target_depth - self.chorus_depth_smooth) * a_motion
        self.chorus.rate_hz = self.chorus_rate_smooth
        self.chorus.depth = self.chorus_depth_smooth
        self.chorus.centre_delay_ms = _clamp(5.5 + 8.5 * motion, 5.0, 16.0)
        self.chorus.feedback = _clamp(0.015 + 0.10 * motion, 0.0, 0.18)
        self.chorus.mix = 1.0
        wet_parts.append(self._run(self.chorus, x))
        wet_targets.append((0.10 + 0.34 * motion) * motion)
        wet_attrs.append("chorus_weight_smooth")
        wet_taus.append((0.10, 0.28))

        # Delay-time modulation is a classic source of clicks/pitch tearing.  Limit
        # its physical slew to roughly 55 ms/s; wet amount can still change faster.
        target_delay = _clamp(float(p.get("delay_ms", 285.0)) / 1000.0, 0.070, 0.520)
        max_delta = max(0.0006, 0.055 * block_s)
        self.delay_seconds_smooth += _clamp(target_delay - self.delay_seconds_smooth, -max_delta, max_delta)
        a_fb = 1.0 - math.exp(-block_s / 0.18)
        target_fb = _clamp(p.get("delay_feedback", 0.08), 0.02, 0.22)
        self.delay_feedback_smooth += (target_fb - self.delay_feedback_smooth) * a_fb
        self.delay.delay_seconds = self.delay_seconds_smooth
        self.delay.feedback = self.delay_feedback_smooth
        self.delay.mix = 1.0
        dl = self._run(self.delay, send_source)
        self.delay_hp.cutoff_frequency_hz = 72.0
        self.delay_lp.cutoff_frequency_hz = _clamp(11200.0 - 3600.0 * self.delay_feedback_smooth, 6800.0, min(11200.0, self.sample_rate * 0.45))
        dl = self._run(self.delay_hp, dl)
        dl = self._run(self.delay_lp, dl)
        wet_parts.append(dl)
        wet_targets.append((0.02 + 0.55 * delay_wet) * delay_wet)
        wet_attrs.append("delay_weight_smooth")
        wet_taus.append((0.09, 0.34))

        # StudioDSP v30.4.21: orthogonal 8-line FDN late field. The FDN owns
        # predelay, diffusion, damping and RT60 mapping, so these parameters change
        # acoustic structure rather than merely crossfading a generic room macro.
        decay = _clamp(p.get("reverb_decay", 1.8), 0.7, 4.5)
        diffusion = _clamp(p.get("reverb_diffusion", 0.62), 0.20, 0.95)
        damp = _clamp(p.get("reverb_damping", 0.62), 0.48, 0.84)
        tone = _clamp(p.get("reverb_tone_hz", 11800.0), 4200.0, min(16000.0, self.sample_rate * 0.45))
        pred_ms = _clamp(p.get("reverb_predelay_ms", 0.0), 0.0, 115.0)
        rv = self.studio_fdn.process(
            send_source, min(0.88, rev_wet * 1.08), decay, damp, tone, pred_ms, diffusion
        )
        wet_parts.append(rv)
        wet_targets.append((0.055 + 1.04 * rev_wet) * rev_wet)
        wet_attrs.append("reverb_weight_smooth")
        wet_taus.append((0.12, 0.44))

        wet = np.zeros_like(x, dtype=np.float32)
        for part, target_weight, attr, taus in zip(wet_parts, wet_targets, wet_attrs, wet_taus):
            old_weight = float(getattr(self, attr))
            tau_up, tau_down = taus
            tau = tau_up if target_weight > old_weight else tau_down
            a_weight = 1.0 - math.exp(-block_s / tau)
            new_weight = old_weight + (float(target_weight) - old_weight) * a_weight
            weight_ramp = np.linspace(old_weight, new_weight, n, endpoint=True, dtype=np.float32)[:, None]
            setattr(self, attr, float(new_weight))
            wet += part * weight_ramp

        core_rms = float(np.sqrt(np.mean(x * x) + 1e-12))
        wet_rms = float(np.sqrt(np.mean(wet * wet) + 1e-12))
        self.core_rms_ema = 0.965 * self.core_rms_ema + 0.035 * core_rms
        self.wet_rms_ema = 0.965 * self.wet_rms_ema + 0.035 * wet_rms
        intensity = _clamp(0.84 * rev_wet + 0.24 * delay_wet + 0.33 * motion, 0.0, 1.0)
        desired_ratio = 0.82 * intensity
        target_wet_gain = _clamp(desired_ratio * self.core_rms_ema / max(self.wet_rms_ema, 1e-7), 0.28, 2.20)
        old_wet_gain = float(self.wet_gain)
        tau_wg = 0.09 if target_wet_gain < old_wet_gain else 0.18
        a_wg = 1.0 - math.exp(-block_s / tau_wg)
        new_wet_gain = old_wet_gain + (target_wet_gain - old_wet_gain) * a_wg
        wet_gain_ramp = np.linspace(old_wet_gain, new_wet_gain, n, endpoint=True, dtype=np.float32)[:, None]
        self.wet_gain = float(new_wet_gain)
        wet *= wet_gain_ramp

        # Preserve at least ~58% direct amplitude even at extreme spatial settings.
        # The target formula is unchanged from v30.4.2; only its application is
        # sample-ramped so x5 spatial automation cannot create block-edge gain kinks.
        target_dry_gain = math.sqrt(max(0.34, 1.0 - 0.70 * rev_wet - 0.12 * delay_wet - 0.18 * motion))
        old_dry_gain = float(self.direct_gain_smooth)
        tau_dry = 0.10 if target_dry_gain < old_dry_gain else 0.24
        a_dry = 1.0 - math.exp(-block_s / tau_dry)
        new_dry_gain = old_dry_gain + (target_dry_gain - old_dry_gain) * a_dry
        dry_gain_ramp = np.linspace(old_dry_gain, new_dry_gain, n, endpoint=True, dtype=np.float32)[:, None]
        self.direct_gain_smooth = float(new_dry_gain)
        return (x * dry_gain_ramp + wet).astype(np.float32, copy=False)



class ProfessionalFXRack:
    """Realtime-safe DJ effect rack inspired by professional controller routing.

    This is intentionally native to JoyMetric rather than embedding a second DJ
    application.  It provides a tempo-synchronised SEND bus (filtered ping-pong
    beat echo), a low-end-safe rhythmic gate, stereo-width control on the wet bus,
    feedback tails that survive a send release, and program-dependent ducking.
    The rack owns only preallocated buffers and numeric state; no allocation-heavy
    plugin discovery, model inference, file I/O, or network activity happens here.
    """
    def __init__(self, sample_rate: int, channels: int):
        import numpy as np
        self.sample_rate=int(sample_rate); self.channels=int(channels)
        self.delay_capacity=max(8192,int(self.sample_rate*2.6))
        self.delay=np.zeros((self.delay_capacity,self.channels),dtype=np.float32)
        self.delay_pos=0
        self._last_delay_frames=max(64,int(self.sample_rate*.25))
        self.gate_hp=Biquad(channels)
        self.echo_hp=Biquad(channels); self.echo_lp=Biquad(channels)
        self._gate_phase=0.0
        self._duck_env=0.0
        self.cur={'fx_rack_wet':0.0,'fx_echo_send':0.0,'fx_echo_feedback':0.0,'fx_gate':0.0,'fx_width':0.0,'fx_duck':0.0}
        self.status={'rack_wet':0.0,'beat_echo':0.0,'echo_feedback':0.0,'echo_beats':0.5,'rhythm_gate':0.0,'gate_div':2.0,'width':0.0,'duck':0.0,'tail_rms':0.0}

    @staticmethod
    def _smooth(cur: float, tgt: float, block_s: float, tau: float=.075, slew_per_s: float=3.0) -> float:
        a=1.0-math.exp(-block_s/max(.008,tau)); nxt=cur+(tgt-cur)*a
        return cur+_clamp(nxt-cur,-slew_per_s*block_s,slew_per_s*block_s)

    def _read_delay(self, delay_frames: int, n: int):
        import numpy as np
        idx=(self.delay_pos-int(delay_frames)+np.arange(n,dtype=np.int64))%self.delay_capacity
        return self.delay[idx].copy()

    def _write_delay(self, block):
        n=int(block.shape[0])
        if n<=0: return
        if n>=self.delay_capacity:
            block=block[-self.delay_capacity:]; n=int(block.shape[0])
        end=self.delay_pos+n
        if end<=self.delay_capacity:
            self.delay[self.delay_pos:end]=block
        else:
            first=self.delay_capacity-self.delay_pos
            self.delay[self.delay_pos:]=block[:first]
            self.delay[:n-first]=block[first:]
        self.delay_pos=(self.delay_pos+n)%self.delay_capacity

    def process(self, y, controls: dict, block_s: float):
        import numpy as np
        x=np.asarray(y,dtype=np.float32); n=int(x.shape[0])
        if n<=0: return x
        wet=self._smooth(float(self.cur['fx_rack_wet']),_clamp(float(controls.get('fx_rack_wet',0.0) or 0.0),0,1),block_s,.095,2.5)
        send=self._smooth(float(self.cur['fx_echo_send']),_clamp(float(controls.get('fx_echo_send',0.0) or 0.0),0,1),block_s,.070,4.0)
        feedback=self._smooth(float(self.cur['fx_echo_feedback']),_clamp(float(controls.get('fx_echo_feedback',0.0) or 0.0),0,.46),block_s,.140,1.45)
        gate=self._smooth(float(self.cur['fx_gate']),_clamp(float(controls.get('fx_gate',0.0) or 0.0),0,.42),block_s,.085,2.4)
        width=self._smooth(float(self.cur['fx_width']),_clamp(float(controls.get('fx_width',0.0) or 0.0),0,1),block_s,.110,2.0)
        duck=self._smooth(float(self.cur['fx_duck']),_clamp(float(controls.get('fx_duck',0.0) or 0.0),0,1),block_s,.080,3.0)
        self.cur.update({'fx_rack_wet':wet,'fx_echo_send':send,'fx_echo_feedback':feedback,'fx_gate':gate,'fx_width':width,'fx_duck':duck})
        bpm=_clamp(float(controls.get('bpm',100.0) or 100.0),55.0,190.0)
        echo_beats=_clamp(float(controls.get('fx_echo_beats',.50) or .50),.125,1.50)
        gate_div=_clamp(float(controls.get('fx_gate_div',2.0) or 2.0),1.0,8.0)

        # True zero-work bypass when the rack and its previous echo tail are idle.
        # This matters on Windows because Python callback headroom is more valuable
        # than running filters over silence just to report zeros.
        tail_prev=float(self.status.get('tail_rms',0.0) or 0.0)
        if max(wet,send,feedback,gate,width,duck) < 1e-4 and tail_prev < 2e-5:
            self.status={'rack_wet':0.0,'beat_echo':0.0,'echo_feedback':0.0,'echo_beats':round(echo_beats,3),'rhythm_gate':0.0,'gate_div':round(gate_div,2),'width':0.0,'duck':0.0,'tail_rms':0.0}
            return x

        out=x.copy()
        # TRANS/GATE bus: only the >220 Hz content is chopped. Sub and kick body stay
        # continuous, which is much closer to a professional DJ trans-gate than
        # amplitude-modulating the whole song.
        if gate>1e-4:
            self.gate_hp.set_coeffs('highpass',self.sample_rate,220.0,q=.707)
            hi=self.gate_hp.process(x)
            rate=(bpm/60.0)*gate_div
            ph=(self._gate_phase+np.arange(n,dtype=np.float32)*(rate/float(self.sample_rate)))%1.0
            pulse=(.5+.5*np.cos(2.0*np.pi*ph))**1.65
            # Concert-safe transformer: never hard-mute the presence band. A 36%
            # floor plus smooth cosine shape reads as rhythmic articulation rather
            # than intermittent dropouts on headphones.
            env=(.36+.64*pulse).astype(np.float32)[:,None]
            out += hi*(env-1.0)*gate
            self._gate_phase=float((self._gate_phase+n*(rate/float(self.sample_rate)))%1.0)

        # SEND/RETURN bus: tempo-synchronised filtered ping-pong echo. The delay
        # buffer stores only the send + feedback path, so releasing SEND leaves a
        # natural post-fader tail rather than abruptly killing the effect.
        delay_frames=int(round(self.sample_rate*(60.0/bpm)*echo_beats))
        delay_frames=max(n+8,min(delay_frames,self.delay_capacity-n-8))
        old_frames=int(self._last_delay_frames)
        tap_new=self._read_delay(delay_frames,n)
        if abs(delay_frames-old_frames)>32:
            tap_old=self._read_delay(max(n+8,min(old_frames,self.delay_capacity-n-8)),n)
            a=np.linspace(0.0,1.0,n,endpoint=True,dtype=np.float32)[:,None]
            tap=tap_old*(1.0-a)+tap_new*a
        else:
            tap=tap_new
        self._last_delay_frames=delay_frames
        if self.channels>=2:
            tap=tap.copy(); l=tap[:,0].copy(); tap[:,0]=tap[:,1]; tap[:,1]=l
        self.echo_hp.set_coeffs('highpass',self.sample_rate,180.0+120.0*wet,q=.707)
        self.echo_lp.set_coeffs('lowpass',self.sample_rate,min(10800.0,self.sample_rate*.42),q=.68)
        tail=self.echo_lp.process(self.echo_hp.process(tap))
        rms=float(np.sqrt(np.mean(x*x)+1e-12))
        target_duck=_clamp(rms/.20,0,1)
        if target_duck>self._duck_env: self._duck_env=.55*self._duck_env+.45*target_duck
        else: self._duck_env=.94*self._duck_env+.06*target_duck
        return_gain=wet*(1.0-.58*duck*self._duck_env)
        if self.channels>=2 and width>1e-4:
            mid=.5*(tail[:,0]+tail[:,1]); side=.5*(tail[:,0]-tail[:,1])*(1.0+.85*width)
            tail=tail.copy(); tail[:,0]=mid+side; tail[:,1]=mid-side
        
        # Clean-return guard: keep the echo audible without letting a dense feedback
        # tail become a second full-band mix on top of StudioDSP.
        dry_rms=max(1e-6,float(np.sqrt(np.mean(x*x)+1e-12)))
        tail_rms_pre=float(np.sqrt(np.mean(tail*tail)+1e-12))
        tail_cap=dry_rms*(0.18+0.16*wet)
        if tail_rms_pre>tail_cap:
            tail*=tail_cap/max(tail_rms_pre,1e-8)
        out += tail*(.06+.20*return_gain)*return_gain
        write=x*(.06+.58*send)*send + tail*(feedback*0.92)
        # Bounded feedback saturation prevents a long transition from accumulating
        # runaway energy while preserving a musically useful echo tail.
        write=np.tanh(write*0.96).astype(np.float32,copy=False)
        self._write_delay(write)
        tail_rms=float(np.sqrt(np.mean(tail*tail)+1e-12))
        self.status={'rack_wet':round(wet,4),'beat_echo':round(send,4),'echo_feedback':round(feedback,4),'echo_beats':round(echo_beats,3),'rhythm_gate':round(gate,4),'gate_div':round(gate_div,2),'width':round(width,4),'duck':round(duck,4),'tail_rms':round(tail_rms,5)}
        return np.nan_to_num(out,nan=0.0,posinf=0.0,neginf=0.0).astype(np.float32,copy=False)



def _voice_lookback(pos16, rate, vel_fn, swing=0.0, max_back=16):
    """v31.16 click-free continuity for the numpy fallback path.

    For every sample find the most recent hit of a voice (searching up to
    max_back 16th steps back, honoring swing onsets) and return (tt, vel, note)
    arrays so envelopes decay across step edges instead of being truncated.
    vel_fn(steps:int64 array) -> (vel array, note array)."""
    import numpy as np
    stepi = np.floor(pos16).astype(np.int64)
    s_min = int(stepi.min()) - int(max_back); s_max = int(stepi.max())
    steps = np.arange(s_min, s_max + 1, dtype=np.int64)
    vel, note = vel_fn(steps)
    if swing > 1e-3:
        hit_pos = steps.astype(np.float64) + np.float64(swing) * (np.mod(steps, 2) == 1)
    else:
        hit_pos = steps.astype(np.float64)
    valid = vel > 1e-4
    if not np.any(valid):
        z = np.zeros(pos16.shape, dtype=np.float32)
        return np.full(pos16.shape, -1.0, dtype=np.float32), z, z.copy()
    hp = hit_pos[valid]; vv = vel[valid]; nn = note[valid]
    idx = np.searchsorted(hp, pos16, side="right") - 1
    has = idx >= 0
    idc = np.clip(idx, 0, len(hp) - 1)
    tt = np.where(has, (pos16 - hp[idc]) / np.float64(rate), -1.0).astype(np.float32)
    v = np.where(has, vv[idc], 0.0).astype(np.float32)
    n_ = np.where(has, nn[idc], 0.0).astype(np.float32)
    return tt, v, n_

class SampleDrumLayer:
    """v31.19 DrumMind renderer: sample-based kick / snare / closed-hat / open-hat
    voices on the frame-accurate grid.  Pattern = 32 sixteenths x 4 instruments
    (velocity, microtiming in steps) written by the AI drummer; kit = one-shots
    cut from the record itself (or tuned synthesis).  Voices overlap-add across
    block boundaries, a closed hat chokes the open hat, the kick keys a short
    sidechain duck on the programme."""
    NAMES = ('kick', 'snare', 'chat', 'ohat')

    def __init__(self, sr, channels):
        import numpy as np
        self.sr = int(sr); self.ch = int(channels); self.kit = None
        self.V = np.zeros((32, 4), np.float32); self.MT = np.zeros((32, 4), np.float32); self.pat_seq = -1
        self.voices = []; self._sched = {}; self._duck = 0.0; self.hits_rendered = 0

    def set_kit(self, kit):
        self.kit = kit

    def active(self):
        return bool(self.V.max() > 0.0)

    def set_pattern(self, vel, mt, seq):
        import numpy as np
        try:
            v = np.asarray([float(q) for q in list(vel or [])], np.float32); m = np.asarray([float(q) for q in list(mt or [])], np.float32)
            if v.size == 128 and m.size == 128:
                self.V = np.clip(v.reshape(32, 4), 0.0, 1.2); self.MT = np.clip(m.reshape(32, 4), -0.5, 0.5)
        except Exception:
            pass
        self.pat_seq = int(seq)

    def render(self, n, grid_base, grid_bps, amounts, gains, post):
        import numpy as np, math
        sr = self.sr; out = np.zeros((n, self.ch), np.float32)
        kit = self.kit
        if kit is None or not self.active() or n <= 0:
            self._duck *= math.exp(-max(n, 1) / sr / 0.09)
            return out, np.zeros(n, np.float32)
        rate16 = grid_bps * 4.0; step0 = grid_base * 4.0; span = n / sr * rate16
        k_from = int(math.floor(step0)) - 1; k_to = int(math.floor(step0 + span)) + 1
        kick_events = []
        for k in range(k_from, k_to + 1):
            row = k % 32
            for i in range(4):
                v = float(self.V[row, i])
                if v <= 0.0:
                    continue
                off = (k + float(self.MT[row, i]) - step0) / rate16 * sr
                if off < 0.0 or off >= n:
                    continue
                key = (k, i)
                if key in self._sched:
                    continue
                self._sched[key] = True
                amt = amounts[0] if i == 0 else (amounts[2] if i == 1 else amounts[1])
                if amt <= 1e-3:
                    continue
                buf = kit.get(self.NAMES[i])
                if buf is None or len(buf) < 8:
                    continue
                if i == 2:                      # closed hat chokes the open hat
                    for vc in self.voices:
                        if vc[4] == 3:
                            vc[3] = 0.0
                self.voices.append([buf, 0, int(off), (v ** 1.4) * amt, i]); self.hits_rendered += 1
                if i == 0:
                    kick_events.append(int(off))
        if len(self._sched) > 512:
            self._sched = {kk: vv for kk, vv in self._sched.items() if kk[0] >= k_from - 64}
        bus = [None, None, None, None]; keep = []
        for vc in self.voices:
            buf, pos, start, g, inst = vc
            if g <= 0.0:
                continue
            st = max(0, start); m = min(len(buf) - pos, n - st)
            if m > 0:
                b = bus[inst]
                if b is None:
                    b = np.zeros(n, np.float32); bus[inst] = b
                b[st:st + m] += buf[pos:pos + m] * np.float32(g)
                pos += m
            if pos < len(buf):
                keep.append([buf, pos, 0, g, inst])
        self.voices = keep[-24:]
        lm_k, lm_h = gains
        mono = np.zeros(n, np.float32)
        if bus[0] is not None: mono += bus[0] * lm_k
        if bus[1] is not None: mono += bus[1]
        if bus[2] is not None: mono += bus[2] * lm_h
        if bus[3] is not None: mono += bus[3] * lm_h
        mono *= np.float32(post)
        out[:] = mono[:, None]
        t = np.arange(n, dtype=np.float32) / sr
        env = (self._duck * np.exp(-t / 0.09)).astype(np.float32)
        for e in kick_events:
            env[e:] = np.maximum(env[e:], np.exp(-(t[e:] - t[e]) / 0.09).astype(np.float32))
        self._duck = float(env[-1])
        return out, env


class DeDrum:
    """v31.20: strip the record's own drums out of a Deck-B loop so the AI drummer
    can own the groove.  A two-band transient designer in cut mode (fast/slow
    envelope ratio -> smoothed gain reduction, separately below and above 4 kHz),
    plus a kick-body low cut and a top-end trim.  Stateful, per-sample, no lookahead."""
    def __init__(self, sr, channels):
        import numpy as np
        self.sr = int(sr); self.ch = int(channels)
        self.split_lp = Biquad(channels); self.split_hp = Biquad(channels)
        self.low_cut = Biquad(channels); self.top_cut = Biquad(channels)
        self.split_lp.set_coeffs('lowpass', self.sr, 4000.0, q=.707); self.split_hp.set_coeffs('highpass', self.sr, 4000.0, q=.707)
        self.low_cut.set_coeffs('highpass', self.sr, 120.0, q=.707); self.top_cut.set_coeffs('lowpass', self.sr, 9000.0, q=.707)
        a_f = float(np.exp(-1.0 / (0.003 * self.sr))); a_s = float(np.exp(-1.0 / (0.045 * self.sr))); a_g = float(np.exp(-1.0 / (0.004 * self.sr)))
        self.coef = (a_f, a_s, a_g)
        self.z = {k: np.zeros(1, np.float64) for k in ('fl', 'sl', 'fh', 'sh', 'gl', 'gh')}
        self.reduction_db = 0.0

    def _env(self, x, a, key):
        from scipy.signal import lfilter
        y, zf = lfilter([1.0 - a], [1.0, -a], x, zi=self.z[key])
        self.z[key] = zf
        return y

    def process(self, bb, strength: float):
        import numpy as np
        if strength <= 1e-3 or bb.shape[0] < 4:
            return bb
        a_f, a_s, a_g = self.coef
        lo = self.split_lp.process(bb); hi = self.split_hp.process(bb)
        gains = []
        for band, kf, ks, kg in ((lo, 'fl', 'sl', 'gl'), (hi, 'fh', 'sh', 'gh')):
            m = np.abs(band).mean(axis=1).astype(np.float64)
            fast = self._env(m, a_f, kf); slow = self._env(m, a_s, ks)
            ratio = fast / (slow + 1e-6)
            over = np.maximum(0.0, ratio - 1.35)
            g = 1.0 / (1.0 + (3.5 * strength) * over) ** 1.4
            g = self._env(g, a_g, kg)
            gains.append(g.astype(np.float32))
        y = lo * gains[0][:, None] + hi * gains[1][:, None]
        # kick body and hat air belong to the drummer now
        body_cut = self.low_cut.process(y)
        y = y * (1.0 - 0.75 * strength) + body_cut * (0.75 * strength)
        top_cut = self.top_cut.process(y)
        y = y * (1.0 - 0.6 * strength) + top_cut * (0.6 * strength)
        self.reduction_db = float(20.0 * np.log10(max(1e-4, float(np.mean(gains[0])) * 0.5 + float(np.mean(gains[1])) * 0.5)))
        return y.astype(np.float32, copy=False)


class SynthDeck:
    """v31.22 FutureSynth: one-shot clips (Stable Audio synth parts generated from the
    record's UPCOMING bars) scheduled at absolute render-frame positions, mixed into
    the programme exactly when those bars play.  A clip that arrives late simply
    joins in progress - it stays aligned.  20 ms edge fades, up to 4 clips resident."""
    def __init__(self, sr, channels):
        self.sr = int(sr); self.ch = int(channels); self.clips = []; self.played = 0; self.active = 0.0

    def schedule(self, buf, start_frame: int, gain: float, seq: int = 0):
        import numpy as np
        b = np.array(buf, dtype=np.float32, copy=True)          # v31.28.1: own copy (worker buffers are read-only; the fades write in place)
        if b.ndim == 1:
            b = np.stack([b, b], axis=1)
        b = np.ascontiguousarray(b[:, :self.ch])
        n = b.shape[0]
        if n < 256:
            return False
        fade = min(int(0.02 * self.sr), n // 4)
        w = np.linspace(0.0, 1.0, fade, dtype=np.float32)[:, None]
        b[:fade] *= w; b[-fade:] *= w[::-1]
        self.clips.append([b, int(start_frame), float(gain), False, int(seq)])
        self.clips = self.clips[-16:]
        return True

    def cancel(self, seq: int) -> bool:
        """v31.22.2: drop a clip that has not started (or fade one that has) - play-time harmonic gate."""
        hit = False
        for clip in self.clips:
            if clip[4] == int(seq):
                clip[2] = 0.0; hit = True
        self.clips = [c for c in self.clips if c[2] > 0.0]
        return hit

    def render(self, n, frame_start: int):
        import numpy as np
        out = np.zeros((n, self.ch), np.float32); keep = []; act = 0.0
        for clip in self.clips:
            b, s0, g, started, _sq = clip
            end = s0 + b.shape[0]
            if end <= frame_start:
                continue                                       # finished
            if s0 >= frame_start + n:
                keep.append(clip); continue                   # not yet
            a = max(s0, frame_start); e = min(end, frame_start + n)
            out[a - frame_start:e - frame_start] += b[a - s0:e - s0] * np.float32(g)
            act = max(act, g)
            if not started:
                clip[3] = True; self.played += 1               # counts late joins too
            keep.append(clip)
        self.clips = keep; self.active = act
        return out


class TakeoverLane:
    """v31.23 RackMind: frame-scheduled programme takeover segments.  Two curves per block:
    `amt` (melody takeover: mid/high centre duck, the floor stays) and `full` (freeze: the whole
    programme steps aside for the roll).  Trapezoids: full amount at `start`, held to `end`,
    ramp-in before the start and ramp-out after the end."""
    def __init__(self, sr, channels):
        self.sr = int(sr); self.segments = []          # [start, end, amount, ramp_in, ramp_out, seq, full]
        self.amount = 0.0; self.full = 0.0

    def schedule(self, start, end, amount, ramp_in_s, ramp_out_s, seq=0, mode="takeover"):
        if int(end) <= int(start):
            return False
        self.segments.append([int(start), int(end), float(max(0.0, min(1.0, amount))), max(1, int(ramp_in_s * self.sr)),
                              max(1, int(ramp_out_s * self.sr)), int(seq), 1.0 if str(mode) == "freeze" else 0.0])
        self.segments = self.segments[-8:]
        return True

    def cancel(self, seq):
        n = len(self.segments); self.segments = [q for q in self.segments if q[5] != int(seq)]
        return len(self.segments) < n

    def curves(self, n, frame_start):
        import numpy as np
        t = np.arange(frame_start, frame_start + n, dtype=np.float64)
        amt = np.zeros(n, np.float64); full = np.zeros(n, np.float64); keep = []
        for q in self.segments:
            a, b, g, ri, ro, _sq, fl = q
            if b + ro <= frame_start:
                continue                                       # over
            keep.append(q)
            if a - ri >= frame_start + n:
                continue                                       # not yet
            up = np.clip((t - (a - ri)) / ri, 0.0, 1.0); down = np.clip((b + ro - t) / ro, 0.0, 1.0)
            c = g * np.minimum(up, down)
            if fl > 0.5:
                full = np.maximum(full, c)
            else:
                amt = np.maximum(amt, c)
        self.segments = keep
        self.amount = float(amt[-1]) if n else 0.0; self.full = float(full[-1]) if n else 0.0
        return amt.astype(np.float32), full.astype(np.float32)


class TakeoverDuck:
    """v31.23: the programme steps back for the rack.  Below 170 Hz the floor is kept (bass and kick
    stay under a melody takeover); above it the CENTRE (mid channel) is ducked and darkened while the
    sides survive at half strength, so the record is still felt as a bed.  `full` (the freeze) ducks
    everything.  Reconstruction is exact at zero amount, so an idle stage is transparent."""
    def __init__(self, sr, channels):
        self.sr = int(sr); self.ch = int(channels)
        self.split = Biquad(channels); self.split.set_coeffs('lowpass', self.sr, 170.0, q=.707)
        self.dark = Biquad(1); self.dark.set_coeffs('lowpass', self.sr, 1800.0, q=.707)

    def process(self, x, amt, full):
        import numpy as np
        lo = self.split.process(x); hi = x - lo
        a = np.asarray(amt, dtype=np.float32)[:, None]; f = np.asarray(full, dtype=np.float32)[:, None]
        if self.ch >= 2:
            mid = 0.5 * (hi[:, 0:1] + hi[:, 1:2]); side = 0.5 * (hi[:, 0:1] - hi[:, 1:2])
            dark = self.dark.process(np.ascontiguousarray(mid))
            mid2 = mid * (1.0 - 0.92 * a) + dark * (0.18 * a)         # ~-12 dB centre, a darkened bed remains
            side2 = side * (1.0 - 0.45 * a)
            hi2 = np.concatenate([mid2 + side2, mid2 - side2], axis=1)
            if self.ch > 2:
                hi2 = np.concatenate([hi2, hi[:, 2:] * (1.0 - 0.6 * a)], axis=1)
        else:
            dark = self.dark.process(hi); hi2 = hi * (1.0 - 0.92 * a) + dark * (0.18 * a)
        y = (lo * (1.0 - 0.12 * a) + hi2) * (1.0 - 0.985 * f)
        return y.astype(np.float32, copy=False)


class LineDuck:
    """v31.26 RackMind: frame-scheduled HARMONIC notches that follow one line of the record (the lead
    / vocal / keys line LineFinder found) so the generated part can take its place.  Per segment:
    [start, end, midi, amount, seq]; notches at f0, 2 f0, 3 f0 (Q 12) with 15 ms amount ramps; the
    filters are re-tuned when the note changes and bypassed (exact reconstruction) at zero amount."""
    def __init__(self, sr, channels):
        self.sr = int(sr); self.ch = int(channels); self.segments = []
        self.notch = [Biquad(channels) for _ in range(3)]; self.tuned = -1; self.amount = 0.0
        for bq in self.notch:
            bq.set_coeffs('peaking', self.sr, 1000.0, q=12.0, gain_db=-30.0)      # a deep narrow cut = a notch

    def schedule(self, start, end, midi, amount, seq=0):
        if int(end) <= int(start):
            return False
        self.segments.append([int(start), int(end), int(midi), float(max(0.0, min(1.0, amount))), int(seq)])
        self.segments = self.segments[-64:]
        return True

    def cancel(self, seq):
        n = len(self.segments); self.segments = [q for q in self.segments if q[4] != int(seq)]; return len(self.segments) < n

    def process(self, x, frame_start, n):
        import numpy as np, math
        ramp = int(0.015 * self.sr)
        t = np.arange(frame_start, frame_start + n, dtype=np.float64)
        amt = np.zeros(n, np.float64); midi = -1; keep = []
        for q in self.segments:
            a, b, m, g, _sq = q
            if b + ramp <= frame_start:
                continue
            keep.append(q)
            if a - ramp >= frame_start + n:
                continue
            up = np.clip((t - (a - ramp)) / ramp, 0.0, 1.0); down = np.clip((b + ramp - t) / ramp, 0.0, 1.0)
            c = g * np.minimum(up, down)
            if float(c.max()) > float(amt.max()):
                midi = m
            amt = np.maximum(amt, c)
        self.segments = keep
        self.amount = float(amt[-1]) if n else 0.0
        if midi < 0 or float(amt.max()) <= 1e-4:
            return x
        if midi != self.tuned:
            f0 = 440.0 * 2.0 ** ((midi - 69) / 12.0)
            for k, bq in enumerate(self.notch):
                f = min(0.45 * self.sr, f0 * (k + 1))
                bq.set_coeffs('peaking', self.sr, f, q=12.0, gain_db=-30.0)
            self.tuned = midi
        y = x
        for bq in self.notch:
            y = bq.process(y)
        a = amt.astype(np.float32)[:, None]
        return (x * (1.0 - a) + y * a).astype(np.float32, copy=False)


class RealtimeDJMixer:
    """Realtime-safe two-surface DJ mixer for the Agentic DJ live route.

    Deck A is the delayed live Spotify PCM. Deck B is a bounded loop/roll deck
    captured from Deck A's own rolling history. All expensive analysis/planning
    stays off the audio thread; this class only performs preallocated PCM mixing,
    de-zippered EQ/filter motion and loop playback.
    """
    def __init__(self, sample_rate: int, channels: int, history_seconds: float = 18.0, max_loop_seconds: float = 8.0):
        import numpy as np
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.history_capacity = max(4096, int(self.sample_rate * float(history_seconds)))
        self.history = np.zeros((self.history_capacity, self.channels), dtype=np.float32)
        self.history_pos = 0
        self.history_filled = 0
        self.max_loop_frames = max(2048, int(self.sample_rate * float(max_loop_seconds)))
        self.loop = np.zeros((self.max_loop_frames, self.channels), dtype=np.float32)
        # v31.21 SynthWeave: an AI synth/melody variation of the captured loop, sample-aligned
        self.loop_ai = np.zeros((self.max_loop_frames, self.channels), dtype=np.float32)
        self.loop_ai_len = 0; self.loop_ai_seq = -1; self._ai_mix_eff = 0.0
        self.loop_len = 0
        self.loop_pos = 0
        self.loop_started_at = 0.0
        self._loop_beats = 0.0
        self._loop_selected_offset_beats = 0.0
        self._loop_selection_score = 0.0
        # v31.8.8 Musical Deck-B Match: loop capture is scored against the
        # upcoming audible phrase, not only against its own seam.
        self._loop_harmonic_match = 0.0
        self._loop_rhythm_match = 0.0
        self._loop_salience = 0.0
        self._loop_energy_match = 0.0
        self._loop_role = "BALANCED"
        # v31.8.9 fixed musical gate: creativity may create more shift
        # opportunities, but it may never lower this qualification.
        self._loop_quality_gate = 0.0
        self.last_capture_seq = 0
        self.last_release_seq = 0
        self._loop_release_pending = False
        self.a_low = Biquad(channels); self.a_mid = Biquad(channels); self.a_high = Biquad(channels)
        self.a_hp = Biquad(channels); self.a_lp = Biquad(channels)
        self.b_low = Biquad(channels); self.b_mid = Biquad(channels); self.b_high = Biquad(channels)
        self.b_hp = Biquad(channels); self.b_lp = Biquad(channels)
        # v31.8.5 Deck B Remix Engine. Deck B can act as a low-correlation
        # source-derived remix layer instead of only a duplicate full-band roll.
        self.b_focus_hp = Biquad(channels); self.b_focus_lp = Biquad(channels)
        self.b_transient_lp = Biquad(channels)
        # v31.9.1 Musical Co-Mix: Deck B is spectrally slotted around Deck A
        # instead of being treated as one broadband second signal. Dedicated
        # crossover state keeps the operation realtime-safe and avoids FFT work
        # in the audio callback.
        self.comix_a_low = Biquad(channels); self.comix_a_body = Biquad(channels)
        self.comix_b_low = Biquad(channels); self.comix_b_body = Biquad(channels)
        self._b_layer_gain = 0.0
        self._loop_micro_align_ms = 0.0
        self._comix_role = 'BALANCED'
        self._comix_low_keep = 0.0
        self._comix_body_keep = 1.0
        self._comix_air_keep = 1.0
        self._comix_center_keep = 1.0
        self._b_texture = 0.0
        self._b_transient = 0.0
        self._last_retrigger_seq = 0
        # v31.8.6 Intelligent Live Slicer. Deck B is divided into eight equal,
        # beat-synchronous slices. The agent chooses a musical pattern family;
        # the engine refines replaceable steps using source transient/sustain
        # descriptors while preserving downbeat anchors (0 and 4).
        self._slice_energy = np.ones(8, dtype=np.float32) * 0.5
        self._slice_transient = np.ones(8, dtype=np.float32) * 0.5
        self._slice_brightness = np.ones(8, dtype=np.float32) * 0.5
        self._slice_salience = np.ones(8, dtype=np.float32) * 0.5
        self._slice_map = np.arange(8, dtype=np.int32)
        self._slice_mode = 0
        self._slice_motion = 0.0
        self._slice_mix = 0.0
        self._b_fx_amount = 0.0
        # Dedicated Deck-B remix FX bus. This is intentionally separate from the
        # post-mix Performance FX Rack so the ghost/remix material can sound
        # larger than Deck A without smearing the whole master.
        self.b_fx_body_lp = Biquad(channels)
        self._b_fx_delay_len = max(4096, int(self.sample_rate * 1.35))
        self._b_fx_delay = np.zeros((self._b_fx_delay_len, self.channels), dtype=np.float32)
        self._b_fx_pos = 0
        self._b_fx_env = 0.0
        # Source-derived performance FX. No sample files, model calls or filesystem
        # work occur here: risers/impacts are synthesized deterministically and the
        # drum layer is extracted from the already-playing mix.
        self.drum_hp = Biquad(channels); self.drum_lp = Biquad(channels)
        # v31.8.3 Source-Aware Transition FX: synthetic percussion/noise is only
        # a support layer. Separate stateful filters extract timbre from the live
        # song so hats, fills and sweeps inherit the record's spectral fingerprint.
        self.top_hp = Biquad(channels); self.top_lp = Biquad(channels)
        self.hat_src_hp = Biquad(channels); self.hat_src_lp = Biquad(channels)
        self.riser_hp = Biquad(channels); self.riser_lp = Biquad(channels)
        self.riser_src_hp = Biquad(channels); self.riser_src_lp = Biquad(channels)
        self.reverse_hp = Biquad(channels); self.reverse_lp = Biquad(channels)
        self.reverse_src_hp = Biquad(channels); self.reverse_src_lp = Biquad(channels)
        self.rush_hp = Biquad(channels); self.rush_lp = Biquad(channels)
        self.rush_src_hp = Biquad(channels); self.rush_src_lp = Biquad(channels)
        self.impact_click_hp = Biquad(channels); self.impact_crash_hp = Biquad(channels); self.impact_crash_lp = Biquad(channels)
        self._fx_rng = np.random.default_rng(0x4A4D3133)
        self._impact_env = 0.0
        self._impact_phase = 0.0
        self._last_impact_seq = 0
        self._rhythm_phase = 0.0
        self._rush_phase = 0.0
        self._fx_sample_clock = 0
        self._hat_variant = 0
        self._rush_variant = 0
        self._riser_variant = 0
        self._reverse_variant = 0
        self._hat_was_active = False
        self._rush_was_active = False
        self._riser_was_active = False
        self._reverse_was_active = False
        self._riser_age_sec = 0.0
        self._reverse_age_sec = 0.0
        self._fx_bus_gain = 1.0
        self._res_gain = 1.0
        self._res_rms_track = 0.0
        self._trim_gain = 1.0
        # v31.11 StageCraft FX state.  Spinback/brake resamples the mixer's own
        # recent output ring (a "record" of what the listener just heard); pump is
        # a beat-locked sidechain gain; bass-cut is the build-section low strip.
        import numpy as _np
        self._fxout_ring = _np.zeros((int(sample_rate*6), channels), dtype=_np.float32)
        self._fxout_abs = 0
        self._brake_active = False
        self._brake_mode = 0
        self._brake_len_s = 0.5
        self._brake_age_s = 0.0
        self._brake_read_abs = 0.0
        self._last_brake_seq = 0
        self._brake_refade = 0
        # v31.14 TimeWeave playheads (program coords / capture coords).
        self._weave_src_abs = 0
        self._weave_fut_abs = 0
        self._weave_cell_beats = 2.0
        self._weave_prev_mask = 0.0
        self._pump_phase = 0.0
        self._last_pump_sync_seq = 0
        self.bass_cut_hp = Biquad(channels)
        # v31.13 GridLock: one beat-phase accumulator drives every rhythmic FX
        # (hats / rush / redrum / pump).  It advances by BPM per block (immune to
        # BPM re-estimate jumps) and is gently PLL-pulled toward the agent's
        # audible beat anchor, so pattern accents land ON the song's beats
        # instead of on an arbitrary engine-start grid.
        self._grid_beats = 0.0
        self._grid_locked = False
        self._grid_conf = 0.0
        self._hat_density = 1.0
        # v31.12 FullRemix re-drum: a synthesized kick/clap groove played UNDER
        # the live song — the defining remix signature from production practice.
        self.redrum_click_hp = Biquad(channels)
        self.redrum_clap_hp = Biquad(channels)
        self.redrum_clap_lp = Biquad(channels)
        # v31.14 TimeWeaver: key-following synth bass + power-chord stab layer
        # (root/fifth/octave only — no third, so major and minor keys are both safe).
        self.stab_hp = Biquad(channels)
        self.stab_lp = Biquad(channels)
        # v31.18 level matcher: added kick/bass and hats follow the record's own
        # low-band / top-band transient level instead of a fixed design gain.
        self.lm_low_lp = Biquad(channels)
        self.lm_hf_hp = Biquad(channels)
        self._lm = {}
        self.drums = SampleDrumLayer(sample_rate, channels)
        self._drum_sample_mode = False
        self.dedrum = DeDrum(sample_rate, channels)
        self._b_dedrum = 0.0
        self.synth = SynthDeck(sample_rate, channels)
        self.takeover = TakeoverLane(self.sample_rate, self.channels)
        self._duck_y = TakeoverDuck(self.sample_rate, self.channels); self._duck_out = TakeoverDuck(self.sample_rate, self.channels)
        self.lineduck_y = LineDuck(self.sample_rate, self.channels); self.lineduck_out = LineDuck(self.sample_rate, self.channels)
        # v31.28 VocalWeave: a frame-scheduled SUBTRACTION deck (the record's separated vocal, negative gain) applied to the
        # dry reference and the programme before the residual cap: the vocal leaves, every DJ layer stays
        self.cancel_deck = SynthDeck(self.sample_rate, self.channels); self._cancel_active = 0.0
        self._frame_start = -1
        self._clar_src = 1.0
        self._clar_gate = 1.0
        self._grid_frame_lock = False
        # Future-material tap for time weaving (attached by the engine after the
        # lookahead ring exists).  Reads are one small slice per block, callback-safe.
        self._future_ring = None
        # v31.15 RustCore: native hot-path kernels (deck strips, program
        # bass-cut, groove synth).  None => pure-numpy path, behavior unchanged.
        try:
            import rust_dsp as _rust_dsp
            self._rust = _rust_dsp.create(self.sample_rate, self.channels, BIQUAD_MORPH_FRAMES)
        except Exception:
            self._rust = None
        self.pro_fx = ProfessionalFXRack(self.sample_rate,self.channels)
        self.cur = {
            'a_low_db':0.0,'a_mid_db':0.0,'a_high_db':0.0,'a_filter':0.0,'a_gain':1.0,
            'b_low_db':0.0,'b_mid_db':0.0,'b_high_db':0.0,'b_filter':0.0,'b_gain':1.0,
            'crossfader':0.0,'b_layer':0.0,'b_texture':0.0,'b_transient':0.0,'b_slicer_mix':0.0,'b_slice_mode':0.0,'b_fx':0.0,'noise_riser':0.0,'fx_reverse_swell':0.0,'fx_snare_rush':0.0,'impact_strength':0.0,'drum_drive':0.0,'doubletime':0.0,
            'fx_pump':0.0,'fx_bass_cut':0.0,'fx_rush_accel':0.0,'fx_redrum':0.0,
            'fx_bass_synth':0.0,'fx_stab':0.0,
        }
        self.status = {'loop_ready':False,'loop_frames':0,'loop_beats':0.0,'loop_age_sec':0.0,'loop_selected_offset_beats':0.0,'loop_selection_score':0.0,'loop_harmonic_match':0.0,'loop_rhythm_match':0.0,'loop_salience':0.0,'loop_energy_match':0.0,'loop_role':'BALANCED','loop_quality_gate':0.0,'loop_micro_align_ms':0.0,'comix_role':'BALANCED','comix_low_keep':0.0,'comix_body_keep':1.0,'comix_air_keep':1.0,'comix_center_keep':1.0,'crossfader':0.0,'b_layer':0.0,'b_texture':0.0,'b_transient':0.0,'b_slicer_mix':0.0,'b_slice_mode':'OFF','b_slice_map':[0,1,2,3,4,5,6,7],'b_slice_motion':0.0,'b_fx':0.0,'deckb_mode':'FULL','correlation':0.0,'noise_riser':0.0,'fx_reverse_swell':0.0,'fx_snare_rush':0.0,'impact_strength':0.0,'drum_drive':0.0,'doubletime':0.0,'impact_env':0.0}
        self.status.update({'rack_wet':0.0,'beat_echo':0.0,'echo_feedback':0.0,'echo_beats':0.5,'rhythm_gate':0.0,'gate_div':2.0,'width':0.0,'duck':0.0,'tail_rms':0.0,
                            'hat_variant':0,'riser_variant':0,'rush_variant':0,'reverse_variant':0,'source_aware_fx':True})

    def _append_history(self, x):
        n = int(x.shape[0])
        if n <= 0: return
        if n >= self.history_capacity:
            x = x[-self.history_capacity:]; n = int(x.shape[0])
        end = self.history_pos + n
        if end <= self.history_capacity:
            self.history[self.history_pos:end] = x
        else:
            first = self.history_capacity - self.history_pos
            self.history[self.history_pos:] = x[:first]
            self.history[:n-first] = x[first:]
        self.history_pos = (self.history_pos + n) % self.history_capacity
        self.history_filled = min(self.history_capacity, self.history_filled + n)

    def _history_tail(self, n):
        import numpy as np
        n = min(max(0, int(n)), int(self.history_filled), int(self.max_loop_frames))
        if n <= 0:
            return np.zeros((0,self.channels), dtype=np.float32)
        start = (self.history_pos - n) % self.history_capacity
        if start < self.history_pos or self.history_filled < self.history_capacity:
            return self.history[start:start+n].copy()
        return np.concatenate((self.history[start:], self.history[:self.history_pos]), axis=0).astype(np.float32, copy=False)

    def _history_window(self, n: int, end_offset: int = 0):
        """Return a contiguous copy ending ``end_offset`` frames before now.

        Unlike ``_history_tail`` this can inspect earlier beat-aligned candidates
        without changing the realtime history pointer. It runs only on a quantized
        loop-capture event.
        """
        import numpy as np
        n=max(0,int(n)); end_offset=max(0,int(end_offset))
        available=max(0,int(self.history_filled)-end_offset)
        n=min(n,available,int(self.max_loop_frames))
        if n<=0:
            return np.zeros((0,self.channels),dtype=np.float32)
        end=(self.history_pos-end_offset)%self.history_capacity
        start=(end-n)%self.history_capacity
        if start<end:
            return self.history[start:end].copy()
        return np.concatenate((self.history[start:],self.history[:end]),axis=0).astype(np.float32,copy=False)

    def _chroma_signature(self, mono):
        """Sparse 12-bin pitch-class profile for a captured Deck-B candidate."""
        import numpy as np
        x=np.asarray(mono,dtype=np.float32).reshape(-1)
        if x.size<512:
            return np.ones(12,dtype=np.float32)/12.0,0.0
        fft_n=2048 if self.sample_rate>=16000 else 1024
        if x.size<fft_n:
            x=np.pad(x,(0,fft_n-x.size))
        centers=np.linspace(fft_n//2,max(fft_n//2,x.size-fft_n//2),min(6,max(2,x.size//fft_n+1)),dtype=np.int64)
        win=np.hanning(fft_n).astype(np.float32); acc=None
        for c0 in centers:
            a=max(0,int(c0)-fft_n//2); z=x[a:a+fft_n]
            if z.size<fft_n: z=np.pad(z,(0,fft_n-z.size))
            sp=np.abs(np.fft.rfft(z*win)).astype(np.float64)
            acc=sp if acc is None else acc+sp
        spec=acc/max(1,len(centers)); freqs=np.fft.rfftfreq(fft_n,1.0/self.sample_rate)
        chroma=np.zeros(12,dtype=np.float64)
        for fi in np.where((freqs>=70)&(freqs<=3200))[0]:
            f=float(freqs[fi])
            if f<=0: continue
            midi=int(round(69+12*math.log2(f/440.0)))
            chroma[midi%12]+=float(spec[fi]**1.15)
        sm=float(np.sum(chroma))
        if sm<=1e-12: return np.ones(12,dtype=np.float32)/12.0,0.0
        chroma/=sm
        entropy=-float(np.sum(chroma*np.log(chroma+1e-12)))/math.log(12.0)
        tonal=_clamp(1.0-entropy,0,1)
        return chroma.astype(np.float32),float(tonal)

    def _rhythm_signature(self, mono, beats: float):
        """16th-grid accent profile, normalized to one four-beat phrase."""
        import numpy as np
        x=np.asarray(mono,dtype=np.float32).reshape(-1)
        steps=max(4,int(round(max(.25,float(beats))*4.0)))
        edges=np.linspace(0,x.size,steps+1,dtype=np.int64); r=np.zeros(steps,dtype=np.float32)
        for i in range(steps):
            z=x[int(edges[i]):max(int(edges[i])+8,int(edges[i+1]))]
            if z.size>16:
                aw=max(8,min(int(self.sample_rate*.032),max(8,z.size//3)))
                attack=float(np.sqrt(np.mean(z[:aw]*z[:aw])+1e-12))
                body=float(np.sqrt(np.mean(z[aw:]*z[aw:])+1e-12)) if z.size>aw+8 else attack
                r[i]=max(0.0,attack-.68*body)+.16*attack
        mx=float(np.max(r))
        if mx>1e-8: r/=mx
        # Tile or interpolate to a canonical 16-step bar so short roll material can
        # be compared to the next full bar without punishing its duration.
        if steps==16: return r
        idx=np.arange(16,dtype=np.float32)*(steps/16.0)
        return np.interp(idx,np.arange(steps,dtype=np.float32),r,period=steps).astype(np.float32)

    @staticmethod
    def _cosine01(a,b):
        import numpy as np
        aa=np.asarray(a,dtype=np.float32); bb=np.asarray(b,dtype=np.float32)
        if aa.size!=bb.size or aa.size==0: return .5
        den=float(np.linalg.norm(aa)*np.linalg.norm(bb))
        if den<1e-9: return .5
        return _clamp(float(np.dot(aa,bb))/den,0.0,1.0)

    def _loop_candidate_features(self, src, beats: float, texture: float):
        """Role-aware salience, rhythm and local harmonic identity for Deck B."""
        import numpy as np
        mono=np.mean(src,axis=1).astype(np.float32,copy=False)
        rms=float(np.sqrt(np.mean(mono*mono)+1e-12))
        chroma,tonal=self._chroma_signature(mono)
        rhythm=self._rhythm_signature(mono,beats)
        # Accent contrast identifies meaningful kick/snare/hat motifs rather than
        # background wash; tonal concentration identifies hooks/chordal material.
        rmean=float(np.mean(rhythm)); rstd=float(np.std(rhythm))
        accent=_clamp((float(np.percentile(rhythm,90))-rmean)/(rstd+.20),0,1)
        dyn=_clamp(rms/.20,0,1)
        if texture>.12:
            role='PERCUSSIVE'; sal=.52*accent+.20*float(np.max(rhythm))+.18*dyn+.10*(1.0-tonal)
        elif texture<-.12:
            role='HARMONIC'; sal=.56*tonal+.25*dyn+.19*(1.0-accent)
        else:
            role='BALANCED'; sal=.34*accent+.34*tonal+.22*dyn+.10*max(accent,tonal)
        # Absolute audibility matters: a nearly silent ambience cell can have high
        # *relative* accent contrast, but it is not a significant DJ motif.
        activity_gate=.20+.80*math.sqrt(_clamp(dyn,0,1))
        sal*=activity_gate
        return {'mono':mono,'rms':rms,'chroma':chroma,'tonal':float(tonal),'rhythm':rhythm,
                'accent':float(accent),'salience':_clamp(float(sal),0,1),'role':role,'energy':_clamp(rms/.24,0,1)}

    def _loop_candidate_score(self, src, texture: float, shift_beats: float, beats: float, controls=None):
        """Future-aware musical loop score.

        A clean seam is necessary but no longer sufficient. The captured material
        must also agree with the next audible phrase rhythmically and, when tonal,
        harmonically. A role-aware salience term prevents irrelevant ambience from
        winning merely because its seam is easy to loop.
        """
        import numpy as np
        controls=controls or {}
        if src is None or src.shape[0]<256: return -1e9,{}
        feat=self._loop_candidate_features(src,beats,texture); mono=feat['mono']; rms=feat['rms']
        if rms<2e-5: return -2.0-.02*float(shift_beats),feat
        seam=max(32,min(int(self.sample_rate*.010),mono.size//12)); head=mono[:seam]; tail=mono[-seam:]
        seam_err=float(np.sqrt(np.mean((head-tail)**2)+1e-12))/max(rms,1e-6)
        amp_jump=abs(float(head[0]-tail[-1]))/max(rms,1e-6)
        dh=np.diff(head); dt=np.diff(tail)
        slope_err=abs(float(np.mean(dh)-np.mean(dt)))/max(rms,1e-6) if dh.size and dt.size else 0.0
        edges=np.linspace(0,mono.size,9,dtype=np.int64); er=[]
        for i in range(8):
            z=mono[int(edges[i]):max(int(edges[i])+4,int(edges[i+1])):4]
            er.append(float(np.sqrt(np.mean(z*z)+1e-12)) if z.size else 0.0)
        er=np.asarray(er,dtype=np.float32); energy_cv=float(np.std(er)/(np.mean(er)+1e-6))
        seam_score=_clamp(1.0-.28*min(2.5,seam_err)-.09*min(3.0,amp_jump)-.06*min(3.0,slope_err)-.08*min(2.0,energy_cv),0,1)

        tc=np.asarray(controls.get('future_chroma') or [],dtype=np.float32)
        tr=np.asarray(controls.get('future_rhythm') or [],dtype=np.float32)
        tonal_t=_clamp(float(controls.get('future_tonal_strength',0.0) or 0.0),0,1)
        harmonic=.5
        if tc.size==12:
            direct=self._cosine01(feat['chroma'],tc)
            # Related pitch classes receive a little tolerance (perfect fourth/fifth)
            # without allowing arbitrary transposition to masquerade as a match.
            ca=.76*feat['chroma']+.12*np.roll(feat['chroma'],5)+.12*np.roll(feat['chroma'],-5)
            cb=.76*tc+.12*np.roll(tc,5)+.12*np.roll(tc,-5)
            raw_h=.76*direct+.24*self._cosine01(ca,cb)
            # Chroma from noise/percussion is unstable. Only grant large harmonic
            # bonuses when both the candidate and future phrase have tonal identity.
            tg=_clamp(.15+2.50*math.sqrt(max(0.0,float(feat['tonal']))*max(.02,tonal_t)),.15,1.0)
            harmonic=.5+(raw_h-.5)*tg
        rhythm=.5
        if tr.size==16:
            w=np.asarray([1.30 if i%4==0 else (1.10 if i%2==0 else .92) for i in range(16)],dtype=np.float32)
            rhythm=self._cosine01(feat['rhythm']*w,tr*w)
        target_energy=_clamp(float(controls.get('future_energy',feat['energy']) or feat['energy']),0,1)
        energy_match=_clamp(1.0-abs(float(feat['energy'])-target_energy),0,1)
        sal=float(feat['salience']); texture=float(texture)
        if texture>.12:
            score=.28*rhythm+.10*harmonic*(.35+.65*tonal_t)+.28*sal+.24*seam_score+.10*energy_match
        elif texture<-.12:
            score=.12*rhythm+.34*harmonic*(.45+.55*tonal_t)+.27*sal+.19*seam_score+.08*energy_match
        else:
            score=.23*rhythm+.23*harmonic*(.40+.60*tonal_t)+.27*sal+.19*seam_score+.08*energy_match
        # Very low-information candidates should not win just because they are smooth.
        if sal<.20: score-=.18*(.20-sal)/.20
        score-=.014*float(shift_beats)
        feat.update({'seam_score':float(seam_score),'harmonic_match':float(harmonic),'rhythm_match':float(rhythm),'energy_match':float(energy_match)})
        return float(score),feat

    def _micro_align_loop(self, src, beats: float, texture: float, controls=None):
        """Sub-beat phase alignment for a captured Deck-B motif.

        Beat-grid capture can still land a few milliseconds away from the most
        convincing transient.  Search only a tiny circular neighborhood so phrase
        identity is preserved, then prefer the phase whose first attack, seam and
        future-rhythm agreement are best.  Harmonic beds are moved less than
        percussive material to avoid audible pitch/phase wobble.
        """
        import numpy as np
        controls=controls or {}
        if src is None or src.shape[0] < 512:
            return src,0.0
        max_ms=30.0 if float(texture)>.12 else (14.0 if float(texture)<-.12 else 22.0)
        steps=[0.0,-max_ms,-max_ms*.5,max_ms*.5,max_ms]
        future_r=np.asarray(controls.get('future_rhythm') or [],dtype=np.float32)
        want_attack=float(future_r[0]) if future_r.size==16 else .55
        best_src=src; best_ms=0.0; best_score=-1e9
        base_rms=float(np.sqrt(np.mean(src*src)+1e-12))
        for ms in steps:
            off=int(round(float(ms)*self.sample_rate/1000.0))
            z=np.roll(src,off,axis=0) if off else src
            mono=np.mean(z,axis=1).astype(np.float32,copy=False)
            aw=max(16,min(int(self.sample_rate*.040),mono.size//5))
            attack=float(np.sqrt(np.mean(mono[:aw]*mono[:aw])+1e-12))/max(base_rms,1e-7)
            seam=max(24,min(int(self.sample_rate*.006),mono.size//14))
            seam_err=float(np.sqrt(np.mean((mono[:seam]-mono[-seam:])**2)+1e-12))/max(base_rms,1e-7)
            rr=self._rhythm_signature(mono,beats)
            rhy=self._cosine01(rr,future_r) if future_r.size==16 else .5
            # Strong future downbeats favor a crisp first attack; weak downbeats
            # favor continuity instead of forcing a transient.
            attack_fit=1.0-abs(_clamp(attack/1.85,0,1)-_clamp(.25+.75*want_attack,0,1))
            score=.48*rhy+.30*_clamp(1.0-.42*seam_err,0,1)+.22*attack_fit-.0009*abs(float(ms))
            if score>best_score:
                best_score=score; best_src=z.copy() if off else src; best_ms=float(ms)
        return best_src,float(best_ms)

    def _resolve_comix_role(self, requested_texture: float, controls=None):
        """Choose the audible Deck-B role from measured compatibility, not intent alone."""
        controls=controls or {}
        h=_clamp(float(self._loop_harmonic_match),0,1); r=_clamp(float(self._loop_rhythm_match),0,1)
        s=_clamp(float(self._loop_salience),0,1); tonal=_clamp(float(controls.get('future_tonal_strength',0.0) or 0.0),0,1)
        vocal=_clamp(float(controls.get('future_vocal',0.0) or 0.0),0,1)
        req=float(requested_texture)
        # A rhythmically strong but harmonically weak capture is useful as drums,
        # not as a second melody. This is the most important anti-clash fallback.
        if (h < .53 and r >= .60) or vocal>.72:
            return 'PERCUSSIVE',max(.58,req)
        # Strong tonal agreement can keep a recognizable hook/bed, provided the
        # future is not vocal-heavy.
        if h>=.68 and tonal>=.40 and s>=.20 and vocal<.60:
            return ('HARMONIC',min(-.34,req)) if req<.05 or r<.64 else ('BALANCED',min(.12,max(-.18,req)))
        if r>=.67:
            return 'PERCUSSIVE',max(.40,req)
        return 'BALANCED',_clamp(req,-.22,.42)

    def _spectral_comix(self, aa, bb, xf: float, layer: float, controls: dict):
        """Complementary A/B spectral placement with center-vocal protection.

        Deck B is decomposed into low/body/air bands using persistent IIR
        crossovers.  The band gains depend on Deck-B role, A's current spectral
        occupancy and future vocal density.  This approximates the frequency
        discipline of a skilled three-band DJ mix without claiming stem separation.
        """
        import numpy as np
        if bb is None or bb.size==0:
            return bb
        self.comix_a_low.set_coeffs('lowpass',self.sample_rate,180.0,q=.707)
        self.comix_a_body.set_coeffs('lowpass',self.sample_rate,3600.0,q=.707)
        self.comix_b_low.set_coeffs('lowpass',self.sample_rate,180.0,q=.707)
        self.comix_b_body.set_coeffs('lowpass',self.sample_rate,3600.0,q=.707)
        al=self.comix_a_low.process(aa); ablp=self.comix_a_body.process(aa); am=ablp-al; ah=aa-ablp
        bl=self.comix_b_low.process(bb); bblp=self.comix_b_body.process(bb); bm=bblp-bl; bh=bb-bblp
        def rms(z): return float(np.sqrt(np.mean(z*z)+1e-12))
        ae=np.asarray([rms(al),rms(am),rms(ah)],dtype=np.float32); total=float(np.sum(ae)+1e-8); occ=ae/total
        role,eff_tex=self._resolve_comix_role(float(controls.get('b_texture',0.0) or 0.0),controls)
        self._comix_role=role
        vocal=_clamp(float(controls.get('future_vocal',0.0) or 0.0),0,1)
        h=_clamp(float(self._loop_harmonic_match),0,1); r=_clamp(float(self._loop_rhythm_match),0,1)
        foreground=_clamp((float(xf)-.10)/.34,0,1)
        if role=='PERCUSSIVE': base=np.asarray([.035,.46,.90],dtype=np.float32)
        elif role=='HARMONIC': base=np.asarray([.045,.72,.56],dtype=np.float32)
        else: base=np.asarray([.055,.62,.72],dtype=np.float32)
        # Deck A retains sub authority. Body/air can open where A is sparse.
        availability=np.asarray([.30,.48,.60],dtype=np.float32)+np.asarray([.15,.34,.28],dtype=np.float32)*(1.0-occ)
        keep=base*availability
        keep[0]*=.55+.18*foreground
        keep[1]*=1.0-.42*vocal
        # Excellent harmonic+rhythmic compatibility can open the musical body a
        # little more; poor harmony can never be compensated by creativity.
        keep[1]*=.72+.35*h; keep[2]*=.78+.28*r
        keep=np.clip(keep,0.015,1.0)
        out=bl*float(keep[0])+bm*float(keep[1])+bh*float(keep[2])
        center_keep=1.0
        if self.channels>=2:
            mid=.5*(out[:,0]+out[:,1]); side=.5*(out[:,0]-out[:,1])
            # When a vocal is expected, Deck B vacates the center rather than
            # competing with the lead. Preserve side detail for width/excitement.
            center_keep=_clamp(1.0-.46*vocal*(1.0-.35*foreground),.48,1.0)
            side_keep=_clamp(1.02+.18*(1.0-vocal)+.10*(role=='PERCUSSIVE'),1.0,1.28)
            out=out.copy(); out[:,0]=mid*center_keep+side*side_keep; out[:,1]=mid*center_keep-side*side_keep
        src_r=rms(bb); out_r=rms(out)
        cap=src_r*(.92 if role=='PERCUSSIVE' else .86)
        if out_r>cap and out_r>1e-8: out*=cap/out_r
        self._comix_low_keep=float(keep[0]); self._comix_body_keep=float(keep[1]); self._comix_air_keep=float(keep[2]); self._comix_center_keep=float(center_keep)
        return out.astype(np.float32,copy=False)

    def _deckb_quality_factor(self, texture: float, controls=None) -> float:
        """Fixed role-aware gate for any audible Deck-B shift/layer.

        This is intentionally independent of creativity/agent authority. More
        creativity can request more A<->B pulses, but a weak harmonic/rhythmic/
        salient capture is still attenuated or rejected.
        """
        controls=controls or {}
        h=_clamp(float(self._loop_harmonic_match),0,1); r=_clamp(float(self._loop_rhythm_match),0,1)
        s=_clamp(float(self._loop_salience),0,1); e=_clamp(float(self._loop_energy_match),0,1)
        tonal=_clamp(float(controls.get('future_tonal_strength',0.0) or 0.0),0,1)
        texture=float(texture)
        if texture>.12:
            comp=.50*r+.25*s+.10*e+.15*(h if tonal>.45 else .72)
            hard_ok=(r>=.52 and s>=.18 and (tonal<=.58 or h>=.42))
        elif texture<-.12:
            comp=.50*h+.25*s+.15*e+.10*r
            hard_ok=(s>=.18 and (tonal<=.32 or h>=.55) and r>=.34)
        else:
            comp=.34*h+.36*r+.22*s+.08*e
            hard_ok=(r>=.50 and s>=.18 and (tonal<=.50 or h>=.48))
        if not hard_ok:
            return 0.0
        # Smooth gate: qualifying material enters gently; excellent matches receive
        # full authority. The threshold never changes with creativity.
        x=_clamp((comp-.50)/.24,0,1)
        return _clamp(x*x*(3.0-2.0*x),0,1)

    def _capture_loop(self, bpm: float, beats: float, controls=None):
        import numpy as np, time
        controls=controls or {}
        bpm = _clamp(float(bpm or 100.0), 55.0, 190.0)
        beats = _clamp(float(beats or 4.0), 0.25, 16.0)
        beat_frames=max(64,int(round(self.sample_rate*60.0/bpm)))
        n = int(round(beat_frames * beats))
        n = max(256, min(n, self.max_loop_frames, self.history_filled))
        if n<256:
            return
        texture=float(controls.get('b_texture',0.0) or 0.0)
        # Search a bounded set of whole-beat offsets. Older material is considered
        # only because a salient motif can be much more useful than the most recent
        # low-information tail; recency remains a penalty in the scorer.
        shifts=[0.0,1.0,2.0,4.0]
        if self.history_filled>=n+int(6*beat_frames): shifts.append(6.0)
        if self.history_filled>=n+int(8*beat_frames): shifts.append(8.0)
        candidates=[]
        for sb in shifts:
            off=int(round(sb*beat_frames))
            if off+n>self.history_filled: continue
            src=self._history_window(n,off)
            if src.shape[0]!=n: continue
            score,feat=self._loop_candidate_score(src,texture,sb,beats,controls)
            candidates.append((score,sb,src,feat))
        if not candidates: return
        candidates.sort(key=lambda z:z[0],reverse=True)
        newest=next((z for z in candidates if z[1]==0.0),candidates[0]); best=candidates[0]
        # Significant future-compatible material is allowed to come from further
        # back, but tiny score wins never cause surprising historical grabs.
        if best[1]>0 and best[0] < newest[0]+.045: best=newest
        score,shift_beats,src,feat=best
        # Sub-beat alignment: keep the same phrase, but rotate only a few ms so
        # the captured motif's first transient/seam agrees better with the future
        # groove. This is intentionally tiny; phrase identity never moves.
        src,micro_ms=self._micro_align_loop(src,beats,texture,controls)
        self._loop_micro_align_ms=float(micro_ms)
        # Re-evaluate the final aligned candidate for status/quality telemetry.
        score2,feat2=self._loop_candidate_score(src,texture,shift_beats,beats,controls)
        if score2>-1e8:
            score=float(score2); feat=feat2
        self.loop[:src.shape[0]] = src
        self.loop_len = int(src.shape[0]); self.loop_pos = 0; self.loop_started_at = time.monotonic()
        self._loop_beats=float(beats); self._loop_selected_offset_beats=float(shift_beats); self._loop_selection_score=float(score)
        self._loop_harmonic_match=float(feat.get('harmonic_match',.5)); self._loop_rhythm_match=float(feat.get('rhythm_match',.5))
        self._loop_salience=float(feat.get('salience',0.0)); self._loop_energy_match=float(feat.get('energy_match',.5)); self._loop_role=str(feat.get('role') or 'BALANCED')
        self._loop_quality_gate=float(self._deckb_quality_factor(texture,controls))
        # DC-neutralize once at capture; low-frequency musical content is retained.
        self.loop[:self.loop_len] -= np.mean(self.loop[:self.loop_len], axis=0, keepdims=True).astype(np.float32) * 0.015
        self._analyze_loop_slices()
        self.status.update({'loop_ready':True,'loop_frames':self.loop_len,'loop_beats':round(beats,3),
                            'loop_age_sec':0.0,'loop_selected_offset_beats':round(float(shift_beats),3),
                            'loop_selection_score':round(float(score),4),'loop_harmonic_match':round(self._loop_harmonic_match,4),
                            'loop_rhythm_match':round(self._loop_rhythm_match,4),'loop_salience':round(self._loop_salience,4),
                            'loop_energy_match':round(self._loop_energy_match,4),'loop_role':self._loop_role,'loop_quality_gate':round(self._loop_quality_gate,4),'loop_micro_align_ms':round(float(self._loop_micro_align_ms),2)})

    def _analyze_loop_slices(self):
        """Compute tiny source descriptors for eight musical slices.

        This runs only on a quantized capture event. It uses a sparse mono view so
        there is no full MIR/model pass in the realtime callback. Scores are
        normalized per capture and later used only to choose among safe neighbours.
        """
        import numpy as np
        if self.loop_len < 256:
            self._slice_energy[:] = .5; self._slice_transient[:] = .5; self._slice_brightness[:] = .5; self._slice_salience[:] = .5
            self._slice_map[:] = np.arange(8,dtype=np.int32); return
        mono=np.mean(self.loop[:self.loop_len],axis=1).astype(np.float32,copy=False)
        edges=np.linspace(0,self.loop_len,9,dtype=np.int64)
        e=[]; tr=[]; br=[]
        for i in range(8):
            a=int(edges[i]); b=max(a+8,int(edges[i+1])); z=mono[a:b:4]
            if z.size<4:
                e.append(0.0); tr.append(0.0); br.append(0.0); continue
            rms=float(np.sqrt(np.mean(z*z)+1e-12))
            d=np.diff(z)
            t=float(np.mean(np.abs(d)))
            # spectral-brightness proxy without an FFT: normalized high-frequency
            # finite-difference energy is robust enough for slice ranking.
            bright=float(np.sqrt(np.mean(d*d)+1e-12))/max(rms,1e-5)
            e.append(rms); tr.append(t); br.append(bright)
        def norm(v):
            a=np.asarray(v,dtype=np.float32); lo=float(np.min(a)); hi=float(np.max(a))
            if hi-lo<1e-7: return np.ones(8,dtype=np.float32)*.5
            return ((a-lo)/(hi-lo)).astype(np.float32)
        self._slice_energy=norm(e); self._slice_transient=norm(tr); self._slice_brightness=norm(br)
        self._slice_salience=np.clip(.38*self._slice_energy+.46*self._slice_transient+.16*self._slice_brightness,0,1).astype(np.float32)
        self._slice_map=np.arange(8,dtype=np.int32)

    def _choose_slice_map(self, mode: int, texture: float, transient: float):
        """Return an anchor-safe, source-aware eight-slice arrangement.

        0=identity, 1=groove, 2=fill, 3=call/response, 4=acceleration, 5=ghost.
        Downbeats 0 and 4 are never moved. Only musically local alternatives are
        considered, which prevents the slicer becoming random glitch generation.
        """
        import numpy as np
        mode=int(max(0,min(5,mode)))
        templates={
            0:[0,1,2,3,4,5,6,7],
            1:[0,1,2,1,4,5,6,5],
            2:[0,1,2,3,4,5,7,6],
            3:[0,1,2,3,4,1,6,3],
            4:[0,1,2,3,4,6,7,7],
            5:[0,1,1,3,4,5,5,7],
        }
        base=np.asarray(templates[mode],dtype=np.int32)
        if mode==0 or self.loop_len<256:
            return base
        percussive = texture >= -.08
        # Preserve structural anchors and only refine steps already intended to move.
        anchors={0,4}
        out=base.copy()
        for dst in range(8):
            if dst in anchors or int(base[dst])==dst:
                continue
            b=int(base[dst])
            candidates=[]
            for c in (b-1,b,b+1):
                c=max(0,min(7,c))
                if c in anchors and c!=b: continue
                # A rearrangement family must actually rearrange its marked cells.
                # This keeps the effect obvious while anchors/locality keep it musical.
                if c==dst: continue
                if c not in candidates: candidates.append(c)
            if not candidates: candidates=[b]
            prev=int(out[dst-1]) if dst>0 else 0
            best=b; best_score=-1e9
            for c in candidates:
                jump=abs(c-prev)/7.0
                if percussive:
                    score=.49*float(self._slice_transient[c])+.20*float(self._slice_brightness[c])+.13*float(self._slice_energy[c])+.18*float(self._slice_salience[c])-.16*jump
                else:
                    harmonic_sal=_clamp(.72*float(self._slice_energy[c])+.28*(1.0-float(self._slice_transient[c])),0,1)
                    score=.42*(1.0-float(self._slice_transient[c]))+.27*float(self._slice_energy[c])+.13*(1.0-float(self._slice_brightness[c]))+.18*harmonic_sal-.20*jump
                # transient parameter can deliberately bias towards punchier source
                score += .12*float(transient)*(float(self._slice_transient[c])-.5)
                if score>best_score: best_score=score; best=c
            out[dst]=best
        out[0]=0; out[4]=4
        return out.astype(np.int32)

    def set_ai_loop(self, buf, seq: int) -> bool:
        """v31.21: accept a synth loop only for the loop that is still playing (same capture, same length)."""
        import numpy as np
        try:
            b = np.asarray(buf, dtype=np.float32)
            if b.ndim == 1:
                b = np.stack([b, b], axis=1)
            if int(seq) != int(self.last_capture_seq) or b.shape[0] != int(self.loop_len) or self.loop_len < 8 or b.shape[1] < self.channels:
                return False
            self.loop_ai[:self.loop_len] = b[:self.loop_len, :self.channels]
            self.loop_ai_len = int(self.loop_len); self.loop_ai_seq = int(seq)
            return True
        except Exception:
            return False

    def _loop_block(self, n, controls=None):
        import numpy as np
        controls=controls or {}
        if self.loop_len < 8:
            return np.zeros((n,self.channels),dtype=np.float32)
        pos=(self.loop_pos + np.arange(n,dtype=np.int64)) % self.loop_len
        raw=self.loop[pos].copy()
        if self.loop_ai_len==self.loop_len and self.loop_ai_seq==self.last_capture_seq and self._ai_mix_eff>1e-3:
            _m=np.float32(self._ai_mix_eff)
            raw=raw*(np.float32(1.0)-_m)+self.loop_ai[pos]*_m
        # Original seam guard.
        seam_xf=min(max(32,int(self.sample_rate*.006)),max(32,self.loop_len//12))
        near=pos >= (self.loop_len-seam_xf)
        if np.any(near):
            ii=pos[near]-(self.loop_len-seam_xf)
            a=(ii.astype(np.float32)/float(max(1,seam_xf)))[:,None]
            # Equal-power seam keeps perceived level steadier than the old linear
            # blend while the candidate scorer above already minimizes mismatch.
            raw[near]=raw[near]*np.cos(a*math.pi*.5)+self.loop[ii]*np.sin(a*math.pi*.5)

        block_s=n/max(1.0,float(self.sample_rate))
        mix=self._smooth(float(self._slice_mix),_clamp(float(controls.get('b_slicer_mix',0.0)),0.0,1.0),block_s,.055,3.5)
        self._slice_mix=mix
        mode=int(round(_clamp(float(controls.get('b_slice_mode',0.0)),0.0,5.0)))
        texture=float(controls.get('b_texture',0.0) or 0.0); transient=float(controls.get('b_transient',0.0) or 0.0)
        if mode!=self._slice_mode or mix>.01:
            self._slice_mode=mode
            self._slice_map=self._choose_slice_map(mode,texture,transient)
            self._slice_motion=float(np.mean(self._slice_map!=np.arange(8,dtype=np.int32)))
        if mix<=.006 or mode==0:
            self.loop_pos=int((self.loop_pos+n)%self.loop_len)
            return raw

        # Eight equal musical cells. Remainder frames remain attached to the final
        # cell, but source addresses are clipped so no out-of-range read is possible.
        sl=max(16,self.loop_len//8)
        dst=np.minimum(7,(pos//sl).astype(np.int32))
        within=(pos%sl).astype(np.int64)
        src_slice=self._slice_map[dst]
        src=np.minimum(self.loop_len-1,src_slice.astype(np.int64)*sl+within)
        sliced=self.loop[src].copy()

        # 2 ms micro-crossfade at every rearranged slice boundary. Blend from the
        # previous mapped slice tail, eliminating clicks while keeping attacks crisp.
        xf=min(max(16,int(self.sample_rate*.002)),max(16,sl//8))
        edge=within<xf
        if np.any(edge):
            eidx=np.where(edge)[0]; d=dst[eidx]; changed=self._slice_map[d] != self._slice_map[(d-1)%8]
            if np.any(changed):
                ee=eidx[changed]; dd=d[changed]; ww=within[ee]
                prev_src=self._slice_map[(dd-1)%8].astype(np.int64)*sl + (sl-xf+ww)
                prev_src=np.minimum(self.loop_len-1,np.maximum(0,prev_src))
                a=(ww.astype(np.float32)/float(max(1,xf)))[:,None]
                sliced[ee]=self.loop[prev_src]*(1.0-a)+sliced[ee]*a

        # Equal-energy morph from ordinary slip-loop to rearranged loop.
        theta=float(mix)*math.pi*.5
        y=raw*math.cos(theta)+sliced*math.sin(theta)
        # Avoid the equal-power +3 dB midpoint when patterns happen to correlate.
        den=float(np.sqrt(np.mean(raw*raw)+1e-12)*np.sqrt(np.mean(sliced*sliced)+1e-12))
        corr=float(np.mean(raw*sliced))/den if den>1e-9 else 0.0
        if corr>.15:
            y/=math.sqrt(max(1.0,1.0+2.0*corr*math.sin(theta)*math.cos(theta)))
        self.loop_pos=int((self.loop_pos+n)%self.loop_len)
        return y.astype(np.float32,copy=False)

    def _deck_b_fx(self, bb, controls: dict, block_s: float):
        """Dedicated high-authority FX bus for Deck-B remix material only.

        Short beat-synchronous taps, transient lift and mid/side bloom make Deck B
        feel like a performance surface. The wet residual is RMS-bounded relative
        to Deck B itself, so a dramatic B layer cannot overload the shared master.
        """
        import numpy as np
        n=int(bb.shape[0])
        target=_clamp(float(controls.get('b_fx',0.0)),0.0,1.0)
        fx=self._smooth(float(self._b_fx_amount),target,block_s,.060,3.5); self._b_fx_amount=fx
        if fx<=.008 or n<=0:
            return bb
        bpm=_clamp(float(controls.get('bpm',100.0) or 100.0),55.0,190.0)
        slicer=_clamp(float(self._slice_mix),0,1); motion=_clamp(float(self._slice_motion),0,1)
        beat_frames=max(64,int(round(self.sample_rate*60.0/bpm)))
        d1=max(32,min(self._b_fx_delay_len-8,int(beat_frames*(.125 if slicer>.25 else .25))))
        d2=max(64,min(self._b_fx_delay_len-8,d1*2))
        idx=(self._b_fx_pos+np.arange(n,dtype=np.int64))%self._b_fx_delay_len
        r1=(idx-d1)%self._b_fx_delay_len; r2=(idx-d2)%self._b_fx_delay_len
        tap1=self._b_fx_delay[r1].copy(); tap2=self._b_fx_delay[r2].copy()
        if self.channels>=2:
            # Ping-pong character only on the wet bus; mono compatibility remains high.
            t=tap1.copy(); tap1[:,0]=t[:,1]; tap1[:,1]=t[:,0]
        self.b_fx_body_lp.set_coeffs('lowpass',self.sample_rate,1800.0,q=.66)
        body=self.b_fx_body_lp.process(bb)
        attack=bb-body
        wet=(.34+.20*motion)*tap1 + (.16+.10*slicer)*tap2 + attack*(.16+.18*motion)
        # Width grows on the remix bus but never uses hard anti-phase.
        if self.channels>=2:
            mid=.5*(wet[:,0]+wet[:,1]); side=.5*(wet[:,0]-wet[:,1])*(1.0+.55*fx)
            wet=wet.copy(); wet[:,0]=mid+side; wet[:,1]=mid-side
        wet=np.tanh(wet*(1.02+.22*fx)).astype(np.float32,copy=False)
        dry_rms=float(np.sqrt(np.mean(bb*bb)+1e-12)); wet_rms=float(np.sqrt(np.mean(wet*wet)+1e-12))
        cap=max(.004,dry_rms*(.24+.24*fx))
        if wet_rms>cap: wet*=cap/max(wet_rms,1e-8)
        # User asked for B to be more obviously effected: authority is intentionally
        # higher than the global garnish layer, while the cap above preserves quality.
        out=bb + wet*(.28+.46*fx)
        feedback=.18+.24*fx
        write=bb*(.16+.32*fx)+tap1*feedback
        write=np.tanh(write*.94).astype(np.float32,copy=False)
        self._b_fx_delay[idx]=write
        self._b_fx_pos=int((self._b_fx_pos+n)%self._b_fx_delay_len)
        return out.astype(np.float32,copy=False)

    @staticmethod
    def _smooth(cur: float, tgt: float, block_s: float, tau: float = 0.055, slew_per_s: float = 16.0) -> float:
        a = 1.0 - math.exp(-block_s/max(0.008,tau))
        nxt = cur + (tgt-cur)*a
        return cur + _clamp(nxt-cur, -slew_per_s*block_s, slew_per_s*block_s)

    def _deck(self, x, prefix: str, controls: dict, block_s: float):
        if self._rust is not None:
            # v31.15 RustCore: the whole channel strip (5 morphing biquads +
            # smoothed gain) runs as one native call; smoothed effective values
            # come back so telemetry stays truthful.
            import numpy as np
            y = np.ascontiguousarray(x, dtype=np.float32)
            if y is x:
                y = x.copy()
            try:
                vals = self._rust.deck(0 if prefix == 'a' else 1, y,
                                       float(controls.get(prefix+'_low_db', 0.0) or 0.0),
                                       float(controls.get(prefix+'_mid_db', 0.0) or 0.0),
                                       float(controls.get(prefix+'_high_db', 0.0) or 0.0),
                                       float(controls.get(prefix+'_filter', 0.0) or 0.0),
                                       float(controls.get(prefix+'_gain', 1.0) or 1.0))
                (self.cur[prefix+'_low_db'], self.cur[prefix+'_mid_db'], self.cur[prefix+'_high_db'],
                 self.cur[prefix+'_filter'], self.cur[prefix+'_gain']) = vals
                return y
            except Exception:
                pass
        low = self._smooth(float(self.cur[prefix+'_low_db']), _clamp(float(controls.get(prefix+'_low_db',0.0)), -8.0, 6.0), block_s, .075, 18.0)
        mid = self._smooth(float(self.cur[prefix+'_mid_db']), _clamp(float(controls.get(prefix+'_mid_db',0.0)), -8.0, 6.0), block_s, .075, 18.0)
        high = self._smooth(float(self.cur[prefix+'_high_db']), _clamp(float(controls.get(prefix+'_high_db',0.0)), -8.0, 6.0), block_s, .075, 18.0)
        filt = self._smooth(float(self.cur[prefix+'_filter']), _clamp(float(controls.get(prefix+'_filter',0.0)), -1.0, 1.0), block_s, .065, 3.2)
        gain = self._smooth(float(self.cur[prefix+'_gain']), _clamp(float(controls.get(prefix+'_gain',1.0)), 0.0, 1.20), block_s, .055, 3.0)
        self.cur[prefix+'_low_db']=low; self.cur[prefix+'_mid_db']=mid; self.cur[prefix+'_high_db']=high; self.cur[prefix+'_filter']=filt; self.cur[prefix+'_gain']=gain
        lowf,midf,highf,hp,lp = (self.a_low,self.a_mid,self.a_high,self.a_hp,self.a_lp) if prefix=='a' else (self.b_low,self.b_mid,self.b_high,self.b_hp,self.b_lp)
        lowf.set_coeffs('lowshelf',self.sample_rate,120.0,gain_db=low)
        midf.set_coeffs('peaking',self.sample_rate,1150.0,q=.78,gain_db=mid)
        highf.set_coeffs('highshelf',self.sample_rate,7600.0,gain_db=high)
        y=lowf.process(x); y=midf.process(y); y=highf.process(y)
        # Bipolar DJ filter: left = LP, right = HP, neutral = transparent endpoints.
        if filt >= 0:
            hp_hz = 20.0 * ((1200.0/20.0) ** (filt**1.35)); lp_hz = self.sample_rate*.46
        else:
            hp_hz = 20.0; lp_hz = (self.sample_rate*.46) * ((520.0/(self.sample_rate*.46)) ** ((-filt)**1.25))
        hp.set_coeffs('highpass',self.sample_rate,hp_hz,q=.707); lp.set_coeffs('lowpass',self.sample_rate,lp_hz,q=.707)
        y=hp.process(y); y=lp.process(y)
        return (y*gain).astype(x.dtype,copy=False)

    def _deck_b_remix_texture(self, bb, controls: dict, block_s: float):
        """Shape Deck B into a musically safer remix layer.

        texture > 0 favors drums/transients and removes low-end/vocal body;
        texture < 0 creates a quiet harmonic/atmospheric bed.  transient adds
        source-derived attack emphasis without synthesizing unrelated hats.
        """
        import numpy as np
        texture=self._smooth(float(self._b_texture),_clamp(float(controls.get('b_texture',0.0)),-1.0,1.0),block_s,.070,2.4)
        transient=self._smooth(float(self._b_transient),_clamp(float(controls.get('b_transient',0.0)),0.0,1.0),block_s,.060,2.8)
        self._b_texture=texture; self._b_transient=transient
        if abs(texture)<.015 and transient<.015:
            return bb
        if texture>=0.0:
            # Percussive ghost: progressively remove bass/body while retaining
            # cymbal/snare/kick attack information from the source itself.
            hp_hz=90.0 + 1050.0*(texture**1.35)
            lp_hz=min(self.sample_rate*.43, 12500.0-2600.0*texture)
        else:
            q=-texture
            # Harmonic bed: remove sub and top-edge, keep a restrained mid bed.
            hp_hz=70.0+90.0*q
            lp_hz=max(900.0, 7200.0*(1.0-q)+1700.0*q)
        self.b_focus_hp.set_coeffs('highpass',self.sample_rate,hp_hz,q=.707)
        self.b_focus_lp.set_coeffs('lowpass',self.sample_rate,lp_hz,q=.707)
        focused=self.b_focus_lp.process(self.b_focus_hp.process(bb))
        if transient>.015:
            # Attack extractor: subtract a smoothed low-passed body.  This behaves
            # like a crude source-derived percussion stem but makes no stem claim.
            self.b_transient_lp.set_coeffs('lowpass',self.sample_rate,1250.0+650.0*(1.0-texture if texture>0 else 0.0),q=.62)
            body=self.b_transient_lp.process(focused)
            attack=focused-body
            focused=focused*(1.0-.38*transient)+attack*(.62+.38*transient)
        rms=float(np.sqrt(np.mean(focused*focused)+1e-12))
        src=float(np.sqrt(np.mean(bb*bb)+1e-12))
        # Never let texture extraction become a hidden gain booster.
        if rms>src*1.12 and rms>1e-8:
            focused*=src*1.12/rms
        return focused.astype(np.float32,copy=False)

    def _read_out_ring(self, back_frames: int, n: int):
        """Contiguous n-frame read starting back_frames behind the program head.
        Same-thread ring, no lock needed.  Returns None if not resident."""
        import numpy as np
        ring=self._fxout_ring; L=int(ring.shape[0])
        back=int(back_frames)
        if back<n or back>min(self._fxout_abs,L)-1:
            return None
        start=(self._fxout_abs-back)%L
        if start+n<=L:
            return ring[start:start+n].copy()
        first=L-start
        return np.concatenate((ring[start:],ring[:n-first]),axis=0)

    def _apply_brake_fx(self, y, controls: dict):
        """v31.14 TIMEWEAVE — the DJ plays with time itself.

        The mixer archives its own output (the past the listener heard) and can
        read the engine's lookahead ring (the future the listener has NOT heard
        yet).  One sequence-triggered state machine renders five time gestures
        on the program bus while the real stream marches on untouched underneath:

          0 BRAKE        turntable power-off (past, resampled to zero)
          1 BACKSPIN     accelerating reverse rip (past)
          2 JUMPBACK     beat-exact replay of the previous phrase (arrangement edit)
          3 FLASHFORWARD a short vision of audio from N beats in the future —
                         the hook is foreshadowed before the song reaches it
          4 INTERLEAVE   call-and-response between NOW and the FUTURE in
                         alternating beat cells (present/+offset/present/…)

        Return to live is sample-exact on the landing beat; every path is
        vectorized and clamped, with graceful no-ops when material is missing.
        """
        import numpy as np
        n=int(y.shape[0])
        if n<=0:
            return y
        ring=self._fxout_ring; L=int(ring.shape[0])
        # Always archive the live program first so the effect can read up to "now".
        w0=self._fxout_abs % L
        if w0+n<=L:
            ring[w0:w0+n]=y
        else:
            first=L-w0
            ring[w0:]=y[:first]; ring[:n-first]=y[first:]
        self._fxout_abs += n

        sr=float(self.sample_rate)
        bpm=_clamp(float(controls.get('bpm',100.0) or 100.0),55.0,190.0)
        spb=sr*60.0/bpm     # samples per beat
        seq=int(controls.get('fx_brake_seq',0) or 0)
        if seq != self._last_brake_seq:
            self._last_brake_seq=seq
            mode=int(_clamp(float(controls.get('fx_brake_mode',0) or 0),0,4))
            beats=_clamp(float(controls.get('fx_brake_beats',1.0) or 1.0),0.25,8.0)
            ok = self._fxout_abs > int(sr*0.5)
            if ok and mode==2:
                off_b=_clamp(float(controls.get('fx_weave_offset_beats',4.0) or 4.0),1.0,16.0)
                off_f=int(off_b*spb)
                off_f=min(off_f,L-int(sr*0.25))
                if self._fxout_abs>off_f+int(sr*0.1):
                    self._weave_src_abs=self._fxout_abs-off_f
                else:
                    ok=False
            elif ok and mode in (3,4):
                fring=self._future_ring
                ok=False
                if fring is not None:
                    lead=int(fring.total)-int(self._fxout_abs)
                    margin=int(sr*0.40)+3*n
                    off_b=_clamp(float(controls.get('fx_weave_offset_beats',16.0) or 16.0),1.0,32.0)
                    off_f=int(off_b*spb)
                    if lead>margin+int(sr*0.5):
                        off_f=min(off_f,lead-margin)
                        self._weave_fut_abs=int(fring.total)-lead+off_f
                        self._weave_cell_beats=_clamp(float(controls.get('fx_weave_cell_beats',2.0) or 2.0),0.5,4.0)
                        self._weave_prev_mask=0.0
                        ok=True
            if ok:
                self._brake_mode=mode
                self._brake_len_s=(60.0/bpm)*beats
                self._brake_age_s=0.0
                self._brake_read_abs=float(self._fxout_abs-1)
                self._brake_active=True

        if not self._brake_active:
            if self._brake_refade>0:
                # Short guard fade-in after the effect so the return can never click.
                k=min(self._brake_refade,n)
                fade=np.linspace(1.0-self._brake_refade/96.0,1.0-(self._brake_refade-k)/96.0,k,dtype=np.float32)
                y[:k]*=np.clip(fade,0.0,1.0)[:,None]
                self._brake_refade-=k
            return y

        tt=self._brake_age_s + np.arange(n,dtype=np.float32)/sr
        u=np.clip(tt/max(1e-3,self._brake_len_s),0.0,1.0).astype(np.float32)
        mode=int(self._brake_mode)
        if mode<=1:
            if mode==0:
                rate=((1.0-u)**1.35).astype(np.float32)                    # 1 -> 0 wind-down
                gain=((1.0-0.22*u)*(1.0-u**6)).astype(np.float32)
            else:
                rate=(-(0.55+3.05*(u**1.25))).astype(np.float32)           # reverse, accelerating
                gain=(((1.0-u)**0.85)*0.92+0.04).astype(np.float32)
            pos=self._brake_read_abs + np.cumsum(rate,dtype=np.float64)
            lo=float(max(0,self._fxout_abs-L+2)); hi=float(self._fxout_abs-2)
            pos=np.clip(pos,lo,hi)
            i0=np.floor(pos).astype(np.int64); fr=(pos-i0).astype(np.float32)[:,None]
            a=ring[(i0)%L]; b=ring[(i0+1)%L]
            eff=(a*(1.0-fr)+b*fr)*gain[:,None]
            self._brake_read_abs=float(pos[-1])
        elif mode==2:
            back=self._fxout_abs-int(self._weave_src_abs)
            seg=self._read_out_ring(back,n)
            self._weave_src_abs+=n
            eff=seg if seg is not None else y.copy()
        else:
            fring=self._future_ring
            seg=None
            if fring is not None:
                back=int(fring.total)-int(self._weave_fut_abs)
                seg=fring.read_at(back,n)
            self._weave_fut_abs+=n
            if seg is None:
                eff=y.copy()
            else:
                if seg.shape[1]!=self.channels:
                    seg=np.repeat(seg[:,:1],self.channels,axis=1) if seg.shape[1]<self.channels else seg[:,:self.channels]
                eff=seg.astype(np.float32,copy=False)
                if mode==4:
                    # Alternating present/future beat cells with a ~2 ms smoothed mask.
                    ebeats=tt*(bpm/60.0)
                    parity=(np.floor(ebeats/max(0.5,self._weave_cell_beats)).astype(np.int64)%2).astype(np.float32)
                    kern=np.full(96,1.0/96.0,dtype=np.float32)
                    mpad=np.concatenate((np.full(95,np.float32(self._weave_prev_mask),dtype=np.float32),parity))
                    ms=np.convolve(mpad,kern,mode='valid').astype(np.float32)
                    self._weave_prev_mask=float(parity[-1])
                    eff=y*(1.0-ms[:,None])+eff*ms[:,None]
        if self._brake_age_s<=0.0:
            k=min(96,n)  # 2 ms live->effect crossfade at start
            xf=np.linspace(0.0,1.0,k,dtype=np.float32)[:,None]
            eff[:k]=y[:k]*(1.0-xf)+eff[:k]*xf
        self._brake_age_s=float(tt[-1])
        if u[-1]>=1.0:
            self._brake_active=False
            self._brake_refade=96
        return np.nan_to_num(eff,nan=0.0,posinf=0.0,neginf=0.0).astype(np.float32,copy=False)

    def _level_match_gain(self, kind: str, src, layer_nominal: float, amount: float, block_s: float, n: int, boost: float = 0.0):
        """v31.18: per-block gain ramp that puts an added layer at a musically sane level
        relative to the record itself.  Trackers are peak-hold followers: source band
        peak (1.5 s release), program peak (1.5 s), layer peak (0.6 s).  The target is
        the record's band peak times (0.35+0.55*amount) - the policy amount decides how
        present the layer is RELATIVE to the record - floored and capped by the program
        peak so silent or hat-less records get a soft, bounded layer instead of a
        fixed design level that is 7 dB off in either direction."""
        import numpy as np, math
        st=self._lm
        if kind=='kick':
            self.lm_low_lp.set_coeffs('lowpass',self.sample_rate,150.0,q=.707)
            band=self.lm_low_lp.process(np.ascontiguousarray(src,dtype=np.float32))
            ratio=0.15+0.70*_clamp(amount,0.0,1.0)+0.35*_clamp(boost,0.0,1.0); floor_abs=0.060; ceil_abs=0.55+0.15*_clamp(boost,0.0,1.0)
        else:
            self.lm_hf_hp.set_coeffs('highpass',self.sample_rate,6000.0,q=.707)
            band=self.lm_hf_hp.process(np.ascontiguousarray(src,dtype=np.float32))
            ratio=0.15+0.70*_clamp(amount,0.0,1.0)+0.35*_clamp(boost,0.0,1.0); floor_abs=0.018; ceil_abs=0.30+0.10*_clamp(boost,0.0,1.0)
        spk=float(np.max(np.abs(band))) if band.size else 0.0
        full=float(np.max(np.abs(src))) if src.size else 0.0
        rel_s=math.exp(-block_s/4.0); rel_l=math.exp(-block_s/0.6)   # slow reference: a 2-bar break must not yank the layer
        st[kind+'_src']=max(spk,st.get(kind+'_src',0.0)*rel_s)
        st[kind+'_full']=max(full,st.get(kind+'_full',0.0)*rel_s)
        st[kind+'_lay']=max(float(layer_nominal),1e-4)   # design peak of the layer at this amount (path-independent)
        # absolute floor / ceiling: no record reference (silence, intro) leaves a soft
        # but present layer instead of one that vanishes with the programme
        # the policy amount scales the target itself: a layer the agent (or the beat
        # confidence) has pulled down must stay down, whatever the record does
        tgt=max(ratio*st[kind+'_src'],floor_abs*(0.10+0.90*_clamp(amount,0.0,1.0)))
        tgt=min(tgt,ceil_abs)
        g_raw=_clamp(tgt/max(st[kind+'_lay'],1e-4),0.25,2.5)
        g_prev=float(st.get(kind+'_g',1.0))
        a=1.0-math.exp(-block_s/0.4)
        g=g_prev+(g_raw-g_prev)*a
        st[kind+'_g']=g
        if abs(g-g_prev)<1e-6:
            return np.float32(g)
        return np.linspace(g_prev,g,n,dtype=np.float32)

    def _performance_fx(self, y, controls: dict, block_s: float):
        """Beat-aware professional FX layer used by transition choreography.

        The layer is deliberately conservative: source-derived drum excitation, a
        filtered noise riser, optional double-time top percussion, and a short
        one-shot impact. Everything is vectorized and bounded before the existing
        LowEndIntegrity master.
        """
        import numpy as np
        n=int(y.shape[0])
        if n <= 0:
            return y
        riser=self._smooth(float(self.cur['noise_riser']),_clamp(float(controls.get('noise_riser',0.0) or 0.0),0.0,1.0),block_s,.090,2.6)
        drive=self._smooth(float(self.cur['drum_drive']),_clamp(float(controls.get('drum_drive',0.0) or 0.0),0.0,1.0),block_s,.080,2.8)
        double=self._smooth(float(self.cur['doubletime']),_clamp(float(controls.get('doubletime',0.0) or 0.0),0.0,1.0),block_s,.115,2.1)
        reverse=self._smooth(float(self.cur['fx_reverse_swell']),_clamp(float(controls.get('fx_reverse_swell',0.0) or 0.0),0.0,1.0),block_s,.120,2.2)
        rush=self._smooth(float(self.cur['fx_snare_rush']),_clamp(float(controls.get('fx_snare_rush',0.0) or 0.0),0.0,1.0),block_s,.090,2.8)
        impact_strength=self._smooth(float(self.cur['impact_strength']),_clamp(float(controls.get('impact_strength',0.0) or 0.0),0.0,1.0),block_s,.060,8.0)
        self.cur['noise_riser']=riser; self.cur['drum_drive']=drive; self.cur['doubletime']=double; self.cur['fx_reverse_swell']=reverse; self.cur['fx_snare_rush']=rush; self.cur['impact_strength']=impact_strength

        # v31.13 GridLock: advance the shared beat-phase grid for this block.
        bpm_grid=_clamp(float(controls.get('bpm',100.0) or 100.0),55.0,190.0)
        grid_bps=bpm_grid/60.0
        grid_conf=_clamp(float(controls.get('grid_conf',0.0) or 0.0),0.0,1.0)
        grid_anchor=float(controls.get('grid_anchor_t',0.0) or 0.0)
        grid_base=float(self._grid_beats)
        anchor_frame=float(controls.get('grid_anchor_frame',0.0) or 0.0)
        frame_end=float(controls.get('grid_frame_end',0.0) or 0.0)
        frame_lock=False
        # v31.18 groove clarity: rhythmic layers are scaled by how much of the
        # record's own transient content actually sits on a 16th grid.
        clar_raw=controls.get('fx_groove_clarity',None)
        clar=_clamp(float(clar_raw),0.0,1.0) if clar_raw is not None else 1.0
        self._clar_src=clar
        self._clar_gate=0.10+0.90*(clar**1.3)
        self._clar_post=0.20+0.80*clar     # second factor on the rendered layer itself (design gains have a constant term)
        # v31.19 DrumMind: when the AI drummer has a kit and a pattern, the synthesized
        # re-drum kick and ghost hats step aside and the sample layer plays instead.
        _dseq=int(controls.get('drum_pat_seq',0) or 0)
        if _dseq!=self.drums.pat_seq:
            self.drums.set_pattern(controls.get('drum_vel'),controls.get('drum_mt'),_dseq)
        self._drum_sample_mode=bool(float(controls.get('drum_mode',0.0) or 0.0)>=0.5 and self.drums.kit is not None and self.drums.active())
        if anchor_frame>0.0 and frame_end>0.0:
            # v31.18 frame-accurate grid: the agent anchors a DOWNBEAT at an absolute
            # render-frame index measured on the audible ring; this block starts at
            # frame_end-n.  No wall clock anywhere, so scheduling jitter cannot move
            # the grid.  The pull is on the bar phase (mod 4 beats) so bar-level
            # patterns and fills share the agent's downbeat; a large error snaps.
            frame_start=frame_end-float(n)
            self._frame_start=int(frame_start)
            target=(frame_start-anchor_frame)/float(self.sample_rate)*grid_bps
            err=((target-grid_base+2.0)%4.0)-2.0
            if abs(err)>0.5:
                grid_base+=err
            else:
                grid_base+=err*(0.25+0.35*grid_conf)
            self._grid_locked=True
            frame_lock=True
        elif grid_anchor>0.0:
            now_ref=float(controls.get('grid_now_t',0.0) or 0.0)
            if now_ref<=0.0:
                now_ref=time.monotonic()
            lead=_clamp(float(controls.get('grid_lead_ms',0.0) or 0.0),0.0,250.0)/1000.0
            target=((now_ref-grid_anchor)+lead)*grid_bps
            err=((target-grid_base+0.5)%1.0)-0.5
            grid_base+=err*(0.06+0.16*grid_conf)   # gentle, jump-free phase pull
            self._grid_locked=True
        else:
            self._grid_locked=False
        self._grid_conf=grid_conf
        self._grid_frame_lock=frame_lock
        self._grid_beats=grid_base+n/float(self.sample_rate)*grid_bps
        grid_beats_arr=(grid_base+np.arange(n,dtype=np.float64)/float(self.sample_rate)*grid_bps)
        swing=_clamp(float(controls.get('fx_swing',0.0) or 0.0),0.0,0.6)

        out=y.astype(np.float32,copy=True)
        # Extract presence/transient band from the live song itself. Soft clipping the
        # parallel band adds density without touching vocal/sub fundamentals directly.
        if drive > 1e-4:
            self.drum_hp.set_coeffs('highpass',self.sample_rate,620.0,q=.707)
            self.drum_lp.set_coeffs('lowpass',self.sample_rate,9800.0,q=.707)
            band=self.drum_lp.process(self.drum_hp.process(y))
            excited=np.tanh(band*(1.55+1.25*drive)).astype(np.float32,copy=False)
            out += excited*(0.030+0.050*drive)*drive

        # v31.8.3 SOURCE-AWARE TOP PERCUSSION
        # --------------------------------------
        # Earlier versions synthesized every hat from the same white-noise pulse.
        # That was rhythmically correct but timbrally disconnected from the record.
        # We now gate the *song's own* 4-13 kHz transient band and use a small,
        # coherent noise layer only when the source has too little top percussion.
        # v31.13: ghost hats are gated by beat-tracking confidence when the grid
        # is agent-locked — a rhythally unsure grid must not hammer 16th accents.
        double_eff=double*((0.20+0.80*(grid_conf**1.35)) if self._grid_locked else 1.0)*float(self._clar_gate)*(0.0 if self._drum_sample_mode else 1.0)
        rust_hats_done=False
        if self._rust is not None and double_eff > 1e-4:
            # v31.16 Hats 2.0 (native): GrooVAE-style humanized, style-aware,
            # source-density-thinned, click-free hats in one call.
            try:
                hat_add,hat_dens=self._rust.hats(np.ascontiguousarray(y,dtype=np.float32),
                    grid_base=grid_base,bps=grid_bps,swing=swing,level=double_eff,
                    style=int(_clamp(float(controls.get('fx_hat_style',0.0) or 0.0),0,3)),
                    var=_clamp(float(controls.get('fx_hat_var',0.5) or 0.0),0.0,1.0),
                    fill=_clamp(float(controls.get('fx_hat_fill',0.5) or 0.0),0.0,1.0))
                lm_h=self._level_match_gain('hat',y,(0.010+0.013*double_eff)*double_eff*3.0,double_eff,block_s,n)
                if np.ndim(lm_h)==1:
                    out+=hat_add*(lm_h*np.float32(self._clar_post))[:,None]
                else:
                    out+=hat_add*np.float32(lm_h*self._clar_post)
                self._hat_density=float(hat_dens)
                double=double_eff
                rust_hats_done=True
                self._hat_was_active=True
            except Exception:
                rust_hats_done=False
        if (not rust_hats_done) and double_eff > 1e-4:
            if not self._hat_was_active:
                self._hat_variant=int(self._fx_rng.integers(0,4))
            self._hat_was_active=True
            pos16=grid_beats_arr*4.0
            step=np.floor(pos16).astype(np.int64)
            phase=(pos16-step).astype(np.float32)
            if swing>1e-3:
                # Delay every odd 16th by the swing amount (shuffle feel).
                odd=(np.mod(step,2)==1)
                ph_shift=np.where(odd,phase-np.float32(swing),phase)
                phase=np.where(ph_shift>=0.0,ph_shift,np.float32(2.0)).astype(np.float32)  # 2.0 => enveloped to silence
            double=double_eff
            patterns=(
                np.array([1.00,.12,.62,.08,.86,.18,.48,.12, 1.00,.10,.72,.16,.82,.12,.56,.20],dtype=np.float32),
                np.array([1.00,.08,.38,.18,.92,.12,.64,.08, .78,.22,.52,.10,1.00,.10,.44,.26],dtype=np.float32),
                np.array([.86,.16,.52,.08,1.00,.10,.34,.24, .90,.08,.68,.14,.76,.28,.46,.10],dtype=np.float32),
                np.array([1.00,.10,.46,.24,.78,.08,.70,.12, .92,.20,.36,.08,1.00,.12,.58,.18],dtype=np.float32),
            )
            pat=patterns[self._hat_variant]
            velocity=pat[np.mod(step,pat.size)]
            decay=10.0+7.0*velocity+2.0*float(self._hat_variant)
            pulse=np.exp(-phase*decay).astype(np.float32)*velocity
            # Pull actual metallic/transient texture from the current record.
            src_hp=3900.0+350.0*self._hat_variant
            src_lp=11800.0+450.0*(self._hat_variant&1)
            self.hat_src_hp.set_coeffs('highpass',self.sample_rate,src_hp,q=.72)
            self.hat_src_lp.set_coeffs('lowpass',self.sample_rate,min(src_lp,self.sample_rate*.43),q=.68)
            src_top=self.hat_src_lp.process(self.hat_src_hp.process(y))
            src_rms=float(np.sqrt(np.mean(src_top*src_top)+1e-12))
            # Coherent mono-ish air support avoids headphone fizz from fully
            # independent L/R white-noise hats.
            mono_noise=self._fx_rng.standard_normal(n).astype(np.float32)
            noise=np.repeat(mono_noise[:,None],self.channels,axis=1)
            if self.channels>=2:
                side=self._fx_rng.standard_normal(n).astype(np.float32)
                noise[:,0]+=0.10*side; noise[:,1]-=0.10*side
            self.top_hp.set_coeffs('highpass',self.sample_rate,5100.0+300.0*self._hat_variant,q=.707)
            self.top_lp.set_coeffs('lowpass',self.sample_rate,min(13200.0,self.sample_rate*.44),q=.68)
            noise_top=self.top_lp.process(self.top_hp.process(noise))
            # More source when the record already contains useful hats; more air
            # support for sparse/old recordings. Soft normalization prevents one
            # bright master from creating over-loud ghost hats.
            source_gain=_clamp(0.72/(src_rms*32.0+0.72),0.34,0.78)
            texture=src_top*source_gain + noise_top*(0.17+0.08*(1.0-source_gain))
            texture=np.tanh(texture*1.35).astype(np.float32,copy=False)
            amp=(pulse*(0.010+0.013*double)*double)[:,None]
            out += texture*amp
        else:
            self._hat_was_active=False

        # v31.8.3 SOURCE-AWARE RISER
        # -------------------------
        # A riser event gets one of four stable spectral fingerprints. It blends a
        # coherent filtered-air bed with the current song's upper-mid/high texture,
        # so successive transitions do not sound like the same stock white-noise FX.
        if riser > 1e-4:
            if not self._riser_was_active:
                self._riser_variant=int(self._fx_rng.integers(0,4))
                self._riser_age_sec=0.0
            self._riser_was_active=True
            self._riser_age_sec += block_s
            rv=self._riser_variant
            starts=(420.0,680.0,980.0,520.0); spans=(5200.0,6900.0,4300.0,7600.0)
            tops=(10400.0,12900.0,9200.0,11800.0)
            hp_hz=starts[rv]+spans[rv]*(riser**(1.12+0.09*rv))
            lp_hz=min(tops[rv],self.sample_rate*.42)
            # Coherent air layer: common noise plus a restrained side component.
            mono_noise=self._fx_rng.standard_normal(n).astype(np.float32)
            noise=np.repeat(mono_noise[:,None],self.channels,axis=1)
            if self.channels>=2:
                side=self._fx_rng.standard_normal(n).astype(np.float32)
                side_amt=(.07,.12,.05,.10)[rv]
                noise[:,0]+=side_amt*side; noise[:,1]-=side_amt*side
            self.riser_hp.set_coeffs('highpass',self.sample_rate,hp_hz,q=(.62,.72,.56,.68)[rv])
            self.riser_lp.set_coeffs('lowpass',self.sample_rate,lp_hz,q=(.66,.70,.60,.72)[rv])
            air=self.riser_lp.process(self.riser_hp.process(noise))
            # Source fingerprint follows a broader band than the air bed. The live
            # audio is never delayed/reordered, only used as a quiet parallel grain.
            src_hp=(1500.0,2300.0,1100.0,3100.0)[rv]
            src_lp=(8200.0,11800.0,6800.0,12600.0)[rv]
            self.riser_src_hp.set_coeffs('highpass',self.sample_rate,src_hp,q=.68)
            self.riser_src_lp.set_coeffs('lowpass',self.sample_rate,min(src_lp,self.sample_rate*.43),q=.66)
            src=self.riser_src_lp.process(self.riser_src_hp.process(y))
            srms=float(np.sqrt(np.mean(src*src)+1e-12))
            srcn=src*min(2.2,0.055/max(srms,1e-4))
            # Slow event-specific breathing keeps a 4-bar rise alive without an LFO
            # that sounds detached from the song.
            tt=self._riser_age_sec + np.arange(n,dtype=np.float32)/float(self.sample_rate)
            motion=(0.90+0.10*np.sin(2.0*np.pi*((.17,.11,.23,.14)[rv])*tt + rv*1.31)).astype(np.float32)
            blend=(air*(.62,.52,.70,.46)[rv] + srcn*(.38,.48,.30,.54)[rv])
            amp=(0.009+0.024*riser)*(riser**1.18)
            out += blend*(motion*amp)[:,None]
        else:
            self._riser_was_active=False
            self._riser_age_sec=0.0

        # Source-aware reverse swell: darker live-song body + restrained air. This
        # replaces the old fully synthetic reverse-cymbal illusion while remaining
        # callback-safe and file-free.
        if reverse > 1e-4:
            if not self._reverse_was_active:
                self._reverse_variant=int(self._fx_rng.integers(0,3))
                self._reverse_age_sec=0.0
            self._reverse_was_active=True
            self._reverse_age_sec += block_s
            rv=self._reverse_variant
            mono_noise=self._fx_rng.standard_normal(n).astype(np.float32)
            noise=np.repeat(mono_noise[:,None],self.channels,axis=1)
            self.reverse_hp.set_coeffs('highpass',self.sample_rate,(240.0,420.0,680.0)[rv]+900.0*(reverse**1.1),q=.62)
            self.reverse_lp.set_coeffs('lowpass',self.sample_rate,min((6900.0,9800.0,7600.0)[rv]+2500.0*reverse,self.sample_rate*.42),q=.66)
            air=self.reverse_lp.process(self.reverse_hp.process(noise))
            self.reverse_src_hp.set_coeffs('highpass',self.sample_rate,(420.0,780.0,1100.0)[rv],q=.64)
            self.reverse_src_lp.set_coeffs('lowpass',self.sample_rate,min((5200.0,7600.0,9200.0)[rv],self.sample_rate*.42),q=.66)
            src=self.reverse_src_lp.process(self.reverse_src_hp.process(y))
            srms=float(np.sqrt(np.mean(src*src)+1e-12)); srcn=src*min(1.8,0.045/max(srms,1e-4))
            wash=air*(.50,.42,.58)[rv]+srcn*(.50,.58,.42)[rv]
            if self.channels>=2:
                mid=.5*(wash[:,0]+wash[:,1]); side=.5*(wash[:,0]-wash[:,1])
                wash[:,0]=mid+side*1.18; wash[:,1]=mid-side*1.18
            out += wash*(0.006+0.018*reverse)*(reverse**1.15)
        else:
            self._reverse_was_active=False
            self._reverse_age_sec=0.0

        # v31.8.3 SOURCE-AWARE DRUM/SNARE RUSH
        # The fill borrows body from the live record and varies a 16-step velocity
        # pattern per event. Noise is only the stick/air component, not the whole hit.
        rush_accel=self._smooth(float(self.cur['fx_rush_accel']),_clamp(float(controls.get('fx_rush_accel',0.0) or 0.0),0.0,1.0),block_s,.10,2.5)
        self.cur['fx_rush_accel']=rush_accel
        if rush > 1e-4:
            bpm=_clamp(float(controls.get('bpm',100.0) or 100.0),55.0,190.0)
            if not self._rush_was_active:
                self._rush_variant=int(self._fx_rng.integers(0,4))
            self._rush_was_active=True
            subdiv=2.0+2.0*rush
            if rush_accel > 1e-3:
                # v31.11 build-grammar ladder: onset rate climbs the metric grid
                # (1/4 -> 1/8 -> 1/16 -> 1/32) exactly like a produced EDM build,
                # instead of a static roll density.
                subdiv=float(2.0**math.floor(rush_accel*3.999))
            posr=grid_beats_arr*subdiv
            step=np.floor(posr).astype(np.int64); ph=(posr-step).astype(np.float32)
            patterns=(
                np.array([1,.32,.66,.24,.88,.38,.72,.28,1,.42,.62,.30,.90,.36,.78,.48],dtype=np.float32),
                np.array([1,.20,.54,.34,.82,.28,.70,.42,.94,.24,.60,.38,1,.30,.74,.50],dtype=np.float32),
                np.array([.88,.36,.70,.26,1,.32,.58,.46,.92,.28,.76,.34,.84,.52,.64,.40],dtype=np.float32),
                np.array([1,.28,.62,.44,.78,.22,.86,.36,.94,.48,.56,.30,1,.26,.72,.54],dtype=np.float32),
            )
            velocity=patterns[self._rush_variant][np.mod(step,16)]
            burst=np.exp(-ph*(13.0+12.0*rush+3.0*velocity)).astype(np.float32)*velocity
            self.rush_src_hp.set_coeffs('highpass',self.sample_rate,850.0+260.0*self._rush_variant,q=.68)
            self.rush_src_lp.set_coeffs('lowpass',self.sample_rate,min(7800.0+700.0*self._rush_variant,self.sample_rate*.42),q=.66)
            src=self.rush_src_lp.process(self.rush_src_hp.process(y))
            srms=float(np.sqrt(np.mean(src*src)+1e-12)); srcn=src*min(2.0,0.060/max(srms,1e-4))
            mono_noise=self._fx_rng.standard_normal(n).astype(np.float32)
            noise=np.repeat(mono_noise[:,None],self.channels,axis=1)
            self.rush_hp.set_coeffs('highpass',self.sample_rate,1800.0+450.0*self._rush_variant,q=.68)
            self.rush_lp.set_coeffs('lowpass',self.sample_rate,min(9800.0+550.0*self._rush_variant,self.sample_rate*.43),q=.65)
            stick=self.rush_lp.process(self.rush_hp.process(noise))
            sn=srcn*.68+stick*.32
            sn*= (burst*(0.010+0.015*rush)*rush)[:,None]
            out += sn
        else:
            self._rush_was_active=False

        # v31.12 FULLREMIX RE-DRUM
        # ------------------------
        # The remix literature's core move: play a NEW drum pattern under the
        # original.  A pitch-swept sine kick with click transient plus a band
        # noise clap follow one of four 16-step grids; each kick momentarily
        # ducks the program (the "pump keyed from the kick" production staple),
        # so the groove glues instead of stacking.  Fully vectorized, bounded,
        # and inactive at zero cost when the level is zero.
        redrum=self._smooth(float(self.cur['fx_redrum']),_clamp(float(controls.get('fx_redrum',0.0) or 0.0),0.0,1.0),block_s,.095,2.4)
        self.cur['fx_redrum']=redrum
        rd_pattern=int(_clamp(float(controls.get('fx_redrum_pattern',0.0) or 0.0),0,3))
        # v31.13: the remix kick follows the locked grid and eases off when the
        # beat tracker is unsure — a confidently wrong kick is the worst artifact.
        redrum_eff=redrum*((0.35+0.65*grid_conf) if self._grid_locked else 1.0)*float(self._clar_gate)*(0.0 if self._drum_sample_mode else 1.0)
        # v31.15 RustCore: level smoothing for the key-following synth layers is
        # shared by both paths; the whole groove (redrum + keybass + stab, incl.
        # the kick-keyed sidechain) then renders in ONE native call when the
        # Rust kernels are loaded — no numpy temporaries in the callback.
        bass_syn=self._smooth(float(self.cur.get('fx_bass_synth',0.0)),_clamp(float(controls.get('fx_bass_synth',0.0) or 0.0),0.0,0.9),block_s,.10,2.2)
        self.cur['fx_bass_synth']=bass_syn
        bass_conf=_clamp(float(controls.get('fx_bass_conf',0.0) or 0.0),0.0,1.0)
        bass_eff=bass_syn*((0.20+0.80*bass_conf) if self._grid_locked else 1.0)*float(self._clar_gate)
        stab=self._smooth(float(self.cur.get('fx_stab',0.0)),_clamp(float(controls.get('fx_stab',0.0) or 0.0),0.0,0.8),block_s,.11,2.0)
        self.cur['fx_stab']=stab
        stab_eff=stab*((0.15+0.85*bass_conf) if self._grid_locked else 1.0)*float(self._clar_gate)
        rust_groove_done=False
        self._lm_k_cur=None
        if self._rust is not None and (redrum_eff>1e-4 or bass_eff>1e-4 or stab_eff>1e-4):
            try:
                mul_r,addm_r,adds_r=self._rust.groove(n,grid_base=grid_base,bps=grid_bps,swing=swing,
                    redrum=redrum_eff,rd_pattern=rd_pattern,
                    rd_var=_clamp(float(controls.get('fx_redrum_var',0.0) or 0.0),0.0,1.0),
                    rd_fill=_clamp(float(controls.get('fx_redrum_fill',0.0) or 0.0),0.0,1.0),
                    bass=bass_eff,bass_root=int(_clamp(float(controls.get('fx_bass_root',9.0) or 9.0),0,11)),
                    bass_pattern=int(_clamp(float(controls.get('fx_bass_pattern',0.0) or 0.0),0,3)),
                    stab=stab_eff,stab_pattern=int(_clamp(float(controls.get('fx_stab_pattern',0.0) or 0.0),0,1)))
                out*=mul_r[:,None]
                lm_k=self._level_match_gain('kick',y,0.055+0.085*redrum_eff,redrum_eff,block_s,n)
                out+=(addm_r*lm_k*np.float32(self._clar_post))[:,None]
                out+=adds_r*np.float32(self._clar_post)
                if redrum_eff>1e-4: redrum=redrum_eff
                rust_groove_done=True
            except Exception:
                rust_groove_done=False
        if (not rust_groove_done) and redrum_eff > 1e-4:
            redrum=redrum_eff
            rate_hz=grid_bps*4.0
            pos16=grid_beats_arr*4.0
            stepi=np.floor(pos16).astype(np.int64)
            ph=(pos16-stepi).astype(np.float32)
            if swing>1e-3:
                oddr=(np.mod(stepi,2)==1)
                phs=np.where(oddr,ph-np.float32(swing),ph)
                ph=np.where(phs>=0.0,phs,np.float32(4.0)).astype(np.float32)  # 4.0 => fully decayed
            tt=ph/np.float32(rate_hz)                       # seconds into current 16th step
            RD_KICKS=(
                np.array([1,0,0,0, 1,0,0,0, 1,0,0,0, 1,0,0,.30],dtype=np.float32),   # FLOOR
                np.array([1,0,0,0, 0,0,0,0, 0,0,.55,0, 0,0,0,0],dtype=np.float32),   # HALF
                np.array([1,0,0,0, 0,0,0,.65, 0,0,1,0, 0,.45,0,0],dtype=np.float32), # BREAK
                np.array([0,0,1,0, 0,0,1,0, 0,0,1,0, 0,0,1,0],dtype=np.float32),     # UPBEAT
            )
            RD_CLAPS=(
                np.array([0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0],dtype=np.float32),
                np.array([0,0,0,0, 0,0,0,0, 1,0,0,0, 0,0,0,0],dtype=np.float32),
                np.array([0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,.35],dtype=np.float32),
                np.array([0,0,0,0, 1,0,0,0, 0,0,0,0, 1,0,0,0],dtype=np.float32),
            )
            def _kc_vel(steps):
                _inb=np.mod(steps,16); _bar=steps//16
                _kv=RD_KICKS[rd_pattern][_inb].copy(); _cv=RD_CLAPS[rd_pattern][_inb].copy()
                _var=_clamp(float(controls.get('fx_redrum_var',0.0) or 0.0),0.0,1.0)
                _fill=_clamp(float(controls.get('fx_redrum_fill',0.0) or 0.0),0.0,1.0)
                if _var>1e-3:
                    _h=(((_bar*np.int64(2654435761))+(_inb*np.int64(40503)))&np.int64(0xFFFFF)).astype(np.float32)/np.float32(1048575.0)
                    _kv=_kv*(1.0-0.20*_var*(_h-0.5)*2.0)
                    _kv=np.clip(_kv+((_h>(0.94-0.08*_var))&(_kv<0.05)).astype(np.float32)*(0.28*_var),0.0,1.15)
                    _cv=_cv*(1.0-0.6*_var*((_h>0.86)&(_h<0.90)).astype(np.float32))
                if _fill>1e-3:
                    _b4=np.mod(_bar,4); _b8=np.mod(_bar,8); _ft=np.mod(_bar*np.int64(7)+np.int64(3),np.int64(3))
                    _fz=(_b4==3)&(_inb>=12); _bz=(_b8==7)&(_inb>=8)
                    _fv=np.where(_ft==0,np.float32(0.62),np.where(_ft==1,((_inb.astype(np.float32)-11.0)*0.16+0.34),np.float32(0.0))).astype(np.float32)
                    _kv=np.where(_fz,_kv*(1.0-_fill)+_fv*_fill,_kv)
                    _kv=np.where(_bz&(_ft!=2),np.maximum(_kv,(0.30+0.05*_inb.astype(np.float32))*_fill*0.8),_kv)
                    _cv=np.where(_fz&(_ft==1),np.maximum(_cv,np.float32(0.55)*_fill),_cv)
                return _kv.astype(np.float32),_cv.astype(np.float32)
            _z=lambda st: np.zeros(len(st),dtype=np.float32)
            tt,kv,_=_voice_lookback(pos16,rate_hz,lambda st:(_kc_vel(st)[0],_z(st)),swing)
            ttc,cv,_=_voice_lookback(pos16,rate_hz,lambda st:(_kc_vel(st)[1],_z(st)),swing)
            tt=np.where(tt<0,np.float32(4.0),tt); ttc=np.where(ttc<0,np.float32(4.0),ttc)
            # Kick: exponential 139->47 Hz pitch drop (closed-form phase integral).
            f_end=47.0; f_span=92.0; tau=.020
            kphase=2.0*np.pi*(f_end*tt + f_span*tau*(1.0-np.exp(-tt/tau)))
            kenv=(np.exp(-tt/.115)*kv*np.minimum(tt/np.float32(0.0015),1.0)).astype(np.float32)
            kick=np.sin(kphase).astype(np.float32)*kenv
            click_noise=self._fx_rng.standard_normal((n,self.channels)).astype(np.float32)
            self.redrum_click_hp.set_coeffs('highpass',self.sample_rate,2400.0,q=.707)
            click=self.redrum_click_hp.process(click_noise)*((kenv**3)*(0.007+0.011*redrum))[:,None]
            cenv=(np.exp(-ttc/.055)*cv*np.minimum(ttc/np.float32(0.001),1.0)).astype(np.float32)
            cnoise=self._fx_rng.standard_normal((n,self.channels)).astype(np.float32)
            self.redrum_clap_hp.set_coeffs('highpass',self.sample_rate,950.0,q=.707)
            self.redrum_clap_lp.set_coeffs('lowpass',self.sample_rate,min(3600.0,self.sample_rate*.42),q=.707)
            clap=self.redrum_clap_lp.process(self.redrum_clap_hp.process(cnoise))*(cenv*(0.011+0.019*redrum))[:,None]
            # Kick-keyed program glue duck: the groove pushes the song, it does
            # not just sit on top of it.
            out*=(1.0-(0.26*redrum)*kenv)[:,None]
            layer=kick[:,None]*(0.055+0.085*redrum) + click + clap
            lm_k=self._level_match_gain('kick',y,0.055+0.085*redrum_eff,redrum_eff,block_s,n)
            self._lm_k_cur=lm_k
            if np.ndim(lm_k)==1:
                out+=layer*(lm_k*np.float32(self._clar_post))[:,None]
            else:
                out+=layer*np.float32(lm_k*self._clar_post)
            kenv_block=kenv
        else:
            kenv_block=None

        # v31.14 KEYBASS — a synthesized bassline in the detected key.
        # A remix does not just decorate the source: it OWNS the low end.  The
        # agent strips the original floor (fx_bass_cut) and this synth replaces
        # it with a new root/fifth/octave bassline (no third — key-mode safe),
        # sidechain-ducked from the redrum kick, gated by key confidence.
        # (Level smoothing hoisted above; rendered natively when RustCore is live.)
        if (not rust_groove_done) and bass_eff>1e-4:
            root_pc=int(_clamp(float(controls.get('fx_bass_root',9.0) or 9.0),0,11))
            bpat=int(_clamp(float(controls.get('fx_bass_pattern',0.0) or 0.0),0,3))
            pos16b=grid_beats_arr*4.0
            BASS_V=(
                np.array([0,0,.95,0, 0,0,.9,0, 0,0,.95,0, 0,0,.9,0],dtype=np.float32),     # PULSE (offbeat 8ths)
                np.array([1,0,0,.55, 0,0,.8,0, .9,0,0,.55, 0,0,.75,0],dtype=np.float32),   # DRIVE (syncopated)
                np.array([1,0,0,0, 0,0,0,0, .85,0,0,0, 0,0,0,0],dtype=np.float32),         # SUB (long roots)
                np.array([.95,0,.7,0, .95,0,.7,0, .95,0,.7,0, .95,0,.7,0],dtype=np.float32),# OCT (octave 8ths)
            )
            BASS_N=(
                np.array([0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0],dtype=np.float32),
                np.array([0,0,0,0, 0,0,0,0, 0,0,0,7, 0,0,0,0],dtype=np.float32),
                np.array([0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0],dtype=np.float32),
                np.array([0,0,12,0, 0,0,12,0, 0,0,12,0, 0,0,12,0],dtype=np.float32),
            )
            def _bass_vel(steps):
                _inb=np.mod(steps,16); _bar=steps//16
                _vb=BASS_V[bpat][_inb].copy(); _nb=BASS_N[bpat][_inb].copy()
                _hb=(((_bar*np.int64(97561))+(_inb*np.int64(6151)))&np.int64(0xFFFFF)).astype(np.float32)/np.float32(1048575.0)
                _nb=np.where((_hb>0.86)&(_vb>0.1)&(_nb==0),np.float32(7.0),_nb)
                _nb=np.where((_hb<0.06)&(_vb>0.1),np.float32(12.0),_nb)
                _vb=_vb*(1.0-0.12*(_hb-0.5)*2.0)
                return _vb.astype(np.float32),_nb.astype(np.float32)
            ttb,vb,nb=_voice_lookback(pos16b,grid_bps*4.0,_bass_vel,swing)
            ttb=np.where(ttb<0,np.float32(9.0),ttb)
            f0=np.float32(32.703)*np.float32(2.0)**(np.float32(root_pc)/12.0)
            if f0<38.0: f0=f0*2.0
            fb=f0*np.float32(2.0)**(nb/12.0)
            gate=(0.42,0.24,0.60,0.20)[bpat]
            envb=(np.exp(-ttb/np.float32(gate))*vb).astype(np.float32)
            envb*=np.minimum(ttb/np.float32(0.004),1.0).astype(np.float32)
            wave=np.sin(2.0*np.pi*fb*ttb)+np.float32(0.34)*np.sin(4.0*np.pi*fb*ttb)
            bassline=(wave*envb).astype(np.float32)
            if kenv_block is not None:
                bassline*=(1.0-np.float32(0.55)*kenv_block)   # sidechain from the kick
            # v31.18 parity with the native path: the whole low groove layer (kick+bass)
            # shares the record-relative level gain and the clarity post-gate
            _gk=self._lm_k_cur if self._lm_k_cur is not None else np.float32(self._lm.get('kick_g',1.0))
            _sc=_gk*np.float32(self._clar_post)
            if np.ndim(_sc)==1:
                out+=bassline[:,None]*(0.085+0.13*bass_eff)*_sc[:,None]
            else:
                out+=bassline[:,None]*(0.085+0.13*bass_eff)*_sc

        # v31.14 STAB — power-chord stabs (root+fifth+octave, band-passed) that
        # answer the groove on one of two patterns; the melodic "new element"
        # a listener instantly reads as remix production.
        # (Level smoothing hoisted above; rendered natively when RustCore is live.)
        if (not rust_groove_done) and stab_eff>1e-4:
            root_pc=int(_clamp(float(controls.get('fx_bass_root',9.0) or 9.0),0,11))
            spat=int(_clamp(float(controls.get('fx_stab_pattern',0.0) or 0.0),0,1))
            pos16s=grid_beats_arr*4.0
            STAB_V=(
                np.array([0,0,.8,0, 0,0,.8,0, 0,0,.8,0, 0,0,.8,0],dtype=np.float32),      # OFFBEAT
                np.array([0,0,0,.9, 0,0,0,0, 0,0,0,.85, 0,0,.4,0],dtype=np.float32),      # DUB
            )
            def _stab_vel(steps):
                _ins=np.mod(steps,16); _bars=steps//16
                _vs=STAB_V[spat][_ins].copy()
                _hs=(((_bars*np.int64(52361))+(_ins*np.int64(911)))&np.int64(0xFFFFF)).astype(np.float32)/np.float32(1048575.0)
                _vs=_vs*np.where(_hs>0.80,np.float32(0.0),np.float32(1.0))
                return _vs.astype(np.float32),np.zeros(len(steps),dtype=np.float32)
            tts,vs,_=_voice_lookback(pos16s,grid_bps*4.0,_stab_vel,0.0)
            tts=np.where(tts<0,np.float32(9.0),tts)
            f0=np.float32(32.703)*np.float32(2.0)**(np.float32(root_pc)/12.0)
            if f0<38.0: f0=f0*2.0
            f1=f0*4.0; f2=f1*np.float32(2.0)**(7.0/12.0); f3=f0*8.0
            envs=(np.exp(-tts/np.float32(0.085))*vs).astype(np.float32)
            envs*=np.minimum(tts/np.float32(0.003),1.0).astype(np.float32)
            chord=(np.sin(2.0*np.pi*f1*tts)+np.sin(2.0*np.pi*f2*tts)+np.float32(0.6)*np.sin(2.0*np.pi*f3*tts))
            stab_sig=np.tanh(chord*1.2).astype(np.float32)*envs
            stab_st=np.repeat(stab_sig[:,None],self.channels,axis=1)
            self.stab_hp.set_coeffs('highpass',self.sample_rate,180.0,q=.707)
            self.stab_lp.set_coeffs('lowpass',self.sample_rate,min(2400.0,self.sample_rate*.40),q=.707)
            stab_st=self.stab_lp.process(self.stab_hp.process(stab_st))
            if kenv_block is not None:
                stab_st*=(1.0-np.float32(0.45)*kenv_block)[:,None]
            out+=stab_st*(0.05+0.10*stab_eff)*np.float32(self._clar_post)

        # One absolute clock drives all rhythmic FX, preventing pattern/accent state
        # from silently resetting at every audio block.
        self._fx_sample_clock += n
        # Professional controller rack sits after source-derived build layers and
        # before the one-shot impact, matching a pre-master FX-unit topology.
        out=self.pro_fx.process(out,controls,block_s)

        # Impact is sequence-triggered, not level-triggered, so one request yields one
        # clean downbeat hit even if controller packets repeat for several blocks.
        impact_seq=int(controls.get('impact_seq',0) or 0)
        if impact_seq != self._last_impact_seq:
            self._last_impact_seq=impact_seq
            self._impact_env=1.0
            self._impact_phase=0.0
        if self._impact_env > 1e-4:
            t=np.arange(n,dtype=np.float32)/float(self.sample_rate)
            env=(self._impact_env*np.exp(-t/0.24)).astype(np.float32)
            phase=self._impact_phase + 2.0*np.pi*(54.0*t + 22.0*(t*t))
            strength=max(0.18,float(impact_strength))
            thump=np.sin(phase).astype(np.float32)*env*(0.050+0.040*strength)
            click_noise=self._fx_rng.standard_normal((n,self.channels)).astype(np.float32)
            self.impact_click_hp.set_coeffs('highpass',self.sample_rate,1650.0,q=.707)
            click=self.impact_click_hp.process(click_noise)*(env[:,None]**2)*(0.010+0.018*strength)
            # Short wide crash/noise tail adds scale without extending the sub decay.
            crash_noise=self._fx_rng.standard_normal((n,self.channels)).astype(np.float32)
            self.impact_crash_hp.set_coeffs('highpass',self.sample_rate,4200.0,q=.707)
            crash=self.impact_crash_hp.process(crash_noise)
            self.impact_crash_lp.set_coeffs('lowpass',self.sample_rate,min(10500.0,self.sample_rate*.40),q=.707)
            crash=self.impact_crash_lp.process(crash)*(env[:,None]**1.35)*(0.0045+0.011*strength)
            if self.channels >= 2: crash[:,1] *= -0.30
            # Momentary program duck creates headroom for the downbeat hit instead
            # of making the impact louder by simple summation.
            duck_env=(env[:,None]*(.10+.12*strength)).astype(np.float32)
            out *= (1.0-duck_env)
            out += thump[:,None] + click + crash
            self._impact_phase=float((phase[-1] + 2.0*np.pi*54.0/float(self.sample_rate))%(2.0*np.pi)) if n else self._impact_phase
            self._impact_env=float(self._impact_env*math.exp(-block_s/0.24))

        # v31.8.2 Clean Headroom: Agentic FX and the shared Realtime StudioDSP are
        # driven by the same prompt. Bound the *residual* performance bus before it
        # enters saturation/compression so two good-sounding processors do not sum
        # into limiter fizz. This is a transparent gain budget, not a hard clip.
        load=_clamp(0.22*riser+0.18*drive+0.14*double+0.18*reverse+0.18*rush+0.12*impact_strength
                    +0.16*redrum+0.14*float(self.cur.get('fx_bass_synth',0.0))+0.10*float(self.cur.get('fx_stab',0.0))
                    +0.18*float(self.pro_fx.status.get('rack_wet',0.0) or 0.0)
                    +0.14*float(self.pro_fx.status.get('beat_echo',0.0) or 0.0),0.0,1.25)
        # v31.23 RackMind takeover / freeze: frame-scheduled programme duck so the rack's part (or the
        # rolled drop hit) takes the front.  Applied to the dry reference too: the residual cap must
        # keep seeing only the added layers, not the duck.
        if self.takeover.segments and frame_end>0.0:
            _tk,_tf=self.takeover.curves(n,int(frame_end-float(n)))
            if float(_tk.max())>1e-4 or float(_tf.max())>1e-4:
                y=self._duck_y.process(y,_tk,_tf); out=self._duck_out.process(out,_tk,_tf)
        else:
            self.takeover.amount=0.0; self.takeover.full=0.0
        # v31.28: the separated vocal is subtracted sample-exactly (VocalWeave), from y and out alike
        if self.cancel_deck.clips and frame_end>0.0:
            _sub=self.cancel_deck.render(n,int(frame_end-float(n)))
            y=y-_sub; out=out-_sub; self._cancel_active=float(self.cancel_deck.active)
        else:
            self._cancel_active=0.0
        # v31.26: the record's own line steps aside for the generated part (harmonic notches on its notes)
        if self.lineduck_out.segments and frame_end>0.0:
            _fs=int(frame_end-float(n))
            y=self.lineduck_y.process(y,_fs,n); out=self.lineduck_out.process(out,_fs,n)
        # v31.19 DrumMind sample layer (AI drummer): record-matched one-shots on the
        # frame-accurate grid, level-matched to the record, clarity-gated, kick-ducked.
        if self._drum_sample_mode:
            # v31.20: presence (creativity) lifts the drummer above the record's own drums and
            # softens the clarity gate on material that clearly has a grid
            presence=_clamp(float(controls.get('drum_presence',0.5) or 0.0),0.0,1.0)
            d_gate=min(1.0,float(self._clar_gate)+0.30*float(self._clar_src)*presence)
            d_post=min(1.0,float(self._clar_post)+0.25*float(self._clar_src)*presence)
            k_amt=_clamp(float(controls.get('drum_kick',0.0) or 0.0),0.0,1.0)*d_gate
            h_amt=_clamp(float(controls.get('drum_hat',0.0) or 0.0),0.0,1.0)*d_gate
            s_amt=_clamp(float(controls.get('drum_snare',0.0) or 0.0),0.0,1.0)*d_gate
            kit=self.drums.kit
            lm_k=self._level_match_gain('kick',y,float(kit.get('kick_peak',0.8))*(0.35+0.65*k_amt)*0.6,k_amt,block_s,n,boost=presence) if k_amt>1e-3 else np.float32(1.0)
            lm_h=self._level_match_gain('hat',y,float(kit.get('hat_peak',0.8))*(0.35+0.65*h_amt)*0.35,h_amt,block_s,n,boost=presence) if h_amt>1e-3 else np.float32(1.0)
            d_add,d_env=self.drums.render(n,grid_base,grid_bps,(k_amt,h_amt,s_amt),(lm_k,lm_h),d_post)
            out*=(1.0-0.24*k_amt*d_env)[:,None]
            out+=d_add
        residual=out-y
        dry_rms=float(np.sqrt(np.mean(y*y)+1e-12)); res_rms=float(np.sqrt(np.mean(residual*residual)+1e-12))
        # v31.14: the re-drum/keybass are REPLACEMENT material (the agent strips
        # the original floor to make room), so they earn dry-independent headroom
        # instead of being capped relative to an intentionally thinned program.
        res_cap=max(0.006,dry_rms*(0.16+0.12*min(1.0,load)))+0.050*_clamp(redrum+float(self.cur.get('fx_bass_synth',0.0)),0.0,1.4)
        # v31.16 CLICK-FREE PROTECTION CHAIN.  These three stages used to apply a
        # different constant gain to every 43 ms block (block-RMS residual cap,
        # per-block trim, instant-attack bus preconditioner).  With the remix
        # layers held at performance level they were active on most blocks, and
        # the gain STEPS between blocks were audible as a crackle.  Every gain is
        # now a per-sample ramp from the previous block's value, and the cap
        # tracks a smoothed residual level (fast attack, slow release) instead of
        # flapping on single-block RMS.
        rr_prev=float(getattr(self,'_res_rms_track',0.0))
        if res_rms>rr_prev:
            rr_track=rr_prev+(res_rms-rr_prev)*(1.0-math.exp(-block_s/0.012))
        else:
            rr_track=rr_prev+(res_rms-rr_prev)*(1.0-math.exp(-block_s/0.180))
        self._res_rms_track=float(rr_track)
        g_target=min(1.0,res_cap/max(rr_track,1e-8))
        g_prev=float(getattr(self,'_res_gain',1.0))
        if abs(g_target-g_prev)>1e-6:
            residual*=np.linspace(g_prev,g_target,n,dtype=np.float32)[:,None]
        elif g_target<0.999999:
            residual*=np.float32(g_target)
        self._res_gain=float(g_target)
        out=y+residual
        # v31.23: the rack's parts are scheduled PROGRAMME material, not FX residual: they join after the
        # residual cap (which pinned the v31.22 synths to ~20 % of the record) and before the bus preconditioner
        if self.synth.clips and frame_end>0.0:
            out+=self.synth.render(n,int(frame_end-float(n)))
        # Reserve 1.0..3.2 dB before StudioDSP according to concert-FX activity
        # (ramped, never stepped).
        trim_db=-(0.85+2.35*min(1.0,load)) if load>0.03 else 0.0
        trim_t=float(10.0**(trim_db/20.0))
        trim_prev=float(getattr(self,'_trim_gain',1.0))
        if abs(trim_t-trim_prev)>1e-6:
            out*=np.linspace(trim_prev,trim_t,n,dtype=np.float32)[:,None]
        elif trim_t<0.999999:
            out*=np.float32(trim_t)
        self._trim_gain=trim_t
        # Block peak preconditioner: fast (not instantaneous) attack, slow release,
        # applied as a per-sample ramp so downstream nonlinear stages see a
        # continuous gain trajectory.
        peak=float(np.max(np.abs(out))) if out.size else 0.0
        target_g=min(1.0,0.82/max(peak,1e-8))
        old_g=float(self._fx_bus_gain)
        if target_g<old_g:
            new_g=target_g
        else:
            a_rel=1.0-math.exp(-block_s/0.55)
            new_g=old_g+(target_g-old_g)*a_rel
        new_g=float(_clamp(new_g,0.38,1.0))
        if abs(new_g-old_g)>1e-6:
            out*=np.linspace(old_g,new_g,n,dtype=np.float32)[:,None]
        elif new_g<0.999999:
            out*=np.float32(new_g)
        self._fx_bus_gain=new_g

        # v31.11 beat-locked sidechain pump.  Applied after the residual cap and
        # bus preconditioner because it is a pure bounded gain (<=1): the classic
        # "breathing" remix feel must modulate the WHOLE program, and must never
        # be reinterpreted as excess FX residual by the protection stages.
        pump=self._smooth(float(self.cur['fx_pump']),_clamp(float(controls.get('fx_pump',0.0) or 0.0),0.0,0.85),block_s,.080,3.0)
        self.cur['fx_pump']=pump
        sync=int(controls.get('fx_pump_sync_seq',0) or 0)
        if sync != self._last_pump_sync_seq:
            self._last_pump_sync_seq=sync
            self._pump_phase=0.0
        if pump > 1e-3:
            cycle_beats=_clamp(float(controls.get('fx_pump_cycle',1.0) or 1.0),0.25,4.0)
            cyc_s=(1.0/grid_bps)*cycle_beats
            if self._grid_locked:
                # v31.13: pump breathes on the locked beat grid — always in time.
                phb=grid_beats_arr/np.float64(cycle_beats)
                frac=(phb-np.floor(phb)).astype(np.float32)
            else:
                ph=self._pump_phase + np.arange(n,dtype=np.float32)/(float(self.sample_rate)*cyc_s)
                frac=(ph-np.floor(ph)).astype(np.float32)
                self._pump_phase=float(ph[-1]%1024.0)
            attack=max(0.0025/cyc_s,0.004)  # ~3-4 ms duck edge, never a click
            shape=np.minimum(frac/attack,1.0)*np.exp(-frac*(5.5+2.5*pump))
            out*= (1.0-pump*shape)[:,None]
        self.status.update({'noise_riser':round(riser,4),'fx_reverse_swell':round(reverse,4),'fx_snare_rush':round(rush,4),'impact_strength':round(impact_strength,4),'drum_drive':round(drive,4),'doubletime':round(double,4),'impact_env':round(float(self._impact_env),4),
                            'hat_variant':int(self._hat_variant),'riser_variant':int(self._riser_variant),'rush_variant':int(self._rush_variant),'reverse_variant':int(self._reverse_variant),'source_aware_fx':True,
                            'fx_pump':round(pump,4),'fx_rush_accel':round(rush_accel,4),
                            'fx_redrum':round(redrum,4),'fx_redrum_pattern':int(rd_pattern),
                            'redrum_pattern':('FLOOR','HALF','BREAK','UPBEAT')[rd_pattern],
                            'fx_grid_locked':bool(self._grid_locked),'grid_conf':round(float(self._grid_conf),3),
                            'fx_swing':round(swing,3),'hat_density':round(float(self._hat_density),3),
                            'fx_hat_style':int(_clamp(float(controls.get('fx_hat_style',0.0) or 0.0),0,3)),
                            'groove_clarity':round(float(self._clar_src),3),'grid_frame_lock':bool(self._grid_frame_lock),
                            'lm_kick_gain':round(float(self._lm.get('kick_g',1.0)),3),'lm_hat_gain':round(float(self._lm.get('hat_g',1.0)),3),
                            'drum_mode':bool(self._drum_sample_mode),'drum_voices':int(len(self.drums.voices)),'drum_hits':int(self.drums.hits_rendered),
                            'drum_kit':str((self.drums.kit or {}).get('label','') if self.drums.kit else ''),
                            'b_dedrum':round(float(self._b_dedrum),3),'b_dedrum_db':round(float(self.dedrum.reduction_db),2),
                            'ai_loop_ready':bool(self.loop_len>0 and self.loop_ai_len==self.loop_len and self.loop_ai_seq==self.last_capture_seq),
                            'b_ai_mix':round(float(self._ai_mix_eff),3),'loop_capture_seq':int(self.last_capture_seq),
                            'synth_clips':int(len(self.synth.clips)),'synth_played':int(self.synth.played),'synth_active':round(float(self.synth.active),3),
                            'takeover':round(float(self.takeover.amount),3),'freeze':round(float(self.takeover.full),3),'takeover_segments':int(len(self.takeover.segments)),
                            'lineduck':round(float(self.lineduck_out.amount),3),'lineduck_segments':int(len(self.lineduck_out.segments)),
                            'vocal_cancel':round(float(self._cancel_active),3),'cancel_clips':int(len(self.cancel_deck.clips))})
        self.status.update(self.pro_fx.status)
        return np.nan_to_num(out,nan=0.0,posinf=0.0,neginf=0.0).astype(np.float32,copy=False)

    def process(self, live, controls: dict):
        import numpy as np, time
        x = np.asarray(live,dtype=np.float32)
        self._append_history(x)
        cap_seq = int(controls.get('loop_capture_seq',0) or 0)
        rel_seq = int(controls.get('loop_release_seq',0) or 0)
        retrigger_seq = int(controls.get('loop_retrigger_seq',0) or 0)
        if cap_seq != self.last_capture_seq:
            self.last_capture_seq = cap_seq
            self._capture_loop(float(controls.get('bpm',100.0)), float(controls.get('loop_beats',4.0)), controls)
        if retrigger_seq != self._last_retrigger_seq and self.loop_len>0:
            self._last_retrigger_seq=retrigger_seq
            self.loop_pos=0
        if rel_seq != self.last_release_seq:
            # v31.7 graceful loop release: do NOT erase Deck B while the smoothed
            # crossfader still contains B energy.  The old behavior created a short
            # level hole that sounded like an audio dropout on ROLL -> DROP exits.
            self.last_release_seq = rel_seq
            self._loop_release_pending = True
        n=int(x.shape[0]); block_s=n/max(1.0,float(self.sample_rate))
        self._ai_mix_eff=self._smooth(float(self._ai_mix_eff),_clamp(float(controls.get('b_ai_mix',0.0) or 0.0),0.0,1.0),block_s,.120,1.5)
        b=self._loop_block(n,controls)
        aa=self._deck(x,'a',controls,block_s); bb_full=self._deck(b,'b',controls,block_s)
        # v31.9.1 Musical Co-Mix: resolve what Deck B should *be* from measured
        # future compatibility. A rhythmically useful but tonally weak motif is
        # automatically stripped toward percussion instead of clashing melodically.
        role,eff_texture=self._resolve_comix_role(float(controls.get('b_texture',0.0) or 0.0),controls)
        mix_controls=dict(controls); mix_controls['b_texture']=float(eff_texture)
        self._comix_role=str(role)
        bb=self._deck_b_remix_texture(bb_full,mix_controls,block_s)
        # v31.20: A/B loops must not loop the record's drums while the AI drummer plays
        _dd=self._smooth(float(self._b_dedrum),_clamp(float(controls.get('b_dedrum',0.0) or 0.0),0.0,1.0),block_s,.080,2.0)
        self._b_dedrum=_dd
        if _dd>1e-3 and self.loop_len>0:
            bb=self.dedrum.process(bb,_dd)
        target_xf = _clamp(float(controls.get('crossfader',0.0)),0.0,1.0) if self.loop_len > 0 else 0.0
        target_layer = _clamp(float(controls.get('b_layer',0.0)),0.0,.46) if self.loop_len > 0 else 0.0
        # Fixed musical qualification. Frequent creativity-max shifts reuse the same
        # matched Deck-B phrase, but cannot force an unrelated loop into the mix.
        qgate=self._deckb_quality_factor(float(eff_texture),mix_controls) if self.loop_len>0 else 0.0
        self._loop_quality_gate=float(qgate)
        target_xf *= qgate
        target_layer *= qgate
        if self._loop_release_pending:
            target_xf = 0.0; target_layer = 0.0
        xf=self._smooth(float(self.cur['crossfader']), target_xf, block_s, .045, 4.0)
        layer=self._smooth(float(self._b_layer_gain), target_layer, block_s, .060, 2.6)
        self.cur['crossfader']=xf; self.cur['b_layer']=layer; self._b_layer_gain=layer
        # Place B into complementary spectral/center space *before* its dedicated
        # remix FX bus. This makes the second voice sound integrated rather than
        # like a duplicate record sitting on top of A.
        bb=self._spectral_comix(aa,bb,xf,layer,mix_controls)
        bb=self._deck_b_fx(bb,mix_controls,block_s)
        ga=math.cos(xf*math.pi*.5); gb=math.sin(xf*math.pi*.5)
        # Correlation-aware equal-power normalization. The loop is often captured
        # from the same source, so correlated A+B material would otherwise create a
        # +3 dB peak at centre even before master limiting.
        corr=0.0
        if self.loop_len>0 and np.max(np.abs(bb))>1e-7:
            den=float(np.sqrt(np.mean(aa*aa)+1e-12)*np.sqrt(np.mean(bb*bb)+1e-12))
            if den>1e-9: corr=_clamp(float(np.mean(aa*bb))/den,-.95,.95)
        norm=math.sqrt(max(.30,ga*ga+gb*gb+2.0*max(0.0,corr)*ga*gb))
        y=(aa*ga+bb*gb)/max(1.0,norm)
        # Remix-layer mode keeps Deck A present while Deck B contributes a quiet,
        # texture-shaped loop. Positive correlation automatically reduces the layer
        # so a source-derived loop cannot create a +3 dB correlated pile-up.
        if layer>.002 and xf<.62:
            l=layer*(1.0-.52*max(0.0,corr))
            lnorm=math.sqrt(max(1.0,1.0+l*l+2.0*max(0.0,corr)*l))
            y=(y + bb*l)/lnorm
        # Retire the loop only after the audible crossfade has completed.
        if self._loop_release_pending and xf <= 0.006 and layer <= 0.006:
            self.loop_len = 0; self.loop_pos = 0; self._loop_release_pending = False
        # v31.11 build bass strip: produced EDM builds remove the low end for the
        # whole build and slam it back on the drop.  A smoothed program highpass
        # (transparent at 22 Hz when idle) gives that exact "floor drops away"
        # feeling; the agent lanes it up through a build and zeroes it on landings.
        if self._rust is not None:
            y=np.ascontiguousarray(y,dtype=np.float32)
            self.cur['fx_bass_cut']=self._rust.bass_cut(y,_clamp(float(controls.get('fx_bass_cut',0.0) or 0.0),0.0,1.0))
        else:
            bass_cut=self._smooth(float(self.cur['fx_bass_cut']),_clamp(float(controls.get('fx_bass_cut',0.0) or 0.0),0.0,1.0),block_s,.070,3.0)
            self.cur['fx_bass_cut']=bass_cut
            if bass_cut > 1e-3:
                hp_hz=22.0+188.0*(bass_cut**1.25)
                self.bass_cut_hp.set_coeffs('highpass',self.sample_rate,hp_hz,q=.78)
                y=self.bass_cut_hp.process(y)
        y=self._performance_fx(y,controls,block_s)
        y=self._apply_brake_fx(y,controls)
        mode_names={0:'OFF',1:'GROOVE',2:'FILL',3:'CALL',4:'ACCEL',5:'GHOST'}
        # v31.10: publish the *effective* smoothed deck values so the UI deck
        # sliders can mirror what the audio callback is actually applying instead
        # of only the planner's sparse targets.  Pure telemetry; no DSP change.
        self.status.update({
            'a_low_db':round(float(self.cur['a_low_db']),3),'a_mid_db':round(float(self.cur['a_mid_db']),3),
            'a_high_db':round(float(self.cur['a_high_db']),3),'a_filter':round(float(self.cur['a_filter']),4),
            'a_gain':round(float(self.cur['a_gain']),4),
            'b_low_db':round(float(self.cur['b_low_db']),3),'b_mid_db':round(float(self.cur['b_mid_db']),3),
            'b_high_db':round(float(self.cur['b_high_db']),3),'b_filter':round(float(self.cur['b_filter']),4),
            'b_gain':round(float(self.cur['b_gain']),4),
            'reverb':round(_clamp(float(controls.get('reverb',0.0) or 0.0),0.0,1.0),4),
            'echo':round(_clamp(float(controls.get('echo',0.0) or 0.0),0.0,1.0),4),
            'fx_bass_cut':round(float(self.cur['fx_bass_cut']),4),
            'rust_core':bool(self._rust is not None),
            'brake_active':bool(self._brake_active),
            'brake_mode':('BRAKE','BACKSPIN','JUMPBACK','FLASHFWD','INTERLEAVE')[int(_clamp(self._brake_mode,0,4))],
            'brake_u':round(_clamp(self._brake_age_s/max(1e-3,self._brake_len_s),0.0,1.0),3) if self._brake_active else 0.0,
            'fx_bass_synth':round(float(self.cur.get('fx_bass_synth',0.0)),4),
            'fx_stab':round(float(self.cur.get('fx_stab',0.0)),4),
            'fx_bass_root':int(_clamp(float(controls.get('fx_bass_root',9.0) or 9.0),0,11)),
            'fx_bass_pattern':int(_clamp(float(controls.get('fx_bass_pattern',0.0) or 0.0),0,3)),
            'bass_pattern':('PULSE','DRIVE','SUB','OCT')[int(_clamp(float(controls.get('fx_bass_pattern',0.0) or 0.0),0,3))],
            'fx_redrum_var':round(_clamp(float(controls.get('fx_redrum_var',0.0) or 0.0),0,1),3),
            'fx_redrum_fill':round(_clamp(float(controls.get('fx_redrum_fill',0.0) or 0.0),0,1),3),
        })
        self.status.update({'loop_ready':bool(self.loop_len>0),'loop_frames':int(self.loop_len),'loop_age_sec':round(max(0.0,time.monotonic()-self.loop_started_at),3) if self.loop_len else 0.0,'crossfader':round(xf,4),'b_layer':round(layer,4),'b_texture':round(float(self._b_texture),4),'b_transient':round(float(self._b_transient),4),'b_slicer_mix':round(float(self._slice_mix),4),'b_slice_mode':mode_names.get(int(self._slice_mode),'OFF'),'b_slice_map':[int(v) for v in self._slice_map.tolist()],'b_slice_motion':round(float(self._slice_motion),4),'b_fx':round(float(self._b_fx_amount),4),'deckb_mode':('SLICER' if self._slice_mix>.12 else ('PERCUSSIVE' if self._b_texture>.18 else ('HARMONIC' if self._b_texture<-.18 else 'FULL'))),'correlation':round(corr,4),'loop_release_pending':bool(self._loop_release_pending),'loop_selected_offset_beats':round(float(self._loop_selected_offset_beats),3),'loop_selection_score':round(float(self._loop_selection_score),4),'loop_harmonic_match':round(float(self._loop_harmonic_match),4),'loop_rhythm_match':round(float(self._loop_rhythm_match),4),'loop_salience':round(float(self._loop_salience),4),'loop_energy_match':round(float(self._loop_energy_match),4),'loop_role':str(self._loop_role),'loop_quality_gate':round(float(self._loop_quality_gate),4),'loop_micro_align_ms':round(float(self._loop_micro_align_ms),2),'comix_role':str(self._comix_role),'comix_low_keep':round(float(self._comix_low_keep),4),'comix_body_keep':round(float(self._comix_body_keep),4),'comix_air_keep':round(float(self._comix_air_keep),4),'comix_center_keep':round(float(self._comix_center_keep),4)})
        return np.nan_to_num(y,nan=0.0,posinf=0.0,neginf=0.0).astype(np.float32,copy=False)

def _shared_agentic_headroom(params: dict, controls: dict) -> dict:
    """Coordinate Realtime FX and Agentic DJ when both obey one prompt.

    The two engines remain expressive, but share one gain/nonlinearity budget.
    Strong controller FX automatically reduce StudioDSP saturation, duplicate
    time-FX and positive EQ boosts before the audio reaches the master chain.
    """
    p=dict(params or {})
    c=controls or {}
    def cv(name, default=0.0):
        try: return float(c.get(name,default) or default)
        except Exception: return float(default)
    perf=_clamp(
        0.18*cv('noise_riser')+0.16*cv('fx_reverse_swell')+0.16*cv('fx_snare_rush')
        +0.14*cv('drum_drive')+0.10*cv('doubletime')+0.16*cv('impact_strength')
        +0.18*cv('fx_rack_wet')+0.14*cv('fx_echo_send')+0.10*cv('fx_gate'),0.0,1.0)
    if perf<0.02:
        return p
    # Avoid duplicated distortion: performance drum excitation gets priority over
    # StudioDSP harmonic drive during concert gestures.
    for k,amt in (('drive',.56),('low_drive',.46),('mid_drive',.54),('high_drive',.64),('saturation_mix',.40)):
        if k in p: p[k]=float(p[k])*max(.30,1.0-amt*perf)
    # One time-FX authority at a time: rack echo/space owns the foreground tail.
    if 'reverb' in p: p['reverb']=float(p['reverb'])*max(.52,1.0-.38*perf)
    if 'delay' in p: p['delay']=float(p['delay'])*max(.38,1.0-.56*perf)
    # Pull only positive EQ boosts; cuts remain available for creative filtering.
    for k in ('sub_db','bass_db','body_db','mid_db','presence_db','air_db'):
        if k in p and float(p[k])>0.0:
            p[k]=float(p[k])*(1.0-.24*perf)
    if 'comp_makeup_db' in p:
        p['comp_makeup_db']=min(float(p['comp_makeup_db']),max(0.0,1.0-1.25*perf))
    # Shared master headroom is deliberately outside the learned/search authority.
    p['output_db']=min(float(p.get('output_db',0.0)),-(0.8+2.6*perf))
    return p


class RealtimeDSP:
    """Full-rate realtime DSP graph shared by manual, 15D DJ, and 50D modes.

    Every parameter in DSP50_SPECS changes a genuinely independent part of the
    graph.  Parameter motion is block-smoothed before coefficient updates; audio
    never waits for semantic inference.
    """

    def __init__(self, sample_rate: int, channels: int):
        import numpy as np
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.filters = [Biquad(channels) for _ in range(8)]
        self.sat_low = Biquad(channels)
        self.sat_sub = Biquad(channels)
        self.sat_mid = Biquad(channels)
        self.sat_post_lp = Biquad(channels)
        self.sat_dc_hp = Biquad(channels)
        self.side_hp = Biquad(1)
        self.side_tilt = Biquad(1)
        # Mastering-style compressor detector HPF. Bass still passes through the
        # audio path unchanged, but sub energy does not make the broadband gain
        # computer pump on every kick. State stays warm at all times.
        self.comp_sc_hp = Biquad(channels)
        self.delay = DelayBuffer(sample_rate, channels)
        self.reverb = MultiTapReverb(sample_rate, channels)
        self.native_core = NativeStudioCore(sample_rate, channels)
        self.backend_name = "StudioDSP v31 Clean6x · MusicCLAP · LowEndIntegrity · sub-safe nonlinear · smooth lookahead" if self.native_core.available else "JoyMetric StudioDSP fallback · dry-anchor"
        self.comp_env = 0.0
        self.comp_gain = 1.0
        self.trans_fast = 0.0
        self.trans_slow = 0.0
        self.phase = 0.0
        self._adaa_prev = {
            "low": np.zeros((channels,), dtype=np.float32),
            "mid": np.zeros((channels,), dtype=np.float32),
            "high": np.zeros((channels,), dtype=np.float32),
        }
        self.declick = SingleSampleDeClicker(channels)
        self.fidelity_guard = SourceFidelityGuard(sample_rate, channels)
        self.auto_gain = TransparentAutoGainStage(sample_rate, max_peak_lift_db=2.15, max_rms_lift_db=2.45)
        self.master_comp = MasterSafetyCompressor(sample_rate, channels)
        self.limiter = LookaheadPeakLimiter(sample_rate, channels, ceiling_db=-2.04, release_ms=230.0, lookahead_ms=5.5)
        # Two-stage automation state. control_params absorbs AI/window updates;
        # smoothed_params is the faster audio-facing slew. Both persist for the
        # complete WASAPI stream and are never recreated at semantic boundaries.
        self.control_params = dict(DEFAULT_PARAMS)
        self.smoothed_params = dict(DEFAULT_PARAMS)
        # C1 automation trajectory: position is smoothed_params, while the
        # normalized velocity is persistent across blocks.  Target updates can
        # therefore change *where* the mix is going without abruptly changing the
        # slope of EQ/wet/dynamics automation.  The 2 s preview budget lets us use
        # this much more musical motion without making the perceived effect late.
        self.param_velocity_norm = {name: 0.0 for name in PARAM_BOUNDS}
        # v30.4.3 adds a persistent acceleration state as well.  Position, velocity
        # and acceleration all remain continuous when a new semantic target arrives;
        # jerk is bounded per block.  The x5 destination is unchanged — only the
        # path taken to that destination is smoother.
        self.param_accel_norm = {name: 0.0 for name in PARAM_BOUNDS}
        self._np = np

    @staticmethod
    def _gain_db(db: float) -> float:
        return 10.0 ** (float(db) / 20.0)

    def _compress(self, y, p: dict[str, float]):
        import numpy as np
        c = _clamp(p.get("compression", 0.0), 0.0, 1.0)
        if c < 1e-4:
            self.comp_gain += (1.0 - self.comp_gain) * 0.03
            return y * self.comp_gain

        fs = float(self.sample_rate)
        # Side-chain high-pass + dry blend: enough low end remains in the detector
        # for musical glue, but 20-60 Hz excursions no longer dominate gain
        # reduction. This is substantially smoother on bass-heavy material.
        self.comp_sc_hp.set_coeffs("highpass", self.sample_rate, 82.0, 0.707, 0.0)
        sc_hp = self.comp_sc_hp.process(y)
        detector_signal = 0.30 * y + 0.70 * sc_hp
        detector = np.sqrt(np.mean(detector_signal * detector_signal, axis=1) + 1e-12)
        attack_s = _clamp(p.get("comp_attack_ms", 7.5), 1.5, 45.0) / 1000.0
        release_s = _clamp(p.get("comp_release_ms", 165.0), 45.0, 420.0) / 1000.0
        a_att = math.exp(-1.0 / max(1.0, fs * attack_s))
        a_rel = math.exp(-1.0 / max(1.0, fs * release_s))
        envs = np.empty_like(detector, dtype=np.float32)
        env = float(self.comp_env)
        for i, level in enumerate(detector):
            coeff = a_att if float(level) > env else a_rel
            env = coeff * env + (1.0 - coeff) * float(level)
            envs[i] = env
        self.comp_env = env

        level_db = 20.0 * np.log10(np.maximum(envs, 1e-8))
        threshold = _clamp(p.get("comp_threshold_db", -18.0), -34.0, -6.0)
        requested_ratio = _clamp(p.get("comp_ratio", 3.0), 1.2, 8.0)
        ratio = 1.0 + (requested_ratio - 1.0) * c
        knee = _clamp(p.get("comp_knee_db", 6.0), 1.0, 14.0)
        lower = threshold - knee * 0.5
        upper = threshold + knee * 0.5
        reduction = np.zeros_like(level_db, dtype=np.float32)
        above = level_db >= upper
        reduction[above] = (level_db[above] - threshold) * (1.0 - 1.0 / ratio)
        mid = (level_db > lower) & (level_db < upper)
        z = level_db[mid] - lower
        reduction[mid] = (1.0 - 1.0 / ratio) * (z * z) / (2.0 * knee)
        target_gain = np.power(10.0, -reduction / 20.0).astype(np.float32)

        # Gain envelope is slower than the detector so automation remains smooth
        # even when the 50D optimizer changes compressor time constants.
        g_att = math.exp(-1.0 / max(1.0, fs * max(0.0025, attack_s * 0.65)))
        g_rel = math.exp(-1.0 / max(1.0, fs * max(0.055, release_s * 0.70)))
        gains = np.empty_like(target_gain)
        g = float(self.comp_gain)
        for i, tgt in enumerate(target_gain):
            coeff = g_att if float(tgt) < g else g_rel
            g = coeff * g + (1.0 - coeff) * float(tgt)
            gains[i] = g
        self.comp_gain = g
        makeup = self._gain_db(_clamp(p.get("comp_makeup_db", 0.0), -2.0, 4.0) * c)
        return (y * gains[:, None] * makeup).astype(np.float32, copy=False)

    def _transient_shape(self, y, attack: float, sustain: float):
        import numpy as np
        a = _clamp(attack, -1.0, 1.0)
        s = _clamp(sustain, -1.0, 1.0)
        if abs(a) < 1e-4 and abs(s) < 1e-4:
            return y
        fs = float(self.sample_rate)
        det = np.max(np.abs(y), axis=1)
        f_att = math.exp(-1.0 / max(1.0, fs * 0.0018))
        f_rel = math.exp(-1.0 / max(1.0, fs * 0.032))
        s_att = math.exp(-1.0 / max(1.0, fs * 0.024))
        s_rel = math.exp(-1.0 / max(1.0, fs * 0.220))
        fast = float(self.trans_fast); slow = float(self.trans_slow)
        fbuf = np.empty_like(det, dtype=np.float32); sbuf = np.empty_like(det, dtype=np.float32)
        for i, v in enumerate(det):
            fv = float(v)
            cf = f_att if fv > fast else f_rel
            cs = s_att if fv > slow else s_rel
            fast = cf * fast + (1.0 - cf) * fv
            slow = cs * slow + (1.0 - cs) * fv
            fbuf[i] = fast; sbuf[i] = slow
        self.trans_fast, self.trans_slow = fast, slow
        transient = (fbuf - sbuf) / np.maximum(0.035, sbuf)
        attack_shape = np.tanh(transient * 2.8)
        sustain_mask = np.clip(sbuf / np.maximum(fbuf, 0.025), 0.0, 1.0)
        gain_db = 4.2 * a * attack_shape + 2.5 * s * sustain_mask
        # Remove most average gain so the semantic critic cannot win only by
        # making the signal louder.
        gain_db -= float(np.mean(gain_db)) * 0.72
        gain = np.power(10.0, gain_db / 20.0).astype(np.float32)
        return (y * gain[:, None]).astype(np.float32, copy=False)

    @staticmethod
    def _sat_curve(x, amount: float, asymmetry: float):
        import numpy as np
        amt = _clamp(amount, 0.0, 1.35)
        if amt < 1e-5:
            return x
        # Soft analogue-style transfer with level compensation. The previous
        # tanh()/pregain curve darkened driven bands simply because stronger drive
        # also reduced their output level. This curve keeps the tone change tied
        # to harmonics rather than accidental attenuation.
        pregain = 1.0 + 3.2 * amt
        bias = 0.12 * _clamp(asymmetry, -0.55, 0.55)
        offset = math.tanh(pregain * bias)
        shaped = np.tanh((x + bias) * pregain) - offset
        # Blend in a gentler arctangent branch to avoid the glassy edge of pure
        # tanh at high drive, then RMS-match within a conservative range.
        atan_branch = (2.0 / math.pi) * np.arctan((x + bias) * pregain * 1.12)
        shaped = 0.72 * shaped + 0.28 * atan_branch
        rin = float(np.sqrt(np.mean(x * x) + 1e-12))
        rout = float(np.sqrt(np.mean(shaped * shaped) + 1e-12))
        comp = _clamp(rin / max(rout, 1e-7), 0.62, 1.32)
        return (shaped * comp).astype(np.float32, copy=False)

    @classmethod
    def _sat_curve_2x(cls, x, amount: float, asymmetry: float):
        """Cheap 2x oversampled nonlinear branch with reconstruction averaging.

        This is only used when drive is clearly audible. It materially reduces the
        brittle alias components of strong saturation without putting an expensive
        resampler in the always-on dry path.
        """
        import numpy as np
        if float(amount) < 0.22 or x.shape[0] < 2:
            return cls._sat_curve(x, amount, asymmetry)
        n = x.shape[0]
        up = np.empty((n * 2, x.shape[1]), dtype=np.float32)
        up[0::2] = x
        nxt = np.vstack([x[1:], x[-1:]])
        up[1::2] = 0.5 * (x + nxt)
        shaped = cls._sat_curve(up, amount, asymmetry)
        # Two-tap reconstruction average before decimation suppresses much of the
        # newly-created top-octave energy that would otherwise fold back.
        return (0.5 * (shaped[0::2] + shaped[1::2])).astype(np.float32, copy=False)

    def _sat_curve_adaa(self, x, amount: float, asymmetry: float, state_key: str):
        """First-order antiderivative anti-aliased tanh saturator.

        Unlike the old interpolate→tanh→average 2x branch, ADAA preserves a
        continuous nonlinear state across block boundaries and suppresses a large
        share of alias products without resampling the dry path.  The computation
        is vectorized, so it stays viable in the realtime Python process.
        """
        import numpy as np
        amt = _clamp(float(amount), 0.0, 1.20)
        if amt < 1e-5:
            if x.shape[0]:
                self._adaa_prev[state_key] = x[-1].astype(np.float32, copy=True)
            return x
        pregain = 1.0 + 2.65 * amt
        bias = 0.085 * _clamp(float(asymmetry), -0.55, 0.55)
        offset = math.tanh(pregain * bias)
        prev0 = self._adaa_prev.get(state_key)
        if prev0 is None or len(prev0) != x.shape[1]:
            prev0 = x[0].astype(np.float32, copy=True)
        xp = np.vstack((prev0[None, :], x[:-1])).astype(np.float64, copy=False)
        xd = x.astype(np.float64, copy=False)
        z = pregain * (xd + bias)
        zp = pregain * (xp + bias)
        logcosh = np.logaddexp(z, -z) - math.log(2.0)
        logcosh_p = np.logaddexp(zp, -zp) - math.log(2.0)
        # Antiderivative of tanh(k(x+b))-offset.
        F = logcosh / pregain - offset * xd
        Fp = logcosh_p / pregain - offset * xp
        dx = xd - xp
        mid = 0.5 * (xd + xp)
        direct = np.tanh(pregain * (mid + bias)) - offset
        shaped = np.where(np.abs(dx) > 1e-6, (F - Fp) / np.where(np.abs(dx) > 1e-12, dx, 1.0), direct)
        self._adaa_prev[state_key] = x[-1].astype(np.float32, copy=True)
        shaped = shaped.astype(np.float32, copy=False)
        rin = float(np.sqrt(np.mean(x * x) + 1e-12))
        rout = float(np.sqrt(np.mean(shaped * shaped) + 1e-12))
        comp = _clamp(rin / max(rout, 1e-7), 0.68, 1.22)
        return (shaped * comp).astype(np.float32, copy=False)

    def _saturate_multiband(self, y, p: dict[str, float]):
        import numpy as np
        global_drive = _clamp(p.get("drive", 0.0), 0.0, 1.0)
        lo_amt = _clamp(global_drive + 0.82 * p.get("low_drive", 0.0), 0.0, 1.12)
        mid_amt = _clamp(global_drive + 0.82 * p.get("mid_drive", 0.0), 0.0, 1.08)
        # Top-band nonlinear energy is the easiest to hear as digital fizz. Keep
        # it deliberately gentler; Air/Presence EQ carries brightness instead.
        hi_amt = _clamp(0.78 * global_drive + 0.64 * p.get("high_drive", 0.0), 0.0, 0.82)
        # Bright CLAP targets already receive Air/Presence EQ. Reduce nonlinear
        # high-band drive as those boosts rise so excitement comes from tonal EQ,
        # not aliased/fizzy upper harmonics.
        bright_boost = max(0.0, float(p.get("air_db", 0.0))) + 0.55 * max(0.0, float(p.get("presence_db", 0.0)))
        hi_amt *= 1.0 / (1.0 + 0.045 * bright_boost)
        max_amt = max(lo_amt, mid_amt, hi_amt)
        # Keep crossover states warm even while saturation is neutral. Resetting
        # these IIR states at 0 and re-enabling them on the next AI move creates a
        # tiny transient at exactly the moments the DJ controller is most active.
        self.sat_low.set_coeffs("lowpass", self.sample_rate, 220.0, 0.707, 0.0)
        self.sat_sub.set_coeffs("lowpass", self.sample_rate, 92.0, 0.707, 0.0)
        self.sat_mid.set_coeffs("lowpass", self.sample_rate, 3600.0, 0.707, 0.0)
        low = self.sat_low.process(y)
        sub = self.sat_sub.process(low)
        low_body = low - sub
        low_mid = self.sat_mid.process(y)
        if max_amt < 1e-5:
            return y
        mid = low_mid - low
        high = y - low_mid
        asym = _clamp(p.get("saturation_asymmetry", 0.0), -0.55, 0.55)
        # LowEndIntegrity: preserve the deepest kick/808 fundamental linearly and
        # saturate the 92..220 Hz body instead. Nonlinear processing of two large
        # low-frequency tones creates intermodulation that a limiter cannot undo.
        shaped = (
            sub
            + self._sat_curve_adaa(low_body, lo_amt, asym, "low")
            + self._sat_curve_adaa(mid, mid_amt, asym * 0.78, "mid")
            + self._sat_curve_adaa(high, hi_amt, asym * 0.55, "high")
        )
        # Anti-harshness return filters: only the nonlinear branch is band-limited,
        # so neutral and lightly-driven material keeps its original top octave.
        # Clean6x dynamic nonlinear return bandwidth. The dry path keeps the full
        # source bandwidth; only newly-created harmonic energy is progressively
        # band-limited as drive rises. This removes the brittle top-octave hash
        # without dulling Air/Clarity EQ.
        nl_lp = min(18500.0 - 2100.0 * min(1.0, max_amt), self.sample_rate * 0.435)
        nl_lp = max(15200.0, nl_lp)
        self.sat_post_lp.set_coeffs("lowpass", self.sample_rate, nl_lp, 0.707, 0.0)
        self.sat_dc_hp.set_coeffs("highpass", self.sample_rate, 18.0, 0.707, 0.0)
        shaped = self.sat_post_lp.process(shaped)
        shaped = self.sat_dc_hp.process(shaped)
        mix = _clamp(p.get("saturation_mix", 0.34), 0.10, 0.52) * min(1.0, max_amt) * 0.54
        # Dry and saturated branches are highly correlated. Equal-power mixing
        # can therefore add close to +3 dB at the exact drive settings where the
        # user reported clipping. Use a correlation-safe linear blend, then allow
        # only a tiny density lift while preserving the harmonic character.
        mixed = y * (1.0 - mix) + shaped * mix
        rin = float(np.sqrt(np.mean(y * y) + 1e-12))
        rout = float(np.sqrt(np.mean(mixed * mixed) + 1e-12))
        target_rms = rin * (1.0 + 0.055 * min(1.0, max_amt) * mix)
        comp = _clamp(target_rms / max(rout, 1e-8), 0.68, 1.06)
        return (mixed * comp).astype(np.float32, copy=False)

    def _stereo_stage(self, y, p: dict[str, float], hypnotic: float):
        import numpy as np
        if self.channels < 2:
            return y
        mid = 0.5 * (y[:, 0] + y[:, 1])
        side = 0.5 * (y[:, 0] - y[:, 1])
        bass_mono = _clamp(p.get("bass_mono_hz", 20.0), 20.0, 260.0)
        self.side_hp.set_coeffs("highpass", self.sample_rate, max(20.0, bass_mono), 0.707, 0.0)
        side_hp = self.side_hp.process(side[:, None])[:, 0]
        mono_mix = _clamp((bass_mono - 20.0) / 18.0, 0.0, 1.0)
        side = side * (1.0 - mono_mix) + side_hp * mono_mix
        side_tilt = _clamp(p.get("side_tilt_db", 0.0), -6.0, 6.0)
        self.side_tilt.set_coeffs("highshelf", self.sample_rate, 3500.0, 0.707, side_tilt)
        # At 0 dB this is already unity, but processing continuously preserves the
        # filter history so a later AI tilt cannot click into existence.
        side = self.side_tilt.process(side[:, None])[:, 0]

        width = _clamp(p.get("width", 1.0), 0.0, 1.6)
        side *= width
        # Correlation-aware width guard. Only intervenes when an already-wide
        # source plus AI widening would push the block toward anti-phase. This lets
        # the controller sound wider while remaining mono-compatible and less
        # phasey on headphones.
        if y.shape[0] >= 32:
            den = float(np.sqrt(np.mean(y[:,0] * y[:,0]) * np.mean(y[:,1] * y[:,1]) + 1e-12))
            corr = float(np.mean(y[:,0] * y[:,1]) / max(den, 1e-8))
            if corr < 0.08 and width > 1.05:
                guard = _clamp((corr + 0.18) / 0.26, 0.25, 1.0)
                side *= 0.72 + 0.28 * guard
        h = _clamp(max(float(hypnotic or 0.0), float(p.get("hypnotic_motion_depth", 0.0) or 0.0)), 0.0, 1.35)
        if h > 1e-4:
            n = y.shape[0]
            rate = _clamp(float(p.get("hypnotic_rate_hz", 0.18) or 0.18), 0.05, 0.70)
            phase_offset = 2.0 * math.pi * _clamp(p.get("motion_phase", 0.25), 0.0, 1.0)
            phases = self.phase + (2.0 * math.pi * rate / self.sample_rate) * np.arange(n, dtype=np.float32)
            motion = np.sin(phases + phase_offset)
            side *= 1.0 + (0.16 * min(1.0, h)) * np.sin(phases * 0.63 + 1.1 + phase_offset * 0.5)
            pan = motion * min(0.24, 0.18 * h)
            l = (mid + side) * np.sqrt(np.clip(1.0 - pan, 0.70, 1.30))
            r = (mid - side) * np.sqrt(np.clip(1.0 + pan, 0.70, 1.30))
            self.phase = float((phases[-1] + 2.0 * math.pi * rate / self.sample_rate) % (2.0 * math.pi))
        else:
            l, r = mid + side, mid - side

        balance = _clamp(p.get("stereo_balance", 0.0), -0.45, 0.45)
        if abs(balance) > 1e-5:
            if balance > 0:
                l *= self._gain_db(-5.0 * balance)
            else:
                r *= self._gain_db(5.0 * balance)
        return np.stack([l, r], axis=1).astype(np.float32, copy=False)

    def process(self, x, params: dict[str, float], hypnotic: float = 0.0):
        import numpy as np
        target = {**DEFAULT_PARAMS, **(params or {})}
        for name, (lo, hi) in PARAM_BOUNDS.items():
            if name in target:
                target[name] = _clamp(target[name], lo, hi)

        fs = self.sample_rate
        block_s = float(max(1, x.shape[0])) / float(fs)
        # 50 moving nodes demand deliberate coefficient slew. Frequency/Q/time
        # controls move slower than gains/wet levels, avoiding zippering and pole
        # jumps while still tracking the optimizer visibly.
        slow_names = {
            "low_cut_hz","low_cut_q","sub_freq_hz","bass_freq_hz","body_freq_hz","body_q",
            "mid_freq_hz","mid_q","presence_freq_hz","presence_q","air_freq_hz",
            "high_cut_hz","high_cut_q","comp_attack_ms","comp_release_ms","comp_ratio",
            "reverb_decay","reverb_predelay_ms","reverb_diffusion","reverb_damping",
            "reverb_tone_hz","delay_ms","delay_feedback","bass_mono_hz",
            "hypnotic_rate_hz","motion_phase",
        }
        medium_names = {
            "compression","comp_threshold_db","comp_knee_db","comp_makeup_db",
            "transient_attack","transient_sustain","drive","saturation_mix",
            "saturation_asymmetry","low_drive","mid_drive","high_drive","width",
            "side_tilt_db","stereo_balance","hypnotic_motion_depth","reverb","delay",
        }
        for name, value in target.items():
            # Stage 1: semantic/control-rate smoothing. This deliberately ignores
            # the exact analysis-window boundary and turns AI decisions into a
            # continuous production trajectory. Time/frequency/topology-like
            # controls move slowest; gains/wet amounts stay responsive.
            tau_control_base = 1.62 if name in slow_names else (1.05 if name in medium_names else 0.72)
            tau_control = tau_control_base / CONTROL_RESPONSE_SPEED
            a_control = 1.0 - math.exp(-block_s / max(0.08, tau_control))
            ctl = float(self.control_params.get(name, value))
            ctl += (float(value) - ctl) * a_control
            self.control_params[name] = ctl

            # Stage 2: velocity-continuous audio trajectory.  A classic EMA is
            # value-continuous but its slope can still kink whenever a new AI
            # target arrives.  Here the normalized parameter velocity itself is
            # smoothed and rate-limited, so automation is C1-like across semantic
            # window boundaries.  This is especially important at x5 strength.
            if name in PARAM_BOUNDS:
                cur = float(self.smoothed_params.get(name, ctl))
                cur_n = dsp50_normalized(cur, name)
                ctl_n = dsp50_normalized(ctl, name)
                err_n = ctl_n - cur_n
                if name in slow_names:
                    track_tau, accel_tau, max_rate, max_accel, max_jerk = 0.82, 0.36, 0.18, 0.52, 2.05
                elif name in medium_names:
                    track_tau, accel_tau, max_rate, max_accel, max_jerk = 0.46, 0.22, 0.42, 1.55, 7.20
                else:
                    track_tau, accel_tau, max_rate, max_accel, max_jerk = 0.27, 0.14, 0.78, 3.40, 18.0
                # Faster destination tracking without throwing away C2 continuity.
                # Tau is shortened more than rate/acceleration limits are expanded,
                # which makes controls feel immediate while retaining a smooth slope.
                speed = CONTROL_RESPONSE_SPEED
                track_tau /= speed
                accel_tau /= max(1.0, 0.82 * speed)
                max_rate *= 1.0 + 0.58 * (speed - 1.0)
                max_accel *= 1.0 + 0.92 * (speed - 1.0)
                max_jerk *= 1.0 + 1.10 * (speed - 1.0)
                desired_v = _clamp(err_n / max(track_tau, block_s), -max_rate, max_rate)
                v = float(self.param_velocity_norm.get(name, 0.0))
                acc = float(self.param_accel_norm.get(name, 0.0))
                # Brake before the target instead of snapping onto it. A snap is
                # position-continuous but creates an abrupt velocity reversal; a
                # small, smoothly damped overshoot is much less audible.
                stopping = (v * v) / max(1e-6, 2.0 * max_accel)
                if err_n * v > 0.0 and abs(err_n) < stopping * 1.30:
                    desired_v = 0.0
                if abs(err_n) < 2e-5:
                    desired_v = 0.0
                desired_acc = _clamp((desired_v - v) / max(accel_tau, block_s), -max_accel, max_accel)
                # Jerk-limited acceleration update: no block can abruptly change
                # the *slope of the velocity*. This is the C2-like part missing in
                # v30.4.2 and is especially audible on x5 wet/EQ automation.
                max_da = max_jerk * block_s
                acc += _clamp(desired_acc - acc, -max_da, max_da)
                acc = _clamp(acc, -max_accel, max_accel)
                v_next = _clamp(v + acc * block_s, -max_rate, max_rate)
                step_n = 0.5 * (v + v_next) * block_s
                nxt_n = cur_n + step_n
                if nxt_n <= 0.0 or nxt_n >= 1.0:
                    nxt_n = _clamp(nxt_n, 0.0, 1.0)
                    v_next *= 0.35
                    acc *= 0.25
                self.smoothed_params[name] = dsp50_from_normalized(nxt_n, name)
                self.param_velocity_norm[name] = v_next
                self.param_accel_norm[name] = acc
            else:
                tau_audio_base = 0.82 if name in slow_names else (0.50 if name in medium_names else 0.30)
                tau_audio = tau_audio_base / CONTROL_RESPONSE_SPEED
                a_audio = 1.0 - math.exp(-block_s / max(0.06, tau_audio))
                cur = float(self.smoothed_params.get(name, ctl))
                self.smoothed_params[name] = cur + (ctl - cur) * a_audio
        p = dict(self.smoothed_params)

        specs = [
            ("highpass", p["low_cut_hz"], p["low_cut_q"], 0.0),
            ("peaking", p["sub_freq_hz"], 0.72, p["sub_db"]),
            ("lowshelf", p["bass_freq_hz"], 0.707, p["bass_db"]),
            ("peaking", p["body_freq_hz"], p["body_q"], p["body_db"]),
            ("peaking", p["mid_freq_hz"], p["mid_q"], p["mid_db"]),
            ("peaking", p["presence_freq_hz"], p["presence_q"], p["presence_db"]),
            ("highshelf", p["air_freq_hz"], 0.707, p["air_db"]),
            ("lowpass", p["high_cut_hz"], p["high_cut_q"], 0.0),
        ]
        y = x.astype(np.float32, copy=False)
        for filt, spec in zip(self.filters, specs):
            kind, freq, q, gain = spec
            # Never hard-reset a biquad when an AI-controlled parameter crosses its
            # neutral point. A reset followed by re-enable creates a state
            # discontinuity that is heard as a tick/crackle. Keep every filter state
            # warm and crossfade the neutral HP/LP edges instead. Gain filters are
            # mathematically unity at 0 dB, so they can run continuously.
            dry_stage = y
            filt.set_coeffs(kind, fs, freq, q, gain)
            wet_stage = filt.process(y)
            if kind == "highpass":
                mix = _clamp((float(freq) - 20.0) / 10.0, 0.0, 1.0)
                y = dry_stage * (1.0 - mix) + wet_stage * mix
            elif kind == "lowpass":
                mix = _clamp((20000.0 - float(freq)) / 1200.0, 0.0, 1.0)
                y = dry_stage * (1.0 - mix) + wet_stage * mix
            else:
                y = wet_stage

        y = self._transient_shape(y, p["transient_attack"], p["transient_sustain"])
        if self.native_core.available:
            y = self.native_core.process_compressor(y, p)
        else:
            y = self._compress(y, p)
        # Keep a cleaner post-dynamics source for reverb/delay excitation.
        # This avoids repeatedly reverberating saturation alias/roughness while
        # preserving the same musical envelope and EQ context.
        space_source = y.astype(np.float32, copy=True)
        y = self._saturate_multiband(y, p)
        # Native Chorus carries most motion when available; keep the custom M/S
        # stage for width, bass mono, balance and side tilt only.
        stereo_h = 0.0 if self.native_core.available else hypnotic
        y = self._stereo_stage(y, p, stereo_h)

        native_time = self.native_core.process_time_fx(y, p, hypnotic, clean_source=space_source) if self.native_core.available else None
        if native_time is not None:
            y = native_time
        else:
            rev_wet = _clamp(p["reverb"], 0.0, 0.75)
            delay_wet = _clamp(p["delay"], 0.0, 0.12)
            rev = self.reverb.process(
                y, min(0.90, rev_wet * 1.24), p["reverb_decay"],
                p["reverb_damping"], p["reverb_tone_hz"],
                p["reverb_predelay_ms"], p["reverb_diffusion"],
            )
            echo = self.delay.process(
                y, p["delay_ms"], min(0.12, delay_wet * 0.55),
                _clamp(p["delay_feedback"], 0.02, 0.22),
            )
            dry = math.sqrt(max(0.42, 1.0 - 0.56 * rev_wet - 0.08 * delay_wet))
            y = y * dry + rev + echo

        # v29 HiFi source preservation: the live waveform is never reconstructed.
        # The complete DSP result is treated as a residual around the original
        # full-rate block and bounded only when it becomes implausibly destructive.
        y = self.fidelity_guard.process(y, x, p)
        y *= self._gain_db(_clamp(p.get("output_db", 0.0), -12.0, 3.0))

        # Remove DSP-created level inflation before safety dynamics. This keeps
        # strong tonal/spatial changes but prevents the limiter from becoming the
        # main audible effect.
        y = self.auto_gain.process(y, x)
        # v30.4.47: conservative isolated-sample repair remains ON by default as a last
        # anti-click guard. The detector requires neighbor agreement and a very
        # large one-sample interpolation error, so ordinary musical attacks pass.
        # Set JOY_SAMPLE_DECLICK=0 to disable for A/B diagnostics.
        if os.environ.get("JOY_SAMPLE_DECLICK", "1").strip().lower() in {"1","true","yes"}:
            y = self.declick.process(y)
        y = self.master_comp.process(y)
        y = self.limiter.process(y)
        # Numerical emergency rail only. With the true-peak limiter active this is
        # normally a no-op, but it guarantees no driver ever receives >0 dBFS.
        y = np.clip(y, -0.999, 0.999)
        return np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32, copy=False)


@dataclass
class EngineStatus:
    running: bool = False
    message: str = "Stopped"
    input_label: str = ""
    output_label: str = ""
    source_kind: str = "device"
    source_pid: int = 0
    capture_backend: str = "WASAPI endpoint"
    playback_backend: str = ""
    source_suppressed: bool = False
    source_attenuation_db: float = 0.0
    source_volume_compensation: str = "none"
    sample_rate: int = 0
    channels: int = 0
    buffer_frames: int = 0
    buffer_ms: float = 0.0
    io_latency_ms: float = 0.0
    half_rate: bool = False
    input_db: float = -120.0
    output_db: float = -120.0
    limiter_reduction_db: float = 0.0
    master_comp_reduction_db: float = 0.0
    auto_gain_reduction_db: float = 0.0
    fidelity_residual_gain: float = 1.0
    fidelity_quality_score: float = 1.0
    declick_repairs: int = 0
    output_buffer_frames: int = 0
    output_buffer_ms: float = 0.0
    continuity_lookahead_ms: float = 0.0
    continuity_schedule_ahead_ms: float = 0.0
    continuity_schedule_queue: int = 0
    continuity_schedule_late_count: int = 0
    continuity_schedule_applied_count: int = 0
    xruns: int = 0
    bridge_fill_ms: float = 0.0
    bridge_target_ms: float = 0.0
    dsp_max_ms: float = 0.0
    capture_gap_max_ms: float = 0.0
    drift_correction_ppm: float = 0.0
    concealments: int = 0
    dsp_backend: str = ""
    spectrum: list[float] = field(default_factory=list)
    ai_enabled: bool = True
    ai_ready: bool = False
    ai_state: str = "waiting"
    ai_model: str = "laion/clap-htsat-unfused"
    ai_detail_model: str = ""
    ai_detail_confidence: float = 0.0
    ai_device: str = ""
    ai_inference_ms: float = 0.0
    ai_last_update: float = 0.0
    ai_scales: dict[str, float] = field(default_factory=dict)
    ai_delta: dict[str, float] = field(default_factory=dict)
    features: dict[str, float] = field(default_factory=dict)
    dsp_params: dict[str, float] = field(default_factory=dict)
    effect_intensity: float = 1.0
    neural_polish_enabled: bool = False
    neural_polish_wet: float = 0.0
    neural_polish_ready: bool = False
    neural_polish_rtf: float = 0.0
    neural_polish_inference_ms: float = 0.0
    neural_polish_delay_ms: float = 0.0
    neural_polish_hold_ms: float = 0.0
    neural_polish_mix: float = 0.0
    neural_polish_deadline_misses: int = 0
    neural_polish_overload_bypasses: int = 0
    neural_polish_message: str = "GPU restoration removed"
    dj_mode: bool = False
    dj_prompt: str = ""
    dj_score: float = 0.0
    dj_cycle: int = 0
    dj_update_hz: float = 0.0
    dj_window_ms: float = 0.0
    dj_result_age_ms: float = 0.0
    dj_feature_gradients: dict[str, float] = field(default_factory=dict)
    dj_node_gradients: dict[str, float] = field(default_factory=dict)
    dj_node_values: dict[str, float] = field(default_factory=dict)

    dsp50_mode: bool = False
    dsp50_prompt: str = ""
    dsp50_intensity: float = 1.45
    dsp50_score: float = 0.0
    dsp50_cycle: int = 0
    dsp50_gradients: dict[str, float] = field(default_factory=dict)
    dsp50_confidence: dict[str, float] = field(default_factory=dict)
    dsp50_node_values: dict[str, float] = field(default_factory=dict)
    dsp50_active_nodes: list[str] = field(default_factory=list)
    dsp50_optimizer: str = "Projected AdaBelief"
    dsp50_update_hz: float = 0.0
    dsp50_window_ms: float = 0.0
    dsp50_result_age_ms: float = 0.0
    dsp50_render_count: int = 0


_PROCESS_RESTORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime", "process_capture_volume_restore.json")


def _same_name_root_pid(pid: int) -> int:
    """Return the highest live ancestor that uses the same executable name."""
    try:
        import psutil
        cur = psutil.Process(int(pid))
        name = (cur.name() or "").lower()
        while True:
            parent = cur.parent()
            if not parent or (parent.name() or "").lower() != name:
                return int(cur.pid)
            cur = parent
    except Exception:
        return int(pid)


def _pid_tree(root_pid: int) -> set[int]:
    ids = {int(root_pid)}
    try:
        import psutil
        root = psutil.Process(int(root_pid))
        ids.update(int(p.pid) for p in root.children(recursive=True))
    except Exception:
        pass
    return ids


def _restore_stale_process_volumes() -> None:
    """Recover source-session state left by any previous JoyMetric build.

    Previous versions lived in sibling version folders. start.ps1 may terminate
    an older Flask process by port before its audio-stream close handler gets a
    chance to restore Spotify. v30.4.33 therefore scans the current runtime and
    sibling JoyMetric runtimes for saved exact volume/mute state before opening
    any new APP TAP.
    """
    if os.name != "nt":
        return
    try:
        from pathlib import Path
        here = Path(__file__).resolve().parent
        paths = [Path(_PROCESS_RESTORE_FILE)]
        try:
            paths.extend(here.parent.glob("JoyMetric_Workstation_UI_*/runtime/process_capture_volume_restore.json"))
        except Exception:
            pass
        # unique, newest first so the most recent saved state wins if duplicate
        uniq = {}
        for path in paths:
            try:
                if path.exists():
                    uniq[str(path.resolve()).lower()] = path
            except Exception:
                pass
        paths = sorted(uniq.values(), key=lambda x: x.stat().st_mtime if x.exists() else 0.0, reverse=True)
        if not paths:
            return

        payloads = []
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                payloads.append((path, payload))
            except Exception:
                continue
        if not payloads:
            return

        import comtypes
        from pycaw.pycaw import AudioUtilities
        comtypes.CoInitialize()
        try:
            sessions = list(AudioUtilities.GetAllSessions())
            restored_keys = set()
            for path, payload in payloads:
                wanted_by_id = {
                    str(x.get("instance_id") or ""): x
                    for x in payload.get("sessions", []) if x.get("instance_id")
                }
                wanted_by_pid = {
                    int(x.get("pid") or 0): x
                    for x in payload.get("sessions", []) if int(x.get("pid") or 0) > 0
                }
                for session in sessions:
                    try:
                        key = str(session.InstanceIdentifier or "")
                        pid = int(session.ProcessId or 0)
                        saved = wanted_by_id.get(key) or wanted_by_pid.get(pid)
                        if not saved:
                            continue
                        dedupe = key or f"pid:{pid}"
                        if dedupe in restored_keys:
                            continue
                        vol = session.SimpleAudioVolume
                        vol.SetMasterVolume(float(saved.get("volume", 1.0)), None)
                        vol.SetMute(bool(saved.get("mute", False)), None)
                        restored_keys.add(dedupe)
                    except Exception:
                        continue
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

        for path, _ in payloads:
            try:
                path.unlink()
            except Exception:
                pass
    except Exception:
        pass





class _SelfAudioSessionVolumeGuard:
    """Keep JoyMetric's own Windows render session at unity while APP TAP is LIVE.

    Windows Volume Mixer persists per-application volume. After switching from a
    virtual mixer route to direct JoyMetric playback, a stale low Python/JoyMetric
    session can make correct float32 audio sound much quieter. This guard touches
    only the current JoyMetric process and restores the user's previous value on
    Stop.
    """

    def __init__(self):
        self.pid = int(os.getpid())
        self._saved = {}
        self._last_refresh = 0.0

    def enable(self):
        if os.name != "nt":
            return 0
        try:
            from pycaw.pycaw import AudioUtilities
            changed = 0
            for session in AudioUtilities.GetAllSessions():
                try:
                    if int(session.ProcessId or 0) != self.pid:
                        continue
                    simple = session.SimpleAudioVolume
                    if simple is None:
                        continue
                    key = str(session.InstanceIdentifier or f"self:{id(simple)}")
                    if key not in self._saved:
                        self._saved[key] = (simple, float(simple.GetMasterVolume()), bool(simple.GetMute()))
                    simple.SetMute(False, None)
                    simple.SetMasterVolume(1.0, None)
                    changed += 1
                except Exception:
                    continue
            self._last_refresh = time.time()
            return changed
        except Exception:
            return 0

    def refresh(self):
        if time.time() - self._last_refresh >= 1.0:
            self.enable()

    def close(self):
        for simple, volume, mute in list(self._saved.values()):
            try:
                simple.SetMasterVolume(float(volume), None)
                simple.SetMute(bool(mute), None)
            except Exception:
                pass
        self._saved.clear()



class _PerAppRenderRouter:
    """Route a selected Windows app to JoyMetric's private render endpoint.

    v30.4.44 rules:
      * resolve the exact SoundVolumeCommandLine-friendly render ID once;
      * set the persisted per-app render preference once at stream start;
      * NEVER launch routing subprocesses from the realtime audio read path.

    Windows keeps the per-application endpoint preference. Re-applying it every
    few seconds is both unnecessary and actively harmful because it can force an
    active Spotify audio client to migrate/reopen while the capture thread is
    waiting on a child process.
    """

    def __init__(self, root_pid: int, target_device: str = ""):
        self.root_pid = int(root_pid)
        self.target_device = str(target_device or "").strip()
        self.exe = str(os.environ.get("JOY_SVCL_EXE", "") or "").strip()
        self.enabled = False
        self.last_error = ""
        self._targets = []
        self._exact_device_id = ""
        self._route_stdout = ""

    def _process_targets(self):
        """Return stable app name first, then current live Spotify-style PIDs."""
        out = []
        seen = set()
        names = []
        pids = []
        try:
            import psutil
            root = psutil.Process(self.root_pid)
            procs = [root] + root.children(recursive=True)
            for proc in procs:
                try:
                    pid = int(proc.pid)
                    name = str(proc.name() or "").strip()
                except Exception:
                    continue
                if name:
                    names.append(name)
                pids.append(str(pid))
        except Exception:
            pids.append(str(self.root_pid))

        # A process-name preference is persistent across Spotify child/session
        # recreation. Always include Spotify.exe for the agentic/live source so
        # a short-lived PyCAW session/PID rotation cannot break LET'S GO.
        if any("spotify" in str(n).lower() for n in names):
            names = ["Spotify.exe"] + names
        for target in names + pids:
            key = str(target).lower()
            if target and key not in seen:
                seen.add(key)
                out.append(str(target))
        return out

    def _run(self, *args, stdout_items: bool = False, timeout: float = 5.0):
        if not self.exe or not os.path.isfile(self.exe):
            raise RuntimeError("Windows app-routing helper is not installed")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        cmd = [self.exe]
        if stdout_items:
            # SoundVolumeCommandLine explicitly supports /Stdout on set commands;
            # it prints the sound items matched by the command.
            cmd.append("/Stdout")
        cmd.extend(str(x) for x in args)
        cp = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=float(timeout),
            creationflags=flags,
            text=True,
            errors="replace",
        )
        if cp.returncode != 0:
            msg = (cp.stderr or cp.stdout or f"exit {cp.returncode}").strip()
            raise RuntimeError(msg[:700])
        return (cp.stdout or "").strip()

    @staticmethod
    def _norm(s: str) -> str:
        import re
        return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())

    def _resolve_exact_render_id(self) -> str:
        """Resolve JoyMetric's exact SoundVolumeCommandLine-friendly render ID.

        Use a real temporary CSV file rather than relying on an empty export
        filename being treated as stdout. This follows SoundVolumeView/SVCL's
        documented /scomma <Filename> contract and is stable across builds.
        """
        import csv
        import tempfile
        from pathlib import Path

        tmp_name = ""
        rows = []
        try:
            fd, tmp_name = tempfile.mkstemp(prefix="joymetric-svcl-", suffix=".csv")
            os.close(fd)
            self._run(
                "/SaveFileEncoding", "3",
                "/scomma", tmp_name,
                "/Columns", "Type,Name,Device Name,Command-Line Friendly ID,Item ID,Device State,Direction",
                timeout=5.0,
            )
            raw = Path(tmp_name).read_text(encoding="utf-8-sig", errors="replace")
            rows = list(csv.DictReader(raw.splitlines()))
        except Exception:
            rows = []
        finally:
            if tmp_name:
                try: os.unlink(tmp_name)
                except Exception: pass

        candidates = []
        for row in rows:
            vals = {str(k or "").strip().lower(): str(v or "").strip() for k, v in row.items()}
            name = vals.get("name", "")
            dev_name = vals.get("device name", "")
            friendly = vals.get("command-line friendly id", "")
            item_id = vals.get("item id", "")
            typ = vals.get("type", "")
            state = vals.get("device state", "")
            direction = vals.get("direction", "")
            blob = " ".join((name, dev_name, friendly, item_id, typ, state, direction)).lower()
            if "joymetric" not in blob:
                continue
            is_render = ("\\device\\speakers\\render" in friendly.lower() or
                         "\\device\\speakers\\render" in item_id.lower() or
                         "render" in direction.lower() or "speaker" in name.lower())
            if not is_render:
                continue
            candidate = friendly or item_id
            if candidate:
                score = 0
                if "joymetric" in candidate.lower(): score += 8
                if "\\device\\speakers\\render" in candidate.lower(): score += 8
                if "active" in state.lower(): score += 2
                if "render" in direction.lower(): score += 2
                candidates.append((score, candidate))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            return str(candidates[0][1])
        return "*JoyMetric*\\Device\\Speakers\\Render"

    def enable(self) -> bool:
        if self.enabled:
            return True
        try:
            self._targets = self._process_targets()
            if not self._targets:
                # The caller may have resolved a stable Spotify root while the
                # audio session itself is being recreated. The root PID is still
                # a valid /SetAppDefault target.
                self._targets = ["Spotify.exe", str(self.root_pid)]
            elif not any(str(t).lower() == "spotify.exe" for t in self._targets):
                self._targets.insert(0, "Spotify.exe")

            self._exact_device_id = self._resolve_exact_render_id()
            device_ids = []
            for did in (self._exact_device_id, self.target_device, "*JoyMetric*\\Device\\Speakers\\Render"):
                did = str(did or "").strip()
                if did and did.lower() not in {x.lower() for x in device_ids}:
                    device_ids.append(did)

            route_errors = []
            route_stdout = []
            successful_calls = 0
            # Spotify.exe is the durable preference; PIDs make the currently
            # running renderer migrate immediately on Windows builds that cache
            # a per-process preference. A zero exit code is a successful set
            # operation even if /Stdout prints no matched item.
            targets = []
            for t in ["Spotify.exe"] + list(self._targets) + [str(self.root_pid)]:
                if t and str(t).lower() not in {str(x).lower() for x in targets}:
                    targets.append(str(t))
            for did in device_ids:
                for target in targets:
                    try:
                        out = self._run(
                            "/SetAppDefault", did, "all", target,
                            stdout_items=True, timeout=5.0,
                        )
                        successful_calls += 1
                        if out:
                            route_stdout.append(out)
                    except Exception as exc:
                        route_errors.append(f"{did} -> {target}: {exc}")
                if successful_calls:
                    self._exact_device_id = did
                    break

            if successful_calls <= 0:
                detail = " | ".join(route_errors[-6:])
                raise RuntimeError(
                    "Windows rejected the Spotify → JoyMetric per-app output route" +
                    (f" ({detail})" if detail else "")
                )

            self.target_device = self._exact_device_id
            self._route_stdout = "\n".join(route_stdout)[:1500]
            self.enabled = True
            self.last_error = ""
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self.enabled = False
            return False

    def refresh(self, force: bool = False):
        # Intentionally NO-OP in v30.4.44.
        # Never launch svcl/COM/process enumeration from the realtime capture path.
        return

    def disable(self):
        if not self.enabled:
            return
        # Restore the durable process-name preference first. This call happens only
        # during Stop/close, never from an audio thread's per-block path.
        targets = self._targets or self._process_targets()
        restored_names = set()
        for target in targets:
            key = str(target).lower()
            if key in restored_names:
                continue
            try:
                self._run("/SetAppDefault", "DefaultRenderDevice", "all", target, timeout=4.0)
                restored_names.add(key)
                if not str(target).isdigit():
                    break
            except Exception:
                pass
        self.enabled = False

    def close(self):
        self.disable()




def _find_managed_virtual_channel(p, pyaudio):
    """Resolve JoyMetric's private render endpoint and its WASAPI loopback.

    v30.4.44 captures the loopback of the JoyMetric render/output endpoint
    instead of relying on the driver's paired microphone/ring endpoint. The
    virtual render endpoint is still JoyMetric's own WDM/WaveRT driver and is
    the endpoint Spotify is routed to; WASAPI loopback simply exposes the exact
    mixed PCM already entering that private sink. Internal endpoints remain
    hidden from the UI.
    """
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        wasapi_index = int(wasapi.get("index", -1))
    except Exception:
        wasapi_index = -1
    render = []
    loopbacks = []
    paired_capture = []
    devices = []
    for i in range(int(p.get_device_count())):
        try:
            d = p.get_device_info_by_index(i)
        except Exception:
            continue
        if wasapi_index >= 0 and int(d.get("hostApi", -2)) != wasapi_index:
            continue
        name = str(d.get("name") or "").strip()
        if not name:
            continue
        low = name.lower()
        devices.append((i, name, low, d))
        joy = "joymetric" in low
        is_loop = bool(d.get("isLoopbackDevice", False))
        if joy and int(d.get("maxOutputChannels") or 0) >= 2 and not is_loop:
            render.append((0 if "virtual input" in low else 1, i, name, d))
        if joy and is_loop and int(d.get("maxInputChannels") or 0) >= 2:
            loopbacks.append((0 if "virtual input" in low else 1, i, name, d))
        if joy and int(d.get("maxInputChannels") or 0) >= 2 and not is_loop:
            paired_capture.append((0 if "internal capture" in low else 1, i, name, d))
    if not render:
        return None
    render.sort(key=lambda x: (x[0], x[1]))
    rr = render[0]

    # Some PyAudioWPatch builds decorate the loopback device with the underlying
    # render name but omit the INF-friendly JoyMetric prefix. If the direct name
    # match missed it, pair by normalized render label.
    if not loopbacks:
        rlow = str(rr[2]).lower().replace("[loopback]", "").replace("(loopback)", "").strip()
        for i, name, low, d in devices:
            if not bool(d.get("isLoopbackDevice", False)) or int(d.get("maxInputChannels") or 0) < 2:
                continue
            clean = low.replace("[loopback]", "").replace("(loopback)", "").strip()
            if rlow and (rlow in clean or clean in rlow):
                loopbacks.append((0, i, name, d))
    loopbacks.sort(key=lambda x: (x[0], x[1]))
    paired_capture.sort(key=lambda x: (x[0], x[1]))
    ll = loopbacks[0] if loopbacks else None
    cc = paired_capture[0] if paired_capture else None
    return {
        "render_index": int(rr[1]),
        "render_name": str(rr[2]),
        "loopback_index": int(ll[1]) if ll else None,
        "loopback_name": str(ll[2]) if ll else "",
        "capture_index": int(cc[1]) if cc else None,
        "capture_name": str(cc[2]) if cc else "",
        "sample_rate": 48000,
        "transport": "JoyMetric WDM/WaveRT virtual render + private WASAPI loopback",
    }


class _Int16ToFloat32InputStream:
    """Expose a signed PCM16 WASAPI loopback stream as float32 DSP bytes."""
    def __init__(self, inner):
        self.inner = inner

    def start_stream(self):
        return self.inner.start_stream()

    def stop_stream(self):
        return self.inner.stop_stream()

    def is_active(self):
        return self.inner.is_active()

    def get_input_latency(self):
        try:
            return self.inner.get_input_latency()
        except Exception:
            return 0.0

    def read(self, *args, **kwargs):
        raw = self.inner.read(*args, **kwargs)
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        x *= np.float32(1.0 / 32768.0)
        return np.ascontiguousarray(x, dtype=np.float32).tobytes()

    def close(self):
        return self.inner.close()


class _Int32ToFloat32InputStream:
    """Expose a 32-bit PCM WASAPI capture stream as float32 bytes to the DSP."""
    def __init__(self, inner):
        self.inner = inner

    def start_stream(self):
        return self.inner.start_stream()

    def stop_stream(self):
        return self.inner.stop_stream()

    def is_active(self):
        return self.inner.is_active()

    def get_input_latency(self):
        try:
            return self.inner.get_input_latency()
        except Exception:
            return 0.0

    def read(self, *args, **kwargs):
        raw = self.inner.read(*args, **kwargs)
        x = np.frombuffer(raw, dtype=np.int32).astype(np.float32)
        x *= np.float32(1.0 / 2147483648.0)
        return np.ascontiguousarray(x, dtype=np.float32).tobytes()

    def close(self):
        return self.inner.close()


class _ManagedVirtualChannelInputStream:
    """PyAudio-like stream for app -> virtual cable -> JoyMetric capture.

    The app is moved to JoyMetric Virtual Input before capture starts.
    JoyMetric then reads the private WASAPI loopback of that same render
    endpoint. On close, the app is returned to Windows' default render endpoint.
    """
    is_process_capture = True
    is_managed_virtual_channel = True

    def __init__(self, inner, root_pid: int, router: _PerAppRenderRouter, channel_info: dict):
        self.inner = inner
        self.root_pid = int(root_pid)
        self.router = router
        self.channel_info = dict(channel_info or {})
        self.source_suppressed = True  # dry physical path is absent by routing, not mute
        self.volume_path = "processed-only · Spotify → JoyMetric Virtual Input → private WASAPI loopback"
        self._started = False
        self._closed = False

    def start_stream(self):
        if self._started:
            return
        # Arm the private loopback first. This makes the JoyMetric render endpoint
        # fully active before Windows migrates Spotify's audio session to it.
        self.inner.start_stream()
        try:
            if not self.router.enable():
                raise RuntimeError(
                    "Could not route Spotify to JoyMetric Virtual Input: " +
                    str(self.router.last_error or "Windows app routing failed")
                )
            # Expose the exact resolved route in status/debug text.
            exact_id = str(getattr(self.router, "_exact_device_id", "") or self.channel_info.get("render_name") or "JoyMetric Virtual Input")
            self.volume_path = f"processed-only · Spotify auto-routed → {exact_id}"
            # Give Windows a brief window to migrate/recreate the live shared-mode
            # audio client. Reads during this interval are harmless zeros.
            time.sleep(0.45)
            self._started = True
        except Exception:
            try:
                self.inner.stop_stream()
            except Exception:
                pass
            raise

    def read(self, *args, **kwargs):
        # Realtime invariant: this method performs only the WASAPI read. Routing
        # subprocesses/process enumeration are forbidden on the audio thread.
        return self.inner.read(*args, **kwargs)

    def get_input_latency(self):
        try:
            return self.inner.get_input_latency()
        except Exception:
            return 0.0

    def is_active(self):
        try:
            return self.inner.is_active()
        except Exception:
            return self._started and not self._closed

    def stop_stream(self):
        try:
            if self._started:
                self.inner.stop_stream()
        finally:
            self._started = False

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.inner.close()
        finally:
            self.router.close()



class _SoundCardOutputStream:
    """PyAudio-like playback wrapper using SoundCard's native Windows WASAPI path.

    This remains a fallback behind the exact PyAudioWPatch endpoint selected by the
    user, so a PortAudio endpoint quirk cannot leave capture alive but Audio Out mute.
    """

    is_soundcard_output = True

    def __init__(self, output_name: str, rate: int, channels: int, blocksize: int):
        self.output_name = str(output_name or "")
        self.rate = int(rate)
        self.channels = int(channels)
        self.blocksize = int(blocksize)
        self._ctx = None
        self._player = None
        self._started = False
        self._closed = False
        self.backend_name = "SoundCard WASAPI"

    @staticmethod
    def _norm(name: str) -> str:
        import re
        return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())

    def _resolve_speaker(self):
        import soundcard as sc
        # First let SoundCard's own fuzzy resolver try the exact PyAudio label.
        try:
            sp = sc.get_speaker(self.output_name)
            if sp is not None:
                return sp
        except Exception:
            pass
        speakers = list(sc.all_speakers())
        if not speakers:
            raise RuntimeError("SoundCard found no Windows playback endpoints.")
        target = self._norm(self.output_name)
        if target:
            import difflib
            scored = []
            for sp in speakers:
                name = str(getattr(sp, "name", ""))
                norm = self._norm(name)
                if not norm:
                    continue
                score = difflib.SequenceMatcher(None, target, norm).ratio()
                if target in norm or norm in target:
                    score += 0.35
                scored.append((score, sp))
            if scored:
                scored.sort(key=lambda x: x[0], reverse=True)
                if scored[0][0] >= 0.42:
                    return scored[0][1]
        try:
            return sc.default_speaker()
        except Exception as exc:
            raise RuntimeError(f"Could not resolve SoundCard Audio Out '{self.output_name}': {exc}") from exc

    def start_stream(self):
        if self._started:
            return
        speaker = self._resolve_speaker()
        # Shared WASAPI is deliberate here: Spotify and JoyMetric can use the same
        # physical endpoint while the app tap isolates the source process.
        self._ctx = speaker.player(
            samplerate=self.rate,
            channels=self.channels,
            blocksize=max(128, self.blocksize),
        )
        self._player = self._ctx.__enter__()
        self._started = True

    def write(self, raw: bytes, exception_on_underflow: bool = False):
        if not self._started:
            self.start_stream()
        import numpy as np
        x = np.frombuffer(raw, dtype=np.float32)
        if x.size == 0:
            return
        usable = (x.size // self.channels) * self.channels
        if usable <= 0:
            return
        x = x[:usable].reshape(-1, self.channels)
        # SoundCard works natively with float32 frame x channel arrays.
        self._player.play(x)

    def stop_stream(self):
        if not self._started:
            return
        try:
            if self._ctx is not None:
                self._ctx.__exit__(None, None, None)
        finally:
            self._ctx = None
            self._player = None
            self._started = False

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.stop_stream()

    def get_output_latency(self):
        try:
            return float(getattr(self._player, "latency", 0.0) or 0.0)
        except Exception:
            return 0.0


class _SoundDeviceOutputStream:
    """Independent PortAudio/sounddevice WASAPI playback path.

    PyAudioWPatch and SoundCard can both enumerate a Windows endpoint yet still hit
    machine-specific playback quirks. This wrapper resolves the selected endpoint
    through sounddevice's own WASAPI device table and provides a third implementation.
    """

    is_sounddevice_output = True

    def __init__(self, output_name: str, rate: int, channels: int, blocksize: int, device_index: int | None = None, exact_selected: bool = False):
        self.output_name = str(output_name or "")
        self.rate = int(rate)
        self.channels = int(channels)
        self.blocksize = int(blocksize)
        self.device_index = int(device_index) if device_index is not None else None
        self._stream = None
        self._started = False
        self._closed = False
        self.backend_name = "sounddevice WASAPI · safe buffered"
        self.is_exact_selected_output = bool(exact_selected)

    @staticmethod
    def _norm(name: str) -> str:
        import re
        return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())

    def _resolve_device(self):
        import sounddevice as sd
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        if self.device_index is not None:
            i = int(self.device_index)
            if i < 0 or i >= len(devices):
                raise RuntimeError(f"Selected sounddevice output index disappeared: {i}")
            d = devices[i]
            if int(d.get('max_output_channels', 0) or 0) < 1:
                raise RuntimeError(f"Selected sounddevice endpoint is not an output: {self.output_name}")
            try:
                api_name = str(hostapis[int(d.get('hostapi', -1))].get('name', ''))
            except Exception:
                api_name = ''
            if 'wasapi' not in api_name.lower():
                raise RuntimeError(f"Selected endpoint is no longer a WASAPI output: {self.output_name}")
            return i
        target = self._norm(self.output_name)
        scored = []
        import difflib
        for i, d in enumerate(devices):
            if int(d.get("max_output_channels", 0) or 0) < 1:
                continue
            try:
                api_name = str(hostapis[int(d.get("hostapi", -1))].get("name", ""))
            except Exception:
                api_name = ""
            if "wasapi" not in api_name.lower():
                continue
            name = str(d.get("name", ""))
            norm = self._norm(name)
            if not norm:
                continue
            score = difflib.SequenceMatcher(None, target, norm).ratio() if target else 0.0
            if target and (target in norm or norm in target):
                score += 0.40
            scored.append((score, i, name))
        if scored:
            scored.sort(reverse=True, key=lambda x: x[0])
            if scored[0][0] >= 0.48:
                return int(scored[0][1])
        raise RuntimeError(f"sounddevice could not map selected WASAPI output: {self.output_name}")

    def start_stream(self):
        if self._started:
            return
        import sounddevice as sd
        dev = self._resolve_device()
        self._stream = sd.RawOutputStream(
            samplerate=self.rate,
            blocksize=max(128, self.blocksize),
            device=dev,
            channels=self.channels,
            dtype="float32",
            latency="high",
        )
        self._stream.start()
        self._started = True

    def write(self, raw: bytes, exception_on_underflow: bool = False):
        if not self._started:
            self.start_stream()
        self._stream.write(raw)

    def stop_stream(self):
        if not self._started:
            return
        try:
            self._stream.stop()
        finally:
            self._started = False

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._stream is not None:
                try:
                    self._stream.stop()
                except Exception:
                    pass
                self._stream.close()
        finally:
            self._stream = None
            self._started = False

    def get_output_latency(self):
        try:
            return float(getattr(self._stream, "latency", 0.0) or 0.0)
        except Exception:
            return 0.0


class _FailoverOutputStream:
    """Runtime failover across independent Windows playback implementations."""

    def __init__(self, factories):
        self._factories = list(factories)
        self._index = -1
        self._stream = None
        self._started = False
        self.backend_name = "pending WASAPI failover"
        self.last_errors = []

    def _close_current(self):
        st = self._stream
        self._stream = None
        self._started = False
        if st is not None:
            for fn in ("stop_stream", "close"):
                try:
                    getattr(st, fn)()
                except Exception:
                    pass

    def _advance_and_start(self):
        self._close_current()
        while self._index + 1 < len(self._factories):
            self._index += 1
            label, factory = self._factories[self._index]
            st = None
            try:
                st = factory()
                st.start_stream()
                self._stream = st
                self._started = True
                self.backend_name = str(getattr(st, "backend_name", label))
                return
            except Exception as exc:
                self.last_errors.append(f"{label}: {exc}")
                try:
                    st.close()
                except Exception:
                    pass
        raise RuntimeError("All Audio Out backends failed: " + " | ".join(self.last_errors[-6:]))

    def start_stream(self):
        if not self._started:
            self._advance_and_start()

    def write(self, raw: bytes, exception_on_underflow: bool = False):
        if not self._started:
            self.start_stream()
        try:
            self._stream.write(raw, exception_on_underflow=exception_on_underflow)
            self.backend_name = str(getattr(self._stream, "backend_name", self.backend_name))
        except TypeError:
            try:
                self._stream.write(raw)
            except Exception:
                self._advance_and_start()
                try:
                    self._stream.write(raw, exception_on_underflow=exception_on_underflow)
                except TypeError:
                    self._stream.write(raw)
        except Exception:
            self._advance_and_start()
            try:
                self._stream.write(raw, exception_on_underflow=exception_on_underflow)
            except TypeError:
                self._stream.write(raw)

    @property
    def is_exact_selected_output(self):
        return bool(getattr(self._stream, "is_exact_selected_output", False)) if self._stream is not None else False

    def stop_stream(self):
        self._close_current()

    def close(self):
        self._close_current()

    def get_output_latency(self):
        try:
            return float(self._stream.get_output_latency()) if self._stream else 0.0
        except Exception:
            return 0.0


class NativeRealtimeEngine:
    """Windows realtime bridge using JoyMetric's own virtual audio driver.

    App sources are routed to JoyMetric Virtual Input, captured from that private
    render endpoint's WASAPI loopback, processed by the existing DSP graph, and
    returned only to the physical Audio Out selected by the user.  The two internal
    driver endpoints are deliberately hidden from the UI.
    """

    def __init__(self, ai_host: str = "127.0.0.1", ai_port: int = 8767):
        _restore_stale_process_volumes()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._ai_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._ai_stop_event = threading.Event()
        self._features: dict[str, float] = {name: 0.0 for name in FEATURE_NAMES}
        self._ai_scales: dict[str, float] = {name: 1.0 for name in FEATURE_NAMES}
        # Global 15D effect macro. UI writes the target; the realtime audio thread
        # glides the current value so moving the intensity slider cannot click.
        self._effect_intensity = 1.0
        self._effect_intensity_target = 1.0
        # v30.4.24: GPU restoration remains removed; 15D render authority is 3x v30.4.23. The native full-band StudioDSP
        # render is the final audible signal; no neural reconstruction stage exists.
        self._neural_polish_enabled = False
        self._neural_polish_wet = 0.0
        self._neural_polish_worker = None
        self._neural_polish_mix = 0.0
        self._neural_polish_hold_sec = 0.0
        self._params = feature_params(self._features, self._ai_scales, self._effect_intensity)
        self._status = EngineStatus(ai_scales=dict(self._ai_scales), features=dict(self._features), dsp_params=dict(self._params))
        self._last_error = ""
        self._ai_host = str(ai_host or "127.0.0.1")
        self._ai_port = int(ai_port or 8767)
        self._ai_enabled = True
        self._dj_mode = False
        self._dj_prompt = ""
        self._dj_target_features: dict[str, float] = {name: 0.0 for name in FEATURE_NAMES}
        self._dj_feature_gradients: dict[str, float] = {name: 0.0 for name in FEATURE_NAMES}
        self._dj_node_gradients: dict[str, float] = {name: 0.0 for name in DJ_NODE_SPECS}
        self._dj_node_offsets: dict[str, float] = {name: 0.0 for name in DJ_NODE_SPECS}
        self._dj_render_target_features: dict[str, float] = dict(self._dj_target_features)
        self._dj_feature_schedule: list[tuple[int, dict[str, float], str]] = []
        self._dj_schedule_applied_count = 0
        self._dj_schedule_late_count = 0
        self._dj_node_cursor = 0
        self._dj_cycle = 0
        self._dj_score = 0.0
        self._dj_last_pass_time = 0.0
        self._dj_last_window_ms = 0.0
        self._dj_last_result_age_ms = 0.0
        self._dj_profile_applied_prompt = ""

        # Separate 50D direct-DSP gradient lab. Unlike DJ Gradient Mode, this mode
        # does not project through the 15 musical features: CLAP derivatives update
        # the 50 physical DSP coordinates themselves.
        self._dsp50_mode = False
        self._dsp50_prompt = ""
        self._dsp50_intensity = 1.45
        self._dsp50_params: dict[str, float] = dsp50_safety_params(DEFAULT_PARAMS)
        # Prompt-compiled anchor. Live CLAP/AdaBelief is allowed to make only a
        # small current-song correction around this base, preventing section-to-
        # section hunting while preserving the strong x5 prompt character.
        self._dsp50_base_params: dict[str, float] = dict(self._dsp50_params)
        self._dsp50_gradients: dict[str, float] = {name: 0.0 for name in DSP50_SPECS}
        self._dsp50_confidence: dict[str, float] = {name: 0.0 for name in DSP50_SPECS}
        # Projected AdaBelief state in normalized [0,1] DSP coordinates. Unlike
        # plain momentum/Adam, the second moment tracks gradient *surprise*; noisy
        # SPSA directions therefore get smaller steps while persistent current-
        # section gradients get larger, more audible updates.
        self._dsp50_adam_m: dict[str, float] = {name: 0.0 for name in DSP50_SPECS}
        self._dsp50_adam_s: dict[str, float] = {name: 0.0 for name in DSP50_SPECS}
        self._dsp50_adam_t: int = 0
        self._dsp50_last_pass_time: float = 0.0
        self._dsp50_last_window_ms: float = 0.0
        self._dsp50_last_result_age_ms: float = 0.0
        self._dsp50_last_render_count: int = 0
        self._dsp50_coord_cursor = 0
        self._dsp50_cycle = 0
        self._dsp50_score = 0.0
        self._dsp50_active_nodes: list[str] = []
        self._dsp50_profile_applied_prompt = ""

        # v30.4.3 semantic/audio timeline.  _params remains the newest semantic
        # target for the UI and analyser; the audio thread consumes a separate
        # timestamped render queue keyed to source-frame time.
        self._capture_frame_index: int = 0
        self._render_source_frame_index: int = 0
        self._dsp50_render_params: dict[str, float] = dict(self._params)
        self._dsp50_param_schedule: list[tuple[int, dict[str, float], str]] = []
        self._dsp50_schedule_late_count: int = 0
        self._dsp50_schedule_applied_count: int = 0

        self._ai_in_ring: AudioRing | None = None
        self._ai_out_ring: AudioRing | None = None
        self._dj_stereo_ring: StereoAudioRing | None = None
        # Delayed/source-time ring: what is about to be heard after the intentional
        # lookahead. Agent planning reads _dj_stereo_ring; execution safety/beat lock
        # reads this ring so 15 s foresight never shifts an FX event 15 s early.
        self._dj_render_ring: StereoAudioRing | None = None
        self._ai_sample_rate = 0
        self._last_feature_snapshot = dict(self._features)
        self._playback_thread: threading.Thread | None = None
        # v31.8.1 startup handshake: physical Audio Out is started/validated
        # before Spotify is migrated to JoyMetric Virtual Input.  Older builds
        # deferred outstream.start_stream() until the 15 s lookahead FIFO filled,
        # so a stale/Bluetooth WASAPI endpoint could fail only after ~15 s and
        # make Spotify suddenly fall back to its original device.
        self._playback_ready_event = threading.Event()
        # v31.5.1 runtime CrackleGuard. Playback underruns arm a temporary
        # performance-FX shedder; the dry/core DJ path stays continuous and full
        # quality while the expensive additive/send layers recover gradually.
        self._fx_guard_level: float = 0.0
        self._fx_guard_last_xrun: float = 0.0
        self._playback_error = ""
        self._meter_thread: threading.Thread | None = None
        self._meter_stop_event = threading.Event()
        self._meter_lock = threading.Lock()
        self._meter_latest = None
        self._meter_rate = 0
        # v31.1 Realtime Agentic DJ: traditional DJ-controller overlay lives
        # beside the existing MusicCLAP/15D semantic engine. Planning is external;
        # the audio thread receives only bounded numeric targets.
        self._agentic_live_enabled = False
        self._agentic_controls: dict[str, Any] = {
            "enabled": False, "bpm": 100.0,
            "a_low_db":0.0,"a_mid_db":0.0,"a_high_db":0.0,"a_filter":0.0,"a_gain":1.0,
            "b_low_db":0.0,"b_mid_db":0.0,"b_high_db":0.0,"b_filter":0.0,"b_gain":1.0,
            "crossfader":0.0,"b_layer":0.0,"b_texture":0.0,"b_transient":0.0,"b_slicer_mix":0.0,"b_slice_mode":0.0,"b_fx":0.0,"reverb":0.0,"echo":0.0,"loop_beats":4.0,
            "noise_riser":0.0,"fx_reverse_swell":0.0,"fx_snare_rush":0.0,"impact_strength":0.0,"drum_drive":0.0,"doubletime":0.0,
            "fx_rack_wet":0.0,"fx_echo_send":0.0,"fx_echo_feedback":0.0,"fx_echo_beats":0.5,"fx_gate":0.0,"fx_gate_div":2.0,"fx_width":0.0,"fx_duck":0.0,
            "perf_punch":0.0,"perf_energy":0.0,"perf_clarity":0.0,"perf_air":0.0,"bass_tighten":0.0,
            "fx_pump":0.0,"fx_pump_cycle":1.0,"fx_bass_cut":0.0,"fx_rush_accel":0.0,"fx_brake_beats":1.0,"fx_brake_mode":0.0,
            "fx_redrum":0.0,"fx_redrum_pattern":0.0,
            "fx_swing":0.0,"grid_conf":0.0,"grid_anchor_t":0.0,"grid_lead_ms":0.0,
            "fx_redrum_var":0.0,"fx_redrum_fill":0.0,
            "fx_bass_synth":0.0,"fx_bass_pattern":0.0,"fx_bass_root":9.0,"fx_bass_conf":0.0,
            "fx_stab":0.0,"fx_stab_pattern":0.0,
            "fx_weave_offset_beats":16.0,"fx_weave_cell_beats":2.0,
            "fx_hat_style":0.0,"fx_hat_var":0.5,"fx_hat_fill":0.5,
            "loop_capture_seq":0,"loop_release_seq":0,"loop_retrigger_seq":0,"impact_seq":0,"fx_brake_seq":0,"fx_pump_sync_seq":0,
        }
        self._agentic_mixer_status: dict[str, Any] = {"loop_ready":False,"loop_frames":0,"loop_beats":0.0,"loop_age_sec":0.0,"crossfader":0.0,"b_layer":0.0,"b_texture":0.0,"b_transient":0.0,"b_slicer_mix":0.0,"b_slice_mode":"OFF","b_slice_map":[0,1,2,3,4,5,6,7],"b_slice_motion":0.0,"b_fx":0.0,"deckb_mode":"FULL","correlation":0.0,"noise_riser":0.0,"fx_reverse_swell":0.0,"fx_snare_rush":0.0,"impact_strength":0.0,"drum_drive":0.0,"doubletime":0.0,"impact_env":0.0,"rack_wet":0.0,"beat_echo":0.0,"echo_feedback":0.0,"echo_beats":0.5,"rhythm_gate":0.0,"gate_div":2.0,"width":0.0,"duck":0.0,"tail_rms":0.0}
        _raise_audio_process_priority()

    @staticmethod
    def _imports():
        try:
            import numpy as np  # noqa: F401
            import pyaudiowpatch as pyaudio
        except Exception as exc:
            raise RuntimeError(
                "Native realtime audio dependency is missing. Restart JoyMetric so start.ps1 can install PyAudioWPatch. "
                f"({exc})"
            ) from exc
        return pyaudio

    def _list_process_audio_sources(self) -> tuple[list[dict[str, Any]], str]:
        if os.name != "nt":
            return [], "JoyMetric app routing is Windows-only."
        try:
            import comtypes
            import psutil
            from pycaw.pycaw import AudioUtilities
            comtypes.CoInitialize()
            grouped = {}
            try:
                for session in AudioUtilities.GetAllSessions():
                    try:
                        pid = int(session.ProcessId or 0)
                        proc = session.Process
                        if pid <= 4 or not proc or pid == os.getpid():
                            continue
                        name = str(proc.name() or f"PID {pid}")
                        low = name.lower()
                        if low in {"audiodg.exe", "system", "system sounds"}:
                            continue
                        root_pid = _same_name_root_pid(pid)
                        state = getattr(session, "State", 0)
                        state_text = str(state or "").lower()
                        active = bool(state == 1 or "active" in state_text)
                        item = grouped.setdefault(root_pid, {"name": name, "active": False, "session_pids": set()})
                        item["session_pids"].add(pid)
                        item["active"] = bool(item["active"] or active)
                    except Exception:
                        continue
            finally:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass
            rows = []
            for root_pid, item in grouped.items():
                display = os.path.splitext(item["name"])[0]
                rows.append({
                    "id": f"app:{int(root_pid)}",
                    "pid": int(root_pid),
                    "label": f"{display}  ·  AUTO → JOYMETRIC INTERNAL DRIVER",
                    "sample_rate": 48000,
                    "input_channels": 2,
                    "output_channels": 0,
                    "loopback": False,
                    "app_capture": True,
                    "source_kind": "joymetric_virtual_driver",
                    "active": bool(item["active"]),
                })
            def prio(x):
                label = x["label"].lower()
                spotify = 0 if "spotify" in label else 1
                active = 0 if x.get("active") else 1
                return (spotify, active, label)
            rows.sort(key=prio)
            return rows, ""
        except Exception as exc:
            return [], str(exc)

    def list_devices(self) -> dict[str, Any]:
        pyaudio = self._imports()
        p = pyaudio.PyAudio()
        try:
            try:
                wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            except OSError as exc:
                raise RuntimeError("Windows WASAPI is not available on this machine.") from exc
            host_index = int(wasapi["index"])
            inputs, outputs = [], []
            for i in range(p.get_device_count()):
                try:
                    d = p.get_device_info_by_index(i)
                except Exception:
                    continue
                if int(d.get("hostApi", -1)) != host_index:
                    continue
                name = str(d.get("name") or f"Device {i}").strip()
                loopback = bool(d.get("isLoopbackDevice", False))
                # JoyMetric's render/capture endpoints are private transport plumbing.
                # Never ask the user to select either end.
                low_name = name.lower()
                if "joymetric" in low_name:
                    continue
                base = {
                    "id": str(i),
                    "index": int(i),
                    "label": name,
                    "sample_rate": int(float(d.get("defaultSampleRate") or 48000)),
                    "input_channels": int(d.get("maxInputChannels") or 0),
                    "output_channels": int(d.get("maxOutputChannels") or 0),
                    "loopback": loopback,
                }
                if loopback and base["input_channels"] > 0:
                    clean = name.replace("[Loopback]", "").replace("(loopback)", "").strip(" -")
                    item = dict(base)
                    item["label"] = f"{clean}  ·  LOOPBACK"
                    item["source_kind"] = "loopback"
                    inputs.append(item)
                elif base["input_channels"] > 0:
                    item = dict(base)
                    item["source_kind"] = "input"
                    inputs.append(item)
                if base["output_channels"] > 0 and not loopback:
                    outputs.append(dict(base))

            # Some Bluetooth/USB headphones are exposed by Windows WASAPI through
            # sounddevice but are missing from a particular PyAudioWPatch build.
            # Merge only *missing* WASAPI playback endpoints, preserving the exact
            # PyAudio index for endpoints already known there. IDs prefixed sd: are
            # opened through sounddevice by exact device index, never fuzzy-matched.
            try:
                import re, sounddevice as sd
                sd_devices = sd.query_devices()
                sd_apis = sd.query_hostapis()
                def _devnorm(v):
                    return re.sub(r'[^a-z0-9]+', '', str(v or '').lower())
                known = {_devnorm(x.get('label')) for x in outputs}
                for si, sd_d in enumerate(sd_devices):
                    try:
                        if int(sd_d.get('max_output_channels', 0) or 0) < 1:
                            continue
                        api_name = str(sd_apis[int(sd_d.get('hostapi', -1))].get('name', ''))
                        if 'wasapi' not in api_name.lower():
                            continue
                        name = str(sd_d.get('name') or f'WASAPI Output {si}').strip()
                        if 'joymetric' in name.lower():
                            continue
                        norm = _devnorm(name)
                        if norm in known:
                            continue
                        outputs.append({
                            'id': f'sd:{si}', 'index': int(si), 'label': name,
                            'sample_rate': int(float(sd_d.get('default_samplerate') or 48000)),
                            'input_channels': int(sd_d.get('max_input_channels') or 0),
                            'output_channels': int(sd_d.get('max_output_channels') or 0),
                            'loopback': False, 'output_backend': 'sounddevice_wasapi',
                        })
                        known.add(norm)
                    except Exception:
                        continue
            except Exception:
                pass

            process_inputs, process_error = self._list_process_audio_sources()
            inputs.extend(process_inputs)

            def input_priority(item):
                s = item["label"].lower()
                if item.get("app_capture") and "spotify" in s:
                    return (0, s)
                if item.get("app_capture"):
                    return (1, s)
                if item.get("loopback"):
                    return (2, s)
                return (3, s)

            inputs.sort(key=input_priority)
            outputs.sort(key=lambda item: (0 if "speaker" in item["label"].lower() or "headphone" in item["label"].lower() else 1, item["label"].lower()))
            preferred_app = next((x for x in inputs if x.get("app_capture") and "spotify" in x["label"].lower()), None)
            return {
                "inputs": inputs,
                "outputs": outputs,
                "default_input_id": str(preferred_app["id"] if preferred_app else wasapi.get("defaultInputDevice", "")),
                "default_output_id": str(wasapi.get("defaultOutputDevice", "")),
                "process_capture_available": bool(process_inputs),
                "process_capture_error": process_error or None,
            }
        finally:
            p.terminate()


    def _compose_params_locked(self) -> dict[str, float]:
        if self._dsp50_mode:
            return studio_dsp_render_params(self._dsp50_params, mode="dsp50")
        scales = {name: 1.0 for name in FEATURE_NAMES} if self._dj_mode else self._ai_scales
        base = feature_params(self._features, scales, self._effect_intensity)
        if self._dj_mode:
            # v30.4.4: DJ owns only the same 15 relative musical axes the user sees.
            # No hidden raw-DSP node offset and no Auto-DSP/x5 render expansion.
            base = _project_musical_params(base)
        if self._agentic_live_enabled:
            c = self._agentic_controls
            # Traditional master FX overlay. These are deliberately restrained and
            # still pass through the proven LowEndIntegrity master safety chain.
            rev = _clamp(float(c.get("reverb",0.0) or 0.0),0.0,1.0)
            ech = _clamp(float(c.get("echo",0.0) or 0.0),0.0,1.0)
            bpm = _clamp(float(c.get("bpm",100.0) or 100.0),55.0,190.0)
            base["reverb"] = _clamp(float(base.get("reverb",0.0)) + 0.22*rev, 0.0, 0.48)
            base["delay"] = _clamp(float(base.get("delay",0.0)) + 0.16*ech, 0.0, 0.34)
            if ech > 0.002:
                base["delay_ms"] = _clamp((60000.0/bpm)*0.50, 85.0, 430.0)
                base["delay_feedback"] = _clamp(0.06 + 0.16*ech, 0.04, 0.22)

            # Performance-FX overlay: the Agentic DJ can use the same production
            # vocabulary as JoyMetric's realtime FX without taking ownership of the
            # full semantic controller. Values are intentionally bounded and remain
            # downstream of LowEndIntegrity / CrackleGuard / true-peak protection.
            punch = _clamp(float(c.get("perf_punch",0.0) or 0.0),0.0,1.0)
            energy = _clamp(float(c.get("perf_energy",0.0) or 0.0),0.0,1.0)
            clarity = _clamp(float(c.get("perf_clarity",0.0) or 0.0),0.0,1.0)
            air = _clamp(float(c.get("perf_air",0.0) or 0.0),0.0,1.0)
            tight = _clamp(float(c.get("bass_tighten",0.0) or 0.0),0.0,1.0)
            base["transient_attack"] = _clamp(float(base.get("transient_attack",0.0)) + 0.40*punch + 0.10*energy, -1.0, 0.88)
            base["transient_sustain"] = _clamp(float(base.get("transient_sustain",0.0)) - 0.16*tight, -1.0, 1.0)
            base["compression"] = _clamp(float(base.get("compression",0.0)) + 0.18*punch + 0.11*energy, 0.0, 0.82)
            base["presence_db"] = _clamp(float(base.get("presence_db",0.0)) + 0.95*punch + 0.75*energy + 0.70*clarity, -12.0, 12.0)
            base["air_db"] = _clamp(float(base.get("air_db",0.0)) + 0.42*energy + 0.85*air + 0.35*clarity, -12.0, 12.0)
            base["body_db"] = _clamp(float(base.get("body_db",0.0)) - 0.38*clarity - 0.20*tight, -12.0, 12.0)
            base["bass_db"] = _clamp(float(base.get("bass_db",0.0)) + 0.30*tight + 0.20*energy, -15.0, 15.0)
            base["low_cut_hz"] = _clamp(float(base.get("low_cut_hz",20.0)) + 8.0*tight, 20.0, 220.0)
            base["drive"] = _clamp(float(base.get("drive",0.0)) + 0.055*punch + 0.035*energy, 0.0, 0.55)
            base["width"] = _clamp(float(base.get("width",1.0)) + 0.055*air, 0.0, 1.55)
            # Reserve a little headroom before the master when several performance
            # layers arrive together; this is a trim, never makeup gain.
            base["output_db"] = min(float(base.get("output_db",0.0)), -0.10*(punch+energy) - 0.06*air)
        return base

    def _reset_dsp50_timeline_locked(self):
        self._capture_frame_index = 0
        self._render_source_frame_index = 0
        self._pending_drum_kit = None
        self._pending_ai_loop = None
        self._dj_mixer_ref = None
        self._pending_synth_clip = None
        self._pending_synth_cancel = None
        self._pending_takeover = []
        self._pending_takeover_cancel = None
        self._pending_lineduck = []
        self._pending_lineduck_cancel = None
        self._pending_cancel_clip = []
        self._dsp50_render_params = dict(self._params)
        self._dsp50_param_schedule = []
        self._dsp50_schedule_late_count = 0
        self._dsp50_schedule_applied_count = 0

    def _schedule_dsp50_render_locked(
        self, params: dict[str, float], capture_frame: int, rate: int,
        analysis_window_sec: float = 0.0, reason: str = "semantic",
    ) -> int:
        """Schedule a semantic target on the delayed source-audio timeline.

        v30.4.2 applied a target as soon as inference returned. Because Audio Out
        hears ~2 s older audio, that target was semantically early. Here the result
        is stamped with the source frame that produced it. The trajectory begins
        shortly before the centre of that analysed window, using the intentional
        lookahead as computation/settling budget rather than as a timing offset.
        """
        rate = max(1, int(rate or 0))
        capture_frame = max(0, int(capture_frame or 0))
        window = max(0.0, float(analysis_window_sec or 0.0))
        center_back = window * _clamp(CONTINUITY_ANALYSIS_CENTER_FRAC, 0.30, 0.70)
        desired = capture_frame - int(round(rate * (center_back + CONTINUITY_PARAM_PREROLL_SEC)))
        min_ahead = int(round(rate * CONTINUITY_MIN_SCHEDULE_AHEAD_SEC))
        floor_frame = int(self._render_source_frame_index) + min_ahead
        if desired < floor_frame:
            desired = floor_frame
            self._dsp50_schedule_late_count += 1

        item = (int(desired), dict(params), str(reason or "semantic"))
        # Results arrive in chronological order in normal operation. Sort anyway so
        # one slow semantic pass can never reorder two audio targets.
        self._dsp50_param_schedule.append(item)
        self._dsp50_param_schedule.sort(key=lambda it: it[0])
        if len(self._dsp50_param_schedule) > 24:
            self._dsp50_param_schedule = self._dsp50_param_schedule[-24:]
        self._status.continuity_schedule_ahead_ms = 1000.0 * max(0, desired - int(self._render_source_frame_index)) / float(rate)
        self._status.continuity_schedule_queue = len(self._dsp50_param_schedule)
        self._status.continuity_schedule_late_count = int(self._dsp50_schedule_late_count)
        return int(desired)

    def _scheduled_dsp50_params_locked(self, source_frame: int) -> dict[str, float]:
        source_frame = int(source_frame)
        latest = None
        while self._dsp50_param_schedule and self._dsp50_param_schedule[0][0] <= source_frame:
            latest = self._dsp50_param_schedule.pop(0)
        if latest is not None:
            _, params, _ = latest
            self._dsp50_render_params = dict(params)
            self._dsp50_schedule_applied_count += 1
        self._status.continuity_schedule_queue = len(self._dsp50_param_schedule)
        self._status.continuity_schedule_applied_count = int(self._dsp50_schedule_applied_count)
        self._status.continuity_schedule_late_count = int(self._dsp50_schedule_late_count)
        return dict(self._dsp50_render_params)

    def _reset_dsp50_derivatives_locked(self, keep_params: bool = True):
        self._dsp50_gradients = {name: 0.0 for name in DSP50_SPECS}
        self._dsp50_confidence = {name: 0.0 for name in DSP50_SPECS}
        self._dsp50_adam_m = {name: 0.0 for name in DSP50_SPECS}
        self._dsp50_adam_s = {name: 0.0 for name in DSP50_SPECS}
        self._dsp50_adam_t = 0
        self._dsp50_last_pass_time = 0.0
        self._dsp50_last_window_ms = 0.0
        self._dsp50_last_result_age_ms = 0.0
        self._dsp50_last_render_count = 0
        self._dsp50_coord_cursor = 0
        self._dsp50_cycle = 0
        self._dsp50_score = 0.0
        self._dsp50_active_nodes = []
        self._dsp50_profile_applied_prompt = ""
        if not keep_params:
            self._dsp50_params = dsp50_safety_params(DEFAULT_PARAMS)
            self._dsp50_base_params = dict(self._dsp50_params)

    def set_agentic_live_controls(self, controls: dict[str, Any] | None = None, enabled: bool | None = None):
        """Post bounded DJ-controller targets without touching the audio callback."""
        with self._lock:
            if enabled is not None:
                self._agentic_live_enabled = bool(enabled)
            if controls:
                c = dict(self._agentic_controls)
                bounds = {
                    'bpm':(55.0,190.0),'a_low_db':(-8,6),'a_mid_db':(-8,6),'a_high_db':(-8,6),'a_filter':(-1,1),'a_gain':(0,1.2),
                    'b_low_db':(-8,6),'b_mid_db':(-8,6),'b_high_db':(-8,6),'b_filter':(-1,1),'b_gain':(0,1.2),'crossfader':(0,1),'b_layer':(0,.62),'b_texture':(-1,1),'b_transient':(0,1),'b_slicer_mix':(0,1),'b_slice_mode':(0,5),'b_fx':(0,1),
                    'reverb':(0,1),'echo':(0,1),'loop_beats':(.25,16),
                    'noise_riser':(0,1),'fx_reverse_swell':(0,1),'fx_snare_rush':(0,1),'impact_strength':(0,1),'drum_drive':(0,1),'doubletime':(0,1),
                    'fx_rack_wet':(0,1),'fx_echo_send':(0,1),'fx_echo_feedback':(0,.68),'fx_echo_beats':(.125,1.5),'fx_gate':(0,.62),'fx_gate_div':(1,8),'fx_width':(0,1),'fx_duck':(0,1),
                    'perf_punch':(0,1),'perf_energy':(0,1),'perf_clarity':(0,1),'perf_air':(0,1),'bass_tighten':(0,1),
                    'fx_pump':(0,.85),'fx_pump_cycle':(.25,4),'fx_bass_cut':(0,1),'fx_rush_accel':(0,1),
                    'fx_brake_beats':(.25,8),'fx_brake_mode':(0,4),
                    'fx_redrum':(0,1),'fx_redrum_pattern':(0,3),
                    'fx_swing':(0,.6),'grid_conf':(0,1),'grid_lead_ms':(0,250),
                    'fx_redrum_var':(0,1),'fx_redrum_fill':(0,1),
                    'fx_bass_synth':(0,.9),'fx_bass_pattern':(0,3),'fx_bass_root':(0,11),'fx_bass_conf':(0,1),
                    'fx_stab':(0,.8),'fx_stab_pattern':(0,1),
                    'fx_weave_offset_beats':(1,32),'fx_weave_cell_beats':(.25,4),
                    'fx_hat_style':(0,3),'fx_hat_var':(0,1),'fx_hat_fill':(0,1),
                    'fx_groove_clarity':(0,1),
                    'drum_mode':(0,1),'drum_kick':(0,1),'drum_hat':(0,1),'drum_snare':(0,1),'drum_presence':(0,1),'b_dedrum':(0,1),'b_ai_mix':(0,1),
                }
                for k,v in controls.items():
                    if k in {'loop_capture_seq','loop_release_seq','loop_retrigger_seq','impact_seq','fx_brake_seq','fx_pump_sync_seq','drum_pat_seq'}:
                        try: c[k]=max(0,int(v))
                        except Exception: pass
                    elif k in {'grid_anchor_t','grid_now_t','grid_anchor_frame'}:
                        # Monotonic-clock references for the beat-locked FX grid.
                        try:
                            fv=float(v)
                            if math.isfinite(fv) and fv>=0.0: c[k]=fv
                        except Exception: pass
                    elif k in {'future_chroma','future_rhythm','drum_vel','drum_mt'}:
                        try:
                            arr=[float(q) for q in list(v)]
                            need=12 if k=='future_chroma' else (16 if k=='future_rhythm' else 128)
                            if len(arr)==need and all(math.isfinite(q) for q in arr): c[k]=arr
                        except Exception: pass
                    elif k in {'future_tonal_strength','future_energy','future_vocal','future_drums'}:
                        try: c[k]=_clamp(float(v),0.0,1.0)
                        except Exception: pass
                    elif k in bounds:
                        try: c[k]=_clamp(float(v),*bounds[k])
                        except Exception: pass
                    elif k == 'enabled':
                        c[k]=bool(v)
                self._agentic_controls=c
            # Master FX overlay is folded into the same parameter mapping and then
            # smoothed by RealtimeDSP; no raw plugin control jumps are introduced.
            self._params = self._compose_params_locked()
            self._status.dsp_params = dict(self._params)

    def agentic_audio_snapshot(self, seconds: float = 15.0):
        """Newest future/raw capture used for lookahead planning."""
        rate=max(1,int(self._ai_sample_rate or self._status.sample_rate or 48000))
        n=int(rate*_clamp(float(seconds or 0.0),0.25,58.0))   # v31.30.13: the ring holds 60 s; the weave reaches back to the first window after LET'S GO
        ring=self._dj_stereo_ring
        return ring.snapshot(n) if ring is not None else None

    def agentic_render_audio_snapshot(self, seconds: float = 15.0):
        """Delayed source-time PCM aligned with what the listener is hearing."""
        rate=max(1,int(self._ai_sample_rate or self._status.sample_rate or 48000))
        n=int(rate*_clamp(float(seconds or 0.0),0.25,30.0))
        ring=self._dj_render_ring
        return ring.snapshot(n) if ring is not None else None

    def agentic_render_audio_snapshot_indexed(self, seconds: float = 8.0):
        """v31.18: (audible PCM, absolute render-frame index of its last sample)."""
        rate=max(1,int(self._ai_sample_rate or self._status.sample_rate or 48000))
        n=int(rate*_clamp(float(seconds or 0.0),0.25,30.0))
        ring=self._dj_render_ring
        if ring is None:
            return None, 0
        return ring.snapshot_indexed(n)

    def agentic_audio_snapshot_indexed(self, seconds: float = 15.0):
        """v31.22: (future/raw PCM, absolute CAPTURE-frame index of its last sample)."""
        rate=max(1,int(self._ai_sample_rate or self._status.sample_rate or 48000))
        n=int(rate*_clamp(float(seconds or 0.0),0.25,30.0))
        ring=self._dj_stereo_ring
        if ring is None:
            return None, 0
        return ring.snapshot_indexed(n)

    def set_synth_clip(self, buf, render_start_frame: int, gain: float = 0.6, seq: int = 0):
        """v31.22: schedule a synth clip at an absolute render-frame position."""
        self._pending_synth_clip = (buf, int(render_start_frame), float(gain), int(seq))
        return True

    def cancel_synth_clip(self, seq: int):
        """v31.22.2: cancel a scheduled synth clip (play-time harmonic gate)."""
        self._pending_synth_cancel = int(seq)
        return True

    def schedule_takeover(self, start_frame: int, end_frame: int, amount: float = 0.7, ramp_in_s: float = 0.5, ramp_out_s: float = 2.0, seq: int = 0, mode: str = "takeover"):
        """v31.23: frame-scheduled programme takeover (mode 'takeover') or freeze (mode 'freeze')."""
        self._pending_takeover.append((int(start_frame), int(end_frame), float(amount), float(ramp_in_s), float(ramp_out_s), int(seq), str(mode)))
        return True

    def cancel_takeover(self, seq: int):
        self._pending_takeover_cancel = int(seq)
        return True

    def schedule_line_duck(self, segments, seq: int = 0):
        """v31.26: [(start_frame, end_frame, midi, amount)] - harmonic notches following one line of the record."""
        for (a, b, m, g) in segments:
            self._pending_lineduck.append((int(a), int(b), int(m), float(g), int(seq)))
        return True

    def cancel_line_duck(self, seq: int):
        self._pending_lineduck_cancel = int(seq)
        return True

    def set_cancel_clip(self, buf, render_start_frame: int, amount: float = 1.0, seq: int = 0):
        """v31.28: schedule a separated-vocal clip to be SUBTRACTED from the programme at an absolute render frame."""
        self._pending_cancel_clip.append((buf, int(render_start_frame), float(amount), int(seq)))
        return True

    def agentic_loop_snapshot(self):
        """v31.21: (copy of the playing Deck-B loop, its capture seq, its length in beats) or None."""
        m = self._dj_mixer_ref
        try:
            if m is None or int(m.loop_len) < 8:
                return None
            n = int(m.loop_len)
            return m.loop[:n].copy(), int(m.last_capture_seq), float(getattr(m, '_loop_beats', 0.0) or 0.0)
        except Exception:
            return None

    def set_ai_loop(self, buf, seq: int):
        """v31.21: hand a synth loop (sample-aligned with capture `seq`) to the mixer at the next block."""
        self._pending_ai_loop = (buf, int(seq))
        return True

    def set_drum_kit(self, kit):
        """v31.19: hand a per-record drum kit (dict of one-shots) to the mixer at the next block."""
        self._pending_drum_kit = kit

    def set_controls(
        self,
        params: dict[str, Any] | None = None,
        features: dict[str, Any] | None = None,
        ai_enabled: bool | None = None,
        dj_mode: bool | None = None,
        dj_prompt: str | None = None,
        dsp50_mode: bool | None = None,
        dsp50_prompt: str | None = None,
        dsp50_intensity: float | None = None,
        effect_intensity: float | None = None,
        neural_polish_enabled: bool | None = None,
        neural_polish_wet: float | None = None,
    ):
        with self._lock:
            # GPU restoration was removed in v30.4.23. Keep legacy API fields
            # disabled for compatibility with older browser payloads.
            self._neural_polish_enabled = False
            self._neural_polish_wet = 0.0
            if effect_intensity is not None:
                try:
                    self._effect_intensity_target = _clamp(float(effect_intensity), 0.0, 1.60)
                except (TypeError, ValueError):
                    pass
            if dsp50_prompt is not None:
                clean = " ".join(str(dsp50_prompt or "").split()).strip()[:4000]
                if clean != self._dsp50_prompt:
                    self._dsp50_prompt = clean
                    # Keep the current sound but forget stale derivatives from the
                    # previous semantic target. Freeze it as the temporary base so
                    # no old-prompt correction can leak into the new prompt window.
                    self._dsp50_base_params = dict(self._dsp50_params)
                    self._reset_dsp50_derivatives_locked(keep_params=True)
            if dsp50_intensity is not None:
                try:
                    self._dsp50_intensity = _clamp(float(dsp50_intensity), 0.35, 2.40)
                except (TypeError, ValueError):
                    pass

            if dj_prompt is not None:
                clean_prompt = " ".join(str(dj_prompt or "").split()).strip()[:4000]
                if clean_prompt != self._dj_prompt:
                    self._dj_prompt = clean_prompt
                    self._dj_node_cursor = 0
                    self._dj_cycle = 0
                    self._dj_last_pass_time = 0.0
                    self._dj_last_window_ms = 0.0
                    self._dj_last_result_age_ms = 0.0
                    self._dj_profile_applied_prompt = ""
                    self._dj_feature_gradients = {name: 0.0 for name in FEATURE_NAMES}
                    self._dj_node_gradients = {name: 0.0 for name in DJ_NODE_SPECS}
                    self._dj_node_offsets = {name: float(self._dj_node_offsets.get(name, 0.0)) * 0.35 for name in DJ_NODE_SPECS}

            requested_dsp50 = False  # v30.4.4: Auto DSP / 50D mode removed from product path.
            requested_dj = self._dj_mode if dj_mode is None else bool(dj_mode)
            requested_dsp50 = False

            if requested_dsp50 != self._dsp50_mode:
                self._dsp50_mode = requested_dsp50
                self._ai_scales = {name: 1.0 for name in FEATURE_NAMES}
                self._reset_dsp50_derivatives_locked(keep_params=False)
                if requested_dsp50:
                    self._dj_mode = False
                    self._dj_node_offsets = {name: 0.0 for name in DJ_NODE_SPECS}
                    self._status.ai_state = "Auto DSP engaged · compiling music-domain target"
                else:
                    self._status.ai_state = "Auto DSP off · returning to feature engine"

            if requested_dj != self._dj_mode:
                self._dj_mode = requested_dj
                self._ai_scales = {name: 1.0 for name in FEATURE_NAMES}
                self._dj_node_offsets = {name: 0.0 for name in DJ_NODE_SPECS}
                self._dj_node_gradients = {name: 0.0 for name in DJ_NODE_SPECS}
                self._dj_feature_gradients = {name: 0.0 for name in FEATURE_NAMES}
                self._dj_node_cursor = 0
                self._dj_cycle = 0
                self._dj_last_pass_time = 0.0
                self._dj_last_window_ms = 0.0
                self._dj_last_result_age_ms = 0.0
                self._dj_profile_applied_prompt = ""
                self._dj_feature_schedule = []
                self._dj_schedule_applied_count = 0
                self._dj_schedule_late_count = 0
                self._dj_render_target_features = dict(self._features)
                if requested_dj:
                    self._dsp50_mode = False
                    self._dj_target_features = {name: _clamp(float(self._features.get(name, 0.0)), -DJ_FEATURE_LIMIT, DJ_FEATURE_LIMIT) for name in FEATURE_NAMES}
                else:
                    self._dj_target_features = dict(self._features)

            if ai_enabled is not None:
                requested_ai = bool(ai_enabled)
                if requested_ai != self._ai_enabled:
                    self._ai_scales = {name: 1.0 for name in FEATURE_NAMES}
                self._ai_enabled = requested_ai
                self._status.ai_enabled = self._ai_enabled

            # Derivative ownership modes lock the manual 15D deck. Browser feature
            # writes are ignored while either optimizer is engaged.
            if features is not None and not self._dj_mode and not self._dsp50_mode:
                clean = dict(self._features)
                for name in FEATURE_NAMES:
                    if name not in features:
                        continue
                    try:
                        value = _clamp(float(features[name]), -1.25, 1.25)
                    except (TypeError, ValueError):
                        continue
                    old = float(clean.get(name, 0.0))
                    if old * value < 0 or abs(value - old) > 0.34 or abs(value) < 0.015:
                        self._ai_scales[name] = 1.0
                    clean[name] = value
                self._features = clean
                self._last_feature_snapshot = dict(clean)
                self._status.ai_scales = dict(self._ai_scales)
            elif params and not self._dj_mode and not self._dsp50_mode:
                for k, v in params.items():
                    if k in DEFAULT_PARAMS:
                        try:
                            self._params[k] = float(v)
                        except (TypeError, ValueError):
                            pass

            self._params = self._compose_params_locked()
            self._status.features = dict(self._features)
            self._status.dsp_params = dict(self._params)
            self._status.effect_intensity = float(self._effect_intensity_target)
            self._status.neural_polish_enabled = False
            self._status.neural_polish_wet = 0.0
            self._status.dj_mode = bool(self._dj_mode)
            self._status.dj_prompt = self._dj_prompt
            self._status.dsp50_mode = bool(self._dsp50_mode)
            self._status.dsp50_prompt = self._dsp50_prompt
            self._status.dsp50_intensity = float(self._dsp50_intensity)
            self._status.dsp50_node_values = {k: float(self._params.get(k, DEFAULT_PARAMS[k])) for k in DSP50_SPECS}

    def start(
        self, input_id: str, output_id: str, params: dict[str, Any] | None = None,
        features: dict[str, Any] | None = None, ai_enabled: bool | None = None,
        dj_mode: bool | None = None, dj_prompt: str | None = None,
        dsp50_mode: bool | None = None, dsp50_prompt: str | None = None,
        dsp50_intensity: float | None = None, effect_intensity: float | None = None,
        neural_polish_enabled: bool | None = None, neural_polish_wet: float | None = None,
    ) -> dict[str, Any]:
        self.stop()
        self.set_controls(
            params, features, ai_enabled, dj_mode, dj_prompt,
            dsp50_mode, dsp50_prompt, dsp50_intensity, effect_intensity,
            neural_polish_enabled, neural_polish_wet,
        )
        pyaudio = self._imports()
        p = pyaudio.PyAudio()
        is_process = str(input_id).startswith("app:")
        try:
            if str(output_id).startswith('sd:'):
                import sounddevice as sd
                sd_idx = int(str(output_id).split(':', 1)[1])
                sd_d = sd.query_devices(sd_idx)
                sd_apis = sd.query_hostapis()
                try:
                    api_name = str(sd_apis[int(sd_d.get('hostapi', -1))].get('name', ''))
                except Exception:
                    api_name = ''
                if 'wasapi' not in api_name.lower() or int(sd_d.get('max_output_channels', 0) or 0) < 1:
                    raise RuntimeError('Selected headphone/output is no longer a WASAPI playback endpoint.')
                out_idx = f'sd:{sd_idx}'
                out_info = {
                    'name': str(sd_d.get('name') or output_id),
                    'maxOutputChannels': int(sd_d.get('max_output_channels') or 0),
                    'defaultSampleRate': float(sd_d.get('default_samplerate') or 48000),
                }
            else:
                out_idx = int(output_id)
                out_info = p.get_device_info_by_index(out_idx)
            if is_process:
                process_pid = int(str(input_id).split(":", 1)[1])
                try:
                    import psutil
                    process_name = psutil.Process(process_pid).name()
                except Exception:
                    process_name = f"PID {process_pid}"
                in_idx = f"app:{process_pid}"
                in_info = {"name": f"{os.path.splitext(process_name)[0]} · APP TAP", "maxInputChannels": 2, "defaultSampleRate": 48000}
            else:
                in_idx = int(input_id)
                in_info = p.get_device_info_by_index(in_idx)
        except Exception:
            p.terminate()
            raise RuntimeError("The selected Windows audio source/output is no longer available. Press Scan and select it again.")
        p.terminate()

        if int(in_info.get("maxInputChannels") or 0) < 1:
            raise RuntimeError("Selected input is not capturable. Choose an APP TAP, recording input, or LOOPBACK source.")
        if int(out_info.get("maxOutputChannels") or 0) < 1:
            raise RuntimeError("Selected Audio Out is not a playback device.")

        self._stop_event.clear()
        self._playback_ready_event.clear()
        self._playback_error = ""
        with self._lock:
            self._status = EngineStatus(
                running=False,
                message="Starting native-rate WASAPI processing…",
                input_label=str(in_info.get("name") or input_id),
                output_label=str(out_info.get("name") or output_id),
                source_kind="joymetric_virtual_driver" if is_process else "device",
                source_pid=int(process_pid) if is_process else 0,
                capture_backend="JoyMetric Virtual Input · private WASAPI loopback" if is_process else "WASAPI endpoint",
                ai_enabled=bool(self._ai_enabled or self._dj_mode or self._dsp50_mode),
                ai_scales=dict(self._ai_scales),
                features=dict(self._features),
                dsp_params=dict(self._params),
                effect_intensity=float(self._effect_intensity_target),
                neural_polish_enabled=bool(self._neural_polish_enabled),
                neural_polish_wet=float(self._neural_polish_wet),
                dj_mode=bool(self._dj_mode),
                dj_prompt=self._dj_prompt,
                dj_score=float(self._dj_score),
                dj_cycle=int(self._dj_cycle),
                dj_feature_gradients=dict(self._dj_feature_gradients),
                dj_node_gradients=dict(self._dj_node_gradients),
                dj_node_values={k: float(self._params.get(k, DEFAULT_PARAMS.get(k, 0.0))) for k in DJ_NODE_SPECS},
                dsp50_mode=bool(self._dsp50_mode),
                dsp50_prompt=self._dsp50_prompt,
                dsp50_intensity=float(self._dsp50_intensity),
                dsp50_score=float(self._dsp50_score),
                dsp50_cycle=int(self._dsp50_cycle),
                dsp50_gradients=dict(self._dsp50_gradients),
                dsp50_confidence=dict(self._dsp50_confidence),
                dsp50_node_values={k: float(self._params.get(k, DEFAULT_PARAMS[k])) for k in DSP50_SPECS},
                dsp50_active_nodes=list(self._dsp50_active_nodes),
            )
            self._last_error = ""
        self._thread = threading.Thread(
            target=self._run,
            args=(in_idx, out_idx),
            daemon=True,
            name="joymetric-native-realtime",
        )
        self._thread.start()

        # v31.8.1: Spotify per-app routing may legitimately need several Windows
        # SoundVolumeView/SVCL calls (each has its own bounded timeout).  The old
        # 2.7 s web-start deadline could race a still-valid route migration: the
        # API would see running=False, call stop(), and Spotify would immediately
        # be restored to its old output. Keep the start request synchronous until
        # the route is genuinely live or a real error is surfaced.
        deadline = time.time() + 12.0
        while time.time() < deadline:
            with self._lock:
                if self._status.running:
                    return self.status()
                if self._last_error:
                    raise RuntimeError(self._last_error)
            time.sleep(0.04)
        return self.status()

    def _open_pair(self, p, pyaudio, in_idx: int | str, out_idx: int):
        if isinstance(in_idx, str) and in_idx.startswith("app:"):
            pid = int(in_idx.split(":", 1)[1])
            selected_sd_idx = None
            if isinstance(out_idx, str) and out_idx.startswith('sd:'):
                import sounddevice as sd
                selected_sd_idx = int(out_idx.split(':', 1)[1])
                sd_d = sd.query_devices(selected_sd_idx)
                out_info = {
                    'name': str(sd_d.get('name') or out_idx),
                    'maxOutputChannels': int(sd_d.get('max_output_channels') or 0),
                    'defaultSampleRate': float(sd_d.get('default_samplerate') or 48000),
                }
            else:
                out_info = p.get_device_info_by_index(int(out_idx))
            if int(out_info.get("maxOutputChannels") or 0) < 2:
                raise RuntimeError("Agentic DJ requires a stereo Audio Out. Choose the stereo/music headphone endpoint, not the mono Hands-Free/Headset endpoint.")

            channel = _find_managed_virtual_channel(p, pyaudio)
            if not channel:
                raise RuntimeError(
                    "JoyMetric Virtual Audio Driver is not installed yet. Run INSTALL_JOYMETRIC_DRIVER.ps1 "
                    "once as Administrator, reboot Windows, then start JoyMetric again."
                )

            preferred_rate = int(round(float(out_info.get('defaultSampleRate') or 48000)))
            rate = preferred_rate if preferred_rate >= 32000 else 48000
            channels = 2
            # v31.5.1 CrackleGuard: the v31.5 FX rack left too little scheduler
            # margin at 1024 frames on real Windows machines. Agentic mode already
            # has 15 s musical lookahead, so a 2048-frame processing quantum adds
            # only ~21 ms versus v31.5 while doubling the realtime deadline. This
            # is a much better trade than audible WASAPI starvation/crackle.
            try:
                safe_frames = int(os.environ.get('JOY_AGENTIC_AUDIO_FRAMES', '2048') or 2048)
            except Exception:
                safe_frames = 2048
            frames = max(1024, min(4096, safe_frames))
            # Keep musical/control execution at 2048f by default, but give the
            # physical Audio Out its own 4096f write quantum. The output worker is
            # already decoupled by an elastic FIFO, so this adds safety without
            # moving FX relative to the source samples.
            out_frames = max(4096, frames)
            output_name = str(out_info.get("name") or out_idx)

            # The selected physical Audio Out remains exact. This is the known-good
            # v30.4.31 route and is intentionally unchanged.
            def mk_exact_pyaudio():
                if selected_sd_idx is not None:
                    raise RuntimeError('PyAudio exact route is not applicable to a sounddevice-only endpoint.')
                st = p.open(
                    format=pyaudio.paFloat32,
                    channels=channels,
                    rate=rate,
                    output=True,
                    output_device_index=int(out_idx),
                    frames_per_buffer=out_frames,
                    start=False,
                )
                try:
                    st.backend_name = "PyAudioWPatch WASAPI · exact selected endpoint · safe 2048f"
                    st.is_exact_selected_output = True
                except Exception:
                    pass
                return st

            def mk_sounddevice():
                return _SoundDeviceOutputStream(output_name, rate, channels, out_frames, device_index=selected_sd_idx, exact_selected=(selected_sd_idx is not None))

            def mk_soundcard():
                return _SoundCardOutputStream(output_name, rate, channels, out_frames)

            factories = []
            if selected_sd_idx is None:
                factories.append(("PyAudioWPatch WASAPI · exact selected endpoint · safe buffered", mk_exact_pyaudio))
                factories.append(("sounddevice WASAPI · name fallback · safe buffered", mk_sounddevice))
            else:
                factories.append(("sounddevice WASAPI · exact selected headphone/output · safe buffered", mk_sounddevice))
            factories.append(("SoundCard WASAPI · name fallback", mk_soundcard))
            outstream = _FailoverOutputStream(factories)

            # Capture the private WASAPI loopback of JoyMetric Virtual Input.
            # This bypasses the paired-mic kernel ring entirely and reads exactly
            # what Windows is rendering into JoyMetric's own virtual sink.
            cap_idx = channel.get("loopback_index")
            if cap_idx is None:
                raise RuntimeError(
                    "JoyMetric Virtual Input exists, but its WASAPI loopback endpoint was not found. "
                    "Restart Windows Audio or reboot once after installing the JoyMetric driver."
                )
            cap_idx = int(cap_idx)
            cap_info = p.get_device_info_by_index(cap_idx)
            cap_rates = [rate, int(round(float(cap_info.get("defaultSampleRate") or 48000))), 48000, 44100]
            cap_rates = [r for n, r in enumerate(cap_rates) if r >= 32000 and r not in cap_rates[:n]]
            last_exc = None
            inner = None
            for cap_rate in cap_rates:
                # JoyMetric v0.2 intentionally restores Microsoft's stock
                # SimpleAudioSample render format (48k stereo PCM16). WASAPI
                # shared mode often accepts float32 directly, but some endpoint
                # stacks expose only the native integer mix format. Try float32
                # first, then exact PCM16, then PCM32 as a compatibility fallback.
                for fmt, wrap_kind in (
                    (pyaudio.paFloat32, "float32"),
                    (pyaudio.paInt16, "int16"),
                    (pyaudio.paInt32, "int32"),
                ):
                    try:
                        raw_inner = p.open(
                            format=fmt,
                            channels=channels,
                            rate=cap_rate,
                            input=True,
                            input_device_index=cap_idx,
                            frames_per_buffer=frames,
                            start=False,
                        )
                        if wrap_kind == "int16":
                            inner = _Int16ToFloat32InputStream(raw_inner)
                        elif wrap_kind == "int32":
                            inner = _Int32ToFloat32InputStream(raw_inner)
                        else:
                            inner = raw_inner
                        rate = int(cap_rate)
                        break
                    except Exception as exc:
                        last_exc = exc
                if inner is not None:
                    break
            if inner is None:
                raise RuntimeError(f"JoyMetric Virtual Input loopback could not open: {last_exc}")

            router = _PerAppRenderRouter(pid, str(channel["render_name"]))
            instream = _ManagedVirtualChannelInputStream(inner, pid, router, channel)
            return instream, outstream, rate, channels, frames, out_frames, False

        in_info = p.get_device_info_by_index(in_idx)
        selected_sd_idx = None
        if isinstance(out_idx, str) and out_idx.startswith('sd:'):
            import sounddevice as sd
            selected_sd_idx = int(out_idx.split(':', 1)[1])
            sd_d = sd.query_devices(selected_sd_idx)
            out_info = {
                'name': str(sd_d.get('name') or out_idx),
                'maxOutputChannels': int(sd_d.get('max_output_channels') or 0),
                'defaultSampleRate': float(sd_d.get('default_samplerate') or 48000),
            }
        else:
            out_info = p.get_device_info_by_index(int(out_idx))
        in_channels = int(in_info.get("maxInputChannels") or 0)
        out_channels = int(out_info.get("maxOutputChannels") or 0)
        channels = 2 if in_channels >= 2 and out_channels >= 2 else 1

        # Full native fidelity. CrackleGuard prefers a 512-frame processing quantum first. At 48 kHz this is
        # 10.67 ms: still interactive, but substantially safer for Windows audio
        # scheduling than tiny Python buffers. Input/output clocks are decoupled later
        # by an adaptive elastic FIFO, so device-clock mismatch never needs a hard
        # dropped/repeated block.
        in_default = int(round(float(in_info.get("defaultSampleRate") or 48000)))
        out_default = int(round(float(out_info.get("defaultSampleRate") or 48000)))
        rates: list[int] = []
        for rate in (in_default, out_default, 48000, 44100):
            if rate >= 32000 and rate not in rates:
                rates.append(rate)

        errors = []
        for rate in rates:
            for frames in (512, 384, 256, 768, 1024, 192, 128):
                out_candidates = []
                for of in (max(1024, frames), max(768, frames), frames):
                    if of not in out_candidates:
                        out_candidates.append(of)
                for out_frames in out_candidates:
                    instream = outstream = None
                    try:
                        instream = p.open(
                            format=pyaudio.paFloat32,
                            channels=channels,
                            rate=rate,
                            input=True,
                            input_device_index=in_idx,
                            frames_per_buffer=frames,
                            start=False,
                        )
                        if selected_sd_idx is not None:
                            outstream = _SoundDeviceOutputStream(
                                str(out_info.get('name') or out_idx), rate, channels,
                                max(2048, out_frames), device_index=selected_sd_idx, exact_selected=True,
                            )
                        else:
                            outstream = p.open(
                                format=pyaudio.paFloat32,
                                channels=channels,
                                rate=rate,
                                output=True,
                                output_device_index=int(out_idx),
                                frames_per_buffer=out_frames,
                                start=False,
                            )
                        return instream, outstream, rate, channels, frames, max(2048,out_frames) if selected_sd_idx is not None else out_frames, False
                    except Exception as exc:
                        errors.append(f"{rate} Hz/{frames}f in/{out_frames}f out: {exc}")
                        for stream in (instream, outstream):
                            try:
                                if stream:
                                    stream.close()
                            except Exception:
                                pass
        raise RuntimeError("Could not open the selected Input → Audio Out route at native quality. " + " | ".join(errors[-6:]))

    def _semantic_health(self) -> dict[str, Any]:
        url = f"http://{self._ai_host}:{self._ai_port}/health"
        try:
            with urllib.request.urlopen(url, timeout=1.4) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            return {"ready": False, "state": "semantic server unavailable", "error": str(exc)}

    def _semantic_score_pair(self, input_audio, output_audio, sample_rate: int) -> dict[str, Any]:
        payload = {
            "sample_rate": int(sample_rate),
            "input_b64": base64.b64encode(input_audio.astype("<f4", copy=False).tobytes()).decode("ascii"),
            "output_b64": base64.b64encode(output_audio.astype("<f4", copy=False).tobytes()).decode("ascii"),
        }
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            f"http://{self._ai_host}:{self._ai_port}/score",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=32.0) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _semantic_prompt_profile(self, prompt: str) -> dict[str, Any]:
        data = json.dumps({"prompt": str(prompt or "")}, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            f"http://{self._ai_host}:{self._ai_port}/prompt-profile",
            data=data, headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _semantic_dj_gradient(self, prompt: str, dry_audio, sample_rate: int, params: dict[str, float], nodes: list[str], cycle: int) -> dict[str, Any]:
        payload = {
            "prompt": str(prompt or ""),
            "sample_rate": int(sample_rate),
            "dry_b64": base64.b64encode(dry_audio.astype("<f4", copy=False).tobytes()).decode("ascii"),
            "channels": int(dry_audio.shape[1]) if getattr(dry_audio, "ndim", 1) == 2 else 1,
            "params": {k: float(v) for k, v in params.items() if k in PARAM_BOUNDS},
            "nodes": list(nodes),
            "cycle": int(cycle),
        }
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            f"http://{self._ai_host}:{self._ai_port}/dj-gradient",
            data=data, headers={"Content-Type": "application/json"}, method="POST",
        )
        # Central finite-difference rendering + a CLAP batch is deliberately
        # out-of-band and may take seconds on CPU. It never blocks the audio path.
        with urllib.request.urlopen(req, timeout=45.0) as resp:
            return json.loads(resp.read().decode("utf-8"))


    def _semantic_dsp50_gradient(
        self, prompt: str, dry_audio, sample_rate: int,
        params: dict[str, float], coordinate_nodes: list[str], cycle: int,
    ) -> dict[str, Any]:
        payload = {
            "prompt": str(prompt or ""),
            "sample_rate": int(sample_rate),
            "dry_b64": base64.b64encode(dry_audio.astype("<f4", copy=False).tobytes()).decode("ascii"),
            "channels": int(dry_audio.shape[1]) if getattr(dry_audio, "ndim", 1) == 2 else 1,
            "params": {k: float(params.get(k, DEFAULT_PARAMS[k])) for k in DSP50_SPECS},
            "coordinate_nodes": list(coordinate_nodes),
            "cycle": int(cycle),
        }
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            f"http://{self._ai_host}:{self._ai_port}/dsp50-gradient",
            data=data, headers={"Content-Type": "application/json"}, method="POST",
        )
        # Three simultaneous two-sided SPSA directions estimate all 50 partials,
        # while two exact coordinate probes calibrate the noisiest dimensions.
        with urllib.request.urlopen(req, timeout=55.0) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _select_dsp50_coordinate_nodes_locked(self) -> list[str]:
        names = list(DSP50_SPECS)
        score = lambda n: abs(float(self._dsp50_gradients.get(n, 0.0))) * (0.35 + 0.65 * float(self._dsp50_confidence.get(n, 0.0)))
        ranked = sorted(names, key=score, reverse=True)
        chosen: list[str] = []
        if ranked and score(ranked[0]) > 1e-5:
            chosen.append(ranked[0])
        while len(chosen) < DSP50_COORDS_PER_CYCLE:
            name = names[self._dsp50_coord_cursor % len(names)]
            self._dsp50_coord_cursor = (self._dsp50_coord_cursor + 1) % len(names)
            if name not in chosen:
                chosen.append(name)
        return chosen

    def _prime_dsp50_from_profile_locked(
        self, dsp_prompt_prior: dict[str, float], detail_confidence: float = 0.0,
    ):
        """Apply a deterministic first musical move before stochastic live probing.

        This is intentionally conservative: the text planner picks a sparse set of
        strong physical directions immediately, then current-audio gradients refine
        them. The user hears the requested character quickly without waiting for an
        SPSA pass or allowing 50 coordinates to jump at once.
        """
        prior = dsp_prompt_prior or {}
        conf = _clamp(float(detail_confidence or 0.0), 0.0, 1.0)
        if conf <= 0.05:
            return
        caps = {"EQ":4,"DYNAMICS":2,"HARMONICS":2,"STEREO":2,"REVERB":2,"DELAY":1}
        ranked = sorted(
            DSP50_SPECS,
            key=lambda n: abs(float(prior.get(n, 0.0) or 0.0))
                        * (1.0 if n in DSP50_DIRECT_NODES else 0.42),
            reverse=True,
        )
        chosen: list[str] = []
        used = {g:0 for g in caps}
        for name in ranked:
            pv = abs(float(prior.get(name, 0.0) or 0.0))
            if pv < 0.16:
                continue
            group = str(DSP50_SPECS[name]["group"])
            if used.get(group, 0) >= caps.get(group, 2):
                continue
            chosen.append(name); used[group] = used.get(group, 0) + 1
            if len(chosen) >= 9:
                break

        updated = dict(self._dsp50_params)
        intensity = _clamp(self._dsp50_intensity, 0.35, 2.40)
        for name in DSP50_SPECS:
            cur = dsp50_normalized(updated.get(name, DEFAULT_PARAMS[name]), name)
            neutral = dsp50_normalized(DEFAULT_PARAMS[name], name)
            pv = _clamp(float(prior.get(name, 0.0) or 0.0), -1.0, 1.0)
            if name in chosen:
                reach = 0.24 if name in DSP50_DIRECT_NODES else 0.13
                desired = _clamp(neutral + reach * pv, 0.0, 1.0)
                alpha = (0.20 + 0.10 * conf) * min(1.12, 0.70 + 0.18 * intensity)
                if name in DSP50_SLOW_NODES:
                    alpha *= 0.42
                nxt = cur + (desired - cur) * alpha
            else:
                # Do not flatten the current sound on every prompt edit. Only a
                # tiny drift toward neutral clears stale coloration gracefully.
                nxt = cur + (neutral - cur) * 0.004
            updated[name] = dsp50_from_normalized(_clamp(nxt, 0.0, 1.0), name)
        self._dsp50_params = dsp50_safety_params(updated)
        self._dsp50_base_params = dict(self._dsp50_params)
        self._dsp50_active_nodes = chosen

    def _update_dsp50_targets_locked(
        self, spsa_gradients: dict[str, float], coordinate_gradients: dict[str, float],
        freshness: float = 1.0, feature_affinity: dict[str, float] | None = None,
        current_feature_state: dict[str, float] | None = None,
        dsp_prompt_prior: dict[str, float] | None = None, detail_confidence: float = 0.0,
    ):
        """Projected AdaBelief update for the 50 physical DSP coordinates.

        The optimizer uses the *current song moment* twice: the black-box DSP
        derivative gives direction, while prompt-vs-current CLAP feature residuals
        determine how much control budget each DSP family receives. This prevents a
        chorus that is already wide/dreamy from being pushed as hard as a dry verse.
        """
        freshness = _clamp(float(freshness), 0.42, 1.0)
        affinity = feature_affinity or {}
        current = current_feature_state or {}
        prior = dsp_prompt_prior or {}
        detail_conf = _clamp(float(detail_confidence or 0.0), 0.0, 1.0)
        gaps = {name: _clamp(float(affinity.get(name, 0.0) or 0.0) - float(current.get(name, 0.0) or 0.0), -2.0, 2.0) for name in FEATURE_NAMES}
        group_features = {
            "EQ": ("air","warmth","brightness","bass","clarity","depth"),
            "DYNAMICS": ("punch","energy","intimacy","clarity"),
            "HARMONICS": ("warmth","vintage","energy","depth"),
            "STEREO": ("width","hypnotic","space","dreamy"),
            "REVERB": ("dreamy","space","hypnotic","intimacy"),
            "DELAY": ("hypnotic","dreamy","space"),
        }
        group_need = {}
        for group, feats in group_features.items():
            vals = [abs(gaps.get(f, 0.0)) for f in feats]
            group_need[group] = _clamp(sum(vals) / max(1, len(vals)), 0.0, 1.6)

        # 1) Fast derivative tracking. Exact coordinates periodically anchor the
        # stochastic SPSA field; every current-moment SPSA pass still updates all 50.
        for name in DSP50_SPECS:
            oldg = float(self._dsp50_gradients.get(name, 0.0))
            oldc = float(self._dsp50_confidence.get(name, 0.0))
            measured_here = name in spsa_gradients or name in coordinate_gradients
            if not measured_here:
                # v30 sparse trust-region SPSA intentionally does not probe every
                # node every pass. Missing is "unmeasured", not a zero gradient.
                self._dsp50_gradients[name] = oldg * 0.84
                self._dsp50_confidence[name] = oldc * 0.93
                continue
            sg = _clamp(float(spsa_gradients.get(name, oldg) or 0.0), -5.0, 5.0)
            if name in coordinate_gradients:
                cg = _clamp(float(coordinate_gradients.get(name, 0.0) or 0.0), -5.0, 5.0)
                measured = freshness * (0.86 * cg + 0.14 * sg)
                self._dsp50_gradients[name] = oldg * 0.30 + measured * 0.70
                self._dsp50_confidence[name] = min(1.0, oldc * 0.44 + 0.64 * freshness)
            else:
                measured = freshness * sg
                self._dsp50_gradients[name] = oldg * 0.58 + measured * 0.42
                self._dsp50_confidence[name] = min(0.92, oldc * 0.86 + 0.14 * freshness)

        # 2) Wider audible budget, still capped per processing family.
        caps = {"EQ": 4, "DYNAMICS": 3, "HARMONICS": 2, "STEREO": 3, "REVERB": 3, "DELAY": 2}
        def _prior_pressure(name: str) -> float:
            pv = _clamp(float(prior.get(name, 0.0) or 0.0), -1.0, 1.0)
            if abs(pv) < 1e-5:
                return 0.0
            cur_n = dsp50_normalized(self._dsp50_params.get(name, DEFAULT_PARAMS[name]), name)
            neutral_n = dsp50_normalized(DEFAULT_PARAMS[name], name)
            desired_n = _clamp(neutral_n + 0.28 * pv, 0.0, 1.0)
            return abs(desired_n - cur_n) * abs(pv) * (0.55 + 0.45 * detail_conf)

        ranked = sorted(
            DSP50_SPECS,
            key=lambda n: (
                abs(float(self._dsp50_gradients[n])) * (0.32 + 0.68 * float(self._dsp50_confidence[n]))
                * (0.88 + 0.55 * group_need.get(str(DSP50_SPECS[n]["group"]), 0.0))
                + 0.24 * _prior_pressure(n)
            ),
            reverse=True,
        )
        active: list[str] = []
        used = {g: 0 for g in caps}
        for name in ranked:
            group = str(DSP50_SPECS[name]["group"])
            mag = abs(float(self._dsp50_gradients[name]))
            prior_p = _prior_pressure(name)
            if mag < 0.00045 and prior_p < 0.018:
                continue
            if used.get(group, 0) >= caps.get(group, 3):
                continue
            active.append(name)
            used[group] = used.get(group, 0) + 1
            if len(active) >= 10:
                break
        active_set = set(active)

        mags = sorted((abs(float(self._dsp50_gradients[n])) for n in active), reverse=True)
        ref = max(0.0018, mags[min(7, len(mags)-1)] if mags else 0.0018)
        intensity = _clamp(self._dsp50_intensity, 0.35, 2.40)
        # Closed-loop quality governor: if the dry-anchored fidelity guard or final
        # limiter is already working hard, semantic motion slows rather than asking
        # the safety stages to become part of the sound.
        q = _clamp(float(getattr(self._status, "fidelity_quality_score", 1.0) or 1.0), 0.0, 1.0)
        lim = max(0.0, float(getattr(self._status, "limiter_reduction_db", 0.0) or 0.0))
        quality_motion = _clamp(0.42 + 0.72 * q - 0.10 * min(3.0, lim), 0.30, 1.0)
        intensity *= quality_motion

        # 3) Projected AdaBelief with a short-memory belief state: musical sections
        # are non-stationary, so stale verse momentum must not dominate a drop.
        self._dsp50_adam_t += 1
        t = max(1, int(self._dsp50_adam_t))
        beta1, beta2 = 0.84, 0.96
        b1corr = max(1e-6, 1.0 - beta1 ** t)
        b2corr = max(1e-6, 1.0 - beta2 ** t)
        updated = dict(self._dsp50_params)
        base_params = dict(self._dsp50_base_params or self._dsp50_params)

        for name in DSP50_SPECS:
            g = float(self._dsp50_gradients[name])
            conf = float(self._dsp50_confidence[name])
            cur = dsp50_normalized(updated.get(name, DEFAULT_PARAMS[name]), name)
            neutral = dsp50_normalized(DEFAULT_PARAMS[name], name)
            base_n = dsp50_normalized(base_params.get(name, DEFAULT_PARAMS[name]), name)
            pv = _clamp(float(prior.get(name, 0.0) or 0.0), -1.0, 1.0)
            desired_prior = _clamp(neutral + 0.28 * pv, 0.0, 1.0)
            prior_error = desired_prior - cur
            m_prev = float(self._dsp50_adam_m.get(name, 0.0))
            s_prev = float(self._dsp50_adam_s.get(name, 0.0))
            group = str(DSP50_SPECS[name]["group"])
            need = group_need.get(group, 0.0)

            if name in active_set:
                need_gain = 0.88 + 0.72 * need
                measured_term = math.tanh((g / ref) * 0.94) * (0.52 + 0.48 * conf) * freshness * need_gain
                # Detailed prompt prior is a slow target pull, not a blind preset.
                # It gives specific language (e.g. long dark reverb, slow delay,
                # silky air) a clear direction while the current-audio derivative
                # still determines whether the move actually helps this moment.
                prior_term = math.tanh(prior_error * 3.2) * abs(pv) * (0.30 + 0.70 * detail_conf)
                gnorm = 0.58 * measured_term + 0.42 * prior_term
                m = beta1 * m_prev + (1.0 - beta1) * gnorm
                surprise = gnorm - m
                second = beta2 * s_prev + (1.0 - beta2) * (surprise * surprise)
                mhat = m / b1corr
                shat = second / b2corr
                lr = 0.024 * intensity * (0.72 + 0.28 * conf)
                raw_step = lr * mhat / (math.sqrt(shat) + 0.20)
                max_step = (0.026 if name in DSP50_SLOW_NODES else 0.040) + (0.003 if name in DSP50_SLOW_NODES else 0.0045) * min(2.2, intensity)
                step = _clamp(raw_step, -max_step, max_step)
                # Tiny restoring force is toward the compiled prompt base, not
                # transparent neutral. This preserves the requested character
                # while live analysis only rides the current section.
                nxt = cur + step + (base_n - cur) * 0.006
            else:
                m = m_prev * 0.62
                second = s_prev * 0.80
                relax = 0.030 if conf < 0.25 else 0.020
                if abs(pv) > 0.22 and detail_conf > 0.45:
                    # Small background pull keeps detailed prompt clauses alive
                    # between noisy SPSA measurements without making the 50D deck twitch.
                    nxt = cur + (desired_prior - cur) * (0.006 + 0.008 * detail_conf)
                else:
                    nxt = cur + (base_n - cur) * relax

            # Hysteresis + prompt-anchor trust band. The strong audible x5 move
            # comes from the deterministic prompt base. Current-song analysis is a
            # micro-correction only, so it cannot make the mix hunt every 1.3 s.
            band = CONTINUITY_LIVE_BAND_SLOW if name in DSP50_SLOW_NODES else CONTINUITY_LIVE_BAND_FAST
            deadband = CONTINUITY_DEADBAND_SLOW if name in DSP50_SLOW_NODES else CONTINUITY_DEADBAND_FAST
            nxt = _clamp(nxt, max(0.0, base_n - band), min(1.0, base_n + band))
            if abs(nxt - cur) < deadband:
                nxt = cur
                # Let belief state decay gently while a sub-threshold correction is
                # held, avoiding a stored-up jump after several near-identical bars.
                m *= 0.88
                second *= 0.94

            self._dsp50_adam_m[name] = m
            self._dsp50_adam_s[name] = second
            updated[name] = dsp50_from_normalized(_clamp(nxt, 0.0, 1.0), name)

        self._dsp50_active_nodes = active
        self._dsp50_params = dsp50_safety_params(updated)

    def _dsp50_control_pass(self, rate: int, prompt: str, health: dict[str, Any]) -> bool:
        if (not self._dj_stereo_ring and not self._ai_in_ring) or rate <= 0:
            return False
        window_sec = 1.30
        n = int(rate * window_sec)
        dry = self._dj_stereo_ring.snapshot(n) if self._dj_stereo_ring else self._ai_in_ring.snapshot(n)
        # Source-frame stamp for semantic/audio alignment. This is captured with
        # the analysis window, before inference latency can advance the live input.
        capture_frame = int(self._capture_frame_index)
        if dry.shape[0] < int(rate * 0.34):
            with self._lock:
                self._status.ai_state = "Auto DSP listening · building high-confidence music window"
                self._status.ai_ready = True
            return False
        with self._lock:
            params = dict(self._dsp50_params)
            coords = self._select_dsp50_coordinate_nodes_locked()[:1]
            cycle = int(self._dsp50_cycle)
        captured_at = time.time()
        result = self._semantic_dsp50_gradient(prompt, dry, rate, params, coords, cycle)
        arrived_at = time.time()
        age = max(0.0, arrived_at - captured_at)
        # CPU inference is intentionally isolated/BelowNormal. Keep a useful amount
        # of authority for a 1-2 s result while still discounting genuinely stale
        # musical moments.
        freshness = math.exp(-max(0.0, age - 0.40) / 2.60)
        freshness = _clamp(freshness, 0.42, 1.0)
        spsa = dict(result.get("spsa_gradients") or {})
        coords_g = dict(result.get("coordinate_gradients") or {})
        affinity = dict(result.get("feature_affinity") or {})
        current_state = dict(result.get("current_feature_state") or {})
        dsp_prior = dict(result.get("dsp_prompt_prior") or {})
        detail_conf = float(result.get("detail_confidence") or 0.0)
        detail_model = str(result.get("detail_model") or "")
        with self._lock:
            self._update_dsp50_targets_locked(
                spsa, coords_g, freshness=freshness,
                feature_affinity=affinity, current_feature_state=current_state,
                dsp_prompt_prior=dsp_prior, detail_confidence=detail_conf,
            )
            self._dsp50_score = float(result.get("score") or 0.0)
            self._dsp50_cycle += 1
            self._params = self._compose_params_locked()
            self._schedule_dsp50_render_locked(
                self._params, capture_frame, rate, analysis_window_sec=window_sec, reason="live-gradient"
            )
            prev_pass = self._dsp50_last_pass_time
            self._dsp50_last_pass_time = arrived_at
            self._dsp50_last_window_ms = float(result.get("analysis_window_ms") or window_sec * 1000.0)
            self._dsp50_last_result_age_ms = age * 1000.0
            self._dsp50_last_render_count = int(result.get("render_count") or 0)
            update_hz = (1.0 / max(1e-4, arrived_at - prev_pass)) if prev_pass > 0 else 0.0
            grad_energy = max((abs(float(v)) for v in self._dsp50_gradients.values()), default=0.0)
            self._status.ai_enabled = True
            self._status.ai_ready = True
            self._status.ai_state = f"Auto DSP continuous · prompt anchor + hysteresis micro-correction · ∇ {grad_energy:.4f}"
            self._status.ai_model = str(result.get("model") or "laion/clap-htsat-unfused")
            self._status.ai_detail_model = detail_model
            self._status.ai_detail_confidence = detail_conf
            self._status.ai_device = "cpu"
            self._status.ai_inference_ms = float(result.get("inference_ms") or 0.0)
            self._status.ai_last_update = arrived_at
            self._status.dsp50_mode = True
            self._status.dsp50_prompt = self._dsp50_prompt
            self._status.dsp50_intensity = float(self._dsp50_intensity)
            self._status.dsp50_score = self._dsp50_score
            self._status.dsp50_cycle = self._dsp50_cycle
            self._status.dsp50_gradients = {k: round(float(v), 6) for k, v in self._dsp50_gradients.items()}
            self._status.dsp50_confidence = {k: round(float(v), 4) for k, v in self._dsp50_confidence.items()}
            self._status.dsp50_node_values = {k: round(float(self._params.get(k, DEFAULT_PARAMS[k])), 6) for k in DSP50_SPECS}
            self._status.dsp50_active_nodes = list(self._dsp50_active_nodes)
            self._status.dsp50_optimizer = "Anchored AdaBelief + hysteresis"
            self._status.dsp50_update_hz = round(float(update_hz), 3)
            self._status.dsp50_window_ms = round(float(self._dsp50_last_window_ms), 1)
            self._status.dsp50_result_age_ms = round(float(self._dsp50_last_result_age_ms), 1)
            self._status.dsp50_render_count = int(self._dsp50_last_render_count)
            self._status.dsp_params = dict(self._params)
        return True

    def _select_dj_nodes_locked(self) -> list[str]:
        names = list(DJ_NODE_SPECS)
        if not names:
            return []
        # Rotate for coverage but bias two slots toward the strongest retained
        # gradients so locally important controls get re-measured more often.
        ranked = sorted(names, key=lambda n: abs(float(self._dj_node_gradients.get(n, 0.0))), reverse=True)
        chosen: list[str] = []
        for n in ranked[:2]:
            if abs(float(self._dj_node_gradients.get(n, 0.0))) > 1e-5 and n not in chosen:
                chosen.append(n)
        while len(chosen) < DJ_NODES_PER_CYCLE:
            n = names[self._dj_node_cursor % len(names)]
            self._dj_node_cursor = (self._dj_node_cursor + 1) % len(names)
            if n not in chosen:
                chosen.append(n)
        return chosen

    def _update_dj_targets_locked(
        self, feature_affinity: dict[str, float], freshness: float = 1.0,
        current_feature_state: dict[str, float] | None = None,
        dsp_prompt_prior: dict[str, float] | None = None, detail_confidence: float = 0.0,
    ):
        """Update only the public 15D feature target.

        v30.4.4 intentionally removes the hidden raw-DSP residual controller.  The
        critic may still use local DSP probes to estimate which *feature* direction
        helps, but the audible controller can only move Air/Warmth/.../Vintage.
        """
        freshness = _clamp(float(freshness), 0.38, 1.0)
        current = current_feature_state or {}
        node_names = list(DJ_NODE_SPECS)
        jac = feature_param_jacobian(self._features, node_names)
        chain: dict[str, float] = {}
        for feat in FEATURE_NAMES:
            local_g = 0.0
            for node in node_names:
                local_g += float(self._dj_node_gradients.get(node, 0.0)) * float(jac.get(feat, {}).get(node, 0.0))
            target_sem = float(feature_affinity.get(feat, 0.0) or 0.0)
            now_sem = float(current.get(feat, 0.0) or 0.0)
            residual = _clamp(target_sem - now_sem, -1.7, 1.7)
            semantic_need = math.tanh(1.18 * residual)
            need_strength = 0.20 + 0.80 * min(1.0, abs(residual))
            weight = float(FEATURE_AI_WEIGHTS.get(feat, 0.8))
            # CLAP semantic residual remains the anchor. Local derivatives add
            # current-song adaptation but cannot dominate the 15D direction.
            local_mix = 0.56 if self._dj_cycle >= 1 else 0.36
            chain[feat] = weight * (
                local_mix * local_g * freshness * need_strength
                + (1.0 - local_mix) * semantic_need
            )

        mags = sorted((abs(v) for v in chain.values()), reverse=True)
        ref = max(0.010, mags[min(4, len(mags)-1)] if mags else 0.010)
        strongest = max(ref, mags[0] if mags else ref)
        # Threshold-aware sparse ranking. Dreamy/Space at 0.70 can enter the same
        # five-axis budget with ~30% less semantic evidence, while all other axes
        # keep the previous threshold.
        ordered = sorted(
            FEATURE_NAMES,
            key=lambda n: abs(chain[n]) / max(0.40, float(DJ_FEATURE_THRESHOLD_SCALE.get(n, 1.0))),
            reverse=True,
        )
        active = set(ordered[:5])  # sparse, readable DJ character instead of all-axis motion
        for feat in FEATURE_NAMES:
            raw = float(chain[feat])
            oldg = float(self._dj_feature_gradients.get(feat, 0.0))
            new_weight = DJ_RELUCTANT_GRADIENT_NEW_WEIGHT if feat in DJ_RELUCTANT_FEATURES else 0.24
            smoothg = oldg * (1.0 - new_weight) + raw * new_weight
            self._dj_feature_gradients[feat] = smoothg
            mag = abs(smoothg)
            activation_scale = float(DJ_FEATURE_THRESHOLD_SCALE.get(feat, 1.0))
            if feat in active and mag >= (0.11 * strongest * activation_scale):
                norm = _clamp(mag / max(ref, 0.52 * strongest), 0.0, 1.7)
                level = 0.10 + (DJ_FEATURE_LIMIT - 0.10) * math.tanh(1.35 * norm)
                desired = math.copysign(min(DJ_FEATURE_LIMIT, level), smoothg)
            else:
                desired = 0.0
            old = float(self._dj_target_features.get(feat, 0.0))
            if abs(desired - old) < DJ_FEATURE_DEADBAND:
                desired = old
            step_limit = DJ_FEATURE_STEP * (DJ_RELUCTANT_TARGET_STEP_SCALE if feat in DJ_RELUCTANT_FEATURES else 1.0)
            step = _clamp(desired - old, -step_limit, step_limit)
            self._dj_target_features[feat] = _clamp(old + step, -DJ_FEATURE_LIMIT, DJ_FEATURE_LIMIT)

        # No hidden audible DSP offsets in v30.4.4. Keep derivative telemetry only.
        self._dj_node_offsets = {name: 0.0 for name in DJ_NODE_SPECS}

    def _schedule_dj_features_locked(
        self, target: dict[str, float], capture_frame: int, rate: int,
        analysis_window_sec: float = 0.95, reason: str = "dj-gradient",
    ) -> int:
        rate = max(1, int(rate or 0))
        capture_frame = max(0, int(capture_frame or 0))
        center_back = max(0.0, float(analysis_window_sec or 0.0)) * 0.50
        desired = capture_frame - int(round(rate * (center_back + DJ_FEATURE_PREROLL_SEC)))
        min_ahead = int(round(rate * CONTINUITY_MIN_SCHEDULE_AHEAD_SEC))
        floor_frame = int(self._render_source_frame_index) + min_ahead
        if desired < floor_frame:
            desired = floor_frame
            self._dj_schedule_late_count += 1
        snap = {name: _clamp(float(target.get(name, 0.0)), -DJ_FEATURE_LIMIT, DJ_FEATURE_LIMIT) for name in FEATURE_NAMES}
        self._dj_feature_schedule.append((int(desired), snap, str(reason)))
        self._dj_feature_schedule.sort(key=lambda it: it[0])
        if len(self._dj_feature_schedule) > 24:
            self._dj_feature_schedule = self._dj_feature_schedule[-24:]
        return int(desired)

    def _scheduled_dj_features_locked(self, source_frame: int) -> dict[str, float]:
        latest = None
        while self._dj_feature_schedule and self._dj_feature_schedule[0][0] <= int(source_frame):
            latest = self._dj_feature_schedule.pop(0)
        if latest is not None:
            _, target, _ = latest
            self._dj_render_target_features = dict(target)
            self._dj_schedule_applied_count += 1
        return dict(self._dj_render_target_features)

    def _dj_control_pass(self, rate: int, prompt: str, health: dict[str, Any]) -> bool:
        if (not self._dj_stereo_ring and not self._ai_in_ring) or rate <= 0:
            return False
        window_sec = 0.95
        n = int(rate * window_sec)
        dry = self._dj_stereo_ring.snapshot(n) if self._dj_stereo_ring else self._ai_in_ring.snapshot(n)
        if dry.shape[0] < int(rate * 0.34):
            with self._lock:
                self._status.ai_state = "DJ Gradient listening · building current-moment window"
                self._status.ai_ready = True
            return False
        with self._lock:
            params = dict(self._params)
            nodes = self._select_dj_nodes_locked()[:1]
            cycle = int(self._dj_cycle)
            capture_frame = int(self._capture_frame_index)
        captured_at = time.time()
        result = self._semantic_dj_gradient(prompt, dry, rate, params, nodes, cycle)
        arrived_at = time.time()
        age = max(0.0, arrived_at - captured_at)
        freshness = math.exp(-max(0.0, age - 0.38) / 2.35)
        freshness = _clamp(freshness, 0.40, 1.0)
        grads = dict(result.get("gradients") or {})
        exact = dict(result.get("coordinate_gradients") or {})
        affinity = dict(result.get("feature_affinity") or {})
        current_state = dict(result.get("current_feature_state") or {})
        dsp_prior = dict(result.get("dsp_prompt_prior") or {})
        detail_conf = float(result.get("detail_confidence") or 0.0)
        detail_model = str(result.get("detail_model") or "")
        with self._lock:
            for node in DJ_NODE_SPECS:
                old = float(self._dj_node_gradients.get(node, 0.0))
                if node in grads:
                    measured = _clamp(float(grads.get(node, 0.0) or 0.0), -4.0, 4.0) * freshness
                    if node in exact:
                        measured = 0.86 * _clamp(float(exact[node]), -4.0, 4.0) * freshness + 0.14 * measured
                        self._dj_node_gradients[node] = old * 0.24 + measured * 0.76
                    else:
                        self._dj_node_gradients[node] = old * 0.46 + measured * 0.54
                else:
                    self._dj_node_gradients[node] = old * 0.80
            self._update_dj_targets_locked(
                affinity, freshness=freshness, current_feature_state=current_state,
                dsp_prompt_prior=dsp_prior, detail_confidence=detail_conf,
            )
            self._schedule_dj_features_locked(
                self._dj_target_features, capture_frame, rate, analysis_window_sec=window_sec, reason="live-15d"
            )
            self._dj_score = float(result.get("score") or 0.0)
            self._dj_cycle += 1
            prev_pass = self._dj_last_pass_time
            self._dj_last_pass_time = arrived_at
            self._dj_last_window_ms = float(result.get("analysis_window_ms") or window_sec * 1000.0)
            self._dj_last_result_age_ms = age * 1000.0
            update_hz = (1.0 / max(1e-4, arrived_at - prev_pass)) if prev_pass > 0 else 0.0
            self._params = self._compose_params_locked()
            grad_energy = max((abs(float(v)) for v in self._dj_node_gradients.values()), default=0.0)
            self._status.ai_enabled = True
            self._status.ai_ready = True
            self._status.ai_state = f"DJ 15D continuous · sparse musical target · ∇ {grad_energy:.4f}"
            self._status.ai_model = str(result.get("model") or "laion/clap-htsat-unfused")
            self._status.ai_detail_model = detail_model
            self._status.ai_detail_confidence = detail_conf
            self._status.ai_device = "cpu"
            self._status.ai_inference_ms = float(result.get("inference_ms") or 0.0)
            self._status.ai_last_update = arrived_at
            self._status.dj_score = self._dj_score
            self._status.dj_cycle = self._dj_cycle
            self._status.dj_update_hz = round(float(update_hz), 3)
            self._status.dj_window_ms = round(float(self._dj_last_window_ms), 1)
            self._status.dj_result_age_ms = round(float(self._dj_last_result_age_ms), 1)
            self._status.dj_feature_gradients = {k: round(float(v), 5) for k, v in self._dj_feature_gradients.items()}
            self._status.dj_node_gradients = {k: round(float(v), 5) for k, v in self._dj_node_gradients.items()}
            self._status.dj_node_values = {k: round(float(self._params.get(k, DEFAULT_PARAMS.get(k, 0.0))), 5) for k in DJ_NODE_SPECS}
            self._status.dsp_params = dict(self._params)
        return True

    def _ai_control_loop(self):
        # The AI loop never touches the audio callback. It listens to rolling
        # INPUT/OUTPUT windows and slowly corrects macro *gain* so the pretrained
        # CLAP critic sees the requested relative perceptual move.
        time.sleep(1.0)
        last_analysis = 0.0
        while not self._ai_stop_event.is_set() and not self._stop_event.is_set():
            with self._lock:
                enabled = bool(self._ai_enabled)
                running = bool(self._status.running)
                rate = int(self._ai_sample_rate or self._status.sample_rate or 0)
                features = dict(self._features)
                dj_mode = bool(self._dj_mode)
                dj_prompt = str(self._dj_prompt or "")
                dsp50_mode = False
                dsp50_prompt = str(self._dsp50_prompt or "")
            if not running:
                time.sleep(0.35)
                continue

            if dsp50_mode:
                if not dsp50_prompt:
                    with self._lock:
                        self._status.ai_enabled = True
                        self._status.ai_ready = False
                        self._status.ai_state = "Auto DSP waiting · enter a production prompt"
                    time.sleep(0.55)
                    continue
                health = self._semantic_health()
                device = str(health.get("device") or "cpu").lower()
                min_interval = 1.05 * _fx_gradient_scale()          # v31.30.26: slower on battery (frees the FX critic core)
                now = time.time()
                if now - last_analysis < min_interval:
                    time.sleep(0.10)
                    continue
                if not health.get("ready"):
                    with self._lock:
                        self._status.ai_enabled = True
                        self._status.ai_ready = False
                        self._status.ai_state = str(health.get("state") or "Auto DSP semantic model loading")
                        self._status.ai_device = str(health.get("device") or "")
                    time.sleep(1.1)
                    continue
                with self._lock:
                    need_profile = self._dsp50_profile_applied_prompt != dsp50_prompt
                if need_profile:
                    try:
                        profile = self._semantic_prompt_profile(dsp50_prompt)
                        with self._lock:
                            self._prime_dsp50_from_profile_locked(
                                dict(profile.get("dsp_prompt_prior") or {}),
                                detail_confidence=float(profile.get("detail_confidence") or 0.0),
                            )
                            self._dsp50_profile_applied_prompt = dsp50_prompt
                            self._params = self._compose_params_locked()
                            self._schedule_dsp50_render_locked(
                                self._params, int(self._capture_frame_index), rate,
                                analysis_window_sec=0.0, reason="prompt-anchor"
                            )
                            self._status.ai_detail_model = str(profile.get("detail_model") or "")
                            self._status.ai_detail_confidence = float(profile.get("detail_confidence") or 0.0)
                            self._status.ai_ready = True
                            self._status.ai_state = "Auto DSP music target compiled · refining against live audio"
                    except Exception:
                        pass
                try:
                    pass_started = time.time()
                    did = self._dsp50_control_pass(rate, dsp50_prompt, health)
                    if did:
                        last_analysis = pass_started
                    else:
                        time.sleep(0.40)
                except Exception:
                    with self._lock:
                        self._status.ai_ready = False
                        self._status.ai_state = "Auto DSP analysis retrying · live DSP unchanged"
                    last_analysis = time.time()
                    time.sleep(0.75)
                continue

            if dj_mode:
                if not dj_prompt:
                    with self._lock:
                        self._status.ai_enabled = True
                        self._status.ai_ready = False
                        self._status.ai_state = "DJ Gradient waiting · enter a prompt"
                    time.sleep(0.6)
                    continue
                health = self._semantic_health()
                device = str(health.get("device") or "cpu").lower()
                min_interval = 0.82 * _fx_gradient_scale()          # v31.30.26: slower on battery (frees the FX critic core)
                now = time.time()
                if now - last_analysis < min_interval:
                    time.sleep(0.10)
                    continue
                if not health.get("ready"):
                    with self._lock:
                        self._status.ai_enabled = True
                        self._status.ai_ready = False
                        self._status.ai_state = str(health.get("state") or "DJ semantic model loading")
                        self._status.ai_device = str(health.get("device") or "")
                    time.sleep(1.2)
                    continue
                with self._lock:
                    need_profile = self._dj_profile_applied_prompt != dj_prompt
                if need_profile:
                    try:
                        profile = self._semantic_prompt_profile(dj_prompt)
                        with self._lock:
                            # Text-space prior gives the deck an immediate, musically
                            # plausible first motion; measured audio derivatives take
                            # over as soon as the finite-difference sweeps arrive.
                            self._update_dj_targets_locked(
                                dict(profile.get("feature_affinity") or {}),
                                dsp_prompt_prior=dict(profile.get("dsp_prompt_prior") or {}),
                                detail_confidence=float(profile.get("detail_confidence") or 0.0),
                            )
                            self._dj_profile_applied_prompt = dj_prompt
                            self._status.ai_detail_model = str(profile.get("detail_model") or "")
                            self._status.ai_detail_confidence = float(profile.get("detail_confidence") or 0.0)
                            self._status.ai_ready = True
                            self._status.ai_state = "DJ Gradient detailed target mapped · measuring live DSP derivatives"
                    except Exception:
                        pass
                try:
                    pass_started = time.time()
                    did = self._dj_control_pass(rate, dj_prompt, health)
                    if did:
                        last_analysis = pass_started
                    else:
                        time.sleep(0.45)
                except Exception:
                    with self._lock:
                        self._status.ai_ready = False
                        self._status.ai_state = "DJ Gradient analysis retrying · live DSP unchanged"
                    last_analysis = time.time()
                    time.sleep(0.8)
                continue

            if not enabled:
                with self._lock:
                    self._status.ai_enabled = False
                    self._status.ai_state = "AI adaptation off · deterministic 15D DSP"
                    self._status.ai_ready = False
                time.sleep(0.8)
                continue

            now = time.time()
            if now - last_analysis < 3.6:
                time.sleep(0.22)
                continue

            active = {k: v for k, v in features.items() if abs(float(v)) >= 0.025}
            if not active:
                with self._lock:
                    for name in FEATURE_NAMES:
                        self._ai_scales[name] += (1.0 - self._ai_scales[name]) * 0.18
                    self._params = feature_params(self._features, self._ai_scales, self._effect_intensity)
                    self._status.ai_enabled = True
                    self._status.ai_state = "AI ready · move a feature to start semantic adaptation"
                    self._status.ai_scales = dict(self._ai_scales)
                time.sleep(0.8)
                continue

            health = self._semantic_health()
            if not health.get("ready"):
                with self._lock:
                    self._status.ai_enabled = True
                    self._status.ai_ready = False
                    self._status.ai_state = str(health.get("state") or "loading semantic model")
                    self._status.ai_device = str(health.get("device") or "")
                time.sleep(1.5)
                continue

            if not self._ai_in_ring or not self._ai_out_ring or rate <= 0:
                time.sleep(0.5)
                continue
            n = int(rate * 3.2)
            ina = self._ai_in_ring.snapshot(n)
            outa = self._ai_out_ring.snapshot(n)
            if ina.size < int(rate * 1.7) or outa.size < int(rate * 1.7):
                with self._lock:
                    self._status.ai_state = "AI listening · building analysis window"
                    self._status.ai_ready = True
                time.sleep(0.55)
                continue

            try:
                result = self._semantic_score_pair(ina, outa, rate)
                if self._ai_stop_event.is_set() or self._stop_event.is_set():
                    break
                delta = result.get("delta") or {}
                with self._lock:
                    # User intent remains primary. CLAP only adjusts how much of the
                    # hand-designed macro is needed for THIS song/section.
                    for name in FEATURE_NAMES:
                        value = float(self._features.get(name, 0.0) or 0.0)
                        old = float(self._ai_scales.get(name, 1.0))
                        if abs(value) < 0.025:
                            self._ai_scales[name] = old + (1.0 - old) * 0.22
                            continue
                        observed = float(delta.get(name, 0.0) or 0.0)
                        weight = float(FEATURE_AI_WEIGHTS.get(name, 0.8))
                        # Strong semantic controller: CLAP now has authority to make
                        # an unmistakable correction while still respecting the sign
                        # chosen by the user. Larger semantic goals and wider macro
                        # gain bounds make AI adaptation materially audible.
                        target_span = 0.052 * (0.62 + 0.38 * weight)
                        target = _clamp(value, -1.0, 1.0) * target_span
                        error = target - observed
                        direction = 1.0 if value > 0 else -1.0
                        normalized = _clamp(error / 0.020, -2.6, 2.6)
                        proposed = old + 0.34 * weight * normalized * direction
                        proposed = _clamp(proposed, 0.55, 2.35)
                        # Stronger than v23.2, but still bounded per analysis pass; the
                        # DSP layer then ramps the resulting targets continuously.
                        proposed = _clamp(proposed, old - 0.18, old + 0.18)
                        self._ai_scales[name] = proposed
                    self._params = feature_params(self._features, self._ai_scales, self._effect_intensity)
                    self._status.ai_enabled = True
                    self._status.ai_ready = True
                    self._status.ai_state = "AI dominant adaptation · 3.5× semantic authority"
                    self._status.ai_model = str(result.get("model") or "laion/clap-htsat-unfused")
                    self._status.ai_device = str(result.get("device") or health.get("device") or "")
                    self._status.ai_inference_ms = float(result.get("inference_ms") or 0.0)
                    self._status.ai_last_update = time.time()
                    self._status.ai_scales = {k: round(float(v), 3) for k, v in self._ai_scales.items()}
                    self._status.ai_delta = {k: round(float(delta.get(k, 0.0) or 0.0), 5) for k in FEATURE_NAMES}
                last_analysis = time.time()
            except Exception as exc:
                with self._lock:
                    # A semantic-analysis miss must never look like an audio-path
                    # failure. Keep the current DSP mapping and retry later.
                    self._status.ai_ready = False
                    self._status.ai_state = "Semantic AI analysis retrying · realtime DSP unchanged"
                last_analysis = time.time()
                time.sleep(1.1)

    def _meter_worker(self):
        """Low-priority metering/FFT worker; never blocks the audio producer."""
        import numpy as np
        nfft = 1024
        win = np.hanning(nfft).astype(np.float32)
        edges = np.geomspace(1, nfft // 2, 57).astype(int)
        while not self._meter_stop_event.is_set() and not self._stop_event.is_set():
            block = None
            rate = 0
            if self._meter_lock.acquire(False):
                try:
                    if self._meter_latest is not None:
                        block = self._meter_latest
                        self._meter_latest = None
                        rate = int(self._meter_rate)
                finally:
                    self._meter_lock.release()
            if block is None:
                time.sleep(0.045)
                continue
            try:
                x, y = block
                in_rms = float(np.sqrt(np.mean(x * x) + 1e-12))
                out_rms = float(np.sqrt(np.mean(y * y) + 1e-12))
                mono = y[:, 0] if y.shape[1] == 1 else (y[:, 0] + y[:, 1]) * 0.5
                if mono.shape[0] < nfft:
                    padded = np.zeros((nfft,), dtype=np.float32)
                    padded[:mono.shape[0]] = mono
                    mono = padded
                else:
                    mono = mono[-nfft:]
                mag = np.abs(np.fft.rfft(mono * win)) + 1e-9
                peak_mag = float(np.max(mag))
                vals = []
                for bi in range(56):
                    lo, hi = int(edges[bi]), max(int(edges[bi + 1]), int(edges[bi]) + 1)
                    vals.append(float(np.max(mag[lo:hi])) / max(1e-7, peak_mag))
                if self._lock.acquire(False):
                    try:
                        self._status.input_db = 20.0 * math.log10(max(in_rms, 1e-7))
                        self._status.output_db = 20.0 * math.log10(max(out_rms, 1e-7))
                        self._status.spectrum = vals
                    finally:
                        self._lock.release()
            except Exception:
                pass
            time.sleep(0.065)

    def _playback_worker(self, outstream, fifo: AudioFrameFifo, frames: int, rate: int, channels: int, source_stream=None):
        """Feed Audio Out on its own clock with a tiny elastic jitter buffer.

        The queue is intentionally only a few blocks deep. It removes block drops
        caused by capture and physical output endpoints having slightly
        different clocks, while keeping the safety cushion around 20-30 ms at 48 kHz.
        """
        import numpy as np

        mmcss = _enter_windows_pro_audio_thread()
        # v31.7 Concert Continuity Bridge: agentic mode already carries 15 s of
        # musical lookahead, so spend a little more queue latency on uninterrupted
        # playback.  This isolates Windows scheduler spikes from the audience path.
        # v31.30.4: the cushion is configurable (JOY_BRIDGE_BLOCKS, default 7 blocks ~128 ms).  With the 40 s agentic
        # render lag an extra half second of cushion costs nothing audible, and it absorbs the scheduler / GPU-worker
        # stalls that showed up as a dropout ("crackle") every few seconds.
        try:
            _bb = int(float(os.environ.get("JOY_BRIDGE_BLOCKS", "14") or 14))
        except Exception:
            _bb = 14
        _bb = max(3, min(60, _bb))
        base_target_fill = int(frames * _bb)
        target_fill = int(base_target_fill)
        max_target_fill = int(frames * max(14, 2 * _bb))
        startup_fill = int(frames * max(8, _bb + 1))
        last_sample = np.zeros((channels,), dtype=np.float32)
        last_good_block = np.zeros((frames, channels), dtype=np.float32)
        recovering = False
        smooth_ppm = 0.0
        last_status = 0.0
        consecutive_write_errors = 0
        started = False
        audible_output_confirmed = False
        self_volume_guard = _SelfAudioSessionVolumeGuard() if (bool(getattr(source_stream, "is_process_capture", False)) and not bool(getattr(source_stream, "is_managed_virtual_channel", False))) else None
        # Fractional PI drift servo. Instead of toggling N±1 every time the queue
        # crosses a threshold, accumulate a tiny correction and apply one sample
        # only when the integral reaches a whole frame. This removes periodic
        # resampling chatter while still following independent device clocks.
        drift_integral = 0.0
        correction_accum = 0.0
        last_underrun_at = 0.0
        stable_since = time.time()
        try:
            # v31.8.1: validate Audio Out immediately, before Spotify is routed.
            # During the intentional 15 s lookahead we keep the physical endpoint
            # alive with true silence.  This avoids a delayed WASAPI/Bluetooth
            # start failure at T+15 s and makes LET'S GO fail synchronously instead
            # of briefly routing Spotify and then returning it to the old device.
            outstream.start_stream()
            started = True
            if self_volume_guard is not None:
                self_volume_guard.enable()

            silence = np.zeros((frames, channels), dtype=np.float32)
            silence_bytes = silence.tobytes()
            # Confirm one real write as part of the preflight; start_stream() alone
            # can succeed on a stale wireless endpoint that fails on the first write.
            outstream.write(silence_bytes, exception_on_underflow=False)
            self._playback_ready_event.set()
            while not self._stop_event.is_set() and fifo.available() < startup_fill:
                try:
                    outstream.write(silence_bytes, exception_on_underflow=False)
                except Exception as exc:
                    consecutive_write_errors += 1
                    if consecutive_write_errors >= 2:
                        raise RuntimeError(f"Audio Out preflight failed: {exc}")
                    time.sleep(max(0.001, frames / float(rate) * 0.25))
            if self._stop_event.is_set():
                return
            # v31.17: the capture side hands its start-up burst to the bridge in one
            # go.  Drain that excess BEFORE the first audible block; otherwise the
            # audience path carries an extra ~0.4 s forever (the drift servo trims
            # only a couple of samples per block).  Nothing audible is lost: no
            # music has been played yet.
            excess = int(fifo.available()) - int(startup_fill)
            if excess >= frames:
                fifo.read(int(excess))
            # First musical block fades up from the intentional lookahead silence.
            recovering = True
            consecutive_write_errors = 0

            while not self._stop_event.is_set():
                if self_volume_guard is not None:
                    self_volume_guard.refresh()
                avail = fifo.available()
                error_blocks = (float(avail) - float(target_fill)) / float(max(1, frames))
                drift_integral = _clamp(drift_integral + error_blocks * 0.018, -2.5, 2.5)
                servo = _clamp(0.10 * error_blocks + 0.022 * drift_integral, -0.32, 0.32)
                correction_accum += servo
                correction = 0
                if correction_accum >= 1.0:
                    correction = 1
                    correction_accum -= 1.0
                elif correction_accum <= -1.0 and avail >= frames:
                    correction = -1
                    correction_accum += 1.0
                # Latency reclaim (v31.17): anything above target+1 block is latency,
                # not safety (the audience heard it 0.9 s late in the field with the
                # old +2-samples-above-4-blocks rule).  Proportional: 4 samples per
                # excess block, capped at 16 per block (0.39 %, under 7 cents), via
                # the whole-block linear resampler in _elastic_micro_adjust.
                excess_blocks = (float(avail) - float(target_fill)) / float(max(1, frames))
                if excess_blocks > 1.0:
                    correction = max(correction, int(min(16, round(4.0 * excess_blocks))))
                # v31.30.9 deficit reclaim: the mirror image.  Below target-1 block the cushion is REFILLED at up to
                # 16 samples per block (0.39 %, under 7 cents, the same resampler): a stall used to cost the cushion
                # permanently (the servo alone gives back one sample per ~3 blocks = 20 minutes for 0.4 s), so every
                # later stall became an audible dropout.  Now 0.4 s comes back in ~50 s.
                deficit_blocks = (float(target_fill) - float(avail)) / float(max(1, frames))
                if deficit_blocks > 1.0 and avail >= frames:
                    correction = min(correction, -int(min(16, round(4.0 * deficit_blocks))))
                consume = max(2, frames + correction)
                # If a +1 drift correction is the only reason the queue is short,
                # skip that correction for this block instead of declaring an
                # underrun. Audio Out still has a complete block available.
                if avail >= frames and avail < consume:
                    consume = frames
                    correction = 0
                block = fifo.read(consume)

                if block is None:
                    partial = fifo.read_partial(consume)
                    y = np.zeros((frames, channels), dtype=np.float32)
                    valid_n = min(frames, int(partial.shape[0]))
                    if valid_n > 0:
                        y[:valid_n] = partial[:valid_n]
                        edge = y[valid_n - 1].copy()
                    else:
                        edge = last_sample.copy()
                    remain = frames - valid_n
                    # v31.7 dropout concealment: never replace a short scheduler miss
                    # with a block of silence. Recycle a tiny recent waveform grain,
                    # crossfade into it, and apply a shallow decay. This is far less
                    # audible than a zero-filled hole and preserves continuous room/FX tails.
                    if remain > 0:
                        grain_n = min(max(128, int(rate * 0.018)), frames)
                        grain = last_good_block[-grain_n:]
                        if grain.size and float(np.max(np.abs(grain))) > 1e-7:
                            reps = int(math.ceil(remain / float(grain_n)))
                            conceal = np.tile(grain, (reps, 1))[:remain].astype(np.float32, copy=False)
                            xfade = min(remain, grain_n, max(96, int(rate * 0.006)))
                            if xfade > 1:
                                t = np.linspace(0.0, 1.0, xfade, endpoint=True, dtype=np.float32)[:,None]
                                conceal[:xfade] = edge[None,:]*(1.0-t) + conceal[:xfade]*t
                            decay = np.linspace(1.0, 0.94, remain, endpoint=True, dtype=np.float32)[:,None]
                            y[valid_n:] = conceal * decay
                        else:
                            fade_n = min(remain, max(64, int(rate * 0.012)))
                            if fade_n > 1:
                                t = np.linspace(0.0, 1.0, fade_n, endpoint=True, dtype=np.float32)
                                ramp = (0.5 + 0.5 * np.cos(np.pi * t))[:, None]
                                y[valid_n:valid_n + fade_n] = edge[None, :] * ramp
                    recovering = True
                    last_sample = y[-1].copy()
                    last_underrun_at = time.time()
                    stable_since = last_underrun_at
                    target_fill = min(max_target_fill, target_fill + 2 * frames)
                    self._fx_guard_level = 1.0
                    self._fx_guard_last_xrun = time.time()
                    with self._lock:
                        self._status.xruns += 1
                        self._status.concealments += 1
                else:
                    y = _elastic_micro_adjust(block, frames)
                    if recovering:
                        fade_n = min(frames, max(64, int(rate * 0.012)))
                        if fade_n > 1:
                            t = np.linspace(0.0, 1.0, fade_n, endpoint=True, dtype=np.float32)
                            ramp = np.sin(0.5 * np.pi * t)[:, None]
                            y[:fade_n] *= ramp
                        recovering = False
                    last_sample = y[-1].copy()
                # Keep one exact recent output block for non-silent concealment.
                last_good_block = y.copy()

                try:
                    outstream.write(y.astype(np.float32, copy=False).tobytes(), exception_on_underflow=False)
                    consecutive_write_errors = 0
                    if not audible_output_confirmed:
                        out_rms = float(np.sqrt(np.mean(y * y) + 1e-20))
                        if out_rms >= 2.0e-5:
                            audible_output_confirmed = True
                            exact_route = bool(getattr(outstream, "is_exact_selected_output", False))
                            if exact_route and source_stream is not None and hasattr(source_stream, "allow_source_suppression"):
                                try:
                                    source_stream.allow_source_suppression()
                                except Exception:
                                    pass
                            with self._lock:
                                backend = str(getattr(outstream, "backend_name", "PyAudioWPatch WASAPI"))
                                self._status.playback_backend = backend
                                if exact_route:
                                    self._status.message = f"Audio Out confirmed · EXACT selected endpoint · {backend}"
                                else:
                                    self._status.message = f"Audio Out fallback active · source left audible for safety · {backend}"
                except Exception as exc:
                    consecutive_write_errors += 1
                    self._fx_guard_level = 1.0
                    self._fx_guard_last_xrun = time.time()
                    with self._lock:
                        self._status.xruns += 1
                        self._status.message = f"Audio Out recovery: {exc}"
                    if consecutive_write_errors >= 4:
                        self._playback_error = f"Audio Out stream failed repeatedly: {exc}"
                        self._stop_event.set()
                        break
                    time.sleep(max(0.001, frames / float(rate) * 0.35))

                inst_ppm = (float(correction) / float(max(1, frames))) * 1_000_000.0
                smooth_ppm = smooth_ppm * 0.94 + inst_ppm * 0.06
                now = time.time()
                # After a long stable period, very slowly return toward the normal
                # cushion. A machine that needed extra protection keeps it for tens
                # of seconds instead of oscillating between latency and underruns.
                if target_fill > base_target_fill and now - max(last_underrun_at, stable_since) > 45.0:
                    target_fill = max(base_target_fill, target_fill - max(1, frames // 4))
                    stable_since = now
                if now - last_status > 0.12:
                    fill_ms = 1000.0 * float(fifo.available()) / float(rate)
                    with self._lock:
                        self._status.bridge_fill_ms = fill_ms
                        self._status.bridge_target_ms = 1000.0 * float(target_fill) / float(rate)
                        self._status.drift_correction_ppm = smooth_ppm
                    last_status = now
        except Exception as exc:
            # Never let Audio Out die silently while capture keeps running. Surface
            # the failure to the producer thread, which stops/restores the source.
            self._playback_error = f"Audio Out backend failed: {exc}"
            self._playback_ready_event.set()
            self._stop_event.set()
            try:
                with self._lock:
                    self._status.xruns += 1
                    self._status.message = self._playback_error
            except Exception:
                pass
        finally:
            if self_volume_guard is not None:
                try:
                    self_volume_guard.close()
                except Exception:
                    pass
            if started:
                try:
                    outstream.stop_stream()
                except Exception:
                    pass
            _leave_windows_pro_audio_thread(mmcss)

    def _run(self, in_idx: int, out_idx: int):
        pyaudio = None
        p = instream = outstream = None
        mmcss = _enter_windows_pro_audio_thread()
        try:
            import numpy as np

            pyaudio = self._imports()
            p = pyaudio.PyAudio()
            instream, outstream, rate, channels, frames, out_frames, _ = self._open_pair(p, pyaudio, in_idx, out_idx)
            dsp = RealtimeDSP(rate, channels)
            dj_mixer = RealtimeDJMixer(rate, channels)
            self._dj_mixer_ref = dj_mixer
            with self._lock:
                self._reset_dsp50_timeline_locked()
            # Capture-side semantic preview.  The newest dry audio is exposed to
            # Auto DSP immediately, while the exact original waveform waits here
            # before entering the DSP graph.  No resampling/reconstruction occurs.
            active_lookahead_sec = AGENTIC_LOOKAHEAD_SEC if self._agentic_live_enabled else CONTINUITY_LOOKAHEAD_SEC
            lookahead_frames = int(round(rate * active_lookahead_sec))
            raw_lookahead_fifo = AudioFrameFifo(max(int(rate * (active_lookahead_sec + 0.75)), lookahead_frames + frames * 12), channels)
            # Output-clock elastic FIFO remains independent of the intentional
            # semantic lookahead latency.
            playback_fifo = AudioFrameFifo(int(rate * 1.80), channels)

            # v30.4.23: no GPU restoration / neural post-render stage.
            # StudioDSP output goes directly to the elastic playback FIFO.
            self._neural_polish_worker = None
            self._neural_polish_mix = 0.0
            self._playback_error = ""
            self._ai_in_ring = AudioRing(rate, seconds=5.0)
            self._ai_out_ring = AudioRing(rate, seconds=5.0)
            self._dj_stereo_ring = StereoAudioRing(rate, seconds=max(30.0, active_lookahead_sec + 12.0), channels=2)
            self._dj_render_ring = StereoAudioRing(rate, seconds=30.0, channels=2)
            # v31.14 TimeWeave: the mixer may read future material (audio the
            # listener has not heard yet) directly from the lookahead ring.
            dj_mixer._future_ring = self._dj_stereo_ring
            self._ai_sample_rate = int(rate)
            self._ai_stop_event.clear()
            process_capture = bool(getattr(instream, "is_process_capture", False))

            # Start/validate the exact physical Audio Out BEFORE enabling the
            # per-app Spotify → JoyMetric route.  Playback writes intentional
            # silence until the lookahead FIFO is ready.
            self._playback_ready_event.clear()
            self._playback_thread = threading.Thread(
                target=self._playback_worker,
                args=(outstream, playback_fifo, out_frames, rate, channels, instream),
                daemon=True,
                name="joymetric-audio-out",
            )
            self._playback_thread.start()
            preflight_deadline = time.time() + 2.2
            while not self._playback_ready_event.is_set() and time.time() < preflight_deadline:
                if self._playback_error:
                    raise RuntimeError(self._playback_error)
                if self._stop_event.is_set():
                    raise RuntimeError(self._playback_error or "Audio Out preflight stopped unexpectedly.")
                time.sleep(0.01)
            if self._playback_error:
                raise RuntimeError(self._playback_error)
            if not self._playback_ready_event.is_set():
                raise RuntimeError(self._playback_error or "Selected Audio Out did not start within the WASAPI preflight window.")

            # Only now migrate Spotify. If routing/capture fails, the output has
            # already been proven good and router cleanup restores Spotify safely.
            instream.start_stream()
            try:
                io_latency_ms = 1000.0 * (float(instream.get_input_latency()) + float(outstream.get_output_latency()))
            except Exception:
                io_latency_ms = 0.0
            buffer_ms = 1000.0 * float(frames) / float(rate)
            with self._lock:
                self._status.running = True
                self._status.sample_rate = rate
                self._status.channels = channels
                self._status.buffer_frames = frames
                self._status.buffer_ms = buffer_ms
                self._status.output_buffer_frames = int(out_frames)
                self._status.output_buffer_ms = 1000.0 * float(out_frames) / float(rate)
                self._status.continuity_lookahead_ms = 1000.0 * float(lookahead_frames) / float(rate)
                self._status.io_latency_ms = io_latency_ms
                self._status.half_rate = False
                self._status.ai_enabled = bool(self._ai_enabled or self._dj_mode or self._dsp50_mode)
                self._status.dj_mode = bool(self._dj_mode)
                self._status.dj_prompt = self._dj_prompt
                self._status.effect_intensity = float(self._effect_intensity_target)
                self._status.neural_polish_enabled = False
                self._status.neural_polish_wet = 0.0
                self._status.neural_polish_hold_ms = 0.0
                self._status.dsp50_mode = False
                self._status.dsp50_prompt = self._dsp50_prompt
                self._status.dsp50_intensity = float(self._dsp50_intensity)
                self._status.dsp_backend = str(dsp.backend_name)
                if process_capture:
                    managed = bool(getattr(instream, "is_managed_virtual_channel", False))
                    self._status.source_kind = "joymetric_virtual_driver" if managed else "input"
                    self._status.capture_backend = (
                        "JoyMetric Virtual Input · WASAPI loopback" if managed else "WASAPI input"
                    )
                    self._status.playback_backend = str(getattr(outstream, "backend_name", "PyAudioWPatch WASAPI"))
                    self._status.source_pid = int(getattr(instream, "root_pid", 0) or 0)
                    self._status.source_suppressed = bool(getattr(instream, "source_suppressed", False))
                    self._status.source_attenuation_db = float(getattr(getattr(instream, "_attenuator", None), "attenuation_db", 0.0) or 0.0)
                    self._status.source_volume_compensation = str(getattr(instream, "volume_path", "direct route"))
                if self._dsp50_mode:
                    self._status.ai_state = "50D Gradient Lab listening · direct DSP derivative engine warming up"
                elif self._dj_mode:
                    self._status.ai_state = "DJ Gradient listening · prompt derivative engine warming up"
                else:
                    self._status.ai_state = "AI listening · model loads out-of-band" if self._ai_enabled else "AI adaptation off"
                self._status.message = f"BUFFERING · MusicCLAP + StudioDSP · {rate} Hz"

            # Audio Out is already running from the preflight handshake above.
            self._meter_stop_event.clear()
            self._meter_rate = int(rate)
            self._meter_thread = threading.Thread(target=self._meter_worker, daemon=True, name="joymetric-meter")
            self._meter_thread.start()
            if _fx_enabled():
                self._ai_thread = threading.Thread(target=self._ai_control_loop, daemon=True, name="joymetric-semantic-controller")
                self._ai_thread.start()

            # Realtime-owned snapshots. The audio thread never waits for the UI or
            # semantic controller lock; if a control update is in progress it uses
            # the previous complete snapshot for one block.
            rt_params = dict(self._params)
            rt_features = dict(self._features)
            rt_scales = dict(self._ai_scales)
            rt_agentic_enabled = bool(self._agentic_live_enabled)
            rt_agentic_controls = dict(self._agentic_controls)
            _last_read_ret = 0.0; _gap_max = 0.0; _dsp_max = 0.0; _diag_t = time.perf_counter()
            while not self._stop_event.is_set():
                if self._playback_error:
                    raise RuntimeError(self._playback_error)
                try:
                    _t_read0 = time.perf_counter()
                    raw = instream.read(frames, exception_on_overflow=False)
                    _t_read1 = time.perf_counter()
                    # v31.30.10 diagnostics: the longest wait for capture and the longest DSP block, over the last second
                    _gap = (_t_read1 - _last_read_ret) * 1000.0 if _last_read_ret else 0.0; _last_read_ret = _t_read1
                    _gap_max = max(_gap_max, _gap)
                    if process_capture and self._lock.acquire(False):
                        try:
                            self._status.source_suppressed = bool(getattr(instream, "source_suppressed", False))
                            self._status.source_attenuation_db = float(getattr(getattr(instream, "_attenuator", None), "attenuation_db", 0.0) or 0.0)
                            self._status.source_volume_compensation = str(getattr(instream, "volume_path", "direct route"))
                        finally:
                            self._lock.release()
                    x = np.frombuffer(raw, dtype=np.float32)
                    if x.size == 0:
                        continue
                    x = x.reshape(-1, channels).copy()
                    # Preview ring receives the *current* dry capture before the
                    # deliberate lookahead delay. Agentic mode defaults to 15 s so
                    # planning can settle before the corresponding waveform is heard.
                    if self._dj_stereo_ring:
                        self._dj_stereo_ring.append(x)
                    raw_lookahead_fifo.write(x)
                    self._capture_frame_index += int(x.shape[0])
                    if raw_lookahead_fifo.available() < lookahead_frames + x.shape[0]:
                        # Startup only: keep capturing/analyzing until the preview
                        # cushion is full. Audio Out has not started yet, so there is
                        # no dry->wet switch or artificial silence inside playback.
                        continue
                    delayed_x = raw_lookahead_fifo.read(x.shape[0])
                    if delayed_x is None:
                        continue
                    source_frame_start = int(self._render_source_frame_index)

                    got_state = self._lock.acquire(False)
                    if got_state:
                        try:
                            # v30.4.5 intensity continuity: a UI macro change is
                            # converted to a slow, bounded control trajectory before
                            # remapping the 15D deck. This applies in manual and DJ.
                            block_s = float(max(1, delayed_x.shape[0])) / float(rate)
                            intensity_alpha = 1.0 - math.exp(-block_s / (0.55 / CONTROL_RESPONSE_SPEED))
                            intensity_step = (1.10 * CONTROL_RESPONSE_SPEED) * block_s
                            i_cur = float(self._effect_intensity)
                            i_tgt = float(self._effect_intensity_target)
                            i_next = i_cur + (i_tgt - i_cur) * intensity_alpha
                            i_next = i_cur + _clamp(i_next - i_cur, -intensity_step, intensity_step)
                            intensity_changed = abs(i_next - i_cur) > 1e-7
                            self._effect_intensity = _clamp(i_next, 0.0, 1.60)
                            if self._dj_mode:
                                scheduled = self._scheduled_dj_features_locked(source_frame_start)
                                for name in FEATURE_NAMES:
                                    cur = float(self._features.get(name, 0.0))
                                    tgt = float(scheduled.get(name, 0.0))
                                    if name in DJ_RELUCTANT_FEATURES:
                                        smooth_sec = DJ_RELUCTANT_SMOOTH_SEC
                                        slew_per_sec = DJ_RELUCTANT_SLEW_PER_SEC
                                    else:
                                        smooth_sec = DJ_FEATURE_SMOOTH_SEC
                                        slew_per_sec = DJ_FEATURE_SLEW_PER_SEC
                                    alpha = 1.0 - math.exp(-block_s / max(0.25, smooth_sec))
                                    max_step = slew_per_sec * block_s
                                    proposed = cur + (tgt - cur) * alpha
                                    proposed = cur + _clamp(proposed - cur, -max_step, max_step)
                                    self._features[name] = _clamp(proposed, -DJ_FEATURE_LIMIT, DJ_FEATURE_LIMIT)
                                self._params = self._compose_params_locked()
                                self._status.features = dict(self._features)
                                self._status.dsp_params = dict(self._params)
                                self._status.dj_node_values = {k: round(float(self._params.get(k, DEFAULT_PARAMS.get(k, 0.0))), 5) for k in DJ_NODE_SPECS}
                            elif intensity_changed and not self._dsp50_mode:
                                self._params = self._compose_params_locked()
                                self._status.dsp_params = dict(self._params)
                            self._status.effect_intensity = float(self._effect_intensity)
                            if self._dsp50_mode:
                                rt_params = self._scheduled_dsp50_params_locked(source_frame_start)
                            else:
                                rt_params = dict(self._params)
                            rt_features = dict(self._features)
                            rt_scales = dict(self._ai_scales)
                            rt_agentic_enabled = bool(self._agentic_live_enabled)
                            rt_agentic_controls = dict(self._agentic_controls)
                        finally:
                            self._lock.release()
                    params = rt_params
                    features = rt_features
                    scales = rt_scales
                    if self._dsp50_mode:
                        hypnotic = 0.0
                    else:
                        eff_h = float(features.get("hypnotic", 0.0) or 0.0) * _effective_ai_scale(float(scales.get("hypnotic", 1.0) or 1.0))
                        hypnotic = max(0.0, eff_h)
                        if hypnotic > 2.0:
                            hypnotic /= 100.0
                    # Keep a second ring aligned to delayed/source time. Planning
                    # looks ahead on _dj_stereo_ring; beat/transient execution reads
                    # this ring so creative events land on the audible waveform.
                    if self._dj_render_ring:
                        self._dj_render_ring.append(delayed_x)
                        # v31.18: the mixer sees the same frame counter the agent measures on
                        try:
                            rt_agentic_controls['grid_frame_end'] = float(self._dj_render_ring.total)
                        except Exception:
                            pass
                    dsp_input = delayed_x
                    if self._pending_drum_kit is not None:
                        try:
                            dj_mixer.drums.set_kit(self._pending_drum_kit)
                        except Exception:
                            pass
                        self._pending_drum_kit = None
                    if self._pending_ai_loop is not None:
                        _buf, _seq = self._pending_ai_loop; self._pending_ai_loop = None
                        try:
                            dj_mixer.set_ai_loop(_buf, _seq)
                        except Exception:
                            pass
                    if self._pending_synth_clip is not None:
                        _buf, _sf, _g, _sq = self._pending_synth_clip; self._pending_synth_clip = None
                        try:
                            dj_mixer.synth.schedule(_buf, _sf, _g, _sq)
                        except Exception:
                            pass
                    if self._pending_synth_cancel is not None:
                        _sq = self._pending_synth_cancel; self._pending_synth_cancel = None
                        try:
                            dj_mixer.synth.cancel(_sq)
                        except Exception:
                            pass
                    if self._pending_takeover:
                        _tks = self._pending_takeover; self._pending_takeover = []
                        for _tk in _tks:
                            try:
                                dj_mixer.takeover.schedule(_tk[0], _tk[1], _tk[2], _tk[3], _tk[4], _tk[5], _tk[6])
                            except Exception:
                                pass
                    if self._pending_takeover_cancel is not None:
                        _sq = self._pending_takeover_cancel; self._pending_takeover_cancel = None
                        try:
                            dj_mixer.takeover.cancel(_sq)
                        except Exception:
                            pass
                    if self._pending_cancel_clip:
                        _ccs = self._pending_cancel_clip; self._pending_cancel_clip = []
                        for _cc in _ccs:
                            try:
                                if not dj_mixer.cancel_deck.schedule(_cc[0], _cc[1], _cc[2], _cc[3]):
                                    self._cancel_sched_errors = int(getattr(self, '_cancel_sched_errors', 0)) + 1
                            except Exception as _exc:
                                self._cancel_sched_errors = int(getattr(self, '_cancel_sched_errors', 0)) + 1; self._cancel_sched_error = str(_exc)[:100]
                    if self._pending_lineduck:
                        _lds = self._pending_lineduck; self._pending_lineduck = []
                        for _ld in _lds:
                            try:
                                dj_mixer.lineduck_y.schedule(_ld[0], _ld[1], _ld[2], _ld[3], _ld[4]); dj_mixer.lineduck_out.schedule(_ld[0], _ld[1], _ld[2], _ld[3], _ld[4])
                            except Exception:
                                pass
                    if self._pending_lineduck_cancel is not None:
                        _sq = self._pending_lineduck_cancel; self._pending_lineduck_cancel = None
                        try:
                            dj_mixer.lineduck_y.cancel(_sq); dj_mixer.lineduck_out.cancel(_sq)
                        except Exception:
                            pass
                    if rt_agentic_enabled:
                        fx_guard = float(self._fx_guard_level)
                        if fx_guard > 1e-4 and (time.time() - float(self._fx_guard_last_xrun)) > 4.0:
                            # ~2-3 s smooth recovery at the default 2048f quantum.
                            fx_guard *= 0.982
                            if fx_guard < 0.015:
                                fx_guard = 0.0
                            self._fx_guard_level = fx_guard
                        if fx_guard > 1e-4:
                            safe_controls = dict(rt_agentic_controls)
                            authority = max(0.18, 1.0 - 0.78 * fx_guard)
                            for key in ('noise_riser','fx_reverse_swell','fx_snare_rush','doubletime','fx_rack_wet','fx_echo_send','fx_echo_feedback','fx_gate','fx_width'):
                                try:
                                    safe_controls[key] = float(safe_controls.get(key, 0.0) or 0.0) * authority
                                except Exception:
                                    pass
                            # Keep impact/drum punch more present so recovery does not
                            # sound like the transition disappeared entirely.
                            try:
                                safe_controls['drum_drive'] = float(safe_controls.get('drum_drive',0.0) or 0.0) * max(.55, authority)
                            except Exception:
                                pass
                            dsp_input = dj_mixer.process(delayed_x, safe_controls)
                        else:
                            dsp_input = dj_mixer.process(delayed_x, rt_agentic_controls)
                    if rt_agentic_enabled:
                        params = _shared_agentic_headroom(params, rt_agentic_controls)
                    if _fx_enabled():
                        y = dsp.process(dsp_input, params, hypnotic)
                    else:
                        y = _fx_bypass_limit(dsp_input)          # v31.30.28: FX removed -> raw VocalWeave mix (+ safety), no 15D StudioDSP per block
                    source_n = int(delayed_x.shape[0])
                    self._render_source_frame_index += source_n

                    # v30.4.23 direct full-band StudioDSP path. No generative
                    # restoration, no GPU queue and no neural reconstruction latency.
                    y_final = y
                    dropped = playback_fifo.write(y_final)
                    _dsp_ms = (time.perf_counter() - _t_read1) * 1000.0; _dsp_max = max(_dsp_max, _dsp_ms)
                    if _t_read1 - _diag_t > 1.0:
                        if self._lock.acquire(False):
                            try:
                                self._status.dsp_max_ms = round(_dsp_max, 1); self._status.capture_gap_max_ms = round(_gap_max, 1)
                            finally:
                                self._lock.release()
                        _dsp_max = 0.0; _gap_max = 0.0; _diag_t = _t_read1
                    if dropped:
                        with self._lock:
                            self._status.xruns += 1

                    if self._ai_in_ring:
                        self._ai_in_ring.append(delayed_x)
                    if self._ai_out_ring:
                        self._ai_out_ring.append(y_final)

                    if self._meter_lock.acquire(False):
                        try:
                            self._meter_latest = (delayed_x.copy(), y_final.copy())
                            self._meter_rate = int(rate)
                        finally:
                            self._meter_lock.release()
                    if self._lock.acquire(False):
                        try:
                            self._status.neural_polish_enabled = False
                            self._status.neural_polish_wet = 0.0
                            self._status.neural_polish_ready = False
                            self._status.neural_polish_rtf = 0.0
                            self._status.neural_polish_inference_ms = 0.0
                            self._status.neural_polish_delay_ms = 0.0
                            self._status.neural_polish_hold_ms = 0.0
                            self._status.neural_polish_mix = 0.0
                            self._status.neural_polish_deadline_misses = 0
                            self._status.neural_polish_overload_bypasses = 0
                            self._status.neural_polish_message = "GPU restoration removed"
                            if process_capture:
                                if bool(getattr(instream, "is_managed_virtual_channel", False)):
                                    self._status.message = f"LIVE · JOYMETRIC CHANNEL (processed-only) → MusicCLAP → 15D StudioDSP → Audio Out · {rate} Hz"
                                else:
                                    suppress = "processed-only" if self._status.source_suppressed else "app-isolated"
                                    self._status.message = f"LIVE · APP TAP ({suppress}) → MusicCLAP → 15D StudioDSP → Audio Out · {rate} Hz"
                            else:
                                self._status.message = f"LIVE · MusicCLAP → 15D StudioDSP → Audio Out · {rate} Hz"
                            self._status.limiter_reduction_db = float(dsp.limiter.last_reduction_db)
                            self._status.master_comp_reduction_db = float(dsp.master_comp.last_reduction_db)
                            self._status.auto_gain_reduction_db = float(dsp.auto_gain.last_reduction_db)
                            self._status.fidelity_residual_gain = float(dsp.fidelity_guard.last_residual_gain)
                            self._status.fidelity_quality_score = float(dsp.fidelity_guard.last_quality_score)
                            self._status.declick_repairs = int(dsp.declick.repairs)
                            self._status.dsp_backend = str(dsp.backend_name)
                            self._agentic_mixer_status = dict(dj_mixer.status)
                        finally:
                            self._lock.release()
                except Exception as exc:
                    # Read-side faults are recoverable unless they repeat through the
                    # outer loop; the elastic bridge prevents one bad block becoming a
                    # hard waveform discontinuity at Audio Out.
                    with self._lock:
                        self._status.xruns += 1
                        self._status.message = f"Input stream recovery: {exc}"
                    time.sleep(max(0.001, frames / float(rate) * 0.25))
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
                self._status.running = False
                self._status.message = f"Realtime error: {exc}"
        finally:
            self._stop_event.set()
            self._ai_stop_event.set()
            self._meter_stop_event.set()
            mt = self._meter_thread
            if mt and mt.is_alive() and mt is not threading.current_thread():
                mt.join(timeout=0.35)
            self._meter_thread = None
            pw = self._neural_polish_worker
            if pw is not None:
                try:
                    pw.stop()
                except Exception:
                    pass
            self._neural_polish_worker = None
            pb = self._playback_thread
            if pb and pb.is_alive() and pb is not threading.current_thread():
                pb.join(timeout=1.2)
            self._playback_thread = None
            for stream in (instream, outstream):
                try:
                    if stream and stream is instream:
                        stream.stop_stream()
                except Exception:
                    pass
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass
            try:
                if p:
                    p.terminate()
            except Exception:
                pass
            with self._lock:
                if not self._last_error:
                    self._status.running = False
                    self._status.message = "Stopped"
            _leave_windows_pro_audio_thread(mmcss)

    def stop(self):
        self._stop_event.set()
        self._ai_stop_event.set()
        self._meter_stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.8)
        ai_thread = self._ai_thread
        if ai_thread and ai_thread.is_alive() and ai_thread is not threading.current_thread():
            ai_thread.join(timeout=0.25)
        playback_thread = self._playback_thread
        if playback_thread and playback_thread.is_alive() and playback_thread is not threading.current_thread():
            playback_thread.join(timeout=0.6)
        self._thread = None
        self._ai_thread = None
        self._playback_thread = None
        self._ai_in_ring = None
        self._ai_out_ring = None
        self._dj_stereo_ring = None
        self._dj_render_ring = None
        self._ai_sample_rate = 0
        with self._lock:
            self._status.running = False
            if not self._last_error:
                self._status.message = "Stopped"

    def status(self) -> dict[str, Any]:
        with self._lock:
            s = self._status
            return {
                "running": bool(s.running),
                "message": s.message,
                "input_label": s.input_label,
                "output_label": s.output_label,
                "source_kind": str(s.source_kind or "device"),
                "source_pid": int(s.source_pid or 0),
                "capture_backend": str(s.capture_backend or ""),
                "source_suppressed": bool(s.source_suppressed),
                "source_attenuation_db": round(float(s.source_attenuation_db or 0.0), 1),
                "source_volume_compensation": str(s.source_volume_compensation or "none"),
                "sample_rate": s.sample_rate,
                "channels": s.channels,
                "buffer_frames": s.buffer_frames,
                "buffer_ms": round(s.buffer_ms, 2),
                "io_latency_ms": round(s.io_latency_ms, 2),
                "half_rate": bool(s.half_rate),
                "input_db": round(s.input_db, 1),
                "output_db": round(s.output_db, 1),
                "limiter_reduction_db": round(s.limiter_reduction_db, 2),
                "master_comp_reduction_db": round(s.master_comp_reduction_db, 2),
                "auto_gain_reduction_db": round(s.auto_gain_reduction_db, 2),
                "fidelity_residual_gain": round(s.fidelity_residual_gain, 4),
                "fidelity_quality_score": round(s.fidelity_quality_score, 4),
                "declick_repairs": int(s.declick_repairs),
                "output_buffer_frames": int(s.output_buffer_frames),
                "output_buffer_ms": round(s.output_buffer_ms, 2),
                "agentic_lookahead_sec": round(float(s.continuity_lookahead_ms or 0.0)/1000.0, 3),
                "capture_frame_index": int(self._capture_frame_index),
                "render_source_frame_index": int(self._render_source_frame_index),
                "xruns": s.xruns,
                "bridge_fill_ms": round(s.bridge_fill_ms, 2),
                "bridge_target_ms": round(s.bridge_target_ms, 1),
                "dsp_max_ms": round(s.dsp_max_ms, 1),
                "capture_gap_max_ms": round(s.capture_gap_max_ms, 1),
                "drift_correction_ppm": round(s.drift_correction_ppm, 1),
                "concealments": int(s.concealments),
                "dsp_backend": str(s.dsp_backend or ""),
                "spectrum": list(s.spectrum),
                "ai_enabled": bool(s.ai_enabled),
                "ai_ready": bool(s.ai_ready),
                "ai_state": s.ai_state,
                "ai_model": s.ai_model,
                "ai_device": s.ai_device,
                "ai_detail_model": str(s.ai_detail_model or ""),
                "ai_detail_confidence": round(s.ai_detail_confidence, 3),
                "ai_inference_ms": round(s.ai_inference_ms, 1),
                "ai_last_update": s.ai_last_update or None,
                "ai_scales": dict(s.ai_scales),
                "ai_delta": dict(s.ai_delta),
                "features": {k: round(float(v), 5) for k, v in (s.features or self._features).items()},
                "dsp_params": {k: round(float(v), 5) for k, v in (s.dsp_params or self._params).items()},
                "effect_intensity": round(float(self._effect_intensity_target), 3),
                "effect_intensity_current": round(float(self._effect_intensity), 3),
                "neural_polish_enabled": bool(self._neural_polish_enabled),
                "neural_polish_wet": round(float(self._neural_polish_wet), 3),
                "neural_polish_ready": bool(s.neural_polish_ready),
                "neural_polish_rtf": round(float(s.neural_polish_rtf), 3),
                "neural_polish_inference_ms": round(float(s.neural_polish_inference_ms), 1),
                "neural_polish_delay_ms": round(float(s.neural_polish_delay_ms), 2),
                "neural_polish_hold_ms": round(float(s.neural_polish_hold_ms), 1),
                "neural_polish_mix": round(float(s.neural_polish_mix), 3),
                "neural_polish_deadline_misses": int(s.neural_polish_deadline_misses),
                "neural_polish_overload_bypasses": int(s.neural_polish_overload_bypasses),
                "neural_polish_message": str(s.neural_polish_message or ""),
                "dj_mode": bool(s.dj_mode or self._dj_mode),
                "dj_prompt": s.dj_prompt or self._dj_prompt,
                "dj_score": round(float(s.dj_score or self._dj_score), 6),
                "dj_cycle": int(s.dj_cycle or self._dj_cycle),
                "dj_update_hz": float(s.dj_update_hz or 0.0),
                "dj_window_ms": float(s.dj_window_ms or self._dj_last_window_ms),
                "dj_result_age_ms": float(s.dj_result_age_ms or self._dj_last_result_age_ms),
                "dj_feature_gradients": dict(s.dj_feature_gradients or self._dj_feature_gradients),
                "dj_node_gradients": dict(s.dj_node_gradients or self._dj_node_gradients),
                "dj_node_values": dict(s.dj_node_values or {k: self._params.get(k, DEFAULT_PARAMS.get(k, 0.0)) for k in DJ_NODE_SPECS}),
                "dsp50_mode": bool(s.dsp50_mode or self._dsp50_mode),
                "dsp50_prompt": s.dsp50_prompt or self._dsp50_prompt,
                "dsp50_intensity": round(float(s.dsp50_intensity or self._dsp50_intensity), 3),
                "dsp50_score": round(float(s.dsp50_score or self._dsp50_score), 6),
                "dsp50_cycle": int(s.dsp50_cycle or self._dsp50_cycle),
                "dsp50_gradients": dict(s.dsp50_gradients or self._dsp50_gradients),
                "dsp50_confidence": dict(s.dsp50_confidence or self._dsp50_confidence),
                "dsp50_node_values": dict(s.dsp50_node_values or {k: self._params.get(k, DEFAULT_PARAMS[k]) for k in DSP50_SPECS}),
                "dsp50_active_nodes": list(s.dsp50_active_nodes or self._dsp50_active_nodes),
                "dsp50_optimizer": str(s.dsp50_optimizer or "Projected AdaBelief"),
                "dsp50_update_hz": float(s.dsp50_update_hz or 0.0),
                "dsp50_window_ms": float(s.dsp50_window_ms or self._dsp50_last_window_ms),
                "dsp50_result_age_ms": float(s.dsp50_result_age_ms or self._dsp50_last_result_age_ms),
                "dsp50_render_count": int(s.dsp50_render_count or self._dsp50_last_render_count),
                "dsp50_specs": {k: {"label": v["label"], "group": v["group"], "unit": v["unit"], "min": PARAM_BOUNDS[k][0], "max": PARAM_BOUNDS[k][1], "default": DEFAULT_PARAMS[k]} for k, v in DSP50_SPECS.items()},
                "agentic_live_enabled": bool(self._agentic_live_enabled),
                "agentic_controls": dict(self._agentic_controls),
                "agentic_mixer": dict(self._agentic_mixer_status),
                "capture_frame_index": int(self._capture_frame_index),
                "render_source_frame_index": int(self._render_source_frame_index),
                "error": self._last_error or None,
            }
