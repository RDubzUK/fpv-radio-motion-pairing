"""Short browser-compatible fragments generated only near the requested playhead."""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from fpv_matcher.video.reader import cuda_decoder_available

VERSION = "preview-fragments-v1"
CHUNK_SECONDS = 4.0
CACHE_LIMIT = 1024 ** 3
_LOCKS = [threading.Lock() for _ in range(64)]
_CLEANUP_LOCK = threading.Lock()
_last_cleanup = 0.0


def source_key(path):
    path = Path(path)
    stat = path.stat()
    return hashlib.sha256(json.dumps([VERSION, str(path.resolve()), stat.st_size, stat.st_mtime_ns]).encode()).hexdigest()


def manifest(path, info):
    audio = bool(info["has_audio"])
    return {"version": VERSION, "source_key": source_key(path), "duration": info["duration"],
            "chunk_seconds": CHUNK_SECONDS, "chunks": math.ceil(info["duration"] / CHUNK_SECONDS),
            "height_limit": 480, "fps": 30, "has_audio": audio,
            "mime": 'video/mp4; codecs="avc1.42c01f' + (', mp4a.40.2"' if audio else '"')}


def prune(cache):
    """Bound the preview-only cache, never touching originals or movement data."""
    global _last_cleanup
    if time.monotonic() - _last_cleanup < 60 or not _CLEANUP_LOCK.acquire(blocking=False):
        return
    try:
        _last_cleanup = time.monotonic()
        files = sorted(cache.glob("*/*.mp4"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for path in files:
            if total <= CACHE_LIMIT:
                break
            # Recently requested/current chunks remain available to FileResponse.
            if time.time() - path.stat().st_mtime < 120:
                continue
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            path.with_suffix(".json").unlink(missing_ok=True)
            total -= size
    finally:
        _CLEANUP_LOCK.release()


def fragment(path, info, index, cache, execute, process_key, cancelled):
    path, cache = Path(path), Path(cache)
    descriptor = manifest(path, info)
    if index < 0 or index >= descriptor["chunks"]:
        raise ValueError("Preview chunk is outside this recording")
    source_id = descriptor["source_key"]
    output = cache / source_id / f"{index:06d}.mp4"
    metadata = output.with_suffix(".json")
    lock = _LOCKS[int(source_id[:8], 16) % len(_LOCKS)]
    while not lock.acquire(timeout=.25):
        if cancelled.is_set():
            raise RuntimeError("Preview request cancelled")
    try:
        if cancelled.is_set():
            raise RuntimeError("Preview request cancelled")
        if output.is_file() and metadata.is_file():
            try:
                details = json.loads(metadata.read_text())
                if not all(math.isfinite(float(details[k])) for k in ("start", "duration", "video_start")):
                    raise ValueError("Invalid cached preview timing")
                os.utime(output, None)
                return output, details
            except (ValueError, KeyError):
                metadata.unlink(missing_ok=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        start = index * CHUNK_SECONDS
        duration = min(CHUNK_SECONDS, info["duration"] - start)
        scale = min(1, 854 / info["width"], 480 / info["height"])
        width, height = [max(2, int(v * scale / 2) * 2) for v in (info["width"], info["height"])]
        temporary = output.with_suffix(".partial.mp4")
        gpu = info["codec"] in {"h264", "hevc"} and not info.get("rotation") and cuda_decoder_available()
        methods = ["cuda", "cpu"] if gpu else ["cpu"]
        encoder = "cpu"
        for method in methods:
            if cancelled.is_set():
                raise RuntimeError("Preview request cancelled")
            command = [shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-y", "-v", "error", "-threads", "2"]
            if method == "cuda":
                command += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-hwaccel_device", "0"]
            command += ["-ss", f"{start:.9f}", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", "-filter_threads", "1"]
            if method == "cuda":
                downloaded = "p010le" if "10" in (info.get("pixel_format") or "") else "nv12"
                filters = f"scale_cuda={width}:{height}:interp_algo=bilinear,hwdownload,format={downloaded},format=yuv420p"
            else:
                filters = f"scale={width}:{height}:flags=fast_bilinear,format=yuv420p"
            command += ["-vf", filters + ",setsar=1,setpts=PTS-STARTPTS,fps=30:start_time=0",
                        "-af", "asetpts=PTS-STARTPTS,aresample=48000:async=1:first_pts=0,apad",
                        "-t", f"{duration:.9f}", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                        "-crf", "27", "-profile:v", "baseline", "-level:v", "3.1", "-pix_fmt", "yuv420p",
                        "-g", "30", "-keyint_min", "30", "-sc_threshold", "0", "-bf", "0", "-threads", "2",
                        "-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-ac", "2",
                        "-avoid_negative_ts", "make_zero", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                        "-f", "mp4", str(temporary)]
            try:
                execute(process_key, command, output.with_suffix(".log"), timeout=120)
                encoder = method
                break
            except RuntimeError:
                if method == methods[-1] or cancelled.is_set():
                    raise
        if cancelled.is_set() or source_key(path) != source_id:
            raise RuntimeError("Preview cancelled or source recording changed; reload the video")
        # fMP4 muxing may shift video by AAC encoder priming. Report the actual
        # first video timestamp so MSE can place every frame on the source clock.
        probe = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0",
                                "-show_entries", "stream=start_time", "-of", "json", str(temporary)],
                               capture_output=True, text=True, timeout=10, check=True)
        video_start = float(json.loads(probe.stdout)["streams"][0].get("start_time", 0))
        if not math.isfinite(video_start):
            raise ValueError("Preview fragment has invalid timing")
        details = {"start": start, "duration": duration, "video_start": video_start, "decoder": encoder}
        os.replace(temporary, output)
        metadata.write_text(json.dumps(details), encoding="utf-8")
        # Logs contain no useful cache data after successful encoding.
        output.with_suffix(".log").unlink(missing_ok=True)
        prune(cache)
        return output, details
    finally:
        output.with_suffix(".partial.mp4").unlink(missing_ok=True)
        lock.release()
