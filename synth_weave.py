"""SynthWeave (JoyMetric v31.21) - Stable Audio synth / melody layer that fits the record.

The captured Deck-B loop (the record's own bars, already beat-aligned) is sent to
the local Stable Audio worker as INIT AUDIO together with a prompt for a melodic
synth part (arpeggio / pad / lead / plucks).  Audio-to-audio generation from the
record's bars inherits tempo, key, chord movement and phrase length; the
negative prompt and a post high-pass push drums out (the AI drummer owns them);
the result comes back sample-aligned with the loop, so Deck B can crossfade
from the record loop to the synth loop on the grid.  Fully asynchronous: the
audio thread never waits for the GPU.
"""
from __future__ import annotations
import json, os, tempfile, threading, time, urllib.request
import numpy as np

KINDS = {
    "arp": "analog synthesizer arpeggio, sawtooth sequence through a resonant low-pass filter, sidechain pumping",
    "pad": "wide analog supersaw synth pad, slow filter sweep, lush chorus",
    "lead": "monophonic analog synth lead with portamento and delay",
    "pluck": "detuned square-wave synth plucks, short envelope, sidechained",
}

# v31.22.3: the composer is told what the SONG is and asked for a part that fits it.  The DJ prompt is an
# instruction sheet for the DJ ("keep the 808 clean, phrase-aware changes") - pasting it into an audio caption
# produced unrelated material; only its genre / mood words are kept.
GENRES = [
    ("hip-hop", ("hip-hop", "hiphop", "hip hop", "trap", "808", "boom bap", "rap", "drill")),
    ("house", ("deep house", "tech house", "house", "garage")),
    ("techno", ("techno", "industrial", "minimal techno")),
    ("trance", ("trance", "psytrance", "progressive")),
    ("drum and bass", ("drum and bass", "dnb", "jungle", "breakbeat")),
    ("dubstep", ("dubstep", "bass music", "riddim")),
    ("ambient", ("ambient", "downtempo", "chill", "lo-fi", "lofi", "chillout")),
    ("r&b", ("r&b", "rnb", "soul", "neo-soul")),
    ("reggaeton", ("reggaeton", "dembow", "latin")),
    ("afrobeats", ("afrobeat", "afro house", "amapiano")),
    ("disco", ("disco", "nu-disco", "funk")),
    ("pop", ("pop", "synthpop", "synth-pop")),
    ("rock", ("rock", "metal", "punk", "indie")),
    ("jazz", ("jazz", "swing")),
]
MOODS = ("dreamy", "hypnotic", "dark", "bright", "late-night", "euphoric", "melancholic", "aggressive", "warm",
         "cold", "minimal", "epic", "groovy", "floating", "moody", "uplifting", "deep", "cinematic", "dirty",
         "smooth", "energetic", "chill", "nostalgic", "spacey", "modern", "vintage", "underground")
STATE_WORDS = {
    "buildup": "during a rising buildup", "drop": "in a full-energy drop section", "breakdown": "in a sparse breakdown",
    "low_energy": "in a calm section", "high_energy": "in an intense section", "bass_heavy": "over heavy sub bass",
    "vocal_focus": "under a foreground vocal", "instrumental": "in an instrumental passage",
    "stable_groove": "", "phrase_transition": "at a phrase transition", "drum_dense": "over dense drums",
}


def style_words(style: str):
    """genre + mood adjectives found in a DJ prompt / policy style (never the instruction text itself)."""
    txt = " " + (style or "").lower().replace("_", " ") + " "
    genre = ""
    for name, keys in GENRES:
        if any((" " + k + " ") in txt or (" " + k + ",") in txt or (" " + k + ".") in txt or (" " + k) in txt for k in keys):
            genre = name; break
    moods = [m for m in MOODS if m in txt][:3]
    return genre or "electronic", moods


def key_words(key: str) -> str:
    k = (key or "").strip()
    if not k:
        return "a minor key"
    root = k.rstrip("m"); minor = k.endswith("m")
    return "%s %s" % (root, "minor" if minor else "major")


