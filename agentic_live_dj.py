from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import random
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from typing import Any

import numpy as np
from scipy.signal import resample_poly, stft
from beat_clock import BeatClock
from drum_brain import DrumMind, style_id_from_text
import drum_kit
from synth_weave import SynthWeave, tonal_chroma, chroma_agreement, allowed_pitch_classes
from synth_rack import SynthRack, key_pcs, rms_hp, prompt_profile
from timbre_select import TimbreSelector
from groove_live import GrooveLive
from gen_composer import GenComposer
from vocal_weave import VocalWeave
from genre_probe import GenreProbe, PROMPT_GENRE
from groove_net import GrooveNet
from block_planner import BlockPlanner
from collections import deque
from dj_director_v3 import DJDirectorV3, PARAM_SCHEMAS
from musical_intelligence import MusicalIntelligence

# How long one manual slider touch owns its parameter before the agent resumes.
MANUAL_OVERRIDE_HOLD_SEC = 10.0


SR_ANALYSIS = 6000
FEATURE_KEYS = (
    "air", "warmth", "brightness", "bass", "clarity", "hypnotic", "dreamy", "space",
    "width", "intimacy", "punch", "joy", "depth", "energy", "vintage",
)
KEY_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
MAJOR_PROFILE = np.asarray([6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88], dtype=np.float64)
MINOR_PROFILE = np.asarray([6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17], dtype=np.float64)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


def _smoothstep(x: float) -> float:
    x = _clamp(x, 0.0, 1.0)
    return x*x*(3.0-2.0*x)


def _seconds(v: Any) -> float:
    if v is None:
        return 0.0
    try:
        if hasattr(v, "total_seconds"):
            return float(v.total_seconds())
    except Exception:
        pass
    try:
        # Some WinRT projections expose a 100 ns duration integer.
        d = getattr(v, "duration", None)
        if d is not None:
            return float(d) / 10_000_000.0
    except Exception:
        pass
    try:
        return float(v)
    except Exception:
        return 0.0


class SpotifyMediaController:
    """Optional Windows GSMTC transport bridge.

    This never touches the audio thread. If PyWinRT is missing or Spotify refuses
    an operation, the DJ audio engine continues normally and reports the capability
    as unavailable.
    """

    @staticmethod
    async def _get_session():
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
        manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        sessions = list(manager.get_sessions())
        ranked = sorted(
            sessions,
            key=lambda s: (0 if "spotify" in str(getattr(s, "source_app_user_model_id", "")).lower() else 1),
        )
        for s in ranked:
            if "spotify" in str(getattr(s, "source_app_user_model_id", "")).lower():
                return s
        return None

    @classmethod
    async def _status_async(cls) -> dict[str, Any]:
        s = await cls._get_session()
        if s is None:
            return {"available": False, "state": "Spotify media session not found"}
        title = artist = ""
        try:
            mp = await s.try_get_media_properties_async()
            title = str(getattr(mp, "title", "") or "")
            artist = str(getattr(mp, "artist", "") or "")
        except Exception:
            pass
        pos = dur = 0.0
        try:
            tl = s.get_timeline_properties()
            pos = _seconds(getattr(tl, "position", None))
            start = _seconds(getattr(tl, "start_time", None))
            end = _seconds(getattr(tl, "end_time", None))
            dur = max(0.0, end-start)
        except Exception:
            pass
        playing = False
        try:
            pi = s.get_playback_info()
            st = str(getattr(pi, "playback_status", "")).lower()
            playing = "playing" in st or st.endswith("_4")
        except Exception:
            pass
        return {
            "available": True,
            "state": "ready",
            "title": title,
            "artist": artist,
            "position_sec": round(pos, 3),
            "duration_sec": round(dur, 3),
            "playing": bool(playing),
        }

    @classmethod
    def status(cls) -> dict[str, Any]:
        try:
            return asyncio.run(cls._status_async())
        except Exception as exc:
            return {"available": False, "state": f"GSMTC unavailable: {exc}"}

    @classmethod
    def command(cls, action: str, value: float | None = None) -> dict[str, Any]:
        async def run():
            s = await cls._get_session()
            if s is None:
                return {"ok": False, "error": "Spotify media session not found"}
            a = str(action or "").lower()
            if a == "play_pause":
                ok = await s.try_toggle_play_pause_async()
            elif a == "play":
                ok = await s.try_play_async()
            elif a == "pause":
                ok = await s.try_pause_async()
            elif a == "next":
                ok = await s.try_skip_next_async()
            elif a == "previous":
                ok = await s.try_skip_previous_async()
            elif a == "seek":
                ok = await s.try_change_playback_position_async(int(max(0.0, float(value or 0.0))*10_000_000.0))
            else:
                return {"ok": False, "error": f"Unsupported transport action: {action}"}
            return {"ok": bool(ok)}
        try:
            return asyncio.run(run())
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


@dataclass
class LivePolicy:
    prompt: str
    style: str = "balanced"
    autonomy: str = "autopilot"
    creativity: float = 0.35
    phrase_bars: int = 8
    eq_motion: float = 0.40
    filter_motion: float = 0.32
    fx_motion: float = 0.20
    loop_probability: float = 0.12
    low_end_protection: float = 0.86
    semantic_fx: float = 0.26
    energy_direction: str = "hold"
    morph_target: str = "native"
    morph_bars: int = 16
    target_feel_bpm: float = 0.0
    performance_fx: float = 0.34
    fx_profile: str = "balanced"
    fx_presence: float = 0.46
    fx_riser: float = 0.46
    fx_impact: float = 0.52
    fx_reverse: float = 0.28
    fx_snare_rush: float = 0.26
    fx_space: float = 0.24
    fx_echo: float = 0.24
    fx_phrase_accents: float = 0.18
    action_density: float = 0.58
    agentic_authority: float = 1.0
    commit_beats: int = 2
    temporal_segments: int = 5
    controller_mode: str = "concert"
    lookahead_sec: float = 15.0
    # v31.8.9: creativity-max increases qualified A<->B micro-shift cadence
    # without lowering musical-match requirements.
    deckb_shift_multiplier: float = 1.0
    # v31.12 FullRemix: how strongly the persistent remix arrangement layer
    # (re-drum + resident Deck B + pump + slicer sections) is allowed to run.
    remix_intensity: float = 0.0
    redrum_pattern: int = 0
    # v31.13 GridLock: shuffle feel for the rhythmic FX grid (0 = straight).
    swing_amount: float = 0.0
    # v31.16 Hats 2.0: style family + humanization depth.
    hat_style: int = 0
    hat_var: float = 0.5


class NeuralMusicBrainClient:
    """Out-of-band neural audio observer backed by the isolated CLAP worker."""
    def __init__(self, url: str = "http://127.0.0.1:8768/agent-state"):
        self.url = url

    def analyze(self, audio: np.ndarray, sample_rate: int, prompt: str) -> dict[str, Any]:
        x=np.asarray(audio,dtype=np.float32)
        if x.ndim==2:
            x=np.mean(x,axis=1,dtype=np.float32)
        x=np.nan_to_num(x,nan=0.0,posinf=0.0,neginf=0.0).astype('<f4',copy=False)
        # Keep request bounded; CLAP itself uses a <=10 s causal window.
        max_n=max(24000,int(sample_rate*9.75))
        if x.size>max_n: x=x[-max_n:]
        payload=json.dumps({
            "audio_b64":base64.b64encode(x.tobytes()).decode("ascii"),
            "sample_rate":int(sample_rate),
            "prompt":str(prompt or "")[:1200],
        }).encode("utf-8")
        req=urllib.request.Request(self.url,data=payload,headers={"Content-Type":"application/json"},method="POST")
        with urllib.request.urlopen(req,timeout=18.0) as resp:
            return json.loads(resp.read().decode("utf-8"))

