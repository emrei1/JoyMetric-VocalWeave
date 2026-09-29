"""JoyMetric v31.26 MelodyNet runtime (numpy) - OUR trained part writer.

Trained on the Lakh MIDI dataset (real multi-track songs, ~30 k): given what the other tracks of a song
do in a 2-bar window (bass pitch class, inner-voice pitch classes, the top line's pitch class, kick /
snare / hat, position, key, tempo - exactly what LineFinder extracts from the record's audio) plus the
instrument family of the part to write and the song's genre, it writes that part sixteenth by sixteenth
(rest / hold / pitch 48..84).  Sampling is masked for the live DJ: allowed pitch classes per cell (chord
tones on the beats, the key in between), consonance against the record's top line at note onsets, a
register per role, and "hold" only inside a note.  The same network scores any candidate part
(log-likelihood), which ranks parts from other generators.
"""
from __future__ import annotations

import os

import numpy as np

V = 39; PITCH_LO = 48; N_CTX = 58; N_COND = 24; START = V
CONSONANT = (0, 3, 4, 5, 7, 8, 9)            # intervals (mod 12) against the record's top line at an onset
FAMILY_LEAD, FAMILY_PAD = 7, 8


def _sig(x):
    return 1.0 / (1.0 + np.exp(-x))


def _log_softmax(x):
    m = x.max(); e = np.exp(x - m); return x - m - np.log(e.sum())


