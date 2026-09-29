const $ = (id) => document.getElementById(id);

const state = {
  jobId: null,
  poll: null,
  waveItems: [],
  spectrumItems: [],
  activeSource: 'original',
  outputs: null,
  selectedFileUrl: null,
  projectDuration: 0,
  activeLibrary: null,
  settingsOpen: false,
  sourceMode: 'file',
  referenceSource: null,
  referenceItems: [],
  recordedFile: null,
  micRecorder: null,
  micStream: null,
  micPreviewUrl: null,
  micBackingFile: null,
  micBackingUrl: null,
  visualDisposers: [],
  visualEpoch: 0,
  titleSyncTimer: null,
  libraryCoverDisposers: [],
  libraryTrackUrl: null,
  libraryTrackId: null,
  libraryTrackCollection: null,
  projectTransportMeta: 'No source loaded',
  projectCoverUrl: null,
  transportCoverSurface: null,
  transportCoverImage: null,
  transportCoverToken: 0,
  playbackSources: {},
  playbackLoadTokens: {},
  playbackLoadPromises: {},
  masterVolume: 0.85,
  sourceSwitchToken: 0,
};

const audioMap = () => ({
  original: $('originalPlayer'),
  modified: $('modifiedPlayer'),
  vocal: $('vocalStemPlayer'),
  instrument: $('instrumentStemPlayer'),
  library: $('libraryPlayer'),
});

const spectrumCache = new Map();
let spectrumWorkQueue = Promise.resolve();
const playbackBlobCache = new Map();
const playbackBlobInflight = new Map();
const coverImageCache = new Map();
const coverImageInflight = new Map();
const coverPersistInflight = new Map();
const coverPersistedSources = new Set();
let spectralWorker = null;
let spectralWorkerSeq = 0;
const spectralWorkerPending = new Map();

const liveThemeState = {
  raf: 0,
  url: '',
  surface: null,
  loadToken: 0,
  loading: false,
  lastTick: 0,
  lastCanvasRefresh: 0,
  lastWaterDraw: 0,
  waterTime: 0,
  initialized: false,
  current: {
    accent: [76,156,248],
    accent2: [181,84,255],
    accent3: [57,207,220],
    local1: [62,188,176],
    local2: [215,89,173],
    local3: [221,158,55],
    bg: [8,12,18],
    bg2: [12,10,20],
    bg3: [8,18,20],
    panel: [14,19,27],
    panel2: [18,24,34],
    border: [47,59,75],
    glow: [76,156,248],
    hue: 214,
    energy: .42,
  },
};

function audioSourceUrl(name) {
  return String(state.playbackSources[name] || '');
}

function clearPlaybackBlobCache() {
  for (const entry of playbackBlobCache.values()) {
    try { URL.revokeObjectURL(entry.objectUrl); } catch (_) {}
  }
  playbackBlobCache.clear();
  playbackBlobInflight.clear();
}

function prunePlaybackBlobCache() {
  const maxEntries = 7;
  const maxBytes = 420 * 1024 * 1024;
  let total = 0;
  const entries = Array.from(playbackBlobCache.entries());
  for (const [, entry] of entries) total += Number(entry.size || 0);
  if (entries.length <= maxEntries && total <= maxBytes) return;
  entries.sort((a, b) => Number(a[1].lastUsed || 0) - Number(b[1].lastUsed || 0));
  for (const [url, entry] of entries) {
    const inUse = Object.entries(state.playbackSources).some(([name, logical]) => {
      const a = audioMap()[name];
      return logical === url && a && !a.paused;
    });
    if (inUse) continue;
    playbackBlobCache.delete(url);
    total -= Number(entry.size || 0);
    try { URL.revokeObjectURL(entry.objectUrl); } catch (_) {}
    if (playbackBlobCache.size <= maxEntries && total <= maxBytes) break;
  }
}

async function getBufferedPlaybackUrl(url) {
  if (!url) throw new Error('No audio source URL.');
  const cached = playbackBlobCache.get(url);
  if (cached) {
    cached.lastUsed = performance.now();
    return cached.objectUrl;
  }
  if (playbackBlobInflight.has(url)) return playbackBlobInflight.get(url);
  const pending = (async () => {
    const response = await fetch(url, { cache: 'no-store' });
    if (!response.ok) throw new Error(`Audio preload failed (${response.status}).`);
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    playbackBlobCache.set(url, { objectUrl, blob, size: blob.size, lastUsed: performance.now() });
    prunePlaybackBlobCache();
    return objectUrl;
  })().finally(() => playbackBlobInflight.delete(url));
  playbackBlobInflight.set(url, pending);
  return pending;
}

function waitForAudioReady(audio, timeoutMs = 8000) {
  if (!audio) return Promise.reject(new Error('Audio player missing.'));
  if (audio.readyState >= 2 && Number.isFinite(audio.duration)) return Promise.resolve();
  return new Promise((resolve, reject) => {
    let done = false;
    const cleanup = () => {
      audio.removeEventListener('loadedmetadata', onReady);
      audio.removeEventListener('canplay', onReady);
      audio.removeEventListener('error', onError);
      clearTimeout(timer);
    };
    const finish = fn => {
      if (done) return;
      done = true;
      cleanup();
      fn();
    };
    const onReady = () => finish(resolve);
    const onError = () => finish(() => reject(new Error('Audio decoder could not open the buffered source.')));
    const timer = setTimeout(() => finish(resolve), timeoutMs);
    audio.addEventListener('loadedmetadata', onReady, { once: true });
    audio.addEventListener('canplay', onReady, { once: true });
    audio.addEventListener('error', onError, { once: true });
  });
}

function setPlaybackSource(name, url, eager = false) {
  const audio = audioMap()[name];
  state.playbackSources[name] = String(url || '');
  state.playbackLoadTokens[name] = Number(state.playbackLoadTokens[name] || 0) + 1;
  state.playbackLoadPromises[name] = null;
  if (audio) {
    try { audio.pause(); } catch (_) {}
    audio.removeAttribute('src');
    audio.dataset.bufferedFor = '';
    audio.dataset.ready = '0';
    audio.preload = 'auto';
    audio.load();
  }
  if (eager && url) {
    const run = () => ensureBufferedPlayback(name).catch(() => {});
    if ('requestIdleCallback' in window) requestIdleCallback(run, { timeout: 900 });
    else setTimeout(run, 0);
  }
}

async function ensureBufferedPlayback(name) {
  const audio = audioMap()[name];
  const logicalUrl = audioSourceUrl(name);
  if (!audio || !logicalUrl) throw new Error('Audio source is not available.');
  if (audio.dataset.bufferedFor === logicalUrl && audio.src) {
    await waitForAudioReady(audio);
    return audio;
  }
  if (state.playbackLoadPromises[name]) return state.playbackLoadPromises[name];
  const token = Number(state.playbackLoadTokens[name] || 0);
  const pending = (async () => {
    let playbackUrl = logicalUrl;
    try {
      playbackUrl = await getBufferedPlaybackUrl(logicalUrl);
    } catch (_) {
      // Safe fallback: direct localhost playback if a browser blocks blob buffering.
      playbackUrl = logicalUrl;
    }
    if (token !== Number(state.playbackLoadTokens[name] || 0) || logicalUrl !== audioSourceUrl(name)) {
      throw new Error('Audio source changed while buffering.');
    }
    audio.preload = 'auto';
    audio.src = playbackUrl;
    audio.dataset.bufferedFor = logicalUrl;
    audio.dataset.ready = '0';
    audio.volume = clamp(state.masterVolume);
    audio.load();
    await waitForAudioReady(audio);
    audio.dataset.ready = '1';
    return audio;
  })().finally(() => {
    if (state.playbackLoadPromises[name] === pending) state.playbackLoadPromises[name] = null;
  });
  state.playbackLoadPromises[name] = pending;
  return pending;
}

function cancelAudioFade(audio) {
  if (!audio) return;
  if (audio.__jmFadeRaf) cancelAnimationFrame(audio.__jmFadeRaf);
  audio.__jmFadeRaf = 0;
  const finish = audio.__jmFadeResolve;
  audio.__jmFadeResolve = null;
  if (finish) finish();
}

function rampAudioVolume(audio, target, ms = 24) {
  if (!audio) return Promise.resolve();
  cancelAudioFade(audio);
  const from = Number(audio.volume || 0);
  const to = clamp(target);
  if (Math.abs(from - to) < 0.002 || ms <= 0) {
    audio.volume = to;
    return Promise.resolve();
  }
  return new Promise(resolve => {
    let finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      audio.__jmFadeRaf = 0;
      audio.__jmFadeResolve = null;
      resolve();
    };
    audio.__jmFadeResolve = finish;
    const start = performance.now();
    const tick = now => {
      if (finished) return;
      const t = clamp((now - start) / ms);
      // Smoothstep avoids an abrupt derivative at either end of the fade.
      const k = t * t * (3 - 2 * t);
      audio.volume = clamp(from + (to - from) * k);
      if (t >= 1) finish();
      else audio.__jmFadeRaf = requestAnimationFrame(tick);
    };
    audio.__jmFadeRaf = requestAnimationFrame(tick);
  });
}

async function smoothPlayAudio(audio) {
  if (!audio) return;
  audio.__jmPauseToken = Number(audio.__jmPauseToken || 0) + 1;
  cancelAudioFade(audio);
  const target = clamp(state.masterVolume);
  audio.volume = 0;
  await audio.play();
  await rampAudioVolume(audio, target, 30);
}

async function smoothPauseAudio(audio, immediate = false) {
  if (!audio || audio.paused) return;
  const pauseToken = Number(audio.__jmPauseToken || 0) + 1;
  audio.__jmPauseToken = pauseToken;
  if (immediate) {
    cancelAudioFade(audio);
    if (audio.__jmPauseToken === pauseToken) audio.pause();
    audio.volume = clamp(state.masterVolume);
    return;
  }
  await rampAudioVolume(audio, 0, 22);
  if (audio.__jmPauseToken !== pauseToken) return;
  audio.pause();
  audio.volume = clamp(state.masterVolume);
}

async function smoothSeekAudio(audio, nextTime) {
  if (!audio || !Number.isFinite(nextTime)) return;
  const d = Number.isFinite(audio.duration) ? audio.duration : 0;
  const targetTime = d > 0 ? Math.max(0, Math.min(d, nextTime)) : Math.max(0, nextTime);
  if (audio.paused) {
    audio.currentTime = targetTime;
    syncTransportUI();
    return;
  }
  await rampAudioVolume(audio, 0, 16);
  audio.currentTime = targetTime;
  await new Promise(resolve => {
    let settled = false;
    const done = () => { if (settled) return; settled = true; audio.removeEventListener('seeked', done); resolve(); };
    audio.addEventListener('seeked', done, { once: true });
    setTimeout(done, 120);
  });
  await rampAudioVolume(audio, state.masterVolume, 22);
}

function clearPlaybackSession() {
  state.sourceSwitchToken += 1;
  for (const [name, audio] of Object.entries(audioMap())) {
    state.playbackSources[name] = '';
    state.playbackLoadTokens[name] = Number(state.playbackLoadTokens[name] || 0) + 1;
    state.playbackLoadPromises[name] = null;
    if (!audio) continue;
    cancelAudioFade(audio);
    try { audio.pause(); } catch (_) {}
    audio.removeAttribute('src');
    audio.dataset.bufferedFor = '';
    audio.dataset.ready = '0';
    audio.load();
  }
  clearPlaybackBlobCache();
}

function getSpectralWorker() {
  if (spectralWorker) return spectralWorker;
  if (!('Worker' in window)) return null;
  try {
    spectralWorker = new Worker('/static/spectrum-worker.js?v=16-hifi-playback');
    spectralWorker.onmessage = event => {
      const msg = event.data || {};
      const pending = spectralWorkerPending.get(msg.id);
      if (!pending) return;
      spectralWorkerPending.delete(msg.id);
      if (!msg.ok) {
        pending.reject(new Error(msg.error || 'Spectrum worker failed.'));
        return;
      }
      pending.resolve({
        duration: msg.duration,
        columns: msg.columns,
        bins: msg.bins,
        fingerprint: msg.fingerprint >>> 0,
        descriptors: msg.descriptors || {},
        values: new Float32Array(msg.valuesBuffer),
        bandProfile: new Float32Array(msg.bandProfileBuffer),
        timeProfile: new Float32Array(msg.timeProfileBuffer),
      });
    };
    spectralWorker.onerror = error => {
      for (const pending of spectralWorkerPending.values()) pending.reject(error);
      spectralWorkerPending.clear();
      try { spectralWorker.terminate(); } catch (_) {}
      spectralWorker = null;
    };
    return spectralWorker;
  } catch (_) {
    spectralWorker = null;
    return null;
  }
}

function analyzeSpectrumInWorker(mono, sr, duration, targetCols, targetBins) {
  const worker = getSpectralWorker();
  if (!worker) return Promise.reject(new Error('Web Worker unavailable.'));
  const id = ++spectralWorkerSeq;
  return new Promise((resolve, reject) => {
    spectralWorkerPending.set(id, { resolve, reject });
    worker.postMessage({
      id,
      monoBuffer: mono.buffer,
      sr,
      duration,
      targetCols,
      targetBins,
    }, [mono.buffer]);
  });
}

// Full-spectrum frequency palette used by the generated cover artwork.
// The previous palette was intentionally moody but over-concentrated in blue,
// magenta and red.  This closed colour wheel deliberately includes warm, green,
// teal, cyan, blue, violet and pink regions.  Each song rotates around the wheel
// from an audio-derived fingerprint, while frequency still selects the local hue.
const coverFrequencyPalette = [
  [226, 70, 61],   // red
  [239, 121, 44],  // orange
  [232, 184, 48],  // gold / yellow
  [154, 190, 55],  // lime
  [68, 178, 91],   // green
  [34, 177, 139],  // emerald
  [31, 172, 181],  // teal
  [42, 151, 211],  // cyan / sky
  [66, 107, 218],  // royal blue
  [108, 79, 214],  // violet
  [166, 71, 202],  // purple
  [214, 69, 151],  // pink
  [226, 70, 61],   // close the wheel back at red
];

function mediaUrl(jobId, path) {
  return `/media/${encodeURIComponent(jobId)}/${path.split('/').map(encodeURIComponent).join('/')}`;
}
function downloadUrl(jobId, path) {
  return `/download/${encodeURIComponent(jobId)}/${path.split('/').map(encodeURIComponent).join('/')}`;
}
function formatTime(sec) {
  if (!Number.isFinite(sec) || sec < 0) return '0:00';
  sec = Math.floor(sec);
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
}
function clamp(v, min = 0, max = 1) {
  return Math.max(min, Math.min(max, v));
}
function stripExt(name) {
  return String(name || 'Untitled Track').replace(/\.[^.]+$/, '') || 'Untitled Track';
}

function cleanProjectTitle(value) {
  const title = String(value || '').replace(/\s+/g, ' ').trim().slice(0, 160);
  return title || 'Untitled Track';
}
function getProjectTitle() {
  return cleanProjectTitle($('projectTitle')?.value || 'Untitled Track');
}
function setProjectTitle(value, sync = false) {
  const title = cleanProjectTitle(value);
  if ($('projectTitle')) $('projectTitle').value = title;
  if ($('transportTitle') && state.activeSource !== 'library') $('transportTitle').textContent = title;
  if (sync) queueProjectTitleSync();
  return title;
}
async function syncProjectTitleNow() {
  if (!state.jobId) return;
  const title = getProjectTitle();
  try {
    await fetch(`/api/jobs/${encodeURIComponent(state.jobId)}/title`, {
      method: 'PUT',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({project_title:title})
    });
  } catch (_) {}
}
function queueProjectTitleSync() {
  if (state.titleSyncTimer) clearTimeout(state.titleSyncTimer);
  state.titleSyncTimer = setTimeout(() => {
    state.titleSyncTimer = null;
    syncProjectTitleNow();
  }, 350);
}
function setupProjectTitleEditing() {
  const input = $('projectTitle');
  if (!input) return;
  input.addEventListener('input', () => {
    if (state.activeSource !== 'library') $('transportTitle').textContent = cleanProjectTitle(input.value);
    queueProjectTitleSync();
  });
  input.addEventListener('blur', () => {
    setProjectTitle(input.value, true);
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
  });
  $('projectTitleEditBtn')?.addEventListener('click', () => {
    input.focus(); input.select();
  });
}
function sanitizeVisibleText(text) {
  let s = String(text || '');
  s = s.replace(/Stable Audio 3 Medium|Stable Audio 3|Stable Audio|SA3 Medium|SA3/gi, 'audio engine');
  s = s.replace(/MelBand[- ]RoFormer|RoFormer/gi, 'layer engine');
  s = s.replace(/SAME-L/gi, 'decoder');
  s = s.replace(/TFLite\/XNNPACK/gi, 'compatibility backend');
  s = s.replace(/stableaudio3_(gpu|tflite)/gi, 'audio engine');
  s = s.replace(/melband[-_ ]?roformer[-_ ]?infer/gi, 'layer engine');
  return s;
}
function backendLabel(v) {
  const x = String(v || '').toLowerCase();
  if (x === 'gpu') return 'Accelerated';
  if (x === 'cpu') return 'Compatibility';
  return 'Automatic';
}

function setSideEngine(label, mode = '') {
  $('sideEngineText').textContent = label;
  $('sideEngineText').title = label;
  $('sideEngineDot').className = `engine-dot ${mode}`.trim();
}

function compactDeviceName(name) {
  const raw = String(name || '').trim();
  if (!raw) return '';
  return raw
    .replace(/^NVIDIA\s+/i, '')
    .replace(/^AMD\s+/i, '')
    .replace(/Laptop GPU$/i, 'Laptop')
    .replace(/Graphics$/i, '')
    .replace(/\s{2,}/g, ' ')
    .trim();
}

async function refreshBackend() {
  try {
    const r = await fetch('/api/backends', { cache: 'no-store' });
    const x = await r.json();
    const gpuName = compactDeviceName(x.gpu_engine?.gpu || x.gpu?.name);
    const gpuLabel = gpuName || 'GPU';
    if (x.gpu_engine?.state === 'ready') {
      setSideEngine(gpuLabel, 'ready');
    } else if (x.gpu_engine?.state === 'busy') {
      setSideEngine(gpuLabel, 'busy');
    } else if (x.gpu_engine?.state === 'starting') {
      setSideEngine(gpuLabel, 'busy');
    } else if (x.gpu?.available) {
      setSideEngine(gpuLabel, 'ready');
    } else if (x.cpu?.available) {
      setSideEngine('CPU', 'ready');
    } else {
      setSideEngine('No backend');
    }
  } catch (_) {
    setSideEngine('Unknown device');
  }
}

function updateProjectFromFile(file) {
  if (!file) return;
  const title = stripExt(file.name);
  const ext = (file.name.split('.').pop() || 'audio').toUpperCase();
  setProjectTitle(title, false);
  $('projectFormat').textContent = `${ext} source`;
  state.projectTransportMeta = 'Source loaded · local project';
  if (state.activeSource !== 'library') $('transportMeta').textContent = state.projectTransportMeta;

  if (state.selectedFileUrl) URL.revokeObjectURL(state.selectedFileUrl);
  state.selectedFileUrl = URL.createObjectURL(file);
  const probe = document.createElement('audio');
  probe.preload = 'metadata';
  probe.src = state.selectedFileUrl;
  probe.addEventListener('loadedmetadata', () => {
    const d = Number(probe.duration || 0);
    state.projectDuration = d;
    $('projectDuration').textContent = formatTime(d);
    $('originalMetaDuration').textContent = formatTime(d);
    $('transportTime').textContent = `0:00 / ${formatTime(d)}`;
    drawAllPlaceholders();
  }, { once: true });
}

function updateProjectFromReference(item) {
  if (!item) return;
  if (state.selectedFileUrl) { URL.revokeObjectURL(state.selectedFileUrl); state.selectedFileUrl = null; }
  const name = item.original_name || item.title || 'Reference Track';
  const title = stripExt(item.title || name);
  const ext = (String(name).split('.').pop() || 'audio').toUpperCase();
  const d = Number(item.duration || 0);
  state.projectDuration = d;
  setProjectTitle(title, false);
  $('projectFormat').textContent = `${ext} · Reference Library`;
  state.projectTransportMeta = 'Reference Library source · local';
  if (state.activeSource !== 'library') $('transportMeta').textContent = state.projectTransportMeta;
  $('projectDuration').textContent = d ? formatTime(d) : '--:--';
  $('originalMetaDuration').textContent = d ? formatTime(d) : '--:--';
  $('transportTime').textContent = `0:00 / ${d ? formatTime(d) : '0:00'}`;
  drawAllPlaceholders();
}

function setSourceMode(mode) {
  if (!['file','reference','mic'].includes(mode)) mode = 'file';
  state.sourceMode = mode;
  document.querySelectorAll('.source-mode').forEach(btn => btn.classList.toggle('active', btn.dataset.sourceMode === mode));
  document.querySelectorAll('[data-source-panel]').forEach(panel => panel.classList.toggle('hidden', panel.dataset.sourcePanel !== mode));
  if (mode === 'reference') refreshReferenceSourceOptions();
}

async function refreshReferenceSourceOptions(preselectId = null) {
  const select = $('referenceSourceSelect');
  if (!select) return;
  const wanted = preselectId || state.referenceSource?.id || select.value;
  select.innerHTML = '<option value="">Loading references…</option>';
  try {
    const r = await fetch('/api/library/references', {cache:'no-store'});
    const x = await r.json();
    if (!r.ok) throw new Error(x.error || 'Could not load Reference Library');
    state.referenceItems = x.items || [];
    select.innerHTML = '<option value="">Select a reference track…</option>';
    state.referenceItems.forEach(item => {
      const opt = document.createElement('option');
      opt.value = item.id;
      opt.textContent = `${item.title || 'Untitled Reference'}${item.duration ? ` · ${formatTime(Number(item.duration))}` : ''}`;
      select.appendChild(opt);
    });
    if (wanted && state.referenceItems.some(x => x.id === wanted)) {
      select.value = wanted;
      state.referenceSource = state.referenceItems.find(x => x.id === wanted);
    } else if (wanted) {
      state.referenceSource = null;
    }
    $('referenceSelectedNote').textContent = state.referenceSource ? `Selected: ${state.referenceSource.title}` : (state.referenceItems.length ? 'Choose a track from the menu above.' : 'Reference Library is empty. Upload songs from the Reference Library page.');
  } catch (e) {
    select.innerHTML = '<option value="">Reference Library unavailable</option>';
    $('referenceSelectedNote').textContent = sanitizeVisibleText(e.message);
  }
}

function setupSourceModes() {
  document.querySelectorAll('.source-mode').forEach(btn => btn.addEventListener('click', () => setSourceMode(btn.dataset.sourceMode)));
  $('referenceSourceSelect').addEventListener('change', e => {
    const item = state.referenceItems.find(x => x.id === e.target.value) || null;
    state.referenceSource = item;
    if (item) {
      setSourceMode('reference');
      updateProjectFromReference(item);
      $('referenceSelectedNote').textContent = `Selected: ${item.title}`;
    } else {
      $('referenceSelectedNote').textContent = 'No reference selected.';
    }
  });
}

function dbToLinear(db) { return Math.pow(10, Number(db || 0) / 20); }

function stopBackingMonitor(reset=false) {
  const a=$('micBackingPreview');
  if(!a) return;
  try { a.pause(); if(reset)a.currentTime=0; } catch (_) {}
}

function clearMicBacking() {
  stopBackingMonitor(true);
  state.micBackingFile=null;
  if(state.micBackingUrl) URL.revokeObjectURL(state.micBackingUrl);
  state.micBackingUrl=null;
  const input=$('micBackingInput'); if(input) input.value='';
  const a=$('micBackingPreview'); if(a){a.removeAttribute('src');a.load();a.classList.add('hidden');}
  $('micBackingName').textContent='No melody loaded';
  $('micBackingMeta').textContent='WAV, FLAC, MP3, M4A, AAC, OGG or WebM';
  $('micBackingClearBtn').disabled=true;
  $('micAiReady').classList.add('hidden');
  if(state.recordedFile)$('micStatus').textContent='Recording ready · add a melody or process the voice alone.';
}

function setMicBackingFile(file) {
  if(!file) return;
  if(state.micBackingUrl) URL.revokeObjectURL(state.micBackingUrl);
  state.micBackingFile=file;
  state.micBackingUrl=URL.createObjectURL(file);
  const a=$('micBackingPreview');
  a.src=state.micBackingUrl;a.classList.remove('hidden');
  a.volume=Math.max(0,Math.min(1,dbToLinear($('micBackingGain').value)));
  $('micBackingName').textContent=file.name;
  $('micBackingMeta').textContent=`${(file.size/1024/1024).toFixed(1)} MB · will be mixed server-side with the microphone recording`;
  $('micBackingClearBtn').disabled=false;
  if(state.recordedFile){
    $('micStatus').textContent='Voice + backing melody ready for AI processing.';
    $('micAiReady').classList.remove('hidden');
  }
}

function setupMicBacking() {
  const input=$('micBackingInput');
  $('micBackingBrowseBtn').addEventListener('click',()=>input.click());
  $('micBackingClearBtn').addEventListener('click',clearMicBacking);
  input.addEventListener('change',()=>setMicBackingFile(input.files?.[0]||null));
  $('micVoiceGain').addEventListener('input',e=>$('micVoiceGainValue').textContent=`${Number(e.target.value).toFixed(1)} dB`);
  $('micBackingGain').addEventListener('input',e=>{
    $('micBackingGainValue').textContent=`${Number(e.target.value).toFixed(1)} dB`;
    $('micBackingPreview').volume=Math.max(0,Math.min(1,dbToLinear(e.target.value)));
  });
}

function bestRecordingMime() {
  if (!window.MediaRecorder) return '';
  const choices = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'];
  return choices.find(x => MediaRecorder.isTypeSupported?.(x)) || '';
}

function stopMicStream() {
  if (state.micStream) state.micStream.getTracks().forEach(t => t.stop());
  state.micStream = null;
}

function clearMicRecording() {
  stopBackingMonitor(true);
  if (state.micRecorder && state.micRecorder.state !== 'inactive') { try { state.micRecorder.stop(); } catch (_) {} }
  stopMicStream();
  state.micRecorder = null;
  state.recordedFile = null;
  if (state.micPreviewUrl) URL.revokeObjectURL(state.micPreviewUrl);
  state.micPreviewUrl = null;
  $('micPreview').pause(); $('micPreview').removeAttribute('src'); $('micPreview').load(); $('micPreview').classList.add('hidden');
  $('micStatus').textContent = state.micBackingFile ? 'Microphone idle · backing melody armed.' : 'Microphone idle · backing melody is optional.';
  $('micAiReady').classList.add('hidden');
  $('micRecordBtn').disabled = false; $('micStopBtn').disabled = true; $('micClearBtn').disabled = true;
  $('micOrb').classList.remove('recording');
}

async function startMicRecording() {
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    $('micStatus').textContent = 'Microphone recording is not supported by this browser runtime.';
    return;
  }
  try {
    if (state.micPreviewUrl) { URL.revokeObjectURL(state.micPreviewUrl); state.micPreviewUrl = null; }
    state.recordedFile = null;
    const stream = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:false,noiseSuppression:false,autoGainControl:false,sampleRate:{ideal:48000},channelCount:{ideal:2}}});
    state.micStream = stream;
    const mime = bestRecordingMime();
    const recorderOpts={audioBitsPerSecond:256000}; if(mime)recorderOpts.mimeType=mime; const recorder = new MediaRecorder(stream, recorderOpts);
    const chunks = [];
    state.micRecorder = recorder;
    recorder.addEventListener('dataavailable', e => { if (e.data?.size) chunks.push(e.data); });
    recorder.addEventListener('stop', () => {
      const type = recorder.mimeType || mime || 'audio/webm';
      const ext = type.includes('ogg') ? 'ogg' : 'webm';
      const blob = new Blob(chunks, {type});
      const stamp = new Date().toISOString().replace(/[:.]/g,'-').slice(0,19);
      const file = new File([blob], `JoyMetric Recording ${stamp}.${ext}`, {type});
      state.recordedFile = file;
      state.micPreviewUrl = URL.createObjectURL(blob);
      $('micPreview').src = state.micPreviewUrl; $('micPreview').classList.remove('hidden');
      stopBackingMonitor(false);
      $('micStatus').textContent = state.micBackingFile ? `Voice + melody ready · recording ${(blob.size/1024/1024).toFixed(1)} MB` : `Recording ready · ${(blob.size/1024/1024).toFixed(1)} MB`;
      $('micRecordBtn').disabled = false; $('micStopBtn').disabled = true; $('micClearBtn').disabled = false;
      $('micOrb').classList.remove('recording');
      $('micAiReady').classList.toggle('hidden', !state.micBackingFile);
      stopMicStream();
      updateProjectFromFile(file);
      setSourceMode('mic');
    });
    recorder.start(250);
    if(state.micBackingFile && $('micBackingMonitor').checked){
      const backing=$('micBackingPreview');
      try{
        backing.currentTime=0;
        backing.loop=$('micBackingLoop').checked;
        backing.volume=Math.max(0,Math.min(1,dbToLinear($('micBackingGain').value)));
        await backing.play();
      }catch(_){ /* recording must still work if local preview playback is blocked */ }
    }
    $('micStatus').textContent = state.micBackingFile ? 'Recording voice over backing… click Stop when finished.' : 'Recording… click Stop when finished.';
    $('micRecordBtn').disabled = true; $('micStopBtn').disabled = false; $('micClearBtn').disabled = true;
    $('micOrb').classList.add('recording');
  } catch (e) {
    stopMicStream();
    $('micStatus').textContent = `Microphone unavailable: ${sanitizeVisibleText(e.message)}`;
    $('micRecordBtn').disabled = false; $('micStopBtn').disabled = true;
    $('micOrb').classList.remove('recording');
  }
}

function setupMicrophoneRecorder() {
  setupMicBacking();
  $('micRecordBtn').addEventListener('click', startMicRecording);
  $('micStopBtn').addEventListener('click', () => { stopBackingMonitor(false); if (state.micRecorder?.state === 'recording') state.micRecorder.stop(); });
  $('micClearBtn').addEventListener('click', clearMicRecording);
}

function setupFilePicker() {
  const dz = $('dropzone');
  const input = $('audioFile');
  input.addEventListener('change', () => {
    const file = input.files?.[0];
    $('fileLabel').textContent = file?.name || 'Upload audio or drop a file here';
    if (file) { state.referenceSource = null; state.recordedFile = null; setSourceMode('file'); updateProjectFromFile(file); }
  });
  for (const ev of ['dragenter', 'dragover']) {
    dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add('drag'); });
  }
  for (const ev of ['dragleave', 'drop']) {
    dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove('drag'); });
  }
  dz.addEventListener('drop', e => {
    if (!e.dataTransfer.files?.length) return;
    const dt = new DataTransfer();
    dt.items.add(e.dataTransfer.files[0]);
    input.files = dt.files;
    $('fileLabel').textContent = input.files[0].name;
    state.referenceSource = null; state.recordedFile = null; setSourceMode('file'); updateProjectFromFile(input.files[0]);
  });
}

