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
from fpv_matcher.audio.fingerprint import best_offset, extract_audio, landmark_match, score_at_offset
from fpv_matcher.cache.fingerprints import get_or_extract
from fpv_matcher.config import calibration_for
from fpv_matcher.matching.assignment import assign, assign_segments, candidate_margin
from fpv_matcher.matching.evidence import normalize_sources, motion_enabled, audio_enabled_pair
from fpv_matcher.matching.correlation import MatchResult, compare, prepare, section_support
from fpv_matcher.motion.derivation import cached_visual_motion
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
        distinctive = result.reliable_axes >= 2 and result.peak_margin >= 0.025
        distinctive |= aud["best_score"] >= 0.82 and aud["peak_margin"] >= 0.08
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
    evidence_sources: dict | None = None,
    stick_sampling_ratio: float | None = None,
) -> dict:
    from fpv_matcher.radio.sampling import tracking_fps, validate_ratio
    stick_sampling_ratio = validate_ratio(stick_sampling_ratio)
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
    if use_motion and allow_unconfigured_radio and not audio_enabled:
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

    report("extract", f"Preparing {len(fpv_paths) + len(radio_paths)} recordings", 0)
    cv2.setNumThreads(1)
    errors, clips = [], {"fpv": [], "radio": []}
    identities = config.get("identities", {})

    def extraction_progress(kind, path, event, phase="motion"):
        with progress_lock:
            key = (kind, path.name)
            previous = extraction.get(key, {})
            extraction[key] = {**previous, **event, "kind": kind, "name": path.name, "phase": phase}
            fraction = sum(min(1, v.get("fraction", v.get("percent", 0) / 100 * .9)) for v in extraction.values()) / total
            active = [v for v in extraction.values() if not v.get("finished")]
            eta = event.get("eta_seconds")
            label = "Boundary audio" if phase == "audio" else ("Preparing recording" if not use_motion else "Stick detection" if kind == "radio" else "FPV motion")
            text = f"{label}: {path.name} · {event.get('percent', 0):.1f}%"
            if eta is not None and phase == "motion" and use_motion:
                text += f" · about {int(eta // 60)}m {int(eta % 60)}s remaining for motion extraction"
            report("extract", text, fraction * 65, active_clips=active, completed=completed, total=total)

    def empty_motion(duration):
        data = {k: np.zeros(2) for k in ("roll", "pitch", "yaw", "throttle", "rotation", "dx", "dy", "quality")}
        data["time"] = np.array([0.0, max(0.0, duration - 1 / sample_fps)])
        return data

    def process(kind, path):
        extraction_progress(kind, path, {"percent": 0, "eta_seconds": None})
        signature = [path.stat().st_size, path.stat().st_mtime_ns]
        info = probe_video(path)
        info["mtime_ns"] = signature[1]
        identity = identities.get(kind, {}).get(path.name)
        info["motion_used"] = False
        if not use_motion:
            motion = empty_motion(info["duration"])
            info["motion_source"] = "disabled"
            info["motion_label"] = "Motion matching disabled"
        elif kind == "fpv":
            motion = None
            telemetry_note = None
            if motion_source != "visual":
                try:
                    telemetry = cached_telemetry(path, cache, identity)
                    motion = body_rates(telemetry, camera_uptilt_deg, imu_orientation, gyro_video_offset)
                    info["motion_source"] = str(telemetry["kind"].item())
                    info["motion_label"] = str(telemetry["label"].item())
                except (ValueError, RuntimeError, ImportError) as exc:
                    if motion_source == "gyro" and not audio_enabled:
                        raise ValueError(f"Embedded FPV motion unavailable: {exc}") from exc
                    telemetry_note = str(exc)
            if motion is None and evidence_sources["fpv_visual"]:
                motion = cached_visual_motion(path, cache, sample_fps, identity,
                    progress=lambda event: extraction_progress(kind, path, event))
                info["motion_source"] = "visual"
                info["motion_label"] = "2D image motion"
                if telemetry_note:
                    info["telemetry_note"] = telemetry_note
            if motion is None:
                motion = empty_motion(info["duration"])
                info["motion_source"] = "unavailable"
                info["motion_label"] = "Selected embedded motion unavailable; audio only"
                info["telemetry_note"] = telemetry_note
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
            if clip_config is None and allow_unconfigured_radio:
                clip_config = {"skip_motion": True}
            if clip_config and clip_config.get("skip_motion"):
                if not audio_enabled:
                    raise ValueError("Unconfigured or audio-only StickCam requires both audio sources")
                motion = empty_motion(info["duration"])
                info["motion_source"] = "disabled"
            else:
                calibration = calibration_for(config, path.name)
                from fpv_matcher.radio.derivation import cached_sticks
                radio_fps = tracking_fps(sample_fps, stick_sampling_ratio, info.get("fps"))
                info["stick_sample_fps"] = radio_fps
                motion = cached_sticks(path, cache, calibration, radio_fps, identity,
                                       lambda event: extraction_progress(kind, path, event))
                info["motion_used"] = True
                info["motion_source"] = "stick_inputs"
        extraction_progress(kind, path, {"percent": 100, "fraction": .9, "eta_seconds": 0}, phase="audio" if audio_enabled else "finishing")
        from fpv_matcher.audio.fingerprint import _empty

        audio = (
            get_or_extract(
                str(path),
                cache,
                "audio",
                AUDIO_VERSION,
                {"boundary_seconds": 30},
                lambda: extract_audio(str(path)),
                identity,
            )
            if audio_enabled
            else _empty()
        )
        info["audio_used"] = audio_enabled and bool(len(audio["time"]))
        if signature != [path.stat().st_size, path.stat().st_mtime_ns]:
            raise ValueError(
                "Recording changed during extraction; wait until it finishes copying, then rerun"
            )
        quality = float(np.mean(motion["quality"])) if len(motion["quality"]) else 0.0
        return {
            "path": path,
            "name": path.name,
            "info": info,
            "motion": motion,
            "audio": audio,
            "tracking_quality": quality,
            "clock": filename_timestamp(path.name) if evidence_sources["filename_timestamps"] else None,
        }

    total, completed = len(fpv_paths) + len(radio_paths), 0
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
                errors.append({"kind": kind, "name": path.name, "error": str(exc)})
                message = f"Skipped {path.name}: {exc}"
            with progress_lock:
                completed += 1
                extraction[(kind, path.name)] = {"fraction": 1, "finished": True}
                fraction = sum(min(1, v.get("fraction", v.get("percent", 0) / 100 * .9)) for v in extraction.values()) / total
                report("extract", message, fraction * 65, completed=completed, total=total, errors=len(errors),
                       active_clips=[v for v in extraction.values() if not v.get("finished")])
    for kind in clips:
        clips[kind].sort(key=lambda clip: clip["name"])
    radio, fpv = clips["radio"], clips["fpv"]
    if not radio or not fpv:
        atomic_json(output / "errors.json", {"errors": errors})
        raise ValueError(
            "No usable recordings of both types remain. " + "; ".join(e["error"] for e in errors[:3])
        )
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
            best_score, aud_offset, best_overlap, aud_margin = best_offset(rc["audio"], fc["audio"])
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
                        matrix[ri, fi] = max(
                            matrix[ri, fi],
                            0.5 + 0.1 * clock_confidence + 0.12 * candidate.score + 0.03 * audio_score,
                        )
                        basis[ri, fi] = "camera clock + motion · review"
                error = results[ri, fi].offset_seconds - expected
                agreement = float(np.exp(-0.5 * (error / 10) ** 2))
                agreements[ri, fi] = agreement
                if matrix[ri, fi] >= 0.3:
                    matrix[ri, fi] += 0.08 * clock_confidence * agreement * (1 - matrix[ri, fi])
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
            strong = (
                matrix[ri, fi] >= 0.65
                and margin >= 0.08
                and result.peak_margin >= 0.035
                and result.reliable_axes >= 2
                and result.coverage >= 0.4
                and result.valid_seconds >= 15
                and sections["correlated_sections"] >= 5
                and sections["section_agreement"] >= .6
                and sections["section_spread"] >= 2 / 3
                and basis[ri, fi] == "motion"
            )
            row = {
                "radio_video": rc["name"],
                "fpv_video": fc["name"],
                "score": result.score,
                "combined_score": float(matrix[ri, fi]),
                "confidence": "strong" if strong else "review",
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
                "alternatives": result.alternatives,
                "axis_details": result.details,
            }
            rows.append(row)
    assigned_radio = {row["radio_video"] for row in rows if row["global_assignment"]}
    assigned_fpv = {row["fpv_video"] for row in rows if row["global_assignment"]}
    summary = {
        "version": 3,
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
        "settings": {
            "sample_fps": sample_fps,
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
