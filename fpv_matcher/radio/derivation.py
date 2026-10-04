"""One cache contract for batch matching, chart review and preparing all StickCam inputs."""
from __future__ import annotations

import json
import sys
import time
from functools import lru_cache
from pathlib import Path
from zipfile import BadZipFile

import numpy as np

from fpv_matcher.cache.fingerprints import _key, get_or_extract
from fpv_matcher.config import validate_calibration
from fpv_matcher.radio.stabilization import VERSION as OUTLINE_VERSION, extract_review_sticks
from fpv_matcher.radio.stick_tracker import ALGORITHM_VERSION, MAX_DIMENSION, extract_sticks

FULL_SPAN_VERSION = "reference-backward-tracking-v1"


def specification(calibration, fps):
    calibration = validate_calibration(calibration)
    calibration.pop("stick_mode", None)  # Canonical Mode 2 observations are remapped cheaply after loading.
    calibration.pop("radio_orientation_confirmed", None)
    calibration.pop("radio_orientation_manual", None)
    if calibration.get("radio_rotation_deg") == 0:
        calibration.pop("radio_rotation_deg")  # Preserve existing upright caches.
    outline = "radio_outline" in calibration
    return ("radio-review" if outline else "radio",
            (ALGORITHM_VERSION + OUTLINE_VERSION if outline else ALGORITHM_VERSION) + (FULL_SPAN_VERSION if calibration["reference_time"] > 0 else ""),
            {"sample_fps": float(fps), "config": calibration, "max_dimension": MAX_DIMENSION})


def cache_path(path, cache, calibration, fps, identity=None):
    kind, algorithm, config = specification(calibration, fps)
    target = Path(cache) / f"{kind}.{_key(str(path), algorithm, config, identity)}.npz"
    # Older batch callers encoded integral sample rates as integers; preserve those caches too.
    if not target.exists() and float(fps).is_integer():
        legacy = {**config, "sample_fps": int(fps)}
        prior = Path(cache) / f"{kind}.{_key(str(path), algorithm, legacy, identity)}.npz"
        if prior.exists():
            return prior
    return target


def cached_sticks(path, cache, calibration, fps, identity=None, progress=None):
    kind, algorithm, config = specification(calibration, fps)
    target = cache_path(path, cache, calibration, fps, identity)
    if target.stem.split(".", 1)[1] != _key(str(path), algorithm, config, identity):
        config["sample_fps"] = int(fps)
    extractor = extract_review_sticks if "radio_outline" in config["config"] else extract_sticks
    from fpv_matcher.radio.modes import remap_cached_inputs
    def extract():
        data = extractor(str(path), fps, config["config"], progress=progress)
        return remap_cached_inputs(data, 2, config["config"].get("signs"))
    data = get_or_extract(str(path), cache, kind, algorithm, config, extract, identity, validate=_validate)
    return remap_cached_inputs(data, calibration.get("stick_mode", 2), calibration.get("signs"))


def _validate(data):
    times = data["time"]
    if times.ndim != 1 or len(times) < 3 or not np.isfinite(times).all() or times[0] < 0 or np.any(np.diff(times) <= 0):
        raise ValueError("Invalid saved StickCam timeline")
    for key in ("roll", "pitch", "yaw", "throttle", "quality"):
        values = np.asarray(data[key])
        if values.shape != times.shape or not np.isfinite(values).all():
            raise ValueError("Incomplete or invalid saved StickCam inputs")
    for key in ("quality", "roll_quality", "pitch_quality", "yaw_quality", "throttle_quality", "radio_orientation_quality"):
        if key in data:
            quality = np.asarray(data[key])
            if quality.shape != times.shape or not np.isfinite(quality).all() or ((quality < 0) | (quality > 1)).any():
                raise ValueError("Invalid saved StickCam observation quality")


def observation_status(data):
    """Describe valid coverage; a saved file is not proof of usable controls."""
    times = np.asarray(data["time"])
    per_axis = {}
    observed = np.ones(len(times), bool)
    for axis in ("roll", "pitch", "yaw"):
        key = axis + "_quality"
        quality = np.asarray(data[key] if key in data else data["quality"])
        usable = np.isfinite(quality) & (quality > .1)
        per_axis[axis] = round(float(usable.mean() * 100), 1) if len(times) else 0
        observed &= usable
    step = float(np.median(np.diff(times))) if len(times) > 1 else 0
    percent = round(float(observed.mean() * 100), 1) if len(times) else 0
    result = {"usable_percent": percent, "per_axis_usable_percent": per_axis,
              "usable_seconds": round(min(float(times[-1] - times[0]) if len(times) else 0,
                                          float(observed.sum() * step)), 2),
              "observation_state": "no_observations" if not observed.any() else "partial" if percent < 99 else "observed"}
    if "radio_orientation_quality" in data:
        result["outline_usable_percent"] = round(float((np.asarray(data["radio_orientation_quality"]) >= .35).mean() * 100), 1)
    if percent < 10:
        result["quality_warning"] = "Saved inputs contain very little jointly observed stick data. Review tracking or use audio; repeated extraction with the same calibration will not fix covered tips."
    return result


