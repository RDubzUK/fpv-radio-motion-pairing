"""Resolve StickCam sampling independently of FPV extraction/comparison Hz."""
from __future__ import annotations

import math


def validate_ratio(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Choose fixed Hz, half (0.5), or quarter (0.25) of StickCam frames")
    try:
        ratio = float(value)
    except (ValueError, TypeError):
        raise ValueError("Invalid StickCam sampling ratio") from None
    if ratio not in {0.5, 0.25}:
        raise ValueError("Choose fixed Hz, half (0.5), or quarter (0.25) of StickCam frames")
    return ratio


def tracking_fps(fixed_fps, ratio=None, source_fps=None):
    ratio = validate_ratio(ratio)
    if ratio is None:
        fps = float(fixed_fps)
    else:
        if source_fps is None or not math.isfinite(float(source_fps)) or float(source_fps) <= 0:
            raise ValueError("Cannot determine StickCam frame rate; choose fixed Hz instead")
        fps = round(float(source_fps) * ratio, 8)
    if not math.isfinite(fps) or not 0 < fps <= 60:
        raise ValueError("StickCam tracking rate must be greater than zero and at most 60 Hz; choose a smaller ratio or fixed Hz")
    return fps
