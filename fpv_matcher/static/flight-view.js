import {releasePreview, setPreviewPlaybackIntent} from './preview-player.js?v=4';

const $ = id => document.getElementById(id);
let context, sessionId, stickId = '', key = '', viewToken = 0, fpvToken = 0, seekToken = 0;
let radioReady = false, fpvLoading = false, playing = false, activePart = null, overrideId = null;
const playPending = new WeakSet();
const radio = () => $('flight-stick-player'), fpv = () => $('flight-fpv-player');
const group = () => context?.getDoc()?.flights?.find(f => f.id === stickId);
const source = () => context?.getDoc()?.videos.find(r => r.id === stickId);
export const selectedFlightStick = () => stickId;
const pair = part => context.getDoc().pairs.find(p => p.id === part?.id);
const plannedParts = () => (group()?.parts || []).filter(p => p.on_timeline);
function partAt(time) {
  const parts = group()?.parts || [];
  const alternative = parts.find(p => p.id === overrideId && time >= p.start && time < p.end - .01);
  return alternative || plannedParts().find(p => time >= p.start && time < p.end - .01) || null;
}
function ahead(video) {
  for (let i = 0; i < video.buffered.length; i++) {
    if (video.buffered.start(i) <= video.currentTime + .04 && video.buffered.end(i) > video.currentTime)
      return video.buffered.end(i) - video.currentTime;
  }
  return 0;
}
function hold() { radio().pause(); fpv().pause(); }
export function pauseFlight() {
  playing = false; hold();
  for (const video of [radio(), fpv()]) setPreviewPlaybackIntent(video, false);
}
export function clearFlightPreview() {
  if (!context) return;
  pauseFlight(); viewToken++; fpvToken++; seekToken++; radioReady = false; fpvLoading = false; activePart = null; overrideId = null;
  for (const video of [radio(), fpv()]) { releasePreview(video); video.removeAttribute('src'); video.load(); }
  $('flight-preview').hidden = true;
  $('flight-gap').hidden = true;
  $('flight-stick-buffer').hidden = $('flight-fpv-buffer').hidden = true;
  $('flight-play').disabled = true; $('flight-review').disabled = true;
}
function showTime() {
  const time = radio().currentTime || 0, duration = source()?.metadata?.duration || 1;
  $('flight-seek').value = time;
  $('flight-source-time').textContent = `StickCam ${context.fmt(time)} / ${context.fmt(duration)}`;
  const cursor = $('flight-cursor'); if (cursor) cursor.style.left = `${100 * time / duration}%`;
}
function status(text) { $('flight-preview-status').textContent = text; }
async function switchPart(part, time) {
  if (part?.id === activePart?.id && (part || !$('flight-gap').hidden)) return;
  hold(); fpvToken++; const generation = viewToken, token = fpvToken;
  activePart = part; fpvLoading = !!part;
  releasePreview(fpv()); fpv().removeAttribute('src'); fpv().load(); fpv().muted = true;
  $('flight-gap').hidden = !!part;
  $('flight-review').disabled = !part;
  if (!part) {
    $('flight-fpv-title').textContent = 'FPV · no identified part at this time';
    status('No identified FPV footage at this point. StickCam continues through the gap.');
    return;
  }
  const p = pair(part), record = context.getDoc().videos.find(r => r.id === p?.fpv);
  if (!p || !record || record.missing || record.error) {
    fpvLoading = false; pauseFlight(); status('This FPV recording is unavailable. Relink or rescan it before previewing.'); return;
  }
  $('flight-fpv-title').textContent = `FPV · ${record.name}`;
  const current = () => generation === viewToken && token === fpvToken;
  try {
    await context.loadPreview(fpv(), record, Math.max(0, time - p.offset), current, message => {
      if (current()) status(message);
    });
    if (current()) {
      // A scrub made while the source loaded may have changed the requested time.
      if (partAt(radio().currentTime)?.id === part.id)
        fpv().currentTime = Math.max(0, radio().currentTime - p.offset);
      status(`${part.on_timeline ? 'Timeline part' : 'Conflicting alternative'} · ${record.name} · ${part.state}. ${part.on_timeline ? '' : 'Review its alignment before using it as a split part.'}`);
    }
  } catch (error) { if (current()) { pauseFlight(); status(error.message); } }
  finally { if (current()) { fpvLoading = false; setPreviewPlaybackIntent(fpv(), playing); } }
}
async function jump(time, id = null) {
  const generation = viewToken, seek = ++seekToken;
  pauseFlight(); overrideId = id;
  const duration = source()?.metadata?.duration || 1;
  time = Math.max(0, Math.min(time, duration - .05));
  radio().currentTime = time;
  await switchPart(partAt(time), time);
  if (generation !== viewToken || seek !== seekToken) return;
  const p = pair(activePart);
  if (p && fpv().readyState >= 1) fpv().currentTime = Math.max(0, time - p.offset);
  showTime();
}
async function openStick(time = 0, id = null) {
  const recording = source();
  if (!recording?.metadata || recording.missing || recording.error) {
    context.notice('Choose a readable StickCam recording; relink or rescan unavailable sources.', true); return;
  }
  clearFlightPreview(); context.clearReview();
  const generation = viewToken, current = () => generation === viewToken;
  $('flight-preview').hidden = false;
  $('flight-stick-buffer').hidden = false;
  $('flight-stick-title').textContent = `StickCam · ${recording.name}`;
  $('flight-seek').max = recording.metadata.duration;
  radio().muted = false; radio().volume = 1; radio().playbackRate = fpv().playbackRate = 1;
  status('Preparing this StickCam flight…');
  try {
    await context.loadPreview(radio(), recording, time, current, message => { if (current()) status(message); });
    if (!current()) return;
    radioReady = true; $('flight-stick-buffer').hidden = true; $('flight-play').disabled = false;
    await jump(time, id);
  } catch (error) { if (current()) status(error.message); }
}
async function previewPart(id) {
  const part = group()?.parts.find(p => p.id === id); if (!part) return;
  if (!radioReady) await openStick(part.start, id); else await jump(part.start, id);
  $('flight-preview').scrollIntoView({block: 'start', behavior: 'smooth'});
}
export function installFlightView(ctx) {
  context = ctx;
  $('flight-stick').onchange = () => { stickId = $('flight-stick').value; key = ''; renderFlightView(ctx.getDoc()); void openStick(); };
  $('flights-panel').addEventListener('toggle', () => {
    if ($('flights-panel').open && !radioReady && !ctx.isSimple()) void openStick();
    else if (!$('flights-panel').open) pauseFlight();
  });
  $('flight-play').onclick = () => {
    if (!radioReady) return;
    if (radio().currentTime >= source().metadata.duration - .05) void jump(0).then(() => { playing = true; });
    else playing = true;
    for (const video of [radio(), fpv()]) setPreviewPlaybackIntent(video, true);
  };
  $('flight-pause').onclick = pauseFlight;
  $('flight-seek').oninput = () => void jump(Number($('flight-seek').value)).catch(e => status(e.message));
  $('flight-review').onclick = () => {
    const id = activePart?.id;
    if (id) void ctx.openPair(id, {scrollToPreview: true}).catch(e => ctx.notice(e.message, true));
  };
  setInterval(() => {
    if (!radioReady || $('flight-preview').hidden) return;
    const time = radio().currentTime, part = partAt(time);
    showTime();
    $('flight-stick-buffer').hidden = !(radio().seeking || (playing && radio().readyState < 3));
    $('flight-fpv-buffer').hidden = !part || !(fpvLoading || fpv().seeking || (playing && fpv().readyState < 3));
    if (!playing) return;
    if (time >= source().metadata.duration - .05) { pauseFlight(); return; }
    if (part?.id !== activePart?.id) { void switchPart(part, time); return; }
    const p = pair(part);
    const radioNeed = Math.min(.1, Math.max(.005, source().metadata.duration - time - .02));
    const fpvNeed = part ? Math.min(.1, Math.max(.005, part.end - time - .02)) : 0;
    const fpvAtEnd = p && fpv().ended && part.end - time < .1;
    if (fpvLoading || radio().seeking || radio().readyState < 3 || ahead(radio()) < radioNeed ||
        p && !fpvAtEnd && (fpv().seeking || fpv().readyState < 3 || ahead(fpv()) < fpvNeed)) { hold(); return; }
    if (p && Math.abs(fpv().currentTime - (time - p.offset)) > .12) {
      hold(); fpv().currentTime = Math.max(0, time - p.offset); return;
    }
    for (const video of p ? [radio(), fpv()] : [radio()]) {
      if (video.paused && !playPending.has(video) && !(video === fpv() && fpvAtEnd)) {
        const generation = viewToken, token = fpvToken;
        playPending.add(video);
        video.play().catch(error => {
          if (playing && generation === viewToken && (video === radio() || token === fpvToken) && error.name !== 'AbortError') { pauseFlight(); status(error.message); }
        }).finally(() => playPending.delete(video));
      }
    }
  }, 150);
  window.addEventListener('pagehide', pauseFlight);
}
export function renderFlightView(doc) {
  if (!context) return;
  const {escape, fmt} = context;
  if (sessionId !== doc.id) { clearFlightPreview(); sessionId = doc.id; stickId = ''; key = ''; }
  const sticks = doc.videos.filter(r => r.kind === 'stick');
  if (!sticks.some(r => r.id === stickId)) stickId = doc.flights?.find(f => f.confirmed)?.id || doc.flights?.[0]?.id || sticks[0]?.id || '';
  if (radioReady && (source()?.missing || source()?.error || !source()?.metadata)) clearFlightPreview();
  const f = group(), nextKey = JSON.stringify([doc.id, stickId, sticks.map(r => [r.id, r.name, r.metadata?.duration]), f]);
  if (nextKey === key) return; key = nextKey;
  $('flight-stick').innerHTML = sticks.length ? sticks.map(r => `<option value="${r.id}">${escape(r.name)}${r.metadata ? ` · ${fmt(r.metadata.duration)}` : ' · not scanned'}</option>`).join('') : '<option value="">No StickCam recordings</option>';
  $('flight-stick').value = stickId;
  const duration = source()?.metadata?.duration || 1, parts = f?.parts || [], planned = parts.filter(p => p.on_timeline), conflicts = parts.filter(p => !p.on_timeline);
  $('flight-groups').innerHTML = `<p>${f?.confirmed || 0} confirmed parts · ${planned.length} proposed timeline parts · ${conflicts.length} conflicting alternatives</p><div class="flight-time-labels"><span>0:00</span><span>${fmt(duration)}</span></div><div id="flight-track" class="flight-timeline" aria-label="FPV parts on the StickCam timeline">${planned.map(part => `<button data-flight-pair="${part.id}" class="flight-part ${part.state}" style="left:${100 * part.display_start / duration}%;width:${100 * (part.end - part.display_start) / duration}%" title="${escape(part.name)} · ${fmt(part.start)}–${fmt(part.end)} · ${part.state}">${escape(part.name)}</button>`).join('')}<span id="flight-cursor" class="flight-cursor"></span></div><p class="hint">Green: confirmed. Blue: suggested. Empty sections: no proposed FPV footage. Choose a part to preview it, or click the timeline to seek.</p>${parts.length ? '<div class="flight-candidate-list">' + parts.map(part => `<div class="flight-part-row ${part.on_timeline ? '' : 'flight-conflict'}"><span><strong>${escape(part.name)}</strong><small>${fmt(part.start)}–${fmt(part.end)} in StickCam · ${escape(part.state)} · ${escape(part.confidence)}</small>${part.overlap_with.length ? `<small class="error">Conflicts with ${part.overlap_with.map(c => `${escape(c.name)} (${c.seconds.toFixed(2)}s overlap)`).join(', ')}. Review the alignment.</small>` : ''}${part.other_owner ? '<small class="error">Already confirmed with another StickCam.</small>' : ''}</span><button data-flight-pair="${part.id}">${part.on_timeline ? 'Preview part' : 'Preview alternative'}</button></div>`).join('') + '</div>' : '<p class="hint">No candidates for this StickCam yet. You can preview the recording, or find matches first.</p>'}${f?.overlaps.length ? '<p class="error">Saved confirmed parts conflict. Unconfirm or correct their alignments before a grouped export.</p>' : ''}<button id="flight-export" ${f?.confirmed && !f.overlaps.length ? '' : 'disabled'}>Export confirmed flight parts</button>`;
  $('flight-groups').querySelectorAll('[data-flight-pair]').forEach(b => b.onclick = event => { event.stopPropagation(); void previewPart(b.dataset.flightPair).catch(e => context.notice(e.message, true)); });
  $('flight-track').onclick = event => {
    const bounds = event.currentTarget.getBoundingClientRect(), time = (event.clientX - bounds.left) / bounds.width * duration;
    if (!radioReady) void openStick(time); else void jump(time).catch(e => status(e.message));
  };
  $('flight-export').onclick = () => context.exportPairs(doc.pairs.filter(p => p.stick === stickId && p.confirmed && !p.stale).map(p => p.id)).catch(e => context.notice(e.message, true));
  showTime();
}