@lru_cache(maxsize=256)
def _inspect(filename, modified, size):
    # Validate/read once per cache signature; repeated UI polls reuse the summary.
    with np.load(filename, allow_pickle=False) as data:
        expected = Path(filename).stem.split(".", 1)[1]
        if str(data["cache_key"].item()) != expected or not {"time", "roll", "pitch", "yaw", "throttle", "quality"}.issubset(data.files):
            raise ValueError("Incomplete cache")
        times = data["time"]
        if len(times) < 3 or not np.isfinite(times).all() or times[0] < 0 or np.any(np.diff(times) <= 0):
            raise ValueError("Invalid saved timeline")
        _validate(data)
        return {"samples": len(times), "duration": float(times[-1] - times[0]) if len(times) else 0,
                "range": [float(times[0]), float(times[-1])] if len(times) else None,
                "cached_at": modified / 1e9, "bytes": size, **observation_status(data)}


def cache_status(path, cache, calibration, fps, identity=None):
    if not calibration:
        return {"status": "needs_calibration"}
    if calibration.get("exclude") or calibration.get("skip_motion"):
        return {"status": "excluded" if calibration.get("exclude") else "audio_only"}
    try:
        target = cache_path(path, cache, calibration, fps, identity)
        if not target.exists():
            return {"status": "not_derived", "sample_fps": fps}
        stat = target.stat()
        from fpv_matcher.radio.modes import MODES, validate_mode
        mode = validate_mode(calibration.get("stick_mode", 2))
        summary = _inspect(str(target), stat.st_mtime_ns, stat.st_size)
        old = summary["per_axis_usable_percent"]
        # Mode changes can exchange pitch with throttle; both share their
        # physical gimbal's observation quality in the canonical cache.
        physical_quality = {p: old.get(a, old["yaw"]) for p, a in MODES[2].items()}
        per_axis = {axis: physical_quality[p] for p, axis in MODES[mode].items() if axis != "throttle"}
        return {"status": "cached", "sample_fps": fps, **summary, "stick_mode": mode, "per_axis_usable_percent": per_axis}
    except (OSError, ValueError, KeyError, EOFError, BadZipFile):
        return {"status": "unavailable"}


def run(request_path):
    import cv2
    import os
    from fpv_matcher.pipeline import atomic_json
    request = json.loads(Path(request_path).read_text())
    directory = Path(request["directory"])
    os.environ["FPV_MATCHER_DECODE"] = request.get("decode_mode", "auto")
    cv2.setNumThreads(1)
    rows, total = [], len(request["clips"])
    for index, clip in enumerate(request["clips"]):
        path = Path(clip["path"])
        fps = clip.get("sample_fps", request["sample_fps"])
        started = time.perf_counter()
        def report(event):
            atomic_json(directory / "progress.json", {"stage": "stickcam", "name": clip["name"],
                        "completed": index, "total": total, "clips": rows,
                        "elapsed_seconds": round(time.perf_counter() - started, 2), "sample_fps": fps,
                        **event, "percent": (index + float(event.get("percent", 0)) / 100) / total * 100})
        report({"percent": 0})
        try:
            before = cache_status(path, request["cache"], clip["calibration"], fps, clip.get("identity"))
            data = cached_sticks(path, request["cache"], clip["calibration"], fps, clip.get("identity"), report)
            stat = path.stat()
            if [stat.st_size, stat.st_mtime_ns] != clip["signature"]:
                raise ValueError("Source changed during derivation")
            reused = before["status"] == "cached"
            elapsed = round(time.perf_counter() - started, 2)
            rows.append({"name": clip["name"], "status": "cached", "reused": reused,
                         "elapsed_seconds": elapsed, "extraction_seconds": None if reused else elapsed,
                         "sample_fps": fps, "samples": len(data["time"]), **observation_status(data)})
        except (ValueError, RuntimeError, OSError, cv2.error) as exc:
            rows.append({"name": clip["name"], "status": "failed", "elapsed_seconds": round(time.perf_counter() - started, 2),
                         "sample_fps": fps, "message": str(exc)})
        report({"percent": 100, "eta_seconds": 0})
    atomic_json(directory / "derivation.json", {"clips": rows, "sample_fps": request["sample_fps"],
                "stick_sampling_ratio": request.get("stick_sampling_ratio"),
                "complete": sum(r["status"] == "cached" for r in rows), "total": total})


if __name__ == "__main__":
    run(sys.argv[1])