function setupPromptChips() {
  document.querySelectorAll('#promptChips button').forEach(btn => {
    btn.addEventListener('click', () => {
      $('prompt').value = btn.dataset.prompt || btn.textContent;
      $('prompt').focus();
    });
  });
  $('examplesBtn').addEventListener('click', () => {
    const chips = document.querySelector('#promptChips');
    chips.scrollTo({ left: chips.scrollWidth, behavior: 'smooth' });
  });
}

function setupSliders() {
  $('vocalNoise').addEventListener('input', e => $('vocalNoiseValue').textContent = Number(e.target.value).toFixed(2));
  $('instNoise').addEventListener('input', e => $('instNoiseValue').textContent = Number(e.target.value).toFixed(2));
}

function setNavActive(name) {
  document.querySelectorAll('.nav-item').forEach(btn => btn.classList.remove('active'));
  document.querySelectorAll('.nav-item[data-nav]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.nav === name);
  });
}
function showEditorView() {
  clearLibraryCoverBindings();
  $('libraryView').classList.add('hidden');
  $('libraryView').removeAttribute('data-collection');
  $('realtimeView')?.classList.add('hidden');
  $('agenticView')?.classList.add('hidden');
  $('workArea').classList.remove('hidden');
  state.activeLibrary = null;
}
function clearVisualBindings() {
  state.visualEpoch += 1;
  state.visualDisposers.splice(0).forEach(fn => { try { fn(); } catch (_) {} });
  state.waveItems = [];
  state.spectrumItems = [];
}

function resetOutputVisuals() {
  clearVisualBindings();
  state.outputs = null;
  clearPlaybackSession();
  state.libraryTrackUrl = null;
  state.libraryTrackId = null;
  state.libraryTrackCollection = null;
  state.projectCoverUrl = null;
  setTransportCoverUrl(null);
  ['protectedVocals','originalInstrumental','editedVocals'].forEach(id => {
    const a=$(id); if(!a)return; try{a.pause();}catch(_){} a.removeAttribute('src'); a.load();
  });
  ['originalPlayhead','modifiedPlayhead'].forEach(id => { const el=$(id); if(el){el.style.left='0%';el.style.opacity='0';} });
  ['originalMeter','modifiedMeter'].forEach(id => { const el=$(id); if(el)el.style.height='0%'; });
  ['originalPeak','modifiedPeak'].forEach(id => { const el=$(id); if(el)el.textContent='—'; });
  state.activeSource = 'original';
  $('transportSeek').value = 0;
  syncPlayButtons();
  requestAnimationFrame(() => requestAnimationFrame(() => drawAllPlaceholders()));
}

function redrawWorkspaceVisuals() {
  requestAnimationFrame(() => {
    if (!state.outputs) {
      drawAllPlaceholders();
      return;
    }
    state.waveItems.forEach(item => item.audio.dispatchEvent(new Event('timeupdate')));
    state.spectrumItems.forEach(item => drawSpectrum(item.canvas, item.surface, item.tone));
  });
}
function showMasterPane(focus = 'original') {
  $('masterPane').classList.remove('hidden');
  $('stemPane').classList.add('hidden');
  document.querySelectorAll('.workspace-tab').forEach(btn => {
    btn.classList.remove('active');
    if (btn.dataset.tab === 'stems') btn.setAttribute('aria-pressed','false');
  });
  redrawWorkspaceVisuals();
  if (focus === 'modified') $('modifiedCard').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  if (focus === 'original') $('originalCard').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
function showStemPane() {
  $('masterPane').classList.add('hidden');
  $('stemPane').classList.remove('hidden');
  document.querySelectorAll('.workspace-tab').forEach(btn => {
    const on = btn.dataset.tab === 'stems';
    btn.classList.toggle('active', on);
    if (on) btn.setAttribute('aria-pressed','true');
  });
  redrawWorkspaceVisuals();
}
function syncSettingsState(open) {
  state.settingsOpen = Boolean(open);
  $('advancedOptions').open = state.settingsOpen;
  $('settingsBtn').classList.toggle('active', state.settingsOpen);
  const sideSettings = document.querySelector('.nav-item[data-nav="settings"]');
  if (sideSettings) sideSettings.classList.toggle('active', state.settingsOpen);
}
function toggleSettings() {
  showEditorView();
  const next = !$('advancedOptions').open;
  syncSettingsState(next);
  if (next) {
    setNavActive('settings');
    $('advancedOptions').scrollIntoView({ behavior: 'smooth', block: 'center' });
  } else {
    setNavActive('home');
    $('prompt').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}
function setupNavigation() {
  document.querySelectorAll('[data-nav]').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = btn.dataset.nav;
      showEditorView();
      if (target === 'home') {
        syncSettingsState(false);
        setNavActive('home');
        $('editorSection').scrollTo({ top: 0, behavior: 'smooth' });
        showMasterPane(state.activeSource === 'modified' ? 'modified' : 'original');
      } else if (target === 'realtime') {
        syncSettingsState(false);
        showRealtimeView();
      } else if (target === 'agentic') {
        syncSettingsState(false);
        showAgenticDJView();
      } else if (target === 'settings') {
        toggleSettings();
      }
    });
  });
  document.querySelectorAll('[data-tab]').forEach(btn => {
    btn.addEventListener('click', () => {
      const t = btn.dataset.tab;
      showEditorView();
      syncSettingsState(false);
      setNavActive('home');
      if (t === 'stems') {
        const stemIsOpen = !$('stemPane').classList.contains('hidden');
        if (stemIsOpen) showMasterPane(state.activeSource === 'modified' ? 'modified' : 'original');
        else showStemPane();
      } else {
        showMasterPane(t);
      }
    });
  });
  $('settingsBtn').addEventListener('click', toggleSettings);
  $('advancedOptions').addEventListener('toggle', () => {
    state.settingsOpen = $('advancedOptions').open;
    $('settingsBtn').classList.toggle('active', state.settingsOpen);
    const sideSettings = document.querySelector('.nav-item[data-nav="settings"]');
    if (sideSettings && state.settingsOpen) setNavActive('settings');
  });
}
function progressClassFor(pct) {
  if (pct < 12) return 0;
  if (pct < 31) return 1;
  if (pct < 88) return 2;
  return 3;
}
function setProgress(stage, pct, message) {
  const p = Math.round(clamp(Number(pct) || 0, 0, 100));
  $('progressPct').textContent = `${p}%`;
  $('progressBar').style.width = `${p}%`;
  $('progressRing').style.setProperty('--p', p);
  $('stageText').textContent = sanitizeVisibleText(stage || 'Processing');
  $('messageText').textContent = sanitizeVisibleText(message || '');
  $('progressStatus').textContent = p >= 100 ? 'Complete' : p > 0 ? 'Working' : 'Ready';

  const current = progressClassFor(p);
  const ids = ['stepAnalyze', 'stepSeparate', 'stepTransform', 'stepRender'];
  ids.forEach((id, idx) => {
    const el = $(id);
    el.classList.remove('active', 'done');
    if (p >= 100 || idx < current) el.classList.add('done');
    else if (idx === current && p > 0) el.classList.add('active');
  });
}

function activeColorTheme(){
  let t=String(document.documentElement.dataset.theme||'original');
  if(t==='graphite')t='original';
  return ['original','sand','night','metal','green','live','crimson','teal','royal','sunset','arctic','neon','emerald','amber','slate','vapor'].includes(t)?t:'original';
}
function paletteFor(tone) {
  if(activeColorTheme()==='live'){
    const c=liveThemeState.current;
    const pick=(base,amount)=>mixRgb(base,[255,255,255],amount);
    const dim=(base,amount)=>mixRgb(base,[4,7,10],amount);
    const palettes={
      original:{waveIdle:rgba(dim(c.accent,.56),1),waveActive:rgba(pick(c.accent,.16),1),glow:rgba(c.accent,.38),low:dim(c.accent3,.20),high:pick(c.accent,.18)},
      modified:{waveIdle:rgba(dim(c.accent2,.54),1),waveActive:rgba(pick(c.accent2,.16),1),glow:rgba(c.accent2,.38),low:dim(c.accent2,.18),high:pick(c.accent2,.22)},
      vocal:{waveIdle:rgba(dim(c.accent3,.52),1),waveActive:rgba(pick(c.accent3,.20),1),glow:rgba(c.accent3,.32),low:dim(c.accent3,.16),high:pick(c.accent3,.24)},
      instrument:{waveIdle:rgba(dim(mixRgb(c.accent,c.accent3,.52),.55),1),waveActive:rgba(pick(mixRgb(c.accent,c.accent3,.52),.17),1),glow:rgba(mixRgb(c.accent,c.accent3,.52),.32),low:dim(c.accent,.22),high:pick(c.accent3,.18)}
    };
    return palettes[tone]||palettes.original;
  }
  const themes = {
    original: {
      original: { waveIdle: '#1d3b63', waveActive: '#4b9cf5', glow: 'rgba(70,156,248,.35)', low: [31,83,160], high: [73,170,255] },
      modified: { waveIdle: '#4c2869', waveActive: '#b653f4', glow: 'rgba(180,84,255,.36)', low: [91,37,132], high: [206,86,246] },
      vocal: { waveIdle: '#553149', waveActive: '#e77eb9', glow: 'rgba(229,122,187,.3)', low: [91,47,76], high: [238,133,191] },
      instrument: { waveIdle: '#214550', waveActive: '#62d0ee', glow: 'rgba(101,214,247,.3)', low: [28,76,91], high: [89,215,245] },
    },
    sand: {
      original: { waveIdle: '#6f5a3b', waveActive: '#ead8ab', glow: 'rgba(226,188,108,.34)', low: [105,76,40], high: [235,210,155] },
      modified: { waveIdle: '#76501f', waveActive: '#d6a64f', glow: 'rgba(216,164,72,.38)', low: [112,72,28], high: [220,171,82] },
      vocal: { waveIdle: '#755f42', waveActive: '#dfc58f', glow: 'rgba(220,191,133,.28)', low: [103,80,50], high: [226,199,145] },
      instrument: { waveIdle: '#66513a', waveActive: '#b88a45', glow: 'rgba(186,137,67,.30)', low: [88,66,42], high: [191,145,74] },
    },
    night: {
      original: { waveIdle: '#5b4933', waveActive: '#d9be86', glow: 'rgba(217,178,100,.31)', low: [82,58,31], high: [221,190,128] },
      modified: { waveIdle: '#67451e', waveActive: '#c88b37', glow: 'rgba(209,144,53,.38)', low: [94,58,22], high: [210,151,61] },
      vocal: { waveIdle: '#554631', waveActive: '#c9ae7d', glow: 'rgba(203,171,113,.25)', low: [80,63,41], high: [207,177,125] },
      instrument: { waveIdle: '#493a2a', waveActive: '#9d7139', glow: 'rgba(165,113,50,.25)', low: [68,50,31], high: [173,124,60] },
    },
    metal: {
      original: { waveIdle: '#29445a', waveActive: '#8fc5ec', glow: 'rgba(103,177,229,.34)', low: [37,69,94], high: [145,200,238] },
      modified: { waveIdle: '#465d72', waveActive: '#d0dde8', glow: 'rgba(191,216,235,.30)', low: [61,83,104], high: [205,224,238] },
      vocal: { waveIdle: '#315561', waveActive: '#88c5cf', glow: 'rgba(118,194,207,.27)', low: [42,81,93], high: [140,203,213] },
      instrument: { waveIdle: '#4b5963', waveActive: '#aebbc4', glow: 'rgba(177,195,207,.24)', low: [66,79,88], high: [178,194,204] },
    },
    green: {
      original: { waveIdle: '#345344', waveActive: '#a8c8a2', glow: 'rgba(147,190,144,.30)', low: [49,82,65], high: [170,205,163] },
      modified: { waveIdle: '#586140', waveActive: '#c3b96f', glow: 'rgba(192,182,105,.30)', low: [78,91,59], high: [199,188,113] },
      vocal: { waveIdle: '#50634f', waveActive: '#b8c8a7', glow: 'rgba(176,197,159,.25)', low: [72,94,72], high: [187,205,169] },
      instrument: { waveIdle: '#3d584c', waveActive: '#85aa97', glow: 'rgba(123,169,149,.25)', low: [53,83,69], high: [137,177,157] },
    }
  };
  const map=themes[activeColorTheme()]||themes.original;
  return map[tone] || map.original;
}
function visualSurfaceTheme(){
  const t=activeColorTheme();
  if(t==='original')return {background:'#090e15',grid:'rgba(255,255,255,.035)'};
  if(t==='sand')return {background:'#17110c',grid:'rgba(221,181,105,.055)'};
  if(t==='night')return {background:'#0d0a08',grid:'rgba(207,159,76,.050)'};
  if(t==='metal')return {background:'#09121a',grid:'rgba(135,184,220,.050)'};
  if(t==='green')return {background:'#0d1510',grid:'rgba(143,177,130,.050)'};
  if(t==='live'){
    const c=liveThemeState.current;
    return {background:rgba(mixRgb(c.bg,[0,0,0],.18),1),grid:rgba(mixRgb(c.border,[255,255,255],.08),.10)};
  }
  return {background:'#090e15',grid:'rgba(255,255,255,.035)'};
}
function mixRgb(a, b, t) {
  return [0,1,2].map(i => Math.round(a[i] + (b[i] - a[i]) * t));
}
function rgba(rgb, a) { return `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${a})`; }

function drawPlaceholderWave(canvas, tone) {
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  if (rect.width < 2 || rect.height < 2) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * dpr);
  canvas.height = Math.round(rect.height * dpr);
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0,0,W,H);
  const p = paletteFor(tone);
  const mid = H/2;
  const n = Math.max(80, Math.floor(rect.width / 4));
  const step = W/n;
  for (let i=0;i<n;i++) {
    const t=i/n;
    const env=.2 + .55*Math.abs(Math.sin(t*11.7)) + .18*Math.abs(Math.sin(t*47.2));
    const h=Math.max(1.2*dpr, Math.min(H*.42, env*H*.33));
    ctx.fillStyle = p.waveIdle;
    ctx.globalAlpha = .38;
    ctx.fillRect(i*step, mid-h, Math.max(1*dpr,step*.55), h*2);
  }
  ctx.globalAlpha=1;
}
function drawPlaceholderSpectrum(canvas, tone) {
  if (!canvas) return;
  const rect=canvas.getBoundingClientRect();
  if (rect.width<2||rect.height<2)return;
  const dpr=window.devicePixelRatio||1;
  canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);
  const ctx=canvas.getContext('2d');const W=canvas.width,H=canvas.height;ctx.clearRect(0,0,W,H);
  const p=paletteFor(tone);
  const cols=Math.max(60,Math.floor(rect.width/7));const rows=14;const cw=W/cols;const rh=H/rows;
  for(let c=0;c<cols;c++){
    for(let r=0;r<rows;r++){
      const t=c/cols;const f=r/(rows-1);
      const v=clamp((.42*Math.abs(Math.sin(t*18+r*.7))+.2*Math.abs(Math.sin(t*57-r)))*(1-f*.45));
      if(v<.14)continue;
      const rgb=mixRgb(p.low,p.high,1-f);
      ctx.fillStyle=rgba(rgb,.06+v*.23);ctx.fillRect(c*cw,H-(r+1)*rh,cw*.74,rh*.72);
    }
  }
}
function drawAllPlaceholders() {
  [['originalWave','original'],['modifiedWave','modified'],['vocalWave','vocal'],['instrumentWave','instrument']].forEach(([id,t])=>drawPlaceholderWave($(id),t));
  [['originalSpectrum','original'],['modifiedSpectrum','modified'],['vocalSpectrum','vocal'],['instrumentSpectrum','instrument']].forEach(([id,t])=>drawPlaceholderSpectrum($(id),t));
  buildTimeAxis('originalTimeAxis', state.projectDuration || 270);
  buildTimeAxis('modifiedTimeAxis', state.projectDuration || 270);
}

function attachWaveform(canvas, audio, wave, tone, playheadEl, peakEl, meterEl) {
  if (!canvas || !audio || !wave?.peaks?.length) return;
  const item = { canvas, audio, wave, tone, playheadEl, peakEl, meterEl };
  state.waveItems.push(item);

  const draw = () => {
    const rect=canvas.getBoundingClientRect();
    if(rect.width<2||rect.height<2)return;
    const dpr=window.devicePixelRatio||1;
    const W=Math.round(rect.width*dpr),H=Math.round(rect.height*dpr);
    if(canvas.width!==W||canvas.height!==H){canvas.width=W;canvas.height=H;}
    const ctx=canvas.getContext('2d');ctx.clearRect(0,0,W,H);
    const peaks=wave.peaks;const duration=(Number.isFinite(audio.duration)&&audio.duration>0)?audio.duration:Number(wave.duration||0);
    const played=duration>0?clamp(audio.currentTime/duration):0;
    const pal=paletteFor(tone);const mid=H/2;const maxAmp=H*.40;
    const target=Math.max(90,Math.min(340,Math.floor(rect.width/3.2)));
    const stride=Math.max(1,Math.floor(peaks.length/target));const bars=[];
    for(let i=0;i<peaks.length;i+=stride){let m=0;for(let j=i;j<Math.min(peaks.length,i+stride);j++)m=Math.max(m,peaks[j]);bars.push(m);}
    const step=W/bars.length;const bw=Math.max(1*dpr,step*.62);
    for(let i=0;i<bars.length;i++){
      const nx=i/Math.max(1,bars.length-1);const h=Math.max(1*dpr,Math.pow(Math.max(.001,bars[i]),.76)*maxAmp);
      ctx.fillStyle=nx<=played?pal.waveActive:pal.waveIdle;ctx.globalAlpha=nx<=played?1:.72;
      if(nx<=played){ctx.shadowColor=pal.glow;ctx.shadowBlur=3*dpr;}else ctx.shadowBlur=0;
      ctx.fillRect(i*step,mid-h,bw,h*2);
    }
    ctx.shadowBlur=0;ctx.globalAlpha=1;
    if(playheadEl){playheadEl.style.left=`${played*100}%`;playheadEl.style.opacity=duration?'.8':'0';}
    if(duration&&peakEl&&meterEl){
      const idx=Math.min(peaks.length-1,Math.max(0,Math.floor(played*peaks.length)));
      const amp=Number(peaks[idx]||0);const db=20*Math.log10(Math.max(amp,1e-4));
      peakEl.textContent=Number.isFinite(db)?`${db.toFixed(1)}`:'—';meterEl.style.height=`${clamp((db+48)/48)*100}%`;
    }
  };
  const clickHandler=e=>{
    const d=(Number.isFinite(audio.duration)&&audio.duration>0)?audio.duration:Number(wave.duration||0);if(!d)return;
    const r=canvas.getBoundingClientRect();audio.currentTime=clamp((e.clientX-r.left)/r.width)*d;draw();
  };
  const events=['loadedmetadata','durationchange','timeupdate','seeked','play','pause','ended'];
  canvas.addEventListener('click',clickHandler);
  events.forEach(ev=>audio.addEventListener(ev,draw));
  const ro='ResizeObserver'in window?new ResizeObserver(draw):null;
  ro?.observe(canvas);
  state.visualDisposers.push(()=>{
    canvas.removeEventListener('click',clickHandler);
    events.forEach(ev=>audio.removeEventListener(ev,draw));
    ro?.disconnect();
  });
  draw();
}

function getDecodeContext() {
  if (!window.__joyMetricDecodeCtx) {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    window.__joyMetricDecodeCtx = new AudioCtx();
  }
  return window.__joyMetricDecodeCtx;
}
function downmix(buffer) {
  const mono=new Float32Array(buffer.length);
  for(let c=0;c<buffer.numberOfChannels;c++){
    const ch=buffer.getChannelData(c);for(let i=0;i<mono.length;i++)mono[i]+=ch[i];
  }
  const div=Math.max(1,buffer.numberOfChannels);for(let i=0;i<mono.length;i++)mono[i]/=div;return mono;
}
function downsampleAverage(data,factor){if(factor<=1)return data;const n=Math.max(1,Math.floor(data.length/factor));const out=new Float32Array(n);for(let i=0;i<n;i++){let sum=0,c=0;for(let k=0;k<factor&&i*factor+k<data.length;k++){sum+=data[i*factor+k];c++;}out[i]=c?sum/c:0;}return out;}

async function buildSpectralSurface(url, targetCols=170, targetBins=24) {
  if(spectrumCache.has(url))return spectrumCache.get(url);
  const task=async()=>{
    // Reuse the fully-buffered playback Blob when it already exists. This avoids a
    // second localhost read for the same track while the user is listening.
    if(playbackBlobInflight.has(url)){try{await playbackBlobInflight.get(url);}catch(_){}}
    const buffered=playbackBlobCache.get(url);
    let bytes;
    if(buffered?.blob) bytes=await buffered.blob.arrayBuffer();
    else {const res=await fetch(url,{cache:'no-store'});if(!res.ok)throw new Error(`Spectrum fetch failed (${res.status}).`);bytes=await res.arrayBuffer();}
    const decoded=await getDecodeContext().decodeAudioData(bytes.slice(0));
    let mono=downmix(decoded);let sr=decoded.sampleRate;
    if(sr>12000){const factor=Math.max(1,Math.floor(sr/12000));mono=downsampleAverage(mono,factor);sr=Math.round(sr/factor);}
    // Keep cover/spectrum math off the UI thread. In embedded WebView builds, doing
    // several Goertzel analyses on the main thread while music is playing can steal
    // enough time from the renderer to make otherwise-clean audio sound scratchy.
    // The worker receives a copy so the inline implementation remains a safe fallback.
    try {
      return await analyzeSpectrumInWorker(mono.slice(), sr, decoded.duration, targetCols, targetBins);
    } catch (_) {}
    const frameSize=256;const cols=Math.max(70,Math.min(targetCols,Math.floor(decoded.duration*5.2)||70));
    const hop=Math.max(64,Math.floor(Math.max(1,mono.length-frameSize)/Math.max(1,cols-1)));
    const actualCols=Math.max(1,Math.min(cols,1+Math.floor(Math.max(0,mono.length-frameSize)/hop)));const bins=targetBins;
    const hann=new Float32Array(frameSize);for(let i=0;i<frameSize;i++)hann[i]=.5-.5*Math.cos((2*Math.PI*i)/(frameSize-1));
    const minF=70,maxF=Math.min(sr*.47,7000);const coeffs=new Float32Array(bins);
    for(let b=0;b<bins;b++){const t=b/Math.max(1,bins-1);const f=minF*Math.pow(maxF/minF,t);coeffs[b]=2*Math.cos(2*Math.PI*f/sr);}
    const rawValues=new Float32Array(actualCols*bins);
    const rawBandProfile=new Float32Array(bins);
    const rawTimeProfile=new Float32Array(actualCols);
    let rawTotal=0,rawWeighted=0,rawPeak=0;
    for(let c=0;c<actualCols;c++){
      const start=c*hop;let colSum=0;
      for(let b=0;b<bins;b++){
        const coeff=coeffs[b];let s0=0,s1=0,s2=0;
        for(let n=0;n<frameSize;n++){const idx=start+n;const sample=(idx<mono.length?mono[idx]:0)*hann[n];s0=sample+coeff*s1-s2;s2=s1;s1=s0;}
        const power=Math.max(0,s1*s1+s2*s2-coeff*s1*s2);const v=Math.log1p(power*20);
        rawValues[c*bins+b]=v;rawBandProfile[b]+=v;colSum+=v;rawTotal+=v;rawWeighted+=v*b;rawPeak=Math.max(rawPeak,v);
      }
      rawTimeProfile[c]=colSum/Math.max(1,bins);
    }
    for(let b=0;b<bins;b++)rawBandProfile[b]/=Math.max(1,actualCols);

    // Global, non-normalized descriptors keep real differences between tracks.
    const rawMean=rawTotal/Math.max(1,rawValues.length);
    const rawCentroid=rawTotal>1e-9?rawWeighted/rawTotal/Math.max(1,bins-1):.5;
    let variance=0,flux=0,timeMean=0,timePeak=0;
    for(const v of rawValues)variance+=(v-rawMean)*(v-rawMean);
    for(let c=0;c<actualCols;c++){timeMean+=rawTimeProfile[c];timePeak=Math.max(timePeak,rawTimeProfile[c]);if(c)flux+=Math.abs(rawTimeProfile[c]-rawTimeProfile[c-1]);}
    variance/=Math.max(1,rawValues.length);timeMean/=Math.max(1,actualCols);flux/=Math.max(1,actualCols-1);
    const rawStd=Math.sqrt(variance);
    const crest=timeMean>1e-9?timePeak/timeMean:1;

    // Build a stable fingerprint only from spectral data. This is used to vary
    // palette phase / layout so two genuinely different songs cannot collapse to
    // the same-looking cover after per-track normalization.
    let fingerprint=2166136261>>>0;
    const stride=Math.max(1,Math.floor(rawValues.length/280));
    for(let i=0;i<rawValues.length;i+=stride){
      const q=Math.max(0,Math.min(65535,Math.round(rawValues[i]*4096)));
      fingerprint^=(q&255);fingerprint=Math.imul(fingerprint,16777619)>>>0;
      fingerprint^=(q>>>8);fingerprint=Math.imul(fingerprint,16777619)>>>0;
    }
    for(let b=0;b<bins;b++){
      const q=Math.max(0,Math.min(65535,Math.round(rawBandProfile[b]*4096)));
      fingerprint^=q;fingerprint=Math.imul(fingerprint,16777619)>>>0;
    }

    const sorted=Array.from(rawValues).sort((a,b)=>a-b);const pick=p=>sorted[Math.floor((sorted.length-1)*p)]||0;const floor=pick(.18),ceil=Math.max(pick(.995),floor+1e-6);
    const values=new Float32Array(rawValues.length);
    for(let i=0;i<rawValues.length;i++)values[i]=clamp((rawValues[i]-floor)/(ceil-floor));

    // Preserve a normalized *shape* profile separately. This influences the artwork
    // without erasing the absolute descriptors above.
    let maxBand=1e-9,maxTime=1e-9;
    for(const v of rawBandProfile)maxBand=Math.max(maxBand,v);
    for(const v of rawTimeProfile)maxTime=Math.max(maxTime,v);
    const bandProfile=Float32Array.from(rawBandProfile,v=>clamp(v/maxBand));
    const timeProfile=Float32Array.from(rawTimeProfile,v=>clamp(v/maxTime));

    return{
      duration:decoded.duration,columns:actualCols,bins,values,
      bandProfile,timeProfile,
      fingerprint,
      descriptors:{rawMean,rawStd,rawCentroid:clamp(rawCentroid),flux,crest,rawPeak}
    };
  };
  const promise=spectrumWorkQueue.then(task);
  spectrumWorkQueue=promise.catch(()=>{});
  spectrumCache.set(url,promise);return promise;
}

function drawSpectrum(canvas,surface,tone){
  const rect=canvas.getBoundingClientRect();if(rect.width<2||rect.height<2)return;
  const dpr=window.devicePixelRatio||1;const W=Math.round(rect.width*dpr),H=Math.round(rect.height*dpr);
  if(canvas.width!==W||canvas.height!==H){canvas.width=W;canvas.height=H;}
  const ctx=canvas.getContext('2d');ctx.clearRect(0,0,W,H);const pal=paletteFor(tone);
  const surfaceTheme=visualSurfaceTheme();
  ctx.fillStyle=surfaceTheme.background;ctx.fillRect(0,0,W,H);
  ctx.strokeStyle=surfaceTheme.grid;ctx.lineWidth=Math.max(1,dpr*.6);
  for(let i=1;i<5;i++){const y=H*i/5;ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(W,y);ctx.stroke();}
  for(let i=1;i<7;i++){const x=W*i/7;ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,H);ctx.stroke();}
  if(!surface?.columns){drawPlaceholderSpectrum(canvas,tone);return;}
  const {columns:cols,bins,values}=surface;const cw=W/cols,rh=H/bins;
  for(let c=0;c<cols;c++){
    for(let b=0;b<bins;b++){
      const v=values[c*bins+b];if(v<.035)continue;
      const f=b/Math.max(1,bins-1);const rgb=mixRgb(pal.low,pal.high,f);
      const x=c*cw,y=H-(b+1)*rh;
      ctx.fillStyle=rgba(rgb,.08+v*.75);ctx.shadowColor=rgba(rgb,v*.22);ctx.shadowBlur=v>0.65?5*dpr:0;
      const hh=Math.max(1*dpr,rh*(.45+v*.55));ctx.fillRect(x,y+(rh-hh),Math.max(1*dpr,cw*.72),hh);
    }
  }
  ctx.shadowBlur=0;
}
function attachSpectrum(canvas,url,tone){
  const epoch=state.visualEpoch;
  let disposed=false;
  let ro=null;
  drawPlaceholderSpectrum(canvas,tone);
  state.visualDisposers.push(()=>{disposed=true;ro?.disconnect();});
  buildSpectralSurface(url).then(surface=>{
    if(disposed||epoch!==state.visualEpoch)return;
    const item={canvas,surface,tone};
    state.spectrumItems.push(item);drawSpectrum(canvas,surface,tone);
    if('ResizeObserver'in window){ro=new ResizeObserver(()=>{if(!disposed&&epoch===state.visualEpoch)drawSpectrum(canvas,surface,tone);});ro.observe(canvas);}
  }).catch(()=>{if(!disposed&&epoch===state.visualEpoch)drawPlaceholderSpectrum(canvas,tone);});
}


