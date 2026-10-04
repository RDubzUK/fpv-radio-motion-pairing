from __future__ import annotations

import shutil
import subprocess
from collections import defaultdict

import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.signal import correlate, find_peaks

from fpv_matcher.video.metadata import probe_video

ALGORITHM_VERSION = "audio-boundary-trends-landmarks-v4"
FRAME_RATE = 10.0
SAMPLE_RATE = 8000
BOUNDARY_SECONDS = 30.0


def _empty():
    return {
        "time": np.array([], float),
        "features": np.empty((0, 5)),
        "valid": np.array([], bool),
        "segments": np.array([], np.int32),
        "quality": np.asarray(0.0),
        "duration": np.asarray(0.0),
        "hashes": np.array([], np.uint32),
        "hash_times": np.array([], float),
    }


def validate_audio(data):
    """Reject partial or malformed cached fingerprints before comparison."""
    times = np.asarray(data["time"], float)
    features = np.asarray(data["features"], float)
    if times.ndim != 1 or features.shape != (len(times), 5):
        raise ValueError("Audio fingerprints need five features at each timestamp")
    if not np.isfinite(times).all() or not np.isfinite(features).all() or np.any(times < 0) or np.any(np.diff(times) <= 0):
        raise ValueError("Audio fingerprint timestamps and features must be finite and ordered")
    for key in ("valid", "segments"):
        if np.asarray(data[key]).shape != times.shape:
            raise ValueError("Audio fingerprint segments must match their timestamps")
    hashes, hash_times = np.asarray(data["hashes"]), np.asarray(data["hash_times"], float)
    if hashes.ndim != 1 or hash_times.shape != hashes.shape or not np.isfinite(hash_times).all() or np.any(hash_times < 0):
        raise ValueError("Audio fingerprint hashes must have finite source timestamps")
    quality, duration = float(np.asarray(data["quality"]).item()), float(np.asarray(data["duration"]).item())
    if not np.isfinite(quality) or not 0 <= quality <= 1 or not np.isfinite(duration) or duration < 0:
        raise ValueError("Audio fingerprint quality and duration are invalid")


