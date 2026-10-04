"""Bounded chart payloads and an isolated worker for on-demand motion inspection."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

from fpv_matcher.motion.derivation import cached_visual_motion
from fpv_matcher.motion.telemetry import cached_telemetry
from fpv_matcher.pipeline import atomic_json
from fpv_matcher.radio.rates import apply_rate_profile
from fpv_matcher.radio.stabilization import review_poses
from fpv_matcher.radio.derivation import cached_sticks


def chart_trace(data, key, label, unit, start, end, shift=0, budget=1800):
    times = np.asarray(data["time"], float) + shift
    values = np.asarray(data[key], float)
    quality = np.asarray(data.get(key + "_quality", data["quality"]), float)
    keep = (times >= start) & (times <= end)
    estimated = np.asarray(data.get("_interpolated", np.zeros(len(times), bool)))[keep]
    times, values, quality = times[keep], values[keep], quality[keep]
    valid = np.isfinite(values) & np.isfinite(times) & (quality > 0.1)
    # Retain min/max and an invalid sample per bin: spikes and tracking gaps survive zoom-out.
    indices = []
    for block in np.array_split(np.arange(len(times)), max(1, min(len(times), budget // 3))):
        good = block[valid[block]]
        selected = []
        if len(good):
            selected += [good[np.argmin(values[good])], good[np.argmax(values[good])]]
        bad = block[~valid[block]]
        if len(bad):
            selected.append(bad[0])
        indices.extend(sorted(set(selected)))
    # Preserve estimate boundaries so dashed bridges cannot become solid data
    # during chart downsampling. This remains bounded by the payload budget.
    changes = np.flatnonzero(np.diff(estimated.astype(int)))
    if estimated.any():
        indices = sorted(set(indices) | {int(i) for v in changes for i in (v, v+1)})
        if len(indices) > budget:
            # Prefer truthful gaps over a misleading solid line at wide zoom.
            unfilled = {k: v for k, v in data.items() if k != "_interpolated"}
            unfilled[key + "_quality"] = np.where(np.asarray(data["_interpolated"]), 0., data.get(key + "_quality", data["quality"]))
            return chart_trace(unfilled, key, label, unit, start, end, shift, budget)
    finite = np.asarray(data[key], float)
    good_all = np.isfinite(finite) & (np.asarray(data.get(key + "_quality", data["quality"])) > 0.1)
    scale = float(np.percentile(np.abs(finite[good_all]), 95)) if good_all.any() else 1.0
    return {"key": key, "label": label, "unit": unit, "scale": max(scale, 1e-8),
            "points": [[round(float(times[i]), 6), round(float(values[i]), 6) if valid[i] else None,
                        bool(estimated[i])] for i in indices]}


def run(request_path):
    request = json.loads(Path(request_path).read_text())
    directory, cache = Path(request["directory"]), Path(request["cache"])
    import cv2
    os.environ["FPV_MATCHER_TRACKING"] = request.get("tracking_backend", "auto")
    cv2.setNumThreads(1)
    os.environ["FPV_MATCHER_DECODE"] = request.get("decode_mode", "auto")
    source = request["source"]
    def report(stage, event=None):
        event = event or {}
        atomic_json(directory / "progress.json", {"stage": stage, **event})
    report("fpv", {"percent": 0})
    warnings = []
    fpv = None
    if source != "visual":
        try:
            fpv = cached_telemetry(request["fpv_path"], cache, request.get("fpv_identity"))
        except (ValueError, RuntimeError, ImportError) as exc:
            if source == "gyro":
                raise ValueError(f"Embedded motion unavailable: {exc}") from exc
            warnings.append(f"Embedded motion unavailable: {exc}. Showing image-derived motion.")
    if fpv is None:
        fpv = cached_visual_motion(request["fpv_path"], cache, request.get("fpv_sample_fps", request["sample_fps"]), request.get("fpv_identity"),
                                   progress=lambda event: report("fpv", event), camera_profile=request.get("visual_camera"))

    np.savez_compressed(directory / "fpv.npz", **fpv)
    report("fpv", {"percent": 100})
    radio = None
    calibration = request.get("calibration")
    if calibration and not calibration.get("skip_motion") and not calibration.get("exclude"):
        report("stickcam", {"percent": 0})
        outline = "radio_outline" in calibration
        radio = cached_sticks(request["radio_path"], cache, calibration, request.get("stick_sample_fps", request["sample_fps"]),
                              request.get("radio_identity"), lambda event: report("stickcam", event))
        report("stickcam", {"percent": 100})
        radio_gyro = None
        if outline:
            try:
                radio_gyro = cached_telemetry(request["radio_path"], cache, request.get("radio_identity"))
            except (ValueError, RuntimeError, ImportError):
                pass
        poses, stabilization_note = review_poses(radio, radio_gyro)
        atomic_json(directory / "stabilization.json", {"poses": poses, "note": stabilization_note,
                    "gyro_available": radio_gyro is not None})
        radio = apply_rate_profile(radio, request.get("rate_profile"))
        np.savez_compressed(directory / "radio.npz", **radio)
    elif request.get("radio_video"):
        warnings.append("StickCam motion unavailable. Save a stick-tip calibration and load the signals again.")
    for kind in ("fpv", "radio"):
        if request.get(kind + "_path"):
            stat = Path(request[kind + "_path"]).stat()
            if [stat.st_size, stat.st_mtime_ns] != request["signatures"][kind]:
                raise ValueError("Source changed during extraction. Load the signals again after copying finishes.")
    report("complete", {"percent": 100, "eta_seconds": 0})
    atomic_json(directory / "review.json", {"fpv_kind": str(fpv["kind"].item()), "fpv_label": str(fpv["label"].item()),
        "fpv_note": str(fpv["note"].item()), "radio_available": radio is not None,
        "rate_profile": request.get("rate_profile"), "stick_mode": (calibration or {}).get("stick_mode", 2), "warnings": warnings,
        "stick_sample_fps": request.get("stick_sample_fps", request["sample_fps"]),
        "fpv_video": request["fpv_video"], "radio_video": request.get("radio_video"),
        "fpv_range": [float(fpv["time"][0]), float(fpv["time"][-1])],
        "radio_range": [float(radio["time"][0]), float(radio["time"][-1])] if radio is not None and len(radio["time"]) else None})


if __name__ == "__main__":
    run(sys.argv[1])
