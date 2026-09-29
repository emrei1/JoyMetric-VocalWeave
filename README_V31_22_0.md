# JoyMetric v31.22.0 — FutureSynth (with v31.20 DrumFront and v31.21 SynthWeave)

Briefs: "the generated drums should show themselves more at creativity max; in the A/B loops the drums must not loop" (v31.20) · "add synths and melodies that fit, with Stable Audio" (v31.21 → v31.22).

## v31.20 DrumFront

- **Presence.** Creativity buys the AI drummer level (`drum_presence` raises the record-relative target and ceiling), density (DrumMind density target) and adventurousness (sampling temperature 0.85 → 1.15). On a club record the drummer's own hat band is **4.2×** and its kick band **4.3×** louder at creativity 1 than at 0; no clipping; the clarity gate is softened where the record clearly has a grid.
- **Drum-free A/B loops.** While the drummer plays, Deck B runs through `DeDrum` (two-band transient designer in cut mode, kick-body low cut, top trim): transient energy **−12 dB**, kick body **−18.7 dB**, hats **−13.4 dB** on a drum loop, a pad unchanged (−0.7 dB), 0.34 ms per block. Loops keep chords, bass, vocals and textures; drums come from the drummer alone.

## v31.21 SynthWeave — loop path

The captured Deck-B loop goes to the local Stable Audio worker (RTX 5070) as **init audio** with a prompt for a melodic synth part in the record's key and tempo (negative prompt pushes drums/vocals out); the result is post-processed (high-pass 140 Hz, level matched, soft limit, seamless seam) and Deck B crossfades toward it on the grid (`b_ai_mix`) where the section has room. Kept for loops of ≥ 1 beat; in practice most captures are 0.5–2-beat rolls that change faster than the GPU can answer, which led to v31.22.

## v31.22 FutureSynth — composing on the record's upcoming bars

The programme is heard ~15 s after capture. Every 8 bars the agent cuts the record's **next 1–2 bars** out of the lookahead ring — downbeat-aligned through the frame anchor (capture and render counters index the same stream samples) — sends them to Stable Audio as init audio with the chosen part (arpeggio / pad / lead / plucks by vocal presence, energy arc and rotation; key from the stabilised key estimate), and schedules the returned clip at the **exact render frame** where those bars play. Generation takes 6–10 s against a lead of ~10 s, so the part is normally in place early; a late clip joins in progress, still aligned. The clip is a one-shot (never looped), edge-faded, gained by creativity and ducked under vocals, and only composed on material with a rhythmic grid (clarity ≥ 0.25). The lead adapts to the measured generation time (EMA; live it was 17–19 s under full CPU load versus 6–9 s idle, so clips start as 1 bar and grow to 2 bars when the GPU proves fast), diffusion runs 10 steps, and a clip that still arrives after its bars have started is re-aligned to the next even bar boundary instead of being dropped. Measured: clip cut at the impulse-marked downbeat exactly, scheduled 10.5 s ahead, audible from its scheduled frame within 2 samples; a 2-bar lead composed by the real worker in **8.8 s**.

## UI

SYNTH AI (composing / scheduled · kind), LOOPS (DRUM-FREE · SYNTH %), DRUM AI, DRUM KIT chips; `synth_ai` and `drum_ai` in the status JSON.

## Validation — see `V31_22_0_TESTS.txt`

## v31.22.1 — why the synths sounded unrelated, and the fix

The Stable Audio worker takes the file's sample rate as the model's native **44.1 kHz** and never resamples. Every clip was sent at 48 kHz, so the model rendered the requested duration at 44.1 kHz and the worker handed it back interpreted as 48 kHz: **8.8 % too fast and ~1.5 semitones sharp** — every generated part was in the wrong key and tempo, and the worker's duration check sent longer clips through slow "safe chunk" retries (17–19 s live). Clips are now converted to 44.1 kHz for the worker and back to 48 kHz; generation takes **1.4–1.5 s** and the parts sit in the record's key: harmonic agreement (peak-based tonal chroma) 0.86–0.89 against the record's bars, where the record's own next bars score 0.81 and an off-key dyad 0.18.

