from __future__ import annotations

import numpy as np
from scipy.ndimage import gaussian_filter1d


def resample(data: dict, fps: float) -> dict:
    """Resample actual timestamps, preserving invalid periods and clip-relative zero.

    No extrapolation before a calibration frame and no interpolation over gaps
    longer than half a second. Smooth before reducing the sample rate.
    """
    if not np.isfinite(fps) or fps <= 0:
        raise ValueError("Sampling rate must be finite and positive")
    times = np.asarray(data["time"], dtype=float)
    if not len(times):
        return {**data, "time": times, "sample_fps": np.asarray(fps)}
    good = np.isfinite(times)
    indices = np.flatnonzero(good)[np.argsort(times[good], kind="stable")]
    if not len(indices):
        raise ValueError("Fingerprint has no finite timestamps")
    unique = np.r_[True, np.diff(times[indices]) > 1e-8]
    indices = indices[unique]
    times = times[indices]
    if not len(times):
        raise ValueError("Fingerprint has no finite timestamps")
    if times[0] < 0:
        raise ValueError("Fingerprint timestamps must be relative to the clip start")
    grid = np.arange(int(np.floor(times[-1] * fps + 1e-6)) + 1, dtype=float) / fps
    result = {"time": grid, "sample_fps": np.asarray(fps)}
    delta = float(np.median(np.diff(times))) if len(times) > 1 else 1 / fps
    sigma = max(0, (0.4 / fps) / max(delta, 1e-6)) if delta < 0.8 / fps else 0
    positions = np.searchsorted(times, grid, side="left")
    low, high = np.clip(positions - 1, 0, len(times) - 1), np.clip(positions, 0, len(times) - 1)
    within = (grid >= times[0] - 1e-7) & (grid <= times[-1] + 1e-7)
    exact_high, exact_low = np.abs(grid - times[high]) < 1e-7, np.abs(grid - times[low]) < 1e-7
    close = ((times[high] - times[low]) <= max(0.5, delta * 1.6)) | exact_high | exact_low
    base_quality = np.clip(
        np.nan_to_num(
            np.asarray(data.get("quality", np.ones(len(data["time"]))), float)[indices],
            nan=0,
            posinf=0,
            neginf=0,
        ),
        0,
        1,
    )
    invalid_signals = {}
    for key, value in data.items():
        if key == "time" or key.startswith("d_"):
            continue
        array = np.asarray(value)
        if array.ndim != 1 or len(array) != len(data["time"]):
            result[key] = value
            continue
        array = array[indices].astype(float)
        finite = np.isfinite(array)
        array = np.nan_to_num(array, nan=0, posinf=0, neginf=0)
        if "quality" in key:
            sampled = np.minimum(array[low], array[high])
            sampled[exact_high] = array[high[exact_high]]
            sampled[exact_low] = array[low[exact_low]]
        else:
            weights = (
                np.asarray(data[key + "_quality"], dtype=float)[indices]
                if key + "_quality" in data
                else base_quality
            )
            if sigma and len(array) > 2:
                denom = gaussian_filter1d(weights, sigma)
                array = gaussian_filter1d(array * weights, sigma) / np.maximum(denom, 1e-6)
            sampled = np.interp(grid, times, array)
            if not finite.all():
                valid = finite[low] & finite[high]
                valid[exact_high] = finite[high[exact_high]]
                valid[exact_low] = finite[low[exact_low]]
                invalid_signals[key] = valid
        sampled[~(within & close)] = 0
        result[key] = sampled
    if "quality" not in result:
        result["quality"] = np.where(within & close, 1.0, 0.0)
    for key, valid in invalid_signals.items():
        result[key + "_quality"] = result.get(key + "_quality", result["quality"]).copy() * valid
    for key in ("roll", "pitch", "yaw", "throttle"):
        if key in result:
            quality = result.get(key + "_quality", result["quality"])
            derivative = np.gradient(result[key], 1 / fps) if len(grid) > 1 else np.zeros(len(grid))
            usable = quality > 0.1
            usable &= np.r_[False, quality[:-1] > 0.1] & np.r_[quality[1:] > 0.1, False]
            derivative[~usable] = 0
            result["d_" + key] = derivative
    return result


def standardized(values: np.ndarray, quality: np.ndarray) -> np.ndarray:
    values = np.nan_to_num(np.asarray(values, float))
    valid = quality > 0.1
    if valid.sum() < 3:
        return np.zeros_like(values)
    median = np.median(values[valid])
    scale = max(float(np.percentile(np.abs(values[valid] - median), 75)) * 1.4826, 1e-6)
    return np.clip((values - median) / scale, -5, 5)