function coverColorAt(t, phase=0) {
  // Circular full-spectrum lookup.  `phase` is dominated by the song fingerprint,
  // so two songs with similar EQ balance can still land in completely different
  // hue families; `t` remains the frequency position inside that rotated wheel.
  let u=(Number(t)||0)+(Number(phase)||0);u=((u%1)+1)%1;
  const x=u*(coverFrequencyPalette.length-1);
  const i=Math.min(coverFrequencyPalette.length-2,Math.floor(x));
  return mixRgb(coverFrequencyPalette[i],coverFrequencyPalette[i+1],x-i);
}
function normalizeCoverSource(url){
  if(!url)return '';
  try{const u=new URL(String(url),window.location.href);return `${u.pathname||''}${u.search||''}`||String(url);}
  catch(_){return String(url||'');}
}
async function requestCachedCoverInfo(url){
  const source=normalizeCoverSource(url);if(!source)return null;
  try{
    const r=await fetch(`/api/cover-cache?source=${encodeURIComponent(source)}`,{cache:'no-store'});
    if(!r.ok)return null;
    const x=await r.json();
    if(x?.url){coverPersistedSources.add(source);return {source,url:String(x.url)};}
  }catch(_){}
  return null;
}
function loadImageElement(src){
  return new Promise((resolve,reject)=>{
    const img=new Image();img.decoding='async';
    img.onload=()=>resolve(img);img.onerror=()=>reject(new Error('Image load failed'));img.src=src;
  });
}
async function getCachedCoverImage(url){
  const source=normalizeCoverSource(url);if(!source)return null;
  if(coverImageCache.has(source))return coverImageCache.get(source);
  if(coverImageInflight.has(source))return coverImageInflight.get(source);
  const pending=(async()=>{
    const info=await requestCachedCoverInfo(source);
    if(!info?.url)return null;
    const img=await loadImageElement(info.url);
    coverImageCache.set(source,img);
    return img;
  })().finally(()=>coverImageInflight.delete(source));
  coverImageInflight.set(source,pending);
  return pending;
}
function drawCoverImage(canvas,img){
  if(!canvas||!img){drawCoverPlaceholder(canvas);return;}
  const {W,H}=sizeCoverCanvas(canvas);const ctx=canvas.getContext('2d');ctx.clearRect(0,0,W,H);
  ctx.drawImage(img,0,0,W,H);
  canvas.parentElement?.style.setProperty('--cover-accent','rgba(164,174,184,.34)');
}
function coverRgbCss(rgb,a=1){return `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${a})`;}
function sizeCoverCanvas(canvas,forceWidth=0,forceHeight=0) {
  const dpr=Math.min(2.25,window.devicePixelRatio||1);
  let W=0,H=0;
  if(forceWidth>0&&forceHeight>0){W=Math.max(96,Math.round(forceWidth));H=Math.max(96,Math.round(forceHeight));}
  else {
    const rect=canvas.getBoundingClientRect();
    const cssW=Math.max(48,rect.width||canvas.width||64),cssH=Math.max(48,rect.height||canvas.height||64);
    W=Math.max(96,Math.round(cssW*dpr));H=Math.max(96,Math.round(cssH*dpr));
  }
  if(canvas.width!==W||canvas.height!==H){canvas.width=W;canvas.height=H;}
  return {W,H,dpr};
}
function drawCoverPlaceholder(canvas) {
  if(!canvas)return;
  const {W,H}=sizeCoverCanvas(canvas);const ctx=canvas.getContext('2d');ctx.clearRect(0,0,W,H);
  const base=ctx.createLinearGradient(0,0,W,H);base.addColorStop(0,'#151922');base.addColorStop(.48,'#20242b');base.addColorStop(1,'#11151c');ctx.fillStyle=base;ctx.fillRect(0,0,W,H);
  ctx.save();ctx.globalCompositeOperation='screen';ctx.filter=`blur(${Math.max(9,W*.075)}px)`;
  [[.18,.18,.55,[60,72,86]],[.82,.32,.62,[86,76,63]],[.56,.86,.58,[65,81,76]]].forEach(([x,y,r,c])=>{
    const g=ctx.createRadialGradient(x*W,y*H,0,x*W,y*H,r*W);g.addColorStop(0,coverRgbCss(c,.48));g.addColorStop(1,coverRgbCss(c,0));ctx.fillStyle=g;ctx.fillRect(-W*.2,-H*.2,W*1.4,H*1.4);
  });
  ctx.restore();
  const shade=ctx.createRadialGradient(W*.46,H*.40,W*.05,W*.5,H*.5,W*.78);shade.addColorStop(0,'rgba(255,255,255,.03)');shade.addColorStop(.65,'rgba(0,0,0,.02)');shade.addColorStop(1,'rgba(0,0,0,.38)');ctx.fillStyle=shade;ctx.fillRect(0,0,W,H);
  canvas.parentElement?.style.setProperty('--cover-accent','rgba(112,122,132,.28)');
}
function spectrumSliceStats(surface,startCol,endCol) {
  const cols=surface.columns,bins=surface.bins,values=surface.values;
  let total=0,weighted=0,peak=0,peakBin=0;
  const a=Math.max(0,Math.min(cols-1,startCol)),z=Math.max(a+1,Math.min(cols,endCol));
  for(let c=a;c<z;c++)for(let b=0;b<bins;b++){
    const v=values[c*bins+b];const w=v*v;total+=w;weighted+=w*b;if(v>peak){peak=v;peakBin=b;}
  }
  const denom=Math.max(1,(z-a)*bins);const energy=Math.sqrt(total/denom);
  const centroid=total>1e-9?weighted/total/Math.max(1,bins-1):.45;
  return {energy:clamp(energy*1.9),centroid:clamp(centroid),peak:clamp(peak),peakNorm:peakBin/Math.max(1,bins-1)};
}
function mulberry32(seed){let a=(seed>>>0)||0x6d2b79f5;return()=>{a=(a+0x6D2B79F5)>>>0;let t=a;t=Math.imul(t^(t>>>15),t|1);t^=t+Math.imul(t^(t>>>7),t|61);return((t^(t>>>14))>>>0)/4294967296;};}
function renderFrequencyCover(canvas,surface,{forceWidth=0,forceHeight=0}={}) {
  if(!canvas||!surface?.columns){drawCoverPlaceholder(canvas);return;}
  const {W,H,dpr}=sizeCoverCanvas(canvas,forceWidth,forceHeight);const ctx=canvas.getContext('2d');ctx.clearRect(0,0,W,H);
  const desc=surface.descriptors||{};const rand=mulberry32(surface.fingerprint||1);
  const slices=13;const stats=[];
  for(let i=0;i<slices;i++)stats.push(spectrumSliceStats(surface,Math.floor(surface.columns*i/slices),Math.ceil(surface.columns*(i+1)/slices)));

  const bandEnergy=surface.bandProfile?.length===surface.bins?surface.bandProfile:new Float32Array(surface.bins);
  if(!surface.bandProfile){for(let b=0;b<surface.bins;b++){let sum=0;for(let c=0;c<surface.columns;c++)sum+=surface.values[c*surface.bins+b];bandEnergy[b]=sum/Math.max(1,surface.columns);}}
  let low=0,mid=0,high=0,lowN=0,midN=0,highN=0;
  for(let b=0;b<surface.bins;b++){
    const n=bandEnergy[b],t=b/Math.max(1,surface.bins-1);
    if(t<.34){low+=n;lowN++;}else if(t<.68){mid+=n;midN++;}else{high+=n;highN++;}
  }
  low/=Math.max(1,lowN);mid/=Math.max(1,midN);high/=Math.max(1,highN);
  const bandNorm=Math.max(1e-6,low+mid+high);const lowMix=low/bandNorm,midMix=mid/bandNorm,highMix=high/bandNorm;

  // Hue identity is now fingerprint-first.  The FNV spectrum fingerprint is close
  // to uniform over 32-bit space, so the dominant family can genuinely land on
  // orange, yellow, green, teal, cyan, blue, violet, pink or red instead of being
  // pulled toward one fixed subset of the palette.  Spectral descriptors provide
  // a smaller musical nudge without destroying that diversity.
  const fingerprintUnit=(surface.fingerprint>>>0)/4294967295;
  const descriptorNudge=(clamp(desc.rawCentroid||.5)*.09 + clamp((desc.flux||0)*1.6)*.045 + clamp((desc.rawStd||0)*2.2)*.025);
  const phase=(fingerprintUnit*.84 + descriptorNudge)%1;
  const angle=(rand()*.72+.14)*Math.PI*2;
  const x2=W*(.5+.72*Math.cos(angle)),y2=H*(.5+.72*Math.sin(angle));

  // Keep the three broad frequency zones about a third of the wheel apart.  This
  // makes the dominant gradient itself cover a wide chromatic interval instead of
  // collapsing into neighbouring blue/pink/red shades.
  const lowC=coverColorAt(.02+.10*lowMix,phase);
  const midC=coverColorAt(.34+.10*midMix,phase);
  const highC=coverColorAt(.67+.11*highMix,phase);
  const base=ctx.createLinearGradient(W*.5-W*.55*Math.cos(angle),H*.5-H*.55*Math.sin(angle),x2,y2);
  base.addColorStop(0,coverRgbCss(lowC,.99));base.addColorStop(.48,coverRgbCss(midC,.96));base.addColorStop(1,coverRgbCss(highC,.9));ctx.fillStyle=base;ctx.fillRect(0,0,W,H);

  // A low-opacity complementary atmosphere gives every cover a second chromatic
  // pole.  Its position and size are audio-seeded, so it broadens colour variety
  // without looking like a synthetic rainbow overlay.
  ctx.save();ctx.globalCompositeOperation='screen';ctx.filter=`blur(${Math.max(11*dpr,W*.09)}px)`;
  const atmosphereC=coverColorAt(.50,phase);
  const atmosphereX=W*(.18+.64*rand()),atmosphereY=H*(.18+.64*rand()),atmosphereR=W*(.34+.14*rand());
  const atmosphere=ctx.createRadialGradient(atmosphereX,atmosphereY,0,atmosphereX,atmosphereY,atmosphereR);
  atmosphere.addColorStop(0,coverRgbCss(atmosphereC,.18+.10*clamp(desc.flux||0)));
  atmosphere.addColorStop(.46,coverRgbCss(atmosphereC,.09));atmosphere.addColorStop(1,coverRgbCss(atmosphereC,0));
  ctx.fillStyle=atmosphere;ctx.fillRect(atmosphereX-atmosphereR,atmosphereY-atmosphereR,atmosphereR*2,atmosphereR*2);ctx.restore();

  // Temporal/frequency blobs. Geometry is deterministic but seeded by the actual
  // spectrum fingerprint, while x/y remain anchored to time and frequency stats.
  ctx.save();ctx.globalCompositeOperation='screen';ctx.filter=`blur(${Math.max(9*dpr,W*(.066+.025*clamp(desc.rawStd||0)))}px) saturate(1.24)`;
  stats.forEach((st,i)=>{
    const timeT=(i+.5)/slices;const profile=surface.timeProfile?.[Math.min(surface.timeProfile.length-1,Math.floor(timeT*Math.max(0,surface.timeProfile.length-1)))]??st.energy;
    const jitterX=(rand()-.5)*W*(.08+.07*clamp(desc.flux||0));
    const jitterY=(rand()-.5)*H*.16;
    const x=W*(.04+.92*timeT)+jitterX;
    const freq=clamp(st.centroid*.62+st.peakNorm*.38);
    const y=H*(.10+.78*(1-freq))+jitterY;
    const color=coverColorAt(freq,phase+(rand()-.5)*.08);
    const radius=W*(.18+.22*st.energy+.12*profile+.05*rand());
    const alpha=.25+.34*st.energy+.14*profile;
    const g=ctx.createRadialGradient(x,y,0,x,y,radius);g.addColorStop(0,coverRgbCss(color,alpha));g.addColorStop(.34,coverRgbCss(color,alpha*.58));g.addColorStop(1,coverRgbCss(color,0));ctx.fillStyle=g;ctx.fillRect(x-radius,y-radius,radius*2,radius*2);
  });

  // Dominant frequency bands form soft ribbons. The offset comes from the same
  // audio fingerprint so broadly similar spectra still get distinct compositions.
  const ribbonStep=Math.max(1,Math.floor(surface.bins/8));
  for(let b=0;b<surface.bins;b+=ribbonStep){
    const t=b/Math.max(1,surface.bins-1),e=bandEnergy[b];if(e<.13)continue;
    const c=coverColorAt(t,phase);const slope=(rand()-.5)*H*.34;
    const y=H*(.9-.76*t)+(rand()-.5)*H*.08;
    const g=ctx.createLinearGradient(0,y-slope,W,y+slope);g.addColorStop(0,coverRgbCss(c,0));g.addColorStop(.48,coverRgbCss(c,.08+.24*e));g.addColorStop(1,coverRgbCss(c,0));ctx.fillStyle=g;ctx.fillRect(-W*.12,y-H*.24,W*1.24,H*.48);
  }
  ctx.restore();

  // Add one darker audio-driven pool for depth; energetic/bright tracks make it
  // smaller, sparse/darker tracks make it larger.
  ctx.save();ctx.globalCompositeOperation='multiply';ctx.filter=`blur(${Math.max(8*dpr,W*.055)}px)`;
  const shadowX=W*(.18+.64*rand()),shadowY=H*(.18+.64*rand());
  const shadowR=W*(.30+.20*clamp((desc.crest||1)/4));
  const shadow=ctx.createRadialGradient(shadowX,shadowY,0,shadowX,shadowY,shadowR);shadow.addColorStop(0,'rgba(5,8,18,.10)');shadow.addColorStop(1,'rgba(3,5,12,.48)');ctx.fillStyle=shadow;ctx.fillRect(shadowX-shadowR,shadowY-shadowR,shadowR*2,shadowR*2);ctx.restore();

  ctx.save();ctx.globalCompositeOperation='soft-light';
  const sheenAngle=rand()*Math.PI*2;const sx=W*(.5-.7*Math.cos(sheenAngle)),sy=H*(.5-.7*Math.sin(sheenAngle)),ex=W*(.5+.7*Math.cos(sheenAngle)),ey=H*(.5+.7*Math.sin(sheenAngle));
  const sheen=ctx.createLinearGradient(sx,sy,ex,ey);sheen.addColorStop(0,'rgba(255,255,255,.17)');sheen.addColorStop(.28,'rgba(255,255,255,.02)');sheen.addColorStop(.72,'rgba(0,0,0,.08)');sheen.addColorStop(1,'rgba(0,0,0,.26)');ctx.fillStyle=sheen;ctx.fillRect(0,0,W,H);ctx.restore();
  const vignette=ctx.createRadialGradient(W*.48,H*.43,W*.08,W*.5,H*.5,W*.79);vignette.addColorStop(0,'rgba(0,0,0,0)');vignette.addColorStop(.66,'rgba(0,0,0,.04)');vignette.addColorStop(1,'rgba(0,0,0,.44)');ctx.fillStyle=vignette;ctx.fillRect(0,0,W,H);
  const accent=coverColorAt(clamp((desc.rawCentroid||.5)*.72+highMix*.28),phase);canvas.parentElement?.style.setProperty('--cover-accent',coverRgbCss(accent,.48));
}

async function persistFrequencyCover(url,surface){
  const source=normalizeCoverSource(url);
  if(!source||!surface?.columns)return null;
  if(coverPersistedSources.has(source))return source;
  if(coverPersistInflight.has(source))return coverPersistInflight.get(source);
  const pending=(async()=>{
    try{
      const already=await requestCachedCoverInfo(source);
      if(already?.url){
        try{const img=await loadImageElement(already.url);coverImageCache.set(source,img);}catch(_){}
        coverPersistedSources.add(source);
        return source;
      }
      const exportCanvas=document.createElement('canvas');
      renderFrequencyCover(exportCanvas,surface,{forceWidth:640,forceHeight:640});
      const dataUrl=exportCanvas.toDataURL('image/png');
      const r=await fetch('/api/cover-cache',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({source,image_data:dataUrl})
      });
      const x=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(x.error||'Cover save failed');
      if(x?.url){
        try{const img=await loadImageElement(String(x.url));coverImageCache.set(source,img);}catch(_){}
      }
      coverPersistedSources.add(source);
      return source;
    }catch(_){return null;}
    finally{coverPersistInflight.delete(source);}
  })();
  coverPersistInflight.set(source,pending);
  return pending;
}

function clearLibraryCoverBindings(){state.libraryCoverDisposers.splice(0).forEach(fn=>{try{fn();}catch(_){}});}
function attachFrequencyCover(canvas,url,{lazy=false,bucket='visual'}={}) {
  if(!canvas){return;}
  drawCoverPlaceholder(canvas);
  if(!url)return;
  let disposed=false,started=false,ro=null,io=null,surface=null,cachedImage=null;
  const draw=()=>{
    if(disposed)return;
    if(cachedImage)drawCoverImage(canvas,cachedImage);
    else if(surface)renderFrequencyCover(canvas,surface);
    else drawCoverPlaceholder(canvas);
  };
  const bindResize=()=>{if('ResizeObserver'in window){ro=new ResizeObserver(draw);ro.observe(canvas);}};
  const start=async()=>{
    if(started||disposed)return;started=true;
    try{
      cachedImage=await getCachedCoverImage(url);
      if(disposed)return;
      if(cachedImage){draw();bindResize();return;}
    }catch(_){}
    buildSpectralSurface(url).then(async x=>{
      if(disposed)return;surface=x;draw();bindResize();persistFrequencyCover(url,x).catch(()=>{});
    }).catch(()=>{if(!disposed)drawCoverPlaceholder(canvas);});
  };
  if(lazy&&'IntersectionObserver'in window){io=new IntersectionObserver(entries=>{if(entries.some(e=>e.isIntersecting)){io.disconnect();io=null;start();}},{rootMargin:'180px'});io.observe(canvas);}else start();
  const dispose=()=>{disposed=true;io?.disconnect();ro?.disconnect();};
  if(bucket==='library')state.libraryCoverDisposers.push(dispose);else state.visualDisposers.push(dispose);
}

async function setTransportCoverUrl(url) {
  const token = ++state.transportCoverToken;
  state.transportCoverSurface = null;
  state.transportCoverImage = null;
  drawCoverPlaceholder($('transportCover'));
  if (!url) return;
  try {
    const cachedImage = await getCachedCoverImage(url);
    if (token !== state.transportCoverToken) return;
    if (cachedImage) {
      state.transportCoverImage = cachedImage;
      drawCoverImage($('transportCover'), cachedImage);
      return;
    }
  } catch (_) {}
  try {
    const surface = await buildSpectralSurface(url);
    if (token !== state.transportCoverToken) return;
    state.transportCoverSurface = surface;
    renderFrequencyCover($('transportCover'), surface);
    persistFrequencyCover(url, surface).catch(()=>{});
  } catch (_) {
    if (token === state.transportCoverToken) drawCoverPlaceholder($('transportCover'));
  }
}

function redrawTransportCover() {
  if (state.transportCoverImage) drawCoverImage($('transportCover'), state.transportCoverImage);
  else if (state.transportCoverSurface) renderFrequencyCover($('transportCover'), state.transportCoverSurface);
  else drawCoverPlaceholder($('transportCover'));
}

function restoreProjectTransport() {
  $('transportTitle').textContent = getProjectTitle();
  $('transportMeta').textContent = state.projectTransportMeta || 'Local project';
  if (state.projectCoverUrl) setTransportCoverUrl(state.projectCoverUrl);
  else redrawTransportCover();
}

function buildTimeAxis(id,duration){
  const el=$(id);if(!el)return;el.innerHTML='';const d=Math.max(1,Number(duration)||1);const count=5;
  for(let i=0;i<count;i++){const span=document.createElement('span');span.textContent=formatTime(d*i/(count-1));el.appendChild(span);}
}

async function setAudioSource(name, preserve=true, autoPlay=false){
  const map=audioMap();const next=map[name];const logicalUrl=audioSourceUrl(name);
  if(!next||!logicalUrl)return;
  const switchToken=++state.sourceSwitchToken;
  const leavingLibrary = state.activeSource === 'library' && name !== 'library';
  const prev=map[state.activeSource];
  const previousTime=preserve&&prev&&Number.isFinite(prev.currentTime)?prev.currentTime:0;
  const wasPlaying=prev?!prev.paused:false;
  await Promise.all(Object.values(map).filter(a=>a&&a!==next&&!a.paused).map(a=>smoothPauseAudio(a).catch(()=>{})));
  if(switchToken!==state.sourceSwitchToken)return;
  state.activeSource=name;
  if(leavingLibrary)restoreProjectTransport();
  try { await ensureBufferedPlayback(name); } catch (_) { return; }
  if(switchToken!==state.sourceSwitchToken)return;
  if(preserve&&Number.isFinite(previousTime)&&Number.isFinite(next.duration)&&next.duration>0){
    next.currentTime=Math.min(previousTime,Math.max(0,next.duration-.01));
  }
  document.querySelectorAll('.compare-choice[data-source]').forEach(b=>b.classList.toggle('active',b.dataset.source===name));
  document.querySelectorAll('.workspace-tab').forEach(b=>{if(b.dataset.tab==='original'||b.dataset.tab==='modified')b.classList.toggle('active',b.dataset.tab===name);});
  syncTransportUI();
  if(autoPlay||wasPlaying)smoothPlayAudio(next).catch(()=>{});
}
async function toggleAudio(name){
  const a=audioMap()[name];if(!a||!audioSourceUrl(name))return;
  if(state.activeSource!==name){
    await setAudioSource(name,true,false);
    if(state.activeSource!==name)return;
  }else{
    try { await ensureBufferedPlayback(name); } catch (_) { return; }
  }
  const current=audioMap()[name];
  if(current.paused)smoothPlayAudio(current).catch(()=>{});else smoothPauseAudio(current).catch(()=>{});
}
function syncPlayButtons(){
  const map=audioMap();document.querySelectorAll('[data-play]').forEach(btn=>{const a=map[btn.dataset.play];btn.textContent=a&&!a.paused?'❚❚':'▶';});
  const libraryPlayer=map.library;
  document.querySelectorAll('.library-cover-play').forEach(btn=>{
    const same = Boolean(state.libraryTrackUrl && btn.dataset.libraryUrl === state.libraryTrackUrl);
    btn.textContent = same && libraryPlayer && !libraryPlayer.paused ? '❚❚' : '▶';
    btn.classList.toggle('playing', same && libraryPlayer && !libraryPlayer.paused);
  });
  const active=map[state.activeSource];$('transportPlay').textContent=active&&!active.paused?'❚❚':'▶';
}
function syncTransportUI(){
  const a=audioMap()[state.activeSource];if(!a)return;
  const d=Number.isFinite(a.duration)?a.duration:0;const t=Number.isFinite(a.currentTime)?a.currentTime:0;
  $('transportTime').textContent=`${formatTime(t)} / ${formatTime(d)}`;
  $('transportSeek').value=d?Math.round(clamp(t/d)*1000):0;
  syncPlayButtons();
}
function setupTransport(){
  document.querySelectorAll('[data-play]').forEach(btn=>btn.addEventListener('click',()=>{toggleAudio(btn.dataset.play).catch(()=>{});}));
  document.querySelectorAll('.compare-choice[data-source]').forEach(btn=>btn.addEventListener('click',()=>{setAudioSource(btn.dataset.source,true,false).catch(()=>{});}));
  $('abButton').addEventListener('click',()=>{setAudioSource(state.activeSource==='original'?'modified':'original',true,true).catch(()=>{});});
  $('transportPlay').addEventListener('click',()=>{toggleAudio(state.activeSource).catch(()=>{});});
  $('prevBtn').addEventListener('click',()=>{const a=audioMap()[state.activeSource];if(a)smoothSeekAudio(a,Math.max(0,(Number(a.currentTime)||0)-10)).catch(()=>{});});
  $('nextBtn').addEventListener('click',()=>{const a=audioMap()[state.activeSource];if(a&&Number.isFinite(a.duration))smoothSeekAudio(a,Math.min(a.duration,(Number(a.currentTime)||0)+10)).catch(()=>{});});
  $('transportSeek').addEventListener('input',e=>{
    const a=audioMap()[state.activeSource];
    if(!a||!Number.isFinite(a.duration)||a.duration<=0)return;
    const preview=(Number(e.target.value)/1000)*a.duration;
    $('transportTime').textContent=`${formatTime(preview)} / ${formatTime(a.duration)}`;
  });
  $('transportSeek').addEventListener('change',e=>{
    const a=audioMap()[state.activeSource];
    if(a&&Number.isFinite(a.duration))smoothSeekAudio(a,(Number(e.target.value)/1000)*a.duration).catch(()=>{});
  });
  const volumeInputs=[$('transportVolume'),$('compareVolume')].filter(Boolean);
  state.masterVolume=Number($('transportVolume')?.value||.85);
  volumeInputs.forEach(input=>input.addEventListener('input',e=>{
    const v=clamp(Number(e.target.value));
    state.masterVolume=v;
    Object.values(audioMap()).forEach(a=>{if(a&&!a.__jmFadeRaf)a.volume=v;});
    volumeInputs.forEach(other=>{if(other!==input)other.value=v;});
  }));
  Object.entries(audioMap()).forEach(([name,a])=>{
    if(!a)return;
    a.preload='auto';
    a.volume=state.masterVolume;
    for(const ev of ['play','pause','ended','timeupdate','seeked','loadedmetadata','canplay'])a.addEventListener(ev,()=>{
      if(ev==='play'){
        state.activeSource=name;
        Object.entries(audioMap()).forEach(([other,b])=>{if(other!==name&&b&&!b.paused)smoothPauseAudio(b).catch(()=>{});});
      }
      if(state.activeSource===name)syncTransportUI();
      syncPlayButtons();
    });
  });
}

function renderResults(jobId,out){
  state.outputs=out;
  $('exportBtn').disabled=false;
  $('saveLibraryBtn').disabled=false;
  $('exportBtn').onclick=()=>{if(out.modified_full)window.location.href=downloadUrl(jobId,out.modified_full);};
  const original=mediaUrl(jobId,out.original_full);
  // Prefer the lossless PCM render for in-app playback. The MP3 remains available
  // for export/compatibility, but the transport no longer uses it when a WAV exists.
  const modified=mediaUrl(jobId,out.modified_full_wav||out.modified_full);
  setPlaybackSource('original',original,true);
  setPlaybackSource('modified',modified,true);
  $('protectedVocals').src=mediaUrl(jobId,out.protected_vocals);
  $('originalInstrumental').src=mediaUrl(jobId,out.original_instrumental);
  const vocalRel=out.edited_vocals||out.protected_vocals;const vocalUrl=mediaUrl(jobId,vocalRel);const instUrl=mediaUrl(jobId,out.edited_instrumental);
  setPlaybackSource('vocal',vocalUrl,false);
  setPlaybackSource('instrument',instUrl,false);
  $('vocalStemTitle').textContent=out.edited_vocals?'Vocals · edited':'Vocals · protected';
  $('vocalStemSubtitle').textContent=out.edited_vocals?'Edited vocal layer used in the final mix':'Protected vocal layer used in the final mix';
  if(out.edited_vocals){$('editedVocalsWrap').classList.remove('hidden');$('editedVocals').src=mediaUrl(jobId,out.edited_vocals);}else{$('editedVocalsWrap').classList.add('hidden');}

  const d=Number(out.duration_seconds||0);state.projectDuration=d;
  $('originalMetaDuration').textContent=formatTime(d);$('modifiedMetaDuration').textContent=formatTime(d);$('projectDuration').textContent=formatTime(d);
  state.projectTransportMeta=`${backendLabel(out.backend)} · ${out.steps||''} steps · ${out.cache_hit?'layer cache':'fresh render'}`;
  if(state.activeSource!=='library')$('transportMeta').textContent=state.projectTransportMeta;
  buildTimeAxis('originalTimeAxis',d);buildTimeAxis('modifiedTimeAxis',d);

  clearVisualBindings();
  attachWaveform($('originalWave'),$('originalPlayer'),out.waveforms?.original,'original',$('originalPlayhead'),$('originalPeak'),$('originalMeter'));
  attachWaveform($('modifiedWave'),$('modifiedPlayer'),out.waveforms?.modified,'modified',$('modifiedPlayhead'),$('modifiedPeak'),$('modifiedMeter'));
  const vocalWave=out.edited_vocals?out.waveforms?.edited_vocals:out.waveforms?.protected_vocals;
  attachWaveform($('vocalWave'),$('vocalStemPlayer'),vocalWave,'vocal');
  attachWaveform($('instrumentWave'),$('instrumentStemPlayer'),out.waveforms?.edited_instrumental,'instrument');
  attachSpectrum($('originalSpectrum'),original,'original');
  attachSpectrum($('modifiedSpectrum'),modified,'modified');
  attachSpectrum($('vocalSpectrum'),vocalUrl,'vocal');
  attachSpectrum($('instrumentSpectrum'),instUrl,'instrument');
  state.projectCoverUrl=modified;
  setTransportCoverUrl(modified);

  setAudioSource('original',false,false);setNavActive('home');showMasterPane('original');setTimeout(redrawWorkspaceVisuals,80);
}

async function pollJob(jobId){
  const r=await fetch(`/api/jobs/${encodeURIComponent(jobId)}`,{cache:'no-store'});const x=await r.json();if(!r.ok)throw new Error(x.error||'Job status failed');
  setProgress(x.stage,x.progress,x.message);
  if(x.status==='done'){
    clearInterval(state.poll);state.poll=null;$('processBtn').disabled=false;renderResults(jobId,x.outputs);setTimeout(refreshLibraryCounts,300);
  }else if(x.status==='error'){
    clearInterval(state.poll);state.poll=null;$('processBtn').disabled=false;$('stageText').textContent='Failed';$('messageText').textContent=sanitizeVisibleText(x.error||x.message||'Processing failed');$('messageText').classList.add('error-box');
  }
}

async function processSong(){
  let file=null;
  let referenceId='';
  let sourceLabel='';
  if(state.sourceMode==='reference'){
    if(!state.referenceSource){alert('Choose a track from Reference Library first.');return;}
    referenceId=state.referenceSource.id;
    sourceLabel=state.referenceSource.title||'Reference Library track';
  }else if(state.sourceMode==='mic'){
    if(state.micRecorder?.state==='recording'){alert('Stop the microphone recording first.');return;}
    file=state.recordedFile;
    if(!file){alert('Record a microphone source first.');return;}
    sourceLabel=file.name;
  }else{
    file=$('audioFile').files?.[0];
    if(!file){alert('Choose an audio file first.');return;}
    sourceLabel=file.name;
  }

  $('messageText').classList.remove('error-box');
  $('processBtn').disabled=true;$('saveLibraryBtn').disabled=true;$('exportBtn').disabled=true;
  showEditorView();syncSettingsState(false);setNavActive('home');
  resetOutputVisuals();
  setProgress(referenceId?'Preparing source':'Uploading',1,referenceId?`Loading ${sourceLabel} from Reference Library…`:`Preparing ${sourceLabel}…`);

  const fd=new FormData();
  if(referenceId)fd.append('reference_id',referenceId);else fd.append('audio',file);
  fd.append('source_kind',state.sourceMode==='mic'?'microphone':state.sourceMode);
  if(state.sourceMode==='mic' && state.micBackingFile){
    fd.append('backing_audio',state.micBackingFile);
    fd.append('mic_gain_db',$('micVoiceGain').value);
    fd.append('backing_gain_db',$('micBackingGain').value);
    fd.append('backing_loop',$('micBackingLoop').checked?'true':'false');
  }
  fd.append('project_title',getProjectTitle());
  fd.append('prompt',$('prompt').value);fd.append('vocal_noise',$('vocalNoise').value);fd.append('inst_noise',$('instNoise').value);fd.append('seed',$('seed').value);fd.append('backend',$('backend').value);fd.append('speed_mode',$('speedMode').value);fd.append('edit_vocals',$('editVocals').checked?'true':'false');
  try{
    const r=await fetch('/api/jobs',{method:'POST',body:fd});const x=await r.json();if(!r.ok)throw new Error(x.error||'Source submission failed');state.jobId=x.job_id;await pollJob(state.jobId);state.poll=setInterval(()=>pollJob(state.jobId).catch(err=>{clearInterval(state.poll);state.poll=null;$('processBtn').disabled=false;setProgress('Connection error',100,err.message);}),1400);
  }catch(e){$('processBtn').disabled=false;setProgress('Failed',100,e.message);requestAnimationFrame(()=>drawAllPlaceholders());}
}

