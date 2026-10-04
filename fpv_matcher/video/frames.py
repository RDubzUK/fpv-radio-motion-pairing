"""Decode a bounded calibration still independently of browser codec support."""
import shutil
import subprocess


def jpeg_frame(path, at):
    result = subprocess.run([shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-v", "error", "-threads", "1",
        "-ss", str(at), "-i", str(path), "-frames:v", "1", "-filter_threads", "1",
        "-vf", "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease",
        "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"], capture_output=True, timeout=60)
    if result.returncode or not result.stdout:
        raise ValueError("Could not decode this calibration frame")
    return result.stdout
