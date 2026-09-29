from __future__ import annotations
import faulthandler
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

from processor import process_job, probe_backends
from gpu_engine import PersistentGPUWorker
from library_store import LibraryStore, COLLECTIONS
from cover_cache import CoverCache
from realtime_native_engine import NativeRealtimeEngine
from agentic_live_dj import RealtimeAgenticDJ

ROOT = Path(__file__).resolve().parent
JOBS_ROOT = ROOT / "runtime" / "jobs"
JOBS_ROOT.mkdir(parents=True, exist_ok=True)
# Keep a native-fault trace open for the whole server lifetime. On Windows,
# faulthandler can record access violations that bypass Python exceptions.
_BACKEND_FATAL_LOG = ROOT / "runtime" / "backend_fatal.log"
_BACKEND_FATAL_HANDLE = open(_BACKEND_FATAL_LOG, "a", encoding="utf-8", buffering=1)
try:
    faulthandler.enable(file=_BACKEND_FATAL_HANDLE, all_threads=True)
except Exception:
    pass
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024
JOBS = {}
LOCK = threading.Lock()
ALLOWED = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".webm"}
GPU_ENGINE = PersistentGPUWorker(ROOT, CONFIG)
LIBRARY = LibraryStore(ROOT)
COVER_CACHE = CoverCache(ROOT)
REALTIME_ENGINE = NativeRealtimeEngine(ai_host=CONFIG.get("realtime_model_host", "127.0.0.1"), ai_port=CONFIG.get("realtime_model_port", 8767))
AGENTIC_DJ = RealtimeAgenticDJ(REALTIME_ENGINE)
_SEMANTIC_HANDOFF_LOCK = threading.Lock()
_SEMANTIC_HANDOFF_THREAD = None
_SEMANTIC_DESIRED_DEVICE = "cpu"

_AGENTIC_START_LOCK = threading.Lock()
_AGENTIC_ERROR_LOG = ROOT / "runtime" / "agentic_start_error.log"

# v31.1.2: NEVER enumerate WASAPI/PyCAW inside the Flask server process.
# Native audio enumeration can terminate the interpreter on some Windows driver
# stacks (ACCESS_VIOLATION/COM faults), which the browser only reports as
# "Failed to fetch". A short-lived worker owns enumeration; if that native
# worker dies, the web server and live engine stay alive.
_DEVICE_SCAN_LOCK = threading.Lock()
_DEVICE_SCAN_CACHE = None
_DEVICE_SCAN_CACHE_AT = 0.0
_DEVICE_SCAN_CACHE_TTL = 1.5
_DEVICE_SCAN_TIMEOUT_SEC = 10.0
_DEVICE_SCAN_ERROR_LOG = ROOT / "runtime" / "device_scan_error.log"

def _safe_device_scan(force: bool = False):
    global _DEVICE_SCAN_CACHE, _DEVICE_SCAN_CACHE_AT
    now = time.monotonic()
    if (not force) and _DEVICE_SCAN_CACHE is not None and (now - _DEVICE_SCAN_CACHE_AT) < _DEVICE_SCAN_CACHE_TTL:
        return dict(_DEVICE_SCAN_CACHE)
    with _DEVICE_SCAN_LOCK:
        now = time.monotonic()
        if (not force) and _DEVICE_SCAN_CACHE is not None and (now - _DEVICE_SCAN_CACHE_AT) < _DEVICE_SCAN_CACHE_TTL:
            return dict(_DEVICE_SCAN_CACHE)
        runtime = ROOT / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        result_path = runtime / f"device_scan_{uuid.uuid4().hex}.json"
        worker = ROOT / "device_scan_worker.py"
        cmd = [sys.executable, str(worker), str(result_path), str(CONFIG.get("realtime_model_host", "127.0.0.1")), str(int(CONFIG.get("realtime_model_port", 8767)))]
        try:
            cp = subprocess.run(
                cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=_DEVICE_SCAN_TIMEOUT_SEC, creationflags=(0x08000000 if os.name == "nt" else 0),
            )
            payload = None
            if result_path.exists():
                try:
                    payload = json.loads(result_path.read_text(encoding="utf-8"))
                except Exception:
                    payload = None
            if cp.returncode == 0 and isinstance(payload, dict) and isinstance(payload.get("outputs"), list):
                payload["scan_isolated"] = True
                payload["scan_cached"] = False
                _DEVICE_SCAN_CACHE = dict(payload)
                _DEVICE_SCAN_CACHE_AT = time.monotonic()
                return payload
            detail = (cp.stderr or cp.stdout or f"device scan worker exited {cp.returncode}").strip()[-8000:]
            raise RuntimeError(detail or f"device scan worker exited {cp.returncode}")
        except Exception as exc:
            try:
                _DEVICE_SCAN_ERROR_LOG.write_text(traceback.format_exc(), encoding="utf-8")
            except Exception:
                pass
            if _DEVICE_SCAN_CACHE is not None:
                fallback = dict(_DEVICE_SCAN_CACHE)
                fallback["scan_isolated"] = True
                fallback["scan_cached"] = True
                fallback["scan_warning"] = f"Native device rescan failed; using last known-good list: {exc}"
                return fallback
            return {
                "inputs": [], "outputs": [], "default_input_id": None, "default_output_id": None,
                "process_capture_error": str(exc), "scan_isolated": True, "scan_cached": False,
                "scan_warning": "Windows audio scan failed safely; JoyMetric server remained alive.",
            }
        finally:
            try:
                result_path.unlink(missing_ok=True)
            except Exception:
                pass