const libraryInfo = {
  history: {kicker:'ACTIVITY', title:'History', subtitle:'Every completed JoyMetric render is recorded automatically here.', empty:'Process a track and completed renders will appear here automatically.'},
  projects: {kicker:'LIBRARY', title:'My Projects', subtitle:'Full saved working sets with original, modified output, stems and edit metadata.', empty:'Save a completed render to My Projects to keep the full working set.'},
  audio: {kicker:'LIBRARY', title:'My Audio', subtitle:'A clean local collection of audio renders you chose to keep.', empty:'Save a completed render to My Audio and it will appear here.'},
  favorites: {kicker:'LIBRARY', title:'Favorites', subtitle:'Your pinned JoyMetric renders for quick access.', empty:'Save a render to Favorites to pin it here.'},
  references: {kicker:'TOOLS', title:'Reference Library', subtitle:'Upload songs from your computer or keep JoyMetric renders as reusable source references.', empty:'Upload a song from your computer to start your persistent Reference Library.'}
};

function setLibraryNavActive(collection) {
  document.querySelectorAll('.nav-item').forEach(btn => btn.classList.remove('active'));
  document.querySelectorAll(`[data-library="${collection}"]`).forEach(btn => btn.classList.add('active'));
}
function libraryDate(ts) {
  try { return new Intl.DateTimeFormat(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}).format(new Date(Number(ts||0)*1000)); }
  catch (_) { return ''; }
}
function makeEl(tag,className,text) {
  const el=document.createElement(tag); if(className)el.className=className; if(text!==undefined&&text!==null)el.textContent=text; return el;
}
function preferredLibraryAsset(item) {
  const order=['modified_full','original_full','modified_full_wav','edited_instrumental','edited_vocals','protected_vocals'];
  for(const key of order)if(item.media?.[key])return key;
  return Object.keys(item.media||{})[0]||null;
}
function preferredLibraryPlaybackAsset(item) {
  const order=['modified_full_wav','modified_full','original_full','edited_instrumental','edited_vocals','protected_vocals'];
  for(const key of order)if(item.media?.[key])return key;
  return Object.keys(item.media||{})[0]||null;
}
function libraryAssetLabel(key) {
  if (key === 'original_full') return 'Original';
  if (key === 'modified_full' || key === 'modified_full_wav') return 'Modified';
  if (key === 'edited_instrumental') return 'Instrumental';
  if (key === 'edited_vocals') return 'Vocals';
  return 'Audio';
}

async function playLibraryItem(item, collection, key, url) {
  const player = $('libraryPlayer');
  if (!player || !url) return;
  const same = state.libraryTrackUrl === url && audioSourceUrl('library') === url;
  if (same) {
    await toggleAudio('library');
    return;
  }
  state.libraryTrackUrl = url;
  state.libraryTrackId = item.id || null;
  state.libraryTrackCollection = collection;
  state.activeSource = 'library';
  $('transportTitle').textContent = item.title || 'Untitled Track';
  $('transportMeta').textContent = `${libraryInfo[collection]?.title || 'Library'} · ${libraryAssetLabel(key)} · buffered playback`;
  setTransportCoverUrl(url);
  setPlaybackSource('library', url, false);
  syncTransportUI();
  await setAudioSource('library', false, true);
}

function stopLibraryPlaybackIf(collection, itemId) {
  if (state.libraryTrackCollection !== collection || state.libraryTrackId !== itemId) return;
  const player = $('libraryPlayer');
  smoothPauseAudio(player, true).catch(()=>{});
  setPlaybackSource('library', '', false);
  state.libraryTrackUrl = null;
  state.libraryTrackId = null;
  state.libraryTrackCollection = null;
  state.activeSource = 'original';
  restoreProjectTransport();
  syncTransportUI();
}
function renderLibraryItem(item,collection) {
  const card=makeEl('article','library-card');
  const top=makeEl('div','library-card-top'); const titleWrap=makeEl('div','library-card-title');
  const durationLabel=Number(item.duration||0)>0?` · ${formatTime(Number(item.duration))}`:'';
  titleWrap.append(makeEl('strong','',item.title||'Untitled Track'),makeEl('span','',`${libraryDate(item.created_at)}${durationLabel}`));
  const identity=makeEl('div','library-card-identity');
  const coverShell=makeEl('div','frequency-cover-shell library-cover-shell');
  const cover=document.createElement('canvas');cover.className='frequency-cover';cover.setAttribute('aria-label','Audio color cover');
  const coverKey=preferredLibraryAsset(item);const coverUrl=coverKey?item.media?.[coverKey]:null;
  const playbackKey=preferredLibraryPlaybackAsset(item);const playbackUrl=playbackKey?item.media?.[playbackKey]:null;
  coverShell.append(cover,makeEl('span','frequency-cover-mark','JM'));
  if(playbackUrl){
    const play=makeEl('button','library-cover-play','▶');play.type='button';play.title='Play in bottom player';play.setAttribute('aria-label',`Play ${item.title||'track'} in bottom player`);play.dataset.libraryUrl=playbackUrl;
    play.addEventListener('click',e=>{e.stopPropagation();playLibraryItem(item,collection,playbackKey,playbackUrl).catch(()=>{});});
    coverShell.appendChild(play);
  }
  identity.append(coverShell,titleWrap);
  top.append(identity,makeEl('span','library-card-badge',collection==='history'?'RENDER':collection.toUpperCase())); card.appendChild(top);
  attachFrequencyCover(cover,coverUrl,{lazy:true,bucket:'library'});
  const meta=makeEl('div','library-card-meta');
  if(item.backend)meta.appendChild(makeEl('span','',backendLabel(item.backend)));
  if(item.extra?.steps)meta.appendChild(makeEl('span','',`${item.extra.steps} steps`));
  if(item.asset_kind)meta.appendChild(makeEl('span','',item.asset_kind));
  card.appendChild(meta);
  card.appendChild(makeEl('p','library-card-prompt',item.prompt||'No prompt saved for this item.'));
  const actions=makeEl('div','library-card-actions'); const primary=preferredLibraryAsset(item);
  if(primary&&item.downloads?.[primary]){const dl=makeEl('a','','Download');dl.href=item.downloads[primary];actions.appendChild(dl);}
  const del=makeEl('button','danger','Delete');del.type='button';
  del.addEventListener('click',async()=>{
    if(!confirm(`Delete “${item.title||'this item'}” from ${libraryInfo[collection]?.title||collection}?`))return;
    del.disabled=true;
    try{const r=await fetch(`/api/library/${encodeURIComponent(collection)}/${encodeURIComponent(item.id)}`,{method:'DELETE'});const x=await r.json();if(!r.ok)throw new Error(x.error||'Delete failed');stopLibraryPlaybackIf(collection,item.id);card.remove();if(!$('libraryGrid').children.length)showLibraryEmpty(collection,true);refreshLibraryCounts();if(collection==='references')refreshReferenceSourceOptions();}
    catch(e){alert(e.message);del.disabled=false;}
  });
  actions.appendChild(del);card.appendChild(actions);return card;
}
function showLibraryEmpty(collection,show) {
  const info=libraryInfo[collection]||libraryInfo.audio;
  $('libraryEmpty').classList.toggle('hidden',!show);$('libraryEmptyTitle').textContent='Nothing here yet';$('libraryEmptyText').textContent=info.empty;
}
async function openLibrary(collection) {
  const info=libraryInfo[collection];if(!info)return;
  clearLibraryCoverBindings();
  state.activeLibrary=collection;$('realtimeView')?.classList.add('hidden');$('agenticView')?.classList.add('hidden');$('workArea').classList.add('hidden');$('libraryView').classList.remove('hidden');$('libraryView').dataset.collection=collection;setLibraryNavActive(collection);
  $('referenceUploadBtn').classList.toggle('hidden',collection!=='references');
  $('libraryKicker').textContent=info.kicker;$('libraryTitle').textContent=info.title;$('librarySubtitle').textContent=info.subtitle;
  const grid=$('libraryGrid');grid.innerHTML='';grid.appendChild(makeEl('div','library-loading','Loading local library…'));showLibraryEmpty(collection,false);
  try{const r=await fetch(`/api/library/${encodeURIComponent(collection)}`,{cache:'no-store'});const x=await r.json();if(!r.ok)throw new Error(x.error||'Library load failed');grid.innerHTML='';(x.items||[]).forEach(item=>grid.appendChild(renderLibraryItem(item,collection)));syncPlayButtons();showLibraryEmpty(collection,!(x.items||[]).length);}
  catch(e){grid.innerHTML='';grid.appendChild(makeEl('div','library-loading error-box',sanitizeVisibleText(e.message)));}
}
async function refreshLibraryCounts() {
  const collections=['history','projects','audio','favorites','references'];
  await Promise.all(collections.map(async collection=>{
    try{
      const r=await fetch(`/api/library/${encodeURIComponent(collection)}`,{cache:'no-store'});
      const x=await r.json();
      if(!r.ok)return;
      document.querySelectorAll(`[data-count-for="${collection}"]`).forEach(el=>{el.textContent=String((x.items||[]).length);});
    }catch(_){ }
  }));
}
async function uploadReferenceFile(file) {
  if(!file)return;
  const fd=new FormData();fd.append('audio',file);
  const btn=$('referenceUploadBtn');const old=btn.textContent;btn.disabled=true;btn.textContent='Uploading…';
  try{
    const r=await fetch('/api/library/references/upload',{method:'POST',body:fd});const x=await r.json();if(!r.ok)throw new Error(x.error||'Reference upload failed');
    await refreshReferenceSourceOptions(x.item?.id||null);
    await refreshLibraryCounts();
    if(state.activeLibrary==='references')await openLibrary('references');
  }catch(e){alert(sanitizeVisibleText(e.message));}
  finally{btn.disabled=false;btn.textContent=old;$('referenceUploadInput').value='';}
}
function setupReferenceLibraryUpload() {
  $('referenceUploadBtn').addEventListener('click',()=>$('referenceUploadInput').click());
  $('referenceUploadInput').addEventListener('change',()=>uploadReferenceFile($('referenceUploadInput').files?.[0]));
}

function setupLibraryNavigation() {
  document.querySelectorAll('[data-library]').forEach(btn=>btn.addEventListener('click',()=>openLibrary(btn.dataset.library)));
  $('topHistoryBtn').addEventListener('click',()=>openLibrary('history'));
  $('libraryBackBtn').addEventListener('click',()=>{showEditorView();syncSettingsState(false);setNavActive('home');});
  $('libraryRefreshBtn').addEventListener('click',()=>{if(state.activeLibrary)openLibrary(state.activeLibrary);});
}

function updateSaveSelectionUI() {
  const checks = [...document.querySelectorAll('.save-destination-check')];
  const count = checks.filter(c => c.checked).length;
  const assetCount = document.querySelectorAll('input[name="saveAssetKind"]:checked').length;
  checks.forEach(c => c.closest('.save-destination')?.classList.toggle('selected', c.checked));
  $('saveSelectedCount').textContent = String(count);
  $('saveSelectedBtn').disabled = count === 0 || assetCount === 0;
  if (!$('saveModal').classList.contains('saving')) {
    $('saveModalStatus').className = 'save-modal-status';
    if (!count) $('saveModalStatus').textContent = 'Select one or more destinations.';
    else if (!assetCount) $('saveModalStatus').textContent = 'Select Original, Modified, or both.';
    else $('saveModalStatus').textContent = `${count} destination${count === 1 ? '' : 's'} · ${assetCount} audio version${assetCount === 1 ? '' : 's'} selected.`;
  }
}
function openSaveModal() {
  if(!state.jobId||!state.outputs)return;
  document.querySelectorAll('.save-destination-check').forEach(c => { c.checked = false; });
  $('saveModal').classList.remove('saving');
  $('saveModal').classList.remove('hidden');
  updateSaveSelectionUI();
}
function closeSaveModal() {
  if ($('saveModal').classList.contains('saving')) return;
  $('saveModal').classList.add('hidden');
}
async function saveToCollection(collection, assetKind) {
  const r=await fetch('/api/library/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({collection,job_id:state.jobId,asset_kind:assetKind,project_title:getProjectTitle()})});
  const x=await r.json();
  if(!r.ok)throw new Error(x.error||`Save to ${libraryInfo[collection]?.title||collection} failed`);
  return x;
}
async function saveSelectedDestinations() {
  if(!state.jobId||!state.outputs)return;
  const selected=[...document.querySelectorAll('.save-destination-check:checked')].map(c=>c.value);
  const assetKinds=[...document.querySelectorAll('input[name="saveAssetKind"]:checked')].map(c=>c.value);
  if(!selected.length||!assetKinds.length)return;
  const operations=[];
  selected.forEach(collection=>{
    if(collection==='projects') operations.push({collection,assetKind:'modified',label:libraryInfo[collection]?.title||collection});
    else assetKinds.forEach(assetKind=>operations.push({collection,assetKind,label:`${libraryInfo[collection]?.title||collection} · ${assetKind==='original'?'Original':'Modified'}`}));
  });
  const status=$('saveModalStatus');
  $('saveModal').classList.add('saving');
  document.querySelectorAll('.save-destination-check').forEach(c=>c.disabled=true);
  document.querySelectorAll('input[name="saveAssetKind"]').forEach(c=>c.disabled=true);
  $('saveSelectedBtn').disabled=true;
  let saved=[]; const failed=[];
  for(let i=0;i<operations.length;i++){
    const op=operations[i];
    status.className='save-modal-status';
    status.textContent=`Saving ${i+1}/${operations.length} · ${op.label}…`;
    try{await saveToCollection(op.collection,op.assetKind);saved.push(op.label);}
    catch(e){failed.push(`${op.label}: ${sanitizeVisibleText(e.message)}`);}
  }
  await refreshLibraryCounts();
  if(selected.includes('references'))await refreshReferenceSourceOptions();
  $('saveModal').classList.remove('saving');
  document.querySelectorAll('.save-destination-check').forEach(c=>c.disabled=false);
  document.querySelectorAll('input[name="saveAssetKind"]').forEach(c=>c.disabled=false);
  if(failed.length){
    updateSaveSelectionUI();
    status.className='save-modal-status bad';
    status.textContent=`Saved ${saved.length}/${operations.length}. ${failed.join(' · ')}`;
  } else {
    status.className='save-modal-status good';
    status.textContent=`Saved ${saved.length} item${saved.length===1?'':'s'} successfully.`;
    setTimeout(()=>{ $('saveModal').classList.add('hidden'); },900);
  }
}
function setupSaveLibrary() {
  $('saveLibraryBtn').addEventListener('click',openSaveModal);
  $('saveModalClose').addEventListener('click',closeSaveModal);
  $('saveModal').addEventListener('click',e=>{if(e.target===$('saveModal'))closeSaveModal();});
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('saveModal').classList.contains('hidden'))closeSaveModal();});
  document.querySelectorAll('.save-destination-check').forEach(c=>c.addEventListener('change',updateSaveSelectionUI));
  document.querySelectorAll('input[name="saveAssetKind"]').forEach(c=>c.addEventListener('change',updateSaveSelectionUI));
  $('saveSelectedBtn').addEventListener('click',saveSelectedDestinations);
}
function liveSpectrumRgbAt(t){
  const u=clamp(Number(t)||0);
  const count=Math.max(2,coverFrequencyPalette.length-1);
  const x=u*(count-1);
  const i=Math.min(count-2,Math.max(0,Math.floor(x)));
  return mixRgb(coverFrequencyPalette[i],coverFrequencyPalette[i+1],x-i);
}
function rgbHue(rgb){
  const r=rgb[0]/255,g=rgb[1]/255,b=rgb[2]/255;
  const max=Math.max(r,g,b),min=Math.min(r,g,b),d=max-min;
  if(d<1e-7)return 210;
  let h=max===r?((g-b)/d)%6:max===g?((b-r)/d)+2:((r-g)/d)+4;
  h*=60;if(h<0)h+=360;return h;
}
function liveMixNumber(a,b,t){return a+(b-a)*t;}
function liveSmoothHue(a,b,t){
  let delta=((b-a+540)%360)-180;
  let out=a+delta*t;
  out=((out%360)+360)%360;
  return out;
}
function liveSmoothRgb(current,target,k){
  for(let i=0;i<3;i++)current[i]=liveMixNumber(current[i],target[i],k);
}
function liveTemporalAccent(surface,progress,secondsOffset,frequencyBias=0){
  if(!surface?.columns||!surface?.bins)return [100,140,210];
  const duration=Math.max(.25,Number(surface.duration)||1);
  const shifted=clamp(Number(progress||0)+(Number(secondsOffset||0)/duration));
  const pos=shifted*Math.max(0,surface.columns-1);
  const c0=Math.floor(pos),c1=Math.min(surface.columns-1,c0+1),f=pos-c0;
  let total=0,weighted=0,low=0,high=0,peak=0;
  for(let b=0;b<surface.bins;b++){
    const v0=surface.values[c0*surface.bins+b]||0,v1=surface.values[c1*surface.bins+b]||0;
    const v=Math.max(0,v0+(v1-v0)*f),w=Math.pow(v,1.68)+.00011,t=b/Math.max(1,surface.bins-1);
    total+=w;weighted+=w*t;peak=Math.max(peak,v);
    if(t<.36)low+=w;else if(t>.66)high+=w;
  }
  const centroid=total>1e-8?weighted/total:.5;
  const tilt=(high-low)/Math.max(total,1e-8);
  const posColor=clamp(centroid*.88+tilt*.10+frequencyBias);
  let color=liveSpectrumRgbAt(posColor);
  color=mixRgb(color,[255,255,255],.025+.055*clamp(peak));
  return color;
}
function liveRgba(rgb,a=1){return `rgba(${Math.round(rgb[0])},${Math.round(rgb[1])},${Math.round(rgb[2])},${a})`;}
function drawSpectrumWaterField(now,audio){
  const canvas=$('spectrumWaterCanvas');
  if(!canvas||activeColorTheme()!=='live')return;
  const playing=!!(audio&&!audio.paused&&!audio.ended);
  const c=liveThemeState.current;
  const dt=liveThemeState.lastWaterDraw?Math.min(.12,(now-liveThemeState.lastWaterDraw)/1000):.04;
  if(liveThemeState.lastWaterDraw&&now-liveThemeState.lastWaterDraw<38)return;
  liveThemeState.lastWaterDraw=now;
  // The field nearly stops when playback stops, like water retaining momentum.
  liveThemeState.waterTime+=dt*(playing?(.72+c.energy*.68):.08);
  const cssW=Math.max(1,canvas.clientWidth||window.innerWidth),cssH=Math.max(1,canvas.clientHeight||window.innerHeight);
  const scale=Math.min(.42,720/cssW,460/cssH);
  const W=Math.max(300,Math.round(cssW*scale)),H=Math.max(210,Math.round(cssH*scale));
  if(canvas.width!==W||canvas.height!==H){canvas.width=W;canvas.height=H;}
  const ctx=canvas.getContext('2d',{alpha:false});
  const t=liveThemeState.waterTime;
  const base=ctx.createLinearGradient(0,0,W,H);
  base.addColorStop(0,liveRgba(c.bg,1));base.addColorStop(.5,liveRgba(c.bg2,1));base.addColorStop(1,liveRgba(c.bg3,1));
  ctx.globalCompositeOperation='source-over';ctx.fillStyle=base;ctx.fillRect(0,0,W,H);

  const colors=[c.local1,c.accent,c.local2,c.accent3,c.local3,c.accent2,c.local1,c.accent3];
  const specs=[
    [.10,.18,.17,.12,.54,1.55,.76,0.00],
    [.72,.12,.12,.15,.46,1.32,.90,1.15],
    [.88,.56,.15,.11,.52,1.62,.72,2.45],
    [.42,.84,.13,.16,.58,1.48,.78,3.35],
    [.20,.62,.18,.13,.43,1.72,.68,4.60],
    [.58,.46,.11,.18,.40,1.42,.92,5.55],
    [.94,.90,.10,.13,.45,1.66,.70,6.35],
    [.44,.18,.16,.10,.36,1.30,.88,7.10],
  ];
  ctx.globalCompositeOperation='screen';
  specs.forEach((s,i)=>{
    const [bx,by,ax,ay,rr,stretch,opacity,phase]=s;
    const speed=.23+i*.026;
    const x=W*(bx+ax*Math.sin(t*speed+phase)+.035*Math.sin(t*(speed*1.73)+phase*.43));
    const y=H*(by+ay*Math.cos(t*(speed*.86)+phase*.77)+.03*Math.sin(t*(speed*1.31)+phase));
    const r=Math.max(W,H)*rr*(.72+.20*Math.sin(t*.19+phase));
    ctx.save();ctx.translate(x,y);ctx.rotate(Math.sin(t*.12+phase)*.78);ctx.scale(stretch,1/stretch*.95);
    const g=ctx.createRadialGradient(0,0,0,0,0,r);
    const a=(.16+.23*c.energy)*opacity;
    g.addColorStop(0,liveRgba(colors[i],a));g.addColorStop(.34,liveRgba(colors[i],a*.70));g.addColorStop(.72,liveRgba(colors[i],a*.23));g.addColorStop(1,liveRgba(colors[i],0));
    ctx.fillStyle=g;ctx.fillRect(-r,-r,r*2,r*2);ctx.restore();
  });

  // Thin interference bands give the mesh a slow current rather than just drifting blobs.
  ctx.globalCompositeOperation='soft-light';ctx.save();ctx.translate(W*.5,H*.5);ctx.rotate(Math.sin(t*.085)*.48);
  const sweep=ctx.createLinearGradient(-W,0,W,0);
  sweep.addColorStop(0,liveRgba(c.local2,0));sweep.addColorStop(.28,liveRgba(c.local2,.10+.08*c.energy));sweep.addColorStop(.48,liveRgba(c.local1,.03));sweep.addColorStop(.70,liveRgba(c.local3,.10+.07*c.energy));sweep.addColorStop(1,liveRgba(c.local3,0));
  ctx.fillStyle=sweep;ctx.fillRect(-W*1.2,-H*.42,W*2.4,H*.84);ctx.restore();
}
function liveFrameTarget(surface,progress){
  if(!surface?.columns||!surface?.bins)return null;
  const cols=surface.columns,bins=surface.bins;
  const pos=clamp(progress)*Math.max(0,cols-1);
  const c0=Math.floor(pos),c1=Math.min(cols-1,c0+1),f=pos-c0;
  let total=0,centroid=0,low=0,mid=0,high=0,variance=0,peak=0;
  const values=new Float32Array(bins);
  for(let b=0;b<bins;b++){
    const v0=surface.values[c0*bins+b]||0,v1=surface.values[c1*bins+b]||0;
    const v=Math.max(0,v0+(v1-v0)*f);values[b]=v;
    const w=Math.pow(v,1.72)+.00012;const t=b/Math.max(1,bins-1);
    total+=w;centroid+=w*t;peak=Math.max(peak,v);
    if(t<.34)low+=w;else if(t<.69)mid+=w;else high+=w;
  }
  centroid=total>0?centroid/total:.5;
  for(let b=0;b<bins;b++){
    const v=values[b],w=Math.pow(v,1.72)+.00012,t=b/Math.max(1,bins-1);
    variance+=w*(t-centroid)*(t-centroid);
  }
  const spread=Math.sqrt(variance/Math.max(total,1e-8));
  const bandTotal=Math.max(low+mid+high,1e-8),lowMix=low/bandTotal,highMix=high/bandTotal;
  const energy=clamp((total/bins)*2.35*.72+peak*.28);
  const primaryPos=clamp(centroid*.92 + highMix*.07 - lowMix*.03);
  const secondaryPos=clamp(primaryPos + .16 + spread*.30);
  const tertiaryPos=clamp(primaryPos - .18 - spread*.22);
  let accent=liveSpectrumRgbAt(primaryPos);
  let accent2=liveSpectrumRgbAt(secondaryPos);
  let accent3=liveSpectrumRgbAt(tertiaryPos);
  accent=mixRgb(accent,[255,255,255],.035+.07*energy);
  accent2=mixRgb(accent2,[255,255,255],.02+.045*energy);
  accent3=mixRgb(accent3,[255,255,255],.015+.035*energy);
  // The backdrop follows the music too. Keep a dark luminance floor for text
  // readability, but let three separately-smoothed spectral colors tint the full
  // application background strongly enough to be visible at a glance.
  const bg=mixRgb([5,8,12],accent,.22+.10*energy);
  const bg2=mixRgb([7,8,13],accent2,.19+.09*energy);
  const bg3=mixRgb([6,10,12],accent3,.18+.085*energy);
  const panel=mixRgb([11,15,21],accent2,.135+.055*energy);
  const panel2=mixRgb([15,19,27],accent3,.135+.06*energy);
  const border=mixRgb([50,58,72],mixRgb(accent,accent2,.5),.30+.10*energy);
  const glow=mixRgb(accent,accent2,.28+.30*highMix);
  // Nearby moments feed different regions of the animated water field, so the
  // background has local color variation rather than one global tint.
  const local1=liveTemporalAccent(surface,progress,-3.4,-.035);
  const local2=liveTemporalAccent(surface,progress,3.1,.025);
  const local3=liveTemporalAccent(surface,progress,7.2,-.012);
  return {accent,accent2,accent3,local1,local2,local3,bg,bg2,bg3,panel,panel2,border,glow,hue:rgbHue(accent),energy};
}
function applyLiveThemeVariables(){
  const root=document.documentElement,c=liveThemeState.current;
  const rgb=(x)=>`${Math.round(x[0])},${Math.round(x[1])},${Math.round(x[2])}`;
  root.style.setProperty('--live-bg',`rgb(${rgb(c.bg)})`);
  root.style.setProperty('--live-bg-2',`rgb(${rgb(c.bg2)})`);
  root.style.setProperty('--live-bg-3',`rgb(${rgb(c.bg3)})`);
  root.style.setProperty('--live-panel',`rgb(${rgb(c.panel)})`);
  root.style.setProperty('--live-panel-2',`rgb(${rgb(c.panel2)})`);
  root.style.setProperty('--live-border',`rgb(${rgb(c.border)})`);
  root.style.setProperty('--live-accent',`rgb(${rgb(c.accent)})`);
  root.style.setProperty('--live-accent-2',`rgb(${rgb(c.accent2)})`);
  root.style.setProperty('--live-accent-3',`rgb(${rgb(c.accent3)})`);
  root.style.setProperty('--accent',`rgb(${rgb(c.accent)})`);
  root.style.setProperty('--accent-2',`rgb(${rgb(c.accent2)})`);
  root.style.setProperty('--accent-3',`rgb(${rgb(c.accent3)})`);   /* v31.30.29 canonical accent */
  root.style.setProperty('--live-local-1',`rgb(${rgb(c.local1)})`);
  root.style.setProperty('--live-local-2',`rgb(${rgb(c.local2)})`);
  root.style.setProperty('--live-local-3',`rgb(${rgb(c.local3)})`);
  root.style.setProperty('--live-glow',`rgba(${rgb(c.glow)},${(.14+.18*c.energy).toFixed(3)})`);
  root.style.setProperty('--live-accent-soft',`rgba(${rgb(c.accent)},${(.09+.08*c.energy).toFixed(3)})`);
  root.style.setProperty('--live-accent2-soft',`rgba(${rgb(c.accent2)},${(.08+.07*c.energy).toFixed(3)})`);
  root.style.setProperty('--live-wave-hue',`${(c.hue-211).toFixed(2)}deg`);
  root.style.setProperty('--live-energy',c.energy.toFixed(4));
  const swatch=$('colorButtonSwatch');
  if(swatch&&activeColorTheme()==='live')swatch.style.background=`linear-gradient(135deg,rgb(${rgb(c.accent3)}) 0 32%,rgb(${rgb(c.accent)}) 33% 66%,rgb(${rgb(c.accent2)}) 67%)`;
  const meta=$('themeColorMeta');if(meta&&activeColorTheme()==='live')meta.setAttribute('content',`rgb(${rgb(c.bg)})`);
}
function clearLiveThemeLoop(){
  if(liveThemeState.raf)cancelAnimationFrame(liveThemeState.raf);
  liveThemeState.raf=0;liveThemeState.lastTick=0;liveThemeState.lastWaterDraw=0;
}
function prepareLiveSurface(url){
  const logical=String(url||'');
  if(!logical)return;
  if(liveThemeState.url===logical&&(liveThemeState.surface||liveThemeState.loading))return;
  liveThemeState.url=logical;liveThemeState.surface=null;liveThemeState.loading=true;
  const token=++liveThemeState.loadToken;
  const start=()=>buildSpectralSurface(logical).then(surface=>{
    if(token!==liveThemeState.loadToken||liveThemeState.url!==logical)return;
    liveThemeState.surface=surface;liveThemeState.loading=false;
  }).catch(()=>{if(token===liveThemeState.loadToken)liveThemeState.loading=false;});
  if('requestIdleCallback'in window)requestIdleCallback(start,{timeout:550});else setTimeout(start,70);
}
function liveThemeTick(now){
  if(activeColorTheme()!=='live'){clearLiveThemeLoop();return;}
  liveThemeState.raf=requestAnimationFrame(liveThemeTick);
  if(liveThemeState.lastTick&&now-liveThemeState.lastTick<(document.body.classList.contains('perf')?100:28))return;
  const dt=liveThemeState.lastTick?Math.min(.12,(now-liveThemeState.lastTick)/1000):.033;
  liveThemeState.lastTick=now;
  const audio=audioMap()[state.activeSource];const url=audioSourceUrl(state.activeSource);
  if(url&&(liveThemeState.url!==url||(!liveThemeState.surface&&!liveThemeState.loading)))prepareLiveSurface(url);
  let target=null;
  if(audio&&url&&liveThemeState.surface&&Number.isFinite(audio.duration)&&audio.duration>0){
    const progress=clamp((Number(audio.currentTime)||0)/audio.duration);
    target=liveFrameTarget(liveThemeState.surface,progress);
  }
  if(!target){
    target={accent:[75,155,247],accent2:[180,83,244],accent3:[42,180,190],local1:[52,180,160],local2:[210,83,170],local3:[219,151,51],bg:[13,23,36],bg2:[27,17,43],bg3:[11,34,36],panel:[17,22,31],panel2:[20,27,36],border:[48,58,73],glow:[91,134,239],hue:214,energy:.38};
  }
  // Slow exponential response makes color motion liquid rather than frame-stepped.
  const tau=(audio&&!audio.paused)?0.62:1.05;
  const k=1-Math.exp(-dt/tau),c=liveThemeState.current;
  for(const key of ['accent','accent2','accent3','bg','bg2','bg3','panel','panel2','border','glow'])liveSmoothRgb(c[key],target[key],k);
  // Local water colors are deliberately a little slower than UI accents; this
  // creates soft currents and avoids the impression of synchronized color blocks.
  const localK=1-Math.exp(-dt/0.82);
  for(const key of ['local1','local2','local3'])liveSmoothRgb(c[key],target[key],localK);
  c.hue=liveSmoothHue(c.hue,target.hue,k*.82);c.energy=liveMixNumber(c.energy,target.energy,k);
  liveThemeState.initialized=true;applyLiveThemeVariables();drawSpectrumWaterField(now,audio);
  // Canvas filters follow the live CSS variables directly; normal audio timeupdate events redraw wave data.
}
function startLiveThemeLoop(){
  clearLiveThemeLoop();applyLiveThemeVariables();liveThemeState.raf=requestAnimationFrame(liveThemeTick);
}

