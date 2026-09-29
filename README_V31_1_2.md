# JoyMetric v31.1.2 — Crash-Safe Audio Scan

This release fixes a native Windows-audio stability issue in v31.1.1.

## Root cause
Both hidden Realtime and Agentic DJ views could trigger `NativeRealtimeEngine.list_devices()` during page startup. That call enumerates PyAudioWPatch/WASAPI and PyCAW/COM inside the Flask server process. Two overlapping native scans could therefore hit fragile Windows audio-driver/COM paths. A native access violation cannot be caught by Python `try/except`; the Flask process disappears and the browser reports `Failed to fetch`.

## Fix
- No WASAPI scan while Realtime/Agentic views are hidden at application boot.
- Device enumeration runs in `device_scan_worker.py`, a disposable subprocess.
- The Flask server uses a single scan lock and a 1.5 s last-known-good cache.
- If the worker crashes, times out, or raises, the JoyMetric web server remains alive.
- If a previous successful scan exists it is returned with `scan_cached=true`; otherwise the UI receives a controlled scan error rather than losing the backend.
- Agentic LET'S GO retains the v31.1.1 route recovery logic.
- Remaining native faults during engine start are traced to `runtime/backend_fatal.log` with Python faulthandler.
- Audio DSP, Agentic mixer, LowEndIntegrity master, virtual driver, and exact physical Audio Out path are unchanged from v31.1.1.

## Intended flow
1. Launch JoyMetric. No native device scan occurs during hidden-view initialization.
2. Open Agentic DJ. One isolated scan populates physical Audio Out.
3. Start Spotify playback.
4. Choose Audio Out and press LET'S GO.
5. Spotify is routed to the JoyMetric render endpoint and the live Agentic DJ starts.
