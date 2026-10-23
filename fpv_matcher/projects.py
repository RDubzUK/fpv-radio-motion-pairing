"""Portable session metadata and bounded, source-bound fingerprint bundles."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import time
import uuid
import zipfile
from pathlib import Path

import numpy as np

from fpv_matcher import jobs
from fpv_matcher.cache.fingerprints import _key
from fpv_matcher.storage import atomic_json

FORMAT = "propnutz-flight-matcher-project"
LIMIT = 128 * 1024**2
FIELDS = ("label", "settings", "inputs", "calibrations", "calibration_signatures", "calibration_reviews",
          "pair_overrides", "overrides", "rejected_pairs", "review_decisions", "sync_points", "clock_settings", "no_counterpart", "references",
          "radio_profile", "radio_orientation_checks", "portrait_layouts", "clock_rejections")


def content_id(path):
    source = Path(path)
    before = source.stat()
    digest = hashlib.sha256(str(before.st_size).encode())
    with source.open("rb") as stream:
        digest.update(stream.read(128 * 1024))
        stream.seek(max(0, before.st_size - 128 * 1024))
        digest.update(stream.read(128 * 1024))
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Recording changed while checking its head/tail identity")
    return "head-tail-v1:" + digest.hexdigest()


def bindings(record, kind, job):
    from fpv_matcher.audio.fingerprint import ALGORITHM_VERSION
    result = [("audio", ALGORITHM_VERSION, {"boundary_seconds": rate}) for rate in (30, 60, 120, 300)]
    from fpv_matcher.audio.motor import VERSION as MOTOR_VERSION,config as motor_config
    result.append(("motor-audio",MOTOR_VERSION,motor_config(job["settings"].get("motor_band"))))
    if kind=="radio":
        from fpv_matcher.radio.scene_activity import VERSION as SCENE_VERSION,CONFIG
        result.append(("scene-activity",SCENE_VERSION,CONFIG))
    if kind == "radio" and job.get("calibrations", {}).get(record["name"]):
        from fpv_matcher.radio.derivation import specification
        from fpv_matcher.radio.sampling import tracking_fps
        try:
            fps = tracking_fps(job["settings"].get("sample_fps", 30), job["settings"].get("stick_sampling_ratio"), record.get("metadata", {}).get("fps"))
            result.append(specification(job["calibrations"][record["name"]], fps))
        except (ValueError, KeyError, TypeError):
            pass
        from fpv_matcher.radio.activity import VERSION as ACTIVITY_VERSION, specification as activity_specification
        try:
            result.append(("gimbal-activity", ACTIVITY_VERSION, activity_specification(job["calibrations"][record["name"]])))
        except (ValueError, KeyError, TypeError):
            pass
    if kind == "fpv":
        from fpv_matcher.motion.telemetry import VERSION
        result.append(("telemetry", VERSION, {}))
        from fpv_matcher.motion.derivation import VERSION, MAX_DIMENSION
        from fpv_matcher.motion.visual_rotation import validate_profile
        from fpv_matcher.motion.sampling import visual_fps
        try:
            fps = visual_fps(job["settings"].get("fpv_sampling_mode", "auto"), job["settings"].get("fpv_visual_fps", 30), record.get("metadata", {}).get("fps"))
            result.append(("fpv", VERSION, {"sample_fps": float(fps), "max_dimension": MAX_DIMENSION,
                                          "camera_profile": validate_profile(job["settings"].get("visual_camera"))}))
        except ValueError:
            pass
    return result


def document(job):
    result = {key: copy.deepcopy(job[key]) for key in FIELDS if key in job}
    if result.get("radio_profile"):
        result["radio_profile"].pop("path", None)
    result["source_hints"] = {kind: sorted({str(Path(r["path"]).parent) for r in records}) for kind, records in job["inputs"].items()}
    for kind, records in result["inputs"].items():
        for record in records:
            record.pop("cache_bindings", None)
            path = Path(record["path"])
            try:
                stat = path.stat()
                signature = [stat.st_size, stat.st_mtime_ns]
                expected = record.get("metadata_signature") or record.get("upload_signature")
                if expected and signature != expected:
                    raise ValueError("Source changed since session preparation")
                record["content_id"] = content_id(path)
                record["camera_modified_time"] = record.get("camera_modified_time") if record.get("camera_modified_time") is not None else (record["original_modified_ms"] / 1000 if record.get("original_modified_ms") is not None else stat.st_mtime)
                identity = record.get("sha256") if record.get("upload_signature") == signature else None
                record["cache_bindings"] = [{"kind": k, "algorithm": algorithm, "config": config,
                    "name": f"{k}.{_key(str(path), algorithm, config, identity)}.npz"} for k, algorithm, config in bindings(record, kind, job)]
            except (OSError, ValueError, TypeError):
                record.pop("content_id", None)
            record.pop("path", None)
    result["results"] = None
    try:
        result["results"] = json.loads((jobs.job_dir(job["id"]) / "results" / "results.json").read_text())
    except (OSError, ValueError):
        pass
    return {"format": FORMAT, "version": 1, "saved_at": time.time(), "session": result,
            "note": "Original videos and export history are not included. Relink recordings after opening."}


def bundle(job):
    doc = document(job)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("project.json", json.dumps(doc, allow_nan=False))
        known = set()
        for records in doc["session"]["inputs"].values():
            for record in records:
                for binding in record.get("cache_bindings", []):
                    path = jobs.DATA / "cache" / binding["name"]
                    if binding["name"] in known or not path.is_file():
                        continue
                    if buffer.tell() + path.stat().st_size > LIMIT - 1024**2:
                        raise ValueError("Project fingerprints exceed 128 MiB. Save project JSON and re-derive after relinking instead.")
                    archive.write(path, "fingerprints/" + path.name)
                    known.add(path.name)
    if len(buffer.getvalue()) > LIMIT:
        raise ValueError("Project bundle exceeds 128 MiB")
    return buffer.getvalue()


def open_project(payload):
    if len(payload) > LIMIT:
        raise ValueError("Project import is limited to 128 MiB")
    archive = None
    if payload[:2] == b"PK":
        archive = zipfile.ZipFile(io.BytesIO(payload))
        if sum(item.file_size for item in archive.infolist()) > LIMIT or len(archive.infolist()) > 2000:
            raise ValueError("Project bundle is too large")
        doc = json.loads(archive.read("project.json"))
    else:
        doc = json.loads(payload)
    if not isinstance(doc, dict):
        raise ValueError("Choose a project document, not a JSON list")
    try:
        json.dumps(doc, allow_nan=False)
    except ValueError as exc:
        raise ValueError("Project values must be finite") from exc
    if doc.get("format") != FORMAT or doc.get("version") != 1 or not isinstance(doc.get("session"), dict):
        raise ValueError("Choose a Flight Matcher project JSON or fingerprint bundle")
    data = doc["session"]
    for key in FIELDS:
        if key in {"label", "references", "no_counterpart"}:
            continue
        if key in data and not isinstance(data[key], dict):
            raise ValueError(f"Invalid project {key}")
    for key in ("references", "no_counterpart"):
        if key in data and not isinstance(data[key], list):
            raise ValueError(f"Invalid project {key}")
    result = data.get("results")
    if result is not None and (not isinstance(result, dict) or not isinstance(result.get("rows"), list)
                              or not isinstance(result.get("clips"), dict)):
        raise ValueError("Invalid project matching results")
    if set(data.get("inputs", {})) != {"fpv", "radio"} or not isinstance(data.get("settings"), dict):
        raise ValueError("Invalid project sources/settings")
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    job = {key: data[key] for key in FIELDS if key in data}
    job.update(id=job_id, source="local", created_at=now, updated_at=now, status="draft", settings_defaults_version=2,
               message="Project opened. Relink both recording folders to verify source identities.", exports={}, proxies={}, charts={},
               source_hints=data.get("source_hints", {}), label=str(data.get("label", "Imported session"))[:120],
               calibrations=data.get("calibrations", {}), overrides=data.get("overrides", {}))
    allowed = set()
    for kind, records in job["inputs"].items():
        if not isinstance(records, list):
            raise ValueError("Invalid recording list")
        names = set()
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("Invalid recording metadata")
            name = record.get("name")
            if "ignored" in record and not isinstance(record["ignored"], bool):
                raise ValueError("Invalid recording ignore setting")
            if not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name or name in names or not isinstance(record.get("size"), int) or record["size"] <= 0:
                raise ValueError("Invalid or duplicate recording filename")
            names.add(name)
            record["path"] = str(jobs.job_dir(job_id) / "unlinked" / kind / name)
            record["uploaded"] = record["size"]
            for binding in record.get("cache_bindings", []):
                if (isinstance(binding, dict) and binding.get("kind") in {"audio", "radio", "radio-review", "telemetry", "fpv", "gimbal-activity"}
                    and isinstance(binding.get("algorithm"), str) and len(binding["algorithm"]) <= 200
                    and isinstance(binding.get("config"), dict)
                    and re.fullmatch(re.escape(binding["kind"]) + r"\.[a-f0-9]{64}\.npz", binding.get("name", ""))):
                    allowed.add(binding["name"])
                else:
                    raise ValueError("Invalid fingerprint binding")
    if archive:
        cache = jobs.DATA / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        for name in allowed:
            entry = "fingerprints/" + name
            if entry in archive.namelist() and not (cache / name).exists():
                raw = archive.read(entry)
                with zipfile.ZipFile(io.BytesIO(raw)) as npz:
                    if sum(i.file_size for i in npz.infolist()) > LIMIT:
                        raise ValueError("Fingerprint arrays exceed the import limit")
                with np.load(io.BytesIO(raw), allow_pickle=False) as saved:
                    if str(saved["cache_key"].item()) != name.split(".")[1]:
                        raise ValueError("Fingerprint key disagrees with its binding")
                (cache / name).write_bytes(raw)
    jobs.save(job)
    if isinstance(data.get("results"), dict):
        atomic_json(jobs.job_dir(job_id) / "results" / "results.json", data["results"])
    return job


def restore_caches(record):
    """Called only after a relink passed the saved bounded content identity."""
    cache = jobs.DATA / "cache"
    for binding in record.get("cache_bindings", []):
        source = cache / binding["name"]
        if not source.is_file():
            continue
        key = _key(record["path"], binding["algorithm"], binding["config"])
        target = cache / f"{binding['kind']}.{key}.npz"
        if target.exists():
            continue
        with np.load(source, allow_pickle=False) as saved:
            if str(saved["cache_key"].item()) != binding["name"].split(".")[1]:
                continue
            data = {k: saved[k] for k in saved.files if k != "cache_key"}
        temporary = target.with_name("." + target.name + "." + uuid.uuid4().hex + ".npz")
        try:
            np.savez_compressed(temporary, cache_key=np.asarray(key), **data)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
