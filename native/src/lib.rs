//! JoyMetric RustCore (v31.15) — the realtime hot path in native code.
//!
//! Scope: the per-block, always-on DSP that used to run as many separate
//! numpy/scipy calls inside the Windows audio callback: the deck channel
//! strips (3 RBJ EQ biquads + bipolar DJ filter + smoothed gain), the program
//! bass-cut, and the FullRemix groove synthesizer (redrum kick/clap with
//! pattern DNA + key-following bassline + power-chord stabs, incl. the
//! kick-keyed sidechain).  Every formula is a faithful port of the Python
//! reference (see realtime_native_engine.py) so behavior is preserved while
//! per-block cost and allocation jitter collapse: zero heap allocation per
//! call, f64 internal precision, f32 interleaved I/O, C ABI (loaded from
//! Python with ctypes; no Python headers required).
//!
//! The out-of-band planner ("the agentic brain") intentionally stays in
//! Python: it never runs inside the callback and gains nothing from Rust.

#![allow(clippy::many_single_char_names)]

use std::f64::consts::PI;

#[inline(always)]
fn clampf(x: f64, lo: f64, hi: f64) -> f64 {
    if x < lo { lo } else if x > hi { hi } else { x }
}

// ---------------------------------------------------------------- smoothing
// Exact port of RealtimeDJMixer._smooth (tau one-pole + per-second slew cap).
#[inline(always)]
fn smooth(cur: f64, tgt: f64, block_s: f64, tau: f64, slew_per_s: f64) -> f64 {
    let a = 1.0 - (-block_s / tau.max(0.008)).exp();
    let nxt = cur + (tgt - cur) * a;
    cur + clampf(nxt - cur, -slew_per_s * block_s, slew_per_s * block_s)
}

// ---------------------------------------------------------------- biquad
// Exact port of the Python Biquad: RBJ coefficients, transposed DF-II state,
// linear coefficient morph in MORPH-frame sub-blocks toward the stored target.
#[derive(Clone, Copy)]
struct Biquad {
    b0: f64, b1: f64, b2: f64, a1: f64, a2: f64,
    tb0: f64, tb1: f64, tb2: f64, ta1: f64, ta2: f64,
    z1: [f64; 2], z2: [f64; 2],
    init: bool,
}

impl Biquad {
    fn new() -> Self {
        Biquad { b0: 1.0, b1: 0.0, b2: 0.0, a1: 0.0, a2: 0.0,
                 tb0: 1.0, tb1: 0.0, tb2: 0.0, ta1: 0.0, ta2: 0.0,
                 z1: [0.0; 2], z2: [0.0; 2], init: false }
    }

