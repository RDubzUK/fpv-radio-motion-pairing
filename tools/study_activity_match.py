"""Tip-free movement-rhythm experiment with source gaps preserved."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from study_content_signals import load,FPS
from study_common_lag import preprocess,sections
from fpv_matcher.matching.correlation import weighted_correlations


def activity_data(output,n):
    path=output/f'p{n}-gimbal-activity.npz';data=load(path)
    alt=output/f'p{n}-gimbal-activity-alternate.npz'
    if alt.exists():
        candidate=load(alt)
        if np.mean(candidate['quality']>=.35)>np.mean(data['quality']>=.35):data=candidate;path=alt
    data['activity']=(data['left_activity']+data['right_activity'])/2
    data['quality']=data['motion_quality']
    return data,path.name


def search(radio,fpv,reference=None):
    corr,count=weighted_correlations(radio['values'][0],fpv['values'][0],radio['quality'],fpv['quality'],80)
    offsets=-np.arange(-len(radio['time'])+1,len(fpv['time']))/FPS
    scores=np.maximum(corr,0)*np.minimum(1,np.sqrt(np.maximum(count,0)/300))
    index=int(np.argmax(scores));offset=float(offsets[index]);score=float(scores[index])
    rivals=np.abs(offsets-offset)>2
    row={'offset':offset,'score':score,'correlation':float(corr[index]),'seconds':float(count[index]/FPS),
         'peak_margin':score-float(scores[rivals].max()) if rivals.any() else score,
         'sections':sections(radio,fpv,offset) if score else []}
    # sections() accepts any number of axes; single rhythm needs its own threshold.
    for window in row['sections']:
        c=window['correlations'][0];window['matched']=c is not None and c>=.6;window['usable']=c is not None
    if reference is not None:
        index=int(np.argmin(abs(offsets-reference)))
        row['reference_offset']=reference;row['error']=offset-reference
        row['at_reference']={'correlation':float(corr[index]),'score':float(scores[index]),'seconds':float(count[index]/FPS)}
    return row


def main(output):
    pairs=json.loads((output/'reference.json').read_text())['pairs'];result=[]
    for ri in range(1,len(pairs)+1):
        source,reference=activity_data(output,ri)
        radio=preprocess(source,[('activity',1)])
        control=preprocess(source,[('control_activity',1)])
        for fi in range(1,len(pairs)+1):
            fpv=preprocess(load(output/f'p{fi}-gyro-features.npz'),[('changes',1)])
            row=search(radio,fpv,pairs[ri-1]['reference_offset'] if ri==fi else None)
            row.update(radio_pair=ri,fpv_pair=fi,reference=reference,
                       shell_control=search(control,fpv,pairs[ri-1]['reference_offset'] if ri==fi else None))
            result.append(row)
            print(ri,fi,'offset',row['offset'],'score',round(row['score'],3),'r',round(row['correlation'],3),flush=True)
    (output/'activity-matches.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    main(parser.parse_args().output)
