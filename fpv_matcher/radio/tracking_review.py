"""Observed tracking geometry and correction diagnostics for video review."""
from __future__ import annotations

import cv2
import numpy as np

SIDES = ("left", "right")
GAP_REASONS = {0: "Visible tip", 1: "No distinctive tip template", 2: "Tip covered or appearance ambiguous",
               3: "Tip outside calibrated travel", 4: "Radio/gimbal pose unobserved", 5: "Tip appearance too weak"}
GEOMETRY_KEYS = tuple(
    [f"{side}_gimbal_{corner}_{axis}" for side in SIDES for corner in range(4) for axis in ("x", "y")]
    + [f"{side}_{key}" for side in SIDES for key in
       ("tip_x", "tip_y", "housing_quality", "tip_quality", "tip_gap_reason", "tip_match_score", "tip_match_margin")]
)


def geometry_values(trackers, scale):
    result = {}
    for side, tracker in zip(SIDES, trackers):
        corners = np.float32([[[tracker.x, tracker.y], [tracker.x + tracker.width, tracker.y],
                              [tracker.x + tracker.width, tracker.y + tracker.height], [tracker.x, tracker.y + tracker.height]]])
        pose_quality = float(tracker.housing_quality)
        moved = cv2.perspectiveTransform(corners, tracker.transform)[0] / scale
        for index, point in enumerate(np.nan_to_num(moved, nan=0., posinf=0., neginf=0.)):
            result[f"{side}_gimbal_{index}_x"], result[f"{side}_gimbal_{index}_y"] = point
        tip = np.zeros(2)
        if tracker.last_tip is not None and tracker.last_quality > .1:
            position = np.float32([[[tracker.x + tracker.last_tip[0], tracker.y + tracker.last_tip[1]]]])
            tip = cv2.perspectiveTransform(position, tracker.transform)[0, 0] / scale
        result[f"{side}_tip_x"], result[f"{side}_tip_y"] = np.nan_to_num(tip, nan=0., posinf=0., neginf=0.)
        result[f"{side}_housing_quality"] = pose_quality
        result[f"{side}_tip_quality"] = tracker.last_quality
        reason = tracker.reason
        result[f"{side}_tip_gap_reason"] = (0 if tracker.last_quality > .1 else 1 if "distinctive" in reason
            else 2 if "ambiguous" in reason else 3 if "travel" in reason else 5 if "weak" in reason else 4)
        result[f"{side}_tip_match_score"] = tracker.match_score
        result[f"{side}_tip_match_margin"] = tracker.match_margin
    return result