    fn set_coeffs(&mut self, kind: u32, fs: f64, freq: f64, q: f64, gain_db: f64) {
        let fs = fs.max(8000.0);
        let f = clampf(freq, 10.0, fs * 0.48);
        let w0 = 2.0 * PI * f / fs;
        let (cw, sw) = (w0.cos(), w0.sin());
        let a_amp = 10f64.powf(gain_db / 40.0);
        let alpha = sw / (2.0 * q.max(0.05));
        let (b0, b1, b2, a0, a1, a2) = match kind {
            0 => { // highpass
                ((1.0 + cw) / 2.0, -(1.0 + cw), (1.0 + cw) / 2.0,
                 1.0 + alpha, -2.0 * cw, 1.0 - alpha)
            }
            1 => { // lowpass
                ((1.0 - cw) / 2.0, 1.0 - cw, (1.0 - cw) / 2.0,
                 1.0 + alpha, -2.0 * cw, 1.0 - alpha)
            }
            2 => { // peaking
                (1.0 + alpha * a_amp, -2.0 * cw, 1.0 - alpha * a_amp,
                 1.0 + alpha / a_amp, -2.0 * cw, 1.0 - alpha / a_amp)
            }
            3 | 4 => { // lowshelf / highshelf
                let sqrt_a = a_amp.sqrt();
                let shelf_alpha = sw / 2.0 * 2f64.sqrt();
                let beta = 2.0 * sqrt_a * shelf_alpha;
                if kind == 3 {
                    (a_amp * ((a_amp + 1.0) - (a_amp - 1.0) * cw + beta),
                     2.0 * a_amp * ((a_amp - 1.0) - (a_amp + 1.0) * cw),
                     a_amp * ((a_amp + 1.0) - (a_amp - 1.0) * cw - beta),
                     (a_amp + 1.0) + (a_amp - 1.0) * cw + beta,
                     -2.0 * ((a_amp - 1.0) + (a_amp + 1.0) * cw),
                     (a_amp + 1.0) + (a_amp - 1.0) * cw - beta)
                } else {
                    (a_amp * ((a_amp + 1.0) + (a_amp - 1.0) * cw + beta),
                     -2.0 * a_amp * ((a_amp - 1.0) + (a_amp + 1.0) * cw),
                     a_amp * ((a_amp + 1.0) + (a_amp - 1.0) * cw - beta),
                     (a_amp + 1.0) - (a_amp - 1.0) * cw + beta,
                     2.0 * ((a_amp - 1.0) - (a_amp + 1.0) * cw),
                     (a_amp + 1.0) - (a_amp - 1.0) * cw - beta)
                }
            }
            _ => (1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
        };
        let inv = 1.0 / a0;
        self.tb0 = b0 * inv; self.tb1 = b1 * inv; self.tb2 = b2 * inv;
        self.ta1 = a1 * inv; self.ta2 = a2 * inv;
        if !self.init {
            self.b0 = self.tb0; self.b1 = self.tb1; self.b2 = self.tb2;
            self.a1 = self.ta1; self.a2 = self.ta2;
            self.init = true;
        }
    }

    #[inline(always)]
    fn run_fixed(&mut self, buf: &mut [f32], ch: usize, start: usize, end: usize) {
        for c in 0..ch {
            let mut z1 = self.z1[c];
            let mut z2 = self.z2[c];
            let (b0, b1, b2, a1, a2) = (self.b0, self.b1, self.b2, self.a1, self.a2);
            let mut i = start * ch + c;
            let stop = end * ch;
            while i < stop {
                let v = buf[i] as f64;
                let out = b0 * v + z1;
                z1 = b1 * v - a1 * out + z2;
                z2 = b2 * v - a2 * out;
                buf[i] = out as f32;
                i += ch;
            }
            self.z1[c] = z1;
            self.z2[c] = z2;
        }
    }

    fn process(&mut self, buf: &mut [f32], n: usize, ch: usize, morph_frames: usize) {
        if n == 0 { return; }
        let d = (self.tb0 - self.b0).abs()
            .max((self.tb1 - self.b1).abs())
            .max((self.tb2 - self.b2).abs())
            .max((self.ta1 - self.a1).abs())
            .max((self.ta2 - self.a2).abs());
        if d < 2e-9 {
            self.run_fixed(buf, ch, 0, n);
            return;
        }
        let morph = morph_frames.max(16);
        let start = (self.b0, self.b1, self.b2, self.a1, self.a2);
        let target = (self.tb0, self.tb1, self.tb2, self.ta1, self.ta2);
        let mut a = 0usize;
        while a < n {
            let b = (a + morph).min(n);
            let frac = b as f64 / n.max(1) as f64;
            self.b0 = start.0 + (target.0 - start.0) * frac;
            self.b1 = start.1 + (target.1 - start.1) * frac;
            self.b2 = start.2 + (target.2 - start.2) * frac;
            self.a1 = start.3 + (target.3 - start.3) * frac;
            self.a2 = start.4 + (target.4 - start.4) * frac;
            self.run_fixed(buf, ch, a, b);
            a = b;
        }
        self.b0 = target.0; self.b1 = target.1; self.b2 = target.2;
        self.a1 = target.3; self.a2 = target.4;
    }
}

// ---------------------------------------------------------------- rng
struct XorShift(u64);
impl XorShift {
    #[inline(always)]
    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13; x ^= x >> 7; x ^= x << 17;
        self.0 = x;
        x
    }
    /// approx standard normal (CLT over 3 uniforms, var 1)
    #[inline(always)]
    fn normal(&mut self) -> f64 {
        let u1 = (self.next_u64() >> 11) as f64 / 9007199254740992.0;
        let u2 = (self.next_u64() >> 11) as f64 / 9007199254740992.0;
        let u3 = (self.next_u64() >> 11) as f64 / 9007199254740992.0;
        (u1 + u2 + u3 - 1.5) * 2.0
    }
}

// ---------------------------------------------------------------- context
struct DeckState {
    low: Biquad, mid: Biquad, high: Biquad, hp: Biquad, lp: Biquad,
    s_low: f64, s_mid: f64, s_high: f64, s_filt: f64, s_gain: f64,
}
impl DeckState {
    fn new() -> Self {
        DeckState { low: Biquad::new(), mid: Biquad::new(), high: Biquad::new(),
                    hp: Biquad::new(), lp: Biquad::new(),
                    s_low: 0.0, s_mid: 0.0, s_high: 0.0, s_filt: 0.0, s_gain: 1.0 }
    }
}

pub struct Ctx {
    sr: f64,
    ch: usize,
    morph: usize,
    deck_a: DeckState,
    deck_b: DeckState,
    bass_cut_hp: Biquad,
    s_bass_cut: f64,
    stab_hp: Biquad,
    stab_lp: Biquad,
    click_hp: Biquad,
    clap_hp: Biquad,
    clap_lp: Biquad,
    hat_src_hp: Biquad,
    hat_src_lp: Biquad,
    top_hp: Biquad,
    top_lp: Biquad,
    rng: XorShift,
    v_kick: Voice,
    v_clap: Voice,
    v_bass: Voice,
    v_stab: Voice,
    v_hat: Voice,
    hat_density: f64,
}

#[no_mangle]
pub extern "C" fn jm_version() -> u32 { 3117 }

#[no_mangle]
pub extern "C" fn jm_ctx_new(sample_rate: f64, channels: u32, morph_frames: u32) -> *mut Ctx {
    let ctx = Box::new(Ctx {
        sr: sample_rate.max(8000.0),
        ch: (channels.max(1) as usize).min(2),
        morph: morph_frames.max(16) as usize,
        deck_a: DeckState::new(),
        deck_b: DeckState::new(),
        bass_cut_hp: Biquad::new(),
        s_bass_cut: 0.0,
        stab_hp: Biquad::new(),
        stab_lp: Biquad::new(),
        click_hp: Biquad::new(),
        clap_hp: Biquad::new(),
        clap_lp: Biquad::new(),
        hat_src_hp: Biquad::new(),
        hat_src_lp: Biquad::new(),
        top_hp: Biquad::new(),
        top_lp: Biquad::new(),
        rng: XorShift(0x4A4D_3133_9E37_79B9),
        v_kick: Voice::new(),
        v_clap: Voice::new(),
        v_bass: Voice::new(),
        v_stab: Voice::new(),
        v_hat: Voice::new(),
        hat_density: 1.0,
    });
    Box::into_raw(ctx)
}

