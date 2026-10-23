"""Rank every source pairing and lag; reference labels are used only in scoring."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
import numpy as np
from scipy.ndimage import gaussian_filter1d, minimum_filter1d
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from study_content_signals import load, scalar_search, FPS
from fpv_matcher.audio.fingerprint import extract_audio, landmark_match, best_offset
from fpv_matcher.cache.fingerprints import get_or_extract
from fpv_matcher.audio.fingerprint import ALGORITHM_VERSION


def gimbal_signals(output,number):
    data=load(output/f'p{number}-gimbal-activity.npz')
    data['activity']=(data['left_activity']+data['right_activity'])/2
    data['difference']=(data['left_difference']+data['right_difference'])/2
    data['activity_quality']=data['difference_quality']=data['motion_quality']
    for color in ('bright','dark'):
        values=[data[f'right_{color}_{axis}'] for axis in ('x','y')]
        data[color+'_demand']=np.hypot(*values)
    return data


def evaluate(output):
    pairs=json.loads((output/'reference.json').read_text())['pairs']
    results=[]
    fixed=[('activity','changes'),('difference','changes'),('right_activity','changes'),
           ('right_difference','changes'),('bright_demand','speed'),('dark_demand','speed'),
           ('control_activity','changes'),('control_difference','changes')]
    for ri in range(1,len(pairs)+1):
        if not (output/f'p{ri}-gimbal-activity.npz').exists(): continue
        radio=gimbal_signals(output,ri)
        for fi in range(1,len(pairs)+1):
            fpv=load(output/f'p{fi}-gyro-features.npz')
            reference=pairs[ri-1]['reference_offset'] if ri==fi else None
            signals=[scalar_search(radio,fpv,rkey,fkey,reference) for rkey,fkey in fixed]
            # Same camera basis/sign for all sources and offsets. Report signed
            # correlations, rather than picking an axis reversal for each peak.
            fpv.update(roll=-fpv['z'],pitch=fpv['x'],yaw=-fpv['y'])
            for axis in ('roll','pitch','yaw'):
                for color in ('bright','dark'):
                    side,direction=('left','x') if axis=='yaw' else ('right','x' if axis=='roll' else 'y')
                    key=f'{side}_{color}_{direction}'
                    for sign in (1,-1):
                        label=axis if sign==1 else '-'+axis
                        fpv[label]=fpv[axis]*sign
                        signals.append(scalar_search(radio,fpv,key,label,reference))
            results.append({'radio_pair':ri,'fpv_pair':fi,'signals':signals})
    (output/'gimbal-comparisons.json').write_text(json.dumps(results,indent=2,allow_nan=False)+'\n')
    for item in results:
        if item['radio_pair']!=item['fpv_pair']:continue
        strongest=sorted(item['signals'],key=lambda s:s.get('at_reference',{}).get('correlation',0),reverse=True)[:5]
        print(json.dumps({'pair':item['radio_pair'],'strongest_at_reference':strongest}),flush=True)


def audio_controls(output):
    pairs=json.loads((output/'reference.json').read_text())['pairs']
    signals={}
    for number,pair in enumerate(pairs,1):
        for kind,source in pair['sources'].items():
            if not source['metadata']['has_audio']:continue
            print('Audio fingerprints',number,kind,flush=True)
            signals[number,kind]=get_or_extract(source['path'],output/'cache','audio',ALGORITHM_VERSION,
                {'boundary_seconds':120.},lambda p=source['path']:extract_audio(p,120),source['content_id'])
    controls=[]
    for ri in range(1,len(pairs)+1):
        for fi in range(1,len(pairs)+1):
            if (ri,'radio') not in signals or (fi,'fpv') not in signals:continue
            landmarks=landmark_match(signals[ri,'radio'],signals[fi,'fpv'])
            trend=best_offset(signals[ri,'radio'],signals[fi,'fpv'],2,include_landmarks=False)
            controls.append({'radio_pair':ri,'fpv_pair':fi,'fingerprint':list(landmarks),'trend':list(trend),
                             'reference_offset':pairs[ri-1]['reference_offset'] if ri==fi else None})
    (output/'audio-controls.json').write_text(json.dumps(controls,indent=2,allow_nan=False)+'\n')
    print(json.dumps(controls),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    parser.add_argument('--audio-controls',action='store_true');args=parser.parse_args()
    if args.audio_controls:audio_controls(args.output)
    evaluate(args.output)
