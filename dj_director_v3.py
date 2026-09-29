from __future__ import annotations

"""JoyMetric DJ Director V4 (compatibility filename dj_director_v3.py).

A lightweight, no-fine-tune planning layer that converts bar-scale future audio
state into several candidate DJ choreographies, scores them against musical
structure + prompt intent + recent performance memory, then emits parameterized
controller gestures.  It intentionally contains no audio DSP and no model call;
it is safe to run out-of-band beside the realtime audio engine.
"""

from dataclasses import dataclass, asdict
from typing import Any
import math
import random


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


DESTRUCTIVE = {"ROLL_ACCEL", "ROLL_HALF", "ROLL_QUARTER", "TRANS_GATE", "DRUM_FILL", "DECKB_SWAP", "DECKB_SLICE_CALL", "DECKB_SLICE_ACCEL", "DECKB_ECHO_FREEZE", "DECKB_PICKUP_STUTTER", "DECKB_DROP_FLASH", "BRAKE_STOP", "ROLL_LADDER", "TIME_INTERLEAVE", "JUMPBACK"}

# Families that release accumulated tension (drop-grammar landings) and families
# that build it.  Used with the MusicalIntelligence tension field so a landing
# only fires when there is tension to spend (ISMIR 2014 drop grammar).
RELEASE_FAMILIES = {"DROP", "PUNCH_IN", "DECKB_DROP_FLASH"}
BUILD_FAMILIES = {"TENSION", "RISER", "FILTER_SWEEP", "TRANS_GATE", "DRUM_FILL", "AB_CONTRARY_RISE", "BASS_SWAP_GLIDE", "QUANT_BUILD", "ROLL_LADDER"}

# v31.11 StageCraft showmanship families â€” the canonical live-DJ crowd moves
# (backspin/brake section marker, sidechain pump, produced build, roll ladder).
# v31.14 adds the TimeWeave vocabulary (gestures on the time axis itself).
SHOWMANSHIP_FAMILIES = {"SPINBACK", "BRAKE_STOP", "PUMP_GROOVE", "QUANT_BUILD", "ROLL_LADDER",
                        "FLASH_FORWARD", "JUMPBACK", "TIME_INTERLEAVE"}
TIMEWEAVE_FAMILIES = {"FLASH_FORWARD", "JUMPBACK", "TIME_INTERLEAVE"}

FAMILY_TO_MACRO = {
    "TENSION": "MACRO_TENSION",
    "FILTER_SWEEP": "MACRO_FILTER_SWEEP",
    "TRANS_GATE": "MACRO_TRANS_BUILD",
    "DRUM_FILL": "MACRO_DRUM_FILL",
    "ROLL_ACCEL": "MACRO_ROLL_ACCEL",
    "ROLL_HALF": "MACRO_ROLL_HALF",
    "ROLL_QUARTER": "MACRO_ROLL_QUARTER",
    "ECHO_HALF": "MACRO_ECHO_HALF",
    "ECHO_ONE": "MACRO_ECHO_ONE",
    "ECHO_OUT": "MACRO_ECHO_OUT",
    "WASH_OUT": "MACRO_WASH_OUT",
    "SPACE_BREAK": "MACRO_SPACE_BREAK",
    "GROOVE_LIFT": "MACRO_GROOVE_LIFT",
    "PUNCH_IN": "MACRO_PUNCH_IN",
    "DROP": "MACRO_DROP",
    "CLEAN_RESET": "MACRO_CLEAN_RESET",
    "RISER": "MACRO_TENSION",
    "REVERSE_SWELL": "MACRO_WASH_OUT",
    "DECKB_GHOST": "MACRO_GROOVE_LIFT",
    "DECKB_DRUM_LAYER": "MACRO_GROOVE_LIFT",
    "DECKB_HARMONIC_BED": "MACRO_GROOVE_LIFT",
    "DECKB_TEASE": "MACRO_ECHO_HALF",
    "DECKB_SWAP": "MACRO_ROLL_HALF",
    "DECKB_MICRO_SHIFT": "MACRO_GROOVE_LIFT",
    "DECKB_RETURN": "MACRO_CLEAN_RESET",
    "DECKB_SLICE_GROOVE": "MACRO_GROOVE_LIFT",
    "DECKB_SLICE_FILL": "MACRO_DRUM_FILL",
    "DECKB_SLICE_CALL": "MACRO_ECHO_HALF",
    "DECKB_SLICE_ACCEL": "MACRO_ROLL_ACCEL",
    "DECKB_MOTIF_TEASE": "MACRO_GROOVE_LIFT",
    "DECKB_COUNTER_GROOVE": "MACRO_GROOVE_LIFT",
    "DECKB_ECHO_FREEZE": "MACRO_ECHO_HALF",
    "DECKB_PICKUP_STUTTER": "MACRO_DRUM_FILL",
    "DECKB_HOOK_CALL": "MACRO_ECHO_HALF",
    "DECKB_DROP_FLASH": "MACRO_PUNCH_IN",
    "DECKB_RELEASE": "MACRO_CLEAN_RESET",
    # v31.10 contrary-motion transition grammar: one deck rises while the other
    # falls, rendered as opposing control-rate automation lanes.
    "AB_CONTRARY_RISE": "MACRO_GROOVE_LIFT",
    "AB_CONTRARY_FALL": "MACRO_WASH_OUT",
    "BASS_SWAP_GLIDE": "MACRO_GROOVE_LIFT",
    "AB_BREATH": "MACRO_SPACE_BREAK",
    # v31.11 StageCraft â€” the live-DJ showmanship vocabulary.
    "SPINBACK": "MACRO_GROOVE_LIFT",
    "BRAKE_STOP": "MACRO_WASH_OUT",
    "PUMP_GROOVE": "MACRO_GROOVE_LIFT",
    "QUANT_BUILD": "MACRO_TENSION",
    "ROLL_LADDER": "MACRO_GROOVE_LIFT",
    # v31.14 TimeWeave â€” gestures on the time axis itself.
    "FLASH_FORWARD": "MACRO_GROOVE_LIFT",
    "JUMPBACK": "MACRO_GROOVE_LIFT",
    "TIME_INTERLEAVE": "MACRO_GROOVE_LIFT",
}

# Human-readable action schema. Every family carries continuous parameters, so
# the expressive space is not limited to the number of discrete family names.
PARAM_SCHEMAS = {
    "TENSION": ("filter_depth", "riser", "reverse", "width", "echo_send", "duration_beats"),
    "FILTER_SWEEP": ("depth", "riser", "width", "duration_beats", "curve"),
    "TRANS_GATE": ("depth", "division", "drum_drive", "duration_beats"),
    "DRUM_FILL": ("snare", "drum_drive", "doubletime", "duration_beats"),
    "ROLL_ACCEL": ("start_beats", "end_beats", "xfade", "gate", "acceleration", "duration_beats"),
    "ROLL_HALF": ("xfade", "duration_beats"),
    "ROLL_QUARTER": ("xfade", "duration_beats"),
    "ECHO_HALF": ("send", "feedback", "width", "duck", "duration_beats"),
    "ECHO_ONE": ("send", "feedback", "width", "duck", "duration_beats"),
    "ECHO_OUT": ("send", "feedback", "width", "duck", "tail_beats"),
    "WASH_OUT": ("reverse", "reverb", "width", "filter", "duration_beats"),
    "SPACE_BREAK": ("reverse", "reverb", "width", "echo_send", "duration_beats"),
    "GROOVE_LIFT": ("punch", "energy", "drum_drive", "air", "duration_beats"),
    "PUNCH_IN": ("punch", "energy", "drum_drive", "clarity", "duration_beats"),
    "DROP": ("impact", "punch", "energy", "air", "duration_beats"),
    "CLEAN_RESET": ("tail_keep", "duration_beats"),
    "RISER": ("riser", "filter_depth", "width", "duration_beats"),
    "REVERSE_SWELL": ("reverse", "reverb", "width", "duration_beats"),
    "DECKB_GHOST": ("loop_beats","layer","texture","transient","b_low_db","duration_beats"),
    "DECKB_DRUM_LAYER": ("loop_beats","layer","texture","transient","b_low_db","b_mid_db","duration_beats"),
    "DECKB_HARMONIC_BED": ("loop_beats","layer","texture","transient","b_low_db","b_high_db","duration_beats"),
    "DECKB_TEASE": ("loop_beats","layer","texture","transient","echo_send","feedback","duration_beats"),
    "DECKB_SWAP": ("loop_beats","xfade","texture","transient","b_low_db","duration_beats"),
    "DECKB_MICRO_SHIFT": ("xfade","layer","b_fx","duration_beats"),
    "DECKB_RETURN": ("duration_beats",),
    "DECKB_SLICE_GROOVE": ("loop_beats","layer","slice_mix","b_fx","texture","transient","duration_beats"),
    "DECKB_SLICE_FILL": ("loop_beats","layer","slice_mix","b_fx","texture","transient","duration_beats"),
    "DECKB_SLICE_CALL": ("loop_beats","layer","slice_mix","b_fx","texture","transient","duration_beats"),
    "DECKB_SLICE_ACCEL": ("loop_beats","layer","slice_mix","b_fx","texture","transient","duration_beats"),
    "DECKB_MOTIF_TEASE": ("loop_beats","layer","slice_mix","b_fx","texture","transient","echo_send","duration_beats"),
    "DECKB_COUNTER_GROOVE": ("loop_beats","layer","slice_mix","b_fx","texture","transient","duration_beats"),
    "DECKB_ECHO_FREEZE": ("loop_beats","layer","slice_mix","b_fx","texture","transient","echo_send","feedback","duration_beats"),
    "DECKB_PICKUP_STUTTER": ("loop_beats","layer","slice_mix","b_fx","texture","transient","duration_beats"),
    "DECKB_HOOK_CALL": ("loop_beats","layer","slice_mix","b_fx","texture","transient","echo_send","duration_beats"),
    "DECKB_DROP_FLASH": ("loop_beats","xfade","layer","b_fx","texture","transient","duration_beats"),
    "DECKB_RELEASE": ("duration_beats",),
    "AB_CONTRARY_RISE": ("depth","riser","layer","b_open","a_close","bass_handoff","duration_beats","curve"),
    "AB_CONTRARY_FALL": ("depth","reverse","a_open","b_close","reverb","duration_beats","curve"),
    "BASS_SWAP_GLIDE": ("handoff","layer","hold_beats","duration_beats"),
    "AB_BREATH": ("width","reverb","echo_send","depth","duration_beats"),
    "SPINBACK": ("beats","duration_beats"),
    "BRAKE_STOP": ("beats","duration_beats"),
    "PUMP_GROOVE": ("depth","cycle_beats","duration_beats"),
    "QUANT_BUILD": ("depth","riser","bass_strip","rush","duration_beats"),
    "ROLL_LADDER": ("xfade","start_beats","duration_beats"),
    "FLASH_FORWARD": ("beats","offset_beats","duration_beats"),
    "JUMPBACK": ("beats","offset_beats","duration_beats"),
    "TIME_INTERLEAVE": ("beats","offset_beats","cell_beats","duration_beats"),
}