#[no_mangle]
pub extern "C" fn jm_ctx_free(ctx: *mut Ctx) {
    if !ctx.is_null() {
        unsafe { drop(Box::from_raw(ctx)); }
    }
}

/// Full deck channel strip, in place on interleaved f32.
/// which: 0 = deck A, 1 = deck B.  out_state (len 5) receives the smoothed
/// effective low/mid/high/filter/gain so Python telemetry stays truthful.
#[no_mangle]
pub extern "C" fn jm_deck(ctx: *mut Ctx, which: u32, buf: *mut f32, n: u32,
                          low_db: f64, mid_db: f64, high_db: f64, filt: f64, gain: f64,
                          out_state: *mut f64) -> i32 {
    if ctx.is_null() || buf.is_null() { return -1; }
    let ctx = unsafe { &mut *ctx };
    let n = n as usize;
    let ch = ctx.ch;
    let buf = unsafe { std::slice::from_raw_parts_mut(buf, n * ch) };
    let block_s = n as f64 / ctx.sr;
    let d = if which == 0 { &mut ctx.deck_a } else { &mut ctx.deck_b };

    d.s_low = smooth(d.s_low, clampf(low_db, -8.0, 6.0), block_s, 0.075, 18.0);
    d.s_mid = smooth(d.s_mid, clampf(mid_db, -8.0, 6.0), block_s, 0.075, 18.0);
    d.s_high = smooth(d.s_high, clampf(high_db, -8.0, 6.0), block_s, 0.075, 18.0);
    d.s_filt = smooth(d.s_filt, clampf(filt, -1.0, 1.0), block_s, 0.065, 3.2);
    d.s_gain = smooth(d.s_gain, clampf(gain, 0.0, 1.20), block_s, 0.055, 3.0);

    d.low.set_coeffs(3, ctx.sr, 120.0, 0.707, d.s_low);
    d.mid.set_coeffs(2, ctx.sr, 1150.0, 0.78, d.s_mid);
    d.high.set_coeffs(4, ctx.sr, 7600.0, 0.707, d.s_high);
    let (hp_hz, lp_hz) = if d.s_filt >= 0.0 {
        (20.0 * (1200.0f64 / 20.0).powf(d.s_filt.powf(1.35)), ctx.sr * 0.46)
    } else {
        (20.0, (ctx.sr * 0.46) * (520.0 / (ctx.sr * 0.46)).powf((-d.s_filt).powf(1.25)))
    };
    d.hp.set_coeffs(0, ctx.sr, hp_hz, 0.707, 0.0);
    d.lp.set_coeffs(1, ctx.sr, lp_hz, 0.707, 0.0);

    let morph = ctx.morph;
    d.low.process(buf, n, ch, morph);
    d.mid.process(buf, n, ch, morph);
    d.high.process(buf, n, ch, morph);
    d.hp.process(buf, n, ch, morph);
    d.lp.process(buf, n, ch, morph);
    let g = d.s_gain as f32;
    for v in buf.iter_mut() { *v *= g; }

    if !out_state.is_null() {
        let os = unsafe { std::slice::from_raw_parts_mut(out_state, 5) };
        os[0] = d.s_low; os[1] = d.s_mid; os[2] = d.s_high; os[3] = d.s_filt; os[4] = d.s_gain;
    }
    0
}

/// Program bass-cut highpass (build floor-strip).  Returns the smoothed
/// effective cut.  Transparent (and filter bypassed) at ~zero, like Python.
#[no_mangle]
pub extern "C" fn jm_bass_cut(ctx: *mut Ctx, buf: *mut f32, n: u32, cut: f64) -> f64 {
    if ctx.is_null() || buf.is_null() { return 0.0; }
    let ctx = unsafe { &mut *ctx };
    let n = n as usize;
    let ch = ctx.ch;
    let block_s = n as f64 / ctx.sr;
    ctx.s_bass_cut = smooth(ctx.s_bass_cut, clampf(cut, 0.0, 1.0), block_s, 0.070, 3.0);
    if ctx.s_bass_cut > 1e-3 {
        let hp_hz = 22.0 + 188.0 * ctx.s_bass_cut.powf(1.25);
        ctx.bass_cut_hp.set_coeffs(0, ctx.sr, hp_hz, 0.78, 0.0);
        let buf = unsafe { std::slice::from_raw_parts_mut(buf, n * ch) };
        let morph = ctx.morph;
        ctx.bass_cut_hp.process(buf, n, ch, morph);
    }
    ctx.s_bass_cut
}