Two guarantees were added on top: a **harmonic sieve** (an STFT mask that attenuates pitch classes the record does not use by 14 dB, raising agreement to 0.90–0.92) and an **in-key gate** (a part whose tonal energy inside the record's pitch classes is below 60 % is rejected and never scheduled; the status shows `inkey` / `harm`). Init noise 0.45, 10 diffusion steps.

## v31.22.2 — "still unrelated, and every synth sounds like a cliché piano"

Three more causes, each measured on the live system:

- **Timing under load.** Generation took 10–19 s during a live session (1.5 s when the machine is idle): the Stable Audio worker's CPU-side stages lose to the CLAP critic. The worker now runs at AboveNormal priority (launcher + watchdog), diffusion is 8 steps, and a clip that would need more than 2 bars of shifting is dropped instead of played over music it was not written for.
- **The music moves on.** A part composed on bars N–N+1 can meet a chord change, a section change or a Spotify skip by the time it plays. A **play-time harmonic gate** compares every scheduled clip, 2.5 s before it starts, with the bars that are actually about to play (they are in the lookahead ring by then): tonal energy inside the current pitch classes < 60 % or chroma agreement < 0.55 → the clip is cancelled; the status shows `gate_inkey` / `gate_agree`. Clip gain is now 0.28–0.55 (was up to 0.9).
- **Piano timbre.** The prompts described "melodic synth" parts, which the model read as keys. Prompts now name synthesizer sound design explicitly (sawtooth arpeggio through a resonant low-pass with sidechain pumping, supersaw pad with a slow filter sweep, monophonic lead with portamento, detuned square plucks; "synthesizer only"), the negative prompt lists piano, electric piano, rhodes, keys, guitar, strings, orchestra, brass; cfg 7.5 with init noise 0.50 so the timbre can change while the sieve and gates keep the harmony.
- **Availability.** A worker that reports `busy` no longer disables the feature for the whole session; health is re-checked every 20 s and requests queue behind a running job.

## v31.22.3 — measured: the slowness was GPU memory paging; the prompt now describes the song

**Why the parts still came late or unrelated.** With the live session running, the Stable Audio worker held
7.7 GB of *dedicated* VRAM plus 1.9 GB of *shared* (system-RAM backed) GPU memory on an 8 GB card. WDDM then
pages allocations in and out on every diffusion step: `nvidia-smi` showed 100 % "utilization" for 8-19 s per
5-second clip although the same clip takes ~1.5 s on an idle machine, and the time no longer scaled with the
number of steps (4 steps 6.9 s, 8 steps 8.5 s, a 2.6 s clip 11.5 s). It was never CPU contention. Fix:

- `sa3_persistent_server.py` caps the CUDA caching allocator (`JOY_SA3_MEM_FRACTION`, default 0.82 of the card)
  and calls `torch.cuda.empty_cache()` after every job, so the resident set stays in dedicated memory;
  `gpu_engine.py` launches it with `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` and AboveNormal priority.
- A clip that misses its bars by more than 2 bars is dropped instead of played over unrelated music.

**The prompt now tells the model what to fit.** The old caption pasted the first 60 characters of the DJ prompt
("keep the 808 clean, phrase-aware changes") into the audio prompt. The caption is now built from the song:
genre and mood words extracted from the DJ prompt (never the instruction text), the detected key and tempo,
energy, whether it sits under a lead vocal, whether it rides the existing drums, and the CLAP musical state of
the bars it is written for (buildup / drop / breakdown / ...). It explicitly asks for
"an extra synthesizer layer composed for this exact song, following its chord progression and melody, in D#
minor at 92 BPM ... locked to its groove and complementing the existing arrangement", followed by the synth
sound design and "synthesizer only"; the negative prompt lists piano, rhodes, keys, guitar, strings, drums,
vocals, "different key, different tempo".

**Measured with CLAP** (zero-shot text similarity on the generated clips, hip-hop source at 92 BPM):
the input scores 0.18 for "solo piano"; the generated parts score 0.00-0.11 for piano and 0.19-0.30 for
"synthesizer pad" / "synth arpeggio", and their in-key energy against the record's own bars is 0.87.
