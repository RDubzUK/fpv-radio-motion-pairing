from __future__ import annotations

import copy
import math


def finite(value, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def validate_calibration(config: dict, dimensions: tuple[int, int] | None = None) -> dict:
    if not isinstance(config, dict):
        raise ValueError("Calibration must be a JSON object")
    result = copy.deepcopy(config)
    mode = result.setdefault("stickcam_mode", "fixed")
    if mode not in {"fixed", "wonky"}:
        raise ValueError("StickCam mode must be fixed or wonky")
    result["reference_time"] = finite(result.get("reference_time", 0), "Reference time")
    if result["reference_time"] < 0:
        raise ValueError("Reference time must be non-negative")
    for side in ("left_gimbal", "right_gimbal"):
        spec = result.get(side)
        if not isinstance(spec, dict):
            raise ValueError(f"Calibrate {side.replace('_', ' ')}")
        for key in ("x", "y", "width", "height"):
            spec[key] = finite(spec[key], key)
        if min(spec["x"], spec["y"]) < 0 or min(spec["width"], spec["height"]) < 8:
            raise ValueError("Gimbal regions must be inside the frame and at least 8 pixels across")
        if dimensions and (
            spec["x"] + spec["width"] > dimensions[0] + 1 or spec["y"] + spec["height"] > dimensions[1] + 1
        ):
            raise ValueError("Gimbal region extends outside the source frame")
        for key, default in (
            ("center_x", spec["width"] / 2),
            ("center_y", spec["height"] / 2),
            ("travel_x", spec["width"] * 0.4),
            ("travel_y", spec["height"] * 0.4),
            ("tip_radius", min(spec["width"], spec["height"]) * 0.035),
            ("threshold", 70),
        ):
            spec[key] = finite(spec.get(key, default), key)
        if (
            min(spec["travel_x"], spec["travel_y"], spec["tip_radius"]) <= 0
            or not 0 <= spec["threshold"] <= 255
        ):
            raise ValueError("Invalid travel range, tip radius or threshold")
        if ("tip_x" in spec) != ("tip_y" in spec):
            raise ValueError("Both tip coordinates are required")
        for key, limit in (("tip_x", spec["width"]), ("tip_y", spec["height"])):
            if key in spec:
                spec[key] = finite(spec[key], key)
                if not 0 <= spec[key] < limit:
                    raise ValueError("Stick tips must be inside their gimbal region")
        if spec.get("polarity", "dark") not in {"dark", "light"}:
            raise ValueError("Tip polarity must be dark or light")
    if "radio_outline" in result:
        outline = result["radio_outline"]
        if not isinstance(outline, list) or len(outline) != 4:
            raise ValueError("Radio outline needs four corner points")
        points = []
        for point in outline:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError("Radio outline points must each contain x and y")
            x, y = finite(point[0], "Radio outline x"), finite(point[1], "Radio outline y")
            if x < 0 or y < 0 or dimensions and (x >= dimensions[0] or y >= dimensions[1]):
                raise ValueError("Radio outline points must be inside the source frame")
            points.append([x, y])
        edges = [
            (points[(index + 1) % 4][0] - points[index][0])
            * (points[(index + 2) % 4][1] - points[(index + 1) % 4][1])
            - (points[(index + 1) % 4][1] - points[index][1])
            * (points[(index + 2) % 4][0] - points[(index + 1) % 4][0])
            for index in range(4)
        ]
        area = (
            abs(
                sum(
                    points[index][0] * points[(index + 1) % 4][1]
                    - points[(index + 1) % 4][0] * points[index][1]
                    for index in range(4)
                )
            )
            / 2
        )
        if area < 100 or not (all(edge > 0 for edge in edges) or all(edge < 0 for edge in edges)):
            raise ValueError("Radio outline must be four corners in order around the radio")
        result["radio_outline"] = points
    signs = result.setdefault("signs", {})
    for axis in ("roll", "pitch", "yaw", "throttle"):
        signs[axis] = finite(signs.get(axis, 1), f"{axis} sign")
        if signs[axis] not in {-1, 1}:
            raise ValueError("Axis signs must be +1 or -1")
    return result


def calibration_for(config: dict, name: str) -> dict:
    # CLI supports the original global config and a per-recording configuration.
    value = config.get("clips", {}).get(name, config.get("default", config))
    return validate_calibration(value)