// ---------------------------------------------------------------- groove
const RD_KICKS: [[f64; 16]; 4] = [
    [1.0,0.0,0.0,0.0, 1.0,0.0,0.0,0.0, 1.0,0.0,0.0,0.0, 1.0,0.0,0.0,0.30],
    [1.0,0.0,0.0,0.0, 0.0,0.0,0.0,0.0, 0.0,0.0,0.55,0.0, 0.0,0.0,0.0,0.0],
    [1.0,0.0,0.0,0.0, 0.0,0.0,0.0,0.65, 0.0,0.0,1.0,0.0, 0.0,0.45,0.0,0.0],
    [0.0,0.0,1.0,0.0, 0.0,0.0,1.0,0.0, 0.0,0.0,1.0,0.0, 0.0,0.0,1.0,0.0],
];
const RD_CLAPS: [[f64; 16]; 4] = [
    [0.0,0.0,0.0,0.0, 1.0,0.0,0.0,0.0, 0.0,0.0,0.0,0.0, 1.0,0.0,0.0,0.0],
    [0.0,0.0,0.0,0.0, 0.0,0.0,0.0,0.0, 1.0,0.0,0.0,0.0, 0.0,0.0,0.0,0.0],
    [0.0,0.0,0.0,0.0, 1.0,0.0,0.0,0.0, 0.0,0.0,0.0,0.0, 1.0,0.0,0.0,0.35],
    [0.0,0.0,0.0,0.0, 1.0,0.0,0.0,0.0, 0.0,0.0,0.0,0.0, 1.0,0.0,0.0,0.0],
];
const BASS_V: [[f64; 16]; 4] = [
    [0.0,0.0,0.95,0.0, 0.0,0.0,0.9,0.0, 0.0,0.0,0.95,0.0, 0.0,0.0,0.9,0.0],
    [1.0,0.0,0.0,0.55, 0.0,0.0,0.8,0.0, 0.9,0.0,0.0,0.55, 0.0,0.0,0.75,0.0],
    [1.0,0.0,0.0,0.0, 0.0,0.0,0.0,0.0, 0.85,0.0,0.0,0.0, 0.0,0.0,0.0,0.0],
    [0.95,0.0,0.7,0.0, 0.95,0.0,0.7,0.0, 0.95,0.0,0.7,0.0, 0.95,0.0,0.7,0.0],
];
const BASS_N: [[f64; 16]; 4] = [
    [0.0;16],
    [0.0,0.0,0.0,0.0, 0.0,0.0,0.0,0.0, 0.0,0.0,0.0,7.0, 0.0,0.0,0.0,0.0],
    [0.0;16],
    [0.0,0.0,12.0,0.0, 0.0,0.0,12.0,0.0, 0.0,0.0,12.0,0.0, 0.0,0.0,12.0,0.0],
];
const STAB_V: [[f64; 16]; 2] = [
    [0.0,0.0,0.8,0.0, 0.0,0.0,0.8,0.0, 0.0,0.0,0.8,0.0, 0.0,0.0,0.8,0.0],
    [0.0,0.0,0.0,0.9, 0.0,0.0,0.0,0.0, 0.0,0.0,0.0,0.85, 0.0,0.0,0.4,0.0],
];
const BASS_GATE: [f64; 4] = [0.42, 0.24, 0.60, 0.20];

// Hat style families (v31.16 Hats 2.0): velocity + open flag per 16th step.
// 0 CLUB: open offbeat 8ths + 16th ghosts   1 HIPHOP: closed 8ths, sparse ghosts
// 2 DNB: dense 16ths, offbeat accents        3 SPARSE: offbeat 8ths only
const HAT_V: [[f64; 16]; 4] = [
    [0.50,0.18,0.92,0.16, 0.40,0.18,0.90,0.16, 0.50,0.18,0.92,0.16, 0.40,0.18,0.90,0.22],
    [0.85,0.00,0.70,0.15, 0.85,0.00,0.72,0.15, 0.85,0.00,0.70,0.15, 0.85,0.00,0.74,0.25],
    [0.62,0.55,0.95,0.55, 0.62,0.55,0.95,0.55, 0.62,0.55,0.95,0.55, 0.62,0.55,0.95,0.60],
    [0.00,0.00,0.72,0.00, 0.00,0.00,0.72,0.00, 0.00,0.00,0.72,0.00, 0.00,0.00,0.72,0.00],
];
const HAT_OPEN: [[u8; 16]; 4] = [
    [0,0,1,0, 0,0,1,0, 0,0,1,0, 0,0,1,0],
    [0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,1,0],
    [0,0,1,0, 0,0,1,0, 0,0,1,0, 0,0,1,0],
    [0,0,1,0, 0,0,1,0, 0,0,1,0, 0,0,1,0],
];

#[inline(always)]
fn hash01(bar: i64, step: i64, m1: i64, m2: i64) -> f64 {
    (((bar.wrapping_mul(m1)).wrapping_add(step.wrapping_mul(m2))) & 0xFFFFF) as f64 / 1048575.0
}

/// Avalanche hash (splitmix-style) for the hat humanizer: consecutive bars must
/// look independent, not like a slow ramp.
#[inline(always)]
fn hash01x(a: i64, b: i64, salt: u64) -> f64 {
    let mut x = (a as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15) ^ (b as u64).wrapping_mul(0xC2B2_AE3D_27D4_EB4F) ^ salt;
    x ^= x >> 30; x = x.wrapping_mul(0xBF58_476D_1CE4_E5B9);
    x ^= x >> 27; x = x.wrapping_mul(0x94D0_49BB_1331_11EB);
    x ^= x >> 31;
    (x & 0xFFFFF) as f64 / 1048575.0
}

