from __future__ import annotations
import argparse
import json
import math
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from stable_audio_3 import StableAudioModel


class GenerationLengthError(RuntimeError):
    pass


class SuspiciousSilenceError(RuntimeError):
    pass


def emit(stage: str, progress: float, message: str, **extra):
    payload = {"stage": stage, "progress": float(progress), "message": message, **extra}
    print("PROGRESS " + json.dumps(payload, ensure_ascii=False), flush=True)


def read_audio(path: str | Path):
    x, sr = sf.read(str(path), always_2d=True, dtype="float32")
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    elif x.shape[1] > 2:
        x = x[:, :2]
    return np.ascontiguousarray(x), int(sr)


def tensor_from_np(x: np.ndarray) -> torch.Tensor:
    return torch.from_numpy(np.ascontiguousarray(x.T))


def _resample_length_linear(y: np.ndarray, target_samples: int) -> np.ndarray:
    if len(y) == target_samples:
        return np.ascontiguousarray(y, dtype=np.float32)
    if len(y) < 2:
        raise GenerationLengthError(f"Generated audio is empty/too short: {len(y)} samples")
    src_pos = np.linspace(0.0, 1.0, len(y), dtype=np.float64)
    dst_pos = np.linspace(0.0, 1.0, target_samples, dtype=np.float64)
    out = np.empty((target_samples, y.shape[1]), dtype=np.float32)
    for c in range(y.shape[1]):
        out[:, c] = np.interp(dst_pos, src_pos, y[:, c]).astype(np.float32)
    return np.ascontiguousarray(out)


def np_from_output(audio, target_samples: int, sr: int) -> np.ndarray:
    if isinstance(audio, (tuple, list)):
        audio = audio[0]
    if not torch.is_tensor(audio):
        audio = torch.as_tensor(audio)
    while audio.ndim > 2:
        audio = audio[0]
    if audio.ndim == 1:
        audio = audio.unsqueeze(0)
    if audio.shape[0] > 2:
        audio = audio[:2]
    if audio.shape[0] == 1:
        audio = audio.repeat(2, 1)
    y = audio.detach().float().cpu().numpy().T
    if not np.isfinite(y).all():
        raise RuntimeError("Stable Audio returned NaN/Inf samples")

    # Never hide a truncated long generation with zero-padding.
    delta = target_samples - len(y)
    tolerance = max(int(0.35 * sr), int(target_samples * 0.005))
    if abs(delta) > tolerance:
        raise GenerationLengthError(
            f"Stable Audio returned {len(y)/sr:.2f}s for a {target_samples/sr:.2f}s request "
            f"(difference {abs(delta)/sr:.2f}s)."
        )
    return _resample_length_linear(np.ascontiguousarray(y, dtype=np.float32), target_samples)


def _window_rms(x: np.ndarray, sr: int, seconds: float = 1.0) -> np.ndarray:
    n = max(1, int(sr * seconds))
    vals = []
    for start in range(0, len(x), n):
        z = x[start:start+n]
        if len(z):
            vals.append(float(np.sqrt(np.mean(z.astype(np.float64) ** 2) + 1e-15)))
    return np.asarray(vals, dtype=np.float64)


def detect_suspicious_silence(source: np.ndarray, edited: np.ndarray, sr: int) -> None:
    a = _window_rms(source, sr, 1.0)
    b = _window_rms(edited, sr, 1.0)
    m = min(len(a), len(b))
    if not m:
        return
    a, b = a[:m], b[:m]
    bad = (a > 3e-4) & (b < np.maximum(2e-6, a * 0.002))
    run = 0
    for flag in bad:
        run = run + 1 if flag else 0
        if run >= 3:
            raise SuspiciousSilenceError("Generated chunk contains >=3s of unexpected near-silence")


