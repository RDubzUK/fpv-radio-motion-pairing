"""Render independently measured content alignment evidence, with source gaps."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from study_content_signals import load
from study_common_lag import preprocess
from study_activity_match import activity_data


def main(output):
    reference=json.loads((output/'reference.json').read_text())
    matches=json.loads((output/'activity-matches.json').read_text())
    extra=json.loads((output/'extra-candidates.json').read_text())
    own=[m for m in matches if m['radio_pair']==m['fpv_pair']]
    summary=[]
    for match in own:
        n=match['radio_pair'];pair=reference['pairs'][n-1]
        rivals=[{'label':reference['pairs'][r['fpv_pair']-1]['sources']['fpv']['name'],**r}
                for r in matches if r['radio_pair']==n and r['fpv_pair']!=n]
        rivals += [{'label':c['name'],**v['activity_rhythm']} for c in extra for v in c.get('comparisons',[])
                   if v['radio_pair']==n and 'activity_rhythm' in v]
        rival=max(rivals,key=lambda r:r['score'])
        windows=match['sections']
        summary.append({'pair':n,'project':pair['project'],'fpv':pair['sources']['fpv']['name'],
                        'stickcam':pair['sources']['radio']['name'],'reference_offset':pair['reference_offset'],
                        'content_offset':match['offset'],'offset_error':match['error'],
                        'score':match['score'],'correlation':match['correlation'],'valid_seconds':match['seconds'],
                        'matched_sections':sum(w['matched'] for w in windows),
                        'usable_sections':sum(w['usable'] for w in windows),
                        'runner_up':{k:rival[k] for k in ('label','score','offset','seconds')},
                        'candidate_margin':match['score']-rival['score'],
                        'rank':1+sum(r['score']>match['score'] for r in rivals),
                        'candidate_recordings':1+len(rivals)})
    (output/'summary.json').write_text(json.dumps(summary,indent=2,allow_nan=False)+'\n')
    plt.rcParams.update({'font.size':10,'axes.facecolor':'#101b2c','figure.facecolor':'#0a1220',
                         'text.color':'#e6edf7','axes.labelcolor':'#e6edf7','xtick.color':'#bbc7d8',
                         'ytick.color':'#bbc7d8','axes.edgecolor':'#41516b','grid.color':'#344057'})
    fig=plt.figure(figsize=(16,12),layout='constrained')
    gs=fig.add_gridspec(5,2,height_ratios=[1.3,1.3,1.3,1.3,1.2],width_ratios=[3,1])
    for row,match in enumerate(own):
        n=match['radio_pair'];source,_=activity_data(output,n)
        radio=preprocess(source,[('activity',1)])
        fpv=preprocess(load(output/f'p{n}-gyro-features.npz'),[('changes',1)])
        ax=fig.add_subplot(gs[row,0]);offset=match['offset']
        start=max(0,-offset);end=min(fpv['time'][-1],radio['time'][-1]-offset)
        for signal,color,label,shift in ((fpv,'#61aafa','FPV angular-rate changes',0),
                                         (radio,'#f1b45d','Stabilised gimbal/finger activity',offset)):
            time=signal['time']-shift;values=signal['values'][0].copy();q=signal['quality']>.1
            visible=q&(time>=start)&(time<=end)
            scale=np.std(values[visible]) if visible.any() else 1
            values/=max(scale,1e-8);values[~q]=np.nan
            ax.plot(time,values,color=color,lw=.8,alpha=.9,label=label)
        for window in match['sections']:
            if window['matched']:ax.axvspan(window['fpv_start'],window['fpv_start']+window['duration'],color='#35d68d',alpha=.12)
        s=summary[row]
        ax.set(title=f"Pair {n} · {reference['pairs'][n-1]['project']} project · content {offset:+.1f}s / saved {match['reference_offset']:+.3f}s",
               xlim=(start,end),ylim=(-3.5,5),ylabel='Normalized changes')
        ax.grid(alpha=.25);ax.set_xlabel('FPV source time (seconds)')
        if row==0:ax.legend(loc='upper left',fontsize=8,ncols=2)
        metrics=fig.add_subplot(gs[row,1]);metrics.axis('off')
        metrics.text(.02,.94,f"{s['matched_sections']}/{s['usable_sections']} usable 3s sections agree\n"
                     f"{s['valid_seconds']:.1f}s of observed support\n"
                     f"Correlation {s['correlation']:.3f}\n"
                     f"Rank {s['rank']}/{s['candidate_recordings']} recordings\n"
                     f"Runner-up margin {s['candidate_margin']:.3f}\n\n"
                     'Gaps are unobserved.\nGreen: local rhythm agreement.\nScores are not probabilities.',va='top',linespacing=1.5)
    ax=fig.add_subplot(gs[4,:]);ax.axis('off')
    ax.text(0,.9,'Exploratory prototype: no audio, file dates, length similarity or saved offsets enter the content searches.\n'
                 'Hand-marked radio geometry is required. References are selected by pose coverage, not match score.\n'
                 '12 candidate recordings were examined per StickCam; one is too short for this study’s minimum 8s of evidence.\n'
                 '10Hz search resolution is not frame-accurate sync. Pair 2 has a close rival; pair 4 has only two supported sections.\n'
                 'Calibration, matching decisions, production caches, source footage and exports were not changed.',va='top',linespacing=1.7)
    fig.suptitle('Content-only flight fingerprints · confirmed-pair study',fontsize=21,fontweight='bold')
    fig.savefig(output/'content-fingerprints.png',dpi=125)
    plt.close(fig)
    print(json.dumps(summary,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    main(parser.parse_args().output)
