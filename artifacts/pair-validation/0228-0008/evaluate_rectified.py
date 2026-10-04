"""Compare the diagnostic extraction with the existing extraction at equal Hz."""
import json
from dataclasses import asdict
from importlib import import_module

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

s=import_module('artifacts.pair-validation.0228-0008.analyze')
r=json.loads((s.HERE/'results.json').read_text())
old=s.load(s.cache_path(r['radio']['path'],s.DATA/'cache',r['calibration'],30))
new=s.load(s.HERE/'rectified-sticks.npz')
tel=s.load(s.cache_file(r['fpv'],'telemetry',s.VERSION,{}))
offset=r['audio']['best']['offset']
output={'diagnostic_fps':5,'diagnostic_dimension':800,'production_calibration_changed':False,'variants':{}}
for name,data in [('existing',old),('rectified',new)]:
    rp=s.prepare(data,5,True)
    cases=[]
    for tilt in [0,30]:
        fp=s.prepare(s.body_rates(tel,tilt),5,False)
        fixed=s.compare(rp,fp,5,2,(offset-.099,offset+.099))
        sections=s.section_support(rp,fp,5,offset,fixed.details,include_windows=True)
        cases.append({'uptilt':tilt,'at_audio':asdict(fixed),'exact_audio_sections':sections,
            'near_audio':asdict(s.compare(rp,fp,5,2,(offset-.5,offset+.5))),
            'global':asdict(s.compare(rp,fp,5,2))})
    output['variants'][name]={'cases':cases,'usable_fraction':{k:float(np.mean(data[k+'_quality']>.1)) for k in ['roll','pitch','yaw']}}
output['absolute_reference_valid_fraction']=float(np.mean(new['reference_quality']>.1))
(s.HERE/'rectified-results.json').write_text(json.dumps(output,indent=2,allow_nan=False))
plt.style.use('dark_background')
fig,axes=plt.subplots(4,1,figsize=(13,10),layout='constrained')
fig.suptitle('Diagnostic extraction at 5 Hz: original housing flow vs absolute reference\nFPV 00:05.6 aligned with StickCam 00:00.0')
fp=s.prepare(s.body_rates(tel),5,False)
for ax,k in zip(axes[:3],['roll','pitch','yaw']):
    ax.plot(fp['time']+offset,fp['_'+k],color='#f1aa52',lw=.8,label='FPV attitude-derived rates')
    for label,data,color in [('Original',old,'#ec7575'),('Reference rectification',new,'#7bd3ae')]:
        rp=s.prepare(data,5,True);signal=rp['_'+k].copy();signal[rp['_q_'+k]<=.1]=np.nan
        ax.plot(rp['time'],signal,lw=.8,color=color,label=label)
    ax.set_ylabel(k.title()+'\nnormalised');ax.set_ylim(-3.5,3.5)
axes[0].legend(loc='upper right',fontsize=8)
axes[3].plot(new['time'],new['reference_quality'],color='#7bd3ae',lw=.8)
axes[3].set_ylabel('Absolute reference\nquality');axes[3].set_xlabel('StickCam seconds')
for ax in axes:ax.set_xlim(0,237.67);ax.grid(alpha=.12)
fig.savefig(s.HERE/'rectified-comparison.png',dpi=130)
for name,variant in output['variants'].items():
    print(name,'usable',variant['usable_fraction'])
    for case in variant['cases']:
        print('tilt',case['uptilt'],'score',case['at_audio']['score'],'details',case['at_audio']['details'],
              'sections', {k:v for k,v in case['exact_audio_sections'].items() if k!='windows'},
              'best',case['global']['score'],case['global']['offset_seconds'])
print('reference valid',output['absolute_reference_valid_fraction'])
