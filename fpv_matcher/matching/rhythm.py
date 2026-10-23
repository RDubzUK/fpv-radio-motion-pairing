"""Coarse, unsigned movement timing for review-only shortlists.

Stick changes are compared with changes in observed angular velocity, not with
the orientation of the horizon. Names/signs of axes are deliberately discarded.
This cannot recover lost radio pose or turn tracking gaps into observations.
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import gaussian_filter1d, minimum_filter1d
from scipy.signal import find_peaks

from .correlation import weighted_correlations
from .signals import resample, standardized

VERSION = "unsigned-movement-rhythm-v1"


def prepare(data, fps=10, radio=False):
    sampled = resample(data, fps)
    time = sampled["time"]
    n = len(time)
    empty = {"time": time, "energy": np.zeros(n), "events": np.zeros(n), "quality": np.zeros(n),
             "event_times": [], "fps": fps, "source": "stick changes" if radio else "angular-rate changes"}
    if n < 4:
        return empty
    body = radio or bool(data.get("_body_rates", False))
    keys = ("roll", "pitch", "yaw", "throttle") if radio else ("roll", "pitch", "yaw") if body else ("rotation", "dx", "dy")
    velocities, qualities = [], []
    for key in keys:
        if key not in sampled:
            continue
        value = np.asarray(sampled[key], float)
        q = np.clip(np.nan_to_num(sampled.get(key + "_quality", sampled.get("quality", np.zeros(n))),
                                 nan=0, posinf=0, neginf=0), 0, 1)
        q = np.where(np.isfinite(value) & (q > .15), q, 0)
        valid = q > .15
        floor = .01 if radio else 1. if body else .0001
        if valid.sum() < 4 or np.std(value[valid]) < floor:
            continue
        # Normalize channels before combining: no rates profile, axis mapping,
        # polarity search, or camera-clock hint can manufacture rhythm support.
        normalized = standardized(value, q)
        smooth = gaussian_filter1d(normalized * q, max(.5, fps * .08)) / np.maximum(
            gaussian_filter1d(q, max(.5, fps * .08)), 1e-8)
        neighbor = minimum_filter1d(q, size=5, mode="constant", cval=0)
        velocities.append(np.where(neighbor > .15, np.gradient(smooth, 1 / fps), 0))
        qualities.append(neighbor)
    if not velocities:
        return empty
    velocities, qualities = np.asarray(velocities), np.asarray(qualities)
    present = qualities > .15
    q = qualities.max(axis=0)
    # Changing which controls are visible must not create an activity burst.
    changes = np.any(present[:, 1:] != present[:, :-1], axis=0)
    stable = minimum_filter1d(np.r_[False, ~changes].astype(float), size=5, mode="constant", cval=0)
    q *= stable
    magnitude = np.sqrt(np.sum(velocities ** 2, axis=0))
    energy = gaussian_filter1d(magnitude * q, max(.5, fps * .12)) / np.maximum(
        gaussian_filter1d(q, max(.5, fps * .12)), 1e-8)
    energy = standardized(np.log1p(energy), q)
    energy[q <= .15] = 0
    peaks, _ = find_peaks(energy, height=.6, prominence=.5, distance=max(1, round(fps * .4)))
    peaks = peaks[q[peaks] > .15]
    events = np.zeros(n)
    events[peaks] = 1
    events = standardized(gaussian_filter1d(events, max(.5, fps * .12)), q)
    events[q <= .15] = 0
    return {**empty, "energy": energy, "events": events, "quality": q,
            "event_times": time[peaks].tolist(), "source": empty["source"] if body else "experimental image-motion changes"}


def sections(radio, fpv, offset, seconds=3.):
    rt, ft = radio["time"], fpv["time"]
    windows = []
    if not len(rt) or not len(ft):
        return {"matched": 0, "usable": 0, "agreement": 0., "spread": 0., "windows": []}
    start, end = max(rt[0], ft[0] + offset), min(rt[-1], ft[-1] + offset)
    for a in np.arange(start, end, seconds):
        b = min(end, a + seconds)
        if b - a < 2.:
            continue
        mask = (rt >= a) & (rt < b)
        times = rt[mask]
        x = radio["energy"][mask]
        y = np.interp(times - offset, ft, fpv["energy"])
        q = np.minimum(radio["quality"][mask], np.interp(times - offset, ft, fpv["quality"], left=0, right=0))
        valid = q > .15
        usable = valid.sum() >= .7 * len(times)
        correlation = 0.
        if usable:
            weights = q[valid]
            dx = x[valid] - np.average(x[valid], weights=weights)
            dy = y[valid] - np.average(y[valid], weights=weights)
            vx, vy = np.average(dx ** 2, weights=weights), np.average(dy ** 2, weights=weights)
            usable = vx > .05 and vy > .05
            if usable:
                correlation = float(np.average(dx * dy, weights=weights) / np.sqrt(vx * vy))
        windows.append({"radio_start": float(a), "fpv_start": float(a - offset), "duration": float(b - a),
                        "usable": bool(usable), "matched": bool(usable and correlation >= .65),
                        "correlation": correlation})
    good = [w for w in windows if w["matched"]]
    count = sum(w["usable"] for w in windows)
    spread = (max(w["radio_start"] for w in good) - min(w["radio_start"] for w in good)) / max(seconds, end - start) if good else 0.
    return {"matched": len(good), "usable": count, "agreement": len(good) / count if count else 0.,
            "spread": float(spread), "windows": windows}


def compare(radio, fpv, min_overlap=2.):
    fps = radio["fps"]
    empty = {"version": VERSION, "score": 0., "offset_seconds": 0., "peak_margin": 0., "valid_seconds": 0.,
             "radio_events": 0, "fpv_events": 0, "sections": {"matched": 0, "usable": 0, "agreement": 0., "windows": []},
             "eligible": False, "suggested": False, "timing_resolution_seconds": 1 / fps,
             "reason": "Not enough observed movement; lost pose, static signals and gaps are not rhythm evidence."}
    na, nb = len(radio["time"]), len(fpv["time"])
    if min(na, nb) < 4:
        return empty
    minimum = max(4, round(max(12., min_overlap) * fps))
    q, p = radio["quality"], fpv["quality"]
    trend, count = weighted_correlations(radio["energy"], fpv["energy"], q, p, minimum)
    burst, _ = weighted_correlations(radio["events"], fpv["events"], q, p, minimum)
    count = np.maximum(count, 0)  # FFT rounding can leave tiny negative counts.
    scores = (.75 * np.maximum(trend, 0) + .25 * np.maximum(burst, 0)) * np.minimum(1, np.sqrt(count / (30 * fps)))
    offsets = -np.arange(-na + 1, nb) / fps
    index = int(np.argmax(scores))
    if scores[index] <= 0:
        return empty
    offset, score = float(offsets[index]), float(scores[index])
    rivals = np.abs(offsets - offset) > 2.
    margin = score - float(scores[rivals].max()) if rivals.any() else score
    local = sections(radio, fpv, offset)
    start, end = max(0., offset), min(radio["time"][-1], fpv["time"][-1] + offset)
    re = int(sum(start <= t <= end for t in radio["event_times"]))
    fe = int(sum(start <= t + offset <= end for t in fpv["event_times"]))
    eligible = (score >= .55 and margin >= .06 and re >= 4 and fe >= 4
                and local["matched"] >= 3 and local["agreement"] >= .6 and local["spread"] >= .3)
    return {**empty, "score": score, "offset_seconds": offset, "peak_margin": max(0., margin),
            "valid_seconds": float(count[index] / fps), "radio_events": re, "fpv_events": fe, "sections": local,
            "eligible": bool(eligible), "trend_score": float(trend[index]), "burst_score": float(burst[index]),
            "fpv_signal": fpv["source"], "reason": "Movement rhythm needs independent footage review." if eligible else
            "Rhythm is weak, repetitive or insufficiently supported across separate sections."}
