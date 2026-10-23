"""Content-only searches against an evenly spaced, unconfirmed FPV sample.

Other clips can be split parts of a true flight; they are competing candidates,
not automatically labelled negatives. Selection uses list index, not dates.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
import numpy as np
from scipy.ndimage import gaussian_filter1d
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from study_content_signals import load,FPS
from study_common_lag import preprocess,curve,CHANNELS
from fpv_matcher.motion.telemetry import cached_telemetry
from study_activity_match import activity_data,search


def main(output,session):
    job=json.loads(session.read_text())
    reference=json.loads((output/'reference.json').read_text())['pairs']
    excluded={p['sources']['fpv']['name'] for p in reference}
    records=job['inputs']['fpv']
    candidates=[records[i] for i in np.linspace(0,len(records)-1,8).round().astype(int)
                if records[i]['name'] not in excluded]
    results=[]
    for index,record in enumerate(candidates):
        path=output/f'extra-{index}-gyro-features.npz'
        print('Candidate',index,record['name'],flush=True)
        item={'name':record['name'],'path':record['path'],'metadata':record['metadata'],
              'selection':'8 evenly spaced source list indices; no dates or saved offsets', 'comparisons':[]}
        try:
            stat=Path(record['path']).stat();item['signature']=[stat.st_size,stat.st_mtime_ns]
            if not path.exists() or 'changes' not in load(path):
                data=cached_telemetry(record['path'],output/'cache',record.get('content_id'))
                t=data['time'];v=np.column_stack([data[k] for k in ('x','y','z')])
                fs=min(2000.,round(1/np.median(np.diff(t))))
                grid=np.arange(max(0,t[0]),min(t[-1],record['metadata']['duration']),1/fs)
                v=np.column_stack([np.interp(grid,t,v[:,i]) for i in range(3)])
                smooth=gaussian_filter1d(v,fs*.06,axis=0)
                times=np.arange(grid[0]+.128,grid[-1]-.128,1/FPS)
                changes=np.log1p(np.linalg.norm(np.gradient(smooth,1/fs,axis=0),axis=1))
                features={'time':times,'changes':np.interp(times,grid,changes),
                          **{key:np.interp(times,grid,smooth[:,i]) for i,key in enumerate(('x','y','z'))}}
                np.savez_compressed(path,**features)
            features=load(path)
            if len(features['time'])<4:
                raise ValueError('Too short for this experimental fingerprint')
            fpv=preprocess(features,[(f,sign) for _,f,sign in CHANNELS])
            for ri in range(1,len(reference)+1):
                gpath=output/f'p{ri}-gimbal-activity.npz'
                alternate=output/f'p{ri}-gimbal-activity-alternate.npz'
                radio=load(gpath)
                if alternate.exists():
                    alt=load(alternate)
                    if np.mean(alt['quality']>=.35)>np.mean(radio['quality']>=.35):radio=alt
                radio=preprocess(radio,[(r,1) for r,_,_ in CHANNELS])
                offsets,scores,correlations,seconds=curve(radio,fpv)
                best=int(np.argmax(scores))
                item['comparisons'].append({'radio_pair':ri,'score':float(scores[best]),'offset':float(offsets[best]),
                                            'axis_correlations':correlations[:,best].tolist(),'valid_seconds':float(seconds[best])})
                activity,_=activity_data(output,ri)
                rhythm=search(preprocess(activity,[('activity',1)]),preprocess(features,[('changes',1)]))
                item['comparisons'][-1]['activity_rhythm']=rhythm
        except (OSError,ValueError,RuntimeError) as exc:
            item['unavailable']=str(exc)
        results.append(item)
        (output/'extra-candidates.json').write_text(json.dumps(results,indent=2,allow_nan=False)+'\n')
        print(json.dumps(item),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    parser.add_argument('--session',type=Path,required=True);args=parser.parse_args()
    main(args.output,args.session)
