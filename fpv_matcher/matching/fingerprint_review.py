"""Read the exact cached movement features used by the experimental matchers."""
from __future__ import annotations

from pathlib import Path
import numpy as np

from fpv_matcher.cache.fingerprints import _key
from fpv_matcher.matching import activity, rhythm
from fpv_matcher.motion.telemetry import VERSION as TELEMETRY_VERSION, body_rates


def read_cache(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError("Matching data is not saved at these settings. Prepare the data or run matching first.")
    with np.load(path,allow_pickle=False) as saved:
        if str(saved["cache_key"].item()) != path.stem.split(".",1)[1]:
            raise ValueError("Saved fingerprint identity differs; prepare it again")
        return {k:saved[k] for k in saved.files if k!="cache_key"}


def fpv_motion(record,cache,settings,embedded_only=False):
    path,identity = record["path"],record.get("identity")
    target = Path(cache)/f"telemetry.{_key(path,TELEMETRY_VERSION,{},identity)}.npz"
    if target.is_file():
        data = read_cache(target)
        if "unavailable" not in data:
            return body_rates(data,settings.get("camera_uptilt_deg",0),settings.get("imu_orientation","zXy"),settings.get("gyro_video_offset",0)),str(data["label"].item())
    if embedded_only:
        raise ValueError("No supported embedded FPV motion is saved. Check embedded motion first.")
    from fpv_matcher.motion.derivation import cache_path, _validate
    from fpv_matcher.motion.sampling import visual_fps
    from fpv_matcher.motion.visual_rotation import visual_body_rates
    fps = visual_fps(settings.get("fpv_sampling_mode","auto"),settings.get("fpv_visual_fps",30),record.get("metadata",{}).get("fps",30))
    data = read_cache(cache_path(path,cache,fps,identity,settings.get("visual_camera")))
    _validate(data)
    return visual_body_rates(data,settings.get("camera_uptilt_deg",0)),"Experimental visual angular rates"


def signals(radio,fpv,calibration,cache,settings,method):
    fdata,label = fpv_motion(fpv,cache,settings,settings.get("motion_source")=="gyro")
    if method == "scene_activity":
        from fpv_matcher.radio.scene_activity import cache_path,validate
        from fpv_matcher.matching import window_rhythm
        data=read_cache(cache_path(radio["path"],cache,radio.get("identity")));validate(data)
        return activity.prepare_signal(data,"activity"),activity.prepare_fpv(fdata),label,window_rhythm
    if method == "gimbal_activity":
        from fpv_matcher.radio.activity import cache_path,validate
        data = read_cache(cache_path(radio["path"],cache,calibration,radio.get("identity")))
        validate(data)
        return activity.prepare_signal(data,"activity"),activity.prepare_fpv(fdata),label,activity
    if method == "rhythm":
        from fpv_matcher.radio.derivation import cache_path,_validate
        from fpv_matcher.radio.sampling import tracking_fps
        from fpv_matcher.radio.modes import remap_cached_inputs
        fps = tracking_fps(settings.get("sample_fps",30),settings.get("stick_sampling_ratio"),radio.get("metadata",{}).get("fps"))
        data = read_cache(cache_path(radio["path"],cache,calibration,fps,radio.get("identity")))
        _validate(data)
        data = remap_cached_inputs(data,calibration.get("stick_mode",2),calibration.get("signs"))
        return rhythm.prepare(data,radio=True),rhythm.prepare(fdata),label,rhythm
    if method == "motor_activity":
        from fpv_matcher.audio.motor import cache_path,prepare,validate
        data = read_cache(cache_path(radio["path"],cache,settings.get("motor_band"),radio.get("identity")))
        validate(data)
        return prepare(data),activity.prepare_fpv(fdata),label,activity
    raise ValueError("Choose gimbal movement, derived-input rhythm or motor sound")


def trace(signal):
    times,values,q = (np.asarray(signal[k],float) for k in ("time","energy","quality"))
    good = q > .1
    mean = float(np.average(values[good],weights=q[good])) if good.any() else 0.
    scale = float(np.sqrt(np.average((values[good]-mean)**2,weights=q[good]))) if good.any() else 1.
    # Keep every 10 Hz matching sample, including explicit missing observations.
    points = [[round(float(t),4),round(float((v-mean)/max(scale,1e-6)),5) if valid>.1 else None,round(float(valid),3)]
              for t,v,valid in zip(times,values,q)]
    return {"points":points,"event_times":signal["event_times"],"sample_fps":signal["fps"],
            "usable_seconds":round(float(good.sum()/signal["fps"]),2),"units":"normalised activity (standard deviations)"}


def document(radio,fpv,label,algorithm,offset=None):
    best = algorithm.compare(radio,fpv)
    if best["score"]<=0:
        if offset is None:
            raise ValueError("Saved signals contain no comparable rhythm; no offset has been estimated")
        best={**best,"offset_seconds":None}
    offset = best["offset_seconds"] if offset is None else float(offset)
    local = algorithm.sections(radio,fpv,offset)
    return {"radio":trace(radio),"fpv":trace(fpv),"fpv_label":label,"offset_seconds":offset,
            "best":best,"sections":local,"automatic_confirmation":False,
            "note":"These are the matcher’s cached activity signals, not pitch/roll/yaw positions. Gaps are excluded. Green marks local agreement, not proof of a flight."}