function hslToRgb(h,s,l){/* v31.30.29 canonical accent */
  h=(((h%360)+360)%360)/360;
  const q=l<.5?l*(1+s):l+s-l*s,p=2*l-q;
  const f=(x)=>{x=(x+1)%1;if(x<1/6)return p+(q-p)*6*x;if(x<.5)return q;if(x<2/3)return p+(q-p)*(2/3-x)*6;return p;};
  return [Math.round(f(h+1/3)*255),Math.round(f(h)*255),Math.round(f(h-1/3)*255)];
}
const agenticPulse={raf:0,last:0,t:0};
function agenticAccentBaseHue(){
  try{const v=getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
    let m=v.match(/(\d+)[ ,]+(\d+)[ ,]+(\d+)/);
    if(m)return rgbHue([+m[1],+m[2],+m[3]]);
    const x=v.replace('#','');if(x.length>=6)return rgbHue([parseInt(x.slice(0,2),16),parseInt(x.slice(2,4),16),parseInt(x.slice(4,6),16)]);
  }catch(_){}return 260;
}
function agenticColorTick(now){
  const view=$('agenticView');
  if(!view||view.classList.contains('hidden')){stopAgenticPulse();return;}
  agenticPulse.raf=requestAnimationFrame(agenticColorTick);
  const gap=document.body.classList.contains('perf')?95:40;
  if(agenticPulse.last&&now-agenticPulse.last<gap)return;
  const dt=agenticPulse.last?Math.min(.12,(now-agenticPulse.last)/1000):.033;agenticPulse.last=now;agenticPulse.t+=dt;
  const t=agenticPulse.t,live=activeColorTheme()==='live';
  const base=agenticAccentBaseHue(),span=live?155:62,speed=live?.36:.26,sat=live?.95:.90,lig=.61;   /* v31.30.30 bolder accent */
  const h1=base+span*Math.sin(t*speed);
  const a1=hslToRgb(h1,sat,lig);
  const a2=hslToRgb(h1+46+8*Math.sin(t*speed*1.7),sat,lig+.05);
  const a3=hslToRgb(h1-42-6*Math.cos(t*speed*1.3),sat*.96,lig-.04);
  view.style.setProperty('--accent',liveRgba(a1,1));
  view.style.setProperty('--accent-2',liveRgba(a2,1));
  view.style.setProperty('--accent-3',liveRgba(a3,1));
}
function startAgenticPulse(){stopAgenticPulse();agenticPulse.last=0;agenticPulse.raf=requestAnimationFrame(agenticColorTick);}
function stopAgenticPulse(){if(agenticPulse.raf)cancelAnimationFrame(agenticPulse.raf);agenticPulse.raf=0;const view=$('agenticView');if(view){['--accent','--accent-2','--accent-3'].forEach(p=>view.style.removeProperty(p));}}
function applyColorTheme(theme,{persist=true,closeMenu=true}={}){
  let next=String(theme||'original');
  if(next==='graphite')next='original';
  if(!['original','sand','night','metal','green','live','crimson','teal','royal','sunset','arctic','neon','emerald','amber','slate','vapor'].includes(next))next='original';
  document.documentElement.dataset.theme=next;
  if(next!=='live'){['--accent','--accent-2','--accent-3'].forEach(p=>document.documentElement.style.removeProperty(p));}   /* v31.30.29 canonical accent: drop live's inline accent */
  if(persist){try{localStorage.setItem('joymetric-color-theme',next);}catch(_){}}
  const menu=$('colorThemeMenu');
  document.querySelectorAll('[data-theme-choice]').forEach(btn=>btn.classList.toggle('active',btn.dataset.themeChoice===next));
  const swatch=$('colorButtonSwatch');
  if(swatch){
    const backgrounds={
      original:'linear-gradient(135deg,#d01418 0 30%,#469cf8 31% 63%,#b454ff 64% 100%)',
      sand:'linear-gradient(135deg,#e4d0a5 0 43%,#c49543 44% 68%,#2b2015 69%)',
      night:'linear-gradient(135deg,#16110d 0 44%,#b37d30 45% 68%,#5b3e22 69%)',
      metal:'linear-gradient(135deg,#111b25 0 38%,#7fa8c5 39% 68%,#d4dde4 69%)',
      green:'linear-gradient(135deg,#17231c 0 39%,#6f8f70 40% 70%,#c1b66f 71%)',
      crimson:'linear-gradient(135deg,#150c0e 0 40%,#ff3b52 41% 70%,#ff7a5c 71%)',
      teal:'linear-gradient(135deg,#0b1c1b 0 40%,#22e0c8 41% 70%,#5cf0e0 71%)',
      royal:'linear-gradient(135deg,#140d24 0 40%,#a45cff 41% 70%,#c98bff 71%)',
      sunset:'linear-gradient(135deg,#1e120c 0 40%,#ff7a3c 41% 70%,#ffb14f 71%)',
      arctic:'linear-gradient(135deg,#111a24 0 40%,#5cc8ff 41% 70%,#9ae0ff 71%)',
      neon:'linear-gradient(135deg,#170c15 0 40%,#ff4fd0 41% 70%,#ff7ae0 71%)',
      emerald:'linear-gradient(135deg,#0c1e18 0 40%,#2fe08a 41% 70%,#7af0b0 71%)',
      amber:'linear-gradient(135deg,#1a140b 0 40%,#ffb02e 41% 70%,#ffd07a 71%)',
      slate:'linear-gradient(135deg,#141922 0 40%,#7f9bc0 41% 70%,#a9c0d8 71%)',
      vapor:'linear-gradient(135deg,#130d22 0 40%,#ff6ec7 41% 70%,#63e6ff 71%)',
      live:'linear-gradient(135deg,#e8792c 0 27%,#34b394 28% 53%,#4395db 54% 76%,#c24cba 77%)'
    };
    swatch.style.background=backgrounds[next];
  }
  const metaColors={original:'#07090e',sand:'#21180f',night:'#100d0a',metal:'#0a1118',green:'#101713',crimson:'#060304',teal:'#030b0b',royal:'#06040d',sunset:'#0c0604',arctic:'#06090f',neon:'#070307',emerald:'#030c09',amber:'#090603',slate:'#070a0d',vapor:'#05040c',live:'#0a1018'};
  const meta=$('themeColorMeta');if(meta)meta.setAttribute('content',metaColors[next]||'#07090e');
  if(next==='live')startLiveThemeLoop();else clearLiveThemeLoop();
  if(closeMenu&&menu){menu.classList.add('hidden');$('colorThemeBtn')?.setAttribute('aria-expanded','false');}
  requestAnimationFrame(()=>{
    if(!state.outputs)drawAllPlaceholders();
    else{
      state.waveItems.forEach(item=>item.audio.dispatchEvent(new Event('timeupdate')));
      state.spectrumItems.forEach(item=>drawSpectrum(item.canvas,item.surface,item.tone));
    }
  });
}
function setupColorThemePicker(){
  const btn=$('colorThemeBtn'),menu=$('colorThemeMenu'),wrap=$('colorPickerWrap');
  if(!btn||!menu)return;
  const initial=activeColorTheme();applyColorTheme(initial,{persist:false,closeMenu:false});menu.classList.add('hidden');btn.setAttribute('aria-expanded','false');
  btn.addEventListener('click',e=>{e.stopPropagation();const opening=menu.classList.contains('hidden');menu.classList.toggle('hidden',!opening);btn.setAttribute('aria-expanded',opening?'true':'false');});
  menu.querySelectorAll('[data-theme-choice]').forEach(option=>option.addEventListener('click',e=>{e.stopPropagation();applyColorTheme(option.dataset.themeChoice);}));
  document.addEventListener('click',e=>{if(!wrap.contains(e.target)){menu.classList.add('hidden');btn.setAttribute('aria-expanded','false');}});
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!menu.classList.contains('hidden')){menu.classList.add('hidden');btn.setAttribute('aria-expanded','false');btn.focus();}});
}


// -----------------------------------------------------------------------------
// v30 Realtime: native full-rate dry-anchor audio + Pedalboard C++/ADAA DSP + dual semantic planner/current-moment critic.
// No language model is required. Feature moves are translated directly into the
// The browser is control/UI only. Preferred Spotify source is the managed JoyMetric virtual channel; endpoint loopback and recording inputs remain fallback routes.
// -----------------------------------------------------------------------------
const nativeRtState={running:false,pollTimer:0,pushTimer:0,lastStatus:null,scanning:false,silenceTicks:0,djMode:false,djPrompt:"",effectIntensity:1.0,neuralPolish:true,neuralPolishWet:.08,dsp50Mode:false,dsp50Prompt:"",dsp50Intensity:1.0,controlRevision:0,controlHoldUntil:0,djPromptDirty:false,dsp50PromptDirty:false};
function markRealtimeControlLocal(holdMs=1400){nativeRtState.controlRevision+=1;nativeRtState.controlHoldUntil=Math.max(nativeRtState.controlHoldUntil,performance.now()+holdMs);}
function realtimeControlsLocallyOwned(){return performance.now()<nativeRtState.controlHoldUntil;}
function persistRealtimePrompt(kind,input){
  const value=String(input?.value||'');
  if(kind==='dj'){nativeRtState.djPrompt=value;nativeRtState.djPromptDirty=true;try{localStorage.setItem('joymetricDjPrompt',value);}catch(_){}}
  else{nativeRtState.dsp50Prompt=value;nativeRtState.dsp50PromptDirty=true;try{localStorage.setItem('joymetricDsp50Prompt',value);}catch(_){}}
  markRealtimeControlLocal();
  return value;
}
const rtState={
  ctx:null,inputBus:null,inputAnalyser:null,mediaSource:null,liveSource:null,liveStream:null,activeInput:'none',
  lowCut:null,low:null,mid:null,presence:null,high:null,highCut:null,compressor:null,
  drivePre:null,shaper:null,driveDry:null,driveWet:null,driveJoin:null,
  widthSplitter:null,widthLL:null,widthLR:null,widthRL:null,widthRR:null,widthMerger:null,
  dryGain:null,reverb:null,reverbGain:null,delay:null,delayFeedback:null,delayGain:null,
  processedBus:null,processedGain:null,bypassGain:null,master:null,limiter:null,analyser:null,
  hypnoticPanner:null,hypnoticLfo:null,hypnoticDepth:null,
  raf:0,fileUrl:null,bypassed:false,lastImpulseDecay:1.8,impulseTimer:0,inputLabel:'',
  deviceInputs:[],deviceOutputs:[],devicePermissionGranted:false,vmPlaybackPairs:new Map(),
};
const rtDefaults={
  low_cut_hz:20,bass_db:0,body_db:0,presence_db:0,air_db:0,high_cut_hz:20000,
  compression:0,drive:0,width:1,reverb:0,reverb_decay:1.8,delay:0,delay_ms:285,output_db:0
};
const rtParamIds={
  low_cut_hz:'rtLowCut',bass_db:'rtBass',body_db:'rtBody',presence_db:'rtPresence',air_db:'rtAir',high_cut_hz:'rtHighCut',
  compression:'rtCompression',drive:'rtDrive',width:'rtWidth',reverb:'rtReverb',reverb_decay:'rtReverbDecay',
  delay:'rtDelay',delay_ms:'rtDelayMs',output_db:'rtOutput'
};
const unifiedRealtimeFxMount={deck:null,parent:null,next:null};
function rememberRealtimeFxHome(){
  if(unifiedRealtimeFxMount.deck)return;
  const deck=document.querySelector('.realtime-feature-deck');if(!deck)return;
  unifiedRealtimeFxMount.deck=deck;unifiedRealtimeFxMount.parent=deck.parentNode;unifiedRealtimeFxMount.next=deck.nextSibling;
}
function mountRealtimeFxInAgentic(){
  return;   /* v31.30.28: Realtime FX removed */
  rememberRealtimeFxHome();const mount=$('agenticRealtimeFxMount');
  if(mount&&unifiedRealtimeFxMount.deck)mount.appendChild(unifiedRealtimeFxMount.deck);
}
function restoreRealtimeFxDeck(){
  rememberRealtimeFxHome();const d=unifiedRealtimeFxMount.deck,p=unifiedRealtimeFxMount.parent,n=unifiedRealtimeFxMount.next;
  if(!d||!p)return;if(n&&n.parentNode===p)p.insertBefore(d,n);else p.appendChild(d);
}
function syncUnifiedPrompt(source='agentic'){
  const a=$('agenticPrompt'),r=$('realtimeDjPrompt');if(!a||!r)return;
  if(source==='realtime')a.value=r.value;else r.value=a.value;
  try{localStorage.setItem('joymetric_realtime_dj_prompt',r.value);}catch(_){}
}
function showRealtimeView(){
  return showAgenticDJView();   /* v31.30.28: Realtime FX view removed -> stay on VocalWeave */
  restoreRealtimeFxDeck();
  clearLibraryCoverBindings();
  $('libraryView')?.classList.add('hidden');
  $('agenticView')?.classList.add('hidden');
  $('workArea')?.classList.add('hidden');
  $('realtimeView')?.classList.remove('hidden');
  state.activeLibrary=null;setNavActive('realtime');
  for(const a of Object.values(audioMap())){try{a?.pause();}catch(_){} }
  refreshNativeRealtimeDevices({keepSelection:true}).catch(()=>{});
  requestAnimationFrame(drawRealtimeSpectrum);
}
function rtDbToGain(db){return Math.pow(10,Number(db||0)/20);}
function rtMakeCurve(){
  const n=4096,curve=new Float32Array(n),k=2.15;
  const norm=Math.tanh(k);
  for(let i=0;i<n;i++){const x=i*2/(n-1)-1;curve[i]=Math.tanh(k*x)/norm;}
  return curve;
}
function rtBuildImpulse(ctx,decay=1.8){
  const seconds=Math.max(.8,Math.min(4.7,Number(decay||1.8)+.55));
  const len=Math.max(1,Math.floor(ctx.sampleRate*seconds)),buf=ctx.createBuffer(2,len,ctx.sampleRate);
  for(let ch=0;ch<2;ch++){
    const data=buf.getChannelData(ch);let seed=(0x9e3779b9^(ch*0x85ebca6b)^(Math.round(decay*997)))>>>0;
    for(let i=0;i<len;i++){
      seed^=seed<<13;seed^=seed>>>17;seed^=seed<<5;
      const r=((seed>>>0)/4294967296)*2-1,t=i/len;
      const envelope=Math.pow(1-t,1.25+Math.max(.25,4.3/Math.max(.7,decay))*.45);
      data[i]=r*envelope*.34;
    }
  }
  return buf;
}
function rtScheduleImpulse(decay){
  const d=Math.max(.7,Math.min(4.5,Number(decay||1.8)));
  if(Math.abs(d-rtState.lastImpulseDecay)<.08)return;
  clearTimeout(rtState.impulseTimer);
  rtState.impulseTimer=setTimeout(()=>{
    if(!rtState.ctx||!rtState.reverb)return;
    try{rtState.reverb.buffer=rtBuildImpulse(rtState.ctx,d);rtState.lastImpulseDecay=d;}catch(_){}
  },190);
}
function rtRamp(param,value,tau=.08){
  if(!param||!rtState.ctx)return;const now=rtState.ctx.currentTime;
  try{param.cancelScheduledValues(now);param.setTargetAtTime(Number(value),now,tau);}catch(_){try{param.value=Number(value);}catch(__){}}
}
function ensureRealtimeGraph(){
  if(rtState.ctx)return rtState.ctx;
  const AC=window.AudioContext||window.webkitAudioContext;if(!AC)throw new Error('Web Audio is not supported by this browser.');
  const ctx=new AC({latencyHint:'interactive'});rtState.ctx=ctx;
  rtState.inputBus=ctx.createGain();
  rtState.inputAnalyser=ctx.createAnalyser();rtState.inputAnalyser.fftSize=4096;rtState.inputAnalyser.smoothingTimeConstant=.72;
  rtState.inputBus.connect(rtState.inputAnalyser);

  rtState.lowCut=ctx.createBiquadFilter();rtState.lowCut.type='highpass';rtState.lowCut.frequency.value=24;rtState.lowCut.Q.value=.68;
  rtState.low=ctx.createBiquadFilter();rtState.low.type='lowshelf';rtState.low.frequency.value=120;
  rtState.mid=ctx.createBiquadFilter();rtState.mid.type='peaking';rtState.mid.frequency.value=850;rtState.mid.Q.value=.72;
  rtState.presence=ctx.createBiquadFilter();rtState.presence.type='peaking';rtState.presence.frequency.value=3000;rtState.presence.Q.value=.85;
  rtState.high=ctx.createBiquadFilter();rtState.high.type='highshelf';rtState.high.frequency.value=8000;
  rtState.highCut=ctx.createBiquadFilter();rtState.highCut.type='lowpass';rtState.highCut.frequency.value=19800;rtState.highCut.Q.value=.68;
  rtState.compressor=ctx.createDynamicsCompressor();

  // Smooth saturation: drive is a crossfade + pre-gain, not a repeatedly replaced
  // waveshaper curve. That keeps feature moves click-free while audio is live.
  rtState.drivePre=ctx.createGain();rtState.drivePre.gain.value=1;
  rtState.shaper=ctx.createWaveShaper();rtState.shaper.oversample='4x';rtState.shaper.curve=rtMakeCurve();
  rtState.driveDry=ctx.createGain();rtState.driveDry.gain.value=1;
  rtState.driveWet=ctx.createGain();rtState.driveWet.gain.value=0;
  rtState.driveJoin=ctx.createGain();

  // Mid/side-equivalent width matrix using only built-in Web Audio nodes.
  rtState.widthSplitter=ctx.createChannelSplitter(2);
  rtState.widthLL=ctx.createGain();rtState.widthLR=ctx.createGain();rtState.widthRL=ctx.createGain();rtState.widthRR=ctx.createGain();
  rtState.widthMerger=ctx.createChannelMerger(2);

  // Hypnotic adds a very slow stereo drift when pushed positive. At zero or
  // negative values the modulation depth is fully off, so the clean path is exact.
  rtState.hypnoticPanner=ctx.createStereoPanner?ctx.createStereoPanner():ctx.createGain();
  if(rtState.hypnoticPanner.pan){
    rtState.hypnoticLfo=ctx.createOscillator();rtState.hypnoticLfo.type='sine';rtState.hypnoticLfo.frequency.value=.18;
    rtState.hypnoticDepth=ctx.createGain();rtState.hypnoticDepth.gain.value=0;
    rtState.hypnoticLfo.connect(rtState.hypnoticDepth);rtState.hypnoticDepth.connect(rtState.hypnoticPanner.pan);rtState.hypnoticLfo.start();
  }

  rtState.dryGain=ctx.createGain();rtState.dryGain.gain.value=1;
  rtState.reverb=ctx.createConvolver();rtState.reverb.buffer=rtBuildImpulse(ctx,1.8);
  rtState.reverbGain=ctx.createGain();rtState.reverbGain.gain.value=.02;
  rtState.delay=ctx.createDelay(1.0);rtState.delay.delayTime.value=.285;
  rtState.delayFeedback=ctx.createGain();rtState.delayFeedback.gain.value=.12;
  rtState.delayGain=ctx.createGain();rtState.delayGain.gain.value=0;
  rtState.processedBus=ctx.createGain();rtState.processedGain=ctx.createGain();rtState.processedGain.gain.value=1;
  rtState.bypassGain=ctx.createGain();rtState.bypassGain.gain.value=0;
  rtState.master=ctx.createGain();rtState.master.gain.value=1;
  rtState.limiter=ctx.createDynamicsCompressor();rtState.limiter.threshold.value=-1;rtState.limiter.knee.value=0;rtState.limiter.ratio.value=20;rtState.limiter.attack.value=.003;rtState.limiter.release.value=.08;
  rtState.analyser=ctx.createAnalyser();rtState.analyser.fftSize=2048;rtState.analyser.smoothingTimeConstant=.84;

  rtState.inputBus.connect(rtState.lowCut);rtState.lowCut.connect(rtState.low);rtState.low.connect(rtState.mid);rtState.mid.connect(rtState.presence);rtState.presence.connect(rtState.high);rtState.high.connect(rtState.highCut);rtState.highCut.connect(rtState.compressor);
  rtState.compressor.connect(rtState.driveDry);rtState.driveDry.connect(rtState.driveJoin);
  rtState.compressor.connect(rtState.drivePre);rtState.drivePre.connect(rtState.shaper);rtState.shaper.connect(rtState.driveWet);rtState.driveWet.connect(rtState.driveJoin);

  rtState.driveJoin.connect(rtState.widthSplitter);
  rtState.widthSplitter.connect(rtState.widthLL,0);rtState.widthLL.connect(rtState.widthMerger,0,0);
  rtState.widthSplitter.connect(rtState.widthLR,0);rtState.widthLR.connect(rtState.widthMerger,0,1);
  rtState.widthSplitter.connect(rtState.widthRL,1);rtState.widthRL.connect(rtState.widthMerger,0,0);
  rtState.widthSplitter.connect(rtState.widthRR,1);rtState.widthRR.connect(rtState.widthMerger,0,1);

  rtState.widthMerger.connect(rtState.hypnoticPanner);
  rtState.hypnoticPanner.connect(rtState.dryGain);rtState.dryGain.connect(rtState.processedBus);
  rtState.hypnoticPanner.connect(rtState.reverb);rtState.reverb.connect(rtState.reverbGain);rtState.reverbGain.connect(rtState.processedBus);
  rtState.hypnoticPanner.connect(rtState.delay);rtState.delay.connect(rtState.delayGain);rtState.delayGain.connect(rtState.processedBus);rtState.delay.connect(rtState.delayFeedback);rtState.delayFeedback.connect(rtState.delay);
  rtState.processedBus.connect(rtState.processedGain);rtState.processedGain.connect(rtState.master);
  rtState.inputBus.connect(rtState.bypassGain);rtState.bypassGain.connect(rtState.master);
  rtState.master.connect(rtState.limiter);rtState.limiter.connect(rtState.analyser);rtState.analyser.connect(ctx.destination);
  applyRealtimeParams(readRealtimeParams(),false);
  // If feature values were restored from the previous session, make the first
  // live graph immediately match what the macro UI shows.
  try{
    if(typeof computeRealtimeFeatureParams==='function'&&typeof readRealtimeFeatures==='function'){
      const features=readRealtimeFeatures(),target=computeRealtimeFeatureParams(features);applyRealtimeParams(target,true);
      if(typeof rtApplyHypnoticMotion==='function')rtApplyHypnoticMotion(features.hypnotic||0);
    }
  }catch(_){}
  return ctx;
}
function disconnectRealtimeSources(){
  try{rtState.mediaSource?.disconnect();}catch(_){}
  try{rtState.liveSource?.disconnect();}catch(_){}
}
function connectRealtimeInput(kind){
  ensureRealtimeGraph();disconnectRealtimeSources();
  if(kind==='file'&&rtState.mediaSource){rtState.mediaSource.connect(rtState.inputBus);rtState.activeInput='file';return true;}
  if((kind==='device'||kind==='system')&&rtState.liveSource){rtState.liveSource.connect(rtState.inputBus);rtState.activeInput=kind;return true;}
  return false;
}
function readRealtimeParams(){
  const out={};for(const [name,id] of Object.entries(rtParamIds)){out[name]=Number($(id)?.value??rtDefaults[name]??0);}return out;
}
function updateRealtimeParamLabels(params=readRealtimeParams()){
  const db=x=>`${Number(x)>=0?'+':''}${Number(x).toFixed(1)} dB`,pct=x=>`${Math.round(Number(x)*100)}%`;
  $('rtLowCutValue').textContent=`${Math.round(params.low_cut_hz)} Hz`;
  $('rtBassValue').textContent=db(params.bass_db);$('rtBodyValue').textContent=db(params.body_db);$('rtPresenceValue').textContent=db(params.presence_db);$('rtAirValue').textContent=db(params.air_db);
  $('rtHighCutValue').textContent=`${(Number(params.high_cut_hz)/1000).toFixed(1)} kHz`;
  $('rtCompressionValue').textContent=pct(params.compression);$('rtDriveValue').textContent=pct(params.drive);$('rtWidthValue').textContent=`${Number(params.width).toFixed(2)}×`;
  $('rtReverbValue').textContent=pct(params.reverb);$('rtReverbDecayValue').textContent=`${Number(params.reverb_decay).toFixed(1)} s`;
  $('rtDelayValue').textContent=pct(params.delay);$('rtDelayMsValue').textContent=`${Math.round(params.delay_ms)} ms`;$('rtOutputValue').textContent=db(params.output_db);
}
function applyRealtimeParams(params,updateInputs=true){
  ensureRealtimeGraph();const p={...rtDefaults,...readRealtimeParams(),...(params||{})};
  if(updateInputs){for(const [name,id] of Object.entries(rtParamIds)){if(p[name]!=null&&$(id))$(id).value=String(p[name]);}}
  updateRealtimeParamLabels(p);
  rtRamp(rtState.lowCut.frequency,p.low_cut_hz,.09);rtRamp(rtState.low.gain,p.bass_db,.09);rtRamp(rtState.mid.gain,p.body_db,.09);rtRamp(rtState.presence.gain,p.presence_db,.09);rtRamp(rtState.high.gain,p.air_db,.09);rtRamp(rtState.highCut.frequency,p.high_cut_hz,.09);
  const c=clamp(Number(p.compression||0));rtRamp(rtState.compressor.threshold,-7-28*c,.08);rtRamp(rtState.compressor.ratio,1+7*c,.08);rtRamp(rtState.compressor.attack,.024-.018*c,.08);rtRamp(rtState.compressor.release,.10+.23*c,.08);rtState.compressor.knee.value=16;
  const drive=clamp(Number(p.drive||0));rtRamp(rtState.drivePre.gain,1+10*drive,.055);rtRamp(rtState.driveWet.gain,Math.min(1,drive*1.05),.055);rtRamp(rtState.driveDry.gain,1-drive*.38,.055);
  const width=Math.max(0,Math.min(1.6,Number(p.width??1))),same=(1+width)/2,cross=(1-width)/2;
  rtRamp(rtState.widthLL.gain,same,.085);rtRamp(rtState.widthRR.gain,same,.085);rtRamp(rtState.widthLR.gain,cross,.085);rtRamp(rtState.widthRL.gain,cross,.085);
  const reverbWet=clamp(Number(p.reverb||0));rtRamp(rtState.reverbGain.gain,Math.min(.90,reverbWet*1.34),.10);rtScheduleImpulse(p.reverb_decay);
  const delayWet=Math.min(.12,clamp(Number(p.delay||0)));rtRamp(rtState.delay.delayTime,Math.max(.055,Math.min(.62,Number(p.delay_ms||285)/1000)),.10);rtRamp(rtState.delayGain.gain,Math.min(.12,delayWet*.55),.10);rtRamp(rtState.delayFeedback.gain,Math.min(.22,.06+.18*delayWet),.10);
  // At high ambience values the dry path yields slightly, so Dreamy/Space/Hypnotic
  // read as an actual character change instead of just a quiet effect underneath.
  rtRamp(rtState.dryGain.gain,Math.max(.68,1-.25*reverbWet-.025*delayWet),.10);
  rtRamp(rtState.master.gain,rtDbToGain(p.output_db),.09);
}
const rtParamBounds={
  low_cut_hz:[20,180],bass_db:[-9,9],body_db:[-9,9],presence_db:[-7,7],air_db:[-9,9],high_cut_hz:[6000,20000],
  compression:[0,1],drive:[0,1],width:[0,1.6],reverb:[0,.65],reverb_decay:[.7,4.5],delay:[0,.12],delay_ms:[55,620],output_db:[-6,3]
};
function clampRealtimeParam(name,value){const [lo,hi]=rtParamBounds[name]||[-1e9,1e9];return Math.max(lo,Math.min(hi,Number(value)));}
function morphRealtimeParams(target,duration=420){
  const start=readRealtimeParams(),t0=performance.now();
  const tick=now=>{
    const u=clamp((now-t0)/duration),k=u*u*(3-2*u),cur={};
    for(const name of Object.keys(rtParamIds)){cur[name]=start[name]+(Number(target[name]??start[name])-start[name])*k;}
    applyRealtimeParams(cur,true);if(u<1)requestAnimationFrame(tick);else applyRealtimeParams(target,true);
  };requestAnimationFrame(tick);
}
const rtFeatureIds={
  air:'rtFeatureAir',warmth:'rtFeatureWarmth',brightness:'rtFeatureBrightness',bass:'rtFeatureBass',clarity:'rtFeatureClarity',
  hypnotic:'rtFeatureHypnotic',dreamy:'rtFeatureDreamy',space:'rtFeatureSpace',width:'rtFeatureWidth',intimacy:'rtFeatureIntimacy',
  punch:'rtFeaturePunch',joy:'rtFeatureJoy',depth:'rtFeatureDepth',energy:'rtFeatureEnergy',vintage:'rtFeatureVintage'
};
const rtFeatureValueIds={
  air:'rtFeatureAirValue',warmth:'rtFeatureWarmthValue',brightness:'rtFeatureBrightnessValue',bass:'rtFeatureBassValue',clarity:'rtFeatureClarityValue',
  hypnotic:'rtFeatureHypnoticValue',dreamy:'rtFeatureDreamyValue',space:'rtFeatureSpaceValue',width:'rtFeatureWidthValue',intimacy:'rtFeatureIntimacyValue',
  punch:'rtFeaturePunchValue',joy:'rtFeatureJoyValue',depth:'rtFeatureDepthValue',energy:'rtFeatureEnergyValue',vintage:'rtFeatureVintageValue'
};
function rtShapeFeature(v){
  const x=Math.max(-1,Math.min(1,Number(v||0)/100));
  // v30.4: keep the same near-linear slider geometry. Audible strength is raised
  // in the macro mapping, not by distorting the physical slider curve.
  return Math.sign(x)*Math.pow(Math.abs(x),.98);
}
function rtUnshapeFeature(v){
  const x=Math.max(-1,Math.min(1,Number(v||0)));
  return Math.sign(x)*Math.pow(Math.abs(x),1/.98)*100;
}
function readRealtimeFeatures(){
  const out={};for(const [name,id] of Object.entries(rtFeatureIds))out[name]=rtShapeFeature($(id)?.value||0);return out;
}
function updateRealtimeFeatureLabels(){
  for(const [name,id] of Object.entries(rtFeatureIds)){
    const raw=Math.round(Number($(id)?.value||0)),label=$(rtFeatureValueIds[name]);if(label)label.textContent=raw>0?`+${raw}`:`${raw}`;
    const card=$(id)?.closest?.('.realtime-feature');if(card){card.classList.toggle('positive',raw>0);card.classList.toggle('negative',raw<0);card.classList.toggle('active',raw!==0);}
  }
}
function readRealtimeEffectIntensity(){
  const el=$('realtimeEffectIntensity');
  const raw=el?Number(el.value):Math.round((nativeRtState.effectIntensity||1)*100);
  return Math.max(0,Math.min(1.60,(Number.isFinite(raw)?raw:100)/100));
}
function updateRealtimeEffectIntensityLabel(){
  const x=readRealtimeEffectIntensity();nativeRtState.effectIntensity=x;
  if($('realtimeEffectIntensityValue'))$('realtimeEffectIntensityValue').textContent=`${Math.round(x*100)}%`;
}
function readNeuralPolishWet(){
  const el=$('realtimeNeuralPolishWet'),raw=Number(el?.value??8);
  return Math.max(0,Math.min(.20,(Number.isFinite(raw)?raw:8)/100));
}
function updateNeuralPolishUiLabel(){
  nativeRtState.neuralPolish=Boolean($('realtimeNeuralPolish')?.checked);nativeRtState.neuralPolishWet=readNeuralPolishWet();
  if($('realtimeNeuralPolishValue'))$('realtimeNeuralPolishValue').textContent=`${Math.round(nativeRtState.neuralPolishWet*100)}%`;
}
function computeRealtimeFeatureParams(features=readRealtimeFeatures()){
  const p={...rtDefaults};
  // v30.4.24: 3x stronger physical render than v30.4.23.
  // MusicCLAP/15D directions are unchanged; only render authority is increased.
  const G=19.5*readRealtimeEffectIntensity(),sc=x=>Math.max(-19.5,Math.min(19.5,Number(x||0)*G));
  const a=sc(features.air),w=sc(features.warmth),br=sc(features.brightness),ba=sc(features.bass),c=sc(features.clarity);
  const h=sc(features.hypnotic),dr=sc(features.dreamy),sp=sc(features.space),wd=sc(features.width),inti=sc(features.intimacy);
  const pu=sc(features.punch),j=sc(features.joy),d=sc(features.depth),en=sc(features.energy),v=sc(features.vintage);

  // TONE & COLOR ------------------------------------------------------------
  // Air: more obvious high-shelf opening/closing plus presence lift.
  p.air_db+=8.1*a;p.presence_db+=1.9*a;p.high_cut_hz+=a<0?8800*a:220*a;
  // Warmth: richer low mids, a little low-end bloom and audible harmonic colour.
  p.bass_db+=5.25*w;p.body_db+=4.55*w;p.air_db-=2.55*w;p.presence_db-=.8*w;p.drive+=.24*Math.max(0,w);p.high_cut_hz-=2350*Math.max(0,w);
  // Brightness: stronger broad spectral tilt than Air.
  p.air_db+=6.05*br;p.presence_db+=3.0*br;p.body_db-=.9*br;p.high_cut_hz+=br<0?7200*br:210*br;
  // Bass: clearly changes low-end weight at moderate slider values.
  p.bass_db+=7.45*ba;p.body_db+=1.75*ba;p.low_cut_hz+=68*Math.max(0,-ba)-7*Math.max(0,ba);p.output_db-=.42*Math.max(0,ba);
  // Clarity: stronger presence/air separation versus a distinctly softer haze.
  p.presence_db+=5.15*c;p.air_db+=2.65*c;p.body_db-=1.9*c;p.bass_db-=.65*c;p.low_cut_hz+=27*Math.max(0,c);p.high_cut_hz+=c<0?4350*c:130*c;

  // MOTION & SPACE -----------------------------------------------------------
  // Hypnotic: a deliberately obvious animated field, not merely extra reverb.
  p.body_db+=1.55*h;p.presence_db-=1.45*h;p.width+=.46*h;p.reverb+=.39*h;p.reverb_decay+=2.15*h;p.delay+=.07*h;p.delay_ms+=165*h;p.high_cut_hz-=650*Math.max(0,h);
  // Dreamy: floating soft-focus ambience, long tail and a noticeably diffused top.
  p.air_db+=1.55*dr;p.presence_db-=2.05*dr;p.body_db+=.75*dr;p.width+=.37*dr;p.reverb+=.43*dr;p.reverb_decay+=2.35*dr;p.delay+=.018*dr;p.high_cut_hz-=1550*Math.max(0,dr);
  // Space: large environmental move with stronger wet level and decay.
  p.reverb+=.50*sp;p.reverb_decay+=2.70*sp;p.width+=.43*sp;p.delay+=.015*Math.max(0,sp);p.delay_ms+=92*sp;
  // Width: direct stereo image control that reaches the practical graph limits.
  p.width+=.48*wd;p.reverb+=.045*Math.max(0,wd);
  // Intimacy: stronger dry/forward move in the positive direction, more distant
  // and ambient when negative.
  p.presence_db+=2.35*inti;p.body_db+=1.25*inti;p.width-=.27*inti;p.reverb-=.31*inti;p.reverb_decay-=1.25*inti;p.delay-=.10*inti;p.air_db+=.45*inti;

  // IMPACT & FEEL ------------------------------------------------------------
  // Punch: stronger compressor action, presence snap and low-end hit.
  p.bass_db+=3.05*pu;p.presence_db+=4.15*pu;p.compression+=.59*pu;p.drive+=.18*Math.max(0,pu);p.reverb-=.065*Math.max(0,pu);p.output_db-=.48*Math.max(0,pu);
  // Joy: broader lift across sparkle, presence, width and density.
  p.air_db+=3.55*j;p.presence_db+=2.45*j;p.bass_db+=1.45*j;p.body_db+=.75*j;p.width+=.27*j;p.reverb+=.085*j;p.compression+=.16*j;
  // Depth: a more physical foundation/body shift.
  p.bass_db+=5.25*d;p.body_db+=3.65*d;p.presence_db-=.7*d;p.low_cut_hz+=78*Math.max(0,-d)-6*Math.max(0,d);
  // Energy: denser, more forward and more harmonically active.
  p.compression+=.45*en;p.presence_db+=2.85*en;p.air_db+=1.8*en;p.bass_db+=1.65*en;p.drive+=.18*Math.max(0,en);p.width+=.12*en;p.output_db-=.34*Math.max(0,en);
  // Vintage: unmistakably aged bandwidth + saturation versus cleaner/modern.
  p.air_db-=4.05*v;p.presence_db-=.95*v;p.body_db+=2.35*v;p.drive+=.33*Math.max(0,v);p.high_cut_hz-=6100*Math.max(0,v);p.high_cut_hz+=180*Math.max(0,-v);p.width-=.12*Math.max(0,v);p.reverb+=.055*Math.max(0,v);

  for(const name of Object.keys(rtParamIds))p[name]=clampRealtimeParam(name,p[name]);
  return p;
}
function realtimeChangedParams(target,current){
  const changed=[];for(const name of Object.keys(rtParamIds)){
    const [lo,hi]=rtParamBounds[name]||[0,1],span=Math.max(1e-9,hi-lo),a=Number(current?.[name]??rtDefaults[name]),b=Number(target?.[name]??a);
    if(Number.isFinite(a)&&Number.isFinite(b)&&Math.abs(b-a)/span>=.006)changed.push(name);
  }return changed;
}
function highlightRealtimeFeatureChanges(names){
  const wanted=new Set((Array.isArray(names)?names:[]).map(String));
  document.querySelectorAll('.realtime-param.feature-changed').forEach(el=>el.classList.remove('feature-changed'));
  for(const [name,id] of Object.entries(rtParamIds)){
    if(!wanted.has(name))continue;const card=$(id)?.closest?.('.realtime-param');if(!card)continue;
    card.classList.add('feature-changed');clearTimeout(card.__featureGlowTimer);card.__featureGlowTimer=setTimeout(()=>card.classList.remove('feature-changed'),620);
  }
}
function rtApplyHypnoticMotion(h){
  if(!rtState.ctx)return;const positive=Math.max(0,Number(h||0));
  if(rtState.hypnoticDepth?.gain)rtRamp(rtState.hypnoticDepth.gain,Math.min(.92,.78*positive),.16);
  if(rtState.hypnoticLfo?.frequency)rtRamp(rtState.hypnoticLfo.frequency,.11+.43*positive,.18);
}
function saveRealtimeFeatures(){
  try{const raw={};for(const [name,id] of Object.entries(rtFeatureIds))raw[name]=Number($(id)?.value||0);localStorage.setItem('joymetricRealtimeFeaturesV2',JSON.stringify(raw));}catch(_){}
}
function restoreRealtimeFeatures(){
  try{const raw=JSON.parse(localStorage.getItem('joymetricRealtimeFeaturesV2')||localStorage.getItem('joymetricRealtimeFeaturesV1')||'{}');for(const [name,id] of Object.entries(rtFeatureIds)){if($(id)&&Number.isFinite(Number(raw[name])))$(id).value=String(Math.max(-100,Math.min(100,Number(raw[name]))));}}catch(_){}
  updateRealtimeFeatureLabels();
}
function activeRealtimeFeatureSummary(){
  const parts=[];for(const [name,id] of Object.entries(rtFeatureIds)){const v=Math.round(Number($(id)?.value||0));if(Math.abs(v)>=2)parts.push(`${name[0].toUpperCase()+name.slice(1)} ${v>0?'+':''}${v}`);}return parts;
}
function syncRealtimeParamMirror(params){
  const p={...rtDefaults,...(params||{})};
  for(const [name,id] of Object.entries(rtParamIds)){if($(id)&&p[name]!=null)$(id).value=String(p[name]);}
  updateRealtimeParamLabels(p);
}
function nativeRealtimePayload(){
  const features=readRealtimeFeatures(),params=computeRealtimeFeatureParams(features);
  const djMode=$('realtimeDjMode')?Boolean($('realtimeDjMode').checked):false;
  const djInput=$('realtimeDjPrompt');
  const djPrompt=String(djInput?djInput.value:nativeRtState.djPrompt||'').trim();
  const aiEnabled=djMode?true:($('realtimeAiEnabled')?Boolean($('realtimeAiEnabled').checked):true);
  return {features,params,effect_intensity:readRealtimeEffectIntensity(),neural_polish_enabled:false,neural_polish_wet:0.0,ai_enabled:aiEnabled,dj_mode:djMode,dj_prompt:djPrompt,dsp50_mode:false,dsp50_prompt:'',dsp50_intensity:1.0};
}

