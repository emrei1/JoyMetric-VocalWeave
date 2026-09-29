from __future__ import annotations

import atexit
import ctypes
import json
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path


class PersistentGPUWorker:
    """Owns the long-lived Stable Audio GPU subprocess.

    The web app runs in the RoFormer environment while Stable Audio lives in a
    separate conda environment. Keeping SA3 in a child process lets us retain
    that separation while keeping the model resident in VRAM between jobs.
    """

    def __init__(self, app_root: Path, cfg: dict):
        self.app_root = Path(app_root)
        self.cfg = cfg
        self.host = str(cfg.get("gpu_worker_host", "127.0.0.1"))
        self.port = int(cfg.get("gpu_worker_port", 8766))
        self.proc: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._log_handle = None
        self._monitor = None
        # v31.30.38 supervisor state: backoff after failed loads, commit-charge gate, forensics
        self._fails = 0
        self._restarts = 0
        self._last_spawn = 0.0
        self._last_exit = 0.0
        self._last_exit_code = None
        self._deferred_since = 0.0
        self._deferred_reason = ""
        self._last_defer_log = 0.0
        atexit.register(self.stop)

    def _conda_env_python(self) -> Path | None:
        conda = Path(self.cfg["conda_exe"])
        if not conda.exists():
            return None
        try:
            r = subprocess.run(
                [str(conda), "env", "list", "--json"],
                capture_output=True, text=True, timeout=25,
            )
            if r.returncode:
                return None
            wanted = str(self.cfg["sa3_gpu_env"]).lower()
            for x in json.loads(r.stdout).get("envs", []):
                env = Path(x)
                if env.name.lower() == wanted:
                    py = env / "python.exe"
                    return py if py.exists() else None
        except Exception:
            return None
        return None

    def _health(self, timeout=0.35):
        try:
            with urllib.request.urlopen(
                f"http://{self.host}:{self.port}/health", timeout=timeout
            ) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None

    def start(self):
        with self._lock:
            if self._stop.is_set():
                return
            if self.proc is not None and self.proc.poll() is None:
                return
            # Reuse a healthy worker if the parent app restarted without the
            # worker being killed (start.ps1 normally cleans the port first).
            if self._health(timeout=0.25):
                return
            # v31.30.5: a worker that is still LOADING answers no health probe for ~30 s; a second start() in that
            # window used to spawn a second 4.5 GB worker (two of them page the GPU: 30 s edits, dropouts).  A pid file
            # marks a worker that was started recently and is still alive -> wait for it instead of spawning again.
            pid_path = self.app_root / "runtime" / "sa3_engine.pid"
            try:
                if pid_path.is_file():
                    parts = pid_path.read_text(encoding="utf-8").split()
                    pid, t_start = int(parts[0]), float(parts[1])
                    if time.time() - t_start < 300.0 and self._pid_alive(pid):
                        return
            except Exception:
                pass

            # v31.30.38: no blind respawn storms.  Measured 2026-09-19: a worker (re)load needs ~12 GB of
            # free commit charge (RAM + pagefile) with the CPU-first loader (~22 GB with the upstream one);
            # without it the load dies with 'The paging file is too small' (error 1455) or an access
            # violation in c10.dll, and the old monitor retried every 4 s - 672 of 900 starts in one log
            # failed that way while the user heard 'the model stopped'.  (1) exponential backoff after a
            # failed load, (2) wait for commit headroom (up to JOY_SA3_COMMIT_WAIT_S, then try anyway).
            now = time.time()
            if self._fails > 0:
                backoff = min(10.0 * (2 ** (self._fails - 1)), 90.0)
                if now - self._last_exit < backoff:
                    self._deferred_reason = "backoff %.0f s after %d failed load(s)" % (backoff, self._fails)
                    return
            avail, limit = self._commit_avail_gb()
            need = float(os.environ.get("JOY_SA3_MIN_COMMIT_GB", "12") or 12.0)
            max_wait = float(os.environ.get("JOY_SA3_COMMIT_WAIT_S", "300") or 300.0)
            if avail is not None and avail < need:
                if not self._deferred_since:
                    self._deferred_since = now
                if now - self._deferred_since < max_wait:
                    self._deferred_reason = "low commit: %.1f GB free < %.0f GB needed (limit %.1f GB)" % (avail, need, limit or 0.0)
                    if now - self._last_defer_log > 30.0:
                        self._last_defer_log = now
                        self._log("SA3 start deferred: " + self._deferred_reason)
                    return
                self._log("SA3 start: commit still low (%.1f GB free) after %.0f s - attempting anyway" % (avail, now - self._deferred_since))
            self._deferred_since = 0.0
            self._deferred_reason = ""

            py = self._conda_env_python()
            if not py:
                return

            log_dir = self.app_root / "runtime"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "sa3_engine.log"
            if self._log_handle:
                try:
                    self._log_handle.close()
                except Exception:
                    pass
            self._log_handle = log_path.open("a", encoding="utf-8", errors="replace")
            self._log_handle.write("\n=== starting persistent SA3 engine === " + time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
            self._log_handle.flush()

            script = self.app_root / "workers" / "sa3_persistent_server.py"
            creationflags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                creationflags = subprocess.CREATE_NO_WINDOW
            # v31.22.3: the synth composer must come back in seconds while the CPU critic and the
            # realtime engine are busy -> scheduling priority above them
            # v31.30.4/5: GPU work does not need CPU priority; AboveNormal preempted the audio process (dropouts)
            if hasattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS"):
                creationflags |= subprocess.BELOW_NORMAL_PRIORITY_CLASS
            env = dict(os.environ)
            # expandable segments: no fragmentation growth across many short live clips (8 GB laptop card)
            env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
            env.setdefault("JOY_SA3_MEM_FRACTION", "0.82")
            self.proc = subprocess.Popen(
                [str(py), str(script), "--host", self.host, "--port", str(self.port), "--model", "medium"],
                cwd=str(self.app_root),
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=creationflags,
                env=env,
            )
            try:
                pid_path.write_text("%d %.0f" % (self.proc.pid, time.time()), encoding="utf-8")
            except Exception:
                pass
            self._last_spawn = time.time()
            self._restarts += 1
            self._log("SA3 worker spawned pid=%d (spawn #%d, failed loads so far %d)" % (self.proc.pid, self._restarts, self._fails))

    @staticmethod
    def _commit_avail_gb():
        """(available commit GB, commit limit GB) from GlobalMemoryStatusEx; (None, None) if unavailable."""
        try:
            class _MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                            ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                            ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                            ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                            ("ullAvailExtendedVirtual", ctypes.c_uint64)]
            ms = _MS()
            ms.dwLength = ctypes.sizeof(_MS)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                return None, None
            return ms.ullAvailPageFile / 2 ** 30, ms.ullTotalPageFile / 2 ** 30
        except Exception:
            return None, None

    def _log(self, msg: str):
        """Timestamped supervisor line in runtime/sa3_engine.log (the worker's own prints carry no time)."""
        try:
            if self._log_handle is None:
                log_dir = self.app_root / "runtime"
                log_dir.mkdir(parents=True, exist_ok=True)
                self._log_handle = (log_dir / "sa3_engine.log").open("a", encoding="utf-8", errors="replace")
            self._log_handle.write("[gpu_engine %s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
            self._log_handle.flush()
        except Exception:
            pass

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code)); ctypes.windll.kernel32.CloseHandle(h)
            return bool(ok) and code.value == 259                                   # STILL_ACTIVE
        except Exception:
            return False

    def start_background(self):
        self.start()
        if self._monitor and self._monitor.is_alive():
            return

        def monitor():
            healthy_checked = 0.0
            while not self._stop.wait(3.0):
                with self._lock:
                    p = self.proc
                    dead = p is not None and p.poll() is not None
                    code = p.poll() if dead else None
                if dead:
                    # A CUDA crash/OOM should not permanently brick the app.  v31.30.38: a death within 180 s of
                    # the spawn is a FAILED LOAD (backoff + commit gate in start()); every exit is logged with time.
                    now = time.time()
                    self._last_exit = now
                    self._last_exit_code = code
                    since = now - self._last_spawn if self._last_spawn else -1.0
                    if 0.0 <= since < 180.0:
                        self._fails += 1
                        self._log("SA3 worker pid=%s exited code=%s after %.0f s - failed load #%d" % (getattr(p, "pid", "?"), code, since, self._fails))
                    else:
                        self._fails = 0
                        self._log("SA3 worker pid=%s exited code=%s after %.0f s of service - restarting" % (getattr(p, "pid", "?"), code, since))
                    with self._lock:
                        if self.proc is p:
                            self.proc = None
                    time.sleep(1.0)
                    try:
                        self.start()
                    except Exception:
                        pass
                elif self.proc is None and not self._stop.is_set():
                    # start was deferred (backoff / low commit) - keep trying every 3 s
                    try:
                        self.start()
                    except Exception:
                        pass
                elif self._fails and time.time() - healthy_checked > 20.0:
                    healthy_checked = time.time()
                    if self._health(timeout=0.5):
                        self._log("SA3 worker healthy again - clearing %d failed-load mark(s)" % self._fails)
                        self._fails = 0

        self._monitor = threading.Thread(target=monitor, name="sa3-engine-monitor", daemon=True)
        self._monitor.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            p = self.proc
            self.proc = None
        if p is not None and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=6)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        if self._log_handle:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None

    def set_device(self, target: str, timeout: float = 25.0):
        """Move the resident Stable Audio model off/on CUDA without killing it."""
        target = str(target or "cpu").strip().lower()
        path = "/activate" if target in {"cuda", "gpu"} else "/offload"
        try:
            req = urllib.request.Request(
                f"http://{self.host}:{self.port}{path}", data=b"{}",
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as exc:
            return {"ok": False, "error": str(exc), "device": None}

    def offload(self, timeout: float = 25.0):
        return self.set_device("cpu", timeout=timeout)

    def activate(self, timeout: float = 60.0):
        return self.set_device("cuda", timeout=timeout)

    def status(self):
        h = self._health(timeout=0.5)
        sup = self.supervisor_status()
        if h:
            try:
                h["supervisor"] = sup
            except Exception:
                pass
            return h
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return {"ok": False, "state": "starting", "model": "medium", "supervisor": sup}
        state = "deferred" if self._deferred_reason else "offline"
        return {"ok": False, "state": state, "model": "medium", "supervisor": sup}

    def supervisor_status(self):
        """v31.30.38: restart/backoff/commit facts for /api status and the monitor."""
        avail, limit = self._commit_avail_gb()
        return {"restarts": self._restarts, "failed_loads": self._fails, "deferred": self._deferred_reason or None,
                "last_exit_code": self._last_exit_code,
                "commit_avail_gb": None if avail is None else round(avail, 1),
                "commit_limit_gb": None if limit is None else round(limit, 1)}
