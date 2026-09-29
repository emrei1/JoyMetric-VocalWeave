from __future__ import annotations

"""JoyMetric Musical Intelligence Core (v31.10.0).

A lightweight, model-free musical-awareness layer that raises the Agentic DJ's
musicality from "reactive FX scheduler" to "phrase-, tension- and arc-aware
performer".  Every component is O(small) per tick, allocates almost nothing,
and never touches the audio callback: it only reads analysis dictionaries the
agent already computes and produces advisory context for the DJ Director.

The design operationalizes published MIR / auto-DJ findings:

- Real-world DJ transitions overwhelmingly happen on 8/16/32-beat phrase
  boundaries and are dominated by EQ/fader moves, not by exotic FX
  (Kim et al., "A Computational Analysis of Real-World DJ Mixes using
  Mix-To-Track Subsequence Alignment", ISMIR 2020; Vande Veire & De Bie,
  "From raw audio to a seamless mix", 2018).            -> PhraseClock
- Cue/switch points live on structural novelty boundaries
  (Zehren et al., "Automatic Detection of Cue Points for DJ Mixing", 2020;
  Foote's audio-novelty segmentation).                  -> PhraseClock + arc
- Musical tension is a dimension of its own, distinct from energy: it is
  raised by risers, rhythmic instability, spectral sharpening and loudness
  slope, peaks in the build/breakdown and is *released* by the drop
  (Yadati et al., "Detecting Drops in EDM", ISMIR 2014). -> TensionField
- Learned DJ transition generators converge on slow contrary EQ motion of the
  outgoing/incoming decks with a single low-end owner at all times
  (Chen et al., "Automatic DJ Transitions with Differentiable Audio Effects
  and GANs", 2021).                                     -> bass-swap grammar
- Harmonic mixing practice constrains simultaneous voices to near keys on the
  circle of fifths / Camelot wheel.                     -> HarmonicField
"""

import math
import time
from typing import Any

KEY_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Camelot wheel numbers for major (B) and minor (A) keys, indexed by pitch class.
_CAMELOT_MAJOR = {0: "8B", 1: "3B", 2: "10B", 3: "5B", 4: "12B", 5: "7B", 6: "2B", 7: "9B", 8: "4B", 9: "11B", 10: "6B", 11: "1B"}
_CAMELOT_MINOR = {0: "5A", 1: "12A", 2: "7A", 3: "2A", 4: "9A", 5: "4A", 6: "11A", 7: "6A", 8: "1A", 9: "8A", 10: "3A", 11: "10A"}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


def _key_to_pc(key: str) -> tuple[int, bool] | None:
    """Return (pitch_class, is_minor) for a key string like 'F#m' / 'A'."""
    k = str(key or "").strip()
    if not k or k == "—":
        return None
    minor = k.endswith("m")
    root = k[:-1] if minor else k
    if root not in KEY_NAMES:
        return None
    return KEY_NAMES.index(root), minor


def camelot_label(key: str) -> str:
    pc = _key_to_pc(key)
    if pc is None:
        return "—"
    idx, minor = pc
    return (_CAMELOT_MINOR if minor else _CAMELOT_MAJOR)[idx]


def key_compatibility(key_a: str, key_b: str) -> float:
    """0..1 harmonic-mixing compatibility between two key strings.

    1.0 same key · ~0.9 relative major/minor · ~0.8 circle-of-fifths neighbor ·
    decreasing with fifths distance · tritone-distant keys score near zero.
    Unknown keys return a permissive 0.5 so the fixed realtime quality gate
    stays the final authority.
    """
    a = _key_to_pc(key_a); b = _key_to_pc(key_b)
    if a is None or b is None:
        return 0.5
    (pa, ma), (pb, mb) = a, b
    if ma != mb:
        # Compare on the relative-major plane: A minor ≡ C major.
        pa_rel = (pa + 3) % 12 if ma else pa
        pb_rel = (pb + 3) % 12 if mb else pb
        if pa_rel == pb_rel:
            return 0.90
        pa, pb = pa_rel, pb_rel
        penalty = 0.06
    else:
        penalty = 0.0
    # Distance in steps around the circle of fifths (0..6).
    fifths = (pb - pa) * 7 % 12
    d = min(fifths, 12 - fifths)
    base = {0: 1.0, 1: 0.80, 2: 0.55, 3: 0.34, 4: 0.20, 5: 0.10, 6: 0.05}[d]
    return _clamp(base - penalty)


