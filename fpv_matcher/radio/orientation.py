"""Bounded midpoint checks against a human-confirmed session radio appearance."""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

from fpv_matcher.pipeline import atomic_json
from fpv_matcher.radio.reference import suggest_from_reference
from fpv_matcher.video.frames import jpeg_frame

VERSION = "session-radio-midpoint-orientation-v1"


def rotation_from_regions(regions):
    """Physical left/right landmarks distinguish inversion from symmetric rims."""
    centers = [np.array([regions[s]["x"] + regions[s]["center_x"],
                         regions[s]["y"] + regions[s]["center_y"]])
               for s in ("left_gimbal", "right_gimbal")]
    vector = centers[1] - centers[0]
    angle = math.degrees(math.atan2(float(vector[1]), float(vector[0])))
    rotation = 0 if abs(angle) <= 45 else 180 if abs(angle) >= 135 else None
    return {"rotation_deg": rotation, "angle_deg": round(angle, 2)}


def scaled_region(region, sx, sy):
    return {key: value * (sx if key.endswith("x") or key in {"x", "width"} else sy)
            if key in {"x", "y", "width", "height", "center_x", "center_y", "travel_x", "travel_y", "tip_x", "tip_y"}
            else value * min(sx, sy) if key == "tip_radius" else value for key, value in region.items()}


def suggest_upright_regions(image, outline=None, rotation=0, radio_model="other"):
    from fpv_matcher.radio.auto_calibration import suggest_gimbal_regions
    if rotation != 180:
        return suggest_gimbal_regions(image, outline, radio_model)
    height, width = image.shape[:2]
    upright_outline = [[width - 1 - x, height - 1 - y] for x, y in outline] if outline else None
    if upright_outline:
        upright_outline = upright_outline[2:] + upright_outline[:2]
    proposal = suggest_gimbal_regions(cv2.rotate(image, cv2.ROTATE_180), upright_outline, radio_model)
    if proposal.get("radio_outline"):
        points = [[width - 1 - x, height - 1 - y] for x, y in proposal["radio_outline"]]
        proposal["radio_outline"] = points[2:] + points[:2]
    if proposal.get("shape_shell"):
        proposal["shape_shell"] = [[width - 1 - x, height - 1 - y] for x, y in proposal["shape_shell"]]
    for region in proposal["regions"].values():
        region["x"], region["y"] = width - region["x"] - region["width"], height - region["y"] - region["height"]
        for key in ("center_x", "tip_x"):
            if key in region:
                region[key] = region["width"] - 1 - region[key]
        for key in ("center_y", "tip_y"):
            if key in region:
                region[key] = region["height"] - 1 - region[key]
    return proposal


def _signature(path):
    stat = Path(path).stat()
    return [stat.st_size, stat.st_mtime_ns]


def _image(path, at):
    raw = jpeg_frame(path, at)
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError("Cannot decode orientation frame")
    return image, raw


