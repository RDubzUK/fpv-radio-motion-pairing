"""Optional Betaflight rates curves for mapping visible stick positions to setpoints."""

from __future__ import annotations

import copy
import math

import numpy as np


def validate_rate_profile(profile: dict | None) -> dict | None:
    if profile is None:
        return None
    if not isinstance(profile, dict) or profile.get("model") not in {"actual", "betaflight", "kiss"}:
        raise ValueError("Choose the ACTUAL, legacy Betaflight or KISS rates model")
    result = copy.deepcopy(profile)
    for axis in ("roll", "pitch", "yaw"):
        params = result.get(axis)
        if not isinstance(params, dict):
            raise ValueError(f"Enter the {axis} rates")
        if result["model"] == "actual":
            names = (("center_dps", 0, 1998), ("max_dps", 0, 1998), ("expo", 0, 1))
        elif result["model"] == "betaflight":
            names = (("rc_rate", 0, 255), ("super_rate", 0, 100), ("expo", 0, 100))
        else:
            names = (("rc_rate", 0, 2.55), ("rate", 0, 1), ("rc_curve", 0, 1))
        for name, minimum, maximum in names:
            try:
                value = float(params[name])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Enter a numeric {axis} {name.replace('_', ' ')}") from exc
            if not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError(
                    f"{axis.title()} {name.replace('_', ' ')} must be between {minimum} and {maximum}"
                )
            params[name] = value
        if result["model"] == "actual" and params["max_dps"] < params["center_dps"]:
            raise ValueError(f"{axis.title()} maximum rate must be at least the centre rate")
    return result


def _actual(stick: np.ndarray, values: dict) -> np.ndarray:
    center = values["center_dps"]
    expo = values["expo"]
    movement = max(0.0, values["max_dps"] - center)
    return stick * center + movement * np.abs(stick) * (np.power(stick, 5) * expo + stick * (1 - expo))


def _betaflight(stick: np.ndarray, values: dict) -> np.ndarray:
    expo = values["expo"] / 100
    command = stick * np.power(np.abs(stick), 3) * expo + stick * (1 - expo)
    rc_rate = values["rc_rate"] / 100
    if rc_rate > 2:
        rc_rate += 14.54 * (rc_rate - 2)
    rate = 200 * rc_rate * command
    if values["super_rate"]:
        divisor = np.maximum(1 - np.abs(command) * values["super_rate"] / 100, 0.01)
        rate *= 1 / divisor
    return rate


def _kiss(stick: np.ndarray, values: dict) -> np.ndarray:
    absolute = np.abs(stick)
    command = stick**3 * values["rc_curve"] + stick * (1 - values["rc_curve"])
    divisor = np.maximum(1 - absolute * values["rate"], 0.01)
    return np.clip(2000 * (values["rc_rate"] / 10) * command / divisor, -1998, 1998)


def apply_rate_profile(data: dict[str, np.ndarray], profile: dict | None) -> dict[str, np.ndarray]:
    """Return a transformed copy; cached stick positions remain unchanged."""
    profile = validate_rate_profile(profile)
    if profile is None:
        return data
    transform = {
        "actual": _actual,
        "betaflight": _betaflight,
        "kiss": _kiss,
    }[profile["model"]]
    result = dict(data)
    for axis in ("roll", "pitch", "yaw"):
        result[axis] = transform(np.asarray(data[axis], dtype=float), profile[axis])
    return result