def _landmarks(samples, start):
    """Frequency-peak pairs provide evidence beyond similar volume envelopes.

    Quantised frequencies and relative times tolerate microphone gain/EQ changes.
    Repetitive hashes are removed later, suppressing constant motor tones/beeps.
    """
    frame, hop = 1024, 400
    if len(samples) < frame:
        return np.array([], np.uint32), np.array([], float)
    windows = np.lib.stride_tricks.sliding_window_view(samples, frame)[::hop]
    spectrum = np.log1p(np.abs(np.fft.rfft(windows * np.hanning(frame), axis=1)))
    whitened = spectrum - gaussian_filter1d(spectrum, 3, axis=1)
    peaks = []
    for row in whitened:
        indices, properties = find_peaks(row[20:449], height=max(0.01, float(np.std(row))), distance=4)
        ranked = sorted(
            zip(indices + 20, properties["peak_heights"], strict=True), key=lambda item: item[1], reverse=True
        )[:3]
        peaks.append([int(index // 2) for index, _ in ranked])
    hashes, times = [], []
    for index, anchors in enumerate(peaks):
        for delta in (4, 8, 12, 16, 24):
            if index + delta >= len(peaks):
                break
            for anchor in anchors:
                for target in peaks[index + delta][:2]:
                    hashes.append((anchor << 16) | (target << 8) | delta)
                    times.append(start + (index * hop + frame / 2) / SAMPLE_RATE)
    return np.asarray(hashes, np.uint32), np.asarray(times, float)


def _segment_features(samples, start):
    frame, hop = 1024, 800
    if len(samples) < frame:
        return np.empty(0), np.empty((0, 5))
    windows = np.lib.stride_tricks.sliding_window_view(samples, frame)[::hop]
    spectra = np.abs(np.fft.rfft(windows * np.hanning(frame), axis=1))
    power = spectra**2
    frequency = np.fft.rfftfreq(frame, 1 / SAMPLE_RATE)
    energy = np.log1p(np.mean(windows**2, axis=1) * 1e5)
    normalized = spectra / np.maximum(spectra.sum(axis=1, keepdims=True), 1e-8)
    flux = np.r_[0.0, np.maximum(0, np.diff(normalized, axis=0)).sum(axis=1)]
    bands = [
        np.log1p(power[:, (frequency >= lo) & (frequency < hi)].mean(axis=1))
        for lo, hi in ((50, 250), (250, 1000), (1000, 4000))
    ]
    return start + (np.arange(len(windows)) * hop + frame / 2) / SAMPLE_RATE, np.column_stack(
        [energy, flux, *bands]
    )


def extract_audio(path: str, boundary_seconds: float = BOUNDARY_SECONDS):
    info = probe_video(path)
    if not info["has_audio"]:
        return _empty()
    duration = info["duration"]
    sections = (
        [(0.0, duration)]
        if duration <= 2 * boundary_seconds
        else [(0.0, boundary_seconds), (duration - boundary_seconds, boundary_seconds)]
    )
    times_all, features_all, segment_ids, hashes_all, hash_times_all = [], [], [], [], []
    for segment, (start, length) in enumerate(sections):
        try:
            result = subprocess.run(
                [
                    shutil.which("ffmpeg") or "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-threads",
                    "1",
                    "-ss",
                    str(start),
                    "-i",
                    path,
                    "-t",
                    str(length),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    str(SAMPLE_RATE),
                    "-f",
                    "f32le",
                    "pipe:1",
                ],
                capture_output=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            continue  # Optional audio cannot invalidate a usable motion fingerprint.
        if result.returncode:
            continue
        samples = np.clip(
            np.nan_to_num(np.frombuffer(result.stdout, "<f4"), nan=0, posinf=0, neginf=0), -4, 4
        )
        times, features = _segment_features(samples, start)
        hashes, hash_times = _landmarks(samples, start)
        hashes_all.append(hashes)
        hash_times_all.append(hash_times)
        if len(times):
            times_all.append(times)
            features_all.append(features)
            segment_ids.append(np.full(len(times), segment, np.int32))
    if not times_all:
        return _empty()
    times, features, segments = (
        np.concatenate(times_all),
        np.concatenate(features_all),
        np.concatenate(segment_ids),
    )
    spread = np.std(features, axis=0)
    quality = float(np.mean(np.clip(spread / np.array([0.12, 0.01, 0.05, 0.05, 0.05]), 0, 1)))
    median = np.median(features, axis=0)
    scale = np.maximum(np.percentile(np.abs(features - median), 75, axis=0) * 1.4826, 1e-5)
    features = np.clip((features - median) / scale, -5, 5)
    hashes, hash_times = np.concatenate(hashes_all), np.concatenate(hash_times_all)
    if len(hashes):
        _, inverse, counts = np.unique(hashes, return_inverse=True, return_counts=True)
        keep = counts[inverse] <= 8
        hashes, hash_times = hashes[keep], hash_times[keep]
    return {
        "time": times,
        "features": features,
        "valid": np.ones(len(times), bool),
        "segments": segments,
        "quality": np.asarray(quality),
        "duration": np.asarray(duration),
        "hashes": hashes,
        "hash_times": hash_times,
    }


def _hash_index(audio):
    if "_hash_index" not in audio:
        index = defaultdict(list)
        for key, timestamp in zip(audio.get("hashes", []), audio.get("hash_times", []), strict=True):
            index[int(key)].append(float(timestamp))
        audio["_hash_index"] = index
    return audio["_hash_index"]


def landmark_match(radio, fpv):
    """Return strength, offset, peak margin, unique hashes, temporal span."""
    key = id(fpv)
    cached = radio.setdefault("_landmark_pairs", {})
    if key in cached:
        return cached[key][1]
    ra, fb = _hash_index(radio), _hash_index(fpv)
    votes = defaultdict(dict)
    for hashed in ra.keys() & fb.keys():
        for rt in ra[hashed]:
            for ft in fb[hashed]:
                bucket = round((rt - ft) * 20)
                votes[bucket][hashed] = rt
    best = None
    for bucket in votes:
        nearby = {}
        for adjacent in (bucket - 1, bucket, bucket + 1):
            nearby.update(votes.get(adjacent, {}))
        if best is None or len(nearby) > best[0]:
            best = (len(nearby), bucket / 20, nearby)
    value = (0.0, 0.0, 0.0, 0, 0.0)
    if best:
        count, offset, nearby = best
        span = max(nearby.values()) - min(nearby.values())
        second = max(
            (
                len(votes[b]) + len(votes.get(b - 1, {})) + len(votes.get(b + 1, {}))
                for b in votes
                if abs(b / 20 - offset) > 2
            ),
            default=0,
        )
        distinct = max(0.0, 1 - second / max(count, 1))
        minimum = max(30, 0.015 * min(len(ra), len(fb)))
        if count >= minimum and span >= 2 and distinct >= 0.25:
            strength = min(0.98, 0.6 + 0.25 * min(1, count / max(100, minimum * 2)) + 0.15 * distinct)
            value = (strength, offset, 0.2 * distinct, count, span)
    # Retain the source object so its id cannot be recycled under this cache key.
    cached[key] = (fpv, value)
    return value


def _chunks(audio):
    for segment in np.unique(audio["segments"]):
        indices = np.flatnonzero(audio["segments"] == segment)
        if len(indices):
            yield float(audio["time"][indices[0]]), audio["features"][indices]


def _boundary_overlap(radio, fpv, offset):
    return max(
        (
            max(
                0.0,
                min(rstart + len(ra) / FRAME_RATE, fstart + offset + len(fb) / FRAME_RATE)
                - max(rstart, fstart + offset),
            )
            for rstart, ra in _chunks(radio)
            for fstart, fb in _chunks(fpv)
        ),
        default=0.0,
    )


def score_at_offset(radio, fpv, offset_seconds, min_overlap_seconds=2.0, include_landmarks=True):
    minimum = max(3, round(min_overlap_seconds * FRAME_RATE))
    by_feature, overlap = {}, 0
    for rstart, ra in _chunks(radio):
        for fstart, fb in _chunks(fpv):
            lag = round((rstart - fstart - offset_seconds) * FRAME_RATE)
            ia, ib = max(0, -lag), max(0, lag)
            count = min(len(ra) - ia, len(fb) - ib)
            if count < minimum:
                continue
            overlap = max(overlap, count)
            for col in range(ra.shape[1]):
                x, y = ra[ia : ia + count, col], fb[ib : ib + count, col]
                if min(np.std(x), np.std(y)) > 0.03:
                    value = max(0.0, float(np.corrcoef(x, y)[0, 1]))
                    by_feature[col] = max(value, by_feature.get(col, 0))
    values = sorted(by_feature.values(), reverse=True)[:3]
    score = float(np.mean(values)) if len(values) >= 2 else 0.0
    landmark_score, landmark_offset, _, _, _ = landmark_match(radio, fpv) if include_landmarks else (0, 0, 0, 0, 0)
    if include_landmarks and abs(landmark_offset - offset_seconds) <= 0.15 and landmark_score:
        boundary_overlap = _boundary_overlap(radio, fpv, offset_seconds)
        if boundary_overlap >= min_overlap_seconds:
            score, overlap = max(score, landmark_score), max(overlap, boundary_overlap * FRAME_RATE)
    return score, overlap / FRAME_RATE


def best_offset(radio, fpv, min_overlap_seconds=2.0, include_landmarks=True):
    """Return score, offset, overlap, competing-peak margin at a single shared lag."""
    landmark_score, landmark_offset, landmark_margin, _, landmark_span = landmark_match(radio, fpv)
    if min(float(radio["quality"]), float(fpv["quality"])) < 0.1:
        return 0.0, 0.0, 0.0, 0.0
    minimum = round(min_overlap_seconds * FRAME_RATE)
    buckets = {}
    for rstart, ra in _chunks(radio):
        for fstart, fb in _chunks(fpv):
            ones_a, ones_b = np.ones(len(ra)), np.ones(len(fb))
            count = correlate(ones_b, ones_a, method="fft")
            n = np.maximum(count, 1)
            values = []
            for col in range(ra.shape[1]):
                a, b = ra[:, col], fb[:, col]
                sa, sb = correlate(ones_b, a, method="fft"), correlate(b, ones_a, method="fft")
                va = np.maximum(correlate(ones_b, a * a, method="fft") - sa * sa / n, 0)
                vb = np.maximum(correlate(b * b, ones_a, method="fft") - sb * sb / n, 0)
                corr = (correlate(b, a, method="fft") - sa * sb / n) / np.sqrt(np.maximum(va * vb, 1e-12))
                corr[(count < minimum - 1e-5) | (va / n < 0.001) | (vb / n < 0.001)] = 0
                values.append(np.clip(np.nan_to_num(corr), 0, 1))
            # Correlations are evaluated at the same lag, never independent maxima.
            scores = np.sort(values, axis=0)[-3:].mean(axis=0) * np.minimum(
                1, np.sqrt(np.maximum(count, 0) / (10 * FRAME_RATE))
            )
            peaks, _ = find_peaks(scores, distance=5)
            for index in sorted(peaks, key=lambda i: scores[i], reverse=True)[:6]:
                offset = rstart - fstart - (index - len(ra) + 1) / FRAME_RATE
                bucket = round(offset * FRAME_RATE)
                item = (float(scores[index]), offset, float(count[index] / FRAME_RATE))
                if item[0] > buckets.get(bucket, (0, 0, 0))[0]:
                    buckets[bucket] = item
    if not buckets:
        if not include_landmarks:
            return 0.0, 0.0, 0.0, 0.0
        return (
            landmark_score,
            landmark_offset,
            _boundary_overlap(radio, fpv, landmark_offset),
            landmark_margin,
        )
    score, offset, overlap = max(buckets.values())
    second = max((item[0] for item in buckets.values() if abs(item[1] - offset) > 2), default=0.0)
    margin = max(0.0, score - second)
    if include_landmarks and landmark_score >= 0.82 and landmark_margin >= 0.08:
        trend, trend_overlap = score_at_offset(radio, fpv, landmark_offset, min_overlap_seconds)
        return (
            max(landmark_score, trend),
            landmark_offset,
            max(_boundary_overlap(radio, fpv, landmark_offset), trend_overlap),
            landmark_margin,
        )
    return score, offset, overlap, margin
