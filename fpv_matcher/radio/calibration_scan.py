"""Find editable calibration proposals; operator confirmation remains separate."""
from __future__ import annotations
import json
import sys
import subprocess
from pathlib import Path

import cv2
import numpy as np

from fpv_matcher.pipeline import atomic_json
from fpv_matcher.radio.reference import suggest_from_reference
from fpv_matcher.radio.orientation import rotation_from_regions, suggest_radio_orientation
from fpv_matcher.radio.tips import suggest_visible_tip
from fpv_matcher.video.frames import jpeg_frame
from fpv_matcher.video.metadata import probe_video

VERSION = "calibration-soft-body-orientation-v5"


def _frame(path, at):
    image = cv2.imdecode(np.frombuffer(jpeg_frame(path, at), np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError("Could not read calibration image")
    return image


def _scale_region(region, sx, sy):
    return {key: value * (sx if key.endswith("x") or key in {"x", "width"} else sy)
            if key in {"x", "y", "width", "height", "center_x", "center_y", "travel_x", "travel_y", "tip_x", "tip_y"}
            else value * min(sx, sy) if key == "tip_radius" else value for key, value in region.items()}


def _proposal(image, references, radio_model="auto"):
    proposals = []
    for reference in references:
        candidate = suggest_from_reference(reference["image"], image, reference["outline"], reference["regions"])
        if candidate is not None:
            if reference.get("orientation_confirmed"):
                candidate["radio_rotation_deg"] = rotation_from_regions(candidate["regions"])["rotation_deg"]
                candidate["orientation_ambiguous"] = candidate["radio_rotation_deg"] is None
            proposals.append({**candidate, "reference_clip": reference["name"]})
    if proposals:
        proposal = max(proposals, key=lambda p: (sum("tip_x" in r for r in p["regions"].values()), p["reference_inliers"]))
    else:
        proposal = suggest_radio_orientation(image, radio_model=radio_model)
    for region in proposal["regions"].values():
        if "tip_x" not in region:
            tip = suggest_visible_tip(image, region)
            if tip is not None:
                region.update(tip)
    proposal["missing_tips"] = [side for side, region in proposal["regions"].items() if "tip_x" not in region]
    return {"frame_width": int(image.shape[1]), "frame_height": int(image.shape[0]), **proposal}


def run(request_path):
    request = json.loads(Path(request_path).read_text())
    directory = Path(request["directory"])
    cv2.setNumThreads(1)
    references, warnings = [], []
    for reference in request["references"]:
        try:
            stat = Path(reference["path"]).stat()
            if [stat.st_size, stat.st_mtime_ns] != reference["signature"]:
                raise ValueError("Reference source changed")
            info = probe_video(reference["path"])
            config = reference["calibration"]
            image = _frame(reference["path"], config["reference_time"])
            sx, sy = image.shape[1] / info["width"], image.shape[0] / info["height"]
            references.append({"name": reference["name"], "image": image,
                "orientation_confirmed": config.get("radio_orientation_confirmed", False),
                "outline": [[x * sx, y * sy] for x, y in config["radio_outline"]],
                "regions": {side: _scale_region(config[side], sx, sy) for side in ("left_gimbal", "right_gimbal")}})
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError, cv2.error) as exc:
            warnings.append(f"{reference['name']}: {exc}")
    rows, total = [], len(request["clips"])
    def save():
        atomic_json(directory / "calibration-review.json", {"clips": rows, "warnings": warnings,
            "step_seconds": 5, "search_seconds": request["search_seconds"]})
    save()
    for index, clip in enumerate(request["clips"]):
        attempts, best = [], None
        try:
            path = Path(clip["path"])
            info = probe_video(path)
            start = clip.get("start", 0)
            if not 0 <= start < info["duration"]:
                raise ValueError("Search start is outside the recording")
            times = np.arange(start, min(info["duration"], start + request["search_seconds"] + .001), 5)
            for attempt, at in enumerate(times):
                atomic_json(directory / "progress.json", {"stage": "calibration", "name": clip["name"],
                    "at": float(at), "completed": index, "total": total, "clips": rows,
                    "percent": (index + attempt / max(1, len(times))) / total * 100})
                try:
                    proposal = _proposal(_frame(path, float(at)), [r for r in references if r["name"] != clip["name"]], request.get("radio_model", "auto"))
                    outline = proposal.get("radio_outline")
                    rank = (bool(outline), not proposal.get("orientation_ambiguous", False),
                            2 - len(proposal["missing_tips"]), proposal.get("reference_inliers", 0))
                    sx, sy = info["width"] / proposal["frame_width"], info["height"] / proposal["frame_height"]
                    config = {"stickcam_mode": clip.get("stickcam_mode", "wonky"), "stick_mode": clip.get("stick_mode", 2), "reference_time": float(at),
                        "radio_rotation_deg": proposal.get("radio_rotation_deg") or 0, "radio_orientation_confirmed": False,
                        "signs": {axis: 1 for axis in ("roll", "pitch", "yaw", "throttle")},
                        **{side: _scale_region(region, sx, sy) for side, region in proposal["regions"].items()}}
                    if outline:
                        config["radio_outline"] = [[x * sx, y * sy] for x, y in outline]
                    ready = outline and not proposal["missing_tips"] and not proposal.get("orientation_ambiguous")
                    candidate = {"name": clip["name"], "status": "suggested" if ready else "needs_adjustment",
                        "at": float(at), "calibration": config, "proposal": proposal, "signature": clip["signature"],
                        "message": "Review outline, both gimbals and both tips before confirming." if ready
                        else "Confirm orientation and complete outline/tip markers, or try a later frame."}
                    if best is None or rank > best[0]:
                        best = (rank, candidate)
                    attempts.append({"at": float(at), "status": candidate["status"], "missing_tips": proposal["missing_tips"], "outline_available": bool(outline)})
                    if candidate["status"] == "suggested":
                        break
                except (ValueError, OSError, RuntimeError, subprocess.SubprocessError, cv2.error) as exc:
                    attempts.append({"at": float(at), "status": "no_clear_radio", "message": str(exc)})
            stat = path.stat()
            if [stat.st_size, stat.st_mtime_ns] != clip["signature"]:
                raise ValueError("Source changed during calibration search")
            row = best[1] if best else {"name": clip["name"], "status": "manual", "signature": clip["signature"], "message": "No clear radio view found in the search window. Extend the search or choose a frame manually."}
            rows.append({**row, "attempts": attempts})
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError, cv2.error) as exc:
            rows.append({"name": clip["name"], "status": "failed", "message": str(exc), "attempts": attempts})
        save()
    atomic_json(directory / "progress.json", {"stage": "complete", "percent": 100, "completed": total, "total": total})


if __name__ == "__main__":
    run(sys.argv[1])
