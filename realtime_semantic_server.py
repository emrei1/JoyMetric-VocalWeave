from __future__ import annotations

import base64
import json
import math
import os
import sys
import threading
import time
import re
import unicodedata
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HOST = os.environ.get("JOY_SEMANTIC_HOST", "127.0.0.1")
PORT = int(os.environ.get("JOY_SEMANTIC_PORT", "8767"))
MODEL_ID = os.environ.get("JOY_SEMANTIC_MODEL", "laion/clap-htsat-unfused")
PREFERRED_DEVICE = os.environ.get("JOY_SEMANTIC_DEVICE", "cpu").strip().lower() or "cpu"
ALLOW_CUDA = os.environ.get("JOY_SEMANTIC_ALLOW_CUDA", "0").strip().lower() not in {"0","false","no"}
DETAIL_MODEL_ID = os.environ.get("JOY_SEMANTIC_DETAIL_MODEL", "laion/larger_clap_music")
ENABLE_DETAIL_PLANNER = os.environ.get("JOY_SEMANTIC_DETAIL_PLANNER", "1").strip().lower() not in {"0","false","no"}

# Keep the out-of-band critic polite to the realtime process. CPU fallback stays
# low-thread-count; on CUDA the heavy CLAP forward pass lives on the GPU.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

FEATURE_ANCHORS = {
    "air": (
        "airy, open, sparkling high frequencies with breath and sheen",
        "closed, muffled, rolled-off high frequencies with little air",
    ),
    "warmth": (
        "warm, rich, rounded, full-bodied analog tone",
        "cold, thin, clinical, lean tone",
    ),
    "brightness": (
        "bright, crisp, luminous, vivid tonal balance",
        "dark, dull, subdued tonal balance",
    ),
    "bass": (
        "deep, powerful, bass-heavy low end with weight",
        "bass-light, lean low end with little weight",
    ),
    "clarity": (
        "clear, defined, separated, detailed sound",
        "hazy, muddy, blurred, indistinct sound",
    ),
    "hypnotic": (
        "hypnotic, mesmerizing, trance-like, slowly evolving spatial sound",
        "direct, static, dry, unmesmerizing sound",
    ),
    "dreamy": (
        "dreamy, ethereal, floating, soft-focus spacious ambience",
        "dry, grounded, direct, sharply focused sound",
    ),
    "space": (
        "large, spacious, reverberant, distant acoustic space",
        "tight, dry, small, close acoustic space",
    ),
    "width": (
        "very wide, expansive stereo image",
        "narrow, centered, almost mono stereo image",
    ),
    "intimacy": (
        "intimate, close, dry, present sound",
        "distant, remote, roomy sound",
    ),
    "punch": (
        "punchy, impactful, snappy, hard-hitting transients",
        "soft, flat, gentle, low-impact transients",
    ),
    "joy": (
        "joyful, uplifting, lively, sparkling musical character",
        "moody, somber, gloomy, restrained musical character",
    ),
    "depth": (
        "deep, full, dimensional, physically substantial sound",
        "shallow, thin, lightweight sound",
    ),
    "energy": (
        "energetic, exciting, forward, dense musical sound",
        "calm, subdued, relaxed, low-energy musical sound",
    ),
    "vintage": (
        "vintage, analog, saturated, softened old-record character",
        "modern, pristine, clean, transparent digital character",
    ),
}

# v29: 50-node prompt-detail planner.  These anchors describe the audible
# consequence of moving each physical DSP coordinate upward vs downward.  The
# slow planner only runs in text space when a prompt changes; the fast CLAP audio
# critic still owns current-moment scoring.  Frequency/Q/topology coordinates are
# intentionally lower-authority so detailed language cannot turn into unstable
# resonances or constant time sweeps.
DSP_DETAIL_ANCHORS = {
    "low_cut_hz": ("tight clean low end with subsonic rumble filtered out", "deep extended sub bass with very low frequencies preserved"),
    "low_cut_q": ("focused resonant low-cut edge", "gentle smooth low-cut transition"),
    "sub_db": ("powerful deep sub bass weight", "lean restrained sub bass"),
    "sub_freq_hz": ("upper sub punch around 70 to 100 hertz", "very deep sub focus around 30 to 50 hertz"),
    "bass_db": ("full strong bass and low-end body", "light controlled bass"),
    "bass_freq_hz": ("upper bass punch around 120 to 180 hertz", "deep bass emphasis around 60 to 90 hertz"),
    "body_db": ("thick warm low-mid body", "lean scooped low-mid body"),
    "body_freq_hz": ("upper low-mid warmth around 500 hertz", "lower low-mid warmth around 200 hertz"),
    "body_q": ("focused narrow low-mid tone", "broad gentle low-mid tone"),
    "mid_db": ("forward audible midrange", "recessed relaxed midrange"),
    "mid_freq_hz": ("upper mid focus around two kilohertz", "lower mid focus around seven hundred hertz"),
    "mid_q": ("focused narrow midrange presence", "broad natural midrange shaping"),
    "presence_db": ("clear forward detailed presence and attack", "soft laid-back smooth presence"),
    "presence_freq_hz": ("high presence and edge around six kilohertz", "lower presence around two kilohertz"),
    "presence_q": ("focused narrow presence detail", "broad smooth presence contour"),
    "air_db": ("airy open silky sparkling top end", "dark restrained softened top end"),
    "air_freq_hz": ("very high air and sheen above twelve kilohertz", "lower treble brightness around seven kilohertz"),
    "high_cut_hz": ("open extended high frequencies", "rolled-off dark smooth high frequencies"),
    "high_cut_q": ("focused resonant high-cut edge", "gentle natural high-cut slope"),
    "compression": ("dense controlled glued dynamics", "open natural uncompressed dynamics"),
    "comp_threshold_db": ("open dynamics with compression acting only on peaks", "dense compression acting on more of the signal"),
    "comp_ratio": ("firm controlled compression", "gentle subtle compression"),
    "comp_attack_ms": ("slower compression attack preserving punchy transients", "fast compression attack smoothing transients"),
    "comp_release_ms": ("slow smooth stable compressor release", "fast energetic compressor release"),
    "comp_knee_db": ("soft transparent compression knee", "hard assertive compression knee"),
    "comp_makeup_db": ("slightly more post-compression level", "slightly less post-compression level"),
    "transient_attack": ("sharp punchy defined attacks", "soft rounded attacks"),
    "transient_sustain": ("long full sustained body", "tight short controlled sustain"),
    "drive": ("rich warm analog harmonic drive", "clean pristine low-distortion signal"),
    "saturation_mix": ("audible saturated harmonic texture", "mostly clean transparent texture"),
    "saturation_asymmetry": ("asymmetric colorful even-order harmonic character", "symmetric clean odd-order harmonic character"),
    "low_drive": ("warm saturated bass harmonics", "clean transparent low frequencies"),
    "mid_drive": ("dense colorful saturated midrange", "clean transparent midrange"),
    "high_drive": ("excited textured high-frequency harmonics", "silky clean low-distortion highs"),
    "width": ("very wide immersive stereo image", "narrow centered mono-like stereo image"),
    "bass_mono_hz": ("stable mono-centered bass extending higher", "wide stereo bass extending lower"),
    "side_tilt_db": ("bright airy stereo sides", "dark warm stereo sides"),
    "stereo_balance": ("stereo image biased slightly to the right", "stereo image biased slightly to the left"),
    "hypnotic_motion_depth": ("deep hypnotic swirling stereo motion", "stable static stereo field"),
    "hypnotic_rate_hz": ("faster pulsing spatial motion", "slow drifting spatial motion"),
    "motion_phase": ("offset asymmetrical stereo motion phase", "aligned centered stereo motion phase"),
    "reverb": ("lush spacious audible reverberation", "dry close direct sound"),
    "reverb_decay": ("long floating reverberation tail", "short tight room decay"),
    "reverb_predelay_ms": ("clear separated reverb with long pre-delay", "immediate blended reverb with little pre-delay"),
    "reverb_diffusion": ("dense smooth diffuse reverb cloud", "sparse distinct early reflections"),
    "reverb_damping": ("dark soft damped reverb tail", "bright lively reverb tail"),
    "reverb_tone_hz": ("open bright airy reverb return", "dark warm filtered reverb return"),
    "delay": ("audible rhythmic echo and repeats", "dry sound with almost no echo"),
    "delay_ms": ("long spacious echo timing", "short slapback echo timing"),
    "delay_feedback": ("many sustained repeating echoes", "few quickly fading echoes"),
}

DSP_DETAIL_PRIOR_STRENGTH = {
    # highly audible / semantically direct
    "sub_db":1.00,"bass_db":1.00,"body_db":0.92,"mid_db":0.82,"presence_db":1.00,"air_db":1.00,
    "compression":0.90,"comp_ratio":0.72,"comp_attack_ms":0.70,"comp_release_ms":0.55,"comp_knee_db":0.48,
    "transient_attack":1.00,"transient_sustain":0.88,"drive":0.90,"saturation_mix":0.82,
    "low_drive":0.72,"mid_drive":0.78,"high_drive":0.52,"width":1.00,"bass_mono_hz":0.52,"side_tilt_db":0.64,
    "hypnotic_motion_depth":1.00,"hypnotic_rate_hz":0.52,"reverb":1.00,"reverb_decay":0.82,
    "reverb_predelay_ms":0.58,"reverb_diffusion":0.82,"reverb_damping":0.68,"reverb_tone_hz":0.72,
    "delay":1.00,"delay_ms":0.58,"delay_feedback":0.78,"high_cut_hz":0.78,"low_cut_hz":0.55,
    # topology / center-frequency coordinates: useful for detail, but never dominant
    "sub_freq_hz":0.30,"bass_freq_hz":0.28,"body_freq_hz":0.24,"mid_freq_hz":0.24,"presence_freq_hz":0.28,"air_freq_hz":0.30,
    "low_cut_q":0.14,"body_q":0.16,"mid_q":0.16,"presence_q":0.16,"high_cut_q":0.14,
    "comp_threshold_db":0.55,"comp_makeup_db":0.12,"saturation_asymmetry":0.28,"stereo_balance":0.12,"motion_phase":0.08,
}

# Direct instruction overrides are deliberately sparse and musical.  They only
# reinforce clear production words; the embedding planner handles the rest.
DSP_DETAIL_KEYWORDS = {
    "reverb": {"reverb":1.0,"reverb_decay":0.65,"reverb_diffusion":0.55},
    "echo": {"delay":1.0,"delay_feedback":0.55}, "delay": {"delay":1.0,"delay_feedback":0.50},
    "wide": {"width":1.0}, "narrow": {"width":-1.0}, "mono": {"width":-1.0,"bass_mono_hz":0.55},
    "airy": {"air_db":1.0,"high_cut_hz":0.65}, "air": {"air_db":1.0,"high_cut_hz":0.55},
    "bright": {"air_db":0.85,"presence_db":0.55,"high_cut_hz":0.50}, "dark": {"air_db":-0.85,"high_cut_hz":-0.70},
    "warm": {"body_db":0.75,"bass_db":0.40,"drive":0.45,"air_db":-0.20},
    "punch": {"transient_attack":1.0,"comp_attack_ms":0.55,"presence_db":0.45},
    "soft": {"transient_attack":-0.65,"presence_db":-0.35}, "smooth": {"high_drive":-0.55,"presence_q":-0.25,"comp_knee_db":0.45},
    "clean": {"drive":-0.85,"saturation_mix":-0.80,"high_drive":-0.70}, "saturated": {"drive":0.95,"saturation_mix":0.85},
    "vintage": {"drive":0.70,"high_cut_hz":-0.62,"body_db":0.55,"air_db":-0.30},
    "hypnotic": {"hypnotic_motion_depth":1.0,"delay":0.55,"reverb":0.40},
    "dreamy": {"reverb":0.90,"reverb_decay":0.72,"reverb_diffusion":0.65,"width":0.45,"delay":0.30},
    "spacious": {"reverb":0.82,"width":0.72,"reverb_predelay_ms":0.36}, "intimate": {"reverb":-0.72,"delay":-0.55,"width":-0.28,"presence_db":0.38},
    "bass": {"bass_db":0.92,"sub_db":0.70}, "sub": {"sub_db":1.0,"sub_freq_hz":-0.35},
    "clear": {"presence_db":0.70,"mid_db":0.30,"drive":-0.20}, "muddy": {"body_db":-0.45,"low_cut_hz":0.38},
    "dry": {"reverb":-1.0,"delay":-0.95},
}


