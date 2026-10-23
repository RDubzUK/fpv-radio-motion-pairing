"""Experimental motor-band activity. No RPM, attitude or drone identity claim."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
from scipy.ndimage import minimum_filter1d

from fpv_matcher.cache.fingerprints import _key,get_or_extract
from fpv_matcher.matching import activity

VERSION = "motor-band-flux-tonality-v1"
SAMPLE_RATE,HOP,FRAME = 16000,1600,2048


def band(value=None):
    value = value or [150,6000]
    if not isinstance(value,(list,tuple)) or len(value)!=2:
        raise ValueError("Choose lower and upper motor-band frequencies")
    low,high = map(float,value)
    if not np.isfinite([low,high]).all() or not 50<=low<high<=7500 or high-low<200:
        raise ValueError("Motor band must span at least 200 Hz, within 50–7500 Hz")
    return [low,high]


def config(value=None):
    return {"band_hz":band(value),"sample_rate":SAMPLE_RATE,"frame":FRAME,"hop":HOP}


def cache_path(path,cache,value=None,identity=None):
    return Path(cache)/f"motor-audio.{_key(str(path),VERSION,config(value),identity)}.npz"


def validate(data):
    t = np.asarray(data["time"],float)
    if t.ndim!=1 or len(t)<3 or not np.isfinite(t).all() or np.any(t<0) or np.any(np.diff(t)<=0):
        raise ValueError("Motor audio has insufficient ordered samples")
    for k in ("frequency_hz","flux","band_energy","quality"):
        a=np.asarray(data[k],float)
        if a.shape!=t.shape or not np.isfinite(a).all():
            raise ValueError("Motor audio cache is incomplete")
    if np.any((data["quality"]<0)|(data["quality"]>1)):
        raise ValueError("Invalid motor audio confidence")


def cached_motor(path,cache,value=None,identity=None,progress=None):
    return get_or_extract(str(path),cache,"motor-audio",VERSION,config(value),
                          lambda:extract(path,value,progress),identity,validate=validate)


def extract(path,value=None,progress=None):
    from fpv_matcher.video.metadata import probe_video
    info=probe_video(path)
    if not info["has_audio"]:
        raise ValueError("This recording has no audio")
    low,high=band(value)
    result=subprocess.run([shutil.which("ffprobe") or "ffprobe","-v","error","-show_entries","stream=codec_type,start_time","-of","json",str(path)],capture_output=True,check=True,text=True)
    streams=json.loads(result.stdout)["streams"]
    def start(kind):
        return float(next((s.get("start_time",0) for s in streams if s.get("codec_type")==kind),0) or 0)
    audio_shift=start("audio")-start("video")
    proc=subprocess.Popen([shutil.which("ffmpeg") or "ffmpeg","-nostdin","-v","error","-i",str(path),"-map","0:a:0","-vn","-ac","1","-ar",str(SAMPLE_RATE),"-f","f32le","pipe:1"],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    frequency=np.fft.rfftfreq(FRAME,1/SAMPLE_RATE)
    mask=(frequency>=low)&(frequency<=high)
    selected_frequency=frequency[mask]
    tail=np.empty(0,np.float32); position=0; rows=[]; previous=None
    started=time.monotonic()
    try:
        while True:
            raw=proc.stdout.read(SAMPLE_RATE*4*5)
            if not raw:
                break
            samples=np.r_[tail,np.frombuffer(raw,dtype="<f4")]
            if len(samples)<FRAME:
                tail=samples;continue
            windows=np.lib.stride_tricks.sliding_window_view(samples,FRAME)[::HOP]
            spectrum=np.abs(np.fft.rfft(windows*np.hanning(FRAME),axis=1))[:,mask]
            power=spectrum**2; energy=np.mean(power,axis=1)
            normal=spectrum/np.maximum(spectrum.sum(axis=1,keepdims=True),1e-10)
            peak=np.argmax(power,axis=1)
            contrast=np.max(power,axis=1)/np.maximum(np.median(power,axis=1),1e-12)
            quality=np.clip((np.log1p(contrast)-2)/4,0,1)
            quality[(energy<1e-8)|(np.mean(np.abs(windows)>=.99,axis=1)>.01)]=0
            for i in range(len(windows)):
                timestamp=audio_shift+(position+i*HOP+FRAME/2)/SAMPLE_RATE
                flux=float(np.abs(normal[i]-previous).sum()) if previous is not None else 0.
                previous=normal[i]
                if timestamp>=0:
                    rows.append((timestamp,selected_frequency[peak[i]],flux,np.log1p(energy[i]),quality[i]))
            consumed=len(windows)*HOP
            position+=consumed;tail=samples[consumed:]
            if progress:
                progress({"percent":min(99.,position/SAMPLE_RATE/max(info["duration"],.1)*100),"message":"Reading motor-band dynamics","elapsed_seconds":round(time.monotonic()-started,1)})
        if proc.wait(timeout=30):
            raise ValueError("Audio decoding failed")
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.terminate();proc.wait(timeout=10)
    if len(rows)<3:
        raise ValueError("Not enough audio samples for motor dynamics")
    a=np.asarray(rows,float)
    return {**{k:a[:,i] for i,k in enumerate(("time","frequency_hz","flux","band_energy","quality"))},
            "band_hz":np.asarray([low,high]),"audio_video_shift_seconds":np.asarray(audio_shift),
            "extraction_seconds":np.asarray(time.monotonic()-started)}


def prepare(data):
    validate(data)
    freq=np.maximum(data["frequency_hz"],1)
    q=np.asarray(data["quality"],float).copy()
    # Peak switching between harmonics is not observed motor acceleration.
    jumps=np.r_[True,np.abs(np.diff(np.log(freq)))>.4]
    q[jumps]=0
    q=minimum_filter1d(q,size=3,mode="constant",cval=0)
    changes=np.abs(np.gradient(np.log(freq),data["time"]))
    signal=activity.prepare_signal({"time":data["time"],"quality":q,
                                   "activity":np.log1p(10*changes+5*data["flux"])},"activity")
    return signal
