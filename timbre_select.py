"""JoyMetric v31.23.2 - TimbreSelector: the rack's INSTRUMENT must fit BOTH the record and the DJ prompt.

Once per record / prompt (in a background thread, throttled so the CPU critic is never starved), every
instrument candidate of the pad role (analog pads, strings, choir, organ, electric piano) and of the
transient role (synth plucks, electric piano, bells, marimba, guitar, harp, brass, organ) is rendered
for 2 s on the record's current chords and sent to the CLAP worker together with the DJ prompt.  One
call returns the candidate's similarity to the prompt text and its audio embedding; the record's own
2 s block is embedded the same way.  Candidates are ranked by 0.6 * z(prompt similarity) +
0.4 * z(cosine to the record).  The best instrument plays; at high creativity the top three rotate
between windows (variety that still fits).  Nothing here touches the audio thread; a worker that is
busy or offline leaves the defaults.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.request

import numpy as np

from synth_rack import PAD_VARIANTS, PLUCK_VARIANTS, render_block
from synth_weave import style_words

DEFAULT_RANK = {"pad": ["warm", "strings", "wide"], "pluck": ["soft", "epiano", "tight"]}
# v31.25.1: what an instrument may even be considered for, per genre of the DJ prompt (the user heard
# "train-station chimes" on a hip-hop record: bells on a dark urban track are wrong before any score)
GENRE_ALLOWED = {
    "hip-hop": {"pad": ("warm", "wide", "hollow", "strings", "epiano", "choir"), "pluck": ("soft", "tight", "epiano", "guitar", "organ")},
    "r&b": {"pad": ("warm", "wide", "strings", "epiano", "choir"), "pluck": ("soft", "epiano", "guitar", "organ")},
    "house": {"pad": ("warm", "wide", "bright", "hollow", "strings", "organ"), "pluck": ("soft", "tight", "bright", "epiano", "organ")},
    "techno": {"pad": ("warm", "wide", "hollow", "glass"), "pluck": ("soft", "tight", "bright", "organ")},
    "trance": {"pad": ("wide", "bright", "glass", "strings"), "pluck": ("tight", "bright", "soft")},
    "ambient": {"pad": ("warm", "wide", "glass", "strings", "choir", "epiano"), "pluck": ("soft", "harp", "bells", "marimba", "epiano")},
    "pop": {"pad": ("warm", "wide", "bright", "strings", "epiano"), "pluck": ("soft", "epiano", "guitar", "marimba")},
    "drum and bass": {"pad": ("warm", "wide", "hollow", "strings"), "pluck": ("soft", "tight", "epiano")},
    "dubstep": {"pad": ("wide", "hollow", "warm"), "pluck": ("tight", "soft", "brass")},
    "disco": {"pad": ("warm", "strings", "epiano"), "pluck": ("epiano", "guitar", "brass", "soft")},
    "jazz": {"pad": ("epiano", "strings", "organ"), "pluck": ("epiano", "guitar", "organ")},
    "rock": {"pad": ("organ", "strings", "warm"), "pluck": ("guitar", "organ", "epiano")},
    "reggaeton": {"pad": ("warm", "wide", "strings"), "pluck": ("soft", "epiano", "guitar", "marimba")},
    "afrobeats": {"pad": ("warm", "epiano", "strings"), "pluck": ("guitar", "marimba", "epiano", "soft")},
}
DARK_EXCLUDE = ("bells", "glass", "marimba", "brass", "harp", "guitar", "organ")
# v31.25.1: the added melodies are SYNTHS - never bells, guitar, brass, mallets, organ ("stay away from those")
SYNTH_ONLY = {"pad": ("warm", "wide", "bright", "hollow", "strings", "choir", "epiano"), "pluck": ("soft", "tight", "bright", "epiano")}


class TimbreSelector:
    def __init__(self, sr: int = 48000, url: str | None = None, throttle_s: float = 1.5):
        host = os.environ.get("JOY_SEMANTIC_HOST", "127.0.0.1"); port = int(os.environ.get("JOY_SEMANTIC_PORT", "8767") or 8767)
        self.url = url or ("http://%s:%d/agent-state" % (host, port))
        self.sr = int(sr); self.throttle_s = float(throttle_s)
        self.rank = {k: list(v) for k, v in DEFAULT_RANK.items()}
        self.best = {k: v[0] for k, v in DEFAULT_RANK.items()}
        self.scores = {}; self.state = "idle"; self.last_key = None; self.last_t = 0.0; self.busy = False; self.last_ms = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ gating / choice
    def needed(self, key, now: float) -> bool:
        with self._lock:
            return (not self.busy) and key != self.last_key and (now - self.last_t) > 20.0

    def pick(self, kind: str, window: int, creativity: float) -> str:
        """the instrument for a window: the best one, or (creativity >= 0.8) the top two in a 2:1 rotation."""
        with self._lock:
            rank = list(self.rank.get(kind) or DEFAULT_RANK.get(kind) or ["warm"])
        if creativity < 0.8 or len(rank) < 2:
            return rank[0]
        slots = (0, 1, 0, 0, 1, 0)                     # top two only: variety without surprises
        return rank[min(len(rank) - 1, slots[int(window) % len(slots)])]

    def status(self):
        with self._lock:
            return {"state": self.state, "best": dict(self.best), "rank": {k: list(v[:3]) for k, v in self.rank.items()},
                    "scores": {k: {v: round(float(x), 3) for v, x in d.items()} for k, d in self.scores.items()}, "ms": int(self.last_ms)}

    # ------------------------------------------------------------------ CLAP
    def _embed(self, audio, prompt: str):
        """(prompt similarity, normalised CLAP audio embedding) for a mono float32 clip at self.sr."""
        x = np.asarray(audio, dtype=np.float32)
        if x.ndim == 2:
            x = x.mean(axis=1)
        body = {"audio_b64": base64.b64encode(x.tobytes()).decode("ascii"), "sample_rate": int(self.sr), "prompt": str(prompt or "")}
        data = json.dumps(body).encode("utf-8")
        last = None
        for attempt in range(6):
            try:
                req = urllib.request.Request(self.url, data=data, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=40) as r:
                    out = json.loads(r.read().decode("utf-8"))
                emb = np.asarray(out.get("embedding") or [], dtype=np.float64)
                if emb.size == 0:
                    raise RuntimeError("no embedding")
                emb /= (np.linalg.norm(emb) + 1e-9)
                sim = out.get("prompt_similarity")
                return (float(sim) if sim is not None else 0.0), emb
            except Exception as exc:                        # 503 busy / warming: back off, never spin
                last = exc; time.sleep(2.0 + attempt)
        raise RuntimeError("CLAP unavailable: %s" % str(last)[:80])

    # ------------------------------------------------------------------ selection
    def start(self, key, prompt: str, ref_audio, chords, bpm: float, style: str, key_str: str, profile=None) -> bool:
        with self._lock:
            if self.busy:
                return False
            self.busy = True; self.state = "selecting"; self.last_key = key; self.last_t = time.time()
        args = (prompt, np.array(ref_audio, dtype=np.float32, copy=True), list(chords), float(bpm), str(style), str(key_str), dict(profile or {}))
        threading.Thread(target=self._run, args=args, daemon=True, name="joymetric-timbre-select").start()
        return True

    @staticmethod
    def candidates_for(kind, style, profile=None):
        genre, moods = style_words(style or "")
        table = PAD_VARIANTS if kind == "pad" else PLUCK_VARIANTS
        allowed = GENRE_ALLOWED.get(genre, {}).get(kind)
        names = [v for v in table if (allowed is None or v in allowed) and v in SYNTH_ONLY.get(kind, ())]
        dark = ("dark" in moods or "deep" in moods or "underground" in moods or float(((profile or {}).get("axes") or {}).get("brightness", 0.0)) < -0.2)
        if dark:
            names = [v for v in names if v not in DARK_EXCLUDE] or names
        return names

    def _run(self, prompt, ref_audio, chords, bpm, style, key_str, profile):
        t0 = time.time()
        try:
            import ctypes; ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -1)
        except Exception:
            pass
        try:
            _sim_ref, emb_ref = self._embed(ref_audio, prompt)
            time.sleep(self.throttle_s)
            n = int(2.0 * self.sr)
            chords = [dict(c, a=max(0, min(n, int(c["a"]))), b=max(0, min(n, int(c["b"])))) for c in chords]
            chords = [c for c in chords if c["b"] > c["a"]] or [{"a": 0, "b": n, "root": 9, "quality": "min", "label": "Am"}]
            rank = {}; best = {}; scores = {}
            for kind, variants in (("pad", self.candidates_for("pad", style, profile)), ("pluck", self.candidates_for("pluck", style, profile))):
                sims = {}; cos = {}
                for v in variants:
                    spec = {"kind": kind, "sr": self.sr, "bpm": bpm, "n": n, "b0": 0.0, "chords": chords, "style": style, "seed": 11,
                            "level_rms": 0.08, "energy": 0.6, "drums": 1.0, "variant": v, "profile": profile, "tail_s": 0.0}
                    y, _info = render_block(spec)
                    s, emb = self._embed(y[:n], prompt)
                    sims[v] = s; cos[v] = float(emb @ emb_ref)
                    time.sleep(self.throttle_s)
                score = self.combine(sims, cos)
                order = sorted(variants, key=lambda v: score[v], reverse=True)
                rank[kind] = order; best[kind] = order[0]
                scores[kind] = {v: score[v] for v in variants}
                scores[kind + "_prompt"] = dict(sims); scores[kind + "_song"] = dict(cos)
            with self._lock:
                self.rank.update(rank); self.best.update(best); self.scores = scores; self.state = "selected"
        except Exception as exc:
            with self._lock:
                self.state = "failed: %s" % str(exc)[:60]
        finally:
            with self._lock:
                self.busy = False; self.last_ms = int((time.time() - t0) * 1000)

    @staticmethod
    def combine(sims: dict, cos: dict, w_prompt: float = 0.6, w_song: float = 0.4) -> dict:
        """z-scored inside the candidate set, so neither scale dominates."""
        keys = list(sims)
        p = np.array([float(sims[k]) for k in keys]); s = np.array([float(cos[k]) for k in keys])

        def z(v):
            sd = float(v.std())
            return (v - v.mean()) / sd if sd > 1e-9 else np.zeros_like(v)

        tot = w_prompt * z(p) + w_song * z(s)
        return {k: float(tot[i]) for i, k in enumerate(keys)}
