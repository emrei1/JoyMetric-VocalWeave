"""JoyMetric v31.26 - LineFinder: the lines that are playing in the record RIGHT NOW, one by one.

A 2-bar window of the lookahead is transcribed with Spotify's Basic Pitch (polyphonic note
transcription CNN, ONNX on the CPU, ~1.2 s for 6 s of audio) and split into voices on the beat grid:
  bass   the lowest sounding note per sixteenth (below MIDI 52 or the lowest voice),
  top    the highest sounding note per sixteenth (the lead / vocal melody line, "skyline"),
  inner  everything in between as pitch-class activations (chords, keys, guitars),
  drums  kick / snare / hat strengths per sixteenth (GrooveNet features, when given).
The result is both a symbolic picture of the record (for the generative models) and the per-cell
context vector MelodyNet was trained on (`context_matrix`).
"""
from __future__ import annotations

import os
import tempfile
import threading
import time

import numpy as np

N_CTX = 13 + 12 + 13 + 3 + 4 + 12 + 1      # bass pc(13) + inner pcs(12) + top pc(13) + drums(3) + position(4) + key pcs(12) + tempo(1)
N_CTX3 = 135                                # v31.27 SongMind: full-resolution 4-bar context (see context_v3)
BASS_LO, TOP_LO = 28, 48