def run(request_path):
    request = json.loads(Path(request_path).read_text())
    directory = Path(request["directory"])
    cv2.setNumThreads(1)
    anchor = request["reference"]
    if _signature(anchor["path"]) != anchor["signature"]:
        raise ValueError("Session radio reference source changed; confirm a new reference")
    config = anchor["calibration"]
    reference, _ = _image(anchor["path"], config["reference_time"])
    sx, sy = reference.shape[1] / anchor["width"], reference.shape[0] / anchor["height"]
    outline = [[x * sx, y * sy] for x, y in config["radio_outline"]]
    regions = {s: scaled_region(config[s], sx, sy) for s in ("left_gimbal", "right_gimbal")}
    rows = []
    for index, clip in enumerate(request["clips"]):
        attempts, best, calibration_result = [], None, None
        row = {"name": clip["name"], "signature": clip["signature"],
               "frame_index": index,
               "calibration_time": clip["calibration_time"], "profile_revision": request["profile_revision"],
               "radio_label": anchor["label"], "reference_clip": anchor["name"]}
        try:
            midpoint = clip["duration"] / 2
            times = list(dict.fromkeys(round(max(0, min(clip["duration"] - 0.05, midpoint + d)), 3)
                                       for d in (0, -5, 5, -10, 10)))
            for attempt, at in enumerate(times):
                atomic_json(directory / "progress.json", {"stage": "radio-orientation", "name": clip["name"],
                            "at": at, "completed": index, "total": len(request["clips"]),
                            "percent": (index + attempt / len(times)) / len(request["clips"]) * 100})
                try:
                    image, raw = _image(clip["path"], at)
                    candidate = suggest_from_reference(reference, image, outline, regions, detect_tips=False)
                except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, cv2.error) as exc:
                    attempts.append({"at": at, "status": "unreadable_frame", "message": str(exc)})
                    continue
                evidence = {"at": at, "status": "no_clear_reference_match"}
                if candidate:
                    evidence.update(rotation_from_regions(candidate["regions"]),
                                    inliers=candidate["reference_inliers"], coverage=candidate["reference_coverage"],
                                    error_px=candidate["reference_error_px"], status="matched")
                    if best is None:
                        best = evidence
                    if evidence["rotation_deg"] is not None:
                        best = evidence
                attempts.append(evidence)
                if attempt == 0 or best is evidence:
                    filename = f"frame-{index}.jpg"
                    if candidate:
                        annotated = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
                        cv2.polylines(annotated, [np.asarray(candidate["radio_outline"], np.int32)], True, (40, 220, 40), 2)
                        for side, region in candidate["regions"].items():
                            center = (round(region["x"] + region["center_x"]), round(region["y"] + region["center_y"]))
                            cv2.circle(annotated, center, 8, (220, 80, 200) if side == "left_gimbal" else (240, 210, 40), 2)
                            cv2.putText(annotated, "Physical " + side.split("_")[0], center, cv2.FONT_HERSHEY_SIMPLEX, .5, (40, 220, 40), 1)
                        encoded, jpeg = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
                        if encoded:
                            raw = jpeg.tobytes()
                    (directory / filename).write_bytes(raw)
                    row.update(image_file=filename, image_at=at)
                if candidate and evidence["rotation_deg"] is not None:
                    break
            # A midpoint result must not silently change axes at a different calibration frame.
            image, _ = _image(clip["path"], clip["calibration_time"])
            candidate = suggest_from_reference(reference, image, outline, regions, detect_tips=False)
            if candidate:
                calibration_result = rotation_from_regions(candidate["regions"])
            if _signature(clip["path"]) != clip["signature"] or _signature(anchor["path"]) != anchor["signature"]:
                raise ValueError("Source changed during the orientation check")
            midpoint_rotation = best.get("rotation_deg") if best else None
            calibration_rotation = calibration_result.get("rotation_deg") if calibration_result else None
            row.update(status="suggested" if midpoint_rotation is not None else "unknown", attempts=attempts,
                       midpoint_rotation_deg=midpoint_rotation, calibration_rotation_deg=calibration_rotation,
                       calibration_angle_deg=calibration_result.get("angle_deg") if calibration_result else None,
                       evidence=best, orientation_changes=midpoint_rotation is not None and calibration_rotation is not None
                       and midpoint_rotation != calibration_rotation,
                       message="Review the matched radio and confirm the calibration-frame orientation." if midpoint_rotation is not None
                       else "No clear upright/inverted reference match near the midpoint. Set orientation manually; use a clearer session reference if needed.")
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, cv2.error) as exc:
            row.update(status="unknown", attempts=attempts, message=str(exc), midpoint_rotation_deg=None,
                       calibration_rotation_deg=None)
        rows.append(row)
        atomic_json(directory / "orientation.json", {"clips": rows, "profile_revision": request["profile_revision"]})
    atomic_json(directory / "progress.json", {"stage": "complete", "percent": 100})


if __name__ == "__main__":
    run(sys.argv[1])
