"""Cheap generic foreground activity, explicitly distinct from stick tracking."""
from __future__ import annotations

import time
from pathlib import Path
from functools import lru_cache
from zipfile import BadZipFile
import cv2
import numpy as np
from fpv_matcher.cache.fingerprints import get_or_extract,_key
from fpv_matcher.video.reader import sampled_frames
from fpv_matcher.video.metadata import probe_video

VERSION="scene-residual-affine-activity-v1"
CONFIG={"fps":10,"width":320}


def cache_path(path,cache,identity=None):
    return Path(cache)/f"scene-activity.{_key(str(path),VERSION,CONFIG,identity)}.npz"


@lru_cache(maxsize=256)
def _status(filename,modified,size):
    with np.load(filename,allow_pickle=False) as data:
        validate(data)
        if str(data["cache_key"].item())!=Path(filename).stem.split(".",1)[1]:
            raise ValueError("Scene activity identity differs")
        return {"status":"cached","sample_fps":10,"samples":len(data["time"]),
                "usable_percent":round(float(np.mean(data["quality"]>.1))*100,1),
                "extraction_seconds":float(data["extraction_seconds"])}


def cache_status(path,cache,identity=None):
    try:
        target=cache_path(path,cache,identity)
        if not target.is_file():return {"status":"not_derived","sample_fps":10}
        stat=target.stat();return dict(_status(str(target),stat.st_mtime_ns,stat.st_size))
    except (OSError,ValueError,KeyError,EOFError,BadZipFile):
        return {"status":"unavailable"}


def validate(data):
    t=np.asarray(data["time"],float)
    if len(t)<3 or not np.isfinite(t).all() or np.any(np.diff(t)<=0):
        raise ValueError("Insufficient generic scene activity")
    for key in ("activity","quality","camera_activity"):
        a=np.asarray(data[key],float)
        if a.shape!=t.shape or not np.isfinite(a).all():
            raise ValueError("Invalid scene activity cache")


def cached_scene(path,cache,identity=None,progress=None):
    return get_or_extract(str(path),cache,"scene-activity",VERSION,CONFIG,
                          lambda:extract(path,progress),identity,validate=validate)


def extract(path,progress=None):
    info=probe_video(path);previous=None;rows=[];started=time.monotonic()
    for at,frame in sampled_frames(path,10,320):
        gray=frame if frame.ndim==2 else cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
        activity,camera,quality=0.,0.,0.
        if previous is not None:
            points=cv2.goodFeaturesToTrack(previous,100,.02,8)
            if points is not None and len(points)>=8:
                moved,status,_=cv2.calcOpticalFlowPyrLK(previous,gray,points,None)
                good=status.ravel()>0 if status is not None else np.zeros(len(points),bool)
                if good.sum()>=8:
                    affine,inliers=cv2.estimateAffinePartial2D(points[good],moved[good],method=cv2.RANSAC,ransacReprojThreshold=2)
                    if affine is not None and inliers is not None and inliers.sum()>=6:
                        h,w=gray.shape
                        warped=cv2.warpAffine(previous,affine,(w,h))
                        valid=cv2.warpAffine(np.ones_like(previous),affine,(w,h))>0
                        valid[:5]=False;valid[-5:]=False;valid[:,:5]=False;valid[:,-5:]=False
                        if valid.mean()>.7:
                            delta=(gray.astype(float)-warped.astype(float))[valid]
                            delta-=np.median(delta)
                            activity=float(np.quantile(np.abs(delta),.85)/255)
                            camera=float(np.linalg.norm(affine[:,2])/max(w,h))
                            quality=float(min(1.,inliers.sum()/max(12,len(points)))*valid.mean())
        rows.append((at,activity,quality,camera));previous=gray
        if progress and len(rows)%50==0:
            progress({"percent":at/max(info["duration"],.1)*100,"message":"Reading generic scene activity"})
    if len(rows)<3:
        raise ValueError("Not enough video frames for scene activity")
    a=np.asarray(rows)
    return {**{k:a[:,i] for i,k in enumerate(("time","activity","quality","camera_activity"))},
            "extraction_seconds":np.asarray(time.monotonic()-started)}
