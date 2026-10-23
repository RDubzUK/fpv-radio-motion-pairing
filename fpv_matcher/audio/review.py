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

from fpv_matcher.audio.fingerprint import ALGORITHM_VERSION, SAMPLE_RATE, extract_audio, validate_audio, validate_audio
from fpv_matcher.audio.evidence import audio_sections
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
        boundary = request.get("boundary_seconds", 30)
        audio = get_or_extract(path, cache, "audio", ALGORITHM_VERSION, {"boundary_seconds": boundary}, lambda: extract_audio(path, boundary), identity, validate=validate_audio)
        stat = Path(path).stat()
        if [stat.st_size, stat.st_mtime_ns] != request["signatures"][kind]:
            raise ValueError("Audio source changed while reading it; reload the waveforms")
        np.savez_compressed(directory / (kind + "-waveform.npz"), **waveform)
        np.savez_compressed(directory / (kind + "-audio.npz"), **audio)
        progress(100)
    atomic_json(directory / "audio-review.json", {"fpv_video": request["fpv_video"], "radio_video": request.get("radio_video")})


if __name__ == "__main__":
    run(sys.argv[1])
