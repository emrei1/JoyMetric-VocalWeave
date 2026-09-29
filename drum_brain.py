"""DrumMind runtime (JoyMetric v31.19) - the AI drummer.

Pattern source, per 2-bar phrase:
  * a conditional GRU groove model trained on the Groove MIDI Dataset (1150 human
    performances, 22k bars; Gillick et al. 2019 "tap2drum" formulation): given the
    record's own kick / snare / hat accent profiles, tempo, style, a density target
    and a fill flag it writes a complete performance - hit, velocity and
    microtiming for kick, snare, closed hat and open hat on 32 sixteenths;
  * 6000 real human grooves as a retrieval library (nearest by kick+snare accents);
  * every candidate (model samples + retrieved performances) is scored for MUSICAL
    FIT against the record - reinforce its kicks, never stomp on its backbeat, hat
    density complementary to the record's own hats, density target - and the best
    one is post-edited by the same rules.  numpy only: ~1 ms per phrase.
"""
from __future__ import annotations
import json, math, os
import numpy as np

STYLES = ["rock", "funk", "jazz", "latin", "hiphop", "soul", "afrocuban", "punk", "neworleans", "country", "pop", "reggae"]
_STYLE_WORDS = {
    "hiphop": ("hip-hop", "hiphop", "hip hop", "trap", "boom bap", "rap", "lofi", "lo-fi", "dreamy", "chill", "downtempo", "trip"),
    "funk": ("funk", "disco", "house", "club", "techno", "edm", "dance", "electro", "groove", "tech house", "deep house", "minimal"),
    "soul": ("soul", "r&b", "rnb", "neo soul", "ambient", "hypnotic", "late-night", "late night", "floating", "space"),
    "latin": ("latin", "reggaeton", "salsa", "bossa", "samba", "cumbia", "tropical"),
    "afrocuban": ("afro", "afrobeat", "afrocuban", "cuban", "percussive"),
    "jazz": ("jazz", "swing", "bebop", "broken beat"),
    "rock": ("rock", "indie", "alt", "grunge", "metal"),
    "punk": ("punk", "hardcore", "fast", "dnb", "drum and bass", "jungle", "breakbeat", "breaks"),
    "reggae": ("reggae", "dub", "dancehall", "ska"),
    "pop": ("pop", "synthpop", "electropop"),
    "neworleans": ("new orleans", "second line", "brass"),
    "country": ("country", "folk", "americana"),
}


def style_id_from_text(text: str) -> int:
    t = (text or "").lower()
    best, best_n = 1, 0
    for name, words in _STYLE_WORDS.items():
        n = sum(1 for w in words if w in t)
        if n > best_n:
            best_n = n; best = STYLES.index(name)
    return int(best)


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def _bar_avg(V: np.ndarray, inst) -> np.ndarray:
    """16-step profile averaged over the two bars for instrument index(es)."""
    v = V[:, inst] if isinstance(inst, int) else np.max(V[:, list(inst)], axis=1)
    p = v.reshape(2, 16).mean(axis=0)
    m = float(p.max())
    return p / m if m > 1e-6 else p


