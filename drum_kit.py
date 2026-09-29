"""Per-record drum kit (JoyMetric v31.19).

The AI drummer plays SOUNDS THAT BELONG TO THE RECORD:
  * closed / open hats are cut out of the record itself (DOSE-lite: the most
    isolated high-band transients whose low/mid content is negligible, decay
    classified, high-passed, enveloped);
  * the kick is either cut from the record (an isolated low-band transient that
    decays like a drum, not a bass note) or synthesised to the record's low end
    (fundamental and decay measured from the record's own low-band onsets);
  * a clap is synthesised;
  * optional: Stable Audio audio-to-audio polish of the one-shots (local worker).
All numpy / scipy; a kit builds in ~60 ms from a 12 s window.
"""
from __future__ import annotations
import math, os, json, time, tempfile
import numpy as np
from scipy.signal import butter, sosfilt


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def _env5(x, sr):
    hop = max(1, int(round(sr * 0.005)))
    n = len(x) // hop
    if n < 4:
        return np.zeros(4), hop
    return np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12), hop


def _peaks(env, thr_rel=0.25, min_gap=12):
    m = float(env.max()) if env.size else 0.0
    if m <= 0:
        return []
    c = env[1:-1]
    cand = np.where((c >= env[:-2]) & (c >= env[2:]) & (c > thr_rel * m))[0] + 1
    out = []
    for i in cand:
        if out and i - out[-1] < min_gap:
            if env[i] > env[out[-1]]: out[-1] = int(i)
        else:
            out.append(int(i))
    return out


def _decay_frames(env, i, ratio):
    pk = env[i]
    for j in range(i + 1, min(len(env), i + 200)):
        if env[j] < ratio * pk:
            return j - i
    return 200


def _fade_env(n, sr, tau, attack=0.001):
    t = np.arange(n) / sr
    return (np.minimum(t / max(attack, 1e-5), 1.0) * np.exp(-t / tau)).astype(np.float32)


def _norm(x, peak=0.9):
    m = float(np.max(np.abs(x))) if x.size else 0.0
    return (x / m * peak).astype(np.float32) if m > 1e-9 else x.astype(np.float32)


def synth_hat(sr, closed=True, seed=3):
    r = np.random.default_rng(seed)
    n = int(sr * (0.08 if closed else 0.22))
    x = r.standard_normal(n).astype(np.float32)
    x = sosfilt(butter(4, 6500.0, btype="high", fs=sr, output="sos"), x)
    x = sosfilt(butter(2, [7000.0, 12000.0], btype="band", fs=sr, output="sos"), x) * 0.6 + x * 0.6
    return _norm(x * _fade_env(n, sr, 0.022 if closed else 0.075), 0.9)


def synth_kick(sr, f0=52.0, tau=0.11, click=0.5):
    n = int(sr * 0.28)
    t = np.arange(n) / sr
    tp = 0.012
    phase = 2 * np.pi * (f0 * t + 2.4 * f0 * tp * (1 - np.exp(-t / tp)))
    body = np.sin(phase) * np.exp(-t / tau) * np.minimum(t / 0.0006, 1.0)
    r = np.random.default_rng(5)
    ck = sosfilt(butter(2, 2500.0, btype="high", fs=sr, output="sos"), r.standard_normal(n).astype(np.float32)) * np.exp(-t / 0.0025) * 0.35 * click
    y = np.tanh(1.4 * body) + ck
    return _norm(y, 0.95)


def synth_clap(sr, seed=9):
    r = np.random.default_rng(seed)
    n = int(sr * 0.16); t = np.arange(n) / sr
    y = np.zeros(n, np.float32)
    noise = sosfilt(butter(2, [900.0, 4500.0], btype="band", fs=sr, output="sos"), r.standard_normal(n).astype(np.float32))
    for k, off in enumerate((0.0, 0.009, 0.018, 0.027)):
        i = int(off * sr); env = np.exp(-(t[: n - i]) / 0.004) * (0.7 + 0.3 * (k == 3))
        y[i:] += noise[: n - i] * env
    y += noise * np.exp(-t / 0.035) * 0.6
    return _norm(y, 0.8)


def _extend_tail(sample, sr, total_s, tau, seed=11):
    """Extend a short noise-like sample to total_s with looped, polarity-flipped grains."""
    r = np.random.default_rng(seed)
    n_tot = int(total_s * sr)
    if len(sample) >= n_tot:
        return sample[:n_tot]
    g0 = int(0.025 * sr); g1 = min(len(sample), int(0.045 * sr))
    grain = sample[g0:g1]
    if grain.size < 64:
        return sample
    out = np.zeros(n_tot, np.float32); out[:len(sample)] = sample
    pos = len(sample); xf = int(0.004 * sr)
    while pos < n_tot:
        g = grain * (1 if r.random() < 0.5 else -1)
        m = min(len(g), n_tot - pos)
        w = np.ones(m, np.float32)
        if xf < m: w[:xf] = np.linspace(0, 1, xf)
        out[pos:pos + m] = out[pos:pos + m] * (1 - w) + g[:m] * w
        pos += m - xf if m > xf else m
    return out * np.exp(-np.arange(n_tot) / sr / tau).astype(np.float32)


