from __future__ import annotations
import json
import math
import os
import shutil
import subprocess
import time
import hashlib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import imageio_ffmpeg
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly


class ProcessingError(RuntimeError):
    pass


class PersistentWorkerUnavailable(ProcessingError):
    pass


_ENV_CACHE = {}
_PROBE_CACHE = {"at": 0.0, "value": None}


def log_line(log: Path, text: str):
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8", errors="replace") as f:
        f.write(text + "\n")


def run(cmd, log: Path, cwd=None):
    log_line(log, "\n$ " + " ".join(map(str, cmd)))
    with log.open("a", encoding="utf-8", errors="replace") as f:
        p = subprocess.Popen(
            list(map(str, cmd)),
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for line in p.stdout:
            f.write(line)
            f.flush()
        code = p.wait()
    if code:
        raise ProcessingError(f"Command failed ({code}). See process.log.")


def run_gpu_worker(cmd, log: Path, progress):
    log_line(log, "\n$ " + " ".join(map(str, cmd)))
    with log.open("a", encoding="utf-8", errors="replace") as f:
        p = subprocess.Popen(
            list(map(str, cmd)),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        for line in p.stdout:
            f.write(line)
            f.flush()
            if line.startswith("PROGRESS "):
                try:
                    x = json.loads(line[len("PROGRESS "):])
                    worker_pct = float(x.get("progress", 0.0))
                    # GPU worker occupies 35..88% of total job progress.
                    pct = 35.0 + worker_pct * 0.53
                    progress(x.get("stage", "SA3 GPU"), min(88, int(pct)), x.get("message", ""))
                except Exception:
                    pass
        code = p.wait()
    if code:
        raise ProcessingError(f"Stable Audio GPU worker failed ({code}). See process.log.")


def _persistent_worker_base(cfg):
    host = str(cfg.get("gpu_worker_host", "127.0.0.1"))
    port = int(cfg.get("gpu_worker_port", 8766))
    return f"http://{host}:{port}"


def persistent_worker_health(cfg, timeout=0.4):
    try:
        with urllib.request.urlopen(_persistent_worker_base(cfg) + "/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def persistent_worker_control(cfg, action: str, timeout=180.0):
    """Move the already-loaded model between CPU RAM and GPU VRAM.

    This does not reconstruct or re-download the checkpoint; it only moves the
    existing torch module so RoFormer can temporarily own the 8 GB laptop GPU.
    """
    if action not in {"offload", "activate"}:
        raise ValueError(action)
    req = urllib.request.Request(
        _persistent_worker_base(cfg) + f"/{action}", data=b"", method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return bool(data.get("ok")), data
    except Exception as e:
        return False, {"ok": False, "error": str(e)}


def run_persistent_gpu_worker(cfg, data: dict, log: Path, progress):
    """Send one job to the app-lifetime SA3 process and stream progress back."""
    startup_timeout = float(cfg.get("gpu_worker_startup_timeout_seconds", 180))
    deadline = time.monotonic() + startup_timeout
    announced = False
    while time.monotonic() < deadline:
        h = persistent_worker_health(cfg, timeout=0.35)
        if h and h.get("ok"):
            # Realtime DJ may have parked Stable Audio on CPU to give CLAP the
            # GPU. Move the already-loaded model back before an offline GPU job.
            if h.get("device") != "cuda":
                progress("Restore audio engine", 34, "Returning the resident Stable Audio model to CUDA.")
                ok, info = persistent_worker_control(cfg, "activate", timeout=180.0)
                if not ok:
                    raise PersistentWorkerUnavailable("Could not reactivate Stable Audio CUDA: " + str(info))
            break
        if not announced:
            progress("SA3 engine startup", 35, "Loading Stable Audio 3 Medium once; later edits reuse the same GPU model.")
            announced = True
        time.sleep(0.5)
    else:
        raise PersistentWorkerUnavailable(f"Persistent SA3 engine did not become ready within {startup_timeout:.0f}s.")

    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        _persistent_worker_base(cfg) + "/run",
        data=body,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        method="POST",
    )
    log_line(log, "\n$ persistent SA3 engine POST /run")
    done = False
    try:
        # Generation can take minutes without network activity inside a single
        # model.generate(), so use a deliberately long socket timeout.
        with urllib.request.urlopen(req, timeout=7200) as r, log.open("a", encoding="utf-8", errors="replace") as f:
            for raw in r:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                f.write("SA3_ENGINE " + line + "\n")
                f.flush()
                try:
                    x = json.loads(line)
                except Exception:
                    continue
                typ = x.get("type")
                if typ == "progress":
                    worker_pct = float(x.get("progress", 0.0))
                    pct = 35.0 + worker_pct * 0.53
                    progress(x.get("stage", "SA3 GPU"), min(88, int(pct)), x.get("message", ""))
                elif typ == "engine":
                    progress("SA3 GPU ready", 36, x.get("message", "Persistent model ready."))
                elif typ == "error":
                    raise ProcessingError("Persistent Stable Audio engine failed: " + str(x.get("error", "unknown error")))
                elif typ == "done":
                    done = True
    except urllib.error.URLError as e:
        raise PersistentWorkerUnavailable(f"Persistent SA3 engine connection failed: {e}") from e
    if not done:
        raise PersistentWorkerUnavailable("Persistent SA3 engine disconnected before completing the job.")


def conda(cfg):
    p = Path(cfg["conda_exe"])
    if not p.exists():
        raise ProcessingError(f"conda.exe not found: {p}")
    return str(p)


def env_path(cfg, name):
    key = name.lower()
    if key in _ENV_CACHE and Path(_ENV_CACHE[key]).exists():
        return Path(_ENV_CACHE[key])
    r = subprocess.run([conda(cfg), "env", "list", "--json"], capture_output=True, text=True, timeout=20)
    if r.returncode:
        return None
    for x in json.loads(r.stdout).get("envs", []):
        p = Path(x)
        if p.name.lower() == key:
            _ENV_CACHE[key] = str(p)
            return p
    return None


def env_python(cfg, name):
    p = env_path(cfg, name)
    if not p:
        return None
    py = p / "python.exe"
    return py if py.exists() else None


def env_exe(cfg, name, exe):
    p = env_path(cfg, name)
    if not p:
        return None
    candidates = [p / "Scripts" / exe, p / exe]
    for c in candidates:
        if c.exists():
            return c
    return None


def gpu_probe(app_root, cfg):
    # If the persistent app-lifetime engine is already alive, it is a stronger
    # readiness signal than launching another CUDA probe process.
    h = persistent_worker_health(cfg, timeout=0.35)
    if h and h.get("ok"):
        return {
            "available": True,
            "reason": "Persistent SA3 Medium ready",
            "persistent": True,
            "name": h.get("gpu") or "GPU",
        }
    py = env_python(cfg, cfg["sa3_gpu_env"])
    if not py:
        return {"available": False, "reason": "stableaudio3_gpu env not installed"}
    try:
        r = subprocess.run([str(py), str(app_root / "workers" / "sa3_gpu_probe.py")], capture_output=True, text=True, timeout=45)
        txt = (r.stdout + "\n" + r.stderr).strip()
        ok = r.returncode == 0 and "GPU_PROBE_OK" in txt
        gpu_name = None
        if ok:
            for line in txt.splitlines():
                if line.lower().startswith("gpu:"):
                    gpu_name = line.split(":", 1)[1].strip() or None
                    break
        return {
            "available": ok,
            "reason": "CUDA Medium ready" if ok else txt[-650:],
            "name": gpu_name if ok else None,
        }
    except Exception as e:
        return {"available": False, "reason": str(e)}


def probe_backends(app_root, cfg, force=False):
    now = time.time()
    if not force and _PROBE_CACHE["value"] is not None and now - _PROBE_CACHE["at"] < 60:
        return _PROBE_CACHE["value"]
    gp = gpu_probe(app_root, cfg)
    cpu_py = env_python(cfg, cfg["sa3_cpu_env"])
    cpu_script = Path(cfg["stable_audio_repo"]) / "optimized" / "tflite" / "scripts" / "sa3_tflite.py"
    cpu = bool(cpu_py and cpu_script.exists())
    ro = bool(env_exe(cfg, cfg["roformer_env"], "melband-roformer-infer.exe") or env_path(cfg, cfg["roformer_env"]))
    value = {
        "gpu": gp,
        "cpu": {"available": cpu, "reason": "SA3 Medium TFLite/XNNPACK (slow fallback)" if cpu else "CPU backend missing"},
        "roformer": {"available": ro, "reason": "MelBand RoFormer CUDA ready" if ro else "roformer_sep missing"},
    }
    _PROBE_CACHE.update(at=now, value=value)
    return value


def link_or_copy(src: Path, dst: Path):
    """Prefer an NTFS hard-link so large WAVs are not copied again."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def file_sha256(path: Path, chunk_bytes: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while True:
            b = f.read(chunk_bytes)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _cache_dir(app_root: Path, key: str) -> Path:
    return Path(app_root) / "runtime" / "cache" / "stems_v2" / key


def _cache_paths(cache_dir: Path):
    return {
        "full": cache_dir / "ORIGINAL_FULL.wav",
        "protected": cache_dir / "PROTECTED_VOCALS_FULL.wav",
        "instrumental": cache_dir / "ORIGINAL_INSTRUMENTAL_FULL.wav",
        "meta": cache_dir / "meta.json",
    }


def _cache_valid(cache_dir: Path) -> bool:
    q = _cache_paths(cache_dir)
    return all(q[k].exists() and q[k].stat().st_size > 0 for k in ("full", "protected", "instrumental"))


def _store_stem_cache(cache_dir: Path, full: Path, protected: Path, instrumental: Path, duration: float):
    cache_dir.mkdir(parents=True, exist_ok=True)
    q = _cache_paths(cache_dir)
    for src, key in ((full, "full"), (protected, "protected"), (instrumental, "instrumental")):
        tmp = q[key].with_suffix(q[key].suffix + ".tmp")
        if tmp.exists():
            tmp.unlink()
        try:
            os.link(src, tmp)
        except OSError:
            shutil.copy2(src, tmp)
        os.replace(tmp, q[key])
    q["meta"].write_text(json.dumps({"duration": duration, "created_at": time.time()}, indent=2), encoding="utf-8")
    try:
        os.utime(cache_dir, None)
    except OSError:
        pass


def _prune_stem_cache(app_root: Path, keep: int = 6):
    root = Path(app_root) / "runtime" / "cache" / "stems_v2"
    if not root.exists():
        return
    dirs = [d for d in root.iterdir() if d.is_dir()]
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs[max(1, int(keep)):]:
        shutil.rmtree(d, ignore_errors=True)


def decode_full(input_path: Path, full_wav: Path):
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    r = subprocess.run(
        [ff, "-y", "-i", str(input_path), "-ar", "44100", "-ac", "2", "-c:a", "pcm_f32le", str(full_wav)],
        capture_output=True,
        text=True,
    )
    if r.returncode:
        raise ProcessingError("Could not decode uploaded audio.")


def audio_duration(path: Path):
    info = sf.info(str(path))
    return float(info.frames) / float(info.samplerate)


def build_microphone_backing_mix(
    mic_path: Path,
    backing_path: Path,
    out_path: Path,
    mic_gain_db: float = 0.0,
    backing_gain_db: float = -8.0,
    loop_backing: bool = True,
):
    """Create the exact source that the downstream separator/AI will hear.

    Both inputs are decoded by ffmpeg to 44.1 kHz stereo float. The backing is
    cropped or tiled to the microphone take, gains are applied in float32, and
    one transparent *static* headroom correction is used only when required.
    There is no realtime compressor/limiter here, so the source presented to
    RoFormer/SA3 has no pumping or clipped samples.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_mic = out_path.with_name(out_path.stem + "_MIC.wav")
    tmp_back = out_path.with_name(out_path.stem + "_BACKING.wav")
    decode_full(mic_path, tmp_mic)
    decode_full(backing_path, tmp_back)
    mic, sr = sf.read(tmp_mic, always_2d=True, dtype="float32")
    back, bsr = sf.read(tmp_back, always_2d=True, dtype="float32")
    if bsr != sr:
        g = math.gcd(int(bsr), int(sr))
        back = resample_poly(back, sr // g, bsr // g, axis=0).astype(np.float32)
    if mic.shape[1] == 1:
        mic = np.repeat(mic, 2, axis=1)
    if back.shape[1] == 1:
        back = np.repeat(back, 2, axis=1)
    mic = mic[:, :2]
    back = back[:, :2]
    n = len(mic)
    if n <= 0:
        raise ProcessingError("Microphone recording is empty.")
    if len(back) <= 0:
        raise ProcessingError("Backing melody is empty.")
    if len(back) < n:
        if loop_backing:
            reps = int(math.ceil(n / len(back)))
            back = np.tile(back, (reps, 1))[:n]
        else:
            back = np.pad(back, ((0, n - len(back)), (0, 0)))
    else:
        back = back[:n]

    mg = 10.0 ** (float(mic_gain_db) / 20.0)
    bg = 10.0 ** (float(backing_gain_db) / 20.0)
    mixed = mic.astype(np.float64) * mg + back.astype(np.float64) * bg

    # Tiny equal-power edge ramps eliminate MediaRecorder/container boundary
    # discontinuities without touching the musical body of the take.
    fade_n = min(int(sr * 0.008), max(0, n // 4))
    if fade_n > 1:
        t = np.linspace(0.0, 1.0, fade_n, endpoint=True)
        ramp = np.sin(t * np.pi * 0.5) ** 2
        mixed[:fade_n] *= ramp[:, None]
        mixed[-fade_n:] *= ramp[::-1, None]

    raw_peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
    ceiling = 10.0 ** (-1.0 / 20.0)  # -1 dBFS source headroom
    safety_gain = min(1.0, ceiling / max(raw_peak, 1e-12))
    mixed *= safety_gain
    mixed = np.ascontiguousarray(mixed, dtype=np.float32)
    sf.write(out_path, mixed, sr, subtype="FLOAT")
    for tmp in (tmp_mic, tmp_back):
        try:
            tmp.unlink()
        except OSError:
            pass
    return {
        "sample_rate": int(sr),
        "duration": float(n) / float(sr),
        "input_peak": raw_peak,
        "safety_gain_db": 20.0 * math.log10(max(safety_gain, 1e-12)),
        "mic_gain_db": float(mic_gain_db),
        "backing_gain_db": float(backing_gain_db),
        "loop_backing": bool(loop_backing),
    }


def match(path, target_sr, n, ch):
    x, sr = sf.read(path, always_2d=True, dtype="float32")
    if sr != target_sr:
        g = math.gcd(sr, target_sr)
        x = resample_poly(x, target_sr // g, sr // g, axis=0).astype(np.float32)
    if x.shape[1] == 1 and ch == 2:
        x = np.repeat(x, 2, axis=1)
    if x.shape[1] > ch:
        x = x[:, :ch]
    if len(x) < n:
        x = np.pad(x, ((0, n - len(x)), (0, 0)))
    return x[:n]


def match_strict(path, target_sr, n, ch, label="edited stem"):
    x, sr = sf.read(path, always_2d=True, dtype="float32")
    if sr != target_sr:
        g = math.gcd(sr, target_sr)
        x = resample_poly(x, target_sr // g, sr // g, axis=0).astype(np.float32)
    if x.shape[1] == 1 and ch == 2:
        x = np.repeat(x, 2, axis=1)
    if x.shape[1] > ch:
        x = x[:, :ch]
    tolerance = max(int(target_sr * 0.35), int(n * 0.005))
    if abs(len(x) - n) > tolerance:
        raise ProcessingError(
            f"{label} length mismatch: got {len(x)/target_sr:.2f}s, expected {n/target_sr:.2f}s. "
            "Refusing to hide the missing tail with silence."
        )
    if len(x) != n:
        # Tiny codec/rounding mismatches are edge-padded/cropped only; never a multi-second silent tail.
        if len(x) < n:
            pad = np.repeat(x[-1:], n - len(x), axis=0) if len(x) else np.zeros((n, ch), np.float32)
            x = np.concatenate([x, pad], axis=0)
        else:
            x = x[:n]
    return np.ascontiguousarray(x, dtype=np.float32)


def rms(x):
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2) + 1e-12))


def make_residual(full, vocal, residual, protected):
    src, sr = sf.read(full, always_2d=True, dtype="float32")
    v = match(vocal, sr, len(src), src.shape[1])
    i = src - v
    sf.write(residual, i, sr, subtype="FLOAT")
    sf.write(protected, v, sr, subtype="FLOAT")
    return float(np.max(np.abs((v + i) - src)))


def roformer_fullsong(cfg, full_wav: Path, sep_dir: Path, stems: Path, log: Path):
    link_or_copy(full_wav, sep_dir / "song.wav")
    direct = env_exe(cfg, cfg["roformer_env"], "melband-roformer-infer.exe")
    if direct:
        cmd = [str(direct), "--input_folder", str(sep_dir), "--store_dir", str(stems), "--model", "melband-roformer-kim-vocals", "--device", "cuda"]
    else:
        py = env_python(cfg, cfg["roformer_env"])
        if not py:
            raise ProcessingError("roformer_sep environment not found.")
        cmd = [str(py), "-m", "melband_roformer_infer", "--input_folder", str(sep_dir), "--store_dir", str(stems), "--model", "melband-roformer-kim-vocals", "--device", "cuda"]
    run(cmd, log)


def sa3_gpu_fullsong(app_root, cfg, tasks, speed_mode, seed, log, progress):
    py = env_python(cfg, cfg["sa3_gpu_env"])
    if not py:
        raise ProcessingError("stableaudio3_gpu environment not found.")
    steps = {"turbo": 4, "balanced": 6, "quality": 8}.get(speed_mode, 4)
    manifest = log.parent / "sa3_manifest.json"
    data = {
        "model": "medium",
        "steps": steps,
        "seed": int(seed),
        "direct_max_seconds": float(cfg.get("gpu_direct_max_seconds", 300)),
        "chunk_seconds": float(cfg.get("gpu_chunk_seconds", 150)),
        "overlap_seconds": float(cfg.get("gpu_chunk_overlap_seconds", 6)),
        "fallback_chunk_seconds": cfg.get("gpu_fallback_chunk_seconds", [150, 120, 90, 60]),
        "prefer_unchunked_decode": bool(cfg.get("gpu_prefer_unchunked_decode", True)),
        "tasks": tasks,
    }
    manifest.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        run_persistent_gpu_worker(cfg, data, log, progress)
    except PersistentWorkerUnavailable as persistent_error:
        # Emergency compatibility fallback only. Normal operation never reaches
        # this branch: app.py keeps the persistent engine alive/restarts it.
        log_line(log, f"Persistent engine unavailable; legacy one-shot fallback: {persistent_error}")
        progress("SA3 engine recovery", 35, "Persistent engine unavailable; using one-shot recovery for this edit.")
        run_gpu_worker([str(py), str(app_root / "workers" / "sa3_fullsong_worker.py"), str(manifest)], log, progress)


def sa3_cpu(cfg, inp, out, prompt, neg, noise, duration, steps, seed, log):
    py = env_python(cfg, cfg["sa3_cpu_env"])
    script = Path(cfg["stable_audio_repo"]) / "optimized" / "tflite" / "scripts" / "sa3_tflite.py"
    if not py or not script.exists():
        raise ProcessingError("CPU backend unavailable.")
    run([
        str(py), str(script), "--prompt", prompt, "--negative-prompt", neg, "--cfg", "1.0", "--dit", "medium", "--decoder", "same-l",
        "--init-audio", str(inp), "--init-noise-level", str(noise), "--seconds", str(duration), "--steps", str(steps),
        "--seed", str(seed), "--threads", "8", "--out", str(out)
    ], log, cwd=Path(cfg["stable_audio_repo"]))



def _ola_parts(parts, starts, total_samples):
    if not parts:
        raise ProcessingError("No generated chunks to stitch.")
    ch = parts[0].shape[1]
    out = np.zeros((total_samples, ch), dtype=np.float32)
    weights = np.zeros((total_samples, 1), dtype=np.float32)
    for i, (start, part) in enumerate(zip(starts, parts)):
        end = min(total_samples, start + len(part))
        part = part[:end-start]
        w = np.ones((len(part), 1), dtype=np.float32)
        if i > 0:
            prev_end = min(total_samples, starts[i-1] + len(parts[i-1]))
            ov = min(max(0, prev_end - start), len(part))
            if ov:
                theta = np.linspace(0.0, math.pi/2.0, ov, dtype=np.float32)[:, None]
                w[:ov] = np.sin(theta) ** 2
        if i + 1 < len(parts):
            next_start = starts[i+1]
            ov = min(max(0, end - next_start), len(part))
            if ov:
                theta = np.linspace(0.0, math.pi/2.0, ov, dtype=np.float32)[:, None]
                w[-ov:] = np.minimum(w[-ov:], np.cos(theta) ** 2)
        out[start:end] += part * w
        weights[start:end] += w
    if float(weights.min()) <= 1e-6:
        raise ProcessingError("Chunk stitching left uncovered samples.")
    out /= weights
    return np.ascontiguousarray(out, dtype=np.float32)


def sa3_cpu_fullsong_safe(cfg, inp, out, prompt, neg, noise, steps, seed, log, progress, stage):
    """Reliable CPU fallback. Slower, but never requests a multi-minute TFLite generation."""
    x, sr = sf.read(inp, always_2d=True, dtype="float32")
    total = len(x)
    chunk_samples = int(30 * sr)
    overlap_samples = int(4 * sr)
    hop = chunk_samples - overlap_samples
    if total <= chunk_samples:
        starts = [0]
    else:
        starts = list(range(0, total - chunk_samples + 1, hop))
        last = total - chunk_samples
        if starts[-1] != last:
            starts.append(last)
    temp_dir = log.parent / f"cpu_{stage.replace(' ', '_').lower()}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    parts = []
    for idx, start in enumerate(starts):
        end = min(total, start + chunk_samples)
        cin = temp_dir / f"in_{idx:03d}.wav"
        cout = temp_dir / f"out_{idx:03d}.wav"
        sf.write(cin, x[start:end], sr, subtype="FLOAT")
        progress(stage, 38 + int(48 * (idx / max(1, len(starts)))), f"CPU safe chunk {idx+1}/{len(starts)}")
        sa3_cpu(cfg, cin, cout, prompt, neg, noise, (end-start)/sr, steps, seed + idx, log)
        y = match_strict(cout, sr, end-start, x.shape[1], f"CPU generated chunk {idx+1}")
        parts.append(y)
    y = _ola_parts(parts, starts, total)
    sf.write(out, y, sr, subtype="FLOAT")


def remix(orig_v, orig_i, edit_v, edit_i, out):
    oi, sr = sf.read(orig_i, always_2d=True, dtype="float32")
    n, ch = len(oi), oi.shape[1]
    ov = match(orig_v, sr, n, ch)
    ei = match_strict(edit_i, sr, n, ch, "edited instrumental")
    if edit_v:
        ev = match_strict(edit_v, sr, n, ch, "edited vocal")
        gv = float(np.clip(rms(ov) / max(rms(ev), 1e-8), 0.5, 2.0))
        ev *= gv
    else:
        ev = ov.copy()
        gv = 1.0
    gi = float(np.clip(rms(oi) / max(rms(ei), 1e-8), 0.5, 2.0))
    ei *= gi
    y = ev + ei
    peak = float(np.max(np.abs(y)))
    if peak > 0.995:
        y *= 0.995 / peak
    sf.write(out, y, sr, subtype="PCM_24")
    return {"vocal_gain": gv, "instrumental_gain": gi, "pre_limit_peak": peak}



def waveform_peaks(path: Path, points: int = 1200):
    """Return a compact server-side waveform envelope for reliable browser rendering."""
    x, sr = sf.read(path, always_2d=True, dtype="float32")
    if len(x) == 0:
        return {"peaks": [], "duration": 0.0}
    mono = np.max(np.abs(x), axis=1)
    points = max(200, min(int(points), 2400))
    edges = np.linspace(0, len(mono), points + 1, dtype=np.int64)
    peaks = np.zeros(points, dtype=np.float32)
    for i in range(points):
        a, b = int(edges[i]), int(edges[i + 1])
        if b <= a:
            b = min(len(mono), a + 1)
        if a < len(mono):
            peaks[i] = float(np.max(mono[a:b])) if b > a else float(mono[a])
    # Robust normalization keeps one transient from flattening the whole display.
    scale = float(np.percentile(peaks, 99.0)) if len(peaks) else 1.0
    scale = max(scale, 1e-6)
    peaks = np.clip(peaks / scale, 0.0, 1.0)
    return {
        "peaks": [round(float(v), 4) for v in peaks],
        "duration": float(len(x)) / float(sr),
    }

def encode_mp3(wav: Path, mp3: Path):
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    r = subprocess.run([ff, "-y", "-i", str(wav), "-c:a", "libmp3lame", "-b:a", "320k", str(mp3)], capture_output=True, text=True)
    return mp3 if r.returncode == 0 else wav


def process_job(app_root, job_id, p, cfg, progress):
    job_t0 = time.perf_counter()
    d = app_root / "runtime" / "jobs" / job_id
    work = d / "work"
    results = d / "results"
    stems = work / "stems"
    sep = work / "separator_input"
    for x in [work, results, stems, sep]:
        x.mkdir(parents=True, exist_ok=True)
    log = d / "process.log"
    inp = Path(p["input_path"])
    backing_path = Path(p["backing_path"]) if p.get("backing_path") else None
    if backing_path:
        progress("Build voice + melody source", 3, "Combining microphone recording and backing melody with transparent anti-clip headroom.")
        combined = work / "MICROPHONE_WITH_BACKING.wav"
        mix_stats = build_microphone_backing_mix(
            inp, backing_path, combined,
            mic_gain_db=float(p.get("mic_gain_db", 0.0)),
            backing_gain_db=float(p.get("backing_gain_db", -8.0)),
            loop_backing=bool(p.get("backing_loop", True)),
        )
        log_line(log, "MIC_BACKING_MIX " + json.dumps(mix_stats, ensure_ascii=False))
        inp = combined

    full = work / "ORIGINAL_FULL.wav"
    protected = results / "PROTECTED_VOCALS_FULL.wav"
    original_i = results / "ORIGINAL_INSTRUMENTAL_FULL.wav"

    # Local stem/decode cache: repeated prompt/strength experiments on the same
    # exact source skip the expensive decode + RoFormer pass entirely.
    progress("Check local cache", 2, "Checking whether this song was already separated locally.")
    source_key = file_sha256(inp)
    cache_dir = _cache_dir(app_root, source_key)
    cache_hit = _cache_valid(cache_dir)

    probes = probe_backends(app_root, cfg)
    if cache_hit:
        q = _cache_paths(cache_dir)
        link_or_copy(q["full"], full)
        link_or_copy(q["protected"], protected)
        link_or_copy(q["instrumental"], original_i)
        duration = audio_duration(full)
        err = 0.0
        try:
            os.utime(cache_dir, None)
        except OSError:
            pass
        progress("Stem cache hit", 28, f"Reusing cached vocals/instruments for this {duration:.1f}s track.")
    else:
        progress("Decode full song", 4, "Decoding the complete track once at 44.1 kHz stereo.")
        decode_full(inp, full)
        duration = audio_duration(full)

        if not probes["roformer"]["available"]:
            raise ProcessingError("RoFormer backend not ready.")

        # SA3 Medium is already loaded for app-lifetime reuse. On an ~8 GB GPU
        # keeping it resident while RoFormer also loads can create unnecessary
        # VRAM pressure. Temporarily move the *same loaded model* to CPU RAM; no
        # checkpoint reload occurs. After separation it returns to CUDA.
        sa3_offloaded = False
        if bool(cfg.get("gpu_worker_offload_for_roformer", True)):
            h = persistent_worker_health(cfg, timeout=0.5)
            if h and h.get("ok") and h.get("device") == "cuda":
                progress("Free GPU for separation", 10, "Temporarily moving the loaded SA3 model to RAM for RoFormer.")
                ok, info = persistent_worker_control(cfg, "offload")
                sa3_offloaded = ok
                if not ok:
                    log_line(log, "SA3 offload warning: " + str(info))

        progress("Full-song vocal separation", 12, f"Separating vocals for the entire {duration:.1f}s track on CUDA.")
        try:
            roformer_fullsong(cfg, full, sep, stems, log)
        finally:
            if sa3_offloaded:
                progress("Restore audio engine", 25, "Moving the already-loaded SA3 model back to CUDA.")
                ok, info = persistent_worker_control(cfg, "activate")
                if not ok:
                    log_line(log, "SA3 reactivate warning: " + str(info))

        cands = sorted(stems.rglob("*_vocals.wav")) or sorted(stems.rglob("*vocals*.wav"))
        if not cands:
            raise ProcessingError("No vocal stem was produced.")

        progress("Build residual", 27, "Building exact instrumental residual: original - protected vocals.")
        # Write the exact residual directly to its result path; old v4 wrote it
        # once in work/ and then copied the entire WAV a second time.
        err = make_residual(full, cands[0], original_i, protected)
        _store_stem_cache(cache_dir, full, protected, original_i, duration)
        _prune_stem_cache(app_root, int(cfg.get("stem_cache_entries", 6)))

    sel = p["backend"]
    gpu = probes["gpu"]["available"]
    cpu = probes["cpu"]["available"]
    backend = "gpu" if (sel == "gpu" or (sel == "auto" and gpu)) else "cpu"
    if backend == "gpu" and not gpu:
        raise ProcessingError("CUDA selected but unavailable: " + probes["gpu"]["reason"])
    if backend == "cpu" and not cpu:
        raise ProcessingError("CPU backend unavailable.")

    target = p["prompt"]
    ei = results / "EDITED_INSTRUMENTAL_FULL.wav"
    ev = results / "EDITED_VOCALS_FULL.wav"
    steps = {"turbo": 4, "balanced": 6, "quality": 8}.get(p["speed_mode"], 4)

    # Hide source-waveform work behind GPU inference instead of doing it after.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="wave") as pool:
        source_waves = {
            "original": pool.submit(waveform_peaks, full),
            "protected_vocals": pool.submit(waveform_peaks, protected),
            "original_instrumental": pool.submit(waveform_peaks, original_i),
        }

        if backend == "gpu":
            tasks = [
                {
                    "name": "Instrumental edit",
                    "input": str(original_i),
                    "output": str(ei),
                    "prompt": target + ". Instrumental only. Preserve original melody, rhythm, tempo, harmony and musical structure.",
                    "negative_prompt": "vocals, singing, rapping, speech, spoken voice, vocal chops",
                    "noise": p["inst_noise"],
                    "cfg_scale": 1.0,
                    "seed": p["seed"],
                }
            ]
            if p["edit_vocals"]:
                tasks.append(
                    {
                        "name": "Vocal edit",
                        "input": str(protected),
                        "output": str(ev),
                        "prompt": target + ". Keep exact lyrics, timing, cadence, pronunciation, pitch contour and vocal identity.",
                        "negative_prompt": "changed lyrics, different words, new vocals, altered cadence, vocal chops, distorted speech",
                        "noise": p["vocal_noise"],
                        "cfg_scale": 1.0,
                        "seed": p["seed"] + 1000,
                    }
                )
            progress(
                "SA3 GPU fast path", 34,
                f"{steps} steps unchanged · trying one long SA3 pass before safe fallback chunks."
            )
            sa3_gpu_fullsong(app_root, cfg, tasks, p["speed_mode"], p["seed"], log, progress)
        else:
            progress("SA3 CPU", 38, "CPU fallback is active; full-song processing will be much slower than CUDA.")
            sa3_cpu_fullsong_safe(
                cfg, original_i, ei,
                target + ". Instrumental only. Preserve original melody, rhythm, tempo and structure.",
                "vocals, singing, rapping, speech", p["inst_noise"], steps, p["seed"], log, progress, "SA3 CPU"
            )
            if p["edit_vocals"]:
                progress("SA3 CPU vocal", 70, "Editing the complete vocal stem on CPU with safe chunks.")
                sa3_cpu_fullsong_safe(
                    cfg, protected, ev,
                    target + ". Preserve exact lyrics, timing, cadence and identity.",
                    "changed lyrics, different words, distorted speech", p["vocal_noise"], steps, p["seed"] + 1000, log, progress, "SA3 CPU vocal"
                )

        progress("Full-song remix", 91, "Level-matching edited stems and rebuilding the entire track.")
        final_wav = results / "MODIFIED_FULL.wav"
        stats = remix(protected, original_i, ev if ev.exists() else None, ei, final_wav)

        # Encode and waveform scans run concurrently on CPU/disk.
        progress("Finalize", 96, "Encoding playback file and building waveforms in parallel.")
        final_mp3 = results / "MODIFIED_FULL.mp3"
        encode_future = pool.submit(encode_mp3, final_wav, final_mp3)
        output_waves = {
            "modified": pool.submit(waveform_peaks, final_wav),
            "edited_instrumental": pool.submit(waveform_peaks, ei),
        }
        if ev.exists():
            output_waves["edited_vocals"] = pool.submit(waveform_peaks, ev)

        mod = encode_future.result()
        waveforms = {
            "original": source_waves["original"].result(),
            "protected_vocals": source_waves["protected_vocals"].result(),
            "original_instrumental": source_waves["original_instrumental"].result(),
            "modified": output_waves["modified"].result(),
            "edited_instrumental": output_waves["edited_instrumental"].result(),
            "edited_vocals": output_waves["edited_vocals"].result() if "edited_vocals" in output_waves else None,
        }

    rel = lambda x: str(Path(x).relative_to(d)).replace("\\", "/")
    return {
        "backend": backend,
        "duration_seconds": duration,
        "speed_mode": p["speed_mode"],
        "steps": steps,
        "cache_hit": cache_hit,
        "processing_seconds": round(time.perf_counter() - job_t0, 2),
        "reconstruction_max_error": err,
        "mix_stats": stats,
        "original_full": rel(full),
        "modified_full": rel(mod),
        "modified_full_wav": rel(final_wav),
        "protected_vocals": rel(protected),
        "original_instrumental": rel(original_i),
        "edited_instrumental": rel(ei),
        "edited_vocals": rel(ev) if ev.exists() else None,
        "waveforms": waveforms,
    }
