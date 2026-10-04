from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

_PATTERNS = (
    re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})[_T -]?(\d{2})(\d{2})(\d{2})(?!\d)"),
    re.compile(r"(?<!\d)((?:19|20)\d{2})[-_](\d{2})[-_](\d{2})[T _-](\d{2})[-_:](\d{2})[-_:](\d{2})(?!\d)"),
)


def filename_timestamp(filename: str) -> float | None:
    """Parse a camera's clock reading, without assuming it is a correct wall clock.

    Even 1970 dates can carry useful relative ordering. Repeated/reset clock
    readings are excluded from clock consensus by the matching engine.
    """
    for pattern in _PATTERNS:
        match = pattern.search(filename)
        if match:
            try:
                return datetime(*(int(x) for x in match.groups()), tzinfo=timezone.utc).timestamp()
            except ValueError:
                pass
    return None


def format_timestamp(value: float | None) -> str:
    return "" if value is None else datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def probe_video(path: str | Path) -> dict:
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("FFmpeg and ffprobe must be installed")
    result = subprocess.run(
        [executable, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise ValueError(f"Cannot read {Path(path).name}: {result.stderr.strip()[-400:]}")
    data = json.loads(result.stdout)
    video = next(
        (
            s
            for s in data.get("streams", [])
            if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    if video is None:
        raise ValueError(f"No video stream in {Path(path).name}")
    try:
        duration = float(video.get("duration") or data["format"]["duration"])
        fps = float(Fraction(video.get("avg_frame_rate", "0/1")))
    except (ValueError, KeyError, ZeroDivisionError):
        raise ValueError(f"Cannot determine duration/frame rate for {Path(path).name}") from None
    if not math.isfinite(duration) or duration <= 0 or duration > 24 * 3600:
        raise ValueError("Recording duration must be between zero and 24 hours")
    width, height = int(video["width"]), int(video["height"])
    rotation = int(video.get("tags", {}).get("rotate", 0))
    for side in video.get("side_data_list", []):
        rotation = int(side.get("rotation", rotation))
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return {
        "duration": duration,
        "fps": fps,
        "width": width,
        "height": height,
        "codec": video.get("codec_name"),
        "rotation": rotation,
        "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
        "pixel_format": video.get("pix_fmt"),
        "color": {
            k: video.get(k) for k in ("color_space", "color_primaries", "color_transfer", "color_range")
        },
        "size": Path(path).stat().st_size,
    }
