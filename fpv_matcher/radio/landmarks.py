"""Source-frame stick-tip observations used as timed tracking anchors."""
from __future__ import annotations

import math

VERSION = "timed-tip-landmarks-v1"
TIP_KEYS = ("left_tip", "right_tip")
LANDMARK_KEYS = (*TIP_KEYS, "calibration")


def validate_landmarks(points, duration=None, dimensions=None):
    if not isinstance(points, list) or len(points) > 5000:
        raise ValueError("Tracking landmarks must be a list of at most 5,000 points")
    merged = {}
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            raise ValueError(f"Tracking landmark {index + 1} must be an object")
        at = point.get("time")
        if (isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at)
                or at < 0 or duration is not None and at >= duration):
            raise ValueError(f"Tracking landmark {index + 1} must be inside the video")
        at = round(float(at), 4)
        if duration is not None and at >= duration:
            raise ValueError(f"Tracking landmark {index + 1} must be inside the video")
        cleaned = {"time": at}
        for side in TIP_KEYS:
            position = point.get(side)
            if position is None:
                continue
            if (not isinstance(position, (list, tuple)) or len(position) != 2
                    or any(isinstance(v, bool) or not isinstance(v, (int, float))
                           or not math.isfinite(v) or not 0 <= v <= 1 for v in position)):
                raise ValueError(f"Tracking landmark {index + 1} has invalid {side} coordinates")
            cleaned[side] = [round(float(v), 6) for v in position]
        if "calibration" in point:
            from fpv_matcher.config import validate_calibration
            snapshot = point["calibration"]
            if not isinstance(snapshot, dict) or snapshot.get("exclude") or snapshot.get("skip_motion"):
                raise ValueError(f"Tracking landmark {index + 1} needs a complete frame calibration")
            # Keep one frame's geometry, excluding recursive landmarks and
            # session-wide channel assignments or diagnostic flags.
            keys = ("left_gimbal", "right_gimbal", "radio_outline", "stickcam_mode", "radio_rotation_deg")
            snapshot = validate_calibration({key: snapshot[key] for key in keys if key in snapshot}, dimensions)
            if any("tip_x" not in snapshot[side] for side in ("left_gimbal", "right_gimbal")):
                raise ValueError(f"Tracking landmark {index + 1} needs both marked stick tips")
            cleaned["calibration"] = {key: snapshot[key] for key in keys if key in snapshot}
        if len(cleaned) == 1:
            raise ValueError(f"Tracking landmark {index + 1} needs a left or right stick tip")
        merged.setdefault(at, {"time": at}).update(cleaned)
    return [merged[at] for at in sorted(merged)]


def from_annotations(annotations, duration=None, dimensions=None):
    # Expected control values and notes remain diagnostic labels. Only image
    # observations may seed tracking or become measured matching evidence.
    return validate_landmarks([
        {"time": point["time"], **{key: point[key] for key in LANDMARK_KEYS if key in point}}
        for point in annotations if any(key in point for key in LANDMARK_KEYS)
    ], duration, dimensions)


def landmark_frames(frames, landmarks, decode, reverse=False):
    """Insert exact marked frames into the sampled timeline, with bounded memory.

    Off-grid landmarks need one short decode each. Existing sampled frames are
    reused when their timestamps agree; no stick positions are interpolated.
    """
    ordered = iter(sorted(landmarks, key=lambda point: point["time"], reverse=reverse))
    pending = next(ordered, None)
    direction = -1 if reverse else 1
    try:
        for timestamp, frame in frames:
            while pending is not None and direction * (pending["time"] - timestamp) < -1e-6:
                yield pending["time"], decode(pending["time"])
                pending = next(ordered, None)
            if pending is not None and abs(pending["time"] - timestamp) <= 1e-6:
                # Use the marked time itself to keep cache events reproducible.
                timestamp = pending["time"]
                pending = next(ordered, None)
            yield timestamp, frame
        while pending is not None:
            yield pending["time"], decode(pending["time"])
            pending = next(ordered, None)
    finally:
        if hasattr(frames, "close"):
            frames.close()
