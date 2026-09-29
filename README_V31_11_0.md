# JoyMetric v31.11.0 — StageCraft

The goal of this release: make the performance sound like a **real remix** — striking, rhythm-locked, structured like produced dance music — by implementing the canonical live-DJ showmanship vocabulary as real DSP and giving the planner section-level structural awareness.

## 1. New engine effects (all beat-locked, all callback-safe)

### Spinback / Brake (`_apply_brake_fx`)
The mixer keeps a 6 s ring of its **own final output** — a "record" of what the listener just heard. On a beat-quantized trigger the live program is replaced by a resampled read of that record:
- **SPINBACK**: playhead accelerates backwards (0.55→3.6× reverse) while decaying — the definitive transition marker of club DJing.
- **BRAKE**: playhead winds down to zero (turntable power-off) — the gentler sibling.
The live stream keeps rendering underneath, so when the effect ends the program snaps back **exactly on the landing beat**. Vectorized (cumsum + gather + linear interp), 2 ms entry crossfade, 96-sample return fade — click-free by construction.

### Beat-locked sidechain pump (`fx_pump`, `fx_pump_cycle`, `fx_pump_sync_seq`)
The signature produced-remix "breathing": instant duck on the beat with a ~3 ms anti-click attack and musical exponential recovery, cycle length in beats, phase re-syncable by sequence trigger (every DROP re-locks the pump to the landing). Applied after the FX residual protections because it is a pure bounded gain — the protections would otherwise misread the duck as excess FX residual.

### Build bass strip (`fx_bass_cut`)
Produced EDM builds **remove the low end for the whole build and slam it back on the drop**. A smoothed program highpass (transparent at 22 Hz idle, up to ~210 Hz at full strip) gives that exact "floor drops away" feeling. DROP/PUNCH_IN restore it to zero instantly.

### Snare-rush ladder (`fx_rush_accel`)
The build-anatomy acceleration: onset rate climbs the metric grid **1/4 → 1/8 → 1/16 → 1/32** as the lane rises (instead of a static roll density), exactly like the exponential onset acceleration described for produced builds.

## 2. New gesture families + Director archetypes

Families: `SPINBACK`, `BRAKE_STOP`, `PUMP_GROOVE`, `QUANT_BUILD` (riser + rush ladder + bass strip, all ending on the landing), `ROLL_LADDER` (the halving beat-roll: 2 → 1 → ½ → ¼, each recapture beat-quantized via a deferred-trigger list).

Archetypes: **spinback_reveal** (full build → backspin the last beat → DROP into the vacuum → pump groove), **quantized_build_drop**, **roll_ladder_drop**, **brake_fakeout** (power-off mid-phrase → negative space → PUNCH back), **echo_spin_exit** (beat-synced echo tail → spinback through it → open the break), **pump_flow**. Concert pads gained SPINBACK / BRAKE / BUILD 8 / PUMP 8.

## 3. Section-aware structure (Musical Intelligence upgrade)

New **SectionNarrator** labels the 15 s lookahead with functional-structure classes — LOW / GROOVE / BUILD / PEAK / BREAK — and exposes the next boundary (`GROOVE→PEAK · 8 beats`). The Director's critic gained a **section_fit** term: an approaching PEAK rewards build/spin/drop choreography; an approaching BREAK rewards brake / echo-spin / wash exits; a GROOVE plateau invites the pump. Tension drivers now include the bass strip (+0.22) and rush ladder (+0.12) — the two strongest produced-EDM tension signals.

## 4. UI

- FX Rack grew to **21 live faders** (+ PUMP, BASS CUT, RUSH LADDER).
- Pad bank: + SPINBACK, BRAKE, BUILD 8, PUMP 8 (StageCraft accent styling).
- Transition Matrix shows live **SPINBACK/BRAKE winding progress**, SECTION boundary (`GROOVE→PEAK`) and the next-move window; DJ Director grid gained a SECTION cell.

## 5. Literature grounding (researched for this release)

DJ practice vocabulary:
- Backspin = abrupt, definitive transition marker; Brake = the gentler stop ([DJingTips — Backspins, Stops and Power-Offs](https://www.djingtips.com/how-to-dj/backspins-stops-and-power-offs/), [zZounds — Well-Placed Backspin](https://blog.zzounds.com/2020/01/10/improve-your-mix-with-a-well-placed-backspin/), [Wikipedia — Back spinning](https://en.wikipedia.org/wiki/Back_spinning))
- Echo-out on the last phrase beat with beat-synced delay; beat-roll halving 2→1→½→¼ as a build ([Digital DJ Pool — 7 Transitions](https://digitaldjpool.com/blog/dj-transitions-for-beginners/), [Virtuoso — DJ effects for transitions](https://playvirtuoso.com/blog/how-to-use-dj-effects-to-improve-your-transitions-and-blends/79), [vibesdj — DJ Transitions Guide](https://vibesdj.io/dj-tools/dj-transitions), [Thomann — 5 most important DJ transitions](https://www.thomannmusic.com/blog/gear/the-5-most-important-dj-transitions/))

Build anatomy / musical structure:
- Builds: snare onsets accelerate exponentially quarter→eighth→sixteenth→blurred roll; risers climb in pitch+intensity; the bass is stripped through the build and restored at the drop ([Osborn — Formal Functions and Rotations in Top-40 EDM, Intégral 2023](https://theory.esm.rochester.edu/integral/36-2023/osborn/), [EDMProd — Ultimate Guide to Build-Ups](https://www.edmprod.com/ultimate-guide-build-ups/), [Attack Magazine — 10 Snare Rolls for the Drop](https://www.attackmagazine.com/technique/tutorials/10-snare-rolls-for-the-drop/))
- Functional structure labeling (intro/verse/chorus… → here LOW/GROOVE/BUILD/PEAK/BREAK) and boundary detection via homogeneity/novelty/repetition ([To Catch a Chorus… — structural functions, 2022](https://arxiv.org/abs/2205.14700), [All-In-One Music Structure Analyzer](https://github.com/mir-aidj/all-in-one), [SongFormer, 2025](https://arxiv.org/pdf/2510.02797), [MuSFA](https://arxiv.org/pdf/2211.15787))
- Drop grammar: tension peaks in the build and is released by the drop ([Yadati et al. — Detecting Drops in EDM, ISMIR 2014](https://archives.ismir.net/ismir2014/paper/000297.pdf), [MediaEval 2014 — multimodal drop detection](https://ceur-ws.org/Vol-1263/mediaeval2014_submission_88.pdf))

## 6. Validation — 61/61 checks green

- **StageCraft suite (29)**: backspin engages/diverges/completes/returns clean; power-off winds to silence (tail RMS 0.0000); pump breathes beat-locked and never boosts; bass cut strips a 60 Hz floor 0.168→0.024 RMS; **neutral-controls output is sample-identical to legacy controls**; QUANT_BUILD lanes riser+ladder+strip and DROP slams the floor back + resyncs the pump; roll ladder fires 3 deferred recaptures down to ¼ beat; SectionNarrator labels PEAK/BUILD/BREAK approaches; Director prefers spinback_reveal at a PEAK boundary and offers brake/echo-spin exits at a BREAK; pads accepted and beat-quantized.
- **v31.10 regression suite (32)**: all green (tension threshold recalibrated to the rebalanced driver weights).

Windows Spotify routing and physical WASAPI output still require ear-validation on the target machine.