/// Kick + clap velocities for an absolute 16th step (pattern DNA + fills).
#[inline(always)]
fn kick_clap_vel(step: i64, pat: usize, rd_var: f64, rd_fill: f64) -> (f64, f64) {
    let inbar = step.rem_euclid(16);
    let bar = step.div_euclid(16);
    let mut kv = RD_KICKS[pat][inbar as usize];
    let mut cv = RD_CLAPS[pat][inbar as usize];
    if rd_var > 1e-3 {
        let h = hash01(bar, inbar, 2654435761, 40503);
        kv *= 1.0 - 0.20 * rd_var * (h - 0.5) * 2.0;
        if h > (0.94 - 0.08 * rd_var) && kv < 0.05 { kv += 0.28 * rd_var; }
        kv = clampf(kv, 0.0, 1.15);
        if h > 0.86 && h < 0.90 { cv *= 1.0 - 0.6 * rd_var; }
    }
    if rd_fill > 1e-3 {
        let ftype = (bar.wrapping_mul(7).wrapping_add(3)).rem_euclid(3);
        let fill_zone = bar.rem_euclid(4) == 3 && inbar >= 12;
        let big_zone = bar.rem_euclid(8) == 7 && inbar >= 8;
        let fv = match ftype { 0 => 0.62, 1 => (inbar as f64 - 11.0) * 0.16 + 0.34, _ => 0.0 };
        if fill_zone { kv = kv * (1.0 - rd_fill) + fv * rd_fill; }
        if big_zone && ftype != 2 { kv = kv.max((0.30 + 0.05 * inbar as f64) * rd_fill * 0.8); }
        if fill_zone && ftype == 1 { cv = cv.max(0.55 * rd_fill); }
    }
    (kv, cv)
}

#[inline(always)]
fn bass_vel(step: i64, pat: usize) -> (f64, f64) {
    let inbar = step.rem_euclid(16);
    let bar = step.div_euclid(16);
    let mut vb = BASS_V[pat][inbar as usize];
    let mut nb = BASS_N[pat][inbar as usize];
    let hb = hash01(bar, inbar, 97561, 6151);
    if hb > 0.86 && vb > 0.1 && nb == 0.0 { nb = 7.0; }
    if hb < 0.06 && vb > 0.1 { nb = 12.0; }
    vb *= 1.0 - 0.12 * (hb - 0.5) * 2.0;
    (vb, nb)
}

#[inline(always)]
fn stab_vel(step: i64, pat: usize) -> f64 {
    let inbar = step.rem_euclid(16);
    let bar = step.div_euclid(16);
    let mut vs = STAB_V[pat][inbar as usize];
    let hs = hash01(bar, inbar, 52361, 911);
    if hs > 0.80 { vs = 0.0; }
    vs
}

/// Hat velocity / open flag / microtiming (ms) for an absolute step.
/// GrooVAE-style humanization: velocity spread, per-step microtiming that is
/// stable inside a 4-bar group (a drummer, not a dice), ghost-note drop/add,
/// open-hat accents at bar ends, 16th rolls on every 4th bar, flavor rotation,
/// and source-density thinning (never double a record that already has busy hats).
#[inline(always)]
fn hat_vel(step: i64, style: usize, var: f64, fill: f64, density: f64) -> (f64, bool, f64) {
    let inbar = step.rem_euclid(16);
    let bar = step.div_euclid(16);
    let grp = bar.div_euclid(4);
    let mut v = HAT_V[style][inbar as usize];
    let mut open = HAT_OPEN[style][inbar as usize] != 0;
    let hg = hash01x(grp, inbar, 0x51ED_270B);
    let hb = hash01x(bar, inbar, 0x1F83_D9AB);
    v *= 1.0 + 0.15 * (hg - 0.5) * 2.0;
    v *= 1.0 - 0.25 * var * (hb - 0.5) * 2.0;
    if v < 0.30 {
        v *= density;
        if density < 0.62 || (hb > 0.72 && var > 0.3) { v = 0.0; }
    } else if hb < 0.05 * var && inbar % 2 == 1 {
        v = 0.0;
    }
    if v < 0.02 && hb > 0.985 - 0.06 * var && inbar % 2 == 1 { v = 0.22 * var; }
    if bar.rem_euclid(2) == 1 && (inbar == 14 || inbar == 15) && hb > 0.55 { open = true; v = v.max(0.55); }
    if inbar == 0 && bar.rem_euclid(4) == 0 { v = v.max(0.62); }
    if fill > 1e-3 && bar.rem_euclid(4) == 3 && inbar >= 12 {
        let roll = 0.40 + 0.14 * (inbar - 12) as f64;
        v = v.max(roll * fill);
        open = open && inbar == 15;
    }
    let mt_ms = (hg - 0.5) * 5.0 + if inbar % 2 == 1 { 1.2 } else { 0.0 };
    (clampf(v, 0.0, 1.2), open, mt_ms * var)
}

