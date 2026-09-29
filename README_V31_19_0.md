# JoyMetric v31.19.0 — DrumMind (the AI drummer)

Brief: "the added hats and kicks should be added with an AI model — a drum rack, or a hat sampled from the song, with a model that decides the rhythm from the song. Research it thoroughly and implement it." Follow-ups: looping vocals are fine, the problem is hats and drums; GPU may be used; Stable Audio is fine too but choose the best option.

## 0. Research → decision

| Approach | What it is | Verdict for a live CPU audio process |
|---|---|---|
| GrooVAE / **tap2drum / Drumify** ([Gillick et al., ICML 2019](https://arxiv.org/pdf/1905.06118), [Magenta GrooVAE](https://magenta.tensorflow.org/groovae)) | a compressed rhythm (taps / accents) → a complete human drum performance with velocity and microtiming, trained on the [Groove MIDI Dataset](https://magenta.tensorflow.org/datasets/groove) (1150 performances, 22 000 bars) | **the right formulation**: our record accents are the "taps"; the dataset is public; a small model runs in ~8 ms in numpy |
| DARC ([arXiv 2601.02357](https://arxiv.org/abs/2601.02357)), TRIA ([arXiv 2509.15625](https://arxiv.org/abs/2509.15625)), JukeDrummer (ISMIR 2022), LiveBand ([arXiv 2606.03803](https://arxiv.org/pdf/2606.03803)), StemGen / MusicGen-Stem | audio-domain drum accompaniment conditioned on the mixture and/or a rhythm prompt | closest to the wish, but no released weights for DARC/TRIA, multi-GB GPU inference, seconds of latency, and they render the drums themselves (no control over grid, level, clarity) |
| DOSE ([arXiv 2504.18157](https://arxiv.org/html/2504.18157v1)) | extract kick / snare / hat one-shots from a music mixture (transformer + DAC codec) | the *idea* is adopted directly: our kit cuts the record's own hats and kick out of the mixture with an isolation-scored transient search (DOSE-lite, numpy, 30 ms) |
| Stable Audio Open one-shots ([Stability AI](https://stability.ai/news-updates/introducing-stable-audio-open)) | text → drum samples | the local worker (`sa3_persistent_server`) is an audio-to-audio *edit* pipeline; wired as an optional GPU polish of the sampled kit (`JOY_DRUM_AI_POLISH=1`), never on the audio path |

## 1. DrumMind — the pattern model (`drum_brain.py`, `models/drummind.npz`)

- **Data**: Groove MIDI Dataset → 20 522 two-bar windows (kick / snare / closed hat / open hat, velocity + microtiming per 16th), 16 457 train / 2 000 val / 2 065 test, style + tempo + fill labels.
- **Model**: conditional GRU (160 hidden) with teacher forcing. Condition = the record's bar-averaged kick / snare / hat accent profiles (16 steps each), tempo, style (12 GMD styles mapped from the prompt), a density target and a fill flag; heavy augmentation (blur, spurious onsets, dropout, scaling) so accents measured on a full mix stay in distribution. Trained on the RTX 5070 in 19 s.
- **Quality**: test hit-F1 given the true accents — kick **0.84**, snare **0.77**, closed hat **0.87** (retrieval-of-real-grooves baseline 0.78 / 0.71 / 0.67).
- **Runtime**: numpy inference (~8 ms per phrase); 6 model samples + 3 retrieved human grooves compete on a **musical fit** score (reinforce the record's kicks, never a kick on a snare-only backbeat, complementary hat density, density target), the winner is post-edited by the same rules; microtiming scaled per style (club/house 0.35 → tight, hip-hop 0.8, jazz/latin 1.0). New pattern every 2 bars, fills on bars 7–8 of the phrase.

## 2. DrumKit — sounds that belong to the record (`drum_kit.py`)

- closed hat: the most isolated high-band transient of the lookahead window (transient-based isolation, decay ≤ 90 ms), cut, high-passed, enveloped; open hat: a longer high-band transient or the closed sample extended with looped grains;
- kick: an isolated low-band transient that decays like a drum (not a bass note) cut from the record — else a kick synthesised to the record's measured fundamental and decay (f0 within 7 % on the synthetic test);
- clap synthesised; noise / beat-less material falls back to synthesis (no bogus samples).

## 3. Rendering (`SampleDrumLayer` in the engine)

Sample voices scheduled on the **frame-accurate, downbeat-aligned grid** of v31.18 (velocity, microtiming, overlap-add across blocks, closed hat chokes open hat, kick-keyed sidechain duck), **level-matched** to the record's own kick / hat bands, **clarity-gated** (a beat-less passage gets no drummer), 0.7 ms per block. While the drummer plays, the synthesized re-drum kick and ghost hats are muted. Measured: kicks on the anchored beats with 0.7 ms median deviation; in the 30 s real-time end-to-end run the drummer's low hits sit on the record's beats (median 1.7 ms, p90 11 ms) and its hats on the 16th grid within the intended humanization.

## 4. UI

Agentic panel: **DRUM AI** (pattern source · hits) and **DRUM KIT** (hat:record/synth, kick:record/synth, f0) chips; `drum_ai` in the status JSON.

## 5. Validation — 228/228

DrumMind suite 25 (model quality vs baseline, accent following, backbeat rule, humanization, variation, record-sampled kit, tuned kick, sample layer timing/level/cost, real-time e2e), plus every earlier suite: v31.18 (21), BeatClock (14), v31.17 (18), v31.16 (24), v31.15 (11), v31.14 (21), v31.13 (12), v31.12 (21), v31.11 (29), v31.10 (32).