def song_words(song) -> str:
    """what the composer must fit: energy, vocal, drums, and the CLAP musical state of the bars it is written for."""
    song = dict(song or {})
    parts = []
    e = song.get("energy")
    if e is not None:
        e = float(e); parts.append("high energy, driving" if e > 0.72 else ("mellow, low energy" if e < 0.35 else "mid energy"))
    if float(song.get("vocal") or 0.0) > 0.55:
        parts.append("sitting under the lead vocal")
    if float(song.get("drums") or 0.0) > 0.5:
        parts.append("riding the existing drum groove")
    st = str(song.get("state") or "")
    if STATE_WORDS.get(st) and st != "vocal_focus":
        parts.append(STATE_WORDS[st])
    return ", ".join(parts)


def build_prompt(kind: str, key: str, bpm: float, style: str, song=None):
    kind_txt = KINDS.get(kind, KINDS["arp"])
    genre, moods = style_words(style)
    head = genre + " track" + ((", " + " ".join(moods)) if moods else "")
    fit = song_words(song)
    prompt = ("%s: an extra synthesizer layer composed for this exact song, following its chord progression and melody, "
              "in %s at %d BPM%s, locked to its groove and complementing the existing arrangement; %s; synthesizer only, clean mix"
              % (head, key_words(key), int(round(bpm)), (", " + fit) if fit else "", kind_txt))
    negative = ("piano, acoustic piano, electric piano, rhodes, keys, guitar, strings, orchestra, brass, drums, percussion, "
                "kick drum, snare, hi-hat, cymbals, vocals, singing, noise, distortion, silence, different key, different tempo")
    return prompt, negative


# ---------------------------------------------------------------------------
# Harmonic agreement: tonal (peak-based) chroma and a pitch-class sieve.
# ---------------------------------------------------------------------------
def tonal_chroma(x, sr: int, fmin: float = 80.0, fmax: float = 5000.0):
    """12-bin pitch-class profile from spectral PEAKS after whitening (energy in
    broadband noise / drums barely counts).  Returns (chroma sum=1, tonalness 0..1)."""
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 2:
        x = x.mean(axis=1)
    win = 4096; hop = 1024
    n = 1 + (len(x) - win) // hop
    if n < 2:
        return np.ones(12) / 12.0, 0.0
    idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
    F = np.abs(np.fft.rfft(x[idx] * np.hanning(win).astype(np.float32), axis=1)).astype(np.float32)
    f = np.fft.rfftfreq(win, 1.0 / sr)
    band = (f >= fmin) & (f <= fmax)
    from scipy.ndimage import uniform_filter1d
    env = uniform_filter1d(F, 48, axis=1) + 1e-6
    W = F / env
    peaks = (W[:, 1:-1] > W[:, :-2]) & (W[:, 1:-1] >= W[:, 2:]) & (W[:, 1:-1] > 2.2)
    P = np.zeros_like(F, dtype=bool); P[:, 1:-1] = peaks
    P &= band[None, :]
    pc = np.zeros(len(f), dtype=int); ok = f > 0
    pc[ok] = (np.round(12.0 * np.log2(f[ok] / 440.0)) + 69).astype(int) % 12
    w = np.log1p(F) * P
    chroma = np.zeros(12)
    for k in range(12):
        chroma[k] = float(w[:, pc == k].sum())
    tot = float(chroma.sum())
    tonal = float((F * P).sum() / (F[:, band].sum() + 1e-9))
    return (chroma / tot if tot > 1e-9 else np.ones(12) / 12.0), tonal


def chroma_agreement(a, b) -> float:
    a = np.asarray(a, dtype=np.float64); b = np.asarray(b, dtype=np.float64)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))


def allowed_pitch_classes(chroma, rel: float = 0.33, min_classes: int = 5):
    c = np.asarray(chroma, dtype=np.float64)
    allowed = c >= rel * c.max()
    if allowed.sum() < min_classes:
        allowed = np.zeros(12, bool); allowed[np.argsort(c)[::-1][:min_classes]] = True
    return allowed


