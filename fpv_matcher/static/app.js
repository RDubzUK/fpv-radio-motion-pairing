import {installManualPairing} from './manual-pairing.js?v=2';
import {installFingerprintReview} from './fingerprint-review.js?v=1';
import {installFlightView, renderFlightView, clearFlightPreview, pauseFlight} from './flight-view.js?v=1';
import {loadFastPreview, releasePreview, releaseAllPreviews, setPreviewPlaybackIntent} from '/static/preview-player.js?v=4';
const $ = id => document.getElementById(id);
const state = { job: null, results: null, source: 'upload', busy: false, uploadController: null,
  jobToken: 0, calToken: 0, viewerToken: 0, pollTimer: null, exportTimers: new Map(),
  calibration: null, metadata: null, tool: null, drag: null, pair: null, page: 0,
  playing: false, startingPlayback: false, animation: null, lastCorrection: 0,
  frameController: null, frameURL: null, frameToken: 0, offsetToken: 0, targets: {fpv: new Set(), radio: new Set()},
  calibrationRotationOverride: null, calibrationName: null, calibrationProposalId: null, calibrationDrafts: {}, calibrationFrameReady: false, suggesting: false, suggestionToken: 0, suggestionController: null };
let calibrationLoad = null;
const storage = {getItem(key){try{return window.localStorage.getItem(key);}catch{return null;}},setItem(key,value){try{window.localStorage.setItem(key,value);}catch{}},removeItem(key){try{window.localStorage.removeItem(key);}catch{}}};
const clone = value => JSON.parse(JSON.stringify(value));
const calibrationScan = { id: null, jobId: null, token: 0, timer: null, running: false, rows: [] };
const segmentScan = {token:0, timer:null, task:null, running:false};
const fpvPreparation = {key:'', refreshed:0, token:0, task:null, jobId:null, timer:null, running:false, selected:new Set()};
const fpvVisualReview = {token:0, windowToken:0, name:null, fps:null, data:null, duration:0, frame:null, pending:false, controller:null};
const radioOrientation = { token: 0, timer: null, task: null, running: false, result: null };
const stickReview = { token: 0, name: null, jobId: null, data: null, dirty: false, frame: null, reloadAfterDerivation: null,
  trackingWindow:null, windowPending:false, windowToken:0, windowRetry:0, framePurpose:'initial', frameEdits:false, loading:false, saving:null };
const counterpartSearch = {starting:false, candidatesKey:null};
const part = value => encodeURIComponent(value);
const path = suffix => `/api/jobs/${state.job.id}${suffix}`;
const show = (id, visible = true) => $(id).classList.toggle('hidden', !visible);
function openPanel(id) { show(id); $(id).classList.add("guided-open"); $(id).open = true; }
const message = (id, text, error = false) => { $(id).textContent = text; $(id).classList.toggle('error', error); $(id).classList.remove('warning'); };
const bytes = value => value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(1)} GiB` : `${(value / 1024 ** 2).toFixed(1)} MiB`;
const time = (value, precise = false) => {
  value = Math.max(0, Number(value) || 0);
  const hours = Math.floor(value / 3600), minutes = Math.floor(value / 60) % 60;
  const seconds = precise ? (value % 60).toFixed(3).padStart(6, '0') : Math.floor(value % 60).toString().padStart(2, '0');
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${seconds}` : `${minutes}:${seconds}`;
};
const clipDuration = record => Number.isFinite(record?.metadata?.duration) ? time(record.metadata.duration) : 'duration pending';
const clipLabel = record => `${record.name} · ${clipDuration(record)}`;
const offsetText = value => `${value >= 0 ? '+' : ''}${Number(value).toFixed(3)}s`;
const el = (tag, text, className) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; };
let radioModels = [];
const action = (text, callback, className = 'secondary') => { const button = el('button', text, className); button.addEventListener('click', callback); return button; };
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function waitForServer() {
  let lastError;
  for (let attempt = 1; attempt <= 45; attempt++) {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5000);
      try { return await api('/api/system', { signal: controller.signal }); }
      finally { clearTimeout(timeout); }
    }
    catch (error) {
      lastError = error;
      message('system', `Connecting to the local server… (${attempt}/45)`);
      if (attempt < 45) await sleep(1000);
    }
  }
  throw new Error(`Could not connect to the local server: ${lastError?.message || 'no response'}`);
}
async function api(url, options = {}) {
  const { json, ...request } = options;
  if (json !== undefined) { request.body = JSON.stringify(json); request.headers = { ...request.headers, 'Content-Type': 'application/json' }; }
  const response = await fetch(url, request);
  if (!response.ok) {
    let detail; try { detail = (await response.json()).detail; } catch { detail = response.statusText; }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return response.json();
}
function guarded(callback, target) {
  return async (...args) => { try { await callback(...args); } catch (error) { if (error.name !== 'AbortError') message(target, error.message, true); } };
}
const fingerprintReview=installFingerprintReview({$,getJob:()=>state.job,getToken:()=>state.jobToken,getPair:()=>state.pair,api,notice:message,fmt:time,onTaskChange:()=>{renderVideoLists();refreshStickData(true).catch(error=>message('stick-data-message',error.message,true));},
  onPreview:async(radio,fpv,offset,signatures)=>{
    const info=await api(path('/pair-info'),{method:'POST',json:{radio_video:radio,fpv_video:fpv,offset_seconds:offset,manual_pair:true}});
    if(signatures && ['radio','fpv'].some(k=>JSON.stringify(signatures[k])!==JSON.stringify(info.source_signatures[k])))throw new Error('Source changed since these rhythms were loaded. Reload the rhythm comparison.');
    await openPair(radio,fpv,offset,false,false,{source_revisions:info.source_revisions});
  },
  onSeek:(radio,fpv,at)=>{if(state.pair?.radio_video===radio&&state.pair.fpv_video===fpv){pausePair();seekPair(at-state.pair.radio_start);}},
  getPlayhead:(radio,fpv)=>state.pair?.radio_video===radio&&state.pair.fpv_video===fpv?$('radio-preview').currentTime:null});
const manualBrowser = installManualPairing({$,getJob:()=>state.job,getToken:()=>state.jobToken,api,loadVideo,fmt:time,
  onPreview:(radio,fpv,offset,options)=>openPair(radio,fpv,offset,false,false,options),
  onLabel:assignFPVRecorder,pauseOther:()=>{pausePair();pauseFlight();$('cal-video').pause();pauseMotionPreview();}});
async function assignFPVRecorder(names,label) {
  if(!state.job)return;
  const id=state.job.id,token=state.jobToken;
  const job=await api(`/api/jobs/${id}/fpv-recorders`,{method:'PUT',json:{names,label}});
  if(token!==state.jobToken)return;
  state.job=job;manualBrowser.render();renderVideoLists();
  if(job.has_results)await loadResults();
}
$('fpv-recorder-apply').onclick=guarded(async()=>{
  if($('match-target-kind').value!=='fpv'||!state.targets.fpv.size)throw new Error('Choose Selected FPV scope and select the files from this recorder.');
  await assignFPVRecorder([...state.targets.fpv],$('fpv-recorder-label').value.trim());
  message('fpv-recorder-message',`Recorder label saved for ${state.targets.fpv.size} FPV file(s). Date suggestions updated using separate recorder clocks.`);
},'fpv-recorder-message');
$('workflow-manual').onclick=()=>{openPanel('manual-pair-panel');manualBrowser.render();$('manual-pair-panel').scrollIntoView({behavior:'smooth',block:'start'});};

function setBusy(value) {
  state.busy = value;
  $('prepare').disabled = value;
  $('new-session').disabled = value;
  document.querySelectorAll('#history button').forEach(button => button.disabled = value);
}
function activeAnalysis() { return state.job && ['queued', 'running', 'cancelling'].includes(state.job.status); }
const evidenceFields = {fpv_gyro: 'use-fpv-gyro', fpv_visual: 'use-fpv-visual', stick_inputs: 'use-stick-inputs', fpv_audio: 'use-fpv-audio', stickcam_audio: 'use-stickcam-audio', filename_timestamps: 'use-filename-timestamps', movement_rhythm:'use-movement-rhythm', gimbal_activity:'use-gimbal-activity'};
function readEvidenceSources() {
  return Object.fromEntries(Object.entries(evidenceFields).map(([key, id]) => [key, $(id).checked]));
}
function evidenceMode(sources = readEvidenceSources()) {
  return {motion: (sources.stick_inputs || sources.gimbal_activity) && (sources.fpv_gyro || sources.fpv_visual), audio: sources.fpv_audio && sources.stickcam_audio};
}
function loadEvidenceSources(settings = {}) {
  const source = settings.motion_source || 'auto', audio = settings.audio_enabled !== false;
  const defaults = {fpv_gyro: source !== 'visual', fpv_visual: source !== 'gyro', stick_inputs: true, fpv_audio: audio, stickcam_audio: audio, filename_timestamps: true, movement_rhythm:false, gimbal_activity:true};
  for (const [key, id] of Object.entries(evidenceFields)) $(id).checked = settings.evidence_sources?.[key] ?? defaults[key];
  $('workflow-rhythm').checked=$('use-movement-rhythm').checked;
  updateEvidenceSummary();
}
function updateEvidenceSummary() {
  if(!$('use-stick-inputs').checked)$('use-movement-rhythm').checked=false;
  $('use-movement-rhythm').disabled=!$('use-stick-inputs').checked;
  $('workflow-rhythm').checked=$('use-movement-rhythm').checked;
  $('workflow-gimbal-activity').checked=$('use-gimbal-activity').checked;
  const sources = readEvidenceSources(), mode = evidenceMode(sources), notes = [];
  if (mode.motion) notes.push(sources.fpv_gyro ? (sources.fpv_visual ? 'Embedded FPV motion first; visual fallback if needed.' : 'Embedded FPV motion only; no visual extraction.') : 'Visual FPV motion only; embedded data is skipped.');
  else notes.push('All motion extraction and StickCam derivation are skipped; no calibration is required.');
  notes.push(mode.audio ? 'Boundary audio matching is enabled.' : 'Audio extraction and matching are skipped.');
  if(sources.gimbal_activity) notes.push(mode.motion ? 'Tip-free gimbal activity is cached independently; tips and rates are unnecessary. Suggestions need visible-event review.' : 'Gimbal movement needs an FPV motion source.');
  if(sources.gimbal_activity && sources.stick_inputs) notes.push('Both methods are selected: precise stick derivation will also run. Choose Movement · no tip tracking for the lighter pass.');
  if(sources.movement_rhythm) notes.push(sources.stick_inputs && mode.motion ? 'Derived-input rhythm uses the saved stick data.' : 'Derived-input rhythm needs StickCam inputs and an FPV motion source.');
  if (sources.fpv_audio !== sources.stickcam_audio) notes.push('Select both audio sources to use audio.');
  if (!mode.motion && (sources.stick_inputs || sources.gimbal_activity || sources.fpv_gyro || sources.fpv_visual)) notes.push('Select StickCam inputs or gimbal movement and an FPV motion source.');
  if (sources.filename_timestamps) notes.push('Filename clocks need an independent match; timestamps alone cannot sync footage.');
  const valid = mode.motion || mode.audio;
  if (!valid) notes.push('Choose a complete audio or motion comparison before analyzing.');
  if ($('matching-strategy').value === 'selected' && mode.motion && $('audio-only-unconfigured').checked && !mode.audio) notes.push('Unconfigured StickCam fallback requires both audio sources.');
  message('evidence-summary', notes.join(' '), !valid || ($('matching-strategy').value === 'selected' && mode.motion && $('audio-only-unconfigured').checked && !mode.audio));
  $('audio-only-unconfigured').disabled = !mode.motion;
  if (state.job) $('analyze').disabled = activeAnalysis() || stickPreparation.running || fpvPreparation.running || !Object.values(state.job.inputs).flat().every(item => item.uploaded === item.size) || !valid || ($('matching-strategy').value === 'selected' && mode.motion && $('audio-only-unconfigured').checked && !mode.audio);
  if (state.job) { renderCounterpart();renderWorkflow(); }
}
function calibrated(config) { return config && !config.source_changed && (config.exclude || config.skip_motion || ['left_gimbal', 'right_gimbal'].every(side => config[side]?.tip_x !== undefined)); }
function activityConfigured(config) { return config && !config.source_changed && !config.exclude && !config.skip_motion && config.radio_outline?.length === 4 && ['left_gimbal','right_gimbal'].every(side=>config[side]?.width >= 8 && config[side]?.height >= 8); }
function matchingCalibrationReady(config,sources) { return config && !config.source_changed && (config.exclude || config.skip_motion || ((!sources.stick_inputs || calibrated(config)) && (!sources.gimbal_activity || activityConfigured(config)))); }
function matchingReadiness(sources,radioInputs=state.job?.inputs.radio || [],fpvInputs=state.job?.inputs.fpv || [],strategy=$('matching-strategy').value) {
  if (!radioInputs.length || !fpvInputs.length) return {blocked:true,autoSeed:false,regionReady:0,preciseReady:0,message:'Include at least one FPV and one StickCam recording. Restore ignored videos in Video lists & match scope.'};
  const mode=evidenceMode(sources), active=radioInputs.filter(r=>!state.job.calibrations[r.name]?.exclude);
  const regionReady=active.filter(r=>activityConfigured(state.job.calibrations[r.name])).length;
  const preciseReady=active.filter(r=>{const c=state.job.calibrations[r.name];return c && !c.exclude && !c.skip_motion && calibrated(c);}).length;
  const motionReady=mode.motion && ((sources.gimbal_activity && regionReady>0)||(sources.stick_inputs && preciseReady>0));
  const audioPossible=mode.audio && [active,fpvInputs].every(records=>records.some(r=>r.metadata?.has_audio!==false));
  const parts=[];
  if(sources.gimbal_activity)parts.push(`Tip-free movement: ${regionReady}/${active.length} StickCam clips have saved radio regions.`);
  if(sources.stick_inputs)parts.push(`Derived inputs: ${preciseReady}/${active.length} clips have calibrated tips.`);
  if(mode.audio && !audioPossible)parts.push('Audio cannot be compared: one feed has no audio.');
  const autoSeed=strategy==='telemetry_seed' || (strategy==='automatic' && $('embedded-seed-enabled').checked && sources.fpv_gyro && sources.gimbal_activity && !sources.stick_inputs && fpvInputs.length>0 && fpvInputs.every(r=>r.metadata?.has_audio===false));
  const blocked=!audioPossible && !motionReady && !autoSeed;
  if(autoSeed)parts.push(`Radio regions will be found automatically, one StickCam at a time. Embedded motion seed → modified-date suggestions → ${$('seed-spot-checks').value} optional spot checks → review. No full-session StickCam derivation is required.`);
  if(blocked)parts.push('Save radio regions in Resolve calibration before running movement matching. No match or offset is available yet.');
  return {blocked,autoSeed,regionReady,preciseReady,message:parts.join(' ')};
}
function identifierTable(items) {
  const table = el('table', undefined, 'identifier-table'), body = document.createElement('tbody');
  for (const item of items) {
    const row = document.createElement('tr'), indicatorCell = document.createElement('td'), label = document.createElement('th'), value = document.createElement('td');
    const glyph = item.state === 'available' ? '✓' : item.state === 'missing' ? '✕' : item.state === 'warning' ? '!' : '·';
    const mark = el('span', glyph, `identifier-mark ${item.state}`); mark.setAttribute('aria-hidden', 'true');
    indicatorCell.append(mark);
    label.scope = 'row'; label.textContent = item.label;
    value.textContent = item.value;
    if (item.title) { row.title = item.title; value.title = item.title; }
    row.append(indicatorCell, label, value); body.append(row);
  }
  table.append(body);
  return table;
}
function audioIdentifiers(audio) {
  const stream = audio?.stream === 'present'
    ? {state: 'available', value: 'Present'}
    : audio?.stream === 'absent'
      ? {state: 'missing', value: 'No audio stream'}
      : {state: 'unknown', value: 'Not scanned'};
  let fingerprint;
  if (audio?.stream === 'absent') fingerprint = {state: 'missing', value: 'No audio stream'};
  else if (audio?.fingerprint === 'cached_useful') fingerprint = {state: 'available', value: 'Ready'};
  else if (audio?.fingerprint === 'cached_empty') fingerprint = {state: 'missing', value: 'No useful fingerprint'};
  else if (audio?.fingerprint === 'corrupt') fingerprint = {state: 'missing', value: 'Cache needs refresh'};
  else if (audio?.fingerprint === 'not_prepared') fingerprint = {state: 'unknown', value: 'Not prepared'};
  else fingerprint = {state: 'unknown', value: 'Not checked'};
  return [{label: 'Audio', ...stream}, {label: 'Audio fingerprint', ...fingerprint}];
}
function clockIdentifier(label, clock) {
  if (!clock?.display) return {label, state: clock ? 'missing' : 'unknown', value: clock ? 'Unavailable' : 'Not scanned'};
  return {label, state: clock.suspect ? 'warning' : 'available', value: `${clock.suspect ? 'Suspect · ' : ''}${clock.display}`, title: clock.suspect ? 'Timestamp is present but may be unreliable.' : clock.display};
}
function updateMatchTargetOptions() {
  if (!state.job) return;
  const kind = $('match-target-kind').value, select = $('match-target-name'), previous = select.value;
  const records = kind === 'radio' ? state.job.inputs.radio : kind === 'fpv' ? state.job.inputs.fpv : [];
  select.replaceChildren(...records.filter(record => kind !== 'radio' || !state.job.calibrations[record.name]?.exclude).map(record => {
    const option = el('option', clipLabel(record)); option.value = record.name; return option;
  }));
  if (records.some(record => record.name === previous)) select.value = previous;
  show('match-target-wrap', kind !== 'all');
  renderVideoLists();
}
let recordingScopeSaving = false;
async function setRecordingIgnored(kind, name, ignored) {
  if (!state.job || recordingScopeSaving) return;
  const jobId = state.job.id, token = state.jobToken;
  const panels = [...document.querySelectorAll('[data-major-panel]')].filter(p => p.open).map(p => p.id);
  const targets = {fpv:new Set(state.targets.fpv), radio:new Set(state.targets.radio)};
  recordingScopeSaving = true; renderVideoLists();
  try {
    await api(`/api/jobs/${jobId}/recordings/${kind}/${part(name)}/ignored`, {method:'PUT',json:{ignored}});
    if (token !== state.jobToken) return;
    await selectJob(jobId);
    if (state.job?.id !== jobId) return;
    for (const side of ['fpv','radio']) state.targets[side] = new Set([...targets[side]].filter(n => state.job.inputs[side].some(r => r.name === n)));
    for (const id of panels) if ($(id) && !$(id).classList.contains('hidden')) $(id).open = true;
    openPanel('video-list-panel');
    if (ignored) $('ignored-recordings').open = true;
    message('recording-scope-message', `${name} ${ignored ? 'ignored for this session. Its saved data and pairings are kept.' : 'restored to this session.'}`);
  } finally {
    recordingScopeSaving = false;
    renderVideoLists();
  }
}
function renderVideoLists() {
  if (!state.job) return;
  const mode = $('match-target-kind').value;
  for (const kind of ['fpv', 'radio']) {
    const records = state.job.inputs[kind], list = $(`${kind}-video-list`);
    const hash = JSON.stringify([mode, [...state.targets[kind]], records.map(r => [r.name, r.metadata?.duration, r.fpv_recorder, calibrated(state.job.calibrations[r.name]), !!state.job.calibrations[r.name]?.exclude, state.stickData?.clips.find(c => c.name === r.name), state.fpvData?.clips.find(c => c.name === r.name), state.job.calibrations[r.name]?.radio_rotation_deg, state.job.radio_orientation_checks?.[r.name]?.status, state.job.radio_orientation_checks?.[r.name]?.midpoint_rotation_deg]), state.results?.match_scope, state.results?.recorder_groups, activeAnalysis(), state.job.scope_editable, recordingScopeSaving]);
    if (list.dataset.hash === hash) continue;
    list.dataset.hash = hash; list.replaceChildren(); $(`${kind}-list-title`).textContent = `${kind === 'fpv' ? 'FPV' : 'StickCam'} videos (${records.length} included)`;
    for (const record of records) {
      const row = el('div', undefined, 'video-file-row'), header = el('label', undefined, 'video-file-header'), check = el('input'); check.type = 'checkbox'; check.checked = mode === 'all' || state.targets[kind].has(record.name); check.setAttribute('aria-label', `Select ${record.name}`);
      check.disabled = mode !== kind || activeAnalysis() || (kind === 'radio' && state.job.calibrations[record.name]?.exclude);
      check.onchange = () => { if (check.checked) state.targets[kind].add(record.name); else state.targets[kind].delete(record.name); updateSelectionSummary(); };
      const cached = state.stickData?.clips.find(c => c.name === record.name);
      const fpv = state.fpvData?.clips.find(c => c.name === record.name);
      const config = state.job.calibrations[record.name];
      const status = kind === 'radio' ? (config?.exclude ? 'Excluded' : config?.skip_motion ? 'Audio only' : cached?.status === 'cached' ? `Stick inputs saved${Number.isFinite(cached.usable_percent)?` · ${cached.usable_percent}% observed`:''}` : cached?.status === 'other_rate' ? `Saved at ${cached.sample_fps} Hz · needs ${cached.requested_fps} Hz` : calibrated(state.job.calibrations[record.name]) ? 'Calibrated · needs derivation' : 'Needs calibration') : bytes(record.size);
      const orientation = kind === 'radio' ? state.job.radio_orientation_checks?.[record.name] : null;
      const savedRotation = state.job.calibrations[record.name]?.radio_rotation_deg;
      const orientationText = savedRotation === 180 ? ' · inverted reference confirmed' : orientation ? ` · midpoint ${orientation.midpoint_rotation_deg === 180 ? 'inverted' : orientation.midpoint_rotation_deg === 0 ? 'upright' : 'unknown'}` : '';
      const group = kind==='fpv' ? state.results?.recorder_groups?.find(g=>g.confirmed_members.includes(record.name)) : null;
      const deferred = kind === 'fpv' ? state.results?.match_scope?.deferred_fpv : state.results?.match_scope?.deferred_radio;
      const outsideSample = (deferred || []).includes(record.name);
      const audio = kind === 'fpv' ? fpv?.audio : cached?.audio;
      const identifiers = kind === 'fpv' ? fpv : cached;
      const items = [...audioIdentifiers(audio)];
      if (kind === 'fpv') {
        const gyro = fpv?.embedded?.status;
        items.push({label: 'Embedded gyro', state: gyro === 'available' ? 'available' : gyro === 'unavailable' ? 'missing' : 'unknown', value: gyro === 'available' ? 'Found' : gyro === 'unavailable' ? 'Not found' : 'Not checked'});
        const visual = fpv?.visual?.status;
        items.push({label: 'Visual motion', state: visual === 'cached' ? 'available' : ['other_rate', 'older_algorithm'].includes(visual) ? 'warning' : ['unavailable', 'not_derived'].includes(visual) ? 'missing' : 'unknown', value: visual === 'cached' ? 'Saved' : visual === 'other_rate' ? 'Saved at another rate' : visual === 'older_algorithm' ? 'Older saved data' : visual === 'unavailable' ? 'Unavailable' : visual === 'not_derived' ? 'Not derived' : 'Not checked'});
      } else {
        items.push({label: 'Calibration', state: config?.exclude || config?.skip_motion || config?.auto_generated ? 'warning' : calibrated(config) || activityConfigured(config) ? 'available' : 'missing', value: config?.exclude ? 'Excluded' : config?.skip_motion ? 'Audio only' : config?.auto_generated ? 'Automatic regions · review' : calibrated(config) ? 'Ready' : activityConfigured(config) ? 'Radio regions ready' : 'Needs setup'});
        const stickStatus = cached?.status;
        items.push({label: 'Stick inputs', state: config?.exclude || config?.skip_motion || stickStatus === 'other_rate' ? 'warning' : stickStatus === 'cached' ? (cached.usable_percent < 10 ? 'warning' : 'available') : ['not_derived', 'needs_calibration', 'unavailable'].includes(stickStatus) ? 'missing' : 'unknown', value: config?.exclude ? 'Excluded' : config?.skip_motion ? 'Audio only' : stickStatus === 'cached' ? `Saved${Number.isFinite(cached.usable_percent)?` · ${cached.usable_percent}% observed`:''}` : stickStatus === 'other_rate' ? `Saved at ${cached.sample_fps} Hz` : stickStatus === 'unavailable' ? 'Unavailable' : stickStatus === 'needs_calibration' ? 'Needs setup' : stickStatus === 'not_derived' ? 'Not derived' : 'Not checked'});
        const activity=cached?.gimbal_activity;
        items.push({label:'Gimbal movement',state:activity?.status==='cached' ? (activity.usable_percent>0?'available':'warning') : ['not_derived','needs_calibration','unavailable'].includes(activity?.status)?'missing':'unknown',value:activity?.status==='cached'?`Saved · ${activity.usable_percent}% observed`:activity?.status==='not_derived'?'Not derived':activity?.status==='needs_calibration'?'Needs radio regions':activity?.status==='unavailable'?'Unavailable':'Not checked'});
        const overall=cached?.overall_movement;
        items.push({label:'Overall movement',title:'Generic foreground image change after camera-motion compensation; separate from detected controls.',state:overall?.status==='cached'?(overall.usable_percent>0?'available':'warning'):['not_derived','unavailable'].includes(overall?.status)?'missing':'unknown',value:overall?.status==='cached'?`Saved · ${overall.usable_percent}% observed · ${overall.sample_fps} Hz`:overall?.status==='not_derived'?'Not derived':overall?.status==='unavailable'?'Unavailable':'Not checked'});
      }
      items.push(clockIdentifier('Filename time', identifiers?.filename_time), clockIdentifier('Modified time', identifiers?.modified_time));
      header.append(check, el('span', record.name));
      const heading = el('div',undefined,'video-file-heading');
      const ignore = action('Ignore',guarded(()=>setRecordingIgnored(kind,record.name,true),'recording-scope-message'),'secondary recording-scope-button');
      ignore.setAttribute('aria-label',`Ignore ${kind==='fpv'?'FPV':'StickCam'} video ${record.name}`);
      ignore.disabled = recordingScopeSaving || state.job.scope_editable === false;
      ignore.title = 'Leave this video out of this session; keep its file and saved data';
      heading.append(header,ignore);
      const modified=identifiers?.modified_time?.timestamp ?? record.camera_modified_time ?? (record.metadata_signature?.[1]?record.metadata_signature[1]/1e9:null);
      row.append(heading,...(modified==null?[]:[el('small',`Modified: ${new Date(modified*1000).toLocaleString()}`,'video-file-summary')]), el('small', `${clipDuration(record)} · ${status}${kind==='fpv' ? ` · recorder: ${record.fpv_recorder || 'Unassigned'}${group?` · ${group.label}`:''}` : ''}${outsideSample ? ' · Not analyzed in last sample' : ''}${orientationText}`, 'video-file-summary'), identifierTable(items));
      list.append(row);
    }
  }
  const ignored = state.job.ignored_inputs || {fpv:[],radio:[]};
  const ignoredCount = ignored.fpv.length + ignored.radio.length;
  show('ignored-recordings', ignoredCount > 0);
  $('ignored-recordings-title').textContent = `Ignored recordings (${ignoredCount}) · restore when needed`;
  for (const kind of ['fpv','radio']) {
    const list=$(`ignored-${kind}-list`); list.replaceChildren();
    for (const record of ignored[kind]) {
      const row=el('div',undefined,'video-file-row ignored-recording-row'), heading=el('div',undefined,'video-file-heading');
      const restore=action('Restore',guarded(()=>setRecordingIgnored(kind,record.name,false),'recording-scope-message'),'secondary recording-scope-button');
      restore.setAttribute('aria-label',`Restore ${kind==='fpv'?'FPV':'StickCam'} video ${record.name}`);
      restore.disabled=recordingScopeSaving || state.job.scope_editable===false;
      heading.append(el('span',record.name),restore); row.append(heading,el('small',`${clipDuration(record)} · ignored in this session`,'video-file-summary')); list.append(row);
    }
    if (!ignored[kind].length) list.append(el('p','None','hint'));
  }
  updateSelectionSummary();
}
function updateSelectionSummary() {
  const kind = $('match-target-kind').value;
  const scope = Number($('match-sample-percent')?.value || 100);
  message('target-selection-summary', `${kind === 'all' ? 'Compare all FPV and StickCam recordings' : `${state.targets[kind].size} ${kind === 'fpv' ? 'FPV' : 'StickCam'} target(s) selected · compare with all available ${kind === 'fpv' ? 'StickCam' : 'FPV'} candidates`}. Initial pass: ${scope === 100 ? 'all files' : `${scope}% spread across each file list`}. Sampled runs finish that subset before presenting clock-and-duration suggestions; use 100% to enable stop-on-first-strong-match review.`);
  $('fpv-recorder-apply').disabled = kind!=='fpv' || !state.targets.fpv.size;
  for (const id of ['targets-select-all', 'targets-clear']) $(id).disabled = kind === 'all' || activeAnalysis();
  $('stop-on-confident-match').disabled = kind !== 'fpv' || activeAnalysis() || scope < 100;
}
function updateCalibrationCount() {
  if (!state.job) return;
  const count = state.job.inputs.radio.filter(record => calibrated(state.job.calibrations[record.name])).length;
  $('calibrated-count').textContent = `${count} / ${state.job.inputs.radio.length} configured`;
  const selected = $('calibration-clip').value;
  $('calibration-clip').replaceChildren(...state.job.inputs.radio.map(record => {
    const config = state.job.calibrations[record.name];
    const option = el('option', `${calibrated(config) ? '✓ ' : ''}${clipLabel(record)}${config?.exclude ? ' · excluded' : config?.skip_motion ? ' · audio only' : ''}`);
    option.value = record.name; return option;
  }));
  if (state.job.inputs.radio.some(record => record.name === selected)) $('calibration-clip').value = selected;
  refreshStickData().catch(error => message('stick-data-message', error.message, true));
}
function updateJobUI() {
  if (!state.job) return;
  const completeInputs = Object.values(state.job.inputs).flat().every(item => item.uploaded === item.size);
  show('inputs-panel', !completeInputs);
  show('calibration-panel', completeInputs);
  show('analysis-panel', completeInputs);
  show('fpv-preparation-panel', completeInputs);
  show('workflow-panel', true); renderWorkflow();
  if (completeInputs) refreshFPVData().catch(error => message('fpv-data-message', error.message, true));
  show('motion-panel', completeInputs);
  show('fingerprint-panel',completeInputs);fingerprintReview.render();
  show('video-list-panel', completeInputs);
  show('counterpart-panel', completeInputs); show('manual-pair-panel',completeInputs);manualBrowser.render();
  renderCounterpart();
  updateMotionOptions();
  $('analyze').disabled = activeAnalysis() || stickPreparation.running || fpvPreparation.running || !completeInputs;
  $('match-sample-percent').disabled = activeAnalysis();
  updateEvidenceSummary();
  $('analyze').textContent = ['failed', 'interrupted', 'cancelled', 'complete'].includes(state.job.status) ? 'Run analysis again (reuse cache)' : 'Analyze recordings';
  show('cancel-analysis', activeAnalysis());
  show('analysis-log', !['uploading', 'draft'].includes(state.job.status));
  $('analysis-log').href = path('/file/analysis.log');
  $('analysis-bar').value = state.job.progress?.percent || 0;
  renderExtractionProgress();
  message('analysis-message', state.job.message, state.job.status === 'failed');
  ['save-calibration', 'check-tracking', 'copy-regions'].forEach(id => $(id).disabled = activeAnalysis() || stickPreparation.running);
  updateRadioOrientationUI();
  $('stick-sampling-ratio').disabled = activeAnalysis() || stickPreparation.running || fpvPreparation.running;
  updateMarkTools();
  for (const id of ['auto-find-radio', 'auto-find-all']) $(id).disabled = activeAnalysis() || calibrationScan.running || !completeInputs;
  if (completeInputs) updateCalibrationCount();
  const latestScan = state.job.calibration_scans?.at(-1);
  if (latestScan && calibrationScan.id !== latestScan.id) watchCalibrationScan(latestScan.id, state.job.id, state.jobToken);
  updateMatchTargetOptions();
  const activeSegmentScan = state.job.segment_scans?.find(task => ['queued', 'running'].includes(task.status));
  if (activeSegmentScan && segmentScan.task !== activeSegmentScan.id) watchRelatedSegments(activeSegmentScan.id, state.job.id, state.jobToken);
  const early = state.job.early_proposal; show('early-proposal', early?.status === 'pending');
  if (early?.status === 'pending') message('early-proposal-evidence', `${early.candidate.fpv_video} ↔ ${early.candidate.radio_video} · offset ${offsetText(early.candidate.offset_seconds)} · ${early.remaining.length} StickCam candidates remain unscanned. ${sectionsText(early.candidate)}`);
  $('prepare').textContent = state.job.status === 'uploading' ? 'Resume uploads' : 'Prepare new session';
  if (state.job.status === 'uploading') {
    show('upload-progress');
    const items = Object.values(state.job.inputs).flat();
    const total = items.reduce((sum, item) => sum + item.size, 0), uploaded = items.reduce((sum, item) => sum + item.uploaded, 0);
    $('upload-bar').value = uploaded / total * 100;
    message('upload-message', `${bytes(uploaded)} / ${bytes(total)} uploaded. Reselect unfinished files to resume.`);
  }
}
async function history() {
  const data = await api('/api/jobs');
  $('history').replaceChildren();
  if (!data.jobs.length) $('history').append(el('span', 'Your sessions will appear here.', 'muted'));
  for (const job of data.jobs) {
    const button = action('', guarded(() => selectJob(job.id), 'input-error'), `secondary${state.job?.id === job.id ? ' active' : ''}`);
    if (job.thumbnail_url) {
      const image = el('img', undefined, 'session-thumbnail'); image.src = job.thumbnail_url; image.alt = `FPV frame from ${job.label}`; image.loading = 'lazy'; image.decoding = 'async';
      image.onerror = () => image.replaceWith(el('span', 'FPV thumbnail unavailable', 'session-thumbnail-placeholder'));
      button.append(image);
    }
    button.append(el('span', job.label, 'session-title'));
    button.append(el('small', `${job.radio_count} StickCam / ${job.fpv_count} FPV${job.ignored_count ? ` · ${job.ignored_count} ignored` : ''} · ${job.status}`));
    button.disabled = state.busy; $('history').append(button);
  }
  const deleted = await api('/api/deleted-sessions');
  show('deleted-sessions', deleted.sessions.length > 0); $('deleted-session-list').replaceChildren();
  for (const session of deleted.sessions) $('deleted-session-list').append(action(`Restore ${session.label}`, guarded(async () => {
    await api(`/api/jobs/${session.id}/restore`, {method:'POST'}); await history(); await selectJob(session.id);
    message('session-message', 'Session restored; exports were kept.');
  }, 'session-message')));
}
function clearSession() {
  fingerprintReview.reset();
  manualBrowser.reset();
  clearFlightPreview();
  for(const id of ['save-project','save-project-bundle'])show(id,false);$('session-backup-list').replaceChildren();show('installation-details',false);$('export-destination').value='';
  rememberCalibrationDraft();
  cancelGimbalSuggestion();
  segmentScan.token++; clearTimeout(segmentScan.timer); segmentScan.running = false; segmentScan.task = null;
  show('related-segments', false);
  fpvPreparation.token++; clearTimeout(fpvPreparation.timer); fpvPreparation.key = '';
  fpvPreparation.running = false; fpvPreparation.task = null; fpvPreparation.selected.clear(); state.fpvData = null;
  resetFPVVisualReview();
  liveStickPreview=null; show('stick-live-preview',false);
  $('fpv-data-rows').replaceChildren(); $('fpv-data-log').replaceChildren();
  show('fpv-data-progress', false); show('fpv-preparation-cancel', false);
  resetMotionReview();
  stickPreparation.token++; clearTimeout(stickPreparation.timer); stickPreparation.key = '';
  stickPreparation.running = false; stickPreparation.task = null; state.stickData = null;
  show('derive-sticks-cancel', false); show('stick-progress-summary', false); $('stick-data-log').replaceChildren();
  calibrationScan.token++; clearTimeout(calibrationScan.timer); calibrationScan.id = null; calibrationScan.rows = []; calibrationScan.running = false;
  radioOrientation.token++; clearTimeout(radioOrientation.timer); radioOrientation.running = false; radioOrientation.result = null;
  $('session-radio-label').value = 'auto'; renderRadioShape(); show('radio-orientation-result', false); show('radio-orientation-progress', false); show('cancel-radio-orientation', false);
  state.calibrationName = state.calibrationProposalId = null; state.calibrationDrafts = {};
  state.calibrationRotationOverride = null; $('stickcam-mode').value = 'wonky';
  show('auto-calibration-review', false); show('auto-find-cancel', false); show('auto-calibration-progress', false); message('auto-calibration-message', '');
  show('inputs-panel'); show('delete-session-confirmation', false);
  for (const panel of document.querySelectorAll('[data-major-panel]')) panel.open = true;
  state.jobToken++; state.calToken++; state.viewerToken++;
  state.frameController?.abort(); state.frameToken++;
  clearTimeout(state.pollTimer); for (const timer of state.exportTimers.values()) clearTimeout(timer); state.exportTimers.clear();
  releaseAllPreviews(); pausePair(); $('cal-video').pause(); $('cal-video').removeAttribute('src'); $('cal-video').load();
  resetStickReviewState();
  state.job = state.results = state.pair = state.calibration = state.metadata = null;
  state.calibrationFrameReady = false;
  counterpartSearch.starting = false; counterpartSearch.candidatesKey = null;
  $('counterpart-fpv').replaceChildren(); delete $('counterpart-fpv').dataset.optionsKey;
  $('counterpart-method').value = 'automatic'; $('counterpart-visual').checked = true; $('counterpart-show-all').checked = false;
  $('counterpart-rows').replaceChildren(); $('counterpart-notes').replaceChildren(); message('counterpart-message', '');
  show('counterpart-panel', false); show('manual-pair-panel',false);$('manual-pair-panel').open=false;
  show('workflow-panel', false);
  for (const panel of document.querySelectorAll('.technical-panel')) { panel.classList.remove('guided-open'); panel.open = false; }
  state.targets = {fpv: new Set(), radio: new Set()};
  storage.removeItem('fpv-session');
  show('rename-session-controls', false); $('rename-session-label').value = ''; message('session-message', '');
  for (const id of ['fpv-preparation-panel', 'video-list-panel', 'fingerprint-panel', 'motion-panel', 'calibration-panel', 'analysis-panel', 'results-panel', 'viewer-panel', 'portrait-panel', 'exports-panel', 'diagnostics-panel', 'upload-progress']) show(id, false);
  message('input-error', ''); $('prepare').textContent = 'Prepare session';
}
function loadRateProfile(profile) {
  $('use-fpv-rates').checked = !!profile;
  const model = profile?.model || 'actual';
  $('rates-model').value = model;
  for (const axis of ['roll', 'pitch', 'yaw']) {
    const values = profile?.[axis] || {};
    const actual = model === 'actual' ? values : {}, legacy = model === 'betaflight' ? values : {}, kiss = model === 'kiss' ? values : {};
    $(`${axis}-center-dps`).value = actual.center_dps ?? 70;
    $(`${axis}-max-dps`).value = actual.max_dps ?? 670;
    $(`${axis}-expo-actual`).value = actual.expo ?? 0;
    $(`${axis}-rc-rate`).value = legacy.rc_rate ?? 100;
    $(`${axis}-super-rate`).value = legacy.super_rate ?? 70;
    $(`${axis}-expo-legacy`).value = legacy.expo ?? 0;
    $(`${axis}-kiss-rc-rate`).value = kiss.rc_rate ?? 0.7;
    $(`${axis}-kiss-rate`).value = kiss.rate ?? 0.7;
    $(`${axis}-kiss-curve`).value = kiss.rc_curve ?? 0.4;
  }
  show('actual-rates', model === 'actual'); show('legacy-rates', model === 'betaflight'); show('kiss-rates', model === 'kiss');
  scheduleRateTotals();
}
function readRateProfile(force = false) {
  if (!force && !$('use-fpv-rates').checked) return null;
  const model = $('rates-model').value, profile = { model };
  for (const axis of ['roll', 'pitch', 'yaw']) {
    profile[axis] = model === 'actual'
      ? { center_dps: Number($(`${axis}-center-dps`).value), max_dps: Number($(`${axis}-max-dps`).value), expo: Number($(`${axis}-expo-actual`).value) }
      : model === 'betaflight'
        ? { rc_rate: Number($(`${axis}-rc-rate`).value), super_rate: Number($(`${axis}-super-rate`).value), expo: Number($(`${axis}-expo-legacy`).value) }
        : { rc_rate: Number($(`${axis}-kiss-rc-rate`).value), rate: Number($(`${axis}-kiss-rate`).value), rc_curve: Number($(`${axis}-kiss-curve`).value) };
  }
  return profile;
}
$('rates-model').onchange = () => { show('actual-rates', $('rates-model').value === 'actual'); show('legacy-rates', $('rates-model').value === 'betaflight'); show('kiss-rates', $('rates-model').value === 'kiss'); scheduleRateTotals(); };
$('use-fpv-rates').onchange = () => { /* Values are retained while the optional mapping is disabled. */ };
let rateTotalsTimer = null, rateTotalsToken = 0;
for (const tableId of ['actual-rates', 'legacy-rates', 'kiss-rates']) {
  const table = $(tableId).querySelector('table');
  table.querySelector('thead tr').append(el('th', 'Calculated max (°/s)'));
  [...table.querySelectorAll('tbody tr')].forEach((row, index) => {
    const cell = el('td'), output = el('output', '—', 'rate-total');
    output.id = `${tableId}-${['roll', 'pitch', 'yaw'][index]}-total`; cell.append(output); row.append(cell);
  });
  table.addEventListener('input', scheduleRateTotals);
}
function scheduleRateTotals() {
  clearTimeout(rateTotalsTimer); const token = ++rateTotalsToken;
  for (const output of document.querySelectorAll('.rate-total')) output.textContent = '—';
  rateTotalsTimer = setTimeout(async () => {
    try {
      const profile = readRateProfile(true);
      const result = await api('/api/rates-preview', {method:'POST', json:{profile}});
      if (token !== rateTotalsToken) return;
      const tableId = {actual:'actual-rates', betaflight:'legacy-rates', kiss:'kiss-rates'}[profile.model];
      for (const axis of ['roll','pitch','yaw']) $(`${tableId}-${axis}-total`).textContent = `${result.maximum_dps[axis].toLocaleString(undefined,{maximumFractionDigits:1})} °/s`;
      message('rate-totals-message', 'Maximum rotation speed at full stick deflection, calculated using the matching rate curve.');
    } catch(error) { if (token === rateTotalsToken) message('rate-totals-message', error.message, true); }
  },180);
}
scheduleRateTotals();

async function selectJob(id) {
  clearSession(); const token = state.jobToken;
  const job = await api(`/api/jobs/${id}`); if (token !== state.jobToken) return;
  if (job.deleted_at) { await history(); message('session-message', 'This session was deleted from the frontend; use Deleted sessions to restore it.'); return; }
  state.job = job; $('matching-strategy').value = job.settings.matching_strategy || 'automatic';$('embedded-seed-enabled').checked=job.settings.embedded_seed_enabled!==false;$('seed-spot-checks').value=String(job.settings.seed_spot_checks??2); $('match-sample-percent').value = String(job.settings.match_sample_percent || 25); $('audio-boundary').value=String(job.settings.audio_boundary || 30); $('audio-adaptive').checked=job.settings.audio_adaptive!==false; $('audio-retry-limit').value=String(job.settings.audio_retry_limit || 120); state.source = job.source; storage.setItem('fpv-session', id);
  $('session-radio-label').value = job.radio_profile?.model_id || (job.radio_profile ? 'other' : 'auto'); renderRadioShape();
  $('rename-session-label').value = job.label || ''; show('rename-session-controls');
  setSource(job.source); updateJobUI();
  $('export-destination').value=job.export_destination || '';
  for(const [id,suffix] of [['save-project',''],['save-project-bundle','?fingerprints=true']]){$(id).href=path('/project')+suffix;show(id);}
  if(job.recovery_required){message('session-message',`Session JSON needs recovery. Available backup: ${job.recovery_required}. Open Session backups and restore before processing.`,true);return;}
  const oldDefaults = {sample_fps:15, match_fps:7.5, min_overlap:5}, newDefaults = {sample_fps:30, match_fps:30, min_overlap:2};
  for (const [key, saved] of Object.entries(job.settings)) {
    const value = !job.settings_defaults_version && saved === oldDefaults[key] ? newDefaults[key] : saved;
    const input = $(key.replaceAll('_', '-')); if (input) { if (input.type === 'checkbox') input.checked = value; else input.value = value; }
  }
  $('stick-sampling-ratio').value = job.settings.stick_sampling_ratio == null ? '' : String(job.settings.stick_sampling_ratio);
  $('fpv-preparation-mode').value = job.settings.fpv_sampling_mode || 'auto';
  $('fpv-preparation-fps').value = job.settings.fpv_visual_fps || 30;
  $('visual-horizontal-fov').value = job.settings.visual_camera?.horizontal_fov_deg || 110;
  $('visual-projection').value = job.settings.visual_camera?.projection || 'rectilinear';
  $('visual-track-full-rate').checked = job.settings.visual_camera?.track_full_rate !== false;
  $('tracking-backend').value = job.settings.tracking_backend || 'auto';
  updateFPVSamplingUI();
  loadRateProfile(job.settings.rate_profile);
  selectSavedRatePreset(job.settings.rate_profile);
  loadEvidenceSources(job.settings);
  if (job.settings.match_target_kind !== 'all' && job.settings.match_target_kind) state.targets[job.settings.match_target_kind] = new Set(job.settings.match_target_names || (job.settings.match_target_name ? [job.settings.match_target_name] : []));
  updateMatchTargetOptions();
  refreshStickData(true).catch(error => message('stick-data-message', error.message, true));
  refreshFPVData(true).catch(error => message('fpv-data-message', error.message, true));
  await history();
  $('sessions-panel').open = false; $('video-list-panel').open = false;
  if (!job.settings.matching_strategy) $('use-fpv-visual').checked = false;
  if (job.has_results) await loadResults();
  if (job.exports?.length) renderExports();
  if ($('technical-tools').checked && $('calibration-panel').open) await ensureCalibrationLoaded();
  if (activeAnalysis()) pollJob(id, token);
  loadClipDurations(id, token).catch(error => message('input-error', error.message, true));
}
async function loadClipDurations(jobId, token) {
  const records = Object.entries(state.job.inputs).flatMap(([kind, items]) => items.filter(r => r.uploaded === r.size).map(record => ({kind, record})));
  let index = 0, refreshed = 0;
  const render = () => { updateMotionOptions(); updateMatchTargetOptions(); updateCalibrationCount(); updateStickSamplingPreview(); renderFPVData(); };
  async function worker() {
    while (token === state.jobToken && index < records.length) {
      const {kind, record} = records[index++];
      try {
        const metadata = await api(`/api/jobs/${jobId}/metadata/${kind}/${part(record.name)}`);
        if (token !== state.jobToken) return;
        record.metadata = metadata;
        if (Date.now() - refreshed > 500) { refreshed = Date.now(); render(); }
      } catch(error) { record.durationError = error.message; }
    }
  }
  await Promise.all([worker(),worker()]);
  if (token === state.jobToken) { render(); manualBrowser.render();await refreshFPVData(true);if(state.job.has_results)await loadResults(); }
}
function setSource(source) {
  state.source = source; show('local-fields', source === 'local'); show('upload-fields', source === 'upload');
  $('source-local').classList.toggle('active', source === 'local'); $('source-upload').classList.toggle('active', source === 'upload');
}
let folderTarget = null, folderPath = null, folderRequestToken = 0;
async function browseFolder(path) {
  const token = ++folderRequestToken; folderPath = null; $('folder-select').disabled = true;
  $('folder-current-path').textContent = path || 'Your home folder';
  $('folder-parent').disabled = !path || path === '/';
  $('folder-video-count').textContent = '';
  $('folder-entries').replaceChildren();
  message('folder-picker-message', 'Loading folder…');
  const query = path ? `?${new URLSearchParams({ path })}` : '';
  const listing = await api(`/api/browse${query}`);
  if (token !== folderRequestToken || !$('folder-picker').open) return;
  folderPath = listing.path; $('folder-select').disabled = false;
  const shares = $('folder-network-buttons');
  shares.replaceChildren();
  for (const share of listing.network_shares || []) {
    const button = action(share.name, guarded(() => browseFolder(share.path), 'folder-picker-message'), 'secondary');
    button.title = share.path;
    shares.append(button);
  }
  show('folder-network-shares', shares.childElementCount > 0);
  $('folder-current-path').textContent = listing.path;
  $('folder-parent').disabled = !listing.parent;
  $('folder-parent').onclick = guarded(() => browseFolder(listing.parent), 'folder-picker-message');
  $('folder-video-count').textContent = `${listing.video_count} supported video${listing.video_count === 1 ? '' : 's'} in this folder`;
  if (!listing.entries.length) $('folder-entries').append(el('p', 'No folders or supported videos here.', 'muted'));
  for (const entry of listing.entries) {
    const row = el('div', undefined, `folder-entry${entry.kind === 'video' ? ' folder-video' : ''}`);
    row.setAttribute('role', 'listitem');
    row.append(el('span', entry.kind === 'folder' ? '▸' : '▰', 'folder-entry-icon'));
    row.append(el('span', entry.name, 'folder-entry-name'));
    if (entry.kind === 'folder') {
      const button = action('Open', guarded(() => browseFolder(entry.path), 'folder-picker-message'), 'secondary');
      row.append(button);
    } else {
      row.append(el('small', bytes(entry.size)));
    }
    $('folder-entries').append(row);
  }
  message('folder-picker-message', folderTarget?.id==='export-destination'?'Choose a destination for aligned exports.': listing.truncated ? 'Showing the first 500 folders and videos. Narrow the path to see more.' : `Choose a folder containing ${folderTarget?.id === 'fpv-path' ? 'FPV' : 'StickCam'} recordings.`);
}
for (const button of document.querySelectorAll('.browse-folder')) button.onclick = guarded(async () => {
  folderTarget = $(button.dataset.folderField);
  const purpose = folderTarget.id === 'export-destination' ? 'export output' : folderTarget.id === 'fpv-path' || (folderTarget.id === 'relink-path' && $('relink-kind').value === 'fpv') ? 'FPV video' : 'StickCam video';
  $('folder-picker-title').textContent = `Choose ${purpose}${folderTarget.id==='export-destination'?' folder':' source folder'}`;
  $('folder-purpose').textContent = `Selecting ${purpose} footage`;
  $('folder-picker').showModal();
  const currentPath = folderTarget.value.trim();
  const fpvPath = folderTarget.id === 'radio-path' ? $('fpv-path').value.trim() : '';
  await browseFolder(currentPath || fpvPath || undefined);
}, 'folder-picker-message');
$('folder-picker-close').onclick = () => $('folder-picker').close();
$('folder-select').onclick = () => {
  if (!folderTarget || !folderPath) return;
  folderTarget.value = folderPath;
  $('folder-picker').close();
  folderTarget.focus();
};
for (const kind of ['fpv', 'radio']) $(kind + '-files').addEventListener('change', () => {
  const files = [...$(kind + '-files').files]; message(kind + '-count', `${files.length} clips · ${bytes(files.reduce((sum, file) => sum + file.size, 0))}`);
});
$('source-upload').onclick = () => setSource('upload'); $('source-local').onclick = () => setSource('local');
$('new-session').onclick = guarded(async () => { clearSession(); loadEvidenceSources(); applyDefaultRatePreset(); await history(); }, 'input-error');
$('rename-session').onclick = guarded(async () => {
  if (!state.job) throw new Error('Choose a session first.');
  const label = $('rename-session-label').value.trim();
  const renamed = await api(`/api/jobs/${state.job.id}`, { method: 'PATCH', json: { label } });
  state.job = { ...state.job, ...renamed };
  $('rename-session-label').value = renamed.label;
  await history(); message('session-message', `Session renamed to “${renamed.label}”.`);
}, 'session-message');
$('prepare').onclick = guarded(async () => {
  message('input-error', ''); setBusy(true);
  try {
    const files = { fpv: [...$('fpv-files').files], radio: [...$('radio-files').files] };
    if (!state.job || state.job.status !== 'uploading' || state.job.source !== 'upload' || state.source !== 'upload') {
      let body;
      if (state.source === 'local') body = { source: 'local', fpv_path: $('fpv-path').value.trim(), radio_path: $('radio-path').value.trim() };
      else {
        if (!files.fpv.length || !files.radio.length) throw new Error('Choose recordings of both types.');
        body = { source: 'upload', files: Object.fromEntries(Object.entries(files).map(([kind, list]) => [kind, list.map(file => ({ name: file.name, size: file.size, last_modified: file.lastModified }))])) };
      }
      body.label = $('session-label').value.trim();
      const job = await api('/api/jobs', { method: 'POST', json: body }); await selectJob(job.id);
    }
    if (state.job.source === 'upload' && state.job.status === 'uploading') await uploadFiles(files);
  } finally { setBusy(false); show('pause-upload', false); }
}, 'input-error');
async function uploadFiles(files) {
  const id = state.job.id, token = state.jobToken;
  const controller = new AbortController(); state.uploadController = controller;
  const maps = Object.fromEntries(Object.entries(files).map(([kind, items]) => [kind, new Map(items.map(file => [file.name, file]))]));
  const records = Object.values(state.job.inputs).flat(), total = records.reduce((sum, record) => sum + record.size, 0);
  for (const kind of ['fpv', 'radio']) for (const record of state.job.inputs[kind]) {
    if (record.uploaded >= record.size) continue;
    const file = maps[kind].get(record.name);
    if (!file || file.size !== record.size) throw new Error(`Reselect the original ${record.name} (${bytes(record.size)}) to resume.`);
  }
  show('pause-upload'); show('upload-progress');
  for (const kind of ['fpv', 'radio']) for (const record of state.job.inputs[kind]) {
    const file = maps[kind].get(record.name);
    while (record.uploaded < record.size) {
      if (controller.signal.aborted || token !== state.jobToken) throw new DOMException('Paused', 'AbortError');
      const start = record.uploaded, end = Math.min(record.size, start + 8 * 1024 ** 2);
      message('upload-message', `${record.name} · ${bytes(start)} / ${bytes(record.size)}`);
      try {
        const result = await api(`/api/jobs/${id}/upload/${kind}/${part(record.name)}`, { method: 'PUT', body: file.slice(start, end), signal: controller.signal,
          headers: { 'Content-Type': 'application/octet-stream', 'Content-Range': `bytes ${start}-${end - 1}/${record.size}` } });
        record.uploaded = result.uploaded;
      } catch (error) {
        if (error.name === 'AbortError') { message('upload-message', 'Upload paused. This session can be resumed.'); throw error; }
        // If the reply was lost after committing a chunk, discover the checkpoint.
        const latest = await api(`/api/jobs/${id}`);
        const saved = latest.inputs[kind].find(item => item.name === record.name).uploaded;
        if (saved > start) record.uploaded = saved; else throw error;
      }
      $('upload-bar').value = records.reduce((sum, item) => sum + item.uploaded, 0) / total * 100;
    }
  }
  message('upload-message', 'Uploads complete. Originals are kept in this saved session.');
  state.uploadController = null;
  await selectJob(id);
}
$('pause-upload').onclick = () => state.uploadController?.abort();

// Per-recording calibration uses a decoded still, including formats the browser
// cannot play. Motion preview requests short compatible chunks when necessary.
function emptyCalibration() { return { stickcam_mode: 'wonky', stick_mode: state.job?.settings?.stick_mode || 2, reference_time: 0, signs: { roll: 1, pitch: 1, yaw: 1, throttle: 1 } }; }
function rememberCalibrationDraft() {
  if (!state.calibrationName || !state.calibration || stickReview.framePurpose === 'correction') return;
  const config = clone(state.calibration);
  if ($('exclude-clip').checked) config.exclude = true; else delete config.exclude;
  if ($('skip-motion').checked) config.skip_motion = true; else delete config.skip_motion;
  for (const side of ['left_gimbal', 'right_gimbal']) if (config[side]) Object.assign(config[side], { threshold: Number($('tip-threshold').value), polarity: $('tip-polarity').value });
  state.calibrationDrafts[state.calibrationName] = { config, proposalId: state.calibrationProposalId };
  const dirty = JSON.stringify(config) !== JSON.stringify(state.job?.calibrations[state.calibrationName] || emptyCalibration());
  show('calibration-dirty', dirty); show('discard-calibration-draft', dirty);
  try { const key = calibrationDraftKey(state.calibrationName); if (dirty && state.metadata?.source_signature) storage.setItem(key, JSON.stringify({config, proposalId:state.calibrationProposalId, signature:state.metadata.source_signature})); else if (!dirty) storage.removeItem(key); } catch {}
}
async function loadCalibration(name) {
  if (!name || !state.job) return;
  if (calibrationLoad?.name === name && calibrationLoad.jobToken === state.jobToken) return calibrationLoad.promise;
  if (stickReview.frameEdits) { $('calibration-clip').value = state.calibrationName; throw new Error('Add or discard the current frame correction before reloading or changing clips.'); }
  if (stickReview.name && stickReview.name !== name) {
    if (stickReview.frameEdits) { $('calibration-clip').value = stickReview.name; throw new Error('Add or discard the current frame correction before changing clips.'); }
    if (stickReview.dirty) await saveStickReview();
    rememberCalibrationDraft(); state.calibration = null;
    resetStickReviewState();
  }
  cancelGimbalSuggestion();
  const loading = {name, jobToken: state.jobToken};
  loading.promise = loadCalibrationFrame(name);
  calibrationLoad = loading;
  try { await loading.promise; }
  finally { if (calibrationLoad === loading) { calibrationLoad = null; updateMarkTools(); } }
}
async function ensureCalibrationLoaded() {
  if (!state.job || !state.job.inputs.radio.length) return;
  let name = $('calibration-clip').value;
  if (!state.calibrationName && state.job.calibrations[name]?.exclude) name = state.job.inputs.radio.find(r => !state.job.calibrations[r.name]?.exclude)?.name || name;
  $('calibration-clip').value = name;
  if (state.calibrationName !== name || !state.metadata || !state.calibrationFrameReady) await loadCalibration(name);
}
async function loadCalibrationFrame(name) {
  openPanel('calibration-panel');
  if (!name || !state.job) return;
  rememberCalibrationDraft();
  $('calibration-clip').value = name;
  const token = ++state.calToken, jobToken = state.jobToken;
  state.frameController?.abort();
  $('cal-video').pause(); show('cal-video', false); show('cal-image'); show('cal-pause', false); show('roi-canvas'); show('tracking-check', false);
  $('cal-stage').classList.remove('watching');
  state.calibrationName = name; state.calibrationProposalId = state.calibrationDrafts[name]?.proposalId || null;
  stickReview.framePurpose = 'initial'; stickReview.frameEdits = false;
  state.tool = state.drag = null; state.calibration = clone(state.calibrationDrafts[name]?.config || state.job.calibrations[name] || emptyCalibration());
  state.calibrationRotationOverride = null;
  $('radio-rotation-deg').value = String(state.calibration.radio_rotation_deg || 0);
  $('suggest-radio-orientation').checked = !state.calibration.radio_orientation_confirmed && !state.calibration.radio_orientation_manual;
  renderStickMode();
  radioOrientation.token++; clearTimeout(radioOrientation.timer); radioOrientation.running = false; radioOrientation.result = null;
  show('radio-orientation-result', false); show('radio-orientation-progress', false); show('cancel-radio-orientation', false);
  $('stickcam-mode').value = state.calibration.stickcam_mode === 'fixed' ? 'fixed' : 'wonky'; $('skip-motion').checked = !!state.calibration.skip_motion;
  $('exclude-clip').checked = !!state.calibration.exclude;
  $('reference-time').value = state.calibration.reference_time || 0;
  const spec = state.calibration.left_gimbal;
  $('tip-radius').value = spec?.tip_radius || 10; $('tip-threshold').value = spec?.threshold ?? 70; $('tip-polarity').value = spec?.polarity || 'dark';
  message('calibration-message', `Loading ${name}…`);
  state.metadata = null; state.calibrationFrameReady = false; message('cal-duration', '');
  updateMarkTools();
  let info;
  try { info = await api(path(`/metadata/radio/${part(name)}`)); }
  catch (error) {
    if (token !== state.calToken || jobToken !== state.jobToken) return;
    show('cal-stage', false); updateMarkTools();
    message('calibration-message', `Could not read this clip: ${error.message}. Select “Exclude this clip” and save to continue with the rest of the batch.`, true); return;
  }
  if (token !== state.calToken || jobToken !== state.jobToken) return;
  state.metadata = info;
  try { const saved = JSON.parse(storage.getItem(calibrationDraftKey(name)) || 'null'); if (saved && JSON.stringify(saved.signature) === JSON.stringify(info.source_signature) && !state.calibrationDrafts[name]) { state.calibrationDrafts[name] = saved; state.calibration = clone(saved.config); state.calibrationProposalId = saved.proposalId; $('reference-time').value = saved.config.reference_time || 0; $('stickcam-mode').value = saved.config.stickcam_mode || 'wonky'; $('radio-rotation-deg').value = String(saved.config.radio_rotation_deg || 0); $('skip-motion').checked = !!saved.config.skip_motion; $('exclude-clip').checked = !!saved.config.exclude; } else if (saved && JSON.stringify(saved.signature) !== JSON.stringify(info.source_signature)) storage.removeItem(calibrationDraftKey(name)); } catch {}
  rememberCalibrationDraft(); message('cal-duration', `Duration ${time(info.duration)} · ${info.fps.toFixed(2)} fps`);
  const record = state.job.inputs.radio.find(r => r.name === name); if (record) record.metadata = info;
  updateStickSamplingPreview();
  show('cal-stage');
  $('reference-time').max = Math.max(0, info.duration - 1 / (info.fps || 30));
  $('cal-stage').style.aspectRatio = `${info.width} / ${info.height}`;
  $('roi-canvas').width = info.width; $('roi-canvas').height = info.height;
  await showFrame(false, token);
  if (token !== state.calToken || jobToken !== state.jobToken) return;
  updateMarkTools();
  message('calibration-message', state.calibrationProposalId ? 'Automatic suggestion pending review. Check all markers and confirm when ready.' : state.calibration.exclude ? 'This recording is excluded from analysis.' : state.calibration.skip_motion ? 'This clip will use boundary audio and any established camera-clock evidence.' :
    calibrated(state.calibration) ? state.job.calibrations[name] ? 'Review the markers. Confirm to save any changes.' : 'Unsaved calibration draft. Check tracking, then confirm.' : 'Draw both regions, then click the tips. Purple = left; cyan = right.');
  updateStickWorkspace();
  if (stickReview.name !== name || !stickReview.data) openStickReview().catch(error => message('stick-review-message', error.message, true));
}
async function showFrame(reset = true, token = state.calToken) {
  if (!state.metadata) return;
  const at = Number($('reference-time').value);
  if (!Number.isFinite(at) || at < 0 || at >= state.metadata.duration) throw new Error('Choose a reference frame inside the clip.');
  if (stickReview.frameEdits && Math.abs(at - state.calibration.reference_time) > .01) throw new Error('Add or discard the current frame correction before moving to another frame.');
  if (!$('cal-video').classList.contains('hidden') && calibrated(state.job.calibrations[state.calibrationName])) { rememberCalibrationDraft(); stickReview.framePurpose = 'correction'; }
  $('cal-video').pause(); show('cal-video', false); show('cal-image'); show('cal-pause', false); show('roi-canvas');
  $('cal-stage').classList.remove('watching');
  const nextCalibration = clone(state.calibration);
  if (reset && Math.abs(at - nextCalibration.reference_time) > .01) {
    nextCalibration.radio_orientation_confirmed = false;
    for (const side of ['left_gimbal', 'right_gimbal']) {
      if (nextCalibration.stickcam_mode === 'wonky') delete nextCalibration[side];
      else if (nextCalibration[side]) { delete nextCalibration[side].tip_x; delete nextCalibration[side].tip_y; }
    }
    delete nextCalibration.radio_outline;
  }
  nextCalibration.reference_time = at;
  const name = $('calibration-clip').value;
  const url = path(`/frame/radio/${part(name)}?at=${at}`);
  state.frameController?.abort();
  state.calibrationFrameReady = false; updateMarkTools();
  $('cal-stage').setAttribute('aria-busy', 'true');
  message('calibration-message', `Loading frame at ${time(at, true)}…`);
  const controller = new AbortController(), frameToken = ++state.frameToken;
  state.frameController = controller;
  let blob;
  try {
    const response = await fetch(url, { signal: controller.signal });
    if (!response.ok) { const error = await response.json(); throw new Error(error.detail || 'Could not load that frame.'); }
    blob = await response.blob();
  } catch (error) {
    if (controller.signal.aborted || token !== state.calToken || frameToken !== state.frameToken) return;
    $('cal-stage').setAttribute('aria-busy', 'false');
    throw error;
  }
  if (token !== state.calToken || frameToken !== state.frameToken) return;
  const objectURL = URL.createObjectURL(blob), previousURL = state.frameURL;
  $('cal-image').src = objectURL;
  await $('cal-image').decode();
  if (previousURL) URL.revokeObjectURL(previousURL);
  state.frameURL = objectURL;
  if (token !== state.calToken || frameToken !== state.frameToken) return;
  state.calibration = nextCalibration;
  if (stickReview.framePurpose === 'correction') seedStickCorrection(at);
  state.calibrationFrameReady = true; updateMarkTools();
  $('cal-stage').setAttribute('aria-busy', 'false');
  if (reset) message('calibration-message', `Showing frame at ${time(at, true)}. Review the markers before confirming.`);
  $('radio-rotation-deg').value = String(nextCalibration.radio_rotation_deg || 0);
  renderStickMode();
  drawRegions(); updateStickWorkspace(); stickReviewPositionChanged();
  updateRadioOrientationUI();
  if (stickReview.framePurpose !== 'correction' && state.job.radio_profile && !activeAnalysis()) checkRadioOrientation(false, true).catch(error => message('radio-orientation-message', error.message, true));
}
$('calibration-clip').onchange = guarded(() => loadCalibration($('calibration-clip').value), 'calibration-message');
async function loadRadioModels() {
  const data = await api('/api/radio-models'); radioModels = data.models;
  const select = $('session-radio-label'), previous = select.value;
  select.replaceChildren();
  for (const [value, label] of [['auto', 'Auto-detect · confirm a model'], ['other', 'Other']]) { const option = el('option', label); option.value = value; select.append(option); }
  for (const model of radioModels) { const option = el('option', `${model.label}${model.variants.length ? ` · ${model.variants.join(' / ')}` : ''}`); option.value = model.id; select.append(option); }
  select.value = previous || 'auto'; renderRadioShape();
}
const STICK_MODES = {
  1: ['Yaw', 'Pitch', 'Roll', 'Throttle'], 2: ['Yaw', 'Throttle', 'Roll', 'Pitch'],
  3: ['Roll', 'Pitch', 'Yaw', 'Throttle'], 4: ['Roll', 'Throttle', 'Yaw', 'Pitch']
};
function renderStickMode() {
  const mode = state.calibration?.stick_mode || state.job?.settings?.stick_mode || 2;
  $('radio-stick-mode').value = String(mode);
  const [lx,ly,rx,ry] = STICK_MODES[mode];
  message('radio-stick-mode-summary', `Physical left: ↔ ${lx}, ↕ ${ly}. Physical right: ↔ ${rx}, ↕ ${ry}. Saved inputs are Throttle / Yaw / Pitch / Roll. Up and right are positive before axis sign corrections.`);
}
$('radio-stick-mode').onchange = () => {
  if (!state.calibration) return;
  state.calibration.stick_mode = Number($('radio-stick-mode').value); renderStickMode(); rememberCalibrationDraft();
  message('calibration-message', 'Stick assignments changed in this draft. Confirm calibration to save; saved tracking can be remapped without scanning the video again.');
};
$('apply-stick-mode-all').onclick = guarded(async () => {
  const mode = Number($('radio-stick-mode').value), token = state.jobToken;
  const result = await api(path('/stick-mode'), {method:'PUT', json:{stick_mode:mode}});
  if (token !== state.jobToken) return;
  state.job.settings.stick_mode = mode; state.job.calibrations = result.calibrations;
  for (const [name,draft] of Object.entries(state.calibrationDrafts)) {
    draft.config.stick_mode = mode;
    try { const key = calibrationDraftKey(name), saved = JSON.parse(storage.getItem(key) || 'null'); if (saved) { saved.config.stick_mode = mode; storage.setItem(key,JSON.stringify(saved)); } } catch {}
  }
  if (state.calibration) state.calibration.stick_mode = mode;
  renderStickMode(); rememberCalibrationDraft(); await refreshStickData(true);
  message('calibration-message', `Mode ${mode} saved for the session and all existing StickCam calibrations. Stored tracking is reused; reopen motion charts or rerun motion matching to use the new assignments.`);
}, 'calibration-message');

function renderRadioShape() {
  const model = radioModels.find(row => row.id === $('session-radio-label').value), canvas = $('radio-shape-preview');
  show('radio-shape-preview', !!model);
  const hint = $('radio-shape-message'); hint.replaceChildren();
  if (!model) { hint.textContent = $('session-radio-label').value === 'other' ? 'Other uses your confirmed radio frame and visible gimbal rims, without a catalogue shape assumption.' : 'Compare known shell shapes after detecting gimbal rims. Predictions remain unconfirmed; similar radios and covered bodies may need a manual outline.'; return; }
  const context = canvas.getContext('2d'); context.clearRect(0, 0, canvas.width, canvas.height);
  const w = Math.min(250, 145 * model.body_aspect), h = w / model.body_aspect, x = (300 - w) / 2, y = (180 - h) / 2;
  context.beginPath(); model.shell.forEach(([px, py], i) => { if (i) context.lineTo(x + px*w, y + py*h); else context.moveTo(x + px*w, y + py*h); }); context.closePath();
  context.fillStyle = '#1b1e1c'; context.fill(); context.strokeStyle = '#e8e3d8'; context.lineWidth = 2; context.stroke();
  context.strokeStyle = '#49a7f2';
  for (const [px, py] of model.gimbals) { context.beginPath(); context.arc(x + px*w, y + py*h, w*model.gimbal_radius, 0, Math.PI*2); context.stroke(); context.beginPath(); context.arc(x + px*w, y + py*h, 3, 0, Math.PI*2); context.fillStyle = '#49a7f2'; context.fill(); }
  if (model.screen) { const [a,b,c,d] = model.screen; context.strokeStyle = '#aaa99f'; context.strokeRect(x+a*w, y+b*h, (c-a)*w, (d-b)*h); }
  hint.append(document.createTextNode(`Approximate ${model.family} shell and gimbal layout. Shape supports detection; your confirmed session image takes priority. `));
  const link = el('a', 'Manufacturer reference'); link.href = model.source; link.target = '_blank'; link.rel = 'noopener'; hint.append(link);
}
$('session-radio-label').onchange = renderRadioShape;

function updateRadioOrientationUI() {
  const profile = state.job?.radio_profile, unavailable = !state.metadata || $('exclude-clip').checked || activeAnalysis() || state.suggesting;
  message('session-radio-message', profile ? `Confirmed: ${profile.label} · reference ${profile.name} at ${time(profile.reference_time)}. Predictions identify this saved appearance; the model label can be corrected above.` : 'Confirm one clear, calibrated radio view to establish the expected appearance for this session.');
  $('radio-rotation-deg').disabled = unavailable;
  $('radio-stick-mode').disabled = unavailable; $('apply-stick-mode-all').disabled = unavailable;
  $('suggest-radio-orientation').disabled = unavailable;
  $('confirm-session-radio').textContent = profile ? 'Change session radio using this frame' : 'Confirm radio & use this frame as reference';
  $('cal-rotate-180').disabled = unavailable;
  $('confirm-session-radio').disabled = unavailable || $('skip-motion').checked || radioOrientation.running;
  for (const id of ['check-radio-midpoint','check-radio-all']) $(id).disabled = unavailable || !profile || radioOrientation.running;
  const row = radioOrientation.result;
  $('apply-radio-orientation').disabled = unavailable || !row || ![0,180].includes(row.calibration_rotation_deg)
    || row.profile_revision !== profile?.revision || row.name !== $('calibration-clip').value
    || Math.abs(row.calibration_time - Number($('reference-time').value)) > .01;
}
function applyRadioRotation(rotation) {
  if (!state.calibration) return;
  const left = state.calibration.left_gimbal, right = state.calibration.right_gimbal;
  if (left && right) {
    const physicalLeftOnImageLeft = left.x + left.center_x < right.x + right.center_x;
    if ((rotation === 0 && !physicalLeftOnImageLeft) || (rotation === 180 && physicalLeftOnImageLeft))
      [state.calibration.left_gimbal, state.calibration.right_gimbal] = [right, left];
  }
  state.calibrationRotationOverride = null; state.drag = null;
  state.calibration.radio_rotation_deg = rotation; state.calibration.radio_orientation_confirmed = false; state.calibration.radio_orientation_manual = true;
  $('radio-rotation-deg').value = String(rotation); drawRegions();
  message('radio-orientation-message', `${rotation === 180 ? 'Upside down' : 'Upright'} at this calibration frame. Check physical left/right regions and tips, then confirm calibration to save. Corrected stick directions require movement data derived with this orientation.`);
  updateRadioOrientationUI();
}
function renderRadioOrientation(row) {
  radioOrientation.result = row || null;
  show('radio-orientation-result', !!row); updateRadioOrientationUI();
  if (!row) return;
  const describe = value => value === 180 ? 'upside down (180°)' : value === 0 ? 'upright (0°)' : 'unknown';
  message('radio-orientation-message', `${row.message}${row.orientation_changes ? ' Orientation differs between the calibration frame and midpoint; do not use a blanket clip-wide direction flip.' : ''}`);
  const evidence = row.evidence;
  message('radio-orientation-evidence', `Expected radio: ${row.radio_label}. Near midpoint at ${time(row.image_at ?? row.calibration_time)}: ${describe(row.midpoint_rotation_deg)}. Calibration frame at ${time(row.calibration_time)}: ${describe(row.calibration_rotation_deg)}.${evidence ? ` Saved appearance matched: ${evidence.inliers} inliers, ${Math.round(evidence.coverage * 100)}% distributed outline coverage, ${Number(evidence.error_px).toFixed(2)} px projection error.` : ' No clear reference match.'}`);
  if (row.image_url) $('radio-orientation-image').src = row.image_url;
  else if (row.image_file && row.task_id) $('radio-orientation-image').src = `/api/jobs/${state.job.id}/radio-orientation/${row.task_id}/frame/${row.frame_index}`;
  else $('radio-orientation-image').removeAttribute('src');
  show('radio-orientation-image', !!row.image_url || !!row.image_file);
}
async function checkRadioOrientation(all = false, automatic = false) {
  if (!state.job?.radio_profile || !state.metadata || activeAnalysis() || $('exclude-clip').checked) return;
  if (radioOrientation.running) { if (automatic) return; throw new Error('Wait for the current orientation check or cancel it.'); }
  const jobId = state.job.id, jobToken = state.jobToken, name = $('calibration-clip').value, frameTime = Number($('reference-time').value);
  const token = ++radioOrientation.token;
  radioOrientation.running = true; updateRadioOrientationUI();
  message('radio-orientation-message', 'Checking the radio near the midpoint and at the calibration frame…');
  try {
    const task = await api(`/api/jobs/${jobId}/radio-orientation`, {method:'POST', json:all ? {} : {radio_video:name, reference_time:frameTime}});
    if (token !== radioOrientation.token || jobToken !== state.jobToken) return;
    radioOrientation.task = task.id; show('radio-orientation-progress'); show('cancel-radio-orientation');
    const poll = async () => {
      if (token !== radioOrientation.token || jobToken !== state.jobToken) return;
      try {
        const result = await api(`/api/jobs/${jobId}/motion-review/${task.id}`);
        if (token !== radioOrientation.token || jobToken !== state.jobToken) return;
        if (['queued','running'].includes(result.status)) {
          const p = result.progress;
          if (p) { $('radio-orientation-progress').value = p.percent; message('radio-orientation-message', `${p.name} · checking ${time(p.at)} · ${Number(p.percent).toFixed(1)}%`); }
          else $('radio-orientation-progress').removeAttribute('value');
          radioOrientation.timer = setTimeout(poll, 1500); return;
        }
        radioOrientation.running = false; show('radio-orientation-progress', false); show('cancel-radio-orientation', false); updateRadioOrientationUI();
        if (result.status !== 'complete') { message('radio-orientation-message', result.message, result.status === 'failed'); return; }
        if (result.profile_revision !== state.job.radio_profile?.revision) return;
        state.job.radio_orientation_checks ??= {};
        for (const row of result.clips) state.job.radio_orientation_checks[row.name] = {...row, task_id:task.id};
        renderRadioOrientation(result.clips.find(row => row.name === $('calibration-clip').value));
        renderVideoLists();
        if (all) message('session-radio-message', `${state.job.radio_profile.label} · ${result.clips.length} clips checked, ${result.clips.filter(r => r.status === 'suggested').length} orientation suggestions. Open each clip to review; unknown results require manual orientation.`);
      } catch(error) {
        radioOrientation.running = false; show('radio-orientation-progress', false); show('cancel-radio-orientation', false); updateRadioOrientationUI();
        message('radio-orientation-message', error.message, true);
      }
    };
    await poll();
  } catch(error) { if (token === radioOrientation.token) { radioOrientation.running = false; updateRadioOrientationUI(); } throw error; }
}
$('suggest-radio-orientation').onchange = () => { if (state.calibration) { state.calibration.radio_orientation_manual = !$('suggest-radio-orientation').checked; rememberCalibrationDraft(); } };
$('radio-rotation-deg').onchange = () => { $('suggest-radio-orientation').checked = false; applyRadioRotation(Number($('radio-rotation-deg').value)); rememberCalibrationDraft(); };
$('apply-radio-orientation').onclick = () => applyRadioRotation(radioOrientation.result.calibration_rotation_deg);
$('check-radio-midpoint').onclick = guarded(() => checkRadioOrientation(), 'radio-orientation-message');
$('check-radio-all').onclick = guarded(() => checkRadioOrientation(true), 'radio-orientation-message');
$('cancel-radio-orientation').onclick = guarded(async () => {
  if (radioOrientation.task) await api(`/api/jobs/${state.job.id}/motion-review/${radioOrientation.task}/cancel`, {method:'POST'});
}, 'radio-orientation-message');
$('confirm-session-radio').onclick = guarded(async () => {
  const jobId = state.job.id, jobToken = state.jobToken, name = $('calibration-clip').value;
  const choice = $('session-radio-label').value;
  if (choice === 'auto') throw new Error('Choose the suggested radio model, or Other, before confirming.');
  const label = radioModels.find(model => model.id === choice)?.label || 'Other';
  if (state.calibration.radio_outline?.length !== 4) throw new Error('Mark the radio outline, both gimbals and both tips before confirming this reference.');
  await saveCalibration(false);
  if (jobToken !== state.jobToken || name !== $('calibration-clip').value) return;
  const saved = await api(`/api/jobs/${jobId}/session-radio`, {method:'PUT', json:{radio_video:name, label}});
  if (jobToken !== state.jobToken) return;
  state.job.radio_profile = saved.profile; state.job.radio_orientation_checks = {};
  calibrationScan.token++; clearTimeout(calibrationScan.timer); calibrationScan.id = null; calibrationScan.rows = []; calibrationScan.running = false;
  state.job.calibration_scans = [];
  for (const [clip, draft] of Object.entries(state.calibrationDrafts)) if (draft.proposalId) delete state.calibrationDrafts[clip];
  show('auto-calibration-review', false); show('radio-orientation-result', false); radioOrientation.result = null;
  message('auto-calibration-message', 'Session radio reference changed. Find new suggestions; previously confirmed calibrations remain saved for review.');
  updateRadioOrientationUI();
  await checkRadioOrientation();
}, 'radio-orientation-message');
function proposalConfirmed(row) { return row.confirmed || state.job?.calibration_reviews?.[row.name]?.proposal_id === calibrationScan.id; }
function nextCalibrationProposal(exclude = '') {
  return calibrationScan.rows.find(row => row.name !== exclude && row.calibration && !proposalConfirmed(row));
}
function renderCalibrationProposals() {
  const list = $('auto-calibration-rows'); list.replaceChildren(); show('auto-calibration-review', calibrationScan.rows.length > 0);
  for (const row of calibrationScan.rows) {
    const confirmed = proposalConfirmed(row), tr = el('tr');
    const status = el('td', confirmed ? 'Confirmed & saved' : row.status === 'suggested' ? 'Ready to review · not saved' : row.status === 'needs_adjustment' ? 'Needs outline/tip adjustment' : row.message || row.status);
    if (confirmed) status.className = 'cache-ready';
    const inspect = el('td'); inspect.append(action(row.calibration ? 'Review markers' : 'Open manually', guarded(() => openCalibrationProposal(row), 'calibration-message')));
    tr.append(el('td', row.name), el('td', row.at === undefined ? '—' : `${row.at.toFixed(1)}s · ${row.attempts?.length || 0} frame(s) tried`), status, inspect); list.append(tr);
  }
  $('auto-review-next').disabled = !nextCalibrationProposal();
}
async function openCalibrationProposal(row) {
  const jobToken = state.jobToken, scanId = calibrationScan.id;
  $('calibration-clip').value = row.name; await loadCalibration(row.name);
  if (jobToken !== state.jobToken || !state.metadata || $('calibration-clip').value !== row.name) return;
  if (!row.calibration || proposalConfirmed(row)) return;
  state.calibration = clone(row.calibration); state.calibrationProposalId = scanId;
  $('stickcam-mode').value = state.calibration.stickcam_mode === 'fixed' ? 'fixed' : 'wonky';
  $('skip-motion').checked = false; $('exclude-clip').checked = false; $('reference-time').value = row.at;
  renderStickMode();
  $('tip-radius').value = state.calibration.left_gimbal.tip_radius; $('tip-threshold').value = state.calibration.left_gimbal.threshold; $('tip-polarity').value = state.calibration.left_gimbal.polarity;
  await showFrame(false);
  if (jobToken !== state.jobToken) return;
  updateMarkTools(); rememberCalibrationDraft();
  const shapes = row.proposal?.shape_matches?.map(candidate => candidate.label).join(', ');
  message('calibration-message', `Pending suggestion at ${row.at.toFixed(1)}s. ${row.message} ${shapes ? `Shape candidates: ${shapes}. ` : ''}${row.proposal?.shape_message || ''} Dashed circles are proposed tips; confirm only after checking the outline, housings, centres and both tips.`);
}
async function navigateCalibration(direction) {
  const records = state.job.inputs.radio, index = records.findIndex(r => r.name === $('calibration-clip').value);
  const name = records[(index + direction + records.length) % records.length].name;
  const proposal = calibrationScan.rows.find(r => r.name === name && r.calibration && !proposalConfirmed(r));
  if (proposal && !state.calibrationDrafts[name]) await openCalibrationProposal(proposal);
  else { $('calibration-clip').value = name; await loadCalibration(name); }
}
$('calibration-previous').onclick = guarded(() => navigateCalibration(-1), 'calibration-message');
$('calibration-next').onclick = guarded(() => navigateCalibration(1), 'calibration-message');
$('auto-review-next').onclick = guarded(async () => { const row = nextCalibrationProposal(); if (row) await openCalibrationProposal(row); }, 'calibration-message');
async function startCalibrationSearch(all = false) {
  const jobId = state.job.id, jobToken = state.jobToken;
  const task = await api(`/api/jobs/${jobId}/calibration-scan`, {method:'POST', json: {
    radio_video: all ? null : $('calibration-clip').value, start: all ? 0 : Number($('reference-time').value),
    radio_model: $('session-radio-label').value, stick_mode: Number($('radio-stick-mode').value), search_seconds: Number($('auto-search-seconds').value), stickcam_mode: all ? 'wonky' : $('stickcam-mode').value
  }});
  if (jobToken !== state.jobToken) return;
  watchCalibrationScan(task.id, jobId, jobToken);
}
function watchCalibrationScan(id, jobId, jobToken) {
  clearTimeout(calibrationScan.timer); const token = ++calibrationScan.token;
  calibrationScan.id = id; calibrationScan.jobId = jobId; calibrationScan.running = true; calibrationScan.rows = [];
  for (const button of ['auto-find-radio','auto-find-all']) $(button).disabled = true;
  show('auto-find-cancel'); show('auto-calibration-progress'); message('auto-calibration-message', 'Searching for usable radio views…');
  const poll = async () => {
    if (token !== calibrationScan.token || jobToken !== state.jobToken) return;
    try {
      const result = await api(`/api/jobs/${jobId}/motion-review/${id}`);
      if (token !== calibrationScan.token || jobToken !== state.jobToken) return;
      calibrationScan.rows = result.clips || result.progress?.clips || calibrationScan.rows; renderCalibrationProposals();
      const running = ['queued','running'].includes(result.status), progress = result.progress;
      if (running) {
        if (progress) { $('auto-calibration-progress').value = progress.percent; message('auto-calibration-message', `${progress.completed}/${progress.total} clips searched · ${progress.name} at ${Number(progress.at).toFixed(1)}s · advancing in 5s steps when unclear`); }
        else { $('auto-calibration-progress').removeAttribute('value'); message('auto-calibration-message', result.message); }
        calibrationScan.timer = setTimeout(poll,1500); return;
      }
      calibrationScan.running = false; show('auto-find-cancel',false); show('auto-calibration-progress',false);
      for (const button of ['auto-find-radio','auto-find-all']) $(button).disabled = activeAnalysis();
      const ready = calibrationScan.rows.filter(r => r.calibration).length;
      message('auto-calibration-message', result.status === 'complete' ? `${ready}/${calibrationScan.rows.length} clips have editable suggestions. Review, adjust and confirm each one; suggestions are not saved calibrations. ${result.warnings?.join(' ') || ''}` : result.message, result.status === 'failed');
    } catch (error) { message('auto-calibration-message', `Connection interrupted; retrying… ${error.message}`,true); calibrationScan.timer = setTimeout(poll,3000); }
  };
  poll();
}
$('auto-find-radio').onclick = guarded(() => startCalibrationSearch(false),'auto-calibration-message');
$('auto-find-all').onclick = guarded(() => startCalibrationSearch(true),'auto-calibration-message');
$('auto-find-cancel').onclick = guarded(async () => {
  if (calibrationScan.id) await api(`/api/jobs/${calibrationScan.jobId}/motion-review/${calibrationScan.id}/cancel`,{method:'POST'});
},'auto-calibration-message');
$('load-frame').onclick = guarded(() => showFrame(), 'calibration-message');
for (const [id, step] of [['cal-frame-back', -5], ['cal-frame-forward', 5]]) $(id).onclick = guarded(async () => {
  if (!state.metadata) return;
  $('reference-time').value = Math.max(0, Math.min(Number($('reference-time').value) + step, state.metadata.duration - 1 / (state.metadata.fps || 30))).toFixed(3);
  await showFrame();
}, 'calibration-message');
$('auto-regions').onclick = guarded(async () => {
  await ensureCalibrationLoaded();
  if (!state.metadata || !state.calibration) throw new Error('Load a reference frame first.');
  const name = $('calibration-clip').value, referenceTime = Number($('reference-time').value);
  if (Math.abs(referenceTime - state.calibration.reference_time) > .01) await showFrame();
  if (state.suggesting) return;
  const requestToken = ++state.suggestionToken, controller = new AbortController();
  state.suggestionController = controller; state.suggesting = true; updateMarkTools();
  let timedOut = false;
  const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 30000);
  try {
  const token = state.calToken, jobToken = state.jobToken, frameToken = state.frameToken;
  const previousCalibration = JSON.stringify(state.calibration);
  const outlined = state.calibration.radio_outline?.length === 4;
  message('calibration-message', outlined ? 'Checking gimbal rims inside the radio outline…' : 'Checking saved radio references and visible gimbal rims…');
  const proposal = await api(path(`/calibration/${part(name)}/suggest?at=${referenceTime}`), { method: 'POST', signal: controller.signal, json: { radio_model: $('session-radio-label').value, radio_outline: state.calibration.radio_outline || [], radio_rotation_deg: state.calibration.radio_rotation_deg || 0, suggest_orientation: $('suggest-radio-orientation').checked } });
  if (requestToken !== state.suggestionToken || token !== state.calToken || jobToken !== state.jobToken || frameToken !== state.frameToken) return;
  if (previousCalibration !== JSON.stringify(state.calibration)) { message('calibration-message', 'The calibration changed while checking. Click Suggest again for the current markers.', true); return; }
  const sx = state.metadata.width / proposal.frame_width, sy = state.metadata.height / proposal.frame_height;
  if (proposal.radio_rotation_deg !== undefined) {
    state.calibration.radio_rotation_deg = proposal.radio_rotation_deg; state.calibration.radio_orientation_confirmed = false;
    $('radio-rotation-deg').value = String(proposal.radio_rotation_deg);
  }
  if (proposal.radio_outline) state.calibration.radio_outline = proposal.radio_outline.map(([x, y]) => [x * sx, y * sy]);
  for (const side of ['left_gimbal', 'right_gimbal']) {
    const spec = proposal.regions[side];
    state.calibration[side] = { ...spec, x: spec.x * sx, y: spec.y * sy, width: spec.width * sx, height: spec.height * sy,
      center_x: spec.center_x * sx, center_y: spec.center_y * sy, travel_x: spec.travel_x * sx, travel_y: spec.travel_y * sy,
      tip_radius: (spec.tip_radius || 10) * Math.min(sx, sy) };
    if (Number.isFinite(spec.tip_x) && Number.isFinite(spec.tip_y)) {
      state.calibration[side].tip_x = spec.tip_x * sx;
      state.calibration[side].tip_y = spec.tip_y * sy;
    } else {
      delete state.calibration[side].tip_x; delete state.calibration[side].tip_y;
    }
  }
  $('tip-radius').value = Number(state.calibration.left_gimbal.tip_radius.toFixed(1));
  state.tool = null; document.querySelectorAll('[data-tool]').forEach(button => button.classList.remove('active'));
  drawRegions();
  if (stickReview.framePurpose === 'correction') stickReview.frameEdits = true;
  rememberCalibrationDraft(); updateStickWorkspace();
  const missingTips = ['left_gimbal', 'right_gimbal'].filter(side => state.calibration[side].tip_x === undefined);
  const tipMessage = missingTips.length ? `Could not reliably locate the ${missingTips.map(side => side.startsWith('left') ? 'left' : 'right').join(' and ')} tip${missingTips.length > 1 ? 's' : ''}; click the corresponding tip tool to mark ${missingTips.length > 1 ? 'them' : 'it'}.` : 'Both current stick tips are suggested. Review all markers, adjust any point, then save or check tracking.';
  message('calibration-message', `${proposal.needs_review ? 'Uncertain suggestion — review before confirming. ' : ''}${proposal.reference_clip ? `Located a saved radio reference from ${proposal.reference_clip}.` : `Suggested gimbal housings${outlined ? ' inside the marked outline' : ''}.`} ${(proposal.review_reasons || []).join(' ')} ${proposal.shape_matches?.length ? `Shape candidates: ${proposal.shape_matches.map(row => row.label).join(', ')}. ` : ''}${proposal.shape_message || ''} ${tipMessage} Circles mark tips; crosses mark estimated neutral centres.`);
  $('calibration-message').classList.toggle('warning', !!proposal.needs_review);
  } catch (error) {
    if (requestToken !== state.suggestionToken) return;
    if (timedOut) message('calibration-message', 'Suggestion timed out after 30 seconds. Try another frame or mark the outline, then retry.', true);
    else throw error;
  } finally {
    clearTimeout(timeout);
    if (requestToken === state.suggestionToken) { state.suggesting = false; state.suggestionController = null; updateMarkTools(); }
  }
}, 'calibration-message');
function cancelGimbalSuggestion() {
  state.suggestionToken++; state.suggestionController?.abort(); state.suggestionController = null; state.suggesting = false;
}
$('stickcam-mode').onchange = () => {
  if (!state.calibration) return;
  state.calibration.stickcam_mode = $('stickcam-mode').value;
  message('calibration-message', $('stickcam-mode').value === 'fixed' ? 'Fixed mode tracks tips inside these regions.' : 'Wonky mode also tracks the housing. Include detailed housing edges around each gimbal.');
};
function updateMarkTools() {
  const unavailable = $('skip-motion').checked || $('exclude-clip').checked || !state.metadata || !state.calibrationFrameReady || (stickReview.framePurpose === 'correction' && !stickReview.data);
  $('cal-stage').classList.toggle('disabled', unavailable);
  document.querySelectorAll('[data-tool]').forEach(button => button.disabled = unavailable || activeAnalysis() || stickPreparation.running || state.suggesting || !!stickReview.saving);
  $('check-tracking').disabled = unavailable || activeAnalysis();
  $('auto-regions').disabled = unavailable || activeAnalysis() || stickPreparation.running || state.suggesting || !!stickReview.saving;
  $('auto-regions').textContent = state.suggesting ? 'Finding gimbal regions…' : 'Suggest gimbal regions';
  $('auto-regions').setAttribute('aria-busy', String(state.suggesting));
  $('save-calibration').disabled = stickReview.framePurpose === 'correction' || !state.calibration || activeAnalysis() || stickPreparation.running || state.suggesting || !!calibrationLoad || (!state.calibrationFrameReady && !$('exclude-clip').checked && !$('skip-motion').checked);
  $('save-activity-regions').disabled = $('save-calibration').disabled || !activityConfigured(state.calibration);
  $('stick-review-load').disabled = !state.job || !state.calibrationName || activeAnalysis() || stickReview.loading || !!stickReview.saving;
  $('stick-review-load').textContent = stickReview.loading ? 'Reading saved tracking…' : 'Reload saved tracking';
  $('stick-review-rederive').disabled = !!stickReview.saving || stickReview.loading || !state.calibration || !calibrated(stickReview.framePurpose === 'correction' ? state.job?.calibrations[state.calibrationName] : state.calibration) || $('skip-motion').checked || $('exclude-clip').checked || activeAnalysis() || stickPreparation.running || state.suggesting || !!calibrationLoad;
  for (const id of ['load-frame', 'cal-preview', 'cal-frame-back', 'cal-frame-forward', 'reference-time']) $(id).disabled = !state.metadata || $('exclude-clip').checked || activeAnalysis() || state.suggesting;
  $('cal-preview').disabled ||= !state.calibrationFrameReady || stickReview.frameEdits;
  $('cal-pause').disabled = !state.metadata || !state.calibrationFrameReady || activeAnalysis() || state.suggesting;
  $('stick-review-save').disabled = !stickReview.data || (!stickReview.dirty && !stickReview.frameEdits) || activeAnalysis() || stickPreparation.running || !!stickReview.saving;
  $('stick-review-calibration-landmark').disabled = !stickReview.data || !state.calibrationFrameReady || activeAnalysis() || stickPreparation.running || !!stickReview.saving;
  $('stick-edit-initial').disabled = activeAnalysis() || stickPreparation.running || !!stickReview.saving;
  for (const id of ['stick-review-add','stick-review-remove','stick-review-throttle','stick-review-yaw','stick-review-pitch','stick-review-roll','stick-review-note']) $(id).disabled = activeAnalysis() || stickPreparation.running || !!stickReview.saving;
  for (const id of ['radio-stick-mode','skip-motion','exclude-clip']) { $(id).disabled = activeAnalysis() || state.suggesting || stickReview.framePurpose === 'correction'; $(id).title = stickReview.framePurpose === 'correction' ? 'Choose Edit initial calibration to change this clip setting.' : ''; }
  for (const id of ['stickcam-mode', 'session-radio-label']) $(id).disabled = activeAnalysis() || state.suggesting;
  updateRadioOrientationUI();
}
$('skip-motion').onchange = updateMarkTools;
$('exclude-clip').onchange = updateMarkTools;
for (const button of document.querySelectorAll('[data-tool]')) button.onclick = guarded(async () => {
  if (!$('cal-video').classList.contains('hidden') && !await editStickPlaybackFrame()) return;
  state.tool = button.dataset.tool;
  if (state.tool === 'radio-outline') { state.calibration.radio_outline = []; if (stickReview.framePurpose === 'correction') stickReview.frameEdits = true; drawRegions(); updateStickWorkspace(); }
  document.querySelectorAll('[data-tool]').forEach(item => item.classList.toggle('active', item === button));
  message('calibration-message', state.tool === 'radio-outline' ? 'Click the radio body corners in order: top left, top right, bottom right, bottom left. Stay on the shell and avoid including your hands.' : state.tool.startsWith('box') ? 'Drag a rectangle around the complete gimbal and its housing.' : state.tool.startsWith('tip') ? 'Click the centre of the visible stick tip.' : 'Click the neutral gimbal centre (where the tip would be at rest).');
}, 'calibration-message');
function calibrationViewRotated() { return state.calibrationRotationOverride ?? state.calibration?.radio_rotation_deg === 180; }
$('cal-rotate-180').onchange = () => { $('suggest-radio-orientation').checked = false; applyRadioRotation($('cal-rotate-180').checked ? 180 : 0); rememberCalibrationDraft(); };
function coordinates(event) {
  const bounds = $('roi-canvas').getBoundingClientRect();
  const x = (event.clientX - bounds.left) / bounds.width * state.metadata.width;
  const y = (event.clientY - bounds.top) / bounds.height * state.metadata.height;
  const rotated = calibrationViewRotated();
  return { x: Math.max(0, Math.min(state.metadata.width - 1, rotated ? state.metadata.width - x : x)),
    y: Math.max(0, Math.min(state.metadata.height - 1, rotated ? state.metadata.height - y : y)) };
}
$('roi-canvas').onpointerdown = event => {
  if (!state.metadata || !state.tool || $('skip-motion').checked || $('exclude-clip').checked || activeAnalysis() || stickPreparation.running || state.suggesting || stickReview.saving) return;
  const [kind, side] = state.tool.split(':');
  if (kind !== 'radio-outline' && !state.calibration[side] && kind !== 'box' && !(kind === 'tip' && stickReview.framePurpose === 'correction')) { message('calibration-message', 'Draw that gimbal region first.', true); return; }
  $('roi-canvas').setPointerCapture(event.pointerId); state.drag = { start: coordinates(event), current: coordinates(event), kind, side }; event.preventDefault();
};
$('roi-canvas').onpointermove = event => { if (state.drag) { state.drag.current = coordinates(event); drawRegions(); } };
$('roi-canvas').onpointerup = event => {
  if (!state.drag) return;
  const { start, kind, side } = state.drag, end = coordinates(event); state.drag = null;
  if (kind === 'radio-outline') {
    const outline = state.calibration.radio_outline || [];
    if (outline.length >= 4) state.calibration.radio_outline = [];
    state.calibration.radio_outline = [...(state.calibration.radio_outline || []), [Math.round(end.x), Math.round(end.y)]];
    const count = state.calibration.radio_outline.length;
    if (count === 4 && calibrationViewRotated()) {
      const points = state.calibration.radio_outline;
      state.calibration.radio_outline = [points[2], points[3], points[0], points[1]];
    }
    message('calibration-message', count === 4 ? 'Radio outline saved in this calibration. Check the orientation trace during the tracking preview.' : `Radio outline: ${count}/4 corners marked.`);
  } else if (kind === 'box') {
    const x = Math.floor(Math.min(start.x, end.x)), y = Math.floor(Math.min(start.y, end.y));
    const width = Math.ceil(Math.abs(end.x - start.x)), height = Math.ceil(Math.abs(end.y - start.y));
    if (Math.min(width, height) < 8) { message('calibration-message', 'Draw a larger region.', true); return; }
    state.calibration[side] = { x, y, width, height, center_x: width / 2, center_y: height / 2, travel_x: width * .4, travel_y: height * .4 };
  } else if (kind === 'tip' && stickReview.framePurpose === 'correction') {
    addStickTipCorrection(side, end);
  } else {
    const spec = state.calibration[side], x = end.x - spec.x, y = end.y - spec.y;
    if (x < 0 || y < 0 || x >= spec.width || y >= spec.height) { message('calibration-message', 'Choose a point inside that gimbal region.', true); return; }
    spec[kind === 'tip' ? 'tip_x' : 'center_x'] = x; spec[kind === 'tip' ? 'tip_y' : 'center_y'] = y;
    if (kind === 'tip') delete spec.tip_suggested;
  }
  if (stickReview.framePurpose === 'correction' && kind !== 'tip') stickReview.frameEdits = true;
  drawRegions(); updateMarkTools(); updateStickWorkspace();
  if (stickReview.framePurpose === 'correction') { message('calibration-message', kind === 'tip' ? 'Tip correction added at this time. Save corrections or re-derive to apply it.' : 'Frame correction draft. Check both regions, centres, tips and outline, then add the correction.'); return; }
  rememberCalibrationDraft();
  message('calibration-message', calibrated(state.calibration) ? 'Both tips marked. Save or check the tracking.' : 'Continue marking the remaining region or tip.');
};
$('roi-canvas').onpointercancel = () => { state.drag = null; drawRegions(); };
function drawRegions() {
  const canvas = $('roi-canvas'), context = canvas.getContext('2d'); context.clearRect(0, 0, canvas.width, canvas.height);
  const rotated = calibrationViewRotated();
  $('cal-stage').classList.toggle('cal-rotated-180', rotated); $('cal-rotate-180').checked = rotated;
  context.save();
  if (rotated) { context.translate(canvas.width, canvas.height); context.scale(-1, -1); }
  // Geometry uses source coordinates; draw labels normally so they remain readable.
  const label = (text, x, y) => {
    context.save();
    if (rotated) { context.setTransform(1, 0, 0, 1, 0, 0); x = canvas.width - x - context.measureText(text).width; y = canvas.height - y; }
    context.fillText(text, x, y); context.restore();
  };
  const line = Math.max(2, canvas.width / 500); context.lineWidth = line; context.font = `${Math.max(14, canvas.width / 70)}px system-ui`;
  for (const [index, side] of ['left_gimbal', 'right_gimbal'].entries()) {
    const spec = state.calibration?.[side]; if (!spec) continue;
    context.strokeStyle = context.fillStyle = index ? '#f06f62' : '#49a7f2'; context.strokeRect(spec.x, spec.y, spec.width, spec.height);
    label(index ? 'RIGHT' : 'LEFT', spec.x + line * 2, Math.max(20, spec.y - line * 2));
    const cx = spec.x + spec.center_x, cy = spec.y + spec.center_y;
    context.beginPath(); context.moveTo(cx - line * 4, cy); context.lineTo(cx + line * 4, cy); context.moveTo(cx, cy - line * 4); context.lineTo(cx, cy + line * 4); context.stroke();
    if (spec.tip_x !== undefined) {
      const tx = spec.x + spec.tip_x, ty = spec.y + spec.tip_y;
      const radius = spec.tip_radius ?? Number($('tip-radius').value);
      context.setLineDash(spec.tip_suggested ? [line * 3, line * 2] : []);
      context.beginPath(); context.arc(tx, ty, radius, 0, Math.PI * 2); context.stroke(); context.setLineDash([]);
      context.beginPath(); context.arc(tx, ty, line, 0, Math.PI * 2); context.fill();
      label('TIP', tx + radius + line * 2, ty);
    }
  }
  const outline = state.calibration?.radio_outline || [];
  if (outline.length) {
    context.strokeStyle = '#ffd166';
    context.fillStyle = '#ffd166';
    context.beginPath(); context.moveTo(outline[0][0], outline[0][1]);
    for (const point of outline.slice(1)) context.lineTo(point[0], point[1]);
    if (outline.length === 4) context.closePath();
    context.stroke();
    outline.forEach(([x, y], index) => { context.beginPath(); context.arc(x, y, line * 3, 0, Math.PI * 2); context.fill(); label(String(rotated && outline.length === 4 ? [2,3,0,1].indexOf(index) + 1 : index + 1), x + line * 3, y - line * 3); });
  }
  if (state.drag?.kind === 'box') { const { start, current } = state.drag; context.strokeStyle = '#fff'; context.strokeRect(start.x, start.y, current.x - start.x, current.y - start.y); }
  context.restore(); drawStickReviewOverlay();
}
$('tip-radius').oninput = () => {
  for (const side of ['left_gimbal', 'right_gimbal']) if (state.calibration?.[side]) state.calibration[side].tip_radius = Number($('tip-radius').value);
  drawRegions();
};
function currentCalibrationConfig(activityOnly = false) {
  let config = clone(state.calibration);
  delete config.exclude; delete config.skip_motion;
  if (config.radio_outline?.length === 0) delete config.radio_outline;
  if ($('exclude-clip').checked) config = { exclude: true };
  else if ($('skip-motion').checked) config = { skip_motion: true, stickcam_mode: $('stickcam-mode').value };
  else {
    config.stickcam_mode = $('stickcam-mode').value;
    config.stick_mode = Number($('radio-stick-mode').value);
    config.radio_rotation_deg = Number($('radio-rotation-deg').value); config.radio_orientation_confirmed = true;
    if (activityOnly ? !activityConfigured(config) : !calibrated(config)) throw new Error(activityOnly ? 'Mark the radio outline and both gimbal regions. Stick tips are optional for movement.' : 'Draw both regions and click both tips, or save radio regions for tip-free movement.');
    if (config.radio_outline && config.radio_outline.length !== 4) throw new Error('Mark all four radio outline corners, or click the outline tool again to start over.');
    for (const side of ['left_gimbal', 'right_gimbal']) {
      Object.assign(config[side], { tip_radius: config[side].tip_radius ?? Number($('tip-radius').value), threshold: Number($('tip-threshold').value), polarity: $('tip-polarity').value });
      if(activityOnly && config[side].tip_suggested){delete config[side].tip_x;delete config[side].tip_y;}
      delete config[side].tip_suggested;
    }
  }
  return config;
}
async function saveCalibration(next = false, activityOnly = false) {
  if (!state.job || !state.calibration) return;
  if (stickReview.framePurpose === 'correction') throw new Error('Save this as a timed correction, or choose Edit initial calibration to change the starting frame.');
  const name = $('calibration-clip').value, token = state.calToken, jobToken = state.jobToken;
  const config = currentCalibrationConfig(activityOnly), proposalId = state.calibrationProposalId;
  const saved = await api(path(`/calibration/${part(name)}${proposalId ? `?proposal_id=${proposalId}` : ''}`), { method: 'PUT', json: config });
  if (jobToken !== state.jobToken) return;
  if (stickReview.name === name && stickReview.data?.movement && JSON.stringify(state.job.calibrations[name]) !== JSON.stringify(saved.config)) stickReview.data.movement.stale = true;
  state.job.calibrations[name] = saved.config;
  delete state.calibrationDrafts[name]; try { storage.removeItem(calibrationDraftKey(name)); } catch {}
  show('calibration-dirty', false); show('discard-calibration-draft', false); renderWorkflow();
  if (proposalId) { state.job.calibration_reviews ??= {}; state.job.calibration_reviews[name] = {proposal_id: proposalId, status:'confirmed'}; }
  renderCalibrationProposals();
  if (token !== state.calToken) { updateCalibrationCount(); return; }
  state.calibrationProposalId = null;
  state.job.calibrations[name] = saved.config; state.calibration = clone(saved.config); updateCalibrationCount(); message('calibration-message', `Confirmed and saved ${name}.`);
  if (next) {
    const proposal = nextCalibrationProposal(name);
    if (proposal) { await openCalibrationProposal(proposal); return; }
    const records = state.job.inputs.radio, current = records.findIndex(record => record.name === name);
    const pending = [...records.slice(current + 1), ...records.slice(0, current)].find(record => !calibrated(state.job.calibrations[record.name]));
    if (pending) { $('calibration-clip').value = pending.name; await loadCalibration(pending.name); }
    else message('calibration-message', 'Saved calibrations are ready. Run automatic matching to reuse audio and derive only the unresolved inputs needed.');
  }
}
$('save-calibration').onclick = guarded(async () => { await saveCalibration(false); if (stickReview.data) stickReview.data.calibration = clone(state.calibration); updateStickWorkspace(); }, 'calibration-message');
$('save-activity-regions').onclick = guarded(async () => { await saveCalibration(false,true); message('calibration-message','Radio regions saved for tip-free movement matching. Exact inputs still need marked tips.'); await refreshStickData(true); updateStickWorkspace(); }, 'calibration-message');
$('check-tracking').onclick = guarded(async () => {
  const token = state.calToken, jobToken = state.jobToken;
  const config = currentCalibrationConfig();
  $('check-tracking').disabled = true; message('calibration-message', 'Checking the first 3 seconds after this reference…');
  try {
    const result = await api(path(`/calibration/${part($('calibration-clip').value)}/check`), { method: 'POST', json: config });
    if (token !== state.calToken) return;
    $('tracking-image').src = result.image_url;
    const outline = result.radio_orientation_quality === null ? '' : ` · radio orientation quality ${result.radio_orientation_quality.toFixed(2)} · ${result.radio_orientation_span_degrees.toFixed(1)}° tracked`;
    message('tracking-message', `Tracking quality ${result.quality.toFixed(2)}${outline} · ${result.message}`); show('tracking-check');
    message('calibration-message', 'Inspect the tip markers in the tracking images. A high quality value alone does not establish correct detection.');
  } finally { updateMarkTools(); }
}, 'calibration-message');
$('copy-regions').onclick = guarded(async () => {
  const jobId = state.job.id, jobToken = state.jobToken;
  await saveCalibration(false);
  if (jobToken !== state.jobToken) return;
  if (state.calibration.skip_motion || state.calibration.exclude) throw new Error('This clip has no motion regions to copy.');
  const sourceName = $('calibration-clip').value, config = clone(state.calibration), info = state.metadata;
  $('copy-regions').disabled = true;
  try {
    let count = 0;
    for (const record of state.job.inputs.radio) {
      if (jobToken !== state.jobToken) return;
      if (record.name === sourceName || calibrated(state.job.calibrations[record.name])) continue;
      message('calibration-message', `Copying regions to ${record.name}…`);
      let metadata;
      try { metadata = await api(`/api/jobs/${jobId}/metadata/radio/${part(record.name)}`); }
      catch { continue; }
      if (jobToken !== state.jobToken) return;
      if (Math.abs(metadata.width / metadata.height - info.width / info.height) > .02) continue;
      const target = clone(config), sx = metadata.width / info.width, sy = metadata.height / info.height;
      delete target.radio_outline;
      delete target.tracking_landmarks;
      delete target.capture_tracking_geometry;
      target.reference_time = 0;
      for (const side of ['left_gimbal', 'right_gimbal']) {
        delete target[side].tip_x; delete target[side].tip_y;
        for (const key of ['x', 'width', 'center_x', 'travel_x']) target[side][key] *= sx;
        for (const key of ['y', 'height', 'center_y', 'travel_y']) target[side][key] *= sy;
        target[side].tip_radius *= Math.min(sx, sy);
      }
      const saved = await api(`/api/jobs/${jobId}/calibration/${part(record.name)}`, { method: 'PUT', json: target });
      if (jobToken !== state.jobToken) return;
      state.job.calibrations[record.name] = saved.config; count++;
    }
    updateCalibrationCount(); message('calibration-message', `Copied gimbal regions to ${count} clips. Mark each clip's stick tips and radio outline separately.`);
  } finally { $('copy-regions').disabled = false; }
}, 'calibration-message');

function metadataReady(video, url) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => finish(new Error('Video could not load for browser playback.')), 20000);
    const loaded = () => video.videoWidth && video.videoHeight ? finish() : failed(), failed = () => finish(new Error('This source format needs a fast browser preview.'));
    const metadata = () => { if (!video.videoWidth || !video.videoHeight) failed(); };
    function finish(error) { clearTimeout(timer); video.removeEventListener('loadeddata', loaded); video.removeEventListener('loadedmetadata', metadata); video.removeEventListener('error', failed); error ? reject(error) : resolve(); }
    video.addEventListener('loadeddata', loaded); video.addEventListener('loadedmetadata', metadata); video.addEventListener('error', failed); video.preload = 'auto'; video.src = url; video.load();
  });
}
async function loadVideo(video, kind, name, isCurrent, target, preferFast = false, initialTime = 0) {
  releasePreview(video);
  const id = state.job.id, url = `/api/jobs/${id}/video/${kind}/${part(name)}`;
  const metadata = await api(`/api/jobs/${id}/metadata/${kind}/${part(name)}`);
  if (!isCurrent()) return;
  const hevcSupported = video.canPlayType('video/mp4; codecs="hvc1"') || video.canPlayType('video/mp4; codecs="hev1"');
  if (!preferFast && (metadata.codec !== 'hevc' || hevcSupported)) {
    try {
      await metadataReady(video, url);
      if (!isCurrent()) { video.pause(); return; }
      video.currentTime = Math.max(0, Math.min(initialTime, metadata.duration - 1 / 30));
      return;
    } catch (error) { if (!isCurrent()) return; }
  }
  await loadFastPreview(video, `/api/jobs/${id}/preview/${kind}/${part(name)}`, isCurrent,
    (text, error = false) => { if (isCurrent()) message(target, `${name}: ${text}`, error); }, initialTime, $('preview-acceleration').value);
}
$('cal-preview').onclick = guarded(async () => {
  const token = state.calToken, jobToken = state.jobToken, at = stickWorkspaceTime(), name = $('calibration-clip').value;
  const video = $('cal-video'), key = `${state.job.id}/${name}`;
  if (video.dataset.stickSource !== key || !video.readyState) {
    await loadVideo(video, 'radio', name, () => token === state.calToken && jobToken === state.jobToken, 'calibration-message', true, at);
    if (token !== state.calToken || jobToken !== state.jobToken) return;
    video.dataset.stickSource = key;
  }
  video.currentTime = at; show('cal-image', false); show('cal-video'); show('cal-pause'); show('roi-canvas', false);
  $('cal-stage').classList.add('watching'); updateStickWorkspace();
  video.muted = !$('stick-review-audio').checked; video.playbackRate = Number($('stick-review-speed').value);
  await video.play();
  message('calibration-message', 'Watch the saved tracking and controls below. Pause & correct this frame, or choose a marking tool to stop and correct it.');
}, 'calibration-message');
$('cal-pause').onclick = guarded(editStickPlaybackFrame, 'calibration-message');
$('stick-edit-initial').onclick = guarded(async () => {
  if (stickReview.frameEdits) throw new Error('Add or discard the current frame correction first.');
  if (stickReview.dirty) await saveStickReview();
  await loadCalibrationFrame($('calibration-clip').value);
}, 'calibration-message');

// Analysis, recovery and result review.
async function startMatching(overrides = {}) {
  const jobId = state.job.id, jobToken = state.jobToken;
  if (activeAnalysis() || stickPreparation.running || fpvPreparation.running) throw new Error('Wait for the current processing task to finish.');
  const strategy=overrides.matching_strategy || $('matching-strategy').value,automatic=strategy!=='selected';
  const targetKind = overrides.match_target_kind || $('match-target-kind').value, targetNames = overrides.match_target_names || (targetKind === 'all' ? [] : [...state.targets[targetKind]]), targetName = targetNames.length === 1 ? targetNames[0] : '';
  if (targetKind !== 'all' && !targetNames.length) throw new Error('Select one or more targets in the video lists.');
  const radioInputs = targetKind === 'radio' ? state.job.inputs.radio.filter(record => targetNames.includes(record.name)) : state.job.inputs.radio;
  const evidenceSources = overrides.evidence_sources || readEvidenceSources(), mode = evidenceMode(evidenceSources);
  if (!mode.motion && !mode.audio) throw new Error('Select both audio sources, or StickCam inputs/gimbal movement and an FPV motion source.');
  if(evidenceSources.movement_rhythm && !evidenceSources.stick_inputs)throw new Error('Derived-input rhythm needs StickCam derived inputs. Use tip-free gimbal movement to skip exact tip tracking.');
  const fpvInputs=targetKind==='fpv'?state.job.inputs.fpv.filter(record=>targetNames.includes(record.name)):state.job.inputs.fpv;
  const readiness=matchingReadiness(evidenceSources,radioInputs,fpvInputs,strategy);
  if(readiness.blocked){openPanel('calibration-panel');message('matching-readiness',readiness.message,true);throw new Error(readiness.message);}
  const allowUnconfigured = automatic || (mode.motion && $('audio-only-unconfigured').checked);
  if (!automatic && allowUnconfigured && !mode.audio) throw new Error('Unconfigured StickCam fallback requires both audio sources.');
  const pending = !automatic && mode.motion && radioInputs.find(record => {
    const config = state.job.calibrations[record.name];
    return !config ? !allowUnconfigured : !matchingCalibrationReady(config,evidenceSources);
  });
  if (pending) { openPanel('calibration-panel'); $('calibration-clip').value = pending.name; await loadCalibration(pending.name); $('calibration-panel').scrollIntoView({behavior:'smooth',block:'start'}); throw new Error(`Finish the calibration for ${pending.name}, or enable audio + filename-clock matching for unconfigured StickCam.`); }
  const settings = { matching_strategy: strategy,embedded_seed_enabled:$('embedded-seed-enabled').checked,seed_spot_checks:Number($('seed-spot-checks').value), match_sample_percent: Number($('match-sample-percent').value), sample_fps: Number($('sample-fps').value), match_fps: Number($('match-fps').value), min_overlap: Number($('min-overlap').value),
    audio_boundary:Number($('audio-boundary').value), audio_adaptive:$('audio-adaptive').checked, audio_retry_limit:Number($('audio-retry-limit').value),
    workers: Number($('workers').value), minimum_score: Number($('minimum-score').value), audio_enabled: mode.audio, evidence_sources: evidenceSources,
    match_target_kind: targetKind, match_target_name: targetName, match_target_names: targetNames, stop_on_confident_match: $('stop-on-confident-match').checked, allow_unconfigured_radio: allowUnconfigured, rate_profile: evidenceSources.stick_inputs && mode.motion ? readRateProfile() : null, ...readMotionSettings(), ...overrides };
  await api(`/api/jobs/${jobId}/run`, { method: 'POST', json: settings });
  if (jobId !== state.job?.id || jobToken !== state.jobToken) return;
  state.job.settings = {...state.job.settings, ...settings};
  state.job.status = 'queued'; state.job.message = 'Queued for analysis'; state.job.progress = null;
  pausePair(); show('viewer-panel', false); show('diagnostics-panel', false);
  updateJobUI(); await history(); pollJob(state.job.id, state.jobToken);
}
$('analyze').onclick = guarded(() => startMatching(), 'analysis-message');
$('workflow-find').onclick = guarded(() => startMatching(), 'workflow-message');
$('workflow-movement').onclick=()=>{ $('matching-strategy').value='automatic';$('audio-only-unconfigured').checked=false;loadEvidenceSources({evidence_sources:{fpv_gyro:true,fpv_visual:true,stick_inputs:false,gimbal_activity:true,movement_rhythm:false,fpv_audio:true,stickcam_audio:true,filename_timestamps:true}});renderWorkflow();message('workflow-message','Audio + tip-free movement selected. Run Quick Match to reuse saved activity and skip exact stick-input derivation.'); };
function selectEmbeddedSeed() {
  $('matching-strategy').value='telemetry_seed';$('embedded-seed-enabled').checked=true;$('audio-only-unconfigured').checked=false;
  loadEvidenceSources({evidence_sources:{fpv_gyro:true,fpv_visual:false,stick_inputs:false,gimbal_activity:true,movement_rhythm:false,fpv_audio:false,stickcam_audio:false,filename_timestamps:false}});
  renderWorkflow();message('workflow-message','O4 Quick Match selected. Run to find radio regions automatically, derive one StickCam at a time, then suggest and spot-check other pairs using modified dates.');
}
$('workflow-embedded-seed').onclick=selectEmbeddedSeed;

// One FPV recording against the full available StickCam list, regardless of the
// general match-scope/sample controls. Confirmation stays in the shared viewer.
function counterpartOptions() {
  const method = $('counterpart-method').value;
  if (method === 'selected') return {strategy:'selected', sources:readEvidenceSources()};
  return {strategy:'automatic', sources:{fpv_audio:true, stickcam_audio:true, filename_timestamps:true,
    fpv_gyro:method !== 'audio', stick_inputs:method !== 'audio' && (!$('workflow-gimbal-activity').checked || $('workflow-rhythm').checked),
    gimbal_activity:method !== 'audio' && $('workflow-gimbal-activity').checked,
    fpv_visual:method !== 'audio' && $('counterpart-visual').checked, movement_rhythm:method !== 'audio' && $('workflow-rhythm').checked}};
}
function renderCounterpart() {
  if (!state.job) return;
  const job = state.job, select = $('counterpart-fpv'), records = job.inputs.fpv;
  const optionsKey = JSON.stringify(records.map(r => [r.name, r.metadata?.duration]));
  if (select.dataset.optionsKey !== optionsKey) {
    const previous = select.value;
    const saved = storage.getItem(`fpv-counterpart:${job.id}`);
    const target = job.settings.match_target_kind === 'fpv' && job.settings.match_target_names?.length === 1 ? job.settings.match_target_names[0] : '';
    select.replaceChildren(...records.map(record => {const option=el('option',clipLabel(record));option.value=record.name;return option;}));
    const choice = [previous,saved,target].find(name=>records.some(r=>r.name===name));
    if (choice) select.value = choice;
    select.dataset.optionsKey = optionsKey;
  }
  const name = select.value, available = job.inputs.radio.filter(r=>!job.calibrations[r.name]?.exclude);
  const excluded = job.inputs.radio.length - available.length;
  const busy = activeAnalysis() || stickPreparation.running || fpvPreparation.running || counterpartSearch.starting;
  const ready = Object.values(job.inputs).flat().every(r=>r.uploaded === r.size);
  const {strategy,sources} = counterpartOptions(), mode = evidenceMode(sources);
  select.disabled = busy; $('counterpart-method').disabled = busy; $('counterpart-visual').disabled = busy;
  $('counterpart-find').disabled = busy || !ready || !name || !available.length || (!mode.audio && !mode.motion);
  $('workflow-counterpart').disabled = !ready || !records.length;
  show('counterpart-visual-option', $('counterpart-method').value === 'automatic');
  show('counterpart-evidence-settings', $('counterpart-method').value === 'selected');
  const evidence = [mode.audio ? 'boundary audio' : '', mode.motion && sources.fpv_gyro ? 'embedded FPV motion' : '', mode.motion && sources.fpv_visual ? 'experimental FPV visual fallback' : '', mode.motion && sources.gimbal_activity ? 'tip-free gimbal activity' : '', mode.motion && sources.stick_inputs ? 'precise StickCam inputs' : ''].filter(Boolean).join(' · ');
  const seedReady=matchingReadiness(sources,available,records.filter(r=>r.name===name),strategy).autoSeed;
  message('counterpart-scope', `${name || 'Choose an FPV recording'} · ${available.length} StickCam candidate(s)${excluded ? ` · ${excluded} excluded` : ''}. ${seedReady?'Checks one StickCam at a time and stops on a supported embedded-motion seed.':'This search uses one FPV clip and 100% of the available StickCam list.'} Evidence: ${evidence || 'choose a complete audio or motion comparison in Find matching flights'}.${seedReady?' Radio regions are found automatically.':strategy === 'automatic' && mode.motion ? ' Motion fallback needs a saved StickCam calibration; missing setups are reported below.' : ''}`);
  const targets = job.settings.match_target_names || (job.settings.match_target_name ? [job.settings.match_target_name] : []);
  const searching = activeAnalysis() && job.settings.match_target_kind === 'fpv' && targets.length === 1 && targets[0] === name;
  show('counterpart-progress', searching); $('counterpart-progress').value = job.progress?.percent || 0;
  show('counterpart-cancel', searching); $('counterpart-cancel').disabled = job.status === 'cancelling';
  const rows = (state.results?.rows || []).filter(row=>row.fpv_video === name).sort((a,b)=>
    Number(!!a.rejected)-Number(!!b.rejected) || Number(!!b.reviewed)-Number(!!a.reviewed) ||
    Number(!!b.global_assignment)-Number(!!a.global_assignment) || Number(!!a.timing_only)-Number(!!b.timing_only) || candidateStrength(b)-candidateStrength(a));
  const supported = rows.filter(r=>r.alignment_available && !r.rejected && !r.timing_only && (r.reviewed || r.alignment_suggested));
  const visible = $('counterpart-show-all').checked ? rows : rows.slice(0,5);
  const considered = (state.results?.considered_fpv || state.results?.match_scope?.sampled_fpv || []).includes(name);
  let status = !ready ? 'Finish uploading your recordings first.' : !available.length ? 'No StickCam recordings are available. Include at least one in this session.' : !rows.length ? considered ? 'The saved search found no comparable candidates for this FPV recording. See any preparation or source problems below.' : 'Choose a recording and find its counterpart.' : supported.some(r=>r.reviewed) ? 'A saved, reviewed counterpart is available below.' : supported.length ? 'Matching proposals are ready. Preview the shared events before confirming.' : 'No confident counterpart found in the saved results. The strongest alternatives are below for inspection.';
  if (counterpartSearch.starting) status = 'Starting the counterpart search…';
  else if (searching) status = `${job.message}${job.progress?.percent == null ? '' : ` · ${Number(job.progress.percent).toFixed(1)}%`}${rows.length ? ' Previous saved candidates remain visible while the new search runs.' : ''}`;
  else if (busy) status = 'Another processing task is running. You can search after it finishes.';
  else if (['failed','cancelled','interrupted'].includes(job.status) && job.settings.match_target_kind === 'fpv' && targets.includes(name)) status = `${job.message}${rows.length ? ' Previous saved candidates are shown below.' : ''}`;
  else if (rows.length) status += ` Showing ${visible.length}/${rows.length} saved candidates for this FPV clip.`;
  if(!busy && !counterpartSearch.starting && rows.length && !rows.some(r=>r.alignment_available))status='No matching alignment was found. The saved rows contain no usable matching evidence; no offset has been estimated. Save radio regions for movement matching, or find a pair manually.';
  if (!searching && !counterpartSearch.starting && rows.length && (state.results.match_scope?.deferred_radio || []).length) status += ` The last completed run left ${state.results.match_scope.deferred_radio.length} StickCam recordings unscanned; find the counterpart to check the full list.`;
  message('counterpart-message',status,job.status === 'failed' && targets.includes(name));
  show('counterpart-candidates',rows.length > 0);
  const resultKey = JSON.stringify([name,$('counterpart-show-all').checked,rows,state.results?.workflow,state.results?.errors,available.map(r=>[r.name,calibrated(job.calibrations[r.name])])]);
  if (counterpartSearch.candidatesKey === resultKey) return;
  counterpartSearch.candidatesKey = resultKey;
  $('counterpart-rows').replaceChildren(...visible.map(row=>{
    const tr=el('tr'), recording=el('td'), matching=el('td'), alignment=el('td'), review=el('td');
    const activityCandidate=row.alignment_method==='gimbal_activity';
    const candidateOffset=row.alignment_offset_seconds;
    const record=job.inputs.radio.find(r=>r.name===row.radio_video);
    recording.append(el('span',row.radio_video),el('small',clipDuration(record)));
    const label=!row.alignment_available?'No matching evidence':row.rejected ? 'Rejected' : row.reviewed ? 'Reviewed alignment' : row.timing_only ? 'Clock suggestion · unscanned' : row.confidence === 'strong' && row.global_assignment ? 'Strong proposal' : row.global_assignment ? 'Proposal · needs review' : activityCandidate ? 'Movement candidate · needs review' : 'Weak / ambiguous candidate';
    matching.append(el('span',label,`badge ${row.reviewed ? 'saved' : row.confidence === 'strong' && row.global_assignment ? 'strong' : 'review'}`));
    matching.append(el('small',row.timing_only ? 'Dates and durations only; content has not been checked.' : `Evidence score ${Number(row.combined_score).toFixed(3)} · ${row.evidence}`));
    if (!row.timing_only) {
      const counts=[];
      if(row.audio_usable_sections != null)counts.push(`Audio: ${row.audio_correlated_sections}/${row.audio_usable_sections} sections`);
      if(row.motion_included !== false && row.usable_sections != null)counts.push(`Motion: ${row.correlated_sections}/${row.usable_sections} sections`);
      if(counts.length)matching.append(el('small',counts.join(' · ')));
      const details=el('details');details.append(el('summary','Evidence details'),el('small',sectionsText(row)),el('small',`Candidate separation ${Number(row.candidate_margin || 0).toFixed(3)} · timing peak margin ${Number(row.peak_margin || 0).toFixed(3)}`));matching.append(details);
    }
    if(row.clock_support?.length) {
      const detail=el('details');detail.append(el('summary','Modified-date evidence'));
      clockDetails(detail,row.clock_support,[],row.timing_only);matching.append(detail);
    }
    if(row.gimbal_activity) {
      const p=row.gimbal_activity;matching.append(el('small',`Tip-free movement: ${p.score.toFixed(3)} · ${p.sections.matched}/${p.sections.usable} sections · ${p.reason}`));
      if(p.suggested)review.append(action('Preview movement match',guarded(()=>openPair(p.radio_video || row.radio_video,p.fpv_video || row.fpv_video,p.offset_seconds,false,'gimbal_activity'),'counterpart-message')));
    }
    if(row.rhythm) {
      matching.append(el('small',`Movement rhythm: ${row.rhythm.score.toFixed(3)} · ${row.rhythm.sections.matched}/${row.rhythm.sections.usable} sections · ${row.rhythm.reason}`));
      if(row.rhythm.suggested)review.append(action('Preview rhythm offset',guarded(()=>openPair(row.radio_video,row.fpv_video,row.rhythm.offset_seconds,false,true),'counterpart-message')));
    }
    alignment.append(el('span',row.alignment_available?`Offset ${offsetText(candidateOffset)}`:'Offset not estimated'));
    if(row.alignment_available)alignment.append(el('small',`${time(row.alignment_overlap_seconds ?? row.overlap_seconds)} shared footage`));
    const sourceError = (state.results.clips?.radio || []).find(c=>c.name===row.radio_video)?.source_usable === false || (state.results.clips?.fpv || []).find(c=>c.name===name)?.source_usable === false;
    const button=action(row.reviewed ? 'Review saved pair' : 'Preview candidate',guarded(()=>openCandidate(row),'counterpart-message'));
    button.disabled=sourceError || !row.alignment_available; review.append(button); if(sourceError)review.append(el('small','Source unavailable'));else if(!row.alignment_available)review.append(el('small',row.alignment_reason));
    tr.append(recording,matching,alignment,review);return tr;
  }));
  $('counterpart-notes').replaceChildren();
  if (!rows.length && !considered) return;
  const needs=(state.results?.workflow?.needs_calibration || []).filter(n=>available.some(r=>r.name===n) && !matchingCalibrationReady(job.calibrations[n],sources));
  if(needs.length){
    const details=el('details'), summary=el('summary',`${needs.length} StickCam recording(s) need calibration for motion fallback`);details.append(summary);
    for(const clip of needs){const row=el('div',undefined,'actions');row.append(el('span',clip,'hint'),action('Calibrate this clip',guarded(async()=>{openPanel('calibration-panel');await loadCalibration(clip);$('calibration-panel').scrollIntoView({behavior:'smooth'});},'counterpart-message')));details.append(row);}
    $('counterpart-notes').append(details);
  }
  const skipped = (state.results?.workflow?.skipped_motion || []).filter(item=>item.kind === 'fpv' && item.name === name);
  for(const item of skipped)$('counterpart-notes').append(el('p',`FPV motion: ${item.reason}`,'hint'));
  for(const item of state.results?.errors || [])if(item.name===name || available.some(r=>r.name===item.name))$('counterpart-notes').append(el('p',`${item.name}: ${item.error}`,'error'));
}
$('workflow-counterpart').onclick=()=>{openPanel('counterpart-panel');$('counterpart-panel').scrollIntoView({behavior:'smooth',block:'start'});};
$('counterpart-evidence-settings').onclick=()=>{setTechnicalTools(true);openPanel('analysis-panel');$('analysis-panel').scrollIntoView({behavior:'smooth',block:'start'});};
$('counterpart-fpv').onchange=()=>{if(state.job)storage.setItem(`fpv-counterpart:${state.job.id}`,$('counterpart-fpv').value);renderCounterpart();};
for(const id of ['counterpart-method','counterpart-visual','counterpart-show-all'])$(id).onchange=renderCounterpart;
$('counterpart-find').onclick=guarded(async()=>{
  if(!state.job || counterpartSearch.starting)return;
  const name=$('counterpart-fpv').value;
  if(!name || !state.job.inputs.fpv.some(r=>r.name===name))throw new Error('Choose an FPV recording from this session.');
  const {strategy,sources}=counterpartOptions(), token=state.jobToken;
  counterpartSearch.starting=true;renderCounterpart();
  try{
    await startMatching({matching_strategy:strategy,evidence_sources:sources,match_target_kind:'fpv',match_target_name:name,match_target_names:[name],match_sample_percent:100,stop_on_confident_match:false});
  }finally{if(token===state.jobToken){counterpartSearch.starting=false;renderCounterpart();}}
},'counterpart-message');
$('counterpart-cancel').onclick=guarded(async()=>{
  await api(path('/cancel'),{method:'POST'});state.job.status='cancelled';state.job.message='Counterpart search cancelled. Completed fingerprints remain cached.';updateJobUI();await history();
},'counterpart-message');
$('match-target-kind').onchange = updateMatchTargetOptions;
$('match-target-name').onchange = () => { const kind = $('match-target-kind').value; if (kind !== 'all') { state.targets[kind] = new Set([$('match-target-name').value]); renderVideoLists(); } };
$('targets-select-all').onclick = () => { const kind = $('match-target-kind').value; if (kind !== 'all') { state.targets[kind] = new Set(state.job.inputs[kind].filter(r => !state.job.calibrations[r.name]?.exclude).map(r => r.name)); renderVideoLists(); } };
$('targets-clear').onclick = () => { const kind = $('match-target-kind').value; if (kind !== 'all') { state.targets[kind].clear(); renderVideoLists(); } };
for (const actionName of ['confirm', 'continue']) $(`early-${actionName}`).onclick = guarded(async () => {
  const early = state.job.early_proposal; if (!early) return;
  await api(path('/early-decision'), { method: 'POST', json: { nonce: early.nonce, action: actionName } });
  show('early-proposal', false); state.job.early_proposal = null;
}, 'analysis-message');
$('early-preview').onclick = guarded(async () => {
  const candidate = state.job.early_proposal?.candidate; if (!candidate) return;
  updateMotionOptions(); $('motion-fpv').value = candidate.fpv_video; $('motion-radio').value = candidate.radio_video; $('motion-offset').value = candidate.offset_seconds.toFixed(6);
  await previewSelectedVideos(); $('motion-panel').scrollIntoView({behavior:'smooth'});
}, 'analysis-message');
$('audio-only-unconfigured').onchange = updateEvidenceSummary;
for (const id of Object.values(evidenceFields)) $(id).onchange = updateEvidenceSummary;
for (const [id, sources] of Object.entries({
  'evidence-all': {fpv_gyro: true, fpv_visual: true, stick_inputs: true, fpv_audio: true, stickcam_audio: true, filename_timestamps: true, gimbal_activity:true},
  'evidence-audio': {fpv_gyro: false, fpv_visual: false, stick_inputs: false, fpv_audio: true, stickcam_audio: true, filename_timestamps: true, gimbal_activity:false},
  'evidence-gyro': {fpv_gyro: true, fpv_visual: false, stick_inputs: true, fpv_audio: false, stickcam_audio: false, filename_timestamps: true, gimbal_activity:false},
  'evidence-movement': {fpv_gyro:true,fpv_visual:true,stick_inputs:false,fpv_audio:false,stickcam_audio:false,filename_timestamps:true,movement_rhythm:false,gimbal_activity:true}
})) $(id).onclick = () => {
  $('audio-only-unconfigured').checked = false;
  loadEvidenceSources({evidence_sources: sources});
};
$('cancel-analysis').onclick = guarded(async () => {
  await api(path('/cancel'), { method: 'POST' }); state.job.status = 'cancelled'; state.job.message = 'Analysis cancelled. Cached fingerprints can be reused.'; updateJobUI(); await history();
}, 'analysis-message');
async function pollJob(id, token) {
  clearTimeout(state.pollTimer);
  try {
    const job = await api(`/api/jobs/${id}`); if (token !== state.jobToken) return;
    state.job = job; updateJobUI();
    if (job.status === 'complete') { await loadResults(); await history(); return; }
    if (!['queued', 'running', 'cancelling'].includes(job.status)) { await history(); return; }
  } catch (error) { if (token !== state.jobToken) return; message('analysis-message', `Connection interrupted; retrying… ${error.message}`, true); }
  state.pollTimer = setTimeout(() => pollJob(id, token), 1500);
}
async function loadResults() {
  const token = state.jobToken, results = await api(path('/matches')); if (token !== state.jobToken) return;
  state.results = results; renderResults(); renderExports(); renderWorkflow(); renderVideoLists();
  refreshStickData(true).catch(error=>message('stick-data-message',error.message,true));
  refreshFPVData(true).catch(error=>message('fpv-data-message',error.message,true));
  if (state.job.status !== 'complete') return;
  const plots = await api(path('/plots')); if (token !== state.jobToken) return;
  $('plots').replaceChildren(...plots.files.map(filename => { const link = el('a'); link.href = path(`/file/${part(filename)}`); link.target = '_blank'; link.rel = 'noopener';
    const image = el('img'); image.src = link.href; image.alt = 'Aligned motion and tracking quality diagnostics'; image.loading = 'lazy'; link.append(image); return link; }));
  show('diagnostics-panel', plots.files.length > 0);
}
function candidateStrength(row) { return row.alignment_available ? Number(row.alignment_score)||0 : 0; }
function openCandidate(row) {
  if(!row.alignment_available)throw new Error(row.alignment_reason || 'No matching alignment was found. Save radio regions or find a pair manually.');
  return openPair(row.radio_video,row.fpv_video,row.alignment_offset_seconds,!!row.timing_only,row.alignment_method==='gimbal_activity'?'gimbal_activity':row.alignment_method==='rhythm');
}
function renderResults() {
  show('results-panel'); show('exports-panel'); const data = state.results;
  const assigned = data.rows.filter(row => row.global_assignment).sort((a, b) => a.radio_video.localeCompare(b.radio_video) || a.radio_common_start_seconds - b.radio_common_start_seconds);
  const scope = data.match_scope?.kind !== 'all' ? ` · ${data.match_scope.kind === 'fpv' ? 'FPV' : 'StickCam'} targets: ${(data.match_scope.targets || [data.match_scope.target]).join(', ')}` : '';
  const scopeText = data.manual_only ? ' · manual review; automated matching has not run' : data.match_scope?.sample_percent < 100 ? ` · ${data.match_scope.sample_percent}% sample setting (${data.match_scope.sampled_fpv?.length || 0}/${data.match_scope.total_fpv} FPV, ${data.match_scope.sampled_radio?.length || 0}/${data.match_scope.total_radio} StickCam scanned)` : ' · all selected files scanned';
  const unmatchedLabel = data.match_scope?.sample_percent < 100 ? 'unmatched in sample' : 'unmatched';
  message('result-summary', `${assigned.length} proposed FPV pairs across ${new Set(assigned.map(r => r.radio_video)).size} StickCam recordings${scope}${data.workflow?.strategy==='telemetry_seed'?' · sequential embedded-motion seed; other clips may be date-only':scopeText} · ${data.unmatched_radio.length} StickCam and ${data.unmatched_fpv.length} FPV ${unmatchedLabel} · ${Number.isFinite(data.elapsed_seconds) ? `${(data.elapsed_seconds / 60).toFixed(1)} min analysis` : 'saved results'}`);
  if(data.workflow?.strategy==='telemetry_seed')message('result-summary',`${(data.gimbal_activity_proposals || []).length} movement candidates · ${(data.match_scope?.timing_suggestions || []).length} date-based relationships · ${data.workflow.seed_search.attempts.length}/${state.job.inputs.radio.length} StickCam prepared · ${assigned.filter(r=>r.reviewed).length} confirmed alignments · ${(data.elapsed_seconds/60).toFixed(1)} min. Review content and sync before confirming.`);
  renderClocks(data);
  const counts=data.review_summary || {};
  message('review-summary',Object.entries(counts).map(([key,value])=>`${value} ${key}`).join(' · '));
  renderFlightView(flightDocument());
  const activity=data.gimbal_activity_proposals || [];show('gimbal-activity-suggestions',activity.length>0);
  $('gimbal-activity-rows').replaceChildren(...activity.map(p=>{
    const row=el('div',undefined,'timing-suggestion-row');row.append(el('span',`${p.radio_video} ↔ ${p.fpv_video}`),
      el('small',`Movement score ${p.score.toFixed(3)} · ${p.sections.matched}/${p.sections.usable} sections · ${p.valid_seconds.toFixed(1)}s observed · offset ${offsetText(p.offset_seconds)} · ${p.reason}`),
      action('Preview movement match',guarded(()=>openPair(p.radio_video,p.fpv_video,p.offset_seconds,false,'gimbal_activity'),'viewer-message')));return row;
  }));
  const rhythms=data.rhythm_proposals || [];show('rhythm-suggestions',rhythms.length>0);
  $('rhythm-suggestion-rows').replaceChildren(...rhythms.map(p=>{
    const row=el('div',undefined,'timing-suggestion-row');row.append(el('span',`${p.radio_video} ↔ ${p.fpv_video}`),
      el('small',`Rhythm ${p.score.toFixed(3)} · ${p.sections.matched}/${p.sections.usable} active sections · ${p.radio_events}/${p.fpv_events} bursts · offset ${offsetText(p.offset_seconds)} · review required`),
      action('Preview rhythm suggestion',guarded(()=>openPair(p.radio_video,p.fpv_video,p.offset_seconds,false,true),'viewer-message')));return row;
  }));
  const suggestions = data.match_scope?.timing_suggestions || [];
  show('timing-suggestions', suggestions.length > 0);
  $('timing-suggestion-rows').replaceChildren(...suggestions.slice(0, 30).map(renderDateSuggestion));
  if (suggestions.length > 30) $('timing-suggestion-rows').append(action(`Show all ${suggestions.length} date suggestions`, () => {
    $('timing-suggestion-rows').replaceChildren(...suggestions.map(renderDateSuggestion));
  }));
  if (data.match_scope?.timing_suggestions_truncated) $('timing-suggestion-rows').append(el('p', `Showing the first 250 of at least ${250 + data.match_scope.timing_suggestions_truncated} time-based candidates. Run all files to check more clips with content evidence.`, 'hint'));
  const ratesName = data.rate_profile?.model === 'actual' ? 'ACTUAL' : data.rate_profile?.model === 'kiss' ? 'KISS' : 'Betaflight legacy';
  message('rate-summary', data.rate_profile ? `Stick positions compared through the ${ratesName} rate curves.` : 'No FPV rate curve was applied.');
  show('csv-download',!data.manual_only);$('csv-download').href = path('/file/matches.csv'); $('pair-cards').replaceChildren();
  for (const row of assigned) {
    const button = action('', guarded(() => openPair(row.radio_video, row.fpv_video), 'viewer-message'), `pair-card${state.pair?.radio_video === row.radio_video && state.pair?.fpv_video === row.fpv_video ? ' active' : ''}`);
    button.append(el('span', row.reviewed ? 'Reviewed' : row.confidence === 'strong' ? 'Strong proposal' : 'Needs review', `badge ${row.reviewed ? 'saved' : row.confidence}`));
    const names = el('div', undefined, 'names'); names.append(el('span', clipLabel(state.job.inputs.radio.find(r => r.name === row.radio_video) || {name:row.radio_video})), el('span', '↔ ' + clipLabel(state.job.inputs.fpv.find(r => r.name === row.fpv_video) || {name:row.fpv_video}))); button.append(names);
    button.append(el('small', `${row.reviewed?'Saved alignment':row.manual_pair?'Manual alignment':`Score ${row.combined_score.toFixed(3)}`} · offset ${offsetText(row.offset_seconds)} · ${time(row.overlap_seconds)} shared`));
    button.append(el('small', row.reviewed && !row.combined_score?'Reviewed alignment · no automated content score in this run':row.evidence));
    if (row.audio_usable_sections != null) button.append(el('small', `Audio: ${row.audio_correlated_sections}/${row.audio_usable_sections} sections agree · ${row.audio_joint_sections || 0} agree in landmarks + trends`));
    if (row.motion_included === false) button.append(el('small', 'Motion skipped'));
    else if (row.usable_sections !== undefined) button.append(el('small', `Motion: ${row.correlated_sections}/${row.usable_sections} usable sections agree · ${Math.round(row.section_agreement * 100)}% support`));
    const siblings = assigned.filter(item => item.radio_video === row.radio_video);
    if (siblings.length > 1) button.append(el('small', `FPV segment ${siblings.indexOf(row) + 1}/${siblings.length} for this StickCam`));
    $('pair-cards').append(button);
  }
  if (!assigned.length) $('pair-cards').append(el('p',data.manual_only?'No confirmed manual pair yet. Browse the two feeds, mark a shared event and confirm its alignment.':data.rows.some(r=>r.alignment_available)?'No candidate passed the proposal threshold. Review available evidence below, or find a pair manually.':'No matching evidence was available. Save radio regions for movement matching, or find a pair manually. No offset has been estimated.','hint'));
  $('unmatched').replaceChildren();
  if ((data.match_scope?.deferred_fpv || []).length || (data.match_scope?.deferred_radio || []).length)
    $('unmatched').append(el('p', `${(data.match_scope.deferred_fpv || []).length} FPV and ${(data.match_scope.deferred_radio || []).length} StickCam files were not content-scanned in this pass. Clock-and-duration candidates are shown separately and require preview or a full pass.`, 'hint'));
  if (data.unmatched_radio.length) $('unmatched').append(el('h3', data.workflow?.strategy==='telemetry_seed'?'StickCam recordings awaiting confirmation':'Unmatched StickCam recordings'));
  for (const name of data.unmatched_radio) {
    const row = el('div', undefined, 'unmatched-row'); row.append(el('span', name));
    const candidate = data.rows.filter(item => item.radio_video === name && item.alignment_available && !item.rejected).sort((a, b) => candidateStrength(b)-candidateStrength(a))[0];
    if (candidate) row.append(action(`Review best candidate · ${candidateStrength(candidate).toFixed(3)}`, guarded(() => openCandidate(candidate), 'viewer-message')));
    else {
      const needsRegions=readEvidenceSources().gimbal_activity && !activityConfigured(state.job.calibrations[name]);
      row.append(el('small',needsRegions?'Radio regions needed · no matching alignment':'No matching alignment found'));
      if(needsRegions)row.append(action('Save radio regions',guarded(async()=>{openPanel('calibration-panel');await loadCalibration(name);$('cal-stage').scrollIntoView({behavior:'smooth',block:'start'});},'viewer-message')));
    }
    $('unmatched').append(row);
  }
  if (data.unmatched_fpv.length) $('unmatched').append(el('p', 'Unmatched FPV: ' + data.unmatched_fpv.join(', '), 'hint'));
  if (data.excluded_radio?.length) $('unmatched').append(el('p', 'Excluded StickCam: ' + data.excluded_radio.join(', '), 'hint'));
  $('file-errors').replaceChildren(...data.errors.map(error => el('p', `${error.name}: ${error.error}`)));
  renderRelatedSegments();
  const filter = $('candidate-filter').value;
  $('candidate-filter').replaceChildren(el('option', 'All clips'), ...state.job.inputs.radio.map(record => { const option = el('option', record.name); option.value = record.name; return option; }));
  $('candidate-filter').options[0].value = ''; $('candidate-filter').value = filter; renderCandidatePage();
  const reviewed = assigned.filter(row => row.reviewed).length;
  $('export-batch').disabled = !reviewed; $('export-batch').textContent = `Export ${reviewed} reviewed pair${reviewed === 1 ? '' : 's'}`;
  renderCounterpart();
}
function renderCandidatePage() {
  if (!state.results) return;
  const rows = state.results.rows.filter(row => (!$('candidate-filter').value || row.radio_video === $('candidate-filter').value) && (!$('review-filter').value || row.review_state===$('review-filter').value)).sort((a, b) => candidateStrength(b)-candidateStrength(a));
  const pages = Math.max(1, Math.ceil(rows.length / 100)); state.page = Math.min(pages - 1, Math.max(0, state.page));
  $('candidate-rows').replaceChildren(...rows.slice(state.page * 100, (state.page + 1) * 100).map(row => {
    const tr = el('tr');
    for (const value of [row.radio_video, row.fpv_video, !row.alignment_available?'No matching evidence':`${row.review_state || 'candidate'} · ${row.manual_pair?'Manual':row.timing_only?'Clock only':candidateStrength(row).toFixed(3)}`, row.timing_only ? 'Unscanned' : row.gimbal_activity ? `Gimbal ${row.gimbal_activity.score.toFixed(3)} · ${row.gimbal_activity.sections.matched}/${row.gimbal_activity.sections.usable}` : row.motion_included === false ? 'Not used' : row.score.toFixed(3), row.timing_only ? 'Unscanned' : row.audio_included === false ? 'Not used' : row.audio_score_at_motion_offset.toFixed(3), row.alignment_available?offsetText(row.alignment_offset_seconds):'Not estimated']) tr.append(el('td', value));
    const td = el('td');if(row.alignment_available)td.append(action(row.timing_only ? 'Preview suggestion' : row.global_assignment ? 'Open pair' : 'Try candidate', guarded(() => openCandidate(row), 'viewer-message')));else td.append(el('span',row.alignment_reason,'hint'));tr.append(td);return tr;
  }));
  message('page-label', `${state.page + 1} / ${pages}`); $('previous-page').disabled = state.page === 0; $('next-page').disabled = state.page === pages - 1;
}
function renderDateSuggestion(candidate) {
  const row=el('div',undefined,'timing-suggestion-row'), controls=el('div',undefined,'actions');
  controls.append(action('Preview; confirm content',guarded(()=>openPair(candidate.radio_video,candidate.fpv_video,candidate.offset_seconds,true),'viewer-message')),
    action('Reject this clock alignment',guarded(async()=>{
      const token=state.jobToken;
      const result=await api(path('/clock-rejections'),{method:'POST',json:{radio_video:candidate.radio_video,fpv_video:candidate.fpv_video,offset_seconds:candidate.offset_seconds}});
      if(token!==state.jobToken)return;
      state.results=result; renderResults(); renderWorkflow();
      message('clock-message','Clock alignment rejected. Other recorder groups and content matches remain available. Restore it under Rejected clock alignments.');
    },'clock-message')));
  row.append(el('span',`${candidate.radio_video} ↔ ${candidate.fpv_video}`),
    el('small',`Offset ${offsetText(candidate.offset_seconds)} · ${time(candidate.overlap_seconds)} overlap · ${candidate.clock_source}${candidate.provisional_clock?' · provisional movement seed':` · ${candidate.clock_support} anchor(s)`}${candidate.tentative?' · tentative':''}${candidate.negative_support?.length?` · caution: ${candidate.negative_support.length} rejected prediction(s) for this FPV in this group`:''}`),controls);
  return row;
}
function clockDetails(container, support, anchors=[], dateOnly=false) {
  container.replaceChildren();
  if (anchors.length) container.append(el('p','This independently reviewed pair contributes a camera-clock anchor. It is not evidence for itself.','hint'));
  for (const item of support || []) {
    const status=item.status==='available' && dateOnly ? `Predicted offset ${offsetText(item.expected_offset)} · content needs independent verification · tolerance ±${item.tolerance}s` : item.status==='available' ? `${item.agrees ? 'Agrees with' : 'Disagrees with'} content alignment · predicted offset ${offsetText(item.expected_offset)} · difference ${offsetText(item.alignment_difference)} · tolerance ±${item.tolerance}s` :
      item.status==='conflicting' ? 'Unavailable: confirmed anchors conflict' : 'Unavailable: repeated dates within a feed';
    const detail=el('details'),summary=el('summary',`${item.label || 'Recorder'} · ${item.method==='modified'?'Modified dates':'Filename dates'} · ${status}`);
    detail.append(summary,el('p',`${item.independent_flights} independent flight(s)${item.tentative?' · tentative':''} · raw StickCam − FPV modified difference: ${item.raw_modified_difference_seconds==null?'unavailable':offsetText(item.raw_modified_difference_seconds)}`,'hint'));
    for (const anchor of item.anchors || []) detail.append(el('p',`${anchor.radio_video} ↔ ${anchor.fpv_video} · saved offset ${offsetText(anchor.offset_seconds)}`,'hint'));
    container.append(detail);
  }
  if (!support?.length && !anchors.length) container.append(el('p','No independently confirmed date model is available for this pair yet.','hint'));
}
function renderClocks(data) {
  const models=data.learned_clocks || [];
  const modified=models.filter(m=>m.method==='modified');
  message('clock-summary',modified.length ? `Recorder modified-date clocks: ${modified.map(m=>`${m.label} ${offsetText(m.delta_seconds)} · ${m.independent_flights} flight(s)${m.tentative?' · tentative':''}${m.consistent?'':' · conflicting; disabled'}`).join('; ')}.` :
    data.clock?.delta_seconds!=null ? `Tentative content-derived FPV − StickCam clock: ${offsetText(data.clock.delta_seconds)}. Independently confirm a pair and label its recorder to teach modified dates.` : 'Confirm a content match to learn its recorder’s modified-date difference. Different drones can have different clocks.');
  $('clock-warnings').replaceChildren(...(data.clock_warnings || []).map(text=>el('p',text)));
  $('modified-kind').value=data.clock_settings?.modified_kind || 'end';
  $('modified-tolerance').value=String(data.clock_settings?.modified_tolerance || 5);
  $('clock-filenames').checked=data.clock_settings?.use_filenames!==false;
  const list=$('clock-anchor-list');list.replaceChildren();
  for (const model of models) {
    list.append(el('h3',`${model.label} · ${model.method==='modified'?'Modified dates':'Filename dates'} · ${model.independent_flights} flight(s) · ±${model.tolerance}s${model.consistent?'':' · conflicting'}`));
    for (const anchor of model.anchors) list.append(el('p',`${anchor.radio_video} ↔ ${anchor.fpv_video} · sync ${offsetText(anchor.offset_seconds)} · raw modified difference ${anchor.raw_modified_difference_seconds==null?'unavailable':offsetText(anchor.raw_modified_difference_seconds)}`,'hint'));
  }
  if (!models.length) list.append(el('p','No independent anchors yet. A date-only confirmation does not teach the camera clock.','hint'));
  const rejected=data.clock_rejections || [];
  show('clock-rejections',rejected.length>0);
  $('clock-rejection-rows').replaceChildren(...rejected.map(item=>{
    const row=el('div',undefined,'actions');
    row.append(el('span',`${item.radio_video} ↔ ${item.fpv_video} · ${item.clock_label} · rejected offset ${offsetText(item.offset_seconds)}`),
      action('Restore alignment suggestion',guarded(async()=>{
        const token=state.jobToken,result=await api(path(`/clock-rejections/${part(item.id)}`),{method:'DELETE'});
        if(token!==state.jobToken)return;
        state.results=result; renderResults(); renderWorkflow(); message('clock-message','Clock alignment suggestion restored.');
      },'clock-message')));return row;
  }));
  if (state.pair) {
    const row=data.rows.find(r=>r.radio_video===state.pair.radio_video && r.fpv_video===state.pair.fpv_video);
    clockDetails($('pair-clock-details'),row?.clock_support,models.flatMap(m=>m.anchors.filter(a=>a.radio_video===state.pair.radio_video && a.fpv_video===state.pair.fpv_video)),state.pair.timing_only || state.pair.date_suggestion || state.pair.verification==='date_only');
  }
}
$('refresh-clock-suggestions').onclick=guarded(async()=>{
  const token=state.jobToken;
  const result=await api(path('/clocks'),{method:'PATCH',json:{modified_kind:$('modified-kind').value,modified_tolerance:Number($('modified-tolerance').value),use_filenames:$('clock-filenames').checked}});
  if(token!==state.jobToken)return;
  state.results=result;renderResults();message('clock-message','Date evidence and suggestions updated without rescanning audio or motion.');
},'clock-message');
$('candidate-filter').onchange = () => { state.page = 0; renderCandidatePage(); };
$('review-filter').onchange = () => { state.page = 0; renderCandidatePage(); };
$('previous-page').onclick = () => { state.page--; renderCandidatePage(); }; $('next-page').onclick = () => { state.page++; renderCandidatePage(); };

// The shared timeline ends at the overlap boundary. Source timestamps are always
// derived from the saved start trims, independently of source frame rates.
function pausePair() {
  state.playing = false; state.buffering = false; for(const video of [$('radio-preview'),$('fpv-preview')]) setPreviewPlaybackIntent(video,false); state.startingPlayback = false; cancelAnimationFrame(state.animation); $('radio-preview').pause(); $('fpv-preview').pause(); $('fpv-preview').playbackRate = 1;
  $('pair-play').textContent = 'Play'; for(const kind of ['radio','fpv'])show(`${kind}-preview-buffer`,false);
}
function applyAudio() { $('radio-preview').muted = $('preview-audio').value !== 'radio'; $('fpv-preview').muted = $('preview-audio').value !== 'fpv'; }
function previewFrameRate(){return Number($('export-fps').value) || Number(state.pair?.source_metadata?.radio?.fps) || 30;}
function elapsed() { return state.pair ? Math.max(0, $('radio-preview').currentTime - state.pair.radio_start) : 0; }
function seekPair(at) {
  if (!state.pair) return;
  const value = Math.max(0, Math.min(at, Math.max(0, state.pair.overlap_duration - 1 / previewFrameRate())));
  $('radio-preview').currentTime = state.pair.radio_start + value; $('fpv-preview').currentTime = state.pair.fpv_start + value;
  $('pair-seek').value = value; message('pair-time', `${time(value, true)} / ${time(state.pair.overlap_duration)}`);
}
function intervalBars() {
  $('overlap-bars').replaceChildren();
  for (const [kind, label] of [['radio', 'StickCam'], ['fpv', 'FPV']]) {
    const duration = state.pair[kind + '_duration'], start = state.pair[kind + '_start'];
    const row = el('div', undefined, 'interval-row'), track = el('div', undefined, 'interval-track'), before = el('div'), shared = el('div', undefined, 'interval-shared');
    before.style.width = `${start / duration * 100}%`; shared.style.width = `${state.pair.overlap_duration / duration * 100}%`; track.append(before, shared); row.append(el('span', label), track);
    $('overlap-bars').append(row, el('div', `${time(start, true)} → ${time(start + state.pair.overlap_duration, true)} of ${time(duration)} source`, 'interval-label'));
  }
}
async function openPair(radioName, fpvName, customOffset = null, dateSuggestion = false, rhythmSuggestion = false, manualOptions = null) {
  const token = ++state.viewerToken, jobToken = state.jobToken;
  manualBrowser.pause(); pauseFlight(); pausePair(); show('portrait-panel', false); state.pair = null; openPanel('viewer-panel'); message('viewer-message', 'Loading both videos…');
  $('open-portrait').disabled = true;
  $('export-pair').disabled = $('save-alignment').disabled = true; $('pair-play').disabled = true;
  $('viewer-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
  if (!state.results) {
    await loadResults();
    if(token!==state.viewerToken || jobToken!==state.jobToken)return;
  }
  const selected=state.results.rows.find(row=>row.radio_video===radioName && row.fpv_video===fpvName);
  if(!manualOptions && customOffset===null) {
    if(!selected?.alignment_available)throw new Error(selected?.alignment_reason || 'No matching alignment is available. Open Find a pair manually to align these recordings.');
    customOffset=selected.alignment_offset_seconds;
    dateSuggestion ||= selected.alignment_method==='timestamp';
    rhythmSuggestion ||= selected.alignment_method==='gimbal_activity'?'gimbal_activity':selected.alignment_method==='rhythm';
  }
  const records=state.job.inputs;
  $('review-radio').replaceChildren(...records.radio.map(record=>{const option=el('option',clipLabel(record));option.value=record.name;return option;}));$('review-radio').value=radioName;
  const candidates=state.results.rows.filter(row=>row.radio_video===radioName).sort((a,b)=>candidateStrength(b)-candidateStrength(a));
  $('review-fpv').replaceChildren(...records.fpv.map(record=>{const row=candidates.find(r=>r.fpv_video===record.name),option=el('option',`${clipLabel(record)} · ${row?.alignment_available?(row.manual_pair?'manual':row.timing_only?'dates':candidateStrength(row).toFixed(3)):'no alignment'}`);option.value=record.name;option.disabled=!manualOptions && !row?.alignment_available;return option;}));$('review-fpv').value=fpvName;
  const analyzed=candidates.some(row=>row.fpv_video===fpvName);
  const manual=!!manualOptions || !analyzed;
  const info=await api(path('/pair-info'),{method:'POST',json:{radio_video:radioName,fpv_video:fpvName,offset_seconds:customOffset??(manual?0:null),manual_pair:manual,sync_points:manualOptions?.sync_points}});
  if(manualOptions && JSON.stringify(manualOptions.source_revisions)!==JSON.stringify(info.source_revisions))throw new Error('A source changed since the manual players were loaded. Reload both recordings and mark the event again.');
  if (token !== state.viewerToken || jobToken !== state.jobToken) return;
  state.pair = info; info.date_suggestion=dateSuggestion;info.rhythm_suggestion=rhythmSuggestion;info.manual_sync_points=manualOptions?.sync_points;info.manual_recorder=manualOptions?.recorder ?? state.job.inputs.fpv.find(r=>r.name===fpvName)?.fpv_recorder ?? '';
  fingerprintReview.render();
  renderSyncPoints();$('no-counterpart').textContent=state.job.no_counterpart?.includes(info.fpv_video)?'Restore FPV counterpart search':'No counterpart for this FPV';
  $('independent-content-check').checked=!!info.reviewed && info.verification==='independent';show('independent-check-wrap',info.manual_pair || !!info.timing_only || dateSuggestion || rhythmSuggestion || info.verification!=='independent');
  renderPairRhythm(rhythmSuggestion === 'gimbal_activity' ? info.gimbal_activity : info.rhythm || info.gimbal_activity);
  clockDetails($('pair-clock-details'),info.clock_support,info.clock_anchors,info.timing_only || info.date_suggestion || info.verification==='date_only');
  selectMotionPair(info);
  $('radio-heading').textContent = radioName; $('fpv-heading').textContent = fpvName; $('review-offset').value = info.offset_seconds.toFixed(6);
  $('review-state').textContent = info.reviewed ? 'Reviewed alignment' : info.review_reason || (info.manual_pair?'Manual alignment · check shared visible events':'Automatic estimate · review');
  $('replace-owner').checked = false; show('replace-owner-wrap', !!info.existing_owner); $('replace-owner-wrap').title = info.existing_owner ? `Currently saved with ${info.existing_owner}` : '';
  $('pair-audio-toggle').checked = false; show('pair-audio-charts', false);
  const candidate = candidates.find(row => row.fpv_video === fpvName);
  if (customOffset !== null && Math.abs(customOffset - (candidate?.offset_seconds ?? customOffset)) > 1e-7) $('review-state').textContent = 'Unsaved alignment adjustment';
  const motionEvidence = candidate?.motion_included === false ? 'motion not used' : `${candidate?.reliable_axes || 0} motion axes · timing peak margin ${Number(candidate?.peak_margin || 0).toFixed(3)}`;
  message('pair-evidence', `${customOffset !== null && Math.abs(customOffset - (candidate?.offset_seconds ?? customOffset)) > 1e-7 ? 'Suggested alignment: confirm shared content; the original axis/audio scores below were measured at another offset. ' : ''}${info.evidence} · score ${info.score.toFixed(3)} · ${motionEvidence} · candidate margin ${Number(candidate?.candidate_margin || 0).toFixed(3)}. ${candidate?sectionsText(candidate):''}`);
  if(info.manual_pair)message('pair-evidence','Manual visual alignment. No audio, gyro or StickCam extraction was required, and no automated match score is claimed. Check more than one event where possible; confirming this pair teaches its recorder’s date clock.');
  if(rhythmSuggestion) { const evidence=rhythmSuggestion==='gimbal_activity'?info.gimbal_activity:info.rhythm; message('pair-evidence',`Experimental ${rhythmSuggestion==='gimbal_activity'?'tip-free gimbal movement':'derived-input rhythm'} · score ${evidence?.score?.toFixed(3)} · resolution ${evidence?.timing_resolution_seconds}s plus unknown response delay. Check shared visible events and refine sync before confirming.`); }
  $('pair-seek').max = info.overlap_duration; intervalBars(); applyAudio();
  await Promise.all([loadVideo($('radio-preview'), 'radio', radioName, () => token === state.viewerToken && jobToken === state.jobToken, 'viewer-message', $('playback-proxy').checked, info.radio_start),
    loadVideo($('fpv-preview'), 'fpv', fpvName, () => token === state.viewerToken && jobToken === state.jobToken, 'viewer-message', $('playback-proxy').checked, info.fpv_start)]);
  if (token !== state.viewerToken || jobToken !== state.jobToken) return;
  seekPair(0); $('pair-play').disabled = $('save-alignment').disabled = false; $('export-pair').disabled = !info.reviewed;
  message('viewer-message', `Shared interval: ${time(info.overlap_duration)}. Check the beginning and end, then save your alignment.`);
  $('open-portrait').disabled = false;
  if(manualOptions?.sync_points?.length)seekPair(manualOptions.sync_points[0].radio_time-info.radio_start);
  renderResults();
}
$('review-radio').onchange = guarded(async () => {
  const name = $('review-radio').value, candidates = state.results.rows.filter(row => row.radio_video === name && row.alignment_available && !row.rejected).sort((a, b) => candidateStrength(b)-candidateStrength(a));
  if(state.pair?.manual_pair)return openPair(name,$('review-fpv').value,0,false,false,{recorder:state.pair.manual_recorder});
  const candidate=candidates.find(row=>row.reviewed || row.global_assignment) || candidates[0];
  if(!candidate){$('review-radio').value=state.pair?.radio_video || '';throw new Error('No matching alignment was found for this StickCam. Save its radio regions or find a pair manually.');}
  await openCandidate(candidate);
}, 'viewer-message');
$('review-fpv').onchange = guarded(() => state.pair?.manual_pair?openPair($('review-radio').value,$('review-fpv').value,0,false,false,{recorder:state.pair.manual_recorder}):openPair($('review-radio').value, $('review-fpv').value), 'viewer-message');
async function updateOffset() {
  if (!state.pair) return;
  const at = $('radio-preview').currentTime, old = state.pair, token = state.viewerToken, offsetToken = ++state.offsetToken;
  pausePair();
  const info = await api(path('/pair-info'), { method: 'POST', json: { radio_video: old.radio_video, fpv_video: old.fpv_video, offset_seconds: Number($('review-offset').value),manual_pair:!!old.manual_pair,sync_points:old.manual_sync_points } });
  if (token !== state.viewerToken || offsetToken !== state.offsetToken) return;
  info.date_suggestion=old.date_suggestion;info.rhythm_suggestion=old.rhythm_suggestion;info.manual_sync_points=old.manual_sync_points;info.manual_recorder=old.manual_recorder; state.pair = info; $('export-pair').disabled = !info.reviewed; $('pair-audio-toggle').checked = false; show('pair-audio-charts', false); $('pair-seek').max = info.overlap_duration; intervalBars(); seekPair(at - info.radio_start);
  renderSyncPoints();
  $('review-state').textContent = 'Unsaved alignment adjustment'; message('viewer-message', 'Offset adjusted. Save pairing & alignment to keep it.');
}
$('review-offset').onchange = guarded(updateOffset, 'viewer-message');
for (const [id, direction] of [['offset-back', -1], ['offset-forward', 1]]) $(id).onclick = guarded(async () => {
  if (!state.pair) return; $('review-offset').value = (Number($('review-offset').value) + direction / previewFrameRate()).toFixed(6); await updateOffset();
}, 'viewer-message');
for (const [id, direction] of [['step-back', -1], ['step-forward', 1]]) $(id).onclick = () => { pausePair(); seekPair(elapsed() + direction / previewFrameRate()); };
$('pair-seek').oninput = () => { pausePair(); seekPair(Number($('pair-seek').value)); syncMotionCursor(); };
$('preview-audio').onchange = applyAudio;
$('playback-proxy').onchange = guarded(async () => {
  storage.setItem('fpv-fast-preview', $('playback-proxy').checked ? '1' : '0');
  if (!state.pair) return;
  const current = state.pair, at = elapsed();
  await openPair(current.radio_video,current.fpv_video,Number($('review-offset').value),!!current.date_suggestion,current.rhythm_suggestion || false,current.manual_pair?{recorder:current.manual_recorder,sync_points:current.manual_sync_points,source_revisions:current.source_revisions}:null);
  seekPair(at);
}, 'viewer-message');
$('pair-play').onclick = guarded(async () => {
  if (state.playing) { pausePair(); return; } if (!state.pair) return;
  pauseMotionPreview();
  if (elapsed() >= state.pair.overlap_duration - .1) seekPair(0);
  manualBrowser.pause();state.playing = true; state.buffering=true; for(const video of [$('radio-preview'),$('fpv-preview')]) setPreviewPlaybackIntent(video,true); $('pair-play').textContent = 'Pause'; playbackTick();
}, 'viewer-message');
function playbackTick() {
  drawPortrait();
  if (!state.playing || !state.pair) return;
  const radio = $('radio-preview'), fpv = $('fpv-preview'), at = elapsed();
  const previewEnd = $('portrait-panel').checkVisibility() && portrait.pair ? Number($('portrait-out').value) : state.pair.overlap_duration;
  if (at >= previewEnd - .5 / previewFrameRate() || radio.ended || fpv.ended) { pausePair(); return; }
  const remaining=state.pair.overlap_duration-at, refill=Math.min(1.5,Math.max(.05,remaining-.1));
  const ready=radio.readyState>=3 && fpv.readyState>=3 && !radio.seeking && !fpv.seeking;
  if(!ready || (state.buffering && (bufferedAhead(radio)<refill || bufferedAhead(fpv)<refill))) {
    state.buffering=true; radio.pause(); fpv.pause();
  }
  else {
    state.buffering=false;
    const expected = state.pair.fpv_start + at, drift = fpv.currentTime - expected;
    if (Math.abs(drift) > .14 && performance.now() - state.lastCorrection > 300) { fpv.currentTime = expected; state.lastCorrection = performance.now(); }
    else fpv.playbackRate = Math.abs(drift) > .035 ? (drift > 0 ? .97 : 1.03) : 1;
    if ((radio.paused || fpv.paused) && !state.startingPlayback) {
      const token = state.viewerToken;
      state.startingPlayback = true;
      Promise.all([radio.play(), fpv.play()]).catch(error => { if (token === state.viewerToken && state.playing && error.name !== 'AbortError') { pausePair(); message('viewer-message', error.message, true); } }).finally(() => { if (token === state.viewerToken) state.startingPlayback = false; });
    }
  }
  for (const [kind,video] of [['radio',radio],['fpv',fpv]]) {
    show(`${kind}-preview-buffer`,state.buffering || video.seeking);
    $(`${kind}-preview-buffer`).textContent=video.seeking?'Seeking…':video.readyState<3 || bufferedAhead(video)<refill?'Buffering…':'Waiting for the other preview…';
  }
  syncMotionCursor();
  $('pair-seek').value = at; message('pair-time', `${time(at, true)} / ${time(state.pair.overlap_duration)}`);
  state.animation = requestAnimationFrame(playbackTick);
}
function bufferedAhead(video) {
  for(let i=0;i<video.buffered.length;i++) if(video.buffered.start(i)<=video.currentTime+.04 && video.buffered.end(i)>video.currentTime) return video.buffered.end(i)-video.currentTime;
  return 0;
}
for (const id of ['radio-preview', 'fpv-preview']) $(id).onended = pausePair;
async function saveAlignment() {
  if (!state.pair) return;
  const initialToken = state.viewerToken;
  // Commit the exact offset displayed, including a typed value before blur.
  if (Math.abs(Number($('review-offset').value) - state.pair.offset_seconds) > 1e-7) await updateOffset();
  if (initialToken !== state.viewerToken || !state.pair) return;
  const current = state.pair, token = state.viewerToken;
  await api(path(`/pairs/${part(current.radio_video)}`), { method: 'PUT', json: { fpv_video: current.fpv_video, offset_seconds: current.offset_seconds, replace_owner: $('replace-owner').checked, independent_check:$('independent-content-check').checked, date_suggestion:!!current.date_suggestion, rhythm_suggestion:!!current.rhythm_suggestion,manual_pair:!!current.manual_pair,fpv_recorder:current.manual_recorder,source_revisions:current.source_revisions,...(current.manual_sync_points?{sync_points:current.manual_sync_points}:{}) } });
  if (token !== state.viewerToken) return;
  state.job = await api(path(''));if(token!==state.viewerToken)return;manualBrowser.render();current.reviewed = true; $('export-pair').disabled = false; show('replace-owner-wrap', false); $('review-state').textContent = 'Reviewed alignment'; message('viewer-message', 'Segment pairing and offset saved. Other segments for this StickCam are kept.'); await loadResults();
  if ($('auto-find-segments').checked) await startRelatedSegments(current);
  return token === state.viewerToken ? current : null;
}
$('save-alignment').onclick = guarded(saveAlignment, 'viewer-message');
$('reject-pair').onclick = guarded(async () => {
  if (!state.pair) return; pausePair();
  await api(path(`/pairs/${part(state.pair.radio_video)}`), { method: 'PUT', json: { fpv_video:state.pair.fpv_video,offset_seconds:state.pair.offset_seconds,reject:true,manual_pair:!!state.pair.manual_pair,source_revisions:state.pair.source_revisions } });
  state.pair.reviewed = false; $('export-pair').disabled = true; await loadResults(); $('review-state').textContent = 'Segment rejected'; message('viewer-message', 'This FPV segment is unmatched. Other saved segments for this StickCam are kept.');
}, 'viewer-message');
$('reject-pair').title='Reject these two recordings as a pair in every recorder group. To reject only a predicted offset, use Reject this clock alignment in date suggestions.';

function renderRelatedSegments() {
  const groups = state.results?.segment_candidates || [];
  show('related-segments', groups.length > 0 || segmentScan.running);
  const list = $('related-segment-rows'); list.replaceChildren();
  for (const group of groups) {
    for (const candidate of group.clips) {
      const matched = state.results.rows.find(r => r.radio_video === candidate.radio_video && r.fpv_video === candidate.fpv_video);
      const row = el('tr'), controls = el('td');
      controls.append(action(matched?.reviewed ? 'View saved segment' : 'Preview predicted alignment', guarded(() => openPair(candidate.radio_video, candidate.fpv_video, matched?.reviewed ? null : candidate.offset_seconds), 'viewer-message')));
      row.append(el('td', candidate.radio_video), el('td', candidate.fpv_video),
        el('td', `${time(candidate.radio_start, true)}–${time(candidate.radio_start + candidate.overlap_seconds, true)}`),
        el('td', matched?.reviewed ? 'Reviewed & saved' : matched?.rejected ? 'Rejected · review again if needed' : `Timestamp candidate · ${candidate.note}`), controls);
      list.append(row);
    }
  }
}
async function startRelatedSegments(pair) {
  if (!pair || !pair.reviewed) return;
  await loadResults();
  if (state.results?.match_scope?.timing_suggestions?.length) {openPanel('timing-suggestions');$('timing-suggestions').scrollIntoView({behavior:'smooth',block:'start'});}
  else message('viewer-message','Alignment saved. No additional reliable date overlaps were found; check the learned camera dates or find another content anchor.');
}
function watchRelatedSegments(task, jobId, jobToken) {
  clearTimeout(segmentScan.timer); const token = ++segmentScan.token;
  segmentScan.task = task; segmentScan.running = true;
  show('related-segments'); show('segment-scan-cancel'); show('segment-scan-progress');
  const poll = async () => {
    if (token !== segmentScan.token || jobToken !== state.jobToken) return;
    try {
      const result = await api(`/api/jobs/${jobId}/motion-review/${task}`);
      if (token !== segmentScan.token || jobToken !== state.jobToken) return;
      if (result.progress) {
        $('segment-scan-progress').value = result.progress.percent;
        message('segment-scan-message', `${result.progress.completed}/${result.progress.total} FPV files checked · ${Number(result.progress.percent).toFixed(1)}%`);
      } else { $('segment-scan-progress').removeAttribute('value'); message('segment-scan-message', result.message); }
      if (['queued','running'].includes(result.status)) { segmentScan.timer = setTimeout(poll, 1000); return; }
      segmentScan.running = false; show('segment-scan-cancel', false); show('segment-scan-progress', false);
      await loadResults(); show('related-segments');
      message('segment-scan-message', `${result.message}${result.failures?.length ? ` ${result.failures.length} source(s) could not be checked.` : ''}`, result.status === 'failed');
    } catch(error) { message('segment-scan-message', `Connection interrupted; retrying… ${error.message}`, true); segmentScan.timer = setTimeout(poll, 3000); }
  };
  poll();
}
$('find-related-segments').onclick = guarded(async () => { const pair = await saveAlignment(); if (pair && !$('auto-find-segments').checked) await startRelatedSegments(pair); }, 'viewer-message');
$('segment-scan-cancel').onclick = guarded(async () => {
  if (segmentScan.task) await api(path(`/motion-review/${segmentScan.task}/cancel`), {method:'POST'});
}, 'segment-scan-message');

// Each export has its own durable status and download links, independent of the
// pair currently selected in the viewer.
function renderExports() {
  if (!state.job) return;
  if(!$('export-destination').value)$('export-destination').value=state.job.export_destination || '';
  show('exports-panel', !!state.results || !!state.job.exports?.length);
  $('export-list').replaceChildren();
  if (!state.job.exports?.length) $('export-list').append(el('p', 'No exports yet. Save an alignment and export a pair.', 'hint'));
  for (const item of state.job.exports || []) {
    const row = el('div', undefined, 'export-row'); row.id = 'export-' + item.id; row.append(el('p', `Export ${item.id} · ${item.message}`)); $('export-list').append(row);
    pollExport(state.job.id, item.id, state.jobToken);
  }
}
async function requestExport(pairs) {
  const id = state.job.id, token = state.jobToken;
  const result = await api(`/api/jobs/${id}/exports`, { method: 'POST', json: { pairs, fps: $('export-fps').value==='original'?null:Number($('export-fps').value), profile: $('export-profile').value, stabilize_radio: $('export-radio-framing').value === 'stabilized', acknowledge_sync_warning:$('sync-export-ack').checked, destination:$('export-destination').value, verify_frames:$('export-verify-frames').checked } });
  if (token !== state.jobToken) return;
  state.job.exports ||= []; if (!state.job.exports.some(item => item.id === result.id)) state.job.exports.push({ id: result.id, status: 'queued', message: 'Preparing aligned clips' }); renderExports();
  openPanel('exports-panel'); $('exports-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
}
$('export-pair').onclick = guarded(async () => { const pair = state.pair; if (!pair?.reviewed || Math.abs(Number($('review-offset').value) - pair.offset_seconds) > 1e-7) throw new Error('Save the current alignment before exporting.'); await requestExport([{radio_video:pair.radio_video, fpv_video:pair.fpv_video, offset_seconds:pair.offset_seconds}]); }, 'viewer-message');
$('export-batch').onclick = guarded(async () => {
  const pairs = state.results.rows.filter(row => row.global_assignment && row.reviewed).map(row => ({ radio_video: row.radio_video, fpv_video: row.fpv_video, offset_seconds: row.offset_seconds }));
  if (pairs.length) await requestExport(pairs);
}, 'viewer-message');
async function pollExport(jobId, exportId, token) {
  clearTimeout(state.exportTimers.get(exportId));
  try {
    const status = await api(`/api/jobs/${jobId}/exports/${exportId}`);
    if (token !== state.jobToken) return;
    const row = $('export-' + exportId); if (!row) return;
    row.replaceChildren(el('p', `Export ${exportId} · ${status.message}`, ['failed', 'interrupted'].includes(status.status) ? 'error' : undefined));
    const links = el('div', undefined, 'actions');
    if (status.download_url) {
      const zip = el('a', 'Download ZIP'); zip.href = status.download_url; links.append(zip);
      for (const filename of status.files) { const link = el('a', filename.split('/').at(-1)); link.href = `/api/jobs/${jobId}/exports/${exportId}/files/${filename.split('/').map(part).join('/')}`; links.append(link); }
    }
    if (['queued', 'running'].includes(status.status)) links.append(action('Cancel export', guarded(async () => { await api(`/api/jobs/${jobId}/exports/${exportId}/cancel`, { method: 'POST' }); await pollExport(jobId, exportId, token); }, 'viewer-message')));
    if(status.eta_seconds!=null && status.status==='running')row.append(el('p',`About ${time(status.eta_seconds)} remaining · ${time(status.elapsed_seconds)} elapsed`,'hint'));
    if(status.directory)row.append(el('p',status.directory,'hint'));
    if(status.percent!=null){const progress=el('progress');progress.max=100;progress.value=status.percent;row.append(progress);}
    row.append(links);
    if (!['queued', 'running'].includes(status.status)) return;
  } catch (error) { if (token !== state.jobToken) return; }
  state.exportTimers.set(exportId, setTimeout(() => pollExport(jobId, exportId, token), 2000));
}
document.addEventListener('keydown', event => {
  if (!state.pair || ['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON', 'CANVAS'].includes(document.activeElement.tagName)) return;
  if (event.code === 'Space') { event.preventDefault(); $('pair-play').click(); }
  if (event.code === 'ArrowLeft' || event.code === 'ArrowRight') { event.preventDefault(); pausePair(); seekPair(elapsed() + (event.code === 'ArrowLeft' ? -1 : 1) / previewFrameRate()); }
});
window.addEventListener('pagehide', () => { pausePair(); state.uploadController?.abort(); });
(async () => {
  let system;
  try {
    system = await waitForServer();
  } catch (error) {
    message('system', 'Server connection failed', true);
    message('input-error', error.message, true);
    return;
  }
  try {
    const tracking = system.tracking || {};
    message('tracking-capability', tracking.available ? `CUDA available · ${tracking.device}. Accelerates optical flow, perspective crops and stick template matching. SIFT reacquisition and geometry/confidence checks use CPU.` : `CPU tracking · ${tracking.reason || 'GPU tracking unavailable'}`); message('system', `${bytes(system.free_bytes)} free · local processing · LAN ready${system.ffmpeg && system.ffprobe ? '' : ' · install FFmpeg before analyzing'}`);
    $('playback-proxy').checked = storage.getItem('fpv-fast-preview') !== '0';
    try { await loadRadioModels(); } catch (error) { message('radio-shape-message', error.message, true); }
    await history();
    try { await loadRatePresets(); } catch (error) { message('rate-preset-message', error.message, true); }
    const saved = storage.getItem('fpv-session'); if (saved) await selectJob(saved);
  } catch (error) { message('input-error', error.message, true); }
})();

// Motion inspection uses one isolated extraction task and bounded, peak-preserving plot payloads.
const motion = { token: 0, windowToken: 0, id: null, data: null, timer: null, cursor: null, hidden: new Set(), drag: null, loading: false };
const audioReview = { token: 0, windowToken: 0, id: null, jobId: null, data: null, timer: null, running: false };
const gyroScan = { token: 0, id: null, jobId: null, timer: null, running: false };
const motionPreview = { token: 0, pair: null, playing: false, frame: null, pending: false, lastDraw: 0, lastCorrection: 0 };
const motionColors = ['#f06f62', '#72cf6b', '#49a7f2', '#dda354'];
function resetMotionReview() {
  resetAudioReview();
  resetGyroScan();
  resetMotionPreview();
  $('radio-stabilize-video').pause(); show('radio-stabilize-media', false);
  motion.token++; motion.windowToken++; clearTimeout(motion.timer); motion.id = motion.data = motion.cursor = null; motion.loading = false;
  show('motion-charts', false); show('motion-cancel', false); show('motion-progress-bar', false); show('motion-progress-detail', false); $('motion-load').disabled = false;
  message('motion-message', 'Choose recordings and load their signals.');
}
function updateMotionOptions() {
  for (const [kind, id] of [['fpv', 'motion-fpv'], ['radio', 'motion-radio']]) {
    const select = $(id), selected = select.value;
    const records = state.job.inputs[kind];
    if (select.dataset.names === JSON.stringify(records.map(r => [r.name, r.metadata?.duration]))) continue;
    select.dataset.names = JSON.stringify(records.map(r => [r.name, r.metadata?.duration]));
    select.replaceChildren(...(kind === 'radio' ? [new Option('FPV only', '')] : []), ...records.map(r => new Option(clipLabel(r), r.name)));
    if ([...select.options].some(o => o.value === selected)) select.value = selected;
  }
  $('motion-use-pair').disabled = !state.pair;
}
function selectMotionPair(pair) {
  updateMotionOptions(); $('motion-fpv').value = pair.fpv_video; $('motion-radio').value = pair.radio_video;
  $('motion-offset').value = pair.offset_seconds.toFixed(6); $('motion-use-pair').disabled = false;
  if (motion.data?.fpv_video !== pair.fpv_video || motion.data?.radio_video !== pair.radio_video) resetMotionReview();
  else refreshMotionWindow(true).catch(error => message('motion-message', error.message, true));
}
for (const id of ['motion-fpv', 'motion-radio', 'motion-source']) $(id).onchange = resetMotionReview;
$('motion-use-pair').onclick = guarded(async () => {
  if (!state.pair) return; selectMotionPair(state.pair); await loadMotionSignals();
}, 'motion-message');
async function loadMotionSignals() {
  openPanel('motion-panel');
  const token = ++motion.token, jobToken = state.jobToken, jobId = state.job.id;
  clearTimeout(motion.timer); motion.data = null; show('motion-charts', false);
  resetMotionPreview();
  resetAudioReview();
  $('motion-load').disabled = true; motion.loading = true;
  $('gyro-scan-start').disabled = true;
  message('motion-message', 'Requesting motion signals…');
  try {
    const task = await api(`/api/jobs/${jobId}/motion-review`, { method: 'POST', json: {
      fpv_video: $('motion-fpv').value, radio_video: $('motion-radio').value, source: $('motion-source').value, sample_fps: Number($('sample-fps').value), match_fps: Number($('match-fps').value), min_overlap: Number($('min-overlap').value), rate_profile: readRateProfile(), ...readMotionSettings()
    }});
    if (token !== motion.token || jobToken !== state.jobToken) return;
    motion.id = task.id; show('motion-cancel', task.status !== 'complete');
    const poll = async () => {
      if (token !== motion.token || jobToken !== state.jobToken) return;
      try {
        const result = await api(`/api/jobs/${jobId}/motion-review/${task.id}?${new URLSearchParams({ offset: $('motion-offset').value, mode: $('motion-view').value })}`);
        if (token !== motion.token || jobToken !== state.jobToken) return;
        message('motion-message', result.message, result.status === 'failed');
        showMotionProgress(result.progress, ['queued', 'running'].includes(result.status));
        if (['queued', 'running'].includes(result.status)) { motion.timer = setTimeout(poll, 1500); return; }
        motion.loading = false; $('motion-load').disabled = false; show('motion-cancel', false);
        if (result.status === 'complete') {
          motion.data = result; motion.hidden.clear(); motion.cursor = null;
          $('gyro-scan-start').disabled = !result.radio_available || gyroScan.running;
          $('motion-preview-load').disabled = !result.radio_video;
          message('motion-sections', sectionsText(result.sections));
          show('motion-charts'); renderMotionLegends(); updateMotionWindowFields(); drawMotion();
          message('motion-message', result.warnings.join(' ') || 'Signals ready. Compare manoeuvres and inspect the timing.');
          $('motion-fpv-title').textContent = result.fpv_label;
          $('motion-radio-title').textContent = result.radio_available ? `StickCam · Mode ${result.stick_mode || 2} · ${result.rate_profile ? 'requested rates + throttle position' : 'detected stick positions'}` : 'StickCam · no calibrated motion';
          message('motion-note', result.fpv_note + ' Normalisation scales each trace by its full-recording 95th percentile; colours do not imply sensor-to-drone axis calibration.');
          if (motion.autoPreview) { motion.autoPreview = false; $('motion-preview-load').click(); }
          if ($('motion-audio-toggle').checked) loadAudioReview().catch(error => message('motion-audio-message', error.message, true));
          refreshStickData(true).catch(error => message('stick-data-message', error.message, true));
        }
      } catch (error) {
        if (token === motion.token) { motion.loading = false; $('motion-load').disabled = false; show('motion-cancel', false); message('motion-message', error.message, true); }
      }
    };
    await poll();
  } catch (error) {
    if (token === motion.token) { motion.loading = false; $('motion-load').disabled = false; message('motion-message', error.message, true); }
  }
}
$('motion-load').onclick = guarded(loadMotionSignals, 'motion-message');
function resetGyroScan() {
  if (gyroScan.running && gyroScan.id) api(`/api/jobs/${gyroScan.jobId}/motion-review/${gyroScan.id}/cancel`, { method: 'POST' }).catch(() => {});
  gyroScan.token++; clearTimeout(gyroScan.timer); gyroScan.id = null; gyroScan.running = false;
  $('gyro-scan-start').disabled = true; show('gyro-scan-cancel', false); show('gyro-scan-progress', false);
  $('gyro-scan-rows').replaceChildren(); $('gyro-scan-errors').replaceChildren(); show('gyro-scan-skipped', false);
  message('gyro-scan-message', 'Load calibrated StickCam signals to enable the scan.'); message('gyro-scan-settings', '');
}
$('gyro-scan-start').onclick = guarded(async () => {
  if (!motion.id || !motion.data?.radio_available) throw new Error('Load calibrated StickCam signals first.');
  const token = ++gyroScan.token, jobId = state.job.id, jobToken = state.jobToken;
  gyroScan.running = true; gyroScan.jobId = jobId; $('gyro-scan-start').disabled = true;
  $('gyro-scan-rows').replaceChildren(); show('gyro-scan-skipped', false);
  const finish = () => { gyroScan.running = false; $('gyro-scan-start').disabled = !motion.data?.radio_available; show('gyro-scan-cancel', false); show('gyro-scan-progress', false); };
  try {
    const task = await api(`/api/jobs/${jobId}/motion-review/${motion.id}/gyro-scan`, { method: 'POST' });
    if (token !== gyroScan.token || jobToken !== state.jobToken) return;
    gyroScan.id = task.id; show('gyro-scan-cancel', task.status !== 'complete');
    const poll = async () => {
      if (token !== gyroScan.token || jobToken !== state.jobToken) return;
      try {
        const result = await api(`/api/jobs/${jobId}/motion-review/${task.id}`);
        if (token !== gyroScan.token || jobToken !== state.jobToken) return;
        const running = ['queued', 'running'].includes(result.status);
        show('gyro-scan-progress', running); $('gyro-scan-progress').value = result.progress?.percent ?? 0;
        const progress = result.progress;
        message('gyro-scan-message', running && progress ? `${progress.completed}/${progress.total} FPV clips · ${progress.name || 'completing scan'}` : result.message, result.status === 'failed');
        if (running) { gyroScan.timer = setTimeout(poll, 1500); return; }
        finish(); if (result.status !== 'complete') return;
        renderGyroScan(result);
      } catch (error) { if (token === gyroScan.token) { finish(); message('gyro-scan-message', error.message, true); } }
    };
    await poll();
  } catch (error) { if (token === gyroScan.token) { finish(); message('gyro-scan-message', error.message, true); } }
}, 'gyro-scan-message');
$('gyro-scan-cancel').onclick = guarded(async () => {
  if (gyroScan.id) await api(`/api/jobs/${gyroScan.jobId}/motion-review/${gyroScan.id}/cancel`, { method: 'POST' });
}, 'gyro-scan-message');
function renderGyroScan(result) {
  message('gyro-scan-message', `${result.clear_candidate ? 'A distinct candidate was found; confirm its timing.' : 'No clear gyro match found; inspect candidates or improve calibration.'} ${result.candidates.length}/${result.total} clips had usable embedded motion. ${result.note}`);
  const settings = result.settings;
  message('gyro-scan-settings', `StickCam: ${result.radio_video} · rates ${settings.rate_profile ? settings.rate_profile.model : 'not configured'} · camera uptilt ${settings.camera_uptilt_deg}° · mapping ${settings.imu_orientation} · gyro shift ${offsetText(settings.gyro_video_offset)}`);
  $('gyro-scan-rows').replaceChildren();
  for (const candidate of result.candidates) {
    const row = el('tr'), details = candidate.details;
    const strengths = ['roll', 'pitch', 'yaw'].map(k => (details[`strength_${k}`] ?? 0).toFixed(2)).join(' / ');
    for (const value of [candidate.fpv_video, candidate.source === 'gyro' ? 'Embedded gyro' : 'Embedded orientation', candidate.score.toFixed(3), strengths, offsetText(candidate.offset_seconds), `${candidate.correlated_sections ?? 0}/${candidate.usable_sections ?? 0} · ${Math.round((candidate.section_agreement ?? 0) * 100)}%`]) row.append(el('td', value));
    row.append(el('td', `${candidate.clear ? 'Distinct candidate' : 'Needs review'} · ${candidate.reliable_axes} axes · timing gap ${candidate.peak_margin.toFixed(3)} · clip gap ${candidate.candidate_margin.toFixed(3)}`));
    const inspect = el('td'); inspect.append(action('Plot at offset', guarded(async () => {
      $('motion-fpv').value = candidate.fpv_video; $('motion-radio').value = result.radio_video;
      $('motion-source').value = 'gyro'; $('motion-offset').value = candidate.offset_seconds.toFixed(6);
      loadRateProfile(settings.rate_profile);
      $('camera-uptilt-deg').value = settings.camera_uptilt_deg; $('imu-orientation').value = settings.imu_orientation; $('gyro-video-offset').value = settings.gyro_video_offset;
      await loadMotionSignals();
    }, 'motion-message'))); row.append(inspect); $('gyro-scan-rows').append(row);
  }
  $('gyro-scan-errors').replaceChildren(...result.skipped.map(item => el('li', `${item.fpv_video}: ${item.reason}`)));
  show('gyro-scan-skipped', result.skipped.length > 0);
}
$('motion-cancel').onclick = guarded(async () => {
  if (!motion.id) return; await api(path(`/motion-review/${motion.id}/cancel`), { method: 'POST' });
}, 'motion-message');
function renderMotionLegends() {
  for (const kind of ['fpv', 'radio']) {
    const legend = $(`motion-${kind}-legend`); legend.replaceChildren();
    for (const [i, trace] of motion.data[kind].entries()) {
      const label = el('label'); label.style.color = motionColors[i]; const check = el('input'); check.type = 'checkbox'; check.checked = !motion.hidden.has(`${kind}:${trace.key}`);
      check.onchange = () => { const key = `${kind}:${trace.key}`; if (check.checked) motion.hidden.delete(key); else motion.hidden.add(key); drawMotion(); };
      const value = el('small', trace.unit); value.id = `motion-value-${kind}-${trace.key}`;
      label.append(check, el('span', trace.label), value); legend.append(label);
    }
  }
}
function updateMotionWindowFields() {
  $('motion-start').value = motion.data.window[0].toFixed(3); $('motion-end').value = motion.data.window[1].toFixed(3);
}
async function refreshMotionWindow(full = false, window = null) {
  if (!motion.id || !motion.data) return;
  const token = motion.token, windowToken = ++motion.windowToken, jobToken = state.jobToken;
  const query = new URLSearchParams({ offset: $('motion-offset').value, mode: $('motion-view').value });
  if (!full) { const values = window || motion.data.window; query.set('start', values[0]); query.set('end', values[1]); }
  const result = await api(path(`/motion-review/${motion.id}?${query}`));
  if (token !== motion.token || windowToken !== motion.windowToken || jobToken !== state.jobToken) return;
  if (result.status !== 'complete') throw new Error(result.message);
  motion.data = result; renderMotionLegends(); updateMotionWindowFields(); drawMotion();
  message('motion-sections', sectionsText(result.sections));
  message('motion-message', result.warnings.join(' ') || 'Signals ready. Compare manoeuvres and inspect the timing.');
  message('motion-note', result.fpv_note + ' Normalisation scales each trace by its full-recording 95th percentile.');
  await refreshAudioWindow();
}
$('motion-offset').onchange = guarded(async () => { updateMotionPreviewOffset(); await refreshMotionWindow(true); }, 'motion-message');
$('motion-normalize').onchange = drawMotion;
$('motion-reset').onclick = guarded(() => refreshMotionWindow(true), 'motion-message');
$('motion-window').onclick = guarded(() => refreshMotionWindow(false, [Number($('motion-start').value), Number($('motion-end').value)]), 'motion-message');
function zoomMotion(factor, anchor = null) {
  if (!motion.data) return Promise.resolve();
  const [a, b] = motion.data.window, middle = anchor ?? (a + b) / 2;
  const width = Math.max(.25, Math.min((b - a) * factor, motion.data.bounds[1] - motion.data.bounds[0]));
  const ratio = (middle - a) / (b - a);
  let start = middle - width * ratio;
  start = Math.max(motion.data.bounds[0], Math.min(start, motion.data.bounds[1] - width));
  return refreshMotionWindow(false, [start, start + width]);
}
$('motion-zoom-in').onclick = guarded(() => zoomMotion(.5), 'motion-message');
$('motion-zoom-out').onclick = guarded(() => zoomMotion(2), 'motion-message');
function motionTimeAt(canvas, clientX) {
  const rect = canvas.getBoundingClientRect(), [start, end] = motion.data.window;
  return start + Math.max(0, Math.min(1, (clientX - rect.left - 55) / (rect.width - 75))) * (end - start);
}
function motionValueAt(trace, at) {
  const p = trace.points; let lo = 0, hi = p.length - 1;
  while (lo < hi) { const mid = Math.floor((lo + hi) / 2); if (p[mid][0] < at) lo = mid + 1; else hi = mid; }
  const choices = [p[lo], p[lo - 1]].filter(Boolean).sort((a, b) => Math.abs(a[0] - at) - Math.abs(b[0] - at));
  const nearest = choices[0];
  return nearest && Math.abs(nearest[0] - at) < Math.max(.2, (motion.data.window[1] - motion.data.window[0]) / 400) ? nearest[1] : null;
}
function drawMotion() {
  if (!motion.data) return;
  const normalized = $('motion-normalize').checked;
  show('motion-scene-chart',$('motion-scene-toggle').checked);
  for (const kind of ['fpv', 'radio','scene']) {
    if(kind==='scene'&&!$('motion-scene-toggle').checked)continue;
    const canvas = $(`motion-${kind}-canvas`), rect = canvas.getBoundingClientRect(), ratio = devicePixelRatio || 1;
    if (!rect.width) continue;
    canvas.width = Math.round(rect.width * ratio); canvas.height = Math.round(rect.height * ratio);
    const ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio);
    const w = rect.width, h = rect.height, left = 55, right = w - 20, top = 18, bottom = h - 30;
    const [start, end] = motion.data.window, x = t => left + (t - start) / (end - start) * (right - left);
    const traces = (motion.data[kind] || []).filter(t => !motion.hidden.has(`${kind}:${t.key}`));
    const mixed = new Set(traces.map(t => t.unit)).size > 1;
    const relative = normalized || mixed;
    let maximum = relative ? 1 : 1e-6;
    for (const trace of traces) for (const [, value] of trace.points) if (value !== null) maximum = Math.max(maximum, Math.abs(value / (relative ? trace.scale : 1)));
    maximum *= 1.08;
    const y = value => (top + bottom) / 2 - value / maximum * (bottom - top) / 2;
    ctx.font = '11px system-ui'; ctx.fillStyle = '#aaa99f'; ctx.strokeStyle = '#ffffff12'; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const value = maximum * (1 - i / 2), yy = y(value); ctx.beginPath(); ctx.moveTo(left, yy); ctx.lineTo(right, yy); ctx.stroke(); ctx.fillText(value.toFixed(1), 2, yy + 4);
    }
    for (let i = 0; i <= 5; i++) { const t = start + i / 5 * (end - start), xx = x(t); ctx.beginPath(); ctx.moveTo(xx, top); ctx.lineTo(xx, bottom); ctx.stroke(); ctx.fillText(`${t.toFixed(1)}s`, Math.min(xx - 10, w - 45), h - 9); }
    ctx.fillText(relative ? 'relative' : (traces[0]?.unit || ''), left, 11);
    ctx.save(); ctx.beginPath(); ctx.rect(left, top, right - left, bottom - top); ctx.clip();
    if(kind!=='scene')drawMatchingBands(ctx, motion.data.sections?.windows, x, top, bottom);
    for (const [i, trace] of (motion.data[kind] || []).entries()) {
      if (motion.hidden.has(`${kind}:${trace.key}`)) continue;
      ctx.strokeStyle = motionColors[i]; ctx.lineWidth = 1.3; ctx.beginPath(); let pen = false;
      for (const [t, value] of trace.points) { if (value === null) { pen = false; continue; } const xx = x(t), yy = y(value / (relative ? trace.scale : 1)); if (pen) ctx.lineTo(xx, yy); else ctx.moveTo(xx, yy); pen = true; } ctx.stroke();
      const value = motion.cursor === null ? null : motionValueAt(trace, motion.cursor);
      const output=$(`motion-value-${kind}-${trace.key}`);if(output)output.textContent = value === null ? trace.unit : `${value.toFixed(2)} ${trace.unit}`;
    }
    if (motion.cursor !== null) { ctx.strokeStyle = '#e8e3d899'; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(x(motion.cursor), top); ctx.lineTo(x(motion.cursor), bottom); ctx.stroke(); }
    ctx.restore();
    if (!motion.data[kind]?.length) { ctx.fillStyle = '#aaa99f'; ctx.fillText(kind==='scene'?'No saved overall movement; extract reference signals first':kind === 'radio' ? 'No calibrated StickCam signal available' : 'No embedded orientation available for this source', left + 10, h / 2); }
  }
  $('motion-cursor').textContent = motion.cursor === null ? 'Time axis: seconds on StickCam recording' : `Cursor: ${motion.cursor.toFixed(3)}s on StickCam · ${(motion.cursor - Number($('motion-offset').value)).toFixed(3)}s on FPV`;
  drawAudio();
}
function syncMotionCursor() {
  if (motion.data && state.pair && motion.data.fpv_video === state.pair.fpv_video && motion.data.radio_video === state.pair.radio_video) { motion.cursor = $('radio-preview').currentTime; drawMotion(); }
}
for (const id of ['motion-fpv-canvas', 'motion-radio-canvas','motion-scene-canvas', 'audio-fpv-canvas', 'audio-radio-canvas']) {
  const canvas = $(id);
  canvas.onwheel = event => { if (!motion.data) return; event.preventDefault(); clearTimeout(motion.timer); const at = motionTimeAt(canvas, event.clientX); motion.timer = setTimeout(() => zoomMotion(event.deltaY > 0 ? 1.4 : 1 / 1.4, at).catch(error => message('motion-message', error.message, true)), 100); };
  canvas.onpointerdown = event => { if (!motion.data) return; canvas.setPointerCapture(event.pointerId); motion.drag = { x: event.clientX, window: [...motion.data.window] }; };
  canvas.onpointermove = event => { if (!motion.data || motion.drag) return; motion.cursor = motionTimeAt(canvas, event.clientX); drawMotion(); };
  canvas.onpointerup = guarded(async event => {
    if (!motion.drag || !motion.data) return; const drag = motion.drag; motion.drag = null;
    const delta = event.clientX - drag.x;
    if (Math.abs(delta) > 5) {
      const shift = -delta / (canvas.getBoundingClientRect().width - 75) * (drag.window[1] - drag.window[0]);
      const width = drag.window[1] - drag.window[0], start = Math.max(motion.data.bounds[0], Math.min(drag.window[0] + shift, motion.data.bounds[1] - width));
      await refreshMotionWindow(false, [start, start + width]);
    } else {
      motion.cursor = motionTimeAt(canvas, event.clientX); drawMotion();
      if (motionPreview.pair) { pauseMotionPreview(); seekMotionPreview(motion.cursor - motionPreview.pair.radio_start); }
      if (state.pair && motion.data.fpv_video === state.pair.fpv_video && motion.data.radio_video === state.pair.radio_video && Math.abs(Number($('motion-offset').value) - state.pair.offset_seconds) < 1e-6) { pausePair(); seekPair(motion.cursor - state.pair.radio_start); }
    }
  }, 'motion-message');
  canvas.onpointercancel = () => { motion.drag = null; };
  canvas.onkeydown = guarded(async event => {
    if (!motion.data) return;
    if (['+', '=', '-'].includes(event.key)) { event.preventDefault(); await zoomMotion(event.key === '-' ? 2 : .5); }
    if (['ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); motion.cursor = Math.max(motion.data.window[0], Math.min(motion.data.window[1], (motion.cursor ?? motion.data.window[0]) + (event.key === 'ArrowRight' ? 1 : -1) * (motion.data.window[1] - motion.data.window[0]) / 100)); drawMotion(); }
  }, 'motion-message');
}
new ResizeObserver(() => drawMotion()).observe($('motion-fpv-canvas'));

function drawMatchingBands(ctx, windows, x, top, bottom) {
  if (!$('motion-highlight-toggle').checked) return;
  ctx.fillStyle = '#35d47726'; ctx.strokeStyle = '#35d477aa'; ctx.lineWidth = 2;
  for (const section of windows || []) {
    if (!section.matched) continue;
    ctx.fillRect(x(section.start), top, x(section.end) - x(section.start), bottom - top);
    ctx.beginPath(); ctx.moveTo(x(section.start), top + 1); ctx.lineTo(x(section.end), top + 1); ctx.stroke();
  }
}
function drawAudio() {
  if (!$('motion-audio-toggle').checked || !motion.data) return;
  for (const kind of ['fpv', 'radio']) {
    const canvas = $(`audio-${kind}-canvas`), rect = canvas.getBoundingClientRect(), ratio = devicePixelRatio || 1;
    if (!rect.width) continue;
    canvas.width = Math.round(rect.width * ratio); canvas.height = Math.round(rect.height * ratio);
    const ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio);
    const left = 55, right = rect.width - 20, top = 18, bottom = rect.height - 30, center = (top + bottom) / 2;
    const [start, end] = motion.data.window, x = t => left + (t - start) / (end - start) * (right - left);
    ctx.font = '11px system-ui'; ctx.fillStyle = '#aaa99f'; ctx.strokeStyle = '#ffffff12';
    for (const value of [-1, 0, 1]) { const y = center - value * (bottom - top) / 2; ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke(); ctx.fillText(String(value), 15, y + 4); }
    for (let i = 0; i <= 5; i++) { const t = start + i / 5 * (end - start); ctx.fillText(`${t.toFixed(1)}s`, Math.min(x(t) - 10, rect.width - 45), rect.height - 9); }
    ctx.save(); ctx.beginPath(); ctx.rect(left, top, right - left, bottom - top); ctx.clip();
    const data = audioReview.data?.[kind];
    drawMatchingBands(ctx, audioReview.data?.sections?.windows, x, top, bottom);
    ctx.strokeStyle = '#49a7f2'; ctx.lineWidth = 1;
    const points = data?.points || [];
    const binWidth = points.length > 1 ? Math.min(5, Math.max(1, (x(points.at(-1)[0]) - x(points[0][0])) / points.length)) : 1;
    for (const [time, low, high] of points) {
      ctx.lineWidth = binWidth; ctx.beginPath(); ctx.moveTo(x(time), center - low * (bottom - top) / 2); ctx.lineTo(x(time), center - high * (bottom - top) / 2); ctx.stroke();
    }
    if (motion.cursor !== null) { ctx.lineWidth = 1; ctx.strokeStyle = '#e8e3d899'; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(x(motion.cursor), top); ctx.lineTo(x(motion.cursor), bottom); ctx.stroke(); }
    ctx.restore();
    if (!data?.available) { ctx.fillStyle = '#aaa99f'; ctx.fillText(audioReview.running ? 'Reading audio waveform…' : audioReview.data ? 'No audio track available' : 'Waveform not loaded', left + 10, center); }
  }
}
function resetAudioReview() {
  if (audioReview.running && audioReview.id) api(`/api/jobs/${audioReview.jobId}/motion-review/${audioReview.id}/cancel`, { method: 'POST' }).catch(() => {});
  audioReview.token++; audioReview.windowToken++; clearTimeout(audioReview.timer);
  audioReview.id = audioReview.data = null; audioReview.running = false;
  for (const kind of ['fpv', 'radio']) show(`audio-${kind}-chart`, false);
  show('motion-audio-message', false);
}
function audioReviewQuery() {
  return new URLSearchParams({ offset: $('motion-offset').value, start: motion.data.window[0], end: motion.data.window[1] });
}
function audioReviewMessage(result) {
  if (['queued', 'running'].includes(result.status)) {
    message('motion-audio-message', result.progress ? `${result.progress.stage === 'radio' ? 'StickCam' : 'FPV'} waveform: ${Number(result.progress.percent).toFixed(1)}%` : result.message);
  } else if (result.status === 'complete') {
    const sections = result.sections;
    message('motion-audio-message', `${sections ? `${sections.correlated_sections}/${sections.usable_sections} usable boundary audio sections agree at this shift. ` : ''}Waveforms show the full recording; green audio bands use boundary evidence only. Microphone levels are shown separately on an amplitude scale of −1…1.`);
  } else message('motion-audio-message', result.message, result.status === 'failed');
}
async function loadAudioReview() {
  if (!motion.id || !motion.data || audioReview.running) return;
  if (audioReview.id && audioReview.data) { await refreshAudioWindow(); return; }
  const token = ++audioReview.token, jobToken = state.jobToken, jobId = state.job.id;
  audioReview.jobId = jobId; audioReview.running = true;
  for (const kind of ['fpv', 'radio']) show(`audio-${kind}-chart`);
  show('motion-audio-message'); drawAudio(); message('motion-audio-message', 'Requesting cached audio waveforms…');
  try {
    const task = await api(`/api/jobs/${jobId}/motion-review/${motion.id}/audio`, { method: 'POST' });
    if (token !== audioReview.token || jobToken !== state.jobToken) return;
    audioReview.id = task.id;
    const poll = async () => {
      if (token !== audioReview.token || jobToken !== state.jobToken || !motion.data) return;
      try {
        const windowToken = ++audioReview.windowToken, query = audioReviewQuery();
        const result = await api(`/api/jobs/${jobId}/motion-review/${task.id}?${query}`);
        if (token !== audioReview.token || jobToken !== state.jobToken) return;
        audioReviewMessage(result);
        if (['queued', 'running'].includes(result.status)) { audioReview.timer = setTimeout(poll, 1500); return; }
        audioReview.running = false;
        if (result.status === 'complete') {
          if (windowToken === audioReview.windowToken && query.toString() === audioReviewQuery().toString()) audioReview.data = result;
          else await refreshAudioWindow();
          drawAudio();
        }
      } catch (error) { audioReview.running = false; message('motion-audio-message', error.message, true); }
    };
    await poll();
  } catch (error) { if (token === audioReview.token) { audioReview.running = false; message('motion-audio-message', error.message, true); } }
}
async function refreshAudioWindow() {
  if (!$('motion-audio-toggle').checked || !audioReview.id || audioReview.running || !motion.data) return;
  const token = audioReview.token, windowToken = ++audioReview.windowToken, jobToken = state.jobToken;
  const result = await api(`/api/jobs/${state.job.id}/motion-review/${audioReview.id}?${audioReviewQuery()}`);
  if (token !== audioReview.token || windowToken !== audioReview.windowToken || jobToken !== state.jobToken) return;
  if (result.status !== 'complete') throw new Error(result.message);
  audioReview.data = result; audioReviewMessage(result); drawAudio();
}
$('motion-highlight-toggle').onchange = drawMotion;
$('motion-scene-toggle').onchange=drawMotion;
$('motion-audio-toggle').onchange = guarded(async () => {
  const enabled = $('motion-audio-toggle').checked;
  for (const kind of ['fpv', 'radio']) show(`audio-${kind}-chart`, enabled);
  show('motion-audio-message', enabled);
  if (enabled) await loadAudioReview();
}, 'motion-audio-message');

function readMotionSettings() {
  return { visual_camera: readVisualCamera(), fpv_sampling_mode: $('fpv-preparation-mode').value, fpv_visual_fps: Number($('fpv-preparation-fps').value), stick_sampling_ratio: readStickSamplingRatio(), motion_source: $('use-fpv-gyro').checked ? ($('use-fpv-visual').checked ? 'auto' : 'gyro') : 'visual', camera_uptilt_deg: Number($('camera-uptilt-deg').value), imu_orientation: $('imu-orientation').value, gyro_video_offset: Number($('gyro-video-offset').value), decode_mode: $('decode-mode').value, tracking_backend: $('tracking-backend').value };
}
function sectionsText(value) {
  if (!value) return 'Load signals or rerun matching to inspect section agreement.';
  const notes=[];
  if (value.audio_usable_sections != null) notes.push(`Audio: ${value.audio_correlated_sections}/${value.audio_usable_sections} usable 3-second sections agree; ${value.audio_joint_sections || 0} have both landmarks and trends.`);
  if (value.motion_included === false) notes.push('Motion comparison was skipped for this pair.');
  else if (value.usable_sections !== undefined) notes.push(`Motion: ${value.correlated_sections}/${value.usable_sections} usable ${value.section_seconds || 3}-second sections correlate (${Math.round((value.section_agreement || 0)*100)}% agreement). Quiet sections and tracking gaps are excluded.`);
  if (value.review_reasons?.length) notes.push(value.review_reasons.join(' '));
  return notes.join(' ') + ' Section agreement is supporting evidence, not a match probability.';
}

function pauseMotionPreview() {
  motionPreview.playing = false; cancelAnimationFrame(motionPreview.frame);
  for (const kind of ['radio', 'fpv']) { $(`motion-preview-${kind}`).pause(); $(`motion-preview-${kind}`).playbackRate = 1; }
  $('motion-preview-play').textContent = 'Play both';
}
function resetMotionPreview() {
  pauseMotionPreview(); motionPreview.token++; motionPreview.pair = null; motionPreview.pending = false;
  for (const kind of ['radio', 'fpv']) { const video = $(`motion-preview-${kind}`); releasePreview(video); video.removeAttribute('src'); video.load(); }
  show('motion-preview-media', false); $('motion-preview-load').disabled = !state.job;
  message('motion-preview-message', 'Choose both recordings to preview them. Motion extraction is optional.');
}
function updateMotionPreviewOffset() {
  if (!motionPreview.pair) return;
  pauseMotionPreview(); const pair = motionPreview.pair, offset = Number($('motion-offset').value);
  if (!Number.isFinite(offset)) return;
  const at = $('motion-preview-radio').currentTime;
  pair.radio_start = Math.max(0, offset); pair.fpv_start = Math.max(0, -offset);
  pair.duration = Math.max(0, Math.min(pair.radio_duration - pair.radio_start, pair.fpv_duration - pair.fpv_start));
  $('motion-preview-seek').max = pair.duration; $('motion-preview-play').disabled = pair.duration <= 0;
  seekMotionPreview(at - pair.radio_start);
  message('motion-preview-message', pair.duration ? `Shared interval ${time(pair.duration)} · FPV shift ${offsetText(offset)} · lengths may differ` : 'This offset has no shared video interval.', pair.duration <= 0);
}
function seekMotionPreview(at) {
  const pair = motionPreview.pair; if (!pair || pair.duration <= 0) return;
  const value = Math.max(0, Math.min(at, Math.max(0, pair.duration - 1 / 30)));
  $('motion-preview-radio').currentTime = pair.radio_start + value; $('motion-preview-fpv').currentTime = pair.fpv_start + value;
  $('motion-preview-seek').value = value; message('motion-preview-time', `${time(value, true)} / ${time(pair.duration)}`);
  motion.cursor = pair.radio_start + value; drawMotion();
}
async function previewSelectedVideos() {
  const fpvName = $('motion-fpv').value, radioName = $('motion-radio').value;
  if (!fpvName || !radioName) throw new Error('Choose both recordings.');
  pausePair(); $('radio-stabilize-video').pause(); pauseMotionPreview();
  const token = ++motionPreview.token, chartToken = motion.token, jobToken = state.jobToken;
  const current = () => token === motionPreview.token && chartToken === motion.token && jobToken === state.jobToken;
  openPanel("motion-panel"); show("motion-charts");
  const names = { radio: radioName, fpv: fpvName };
  $('motion-preview-load').disabled = true; message('motion-preview-message', 'Loading synced videos…');
  try {
    const [r, f] = await Promise.all(['radio', 'fpv'].map(kind => api(path(`/metadata/${kind}/${part(names[kind])}`))));
    if (!current()) return;
    await Promise.all(['radio', 'fpv'].map(kind => loadVideo($(`motion-preview-${kind}`), kind, names[kind], current, 'motion-preview-message', $('playback-proxy').checked, Math.max(0, (kind === 'radio' ? 1 : -1) * Number($('motion-offset').value)))));
    if (!current()) return;
    motionPreview.pair = { radio_duration: r.duration, fpv_duration: f.duration };
    $('motion-preview-fpv-label').textContent = `FPV · ${names.fpv}`; $('motion-preview-radio-label').textContent = `StickCam · ${names.radio}`;
    show('motion-preview-media'); updateMotionPreviewOffset(); seekMotionPreview(0); applyMotionPreviewAudio();
  } finally { if (current()) $('motion-preview-load').disabled = false; }
}
$('motion-preview-load').onclick = guarded(previewSelectedVideos, 'motion-preview-message');
function applyMotionPreviewAudio() {
  for (const kind of ['radio', 'fpv']) $(`motion-preview-${kind}`).muted = $('motion-preview-audio').value !== kind;
}
$('motion-preview-audio').onchange = applyMotionPreviewAudio;
$('motion-preview-layout').onchange = () => $('motion-preview-videos').classList.toggle('stacked', $('motion-preview-layout').value === 'stacked');
$('motion-preview-seek').oninput = () => { pauseMotionPreview(); seekMotionPreview(Number($('motion-preview-seek').value)); };
for (const [id, direction] of [['motion-preview-back', -1], ['motion-preview-forward', 1]]) $(id).onclick = () => {
  pauseMotionPreview(); if (motionPreview.pair) seekMotionPreview($('motion-preview-radio').currentTime - motionPreview.pair.radio_start + direction / 30);
};
$('motion-preview-play').onclick = () => {
  if (motionPreview.playing) { pauseMotionPreview(); return; }
  const pair = motionPreview.pair; if (!pair || pair.duration <= 0) return;
  pausePair(); $('radio-stabilize-video').pause();
  if ($('motion-preview-radio').currentTime - pair.radio_start >= pair.duration - .1) seekMotionPreview(0);
  motionPreview.playing = true; $('motion-preview-play').textContent = 'Pause both'; motionPreviewTick();
};
function motionPreviewTick() {
  const pair = motionPreview.pair; if (!motionPreview.playing || !pair) return;
  const radio = $('motion-preview-radio'), fpv = $('motion-preview-fpv'), at = radio.currentTime - pair.radio_start;
  if (at >= pair.duration - 1 / 60 || radio.ended || fpv.ended) { pauseMotionPreview(); return; }
  if (radio.readyState < 3 || fpv.readyState < 3 || radio.seeking || fpv.seeking) { radio.pause(); fpv.pause(); }
  else {
    const drift = fpv.currentTime - (pair.fpv_start + at), now = performance.now();
    if (Math.abs(drift) > .14 && now - motionPreview.lastCorrection > 300) { fpv.currentTime = pair.fpv_start + at; motionPreview.lastCorrection = now; }
    else fpv.playbackRate = Math.abs(drift) > .035 ? (drift > 0 ? .97 : 1.03) : 1;
    if ((radio.paused || fpv.paused) && !motionPreview.pending) {
      const token = motionPreview.token; motionPreview.pending = true;
      Promise.all([radio.play(), fpv.play()]).catch(error => { if (token === motionPreview.token) { pauseMotionPreview(); message('motion-preview-message', error.message, true); } }).finally(() => { if (token === motionPreview.token) motionPreview.pending = false; });
    }
  }
  $('motion-preview-seek').value = Math.max(0, at); message('motion-preview-time', `${time(Math.max(0, at), true)} / ${time(pair.duration)}`);
  if (performance.now() - motionPreview.lastDraw > 100) { motion.cursor = radio.currentTime; drawMotion(); motionPreview.lastDraw = performance.now(); }
  motionPreview.frame = requestAnimationFrame(motionPreviewTick);
}
for (const kind of ['radio', 'fpv']) $(`motion-preview-${kind}`).onended = pauseMotionPreview;
window.addEventListener('pagehide', pauseMotionPreview);
$('motion-view').onchange = guarded(() => { motion.hidden.clear(); return refreshMotionWindow(); }, 'motion-message');
const stabilization = { poses: [], token: 0, frame: null, previous: null };
$('radio-stabilize-load').onclick = guarded(async () => {
  if (!motion.id || !motion.data?.radio_video) throw new Error('Load signals with a configured StickCam first.');
  const token = ++stabilization.token, chartToken = motion.token;
  $('radio-stabilize-video').pause();
  const data = await api(path(`/motion-review/${motion.id}/stabilization`));
  if (token !== stabilization.token || chartToken !== motion.token) return;
  stabilization.poses = data.poses; stabilization.previous = null;
  message('radio-stabilize-note', data.note);
  if (!data.poses.length) return;
  await loadVideo($('radio-stabilize-video'), 'radio', motion.data.radio_video, () => token === stabilization.token && chartToken === motion.token, 'radio-stabilize-note', true, Math.max(0, data.poses[0][0]));
  if (token !== stabilization.token || chartToken !== motion.token) return;
  const record = state.job.inputs.radio.find(r => r.name === motion.data.radio_video);
  stabilization.width = record?.metadata?.width; stabilization.height = record?.metadata?.height;
  if (!stabilization.width) {
    const info = await api(path(`/metadata/radio/${part(motion.data.radio_video)}`));
    if (token !== stabilization.token || chartToken !== motion.token) return;
    stabilization.width = info.width; stabilization.height = info.height;
  }
  show('radio-stabilize-media');
  $('radio-stabilize-video').currentTime = Math.max(0, data.poses[0][0]);
  drawStabilizedRadio();
}, 'radio-stabilize-note');
function radioPoseAt(at) {
  const poses = stabilization.poses; let lo = 0, hi = poses.length - 1;
  if (!poses.length || at < poses[0][0] || at > poses.at(-1)[0] + .15) return null;
  while (lo < hi) { const mid = Math.floor((lo + hi) / 2); if (poses[mid][0] < at) lo = mid + 1; else hi = mid; }
  const b = poses[lo], a = poses[Math.max(0, lo - 1)];
  if (!a[1] || !b[1] || b[0] - a[0] > .3) return null;
  const weight = b[0] === a[0] ? 0 : Math.max(0, Math.min(1, (at - a[0]) / (b[0] - a[0])));
  return { corners: a[1].map((p, i) => p.map((v, j) => v + weight * (b[1][i][j] - v))), source: b[2] };
}
function drawStabilizedRadio() {
  const video = $('radio-stabilize-video'), canvas = $('radio-stabilize-canvas');
  if (video.readyState < 2 || !video.videoWidth || !stabilization.width || !$('radio-stabilize-media').checkVisibility()) return;
  canvas.width = Math.min(960, video.videoWidth); canvas.height = Math.round(canvas.width * 9 / 16);
  const ctx = canvas.getContext('2d'); ctx.fillStyle = '#0e100e'; ctx.fillRect(0, 0, canvas.width, canvas.height);
  const pose = radioPoseAt(video.currentTime), enabled = $('radio-stabilize-enable').checked;
  if (!pose || !enabled) {
    const scale = Math.min(canvas.width / video.videoWidth, canvas.height / video.videoHeight);
    ctx.drawImage(video, (canvas.width - scale * video.videoWidth) / 2, (canvas.height - scale * video.videoHeight) / 2, scale * video.videoWidth, scale * video.videoHeight);
    message('radio-stabilize-status', enabled ? 'Outline unavailable at this time · showing the full source frame.' : 'Original framing');
    stabilization.previous = null; return;
  }
  const corners = pose.corners.map(p => [p[0] * video.videoWidth / stabilization.width, p[1] * video.videoHeight / stabilization.height]);
  const cx = corners.reduce((s, p) => s + p[0], 0) / 4, cy = corners.reduce((s, p) => s + p[1], 0) / 4;
  let angle = Math.atan2(corners[1][1] - corners[0][1], corners[1][0] - corners[0][0]);
  const prev = stabilization.previous, dt = prev ? video.currentTime - prev.time : 0;
  let center = [cx, cy];
  if (prev && dt > 0 && dt < .2) {
    const blend = 1 - Math.exp(-dt / .1), delta = Math.atan2(Math.sin(angle - prev.angle), Math.cos(angle - prev.angle));
    angle = prev.angle + blend * delta; center = prev.center.map((v, i) => v + blend * (center[i] - v));
  }
  const c = Math.cos(angle), s = Math.sin(angle);
  const rotated = corners.map(p => [(p[0] - center[0]) * c + (p[1] - center[1]) * s, -(p[0] - center[0]) * s + (p[1] - center[1]) * c]);
  const extentX = Math.max(...rotated.map(p => Math.abs(p[0]))), extentY = Math.max(...rotated.map(p => Math.abs(p[1])));
  const fill = Math.max(.4, Math.min(.9, Number($('radio-stabilize-fill').value) / 100));
  let zoom = Math.min(canvas.width * fill / (2 * Math.max(1, extentX)), canvas.height * fill / (2 * Math.max(1, extentY)), 4);
  if (prev && dt > 0 && dt < .2 && zoom > prev.zoom) zoom = prev.zoom + (1 - Math.exp(-dt / .6)) * (zoom - prev.zoom);
  stabilization.previous = { time: video.currentTime, center, angle, zoom };
  ctx.save(); ctx.translate(canvas.width / 2, canvas.height / 2); ctx.scale(zoom, zoom); ctx.rotate(-angle); ctx.translate(-center[0], -center[1]); ctx.drawImage(video, 0, 0); ctx.restore();
  const clipped = corners.some(p => p[0] < 0 || p[1] < 0 || p[0] > video.videoWidth || p[1] > video.videoHeight);
  message('radio-stabilize-status', `${pose.source === 'gyro-assisted' ? 'Gyro-assisted short gap' : 'Outline tracking'} · ${(zoom * video.videoWidth / canvas.width).toFixed(2)}× source framing${clipped ? ' · part of the radio is outside the recorded frame' : ''}`);
}
function stabilizedTick() { drawStabilizedRadio(); if (!$('radio-stabilize-video').paused) stabilization.frame = requestAnimationFrame(stabilizedTick); }
$('radio-stabilize-video').onplay = () => { pauseMotionPreview(); pausePair(); cancelAnimationFrame(stabilization.frame); stabilizedTick(); };
$('radio-stabilize-video').onloadeddata = drawStabilizedRadio;
$('radio-stabilize-video').onpause = () => cancelAnimationFrame(stabilization.frame);
$('radio-stabilize-video').onseeked = () => { stabilization.previous = null; drawStabilizedRadio(); };
$('radio-stabilize-enable').onchange = drawStabilizedRadio;
$('radio-stabilize-fill').onchange = drawStabilizedRadio;
window.addEventListener('pagehide', () => $('radio-stabilize-video').pause());

function etaText(seconds) {
  if (seconds === null || seconds === undefined) return 'Estimating time remaining…';
  if (seconds < 1) return 'Finishing motion extraction…';
  return `About ${Math.floor(seconds / 60)}m ${Math.ceil(seconds % 60)}s remaining for motion extraction`;
}
function showMotionProgress(progress, running) {
  show('motion-progress-bar', running); show('motion-progress-detail', running);
  if (!running) return;
  const bar = $('motion-progress-bar');
  if (!progress) { bar.removeAttribute('value'); message('motion-progress-detail', 'Waiting for extraction to start…'); return; }
  bar.value = progress.percent;
  const label = progress.stage === 'stickcam' ? 'StickCam stick + outline detection' : 'FPV motion extraction';
  message('motion-progress-detail', `${label}: ${Number(progress.percent).toFixed(1)}% · ${etaText(progress.eta_seconds)}`);
}
function renderExtractionProgress() {
  const list = $('extraction-progress-list'); list.replaceChildren();
  if (!activeAnalysis()) return;
  for (const clip of state.job.progress?.active_clips || []) {
    const row = el('div', undefined, 'extraction-progress-row');
    row.append(el('span', `${clip.kind === 'radio' ? 'StickCam' : 'FPV'} · ${clip.name}`));
    const bar = el('progress'); bar.max = 100; bar.value = clip.percent || 0;
    row.append(bar, el('small', clip.phase === 'audio' ? 'Motion ready · reading boundary audio' : `${Number(clip.percent || 0).toFixed(1)}% · ${etaText(clip.eta_seconds)}`));
    list.append(row);
  }
}

const stickPreparation = { key: '', refreshed: 0, token: 0, task: null, jobId: null, timer: null, running: false };
function stickRemaining(seconds, running = true) {
  if (!Number.isFinite(seconds)) return 'Estimating…';
  if (seconds < 1) return running ? 'Finishing / updating estimate…' : 'Complete';
  return `About ${time(Math.ceil(seconds))} remaining`;
}
function legacyStickProgress(progress) {
  if (!progress || progress.clip_percent !== undefined) return progress;
  const rows = progress.clips || [], finished = new Map(rows.map(row => [row.name,row]));
  const fraction = Math.max(0,Math.min(1,progress.percent * progress.total / 100 - progress.completed));
  const currentDone = finished.has(progress.name);
  const required = state.job.inputs.radio.filter(record => {
    const cal = state.job.calibrations[record.name];
    return calibrated(cal) && !cal.exclude && !cal.skip_motion;
  });
  const group = name => { const cal = state.job.calibrations[name] || {}; return `${cal.stickcam_mode || 'wonky'}:${!!cal.radio_outline}`; };
  const rates = new Map();
  for (const row of rows) {
    const record = required.find(item => item.name === row.name);
    const duration = record?.metadata?.duration;
    if (!row.reused && row.status === 'cached' && row.extraction_seconds > 0 && duration > 0)
      rates.set(group(row.name),row.extraction_seconds / (duration * (row.sample_fps || progress.sample_fps)));
  }
  if (!currentDone && progress.elapsed_seconds >= 3 && progress.processed_seconds > 0)
    rates.set(group(progress.name),progress.elapsed_seconds / (progress.processed_seconds * progress.sample_fps));
  const result = {...progress, clip_percent:currentDone ? 100 : Math.min(99, fraction * 100),
    completed:rows.length, clip_status:currentDone ? (finished.get(progress.name).reused ? 'reused' : finished.get(progress.name).status === 'failed' ? 'failed' : 'saved') : fraction >= 1 ? 'saving' : 'extracting',
    batch_elapsed_seconds:rows.reduce((sum,row)=>sum+(row.elapsed_seconds || 0),0)+(currentDone ? 0 : progress.elapsed_seconds || 0),
    estimate_basis:rates.size ? 'observed' : 'estimating', legacy:true};
  // Only infer the queue when its size agrees with the running task. Partial
  // technical runs may have different targets; never invent their remaining clips.
  if (required.length !== progress.total || required.some(row => !Number.isFinite(row.metadata?.duration))) return result;
  const queue = required.map(record => {
    const saved = finished.get(record.name), active = record.name === progress.name && !saved;
    const cache = state.stickData?.clips.find(item => item.name === record.name);
    const reused = saved ? saved.reused : !active && cache?.status === 'cached';
    const fps = saved?.sample_fps || (active ? progress.sample_fps : cache?.requested_fps) || progress.sample_fps;
    const duration = record.metadata.duration, weight = reused ? 0 : duration * fps;
    const eta = saved || reused ? 0 : active ? progress.eta_seconds : rates.has(group(record.name)) ? rates.get(group(record.name)) * weight : null;
    return {name:record.name, status:saved ? saved.status === 'failed' ? 'failed' : reused ? 'reused' : 'saved' : active ? result.clip_status : reused ? 'ready_to_reuse' : 'queued',
      percent:saved ? 100 : active ? result.clip_percent : 0, elapsed_seconds:saved?.elapsed_seconds || (active ? progress.elapsed_seconds : 0),
      eta_seconds:eta, duration_seconds:duration, sample_fps:fps, weight};
  });
  const weight = queue.reduce((sum,row)=>sum+row.weight,0);
  result.percent = weight ? Math.min(rows.length === progress.total ? 100 : 99.9,queue.reduce((sum,row)=>sum+row.weight*row.percent/100,0)/weight*100) : rows.length/progress.total*100;
  const remaining = queue.filter(row => row.percent < 100 && row.status !== 'ready_to_reuse').map(row => row.eta_seconds);
  result.batch_eta_seconds = remaining.every(Number.isFinite) ? remaining.reduce((sum,value)=>sum+value,0) : null;
  result.queue = queue;
  return result;
}
function stickTrackingStageText(data) {
  if (!data) return '';
  const seconds = key => Number.isFinite(Number(data[key])) ? Math.max(0, Number(data[key])) : 0;
  const count = key => Number.isFinite(Number(data[key])) ? Math.max(0, Math.trunc(Number(data[key]))) : 0;
  const showTime = value => `${value.toFixed(1)}s`;
  const stages = [];
  if (data.tracking_decoded_frames !== undefined)
    stages.push(`decode ${showTime(seconds('tracking_decode_seconds'))} · ${count('tracking_decoded_frames').toLocaleString()} frames`);
  if (data.tracking_shell_seconds !== undefined) {
    const roi = count('tracking_outline_roi_searches'), full = count('tracking_outline_global_searches');
    const roiFound = count('tracking_outline_roi_successes'), fullFound = count('tracking_outline_global_successes');
    const searches = roi + full ? ` · SIFT ${showTime(seconds('tracking_outline_sift_seconds'))} / matches ${showTime(seconds('tracking_outline_match_seconds'))} / checks ${showTime(seconds('tracking_outline_validation_seconds'))}; local ${roiFound}/${roi}, full ${fullFound}/${full}` : '';
    stages.push(`radio ${showTime(seconds('tracking_shell_seconds'))}${searches}`);
  }
  if (data.tracking_stick_seconds !== undefined) stages.push(`gimbals ${showTime(seconds('tracking_stick_seconds'))}`);
  if (data.tracking_flow_calls !== undefined || data.tracking_warp_calls !== undefined || data.tracking_template_calls !== undefined) {
    const ops = [];
    if (count('tracking_flow_calls')) ops.push(`flow ${showTime(seconds('tracking_flow_seconds'))}`);
    if (count('tracking_warp_calls')) ops.push(`crops ${showTime(seconds('tracking_warp_seconds'))}`);
    if (count('tracking_template_calls')) ops.push(`tips ${showTime(seconds('tracking_template_seconds'))}`);
    if (ops.length) stages.push(`ops: ${ops.join(', ')}`);
  }
  if (count('tracking_gpu_operations')) {
    const gpu = ['tracking_cuda_flow_seconds', 'tracking_cuda_warp_seconds', 'tracking_cuda_template_seconds']
      .reduce((sum, key) => sum + seconds(key), 0);
    stages.push(`CUDA ${showTime(gpu)} · ${count('tracking_gpu_operations').toLocaleString()} ops / ${count('tracking_gpu_uploads').toLocaleString()} uploads`);
  }
  const cpu = ['tracking_cpu_flow_seconds', 'tracking_cpu_warp_seconds', 'tracking_cpu_template_seconds']
    .reduce((sum, key) => sum + seconds(key), 0);
  if (cpu > 0) stages.push(`CPU flow/crop/tip ops ${showTime(cpu)}`);
  return stages.length ? `Stage time so far · ${stages.join(' · ')}` : '';
}
function renderStickProgress(progress, status) {
  progress = legacyStickProgress(progress);
  liveStickPreview = progress?.live_inputs || null;
  show('stick-live-preview',!!liveStickPreview || ['queued','running'].includes(status));
  show('stick-live-canvas',!!liveStickPreview);
  if (liveStickPreview) { message('stick-live-detail',`${progress.name} · Mode ${liveStickPreview.stick_mode} · latest ${liveStickPreview.window_seconds}s of processed observations`); drawLiveStickInputs(); }
  else if (['queued','running'].includes(status)) message('stick-live-detail','This worker started before live chart output was added. The next derivation will show its input observations here; this run continues without interruption.');
  show('stick-progress-summary');
  const running = ['queued', 'running'].includes(status);
  if (!progress) {
    if (!running) { show('stick-progress-summary',false); return; }
    for (const id of ['stick-data-progress','stick-clip-progress']) $(id).removeAttribute('value');
    message('stick-batch-percent',''); message('stick-clip-percent',''); message('stick-current-name','Waiting for processing to start');
    message('stick-tracking-stages','');
    message('stick-batch-detail','Queued · the timer starts when processing begins.');
    message('stick-batch-eta','Estimating after processing starts…'); message('stick-clip-detail',''); message('stick-clip-eta','');
    $('stick-progress-rows').replaceChildren(); return;
  }
  const percent = Math.max(0, Math.min(100, Number(progress.percent) || 0));
  const clipPercent = Math.max(0, Math.min(100, Number(progress.clip_percent ?? (percent * progress.total - progress.completed * 100)) || 0));
  const age = running && progress.updated_at ? Math.max(0, Date.now() / 1000 - progress.updated_at) : 0;
  const remaining = value => Number.isFinite(value) ? Math.max(0,value - age) : null;
  const batchEta = remaining(progress.batch_eta_seconds), clipEta = remaining(progress.eta_seconds);
  $('stick-data-progress').value = percent; $('stick-clip-progress').value = clipPercent;
  message('stick-batch-percent',`${percent.toFixed(1)}%`); message('stick-clip-percent',`${clipPercent.toFixed(1)}%`);
  message('stick-current-name',progress.name);
  message('stick-tracking-backend',progress.clip_status === 'reusing' ? 'Reusing saved movement data · no tracking required' : progress.tracking_label || 'Waiting for tracking backend');
  message('stick-tracking-stages',stickTrackingStageText(progress));
  const failed = (progress.clips || []).filter(row => row.status === 'failed').length;
  const reused = (progress.clips || []).filter(row => row.reused).length;
  message('stick-batch-detail',`${progress.completed}/${progress.total} videos finished · ${reused} reused${failed ? ` · ${failed} failed` : ''}${Number.isFinite(progress.batch_elapsed_seconds) ? ` · ${time(progress.batch_elapsed_seconds + age)} elapsed` : ''}`);
  const finish = running && batchEta >= 1 ? ` · estimated finish ${new Date(Date.now() + batchEta * 1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})}` : '';
  message('stick-batch-eta',`${running ? stickRemaining(batchEta) : status === 'complete' ? 'Batch finished' : `Batch ${status}`}${finish}`);
  const phases = {extracting:'Deriving inputs', saving:'Saving movement data', reusing:'Loading saved data', ready_to_reuse:'Ready to reuse', queued:'Queued', reused:'Reused saved data', saved:'Derived & saved', failed:'Failed'};
  const phase = phases[progress.clip_status] || 'Deriving inputs';
  message('stick-clip-detail',`${phase} · ${time((progress.elapsed_seconds || 0) + (clipPercent < 100 ? age : 0))} elapsed · ${rateText(progress.sample_fps)} Hz${Number.isFinite(progress.processed_seconds) && Number.isFinite(progress.duration_seconds) && clipPercent < 100 ? ` · ${time(progress.processed_seconds)} of ${time(progress.duration_seconds)} footage scanned` : ''}`);
  message('stick-clip-eta',clipPercent >= 100 ? phase : stickRemaining(clipEta,running));
  message('stick-estimate-note',`${progress.estimate_basis === 'history' ? 'Initial estimates use previous successful derivations in this session.' : progress.estimate_basis === 'estimating' ? 'Time estimates appear after enough footage has been processed.' : 'Estimates use observed processing speed and update during derivation.'} ${progress.legacy && !progress.queue ? 'This older task has a clip-count batch percentage; a total estimate needs the remaining clip metadata.' : 'Batch progress is weighted by clip duration and tracking rate; cached videos require no new tracking.'} Decode, SMB speed, backwards tracking and saving can change the estimate.`);
  const list = $('stick-progress-rows'); list.replaceChildren();
  for (const row of progress.queue || []) {
    const tr = el('tr'), cell = el('td'), bar = el('progress'); bar.max = 100; bar.value = row.percent;
    bar.setAttribute('aria-label',`${row.name} progress`); cell.append(bar,el('small',`${Number(row.percent).toFixed(1)}%`));
    tr.append(el('td',`${row.name} · ${time(row.duration_seconds)} · ${rateText(row.sample_fps)} Hz`), el('td',phases[row.status] || row.status), cell,
      el('td',row.status === 'failed' ? 'Failed' : row.percent >= 100 ? `Finished in ${time(row.elapsed_seconds)}` : row.status === 'ready_to_reuse' ? 'Cache lookup only' : row.status === 'queued' && Number.isFinite(row.eta_seconds) ? `About ${time(Math.ceil(row.eta_seconds))} to process` : stickRemaining(row.status === 'extracting' || row.status === 'saving' ? remaining(row.eta_seconds) : row.eta_seconds,running)));
    list.append(tr);
  }
}
function readStickSamplingRatio() { return $('stick-sampling-ratio').value === '' ? null : Number($('stick-sampling-ratio').value); }
function rateText(value) { return Number(value).toLocaleString(undefined, {maximumFractionDigits:3}); }
function updateStickSamplingPreview() {
  const ratio = readStickSamplingRatio(), record = state.job?.inputs.radio.find(r => r.name === $('calibration-clip').value);
  const source = record?.metadata?.fps;
  const requested = ratio === null ? Number($('sample-fps').value) : Number.isFinite(source) && source > 0 ? source * ratio : null;
  message('stick-sampling-preview', `${Number.isFinite(source) ? `Selected clip: ${rateText(source)} fps → ` : ''}${requested === null ? 'Waiting for source frame rate' : `${rateText(requested)} Hz tracking${requested > 60 ? ' · exceeds 60 Hz: choose a smaller ratio' : ''}`}`);
}
function stickStatusLabel(status) {
  return { cached: 'Derived · saved & reusable', other_rate: 'Saved at another rate · needs re-extraction', not_derived: 'Needs derivation for current settings',
    needs_calibration: 'Needs calibration', excluded: 'Excluded', audio_only: 'Audio only · no stick data', unavailable: 'Source or cache unavailable' }[status] || status;
}
async function refreshStickData(force = false) {
  if (!state.job || Object.values(state.job.inputs).flat().some(r => r.uploaded !== r.size)) return;
  const jobId = state.job.id, jobToken = state.jobToken, fps = Number($('sample-fps').value), ratio = readStickSamplingRatio();
  const key = JSON.stringify([jobId, fps, ratio, state.job.calibrations]);
  if (!force && key === stickPreparation.key && Date.now() - stickPreparation.refreshed < 3000) return;
  stickPreparation.key = key; stickPreparation.refreshed = Date.now();
  const result = await api(`/api/jobs/${jobId}/stick-data?${new URLSearchParams({ sample_fps: fps, stick_sampling_ratio: ratio ?? '' })}`);
  if (jobToken !== state.jobToken || key !== stickPreparation.key) return;
  state.stickData = result;
  for (const clip of result.clips) {
    const record = state.job.inputs.radio.find(item => item.name === clip.name);
    if (record && clip.metadata) record.metadata = clip.metadata;
    if (record) record.modified_time = clip.modified_time;
  }
  const sampling = ratio === null ? `${rateText(fps)} Hz` : `${ratio} × source fps`;
  $('stick-data-count').textContent = `${result.cached} / ${result.clips.length} at ${sampling}${result.saved_other_rate ? ` · ${result.saved_other_rate} saved at another rate` : ''}`;
  updateStickSamplingPreview();
  const list = $('stick-data-rows'); list.replaceChildren();
  for (const clip of result.clips) {
    const row = el('tr'), status = el('td', clip.sampling_error || stickStatusLabel(clip.status));
    if (clip.status === 'cached') { status.className = 'cache-ready'; if (clip.usable_percent !== undefined) status.append(el('small',` · ${clip.usable_percent}% jointly observed${clip.quality_warning ? ' · inspect tracking' : ''}`)); }
    row.append(el('td', clip.name), status,
      el('td', `${Number.isFinite(clip.source_fps) ? `${rateText(clip.source_fps)} fps → ` : ''}${Number.isFinite(clip.requested_fps) ? `${rateText(clip.requested_fps)} Hz requested` : 'Rate unavailable'}${clip.samples !== undefined ? ` · ${clip.samples.toLocaleString()} saved samples at ${rateText(clip.sample_fps)} Hz` : ''}${clip.range ? ` · ${time(clip.range[0], true)}–${time(clip.range[1], true)}` : ''}`),
      el('td', derivationTimeLabel(clip.last_derivation)),
      el('td', clip.cached_at ? new Date(clip.cached_at * 1000).toLocaleString() : '—'));
    list.append(row);
  }
  renderVideoLists(); renderWorkflow();
  const active = result.tasks.find(task => ['queued', 'running'].includes(task.status));
  if (active && (!stickPreparation.running || stickPreparation.jobId !== jobId || stickPreparation.task !== active.id)) watchStickPreparation(active.id, jobId, jobToken);
  if (!active && !stickPreparation.running) {
    message('stick-data-message', `${result.cached} recording(s) ready to reuse · ${result.clips.filter(c => ['not_derived','other_rate'].includes(c.status)).length} calibrated recording(s) need derivation. Status uses ${sampling}. Other saved rates remain available when you switch back.`);
    const logs = $('stick-data-log'); logs.replaceChildren();
    for (const task of result.tasks.slice(-10).reverse()) {
      const row = el('div'), button = el('button', `${task.status} · ${task.message}`, 'secondary');
      button.onclick = guarded(async () => { const details = await api(`/api/jobs/${jobId}/motion-review/${task.id}`); renderStickPreparationLog(details); }, 'stick-data-message');
      row.append(button); logs.append(row);
    }
  }
  $('derive-sticks').disabled = activeAnalysis() || !!active || stickPreparation.running;
  $('stick-sampling-ratio').disabled = activeAnalysis() || !!active || stickPreparation.running;
  $('tracking-backend').disabled = activeAnalysis() || !!active || stickPreparation.running || fpvPreparation.running;
}
// Calibration and timed corrections share one source-coordinate canvas and player.
function stickWorkspaceTime() {
  const video = $('cal-video');
  return !video.classList.contains('hidden') && Number.isFinite(video.currentTime)
    ? video.currentTime : Number(state.calibration?.reference_time || 0);
}
function updateStickWorkspace() {
  const watching = !$('cal-video').classList.contains('hidden'), correcting = stickReview.framePurpose === 'correction';
  $('stick-workspace-mode').textContent = watching ? 'Reviewing saved tracking' : correcting ? `Correcting frame · ${time(stickWorkspaceTime(), true)}` : 'Initial calibration';
  show('save-calibration', !correcting && !watching);
  show('stick-review-calibration-landmark', correcting && !watching);
  show('stick-review-save', !!stickReview.data);
  show('stick-edit-initial', correcting || watching);
  show('cal-pause', watching || (!correcting && calibrated(state.job?.calibrations[state.calibrationName])));
  $('cal-pause').textContent = watching ? 'Pause & correct this frame' : 'Correct this frame';
  show('stick-review-unsaved', stickReview.dirty || stickReview.frameEdits);
  $('stick-review-unsaved').textContent = stickReview.frameEdits ? 'Frame correction draft · add or save it' : 'Corrections not saved';
  $('stick-mark-hint').textContent = correcting
    ? 'Mark either tip to add a timed tip correction. For lost radio/gimbal tracking, mark both housings, neutral centres, tips and shell corners, then Add frame correction. Tip marks alone cannot restore a lost radio pose.'
    : watching ? 'Choose any marking tool, or Pause & correct this frame, to edit this same preview. Blue is physical left; red is physical right.'
    : 'Mark both housings, neutral centres and tips. Optional: mark shell corners clockwise from top left. Blue is physical left; red is physical right.';
  show('stick-review-overlay', !!stickReview.data && (watching || correcting));
  if (correcting) {
    show('calibration-dirty', false); show('discard-calibration-draft', stickReview.frameEdits);
    $('discard-calibration-draft').textContent = 'Discard frame draft';
  } else $('discard-calibration-draft').textContent = 'Discard draft';
  if (document.activeElement !== $('stick-playhead')) $('stick-playhead').value = stickWorkspaceTime();
  $('stick-playhead').max = Math.max(0, (state.metadata?.duration || 0) - 1 / (state.metadata?.fps || 30));
  $('stick-playhead').disabled = !state.metadata || !state.calibrationFrameReady || stickReview.frameEdits || activeAnalysis() || !!stickReview.saving;
}
function stickTrackingAt(at) {
  const window = stickReview.trackingWindow;
  const movement = window && at >= window.start && at < window.end ? window : stickReview.data?.movement;
  return {movement, sample: stickReviewNearestSample(at, movement)};
}
function seedStickCorrection(at) {
  const point = stickReviewPointAt(at, .005), config = state.calibration;
  if (point?.calibration) { Object.assign(config, clone(point.calibration)); return; }
  // Do not copy the starting pose onto a later frame. Only seed visible saved poses.
  delete config.radio_outline;
  for (const side of ['left_gimbal', 'right_gimbal']) delete config[side];
  const {movement, sample} = stickTrackingAt(at), i = sample?.index;
  if (!sample || movement.stale || movement.tracking?.source !== 'stored') return;
  if (movement.radio_outline?.[i] && movement.outline_quality?.[i] >= .35) config.radio_outline = clone(movement.radio_outline[i]);
  for (const side of ['left', 'right']) {
    const corners = movement.tracking.gimbals?.[side]?.[i];
    if (!corners || movement.tracking.gimbal_quality?.[side]?.[i] < .35) continue;
    const xs = corners.map(p => p[0]), ys = corners.map(p => p[1]);
    const x = Math.max(0, Math.floor(Math.min(...xs))), y = Math.max(0, Math.floor(Math.min(...ys)));
    const width = Math.min(state.metadata.width - x, Math.ceil(Math.max(...xs)) - x);
    const height = Math.min(state.metadata.height - y, Math.ceil(Math.max(...ys)) - y);
    if (Math.min(width, height) < 8) continue;
    const spec = {x,y,width,height,center_x:width/2,center_y:height/2,travel_x:width*.4,travel_y:height*.4};
    const tip = movement.tracking.tips?.[side]?.[i];
    if (tip && movement.tracking.tip_quality?.[side]?.[i] > .1 && tip[0] >= x && tip[0] < x+width && tip[1] >= y && tip[1] < y+height) {
      spec.tip_x = tip[0]-x; spec.tip_y = tip[1]-y;
    }
    config[side + '_gimbal'] = spec;
  }
}
async function editStickPlaybackFrame() {
  if (!state.metadata) return false;
  const token = state.calToken, jobToken = state.jobToken;
  if (stickReview.loading) throw new Error('Wait for saved tracking to finish loading.');
  const at = stickWorkspaceTime();
  $('cal-video').pause(); rememberCalibrationDraft();
  stickReview.framePurpose = calibrated(state.job.calibrations[state.calibrationName]) ? 'correction' : 'initial';
  $('reference-time').value = at.toFixed(3);
  await ensureStickTrackingWindow(at);
  if (token !== state.calToken || jobToken !== state.jobToken) return false;
  await showFrame(true,token);
  if (token !== state.calToken || jobToken !== state.jobToken) return false;
  state.tool = null; document.querySelectorAll('[data-tool]').forEach(button => button.classList.remove('active'));
  updateStickWorkspace();
  message('calibration-message', stickReview.framePurpose === 'correction'
    ? `Correcting ${time(at, true)}. Visible saved poses are editable starting suggestions; check the boxes and neutral centres. Missing poses stay unmarked. The initial calibration is kept separately.`
    : 'Mark the initial calibration here, then confirm it before adding timed corrections.');
  return true;
}
async function seekStickWorkspace(at) {
  if (!state.metadata) return;
  const token = state.calToken;
  if (stickReview.saving) await stickReview.saving;
  if (token !== state.calToken) return;
  if (stickReview.frameEdits) throw new Error('Add or discard the current frame correction before seeking.');
  at = Math.max(0, Math.min(Number(at) || 0, state.metadata.duration - 1 / (state.metadata.fps || 30)));
  const video = $('cal-video');
  if (!video.classList.contains('hidden') && video.readyState) { video.pause(); video.currentTime = at; }
  else {
    if (calibrated(state.job.calibrations[state.calibrationName])) { rememberCalibrationDraft(); stickReview.framePurpose = 'correction'; }
    $('reference-time').value = at.toFixed(3); await showFrame();
  }
  updateStickWorkspace(); stickReviewPositionChanged();
}
function addStickTipCorrection(side, position) {
  if (!stickReview.data || stickReview.name !== state.calibrationName) throw new Error('Wait for this clip’s saved tracking to load before adding corrections.');
  const at = Number(stickWorkspaceTime().toFixed(4)), key = side.startsWith('left') ? 'left_tip' : 'right_tip';
  const point = stickReviewPointAt(at, .005) || {time:at};
  point[key] = [position.x / state.metadata.width, position.y / state.metadata.height];
  // Keep an existing full-frame landmark and its tip consistent.
  const spec = state.calibration[side];
  if (spec && position.x >= spec.x && position.x < spec.x+spec.width && position.y >= spec.y && position.y < spec.y+spec.height) {
    spec.tip_x = position.x-spec.x; spec.tip_y = position.y-spec.y;
    if (point.calibration?.[side]) Object.assign(point.calibration[side], {tip_x:spec.tip_x,tip_y:spec.tip_y});
  } else if (spec) {
    delete spec.tip_x; delete spec.tip_y;
  }
  if (!stickReview.data.annotations.includes(point)) stickReview.data.annotations.push(point);
  stickReview.data.annotations.sort((a,b) => a.time-b.time);
  setStickReviewDirty(true); renderStickReviewRows(); stickReviewPositionChanged();
}
function addStickFrameCorrection() {
  if (stickReview.framePurpose !== 'correction' || !stickReview.data || stickReview.name !== state.calibrationName) throw new Error('Pause the clip at the frame you want to correct first.');
  if (!calibrated(state.job.calibrations[stickReview.name])) throw new Error('Confirm an initial calibration before adding frame corrections.');
  const config = currentCalibrationConfig(), at = Number(config.reference_time.toFixed(4));
  const point = stickReviewPointAt(at, .005) || {time:at};
  point.calibration = Object.fromEntries(['left_gimbal','right_gimbal','radio_outline','radio_rotation_deg','stickcam_mode'].filter(key => config[key] !== undefined).map(key => [key,clone(config[key])]));
  // Full-frame tips supersede any earlier tip-only marks at the same time.
  delete point.left_tip; delete point.right_tip;
  if (!stickReview.data.annotations.includes(point)) stickReview.data.annotations.push(point);
  stickReview.data.annotations.sort((a,b) => a.time-b.time);
  stickReview.frameEdits = false; setStickReviewDirty(true); renderStickReviewRows(); stickReviewPositionChanged();
  message('stick-review-message', `Added a complete frame correction at ${time(at,true)}. Save corrections or re-derive this clip to apply it. The starting calibration is unchanged.`);
}

function stickReviewPointAt(at, tolerance = .2) {
  const rows = stickReview.data?.annotations || [];
  let best = null, delta = tolerance;
  for (const row of rows) {
    const distance = Math.abs(row.time - at);
    if (distance <= delta) { best = row; delta = distance; }
  }
  return best;
}
function stickReviewSetFields(point) {
  for (const axis of ['throttle', 'yaw', 'pitch', 'roll']) {
    const input = $(`stick-review-${axis}`);
    if (document.activeElement !== input) input.value = point?.controls?.[axis] ?? '';
  }
  if (document.activeElement !== $('stick-review-note')) $('stick-review-note').value = point?.note || '';
}
function stickReviewNearestSample(at, movement = stickReview.data?.movement) {
  if (!movement?.time?.length) return null;
  const times = movement.time; let low = 0, high = times.length - 1;
  while (low < high) { const mid = (low + high) >> 1; if (times[mid] < at) low = mid + 1; else high = mid; }
  if (low > 0 && Math.abs(times[low - 1] - at) < Math.abs(times[low] - at)) low--;
  if (Math.abs(times[low] - at) > Math.max(.1, 1 / movement.sample_fps)) return null;
  return { index: low, time: times[low], quality: movement.quality?.[low] ?? 0,
    axes: Object.fromEntries(['throttle', 'yaw', 'pitch', 'roll'].map(axis => [axis, movement.axes?.[axis]?.[low] ?? null])) };
}
async function ensureStickTrackingWindow(at) {
  if (!stickReview.data?.movement || stickReview.data.movement.stale || stickReview.windowPending || Date.now() < stickReview.windowRetry) return;
  const current = stickReview.trackingWindow;
  if (current && at >= current.start && (at < current.end - 2 || current.end >= stickReview.data.duration - .01)) return;
  const token = ++stickReview.windowToken, reviewToken = stickReview.token, jobToken = state.jobToken;
  stickReview.windowPending = true;
  try {
    const start = Math.max(0, at - 2), result = await api(`/api/jobs/${stickReview.jobId}/stick-review/${part(stickReview.name)}/tracking?${new URLSearchParams({start,duration:20})}`);
    if (token !== stickReview.windowToken || reviewToken !== stickReview.token || jobToken !== state.jobToken) return;
    stickReview.trackingWindow = result; stickReview.windowRetry = 0;
    drawStickReviewOverlay();
  } catch { if (token === stickReview.windowToken) stickReview.windowRetry = Date.now() + 5000; }
  finally { if (token === stickReview.windowToken) stickReview.windowPending = false; }
}
function stickReviewPositionChanged() {
  if (!stickReview.data) return;
  const video = $('cal-video'), at = stickWorkspaceTime();
  if (!video.classList.contains('hidden') && document.activeElement !== $('reference-time')) $('reference-time').value = at.toFixed(3);
  const sample = stickReviewNearestSample(at), point = stickReviewPointAt(at);
  const outlineQuality = sample && stickReview.data.movement?.outline_quality?.[sample.index];
  message('stick-review-auto-values', sample ? `Saved automatic output at ${time(sample.time, true)} · quality ${Number(sample.quality).toFixed(2)} · ` +
    ['throttle', 'yaw', 'pitch', 'roll'].map(axis => `${axis} ${sample.axes[axis] === null ? '—' : sample.axes[axis].toFixed(3)}`).join(' · ') +
    (outlineQuality === undefined ? '' : ` · radio outline ${Number(outlineQuality).toFixed(2)}`) : 'No saved automatic movement trace is available at this tracking rate. You can still label expected controls and stick-tip positions against the video.');
  stickReviewSetFields(point);
  updateStickWorkspace(); drawStickReviewOverlay(); drawStickReviewChart();
}
function stickReviewVideoRect(width, height, videoWidth, videoHeight) {
  const scale = Math.min(width / videoWidth, height / videoHeight);
  const drawnWidth = videoWidth * scale, drawnHeight = videoHeight * scale;
  return { x: (width - drawnWidth) / 2, y: (height - drawnHeight) / 2, width: drawnWidth, height: drawnHeight };
}
function drawStickReviewOverlay() {
  const canvas = $('stick-review-overlay'), video = $('cal-video');
  if (!canvas || !stickReview.data || !state.metadata) return;
  const at = stickWorkspaceTime(), videoWidth = state.metadata.width, videoHeight = state.metadata.height;
  const rect = canvas.getBoundingClientRect(); if (!rect.width || !rect.height) return;
  const dpr = window.devicePixelRatio || 1, width = Math.round(rect.width * dpr), height = Math.round(rect.height * dpr);
  if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
  const ctx = canvas.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, rect.width, rect.height);
  const rotated = calibrationViewRotated();
  if (rotated) { ctx.translate(rect.width, rect.height); ctx.scale(-1, -1); }
  const point = stickReviewPointAt(at, .18), box = stickReviewVideoRect(rect.width, rect.height, videoWidth, videoHeight);
  ensureStickTrackingWindow(at);
  const overlayWindow = stickReview.trackingWindow;
  const movement = overlayWindow && at >= overlayWindow.start && at < overlayWindow.end ? overlayWindow : stickReview.data?.movement;
  const sample = stickReviewNearestSample(at, movement);
  const corners = movement?.radio_outline?.[sample?.index], shellQuality = movement?.outline_quality?.[sample?.index] ?? 0;
  const polygon = (points, color, dashed = false) => {
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.setLineDash(dashed ? [6, 4] : []); ctx.beginPath();
    points.forEach((p, index) => { const x = box.x + (p[0] / stickReview.data.width) * box.width, y = box.y + (p[1] / stickReview.data.height) * box.height; if (index) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
    ctx.closePath(); ctx.stroke(); ctx.setLineDash([]);
  };
  const showAutomatic = $('stick-review-show-overlay').checked;
  if (showAutomatic && corners && shellQuality >= .35) {
    ctx.strokeStyle = '#72cf6b'; ctx.fillStyle = '#72cf6b'; ctx.lineWidth = 2.5; ctx.beginPath();
    corners.forEach((p, index) => { const x = box.x + (p[0] / stickReview.data.width) * box.width, y = box.y + (p[1] / stickReview.data.height) * box.height; if (index) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
    ctx.closePath(); ctx.stroke(); ctx.font = 'bold 12px system-ui'; if (!rotated) ctx.fillText(`AUTO RADIO OUTLINE · ${Number(shellQuality).toFixed(2)}`, box.x + 8, box.y + 18);
  }
  const tracking = movement?.tracking, status = [];
  for (const [side, color] of [['left', '#49a7f2'], ['right', '#f06f62']]) {
    const gimbal = tracking?.gimbals?.[side]?.[sample?.index], poseQuality = tracking?.gimbal_quality?.[side]?.[sample?.index] ?? 0;
    const tip = tracking?.tips?.[side]?.[sample?.index], tipQuality = tracking?.tip_quality?.[side]?.[sample?.index] ?? 0;
    if (showAutomatic && gimbal && poseQuality >= .35) polygon(gimbal, color);
    if (showAutomatic && tip && tipQuality > .1) {
      const x = box.x + tip[0] / stickReview.data.width * box.width, y = box.y + tip[1] / stickReview.data.height * box.height;
      ctx.fillStyle = color; ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(x, y, 5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    }
    const reason = tracking?.gap_reasons?.[tracking?.tip_gap_reason?.[side]?.[sample?.index]] || 'No tracked position';
    status.push(`${side}: ${tipQuality > .1 ? `tip ${(tipQuality * 100).toFixed(0)}% tracking quality` : reason}`);
  }
  message('stick-review-tracking-status', !sample ? 'No saved tracking sample at this time.' : !tracking || tracking.source === 'unavailable' ? 'This older trace has no moving gimbal/tip poses. Re-derive this clip once to save its complete tracking overlay.' : `${stickReview.data.movement?.stale ? 'Previous trace · save and re-derive to update. ' : ''}${status.join(' · ')}${tracking.source !== 'stored' ? ' · positions reconstructed from saved controls and calibrated geometry' : ''}`);
  if (point?.calibration) {
    const calibration = point.calibration;
    if (calibration.radio_outline) polygon(calibration.radio_outline, '#ffcc72', true);
    for (const side of ['left', 'right']) {
      const spec = calibration[side + '_gimbal'];
      polygon([[spec.x, spec.y], [spec.x + spec.width, spec.y], [spec.x + spec.width, spec.y + spec.height], [spec.x, spec.y + spec.height]], '#ffcc72', true);
    }
  }
  if (!point) return;
  for (const [key, color, label] of [['left_tip', '#49a7f2', 'L'], ['right_tip', '#f06f62', 'R']]) {
    const spec = point.calibration?.[key === 'left_tip' ? 'left_gimbal' : 'right_gimbal'];
    const marker = point[key] || (spec ? [(spec.x + spec.tip_x) / stickReview.data.width, (spec.y + spec.tip_y) / stickReview.data.height] : null); if (!marker) continue;
    const x = box.x + marker[0] * box.width, y = box.y + marker[1] * box.height;
    ctx.strokeStyle = '#ffcc72'; ctx.fillStyle = color; ctx.lineWidth = 3; ctx.beginPath(); ctx.arc(x, y, 9, 0, Math.PI * 2); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x - 12, y); ctx.lineTo(x + 12, y); ctx.moveTo(x, y - 12); ctx.lineTo(x, y + 12); ctx.stroke();
    ctx.font = 'bold 13px system-ui'; if (!rotated) ctx.fillText(label, x + 13, y - 9);
  }
}
function drawStickReviewChart() {
  const canvas = $('stick-review-chart'), data = stickReview.data;
  if (!canvas || !data) return;
  const rect = canvas.getBoundingClientRect(); if (!rect.width) return;
  const dpr = window.devicePixelRatio || 1, width = Math.round(rect.width * dpr), height = Math.round(rect.height * dpr);
  if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
  const ctx = canvas.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, rect.width, rect.height);
  ctx.fillStyle = '#111311'; ctx.fillRect(0, 0, rect.width, rect.height);
  const left = 62, right = 12, top = 12, bottom = 28, plotWidth = Math.max(1, rect.width - left - right), plotHeight = Math.max(1, rect.height - top - bottom), band = plotHeight / 4;
  const duration = Math.max(.01, data.duration || 0), movement = data.movement, colors = { throttle: '#dda354', yaw: '#49a7f2', pitch: '#72cf6b', roll: '#f06f62' };
  ctx.font = '11px system-ui'; ctx.textBaseline = 'middle';
  for (let index = 0; index < 4; index++) {
    const axis = ['throttle', 'yaw', 'pitch', 'roll'][index], y0 = top + index * band, mid = y0 + band / 2;
    ctx.strokeStyle = '#ffffff18'; ctx.lineWidth = 1;
    for (const level of [-1, 0, 1]) { const y = mid - level * band * .4; ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(rect.width - right, y); ctx.stroke(); }
    ctx.fillStyle = colors[axis]; ctx.fillText(axis[0].toUpperCase() + axis.slice(1), 8, mid);
    if (movement?.time?.length) {
      const values = movement.axes?.[axis] || [], quality = movement.axis_quality?.[axis] || movement.quality || [];
      ctx.strokeStyle = colors[axis]; ctx.lineWidth = 1.5; ctx.beginPath(); let drawing = false;
      for (let i = 0; i < Math.min(movement.time.length, values.length); i++) {
        if (!Number.isFinite(values[i]) || (quality[i] ?? 1) <= .1) { drawing = false; continue; }
        const x = left + Math.max(0, Math.min(1, movement.time[i] / duration)) * plotWidth;
        const y = mid - Math.max(-1, Math.min(1, values[i])) * band * .4;
        if (!drawing) ctx.moveTo(x, y); else ctx.lineTo(x, y); drawing = true;
      }
      ctx.stroke();
    }
    for (const reference of data.annotations || []) {
      if (reference.controls?.[axis] === undefined) continue;
      const x = left + Math.max(0, Math.min(1, reference.time / duration)) * plotWidth;
      const y = mid - Math.max(-1, Math.min(1, reference.controls[axis])) * band * .4;
      ctx.fillStyle = '#fff'; ctx.strokeStyle = colors[axis]; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(x, y, 4.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    }
  }
  ctx.fillStyle = '#aaa99f'; ctx.textBaseline = 'top';
  for (let tick = 0; tick <= 4; tick++) { const seconds = duration * tick / 4, x = left + plotWidth * tick / 4; ctx.fillText(time(seconds), x - 14, rect.height - bottom + 7); }
  const at = stickWorkspaceTime(), x = left + Math.max(0, Math.min(1, at / duration)) * plotWidth;
  ctx.strokeStyle = '#fff9'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + plotHeight); ctx.stroke();
  $('stick-review-chart-summary').textContent = movement ? `${movement.stale ? 'Previous trace · ' : ''}${movement.source_samples.toLocaleString()} original samples · plotted at ${rateText(movement.sample_fps)} Hz` : 'No saved derivation for current settings';
}
function renderStickReviewRows() {
  if (!stickReview.data) return;
  const body = $('stick-review-rows'); body.replaceChildren();
  for (const point of [...(stickReview.data.annotations || [])].sort((a, b) => a.time - b.time)) {
    const tr = el('tr'), active = stickReview.data.calibration?.tracking_landmarks?.find(row => Math.abs(row.time - point.time) < .0001);
    const event = !stickReview.data.movement?.stale && stickReview.data.movement?.landmark_events?.find(row => Math.abs(row.time - point.time) < .0001);
    const labels = [];
    if (point.calibration) labels.push(`full frame calibration · ${active?.calibration ? event ? 'applied' : 'awaiting derivation' : 'unsaved / inactive'}`);
    for (const key of ['left_tip', 'right_tip']) if (point[key]) {
      const enabled = JSON.stringify(active?.[key]) === JSON.stringify(point[key]);
      labels.push(`${key === 'left_tip' ? 'left' : 'right'} tip · ${enabled ? event?.[key] || 'awaiting derivation' : 'unsaved / inactive'}`);
    }
    const landmarks = labels.join(', ') || '—';
    const controls = point.controls ? ['throttle', 'yaw', 'pitch', 'roll'].filter(axis => point.controls[axis] !== undefined).map(axis => `${axis} ${point.controls[axis].toFixed(2)}`).join(' · ') : '—';
    const inspect = el('td'), jump = action('Review', guarded(() => seekStickWorkspace(point.time), 'stick-review-message'));
    const remove = action('Delete', () => { if (stickReview.saving || stickPreparation.running || activeAnalysis()) return; stickReview.data.annotations = stickReview.data.annotations.filter(row => row !== point); setStickReviewDirty(true); renderStickReviewRows(); stickReviewPositionChanged(); }, 'secondary');
    inspect.append(jump, remove);
    const check = el('td');
    for (const [kind, title] of [['before', 'Earlier automatic trace'], ['current', 'Current trace']]) {
      const diagnostic = stickReview.data.landmark_diagnostics?.[kind]?.find(row => Math.abs(row.time - point.time) < .0001);
      if (!diagnostic || kind === 'current' && stickReview.data.movement?.stale) continue;
      const text = Object.entries(diagnostic.tips || {}).map(([side, value]) => `${side}: ${value.reason}${value.error_pixels === null ? '' : ` · ${value.error_pixels.toFixed(1)} px from mark`}`).join('; ');
      if (text) check.append(el('small', `${title} · ${text}`));
    }
    tr.append(el('td', time(point.time, true)), el('td', landmarks), check, el('td', controls), el('td', point.note || '—'), inspect); body.append(tr);
  }
  $('stick-review-landmark-count').textContent = `${stickReview.data.annotations.length} point(s)`;
  $('stick-review-chart-summary').setAttribute('aria-label', `${stickReview.data.annotations.length} labeled reference points`);
}
function setStickReviewDirty(dirty = true) {
  stickReview.dirty = dirty;
  $('stick-review-save').disabled = !stickReview.data || !dirty;
  $('stick-review-download').disabled = !stickReview.data;
  updateStickWorkspace(); updateMarkTools();
}
function resetStickReviewState() {
  stickReview.token++; cancelAnimationFrame(stickReview.frame);
  stickReview.name = stickReview.jobId = stickReview.data = null;
  stickReview.dirty = false; stickReview.reloadAfterDerivation = null;
  stickReview.windowToken++; stickReview.trackingWindow = null; stickReview.windowPending = false; stickReview.windowRetry = 0;
  const video = $('cal-video'); releasePreview(video); video.pause(); video.removeAttribute('src'); delete video.dataset.stickSource; video.load();
  stickReview.framePurpose = 'initial'; stickReview.frameEdits = false; stickReview.loading = false;
  show('stick-review-workspace', false); show('stick-review-reset', false);
  $('stick-review-current-name').textContent = 'Select a StickCam clip above';
  stickReview.saving = null; setStickReviewDirty(false);
}
async function openStickReview() {
  if (!state.job) return;
  const name = $('calibration-clip').value;
  if (!name) throw new Error('Choose a StickCam clip in Calibrate & review StickCam first.');
  if (stickReview.dirty) throw new Error('Save or download the current landmarks and references before reloading this review.');
  if (state.calibrationName !== name || !state.metadata) { await loadCalibration(name); return; }
  if (stickReview.loading) return;
  if (stickReview.frameEdits) throw new Error('Add or discard the current frame correction before reloading.');
  const token = ++stickReview.token, jobToken = state.jobToken, jobId = state.job.id;
  stickReview.name = name; stickReview.jobId = jobId; stickReview.data = null; setStickReviewDirty(false);
  stickReview.windowToken++; stickReview.trackingWindow = null; stickReview.windowPending = false; stickReview.windowRetry = 0;
  $('stick-review-current-name').textContent = clipLabel(state.job.inputs.radio.find(record => record.name === name));
  show('stick-review-workspace'); show('stick-review-reset', false); $('stick-review-rows').replaceChildren();
  message('stick-review-message', 'Reading saved tracking and controls…');
  stickReview.loading = true; updateMarkTools();
  let data;
  try { data = await api(`/api/jobs/${jobId}/stick-review/${part(name)}`); }
  finally { if (token === stickReview.token) { stickReview.loading = false; updateMarkTools(); } }
  if (token !== stickReview.token || jobToken !== state.jobToken) return;
  stickReview.data = data; data.annotations ||= []; show('stick-review-reset', data.stale_source_annotations > 0);
  const cacheLabel = data.movement ? `Saved movement trace loaded (${data.movement.source_samples.toLocaleString()} source samples; displayed at ${rateText(data.movement.sample_fps)} Hz).` : `No usable saved movement trace for current settings (${data.cache.status}). The video can still be labeled manually.`;
  const warning = data.calibration_changed ? ' Calibration changed since these labels were saved; review them before reuse.' : '';
  message('stick-review-message', `${cacheLabel} ${data.annotations.length} reference point(s) saved.${data.landmarks_pending ? ' Save and re-derive to activate the saved tracking landmarks.' : ''}${warning}${data.stale_source_annotations ? ' Saved labels belong to a changed source; start a new review to replace them.' : ''}`);
  if (data.calibration_changed) $('stick-review-message').classList.add('warning');
  setStickReviewDirty(data.landmarks_pending); renderStickReviewRows(); stickReviewPositionChanged(); updateStickWorkspace();
}
function addStickReviewReference() {
  if (!stickReview.data || stickReview.saving) return;
  const at = stickWorkspaceTime(), point = stickReviewPointAt(at, .005) || {time: Number(at.toFixed(3))};
  point.time = Number(at.toFixed(3));
  const controls = {};
  for (const axis of ['throttle', 'yaw', 'pitch', 'roll']) {
    const value = $(`stick-review-${axis}`).value.trim();
    if (value === '') continue;
    const number = Number(value); if (!Number.isFinite(number) || number < -1 || number > 1) throw new Error(`${axis} must be between −1 and +1.`);
    controls[axis] = number;
  }
  if (Object.keys(controls).length) point.controls = controls; else delete point.controls;
  point.note = $('stick-review-note').value.trim(); if (!point.note) delete point.note;
  if (!point.left_tip && !point.right_tip && !point.calibration && !point.controls && !point.note) throw new Error('Mark a stick tip, enter at least one expected control value, or add a review note.');
  const rows = stickReview.data.annotations, index = rows.findIndex(row => Math.abs(row.time - at) <= .005);
  if (index >= 0) rows[index] = point; else rows.push(point);
  rows.sort((a, b) => a.time - b.time); setStickReviewDirty(true); renderStickReviewRows(); stickReviewPositionChanged();
}
async function saveStickReview(resetStaleSource = false) {
  if (stickReview.saving) return stickReview.saving;
  if (!stickReview.data) return;
  const pending = persistStickReview(resetStaleSource);
  stickReview.saving = pending; updateMarkTools();
  try { await pending; }
  finally { if (stickReview.saving === pending) { stickReview.saving=null; updateMarkTools(); } }
}
async function persistStickReview(resetStaleSource) {
  const token = stickReview.token, jobToken = state.jobToken;
  const result = await api(`/api/jobs/${stickReview.jobId}/stick-review/${part(stickReview.name)}`, {method:'PUT', json:{annotations:stickReview.data.annotations, reset_stale_source:resetStaleSource, baseline_calibration:stickReview.data.calibration}});
  if (token !== stickReview.token || jobToken !== state.jobToken) return;
  stickReview.data.annotations = result.annotations; stickReview.data.annotation_revision = result.revision;
  stickReview.data.calibration = result.calibration; stickReview.data.tracking_landmark_count = result.tracking_landmark_count; stickReview.data.landmarks_pending = false;
  if (result.calibration && state.job?.id === stickReview.jobId) {
    state.job.calibrations[stickReview.name] = clone(result.calibration);
    for (const config of [state.calibrationName === stickReview.name ? state.calibration : null, state.calibrationDrafts[stickReview.name]?.config]) if (config) {
      if (result.calibration.tracking_landmarks) config.tracking_landmarks = clone(result.calibration.tracking_landmarks);
      else delete config.tracking_landmarks;
    }
    if (state.calibrationName === stickReview.name) rememberCalibrationDraft();
  }
  if (result.landmarks_changed && stickReview.data.movement) stickReview.data.movement.stale = true;
  if (result.landmarks_changed) { stickReview.windowToken++; stickReview.windowPending = false; stickReview.trackingWindow = null; }
  setStickReviewDirty(false); show('stick-review-reset', false);
  message('stick-review-message', `Saved review revision ${result.revision}: ${result.tracking_landmark_count} timed tracking landmark(s). ${result.landmarks_enabled ? result.landmarks_changed ? 'Save & re-derive this clip to apply the changes to its movement trace.' : 'Unchanged tracking landmarks reuse their saved movement data.' : 'Confirm the clip calibration, then re-derive to use these landmarks.'} Expected controls and notes remain diagnostic references.`);
  refreshStickData(true).catch(error => message('stick-data-message', error.message, true));
  renderStickReviewRows(); drawStickReviewChart();
}
function downloadStickReview() {
  if (!stickReview.data) return;
  const payload = {...stickReview.data, exported_at: new Date().toISOString(), annotations: stickReview.data.annotations};
  const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], {type:'application/json'}));
  const link = el('a'); link.href = url; link.download = `${stickReview.name.replace(/[^a-z0-9._-]+/gi, '_')}.stickcam-review.json`;
  document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function removeStickReviewPoint() {
  if (!stickReview.data || stickReview.saving) return;
  const at = stickWorkspaceTime(), point = stickReviewPointAt(at, .2);
  if (!point) { message('stick-review-message', 'There is no reference point near the current video time.'); return; }
  stickReview.data.annotations = stickReview.data.annotations.filter(row => row !== point); setStickReviewDirty(true); renderStickReviewRows(); stickReviewPositionChanged();
}
function seekStickReviewFromChart(event) {
  if (!stickReview.data) return;
  const rect = $('stick-review-chart').getBoundingClientRect(), fraction = Math.max(0, Math.min(1, (event.clientX - rect.left - 62) / Math.max(1, rect.width - 74)));
  seekStickWorkspace(fraction * stickReview.data.duration).catch(error => message('stick-review-message', error.message, true));
}
for (const event of ['timeupdate', 'seeked', 'loadedmetadata', 'loadeddata', 'pause']) $('cal-video').addEventListener(event, stickReviewPositionChanged);
function stickReviewOverlayTick() { drawStickReviewOverlay(); if (!$('cal-video').paused) stickReview.frame = requestAnimationFrame(stickReviewOverlayTick); }
$('cal-video').addEventListener('play', () => { setPreviewPlaybackIntent($('cal-video'),true); cancelAnimationFrame(stickReview.frame); stickReviewOverlayTick(); });
$('cal-video').addEventListener('pause', () => { setPreviewPlaybackIntent($('cal-video'),false); cancelAnimationFrame(stickReview.frame); });
$('stick-review-load').onclick = guarded(openStickReview, 'stick-review-message');
$('stick-review-save').onclick = guarded(async () => { if (stickReview.frameEdits) addStickFrameCorrection(); await saveStickReview(false); }, 'stick-review-message');
$('stick-review-download').onclick = downloadStickReview;
$('stick-review-calibration-landmark').onclick = guarded(addStickFrameCorrection, 'stick-review-message');
$('stick-review-rederive').onclick = guarded(async () => {
  const name = $('calibration-clip').value, jobId = state.job.id, jobToken = state.jobToken;
  if (!name) throw new Error('Choose a StickCam clip first.');
  if (state.calibrationName !== name || !state.calibrationFrameReady) await loadCalibration(name);
  if (stickReview.framePurpose === 'correction') { if (stickReview.frameEdits) addStickFrameCorrection(); }
  else await saveCalibration(false);
  if (stickReview.data && stickReview.name === name) await saveStickReview(false);
  const settings = {sample_fps:Number($('sample-fps').value), stick_sampling_ratio:readStickSamplingRatio(), decode_mode:$('decode-mode').value, tracking_backend:$('tracking-backend').value, radio_names:[name], capture_tracking_geometry:true};
  const task = await api(`/api/jobs/${jobId}/derive-sticks`, {method:'POST', json:settings});
  if (jobToken !== state.jobToken) return;
  Object.assign(state.job.calibrations, task.calibrations || {});
  if (task.calibrations?.[name]) state.calibration = clone(task.calibrations[name]);
  state.job.settings.sample_fps=settings.sample_fps; state.job.settings.stick_sampling_ratio=settings.stick_sampling_ratio; state.job.settings.tracking_backend=settings.tracking_backend;
  stickReview.reloadAfterDerivation=name; $('cal-video').pause();
  message('stick-review-message', `Re-deriving ${name} with its calibration and timed landmarks, including complete radio/gimbal/tip overlay data. Unchanged corrected data is reused.`);
  watchStickPreparation(task.id, jobId, jobToken);
}, 'stick-review-message');
$('stick-review-add').onclick = guarded(addStickReviewReference, 'stick-review-message');
$('stick-review-remove').onclick = removeStickReviewPoint;
$('stick-review-chart').addEventListener('click', seekStickReviewFromChart);
$('stick-review-show-overlay').onchange = drawStickReviewOverlay;
$('stick-review-audio').onchange = () => { $('cal-video').muted = !$('stick-review-audio').checked; };
$('stick-review-speed').onchange = () => { $('cal-video').playbackRate = Number($('stick-review-speed').value); };
$('stick-review-reset').onclick = guarded(async () => {
  if (!window.confirm('The saved points belong to an earlier version of this source video. Save the current review points as a new review for this source?')) return;
  setStickReviewDirty(true); await saveStickReview(true);
}, 'stick-review-message');
window.addEventListener('resize', () => { drawStickReviewOverlay(); drawStickReviewChart(); });
window.addEventListener('pagehide', () => $('cal-video').pause());

function renderStickPreparationLog(result) {
  const list = $('stick-data-log'); list.replaceChildren();
  const rows = result.clips || result.progress?.clips || [];
  for (const row of rows) {
    const details = [row.name, row.status === 'cached'
      ? row.reused ? `Reused saved movement data · ${Number(row.elapsed_seconds || 0).toFixed(2)}s cache lookup`
        : `Derived & saved · ${Number(row.extraction_seconds ?? row.elapsed_seconds ?? 0).toFixed(2)}s extraction`
      : `${row.message || row.status}${row.elapsed_seconds !== undefined ? ` · ${Number(row.elapsed_seconds).toFixed(2)}s elapsed` : ''}`];
    if (row.tracking_label) details.push(row.tracking_label);
    if (row.sample_fps) details.push(`${row.sample_fps} Hz`);
    if (row.samples !== undefined) details.push(`${row.samples.toLocaleString()} samples`);
    if (row.current_cache && row.current_cache.status !== 'cached') details.push('current settings now require fresh derivation');
    const stages = stickTrackingStageText(row);
    if (stages) details.push(stages);
    list.append(el('div', details.join(' · ')));
  }
  const current = result.progress;
  if (current?.name && current.elapsed_seconds !== undefined && !rows.some(row => row.name === current.name))
    list.append(el('div', `${current.name} · extracting · ${Number(current.elapsed_seconds).toFixed(2)}s elapsed at ${current.sample_fps || result.sample_fps || 'current'} Hz`));
}
function derivationTimeLabel(entry) {
  if (!entry || entry.elapsed_seconds === undefined || entry.elapsed_seconds === null) return 'Not recorded yet';
  const seconds = `${Number(entry.elapsed_seconds).toFixed(2)} s`, rate = entry.sample_fps ? ` at ${entry.sample_fps} Hz` : '';
  if (entry.status === 'running') return `In progress · ${seconds}${rate}`;
  if (entry.status === 'failed') return `Failed after ${seconds}${rate}`;
  if (['interrupted', 'cancelled'].includes(entry.status)) return `${entry.status[0].toUpperCase()}${entry.status.slice(1)} after ${seconds}${rate}`;
  return entry.reused ? `Reused in ${seconds}${rate}` : `${Number(entry.extraction_seconds ?? entry.elapsed_seconds).toFixed(2)} s${rate}`;
}
function watchStickPreparation(task, jobId, jobToken) {
  clearTimeout(stickPreparation.timer);
  const token = ++stickPreparation.token;
  stickPreparation.task = task; stickPreparation.jobId = jobId; stickPreparation.running = true;
  $('stick-sampling-ratio').disabled = true;
  $('derive-sticks').disabled = true; show('derive-sticks-cancel'); renderStickProgress(null, 'queued');
  $('analyze').disabled = true;
  const poll = async () => {
    if (token !== stickPreparation.token || jobToken !== state.jobToken) return;
    try {
      const result = await api(`/api/jobs/${jobId}/motion-review/${task}`);
      if (token !== stickPreparation.token || jobToken !== state.jobToken) return;
      const progress = result.progress;
      renderStickProgress(progress, result.status);
      if (progress) {
        message('stick-data-message', `${progress.completed}/${progress.total} videos finished. Completed movement data is saved and can be reused.`);
        renderStickPreparationLog(result);
      } else { message('stick-data-message', result.message); }
      await refreshStickData(true);
      if (['queued', 'running'].includes(result.status)) { stickPreparation.timer = setTimeout(poll, 1500); return; }
      stickPreparation.running = false; show('derive-sticks-cancel', false); $('derive-sticks').disabled = activeAnalysis();
      $('stick-sampling-ratio').disabled = activeAnalysis();
      updateFPVPreparationUI();
      renderStickPreparationLog(result);
      message('stick-data-message', result.status === 'complete' ? `${result.complete}/${result.total} StickCam recordings have saved movement data. ${result.total - result.complete} failed; see the derivation log.` : result.message, result.status === 'failed');
      const reloadName = stickReview.reloadAfterDerivation;
      if (reloadName && reloadName === stickReview.name) { stickReview.reloadAfterDerivation = null; await openStickReview(); }
    } catch (error) {
      message('stick-data-message', `Connection interrupted; retrying… ${error.message}`, true);
      stickPreparation.timer = setTimeout(poll, 3000);
    }
  };
  poll();
}
$('derive-sticks').onclick = guarded(async () => {
  const jobId = state.job.id, jobToken = state.jobToken;
  const settings = { sample_fps: Number($('sample-fps').value), stick_sampling_ratio: readStickSamplingRatio(), decode_mode: $('decode-mode').value, tracking_backend: $('tracking-backend').value };
  const task = await api(`/api/jobs/${jobId}/derive-sticks`, { method: 'POST', json: settings });
  if (jobToken !== state.jobToken) return;
  state.job.settings.sample_fps = settings.sample_fps; state.job.settings.stick_sampling_ratio = settings.stick_sampling_ratio; state.job.settings.tracking_backend = settings.tracking_backend;
  watchStickPreparation(task.id, jobId, jobToken);
}, 'stick-data-message');
$('derive-sticks-cancel').onclick = guarded(async () => {
  if (stickPreparation.task) await api(`/api/jobs/${stickPreparation.jobId}/motion-review/${stickPreparation.task}/cancel`, { method: 'POST' });
}, 'stick-data-message');
$('stick-data-refresh').onclick = guarded(() => refreshStickData(true), 'stick-data-message');
for (const id of ['sample-fps', 'stick-sampling-ratio']) $(id).onchange = guarded(async () => {
  updateStickSamplingPreview(); resetMotionReview(); await refreshStickData(true); await refreshFPVData(true);
}, 'stick-data-message');

function updateFPVPreparationUI() {
  const busy = activeAnalysis() || fpvPreparation.running || stickPreparation.running;
  for (const id of ['fpv-check-all', 'fpv-derive-all', 'fpv-select-eligible', 'fpv-preparation-mode', 'fpv-preparation-fps', 'fpv-preparation-decode']) $(id).disabled = busy;
  for (const id of ['fpv-check-selected', 'fpv-derive-selected']) $(id).disabled = busy || !fpvPreparation.selected.size;
  $('fpv-selection-count').textContent = `${fpvPreparation.selected.size} selected`;
  $('analyze').disabled = busy || !state.job || Object.values(state.job.inputs).flat().some(r => r.uploaded !== r.size);
  $('derive-sticks').disabled = busy;
  $('sample-fps').disabled = busy;
  $('decode-mode').disabled = busy;
  $('tracking-backend').disabled = busy;
  updateEvidenceSummary();
}
function embeddedMotionLabel(data) {
  return data.status === 'available' ? data.kind === 'quaternion_rates' ? 'Embedded attitude rates · preferred' : 'Embedded gyro · preferred'
    : data.status === 'unavailable' ? 'No supported embedded motion' : 'Not checked';
}
function renderFPVData() {
  const result = state.fpvData;
  if (!result || !state.job) return;
  $('fpv-data-count').textContent = `${result.embedded} embedded · ${result.cached} visual caches at requested rates`;
  const list = $('fpv-data-rows'); list.replaceChildren();
  for (const clip of result.clips) {
    const record = state.job.inputs.fpv.find(r => r.name === clip.name);
    const row = el('tr'), selectCell = el('td'), checkbox = el('input');
    checkbox.type = 'checkbox'; checkbox.checked = fpvPreparation.selected.has(clip.name);
    checkbox.setAttribute('aria-label', `Select ${clip.name}`);
    checkbox.onchange = () => { checkbox.checked ? fpvPreparation.selected.add(clip.name) : fpvPreparation.selected.delete(clip.name); updateFPVPreparationUI(); };
    selectCell.append(checkbox);
    const embedded = el('td', embeddedMotionLabel(clip.embedded));
    embedded.title = clip.embedded.message || '';
    if (clip.embedded.status === 'available') embedded.className = 'cache-ready';
    const visual = clip.visual, status = el('td', {cached:'Derived · saved', older_algorithm:'Earlier estimates · derive to upgrade', other_rate:'Saved at another rate', not_derived:'Not derived', unavailable:'Source or cache unavailable'}[visual.status] || visual.status);
    if (visual.status === 'cached') status.className = 'cache-ready';
    const requested = `${Number.isFinite(clip.source_fps) ? `${rateText(clip.source_fps)} fps → ` : ''}${Number.isFinite(clip.requested_fps) ? `${rateText(clip.requested_fps)} Hz requested` : 'Detecting frame rate…'}`;
    const details = el('td', `${requested}${visual.samples !== undefined ? ` · ${visual.samples.toLocaleString()} saved samples at ${rateText(visual.sample_fps)} Hz · ${visual.usable_percent}% usable tracking${visual.range ? ` · ${time(visual.range[0])}–${time(visual.range[1])}` : ''}` : ''}`);
    const elapsed = visual.extraction_seconds != null ? `${Number(visual.extraction_seconds).toFixed(2)} s extraction` : clip.last_preparation ? `${clip.last_preparation.status} · ${Number(clip.last_preparation.elapsed_seconds || 0).toFixed(2)} s` : 'Not recorded';
    const inspect = el('td');
    if (['cached', 'other_rate', 'older_algorithm'].includes(visual.status)) {
      const button = action('Review video + motion', guarded(() => openFPVVisualReview(clip.name, visual.sample_fps), 'fpv-data-message'));
      button.disabled = fpvPreparation.running; inspect.append(button);
    }
    row.append(selectCell, el('td', clip.name), el('td', clipDuration(record)), embedded, status, details, el('td', elapsed), inspect);
    list.append(row);
  }
  updateFPVPreparationUI();
}
async function refreshFPVData(force = false) {
  if (!state.job || state.job.inputs.fpv.some(r => r.uploaded !== r.size)) return;
  const jobId = state.job.id, jobToken = state.jobToken, fps = Number($('fpv-preparation-fps').value), mode = $('fpv-preparation-mode').value;
  const key = `${jobId}:${mode}:${fps}`;
  if (!force && key === fpvPreparation.key && Date.now() - fpvPreparation.refreshed < 3000) return;
  fpvPreparation.key = key; fpvPreparation.refreshed = Date.now();
  $('fpv-preparation-decode').value = $('decode-mode').value;
  const result = await api(`/api/jobs/${jobId}/fpv-data?${new URLSearchParams({sample_fps:fps,sampling_mode:mode})}`);
  if (jobToken !== state.jobToken || key !== fpvPreparation.key) return;
  state.fpvData = result;
  for (const clip of result.clips) {
    const record = state.job.inputs.fpv.find(item => item.name === clip.name);
    if (record && clip.metadata) record.metadata = clip.metadata;
    if (record) record.modified_time = clip.modified_time;
  }
  renderFPVData(); renderVideoLists();
  const active = result.tasks.find(task => ['queued','running'].includes(task.status));
  if (active && (!fpvPreparation.running || fpvPreparation.task !== active.id)) watchFPVPreparation(active.id, jobId, jobToken);
  if (!active && !fpvPreparation.running) {
    message('fpv-data-message', `${result.clips.filter(c => c.embedded.status === 'unknown').length} not checked · ${result.clips.filter(c => c.embedded.status === 'unavailable').length} without supported embedded motion. Check or derive selected clips, or prepare all. Existing data is reused.`);
    const list = $('fpv-data-log'); list.replaceChildren();
    for (const task of result.tasks.slice(-10).reverse()) list.append(action(`${task.status} · ${task.message}`, guarded(async () => {
      renderFPVPreparationLog(await api(`/api/jobs/${jobId}/motion-review/${task.id}`));
    }, 'fpv-data-message')));
  }
}
function renderFPVPreparationLog(result) {
  const list = $('fpv-data-log'); list.replaceChildren();
  for (const row of result.clips || result.progress?.clips || []) {
    list.append(el('div', `${row.name} · ${row.status === 'cached' ? row.reused ? 'Reused saved visual motion' : 'Derived & saved visual motion' : row.status === 'checked' ? embeddedMotionLabel(row.embedded) : row.message || row.status} · ${Number(row.elapsed_seconds || 0).toFixed(2)} s${row.usable_percent !== undefined ? ` · ${row.usable_percent}% usable tracking` : ''}`));
  }
}
function watchFPVPreparation(task, jobId, jobToken) {
  clearTimeout(fpvPreparation.timer);
  const token = ++fpvPreparation.token;
  fpvPreparation.task = task; fpvPreparation.running = true; fpvPreparation.jobId = jobId;
  updateFPVPreparationUI(); show('fpv-preparation-cancel'); show('fpv-data-progress');
  const poll = async () => {
    if (token !== fpvPreparation.token || jobToken !== state.jobToken) return;
    try {
      const result = await api(`/api/jobs/${jobId}/motion-review/${task}`);
      if (token !== fpvPreparation.token || jobToken !== state.jobToken) return;
      if (result.progress) {
        const p = result.progress; $('fpv-data-progress').value = p.percent;
        message('fpv-data-message', `${p.completed}/${p.total} clips finished · ${p.name} · ${p.stage === 'embedded' ? 'Checking embedded motion' : 'Deriving visual motion'} · ${Number(p.elapsed_seconds || 0).toFixed(1)} s on this clip · ${Number(p.percent).toFixed(1)}% overall${p.stage === 'visual' ? ` · current clip ${etaText(p.eta_seconds)}` : ''}`);
      } else { $('fpv-data-progress').removeAttribute('value'); message('fpv-data-message', result.message); }
      renderFPVPreparationLog(result);
      await refreshFPVData(true);
      if (['queued','running'].includes(result.status)) { fpvPreparation.timer = setTimeout(poll, 1500); return; }
      fpvPreparation.running = false; show('fpv-preparation-cancel', false); show('fpv-data-progress', false);
      renderFPVData(); renderFPVPreparationLog(result); updateEvidenceSummary();
      message('fpv-data-message', result.status === 'complete' ? result.action === 'check' ? `Embedded motion check finished · ${result.failed} failed; see log.` : `${result.cached} visual caches ready · ${result.skipped} clips kept embedded motion · ${result.failed} failed; see log.` : result.message, result.status === 'failed' || result.failed > 0);
    } catch(error) {
      message('fpv-data-message', `Connection interrupted; retrying… ${error.message}`, true);
      fpvPreparation.timer = setTimeout(poll, 3000);
    }
  };
  poll();
}
async function startFPVPreparation(action, selected) {
  const jobId = state.job.id, jobToken = state.jobToken;
  const fps = Number($('fpv-preparation-fps').value), mode = $('fpv-preparation-mode').value, decode = $('fpv-preparation-decode').value;
  const task = await api(`/api/jobs/${jobId}/prepare-fpv`, {method:'POST', json:{action, sample_fps:fps, decode_mode:decode, tracking_backend:$('tracking-backend').value,
    sampling_mode:mode, visual_camera:readVisualCamera(), camera_uptilt_deg:Number($('camera-uptilt-deg').value),
    ...(selected ? {names:[...fpvPreparation.selected]} : {})}});
  if (jobToken !== state.jobToken) return;
  $('decode-mode').value = decode; state.job.settings.fpv_visual_fps = fps; state.job.settings.fpv_sampling_mode = mode; state.job.settings.visual_camera = readVisualCamera(); state.job.settings.camera_uptilt_deg = Number($('camera-uptilt-deg').value);
  watchFPVPreparation(task.id, jobId, jobToken);
}
for (const [id, action, selected] of [['fpv-check-all','check',false], ['fpv-check-selected','check',true], ['fpv-derive-all','derive',false], ['fpv-derive-selected','derive',true]])
  $(id).onclick = guarded(() => startFPVPreparation(action, selected), 'fpv-data-message');
$('fpv-data-refresh').onclick = guarded(() => refreshFPVData(true), 'fpv-data-message');
$('fpv-select-eligible').onclick = () => {
  fpvPreparation.selected = new Set((state.fpvData?.clips || []).filter(c => c.embedded.status !== 'available').map(c => c.name)); renderFPVData();
};
$('fpv-clear-selection').onclick = () => { fpvPreparation.selected.clear(); renderFPVData(); };
$('fpv-preparation-cancel').onclick = guarded(async () => {
  if (fpvPreparation.task) await api(`/api/jobs/${fpvPreparation.jobId}/motion-review/${fpvPreparation.task}/cancel`, {method:'POST'});
}, 'fpv-data-message');
function readVisualCamera() { return {horizontal_fov_deg:Number($('visual-horizontal-fov').value), projection:$('visual-projection').value,track_full_rate:$('visual-track-full-rate').checked}; }
for (const id of ['visual-horizontal-fov','visual-projection','visual-track-full-rate']) $(id).onchange = guarded(async () => {
  const token = state.jobToken;
  const result = await api(path('/visual-camera'), {method:'PUT', json:readVisualCamera()});
  if (token !== state.jobToken) return;
  state.job.settings.visual_camera = result.visual_camera;
  resetFPVVisualReview(); resetMotionReview(); await refreshFPVData(true);
  message('visual-camera-message', 'Lens approximation saved. Existing caches from different geometry are retained; derive again to use these settings.');
}, 'visual-camera-message');
function updateFPVSamplingUI() { show('fpv-preparation-custom', $('fpv-preparation-mode').value === 'custom'); }
for (const id of ['fpv-preparation-fps', 'fpv-preparation-mode']) $(id).onchange = guarded(async () => {
  updateFPVSamplingUI(); resetMotionReview(); await refreshFPVData(true);
}, 'fpv-data-message');
$('fpv-preparation-decode').onchange = () => { $('decode-mode').value = $('fpv-preparation-decode').value; };
$('decode-mode').addEventListener('change', () => { $('fpv-preparation-decode').value = $('decode-mode').value; });

// Playback inspection reads the saved cache directly and cannot trigger extraction.
function resetFPVVisualReview() {
  fpvVisualReview.controller?.abort(); fpvVisualReview.controller = null;
  fpvVisualReview.token++; fpvVisualReview.windowToken++; fpvVisualReview.data = null;
  fpvVisualReview.name = null; fpvVisualReview.pending = false;
  cancelAnimationFrame(fpvVisualReview.frame);
  const video = $('fpv-visual-review-video'); video.pause(); releasePreview(video); video.removeAttribute('src'); video.load();
  const canvas = $('fpv-visual-review-canvas'); canvas.getContext('2d').clearRect(0,0,canvas.width,canvas.height);
  $('fpv-visual-review-seek').disabled = true; $('fpv-visual-review-window').disabled = true;
  show('fpv-visual-review', false);
}
async function openFPVVisualReview(name, fps) {
  resetFPVVisualReview(); const token = fpvVisualReview.token, jobToken = state.jobToken;
  fpvVisualReview.name = name; fpvVisualReview.fps = fps;
  const current = () => token === fpvVisualReview.token && jobToken === state.jobToken;
  show('fpv-visual-review'); $('fpv-visual-review-title').textContent = name;
  message('fpv-visual-review-message', 'Reading saved visual estimates…');
  $('fpv-visual-review').scrollIntoView({behavior:'smooth', block:'start'});
  try {
  const controller = new AbortController(); fpvVisualReview.controller = controller;
  const timeout = setTimeout(()=>controller.abort(),30000);
  let metadata;
  try { metadata = await api(path(`/metadata/fpv/${part(name)}`),{signal:controller.signal}); }
  catch (error) { if (current() && controller.signal.aborted) throw new Error('Reading video metadata timed out. Check the source connection and try Review video + motion again.'); throw error; }
  finally { clearTimeout(timeout); }
  if (!current()) return;
  fpvVisualReview.duration = metadata.duration;
  $('fpv-visual-review-seek').max = Math.max(0, metadata.duration - 1 / (metadata.fps || 30));
  $('fpv-visual-review-seek').value = 0;
  await refreshFPVVisualWindow(0);
  if (!current()) return;
  if (!fpvVisualReview.data) throw new Error('No saved visual chart was returned');
  $('fpv-visual-review-seek').disabled = false; $('fpv-visual-review-window').disabled = false;
  message('fpv-visual-review-message', 'Visual chart ready. Loading video preview…');
  await loadVideo($('fpv-visual-review-video'), 'fpv', name, current, 'fpv-visual-review-message', false, 0);
  if (!current()) return;
  message('fpv-visual-review-message', `Saved ${rateText(fps)} Hz angular estimates · ${fpvVisualReview.data.usable_percent}% usable rotation tracking · FOV ${fpvVisualReview.data.visual_camera?.horizontal_fov_deg}° · camera uptilt ${fpvVisualReview.data.camera_uptilt_deg}°. Approximate lens geometry; compare the estimated Roll/Pitch/Yaw with playback.`);
  syncFPVVisualPlayback();
  } catch (error) {
    if (current()) message('fpv-visual-review-message', `Could not open this review: ${error.message}. Retry Review video + motion.`,true);
  }
}
async function refreshFPVVisualWindow(at = $('fpv-visual-review-video').currentTime) {
  if (!fpvVisualReview.name || !state.job) return;
  const token = fpvVisualReview.token, jobToken = state.jobToken, windowToken = ++fpvVisualReview.windowToken;
  const width = Number($('fpv-visual-review-window').value) || fpvVisualReview.duration;
  const start = Math.max(0, Math.min(at - width * .25, fpvVisualReview.duration - width));
  const end = Math.min(fpvVisualReview.duration, start + width);
  if (!(end > start)) return;
  fpvVisualReview.pending = true;
  fpvVisualReview.controller?.abort();
  const controller = new AbortController(); fpvVisualReview.controller = controller;
  const timeout = setTimeout(()=>controller.abort(),30000);
  try {
    const query = new URLSearchParams({sample_fps:fpvVisualReview.fps, start, end, fill_gap_seconds:$('fpv-visual-fill-gaps').checked ? Number($('fpv-visual-gap-limit').value) : 0});
    const data = await api(path(`/fpv-data/${part(fpvVisualReview.name)}/visual?${query}`),{signal:controller.signal});
    if (token !== fpvVisualReview.token || jobToken !== state.jobToken || windowToken !== fpvVisualReview.windowToken) return;
    fpvVisualReview.data = data; drawFPVVisualReview();
    const gaps = Object.entries(data.gap_reasons || {}).map(([label,count])=>`${label}: ${count}`).join(' · ');
    message('fpv-visual-gap-summary', `${data.previous_algorithm ? 'Older algorithm — select this clip and Derive selected to use improved tracking. ' : ''}${data.tracking_label} · tracking ${rateText(data.tracking_fps)} Hz, saved ${rateText(data.sample_fps)} Hz. ${gaps ? `Missing output samples — ${gaps}. ` : ''}${data.fill_gap_seconds ? `${data.filled_samples} samples filled between valid observations (span ≤ ${data.fill_gap_seconds}s); dashed and excluded from matching.` : 'Gap filling is off.'}`);
  } catch (error) {
    if (token !== fpvVisualReview.token || jobToken !== state.jobToken || windowToken !== fpvVisualReview.windowToken) return;
    const detail = controller.signal.aborted ? 'Reading the saved visual chart timed out. Check the source connection and retry.' : `Could not read the saved visual chart: ${error.message}`;
    message('fpv-visual-review-message',detail,true);
    throw new Error(detail);
  } finally { clearTimeout(timeout); if (windowToken === fpvVisualReview.windowToken) fpvVisualReview.pending = false; }
}
function drawFPVVisualReview() {
  const data = fpvVisualReview.data, canvas = $('fpv-visual-review-canvas'); if (!data) return;
  const rect = canvas.getBoundingClientRect(); if (!rect.width) return;
  const ratio = devicePixelRatio || 1; canvas.width = Math.round(rect.width * ratio); canvas.height = Math.round(rect.height * ratio);
  const ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio);
  const left = 65, right = rect.width - 18, [start,end] = data.window, at = $('fpv-visual-review-video').currentTime;
  const x = value => left + (value - start) / (end - start) * (right - left);
  const laneHeight = (rect.height - 30) / data.traces.length;
  ctx.font = '11px system-ui';
  for (const [index, trace] of data.traces.entries()) {
    const top = index * laneHeight + 27, bottom = (index + 1) * laneHeight - 12, middle = (top + bottom) / 2;
    let maximum = 1e-6;
    for (const [, value] of trace.points) if (value !== null) maximum = Math.max(maximum, Math.abs(value));
    maximum *= 1.1;
    const y = value => middle - value / maximum * (bottom - top) / 2;
    ctx.fillStyle = '#aaa99f'; ctx.fillText(`${trace.label} · ${trace.unit}`, left, top - 9);
    ctx.strokeStyle = '#ffffff18';
    for (const v of [-maximum,0,maximum]) { ctx.beginPath(); ctx.moveTo(left,y(v)); ctx.lineTo(right,y(v)); ctx.stroke(); ctx.fillText(v.toFixed(Math.abs(v) < 10 ? 2 : 0),2,y(v)+4); }
    ctx.save(); ctx.beginPath(); ctx.rect(left,top,right-left,bottom-top); ctx.clip();
    ctx.strokeStyle = ['#f06f62','#72cf6b','#49a7f2'][index]; ctx.lineWidth = 1.4;
    let previous = null;
    for (const point of trace.points) {
      const [t,value,estimated] = point;
      if (value === null) { previous = null; continue; }
      if (previous) {
        ctx.setLineDash(estimated || previous[2] ? [4,4] : []);
        ctx.beginPath(); ctx.moveTo(x(previous[0]),y(previous[1])); ctx.lineTo(x(t),y(value)); ctx.stroke();
      }
      previous = point;
    }
    ctx.setLineDash([]);
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(x(at),top); ctx.lineTo(x(at),bottom); ctx.stroke(); ctx.restore();
    if (!trace.points.some(p=>p[1] !== null)) { ctx.fillStyle = '#aaa99f'; ctx.fillText('Tracking unavailable in this window',left+8,middle); }
  }
  ctx.fillStyle = '#aaa99f'; for (let i=0;i<=5;i++) { const t=start+(end-start)*i/5; ctx.fillText(time(t),Math.min(x(t)-9,right-38),rect.height-7); }
  $('fpv-visual-review-position').textContent = `${time(at,true)} / ${time(fpvVisualReview.duration)} · ${time(start)}–${time(end)} shown`;
}
function syncFPVVisualPlayback() {
  if (!fpvVisualReview.data) return;
  const video = $('fpv-visual-review-video'), at = video.currentTime, [start,end] = fpvVisualReview.data.window;
  $('fpv-visual-review-seek').value = at; drawFPVVisualReview();
  if ($('fpv-visual-review-follow').checked && !fpvVisualReview.pending && (at < start || at > end - .1) && at < fpvVisualReview.duration - .1)
    refreshFPVVisualWindow(at).catch(error=>message('fpv-visual-review-message',error.message,true));
}
function animateFPVVisualReview() {
  syncFPVVisualPlayback(); if (!$('fpv-visual-review-video').paused && fpvVisualReview.name) fpvVisualReview.frame = requestAnimationFrame(animateFPVVisualReview);
}
$('fpv-visual-review-video').addEventListener('play',()=>{cancelAnimationFrame(fpvVisualReview.frame); animateFPVVisualReview();});
for (const event of ['timeupdate','seeked','pause']) $('fpv-visual-review-video').addEventListener(event,syncFPVVisualPlayback);
$('fpv-visual-review-close').onclick = resetFPVVisualReview;
$('fpv-visual-review-window').onchange = guarded(()=>refreshFPVVisualWindow(),'fpv-visual-review-message');
$('fpv-visual-review-follow').onchange = syncFPVVisualPlayback;
for (const id of ['fpv-visual-fill-gaps','fpv-visual-gap-limit']) $(id).onchange = guarded(()=>refreshFPVVisualWindow(),'fpv-visual-review-message');
function seekFPVVisual(at) { const video=$('fpv-visual-review-video'); video.pause(); video.currentTime=Math.max(0,Math.min(at,Number($('fpv-visual-review-seek').max))); syncFPVVisualPlayback(); }
$('fpv-visual-review-seek').oninput = ()=>seekFPVVisual(Number($('fpv-visual-review-seek').value));
$('fpv-visual-review-canvas').onclick = event=>{if(!fpvVisualReview.data)return;const rect=event.currentTarget.getBoundingClientRect(),[a,b]=fpvVisualReview.data.window;seekFPVVisual(a+Math.max(0,Math.min(1,(event.clientX-rect.left-65)/(rect.width-83)))*(b-a));};
$('fpv-visual-review-canvas').onkeydown = event=>{if(['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();seekFPVVisual($('fpv-visual-review-video').currentTime+(event.key==='ArrowRight'?1:-1));}};
new ResizeObserver(drawFPVVisualReview).observe($('fpv-visual-review-canvas'));

let liveStickPreview = null;
function drawLiveStickInputs() {
  const canvas=$('stick-live-canvas'), preview=liveStickPreview;
  if (!preview?.time?.length) return;
  const rect=canvas.getBoundingClientRect(); if (rect.width < 10) return;
  const ratio=devicePixelRatio || 1; canvas.width=Math.round(rect.width*ratio); canvas.height=Math.round(rect.height*ratio);
  const ctx=canvas.getContext('2d'); ctx.scale(ratio,ratio); ctx.font='11px system-ui';
  const order=preview.time.map((_,i)=>i).sort((a,b)=>preview.time[a]-preview.time[b]);
  const first=preview.time[order[0]], last=preview.time[order[order.length-1]], span=Math.max(.1,last-first);
  const left=78,right=rect.width-20,x=t=>left+(t-first)/span*(right-left);
  const axes=['throttle','yaw','pitch','roll'], lane=(rect.height-26)/4;
  for (const [laneIndex,axis] of axes.entries()) {
    const top=laneIndex*lane+18, bottom=(laneIndex+1)*lane-5, middle=(top+bottom)/2;
    const y=v=>middle-v*(bottom-top)/2;
    ctx.fillStyle='#aaa99f'; ctx.fillText(axis[0].toUpperCase()+axis.slice(1),5,middle+4);
    ctx.strokeStyle='#ffffff18';
    for (const v of [-1,0,1]) { ctx.beginPath();ctx.moveTo(left,y(v));ctx.lineTo(right,y(v));ctx.stroke(); }
    ctx.strokeStyle=['#dda354','#49a7f2','#72cf6b','#f06f62'][laneIndex];ctx.lineWidth=1.5;ctx.beginPath();let pen=false;
    for (const i of order) { const v=preview.channels[axis][i]; if(v===null){pen=false;continue;}if(pen)ctx.lineTo(x(preview.time[i]),y(v));else ctx.moveTo(x(preview.time[i]),y(v));pen=true; }
    ctx.stroke(); const latest=preview.channels[axis].at(-1), q=preview.quality[axis].at(-1);
    ctx.fillStyle=ctx.strokeStyle;ctx.fillText(latest===null?'Unobserved':`${latest.toFixed(2)} · quality ${q.toFixed(2)}`,Math.max(left,right-175),top+10);
  }
  ctx.fillStyle='#aaa99f';ctx.fillText(time(first,true),left,rect.height-4);ctx.fillText(time(last,true),right-55,rect.height-4);
}
new ResizeObserver(drawLiveStickInputs).observe($('stick-live-canvas'));
let ratePresets = { presets: {}, default: null };
async function loadRatePresets(selected = '') {
  ratePresets = await api('/api/rate-presets');
  $('rate-preset').replaceChildren(new Option('Current / custom rates', ''), ...Object.keys(ratePresets.presets).map(name => new Option(`${name}${ratePresets.default === name ? ' · default' : ''}`, name)));
  if (ratePresets.presets[selected]) $('rate-preset').value = selected;
  else if (!selected && !state.job) applyDefaultRatePreset();
  message('rate-preset-message', `Stored in rate_presets.yaml · ${Object.keys(ratePresets.presets).length} preset(s)${ratePresets.default ? ` · default: ${ratePresets.default}` : ''}`);
}
function applyDefaultRatePreset() {
  const name = ratePresets.default, profile = name && ratePresets.presets[name];
  $('rate-preset').value = profile ? name : '';
  $('rate-preset-name').value = profile ? name : '';
  loadRateProfile(profile || null);
}
function selectSavedRatePreset(profile) {
  const match = profile && Object.entries(ratePresets.presets).find(([, saved]) => JSON.stringify(saved) === JSON.stringify(profile));
  const name = match?.[0] || '';
  $('rate-preset').value = name;
  $('rate-preset-name').value = name;
}
$('rate-preset-load').onclick = guarded(async () => {
  const name = $('rate-preset').value;
  if (!name) throw new Error('Choose a saved rate preset.');
  // Re-read so direct edits to YAML are picked up without a server restart.
  await loadRatePresets(name);
  if (!ratePresets.presets[name]) throw new Error('That preset is no longer in the YAML file.');
  loadRateProfile(ratePresets.presets[name]); $('rate-preset-name').value = name;
  message('rate-preset-message', `${name} loaded. These rates will be used for the next analysis or motion chart.`);
}, 'rate-preset-message');
$('rate-preset-save').onclick = guarded(async () => {
  const name = $('rate-preset-name').value.trim();
  if (!name) throw new Error('Enter a name for these rates.');
  const enabled = $('use-fpv-rates').checked;
  $('use-fpv-rates').checked = true;
  const profile = readRateProfile(); $('use-fpv-rates').checked = enabled;
  await api('/api/rate-presets', { method: 'PUT', json: { name, profile, make_default: $('rate-preset-default').checked } });
  await loadRatePresets(name); loadRateProfile(profile);
  message('rate-preset-message', `${name} saved to rate_presets.yaml${$('rate-preset-default').checked ? ' and set as the default for new sessions' : ''}.`);
}, 'rate-preset-message');

const portrait = { pair: null, token: 0 };
const portraitDefaults = { split: .6, fpv: { zoom: .6, x: 0, y: 0, rotation: 0, background: 'blur' }, radio: { zoom: 1, x: 0, y: 0, rotation: 0, background: 'black' }, audio: 'radio', in: 0, out: null };
function readPortraitLayout() {
  const result = { split: Number($('portrait-split').value) / 100, audio: $('portrait-audio').value, in: Number($('portrait-in').value), out: Number($('portrait-out').value) };
  for (const kind of ['fpv', 'radio']) result[kind] = { zoom: Number($(`portrait-${kind}-zoom`).value) / 100, x: Number($(`portrait-${kind}-x`).value), y: Number($(`portrait-${kind}-y`).value), rotation: Number($(`portrait-${kind}-rotation`).value), background: $(`portrait-${kind}-background`).value };
  return result;
}
function writePortraitLayout(layout) {
  $('portrait-split').value = layout.split * 100;
  for (const kind of ['fpv', 'radio']) for (const key of ['zoom', 'rotation', 'x', 'y', 'background']) $(`portrait-${kind}-${key}`).value = key === 'zoom' ? layout[kind][key] * 100 : (layout[kind][key] ?? portraitDefaults[kind][key]);
  $('portrait-audio').value = layout.audio; $('portrait-in').value = layout.in; $('portrait-out').value = layout.out ?? (Math.floor(portrait.pair.overlap_duration * 1000) / 1000);
  drawPortrait();
}
$('open-portrait').onclick = guarded(async () => {
  const saved = await saveAlignment(); if (!saved) return;
  pausePair(); const token = ++portrait.token, viewerToken = state.viewerToken;
  const result = await api(path('/portrait-layout'), { method: 'POST', json: { radio_video: saved.radio_video, fpv_video: saved.fpv_video, offset_seconds: saved.offset_seconds } });
  if (token !== portrait.token || viewerToken !== state.viewerToken) return;
  portrait.pair = result.pair;
  openPanel('portrait-panel'); $('portrait-pair-label').textContent = `${saved.fpv_video} + ${saved.radio_video} · saved offset ${offsetText(saved.offset_seconds)}`;
  $('portrait-seek').max = saved.overlap_duration; $('portrait-seek').value = elapsed();
  writePortraitLayout(result.layout); message('portrait-message', 'Adjust the framing, then save or export.');
  $('portrait-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
}, 'viewer-message');
function drawPortrait() {
  if (!$('portrait-panel').checkVisibility() || !portrait.pair || !state.pair) return;
  const canvas = $('portrait-canvas'), ctx = canvas.getContext('2d'), layout = readPortraitLayout(), top = Math.round(1920 * layout.split / 2) * 2 / 2;
  const width = canvas.width, height = canvas.height;
  ctx.fillStyle = '#000'; ctx.fillRect(0, 0, width, height);
  for (const [kind, y, paneHeight] of [['fpv', 0, top], ['radio', top, height - top]]) {
    const video = $(`${kind}-preview`), transform = layout[kind];
    if (!video.videoWidth || video.readyState < 2) continue;
    const fill = Math.max(width / video.videoWidth, paneHeight / video.videoHeight);
    ctx.save(); ctx.beginPath(); ctx.rect(0, y, width, paneHeight); ctx.clip();
    if (transform.background === 'blur') {
      const scale = fill * 1.12; ctx.filter = 'blur(12px)';
      ctx.drawImage(video, (width - video.videoWidth * scale) / 2, y + (paneHeight - video.videoHeight * scale) / 2, video.videoWidth * scale, video.videoHeight * scale); ctx.filter = 'none';
    }
    const zoom = Math.max(.1, Math.min(4, transform.zoom)) * fill;
    ctx.translate(width / 2 + transform.x / 100 * width / 2, y + paneHeight / 2 + transform.y / 100 * paneHeight / 2); ctx.rotate(transform.rotation * Math.PI / 180); ctx.scale(zoom, zoom); ctx.drawImage(video, -video.videoWidth / 2, -video.videoHeight / 2); ctx.restore();
  }
  $('portrait-split-label').textContent = `${Math.round(layout.split * 100)}% FPV / ${Math.round((1 - layout.split) * 100)}% StickCam`;
  $('portrait-seek').value = elapsed(); $('portrait-play').textContent = state.playing ? 'Pause' : 'Play';
  message('portrait-time', `${time(elapsed(), true)} / ${time(portrait.pair.overlap_duration)}`);
}
for (const input of document.querySelectorAll('#portrait-panel input, #portrait-panel select')) input.addEventListener('input', () => { drawPortrait(); message('portrait-message', 'Layout changed · save to keep it.'); });
$('portrait-seek').oninput = () => { pausePair(); seekPair(Number($('portrait-seek').value)); };
for (const id of ['radio-preview', 'fpv-preview']) $(id).addEventListener('seeked', drawPortrait);
$('portrait-play').onclick = () => {
  const layout = readPortraitLayout();
  if (!state.playing && (elapsed() < layout.in || elapsed() >= layout.out)) seekPair(layout.in);
  $('pair-play').click();
};
$('portrait-reset').onclick = () => { writePortraitLayout(portraitDefaults); message('portrait-message', 'Default layout restored · save to keep it.'); };
async function savePortraitLayout() {
  if (!portrait.pair || !state.pair || !state.pair.reviewed || Math.abs(state.pair.offset_seconds - portrait.pair.offset_seconds) > 1e-7) throw new Error('Reopen the editor after saving the current video alignment.');
  const pair = portrait.pair, token = state.viewerToken;
  const result = await api(path('/portrait-layout'), { method: 'PUT', json: { radio_video: pair.radio_video, fpv_video: pair.fpv_video, offset_seconds: pair.offset_seconds, layout: readPortraitLayout() } });
  if (token !== state.viewerToken) return;
  message('portrait-message', 'Layout saved for this pair.'); return result.layout;
}
$('portrait-save').onclick = guarded(savePortraitLayout, 'portrait-message');
$('portrait-export').onclick = guarded(async () => {
  const layout = await savePortraitLayout(); if (!layout) return;
  const jobId = state.job.id, token = state.jobToken, pair = portrait.pair;
  const result = await api(path('/exports'), { method: 'POST', json: { pairs: [{ radio_video: pair.radio_video, fpv_video: pair.fpv_video, offset_seconds: pair.offset_seconds }], fps: Number($('portrait-fps').value), profile: 'h264', portrait: layout } });
  if (token !== state.jobToken) return;
  message('portrait-message', 'Portrait export queued. Progress and downloads are in Aligned exports.');
  state.job = await api(`/api/jobs/${jobId}`); renderExports();
}, 'portrait-message');

$('collapse-all').onclick = () => { for (const panel of document.querySelectorAll('[data-major-panel]')) panel.open = false; };
$('expand-all').onclick = () => { for (const panel of document.querySelectorAll('[data-major-panel]')) panel.open = true; };
for (const panel of document.querySelectorAll('[data-major-panel]')) panel.addEventListener('toggle', () => {
  if (!panel.open) for (const video of panel.querySelectorAll('video')) video.pause();
  if (!panel.open && panel.id === 'viewer-panel') pausePair();
  if (!panel.open && panel.id === 'manual-pair-panel') manualBrowser.pause();
  if (!panel.open && panel.id === 'motion-panel') pauseMotionPreview();
});
$('csv-download').addEventListener('click', event => event.stopPropagation());
$('delete-session').onclick = () => {
  if (!state.job) return;
  message('delete-session-description', `Delete “${state.job.label || state.job.id}” from the frontend? Its source footage, exports and download links will be kept. You can restore it later.`);
  show('delete-session-confirmation');
};
$('cancel-delete-session').onclick = () => show('delete-session-confirmation', false);
$('confirm-delete-session').onclick = guarded(async () => {
  if (!state.job) return;
  const id = state.job.id;
  await api(`/api/jobs/${id}`, {method:'DELETE'});
  state.uploadController?.abort(); clearSession(); await history();
  message('session-message', 'Session deleted from the frontend. Exports and source footage were kept; use Deleted sessions to restore.');
}, 'session-message');


// A single main path; technical tools and evidence remain available on demand.
function renderWorkflow() {
  if (!state.job) return;
  const job = state.job, rows = state.results?.rows || [], proposed = rows.filter(r => r.alignment_available && r.alignment_suggested && !r.rejected), reviewed = rows.filter(r => r.reviewed && !r.rejected);
  const readiness=matchingReadiness(readEvidenceSources());message('matching-readiness',readiness.message,readiness.blocked);
  $('match-sample-percent').disabled=readiness.autoSeed;
  $('seed-run-summary').replaceChildren();
  const seedRun=state.results?.workflow?.seed_search;
  if(seedRun) {
    const container=$('seed-run-summary'),seed=seedRun.seed;
    container.append(el('p',seed?`Embedded-motion seed: ${seed.radio_video} ↔ ${seed.fpv_video} · offset ${offsetText(seed.offset_seconds)}. ${seedRun.attempts.length}/${job.inputs.radio.length} StickCam clips prepared; ${(seedRun.spot_checks || []).length} spot checks. All pairings need review.`:seedRun.seed_ignored?'The saved movement seed includes an ignored recording; its date predictions are inactive.':'No distinct embedded-motion seed found. Review the automatic region proposals or try another matching method.'));
    const details=el('details');details.append(el('summary','Automatic preparation, clocks & spot checks'));
    for(const model of seedRun.clocks || [])details.append(el('p',`${model.label}${model.recorder?` · ${model.recorder}`:' · recorder unassigned'} · raw modified-date difference ${offsetText(model.raw_modified_difference_seconds)} · duration-adjusted clock ${offsetText(model.delta_seconds)} · ${model.spot_status}. Provisional until content is independently confirmed.`));
    for(const check of seedRun.spot_checks || [])details.append(el('p',`${check.radio_video} ↔ ${check.fpv_video}: ${check.status}${check.offset_seconds==null?'':` · motion offset ${offsetText(check.offset_seconds)}`}`,check.status==='disagrees'?'error':'hint'));
    for(const clip of seedRun.attempts || []) {
      const row=el('div',undefined,'actions');row.append(el('span',`${clip.name}: ${clip.status} · ${Number(clip.observed_seconds || 0).toFixed(1)}s observed · ${clip.cache_reused?'saved data reused':`${Number(clip.elapsed_seconds || 0).toFixed(1)}s preparation`}${clip.message?` · ${clip.message}`:''}`));
      if(clip.calibration)row.append(action('Review radio regions',guarded(async()=>{openPanel('calibration-panel');await loadCalibration(clip.name);$('cal-stage').scrollIntoView({behavior:'smooth',block:'start'});},'workflow-message')));
      details.append(row);
    }
    container.append(details);
  }
  const ready = Object.values(job.inputs).flat().every(r => r.uploaded === r.size);
  const running = activeAnalysis() || stickPreparation.running || fpvPreparation.running;
  $('workflow-title').textContent = job.label;
  $('workflow-stats').replaceChildren(...[[job.inputs.fpv.length,'FPV files'],[job.inputs.radio.length,'StickCam files'],[proposed.length,'proposed pairs'],[reviewed.length,'confirmed alignments']].map(([count,label]) => {const tile=el('div',undefined,'workflow-stat');tile.append(el('strong',String(count)),el('span',label));return tile;}));
  $('workflow-find').disabled = !ready || running || readiness.blocked || (!evidenceMode().motion && !evidenceMode().audio);
  $('workflow-find').textContent = state.results && !state.results.manual_only ? 'Run Quick Match · reuse saved data' : 'Run Quick Match';
  $('workflow-review').disabled = !rows.some(r => r.alignment_available && r.alignment_suggested && !r.reviewed && !r.rejected && r.review_state!=='later');
  $('workflow-manual').disabled = !ready;
  $('workflow-stick-review').disabled = !ready || !job.inputs.radio.length;
  $('workflow-calibrate').disabled = !ready || running;
  show('workflow-cancel', activeAnalysis()); show('workflow-progress', activeAnalysis()); $('workflow-progress').value = job.progress?.percent || 0;
  const samplePercent = Number($('match-sample-percent').value || 100);
  let advice = !ready ? 'Finish uploading your recordings first.' : running ? job.message : !state.results ? `Start Quick Match. Audio is tried before motion; only ${samplePercent === 100 ? 'all files' : `${samplePercent}% of one feed against all candidates in the other`} are analyzed in this pass. Calibration is needed only for unresolved motion comparisons.` : proposed.some(r => !r.reviewed) ? 'Review a proposed pair, check its shared events and timing, then confirm the alignment.' : reviewed.length ? `${reviewed.length} confirmed alignment(s) ready for export or the portrait editor. Unresolved clips can be examined below.` : 'No pair is confirmed. Inspect the exceptions and evidence; uncertain results stay available for review.';
  if (state.results && !state.results.manual_only && job.status !== 'complete') advice += ' Last completed results remain available while this run is pending, cancelled or failed.';
  const activeClips = job.progress?.active_clips || []; if (running && activeClips.length) advice += ' ' + activeClips.map(c=>`${c.name}: ${Number(c.percent || c.fraction * 100 || 0).toFixed(1)}%${c.eta_seconds == null ? '' : ` · about ${time(c.eta_seconds)} remaining`}`).join(' · ');
  message('workflow-message', advice, job.status === 'failed');
  const exceptions = [], workflow = state.results?.workflow || {};
  const deferredCount = (workflow.unscanned_fpv || []).length + (workflow.unscanned_radio || []).length;
  if (deferredCount) exceptions.push({text:workflow.strategy==='telemetry_seed'?`Sequential seed search left ${(workflow.unscanned_radio || []).length} StickCam clips unprocessed. Their pairs are date-only suggestions. Review the seed and spot-check results before confirming.`:`This ${workflow.sample_percent}% first pass left ${(workflow.unscanned_fpv || []).length} FPV and ${(workflow.unscanned_radio || []).length} StickCam files unscanned. Confirm a content match, use “Find related FPV segments” for possible O4 splits, then run all files to verify the remaining matches.`,action:'Run full matching',run:async()=>{$('match-sample-percent').value='100';$('embedded-seed-enabled').checked=false;$('matching-strategy').value='automatic';await startMatching();}});
  for (const item of workflow.unavailable_sources || []) exceptions.push({text:`${item.name}: ${item.reason}`,action:'Check sources',run:checkSources});
  const calibrationNames = [...(workflow.needs_calibration || [])];
  for (const name of job.inputs.radio.filter(r=>job.calibrations[r.name]?.source_changed).map(r=>r.name)) if (!calibrationNames.includes(name)) calibrationNames.push(name);
  for (const name of calibrationNames) if (!matchingCalibrationReady(job.calibrations[name],readEvidenceSources()) && (!readiness.autoSeed || seedRun?.attempts.some(r=>r.name===name && r.status!=='ready'))) exceptions.push({text:`${name}: ${readiness.autoSeed?'Automatic radio preparation was insufficient; review the attempted frame or try another clip.':'Motion fallback needs a saved calibration.'}`,action:'Calibrate',run:() => loadCalibration(name)});
  for (const clip of state.stickData?.clips || []) if (clip.quality_warning) exceptions.push({text:`${clip.name}: ${clip.quality_warning}`,action:'Inspect calibration',run:() => loadCalibration(clip.name)});
  for (const row of rows.filter(r => r.review_invalidated)) exceptions.push({text:`${row.fpv_video}: ${row.review_reason}`,action:'Review again',run:() => openPair(row.radio_video,row.fpv_video,row.saved_offset_seconds)});
  for (const issue of workflow.gimbal_activity_errors || []) exceptions.push({text:`${issue.name}: ${issue.reason}`,action:'Radio regions',run:async()=>{openPanel('calibration-panel');$('calibration-clip').value=issue.name;await loadCalibration(issue.name);}});
  for (const name of state.results?.unmatched_fpv || []) exceptions.push({text:`${name}: no confirmed content match. Review candidate evidence or refine the unresolved set.`,action:'Candidates',run:() => {openPanel('results-panel'); $('results-panel').scrollIntoView({behavior:'smooth'});}});
  $('workflow-exception-count').textContent = String(exceptions.length);
  $('workflow-exception-list').replaceChildren(...exceptions.map(item => {const row=el('div',undefined,'workflow-exception');row.append(el('span',item.text),action(item.action,guarded(item.run,'workflow-message')));return row;}));
  if (!exceptions.length) $('workflow-exception-list').append(el('p',state.results ? 'No outstanding exceptions reported by the last run.' : 'Matching will report unreadable sources, calibration needs and uncertain results here.','hint'));
  message('source-summary', Object.entries(job.source_folders || {}).map(([kind,roots]) => `${kind==='radio'?'StickCam':'FPV'}: ${roots.join(', ')}`).join(' · ')); show('source-relink',job.source==='local');
}
function setTechnicalTools(enabled) { $('technical-tools').checked=enabled;document.body.classList.toggle('show-technical',enabled);storage.setItem('fpv-technical-tools',enabled?'1':'0'); }
setTechnicalTools(storage.getItem('fpv-technical-tools')==='1');
$('technical-tools').onchange=()=>setTechnicalTools($('technical-tools').checked);
$('matching-strategy').onchange=()=>{if($('matching-strategy').value==='telemetry_seed'){selectEmbeddedSeed();return;}if ($('matching-strategy').value==='selected'){setTechnicalTools(true);openPanel('analysis-panel');}renderWorkflow();};
for(const id of ['embedded-seed-enabled','seed-spot-checks'])$(id).onchange=()=>{if(state.job)state.job.settings[id==='embedded-seed-enabled'?'embedded_seed_enabled':'seed_spot_checks']=id==='embedded-seed-enabled'?$(id).checked:Number($(id).value);renderWorkflow();};
$('match-sample-percent').onchange=()=>{if(state.job)state.job.settings.match_sample_percent=Number($('match-sample-percent').value);updateSelectionSummary();renderWorkflow();};
$('workflow-review').onclick=guarded(async()=>{const row=(state.results?.rows||[]).filter(r=>r.alignment_available && r.alignment_suggested && !r.reviewed&&!r.rejected&&r.review_state!=='later').sort((a,b)=>(b.confidence==='strong')-(a.confidence==='strong') || candidateStrength(b)-candidateStrength(a))[0];if(row)await openCandidate(row);},'workflow-message');
$('workflow-stick-review').onclick=guarded(async()=>{
  openPanel('calibration-panel');
  await refreshStickData(true);
  const target = state.calibrationName || state.stickData?.clips.find(clip=>clip.status==='cached')?.name || state.job.inputs.radio[0]?.name;
  if (!target) throw new Error('This session has no StickCam clips to review.');
  if (state.calibrationName !== target || !state.metadata) await loadCalibration(target);
  else if (!stickReview.data && !stickReview.loading) await openStickReview();
  $('cal-stage').scrollIntoView({behavior:'smooth',block:'start'});
},'workflow-message');
$('workflow-calibrate').onclick=guarded(async()=>{const pending=state.job.inputs.radio.find(r=>!calibrated(state.job.calibrations[r.name]));const name=pending?.name || $('calibration-clip').value,proposal=calibrationScan.rows.find(r=>r.name===name&&!r.confirmed&&r.calibration);if(proposal)await openCalibrationProposal(proposal);else await loadCalibration(name);$('calibration-panel').scrollIntoView({behavior:'smooth'});},'workflow-message');
$('workflow-scope').onclick=()=>{openPanel('video-list-panel');$('video-list-panel').scrollIntoView({behavior:'smooth'});};
$('workflow-cancel').onclick=()=>$('cancel-analysis').click();
async function checkSources(){message('source-message','Checking source files…');const token=state.jobToken,result=await api(path('/sources'));if(token!==state.jobToken)return;$('source-status').replaceChildren(...result.clips.map(r=>el('p',`${r.kind==='radio'?'StickCam':'FPV'} · ${r.name} · ${r.status}${r.reason ? ` · ${r.reason}`:''}`,r.status==='ready'?'hint':'error')));$('session-sources').open=true;message('source-message',`${result.clips.filter(r=>r.status==='ready').length}/${result.clips.length} source files ready.`);}
$('source-check').onclick=guarded(checkSources,'source-message');
$('relink-source').onclick=guarded(async()=>{const id=state.job.id,result=await api(path('/sources'),{method:'PUT',json:{kind:$('relink-kind').value,path:$('relink-path').value}});await selectJob(id);$('session-sources').open=true;message('source-message',`${result.relinked} existing files relinked. ${result.missing.length} filenames not found; their previous paths are kept.`);await checkSources();},'source-message');
function calibrationDraftKey(name){return `fpv-calibration-draft:${state.job?.id}:${name}`;}
$('discard-calibration-draft').onclick=guarded(async()=>{
  if (stickReview.framePurpose === 'correction') {
    stickReview.frameEdits=false; await showFrame(); updateStickWorkspace(); return;
  }
  const name=state.calibrationName;storage.removeItem(calibrationDraftKey(name));delete state.calibrationDrafts[name];state.calibration=null;await loadCalibration(name);
},'calibration-message');
$('stick-playhead').onchange=guarded(()=>seekStickWorkspace($('stick-playhead').value),'calibration-message');
window.addEventListener('beforeunload',event=>{if(stickReview.dirty||stickReview.frameEdits){event.preventDefault();event.returnValue='';}});
let calibrationDraftTimer=null;
$('calibration-panel').addEventListener('input',event=>{
  if (stickReview.framePurpose==='correction' && ['tip-radius','tip-threshold','tip-polarity','stickcam-mode','radio-rotation-deg','cal-rotate-180'].includes(event.target.id)) { stickReview.frameEdits=true;updateStickWorkspace();updateMarkTools(); }
  clearTimeout(calibrationDraftTimer);calibrationDraftTimer=setTimeout(rememberCalibrationDraft,200);
});
$('calibration-panel').addEventListener('toggle', guarded(async () => {
  if ($('calibration-panel').open && !calibrationLoad) await ensureCalibrationLoaded();
}, 'calibration-message'));
window.addEventListener('pagehide',rememberCalibrationDraft);
$('prepare-stabilization').onclick=guarded(async()=>{if(!state.pair)return;const pair=state.pair,name=pair.radio_video,config=state.job.calibrations[name];if(!config?.radio_outline||config.skip_motion||config.exclude){await loadCalibration(name);throw new Error('Save a clear radio outline and both tips, then prepare this clip.');}const jobId=state.job.id,jobToken=state.jobToken,task=await api(path('/derive-sticks'),{method:'POST',json:{radio_names:[name],sample_fps:Number($('sample-fps').value),stick_sampling_ratio:readStickSamplingRatio(),decode_mode:$('decode-mode').value,tracking_backend:$('tracking-backend').value}});openPanel('calibration-panel');watchStickPreparation(task.id,jobId,jobToken);message('viewer-message','Preparing saved outline data for this StickCam only. When complete, choose Centered & level and export. Unsafe or lost-outline intervals retain original framing.');},'viewer-message');

// Audio review is independent of optical motion and calibration.
let pairAudioToken=0, pairAudioData=null;
function drawPairAudio(){if(!pairAudioData||!state.pair)return;for(const kind of ['fpv','radio']){const canvas=$(`pair-audio-${kind}`),rect=canvas.getBoundingClientRect(),ratio=devicePixelRatio||1;if(!rect.width)continue;canvas.width=rect.width*ratio;canvas.height=rect.height*ratio;const ctx=canvas.getContext('2d');ctx.scale(ratio,ratio);const [start,end]=pairAudioData.window,left=45,right=rect.width-15,top=12,bottom=rect.height-25,center=(top+bottom)/2,x=t=>left+(t-start)/(end-start)*(right-left);ctx.fillStyle='#72cf6b22';for(const w of pairAudioData.sections?.windows||[])if(w.matched)ctx.fillRect(x(w.start),top,x(w.end)-x(w.start),bottom-top);ctx.strokeStyle='#49a7f2';for(const [t,low,high] of pairAudioData[kind]?.points||[]){ctx.beginPath();ctx.moveTo(x(t),center-low*(bottom-top)/2);ctx.lineTo(x(t),center-high*(bottom-top)/2);ctx.stroke();}ctx.fillStyle='#aaa99f';ctx.font='11px system-ui';for(let i=0;i<=5;i++){const t=start+(end-start)*i/5;ctx.fillText(time(t,true),Math.min(x(t),right-48),rect.height-7);}if(!pairAudioData[kind]?.available)ctx.fillText('No audio track',left,center);}}
$('pair-audio-toggle').onchange=guarded(async()=>{const enabled=$('pair-audio-toggle').checked;show('pair-audio-charts',enabled);const token=++pairAudioToken,viewerToken=state.viewerToken;if(!enabled||!state.pair)return;const pair=state.pair;pairAudioData=null;message('pair-audio-message','Reading cached audio waveforms…');const task=await api(path('/audio-review'),{method:'POST',json:{fpv_video:pair.fpv_video,radio_video:pair.radio_video}});while(token===pairAudioToken&&viewerToken===state.viewerToken&&$('pair-audio-toggle').checked){const query=new URLSearchParams({offset:pair.offset_seconds,start:pair.radio_start,end:pair.radio_start+pair.overlap_duration}),result=await api(path(`/motion-review/${task.id}?${query}`));if(token!==pairAudioToken||viewerToken!==state.viewerToken)return;if(['queued','running'].includes(result.status)){message('pair-audio-message',result.message);await sleep(1200);continue;}if(result.status!=='complete')throw new Error(result.message);pairAudioData=result;drawPairAudio();message('pair-audio-message',`${result.sections?.correlated_sections||0}/${result.sections?.usable_sections||0} usable 3-second boundary sections agree at this offset. Green bands show supporting content, not a probability. Click a waveform to seek both videos.`);break;}},'pair-audio-message');
for(const kind of ['fpv','radio'])$(`pair-audio-${kind}`).onclick=event=>{if(!pairAudioData||!state.pair)return;const rect=event.currentTarget.getBoundingClientRect(),[start,end]=pairAudioData.window,t=start+Math.max(0,Math.min(1,(event.clientX-rect.left-45)/(rect.width-60)))*(end-start);pausePair();seekPair(t-state.pair.radio_start);};
window.addEventListener('resize',drawPairAudio);
$('pair-motion-tools').onclick=guarded(async()=>{if(!state.pair)return;selectMotionPair(state.pair);openPanel('motion-panel');$('motion-panel').scrollIntoView({behavior:'smooth'});message('motion-message','Motion is loaded only when you choose Load motion signals. Experimental estimates require inspection.');},'viewer-message');

function renderSyncPoints(){
  const pair=state.pair;if(!pair)return;
  $('sync-points').replaceChildren(...(pair.sync_points || []).map(p=>el('p',`FPV ${time(p.fpv_time,true)} ↔ StickCam ${time(p.radio_time,true)} · offset ${offsetText(p.radio_time-p.fpv_time)}`,'hint')));
  message('sync-status',pair.sync_check?.message || 'No independent event checkpoints yet.', ['drift','offset_mismatch'].includes(pair.sync_check?.status));
  $('sync-export-ack').checked=false;
}
async function saveSyncPoints(points){
  if(!state.pair)return;const pair=state.pair,token=state.viewerToken;
  const info=await api(path('/sync-points'),{method:'PUT',json:{radio_video:pair.radio_video,fpv_video:pair.fpv_video,offset_seconds:pair.offset_seconds,points,manual_pair:!!pair.manual_pair,source_revisions:pair.source_revisions}});
  if(token!==state.viewerToken)return;info.date_suggestion=pair.date_suggestion;info.rhythm_suggestion=pair.rhythm_suggestion;info.manual_recorder=pair.manual_recorder;info.manual_sync_points=info.manual_pair?points:undefined;state.pair=info;renderSyncPoints();
}
$('sync-add').onclick=guarded(()=>saveSyncPoints([...(state.pair?.sync_points || []),{fpv_time:Number($('sync-fpv-time').value),radio_time:Number($('sync-radio-time').value)}]),'viewer-message');
$('sync-clear').onclick=guarded(()=>saveSyncPoints([]),'viewer-message');
async function decideReview(decision){
  if(!state.pair)return;await api(path('/review-state'),{method:'POST',json:{radio_video:state.pair.radio_video,fpv_video:state.pair.fpv_video,state:decision,offset_seconds:state.pair.offset_seconds,manual_pair:!!state.pair.manual_pair,source_revisions:state.pair.source_revisions}});state.job=await api(path(''));await loadResults();
  message('viewer-message',decision==='later'?'Saved for later review.':'FPV counterpart decision saved.');
}
$('review-later').onclick=guarded(()=>decideReview('later'),'viewer-message');
$('no-counterpart').onclick=guarded(()=>decideReview(state.job.no_counterpart?.includes(state.pair?.fpv_video)?'restore_counterpart':'no_counterpart'),'viewer-message');
$('review-next').onclick=guarded(async()=>{
  const current=state.pair;
  const next=(state.results?.rows || []).filter(r=>r.alignment_available && r.review_state==='candidate' && !state.job.no_counterpart?.includes(r.fpv_video) && !(r.radio_video===current?.radio_video && r.fpv_video===current?.fpv_video) && (r.alignment_suggested || candidateStrength(r)>=.4))
    .sort((a,b)=>(b.confidence==='strong')-(a.confidence==='strong') || candidateStrength(b)-candidateStrength(a))[0];
  if(next)await openCandidate(next);else message('viewer-message','No remaining review candidates.');
},'viewer-message');
$('preview-acceleration').onchange=()=>{$('playback-proxy').onchange();};
$('export-profile').onchange=()=>{if($('export-profile').value==='copy')$('export-fps').value='original';};
$('export-radio-framing').onchange=()=>{if($('export-radio-framing').value==='stabilized'){if($('export-profile').value==='copy')$('export-profile').value='h264';if($('export-fps').value==='original')$('export-fps').value='30';}};

function saveJSON(name,value){const blob=new Blob([JSON.stringify(value,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
$('open-project').onclick=()=>$('project-file').click();
$('project-file').onchange=guarded(async()=>{
  const file=$('project-file').files[0];if(!file)return;
  const response=await fetch('/api/projects/open',{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});
  const job=await response.json();if(!response.ok)throw new Error(job.detail || 'Could not open project');
  await history();await selectJob(job.id);message('session-message','Project opened as a separate session. Relink both source folders to verify recordings and reuse fingerprints.');$('project-file').value='';
},'session-message');
$('show-backups').onclick=guarded(async()=>{
  if(!state.job)throw new Error('Select a session first.');const token=state.jobToken,jobId=state.job.id;
  const data=await api(path('/backups'));if(token!==state.jobToken)return;
  $('session-backup-list').replaceChildren(...data.backups.map(backup=>{
    const row=el('div',undefined,'actions');row.append(el('span',new Date(backup.created*1000).toLocaleString(),'hint'),action('Restore this metadata snapshot',guarded(async()=>{
      await api(`/api/jobs/${jobId}/backups/restore`,{method:'POST',json:{name:backup.name}});if(token===state.jobToken){await selectJob(jobId);message('session-message','Metadata restored. Previous JSON and completed exports were preserved.');}
    },'session-message')));return row;
  }));if(!data.backups.length)message('session-message','No metadata snapshots yet. The app keeps up to ten automatically.');
},'session-message');
$('installation-report').onclick=guarded(async()=>{
  const report=await api('/api/diagnostics'+(state.job?'?job_id='+state.job.id:''));$('installation-details').textContent=JSON.stringify(report,null,2);show('installation-details');saveJSON('flight-matcher-diagnostics.json',report);
},'session-message');
const referenceValidation={timer:null,task:null,report:null,token:0};
$('reference-save').onclick=guarded(async()=>{
  if(!state.pair)return;const pair=state.pair;
  await api(path('/references'),{method:'PUT',json:{radio_video:pair.radio_video,fpv_video:pair.fpv_video,match:$('reference-match').value==='yes',expected_offset:$('reference-offset').value===''?null:Number($('reference-offset').value)}});
  message('reference-status','Independent reference label saved. It does not change pairing or the saved alignment.');
},'reference-status');
$('reference-run').onclick=guarded(async()=>{
  const job=state.job.id,token=state.jobToken,task=await api(path('/validate-audio'),{method:'POST',json:{}});if(token!==state.jobToken)return;
  referenceValidation.task=task.id;const run=++referenceValidation.token;show('reference-cancel');
  const poll=async()=>{if(run!==referenceValidation.token||token!==state.jobToken)return;
    try{const result=await api(`/api/jobs/${job}/motion-review/${task.id}`);if(token!==state.jobToken)return;
      message('reference-status',result.progress?.message || result.message,result.status==='failed');
      if(['queued','running'].includes(result.status)){referenceValidation.timer=setTimeout(poll,1500);return;}
      show('reference-cancel',false);if(result.status==='complete'){referenceValidation.report=result;show('reference-download');message('reference-status',Object.entries(result.counts).map(([k,v])=>`${v} ${k.replaceAll('_',' ')}`).join(' · ')+'. '+result.note);}
    }catch(error){message('reference-status',error.message,true);}
  };poll();
},'reference-status');
$('reference-cancel').onclick=guarded(()=>api(path(`/motion-review/${referenceValidation.task}/cancel`),{method:'POST'}),'reference-status');
$('reference-download').onclick=()=>{if(referenceValidation.report)saveJSON('audio-reference-report.json',referenceValidation.report);};

function flightDocument() {
  const data=state.results || {}, records=[];
  if(state.flightData?.results===data && state.flightData?.job===state.job) return state.flightData.doc;
  for(const kind of ['fpv','radio']) for(const record of state.job?.inputs?.[kind] || []) {
    const clip=data.clips?.[kind]?.find(c=>c.name===record.name);
    records.push({id:`${kind}:${record.name}`,name:record.name,kind:kind==='radio'?'stick':kind,
      metadata:record.metadata?.duration ? record.metadata : clip,missing:clip?.source_usable===false});
  }
  const doc={id:state.job?.id,videos:records,flights:data.flights || [],pairs:(data.rows || []).filter(row=>row.alignment_available).map(row=>({
    id:row.pair_id || pairKey(row.radio_video,row.fpv_video),stick:`radio:${row.radio_video}`,fpv:`fpv:${row.fpv_video}`,
    radio_video:row.radio_video,fpv_video:row.fpv_video,offset:row.alignment_offset_seconds,confirmed:row.reviewed,
    method:row.timing_only?'timestamp':'content',row}))};
  state.flightData={results:data,job:state.job,doc};return doc;
}
function pairKey(radio,fpv) { return `${radio}::${fpv}`; }
installFlightView({getDoc:flightDocument,fmt:time,escape:value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  isSimple:()=>false,notice:(text)=>message('viewer-message',text),clearReview:pausePair,
  loadPreview:(video,record,initialTime,current)=>loadVideo(video,record.kind==='stick'?'radio':'fpv',record.name,current,'flight-preview-status',false,initialTime),
  openPair:async(id)=>{const p=flightDocument().pairs.find(p=>p.id===id);if(p)await openCandidate(p.row);},
  exportPairs:async(ids)=>requestExport(flightDocument().pairs.filter(p=>ids.includes(p.id)).map(p=>({radio_video:p.radio_video,fpv_video:p.fpv_video,offset_seconds:p.offset})))
});

function renderPairRhythm(data) {
  show('pair-rhythm-details',!!data);$('pair-rhythm-sections').replaceChildren();if(!data)return;
  message('pair-rhythm-summary',`${data.method==='gimbal_activity'?'Tip-free gimbal movement. ':''}${data.reason} Score ${data.score.toFixed(3)} · ${data.sections.matched}/${data.sections.usable} separate 3s sections · ${data.radio_events} StickCam and ${data.fpv_events} FPV bursts. Timing peak margin ${data.peak_margin.toFixed(3)} · candidate separation ${Number(data.candidate_margin || 0).toFixed(3)} · resolution ${data.timing_resolution_seconds}s plus flight-controller response delay. This checks changes, not reliable roll/pitch/yaw labels. Sections below use the rhythm offset ${offsetText(data.offset_seconds)}.`);
  for(const w of data.sections.windows || [])if(w.usable) $('pair-rhythm-sections').append(el('p',`${w.matched?'✓ Matching rhythm':'Different rhythm'} · StickCam ${time(w.radio_start,true)} ↔ FPV ${time(w.fpv_start,true)} · ${w.duration.toFixed(1)}s · correlation ${w.correlation.toFixed(2)}`,w.matched?'rhythm-match':'hint'));
}

$('workflow-gimbal-activity').onchange=()=>{ $('use-gimbal-activity').checked=$('workflow-gimbal-activity').checked;updateEvidenceSummary();};
$('workflow-rhythm').onchange=()=>{if($('workflow-rhythm').checked)$('use-stick-inputs').checked=true;$('use-movement-rhythm').checked=$('workflow-rhythm').checked;updateEvidenceSummary();};
