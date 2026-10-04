"""Streaming waveform envelopes and conservative local boundary-audio evidence."""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from fpv_matcher.audio.fingerprint import ALGORITHM_VERSION, SAMPLE_RATE, extract_audio
from fpv_matcher.cache.fingerprints import get_or_extract
from fpv_matcher.pipeline import atomic_json
from fpv_matcher.video.metadata import probe_video

VERSION = "audio-waveform-envelope-v1"


def extract_waveform(path, progress=None):
    info = probe_video(path)
    empty = {k: np.array([], float) for k in ("time", "minimum", "maximum", "rms")}
    if not info["has_audio"]:
        return empty
    # At most 200k envelopes even for unusually long recordings. Never hold PCM for the full clip.
    width = max(80, math.ceil(info["duration"] * SAMPLE_RATE / 200000))
    values, pending, consumed = [], b"", 0
    command = [shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-v", "error", "-threads", "1",
               "-i", str(path), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
               "-af", "aresample=async=1:first_pts=0", "-t", str(info["duration"]), "-f", "f32le", "pipe:1"]
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        try:
            while chunk := process.stdout.read(65536):
                pending += chunk
                length = len(pending) // (4 * width) * (4 * width)
                if not length:
                    continue
                samples = np.nan_to_num(np.frombuffer(pending[:length], "<f4"), nan=0, posinf=0, neginf=0)
                frames = np.clip(samples, -1, 1).reshape(-1, width)
                times = (consumed + (np.arange(len(frames)) + .5) * width) / SAMPLE_RATE
                values.append(np.column_stack([times, frames.min(axis=1), frames.max(axis=1), np.sqrt((frames * frames).mean(axis=1))]))
                consumed += len(samples)
                pending = pending[length:]
                if progress:
                    progress(min(99, consumed / SAMPLE_RATE / info["duration"] * 100))
            if process.wait(timeout=30):
                errors.seek(0)
                raise ValueError(errors.read().decode(errors="replace")[-500:] or "Audio waveform decoding failed")
            if pending:
                samples = np.clip(np.nan_to_num(np.frombuffer(pending[:len(pending) // 4 * 4], "<f4")), -1, 1)
                if len(samples):
                    values.append(np.array([[(consumed + len(samples) / 2) / SAMPLE_RATE, samples.min(), samples.max(), np.sqrt((samples * samples).mean())]]))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
    all_values = np.concatenate(values) if values else np.empty((0, 4))
    return {k: all_values[:, i] for i, k in enumerate(empty)}


def waveform_trace(data, start, end, shift=0, budget=1600):
    times = data["time"] + shift
    indices = np.flatnonzero((times >= start) & (times <= end))
    points = []
    for block in np.array_split(indices, max(1, min(len(indices), budget))):
        if len(block):
            points.append([float(times[block].mean()), float(data["minimum"][block].min()),
                           float(data["maximum"][block].max()), float(np.sqrt(np.mean(data["rms"][block] ** 2)))])
    return {"points": points, "available": bool(len(times)), "unit": "amplitude −1…1"}


def audio_sections(radio, fpv, offset, seconds=3):
    """Local evidence at exactly the displayed offset; do not search for a new lag."""
    windows = []
    # Fingerprint votes must agree in both time and frequency, with repetitive hashes already removed.
    index = {}
    for hashed, timestamp in zip(fpv["hashes"], fpv["hash_times"], strict=True):
        index.setdefault(int(hashed), []).append(float(timestamp))
    votes = [(float(rt), int(h)) for h, rt in zip(radio["hashes"], radio["hash_times"], strict=True)
             if any(abs(float(rt) - ft - offset) <= .15 for ft in index.get(int(h), []))]
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
                b = np.column_stack([np.interp(radio["time"][indices] - offset, fpv["time"][fi], fpv["features"][fi, col]) for col in range(5)])
                active = [col for col in range(5) if len(indices) >= 20 and min(np.std(a[:, col]), np.std(b[:, col])) > .08]
                corrs = [float(np.corrcoef(a[:, col], b[:, col])[0, 1]) for col in active]
                supported = sum(math.isfinite(c) and c >= .65 for c in corrs)
                local = [(t, h) for t, h in votes if start <= t < stop]
                distinct = len({h for _, h in local})
                span = max((t for t, _ in local), default=start) - min((t for t, _ in local), default=start)
                fingerprint = distinct >= 12 and span >= 1.5
                usable = (len(active) >= 3 and min(float(radio["quality"]), float(fpv["quality"])) > .1) or fingerprint
                matched = usable and (supported >= 3 or fingerprint)
                windows.append({"start": float(start), "end": float(stop), "matched": bool(matched), "usable": bool(usable),
                                "matching_features": supported, "fingerprint_hashes": distinct,
                                "evidence": "spectral fingerprints" if fingerprint else "audio trends"})
    return {"windows": windows, "correlated_sections": sum(w["matched"] for w in windows),
            "usable_sections": sum(w["usable"] for w in windows), "section_seconds": seconds}


def run(request_path):
    request = json.loads(Path(request_path).read_text())
    directory, cache = Path(request["directory"]), Path(request["cache"])
    for kind in ("fpv", "radio"):
        path = request.get(kind + "_path")
        if not path:
            continue
        def progress(percent):
            atomic_json(directory / "progress.json", {"stage": kind, "percent": percent})
        progress(0)
        identity = request.get(kind + "_identity")
        waveform = get_or_extract(path, cache, "waveform", VERSION, {}, lambda: extract_waveform(path, progress), identity)
        audio = get_or_extract(path, cache, "audio", ALGORITHM_VERSION, {"boundary_seconds": 30}, lambda: extract_audio(path), identity)
        stat = Path(path).stat()
        if [stat.st_size, stat.st_mtime_ns] != request["signatures"][kind]:
            raise ValueError("Audio source changed while reading it; reload the waveforms")
        np.savez_compressed(directory / (kind + "-waveform.npz"), **waveform)
        np.savez_compressed(directory / (kind + "-audio.npz"), **audio)
        progress(100)
    atomic_json(directory / "audio-review.json", {"fpv_video": request["fpv_video"], "radio_video": request.get("radio_video")})


if __name__ == "__main__":
    run(sys.argv[1])
