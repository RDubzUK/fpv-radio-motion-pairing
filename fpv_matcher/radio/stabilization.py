"""Radio review poses. Outline observations anchor camera gyro gap prediction."""
from __future__ import annotations

import cv2
import numpy as np

from fpv_matcher.radio.stick_tracker import MAX_DIMENSION, iter_all_sticks
from fpv_matcher.video.metadata import probe_video

VERSION = "radio-review-perspective-valid-gaps-v2"


def extract_review_sticks(path, sample_fps, calibration, progress=None):
    rows = []
    info = probe_video(path)
    from fpv_matcher.progress import ExtractionProgress
    reporter = ExtractionProgress(info["duration"], 0, progress)
    scale = min(1, MAX_DIMENSION / max(info["width"], info["height"]))
    for timestamp, values, _, trackers in iter_all_sticks(path, sample_fps, calibration):
        row = {"time": timestamp, **values}
        if "radio_outline" in calibration:
            tracker = trackers[-1]
            corners = cv2.perspectiveTransform(tracker.outline[None], tracker.transform)[0] / scale
            for i, point in enumerate(corners):
                row[f"outline_{i}_x"], row[f"outline_{i}_y"] = point
        reporter.update(len(rows) / sample_fps)
        rows.append(row)
    if not rows:
        raise ValueError("No StickCam tracking samples")
    reporter.update(0, complete=True)
    order = np.argsort([r["time"] for r in rows])
    data = {key: np.asarray([r[key] for r in rows], float)[order] for key in rows[0]}
    if calibration.get("radio_rotation_deg") == 180:
        data["radio_reference_rotation"] = np.asarray(180)
    return data


def review_poses(data, gyro=None):
    """Store source-pixel corners; use gyro only to bridge short, locally supported gaps."""
    if "outline_0_x" not in data:
        return [], "Mark and save the radio outline to enable the centred preview."
    times = data["time"]
    corners = np.asarray([np.column_stack((data[f"outline_{i}_x"], data[f"outline_{i}_y"])) for i in range(4)]).transpose(1, 0, 2)
    if float(data.get("radio_reference_rotation", 0)) == 180:
        # Pose consumers level the edge from corner 0 to 1. Reorder corners
        # into the radio's physical upright order without moving source pixels.
        corners = np.roll(corners, 2, axis=1)
    quality = data["radio_orientation_quality"]
    angles = np.unwrap(np.arctan2(corners[:, 1, 1] - corners[:, 0, 1], corners[:, 1, 0] - corners[:, 0, 0]))
    gyro_angle = None
    gyro_factor = None
    note = "Outline tracking centres, levels and fits the whole visible radio with a margin."
    if gyro is not None and len(gyro["time"]) > 2 and len(times) > 10:
        gt = gyro["time"]
        z = np.radians(gyro["z"])
        integral = np.r_[0, np.cumsum((z[1:] + z[:-1]) / 2 * np.diff(gt))]
        gyro_angle = np.interp(times, gt, integral, left=np.nan, right=np.nan)
        da, dg = np.diff(angles), np.diff(gyro_angle)
        good = (quality[1:] >= .5) & (quality[:-1] >= .5) & np.isfinite(dg) & (np.abs(dg) > .002) & (np.abs(da) < .2)
        if good.sum() >= 30 and np.std(da[good]) > .002 and np.std(dg[good]) > .002:
            correlation = np.corrcoef(da[good], dg[good])[0, 1]
            factor = np.dot(da[good], dg[good]) / np.dot(dg[good], dg[good])
            if abs(correlation) >= .75 and .75 <= abs(factor) <= 1.25:
                gyro_factor = float(factor)
                note += " Camera gyro supports short tracking gaps after agreeing with the observed outline; it is not used as a control signal."
        if gyro_factor is None:
            note += " Camera motion was decoded, but did not establish reliable optical-axis agreement with the radio; gyro assistance is disabled."
    else:
        note += " No usable camera gyro support; outline tracking is used."
    poses, anchor = [], None
    for i, timestamp in enumerate(times):
        current = corners[i]
        source = "outline"
        valid = quality[i] >= .35
        if valid:
            anchor = i
        elif quality[i] > 0 and anchor is not None and gyro_factor is not None and timestamp - times[anchor] <= .3 and np.isfinite(gyro_angle[i]) and np.isfinite(gyro_angle[anchor]):
            # Camera gyro cannot establish whether the radio is in frame or
            # where it translated. Zero-quality shell loss stays a gap.
            delta = gyro_factor * (gyro_angle[i] - gyro_angle[anchor])
            c, s = np.cos(delta), np.sin(delta)
            center = corners[anchor].mean(axis=0)
            current = (corners[anchor] - center) @ np.array([[c, s], [-s, c]]) + center
            valid, source = True, "gyro-assisted"
        # Invalid intervals stay explicit; a stale crop must not silently hide the radio.
        poses.append([round(float(timestamp), 5), current.round(2).tolist() if valid else None, source])
    return poses, note
