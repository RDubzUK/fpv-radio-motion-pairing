"""Reproducible movement-only benchmark; extraction, blind matching and scoring are separate.

Uses production fingerprints/thresholds and an isolated cold cache. Session files,
calibrations, confirmations and production caches are read-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fpv_matcher.matching import activity as matching
from fpv_matcher.motion.telemetry import VERSION as TELEMETRY_VERSION, body_rates, cached_telemetry
from fpv_matcher.radio import activity
from fpv_matcher.video.acceleration import capabilities
from fpv_matcher.video.reader import cuda_decoder_available


def write(path, value):
    def scalar(item):
        if isinstance(item, np.generic):
            return item.item()
        raise TypeError(f"Unsupported JSON value: {type(item).__name__}")
    path.write_text(json.dumps(value, indent=2, allow_nan=False, default=scalar) + "\n")


def read(path):
    return json.loads(path.read_text())


def code_hashes():
    files = ["radio/activity.py", "radio/outline.py", "matching/activity.py", "matching/signals.py",
             "matching/correlation.py", "motion/telemetry.py", "video/reader.py", "video/acceleration.py"]
    return {name: hashlib.sha256((ROOT / "fpv_matcher" / name).read_bytes()).hexdigest() for name in files}


def check_protocol(output):
    if read(output / "protocol.json")["code_sha256"] != code_hashes():
        raise ValueError("Production algorithm changed after freezing this benchmark")


def source(record, expected=None):
    path = Path(record["path"])
    stat = path.stat()
    signature = [stat.st_size, stat.st_mtime_ns]
    if expected and signature != expected:
        raise ValueError(f"Confirmed source changed: {path.name}")
    return {"path": str(path), "name": path.name, "signature": signature,
            "metadata": record["metadata"], "identity": record.get("content_id")}


def research_calibration(number, width):
    from study_gimbal_activity import REFERENCES
    from fpv_matcher.config import validate_calibration
    study = ROOT / "artifacts/pair-validation/gridworks-oct26"
    spec = REFERENCES[number]
    alternate = study / f"p{number}-gimbal-summary-alternate.json"
    baseline = study / f"p{number}-gimbal-summary.json"
    if alternate.exists() and read(alternate)["pose_coverage"] > read(baseline)["pose_coverage"]:
        spec = read(alternate)["setup"]
    specs = [spec]
    references = []
    for spec in specs:
        scale = width / 800
        calibration = {"reference_time": spec["time"], "stickcam_mode": "wonky",
                       "radio_outline": (np.asarray(spec["outline"]) * scale).tolist()}
        for side, center, size in zip(("left_gimbal", "right_gimbal"), spec["centres"], spec["widths"]):
            x, y = np.asarray(center) * scale
            size *= scale
            calibration[side] = {"x": x-size/2, "y": y-size/2, "width": size, "height": size,
                                 "center_x": size/2, "center_y": size/2}
        references.append(validate_calibration(calibration))
    result = references[0]
    if len(references) > 1:
        result["tracking_landmarks"] = [{"time": c["reference_time"], "calibration": c} for c in references[1:]]
    return result


def prepare(output):
    if (output / "protocol.json").exists():
        raise ValueError("Use a new output folder to freeze another benchmark")
    output.mkdir(parents=True, exist_ok=True)
    jobs = Path.home() / ".fpv-radio-motion-pairing/jobs"
    pairs = []
    for job_path in sorted(jobs.glob("*/job.json")):
        job = read(job_path)
        for pair in job.get("pair_overrides", {}).values():
            if pair.get("verification", "independent") != "independent" or not pair.get("source_signatures"):
                continue
            records = {}
            for kind, key in (("radio", "radio_video"), ("fpv", "fpv_video")):
                record = next(r for r in job["inputs"][kind] if r["name"] == pair[key])
                records[kind] = source(record, pair["source_signatures"][kind])
            pairs.append({"sources": records, "session": job["label"], "origin": "motion",
                          "reference_offset": pair["offset_seconds"], "timing_label": "source-bound independent confirmation"})
    sister_path = ROOT.parent / "fpv-audio-pairing/data/sessions"
    for session_path in sorted(sister_path.glob("*/session.json")):
        session = read(session_path)
        videos = {v["id"]: v for v in session["videos"]}
        for pair in session["pairs"]:
            if not pair.get("confirmed") or pair.get("stale") or pair.get("verification", "independent") != "independent":
                continue
            records = {kind: source(videos[pair[key]], pair["source_signatures"][sig])
                       for kind, key, sig in (("fpv", "fpv", "fpv"), ("radio", "stick", "stick"))}
            pairs.append({"sources": records, "session": session["name"], "origin": "audio",
                          "reference_offset": pair["offset"], "timing_label": "source-bound confirmed audio alignment"})
    # This earlier pair was explicitly supplied by the user as the same flight.
    # Its historical offset is not a current source-bound timing confirmation.
    for job_path in sorted(jobs.glob("*/job.json")):
        job = read(job_path)
        for name, pair in job.get("overrides", {}).items():
            if name != "DJI_20260406162534_0228_D.MP4" or pair.get("fpv_video") != "DJI_20260406161138_0008_D.MP4":
                continue
            records = {kind: source(next(r for r in job["inputs"][kind] if r["name"] == filename))
                       for kind, filename in (("radio", name), ("fpv", pair["fpv_video"]))}
            pairs.append({"sources": records, "session": job["label"], "origin": "legacy user-identified pair",
                          "reference_offset": pair["offset_seconds"], "timing_label": "legacy manual timing, unverified",
                          "calibration": job["calibrations"][name]})
    unique = {}
    for pair in pairs:
        key = tuple(pair["sources"][k]["path"] for k in ("radio", "fpv"))
        unique.setdefault(key, pair)
    pairs = list(unique.values())
    if not 5 <= len(pairs) <= 10:
        raise ValueError(f"Expected 5-10 unique known pairs; found {len(pairs)}")
    study_pairs = read(ROOT / "artifacts/pair-validation/gridworks-oct26/reference.json")["pairs"]
    for pair in pairs:
        prior = next((i for i, old in enumerate(study_pairs, 1)
                      if old["sources"]["radio"]["path"] == pair["sources"]["radio"]["path"]), None)
        pair["previously_studied"] = prior is not None
        if prior:
            pair["calibration"] = research_calibration(prior, pair["sources"]["radio"]["metadata"]["width"])
        # Freeze geometry-only setup before any benchmark comparisons.
        pair["activity_specification"] = activity.specification(pair["calibration"])
    rng = random.Random(20261013)
    ordered = {}
    for kind, prefix in (("radio", "R"), ("fpv", "F")):
        indices = list(range(len(pairs)))
        rng.shuffle(indices)
        ordered[kind] = indices
        for number, i in enumerate(indices, 1):
            pairs[i][kind+"_id"] = f"{prefix}{number:02}"
    extraction = {"radio": [], "fpv": []}
    truth = []
    for i, pair in enumerate(pairs, 1):
        for kind in extraction:
            item = {"id": pair[kind+"_id"], **pair["sources"][kind]}
            if kind == "radio":
                item["calibration"] = pair["calibration"]
                item["activity_specification"] = pair["activity_specification"]
            extraction[kind].append(item)
        truth.append({"case": i, "radio": pair["radio_id"], "fpv": pair["fpv_id"], "session": pair["session"],
                      "origin": pair["origin"], "reference_offset": pair["reference_offset"],
                      "timing_label": pair["timing_label"], "previously_studied": pair["previously_studied"],
                      "radio_name": pair["sources"]["radio"]["name"], "fpv_name": pair["sources"]["fpv"]["name"]})
    for items in extraction.values():
        items.sort(key=lambda r: r["id"])
    protocol = {"prepared_at": time.time(), "pairs": len(pairs), "seed": 20261013,
                "code_sha256": code_hashes(), "activity_version": activity.VERSION,
                "matching_version": matching.VERSION, "telemetry_version": TELEMETRY_VERSION,
                "radio_sample_hz": 10, "min_overlap_setting_seconds": 2, "minimum_observed_seconds": 8,
                "all_source_combinations": True, "all_feasible_lags": True,
                "audio": False, "filenames_as_evidence": False, "file_dates_as_evidence": False,
                "duration_similarity": False, "exact_stick_inputs": False, "saved_offsets_as_constraints": False,
                "owner_separation_threshold": .04, "correct_sync_tolerance_seconds": 1.,
                "no_threshold_tuning_after_results": True, "opencv_threads": 1,
                "limitations": ["Four clips helped develop the algorithm; one newly evaluated pair.",
                                "Radio geometry is pre-marked, not automatic calibration.",
                                "One appearance reference per clip, selected by prior pose coverage where available.",
                                "Fifth identity is user-supplied; its old timing is not source-bound.",
                                "Cold algorithm cache does not imply a cold operating-system/NAS cache."]}
    write(output / "extraction-input.json", extraction)
    write(output / "truth.json", truth)
    write(output / "protocol.json", protocol)
    print(json.dumps({"prepared": len(pairs), "sessions": sorted(set(p["session"] for p in pairs)),
                      "output": str(output)}), flush=True)


def load_npz(path):
    with np.load(path, allow_pickle=False) as saved:
        result = {key: saved[key] for key in saved.files}
    result["event_times"] = result["event_times"].tolist()
    return result


def extract(output):
    check_protocol(output)
    cv2.setNumThreads(1)
    sources = read(output / "extraction-input.json")
    cache, signals = output / "cold-cache", output / "signals"
    signals.mkdir(exist_ok=True)
    timings = read(output / "extraction-timings.json") if (output / "extraction-timings.json").exists() else []
    write(output / "environment.json", {"platform": platform.platform(), "python": sys.version,
          "opencv": cv2.__version__, "opencv_threads": cv2.getNumThreads(), "tracking": capabilities(),
          "cuda_decoder_available": cuda_decoder_available(), "decode_mode": os.environ.get("FPV_MATCHER_DECODE", "auto")})
    matcher_input = {"radio": [], "fpv": []}
    for kind, records in sources.items():
        for record in records:
            ident = record["id"]
            target = signals / f"{ident}.npz"
            if target.exists() and any(t["id"] == ident for t in timings):
                matcher_input[kind].append({"id": ident, "signal": str(target.resolve())})
                continue
            signature = lambda: [Path(record["path"]).stat().st_size, Path(record["path"]).stat().st_mtime_ns]
            if signature() != record["signature"]:
                raise ValueError(f"Source changed: {ident}")
            print(f"START {ident} {kind} {record['metadata']['duration']:.1f}s", flush=True)
            started, last = time.perf_counter(), [0.]
            def progress(event):
                now = time.perf_counter()
                if now-last[0] >= 20 or event.get("percent", 0) >= 100:
                    print(json.dumps({"id": ident, "percent": round(event.get("percent", 0), 1),
                          "elapsed_seconds": round(now-started, 2), "eta_seconds": event.get("eta_seconds")}), flush=True)
                    last[0] = now
            if kind == "radio":
                expected = activity.cache_path(record["path"], cache, record["calibration"], record["identity"])
                reused = expected.exists()
                raw = activity.cached_activity(record["path"], cache, record["calibration"], record["identity"], progress)
                raw_done = time.perf_counter()
                signal = matching.prepare_signal(raw, "activity")
                source_kind = "gimbal_activity"
                load_again = lambda: activity.cached_activity(record["path"], cache, record["calibration"], record["identity"])
            else:
                from fpv_matcher.cache.fingerprints import _key
                expected = cache / f"telemetry.{_key(record['path'], TELEMETRY_VERSION, {}, record['identity'])}.npz"
                reused = expected.exists()
                raw = cached_telemetry(record["path"], cache, record["identity"])
                raw_done = time.perf_counter()
                signal = matching.prepare_fpv(body_rates(raw))
                source_kind = str(raw["kind"].item())
                load_again = lambda: cached_telemetry(record["path"], cache, record["identity"])
            prepared = time.perf_counter()
            np.savez_compressed(target, time=signal["time"], energy=signal["energy"], quality=signal["quality"],
                                event_times=signal["event_times"], fps=np.asarray(signal["fps"]))
            warm_start = time.perf_counter()
            load_again()
            warm_seconds = time.perf_counter()-warm_start
            if signature() != record["signature"]:
                raise ValueError(f"Source changed during extraction: {ident}")
            item = {"id": ident, "kind": kind, "duration_seconds": record["metadata"]["duration"],
                    "source_kind": source_kind, "cold_extraction_seconds": raw_done-started,
                    "signal_preparation_seconds": prepared-raw_done, "cache_reused_on_first_read": reused,
                    "warm_cache_read_seconds": warm_seconds,
                    "observed_percent": float(np.mean(signal["quality"] > .1)*100),
                    "observed_seconds": float(np.sum(signal["quality"] > .1)/matching.FPS),
                    "samples": len(signal["time"])}
            timings.append(item)
            write(output / "extraction-timings.json", timings)
            matcher_input[kind].append({"id": ident, "signal": str(target.resolve())})
            print("DONE " + json.dumps(item), flush=True)
    # Matching input contains only anonymous IDs and motion arrays.
    write(output / "matcher-input.json", matcher_input)


def match(output):
    check_protocol(output)
    sources = read(output / "matcher-input.json")
    started = time.perf_counter()
    signals = {kind: {r["id"]: load_npz(Path(r["signal"])) for r in records} for kind, records in sources.items()}
    loaded = time.perf_counter()
    results = []
    for rid, radio in signals["radio"].items():
        for fid, fpv in signals["fpv"].items():
            row = matching.compare(radio, fpv, min_overlap=2.)
            results.append({"radio": rid, "fpv": fid, **row})
    # Same competing-owner rule as the production pipeline. No forced 1:1 assignment.
    for row in results:
        competing = [r["score"] for r in results if r["fpv"] == row["fpv"] and r["radio"] != row["radio"]]
        row["candidate_separation"] = row["score"]-max(competing, default=0.)
        row["suggested"] = bool(row["eligible"] and row["candidate_separation"] >= .04)
    finished = time.perf_counter()
    write(output / "blind-results.json", {"rows": results, "combinations": len(results),
          "signal_load_seconds": loaded-started, "matching_seconds": finished-loaded,
          "total_seconds": finished-started, "ground_truth_read": False})
    print(json.dumps({"combinations": len(results), "matching_seconds": finished-loaded,
                      "suggestions": sum(r["suggested"] for r in results)}), flush=True)


def score(output):
    check_protocol(output)
    truth, blind = read(output / "truth.json"), read(output / "blind-results.json")
    rows, cases = blind["rows"], []
    radio_sessions = {p["radio"]: p["session"] for p in truth}
    for pair in truth:
        ranking = sorted((r for r in rows if r["fpv"] == pair["fpv"]), key=lambda r: r["score"], reverse=True)
        reverse = sorted((r for r in rows if r["radio"] == pair["radio"]), key=lambda r: r["score"], reverse=True)
        correct = next(r for r in ranking if r["radio"] == pair["radio"])
        within_session = [r for r in ranking if radio_sessions[r["radio"]] == pair["session"]]
        cases.append({**pair, "top_radio": ranking[0]["radio"], "correct_rank": ranking.index(correct)+1,
                      "reverse_correct_rank": reverse.index(correct)+1,
                      "within_session_correct_rank": within_session.index(correct)+1,
                      "correct_pair_score": correct["score"], "top_score": ranking[0]["score"],
                      "runner_up_score": ranking[1]["score"], "estimated_offset": correct["offset_seconds"],
                      "signed_offset_error": correct["offset_seconds"]-pair["reference_offset"],
                      "suggested": correct["suggested"], "eligible": correct["eligible"],
                      "peak_margin": correct["peak_margin"], "owner_margin": correct["candidate_separation"],
                      "valid_seconds": correct["valid_seconds"], "sections": correct["sections"],
                      "top_offset": ranking[0]["offset_seconds"]})
    positives = {(p["radio"], p["fpv"]) for p in truth}
    suggestions = [r for r in rows if r["suggested"]]
    summary = {"cases": len(cases), "top1_correct": sum(c["correct_rank"] == 1 for c in cases),
               "within_session_top1_correct": sum(c["within_session_correct_rank"] == 1 for c in cases),
               "reverse_top1_correct": sum(c["reverse_correct_rank"] == 1 for c in cases),
               "suggestions": len(suggestions), "correct_suggestions": sum((r["radio"], r["fpv"]) in positives for r in suggestions),
               "incorrect_suggestions": sum((r["radio"], r["fpv"]) not in positives for r in suggestions),
               "source_bound_sync_within_1s": sum(abs(c["signed_offset_error"]) <= 1 for c in cases if c["previously_studied"]),
               "matching_seconds": blind["matching_seconds"]}
    write(output / "scored-results.json", {"summary": summary, "cases": cases})
    print(json.dumps(summary, indent=2), flush=True)
    for case in cases:
        print(json.dumps({k: v for k, v in case.items() if k != "sections"}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "extract", "match", "score"))
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    globals()[args.stage](args.output.resolve())
