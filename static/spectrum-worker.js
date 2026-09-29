'use strict';

const clamp = (v, min = 0, max = 1) => Math.max(min, Math.min(max, v));

function computeSurface(mono, sr, duration, targetCols, targetBins) {
  const frameSize = 256;
  const cols = Math.max(70, Math.min(targetCols || 170, Math.floor((duration || 0) * 5.2) || 70));
  const hop = Math.max(64, Math.floor(Math.max(1, mono.length - frameSize) / Math.max(1, cols - 1)));
  const actualCols = Math.max(1, Math.min(cols, 1 + Math.floor(Math.max(0, mono.length - frameSize) / hop)));
  const bins = targetBins || 24;

  const hann = new Float32Array(frameSize);
  for (let i = 0; i < frameSize; i++) hann[i] = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (frameSize - 1));

  const minF = 70;
  const maxF = Math.min(sr * 0.47, 7000);
  const coeffs = new Float32Array(bins);
  for (let b = 0; b < bins; b++) {
    const t = b / Math.max(1, bins - 1);
    const f = minF * Math.pow(maxF / minF, t);
    coeffs[b] = 2 * Math.cos(2 * Math.PI * f / sr);
  }

  const rawValues = new Float32Array(actualCols * bins);
  const rawBandProfile = new Float32Array(bins);
  const rawTimeProfile = new Float32Array(actualCols);
  let rawTotal = 0;
  let rawWeighted = 0;
  let rawPeak = 0;

  for (let c = 0; c < actualCols; c++) {
    const start = c * hop;
    let colSum = 0;
    for (let b = 0; b < bins; b++) {
      const coeff = coeffs[b];
      let s0 = 0, s1 = 0, s2 = 0;
      for (let n = 0; n < frameSize; n++) {
        const idx = start + n;
        const sample = (idx < mono.length ? mono[idx] : 0) * hann[n];
        s0 = sample + coeff * s1 - s2;
        s2 = s1;
        s1 = s0;
      }
      const power = Math.max(0, s1 * s1 + s2 * s2 - coeff * s1 * s2);
      const v = Math.log1p(power * 20);
      rawValues[c * bins + b] = v;
      rawBandProfile[b] += v;
      colSum += v;
      rawTotal += v;
      rawWeighted += v * b;
      rawPeak = Math.max(rawPeak, v);
    }
    rawTimeProfile[c] = colSum / Math.max(1, bins);
  }

  for (let b = 0; b < bins; b++) rawBandProfile[b] /= Math.max(1, actualCols);

  const rawMean = rawTotal / Math.max(1, rawValues.length);
  const rawCentroid = rawTotal > 1e-9 ? rawWeighted / rawTotal / Math.max(1, bins - 1) : 0.5;
  let variance = 0, flux = 0, timeMean = 0, timePeak = 0;
  for (const v of rawValues) variance += (v - rawMean) * (v - rawMean);
  for (let c = 0; c < actualCols; c++) {
    timeMean += rawTimeProfile[c];
    timePeak = Math.max(timePeak, rawTimeProfile[c]);
    if (c) flux += Math.abs(rawTimeProfile[c] - rawTimeProfile[c - 1]);
  }
  variance /= Math.max(1, rawValues.length);
  timeMean /= Math.max(1, actualCols);
  flux /= Math.max(1, actualCols - 1);
  const rawStd = Math.sqrt(variance);
  const crest = timeMean > 1e-9 ? timePeak / timeMean : 1;

  let fingerprint = 2166136261 >>> 0;
  const stride = Math.max(1, Math.floor(rawValues.length / 280));
  for (let i = 0; i < rawValues.length; i += stride) {
    const q = Math.max(0, Math.min(65535, Math.round(rawValues[i] * 4096)));
    fingerprint ^= (q & 255);
    fingerprint = Math.imul(fingerprint, 16777619) >>> 0;
    fingerprint ^= (q >>> 8);
    fingerprint = Math.imul(fingerprint, 16777619) >>> 0;
  }
  for (let b = 0; b < bins; b++) {
    const q = Math.max(0, Math.min(65535, Math.round(rawBandProfile[b] * 4096)));
    fingerprint ^= q;
    fingerprint = Math.imul(fingerprint, 16777619) >>> 0;
  }

  const sorted = Array.from(rawValues).sort((a, b) => a - b);
  const pick = p => sorted[Math.floor((sorted.length - 1) * p)] || 0;
  const floor = pick(0.18);
  const ceil = Math.max(pick(0.995), floor + 1e-6);
  const values = new Float32Array(rawValues.length);
  for (let i = 0; i < rawValues.length; i++) values[i] = clamp((rawValues[i] - floor) / (ceil - floor));

  let maxBand = 1e-9, maxTime = 1e-9;
  for (const v of rawBandProfile) maxBand = Math.max(maxBand, v);
  for (const v of rawTimeProfile) maxTime = Math.max(maxTime, v);
  const bandProfile = Float32Array.from(rawBandProfile, v => clamp(v / maxBand));
  const timeProfile = Float32Array.from(rawTimeProfile, v => clamp(v / maxTime));

  return {
    duration,
    columns: actualCols,
    bins,
    fingerprint,
    descriptors: {
      rawMean,
      rawStd,
      rawCentroid: clamp(rawCentroid),
      flux,
      crest,
      rawPeak,
    },
    values,
    bandProfile,
    timeProfile,
  };
}

self.onmessage = event => {
  const { id, monoBuffer, sr, duration, targetCols, targetBins } = event.data || {};
  try {
    const mono = new Float32Array(monoBuffer);
    const surface = computeSurface(mono, Number(sr) || 12000, Number(duration) || 0, Number(targetCols) || 170, Number(targetBins) || 24);
    self.postMessage({
      id,
      ok: true,
      duration: surface.duration,
      columns: surface.columns,
      bins: surface.bins,
      fingerprint: surface.fingerprint,
      descriptors: surface.descriptors,
      valuesBuffer: surface.values.buffer,
      bandProfileBuffer: surface.bandProfile.buffer,
      timeProfileBuffer: surface.timeProfile.buffer,
    }, [surface.values.buffer, surface.bandProfile.buffer, surface.timeProfile.buffer]);
  } catch (error) {
    self.postMessage({ id, ok: false, error: String(error && error.message ? error.message : error) });
  }
};
