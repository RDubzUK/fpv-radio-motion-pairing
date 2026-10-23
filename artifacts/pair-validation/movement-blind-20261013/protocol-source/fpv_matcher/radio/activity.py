"""Cached, tip-free gimbal activity. This is not a stick-position measurement."""
from __future__ import annotations

import time
from functools import lru_cache
from pathlib import Path
from zipfile import BadZipFile

import cv2
import numpy as np

from fpv_matcher.cache.fingerprints import _key, get_or_extract
from fpv_matcher.config import validate_calibration
from fpv_matcher.progress import ExtractionProgress
from fpv_matcher.radio.outline import RadioOutlineTracker
from fpv_matcher.video.acceleration import TrackingBackend
from fpv_matcher.video.metadata import probe_video
from fpv_matcher.video.reader import sampled_frames

VERSION = "observed-gimbal-activity-bank-v1"
SAMPLE_FPS, MAX_DIMENSION, CROP_SIZE = 10., 960, 96


def specification(calibration):
    if not calibration or calibration.get("source_changed"):
        raise ValueError("Save the radio outline and both gimbal regions for movement matching")
    if calibration.get("exclude") or calibration.get("skip_motion"):
        raise ValueError("Movement matching is disabled for this StickCam recording")
    config = validate_calibration(calibration)
    candidates = [config] + [dict(point["calibration"], reference_time=point["time"])
                             for point in config.get("tracking_landmarks", []) if point.get("calibration")]
    references = []
    for candidate in candidates:
        if "radio_outline" not in candidate:
            continue
        clean = {"reference_time": candidate["reference_time"], "radio_outline": candidate["radio_outline"]}
        for side in ("left_gimbal", "right_gimbal"):
            # Tips, rates, assignments and expected-control annotations cannot
            # alter this fingerprint or invalidate its cache.
            clean[side] = {key: candidate[side][key] for key in
                          ("x", "y", "width", "height", "center_x", "center_y")}
        if clean not in references:
            references.append(clean)
    if not references:
        raise ValueError("Mark and save the radio outline for tip-free movement matching")
    # Bound appearance/decode work even with thousands of corrections.
    if len(references) > 6:
        indices = np.linspace(0, len(references)-1, 6).round().astype(int)
        references = [references[i] for i in indices]
    return {"references": references, "sample_fps": SAMPLE_FPS, "max_dimension": MAX_DIMENSION,
            "crop_size": CROP_SIZE}


def validate(data):
    times = np.asarray(data["time"], float)
    if times.ndim != 1 or len(times) < 3 or not np.isfinite(times).all() or times[0] < 0 or np.any(np.diff(times) <= 0):
        raise ValueError("Invalid saved gimbal activity timeline")
    for key in ("activity", "left_activity", "right_activity", "quality", "pose_quality", "reference_index"):
        values = np.asarray(data[key], float)
        if values.shape != times.shape or not np.isfinite(values).all():
            raise ValueError("Invalid saved gimbal activity samples")
        if "quality" in key and ((values < 0) | (values > 1)).any():
            raise ValueError("Invalid gimbal activity observation quality")


def cache_path(path, cache, calibration, identity=None):
    return Path(cache) / f"gimbal-activity.{_key(str(path), VERSION, specification(calibration), identity)}.npz"


@lru_cache(maxsize=256)
def _inspect(filename, modified, size):
    with np.load(filename, allow_pickle=False) as saved:
        if str(saved["cache_key"].item()) != Path(filename).stem.split(".", 1)[1]:
            raise ValueError("Saved gimbal activity identity differs")
        validate(saved)
        return {"status": "cached", "sample_fps": float(saved["sample_fps"]), "samples": len(saved["time"]),
                "usable_percent": round(float(np.mean(saved["quality"] > .1))*100, 1),
                "extraction_seconds": float(saved["extraction_seconds"])}


def cache_status(path, cache, calibration, identity=None):
    try:
        target = cache_path(path, cache, calibration, identity)
        if not target.exists():
            return {"status": "not_derived", "sample_fps": SAMPLE_FPS}
        stat = target.stat()
        return dict(_inspect(str(target), stat.st_mtime_ns, stat.st_size))
    except (ValueError, KeyError, TypeError) as exc:
        return {"status": "needs_calibration", "message": str(exc)}
    except (OSError, EOFError, BadZipFile):
        return {"status": "unavailable", "message": "Source or saved gimbal activity is unavailable"}


def cached_activity(path, cache, calibration, identity=None, progress=None):
    config = specification(calibration)
    return get_or_extract(str(path), cache, "gimbal-activity", VERSION, config,
                          lambda: extract_activity(path, config, progress), identity, validate=validate)


