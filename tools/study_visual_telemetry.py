"""Check FPV image-derived rates against embedded attitude on the same source."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
import cv2
import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.spatial.transform import Rotation
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fpv_matcher.motion.lucas_kanade import LucasKanadeEstimator
from fpv_matcher.motion.visual_rotation import gap_summary


def main(output):
    cv2.setNumThreads(1)
    pair = json.loads((output/'reference.json').read_text())['pairs'][0]
    target=output/'p1-fpv-visual-study.npz'
    if target.exists():
        with np.load(target,allow_pickle=False) as data: visual=dict(data)
    else:
        visual=LucasKanadeEstimator(max_dimension=480,max_corners=300,replenish_below=100).extract(
            pair['sources']['fpv']['path'],30,camera_profile={'horizontal_fov_deg':110,'projection':'rectilinear','track_full_rate':False})
        np.savez_compressed(target,**visual)
    with np.load(output/'p1-fpv-telemetry.npz',allow_pickle=False) as data: telemetry=dict(data)
    t=visual['time']; observed=np.column_stack([visual[k] for k in ('x','y','z')])
    q=visual['quality']>.1
    results={'valid_fraction':float(q.mean()),'gap_reasons':gap_summary(visual),'conventions':[]}
    rot=Rotation.from_quat(np.column_stack([telemetry[k] for k in ('qx','qy','qz','qw')]))
    dt=np.diff(telemetry['time'])
    variants={'local':np.degrees((rot[:-1].inv()*rot[1:]).as_rotvec())/dt[:,None],
              'inverse_local':-np.degrees((rot[1:]*rot[:-1].inv()).as_rotvec())/dt[:,None]}
    for convention,vector in variants.items():
        smooth=gaussian_filter1d(vector,40,axis=0)
        value=np.column_stack([np.interp(t,telemetry['time'][:-1],smooth[:,i]) for i in range(3)])
        keep=q&np.isfinite(value).all(axis=1)&(np.linalg.norm(value,axis=1)>15)&(np.linalg.norm(observed,axis=1)>15)
        train=keep&(t<8); test=keep&(t>=8)
        if min(train.sum(),test.sum())<8: continue
        transform,loss=Rotation.align_vectors(observed[train],value[train])
        prediction=transform.apply(value)
        corr=[float(np.corrcoef(prediction[test,i],observed[test,i])[0,1]) for i in range(3)]
        results['conventions'].append({'convention':convention,'train_samples':int(train.sum()),'test_samples':int(test.sum()),
                    'rotation':transform.as_matrix().tolist(),'test_axis_correlations':corr,
                    'test_vector_relative_error':float(np.linalg.norm(prediction[test]-observed[test])/np.linalg.norm(observed[test]))})
    (output/'visual-telemetry.json').write_text(json.dumps(results,indent=2,allow_nan=False)+'\n')
    print(json.dumps(results),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    main(parser.parse_args().output)
