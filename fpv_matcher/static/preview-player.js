// Keep HTMLVideoElement timestamps in the original recording's time domain.
// Each fragment is independently seekable; no whole-file transcode is queued.
const players = new Map();
const clamp = (value, maximum) => Math.max(0, Math.min(Number(value) || 0, Math.max(0, maximum - 1 / 30)));

export function releasePreview(video) {
  players.get(video)?.dispose();
}
export function releaseAllPreviews() {
  for (const player of [...players.values()]) player.dispose();
}

function eventUntil(target, event, action, timeout = 45000) {
  return new Promise((resolve, reject) => {
    const done = () => finish(), failed = () => finish(new Error('The browser could not decode the fast preview.'));
    const timer = setTimeout(() => finish(new Error('Fast preview took too long to load. Try again or use an original supported by your browser.')), timeout);
    function finish(error) {
      clearTimeout(timer); target.removeEventListener(event, done); target.removeEventListener('error', failed);
      error ? reject(error) : resolve();
    }
    target.addEventListener(event, done, {once: true}); target.addEventListener('error', failed, {once: true});
    try { action?.(); } catch (error) { finish(error); }
  });
}

function decodedFrame(video, at) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => finish(new Error('Could not decode the requested preview frame.')), 45000);
    const failed = () => finish(new Error('The browser could not decode the fast preview.'));
    const check = () => { if (!video.seeking && video.readyState >= 2 && video.videoWidth) finish(); };
    function finish(error) {
      clearTimeout(timer);
      for (const event of ['loadeddata', 'seeked', 'canplay']) video.removeEventListener(event, check);
      video.removeEventListener('error', failed); error ? reject(error) : resolve();
    }
    for (const event of ['loadeddata', 'seeked', 'canplay']) video.addEventListener(event, check);
    video.addEventListener('error', failed);
    try { video.currentTime = at; check(); } catch (error) { finish(error); }
  });
}