def extract_activity(path, config, progress=None):
    info = probe_video(path)
    fps = min(SAMPLE_FPS, float(info["fps"]))
    if not np.isfinite(fps) or fps <= 0:
        raise ValueError("Cannot determine this StickCam recording's frame rate")
    scale = min(1., MAX_DIMENSION/max(info["width"], info["height"]))
    backend = TrackingBackend()
    reporter = ExtractionProgress(info["duration"], callback=(lambda event: progress(
        {**event, **backend.status(), "sample_fps": fps})) if progress else None)
    started = time.monotonic()
    bank = []
    for reference in config["references"]:
        if reference["reference_time"] >= info["duration"]:
            raise ValueError("Gimbal activity reference is outside the source recording")
        outline = np.float32(reference["radio_outline"])*scale
        regions = [{k: value*scale for k, value in reference[side].items()}
                   for side in ("left_gimbal", "right_gimbal")]
        if any(min(region["width"], region["height"]) < 16 for region in regions):
            raise ValueError("Gimbal regions are too small to observe movement at the processing resolution")
        decoder = sampled_frames(path, fps, MAX_DIMENSION, start=reference["reference_time"], duration=1/fps)
        try:
            decoded = next(decoder, None)
            if decoded is None:
                raise ValueError("Cannot decode the radio appearance reference")
            frame = decoded[1]
        finally:
            decoder.close()
        shell = RadioOutlineTracker(outline, regions, fps, backend)
        shell.initialize(frame)
        shell._lost("Reacquire this reference on the source timeline")
        transforms, masks = [], []
        yy, xx = np.mgrid[:CROP_SIZE, :CROP_SIZE]
        for region in regions:
            transforms.append(np.float64([[region["width"]/CROP_SIZE, 0, region["x"]],
                                          [0, region["height"]/CROP_SIZE, region["y"]], [0, 0, 1]]))
            cx, cy = region["center_x"]/region["width"]*CROP_SIZE, region["center_y"]/region["height"]*CROP_SIZE
            masks.append((xx-cx)**2+(yy-cy)**2 < (CROP_SIZE*.34375)**2)
        bank.append((shell, transforms, masks))
    rows, previous, previous_crops, previous_quality, active = [], None, None, 0., None
    frames = sampled_frames(path, fps, MAX_DIMENSION)
    try:
        for timestamp, frame in frames:
            reporter.update(timestamp)
            selected, transform, quality = active, None, 0.
            if active is not None and previous is not None:
                transform, quality = bank[active][0].update(previous, frame)
            if transform is None:
                # A stale flow state from an inactive view is never reused.
                choices = []
                for index, (shell, _, _) in enumerate(bank):
                    if previous is None or index != active:
                        shell._lost("Inactive appearance reference")
                    shell.index += 1
                    if shell.index % shell.retry_interval == 0:
                        result = shell._reacquire(frame, full_frame=shell.index % shell.global_interval == 0)
                        if result is not None:
                            choices.append((result[1], index, result[0]))
                if choices:
                    quality, selected, transform = max(choices, key=lambda choice: choice[0])
            crops, values = None, [0., 0.]
            motion_quality = 0.
            if transform is not None:
                shell, transforms, masks = bank[selected]
                crops = [backend.warp(frame, transform@crop_transform, (CROP_SIZE, CROP_SIZE),
                         flags=cv2.INTER_LINEAR|cv2.WARP_INVERSE_MAP, borderValue=127) for crop_transform in transforms]
                if selected == active and previous_crops is not None and previous_quality >= .35:
                    motion_quality = min(quality, previous_quality)
                    for side, (before, now, mask) in enumerate(zip(previous_crops, crops, masks)):
                        flow = cv2.calcOpticalFlowFarneback(before, now, None, .5, 3, 15, 3, 5, 1.2, 0)
                        values[side] = float(np.mean(np.linalg.norm(flow, axis=2)[mask])*fps/(CROP_SIZE*.34375))
            rows.append((timestamp, sum(values)/2, *values, motion_quality, quality,
                         selected if transform is not None else -1))
            previous, previous_crops, previous_quality = frame, crops, quality
            active = selected if transform is not None else None
    finally:
        frames.close()
    reporter.update(0, complete=True)
    keys = ("time", "activity", "left_activity", "right_activity", "quality", "pose_quality", "reference_index")
    return {**{key: np.asarray([row[i] for row in rows], float) for i, key in enumerate(keys)},
            "sample_fps": np.asarray(fps), "extraction_seconds": np.asarray(time.monotonic()-started),
            "kind": np.asarray("gimbal_activity"), "label": np.asarray("Tip-free gimbal/finger activity"),
            **backend.metadata()}