def harmonic_sieve(y, sr: int, allowed, depth_db: float = 14.0, fmin: float = 90.0, fmax: float = 6000.0):
    """Attenuate spectral bins whose pitch class the record does not use (STFT mask)."""
    from scipy.signal import stft, istft
    y = np.asarray(y, dtype=np.float32)
    mono = y.ndim == 1
    Y = y[:, None] if mono else y
    win = 2048; hop = 512
    out = np.zeros_like(Y)
    f = np.fft.rfftfreq(win, 1.0 / sr)
    pc = np.zeros(len(f), dtype=int); okf = f > 0
    pc[okf] = (np.round(12.0 * np.log2(f[okf] / 440.0)) + 69).astype(int) % 12
    gain = np.ones(len(f), dtype=np.float32)
    band = (f >= fmin) & (f <= fmax)
    g_off = float(10.0 ** (-depth_db / 20.0))
    gain[band & ~np.asarray(allowed)[pc]] = g_off
    for ch in range(Y.shape[1]):
        _, _, Z = stft(Y[:, ch], fs=sr, nperseg=win, noverlap=win - hop, boundary="zeros", padded=True)
        Z = Z * gain[:, None]
        _, z = istft(Z, fs=sr, nperseg=win, noverlap=win - hop, input_onesided=True, boundary=True)
        out[:, ch] = z[:Y.shape[0]] if len(z) >= Y.shape[0] else np.pad(z, (0, Y.shape[0] - len(z)))
    return (out[:, 0] if mono else out).astype(np.float32)


