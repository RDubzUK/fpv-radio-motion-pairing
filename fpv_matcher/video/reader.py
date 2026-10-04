from __future__ import annotations

import math
import os
from functools import lru_cache
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from fpv_matcher.video.metadata import probe_video

EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}


@lru_cache(maxsize=1)
def cuda_decoder_available():
    if not Path("/dev/nvidia0").exists():
        return False
    result = subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-filters"],
                            capture_output=True, text=True, timeout=10)
    return "scale_cuda" in result.stdout


def sampled_frames(
    path: str | Path,
    sample_fps: float,
    max_dimension: int | None = None,
    start: float = 0,
    duration: float | None = None,
) -> Iterator[tuple[float, np.ndarray]]:
    """Sample onto a source-relative time grid; optionally decode/resize on CUDA.

    Only reduced grayscale frames cross into Python. Automatic CUDA falls back
    before yielding a frame; a midstream failure is reported rather than spliced.
    """
    if not math.isfinite(sample_fps) or not 0 < sample_fps <= 120 or not math.isfinite(start) or start < 0:
        raise ValueError("Invalid sampling rate or start time")
    info = probe_video(path)
    scale = min(1.0, (max_dimension or max(info["width"], info["height"])) / max(info["width"], info["height"]))
    width = max(2, round(info["width"] * scale / 2) * 2)
    height = max(2, round(info["height"] * scale / 2) * 2)
    mode = os.environ.get("FPV_MATCHER_DECODE", "auto")
    if mode not in {"auto", "cpu", "cuda"}:
        raise ValueError("Decode mode must be auto, cpu or cuda")
    can_try = info["codec"] in {"h264", "hevc"} and cuda_decoder_available()
    methods = (["cuda", "cpu"] if can_try else ["cpu"]) if mode == "auto" else [mode]
    # Rotation metadata is applied by FFmpeg; CPU handles rotated streams reliably.
    if mode == "auto" and info.get("rotation", 0):
        methods = ["cpu"]
    for method in methods:
        fps_filter = f"fps={sample_fps:.8g}:start_time=0:round=near"
        pixel_format = info.get("pixel_format") or "yuv420p"
        download_format = ("p010le" if "10" in pixel_format else "p016le" if "16" in pixel_format else "nv12")
        if "444" in pixel_format:
            download_format = "yuv444p16le" if "10" in pixel_format or "16" in pixel_format else "yuv444p"
        # Older scale_cuda builds preserve the decoder's format and have no
        # format option. hwdownload must negotiate that exact software format.
        filters = (f"{fps_filter},scale_cuda={width}:{height}:interp_algo=bilinear,hwdownload,format={download_format},format=gray"
                   if method == "cuda" else f"{fps_filter},scale={width}:{height}:flags=area,format=gray")
        command = [shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-v", "error", "-threads", "2"]
        if method == "cuda":
            command += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-hwaccel_device", "0"]
        command += ["-ss", f"{start:.9f}", "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-filter_threads", "1", "-vf", filters]
        if duration is not None:
            command += ["-t", str(duration)]
        command += ["-f", "rawvideo", "-pix_fmt", "gray", "-threads", "1", "pipe:1"]
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
            frame_bytes, index = width * height, 0
            try:
                while True:
                    raw = process.stdout.read(frame_bytes)
                    if not raw:
                        break
                    if len(raw) != frame_bytes:
                        raise ValueError(f"Incomplete decoded frame in {Path(path).name}")
                    yield start + index / sample_fps, np.frombuffer(raw, np.uint8).reshape(height, width)
                    index += 1
                code = process.wait(timeout=60)
                if code or not index:
                    if not index and method == "cuda" and mode == "auto":
                        errors.seek(0)
                        print(f"CUDA decoding unavailable for {Path(path).name}; falling back to CPU: {errors.read().decode(errors='replace')[-250:]}", flush=True)
                        continue
                    errors.seek(0)
                    raise ValueError(f"{method.upper()} decode failed for {Path(path).name}: {errors.read().decode(errors='replace')[-500:]}")
                return
            finally:
                process.stdout.close()
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


def reverse_sampled_frames(path, sample_fps, max_dimension, reference_time):
    """Walk backwards from a calibrated frame using bounded reduced-frame chunks."""
    seed = next(sampled_frames(path, sample_fps, max_dimension, start=reference_time, duration=1 / sample_fps), None)
    if seed is None:
        raise ValueError("Cannot decode the calibration frame for backward tracking")
    yield seed
    # A chunk holds at most 60 reduced grayscale frames, never the entire source clip.
    remaining = max(0, math.floor(reference_time * sample_fps + 1e-8))
    while remaining:
        count = min(60, remaining)
        end = reference_time - (math.floor(reference_time * sample_fps + 1e-8) - remaining) / sample_fps
        start = end - count / sample_fps
        frames = list(sampled_frames(path, sample_fps, max_dimension, start=max(0, start), duration=count / sample_fps))
        for timestamp, frame in reversed(frames):
            if timestamp < end - 1e-6:
                yield timestamp, frame
        remaining -= count
    # The anchored sampling grid may leave less than one sample interval before its first frame.
    if reference_time - math.floor(reference_time * sample_fps + 1e-8) / sample_fps > 1e-6:
        first = next(sampled_frames(path, sample_fps, max_dimension, duration=1 / sample_fps), None)
        if first is not None:
            yield first


def video_files(path: str | Path) -> list[Path]:
    p = Path(path).expanduser().resolve()
    if p.is_file():
        return [p] if p.suffix.lower() in EXTENSIONS else []
    if not p.is_dir():
        raise ValueError(f"Input location does not exist: {p}")
    return sorted(x for x in p.iterdir() if x.is_file() and x.suffix.lower() in EXTENSIONS)