class PhraseClock:
    """Estimate the 8/16/32-beat phrase grid and where 'now' sits inside it.

    Uses the future-segment novelty boundaries the agent already computes: real
    phrase boundaries produce novelty peaks whose beat offsets agree modulo the
    phrase length.  A tiny evidence accumulator per candidate length replaces
    any heavy self-similarity computation.
    """

    def __init__(self) -> None:
        self.phrase_beats = 16
        self.confidence = 0.0
        self.anchor_beat = 0
        self._evidence = {8: 0.30, 16: 0.42, 32: 0.18}
        self._last_update = 0.0

    def observe(self, current_beat: int, segments: list[dict[str, Any]], now: float) -> None:
        if now - self._last_update < 0.9 or not segments:
            return
        self._last_update = now
        peaks = [s for s in segments if float(s.get("novelty") or 0.0) > 0.075]
        for L in self._evidence:
            score = 0.0
            for s in peaks:
                b = current_beat + int(s.get("beat_offset") or 0)
                off = min(b % L, L - (b % L))  # distance to nearest multiple
                score += max(0.0, 1.0 - off / 3.0) * _clamp(float(s.get("novelty") or 0.0) * 6.0)
            self._evidence[L] = 0.86 * self._evidence[L] + 0.14 * _clamp(score / max(1, len(peaks) or 1))
        total = sum(self._evidence.values()) or 1.0
        best = max(self._evidence, key=lambda k: self._evidence[k])
        self.phrase_beats = best
        self.confidence = _clamp(self._evidence[best] / total * 1.9)
        # Anchor to the strongest nearby novelty boundary so "phrase end" lands
        # on the audible structural change, not on beat-count arithmetic alone.
        if peaks:
            lead = max(peaks, key=lambda s: float(s.get("novelty") or 0.0))
            b = current_beat + int(lead.get("beat_offset") or 0)
            self.anchor_beat = b % self.phrase_beats

    def position(self, beat: int) -> dict[str, Any]:
        L = max(4, int(self.phrase_beats))
        rel = (beat - self.anchor_beat) % L
        return {
            "phrase_beats": L,
            "phrase_beat": int(rel),
            "phrase_bar": int(rel // 4),
            "bars_total": int(L // 4),
            "beats_to_boundary": int((L - rel) % L),
            "progress": round(rel / L, 3),
            "confidence": round(self.confidence, 3),
        }

    def quantize_to_boundary(self, beat: int, max_ahead: int = 24) -> int:
        """Snap a planned landing beat to the next phrase boundary if close."""
        L = max(4, int(self.phrase_beats))
        rel = (beat - self.anchor_beat) % L
        ahead = (L - rel) % L
        if ahead == 0:
            return beat
        if ahead <= max_ahead and self.confidence >= 0.45:
            return beat + ahead
        # Low confidence: fall back to the 4-beat bar grid.
        bar_ahead = (4 - (beat % 4)) % 4
        return beat + bar_ahead


class TensionField:
    """Scalar musical tension state (0..1), separate from energy.

    Charged by risers/reverse swells, rhythmic gating, filter displacement,
    echo feedback, loudness slope and structural novelty; discharged by drop /
    punch releases.  Exposes gating advice the planner uses so builds happen
    when there is room and releases only pay off when tension actually exists.
    """

    def __init__(self) -> None:
        self.tension = 0.0
        self.hunger = 0.0          # grows while tension stays high without release
        self._refractory_until = 0.0
        self._last_energy = 0.0
        self._last_t = 0.0

    def observe(self, controller: dict[str, Any], analysis: dict[str, Any], now: float) -> None:
        dt = _clamp(now - self._last_t, 0.0, 1.0) if self._last_t else 0.1
        self._last_t = now
        c = controller or {}; a = analysis or {}
        energy = float(a.get("energy") or 0.0)
        slope = _clamp((energy - self._last_energy) * 6.0, -1.0, 1.0)
        self._last_energy = energy
        drive = (
            0.26 * float(c.get("noise_riser") or 0.0)
            + 0.15 * float(c.get("fx_reverse_swell") or 0.0)
            + 0.13 * float(c.get("fx_gate") or 0.0)
            + 0.12 * abs(float(c.get("a_filter") or 0.0)) * 3.0
            + 0.08 * float(c.get("fx_echo_feedback") or 0.0)
            + 0.07 * float(c.get("fx_snare_rush") or 0.0)
            + 0.09 * max(0.0, slope)
            # v31.11: the build bass-strip and the rush ladder are the two
            # strongest produced-EDM tension signals of all (the floor is gone
            # and the roll is climbing the grid).
            + 0.22 * float(c.get("fx_bass_cut") or 0.0)
            + 0.12 * float(c.get("fx_rush_accel") or 0.0)
        )
        target = _clamp(drive)
        rate = 1.0 - math.exp(-dt / (0.85 if target > self.tension else 2.6))
        self.tension = _clamp(self.tension + (target - self.tension) * rate)
        if self.tension > 0.42:
            self.hunger = _clamp(self.hunger + dt * 0.10 * (self.tension - 0.42) / 0.58)
        else:
            self.hunger = _clamp(self.hunger - dt * 0.055)

    def notify_release(self, now: float, strength: float = 1.0) -> None:
        self.tension = _clamp(self.tension * (1.0 - 0.72 * _clamp(strength)))
        self.hunger = 0.0
        self._refractory_until = now + 6.0 + 8.0 * _clamp(strength)

    def in_refractory(self, now: float) -> bool:
        return now < self._refractory_until

    def release_readiness(self) -> float:
        """0..1: how much a drop/punch would 'pay off' right now."""
        return _clamp(0.15 + 0.60 * self.tension + 0.45 * self.hunger)

    def build_headroom(self) -> float:
        """0..1: how much room remains for another build gesture."""
        return _clamp(1.0 - 0.8 * self.tension)


class EnergyArc:
    """Rolling energy-arc target: RISE / HOLD / FALL with hysteresis.

    The prompt gives an intent; the measured medium-window energy trend and the
    future segment direction refine it.  The arc bias (-1..+1) tilts director
    archetype selection so choreography follows one coherent story instead of
    alternating randomly between lifting and dropping the floor.
    """

    def __init__(self) -> None:
        self.state = "HOLD"
        self.bias = 0.0
        self._trend = 0.0
        self._last_energy = None
        self._hold_until = 0.0

    def observe(self, policy_direction: str, analysis: dict[str, Any],
                segments: list[dict[str, Any]], now: float) -> None:
        energy = float((analysis or {}).get("energy") or 0.0)
        if self._last_energy is None:
            self._last_energy = energy
        self._trend = 0.90 * self._trend + 0.10 * _clamp((energy - self._last_energy) * 8.0, -1.0, 1.0)
        self._last_energy = energy
        seg_dir = 0.0
        if segments:
            seg_dir = float(sum(float(s.get("direction") or 0.0) for s in segments[:3])) / max(1, min(3, len(segments)))
        want = {"rise": 0.62, "fall": -0.62}.get(str(policy_direction or "hold"), 0.0)
        raw = _clamp(0.52 * want + 0.30 * _clamp(seg_dir * 4.0, -1, 1) + 0.18 * self._trend, -1.0, 1.0)
        self.bias = 0.85 * self.bias + 0.15 * raw
        if now >= self._hold_until:
            new_state = "RISE" if self.bias > 0.18 else ("FALL" if self.bias < -0.18 else "HOLD")
            if new_state != self.state:
                self.state = new_state
                self._hold_until = now + 7.0    # hysteresis: an arc is a story, not a tick


class HarmonicField:
    """Stabilized key tracking + harmonic-mixing context.

    Per-frame key estimates flicker; a DJ cares about the *stable* key and about
    modulations.  A small vote buffer produces a stable key, the Camelot label
    for the UI, and a Deck-B role hint based on circle-of-fifths distance
    between the current stable key and the upcoming section's tonal identity.
    """

    def __init__(self) -> None:
        self.stable_key = "—"
        self.camelot = "—"
        self.modulation = 0.0     # 0..1 evidence that key is changing
        self._votes: dict[str, float] = {}

    def observe(self, key_now: str) -> None:
        k = str(key_now or "—")
        if k == "—":
            return
        for name in list(self._votes):
            self._votes[name] *= 0.90
            if self._votes[name] < 0.02:
                del self._votes[name]
        self._votes[k] = self._votes.get(k, 0.0) + 1.0
        best = max(self._votes, key=lambda q: self._votes[q])
        if best != self.stable_key:
            if self._votes[best] > 1.8 * self._votes.get(self.stable_key, 0.01):
                self.modulation = _clamp(1.0 - key_compatibility(self.stable_key, best))
                self.stable_key = best
        else:
            self.modulation = _clamp(self.modulation * 0.94)
        self.camelot = camelot_label(self.stable_key)

    def deckb_role_hint(self, future_tonal_strength: float) -> str:
        """Advisory only; the realtime co-mix resolver stays authoritative."""
        if self.stable_key == "—":
            return "BALANCED"
        if float(future_tonal_strength or 0.0) < 0.22:
            return "PERCUSSIVE"
        if self.modulation > 0.45:
            return "PERCUSSIVE"   # during a modulation, tonal B would smear both keys
        return "HARMONIC" if float(future_tonal_strength or 0.0) > 0.48 else "BALANCED"


class SectionNarrator:
    """Functional-structure labels for the lookahead window (v31.11).

    Music-structure research assigns *functional* labels (intro/verse/chorus…);
    for a live DJ the useful vocabulary is coarser: LOW, GROOVE, BUILD, PEAK,
    BREAK.  The narrator labels the already-computed future segments and exposes
    the next section change, so choreography can be chosen for the boundary type
    (BUILD→PEAK wants a build+drop; PEAK→BREAK wants a brake/wash exit) instead
    of only for a novelty scalar.
    """

    def __init__(self) -> None:
        self.section_now = "—"
        self.section_next = "—"
        self.beats_to_section = 0
        self.boundary = "—"

    @staticmethod
    def _label(seg: dict[str, Any]) -> str:
        e = float(seg.get("energy") or 0.0)
        d = float(seg.get("drums") or 0.0)
        dr = float(seg.get("direction") or 0.0)
        if e >= 0.60 and d >= 0.48:
            return "PEAK"
        if e <= 0.28 or (e <= 0.40 and d <= 0.26):
            return "BREAK"
        if dr > 0.025 and e >= 0.30:
            return "BUILD"
        if e < 0.45 and d < 0.45:
            return "LOW"
        return "GROOVE"

    def observe(self, segments: list[dict[str, Any]]) -> None:
        if not segments:
            return
        self.section_now = self._label(segments[0])
        nxt = self.section_now
        beats = 0
        for s in segments[1:]:
            lab = self._label(s)
            if lab != self.section_now:
                nxt = lab
                beats = int(s.get("beat_offset") or 0)
                break
        self.section_next = nxt
        self.beats_to_section = int(beats)
        self.boundary = f"{self.section_now}→{self.section_next}" if nxt != self.section_now else self.section_now


class GestureGovernor:
    """Cooldown ledger so big releases keep their impact.

    Professional pacing: a drop every phrase is a drop nowhere.  Big-impact
    families get a minimum spacing in beats (relaxed at max authority); the
    planner receives a 0..1 penalty instead of a hard veto so an emergency
    musical opportunity can still override with sufficient margin.
    """

    BIG = {"DROP", "PARAM_DROP", "MACRO_DROP", "FX_IMPACT", "DECKB_DROP_FLASH", "PARAM_DECKB_DROP_FLASH"}

    def __init__(self) -> None:
        self._last_big_beat = -10_000

    def notify(self, kind: str, beat: int) -> None:
        k = str(kind or "").upper()
        if k in self.BIG or k.replace("PARAM_", "") in self.BIG:
            self._last_big_beat = int(beat)

    def big_gesture_penalty(self, beat: int, authority: float = 1.0) -> float:
        spacing = 24 - 8 * _clamp((float(authority) - 1.0) / 2.60)   # 24 beats normal, 16 at max
        since = beat - self._last_big_beat
        if since >= spacing:
            return 0.0
        return _clamp(1.0 - since / max(1.0, spacing))

    def beats_since_big(self, beat: int) -> int:
        return int(beat - self._last_big_beat)


class SongMemory:
    """Song-level structural memory (v31.16 SongMind).

    The 15 s lookahead window can only see ~7 bars.  A DJ, however, learns the
    FORM of a record while it plays: once the first chorus has passed, the next
    one is predictable.  This is the online version of self-similarity-based
    music structure analysis (Foote novelty / lag-matrix repetition detection,
    All-in-One-style joint section reasoning) without any model call:

      * one feature vector per BAR (energy, drums, bass, vocal, melody, tonal
        strength + 12-bin chroma), written as the lookahead window reveals bars;
      * an online self-similarity search: the newest bars are matched against
        every earlier position; a diagonal run of >= 3 similar bars means "we are
        replaying something we already heard" (repeat_of);
      * the boundaries that FOLLOWED the earlier occurrence are projected onto
        the current position -> predicted section boundaries beyond the window,
        with confidence from run length and similarity;
      * 8/16/32-beat phrase-length evidence from the lags at which repetition
        occurs (repetition lags in real records are phrase multiples).
    """

    MAX_BARS = 720          # ~24 minutes at 120 BPM
    FEAT_DIM = 18

    def __init__(self) -> None:
        self.bars: dict[int, list[float]] = {}         # absolute bar index -> feature vector
        self.labels: dict[int, str] = {}
        self.novelty: dict[int, float] = {}
        self.repeat_of: int | None = None
        self.repeat_run: int = 0
        self.repeat_sim: float = 0.0
        self.predicted: list[dict[str, Any]] = []      # [{"beat": int, "type": str, "conf": float, "src": str}]
        self.phrase_votes: dict[int, float] = {8: 0.0, 16: 0.0, 32: 0.0}
        self.form: list[str] = []
        self._last_scan_bar = -1

    @staticmethod
    def _vec(seg: dict[str, Any]) -> list[float]:
        chroma = list(seg.get("chroma") or [])
        if len(chroma) != 12:
            chroma = [1.0 / 12.0] * 12
        return [float(seg.get("energy") or 0.0), float(seg.get("drums") or 0.0), float(seg.get("bass") or 0.0),
                float(seg.get("vocal") or 0.0), float(seg.get("melody") or 0.0), float(seg.get("tonal_strength") or 0.0)] + [float(c) for c in chroma]

    def ingest(self, current_beat: int, segments: list[dict[str, Any]], labels_fn=None) -> None:
        """Write the bars revealed by the lookahead window (absolute bar index)."""
        for s in segments or []:
            bar = int((current_beat + int(s.get("beat_offset") or 0)) // 4)
            if bar < 0:
                continue
            v = self._vec(s)
            if bar in self.bars:
                old = self.bars[bar]
                self.bars[bar] = [0.5 * a + 0.5 * b for a, b in zip(old, v)]
            else:
                self.bars[bar] = v
            self.novelty[bar] = max(self.novelty.get(bar, 0.0), float(s.get("novelty") or 0.0))
            if labels_fn is not None:
                try:
                    self.labels[bar] = str(labels_fn(s))
                except Exception:
                    pass
        if len(self.bars) > self.MAX_BARS:
            for k in sorted(self.bars)[: len(self.bars) - self.MAX_BARS]:
                self.bars.pop(k, None); self.labels.pop(k, None); self.novelty.pop(k, None)

    @staticmethod
    def _sim(a: list[float], b: list[float]) -> float:
        # weighted cosine: activity block and chroma block scored separately
        def cos(x, y):
            nx = math.sqrt(sum(v * v for v in x)) + 1e-9
            ny = math.sqrt(sum(v * v for v in y)) + 1e-9
            return sum(p * q for p, q in zip(x, y)) / (nx * ny)
        act = 1.0 - min(1.0, sum(abs(p - q) for p, q in zip(a[:6], b[:6])) / 3.0)
        chr_ = cos(a[6:], b[6:])
        return 0.55 * act + 0.45 * chr_

    def scan(self, current_beat: int) -> None:
        """Find whether the newest bars replay an earlier passage; predict."""
        if not self.bars:
            return
        latest = max(self.bars)
        if latest == self._last_scan_bar:
            return
        self._last_scan_bar = latest
        keys = sorted(self.bars)
        if len(keys) < 12:
            return
        # newest window of 4 bars (as they stand in memory)
        win = [k for k in keys if latest - 3 <= k <= latest]
        if len(win) < 3:
            return
        best = (0.0, None, 0)
        for cand_end in keys:
            lag = latest - cand_end
            if lag < 8:            # a repeat must be at least two bars-of-four back
                continue
            run = 0; acc = 0.0
            for j, wb in enumerate(reversed(win)):
                eb = cand_end - j
                if eb not in self.bars:
                    break
                sim = self._sim(self.bars[wb], self.bars[eb])
                if sim < 0.78:
                    break
                run += 1; acc += sim
            if run >= 3:
                score = acc / run + 0.03 * run
                # repetitions in real records sit on phrase multiples (4/8 bars);
                # prefer those lags over an off-by-one match inside a homogeneous section
                if lag % 8 == 0: score += 0.10
                elif lag % 4 == 0: score += 0.06
                if score > best[0]:
                    best = (score, cand_end, run)
        score, cand_end, run = best
        if cand_end is None:
            self.repeat_of = None; self.repeat_run = 0; self.repeat_sim = 0.0
            self.predicted = self._novelty_predictions(current_beat, latest)
            return
        lag = latest - cand_end
        self.repeat_of = cand_end; self.repeat_run = run; self.repeat_sim = round(score, 3)
        for L in (8, 16, 32):
            if (lag * 4) % L == 0:
                self.phrase_votes[L] += 1.0 * (1.0 if L != 32 else 1.4)
        # project the boundaries that followed the earlier occurrence
        preds = []
        for k in keys:
            if k <= cand_end or k > cand_end + 32:
                continue
            nov = self.novelty.get(k, 0.0)
            lab_prev = self.labels.get(k - 1, "")
            lab = self.labels.get(k, "")
            if nov > 0.075 or (lab and lab_prev and lab != lab_prev):
                fut_bar = k + lag
                beat = fut_bar * 4
                if beat <= current_beat + 2:
                    continue
                conf = min(0.95, 0.45 + 0.12 * run + 0.35 * (score - 0.78))
                preds.append({"beat": int(beat), "bar": int(fut_bar), "type": lab or "SHIFT",
                              "conf": round(conf, 3), "src": f"repeat of bar {k} (lag {lag})"})
        self.predicted = preds[:6] if preds else self._novelty_predictions(current_beat, latest)
        # keep a compact form string
        self.form = self._form_summary(keys)

    def _novelty_predictions(self, current_beat: int, latest: int) -> list[dict[str, Any]]:
        out = []
        for k in sorted(self.novelty):
            if k * 4 <= current_beat + 2:
                continue
            if self.novelty[k] > 0.075:
                out.append({"beat": int(k * 4), "bar": int(k), "type": self.labels.get(k, "SHIFT"),
                            "conf": round(min(0.8, 0.3 + 3.0 * self.novelty[k]), 3), "src": "lookahead novelty"})
        return out[:6]

    def _form_summary(self, keys: list[int]) -> list[str]:
        out = []; prev = None
        for k in keys:
            lab = self.labels.get(k)
            if lab and lab != prev:
                out.append(f"{lab}@{k}")
                prev = lab
        return out[-12:]

    def phrase_length(self) -> tuple[int, float]:
        tot = sum(self.phrase_votes.values())
        if tot <= 0:
            return 16, 0.0
        L = max(self.phrase_votes, key=lambda k: self.phrase_votes[k])
        return L, min(1.0, self.phrase_votes[L] / tot * min(1.0, tot / 4.0))

    def next_predicted(self, current_beat: int, min_ahead: int = 2) -> dict[str, Any] | None:
        for p in sorted(self.predicted, key=lambda q: q["beat"]):
            if p["beat"] >= current_beat + min_ahead:
                return p
        return None

    def snapshot(self) -> dict[str, Any]:
        L, conf = self.phrase_length()
        return {"bars_known": len(self.bars), "repeat_of": self.repeat_of, "repeat_run": self.repeat_run,
                "repeat_sim": self.repeat_sim, "predicted": list(self.predicted[:4]),
                "phrase_len": L, "phrase_len_conf": round(conf, 3), "form": list(self.form)}


class TransitionLegality:
    """WHEN may a rise / landing / exit / weave begin?  (v31.16 SongMind)

    DJ phrasing practice: every major move — first fader up, bass swap, the
    drop, the final fader down — lands on a phrase boundary; a build of N beats
    must therefore START exactly N beats before a legal landing.  This class
    turns that craft rule plus the musical state into explicit, scored legal
    beats per gesture family, and the Director / arranger may not plan outside
    them.  Inputs are all advisory contexts the agent already computes; the
    output is deterministic and explainable (each legal beat carries reasons).
    """

    FAMILIES = ("LANDING", "RISE4", "RISE8", "RISE16", "EXIT", "WEAVE", "BASS_SWAP")

    def __init__(self) -> None:
        self.last: dict[str, Any] = {"landings": [], "windows": {}, "reasons": []}

    @staticmethod
    def _vocal_at(segments: list[dict[str, Any]], current_beat: int, beat: int, default: float) -> float:
        best = None
        for s in segments or []:
            b0 = current_beat + int(s.get("beat_offset") or 0)
            if b0 <= beat:
                best = float(s.get("vocal") or 0.0)
        return default if best is None else best

    def compute(self, *, current_beat: int, phrase_beats: int, phrase_anchor: int, phrase_conf: int | float,
                segments: list[dict[str, Any]], section_now: str, section_next: str, beats_to_section: int,
                predicted: list[dict[str, Any]], readiness: float, headroom: float, refractory: bool,
                big_penalty_fn, grid_conf: float, key_modulation: float, arc: str, vocal_now: float,
                authority: float = 1.0, horizon: int = 64) -> dict[str, Any]:
        L = max(4, int(phrase_beats))
        reasons: list[str] = []
        # ---- candidate landing beats: phrase boundaries in the horizon
        landings: list[dict[str, Any]] = []
        first = current_beat + ((phrase_anchor - current_beat) % L)
        if first <= current_beat + 1:
            first += L
        b = first
        while b <= current_beat + horizon:
            landings.append({"beat": int(b), "score": 0.55 + 0.25 * float(phrase_conf), "why": ["phrase boundary"]})
            b += L
        # section boundary from the lookahead narrator (near horizon)
        if beats_to_section and beats_to_section > 1:
            sb = current_beat + int(beats_to_section)
            snapped = min(landings, key=lambda q: abs(q["beat"] - sb)) if landings else None
            if snapped is not None and abs(snapped["beat"] - sb) <= 2:
                snapped["score"] += 0.30; snapped["why"].append(f"section {section_now}->{section_next}")
                snapped["type"] = section_next
            else:
                landings.append({"beat": int(sb), "score": 0.62, "why": [f"section {section_now}->{section_next} (off-grid)"], "type": section_next})
        # predicted boundaries from song memory (far horizon)
        for p in predicted or []:
            pb = int(p.get("beat") or 0)
            if pb <= current_beat + 1 or pb > current_beat + horizon:
                continue
            snapped = min(landings, key=lambda q: abs(q["beat"] - pb)) if landings else None
            if snapped is not None and abs(snapped["beat"] - pb) <= 2:
                snapped["score"] += 0.25 * float(p.get("conf") or 0.5); snapped["why"].append(f"memory: {p.get('src')}")
                snapped.setdefault("type", str(p.get("type") or "SHIFT"))
            else:
                landings.append({"beat": pb, "score": 0.40 + 0.3 * float(p.get("conf") or 0.5), "why": [f"memory: {p.get('src')}"], "type": str(p.get("type") or "SHIFT")})
        # ---- gating that applies to all beat-critical gestures
        grid_ok = float(grid_conf) >= 0.45
        if not grid_ok:
            reasons.append("grid confidence low: beat-critical gestures deferred")
        pen_fn = big_penalty_fn if callable(big_penalty_fn) else (lambda beat, auth=1.0: 0.0)
        for ld in landings:
            ld["score"] -= 0.35 * float(pen_fn(ld["beat"], authority))
            vocal_l = self._vocal_at(segments, current_beat, ld["beat"], vocal_now)
            ld["vocal"] = round(vocal_l, 2)
            if not grid_ok:
                ld["score"] -= 0.4
            if str(section_next) == "PEAK" and ld.get("type") in ("PEAK", None):
                ld["score"] += 0.05
            ld["score"] = round(ld["score"], 3)
        landings.sort(key=lambda q: (-q["score"], q["beat"]))
        # ---- family windows
        windows: dict[str, list[dict[str, Any]]] = {f: [] for f in self.FAMILIES}
        for ld in landings:
            lb = ld["beat"]
            # LANDING (drop / punch): needs tension to spend, no refractory, grid ok
            if grid_ok and not refractory and readiness >= 0.42:
                windows["LANDING"].append({"beat": lb, "score": round(ld["score"] + 0.4 * (readiness - 0.42), 3), "why": ld["why"] + [f"readiness {readiness:.2f}"]})
            # RISES: must end ON the landing -> start = landing - N; need build headroom
            for n_beats, fam in ((4, "RISE4"), (8, "RISE8"), (16, "RISE16")):
                start = lb - n_beats
                if start < current_beat + 2:
                    continue
                if headroom < 0.30 or refractory:
                    continue
                if not grid_ok:
                    continue
                v = self._vocal_at(segments, current_beat, start, vocal_now)
                depth_cap = 1.0 - 0.45 * max(0.0, (v - 0.58) / 0.42)
                sc = ld["score"] + 0.25 * headroom - (0.15 if str(arc) == "FALL" else 0.0)
                if str(section_next) == "PEAK" or ld.get("type") == "PEAK":
                    sc += 0.15
                windows[fam].append({"beat": int(start), "landing": lb, "score": round(sc, 3), "depth_cap": round(depth_cap, 2), "why": ld["why"] + [f"ends on landing {lb}", f"headroom {headroom:.2f}"]})
            # EXIT (wash / brake / echo-spin): at boundaries into lower energy, never mid-vocal-phrase
            if str(arc) != "RISE" and ld["vocal"] < 0.60 and grid_ok:
                sc = ld["score"] + (0.2 if ld.get("type") in ("BREAK", "LOW") else 0.0)
                windows["EXIT"].append({"beat": lb, "score": round(sc, 3), "why": ld["why"] + ["energy not rising", f"vocal {ld['vocal']}"]})
            # WEAVE (interleave / jumpback / roll ladders): chops audio -> no foreground vocal
            if ld["vocal"] < 0.55 and grid_ok:
                windows["WEAVE"].append({"beat": lb - 8 if lb - 8 > current_beat + 2 else lb, "landing": lb, "score": round(ld["score"], 3), "why": ld["why"] + ["vocal clear"]})
            # BASS_SWAP: harmonic layers illegal during a modulation
            if key_modulation < 0.45 and grid_ok:
                windows["BASS_SWAP"].append({"beat": lb, "score": round(ld["score"], 3), "why": ld["why"] + [f"key stable ({1-key_modulation:.2f})"]})
        for fam in windows:
            windows[fam].sort(key=lambda q: (-q["score"], q["beat"]))
            windows[fam] = windows[fam][:4]
        if refractory:
            reasons.append("refractory after a release: no landing")
        if readiness < 0.42:
            reasons.append(f"tension too low for a landing ({readiness:.2f})")
        if headroom < 0.30:
            reasons.append(f"no headroom for another build ({headroom:.2f})")
        self.last = {"landings": landings[:6], "windows": windows, "reasons": reasons,
                     "best_landing": (landings[0]["beat"] if landings else None),
                     "best_rise": (windows["RISE8"][0] if windows["RISE8"] else (windows["RISE4"][0] if windows["RISE4"] else None))}
        return self.last

    def is_legal(self, family: str, beat: int, tol: int = 0) -> bool:
        for w in (self.last.get("windows") or {}).get(family, []):
            if abs(int(w["beat"]) - int(beat)) <= tol:
                return True
        return False


class MusicalIntelligence:
    """Facade wiring the five components to the agent loop."""

    def __init__(self) -> None:
        self.phrase = PhraseClock()
        self.tension = TensionField()
        self.arc = EnergyArc()
        self.harmony = HarmonicField()
        self.governor = GestureGovernor()
        self.narrator = SectionNarrator()
        self.song = SongMemory()
        self.legality = TransitionLegality()
        self._beat = 0

    def observe(self, *, beat: int, controller: dict[str, Any], analysis: dict[str, Any],
                segments: list[dict[str, Any]], policy_direction: str, now: float | None = None) -> None:
        t = time.monotonic() if now is None else float(now)
        self._beat = int(beat)
        self.phrase.observe(beat, segments or [], t)
        self.tension.observe(controller or {}, analysis or {}, t)
        self.arc.observe(policy_direction, analysis or {}, segments or [], t)
        self.harmony.observe(str((analysis or {}).get("key") or "—"))
        self.narrator.observe(segments or [])
        # v31.16 SongMind: learn the song's form as it streams, predict beyond
        # the lookahead window, then compute the legal windows for every gesture.
        self.song.ingest(int(beat), segments or [], labels_fn=SectionNarrator._label)
        self.song.scan(int(beat))
        pos = self.phrase.position(int(beat))
        song_L, song_conf = self.song.phrase_length()
        phrase_beats = int(song_L if song_conf > 0.5 and song_conf > self.phrase.confidence else pos["phrase_beats"])
        self.legality.compute(
            current_beat=int(beat), phrase_beats=phrase_beats, phrase_anchor=int(self.phrase.anchor_beat),
            phrase_conf=max(self.phrase.confidence, song_conf), segments=segments or [],
            section_now=self.narrator.section_now, section_next=self.narrator.section_next,
            beats_to_section=int(self.narrator.beats_to_section), predicted=self.song.predicted,
            readiness=self.tension.release_readiness(), headroom=self.tension.build_headroom(),
            refractory=self.tension.in_refractory(t), big_penalty_fn=self.governor.big_gesture_penalty,
            grid_conf=float((analysis or {}).get("bpm_confidence") or 0.0), key_modulation=float(self.harmony.modulation),
            arc=self.arc.state, vocal_now=float((analysis or {}).get("vocal_activity") or 0.0))

    def notify_event(self, kind: str, beat: int, now: float | None = None) -> None:
        t = time.monotonic() if now is None else float(now)
        k = str(kind or "").upper()
        self.governor.notify(k, beat)
        if k in GestureGovernor.BIG or k.replace("PARAM_", "") in GestureGovernor.BIG:
            self.tension.notify_release(t)

    def snapshot(self, future_tonal_strength: float = 0.0) -> dict[str, Any]:
        pos = self.phrase.position(self._beat)
        t = time.monotonic()
        return {
            **pos,
            "tension": round(self.tension.tension, 3),
            "tension_hunger": round(self.tension.hunger, 3),
            "release_readiness": round(self.tension.release_readiness(), 3),
            "build_headroom": round(self.tension.build_headroom(), 3),
            "refractory": bool(self.tension.in_refractory(t)),
            "arc": self.arc.state,
            "arc_bias": round(self.arc.bias, 3),
            "stable_key": self.harmony.stable_key,
            "camelot": self.harmony.camelot,
            "modulation": round(self.harmony.modulation, 3),
            "deckb_role_hint": self.harmony.deckb_role_hint(future_tonal_strength),
            "beats_since_big": self.governor.beats_since_big(self._beat),
            "big_penalty": round(self.governor.big_gesture_penalty(self._beat), 3),
            "section_now": self.narrator.section_now,
            "section_next": self.narrator.section_next,
            "section_boundary": self.narrator.boundary,
            "beats_to_section": int(self.narrator.beats_to_section),
            "song": self.song.snapshot(),
            "legality": {"best_landing": self.legality.last.get("best_landing"),
                         "best_rise": self.legality.last.get("best_rise"),
                         "landings": [{"beat": l["beat"], "score": l["score"], "type": l.get("type"), "why": l["why"][:2]} for l in self.legality.last.get("landings", [])[:4]],
                         "windows": {k: [{"beat": w["beat"], "score": w["score"], "landing": w.get("landing")} for w in v[:2]] for k, v in (self.legality.last.get("windows") or {}).items()},
                         "reasons": list(self.legality.last.get("reasons", []))[:3]},
        }


__all__ = [
    "MusicalIntelligence", "PhraseClock", "TensionField", "EnergyArc",
    "HarmonicField", "GestureGovernor", "SectionNarrator", "SongMemory", "TransitionLegality",
    "key_compatibility", "camelot_label",
]