def geometry_trace(data, calibration, width, height, indices):
    """Prefer stored poses; reconstruct older outline-backed observations."""
    from fpv_matcher.radio.modes import MODES
    count = len(indices)
    result = {"source": "stored", "gimbals": {}, "tips": {}, "gimbal_quality": {}, "tip_quality": {},
              "tip_gap_reason": {}, "gap_reasons": GAP_REASONS}
    direct = all(key in data for key in GEOMETRY_KEYS)
    has_outline = all(f"outline_{corner}_{axis}" in data for corner in range(4) for axis in ("x", "y"))
    if not direct:
        result["source"] = "outline_reconstruction" if has_outline and calibration.get("radio_outline") else "fixed_reconstruction" if calibration.get("stickcam_mode") == "fixed" else "unavailable"
    for side in SIDES:
        if direct:
            corners = np.asarray([np.column_stack((data[f"{side}_gimbal_{i}_x"][indices], data[f"{side}_gimbal_{i}_y"][indices])) for i in range(4)]).transpose(1, 0, 2)
            tips = np.column_stack((data[f"{side}_tip_x"][indices], data[f"{side}_tip_y"][indices]))
            pose_quality = np.asarray(data[f"{side}_housing_quality"])[indices]
            tip_quality = np.asarray(data[f"{side}_tip_quality"])[indices]
            reasons = np.asarray(data[f"{side}_tip_gap_reason"])[indices]
        else:
            spec = calibration[side + "_gimbal"]
            base = np.float32([[spec["x"], spec["y"]], [spec["x"] + spec["width"], spec["y"]],
                               [spec["x"] + spec["width"], spec["y"] + spec["height"]], [spec["x"], spec["y"] + spec["height"]]])
            mapping = MODES[calibration.get("stick_mode", 2)]
            xaxis, yaxis = mapping[side + "_x"], mapping[side + "_y"]
            factor = -1 if calibration.get("radio_rotation_deg", 0) == 180 else 1
            x = np.asarray(data[xaxis])[indices] * calibration["signs"][xaxis] * factor
            y = -np.asarray(data[yaxis])[indices] * calibration["signs"][yaxis] * factor
            source_tips = np.column_stack((spec["x"] + spec["center_x"] + x * spec["travel_x"],
                                           spec["y"] + spec["center_y"] + y * spec["travel_y"]))
            corners, tips = np.zeros((count, 4, 2)), np.zeros((count, 2))
            tip_quality = np.minimum(np.asarray(data.get(xaxis + "_quality", data["quality"]))[indices],
                                     np.asarray(data.get(yaxis + "_quality", data["quality"]))[indices])
            pose_quality = np.zeros(count)
            for row, index in enumerate(indices):
                transform = None
                if result["source"] == "outline_reconstruction" and data["radio_orientation_quality"][index] >= .35:
                    observed = np.float32([[data[f"outline_{i}_x"][index], data[f"outline_{i}_y"][index]] for i in range(4)])
                    transform = cv2.getPerspectiveTransform(np.float32(calibration["radio_outline"]), observed)
                    pose_quality[row] = data["radio_orientation_quality"][index]
                elif result["source"] == "fixed_reconstruction":
                    transform = np.eye(3)
                    pose_quality[row] = 1.
                if transform is not None:
                    corners[row] = cv2.perspectiveTransform(base[None], transform)[0]
                    tips[row] = cv2.perspectiveTransform(source_tips[row].astype(np.float32).reshape(1, 1, 2), transform)[0, 0]
            tip_quality = np.where(pose_quality >= .35, tip_quality, 0.)
            reasons = np.where(tip_quality > .1, 0, np.where(pose_quality >= .35, 2, 4))
        pose_quality = np.where(np.isfinite(corners).all(axis=(1, 2)), pose_quality, 0.)
        tip_quality = np.where(np.isfinite(tips).all(axis=1), tip_quality, 0.)
        result["gimbals"][side] = np.nan_to_num(corners, nan=0., posinf=0., neginf=0.).round(2).tolist()
        result["tips"][side] = np.nan_to_num(tips, nan=0., posinf=0., neginf=0.).round(2).tolist()
        result["gimbal_quality"][side] = np.clip(pose_quality, 0, 1).round(4).tolist()
        result["tip_quality"][side] = np.clip(tip_quality, 0, 1).round(4).tolist()
        result["tip_gap_reason"][side] = np.asarray(reasons, int).tolist()
    return result


def correction_diagnostics(data, calibration, landmarks, width, height):
    """Compare marked tips with earlier observations without inventing a pose."""
    times = np.asarray(data["time"])
    indices = [int(np.argmin(np.abs(times - point["time"]))) for point in landmarks]
    geometry = geometry_trace(data, calibration, width, height, np.asarray(indices, int))
    diagnostics = []
    for row, point in enumerate(landmarks):
        result = {"time": point["time"], "sample_time": float(times[indices[row]]), "tips": {}}
        close = abs(times[indices[row]] - point["time"]) <= .1
        for side in SIDES:
            marked = point.get(side + "_tip")
            if marked is None and point.get("calibration"):
                spec = point["calibration"][side + "_gimbal"]
                marked = [(spec["x"] + spec["tip_x"]) / width, (spec["y"] + spec["tip_y"]) / height]
            if marked is None:
                continue
            quality = geometry["tip_quality"][side][row]
            observed = geometry["tips"][side][row] if quality > .1 and close else None
            error = float(np.linalg.norm(np.asarray(observed) - np.asarray(marked) * [width, height])) if observed is not None else None
            reason = GAP_REASONS.get(geometry["tip_gap_reason"][side][row], "Tracking unavailable")
            if not close:
                reason = "No nearby saved tracking sample"
            elif observed is not None and error > 10:
                reason = "Tracked candidate disagrees with the marked tip"
            result["tips"][side] = {"position": observed, "quality": quality, "error_pixels": round(error, 2) if error is not None else None,
                                     "reason": reason, "sample_close": bool(close)}
        diagnostics.append(result)
    return diagnostics