def build_kit(audio, sr: int, bpm: float = 120.0) -> dict:
    x = np.asarray(audio, dtype=np.float32)
    if x.ndim == 2:
        x = x.mean(axis=1)
    sr = int(sr)
    meta = {"hat_source": "synth", "kick_source": "synth", "snare_source": "synth"}
    hf = sosfilt(butter(4, 5000.0, btype="high", fs=sr, output="sos"), x)
    low = sosfilt(butter(4, 180.0, btype="low", fs=sr, output="sos"), x)
    mid = sosfilt(butter(4, [200.0, 2500.0], btype="band", fs=sr, output="sos"), x)
    e_hf, hop = _env5(hf, sr); e_low, _ = _env5(low, sr); e_mid, _ = _env5(mid, sr)
    n_env = min(len(e_hf), len(e_low), len(e_mid))
    e_hf, e_low, e_mid = e_hf[:n_env], e_low[:n_env], e_mid[:n_env]
    kit = {"sr": sr}
    # ---------------- hats from the record
    closed_best = None; open_best = None
    if n_env > 40 and e_hf.max() > 1e-5:
        med_hf = float(np.median(e_hf)) + 1e-9
        for i in _peaks(e_hf, 0.2, 10):
            j0 = max(0, i - 3); j1 = min(n_env, i + 4)
            pk = float(e_hf[i:j1].max())
            # isolation on the TRANSIENT, not the level: a pad or a bass line keeps the
            # low/mid level up all the time, but a hat alone does not make them JUMP
            d_hf = pk - float(e_hf[j0:i].min())
            d_low = float(e_low[i:j1].max()) - float(e_low[j0:i].min())
            d_mid = float(e_mid[i:j1].max()) - float(e_mid[j0:i].min())
            iso = max(0.0, d_hf) / (max(0.0, d_low) + max(0.0, d_mid) + 0.15 * pk + 1e-6)
            prom = pk / med_hf
            if prom < 2.0:
                continue
            dec = _decay_frames(e_hf, i, 0.3)
            score = iso * min(3.0, prom / 3.0)
            if dec <= 18 and (closed_best is None or score > closed_best[0]):
                closed_best = (score, i, iso, dec)
            elif 18 < dec <= 60 and (open_best is None or score > open_best[0]):
                open_best = (score, i, iso, dec)
    def cut_hat(i, seconds, tau):
        s0 = max(0, i * hop - int(0.001 * sr)); n = int(seconds * sr)
        seg = x[s0:s0 + n]
        if seg.size < n:
            seg = np.pad(seg, (0, n - seg.size))
        seg = sosfilt(butter(4, 3500.0, btype="high", fs=sr, output="sos"), seg)
        return _norm(seg * _fade_env(n, sr, tau), 0.9)
    if closed_best is not None and closed_best[2] >= 1.5:
        kit["chat"] = cut_hat(closed_best[1], 0.075, 0.020)
        meta.update({"hat_source": "record", "hat_isolation": round(float(closed_best[2]), 2), "hat_decay_ms": int(closed_best[3] * 5)})
    else:
        kit["chat"] = synth_hat(sr, True)
    if open_best is not None and open_best[2] >= 1.5:
        kit["ohat"] = cut_hat(open_best[1], min(0.24, open_best[3] * 0.005 + 0.08), 0.085)
        meta["ohat_source"] = "record"
    else:
        kit["ohat"] = _norm(_extend_tail(kit["chat"], sr, 0.20, 0.075), 0.85) if meta["hat_source"] == "record" else synth_hat(sr, False)
        meta["ohat_source"] = "extended" if meta["hat_source"] == "record" else "synth"
    # ---------------- kick: measure the record's low end, cut or synthesise
    f0 = 52.0; tau = 0.11; kick_best = None
    if n_env > 40 and e_low.max() > 1e-5:
        med_low = float(np.median(e_low)) + 1e-9
        f0s = []; taus = []
        for i in _peaks(e_low, 0.3, 16):
            dec = _decay_frames(e_low, i, 0.25)
            if dec > 44:        # > 220 ms: a bass note, not a drum
                continue
            s0 = i * hop; seg = low[s0:s0 + int(0.12 * sr)]
            if seg.size < int(0.05 * sr):
                continue
            spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), n=1 << 15)); fr = np.fft.rfftfreq(1 << 15, 1.0 / sr)
            band = (fr >= 35) & (fr <= 110)
            if not band.any():
                continue
            fpk = float(fr[band][np.argmax(spec[band])])
            f0s.append(fpk); taus.append(max(0.04, min(0.3, dec * 0.005 / 1.4)))
            iso = float(e_low[i]) / (float(e_mid[max(0, i - 1):i + 3].max()) + float(e_hf[max(0, i - 1):i + 3].max()) + 1e-6)
            score = iso * min(3.0, float(e_low[i]) / med_low / 3.0)
            if kick_best is None or score > kick_best[0]:
                kick_best = (score, i, iso, dec)
        if f0s:
            f0 = float(np.median(f0s)); tau = float(np.median(taus))
    if kick_best is not None and kick_best[2] >= 2.5:
        i = kick_best[1]; s0 = max(0, i * hop - int(0.001 * sr)); n = int(0.18 * sr)
        seg = x[s0:s0 + n]
        if seg.size < n: seg = np.pad(seg, (0, n - seg.size))
        body = sosfilt(butter(4, 200.0, btype="low", fs=sr, output="sos"), seg)
        click = seg * _fade_env(n, sr, 0.004)
        kick = _norm((body + 0.5 * click) * _fade_env(n, sr, max(0.05, tau)), 0.95)
        kit["kick"] = kick
        meta.update({"kick_source": "record", "kick_isolation": round(float(kick_best[2]), 2)})
    else:
        kit["kick"] = synth_kick(sr, f0=f0, tau=tau)
        meta["kick_source"] = "synth"
    meta.update({"kick_f0": round(f0, 1), "kick_tau_ms": int(tau * 1000)})
    kit["snare"] = synth_clap(sr)
    kit["kick_peak"] = float(np.max(np.abs(kit["kick"]))); kit["hat_peak"] = float(np.max(np.abs(kit["chat"])))
    kit["meta"] = meta
    kit["label"] = "hat:%s kick:%s f0=%.0fHz" % (meta["hat_source"], meta["kick_source"], f0)
    return kit


