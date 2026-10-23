export function installFingerprintReview({$,getJob,getToken,getPair,api,notice,onPreview,onSeek,getPlayhead,fmt,onTaskChange}) {
  let data=null,timer=null,taskId=null,jobToken=null,frame=null,viewToken=0;
  const part=encodeURIComponent;
  const base=suffix=>`/api/jobs/${getJob().id}${suffix}`;
  const guard=fn=>async()=>{try{await fn();}catch(e){notice('fingerprint-message',e.message,true);}};
  const label=value=>value.replaceAll('_',' ');
  function render(){
    const job=getJob();if(!job)return;
    for(const side of ['fpv','radio']){
      const select=$(`fingerprint-${side}`),previous=select.value;
      const signature=JSON.stringify(job.inputs[side].map(r=>r.name));
      if(select.dataset.signature!==signature){
        select.replaceChildren(...job.inputs[side].map(r=>{const o=document.createElement('option');o.value=r.name;o.textContent=`${r.name} · ${r.metadata?.duration?fmt(r.metadata.duration):'duration pending'}`;return o;}));
        if(job.inputs[side].some(r=>r.name===previous))select.value=previous;
        select.dataset.signature=signature;
      }
    }
    const tasks=job.fingerprint_tasks || [], select=$('fingerprint-saved-task'),previous=select.value;
    select.replaceChildren(...tasks.map(t=>{const o=document.createElement('option');o.value=t.id;o.textContent=`${t.kind==='fingerprint-study'?'Confirmed-pair study':t.kind==='motor-search'?'Motor sound search':'Combined rhythm search'} · ${t.status} · ${t.id}`;return o;}));
    if(tasks.some(t=>t.id===previous))select.value=previous;else if(tasks.length)select.value=tasks.at(-1).id;
    $('fingerprint-task-load').disabled=!tasks.length;
    for(const id of ['fingerprint-study-start','fingerprint-motor-search','fingerprint-mixed-search'])$(id).disabled=job.scope_editable===false;
    $('fingerprint-use-pair').disabled=!getPair();
  }
  function reset(){clearTimeout(timer);cancelAnimationFrame(frame);data=null;taskId=null;viewToken++;$('fingerprint-plots').hidden=true;$('fingerprint-results').replaceChildren();$('fingerprint-contributions').replaceChildren();notice('fingerprint-message','Load saved signals or extract reference rhythms.');}
  function usePair(){const pair=getPair();if(!pair)return;for(const side of ['fpv','radio'])$(`fingerprint-${side}`).value=pair[side+'_video'];$('fingerprint-offset').value=pair.offset_seconds;}
  function activeTrace(side){return data?.[side+'_channels']?.[$(`fingerprint-${side}-channel`).value] || data?.[side];}
  function display(value){
    data=value;viewToken++;
    for(const side of ['fpv','radio']){
      $(`fingerprint-${side}`).value=value[side+'_video'];
      const select=$(`fingerprint-${side}-channel`),o=document.createElement('option');o.value='';o.textContent='Combined / matching signal';select.replaceChildren(o);
      for(const key of Object.keys(value[side+'_channels'] || {})){const option=document.createElement('option');option.value=key;option.textContent=label(key);select.append(option);}
    }
    $('fingerprint-offset').value=value.offset_seconds;
    $('fingerprint-plots').hidden=false;
    const start=Math.min(value.radio.points[0]?.[0]??0,(value.fpv.points[0]?.[0]??0)+value.offset_seconds);
    const end=Math.max(value.radio.points.at(-1)?.[0]??1,(value.fpv.points.at(-1)?.[0]??1)+value.offset_seconds);
    $('fingerprint-from').value=Math.max(0,start);$('fingerprint-to').value=end;
    const b=value.best;
    notice('fingerprint-message',`${value.note} Searched offset: ${b.offset_seconds==null?'not estimated':b.offset_seconds.toFixed(3)+'s'} · score ${b.score.toFixed(3)} · ${b.sections.matched}/${b.sections.usable} agreeing 3s sections · usable ${b.valid_seconds.toFixed(1)}s.${value.reference_offset==null?'':` Known reference: ${value.reference_offset.toFixed(3)}s${b.offset_seconds==null?'':`; error ${(b.offset_seconds-value.reference_offset).toFixed(3)}s`}.`}${b.modality_conflicts?.length?' Visual and audio timing disagree; no confident suggestion is made.':''}`);
    $('fingerprint-estimate').disabled=b.offset_seconds==null;
    $('fingerprint-contributions').replaceChildren();
    for(const candidate of value.consensus_candidates || []){
      const line=document.createElement('div');line.className='actions';const text=document.createElement('span'),button=document.createElement('button');
      text.textContent=`Timing alternative ${candidate.offset_seconds.toFixed(3)}s · ${label(candidate.primary.radio_signal)} ↔ ${label(candidate.primary.fpv_signal)} · ${candidate.primary.sections.matched}/${candidate.primary.sections.usable} agreeing sections`;
      button.className='secondary';button.textContent='Show this timing';button.onclick=()=>{$('fingerprint-offset').value=candidate.offset_seconds;$('fingerprint-radio-channel').value=candidate.primary.radio_signal;$('fingerprint-fpv-channel').value=candidate.primary.fpv_signal;draw();};line.append(text,button);$('fingerprint-contributions').append(line);
    }
    for(const check of value.checks || []){
      const row=document.createElement('p');row.className='hint';row.textContent=`${label(check.stickcam_signal)} ↔ ${label(check.fpv_signal)}: full-trace peak ${check.best_offset==null?'not estimated':check.best_offset.toFixed(3)+'s'}${check.offset_error==null?'':` (error ${check.offset_error.toFixed(3)}s)`}${check.window_offset==null?'':` · repeated-section offset ${check.window_offset.toFixed(3)}s · ${check.window_matched}/${check.window_usable} sections${check.window_offset_error==null?'':` · reference error ${check.window_offset_error.toFixed(3)}s`}`}${check.offset_error==null?'':` · ${check.matched_sections_at_reference}/${check.usable_sections_at_reference} sections at reference`}`;$('fingerprint-contributions').append(row);
    }
    for(const issue of value.issues || []){const p=document.createElement('p');p.textContent=issue;p.className='hint';$('fingerprint-contributions').append(p);}
    if(value.tracking_audit){
      const audit=value.tracking_audit,p=document.createElement('p');p.className='hint';p.textContent=`Prepared observations: ${Object.entries(audit.prepared_coverage_percent).map(([key,percent])=>`${label(key)} ${percent}%`).join(' · ')}${audit.outline_observed_percent==null?'':` · radio outline ${audit.outline_observed_percent}%`}`;$('fingerprint-contributions').append(p);
      for(const text of audit.recommendations){const p=document.createElement('p');p.className='hint';p.textContent=text;$('fingerprint-contributions').append(p);}
    }
    draw();
  }
  async function loadCached(){
    const token=getToken(),r=$('fingerprint-radio').value,f=$('fingerprint-fpv').value;
    if(!r||!f)throw new Error('Choose one FPV and one StickCam recording.');
    const query=new URLSearchParams({radio_video:r,fpv_video:f,method:$('fingerprint-method').value,low_hz:$('fingerprint-low').value,high_hz:$('fingerprint-high').value});
    const pair=getPair();
    if(data?.radio_video===r&&data?.fpv_video===f)query.set('offset',$('fingerprint-offset').value);
    else if(pair?.radio_video===r&&pair?.fpv_video===f)query.set('offset',pair.offset_seconds);
    notice('fingerprint-message','Reading saved matching signals; no video derivation is started.');
    const result=await api(base('/fingerprints?')+query);if(token===getToken())display(result);
  }
  async function loadTask(id){
    clearTimeout(timer);taskId=id;const token=getToken();jobToken=token;
    const result=await api(base(`/motion-review/${part(id)}`));if(token!==getToken()||taskId!==id)return;
    $('fingerprint-progress').hidden=!['queued','running','cancelling'].includes(result.status);
    $('fingerprint-progress').value=result.progress?.percent || 0;
    $('fingerprint-cancel').hidden=!['queued','running'].includes(result.status);
    notice('fingerprint-message',result.progress?.message || result.message,result.status==='failed');
    if(['queued','running','cancelling'].includes(result.status)){timer=setTimeout(()=>guard(()=>loadTask(id))(),2500);return;}
    const current=await api(base(''));if(token!==getToken()||taskId!==id)return;
    Object.assign(getJob(),current);render();onTaskChange?.();
    if(result.status!=='complete')return;
    $('fingerprint-results').replaceChildren();
    for(const row of result.rows || []){
      const line=document.createElement('div');line.className='actions';const text=document.createElement('span');
      text.textContent=`${row.radio_video} ↔ ${row.fpv_video} · ${row.available?`score ${row.best.score.toFixed(3)} · offset ${row.best.offset_seconds.toFixed(3)}s · ${row.suggested?'experimental suggestion':'exploratory; not a confident match'}`:row.issues.join('; ')}`;line.append(text);
      if(row.available){const button=document.createElement('button');button.className='secondary';button.textContent='Show rhythms & contributions';button.onclick=guard(async()=>{const token=getToken(),view=await api(base(`/fingerprint-study/${part(id)}/${row.index}`));if(token===getToken())display(view);});line.append(button);}
      $('fingerprint-results').append(line);
    }
    notice('fingerprint-message',`${result.rows?.length || 0} comparison(s) saved · ${result.elapsed_seconds.toFixed(1)}s processing. ${result.note}`);
  }
  async function start(kind){
    const token=getToken(),body={kind,motor_band:[Number($('fingerprint-low').value),Number($('fingerprint-high').value)]};
    if(kind!=='fingerprint-study'){body.fpv_video=$('fingerprint-fpv').value;if(!$('fingerprint-all-radio').checked)body.radio_names=[$('fingerprint-radio').value];}
    const task=await api(base('/fingerprint-study'),{method:'POST',json:body});if(token!==getToken())return;
    const job=getJob();job.scope_editable=false;job.fingerprint_tasks ||= [];if(!job.fingerprint_tasks.some(t=>t.id===task.id))job.fingerprint_tasks.push({id:task.id,kind,status:task.status});render();onTaskChange?.();await loadTask(task.id);
  }
  function draw(){
    if(!data || $('fingerprint-plots').hidden)return;
    const from=Number($('fingerprint-from').value),to=Number($('fingerprint-to').value),offset=Number($('fingerprint-offset').value);
    if(!Number.isFinite(from+to+offset)||to<=from)return;
    const selectedR=$('fingerprint-radio-channel').value,selectedF=$('fingerprint-fpv-channel').value;
    const check=data.checks?.find(c=>c.stickcam_signal===selectedR&&c.fpv_signal===selectedF);
    const alternative=data.consensus_candidates?.find(c=>Math.abs(c.offset_seconds-offset)<.001&&c.primary.radio_signal===selectedR&&c.primary.fpv_signal===selectedF);
    const windows=alternative?.primary.sections.windows || (check && Math.abs((check.display_offset??Infinity)-offset)<.001 ? check.windows : (!selectedR&&!selectedF&&Math.abs(data.offset_seconds-offset)<.001?data.sections.windows:[]));
    for(const side of ['fpv','radio']){
      const canvas=$(`fingerprint-${side}-canvas`),rect=canvas.getBoundingClientRect(),ratio=devicePixelRatio||1;
      if(!rect.width)continue;
      canvas.width=rect.width*ratio;canvas.height=200*ratio;const ctx=canvas.getContext('2d');ctx.scale(ratio,ratio);
      const left=48,right=rect.width-12,top=12,bottom=175,x=t=>left+(t-from)/(to-from)*(right-left),y=v=>bottom-(Math.max(-4,Math.min(4,v))+4)/8*(bottom-top);
      ctx.save();ctx.beginPath();ctx.rect(left,top,right-left,bottom-top);ctx.clip();
      for(const w of windows || [])if(w.matched){ctx.fillStyle='#65e6a030';ctx.fillRect(x(w.radio_start),top,x(w.radio_start+w.duration)-x(w.radio_start),bottom-top);}
      ctx.strokeStyle='#555';ctx.beginPath();ctx.moveTo(left,y(0));ctx.lineTo(right,y(0));ctx.stroke();
      ctx.strokeStyle=side==='fpv'?'#49a7f2':'#ffca6b';ctx.lineWidth=1.25;ctx.beginPath();let open=false;
      for(const [t,value] of activeTrace(side)?.points || []){const at=t+(side==='fpv'?offset:0);if(value==null){open=false;continue;}if(open)ctx.lineTo(x(at),y(value));else ctx.moveTo(x(at),y(value));open=true;}ctx.stroke();
      const cursor=getPlayhead(data.radio_video,data.fpv_video);
      if(cursor!=null){ctx.strokeStyle='#fff';ctx.beginPath();ctx.moveTo(x(cursor),top);ctx.lineTo(x(cursor),bottom);ctx.stroke();}
      ctx.restore();ctx.fillStyle='#aaa';ctx.font='11px system-ui';ctx.fillText('+4σ',4,top+8);ctx.fillText('0',20,y(0));ctx.fillText('−4σ',4,bottom);
      for(let i=0;i<=5;i++){const t=from+(to-from)*i/5;ctx.fillText(fmt(t),Math.min(x(t),right-35),195);}
    }
  }
  function animate(){draw();if(getPair() && !$('radio-preview').paused)frame=requestAnimationFrame(animate);}
  for(const side of ['fpv','radio'])$(`fingerprint-${side}-canvas`).onclick=e=>{if(!data)return;const rect=e.currentTarget.getBoundingClientRect(),start=Number($('fingerprint-from').value),end=Number($('fingerprint-to').value);const at=start+(e.clientX-rect.left-48)/(rect.width-60)*(end-start);onSeek(data.radio_video,data.fpv_video,Math.max(start,Math.min(end,at)));draw();};
  for(const id of ['fingerprint-from','fingerprint-to','fingerprint-offset','fingerprint-radio-channel','fingerprint-fpv-channel'])$(id).onchange=draw;
  $('radio-preview').addEventListener('play',()=>{cancelAnimationFrame(frame);animate();});$('radio-preview').addEventListener('timeupdate',draw);$('radio-preview').addEventListener('seeked',draw);window.addEventListener('resize',draw);
  $('fingerprint-load').onclick=guard(loadCached);$('fingerprint-use-pair').onclick=()=>{usePair();};
  for(const id of ['fingerprint-radio','fingerprint-fpv','fingerprint-method'])$(id).onchange=()=>{data=null;viewToken++;$('fingerprint-plots').hidden=true;notice('fingerprint-message','Choose Show saved rhythms to inspect this source and method.');};
  $('fingerprint-study-start').onclick=guard(()=>start('fingerprint-study'));$('fingerprint-motor-search').onclick=guard(()=>start('motor-search'));
  $('fingerprint-mixed-search').onclick=guard(()=>start('cross-modal-search'));
  $('fingerprint-task-load').onclick=guard(()=>loadTask($('fingerprint-saved-task').value));
  $('fingerprint-cancel').onclick=guard(async()=>{if(taskId)await api(base(`/motion-review/${part(taskId)}/cancel`),{method:'POST'});if(taskId)await loadTask(taskId);});
  $('fingerprint-preview').onclick=guard(async()=>{if(data)await onPreview(data.radio_video,data.fpv_video,Number($('fingerprint-offset').value),data.source_signatures);});
  $('fingerprint-estimate').onclick=()=>{if(data){$('fingerprint-offset').value=data.best.offset_seconds;draw();}};
  $('fingerprint-download').onclick=()=>{if(!data)return;const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'})),a=document.createElement('a');a.href=url;a.download='movement-rhythm-comparison.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
  return {render,reset,usePair,draw,loadTask};
}
