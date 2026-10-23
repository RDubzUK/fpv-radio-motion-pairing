"""Optional decoded cut samples. These check trimming, not flight identity."""
from __future__ import annotations

import shutil
import subprocess
import time

import numpy as np


def _frame(path, at, flag=None):
    command = [shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-ss", f"{max(0, at):.9f}",
               "-i", str(path), "-frames:v", "1", "-filter_threads", "1", "-vf", "scale=96:54,format=gray", "-f", "rawvideo", "pipe:1"]
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    started = time.monotonic()
    try:
        while True:
            if flag and flag.is_set():
                raise RuntimeError("Cancelled")
            if time.monotonic() - started > 45:
                raise RuntimeError("Decoded cut check timed out")
            try:
                data, error = p.communicate(timeout=.5)
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        if p.poll() is None:
            p.kill()
            p.communicate()
    if p.returncode or len(data) != 96 * 54:
        raise RuntimeError(error.decode(errors="replace")[-400:] or "No decodable frame at this checkpoint")
    return np.frombuffer(data, np.uint8).astype(float)


def compare_cut(source, output, start, duration, fps, flag=None):
    samples = []
    for label, at in (("start", min(.1, duration / 10)), ("middle", duration / 2), ("end", max(0, duration - max(.15, 3 / fps)))):
        if flag and flag.is_set():
            raise RuntimeError("Cancelled")
        try:
            actual = _frame(output, at, flag)
            # Seek/frame rounding varies by the container. Record the best
            # expected adjacent source frame, with a bounded one-frame range.
            scores = [(delta, float(np.mean(np.abs(actual - _frame(source, start + at + delta / fps, flag))) / 255))
                      for delta in (-1, 0, 1)]
            delta, difference = min(scores, key=lambda item: item[1])
            samples.append({"position": label, "output_time": at, "source_time": start + at + delta / fps,
                            "frame_rounding": delta, "mean_difference": difference,
                            "status": "consistent" if difference < .035 else "review"})
        except RuntimeError as exc:
            if flag and flag.is_set():
                raise
            samples.append({"position": label, "status": "unavailable", "reason": str(exc)})
    return {"samples": samples, "status": "consistent" if all(s["status"] == "consistent" for s in samples) else "review",
            "note": "Low-resolution pixel comparisons allow one source-frame rounding. Static scenes cannot establish exact timing; this does not verify that two recordings cover the same flight."}
