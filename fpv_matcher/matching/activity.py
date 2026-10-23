"""Tip-free movement fingerprints: proposals only, with one shared offset."""
from __future__ import annotations

import numpy as np
from scipy.ndimage import gaussian_filter1d, minimum_filter1d
from scipy.signal import find_peaks

from .correlation import weighted_correlations
from .signals import resample, standardized

VERSION, FPS = "gimbal-angular-change-rhythm-v1", 10.


def prepare_signal(data, key):
    sampled = resample({"time": data["time"], key: data[key], "quality": data["quality"]}, FPS)
    t, values = sampled["time"], sampled[key]
    q = np.asarray(sampled["quality"], float)
    q = minimum_filter1d(np.where(q > .1, q, 0), size=5, mode="constant", cval=0)
    if len(t) < 3:
        return {"time": t, "energy": np.zeros(len(t)), "quality": np.zeros(len(t)), "fps": FPS, "event_times": []}
    smooth = gaussian_filter1d(values*q, 1)/np.maximum(gaussian_filter1d(q, 1), 1e-8)
    slow = gaussian_filter1d(smooth*q, FPS*2)/np.maximum(gaussian_filter1d(q, FPS*2), 1e-8)
    energy = smooth-slow
    energy[q <= .1] = 0
    peaks, _ = find_peaks(standardized(energy, q), height=.6, prominence=.5, distance=4)
    return {"time": t, "energy": energy, "quality": q, "fps": FPS,
            "event_times": t[peaks[q[peaks] > .1]].tolist()}


def prepare_fpv(data):
    times = np.asarray(data["time"], float)
    if not bool(data.get("_body_rates", False)) or len(times) < 4:
        return prepare_signal({"time": times, "changes": np.zeros(len(times)), "quality": np.zeros(len(times))}, "changes")
    vectors = np.column_stack([data[key] for key in ("roll", "pitch", "yaw")])
    q = np.minimum.reduce([np.asarray(data.get(key+"_quality", data["quality"]), float)
                           for key in ("roll", "pitch", "yaw")])
    q = np.where(np.isfinite(vectors).all(axis=1) & (q > .1), q, 0)
    dt = float(np.median(np.diff(times)))
    if not np.isfinite(dt) or dt <= 0 or not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
        raise ValueError("FPV movement needs finite, ordered source timestamps")
    gap = np.r_[False, np.diff(times) > max(.05, dt*5)] | np.r_[np.diff(times) > max(.05, dt*5), False]
    q[gap] = 0
    sigma = max(.5, .06/max(dt, 1e-6))
    weights = gaussian_filter1d(q, sigma)
    smooth = gaussian_filter1d(np.nan_to_num(vectors)*q[:, None], sigma, axis=0)/np.maximum(weights[:, None], 1e-8)
    changes = np.log1p(np.linalg.norm(np.gradient(smooth, times, axis=0), axis=1))
    q = minimum_filter1d(q, size=max(3, int(np.ceil(.12/max(dt, 1e-6)))|1), mode="constant", cval=0)
    # Differentiating across an actual timestamp gap is not an observation.
    return prepare_signal({"time": times, "changes": changes, "quality": q}, "changes")


def sections(radio, fpv, offset):
    rt, ft = radio["time"], fpv["time"]
    windows = []
    if len(rt) and len(ft):
        start, end = max(rt[0], ft[0]+offset), min(rt[-1], ft[-1]+offset)
        for a in np.arange(start, end, 3.):
            b = min(end, a+3)
            if b-a < 2:
                break
            mask = (rt >= a)&(rt < b)
            t = rt[mask]
            q = radio["quality"][mask]*np.interp(t-offset, ft, fpv["quality"], left=0, right=0)
            good = q > .01
            x, y = radio["energy"][mask][good], np.interp(t-offset, ft, fpv["energy"])[good]
            usable = bool(good.sum() >= .7*len(t) and len(x) > 2 and min(np.std(x), np.std(y)) > 1e-4)
            corr = float(np.corrcoef(x, y)[0, 1]) if usable else 0.
            windows.append({"radio_start": float(a), "fpv_start": float(a-offset), "duration": float(b-a),
                            "usable": usable, "matched": usable and corr >= .6, "correlation": corr})
    good = [w for w in windows if w["matched"]]
    count = sum(w["usable"] for w in windows)
    spread = (good[-1]["radio_start"]-good[0]["radio_start"])/max(3., end-start) if good else 0.
    return {"matched": len(good), "usable": count, "agreement": len(good)/count if count else 0.,
            "spread": float(spread), "windows": windows}


def compare(radio, fpv, min_overlap=2.):
    empty = {"version": VERSION, "method": "gimbal_activity", "score": 0., "offset_seconds": 0.,
             "peak_margin": 0., "valid_seconds": 0., "radio_events": 0, "fpv_events": 0,
             "sections": {"matched": 0, "usable": 0, "agreement": 0., "spread": 0., "windows": []},
             "eligible": False, "suggested": False, "timing_resolution_seconds": 1/FPS,
             "reason": "Not enough observed gimbal/FPV movement; lost radio poses remain gaps."}
    na, nb = len(radio["time"]), len(fpv["time"])
    if min(na, nb) < 4:
        return empty
    corr, count = weighted_correlations(radio["energy"], fpv["energy"], radio["quality"], fpv["quality"],
                                        round(max(8., min_overlap)*FPS))
    count = np.maximum(count, 0)
    scores = np.maximum(corr, 0)*np.minimum(1, np.sqrt(count/(30*FPS)))
    offsets = -np.arange(-na+1, nb)/FPS
    index = int(np.argmax(scores))
    if scores[index] <= 0:
        return empty
    offset, score = float(offsets[index]), float(scores[index])
    rivals = np.abs(offsets-offset) > 2
    margin = score-float(scores[rivals].max()) if rivals.any() else score
    local = sections(radio, fpv, offset)
    eligible = bool(score >= .35 and margin >= .04 and local["matched"] >= 2
                    and local["agreement"] >= .5 and local["spread"] >= .2)
    start, end = max(0., offset), min(radio["time"][-1], fpv["time"][-1]+offset)
    return {**empty, "score": score, "offset_seconds": offset, "peak_margin": max(0., margin),
            "correlation": float(corr[index]), "valid_seconds": float(count[index]/FPS), "sections": local,
            "radio_events": int(sum(start <= t <= end for t in radio["event_times"])),
            "fpv_events": int(sum(start <= t+offset <= end for t in fpv["event_times"])), "eligible": eligible,
            "reason": ("Tip-free movement timing needs visible-event review and refined sync."
                       if eligible else "Movement is weak, repetitive or insufficiently supported across separate sections.")}
