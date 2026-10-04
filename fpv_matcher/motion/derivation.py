"""Shared visual-motion cache and sequential FPV preparation worker."""
from __future__ import annotations

import json
import os
import sys
import time
from functools import lru_cache
from pathlib import Path
from zipfile import BadZipFile

import numpy as np

from fpv_matcher.cache.fingerprints import _key, get_or_extract
from fpv_matcher.motion.lucas_kanade import LucasKanadeEstimator
from fpv_matcher.motion.visual_rotation import validate_profile
from fpv_matcher.motion.telemetry import VERSION as TELEMETRY_VERSION, cached_telemetry

VERSION = LucasKanadeEstimator.algorithm_version
MAX_DIMENSION = 960


def previous_cache_path(path, cache, fps, identity=None, camera_profile=None):
    """Older estimates remain reviewable; matching only uses the current version."""
    profile = validate_profile(camera_profile)
    profile.pop("track_full_rate", None)
    config = {"sample_fps": float(fps), "max_dimension": MAX_DIMENSION, "camera_profile": profile}
    for rate in (float(fps), int(fps) if float(fps).is_integer() else float(fps)):
        target = Path(cache) / f"fpv.{_key(str(path), 'lk-rays-relative-camera-rotation-fov-v4', {**config, 'sample_fps': rate}, identity)}.npz"
        if target.exists():
            return target
    return None


def cache_path(path, cache, fps, identity=None, camera_profile=None):
    config = {"sample_fps": float(fps), "max_dimension": MAX_DIMENSION, "camera_profile": validate_profile(camera_profile)}
    target = Path(cache) / f"fpv.{_key(str(path), VERSION, config, identity)}.npz"
    if not target.exists() and float(fps).is_integer():
        prior = Path(cache) / f"fpv.{_key(str(path), VERSION, {**config, 'sample_fps': int(fps)}, identity)}.npz"
        if prior.exists():
            return prior
    return target


def _validate(data):
    times = np.asarray(data["time"], float)
    if len(times) < 3 or not np.isfinite(times).all() or times[0] < 0 or np.any(np.diff(times) <= 0):
        raise ValueError("Not enough valid visual motion samples")
    for key in ("dx", "dy", "rotation", "quality", "magnitude", "x", "y", "z"):
        values = np.asarray(data[key], float)
        if values.shape != times.shape or not np.isfinite(values).all():
            raise ValueError("Invalid visual motion cache")


def cached_visual_motion(path, cache, fps, identity=None, progress=None, camera_profile=None):
    path = Path(path)
    signature = (path.stat().st_size, path.stat().st_mtime_ns)
    config = {"sample_fps": float(fps), "max_dimension": MAX_DIMENSION, "camera_profile": validate_profile(camera_profile)}
    if cache_path(path, cache, fps, identity, camera_profile).stem.split(".", 1)[1] != _key(str(path), VERSION, config, identity):
        config["sample_fps"] = int(fps)
    def extract():
        started = time.perf_counter()
        data = LucasKanadeEstimator(max_dimension=MAX_DIMENSION).extract(str(path), fps, progress=progress, camera_profile=camera_profile)
        if (path.stat().st_size, path.stat().st_mtime_ns) != signature:
            raise ValueError("Source changed during visual motion derivation")
        return {**data, "extraction_seconds": np.asarray(time.perf_counter() - started)}
    return get_or_extract(str(path), cache, "fpv", VERSION, config, extract, identity, validate=_validate)


def load_saved_visual(path, cache, fps, identity=None, camera_profile=None):
    """Inspect an existing cache only; playback must never start extraction."""
    source = Path(path)
    stat = source.stat()
    signature = (stat.st_size, stat.st_mtime_ns)
    target = cache_path(path, cache, fps, identity, camera_profile)
    legacy = False
    if not target.exists():
        previous = previous_cache_path(path, cache, fps, identity, camera_profile)
        if previous is not None:
            target, legacy = previous, True
    with np.load(target, allow_pickle=False) as values:
        if str(values["cache_key"].item()) != target.stem.split(".", 1)[1]:
            raise ValueError("Invalid visual motion cache identity")
        data = {key: values[key] for key in values.files if key != "cache_key"}
    _validate(data)
    data["previous_algorithm"] = np.asarray(legacy)
    current = source.stat()
    if (current.st_size, current.st_mtime_ns) != signature:
        raise ValueError("FPV source changed while reading its saved motion")
    return data


@lru_cache(maxsize=256)
def _inspect(filename, modified, size, embedded=False):
    with np.load(filename, allow_pickle=False) as data:
        if str(data["cache_key"].item()) != Path(filename).stem.split(".", 1)[1]:
            raise ValueError("Invalid cache identity")
        if embedded:
            if "unavailable" in data:
                return {"status": "unavailable", "message": str(data["unavailable"].item())}
            if not {"time", "x", "y", "z", "quality", "kind"}.issubset(data.files):
                raise ValueError("Incomplete embedded motion cache")
            return {"status": "available", "kind": str(data["kind"].item()), "samples": len(data["time"])}
        _validate(data)
        times, quality = data["time"], data["quality"]
        return {"status": "cached", "samples": len(times), "range": [float(times[0]), float(times[-1])],
                "usable_percent": round(float(np.mean(quality > .1)) * 100, 1),
                "extraction_seconds": float(data["extraction_seconds"].item()) if "extraction_seconds" in data else None,
                "cached_at": modified / 1e9,
                "tracking_fps": float(data["tracking_fps"]) if "tracking_fps" in data else None,
                "tracking_label": str(data["tracking_label"].item()) if "tracking_label" in data else "Earlier CPU extraction"}


