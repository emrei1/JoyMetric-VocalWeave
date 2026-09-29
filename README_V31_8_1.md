# JoyMetric v31.8.1 — Start/Route Handshake Fix

This patch keeps the v31.8.0 DJ Director V3, candidate planning, performance memory,
parameterized controller gestures, v31.7 continuity bridge, CrackleGuard and headphone
support unchanged. It fixes a startup failure where LET'S GO could appear to buffer and
then return Spotify to the original Windows output.

## Root causes fixed

1. The physical Audio Out stream was opened but not actually started until the 15 s
   lookahead FIFO had filled. A stale/Bluetooth/WASAPI endpoint could therefore fail at
   T+15 s. Native engine cleanup then restored Spotify to its original output.
2. The synchronous native start handshake waited only 2.7 s for Spotify per-app routing.
   SoundVolumeView/SVCL routing can legitimately take longer, causing the API to stop a
   route that was still starting normally.

## v31.8.1 startup order

1. Resolve selected physical Audio Out.
2. Start Audio Out immediately and confirm a real silent block write.
3. Keep Audio Out alive with intentional silence while lookahead fills.
4. Only after Audio Out preflight succeeds, migrate Spotify to JoyMetric Virtual Input.
5. Wait for the real route-live/error result instead of racing a 2.7 s deadline.
6. Capture/analyze the 15 s future PCM.
7. Fade the first processed musical block up from lookahead silence.

If Audio Out cannot start, Spotify is never migrated, so failure is immediate and safe.
If routing fails, the managed router restores Spotify as before.

## DJ architecture retained

- DJ Director V3
- 5-candidate choreography search
- performance memory + repetition critic
- 18 parameterized gesture families
- continuous controller action space
- Realtime FX + Agentic DJ unified prompt
- 15 s future analysis / delayed audible execution
- v31.7 dropout concealment and 2048-frame processing path
