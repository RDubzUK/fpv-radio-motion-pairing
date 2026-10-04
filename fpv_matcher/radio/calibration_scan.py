"""Find editable calibration proposals; operator confirmation remains separate."""
from __future__ import annotations
import json
import sys
import subprocess
from pathlib import Path

import cv2
import numpy as np

from fpv_matcher.pipeline import atomic_json
from fpv_matcher.radio.auto_calibration import suggest_gimbal_regions
from fpv_matcher.radio.reference import suggest_from_reference
from fpv_matcher.radio.tips import suggest_visible_tip
from fpv_matcher.video.frames import jpeg_frame
from fpv_matcher.video.metadata import probe_video

VERSION = "calibration-five-second-reference-search-v1"


def _frame(path, at):
    image = cv2.imdecode(np.frombuffer(jpeg_frame(path, at), np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError("Could not read calibration image")
    return image


def _scale_region(region, sx, sy):
    return {key: value * (sx if key.endswith("x") or key in {"x", "width"} else sy)
            if key in {"x", "y", "width", "height", "center_x", "center_y", "travel_x", "travel_y", "tip_x", "tip_y"}
            else value * min(sx, sy) if key == "tip_radius" else value for key, value in region.items()}


def _proposal(image, references):
    proposals = []
    for reference in references:
        candidate = suggest_from_reference(reference["image"], image, reference["outline"], reference["regions"])
        if candidate is not None:
            proposals.append({**candidate, "reference_clip": reference["name"]})
    proposal = max(proposals, key=lambda p: (sum("tip_x" in r for r in p["regions"].values()), p["reference_inliers"])) if proposals else suggest_gimbal_regions(image)
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
                    proposal = _proposal(_frame(path, float(at)), [r for r in references if r["name"] != clip["name"]])
                    outline = proposal.get("radio_outline")
                    rank = (bool(outline), 2 - len(proposal["missing_tips"]), proposal.get("reference_inliers", 0))
                    sx, sy = info["width"] / proposal["frame_width"], info["height"] / proposal["frame_height"]
                    config = {"stickcam_mode": clip.get("stickcam_mode", "wonky"), "reference_time": float(at),
                        "signs": {axis: 1 for axis in ("roll", "pitch", "yaw", "throttle")},
                        **{side: _scale_region(region, sx, sy) for side, region in proposal["regions"].items()}}
                    if outline:
                        config["radio_outline"] = [[x * sx, y * sy] for x, y in outline]
                    candidate = {"name": clip["name"], "status": "suggested" if outline and not proposal["missing_tips"] else "needs_adjustment",
                        "at": float(at), "calibration": config, "proposal": proposal, "signature": clip["signature"],
                        "message": "Review outline, both gimbals and both tips before confirming." if outline and not proposal["missing_tips"] else "Complete missing outline/tip markers, or try a later frame."}
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
