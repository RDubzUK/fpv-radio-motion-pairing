"""Experimental cross-modal fingerprints; never writes application decisions.

Try motor/vibration dynamics as well as ordinary angular-motion dynamics.
Each feature is computed on its own source timeline without reference offsets.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter1d, median_filter
from scipy.signal import stft

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fpv_matcher.matching.correlation import weighted_correlations

FPS = 10.
BANDS = ((30, 80), (80, 160), (160, 320), (320, 700))


def spectrum_features(samples, sample_rate):
    if samples.ndim == 1:
        samples = samples[:, None]
    size, hop = round(sample_rate * .256), round(sample_rate / FPS)
    frequency, times, z = stft(samples.T, fs=sample_rate, nperseg=size, noverlap=size-hop,
                               boundary=None, padded=False, axis=-1)
    power = np.sum(np.abs(z) ** 2, axis=0).T
    selected = (frequency >= 30) & (frequency <= 700)
    f, p = frequency[selected], power[:, selected]
    norm = p / np.maximum(p.sum(axis=1, keepdims=True), 1e-20)
    features = {"time": times, "rms": np.log1p(power.sum(axis=1) * 1e6),
                "flux": np.r_[0, np.maximum(np.diff(norm, axis=0), 0).sum(axis=1)],
                "centroid": np.sum(norm * f, axis=1), "peak": f[np.argmax(p, axis=1)]}
    for lo, hi in BANDS:
        features[f"band_{lo}_{hi}"] = np.log1p(power[:, (frequency >= lo) & (frequency < hi)].sum(axis=1) * 1e8)
    features["spectrum"] = np.log1p(p * 1e8)
    features["frequencies"] = f
    return features


def extract(output):
    pairs = json.loads((output / "reference.json").read_text())["pairs"]
    for number, pair in enumerate(pairs, 1):
        target = output / f"p{number}-gyro-features.npz"
        if not target.exists():
            with np.load(output / f"p{number}-fpv-telemetry.npz", allow_pickle=False) as data:
                t = data["time"]
                v = np.column_stack([data[k] for k in ("x", "y", "z")])
            fs = min(2000., round(1 / np.median(np.diff(t))))
            grid = np.arange(max(0, t[0]), min(t[-1], pair["sources"]["fpv"]["metadata"]["duration"]), 1 / fs)
            v = np.column_stack([np.interp(grid, t, v[:, i]) for i in range(3)])
            data = spectrum_features(v - gaussian_filter1d(v, fs * .03, axis=0), fs)
            data["time"] += grid[0]
            smooth = gaussian_filter1d(v, fs * .06, axis=0)
            acceleration = np.gradient(smooth, 1 / fs, axis=0)
            for key, x in {"speed": np.linalg.norm(smooth, axis=1),
                           "changes": np.linalg.norm(acceleration, axis=1),
                           **{k: smooth[:, i] for i, k in enumerate(("x", "y", "z"))}}.items():
                data[key] = np.interp(data["time"], grid, np.log1p(x) if key in {"speed", "changes"} else x)
            np.savez_compressed(target, **data)
        for kind in ("fpv", "radio"):
            target = output / f"p{number}-{kind}-audio-features.npz"
            if target.exists() or not pair["sources"][kind]["metadata"]["has_audio"]:
                continue
            path = pair["sources"][kind]["path"]
            print(f"Audio dynamics pair {number} {kind}", flush=True)
            result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-i", path,
                                     "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "pipe:1"],
                                    capture_output=True, timeout=180)
            if result.returncode:
                raise ValueError(result.stderr.decode(errors="replace"))
            np.savez_compressed(target, **spectrum_features(np.frombuffer(result.stdout, np.float32), 8000))


def load(path):
    with np.load(path, allow_pickle=False) as data:
        return {k: data[k] for k in data.files}


def scalar_search(radio, fpv, rkey, fkey, reference=None, min_seconds=8.):
    rt = np.arange(0, radio["time"][-1], 1/FPS)
    ft = np.arange(0, fpv["time"][-1], 1/FPS)
    a, b = np.interp(rt, radio["time"], radio[rkey]), np.interp(ft, fpv["time"], fpv[fkey])
    qa = np.interp(rt, radio["time"], radio.get(rkey + "_quality", radio.get("quality", np.ones(len(radio["time"])))), left=0, right=0)
    qb = np.interp(ft, fpv["time"], fpv.get(fkey + "_quality", fpv.get("quality", np.ones(len(fpv["time"])))), left=0, right=0)
    a, b = gaussian_filter1d(a, 1), gaussian_filter1d(b, 1)
    # Remove slow exposure / microphone gain / proximity drift at every lag.
    a -= gaussian_filter1d(a, FPS * 2)
    b -= gaussian_filter1d(b, FPS * 2)
    qa = np.where(qa >= .35, qa, 0)
    qb = np.where(qb >= .35, qb, 0)
    corr, count = weighted_correlations(a, b, qa, qb, round(min_seconds * FPS))
    offsets = -np.arange(-len(a)+1, len(b)) / FPS
    scores = np.maximum(corr, 0) * np.minimum(1, np.sqrt(np.maximum(count, 0) / (30 * FPS)))
    order = np.argsort(scores)[::-1]
    peaks = []
    for i in order:
        if scores[i] <= 0 or len(peaks) >= 5:
            break
        if all(abs(offsets[i] - p["offset"]) >= 2 for p in peaks):
            peaks.append({"offset": float(offsets[i]), "score": float(scores[i]),
                          "correlation": float(corr[i]), "seconds": float(count[i] / FPS)})
    result = {"radio_feature": rkey, "fpv_feature": fkey, "peaks": peaks}
    if reference is not None:
        index = int(np.argmin(abs(offsets - reference)))
        result["at_reference"] = {"offset": float(offsets[index]), "correlation": float(corr[index]),
                                  "score": float(scores[index]), "seconds": float(count[index] / FPS)}
    return result


def compare_all(output):
    pairs = json.loads((output / "reference.json").read_text())["pairs"]
    results = []
    audio_keys = ["rms", "flux", "centroid", "peak", *[f"band_{lo}_{hi}" for lo, hi in BANDS]]
    gyro_keys = ["speed", "changes", "rms", "flux", "centroid", "peak", *[f"band_{lo}_{hi}" for lo, hi in BANDS]]
    for ri in range(1, len(pairs)+1):
        radio = load(output / f"p{ri}-radio-audio-features.npz")
        for fi in range(1, len(pairs)+1):
            fpv = load(output / f"p{fi}-gyro-features.npz")
            reference = pairs[ri-1]["reference_offset"] if ri == fi else None
            signals = [scalar_search(radio, fpv, rkey, fkey, reference) for rkey in audio_keys for fkey in gyro_keys]
            results.append({"radio_pair": ri, "fpv_pair": fi, "signals": signals})
    (output / "cross-modal.json").write_text(json.dumps(results, indent=2, allow_nan=False)+"\n")
    for item in results:
        strongest = sorted(item["signals"], key=lambda s: s["peaks"][0]["score"] if s["peaks"] else 0, reverse=True)[:3]
        print(json.dumps({"r": item["radio_pair"], "f": item["fpv_pair"], "strongest": strongest}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    extract(args.output)
    compare_all(args.output)
