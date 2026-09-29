"""JoyMetric v31.30.23 - VocalWeave: every 16 s of the record, continuously and in real time (the Home tab's flow, live):
  the VOCALS are protected - separated and heard exactly as they are, never given to Stable Audio;
  the INSTRUMENTAL (mix - vocals) is the Stable Audio input, transformed with the agentic prompt, and replaces the
  record's instrumental sample-exactly (cancel deck), with 0.5 s cross-fades between consecutive windows.
  (v31.28.0/1 history below.)
  1. the 16 s window is separated (Mel-Band RoFormer worker, :8770) into the VOCAL / melody stem and the rest;
  2. when the window carries a vocal, the vocal stem is handed to Stable Audio (the Home tab's model,
     :8766) as the INPUT of an audio-to-audio edit driven by the agentic DJ prompt: the melody's contour and
     timing survive (moderate init noise), the timbre becomes what the prompt asks for (a synth lead);
  3. the result is level-matched to the vocal it replaces, harmonically sieved to the record's key, edge-faded;
  4. the original vocal is SUBTRACTED from the programme sample-exactly for that window (engine cancel deck;
     every DJ layer / FX stays) and the transformed melody is scheduled at the very same frames.
The window is captured 16 s before it plays; separation (~3 s) + Stable Audio (~3-6 s) land well before.
A window without a vocal, a late result or a failed job leaves the record untouched.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
import urllib.request

import numpy as np
from scipy.signal import butter, resample_poly, sosfilt

from synth_weave import allowed_pitch_classes, harmonic_sieve, style_words, tonal_chroma

MSR = 44100
# exactly the Home tab's Stable Audio usage (processor.py): the user's text + this suffix, this negative prompt,
# cfg_scale 1.0, steps by speed mode (turbo 4 / balanced 6 / quality 8); init noise is the Home slider
HOME_SUFFIX = ". Instrumental only. Preserve original melody, rhythm, tempo, harmony and musical structure."
HOME_NEGATIVE = "vocals, singing, rapping, speech, spoken voice, vocal chops"
HOME_CFG = 1.0
DEFAULT_CFG = 4.0      # v31.30.11: prompt guidance for the woven instrumental (Home's 1.0 = the prompt has no effect at all)
HOME_STEPS = {"turbo": 4, "balanced": 6, "quality": 8}
PAD_S = 1.5            # each window is rendered with 1.5 s of context on both sides; consecutive windows cross-fade over it (raised cosine)
# v31.30.15 low-latency profile (JOY_WEAVE_PROFILE=lowlat): 4 s windows, 1 s pads, 3 s context (known prefix 4 s), one hop per
# window.  Measured on the record (GPU shared with a live session): 1.3 s of work per 4 s window (max 1.35 s), effect 51 vs 52
# for the 16 s profile, harmony 0.85; the render lag can then be 10 s (LAUNCH_LOWLAT.ps1).  2 s windows: 1.5 s of work per
# 2 s window (max 2.3 s = not real time) and level jumps at the seams up to 16 dB beyond the record's own -> rejected.
PROFILE = os.environ.get("JOY_WEAVE_PROFILE", "").strip().lower()
if PROFILE == "lowlat":
    PAD_S = 1.0
# v31.30.16 ultra-low profile (JOY_WEAVE_PROFILE=ultralow): 2 s windows, 0.5 s pads, 1.5 s context (known prefix 2 s) - a 4 s render lag
# (LAUNCH_ULTRALOW.ps1).  Work per window must stay under ~1.5 s: fine at FX (cfg) 1.0, tight at 4.0.  Seams are the price.
if PROFILE == "ultralow":
    PAD_S = 0.5
CONTEXT_S = 6.5        # v31.29.3: with the 1.5 s pad the KNOWN prefix is 8 s of our own previous output (measured: renders then differ
                       # between windows no more than the song's own sections do: 25.9 vs 26.1)
HOP_S = 24.0           # v31.30.23: a 24 s window is ONE generation (no internal hop seam) - the largest single generation that fits the 48 s lag; the 8 s known prefix (inpaint) continues each window from the previous
if PROFILE == "lowlat":
    CONTEXT_S = 3.0; HOP_S = 4.0
if PROFILE == "ultralow":
    CONTEXT_S = 1.5; HOP_S = 2.0
# v31.30.17 mid profile (JOY_WEAVE_PROFILE=mid): 8 s windows (one 8 s hop after 8 s of known output - the default profile's hop
# geometry, so the seams are the same), 1.5 s pads, 14 s render lag (LAUNCH_MID.ps1); work 3.4 s per window, budget 4.5 s.
if PROFILE == "mid":
    CONTEXT_S = 6.5; HOP_S = 8.0
SESSION_SEED = 4242    # v31.29 continuity: one seed for every window (a new seed per window gave a new melody per window)


def melody_carrier(voc, sr: int, hop: int = 256, fmin: float = 80.0, fmax: float = 1100.0):
    """The vocal's MELODY as a synth: f0 contour (pYIN on the separated vocal, 22.05 kHz), its loudness
    envelope, rendered as a band-limited saw with a tracking low-pass (2-pole) and a quiet sub-octave.
    Returns (carrier (n, 2) float32, f0 per hop (Hz, 0 = unvoiced), voiced fraction)."""
    import librosa
    x = np.asarray(voc, dtype=np.float32)
    mono = x.mean(axis=1) if x.ndim == 2 else x
    n = mono.shape[0]
    asr = 22050
    xa = resample_poly(mono, asr, sr).astype(np.float32) if sr != asr else mono
    # YIN (fast) + a loudness gate for voicing + median smoothing against octave errors (pYIN is 8 s on 16 s)
    from scipy.ndimage import median_filter
    f0 = np.asarray(librosa.yin(xa, fmin=fmin, fmax=fmax, sr=asr, frame_length=1024, hop_length=hop, trough_threshold=0.15), np.float64)
    rmsf = librosa.feature.rms(y=xa, frame_length=1024, hop_length=hop, center=True)[0]
    n_f = min(len(f0), len(rmsf)); f0 = f0[:n_f]; rmsf = np.asarray(rmsf[:n_f], np.float64)
    vflag = rmsf > max(1e-4, 0.12 * float(np.percentile(rmsf, 95)))
    lf = np.log2(np.maximum(f0, 1.0)); ls = median_filter(lf, size=7, mode="nearest")
    vflag &= np.abs(lf - ls) < 0.3
    f0 = 2.0 ** ls
    frames = np.arange(len(f0)) * hop / asr                                 # frame centres (s)
    env = np.sqrt(np.maximum(1e-12, np.convolve(mono.astype(np.float64) ** 2, np.ones(1024) / 1024.0, mode="same")))
    t = np.arange(n) / float(sr)
    fi = np.interp(t, frames, np.where(vflag, f0, 0.0))                       # 0 where unvoiced
    vo = np.interp(t, frames, vflag.astype(np.float64))
    # smooth the voicing gate (12 ms) so notes start and stop without clicks; hold f0 through short gaps
    k = max(1, int(0.012 * sr)); gate = np.convolve(vo, np.ones(k) / k, mode="same")
    f_held = fi.copy(); last = 0.0
    idx = np.where(f_held <= 0.0)[0]
    if len(idx):
        # forward-fill unvoiced samples with the previous f0 (vectorised)
        m = f_held > 0.0; pos = np.where(m, np.arange(n), 0); np.maximum.accumulate(pos, out=pos); f_held = f_held[pos]
    f_held = np.where(f_held > 0.0, f_held, 220.0)
    phase = np.cumsum(f_held / float(sr)); ph = phase - np.floor(phase)
    saw = 2.0 * ph - 1.0
    sub_ph = 0.5 * phase; sub = 2.0 * (sub_ph - np.floor(sub_ph)) - 1.0
    y = saw + 0.25 * sub
    sos = butter(2, min(0.45 * sr, 3800.0) / (sr / 2.0), btype="low", output="sos"); y = sosfilt(sos, y)
    y = y * gate * env * 1.6
    out = np.stack([y, y], axis=1).astype(np.float32)
    return out, np.where(vflag, f0, 0.0), float(vflag.mean()) if len(vflag) else 0.0


# ---- v31.30.39 harmony guard -----------------------------------------------------------------------------------------
# Measured 2026-09-19 (All Of The Lights, 24 s windows, production pipeline): at STABLE FX (cfg) 2.0 the generated
# instrumental keeps chroma ~0.92 / onset-pattern correlation ~0.79 with the record for matching AND mismatching
# prompts; at cfg 4.0 it drops to ~0.83 / ~0.57 (harmony and rhythm fall apart - heard as "bozukluk") and the prompt
# adherence (CLAP) drops as well; 6 steps also degrade it (0.86 / 0.70); temperature 0.40 raises it to 0.96 / 0.88.
# The guard is outcome-based: it does not judge the prompt, it measures what came back.
GUARD_MIN_S = float(os.environ.get("JOY_WEAVE_GUARD_MIN_S", "16") or 16)          # only for long windows (lag budget)
GUARD_CHROMA = float(os.environ.get("JOY_WEAVE_GUARD_CHROMA", "0.86") or 0.86)
GUARD_ONSET = float(os.environ.get("JOY_WEAVE_GUARD_ONSET", "0.60") or 0.60)
GUARD_NOISE_DROP = float(os.environ.get("JOY_WEAVE_GUARD_NOISE_DROP", "0.12") or 0.12)
GUARD_CFG_MULT = float(os.environ.get("JOY_WEAVE_GUARD_CFG_MULT", "0.6") or 0.6)
# v31.30.39 level-normalised model input: the record's instrumental is boosted to this RMS before Stable Audio and the
# output is divided by the same factor.  0 = off.  Measured at the live capture level (RMS 0.12) the same TEMP kept
# chroma 0.76 / onset 0.63 of the record; at RMS 0.47 it kept 0.92 / 0.79 (img2img anchors in latent magnitude).
INPUT_RMS = float(os.environ.get("JOY_WEAVE_INPUT_RMS", "0.35") or 0.0)
INPUT_GAIN_MAX = float(os.environ.get("JOY_WEAVE_INPUT_GAIN_MAX", "8") or 8.0)


def _guard_metrics(gen, ref, sr: int):
    """chroma cosine (mean over frames) and onset-strength correlation (+-120 ms) of the generated hop vs the record's
    hop, both mono @ 22.05 kHz; None when librosa is unavailable or anything fails (the guard then stays out of the way)."""
    try:
        import librosa
        g = librosa.resample(np.mean(np.asarray(gen, np.float32), axis=1), orig_sr=int(sr), target_sr=22050)
        r = librosa.resample(np.mean(np.asarray(ref, np.float32), axis=1), orig_sr=int(sr), target_sr=22050)
        n = min(len(g), len(r)); g = g[:n]; r = r[:n]
        if n < 22050 or float(np.sqrt(np.mean(r ** 2))) < 1e-4 or float(np.sqrt(np.mean(g ** 2))) < 1e-4:
            return None
        cg = librosa.feature.chroma_stft(y=g, sr=22050, n_fft=4096, hop_length=1024)
        cr = librosa.feature.chroma_stft(y=r, sr=22050, n_fft=4096, hop_length=1024)
        cg = cg / (np.linalg.norm(cg, axis=0, keepdims=True) + 1e-9); cr = cr / (np.linalg.norm(cr, axis=0, keepdims=True) + 1e-9)
        m = min(cg.shape[1], cr.shape[1]); chroma = float(np.mean(np.sum(cg[:, :m] * cr[:, :m], axis=0)))
        og = librosa.onset.onset_strength(y=g, sr=22050, hop_length=512); orr = librosa.onset.onset_strength(y=r, sr=22050, hop_length=512)
        k = min(len(og), len(orr)); og = og[:k] - og[:k].mean(); orr = orr[:k] - orr[:k].mean()
        best = -1.0
        for lag in range(-5, 6):
            a = og[max(0, lag):k + min(0, lag)]; b = orr[max(0, -lag):k - max(0, lag)]
            if len(a) > 10:
                best = max(best, float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9)))
        return {"chroma": round(chroma, 3), "onset": round(best, 3)}
    except Exception:
        return None


class VocalWeave:
    def __init__(self, sr: int = 48000, sep_url: str = "http://127.0.0.1:8770", sa_url: str = "http://127.0.0.1:8766", window_s: float = None):
        if window_s is None:
            window_s = {"lowlat": 4.0, "ultralow": 2.0, "mid": 8.0}.get(PROFILE, 24.0)          # v31.30.23: 24 s single-generation windows (merge the 8 s hops)
        self.sr = int(sr); self.sep_url = sep_url.rstrip("/"); self.sa_url = sa_url.rstrip("/"); self.W = int(round(window_s * sr)); self.pad = int(round(PAD_S * sr))
        self.ctx = int(round(CONTEXT_S * sr)); self.seed = int(SESSION_SEED); self.prev = None          # prev = (w, transformed output BEFORE the envelope, start frame)
        self.hop = int(round(HOP_S * sr))
        # v31.30 live temperature: a UI slider sets the noise; windows already rendered but not yet playing are re-rendered
        self.noise_override = None; self.pending = {}; self.rerender = False; self.gen = 0; self.rerenders = 0
        self.cfg_override = None
        self.guard_retries = 0; self.guard_used = 0          # v31.30.39 harmony guard counters
        self.hops = {}                      # v31.30.1: (w, j) -> hop record: start, n, x (instrumental slice), y (levelled hop output), noise, seq, ...
        self.errors = []                    # v31.30.7: (time, window, error) of the last failures - visible in the status / API
        self.blend = int(0.1 * sr)
        self.busy = False; self.done = {}; self.count = 0; self.skipped = 0; self.late = 0; self.failed = 0; self.last = {}; self.enabled = False
        self.last_health = 0.0; self._lock = threading.Lock(); self.error = ""; self._sa_mode = ""
        self._health_thread = None
        self.health_async()

    # ------------------------------------------------------------------ availability
    def health(self) -> bool:
        self.last_health = time.time()
        ok_sep = ok_sa = False
        try:
            with urllib.request.urlopen(self.sep_url + "/health", timeout=3) as r:
                ok_sep = json.loads(r.read().decode("utf-8")).get("state") == "ready"
        except Exception as exc:
            self.error = "sep: " + str(exc)[:60]
        try:
            with urllib.request.urlopen(self.sa_url + "/health", timeout=3) as r:
                h = json.loads(r.read().decode("utf-8")); ok_sa = h.get("state") in ("ready", "busy")
        except Exception as exc:
            self.error = "sa3: " + str(exc)[:60]
        self.enabled = bool(ok_sep and ok_sa)
        return self.enabled

    def health_async(self):
        """the health probes (up to 2 x 3 s when a worker is down) never block the agent's control loop."""
        t = self._health_thread
        if t is not None and t.is_alive():
            return
        self.last_health = time.time()
        self._health_thread = threading.Thread(target=self.health, daemon=True, name="joymetric-weave-health"); self._health_thread.start()

    def window_of(self, frame: int) -> int:
        return int(frame // self.W)

    def taken(self, frame: int) -> bool:
        """the window of this frame has been requested (processing or scheduled): the weave owns its melody."""
        d = self.done.get(self.window_of(frame))
        return bool(d and d.get("state") in ("processing", "scheduled"))

    def state_of(self, frame: int):
        d = self.done.get(self.window_of(frame))
        return d.get("state") if d else None

    def mark(self, w: int, state: str):
        with self._lock:
            if w not in self.done:
                self.done[w] = {"state": state, "w": int(w)}

    def is_active(self, frame: int) -> bool:
        d = self.done.get(self.window_of(frame))
        return bool(d and d.get("state") == "scheduled")

    # ------------------------------------------------------------------ the pipeline
    def request(self, w: int, audio, prompt: str, style: str, key: str, creativity: float, engine, render_index: int, profile=None, pad_in: int = 0, pad_out: int = 0) -> bool:
        if not self.enabled and time.time() - self.last_health > 5.0:        # v31.30.7: recover within 5 s, not 20 (fewer skipped windows)
            self.health_async()
        with self._lock:
            if self.busy or not self.enabled or w in self.done:
                return False
            self.busy = True; self.done[w] = {"state": "processing", "t": time.time()}
        threading.Thread(target=self._run, args=(int(w), np.array(audio, dtype=np.float32, copy=True), str(prompt), str(style), str(key), float(creativity), engine, int(render_index), dict(profile or {}), int(pad_in), int(pad_out)),
                         daemon=True, name="joymetric-vocal-weave").start()
        return True

    def _separate(self, audio):
        """raw float32 in / raw float32 vocals out (v31.30.2): nothing in this process parses megabytes of JSON any more."""
        x = np.ascontiguousarray(audio, dtype=np.float32)
        req = urllib.request.Request(self.sep_url + "/separate_raw", data=x.tobytes(), headers={"Content-Type": "application/octet-stream", "X-Sample-Rate": str(int(self.sr))})
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read(); h = r.headers
        if r.status != 200:
            raise RuntimeError("separation failed")
        voc = np.frombuffer(body, dtype=np.float32).reshape(-1, 2).copy()      # writable
        acts = [int(a) for a in str(h.get("X-Activity") or "").split(",") if a.strip() in ("0", "1")]
        return voc, None, float(h.get("X-Vocal-Ratio") or 0.0), acts, int(h.get("X-Ms") or 0)

    @staticmethod
    def prompt_for(prompt: str, style: str, key: str, profile=None):
        """the Home tab's prompt: the agentic DJ prompt itself + Home's preservation suffix; Home's negative prompt."""
        text = " ".join(str(prompt or style or "").split()).strip().rstrip(".") or "modern electronic remix"
        return text + HOME_SUFFIX, HOME_NEGATIVE

    def _transform(self, voc48, prompt, negative, noise, cfg, steps, keep_s: float = 0.0):
        """the vocal stem -> Stable Audio audio-to-audio (44.1 kHz in / out) -> 48 kHz."""
        x = resample_poly(voc48, 147, 160, axis=0).astype(np.float32)
        import soundfile as sf
        d = tempfile.mkdtemp(prefix="joy_weave_")
        pin = os.path.join(d, "in.wav"); pout = os.path.join(d, "out.wav")
        sf.write(pin, x, MSR)
        task = {"name": "weave", "input": pin, "output": pout, "prompt": prompt, "negative_prompt": negative, "noise": float(noise), "cfg_scale": float(cfg)}
        if keep_s > 0.0:
            task["inpaint_keep_seconds"] = float(keep_s)          # the previous window's own output leads the input: continue it
        dp = os.environ.get("JOY_WEAVE_PADDING", "1")            # v31.30.1: 1 s of model padding instead of 6 s - half the time per call, same continuity
        if dp:
            task["duration_padding_sec"] = float(dp)
        # v31.30.23: a 24 s generation peaks higher in VRAM than the old 8 s hops; force the low-VRAM chunked DECODE so sa3
        # stays under its per-process cap (no OOM -> no empty_cache/retry storm that starved the audio bridge).  Audio-transparent.
        body = {"steps": int(steps), "seed": int(self.seed), "tasks": [task],
                "prefer_unchunked_decode": (os.environ.get("JOY_WEAVE_UNCHUNKED_DECODE", "0") == "1")}
        req = urllib.request.Request(self.sa_url + "/run", data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"})
        err = None
        try:
            r = urllib.request.urlopen(req, timeout=120)
        except Exception:
            time.sleep(1.0); r = urllib.request.urlopen(req, timeout=120)      # v31.30.7: retry once (a worker mid-restart used to fail the whole window)
        with r:
            for line in r:
                try:
                    ev = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                if ev.get("type") == "error":
                    err = ev.get("error")
                elif ev.get("type") == "progress" and "complete" in str(ev.get("message", "")):
                    self._sa_mode = str(ev.get("message"))[-24:]
        if err:
            raise RuntimeError(str(err)[:120])
        y, sr_out = sf.read(pout, dtype="float32", always_2d=True)
        try:
            os.remove(pin); os.remove(pout); os.rmdir(d)
        except Exception:
            pass
        if sr_out != self.sr:
            y = resample_poly(y, self.sr, sr_out, axis=0).astype(np.float32)
        n = voc48.shape[0]
        if y.shape[0] < n:
            y = np.concatenate([y, np.zeros((n - y.shape[0], y.shape[1]), np.float32)], axis=0)
        return y[:n]

    @staticmethod
    def _rms(a):
        return float(np.sqrt(np.mean(np.asarray(a, np.float64) ** 2) + 1e-12))

    def _post(self, y, voc, mix, level_mult, g_cont=None):
        """DC guard, level to the material it replaces (or, for a continuation, the level that keeps the seam flat,
        pulled 25 % toward the target so the level still tracks the record), short edge fades, soft ceiling."""
        sos = butter(1, 20.0 / (self.sr / 2.0), btype="high", output="sos"); y = sosfilt(sos, np.asarray(y, np.float64), axis=0)
        y = np.asarray(y, np.float64)
        target = self._rms(voc) * float(level_mult)
        g = min(12.0, target / max(1e-6, self._rms(y)))
        if g_cont is not None and g_cont > 0.0:
            g = float(g_cont) * (g / float(g_cont)) ** 0.25          # v31.29.3: seam-flat gain, slow pull toward the target
        y = y * g
        fade = min(int(0.01 * self.sr), y.shape[0] // 8); w = np.linspace(0.0, 1.0, fade)[:, None]
        y[:fade] *= w; y[-fade:] *= w[::-1]
        peak = float(np.abs(y).max())
        if peak > 0.9:
            y = np.tanh(y / 0.9) * 0.9
        return y.astype(np.float32), g

    def reset_session(self):
        """v31.30.12: a new engine session restarts the frame clock - the previous session's hops, pending windows and
        continuity context must not be re-rendered or scheduled into the new one (the slider settings stay)."""
        with self._lock:
            self.hops = {}; self.pending = {}; self.done = {}; self.prev = None; self.rerender = False; self.last_ren = 0; self.last = {}

    def noise_now(self) -> float:
        """the temperature in force right now: the slider (API), else weave.json, else env, else the default"""
        if self.noise_override is not None:
            return float(self.noise_override)
        ov = self.overrides()
        return float(ov.get("noise") or os.environ.get("JOY_WEAVE_NOISE", "0") or 0) or 0.52

    def cfg_now(self) -> float:
        """prompt guidance in force: the slider (API), else weave.json 'cfg', else env, else the default"""
        if self.cfg_override is not None:
            return float(self.cfg_override)
        ov = self.overrides()
        return max(2.0, float(ov.get("cfg") or os.environ.get("JOY_WEAVE_CFG", "0") or 0) or DEFAULT_CFG)   # v31.30.22 floor 2.0

    def set_cfg(self, value: float) -> float:
        """v31.30.11: the effect-strength slider (classifier-free guidance).  Persists to weave.json, applies to the next hop,
        re-renders the pending hops like a temperature change."""
        v = float(min(8.0, max(2.0, float(value))))          # v31.30.22 floor 2.0: below this the prompt barely guides -> monotone / garbled (user hit this; 2.1 was their fix)
        self.cfg_override = v; self.rerender = True
        try:
            fp = os.path.join(os.environ.get("LOCALAPPDATA", ""), "JoyMetric", "weave.json")
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            d = self.overrides(); d["cfg"] = v
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(d, f)
        except Exception:
            pass
        return v

    def set_noise(self, value: float) -> float:
        """v31.30: the slider.  Persists to weave.json, applies to the next hop, and re-renders the windows that are
        rendered but not yet playing so the change is heard as soon as the pipeline allows."""
        v = float(min(0.9, max(0.05, float(value))))
        self.noise_override = v; self.rerender = True
        try:
            fp = os.path.join(os.environ.get("LOCALAPPDATA", ""), "JoyMetric", "weave.json")
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            d = self.overrides(); d["noise"] = v
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(d, f)
        except Exception:
            pass
        return v

    def rerender_tick(self, engine, ren: int, need_s: float = 4.5) -> bool:
        """called by the agent when the pipeline is idle: re-render the earliest due hop in a worker thread."""
        if not self.rerender or self.busy or not self.enabled:
            return False
        key = self.rerender_due(ren, need_s)
        if key is None:
            self.rerender = self._anything_differs(); return False        # far hops come into the horizon later
        with self._lock:
            self.busy = True

        def _job():
            try:
                try:
                    import ctypes; ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -1)
                except Exception:
                    pass
                self._rerender_hop(key, engine)
            except Exception as exc:
                self.last = {"state": "rerender failed", "error": str(exc)[:120]}
            finally:
                with self._lock:
                    self.busy = False
        threading.Thread(target=_job, daemon=True, name="joymetric-weave-rerender").start()
        return True

    @staticmethod
    def overrides() -> dict:
        """live tuning file (read per window): %LOCALAPPDATA%/JoyMetric/weave.json"""
        try:
            fp = os.path.join(os.environ.get("LOCALAPPDATA", ""), "JoyMetric", "weave.json")
            if os.path.isfile(fp):
                with open(fp, "r", encoding="utf-8") as f:
                    d = json.load(f)
                return d if isinstance(d, dict) else {}
        except Exception:
            pass
        return {}

    @staticmethod
    def boundary_envelope(n: int, m_in: int, m_out: int):
        """linear ramps over the first m_in and the last m_out samples; a clip that ends where the next one's ramp ends
        sums with it to exactly 1 (the hop-boundary scheme, now used at window boundaries too)."""
        e = np.ones(n, np.float64)
        if m_in > 0:
            e[:m_in] = np.linspace(0.0, 1.0, m_in, endpoint=False)
        if m_out > 0:
            e[n - m_out:] = np.linspace(1.0, 0.0, m_out, endpoint=False)
        return e[:, None]

    @staticmethod
    def edge_envelope(n: int, pad_in: int, pad_out: int):
        """linear cross-fade ramps over the context pads (two consecutive windows sum to exactly 1 in the overlap)."""
        e = np.ones(n, np.float64)
        if pad_in > 0:
            e[:pad_in] = np.sin(0.5 * np.pi * np.linspace(0.0, 1.0, pad_in, endpoint=False)) ** 2
        if pad_out > 0:
            e[n - pad_out:] = np.cos(0.5 * np.pi * np.linspace(0.0, 1.0, pad_out, endpoint=False)) ** 2
        return e[:, None]

    # ------------------------------------------------------------------ v31.30.1 hop clips
    def _hop_clip(self, y_hop, head_fade, tail_fade, head_cos=False, tail_cos=False):
        """the hop's clip with its blends baked in: linear 100 ms blends at hop boundaries, raised-cosine window edges."""
        y = np.array(y_hop, dtype=np.float64, copy=True); n = y.shape[0]
        if head_fade > 0:
            h = min(head_fade, n); r = np.linspace(0.0, 1.0, h, endpoint=False)
            y[:h] *= (np.sin(0.5 * np.pi * r) ** 2 if head_cos else r)[:, None]
        if tail_fade > 0:
            t = min(tail_fade, n); r = np.linspace(1.0, 0.0, t, endpoint=False)
            y[n - t:] *= (np.cos(0.5 * np.pi * (1.0 - r)) ** 2 if tail_cos else r)[:, None]
        return y.astype(np.float32)

    def _level(self, y_raw, x_ref, known_tail, kept_head):
        """seam-flat gain: the reconstructed known head must equal the previous levelled output; 25 % pull to the RMS target."""
        target = self._rms(x_ref); g = min(12.0, target / max(1e-6, self._rms(y_raw)))
        if known_tail is not None and kept_head is not None:
            ref = self._rms(known_tail); got = self._rms(kept_head)
            if ref > 1e-5 and got > 1e-5:
                g_cont = float(np.clip(ref / got, 0.25, 4.0)); g = g_cont * (g / g_cont) ** 0.25
        return float(g)

    def _input_gain(self, x_hop) -> float:
        """v31.30.39: gain that brings the hop to INPUT_RMS (boost only, peak kept <= 0.98, at most INPUT_GAIN_MAX); 1.0 = off."""
        try:
            ref = float(self.overrides().get("input_rms", INPUT_RMS) or 0.0)
            if ref <= 0.0:
                return 1.0
            cur = float(self._rms(x_hop)); peak = float(np.abs(x_hop).max()) if x_hop.size else 0.0
            if cur < 1e-4 or peak <= 0.0:
                return 1.0
            k = min(ref / cur, INPUT_GAIN_MAX, 0.98 / peak)
            return float(max(1.0, k))
        except Exception:
            return 1.0

    def _hop_generate(self, x_hop, known, p_text, noise=None, cfg=None):
        """one continuation call: known (K frames of our own levelled output, or None) + the record's hop -> the hop's
        output (levelled, raw of envelope) and the reconstructed known tail (for the blend)."""
        ov = self.overrides(); steps = int(HOME_STEPS.get(str(ov.get("speed") or os.environ.get("JOY_WEAVE_SPEED", "turbo")), 4))
        noise = float(noise if noise is not None else self.noise_now())
        k_in = self._input_gain(x_hop)                       # v31.30.39: level-normalised model input
        if known is None:
            cfg = float(cfg) if cfg is not None else self.cfg_now()
            out = self._transform(x_hop * k_in, p_text, HOME_NEGATIVE, noise=noise, cfg=cfg, steps=steps) / k_in
            g = self._level(out, x_hop, None, None); return self._ceiling(out * g), None, noise, steps, g
        keep = known.shape[0]
        cfg = float(cfg) if cfg is not None else self.cfg_now()
        out = self._transform(np.concatenate([known, x_hop], axis=0) * k_in, p_text, HOME_NEGATIVE, noise=noise, cfg=cfg, steps=steps, keep_s=keep / float(self.sr)) / k_in
        gen = out[keep:keep + x_hop.shape[0]]; rec = out[:keep]
        m = min(self.blend, keep)
        g = self._level(gen, x_hop, known[-m:], rec[-m:])
        return self._ceiling(gen * g), self._ceiling(rec * g), noise, steps, g

    def _guard_enabled(self) -> bool:
        """weave.json {"guard": 0/1} else JOY_WEAVE_GUARD (default on)"""
        ov = self.overrides(); v = ov.get("guard")
        if v is None:
            v = os.environ.get("JOY_WEAVE_GUARD", "1")
        return str(v).strip().lower() not in ("0", "false", "off", "no")

    def guarded_hop_generate(self, x_hop, known, p_text, n_hop=None):
        """v31.30.39 harmony guard: generate the hop, measure how much of the record's harmony (chroma) and rhythm
        (onset pattern) survived; when either fell below the thresholds - the prompt fought the melody - render the hop
        ONCE more with less temperature and less prompt guidance and keep the rendition that follows the record better.
        Returns (y_hop, rec, noise, steps, g, guard_info | None)."""
        y_hop, rec, noise, steps, g = self._hop_generate(x_hop, known, p_text)
        n = int(n_hop or x_hop.shape[0])
        if not self._guard_enabled() or n < int(GUARD_MIN_S * self.sr):
            return y_hop, rec, noise, steps, g, None
        gm = _guard_metrics(y_hop[:n], x_hop[:n], self.sr)
        if gm is None:
            return y_hop, rec, noise, steps, g, None
        guard = dict(gm, retry=False, used="first")
        if gm["chroma"] < GUARD_CHROMA or gm["onset"] < GUARD_ONSET:
            n2 = max(0.30, float(noise) - GUARD_NOISE_DROP); c2 = max(1.0, float(self.cfg_now()) * GUARD_CFG_MULT)
            try:
                y2, rec2, noise2, steps2, g2 = self._hop_generate(x_hop, known, p_text, noise=n2, cfg=c2)
                gm2 = _guard_metrics(y2[:n], x_hop[:n], self.sr) or {"chroma": -1.0, "onset": -1.0}
                guard.update(retry=True, noise2=round(n2, 2), cfg2=round(c2, 2), chroma2=gm2["chroma"], onset2=gm2["onset"])
                self.guard_retries += 1
                if (gm2["chroma"] + gm2["onset"]) > (gm["chroma"] + gm["onset"]):
                    y_hop, rec, noise, steps, g = y2, rec2, noise2, steps2, g2
                    guard["used"] = "retry"; self.guard_used += 1
            except Exception as exc:
                guard["retry_error"] = str(exc)[:80]
        return y_hop, rec, noise, steps, g, guard

    @staticmethod
    def _ceiling(y, lim: float = 0.9):
        """soft ceiling: peaks above the limit are bent, not clipped (a hot Stable Audio peak on top of the vocal crackled)."""
        y = np.asarray(y, np.float32)
        peak = float(np.abs(y).max()) if y.size else 0.0
        if peak > lim:
            y = (np.tanh(y / lim) * lim).astype(np.float32)
        return y

    def _known_before(self, w, j):
        """K frames of our own levelled output just before hop (w, j): earlier hops of this window, else the previous window."""
        K = int(self.ctx) + int(self.pad)
        if j > 0:
            parts = []; need = K; jj = j - 1
            while need > 0 and jj >= 0 and (w, jj) in self.hops:
                y = self.hops[(w, jj)]["y"]; take = min(need, y.shape[0]); parts.insert(0, y[y.shape[0] - take:]); need -= take; jj -= 1
            if parts:
                return np.concatenate(parts, axis=0)
            return None
        prev = self.prev
        if prev is not None and prev[0] == w - 1:
            p_w, p_out, p_start = prev; start = w * self.W - int(self.pad)
            a0 = start - self.ctx - p_start; a1 = start + int(self.pad) - p_start
            if a0 >= 0 and a1 <= p_out.shape[0]:
                return np.ascontiguousarray(p_out[a0:a1])
        return None

    def _hop_bounds(self, n_all, pad_in, pad_out):
        """[(pos, end)] of the hops inside a window buffer of n_all frames (the last hop takes the trailing pad)."""
        H = int(self.hop); pos = int(pad_in); out = []
        while pos < n_all:
            end = min(n_all, pos + H) if pos + H + int(pad_out) < n_all else n_all
            out.append((pos, end)); pos = end
        return out

    def _schedule_hop(self, engine, w, j, y_hop, rec_head, start_abs, first, last, pad_in, pad_out, seq):
        """turn a hop's levelled output into its clip (blends baked in) and schedule it.  v31.30.9: every boundary -
        hop or window - is one m-sample (100 ms) blend from our own previous output; the last hop's clip ends at the
        window end (its trailing pad was model context only)."""
        m = int(self.blend)
        y_body = y_hop[: y_hop.shape[0] - int(pad_out)] if (last and pad_out > 0) else y_hop
        if first and rec_head is None:
            # a first window (no previous output): fade in from the record over the leading pad (raised cosine)
            y = y_body; clip_start = start_abs
            clip = self._hop_clip(y, (int(pad_in) if pad_in > 0 else 0), m, head_cos=True, tail_cos=False)
        else:
            head = rec_head[-m:] if rec_head is not None else np.zeros((m, y_hop.shape[1]), np.float32)
            y = np.concatenate([head, y_body], axis=0); clip_start = start_abs - m
            clip = self._hop_clip(y, m, m, head_cos=False, tail_cos=False)
        engine.set_synth_clip(clip, int(clip_start), 1.0, int(seq))
        return clip_start, clip.shape[0]

    HORIZON_S = 20.0        # v31.30.3: re-render only the hops that play within this horizon (the rest are re-rendered as they
                            # approach; a slider that keeps moving no longer floods the GPU with work that is thrown away)
    MIN_DELTA = 0.02        # a hop is re-rendered only when the slider differs from its rendered temperature by at least this

    def rerender_due(self, ren: int, need_s: float = 4.5, horizon_s: float = None):
        """the earliest pending hop whose rendered temperature differs from the slider (dead band), that starts far enough
        ahead to be replaced in time, and within the horizon."""
        target = self.noise_now(); tcfg = self.cfg_now(); best = None; self.last_ren = int(ren)
        hz = float(self.HORIZON_S if horizon_s is None else horizon_s)
        with self._lock:
            for key, hp in list(self.hops.items()):
                if hp["start"] + hp["n"] <= ren:
                    self.hops.pop(key, None); continue
                ahead = hp["start"] - ren
                if (abs(float(hp["noise"]) - target) >= self.MIN_DELTA or abs(float(hp.get("cfg", tcfg)) - tcfg) >= 0.1) and int(need_s * self.sr) < ahead <= int(hz * self.sr):
                    if best is None or hp["start"] < self.hops[best]["start"]:
                        best = key
        return best

    def playing_noise(self):
        """the temperature of the hop that is playing right now (None before the first woven hop)."""
        ren = int(getattr(self, "last_ren", 0) or 0)
        with self._lock:
            for key, hp in self.hops.items():
                if hp["start"] <= ren < hp["start"] + hp["n"]:
                    return float(hp["noise"])
        return None

    def queued(self) -> int:
        """hops still to be re-rendered for the current slider value (within the horizon)."""
        target = self.noise_now(); tcfg = self.cfg_now(); ren = int(getattr(self, "last_ren", 0) or 0); n = 0
        with self._lock:
            for hp in self.hops.values():
                if hp["start"] + hp["n"] > ren and (abs(float(hp["noise"]) - target) >= self.MIN_DELTA or abs(float(hp.get("cfg", tcfg)) - tcfg) >= 0.1) and hp["start"] - ren <= int(self.HORIZON_S * self.sr):
                    n += 1
        return n

    def _rerender_hop(self, key, engine):
        """re-render ONE hop at the slider's temperature and swap its clip (the vocal cancellation stays)."""
        hp = self.hops.get(key)
        if hp is None:
            return False
        w, j = key
        known = self._known_before(w, j)
        y_hop, rec, noise, steps, g = self._hop_generate(hp["x"], known, hp["prompt"])
        ren = int(getattr(engine, "render_source_frame_index", 0) or 0)
        try:
            ren = int(engine.status().get("render_source_frame_index") or ren)
        except Exception:
            pass
        if ren > hp["start"] - int(1.0 * self.sr):
            self.last = {"state": "rerender late", "w": w, "hop": j}; return False
        self.gen += 1; seq = 710000 + w + 1000 * j + 100000 * (self.gen % 80)
        engine.cancel_synth_clip(int(hp["seq"]))
        self._schedule_hop(engine, w, j, y_hop, rec, hp["start"], hp["first"], hp["last"], hp["pad_in"], hp["pad_out"], seq)
        with self._lock:
            hp.update({"y": y_hop, "noise": float(noise), "cfg": float(self.cfg_now()), "seq": seq, "gain": g})
            if self.prev is not None and self.prev[0] == w and hp["last"]:
                self.prev = (w, self._window_output(w), self.prev[2])
        self.rerenders += 1; self.last = {"state": "rerendered", "w": w, "hop": j, "noise": round(noise, 2), "steps": steps, "gain": round(g, 2)}
        return True

    def _window_output(self, w):
        """the window's levelled output (pads included) assembled from its hops - the next window's context."""
        keys = sorted(k for k in self.hops if k[0] == w)
        if not keys:
            return None
        first = self.hops[keys[0]]; n_all = int(first["n_all"]); y = np.zeros((n_all, 2), np.float32)
        for k in keys:
            hp = self.hops[k]; pos = int(hp["pos"]); y[pos:pos + hp["y"].shape[0]] = hp["y"]
            if hp["first"] and hp.get("rec") is not None and pos > 0:
                y[:pos] = hp["rec"][-pos:]
        return y

    def _anything_differs(self) -> bool:
        target = self.noise_now(); tcfg = self.cfg_now()
        with self._lock:
            return any(abs(float(hp["noise"]) - target) >= self.MIN_DELTA or abs(float(hp.get("cfg", tcfg)) - tcfg) >= 0.1 for hp in self.hops.values())

    def service_rerenders(self, engine, ren: int, budget: int = 1) -> int:
        """re-render due hops, earliest first (called by the agent when idle and between the hops of a window job)."""
        done = 0
        while done < budget:
            key = self.rerender_due(ren)
            if key is None:
                self.rerender = self._anything_differs(); break
            if self._rerender_hop(key, engine):
                done += 1
            else:
                break
        return done

    def _generate(self, w, inst, audio, p_text, pad_in, pad_out, prev):
        """the transformed instrumental for one window (pads included, no envelope): 8 s continuation hops after 8 s of our
        own output (prev = (w-1, output, start) or None), seam-flat level.  Returns (y, info)."""
        info = {}; negative = HOME_NEGATIVE; ov = self.overrides()
        noise = self.noise_now()                                 # the slider / weave.json / default
        steps = int(HOME_STEPS.get(str(ov.get("speed") or os.environ.get("JOY_WEAVE_SPEED", "turbo")), 4))          # Home turbo (user: 4-step diffusion)
        # continuity: the previous window's own transformed output (the CONTEXT_S just before this input) leads the
        # input, so the model hears what it played and continues that rendition instead of inventing a new one
        # continuity (v31.29.3): the window is generated in 8 s HOPS.  Every hop's input starts with 8 s of KNOWN audio -
        # our own output just before it (the previous window's tail for the first hop, the first hop for the second) -
        # marked by the inpaint mask; the model continues that rendition, the init audio keeps it on the record.
        start = w * self.W - int(pad_in)
        n_all = inst.shape[0]; H = int(self.hop); K = int(self.ctx) + int(pad_in)
        ctx = None
        if prev is not None and prev[0] == w - 1 and self.ctx > 0 and pad_in > 0:
            p_w, p_out, p_start = prev
            a0 = start - self.ctx - p_start; a1 = start + int(pad_in) - p_start
            if a0 >= 0 and a1 <= p_out.shape[0]:
                ctx = np.ascontiguousarray(p_out[a0:a1])
        y = np.zeros_like(inst); t1 = time.time(); hops = 0
        pos = int(pad_in)                                   # first frame of the window inside the buffer
        if ctx is not None:
            known = ctx; y0 = 0                              # the kept region [0, pad_in) comes back reconstructed
        else:
            known = None; y0 = 0
        while pos < n_all:
            end = min(n_all, pos + H) if pos + H + int(pad_out) < n_all else n_all       # the last hop takes the trailing pad
            if known is None:
                x_in = inst[y0:end]; keep = 0
            else:
                x_in = np.concatenate([known, inst[pos:end]], axis=0); keep = known.shape[0]
            noise = self.noise_now()                           # every hop reads the slider
            out = self._transform(x_in, p_text, negative, noise=noise, cfg=HOME_CFG, steps=steps, keep_s=(keep / float(self.sr)) if keep else 0.0)
            hops += 1
            if known is None:
                y[y0:end] = out[:end - y0]
            else:
                gen = out[keep:keep + (end - pos)]
                m = min(int(0.1 * self.sr), pos - y0, gen.shape[0])
                if hops > 1 and m > 0:                       # 100 ms blend at the hop boundary (codec round trip)
                    wgt = np.linspace(0.0, 1.0, m)[:, None]
                    y[pos - m:pos] = y[pos - m:pos] * (1.0 - wgt) + out[keep - m:keep] * wgt
                elif hops == 1:
                    y[:pos] = out[keep - pos:keep]           # the reconstructed kept cross-fade region
                y[pos:end] = gen
            # the next hop continues the last K frames of what we have
            known = np.ascontiguousarray(y[max(0, end - K):end]); pos = end; y0 = 0
        info["sa_ms"] = int((time.time() - t1) * 1000); info["hops"] = hops
        info["context"] = bool(ctx is not None)
        # level continuity: the kept cross-fade region of this render reproduces the previous window's (already
        # levelled) tail; the gain that makes them equal keeps the seam flat.  Per-window RMS matching alone
        # stepped the continuing texture by up to 6 dB at the seams (heard as "kopukluk").
        g_cont = None
        if ctx is not None and pad_in > 0:
            ref = self._rms(ctx[-int(pad_in):]); got = self._rms(y[:int(pad_in)])
            if ref > 1e-5 and got > 1e-5:
                g_cont = float(np.clip(ref / got, 0.25, 4.0))
        y, g = self._post(y, inst, audio, level_mult=1.0, g_cont=g_cont)
        info["g_cont"] = round(float(g_cont), 3) if g_cont else None
        info.update({"gain": round(float(g), 2), "noise": round(noise, 2), "steps": steps, "prompt": p_text[:90], "sa_mode": self._sa_mode, "pad": [int(pad_in), int(pad_out)]})
        return y, info

    def _run(self, w, audio, prompt, style, key, cr, engine, render_index, profile, pad_in=0, pad_out=0):
        t0 = time.time(); info = {"state": "processing", "w": w}
        try:
            try:
                import ctypes; ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -1)
            except Exception:
                pass
            if self._rms(audio) < 1e-3:
                info.update({"state": "silent", "ms": int((time.time() - t0) * 1000)}); self.skipped += 1
                return
            voc, _inst, ratio, acts, sep_ms = self._separate(audio)
            active = float(np.mean(acts)) if acts else 0.0
            inst = (audio - voc).astype(np.float32)                    # the vocals stay exactly as they are; only this goes on
            info.update({"vocal_ratio": round(ratio, 3), "vocal_active": round(active, 2), "sep_ms": sep_ms})
            p_text, negative = self.prompt_for(prompt, style, key, profile)
            start = w * self.W - int(pad_in); n_all = inst.shape[0]
            bounds = self._hop_bounds(n_all, int(pad_in), int(pad_out))
            ren = int(getattr(engine, "render_source_frame_index", 0) or 0)
            try:
                ren = int(engine.status().get("render_source_frame_index") or ren)
            except Exception:
                pass
            if ren > start:
                info.update({"state": "late", "ms": int((time.time() - t0) * 1000)}); self.late += 1
                self.errors = (self.errors + [(time.strftime("%H:%M:%S"), int(w), "late by %.1f s" % ((ren - start) / float(self.sr)))])[-8:]
                print("[weave] window %d late by %.1f s" % (int(w), (ren - start) / float(self.sr)), flush=True)
                return
            ov = self.overrides(); amount = float(ov.get("amount") or 0) or 1.0
            m = int(self.blend); joined = (self._known_before(w, 0) is not None)
            if joined and pad_in >= m:
                # continuation: the clip runs from start - m to the window END (its tail ramp meets the next window's head)
                c0 = int(pad_in) - m; c1 = n_all - int(pad_out)
                inst_env = (inst[c0:c1] * self.boundary_envelope(c1 - c0, m, m)).astype(np.float32); cancel_start = start + c0
            else:
                c1 = n_all - int(pad_out)
                e = np.ones(c1, np.float64); e[:int(pad_in)] = np.sin(0.5 * np.pi * np.linspace(0.0, 1.0, int(pad_in), endpoint=False)) ** 2
                if m > 0:
                    e[c1 - m:] = np.linspace(1.0, 0.0, m, endpoint=False)
                inst_env = (inst[:c1] * e[:, None]).astype(np.float32); cancel_start = start
            engine.set_cancel_clip(inst_env, cancel_start, amount, 700000 + w)    # the record's instrumental leaves, the vocal stays
            t1 = time.time(); hops = 0; used_ctx = False
            for j, (pos, end) in enumerate(bounds):
                known = self._known_before(w, j)
                if j == 0 and known is not None:
                    used_ctx = True
                if j == 0 and known is None:
                    pos = 0                                           # no context: the first hop renders the leading pad itself (the clip fades in over it)
                x_hop = np.ascontiguousarray(inst[pos:end])
                y_hop, rec, noise, steps, g, guard = self.guarded_hop_generate(x_hop, known, p_text, int(end - pos))   # v31.30.39
                hops += 1
                if guard is not None:
                    info["guard"] = guard
                first = (j == 0); last = (j == len(bounds) - 1)
                self.gen += 1; seq = 710000 + w + 1000 * j + 100000 * (self.gen % 80)
                self._schedule_hop(engine, w, j, y_hop, rec, start + pos, first, last, int(pad_in), int(pad_out), seq)
                with self._lock:
                    self.hops[(int(w), int(j))] = {"start": int(start + pos), "n": int(end - pos), "pos": int(pos), "n_all": int(n_all), "x": x_hop, "y": y_hop, "rec": rec,
                                                   "noise": float(noise), "cfg": float(self.cfg_now()), "seq": int(seq), "prompt": p_text, "first": first, "last": last, "pad_in": int(pad_in), "pad_out": int(pad_out), "gain": g, "guard": guard}
                    for k in [k for k in self.hops if k[0] < w - 2]:
                        self.hops.pop(k, None)
                info.update({"noise": round(noise, 2), "cfg": round(float(self.cfg_now()), 2), "steps": steps, "gain": round(g, 2), "hops": hops})
                if self.rerender and not last:                        # a slider move is serviced before the next hop
                    self.service_rerenders(engine, ren, budget=1)
            info["sa_ms"] = int((time.time() - t1) * 1000); info["context"] = used_ctx
            with self._lock:
                self.prev = (int(w), self._window_output(w), int(start)); self.pending = {k[0]: True for k in self.hops}
            y = self.prev[1] if self.prev[1] is not None else inst_env
            info.update({"state": "scheduled", "amount": amount, "ms": int((time.time() - t0) * 1000), "level": round(self._rms(y), 4)}); self.count += 1
        except Exception as exc:
            info.update({"state": "failed", "error": str(exc)[:120], "ms": int((time.time() - t0) * 1000)}); self.failed += 1
            self.errors = (self.errors + [(time.strftime("%H:%M:%S"), int(w), str(exc)[:160])])[-8:]
            print("[weave] window %d failed: %s" % (int(w), str(exc)[:160]), flush=True)
            if "refused" in str(exc).lower() or "urlopen" in str(exc).lower():
                self.enabled = False
        finally:
            with self._lock:
                self.done[w] = info; self.last = dict(info); self.busy = False
                for k in [k for k in self.done if k < w - 6]:
                    self.done.pop(k, None)
