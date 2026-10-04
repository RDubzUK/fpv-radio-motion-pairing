"""A bounded recent input preview carried in ordinary extraction progress."""
from __future__ import annotations

from collections import deque


class LiveStickInputs:
    def __init__(self, fps, window_seconds=20):
        self.rows = deque(maxlen=max(2, min(1200, round(fps * window_seconds))))
        self.window_seconds = window_seconds
        self.maximum_step = 2 / fps + 1e-6
        self.direction = None

    def append(self, timestamp, values):
        if self.rows:
            delta = timestamp - self.rows[-1]["time"]
            direction = delta > 0
            if abs(delta) > self.maximum_step or (self.direction is not None and self.direction != direction):
                self.rows.clear()
            self.direction = direction
        self.rows.append({"time": timestamp, **{key: values[key] for key in
            ("throttle", "yaw", "pitch", "roll", "throttle_quality", "yaw_quality", "pitch_quality", "roll_quality")}})

    def snapshot(self):
        if not self.rows:
            return None
        return {"time": [round(float(row["time"]), 5) for row in self.rows],
                "channels": {axis: [round(float(row[axis]), 4) if row[axis + "_quality"] > .1 else None
                                    for row in self.rows] for axis in ("throttle", "yaw", "pitch", "roll")},
                "quality": {axis: [round(float(row[axis + "_quality"]), 3) for row in self.rows]
                            for axis in ("throttle", "yaw", "pitch", "roll")},
                "stick_mode": 2, "window_seconds": self.window_seconds}


def remap_preview(preview, mode, signs=None):
    """Use the same physical assignments as the final saved cache."""
    if preview is None:
        return None
    from fpv_matcher.radio.modes import MODES, validate_mode
    signs = signs or {}
    result = {**preview, "channels": {}, "quality": {}, "stick_mode": validate_mode(mode)}
    for physical, old_axis in MODES[2].items():
        axis = MODES[result["stick_mode"]][physical]
        factor = signs.get(old_axis, 1) * signs.get(axis, 1)
        result["channels"][axis] = [None if v is None else v * factor for v in preview["channels"][old_axis]]
        result["quality"][axis] = preview["quality"][old_axis]
    return result
