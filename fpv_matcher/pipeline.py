"""Batch engine shared by the CLI and web worker; no web/UI dependencies."""

from __future__ import annotations

import csv
import json
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
from fpv_matcher.matching.evidence import normalize_sources, motion_enabled, audio_enabled_pair
from fpv_matcher.matching.correlation import MatchResult, compare, prepare, section_support
from fpv_matcher.motion.derivation import cached_visual_motion, cache_status as visual_cache_status, embedded_status
from fpv_matcher.motion.sampling import visual_fps
from fpv_matcher.motion.telemetry import cached_telemetry, body_rates, validate_motion_settings
from fpv_matcher.radio.rates import apply_rate_profile, validate_rate_profile
from fpv_matcher.video.metadata import filename_timestamp, format_timestamp, probe_video


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)


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
    if not fpv_paths or not radio_paths:
        raise ValueError("Provide at least one recording of each type")
    requested_fpv, requested_radio = list(fpv_paths), list(radio_paths)
    if match_target_kind not in {"all", "fpv", "radio"}:
        raise ValueError("Match target must be all, fpv, or radio")
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
    considered_fpv = [path.name for path in fpv_paths]
    considered_radio = [path.name for path in radio_paths]
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
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
    audio_enabled = audio_enabled_pair(evidence_sources)
    motion_source = ("auto" if evidence_sources["fpv_gyro"] and evidence_sources["fpv_visual"]
                     else "gyro" if evidence_sources["fpv_gyro"] else "visual")
    excluded_radio = [p.name for p in radio_paths if config.get("clips", {}).get(p.name, {}).get("exclude")]
    radio_paths = [p for p in radio_paths if p.name not in excluded_radio]
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
    extraction_base, extraction_span = 0., 25.
    total, completed = len(fpv_paths) + len(radio_paths), 0

    def extraction_progress(kind, path, event, phase="motion"):
        with progress_lock:
            key = (kind, path.name)
            previous = extraction.get(key, {})
            extraction[key] = {**previous, **event, "kind": kind, "name": path.name, "phase": phase}
            fraction = sum(min(1, v.get("fraction", v.get("percent", 0) / 100)) for v in extraction.values()) / max(1, total)
            active = [v for v in extraction.values() if not v.get("finished")]
            eta = event.get("eta_seconds")
            label = "Boundary audio" if phase == "audio" else ("Preparing recording" if not use_motion else "Stick detection" if kind == "radio" else "FPV motion")
            text = f"{label}: {path.name} · {event.get('percent', 0):.1f}%"
            if eta is not None and phase == "motion" and use_motion:
                text += f" · about {int(eta // 60)}m {int(eta % 60)}s remaining for motion extraction"
            report("extract", text, extraction_base + fraction * extraction_span, active_clips=active, completed=completed, total=total)

    def empty_motion(duration):
        data = {k: np.zeros(2) for k in ("roll", "pitch", "yaw", "throttle", "rotation", "dx", "dy", "quality")}
        data["time"] = np.array([0.0, max(0.0, duration - 1 / sample_fps)])
        return data

    def process(kind, path):
        """Read cheap evidence first; vision failures must not discard audio."""
        extraction_progress(kind, path, {"percent": 0, "eta_seconds": None}, phase="audio" if audio_enabled else "metadata")
        signature = [path.stat().st_size, path.stat().st_mtime_ns]
        info = probe_video(path)
        info["mtime_ns"] = signature[1]
        identity = identities.get(kind, {}).get(path.name)
        info["motion_used"] = False
        info["source_usable"] = True
        info["motion_source"] = "deferred" if use_motion else "disabled"
        info["motion_label"] = "Motion awaiting evidence screening" if use_motion else "Motion matching disabled"
        audio = _empty()
        if audio_enabled:
            try:
                audio = get_or_extract(str(path), cache, "audio", AUDIO_VERSION,
                    {"boundary_seconds": 30}, lambda: extract_audio(str(path)), identity, validate=validate_audio)
            except (ValueError, RuntimeError, OSError, ImportError) as exc:
                info["audio_error"] = str(exc)
        info["audio_used"] = audio_enabled and bool(len(audio["time"]))
        if signature != [path.stat().st_size, path.stat().st_mtime_ns]:
            raise ValueError("Recording changed while reading it; wait until copying finishes, then rerun")
        return {"path": path, "name": path.name, "info": info, "signature": signature,
            "motion": empty_motion(info["duration"]), "audio": audio, "tracking_quality": 0.,
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
                from fpv_matcher.radio.derivation import cached_sticks, cache_status as stick_cache_status, observation_status
                radio_fps = tracking_fps(sample_fps, stick_sampling_ratio, info.get("fps"))
                info["stick_sample_fps"] = radio_fps
                info["motion_reused"] = stick_cache_status(path, cache, calibration, radio_fps, identity)["status"] == "cached"
                motion = cached_sticks(path, cache, calibration, radio_fps, identity,
                                       lambda event: extraction_progress(kind, path, event))
                info["motion_used"] = True
                info["motion_source"] = "stick_inputs"
                info["stick_observations"] = observation_status(motion)
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
    for kind, items in clips.items():
        errors.extend({"kind": kind, "name": c["name"], "phase": "audio", "error": c["info"]["audio_error"]}
                      for c in items if c["info"].get("audio_error"))

    audio_checks, audio_auto_pairs = {}, set()
    audio_screening = {"enabled": matching_strategy == "automatic" and audio_enabled,
                       "policy": AUDIO_POLICY_VERSION, "eligible_pairs": 0, "distinctive_pairs": 0}
    if audio_screening["enabled"]:
        count = len(radio) * len(fpv)
        report("audio_screening", "Checking independent audio fingerprints and trends before deriving motion", 25, completed=0, total=count)
        eligible, owners = np.zeros((len(radio), len(fpv))), np.zeros((len(radio), len(fpv)))
        for ri, rc in enumerate(radio):
            for fi, fc in enumerate(fpv):
                check = assess_audio_pair(rc["audio"], fc["audio"], min_overlap)
                audio_checks[ri, fi] = check
                owners[ri, fi] = .6 * check["landmark_strength"] + .4 * check["trend_score"]
                common = min(rc["info"]["duration"] - max(0., check["offset"]),
                             fc["info"]["duration"] - max(0., -check["offset"]))
                if check["eligible"] and common >= min_overlap:
                    eligible[ri, fi] = check["score"]
                done = ri * len(fpv) + fi + 1
                if done % max(1, count // 50) == 0 or done == count:
                    report("audio_screening", f"Checked audio for {done}/{count} candidates", 25 + 10 * done / count,
                           completed=done, total=count)
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
                calibration_for(config, clip["name"])
                calibrated_radio.add(ri)
            except (ValueError, KeyError, TypeError, AttributeError):
                clip["info"]["needs_calibration"] = True
                clip["info"]["motion_source"] = "disabled"
                clip["info"]["motion_label"] = "StickCam calibration needed for motion fallback"
                clip["info"]["motion_skipped_reason"] = "StickCam needs a confirmed calibration before deriving inputs"
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
                clip["audio"], clip["motion"] = _empty(), empty_motion(0)
                clip["clock"] = None
                errors.append({"kind": kind, "name": clip["name"], "phase": "source", "error": reason})
    valid_pairs = {(ri, fi) for ri, rc in enumerate(radio) for fi, fc in enumerate(fpv)
                   if rc["info"]["source_usable"] and fc["info"]["source_usable"]}
    audio_auto_pairs &= valid_pairs
    audio_checks = {key: value for key, value in audio_checks.items() if key in valid_pairs}
    audio_screening["distinctive_pairs"] = len(audio_auto_pairs)
    rdata = [prepare(apply_rate_profile(c["motion"], rate_profile), match_fps, True) if c["info"]["motion_used"] else None for c in radio]
    fdata = [prepare(c["motion"], match_fps, False) if c["info"]["motion_used"] else None for c in fpv]

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
            result = correlate_pair(ri, fi)
            aud_score, aud_overlap = score_at_offset(rc["audio"], fc["audio"], result.offset_seconds)
            audio_check = audio_checks.get((ri, fi))
            if audio_check:
                best_score, aud_offset, best_overlap, aud_margin = (audio_check["best_score"], audio_check["best_offset"],
                    audio_check["best_overlap"], audio_check["peak_margin"])
            else:
                best_score, aud_offset, best_overlap, aud_margin = best_offset(rc["audio"], fc["audio"], min_overlap)
            aud_quality = min(float(rc["audio"]["quality"]), float(fc["audio"]["quality"]))
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
                    aud_score, aud_overlap = score_at_offset(rc["audio"], fc["audio"], aud_offset)
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
                aud_score, aud_overlap = score_at_offset(rc["audio"], fc["audio"], result.offset_seconds, min_overlap)
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
    clock_delta, clock_confidence, clock_support = infer_clock_difference(
        [c["clock"] for c in radio], [c["clock"] for c in fpv], results, matrix, audio_results,
        allow_single_anchor=match_target_kind != "all",
    )
    agreements = {}
    if clock_delta is not None:
        for ri, rc in enumerate(radio):
            for fi, fc in enumerate(fpv):
                if rc["clock"] is None or fc["clock"] is None:
                    continue
                expected = fc["clock"] - rc["clock"] - clock_delta
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
                    audio_score, overlap = score_at_offset(rc["audio"], fc["audio"], candidate.offset_seconds)
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
            result, aud = results[ri, fi], audio_results[ri, fi]
            offset = result.offset_seconds
            sections = (section_support(rdata[ri], fdata[fi], match_fps, offset, result.details)
                        if pair_motion(ri, fi) else {"correlated_sections": 0, "usable_sections": 0,
                        "section_seconds": 3.0, "section_agreement": 0.0, "section_spread": 0.0})
            aud["score_at_offset"], aud["overlap"] = score_at_offset(rc["audio"], fc["audio"], offset)
            fingerprint_strength, fingerprint_offset, _, fingerprint_hashes, fingerprint_span = (
                landmark_match(rc["audio"], fc["audio"])
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
                if not pair_motion(ri, fi) and not audio_enabled:
                    reasons.append("No selected motion evidence is available for this pair")
            row = {
                "radio_video": rc["name"],
                "fpv_video": fc["name"],
                "score": result.score,
                "combined_score": float(matrix[ri, fi]),
                "confidence": "strong" if strong_motion or strong_audio else "review",
                "evidence": basis[ri, fi] + (f" · {fc['info']['motion_source']} rotation" if fc["info"].get("motion_source") != "visual" and "motion" in basis[ri, fi] else ""),
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
                **sections,
                "offset_seconds": offset,
                "radio_common_start_seconds": max(0.0, offset),
                "fpv_common_start_seconds": max(0.0, -offset),
                "overlap_seconds": overlap,
                "global_assignment": (ri, fi) in selected and overlap >= min_overlap,
                "radio_filename_time": format_timestamp(rc["clock"]),
                "fpv_filename_time": format_timestamp(fc["clock"]),
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
    assigned_radio = {row["radio_video"] for row in rows if row["global_assignment"]}
    assigned_fpv = {row["fpv_video"] for row in rows if row["global_assignment"]}
    summary = {
        "version": 4,
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
                        "targets": [p.name for p in (fpv_paths if match_target_kind == "fpv" else radio_paths)] if match_target_kind != "all" else []},
        "clips": {
            kind: [{"name": c["name"], **c["info"], "tracking_quality": c["tracking_quality"]} for c in data]
            for kind, data in clips.items()
        },
        "clock": {"delta_seconds": clock_delta, "confidence": clock_confidence, "support": clock_support},
        "workflow": {
            "strategy": matching_strategy, "audio_screening": audio_screening,
            "unresolved_fpv": [p.name for p in fpv_paths if p.name not in assigned_fpv],
            "unresolved_radio": [p.name for p in radio_paths if p.name not in assigned_radio],
            "needs_calibration": [c["name"] for c in radio if c["info"].get("needs_calibration")],
            "skipped_motion": [{"kind": kind, "name": c["name"], "reason": c["info"]["motion_skipped_reason"]}
                for kind, items in clips.items() for c in items if c["info"].get("motion_skipped_reason")],
            "unavailable_sources": [{"kind": kind, "name": c["name"], "reason": c["info"]["source_error"]}
                for kind, items in clips.items() for c in items if c["info"].get("source_error")],
            "motion_loaded": {kind: sum(c["info"]["motion_used"] for c in items) for kind, items in clips.items()},
            "automatic_confirmation": False,
        },
        "settings": {
            "sample_fps": sample_fps,
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
    scalar_keys = [key for key in rows[0] if key not in {"alternatives", "axis_details"}]
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
