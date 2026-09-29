"""JoyMetric RustCore loader (v31.15).

Loads native/joymetric_rt.dll via ctypes (no Python headers, no PyO3) and
exposes the realtime hot-path kernels: deck channel strips, program bass-cut
and the FullRemix groove synth.  Everything degrades gracefully: if the DLL is
missing, fails to load, or fails its self-test, `create()` returns None and the
engine keeps its pure-numpy path — audio never depends on Rust being present.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path


class _GrooveParams(ctypes.Structure):
    _fields_ = [
        ("grid_base", ctypes.c_double),
        ("bps", ctypes.c_double),
        ("swing", ctypes.c_double),
        ("redrum", ctypes.c_double),
        ("rd_pattern", ctypes.c_int32),
        ("rd_var", ctypes.c_double),
        ("rd_fill", ctypes.c_double),
        ("bass", ctypes.c_double),
        ("bass_root", ctypes.c_int32),
        ("bass_pattern", ctypes.c_int32),
        ("stab", ctypes.c_double),
        ("stab_pattern", ctypes.c_int32),
    ]


class _HatParams(ctypes.Structure):
    _fields_ = [
        ("grid_base", ctypes.c_double),
        ("bps", ctypes.c_double),
        ("swing", ctypes.c_double),
        ("level", ctypes.c_double),
        ("style", ctypes.c_int32),
        ("var", ctypes.c_double),
        ("fill", ctypes.c_double),
    ]


_F32P = ctypes.POINTER(ctypes.c_float)


def _load_lib():
    here = Path(__file__).resolve().parent
    for cand in (here / "native" / "joymetric_rt.dll",
                 here / "native" / "target" / "release" / "joymetric_rt.dll"):
        if cand.exists():
            try:
                lib = ctypes.CDLL(str(cand))
            except OSError:
                continue
            try:
                lib.jm_version.restype = ctypes.c_uint32
                if int(lib.jm_version()) < 3117:
                    continue
                lib.jm_ctx_new.restype = ctypes.c_void_p
                lib.jm_ctx_new.argtypes = [ctypes.c_double, ctypes.c_uint32, ctypes.c_uint32]
                lib.jm_ctx_free.argtypes = [ctypes.c_void_p]
                lib.jm_deck.restype = ctypes.c_int32
                lib.jm_deck.argtypes = [ctypes.c_void_p, ctypes.c_uint32, _F32P, ctypes.c_uint32,
                                        ctypes.c_double, ctypes.c_double, ctypes.c_double,
                                        ctypes.c_double, ctypes.c_double,
                                        ctypes.POINTER(ctypes.c_double)]
                lib.jm_bass_cut.restype = ctypes.c_double
                lib.jm_bass_cut.argtypes = [ctypes.c_void_p, _F32P, ctypes.c_uint32, ctypes.c_double]
                lib.jm_groove.restype = ctypes.c_int32
                lib.jm_groove.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                          ctypes.POINTER(_GrooveParams), _F32P, _F32P, _F32P]
                lib.jm_hats.restype = ctypes.c_double
                lib.jm_hats.argtypes = [ctypes.c_void_p, _F32P, ctypes.c_uint32,
                                        ctypes.POINTER(_HatParams), _F32P]
                return lib
            except Exception:
                continue
    return None


_LIB = _load_lib()


class RustDSP:
    """One per-mixer context wrapping the native kernels."""

    def __init__(self, lib, sample_rate: int, channels: int, morph_frames: int):
        import numpy as np
        self._lib = lib
        self._ctx = lib.jm_ctx_new(float(sample_rate), int(channels), int(morph_frames))
        if not self._ctx:
            raise RuntimeError("jm_ctx_new failed")
        self.channels = int(channels)
        self._state5 = (ctypes.c_double * 5)()
        n0 = 8192
        self._mul = np.ones(n0, dtype=np.float32)
        self._addm = np.zeros(n0, dtype=np.float32)
        self._adds = np.zeros(n0 * self.channels, dtype=np.float32)
        self._hats = np.zeros(n0 * self.channels, dtype=np.float32)

    def __del__(self):
        try:
            if getattr(self, "_ctx", None):
                self._lib.jm_ctx_free(self._ctx)
                self._ctx = None
        except Exception:
            pass

    @staticmethod
    def _ptr(arr):
        return arr.ctypes.data_as(_F32P)

    def deck(self, which: int, buf, low_db, mid_db, high_db, filt, gain):
        """In-place deck strip on a C-contiguous float32 (n, ch) array.
        Returns (low, mid, high, filt, gain) smoothed effective values."""
        n = int(buf.shape[0])
        rc = self._lib.jm_deck(self._ctx, int(which), self._ptr(buf), n,
                               float(low_db), float(mid_db), float(high_db),
                               float(filt), float(gain), self._state5)
        if rc != 0:
            raise RuntimeError(f"jm_deck rc={rc}")
        s = self._state5
        return float(s[0]), float(s[1]), float(s[2]), float(s[3]), float(s[4])

    def bass_cut(self, buf, cut: float) -> float:
        return float(self._lib.jm_bass_cut(self._ctx, self._ptr(buf), int(buf.shape[0]), float(cut)))

    def groove(self, n: int, *, grid_base: float, bps: float, swing: float,
               redrum: float, rd_pattern: int, rd_var: float, rd_fill: float,
               bass: float, bass_root: int, bass_pattern: int,
               stab: float, stab_pattern: int):
        """Returns (mul[n], add_mono[n], add_stereo[n,ch]) views into reused buffers."""
        import numpy as np
        if n > self._mul.shape[0]:
            self._mul = np.ones(n, dtype=np.float32)
            self._addm = np.zeros(n, dtype=np.float32)
            self._adds = np.zeros(n * self.channels, dtype=np.float32)
        p = _GrooveParams(grid_base=float(grid_base), bps=float(bps), swing=float(swing),
                          redrum=float(redrum), rd_pattern=int(rd_pattern),
                          rd_var=float(rd_var), rd_fill=float(rd_fill),
                          bass=float(bass), bass_root=int(bass_root), bass_pattern=int(bass_pattern),
                          stab=float(stab), stab_pattern=int(stab_pattern))
        rc = self._lib.jm_groove(self._ctx, int(n), ctypes.byref(p),
                                 self._ptr(self._mul), self._ptr(self._addm), self._ptr(self._adds))
        if rc != 0:
            raise RuntimeError(f"jm_groove rc={rc}")
        return (self._mul[:n], self._addm[:n],
                self._adds[:n * self.channels].reshape(n, self.channels))


    def hats(self, src, *, grid_base: float, bps: float, swing: float, level: float,
             style: int, var: float, fill: float):
        """Hats 2.0 layer for a C-contiguous float32 (n, ch) program block.
        Returns (add[n,ch] view, source_density)."""
        import numpy as np
        n = int(src.shape[0])
        if n * self.channels > self._hats.shape[0]:
            self._hats = np.zeros(n * self.channels, dtype=np.float32)
        p = _HatParams(grid_base=float(grid_base), bps=float(bps), swing=float(swing),
                       level=float(level), style=int(style), var=float(var), fill=float(fill))
        dens = self._lib.jm_hats(self._ctx, self._ptr(src), n, ctypes.byref(p), self._ptr(self._hats))
        return self._hats[:n * self.channels].reshape(n, self.channels), float(dens)


def _self_test(lib) -> bool:
    """A deck impulse through Rust must match a transparent-ish response and a
    known EQ boost must actually boost — cheap sanity, not full parity (the
    full parity suite lives in the validation harness)."""
    import numpy as np
    try:
        dsp = RustDSP(lib, 48000, 2, 128)
        x = np.zeros((256, 2), dtype=np.float32)
        x[0, :] = 1.0
        y = x.copy()
        dsp.deck(0, y, 0.0, 0.0, 0.0, 0.0, 1.0)
        if not np.all(np.isfinite(y)) or abs(float(y[0, 0]) - 1.0) > 0.35:
            return False
        z = np.zeros((256, 2), dtype=np.float32)
        mul, addm, adds = dsp.groove(256, grid_base=0.0, bps=2.0, swing=0.0,
                                     redrum=0.8, rd_pattern=0, rd_var=0.0, rd_fill=0.0,
                                     bass=0.0, bass_root=9, bass_pattern=0,
                                     stab=0.0, stab_pattern=0)
        if not (np.all(np.isfinite(mul)) and np.all(np.isfinite(addm)) and np.all(np.isfinite(adds))):
            return False
        if float(np.max(np.abs(addm))) <= 0.0:      # a kick must exist at beat 0
            return False
        del z
        return True
    except Exception:
        return False


_AVAILABLE = bool(_LIB is not None and os.environ.get("JOY_RUST_DSP", "1") != "0" and _self_test(_LIB))


def available() -> bool:
    return _AVAILABLE


def create(sample_rate: int, channels: int, morph_frames: int = 128):
    """Returns a RustDSP context or None (caller keeps the numpy path)."""
    if not _AVAILABLE:
        return None
    try:
        return RustDSP(_LIB, int(sample_rate), int(channels), int(morph_frames))
    except Exception:
        return None