/// Continuity state per voice: the last committed hit plus a pending hit whose
/// (swung / microtimed) onset lies later inside the current step.
#[derive(Clone, Copy)]
struct Voice {
    step: i64,
    hit_pos: f64,
    vel: f64,
    note: f64,
    open: bool,
    pend_pos: f64,
    pend_vel: f64,
    pend_note: f64,
    pend_open: bool,
    // previous hit, released over ~5 ms at every retrigger (no hard cuts)
    prev_pos: f64,
    prev_vel: f64,
    prev_note: f64,
    prev_open: bool,
    retrig_pos: f64,
}
impl Voice {
    const fn new() -> Self {
        Voice { step: i64::MIN, hit_pos: f64::NEG_INFINITY, vel: 0.0, note: 0.0, open: false,
                pend_pos: f64::INFINITY, pend_vel: 0.0, pend_note: 0.0, pend_open: false,
                prev_pos: f64::NEG_INFINITY, prev_vel: 0.0, prev_note: 0.0, prev_open: false, retrig_pos: f64::NEG_INFINITY }
    }
    #[inline(always)]
    fn commit(&mut self, p: f64, v: f64, nt: f64, op: bool) {
        if self.hit_pos != f64::NEG_INFINITY && self.vel > 1e-4 {
            self.prev_pos = self.hit_pos; self.prev_vel = self.vel; self.prev_note = self.note; self.prev_open = self.open;
            self.retrig_pos = p;
        }
        self.hit_pos = p; self.vel = v; self.note = nt; self.open = op;
        self.pend_pos = f64::INFINITY;
    }
    /// Release factor (0..1) of the previous hit at pos16; 0 when none.
    #[inline(always)]
    fn prev_release(&self, pos16: f64, rate: f64) -> f64 {
        if self.prev_pos == f64::NEG_INFINITY || self.retrig_pos == f64::NEG_INFINITY { return 0.0; }
        let since = ((pos16 - self.retrig_pos) / rate).max(0.0);
        if since > 0.030 { return 0.0; }
        (-since / 0.005).exp()
    }
    #[inline(always)]
    fn tt_prev(&self, pos16: f64, rate: f64) -> f64 {
        ((pos16 - self.prev_pos) / rate).max(0.0)
    }
    /// Per-sample update; `eval(step)` -> (vel, note, open, onset_offset_in_steps).
    #[inline(always)]
    fn advance<F: Fn(i64) -> (f64, f64, bool, f64)>(&mut self, pos16: f64, eval: F) {
        let step = pos16.floor() as i64;
        if step != self.step {
            let fresh = self.step == i64::MIN || step < self.step || step - self.step > 32;
            if fresh {
                self.hit_pos = f64::NEG_INFINITY; self.vel = 0.0; self.pend_pos = f64::INFINITY;
                for k in (step - 16..step).rev() {
                    let (v, nt, op, off) = eval(k);
                    if v > 1e-4 { self.hit_pos = k as f64 + off; self.vel = v; self.note = nt; self.open = op; break; }
                }
            }
            let from = if fresh { step } else { self.step + 1 };
            for k in from..=step {
                let (v, nt, op, off) = eval(k);
                if v > 1e-4 {
                    let p = k as f64 + off;
                    if p <= pos16 { self.commit(p, v, nt, op); }
                    else { self.pend_pos = p; self.pend_vel = v; self.pend_note = nt; self.pend_open = op; }
                }
            }
            self.step = step;
        }
        if pos16 >= self.pend_pos {
            let (p, v, nt, op) = (self.pend_pos, self.pend_vel, self.pend_note, self.pend_open);
            self.commit(p, v, nt, op);
        }
    }
    #[inline(always)]
    fn tt(&self, pos16: f64, rate: f64) -> f64 {
        if self.hit_pos == f64::NEG_INFINITY { return -1.0; }
        ((pos16 - self.hit_pos) / rate).max(0.0)
    }
}

#[repr(C)]
pub struct GrooveParams {
    pub grid_base: f64,
    pub bps: f64,
    pub swing: f64,
    pub redrum: f64,
    pub rd_pattern: i32,
    pub rd_var: f64,
    pub rd_fill: f64,
    pub bass: f64,
    pub bass_root: i32,
    pub bass_pattern: i32,
    pub stab: f64,
    pub stab_pattern: i32,
}

