"""JoyMetric v31.27 SongMind - the part-writing Transformer (torch; shared by the trainer and the GPU worker).

Encoder: 64 song cells (135 numbers each: bass / top MIDI pitch, inner pitch classes, onsets, drums,
position, key, tempo, loudness) + one condition token (family, genre, density, register, relation)
-> 4 Transformer layers, d 256, 8 heads.  Decoder: the part's 64 tokens (rest / hold / pitch 48..84),
causal self-attention + cross-attention to the song, 4 layers.  ~6.5 M parameters.
Sampling (`write`) applies the live masks per cell: allowed pitch classes, register, consonance against
the record's top line at onsets, hold only inside a note, a repetition penalty, a minimum note length;
a prefix (our previous bars) can be forced so the new bars continue it.
"""
from __future__ import annotations

import math

import torch
import torch.nn as nn
import torch.nn.functional as F

V = 39; PITCH_LO = 48; N_CTX = 135; N_COND = 35; CELLS = 64; START = V
CONSONANT = (0, 3, 4, 5, 7, 8, 9)


class SongMind(nn.Module):
    def __init__(self, d_model: int = 256, n_heads: int = 8, n_enc: int = 4, n_dec: int = 4, d_ff: int = 512, dropout: float = 0.1):
        super().__init__()
        self.d = d_model
        self.ctx_in = nn.Linear(N_CTX, d_model); self.cond_in = nn.Linear(N_COND, d_model)
        self.pos_enc = nn.Parameter(torch.randn(CELLS + 1, d_model) * 0.02)
        self.pos_dec = nn.Parameter(torch.randn(CELLS, d_model) * 0.02)
        self.emb = nn.Embedding(V + 1, d_model)
        enc_layer = nn.TransformerEncoderLayer(d_model, n_heads, d_ff, dropout, batch_first=True, norm_first=True)
        dec_layer = nn.TransformerDecoderLayer(d_model, n_heads, d_ff, dropout, batch_first=True, norm_first=True)
        self.encoder = nn.TransformerEncoder(enc_layer, n_enc); self.decoder = nn.TransformerDecoder(dec_layer, n_dec)
        self.out = nn.Linear(d_model, V)
        self.register_buffer("causal", torch.triu(torch.full((CELLS, CELLS), float("-inf")), diagonal=1), persistent=False)

    def encode(self, x, cond):
        """x (B, 64, 135), cond (B, 35) -> memory (B, 65, d)"""
        h = torch.cat([self.cond_in(cond)[:, None, :], self.ctx_in(x)], dim=1) + self.pos_enc[None, :, :]
        return self.encoder(h)

    def decode(self, memory, y_prev):
        """y_prev (B, 64) previous tokens (START first) -> logits (B, 64, V)"""
        h = self.emb(y_prev) + self.pos_dec[None, :y_prev.shape[1], :]
        L = y_prev.shape[1]
        h = self.decoder(h, memory, tgt_mask=self.causal[:L, :L])
        return self.out(h)

    def forward(self, x, cond, y_prev):
        return self.decode(self.encode(x, cond), y_prev)

    # ------------------------------------------------------------------ live writing
    @torch.no_grad()
    def write(self, x, cond, prefix=None, allowed=None, top_line=None, pitch_lo=52, pitch_hi=79, temperature=1.0, top_p=0.92, rep_penalty=1.6, min_hold=1, K=1, seed=0, consonance=True):
        """x (64, 135), cond (35).  prefix: tokens forced for the first cells (our previous bars) or None.
        allowed: list of 64 sets of pitch classes (None = free); top_line: (64,) midi or -1.
        Returns (tokens (K, 64), mean log-prob of the sampled cells (K,))."""
        dev = next(self.parameters()).device
        g = torch.Generator(device=dev); g.manual_seed(int(seed))
        xb = torch.as_tensor(x, dtype=torch.float32, device=dev)[None].repeat(K, 1, 1); cb = torch.as_tensor(cond, dtype=torch.float32, device=dev)[None].repeat(K, 1)
        memory = self.encode(xb, cb)
        toks = torch.full((K, CELLS), START, dtype=torch.long, device=dev)
        out = torch.zeros((K, CELLS), dtype=torch.long, device=dev); lp_sum = torch.zeros(K, device=dev); n_free = 0
        last_pitch = torch.full((K,), -1, dtype=torch.long, device=dev); run = torch.zeros(K, dtype=torch.long, device=dev)
        prefix_len = int(len(prefix)) if prefix is not None else 0
        for t in range(CELLS):
            y_prev = torch.cat([torch.full((K, 1), START, dtype=torch.long, device=dev), out[:, :t]], dim=1) if t > 0 else torch.full((K, 1), START, dtype=torch.long, device=dev)
            logits = self.decode(memory, y_prev)[:, -1, :] / max(1e-3, float(temperature))
            if t < prefix_len:
                tok = torch.full((K,), int(prefix[t]), dtype=torch.long, device=dev)
                out[:, t] = tok
                last_pitch = torch.where(tok >= 2, tok - 2 + PITCH_LO, last_pitch); run = torch.where(tok == 1, run + 1, torch.where(tok >= 2, torch.ones_like(run), torch.zeros_like(run)))
                continue
            mask = torch.zeros((K, V), device=dev)
            prev = out[:, t - 1] if t > 0 else torch.full((K,), START, dtype=torch.long, device=dev)
            mask[:, 1] = torch.where((prev == START) | (prev == 0), torch.tensor(-1e9, device=dev), torch.tensor(0.0, device=dev))
            for k in range(2, V):
                p = PITCH_LO + k - 2
                if p < pitch_lo or p > pitch_hi:
                    mask[:, k] = -1e9; continue
                if allowed is not None and allowed[t] is not None and (p % 12) not in allowed[t]:
                    mask[:, k] = -1e9; continue
                if consonance and top_line is not None and int(top_line[t]) >= 0 and ((p - int(top_line[t])) % 12) not in CONSONANT:
                    mask[:, k] = -1e9
            # repetition penalty on the previous onset pitch; minimum note length (prefer hold right after an onset)
            rep_idx = last_pitch - PITCH_LO + 2
            valid = (last_pitch >= 0)
            if valid.any():
                mask[torch.arange(K, device=dev)[valid], rep_idx[valid]] -= float(rep_penalty)
            short = (run >= 1) & (run < min_hold)
            if short.any():
                mask[short, 0] -= 3.0
                for k in range(2, V):
                    mask[short, k] -= 3.0
            lg = logits + mask
            probs = F.softmax(lg, dim=-1)
            if top_p < 1.0:
                sp, si = torch.sort(probs, descending=True, dim=-1); cum = torch.cumsum(sp, dim=-1)
                remove = cum - sp > top_p
                sp = torch.where(remove, torch.zeros_like(sp), sp); sp = sp / sp.sum(dim=-1, keepdim=True).clamp_min(1e-9)
                choice = torch.multinomial(sp, 1, generator=g)[:, 0]; tok = si[torch.arange(K, device=dev), choice]
            else:
                tok = torch.multinomial(probs, 1, generator=g)[:, 0]
            out[:, t] = tok
            lp_sum += torch.log(probs[torch.arange(K, device=dev), tok].clamp_min(1e-9)); n_free += 1
            last_pitch = torch.where(tok >= 2, tok - 2 + PITCH_LO, last_pitch)
            run = torch.where(tok == 1, run + 1, torch.where(tok >= 2, torch.ones_like(run), torch.zeros_like(run)))
        return out.cpu().numpy(), (lp_sum / max(1, n_free)).cpu().numpy()

    @torch.no_grad()
    def score(self, x, cond, tokens):
        """mean log-likelihood of token sequences (K, 64) under the model (teacher forcing)."""
        dev = next(self.parameters()).device
        tk = torch.as_tensor(tokens, dtype=torch.long, device=dev)
        if tk.ndim == 1:
            tk = tk[None]
        K = tk.shape[0]
        xb = torch.as_tensor(x, dtype=torch.float32, device=dev)[None].repeat(K, 1, 1); cb = torch.as_tensor(cond, dtype=torch.float32, device=dev)[None].repeat(K, 1)
        y_prev = torch.cat([torch.full((K, 1), START, dtype=torch.long, device=dev), tk[:, :-1]], dim=1)
        lp = F.log_softmax(self.forward(xb, cb, y_prev), dim=-1)
        return lp.gather(-1, tk[:, :, None])[:, :, 0].mean(dim=1).cpu().numpy()