class SynthWeave:
    def __init__(self, url: str = "http://127.0.0.1:8766", steps: int = 8, noise: float = 0.50, cfg_scale: float = 7.5, timeout: float = 110.0):
        self.url = url.rstrip("/"); self.steps = int(steps); self.noise = float(noise); self.cfg_scale = float(cfg_scale); self.timeout = float(timeout)
        self.busy = False; self.enabled = False; self.last_error = ""; self.count = 0; self.last_ms = 0.0
        self._lock = threading.Lock()
        self.enabled = self.health()

    def health(self) -> bool:
        try:
            h = json.loads(urllib.request.urlopen(self.url + "/health", timeout=2.0).read().decode("utf-8"))
            # a worker that is busy with another job is still a worker: the request queues behind it
            self.enabled = bool(h.get("ok")) and str(h.get("state", "")) in ("ready", "busy")
        except Exception as exc:
            self.enabled = False; self.last_error = str(exc)[:80]
        self._last_health = time.time()
        return self.enabled

    def request(self, loop: np.ndarray, sr: int, bpm: float, key: str, style: str, seq: int, kind: str, on_done, song=None) -> bool:
        if not self.enabled and time.time() - getattr(self, "_last_health", 0.0) > 20.0:
            self.health()                                   # the worker may have come up (or finished a job) since
        with self._lock:
            if self.busy or not self.enabled:
                return False
            self.busy = True
        threading.Thread(target=self._run, args=(np.array(loop, dtype=np.float32, copy=True), int(sr), float(bpm), str(key), str(style), int(seq), str(kind), on_done, dict(song or {})),
                         daemon=True, name="joymetric-synth-weave").start()
        return True

    # ------------------------------------------------------------------
    def _run(self, loop, sr, bpm, key, style, seq, kind, on_done, song=None):
        t0 = time.time(); meta = {"seq": seq, "kind": kind, "key": key, "bpm": bpm}
        try:
            import soundfile as sf
            if loop.ndim == 1:
                loop = np.stack([loop, loop], axis=1)
            n = int(loop.shape[0])
            # the diffusion model wants a couple of seconds at least: tile short loops (phrase-exact repeats)
            reps = 1
            while n * reps < int(2.2 * sr):
                reps += 1
            clip = np.tile(loop, (reps, 1)).astype(np.float32)
            # The worker assumes the file's rate is the model's native 44.1 kHz and does not
            # resample: a 48 kHz file came back 8.8 % fast and ~1.5 semitones sharp - the
            # reason generated parts sounded unrelated to the record.  Convert both ways.
            MSR = 44100
            if sr != MSR:
                from scipy.signal import resample_poly as _rp
                from math import gcd as _gcd
                g = _gcd(int(MSR), int(sr)); clip = _rp(clip, MSR // g, sr // g, axis=0).astype(np.float32)
            tmp = tempfile.mkdtemp(prefix="joysynth_")
            pin = os.path.join(tmp, "loop_in.wav"); pout = os.path.join(tmp, "loop_out.wav")
            sf.write(pin, clip, MSR, subtype="FLOAT")
            prompt, negative = build_prompt(kind, key, bpm, style, song)
            meta["prompt"] = prompt
            body = json.dumps({"steps": self.steps, "seed": 1000 + seq, "tasks": [{"name": "synth", "input": pin, "output": pout, "prompt": prompt, "negative_prompt": negative, "noise": self.noise, "cfg_scale": self.cfg_scale}]}).encode("utf-8")
            req = urllib.request.Request(self.url + "/run", data=body, headers={"Content-Type": "application/json"}, method="POST")
            done = False
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                for line in r:
                    try:
                        ev = json.loads(line.decode("utf-8"))
                    except Exception:
                        continue
                    if ev.get("type") == "done":
                        done = bool(ev.get("ok")); break
                    if ev.get("type") == "error":
                        meta["error"] = str(ev.get("error"))[:120]; break
                    if time.time() - t0 > self.timeout:
                        break
            if not done:
                raise RuntimeError(meta.get("error") or "generation did not finish")
            y, rsr = sf.read(pout, dtype="float32", always_2d=True)
            if y.shape[1] == 1:
                y = np.repeat(y, 2, axis=1)
            y = y[:, :2]
            rsr = MSR if rsr == MSR else rsr
            if rsr != sr:
                from scipy.signal import resample_poly
                from math import gcd
                g = gcd(int(sr), int(rsr)); y = resample_poly(y, sr // g, rsr // g, axis=0).astype(np.float32)
            buf, hinfo = self.post_process_info(y, loop, sr)
            self.last_ms = (time.time() - t0) * 1000.0; self.count += 1
            meta.update({"ms": round(self.last_ms), "ok": True, "rms": round(float(np.sqrt(np.mean(buf ** 2))), 4)})
            meta.update(hinfo)
            try:
                on_done(seq, buf, meta)
            except Exception:
                pass
        except Exception as exc:
            self.last_error = str(exc)[:120]; meta.update({"ok": False, "error": self.last_error, "ms": round((time.time() - t0) * 1000.0)})
            try:
                on_done(seq, None, meta)
            except Exception:
                pass
        finally:
            with self._lock:
                self.busy = False

    @staticmethod
    def post_process(y: np.ndarray, loop: np.ndarray, sr: int) -> np.ndarray:
        return SynthWeave.post_process_info(y, loop, sr)[0]

    @staticmethod
    def post_process_info(y: np.ndarray, loop: np.ndarray, sr: int, sieve: bool = True):
        """Crop to the loop length (the generation is sample-aligned with its init audio),
        push the low end and any drum residue aside, force the part into the pitch
        classes of the record (harmonic sieve), match the level, soft-limit.  Returns
        (audio, info) with the harmonic agreement before / after the sieve."""
        from scipy.signal import butter, sosfilt
        n = int(loop.shape[0])
        info = {}
        if y.shape[0] < n:
            y = np.pad(y, ((0, n - y.shape[0]), (0, 0)))
        y = y[:n].astype(np.float32)
        y = sosfilt(butter(4, 140.0, btype="high", fs=sr, output="sos"), y, axis=0).astype(np.float32)
        y = sosfilt(butter(2, min(14000.0, 0.45 * sr), btype="low", fs=sr, output="sos"), y, axis=0).astype(np.float32)
        try:
            c_src, tonal_src = tonal_chroma(loop, sr); c_out, _ = tonal_chroma(y, sr)
            info["tonal_src"] = round(tonal_src, 3); info["harm_before"] = round(chroma_agreement(c_src, c_out), 3)
            allowed = allowed_pitch_classes(c_src)
            info["inkey_before"] = round(float(np.asarray(c_out)[allowed].sum()), 3)
            if sieve and tonal_src > 0.08:
                y = harmonic_sieve(y, sr, allowed)
                c_out2, _ = tonal_chroma(y, sr); info["harm_after"] = round(chroma_agreement(c_src, c_out2), 3)
                info["inkey"] = round(float(np.asarray(c_out2)[allowed].sum()), 3)
            else:
                info["harm_after"] = info["harm_before"]; info["inkey"] = info["inkey_before"]
        except Exception as exc:
            info["harm_error"] = str(exc)[:60]
        ref = float(np.sqrt(np.mean(loop ** 2)) + 1e-9); cur = float(np.sqrt(np.mean(y ** 2)) + 1e-9)
        g = min(4.0, 0.85 * ref / cur)
        y = np.tanh(1.15 * y * g) / 1.15
        # seamless loop: 6 ms crossfade of the end into the start
        xf = min(int(0.006 * sr), n // 8)
        if xf > 8:
            w = np.linspace(0.0, 1.0, xf, dtype=np.float32)[:, None]
            head = y[:xf].copy(); tail = y[-xf:].copy()
            y[:xf] = head * w + tail * (1.0 - w)
        return np.ascontiguousarray(y, dtype=np.float32), info
