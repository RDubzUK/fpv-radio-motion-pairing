"""Rank session FPV telemetry against an already extracted StickCam signal."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np

from fpv_matcher.matching.correlation import compare, prepare
from fpv_matcher.motion.telemetry import body_rates, cached_telemetry
from fpv_matcher.pipeline import atomic_json


def check_source(path, signature):
    stat = Path(path).stat()
    if [stat.st_size, stat.st_mtime_ns] != signature:
        raise ValueError("Source changed; load its signals again after copying finishes")


def run(request_path):
    request = json.loads(Path(request_path).read_text())
    directory = Path(request["directory"])
    check_source(request["radio_path"], request["radio_signature"])
    with np.load(request["radio_signals"], allow_pickle=False) as values:
        radio = dict(values)
    fps, minimum = request["match_fps"], request["min_overlap"]
    prepared = prepare(radio, fps, True)
    candidates, skipped = [], []
    total = len(request["clips"])
    for index, clip in enumerate(request["clips"]):
        atomic_json(directory / "progress.json", {"stage": "gyro-scan", "percent": 100 * index / max(1, total),
                    "completed": index, "total": total, "name": clip["name"]})
        try:
            check_source(clip["path"], clip["signature"])
            telemetry = cached_telemetry(clip["path"], Path(request["cache"]), clip.get("identity"))
            motion = body_rates(telemetry, request["camera_uptilt_deg"], request["imu_orientation"], request["gyro_video_offset"])
            result = compare(prepared, prepare(motion, fps, False), fps, minimum)
            check_source(clip["path"], clip["signature"])
            candidates.append({"fpv_video": clip["name"], "source": str(telemetry["kind"].item()),
                               "label": str(telemetry["label"].item()), **asdict(result)})
        except Exception as exc:
            skipped.append({"fpv_video": clip["name"], "reason": str(exc)})
    check_source(request["radio_path"], request["radio_signature"])
    candidates.sort(key=lambda row: row["score"], reverse=True)
    for index, row in enumerate(candidates):
        others = [item["score"] for i, item in enumerate(candidates) if i != index]
        row["candidate_margin"] = max(0, row["score"] - max(others, default=0))
        row["clear"] = bool(index == 0 and row["score"] >= max(.55, request["minimum_score"])
            and row["reliable_axes"] >= 2 and row["peak_margin"] >= .025
            and row["candidate_margin"] >= .04 and row["coverage"] >= .5 and row["valid_seconds"] >= 10)
        row["clear"] = row["clear"] and row["correlated_sections"] >= 5 and row["section_agreement"] >= .6 and row["section_spread"] >= 2 / 3
    clear = bool(candidates and candidates[0]["clear"])
    atomic_json(directory / "scan.json", {"radio_video": request["radio_video"], "candidates": candidates,
        "skipped": skipped, "clear_candidate": clear, "total": total,
        "settings": {k: request[k] for k in ("rate_profile", "camera_uptilt_deg", "imu_orientation", "gyro_video_offset", "match_fps", "min_overlap")},
        "note": "Scores describe correlation evidence, not match probabilities. Confirm timing in the plots and videos. Clips without supported embedded motion are skipped; audio and filename clocks do not affect this scan."})
    atomic_json(directory / "progress.json", {"stage": "gyro-scan", "percent": 100, "completed": total, "total": total})


if __name__ == "__main__":
    run(sys.argv[1])
