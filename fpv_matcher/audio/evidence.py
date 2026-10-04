"""Independent, same-lag checks for a conservative audio-first shortlist.

These are evidence gates, not calibrated match probabilities. They reuse the
small boundary fingerprints and never decode full-recording audio waveforms.
"""
from __future__ import annotations

import math

import numpy as np

from fpv_matcher.audio.fingerprint import _boundary_overlap, best_offset, landmark_match, score_at_offset

POLICY_VERSION = "independent-boundary-audio-sections-v1"


def audio_sections(radio, fpv, offset, seconds=3.0):
    """Check non-overlapping sections at the proposed lag, with no local search."""
    index = {}
    for hashed, timestamp in zip(fpv["hashes"], fpv["hash_times"], strict=True):
        index.setdefault(int(hashed), []).append(float(timestamp))
    votes = [(float(rt), int(h)) for h, rt in zip(radio["hashes"], radio["hash_times"], strict=True)
             if any(abs(float(rt) - ft - offset) <= .15 for ft in index.get(int(h), []))]
    windows = []
    quality = min(float(radio["quality"]), float(fpv["quality"]))
    for rs in np.unique(radio["segments"]):
        ri = np.flatnonzero(radio["segments"] == rs)
        for fs in np.unique(fpv["segments"]):
            fi = np.flatnonzero(fpv["segments"] == fs)
            begin = max(float(radio["time"][ri[0]]), float(fpv["time"][fi[0]]) + offset)
            end = min(float(radio["time"][ri[-1]]), float(fpv["time"][fi[-1]]) + offset)
            for start in np.arange(begin, end, seconds):
                stop = min(end, start + seconds)
                if stop - start < 2:
                    continue
                indices = ri[(radio["time"][ri] >= start) & (radio["time"][ri] < stop)]
                a = radio["features"][indices]
                b = np.column_stack([np.interp(radio["time"][indices] - offset, fpv["time"][fi],
                                               fpv["features"][fi, col]) for col in range(5)])
                active = [col for col in range(5) if len(indices) >= 20
                          and min(np.std(a[:, col]), np.std(b[:, col])) > .08]
                corrs = [float(np.corrcoef(a[:, col], b[:, col])[0, 1]) for col in active]
                supported = sum(math.isfinite(c) and c >= .65 for c in corrs)
                local = [(t, h) for t, h in votes if start <= t < stop]
                distinct = len({h for _, h in local})
                span = max((t for t, _ in local), default=start) - min((t for t, _ in local), default=start)
                fingerprint = distinct >= 12 and span >= 1.5
                trend_usable = len(active) >= 3 and quality > .1
                trend = trend_usable and supported >= 3
                usable = trend_usable or fingerprint
                windows.append({"start": float(start), "end": float(stop),
                    "matched": bool(usable and (trend or fingerprint)), "usable": bool(usable),
                    "trend_usable": bool(trend_usable), "trend_matched": bool(trend),
                    "fingerprint_matched": bool(fingerprint), "jointly_matched": bool(trend and fingerprint),
                    "matching_features": supported, "fingerprint_hashes": distinct,
                    "evidence": "spectral fingerprints + audio trends" if trend and fingerprint
                                else "spectral fingerprints" if fingerprint else "audio trends"})
    return {"windows": windows, "correlated_sections": sum(w["matched"] for w in windows),
        "usable_sections": sum(w["usable"] for w in windows), "section_seconds": seconds,
        "trend_sections": sum(w["trend_matched"] for w in windows),
        "trend_usable_sections": sum(w["trend_usable"] for w in windows),
        "fingerprint_sections": sum(w["fingerprint_matched"] for w in windows),
        "joint_sections": sum(w["jointly_matched"] for w in windows)}


def assess_audio_pair(radio, fpv, min_overlap=2.0):
    """Distinguish trend corroboration from a score already boosted by hashes."""
    strength, offset, margin, hashes, span = landmark_match(radio, fpv)
    trend_best, trend_offset, trend_overlap, trend_margin = best_offset(
        radio, fpv, min_overlap, include_landmarks=False)
    trend, overlap = score_at_offset(radio, fpv, offset, min_overlap, include_landmarks=False)
    # Reuse the trend search instead of running every FFT twice per pair.
    if min(float(radio["quality"]), float(fpv["quality"])) < .1:
        best_score, best_offset_seconds, best_overlap, best_margin = 0., 0., 0., 0.
    elif strength >= .82 and margin >= .08:
        best_score, best_offset_seconds, best_overlap, best_margin = (
            max(strength, trend), offset, max(_boundary_overlap(radio, fpv, offset), overlap), margin)
    elif trend_best > 0:
        best_score, best_offset_seconds, best_overlap, best_margin = trend_best, trend_offset, trend_overlap, trend_margin
    else:
        best_score, best_offset_seconds, best_overlap, best_margin = strength, offset, _boundary_overlap(radio, fpv, offset), margin
    # Local checks are only useful near a reasonably distinctive fingerprint.
    sections = (audio_sections(radio, fpv, offset) if strength >= .82 and span >= 4 else
                {"windows": [], "correlated_sections": 0, "usable_sections": 0, "section_seconds": 3.0,
                 "trend_sections": 0, "trend_usable_sections": 0, "fingerprint_sections": 0, "joint_sections": 0})
    failures = []
    if strength < .88 or margin < .10 or hashes < 60 or span < 8:
        failures.append("Not enough distinctive shared spectral landmarks")
    if trend < .72 or trend_margin < .06 or abs(trend_offset - offset) > .2:
        failures.append("Independent audio trends do not establish the same unique offset")
    if overlap < max(8., min_overlap):
        failures.append("Less than eight seconds of shared boundary-audio evidence")
    if sections["joint_sections"] < 3 or sections["joint_sections"] / max(1, sections["trend_usable_sections"]) < .6:
        failures.append("Fewer than three independent sections agree in both fingerprints and audio trends")
    score = min(.98, .6 * strength + .4 * trend) if not failures else 0.0
    return {"eligible": not failures, "score": score, "offset": float(offset), "reasons": failures,
        "landmark_strength": strength, "landmark_peak_margin": margin, "shared_hashes": hashes,
        "fingerprint_span": span, "trend_score": trend, "trend_best_score": trend_best,
        "trend_best_offset": trend_offset, "trend_peak_margin": trend_margin,
        "trend_overlap": overlap, "sections": sections,
        "best_score": best_score, "best_offset": best_offset_seconds, "best_overlap": best_overlap,
        "peak_margin": best_margin, "quality": min(float(radio["quality"]), float(fpv["quality"]))}