# v30 Auto DSP semantic compiler -------------------------------------------------
# The music-specific text model may surface many correlated controls for one
# adjective.  Realtime sound quality is better when those correlations are
# compiled onto a sparse, physically coherent manifold before live gradients
# are allowed to move the renderer.
DSP_MUSIC_GROUPS = {
    "EQ": (
        "low_cut_hz","low_cut_q","sub_db","sub_freq_hz","bass_db","bass_freq_hz",
        "body_db","body_freq_hz","body_q","mid_db","mid_freq_hz","mid_q",
        "presence_db","presence_freq_hz","presence_q","air_db","air_freq_hz",
        "high_cut_hz","high_cut_q",
    ),
    "DYNAMICS": (
        "compression","comp_threshold_db","comp_ratio","comp_attack_ms",
        "comp_release_ms","comp_knee_db","comp_makeup_db",
        "transient_attack","transient_sustain",
    ),
    "HARMONICS": (
        "drive","saturation_mix","saturation_asymmetry","low_drive","mid_drive","high_drive",
    ),
    "STEREO": (
        "width","bass_mono_hz","side_tilt_db","stereo_balance",
        "hypnotic_motion_depth","hypnotic_rate_hz","motion_phase",
    ),
    "REVERB": (
        "reverb","reverb_decay","reverb_predelay_ms","reverb_diffusion",
        "reverb_damping","reverb_tone_hz",
    ),
    "DELAY": ("delay","delay_ms","delay_feedback"),
}
DSP_MUSIC_TOPK = {"EQ":5,"DYNAMICS":3,"HARMONICS":3,"STEREO":3,"REVERB":3,"DELAY":2}
DSP_STRUCTURAL_NODES = {
    "low_cut_q","sub_freq_hz","bass_freq_hz","body_freq_hz","body_q",
    "mid_freq_hz","mid_q","presence_freq_hz","presence_q","air_freq_hz","high_cut_q",
    "comp_threshold_db","comp_attack_ms","comp_release_ms","comp_knee_db",
    "comp_makeup_db","saturation_asymmetry","bass_mono_hz","stereo_balance",
    "hypnotic_rate_hz","motion_phase","reverb_predelay_ms","reverb_diffusion",
    "reverb_damping","reverb_tone_hz","delay_ms",
}
DSP_GROUP_LANGUAGE = {
    "EQ": ("air","airy","bright","dark","warm","bass","sub","body","mid","presence","clear","clarity","muddy","thin","thick","silky","top end","treble","low end","parlak","karan","sıcak","sicak","bas","tiz","ferah"),
    "DYNAMICS": ("punch","punchy","impact","dynamic","dynamics","compress","glue","snappy","attack","sustain","soft","hard hitting","loud","vurucu","yumuşak","yumusak"),
    "HARMONICS": ("analog","vintage","saturat","drive","distort","harmonic","warm","clean","pristine","dirty","texture","doygun","eski","temiz"),
    "STEREO": ("wide","width","stereo","mono","centered","immersive","hypnotic","psychedelic","swirl","moving","motion","geniş","genis","hipnotik","psikedelik"),
    "REVERB": ("reverb","room","hall","plate","space","spacious","dreamy","ethereal","ambient","distant","intimate","dry","lush","tail","decay","yank","rüy","ruya","kuru","yakın","yakin"),
    "DELAY": ("delay","echo","repeat","slap","dub","feedback","rhythmic","echoes","yank"),
}

def _music_prior_projection(prompt: str, prior: dict[str, float], explicit: dict[str, float]) -> dict[str, float]:
    """Compile dense text embeddings into a sparse, production-safe DSP plan."""
    low = unicodedata.normalize("NFKC", str(prompt or "")).casefold()
    out = {name: float(max(-1.0, min(1.0, prior.get(name, 0.0)))) for name in DSP_DETAIL_ANCHORS}
    explicit_nodes = {k for k, v in explicit.items() if abs(float(v)) >= 0.08}

    # Family relevance is lexical only as a stabilizer; learned CLAP evidence
    # still supplies the direction and nuance within each relevant family.
    relevance = {}
    for group, words in DSP_GROUP_LANGUAGE.items():
        hits = sum(1 for w in words if w in low)
        relevance[group] = min(1.0, 0.18 + 0.26 * hits) if hits else 0.0

    # Broad style words legitimately touch several families.
    if any(w in low for w in ("dreamy","ethereal","rüyamsı","ruyamsi")):
        relevance["REVERB"] = max(relevance["REVERB"], .85); relevance["STEREO"] = max(relevance["STEREO"], .55)
    if any(w in low for w in ("hypnotic","psychedelic","hipnotik","psikedelik")):
        relevance["STEREO"] = max(relevance["STEREO"], .90); relevance["DELAY"] = max(relevance["DELAY"], .55); relevance["REVERB"] = max(relevance["REVERB"], .45)
    if any(w in low for w in ("club","master","polished","kulüp","kulup")):
        relevance["DYNAMICS"] = max(relevance["DYNAMICS"], .45); relevance["EQ"] = max(relevance["EQ"], .42)

    # Keep only a handful of learned coordinates per processing family.
    for group, nodes in DSP_MUSIC_GROUPS.items():
        candidates = sorted(nodes, key=lambda n: abs(out.get(n, 0.0)), reverse=True)
        keep = set(candidates[:DSP_MUSIC_TOPK[group]]) | (explicit_nodes & set(nodes))
        rel = relevance.get(group, 0.0)
        for name in nodes:
            if name in explicit_nodes:
                continue
            if name not in keep:
                out[name] *= 0.10 if rel < 0.20 else 0.24
            elif rel < 0.20:
                out[name] *= 0.42
            # Center-frequency/Q/timing coordinates are coloration details, never
            # primary style actuators unless language explicitly asks for them.
            if name in DSP_STRUCTURAL_NODES:
                out[name] *= 0.34 if rel >= 0.45 else 0.16

    # Hard semantic topology rules eliminate common bad-sounding failure modes.
    if re.search(r"\b(?:no|without|remove|zero|dry|kuru)\s+(?:delay|echo)\b|\b(?:delay|echo)\s*(?:off|yok)\b", low):
        out["delay"] = -1.0; out["delay_feedback"] = -0.95; out["delay_ms"] = 0.0
    if re.search(r"\b(?:no|without|remove|zero|dry|kuru)\s+reverb\b|\breverb\s*(?:off|yok)\b", low):
        out["reverb"] = -1.0; out["reverb_decay"] = -0.80
        for n in ("reverb_predelay_ms","reverb_diffusion","reverb_damping","reverb_tone_hz"):
            out[n] *= 0.10
    if any(w in low for w in ("clean","pristine","silky","temiz","pürüzsüz","puruzsuz")):
        out["drive"] = min(out["drive"], -0.30)
        out["saturation_mix"] = min(out["saturation_mix"], -0.25)
        out["high_drive"] = min(out["high_drive"], -0.35)
    if any(w in low for w in ("punch","punchy","snappy","hard-hitting","hard hitting","vurucu")):
        out["transient_attack"] = max(out["transient_attack"], 0.72)
        # Too much compression destroys the very transient the prompt requests.
        out["compression"] = min(out["compression"], 0.38)
        out["comp_attack_ms"] = max(out["comp_attack_ms"], 0.22)
    if any(w in low for w in ("wide","expansive","geniş","genis")):
        out["width"] = max(out["width"], 0.72)
        out["stereo_balance"] *= 0.08
        out["motion_phase"] *= 0.18
    if any(w in low for w in ("smooth","silky","soft","yumuşak","yumusak")):
        out["high_drive"] = min(out["high_drive"], -0.30)
        out["presence_q"] = min(out["presence_q"], 0.0)

    # Explicit scoped instructions remain authoritative after manifold projection.
    for name, val in explicit.items():
        out[name] = float(max(-1.0, min(1.0, 0.18 * out.get(name, 0.0) + 0.82 * float(val))))

    # Per-family energy normalization avoids "everything at once" mixes.
    family_budget = {"EQ":2.65,"DYNAMICS":1.80,"HARMONICS":1.65,"STEREO":1.70,"REVERB":1.85,"DELAY":1.25}
    for group, nodes in DSP_MUSIC_GROUPS.items():
        energy = sum(abs(out[n]) for n in nodes)
        budget = family_budget[group]
        if energy > budget and energy > 1e-9:
            scale = budget / energy
            for n in nodes:
                if n not in explicit_nodes:
                    out[n] *= scale

    # Absolute "off/no/yok" commands are topology, not style. Re-assert them
    # after blending/normalization so an embedding can never partially undo them.
    no_delay = bool(re.search(
        r"\b(?:no|without|remove|zero|dry|kuru)\s+(?:delay|echo)\b|"
        r"\b(?:delay|echo|gecikme)\s*(?:off|yok|olmasin|olmasın)\b", low))
    no_reverb = bool(re.search(
        r"\b(?:no|without|remove|zero|dry|kuru)\s+(?:reverb|yanki|yankı)\b|"
        r"\b(?:reverb|yanki|yankı)\s*(?:off|yok|olmasin|olmasın)\b", low))
    if no_delay:
        out["delay"] = -1.0
        out["delay_feedback"] = -1.0
        out["delay_ms"] = 0.0
    if no_reverb:
        out["reverb"] = -1.0
        out["reverb_decay"] = -0.90
        for n in ("reverb_predelay_ms","reverb_diffusion","reverb_damping","reverb_tone_hz"):
            out[n] = 0.0
    return {k: float(max(-1.0, min(1.0, v))) for k, v in out.items()}




AGENT_ACTION_PROTOTYPES = {
    "NO_ACTION": "a professional DJ preserving the groove and leaving the music untouched because no intervention is needed",
    "LOW_END_PROTECT": "a professional DJ gently protecting a heavy kick and sub bass with a subtle low EQ move",
    "EQ_TIGHTEN": "a professional DJ making a subtle clean three band EQ correction while preserving the groove",
    "FILTER_BUILD": "a professional DJ starting a restrained high pass filter build before a musical boundary",
    "FILTER_RELEASE": "a professional DJ releasing a filter exactly on a downbeat for impact",
    "ROLL_1": "a professional DJ using a tight one beat loop roll at a rhythmically appropriate moment",
    "ROLL_2": "a professional DJ using a controlled two beat loop roll at a phrase boundary",
    "ECHO_THROW": "a professional DJ applying a short tasteful echo throw at the end of a phrase or vocal",
    "REVERB_SPACE": "a professional DJ adding a small amount of spatial reverb during a sparse musical moment",
    "FX_RISER": "a professional performance DJ using a controlled noise riser or uplifter to build tension into a phrase boundary",
    "FX_REVERSE_SWELL": "a professional performance DJ using a reverse swell or reverse cymbal style suction effect before a transition",
    "FX_SNARE_RUSH": "a professional performance DJ using a fast snare rush or drum fill that accelerates into a drop without masking the vocal",
    "FX_IMPACT": "a professional performance DJ landing a strong cinematic impact exactly on a drop or downbeat",
    "FX_ECHO_BLOOM": "a professional performance DJ using a phrase-end echo bloom with a short spacious tail as a transition effect",
    "FX_BEAT_ECHO": "a professional club DJ using a tempo-synchronised post-fader beat echo or ping-pong echo-out on a phrase boundary",
    "FX_TRANS_GATE": "a professional club DJ using a tempo-synchronised transform gate or rhythmic chop to accelerate a build without destroying the low end",
    "ENERGY_LIFT": "a professional DJ subtly lifting energy with EQ and filter movement without damaging the bass",
    "LOOP_RELEASE": "a professional DJ cleanly releasing a loop on a downbeat and returning to the live track",
    "BEATJUMP_FWD": "a professional DJ making a deliberate phrase aligned eight beat forward jump to crop a repetitive section",
    "BEATJUMP_BACK": "a professional DJ making a deliberate phrase aligned eight beat backward jump to repeat a strong musical moment",
}

AGENT_STATE_PROTOTYPES = {
    "stable_groove": "a stable repeating musical groove with no major structural change",
    "phrase_transition": "a musical phrase ending or section transition approaching",
    "buildup": "a musical buildup with rising tension heading toward a drop",
    "drop": "a strong musical drop with full drums and bass impact",
    "breakdown": "a breakdown or sparse section with reduced drums and bass",
    "vocal_focus": "a clear foreground vocal phrase that should be protected from excessive effects",
    "instrumental": "an instrumental passage with room for creative DJ processing",
    "bass_heavy": "a bass heavy section with strong sub bass or 808 energy",
    "drum_dense": "a rhythmically dense section with strong drums and transients",
    "high_energy": "a high energy intense section of music",
    "low_energy": "a low energy calm or sparse section of music",
}

