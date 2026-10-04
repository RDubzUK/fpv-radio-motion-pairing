"""Resolve visual sampling per clip without changing StickCam extraction."""
from __future__ import annotations

import math

MODES = {"auto", "half", "quarter", "custom"}


def visual_fps(mode="auto", fixed_fps=30, source_fps=None):
    if mode not in MODES:
        raise ValueError("Choose automatic, half, quarter or custom FPV visual sampling")
    fixed = float(fixed_fps)
    if not math.isfinite(fixed) or not 1 <= fixed <= 30:
        raise ValueError("Choose a custom FPV visual extraction rate from 1 to 30 Hz")
    if mode == "custom":
        return fixed
    if source_fps is None or not math.isfinite(float(source_fps)) or float(source_fps) <= 0:
        raise ValueError("Cannot detect this FPV video's frame rate; choose custom Hz instead")
    ratio = {"auto": 1., "half": .5, "quarter": .25}[mode]
    return round(max(1., min(30., float(source_fps) * ratio)), 8)