class LineFinder:
    def __init__(self, sr: int = 48000):
        self.sr = int(sr); self._model = None; self._lock = threading.Lock(); self.ready = False; self.error = ""; self.last_ms = 0
        try:
            from basic_pitch import ICASSP_2022_MODEL_PATH
            from basic_pitch.inference import Model
            self._model = Model(ICASSP_2022_MODEL_PATH); self.ready = True
            try:                                          # SMOOTH: two ONNX threads - the audio callback keeps its cores
                import onnxruntime as ort
                so = ort.SessionOptions(); so.intra_op_num_threads = 2; so.inter_op_num_threads = 1
                sess = getattr(self._model, "model", None)
                if sess is not None and hasattr(sess, "get_inputs"):
                    self._model.model = ort.InferenceSession(str(ICASSP_2022_MODEL_PATH), sess_options=so, providers=["CPUExecutionProvider"])
            except Exception:
                pass
        except Exception as exc:
            self.error = str(exc)[:120]

    # ------------------------------------------------------------------ transcription
    def transcribe(self, audio, sr=None):
        """[(start_s, end_s, midi, amplitude)] for a mono / stereo float32 clip."""
        if not self.ready:
            return []
        sr = int(sr or self.sr)
        x = np.asarray(audio, dtype=np.float32)
        if x.ndim == 2:
            x = x.mean(axis=1)
        if x.size < sr // 2:
            return []
        import soundfile as sf
        from basic_pitch.inference import predict
        t0 = time.time()
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="joy_lf_"); os.close(fd)
        try:
            sf.write(path, x / (float(np.abs(x).max()) + 1e-6) * 0.9, sr)
            with self._lock:
                _mo, _midi, events = predict(path, self._model, onset_threshold=0.5, frame_threshold=0.3, minimum_note_length=70, minimum_frequency=40, maximum_frequency=2100)
        finally:
            try:
                os.remove(path)
            except Exception:
                pass
        self.last_ms = int((time.time() - t0) * 1000)
        return [(float(s), float(e), int(m), float(a)) for s, e, m, a, _b in events]

    # ------------------------------------------------------------------ voices on the grid
    @staticmethod
    def split_voices(notes, beat_s: float, beats: int = 8, cells_per_beat: int = 4, t0: float = 0.0, bass_max: int = 52):
        """notes: [(start_s, end_s, midi, amp)] with t=0 at the window start.  Returns a dict of per-cell
        arrays: bass (midi or -1), top (midi or -1), inner_pcs (32, 12) activations, poly (count), and the
        note lists per voice as [(start_beat, dur_beats, midi)]."""
        n_cells = beats * cells_per_beat; cell_s = beat_s / cells_per_beat
        bass = -np.ones(n_cells, dtype=int); top = -np.ones(n_cells, dtype=int); inner = np.zeros((n_cells, 12), np.float32); poly = np.zeros(n_cells, dtype=int)
        sounding = [[] for _ in range(n_cells)]
        for (s, e, m, a) in notes:
            c0 = int(np.floor((s - t0) / cell_s + 0.25)); c1 = int(np.ceil((e - t0) / cell_s - 0.25))
            for c in range(max(0, c0), min(n_cells, max(c0 + 1, c1))):
                sounding[c].append((int(m), float(a)))
        for c in range(n_cells):
            if not sounding[c]:
                continue
            pitches = sorted(set(m for m, a in sounding[c]))
            poly[c] = len(pitches)
            lo = pitches[0]
            if lo < bass_max or len(pitches) >= 3:
                bass[c] = lo
            hi = pitches[-1]
            if hi >= 55 and (hi != lo or len(pitches) == 1 and hi >= 60):
                top[c] = hi
            for m in pitches:
                if m != bass[c] and m != top[c]:
                    inner[c, m % 12] = 1.0
        def to_notes(line):
            out = []
            for c, m in enumerate(line):
                if m < 0:
                    continue
                if out and out[-1][2] == m and abs(out[-1][0] + out[-1][1] - c / cells_per_beat) < 1e-6:
                    out[-1][1] += 1.0 / cells_per_beat
                else:
                    out.append([c / cells_per_beat, 1.0 / cells_per_beat, int(m)])
            return [tuple(x) for x in out]
        return {"bass": bass, "top": top, "inner": inner, "poly": poly, "bass_notes": to_notes(bass), "top_notes": to_notes(top),
                "inner_notes": [((s - t0) / beat_s, (e - s) / beat_s, int(m)) for (s, e, m, a) in notes if 52 <= m and m not in set(bass) and m not in set(top)]}

    # ------------------------------------------------------------------ the context MelodyNet sees
    @staticmethod
    def context_matrix(voices, drums=None, key_pcs=None, tempo: float = 120.0, cells_per_beat: int = 4):
        """(32, N_CTX) float32: bass pc one-hot (12 + rest), inner pc activations, top pc one-hot (12 + rest),
        kick / snare / hat strengths, position in bar (4 one-hot of the beat), key pitch classes, tempo."""
        n = len(voices["bass"]); X = np.zeros((n, N_CTX), np.float32)
        for c in range(n):
            b = int(voices["bass"][c]); t = int(voices["top"][c])
            X[c, 12 if b < 0 else b % 12] = 1.0
            X[c, 13:25] = voices["inner"][c]
            X[c, 25 + (12 if t < 0 else t % 12)] = 1.0
            if drums is not None and len(drums) > c:
                X[c, 38:41] = np.asarray(drums[c][:3], dtype=np.float32)
            X[c, 41 + ((c // cells_per_beat) % 4)] = 1.0
            if key_pcs:
                for p in key_pcs:
                    X[c, 45 + int(p) % 12] = 1.0
            X[c, 57] = (float(tempo) - 120.0) / 60.0
        return X


    # ------------------------------------------------------------------ v31.27 SongMind: full-resolution 4-bar context
    @staticmethod
    def split_voices_onsets(notes, beat_s: float, beats: int = 16, cells_per_beat: int = 4, bass_max: int = 52):
        """like split_voices over `beats` beats, plus onset flags and inner counts per cell (needed by context_v3)."""
        n_cells = beats * cells_per_beat; cell_s = beat_s / cells_per_beat
        bass = -np.ones(n_cells, dtype=int); top = -np.ones(n_cells, dtype=int); inner = np.zeros((n_cells, 12), np.float32)
        inner_cnt = np.zeros(n_cells, dtype=int); on_b = np.zeros(n_cells, np.float32); on_t = np.zeros(n_cells, np.float32); on_i = np.zeros(n_cells, np.float32)
        sounding = [[] for _ in range(n_cells)]
        for (s, e, m, a) in notes:
            c0 = int(np.floor(s / cell_s + 0.25)); c1 = int(np.ceil(e / cell_s - 0.25))
            for c in range(max(0, c0), min(n_cells, max(c0 + 1, c1))):
                sounding[c].append((int(m), float(a), c == max(0, c0)))
        for c in range(n_cells):
            if not sounding[c]:
                continue
            pitches = sorted(set(m for m, a, o in sounding[c])); lo = pitches[0]; hi = pitches[-1]
            if lo < bass_max or len(pitches) >= 3:
                bass[c] = lo
            if hi >= 55 and (hi != lo or (len(pitches) == 1 and hi >= 60)):
                top[c] = hi
            for m, a, o in sounding[c]:
                if m == bass[c]:
                    on_b[c] = max(on_b[c], float(o))
                elif m == top[c]:
                    on_t[c] = max(on_t[c], float(o))
                else:
                    inner[c, m % 12] = 1.0; inner_cnt[c] += 1; on_i[c] = max(on_i[c], float(o))
        return {"bass": bass, "top": top, "inner": inner, "inner_cnt": inner_cnt, "on_b": on_b, "on_t": on_t, "on_i": on_i}

    @staticmethod
    def context_v3(v, drums=None, key_pcs=None, tempo: float = 120.0, loud=None, cells: int = 64):
        """(64, 135): bass MIDI (rest + 40 from 28), top MIDI (rest + 40 from 48), inner pcs, inner count,
        onsets (bass/top/inner), kick/snare/hat, cell-in-bar (16), bar-in-window (4), key pcs, tempo, loudness."""
        X = np.zeros((cells, N_CTX3), np.float32)
        for c in range(cells):
            b = int(v["bass"][c]) if c < len(v["bass"]) else -1; t = int(v["top"][c]) if c < len(v["top"]) else -1
            X[c, 0 if b < 0 else 1 + min(39, max(0, b - BASS_LO))] = 1.0
            X[c, 41 + (0 if t < 0 else 1 + min(39, max(0, t - TOP_LO)))] = 1.0
            if c < len(v["bass"]):
                X[c, 82:94] = v["inner"][c]; X[c, 94] = min(1.0, float(v["inner_cnt"][c]) / 4.0)
                X[c, 95] = float(v["on_b"][c]); X[c, 96] = float(v["on_t"][c]); X[c, 97] = float(v["on_i"][c])
            if drums is not None and len(drums) > c:
                X[c, 98:101] = np.asarray(drums[c][:3], dtype=np.float32)
            X[c, 101 + (c % 16)] = 1.0; X[c, 117 + min(3, c // 16)] = 1.0
            if key_pcs:
                for p in key_pcs:
                    X[c, 121 + int(p) % 12] = 1.0
            X[c, 133] = (float(tempo) - 120.0) / 60.0
            X[c, 134] = float(loud[c]) if loud is not None and len(loud) > c else 0.5
        return X

    def analyse_v3(self, audio, sr, bpm: float, drums=None, key_pcs=None):
        """4-bar window audio (from a downbeat) -> notes, voices with onsets, loudness per cell, the 135-dim context."""
        beat_s = 60.0 / max(40.0, float(bpm)); sr = int(sr or self.sr)
        notes = self.transcribe(audio, sr)
        v = self.split_voices_onsets(notes, beat_s)
        x = np.asarray(audio, dtype=np.float32); mono = x.mean(axis=1) if x.ndim == 2 else x
        cell_n = max(1, int(beat_s * sr / 4)); loud = np.zeros(64, np.float32)
        for c in range(64):
            seg = mono[c * cell_n:(c + 1) * cell_n]
            loud[c] = float(np.sqrt(np.mean(seg * seg) + 1e-12)) if seg.size else 0.0
        loud = loud / (float(loud.max()) + 1e-6)
        X = self.context_v3(v, drums=drums, key_pcs=key_pcs, tempo=float(bpm), loud=loud)
        top_notes = []
        for c, m in enumerate(v["top"]):
            if m < 0:
                continue
            if top_notes and top_notes[-1][2] == m and abs(top_notes[-1][0] + top_notes[-1][1] - c / 4.0) < 1e-6:
                top_notes[-1][1] += 0.25
            else:
                top_notes.append([c / 4.0, 0.25, int(m)])
        return {"notes": notes, "voices": v, "X": X, "loud": loud, "ms": self.last_ms, "top_notes": [tuple(q) for q in top_notes]}

    def analyse(self, audio, sr, bpm: float, drums=None, key_pcs=None):
        """window audio (2 bars from the downbeat) -> notes, voices, context matrix."""
        beat_s = 60.0 / max(40.0, float(bpm))
        notes = self.transcribe(audio, sr)
        voices = self.split_voices(notes, beat_s)
        X = self.context_matrix(voices, drums=drums, key_pcs=key_pcs, tempo=float(bpm))
        return {"notes": notes, "voices": voices, "X": X, "ms": self.last_ms}