function formatDjNodeValue(name,value){
  const v=Number(value);if(!Number.isFinite(v))return '—';
  if(name.endsWith('_db'))return `${v>=0?'+':''}${v.toFixed(1)} dB`;
  if(name==='high_cut_hz'||name==='reverb_tone_hz')return `${(v/1000).toFixed(1)} kHz`;
  if(name==='delay_ms')return `${Math.round(v)} ms`;
  if(name==='reverb_decay')return `${v.toFixed(2)} s`;
  if(name==='hypnotic_rate_hz')return `${v.toFixed(2)} Hz`;
  if(name==='width')return `${v.toFixed(2)}×`;
  if(['reverb','delay','delay_feedback','reverb_damping','hypnotic_motion_depth','compression','drive'].includes(name))return `${Math.round(v*100)}%`;
  return v.toFixed(2);
}
function renderDjNodeState(st){
  const grads=st?.dj_node_gradients||{},vals=st?.dj_node_values||st?.dsp_params||{};
  document.querySelectorAll('[data-dj-node]').forEach(row=>{
    const name=row.dataset.djNode,g=Math.max(-1,Math.min(1,Number(grads[name]||0))),v=vals[name];
    row.classList.toggle('positive',g>.006);row.classList.toggle('negative',g<-.006);
    const fill=row.querySelector('i>b'),em=row.querySelector('em');
    if(fill){const width=Math.min(50,Math.abs(g)*50);fill.style.width=`${width}%`;fill.style.left=g>=0?'50%':`${50-width}%`;}
    if(em)em.textContent=`${formatDjNodeValue(name,v)} · ∂${g>=0?'+':''}${g.toFixed(3)}`;
  });
}
function syncDjFeaturesFromStatus(st){
  if(!st?.dj_mode||!st.features)return;
  for(const [name,id] of Object.entries(rtFeatureIds)){
    const input=$(id);if(!input)continue;
    const raw=Math.max(-100,Math.min(100,rtUnshapeFeature(Number(st.features[name]||0))));
    input.value=String(Math.round(raw));
    const grad=Number(st.dj_feature_gradients?.[name]||0),label=$(rtFeatureValueIds[name]),card=input.closest?.('.realtime-feature');
    if(label)label.title=`live prompt derivative ${grad>=0?'+':''}${grad.toFixed(4)}`;
    if(card)card.dataset.djDerivative=`${grad>=0?'+':''}${grad.toFixed(4)}`;
  }
  updateRealtimeFeatureLabels();
  if(st.dsp_params)syncRealtimeParamMirror(st.dsp_params);
}
function setDjModeUi(on,st=null){
  const enabled=Boolean(on),view=$('realtimeView'),box=$('djModeController');nativeRtState.djMode=enabled;
  view?.classList.toggle('dj-mode-active',enabled);box?.classList.toggle('engaged',enabled);
  const locked=enabled;
  document.querySelectorAll('[data-rt-feature]').forEach(el=>{el.disabled=locked;});
  if($('rtFeatureZeroBtn'))$('rtFeatureZeroBtn').disabled=locked;if($('realtimeResetBtn'))$('realtimeResetBtn').disabled=locked;
  if($('realtimeAiEnabled'))$('realtimeAiEnabled').disabled=locked;
  // Prompt input is deliberately UI-authoritative. Status polling must never rewrite a user's draft.
  const prompt=$('realtimeDjPrompt');
  const status=$('realtimeDjStatus'),score=$('realtimeDjScore');
  if(status)status.textContent=enabled?(st?.ai_state||'DJ Gradient engaged · listening for derivative field'):'DJ Mode off · manual 15D controls available';
  if(score){const hz=Number(st?.dj_update_hz||0),win=Number(st?.dj_window_ms||0),age=Number(st?.dj_result_age_ms||0);score.textContent=enabled?`prompt score ${Number(st?.dj_score||0).toFixed(4)} · cycle ${Number(st?.dj_cycle||0)}${hz>0?` · ${hz.toFixed(2)} Hz`:''}${win>0?` · ${Math.round(win)} ms current`:''}${age>0?` · ${Math.round(age)} ms age`:''}`:'prompt score — · cycle 0';}
  if(enabled&&st)syncDjFeaturesFromStatus(st);
  renderDjNodeState(st||{});
}

function formatDsp50Value(name,value,spec={}){
  const v=Number(value);if(!Number.isFinite(v))return '—';const unit=String(spec.unit||'');
  if(unit==='dB')return `${v>=0?'+':''}${v.toFixed(1)} dB`;
  if(unit==='Hz'){if(Math.abs(v)>=1000)return `${(v/1000).toFixed(v>=10000?1:2)} kHz`;return `${Math.round(v)} Hz`;}
  if(unit==='ms')return `${v.toFixed(v<20?1:0)} ms`;
  if(unit==='s')return `${v.toFixed(2)} s`;
  if(unit==='Q')return `Q ${v.toFixed(2)}`;
  if(unit==='x')return `${v.toFixed(2)}×`;
  if(unit==='°')return `${Math.round(v*360)}°`;
  if(unit==='%'){
    if(['compression','drive','saturation_mix','low_drive','mid_drive','high_drive','hypnotic_motion_depth','reverb','reverb_diffusion','reverb_damping','delay','delay_feedback'].includes(name))return `${Math.round(v*100)}%`;
    return `${v>=0?'+':''}${Math.round(v*100)}%`;
  }
  return Math.abs(v)>=100?Math.round(v).toString():v.toFixed(2);
}
function ensureDsp50Grid(specs){
  const grid=$('realtimeDsp50Grid');if(!grid||!specs||!Object.keys(specs).length)return;
  const signature=Object.keys(specs).join('|');if(grid.dataset.signature===signature)return;
  grid.dataset.signature=signature;grid.innerHTML='';
  const order=['EQ','DYNAMICS','HARMONICS','STEREO','REVERB','DELAY'];
  for(const group of order){
    const names=Object.keys(specs).filter(n=>String(specs[n]?.group||'')===group);if(!names.length)continue;
    const section=document.createElement('section');section.className='dsp50-group';section.dataset.group=group;
    const head=document.createElement('div');head.className='dsp50-group-head';head.innerHTML=`<span>${group}</span><b>${names.length} nodes</b>`;section.appendChild(head);
    const nodes=document.createElement('div');nodes.className='dsp50-group-grid';
    for(const name of names){
      const sp=specs[name]||{},card=document.createElement('article');card.className='dsp50-node-card';card.dataset.dsp50Node=name;
      card.innerHTML=`<div class="dsp50-node-top"><strong>${sanitizeVisibleText(sp.label||name)}</strong><span>${sanitizeVisibleText(name)}</span></div><div class="dsp50-node-values"><b data-role="value">—</b><em data-role="gradient">∂ 0.000</em></div><div class="dsp50-position"><i data-role="neutral"></i><b data-role="current"></b></div><div class="dsp50-derivative"><i data-role="derivativeFill"></i></div><small data-role="confidence">confidence 0%</small>`;
      nodes.appendChild(card);
    }
    section.appendChild(nodes);grid.appendChild(section);
  }
}
function renderDsp50State(st){
  const enabled=Boolean(st?.dsp50_mode),section=$('realtimeDsp50Section'),box=$('dsp50ModeController');nativeRtState.dsp50Mode=enabled;
  section?.classList.toggle('active',enabled);section?.setAttribute('aria-hidden',enabled?'false':'true');box?.classList.toggle('engaged',enabled);
  const specs=st?.dsp50_specs||{};if(Object.keys(specs).length)ensureDsp50Grid(specs);
  const vals=st?.dsp50_node_values||st?.dsp_params||{},grads=st?.dsp50_gradients||{},conf=st?.dsp50_confidence||{},active=new Set(st?.dsp50_active_nodes||[]);
  document.querySelectorAll('[data-dsp50-node]').forEach(card=>{
    const name=card.dataset.dsp50Node,sp=specs[name]||{},lo=Number(sp.min),hi=Number(sp.max),def=Number(sp.default),val=Number(vals[name]),g=Number(grads[name]||0),c=Math.max(0,Math.min(1,Number(conf[name]||0)));
    card.classList.toggle('active-node',active.has(name));card.classList.toggle('positive',g>.004);card.classList.toggle('negative',g<-.004);
    const vEl=card.querySelector('[data-role="value"]'),gEl=card.querySelector('[data-role="gradient"]'),cEl=card.querySelector('[data-role="confidence"]'),cur=card.querySelector('[data-role="current"]'),neu=card.querySelector('[data-role="neutral"]'),fill=card.querySelector('[data-role="derivativeFill"]');
    if(vEl)vEl.textContent=formatDsp50Value(name,val,sp);if(gEl)gEl.textContent=`∂ ${g>=0?'+':''}${g.toFixed(3)}`;if(cEl)cEl.textContent=`confidence ${Math.round(c*100)}%${active.has(name)?' · ACTIVE':''}`;
    if(Number.isFinite(lo)&&Number.isFinite(hi)&&hi>lo){const pos=Math.max(0,Math.min(100,(val-lo)/(hi-lo)*100)),zero=Math.max(0,Math.min(100,(def-lo)/(hi-lo)*100));if(cur)cur.style.left=`${pos}%`;if(neu)neu.style.left=`${zero}%`;}
    if(fill){const m=Math.min(1,Math.abs(g)/.55),w=m*50;fill.style.width=`${w}%`;fill.style.left=g>=0?'50%':`${50-w}%`;}
  });
  const status=$('realtimeDsp50Status'),score=$('realtimeDsp50Score'),count=$('realtimeDsp50ActiveCount');
  if(status)status.textContent=enabled?(st?.ai_state||'50D CPU current-moment controller · measuring prompt derivatives'):'50D Lab off · choose this mode for direct DSP optimization';
  if(score){const hz=Number(st?.dsp50_update_hz||0),win=Number(st?.dsp50_window_ms||0),age=Number(st?.dsp50_result_age_ms||0),renders=Number(st?.dsp50_render_count||0),opt=String(st?.dsp50_optimizer||'Projected AdaBelief');score.textContent=enabled?`prompt score ${Number(st?.dsp50_score||0).toFixed(4)} · cycle ${Number(st?.dsp50_cycle||0)}${hz>0?` · ${hz.toFixed(2)} Hz`:''}${win>0?` · ${Math.round(win)} ms current window`:''}${age>0?` · ${Math.round(age)} ms result age`:''}${renders?` · ${renders} probes`:''} · ${opt}`:'prompt score — · cycle 0';}
  if(count)count.textContent=`${active.size} active node${active.size===1?'':'s'} / 50`;
  // Prompt input is deliberately UI-authoritative. Backend status may lag by one or more polls.
  const prompt=$('realtimeDsp50Prompt');
  const intensity=$('realtimeDsp50Intensity');if(intensity&&Number.isFinite(Number(st?.dsp50_intensity))&&document.activeElement!==intensity)intensity.value=String(Math.round(Number(st.dsp50_intensity)*100));
  if($('realtimeDsp50IntensityValue'))$('realtimeDsp50IntensityValue').textContent=`${(Number(intensity?.value||145)/100).toFixed(2)}×`;
}
function setDsp50ModeUi(on,st=null){
  const enabled=Boolean(on),view=$('realtimeView');nativeRtState.dsp50Mode=enabled;
  view?.classList.toggle('dsp50-mode-active',enabled);
  const locked=enabled||Boolean(nativeRtState.djMode);document.querySelectorAll('[data-rt-feature]').forEach(el=>{el.disabled=locked;});
  if($('rtFeatureZeroBtn'))$('rtFeatureZeroBtn').disabled=locked;if($('realtimeResetBtn'))$('realtimeResetBtn').disabled=locked;if($('realtimeAiEnabled'))$('realtimeAiEnabled').disabled=locked;
  renderDsp50State(st||{dsp50_mode:enabled,dsp50_prompt:String($('realtimeDsp50Prompt')?.value||''),dsp50_intensity:Number($('realtimeDsp50Intensity')?.value||145)/100});
}
function queueNativeRealtimeControls(){
  clearTimeout(nativeRtState.pushTimer);
  // Snapshot NOW. A stale /status response must not be able to alter the payload
  // between Apply/input and this debounced POST.
  const payload=nativeRealtimePayload(),revision=++nativeRtState.controlRevision;
  nativeRtState.controlHoldUntil=Math.max(nativeRtState.controlHoldUntil,performance.now()+1400);
  nativeRtState.pushTimer=setTimeout(async()=>{
    try{
      const r=await fetch('/api/realtime/controls',{method:'POST',headers:{'Content-Type':'application/json','X-JoyMetric-Control-Revision':String(revision)},body:JSON.stringify(payload)});
      const j=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(j.error||`HTTP ${r.status}`);
      // Keep the local draft; only release the stale-status guard sooner after a successful ack.
      nativeRtState.controlHoldUntil=Math.max(nativeRtState.controlHoldUntil,performance.now()+420);
    }catch(e){
      const status=$('realtimeRouteStatus');if(status)status.textContent=`Realtime control update failed: ${sanitizeVisibleText(e.message)}`;
    }
  },18);
}
function applyRealtimeFeatureMacros(changedFeature='feature'){
  const current=readRealtimeParams();updateRealtimeFeatureLabels();const features=readRealtimeFeatures(),target=computeRealtimeFeatureParams(features),changed=realtimeChangedParams(target,current);
  syncRealtimeParamMirror(target);highlightRealtimeFeatureChanges(changed);saveRealtimeFeatures();queueNativeRealtimeControls();
  const active=activeRealtimeFeatureSummary(),status=$('realtimeRouteStatus');
  if(status&&!nativeRtState.running)status.textContent=active.length?`${active.join(' · ')} · ready. Press LET'S GO to hear this character on the selected live input.`:'All 15 dimensions centered · choose routing and press LET\'S GO.';
}
function zeroRealtimeFeatures({announce=true}={}){
  for(const id of Object.values(rtFeatureIds))if($(id))$(id).value='0';updateRealtimeFeatureLabels();saveRealtimeFeatures();syncRealtimeParamMirror(rtDefaults);queueNativeRealtimeControls();
  if(announce&&$('realtimeRouteStatus'))$('realtimeRouteStatus').textContent=nativeRtState.running?'All 15 dimensions centered · native realtime signal returned to neutral.':'All 15 dimensions centered · neutral settings ready.';
}

// Browser MediaStream/file routing from v22.7 was removed in v22.8.
// Realtime audio now flows entirely through the native WASAPI bridge.
function resetRealtimeDSP(){zeroRealtimeFeatures({announce:true});}

