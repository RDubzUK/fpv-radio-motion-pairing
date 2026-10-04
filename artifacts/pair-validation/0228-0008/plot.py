"""Render measured evidence at the audio offset, including disagreeing motion."""
import json
from collections import defaultdict
from importlib import import_module
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon
import numpy as np

s = import_module('artifacts.pair-validation.0228-0008.analyze')
result = json.loads((s.HERE/'results.json').read_text())
ra, fa = s.audio(result['radio']), s.audio(result['fpv'])
offset = result['audio']['best']['offset']
sticks = s.load(s.cache_path(result['radio']['path'],s.DATA/'cache',result['calibration'],30))
telemetry = s.load(s.cache_file(result['fpv'],'telemetry',s.VERSION,{}))
rp, fp = s.prepare(sticks,30,True), s.prepare(s.body_rates(telemetry),30,False)
index = defaultdict(list)
for h,t in zip(fa['hashes'],fa['hash_times']):
    index[int(h)].append(float(t))
votes = [(float(rt),float(rt)-ft,int(h)) for h,rt in zip(ra['hashes'],ra['hash_times']) for ft in index[int(h)]
         if abs(float(rt)-ft-offset)<.15]
result['audio']['vote_offsets'] = {}
for label, begin, end in [('start',0,30),('end',207.67,237.67)]:
    values = np.array([v[1] for v in votes if begin <= v[0] < end])
    result['audio']['vote_offsets'][label] = {'median':float(np.median(values)),
        'p10':float(np.percentile(values,10)), 'p90':float(np.percentile(values,90))}

plt.style.use('dark_background')
fig, axes = plt.subplots(5,1,figsize=(13,11),layout='constrained')
fig.suptitle('Known flight pair: StickCam 0228 / FPV 0008\nAudio aligns FPV 00:05.6 with StickCam 00:00.0',fontsize=16)
ax = axes[0]
bins = np.arange(0,240,3)
counts = [len({h for t,_,h in votes if a<=t<b}) for a,b in zip(bins[:-1],bins[1:])]
ax.bar(bins[:-1],counts,width=2.7,color='#6adc8b')
ax.axvspan(30,207.67,color='gray',alpha=.2)
ax.text(115,120,'Middle audio not analysed',ha='center',color='silver')
ax.set_ylabel('Distinct matching\naudio hashes / 3s')
ax.set_title('15 supported three-second audio sections, across both clip boundaries')
for axis, ax in zip(['roll','pitch','yaw'],axes[1:4]):
    ax.plot(fp['time']+offset,fp['_'+axis],color='#f1aa52',lw=.7,label='FPV embedded attitude → angular rates')
    ax.plot(rp['time'],rp['_'+axis],color='#7bc9ec',lw=.7,alpha=.85,label='Detected StickCam input')
    ax.set_ylabel(axis.title()+'\nnormalised')
    ax.set_ylim(-4,4)
axes[1].legend(loc='upper right',fontsize=8)
support=result['motion']['sections_at_audio_offset']
axes[1].set_title(f'Motion does not corroborate this offset: {support["correlated_sections"]} / {support["usable_sections"]} usable sections; extraction needs correction')
ax=axes[-1]
ax.plot(sticks['time'],sticks['radio_orientation_quality'],label='Reported outline quality',color='#c692eb')
corners=np.array([[sticks[f'outline_{n}_{a}'] for a in ['x','y']] for n in range(4)]).transpose(2,0,1)
outside = ((corners[:,:,0]<0)|(corners[:,:,0]>2688)|(corners[:,:,1]<0)|(corners[:,:,1]>2016)).any(axis=1)
ax.fill_between(sticks['time'],0,1,where=outside,color='#ef6c6c',alpha=.25,label='Estimated outline extends beyond frame')
ax.set_ylabel('Outline quality')
ax.legend(loc='upper right',fontsize=8)
result['motion']['outline_diagnostics']={'outside_frame_fraction':float(np.mean(outside)),
    'outside_with_high_quality_fraction':float(np.mean(outside & (sticks['radio_orientation_quality']>=.5)))}
for ax in axes:
    ax.set_xlim(0,237.67);ax.grid(alpha=.12)
axes[-1].set_xlabel('StickCam time (seconds); FPV time = this time + 5.6s')
fig.savefig(s.HERE/'evidence.png',dpi=150)
plt.close(fig)

fig, axes=plt.subplots(1,2,figsize=(13,5),layout='constrained')
original=np.array(result['calibration']['radio_outline'])
for ax, sec, name in zip(axes,[20,120],['reference.png','frame-120.png']):
    image=plt.imread(s.HERE/name);ax.imshow(image)
    i=np.argmin(np.abs(sticks['time']-sec));poly=corners[i]*1008/2688
    ax.add_patch(Polygon(poly,fill=False,color='#ef6c6c',lw=2))
    ax.set_title(f'{sec}s: saved outline estimate; quality {sticks["radio_orientation_quality"][i]:.2f}')
    ax.set_xlim(0,1008);ax.set_ylim(756,0);ax.axis('off')
fig.suptitle('Tracking diagnostic: the outline drifts away from the visible radio')
fig.savefig(s.HERE/'tracking-diagnostic.png',dpi=140)
plt.close(fig)
(s.HERE/'results.json').write_text(json.dumps(result,indent=2,allow_nan=False))
print(json.dumps({'vote_offsets':result['audio']['vote_offsets'], 'outline':result['motion']['outline_diagnostics']}))