def embedded_status(path, cache, identity=None):
    try:
        target = Path(cache) / f"telemetry.{_key(str(path), TELEMETRY_VERSION, {}, identity)}.npz"
        if not target.exists():
            return {"status": "unknown"}
        stat = target.stat()
        return _inspect(str(target), stat.st_mtime_ns, stat.st_size, True)
    except (OSError, ValueError, KeyError, EOFError, BadZipFile):
        return {"status": "unknown", "message": "Source or embedded motion cache could not be read"}


def cache_status(path, cache, fps, identity=None, camera_profile=None):
    try:
        target = cache_path(path, cache, fps, identity, camera_profile)
        if not target.exists():
            previous = previous_cache_path(path, cache, fps, identity, camera_profile)
            if previous is not None:
                stat = previous.stat()
                return {**_inspect(str(previous), stat.st_mtime_ns, stat.st_size),
                        "status": "older_algorithm", "sample_fps": fps}
            return {"status": "not_derived", "sample_fps": fps}
        stat = target.stat()
        return {**_inspect(str(target), stat.st_mtime_ns, stat.st_size), "sample_fps": fps}
    except (OSError, ValueError, KeyError, EOFError, BadZipFile):
        return {"status": "unavailable", "sample_fps": fps}


def run(request_path):
    import cv2
    from fpv_matcher.pipeline import atomic_json
    request = json.loads(Path(request_path).read_text())
    directory = Path(request["directory"])
    os.environ["FPV_MATCHER_DECODE"] = request.get("decode_mode", "auto")
    os.environ["FPV_MATCHER_TRACKING"] = request.get("tracking_backend", "auto")
    cv2.setNumThreads(1)
    rows, total, fps = [], len(request["clips"]), request["sample_fps"]
    for index, clip in enumerate(request["clips"]):
        path, started = Path(clip["path"]), time.perf_counter()
        fps = clip.get("sample_fps", request["sample_fps"])
        def report(event=None, stage="embedded"):
            event = event or {}
            atomic_json(directory / "progress.json", {**event, "stage": stage, "name": clip["name"],
                        "completed": len(rows), "total": total, "clips": rows,
                        "elapsed_seconds": round(time.perf_counter() - started, 2), "sample_fps": fps,
                        "percent": (index + float(event.get("percent", 0)) / 100) / total * 100})
        report()
        try:
            stat = path.stat()
            if [stat.st_size, stat.st_mtime_ns] != clip["signature"]:
                raise ValueError("Source changed since this task was queued")
            try:
                telemetry = cached_telemetry(path, request["cache"], clip.get("identity"))
                embedded = {"status": "available", "kind": str(telemetry["kind"].item())}
            except ValueError as exc:
                # Only a persisted unsupported-data result establishes eligibility.
                embedded = embedded_status(path, request["cache"], clip.get("identity"))
                if embedded["status"] != "unavailable":
                    raise ValueError(f"Embedded motion check failed: {exc}") from exc
            if request["action"] == "check":
                row = {"status": "checked", "embedded": embedded}
            elif embedded["status"] == "available":
                row = {"status": "skipped", "embedded": embedded, "message": "Usable embedded motion is preferred"}
            else:
                before = cache_status(path, request["cache"], fps, clip.get("identity"), request.get("visual_camera"))
                data = cached_visual_motion(path, request["cache"], fps, clip.get("identity"), lambda e: report(e, "visual"), request.get("visual_camera"))
                row = {"status": "cached", "embedded": embedded, "reused": before["status"] == "cached",
                       "samples": len(data["time"]), **cache_status(path, request["cache"], fps, clip.get("identity"), request.get("visual_camera"))}
            stat = path.stat()
            if [stat.st_size, stat.st_mtime_ns] != clip["signature"]:
                raise ValueError("Source changed during preparation")
            rows.append({**row, "name": clip["name"], "sample_fps": fps, "elapsed_seconds": round(time.perf_counter() - started, 2)})
        except (ValueError, RuntimeError, OSError, ImportError) as exc:
            rows.append({"name": clip["name"], "status": "failed", "message": str(exc),
                         "sample_fps": fps, "elapsed_seconds": round(time.perf_counter() - started, 2)})
        report({"percent": 100, "eta_seconds": 0}, "complete")
    atomic_json(directory / "preparation.json", {"clips": rows, "action": request["action"], "sample_fps": fps,
                "cached": sum(r["status"] == "cached" for r in rows),
                "skipped": sum(r["status"] == "skipped" for r in rows),
                "failed": sum(r["status"] == "failed" for r in rows), "total": total})


if __name__ == "__main__":
    run(sys.argv[1])