@dataclass
class Gesture:
    beat: int
    family: str
    intensity: float
    params: dict[str, Any]
    rationale: str

    def event(self, score: float, source: str = "DIRECTOR V4") -> dict[str, Any]:
        macro = FAMILY_TO_MACRO[self.family]
        return {
            "beat": int(self.beat),
            "bar": int(self.beat) // 4,
            "kind": f"PARAM_{self.family}",
            "action": f"PARAM_{self.family}",
            "family": self.family,
            "macro": macro,
            "params": dict(self.params),
            "intensity": round(float(self.intensity), 4),
            "deck": "CONCERT CONTROLLER V3",
            "value": round(float(score), 4),
            "score": round(float(score), 4),
            "rationale": self.rationale,
            "source": source,
        }


@dataclass
class CandidatePlan:
    name: str
    archetype: str
    gestures: list[Gesture]
    components: dict[str, float]
    score: float = 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "archetype": self.archetype,
            "score": round(self.score, 4),
            "components": {k: round(float(v), 4) for k, v in self.components.items()},
            "families": [g.family for g in self.gestures],
        }


class DJDirectorV3:
    """Candidate-search DJ policy with explicit performance memory.

    The director does not need fine-tuning: it creates several parameterized
    phrase plans and chooses among them using a critic. Its state is deterministic
    enough for debugging but varies candidate parameters based on musical context
    and recent action history.
    """

    def __init__(self) -> None:
        self.selected: dict[str, Any] = {}
        self.candidates: list[dict[str, Any]] = []
        self.plan_counter = 0

    @staticmethod
    def _history_families(history: list[dict[str, Any]], current_beat: int, horizon: int = 128) -> list[str]:
        out: list[str] = []
        for h in history[-64:]:
            try:
                b = int(h.get("beat", -99999))
            except Exception:
                b = -99999
            if b < current_beat - horizon:
                continue
            fam = str(h.get("family") or h.get("kind") or "").replace("PARAM_", "").replace("MACRO_", "")
            if fam:
                out.append(fam)
        return out

    @staticmethod
    def _dominant_boundary(segments: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not segments:
            return None
        ranked = sorted(segments[1:] or segments, key=lambda s: float(s.get("novelty") or 0.0) + 0.55 * abs(float(s.get("direction") or 0.0)), reverse=True)
        return ranked[0] if ranked else None

    @staticmethod
    def _style_weights(style: str, morph: str) -> dict[str, float]:
        s = str(style or "balanced")
        if morph == "dnb" or "drum" in s:
            return {"ROLL_ACCEL": .15, "DRUM_FILL": .14, "DROP": .16, "TRANS_GATE": .08, "FILTER_SWEEP": .06, "ECHO_OUT": .04, "DECKB_DRUM_LAYER":.16, "DECKB_GHOST":.10, "DECKB_SWAP":.08, "DECKB_MICRO_SHIFT":.16, "DECKB_SLICE_GROOVE":.18, "DECKB_SLICE_FILL":.20, "DECKB_SLICE_ACCEL":.22, "AB_CONTRARY_RISE":.16, "BASS_SWAP_GLIDE":.10, "QUANT_BUILD":.20, "ROLL_LADDER":.20, "SPINBACK":.14, "PUMP_GROOVE":.10, "FLASH_FORWARD":.14, "TIME_INTERLEAVE":.12, "JUMPBACK":.10}
        if s == "club":
            return {"FILTER_SWEEP": .13, "TRANS_GATE": .10, "ECHO_OUT": .12, "TENSION": .08, "DROP": .10, "GROOVE_LIFT": .08, "DECKB_GHOST":.11, "DECKB_TEASE":.10, "DECKB_SWAP":.07, "DECKB_MICRO_SHIFT":.14, "DECKB_SLICE_GROOVE":.18, "DECKB_SLICE_FILL":.16, "DECKB_SLICE_CALL":.15, "AB_CONTRARY_RISE":.18, "AB_CONTRARY_FALL":.12, "BASS_SWAP_GLIDE":.16, "QUANT_BUILD":.20, "PUMP_GROOVE":.18, "SPINBACK":.14, "BRAKE_STOP":.08, "ROLL_LADDER":.12, "FLASH_FORWARD":.17, "TIME_INTERLEAVE":.14, "JUMPBACK":.10}
        if s == "dreamy":
            return {"WASH_OUT": .16, "SPACE_BREAK": .15, "REVERSE_SWELL": .12, "ECHO_ONE": .10, "TENSION": .06, "DECKB_HARMONIC_BED":.18, "DECKB_TEASE":.10, "DECKB_MICRO_SHIFT":.08, "DECKB_SLICE_CALL":.08, "AB_CONTRARY_FALL":.16, "AB_BREATH":.16, "BASS_SWAP_GLIDE":.12, "PUMP_GROOVE":.08, "BRAKE_STOP":.06, "FLASH_FORWARD":.10, "TIME_INTERLEAVE":.10, "JUMPBACK":.08}
        if s == "hip-hop":
            return {"PUNCH_IN": .14, "ECHO_HALF": .11, "DRUM_FILL": .09, "FILTER_SWEEP": .08, "DROP": .10, "GROOVE_LIFT": .08, "DECKB_GHOST":.13, "DECKB_DRUM_LAYER":.13, "DECKB_TEASE":.10, "DECKB_MICRO_SHIFT":.13, "DECKB_SLICE_GROOVE":.18, "DECKB_SLICE_FILL":.15, "DECKB_SLICE_CALL":.14, "AB_CONTRARY_RISE":.13, "BASS_SWAP_GLIDE":.11, "SPINBACK":.20, "BRAKE_STOP":.12, "ROLL_LADDER":.14, "QUANT_BUILD":.12, "PUMP_GROOVE":.10, "FLASH_FORWARD":.13, "TIME_INTERLEAVE":.11, "JUMPBACK":.15}
        return {"TENSION": .08, "FILTER_SWEEP": .08, "ECHO_HALF": .08, "GROOVE_LIFT": .08, "PUNCH_IN": .08, "DROP": .07, "DECKB_GHOST":.09, "DECKB_TEASE":.08, "DECKB_DRUM_LAYER":.08, "DECKB_MICRO_SHIFT":.11, "DECKB_SLICE_GROOVE":.15, "DECKB_SLICE_FILL":.12, "DECKB_SLICE_CALL":.11, "AB_CONTRARY_RISE":.14, "AB_CONTRARY_FALL":.10, "BASS_SWAP_GLIDE":.12, "AB_BREATH":.08, "QUANT_BUILD":.15, "SPINBACK":.13, "PUMP_GROOVE":.12, "ROLL_LADDER":.11, "BRAKE_STOP":.07, "FLASH_FORWARD":.16, "TIME_INTERLEAVE":.13, "JUMPBACK":.11}

    def _params(self, family: str, intensity: float, rng: random.Random, style: str, morph: str) -> dict[str, Any]:
        q = clamp(intensity)
        jitter = lambda a, b: a + (b - a) * rng.random()
        if family == "TENSION":
            return {"filter_depth": jitter(.08,.20)*q, "riser": jitter(.30,.75)*q, "reverse": jitter(.10,.48)*q,
                    "width": jitter(.28,.72)*q, "echo_send": jitter(.08,.30)*q, "duration_beats": jitter(1.5,4.0)}
        if family == "FILTER_SWEEP":
            return {"depth": jitter(.10,.26)*q, "riser": jitter(.08,.45)*q, "width": jitter(.20,.62)*q,
                    "duration_beats": jitter(1.0,4.0), "curve": "smoothstep" if rng.random()>.25 else "exponential"}
        if family == "TRANS_GATE":
            return {"depth": jitter(.12,.36)*q, "division": 4.0 if q>.62 else 2.0, "drum_drive": jitter(.18,.52)*q, "duration_beats": jitter(.5,2.0)}
        if family == "DRUM_FILL":
            return {"snare": jitter(.35,.82)*q, "drum_drive": jitter(.24,.62)*q, "doubletime": jitter(.08,.70)*q if morph=="dnb" else jitter(.0,.24)*q, "duration_beats": jitter(.5,2.0)}
        if family == "ROLL_ACCEL":
            return {"start_beats": 1.0 if q<.65 else .5, "end_beats": .25, "xfade": jitter(.12,.26)*q, "gate": jitter(.08,.28)*q,
                    "acceleration": jitter(.45,.86), "duration_beats": jitter(1.0,2.5)}
        if family in {"ROLL_HALF", "ROLL_QUARTER"}:
            return {"xfade": jitter(.10,.23)*q, "duration_beats": jitter(.5,1.5)}
        if family in {"ECHO_HALF", "ECHO_ONE", "ECHO_OUT"}:
            d={"send": jitter(.30,.72)*q, "feedback": jitter(.26,.54)*q, "width": jitter(.35,.78)*q, "duck": jitter(.60,.88), "duration_beats": jitter(.5,2.0)}
            if family=="ECHO_OUT": d["tail_beats"]=jitter(1.0,3.0)
            return d
        if family == "WASH_OUT":
            return {"reverse": jitter(.28,.72)*q, "reverb": jitter(.10,.32)*q, "width": jitter(.46,.82)*q, "filter": -jitter(.04,.14)*q, "duration_beats": jitter(1.5,4.0)}
        if family == "SPACE_BREAK":
            return {"reverse": jitter(.34,.76)*q, "reverb": jitter(.12,.36)*q, "width": jitter(.50,.86)*q, "echo_send": jitter(.12,.38)*q, "duration_beats": jitter(1.5,4.0)}
        if family == "GROOVE_LIFT":
            return {"punch": jitter(.22,.62)*q, "energy": jitter(.18,.58)*q, "drum_drive": jitter(.16,.54)*q, "air": jitter(.03,.18)*q, "duration_beats": jitter(2.0,8.0)}
        if family == "PUNCH_IN":
            return {"punch": jitter(.42,.84)*q, "energy": jitter(.28,.72)*q, "drum_drive": jitter(.24,.64)*q, "clarity": jitter(.06,.24)*q, "duration_beats": jitter(1.0,4.0)}
        if family == "DROP":
            return {"impact": jitter(.62,.98)*q, "punch": jitter(.48,.88)*q, "energy": jitter(.42,.84)*q, "air": jitter(.04,.18)*q, "duration_beats": jitter(1.0,4.0)}
        if family == "CLEAN_RESET":
            return {"tail_keep": jitter(.18,.48), "duration_beats": jitter(.5,1.5)}
        if family == "RISER":
            return {"riser": jitter(.42,.92)*q, "filter_depth": jitter(.08,.22)*q, "width": jitter(.32,.78)*q, "duration_beats": jitter(2.0,6.0)}
        if family == "REVERSE_SWELL":
            return {"reverse": jitter(.42,.88)*q, "reverb": jitter(.08,.28)*q, "width": jitter(.42,.82)*q, "duration_beats": jitter(1.0,4.0)}
        if family == "DECKB_GHOST":
            return {"loop_beats":2.0 if q<.72 else 1.0,"layer":jitter(.10,.20)*q,"texture":jitter(.48,.72),"transient":jitter(.28,.52)*q,"b_low_db":-6.0,"b_mid_db":-1.8,"b_high_db":.6,"duration_beats":jitter(2.0,6.0)}
        if family == "DECKB_DRUM_LAYER":
            return {"loop_beats":1.0 if q<.78 else .5,"layer":jitter(.13,.25)*q,"texture":jitter(.68,.94),"transient":jitter(.48,.78)*q,"b_low_db":-7.0,"b_mid_db":-2.4,"b_high_db":1.0,"duration_beats":jitter(2.0,5.0)}
        if family == "DECKB_HARMONIC_BED":
            return {"loop_beats":4.0,"layer":jitter(.07,.16)*q,"texture":-jitter(.40,.72),"transient":jitter(.0,.12),"b_low_db":-4.5,"b_mid_db":-1.0,"b_high_db":-3.0,"duration_beats":jitter(3.0,8.0)}
        if family == "DECKB_TEASE":
            return {"loop_beats":2.0 if q<.68 else 1.0,"layer":jitter(.09,.18)*q,"texture":jitter(.30,.70),"transient":jitter(.18,.46)*q,"echo_send":jitter(.12,.32)*q,"feedback":jitter(.20,.38)*q,"duration_beats":jitter(1.5,4.0)}
        if family == "DECKB_SWAP":
            return {"loop_beats":1.0 if q<.74 else .5,"xfade":jitter(.14,.25)*q,"texture":jitter(.42,.82),"transient":jitter(.28,.58)*q,"b_low_db":-5.5,"duration_beats":jitter(.5,1.5)}
        if family == "DECKB_MICRO_SHIFT":
            # Short, audible but non-dominant A->B pulse. The realtime quality
            # gate can attenuate this to zero if the captured loop is not musical.
            return {"xfade":jitter(.085,.165)*q,"layer":jitter(.025,.055)*q,"b_fx":jitter(.34,.58)*q,"duration_beats":jitter(.38,.78)}
        if family == "DECKB_RETURN":
            return {"duration_beats":jitter(.30,.62)}
        if family == "DECKB_SLICE_GROOVE":
            return {"loop_beats":2.0 if q<.68 else 1.0,"layer":jitter(.14,.24)*q,"slice_mix":jitter(.50,.72)*q,"b_fx":jitter(.56,.78)*q,"texture":jitter(.52,.78),"transient":jitter(.36,.58)*q,"b_low_db":-7.0,"b_mid_db":-2.0,"b_high_db":.8,"duration_beats":jitter(2.0,5.0)}
        if family == "DECKB_SLICE_FILL":
            return {"loop_beats":1.0 if q<.72 else .5,"layer":jitter(.15,.27)*q,"slice_mix":jitter(.62,.86)*q,"b_fx":jitter(.68,.90)*q,"texture":jitter(.68,.94),"transient":jitter(.54,.82)*q,"b_low_db":-7.5,"b_mid_db":-2.4,"b_high_db":1.2,"duration_beats":jitter(.75,2.0)}
        if family == "DECKB_SLICE_CALL":
            return {"loop_beats":2.0 if q<.72 else 1.0,"layer":jitter(.13,.23)*q,"slice_mix":jitter(.52,.78)*q,"b_fx":jitter(.70,.94)*q,"texture":jitter(.34,.72),"transient":jitter(.28,.58)*q,"b_low_db":-6.5,"b_mid_db":-1.6,"b_high_db":.5,"duration_beats":jitter(1.0,3.0)}
        if family == "DECKB_SLICE_ACCEL":
            return {"loop_beats":1.0 if q<.62 else .5,"layer":jitter(.17,.29)*q,"slice_mix":jitter(.68,.92)*q,"b_fx":jitter(.74,.95)*q,"texture":jitter(.72,.96),"transient":jitter(.60,.88)*q,"b_low_db":-8.0,"b_mid_db":-2.8,"b_high_db":1.4,"duration_beats":jitter(.75,2.25)}
        if family == "DECKB_MOTIF_TEASE":
            return {"loop_beats":2.0 if q<.78 else 1.0,"layer":jitter(.09,.17)*q,"slice_mix":jitter(.28,.48)*q,"b_fx":jitter(.50,.72)*q,"texture":jitter(-.12,.26),"transient":jitter(.14,.34)*q,"echo_send":jitter(.10,.24)*q,"duration_beats":jitter(1.5,3.5)}
        if family == "DECKB_COUNTER_GROOVE":
            return {"loop_beats":2.0 if q<.70 else 1.0,"layer":jitter(.11,.19)*q,"slice_mix":jitter(.40,.62)*q,"b_fx":jitter(.46,.70)*q,"texture":jitter(.40,.72),"transient":jitter(.38,.62)*q,"duration_beats":jitter(1.5,3.0)}
        if family == "DECKB_ECHO_FREEZE":
            return {"loop_beats":.5,"layer":jitter(.055,.11)*q,"slice_mix":jitter(.34,.56)*q,"b_fx":jitter(.72,.92)*q,"texture":jitter(.46,.74),"transient":jitter(.34,.60)*q,"echo_send":jitter(.34,.58)*q,"feedback":jitter(.34,.50)*q,"duration_beats":jitter(.5,1.25)}
        if family == "DECKB_PICKUP_STUTTER":
            return {"loop_beats":.5,"layer":jitter(.10,.17)*q,"slice_mix":jitter(.66,.86)*q,"b_fx":jitter(.68,.90)*q,"texture":jitter(.58,.84),"transient":jitter(.58,.82)*q,"duration_beats":jitter(.5,1.0)}
        if family == "DECKB_HOOK_CALL":
            return {"loop_beats":2.0,"layer":jitter(.09,.16)*q,"slice_mix":jitter(.42,.64)*q,"b_fx":jitter(.58,.80)*q,"texture":jitter(-.18,.18),"transient":jitter(.12,.34)*q,"echo_send":jitter(.14,.30)*q,"duration_beats":jitter(1.0,2.5)}
        if family == "DECKB_DROP_FLASH":
            return {"loop_beats":.5 if q>.68 else 1.0,"xfade":jitter(.10,.18)*q,"layer":jitter(.03,.07)*q,"b_fx":jitter(.62,.84)*q,"texture":jitter(.34,.64),"transient":jitter(.44,.68)*q,"duration_beats":jitter(.35,.75)}
        if family == "DECKB_RELEASE":
            return {"duration_beats":jitter(.5,1.2)}
        if family == "AB_CONTRARY_RISE":
            # Deck B rises (HPF opens downward, layer/gain lift) while Deck A
            # falls (LPF closes, gain dips).  A single low-end owner at any time.
            return {"depth":jitter(.44,.80)*q,"riser":jitter(.20,.62)*q,"layer":jitter(.14,.30)*q,
                    "b_open":jitter(.55,.85),"a_close":jitter(.30,.58)*q,"bass_handoff":jitter(.35,.75)*q,
                    "duration_beats":jitter(4.0,8.0),"curve":"smoothstep" if rng.random()>.3 else "exponential"}
        if family == "AB_CONTRARY_FALL":
            # Mirror image: Deck A re-opens and rises home while Deck B folds down.
            return {"depth":jitter(.40,.75)*q,"reverse":jitter(.16,.48)*q,"a_open":jitter(.55,.90),
                    "b_close":jitter(.45,.80),"reverb":jitter(.06,.22)*q,
                    "duration_beats":jitter(3.0,6.0),"curve":"smoothstep"}
        if family == "BASS_SWAP_GLIDE":
            # Classic low-end handoff: A's lows glide out exactly as B's glide in.
            return {"handoff":jitter(.55,.90)*q,"layer":jitter(.12,.24)*q,
                    "hold_beats":jitter(2.0,6.0),"duration_beats":jitter(3.0,6.0)}
        if family == "AB_BREATH":
            # Both decks inhale together (width + space) then exhale into the beat.
            return {"width":jitter(.40,.78)*q,"reverb":jitter(.10,.30)*q,"echo_send":jitter(.10,.32)*q,
                    "depth":jitter(.10,.26)*q,"duration_beats":jitter(2.0,4.0)}
        if family == "SPINBACK":
            # The definitive transition marker: usually one beat, occasionally two.
            return {"beats":1.0 if q<.78 else 2.0,"duration_beats":1.0}
        if family == "BRAKE_STOP":
            # Turntable power-off; the gentler sibling of the backspin.
            return {"beats":jitter(1.5,2.5),"duration_beats":2.0}
        if family == "PUMP_GROOVE":
            return {"depth":jitter(.30,.62)*q,"cycle_beats":1.0 if rng.random()>.25 else .5,
                    "duration_beats":jitter(6.0,12.0)}
        if family == "QUANT_BUILD":
            # Produced-EDM build: riser + rush ladder + bass strip, all ending on
            # the landing beat.  Duration is quantized 4/8/16 by the caller offset.
            return {"depth":jitter(.55,.95)*q,"riser":jitter(.50,.88)*q,"bass_strip":jitter(.55,.90)*q,
                    "rush":jitter(.42,.78)*q,"duration_beats":8.0 if q>.55 else 4.0}
        if family == "ROLL_LADDER":
            # Halving beat-roll: 2 -> 1 -> 1/2 -> 1/4 reading as a build.
            return {"xfade":jitter(.16,.30)*q,"start_beats":2.0,"duration_beats":4.0}
        if family == "FLASH_FORWARD":
            # A short vision of a phrase the listener has not reached yet.
            return {"beats":2.0 if q<.7 else 3.0,"offset_beats":float(rng.choice((8.0,16.0,24.0,32.0))),"duration_beats":2.0}
        if family == "JUMPBACK":
            # Replay the previous phrase: a live arrangement edit.
            return {"beats":4.0,"offset_beats":float(rng.choice((4.0,8.0))),"duration_beats":4.0}
        if family == "TIME_INTERLEAVE":
            # Present/future call-and-response in alternating beat cells.
            return {"beats":8.0,"offset_beats":float(rng.choice((8.0,12.0,16.0))),
                    "cell_beats":1.0 if q>.72 else 2.0,"duration_beats":8.0}
        return {"duration_beats": 1.0}

    def _make_plan(self, archetype: str, target: int, direction: float, novelty: float, vocal: float,
                   drums: float, energy: float, current: int, commit: int, style: str, morph: str,
                   density: float, authority: float, rng: random.Random) -> CandidatePlan:
        authority_hi=clamp((float(authority)-1.0)/2.60)
        strength = clamp(.42 + .55 * novelty + .25 * abs(direction) + .15 * density + .16*authority_hi, .28, 1.0)
        seq: list[tuple[int,str,float,str]]
        if archetype == "wow_motif_reveal":
            seq=[(-6,"DECKB_MOTIF_TEASE",.52,"foreshadow a future-compatible motif without stealing the foreground"),(-4,"DECKB_COUNTER_GROOVE",.60,"turn the matched motif into a complementary counter-groove"),(-2,"DECKB_ECHO_FREEZE",.70,"freeze one source fragment as controlled expectation"),(-1,"DECKB_PICKUP_STUTTER",.78,"compress the final pickup into a short source-derived stutter"),(0,"DROP",.94,"release the accumulated contrast on the structural downbeat"),(1,"DECKB_RELEASE",.44,"clear the second voice after the reveal")]
        elif archetype == "wow_deck_dialogue":
            seq=[(-5,"DECKB_HOOK_CALL",.54,"let the matched Deck-B hook answer Deck A once"),(-3,"DECKB_RETURN",.38,"restore foreground space before the next answer"),(-2,"DECKB_COUNTER_GROOVE",.64,"answer with a different rhythmic role from the same motif"),(-1,"DECKB_DROP_FLASH",.74,"briefly foreground Deck B for a one-beat flash"),(0,"PUNCH_IN",.92,"snap back to the source on the musical landing"),(1,"DECKB_RELEASE",.42,"retire the dialogue before it repeats")]
        elif archetype == "wow_fakeout_reveal":
            seq=[(-5,"DECKB_MOTIF_TEASE",.48,"introduce a recognizable future-compatible clue"),(-3,"ECHO_OUT",.52,"create expectation without adding a new sound source"),(-2,"DECKB_ECHO_FREEZE",.68,"hold a short Deck-B memory while A clears"),(-1,"SPACE_BREAK",.62,"make a brief negative-space fakeout"),(0,"DECKB_DROP_FLASH",.76,"flash the matched motif into the landing"),(1,"PUNCH_IN",.88,"restore full forward motion immediately after the surprise"),(2,"DECKB_RELEASE",.40,"clean the remix bus")]
        elif archetype == "ab_contrary_reveal":
            # The requested rise/fall duet: B is teased, then B RISES while A FALLS
            # in opposing automation lanes into the landing; A returns after.
            seq=[(-8,"DECKB_MOTIF_TEASE",.50,"seed the future-matched second voice quietly"),
                 (-6,"AB_CONTRARY_RISE",.72,"Deck B rises as Deck A falls â€” opposing filter/gain lanes into the boundary"),
                 (-1,"DRUM_FILL",.62,"source-derived pickup under the crossing point"),
                 (0,"DROP",.94,"release exactly where the two arcs cross"),
                 (1,"AB_CONTRARY_FALL",.60,"Deck A rises home while Deck B folds away"),
                 (3,"DECKB_RELEASE",.42,"retire the second voice after the story resolves")]
        elif archetype == "spinback_reveal":
            # The crowd move: a full produced build, then the record is ripped
            # backwards on the last beat and the drop lands in the vacuum.
            seq=[(-9,"QUANT_BUILD",.74,"produced build: riser + rush ladder + bass strip, ending on the landing"),
                 (-1,"SPINBACK",.85,"backspin the last beat â€” the definitive transition marker"),
                 (0,"DROP",.96,"slam the full-spectrum drop into the silence the spinback created"),
                 (1,"PUMP_GROOVE",.58,"lock the post-drop groove with a beat-synced pump"),
                 (3,"DECKB_RELEASE",.40,"clean the remix bus")]
        elif archetype == "quantized_build_drop":
            seq=[(-8,"QUANT_BUILD",.70,"phrase-length build with the bass floor stripped away"),
                 (-1,"DRUM_FILL",.66,"final-beat fill over the blurred roll"),
                 (0,"DROP",.94,"restore the low end at maximum impact"),
                 (1,"PUMP_GROOVE",.54,"sidechain breathing keeps the drop physical"),
                 (3,"DECKB_RELEASE",.38,"tidy exit")]
        elif archetype == "roll_ladder_drop":
            seq=[(-6,"QUANT_BUILD",.52,"light build bed under the ladder"),
                 (-4,"ROLL_LADDER",.76,"halving beat-roll: 2 -> 1 -> 1/2 -> 1/4 into the landing"),
                 (0,"DROP",.92,"release the roll exactly on the structural downbeat"),
                 (1,"DECKB_RELEASE",.44,"clear the roll deck")]
        elif archetype == "brake_fakeout":
            seq=[(-2,"BRAKE_STOP",.72,"power-off the record mid-phrase â€” total negative space"),
                 (0,"SPACE_BREAK",.66,"hold the vacuum for the fakeout"),
                 (2,"PUNCH_IN",.88,"snap the groove back before the crowd exhales")]
        elif archetype == "echo_spin_exit":
            seq=[(-3,"ECHO_OUT",.58,"beat-synced echo tail on the phrase end"),
                 (-1,"SPINBACK",.78,"spin the remainder backwards through the echo"),
                 (0,"SPACE_BREAK",.62,"open the lower-energy section in the tail"),
                 (2,"PUNCH_IN",.72,"re-enter clean")]
        elif archetype == "pump_flow":
            seq=[(-4,"PUMP_GROOVE",.56,"beat-locked sidechain breathing under the groove"),
                 (-2,"GROOVE_LIFT",.50,"forward propulsion on top of the pump"),
                 (0,"PUNCH_IN",.70,"phrase accent"),
                 (2,"ECHO_HALF",.44,"texture tail before release")]
        elif archetype == "future_echo_reveal":
            # The signature TimeWeaver move: the hook is HEARD from the future,
            # a build answers it, and when the real phrase finally arrives on the
            # landing the listener already knows it.
            seq=[(-10,"FLASH_FORWARD",.66,"foreshadow a phrase the listener has not reached yet"),
                 (-8,"QUANT_BUILD",.72,"build toward the phrase the flash promised"),
                 (-1,"DRUM_FILL",.60,"final pickup"),
                 (0,"DROP",.94,"the foreshadowed phrase lands for real"),
                 (2,"DECKB_RELEASE",.40,"clean the buses")]
        elif archetype == "time_interleave_break":
            seq=[(-8,"TIME_INTERLEAVE",.72,"call-and-response between NOW and the future in beat cells"),
                 (0,"PUNCH_IN",.86,"resolve the weave on the phrase boundary"),
                 (2,"ECHO_HALF",.42,"texture tail")]
        elif archetype == "hook_extend_edit":
            seq=[(-4,"JUMPBACK",.68,"replay the previous phrase â€” a live arrangement edit"),
                 (0,"GROOVE_LIFT",.56,"lift out of the extended hook"),
                 (2,"ECHO_ONE",.40,"phrase punctuation")]
        elif archetype == "ab_seesaw_dialogue":
            seq=[(-6,"DECKB_COUNTER_GROOVE",.56,"establish the answering voice"),
                 (-4,"AB_CONTRARY_RISE",.62,"first seesaw: B up, A down"),
                 (-2,"AB_CONTRARY_FALL",.58,"answer seesaw: A up, B down"),
                 (0,"PUNCH_IN",.86,"land the dialogue on the phrase boundary"),
                 (1,"DECKB_RELEASE",.40,"clear the remix bus")]
        elif archetype == "bass_swap_glide":
            seq=[(-6,"DECKB_HARMONIC_BED",.50,"lay the tonally matched bed"),
                 (-4,"BASS_SWAP_GLIDE",.66,"hand the low end from A to B â€” one bass owner at all times"),
                 (-1,"AB_BREATH",.50,"shared inhale before the return"),
                 (0,"PUNCH_IN",.82,"A takes the low end back on the downbeat"),
                 (1,"DECKB_RELEASE",.42,"retire the bed")]
        elif archetype == "contrary_fakeout":
            seq=[(-6,"AB_CONTRARY_RISE",.58,"begin the rise/fall cross early"),
                 (-2,"SPACE_BREAK",.60,"suspend both arcs in negative space"),
                 (-1,"DECKB_PICKUP_STUTTER",.72,"stutter the second voice's pickup"),
                 (0,"DECKB_DROP_FLASH",.76,"flash B into the landing instead of the expected A drop"),
                 (1,"AB_CONTRARY_FALL",.56,"resolve the seesaw back to A"),
                 (2,"DECKB_RELEASE",.40,"clean exit")]
        elif archetype == "deckb_slice_remix_drop":
            seq=[(-4,"DECKB_SLICE_GROOVE",.62,"establish an anchor-safe live-slice groove"),(-2,"DECKB_SLICE_FILL",.74,"rearrange only the pickup cells with stronger Deck B FX"),(-1,"DECKB_SLICE_ACCEL",.82,"accelerate the final beat while preserving downbeat anchors"),(0,"DROP",.92,"release the slicer exactly on the structural landing"),(1,"DECKB_RELEASE",.48,"return to clean source after the remix phrase")]
        elif archetype == "deckb_slice_call_punch":
            seq=[(-3,"DECKB_SLICE_GROOVE",.58,"introduce a recognizable sliced motif"),(-2,"DECKB_SLICE_CALL",.70,"create source-derived call-response on Deck B"),(-1,"DECKB_SLICE_FILL",.68,"answer with a short transient-safe pickup"),(0,"PUNCH_IN",.88,"snap the foreground back without a hard glitch"),(1,"DECKB_RELEASE",.44,"clear the remix bus after the phrase")]
        elif archetype == "deckb_slice_space":
            seq=[(-3,"DECKB_HARMONIC_BED",.46,"keep a quiet tonal anchor"),(-2,"DECKB_SLICE_CALL",.50,"use a restrained slice answer rather than a random shuffle"),(-1,"WASH_OUT",.54,"soften the foreground"),(0,"SPACE_BREAK",.76,"open the lower-energy section"),(1,"DECKB_RELEASE",.40,"remove the remix layer before repetition")]
        elif archetype == "deckb_ghost_drop":
            seq=[(-4,"DECKB_GHOST",.62,"introduce a quiet source-derived ghost loop"),(-2,"FILTER_SWEEP",.52,"shape the foreground while Deck B breathes"),(-1,"DECKB_DRUM_LAYER",.72,"push percussive Deck B texture"),(0,"DROP",.90,"return foreground on the structural downbeat"),(1,"DECKB_RELEASE",.48,"retire Deck B after the landing")]
        elif archetype == "deckb_tease_punch":
            seq=[(-3,"DECKB_TEASE",.58,"loop a recognizable phrase fragment with a short tail"),(-2,"GROOVE_LIFT",.48,"keep Deck A propulsion"),(-1,"DECKB_SWAP",.66,"brief call-response handoff"),(0,"PUNCH_IN",.86,"snap cleanly back to Deck A"),(1,"DECKB_RELEASE",.44,"clear the remix layer")]
        elif archetype == "deckb_harmonic_space":
            seq=[(-3,"DECKB_HARMONIC_BED",.50,"lay a restrained source-derived harmonic bed"),(-1,"WASH_OUT",.56,"lower foreground density"),(0,"SPACE_BREAK",.72,"open breathing room"),(1,"DECKB_RELEASE",.40,"remove the bed before it becomes repetitive")]
        elif archetype == "deckb_drums_roll":
            seq=[(-3,"DECKB_DRUM_LAYER",.64,"bring source-derived percussion forward"),(-2,"DECKB_GHOST",.58,"alternate texture without adding unrelated samples"),(-1,"ROLL_ACCEL",.68,"short slip-roll acceleration"),(0,"DROP",.90,"release both decks on the downbeat"),(1,"DECKB_RELEASE",.46,"clean mixer state after landing")]
        elif archetype == "tension_roll_drop":
            seq=[(-3,"TENSION",.72,"open tension"),(-2,"FILTER_SWEEP",.66,"shape the phrase"),(-1,"ROLL_ACCEL",.80,"accelerate source rhythm"),(0,"DROP",.94,"land on structural downbeat")]
        elif archetype == "echo_tease_punch":
            seq=[(-3,"ECHO_HALF",.46,"tease phrase tail"),(-2,"GROOVE_LIFT",.50,"lift groove without masking source"),(-1,"DRUM_FILL",.70,"source-aware pickup"),(0,"PUNCH_IN",.88,"clean transient landing")]
        elif archetype == "gate_riser_drop":
            seq=[(-3,"RISER",.58,"long controlled lift"),(-2,"TRANS_GATE",.55,"rhythmic articulation"),(-1,"DRUM_FILL",.72,"increase drum expectation"),(0,"DROP",.90,"release tension")]
        elif archetype == "filter_echo_drop":
            seq=[(-3,"FILTER_SWEEP",.58,"open motion"),(-2,"ECHO_OUT",.62,"create a tail before landing"),(-1,"TENSION",.58,"final controlled tension"),(0,"DROP",.86,"clean impact landing")]
        elif archetype == "sparse_fakeout":
            seq=[(-3,"GROOVE_LIFT",.38,"keep source recognizable"),(-1,"ECHO_HALF",.48,"small fakeout"),(0,"PUNCH_IN",.76,"understated landing")]
        elif archetype == "echo_wash_space":
            seq=[(-2,"ECHO_HALF",.54,"release foreground"),(-1,"WASH_OUT",.68,"wash into lower energy"),(0,"SPACE_BREAK",.82,"create breathing room")]
        elif archetype == "reverse_space":
            seq=[(-2,"REVERSE_SWELL",.58,"reverse anticipation"),(-1,"ECHO_ONE",.48,"leave coherent tail"),(0,"SPACE_BREAK",.78,"open section")]
        elif archetype == "filter_out_reset":
            seq=[(-2,"FILTER_SWEEP",.48,"soft structural marking"),(-1,"WASH_OUT",.54,"lower density"),(0,"CLEAN_RESET",.72,"return controller to clean state")]
        elif archetype == "groove_variation":
            seq=[(-2,"GROOVE_LIFT",.48,"subtle phrase evolution"),(0,"PUNCH_IN",.58,"clean phrase accent")]
        elif archetype == "echo_variation":
            seq=[(-2,"ECHO_ONE",.44,"alternate phrase texture"),(0,"GROOVE_LIFT",.56,"restore forward motion")]
        else:
            seq=[(-2,"TENSION",.42,"restrained structural mark"),(0,"PUNCH_IN",.60,"clean landing")]

        # Creativity-max concert mode expands a 3â€“4 gesture phrase into a fuller
        # 5â€“6 gesture arc.  This is deliberately a choreography-density increase,
        # not a sample-gain increase; downstream headroom remains unchanged.
        if authority_hi>.65:
            if direction>.03:
                seq=[(-4,"GROOVE_LIFT",.46,"high-authority pre-build propulsion")]+seq+[(1,"PUNCH_IN",.56,"hold the landing for one beat")]
            elif direction<-.03:
                seq=[(-3,"ECHO_ONE",.42,"high-authority pre-release texture")]+seq+[(1,"CLEAN_RESET",.52,"clear the rack after the breath")]
            else:
                seq=[(-3,"GROOVE_LIFT",.40,"high-authority phrase motion")]+seq+[(1,"ECHO_HALF",.42,"short alternate phrase tail")]

        # Collapse any gestures that quantize to the same executable beat.  In the
        # previous max-creativity path, a prepended flourish and the first Deck-B
        # gesture could land on the same minimum beat; the scheduler then delayed one
        # and produced an unintended extra A/B move. Keep the musically stronger
        # primary gesture instead of creating a hidden overdue queue item.
        by_beat={}
        min_b=current+commit+1
        def place(b,entry):
            # Keep the stronger gesture on a contested beat, but try to re-slot
            # the weaker one on a neighbour instead of silently dropping it
            # (short runways used to swallow early foreshadow gestures).
            prev=by_beat.get(b)
            if prev is None:
                by_beat[b]=entry; return
            keep,move=(entry,prev) if float(entry[1])>float(prev[1]) else (prev,entry)
            by_beat[b]=keep
            for nb in (b-1,b+1,b-2,b+2):
                if nb>=min_b and nb<=target+4 and nb not in by_beat:
                    by_beat[nb]=move; return
        for off,fam,rel,why in seq:
            place(max(min_b,target+off),(fam,rel,why))
        gestures: list[Gesture] = []
        for b in sorted(by_beat):
            fam,rel,why=by_beat[b]
            raw=clamp(strength*rel*(.92+.16*rng.random()), .16, 1.0)
            # High creativity increases *controller authority*, not raw sample gain:
            # gestures approach their bounded maxima and remain active longer.
            intensity=clamp(raw+(1.0-raw)*(.58*authority_hi), .16, 1.0)
            params=self._params(fam,intensity,rng,style,morph)
            if "duration_beats" in params:
                params["duration_beats"]=float(params["duration_beats"])*(1.0+.38*authority_hi)
            if "tail_beats" in params:
                params["tail_beats"]=float(params["tail_beats"])*(1.0+.45*authority_hi)
            gestures.append(Gesture(b,fam,intensity,params,why))
        return CandidatePlan(archetype.replace("_"," ").title(),archetype,gestures,{})

    def _augment_qualified_shift_cadence(self, plan: CandidatePlan, *, current: int, target: int, commit: int,
                                         authority: float, vocal: float, style: str, morph: str, rng: random.Random) -> CandidatePlan:
        """Increase A<->B cadence at high creativity without increasing dominance.

        Max-authority mode adds three to four short B pulses, each followed by a
        return to A. The pulses reuse one future-matched capture; they do not cause
        repeated loop analysis. Actual audibility is still controlled downstream by
        the fixed harmonic/rhythm/salience quality gate.
        """
        ahi=clamp((float(authority)-1.0)/2.60)
        if ahi < .68:
            return plan
        existing_b=sum(1 for g in plan.gestures if g.family.startswith("DECKB_"))
        desired_pairs=(2 if existing_b>=3 else (4 if ahi>.90 else 3))
        capture_fams={
            "DECKB_GHOST","DECKB_DRUM_LAYER","DECKB_HARMONIC_BED","DECKB_TEASE","DECKB_SWAP",
            "DECKB_SLICE_GROOVE","DECKB_SLICE_FILL","DECKB_SLICE_CALL","DECKB_SLICE_ACCEL",
            "DECKB_MOTIF_TEASE","DECKB_COUNTER_GROOVE","DECKB_ECHO_FREEZE","DECKB_PICKUP_STUTTER","DECKB_HOOK_CALL","DECKB_DROP_FLASH"
        }
        gestures=list(plan.gestures)
        capture_beats=[g.beat for g in gestures if g.family in capture_fams]
        if not capture_beats:
            b=max(current+commit+1,target-3)
            fam="DECKB_SLICE_GROOVE" if (morph=="dnb" or style in {"club","hip-hop"}) else "DECKB_TEASE"
            intensity=clamp(.58+.22*ahi-.18*clamp((vocal-.62)/.30),.38,.82)
            gestures.append(Gesture(b,fam,intensity,self._params(fam,intensity,rng,style,morph),
                                    "prime one future-matched Deck B motif before higher-cadence A/B phrasing"))
            capture_beats=[b]
        first=max(current+commit+1,min(capture_beats))
        occupied={g.beat for g in gestures}
        # Search a bounded beat window. We prefer on/off pairs around the structural
        # landing and never place two primary gestures on one beat.
        slots=[]
        for b in range(first+1,target+10):
            if b in occupied or (b+1) in occupied:
                continue
            # Keep pulses on musically stable 1/2-beat-grid-equivalent beat cells;
            # no off-grid trigger is created here.
            slots.append((b,b+1)); occupied.add(b); occupied.add(b+1)
            if len(slots)>=desired_pairs:
                break
        vocal_scale=1.0-.48*clamp((vocal-.58)/.34)
        for i,(b,rb) in enumerate(slots):
            rel=clamp((.70+.07*i)*vocal_scale,.34,.90)
            intensity=clamp(rel+(1.0-rel)*(.42*ahi),.34,.94)
            gestures.append(Gesture(b,"DECKB_MICRO_SHIFT",intensity,self._params("DECKB_MICRO_SHIFT",intensity,rng,style,morph),
                                    "qualified short A->B pulse; musical gate remains fixed"))
            gestures.append(Gesture(rb,"DECKB_RETURN",.42,self._params("DECKB_RETURN",.42,rng,style,morph),
                                    "return to Deck A while keeping the matched loop resident"))
        gestures.sort(key=lambda g:g.beat)
        plan.gestures=gestures
        return plan

    def _score(self, plan: CandidatePlan, history: list[str], direction: float, novelty: float, vocal: float,
               drums: float, density: float, style: str, morph: str,
               musical: dict[str, Any] | None = None) -> CandidatePlan:
        fams=[g.family for g in plan.gestures]
        style_w=self._style_weights(style,morph)
        alignment=.42 + .36*clamp(novelty*4.0) + .20*clamp(abs(direction)*5.0)
        # Directional grammar alignment.
        if direction>.03 and any(f in fams for f in ("DROP","PUNCH_IN")): alignment+=.10
        if direction<-.03 and any(f in fams for f in ("SPACE_BREAK","WASH_OUT")): alignment+=.10
        prompt_fit=sum(style_w.get(f,(.085 if f.startswith("DECKB_") else 0.0)) for f in fams)/max(1.0,len(fams)*.16)
        prompt_fit=clamp(prompt_fit)
        recent=history[-24:]
        rep=sum(recent.count(f) for f in fams)/max(1.0,len(fams)*3.0)
        repetition=clamp(rep)
        destructive=sum(1 for f in fams if f in DESTRUCTIVE)
        vocal_risk=clamp(((vocal-.48)/.42) if vocal>.48 else 0.0) * clamp(destructive/2.0)
        variety=clamp(len(set(fams))/5.0)
        smoothness=1.0
        beats=[g.beat for g in plan.gestures]
        if len(set(beats))<len(beats): smoothness-=.30
        if destructive>2: smoothness-=.18
        density_fit=1.0-abs(clamp(len(fams)/5.0)-clamp(.25+.75*density))
        deckb_use=clamp(sum(1 for f in fams if f.startswith("DECKB_"))/2.0)
        slicer_use=clamp(sum(1 for f in fams if f.startswith("DECKB_SLICE_"))/2.0)
        # Deck B is rewarded only as a controlled layer/tease, not as constant full
        # replacement. High vocals reduce the reward for swap-like gestures.
        swap_penalty=.0
        if "DECKB_SWAP" in fams:
            swap_penalty=.34 if vocal>.58 else .18
            deckb_use*=.72
        if vocal>.72 and "DECKB_SWAP" in fams: deckb_use*=.55
        components={
            "structure":clamp(alignment), "prompt":prompt_fit, "novelty":clamp(novelty*4.0),
            "variety":variety, "density_fit":clamp(density_fit), "smoothness":clamp(smoothness),
            "repetition_penalty":repetition, "vocal_penalty":vocal_risk, "swap_penalty":swap_penalty, "deckb_use":deckb_use, "slicer_use":slicer_use,
        }
        wow_arc=1.0 if plan.archetype.startswith("wow_") else 0.0
        components["wow_arc"]=wow_arc
        contrary=clamp(sum(1 for f in fams if f in {"AB_CONTRARY_RISE","AB_CONTRARY_FALL","BASS_SWAP_GLIDE","AB_BREATH"})/2.0)
        components["contrary_motion"]=contrary
        showman=clamp(sum(1 for f in fams if f in SHOWMANSHIP_FAMILIES)/2.0)
        components["showmanship"]=showman
        score=(.24*components["structure"]+.17*prompt_fit+.13*components["novelty"]+.13*variety+
               .09*components["density_fit"]+.13*components["smoothness"]+.10*deckb_use+.08*slicer_use+.055*wow_arc*components["novelty"]+.06*contrary+.07*showman-.16*repetition-.18*vocal_risk-.10*swap_penalty)

        # v31.10 musical-awareness critic (all advisory context, all optional):
        # tension-grammar fit, energy-arc fit and big-gesture pacing.  A landing
        # spends tension; a build needs headroom; drops keep 16-24 beat spacing.
        m=musical or {}
        if m:
            releases=sum(1 for f in fams if f in RELEASE_FAMILIES)
            builds=sum(1 for f in fams if f in BUILD_FAMILIES)
            readiness=float(m.get("release_readiness") or .5)
            headroom=float(m.get("build_headroom") or .5)
            tension_fit=0.0
            if releases: tension_fit+=(readiness-.42)*1.1
            if builds: tension_fit+=(headroom-.38)*.8
            if releases and bool(m.get("refractory")): tension_fit-=.30
            arc_bias=float(m.get("arc_bias") or 0.0)
            plan_dir=(1.0 if any(f in fams for f in ("DROP","PUNCH_IN","AB_CONTRARY_RISE","GROOVE_LIFT")) else 0.0) \
                    -(1.0 if any(f in fams for f in ("SPACE_BREAK","WASH_OUT","AB_CONTRARY_FALL")) else 0.0)
            arc_fit=clamp(.5+.5*arc_bias*plan_dir)
            big_pen=float(m.get("big_penalty") or 0.0) if releases else 0.0
            phrase_conf=float(m.get("confidence") or 0.0)
            components["tension_fit"]=round(clamp(.5+tension_fit),4)
            components["arc_fit"]=round(arc_fit,4)
            components["pacing_penalty"]=round(big_pen,4)
            # v31.11 section grammar as a critic term (not just list order): an
            # approaching PEAK rewards build/spin/drop choreography, an approaching
            # BREAK rewards the brake / echo-spin / wash exits.
            sec=str(m.get("section_next") or "")
            sec_fit=.5
            if sec=="PEAK":
                hits=sum(1 for f in fams if f in {"QUANT_BUILD","SPINBACK","ROLL_LADDER","AB_CONTRARY_RISE"})+(1 if "DROP" in fams else 0)
                sec_fit=.5+.5*min(1.0,hits/2.0)
            elif sec=="BREAK":
                hits=sum(1 for f in fams if f in {"BRAKE_STOP","ECHO_OUT","WASH_OUT","SPACE_BREAK","AB_CONTRARY_FALL","SPINBACK"})
                sec_fit=.5+.5*min(1.0,hits/2.0)
            components["section_fit"]=round(sec_fit,4)
            leg=m.get("legality") or {}
            leg_fit=.5
            wins=leg.get("windows") or {}
            if releases and wins:
                lw=[int(w.get("beat")) for w in (wins.get("LANDING") or [])]
                land_beats=[g.beat for g in plan.gestures if g.family in RELEASE_FAMILIES]
                if land_beats:
                    leg_fit=1.0 if any(b in lw for b in land_beats) else 0.15
            if builds and wins:
                rw=[int(w.get("beat")) for k in ("RISE4","RISE8","RISE16") for w in (wins.get(k) or [])]
                bb=[g.beat for g in plan.gestures if g.family in BUILD_FAMILIES]
                if bb and rw:
                    leg_fit=min(1.0,leg_fit+ (0.25 if any(abs(b-r)<=1 for b in bb for r in rw) else -0.15))
            components["legality_fit"]=round(clamp(leg_fit),4)
            score+=.11*clamp(.5+tension_fit)+.09*(arc_fit-.5)*2.0*(.4+.6*phrase_conf)+.12*(sec_fit-.5)*2.0+.16*(clamp(leg_fit)-.5)*2.0-.17*big_pen
        plan.components=components; plan.score=score
        return plan

    def plan(self, *, segments: list[dict[str, Any]], current_beat: int, commit_beats: int,
             policy: Any, history: list[dict[str, Any]], analysis: dict[str, Any],
             neural_decision: dict[str, Any] | None = None, regen: int = 0,
             musical: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        self.plan_counter += 1
        boundary=self._dominant_boundary(segments)
        if boundary is None:
            return [], {}, []
        novelty=float(boundary.get("novelty") or 0.0); direction=float(boundary.get("direction") or 0.0)
        vocal=float(boundary.get("vocal") or analysis.get("vocal_activity") or 0.0)
        drums=float(boundary.get("drums") or analysis.get("drums_activity") or 0.0)
        energy=float(boundary.get("energy") or analysis.get("energy") or 0.0)
        target=current_beat+max(commit_beats+1,int(boundary.get("beat_offset") or 4))
        m=musical or {}
        # v31.10 phrase-aligned landing: real DJ transitions land on the 8/16/32
        # beat phrase grid (ISMIR 2020 mix analysis).  When the PhraseClock is
        # confident, snap the landing to the next phrase boundary; otherwise keep
        # the historical 2-beat grid.
        phrase_beats=int(m.get("phrase_beats") or 0); phrase_conf=float(m.get("confidence") or 0.0)
        # v31.16 SongMind: the TransitionLegality engine decides WHERE a landing
        # may be.  When it offers a legal landing inside the horizon, that beat
        # is the plan target (phrase boundary + section/song-memory evidence),
        # and rises are placed so they END on it.  Without one, no release-type
        # archetype may be selected.
        leg=m.get("legality") or {}
        legal_landing=leg.get("best_landing")
        legal_landings=[int(l.get("beat")) for l in (leg.get("landings") or []) if l.get("beat") is not None]
        landing_windows=[int(w.get("beat")) for w in ((leg.get("windows") or {}).get("LANDING") or []) if w.get("beat") is not None]
        if legal_landing is not None and int(legal_landing)>=current_beat+commit_beats+2 and int(legal_landing)<=current_beat+40:
            target=int(legal_landing)
        elif phrase_beats>=8 and phrase_conf>=.45:
            beats_to_boundary=int(m.get("beats_to_boundary") or 0)
            boundary_beat=current_beat+beats_to_boundary if beats_to_boundary>0 else current_beat+phrase_beats
            while boundary_beat<current_beat+commit_beats+2:
                boundary_beat+=phrase_beats
            if abs(boundary_beat-target)<=phrase_beats//2:
                target=boundary_beat
            else:
                target=((target+1)//2)*2
        else:
            target=((target+1)//2)*2
        style=str(getattr(policy,"style","balanced")); morph=str(getattr(policy,"morph_target","native")); density=clamp(float(getattr(policy,"action_density",.55))); authority=float(getattr(policy,"agentic_authority",1.0))
        history_fams=self._history_families(history,current_beat)
        seed=(current_beat//2)*1009 + int(novelty*10000)*17 + int(direction*10000)*31 + int(regen)*7919 + len(history_fams)*13
        rng=random.Random(seed)

        wow_ready=authority>=2.55 and (novelty>=.055 or abs(direction)>=.035)
        arc=str(m.get("arc") or "HOLD"); readiness=float(m.get("release_readiness") or .5)
        section_next=str(m.get("section_next") or ""); section_now=str(m.get("section_now") or "")
        if direction>.035 or arc=="RISE":
            archetypes=(["wow_motif_reveal","wow_deck_dialogue"] if wow_ready else [])+["future_echo_reveal","spinback_reveal","quantized_build_drop","ab_contrary_reveal","roll_ladder_drop","deckb_slice_remix_drop","deckb_slice_call_punch","deckb_drums_roll","bass_swap_glide","filter_echo_drop"]
        elif direction<-.035 or arc=="FALL":
            archetypes=(["wow_fakeout_reveal","wow_deck_dialogue"] if wow_ready else [])+["echo_spin_exit","brake_fakeout","time_interleave_break","ab_seesaw_dialogue","deckb_slice_space","deckb_harmonic_space","bass_swap_glide","echo_wash_space","reverse_space"]
        else:
            archetypes=(["wow_deck_dialogue"] if wow_ready else [])+["pump_flow","time_interleave_break","hook_extend_edit","ab_seesaw_dialogue","deckb_slice_call_punch","bass_swap_glide","deckb_slice_remix_drop","deckb_ghost_drop","groove_variation","echo_variation"]
        # v31.11 section grammar: choose showmanship for the boundary TYPE.
        # An upcoming PEAK wants the produced build / spinback reveal; an
        # upcoming BREAK wants the brake / echo-spin exit; a long GROOVE plateau
        # invites the pump.  (Functional-structure labeling per the MSA literature.)
        if section_next=="PEAK":
            for aname in ("quantized_build_drop","spinback_reveal"):
                if aname in archetypes: archetypes.remove(aname)
            archetypes=["spinback_reveal","quantized_build_drop"]+archetypes
        elif section_next=="BREAK":
            for aname in ("brake_fakeout","echo_spin_exit"):
                if aname in archetypes: archetypes.remove(aname)
            archetypes=["echo_spin_exit","brake_fakeout"]+archetypes
        elif section_now=="GROOVE" and section_next in ("GROOVE","") and "pump_flow" not in archetypes:
            archetypes.insert(1,"pump_flow")
        if wow_ready and readiness>.62:
            archetypes.insert(0,"contrary_fakeout")
        # SongMind gate: if the legality engine says no landing is legal at the
        # target, keep only non-release choreography (grooves, dialogues, exits).
        if leg and landing_windows and (target not in landing_windows):
            release_archs={"spinback_reveal","quantized_build_drop","roll_ladder_drop","future_echo_reveal","ab_contrary_reveal",
                           "deckb_slice_remix_drop","deckb_drums_roll","deckb_ghost_drop","filter_echo_drop","tension_roll_drop",
                           "gate_riser_drop","wow_motif_reveal","contrary_fakeout"}
            kept=[a for a in archetypes if a not in release_archs]
            if kept: archetypes=kept
        elif leg and not landing_windows and (leg.get("reasons") or []):
            release_archs={"spinback_reveal","quantized_build_drop","roll_ladder_drop","future_echo_reveal","ab_contrary_reveal",
                           "deckb_slice_remix_drop","deckb_drums_roll","deckb_ghost_drop","filter_echo_drop","tension_roll_drop",
                           "gate_riser_drop","wow_motif_reveal","contrary_fakeout"}
            kept=[a for a in archetypes if a not in release_archs]
            if kept: archetypes=kept
        archetypes=archetypes[:9]
        # Candidate order rotates with memory so the same tie does not always select A.
        rot=(len(history_fams)+regen)%len(archetypes); archetypes=archetypes[rot:]+archetypes[:rot]
        candidates=[]
        for arch in archetypes:
            p=self._make_plan(arch,target,direction,novelty,vocal,drums,energy,current_beat,commit_beats,style,morph,density,authority,rng)
            candidates.append(self._score(p,history_fams,direction,novelty,vocal,drums,density,style,morph,m))

        # Optional neural action can become a low-authority sixth candidate event,
        # but cannot override a coherent multi-beat choreography by itself.
        nd=neural_decision or {}; na=str(nd.get("action") or "NO_ACTION")
        if na!="NO_ACTION" and float(nd.get("score") or 0)>.20:
            for p in candidates[:2]:
                p.components["neural_support"]=clamp(.5+.5*float(nd.get("score") or 0)); p.score+=.025*p.components["neural_support"]

        candidates.sort(key=lambda p:p.score,reverse=True)
        selected=candidates[0]
        # At maximum creativity, prefer a coherent high-contrast "wow" scene when
        # it is musically competitive with the safest plan. This is a bounded
        # exploration rule: wow choreography never overrides a substantially better
        # structural/voice-safe candidate.
        if authority>=3.15:
            wow=[p for p in candidates if p.archetype.startswith("wow_")]
            if wow and wow[0].score >= selected.score-.12:
                selected=wow[0]
        # v31.8.9: at creativity-max, increase A<->B *cadence* ~3-4x using
        # low-dominance micro shifts. Musical qualification is not relaxed.
        selected=self._augment_qualified_shift_cadence(selected,current=current_beat,target=target,commit=commit_beats,
                                                        authority=authority,vocal=vocal,style=style,morph=morph,rng=rng)
        events=[g.event(selected.score,"DJ DIRECTOR V4") for g in selected.gestures]
        self.selected={
            **selected.summary(),
            "target_beat":target,"boundary":{k:boundary.get(k) for k in ("index","start_sec","beat_offset","novelty","direction","energy","drums","vocal")},
            "memory_families":history_fams[-16:],
        }
        self.candidates=[p.summary() for p in candidates]
        return events, dict(self.selected), list(self.candidates)


__all__=["DJDirectorV3","FAMILY_TO_MACRO","PARAM_SCHEMAS"]