function rtPopulateSelect(select,items,defaultId,kind){
  if(!select)return;
  const before=select.value;select.innerHTML='';
  if(!items?.length){const o=document.createElement('option');o.value='';o.textContent=`No ${kind} devices found`;select.appendChild(o);return;}
  const apps=kind==='input'?items.filter(x=>x.app_capture):[],loopbacks=kind==='input'?items.filter(x=>x.loopback&&!x.app_capture):[],normal=kind==='input'?items.filter(x=>!x.loopback&&!x.app_capture):items;
  const addGroup=(label,list)=>{
    if(!list.length)return;const g=document.createElement('optgroup');g.label=label;
    for(const d of list){const o=document.createElement('option');o.value=String(d.id);o.textContent=d.label||`${kind} ${d.id}`;o.dataset.loopback=d.loopback?'1':'0';o.dataset.appCapture=d.app_capture?'1':'0';g.appendChild(o);}select.appendChild(g);
  };
  if(kind==='input'){addGroup('Apps · AUTO → JOYMETRIC DRIVER',apps);addGroup('Playback endpoints · WASAPI LOOPBACK fallback',loopbacks);addGroup('Recording inputs',normal);}else addGroup('Windows Audio Out',normal);
  if(before&&items.some(x=>String(x.id)===String(before)))select.value=before;
  else if(defaultId!=null&&items.some(x=>String(x.id)===String(defaultId)))select.value=String(defaultId);
  else select.value=String(items[0].id);
}
async function refreshNativeRealtimeDevices({keepSelection=true,force=false}={}){
  if(nativeRtState.scanning)return;nativeRtState.scanning=true;
  const status=$('realtimeRouteStatus'),hint=$('realtimeInputHint'),inSel=$('realtimeInputDevice'),outSel=$('realtimeOutputDevice');
  const oldIn=keepSelection?inSel?.value:'',oldOut=keepSelection?outSel?.value:'';
  if(status)status.textContent='Scanning native Windows WASAPI devices…';
  try{
    const r=await fetch(`/api/realtime/devices${force?'?force=1':''}`,{cache:'no-store'}),j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||`HTTP ${r.status}`);
    rtPopulateSelect(inSel,j.inputs||[],j.default_input_id,'input');rtPopulateSelect(outSel,j.outputs||[],j.default_output_id,'output');
    if(oldIn&&(j.inputs||[]).some(x=>String(x.id)===String(oldIn)))inSel.value=oldIn;
    if(oldOut&&(j.outputs||[]).some(x=>String(x.id)===String(oldOut)))outSel.value=oldOut;
    const appTap=(j.inputs||[]).find(x=>x.app_capture&&/spotify/i.test(x.label||''))||(j.inputs||[]).find(x=>x.app_capture);
    if(hint)hint.innerHTML=appTap
      ?`<b>${sanitizeVisibleText(appTap.label||'App Tap')} is ready.</b> Select it and your speakers/headphones, then press LET'S GO. JoyMetric captures only that app and writes to the exact WASAPI Audio Out endpoint selected below. LET'S GO automatically moves the selected app to JoyMetric's internal virtual-audio driver, captures its private paired endpoint, processes it, and sends only the processed return to the exact Audio Out selected below. No VoiceMeeter, VB-CABLE, or manual internal-channel selection is used.`
      :`No active app audio session was found yet. Start playback in Spotify (or the target app), press <b>Scan audio</b>, then choose its APP TAP. Normal LOOPBACK/mic inputs remain available as fallbacks.${j.process_capture_error?`<br><small>${sanitizeVisibleText(j.process_capture_error)}</small>`:''}`;
    if(status)status.textContent=j.scan_warning?`Audio scan recovered safely · ${sanitizeVisibleText(j.scan_warning)}`:`Found ${(j.inputs||[]).length} input/capture routes and ${(j.outputs||[]).length} audio outputs. Pick both, then press LET'S GO.`;
  }catch(e){
    if(status)status.textContent=`Audio scan failed: ${sanitizeVisibleText(e.message)}`;
    if(hint)hint.textContent='Native realtime requires PyAudioWPatch. Restart JoyMetric once if the first-run dependency install did not finish.';
  }finally{nativeRtState.scanning=false;}
}
function rtSetNativeRunningUI(on,statusData=null){
  nativeRtState.running=Boolean(on);nativeRtState.lastStatus=statusData||nativeRtState.lastStatus;
  $('realtimeLetsGoBtn')?.classList.toggle('live',Boolean(on));
  if($('realtimeLetsGoBtn')){$('realtimeLetsGoBtn').disabled=false;$('realtimeLetsGoBtn').querySelector('strong').textContent=on?'LIVE · PROCESSING':"LET'S GO";$('realtimeLetsGoBtn').querySelector('small').textContent=on?'Click again to stop':'Route app → JoyMetric Channel → DSP → Audio Out';}
  if($('realtimeInputDevice'))$('realtimeInputDevice').disabled=Boolean(on);
  if($('realtimeOutputDevice'))$('realtimeOutputDevice').disabled=Boolean(on);
  if($('realtimeRefreshDevices'))$('realtimeRefreshDevices').disabled=Boolean(on);
  $('realtimeRouteStatus')?.classList.toggle('live',Boolean(on));
  if(statusData?.input_label&&$('realtimeSourceTitle'))$('realtimeSourceTitle').textContent=on?`${statusData.input_label}  →  ${statusData.output_label||'Audio Out'}`:'Realtime stopped';
}
async function startNativeRealtime(){
  const inSel=$('realtimeInputDevice'),outSel=$('realtimeOutputDevice'),btn=$('realtimeLetsGoBtn'),status=$('realtimeRouteStatus');
  const inputId=inSel?.value||'',outputId=outSel?.value||'';
  if(!inputId||!outputId){if(status)status.textContent='Choose both Input Channel and Audio Out first.';return;}
  if(btn){btn.disabled=true;btn.querySelector('strong').textContent='STARTING…';btn.querySelector('small').textContent='Opening native Windows audio route';}
  if(status)status.textContent='Opening native App/Input → DSP → Audio Out route…';
  try{
    const payload={input_id:inputId,output_id:outputId,...nativeRealtimePayload()};
    const r=await fetch('/api/realtime/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||`HTTP ${r.status}`);
    const st=j.status||{};rtSetNativeRunningUI(true,st);if(status){const buf=st.buffer_ms?` · ${Number(st.buffer_ms).toFixed(2)} ms block`:'';const mode=st.dj_mode?'CONTINUOUS DJ 15D':'15D FEATURES';const src=st.source_kind==='joymetric_virtual_driver'?'JOYMETRIC DRIVER · PROCESSED-ONLY':(st.source_kind==='process_loopback'?(st.source_suppressed?'APP TAP · PROCESSED-ONLY':'APP TAP · ARMING'):(st.input_label||inSel.selectedOptions?.[0]?.textContent||'Input'));status.textContent=`LIVE · ${src} → ${mode} → ${st.output_label||outSel.selectedOptions?.[0]?.textContent||'Audio Out'}${st.playback_backend?` · ${st.playback_backend}`:''} · ${st.sample_rate||''} Hz${st.channels?` · ${st.channels} ch`:''}${buf} · DRY-ANCHOR · FULL-RATE HI-FI`; }
    startNativeRealtimePolling();
  }catch(e){rtSetNativeRunningUI(false);if(status)status.textContent=`Could not start realtime audio: ${sanitizeVisibleText(e.message)}`;if(btn)btn.disabled=false;}
}
async function stopNativeRealtime(){
  try{await fetch('/api/realtime/stop',{method:'POST'});}catch(_){}
  rtSetNativeRunningUI(false,{input_label:'Realtime stopped',output_label:''});if($('realtimeRouteStatus'))$('realtimeRouteStatus').textContent='Realtime stopped. Select a route and press LET\'S GO to start again.';
}
async function pollNativeRealtimeStatus(){
  try{
    const r=await fetch('/api/realtime/status',{cache:'no-store'}),st=await r.json();nativeRtState.lastStatus=st;
    if(Boolean(st.running)!==nativeRtState.running)rtSetNativeRunningUI(Boolean(st.running),st);
    const localOwn=realtimeControlsLocallyOwned();
    if(!localOwn){
      if($('realtimeDjMode'))$('realtimeDjMode').checked=Boolean(st.dj_mode);
      if($('realtimeDsp50Mode'))$('realtimeDsp50Mode').checked=Boolean(st.dsp50_mode);
      setDsp50ModeUi(Boolean(st.dsp50_mode),st);
      setDjModeUi(Boolean(st.dj_mode),st);
    }else{
      // Still render meters/gradients, but preserve locally edited switch + prompt state.
      const localDsp=Boolean($('realtimeDsp50Mode')?.checked),localDj=Boolean($('realtimeDjMode')?.checked);
      renderDsp50State({...st,dsp50_mode:localDsp,dsp50_prompt:String($('realtimeDsp50Prompt')?.value||nativeRtState.dsp50Prompt||'')});
      setDjModeUi(localDj,{...st,dj_mode:localDj,dj_prompt:String($('realtimeDjPrompt')?.value||nativeRtState.djPrompt||'')});
    }
    const effectIntensity=$('realtimeEffectIntensity');
    if(effectIntensity&&!localOwn&&document.activeElement!==effectIntensity&&Number.isFinite(Number(st.effect_intensity))){
      effectIntensity.value=String(Math.round(Math.max(0,Math.min(1.60,Number(st.effect_intensity)))*100));
      updateRealtimeEffectIntensityLabel();
    }
    if($('realtimeLevelText'))$('realtimeLevelText').textContent=Number.isFinite(Number(st.output_db))?`${Number(st.output_db).toFixed(1)} dB`:'−∞ dB';
    if(st.running){
      nativeRtState.silenceTicks=Number(st.input_db)<-72?nativeRtState.silenceTicks+1:0;
      const route=$('realtimeRouteStatus'),loading=/^LOADING\b/i.test(String(st.message||''));
      if(route&&loading)route.textContent=String(st.message||'BUFFERING · MusicCLAP + StudioDSP');
      else if(route&&nativeRtState.silenceTicks>7)route.textContent=`LIVE route is open, but no input signal is detected (${Number(st.input_db).toFixed(1)} dB). Check that Spotify is playing; JoyMetric should route it to the internal driver automatically.`;
      else if(route&&nativeRtState.silenceTicks===0&&!String(route.textContent||'').startsWith('LIVE ·')){const buf=st.buffer_ms?` · ${Number(st.buffer_ms).toFixed(2)} ms block`:'';const mode=st.dj_mode?'CONTINUOUS DJ 15D':'15D FEATURES';route.textContent=`LIVE · ${st.input_label||'Input'} → ${mode} → STUDIO DSP → ${st.output_label||'Audio Out'}${st.playback_backend?` · ${st.playback_backend}`:''} · ${st.sample_rate||''} Hz${buf} · FULL-RATE HI-FI`;}
    }else nativeRtState.silenceTicks=0;
    const aiBox=$('realtimeAiController'),aiText=$('realtimeAiStatus'),limText=$('realtimeLimiterStatus');
    if(aiBox){aiBox.classList.toggle('ai-off',st.ai_enabled===false);aiBox.classList.toggle('ai-ready',Boolean(st.ai_enabled&&st.ai_ready));aiBox.classList.toggle('ai-loading',Boolean(st.ai_enabled&&!st.ai_ready));}
    if(aiText){const dev=st.ai_device?` · ${String(st.ai_device).toUpperCase()}`:'';const ms=Number(st.ai_inference_ms)>0?` · ${Number(st.ai_inference_ms).toFixed(0)} ms analysis`:'';const detail=String(st.ai_detail_model||'');const dconf=Number(st.ai_detail_confidence||0);const detailTag=detail?` · detail ${detail.split('/').pop()}${dconf>0?` ${Math.round(dconf*100)}%`:''}`:'';let label=String(st.ai_state||'Semantic AI starting');if(/^error\b/i.test(label))label='Semantic AI setup retrying · realtime DSP stays active';aiText.textContent=st.dj_mode?`Continuous DJ owns the 15D feature deck${dev}${ms}${detailTag}`:(st.ai_enabled===false?'AI adaptation off':`${label}${dev}${ms}${detailTag}`);}
    if(limText){const red=Number(st.limiter_reduction_db||0),comp=Number(st.master_comp_reduction_db||0),ag=Number(st.auto_gain_reduction_db||0),fill=Number(st.bridge_fill_ms||0),rec=Number(st.concealments||0),rep=Number(st.declick_repairs||0),outBuf=Number(st.output_buffer_ms||0),backend=String(st.dsp_backend||''),fg=Number(st.fidelity_residual_gain??1),fq=Number(st.fidelity_quality_score??1);const bridge=fill>0?` · bridge ${fill.toFixed(1)} ms`:'';const ob=outBuf>0?` · OUT ${outBuf.toFixed(1)} ms`:'';const recovered=rec>0?` · ${rec} ${rec===1?'smooth underrun recovery':'smooth underrun recoveries'}`:'';const repaired=rep>0?` · ${rep} isolated tick${rep===1?'':'s'} repaired`:'';const be=backend?` · ${backend}`:'';const agTxt=ag<-.03?`Auto gain ${ag.toFixed(1)} dB`:'Auto gain transparent';const compTxt=comp<-.03?`Bus comp ${comp.toFixed(1)} dB`:'Bus comp transparent';const fidelity=`Source fidelity ${Math.round(Math.max(0,Math.min(1,fq))*100)}%${fg<.995?` · residual ${Math.round(fg*100)}%`:''}`;limText.textContent=`${fidelity} · ${agTxt} · ${compTxt} · `+(red<-.05?`True-peak ${red.toFixed(1)} dB`:'True-peak transparent')+` · −1.6 dBTP${ob}${bridge}${be}${recovered}${repaired}`;}
    if(st.error&&$('realtimeRouteStatus'))$('realtimeRouteStatus').textContent=`Realtime error: ${sanitizeVisibleText(st.error)}`;
  }catch(_){}
}
function startNativeRealtimePolling(){
  clearInterval(nativeRtState.pollTimer);nativeRtState.pollTimer=setInterval(pollNativeRealtimeStatus,1000);pollNativeRealtimeStatus();
}
function drawRealtimeSpectrum(){if(document.body.classList.contains('perf')){const _n=performance.now();if(window._specLast&&_n-window._specLast<50){requestAnimationFrame(drawRealtimeSpectrum);return;}window._specLast=_n;}
  if(rtState.raf)cancelAnimationFrame(rtState.raf);const canvas=$('realtimeSpectrum');if(!canvas)return;
  const loop=()=>{
    rtState.raf=requestAnimationFrame(loop);const rect=canvas.getBoundingClientRect();if(rect.width<2||rect.height<2)return;
    const dpr=Math.min(2,devicePixelRatio||1),W=Math.round(rect.width*dpr),H=Math.round(rect.height*dpr);if(canvas.width!==W||canvas.height!==H){canvas.width=W;canvas.height=H;}
    const ctx=canvas.getContext('2d');ctx.clearRect(0,0,W,H);ctx.fillStyle='rgba(5,8,12,.88)';ctx.fillRect(0,0,W,H);
    const data=nativeRtState.lastStatus?.spectrum||[],pal=paletteFor('modified'),bins=data.length||56;
    for(let i=0;i<bins;i++){const v=data.length?Math.max(0,Math.min(1,Number(data[i]||0))):0,t=bins>1?i/(bins-1):0,rgb=mixRgb(pal.low,pal.high,t),bh=Math.max(1,Math.pow(v,.72)*H*.88);ctx.fillStyle=rgba(rgb,.12+.82*v);ctx.fillRect(i*W/bins,H-bh,Math.max(1,W/bins*.66),bh);}
  };loop();
}
function setupRealtimeFeatures(){
  if(!$('realtimeView'))return;
  // v31.1.2: do not enumerate WASAPI while this hidden view is booting.
  // The scan runs when the user actually opens Realtime, preventing concurrent
  // native scans with the Agentic view during application startup.
  startNativeRealtimePolling();
  $('realtimeBackBtn')?.addEventListener('click',()=>{showEditorView();setNavActive('home');});
  $('realtimeRefreshDevices')?.addEventListener('click',()=>refreshNativeRealtimeDevices({keepSelection:true,force:true}));
  $('realtimeLetsGoBtn')?.addEventListener('click',()=>{if(nativeRtState.running)stopNativeRealtime();else startNativeRealtime();});
  $('realtimeStopBtn')?.addEventListener('click',()=>stopNativeRealtime());
  const aiToggle=$('realtimeAiEnabled');
  if(aiToggle){try{const saved=localStorage.getItem('joymetricSemanticAiEnabled');if(saved!==null)aiToggle.checked=saved!=='0';}catch(_){}aiToggle.addEventListener('change',()=>{if($('realtimeDjMode')?.checked)return;try{localStorage.setItem('joymetricSemanticAiEnabled',aiToggle.checked?'1':'0');}catch(_){}queueNativeRealtimeControls();pollNativeRealtimeStatus();});}
  const djToggle=$('realtimeDjMode'),djPrompt=$('realtimeDjPrompt'),djApply=$('realtimeDjApply'),effectIntensity=$('realtimeEffectIntensity');
  const dsp50Toggle=$('realtimeDsp50Mode'),dsp50Prompt=$('realtimeDsp50Prompt'),dsp50Apply=$('realtimeDsp50Apply'),dsp50Intensity=$('realtimeDsp50Intensity');
  try{
    const savedPrompt=localStorage.getItem('joymetricDjPrompt');if(savedPrompt!==null&&djPrompt)djPrompt.value=savedPrompt;
    const saved50=localStorage.getItem('joymetricDsp50Prompt');if(saved50!==null&&dsp50Prompt)dsp50Prompt.value=saved50;
    const savedIntensity=Number(localStorage.getItem('joymetricDsp50IntensityV303'));if(Number.isFinite(savedIntensity)&&dsp50Intensity)dsp50Intensity.value=String(Math.max(35,Math.min(240,savedIntensity)));
    const savedEffect=Number(localStorage.getItem('joymetric15DEffectIntensityV3045'));if(Number.isFinite(savedEffect)&&effectIntensity)effectIntensity.value=String(Math.max(0,Math.min(160,savedEffect)));
    const savedPolish=localStorage.getItem('joymetricNeuralPolishV3048');if(savedPolish!==null&&neuralPolish)neuralPolish.checked=savedPolish==='1';
    const savedPolishWet=Number(localStorage.getItem('joymetricNeuralPolishWetV3048'));if(Number.isFinite(savedPolishWet)&&neuralPolishWet)neuralPolishWet.value=String(Math.max(0,Math.min(20,savedPolishWet)));
  }catch(_){}
  nativeRtState.djPrompt=String(djPrompt?.value||'');nativeRtState.dsp50Prompt=String(dsp50Prompt?.value||'');
  updateRealtimeEffectIntensityLabel();
  if($('realtimeDsp50IntensityValue'))$('realtimeDsp50IntensityValue').textContent=`${(Number(dsp50Intensity?.value||145)/100).toFixed(2)}×`;
  const pushDj=()=>{
    markRealtimeControlLocal();const on=Boolean(djToggle?.checked),prompt=String(djPrompt?.value||nativeRtState.djPrompt||'').trim();nativeRtState.djMode=on;nativeRtState.djPrompt=prompt;
    try{localStorage.setItem('joymetricDjPrompt',prompt);}catch(_){}
    setDjModeUi(on,{dj_mode:on,dj_prompt:prompt,ai_state:on?(prompt?'DJ Gradient target submitted · building local derivative field':'DJ Gradient waiting · enter a prompt'):'DJ Mode off · manual 15D controls available'});
    queueNativeRealtimeControls();
  };
  const pushDsp50=()=>{
    markRealtimeControlLocal();const on=Boolean(dsp50Toggle?.checked),prompt=String(dsp50Prompt?.value||nativeRtState.dsp50Prompt||'').trim(),intensity=Math.max(.35,Math.min(2.40,Number(dsp50Intensity?.value||145)/100));
    nativeRtState.dsp50Mode=on;nativeRtState.dsp50Prompt=prompt;nativeRtState.dsp50Intensity=intensity;
    if(on&&djToggle){djToggle.checked=false;nativeRtState.djMode=false;setDjModeUi(false,{dj_mode:false});}
    try{localStorage.setItem('joymetricDsp50Prompt',prompt);localStorage.setItem('joymetricDsp50IntensityV303',String(Math.round(intensity*100)));}catch(_){}
    setDsp50ModeUi(on,{dsp50_mode:on,dsp50_prompt:prompt,dsp50_intensity:intensity,ai_state:on?(prompt?'Auto DSP target compiled · measuring sparse live corrections':'Auto DSP waiting · enter a production prompt'):'Auto DSP off · manual/DJ controls available'});
    queueNativeRealtimeControls();
  };
  djPrompt?.addEventListener('input',()=>persistRealtimePrompt('dj',djPrompt));
  dsp50Prompt?.addEventListener('input',()=>persistRealtimePrompt('dsp50',dsp50Prompt));
  djToggle?.addEventListener('change',pushDj);djApply?.addEventListener('click',()=>{if(djToggle)djToggle.checked=true;persistRealtimePrompt('dj',djPrompt);pushDj();});djPrompt?.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();if(djToggle)djToggle.checked=true;persistRealtimePrompt('dj',djPrompt);pushDj();}});
  dsp50Toggle?.addEventListener('change',pushDsp50);dsp50Apply?.addEventListener('click',()=>{if(dsp50Toggle)dsp50Toggle.checked=true;persistRealtimePrompt('dsp50',dsp50Prompt);pushDsp50();});dsp50Prompt?.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();if(dsp50Toggle)dsp50Toggle.checked=true;persistRealtimePrompt('dsp50',dsp50Prompt);pushDsp50();}});
  dsp50Intensity?.addEventListener('input',()=>{const x=Math.max(.35,Math.min(2.40,Number(dsp50Intensity.value||145)/100));nativeRtState.dsp50Intensity=x;if($('realtimeDsp50IntensityValue'))$('realtimeDsp50IntensityValue').textContent=`${x.toFixed(2)}×`;try{localStorage.setItem('joymetricDsp50IntensityV303',String(Math.round(x*100)));}catch(_){}if(dsp50Toggle?.checked)queueNativeRealtimeControls();});
  effectIntensity?.addEventListener('input',()=>{
    markRealtimeControlLocal(1100);updateRealtimeEffectIntensityLabel();
    try{localStorage.setItem('joymetric15DEffectIntensityV3045',String(Math.round(readRealtimeEffectIntensity()*100)));}catch(_){}
    // Update the hidden mirror immediately for visual consistency; native audio
    // glides the same target on its own continuity trajectory.
    syncRealtimeParamMirror(computeRealtimeFeatureParams(readRealtimeFeatures()));
    queueNativeRealtimeControls();
  });
  restoreRealtimeFeatures();syncRealtimeParamMirror(computeRealtimeFeatureParams(readRealtimeFeatures()));
  document.querySelectorAll('[data-rt-feature]').forEach(input=>{
    input.addEventListener('input',()=>applyRealtimeFeatureMacros(input.dataset.rtFeature||'feature'));
    input.addEventListener('dblclick',()=>{input.value='0';applyRealtimeFeatureMacros(input.dataset.rtFeature||'feature');});
  });
  $('rtFeatureZeroBtn')?.addEventListener('click',()=>zeroRealtimeFeatures({announce:true}));
  $('realtimeResetBtn')?.addEventListener('click',resetRealtimeDSP);
  updateRealtimeParamLabels();updateRealtimeFeatureLabels();drawRealtimeSpectrum();
}



