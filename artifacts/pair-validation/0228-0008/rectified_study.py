"""Diagnostic extraction with an absolute radio reference, without session changes.

Match housing features to the original frame, rectify perspective, then track
tips in that coordinate system. This intentionally costs more than the current
optical-flow path, to isolate accumulated housing drift from other errors.
"""
import json
import time
from importlib import import_module

import cv2
import numpy as np

from fpv_matcher.radio.stick_tracker import GimbalTracker, scaled_config
from fpv_matcher.video.reader import sampled_frames

s = import_module('artifacts.pair-validation.0228-0008.analyze')
cv2.setNumThreads(1)
r = json.loads((s.HERE/'results.json').read_text())
DIMENSION, FPS = 800, 5
config = scaled_config(r['calibration'],DIMENSION/2688)
path = r['radio']['path']
reference = next(sampled_frames(path,FPS,DIMENSION,start=20,duration=1/FPS))[1]
outline = np.float32(config['radio_outline'])
mask=np.zeros(reference.shape,np.uint8)
cv2.fillConvexPoly(mask,np.int32(outline),255)
mask=cv2.erode(mask,np.ones((9,9),np.uint8))
for side in ['left_gimbal','right_gimbal']:
    a=config[side];x,y,w,h=[round(a[k]) for k in ['x','y','width','height']]
    mask[max(0,y):y+h,max(0,x):x+w]=0
sift=cv2.SIFT_create(nfeatures=2500,contrastThreshold=.02)
keys, desc=sift.detectAndCompute(reference,mask)
matcher=cv2.BFMatcher()
trackers=[GimbalTracker(config[side],False) for side in ['left_gimbal','right_gimbal']]
for tracker in trackers:
    tracker.initialize(reference)
rows=[]
began=time.monotonic()
for timestamp,frame in sampled_frames(path,FPS,DIMENSION):
    target_keys,target_desc=sift.detectAndCompute(frame,None)
    transform=None
    quality=0.
    inlier_count=0
    if target_desc is not None and len(target_desc)>=2:
        reverse={a.queryIdx:a.trainIdx for a,b in matcher.knnMatch(target_desc,desc,k=2) if a.distance<.75*b.distance}
        matches=[a for a,b in matcher.knnMatch(desc,target_desc,k=2) if a.distance<.75*b.distance and reverse.get(a.trainIdx)==a.queryIdx]
        if len(matches)>=10:
            source=np.float32([keys[a.queryIdx].pt for a in matches]);target=np.float32([target_keys[a.trainIdx].pt for a in matches])
            h,inliers=cv2.findHomography(source,target,cv2.RANSAC,2.5)
            if h is not None and inliers is not None:
                good=inliers.ravel().astype(bool);mapped=cv2.perspectiveTransform(outline[None],h)[0]
                area=abs(cv2.contourArea(outline));span=cv2.contourArea(cv2.convexHull(source[good]))/area
                inlier_count=int(good.sum())
                valid=(inlier_count>=10 and good.mean()>=.5 and span>=.06 and cv2.isContourConvex(mapped)
                    and np.isfinite(mapped).all() and (mapped>=0).all()
                    and (mapped[:,0]<frame.shape[1]).all() and (mapped[:,1]<frame.shape[0]).all()
                    and .3<abs(cv2.contourArea(mapped))/area<3)
                if valid:
                    transform=h;quality=min(1.,inlier_count/30)*min(1.,span/.15)
    values=[(0.,0.,0.),(0.,0.,0.)]
    if transform is not None:
        rectified=cv2.warpPerspective(frame,transform,reference.shape[::-1],flags=cv2.INTER_LINEAR|cv2.WARP_INVERSE_MAP,borderValue=127)
        values=[tracker.detect(rectified,quality) for tracker in trackers]
    left,right=values
    row={'time':timestamp,'roll':right[0],'pitch':-right[1],'yaw':left[0],'throttle':-left[1],
        'roll_quality':right[2],'pitch_quality':right[2],'yaw_quality':left[2],'throttle_quality':left[2],
        'quality':(left[2]+right[2])/2,'reference_quality':quality,'reference_inliers':inlier_count}
    rows.append(row)
    if len(rows)%100==0:
        print(f'{timestamp:.1f}s / 237.7s, elapsed {time.monotonic()-began:.1f}s, housing quality {quality:.2f}',flush=True)
data={key:np.array([row[key] for row in rows],float) for key in rows[0]}
np.savez_compressed(s.HERE/'rectified-sticks.npz',**data)
print(f'Completed in {time.monotonic()-began:.1f}s',flush=True)