class DrumMind:
    def __init__(self, model_path: str, library_path: str | None = None):
        self.ok = False
        self.model_path = model_path
        self.lib = None
        try:
            w = np.load(model_path)
            self.w = {k: w[k] for k in w.files if k not in ("styles", "meta_json")}
            self.meta = json.loads(str(w["meta_json"])) if "meta_json" in w.files else {}
            self.hidden = int(self.meta.get("hidden", 160)); self.n_style = int(self.meta.get("n_style", len(STYLES) + 1))
            self.ok = True
        except Exception as exc:  # pragma: no cover
            self.error = str(exc)
            return
        if library_path and os.path.exists(library_path):
            try:
                L = np.load(library_path)
                self.lib = {"V": L["V"].astype(np.float32), "MT": L["MT"].astype(np.float32), "meta": L["meta"].astype(np.float32)}
                V = self.lib["V"]
                v2 = V.reshape(-1, 2, 16, 4)
                k = v2[:, :, :, 0].mean(axis=1); s = v2[:, :, :, 1].mean(axis=1)
                acc = np.concatenate([k, s], axis=1)
                self.lib["acc"] = acc / (np.linalg.norm(acc, axis=1, keepdims=True) + 1e-9)
            except Exception:
                self.lib = None

    # ---- model ---------------------------------------------------------
    def _cond(self, acc48, tempo_n, style_id, density, fill):
        st = np.zeros(self.n_style, np.float32); st[int(_clamp(style_id, 0, self.n_style - 1))] = 1.0
        x = np.concatenate([acc48.astype(np.float32), [tempo_n], st, [density], [fill]]).astype(np.float32)
        h = np.tanh(self.w["cond.0.weight"] @ x + self.w["cond.0.bias"])
        return np.tanh(self.w["cond.2.weight"] @ h + self.w["cond.2.bias"])

    def _decode(self, c, rng, temperature=0.9, sample=True):
        Wih, Whh, bih, bhh = self.w["gru.weight_ih_l0"], self.w["gru.weight_hh_l0"], self.w["gru.bias_ih_l0"], self.w["gru.bias_hh_l0"]
        Wo, bo = self.w["head.weight"], self.w["head.bias"]
        H = self.hidden
        h = np.zeros(H, np.float32); prev = np.zeros(8, np.float32)
        V = np.zeros((32, 4), np.float32); MT = np.zeros((32, 4), np.float32)
        for t in range(32):
            step = np.zeros(16, np.float32); step[t % 16] = 1.0
            x = np.concatenate([prev, step, [float(t // 16)], c]).astype(np.float32)
            gi = Wih @ x + bih; gh = Whh @ h + bhh
            r = _sigmoid(gi[:H] + gh[:H]); z = _sigmoid(gi[H:2 * H] + gh[H:2 * H])
            n = np.tanh(gi[2 * H:] + r * gh[2 * H:])
            h = (1.0 - z) * n + z * h
            o = Wo @ h + bo
            p = _sigmoid(o[0:4] / max(0.05, temperature))
            hit = (rng.random(4) < p) if sample else (p > 0.5)
            vel = _sigmoid(o[4:8]) * hit
            mt = 0.5 * np.tanh(o[8:12]) * hit
            V[t] = vel; MT[t] = mt
            prev = np.concatenate([vel, mt]).astype(np.float32)
        return V, MT

    # ---- retrieval -----------------------------------------------------
    def _retrieve(self, acc_k, acc_s, style_id, bpm, k=3):
        if self.lib is None:
            return []
        q = np.concatenate([acc_k, acc_s]); qn = np.linalg.norm(q)
        if qn < 1e-6:
            return []
        sim = self.lib["acc"] @ (q / qn)
        meta = self.lib["meta"]
        tempo_pen = np.abs(np.log((meta[:, 1] + 1e-6) / max(bpm, 1.0))) * 0.35
        style_bonus = (meta[:, 0] == style_id) * 0.06
        score = sim - tempo_pen + style_bonus
        idx = np.argsort(score)[::-1][:k]
        return [(self.lib["V"][i].copy(), self.lib["MT"][i].copy(), float(sim[i])) for i in idx]

    # ---- musical fit -----------------------------------------------------
    @staticmethod
    def fit_score(V, acc_k, acc_s, acc_h, density, song_hat_density):
        pk = _bar_avg(V, 0); ps = _bar_avg(V, 1); ph = _bar_avg(V, (2, 3))
        def cos(a, b):
            na, nb = np.linalg.norm(a), np.linalg.norm(b)
            return float(a @ b / (na * nb)) if na > 1e-6 and nb > 1e-6 else 0.5
        kick_fit = cos(pk, acc_k) if acc_k.max() > 0.2 else 0.5
        snare_fit = cos(ps, acc_s) if acc_s.max() > 0.2 else 0.5
        clash = float(np.mean(pk * ((acc_s > 0.6) & (acc_k < 0.15)))) * 4.0
        hits = float((V > 0).sum()) / 64.0
        density_err = abs(hits - density)
        hat_rate = float((V[:, 2:] > 0).any(axis=1).mean())
        hat_target = 0.85 - 0.55 * _clamp(song_hat_density, 0, 1)
        hat_fit = 1.0 - abs(hat_rate - hat_target)
        return 0.40 * kick_fit + 0.22 * snare_fit - 0.35 * clash - 0.55 * density_err + 0.18 * hat_fit

    @staticmethod
    def apply_rules(V, MT, acc_k, acc_s, song_hat_density):
        V = V.copy(); MT = MT.copy()
        strong_k = acc_k.max() > 0.3
        for t in range(32):
            s = t % 16
            if strong_k:
                V[t, 0] *= 0.35 + 0.65 * acc_k[s]            # reinforce the record's kicks, soften the rest
                if acc_s[s] > 0.6 and acc_k[s] < 0.15:
                    V[t, 0] = 0.0                              # never a kick on a snare-only backbeat
            if V[t, 1] > 0 and acc_s.max() > 0.2 and acc_s[s] < 0.35 and s not in (4, 12):
                V[t, 1] *= 0.25                                # snare only where the record has one (or the backbeat)
            if song_hat_density > 0.7 and s % 2 == 1:
                V[t, 2] *= 0.35; V[t, 3] *= 0.35               # busy record hats: no 16th chatter on top
        V[V < 0.06] = 0.0
        MT[V <= 0] = 0.0
        return V, MT

    def generate(self, acc_k, acc_s, acc_h, bpm, style_id=1, density=0.5, fill=0.0, n_candidates=6,
                 temperature=0.9, seed=None, song_hat_density=0.5, clarity=1.0):
        acc_k = np.clip(np.asarray(acc_k, np.float32).reshape(-1)[:16], 0, 1); acc_s = np.clip(np.asarray(acc_s, np.float32).reshape(-1)[:16], 0, 1)
        acc_h = np.clip(np.asarray(acc_h, np.float32).reshape(-1)[:16], 0, 1)
        if acc_k.size < 16: acc_k = np.pad(acc_k, (0, 16 - acc_k.size))
        if acc_s.size < 16: acc_s = np.pad(acc_s, (0, 16 - acc_s.size))
        if acc_h.size < 16: acc_h = np.pad(acc_h, (0, 16 - acc_h.size))
        rng = np.random.default_rng(seed)
        tempo_n = float(_clamp(bpm / 200.0, 0.2, 1.5))
        density = float(_clamp(density, 0.1, 1.1))
        cands = []
        if self.ok:
            c = self._cond(np.concatenate([acc_k, acc_s, acc_h]), tempo_n, style_id, density, float(fill))
            for i in range(n_candidates):
                V, MT = self._decode(c, rng, temperature=temperature, sample=True)
                cands.append((V, MT, "model"))
            V, MT = self._decode(c, rng, temperature=1.0, sample=False)
            cands.append((V, MT, "model-argmax"))
        for V, MT, sim in self._retrieve(acc_k, acc_s, style_id, bpm, k=3):
            cands.append((V, MT, "human"))
        if not cands:
            return None
        scored = []
        for V, MT, src in cands:
            if (V > 0).sum() < 4:
                continue
            f = self.fit_score(V, acc_k, acc_s, acc_h, density, song_hat_density) + (0.03 if src.startswith("model") else 0.0)
            scored.append((f, V, MT, src))
        if not scored:
            return None
        scored.sort(key=lambda q: -q[0])
        f, V, MT, src = scored[0]
        V2, MT2 = self.apply_rules(V, MT, acc_k, acc_s, song_hat_density)
        info = {"source": src, "fit": round(float(f), 3), "hits": int((V2 > 0).sum()), "kick_hits": int((V2[:, 0] > 0).sum()),
                "hat_hits": int((V2[:, 2:] > 0).sum()), "candidates": len(scored), "density": round(density, 2), "style": STYLES[int(style_id)] if 0 <= int(style_id) < len(STYLES) else "other",
                "runner_up": scored[1][3] if len(scored) > 1 else ""}
        return V2.astype(np.float32), MT2.astype(np.float32), info