def generate_one(model, x: np.ndarray, sr: int, task: dict, steps: int, seed: int,
                 chunked_decode: bool):
    duration = len(x) / float(sr)
    kwargs = dict(
        init_audio=(sr, tensor_from_np(x)),
        init_noise_level=float(task["noise"]),
        prompt=task["prompt"],
        duration=duration,
        steps=int(steps),
        cfg_scale=float(task.get("cfg_scale", 1.0)),
        seed=int(seed),
        batch_size=1,
        chunked_decode=bool(chunked_decode),
    )
    neg = task.get("negative_prompt")
    if neg:
        kwargs["negative_prompt"] = neg
    # v31.29 VocalWeave continuity: the first `inpaint_keep_seconds` of the input are KNOWN audio (the previous
    # window's own output); the model conditions on them (inpaint mask 1 = keep) and generates the rest, which the
    # init audio still anchors to the record (img2img).  Absent -> the Home tab's plain edit, unchanged.
    if task.get("duration_padding_sec") is not None:
        kwargs["duration_padding_sec"] = float(task["duration_padding_sec"])       # v31.30.1: less padding = less compute per call
    keep = float(task.get("inpaint_keep_seconds", 0.0) or 0.0)
    if keep > 0.0 and keep < duration:
        kwargs["inpaint_audio"] = (sr, tensor_from_np(x))
        kwargs["inpaint_mask_start_seconds"] = keep
        _me = float(task.get("inpaint_mask_end_seconds", 0.0) or 0.0)   # v31.30.23: keep both sides when a bridge sets it
        kwargs["inpaint_mask_end_seconds"] = _me if (keep < _me < duration) else duration
    with torch.inference_mode():
        audio = model.generate(**kwargs)
    y = np_from_output(audio, len(x), sr)
    detect_suspicious_silence(x, y, sr)
    return y


def is_oom(exc: BaseException) -> bool:
    s = str(exc).lower()
    return "out of memory" in s or "cuda error: out of memory" in s


def generate_fast_safe(model, x: np.ndarray, sr: int, task: dict, steps: int, seed: int,
                       prefer_unchunked: bool = True):
    """Use the fastest decoder path first, then lower-VRAM decode on OOM only."""
    modes = [False, True] if prefer_unchunked else [True]
    last = None
    for chunked_decode in modes:
        try:
            return generate_one(model, x, sr, task, steps, seed, chunked_decode=chunked_decode)
        except (GenerationLengthError, SuspiciousSilenceError):
            # Decoder mode cannot repair a genuinely truncated/silent generation.
            raise
        except RuntimeError as e:
            last = e
            if not is_oom(e):
                raise
            emit(task["name"], 0, "VRAM limit reached; retrying with chunked SAME decode")
            torch.cuda.empty_cache()
    raise last if last else RuntimeError("Stable Audio generation failed")


def build_starts(total_samples: int, chunk_samples: int, overlap_samples: int) -> list[int]:
    if total_samples <= chunk_samples:
        return [0]
    hop = max(1, chunk_samples - overlap_samples)
    starts = list(range(0, total_samples - chunk_samples + 1, hop))
    last = total_samples - chunk_samples
    if not starts or starts[-1] != last:
        starts.append(last)
    return starts


