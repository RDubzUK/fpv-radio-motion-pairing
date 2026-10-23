"""Camera telemetry for review. Sensor axes are not claimed to be drone axes."""
from __future__ import annotations

import numpy as np
from scipy.spatial.transform import Rotation

VERSION = "camera-telemetry-d45ebf2-quaternion-rates-v4"


def cached_telemetry(path, cache, identity=None):
    """Share telemetry across scans/reviews; remember unsupported sources too."""
    from fpv_matcher.cache.fingerprints import get_or_extract

    def extract():
        try:
            return extract_telemetry(path)
        except ValueError as exc:
            return {"unavailable": np.asarray(str(exc))}

    data = get_or_extract(str(path), cache, "telemetry", VERSION, {}, extract, identity)
    if "unavailable" in data:
        raise ValueError(str(data["unavailable"].item()))
    return data


def quaternion_rates(times, quaternions):
    """Local angular velocity from camera-to-world xyzw rotations, with gap rejection."""
    times, quaternions = np.asarray(times, float), np.asarray(quaternions, float)
    norms = np.linalg.norm(quaternions, axis=1)
    valid = np.isfinite(times) & np.isfinite(quaternions).all(axis=1) & (norms > 0.5)
    times, quaternions = times[valid], quaternions[valid]
    if len(times) < 4:
        raise ValueError("Not enough valid attitude samples")
    order = np.argsort(times, kind="stable")
    times, quaternions = times[order], quaternions[order]
    keep = np.r_[True, np.diff(times) > 1e-8]
    times, quaternions = times[keep], quaternions[keep]
    if len(times) < 3:
        raise ValueError("Not enough valid attitude samples")
    rotations = Rotation.from_quat(quaternions)
    delta = np.diff(times)
    rates = np.degrees((rotations[:-1].inv() * rotations[1:]).as_rotvec()) / delta[:, None]
    quality = ((delta > 0) & (delta <= max(0.05, 5 * np.median(delta)))).astype(float)
    if np.count_nonzero(quality) < 3:
        raise ValueError("Not enough continuous attitude samples")
    rates[quality == 0] = 0
    result = {"time": (times[:-1] + times[1:]) / 2, "x": rates[:, 0], "y": rates[:, 1], "z": rates[:, 2], "quality": quality}
    for i, key in enumerate(("qx", "qy", "qz", "qw")):
        result[key] = rotations.as_quat()[:-1, i]
    return result


def extract_telemetry(path):
    import telemetry_parser

    parser = telemetry_parser.Parser(str(path))
    imu = parser.normalized_imu()
    camera = " ".join(filter(None, (parser.camera, parser.model))) or "Camera"
    if imu:
        rows = [r for r in imu if r.get("gyro") is not None]
        if rows:
            time = np.asarray([r["timestamp_ms"] / 1000 for r in rows], float)
            vectors = np.asarray([r["gyro"] for r in rows], float)
            if vectors.shape != (len(time), 3):
                raise ValueError("Unsupported gyroscope sample layout")
            valid = np.isfinite(time) & np.isfinite(vectors).all(axis=1)
            time, vectors = time[valid], vectors[valid]
            if len(time) < 3:
                raise ValueError("Not enough valid embedded gyroscope samples")
            order = np.argsort(time, kind="stable")
            time, vectors = time[order], vectors[order]
            keep = np.r_[True, np.diff(time) > 1e-8]
            time, vectors = time[keep], vectors[keep]
            if len(time) < 3:
                raise ValueError("Not enough valid embedded gyroscope samples")
            data = {"time": time, "x": vectors[:, 0], "y": vectors[:, 1], "z": vectors[:, 2],
                    "quality": np.ones(len(time))}
            return {**data, "label": np.asarray(f"{camera} · embedded gyroscope"),
                    "kind": np.asarray("gyro"), "note": np.asarray("Parser-normalised sensor X/Y/Z, degrees/second. Camera mount axes and gyro-to-video timing have not been calibrated for drone matching.")}
    times, quaternions = [], []
    for sample in parser.telemetry():
        for row in sample.get("Quaternion", {}).get("Data", []):
            value = row["v"]
            times.append(row["t"] / 1000)
            quaternions.append([value["x"], value["y"], value["z"], value["w"]])
    if not times:
        raise ValueError("No supported embedded gyroscope or attitude samples found")
    data = quaternion_rates(times, quaternions)
    return {**data, "label": np.asarray(f"{camera} · angular rates derived from embedded attitude"),
            "kind": np.asarray("quaternion_rates"), "note": np.asarray("Derived from fused camera quaternions; these are not raw gyro samples. X/Y/Z remain in the parser camera basis, without drone mount correction. Timestamp alignment follows the parser and has not been independently verified.")}


def validate_motion_settings(source="auto", uptilt=0, orientation="zXy", time_offset=0):
    if source not in {"auto", "gyro", "visual"}:
        raise ValueError("Choose automatic, embedded or visual FPV motion")
    uptilt, time_offset = float(uptilt), float(time_offset)
    if not np.isfinite(uptilt) or not -90 <= uptilt <= 90:
        raise ValueError("Camera uptilt must be between −90 and 90 degrees")
    if not np.isfinite(time_offset) or not -30 <= time_offset <= 30:
        raise ValueError("Gyro-to-video shift must be between −30 and 30 seconds")
    if not isinstance(orientation, str) or len(orientation) != 3 or set(orientation.upper()) != set("XYZ"):
        raise ValueError("Sensor mapping must use each of X/Y/Z once; lowercase reverses an axis")
    matrix = np.asarray([[(-1 if c.islower() else 1) if j == "XYZ".index(c.upper()) else 0 for j in range(3)] for c in orientation])
    if np.linalg.det(matrix) < .99:
        raise ValueError("Sensor mapping must describe a physical rotation, with determinant +1")
    return source, uptilt, orientation, time_offset


def body_rates(data, uptilt=0, orientation="zXy", time_offset=0):
    """Camera rates to body forward/right/down axes, then undo camera uptilt."""
    _, uptilt, orientation, time_offset = validate_motion_settings("auto", uptilt, orientation, time_offset)
    vectors = np.column_stack([data[k] for k in ("x", "y", "z")])
    base = np.column_stack([vectors[:, "xyz".index(c.lower())] * (-1 if c.islower() else 1) for c in orientation])
    # Positive uptilt points the camera's forward axis above the drone's forward axis.
    transform = Rotation.from_euler("y", uptilt, degrees=True)
    body = transform.apply(base)
    times = np.asarray(data["time"], float) + time_offset
    keep = np.isfinite(times) & (times >= 0)
    quality = np.asarray(data["quality"], float).copy()
    quality[~np.isfinite(body).all(axis=1)] = 0
    result = {"time": times[keep], "roll": body[keep, 0], "pitch": body[keep, 1],
              "yaw": body[keep, 2], "quality": quality[keep], "_body_rates": np.asarray(True),
              "motion_source": data["kind"], "motion_label": data["label"]}
    if np.count_nonzero(result["quality"] > 0) < 3:
        raise ValueError("No usable telemetry within the video timeline")
    return result