class MelodyNet:
    def __init__(self, path: str | None = None):
        path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "melodynet.npz")
        z = np.load(path)
        self.w = {k: np.asarray(z[k], dtype=np.float32) for k in z.files}
        self.ready = True

    # ------------------------------------------------------------------ GRU pieces (PyTorch gate order r, z, n)
    def _gru_dir(self, x, pre, reverse=False):
        sfx = "_reverse" if reverse else ""
        Wih = self.w[pre + "weight_ih_l0" + sfx]; Whh = self.w[pre + "weight_hh_l0" + sfx]; bih = self.w[pre + "bias_ih_l0" + sfx]; bhh = self.w[pre + "bias_hh_l0" + sfx]
        H = Whh.shape[1]; T = x.shape[0]; h = np.zeros(H, np.float32); out = np.zeros((T, H), np.float32)
        gi_all = x @ Wih.T + bih
        for t in (range(T - 1, -1, -1) if reverse else range(T)):
            gi = gi_all[t]; gh = h @ Whh.T + bhh
            r = _sig(gi[:H] + gh[:H]); zg = _sig(gi[H:2 * H] + gh[H:2 * H]); n = np.tanh(gi[2 * H:] + r * gh[2 * H:])
            h = (1.0 - zg) * n + zg * h; out[t] = h
        return out

    def encode(self, X, cond):
        X = np.asarray(X, dtype=np.float32); cond = np.asarray(cond, dtype=np.float32)
        x = np.concatenate([X, np.repeat(cond[None, :], X.shape[0], axis=0)], axis=1)
        z = np.tanh(x @ self.w["inp.weight"].T + self.w["inp.bias"])
        z = np.concatenate([self._gru_dir(z, "gru1.", False), self._gru_dir(z, "gru1.", True)], axis=1)
        z = np.concatenate([self._gru_dir(z, "gru2.", False), self._gru_dir(z, "gru2.", True)], axis=1)
        return z

    def _dec_step(self, enc_t, tok, h):
        e = self.w["emb.weight"][int(tok)]
        x = np.concatenate([enc_t, e])
        Wih = self.w["dec.weight_ih_l0"]; Whh = self.w["dec.weight_hh_l0"]; bih = self.w["dec.bias_ih_l0"]; bhh = self.w["dec.bias_hh_l0"]
        H = Whh.shape[1]
        gi = x @ Wih.T + bih; gh = h @ Whh.T + bhh
        r = _sig(gi[:H] + gh[:H]); zg = _sig(gi[H:2 * H] + gh[H:2 * H]); n = np.tanh(gi[2 * H:] + r * gh[2 * H:])
        h = (1.0 - zg) * n + zg * h
        return self.w["out.weight"] @ h + self.w["out.bias"], h

    # ------------------------------------------------------------------ writing a part
    @staticmethod
    def cond_vector(family: int, genre: int):
        c = np.zeros(N_COND, np.float32); c[int(family) % 10] = 1.0; c[10 + int(genre) % 14] = 1.0; return c

    def sample(self, X, cond, allowed_pcs=None, top_line=None, pitch_lo=52, pitch_hi=79, temperature=1.0, top_p=0.92, seed=0, consonance=True, min_gap_cells=1):
        """tokens (32,) with the live masks.  allowed_pcs: list of 32 sets (or None); top_line: (32,) midi or -1."""
        rng = np.random.default_rng(int(seed) & 0xFFFFFFFF)
        enc = self.encode(X, cond); H = self.w["dec.weight_hh_l0"].shape[1]; h = np.zeros(H, np.float32); tok = START
        out = np.zeros(32, dtype=np.int64); last_on = -99
        for t in range(32):
            logits, h = self._dec_step(enc[t], tok, h)
            mask = np.zeros(V, np.float32)
            if tok in (START, 0):
                mask[1] = -1e9                                   # hold only inside a note
            if t - last_on < min_gap_cells and tok >= 2:
                pass
            for k in range(2, V):
                p = PITCH_LO + k - 2
                if p < pitch_lo or p > pitch_hi:
                    mask[k] = -1e9; continue
                if allowed_pcs is not None and allowed_pcs[t] is not None and (p % 12) not in allowed_pcs[t]:
                    mask[k] = -1e9; continue
                if consonance and top_line is not None and int(top_line[t]) >= 0:
                    iv = (p - int(top_line[t])) % 12
                    if iv not in CONSONANT:
                        mask[k] = -1e9
            lg = (logits + mask) / max(1e-3, float(temperature))
            lp = _log_softmax(lg); pr = np.exp(lp)
            if pr.sum() <= 0 or not np.isfinite(pr).all():
                tok = 0; out[t] = 0; continue
            order = np.argsort(-pr); cum = np.cumsum(pr[order]); keep = order[:max(1, int(np.searchsorted(cum, top_p) + 1))]
            q = pr[keep] / pr[keep].sum()
            tok = int(rng.choice(keep, p=q)); out[t] = tok
            if tok >= 2:
                last_on = t
        return out

    def score(self, X, cond, tokens):
        """mean log-likelihood of a token sequence (32,) under the model (teacher forcing)."""
        enc = self.encode(X, cond); H = self.w["dec.weight_hh_l0"].shape[1]; h = np.zeros(H, np.float32); tok = START; ll = 0.0
        for t in range(32):
            logits, h = self._dec_step(enc[t], tok, h)
            ll += float(_log_softmax(logits)[int(tokens[t])]); tok = int(tokens[t])
        return ll / 32.0

    def logprobs(self, X, cond, tokens):
        """(32, V) log-probabilities with teacher forcing (parity with torch)."""
        enc = self.encode(X, cond); H = self.w["dec.weight_hh_l0"].shape[1]; h = np.zeros(H, np.float32); tok = START; out = np.zeros((32, V), np.float32)
        for t in range(32):
            logits, h = self._dec_step(enc[t], tok, h)
            out[t] = _log_softmax(logits); tok = int(tokens[t])
        return out

    # ------------------------------------------------------------------ tokens <-> notes
    @staticmethod
    def tokens_to_notes(tokens, cells_per_beat: int = 4):
        """[(start_beat, dur_beats, midi)]"""
        notes = []; cur = None
        for c, tk in enumerate(tokens):
            tk = int(tk)
            if tk >= 2:
                if cur is not None:
                    notes.append(cur)
                cur = [c / cells_per_beat, 1.0 / cells_per_beat, PITCH_LO + tk - 2]
            elif tk == 1 and cur is not None:
                cur[1] += 1.0 / cells_per_beat
            else:
                if cur is not None:
                    notes.append(cur); cur = None
        if cur is not None:
            notes.append(cur)
        return [tuple(n) for n in notes]

    @staticmethod
    def notes_to_tokens(notes, cells_per_beat: int = 4):
        toks = np.zeros(32, dtype=np.int64)
        for (t, d, p) in notes:
            c0 = int(round(float(t) * cells_per_beat)); c1 = c0 + max(1, int(round(float(d) * cells_per_beat)))
            p = int(p)
            while p < PITCH_LO: p += 12
            while p > PITCH_LO + V - 3: p -= 12
            if 0 <= c0 < 32:
                toks[c0] = 2 + (p - PITCH_LO)
                for c in range(c0 + 1, min(32, c1)):
                    toks[c] = 1
        return toks