def ai_polish(kit: dict, style_text: str, url: str = "http://127.0.0.1:8766", steps: int = 10, noise: float = 0.35, timeout: float = 120.0):
    """Optional Stable Audio audio-to-audio polish of the kick and closed hat (local GPU worker).
    Returns a new kit dict or None; never raises."""
    try:
        import soundfile as sf, urllib.request, json as _json
        sr = int(kit["sr"]); tmp = tempfile.mkdtemp(prefix="joykit_")
        tasks = []; paths = {}
        for name, prompt in (("kick", "single punchy kick drum one-shot, %s, clean, dry, high quality" % style_text),
                             ("chat", "single crisp closed hi-hat one-shot, %s, clean, dry, high quality" % style_text)):
            clip = np.zeros(int(1.5 * sr), np.float32); s = kit[name]; clip[int(0.2 * sr):int(0.2 * sr) + len(s)] = s[:len(clip) - int(0.2 * sr)]
            pin = os.path.join(tmp, name + "_in.wav"); pout = os.path.join(tmp, name + "_out.wav")
            sf.write(pin, clip, sr); paths[name] = pout
            tasks.append({"name": name, "input": pin, "output": pout, "prompt": prompt, "noise": float(noise), "cfg_scale": 6.0, "negative_prompt": "music, melody, reverb, noise, distortion"})
        body = _json.dumps({"steps": int(steps), "tasks": tasks, "seed": 1234}).encode("utf-8")
        req = urllib.request.Request(url + "/run", data=body, headers={"Content-Type": "application/json"}, method="POST")
        t0 = time.time(); done = False
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for line in r:
                try:
                    ev = _json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                if ev.get("type") == "done": done = bool(ev.get("ok")); break
                if ev.get("type") == "error": break
                if time.time() - t0 > timeout: break
        if not done:
            return None
        out = dict(kit); out["meta"] = dict(kit["meta"])
        for name, pout in paths.items():
            y, rsr = sf.read(pout, dtype="float32", always_2d=True); y = y.mean(axis=1)
            if rsr != sr:
                from scipy.signal import resample_poly
                from math import gcd
                g = gcd(int(sr), int(rsr)); y = resample_poly(y, sr // g, rsr // g).astype(np.float32)
            # locate the one-shot: strongest onset in the clip, keep its natural length
            e, hop = _env5(y, sr)
            i = int(np.argmax(e)); s0 = max(0, i * hop - int(0.003 * sr)); n = len(kit[name]) if name == "chat" else int(0.26 * sr)
            seg = y[s0:s0 + n]
            if seg.size >= int(0.02 * sr):
                out[name] = _norm(seg * _fade_env(seg.size, sr, 0.025 if name == "chat" else 0.14), 0.9)
                out["meta"][name + "_source"] = kit["meta"].get(("hat_source" if name == "chat" else "kick_source"), "?") + "+sa3"
        out["kick_peak"] = float(np.max(np.abs(out["kick"]))); out["hat_peak"] = float(np.max(np.abs(out["chat"])))
        out["label"] = out["label"] + " · sa3 polished"
        return out
    except Exception:
        return None
