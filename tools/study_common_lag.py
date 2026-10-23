"""Evaluate one fixed three-channel proxy and lag across every candidate.

Feature choice was discovered on pair 1; other pairs are transfer checks, not
training data. Never fit a different sign or axis mapping to each candidate.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
import numpy as np
from scipy.ndimage import gaussian_filter1d, minimum_filter1d
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from study_content_signals import load, FPS
from fpv_matcher.matching.correlation import weighted_correlations

CHANNELS=(('right_dark_x','z',-1),('right_bright_y','x',1),('left_dark_x','y',-1))


def preprocess(data,keys):
    grid=np.arange(0,data['time'][-1],1/FPS)
    quality=np.interp(grid,data['time'],data.get('quality',np.ones(len(data['time']))),left=0,right=0)
    quality=np.where(quality>=.35,quality,0)
    quality=minimum_filter1d(quality,size=5,mode='constant')
    result=[]
    for key,sign in keys:
        value=np.interp(grid,data['time'],data[key])*sign
        smooth=gaussian_filter1d(value*quality,1)/np.maximum(gaussian_filter1d(quality,1),1e-8)
        slow=gaussian_filter1d(smooth*quality,FPS*2)/np.maximum(gaussian_filter1d(quality,FPS*2),1e-8)
        result.append(smooth-slow)
    return {'time':grid,'values':np.array(result),'quality':quality}


def curve(radio,fpv):
    corr,count=[],[]
    for a,b in zip(radio['values'],fpv['values']):
        c,n=weighted_correlations(a,b,radio['quality'],fpv['quality'],round(8*FPS))
        corr.append(c);count.append(n)
    corr=np.array(corr);valid=np.maximum(np.min(count,axis=0),0)
    # Every axis is fixed and contributes once at the same offset.
    score=np.maximum(corr,0).mean(axis=0)*np.minimum(1,np.sqrt(valid/(30*FPS)))
    score[(corr>.35).sum(axis=0)<2]=0
    offsets=-np.arange(-len(radio['time'])+1,len(fpv['time']))/FPS
    return offsets,score,corr,valid/FPS


def sections(radio,fpv,offset):
    start=max(0,offset);end=min(radio['time'][-1],fpv['time'][-1]+offset)
    windows=[]
    for at in np.arange(start,end,3):
        if end-at<2:break
        mask=(radio['time']>=at)&(radio['time']<min(at+3,end))
        t=radio['time'][mask];q=radio['quality'][mask]*np.interp(t-offset,fpv['time'],fpv['quality'],left=0,right=0)
        valid=q>.1
        values=[]
        for a,b in zip(radio['values'],fpv['values']):
            x=a[mask][valid];y=np.interp(t-offset,fpv['time'],b)[valid]
            usable=valid.sum()>=.7*len(t) and min(np.std(x),np.std(y))>.0001
            values.append(float(np.corrcoef(x,y)[0,1]) if usable else None)
        windows.append({'radio_start':float(at),'fpv_start':float(at-offset),'duration':float(min(3,end-at)),
                        'correlations':values,'matched':sum(v is not None and v>=.6 for v in values)>=2,
                        'usable':sum(v is not None for v in values)>=2})
    return windows


def main(output):
    pairs=json.loads((output/'reference.json').read_text())['pairs']
    results=[];prepared={}
    for number in range(1,len(pairs)+1):
        path=output/f'p{number}-gimbal-activity.npz'
        if not path.exists():continue
        data=load(path)
        alternate=output/f'p{number}-gimbal-activity-alternate.npz'
        if alternate.exists():
            candidate=load(alternate)
            # Independently choose a reference using observed pose coverage,
            # never by match score or the known timing.
            if np.mean(candidate['quality']>=.35)>np.mean(data['quality']>=.35):
                data=candidate;path=alternate
        prepared[number,'radio']=preprocess(data,[(r,1) for r,_,_ in CHANNELS])
        prepared[number,'reference']=path.name
        fpv=load(output/f'p{number}-gyro-features.npz')
        prepared[number,'fpv']=preprocess(fpv,[(f,sign) for _,f,sign in CHANNELS])
    for ri in range(1,len(pairs)+1):
        if (ri,'radio') not in prepared:continue
        for fi in range(1,len(pairs)+1):
            fpv=prepared.get((fi,'fpv'))
            if fpv is None:
                fpv=preprocess(load(output/f'p{fi}-gyro-features.npz'),[(f,sign) for _,f,sign in CHANNELS])
            radio=prepared[ri,'radio'];offsets,score,corr,seconds=curve(radio,fpv)
            index=int(np.argmax(score));best=float(offsets[index]);s=float(score[index])
            row={'radio_pair':ri,'fpv_pair':fi,'reference':prepared[ri,'reference'],'best_offset':best,
                 'score':s,'axis_correlations':corr[:,index].tolist(),'valid_seconds':float(seconds[index]),
                 'sections':sections(radio,fpv,best) if s else []}
            if ri==fi:
                label=pairs[ri-1]['reference_offset'];i=int(np.argmin(abs(offsets-label)))
                row['reference_offset']=label;row['offset_error']=best-label
                row['at_reference']={'score':float(score[i]),'axis_correlations':corr[:,i].tolist(),
                                     'valid_seconds':float(seconds[i]),'sections':sections(radio,fpv,label)}
                if ri==1:
                    rng=np.random.default_rng(26);null=[];size=20
                    usable=(len(radio['time'])//size)*size
                    for _ in range(100):
                        indices=np.arange(usable).reshape(-1,size)[rng.permutation(usable//size)].ravel()
                        shuffled={'time':np.arange(usable)/FPS,'values':radio['values'][:,indices],
                                  'quality':radio['quality'][indices].copy()}
                        for boundary in range(size,usable,size):shuffled['quality'][boundary-2:boundary+2]=0
                        null.append(float(curve(shuffled,fpv)[1].max()))
                    row['shuffled_block_controls']={'count':100,'scores':null,'at_least_observed':sum(v>=s for v in null),
                                                   'note':'Exploratory only; feature selection used pair 1.'}
            results.append(row)
            print(ri,fi,'offset',round(best,3),'score',round(s,3),'axes',np.round(corr[:,index],2).tolist(),flush=True)
    (output/'common-lag.json').write_text(json.dumps({'channels':CHANNELS,'discovery_pair':1,'results':results},indent=2,allow_nan=False)+'\n')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    main(parser.parse_args().output)