export async function loadFastPreview(video, base, current, notify, initialTime = 0) {
  releasePreview(video);
  const descriptorResponse = await fetch(base);
  if (!descriptorResponse.ok) throw new Error('Could not read preview metadata.');
  const manifest = await descriptorResponse.json();
  if (!current()) return;
  if (!window.MediaSource || !MediaSource.isTypeSupported(manifest.mime)) {
    throw new Error('Fast preview needs a browser supporting H.264 Media Source playback. Try current Chrome or Edge, or play a supported original.');
  }
  const media = new MediaSource(), url = URL.createObjectURL(media);
  let buffer, disposed = false, busy = false, controller, activeIndex = -1, timer;
  const loaded = new Set();
  const atIndex = () => Math.min(manifest.chunks - 1, Math.floor(clamp(video.currentTime, manifest.duration) / manifest.chunk_seconds));
  const player = {dispose() {
    if (disposed) return;
    disposed = true; clearInterval(timer); controller?.abort();
    for (const event of ['seeking', 'play', 'timeupdate']) video.removeEventListener(event, wake);
    video.removeEventListener('error', fail);
    if (buffer?.updating && media.readyState === 'open') { try { buffer.abort(); } catch {} }
    if (video.getAttribute('src') === url) { video.pause(); video.removeAttribute('src'); video.load(); }
    URL.revokeObjectURL(url); if (players.get(video) === player) players.delete(video);
  }};
  players.set(video, player);
  function fail() { if (!disposed && current()) notify('Fast preview playback failed. Reload the pair to retry.', true); player.dispose(); }
  function wake() {
    if (activeIndex >= 0 && Math.abs(activeIndex - atIndex()) > 1) controller?.abort();
    void pump();
  }
  async function remove(start, end) {
    if (end > start && buffer.buffered.length) await eventUntil(buffer, 'updateend', () => buffer.remove(start, end), 10000);
  }
  async function trim() {
    const at = video.currentTime, size = manifest.chunk_seconds;
    const before = Math.max(0, Math.floor(at / size) * size - 2 * size), after = Math.min(manifest.duration, Math.ceil(at / size) * size + 4 * size);
    if (before > 0) await remove(0, before);
    if (after < manifest.duration) await remove(after, manifest.duration + 1);
    for (const index of loaded) if ((index + 1) * size <= before || index * size >= after) loaded.delete(index);
  }
  async function ensure(index) {
    if (!current()) { player.dispose(); return; }
    if (loaded.has(index) || index < 0 || index >= manifest.chunks || disposed) return;
    activeIndex = index; controller = new AbortController();
    const response = await fetch(`${base}/${index}?source=${encodeURIComponent(manifest.source_key)}`, {signal: controller.signal});
    if (!response.ok) {
      let text = 'Preview chunk could not be prepared.';
      try { text = (await response.json()).detail || text; } catch {}
      throw new Error(text);
    }
    const bytes = await response.arrayBuffer();
    if (disposed || !current()) return;
    const start = Number(response.headers.get('X-Preview-Start'));
    const duration = Number(response.headers.get('X-Preview-Duration'));
    const videoStart = Number(response.headers.get('X-Preview-Video-Start'));
    if (![start, duration, videoStart].every(Number.isFinite) || duration <= 0) throw new Error('Invalid preview timing.');
    buffer.timestampOffset = start - videoStart;
    buffer.appendWindowStart = 0;
    buffer.appendWindowEnd = start + duration;
    buffer.appendWindowStart = start;
    try {
      await eventUntil(buffer, 'updateend', () => buffer.appendBuffer(bytes), 15000);
    } catch (error) {
      if (error.name !== 'QuotaExceededError') throw error;
      await trim();
      await eventUntil(buffer, 'updateend', () => buffer.appendBuffer(bytes), 15000);
    }
    loaded.add(index); activeIndex = -1;
  }
  async function pump() {
    if (disposed || busy || !buffer) return;
    if (!current() || media.readyState === 'closed') { player.dispose(); return; }
    busy = true;
    try {
      await trim();
      let index = atIndex();
      await ensure(index);
      if (index !== atIndex()) return;
      // Paused views buffer one following fragment. While playing, two give
      // both feeds time to decode over LAN without converting the whole clip.
      const count = video.paused ? 1 : 2;
      for (let n = 1; n <= count && index === atIndex(); n++) await ensure(index + n);
      if (index === manifest.chunks - 1 && media.readyState === 'open' && !buffer.updating) media.endOfStream();
    } catch (error) {
      if (error.name !== 'AbortError' && !disposed && current()) { notify(error.message, true); player.dispose(); }
    } finally { busy = false; }
  }
  try {
    notify('Loading a short fast-preview chunk…');
    await eventUntil(media, 'sourceopen', () => { video.preload = 'auto'; video.src = url; video.load(); }, 10000);
    if (!current()) { player.dispose(); return; }
    buffer = media.addSourceBuffer(manifest.mime); buffer.mode = 'segments';
    const at = clamp(initialTime, manifest.duration);
    busy = true;
    const metadata = video.readyState >= 1 ? Promise.resolve() : eventUntil(video, 'loadedmetadata');
    metadata.catch(() => {});
    await ensure(Math.floor(at / manifest.chunk_seconds)); await metadata;
    media.duration = manifest.duration;
    await decodedFrame(video, at);
    if (!current()) { player.dispose(); return; }
    busy = false;
    for (const event of ['seeking', 'play', 'timeupdate']) video.addEventListener(event, wake);
    video.addEventListener('error', fail);
    timer = setInterval(() => void pump(), 500);
    void pump();
    notify('Fast preview ready · 480p chunks on demand; exports use the originals.');
  } catch (error) { player.dispose(); if (current()) throw error; }
}
window.addEventListener('pagehide', releaseAllPreviews);