class RealtimeAgenticDJ:
    """Multi-timescale live DJ agent for the Spotify/JoyMetric route.

    Deck A is the live Spotify stream. Deck B is a realtime loop/roll deck built
    from JoyMetric's own rolling PCM history. The planner runs out-of-band and only
    posts bounded controller targets to NativeRealtimeEngine.
    """

    def __init__(self, realtime_engine):
        self.engine = realtime_engine
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False
        self._prompt = "dark modern hip-hop, clean 808, musical phrase-aware changes, restrained FX"
        self._policy = self.compile_policy(self._prompt, "autopilot", 0.35)
        self._autonomy = "autopilot"
        self._creativity = 0.35
        self._approved = False
        self._locked = False
        self._manual = False
        self._regen = 0
        self._analysis: dict[str, Any] = {}
        self._future_analysis: dict[str, Any] = {}
        self._timeline: list[dict[str, Any]] = []
        self._history: list[dict[str, Any]] = []
        self._media: dict[str, Any] = {"available": False, "state": "not checked"}
        self._last_media_poll = 0.0
        self._last_context = 0.0
        self._last_future_context = 0.0
        self._last_plan = 0.0
        self._bar_anchor_t = 0.0
        self._bar_index = 0
        self._last_action_bar = -999
        self._last_action_beat = -999
        self._beat_anchor_t = 0.0
        self._beat_index = 0
        self._beat_phase = 0.0
        self._future_segments: list[dict[str, Any]] = []
        self._performance_queue: list[dict[str, Any]] = []
        self._last_segment_scan = 0.0
        self._last_queue_refresh = 0.0
        self._loop_capture_seq = 0
        self._loop_release_seq = 0
        self._loop_retrigger_seq = 0
        self._impact_seq = 0
        self._controller = self._neutral_controls()
        self._lookahead_ready = False
        self._lookahead_fill = 0.0
        self._audible_started = False
        self._scene: dict[str, Any] = {"name":"LISTENING","active":False,"stage":"PREVIEW","progress":0.0}
        self._scene_start_bar = 0
        self._scene_impact_fired = False
        self._scene_loop_fired = False
        self._message = "Start Spotify, choose Audio Out, then press LET'S GO."
        self._error = ""
        # v31.17: /agent-state is served by the realtime semantic worker itself (JOY_SEMANTIC_PORT);
        # the old hard-coded :8768 pointed at nothing, so the CLAP action brain was always in fallback.
        self._brain = NeuralMusicBrainClient(url="http://%s:%d/agent-state" % (os.environ.get("JOY_SEMANTIC_HOST", "127.0.0.1"), int(os.environ.get("JOY_SEMANTIC_PORT", "8767") or 8767)))
        self._beat_clock = BeatClock()
        self._beat_err_streak = 0
        self._last_beat_meas_t = -1.0
        self._beat_sec_prev = 0.0
        self._media_track_key = ""
        self._future_cache = None
        self._weave = None; self._weave_next_w = -1; self._weave_info = {}
        self._present_cache = None
        self._last_future_snap = 0.0
        # v31.18 GrooveTruth
        self._present_end_frame = 0
        self._grid_anchor_frame = 0.0
        self._bar_offset = 0
        self._bar_votes = [0.0, 0.0, 0.0, 0.0]
        self._clarity_s = -1.0
        self._sr = 48000
        # v31.19 DrumMind (AI drummer)
        self._drum_brain = None
        self._drum_kit = None
        self._drum_kit_at = 0.0
        self._drum_pat_seq = 0
        self._drum_last_gen_beat = -999
        self._drum_vel = []
        self._drum_mt = []
        self._drum_info = {}
        self._drum_polish_started = False
        # v31.21 SynthWeave (Stable Audio synth / melody layer on the captured loop)
        self._synth = None
        self._synth_last_seq = -1
        self._synth_ready_seq = -1
        self._synth_info = {}
        self._synth_kind_i = 0
        # v31.22 FutureSynth: synth parts composed from the record's upcoming bars
        self._synth_future_pending = None
        self._synth_future_last_bar = -999
        self._synth_future_count = 0
        self._synth_D = 0
        self._synth_ms_ema = 6000.0
        self._synth_sched = []          # scheduled clips awaiting the play-time harmonic gate
        # v31.23 RackMind: DSP synth rack + 16 s / 8-block planner
        self._rack = None; self._planner = None; self._rack_queue = deque(); self._rack_pending = {}
        self._rack_info = {"state": "off"}; self._rack_last_k = -1; self._rack_now = None; self._track_change_frame = None
        self._rack_blocks = {}
        self._rack_timbre = None; self._rack_profile = {}; self._rack_profile_key = None
        self._groove_live = None; self._groove_swing = 0.0; self._groove_info = {}
        self._composer = None; self._gen_info = {"state": "off"}
        self._genre_probe = None
        self._brain_thread: threading.Thread | None = None
        self._dj_controller = os.environ.get("JOY_DJ_CONTROLLER", "0") == "1"   # v31.30.25: AI DJ gesture controller off by default (VocalWeave + DSP FX stay)
        self._brain_stop = threading.Event()
        self._brain_event = threading.Event()
        self._brain_job: tuple[np.ndarray,int,str] | None = None
        self._brain_lock = threading.RLock()
        self._brain_generation = 0
        self._neural: dict[str, Any] = {"ready":False,"state":"waiting for neural audio window"}
        self._neural_memory: list[dict[str, Any]] = []
        self._last_neural_request = 0.0
        self._decision: dict[str, Any] = {"action":"NO_ACTION","score":0.0,"rationale":"waiting for neural context"}
        self._director = DJDirectorV3()
        self._director_selected: dict[str, Any] = {}
        self._director_candidates: list[dict[str, Any]] = []
        self._automation_lanes: list[dict[str, Any]] = []
        # v31.10 Musical Intelligence Core + temporary manual overrides + A/B
        # contrary-motion transition state.
        self._mi = MusicalIntelligence()
        self._overrides: dict[str, tuple[float, float]] = {}
        self._transition: dict[str, Any] = {"active": False, "name": "", "a_role": "HOLD", "b_role": "HOLD", "start_beat": 0, "end_beat": 0}
        # v31.11 StageCraft: one-shot triggers (spinback/pump sync) + a small
        # beat-quantized deferred-trigger list used by the roll ladder.
        self._fx_brake_seq = 0
        self._pump_sync_seq = 0
        self._pending_triggers: list[dict[str, Any]] = []
        # v31.12 FullRemix arrangement state: which persistent remix section is
        # active and until which beat.  The arranger is the "remix producer" —
        # it holds layers for whole phrases instead of letting them decay.
        self._remix: dict[str, Any] = {"enabled": False, "section": "—", "until_beat": 0, "last_capture_beat": -999, "entered_beat": 0}
        self._set_start_t = time.monotonic()

    @staticmethod
    def _neutral_controls() -> dict[str, Any]:
        return {
            "enabled": True,
            "bpm": 100.0,
            "a_low_db": 0.0, "a_mid_db": 0.0, "a_high_db": 0.0,
            "a_filter": 0.0, "a_gain": 1.0,
            "b_low_db": 0.0, "b_mid_db": 0.0, "b_high_db": 0.0,
            "b_filter": 0.0, "b_gain": 1.0,
            "crossfader": 0.0, "b_layer": 0.0, "b_texture": 0.0, "b_transient": 0.0, "b_slicer_mix": 0.0, "b_slice_mode": 0.0, "b_fx": 0.0,
            "reverb": 0.0, "echo": 0.0,
            "noise_riser": 0.0, "fx_reverse_swell": 0.0, "fx_snare_rush": 0.0, "impact_strength": 0.0,
            "drum_drive": 0.0, "doubletime": 0.0,
            "fx_rack_wet": 0.0, "fx_echo_send": 0.0, "fx_echo_feedback": 0.0, "fx_echo_beats": 0.5,
            "fx_gate": 0.0, "fx_gate_div": 2.0, "fx_width": 0.0, "fx_duck": 0.0,
            "perf_punch": 0.0, "perf_energy": 0.0, "perf_clarity": 0.0, "perf_air": 0.0, "bass_tighten": 0.0,
            "fx_pump": 0.0, "fx_pump_cycle": 1.0, "fx_bass_cut": 0.0, "fx_rush_accel": 0.0,
            "fx_brake_beats": 1.0, "fx_brake_mode": 0.0,
            "fx_redrum": 0.0, "fx_redrum_pattern": 0.0,
            "fx_swing": 0.0, "grid_conf": 0.0, "grid_anchor_t": 0.0, "grid_lead_ms": 0.0,
            "fx_redrum_var": 0.0, "fx_redrum_fill": 0.0,
            "fx_bass_synth": 0.0, "fx_bass_pattern": 0.0, "fx_bass_root": 9.0, "fx_bass_conf": 0.0,
            "fx_stab": 0.0, "fx_stab_pattern": 0.0,
            "fx_weave_offset_beats": 16.0, "fx_weave_cell_beats": 2.0,
            "fx_hat_style": 0.0, "fx_hat_var": 0.5, "fx_hat_fill": 0.5,
            "loop_beats": 4.0,
            "loop_capture_seq": 0,
            "loop_release_seq": 0,
            "loop_retrigger_seq": 0,
            "impact_seq": 0,
            "fx_brake_seq": 0,
            "fx_pump_sync_seq": 0,
        }

    @staticmethod
    def compile_policy(prompt: str, autonomy: str, creativity: float) -> LivePolicy:
        """Compile prompt intent into musical + performance-FX policy.

        v31.5 keeps the v31.4 prompt vocabulary but routes it through a controller-style Performance FX Rack; Sound FX as a first-class performance vocabulary rather than
        a fixed garnish.  The neural action bank still decides *when* a gesture is
        musically appropriate; this compiler decides *what kind* of FX palette the
        prompt asks for and how dominant it may become.
        """
        s = " ".join(str(prompt or "").lower().split())
        p = LivePolicy(prompt=str(prompt or "").strip(), autonomy=autonomy, creativity=_clamp(creativity,0,1))
        dnb = any(k in s for k in ("drum n bass","drum and bass","drum & bass","dnb","jungle","breakbeat","break beat"))
        if dnb:
            p.style = "drum-and-bass"; p.fx_profile="dnb-banger"
            p.phrase_bars = 4
            p.eq_motion=.58; p.filter_motion=.52; p.fx_motion=.40
            p.loop_probability=.16+.16*p.creativity; p.low_end_protection=.98; p.semantic_fx=.58
            p.energy_direction="rise"; p.morph_target="dnb"; p.morph_bars=16; p.target_feel_bpm=174.0; p.performance_fx=.84
            p.fx_presence=.88; p.fx_riser=.88; p.fx_impact=.96; p.fx_reverse=.66; p.fx_snare_rush=.86; p.fx_space=.24; p.fx_echo=.34; p.fx_phrase_accents=.62
        elif any(k in s for k in ("hiphop", "hip-hop", "trap", "rap", "808")):
            p.style = "hip-hop"; p.fx_profile="hip-hop-performance"
            p.phrase_bars = 8; p.eq_motion=.34; p.filter_motion=.24; p.fx_motion=.18
            p.loop_probability=.10+.12*p.creativity; p.low_end_protection=.96; p.semantic_fx=.24
            p.fx_presence=.48; p.fx_riser=.38; p.fx_impact=.62; p.fx_reverse=.34; p.fx_snare_rush=.30; p.fx_space=.18; p.fx_echo=.32; p.fx_phrase_accents=.30
        elif any(k in s for k in ("house", "techno", "club", "dance")):
            p.style = "club"; p.fx_profile="club-lift"
            p.phrase_bars = 8; p.eq_motion=.52; p.filter_motion=.50; p.fx_motion=.34
            p.loop_probability=.18+.18*p.creativity; p.low_end_protection=.90; p.semantic_fx=.38
            p.fx_presence=.64; p.fx_riser=.70; p.fx_impact=.74; p.fx_reverse=.54; p.fx_snare_rush=.46; p.fx_space=.32; p.fx_echo=.40; p.fx_phrase_accents=.42
        elif any(k in s for k in ("dreamy", "hypnotic", "ambient", "floating")):
            p.style = "dreamy"; p.fx_profile="dreamy-space"
            p.phrase_bars = 8; p.eq_motion=.30; p.filter_motion=.38; p.fx_motion=.44
            p.loop_probability=.10+.12*p.creativity; p.low_end_protection=.90; p.semantic_fx=.50
            p.fx_presence=.62; p.fx_riser=.34; p.fx_impact=.30; p.fx_reverse=.82; p.fx_snare_rush=.14; p.fx_space=.78; p.fx_echo=.58; p.fx_phrase_accents=.28

        hard = any(k in s for k in ("hard", "aggressive", "cut", "fast", "banger", "heavy drums", "hard drums", "festival", "huge", "massive"))
        if hard:
            p.filter_motion=min(.74,p.filter_motion+.18); p.eq_motion=min(.76,p.eq_motion+.14); p.fx_motion=min(.56,p.fx_motion+.10)
            p.performance_fx=min(1.0,p.performance_fx+.16); p.fx_presence=min(1.0,p.fx_presence+.14)
            p.fx_impact=min(1.0,p.fx_impact+.16); p.fx_snare_rush=min(1.0,p.fx_snare_rush+.12); p.fx_phrase_accents=min(1.0,p.fx_phrase_accents+.12)

        # Direct FX language has authority over the palette.  This is intentionally
        # sparse: it changes strengths, while phrase/transient/vocal guards still
        # decide whether a gesture is safe to execute right now.
        if any(k in s for k in ("riser","rise fx","hiss","noise sweep","sweep up","uplifter","whoosh")):
            p.fx_riser=min(1.0,max(p.fx_riser,.82)); p.fx_reverse=min(1.0,max(p.fx_reverse,.48)); p.fx_presence=min(1.0,max(p.fx_presence,.76))
        if any(k in s for k in ("impact","slam","hit on the drop","big drop","drop hard","explosive")):
            p.fx_impact=min(1.0,max(p.fx_impact,.92)); p.fx_presence=min(1.0,max(p.fx_presence,.78))
        if any(k in s for k in ("snare rush","snare roll","drum roll","drum fill","fill into the drop","rapid drums")):
            p.fx_snare_rush=min(1.0,max(p.fx_snare_rush,.88)); p.fx_phrase_accents=min(1.0,max(p.fx_phrase_accents,.58))
        if any(k in s for k in ("reverse","reverse swell","reverse cymbal","swell","suckback")):
            p.fx_reverse=min(1.0,max(p.fx_reverse,.90)); p.fx_presence=min(1.0,max(p.fx_presence,.72))
        if any(k in s for k in ("echo","echo throw","delay throw","dub echo")):
            p.fx_echo=min(1.0,max(p.fx_echo,.82)); p.fx_presence=min(1.0,max(p.fx_presence,.64))
        if any(k in s for k in ("reverb","space","spacious","wash","bloom")):
            p.fx_space=min(1.0,max(p.fx_space,.78)); p.fx_presence=min(1.0,max(p.fx_presence,.62))
        if any(k in s for k in ("trans gate","transform gate","rhythm gate","gated","chop the beat","chopped rhythm","stutter gate")):
            p.fx_phrase_accents=min(1.0,max(p.fx_phrase_accents,.90)); p.fx_presence=min(1.0,max(p.fx_presence,.76)); p.performance_fx=min(1.0,max(p.performance_fx,.84))
        if any(k in s for k in ("echo out","post fader echo","post-fader echo","ping pong echo","ping-pong echo","feedback echo","beat echo")):
            p.fx_echo=min(1.0,max(p.fx_echo,.94)); p.fx_space=min(1.0,max(p.fx_space,.58)); p.fx_presence=min(1.0,max(p.fx_presence,.78))
        if any(k in s for k in ("wide fx","wide effects","stereo fx","wide transition","stereo width")):
            p.fx_space=min(1.0,max(p.fx_space,.88)); p.fx_presence=min(1.0,max(p.fx_presence,.70))
        if any(k in s for k in ("sound fx", "sound effects", "fx heavy", "strong fx", "more fx", "performance fx")):
            p.fx_presence=min(1.0,max(p.fx_presence,.84)); p.performance_fx=min(1.0,max(p.performance_fx,.82))

        smooth = any(k in s for k in ("smooth", "clean", "professional", "subtle", "restrained", "tasteful"))
        if smooth:
            # Professional/smooth means longer arcs and fewer simultaneous layers,
            # not inaudible FX.  Explicit/genre FX keep a useful floor.
            p.fx_motion*=.86; p.loop_probability*=.70; p.low_end_protection=max(p.low_end_protection,.95)
            p.fx_presence*=.94; p.fx_space*=.92; p.fx_echo*=.90
            if p.morph_target != "native": p.morph_bars=max(p.morph_bars,16)
        if any(k in s for k in ("very restrained fx","minimal fx","almost no fx","dry dj","no sound fx","no fx")):
            p.fx_presence=min(p.fx_presence,.18); p.fx_riser=min(p.fx_riser,.12); p.fx_impact=min(p.fx_impact,.24); p.fx_reverse=min(p.fx_reverse,.10); p.fx_snare_rush=min(p.fx_snare_rush,.08); p.fx_space=min(p.fx_space,.12); p.fx_echo=min(p.fx_echo,.12); p.fx_phrase_accents=min(p.fx_phrase_accents,.08)
            p.fx_profile="minimal"
        if any(k in s for k in ("raise energy", "build", "rising", "increase energy")):
            p.energy_direction="rise"
        elif any(k in s for k in ("lower energy", "cool down", "reduce energy")):
            p.energy_direction="fall"
        # v31.8.4 Agentic Authority Boost.  Creativity now controls aggregate DJ
        # authority, not raw output gain.  At creativity=1.0 the planner/controller
        # combination reaches ~3.6x aggregate intervention versus the normal 0.35
        # operating point through density + intensity + persistence, while every DSP
        # parameter remains inside the same bounded/headroom-safe controller surface.
        hi=_smoothstep(_clamp((p.creativity-.45)/.55,0,1))
        p.agentic_authority=1.0+2.60*hi
        p.action_density=_clamp(.46+.38*p.creativity+.14*hi, .30, .98)
        if any(k in s for k in ("concert set","festival set","performance set","active dj","busy dj","more dj actions","more actions","dj more")):
            p.action_density=max(p.action_density,.84+.10*hi)
        if hard:
            p.action_density=min(.99,p.action_density+.08)
        if any(k in s for k in ("minimal dj","very restrained dj","almost no dj moves","leave it alone")):
            p.action_density=min(p.action_density,.34)
            p.agentic_authority=min(p.agentic_authority,1.15)
            p.deckb_shift_multiplier=1.0
            hi=min(hi,.06)
        # More creativity makes the Agentic layer more *present*, but does not
        # multiply samples or master gain. Shared-headroom / limiter guards remain
        # authoritative downstream.
        p.performance_fx=_clamp(p.performance_fx*(1.0+.55*hi),0,1)
        p.fx_presence=_clamp(p.fx_presence*(1.0+.45*hi),0,1)
        p.eq_motion=_clamp(p.eq_motion*(1.0+.24*hi),0,1)
        p.filter_motion=_clamp(p.filter_motion*(1.0+.30*hi),0,1)
        p.fx_motion=_clamp(p.fx_motion*(1.0+.34*hi),0,1)
        p.commit_beats=1 if hi>.62 else 2
        # 1.0x at normal creativity -> 3.5x at max. This multiplier controls
        # *opportunity cadence* only; the realtime musical gate remains fixed.
        p.deckb_shift_multiplier=1.0+2.50*hi
        p.temporal_segments=7 if p.action_density>.90 else (6 if p.action_density>.76 else 5)
        # v31.12 FullRemix intensity.  Below ~0.55 creativity the app behaves like
        # a tasteful DJ; above it, a persistent remix arrangement layer switches
        # on and scales with creativity so creativity=1.0 is unmistakably a remix
        # (new drum groove under the song, resident second voice, section cycle).
        ri=_smoothstep(_clamp((p.creativity-.55)/.45,0,1))*.85
        if any(k in s for k in ("full remix","remix mode","remix it","re-drum","redrum","new beat","own beat","make it a remix")):
            ri=max(ri,.78)
        if any(k in s for k in ("no redrum","no new drums","original drums","keep the drums","no remix")):
            ri=0.0
        p.remix_intensity=_clamp(ri,0,1)
        if p.style in ("club",) or any(k in s for k in ("house","techno","dance","edm","four on the floor")):
            p.redrum_pattern=0
        elif p.style=="hip-hop":
            p.redrum_pattern=1
        elif p.style=="drum-and-bass":
            p.redrum_pattern=2
        else:
            p.redrum_pattern=0
        if any(k in s for k in ("halftime","half time","half-time")): p.redrum_pattern=1
        if any(k in s for k in ("breakbeat","break beat","broken beat")): p.redrum_pattern=2
        if any(k in s for k in ("offbeat","upbeat bounce")): p.redrum_pattern=3
        p.hat_style={"club":0,"hip-hop":1,"drum-and-bass":2,"dreamy":3}.get(p.style,0)
        if any(k in s for k in ("sparse hats","minimal hats","few hats")): p.hat_style=3
        p.hat_var=_clamp(.35+.60*hi,0,1)
        if any(k in s for k in ("swing","shuffle","swung","shuffled")): p.swing_amount=.30
        if any(k in s for k in ("hard swing","heavy shuffle","triplet feel")): p.swing_amount=.45
        if any(k in s for k in ("straight 16","no swing","straight feel")): p.swing_amount=0.0
        p.controller_mode=("FULL REMIX · CONCERT ROLLING-HORIZON" if p.remix_intensity>.30 else ("CONCERT ROLLING-HORIZON · HIGH AUTHORITY" if hi>.62 else "CONCERT ROLLING-HORIZON"))
        return p

    def start(self, prompt: str, autonomy: str = "autopilot", creativity: float = 0.35):
        self.stop()
        with self._lock:
            self._prompt = str(prompt or self._prompt).strip()[:1200]
            self._autonomy = str(autonomy or "autopilot").lower()
            if self._autonomy not in {"autopilot","copilot","manual"}: self._autonomy="autopilot"
            self._creativity = _clamp(creativity,0,1)
            self._policy = self.compile_policy(self._prompt,self._autonomy,self._creativity)
            self._approved = self._autonomy == "autopilot"
            self._manual = self._autonomy == "manual"
            self._locked = False
            self._error = ""
            self._message = "LOOKAHEAD · buffering 15 s future PCM before first audible bar."
            self._timeline=[]; self._history=[]; self._bar_anchor_t=0.0; self._bar_index=0
            self._beat_anchor_t=0.0; self._beat_index=0; self._beat_phase=0.0; self._last_action_beat=-999
            self._beat_clock.reset(); self._beat_err_streak=0; self._last_beat_meas_t=-1.0; self._beat_sec_prev=0.0; self._media_track_key=""
            self._future_cache=None; self._present_cache=None; self._last_future_snap=0.0
            self._weave_next_w=-1
            if self._weave is not None:
                try:
                    self._weave.reset_session()
                except Exception:
                    pass
            self._present_end_frame=0; self._grid_anchor_frame=0.0; self._bar_offset=0; self._bar_votes=[0.0,0.0,0.0,0.0]; self._clarity_s=-1.0
            self._drum_kit=None; self._drum_kit_at=0.0; self._drum_last_gen_beat=-999; self._drum_vel=[]; self._drum_mt=[]; self._drum_info={}; self._drum_polish_started=False
            self._synth_last_seq=-1; self._synth_ready_seq=-1; self._synth_info={}; self._synth_future_pending=None; self._synth_future_last_bar=-999; self._synth_future_count=0; self._synth_D=0; self._synth_ms_ema=6000.0; self._synth_sched=[]
            self._rack=None; self._planner=None; self._rack_queue=deque(); self._rack_pending={}; self._rack_last_k=-1; self._rack_now=None
            self._rack_blocks={}; self._track_change_frame=None; self._rack_info={"state":"off"}
            if os.environ.get("JOY_SYNTH_RACK","1")!="0":
                try:
                    self._rack=SynthRack(self._sr); self._planner=BlockPlanner(self._sr, 2.0, 8)
                    self._rack_timbre=TimbreSelector(self._sr) if os.environ.get("JOY_SYNTH_TIMBRE","1")!="0" else None
                    self._groove_live=GrooveLive(self._sr) if os.environ.get("JOY_GROOVENET","1")!="0" else None
                    self._composer=GenComposer(self._sr, "http://127.0.0.1:%d" % int(os.environ.get("JOY_AMT_PORT","8769") or 8769)) if os.environ.get("JOY_GEN_AI","1")!="0" else None
                    self._gen_info={"state":("ready" if (self._composer and self._composer.enabled) else "no generator"),"count":0,"melodynet":bool(self._composer and self._composer.net_ready),"amt":bool(self._composer and self._composer.amt),"songmind":bool(self._composer and self._composer.songmind)}
                    self._genre_probe=GenreProbe(self._sr) if os.environ.get("JOY_GENRE_PROBE","1")!="0" else None
                    self._groove_swing=0.0; self._groove_info={"ready":bool(self._groove_live and self._groove_live.ready),"error":(self._groove_live.error if self._groove_live else "off")}
                    self._rack_profile={}; self._rack_profile_key=None
                    self._rack_info={"state":"idle","engine":"rack"}
                except Exception as exc:
                    self._rack=None; self._rack_info={"state":"rack failed: %s" % str(exc)[:80]}
            if os.environ.get("JOY_SYNTH_AI","0")=="1":
                try:
                    self._synth=SynthWeave()
                    self._synth_info={"state":"idle" if self._synth.enabled else "worker offline"}
                except Exception as exc:
                    self._synth=None; self._synth_info={"state":"error","error":str(exc)[:80]}
            if self._drum_brain is None:
                try:
                    _mdir=os.path.join(os.path.dirname(os.path.abspath(__file__)),"models")
                    self._drum_brain=DrumMind(os.path.join(_mdir,"drummind.npz"),os.path.join(_mdir,"drummind_library.npz"))
                except Exception as exc:
                    self._drum_brain=None; self._drum_info={"error":str(exc)[:120]}
            self._future_segments=[]; self._performance_queue=[]; self._last_segment_scan=0.0; self._last_queue_refresh=0.0
            self._director = DJDirectorV3(); self._director_selected={}; self._director_candidates=[]; self._automation_lanes=[]
            self._mi = MusicalIntelligence(); self._overrides={}
            self._transition={"active":False,"name":"","a_role":"HOLD","b_role":"HOLD","start_beat":0,"end_beat":0}
            self._fx_brake_seq=0; self._pump_sync_seq=0; self._pending_triggers=[]
            self._remix={"enabled":False,"section":"—","until_beat":0,"last_capture_beat":-999,"entered_beat":0}
            self._set_start_t=time.monotonic()
            self._analysis={}; self._future_analysis={}
            self._neural_memory=[]; self._neural={"ready":False,"state":"neural brain warming · waiting for future raw-audio embedding"}
            self._decision={"action":"NO_ACTION","score":0.0,"rationale":"waiting for neural context"}; self._last_neural_request=0.0
            self._controller=self._neutral_controls()
            self._lookahead_ready=False; self._lookahead_fill=0.0; self._audible_started=False
            self._scene={"name":"LOOKAHEAD","active":False,"stage":"PREVIEW","progress":0.0,"target":self._policy.morph_target}
            self._scene_start_bar=0; self._scene_impact_fired=False; self._scene_loop_fired=False
            self._impact_seq=0
        self._stop.clear(); self._running=True
        self._send_controls(self._controller, enabled=True)
        self._brain_stop.clear(); self._brain_event.clear(); self._brain_generation += 1
        brain_generation=self._brain_generation
        if self._dj_controller:
            self._brain_thread=threading.Thread(target=self._brain_loop,args=(brain_generation,),daemon=True,name="joymetric-neural-dj-brain")
            self._brain_thread.start()
        self._thread=threading.Thread(target=self._loop,daemon=True,name="joymetric-realtime-agentic-dj")
        self._thread.start()

    def stop(self):
        self._brain_generation += 1; self._stop.set(); self._brain_stop.set(); self._brain_event.set()
        t=self._thread
        if t and t.is_alive() and t is not threading.current_thread(): t.join(timeout=.8)
        self._thread=None
        bt=self._brain_thread
        if bt and bt.is_alive() and bt is not threading.current_thread(): bt.join(timeout=.8)
        self._brain_thread=None; self._running=False
        try: self._send_controls(self._neutral_controls(), enabled=False)
        except Exception: pass

    def approve(self):
        with self._lock: self._approved=True; self._message="Copilot plan approved · executing next quantized actions."

    def lock(self, value: bool = True):
        with self._lock: self._locked=bool(value); self._message="Agent controller locked." if value else "Agent controller unlocked."

    def take_control(self):
        """Toggle full manual mode.  A second press hands control back to the agent."""
        with self._lock:
            if self._manual:
                self._manual=False; self._autonomy="autopilot"; self._approved=True; self._overrides={}
                self._message="Agent resumed · controller authority returned to the performance brain."
            else:
                self._manual=True; self._autonomy="manual"
                self._message="Manual takeover · agent analysis remains visible, controls are yours."

    def regenerate(self):
        with self._lock: self._regen += 1; self._last_plan=0.0; self._approved=self._autonomy=="autopilot"; self._message="Replanning next 16 bars from current musical context."

    def manual_controls(self, patch: dict[str, Any]):
        """Apply user slider input.

        v31.10 fix: touching a deck slider no longer silently kills the agent.
        In autopilot/copilot each touched parameter becomes a bounded *temporary
        override* (the agent keeps performing everything else and resumes this
        parameter after MANUAL_OVERRIDE_HOLD_SEC).  Full manual mode still owns
        every parameter permanently.
        """
        now=time.monotonic()
        with self._lock:
            full_manual=self._manual or self._autonomy=="manual"
            for k,v in (patch or {}).items():
                if k not in self._controller: continue
                try: fv=float(v)
                except Exception: continue
                self._controller[k]=fv
                if not full_manual:
                    self._overrides[k]=(fv, now+MANUAL_OVERRIDE_HOLD_SEC)
            if not full_manual and self._overrides:
                keys=", ".join(sorted(self._overrides)[:4])
                self._message=f"MANUAL OVERRIDE · {keys} held for {MANUAL_OVERRIDE_HOLD_SEC:.0f}s · agent keeps performing the rest."
            c=dict(self._controller); live=bool(self._running)
        # While stopped this only stages values (enabled state is left untouched
        # so a later non-agentic Realtime session is not accidentally pre-armed).
        self._send_controls(c, enabled=True if live else None)

    def _apply_overrides_locked(self, c: dict[str, Any], now: float) -> dict[str, Any]:
        """Caller must hold self._lock.  Expired overrides are dropped."""
        if not self._overrides:
            return c
        for k in list(self._overrides):
            v,exp=self._overrides[k]
            if now>=exp:
                del self._overrides[k]; continue
            if k in c: c[k]=v
        return c

    def _lane_active(self, key: str) -> bool:
        return any(str(x.get("key"))==key for x in self._automation_lanes)

    def set_prompt(self, prompt: str, autonomy: str | None = None, creativity: float | None = None):
        """Update the shared live prompt without forcing manual takeover."""
        with self._lock:
            self._prompt=str(prompt or self._prompt).strip()[:1200]
            if autonomy is not None:
                a=str(autonomy or self._autonomy).lower()
                if a in {"autopilot","copilot","manual"}: self._autonomy=a
            if creativity is not None:
                self._creativity=_clamp(float(creativity),0,1)
            self._policy=self.compile_policy(self._prompt,self._autonomy,self._creativity)
            self._approved=self._autonomy=="autopilot"
            self._manual=self._autonomy=="manual"
            # Keep already-committed near-term beats, discard speculative far events.
            keep_until=self._beat_index+max(1,int(self._policy.commit_beats))
            self._performance_queue=[q for q in self._performance_queue if int(q.get("beat",999999))<=keep_until]
            self._last_queue_refresh=0.0; self._last_plan=0.0; self._last_neural_request=0.0
            self._message="SHARED PROMPT UPDATED · Realtime FX + Concert Agent replanning together."
        return asdict(self._policy)

    def trigger_macro(self, action: str):
        allowed={
            "MACRO_TENSION","MACRO_ECHO_OUT","MACRO_ROLL_ACCEL","MACRO_DROP","MACRO_SPACE_BREAK","MACRO_GROOVE_LIFT",
            "MACRO_FILTER_SWEEP","MACRO_TRANS_BUILD","MACRO_ROLL_HALF","MACRO_ROLL_QUARTER",
            "MACRO_ECHO_HALF","MACRO_ECHO_ONE","MACRO_WASH_OUT","MACRO_PUNCH_IN","MACRO_DRUM_FILL","MACRO_CLEAN_RESET",
            "MACRO_SPINBACK","MACRO_BRAKE","MACRO_PUMP","MACRO_BUILD",
            "MACRO_FLASHFWD","MACRO_JUMPBACK","MACRO_WEAVE",
        }
        action=str(action or "").upper()
        if action not in allowed:
            raise ValueError(f"Unsupported concert macro: {action}")
        with self._lock:
            target=int(self._beat_index)+1
            ev=self._concert_event(target,action,1.0,"manual performance-pad request; quantized to next beat","PERFORMANCE PAD")
            self._performance_queue=[q for q in self._performance_queue if not (int(q.get("beat",-1))==target and str(q.get("action"))==action)]
            self._performance_queue.append(ev); self._performance_queue.sort(key=lambda q:int(q.get("beat",0)))
            self._message=f"CONCERT PAD · {action.replace('MACRO_','')} armed for beat {target}."
            return ev

    def capture_loop(self, beats: float):
        with self._lock:
            self._loop_capture_seq += 1
            self._controller["loop_beats"]=_clamp(float(beats),0.25,16.0)
            self._controller["loop_capture_seq"]=self._loop_capture_seq
            c=dict(self._controller)
        self._send_controls(c, enabled=True)

    def release_loop(self):
        with self._lock:
            self._loop_release_seq += 1; self._controller["loop_release_seq"]=self._loop_release_seq; self._controller["crossfader"]=0.0
            c=dict(self._controller)
        self._send_controls(c, enabled=True)

    def transport(self, action: str, value: float | None = None) -> dict[str, Any]:
        if action.startswith("beatjump"):
            try: bars=float(value or 0.0)
            except Exception: bars=0.0
            with self._lock: bpm=max(55.0,float(self._analysis.get("bpm") or 100.0)); pos=float(self._media.get("position_sec") or 0.0)
            target=max(0.0,pos+bars*4.0*60.0/bpm)
            result=SpotifyMediaController.command("seek",target)
        else:
            result=SpotifyMediaController.command(action,value)
        self._last_media_poll=0.0
        return result

    def status(self) -> dict[str, Any]:
        rt=self.engine.status()
        with self._lock:
            return {
                "running": bool(self._running and rt.get("running")),
                "message": self._message,
                "error": self._error or None,
                "prompt": self._prompt,
                "policy": asdict(self._policy),
                "autonomy": self._autonomy,
                "creativity": round(self._creativity,3),
                "approved": bool(self._approved), "locked": bool(self._locked), "manual": bool(self._manual),
                "analysis": dict(self._analysis), "future_analysis": dict(self._future_analysis), "timeline": list(self._timeline), "history": list(self._history[-16:]),
                "controller": ({"removed": True} if not self._dj_controller else dict(self._controller)), "media": dict(self._media),
                "lookahead_ready": bool(self._lookahead_ready), "lookahead_fill": round(float(self._lookahead_fill),3), "audible_started": bool(self._audible_started), "scene": dict(self._scene),
                "brain": {k:v for k,v in self._neural.items() if k != "embedding"},
                "decision": dict(self._decision),
                "memory_depth": len(self._neural_memory),
                "beat_index": int(self._beat_index), "beat_phase": round(float(self._beat_phase),3),
                "future_segments": list(self._future_segments), "performance_queue": list(self._performance_queue[:8]),
                "director_selected": dict(self._director_selected), "director_candidates": list(self._director_candidates[:6]),
                "performance_memory": list(self._history[-12:]), "automation_lanes": list(self._automation_lanes[:12]),
                "controller_action_count": 44, "controller_macro_count": 16, "neural_action_count": 20,
                "parameterized_family_count": len(PARAM_SCHEMAS), "parameterized_action_space": "continuous",
                "musical": self._mi.snapshot(float((self._future_segments[0].get("tonal_strength") if self._future_segments else 0.0) or 0.0)),
                "transition": {**dict(self._transition), "beat_float": round(float(self._beat_index)+float(self._beat_phase),3)},
                "remix": {"enabled": bool(self._remix.get("enabled")), "section": str(self._remix.get("section") or "—"),
                          "until_beat": int(self._remix.get("until_beat") or 0), "entered_beat": int(self._remix.get("entered_beat") or 0),
                          "intensity": round(float(getattr(self._policy,"remix_intensity",0.0)),3),
                          "pattern": ("FLOOR","HALF","BREAK","UPBEAT")[int(_clamp(float(getattr(self._policy,"redrum_pattern",0)),0,3))]},
                "overrides": {k: round(max(0.0,exp-time.monotonic()),1) for k,(v,exp) in self._overrides.items()},
                "drum_ai": dict(self._drum_info),
                "synth_ai": (dict(self._rack_info, blocks=[self._rack_blocks[k] for k in sorted(self._rack_blocks)][-8:], now=(dict((self._rack_now or {}).get("bias") or {}, character=(self._rack_now or {}).get("character")) if self._rack_now else None)) if self._rack is not None else dict(self._synth_info)),
                # v31.17: the agentic view reads only these realtime fields; the full 20 KB
                # realtime status is polled separately by the realtime panel.
                "realtime": {k: rt.get(k) for k in ("running","xruns","concealments","input_db","output_db","limiter_reduction_db","agentic_mixer","agentic_controls","agentic_lookahead_sec","sample_rate","bridge_fill_ms","bridge_target_ms","dsp_max_ms","capture_gap_max_ms","io_latency_ms","message") if k in rt},
            }

    def _analysis_features(self, audio: np.ndarray, sr: int, t_meas: float | None = None) -> dict[str, Any]:
        if audio is None or len(audio)<max(1024,int(sr*.5)):
            return {}
        x=np.mean(np.asarray(audio,dtype=np.float32),axis=1)
        x_full=x; sr_full=int(sr)
        if sr != SR_ANALYSIS:
            x=resample_poly(x, SR_ANALYSIS, sr).astype(np.float32)
        if len(x)<2048: return {}
        hop=120; frame=480
        # Energy/onset envelope at 20 ms hop; autocorrelation is done on this tiny envelope,
        # not on full-rate PCM, so 15 s analysis stays bounded and cheap.
        n=max(1,1+(len(x)-frame)//hop)
        env=np.empty(n,dtype=np.float64); low=np.empty(n,dtype=np.float64)
        win=np.hanning(frame).astype(np.float32)
        freqs=np.fft.rfftfreq(frame,1.0/SR_ANALYSIS)
        lowmask=(freqs>=45)&(freqs<=180); midmask=(freqs>=180)&(freqs<=2500); himask=(freqs>=2500)&(freqs<=SR_ANALYSIS*.48)
        prev=None; onset=np.zeros(n,dtype=np.float64); bass=np.zeros(n,dtype=np.float64); mids=np.zeros(n,dtype=np.float64); highs=np.zeros(n,dtype=np.float64)
        for i in range(n):
            seg=x[i*hop:i*hop+frame]
            if len(seg)<frame: seg=np.pad(seg,(0,frame-len(seg)))
            spec=np.abs(np.fft.rfft(seg*win)).astype(np.float64)
            env[i]=math.sqrt(float(np.mean(seg.astype(np.float64)**2))+1e-12)
            bass[i]=float(np.mean(spec[lowmask])) if np.any(lowmask) else 0.0
            mids[i]=float(np.mean(spec[midmask])) if np.any(midmask) else 0.0
            highs[i]=float(np.mean(spec[himask])) if np.any(himask) else 0.0
            if prev is not None: onset[i]=float(np.mean(np.maximum(spec-prev,0.0)))
            prev=spec
        onset=np.maximum(onset-np.median(onset),0.0)
        if np.max(onset)>0: onset/=np.max(onset)
        hop_s=hop/SR_ANALYSIS
        # v31.17 BeatClock v2 (beat_clock.py): 5 ms-hop log-flux envelope, comb
        # tempo profile with sub-lag refinement, song-level tempo memory with
        # octave-aware hysteresis and a fractional-period phase comb.  Replaces
        # the 20 ms-lag autocorrelation that quantised BPM to 83.33 / 115.38 and
        # flipped octaves between windows (measured on a live capture).
        t_meas=float(t_meas) if t_meas is not None else time.monotonic()
        bc=self._beat_clock.analyze(x,t_meas,x_full=x_full,sr_full=sr_full,groove=True)
        bpm=float(bc["bpm"]); conf=float(bc["bpm_confidence"]); beat_sec=float(bc["beat_sec"])
        to_next=float(bc["to_next_beat_sec"]); since=float(bc["since_last_beat_sec"])
        period=beat_sec/hop_s; p=max(2,int(round(period))); phase=0
        rms=float(np.sqrt(np.mean(x.astype(np.float64)**2)+1e-12)); peak=float(np.max(np.abs(x)))
        # Activity proxies are analysis meters, not stem separation.
        b=float(np.mean(bass)); m=float(np.mean(mids)); h=float(np.mean(highs)); total=max(1e-9,b+m+h)
        bass_activity=_clamp(3.0*b/total,0,1)
        melody_activity=_clamp(2.2*m/total,0,1)
        drums_activity=_clamp(float(np.percentile(onset,85))*1.25,0,1)
        vocal_activity=_clamp((1.35*m/total)*(1.0-.45*drums_activity),0,1)
        # Coarse live key estimate, intentionally low authority.
        key="—"
        try:
            f,t,zx=stft(x,fs=SR_ANALYSIS,nperseg=2048,noverlap=1536,boundary=None,padded=False)
            mag=np.abs(zx).astype(np.float64); chroma=np.zeros(12)
            for fi in np.where((f>=65)&(f<=2900))[0]:
                midi=int(round(69+12*math.log2(float(f[fi])/440.0))); chroma[midi%12]+=float(np.sum(mag[fi]**1.2))
            if np.sum(chroma)>0:
                chroma/=np.sum(chroma); scores=[]
                for root in range(12):
                    scores.append((float(np.dot(chroma,np.roll(MAJOR_PROFILE,root))),KEY_NAMES[root]))
                    scores.append((float(np.dot(chroma,np.roll(MINOR_PROFILE,root))),KEY_NAMES[root]+"m"))
                key=max(scores,key=lambda q:q[0])[1]
        except Exception: pass
        # 160-point rolling waveform for UI.
        bins=160; chunk=max(1,len(x)//bins); wave=[]
        for i in range(bins):
            seg=x[i*chunk:min(len(x),(i+1)*chunk)]; wave.append(float(np.percentile(np.abs(seg),92)) if len(seg) else 0.0)
        wm=max(wave) if wave else 1.0; wave=[round(v/max(1e-8,wm),3) for v in wave]
        return {
            "bpm":round(bpm,2),"bpm_confidence":round(conf,3),"beat_sec":round(beat_sec,4),"to_next_beat_sec":round(to_next,4),
            "_t":t_meas,"bpm_inst":round(float(bc.get("bpm_inst",0.0)),2),"tempo_lock":int(bc.get("lock_count",0)),"tempo_strength":round(float(bc.get("strength",0.0)),3),
            "since_last_beat_sec":round(since,4),"groove_clarity":round(float(bc.get("groove_clarity",0.0)),3),"groove_align":round(float(bc.get("groove_align",0.0)),3),
            "kick_on_grid":round(float(bc.get("kick_on_grid",0.0)),3),"hat_on_grid":round(float(bc.get("hat_on_grid",0.0)),3),
            "downbeat_class":int(bc.get("downbeat_class",0)),"downbeat_conf":round(float(bc.get("downbeat_conf",0.0)),3),"bar_pos":round(float(bc.get("bar_pos",0.0)),3),
            "src_kick":list(bc.get("src_kick",[])),"src_snare":list(bc.get("src_snare",[])),"src_hat":list(bc.get("src_hat",[])),
            "energy":round(_clamp(rms/.24,0,1),3),"peak_dbfs":round(20*math.log10(max(1e-8,peak)),1),"key":key,
            "bass_activity":round(bass_activity,3),"drums_activity":round(drums_activity,3),"vocal_activity":round(vocal_activity,3),"melody_activity":round(melody_activity,3),
            "waveform":wave,
        }

    def _submit_neural_window(self, audio: np.ndarray, sr: int):
        if not getattr(self, "_dj_controller", False):
            return                                             # v31.30.25: neural action brain removed (DSP FX stays)
        with self._brain_lock:
            self._brain_job=(np.asarray(audio,dtype=np.float32).copy(),int(sr),self._prompt)
        self._brain_event.set()

    def _brain_loop(self, generation: int):
        while generation == self._brain_generation and not self._brain_stop.is_set():
            self._brain_event.wait(timeout=.5)
            self._brain_event.clear()
            if generation != self._brain_generation or self._brain_stop.is_set(): break
            with self._brain_lock:
                job=self._brain_job; self._brain_job=None
            if job is None: continue
            audio,sr,prompt=job
            try:
                out=self._brain.analyze(audio,sr,prompt)
                if not out.get("ok"): raise RuntimeError(str(out.get("state") or out.get("error") or "neural observer unavailable"))
                emb=np.asarray(out.get("embedding") or [],dtype=np.float32)
                novelty=0.0
                if generation != self._brain_generation:
                    break
                with self._lock:
                    if emb.size and self._neural_memory:
                        prev=np.asarray(self._neural_memory[-1].get("embedding") or [],dtype=np.float32)
                        if prev.size==emb.size:
                            novelty=float(1.0-np.dot(emb,prev)/(np.linalg.norm(emb)*np.linalg.norm(prev)+1e-9))
                    item={"t":time.monotonic(),"embedding":out.get("embedding") or [],"actions":dict(out.get("actions") or {}),"states":dict(out.get("states") or {})}
                    self._neural_memory.append(item); self._neural_memory=self._neural_memory[-24:]
                    self._neural={
                        "ready":True,"state":"CLAP neural audio brain live","model":out.get("model"),"device":out.get("device"),
                        "window_sec":out.get("window_sec"),"inference_ms":out.get("inference_ms"),"prompt_similarity":out.get("prompt_similarity"),
                        "actions":dict(out.get("actions") or {}),"states":dict(out.get("states") or {}),"embedding":out.get("embedding") or [],
                        "novelty":round(novelty,4),
                    }
            except Exception as exc:
                with self._lock:
                    self._neural={"ready":False,"state":f"neural brain fallback · {type(exc).__name__}: {exc}","actions":{},"states":{}}

    def _recent_action_penalty(self, action: str, bars: int = 4) -> float:
        with self._lock:
            current=int(self._bar_index); hist=list(self._history[-12:])
        penalty=0.0
        for h in hist:
            if str(h.get("kind") or "").upper()==action and current-int(h.get("bar") or -999)<=bars:
                penalty=max(penalty,0.24)
        return penalty

    def _choose_neural_action(self, a: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            neural=dict(self._neural); p=self._policy
        acts=dict(neural.get("actions") or {}); states=dict(neural.get("states") or {})
        bass=float(a.get("bass_activity") or 0); drums=float(a.get("drums_activity") or 0); energy=float(a.get("energy") or 0)
        transient=float(a.get("transient_pressure") or 0)
        mixer=self.engine.status().get("agentic_mixer") or {}
        def A(name): return float(acts.get(name,0.0))
        def S(name): return float(states.get(name,0.0))
        with self._lock: mem=list(self._neural_memory)
        def M(name, n=6):
            vals=[float((q.get("states") or {}).get(name,0.0)) for q in mem[-n:]]
            return float(np.mean(vals)) if vals else S(name)
        def trend(name):
            if len(mem)<6: return 0.0
            recent=[float((q.get("states") or {}).get(name,0.0)) for q in mem[-3:]]
            older=[float((q.get("states") or {}).get(name,0.0)) for q in mem[-9:-3]]
            return (float(np.mean(recent))-float(np.mean(older))) if older else 0.0
        energy_trend=trend("high_energy")-trend("low_energy")
        structural_novelty=float(neural.get("novelty") or 0.0)
        candidates={}
        # Safety/critic priors are deliberately separate from the neural score.
        authority_hi=_clamp((float(getattr(p,"agentic_authority",1.0))-1.0)/2.60,0,1)
        candidates["NO_ACTION"]=A("NO_ACTION") + .10 + .08*max(0,M("stable_groove")) + .07*max(0,S("vocal_focus")) - .10*min(1.0,structural_novelty*4.0) - .10*max(0.0,p.action_density-.5) - .16*authority_hi
        candidates["LOW_END_PROTECT"]=A("LOW_END_PROTECT") + .34*max(0,(bass-.48)/.52) + .15*max(0,S("bass_heavy"))
        candidates["EQ_TIGHTEN"]=A("EQ_TIGHTEN") + .10*max(0,S("drum_dense"))
        candidates["FILTER_BUILD"]=A("FILTER_BUILD") + .16*max(0,S("buildup")) + .11*max(0,M("buildup")) + .13*max(0,S("phrase_transition")) + .08*max(0,structural_novelty*3.0) - .12*max(0,S("vocal_focus"))
        candidates["FILTER_RELEASE"]=A("FILTER_RELEASE") + (.38 if abs(float(self._controller.get("a_filter",0)))>.07 else -.25) + .10*max(0,S("drop"))
        candidates["ROLL_1"]=A("ROLL_1") + .18*drums + .14*max(0,S("instrumental")) + .10*max(0,S("phrase_transition")) - .22*max(0,S("vocal_focus"))
        candidates["ROLL_2"]=A("ROLL_2") + .12*drums + .16*max(0,S("phrase_transition")) - .18*max(0,S("vocal_focus"))
        candidates["ECHO_THROW"]=A("ECHO_THROW") + .20*max(0,S("phrase_transition")) - .12*max(0,S("bass_heavy"))
        candidates["REVERB_SPACE"]=A("REVERB_SPACE") + .16*max(0,S("breakdown")) + .12*max(0,S("instrumental")) - .18*max(0,S("bass_heavy"))
        # v31.4 Pro FX Director: prompt-conditioned neural affinities are combined
        # with the musical state and the compiled FX palette.  These actions are
        # still phrase/transient/vocal guarded below; a strong prompt cannot spam FX.
        fxp=float(p.fx_presence)
        candidates["FX_RISER"]=A("FX_RISER") + .24*p.fx_riser*fxp + .20*max(0,S("buildup")) + .16*max(0,S("phrase_transition")) - .18*max(0,S("vocal_focus"))
        candidates["FX_REVERSE_SWELL"]=A("FX_REVERSE_SWELL") + .22*p.fx_reverse*fxp + .17*max(0,S("breakdown")) + .14*max(0,S("phrase_transition")) - .10*max(0,S("drum_dense"))
        candidates["FX_SNARE_RUSH"]=A("FX_SNARE_RUSH") + .23*p.fx_snare_rush*fxp + .17*drums + .18*max(0,S("buildup")) - .28*max(0,S("vocal_focus"))
        candidates["FX_IMPACT"]=A("FX_IMPACT") + .25*p.fx_impact*fxp + .24*max(0,S("drop")) + .15*max(0,S("phrase_transition")) - .12*max(0,S("vocal_focus"))
        candidates["FX_ECHO_BLOOM"]=A("FX_ECHO_BLOOM") + .20*p.fx_echo*fxp + .17*max(0,S("phrase_transition")) + .10*max(0,S("breakdown")) - .16*max(0,S("bass_heavy"))
        candidates["FX_BEAT_ECHO"]=A("FX_BEAT_ECHO") + .24*p.fx_echo*fxp + .18*max(0,S("phrase_transition")) + .10*max(0,S("breakdown")) - .14*max(0,S("vocal_focus"))
        candidates["FX_TRANS_GATE"]=A("FX_TRANS_GATE") + .22*p.fx_phrase_accents*fxp + .14*drums + .16*max(0,S("buildup")) - .24*max(0,S("vocal_focus"))
        candidates["ENERGY_LIFT"]=A("ENERGY_LIFT") + (.22 if p.energy_direction=="rise" else 0) + .14*max(0,energy_trend) - .16*max(0,S("high_energy"))
        candidates["LOOP_RELEASE"]=A("LOOP_RELEASE") + (.70 if mixer.get("loop_ready") and float(self._controller.get("crossfader",0))>.05 else -.50)
        low_prompt=str(p.prompt or "").lower()
        jump_requested=any(k in low_prompt for k in ("beatjump","beat jump","skip","crop","cut forward","fast cut")) or p.creativity>.88
        back_requested=any(k in low_prompt for k in ("repeat section","replay","jump back","rewind")) and p.creativity>.68
        candidates["BEATJUMP_FWD"]=A("BEATJUMP_FWD") + (.24 if jump_requested else -.55) + .18*max(0,S("phrase_transition")) - .22*max(0,S("vocal_focus"))
        candidates["BEATJUMP_BACK"]=A("BEATJUMP_BACK") + (.24 if back_requested else -.65) + .16*max(0,S("phrase_transition")) - .18*max(0,S("vocal_focus"))
        # Creativity scales creative moves, never safety actions.
        for k in ("ROLL_1","ROLL_2","ECHO_THROW","REVERB_SPACE","FILTER_BUILD","FX_RISER","FX_REVERSE_SWELL","FX_SNARE_RUSH","FX_IMPACT","FX_ECHO_BLOOM","FX_BEAT_ECHO","FX_TRANS_GATE","BEATJUMP_FWD","BEATJUMP_BACK"):
            candidates[k] += (p.creativity-.5)*.28 + (p.action_density-.5)*.12 + .18*authority_hi
            candidates[k] -= (.08 if k=="FX_IMPACT" else .18)*max(0.0,(transient-.58)/.42)
        for k in list(candidates):
            if k!="NO_ACTION": candidates[k]-=self._recent_action_penalty(k,4)*(1.0-.30*authority_hi)
        ranked=sorted(candidates.items(),key=lambda q:q[1],reverse=True)
        action,score=ranked[0]
        # A professional DJ should often do nothing. Require a real margin over
        # NO_ACTION unless a safety action is clearly needed.
        no=candidates["NO_ACTION"]
        min_score=.16-.07*authority_hi
        margin=.025*(1.0-authority_hi)
        if action not in {"LOW_END_PROTECT","LOOP_RELEASE"} and (score<min_score or score<no+margin):
            action="NO_ACTION"; score=no
        rationale=f"neural={A(action):+.2f}; phrase={S('phrase_transition'):+.2f}, buildup={S('buildup'):+.2f}, vocal={S('vocal_focus'):+.2f}, transient={transient:.2f}, 60s-energy-trend={energy_trend:+.2f}, novelty={structural_novelty:.2f}; critic={score:+.2f}"
        return {"action":action,"score":round(float(score),3),"rationale":rationale,"top":[{"action":k,"score":round(float(v),3)} for k,v in ranked[:4]]}

    def _execute_neural_action(self, decision: dict[str, Any], a: dict[str, Any], bar: int, c: dict[str, Any]):
        action=str(decision.get("action") or "NO_ACTION")
        p=self._policy
        if action=="NO_ACTION":
            c["a_filter"]*=.72; c["echo"]*=.55; c["reverb"]*=.82; c["noise_riser"]*=.58; c["fx_reverse_swell"]*=.62; c["fx_snare_rush"]*=.58
        elif action=="LOW_END_PROTECT":
            c["a_low_db"]=_clamp(-.65-1.15*float(a.get("bass_activity") or 0),-1.9,-.45)
            c["bass_tighten"]=_clamp(max(float(c.get("bass_tighten",0)),.34),0,1)
        elif action=="EQ_TIGHTEN":
            c["a_mid_db"]=_clamp(c.get("a_mid_db",0)-.35,-1.0,1.0); c["a_high_db"]=_clamp(c.get("a_high_db",0)+.25,-1.0,1.0)
            c["perf_clarity"]=_clamp(max(float(c.get("perf_clarity",0)),.30),0,1); c["bass_tighten"]=_clamp(max(float(c.get("bass_tighten",0)),.22),0,1)
        elif action=="FILTER_BUILD":
            c["a_filter"]=_clamp(.11+.22*p.filter_motion,0,.30)
            c["noise_riser"]=_clamp((.18+.48*p.fx_motion)*p.fx_riser*p.fx_presence,0,.72); c["fx_reverse_swell"]=_clamp(.28*p.fx_reverse*p.fx_presence,0,.46); c["perf_energy"]=_clamp(max(float(c.get("perf_energy",0)),.28),0,1)
        elif action=="FILTER_RELEASE":
            c["a_filter"]=0.0; c["noise_riser"]=0.0; c["fx_reverse_swell"]=0.0; c["fx_snare_rush"]=0.0
            c["impact_strength"]=_clamp(p.fx_impact*p.fx_presence,0,1); self._impact_seq+=1; c["impact_seq"]=self._impact_seq
        elif action in {"ROLL_1","ROLL_2"}:
            self._loop_capture_seq+=1; c["loop_beats"]=1.0 if action=="ROLL_1" else 2.0; c["loop_capture_seq"]=self._loop_capture_seq
            c["crossfader"]=_clamp(.22+.30*p.creativity,0,.50); c["drum_drive"]=_clamp(max(float(c.get("drum_drive",0)),.32),0,1)
        elif action=="ECHO_THROW":
            c["echo"]=_clamp(max(float(c.get("echo",0)),(.16+.40*p.fx_echo)*p.fx_presence),0,.56)
        elif action=="REVERB_SPACE":
            c["reverb"]=_clamp(max(float(c.get("reverb",0)),(.12+.38*p.fx_space)*p.fx_presence),0,.46)
        elif action=="FX_RISER":
            c["noise_riser"]=_clamp((.46+.36*p.fx_riser)*p.fx_presence,0,.86); c["fx_reverse_swell"]=_clamp(.22*p.fx_reverse*p.fx_presence,0,.40); c["a_filter"]=_clamp(.08+.18*p.filter_motion,0,.28); c["perf_air"]=_clamp(max(float(c.get("perf_air",0)),.24*p.fx_presence),0,1)
            c["fx_rack_wet"]=_clamp(max(float(c.get("fx_rack_wet",0)),.28*p.fx_presence),0,.55); c["fx_width"]=_clamp(max(float(c.get("fx_width",0)),.46*p.fx_presence),0,.72); c["fx_duck"]=.55
        elif action=="FX_REVERSE_SWELL":
            c["fx_reverse_swell"]=_clamp((.52+.36*p.fx_reverse)*p.fx_presence,0,.90); c["reverb"]=_clamp(max(float(c.get("reverb",0)),.16*p.fx_space*p.fx_presence),0,.42)
        elif action=="FX_SNARE_RUSH":
            c["fx_snare_rush"]=_clamp((.50+.42*p.fx_snare_rush)*p.fx_presence,0,.92); c["drum_drive"]=_clamp(max(float(c.get("drum_drive",0)),.28+.18*p.fx_snare_rush),0,1); c["perf_punch"]=_clamp(max(float(c.get("perf_punch",0)),.34),0,1)
            c["fx_gate"]=_clamp(max(float(c.get("fx_gate",0)),.18+.20*p.fx_snare_rush),0,.46); c["fx_gate_div"]=4.0; c["fx_rack_wet"]=_clamp(max(float(c.get("fx_rack_wet",0)),.34),0,.58)
        elif action=="FX_IMPACT":
            c["noise_riser"]=0.0; c["fx_reverse_swell"]=0.0; c["fx_snare_rush"]=0.0; c["impact_strength"]=_clamp((.66+.34*p.fx_impact)*p.fx_presence,0,1); self._impact_seq+=1; c["impact_seq"]=self._impact_seq; c["perf_punch"]=_clamp(max(float(c.get("perf_punch",0)),.48+.20*p.fx_impact),0,1)
        elif action=="FX_ECHO_BLOOM":
            c["echo"]=_clamp(max(float(c.get("echo",0)),(.16+.22*p.fx_echo)*p.fx_presence),0,.42); c["reverb"]=_clamp(max(float(c.get("reverb",0)),(.14+.26*p.fx_space)*p.fx_presence),0,.44); c["fx_reverse_swell"]=_clamp(.20*p.fx_reverse*p.fx_presence,0,.32)
            c["fx_rack_wet"]=_clamp((.44+.28*p.fx_echo)*p.fx_presence,0,.72); c["fx_echo_send"]=_clamp((.38+.42*p.fx_echo)*p.fx_presence,0,.82); c["fx_echo_feedback"]=_clamp(.22+.28*p.fx_echo,0,.54); c["fx_echo_beats"]=.50; c["fx_width"]=_clamp(.42+.34*p.fx_space,0,.78); c["fx_duck"]=.72
        elif action=="FX_BEAT_ECHO":
            c["fx_rack_wet"]=_clamp((.50+.24*p.fx_echo)*p.fx_presence,0,.74); c["fx_echo_send"]=_clamp((.48+.38*p.fx_echo)*p.fx_presence,0,.86); c["fx_echo_feedback"]=_clamp(.28+.28*p.fx_echo,0,.58); c["fx_echo_beats"]=.50; c["fx_width"]=_clamp(.48+.30*p.fx_space,0,.82); c["fx_duck"]=.82
        elif action=="FX_TRANS_GATE":
            c["fx_rack_wet"]=_clamp((.38+.22*p.fx_phrase_accents)*p.fx_presence,0,.62); c["fx_gate"]=_clamp((.28+.28*p.fx_phrase_accents)*p.fx_presence,0,.52); c["fx_gate_div"]=4.0; c["drum_drive"]=_clamp(max(float(c.get("drum_drive",0)),.30),0,.62); c["fx_duck"]=.64
        elif action=="ENERGY_LIFT":
            c["a_high_db"]=_clamp(c.get("a_high_db",0)+.45,-1.2,1.5); c["a_filter"]=_clamp(.07+.10*p.filter_motion,0,.18)
            c["perf_punch"]=_clamp(max(float(c.get("perf_punch",0)),.30),0,1); c["perf_energy"]=_clamp(max(float(c.get("perf_energy",0)),.34),0,1)
        elif action=="LOOP_RELEASE":
            self._loop_release_seq+=1; c["loop_release_seq"]=self._loop_release_seq; c["crossfader"]=0.0
        elif action=="BEATJUMP_FWD":
            self.transport("beatjump",2.0)
        elif action=="BEATJUMP_BACK":
            self.transport("beatjump",-2.0)
        self._history.append({"bar":bar,"kind":action,"value":round(float(decision.get("score") or 0),3),"rationale":decision.get("rationale"),"time":time.time()})
        return c

    def _build_plan(self, a: dict[str, Any], now: float) -> list[dict[str, Any]]:
        d=self._choose_neural_action(a)
        with self._lock:
            self._decision=d
            self._refresh_performance_queue(a,now)
            queue=list(self._performance_queue)
        out=[]
        for q in queue[:6]:
            params=q.get("params") or {}; ps=", ".join(f"{k}={v:.2f}" if isinstance(v,(int,float)) else f"{k}={v}" for k,v in list(params.items())[:4])
            rat=str(q.get("rationale","") or "") + (f" · {ps}" if ps else "")
            out.append({"beat":int(q.get("beat",0)),"bar":int(q.get("beat",0))//4,"kind":q.get("action"),"deck":q.get("deck","CONCERT BUS"),"value":q.get("score",0),"rationale":rat})
        if not out:
            out=[{"beat":self._beat_index+4,"bar":(self._beat_index+4)//4,"kind":"LISTEN / REPLAN","deck":"BRAIN","value":round(float(self._policy.action_density),2),"rationale":"No justified boundary yet; rolling horizon continues instead of forcing a destructive action."}]
        return out

    def _segment_future_context(self, audio: np.ndarray, sr: int, global_analysis: dict[str, Any]) -> list[dict[str, Any]]:
        """Cheap bar-scale future descriptors for the rolling-horizon controller.

        v31.6 called the full BPM/key/STFT analyzer independently for every bar-ish
        chunk. That was musically useful but created periodic CPU bursts in the same
        process as Windows audio. v31.7 keeps the temporal intelligence while using
        one global analysis plus lightweight per-segment energy/band/onset descriptors.
        """
        if audio is None or len(audio)<sr*4:
            return []
        x=np.asarray(audio,dtype=np.float32)
        if x.ndim==2: x=np.mean(x,axis=1,dtype=np.float32)
        bpm=max(65.0,float(global_analysis.get("bpm") or 100.0)); beat_sec=60.0/bpm
        seg_sec=_clamp(4.0*beat_sec,1.35,3.10)
        max_segments=max(4,min(7,int(self._policy.temporal_segments)))
        total_sec=min(15.0,len(x)/float(sr)); out=[]; prev=None
        for i in range(max_segments):
            s0=i*seg_sec; s1=min(total_sec,s0+seg_sec)
            if s1-s0<1.0: break
            chunk=x[int(s0*sr):int(s1*sr)]
            if len(chunk)<512: continue
            # Energy + transient density from short RMS frames.
            step=max(128,int(sr*.020)); frame=max(step*2,int(sr*.040))
            usable=max(1,1+(len(chunk)-frame)//step)
            env=np.empty(usable,dtype=np.float32)
            for j in range(usable):
                z=chunk[j*step:j*step+frame]
                env[j]=float(np.sqrt(np.mean(z*z)+1e-12))
            energy=_clamp(float(np.sqrt(np.mean(chunk*chunk)+1e-12))/.24,0,1)
            den=max(1e-6,float(np.median(env)))
            onset=np.maximum(np.diff(env,prepend=env[:1]),0.0)
            drums=_clamp(float(np.percentile(onset,88))/(den*.42+1e-6),0,1)
            # Four small FFT probes are enough for structural band ratios; no key/BPM
            # estimation is repeated here.
            fft_n=2048 if sr>=16000 else 1024
            specs=[]
            if len(chunk)>=fft_n:
                centers=np.linspace(fft_n//2,len(chunk)-fft_n//2,4,dtype=np.int64)
                win=np.hanning(fft_n).astype(np.float32)
                for c0 in centers:
                    z=chunk[int(c0)-fft_n//2:int(c0)+fft_n//2]
                    if len(z)==fft_n: specs.append(np.abs(np.fft.rfft(z*win)).astype(np.float32))
            chroma_seg=np.ones(12,dtype=np.float32)/12.0
            tonal_strength=0.0
            if specs:
                spec=np.mean(np.stack(specs,axis=0),axis=0); freqs=np.fft.rfftfreq(fft_n,1.0/sr)
                lo=float(np.mean(spec[(freqs>=45)&(freqs<180)]))
                mid=float(np.mean(spec[(freqs>=180)&(freqs<3000)]))
                hi=float(np.mean(spec[(freqs>=3000)&(freqs<min(sr*.46,14000))]))
                total=max(1e-8,lo+mid+hi)
                bass=_clamp(2.7*lo/total,0,1); melody=_clamp(2.0*mid/total,0,1)
                vocal=_clamp((1.22*mid/total)*(1.0-.42*drums),0,1)
                # Beat-local chroma: the Deck-B selector uses this to compare a
                # captured phrase against the section that will be heard after it.
                # This is deliberately lightweight: reuse the four FFT probes
                # already computed above rather than running another STFT/model.
                chroma=np.zeros(12,dtype=np.float64)
                for fi in np.where((freqs>=70)&(freqs<=3200))[0]:
                    ff=float(freqs[fi])
                    if ff<=0: continue
                    midi=int(round(69+12*math.log2(ff/440.0)))
                    chroma[midi%12]+=float(spec[fi]**1.15)
                if float(np.sum(chroma))>1e-12:
                    chroma/=float(np.sum(chroma)); chroma_seg=chroma.astype(np.float32)
                    # Uniform chroma means noise/percussion; concentration means a
                    # usable melodic/harmonic identity.
                    entropy=-float(np.sum(chroma*np.log(chroma+1e-12)))/math.log(12.0)
                    tonal_strength=_clamp(1.0-entropy,0,1)
            else:
                bass=float(global_analysis.get('bass_activity') or 0); melody=float(global_analysis.get('melody_activity') or 0); vocal=float(global_analysis.get('vocal_activity') or 0)

            # 16th-grid accent signature over the bar-ish future segment.  Unlike
            # raw BPM matching, this tells the Deck-B selector where the actual
            # accents sit (kick/snare/hat pattern) so a captured motif can agree
            # with the section that follows.
            rhythm=np.zeros(16,dtype=np.float32)
            re=np.linspace(0,len(chunk),17,dtype=np.int64)
            for ri in range(16):
                zz=chunk[int(re[ri]):max(int(re[ri])+8,int(re[ri+1]))]
                if zz.size>16:
                    aw=max(8,min(int(sr*.032),max(8,zz.size//3)))
                    attack=float(np.sqrt(np.mean(zz[:aw]*zz[:aw])+1e-12))
                    body=float(np.sqrt(np.mean(zz[aw:]*zz[aw:])+1e-12)) if zz.size>aw+8 else attack
                    rhythm[ri]=max(0.0,attack-.68*body)+.16*attack
            rmax=float(np.max(rhythm))
            if rmax>1e-8: rhythm/=rmax
            vec=np.asarray([energy,drums,bass,vocal,melody],dtype=np.float32)
            novelty=0.0; direction=0.0
            if prev is not None:
                d=np.abs(vec-prev); novelty=float(.34*d[0]+.30*d[1]+.16*d[2]+.12*d[3]+.08*d[4])
                direction=float((vec[0]-prev[0])*.58+(vec[1]-prev[1])*.42)
            out.append({"index":i,"start_sec":round(s0,3),"end_sec":round(s1,3),"beat_offset":int(round(s0/beat_sec)),
                        "energy":round(float(energy),3),"drums":round(float(drums),3),"bass":round(float(bass),3),
                        "vocal":round(float(vocal),3),"melody":round(float(melody),3),"novelty":round(novelty,3),"direction":round(direction,3),
                        "tonal_strength":round(float(tonal_strength),3),
                        "chroma":[round(float(v),4) for v in chroma_seg.tolist()],
                        "rhythm":[round(float(v),3) for v in rhythm.tolist()]})
            prev=vec
        return out

    def _update_beat_clock(self, a: dict[str, Any], now: float):
        """Phase-lock the scheduler to the audible delayed PCM instead of wall-clock start.

        v31.17: a phase measurement belongs to the analysis time stamp, not to
        the tick that happens to read it.  Previously every 75 ms tick compared
        a measurement up to 0.85 s old against the prediction for *now* and
        pulled the anchor toward that stale phase, so the grid wobbled by up to
        a beat between analyses.  Each fresh measurement is now applied exactly
        once, at its own time stamp, with a confidence-weighted gain; tempo
        refinements keep the current phase continuous; three consecutive large
        disagreements force one clean re-lock instead of seconds of dragging.
        """
        bpm=max(65.0,float(a.get("bpm") or self._controller.get("bpm") or 100.0)); beat_sec=60.0/bpm
        to_next=float(a.get("to_next_beat_sec") or 0.0)
        t_meas=float(a.get("_t") or now)
        conf=_clamp(float(a.get("bpm_confidence") or 0.0),0,1)
        prev=float(self._beat_sec_prev or 0.0)
        if self._beat_anchor_t>0.0 and prev>0.0 and abs(prev-beat_sec)>1e-9:
            raw_old=(now-self._beat_anchor_t)/prev; self._beat_anchor_t=now-raw_old*beat_sec
        self._beat_sec_prev=beat_sec
        observed=((beat_sec-(to_next%beat_sec))%beat_sec)/beat_sec if beat_sec>0 else 0.0
        # A measurement is identified by its time stamp; analyses without one
        # (legacy callers, tests) count as new only when their content changes.
        meas_key=t_meas if "_t" in a else (round(bpm,4),round(to_next,5))
        fresh=False
        if self._beat_anchor_t<=0.0:
            self._beat_anchor_t=t_meas-observed*beat_sec; self._last_beat_meas_t=meas_key; self._beat_err_streak=0; fresh=True
        elif meas_key!=self._last_beat_meas_t:
            fresh=True
            self._last_beat_meas_t=meas_key
            raw=(t_meas-self._beat_anchor_t)/beat_sec; predicted=raw-math.floor(raw)
            err=(observed-predicted+.5)%1.0-.5
            if abs(err)>0.30 and conf>=0.35:
                # a large disagreement is either an outlier (ignore it) or a
                # relock (two in a row): never smear the grid across it
                self._beat_err_streak+=1
                if self._beat_err_streak>=2:
                    self._beat_anchor_t=t_meas-observed*beat_sec; self._beat_err_streak=0
            else:
                self._beat_err_streak=0
                self._beat_anchor_t-=err*beat_sec*(0.06+0.34*conf)
        if fresh:
            self._ingest_downbeat(a,t_meas,beat_sec,conf)
        raw=max(0.0,(now-self._beat_anchor_t)/beat_sec)
        self._beat_index=int(math.floor(raw)); self._beat_phase=raw-self._beat_index
        self._bar_index=(self._beat_index-int(self._bar_offset))//4
        self._bar_anchor_t=self._beat_anchor_t
        return self._beat_index,self._beat_phase

    def _drum_tick(self, c: dict[str, Any], a: dict[str, Any], beat: int, bar: int, bpm: float, now: float):
        if not getattr(self, "_dj_controller", False):
            c["drum_mode"] = 0.0; return                       # v31.30.25: AI drummer removed
        """v31.19: build / refresh the per-record kit, ask DrumMind for a 2-bar pattern every
        2 bars, and post pattern + amounts to the mixer (which mutes its synthesized
        re-drum/hats while the sample layer plays)."""
        if not self._audible_started or self._drum_brain is None or not getattr(self._drum_brain,"ok",False):
            c["drum_mode"]=0.0; return
        try:
            if (self._drum_kit is None or now-self._drum_kit_at>120.0) and self._future_cache is not None and len(self._future_cache)>=int(self._sr*6):
                kit=drum_kit.build_kit(np.asarray(self._future_cache[-int(self._sr*12):],dtype=np.float32),self._sr,bpm)
                self._drum_kit=kit; self._drum_kit_at=now
                self.engine.set_drum_kit(kit)
                self._drum_info["kit"]=str(kit.get("label","")); self._drum_info["kit_meta"]=dict(kit.get("meta",{}))
                if os.environ.get("JOY_DRUM_AI_POLISH","0")=="1" and not self._drum_polish_started:
                    self._drum_polish_started=True
                    threading.Thread(target=self._drum_polish,args=(kit,),daemon=True,name="joymetric-drum-polish").start()
        except Exception as exc:
            self._drum_info["kit_error"]=str(exc)[:120]
        src_k=list(a.get("src_kick") or []); src_s=list(a.get("src_snare") or []); src_h=list(a.get("src_hat") or [])
        if self._drum_kit is not None and len(src_k)==16 and (beat-self._drum_last_gen_beat>=8 or not self._drum_vel):
            try:
                energy=float(a.get("energy") or .5); cl=float(self._clarity_s) if self._clarity_s>=0 else 1.0
                cr=_clamp(float(self._creativity),0,1)
                # v31.20: creativity buys density and adventurousness (temperature), not just level
                dens=_clamp(0.22+0.45*energy*(0.55+0.45*cl)+0.20*cr,0.15,0.98)
                fill=1.0 if (bar%8) in (6,7) else 0.0
                style_id=style_id_from_text(str(getattr(self._policy,"style",""))+" "+str(self._prompt))
                hat_den=float(np.mean(src_h)) if len(src_h)==16 else 0.5
                res=self._drum_brain.generate(src_k,src_s,src_h,bpm,style_id=style_id,density=dens,fill=fill,
                                              temperature=0.85+0.30*cr,
                                              seed=int(bar//8)*11+int(self._drum_pat_seq%5),song_hat_density=hat_den,clarity=cl)
                if res is not None:
                    V,MT,info=res
                    # humanize amount by style: electronic grooves want tight 16ths, jazz/latin want the full human feel
                    _hum={1:0.35,11:0.5,9:0.5,0:0.6,7:0.5,4:0.8,5:0.7,2:1.0,3:1.0,6:1.0,8:0.9,10:0.6}.get(int(style_id),0.6)
                    MT=MT*np.float32(_hum); info["humanize"]=_hum
                    MT=np.asarray(MT,dtype=np.float32).reshape(32,4).copy()
                    sw=float(getattr(self,"_groove_swing",0.0) or 0.0)
                    if abs(sw)>0.02:
                        for _c in range(2,32,4):                     # the off-beat sixteenths of the hats follow the record's swing
                            MT[_c,2]=0.5*MT[_c,2]+0.5*sw; MT[_c,3]=0.5*MT[_c,3]+0.5*sw
                    self._drum_vel=[float(q) for q in V.reshape(-1)]; self._drum_mt=[float(q) for q in MT.reshape(-1)]
                    self._drum_pat_seq+=1; self._drum_last_gen_beat=beat
                    self._drum_info.update(info); self._drum_info["gen_bar"]=int(bar); self._drum_info["density_target"]=round(dens,2)
            except Exception as exc:
                self._drum_info["gen_error"]=str(exc)[:120]
        on=bool(self._drum_kit is not None and self._drum_vel)
        c["drum_mode"]=1.0 if on else 0.0
        if on:
            # v31.20: at creativity max the drummer steps forward (presence) and the A/B
            # loops hand their drums over to it (de-drum); at low creativity it stays a guest
            cr=_clamp(float(self._creativity),0,1); presence=0.35+0.65*cr
            c["drum_vel"]=self._drum_vel; c["drum_mt"]=self._drum_mt; c["drum_pat_seq"]=int(self._drum_pat_seq)
            kick_base=max(float(c.get("fx_redrum",0.0) or 0.0),0.45*presence)
            hat_base=max(float(c.get("doubletime",0.0) or 0.0),0.50*presence)
            c["drum_kick"]=_clamp(kick_base*(0.6+0.4*presence),0,1)
            c["drum_hat"]=_clamp(hat_base*(0.6+0.4*presence),0,1)
            c["drum_snare"]=_clamp(0.65*c["drum_kick"],0,1)
            c["drum_presence"]=presence
            c["b_dedrum"]=_clamp(0.70+0.30*cr,0,1)
            self._drum_info["presence"]=round(presence,2); self._drum_info["loops"]="drum-free"
        else:
            c["b_dedrum"]=0.0; c["drum_presence"]=0.0

    def _drum_polish(self, kit: dict):
        try:
            new=drum_kit.ai_polish(kit,str(getattr(self._policy,"style","")) or "electronic")
            if new is not None:
                with self._lock:
                    self._drum_kit=new; self._drum_info["kit"]=str(new.get("label",""))
                self.engine.set_drum_kit(new)
        except Exception:
            pass

    def _synth_tick(self, c: dict[str, Any], a: dict[str, Any], rt: dict[str, Any], beat: int, bar: int, bpm: float, now: float):
        """v31.21: after each loop capture ask the GPU worker for a synth variation of that loop;
        once it is back (and the same loop is still playing) crossfade Deck B toward it in
        sections that leave room for a melodic layer."""
        try:
            self._rack_tick(c,a,rt,beat,bar,bpm,now)
        except Exception as exc:
            self._rack_info["error"]=str(exc)[:120]
        if self._synth is not None and not self._synth.enabled and (time.time()-float(getattr(self._synth,"_last_health",0.0) or 0.0))>20.0 and self._audible_started:
            try:
                self._synth.health()                       # the worker may have finished a job / come up since
            except Exception:
                pass
        if self._synth is None or not self._synth.enabled or not self._audible_started:
            c["b_ai_mix"]=0.0; return
        mixer=rt.get("agentic_mixer") or {}
        seq=int(self._loop_capture_seq)
        try:
            self._future_synth_tick(c,a,rt,beat,bar,bpm,now)
        except Exception as exc:
            self._synth_info["future_error"]=str(exc)[:100]
        try:
            self._synth_playtime_gate(rt)
        except Exception as exc:
            self._synth_info["gate_error"]=str(exc)[:100]
        try:
            if (seq!=self._synth_last_seq and mixer.get("loop_ready") and float(mixer.get("loop_age_sec") or 99.0)<3.0
                    and float(mixer.get("loop_beats") or 0.0)>=1.0 and not self._synth.busy and self._synth_future_pending is None):
                snap=self.engine.agentic_loop_snapshot()
                if snap is not None:
                    loop,cseq,beats=snap
                    if int(cseq)==seq and len(loop)>=int(self._sr*0.45):
                        energy=float(a.get("energy") or .5); vocal=float(a.get("vocal_activity") or 0.0)
                        arc=str(getattr(self._mi.arc,"state","HOLD"))
                        if vocal>0.55 or arc=="FALL": kind="pad"
                        elif energy>0.72: kind=("arp","pluck")[self._synth_kind_i%2]
                        else: kind=("arp","pad","lead","pluck")[self._synth_kind_i%4]
                        self._synth_kind_i+=1
                        key=str(self._mi.harmony.stable_key or a.get("key") or "")
                        style=(str(getattr(self._policy,"style","")) + " " + str(self._prompt)[:60]).strip()
                        if self._synth.request(loop,self._sr,bpm,key,style,seq,kind,self._on_synth_done,song=self._synth_song_desc(a)):
                            self._synth_last_seq=seq
                            self._synth_info.update({"state":"generating","kind":kind,"seq":seq,"key":key,"beats":beats,"started":round(now,1)})
        except Exception as exc:
            self._synth_info["error"]=str(exc)[:100]
        ready=bool(self._synth_ready_seq==seq and mixer.get("ai_loop_ready"))
        if ready:
            cr=_clamp(float(self._creativity),0,1)
            section=str(self._remix.get("section") or "")
            room=(not self._remix.get("enabled")) or section in ("HALF","STRIP","BASSLINE","GROOVE+","STAB_GROOVE","ROLL","PERC")
            vocal=float(a.get("vocal_activity") or 0.0)
            target=(0.55+0.45*cr)*(0.6 if vocal>0.6 else 1.0) if room else 0.0
            if target>0.0:
                c["b_layer"]=_clamp(max(float(c.get("b_layer",0.0) or 0.0),0.24+0.16*cr),0,.46)
                c["b_texture"]=min(float(c.get("b_texture",0.0) or 0.0),-0.25)
            c["b_ai_mix"]=_clamp(target,0,1)
            self._synth_info["state"]="playing" if target>0 else "ready"
        else:
            c["b_ai_mix"]=0.0

    def _on_synth_done(self, seq: int, buf, meta: dict):
        try:
            if buf is not None and float(meta.get("tonal_src",0.0) or 0.0)>0.12 and float(meta.get("inkey",1.0) or 0.0)<0.60:
                with self._lock:
                    self._synth_info.update({"state":"rejected off-key","inkey":meta.get("inkey")})
                return
            ok=bool(buf is not None) and bool(self.engine.set_ai_loop(buf,int(seq)))
            with self._lock:
                self._synth_ready_seq=int(seq) if ok else -1
                self._synth_info.update({"state":"ready" if ok else ("failed" if buf is None else "stale"),
                                         "ms":meta.get("ms"),"rms":meta.get("rms"),"error":meta.get("error",""),"prompt":str(meta.get("prompt",""))[:90],"count":int(getattr(self._synth,"count",0))})
        except Exception:
            pass

    def _future_synth_tick(self, c: dict[str, Any], a: dict[str, Any], rt: dict[str, Any], beat: int, bar: int, bpm: float, now: float):
        """v31.22 FutureSynth: every 8 bars, cut the record's upcoming 1-2 bars out of the
        15 s lookahead (downbeat-aligned through the frame anchor), ask the GPU for a synth
        part on them and schedule the result at the exact render frame where those bars
        will play.  The audible programme is ~15 s behind the capture, generation takes
        6-10 s, so the part is normally in place a few seconds early; a late one joins in
        progress, still aligned."""
        if self._synth is None or not self._synth.enabled or self._synth.busy or self._synth_future_pending is not None:
            return
        if bar-self._synth_future_last_bar<8 or self._grid_anchor_frame<=0.0:
            return
        cl=float(self._clarity_s) if self._clarity_s>=0 else 1.0
        # beat-less material still takes a harmonic bed: pads only, softer (the bar cut is then
        # only as good as the uncertain grid, which a pad forgives)
        beatless=cl<0.25
        cap=int(rt.get("capture_frame_index") or 0); ren=int(rt.get("render_source_frame_index") or 0)
        if cap<=0 or ren<=0 or cap<=ren:
            return
        # capture->render offset: constant per session, but a status read can land between the
        # capture increment and the render increment (one block high) -> keep the minimum
        _d=cap-ren
        self._synth_D=_d if (self._synth_D<=0 or _d<self._synth_D) else self._synth_D
        D=int(self._synth_D)
        snap=getattr(self.engine,"agentic_audio_snapshot_indexed",None)
        if snap is None:
            return
        future,end_c=snap(15.0)
        if future is None or end_c<=0 or len(future)<int(self._sr*8):
            return
        sr=float(self._sr); beat_frames=60.0/max(60.0,bpm)*sr
        # lead follows the measured generation time (EMA, default 9 s); the clip length is
        # what still fits inside the 15 s lookahead after that lead
        need=_clamp(self._synth_ms_ema/1000.0*1.25+1.5,6.0,13.6)
        bars=2 if (need+8*beat_frames/sr+0.6)<=15.0 else 1
        length=int(round(bars*4*beat_frames))
        lead=int(min(15.0-length/sr-0.6,max(need,6.0))*sr)
        if lead<int(5.0*sr) or (length/sr+lead/sr+0.6)>15.0:
            return
        anchor=float(self._grid_anchor_frame)
        # first future downbeat (render frame) at least `lead` ahead of the render position
        k=math.ceil((ren+lead-anchor)/(4*beat_frames))
        start_render=anchor+k*4*beat_frames
        # capture and render counters index the SAME stream samples (render merely lags by D
        # in time), so a render frame maps straight into the future ring
        if lead+length>(cap-ren):
            return
        snap_start=end_c-len(future)
        i0=int(round(start_render))-snap_start
        if i0<0 or i0+length>len(future):
            return
        clip=np.asarray(future[i0:i0+length],dtype=np.float32)
        if float(np.sqrt(np.mean(clip**2)))<0.01:
            return
        energy=float(a.get("energy") or .5); vocal=float(a.get("vocal_activity") or 0.0); arc=str(getattr(self._mi.arc,"state","HOLD"))
        if beatless or vocal>0.55 or arc=="FALL": kind="pad"
        elif energy>0.72: kind=("arp","pluck")[self._synth_future_count%2]
        else: kind=("arp","pad","lead","pluck")[self._synth_future_count%4]
        key=str(self._mi.harmony.stable_key or a.get("key") or "")
        style=(str(getattr(self._policy,"style","")) + " " + str(self._prompt)[:60]).strip()
        cr=_clamp(float(self._creativity),0,1)
        gain=(0.28+0.27*cr)*(0.55 if vocal>0.6 else 1.0)*(0.7 if beatless else 1.0)
        seq=100000+self._synth_future_count
        self._synth_future_pending={"seq":seq,"render_start":int(round(start_render)),"gain":gain,"bar":bar,"kind":kind,"t0":now,"bars":bars}
        if self._synth.request(clip,self._sr,bpm,key,style,seq,kind,self._on_future_synth_done,song=self._synth_song_desc(a)):
            self._synth_future_last_bar=bar; self._synth_future_count+=1
            self._synth_info.update({"kind":kind,"key":key,"bars":bars,"lead_s":round(lead/sr,1),"target_render_frame":int(round(start_render))})
            if self._synth_future_pending is not None:      # still in flight (a synchronous worker may already have finished)
                self._synth_info["state"]="composing"
        else:
            self._synth_future_pending=None

    def _on_future_synth_done(self, seq: int, buf, meta: dict):
        try:
            pend=self._synth_future_pending
            self._synth_future_pending=None
            if buf is None or pend is None or int(pend.get("seq",-1))!=int(seq):
                with self._lock:
                    self._synth_info.update({"state":"failed","error":str(meta.get("error",""))[:80],"ms":meta.get("ms")})
                return
            # harmonic gate: a part that does not sit in the pitch classes of the record is not played
            inkey=float(meta.get("inkey",1.0) or 0.0); tonal=float(meta.get("tonal_src",0.0) or 0.0)
            if tonal>0.12 and inkey<0.60:
                with self._lock:
                    self._synth_info.update({"state":"rejected off-key","inkey":inkey,"harm":meta.get("harm_after"),"ms":meta.get("ms")})
                return
            ms=float(meta.get("ms") or 0.0)
            if ms>0: self._synth_ms_ema=0.6*self._synth_ms_ema+0.4*ms
            start=int(pend["render_start"]); shifted=0
            try:
                rt=self.engine.status(); ren=int(rt.get("render_source_frame_index") or 0)
                bpm_=max(60.0,float(rt.get("agentic_controls",{}).get("bpm",0) or self._controller.get("bpm",0) or 120.0))
                bar_frames=4*60.0/bpm_*float(self._sr)
                if ren>0 and start<ren+int(0.25*self._sr):
                    # the bars it was written for have started: land it on the next bar boundary
                    # (even bars for 2-bar clips keep the 2-bar phrase parity)
                    k=int(math.ceil((ren+0.25*self._sr-start)/bar_frames)); step=2 if int(pend.get("bars",2) or 2)==2 else 1
                    k=int(math.ceil(k/step))*step
                    start=int(round(start+k*bar_frames)); shifted=k
            except Exception:
                pass
            if shifted>2:
                # composed too long ago for the music that is playing now: do not play it
                with self._lock:
                    self._synth_info.update({"state":"dropped (late %d bars)" % shifted,"ms":meta.get("ms")})
                return
            try:
                c_clip,_=tonal_chroma(buf,self._sr)
            except Exception:
                c_clip=None
            ok=bool(self.engine.set_synth_clip(buf,start,float(pend["gain"]),int(seq)))
            if ok and c_clip is not None:
                self._synth_sched.append({"seq":int(seq),"start":int(start),"length":int(buf.shape[0]),"chroma":c_clip,"checked":False,"kind":pend.get("kind")})
                self._synth_sched=self._synth_sched[-4:]
            with self._lock:
                self._synth_info.update({"state":"scheduled" if ok else "dropped","ms":meta.get("ms"),"rms":meta.get("rms"),"shifted_bars":shifted,"ms_ema":int(self._synth_ms_ema),"inkey":meta.get("inkey"),"harm":meta.get("harm_after"),
                                         "prompt":str(meta.get("prompt",""))[:90],"count":int(self._synth_future_count),"kind":pend.get("kind"),"gain":round(float(pend["gain"]),2)})
        except Exception:
            pass

    # ------------------------------------------------------------------ v31.23 RackMind
    def _rack_tick(self, c: dict[str, Any], a: dict[str, Any], rt: dict[str, Any], beat: int, bar: int, bpm: float, now: float):
        """16 s lookahead as 8 blocks of 2 s.  A block is planned when the block after it is in the
        ring (the decision looks at the previous plan and at both neighbours); its layers are rendered by
        the DSP rack on the absolute beat grid and scheduled at absolute render frames, its takeover /
        freeze curve is frame-scheduled in the mixer, and its bias shapes the live drum / FX lanes while
        it plays."""
        if self._rack is None or self._planner is None or not self._audible_started:
            return
        cap=int(rt.get("capture_frame_index") or 0); ren=int(rt.get("render_source_frame_index") or 0)
        if cap<=0 or ren<=0:
            return
        sr=float(self._sr); B=int(self._planner.B)
        if self._dj_off():
            if not getattr(self,"_dj_off_prev",False):            # rising edge: drop what the planner already scheduled
                for _k in list(getattr(self._planner,"plans",{}) or {}):
                    try:
                        self.engine.cancel_takeover(500000+int(_k))
                    except Exception:
                        pass
                self._rack_queue=[]
            self._dj_off_prev=True; self._rack_info["dj_off"]=True
            try:
                self._weave_tick(cap,ren,sr)
            except Exception as exc:
                self._weave_info["error"]=str(exc)[:120]
            self._rack_info.update({"queue":0,"weave":(dict(self._weave_info, enabled=bool(self._weave.enabled), count=int(self._weave.count), skipped=int(self._weave.skipped), late=int(self._weave.late),
                                    failed=int(self._weave.failed), busy=bool(self._weave.busy), last=dict(self._weave.last), error=str(self._weave.error)[:80]) if self._weave is not None else None)})
            return
        if getattr(self,"_dj_off_prev",False):
            self._dj_off_prev=False; self._rack_info["dj_off"]=False
        if getattr(self,"_no_instruments",False) and self._rack_queue:
            self._rack_queue=[q for q in self._rack_queue if q[2].get("kind")=="freeze"]; self._rack_info["no_instruments"]=True
        k_last=cap//B-1
        if k_last>self._rack_last_k:
            snap=getattr(self.engine,"agentic_audio_snapshot_indexed",None)
            if snap is None:
                return
            future,end_c=snap(20.0)
            if future is None or end_c<=0:
                return
            try:
                with self._lock:
                    states=dict((self._neural or {}).get("states") or {})
                top=max(states.items(),key=lambda kv: float(kv[1]))[0] if states else ""
            except Exception:
                top=""
            ctx={"bpm":float(bpm),"beat_frames":60.0/max(40.0,float(bpm))*sr,"anchor_frame":float(self._grid_anchor_frame or 0.0),
                 "grid_conf":float(c.get("grid_conf",0.0) or 0.0),"creativity":_clamp(float(self._creativity),0,1),
                 "style":(str(getattr(self._policy,"style","")) + " " + str(self._prompt)[:120]).strip(),
                 "key":str(self._mi.harmony.stable_key or a.get("key") or ""),"vocal":float(a.get("vocal_activity") or 0.0),
                 "energy":float(a.get("energy") or 0.5),"drums":float(a.get("drums_activity") or 0.0),"state":top,
                 "track_change_frame":self._track_change_frame}
            res=self._planner.update(future,int(end_c),ren,ctx)
            self._rack_last_k=k_last
            snap_start=int(end_c)-len(future)
            # v31.23.2: the prompt shapes the rack (profile) and, with the record, picks its timbre (CLAP)
            pkey=(str(self._prompt)[:400],str(getattr(self._policy,"style","")))
            if pkey!=self._rack_profile_key:
                try:
                    self._rack_profile=prompt_profile(ctx["style"]); self._rack_profile_key=pkey
                except Exception:
                    self._rack_profile={}
            if self._genre_probe is not None and self._planner.desc:
                gkey=str(getattr(self,"_media_track_key","") or "")
                if self._genre_probe.needed(gkey,time.time()):
                    kk=max(self._planner.desc); a0=kk*B-snap_start
                    if 0<=a0 and a0+B<=len(future):
                        from synth_weave import style_words
                        g_prompt=PROMPT_GENRE.get(style_words(ctx["style"])[0])
                        self._genre_probe.start(gkey,np.asarray(future[a0:a0+B],dtype=np.float32).mean(axis=1),g_prompt)
            if self._rack_timbre is not None and self._planner.desc:
                tkey=(pkey[0],str(getattr(self,"_media_track_key","") or ""))
                if self._rack_timbre.needed(tkey,time.time()):
                    kk=max(self._planner.desc); dd=self._planner.desc[kk]; a0=kk*B-snap_start
                    if 0<=a0 and a0+B<=len(future):
                        self._rack_timbre.start(tkey,str(self._prompt),np.asarray(future[a0:a0+B],dtype=np.float32).mean(axis=1),dd["chords"],float(bpm),ctx["style"],ctx["key"],self._rack_profile)
            for plan in res.get("new_plans") or []:
                k=int(plan["k"])
                self._rack_blocks[k]={"k":k,"character":plan["character"],"chords":plan["chords"],"layers":[l["kind"] for l in plan["layers"]],
                                      "takeover":(plan["takeover"] or {}).get("mode") if plan.get("takeover") else None,"events":{kk:v for kk,v in (plan.get("events") or {}).items() if v}}
                tk=plan.get("takeover")
                if tk:
                    try:
                        self.engine.schedule_takeover(int(tk["start"]),int(tk["end"]),float(tk["amount"]),float(tk["ramp_in_s"]),float(tk["ramp_out_s"]),500000+k,str(tk.get("mode","takeover")))
                    except Exception as exc:
                        self._rack_info["takeover_error"]=str(exc)[:80]
                for i,l in enumerate(plan["layers"]):
                    self._rack_queue.append((k,i,dict(l)))
            for k in [k for k in self._rack_blocks if k<ren//B-2]:
                self._rack_blocks.pop(k,None)
            self._rack_now=res.get("now")
        else:
            self._rack_now=self._planner.plan_at(ren)
        # feed the rack (one render at a time, ~0.1-0.2 s per 2 s layer)
        if self._rack_queue:
            # v31.25.1: a generated layer waits until its whole 2-bar window is CAPTURED (the block starts
            # ~12 s ahead, the window may reach 6 s past it); layers behind it in the queue are not blocked.
            # A window that is still missing 4 s before the layer plays falls back to the rack.
            gen_ready=(self._composer is not None and self._composer.enabled)
            allow_fb=os.environ.get("JOY_GEN_FALLBACK","0")=="1"
            for qi in range(len(self._rack_queue)):
                k,i,l=self._rack_queue[qi]
                if l["kind"]=="gen" and self._weave is not None and self._weave.enabled:
                    wst=self._weave.state_of(int(l["start"]))
                    if wst is None and (self._weave_next_w<0 or int(l["start"])//int(self._weave.W)<self._weave_next_w):
                        wst="free"                          # a window the weave never took (before its first window)
                    if wst in ("processing","scheduled",None):
                        if wst=="scheduled" or int(l["start"])-ren<int(4.0*sr):
                            del self._rack_queue[qi]; self._gen_info["weave_skipped"]=int(self._gen_info.get("weave_skipped",0))+1; break
                        self._gen_info["weave_wait"]=int(self._gen_info.get("weave_wait",0))+1
                        continue
                if l["kind"]=="gen":
                    if not gen_ready:
                        if allow_fb:
                            l=dict(l); l["kind"]="pluck" if l.get("role")=="pluck" else "pad"; l["fallback"]=True; self._rack_queue[qi]=(k,i,l)
                        else:
                            del self._rack_queue[qi]; self._gen_info["dropped"]=int(self._gen_info.get("dropped",0))+1; break     # AI-ONLY: silence rather than a rack part
                    else:
                        if self._composer.busy:
                            continue
                        if not self._gen_window_ready(l,cap):
                            if int(l["start"])-ren<int(4.0*sr):
                                if allow_fb:
                                    l=dict(l); l["kind"]="pluck" if l.get("role")=="pluck" else "pad"; l["fallback"]=True; self._rack_queue[qi]=(k,i,l)
                                else:
                                    del self._rack_queue[qi]; self._gen_info["dropped"]=int(self._gen_info.get("dropped",0))+1; break
                            else:
                                continue
                        else:
                            del self._rack_queue[qi]
                            try:
                                self._gen_render(k,i,l)
                            except Exception as exc:
                                self._rack_info["render_error"]=str(exc)[:120]
                            break
                if l["kind"]!="gen":
                    if self._rack.busy:
                        continue
                    del self._rack_queue[qi]
                    try:
                        self._rack_render(k,i,l)
                    except Exception as exc:
                        self._rack_info["render_error"]=str(exc)[:120]
                    break
        try:
            self._weave_tick(cap,ren,sr)
        except Exception as exc:
            self._weave_info["error"]=str(exc)[:120]
        try:
            self._synth_playtime_gate(rt)
        except Exception as exc:
            self._rack_info["gate_error"]=str(exc)[:100]
        self._rack_bias(c,self._rack_now,ren,bpm)
        self._rack_info.update({"queue":len(self._rack_queue),"engine":"rack","count":int(self._rack.count),"ms":int(self._rack.last_ms),
                                "planned":len(self._planner.plans),"lookahead_blocks":int(max(0,(cap-ren)//B)),
                                "profile":{k:v for k,v in (self._rack_profile or {}).items() if k in ("axes","level_scale","bass_clean","fc_mult")},
                                "timbre":(self._rack_timbre.status() if self._rack_timbre is not None else None),"groove":dict(self._groove_info),
                                "gen":dict(self._gen_info, enabled=bool(self._composer and self._composer.enabled), last=(dict(self._composer.last) if self._composer else {})),
                                "genre":(self._genre_probe.status() if self._genre_probe is not None else None),
                                "weave":(dict(self._weave_info, enabled=bool(self._weave.enabled), count=int(self._weave.count), skipped=int(self._weave.skipped), late=int(self._weave.late),
                                               failed=int(self._weave.failed), busy=bool(self._weave.busy), last=dict(self._weave.last), error=str(self._weave.error)[:80],
                                               noise=float(self._weave.noise_now()), pending=len(getattr(self._weave,"hops",{}) or {}), rerenders=int(getattr(self._weave,"rerenders",0)),
                                               playing=self._weave.playing_noise(), queued=int(self._weave.queued()), errors=list(getattr(self._weave,"errors",[]))[-5:]) if self._weave is not None else None)})

    # v31.29: the DJ controller's overall effect can be scaled live (user: "reduce the controller's effect a bit"):
    # %LOCALAPPDATA%/JoyMetric/agentic.json {"controller_scale": 0.75} - amount-like keys (neutral 0) are scaled,
    # sequence / mode / length keys are left alone.  Read at most once a second.
    CONTROLLER_SCALED_KEYS = ("crossfader","a_filter","fx_width","fx_echo_send","fx_echo_feedback","fx_reverse_swell","fx_rack_wet",
                              "noise_riser","fx_gate","drum_drive","fx_duck","reverb","fx_snare_rush","perf_punch","impact_strength","b_fx",
                              "b_slicer_mix","b_layer","b_texture","b_transient","perf_energy","drum_presence","perf_clarity","perf_air",
                              "fx_pump","fx_stab","fx_redrum","fx_bass_synth","fx_hat_fill","echo")

    def _controller_scale(self) -> float:
        now = time.time()
        if now - float(getattr(self, "_ctl_scale_at", 0.0)) > 1.0:
            self._ctl_scale_at = now; sc = 1.0; off = False
            try:
                fp = os.path.join(os.environ.get("LOCALAPPDATA", ""), "JoyMetric", "agentic.json")
                if os.path.isfile(fp):
                    with open(fp, "r", encoding="utf-8") as f:
                        d = json.load(f) or {}
                    sc = float(d.get("controller_scale", 1.0)); off = bool(d.get("dj_off", False))
                    self._no_drums = bool(d.get("no_drums", False)); self._no_instruments = bool(d.get("no_instruments", False))
            except Exception:
                sc = 1.0; off = False; self._no_drums = False; self._no_instruments = False
            self._ctl_scale = _clamp(sc, 0.0, 1.5); self._dj_off_flag = off
        return float(getattr(self, "_ctl_scale", 1.0))

    def _dj_off(self) -> bool:
        """v31.29.2 (user: "turn the DJ controller off"): agentic.json {"dj_off": true} - the planner's freeze / takeover /
        generated layers and the rack stop, the controls are neutral (scale 0); the weave (protected vocal + transformed
        instrumental) keeps running.  Live, no relaunch."""
        self._controller_scale()
        if not getattr(self, "_dj_controller", False):
            return True                       # v31.30.25: controller removed -> weave-only path (VocalWeave + FX kept)
        return bool(getattr(self, "_dj_off_flag", False))

    def _on_battery(self) -> bool:
        """v31.30.27: True on battery (AC unplugged). Cached ~2 s; never raises."""
        now = time.time()
        if now - float(getattr(self, "_batt_at", 0.0)) > 2.0:
            self._batt_at = now; on = False
            try:
                if os.environ.get("JOY_FORCE_BATTERY", "0") == "1":
                    on = True
                else:
                    import ctypes
                    class _SPS(ctypes.Structure):
                        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte), ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte), ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
                    sps = _SPS(); ok = ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps))
                    on = bool(ok) and int(sps.ACLineStatus) == 0
            except Exception:
                on = False
            self._batt = on
        return bool(getattr(self, "_batt", False))

    def _mir_interval(self) -> float:
        """v31.30.27: seconds between MIR analyses.  Full rate for the controller; much slower when it is removed
        (the MIR then only feeds VocalWeave's key), slower still on battery - this is the battery-crackle fix."""
        if getattr(self, "_dj_controller", False):
            return 0.85
        base = float(os.environ.get("JOY_MIR_INTERVAL", "3.0") or 3.0)
        if self._on_battery():
            base *= float(os.environ.get("JOY_MIR_BATT_MULT", "2.0") or 2.0)
        return base

    DRUM_KEYS = ("drum_presence", "drum_drive", "fx_redrum", "fx_redrum_fill", "fx_hat_fill", "fx_snare_rush")
    INSTRUMENT_KEYS = ("fx_stab", "fx_bass_synth", "b_layer", "b_texture")

    def _send_controls(self, c, enabled=None):
        sc = self._controller_scale()
        if isinstance(c, dict) and (sc != 1.0 or getattr(self, "_no_drums", False) or getattr(self, "_no_instruments", False)):
            c = dict(c)
            if sc != 1.0:
                for k in self.CONTROLLER_SCALED_KEYS:
                    v = c.get(k)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        c[k] = float(v) * sc
            # v31.29.3 (user: "turn off the drum and instrument-adding part"): the AI drummer / re-drum / hat fills / snare
            # rushes, and the synth stabs / bass synth / deck-B layers are silenced; the rest of the controller stays
            if getattr(self, "_no_drums", False):
                for k in self.DRUM_KEYS:
                    if k in c and isinstance(c[k], (int, float)) and not isinstance(c[k], bool):
                        c[k] = 0.0
            if getattr(self, "_no_instruments", False):
                for k in self.INSTRUMENT_KEYS:
                    if k in c and isinstance(c[k], (int, float)) and not isinstance(c[k], bool):
                        c[k] = 0.0
        return self.engine.set_agentic_live_controls(c, enabled=enabled)

    def set_weave_noise(self, value: float) -> dict:
        """v31.30: the Stable Audio temperature slider (live)."""
        v = float(value)
        if self._weave is None:
            self._weave = VocalWeave(int(self._sr)); self._weave_info = {"windows": 0, "skipped_busy": 0}
        v = self._weave.set_noise(v)
        return {"noise": v, "pending": len(getattr(self._weave, "hops", {}) or {}), "queued": int(self._weave.queued()), "playing": self._weave.playing_noise()}

    def set_weave_cfg(self, value: float) -> dict:
        """v31.30.11: the effect-strength (prompt guidance) slider (live)."""
        if self._weave is None:
            self._weave = VocalWeave(int(self._sr)); self._weave_info = {"windows": 0, "skipped_busy": 0}
        v = self._weave.set_cfg(float(value))
        return {"cfg": v, "pending": len(getattr(self._weave, "hops", {}) or {}), "queued": int(self._weave.queued()), "playing": self._weave.playing_noise()}

    def weave_settings(self) -> dict:
        wv = self._weave
        if wv is None:
            return {"noise": float(VocalWeave.overrides().get("noise") or 0.52), "cfg": float(VocalWeave.overrides().get("cfg") or 4.0), "pending": 0, "playing": None}
        last = wv.last or {}
        return {"noise": wv.noise_now(), "cfg": wv.cfg_now(), "pending": len(getattr(wv, "hops", {}) or {}), "rerenders": int(getattr(wv, "rerenders", 0)), "playing": wv.playing_noise(), "queued": wv.queued(), "busy": bool(wv.busy)}

    def _weave_tick(self, cap: int, ren: int, sr: float):
        """v31.28 VocalWeave: window w = frames [w*W, (w+1)*W) of the record.  The moment the window is fully
        CAPTURED it is cut from the lookahead ring and handed to the pipeline (separate -> Stable Audio ->
        schedule); the render lag (16 s window + processing budget) means it comes back before it plays.
        One window at a time; a window that could not be started before the next one is complete is skipped."""
        if os.environ.get("JOY_VOCAL_WEAVE","1")!="1":
            return
        if self._weave is None:
            self._weave=VocalWeave(int(sr)); self._weave_info={"windows":0,"skipped_busy":0}
        wv=self._weave; W=int(wv.W); pad=int(getattr(wv,"pad",0))
        wv.last_ren=int(ren)
        if not wv.busy and getattr(wv,"rerender",False):
            wv.rerender_tick(self.engine,int(ren))          # v31.30: the temperature slider moved - re-render what is not playing yet
        if self._weave_next_w<0:
            # v31.30.12: start on what the lookahead ring already holds - the first window that starts at least
            # START_LEAD_S ahead of the render position (a 16 s window takes ~4-8 s to weave).  The old rule waited
            # for the NEXT window to be captured and that window played 48 s later: ~96 s of untouched record after
            # every LET'S GO (heard as "Stable is not coming").
            START_LEAD_S={"lowlat":3.0,"ultralow":1.5,"mid":5.0}.get(os.environ.get("JOY_WEAVE_PROFILE","").strip().lower(),8.0)
            self._weave_next_w=max(0,int((int(ren)+int(START_LEAD_S*sr))//W)+1)
        w=self._weave_next_w
        if cap<(w+1)*W+pad:
            return                                  # the window and its 0.5 s of trailing context must be captured
        if wv.busy:
            if cap>=(w+2)*W:                          # the pipeline is slower than real time: drop this window
                wv.mark(w,"skipped"); self._weave_next_w=w+1; self._weave_info["skipped_busy"]=int(self._weave_info.get("skipped_busy",0))+1
            return
        if not wv.enabled:
            if time.time()-wv.last_health>5.0:
                wv.health_async()
            wv.mark(w,"skipped"); self._weave_next_w=w+1
            return
        snap=getattr(self.engine,"agentic_audio_snapshot_indexed",None)
        if snap is None:
            return
        need_s=min(58.0,max(float(W+2*pad)/sr+1.0,(float(cap)-float(w*W-pad))/sr+1.0))   # reach back to the window (the ring holds 60 s)
        future,end_c=snap(need_s)
        if future is None or end_c<=0:
            return
        base=int(end_c)-len(future)
        s0=int(w*W)-base
        if s0<0:
            wv.mark(w,"skipped"); self._weave_next_w=w+1; return         # already gone from the ring
        pad_in=min(pad,s0); e1=s0+W+pad
        if e1>len(future):
            return                                  # not fully in the snapshot yet
        pad_out=pad
        style=(str(getattr(self._policy,"style","")) + " " + str(self._prompt)[:120]).strip()
        key=str(self._mi.harmony.stable_key or "")
        ok=wv.request(w,future[s0-pad_in:e1],str(self._prompt),style,key,_clamp(float(self._creativity),0,1),self.engine,int(ren),self._rack_profile,pad_in=pad_in,pad_out=pad_out)
        if ok:
            self._weave_info["windows"]=int(self._weave_info.get("windows",0))+1; self._weave_info["last_w"]=int(w)
            self._weave_info["lead_s"]=round((w*W-ren)/sr,1)
        else:
            wv.mark(w,"skipped")
        self._weave_next_w=w+1

    def _rack_render(self, k: int, i: int, l: dict):
        """turn a planned layer into a rack spec (audio context cut from the ring) and render it."""
        sr=float(self._sr); start=int(l["start"]); n=int(l["n"])
        if n<=0:
            return
        snap=getattr(self.engine,"agentic_audio_snapshot_indexed",None)
        future,end_c=snap(20.0) if snap is not None else (None,0)
        if future is None or end_c<=0:
            return
        snap_start=int(end_c)-len(future)
        seq=200000+k*8+i
        bf=60.0/max(40.0,float(l["bpm"]))*sr
        if l["kind"]=="freeze":
            s0=int(l["src_start"])-snap_start; s1=s0+int(l["src_n"])
            if s0<0 or s1>len(future):
                self._rack_info["state"]="freeze source outside ring"; return
            src=np.asarray(future[s0:s1],dtype=np.float32)
            spec={"kind":"freeze","bpm":float(l["bpm"]),"beats":float(l["beats"]),"src":src,"level_rms":float(rms_hp(src,int(sr),40.0)),"seed":int(l.get("seed",0))}
        else:
            shift=start-int(l["block_start"]) if "block_start" in l else 0
            chords=[]
            for ch in l["chords"]:
                ca=int(ch["a"])-shift; cb=int(ch["b"])-shift
                ca=max(0,ca); cb=min(n,cb)
                if cb>ca:
                    chords.append({"a":ca,"b":cb,"root":ch["root"],"quality":ch["quality"],"label":ch["label"]})
            if not chords:
                chords=[{"a":0,"b":n,"root":l["chords"][0]["root"],"quality":l["chords"][0]["quality"],"label":l["chords"][0]["label"]}]
            c0=start-snap_start
            src_clip=np.asarray(future[max(0,c0):max(0,c0)+n],dtype=np.float32) if 0<=c0<len(future) else None
            spec={"kind":l["kind"],"bpm":float(l["bpm"]),"n":n,"b0":float(l["b0"])+shift/bf if l.get("grid") else 0.0,"chords":chords,
                  "style":l.get("style",""),"seed":int(l.get("seed",0)),"level_rms":float(l["level"])*float(l["ref_rms"]),
                  "energy":float(l.get("energy",0.5)),"vocal":float(l.get("vocal",0.0)),"drums":float(l.get("drums",1.0)),
                  "continuation":bool(l.get("continuation")),"build":float(l.get("build",0.0)),"swell":bool(l.get("swell")),
                  "pump":float(l.get("pump",1.0)),"scale":key_pcs(str(l.get("key") or "")),"tail_s":0.8 if l["kind"] in ("pad","lead") else 0.5,
                  "src_clip":src_clip,"profile":dict(self._rack_profile or {}),
                  "variant":(self._rack_timbre.pick(l["kind"],k//8,_clamp(float(self._creativity),0,1)) if self._rack_timbre is not None else None)}
            spec["level_rms"]*=float((self._rack_profile or {}).get("level_scale",1.0))
            if l["kind"] in ("pluck","arp") and self._groove_live is not None and self._groove_live.ready and l.get("grid"):
                try:
                    self._groove_plan(spec,l,future,snap_start,bf)
                except Exception as exc:
                    self._groove_info["error"]=str(exc)[:100]
            if (self._rack_profile or {}).get("vocal_care") and float(l.get("vocal",0.0))>0.4:
                spec["level_rms"]*=0.7
        self._rack_pending[seq]={"k":k,"start":start,"n":n,"kind":l["kind"],"level":float(l.get("level",1.0))}
        if not self._rack.request(spec,seq,self._on_rack_done):
            self._rack_queue.appendleft((k,i,l)); self._rack_pending.pop(seq,None)

    def _on_rack_done(self, seq: int, buf, meta: dict):
        pend=self._rack_pending.pop(int(seq),None)
        if pend is None:
            return
        try:
            if buf is None:
                with self._lock:
                    self._rack_info.update({"state":"render failed","error":meta.get("error","")})
                return
            if pend["kind"]!="freeze" and float(meta.get("tonal_src",0.0) or 0.0)>0.12 and float(meta.get("inkey",1.0) or 0.0)<0.55:
                with self._lock:
                    self._rack_info.update({"state":"rejected off-key","inkey":meta.get("inkey"),"kind":pend["kind"]})
                return
            ok=bool(self.engine.set_synth_clip(buf,int(pend["start"]),1.0,int(seq)))
            hist=meta.get("note_hist")
            if ok and hist is not None and pend["kind"]!="freeze":
                self._synth_sched.append({"seq":int(seq),"start":int(pend["start"]),"length":int(buf.shape[0]),"chroma":np.asarray(hist,dtype=np.float64),"checked":False,"kind":pend["kind"],"rack":True})
                self._synth_sched=self._synth_sched[-12:]
            with self._lock:
                self._rack_info.update({"state":"scheduled" if ok else "engine refused","kind":pend["kind"],"block":pend["k"],"chords":meta.get("chords",""),"variant":meta.get("variant"),
                                        "ms":meta.get("ms"),"rms":meta.get("rms"),"inkey":meta.get("inkey"),"harm":meta.get("harm"),"level":round(pend["level"],2),
                                        "prompt":"rack %s · %s" % (pend["kind"],meta.get("chords",""))})
        except Exception as exc:
            with self._lock:
                self._rack_info.update({"state":"schedule error","error":str(exc)[:100]})

    def _rack_bias(self, c: dict[str, Any], plan, ren: int, bpm: float):
        """the block that is playing now shapes the live lanes (never a step: the lanes are smoothed in the mixer)."""
        # v31.23.1: the AI drummer stays lowkey - a global trim on its presence and amounts, then the block bias
        try:
            trim=float(os.environ.get("JOY_DRUM_LOWKEY","0.62") or 0.62)
            if "drum_presence" in c:
                c["drum_presence"]=_clamp(float(c.get("drum_presence",0.5) or 0.0)*trim,0.0,1.0)
            for kk in ("drum_kick","drum_hat","drum_snare"):
                if kk in c:
                    c[kk]=_clamp(float(c.get(kk,0.0) or 0.0)*0.8,0.0,1.0)
        except Exception:
            pass
        if not plan:
            return
        bias=plan.get("bias") or {}
        try:
            dp=float(bias.get("drum_presence",1.0))
            if abs(dp-1.0)>1e-3 and "drum_presence" in c:
                c["drum_presence"]=_clamp(float(c.get("drum_presence",0.5) or 0.0)*dp,0.0,1.0)
            pump=float(bias.get("pump",0.0))
            if pump>0:
                c["fx_pump"]=max(float(c.get("fx_pump",0.0) or 0.0),pump)
            span=max(1,int(plan["end"])-int(plan["start"])); prog=_clamp((ren-int(plan["start"]))/span,0.0,1.0)
            riser=float(bias.get("riser",0.0))
            if riser>0:
                c["noise_riser"]=max(float(c.get("noise_riser",0.0) or 0.0),riser*(0.3+0.7*prog))
            rush=float(bias.get("rush",0.0))
            if rush>0 and ren>int(plan["end"])-int(60.0/max(40.0,bpm)*self._sr):
                c["fx_snare_rush"]=max(float(c.get("fx_snare_rush",0.0) or 0.0),rush)
        except Exception:
            pass

    def _groove_plan(self, spec: dict, l: dict, future, snap_start: int, bf: float):
        """v31.24 GrooveNet: the 2-bar window that starts at the last downbeat at or before the layer
        decides where the stabs go (probability per sixteenth), with what micro-timing (swing),
        at what level and through what tilt."""
        anchor=float(self._grid_anchor_frame or 0.0)
        if anchor<=0 or bf<=0:
            return
        start=int(l["start"]); n=int(l["n"])
        m0=int(math.floor((start-anchor)/(4.0*bf)))*4                       # beat index of the downbeat at / before the layer
        beats=[anchor+(m0+i)*bf for i in range(9)]
        res=self._groove_live.window(future,snap_start,beats,float(l["bpm"]))
        if res is None:
            return
        cr=_clamp(float(self._creativity),0,1)
        density=0.10+0.15*cr
        steps=GrooveNet.pattern(res["on"],density)
        hits=[]
        for c in steps:
            beat_abs=(m0+c/4.0+float(res["mt"][c])/4.0)
            vel=0.6+0.4*float(res["on"][c])
            hits.append((beat_abs,vel))
        # the block only takes the hits inside it; the rest of the 2-bar plan lands with the next block's layer
        b0=float(spec["b0"]); block_beats=n/bf
        spec["pattern_steps"]=[(bt,v) for bt,v in hits if b0-0.02<=bt<b0+block_beats]
        spec["swing"]=float(res["swing"]); spec["tilt_db"]=[float(v) for v in res["tilt_db"]]; spec["level_mult"]=float(res["level_mult"])
        self._groove_swing=0.7*self._groove_swing+0.3*float(res["swing"])
        self._groove_info.update({"ready":True,"swing":round(self._groove_swing,3),"level_mult":round(float(res["level_mult"]),2),
                                  "tilt_db":[round(float(v),1) for v in res["tilt_db"]],"steps":steps,"hits_in_block":len(spec["pattern_steps"]),
                                  "ms":int(self._groove_live.last_ms),"count":int(self._groove_live.count)})

    def _gen_window_ready(self, l: dict, cap: int) -> bool:
        """the 2-bar window of a generated layer (from the downbeat at / before its start) is fully captured."""
        anchor=float(self._grid_anchor_frame or 0.0); bf=60.0/max(40.0,float(l["bpm"]))*float(self._sr)
        if anchor<=0 or bf<=0:
            return True                                   # no grid: the render falls back to the rack anyway
        m0=int(math.floor((int(l["start"])-anchor)/(4.0*bf)))*4
        return (anchor+(m0+8)*bf)<=float(cap)

    def _gen_render(self, k: int, i: int, l: dict):
        """v31.25: a 'gen' layer -> the generative composer (melody + chords of the 2-bar window as
        controls, GrooveNet placement / micro-timing / level / tilt, the CLAP-selected instrument)."""
        sr=float(self._sr); start=int(l["start"]); n=int(l["n"])
        snap=getattr(self.engine,"agentic_audio_snapshot_indexed",None)
        future,end_c=snap(20.0) if snap is not None else (None,0)
        if future is None or end_c<=0:
            return
        snap_start=int(end_c)-len(future)
        bf=60.0/max(40.0,float(l["bpm"]))*sr
        anchor=float(self._grid_anchor_frame or 0.0)
        if anchor<=0 or bf<=0:
            if os.environ.get("JOY_GEN_FALLBACK","0")=="1":
                l=dict(l); l["kind"]="pluck" if l.get("role")=="pluck" else "pad"; self._rack_render(k,i,l)
            return
        m0=int(math.floor((start-anchor)/(4.0*bf)))*4
        beats=[anchor+(m0+j)*bf for j in range(9)]
        w0=int(round(beats[0]))-snap_start; w1=int(round(beats[8]))-snap_start
        if w0<0 or w1>len(future):
            if os.environ.get("JOY_GEN_FALLBACK","0")=="1":
                l=dict(l); l["kind"]="pluck" if l.get("role")=="pluck" else "pad"; self._rack_render(k,i,l)
            return
        window=np.asarray(future[w0:w1],dtype=np.float32)
        # v31.27: the previous 2 bars as well (history is in the ring) -> the 4-bar full-resolution context
        w4=int(round(beats[0]-8*bf))-snap_start
        window4=np.asarray(future[w4:w1],dtype=np.float32) if w4>=0 else None
        # chords of the window: the planner's per-block chords (block-relative frames) -> window beats
        chords_beats=[]
        B=int(self._planner.B)
        for kk in range(int(round(beats[0]))//B,int(round(beats[8]))//B+1):
            d=self._planner.desc.get(kk)
            if not d:
                continue
            for ch in d["chords"]:
                fa=kk*B+int(ch["a"]); fb=kk*B+int(ch["b"])
                ba=(fa-beats[0])/bf; bb=(fb-beats[0])/bf
                ba=max(0.0,ba); bb=min(8.0,bb)
                if bb-ba>0.05:
                    if chords_beats and chords_beats[-1][2]==ch["root"] and chords_beats[-1][3]==ch["quality"] and abs(chords_beats[-1][1]-ba)<0.06:
                        chords_beats[-1]=(chords_beats[-1][0],bb,ch["root"],ch["quality"])
                    else:
                        chords_beats.append((ba,bb,int(ch["root"]),str(ch["quality"])))
        g=None
        if self._groove_live is not None and self._groove_live.ready:
            try:
                g=self._groove_live.window(future,snap_start,beats,float(l["bpm"]))
            except Exception:
                g=None
        c0=start-snap_start
        src_clip=np.asarray(future[max(0,c0):max(0,c0)+n],dtype=np.float32) if 0<=c0<len(future) else None
        spec={"kind":"gen","role":str(l.get("role","pad")),"bpm":float(l["bpm"]),"n":n,"b0":float(l["b0"])+(start-int(l["block_start"]))/bf if l.get("grid") else 0.0,
              "window_beat0":float((beats[0]-anchor)/bf),"chords":l["chords"],"chords_beats":chords_beats,"style":l.get("style",""),"seed":int(l.get("seed",0)),
              "level_rms":float(l["level"])*float(l["ref_rms"])*float((self._rack_profile or {}).get("level_scale",1.0)),
              "energy":float(l.get("energy",0.5)),"vocal":float(l.get("vocal",0.0)),"drums":float(l.get("drums",1.0)),"creativity":_clamp(float(self._creativity),0,1),
              "continuation":bool(l.get("continuation")),"build":float(l.get("build",0.0)),"swell":bool(l.get("swell")),"pump":float(l.get("pump",1.0)),
              "scale":key_pcs(str(l.get("key") or "")),"key":str(l.get("key") or ""),"tail_s":0.8,"src_clip":src_clip,"src_window":window,"src_window4":window4,
              "profile":dict(self._rack_profile or {}),"variant":(self._rack_timbre.pick(str(l.get("role","pad")),k//8,_clamp(float(self._creativity),0,1)) if self._rack_timbre is not None else None)}
        if g is not None:
            spec.update({"on_prob":g["on"],"mt":g["mt"],"tilt_db":[float(v) for v in g["tilt_db"]],"level_mult":float(g["level_mult"]),"swing":float(g["swing"]),
                         "src_kick":g["src_kick"],"src_snare":g["src_snare"],"src_hat":g["src_hat"]})
            self._groove_swing=0.7*self._groove_swing+0.3*float(g["swing"])
        spec["genre_id"]=int(self._genre_probe.genre_id) if self._genre_probe is not None else 13
        spec["replace_line"]=bool(l.get("replace_line"))
        spec["beat_frames"]=float(bf); spec["anchor"]=float(anchor)
        seq=200000+k*8+i
        self._rack_pending[seq]={"k":k,"start":start,"n":n,"kind":"gen","level":float(l.get("level",1.0))}
        if not self._composer.request(spec,seq,self._on_gen_done):
            self._rack_pending.pop(seq,None)
            if os.environ.get("JOY_GEN_FALLBACK","0")=="1":
                l=dict(l); l["kind"]="pluck" if l.get("role")=="pluck" else "pad"; self._rack_render(k,i,l)

    def _on_gen_done(self, seq: int, buf, meta: dict):
        with self._lock:
            self._gen_info.update({"count":int(self._gen_info.get("count",0))+1,"state":("rest" if meta.get("empty") else ("composed" if meta.get("ok") else "failed")),"ms":meta.get("ms"),"gen_ms":meta.get("gen_ms"),
                                   "notes":meta.get("notes"),"in_block":meta.get("in_block"),"melody_notes":meta.get("melody_notes"),"candidates":meta.get("candidates"),
                                   "inkey":meta.get("inkey"),"harm":meta.get("harm"),"variant":meta.get("variant"),"role":meta.get("role"),"error":meta.get("error",""),
                                   "source":meta.get("source"),"lines":meta.get("lines"),"scores":meta.get("scores"),"genre_id":meta.get("genre_id"),
                                   "stats":meta.get("stats"),"prefixed":meta.get("prefixed"),"songmind_ms":meta.get("songmind_ms")})
        if buf is None:
            pend=self._rack_pending.get(int(seq))
            if pend is not None:
                self._rack_pending.pop(int(seq),None)
            return
        rep=meta.get("replace") or []
        if rep:
            try:
                anchor=float(self._grid_anchor_frame or 0.0); bf=60.0/max(40.0,float(meta.get("bpm") or 120.0))*float(self._sr)
                segs=[(int(round(anchor+bt*bf)),int(round(anchor+(bt+d)*bf)),int(m),0.85) for (bt,d,m) in rep]
                self.engine.schedule_line_duck(segs,int(seq))
                with self._lock:
                    self._gen_info["line_duck"]=len(segs)
            except Exception as exc:
                self._gen_info["line_duck_error"]=str(exc)[:80]
        self._on_rack_done(seq,buf,meta)

    def _synth_song_desc(self, a: dict[str, Any]) -> dict[str, Any]:
        """v31.22.3: what the composer is told about the song (energy, vocal, drums, CLAP musical state)."""
        try:
            with self._lock:
                states=dict((self._neural or {}).get("states") or {})
            top=max(states.items(), key=lambda kv: float(kv[1]))[0] if states else ""
        except Exception:
            top=""
        return {"energy":float(a.get("energy") or 0.5),"vocal":float(a.get("vocal_activity") or 0.0),
                "drums":float(a.get("drums_activity") or 0.0),"state":str(top)}

    def _synth_playtime_gate(self, rt: dict[str, Any]):
        """v31.22.2: just before a scheduled clip starts, compare it with the bars that are
        ACTUALLY about to play (they are in the lookahead ring by now).  A record change,
        a chord move or a late shift that no longer fits is caught here and the clip is
        cancelled; nothing unrelated to the music reaches the audience."""
        if not self._synth_sched:
            return
        ren=int(rt.get("render_source_frame_index") or 0)
        if ren<=0:
            return
        keep=[]
        for item in self._synth_sched:
            start=int(item["start"]); length=int(item["length"])
            if start+length<ren:
                continue                                        # finished
            if item.get("checked") or start-ren>int(2.5*self._sr):
                keep.append(item); continue                     # not yet within 2.5 s of its start
            snap=getattr(self.engine,"agentic_audio_snapshot_indexed",None)
            if snap is None:
                keep.append(item); continue
            future,end_c=snap(15.0)
            if future is None or end_c<=0:
                keep.append(item); continue
            snap_start=end_c-len(future); i0=start-snap_start
            if i0<0 or i0+length>len(future):
                keep.append(item); continue
            seg=np.asarray(future[i0:i0+length],dtype=np.float32)
            item["checked"]=True
            try:
                c_now,tonal_now=tonal_chroma(seg,self._sr)
                agree=chroma_agreement(item["chroma"],c_now)
                inkey_now=float(np.asarray(item["chroma"])[allowed_pitch_classes(c_now)].sum())
            except Exception:
                keep.append(item); continue
            if item.get("rack"):
                bad=(tonal_now>0.10) and (inkey_now<0.55)
            else:
                bad=(tonal_now>0.10) and (inkey_now<0.60 or agree<0.55)
            if bad:
                try:
                    self.engine.cancel_synth_clip(int(item["seq"]))
                except Exception:
                    pass
                with self._lock:
                    self._synth_info.update({"state":"cancelled at play time (music changed)","gate_inkey":round(inkey_now,3),"gate_agree":round(agree,3)})
            else:
                with self._lock:
                    self._synth_info.update({"gate_inkey":round(inkey_now,3),"gate_agree":round(agree,3),"state":"playing"})
                keep.append(item)
        self._synth_sched=keep

    def _drum_polish_placeholder(self):
        return None

    def _ingest_downbeat(self, a: dict[str, Any], t_meas: float, beat_sec: float, conf: float):
        """v31.18: vote the bar phase (which absolute beat index is a downbeat) and
        express the last downbeat as an absolute render-frame anchor for the mixer."""
        since=float(a.get("since_last_beat_sec") or 0.0)
        beat_end=int(math.floor((t_meas-self._beat_anchor_t)/beat_sec+1e-9))
        db_class=int(a.get("downbeat_class",0) or 0)
        db_conf=_clamp(float(a.get("downbeat_conf",0.0) or 0.0),0,1)
        if db_conf>0.0 and conf>=0.35:
            first=(sum(self._bar_votes)<=1e-9)
            self._bar_votes=[v*0.90 for v in self._bar_votes]
            self._bar_votes[(beat_end-db_class)%4]+=db_conf
            tot=sum(self._bar_votes)
            best=max(range(4),key=lambda i:self._bar_votes[i])
            if first and db_conf>=0.15:
                self._bar_offset=best          # nothing to lose at session start: take the first vote
            elif tot>0.8 and self._bar_votes[best]/tot>=0.55 and best!=self._bar_offset:
                self._bar_offset=best          # the vote moved and clearly dominates
        end_frame=int(a.get("_end_frame") or 0)
        if end_frame>0:
            frac=_clamp(since/beat_sec,0.0,0.9999) if beat_sec>0 else 0.0
            beats_since_down=float((beat_end-int(self._bar_offset))%4)+frac
            self._grid_anchor_frame=float(end_frame)-beats_since_down*beat_sec*float(self._sr)

    def _concert_event(self, beat: int, action: str, score: float, rationale: str, source: str="STRUCTURE") -> dict[str, Any]:
        return {"beat":int(beat),"bar":int(beat)//4,"kind":str(action),"action":str(action),"deck":"CONCERT BUS",
                "value":round(float(score),3),"score":round(float(score),3),"rationale":str(rationale),"source":source}

    def _refresh_performance_queue(self, a: dict[str, Any], now: float):
        """Director V3 rolling-horizon planner.

        v31.8 does not hard-code one upward and one downward gesture chain.  It asks
        a candidate-search director for several 8–16 bar-ish choreographies, scores
        them against structure/prompt/vocal safety/repetition/performance memory,
        and commits only the near-term beats.  Farther events remain replaceable as
        the 15 s future window moves.
        """
        p=self._policy; current=int(self._beat_index); commit=max(1,int(p.commit_beats))
        locked=[q for q in self._performance_queue if current-1<=int(q.get("beat",0))<=current+commit]
        future_tonal=float((self._future_segments[0].get("tonal_strength") if self._future_segments else 0.0) or 0.0)
        events, selected, candidates = self._director.plan(
            segments=list(self._future_segments), current_beat=current, commit_beats=commit,
            policy=p, history=list(self._history), analysis=dict(a), neural_decision=dict(self._decision),
            regen=int(self._regen), musical=self._mi.snapshot(future_tonal),
        )
        self._director_selected=selected; self._director_candidates=candidates

        # If structural evidence is weak, retain one low-authority neural candidate
        # rather than forcing a choreography. Neural semantics support the director;
        # they do not override a coherent multi-beat plan.
        if not events:
            d=dict(self._decision); da=str(d.get("action") or "NO_ACTION")
            fallback_thr=.18-.07*_clamp((float(getattr(p,"agentic_authority",1.0))-1.0)/2.60,0,1)
            if da!="NO_ACTION" and float(d.get("score") or 0)>=fallback_thr:
                events=[self._concert_event(current+commit+1,da,float(d.get("score") or 0),str(d.get("rationale") or "neural semantic candidate"),"CLAP")]

        # Keep one primary command per beat.  Near-term committed events survive a
        # replan; speculative far events are replaced by the selected choreography.
        priority={"PARAM_DROP":.22,"PARAM_SPACE_BREAK":.20,"PARAM_PUNCH_IN":.18,"PARAM_ROLL_ACCEL":.14,
                  "PARAM_DRUM_FILL":.12,"PARAM_FILTER_SWEEP":.10,"PARAM_TENSION":.08,"PARAM_ECHO_OUT":.07}
        bybeat={}
        for q in locked+events:
            b=int(q.get("beat",0))
            if b<current-1: continue
            rank=float(q.get("score",0))+priority.get(str(q.get("action")),0.0)
            if b not in bybeat or rank>bybeat[b][0]: bybeat[b]=(rank,q)
        queue=[v[1] for _,v in sorted(bybeat.items(),key=lambda kv:kv[0])]
        max_events=16 if float(getattr(p,"agentic_authority",1.0))>2.6 else (12 if p.action_density>.72 else 9)
        self._performance_queue=queue[:max_events]

    def _schedule_automation_lane(self, key: str, start: float, end: float, beat: float, duration_beats: float, curve: str="smoothstep", replace: bool=True):
        """Queue a control-rate ramp.  replace=False keeps existing lanes on the
        same key so a gesture can chain an outgoing move with a later return move
        (used by the v31.10 contrary-motion / bass-swap transitions)."""
        duration=max(.18,float(duration_beats))
        if replace:
            self._automation_lanes=[x for x in self._automation_lanes if str(x.get("key"))!=key]
        self._automation_lanes.append({"key":key,"start":float(start),"end":float(end),"start_beat":float(beat),"end_beat":float(beat)+duration,"curve":str(curve)})
        self._automation_lanes=self._automation_lanes[-24:]

    def _apply_automation_lanes(self, c: dict[str, Any], beat_float: float) -> tuple[dict[str, Any], bool]:
        if not self._automation_lanes: return c,False
        keep=[]; changed=False
        # When several lanes target one key, the latest-starting lane that has
        # already begun wins; not-yet-started lanes are always preserved.
        started_by_key: dict[str, dict[str, Any]] = {}
        for lane in self._automation_lanes:
            sb=float(lane.get("start_beat",beat_float))
            if beat_float<sb:
                keep.append(lane); continue
            key=str(lane.get("key"))
            cur=started_by_key.get(key)
            if cur is None or sb>=float(cur.get("start_beat",-1e18)):
                started_by_key[key]=lane
        for key,lane in started_by_key.items():
            sb=float(lane.get("start_beat",beat_float)); eb=max(sb+.001,float(lane.get("end_beat",sb+.001)))
            t=_clamp((beat_float-sb)/(eb-sb),0,1)
            curve=str(lane.get("curve") or "smoothstep")
            if curve=="exponential": q=t*t
            elif curve=="linear": q=t
            else: q=_smoothstep(t)
            a0=float(lane.get("start",c.get(key,0))); a1=float(lane.get("end",a0))
            c[key]=a0+(a1-a0)*q; changed=True
            if t<1.0: keep.append(lane)
        self._automation_lanes=keep
        return c,changed

    def _automation_scheduler_tick(self, now: float):
        if not getattr(self, "_dj_controller", False):
            return                                             # v31.30.25: automation lanes removed
        """25 ms control-rate automation; no PCM copy, FFT, model call or allocation-heavy DSP."""
        out=None
        with self._lock:
            if self._manual or not self._audible_started or self._beat_anchor_t<=0.0 or (not self._automation_lanes and not self._pending_triggers): return
            bpm=max(65.0,float(self._analysis.get("bpm") or self._controller.get("bpm") or 100.0)); beat_sec=60.0/bpm
            beat_float=max(0.0,(now-self._beat_anchor_t)/beat_sec)
            c=dict(self._controller); fired=False
            # v31.11 deferred one-shot triggers (roll-ladder recaptures etc.),
            # quantized to their scheduled beat at 55 ms resolution.
            if self._pending_triggers:
                due=[t for t in self._pending_triggers if float(t.get("beat",1e18))<=beat_float]
                if due:
                    self._pending_triggers=[t for t in self._pending_triggers if t not in due]
                    for t in due:
                        cb=t.get("capture_beats")
                        if cb is not None:
                            self._loop_capture_seq+=1
                            c["loop_beats"]=_clamp(float(cb),.25,16.0); c["loop_capture_seq"]=self._loop_capture_seq
                        for k,v in (t.get("set") or {}).items():
                            if k in c:
                                try: c[k]=float(v)
                                except Exception: pass
                    fired=True
            c,changed=self._apply_automation_lanes(c,beat_float)
            if changed or fired:
                c=self._apply_overrides_locked(c,now)
                self._controller=c; out=dict(c)
        if out is not None: self._send_controls(out,enabled=True)

    def _execute_parameterized_event(self, ev: dict[str, Any], a: dict[str, Any], beat: int, c: dict[str, Any]):
        """Render a Director V4 gesture onto the bounded existing controller surface.

        The proven DSP stays unchanged.  V3 gains expressiveness by varying the
        controller parameters and gesture duration rather than adding heavier DSP.
        """
        family=str(ev.get("family") or str(ev.get("action") or "").replace("PARAM_", ""))
        p=self._policy
        pre=dict(c)
        macro=str(ev.get("macro") or "MACRO_"+family)
        params=dict(ev.get("params") or {}); intensity=_clamp(float(ev.get("intensity") or .55),0,1)
        score=float(ev.get("score") or intensity); rationale=str(ev.get("rationale") or "Director V4 parameterized gesture")
        before=len(self._history)
        c=self._execute_concert_macro(macro,a,beat,c,score,rationale)
        # Override the macro's fixed defaults with plan-specific continuous values.
        if family in {"TENSION","RISER"}:
            c["a_filter"]=_clamp(max(float(c.get("a_filter",0)),float(params.get("filter_depth",0))),0,.30)
            c["noise_riser"]=_clamp(max(float(c.get("noise_riser",0)),float(params.get("riser",0))),0,.92)
            c["fx_reverse_swell"]=_clamp(max(float(c.get("fx_reverse_swell",0)),float(params.get("reverse",0))),0,.86)
            c["fx_width"]=_clamp(max(float(c.get("fx_width",0)),float(params.get("width",0))),0,.84)
            c["fx_echo_send"]=_clamp(max(float(c.get("fx_echo_send",0)),float(params.get("echo_send",0))),0,.76)
        elif family=="FILTER_SWEEP":
            c["a_filter"]=_clamp(float(params.get("depth",c.get("a_filter",0))),-.28,.28)
            c["noise_riser"]=_clamp(max(float(c.get("noise_riser",0)),float(params.get("riser",0))),0,.72)
            c["fx_width"]=_clamp(max(float(c.get("fx_width",0)),float(params.get("width",0))),0,.76)
        elif family=="TRANS_GATE":
            c["fx_gate"]=_clamp(float(params.get("depth",c.get("fx_gate",0))),0,.42); c["fx_gate_div"]=_clamp(float(params.get("division",4)),1,8)
            c["drum_drive"]=_clamp(max(float(c.get("drum_drive",0)),float(params.get("drum_drive",0))),0,.68)
        elif family=="DRUM_FILL":
            c["fx_snare_rush"]=_clamp(max(float(c.get("fx_snare_rush",0)),float(params.get("snare",0))),0,.88)
            c["drum_drive"]=_clamp(max(float(c.get("drum_drive",0)),float(params.get("drum_drive",0))),0,.70)
            c["doubletime"]=_clamp(max(float(c.get("doubletime",0)),float(params.get("doubletime",0))),0,.78)
        elif family=="ROLL_ACCEL":
            c["crossfader"]=_clamp(float(params.get("xfade",c.get("crossfader",0))),0,.30)
            c["fx_gate"]=_clamp(max(float(c.get("fx_gate",0)),float(params.get("gate",0))),0,.42)
            # Start size is used for the realtime loop capture; end/acceleration are
            # retained in history for the next replan and future controller upgrade.
            c["loop_beats"]=_clamp(float(params.get("start_beats",c.get("loop_beats",.5))),.25,2.0)
        elif family in {"ROLL_HALF","ROLL_QUARTER"}:
            c["crossfader"]=_clamp(float(params.get("xfade",c.get("crossfader",0))),0,.26)
        elif family in {"ECHO_HALF","ECHO_ONE","ECHO_OUT"}:
            c["fx_echo_send"]=_clamp(float(params.get("send",c.get("fx_echo_send",0))),0,.82)
            c["fx_echo_feedback"]=_clamp(float(params.get("feedback",c.get("fx_echo_feedback",0))),0,.56)
            c["fx_width"]=_clamp(float(params.get("width",c.get("fx_width",0))),0,.84); c["fx_duck"]=_clamp(float(params.get("duck",.76)),0,1)
            c["fx_echo_beats"]=.5 if family in {"ECHO_HALF","ECHO_OUT"} else 1.0
        elif family in {"WASH_OUT","REVERSE_SWELL","SPACE_BREAK"}:
            c["fx_reverse_swell"]=_clamp(max(float(c.get("fx_reverse_swell",0)),float(params.get("reverse",0))),0,.88)
            c["reverb"]=_clamp(max(float(c.get("reverb",0)),float(params.get("reverb",0))),0,.38)
            c["fx_width"]=_clamp(max(float(c.get("fx_width",0)),float(params.get("width",0))),0,.86)
            if "filter" in params: c["a_filter"]=_clamp(float(params.get("filter",0)),-.18,.22)
            if "echo_send" in params: c["fx_echo_send"]=_clamp(max(float(c.get("fx_echo_send",0)),float(params.get("echo_send",0))),0,.64)
        elif family in {"DECKB_GHOST","DECKB_DRUM_LAYER","DECKB_HARMONIC_BED","DECKB_TEASE","DECKB_SWAP","DECKB_SLICE_GROOVE","DECKB_SLICE_FILL","DECKB_SLICE_CALL","DECKB_SLICE_ACCEL","DECKB_MOTIF_TEASE","DECKB_COUNTER_GROOVE","DECKB_ECHO_FREEZE","DECKB_PICKUP_STUTTER","DECKB_HOOK_CALL","DECKB_DROP_FLASH"}:
            # Source-derived Remix Deck gestures. Capture is quantized by the same
            # scheduler as all Director events; layer mode keeps Deck A present.
            beats=_clamp(float(params.get("loop_beats",1.0)),.25,4.0)
            self._loop_capture_seq+=1; c["loop_beats"]=beats; c["loop_capture_seq"]=self._loop_capture_seq
            self._loop_retrigger_seq+=1; c["loop_retrigger_seq"]=self._loop_retrigger_seq
            c["b_texture"]=_clamp(float(params.get("texture",.55)),-1,1)
            c["b_transient"]=_clamp(float(params.get("transient",.35)),0,1)
            c["b_low_db"]=_clamp(float(params.get("b_low_db",-5.0)),-8,6)
            c["b_mid_db"]=_clamp(float(params.get("b_mid_db",-1.0)),-8,6)
            c["b_high_db"]=_clamp(float(params.get("b_high_db",.5)),-8,6)
            c["b_filter"]=_clamp(float(params.get("b_filter",0.0)),-1,1)
            # Deck B gets intentionally larger FX authority than Deck A, but only
            # on its own bounded remix bus. Slicer gestures select one of four
            # anchor-safe pattern families and drive the bus harder.
            slice_modes={"DECKB_SLICE_GROOVE":1.0,"DECKB_SLICE_FILL":2.0,"DECKB_SLICE_CALL":3.0,"DECKB_SLICE_ACCEL":4.0}
            if family in slice_modes:
                c["b_slice_mode"]=slice_modes[family]
                c["b_slicer_mix"]=_clamp(float(params.get("slice_mix",.68)),0,.92)
                c["b_fx"]=_clamp(float(params.get("b_fx",.74)),0,.95)
                c["b_texture"]=_clamp(float(params.get("texture",.72)),-1,1)
                c["b_transient"]=_clamp(max(float(c.get("b_transient",0)),float(params.get("transient",.58))),0,1)
            else:
                # Existing Remix Deck moves also receive a stronger, shorter B-only
                # effects field so the second deck is clearly audible as a performance layer.
                c["b_fx"]=_clamp(max(float(c.get("b_fx",0)),.28+.38*intensity),0,.82)
            # v31.9.1 Wow-scene gestures reuse the same future-qualified Deck-B
            # motif but change its performance role. No unrelated samples are added.
            if family=="DECKB_MOTIF_TEASE":
                c["b_slice_mode"]=3.0; c["b_slicer_mix"]=_clamp(float(params.get("slice_mix",.38)),0,.62)
                c["b_fx"]=_clamp(float(params.get("b_fx",.60)),0,.80)
                c["fx_echo_send"]=_clamp(max(float(c.get("fx_echo_send",0)),float(params.get("echo_send",.16))),0,.48)
            elif family=="DECKB_COUNTER_GROOVE":
                c["b_slice_mode"]=1.0; c["b_slicer_mix"]=_clamp(float(params.get("slice_mix",.52)),0,.72)
                c["b_fx"]=_clamp(float(params.get("b_fx",.58)),0,.78)
            elif family=="DECKB_ECHO_FREEZE":
                c["b_slice_mode"]=5.0; c["b_slicer_mix"]=_clamp(float(params.get("slice_mix",.46)),0,.68)
                c["b_fx"]=_clamp(float(params.get("b_fx",.84)),0,.94)
                c["fx_echo_send"]=_clamp(max(float(c.get("fx_echo_send",0)),float(params.get("echo_send",.46))),0,.66)
                c["fx_echo_feedback"]=_clamp(max(float(c.get("fx_echo_feedback",0)),float(params.get("feedback",.42))),0,.54)
                c["fx_echo_beats"]=.5
            elif family=="DECKB_PICKUP_STUTTER":
                c["b_slice_mode"]=2.0; c["b_slicer_mix"]=_clamp(float(params.get("slice_mix",.78)),0,.90)
                c["b_fx"]=_clamp(float(params.get("b_fx",.82)),0,.94)
            elif family=="DECKB_HOOK_CALL":
                c["b_slice_mode"]=3.0; c["b_slicer_mix"]=_clamp(float(params.get("slice_mix",.54)),0,.74)
                c["b_fx"]=_clamp(float(params.get("b_fx",.68)),0,.86)
                c["fx_echo_send"]=_clamp(max(float(c.get("fx_echo_send",0)),float(params.get("echo_send",.22))),0,.52)
            elif family=="DECKB_DROP_FLASH":
                c["b_slice_mode"]=1.0; c["b_slicer_mix"]=_clamp(.40+.22*intensity,0,.72)
                c["b_fx"]=_clamp(float(params.get("b_fx",.74)),0,.90)
                c["crossfader"]=_clamp(float(params.get("xfade",.14)),0,.19)
            vocal_now=_clamp(float(a.get("vocal_activity") or 0.0),0,1)
            drums_now=_clamp(float(a.get("drums_activity") or 0.0),0,1)
            vocal_guard=1.0-.62*_clamp((vocal_now-.58)/.34,0,1)
            if family in {"DECKB_SWAP","DECKB_DROP_FLASH"}:
                # Full call-response is reserved for sparse/instrumental moments.
                # With a foreground vocal it degrades into a small percussive ghost
                # layer rather than repeating a lyric fragment against itself.
                if vocal_now>.68:
                    c["crossfader"]=0.0; c["b_layer"]=_clamp(.08+.08*intensity,0,.18)
                    c["b_texture"]=max(.72,c["b_texture"]); c["b_transient"]=max(.42,c["b_transient"])
                    c["b_low_db"]=-7.0; c["b_mid_db"]=-3.0
                else:
                    if family=="DECKB_DROP_FLASH":
                        c["b_layer"]=_clamp(float(params.get("layer",.05))*vocal_guard,0,.08); c["crossfader"]=_clamp(float(params.get("xfade",.14))*vocal_guard,0,.19)
                    else:
                        c["b_layer"]=_clamp(.05+.05*intensity,0,.11); c["crossfader"]=_clamp(float(params.get("xfade",.22))*vocal_guard,0,.28)
            else:
                c["crossfader"]=_clamp(float(params.get("xfade",0.0)),0,.10)
                layer=float(params.get("layer",.24))*vocal_guard
                if family=="DECKB_DRUM_LAYER" and drums_now<.28: layer*=.58
                if family.startswith("DECKB_SLICE_"):
                    # Slicer is strongest on instrumental/percussive material. Vocals
                    # reduce layer amount, but do not disable the rhythmic remix.
                    layer*=1.0-.38*_clamp((vocal_now-.52)/.40,0,1)
                c["b_layer"]=_clamp(layer,0,.38)
            if family=="DECKB_TEASE":
                c["fx_echo_send"]=_clamp(max(float(c.get("fx_echo_send",0)),float(params.get("echo_send",.20))),0,.54)
                c["fx_echo_feedback"]=_clamp(max(float(c.get("fx_echo_feedback",0)),float(params.get("feedback",.28))),0,.48)
        elif family=="DECKB_MICRO_SHIFT":
            # Reuse the already future-matched Deck-B capture. This creates more
            # A<->B movement without repeatedly recapturing/analysing the loop.
            c["crossfader"]=_clamp(float(params.get("xfade",.12)),0,.20)
            c["b_layer"]=_clamp(max(float(c.get("b_layer",0)),float(params.get("layer",.035))),0,.10)
            c["b_fx"]=_clamp(max(float(c.get("b_fx",0)),float(params.get("b_fx",.42))),0,.72)
        elif family=="DECKB_RETURN":
            # Return fully to A but keep only the *buffer* resident. No audible
            # Deck-B layer remains between pulses; this raises cadence, not dominance.
            c["crossfader"]=0.0
            c["b_layer"]=0.0
            c["b_fx"]=_clamp(min(float(c.get("b_fx",0)),.10),0,.10)
        elif family=="DECKB_RELEASE":
            c["b_layer"]=0.0; c["crossfader"]=0.0; c["b_texture"]=0.0; c["b_transient"]=0.0; c["b_slicer_mix"]=0.0; c["b_slice_mode"]=0.0; c["b_fx"]=0.0
            self._loop_release_seq+=1; c["loop_release_seq"]=self._loop_release_seq
        elif family in {"GROOVE_LIFT","PUNCH_IN","DROP"}:
            c["perf_punch"]=_clamp(max(float(c.get("perf_punch",0)),float(params.get("punch",0))),0,.88)
            c["perf_energy"]=_clamp(max(float(c.get("perf_energy",0)),float(params.get("energy",0))),0,.84)
            c["drum_drive"]=_clamp(max(float(c.get("drum_drive",0)),float(params.get("drum_drive",0))),0,.72)
            c["perf_air"]=_clamp(max(float(c.get("perf_air",0)),float(params.get("air",0))),0,.48)
            c["perf_clarity"]=_clamp(max(float(c.get("perf_clarity",0)),float(params.get("clarity",0))),0,.62)
            if family=="DROP": c["impact_strength"]=_clamp(max(float(c.get("impact_strength",0)),float(params.get("impact",0))),0,1)
        elif family=="AB_CONTRARY_RISE":
            # The rise/fall duet: Deck B RISES (HPF opens downward into full body,
            # layer and lows glide in) while Deck A FALLS (LPF closes, gain dips,
            # lows glide out).  All motion is opposing control-rate lanes over the
            # same beat window, so the two arcs audibly cross.  One low-end owner
            # at any moment (bass-swap grammar from the auto-DJ literature).
            depth=_clamp(float(params.get("depth",.60)),0,1)
            dur=max(2.0,float(params.get("duration_beats",6.0))); curve=str(params.get("curve") or "smoothstep")
            vocal_now=_clamp(float(a.get("vocal_activity") or 0.0),0,1)
            vg=1.0-.45*_clamp((vocal_now-.60)/.32,0,1)
            mixer=self.engine.status().get("agentic_mixer") or {}
            if not mixer.get("loop_ready"):
                self._loop_capture_seq+=1
                c["loop_beats"]=_clamp(float(params.get("loop_beats",2.0)),.5,4.0)
                c["loop_capture_seq"]=self._loop_capture_seq
                c["b_texture"]=_clamp(.25+.30*depth,-1,1); c["b_transient"]=_clamp(.28+.20*depth,0,1)
            a_close=_clamp(float(params.get("a_close",.45))*depth*vg,0,.62)
            b_open=_clamp(float(params.get("b_open",.70)),0,1)
            layer=_clamp(float(params.get("layer",.22))*vg,0,.34)
            handoff=_clamp(float(params.get("bass_handoff",.5))*depth,0,1)
            riser=_clamp(float(params.get("riser",.40))*depth*p.fx_presence,0,.80)
            # Deck A falls…
            self._schedule_automation_lane("a_filter",float(c.get("a_filter",0)),-.30*a_close/.62,beat,dur,curve)
            self._schedule_automation_lane("a_gain",float(c.get("a_gain",1.0)),1.0-.15*a_close,beat,dur,curve)
            self._schedule_automation_lane("a_high_db",float(c.get("a_high_db",0)),-2.2*a_close,beat,dur,curve)
            self._schedule_automation_lane("a_low_db",float(c.get("a_low_db",0)),-4.8*handoff,beat,dur,curve)
            # …while Deck B rises.
            c["b_filter"]=max(float(c.get("b_filter",0)),.55*b_open)
            self._schedule_automation_lane("b_filter",float(c["b_filter"]),0.0,beat,dur,curve)
            self._schedule_automation_lane("b_layer",float(c.get("b_layer",0)),layer,beat,dur,curve)
            self._schedule_automation_lane("b_gain",float(c.get("b_gain",1.0)),min(1.08,.94+.14*depth),beat,dur,curve)
            start_blow=float(c.get("b_low_db",-6.5)); c["b_low_db"]=min(start_blow,-6.5)
            self._schedule_automation_lane("b_low_db",float(c["b_low_db"]),-6.5+6.0*handoff,beat,dur,curve)
            self._schedule_automation_lane("b_high_db",float(c.get("b_high_db",0)),1.8*depth,beat,dur,curve)
            self._schedule_automation_lane("noise_riser",float(c.get("noise_riser",0)),riser,beat,dur,"exponential")
            self._schedule_automation_lane("fx_width",float(c.get("fx_width",0)),_clamp(.28+.34*depth,0,.78),beat,dur,curve)
            self._transition={"active":True,"name":"CONTRARY RISE","a_role":"FALL","b_role":"RISE","start_beat":int(beat),"end_beat":int(beat+math.ceil(dur)),"depth":round(depth,3)}
        elif family=="AB_CONTRARY_FALL":
            # Mirror phase: Deck A rises back home while Deck B folds up and away.
            depth=_clamp(float(params.get("depth",.55)),0,1)
            dur=max(2.0,float(params.get("duration_beats",4.0))); curve=str(params.get("curve") or "smoothstep")
            a_open=_clamp(float(params.get("a_open",.75)),0,1)
            b_close=_clamp(float(params.get("b_close",.65)),0,1)
            self._schedule_automation_lane("a_filter",float(c.get("a_filter",0)),0.0,beat,dur,curve)
            self._schedule_automation_lane("a_gain",float(c.get("a_gain",1.0)),1.0,beat,dur,curve)
            self._schedule_automation_lane("a_high_db",float(c.get("a_high_db",0)),0.0,beat,dur,curve)
            self._schedule_automation_lane("a_low_db",float(c.get("a_low_db",0)),0.0,beat,dur*.8,curve)
            self._schedule_automation_lane("b_layer",float(c.get("b_layer",0)),.035*(1.0-b_close)+.0,beat,dur,curve)
            self._schedule_automation_lane("b_filter",float(c.get("b_filter",0)),.45*b_close,beat,dur,curve)
            self._schedule_automation_lane("b_gain",float(c.get("b_gain",1.0)),.94,beat,dur,curve)
            self._schedule_automation_lane("b_low_db",float(c.get("b_low_db",0)),-6.5,beat,dur*.7,curve)
            c["fx_reverse_swell"]=_clamp(max(float(c.get("fx_reverse_swell",0)),float(params.get("reverse",0))*p.fx_presence),0,.72)
            c["reverb"]=_clamp(max(float(c.get("reverb",0)),float(params.get("reverb",0))*p.fx_presence),0,.32)
            self._transition={"active":True,"name":"CONTRARY FALL","a_role":"RISE","b_role":"FALL","start_beat":int(beat),"end_beat":int(beat+math.ceil(dur)),"depth":round(depth,3)}
        elif family=="BASS_SWAP_GLIDE":
            # Classic one-bass-owner handoff with an automatic scheduled return.
            handoff=_clamp(float(params.get("handoff",.7)),0,1)
            layer=_clamp(float(params.get("layer",.18)),0,.28)
            dur=max(2.0,float(params.get("duration_beats",4.0)))
            hold=max(1.0,float(params.get("hold_beats",4.0)))
            mixer=self.engine.status().get("agentic_mixer") or {}
            if not mixer.get("loop_ready"):
                self._loop_capture_seq+=1; c["loop_beats"]=4.0; c["loop_capture_seq"]=self._loop_capture_seq
                c["b_texture"]=-.35; c["b_transient"]=.10
            c["b_low_db"]=min(float(c.get("b_low_db",-6.5)),-6.5)
            self._schedule_automation_lane("a_low_db",float(c.get("a_low_db",0)),-6.2*handoff,beat,dur)
            self._schedule_automation_lane("b_low_db",float(c["b_low_db"]),-6.5+6.2*handoff,beat,dur)
            self._schedule_automation_lane("b_layer",float(c.get("b_layer",0)),layer,beat,dur)
            # Scheduled return: lows glide home after the hold, without a new event.
            back=beat+dur+hold
            self._schedule_automation_lane("a_low_db",-6.2*handoff,0.0,back,2.5,replace=False)
            self._schedule_automation_lane("b_low_db",-6.5+6.2*handoff,-6.5,back,2.0,replace=False)
            self._schedule_automation_lane("b_layer",layer,.05,back,2.5,replace=False)
            self._transition={"active":True,"name":"BASS SWAP","a_role":"FALL","b_role":"RISE","start_beat":int(beat),"end_beat":int(beat+math.ceil(dur+hold+2.5)),"depth":round(handoff,3)}
        elif family=="AB_BREATH":
            # Shared inhale/exhale: both decks widen into space then settle back.
            width=_clamp(float(params.get("width",.55))*p.fx_presence,0,.80)
            dur=max(1.5,float(params.get("duration_beats",3.0))); half=dur*.5
            c["reverb"]=_clamp(max(float(c.get("reverb",0)),float(params.get("reverb",0))*p.fx_presence),0,.34)
            c["fx_echo_send"]=_clamp(max(float(c.get("fx_echo_send",0)),float(params.get("echo_send",0))*p.fx_presence),0,.52)
            dip=_clamp(float(params.get("depth",.16)),0,.30)
            self._schedule_automation_lane("fx_width",float(c.get("fx_width",0)),width,beat,half)
            self._schedule_automation_lane("fx_width",width,.12,beat+half,half,replace=False)
            self._schedule_automation_lane("a_filter",float(c.get("a_filter",0)),-dip,beat,half)
            self._schedule_automation_lane("a_filter",-dip,0.0,beat+half,half,replace=False)
        elif family in {"SPINBACK","BRAKE_STOP"}:
            # v31.11 the record itself is the effect: brake winds the program to a
            # stop, backspin rips it backwards.  The engine reads its own output
            # ring; the effect ends exactly on the landing beat.
            beats=_clamp(float(params.get("beats",1.0 if family=="SPINBACK" else 2.0)),.25,4.0)
            if macro not in {"MACRO_SPINBACK","MACRO_BRAKE"}:
                # (the pad macros already armed the trigger in that path)
                self._fx_brake_seq+=1
            c["fx_brake_seq"]=self._fx_brake_seq
            c["fx_brake_mode"]=1.0 if family=="SPINBACK" else 0.0
            c["fx_brake_beats"]=beats
            # Build layers must not keep hissing through the vacuum the effect creates.
            c["noise_riser"]*= .25; c["fx_snare_rush"]*=.25; c["fx_gate"]=0.0
            self._transition={"active":True,"name":("SPINBACK" if family=="SPINBACK" else "BRAKE"),
                              "a_role":"FALL","b_role":"HOLD","start_beat":int(beat),"end_beat":int(beat+math.ceil(beats)),"depth":1.0}
        elif family in {"FLASH_FORWARD","JUMPBACK","TIME_INTERLEAVE"}:
            # v31.14 TimeWeave gestures — the DJ plays with time itself:
            # FLASH_FORWARD foreshadows a phrase the listener has not reached yet,
            # JUMPBACK replays the previous phrase (a live arrangement edit),
            # TIME_INTERLEAVE alternates NOW and the FUTURE in beat cells.
            mode={"JUMPBACK":2.0,"FLASH_FORWARD":3.0,"TIME_INTERLEAVE":4.0}[family]
            beats=_clamp(float(params.get("beats",2.0 if family=="FLASH_FORWARD" else (4.0 if family=="JUMPBACK" else 8.0))),.5,8.0)
            if family=="FLASH_FORWARD":
                off=_clamp(float(params.get("offset_beats",16.0)),4.0,32.0)
            elif family=="JUMPBACK":
                off=_clamp(float(params.get("offset_beats",4.0)),1.0,16.0)
            else:
                bpmn=max(65.0,float(a.get("bpm") or 120.0))
                off=_clamp(float(params.get("offset_beats",round(7.5*bpmn/60.0))),4.0,32.0)
            self._fx_brake_seq+=1
            c["fx_brake_seq"]=self._fx_brake_seq
            c["fx_brake_mode"]=mode; c["fx_brake_beats"]=beats
            c["fx_weave_offset_beats"]=off
            c["fx_weave_cell_beats"]=_clamp(float(params.get("cell_beats",2.0)),.5,4.0)
            wname={"JUMPBACK":"JUMPBACK","FLASH_FORWARD":"FLASH-FORWARD","TIME_INTERLEAVE":"TIME INTERLEAVE"}[family]
            self._transition={"active":True,"name":wname,"a_role":("RISE" if family=="FLASH_FORWARD" else "HOLD"),
                              "b_role":"HOLD","start_beat":int(beat),"end_beat":int(beat+math.ceil(beats)),"depth":round(off/32.0,3)}
        elif family=="PUMP_GROOVE":
            depth=_clamp(float(params.get("depth",.45)),0,.72)
            cycle=_clamp(float(params.get("cycle_beats",1.0)),.25,4.0)
            dur=max(2.0,float(params.get("duration_beats",8.0)))
            self._pump_sync_seq+=1
            c["fx_pump_sync_seq"]=self._pump_sync_seq
            c["fx_pump_cycle"]=cycle
            self._schedule_automation_lane("fx_pump",float(c.get("fx_pump",0)),depth,beat,1.0)
            self._schedule_automation_lane("fx_pump",depth,0.0,beat+dur-1.0,1.5,replace=False)
        elif family=="QUANT_BUILD":
            # Produced-EDM build ending exactly on the landing: riser + reverse
            # climb, snare-rush ladder (1/4 -> 1/32), and the bass floor strips
            # away — all opposing the drop that will restore it.
            depth=_clamp(float(params.get("depth",.75)),0,1)
            dur=max(3.0,float(params.get("duration_beats",8.0)))
            riser=_clamp(float(params.get("riser",.70))*p.fx_presence*(0.55+0.45*depth),0,.90)
            strip=_clamp(float(params.get("bass_strip",.75))*depth,0,.92)
            rush=_clamp(float(params.get("rush",.60))*p.fx_presence,0,.85)
            vocal_now=_clamp(float(a.get("vocal_activity") or 0.0),0,1)
            vg=1.0-.38*_clamp((vocal_now-.62)/.30,0,1)
            self._schedule_automation_lane("noise_riser",float(c.get("noise_riser",0)),riser*vg,beat,dur,"exponential")
            self._schedule_automation_lane("fx_rush_accel",0.0,1.0,beat,dur,"linear")
            self._schedule_automation_lane("fx_snare_rush",float(c.get("fx_snare_rush",0)),rush*vg,beat,dur,"exponential")
            self._schedule_automation_lane("fx_bass_cut",float(c.get("fx_bass_cut",0)),strip,beat,dur*.82)
            self._schedule_automation_lane("fx_width",float(c.get("fx_width",0)),_clamp(.30+.35*depth,0,.80),beat,dur)
            self._schedule_automation_lane("a_high_db",float(c.get("a_high_db",0)),1.3*depth,beat,dur)
            c["fx_pump"]=min(float(c.get("fx_pump",0)),.10)
            self._transition={"active":True,"name":"QUANT BUILD","a_role":"RISE","b_role":"HOLD",
                              "start_beat":int(beat),"end_beat":int(beat+math.ceil(dur)),"depth":round(depth,3)}
        elif family=="ROLL_LADDER":
            # The halving beat-roll from DJ practice: 2 -> 1 -> 1/2 -> 1/4, each
            # recapture quantized to its own beat via the deferred-trigger list.
            xf=_clamp(float(params.get("xfade",.22)),0,.34)
            start_beats=_clamp(float(params.get("start_beats",2.0)),1.0,4.0)
            self._loop_capture_seq+=1
            c["loop_beats"]=start_beats; c["loop_capture_seq"]=self._loop_capture_seq
            self._schedule_automation_lane("crossfader",float(c.get("crossfader",0)),xf,beat,3.5)
            self._pending_triggers=[t for t in self._pending_triggers if str(t.get("tag"))!="roll_ladder"]
            self._pending_triggers.extend([
                {"tag":"roll_ladder","beat":float(beat+2.0),"capture_beats":start_beats*.5},
                {"tag":"roll_ladder","beat":float(beat+3.0),"capture_beats":start_beats*.25},
                {"tag":"roll_ladder","beat":float(beat+3.5),"capture_beats":max(.25,start_beats*.125),"set":{"crossfader":min(.34,xf+.06)}},
            ])
            self._transition={"active":True,"name":"ROLL LADDER","a_role":"HOLD","b_role":"RISE",
                              "start_beat":int(beat),"end_beat":int(beat+4),"depth":round(xf,3)}
        # Convert stepped macro values into control-rate musical envelopes. Discrete
        # triggers (loop capture / impact seq) remain sample-safe in the native DSP,
        # while tonal/wet controls ramp over the Director-specified beat duration.
        duration=float(params.get("duration_beats") or params.get("tail_beats") or 1.0)
        curve=str(params.get("curve") or "smoothstep")
        ramp_map={
            "TENSION":("a_filter","noise_riser","fx_reverse_swell","fx_width","fx_echo_send"),
            "RISER":("a_filter","noise_riser","fx_width"),
            "FILTER_SWEEP":("a_filter","noise_riser","fx_width"),
            "TRANS_GATE":("fx_gate","drum_drive"),
            "DRUM_FILL":("fx_snare_rush","drum_drive","doubletime"),
            "ECHO_HALF":("fx_echo_send","fx_echo_feedback","fx_width"),
            "ECHO_ONE":("fx_echo_send","fx_echo_feedback","fx_width"),
            "ECHO_OUT":("fx_echo_send","fx_echo_feedback","fx_width"),
            "WASH_OUT":("fx_reverse_swell","reverb","fx_width","a_filter"),
            "REVERSE_SWELL":("fx_reverse_swell","reverb","fx_width"),
            "SPACE_BREAK":("fx_reverse_swell","reverb","fx_width","fx_echo_send"),
            "GROOVE_LIFT":("perf_punch","perf_energy","drum_drive","perf_air"),
            "PUNCH_IN":("perf_punch","perf_energy","drum_drive","perf_clarity"),
            "DECKB_GHOST":("b_layer","b_texture","b_transient"),
            "DECKB_DRUM_LAYER":("b_layer","b_texture","b_transient"),
            "DECKB_HARMONIC_BED":("b_layer","b_texture"),
            "DECKB_TEASE":("b_layer","b_texture","fx_echo_send"),
            "DECKB_SWAP":("crossfader","b_texture","b_fx"),
            "DECKB_MICRO_SHIFT":("crossfader","b_layer","b_fx"),
            "DECKB_RETURN":("crossfader","b_layer","b_fx"),
            "DECKB_SLICE_GROOVE":("b_layer","b_texture","b_transient","b_slicer_mix","b_fx"),
            "DECKB_SLICE_FILL":("b_layer","b_texture","b_transient","b_slicer_mix","b_fx"),
            "DECKB_SLICE_CALL":("b_layer","b_texture","b_transient","b_slicer_mix","b_fx"),
            "DECKB_SLICE_ACCEL":("b_layer","b_texture","b_transient","b_slicer_mix","b_fx"),
            "DECKB_MOTIF_TEASE":("b_layer","b_texture","b_slicer_mix","b_fx","fx_echo_send"),
            "DECKB_COUNTER_GROOVE":("b_layer","b_texture","b_transient","b_slicer_mix","b_fx"),
            "DECKB_ECHO_FREEZE":("b_layer","b_texture","b_slicer_mix","b_fx","fx_echo_send","fx_echo_feedback"),
            "DECKB_PICKUP_STUTTER":("b_layer","b_texture","b_transient","b_slicer_mix","b_fx"),
            "DECKB_HOOK_CALL":("b_layer","b_texture","b_slicer_mix","b_fx","fx_echo_send"),
            "DECKB_DROP_FLASH":("crossfader","b_layer","b_texture","b_slicer_mix","b_fx"),
            "DECKB_RELEASE":("b_layer","crossfader","b_slicer_mix","b_fx"),
        }
        for key in ramp_map.get(family,()):
            if key in c:
                target=float(c.get(key,0)); start=float(pre.get(key,target)); c[key]=start
                self._schedule_automation_lane(key,start,target,beat,duration,curve)

        # Replace the macro-only memory record with the richer parameterized one.
        if len(self._history)>before:
            self._history[-1].update({"kind":f"PARAM_{family}","family":family,"params":params,"intensity":round(intensity,3),"plan":str(self._director_selected.get("name") or "Director V4")})
        else:
            self._history.append({"beat":beat,"bar":beat//4,"kind":f"PARAM_{family}","family":family,"params":params,"intensity":round(intensity,3),"rationale":rationale,"time":time.time()})
        return c

    def _execute_concert_macro(self, action: str, a: dict[str, Any], beat: int, c: dict[str, Any], score: float=0.5, rationale: str=""):
        p=self._policy; vocal=float(a.get("vocal_activity") or 0.0); strength=_clamp(.58+.42*max(score,p.action_density),0,1)
        if action=="MACRO_TENSION":
            vg=1.0-.55*_clamp((vocal-.45)/.45,0,1)
            c["a_filter"]=_clamp(max(float(c.get("a_filter",0)),.08+.11*strength),0,.28)
            c["noise_riser"]=_clamp(max(float(c.get("noise_riser",0)),p.fx_riser*p.fx_presence*.58*strength*vg),0,.82)
            c["fx_reverse_swell"]=_clamp(max(float(c.get("fx_reverse_swell",0)),p.fx_reverse*p.fx_presence*.52*strength*vg),0,.78)
            c["fx_rack_wet"]=_clamp(max(float(c.get("fx_rack_wet",0)),.34*p.fx_presence),0,.62); c["fx_width"]=_clamp(max(float(c.get("fx_width",0)),.56*p.fx_presence),0,.80)
            c["fx_echo_send"]=_clamp(max(float(c.get("fx_echo_send",0)),.18*p.fx_echo*p.fx_presence),0,.55); c["fx_echo_feedback"]=_clamp(max(float(c.get("fx_echo_feedback",0)),.30),0,.54); c["fx_duck"]=.72
        elif action=="MACRO_ECHO_OUT":
            c["fx_rack_wet"]=_clamp(max(float(c.get("fx_rack_wet",0)),.55*p.fx_presence),0,.72); c["fx_echo_send"]=_clamp((.48+.28*p.fx_echo)*p.fx_presence,0,.82); c["fx_echo_feedback"]=_clamp(.34+.18*p.fx_echo,0,.56); c["fx_echo_beats"]=.5; c["fx_width"]=_clamp(.52+.20*p.fx_space,0,.82); c["fx_duck"]=.82
        elif action=="MACRO_ROLL_ACCEL":
            if vocal<.72:
                self._loop_capture_seq+=1; c["loop_beats"]=.5 if p.morph_target=="dnb" else 1.0; c["loop_capture_seq"]=self._loop_capture_seq; c["crossfader"]=_clamp(.12+.12*strength,0,.28)
                c["drum_drive"]=_clamp(max(float(c.get("drum_drive",0)),.42*strength),0,.72); c["doubletime"]=_clamp(max(float(c.get("doubletime",0)),.38*strength if p.morph_target=="dnb" else .12),0,.72); c["fx_gate"]=_clamp(max(float(c.get("fx_gate",0)),.20*p.fx_presence),0,.42); c["fx_gate_div"]=4.0
        elif action=="MACRO_FILTER_SWEEP":
            vg=1.0-.60*_clamp((vocal-.52)/.38,0,1)
            c["a_filter"]=_clamp(max(float(c.get("a_filter",0)),.10+.10*strength*vg),0,.24)
            c["noise_riser"]=_clamp(max(float(c.get("noise_riser",0)),.24*p.fx_riser*p.fx_presence*strength*vg),0,.56)
            c["fx_width"]=_clamp(max(float(c.get("fx_width",0)),.30*p.fx_presence),0,.58)
        elif action=="MACRO_TRANS_BUILD":
            if vocal<.72:
                c["fx_rack_wet"]=_clamp(max(float(c.get("fx_rack_wet",0)),.38*p.fx_presence),0,.56)
                c["fx_gate"]=_clamp(max(float(c.get("fx_gate",0)),.16+.12*strength),0,.34); c["fx_gate_div"]=4.0
                c["drum_drive"]=_clamp(max(float(c.get("drum_drive",0)),.24+.20*strength),0,.58)
        elif action=="MACRO_ROLL_HALF":
            if vocal<.76:
                self._loop_capture_seq+=1; c["loop_beats"]=.5; c["loop_capture_seq"]=self._loop_capture_seq; c["crossfader"]=_clamp(.13+.10*strength,0,.25)
        elif action=="MACRO_ROLL_QUARTER":
            if vocal<.62:
                self._loop_capture_seq+=1; c["loop_beats"]=.25; c["loop_capture_seq"]=self._loop_capture_seq; c["crossfader"]=_clamp(.10+.08*strength,0,.20)
        elif action=="MACRO_ECHO_HALF":
            c["fx_rack_wet"]=_clamp(max(float(c.get("fx_rack_wet",0)),.46*p.fx_presence),0,.64); c["fx_echo_send"]=_clamp(.42+.20*p.fx_echo,0,.68); c["fx_echo_feedback"]=_clamp(.28+.14*p.fx_echo,0,.48); c["fx_echo_beats"]=.5; c["fx_width"]=_clamp(.42+.18*p.fx_space,0,.68); c["fx_duck"]=.76
        elif action=="MACRO_ECHO_ONE":
            c["fx_rack_wet"]=_clamp(max(float(c.get("fx_rack_wet",0)),.44*p.fx_presence),0,.62); c["fx_echo_send"]=_clamp(.38+.18*p.fx_echo,0,.64); c["fx_echo_feedback"]=_clamp(.26+.14*p.fx_echo,0,.46); c["fx_echo_beats"]=1.0; c["fx_width"]=_clamp(.40+.18*p.fx_space,0,.66); c["fx_duck"]=.74
        elif action=="MACRO_WASH_OUT":
            vg=1.0-.44*_clamp((vocal-.60)/.32,0,1)
            c["fx_reverse_swell"]=_clamp(max(float(c.get("fx_reverse_swell",0)),.42*p.fx_reverse*p.fx_presence*vg),0,.68)
            c["reverb"]=_clamp(max(float(c.get("reverb",0)),.18*p.fx_space*p.fx_presence),0,.32); c["fx_width"]=_clamp(max(float(c.get("fx_width",0)),.56*p.fx_presence),0,.76)
            c["a_filter"]=_clamp(min(float(c.get("a_filter",0)),-.05*strength),-.16,.20)
        elif action=="MACRO_PUNCH_IN":
            c["a_filter"]=0.0; c["fx_gate"]*=.25; c["fx_echo_send"]*=.30
            c["a_gain"]=1.0; c["a_low_db"]=max(float(c.get("a_low_db",0)),-.4)
            c["fx_bass_cut"]=0.0; c["fx_rush_accel"]=0.0
            self._automation_lanes=[l for l in self._automation_lanes if str(l.get("key")) not in {"a_filter","a_gain","a_low_db","a_high_db","noise_riser","fx_bass_cut","fx_rush_accel"}]
            c["perf_punch"]=_clamp(max(float(c.get("perf_punch",0)),.42+.24*strength),0,.82); c["perf_energy"]=_clamp(max(float(c.get("perf_energy",0)),.32+.18*strength),0,.72); c["drum_drive"]=_clamp(max(float(c.get("drum_drive",0)),.30+.18*strength),0,.62)
        elif action=="MACRO_DRUM_FILL":
            if vocal<.70:
                c["fx_snare_rush"]=_clamp(max(float(c.get("fx_snare_rush",0)),.42+.28*strength),0,.78); c["drum_drive"]=_clamp(max(float(c.get("drum_drive",0)),.30+.16*strength),0,.56)
        elif action=="MACRO_CLEAN_RESET":
            for key in ("noise_riser","fx_reverse_swell","fx_snare_rush","fx_gate","fx_echo_send","reverb","echo","a_filter","b_layer","b_texture","b_transient","b_slicer_mix","b_slice_mode","b_fx","fx_pump","fx_bass_cut","fx_rush_accel","fx_redrum"):
                c[key]=0.0
            # True clean bar: park the arranger in a neutral section so it does
            # not immediately re-approach its targets; it re-enters at beat+8.
            self._remix.update({"section":"—","until_beat":int(beat)+8})
            c["a_gain"]=1.0; c["b_gain"]=1.0; c["a_low_db"]=0.0; c["a_mid_db"]=0.0; c["a_high_db"]=0.0; c["b_low_db"]=0.0; c["b_mid_db"]=0.0; c["b_high_db"]=0.0; c["b_filter"]=0.0
            self._automation_lanes=[]
            self._pending_triggers=[]
            self._transition={"active":False,"name":"","a_role":"HOLD","b_role":"HOLD","start_beat":0,"end_beat":0}
            c["fx_rack_wet"]*=.35; c["fx_width"]*=.45
            if float(c.get("crossfader",0))>.01:
                self._loop_release_seq+=1; c["loop_release_seq"]=self._loop_release_seq; c["crossfader"]=0.0
        elif action=="MACRO_DROP":
            c["noise_riser"]=0.0; c["fx_reverse_swell"]=0.0; c["fx_snare_rush"]=0.0; c["fx_gate"]=0.0; c["fx_echo_send"]=0.0; c["a_filter"]=0.0; c["b_layer"]=0.0; c["b_texture"]=0.0; c["b_transient"]=0.0; c["b_slicer_mix"]=0.0; c["b_slice_mode"]=0.0; c["b_fx"]=0.0
            c["a_gain"]=1.0; c["a_low_db"]=0.0; c["a_high_db"]=max(0.0,float(c.get("a_high_db",0)))
            # v31.11 the drop slams the full spectrum back: the build's bass strip
            # and rush ladder end HERE, and a fresh pump cycle locks to this beat.
            c["fx_bass_cut"]=0.0; c["fx_rush_accel"]=0.0
            self._pump_sync_seq+=1; c["fx_pump_sync_seq"]=self._pump_sync_seq
            # A landing owns the mixer: retire any in-flight contrary-motion lanes
            # so a stale ramp cannot re-raise Deck B after the release.
            self._automation_lanes=[l for l in self._automation_lanes if str(l.get("key")) not in {"a_filter","a_gain","a_low_db","a_high_db","b_layer","b_gain","b_filter","b_low_db","b_high_db","crossfader","noise_riser","fx_bass_cut","fx_rush_accel","fx_snare_rush"}]
            self._impact_seq+=1; c["impact_seq"]=self._impact_seq; c["impact_strength"]=_clamp((.64+.30*strength)*p.fx_impact*p.fx_presence,0,1); c["perf_punch"]=_clamp(max(float(c.get("perf_punch",0)),.58*strength),0,.86); c["perf_energy"]=_clamp(max(float(c.get("perf_energy",0)),.52*strength),0,.82)
            if float(c.get("crossfader",0))>.05: self._loop_release_seq+=1; c["loop_release_seq"]=self._loop_release_seq; c["crossfader"]=0.0
        elif action=="MACRO_SPACE_BREAK":
            vg=1.0-.42*_clamp((vocal-.58)/.35,0,1); c["fx_reverse_swell"]=_clamp(max(float(c.get("fx_reverse_swell",0)),.48*p.fx_reverse*p.fx_presence*vg),0,.72); c["reverb"]=_clamp(max(float(c.get("reverb",0)),.20*p.fx_space*p.fx_presence),0,.36); c["fx_rack_wet"]=_clamp(max(float(c.get("fx_rack_wet",0)),.44*p.fx_presence),0,.66); c["fx_width"]=_clamp(max(float(c.get("fx_width",0)),.62*p.fx_presence),0,.82); c["fx_echo_send"]=_clamp(max(float(c.get("fx_echo_send",0)),.24*p.fx_echo*p.fx_presence),0,.58)
        elif action=="MACRO_GROOVE_LIFT":
            c["perf_punch"]=_clamp(max(float(c.get("perf_punch",0)),.24+.18*p.action_density),0,.62); c["perf_energy"]=_clamp(max(float(c.get("perf_energy",0)),.22+.16*p.action_density),0,.58); c["drum_drive"]=_clamp(max(float(c.get("drum_drive",0)),.18+.16*p.action_density),0,.52); c["a_high_db"]=_clamp(float(c.get("a_high_db",0))+.16,-1.2,1.1)
        elif action=="MACRO_SPINBACK":
            self._fx_brake_seq+=1; c["fx_brake_seq"]=self._fx_brake_seq; c["fx_brake_mode"]=1.0; c["fx_brake_beats"]=1.0
            c["noise_riser"]*=.25; c["fx_snare_rush"]*=.25; c["fx_gate"]=0.0
            self._transition={"active":True,"name":"SPINBACK","a_role":"FALL","b_role":"HOLD","start_beat":int(beat),"end_beat":int(beat+1),"depth":1.0}
        elif action=="MACRO_BRAKE":
            self._fx_brake_seq+=1; c["fx_brake_seq"]=self._fx_brake_seq; c["fx_brake_mode"]=0.0; c["fx_brake_beats"]=2.0
            c["noise_riser"]*=.25; c["fx_snare_rush"]*=.25; c["fx_gate"]=0.0
            self._transition={"active":True,"name":"BRAKE","a_role":"FALL","b_role":"HOLD","start_beat":int(beat),"end_beat":int(beat+2),"depth":1.0}
        elif action=="MACRO_PUMP":
            self._pump_sync_seq+=1; c["fx_pump_sync_seq"]=self._pump_sync_seq; c["fx_pump_cycle"]=1.0
            self._schedule_automation_lane("fx_pump",float(c.get("fx_pump",0)),_clamp(.34+.22*strength,0,.62),beat,1.0)
            self._schedule_automation_lane("fx_pump",_clamp(.34+.22*strength,0,.62),0.0,beat+7.0,1.5,replace=False)
        elif action=="MACRO_BUILD":
            self._schedule_automation_lane("noise_riser",float(c.get("noise_riser",0)),_clamp(.55+.30*strength,0,.90)*p.fx_presence,beat,8.0,"exponential")
            self._schedule_automation_lane("fx_rush_accel",0.0,1.0,beat,8.0,"linear")
            self._schedule_automation_lane("fx_snare_rush",float(c.get("fx_snare_rush",0)),_clamp(.45+.25*strength,0,.80)*p.fx_presence,beat,8.0,"exponential")
            self._schedule_automation_lane("fx_bass_cut",float(c.get("fx_bass_cut",0)),_clamp(.55+.30*strength,0,.90),beat,6.5)
            self._transition={"active":True,"name":"QUANT BUILD","a_role":"RISE","b_role":"HOLD","start_beat":int(beat),"end_beat":int(beat+8),"depth":round(strength,3)}
        elif action=="MACRO_FLASHFWD":
            self._fx_brake_seq+=1; c["fx_brake_seq"]=self._fx_brake_seq
            c["fx_brake_mode"]=3.0; c["fx_brake_beats"]=2.0; c["fx_weave_offset_beats"]=16.0
            self._transition={"active":True,"name":"FLASH-FORWARD","a_role":"RISE","b_role":"HOLD","start_beat":int(beat),"end_beat":int(beat+2),"depth":.5}
        elif action=="MACRO_JUMPBACK":
            self._fx_brake_seq+=1; c["fx_brake_seq"]=self._fx_brake_seq
            c["fx_brake_mode"]=2.0; c["fx_brake_beats"]=4.0; c["fx_weave_offset_beats"]=4.0
            self._transition={"active":True,"name":"JUMPBACK","a_role":"HOLD","b_role":"HOLD","start_beat":int(beat),"end_beat":int(beat+4),"depth":.25}
        elif action=="MACRO_WEAVE":
            bpmn=max(65.0,float(a.get("bpm") or 120.0))
            self._fx_brake_seq+=1; c["fx_brake_seq"]=self._fx_brake_seq
            c["fx_brake_mode"]=4.0; c["fx_brake_beats"]=8.0
            c["fx_weave_offset_beats"]=_clamp(round(7.5*bpmn/60.0),4.0,32.0); c["fx_weave_cell_beats"]=2.0
            self._transition={"active":True,"name":"TIME INTERLEAVE","a_role":"HOLD","b_role":"HOLD","start_beat":int(beat),"end_beat":int(beat+8),"depth":.5}
        self._history.append({"beat":beat,"bar":beat//4,"kind":action,"value":round(float(score),3),"rationale":rationale,"time":time.time()})
        return c

    def _morph_profile(self, a: dict[str, Any]) -> dict[str, float]:
        p=self._policy
        future=dict(self._future_analysis or {})
        src=max(55.0,float(future.get("bpm") or a.get("bpm") or 100.0))
        drums=float(future.get("drums_activity") if future else a.get("drums_activity") or 0.0)
        energy=float(future.get("energy") if future else a.get("energy") or 0.0)
        if p.morph_target == "dnb":
            perceived=src*2.0 if src < 112.0 else float(p.target_feel_bpm or 174.0)
            perceived=_clamp(perceived,150.0,190.0)
            tempo_gap=_clamp(abs(float(p.target_feel_bpm or 174.0)-perceived)/55.0,0,1)
            drum_gap=_clamp((.72-drums)/.72,0,1)
            energy_gap=_clamp((.66-energy)/.66,0,1)
            mismatch=_clamp(.28+.34*drum_gap+.22*energy_gap+.16*tempo_gap,0,1)
            authority_hi=_clamp((float(getattr(p,"agentic_authority",1.0))-1.0)/2.60,0,1)
            strength=_clamp(.52+.38*mismatch+.10*p.creativity+.12*authority_hi,0,1)
        else:
            perceived=src; mismatch=0.0; strength=.0
        return {"source_bpm":src,"perceived_bpm":perceived,"mismatch":mismatch,"strength":strength}

    def _apply_morph_scene(self, a: dict[str, Any], bar: int, phase: float, c: dict[str, Any], can_execute: bool) -> dict[str, Any]:
        p=self._policy
        if p.morph_target == "native" or not self._lookahead_ready or not can_execute:
            return c
        prof=self._morph_profile(a); total=max(12,int(p.morph_bars)); rel=max(0.0,float(bar-self._scene_start_bar)+float(phase)); progress=_clamp(rel/float(total),0,1)
        # strength is a one-shot articulation parameter.  Keep the sequence trigger
        # authoritative and clear the level on subsequent planner ticks so the
        # mixer headroom trim is not held down after the impact tail is finished.
        c["impact_strength"]=0.0
        strength=float(prof["strength"]); vocal=float(a.get("vocal_activity") or 0.0); drums=float(a.get("drums_activity") or 0.0)
        vocal_guard=1.0-.58*_clamp((vocal-.42)/.48,0,1)
        fxp=_clamp(float(p.fx_presence)*(0.78+0.22*strength),0,1)
        # Smooth destination curves. The first half mainly improves source drums; the
        # obvious genre cue arrives later so unrelated material does not snap genres.
        if progress < .25:
            stage="FOUNDATION"; q=_smoothstep(progress/.25)
            drive=.12+.30*q; double=.02+.14*q; punch=.18+.28*q; energy=.10+.18*q; tight=.25+.22*q; clarity=.10+.16*q; air=.04+.06*q; riser=0.0; reverse=.02+.06*q; rush=0.0; filt=0.0; echo=0.0; space=.02+.03*q; rack=.04+.04*q; esend=0.0; efb=.16; gate=.0; gdiv=2.0; width=.10*q; duck=.55
        elif progress < .55:
            stage="PROPULSION"; q=_smoothstep((progress-.25)/.30)
            drive=.42+.20*q; double=.16+.38*q; punch=.46+.15*q; energy=.28+.25*q; tight=.47+.08*q; clarity=.26+.10*q; air=.10+.08*q; riser=.03*q; reverse=.08+.16*q; rush=.05+.24*q; filt=.02+.05*q; echo=.02*q; space=.04+.04*q; rack=.12+.18*q; esend=.02+.08*q; efb=.18+.06*q; gate=.04+.14*q; gdiv=2.0+2.0*q; width=.18+.20*q; duck=.62
        elif progress < .78:
            stage="BUILD"; q=_smoothstep((progress-.55)/.23)
            drive=.62+.08*q; double=.54+.14*q; punch=.61+.06*q; energy=.53+.12*q; tight=.55; clarity=.36; air=.18+.08*q; riser=(.18+.78*q)*strength*vocal_guard; reverse=(.18+.58*q)*vocal_guard; rush=(.20+.72*q)*vocal_guard; filt=(.07+.15*q)*vocal_guard; echo=(.025+.12*q)*vocal_guard; space=(.05+.12*q)*vocal_guard; rack=.30+.34*q; esend=(.06+.36*q)*vocal_guard; efb=.22+.22*q; gate=(.10+.28*q)*vocal_guard; gdiv=4.0 if q>.48 else 2.0; width=.38+.30*q; duck=.78
        elif progress < .86:
            stage="RELEASE"; q=_smoothstep((progress-.78)/.08)
            drive=.70; double=.68-.08*q; punch=.68; energy=.67; tight=.58; clarity=.38; air=.25; riser=(.76*(1-q))*strength*vocal_guard; reverse=(.52*(1-q))*vocal_guard; rush=(.66*(1-q))*vocal_guard; filt=(.20*(1-q))*vocal_guard; echo=.11*(1-q)*vocal_guard; space=.10*(1-q)*vocal_guard; rack=.64-.14*q; esend=.32*(1-q)*vocal_guard; efb=.44-.06*q; gate=.34*(1-q)*vocal_guard; gdiv=4.0; width=.68-.10*q; duck=.86
        else:
            stage="DNB FEEL LOCK"; q=_smoothstep((progress-.86)/.14)
            drive=.70-.06*q; double=.60-.08*q; punch=.68-.06*q; energy=.66-.10*q; tight=.58-.04*q; clarity=.38-.04*q; air=.24-.06*q; riser=0.0; reverse=0.0; rush=.10*(1-q); filt=0.0; echo=.025*(1-q); space=.025*(1-q); rack=.26-.16*q; esend=0.0; efb=.34-.14*q; gate=.08*(1-q); gdiv=4.0; width=.44-.22*q; duck=.66

        # Creativity and source mismatch scale the performance layer, but safety values
        # never exceed the bounded realtime engine control surface.
        fx_scale=_clamp(.72+.28*strength,0,1)
        c["drum_drive"]=_clamp(drive*fx_scale*(.88+.12*drums),0,1)
        c["doubletime"]=_clamp(double*fx_scale*vocal_guard,0,.82)
        c["perf_punch"]=_clamp(punch*fx_scale,0,.82); c["perf_energy"]=_clamp(energy*fx_scale,0,.78)
        c["bass_tighten"]=_clamp(tight,0,.75); c["perf_clarity"]=_clamp(clarity,0,.62); c["perf_air"]=_clamp(air*vocal_guard,0,.48)
        c["noise_riser"]=_clamp(riser*p.fx_riser*fxp,0,.92); c["fx_reverse_swell"]=_clamp(reverse*p.fx_reverse*fxp,0,.88); c["fx_snare_rush"]=_clamp(rush*p.fx_snare_rush*fxp,0,.92)
        c["a_filter"]=_clamp(filt,0,.24); c["echo"]=_clamp(max(float(c.get("echo",0))*0.70,echo*p.fx_echo*fxp),0,.32); c["reverb"]=_clamp(max(float(c.get("reverb",0))*0.78,space*p.fx_space*fxp),0,.34)
        # v31.5 Performance FX Rack: controller-style macro automation.  SEND can
        # fall to zero during RELEASE while the return/feedback remains active,
        # producing a natural post-fader echo-out rather than a chopped DSP tail.
        c["fx_rack_wet"]=_clamp(rack*fxp,0,.72); c["fx_echo_send"]=_clamp(esend*p.fx_echo*fxp,0,.82); c["fx_echo_feedback"]=_clamp(efb*(.72+.28*p.fx_echo),0,.56)
        c["fx_echo_beats"]=.50; c["fx_gate"]=_clamp(gate*fxp*(.62+.58*p.fx_phrase_accents),0,.50); c["fx_gate_div"]=_clamp(gdiv,1,8); c["fx_width"]=_clamp(width*fxp*(.70+.42*p.fx_space),0,.82); c["fx_duck"]=_clamp(duck,0,1)

        # A half-beat source roll is used once, late in the build and only when the
        # foreground vocal is quiet. It creates acceleration from the song itself.
        if stage=="BUILD" and progress>.69 and not self._scene_loop_fired and vocal<.40 and drums>.28:
            self._loop_capture_seq+=1; c["loop_beats"]=.50; c["loop_capture_seq"]=self._loop_capture_seq; c["crossfader"]=_clamp(.12+.12*strength,0,.26)
            self._scene_loop_fired=True
            self._history.append({"bar":bar,"kind":"MORPH_HALF_BEAT_ROLL","value":round(strength,3),"rationale":"source-derived acceleration; vocal guard passed","time":time.time()})
        # One impact only, after the riser has begun clearing.
        if stage in {"RELEASE","DNB FEEL LOCK"} and not self._scene_impact_fired:
            c["impact_strength"]=_clamp((.70+.30*strength)*p.fx_impact*fxp,0,1); self._impact_seq+=1; c["impact_seq"]=self._impact_seq; c["noise_riser"]=0.0; c["fx_reverse_swell"]=0.0; c["fx_snare_rush"]=0.0; c["a_filter"]=0.0; c["fx_echo_send"]=0.0; c["fx_gate"]=0.0; c["fx_rack_wet"]=_clamp(max(float(c.get("fx_rack_wet",0)),.48*fxp),0,.62)
            self._scene_impact_fired=True
            self._history.append({"bar":bar,"kind":"MORPH_IMPACT_RELEASE","value":round(strength,3),"rationale":"single bounded downbeat impact after staged build","time":time.time()})

        self._scene={"name":"PERFORMANCE-RACK DNB MORPH","active":True,"stage":stage,"progress":round(progress,3),"target":"dnb","source_bpm":round(prof["source_bpm"],1),"perceived_bpm":round(prof["perceived_bpm"],1),"mismatch":round(prof["mismatch"],3),"strength":round(strength,3),"fx_profile":p.fx_profile,"fx_presence":round(p.fx_presence,3),"lookahead_sec":round(float(p.lookahead_sec),1)}
        return c

    def _musical_motion_layer(self, c: dict[str, Any], a: dict[str, Any], beat: int, phase: float, in_transition: bool, mixer: dict[str, Any] | None = None) -> dict[str, Any]:
        """Phrase-locked continuous deck motion (v31.10).

        Between discrete gestures the deck used to sit frozen at neutral, which
        both looked dead in the UI and sounded static.  Real DJs keep constant
        low-amplitude EQ/filter motion; the mix literature shows EQ moves are the
        dominant transition/performance primitive.  This layer breathes the deck
        controls with two phrase-locked sinusoids.  It never fights an automation
        lane, an active transition owns its keys, and every value stays well
        inside the bounded controller surface.
        """
        p=self._policy
        pos=self._mi.phrase.position(beat)
        bf=float(beat)+_clamp(phase,0,1)
        L=max(4,int(pos.get("phrase_beats") or 16))
        prog=((bf-float(self._mi.phrase.anchor_beat))%L)/L
        breath=math.sin(2.0*math.pi*prog)                 # one cycle per phrase
        sway=math.sin(2.0*math.pi*((bf%8.0)/8.0))         # two-bar sway
        vocal=_clamp(float(a.get("vocal_activity") or 0.0),0,1)
        energy=_clamp(float(a.get("energy") or 0.0),0,1)
        eqm=_clamp(float(p.eq_motion),0,1); fim=_clamp(float(p.filter_motion),0,1)
        depth=(.45+.55*energy)
        blend=.16   # per-tick approach rate; engine-side smoothing does the rest
        def approach(key: str, target: float, lo: float, hi: float):
            if self._lane_active(key): return
            cur=float(c.get(key,0.0))
            c[key]=_clamp(cur+(target-cur)*blend,lo,hi)
        # Deck A: high shelf breathes with the phrase, mids dip away from vocals,
        # low sway stays tiny and disappears while a foreground vocal is present.
        approach("a_high_db",1.05*eqm*depth*breath,-2.2,2.2)
        approach("a_mid_db",-0.70*eqm*depth*max(0.0,breath)*(1.0-.75*vocal),-1.6,1.2)
        if not in_transition and not self._lane_active("a_filter") and abs(float(c.get("a_filter",0)))<.09:
            approach("a_filter",.055*fim*sway*(1.0-.5*vocal),-.12,.12)
        if not in_transition and not self._lane_active("a_low_db"):
            approach("a_low_db",float(c.get("a_low_db",0))+.38*eqm*sway*(1.0-vocal)*.5,-2.4,1.2)
        # Toward the phrase boundary the deck leans forward slightly (a lift a
        # listener feels more than hears); it relaxes right after the downbeat.
        lean=_clamp((prog-.75)/.25,0,1)
        approach("perf_energy",max(float(c.get("perf_energy",0))*.90,.22*lean*depth),0,.60)
        # Deck B breathes in counter-phase whenever a loop is resident.
        mixer=mixer if mixer is not None else (self.engine.status().get("agentic_mixer") or {})
        if mixer.get("loop_ready"):
            approach("b_high_db",-0.9*eqm*depth*breath,-2.0,2.0)
            if float(c.get("b_layer",0))>.05 and not self._lane_active("b_layer"):
                approach("b_layer",float(c.get("b_layer",0))+.014*sway,0,.40)
            if not self._lane_active("b_texture"):
                approach("b_texture",float(c.get("b_texture",0))+.05*breath,-1.0,1.0)
        return c

    # v31.12 FullRemix section targets.  base is scaled by remix intensity; every
    # value stays far inside the bounded engine control surface.  "redrum" is a
    # multiplier on the intensity-derived base level; "pattern" -1 keeps the
    # prompt/genre pattern, otherwise it overrides for the section.
    # v31.14 Arranger 2.0: eight sections; "bass"/"stab" drive the key-following
    # synth layers, "rvar"/"fillv" the redrum pattern-DNA depth.  Every value is
    # a CENTER — each section INSTANCE is jittered ±25 % so no two GROOVE+
    # passages are ever the same performance.
    _REMIX_SECTIONS = {
        "GROOVE+":  {"redrum":1.00,"pattern":-1,"pump":.30,"b_layer":.30,"slicer":.22,"slice_mode":1.0,"b_fx":.35,"b_texture":.45,"b_transient":.35,"doubletime":.12,"drum_drive":.22,"bass_cut":.16,"riser":0.0,"bass":.42,"stab":.14,"rvar":.55,"fillv":.60,"beats":16},
        "ROLL":     {"redrum":.80,"pattern":-1,"pump":.24,"b_layer":.34,"slicer":.58,"slice_mode":2.0,"b_fx":.55,"b_texture":.62,"b_transient":.52,"doubletime":.10,"drum_drive":.26,"bass_cut":0.0,"riser":0.0,"bass":.30,"stab":.10,"rvar":.70,"fillv":.75,"beats":8},
        "STRIP":    {"redrum":.25,"pattern":-1,"pump":.08,"b_layer":.16,"slicer":.15,"slice_mode":3.0,"b_fx":.40,"b_texture":.30,"b_transient":.30,"doubletime":0.0,"drum_drive":.14,"bass_cut":.78,"riser":.26,"bass":.12,"stab":0.0,"rvar":.30,"fillv":.90,"beats":8},
        "DROP+":    {"redrum":1.00,"pattern":-1,"pump":.40,"b_layer":.18,"slicer":.12,"slice_mode":1.0,"b_fx":.30,"b_texture":.40,"b_transient":.30,"doubletime":.18,"drum_drive":.32,"bass_cut":.20,"riser":0.0,"bass":.55,"stab":.22,"rvar":.45,"fillv":.55,"beats":16},
        "HALF":     {"redrum":.85,"pattern":1,"pump":.18,"b_layer":.24,"slicer":.06,"slice_mode":0.0,"b_fx":.24,"b_texture":-.35,"b_transient":.10,"doubletime":0.0,"drum_drive":.12,"bass_cut":.10,"riser":0.0,"bass":.38,"stab":.08,"rvar":.40,"fillv":.45,"beats":8},
        "BASSLINE": {"redrum":.55,"pattern":-1,"pump":.26,"b_layer":.10,"slicer":.05,"slice_mode":0.0,"b_fx":.20,"b_texture":.10,"b_transient":.15,"doubletime":0.0,"drum_drive":.12,"bass_cut":.62,"riser":0.0,"bass":.75,"stab":.10,"rvar":.35,"fillv":.50,"beats":8},
        "PERC":     {"redrum":.20,"pattern":-1,"pump":.10,"b_layer":.30,"slicer":.35,"slice_mode":1.0,"b_fx":.45,"b_texture":.78,"b_transient":.60,"doubletime":.24,"drum_drive":.28,"bass_cut":.30,"riser":0.0,"bass":0.0,"stab":0.0,"rvar":.60,"fillv":.55,"beats":8},
        "STAB_GROOVE":{"redrum":.90,"pattern":-1,"pump":.30,"b_layer":.20,"slicer":.14,"slice_mode":1.0,"b_fx":.30,"b_texture":.30,"b_transient":.25,"doubletime":.08,"drum_drive":.20,"bass_cut":.14,"riser":0.0,"bass":.45,"stab":.48,"rvar":.50,"fillv":.55,"beats":12},
    }

    _RD_TEMPLATES=(
        ("FLOOR", [1,0,0,0, 1,0,0,0, 1,0,0,0, 1,0,0,.3]),
        ("HALF",  [1,0,0,0, 0,0,0,0, 0,0,.55,0, 0,0,0,0]),
        ("BREAK", [1,0,0,0, 0,0,0,.65, 0,0,1,0, 0,.45,0,0]),
        ("UPBEAT",[0,0,1,0, 0,0,1,0, 0,0,1,0, 0,0,1,0]),
    )

    def _pattern_from_source(self, a: dict[str, Any]) -> int:
        """v31.18: pick the re-drum pattern that agrees best with the record's own kick
        accents (cosine similarity on the downbeat-aligned 16-step profile)."""
        src=a.get("src_kick") or []
        try:
            v=np.asarray([float(q) for q in src],dtype=np.float32)
        except Exception:
            v=np.zeros(0,dtype=np.float32)
        if v.size!=16 or float(np.max(v))<0.3 or float(np.sum(v>0.5))<2:
            return int(getattr(self._policy,"redrum_pattern",0) or 0)
        best=0; bests=-1.0
        for i,(_,tpl) in enumerate(self._RD_TEMPLATES):
            t=np.asarray(tpl,dtype=np.float32)
            sim=float(np.dot(v,t)/((np.linalg.norm(v)*np.linalg.norm(t))+1e-9))
            if sim>bests: bests=sim; best=i
        return int(best)

    def _remix_arranger(self, c: dict[str, Any], a: dict[str, Any], beat: int, mixer: dict[str, Any], now: float) -> dict[str, Any]:
        """FullRemix arrangement brain (v31.12).

        Remix production practice: a NEW drum groove plays under the source,
        sidechain glue keyed from the kick, fragments of the source rearranged
        while the loops keep running, sections duplicated/muted into an energy
        arc with one strong drop.  This method holds those layers for whole
        phrase-aligned sections instead of firing decaying two-beat gestures —
        at creativity max the result is unmistakably a remix, not FX seasoning.
        """
        p=self._policy
        ri=_clamp(float(getattr(p,"remix_intensity",0.0)),0,1)
        if ri<=.05 or not self._audible_started:
            if self._remix.get("enabled"):
                # Graceful exit: wind the remix layers down over one bar.
                for key in ("fx_redrum","fx_pump","b_slicer_mix","b_layer","fx_bass_synth","fx_stab","fx_redrum_var","fx_redrum_fill"):
                    self._schedule_automation_lane(key,float(c.get(key,0)),0.0,beat,4.0)
                self._remix={"enabled":False,"section":"—","until_beat":0,"last_capture_beat":self._remix.get("last_capture_beat",-999),"entered_beat":0}
            return c
        vocal=_clamp(float(a.get("vocal_activity") or 0.0),0,1)
        self._remix["enabled"]=True
        # Advance the section cycle on phrase-aligned boundaries.
        if beat>=int(self._remix.get("until_beat",0)):
            last=str(self._remix.get("section") or "—")
            readiness=self._mi.tension.release_readiness()
            big_pen=self._mi.governor.big_gesture_penalty(beat,float(getattr(p,"agentic_authority",1.0)))
            rng=random.Random(beat//4*977+int(self._regen)*31+int((now*7.0))%997)
            def pick(options):
                # options: list of (name, weight)
                # v31.18: rhythmic sections need a record with a real grid; on beat-less
                # material the arrangement leans on STRIP/HALF textures instead.
                _cl=float(self._clarity_s) if self._clarity_s>=0 else 1.0
                _wmap={"GROOVE+":.30+.70*_cl,"DROP+":_cl,"STAB_GROOVE":_cl,"BASSLINE":.40+.60*_cl,"PERC":.20+.80*_cl,"ROLL":_cl,"HALF":.50+.50*_cl,"STRIP":1.0}
                options=[(nm,w*float(_wmap.get(nm,1.0))) for nm,w in options]
                tot=sum(w for _,w in options); r=rng.random()*max(1e-9,tot); acc=0.0
                for name,w in options:
                    acc+=w
                    if r<=acc: return name
                return options[-1][0]
            if last=="STRIP":
                nxt="DROP+"
            elif last=="DROP+":
                nxt=pick([("GROOVE+",.40),("HALF",.25 if self._mi.arc.state=="FALL" else .12),("STAB_GROOVE",.28),("PERC",.15)])
            elif last=="ROLL":
                nxt="STRIP" if (readiness>.42 and big_pen<.45) else pick([("GROOVE+",.5),("PERC",.3),("BASSLINE",.2)])
            elif last=="HALF":
                nxt=pick([("GROOVE+",.6),("BASSLINE",.4)])
            elif last=="BASSLINE":
                nxt="STRIP" if (readiness>.50 and big_pen<.35) else "GROOVE+"
            elif last=="PERC":
                nxt=pick([("GROOVE+",.6),("BASSLINE",.4)])
            elif last=="STAB_GROOVE":
                nxt=pick([("GROOVE+",.5),("ROLL",.5)])
            elif last=="GROOVE+":
                if vocal>.68: nxt=pick([("HALF",.5),("GROOVE+",.35),("BASSLINE",.15)])
                elif readiness>.55 and big_pen<.35: nxt="STRIP"
                else: nxt=pick([("ROLL",.34),("STAB_GROOVE",.20),("BASSLINE",.16),("PERC",.12),("GROOVE+",.18)])
            else:
                nxt="GROOVE+"
            # v31.16 SongMind: a STRIP is a promise of a landing -> only when the
            # legality engine offers one 6..18 beats ahead; and section ends snap
            # to the best legal landing when one is close.
            legal=self._mi.legality.last or {}
            land_beats=[int(w.get("beat")) for w in ((legal.get("windows") or {}).get("LANDING") or [])]
            if nxt=="STRIP":
                near=[b for b in land_beats if 6<=b-beat<=18]
                if not near:
                    nxt="GROOVE+"
            spec=self._REMIX_SECTIONS[nxt]
            dur=int(spec["beats"])
            until=self._mi.phrase.quantize_to_boundary(beat+dur,max_ahead=8)
            if nxt=="STRIP" and near:
                until=min(near)
            else:
                cand=[b for b in land_beats if abs(b-(beat+dur))<=4 and b>beat+3]
                if cand:
                    until=min(cand,key=lambda b:abs(b-(beat+dur)))
            # v31.14 per-instance DNA: every section pass gets its own jittered
            # target set + pattern choices, and consecutive instances of the same
            # section are forced to differ.  This is the 10-20x variance ask:
            # centers are musical, instances are alive.
            def roll_targets():
                tg={}
                for k,v in spec.items():
                    if k in ("pattern","slice_mode","beats"): continue
                    tg[k]=float(v)*(0.75+0.50*rng.random()) if isinstance(v,(int,float)) else v
                base_pat=int(getattr(p,"redrum_pattern",0)) if int(spec["pattern"])<0 else int(spec["pattern"])
                if rng.random()<.25 and base_pat in (0,3): base_pat=3-base_pat
                tg["pattern"]=base_pat
                tg["slice_mode"]=float(spec["slice_mode"]) if rng.random()>.30 else float(rng.choice((1.0,2.0,4.0)))
                if nxt=="BASSLINE": tg["bass_pat"]=float(rng.choice((1,2,3)))
                elif nxt in ("DROP+","STAB_GROOVE"): tg["bass_pat"]=float(rng.choice((0,1)))
                else: tg["bass_pat"]=float(rng.choice((0,0,1,2)))
                tg["stab_pat"]=float(rng.choice((0,1)))
                return tg
            tg=roll_targets()
            sig=(nxt,round(tg.get("redrum",0),2),int(tg.get("bass_pat",0)),int(tg.get("pattern",0)))
            if sig==self._remix.get("last_sig") :
                tg=roll_targets()
            self._remix["targets"]=tg
            self._remix["last_sig"]=(nxt,round(tg.get("redrum",0),2),int(tg.get("bass_pat",0)),int(tg.get("pattern",0)))
            self._remix.update({"section":nxt,"until_beat":int(max(beat+4,until)),"entered_beat":int(beat)})
            # Section-entry one-shots: pump re-locks to the section downbeat and
            # the resident Deck-B motif is captured if the deck is empty.
            if nxt in ("GROOVE+","DROP+","HALF"):
                self._pump_sync_seq+=1; c["fx_pump_sync_seq"]=self._pump_sync_seq; c["fx_pump_cycle"]=1.0
            wants_b=float(spec["b_layer"])>.10
            if wants_b and not mixer.get("loop_ready") and vocal<.75 and beat-int(self._remix.get("last_capture_beat",-999))>=8:
                self._loop_capture_seq+=1
                c["loop_beats"]=4.0 if nxt in ("GROOVE+","HALF") else 2.0
                c["loop_capture_seq"]=self._loop_capture_seq
                self._remix["last_capture_beat"]=int(beat)
            if nxt=="DROP+":
                self._impact_seq+=1; c["impact_seq"]=self._impact_seq
                c["impact_strength"]=_clamp(.5+.4*ri,0,1)
                self._mi.notify_event("DROP",beat,now)
            self._history.append({"beat":int(beat),"bar":int(beat)//4,"kind":f"REMIX_{nxt.replace('+','_PLUS')}","value":round(ri,3),
                                  "rationale":f"FullRemix section {last} -> {nxt} · phrase-aligned hold until beat {self._remix['until_beat']}","time":time.time()})
        # Continuously hold the section INSTANCE's jittered state (blend-approach;
        # lanes, events and manual overrides all still win their own keys).
        tg=self._remix.get("targets") or None
        if tg is None or str(self._remix.get("section")) not in self._REMIX_SECTIONS:
            return c
        base=(.30+.55*ri)
        # v31.14 macro-arc: a slow set-level intensity wave (~170 s period) so a
        # 5-minute performance breathes at the arrangement scale too.
        arcw=1.0+0.25*math.sin(2.0*math.pi*((now-float(getattr(self,"_set_start_t",now)))/170.0))*ri
        vg=1.0-.42*_clamp((vocal-.62)/.30,0,1)
        loop_ready=bool(mixer.get("loop_ready"))
        def approach(key: str, target: float, lo: float, hi: float, blend: float=.20):
            if self._lane_active(key): return
            cur=float(c.get(key,0.0))
            c[key]=_clamp(cur+(target-cur)*blend,lo,hi)
        approach("fx_redrum",base*float(tg.get("redrum",0))*arcw,0,1)
        _pat=int(tg.get("pattern",0))
        c["fx_redrum_pattern"]=float(_pat if _pat>=0 else self._pattern_from_source(a))
        approach("fx_redrum_var",float(tg.get("rvar",.5))*(.5+.5*ri),0,1,blend=.25)
        approach("fx_redrum_fill",float(tg.get("fillv",.5))*(.5+.5*ri),0,1,blend=.25)
        approach("fx_pump",float(tg.get("pump",0))*(.55+.45*ri)*arcw,0,.62)
        approach("fx_bass_cut",float(tg.get("bass_cut",0)),0,.92,blend=.14)
        if float(tg.get("riser",0))>0:
            approach("noise_riser",float(tg.get("riser",0))*p.fx_presence,0,.60)
        approach("doubletime",float(tg.get("doubletime",0))*(.6+.4*ri),0,.60)
        approach("drum_drive",float(tg.get("drum_drive",0))*(.6+.4*ri),0,.55)
        # Key-following synth layers (engine additionally gates them by key
        # confidence, so an uncertain key can never smear a wrong bassline).
        approach("fx_bass_synth",base*float(tg.get("bass",0))*arcw,0,.85)
        c["fx_bass_pattern"]=float(int(tg.get("bass_pat",0)))
        approach("fx_stab",base*float(tg.get("stab",0))*arcw,0,.70)
        c["fx_stab_pattern"]=float(int(tg.get("stab_pat",0)))
        if loop_ready:
            approach("b_layer",float(tg.get("b_layer",0))*vg*(.55+.45*ri)*arcw,0,.44)
            approach("b_slicer_mix",float(tg.get("slicer",0))*vg,0,.72)
            c["b_slice_mode"]=float(tg.get("slice_mode",0.0)) if float(tg.get("slicer",0))>.10 else float(c.get("b_slice_mode",0.0))
            approach("b_fx",float(tg.get("b_fx",0)),0,.80)
            approach("b_texture",float(tg.get("b_texture",0)),-1,1,blend=.12)
            approach("b_transient",float(tg.get("b_transient",0)),0,.80,blend=.12)
        return c

    def _apply_agent_logic(self, a: dict[str, Any], now: float):
        with self._lock:
            if self._manual or self._locked: return
            p=self._policy; c=dict(self._controller); bpm=max(65.0,float(a.get("bpm") or c.get("bpm") or 100.0)); c["bpm"]=bpm
            # Pass a compact description of the *next audible phrase* down to the
            # realtime Deck-B capture selector.  We blend the first two future
            # bar descriptors, weighted toward the later bar, because Deck B is
            # normally heard across the handoff rather than only at capture time.
            segs=list(self._future_segments or [])
            if segs:
                s0=segs[0]; s1=segs[1] if len(segs)>1 else s0
                def blend_vec(name,n):
                    v0=np.asarray(s0.get(name) or [],dtype=np.float32); v1=np.asarray(s1.get(name) or [],dtype=np.float32)
                    if v0.size!=n or v1.size!=n: return []
                    v=.35*v0+.65*v1
                    if name=="chroma":
                        sm=float(np.sum(v)); v=v/sm if sm>1e-8 else np.ones(n,dtype=np.float32)/float(n)
                    elif name=="rhythm":
                        mx=float(np.max(v)); v=v/mx if mx>1e-8 else v
                    return [float(x) for x in v.tolist()]
                c["future_chroma"]=blend_vec("chroma",12)
                c["future_rhythm"]=blend_vec("rhythm",16)
                c["future_tonal_strength"]=float(.35*float(s0.get("tonal_strength") or 0)+.65*float(s1.get("tonal_strength") or 0))
                c["future_energy"]=float(.35*float(s0.get("energy") or 0)+.65*float(s1.get("energy") or 0))
                c["future_vocal"]=float(.35*float(s0.get("vocal") or 0)+.65*float(s1.get("vocal") or 0))
                c["future_drums"]=float(.35*float(s0.get("drums") or 0)+.65*float(s1.get("drums") or 0))
            rt=self.engine.status(); render_frames=int(rt.get("render_source_frame_index") or 0)
            if render_frames>0 and not self._audible_started:
                self._audible_started=True; self._beat_anchor_t=0.0; self._scene_start_bar=0
            if self._audible_started:
                beat,phase=self._update_beat_clock(a,now)
            else:
                beat=0; phase=0.0; self._beat_index=0; self._beat_phase=0.0; self._bar_index=0
            bar=int(self._bar_index)

            # v31.10 Musical Intelligence: phrase clock, tension field, energy arc,
            # stabilized key and gesture pacing all observe every planner tick.
            self._mi.observe(beat=beat,controller=c,analysis=a,segments=list(self._future_segments),
                             policy_direction=str(p.energy_direction),now=now)
            in_transition=bool(self._transition.get("active"))
            if in_transition and beat>int(self._transition.get("end_beat",0))+1:
                self._transition={"active":False,"name":"","a_role":"HOLD","b_role":"HOLD","start_beat":0,"end_beat":0}
                in_transition=False

            # v31.13 GridLock: hand the engine our audible beat anchor + tracker
            # confidence so every rhythmic FX (hats/rush/redrum/pump) locks its
            # accents to the song's real beats instead of a free-running clock.
            c["grid_anchor_t"]=float(self._beat_anchor_t) if (self._audible_started and self._beat_anchor_t>0.0) else 0.0
            c["grid_conf"]=_clamp(float(a.get("bpm_confidence") or 0.0),0,1)
            # v31.18: frame-accurate downbeat anchor + groove clarity for the mixer
            c["grid_anchor_frame"]=float(self._grid_anchor_frame) if (self._audible_started and self._grid_anchor_frame>0.0) else 0.0
            gc=a.get("groove_clarity")
            if gc is not None:
                gcv=_clamp(float(gc),0,1)
                self._clarity_s=gcv if self._clarity_s<0 else self._clarity_s+(gcv-self._clarity_s)*0.35
            c["fx_groove_clarity"]=_clamp(self._clarity_s,0,1) if self._clarity_s>=0 else 1.0
            c["fx_swing"]=_clamp(float(getattr(p,"swing_amount",0.0)),0,.6)
            c["fx_hat_style"]=float(int(getattr(p,"hat_style",0)))
            c["fx_hat_var"]=_clamp(float(getattr(p,"hat_var",.5)),0,1)
            c["fx_hat_fill"]=_clamp(.35+.55*float(getattr(p,"hat_var",.5)),0,1)
            # v31.14: the key-following synth layers get the stabilized root and a
            # confidence derived from key stability (modulation evidence lowers it).
            skey=str(self._mi.harmony.stable_key or "—")
            if skey!="—":
                sroot=skey[:-1] if skey.endswith("m") else skey
                if sroot in KEY_NAMES:
                    c["fx_bass_root"]=float(KEY_NAMES.index(sroot))
                    c["fx_bass_conf"]=_clamp(1.0-float(self._mi.harmony.modulation),0,1)
            else:
                c["fx_bass_conf"]=0.0

            # Deterministic safety servo and gentle release of one-shot performance controls.
            bass=float(a.get("bass_activity") or 0); safe_low=-(max(0.0,bass-.70))*1.8*p.low_end_protection
            if not (in_transition or self._lane_active("a_low_db")):
                c["a_low_db"]=.88*float(c.get("a_low_db",0))+.12*safe_low
            authority_hi=_clamp((float(getattr(p,"agentic_authority",1.0))-1.0)/2.60,0,1)
            c["echo"]*=.94+.035*authority_hi; c["reverb"]*=.97+.020*authority_hi; c["impact_strength"]=0.0
            base_decay={"noise_riser":.965,"fx_reverse_swell":.965,"fx_snare_rush":.950,"fx_echo_send":.940,"fx_gate":.940,"fx_rack_wet":.975,"fx_width":.980}
            max_decay={"noise_riser":.988,"fx_reverse_swell":.988,"fx_snare_rush":.978,"fx_echo_send":.978,"fx_gate":.972,"fx_rack_wet":.993,"fx_width":.995}
            for k in base_decay:
                if self._lane_active(k): continue
                decay=base_decay[k]+(max_decay[k]-base_decay[k])*authority_hi
                c[k]=float(c.get(k,0))*decay

            # v31.10 phrase-locked deck breathing: the visible DJ deck is alive at
            # all times (EQ-first motion, per the DJ-mix literature) instead of
            # sitting at zero between discrete gestures.  Amplitudes stay small,
            # vocal-aware and always inside the bounded controller surface.
            if self._audible_started:
                c=self._musical_motion_layer(c,a,beat,phase,in_transition,rt.get("agentic_mixer") or {})
                # v31.12 FullRemix arrangement layer: persistent, phrase-aligned
                # remix sections at high creativity (redrum + resident Deck B +
                # pump + slicer).  Runs under the discrete event system.
                c=self._remix_arranger(c,a,beat,rt.get("agentic_mixer") or {},now)

            can_execute=self._autonomy=="autopilot" or self._approved
            # Continuous genre-morph bed no longer vetoes creative actions. It is a
            # bed underneath the same beat-locked concert action queue.
            if p.morph_target!="native":
                c=self._apply_morph_scene(a,bar,phase,c,can_execute)

            if can_execute and self._audible_started and beat!=self._last_action_beat and phase<.36:
                due=[q for q in self._performance_queue if int(q.get("beat",999999))<=beat]
                if due:
                    ev=sorted(due,key=lambda q:(int(q.get("beat",0)),-float(q.get("score",0))))[0]
                    action=str(ev.get("action") or "NO_ACTION")
                    family=str(ev.get("family") or "")
                    destructive=action in {"MACRO_ROLL_ACCEL","MACRO_ROLL_HALF","MACRO_ROLL_QUARTER","MACRO_TRANS_BUILD","MACRO_DRUM_FILL","ROLL_1","ROLL_2","FX_TRANS_GATE"} or family in {"ROLL_ACCEL","ROLL_HALF","ROLL_QUARTER","TRANS_GATE","DRUM_FILL"}
                    vocal_limit=.72+.10*_clamp((float(getattr(p,"agentic_authority",1.0))-1.0)/2.60,0,1)
                    if destructive and float(a.get("vocal_activity") or 0)>vocal_limit:
                        ev["beat"]=beat+2; ev["bar"]=(beat+2)//4; ev["rationale"]=str(ev.get("rationale") or "")+" · postponed 2 beats by vocal guard"
                    else:
                        self._last_action_beat=beat; self._last_action_bar=bar
                        if action.startswith("PARAM_"):
                            c=self._execute_parameterized_event(ev,a,beat,c)
                        elif action.startswith("MACRO_"):
                            c=self._execute_concert_macro(action,a,beat,c,float(ev.get("score") or .5),str(ev.get("rationale") or ""))
                        else:
                            c=self._execute_neural_action({"action":action,"score":ev.get("score",0),"rationale":ev.get("rationale","")},a,bar,c)
                        self._mi.notify_event(str(ev.get("family") or action),beat,now)
                        try: self._performance_queue.remove(ev)
                        except ValueError: pass

            mixer=rt.get("agentic_mixer") or {}
            if mixer.get("loop_ready") and float(c.get("crossfader",0))>.05 and not self._transition.get("active"):
                beat_sec=60.0/bpm; age=float(mixer.get("loop_age_sec") or 0)
                if age>max(.55,beat_sec*2.2 if p.morph_target=="dnb" else beat_sec*3.8):
                    c["crossfader"]*=.58
                    if c["crossfader"]<.06:
                        self._loop_release_seq+=1; c["loop_release_seq"]=self._loop_release_seq; c["crossfader"]=0.0
            # v31.19 DrumMind: the AI drummer (record-matched kit + GMD-trained pattern model)
            self._drum_tick(c,a,beat,bar,bpm,now)
            # v31.21 SynthWeave: Stable Audio synth / melody on the captured loop
            self._synth_tick(c,a,rt,beat,bar,bpm,now)
            c=self._apply_overrides_locked(c,now)
            self._controller=c
        self._send_controls(c, enabled=True)

    def _fast_scheduler_tick(self, a: dict[str, Any], now: float):
        if not getattr(self, "_dj_controller", False):
            return                                             # v31.30.25: gesture scheduler removed
        """Low-cost beat execution tick between expensive MIR observations.

        At 170+ BPM the v31.6 180 ms planner polling interval could skip the early
        beat window entirely. This tick uses the already phase-locked anchor and
        cached analysis only; it copies no 15 s PCM and runs no FFT/model work.
        """
        c_out=None
        with self._lock:
            if self._manual or self._locked or not self._audible_started or self._beat_anchor_t<=0.0:
                return
            p=self._policy; c=dict(self._controller); bpm=max(65.0,float(a.get("bpm") or c.get("bpm") or 100.0)); beat_sec=60.0/bpm
            raw=max(0.0,(now-self._beat_anchor_t)/beat_sec); beat=int(math.floor(raw)); phase=raw-beat
            self._beat_index=beat; self._beat_phase=phase; self._bar_index=(beat-int(self._bar_offset))//4
            can_execute=self._autonomy=="autopilot" or self._approved
            if not can_execute or beat==self._last_action_beat or phase>=.48:
                return
            due=[q for q in self._performance_queue if int(q.get("beat",999999))<=beat]
            if not due: return
            ev=sorted(due,key=lambda q:(int(q.get("beat",0)),-float(q.get("score",0))))[0]
            action=str(ev.get("action") or "NO_ACTION")
            family=str(ev.get("family") or "")
            destructive=action in {"MACRO_ROLL_ACCEL","MACRO_ROLL_HALF","MACRO_ROLL_QUARTER","MACRO_TRANS_BUILD","MACRO_DRUM_FILL","ROLL_1","ROLL_2","FX_TRANS_GATE"} or family in {"ROLL_ACCEL","ROLL_HALF","ROLL_QUARTER","TRANS_GATE","DRUM_FILL"}
            vocal_limit=.72+.10*_clamp((float(getattr(p,"agentic_authority",1.0))-1.0)/2.60,0,1)
            if destructive and float(a.get("vocal_activity") or 0)>vocal_limit:
                ev["beat"]=beat+2; ev["bar"]=(beat+2)//4; ev["rationale"]=str(ev.get("rationale") or "")+" · postponed 2 beats by vocal guard"
                return
            self._last_action_beat=beat; self._last_action_bar=beat//4
            if action.startswith("PARAM_"):
                c=self._execute_parameterized_event(ev,a,beat,c)
            elif action.startswith("MACRO_"):
                c=self._execute_concert_macro(action,a,beat,c,float(ev.get("score") or .5),str(ev.get("rationale") or ""))
            else:
                c=self._execute_neural_action({"action":action,"score":ev.get("score",0),"rationale":ev.get("rationale","")},a,beat//4,c)
            self._mi.notify_event(str(ev.get("family") or action),beat,now)
            try: self._performance_queue.remove(ev)
            except ValueError: pass
            c=self._apply_overrides_locked(c,now)
            self._controller=c; c_out=dict(c)
        if c_out is not None:
            self._send_controls(c_out, enabled=True)

    def _loop(self):
        try:
            while not self._stop.is_set():
                rt=self.engine.status()
                if not rt.get("running"):
                    with self._lock: self._message="Waiting for JoyMetric realtime route…"
                    time.sleep(.35); continue
                sr=int(rt.get("sample_rate") or 48000)
                lookahead=float(rt.get("agentic_lookahead_sec") or self._policy.lookahead_sec or 15.0)
                # v31.17: the 15 s / 8 s PCM snapshots (~9 MB of copies) feed the
                # 0.85-1.35 s analyses only; the 15 s window is refreshed at 0.40 s
                # and the 8 s audible window exactly when its analysis is due, so
                # the beat-phase measurement always carries its own time stamp.
                now=time.monotonic()
                if self._future_cache is None or now-self._last_future_snap>=0.40:
                    self._future_cache=self.engine.agentic_audio_snapshot(max(15.0,lookahead)); self._last_future_snap=now
                future=self._future_cache; t_future=self._last_future_snap
                analysis_due=(now-self._last_context>=self._mir_interval()) or (not self._analysis)
                if analysis_due or self._present_cache is None:
                    snap_idx=getattr(self.engine,"agentic_render_audio_snapshot_indexed",None)
                    if snap_idx is not None:
                        self._present_cache,self._present_end_frame=snap_idx(8.0)
                    else:
                        self._present_cache=self.engine.agentic_render_audio_snapshot(8.0); self._present_end_frame=0
                    t_present=now
                else:
                    t_present=now
                self._sr=int(sr)
                present=self._present_cache

                future_len=0 if future is None else int(len(future))
                self._lookahead_fill=_clamp(future_len/max(1.0,float(sr*lookahead)),0,1)
                required=int(sr*max(8.0,lookahead)*.985)
                if future is not None and future_len>=sr*2:
                    if now-self._last_future_context>=self._mir_interval()*1.6 or not self._future_analysis:
                        fan=self._analysis_features(future,sr,t_future); self._last_future_context=now
                        with self._lock: self._future_analysis=fan
                    else:
                        with self._lock: fan=dict(self._future_analysis)
                    if now-self._last_segment_scan>=self._mir_interval()*2.0 and future_len>=sr*6:
                        segs=self._segment_future_context(future,sr,fan or self._future_analysis); self._last_segment_scan=now
                        with self._lock: self._future_segments=segs
                    if now-self._last_neural_request>=6.0:
                        self._submit_neural_window(future,sr); self._last_neural_request=now
                    if future_len>=required:
                        self._lookahead_ready=True

                # Execution analysis is aligned to delayed/audible source time. Before
                # first playback, future analysis is used only to pre-stage safe targets.
                exec_ctx=present if present is not None and len(present)>=sr*2 else future
                if exec_ctx is not None and len(exec_ctx)>=sr*2:
                    if analysis_due:
                        ana=self._analysis_features(exec_ctx,sr,(t_present if exec_ctx is present else t_future)); self._last_context=now
                        ana["_end_frame"]=int(self._present_end_frame) if exec_ctx is present else 0
                        with self._lock: self._analysis=ana
                    else:
                        with self._lock: ana=dict(self._analysis)

                    micro=self.engine.agentic_render_audio_snapshot(.25) if present is not None else self.engine.agentic_audio_snapshot(.25)
                    fast=self.engine.agentic_render_audio_snapshot(1.5) if present is not None else self.engine.agentic_audio_snapshot(1.5)
                    if micro is not None and len(micro)>sr//10:
                        mi=np.asarray(micro,dtype=np.float32); mono=np.mean(mi,axis=1) if mi.ndim==2 else mi
                        mrms=float(np.sqrt(np.mean(mono.astype(np.float64)**2)+1e-12)); mpeak=float(np.max(np.abs(mono)))
                        crest=mpeak/max(1e-6,mrms)
                        ana["transient_pressure"]=round(_clamp((crest-2.2)/5.0,0,1),3)
                    if fast is not None and len(fast)>sr//2:
                        fr=np.asarray(fast,dtype=np.float32); rms=float(np.sqrt(np.mean(fr*fr)+1e-12)); ana["fast_energy"]=round(_clamp(rms/.24,0,1),3)

                    if now-self._last_plan>=1.0:
                        with self._lock: self._timeline=self._build_plan(ana,now); self._last_plan=now
                    self._apply_agent_logic(ana,now)

                    with self._lock:
                        brain_state=str(self._neural.get("state") or "neural warming")
                        if not self._lookahead_ready:
                            pct=int(round(100.0*_clamp(future_len/max(1.0,float(sr*lookahead)),0,1)))
                            self._message=f"LOOKAHEAD BUFFER · {pct}% of {lookahead:.1f} s · analyzing future PCM before Audio Out"
                        elif not self._audible_started:
                            self._message=f"LOOKAHEAD READY · {lookahead:.1f} s analyzed · staging first audible bar · {brain_state}"
                        else:
                            scene=str((self._scene or {}).get("stage") or "LIVE")
                            self._message=f"PRO DJ · {lookahead:.1f} s LOOKAHEAD · {ana.get('bpm','—')} BPM · {scene} · {brain_state}"
                elif future is not None:
                    pct=int(round(100.0*_clamp(future_len/max(1.0,float(sr*lookahead)),0,1)))
                    with self._lock: self._message=f"LOOKAHEAD BUFFER · {pct}% of {lookahead:.1f} s · waiting for musical context"

                if now-self._last_media_poll>=1.5:
                    self._media=SpotifyMediaController.status(); self._last_media_poll=now
                    key="%s|%s" % (str(self._media.get("title") or ""), str(self._media.get("artist") or ""))
                    if key!=self._media_track_key:
                        if self._media_track_key and key!="|":
                            # new record: forget the old tempo memory so the grid relocks at once
                            self._beat_clock.reset(); self._beat_err_streak=0
                            try:
                                self._track_change_frame=int(rt.get("capture_frame_index") or 0) or None
                            except Exception:
                                self._track_change_frame=None
                        self._media_track_key=key
                # Three cheap scheduler ticks keep high-BPM performance actions
                # beat-tight without increasing MIR/CLAP cadence or copying PCM.
                for _ in range(3):
                    if self._stop.is_set(): break
                    time.sleep(.025)   # v31.16: 2.2x tighter control-rate cadence
                    tick_now=time.monotonic(); self._automation_scheduler_tick(tick_now)
                    with self._lock: tick_analysis=dict(self._analysis)
                    if tick_analysis: self._fast_scheduler_tick(tick_analysis,tick_now)
        except Exception as exc:
            with self._lock: self._error=str(exc); self._message=f"Agent error: {exc}"
        finally:
            self._running=False