/// One-pass groove synth with click-free voice continuity: every hit's envelope
/// runs until the NEXT hit of that voice instead of being truncated at the
/// 16th-step edge (that truncation was audible as a per-beat click).
#[no_mangle]
pub extern "C" fn jm_groove(ctx: *mut Ctx, n: u32,
                            p: *const GrooveParams,
                            mul: *mut f32, add_mono: *mut f32, add_st: *mut f32) -> i32 {
    if ctx.is_null() || p.is_null() || mul.is_null() || add_mono.is_null() || add_st.is_null() {
        return -1;
    }
    let ctx = unsafe { &mut *ctx };
    let p = unsafe { &*p };
    let n = n as usize;
    let ch = ctx.ch;
    let sr = ctx.sr;
    let mul = unsafe { std::slice::from_raw_parts_mut(mul, n) };
    let add_m = unsafe { std::slice::from_raw_parts_mut(add_mono, n) };
    let add_s = unsafe { std::slice::from_raw_parts_mut(add_st, n * ch) };
    for v in mul.iter_mut() { *v = 1.0; }
    for v in add_m.iter_mut() { *v = 0.0; }
    for v in add_s.iter_mut() { *v = 0.0; }

    let rate = p.bps * 4.0;
    let redrum_on = p.redrum > 1e-4;
    let bass_on = p.bass > 1e-4;
    let stab_on = p.stab > 1e-4;
    if !redrum_on && !bass_on && !stab_on { return 0; }

    let rd_pat = p.rd_pattern.clamp(0, 3) as usize;
    let b_pat = p.bass_pattern.clamp(0, 3) as usize;
    let s_pat = p.stab_pattern.clamp(0, 1) as usize;
    let root = p.bass_root.clamp(0, 11) as f64;
    let mut f0 = 32.703 * 2f64.powf(root / 12.0);
    if f0 < 38.0 { f0 *= 2.0; }
    let (f1, f2, f3) = (f0 * 4.0, f0 * 4.0 * 2f64.powf(7.0 / 12.0), f0 * 8.0);
    let kick_g = 0.055 + 0.085 * p.redrum;
    let click_g = 0.007 + 0.011 * p.redrum;
    let clap_g = 0.011 + 0.019 * p.redrum;
    let bass_g = 0.085 + 0.13 * p.bass;
    let stab_g = 0.05 + 0.10 * p.stab;
    let gate = BASS_GATE[b_pat];
    let swing = p.swing;
    let (rd_var, rd_fill) = (p.rd_var, p.rd_fill);
    let sw_off = move |k: i64| if swing > 1e-3 && k.rem_euclid(2) == 1 { swing } else { 0.0 };

    ctx.click_hp.set_coeffs(0, sr, 2400.0, 0.707, 0.0);
    ctx.clap_hp.set_coeffs(0, sr, 950.0, 0.707, 0.0);
    ctx.clap_lp.set_coeffs(1, sr, 3600.0f64.min(sr * 0.42), 0.707, 0.0);
    ctx.stab_hp.set_coeffs(0, sr, 180.0, 0.707, 0.0);
    ctx.stab_lp.set_coeffs(1, sr, 2400.0f64.min(sr * 0.40), 0.707, 0.0);

    let mut stab_buf = [0f32; 8192];
    let stab_slice = &mut stab_buf[..n.min(8192)];
    let mut kick_v = ctx.v_kick; let mut clap_v = ctx.v_clap; let mut bass_v = ctx.v_bass; let mut stab_v = ctx.v_stab;

    for i in 0..n {
        let pos16 = (p.grid_base + (i as f64 / sr) * p.bps) * 4.0;
        let mut kenv = 0.0f64;
        if redrum_on {
            kick_v.advance(pos16, |k| { let (kv, _) = kick_clap_vel(k, rd_pat, rd_var, rd_fill); (kv, 0.0, false, sw_off(k)) });
            clap_v.advance(pos16, |k| { let (_, cv) = kick_clap_vel(k, rd_pat, rd_var, rd_fill); (cv, 0.0, false, sw_off(k)) });
            let tt = kick_v.tt(pos16, rate);
            if tt >= 0.0 {
                let (f_end, f_span, tau) = (47.0, 92.0, 0.020);
                let kphase = 2.0 * PI * (f_end * tt + f_span * tau * (1.0 - (-tt / tau).exp()));
                kenv = (-tt / 0.115).exp() * kick_v.vel * (tt / 0.0015).min(1.0);
                let mut kick = kphase.sin() * kenv;
                let rel = kick_v.prev_release(pos16, rate);
                if rel > 1e-4 {
                    let tp = kick_v.tt_prev(pos16, rate);
                    let php = 2.0 * PI * (f_end * tp + f_span * tau * (1.0 - (-tp / tau).exp()));
                    let ep = (-tp / 0.115).exp() * kick_v.prev_vel * rel;
                    kick += php.sin() * ep;
                    kenv = kenv.max(ep);
                }
                add_m[i] += (kick * kick_g) as f32;
                mul[i] = (1.0 - 0.26 * p.redrum * kenv) as f32;
            }
            let ttc = clap_v.tt(pos16, rate);
            let mut cenv = if ttc >= 0.0 { (-ttc / 0.055).exp() * clap_v.vel * (ttc / 0.001).min(1.0) } else { 0.0 };
            let relc = clap_v.prev_release(pos16, rate);
            if relc > 1e-4 { cenv += (-clap_v.tt_prev(pos16, rate) / 0.055).exp() * clap_v.prev_vel * relc; }
            for c in 0..ch {
                let cn = ctx.rng.normal();
                let kn = ctx.rng.normal();
                add_s[i * ch + c] += (kn * (kenv * kenv * kenv) * click_g + cn * cenv * clap_g) as f32;
            }
        }
        if bass_on {
            bass_v.advance(pos16, |k| { let (vb, nb) = bass_vel(k, b_pat); (vb, nb, false, sw_off(k)) });
            let ttb = bass_v.tt(pos16, rate);
            if ttb >= 0.0 {
                let fb = f0 * 2f64.powf(bass_v.note / 12.0);
                let envb = (-ttb / gate).exp() * bass_v.vel * (ttb / 0.004).min(1.0);
                let mut bass = ((2.0 * PI * fb * ttb).sin() + 0.34 * (4.0 * PI * fb * ttb).sin()) * envb;
                let relb = bass_v.prev_release(pos16, rate);
                if relb > 1e-4 {
                    let tp = bass_v.tt_prev(pos16, rate);
                    let fp = f0 * 2f64.powf(bass_v.prev_note / 12.0);
                    let ep = (-tp / gate).exp() * bass_v.prev_vel * relb;
                    bass += ((2.0 * PI * fp * tp).sin() + 0.34 * (4.0 * PI * fp * tp).sin()) * ep;
                }
                add_m[i] += (bass * (1.0 - 0.55 * kenv) * bass_g) as f32;
            }
        }
        if stab_on {
            stab_v.advance(pos16, |k| (stab_vel(k, s_pat), 0.0, false, 0.0));
            let tts = stab_v.tt(pos16, rate);
            if tts >= 0.0 && i < stab_slice.len() {
                let envs = (-tts / 0.085).exp() * stab_v.vel * (tts / 0.003).min(1.0);
                let chord = (2.0 * PI * f1 * tts).sin() + (2.0 * PI * f2 * tts).sin() + 0.6 * (2.0 * PI * f3 * tts).sin();
                let mut sig = (chord * 1.2).tanh() * envs;
                let rels = stab_v.prev_release(pos16, rate);
                if rels > 1e-4 {
                    let tp = stab_v.tt_prev(pos16, rate);
                    let ep = (-tp / 0.085).exp() * stab_v.prev_vel * rels;
                    let chp = (2.0 * PI * f1 * tp).sin() + (2.0 * PI * f2 * tp).sin() + 0.6 * (2.0 * PI * f3 * tp).sin();
                    sig += (chp * 1.2).tanh() * ep;
                }
                stab_slice[i] = (sig * (1.0 - 0.45 * kenv)) as f32;
            }
        }
    }
    ctx.v_kick = kick_v; ctx.v_clap = clap_v; ctx.v_bass = bass_v; ctx.v_stab = stab_v;

    if redrum_on {
        let morph = ctx.morph;
        ctx.click_hp.process(add_s, n, ch, morph);
        ctx.clap_hp.process(add_s, n, ch, morph);
        ctx.clap_lp.process(add_s, n, ch, morph);
    }
    if stab_on {
        let morph = ctx.morph;
        ctx.stab_hp.process(stab_slice, n, 1, morph);
        ctx.stab_lp.process(stab_slice, n, 1, morph);
        for i in 0..n { add_m[i] += stab_slice[i] * stab_g as f32; }
    }
    0
}