class SemanticModel:
    """Out-of-band CLAP critic.

    This worker is isolated from the realtime audio callback.  It normally idles
    on CPU and can be handed a CUDA device while DJ/50D mode is active.
    Model/package/device failures never become audio-path failures; the worker
    falls back or retries while native DSP remains fully usable.
    """

    def __init__(self):
        self.lock = threading.RLock()
        # Serialize model moves and forwards so a GPU/CPU handoff can never occur
        # halfway through a CLAP pass. HTTP/status locking stays independent.
        self.inference_lock = threading.RLock()
        self.ready = False
        self.state = "Semantic AI starting · isolated CPU realtime critic"
        self.device = "cpu"
        self.device_label = "CPU"
        self.cuda_available = False
        self.gpu_name = ""
        self.model = None
        self.feature_extractor = None
        self.tokenizer = None
        self.text_pos = None
        self.text_neg = None
        self.names = list(FEATURE_ANCHORS)
        self.loaded_at = 0.0
        self.inference_ms = 0.0
        self.last_issue = ""
        self.retry_count = 0
        self.prompt_cache: dict[str, Any] = {}
        self.prompt_cache_order: list[str] = []
        self.agent_action_emb = None
        self.agent_state_emb = None
        self.agent_action_names = list(AGENT_ACTION_PROTOTYPES)
        self.agent_state_names = list(AGENT_STATE_PROTOTYPES)
        self.agent_action_prompt_cache: dict[str, Any] = {}
        self.agent_action_prompt_order: list[str] = []

        # v30 music-domain detailed-prompt planner.  This is a *text-only* second model and is
        # never used to reconstruct audio.  It only converts rich free-form prompt
        # language into a stable 50D DSP prior.  The larger model loads after the
        # realtime critic is ready, and every failure falls back to the fast CLAP
        # text space so the live route can never depend on it.
        self.detail_lock = threading.RLock()
        self.detail_ready = False
        self.detail_state = "detail planner waiting"
        self.detail_model = None
        self.detail_tokenizer = None
        self.detail_pos = None
        self.detail_neg = None
        self.detail_fast_pos = None
        self.detail_fast_neg = None
        self.detail_prompt_cache: dict[str, Any] = {}
        self.detail_prompt_cache_order: list[str] = []
        self._stop = threading.Event()
        threading.Thread(target=self._loader_loop, daemon=True, name="joymetric-clap-loader").start()
        if ENABLE_DETAIL_PLANNER:
            threading.Thread(target=self._detail_loader_loop, daemon=True, name="joymetric-detail-planner-loader").start()

    def _set_loading(self, text: str):
        with self.lock:
            self.ready = False
            self.state = text

    def _load_once(self):
        import numpy as np
        import torch
        from transformers import AutoFeatureExtractor, AutoTokenizer, ClapModel

        try:
            torch.set_num_threads(1)
            torch.set_num_interop_threads(1)
        except Exception:
            pass
        try:
            torch.set_float32_matmul_precision("high")
        except Exception:
            pass
        try:
            torch.backends.cuda.matmul.allow_tf32 = True
        except Exception:
            pass

        cuda_ok = bool(ALLOW_CUDA and torch.cuda.is_available())
        gpu_name = ""
        if cuda_ok:
            try:
                gpu_name = str(torch.cuda.get_device_name(0) or "CUDA GPU")
            except Exception:
                gpu_name = "CUDA GPU"

        # Load on CPU first.  The app explicitly moves the critic to CUDA when DJ/50D
        # mode starts, after asking the resident Stable Audio worker to release VRAM.
        self._set_loading(f"Semantic AI loading · CPU audio-priority · {MODEL_ID}")
        tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
        feature_extractor = AutoFeatureExtractor.from_pretrained(MODEL_ID)
        model = ClapModel.from_pretrained(MODEL_ID)
        model.eval()
        model.to("cpu")

        texts: list[str] = []
        for name in self.names:
            pos, neg = FEATURE_ANCHORS[name]
            texts.extend([pos, neg])
        toks = tokenizer(texts, return_tensors="pt", padding=True, truncation=True)
        detail_texts: list[str] = []
        for name in DSP_DETAIL_ANCHORS:
            pos_desc, neg_desc = DSP_DETAIL_ANCHORS[name]
            detail_texts.extend([pos_desc, neg_desc])
        detail_toks = tokenizer(detail_texts, return_tensors="pt", padding=True, truncation=True, max_length=77)
        agent_action_toks = tokenizer(list(AGENT_ACTION_PROTOTYPES.values()), return_tensors="pt", padding=True, truncation=True, max_length=77)
        agent_state_toks = tokenizer(list(AGENT_STATE_PROTOTYPES.values()), return_tensors="pt", padding=True, truncation=True, max_length=77)
        with torch.inference_mode():
            emb = model.get_text_features(**toks)
            emb = emb / emb.norm(dim=-1, keepdim=True).clamp_min(1e-9)
            detail_emb = model.get_text_features(**detail_toks)
            detail_emb = detail_emb / detail_emb.norm(dim=-1, keepdim=True).clamp_min(1e-9)
            agent_action_emb = model.get_text_features(**agent_action_toks)
            agent_action_emb = agent_action_emb / agent_action_emb.norm(dim=-1, keepdim=True).clamp_min(1e-9)
            agent_state_emb = model.get_text_features(**agent_state_toks)
            agent_state_emb = agent_state_emb / agent_state_emb.norm(dim=-1, keepdim=True).clamp_min(1e-9)

        # Full CPU warm-up before advertising ready. This catches processor or
        # operator incompatibilities once, during setup, rather than during a song.
        probe_audio = np.zeros(48000, dtype=np.float32)
        aud = feature_extractor(probe_audio, sampling_rate=48000, return_tensors="pt")
        with torch.inference_mode():
            warm = model.get_audio_features(**aud)
            _ = warm / warm.norm(dim=-1, keepdim=True).clamp_min(1e-9)

        with self.lock:
            self.tokenizer = tokenizer
            self.feature_extractor = feature_extractor
            self.model = model
            self.text_pos = emb[0::2].detach().cpu()
            self.text_neg = emb[1::2].detach().cpu()
            self.detail_fast_pos = detail_emb[0::2].detach().cpu()
            self.detail_fast_neg = detail_emb[1::2].detach().cpu()
            self.agent_action_emb = agent_action_emb.detach().cpu()
            self.agent_state_emb = agent_state_emb.detach().cpu()
            self.cuda_available = cuda_ok
            self.gpu_name = gpu_name
            self.device = "cpu"
            self.device_label = "CPU"
            self.ready = True
            self.state = "ready · CPU realtime critic"
            self.loaded_at = time.time()
            self.last_issue = ""
            self.retry_count = 0

        # Respect an explicit startup preference, but normal JoyMetric startup uses
        # CPU standby and switches to CUDA only when a realtime AI mode is engaged.
        if PREFERRED_DEVICE in {"cuda", "gpu"} and cuda_ok:
            self.set_device("cuda")


    def _detail_loader_loop(self):
        """Load the larger text-only planner after the fast critic is online."""
        # Never race the realtime critic's first model download/warm-up.
        while not self._stop.wait(0.40):
            with self.lock:
                if self.ready:
                    break
        if self._stop.is_set():
            return
        try:
            import torch
            from transformers import AutoTokenizer, ClapTextModelWithProjection
            with self.detail_lock:
                self.detail_state = f"detail planner loading · {DETAIL_MODEL_ID}"
            tokenizer = AutoTokenizer.from_pretrained(DETAIL_MODEL_ID)
            model = ClapTextModelWithProjection.from_pretrained(DETAIL_MODEL_ID)
            model.eval().to("cpu")
            texts = []
            for name in DSP_DETAIL_ANCHORS:
                pos_desc, neg_desc = DSP_DETAIL_ANCHORS[name]
                texts.extend([pos_desc, neg_desc])
            toks = tokenizer(texts, return_tensors="pt", padding=True, truncation=True, max_length=77)
            with torch.inference_mode():
                out = model(**toks)
                emb = out.text_embeds.float()
                emb = emb / emb.norm(dim=-1, keepdim=True).clamp_min(1e-9)
            # A tiny variance sanity check prevents a broken/collapsed text space
            # from silently steering 50 physical controls.
            probe_cos = float((emb[0] * emb[1]).sum().detach().cpu()) if emb.shape[0] >= 2 else 1.0
            if (not math.isfinite(probe_cos)) or abs(probe_cos) > 0.99995:
                raise RuntimeError(f"detail text embedding sanity check failed (cos={probe_cos:.6f})")
            with self.detail_lock:
                self.detail_model = model
                self.detail_tokenizer = tokenizer
                self.detail_pos = emb[0::2].detach().cpu()
                self.detail_neg = emb[1::2].detach().cpu()
                self.detail_ready = True
                self.detail_state = f"ready · detailed prompt planner · {DETAIL_MODEL_ID}"
        except Exception as exc:
            with self.detail_lock:
                self.detail_ready = False
                self.detail_state = f"detail planner fallback · fast CLAP anchors · {type(exc).__name__}"
            print(f"[semantic] detail planner fallback: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    @staticmethod
    def _instructional_prior_overrides(prompt: str) -> dict[str, float]:
        """Phrase-aware deterministic reinforcement for explicit production instructions.

        The learned text planner handles free-form meaning. This layer exists for
        instructions where scope matters: "dark reverb" must darken the reverb
        return, not the whole master; "crisp vocal" should move presence/transient
        controls, not randomly brighten every band.
        """
        low = SemanticModel._canonical_prompt(prompt).lower()
        out: dict[str, float] = {}
        aliases = {
            "rüyamsı":"dreamy","ruyamsi":"dreamy","hipnotik":"hypnotic",
            "sıcak":"warm","sicak":"warm","geniş":"wide","genis":"wide",
            "parlak":"bright","karanlık":"dark","karanlik":"dark",
            "ferah":"airy","havadar":"airy","vurucu":"punchy","yumuşak":"smooth","yumusak":"smooth",
            "temiz":"clean","doygun":"saturated","yankı":"reverb","yanki":"reverb",
            "kuru":"dry","yakın":"intimate","yakin":"intimate","baslı":"bass","basli":"bass",
            "vokal":"vocal","vokaller":"vocals","davul":"drums","tizler":"highs","tiz":"highs",
            "uzun":"long","kısa":"short","kisa":"short","derin":"deep","sıkı":"tight","siki":"tight",
        }
        expanded = low
        for src, dst in aliases.items():
            if src in low:
                expanded += " " + dst

        def add(mapping: dict[str, float], scale: float = 1.0):
            for node, val in mapping.items():
                out[node] = max(-1.0, min(1.0, float(out.get(node, 0.0)) + float(val) * scale))

        # Apply scoped production phrases first and remove those exact spans from
        # the global keyword pass. This prevents adjective leakage across buses.
        global_text = expanded
        scoped_rules = [
            (r"\b(?:long|huge|endless)\s+(?:dark|warm|dull)\s+reverb\b",
             {"reverb":0.70,"reverb_decay":0.94,"reverb_diffusion":0.56,"reverb_tone_hz":-0.78,"reverb_damping":0.58}),
            (r"\b(?:long|huge|endless)\s+(?:bright|airy|sparkling)\s+reverb\b",
             {"reverb":0.70,"reverb_decay":0.94,"reverb_diffusion":0.54,"reverb_tone_hz":0.82,"reverb_damping":-0.50}),
            (r"\b(?:dark|dull|muffled)\s+(?:lush\s+)?reverb\b|\breverb\s+(?:that\s+is\s+)?(?:dark|dull|muffled)\b",
             {"reverb_tone_hz":-0.90,"reverb_damping":0.72,"reverb_diffusion":0.18}),
            (r"\b(?:bright|airy|sparkling)\s+(?:lush\s+)?reverb\b|\breverb\s+(?:that\s+is\s+)?(?:bright|airy|sparkling)\b",
             {"reverb_tone_hz":0.88,"reverb_damping":-0.62,"reverb_diffusion":0.18}),
            (r"\bwarm\s+reverb\b|\breverb\s+(?:that\s+is\s+)?warm\b",
             {"reverb_tone_hz":-0.52,"reverb_damping":0.36,"reverb_diffusion":0.24}),
            (r"\b(?:long|huge|lush|endless)\s+reverb\b|\breverb\s+(?:with\s+)?(?:long|huge|lush|endless)\b",
             {"reverb":0.68,"reverb_decay":0.92,"reverb_diffusion":0.52}),
            (r"\b(?:short|tight|small)\s+reverb\b|\breverb\s+(?:with\s+)?(?:short|tight|small)\b",
             {"reverb":0.25,"reverb_decay":-0.88,"reverb_predelay_ms":-0.35}),
            (r"\b(?:clean|silky|smooth)\s+(?:highs|top\s*end|treble)\b|\b(?:highs|top\s*end|treble)\s+(?:that\s+are\s+)?(?:clean|silky|smooth)\b",
             {"high_drive":-0.92,"drive":-0.28,"air_db":0.30,"high_cut_hz":0.24}),
            (r"\b(?:crisp|clear|detailed|forward)\s+(?:vocal|vocals|voice)\b|\b(?:vocal|vocals|voice)\s+(?:that\s+is\s+)?(?:crisp|clear|detailed|forward)\b",
             {"presence_db":0.78,"mid_db":0.24,"transient_attack":0.26,"drive":-0.12}),
            (r"\b(?:soft|smooth|intimate)\s+(?:vocal|vocals|voice)\b|\b(?:vocal|vocals|voice)\s+(?:that\s+is\s+)?(?:soft|smooth|intimate)\b",
             {"presence_db":-0.34,"comp_knee_db":0.36,"reverb":-0.20}),
            (r"\b(?:punchy|hard[- ]?hitting|snappy)\s+(?:drum|drums|kick|percussion)\b|\b(?:drum|drums|kick|percussion)\s+(?:that\s+are\s+)?(?:punchy|hard[- ]?hitting|snappy)\b",
             {"transient_attack":1.00,"comp_attack_ms":0.52,"presence_db":0.28,"transient_sustain":-0.12}),
            (r"\b(?:deep|heavy|massive)\s+(?:bass|sub|low\s*end)\b|\b(?:bass|sub|low\s*end)\s+(?:that\s+is\s+)?(?:deep|heavy|massive)\b",
             {"sub_db":0.92,"bass_db":0.58,"sub_freq_hz":-0.22}),
            (r"\b(?:tight|controlled)\s+(?:bass|low\s*end)\b|\b(?:bass|low\s*end)\s+(?:that\s+is\s+)?(?:tight|controlled)\b",
             {"low_cut_hz":0.30,"bass_db":0.18,"transient_sustain":-0.34,"low_drive":-0.18}),
            (r"\b(?:wide|huge|expansive)\s+(?:stereo|image|field)\b|\b(?:stereo|image|field)\s+(?:that\s+is\s+)?(?:wide|huge|expansive)\b",
             {"width":1.00,"side_tilt_db":0.12}),
            (r"\b(?:narrow|mono|centered)\s+(?:stereo|image|field)\b|\b(?:stereo|image|field)\s+(?:that\s+is\s+)?(?:narrow|mono|centered)\b",
             {"width":-1.00,"bass_mono_hz":0.30}),
        ]
        for pattern, mapping in scoped_rules:
            matches = list(re.finditer(pattern, global_text, flags=re.I))
            if matches:
                add(mapping, min(1.35, 1.0 + 0.12 * (len(matches) - 1)))
                global_text = re.sub(pattern, " ", global_text, flags=re.I)

        more = r"(?:more|add|increase|boost|stronger|extra|very|much|daha|fazla|artir|arttır|artır)"
        less = r"(?:less|reduce|decrease|lower|remove|without|no|not|dry|az|azalt|azaltir|azaltır)"
        for keyword, mapping in DSP_DETAIL_KEYWORDS.items():
            if not re.search(rf"\b{re.escape(keyword)}\b", global_text):
                continue
            sign = 1.0
            if re.search(rf"\b{less}\b[^,.;]{{0,28}}\b{re.escape(keyword)}\b", global_text) or re.search(rf"\b{re.escape(keyword)}\b[^,.;]{{0,18}}\b{less}\b", global_text):
                sign = -1.0
            elif re.search(rf"\b{more}\b[^,.;]{{0,28}}\b{re.escape(keyword)}\b", global_text) or re.search(rf"\b{re.escape(keyword)}\b[^,.;]{{0,18}}\b{more}\b", global_text):
                sign = 1.18
            add(mapping, sign)
        return out

    def _detail_prompt_prior(self, prompt: str) -> tuple[dict[str, float], str, float]:
        """Return a signed 50D DSP prior from detailed text, never from audio resynthesis."""
        import numpy as np
        import torch
        clean, views, weights = self._prompt_views(prompt)
        cache_key = clean
        with self.detail_lock:
            cached = self.detail_prompt_cache.get(cache_key)
        if cached is not None:
            return cached

        planner_name = f"fast-anchor:{MODEL_ID}"
        confidence = 0.62
        emb = None
        detail_scores = None
        pos = neg = None
        with self.detail_lock:
            if self.detail_ready and self.detail_model is not None and self.detail_tokenizer is not None:
                model = self.detail_model
                tokenizer = self.detail_tokenizer
                pos = self.detail_pos
                neg = self.detail_neg
            else:
                model = tokenizer = None
        if model is not None and tokenizer is not None and pos is not None and neg is not None:
            try:
                toks = tokenizer(views, return_tensors="pt", padding=True, truncation=True, max_length=77)
                with self.detail_lock, torch.inference_mode():
                    out = model(**toks)
                    embs = out.text_embeds.float()
                    embs = embs / embs.norm(dim=-1, keepdim=True).clamp_min(1e-9)
                    # Preserve prompt details instead of collapsing every clause into
                    # one vector. Each physical DSP axis is allowed to listen to the
                    # most relevant clause, while the weighted full-prompt mean keeps
                    # the plan coherent. This is text-only and never touches audio.
                    pos_dev = pos.to(embs.device, dtype=embs.dtype)
                    neg_dev = neg.to(embs.device, dtype=embs.dtype)
                    view_scores = embs @ pos_dev.T - embs @ neg_dev.T  # [views, 50]
                    w = torch.as_tensor(weights, dtype=embs.dtype, device=embs.device)[:, None]
                    mean_scores = (view_scores * w).sum(dim=0)
                    # Clause salience: use the strongest signed evidence per node,
                    # but cap its authority so a short phrase cannot hijack the mix.
                    sal_idx = torch.argmax(torch.abs(view_scores), dim=0)
                    cols = torch.arange(view_scores.shape[1], device=embs.device)
                    salient = view_scores[sal_idx, cols]
                    detail_scores = 0.64 * mean_scores + 0.36 * salient
                    emb = None
                planner_name = DETAIL_MODEL_ID
                confidence = 0.97
            except Exception as exc:
                print(f"[semantic] detail prompt fallback: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                emb = None

        if emb is None:
            # Same-space fallback is always available once the fast critic is ready.
            emb = self._prompt_embedding(prompt).detach().float().cpu()
            with self.lock:
                pos = self.detail_fast_pos
                neg = self.detail_fast_neg
        if pos is None or neg is None:
            return ({name: 0.0 for name in DSP_DETAIL_ANCHORS}, planner_name, 0.0)

        if detail_scores is not None:
            raw = detail_scores.detach().float().cpu().numpy()
        else:
            raw = (emb.cpu() @ pos.T - emb.cpu() @ neg.T).squeeze(0).detach().float().cpu().numpy()

        # Contrastive sparsification. Relative CLAP differences can be tiny even for
        # valid prompts; normalizing all 50 axes would incorrectly make every prompt
        # move everything. Keep strong, specific evidence and softly suppress the
        # ambiguous middle of the field. Explicit language overrides are applied
        # afterwards and therefore remain authoritative.
        amag = np.abs(raw)
        p42 = float(np.percentile(amag, 42))
        p78 = max(p42 + 1e-6, float(np.percentile(amag, 78)))
        scale = max(0.010, p78)
        vals = np.tanh(raw / (1.05 * scale))
        gate = np.clip((amag - p42) / max(1e-6, p78 - p42), 0.0, 1.0)
        gate = np.sqrt(gate)
        vals = vals * (0.20 + 0.80 * gate)
        prior = {}
        for i, name in enumerate(DSP_DETAIL_ANCHORS):
            prior[name] = float(np.clip(vals[i] * DSP_DETAIL_PRIOR_STRENGTH.get(name, 0.45), -1.0, 1.0))

        # Explicit "less/no/without/more" instructions should beat embedding
        # ambiguity but still blend with the learned text prior.
        overrides = self._instructional_prior_overrides(prompt)
        for name, val in overrides.items():
            base = float(prior.get(name, 0.0))
            prior[name] = float(np.clip(0.35 * base + 0.65 * val, -1.0, 1.0))

        # v30: compile the dense music/text embedding into a sparse, coherent
        # production manifold. This is the main quality change versus v29:
        # prompts no longer spray correlated motion across dozens of controls.
        prior = _music_prior_projection(prompt, prior, overrides)

        result = (prior, planner_name, confidence)
        with self.detail_lock:
            self.detail_prompt_cache[cache_key] = result
            self.detail_prompt_cache_order.append(cache_key)
            while len(self.detail_prompt_cache_order) > 12:
                old = self.detail_prompt_cache_order.pop(0)
                self.detail_prompt_cache.pop(old, None)
        return result


    def _move_prompt_cache_locked(self, device: str):
        moved = {}
        for key, value in self.prompt_cache.items():
            try:
                moved[key] = value.to(device)
            except Exception:
                pass
        self.prompt_cache = moved
        self.prompt_cache_order = [k for k in self.prompt_cache_order if k in moved]

    def set_device(self, target: str) -> dict[str, Any]:
        """Move the persistent CLAP critic between CPU and CUDA safely.

        The model never enters the audio callback.  CUDA is used only for batched
        semantic scoring, while the native WASAPI/DSP path remains independent.
        """
        import torch
        target = str(target or "cpu").strip().lower()
        if target in {"gpu", "cuda", "cuda:0"}:
            target = "cuda"
        else:
            target = "cpu"
        with self.lock:
            if not self.ready or self.model is None:
                return {"ok": False, "ready": False, "device": self.device, "state": self.state}
            if target == self.device:
                return {"ok": True, "ready": True, "device": self.device, "state": self.state, "gpu": self.gpu_name or None}
            if target == "cuda" and not (ALLOW_CUDA and self.cuda_available and torch.cuda.is_available()):
                self.state = "ready · CPU · CUDA unavailable for Semantic AI"
                self.device = "cpu"
                self.device_label = "CPU"
                return {"ok": False, "ready": True, "device": "cpu", "state": self.state, "gpu": self.gpu_name or None}
            model = self.model
            pos = self.text_pos
            neg = self.text_neg
            self.state = f"Semantic AI moving to {'GPU' if target == 'cuda' else 'CPU'}…"
        try:
            with self.inference_lock:
                model.to(target)
                if pos is not None:
                    pos = pos.to(target)
                if neg is not None:
                    neg = neg.to(target)
                with self.lock:
                    self.text_pos = pos
                    self.text_neg = neg
                    self._move_prompt_cache_locked(target)
                    self.device = target
                    if target == "cuda":
                        self.device_label = self.gpu_name or "CUDA GPU"
                        self.state = f"ready · GPU · {self.device_label}"
                    else:
                        self.device_label = "CPU"
                        self.state = "ready · CPU standby" if self.cuda_available else "ready · CPU"
                if target == "cpu" and torch.cuda.is_available():
                    try:
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
            return {"ok": True, "ready": True, "device": target, "state": self.state, "gpu": self.gpu_name or None}
        except Exception as exc:
            issue = f"{type(exc).__name__}: {exc}"
            print(f"[semantic] device switch issue: {issue}", file=sys.stderr, flush=True)
            # A failed CUDA move must never poison realtime audio. Restore CPU.
            try:
                with self.inference_lock:
                    model.to("cpu")
                    if pos is not None:
                        pos = pos.to("cpu")
                    if neg is not None:
                        neg = neg.to("cpu")
            except Exception:
                pass
            with self.lock:
                self.text_pos = pos
                self.text_neg = neg
                self._move_prompt_cache_locked("cpu")
                self.device = "cpu"
                self.device_label = "CPU"
                self.last_issue = issue
                self.state = "ready · CPU fallback · GPU handoff failed"
            try:
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            return {"ok": False, "ready": True, "device": "cpu", "state": self.state, "error": issue, "gpu": self.gpu_name or None}

    def _to_device_inputs(self, data: dict[str, Any], device: str):
        out = {}
        for key, value in data.items():
            if hasattr(value, "to"):
                try:
                    if device == "cuda" and hasattr(value, "pin_memory") and getattr(value, "device", None) is not None and str(value.device) == "cpu":
                        value = value.pin_memory()
                    out[key] = value.to(device, non_blocking=(device == "cuda"))
                except Exception:
                    out[key] = value.to(device)
            else:
                out[key] = value
        return out

    def _autocast_context(self, torch):
        if self.device == "cuda":
            return torch.autocast(device_type="cuda", dtype=torch.float16)
        return nullcontext()

    def _loader_loop(self):
        # A temporary network/cache/package problem should self-heal without the
        # user having to restart JoyMetric.  The UI never enters a hard error state.
        while not self._stop.is_set():
            try:
                self._load_once()
                return
            except Exception as exc:
                issue = f"{type(exc).__name__}: {exc}"
                print(f"[semantic] setup issue: {issue}", file=sys.stderr, flush=True)
                with self.lock:
                    self.ready = False
                    self.model = None
                    self.feature_extractor = None
                    self.tokenizer = None
                    self.text_pos = None
                    self.text_neg = None
                    self.last_issue = issue
                    self.retry_count += 1
                    self.state = "Semantic AI setup retrying · realtime DSP stays active"
                delay = min(60.0, 8.0 * (2 ** min(self.retry_count - 1, 3)))
                self._stop.wait(delay)

    @staticmethod
    def _decode_audio(value: str):
        import numpy as np

        raw = base64.b64decode(value.encode("ascii"), validate=False)
        arr = np.frombuffer(raw, dtype="<f4").astype(np.float32, copy=True)
        if arr.size == 0:
            raise ValueError("empty audio buffer")
        return np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)

    @staticmethod
    def _resample(x, src_rate: int, dst_rate: int = 48000):
        import numpy as np

        src_rate = int(src_rate)
        if src_rate == dst_rate:
            return x.astype(np.float32, copy=False)
        if x.size < 2:
            n = max(2, int(round(x.size * dst_rate / max(1, src_rate))))
            return np.zeros((n,), dtype=np.float32)

        # Prefer polyphase resampling when scipy is available in the isolated
        # semantic environment. It is both cleaner and cheaper than interpolating
        # long windows sample-by-sample.
        try:
            from math import gcd
            from scipy.signal import resample_poly

            g = gcd(src_rate, dst_rate)
            y = resample_poly(x, dst_rate // g, src_rate // g)
            return y.astype(np.float32, copy=False)
        except Exception:
            n = max(2, int(round(x.size * float(dst_rate) / float(src_rate))))
            old = np.linspace(0.0, 1.0, x.size, endpoint=False, dtype=np.float64)
            new = np.linspace(0.0, 1.0, n, endpoint=False, dtype=np.float64)
            return np.interp(new, old, x).astype(np.float32)

    @staticmethod
    def _canonical_prompt(prompt: str) -> str:
        """Accept essentially any human prompt without letting odd Unicode break CLAP.

        Browsers can submit smart quotes, emoji, zero-width characters, newlines, or
        very long prose.  Older builds passed that string straight into the tokenizer,
        which made some prompts appear to be "not accepted".  We normalize safely,
        preserve the user's wording, and only remove non-printing control characters.
        """
        raw = str(prompt or "")
        try:
            raw = raw.encode("utf-8", "replace").decode("utf-8", "replace")
        except Exception:
            raw = str(raw)
        raw = unicodedata.normalize("NFKC", raw)
        raw = "".join(ch if (ch.isprintable() or ch in "\n\t ") else " " for ch in raw)
        raw = re.sub(r"\s+", " ", raw).strip()
        # Keep enough room for descriptive prompts; the encoder itself receives
        # multiple bounded views, so long prompts are never rejected for length.
        return raw[:4000]

    @classmethod
    def _prompt_views(cls, prompt: str):
        clean = cls._canonical_prompt(prompt)
        if not clean:
            # Empty targets are treated as a transparent, balanced production goal
            # instead of returning HTTP 422 and killing an otherwise live DJ mode.
            clean = "balanced transparent polished music mix"

        # CLAP is noticeably more robust when free-form instructions are grounded
        # into audio-production language.  The raw text remains the dominant view.
        views = [
            clean,
            f"a music mix that sounds {clean}",
            f"audio production characterized by {clean}",
            f"a track with {clean} timbre, spatial character, dynamics and texture",
        ]
        weights = [0.46, 0.20, 0.18, 0.16]

        # Instruction-style prompts ("less reverb", "more punch") are not the
        # distribution CLAP was primarily trained on. Add an audio-description
        # interpretation so imperative wording still has a strong acoustic target.
        low = clean.lower()
        hints = []
        polarity = {
            "reverb": ("spacious reverberant ambience", "dry direct close sound"),
            "delay": ("audible rhythmic echo and repeats", "dry sound with almost no echo"),
            "bass": ("deep strong full bass", "light controlled bass"),
            "bright": ("bright crisp open top end", "dark smooth restrained top end"),
            "air": ("airy open sparkling high frequencies", "closed smooth rolled-off highs"),
            "width": ("very wide immersive stereo image", "narrow centered stereo image"),
            "punch": ("punchy impactful defined transients", "soft rounded low-impact transients"),
            "warm": ("warm rich analog low-mid body", "cool clean lean tonal balance"),
            "compression": ("controlled dense compressed dynamics", "open uncompressed dynamic range"),
            "distortion": ("harmonically saturated driven texture", "clean transparent low-distortion texture"),
        }
        more_words = r"(?:more|add|increase|boost|stronger|extra|fazla|artir|arttır|artır)"
        less_words = r"(?:less|not\s+too|reduce|decrease|lower|remove|without|no|dry|az|azalt|azaltir|azaltır)"
        for key, (pos_desc, neg_desc) in polarity.items():
            if re.search(rf"\b{more_words}\b[^,.;]{{0,24}}\b{re.escape(key)}", low) or re.search(rf"\b{re.escape(key)}\b[^,.;]{{0,16}}\b{more_words}\b", low):
                hints.append(pos_desc)
            if re.search(rf"\b{less_words}\b[^,.;]{{0,24}}\b{re.escape(key)}", low) or re.search(rf"\b{re.escape(key)}\b[^,.;]{{0,16}}\b{less_words}\b", low):
                hints.append(neg_desc)
        if hints:
            views.append("music production that is " + ", ".join(hints[:5]))
            weights.append(0.16)

        # Small bilingual production lexicon: CLAP's English text space is much
        # stronger than its Turkish coverage. This does not replace the raw prompt;
        # it only appends an English acoustic hint when familiar Turkish mix words
        # appear, so Turkish prompts do not look like unsupported targets.
        tr_map = {
            "rüyamsı": "dreamy ethereal", "ruyamsi": "dreamy ethereal", "rüya": "dreamy",
            "hipnotik": "hypnotic mesmerizing", "sıcak": "warm rich", "sicak": "warm rich",
            "geniş": "wide spacious stereo", "genis": "wide spacious stereo",
            "parlak": "bright crisp", "karanlık": "dark smooth", "karanlik": "dark smooth",
            "ferah": "airy open", "havadar": "airy open", "derin": "deep full",
            "yakın": "intimate close", "yakin": "intimate close", "uzak": "distant spacious",
            "vurucu": "punchy impactful", "enerjik": "energetic", "neşeli": "joyful uplifting",
            "neseli": "joyful uplifting", "baslı": "bass heavy", "basli": "bass heavy",
            "temiz": "clean clear", "bulanık": "hazy muddy", "bulanik": "hazy muddy",
            "yankı": "reverberant echo", "yanki": "reverberant echo", "kuru": "dry direct",
            "eski": "vintage analog", "modern": "modern clean",
            "yumuşak": "smooth soft", "yumusak": "smooth soft", "sert": "hard aggressive",
            "kalın": "thick full-bodied", "kalin": "thick full-bodied", "ince": "thin light",
            "boğuk": "muffled dark", "boguk": "muffled dark", "keskin": "sharp crisp",
            "pürüzsüz": "smooth polished", "puruzsuz": "smooth polished",
            "sinematik": "cinematic spacious dramatic", "psikedelik": "psychedelic swirling",
            "kulüp": "club powerful wide", "kulup": "club powerful wide",
            "master": "polished mastered controlled", "doygun": "saturated rich",
        }
        tr_hints = []
        for token, desc in tr_map.items():
            if token in low and desc not in tr_hints:
                tr_hints.append(desc)
        if tr_hints:
            views.append("music production that sounds " + ", ".join(tr_hints[:6]))
            weights.append(0.18)

        # Long prompts often contain several independent production clauses. Keep
        # several compact clause views so late details (for example "dark reverb
        # but crisp vocal, no delay") survive CLAP's short text context instead of
        # being averaged away by the opening words.
        clauses = [c.strip(" ,.;:-") for c in re.split(r"[;.!?]+|\s*,\s*", clean) if c.strip()]
        seen_clause = set()
        for clause in clauses[:6]:
            key = clause.casefold()
            if len(clause) >= 6 and key != clean.casefold() and key not in seen_clause:
                seen_clause.add(key)
                views.append(f"music production detail: {clause}")
                weights.append(0.075)
        total = sum(weights)
        weights = [w / total for w in weights]
        return clean, views, weights

    def _prompt_embedding(self, prompt: str):
        clean, views, weights = self._prompt_views(prompt)
        with self.lock:
            cached = self.prompt_cache.get(clean)
            model = self.model
            tokenizer = self.tokenizer
            device = self.device
        if cached is not None:
            return cached
        if model is None or tokenizer is None:
            raise RuntimeError("semantic model is still preparing")
        import torch
        # CLAP text towers are trained with a short context. Explicit max_length
        # avoids tokenizer-version-specific failures on unusually long prompts.
        try:
            toks = tokenizer(views, return_tensors="pt", padding=True, truncation=True, max_length=77)
        except Exception:
            # Last-resort ASCII view: never reject a live DJ target because a
            # tokenizer/version dislikes an unusual Unicode sequence. The raw
            # canonical prompt remains visible in UI/status.
            safe_views = []
            for v in views:
                v2 = unicodedata.normalize("NFKD", str(v)).encode("ascii", "ignore").decode("ascii", "ignore")
                v2 = re.sub(r"\s+", " ", v2).strip() or "balanced polished music production"
                safe_views.append(v2)
            toks = tokenizer(safe_views, return_tensors="pt", padding=True, truncation=True, max_length=77)
        with self.inference_lock:
            toks = self._to_device_inputs(dict(toks), device)
            with torch.inference_mode(), self._autocast_context(torch):
                embs = model.get_text_features(**toks).float()
                embs = embs / embs.norm(dim=-1, keepdim=True).clamp_min(1e-9)
                w = torch.as_tensor(weights, dtype=embs.dtype, device=embs.device)[:, None]
                emb = (embs * w).sum(dim=0, keepdim=True)
                emb = emb / emb.norm(dim=-1, keepdim=True).clamp_min(1e-9)
            emb = emb.detach().to(device)
        with self.lock:
            self.prompt_cache[clean] = emb
            self.prompt_cache_order.append(clean)
            while len(self.prompt_cache_order) > 16:
                old = self.prompt_cache_order.pop(0)
                self.prompt_cache.pop(old, None)
        return emb

    @staticmethod
    def _normalize_analysis_audio(arrays):
        import numpy as np
        out = []
        for x in arrays:
            x = np.nan_to_num(np.asarray(x, dtype=np.float32).reshape(-1), nan=0.0, posinf=0.0, neginf=0.0)
            rms = float(np.sqrt(np.mean(x * x) + 1e-12)) if x.size else 0.0
            if rms < 3e-5:
                out.append(x)
                continue
            gain = min(4.0, 0.10 / max(rms, 1e-6))
            out.append((x * gain).astype(np.float32, copy=False))
        return out

    def prompt_profile(self, prompt: str) -> dict[str, Any]:
        import numpy as np
        prompt_emb = self._prompt_embedding(prompt)
        with self.lock:
            pos = self.text_pos
            neg = self.text_neg
        affinity_raw = (prompt_emb @ pos.T - prompt_emb @ neg.T).squeeze(0).detach().float().cpu().numpy()
        scale = max(0.025, float(np.percentile(np.abs(affinity_raw), 75)))
        affinity = np.tanh(affinity_raw / (1.35 * scale))
        detail_prior, detail_model, detail_conf = self._detail_prompt_prior(prompt)
        return {
            "ok": True,
            "prompt": self._canonical_prompt(prompt),
            "model": MODEL_ID,
            "device": self.device,
            "feature_affinity": {name: float(affinity[i]) for i, name in enumerate(self.names)},
            "dsp_prompt_prior": detail_prior,
            "detail_model": detail_model,
            "detail_confidence": float(detail_conf),
        }

    def _score_prompt_audio_batch(self, prompt: str, arrays, sample_rate: int, _allow_cpu_retry: bool = True):
        import numpy as np
        import torch
        with self.lock:
            if not self.ready or self.model is None or self.feature_extractor is None:
                raise RuntimeError("semantic model is still preparing")
            model = self.model
            feature_extractor = self.feature_extractor
            pos = self.text_pos
            neg = self.text_neg
            device = self.device
        prompt_emb = self._prompt_embedding(prompt)
        waves = [self._resample(np.asarray(a, dtype=np.float32), int(sample_rate), 48000) for a in arrays]
        waves = self._normalize_analysis_audio(waves)
        if not waves or max((w.size for w in waves), default=0) < 12000:
            raise ValueError("analysis window is too short")
        started = time.perf_counter()
        aud = feature_extractor(waves, sampling_rate=48000, return_tensors="pt")
        try:
            with self.inference_lock:
                aud = self._to_device_inputs(dict(aud), device)
                with torch.inference_mode(), self._autocast_context(torch):
                    ae = model.get_audio_features(**aud).float()
                    ae = ae / ae.norm(dim=-1, keepdim=True).clamp_min(1e-9)
                    prompt_scores = (ae @ prompt_emb.float().T).squeeze(-1)
                    feature_raw = ae @ pos.float().T - ae @ neg.float().T
        except RuntimeError as exc:
            if device == "cuda" and _allow_cpu_retry:
                print(f"[semantic] CUDA analysis fallback: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                self.set_device("cpu")
                return self._score_prompt_audio_batch(prompt, arrays, sample_rate, _allow_cpu_retry=False)
            raise
        elapsed = (time.perf_counter() - started) * 1000.0
        self.inference_ms = elapsed

        # Prompt affinity and *current audio state* are returned in the same
        # normalized [-1, 1] feature space.  This lets the controller react to the
        # newest bar/chorus instead of blindly applying the same gradient forever.
        affinity_raw = (prompt_emb @ pos.T - prompt_emb @ neg.T).squeeze(0).detach().float().cpu().numpy()
        affinity_scale = max(0.025, float(np.percentile(np.abs(affinity_raw), 75)))
        affinity = np.tanh(affinity_raw / (1.35 * affinity_scale))

        raw_profiles = feature_raw.detach().float().cpu().numpy()
        profiles = []
        for row in raw_profiles:
            scale = max(0.020, float(np.percentile(np.abs(row), 75)))
            norm = np.tanh(row / (1.45 * scale))
            profiles.append({name: float(norm[i]) for i, name in enumerate(self.names)})

        return (
            prompt_scores.detach().float().cpu().numpy(),
            {name: float(affinity[i]) for i, name in enumerate(self.names)},
            profiles,
            elapsed,
        )

    def _agent_action_embeddings(self, prompt: str):
        clean=self._canonical_prompt(prompt) if str(prompt or "").strip() else "professional balanced DJ set"
        with self.lock:
            cached=self.agent_action_prompt_cache.get(clean)
            model=self.model; tokenizer=self.tokenizer; device=self.device
        if cached is not None:
            return cached
        if model is None or tokenizer is None:
            raise RuntimeError("semantic model is still preparing")
        texts=[f"In a DJ performance described as: {clean}. This is {desc}." for desc in AGENT_ACTION_PROTOTYPES.values()]
        toks=tokenizer(texts,return_tensors="pt",padding=True,truncation=True,max_length=77)
        import torch
        with self.inference_lock:
            toks=self._to_device_inputs(dict(toks),device)
            with torch.inference_mode(), self._autocast_context(torch):
                embs=model.get_text_features(**toks).float()
                embs=embs/embs.norm(dim=-1,keepdim=True).clamp_min(1e-9)
            embs=embs.detach().cpu()
        with self.lock:
            self.agent_action_prompt_cache[clean]=embs
            self.agent_action_prompt_order.append(clean)
            while len(self.agent_action_prompt_order)>8:
                old=self.agent_action_prompt_order.pop(0); self.agent_action_prompt_cache.pop(old,None)
        return embs

    def agent_state(self, audio_b64: str, sample_rate: int, prompt: str = "") -> dict[str, Any]:
        """Neural live-DJ observation from raw PCM.

        The audio callback never calls this. The agent worker sends a bounded recent
        window to the isolated CLAP process. We score the same audio embedding
        against musical-state and DJ-action text prototypes, then return the
        normalized embedding for temporal memory/novelty tracking.
        """
        import numpy as np
        import torch
        with self.lock:
            if not self.ready or self.model is None or self.feature_extractor is None:
                raise RuntimeError("semantic model is still preparing")
            model = self.model
            fx = self.feature_extractor
            device = self.device
            action_emb = self.agent_action_emb
            state_emb = self.agent_state_emb
            action_names = list(self.agent_action_names)
            state_names = list(self.agent_state_names)
        # Prompt-conditioned action bank is calculated out of band and cached.
        # The static bank remains a fallback if a text-embedding pass fails.
        try:
            action_emb = self._agent_action_embeddings(prompt)
        except Exception:
            pass
        if action_emb is None or state_emb is None:
            raise RuntimeError("agent neural prototypes are still preparing")
        x = self._decode_audio(audio_b64)
        x = self._resample(np.asarray(x, dtype=np.float32), int(sample_rate), 48000)
        # CLAP's processor is a 10 s music/audio window. Use the newest 9.75 s so
        # live decisions are causal and do not depend on future PCM.
        max_n = int(9.75 * 48000)
        if x.size > max_n:
            x = x[-max_n:]
        if x.size < 24000:
            raise ValueError("agent neural window is too short")
        x = self._normalize_analysis_audio([x])[0]
        aud = fx([x], sampling_rate=48000, return_tensors="pt")
        started = time.perf_counter()
        with self.inference_lock:
            aud = self._to_device_inputs(dict(aud), device)
            with torch.inference_mode(), self._autocast_context(torch):
                ae = model.get_audio_features(**aud).float()
                ae = ae / ae.norm(dim=-1, keepdim=True).clamp_min(1e-9)
                act = ae @ action_emb.to(ae.device, dtype=ae.dtype).T
                state = ae @ state_emb.to(ae.device, dtype=ae.dtype).T
                prompt_score = None
                if str(prompt or "").strip():
                    pe = self._prompt_embedding(prompt).to(ae.device, dtype=ae.dtype)
                    prompt_score = float((ae @ pe.T).squeeze().detach().cpu())
        elapsed = (time.perf_counter()-started)*1000.0
        avec = ae.squeeze(0).detach().float().cpu().numpy()
        actv = act.squeeze(0).detach().float().cpu().numpy()
        stv = state.squeeze(0).detach().float().cpu().numpy()
        # Relative scores are more useful than raw CLAP cosine magnitudes. Keep
        # ordering, compress outliers, and preserve NO_ACTION as a true candidate.
        def rel(v):
            v=np.asarray(v,dtype=np.float64)
            med=float(np.median(v)); scale=max(0.018,float(np.percentile(np.abs(v-med),75)))
            return np.tanh((v-med)/(1.6*scale))
        ar=rel(actv); sr=rel(stv)
        return {
            "ok": True,
            "model": MODEL_ID,
            "device": device,
            "window_sec": round(float(x.size)/48000.0,3),
            "inference_ms": round(elapsed,1),
            "prompt_similarity": prompt_score,
            "actions": {name: float(ar[i]) for i,name in enumerate(action_names)},
            "states": {name: float(sr[i]) for i,name in enumerate(state_names)},
            "embedding": [round(float(v),6) for v in avec.tolist()],
        }

    @staticmethod
    def _render_variant(dry_audio, sample_rate: int, params: dict[str, float]):
        import numpy as np
        from realtime_native_engine import RealtimeDSP
        x = np.asarray(dry_audio, dtype=np.float32)
        if x.ndim == 1:
            stereo = np.stack([x, x], axis=1)
        else:
            stereo = x[:, :2]
            if stereo.shape[1] < 2:
                stereo = np.repeat(stereo[:, :1], 2, axis=1)
        dsp = RealtimeDSP(int(sample_rate), 2)
        chunks = []
        # Offline derivative probes do not need the realtime 256-frame quantum.
        # Larger blocks reduce Python overhead without changing the DSP equations.
        block = 1024
        for start in range(0, stereo.shape[0], block):
            piece = stereo[start:start + block]
            if piece.size == 0:
                continue
            chunks.append(dsp.process(piece, params, float(params.get("hypnotic_motion_depth", 0.0) or 0.0)))
        if not chunks:
            return stereo.astype(np.float32, copy=False)
        y = np.concatenate(chunks, axis=0)
        cut = min(max(0, int(sample_rate * 0.30)), max(0, y.shape[0] - int(sample_rate * 0.65)))
        return y[cut:].astype(np.float32, copy=False)

    @staticmethod
    def _stereo_character(y):
        import numpy as np
        y = np.asarray(y, dtype=np.float32)
        if y.ndim != 2 or y.shape[1] < 2 or y.shape[0] < 8:
            return 0.0, 0.0
        l, r = y[:, 0], y[:, 1]
        mid = 0.5 * (l + r); side = 0.5 * (l - r)
        mid_rms = float(np.sqrt(np.mean(mid * mid) + 1e-12))
        side_rms = float(np.sqrt(np.mean(side * side) + 1e-12))
        side_ratio = float(np.tanh((side_rms / max(mid_rms, 1e-6)) * 2.2))
        corr = float(np.mean(l * r) / max(1e-9, np.sqrt(np.mean(l*l) * np.mean(r*r))))
        decorrelation = float(np.clip((1.0 - corr) * 0.5, 0.0, 1.0))
        return side_ratio, decorrelation

    @staticmethod
    def _production_descriptor(audio, sample_rate: int) -> dict[str, float]:
        """Cheap loudness-invariant production descriptors for the derivative critic.

        CLAP supplies semantic meaning; these descriptors supply high-SNR physical
        evidence that EQ/dynamics/space controls actually changed the mix. They are
        intentionally simple, deterministic and evaluated only in the isolated
        semantic worker.
        """
        import numpy as np
        x = np.asarray(audio, dtype=np.float32)
        if x.ndim == 1:
            stereo = np.stack([x, x], axis=1)
        else:
            stereo = x[:, :2] if x.shape[1] >= 2 else np.repeat(x[:, :1], 2, axis=1)
        mono = np.mean(stereo, axis=1).astype(np.float32, copy=False)
        if mono.size < 128:
            return {k: 0.0 for k in ("sub","bass","lowmid","presence","air","centroid","flatness","crest","transient","side","decor","ambience")}
        rms = float(np.sqrt(np.mean(mono * mono) + 1e-12))
        z = mono / max(rms, 1e-6)
        nfft = 4096 if mono.size >= 4096 else 2 ** int(np.floor(np.log2(max(128, mono.size))))
        seg = z[-nfft:]
        win = np.hanning(seg.size).astype(np.float32)
        mag = np.abs(np.fft.rfft(seg * win)).astype(np.float64) + 1e-10
        power = mag * mag
        freqs = np.fft.rfftfreq(seg.size, 1.0 / float(sample_rate))
        total = float(np.sum(power)) + 1e-12
        def band(lo, hi):
            m = (freqs >= lo) & (freqs < min(hi, sample_rate * 0.49))
            return float(np.sum(power[m]) / total) if np.any(m) else 0.0
        sub = band(25, 90); bass = band(90, 240); lowmid = band(240, 1200)
        presence = band(1800, 6000); air = band(6000, 14500)
        centroid = float(np.sum(freqs * power) / total) / max(6000.0, sample_rate * 0.25)
        flatness = float(np.exp(np.mean(np.log(mag))) / max(np.mean(mag), 1e-10))
        crest = float(np.max(np.abs(z))) / 8.0
        transient = float(np.mean(np.abs(np.diff(z)))) / 0.24
        mid = 0.5 * (stereo[:,0] + stereo[:,1]); side = 0.5 * (stereo[:,0] - stereo[:,1])
        mid_r = float(np.sqrt(np.mean(mid * mid) + 1e-12)); side_r = float(np.sqrt(np.mean(side * side) + 1e-12))
        side_ratio = side_r / max(mid_r + side_r, 1e-8)
        den = float(np.sqrt(np.mean(stereo[:,0]**2) * np.mean(stereo[:,1]**2) + 1e-12))
        corr = float(np.mean(stereo[:,0] * stereo[:,1]) / max(den, 1e-8))
        decor = float(np.clip((1.0 - corr) * 0.5, 0.0, 1.0))
        env = np.abs(z).astype(np.float64)
        env2 = float(np.mean(env * env)) + 1e-10
        ac = []
        for sec in (0.028, 0.061, 0.113):
            lag = int(round(float(sample_rate) * sec))
            if 8 <= lag < env.size // 2:
                ac.append(float(np.mean(env[:-lag] * env[lag:]) / env2))
        ambience = float(np.mean(ac)) if ac else 0.0
        return {
            "sub": sub, "bass": bass, "lowmid": lowmid, "presence": presence, "air": air,
            "centroid": centroid, "flatness": flatness, "crest": crest, "transient": transient,
            "side": side_ratio, "decor": decor, "ambience": ambience,
        }

    @classmethod
    def _production_objective(cls, rendered, affinity: dict[str, float], sample_rate: int):
        """Prompt-directed physical mix objective, returned as delta from base.

        This does not replace CLAP. It gives the black-box gradient a clean signal
        for production changes CLAP is sometimes insensitive to (a few dB of EQ,
        width, transient shape, reverb/delay).
        """
        import numpy as np
        desc = [cls._production_descriptor(y, sample_rate) for y in rendered]
        if not desc:
            return np.zeros(0, dtype=np.float32)
        a = lambda name: float(affinity.get(name, 0.0) or 0.0)
        target = {k: 0.0 for k in desc[0]}
        target["air"] += 1.10*a("air") + 0.55*a("brightness") - 0.45*a("vintage")
        target["centroid"] += 0.70*a("brightness") + 0.35*a("clarity") - 0.45*a("warmth") - 0.35*a("vintage")
        target["presence"] += 0.50*a("clarity") + 0.38*a("punch") + 0.30*a("energy")
        target["sub"] += 0.55*a("bass") + 0.25*a("depth")
        target["bass"] += 0.82*a("bass") + 0.38*a("depth") + 0.20*a("warmth")
        target["lowmid"] += 0.65*a("warmth") + 0.34*a("depth") + 0.28*a("vintage")
        target["crest"] += 0.82*a("punch") + 0.26*a("energy") - 0.22*a("dreamy")
        target["transient"] += 0.70*a("punch") + 0.48*a("energy")
        target["side"] += 1.00*a("width") + 0.42*a("space") + 0.30*a("dreamy") + 0.28*a("hypnotic") - 0.55*a("intimacy")
        target["decor"] += 0.55*a("width") + 0.42*a("hypnotic") + 0.30*a("space")
        target["ambience"] += 0.82*a("space") + 0.68*a("dreamy") + 0.44*a("hypnotic") - 0.72*a("intimacy")
        target["flatness"] += 0.22*a("vintage") + 0.14*a("energy")
        scales = {
            "sub":0.055,"bass":0.075,"lowmid":0.080,"presence":0.075,"air":0.060,
            "centroid":0.085,"flatness":0.050,"crest":0.16,"transient":0.18,
            "side":0.11,"decor":0.13,"ambience":0.10,
        }
        norm = sum(abs(v) for v in target.values())
        if norm < 1e-6:
            return np.zeros(len(desc), dtype=np.float32)
        base = desc[0]
        scores = []
        for d in desc:
            val = 0.0
            for name, weight in target.items():
                if abs(weight) < 1e-8:
                    continue
                delta = float(d[name] - base[name])
                val += weight * float(np.tanh(delta / max(1e-5, scales[name])))
            scores.append(val / norm)
        return np.asarray(scores, dtype=np.float32)

    @staticmethod
    def _feature_alignment_objective(affinity: dict[str, float], profiles: list[dict[str, float]]):
        import numpy as np
        if not profiles:
            return np.zeros(0, dtype=np.float32)
        weights = {
            "air":1.0,"warmth":1.0,"brightness":0.9,"bass":1.0,"clarity":0.9,
            "hypnotic":0.8,"dreamy":1.0,"space":1.0,"width":1.0,"intimacy":0.8,
            "punch":1.0,"joy":0.35,"depth":0.9,"energy":0.55,"vintage":0.9,
        }
        vals = []
        for prof in profiles:
            loss = 0.0; denom = 0.0
            for name, w in weights.items():
                target = float(affinity.get(name, 0.0) or 0.0)
                cur = float(prof.get(name, 0.0) or 0.0)
                loss += w * (target - cur) ** 2
                denom += w
            vals.append(-loss / max(1e-6, denom))
        arr = np.asarray(vals, dtype=np.float32)
        return arr - arr[0]

    def dj_gradient(self, prompt: str, dry_b64: str, sample_rate: int, params: dict[str, Any], nodes: list[str], cycle: int = 0, channels: int = 1) -> dict[str, Any]:
        """Current-moment DJ gradient using one antithetic SPSA field.

        Every pass estimates derivatives for *all* DJ internal nodes with only a
        base/+/- render.  Every third pass adds one exact central-difference node
        for calibration.  This is materially faster on CPU than probing only two
        nodes exactly while still giving the feature deck a full gradient field on
        every current musical moment.
        """
        import numpy as np
        import zlib
        from realtime_native_engine import PARAM_BOUNDS, DJ_NODE_SPECS
        raw = self._decode_audio(dry_b64)
        channels = max(1, min(2, int(channels or 1)))
        if channels > 1:
            usable = (raw.size // channels) * channels
            dry = raw[:usable].reshape(-1, channels)
        else:
            dry = raw.reshape(-1)
        live_window = 1.20
        max_n = int(sample_rate * live_window)
        if dry.shape[0] > max_n:
            dry = dry[-max_n:]
        if dry.shape[0] < int(sample_rate * 0.34):
            raise ValueError("DJ Gradient is still building its current listening window")

        clean_params = {}
        for name, bounds in PARAM_BOUNDS.items():
            lo, hi = bounds
            try:
                clean_params[name] = max(lo, min(hi, float(params.get(name, (lo + hi) * 0.5))))
            except Exception:
                clean_params[name] = (lo + hi) * 0.5

        names = list(DJ_NODE_SPECS)
        rendered = [self._render_variant(dry, int(sample_rate), clean_params)]

        seed = (zlib.crc32(str(prompt).encode("utf-8")) + int(cycle) * 2654435761) & 0xFFFFFFFF
        rng = np.random.default_rng(seed)
        signs = rng.choice(np.asarray([-1.0, 1.0], dtype=np.float64), size=len(names), replace=True)
        pminus = dict(clean_params); pplus = dict(clean_params); denoms = {}
        c = 0.032
        for i, name in enumerate(names):
            lo, hi = PARAM_BOUNDS[name]; span = max(1e-9, hi - lo)
            center = float(clean_params[name]); delta = float(signs[i])
            minus = max(lo, min(hi, center - span * c * delta))
            plus = max(lo, min(hi, center + span * c * delta))
            pminus[name] = minus; pplus[name] = plus
            denoms[name] = (plus - minus) / span
        rendered.append(self._render_variant(dry, int(sample_rate), pminus))
        rendered.append(self._render_variant(dry, int(sample_rate), pplus))

        coord = None
        coord_values = None
        valid_coords = [n for n in nodes if n in DJ_NODE_SPECS and n in PARAM_BOUNDS]
        if valid_coords and int(cycle) % 3 == 0:
            coord = valid_coords[0]
            lo, hi = PARAM_BOUNDS[coord]; span = max(1e-9, hi - lo)
            step = span * float(DJ_NODE_SPECS[coord].get("step_frac", 0.045))
            center = float(clean_params[coord])
            minus = max(lo, center - step); plus = min(hi, center + step)
            cm = dict(clean_params); cp = dict(clean_params)
            cm[coord] = minus; cp[coord] = plus
            rendered.append(self._render_variant(dry, int(sample_rate), cm))
            rendered.append(self._render_variant(dry, int(sample_rate), cp))
            coord_values = (minus, plus, span)

        mono_arrays = [np.mean(y[:, :2], axis=1).astype(np.float32, copy=False) if getattr(y, "ndim", 1) == 2 else y for y in rendered]
        scores, affinity, profiles, elapsed = self._score_prompt_audio_batch(prompt, mono_arrays, int(sample_rate))
        width_aff = float(affinity.get("width", 0.0) or 0.0)
        hyp_aff = float(affinity.get("hypnotic", 0.0) or 0.0)
        physical = self._production_objective(rendered, affinity, int(sample_rate))
        align = self._feature_alignment_objective(affinity, profiles)
        augmented = []
        for i, (score, y) in enumerate(zip(scores, rendered)):
            side_ratio, decor = self._stereo_character(y)
            # Dual critic: CLAP remains dominant semantic judge; high-SNR physical
            # production descriptors and feature-alignment make small-but-real DSP
            # changes visible to the gradient instead of disappearing in CLAP noise.
            extra = 0.16 * float(physical[i]) if i < len(physical) else 0.0
            extra += 0.075 * float(align[i]) if i < len(align) else 0.0
            augmented.append(float(score) + extra + 0.024 * width_aff * side_ratio + 0.011 * hyp_aff * decor)
        scores = np.asarray(augmented, dtype=np.float32)
        base_score = float(scores[0])
        diff = float(scores[2] - scores[1])
        gradients = {}
        for name in names:
            denom = float(denoms.get(name, 0.0))
            gradients[name] = float(diff / denom) if abs(denom) >= 1e-7 else 0.0

        coordinate_gradients = {}
        if coord is not None and coord_values is not None:
            minus, plus, span = coord_values
            denom = max(1e-7, (plus - minus) / span)
            exact = float((scores[4] - scores[3]) / denom)
            coordinate_gradients[coord] = exact
            gradients[coord] = 0.82 * exact + 0.18 * gradients.get(coord, 0.0)

        detail_prior, detail_model, detail_conf = self._detail_prompt_prior(prompt)
        return {
            "ok": True,
            "prompt": self._canonical_prompt(prompt),
            "model": MODEL_ID,
            "device": self.device,
            "score": base_score,
            "gradients": gradients,
            "coordinate_gradients": coordinate_gradients,
            "feature_affinity": affinity,
            "current_feature_state": profiles[0] if profiles else {},
            "dsp_prompt_prior": detail_prior,
            "detail_model": detail_model,
            "detail_confidence": float(detail_conf),
            "nodes": {name: float(clean_params[name]) for name in names},
            "inference_ms": round(float(elapsed), 1),
            "analysis_window_ms": round(1000.0 * live_window, 1),
            "render_count": len(rendered),
            "critic": "CLAP + production-physics",
        }


    def dsp50_gradient(
        self, prompt: str, dry_b64: str, sample_rate: int, params: dict[str, Any],
        coordinate_nodes: list[str], cycle: int = 0, channels: int = 1,
    ) -> dict[str, Any]:
        """Estimate a current-moment prompt-conditioned DSP gradient with sparse two-sided SPSA.

        A 101-render coordinate sweep is far too stale for a DJ controller. On CUDA
        CUDA normally uses one antithetic SPSA direction + one exact calibration
        coordinate (5 renders), with a second SPSA direction every fourth cycle
        (7 renders) to recalibrate the stochastic field. CPU fallback stays at five
        renders. The optimizer carries missing information forward with adaptive
        moments, favoring fresher answers over exhaustive but stale sweeps.
        """
        import numpy as np
        import zlib
        from realtime_native_engine import (
            DEFAULT_PARAMS, PARAM_BOUNDS, DSP50_SPECS,
            DSP50_SPSA_DIRECTIONS, DSP50_SPSA_FRAC,
            studio_dsp_render_params, STUDIO_DSP_AUTHORITY,
        )

        raw = self._decode_audio(dry_b64)
        channels = max(1, min(2, int(channels or 1)))
        if channels > 1:
            usable = (raw.size // channels) * channels
            dry = raw[:usable].reshape(-1, channels)
        else:
            dry = raw.reshape(-1)
        live_window = 0.78
        max_n = int(sample_rate * live_window)
        if dry.shape[0] > max_n:
            dry = dry[-max_n:]
        if dry.shape[0] < int(sample_rate * 0.38):
            raise ValueError("50D Gradient is still building its current listening window")

        clean = dict(DEFAULT_PARAMS)
        for name in DSP50_SPECS:
            lo, hi = PARAM_BOUNDS[name]
            try:
                clean[name] = max(lo, min(hi, float(params.get(name, DEFAULT_PARAMS[name]))))
            except Exception:
                clean[name] = DEFAULT_PARAMS[name]
        # Hidden safety trim is recomputed by the live engine, not optimized.
        clean["output_db"] = float(params.get("output_db", 0.0) or 0.0)

        rendered = [self._render_variant(dry, int(sample_rate), studio_dsp_render_params(clean, authority=STUDIO_DSP_AUTHORITY, mode="dsp50"))]
        tags = [("base", None)]

        # Build the music-domain target before probing. v29 perturbed all 50
        # coordinates in each SPSA direction, which is fast but noisy. v30 probes a
        # prompt-conditioned trust region: the strongest semantic nodes plus a few
        # rotating exploration coordinates. Fewer simultaneous actuators produce a
        # much cleaner local derivative without touching the realtime callback.
        detail_prior, detail_model, detail_conf = self._detail_prompt_prior(prompt)
        names_all = list(DSP50_SPECS)
        ranked_prior = sorted(names_all, key=lambda n: abs(float(detail_prior.get(n, 0.0))), reverse=True)
        semantic_nodes = [n for n in ranked_prior if abs(float(detail_prior.get(n, 0.0))) >= 0.075][:12]
        explore_start = (int(cycle) * 4) % max(1, len(names_all))
        exploration = [names_all[(explore_start + j) % len(names_all)] for j in range(4)]
        names = []
        for n in semantic_nodes + list(coordinate_nodes or []) + exploration:
            if n in DSP50_SPECS and n not in names:
                names.append(n)
        if len(names) < 8:
            for n in ranked_prior:
                if n not in names:
                    names.append(n)
                if len(names) >= 8:
                    break
        names = names[:16]

        # Deterministic per prompt/cycle keeps experiments reproducible but changes
        # the Rademacher basis every pass.
        seed = (zlib.crc32(str(prompt).encode("utf-8")) + int(cycle) * 2654435761) & 0xFFFFFFFF
        rng = np.random.default_rng(seed)
        spsa_meta = []
        # Faster dynamic field: fewer renders, more frequent updates. A slightly
        # larger perturbation improves SNR for CLAP's noisy black-box objective.
        # Fast pass most cycles, periodic extra SPSA direction for calibration.
        # AdaBelief carries the stochastic field between cycles, so spending seven
        # renders every single pass is less useful than getting a fresher five-render
        # answer and occasionally taking a higher-confidence seven-render sweep.
        directions = 1
        if self.device == "cuda" and (int(cycle) % 4 == 0):
            directions = 2
        c = max(float(DSP50_SPSA_FRAC), 0.028)
        for k in range(int(directions)):
            signs = rng.choice(np.asarray([-1.0, 1.0], dtype=np.float64), size=len(names), replace=True)
            pminus = dict(clean); pplus = dict(clean)
            denoms = {}
            for i, name in enumerate(names):
                lo, hi = PARAM_BOUNDS[name]; span = max(1e-9, hi - lo)
                center = float(clean[name]); delta = float(signs[i])
                minus = max(lo, min(hi, center - span * c * delta))
                plus = max(lo, min(hi, center + span * c * delta))
                pminus[name] = minus; pplus[name] = plus
                denoms[name] = (plus - minus) / span
            rendered.append(self._render_variant(dry, int(sample_rate), studio_dsp_render_params(pminus, authority=STUDIO_DSP_AUTHORITY, mode="dsp50"))); tags.append(("spsa_minus", k))
            rendered.append(self._render_variant(dry, int(sample_rate), studio_dsp_render_params(pplus, authority=STUDIO_DSP_AUTHORITY, mode="dsp50"))); tags.append(("spsa_plus", k))
            spsa_meta.append((signs, denoms))

        coords = [n for n in coordinate_nodes if n in DSP50_SPECS][:1] if int(cycle) % 2 == 0 else []
        coord_meta = []
        for name in coords:
            lo, hi = PARAM_BOUNDS[name]; span = max(1e-9, hi - lo)
            step = span * float(DSP50_SPECS[name].get("step_frac", 0.04))
            center = float(clean[name])
            minus = max(lo, center - step); plus = min(hi, center + step)
            pminus = dict(clean); pminus[name] = minus
            pplus = dict(clean); pplus[name] = plus
            rendered.append(self._render_variant(dry, int(sample_rate), studio_dsp_render_params(pminus, authority=STUDIO_DSP_AUTHORITY, mode="dsp50"))); tags.append(("coord_minus", name))
            rendered.append(self._render_variant(dry, int(sample_rate), studio_dsp_render_params(pplus, authority=STUDIO_DSP_AUTHORITY, mode="dsp50"))); tags.append(("coord_plus", name))
            coord_meta.append((name, (plus - minus) / span))

        mono = [np.mean(y[:, :2], axis=1).astype(np.float32, copy=False) if getattr(y, "ndim", 1) == 2 else y for y in rendered]
        scores, affinity, profiles, elapsed = self._score_prompt_audio_batch(prompt, mono, int(sample_rate))

        # Studio dual-critic objective: CLAP semantics + production-physics
        # descriptors. The second term is intentionally loudness-invariant and
        # gives EQ/dynamics/space coordinates a much cleaner derivative signal.
        width_aff = float(affinity.get("width", 0.0) or 0.0)
        hyp_aff = float(affinity.get("hypnotic", 0.0) or 0.0)
        space_aff = float(affinity.get("space", 0.0) or 0.0)
        physical = self._production_objective(rendered, affinity, int(sample_rate))
        align = self._feature_alignment_objective(affinity, profiles)
        augmented = []
        for i, (score, y) in enumerate(zip(scores, rendered)):
            side_ratio, decor = self._stereo_character(y)
            extra = 0.18 * float(physical[i]) if i < len(physical) else 0.0
            extra += 0.085 * float(align[i]) if i < len(align) else 0.0
            augmented.append(
                float(score) + extra
                + 0.024 * width_aff * side_ratio
                + 0.012 * hyp_aff * decor
                + 0.008 * space_aff * side_ratio
            )
        scores = np.asarray(augmented, dtype=np.float32)
        base_score = float(scores[0])

        # Parse the runtime ordering: base, N*(minus,plus), M*(minus,plus).
        cursor = 1
        spsa_acc = {name: 0.0 for name in names}
        spsa_count = {name: 0 for name in names}
        for _k, (_signs, denoms) in enumerate(spsa_meta):
            minus_score = float(scores[cursor]); plus_score = float(scores[cursor + 1]); cursor += 2
            diff = plus_score - minus_score
            for name in names:
                denom = float(denoms.get(name, 0.0))
                if abs(denom) < 1e-6:
                    continue
                spsa_acc[name] += diff / denom
                spsa_count[name] += 1
        spsa_gradients = {
            name: float(spsa_acc[name] / max(1, spsa_count[name]))
            for name in names
        }

        coordinate_gradients = {}
        for name, denom in coord_meta:
            minus_score = float(scores[cursor]); plus_score = float(scores[cursor + 1]); cursor += 2
            if abs(float(denom)) >= 1e-6:
                coordinate_gradients[name] = float((plus_score - minus_score) / float(denom))

        return {
            "ok": True,
            "prompt": self._canonical_prompt(prompt),
            "model": MODEL_ID,
            "device": self.device,
            "score": base_score,
            "spsa_gradients": spsa_gradients,
            "gradient_nodes": list(names),
            "coordinate_gradients": coordinate_gradients,
            "coordinate_nodes": coords,
            "feature_affinity": affinity,
            "current_feature_state": profiles[0] if profiles else {},
            "dsp_prompt_prior": detail_prior,
            "detail_model": detail_model,
            "detail_confidence": float(detail_conf),
            "inference_ms": round(float(elapsed), 1),
            "render_count": len(rendered),
            "spsa_directions": int(directions),
            "analysis_window_ms": round(1000.0 * live_window, 1),
            "critic": "CLAP + production-physics",
        }

    def score_pair(self, input_b64: str, output_b64: str, sample_rate: int) -> dict[str, Any]:
        with self.lock:
            if not self.ready or self.model is None or self.feature_extractor is None:
                raise RuntimeError("semantic model is still preparing")
            model = self.model
            feature_extractor = self.feature_extractor
            pos = self.text_pos
            neg = self.text_neg
            device = self.device

        import numpy as np
        import torch

        a = self._resample(self._decode_audio(input_b64), int(sample_rate), 48000)
        b = self._resample(self._decode_audio(output_b64), int(sample_rate), 48000)
        max_n = 48000 * 4
        if a.size > max_n:
            a = a[-max_n:]
        if b.size > max_n:
            b = b[-max_n:]
        rms_a = float(np.sqrt(np.mean(a * a) + 1e-12))
        rms_b = float(np.sqrt(np.mean(b * b) + 1e-12))
        if rms_a < 3e-5:
            raise ValueError("input window is silent")
        # Compare production character rather than simple loudness. Matching the
        # analysis RMS stops compression/output gain from fooling the semantic critic
        # into thinking a louder signal is automatically more energetic/punchy.
        target_rms = 0.10
        a = (a * min(4.0, target_rms / max(rms_a, 1e-6))).astype(np.float32, copy=False)
        b = (b * min(4.0, target_rms / max(rms_b, 1e-6))).astype(np.float32, copy=False)

        started = time.perf_counter()
        aud = feature_extractor([a, b], sampling_rate=48000, return_tensors="pt")
        with self.inference_lock:
            aud = self._to_device_inputs(dict(aud), device)
            with torch.inference_mode(), self._autocast_context(torch):
                ae = model.get_audio_features(**aud).float()
                ae = ae / ae.norm(dim=-1, keepdim=True).clamp_min(1e-9)
                scores = ae @ pos.float().T - ae @ neg.float().T
            values = scores.detach().float().cpu().numpy()
        elapsed = (time.perf_counter() - started) * 1000.0
        self.inference_ms = elapsed

        inp = {name: float(values[0, i]) for i, name in enumerate(self.names)}
        out = {name: float(values[1, i]) for i, name in enumerate(self.names)}
        delta = {name: out[name] - inp[name] for name in self.names}
        return {
            "ok": True,
            "model": MODEL_ID,
            "device": self.device,
            "input": inp,
            "output": out,
            "delta": delta,
            "inference_ms": round(elapsed, 1),
        }

    def health(self):
        with self.lock:
            return {
                "ok": True,
                "ready": bool(self.ready),
                "state": self.state,
                "error": None,
                "model": MODEL_ID,
                "device": self.device,
                "device_label": self.device_label,
                "detail_ready": bool(self.detail_ready),
                "detail_model": DETAIL_MODEL_ID if self.detail_ready else None,
                "detail_state": self.detail_state,
                "cuda_available": bool(self.cuda_available),
                "gpu": self.gpu_name or None,
                "loaded_at": self.loaded_at or None,
                "inference_ms": round(self.inference_ms, 1),
                # Diagnostic is intentionally separate from state/error so the UI
                # does not present a recoverable setup problem as a fatal error.
                "diagnostic": self.last_issue or None,
                "retry_count": int(self.retry_count),
            }


MODEL = SemanticModel()



# ---------------------------------------------------------------------------
# v31.18 GrooveTruth: ONE persistent inference thread + a memory self-guard.
# The field showed this worker growing to 60 GB of commit in about an hour
# (6.5 GB resident) and taking the whole machine down.  ThreadingHTTPServer
# spawns a fresh thread per request; every model call therefore ran on a new
# thread, which leaks per-thread allocator / runtime state on this stack.  All
# model work is now funnelled through a single long-lived thread; the commit
# size is logged every 20 inferences and the process exits cleanly (the launcher
# watchdog restarts it) before it can hurt the audio process.
# ---------------------------------------------------------------------------
import queue as _queue
import gc as _gc
import ctypes as _ctypes

_INFER_Q: "_queue.Queue" = _queue.Queue()
_INFER_COUNT = 0
SEMANTIC_MEM_LIMIT_MB = float(os.environ.get("JOY_SEMANTIC_MEM_LIMIT_MB", "9000") or 9000)


def _proc_commit_mb() -> float:
    try:
        class _PMC(_ctypes.Structure):
            _fields_ = [("cb", _ctypes.c_ulong), ("PageFaultCount", _ctypes.c_ulong),
                        ("PeakWorkingSetSize", _ctypes.c_size_t), ("WorkingSetSize", _ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", _ctypes.c_size_t), ("QuotaPagedPoolUsage", _ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", _ctypes.c_size_t), ("QuotaNonPagedPoolUsage", _ctypes.c_size_t),
                        ("PagefileUsage", _ctypes.c_size_t), ("PeakPagefileUsage", _ctypes.c_size_t)]
        pmc = _PMC(); pmc.cb = _ctypes.sizeof(_PMC)
        k32 = _ctypes.windll.kernel32
        h = k32.GetCurrentProcess()
        fn = getattr(k32, "K32GetProcessMemoryInfo", None)
        if fn is None:
            fn = _ctypes.windll.psapi.GetProcessMemoryInfo
        fn.argtypes = [_ctypes.c_void_p, _ctypes.POINTER(_PMC), _ctypes.c_ulong]
        fn.restype = _ctypes.c_int
        if fn(h, _ctypes.byref(pmc), pmc.cb):
            return float(pmc.PagefileUsage) / 1048576.0
    except Exception:
        pass
    return -1.0


def _inference_worker():
    global _INFER_COUNT
    while True:
        fn, box, ev = _INFER_Q.get()
        try:
            box["result"] = fn()
        except BaseException as exc:      # noqa: BLE001 - re-raised on the request thread
            box["error"] = exc
        finally:
            ev.set()
        _INFER_COUNT += 1
        if _INFER_COUNT % 20 == 0:
            _gc.collect()
            mb = _proc_commit_mb()
            print(f"[semantic] commit={mb:.0f} MB after {_INFER_COUNT} inferences", file=sys.stderr, flush=True)
            if mb > SEMANTIC_MEM_LIMIT_MB:
                print(f"[semantic] commit {mb:.0f} MB exceeds {SEMANTIC_MEM_LIMIT_MB:.0f} MB - exiting for a clean restart", file=sys.stderr, flush=True)
                os._exit(3)


threading.Thread(target=_inference_worker, daemon=True, name="semantic-inference").start()


def run_inference(fn, timeout: float = 40.0):
    """Run fn() on the single inference thread; raise its exception here."""
    box: dict = {}
    ev = threading.Event()
    _INFER_Q.put((fn, box, ev))
    if not ev.wait(timeout):
        raise TimeoutError("semantic inference queue timeout")
    if "error" in box:
        raise box["error"]
    return box["result"]


class Handler(BaseHTTPRequestHandler):
    server_version = "JoyMetricSemantic/1.6"

    def log_message(self, fmt, *args):
        print("[semantic] " + (fmt % args), flush=True)

    def _json(self, code: int, payload: dict[str, Any]):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            h = MODEL.health()
            try:
                h["commit_mb"] = round(_proc_commit_mb(), 0); h["inferences"] = int(_INFER_COUNT)
            except Exception:
                pass
            self._json(200, h)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.rstrip("/")
        if path == "/device":
            try:
                n = int(self.headers.get("Content-Length") or "0")
                body = json.loads(self.rfile.read(n).decode("utf-8")) if n > 0 else {}
                result = MODEL.set_device(str(body.get("device") or "cpu"))
                self._json(200, result)
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc), **MODEL.health()})
            return
        if path not in {"/score", "/dj-gradient", "/dsp50-gradient", "/prompt-profile", "/agent-state"}:
            self._json(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or "0")
            limit = 14 * 1024 * 1024 if path in {"/dj-gradient", "/dsp50-gradient"} else 8 * 1024 * 1024
            if n <= 0 or n > limit:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            if not MODEL.health().get("ready"):
                self._json(503, MODEL.health())
                return
            if _INFER_Q.qsize() >= 3:
                self._json(503, {"ok": False, "state": "Semantic AI busy (inference queue full)", **MODEL.health()})
                return
            if path == "/score":
                result = run_inference(lambda: MODEL.score_pair(
                    str(body.get("input_b64") or ""),
                    str(body.get("output_b64") or ""),
                    int(body.get("sample_rate") or 48000),
                ))
            elif path == "/agent-state":
                result = run_inference(lambda: MODEL.agent_state(
                    str(body.get("audio_b64") or ""),
                    int(body.get("sample_rate") or 48000),
                    str(body.get("prompt") or ""),
                ))
            elif path == "/prompt-profile":
                result = run_inference(lambda: MODEL.prompt_profile(str(body.get("prompt") or "")))
            elif path == "/dsp50-gradient":
                result = run_inference(lambda: MODEL.dsp50_gradient(
                    str(body.get("prompt") or ""),
                    str(body.get("dry_b64") or ""),
                    int(body.get("sample_rate") or 48000),
                    dict(body.get("params") or {}),
                    list(body.get("coordinate_nodes") or []),
                    int(body.get("cycle") or 0),
                    int(body.get("channels") or 1),
                ))
            else:
                result = run_inference(lambda: MODEL.dj_gradient(
                    str(body.get("prompt") or ""),
                    str(body.get("dry_b64") or ""),
                    int(body.get("sample_rate") or 48000),
                    dict(body.get("params") or {}),
                    list(body.get("nodes") or []),
                    int(body.get("cycle") or 0),
                    int(body.get("channels") or 1),
                ))
            self._json(200, result)
        except ValueError as exc:
            self._json(422, {"ok": False, "state": str(exc), **MODEL.health()})
        except Exception as exc:
            # Do not poison global model state because of one bad analysis window.
            print(f"[semantic] analysis issue: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            self._json(503, {"ok": False, "state": "Semantic AI retrying analysis", **MODEL.health()})


if __name__ == "__main__":
    print(f"JoyMetric Continuous DJ 15D semantic server on http://{HOST}:{PORT} · model={MODEL_ID} · CPU audio-priority current-moment critic", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