const agenticDjState={pollTimer:null,last:null,posting:false,locked:false};
function showAgenticDJView(){
  mountRealtimeFxInAgentic();
  clearLibraryCoverBindings();
  $('libraryView')?.classList.add('hidden');$('realtimeView')?.classList.add('hidden');$('workArea')?.classList.add('hidden');
  $('agenticView')?.classList.remove('hidden');state.activeLibrary=null;setNavActive('agentic');
  refreshAgenticOutputs();pollAgenticDJStatus();startAgenticPulse();   /* v31.30.29 canonical accent: live color */
}
function drawAgenticLiveWave(canvasId,wave,accent='A'){
  const c=$(canvasId);if(!c)return;const r=c.getBoundingClientRect(),dpr=Math.min(2,devicePixelRatio||1),W=Math.max(10,Math.round((r.width||760)*dpr)),H=Math.max(10,Math.round((r.height||112)*dpr));if(c.width!==W)c.width=W;if(c.height!==H)c.height=H;
  const ctx=c.getContext('2d');ctx.clearRect(0,0,W,H);ctx.fillStyle='#070910';ctx.fillRect(0,0,W,H);ctx.strokeStyle='rgba(255,255,255,.045)';for(let i=1;i<8;i++){const x=i*W/8;ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,H);ctx.stroke();}
  if(!Array.isArray(wave)||!wave.length){ctx.fillStyle='#374052';ctx.font=`${11*dpr}px system-ui`;ctx.fillText(accent==='A'?'LIVE PCM WAITING':'LOOP DECK EMPTY',16*dpr,H/2);return;}
  ctx.strokeStyle=accent==='A'?'rgba(94,143,255,.96)':'rgba(154,102,255,.96)';ctx.lineWidth=Math.max(1,dpr);ctx.beginPath();for(let i=0;i<wave.length;i++){const x=i/Math.max(1,wave.length-1)*W,a=Math.max(.01,Number(wave[i]||0))*H*.43;ctx.moveTo(x,H/2-a);ctx.lineTo(x,H/2+a);}ctx.stroke();
}
async function agenticPost(url,body={}){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||`HTTP ${r.status}`);return j;}
let unifiedPromptTimer=null;
function scheduleUnifiedPromptUpdate(){
  syncUnifiedPrompt('agentic');clearTimeout(unifiedPromptTimer);
  unifiedPromptTimer=setTimeout(()=>{
    if(!agenticDjState.last?.running)return;
    agenticPost('/api/agentic/prompt',{prompt:String($('agenticPrompt')?.value||''),autonomy:$('agenticAutonomy')?.value||'autopilot',creativity:Number($('agenticCreativity')?.value||35)/100,effect_intensity:readRealtimeEffectIntensity()}).then(pollAgenticDJStatus).catch(e=>{if($('agenticStatus'))$('agenticStatus').textContent=`Prompt update: ${sanitizeVisibleText(e.message)}`;});
  },520);
}
async function refreshAgenticOutputs(force=false){
  const sel=$('agenticOutput');if(!sel)return;try{const r=await fetch(`/api/agentic/devices${force?'?force=1':''}`,{cache:'no-store'}),j=await r.json();if(!r.ok)throw new Error(j.error||`HTTP ${r.status}`);const old=sel.value;sel.innerHTML='<option value="">Choose physical Audio Out…</option>'+(j.outputs||[]).map(o=>`<option value="${String(o.id).replace(/"/g,'&quot;')}">${sanitizeVisibleText(o.label||o.name||o.id)}</option>`).join('');if(old&&[...sel.options].some(o=>o.value===old))sel.value=old;else if(j.default_output_id!=null)sel.value=String(j.default_output_id);if($('agenticStatus')){if(j.scan_warning)$('agenticStatus').textContent=`Audio scan recovered safely · ${sanitizeVisibleText(j.scan_warning)}`;else if(!j.spotify_ready)$('agenticStatus').textContent="Spotify session not visible yet · start playback, then press LET\'S GO.";}}catch(e){sel.innerHTML=`<option value="">${sanitizeVisibleText(e.message)}</option>`;}
}
function agenticSetBar(id,pct){const e=$(id);if(e)e.style.width=`${Math.max(0,Math.min(100,Number(pct)||0))}%`;}
// v31.10 live deck sliders.  Each entry: [controlKey, uiScale, mixerEffectiveKey|null, format, labelId|null].
// The mixer key mirrors the *audible* smoothed value from the audio callback, so
// faders now dance with the actual performance instead of sitting at zero.
const AGENTIC_SLICE_MODES=['OFF','GROOVE','FILL','CALL','ACCEL','GHOST'];
const AGENTIC_CONTROL_DEFS={
  agenticALow:['a_low_db',1,'a_low_db','db','agenticALowVal'],
  agenticAMid:['a_mid_db',1,'a_mid_db','db','agenticAMidVal'],
  agenticAHigh:['a_high_db',1,'a_high_db','db','agenticAHighVal'],
  agenticAFilter:['a_filter',100,'a_filter','pct','agenticAFilterVal'],
  agenticAGain:['a_gain',100,'a_gain','pct','agenticAGainVal'],
  agenticBLow:['b_low_db',1,'b_low_db','db','agenticBLowVal'],
  agenticBMid:['b_mid_db',1,'b_mid_db','db','agenticBMidVal'],
  agenticBHigh:['b_high_db',1,'b_high_db','db','agenticBHighVal'],
  agenticBFilter:['b_filter',100,'b_filter','pct','agenticBFilterVal'],
  agenticBGain:['b_gain',100,'b_gain','pct','agenticBGainVal'],
  agenticBLayerCtl:['b_layer',100,'b_layer','pct','agenticBLayerVal'],
  agenticBTexture:['b_texture',100,'b_texture','pct','agenticBTextureVal'],
  agenticBTransient:['b_transient',100,'b_transient','pct','agenticBTransientVal'],
  agenticBSlicer:['b_slicer_mix',100,'b_slicer_mix','pct','agenticBSlicerVal'],
  agenticBSliceMode:['b_slice_mode',1,'b_slice_mode','mode','agenticBSliceModeVal'],
  agenticBFX:['b_fx',100,'b_fx','pct','agenticBFXVal'],
  agenticCrossfaderInput:['crossfader',100,'crossfader','none',null],
  agenticReverb:['reverb',100,'reverb','pct','agenticReverbVal'],
  agenticEcho:['echo',100,'echo','pct','agenticEchoVal'],
  agenticFxRiser:['noise_riser',100,'noise_riser','pct','agenticFxRiserVal'],
  agenticFxReverse:['fx_reverse_swell',100,'fx_reverse_swell','pct','agenticFxReverseVal'],
  agenticFxRush:['fx_snare_rush',100,'fx_snare_rush','pct','agenticFxRushVal'],
  agenticFxDrive:['drum_drive',100,'drum_drive','pct','agenticFxDriveVal'],
  agenticFxDouble:['doubletime',100,'doubletime','pct','agenticFxDoubleVal'],
  agenticFxGate:['fx_gate',100,'rhythm_gate','pct','agenticFxGateVal'],
  agenticFxGateDiv:['fx_gate_div',1,'gate_div','div','agenticFxGateDivVal'],
  agenticFxEchoSend:['fx_echo_send',100,'beat_echo','pct','agenticFxEchoSendVal'],
  agenticFxEchoFb:['fx_echo_feedback',100,'echo_feedback','pct','agenticFxEchoFbVal'],
  agenticFxEchoBeats:['fx_echo_beats',1,'echo_beats','beats','agenticFxEchoBeatsVal'],
  agenticFxRackWet:['fx_rack_wet',100,'rack_wet','pct','agenticFxRackWetVal'],
  agenticFxWidth:['fx_width',100,'width','pct','agenticFxWidthVal'],
  agenticFxDuck:['fx_duck',100,'duck','pct','agenticFxDuckVal'],
  agenticFxPunch:['perf_punch',100,null,'pct','agenticFxPunchVal'],
  agenticFxEnergy:['perf_energy',100,null,'pct','agenticFxEnergyVal'],
  agenticFxAir:['perf_air',100,null,'pct','agenticFxAirVal'],
  agenticFxClarity:['perf_clarity',100,null,'pct','agenticFxClarityVal'],
  agenticFxBassTight:['bass_tighten',100,null,'pct','agenticFxBassTightVal'],
  agenticFxPump:['fx_pump',100,'fx_pump','pct','agenticFxPumpVal'],
  agenticFxBassCut:['fx_bass_cut',100,'fx_bass_cut','pct','agenticFxBassCutVal'],
  agenticFxRushAccel:['fx_rush_accel',100,'fx_rush_accel','pct','agenticFxRushAccelVal'],
  agenticFxRedrum:['fx_redrum',100,'fx_redrum','pct','agenticFxRedrumVal'],
  agenticFxRedrumPat:['fx_redrum_pattern',1,'fx_redrum_pattern','rpat','agenticFxRedrumPatVal'],
  agenticFxSwing:['fx_swing',100,'fx_swing','pct','agenticFxSwingVal'],
  agenticFxBassSyn:['fx_bass_synth',100,'fx_bass_synth','pct','agenticFxBassSynVal'],
  agenticFxBassPat:['fx_bass_pattern',1,'fx_bass_pattern','bpat','agenticFxBassPatVal'],
  agenticFxStab:['fx_stab',100,'fx_stab','pct','agenticFxStabVal'],
  agenticFxRVar:['fx_redrum_var',100,'fx_redrum_var','pct','agenticFxRVarVal'],
  agenticFxFill:['fx_redrum_fill',100,'fx_redrum_fill','pct','agenticFxFillVal'],
};
const AGENTIC_REDRUM_PATTERNS=['FLOOR','HALF','BREAK','UPBEAT'];
const AGENTIC_BASS_PATTERNS=['PULSE','DRIVE','SUB','OCT'];
const AGENTIC_KEY_NAMES=['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'];
const agenticCtl={guards:{},timers:{}};
function agenticFmt(fmt,num){
  if(fmt==='db')return `${Number(num||0).toFixed(1)} dB`;
  if(fmt==='pct')return `${Math.round(Number(num||0)*100)}%`;
  if(fmt==='mode')return AGENTIC_SLICE_MODES[Math.max(0,Math.min(5,Math.round(Number(num||0))))]||'OFF';
  if(fmt==='div')return `1/${Math.max(1,Math.round(Number(num||2)))}`;
  if(fmt==='rpat')return AGENTIC_REDRUM_PATTERNS[Math.max(0,Math.min(3,Math.round(Number(num||0))))]||'FLOOR';
  if(fmt==='bpat')return AGENTIC_BASS_PATTERNS[Math.max(0,Math.min(3,Math.round(Number(num||0))))]||'PULSE';
  if(fmt==='beats'){const v=Number(num||0.5);return v===0.5?'1/2':(v===0.25?'1/4':(v===0.125?'1/8':`${v}`));}
  return String(num);
}
function setAgenticRangeFill(e){
  const min=Number(e.min||0),max=Number(e.max||100),v=Number(e.value||0);
  const pct=max>min?Math.round(((v-min)/(max-min))*100):0;
  e.style.setProperty('--v',`${pct}%`);
}
function syncAgenticControlInputs(c,mix){
  const now=performance.now();mix=mix||{};c=c||{};
  Object.entries(AGENTIC_CONTROL_DEFS).forEach(([id,[key,scale,mixKey,fmt,labelId]])=>{
    const e=$(id);if(!e)return;
    let raw=(mixKey!=null&&mix[mixKey]!=null)?mix[mixKey]:c[key];
    if(raw==null)raw=key.endsWith('_gain')?1:(key==='fx_gate_div'?2:(key==='fx_echo_beats'?0.5:0));
    let num=raw;
    if(fmt==='mode'&&typeof raw==='string'){const idx=AGENTIC_SLICE_MODES.indexOf(raw);num=idx>=0?idx:0;}
    num=Number(num)||0;
    const guarded=(agenticCtl.guards[id]||0)>now||e.matches(':active');
    if(!guarded){e.value=String(num*Number(scale||1));setAgenticRangeFill(e);}
    if(labelId&&!guarded){const lb=$(labelId);if(lb)lb.textContent=agenticFmt(fmt,num);}
  });
}
function renderTransitionMatrix(st){
  const t=st.transition||{},m=st.musical||{},mix=(st.realtime||{}).agentic_mixer||{},a=st.analysis||{};
  const braking=Boolean(mix.brake_active);
  const active=Boolean(t.active)||braking;
  const roleIcon=r=>r==='RISE'?'↗':(r==='FALL'?'↘':'→');
  const setTxt=(id,v)=>{const e=$(id);if(e)e.textContent=v;};
  setTxt('transName',braking?`${sanitizeVisibleText(String(mix.brake_mode||'BRAKE'))} · record winding`:(active?sanitizeVisibleText(String(t.name||'TRANSITION')):'NO ACTIVE TRANSITION'));
  const bf=Number(t.beat_float||0),sb=Number(t.start_beat||0),eb=Number(t.end_beat||0);
  const prog=braking?Number(mix.brake_u||0):(active&&eb>sb?Math.max(0,Math.min(1,(bf-sb)/(eb-sb))):0);
  if($('transProgress'))$('transProgress').style.width=`${Math.round(prog*100)}%`;
  setTxt('transBeats',active?`beat ${Math.max(0,bf-sb).toFixed(1)} of ${(eb-sb).toFixed(0)} · depth ${Math.round(Number(t.depth||0)*100)}%`:'stagecraft engine armed · waiting for a musical boundary');
  const ra=active?String(t.a_role||'HOLD'):'HOLD',rb=active?String(t.b_role||'HOLD'):'HOLD';
  setTxt('transRoleAIcon',roleIcon(ra));setTxt('transRoleALabel',ra);
  setTxt('transRoleBIcon',roleIcon(rb));setTxt('transRoleBLabel',rb);
  const roleCls=(el,r)=>{if(!el)return;el.classList.toggle('role-rise',r==='RISE');el.classList.toggle('role-fall',r==='FALL');el.classList.toggle('role-active',active);};
  roleCls($('transRoleA'),ra);roleCls($('transRoleB'),rb);
  const aLevel=Math.max(0,Math.min(1,Number(mix.a_gain??1)*(0.25+0.75*Number(a.energy||0))));
  const bLevel=Math.max(0,Math.min(1,Number(mix.b_layer||0)*1.8+Number(mix.crossfader||0)));
  if($('transMeterA'))$('transMeterA').style.height=`${Math.round(aLevel*100)}%`;
  if($('transMeterB'))$('transMeterB').style.height=`${Math.round(bLevel*100)}%`;
  setTxt('musPhrase',`${(Number(m.phrase_beat||0)+1)|0}/${m.phrase_beats||16} · bar ${(Number(m.phrase_bar||0)+1)|0}/${m.bars_total||4} · ${Math.round(Number(m.confidence||0)*100)}%`);
  if($('musPhraseBar'))$('musPhraseBar').style.width=`${Math.round(Number(m.progress||0)*100)}%`;
  setTxt('musTension',`${Math.round(Number(m.tension||0)*100)}% · hunger ${Math.round(Number(m.tension_hunger||0)*100)}%`);
  if($('musTensionBar'))$('musTensionBar').style.width=`${Math.round(Number(m.tension||0)*100)}%`;
  setTxt('musRelease',`${Math.round(Number(m.release_readiness||0)*100)}%${m.refractory?' · refractory':''}`);
  setTxt('musArc',`${m.arc||'HOLD'} ${Number(m.arc_bias||0)>=0?'+':''}${Number(m.arc_bias||0).toFixed(2)}`);
  setTxt('musKey',sanitizeVisibleText(String(m.stable_key||'—')));
  setTxt('musCamelot',`${sanitizeVisibleText(String(m.camelot||'—'))} · mod ${Math.round(Number(m.modulation||0)*100)}%`);
  setTxt('musRoleHint',sanitizeVisibleText(String(m.deckb_role_hint||'—')));
  const since=Number(m.beats_since_big||0);
  setTxt('musPacing',since>=9000?'—':`${since} beats · pen ${Math.round(Number(m.big_penalty||0)*100)}%`);
  setTxt('musSection',sanitizeVisibleText(String(m.section_boundary||m.section_now||'—')));
  const stb=Number(m.beats_to_section||0);
  setTxt('musSectionBeats',stb>0?`${stb} beats to ${sanitizeVisibleText(String(m.section_next||'—'))}`:'holding section');
  const rx=st.remix||{};
  setTxt('musRemix',rx.enabled?`ON · ${Math.round(Number(rx.intensity||0)*100)}% · ${sanitizeVisibleText(String(rx.pattern||'FLOOR'))}`:'OFF (raise creativity)');
  const rxLeft=Math.max(0,Number(rx.until_beat||0)-Number(t.beat_float||0));
  setTxt('musRemixSection',rx.enabled?`${sanitizeVisibleText(String(rx.section||'—'))} · ${rxLeft.toFixed(0)} beats left`:'—');
  const ctl=st.controller||{};
  const broot=AGENTIC_KEY_NAMES[Math.max(0,Math.min(11,Math.round(Number(mix.fx_bass_root??ctl.fx_bass_root??9))))]||'A';
  const bconf=Number(ctl.fx_bass_conf||0);
  setTxt('musBassKey',bconf>0.02?`${broot} · ${Math.round(bconf*100)}% · ${sanitizeVisibleText(String(mix.bass_pattern||'PULSE'))}`:'—');
  setTxt('musWeave',braking?`${sanitizeVisibleText(String(mix.brake_mode||'—'))} · ${Math.round(Number(mix.brake_u||0)*100)}%`:'armed');
  const sg=m.song||{},lg=m.legality||{};
  const rep=(sg.repeat_of!=null)?`repeat of bar ${sg.repeat_of} · run ${sg.repeat_run} · sim ${Math.round(Number(sg.repeat_sim||0)*100)}%`:'learning form…';
  setTxt('musSong',`${Number(sg.bars_known||0)} bars · L${sg.phrase_len||16} (${Math.round(Number(sg.phrase_len_conf||0)*100)}%) · ${rep}`);
  const pr=(sg.predicted||[])[0];
  const curBeat=Number(t.beat_float||0);
  setTxt('musPredicted',pr?`${sanitizeVisibleText(String(pr.type||'SHIFT'))} @ beat ${pr.beat} (+${Math.max(0,Number(pr.beat)-curBeat).toFixed(0)}) · ${Math.round(Number(pr.conf||0)*100)}% · ${sanitizeVisibleText(String(pr.src||''))}`:'—');
  const bl=(lg.landings||[])[0];
  setTxt('musLanding',(lg.best_landing!=null&&bl)?`beat ${lg.best_landing} (+${Math.max(0,Number(lg.best_landing)-curBeat).toFixed(0)}) · score ${Number(bl.score||0).toFixed(2)} · ${sanitizeVisibleText((bl.why||[]).join(', '))}`:'none in horizon');
  const br=lg.best_rise;
  setTxt('musRise',br?`start beat ${br.beat} → land ${br.landing} · depth cap ${Math.round(Number(br.depth_cap||1)*100)}%`:'no legal rise now');
  setTxt('musReasons',(lg.reasons||[]).length?sanitizeVisibleText((lg.reasons||[]).join(' · ')):'all gestures open');
  const hs=['CLUB','HIPHOP','DNB','SPARSE'][Math.max(0,Math.min(3,Number(mix.fx_hat_style||0)))]||'CLUB';
  setTxt('musHats',`${hs} · source density ${Math.round(Number(mix.hat_density??1)*100)}% · humanize ${Math.round(Number((st.controller||{}).fx_hat_var||0)*100)}%`);
}
function renderAgenticPolicy(st){
  const p=st.policy||{},a=st.analysis||{},rt=st.realtime||{},mix=rt.agentic_mixer||{},brain=st.brain||{},dec=st.decision||{},scene=st.scene||{},mus=st.musical||{},tr=st.transition||{};const box=$('agenticPolicy');if(box)box.innerHTML=`<div class="agentic-policy-grid"><span><small>STYLE</small><b>${sanitizeVisibleText(p.style||'balanced')}</b></span><span><small>FX PROFILE</small><b>${sanitizeVisibleText(p.fx_profile||'balanced')}</b></span><span><small>FX PRESENCE</small><b>${Math.round(Number(p.fx_presence||0)*100)}%</b></span><span><small>SCENE</small><b>${sanitizeVisibleText(scene.stage||'LISTENING')}</b></span><span><small>RISER</small><b>${Math.round(Number(mix.noise_riser||0)*100)}%</b></span><span><small>REVERSE</small><b>${Math.round(Number(mix.fx_reverse_swell||0)*100)}%</b></span><span><small>SNARE RUSH</small><b>${Math.round(Number(mix.fx_snare_rush||0)*100)}%</b></span><span><small>IMPACT</small><b>${Math.round(Number(mix.impact_strength||0)*100)}%</b></span><span><small>FX RACK</small><b>${Math.round(Number(mix.rack_wet||0)*100)}%</b></span><span><small>BEAT ECHO</small><b>${Math.round(Number(mix.beat_echo||0)*100)}%</b></span><span><small>TRANS GATE</small><b>${Math.round(Number(mix.rhythm_gate||0)*100)}%</b></span><span><small>WET WIDTH</small><b>${Math.round(Number(mix.width||0)*100)}%</b></span><span><small>LOOKAHEAD</small><b>${Number(rt.agentic_lookahead_sec||p.lookahead_sec||15).toFixed(1)} s</b></span><span><small>BUFFER</small><b>${Math.round(Number(st.lookahead_fill||0)*100)}%</b></span><span><small>NEURAL WINDOW</small><b>${brain.window_sec?Number(brain.window_sec).toFixed(1)+' s':'warming'}</b></span><span><small>MEMORY</small><b>${Number(st.memory_depth||0)} / 24</b></span><span><small>NEURAL BRAIN</small><b>${brain.ready?'CLAP AUDIO LIVE':'FALLBACK'}</b></span><span><small>DECISION</small><b>${sanitizeVisibleText(dec.action||'NO_ACTION')}</b></span><span><small>CRITIC</small><b>${Number(dec.score||0).toFixed(2)}</b></span><span><small>BPM CONF</small><b>${Math.round(Number(a.bpm_confidence||0)*100)}%</b></span><span><small>LOW-END GUARD</small><b>${Math.round(Number(p.low_end_protection||0)*100)}%</b></span><span><small>ACTION DENSITY</small><b>${Math.round(Number(p.action_density||0)*100)}%</b></span><span><small>AGENT AUTHORITY</small><b>${Number(p.agentic_authority||1).toFixed(1)}×</b></span><span><small>TEMP SEGMENTS</small><b>${Number((st.future_segments||[]).length)}</b></span><span><small>QUEUE</small><b>${Number((st.performance_queue||[]).length)}</b></span><span><small>PARAM FAMILIES</small><b>${Number(st.parameterized_family_count||24)}</b></span><span><small>DECK B LAYER</small><b>${Math.round(Number(mix.b_layer||0)*100)}%</b></span><span><small>DECK B MODE</small><b>${sanitizeVisibleText(mix.deckb_mode||'FULL')}</b></span><span><small>SLICER</small><b>${Math.round(Number(mix.b_slicer_mix||0)*100)}% · ${sanitizeVisibleText(mix.b_slice_mode||'OFF')}</b></span><span><small>B REMIX FX</small><b>${Math.round(Number(mix.b_fx||0)*100)}%</b></span><span><small>B HARM FIT</small><b>${Math.round(Number(mix.loop_harmonic_match||0)*100)}%</b></span><span><small>B RHYTHM FIT</small><b>${Math.round(Number(mix.loop_rhythm_match||0)*100)}%</b></span><span><small>B SALIENCE</small><b>${Math.round(Number(mix.loop_salience||0)*100)}%</b></span><span><small>B QUALITY GATE</small><b>${Math.round(Number(mix.loop_quality_gate||0)*100)}%</b></span><span><small>B SHIFT RATE</small><b>${Number(p.deckb_shift_multiplier||1).toFixed(1)}×</b></span><span><small>B ROLE</small><b>${sanitizeVisibleText(mix.loop_role||'BALANCED')}</b></span><span><small>CO-MIX ROLE</small><b>${sanitizeVisibleText(mix.comix_role||mix.loop_role||'BALANCED')}</b></span><span><small>B PHASE ALIGN</small><b>${Number(mix.loop_micro_align_ms||0).toFixed(1)} ms</b></span><span><small>B SPECTRAL SLOT</small><b>L${Math.round(Number(mix.comix_low_keep||0)*100)} · M${Math.round(Number(mix.comix_body_keep||0)*100)} · H${Math.round(Number(mix.comix_air_keep||0)*100)}</b></span><span><small>B CENTER KEEP</small><b>${Math.round(Number(mix.comix_center_keep||1)*100)}%</b></span><span><small>DIRECTOR PLAN</small><b>${sanitizeVisibleText((st.director_selected||{}).name||'LISTENING')}</b></span><span><small>CANDIDATES</small><b>${Number((st.director_candidates||[]).length)}</b></span><span><small>AUTOMATION</small><b>${Number((st.automation_lanes||[]).length)} lanes</b></span><span><small>PERF MEMORY</small><b>${Number((st.performance_memory||[]).length)} events</b></span><span><small>BEAT PHASE</small><b>${Number(st.beat_index||0)} · ${Math.round(Number(st.beat_phase||0)*100)}%</b></span><span><small>PHRASE</small><b>${(Number(mus.phrase_beat||0)+1)|0}/${mus.phrase_beats||16} · ${Math.round(Number(mus.confidence||0)*100)}%</b></span><span><small>TENSION</small><b>${Math.round(Number(mus.tension||0)*100)}%</b></span><span><small>ENERGY ARC</small><b>${sanitizeVisibleText(mus.arc||'HOLD')}</b></span><span><small>SECTION</small><b>${sanitizeVisibleText(mus.section_boundary||mus.section_now||'—')}</b></span><span><small>KEY · CAMELOT</small><b>${sanitizeVisibleText(mus.stable_key||'—')} · ${sanitizeVisibleText(mus.camelot||'—')}</b></span><span><small>TRANSITION</small><b>${tr.active?sanitizeVisibleText(tr.name||'ACTIVE'):'—'}</b></span><span><small>REMIX</small><b>${(st.remix||{}).enabled?sanitizeVisibleText(String((st.remix||{}).section||'ON')):'OFF'}</b></span><span><small>FX GRID</small><b>${mix.fx_grid_locked?`BEAT-LOCKED · ${Math.round(Number(mix.grid_conf||0)*100)}%`:'FREE'}</b></span><span><small>DSP CORE</small><b>${mix.rust_core?'RUST · NATIVE':'PYTHON'}</b></span><span><small>DRUM AI</small><b>${(st.drum_ai||{}).source?sanitizeVisibleText(String((st.drum_ai||{}).source)+' · '+String((st.drum_ai||{}).hits||0)+' hits'):'—'}</b></span><span><small>DRUM KIT</small><b>${sanitizeVisibleText(String((st.drum_ai||{}).kit||'—'))}</b></span><span><small>SYNTH AI</small><b>${sanitizeVisibleText(String((st.synth_ai||{}).state||'—')+((st.synth_ai||{}).kind?(' · '+String((st.synth_ai||{}).kind)):''))}</b></span><span><small>LOOPS</small><b>${(mix.b_dedrum||0)>0.3?'DRUM-FREE':'FULL'}${(mix.b_ai_mix||0)>0.05?(' · SYNTH '+Math.round(Number(mix.b_ai_mix||0)*100)+'%'):''}</b></span><span><small>LEGAL LANDING</small><b>${(mus.legality||{}).best_landing!=null?('beat '+(mus.legality||{}).best_landing):'—'}</b></span><span><small>OVERRIDES</small><b>${Object.keys(st.overrides||{}).length||'—'}</b></span><span><small>MODE</small><b>${st.manual?'MANUAL':(st.locked?'LOCKED':(st.approved?'ACTIVE':'WAITING APPROVAL'))}</b></span></div>`;
  const tl=$('agenticTimeline');if(tl)tl.innerHTML=(st.timeline||[]).map(x=>`<div class="agentic-action"><span>${x.beat!=null?'BEAT '+Number(x.beat).toFixed(0):'BAR '+Number(x.bar||0).toFixed(0)}</span><b>${sanitizeVisibleText(x.kind||'ACTION')}</b><em>${sanitizeVisibleText(x.deck||'')}</em><p><strong>${sanitizeVisibleText(String(x.value??''))}</strong><br>${sanitizeVisibleText(x.rationale||'')}</p></div>`).join('')||'<div class="agentic-policy-empty">Building beat lock and musical context…</div>';
  $('agenticApprove')?.classList.toggle('active',Boolean(st.approved));$('agenticLock')?.classList.toggle('active',Boolean(st.locked));$('agenticTakeControl')?.classList.toggle('active',Boolean(st.manual));
}
async function pollAgenticDJStatus(){
  /* v31.30.14 */ const perfModeActive=(st)=>{try{const on=!!(st&&(st.audible_started||(st.realtime&&st.realtime.running)));if(document.body.classList.contains('perf')!==on){document.body.classList.toggle('perf',on);console.info('[ui] performance mode',on?'ON (live session: blur/backdrop off, theme 10 fps, spectrum 20 fps)':'OFF');}}catch(e){}};

  try{const r=await fetch('/api/agentic/status',{cache:'no-store'}),st=await r.json();agenticDjState.last=st;perfModeActive(st);const a=st.analysis||{},m=st.media||{},rt=st.realtime||{},mix=rt.agentic_mixer||{},c=st.controller||{};
    if($('agenticStatus'))$('agenticStatus').textContent=st.error?`ERROR · ${sanitizeVisibleText(st.error)}`:(st.message||'Realtime VocalWeave');
    if($('agenticProgressBar'))$('agenticProgressBar').style.width=rt.running?`${Math.max(4,Math.round(Number(st.lookahead_fill||0)*100))}%`:'0%';
    if($('agenticATitle'))$('agenticATitle').textContent=m.available?`${m.artist?m.artist+' · ':''}${m.title||'Spotify'}`:'Waiting for Spotify media session';
    if($('agenticABadge')){$('agenticABadge').textContent=rt.running?'LIVE':(m.available?'READY':'OFFLINE');$('agenticABadge').classList.toggle('ready',Boolean(rt.running||m.available));}
    if($('agenticABpm'))$('agenticABpm').textContent=a.bpm?Number(a.bpm).toFixed(1):'—';if($('agenticAKey'))$('agenticAKey').textContent=a.key||'—';if($('agenticABar'))$('agenticABar').textContent=st.history?.length?String(st.history[st.history.length-1]?.bar??'—'):(a.bpm?'LIVE':'—');if($('agenticAEnergy'))$('agenticAEnergy').textContent=a.energy!=null?`${Math.round(Number(a.energy)*100)}%`:'—';drawAgenticLiveWave('agenticAWave',a.waveform||[],'A');
    const loopReady=Boolean(mix.loop_ready);if($('agenticBBadge')){$('agenticBBadge').textContent=loopReady?'LOOPING':'EMPTY';$('agenticBBadge').classList.toggle('ready',loopReady);}if($('agenticBLoop'))$('agenticBLoop').textContent=loopReady?`${Number(mix.loop_beats||0).toFixed(1)} beats`:'—';if($('agenticBAge'))$('agenticBAge').textContent=loopReady?`${Number(mix.loop_age_sec||0).toFixed(1)} s`:'—';if($('agenticBXfade'))$('agenticBXfade').textContent=`${Math.round(Number(mix.crossfader||c.crossfader||0)*100)}% B`;if($('agenticBCorr'))$('agenticBCorr').textContent=loopReady?Number(mix.correlation||0).toFixed(2):'—';if($('agenticBLayer'))$('agenticBLayer').textContent=`${Math.round(Number(mix.b_layer||0)*100)}%`;if($('agenticBMode'))$('agenticBMode').textContent=sanitizeVisibleText(mix.deckb_mode||'FULL');drawAgenticLiveWave('agenticBWave',loopReady?(a.waveform||[]):[],'B');
    if($('agenticCrossfader'))$('agenticCrossfader').style.left=`${Math.max(0,Math.min(100,Number(mix.crossfader??c.crossfader??0)*100))}%`;if($('agenticCtrlBar'))$('agenticCtrlBar').textContent=a.bpm?`${Number(a.bpm).toFixed(1)} BPM`:'BEAT LOCK';syncAgenticControlInputs(c,mix);
    agenticSetBar('agenticStemVocal',Number(a.vocal_activity||0)*100);agenticSetBar('agenticStemDrums',Number(a.drums_activity||0)*100);agenticSetBar('agenticStemBass',Number(a.bass_activity||0)*100);agenticSetBar('agenticStemMelody',Number(a.melody_activity||0)*100);
    if($('agenticInputDb'))$('agenticInputDb').textContent=`${Number(rt.input_db??-120).toFixed(1)} dB`;if($('agenticOutputDb'))$('agenticOutputDb').textContent=`${Number(rt.output_db??-120).toFixed(1)} dB`;if($('agenticLimiter'))$('agenticLimiter').textContent=`${Number(rt.limiter_reduction_db||0).toFixed(2)} dB`;if($('agenticXruns'))$('agenticXruns').textContent=String(rt.xruns||0);renderAgenticPolicy(st);renderTransitionMatrix(st);
  }catch(e){if($('agenticStatus'))$('agenticStatus').textContent=`Status error: ${sanitizeVisibleText(e.message)}`;}
}
function setupAgenticDJ(){
  if(!$('agenticView'))return;$('agenticBackBtn')?.addEventListener('click',()=>{showEditorView();setNavActive('home');});
  document.querySelectorAll('[data-agentic-prompt]').forEach(b=>b.addEventListener('click',()=>{$('agenticPrompt').value=b.dataset.agenticPrompt||'';scheduleUnifiedPromptUpdate();}));
  $('agenticCreativity')?.addEventListener('input',e=>{if($('agenticCreativityValue'))$('agenticCreativityValue').textContent=`${e.target.value}%`;scheduleUnifiedPromptUpdate();});
  let _weaveNoiseTimer=null;$('agenticWeaveNoise')?.addEventListener('input',e=>{const v=Number(e.target.value)/100;if($('agenticWeaveNoiseValue'))$('agenticWeaveNoiseValue').textContent=v.toFixed(2);clearTimeout(_weaveNoiseTimer);_weaveNoiseTimer=setTimeout(()=>{agenticPost('/api/agentic/weave',{noise:v}).then(j=>{if($('agenticWeaveNoiseValue'))$('agenticWeaveNoiseValue').textContent=Number(j.noise).toFixed(2);if($('agenticWeavePlaying'))$('agenticWeavePlaying').textContent=`${j.playing!=null?`playing ${Number(j.playing).toFixed(2)}`:''}${j.queued?` · ${j.queued} queued`:''}`;}).catch(()=>{});},150);});
  setInterval(()=>{fetch('/api/agentic/weave').then(r=>r.json()).then(j=>{if(!j||!$('agenticWeavePlaying'))return;$('agenticWeavePlaying').textContent=`${j.playing!=null?`playing ${Number(j.playing).toFixed(2)}`:''}${j.queued?` · ${j.queued} queued`:''}`;}).catch(()=>{});},2000);
  let _weaveCfgTimer=null;$('agenticWeaveCfg')?.addEventListener('input',e=>{const v=Number(e.target.value)/10;if($('agenticWeaveCfgValue'))$('agenticWeaveCfgValue').textContent=v.toFixed(1);clearTimeout(_weaveCfgTimer);_weaveCfgTimer=setTimeout(()=>{agenticPost('/api/agentic/weave',{cfg:v}).then(j=>{if($('agenticWeaveCfgValue'))$('agenticWeaveCfgValue').textContent=Number(j.cfg).toFixed(1);}).catch(()=>{});},150);});
  fetch('/api/agentic/weave').then(r=>r.json()).then(j=>{if(j&&typeof j.noise==='number'){if($('agenticWeaveNoise'))$('agenticWeaveNoise').value=String(Math.round(j.noise*100));if($('agenticWeaveNoiseValue'))$('agenticWeaveNoiseValue').textContent=Number(j.noise).toFixed(2);}if(j&&typeof j.cfg==='number'){if($('agenticWeaveCfg'))$('agenticWeaveCfg').value=String(Math.round(j.cfg*10));if($('agenticWeaveCfgValue'))$('agenticWeaveCfgValue').textContent=Number(j.cfg).toFixed(1);}}).catch(()=>{});$('agenticPrompt')?.addEventListener('input',scheduleUnifiedPromptUpdate);$('agenticAutonomy')?.addEventListener('change',scheduleUnifiedPromptUpdate);
  $('agenticLetsGoBtn')?.addEventListener('click',async()=>{const btn=$('agenticLetsGoBtn');if(btn?.disabled)return;try{if(btn){btn.disabled=true;btn.classList.add('starting');}if($('agenticStatus'))$('agenticStatus').textContent='LET\'S GO · resolving Spotify → JoyMetric Virtual Input → exact Audio Out…';syncUnifiedPrompt('agentic');const result=await agenticPost('/api/agentic/start',{output_id:$('agenticOutput')?.value||'',prompt:String($('agenticPrompt')?.value||''),autonomy:$('agenticAutonomy')?.value||'autopilot',creativity:Number($('agenticCreativity')?.value||35)/100,effect_intensity:readRealtimeEffectIntensity()});if($('agenticStatus'))$('agenticStatus').textContent='LOOKAHEAD · Spotify routed · analyzing 15 s future PCM before first audible bar';await pollAgenticDJStatus();return result;}catch(e){if($('agenticStatus'))$('agenticStatus').textContent=`LET'S GO failed · ${sanitizeVisibleText(e.message)} · app remains safe/stopped`;await pollAgenticDJStatus();}finally{if(btn){btn.disabled=false;btn.classList.remove('starting');}}});
  $('agenticStopBtn')?.addEventListener('click',()=>agenticPost('/api/agentic/stop').catch(()=>{}));
  document.querySelectorAll('[data-agentic-transport]').forEach(b=>b.addEventListener('click',async()=>{try{const action=b.dataset.agenticTransport,value=b.dataset.value!=null?Number(b.dataset.value):null;await agenticPost('/api/agentic/transport',{action,value});}catch(e){$('agenticStatus').textContent=`Transport: ${sanitizeVisibleText(e.message)}`;}}));
  document.querySelectorAll('[data-agentic-macro]').forEach(b=>b.addEventListener('click',async()=>{try{await agenticPost('/api/agentic/macro',{action:b.dataset.agenticMacro});await pollAgenticDJStatus();}catch(e){if($('agenticStatus'))$('agenticStatus').textContent=`Concert pad: ${sanitizeVisibleText(e.message)}`;}}));
  document.querySelectorAll('[data-agentic-loop]').forEach(b=>b.addEventListener('click',()=>agenticPost('/api/agentic/loop',{action:'capture',beats:Number(b.dataset.agenticLoop||1)}).catch(e=>{$('agenticStatus').textContent=e.message;})));$('agenticLoopRelease')?.addEventListener('click',()=>agenticPost('/api/agentic/loop',{action:'release'}).catch(()=>{}));
  // v31.10: per-slider debounce + drag guard.  Moving two faders quickly no
  // longer drops the first value, the UI label updates instantly, and the
  // status poll cannot snap a fader back while the hand is still on it.
  Object.entries(AGENTIC_CONTROL_DEFS).forEach(([id,[key,scale,_mixKey,fmt,labelId]])=>{
    const el=$(id);if(!el)return;
    el.classList.add('agentic-live-range');setAgenticRangeFill(el);
    const post=()=>{const v=Number(el.value||0)/Number(scale||1);agenticPost('/api/agentic/controls',{controls:{[key]:v}}).catch(()=>{});};
    el.addEventListener('input',()=>{
      agenticCtl.guards[id]=performance.now()+900;
      setAgenticRangeFill(el);
      if(labelId){const lb=$(labelId);if(lb)lb.textContent=agenticFmt(fmt,Number(el.value||0)/Number(scale||1));}
      clearTimeout(agenticCtl.timers[id]);agenticCtl.timers[id]=setTimeout(post,40);
    });
    el.addEventListener('pointerup',()=>{agenticCtl.guards[id]=performance.now()+900;});
    el.addEventListener('dblclick',()=>{
      const neutral=key.endsWith('_gain')?1:(key==='fx_gate_div'?2:(key==='fx_echo_beats'?0.5:0));
      el.value=String(neutral*Number(scale||1));el.dispatchEvent(new Event('input'));
    });
  });
  $('agenticApprove')?.addEventListener('click',()=>agenticPost('/api/agentic/approve').then(pollAgenticDJStatus).catch(()=>{}));$('agenticLock')?.addEventListener('click',()=>{const locked=!Boolean(agenticDjState.last?.locked);agenticPost('/api/agentic/lock',{locked}).then(pollAgenticDJStatus).catch(()=>{});});$('agenticRegenerate')?.addEventListener('click',()=>agenticPost('/api/agentic/regenerate').then(pollAgenticDJStatus).catch(()=>{}));$('agenticTakeControl')?.addEventListener('click',()=>agenticPost('/api/agentic/take-control').then(pollAgenticDJStatus).catch(()=>{}));
  $('realtimeDjPrompt')?.addEventListener('input',()=>{syncUnifiedPrompt('realtime');scheduleUnifiedPromptUpdate();});
  $('realtimeEffectIntensity')?.addEventListener('input',()=>{if(agenticDjState.last?.running)scheduleUnifiedPromptUpdate();});
  // v31.6: the Realtime 15D deck is the same DOM/control surface in both views.
  // v31.1.2: output scan is triggered by showAgenticDJView(), not while hidden at boot.
  clearInterval(agenticDjState.pollTimer);agenticDjState.pollTimer=setInterval(()=>{if(!$('agenticView')?.classList.contains('hidden'))pollAgenticDJStatus();},1000);pollAgenticDJStatus();
}

function setupInitialVisuals(){
  drawAllPlaceholders();
  drawCoverPlaceholder($('transportCover'));
  window.addEventListener('resize',()=>{
    if(!state.outputs){drawAllPlaceholders();redrawTransportCover();}
    else{
      state.waveItems.forEach(item=>item.audio.dispatchEvent(new Event('timeupdate')));
      state.spectrumItems.forEach(item=>drawSpectrum(item.canvas,item.surface,item.tone));
    }
  });
}

setupProjectTitleEditing();
setupFilePicker();
setupSourceModes();
setupMicrophoneRecorder();
setupPromptChips();
setupSliders();
setupNavigation();
setupLibraryNavigation();
setupRealtimeFeatures();
setupAgenticDJ();
setupReferenceLibraryUpload();
setupSaveLibrary();
setupColorThemePicker();
refreshLibraryCounts();
refreshReferenceSourceOptions();
setupTransport();
setupInitialVisuals();
$('processBtn').addEventListener('click',processSong);
refreshBackend();
setInterval(refreshBackend,15000);