#[repr(C)]
pub struct HatParams {
    pub grid_base: f64,
    pub bps: f64,
    pub swing: f64,
    pub level: f64,
    pub style: i32,
    pub var: f64,
    pub fill: f64,
}

/// Hats 2.0: source-aware top percussion with GrooVAE-style humanization and
/// click-free open-hat continuity.  `src` = interleaved program block (texture
/// donor); `add_out` receives the interleaved hat layer.  Returns the measured
/// source top-band density (0..1) so telemetry can show the thinning decision.
#[no_mangle]
pub extern "C" fn jm_hats(ctx: *mut Ctx, src: *const f32, n: u32, p: *const HatParams, add_out: *mut f32) -> f64 {
    if ctx.is_null() || src.is_null() || p.is_null() || add_out.is_null() { return 0.0; }
    let ctx = unsafe { &mut *ctx };
    let p = unsafe { &*p };
    let n = n as usize;
    let ch = ctx.ch;
    let sr = ctx.sr;
    let src = unsafe { std::slice::from_raw_parts(src, n * ch) };
    let out = unsafe { std::slice::from_raw_parts_mut(add_out, n * ch) };
    for v in out.iter_mut() { *v = 0.0; }
    if p.level <= 1e-4 || n == 0 { return ctx.hat_density; }
    let style = p.style.clamp(0, 3) as usize;
    let rate = p.bps * 4.0;
    let tl = (n * ch).min(16384);

    let mut top = [0f32; 16384];
    top[..tl].copy_from_slice(&src[..tl]);
    ctx.hat_src_hp.set_coeffs(0, sr, 4200.0, 0.72, 0.0);
    ctx.hat_src_lp.set_coeffs(1, sr, 12200.0f64.min(sr * 0.43), 0.68, 0.0);
    let morph = ctx.morph;
    ctx.hat_src_hp.process(&mut top[..tl], n, ch, morph);
    ctx.hat_src_lp.process(&mut top[..tl], n, ch, morph);
    let mut acc = 0.0f64;
    for v in &top[..tl] { acc += (*v as f64) * (*v as f64); }
    let src_rms = (acc / tl as f64 + 1e-12).sqrt();
    let source_gain = clampf(0.72 / (src_rms * 32.0 + 0.72), 0.34, 0.78);
    let density = clampf(1.0 - (src_rms * 32.0 - 0.40) * 1.2, 0.35, 1.0);
    ctx.hat_density = 0.7 * ctx.hat_density + 0.3 * density;
    let density = ctx.hat_density;

    let mut noise = [0f32; 16384];
    for i in 0..n {
        let m = ctx.rng.normal() as f32;
        let s = ctx.rng.normal() as f32;
        for c in 0..ch {
            let sgn = if c == 0 { 1.0 } else { -1.0 };
            noise[i * ch + c] = m + 0.10 * sgn * s;
        }
    }
    ctx.top_hp.set_coeffs(0, sr, 5400.0, 0.707, 0.0);
    ctx.top_lp.set_coeffs(1, sr, 13200.0f64.min(sr * 0.44), 0.68, 0.0);
    ctx.top_hp.process(&mut noise[..tl], n, ch, morph);
    ctx.top_lp.process(&mut noise[..tl], n, ch, morph);

    let (var, fill, swing) = (p.var, p.fill, p.swing);
    let mut hv = ctx.v_hat;
    let amp_g = (0.010 + 0.013 * p.level) * p.level;
    for i in 0..n {
        let pos16 = (p.grid_base + (i as f64 / sr) * p.bps) * 4.0;
        hv.advance(pos16, |k| {
            let (v, open, mt_ms) = hat_vel(k, style, var, fill, density);
            let mut off = mt_ms / 1000.0 * rate;
            if swing > 1e-3 && k.rem_euclid(2) == 1 { off += swing; }
            (v, if open { 1.0 } else { 0.0 }, open, off.max(0.0))
        });
        let tt = hv.tt(pos16, rate);
        if tt < 0.0 { continue; }
        let decay = if hv.open { 0.16 } else { 0.028 + 0.012 * hv.vel };
        let mut pulse = (-tt / decay).exp() * hv.vel * (tt / 0.0015).min(1.0);
        let relh = hv.prev_release(pos16, rate);
        if relh > 1e-4 {
            let dp = if hv.prev_open { 0.16 } else { 0.028 + 0.012 * hv.prev_vel };
            pulse += (-hv.tt_prev(pos16, rate) / dp).exp() * hv.prev_vel * relh;
        }
        if pulse < 1e-5 { continue; }
        let a = (pulse * amp_g) as f32;
        for c in 0..ch {
            let idx = i * ch + c;
            let tex = top[idx] * source_gain as f32 + noise[idx] * (0.17 + 0.08 * (1.0 - source_gain)) as f32;
            out[idx] = (tex * 1.35).tanh() * a;
        }
    }
    ctx.v_hat = hv;
    density
}