def _semantic_base():
    return f"http://{CONFIG.get('realtime_model_host','127.0.0.1')}:{int(CONFIG.get('realtime_model_port',8767))}"


def _semantic_set_device(device: str, timeout: float = 8.0):
    try:
        body = json.dumps({"device": str(device)}).encode("utf-8")
        req = urllib.request.Request(
            _semantic_base() + "/device", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _semantic_handoff_async(target: str):
    """Converge the out-of-band semantic critic to CPU/CUDA without thread storms.

    UI control posts can arrive many times per second. A single arbitration worker
    follows the latest desired device, so stale GPU-promote tasks cannot race a
    later Stop/CPU request or repeatedly offload the Stable Audio worker.
    """
    global _SEMANTIC_HANDOFF_THREAD, _SEMANTIC_DESIRED_DEVICE
    target = "cuda" if str(target).lower().startswith(("cuda", "gpu")) else "cpu"
    with _SEMANTIC_HANDOFF_LOCK:
        _SEMANTIC_DESIRED_DEVICE = target
        if _SEMANTIC_HANDOFF_THREAD and _SEMANTIC_HANDOFF_THREAD.is_alive():
            return

        def work():
            global _SEMANTIC_HANDOFF_THREAD
            try:
                while True:
                    with _SEMANTIC_HANDOFF_LOCK:
                        desired = _SEMANTIC_DESIRED_DEVICE
                    if desired == "cuda":
                        # Stable Audio gets parked in CPU RAM first. This avoids CLAP
                        # competing for VRAM while the resident generator is idle.
                        arb_deadline = time.time() + 35.0
                        while time.time() < arb_deadline:
                            with _SEMANTIC_HANDOFF_LOCK:
                                if _SEMANTIC_DESIRED_DEVICE != "cuda":
                                    break
                            h = GPU_ENGINE.status()
                            state = str(h.get("state") or "")
                            device = str(h.get("device") or "")
                            if h.get("ok"):
                                if device == "cuda":
                                    off = GPU_ENGINE.offload(timeout=35.0)
                                    if not off.get("ok"):
                                        time.sleep(0.8)
                                        continue
                                break
                            if state not in {"starting", "busy"}:
                                break
                            time.sleep(0.6)

                        with _SEMANTIC_HANDOFF_LOCK:
                            still_cuda = _SEMANTIC_DESIRED_DEVICE == "cuda"
                        if still_cuda:
                            deadline = time.time() + 45.0
                            while time.time() < deadline:
                                with _SEMANTIC_HANDOFF_LOCK:
                                    if _SEMANTIC_DESIRED_DEVICE != "cuda":
                                        break
                                result = _semantic_set_device("cuda", timeout=15.0)
                                if result.get("ok") and str(result.get("device") or "").startswith("cuda"):
                                    break
                                time.sleep(0.8)
                    else:
                        _semantic_set_device("cpu", timeout=10.0)

                    with _SEMANTIC_HANDOFF_LOCK:
                        if _SEMANTIC_DESIRED_DEVICE == desired:
                            _SEMANTIC_HANDOFF_THREAD = None
                            return
                    # Desired target changed while we were working; converge again.
            finally:
                with _SEMANTIC_HANDOFF_LOCK:
                    if _SEMANTIC_HANDOFF_THREAD is threading.current_thread():
                        _SEMANTIC_HANDOFF_THREAD = None

        _SEMANTIC_HANDOFF_THREAD = threading.Thread(
            target=work, daemon=True, name="joymetric-semantic-device-arbiter"
        )
        _SEMANTIC_HANDOFF_THREAD.start()


def _promote_realtime_semantic_gpu_async():
    # v26.1 audio-priority policy: realtime semantic analysis stays on CPU.
    # Keeping this compatibility hook avoids touching the API/UI call sites while
    # preventing CUDA model moves from racing the native WASAPI route.
    _semantic_handoff_async("cpu")


def _demote_realtime_semantic_async():
    _semantic_handoff_async("cpu")


def _clean_project_title(value, fallback="Untitled Track"):
    title = " ".join(str(value or "").split()).strip()
    return (title or fallback or "Untitled Track")[:160]


def _link_or_copy(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def update(job_id, **kw):
    with LOCK:
        s = JOBS.setdefault(job_id, {})
        s.update(kw)
        s["updated_at"] = time.time()
        d = JOBS_ROOT / job_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "status.json").write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")


def get_state(job_id):
    with LOCK:
        if job_id in JOBS:
            return dict(JOBS[job_id])
    p = JOBS_ROOT / job_id / "status.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def run_job(job_id, params):
    try:
        update(job_id, status="running", stage="Preparing", progress=2, message="Preparing full-song pipeline.")
        def progress(stage, pct, msg):
            update(job_id, status="running", stage=stage, progress=pct, message=msg)
        out = process_job(ROOT, job_id, params, CONFIG, progress)
        try:
            latest = get_state(job_id) or {}
            history_params = latest.get("params") or {k: v for k, v in params.items() if k not in {"input_path", "backing_path"}}
            history_state = {
                "params": history_params,
                "outputs": out,
            }
            LIBRARY.record_history(job_id, JOBS_ROOT / job_id, history_state)
        except Exception:
            (JOBS_ROOT / job_id / "library_warning.txt").write_text(traceback.format_exc(), encoding="utf-8")
        update(job_id, status="done", stage="Complete", progress=100, message="Full song ready.", outputs=out)
    except Exception as e:
        (JOBS_ROOT / job_id / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        update(job_id, status="error", stage="Failed", progress=100, message=str(e), error=str(e))


@app.get("/")
def index():
    return render_template("index.html", probes=probe_backends(ROOT, CONFIG))


@app.get("/api/realtime/devices")
def realtime_devices():
    # Device enumeration is isolated from Flask so a bad WASAPI/COM driver scan
    # cannot terminate the application process. A failed rescan is non-fatal.
    data = _safe_device_scan(force=request.args.get("force") == "1")
    code = 200 if (data.get("inputs") or data.get("outputs") or data.get("scan_cached")) else 503
    return jsonify(data), code


@app.post("/api/realtime/start")
def realtime_start():
    AGENTIC_DJ.stop()
    body = request.get_json(silent=True) or {}
    input_id = str(body.get("input_id", "")).strip()
    output_id = str(body.get("output_id", "")).strip()
    if not input_id or not output_id:
        return jsonify(error="Choose both Input Channel and Audio Out."), 400
    try:
        if bool(body.get("dj_mode", False)):
            _promote_realtime_semantic_gpu_async()
        status = REALTIME_ENGINE.start(
            input_id=input_id,
            output_id=output_id,
            params=body.get("params") or {},
            features=body.get("features") or {},
            ai_enabled=body.get("ai_enabled", True),
            dj_mode=body.get("dj_mode", False),
            dj_prompt=body.get("dj_prompt", ""),
            dsp50_mode=False,
            dsp50_prompt="",
            dsp50_intensity=1.0,
            effect_intensity=body.get("effect_intensity", 1.0),
            neural_polish_enabled=False,
            neural_polish_wet=0.0,
        )
        return jsonify(ok=True, status=status)
    except Exception as e:
        return jsonify(error=str(e), status=REALTIME_ENGINE.status()), 400


@app.post("/api/realtime/controls")
def realtime_controls():
    body = request.get_json(silent=True) or {}
    if body.get("dj_mode") is True:
        _promote_realtime_semantic_gpu_async()
    elif body.get("dj_mode") is False:
        _demote_realtime_semantic_async()
    REALTIME_ENGINE.set_controls(
        body.get("params") or {}, body.get("features") or {}, body.get("ai_enabled"),
        body.get("dj_mode"), body.get("dj_prompt"),
        False, "", 1.0, body.get("effect_intensity", 1.0),
        False, 0.0,
    )
    return jsonify(ok=True, status=REALTIME_ENGINE.status())


@app.post("/api/realtime/stop")
def realtime_stop():
    REALTIME_ENGINE.stop()
    _demote_realtime_semantic_async()
    return jsonify(ok=True, status=REALTIME_ENGINE.status())


@app.get("/api/realtime/status")
def realtime_status():
    return jsonify(REALTIME_ENGINE.status())


@app.get("/api/agentic/status")
def agentic_status():
    return jsonify(AGENTIC_DJ.status())


@app.get("/api/agentic/devices")
def agentic_devices():
    data = _safe_device_scan(force=request.args.get("force") == "1")
    spotify = next((x for x in (data.get("inputs") or []) if x.get("app_capture") and "spotify" in str(x.get("label") or "").lower()), None)
    return jsonify(
        outputs=data.get("outputs") or [],
        default_output_id=data.get("default_output_id"),
        spotify_ready=bool(spotify),
        spotify_label=(spotify or {}).get("label"),
        scan_cached=bool(data.get("scan_cached")),
        scan_warning=data.get("scan_warning") or "",
        process_capture_error=data.get("process_capture_error") or "",
    )


def _spotify_agentic_input_id():
    """Resolve Spotify robustly without requiring PyCAW to expose a session that instant.

    The managed JoyMetric route only needs a live Spotify process root. PyCAW is
    preferred because it points at the current audio session, but Windows can
    briefly recreate that session when playback starts/changes tracks. In that
    gap, fall back to the live Spotify.exe process tree instead of false-failing.
    """
    last_error = ""
    for _ in range(4):
        try:
            data = _safe_device_scan(force=False)
            rows = data.get("inputs") or []
            spotify = next((x for x in rows if x.get("app_capture") and "spotify" in str(x.get("label") or "").lower()), None)
            if spotify and str(spotify.get("id") or "").startswith("app:"):
                return str(spotify.get("id"))
            last_error = str(data.get("process_capture_error") or "")
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.18)

    try:
        import psutil
        candidates = []
        for proc in psutil.process_iter(["pid", "name", "create_time"]):
            try:
                name = str(proc.info.get("name") or "").lower()
                if name == "spotify.exe" or name == "spotify":
                    candidates.append((float(proc.info.get("create_time") or 0.0), int(proc.info["pid"])))
            except Exception:
                continue
        if candidates:
            candidates.sort()
            # Oldest Spotify.exe is normally the stable root; the router also
            # targets current children/PIDs when applying the Windows preference.
            return f"app:{candidates[0][1]}"
    except Exception as exc:
        last_error = str(exc) or last_error

    detail = f" ({last_error})" if last_error else ""
    raise RuntimeError("Spotify process was not found. Start Spotify playback, wait one second, then press LET'S GO." + detail)


def _agentic_require_live():
    st = AGENTIC_DJ.status()
    if not bool(st.get("running")):
        return jsonify(error="Press LET'S GO first. Agentic DJ controls are inactive until the Spotify route is live."), 409
    return None


@app.post("/api/agentic/start")
def agentic_start():
    body = request.get_json(silent=True) or {}
    output_id = str(body.get("output_id") or "").strip()
    prompt = str(body.get("prompt") or "").strip()
    autonomy = str(body.get("autonomy") or "autopilot").strip().lower()
    try:
        creativity = float(body.get("creativity", 0.35))
    except Exception:
        creativity = 0.35
    try:
        effect_intensity = float(body.get("effect_intensity", 1.0))
    except Exception:
        effect_intensity = 1.0
    if not output_id:
        return jsonify(error="Choose a physical Audio Out before LET'S GO."), 400
    if not _AGENTIC_START_LOCK.acquire(blocking=False):
        return jsonify(error="Agentic DJ is already starting. Do not double-click LET'S GO."), 409
    try:
        AGENTIC_DJ.stop()
        REALTIME_ENGINE.stop()
        # Pre-arm Agentic mode before the native audio thread is created so the
        # engine allocates the professional 15 s lookahead from sample zero.
        REALTIME_ENGINE.set_agentic_live_controls({"enabled": True}, enabled=True)
        input_id = _spotify_agentic_input_id()
        status = REALTIME_ENGINE.start(
            input_id=input_id,
            output_id=output_id,
            params={}, features={}, ai_enabled=True,
            dj_mode=True, dj_prompt=prompt,
            dsp50_mode=False, dsp50_prompt="", dsp50_intensity=1.0,
            effect_intensity=effect_intensity,
            neural_polish_enabled=False, neural_polish_wet=0.0,
        )
        if not bool((status or {}).get("running")):
            # NativeRealtimeEngine normally surfaces startup errors synchronously.
            # Never start the agent against a half-started/no-route engine.
            raise RuntimeError(str((status or {}).get("message") or "Spotify route did not become live."))
        AGENTIC_DJ.start(prompt, autonomy=autonomy, creativity=creativity)
        return jsonify(ok=True, phase="live", status=AGENTIC_DJ.status())
    except BaseException as exc:
        try:
            _AGENTIC_ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
            _AGENTIC_ERROR_LOG.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        try: AGENTIC_DJ.stop()
        except BaseException: pass
        try: REALTIME_ENGINE.stop()
        except BaseException: pass
        return jsonify(error=str(exc) or exc.__class__.__name__, status=AGENTIC_DJ.status()), 400
    finally:
        _AGENTIC_START_LOCK.release()


@app.get("/api/agentic/weave")
def agentic_weave_get():
    return jsonify(ok=True, **AGENTIC_DJ.weave_settings())


@app.post("/api/agentic/weave")
def agentic_weave_set():
    """v31.30: the Stable Audio temperature slider - live, no relaunch; pending windows are re-rendered."""
    body = request.get_json(silent=True) or {}
    res = {}
    if body.get("cfg") is not None:
        try:
            cfg = float(body.get("cfg"))
        except Exception:
            return jsonify(error="cfg must be a number"), 400
        if not (2.0 <= cfg <= 8.0):
            return jsonify(error="cfg must be within 2 .. 8"), 400
        res.update(AGENTIC_DJ.set_weave_cfg(cfg))
    if body.get("noise") is not None:
        try:
            noise = float(body.get("noise"))
        except Exception:
            return jsonify(error="noise must be a number"), 400
        if not (0.05 <= noise <= 0.9):
            return jsonify(error="noise must be within 0.05 .. 0.9"), 400
        res.update(AGENTIC_DJ.set_weave_noise(noise))
    if not res:
        return jsonify(error="noise or cfg required"), 400
    return jsonify(ok=True, **res)


@app.post("/api/agentic/prompt")
def agentic_prompt():
    blocked = _agentic_require_live()
    if blocked is not None:
        return blocked
    body = request.get_json(silent=True) or {}
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        return jsonify(error="Prompt cannot be empty."), 400
    creativity = body.get("creativity")
    autonomy = body.get("autonomy")
    try:
        creativity = None if creativity is None else float(creativity)
    except Exception:
        creativity = None
    policy = AGENTIC_DJ.set_prompt(prompt, autonomy=autonomy, creativity=creativity)
    try:
        fx = body.get("effect_intensity")
        fx = None if fx is None else float(fx)
    except Exception:
        fx = None
    # One shared prompt drives both the continuous Realtime 15D DJ and the
    # rolling-horizon Agentic performance layer without triggering manual mode.
    REALTIME_ENGINE.set_controls(
        params=None, features=None, ai_enabled=True, dj_mode=True, dj_prompt=prompt,
        dsp50_mode=False, dsp50_prompt="", dsp50_intensity=1.0, effect_intensity=fx,
        neural_polish_enabled=False, neural_polish_wet=0.0,
    )
    return jsonify(ok=True, policy=policy, status=AGENTIC_DJ.status())


@app.post("/api/agentic/macro")
def agentic_macro():
    return jsonify({"ok": False, "error": "DJ controller removed (v31.30.25)"}), 410
    blocked = _agentic_require_live()
    if blocked is not None:
        return blocked
    body = request.get_json(silent=True) or {}
    try:
        event = AGENTIC_DJ.trigger_macro(str(body.get("action") or ""))
        return jsonify(ok=True, event=event, status=AGENTIC_DJ.status())
    except Exception as e:
        return jsonify(error=str(e), status=AGENTIC_DJ.status()), 400


@app.post("/api/agentic/controls")
def agentic_controls():
    return jsonify({"ok": False, "error": "DJ controller removed (v31.30.25)"}), 410
    # v31.10: slider input is accepted even before LET'S GO. Values are staged
    # into the controller so the deck no longer "snaps back" when touched while
    # the route is not live yet; once live they apply normally as temporary
    # overrides (autopilot) or permanent values (manual mode).
    body = request.get_json(silent=True) or {}
    try:
        AGENTIC_DJ.manual_controls(body.get("controls") or body)
        st = AGENTIC_DJ.status()
        return jsonify(ok=True, staged=not bool(st.get("running")), status=st)
    except Exception as e:
        return jsonify(error=str(e), status=AGENTIC_DJ.status()), 400


@app.post("/api/agentic/approve")
def agentic_approve():
    return jsonify({"ok": False, "error": "DJ controller removed (v31.30.25)"}), 410
    blocked = _agentic_require_live()
    if blocked is not None:
        return blocked
    AGENTIC_DJ.approve()
    return jsonify(ok=True, status=AGENTIC_DJ.status())


@app.post("/api/agentic/lock")
def agentic_lock():
    return jsonify({"ok": False, "error": "DJ controller removed (v31.30.25)"}), 410
    blocked = _agentic_require_live()
    if blocked is not None:
        return blocked
    body = request.get_json(silent=True) or {}
    AGENTIC_DJ.lock(bool(body.get("locked", True)))
    return jsonify(ok=True, status=AGENTIC_DJ.status())


@app.post("/api/agentic/regenerate")
def agentic_regenerate():
    return jsonify({"ok": False, "error": "DJ controller removed (v31.30.25)"}), 410
    blocked = _agentic_require_live()
    if blocked is not None:
        return blocked
    AGENTIC_DJ.regenerate()
    return jsonify(ok=True, status=AGENTIC_DJ.status())


@app.post("/api/agentic/take-control")
def agentic_take_control():
    return jsonify({"ok": False, "error": "DJ controller removed (v31.30.25)"}), 410
    blocked = _agentic_require_live()
    if blocked is not None:
        return blocked
    AGENTIC_DJ.take_control()
    return jsonify(ok=True, status=AGENTIC_DJ.status())


@app.post("/api/agentic/loop")
def agentic_loop():
    return jsonify({"ok": False, "error": "DJ controller removed (v31.30.25)"}), 410
    blocked = _agentic_require_live()
    if blocked is not None:
        return blocked
    body = request.get_json(silent=True) or {}
    action = str(body.get("action") or "capture")
    if action == "release":
        AGENTIC_DJ.release_loop()
    else:
        AGENTIC_DJ.capture_loop(float(body.get("beats", 4.0)))
    return jsonify(ok=True, status=AGENTIC_DJ.status())


@app.post("/api/agentic/transport")
def agentic_transport():
    blocked = _agentic_require_live()
    if blocked is not None:
        return blocked
    body = request.get_json(silent=True) or {}
    result = AGENTIC_DJ.transport(str(body.get("action") or ""), body.get("value"))
    code = 200 if result.get("ok") else 400
    return jsonify(result), code


@app.post("/api/agentic/stop")
def agentic_stop():
    AGENTIC_DJ.stop()
    REALTIME_ENGINE.stop()
    _demote_realtime_semantic_async()
    return jsonify(ok=True, status=AGENTIC_DJ.status())


@app.get("/api/backends")
def backends():
    data = probe_backends(ROOT, CONFIG)
    data["gpu_engine"] = GPU_ENGINE.status()
    return jsonify(data)


@app.post("/api/jobs")
def create():
    f = request.files.get("audio")
    backing_f = request.files.get("backing_audio")
    reference_id = str(request.form.get("reference_id", "")).strip()

    reference_source = None
    if reference_id:
        reference_source = LIBRARY.preferred_asset_path("references", reference_id)
        if not reference_source:
            return jsonify(error="Reference Library track was not found."), 404
    elif not f or not f.filename:
        return jsonify(error="Choose an audio file, microphone recording, or Reference Library track."), 400

    try:
        vn = min(1, max(0, float(request.form.get("vocal_noise", "0.10"))))
        ino = min(1, max(0, float(request.form.get("inst_noise", "0.65"))))
        seed = int(request.form.get("seed", "1337"))
        mic_gain_db = min(6.0, max(-12.0, float(request.form.get("mic_gain_db", "0"))))
        backing_gain_db = min(6.0, max(-24.0, float(request.form.get("backing_gain_db", "-8"))))
    except ValueError:
        return jsonify(error="Invalid numeric setting."), 400
    backing_loop = request.form.get("backing_loop", "true").lower() == "true"
    prompt = request.form.get("prompt", "").strip() or "Dreamy atmospheric hip-hop, ethereal, spacious, lush and immersive."
    backend = request.form.get("backend", "auto")
    if backend not in {"auto", "gpu", "cpu"}:
        backend = "auto"
    speed_mode = request.form.get("speed_mode", "turbo")
    if speed_mode not in {"turbo", "balanced", "quality"}:
        speed_mode = "turbo"
    edit_vocals = request.form.get("edit_vocals", "false").lower() == "true"
    requested_title = " ".join(str(request.form.get("project_title", "") or "").split()).strip()[:160]

    job_id = uuid.uuid4().hex[:12]
    u = JOBS_ROOT / job_id / "upload"
    u.mkdir(parents=True, exist_ok=True)

    if reference_source:
        source_path, item, _asset_key = reference_source
        display_name = item.get("original_name") or item.get("title") or source_path.name or "Reference Track"
        ext = source_path.suffix.lower()
        if ext not in ALLOWED:
            return jsonify(error=f"Unsupported Reference Library type: {ext}"), 400
        stem = secure_filename(Path(display_name).stem) or "reference"
        name = f"{stem}{ext}"
        original_name = name
        path = u / name
        _link_or_copy(source_path, path)
        source_kind = "reference_library"
    else:
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED:
            return jsonify(error=f"Unsupported type: {ext}"), 400
        original_name = f.filename
        name = secure_filename(f.filename) or f"input{ext}"
        path = u / name
        f.save(path)
        source_kind = "microphone" if str(request.form.get("source_kind", "")) == "microphone" else "file"

    backing_path = None
    backing_name = None
    if source_kind == "microphone" and backing_f and backing_f.filename:
        backing_ext = Path(backing_f.filename).suffix.lower()
        if backing_ext not in ALLOWED:
            return jsonify(error=f"Unsupported backing melody type: {backing_ext}"), 400
        backing_name = secure_filename(backing_f.filename) or f"backing{backing_ext}"
        backing_path_obj = u / ("backing_" + backing_name)
        backing_f.save(backing_path_obj)
        backing_path = str(backing_path_obj)
        source_kind = "microphone_with_backing"

    params = dict(
        input_path=str(path),
        original_name=original_name,
        project_title=_clean_project_title(requested_title, Path(original_name).stem or original_name),
        source_kind=source_kind,
        source_reference_id=reference_id or None,
        backing_path=backing_path,
        backing_name=backing_name,
        mic_gain_db=mic_gain_db,
        backing_gain_db=backing_gain_db,
        backing_loop=backing_loop,
        prompt=prompt,
        vocal_noise=vn,
        inst_noise=ino,
        seed=seed,
        backend=backend,
        speed_mode=speed_mode,
        edit_vocals=edit_vocals,
    )
    update(
        job_id,
        id=job_id,
        status="queued",
        stage="Queued",
        progress=0,
        message="Queued for full-song processing.",
        params={k: v for k, v in params.items() if k not in {"input_path", "backing_path"}},
        outputs=None,
    )
    threading.Thread(target=run_job, args=(job_id, params), daemon=True).start()
    return jsonify(job_id=job_id)


@app.get("/api/jobs/<job_id>")
def status(job_id):
    s = get_state(job_id)
    return jsonify(s) if s else (jsonify(error="Job not found."), 404)


@app.put("/api/jobs/<job_id>/title")
def job_title_update(job_id):
    state = get_state(job_id)
    if not state:
        return jsonify(error="Job not found."), 404
    body = request.get_json(silent=True) or {}
    title = _clean_project_title(body.get("project_title", ""))
    params = dict(state.get("params") or {})
    params["project_title"] = title
    update(job_id, params=params)
    try:
        LIBRARY.update_job_title(job_id, title)
    except Exception:
        pass
    return jsonify(ok=True, project_title=title)


@app.get("/media/<job_id>/<path:name>")
def media(job_id, name):
    return send_from_directory(JOBS_ROOT / job_id, name, as_attachment=False)


@app.get("/download/<job_id>/<path:name>")
def download(job_id, name):
    return send_from_directory(JOBS_ROOT / job_id, name, as_attachment=True)


def _library_payload(item):
    if not item:
        return None
    data = dict(item)
    collection = data["collection"]
    item_id = data["id"]
    data["media"] = {
        key: f"/library-media/{collection}/{item_id}/{name}"
        for key, name in (data.get("assets") or {}).items()
    }
    data["downloads"] = {
        key: f"/library-download/{collection}/{item_id}/{name}"
        for key, name in (data.get("assets") or {}).items()
    }
    return data




@app.get("/api/cover-cache")
def cover_cache_lookup():
    source = str(request.args.get("source", "") or "").strip()
    key = str(request.args.get("key", "") or "").strip()
    path = COVER_CACHE.get_path(key=key or None, source=source or None)
    if not path:
        return jsonify(ok=False, cached=False), 404
    resolved_key = path.stem
    return jsonify(ok=True, cached=True, key=resolved_key, url=f"/cover-cache/{resolved_key}.png")


@app.post("/api/cover-cache")
def cover_cache_save():
    body = request.get_json(silent=True) or {}
    source = str(body.get("source", "") or "").strip()
    image_data = str(body.get("image_data", "") or "")
    if not source:
        return jsonify(error="Cover source is required."), 400
    if not image_data:
        return jsonify(error="Cover image data is required."), 400
    try:
        key, _path = COVER_CACHE.save_data_url(source, image_data)
        return jsonify(ok=True, cached=True, key=key, url=f"/cover-cache/{key}.png")
    except Exception as e:
        return jsonify(error=str(e)), 400


@app.get("/cover-cache/<key>.png")
def cover_cache_media(key):
    path = COVER_CACHE.get_path(key=key)
    if not path:
        return jsonify(error="Cover image not found."), 404
    return send_from_directory(path.parent, path.name, as_attachment=False)


@app.get("/api/library/<collection>")
def library_list(collection):
    if collection not in COLLECTIONS:
        return jsonify(error="Unknown library section."), 404
    try:
        items = [_library_payload(x) for x in LIBRARY.list(collection)]
        return jsonify(collection=collection, items=items)
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.post("/api/library/references/upload")
def reference_library_upload():
    f = request.files.get("audio")
    if not f or not f.filename:
        return jsonify(error="Choose an audio file to upload."), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED:
        return jsonify(error=f"Unsupported type: {ext}"), 400
    tmp_dir = ROOT / "runtime" / "reference_uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{uuid.uuid4().hex}{ext}"
    try:
        f.save(tmp)
        item = LIBRARY.import_reference(tmp, secure_filename(f.filename) or f"reference{ext}")
        return jsonify(ok=True, item=_library_payload(item))
    except Exception as e:
        return jsonify(error=str(e)), 400
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


@app.post("/api/library/save")
def library_save():
    body = request.get_json(silent=True) or {}
    collection = str(body.get("collection", "")).strip().lower()
    job_id = str(body.get("job_id", "")).strip()
    asset_kind = str(body.get("asset_kind", "modified")).strip().lower()
    project_title = " ".join(str(body.get("project_title", "") or "").split()).strip()[:160]
    if collection not in COLLECTIONS - {"history"}:
        return jsonify(error="Choose My Projects, My Audio, Favorites, or Reference Library."), 400
    state = get_state(job_id)
    if not state or state.get("status") != "done":
        return jsonify(error="Completed render not found."), 404
    try:
        if project_title:
            params = dict(state.get("params") or {})
            params["project_title"] = project_title
            update(job_id, params=params)
            state = get_state(job_id) or state
            LIBRARY.update_job_title(job_id, project_title)
        item = LIBRARY.save_from_job(collection, job_id, JOBS_ROOT / job_id, state, asset_kind, project_title or None)
        return jsonify(ok=True, item=_library_payload(item))
    except Exception as e:
        return jsonify(error=str(e)), 400


@app.delete("/api/library/<collection>/<item_id>")
def library_delete(collection, item_id):
    if collection not in COLLECTIONS:
        return jsonify(error="Unknown library section."), 404
    if not LIBRARY.delete(collection, item_id):
        return jsonify(error="Library item not found."), 404
    return jsonify(ok=True)


@app.get("/library-media/<collection>/<item_id>/<path:name>")
def library_media(collection, item_id, name):
    path = LIBRARY.asset_path(collection, item_id, name)
    if not path:
        return jsonify(error="Media not found."), 404
    return send_from_directory(path.parent, path.name, as_attachment=False)


@app.get("/library-download/<collection>/<item_id>/<path:name>")
def library_download(collection, item_id, name):
    path = LIBRARY.asset_path(collection, item_id, name)
    if not path:
        return jsonify(error="Media not found."), 404
    return send_from_directory(path.parent, path.name, as_attachment=True)


if __name__ == "__main__":
    # Spawn SA3 immediately. The Flask/UI server comes up without waiting for
    # model load; the first GPU job waits for readiness, subsequent jobs reuse
    # the already-resident model.
    GPU_ENGINE.start_background()
    app.run(host=CONFIG["host"], port=CONFIG["port"], threaded=True, debug=False)
