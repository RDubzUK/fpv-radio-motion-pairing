"""Batch engine shared by the CLI and web worker; no web/UI dependencies."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import cv2
import numpy as np

from fpv_matcher.audio.fingerprint import ALGORITHM_VERSION as AUDIO_VERSION
from fpv_matcher.audio.fingerprint import _empty, best_offset, extract_audio, landmark_match, score_at_offset, validate_audio
from fpv_matcher.audio.evidence import POLICY_VERSION as AUDIO_POLICY_VERSION, assess_audio_pair
from fpv_matcher.cache.fingerprints import get_or_extract
from fpv_matcher.config import calibration_for
from fpv_matcher.matching.assignment import assign, assign_segments, candidate_margin
from fpv_matcher.matching.evidence import normalize_sources, motion_enabled, audio_enabled_pair, precise_motion_enabled
from fpv_matcher.radio.activity import cached_activity, cache_status as activity_cache_status, specification as activity_specification
from fpv_matcher.matching import activity as activity_matching
from fpv_matcher.matching.correlation import MatchResult, compare, prepare, section_support
from fpv_matcher.motion.derivation import cached_visual_motion, cache_status as visual_cache_status, embedded_status
from fpv_matcher.motion.sampling import visual_fps
from fpv_matcher.motion.telemetry import cached_telemetry, body_rates, validate_motion_settings
from fpv_matcher.radio.rates import apply_rate_profile, validate_rate_profile
from fpv_matcher.video.metadata import filename_timestamp, format_timestamp, probe_video
from fpv_matcher.storage import atomic_json


def infer_clock_difference(radio_times, fpv_times, results, scores, audio, allow_single_anchor=False):
    """Anchor a camera-clock difference on independent, distinctive matches.

    Dates needn't be correct. A camera resetting to the same reading repeatedly
    contributes no ordering evidence. A lone anchor stays tentative.
    """
    if len(set(t for t in radio_times if t is not None)) < min(2, len(radio_times)) or len(
        set(t for t in fpv_times if t is not None)
    ) < min(2, len(fpv_times)):
        return None, 0.0, 0
    candidates = []
    threshold = 0.55 if allow_single_anchor and min(len(radio_times), len(fpv_times)) == 1 else 0.6
    for ri, fi in assign(scores, threshold):
        result, aud = results[ri, fi], audio[ri, fi]
        distinctive = (result.score >= .6 and result.reliable_axes >= 2 and result.peak_margin >= .035
                       and result.valid_seconds >= 15 and result.correlated_sections >= 5
                       and result.section_agreement >= .6 and result.section_spread >= 2 / 3)
        distinctive |= (aud["best_score"] >= .88 and aud["peak_margin"] >= .08 and aud["best_overlap"] >= 8
                        and abs(aud["best_offset"] - result.offset_seconds) <= .2)
        if (
            not distinctive
            or candidate_margin(scores, ri, fi, multiple_fpv=True) < 0.04
            or radio_times[ri] is None
            or fpv_times[fi] is None
        ):
            continue
        candidates.append((fpv_times[fi] - radio_times[ri] - result.offset_seconds, scores[ri, fi], ri, fi))
    if not candidates:
        return None, 0.0, 0
    groups = [[c for c in candidates if abs(c[0] - center[0]) <= 10] for center in candidates]
    chosen = max(groups, key=lambda group: (len(group), sum(item[1] for item in group)))
    delta = float(np.median([c[0] for c in chosen]))
    confidence = min(1.0, len(chosen) / 3) * min(1.0, float(np.mean([c[1] for c in chosen])) / 0.75)
    if allow_single_anchor and len(chosen) == 1:
        ri, fi = chosen[0][2], chosen[0][3]
        anchor = audio[ri, fi]
        if anchor["best_score"] >= 0.88 and anchor["peak_margin"] >= 0.08 and anchor["best_overlap"] >= 8:
            confidence = max(confidence, 0.65)
    return delta, confidence, len(chosen)


def _sample_paths(paths, percent, modified_times=None):
    """Choose a deterministic, time-spread subset while preserving the full pool."""
    paths = list(paths)
    if percent == 100 or len(paths) <= 1:
        return paths, []
    count = min(len(paths), max(1, math.ceil(len(paths) * percent / 100)))
    modified_times = modified_times or {}
    def order_key(path):
        timestamp = filename_timestamp(path.name)
        if timestamp is None:
            timestamp = modified_times.get(path.name, {}).get("timestamp")
        return (timestamp is None, timestamp if timestamp is not None else path.name.casefold(), path.name.casefold())
    ordered = sorted(paths, key=order_key)
    indexes = sorted({min(len(ordered) - 1, int((i + .5) * len(ordered) / count)) for i in range(count)})
    selected = [ordered[index] for index in indexes]
    names = {path.name for path in selected}
    return selected, [path for path in paths if path.name not in names]


def run_batch(
    fpv_paths: list[Path],
    radio_paths: list[Path],
    config: dict,
    output: Path,
    cache: Path,
    sample_fps: float = 30,
    match_fps: float = 30,
    min_overlap: float = 2,
    workers: int = 2,
    minimum_score: float = 0.55,
    audio_enabled: bool = True,
    match_target_kind: str = "all",
    match_target_name: str = "",
    match_target_names: list[str] | None = None,
    stop_on_confident_match: bool = False,
    allow_unconfigured_radio: bool = False,
    progress=None,
    make_plots: bool = True,
    rate_profile: dict | None = None,
    motion_source: str = "auto",
    camera_uptilt_deg: float = 0,
    imu_orientation: str = "zXy",
    gyro_video_offset: float = 0,
    decode_mode: str = "auto",
    tracking_backend: str = "auto",
    evidence_sources: dict | None = None,
    stick_sampling_ratio: float | None = None,
    matching_strategy: str = "selected",
    fpv_sampling_mode: str = "custom",
    fpv_visual_fps: float | None = None,
    visual_camera: dict | None = None,
    stick_mode: int = 2,
    match_sample_percent: int = 100,
    audio_boundary: int = 30,
    audio_adaptive: bool = True,
    audio_retry_limit: int = 120,
) -> dict:
    from fpv_matcher.motion.visual_rotation import validate_profile, visual_body_rates
    from fpv_matcher.radio.modes import validate_mode
    visual_camera = validate_profile(visual_camera)
    validate_mode(stick_mode)
    from fpv_matcher.radio.sampling import tracking_fps, validate_ratio
    stick_sampling_ratio = validate_ratio(stick_sampling_ratio)
    fpv_visual_fps = float(sample_fps if fpv_visual_fps is None else fpv_visual_fps)
    visual_fps(fpv_sampling_mode, fpv_visual_fps, 30)
    if (
        not 1 <= sample_fps <= 30
        or not 1 <= match_fps <= sample_fps
        or not 1 <= workers <= 4
        or not 1 <= min_overlap <= 3600
    ):
        raise ValueError(
            "Sampling must be 1–30 Hz; matching 1–sampling Hz; workers 1–4; minimum overlap 1–3600s"
        )
    if not 0 <= minimum_score <= 1:
        raise ValueError("Minimum score must be between zero and one")
    if matching_strategy not in {"selected", "automatic"}:
        raise ValueError("Choose automatic or selected-source matching")
    if match_sample_percent not in {10, 25, 50, 100}:
        raise ValueError("Quick Match sample must be 10%, 25%, 50% or all recordings")
    if audio_boundary not in {30, 60, 120, 300} or audio_retry_limit not in {30, 60, 120, 300} or not isinstance(audio_adaptive, bool):
        raise ValueError("Choose audio boundary/retry limits of 30, 60, 120 or 300 seconds")
    if not fpv_paths or not radio_paths:
        raise ValueError("Provide at least one recording of each type")
    requested_fpv, requested_radio = list(fpv_paths), list(radio_paths)
    if match_target_kind not in {"all", "fpv", "radio"}:
        raise ValueError("Match target must be all, fpv, or radio")
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    if match_target_kind != "all":
        available = requested_fpv if match_target_kind == "fpv" else requested_radio
        names = match_target_names or ([match_target_name] if match_target_name else [])
        if not isinstance(names, list) or not names or any(not isinstance(n, str) for n in names) or len(set(names)) != len(names):
            raise ValueError("Choose one or more distinct target videos")
        selected = [path for path in available if path.name in names]
        if len(selected) != len(names):
            raise ValueError("The selected target video is not in this session")
        if match_target_kind == "fpv":
            fpv_paths = selected
        else:
            radio_paths = selected
    excluded_radio = [p.name for p in radio_paths if config.get("clips", {}).get(p.name, {}).get("exclude")]
    radio_paths = [p for p in radio_paths if p.name not in excluded_radio]
    if match_target_kind != "fpv":
        fpv_paths = [p for p in fpv_paths if p.name not in config.get("no_counterpart", [])]
    full_fpv_paths, full_radio_paths = list(fpv_paths), list(radio_paths)
    modified_times = config.get("modified_times", {})
    fpv_paths, deferred_fpv_paths = _sample_paths(fpv_paths, 100 if match_target_kind == "radio" else match_sample_percent, modified_times.get("fpv"))
    radio_paths, deferred_radio_paths = _sample_paths(radio_paths, match_sample_percent if match_target_kind == "radio" else 100, modified_times.get("radio"))
    if audio_boundary not in {30, 60, 120, 300} or audio_retry_limit not in {30, 60, 120, 300} or not isinstance(audio_adaptive, bool):
        raise ValueError("Choose audio boundary/retry limits of 30, 60, 120 or 300 seconds")
    if not fpv_paths or not radio_paths:
        raise ValueError("The selected Quick Match sample must include at least one recording of each type")
    considered_fpv = [path.name for path in fpv_paths]
    considered_radio = [path.name for path in radio_paths]
    if decode_mode not in {"auto", "cpu", "cuda"}:
        raise ValueError("Choose automatic, CPU or CUDA decoding")
    if tracking_backend not in {"auto", "cpu", "cuda"}:
        raise ValueError("Choose automatic, CPU or CUDA tracking")
    os.environ["FPV_MATCHER_TRACKING"] = tracking_backend
    os.environ["FPV_MATCHER_DECODE"] = decode_mode
    rate_profile = validate_rate_profile(rate_profile)
    motion_source, camera_uptilt_deg, imu_orientation, gyro_video_offset = validate_motion_settings(motion_source, camera_uptilt_deg, imu_orientation, gyro_video_offset)
    evidence_sources = normalize_sources(evidence_sources, motion_source, audio_enabled)
    use_motion = motion_enabled(evidence_sources)
    use_precise_motion = precise_motion_enabled(evidence_sources)
    use_activity = evidence_sources["gimbal_activity"] and use_motion
    audio_enabled = audio_enabled_pair(evidence_sources)
    motion_source = ("auto" if evidence_sources["fpv_gyro"] and evidence_sources["fpv_visual"]
                     else "gyro" if evidence_sources["fpv_gyro"] else "visual")
    if not radio_paths:
        raise ValueError("Keep at least one StickCam recording in this analysis")
    if use_motion and allow_unconfigured_radio and not audio_enabled and matching_strategy != "automatic":
        raise ValueError("Audio-only StickCam matching requires boundary audio")
    output, cache = Path(output), Path(cache)
    output.mkdir(parents=True, exist_ok=True)
    for old_plot in (output / "plots").glob("*.png"):
        old_plot.unlink()
    start_time = time.monotonic()
    progress_lock = threading.RLock()
    extraction = {}

    def report(stage, message, percent, **extra):
        event = {"stage": stage, "message": message, "percent": round(percent, 1), **extra}
        with progress_lock:
            atomic_json(output / "progress.json", event)
            if progress:
                progress(event)

    report("extract", f"Reading metadata and {'boundary audio' if audio_enabled else 'selected sources'} for {len(fpv_paths) + len(radio_paths)} recordings", 0)
    cv2.setNumThreads(1)
    errors, clips = [], {"fpv": [], "radio": []}
    identities = config.get("identities", {})
    extraction_base, extraction_span = 0., 10. if audio_enabled else 25.
    total, completed = len(fpv_paths) + len(radio_paths), 0

    def extraction_progress(kind, path, event, phase="motion"):
        with progress_lock:
            if kind == "radio" and use_activity and use_precise_motion and phase in {"gimbal_activity", "motion"}:
                percent = float(event.get("percent", 0))
                event = {**event, "phase_percent": percent, "percent": percent*.35 if phase == "gimbal_activity" else 35+percent*.65}
            key = (kind, path.name)
            previous = extraction.get(key, {})
            extraction[key] = {**previous, **event, "kind": kind, "name": path.name, "phase": phase}
            fraction = sum(min(1, v.get("fraction", v.get("percent", 0) / 100)) for v in extraction.values()) / max(1, total)
            active = [v for v in extraction.values() if not v.get("finished")]
            eta = event.get("eta_seconds")
            label = "Reading recording" if phase == "metadata" else "Gimbal activity" if phase == "gimbal_activity" else "Boundary audio" if phase == "audio" else ("Preparing recording" if not use_motion else "Stick detection" if kind == "radio" else "FPV motion")
            text = f"{label}: {path.name} · {event.get('percent', 0):.1f}%"
            if eta is not None and phase in {"motion", "gimbal_activity"} and use_motion:
                text += f" · about {int(eta // 60)}m {int(eta % 60)}s remaining for motion extraction"
            report("extract", text, extraction_base + fraction * extraction_span, active_clips=active, completed=completed, total=total)

    def empty_motion(duration):
        data = {k: np.zeros(2) for k in ("roll", "pitch", "yaw", "throttle", "rotation", "dx", "dy", "quality")}
        data["time"] = np.array([0.0, max(0.0, duration - 1 / sample_fps)])
        return data

    def process(kind, path):
        """Read cheap evidence first; vision failures must not discard audio."""
        extraction_progress(kind, path, {"percent": 0, "eta_seconds": None}, phase="metadata")
        signature = [path.stat().st_size, path.stat().st_mtime_ns]
        info = probe_video(path)
        info["mtime_ns"] = signature[1]
        modified = config.get("modified_times", {}).get(kind, {}).get(path.name, {})
        info["modified_time"] = modified.get("timestamp")
        info["modified_time_source"] = modified.get("source", "filesystem")
        info["modified_start_clock"] = (modified["timestamp"] - (info["duration"] if config.get("clock_settings", {}).get("modified_kind", "end") == "end" else 0)
                                         if isinstance(modified.get("timestamp"), (int, float)) else None)
        identity = identities.get(kind, {}).get(path.name)
        info["motion_used"] = False
        info["gimbal_activity_used"] = False
        info["source_usable"] = True
        info["motion_source"] = "deferred" if use_motion else "disabled"
        info["motion_label"] = "Motion awaiting evidence screening" if use_motion else "Motion matching disabled"
        audio = _empty()
        info["audio_used"] = False
        if signature != [path.stat().st_size, path.stat().st_mtime_ns]:
            raise ValueError("Recording changed while reading it; wait until copying finishes, then rerun")
        return {"path": path, "name": path.name, "info": info, "signature": signature,
            "motion": empty_motion(info["duration"]), "audio": audio, "audio_by_boundary": {audio_boundary: audio}, "tracking_quality": 0.,
            "clock": filename_timestamp(path.name) if evidence_sources["filename_timestamps"] else None}

    def load_motion(kind, clip):
        loading_started = time.monotonic()
        path, info = clip["path"], clip["info"]
        identity = identities.get(kind, {}).get(path.name)
        extraction_progress(kind, path, {"percent": 0, "eta_seconds": None})
        if kind == "fpv":
            motion = None
            telemetry_note = None
            if motion_source != "visual":
                try:
                    embedded_cached = embedded_status(path, cache, identity)["status"] == "available"
                    telemetry = cached_telemetry(path, cache, identity)
                    motion = body_rates(telemetry, camera_uptilt_deg, imu_orientation, gyro_video_offset)
                    info["motion_source"] = str(telemetry["kind"].item())
                    info["motion_label"] = str(telemetry["label"].item())
                    info["motion_reused"] = embedded_cached
                except (ValueError, RuntimeError, ImportError) as exc:
                    telemetry_note = str(exc)
            if motion is None and evidence_sources["fpv_visual"]:
                visual_rate = visual_fps(fpv_sampling_mode, fpv_visual_fps, info.get("fps"))
                info["visual_sample_fps"] = visual_rate
                visual_cached = visual_cache_status(path, cache, visual_rate, identity, visual_camera)["status"] == "cached"
                motion = cached_visual_motion(path, cache, visual_rate, identity,
                    progress=lambda event: extraction_progress(kind, path, event), camera_profile=visual_camera)
                motion = visual_body_rates(motion, camera_uptilt_deg)
                info["motion_source"] = "visual"
                info["motion_label"] = "Experimental visual body angular rates"
                info["visual_camera"] = visual_camera
                info["motion_reused"] = visual_cached
                if telemetry_note:
                    info["telemetry_note"] = telemetry_note
            if motion is None:
                motion = empty_motion(info["duration"])
                info["motion_source"] = "unavailable"
                info["motion_label"] = "Selected embedded motion unavailable"
                info["telemetry_note"] = telemetry_note
                info["motion_skipped_reason"] = "No supported embedded FPV motion and visual fallback is disabled"
            else:
                info["motion_used"] = True
            info["camera_uptilt_deg"] = camera_uptilt_deg
            info["imu_orientation"] = imu_orientation
            info["gyro_video_offset"] = gyro_video_offset
        else:
            clip_config = (
                config.get("clips", {}).get(path.name)
                if "clips" in config
                else config.get("default", config)
            )
            if clip_config is None and (allow_unconfigured_radio or matching_strategy == "automatic"):
                clip_config = {"skip_motion": True}
            if clip_config and clip_config.get("skip_motion"):
                if not audio_enabled and matching_strategy != "automatic":
                    raise ValueError("Unconfigured or audio-only StickCam requires both audio sources")
                motion = empty_motion(info["duration"])
                info["motion_source"] = "disabled"
                info["motion_label"] = "StickCam calibration needed for motion fallback"
                info["motion_skipped_reason"] = "StickCam needs a confirmed calibration before deriving inputs"
                info["needs_calibration"] = True
            else:
                calibration = calibration_for(config, path.name)
                motion = empty_motion(info["duration"])
                if use_activity:
                    try:
                        before = activity_cache_status(path, cache, calibration, identity)
                        clip["activity"] = cached_activity(path, cache, calibration, identity,
                            lambda event: extraction_progress(kind, path, event, "gimbal_activity"))
                        info["gimbal_activity_used"] = True
                        info["gimbal_activity_reused"] = before["status"] == "cached"
                        info["gimbal_activity"] = activity_cache_status(path, cache, calibration, identity)
                    except (ValueError, RuntimeError, OSError) as exc:
                        info["gimbal_activity_error"] = str(exc)
                if use_precise_motion and all("tip_x" in calibration[side] for side in ("left_gimbal", "right_gimbal")):
                    from fpv_matcher.radio.derivation import cached_sticks, cache_status as stick_cache_status, observation_status
                    radio_fps = tracking_fps(sample_fps, stick_sampling_ratio, info.get("fps"))
                    info["stick_sample_fps"] = radio_fps
                    info["motion_reused"] = stick_cache_status(path, cache, calibration, radio_fps, identity)["status"] == "cached"
                    motion = cached_sticks(path, cache, calibration, radio_fps, identity,
                                           lambda event: extraction_progress(kind, path, event))
                    info["motion_used"] = True
                    info["motion_source"] = "stick_inputs"
                    info["stick_observations"] = observation_status(motion)
                else:
                    info["motion_source"] = "gimbal_activity" if info["gimbal_activity_used"] else "unavailable"
                    info["motion_label"] = "Tip-free gimbal movement loaded" if info["gimbal_activity_used"] else info.get("gimbal_activity_error", "Gimbal activity unavailable")
                    if use_precise_motion:
                        info["motion_skipped_reason"] = "Mark both tips to enable precise stick-input derivation; gimbal activity is separate"
        if clip["signature"] != [path.stat().st_size, path.stat().st_mtime_ns]:
            raise ValueError(
                "Recording changed during extraction; wait until it finishes copying, then rerun"
            )
        clip["motion"] = motion
        info["motion_loading_seconds"] = round(time.monotonic() - loading_started, 3)
        clip["tracking_quality"] = float(np.mean(motion["quality"])) if len(motion["quality"]) else 0.0
        return clip

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(process, kind, path): (kind, path)
            for kind, paths in (("fpv", fpv_paths), ("radio", radio_paths))
            for path in paths
        }
        for future in as_completed(futures):
            kind, path = futures[future]
            try:
                clip = future.result()
                clips[kind].append(clip)
                evidence = (f"tracking quality {clip['tracking_quality']:.2f}" if clip["info"]["motion_used"]
                            else "motion skipped")
                if audio_enabled:
                    evidence += " · boundary audio loaded" if clip["info"]["audio_used"] else " · no usable audio"
                message = f"{completed + 1}/{total}: {path.name} · {evidence}"
            except Exception as exc:
                errors.append({"kind": kind, "name": path.name, "phase": "metadata", "error": str(exc)})
                try:
                    unavailable_stat = path.stat()
                    unavailable_size, unavailable_mtime = unavailable_stat.st_size, unavailable_stat.st_mtime_ns
                except OSError:
                    unavailable_size, unavailable_mtime = 0, None
                clips[kind].append({"path": path, "name": path.name, "motion": empty_motion(0),
                    "audio": _empty(), "clock": None, "tracking_quality": 0.,
                    "info": {"duration": 0., "fps": 0., "width": 0, "height": 0, "has_audio": False,
                             "size": unavailable_size, "mtime_ns": unavailable_mtime,
                             "source_usable": False, "source_error": str(exc), "motion_used": False,
                             "audio_used": False, "motion_source": "unavailable", "motion_label": "Source unavailable"}})
                message = f"Source unavailable: {path.name}: {exc}"
            with progress_lock:
                completed += 1
                extraction[(kind, path.name)] = {"fraction": 1, "finished": True}
                fraction = sum(min(1, v.get("fraction", v.get("percent", 0) / 100 * .9)) for v in extraction.values()) / total
                report("extract", message, extraction_base + fraction * extraction_span, completed=completed, total=total, errors=len(errors),
                       active_clips=[v for v in extraction.values() if not v.get("finished")])
    for kind in clips:
        clips[kind].sort(key=lambda clip: clip["name"])
    radio, fpv = clips["radio"], clips["fpv"]
    if not radio or not fpv:
        atomic_json(output / "errors.json", {"errors": errors})
        raise ValueError(
            "No usable recordings of both types remain. " + "; ".join(e["error"] for e in errors[:3])
        )
    audio_pair_possible = audio_enabled and any(c["info"].get("has_audio") for c in fpv) and any(c["info"].get("has_audio") for c in radio)
    if audio_pair_possible:
        tasks = [(kind, c) for kind, items in clips.items() for c in items if c["info"].get("has_audio") and c["info"]["source_usable"]]
        extraction.clear()
        extraction_base, extraction_span, total, completed = 10., 15., len(tasks), 0
        def load_audio(kind, clip):
            path = clip["path"]
            extraction_progress(kind, path, {"percent": 0}, "audio")
            identity = identities.get(kind, {}).get(clip["name"])
            data = get_or_extract(str(path), cache, "audio", AUDIO_VERSION,
                {"boundary_seconds": audio_boundary}, lambda: extract_audio(str(path), audio_boundary), identity, validate=validate_audio)
            clip["audio"], clip["audio_by_boundary"][audio_boundary] = data, data
            clip["info"]["audio_used"] = bool(len(data["time"]))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(load_audio, kind, clip): (kind, clip) for kind, clip in tasks}
            for future in as_completed(futures):
                kind, clip = futures[future]
                try:
                    future.result()
                except (ValueError, RuntimeError, OSError, ImportError) as exc:
                    clip["info"]["audio_error"] = str(exc)
                completed += 1
                extraction[(kind, clip["name"])] = {"finished": True, "percent": 100}
                report("extract", f"Boundary audio loaded: {clip['name']}", 10+completed/max(1,total)*15,
                       completed=completed, total=total, active_clips=[v for v in extraction.values() if not v.get("finished")])
    elif audio_enabled:
        report("audio_screening", "No audio stream on both feeds; skipping audio extraction and retries", 25)
    for kind, items in clips.items():
        errors.extend({"kind": kind, "name": c["name"], "phase": "audio", "error": c["info"]["audio_error"]}
                      for c in items if c["info"].get("audio_error"))

    audio_checks, audio_auto_pairs = {}, set()
    audio_screening = {"enabled": bool(audio_pair_possible),
                       "policy": AUDIO_POLICY_VERSION, "eligible_pairs": 0, "distinctive_pairs": 0}
    if audio_screening["enabled"]:
        count = len(radio) * len(fpv)
        report("audio_screening", "Checking independent audio fingerprints and trends before deriving motion", 25, completed=0, total=count)
        eligible, owners = np.zeros((len(radio), len(fpv))), np.zeros((len(radio), len(fpv)))
        levels = [audio_boundary] + ([v for v in (60, 120, 300) if audio_boundary < v <= audio_retry_limit] if audio_adaptive else [])
        pending = {fi for fi, c in enumerate(fpv) if c["info"]["audio_used"] and c["info"]["source_usable"]}
        reused = 0
        rounds = 0
        for boundary in levels:
            if not pending:
                break
            rounds += 1
            prepared = {"radio": {}, "fpv": {}}
            for kind, items in (("radio", radio), ("fpv", fpv)):
                for index, clip in enumerate(items):
                    if kind == "fpv" and index not in pending:
                        continue
                    if boundary == audio_boundary:
                        prepared[kind][index] = clip["audio"]
                        continue
                    if not clip["info"].get("has_audio") or not clip["info"]["source_usable"]:
                        prepared[kind][index] = _empty()
                        continue
                    report("audio_screening", f"Audio retry {boundary}s per end · {clip['name']}", 26)
                    try:
                        identity = identities.get(kind, {}).get(clip["name"])
                        prepared[kind][index] = get_or_extract(str(clip["path"]), cache, "audio", AUDIO_VERSION,
                            {"boundary_seconds": boundary}, lambda c=clip: extract_audio(str(c["path"]), boundary), identity, validate=validate_audio)
                    except (ValueError, RuntimeError, OSError) as exc:
                        prepared[kind][index] = _empty()
                        errors.append({"kind": kind, "name": clip["name"], "phase": "audio retry", "error": str(exc)})
            for kind, prepared_clips in prepared.items():
                for index, data in prepared_clips.items():
                    clip = radio[index] if kind == "radio" else fpv[index]
                    clip.setdefault("audio_by_boundary", {})[boundary] = data
                    clip["info"]["audio_used"] |= bool(len(data["time"]))
            for fi in sorted(pending):
                for ri, rc in enumerate(radio):
                    fc = fpv[fi]
                    key = hashlib.sha256(json.dumps([AUDIO_POLICY_VERSION, AUDIO_VERSION, str(rc["path"].resolve()), rc["signature"],
                        str(fc["path"].resolve()), fc["signature"], boundary, min_overlap]).encode()).hexdigest()
                    saved = cache / "audio-comparisons" / (key + ".json")
                    try:
                        check = json.loads(saved.read_text())
                        if check.get("comparison_key") != key or not isinstance(check.get("sections"), dict):
                            raise ValueError("Invalid comparison checkpoint")
                        reused += 1
                    except (OSError, ValueError):
                        check = assess_audio_pair(prepared["radio"][ri], prepared["fpv"][fi], min_overlap)
                        check.update(comparison_key=key, boundary_seconds=boundary)
                        atomic_json(saved, check)
                    old = audio_checks.get((ri, fi))
                    rank = lambda c: (c["eligible"], c["score"] if c["eligible"] else c["best_score"])
                    if old is None or rank(check) > rank(old):
                        audio_checks[ri, fi] = check
                    best = audio_checks[ri, fi]
                    owners[ri, fi] = .6 * best["landmark_strength"] + .4 * best["trend_score"]
                    common = min(rc["info"]["duration"] - max(0., best["offset"]), fc["info"]["duration"] - max(0., -best["offset"]))
                    if best["eligible"] and common >= min_overlap:
                        eligible[ri, fi] = best["score"]
                report("audio_screening", f"Checked {boundary}s boundary audio · {fc['name']} · {reused} saved comparisons reused", 30)
            pending = {fi for fi in pending if not any(eligible[ri, fi] > 0 and
                       candidate_margin(owners, ri, fi, multiple_fpv=True) >= .1 for ri in range(len(radio)))}
        audio_screening.update(boundary_seconds=audio_boundary, retry_limit=audio_retry_limit,
                               adaptive=audio_adaptive, reused_comparisons=reused, rounds=rounds)
        audio_screening["eligible_pairs"] = int(np.count_nonzero(eligible))
        for ri, fi in np.argwhere(eligible > 0):
            check = audio_checks[int(ri), int(fi)]
            check["owner_margin"] = candidate_margin(owners, int(ri), int(fi), multiple_fpv=True)
            if check["owner_margin"] < .1:
                eligible[ri, fi] = 0.
                check["reasons"].append("Another StickCam recording has similar audio evidence")
        audio_auto_pairs = set(assign_segments(eligible, {key: v["offset"] for key, v in audio_checks.items()},
            [c["info"]["duration"] for c in radio], [c["info"]["duration"] for c in fpv], max(.55, minimum_score)))
        audio_screening["distinctive_pairs"] = len(audio_auto_pairs)
        for key, check in audio_checks.items():
            if check["eligible"] and eligible[key] > 0 and key not in audio_auto_pairs:
                check["reasons"].append("Predicted StickCam coverage overlaps another proposed FPV segment")
        report("audio_screening", f"{len(audio_auto_pairs)} distinctive audio proposals; preparing unresolved motion evidence", 35)

    resolved_fpv = {fi for _, fi in audio_auto_pairs}
    unresolved_fpv = {fi for fi, clip in enumerate(fpv) if fi not in resolved_fpv and clip["info"]["source_usable"]}
    calibrated_radio = set()
    if matching_strategy == "automatic" and use_motion and unresolved_fpv:
        for ri, clip in enumerate(radio):
            if not clip["info"]["source_usable"]:
                continue
            clip_config = config.get("clips", {}).get(clip["name"], config.get("default", config))
            try:
                if clip_config.get("skip_motion"):
                    raise ValueError("StickCam motion is disabled for this recording")
                calibration = calibration_for(config, clip["name"])
                precise_ready = use_precise_motion and all("tip_x" in calibration[side] for side in ("left_gimbal", "right_gimbal"))
                activity_ready = False
                if use_activity:
                    try:
                        activity_specification(calibration)
                        activity_ready = True
                    except ValueError:
                        pass
                if not precise_ready and not activity_ready:
                    raise ValueError("Save radio outline/gimbal regions or complete stick-tip calibration")
                calibrated_radio.add(ri)
            except (ValueError, KeyError, TypeError, AttributeError):
                clip["info"]["needs_calibration"] = True
                clip["info"]["motion_source"] = "disabled"
                clip["info"]["motion_label"] = "StickCam calibration needed for motion fallback"
                clip["info"]["motion_skipped_reason"] = "Save a radio outline and both gimbal regions for movement matching, or mark stick tips for precise inputs"
    motion_tasks = []
    if use_motion:
        for kind, items in clips.items():
            for index, clip in enumerate(items):
                if not clip["info"]["source_usable"]:
                    clip["info"]["motion_skipped_reason"] = "Source could not be read"
                    continue
                if matching_strategy == "automatic" and unresolved_fpv:
                    if kind == "radio" and index not in calibrated_radio:
                        continue
                    if kind == "fpv" and index not in resolved_fpv and not calibrated_radio:
                        clip["info"]["motion_source"] = "deferred"
                        clip["info"]["motion_label"] = "Motion fallback awaits StickCam calibration"
                        clip["info"]["motion_skipped_reason"] = "No confirmed StickCam calibration is available for motion comparison"
                        continue
                skip = matching_strategy == "automatic" and ((kind == "fpv" and index in resolved_fpv)
                                                               or (kind == "radio" and not unresolved_fpv))
                if skip:
                    clip["info"]["motion_source"] = "deferred"
                    clip["info"]["motion_label"] = "Motion skipped after distinctive audio evidence"
                    clip["info"]["motion_skipped_reason"] = ("Distinctive audio identifies this FPV segment; alignment still needs review"
                        if kind == "fpv" else "No unresolved FPV recordings require StickCam movement extraction")
                else:
                    motion_tasks.append((kind, clip))
    extraction.clear()
    extraction_base, extraction_span = (35., 30.) if audio_screening["enabled"] else (25., 40.)
    total, completed = len(motion_tasks), 0
    if motion_tasks:
        report("extract", f"Loading saved motion or deriving fallback for {len(motion_tasks)} recordings", extraction_base,
               completed=0, total=total)
        stages = ([[task for task in motion_tasks if task[0] == "fpv"],
                   [task for task in motion_tasks if task[0] == "radio"]]
                  if matching_strategy == "automatic" else [motion_tasks])
        for stage_tasks in stages:
            if not stage_tasks:
                continue
            if matching_strategy == "automatic" and stage_tasks[0][0] == "radio" and not any(
                    c["info"]["motion_used"] for c in fpv if c["info"]["source_usable"]):
                for _, clip in stage_tasks:
                    clip["info"].update(motion_source="deferred", motion_label="StickCam extraction deferred",
                        motion_skipped_reason="No usable selected FPV motion is available for unresolved clips; StickCam extraction was deferred")
                    completed += 1
                report("extract", "No usable FPV motion to compare; StickCam extraction deferred", extraction_base + extraction_span,
                       completed=completed, total=total)
                continue
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(load_motion, kind, clip): (kind, clip) for kind, clip in stage_tasks}
                for future in as_completed(futures):
                    kind, clip = futures[future]
                    try:
                        future.result()
                        message = f"{clip['name']}: {clip['info'].get('motion_label', 'StickCam movement loaded')}"
                    except Exception as exc:
                        clip["info"]["motion_used"] = False
                        clip["info"]["motion_source"] = "unavailable"
                        clip["info"]["motion_label"] = "Motion fallback unavailable"
                        clip["info"]["motion_error"] = str(exc)
                        clip["info"]["motion_skipped_reason"] = str(exc)
                        if kind == "radio":
                            try:
                                calibration_for(config, clip["name"])
                            except (ValueError, KeyError, TypeError):
                                clip["info"]["needs_calibration"] = True
                        errors.append({"kind": kind, "name": clip["name"], "phase": "motion", "error": str(exc)})
                        message = f"{clip['name']}: retained audio; motion unavailable: {exc}"
                    with progress_lock:
                        completed += 1
                        extraction[(kind, clip["name"])] = {"fraction": 1, "finished": True}
                        report("extract", message, extraction_base + completed / max(1, total) * extraction_span,
                               completed=completed, total=total, errors=len(errors),
                               active_clips=[v for v in extraction.values() if not v.get("finished")])
    # Audio screening and motion can be far apart in a long batch. Never use a
    # fingerprint from bytes that changed while other recordings were prepared.
    for kind, items in clips.items():
        for clip in items:
            if not clip["info"]["source_usable"]:
                continue
            try:
                current = clip["path"].stat()
                current_signature = [current.st_size, current.st_mtime_ns]
            except OSError:
                current_signature = None
            if current_signature != clip["signature"]:
                reason = "Source changed or disappeared during analysis; restore it and rerun"
                clip["info"].update(source_usable=False, source_error=reason, audio_used=False, motion_used=False,
                                    motion_source="unavailable", motion_skipped_reason=reason)
                clip["info"]["gimbal_activity_used"] = False
                clip.pop("activity", None)
                clip["audio"], clip["motion"] = _empty(), empty_motion(0)
                clip["clock"] = None
                errors.append({"kind": kind, "name": clip["name"], "phase": "source", "error": reason})
    valid_pairs = {(ri, fi) for ri, rc in enumerate(radio) for fi, fc in enumerate(fpv)
                   if rc["info"]["source_usable"] and fc["info"]["source_usable"]}
    audio_auto_pairs &= valid_pairs
    audio_checks = {key: value for key, value in audio_checks.items() if key in valid_pairs}
    audio_screening["distinctive_pairs"] = len(audio_auto_pairs)
    rdata = [prepare(apply_rate_profile(c["motion"], rate_profile), match_fps, True) if use_precise_motion and c["info"]["motion_used"] else None for c in radio]
    fdata = [prepare(c["motion"], match_fps, False) if use_precise_motion and c["info"]["motion_used"] else None for c in fpv]
    rhythm_results = {}
    rhythm_enabled = evidence_sources.get("movement_rhythm", False) and use_precise_motion
    if rhythm_enabled:
        from fpv_matcher.matching import rhythm
        rhythm_fps = min(10., match_fps)
        rhythm_radio = [rhythm.prepare(c["motion"], rhythm_fps, True) if c["info"]["motion_used"] else None for c in radio]
        rhythm_fpv = [rhythm.prepare(c["motion"], rhythm_fps, False) if c["info"]["motion_used"] else None for c in fpv]

    activity_results = {}
    if use_activity:
        activity_radio = [activity_matching.prepare_signal(c["activity"], "activity") if c.get("activity") is not None else None for c in radio]
        activity_fpv = [activity_matching.prepare_fpv(c["motion"]) if c["info"]["motion_used"] else None for c in fpv]


    def pair_audio(ri, fi):
        boundary = audio_checks.get((ri, fi), {}).get("boundary_seconds", audio_boundary)
        return (radio[ri].get("audio_by_boundary", {}).get(boundary, radio[ri]["audio"]),
                fpv[fi].get("audio_by_boundary", {}).get(boundary, fpv[fi]["audio"]))

    def pair_motion(ri, fi):
        return rdata[ri] is not None and fdata[fi] is not None

    def correlate_pair(ri, fi, window=None):
        if not pair_motion(ri, fi):
            return MatchResult(0.0, 0.0, 0.0, {})
        return compare(rdata[ri], fdata[fi], match_fps, min_overlap, window)

    matrix = np.zeros((len(radio), len(fpv)))
    results, audio_results, basis = {}, {}, {}
    pairs = len(radio) * len(fpv)
    report("compare", f"Comparing {len(radio)} × {len(fpv)} recordings", 65, completed=0, total=pairs)
    done = 0
    for ri, rc in enumerate(radio):
        for fi, fc in enumerate(fpv):
            ra, fa = pair_audio(ri, fi)
            result = correlate_pair(ri, fi)
            if rhythm_enabled and pair_motion(ri, fi):
                rhythm_results[ri, fi] = rhythm.compare(rhythm_radio[ri], rhythm_fpv[fi], min_overlap)
            if use_activity and activity_radio[ri] is not None and activity_fpv[fi] is not None and (ri, fi) in valid_pairs:
                activity_results[ri, fi] = activity_matching.compare(activity_radio[ri], activity_fpv[fi], min_overlap)

            aud_score, aud_overlap = score_at_offset(*pair_audio(ri, fi), result.offset_seconds)
            audio_check = audio_checks.get((ri, fi))
            if audio_check:
                best_score, aud_offset, best_overlap, aud_margin = (audio_check["best_score"], audio_check["best_offset"],
                    audio_check["best_overlap"], audio_check["peak_margin"])
            else:
                best_score, aud_offset, best_overlap, aud_margin = best_offset(*pair_audio(ri, fi), min_overlap)
            aud_quality = min(float(ra["quality"]), float(fa["quality"]))
            # Audio can reinforce motion, or supply a explicitly reviewable proposal
            # when two microphones have a strong, distinctive shared edge segment.
            combined, source = result.score, "motion" if pair_motion(ri, fi) else "no matching evidence"
            if aud_overlap >= 5 and aud_quality >= 0.1:
                combined += 0.12 * aud_quality * max(0, aud_score - 0.35) * (1 - combined)
            if best_score >= 0.82 and aud_margin >= 0.08 and best_overlap >= 8:
                candidate = correlate_pair(ri, fi, (aud_offset - 0.4, aud_offset + 0.4))
                if candidate.score > result.score - 0.06 and candidate.score >= 0.35:
                    result = candidate
                    result.offset_seconds = aud_offset
                    aud_score, aud_overlap = score_at_offset(*pair_audio(ri, fi), aud_offset)
                    combined = max(combined, 0.85 * result.score + 0.15 * best_score, 0.7 * best_score)
                    source = "motion + boundary audio"
                elif result.score < 0.35:
                    result = correlate_pair(ri, fi, (aud_offset - 1 / match_fps, aud_offset + 1 / match_fps))
                    result.offset_seconds = aud_offset
                    result.overlap_seconds = max(
                        0.0,
                        min(
                            rc["info"]["duration"] - max(0.0, aud_offset),
                            fc["info"]["duration"] - max(0.0, -aud_offset),
                        ),
                    )
                    combined = best_score * 0.7
                    source = "boundary audio · review"
            if (ri, fi) in audio_auto_pairs:
                result = MatchResult(0., audio_check["offset"], 0., {}, peak_margin=audio_check["landmark_peak_margin"])
                result.overlap_seconds = max(0., min(rc["info"]["duration"] - max(0., result.offset_seconds),
                    fc["info"]["duration"] - max(0., -result.offset_seconds)))
                aud_score, aud_overlap = score_at_offset(*pair_audio(ri, fi), result.offset_seconds, min_overlap)
                combined, source = audio_check["score"], "spectral fingerprints + audio trends · review"
            matrix[ri, fi] = combined
            results[ri, fi], basis[ri, fi] = result, source
            audio_results[ri, fi] = {
                "score_at_offset": aud_score,
                "overlap": aud_overlap,
                "best_score": best_score,
                "best_offset": aud_offset,
                "best_overlap": best_overlap,
                "peak_margin": aud_margin,
                "quality": aud_quality,
            }
            done += 1
            if done % max(1, pairs // 80) == 0 or done == pairs:
                report(
                    "compare",
                    f"Compared {done}/{pairs} candidates",
                    65 + done / pairs * 20,
                    completed=done,
                    total=pairs,
                )
    if rhythm_results:
        rhythm_scores = np.zeros_like(matrix)
        for key, check in rhythm_results.items():
            rhythm_scores[key] = check["score"]
        for (ri, fi), check in rhythm_results.items():
            separation = candidate_margin(rhythm_scores, ri, fi, multiple_fpv=True)
            check.update(candidate_margin=float(separation), suggested=bool(check["eligible"] and separation >= .06))
            if check["eligible"] and not check["suggested"]:
                check["reason"] = "Similar rhythms occur in competing StickCam candidates; no distinct shortlist proposal."
    if activity_results:
        activity_scores = np.zeros_like(matrix)
        for key, check in activity_results.items():
            activity_scores[key] = check["score"]
        for (ri, fi), check in activity_results.items():
            separation = candidate_margin(activity_scores, ri, fi, multiple_fpv=True)
            check.update(candidate_margin=float(separation), suggested=bool(check["eligible"] and separation >= .04))
            if check["eligible"] and not check["suggested"]:
                check["reason"] = "Other StickCam clips have similar gimbal activity; compare the competing candidates."
            elif check["suggested"] and (separation < .08 or check["sections"]["matched"] < 3):
                check["reason"] = "Tentative movement suggestion: limited sections or a close competitor. Review visible events."
    # Rhythm/activity shortlists never enter assignment or automatic clock matrices.
    clock_delta, clock_confidence, clock_support = infer_clock_difference(
        [c["clock"] for c in radio], [c["clock"] for c in fpv], results, matrix, audio_results,
        allow_single_anchor=match_target_kind != "all" or (match_sample_percent < 100 and min(len(radio), len(fpv)) == 1),
    )
    clock_source = "filename" if clock_delta is not None else None
    if clock_delta is None:
        clock_delta, clock_confidence, clock_support = infer_clock_difference(
            [c["info"].get("modified_start_clock") for c in radio],
            [c["info"].get("modified_start_clock") for c in fpv], results, matrix, audio_results,
            allow_single_anchor=match_target_kind != "all" or (match_sample_percent < 100 and min(len(radio), len(fpv)) == 1),
        )
        if clock_delta is not None:
            clock_source = "file modified time, adjusted by duration"
    agreements = {}
    confirmed = config.get("confirmed_clocks", [])
    if confirmed:
        model = next((m for m in confirmed if m["method"] == "modified" and m["consistent"]), None) or next((m for m in confirmed if m["consistent"]), None)
        clock_delta = -model["delta_seconds"] if model else None
        clock_support = model["independent_flights"] if model else 0
        clock_confidence = min(1., clock_support / 2) if model else 0.
        clock_source = ("filename" if model["method"] == "filename" else "file modified time, adjusted by duration") if model else None
    # The old search shortcut assumes one FPV recorder clock. Mixed recorder
    # sessions use independent content searches and the per-recorder reviewed
    # clock expansion in the web layer; never apply one drone's prior to another.
    recorder_labels = set(config.get("fpv_recorders", {}).values())
    several_clocks = any(sum(m["method"] == method for m in confirmed) > 1 for method in ("modified", "filename"))
    if len(recorder_labels) > 1 or several_clocks:
        clock_delta, clock_confidence, clock_support, clock_source = None, 0., 0, None
    if clock_delta is not None:
        for ri, rc in enumerate(radio):
            for fi, fc in enumerate(fpv):
                radio_time = rc["clock"] if clock_source == "filename" else rc["info"].get("modified_start_clock")
                fpv_time = fc["clock"] if clock_source == "filename" else fc["info"].get("modified_start_clock")
                if radio_time is None or fpv_time is None:
                    continue
                expected = fpv_time - radio_time - clock_delta
                if (
                    pair_motion(ri, fi)
                    and clock_confidence >= 0.6
                    and -fc["info"]["duration"] + min_overlap
                    <= expected
                    <= rc["info"]["duration"] - min_overlap
                ):
                    candidate = compare(
                        rdata[ri], fdata[fi], match_fps, min_overlap, (expected - 2, expected + 2)
                    )
                    audio_score, overlap = score_at_offset(*pair_audio(ri, fi), candidate.offset_seconds)
                    shared = min(
                        rc["info"]["duration"] - max(0.0, expected),
                        fc["info"]["duration"] - max(0.0, -expected),
                    )
                    # Recordings can have very different start/end times. Require
                    # measured common evidence, never a fraction of clip length.
                    enough_evidence = shared >= min_overlap and candidate.valid_seconds >= min_overlap
                    if (
                        candidate.score >= 0.25
                        and enough_evidence
                        and matrix[ri, fi] < 0.65
                        and (
                            abs(results[ri, fi].offset_seconds - expected) > 2
                            or matrix[ri, fi] < minimum_score
                        )
                    ):
                        results[ri, fi] = candidate
                        # A camera clock narrows the offset search. It cannot
                        # turn weak motion into a passing pairing score.
                        matrix[ri, fi] = max(matrix[ri, fi], candidate.score)
                        basis[ri, fi] = "camera clock + motion · review"
                error = results[ri, fi].offset_seconds - expected
                agreement = float(np.exp(-0.5 * (error / 10) ** 2))
                agreements[ri, fi] = agreement
                # Filename clocks provide context and a search window only;
                # they do not increase a content-derived pairing score.
    # Refine likely motion pairs at the full extraction rate. The expensive work
    # is O(number of clips), while the full candidate matrix remains coarse.
    likely = set(assign_segments(matrix, {key: value.offset_seconds for key, value in results.items()},
                  [c["info"]["duration"] for c in radio], [c["info"]["duration"] for c in fpv], minimum_score))
    likely.update(
        (ri, int(np.argmax(matrix[ri]))) for ri in range(len(radio)) if np.max(matrix[ri]) >= minimum_score
    )
    report("refine", f"Refining {len(likely)} likely alignments at {sample_fps:g} Hz", 86)
    if sample_fps > match_fps:
        prepared_r, prepared_f = {}, {}
        for index, (ri, fi) in enumerate(sorted(likely)):
            if basis[ri, fi] == "motion" or basis[ri, fi].startswith("camera clock"):
                if ri not in prepared_r:
                    prepared_r[ri] = prepare(
                        apply_rate_profile(radio[ri]["motion"], rate_profile), sample_fps, True
                    )
                if fi not in prepared_f:
                    prepared_f[fi] = prepare(fpv[fi]["motion"], sample_fps, False)
                old = results[ri, fi]
                refined = compare(
                    prepared_r[ri],
                    prepared_f[fi],
                    sample_fps,
                    min_overlap,
                    (old.offset_seconds - 0.6, old.offset_seconds + 0.6),
                )
                if refined.score > 0:
                    refined.peak_margin, refined.alternatives = old.peak_margin, old.alternatives
                    results[ri, fi] = refined
                    if basis[ri, fi] == "motion":
                        bonus = max(0.0, matrix[ri, fi] - old.score)
                        matrix[ri, fi] = min(1.0, refined.score + bonus)
            report(
                "refine",
                f"Refined {index + 1}/{len(likely)} alignments",
                86 + (index + 1) / max(1, len(likely)) * 5,
            )
    selected = set(assign_segments(matrix, {key: value.offset_seconds for key, value in results.items()},
                   [c["info"]["duration"] for c in radio], [c["info"]["duration"] for c in fpv], minimum_score))
    rows = []
    for ri, rc in enumerate(radio):
        for fi, fc in enumerate(fpv):
            ra, fa = pair_audio(ri, fi)
            result, aud = results[ri, fi], audio_results[ri, fi]
            offset = result.offset_seconds
            sections = (section_support(rdata[ri], fdata[fi], match_fps, offset, result.details)
                        if pair_motion(ri, fi) else {"correlated_sections": 0, "usable_sections": 0,
                        "section_seconds": 3.0, "section_agreement": 0.0, "section_spread": 0.0})
            aud["score_at_offset"], aud["overlap"] = score_at_offset(*pair_audio(ri, fi), offset)
            fingerprint_strength, fingerprint_offset, _, fingerprint_hashes, fingerprint_span = (
                landmark_match(*pair_audio(ri, fi))
            )
            # Use actual probed durations, not a correlation's valid sample count.
            overlap = max(
                0.0,
                min(rc["info"]["duration"] - max(0.0, offset), fc["info"]["duration"] - max(0.0, -offset)),
            )
            margin = candidate_margin(matrix, ri, fi, multiple_fpv=sum(r == ri for r, _ in selected) > 1)
            strong_motion = (
                matrix[ri, fi] >= 0.65
                and margin >= 0.08
                and result.peak_margin >= 0.035
                and result.reliable_axes >= 2
                and result.coverage >= 0.4
                and result.valid_seconds >= 15
                and sections["correlated_sections"] >= 5
                and sections["section_agreement"] >= .6
                and sections["section_spread"] >= 2 / 3
                and basis[ri, fi] in {"motion", "motion + boundary audio"}
            )
            audio_check = audio_checks.get((ri, fi))
            strong_audio = (ri, fi) in audio_auto_pairs and margin >= .08
            reasons = []
            if not rc["info"]["source_usable"]:
                reasons.append("StickCam source unavailable: " + rc["info"].get("source_error", "Unreadable source"))
            if not fc["info"]["source_usable"]:
                reasons.append("FPV source unavailable: " + fc["info"].get("source_error", "Unreadable source"))
            if rc["info"].get("needs_calibration") and not strong_audio:
                reasons.append("Confirm StickCam calibration to enable motion fallback")
            if margin < .08:
                reasons.append("Similar evidence exists for another candidate")
            if overlap < min_overlap:
                reasons.append("The proposed common interval is shorter than the minimum overlap")
            if not strong_motion and not strong_audio:
                if audio_check:
                    reasons.extend(audio_check["reasons"])
                if pair_motion(ri, fi) and result.correlated_sections < 5:
                    reasons.append("Motion lacks sustained agreement across independent active sections")
                if not pair_motion(ri, fi) and not audio_enabled and (ri, fi) not in activity_results:
                    reasons.append("No selected motion evidence is available for this pair")
            row = {
                "radio_video": rc["name"],
                "fpv_video": fc["name"],
                "score": result.score,
                "combined_score": float(matrix[ri, fi]),
                "confidence": "strong" if strong_motion or strong_audio else "review",
                "evidence": ("Tip-free gimbal movement · review suggestion" if not pair_motion(ri, fi) and not rc["info"]["audio_used"] and (ri, fi) in activity_results else basis[ri, fi]) + (f" · {fc['info']['motion_source']} rotation" if fc["info"].get("motion_source") != "visual" and "motion" in basis[ri, fi] else ""),
                "fpv_motion_source": fc["info"].get("motion_source", "visual"),
                "motion_included": pair_motion(ri, fi),
                "audio_included": rc["info"]["audio_used"] and fc["info"]["audio_used"],
                "camera_uptilt_deg": camera_uptilt_deg,
                "imu_orientation": imu_orientation,
                "gyro_video_offset": gyro_video_offset,
                "candidate_margin": margin,
                "peak_margin": result.peak_margin,
                "tracking_coverage": result.coverage,
                "valid_motion_seconds": result.valid_seconds,
                "reliable_axes": result.reliable_axes,
                "event_score": result.event_score,
                "rhythm": rhythm_results.get((ri, fi)),
                "gimbal_activity": activity_results.get((ri, fi)),
                **sections,
                "offset_seconds": offset,
                "radio_common_start_seconds": max(0.0, offset),
                "fpv_common_start_seconds": max(0.0, -offset),
                "overlap_seconds": overlap,
                "global_assignment": (ri, fi) in selected and overlap >= min_overlap,
                "radio_filename_time": format_timestamp(rc["clock"]),
                "fpv_filename_time": format_timestamp(fc["clock"]),
                "radio_modified_time": rc["info"].get("modified_time"),
                "fpv_modified_time": fc["info"].get("modified_time"),
                "modified_time_start_delta_seconds": (fc["info"].get("modified_start_clock") - rc["info"].get("modified_start_clock")
                    if rc["info"].get("modified_start_clock") is not None and fc["info"].get("modified_start_clock") is not None else None),
                "filename_start_delta_seconds": None
                if rc["clock"] is None or fc["clock"] is None
                else fc["clock"] - rc["clock"],
                "inferred_camera_clock_delta_seconds": clock_delta,
                "clock_consensus_support": clock_support,
                "timestamp_agreement": agreements.get((ri, fi)),
                "audio_score_at_motion_offset": aud["score_at_offset"],
                "audio_best_score": aud["best_score"],
                "audio_best_offset_seconds": aud["best_offset"],
                "audio_overlap_seconds": aud["overlap"],
                "audio_fingerprint_strength": fingerprint_strength,
                "audio_fingerprint_offset_seconds": fingerprint_offset,
                "audio_fingerprint_shared_hashes": fingerprint_hashes,
                "audio_fingerprint_span_seconds": fingerprint_span,
                "matching_strategy": matching_strategy,
                "audio_first_proposal": bool(strong_audio),
                "audio_boundary_seconds": audio_check.get("boundary_seconds", audio_boundary) if audio_check else audio_boundary,
                "motion_deferred": fc["info"]["motion_source"] == "deferred",
                "audio_trend_score": audio_check["trend_score"] if audio_check else None,
                "audio_trend_peak_margin": audio_check["trend_peak_margin"] if audio_check else None,
                "audio_correlated_sections": audio_check["sections"]["correlated_sections"] if audio_check else None,
                "audio_usable_sections": audio_check["sections"]["usable_sections"] if audio_check else None,
                "audio_joint_sections": audio_check["sections"]["joint_sections"] if audio_check else None,
                "audio_owner_margin": audio_check.get("owner_margin") if audio_check else None,
                "review_reasons": list(dict.fromkeys(reasons)),
                "alternatives": result.alternatives,
                "axis_details": result.details,
            }
            rows.append(row)
    deferred_clip_infos = {"fpv": [], "radio": []}
    timing_suggestions = []
    timing_suggestions_truncated = 0
    if deferred_fpv_paths or deferred_radio_paths:
        sampled = {"fpv": {clip["name"]: clip for clip in fpv},
                   "radio": {clip["name"]: clip for clip in radio}}
        deferred_names = {"fpv": {path.name for path in deferred_fpv_paths},
                          "radio": {path.name for path in deferred_radio_paths}}
        full_paths = {"fpv": full_fpv_paths, "radio": full_radio_paths}
        clock_records = {"fpv": {}, "radio": {}}
        for kind in ("fpv", "radio"):
            for path in full_paths[kind]:
                clip = sampled[kind].get(path.name)
                if clip:
                    info = clip["info"]
                else:
                    try:
                        signature = [path.stat().st_size, path.stat().st_mtime_ns]
                        metadata = probe_video(path)
                        modified = config.get("modified_times", {}).get(kind, {}).get(path.name, {})
                        modified_timestamp = modified.get("timestamp")
                        if not isinstance(modified_timestamp, (int, float)):
                            modified_timestamp = path.stat().st_mtime
                        info = {**metadata, "mtime_ns": signature[1], "modified_time": modified_timestamp,
                                "modified_time_source": modified.get("source", "server file date"),
                                "modified_start_clock": modified_timestamp - (metadata["duration"] if config.get("clock_settings", {}).get("modified_kind", "end") == "end" else 0)}
                        deferred_clip_infos[kind].append({"name": path.name, **info, "source_usable": True,
                            "motion_used": False, "audio_used": False, "motion_source": "not_scanned",
                            "motion_label": "Not scanned in this sample", "tracking_quality": 0.})
                    except Exception as exc:
                        errors.append({"kind": kind, "name": path.name, "phase": "scope metadata", "error": str(exc)})
                        continue
                timestamp = (filename_timestamp(path.name) if clock_source == "filename"
                             else info.get("modified_start_clock"))
                clock_records[kind][path.name] = {"timestamp": timestamp, "duration": info.get("duration", 0.),
                    "modified_time": info.get("modified_time"), "modified_start_clock": info.get("modified_start_clock")}
        if clock_delta is not None and clock_source:
            existing = {(row["radio_video"], row["fpv_video"]) for row in rows}
            for radio_name, radio_clock in clock_records["radio"].items():
                for fpv_name, fpv_clock in clock_records["fpv"].items():
                    if radio_name not in deferred_names["radio"] and fpv_name not in deferred_names["fpv"]:
                        continue
                    if (radio_name, fpv_name) in existing or radio_clock["timestamp"] is None or fpv_clock["timestamp"] is None:
                        continue
                    offset = fpv_clock["timestamp"] - radio_clock["timestamp"] - clock_delta
                    overlap = max(0., min(radio_clock["duration"] - max(0., offset),
                                          fpv_clock["duration"] - max(0., -offset)))
                    if overlap >= min_overlap:
                        timing_suggestions.append({"radio_video": radio_name, "fpv_video": fpv_name,
                            "offset_seconds": offset, "overlap_seconds": overlap, "clock_source": clock_source,
                            "radio_filename_time": format_timestamp(filename_timestamp(radio_name)),
                            "fpv_filename_time": format_timestamp(filename_timestamp(fpv_name)),
                            "radio_modified_time": radio_clock["modified_time"], "fpv_modified_time": fpv_clock["modified_time"],
                            "modified_time_start_delta_seconds": (fpv_clock["modified_start_clock"] - radio_clock["modified_start_clock"]
                                if fpv_clock["modified_start_clock"] is not None and radio_clock["modified_start_clock"] is not None else None),
                            "clock_support": clock_support, "clock_confidence": clock_confidence,
                            "tentative": clock_support < 2})
            timing_suggestions.sort(key=lambda suggestion: (-suggestion["overlap_seconds"],
                suggestion["radio_video"].casefold(), suggestion["fpv_video"].casefold()))
            timing_suggestions_truncated = max(0, len(timing_suggestions) - 250)
            timing_suggestions = timing_suggestions[:250]
            if rows:
                template = rows[0]
                for candidate in timing_suggestions:
                    offset = candidate["offset_seconds"]
                    row = {**template, **candidate, "score": 0., "combined_score": 0.,
                        "confidence": "review", "evidence": f"{clock_source} + duration · content not scanned",
                        "fpv_motion_source": "not_scanned", "motion_included": False, "audio_included": False,
                        "candidate_margin": 0., "peak_margin": 0., "tracking_coverage": 0.,
                        "valid_motion_seconds": 0., "reliable_axes": 0, "event_score": 0.,
                        "correlated_sections": 0, "usable_sections": 0, "section_agreement": 0.,
                        "section_spread": 0., "offset_seconds": offset,
                        "radio_common_start_seconds": max(0., offset), "fpv_common_start_seconds": max(0., -offset),
                        "overlap_seconds": candidate["overlap_seconds"], "global_assignment": False,
                        "audio_score_at_motion_offset": 0., "audio_best_score": 0., "audio_best_offset_seconds": None,
                        "audio_overlap_seconds": 0., "audio_fingerprint_strength": 0.,
                        "audio_fingerprint_offset_seconds": None, "audio_fingerprint_shared_hashes": 0,
                        "audio_fingerprint_span_seconds": 0., "audio_first_proposal": False,
                        "motion_deferred": True, "timing_only": True, "reviewed": False, "rhythm": None,
                        "review_reasons": ["This pair was not content-scanned; preview it or run Quick Match on all files."],
                        "alternatives": [], "axis_details": {}}
                    rows.append(row)
    assigned_radio = {row["radio_video"] for row in rows if row["global_assignment"]}
    assigned_fpv = {row["fpv_video"] for row in rows if row["global_assignment"]}
    summary = {
        "version": 5,
        "pairing_model": "multiple_fpv_segments_per_stickcam",
        "rate_profile": rate_profile,
        "rows": rows,
        "errors": errors,
        "excluded_radio": excluded_radio,
        "unmatched_radio": [p.name for p in radio_paths if p.name not in assigned_radio],
        "unmatched_fpv": [p.name for p in fpv_paths if p.name not in assigned_fpv],
        "considered_radio": considered_radio,
        "considered_fpv": considered_fpv,
        "match_scope": {"kind": match_target_kind, "target": match_target_name if match_target_kind != "all" else None,
                        "targets": [p.name for p in (full_fpv_paths if match_target_kind == "fpv" else full_radio_paths)] if match_target_kind != "all" else [],
                        "sample_percent": match_sample_percent,
                        "total_fpv": len(full_fpv_paths), "total_radio": len(full_radio_paths),
                        "sampled_fpv": considered_fpv, "sampled_radio": considered_radio,
                        "deferred_fpv": [p.name for p in deferred_fpv_paths],
                        "deferred_radio": [p.name for p in deferred_radio_paths],
                        "timing_suggestions": timing_suggestions,
                        "timing_suggestions_truncated": timing_suggestions_truncated},
        "clips": {
            kind: ([{"name": c["name"], **c["info"], "tracking_quality": c["tracking_quality"]} for c in data]
                   + deferred_clip_infos[kind])
            for kind, data in clips.items()
        },
        "clock": {"delta_seconds": clock_delta, "confidence": clock_confidence, "support": clock_support,
                  "source": clock_source},
        "workflow": {
            "strategy": matching_strategy, "audio_screening": audio_screening,
            "movement_rhythm": {"enabled": bool(rhythm_enabled), "proposals": sum(c["suggested"] for c in rhythm_results.values()), "automatic_confirmation": False},
            "gimbal_activity": {"enabled": bool(use_activity), "proposals": sum(c["suggested"] for c in activity_results.values()),
                                "sample_fps": 10, "automatic_confirmation": False},
            "sample_percent": match_sample_percent,
            "unscanned_fpv": [p.name for p in deferred_fpv_paths],
            "unscanned_radio": [p.name for p in deferred_radio_paths],
            "timing_suggestion_count": len(timing_suggestions),
            "timing_suggestions_tentative": clock_support < 2 if timing_suggestions else False,
            "unresolved_fpv": [p.name for p in fpv_paths if p.name not in assigned_fpv],
            "unresolved_radio": [p.name for p in radio_paths if p.name not in assigned_radio],
            "needs_calibration": [c["name"] for c in radio if c["info"].get("needs_calibration")],
            "skipped_motion": [{"kind": kind, "name": c["name"], "reason": c["info"]["motion_skipped_reason"]}
                for kind, items in clips.items() for c in items if c["info"].get("motion_skipped_reason")],
            "unavailable_sources": [{"kind": kind, "name": c["name"], "reason": c["info"]["source_error"]}
                for kind, items in clips.items() for c in items if c["info"].get("source_error")],
            "motion_loaded": {kind: sum(c["info"]["motion_used"] for c in items) for kind, items in clips.items()},
            "gimbal_activity_errors": [{"name": c["name"], "reason": c["info"]["gimbal_activity_error"]}
                                      for c in radio if c["info"].get("gimbal_activity_error")],
            "automatic_confirmation": False,
        },
        "settings": {
            "sample_fps": sample_fps,
            "match_sample_percent": match_sample_percent,
            "fpv_sampling_mode": fpv_sampling_mode,
            "fpv_visual_fps": fpv_visual_fps,
            "stick_sampling_ratio": stick_sampling_ratio,
            "match_fps": match_fps,
            "min_overlap": min_overlap,
            "minimum_score": minimum_score,
            "workers": workers,
            "audio_enabled": audio_enabled,
            "evidence_sources": evidence_sources,
            "motion_source": motion_source,
            "camera_uptilt_deg": camera_uptilt_deg,
            "imu_orientation": imu_orientation,
            "gyro_video_offset": gyro_video_offset,
            "allow_unconfigured_radio": allow_unconfigured_radio,
            "matching_strategy": matching_strategy,
        },
        "elapsed_seconds": round(time.monotonic() - start_time, 1),
    }
    atomic_json(output / "results.json", summary)
    temporary = output / "matches.csv.tmp"
    scalar_keys = list(dict.fromkeys(key for row in rows for key in row if key not in {"alternatives", "axis_details"}))
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=scalar_keys)
        writer.writeheader()
        writer.writerows({key: row[key] for key in scalar_keys} for row in rows)
    os.replace(temporary, output / "matches.csv")
    if make_plots and selected:
        from fpv_matcher.diagnostics.plots import plot_alignment

        plotted = sorted(pair for pair in selected if pair_motion(*pair))
        for index, (ri, fi) in enumerate(plotted):
            plot_alignment(
                radio[ri]["motion"],
                fpv[fi]["motion"],
                results[ri, fi],
                output / "plots" / f"pair_{index + 1:02d}.png",
                f"{radio[ri]['name']} ↔ {fpv[fi]['name']}",
                radio[ri]["audio"],
                fpv[fi]["audio"],
            )
            report(
                "diagnostics",
                f"Saved diagnostics {index + 1}/{len(selected)}",
                91 + (index + 1) / len(selected) * 8,
            )
    report(
        "complete",
        f"{len(assigned_fpv)} proposed FPV pairs across {len(assigned_radio)} StickCam recordings; {len(summary['unmatched_radio'])} StickCam and {len(summary['unmatched_fpv'])} FPV unmatched; {len(errors)} skipped files",
        100,
    )
    return summary
