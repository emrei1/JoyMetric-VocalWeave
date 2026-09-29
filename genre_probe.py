"""JoyMetric v31.26 - GenreProbe: zero-shot genre of the record with the CLAP critic (once per record).

The record's last 2 s block is scored against one text prompt per genre ("hip hop music", ...); the
softmax over the similarities is the genre distribution.  It conditions MelodyNet (tagtraum genre
index), the instrument palette and the density / register of the generated part, together with the
DJ prompt's own genre words (the prompt wins when it names a genre explicitly).
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.request

import numpy as np

# tagtraum CD2 classes (MelodyNet's genre conditioning) and their CLAP prompts
GENRES = ["Pop_Rock", "Electronic", "Rap", "Jazz", "Latin", "RnB", "International", "Country", "Reggae", "Blues", "Vocal", "Folk", "New Age"]
PROMPTS = {"Pop_Rock": "pop rock music", "Electronic": "electronic dance music, house, techno", "Rap": "hip hop rap music with 808 bass", "Jazz": "jazz music",
           "Latin": "latin music, reggaeton", "RnB": "r&b soul music", "International": "world music", "Country": "country music", "Reggae": "reggae dub music",
           "Blues": "blues music", "Vocal": "vocal ballad", "Folk": "acoustic folk music", "New Age": "ambient new age music"}
PROMPT_GENRE = {"hip-hop": "Rap", "r&b": "RnB", "house": "Electronic", "techno": "Electronic", "trance": "Electronic", "drum and bass": "Electronic", "dubstep": "Electronic",
                "ambient": "New Age", "pop": "Pop_Rock", "rock": "Pop_Rock", "jazz": "Jazz", "reggaeton": "Latin", "afrobeats": "International", "disco": "Electronic", "electronic": "Electronic"}


class GenreProbe:
    def __init__(self, sr: int = 48000, url: str | None = None, throttle_s: float = 1.0):
        host = os.environ.get("JOY_SEMANTIC_HOST", "127.0.0.1"); port = int(os.environ.get("JOY_SEMANTIC_PORT", "8767") or 8767)
        self.url = url or ("http://%s:%d/agent-state" % (host, port)); self.sr = int(sr); self.throttle_s = float(throttle_s)
        self.genre = "Pop_Rock"; self.genre_id = len(GENRES); self.probs = {}; self.state = "idle"; self.busy = False; self.last_key = None; self.last_t = 0.0
        self._lock = threading.Lock()

    def needed(self, key, now: float) -> bool:
        with self._lock:
            return (not self.busy) and key != self.last_key and (now - self.last_t) > 20.0

    def status(self):
        with self._lock:
            return {"genre": self.genre, "genre_id": int(self.genre_id), "probs": {k: round(float(v), 3) for k, v in self.probs.items()}, "state": self.state}

    def _sim(self, audio, prompt):
        x = np.asarray(audio, dtype=np.float32)
        if x.ndim == 2:
            x = x.mean(axis=1)
        body = {"audio_b64": base64.b64encode(x.tobytes()).decode("ascii"), "sample_rate": int(self.sr), "prompt": str(prompt)}
        data = json.dumps(body).encode("utf-8")
        for attempt in range(5):
            try:
                req = urllib.request.Request(self.url, data=data, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=40) as r:
                    out = json.loads(r.read().decode("utf-8"))
                return float(out.get("prompt_similarity") or 0.0)
            except Exception:
                time.sleep(2.0 + attempt)
        raise RuntimeError("CLAP unavailable")

    def start(self, key, audio, prompt_genre: str | None = None) -> bool:
        with self._lock:
            if self.busy:
                return False
            self.busy = True; self.state = "probing"; self.last_key = key; self.last_t = time.time()
        threading.Thread(target=self._run, args=(np.array(audio, dtype=np.float32, copy=True), prompt_genre), daemon=True, name="joymetric-genre-probe").start()
        return True

    def _run(self, audio, prompt_genre):
        try:
            import ctypes; ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -1)
        except Exception:
            pass
        try:
            sims = {}
            for g in GENRES:
                sims[g] = self._sim(audio, PROMPTS[g]); time.sleep(self.throttle_s)
            v = np.array([sims[g] for g in GENRES]); z = (v - v.mean()) / (v.std() + 1e-6); p = np.exp(2.5 * z); p /= p.sum()
            probs = {g: float(p[i]) for i, g in enumerate(GENRES)}
            best = GENRES[int(np.argmax(p))]
            if prompt_genre in GENRES:
                best = prompt_genre                             # the DJ prompt names the genre: it wins
            with self._lock:
                self.probs = probs; self.genre = best; self.genre_id = GENRES.index(best); self.state = "probed"
        except Exception as exc:
            with self._lock:
                self.state = "failed: %s" % str(exc)[:50]
        finally:
            with self._lock:
                self.busy = False