def edit_chunked(model, x: np.ndarray, sr: int, task: dict, steps: int, seed: int,
                 chunk_seconds: float, overlap_seconds: float, base0: float, span: float,
                 prefer_unchunked: bool):
    total_samples = len(x)
    chunk_samples = max(int(chunk_seconds * sr), int(20 * sr))
    overlap_samples = max(0, min(int(overlap_seconds * sr), chunk_samples // 4))
    starts = build_starts(total_samples, chunk_samples, overlap_samples)
    parts: list[np.ndarray] = []

    for i, start in enumerate(starts):
        end = min(total_samples, start + chunk_samples)
        chunk = x[start:end]
        pct = base0 + span * (i / max(len(starts), 1))
        emit(task["name"], pct, f"Fast chunk {i+1}/{len(starts)} · {(end-start)/sr:.1f}s")
        y = generate_fast_safe(
            model, chunk, sr, task, steps, seed + i,
            prefer_unchunked=prefer_unchunked,
        )
        parts.append(y)
        # Do NOT empty the CUDA allocator after every successful chunk. Keeping
        # allocations cached avoids repeated synchronization/reallocation.

    if len(parts) == 1:
        return parts[0]

    out = np.zeros((total_samples, 2), dtype=np.float32)
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
        raise RuntimeError("Chunk stitching left uncovered samples")
    out /= weights
    detect_suspicious_silence(x, out, sr)
    return np.ascontiguousarray(out, dtype=np.float32)


def edit_long(model, task: dict, cfg: dict, task_index: int, task_count: int):
    x, sr = read_audio(task["input"])
    duration = len(x) / float(sr)
    steps = int(cfg["steps"])
    seed = int(task.get("seed", cfg.get("seed", 1337)))
    direct_max = min(380.0, float(cfg.get("direct_max_seconds", 300)))
    chunk_seconds = float(cfg.get("chunk_seconds", 150))
    overlap_seconds = float(cfg.get("overlap_seconds", 6))
    fallback = cfg.get("fallback_chunk_seconds", [chunk_seconds, 120.0, 90.0, 60.0])
    prefer_unchunked = bool(cfg.get("prefer_unchunked_decode", True))
    base0 = 12.0 + (task_index / max(task_count, 1)) * 84.0
    span = 84.0 / max(task_count, 1)

    # FAST PATH: one model.generate() for the whole song. v4's strict length and
    # silence guards remain, so we can safely try this without reintroducing the
    # old zero-padded tail bug.
    if duration <= direct_max:
        emit(task["name"], base0, f"Fast one-pass {duration:.1f}s edit · {steps} steps")
        try:
            y = generate_fast_safe(
                model, x, sr, task, steps, seed,
                prefer_unchunked=prefer_unchunked,
            )
            sf.write(task["output"], y, sr, subtype="FLOAT")
            emit(task["name"], base0 + span, f"{task['name']} complete · one-pass")
            return
        except (RuntimeError, GenerationLengthError, SuspiciousSilenceError) as e:
            if isinstance(e, RuntimeError) and not (is_oom(e) or isinstance(e, (GenerationLengthError, SuspiciousSilenceError))):
                raise
            emit(task["name"], base0 + 1, f"One-pass rejected ({type(e).__name__}); using safe long chunks")
            torch.cuda.empty_cache()

    # Fallback still uses much longer chunks than old v4 (150/120/90 before 60).
    attempts = []
    for value in [chunk_seconds, *fallback, 60.0]:
        value = float(value)
        if value not in attempts:
            attempts.append(value)

    last_error = None
    for attempt_i, seconds in enumerate(attempts):
        try:
            if attempt_i:
                emit(task["name"], base0 + 1, f"Retrying with {seconds:.0f}s chunks")
            y = edit_chunked(
                model, x, sr, task, steps, seed,
                chunk_seconds=seconds,
                overlap_seconds=min(overlap_seconds, max(3.0, seconds * 0.06)),
                base0=base0,
                span=span,
                prefer_unchunked=prefer_unchunked,
            )
            sf.write(task["output"], y, sr, subtype="FLOAT")
            emit(task["name"], base0 + span, f"{task['name']} complete · {seconds:.0f}s chunks")
            return
        except (RuntimeError, GenerationLengthError, SuspiciousSilenceError) as e:
            last_error = e
            if isinstance(e, RuntimeError) and not (is_oom(e) or isinstance(e, (GenerationLengthError, SuspiciousSilenceError))):
                raise
            torch.cuda.empty_cache()

    raise RuntimeError(f"Stable Audio failed all safe chunk sizes: {last_error}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    args = ap.parse_args()
    cfg = json.loads(Path(args.manifest).read_text(encoding="utf-8"))

    torch.set_grad_enabled(False)
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    torch.backends.cudnn.benchmark = True
    try:
        torch.set_float32_matmul_precision("high")
    except Exception:
        pass

    emit("Load model", 2, "Loading Stable Audio 3 Medium once for the whole job")
    t0 = time.time()
    model = StableAudioModel.from_pretrained(cfg.get("model", "medium"), device="cuda")
    emit("Load model", 10, f"Model ready in {time.time()-t0:.1f}s")

    tasks = cfg.get("tasks", [])
    if not tasks:
        raise RuntimeError("No edit tasks in manifest")
    for idx, task in enumerate(tasks):
        edit_long(model, task, cfg, idx, len(tasks))

    emit("Done", 100, "Stable Audio edits complete")


if __name__ == "__main__":
    main()
