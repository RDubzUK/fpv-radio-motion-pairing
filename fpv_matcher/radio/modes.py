"""Physical stick assignments, independent of image orientation and channel order."""
from __future__ import annotations

import numpy as np

MODES = {
    1: {"left_x": "yaw", "left_y": "pitch", "right_x": "roll", "right_y": "throttle"},
    2: {"left_x": "yaw", "left_y": "throttle", "right_x": "roll", "right_y": "pitch"},
    3: {"left_x": "roll", "left_y": "pitch", "right_x": "yaw", "right_y": "throttle"},
    4: {"left_x": "roll", "left_y": "throttle", "right_x": "yaw", "right_y": "pitch"},
}


def validate_mode(value=2):
    if isinstance(value, bool) or value not in (1, 2, 3, 4, "1", "2", "3", "4"):
        raise ValueError("Choose radio stick Mode 1, 2, 3 or 4")
    return int(value)


def assigned_inputs(left, right, mode=2):
    """Horizontal right / vertical up positive, in physical upright radio axes."""
    values = {}
    for physical, axis in MODES[validate_mode(mode)].items():
        position = left if physical.startswith("left") else right
        values[axis] = position[0] if physical.endswith("x") else -position[1]
        values[axis + "_quality"] = position[2]
    return values


def remap_cached_inputs(data, mode=2, signs=None):
    """Map canonical Mode 2 cache channels without re-scanning the video.

    Older caches already store Mode 2 semantic channels. Undo their signs to
    recover physical positions, then apply the pilot's selected assignments.
    """
    mode, signs = validate_mode(mode), signs or {}
    result = dict(data)
    for physical, old_axis in MODES[2].items():
        result[physical] = np.asarray(data[old_axis]) * signs.get(old_axis, 1)
        result[physical + "_quality"] = np.asarray(data.get(old_axis + "_quality", data["quality"]))
    for physical, axis in MODES[mode].items():
        result[axis] = result[physical] * signs.get(axis, 1)
        result[axis + "_quality"] = result[physical + "_quality"]
    result["stick_mode"] = np.asarray(mode)
    return result
