# JoyMetric v31.1.1 — Agentic LET'S GO + Spotify Route Recovery

- Agentic DJ does nothing until **LET'S GO** is pressed.
- Spotify resolution retries PyCAW and falls back to a live Spotify.exe process.
- SVCL render-device discovery uses a real temporary CSV export.
- `/SetAppDefault` zero-exit is no longer falsely rejected just because `/Stdout` is empty.
- Route writes target durable `Spotify.exe` plus current live PIDs.
- Double-click start is locked; failure is caught/logged and leaves the app stopped instead of crashing.
- Agentic controller/transport endpoints are gated until LET'S GO is live.
- v30.4.47 LowEndIntegrity/master DSP and v31.1.0 DJ mixer DSP are unchanged.
