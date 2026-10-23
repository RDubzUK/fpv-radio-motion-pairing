"""Search for repeated agreeing sections, rather than one full-trace peak."""
from __future__ import annotations

import numpy as np
from fpv_matcher.matching.correlation import weighted_correlations
from fpv_matcher.matching.activity import sections

VERSION="independent-three-second-window-votes-v2"


def compare(radio,fpv,min_overlap=2.):
    fps=10.;step=30;na,nb=len(radio["time"]),len(fpv["time"])
    if min(na,nb)<step:
        raise ValueError("Three-second window matching needs at least three seconds per source")
    size=na+nb-1;matched=np.zeros(size);usable=np.zeros(size);sum_corr=np.zeros(size)
    first=np.full(size,np.inf);last=np.full(size,-np.inf);windows=[]
    def normal(signal):
        values=np.asarray(signal["energy"],float);q=signal["quality"]>.1
        return (values-np.mean(values[q]))/max(float(np.std(values[q])),1e-8) if q.any() else values*0
    a,b=normal(radio),normal(fpv)
    for start in range(0,nb-step+1,step):
        value=b[start:start+step];quality=fpv["quality"][start:start+step]
        if np.count_nonzero(quality>.1)<21 or np.std(value[quality>.1])<1e-4:continue
        corr,count=weighted_correlations(a,value,radio["quality"],quality,21)
        valid=(count>=21-1e-5)&(corr!=0);agree=valid&(corr>=.6)
        indices=np.arange(len(corr))+start
        usable[indices]+=valid;matched[indices]+=agree;sum_corr[indices]+=np.where(agree,corr,0)
        first[indices]=np.where(agree,np.minimum(first[indices],start/fps),first[indices])
        last[indices]=np.where(agree,np.maximum(last[indices],start/fps),last[indices])
        windows.append((start,corr,valid))
    agreement=matched/np.maximum(usable,1)
    finite=np.isfinite(first)&np.isfinite(last);distance=np.zeros(size)
    np.subtract(last,first,out=distance,where=finite)
    spread=np.maximum(0,distance)/max(3.,nb/fps)
    strength=matched*np.sqrt(agreement)*(.5+.5*spread)
    if not np.any(strength>0):
        raise ValueError("No agreeing movement sections were found; no offset has been estimated")
    index=int(np.argmax(strength));offset=(na-1-index)/fps
    rivals=np.abs((na-1-np.arange(size))/fps-offset)>2
    other=float(strength[rivals].max()) if rivals.any() else 0.
    margin=(float(strength[index])-other)/max(float(strength[index]),1e-8)
    sections=[]
    for start,corr,valid in windows:
        at=index-start
        if not 0<=at<len(corr) or not valid[at]:continue
        sections.append({"radio_start":float(offset+start/fps),"fpv_start":float(start/fps),"duration":3.,
                         "usable":True,"matched":bool(corr[at]>=.6),"correlation":float(corr[at])})
    eligible=bool(matched[index]>=6 and agreement[index]>=.3 and margin>=.2 and spread[index]>=.3)
    return {"version":VERSION,"offset_seconds":offset,"score":float(agreement[index]*min(1.,matched[index]/10)),
            "window_strength":float(strength[index]),"peak_margin":max(0.,margin),"valid_seconds":float(usable[index]*3),
            "eligible":eligible,"suggested":False,"sections":{"matched":int(matched[index]),"usable":int(usable[index]),
            "agreement":float(agreement[index]),"spread":float(spread[index]),"windows":sections},
            "reason":"Repeated non-overlapping local sections; independent footage review required."}
