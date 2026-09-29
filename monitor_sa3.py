"""JoyMetric v31.30.38 - read-only health monitor for the Stable Audio (sa3) worker and the realtime stack.

Samples every JOY_MONITOR_INTERVAL seconds (default 10) and appends one line per sample to
%LOCALAPPDATA%/JoyMetric/logs/monitor_sa3.log, plus 'EVENT' lines when something changes:
sa3 health (ready/busy/down), sa3 pid (restart), weave failures, weave stalls while a session runs,
the app going unreachable, low free commit charge, supervisor lines in runtime/sa3_engine.log.
It never touches any process.  Stop it by creating runtime/monitor_stop next to this file.

Run (roformer_sep env):  python monitor_sa3.py
"""
import os, sys, time, json, ctypes, subprocess, urllib.request, pathlib, datetime, re

try:
    import psutil
except Exception:
    psutil = None

ROOT = pathlib.Path(__file__).resolve().parent
LOGDIR = pathlib.Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "JoyMetric" / "logs"
LOG = LOGDIR / "monitor_sa3.log"
INTERVAL = float(os.environ.get("JOY_MONITOR_INTERVAL", "10") or 10.0)
STOP = ROOT / "runtime" / "monitor_stop"
PIDF = ROOT / "runtime" / "monitor_sa3.pid"
SA3LOG = ROOT / "runtime" / "sa3_engine.log"
APP = "http://127.0.0.1:8765"


def log(line):
    LOGDIR.mkdir(parents=True, exist_ok=True)
    try:
        if LOG.exists() and LOG.stat().st_size > 30 * 1024 * 1024:
            LOG.replace(LOG.with_suffix(".1.log"))
    except Exception:
        pass
    with LOG.open("a", encoding="utf-8", errors="replace") as fh:
        fh.write(line + "\n")


def get_json(url, timeout=1.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


class _MS(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64)]


