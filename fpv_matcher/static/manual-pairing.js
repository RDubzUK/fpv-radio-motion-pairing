// Independent source browsing is intentionally separate from synchronized playback.
// Marking source events proposes an offset; only explicit confirmation teaches clocks.
import {releasePreview, setPreviewPlaybackIntent} from './preview-player.js?v=4';

export function installManualPairing({$, getJob, getToken, api, loadVideo, fmt, onPreview, onLabel, pauseOther}) {
  const sides = {fpv:{token:0},radio:{token:0}};
  let recorderName = '', recorderDirty = false, saving = false;
  const linked={playing:false,buffering:false,starting:false,key:'',frame:null,point:null,lastDisplay:0};
  const label = kind => kind === 'fpv' ? 'FPV' : 'StickCam';
  const notice = (id,text,error=false) => { $(id).textContent=text;$(id).classList.toggle('error',error); };
  const guarded = callback => async () => {try{await callback();}catch(error){if(error.name!=='AbortError')notice('manual-message',error.message,true);}};
  const selected = kind => $(`manual-${kind}-clip`).value;
  const video = kind => $(`manual-${kind}-video`);
  const record = kind => getJob()?.inputs[kind].find(r=>r.name===selected(kind));
  function pause(){linked.playing=false;linked.buffering=false;cancelAnimationFrame(linked.frame);for(const kind of ['fpv','radio']){video(kind).pause();video(kind).playbackRate=1;setPreviewPlaybackIntent(video(kind),false);}refresh();}
  function clearSide(kind) {
    if(linked.playing)pause();linked.key='';
    sides[kind].token++; sides[kind].name=null;sides[kind].info=null;sides[kind].loading=false;
    const player=video(kind);releasePreview(player);player.pause();player.removeAttribute('src');player.load();
    $(`manual-${kind}-event`).value='';$(`manual-${kind}-time`).value='0';$(`manual-${kind}-mark`).disabled=true;
  }
  function reset(){pause();for(const kind of ['fpv','radio']){clearSide(kind);$(`manual-${kind}-clip`).replaceChildren();delete $(`manual-${kind}-clip`).dataset.options;}recorderName='';recorderDirty=false;saving=false;notice('manual-message','');}
  function events() {
    const result={};
    for(const kind of ['fpv','radio']){
      const raw=$(`manual-${kind}-event`).value.trim(),at=Number(raw),duration=sides[kind].info?.duration;
      if(!raw || !Number.isFinite(at) || at<0 || !Number.isFinite(duration) || at>=duration)return null;
      result[kind]=at;
    }
    return result;
  }
  function refresh(){
    const point=events(),loading=Object.values(sides).some(s=>s.loading);
    $('manual-preview').disabled=!point || loading || saving;
    $('manual-play-both').disabled=!point || loading || saving;
    $('manual-play-both').textContent=linked.playing?'Pause both':'Play both';
    $('manual-load').disabled=loading || saving || !selected('fpv') || !selected('radio');
    $('manual-recorder-save').disabled=saving || !selected('fpv');
    notice('manual-offset',point?`StickCam − FPV offset: ${(point.radio-point.fpv)>=0?'+':''}${(point.radio-point.fpv).toFixed(3)}s`:'Mark the same event in both feeds');
    for(const kind of ['fpv','radio']){
      const ready=sides[kind].name===selected(kind) && !!sides[kind].info && !sides[kind].loading;
      for(const action of ['mark','back','forward','seek','frame-back','frame-forward'])$(`manual-${kind}-${action}`).disabled=!ready;
    }
  }
  function render(){
    const job=getJob();if(!job)return;
    for(const kind of ['fpv','radio']){
      const select=$(`manual-${kind}-clip`),previous=select.value;
      const signature=JSON.stringify(job.inputs[kind].map(r=>[r.name,r.metadata?.duration,r.fpv_recorder]));
      if(select.dataset.options!==signature){
        select.replaceChildren(...job.inputs[kind].map(r=>{const option=document.createElement('option');option.value=r.name;option.textContent=`${r.name} · ${r.metadata?.duration?fmt(r.metadata.duration):'duration pending'}${r.fpv_recorder?' · '+r.fpv_recorder:''}`;return option;}));
        if(job.inputs[kind].some(r=>r.name===previous))select.value=previous;
        select.dataset.options=signature;
      }
      const item=record(kind),modified=item?.camera_modified_time ?? (item?.original_modified_ms!=null?item.original_modified_ms/1000:item?.metadata_signature?.[1]/1e9);
      notice(`manual-${kind}-details`,`${item?.metadata?.duration?fmt(item.metadata.duration):'Duration pending'} · modified ${Number.isFinite(modified)?new Date(modified*1000).toLocaleString():'not read'}${kind==='fpv'?` · recorder: ${item?.fpv_recorder || 'Unassigned'}`:''}`);
    }
    if(recorderName!==selected('fpv')){recorderName=selected('fpv');recorderDirty=false;}
    if(!recorderDirty)$('manual-recorder').value=record('fpv')?.fpv_recorder || '';
    const groups=[...new Set(job.inputs.fpv.map(r=>r.fpv_recorder).filter(Boolean))].sort();
    $('fpv-recorder-options').replaceChildren(...groups.map(name=>{const option=document.createElement('option');option.value=name;return option;}));
    refresh();
  }
  async function loadSide(kind){
    const job=getJob(),name=selected(kind),jobToken=getToken();if(!job||!name)return;
    if(sides[kind].name===name && video(kind).readyState && !sides[kind].loading)return;
    clearSide(kind);const token=++sides[kind].token;sides[kind].name=name;sides[kind].loading=true;
    const current=()=>token===sides[kind].token && jobToken===getToken() && selected(kind)===name;
    refresh();notice(`manual-${kind}-status`,'Loading metadata and a playable preview…');
    try{
      const info=await api(`/api/jobs/${job.id}/metadata/${kind}/${encodeURIComponent(name)}`);if(!current())return;
      sides[kind].info=info;const item=record(kind);if(item)item.metadata=info;
      await loadVideo(video(kind),kind,name,current,`manual-${kind}-status`,$('manual-fast-preview').checked,0);
      if(!current())return;
      const player=video(kind);player.currentTime=0;
      $(`manual-${kind}-time`).max=$(`manual-${kind}-event`).max=Math.max(0,info.duration-1/(info.fps||30));
      notice(`manual-${kind}-status`,`${label(kind)} ready · play and seek independently. Pause at the shared event and mark it.`);
    }catch(error){if(current()){sides[kind].info=null;notice(`manual-${kind}-status`,error.message,true);}}
    finally{if(current()){sides[kind].loading=false;render();}}
  }
  async function loadBoth(){pauseOther();await Promise.all(['fpv','radio'].map(loadSide));}
  function seek(kind,at){
    const info=sides[kind].info;if(!info)return;
    if(linked.key){pause();linked.key='';}
    const player=video(kind);player.pause();player.currentTime=Math.max(0,Math.min(Number(at)||0,info.duration-1/(info.fps||30)));
    $(`manual-${kind}-time`).value=player.currentTime.toFixed(3);
  }
  for(const kind of ['fpv','radio']){
    $(`manual-${kind}-clip`).onchange=guarded(async()=>{clearSide(kind);render();await loadSide(kind);});
    for(const [action,delta] of [['previous',-1],['next',1]])$(`manual-${kind}-${action}`).onclick=guarded(async()=>{
      const select=$(`manual-${kind}-clip`),count=select.options.length;if(!count)return;
      select.selectedIndex=(select.selectedIndex+delta+count)%count;clearSide(kind);render();await loadSide(kind);
    });
    for(const [action,delta] of [['back',-5],['forward',5],['frame-back',-1],['frame-forward',1]])$(`manual-${kind}-${action}`).onclick=()=>seek(kind,video(kind).currentTime+(action.startsWith('frame')?delta/(sides[kind].info?.fps||30):delta));
    $(`manual-${kind}-seek`).onclick=()=>seek(kind,$(`manual-${kind}-time`).value);
    $(`manual-${kind}-time`).onchange=()=>seek(kind,$(`manual-${kind}-time`).value);
    $(`manual-${kind}-mark`).onclick=()=>{if(linked.key){pause();linked.key='';}video(kind).pause();$(`manual-${kind}-event`).value=video(kind).currentTime.toFixed(3);refresh();};
    $(`manual-${kind}-event`).oninput=()=>{if(linked.key){pause();linked.key='';}refresh();};
    video(kind).addEventListener('timeupdate',()=>{if(document.activeElement!==$(`manual-${kind}-time`))$(`manual-${kind}-time`).value=video(kind).currentTime.toFixed(3);});
    video(kind).addEventListener('play',()=>{pauseOther();setPreviewPlaybackIntent(video(kind),true);});
    video(kind).addEventListener('pause',()=>{if(linked.playing&&!linked.buffering&&!linked.starting)pause();else setPreviewPlaybackIntent(video(kind),linked.playing);});
    video(kind).addEventListener('ended',()=>{if(linked.playing)pause();});
  }
  function buffered(player){
    for(let i=0;i<player.buffered.length;i++)if(player.buffered.start(i)<=player.currentTime+.03&&player.buffered.end(i)>player.currentTime)return player.buffered.end(i)-player.currentTime;
    return 0;
  }
  function tickBoth(){
    if(!linked.playing)return;
    const fpv=video('fpv'),radio=video('radio'),point=linked.point;
    const end=Math.min(sides.fpv.info.duration-point.fpv,sides.radio.info.duration-point.radio);
    if(fpv.currentTime-point.fpv>=end-.03 || radio.ended || fpv.ended){pause();notice('manual-message','Reached the end of the shared footage after the marked event. Play both starts again from the marks.');return;}
    const target=point.radio+fpv.currentTime-point.fpv,delta=radio.currentTime-target;
    if(!radio.seeking && Math.abs(delta)>.1)radio.currentTime=Math.max(0,Math.min(target,sides.radio.info.duration-.01));
    const ready=[fpv,radio].every(player=>player.readyState>=3&&!player.seeking&&buffered(player)>Math.min(.06,Math.max(.001,player.duration-player.currentTime-.005)));
    if(!ready){
      linked.buffering=true;fpv.pause();radio.pause();for(const kind of ['fpv','radio'])setPreviewPlaybackIntent(video(kind),true);
    }else{
      linked.buffering=false;radio.playbackRate=Math.abs(delta)>.02?(delta>0?.97:1.03):1;
      if((fpv.paused||radio.paused)&&!linked.starting){
        linked.starting=true;const key=linked.key;
        Promise.all([fpv.play(),radio.play()]).catch(error=>{if(linked.playing&&key===linked.key&&error.name!=='AbortError'){pause();notice('manual-message',`Could not play both: ${error.message}`,true);}}).finally(()=>{linked.starting=false;});
      }
    }
    if(Date.now()-linked.lastDisplay>200){
      linked.lastDisplay=Date.now();notice('manual-message',`${linked.buffering?'Buffering both feeds…':'Playing from the marked shared event'} · FPV ${fmt(fpv.currentTime,true)} ↔ StickCam ${fmt(radio.currentTime,true)}. Pause to inspect or amend the marks. This preview does not confirm a match.`);
    }
    linked.frame=requestAnimationFrame(tickBoth);
  }
  $('manual-play-both').onclick=guarded(async()=>{
    if(linked.playing){pause();notice('manual-message','Both feeds paused. Play both resumes from here; changing a mark or seeking independently restarts from the marks.');return;}
    const point=events();if(!point)throw new Error('Mark the same visible event inside each recording first.');
    pauseOther();const key=JSON.stringify([selected('fpv'),selected('radio'),point]);
    const end=Math.min(sides.fpv.info.duration-point.fpv,sides.radio.info.duration-point.radio);
    if(linked.key!==key || video('fpv').currentTime-point.fpv>=end-.1 || video('fpv').currentTime<point.fpv-.05){
      pause();video('fpv').currentTime=point.fpv;video('radio').currentTime=point.radio;
    }
    linked.key=key;linked.point=point;linked.playing=true;linked.buffering=true;
    for(const kind of ['fpv','radio'])setPreviewPlaybackIntent(video(kind),true);
    refresh();tickBoth();
  });
  $('manual-load').onclick=guarded(loadBoth);
  $('manual-fast-preview').onchange=guarded(async()=>{
    const jobToken=getToken(),marks=Object.fromEntries(['fpv','radio'].map(kind=>[kind,{name:selected(kind),revision:sides[kind].info?.source_revision,event:$(`manual-${kind}-event`).value}]));
    for(const kind of ['fpv','radio'])clearSide(kind);await loadBoth();if(jobToken!==getToken())return;
    for(const kind of ['fpv','radio'])if(marks[kind].name===selected(kind)&&marks[kind].revision===sides[kind].info?.source_revision)$(`manual-${kind}-event`).value=marks[kind].event;refresh();
  });
  $('manual-layout').onchange=()=>$('manual-video-grid').classList.toggle('stacked',$('manual-layout').value==='stacked');
  $('manual-recorder').oninput=()=>{recorderDirty=true;};
  $('manual-recorder-save').onclick=guarded(async()=>{
    const name=selected('fpv'),jobToken=getToken();saving=true;refresh();
    try{await onLabel([name],$('manual-recorder').value.trim());if(jobToken!==getToken())return;recorderDirty=false;render();notice('manual-message','Recorder label saved. Label its other FPV files in Video lists & match scope.');}
    finally{saving=false;refresh();}
  });
  $('manual-preview').onclick=guarded(async()=>{
    const point=events();if(!point)throw new Error('Load both recordings and mark one shared event inside each clip.');
    pause();saving=true;refresh();
    try{await onPreview(selected('radio'),selected('fpv'),point.radio-point.fpv,{recorder:$('manual-recorder').value.trim(),
      sync_points:[{radio_time:point.radio,fpv_time:point.fpv}],
      source_revisions:{radio:sides.radio.info.source_revision,fpv:sides.fpv.info.source_revision}});}
    finally{saving=false;refresh();}
  });
  $('manual-pair-panel').addEventListener('toggle',()=>{if($('manual-pair-panel').open){render();loadBoth().catch(error=>notice('manual-message',error.message,true));}else pause();});
  window.addEventListener('pagehide',pause);
  return {render,reset,pause};
}
