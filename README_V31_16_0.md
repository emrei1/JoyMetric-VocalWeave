# JoyMetric v31.16.0 — SongMind

Brief (all messages, consolidated): the crackle still happens; make real-time ~10× faster; musical understanding must be far better (hi-hats loop and repeat badly); it must sound like a REAL remix; rethink the 15-second listening logic if needed; **engineer, very robustly, WHEN transitions and rises may happen — taking the whole song into account**; research the DJ and AI-music literature first; surprise me; then report.

## 0. Report summary (what changed, what was measured)

| Problem | Root cause found | Fix | Evidence |
|---|---|---|---|
| **Crackle** (still at 2048 frames) | Not xruns. Two real click sources: (1) every synth voice envelope was **truncated at 16th-step edges** (bass cut at 74 % amplitude, kick at 34 %); (2) the FX **protection chain applied a different constant gain per 43 ms block** (block-RMS residual cap, per-block trim, instant-attack preconditioner) — audible gain steps whenever the remix layers were loud, i.e. always at creativity max | (1) **voice-continuity engine**: a hit's envelope runs until the next hit of that voice; retriggers get a 5 ms release of the previous hit (kick phase continuous); (2) **click-free protection chain**: all three gains are per-sample ramps with fast-attack/slow-release level tracking | max sample jump ÷ max sine slope: **41.3 → 0.69** (below the sine's own slope); kick low-band edge jump 0.0014 vs peak 0.092 |
| **Hi-hats loop badly** | one of four fixed velocity patterns chosen once and looped forever; no relation to the record's own hats; a structurally bad hash made "variation" a slow ramp | **Hats 2.0** (GrooVAE-style humanization, native Rust): style families (CLUB/HIPHOP/DNB/SPARSE from the prompt), per-bar velocity humanization + avalanche hash, ghost-note drop/add, open/closed alternation, ±2.5 ms **microtiming** stable per 4-bar group, phrase accents, 16th rolls every 4th bar, flavor rotation, and **source-density thinning** (a record with busy hats gets fewer added ghosts) | bar-to-bar fingerprint variation **0.021 → 0.169** (8×); busy top end → density 0.35; CLUB vs HIPHOP measurably different |
| **Real-time ×10** | callback still spent most time in numpy hats/groove | native hats kernel + everything-on groove in one call | everything-on block: **5.13 ms (v31.14 Python) → 2.38 (v31.15) → 0.88 ms median / 1.30 ms p99** — 5.8× at the median, 6.4× at the p99 vs the Python baseline, inside a 42.7 ms budget (block size stays 2048: field-proven crackle-free) |
| **WHEN may a rise/transition happen** | planner only saw a 15 s window and a novelty scalar | **SongMemory** (learns the song's form online) + **TransitionLegality** (explicit, scored legal windows per gesture family) + Director/Arranger gating | see §2–§3; 15 rule tests green; Director targets legal landing 52 and lands the DROP exactly there, refuses drop archetypes when nothing is legal |

## 1. Literature that shaped the design

- **DJ phrasing**: phrases are 8/16/32 bars; "start the incoming track on beat 1 of the outgoing phrase"; **every major move — first fader up, bass swap, the drop, the final fader down — lands on a phrase boundary**; count bars 1-2-3-4 ([Wikipedia — Phrasing (DJ)](https://en.wikipedia.org/wiki/Phrasing_(DJ)), [vibesdj — Phrase Mixing 16/32-bar](https://vibesdj.io/learn/techniques/phrase-mixing), [DJingTips — How to match phrases](http://www.djingtips.com/skills/phrase-matching), [dj.studio — Transitions Playbook](https://dj.studio/blog/the-dj-transitions-playbook)) → `TransitionLegality`.
- **Humanized drums**: GrooVAE — velocity + microtiming are what make a drum performance human ([Gillick et al., "Learning to Groove with Inverse Sequence Transformations", ICML 2019](https://arxiv.org/abs/1905.06118), [Magenta GrooVAE](https://magenta.tensorflow.org/groovae); [Drumroll Please — multi-scale rhythmic gestures](https://transactions.ismir.net/articles/10.5334/tismir.98)) → Hats 2.0 velocity/microtiming humanizer.
- **Online structure**: self-similarity / lag-matrix repetition detection and structure-informed downbeat tracking; online segmentation is "more useful and actionable than batch" ([SSM-Net](https://arxiv.org/pdf/2211.08141), [Self-similarity & novelty losses for MSA](https://arxiv.org/pdf/2309.02243), [beat-feature MSA, PLOS ONE 2024](https://journals.plos.org/plosone/article?id=10.1371%2Fjournal.pone.0312608)) → `SongMemory`.

## 2. SongMemory — the 15-second window is no longer the horizon

The lookahead window sees ~7 bars. A DJ learns the FORM of a record while it plays. `SongMemory` writes one 18-dim feature vector per bar as the window reveals bars, runs an online self-similarity search (diagonal runs of ≥3 similar bars, phrase-multiple lags preferred), recognizes **"we are replaying bars we already heard"**, projects the boundaries that followed the earlier occurrence onto the future (**predicted section boundaries beyond the window**, with confidence), and votes phrase length from repetition lags. Verified on an ABAB form: after 20 bars it reports *repeat of bar 3, run 4*, predicts the next chorus at **beat 96 = exactly bar 24**, and infers a 32-beat (8-bar) phrase.

## 3. TransitionLegality — the WHEN engine

For every gesture family it computes explicit, scored, explainable legal beats:
- **LANDING** (drop/punch): phrase boundary (PhraseClock ∪ SongMemory phrase length), boosted by narrator section boundaries and memory-predicted boundaries; requires tension readiness ≥ 0.42, no refractory, beat-grid confidence ≥ 0.45, drop pacing.
- **RISE4/8/16**: must **end on a landing → start = landing − N**; needs build headroom; depth capped under vocals; +score into PEAK.
- **EXIT** (wash/brake/echo-spin): never rising arc, never mid-vocal-phrase.
- **WEAVE** (interleave/jumpback/roll ladders): only when the vocal is clear.
- **BASS_SWAP**: illegal during a key modulation.
Every window carries its reasons (`why`) and every veto its reason. The Director takes the best legal landing as the plan target, places builds so they end on it, and **drops the whole release-archetype family when nothing is legal**; the critic gained a `legality_fit` term. The Arranger enters STRIP only when a legal landing exists 6–18 beats ahead and snaps section ends to legal landings. Rules verified: landings on phrase boundaries; memory+section boundary ranks first; 8-beat rise starts 8 beats before; no landing without tension; unsure grid defers; no weave/exit through vocals; no bass swap in modulation.

## 4. Engine / runtime

- Rust core v3117: voice-continuity + retrigger release for kick/clap/bass/stab/hats; `jm_hats` kernel; avalanche hash.
- numpy fallback got the same continuity (searchsorted lookback) → Rust/Python groove parity preserved (11/11).
- Control-rate cadence 55 → 25 ms; sample declicker enabled in the launcher; `LAUNCH_SONGMIND.ps1` starts the semantic worker first, waits for health, then the app, and kills duplicate workers (a duplicate worker had leaked 63 GB in the field and starved the app).
- UI: SONG MEMORY, NEXT PREDICTED, LEGAL LANDING, LEGAL RISE, WHY/VETO and HATS cells; DJ Director grid shows the legal landing.

## 5. Validation — 150/150

SongMind suite (24) + regressions v31.15 (11), v31.14 (21), v31.13 (12), v31.12 (21), v31.11 (29), v31.10 (32) — all green, all with the Rust path active. Two legacy tests were recalibrated to continuity semantics (kick/bass envelopes now sustain across 16th edges, as they should).