def memory():
    try:
        ms = _MS(); ms.dwLength = ctypes.sizeof(_MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            g = 2 ** 30
            return {"commit_used": (ms.ullTotalPageFile - ms.ullAvailPageFile) / g, "commit_limit": ms.ullTotalPageFile / g,
                    "commit_free": ms.ullAvailPageFile / g, "phys_free": ms.ullAvailPhys / g}
    except Exception:
        pass
    return {}


def procs():
    out = {"sa3": [], "sep": [], "sem": [], "app": []}
    if psutil is None:
        return out
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if "python" not in (p.info.get("name") or "").lower():
                continue
            cl = " ".join(p.info.get("cmdline") or [])
            key = None
            if "sa3_persistent_server" in cl:
                key = "sa3"
            elif "sep_persistent_server" in cl:
                key = "sep"
            elif "realtime_semantic_server" in cl:
                key = "sem"
            elif cl.strip().endswith("app.py") or " app.py" in cl:
                key = "app"
            if key:
                out[key].append((p.pid, int(p.memory_info().private / 2 ** 20)))
        except Exception:
            continue
    return out


def gpu():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,utilization.gpu", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=4)
        parts = [x.strip() for x in r.stdout.strip().split(",")]
        return {"gpu_mem": int(parts[0]), "gpu_util": int(parts[1])}
    except Exception:
        return {}


def sa3_log_state():
    """(number of worker starts, last supervisor line, last exception class line) from the tail of the sa3 log."""
    try:
        size = SA3LOG.stat().st_size
        with SA3LOG.open("rb") as fh:
            fh.seek(max(0, size - 400_000))
            tail = fh.read().decode("utf-8", "replace")
        starts_total = 0
        with SA3LOG.open("rb") as fh:
            for chunk in iter(lambda: fh.read(4 * 1024 * 1024), b""):
                starts_total += chunk.count(b"=== starting persistent SA3 engine ===")
        sup = [l for l in tail.splitlines() if l.startswith("[gpu_engine")]
        exc = [l for l in tail.splitlines() if re.match(r"^[A-Za-z_.]+(Error|Exception):", l)]
        return starts_total, (sup[-1] if sup else ""), (exc[-1][:120] if exc else "")
    except Exception:
        return None, "", ""


def main():
    if PIDF.exists() and psutil is not None:
        try:
            old = int(PIDF.read_text().strip())
            if psutil.pid_exists(old) and old != os.getpid():
                print("monitor already running (pid %d)" % old)
                return
        except Exception:
            pass
    PIDF.parent.mkdir(parents=True, exist_ok=True)
    PIDF.write_text(str(os.getpid()))
    log("=== monitor start %s pid=%d interval=%.0fs folder=%s" % (datetime.datetime.now().isoformat(timespec="seconds"), os.getpid(), INTERVAL, ROOT.name))
    prev = {}
    stall_since = None
    while not STOP.exists():
        t0 = time.time()
        st = get_json(APP + "/api/agentic/status", timeout=4.0)
        h_sa3 = get_json("http://127.0.0.1:8766/health", timeout=1.5)
        h_sep = get_json("http://127.0.0.1:8770/health", timeout=1.5)
        mem = memory(); pr = procs(); g = gpu()
        starts, sup_line, exc_line = sa3_log_state()
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = {}
        if st is None:
            cur["app"] = "down"; rt = {}; w = {}
        else:
            cur["app"] = "ok"; rt = st.get("realtime") or {}; sa = st.get("synth_ai") or {}
            w = sa.get("weave") if isinstance(sa.get("weave"), dict) else {}
        cur["running"] = bool(rt.get("running"))
        cur["xruns"] = rt.get("xruns")
        cur["sa3_pids"] = tuple(sorted(p for p, _ in pr["sa3"]))
        if h_sa3:
            cur["sa3"] = (h_sa3 or {}).get("state", "ready")
        elif cur["sa3_pids"]:
            # the worker's HTTP server is single-threaded: a /health probe waits behind a running 24 s generation,
            # so a timeout with the process alive means BUSY, not dead (false 'down' events on 2026-09-19 02:01)
            cur["sa3"] = "busy?"
        else:
            cur["sa3"] = "down"
        cur["sep"] = (h_sep or {}).get("state", "down") if h_sep else "down"
        cur["w_count"] = w.get("count"); cur["w_failed"] = w.get("failed"); cur["w_late"] = w.get("late")
        cur["w_error"] = str(w.get("error") or "")[:80]
        cur["starts"] = starts; cur["sup"] = sup_line; cur["exc"] = exc_line
        cur["commit_free"] = mem.get("commit_free")
        # xrun rate
        rate = None
        if prev.get("xruns") is not None and cur["xruns"] is not None and prev.get("t"):
            try:
                rate = (float(cur["xruns"]) - float(prev["xruns"])) / max(1e-3, t0 - prev["t"])
            except Exception:
                rate = None
        last = w.get("last") or {}
        gd = last.get("guard") if isinstance(last.get("guard"), dict) else None
        guard_txt = ""
        if gd:
            guard_txt = " guard[ch=%s on=%s%s]" % (gd.get("chroma"), gd.get("onset"), (" RETRY->%s ch2=%s on2=%s" % (gd.get("used"), gd.get("chroma2"), gd.get("onset2"))) if gd.get("retry") else "")
        fmt = ("%s | app=%s run=%d xr=%s(%s) out=%sdB fill=%sms gap=%sms | weave n=%s f=%s late=%s busy=%s last[w=%s gain=%s noise=%s cfg=%s sa=%sms]" + guard_txt + " err=%s"
               " | sa3=%s %s starts=%s | sep=%s sem=%d | commit %.1f/%.1f GB free=%.1f phys_free=%.1f | gpu %sMiB %s%%")
        line = (fmt % (
                    now, cur["app"], int(cur["running"]), cur["xruns"], ("%.1f/s" % rate) if rate is not None else "-", rt.get("output_db"),
                    rt.get("bridge_fill_ms"), rt.get("capture_gap_max_ms"),
                    cur["w_count"], cur["w_failed"], cur["w_late"], w.get("busy"), last.get("w"), last.get("gain"), last.get("noise"), last.get("cfg"), last.get("sa_ms"), cur["w_error"] or "-",
                    cur["sa3"], " ".join("pid%d:%dMB" % (p, m) for p, m in pr["sa3"]) or "no-process", starts,
                    cur["sep"], len(pr["sem"]), mem.get("commit_used", 0.0), mem.get("commit_limit", 0.0), mem.get("commit_free", 0.0), mem.get("phys_free", 0.0),
                    g.get("gpu_mem", "?"), g.get("gpu_util", "?")))
        log(line)
        # events
        ev = []
        if prev:
            if cur["app"] != prev.get("app"):
                ev.append("app %s -> %s" % (prev.get("app"), cur["app"]))
            if cur["sa3"] != prev.get("sa3") and "down" in (cur["sa3"], prev.get("sa3")):
                ev.append("sa3 health %s -> %s" % (prev.get("sa3"), cur["sa3"]))
        # a worker that does not answer /health for 3 samples (~30 s) although its process is alive is wedged or
        # stuck in a very long job (a normal 24 s weave generation takes ~2-9 s)
        # a freshly (re)spawned worker answers nothing while it loads (~25-60 s): label that 'loading', not wedged
        pid_changed_at = prev.get("pid_changed_at", t0)
        if prev and cur["sa3_pids"] != prev.get("sa3_pids"):
            pid_changed_at = t0
        cur["pid_changed_at"] = pid_changed_at
        if cur["sa3"] == "busy?" and t0 - pid_changed_at < 150.0:
            cur["sa3"] = "loading"
        cur["noresp_n"] = (prev.get("noresp_n", 0) + 1) if cur["sa3"] == "busy?" else 0
        if cur["noresp_n"] == 3:
            ev.append("sa3 not answering /health for ~30 s while its process is alive (wedged or very long job)")
        if cur["sa3"] != "busy?" and prev.get("noresp_n", 0) >= 3:
            ev.append("sa3 answering again after %d silent samples" % prev.get("noresp_n", 0))
            if cur["sa3_pids"] != prev.get("sa3_pids"):
                ev.append("sa3 process change %s -> %s (RESTART)" % (prev.get("sa3_pids"), cur["sa3_pids"]))
            if cur["starts"] is not None and prev.get("starts") is not None and cur["starts"] > prev["starts"]:
                ev.append("sa3 worker start #%s logged; supervisor: %s" % (cur["starts"], cur["sup"] or "-"))
            if (cur["w_failed"] or 0) > (prev.get("w_failed") or 0):
                ev.append("weave FAILED window (%s -> %s): %s" % (prev.get("w_failed"), cur["w_failed"], cur["w_error"] or "-"))
            if (cur["w_late"] or 0) > (prev.get("w_late") or 0):
                ev.append("weave LATE window (%s -> %s)" % (prev.get("w_late"), cur["w_late"]))
            if cur["running"] != prev.get("running"):
                ev.append("session running %s -> %s" % (prev.get("running"), cur["running"]))
            if cur["exc"] and cur["exc"] != prev.get("exc") and "ConnectionAbortedError" not in cur["exc"] and "ConnectionResetError" not in cur["exc"]:
                ev.append("sa3 log exception: %s" % cur["exc"])
            if cur["commit_free"] is not None and cur["commit_free"] < 8.0 and (prev.get("commit_free") or 99) >= 8.0:
                ev.append("LOW COMMIT: %.1f GB free" % cur["commit_free"])
            if rate is not None and rate > 3.0 and (prev.get("rate") or 0.0) <= 3.0:
                ev.append("xrun rate %.1f/s" % rate)
            if gd and gd.get("retry") and last.get("w") != prev.get("last_w"):
                ev.append("harmony guard retried window %s: chroma %s->%s onset %s->%s used=%s" % (last.get("w"), gd.get("chroma"), gd.get("chroma2"), gd.get("onset"), gd.get("onset2"), gd.get("used")))
        cur["last_w"] = last.get("w")
        # weave stall: a running session with no new window for > 100 s
        if cur["running"] and cur["w_count"] is not None:
            if prev.get("w_count") == cur["w_count"]:
                if stall_since is None:
                    stall_since = prev.get("t", t0)
                elif t0 - stall_since > 100.0 and not prev.get("stall_reported"):
                    ev.append("weave STALLED: no new window for %.0f s (count %s) sa3=%s" % (t0 - stall_since, cur["w_count"], cur["sa3"]))
                    cur["stall_reported"] = True
            else:
                if prev.get("stall_reported"):
                    ev.append("weave resumed (count %s)" % cur["w_count"])
                stall_since = None
        else:
            stall_since = None
        if prev.get("stall_reported") and "stall_reported" not in cur and stall_since is not None:
            cur["stall_reported"] = True
        for e in ev:
            log("%s | EVENT %s" % (now, e))
        cur["t"] = t0; cur["rate"] = rate
        prev = cur
        time.sleep(max(1.0, INTERVAL - (time.time() - t0)))
    log("=== monitor stop %s" % datetime.datetime.now().isoformat(timespec="seconds"))
    try:
        PIDF.unlink()
    except Exception:
        pass


if __name__ == "__main__":
    main()
