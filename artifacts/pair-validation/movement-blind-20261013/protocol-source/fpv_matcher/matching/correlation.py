from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.signal import correlate, find_peaks

from fpv_matcher.matching.signals import resample, standardized


@dataclass
class MatchResult:
    score: float
    offset_seconds: float
    overlap_seconds: float
    details: dict[str, float]
    peak_margin: float = 0.0
    coverage: float = 0.0
    valid_seconds: float = 0.0
    reliable_axes: int = 0
    event_score: float = 0.0
    alternatives: list[dict] = field(default_factory=list)
    correlated_sections: int = 0
    usable_sections: int = 0
    section_seconds: float = 3.0
    section_agreement: float = 0.0
    section_spread: float = 0.0


def section_support(radio, fpv, fps, offset, details, seconds=3.0, include_windows=False):
    """Independent local checks at one fixed lag and fixed axis polarities.

    Counts non-overlapping active sections, rather than treating adjacent
    sliding windows as independent evidence. Quiet/poorly tracked sections
    don't count as disagreement or agreement.
    """
    rt, ft = radio["time"], fpv["time"]
    body = bool(fpv.get("_body_rates", False))
    if not len(rt) or not len(ft):
        return {"correlated_sections": 0, "usable_sections": 0, "section_seconds": seconds,
                "section_agreement": 0.0, "section_spread": 0.0}
    common = (rt >= max(rt[0], ft[0] + offset)) & (rt <= ft[-1] + offset)
    times = rt[common]
    axes = (("roll", "roll"), ("pitch", "pitch"), ("yaw", "yaw")) if body else (("roll", "rotation"), ("pitch", "dy"), ("yaw", "dx"))
    step = max(3, round(seconds * fps))
    size = len(times)
    bins = (size + step - 1) // step
    if not bins:
        return {"correlated_sections": 0, "usable_sections": 0, "section_seconds": seconds,
                "section_agreement": 0.0, "section_spread": 0.0}
    valid_axes, matching_axes = np.zeros(bins, int), np.zeros(bins, int)
    lengths = np.minimum(step, size - np.arange(bins) * step)
    for rkey, fkey in axes:
        derivative = f"d_{rkey}:{fkey}" in details
        rk = "_d_" + rkey if derivative else "_" + rkey
        qk = "_dq_" + rkey if derivative else "_q_" + rkey
        a = radio[rk][common]
        b = np.interp(times - offset, ft, fpv["_" + fkey])
        q = np.minimum(radio[qk][common], np.interp(times - offset, ft, fpv["_q_" + fkey], left=0, right=0))
        q = np.where(q > .1, q, 0)
        def blocks(values):
            return np.pad(values, (0, bins * step - size)).reshape(bins, step)
        weights = blocks(q); weight = np.maximum(weights.sum(axis=1), 1e-8)
        aa, bb = blocks(a), blocks(b)
        ma, mb = (aa * weights).sum(axis=1) / weight, (bb * weights).sum(axis=1) / weight
        da, db = aa - ma[:, None], bb - mb[:, None]
        va, vb = (da * da * weights).sum(axis=1), (db * db * weights).sum(axis=1)
        usable = ((weights > .1).sum(axis=1) >= np.ceil(.7 * lengths)) & (va / weight > .01) & (vb / weight > .01) & (lengths / fps >= 2)
        polarity = details.get(("d_" if derivative else "") + rkey + ":" + fkey, 1)
        corr = (da * db * weights).sum(axis=1) / np.sqrt(np.maximum(va * vb, 1e-12)) * polarity
        valid_axes += usable
        matching_axes += usable & (corr >= .6)
    usable = valid_axes >= 2
    matched = usable & (matching_axes >= 2)
    count, total = int(matched.sum()), int(usable.sum())
    thirds = np.minimum(2, np.arange(bins) * 3 // bins)
    spread = len(set(thirds[matched].tolist())) / 3
    result = {"correlated_sections": count, "usable_sections": total, "section_seconds": seconds,
              "section_agreement": count / total if total else 0.0, "section_spread": spread}
    if include_windows:
        result["windows"] = [{"start": float(times[i * step]),
                              "end": float(min(times[-1] + 1 / fps, times[i * step] + lengths[i] / fps)),
                              "matched": bool(matched[i]), "usable": bool(usable[i]),
                              "matching_axes": int(matching_axes[i])} for i in range(bins)]
    return result


def prepare(data: dict, fps: float, radio: bool) -> dict:
    result = resample(data, fps)
    result["_prepared"] = True
    keys = ("roll", "yaw", "pitch") if radio or bool(data.get("_body_rates", False)) else ("rotation", "dx", "dy")
    for key in keys:
        signal = np.asarray(result[key], float)
        q = np.clip(np.asarray(result.get(key + "_quality", result["quality"]), float), 0, 1)
        q[q <= 0.1] = 0
        q[~np.isfinite(signal)] = 0
        # Reject static gimbals and stationary optical flow, including numerical noise.
        minimum = 0.012 if radio else (1.0 if bool(data.get("_body_rates", False)) else (0.01 if key == "rotation" else 0.0001))
        valid = q > 0.1
        if valid.sum() < 3 or np.std(signal[valid]) < minimum:
            q[:] = 0
        normalized = standardized(signal, q)
        if len(signal) > 2:
            weights = gaussian_filter1d(q, max(1, fps * 1.5))
            slow = gaussian_filter1d(normalized * q, max(1, fps * 1.5)) / np.maximum(weights, 1e-6)
            normalized = standardized(normalized - slow, q)
            smoothed = gaussian_filter1d(normalized * q, max(0.5, fps * 0.08)) / np.maximum(
                gaussian_filter1d(q, max(0.5, fps * 0.08)), 1e-6
            )
            derivative = np.gradient(smoothed, 1 / fps)
        else:
            derivative = np.zeros(len(signal))
        neighbor_quality = np.minimum(q, np.minimum(np.r_[0, q[:-1]], np.r_[q[1:], 0])) if len(q) else q
        result["_" + key] = normalized
        result["_d_" + key] = standardized(derivative, neighbor_quality)
        # Activity bursts supply independent timing evidence in addition to sign.
        result["_event_" + key] = standardized(np.abs(derivative), neighbor_quality)
        result["_q_" + key], result["_dq_" + key] = q, neighbor_quality
    return result


def weighted_correlations(a, b, qa, qb, minimum):
    weight = correlate(qb, qa, mode="full", method="fft")
    count = correlate((qb > 0.1).astype(float), (qa > 0.1).astype(float), mode="full", method="fft")
    n = np.maximum(weight, 1e-8)
    cross = correlate(qb * b, qa * a, mode="full", method="fft")
    sa = correlate(qb, qa * a, mode="full", method="fft")
    sb = correlate(qb * b, qa, mode="full", method="fft")
    va = np.maximum(correlate(qb, qa * a * a, mode="full", method="fft") - sa * sa / n, 0)
    vb = np.maximum(correlate(qb * b * b, qa, mode="full", method="fft") - sb * sb / n, 0)
    corr = (cross - sa * sb / n) / np.sqrt(np.maximum(va * vb, 1e-12))
    usable = (count >= minimum - 1e-5) & (weight >= minimum * 0.12) & (va > 0.01) & (vb > 0.01)
    corr = np.clip(np.nan_to_num(corr), -1, 1)
    corr[~usable] = 0
    return corr, count


def compare(
    radio: dict,
    fpv: dict,
    sample_fps: float,
    min_overlap_seconds: float = 2,
    offset_range: tuple[float, float] | None = None,
) -> MatchResult:
    """Positive offset: FPV clip starts later, so trim that time from StickCam."""
    if not radio.get("_prepared"):
        radio = prepare(radio, sample_fps, True)
    if not fpv.get("_prepared"):
        fpv = prepare(fpv, sample_fps, False)
    na, nb = len(radio["time"]), len(fpv["time"])
    empty = MatchResult(0, 0, 0, {})
    if min(na, nb) < 3:
        return empty
    minimum = max(3, round(min_overlap_seconds * sample_fps))
    offsets = -np.arange(-na + 1, nb, dtype=float) / sample_fps
    continuous, events, signs, chosen_keys, counts = [], [], [], [], []
    body = bool(fpv.get("_body_rates", False))
    axes = (("roll", "roll"), ("yaw", "yaw"), ("pitch", "pitch")) if body else (("roll", "rotation"), ("yaw", "dx"), ("pitch", "dy"))
    for rkey, fkey in axes:
        position, count = weighted_correlations(
            radio["_" + rkey], fpv["_" + fkey], radio["_q_" + rkey], fpv["_q_" + fkey], minimum
        )
        velocity, _ = weighted_correlations(
            radio["_d_" + rkey], fpv["_" + fkey], radio["_dq_" + rkey], fpv["_q_" + fkey], minimum
        )
        prefer_velocity = np.zeros(len(position), dtype=bool) if body else np.abs(velocity) > np.abs(position)
        selected = np.where(prefer_velocity, velocity, position)
        event, _ = weighted_correlations(
            radio["_event_" + rkey], fpv["_event_" + fkey], radio["_dq_" + rkey], fpv["_dq_" + fkey], minimum
        )
        continuous.append(np.abs(selected))
        events.append(np.maximum(event, 0))
        signs.append(np.sign(selected))
        chosen_keys.append(prefer_velocity)
        counts.append(count)
    continuous, events, counts = np.stack(continuous), np.stack(events), np.stack(counts)
    # Each physical axis can contribute once; position and velocity aren't two independent axes.
    order = np.argsort(continuous, axis=0)
    top = np.take_along_axis(continuous, order[-2:], axis=0).mean(axis=0)
    top_events = np.take_along_axis(events, order[-2:], axis=0).mean(axis=0)
    valid_seconds = np.take_along_axis(counts, order[-2:], axis=0).mean(axis=0) / sample_fps
    lag_scores = (0.85 * top + 0.15 * top_events) * np.minimum(1, np.sqrt(np.maximum(0, valid_seconds) / 25))
    # One visible axis can produce a review candidate, but receives less support.
    lag_scores *= np.where(np.sum(continuous > 0.25, axis=0) >= 2, 1, 0.75)
    lag_scores[~np.isfinite(lag_scores)] = 0
    allowed = np.ones(len(offsets), bool)
    if offset_range is not None:
        allowed = (offsets >= offset_range[0]) & (offsets <= offset_range[1])
        lag_scores[~allowed] = 0
    index = int(np.argmax(lag_scores))
    if lag_scores[index] <= 0:
        return empty
    offset = float(offsets[index])
    if 0 < index < len(lag_scores) - 1 and allowed[index - 1 : index + 2].all():
        left, middle, right = lag_scores[index - 1 : index + 2]
        denominator = left - 2 * middle + right
        if denominator < -1e-8:
            shift = float(np.clip(0.5 * (left - right) / denominator, -0.5, 0.5))
            offset -= shift / sample_fps
    competitors = np.abs(offsets - offset) > 2.0
    second = float(np.max(lag_scores[competitors])) if competitors.any() else 0
    peaks, _ = find_peaks(lag_scores, distance=max(1, round(sample_fps * 2)))
    alternatives = [
        {"offset_seconds": float(offsets[i]), "score": float(lag_scores[i])}
        for i in sorted(peaks, key=lambda i: lag_scores[i], reverse=True)[:4]
    ]
    details = {}
    for i, (rkey, fkey) in enumerate(axes):
        name = "d_" + rkey if chosen_keys[i][index] else rkey
        details[f"{name}:{fkey}"] = float(signs[i][index])
        details[f"strength_{rkey}"] = float(continuous[i, index])
    overlap = max(0, min(na / sample_fps - max(0, offset), nb / sample_fps - max(0, -offset)))
    valid = float(valid_seconds[index])
    result = MatchResult(
        float(lag_scores[index]),
        offset,
        overlap,
        details,
        max(0, float(lag_scores[index]) - second),
        min(1, valid / max(overlap, 1e-6)),
        valid,
        int((continuous[:, index] > 0.3).sum()),
        float(top_events[index]),
        alternatives,
    )
    for key, value in section_support(radio, fpv, sample_fps, offset, details).items():
        setattr(result, key, value)
    return result
