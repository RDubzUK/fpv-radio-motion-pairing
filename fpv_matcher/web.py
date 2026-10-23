from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import anyio
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from fpv_matcher import jobs
from fpv_matcher.session_scope import active_job, scope_busy
from fpv_matcher.storage import SaveError
from fpv_matcher import projects
from fpv_matcher import export_locations
from fpv_matcher.portrait import DEFAULT as PORTRAIT_DEFAULT, validate_layout, export_portrait
from fpv_matcher.config import validate_calibration
from fpv_matcher.motion.review import chart_trace
from fpv_matcher.audio.review import VERSION as WAVEFORM_VERSION, waveform_trace, audio_sections
from fpv_matcher.radio.derivation import cache_status as stick_cache_status
from fpv_matcher.radio.derivation import cached_sticks
from fpv_matcher.radio.activity import cache_status as activity_cache_status, specification as activity_specification
from fpv_matcher.radio.scene_activity import cache_status as scene_cache_status
from fpv_matcher.radio.derivation import FULL_SPAN_VERSION
from fpv_matcher.radio.landmarks import VERSION as LANDMARK_VERSION, from_annotations as review_landmarks, validate_landmarks
from fpv_matcher.radio.tracking_review import geometry_trace, correction_diagnostics
from fpv_matcher.radio.sampling import tracking_fps, validate_ratio
from fpv_matcher.radio.stick_tracker import ALGORITHM_VERSION as STICK_VERSION
from fpv_matcher.radio.stabilization import VERSION as OUTLINE_VERSION
from fpv_matcher.matching.correlation import prepare, section_support
from fpv_matcher.matching import clocks
from fpv_matcher.matching import review as pair_review
from fpv_matcher.matching.candidates import annotate as annotate_alignment
from fpv_matcher.matching.evidence import normalize_sources, motion_enabled, audio_enabled_pair, precise_motion_enabled
from fpv_matcher.motion.telemetry import VERSION as TELEMETRY_VERSION, validate_motion_settings, body_rates
from fpv_matcher.motion.visual_rotation import validate_profile as validate_visual_camera, visual_body_rates, display_interpolation, gap_summary
from fpv_matcher.video.acceleration import capabilities as tracking_capabilities
from zipfile import BadZipFile
from fpv_matcher.motion.derivation import cache_status as fpv_cache_status, embedded_status, load_saved_visual, VERSION as VISUAL_VERSION
from fpv_matcher.motion.sampling import visual_fps, MODES as VISUAL_SAMPLING_MODES
from fpv_matcher.video.metadata import filename_timestamp, format_timestamp
import numpy as np
from fpv_matcher.exports import export_pair, stream_archive
from fpv_matcher.pipeline import atomic_json
from fpv_matcher.radio.rates import validate_rate_profile, apply_rate_profile
from fpv_matcher.radio.presets import load_presets, save_preset
from fpv_matcher.radio.models import MODELS as RADIO_MODELS, VERSION as RADIO_MODEL_VERSION, model_id
from fpv_matcher.video.metadata import probe_video
from fpv_matcher.video.frames import jpeg_frame
from fpv_matcher.video.reader import EXTENSIONS, video_files
from fpv_matcher.video.preview import manifest as preview_manifest, fragment as preview_fragment

ANALYSIS_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analysis")
EXPORT_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="export")
CHART_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="motion-review")
CHART_LOCK = threading.RLock()
CHART_TASKS = {}
PROXY_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="proxy")
PREVIEW_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="preview")
PROCESS_LOCK = threading.RLock()
PROCESSES: dict[str, subprocess.Popen] = {}
CANCELLED: set[str] = set()
UPLOAD_LOCKS: dict[tuple, threading.Lock] = {}
STATIC = Path(__file__).parent / "static"
MAX_FILE_SIZE = 128 * 1024**3
CHUNK_LIMIT = 32 * 1024**2


def _stop_process(key):
    with PROCESS_LOCK:
        CANCELLED.add(key)
        process = PROCESSES.get(key)
        if process and process.poll() is None:
            if os.name == "nt":
                try:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                except OSError:
                    process.terminate()
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass


def _worker_command(module, *arguments):
    if getattr(sys, "frozen", False):
        # Re-enter the packaged executable in worker-dispatch mode. This keeps
        # the child process on Windows inside the same one-folder distribution.
        return [sys.executable, "--fpv-worker", "-m", module, *(str(argument) for argument in arguments)]
    return [sys.executable, "-u", "-m", module, *(str(argument) for argument in arguments)]


def _worker_process_options():
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


@asynccontextmanager
async def lifespan(app):
    jobs.acquire_instance()
    try:
        jobs.recover()
    except Exception:
        jobs.release_instance()
        raise
    yield
    for job in jobs.all_jobs():
        if job.get("recovery_required"):
            continue

        def mark_interrupted(current):
            if current["status"] in {"queued", "running"}:
                current.update(
                    status="interrupted",
                    message="Server stopped; run analysis again to reuse cached fingerprints.",
                )
            for collection in ("exports", "proxies", "charts"):
                for item in current.get(collection, {}).values():
                    if item["status"] in {"queued", "running"}:
                        item.update(status="interrupted", message="Server stopped; request again.")

        jobs.update(job["id"], mark_interrupted)
    with PROCESS_LOCK:
        keys = list(PROCESSES)
    for key in keys:
        _stop_process(key)
    # Queued work will be marked interrupted and can be explicitly rerun after a restart.
    for pool in (ANALYSIS_POOL, EXPORT_POOL, PROXY_POOL, CHART_POOL, PREVIEW_POOL):
        pool.shutdown(wait=False, cancel_futures=True)
    jobs.release_instance()


app = FastAPI(title="PropNutz Flight Matcher", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(SaveError)
async def save_failed(request, exc):
    return Response(json.dumps({"detail": str(exc)}), status_code=503, media_type="application/json")


@app.middleware("http")
async def frontend_types(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        media_type = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}.get(Path(request.url.path).suffix.lower())
        if media_type and response.status_code in {200, 206, 304}:
            response.headers["Content-Type"] = media_type
            response.headers["Cache-Control"] = "no-cache"
    elif request.url.path == "/":
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


def get_job(job_id, allow_recovery=False, include_ignored=False):
    try:
        job = jobs.get(job_id)
        if job.get("recovery_required") and not allow_recovery:
            raise HTTPException(409, "Session metadata is unreadable. Restore an offered backup before processing.")
        if (job.get("radio_profile") or {}).get("label") == "My session radio":
            job["radio_profile"]["label"] = "Other"
        return job if include_ignored else active_job(job)
    except KeyError:
        raise HTTPException(404, "Job not found") from None


def safe_name(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 220
        or Path(value).name != value
        or any(ord(c) < 32 for c in value)
        or "\\" in value
    ):
        raise HTTPException(400, "Video names must be plain filenames with no path or control characters")
    if Path(value).suffix.lower() not in EXTENSIONS:
        raise HTTPException(400, f"Unsupported video type: {value}")
    return value


def _record(job, kind, name):
    if kind not in {"radio", "fpv"}:
        raise HTTPException(404, "Video not found")
    for item in job["inputs"][kind]:
        if item["name"] == name:
            return item
    raise HTTPException(404, "Video not found")


def _input_video(job, kind, name):
    record = _record(job, kind, name)
    if record.get("uploaded", 0) != record["size"] or not Path(record["path"]).is_file():
        raise HTTPException(409, f"{name} has not finished uploading")
    return Path(record["path"])


def _metadata(job, kind, name):
    path = _input_video(job, kind, name)
    try:
        signature = [path.stat().st_size, path.stat().st_mtime_ns]
    except OSError:
        raise HTTPException(409, f"{name} is unavailable. Restore or relink its source folder.") from None
    record = _record(job, kind, name)
    if (
        record.get("metadata_signature") == signature
        and record.get("metadata")
        and "color" in record["metadata"]
    ):
        return {**record["metadata"], "source_signature": signature, "source_revision": _signature_revision(signature)}
    try:
        info = probe_video(path)
        content = projects.content_id(path)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(422, str(exc)) from exc
    date_timestamp = _modified_time_status(job, record)["timestamp"]

    def mutate(current):
        item = _record(current, kind, name)
        if kind == "radio" and record.get("metadata_signature") and record["metadata_signature"] != signature and name in current.get("calibrations", {}):
            current.setdefault("calibration_signatures", {}).setdefault(name, record["metadata_signature"])
            current.setdefault("calibration_reviews", {}).setdefault(name, {}).update(status="source_changed")
        item.update(metadata=info, metadata_signature=signature, content_id=content, content_signature=signature)
        if item.get("camera_modified_time") is None or record.get("content_signature") != signature:
            item["camera_modified_time"] = date_timestamp
        if current["source"] == "local":
            item.update(size=info["size"], uploaded=info["size"])

    jobs.update(job["id"], mutate)
    return {**info, "source_signature": signature, "source_revision": _signature_revision(signature)}


def _public(job):
    job = active_job(job)
    result = {
        key: job.get(key)
        for key in (
            "id",
            "status",
            "message",
            "created_at",
            "updated_at",
            "settings",
            "calibrations",
            "source",
            "label",
            "no_counterpart",
            "calibration_reviews",
            "radio_orientation_checks",
            "settings_defaults_version",
            "deleted_at",
        )
    }
    result["segment_scans"] = [{"id": key, **value} for key, value in job.get("charts", {}).items() if value.get("kind") == "fpv-segments"]
    result["inputs"] = {
        kind: [{key: item.get(key) for key in ("name", "size", "uploaded", "metadata", "metadata_signature", "fpv_recorder", "camera_modified_time")} for item in records]
        for kind, records in job["inputs"].items()
    }
    result["ignored_inputs"] = {
        kind: [{key: item.get(key) for key in ("name", "size", "uploaded", "metadata", "ignored_at")}
               for item in records] for kind, records in job["_ignored_inputs"].items()}
    result["scope_editable"] = not scope_busy(job)
    result["fingerprint_tasks"] = [{"id":key,**task} for key,task in job.get("charts",{}).items()
                                   if task.get("kind") in {"motor-search","cross-modal-search","fingerprint-study"}]
    progress_path = jobs.job_dir(job["id"]) / "results" / "progress.json"
    if job["status"] in {"running", "complete"} and progress_path.exists():
        try:
            result["progress"] = json.loads(progress_path.read_text(encoding="utf-8"))
            result["message"] = result["progress"]["message"]
        except (OSError, ValueError):
            pass
    early_path = progress_path.parent / "early.json"
    if job["status"] == "running" and result.get("progress", {}).get("stage") == "review" and early_path.exists():
        try:
            result["early_proposal"] = json.loads(early_path.read_text())
        except (OSError, ValueError):
            pass
    result["calibrations"] = {name: {**config, **({"source_changed": True} if job.get("calibration_reviews", {}).get(name, {}).get("status") == "source_changed" else {})} for name, config in job.get("calibrations", {}).items()}
    result["source_folders"] = {kind: sorted({str(Path(item["path"]).parent) for item in records}) for kind, records in job["inputs"].items()}
    result["has_results"] = (jobs.job_dir(job["id"]) / "results" / "results.json").is_file() or bool(job.get("pair_overrides") or job.get("rejected_pairs") or any(d.get("manual_pair") for d in job.get("review_decisions", {}).values()))
    result["last_successful_at"] = job.get("last_successful_at")
    result["recovery_required"] = job.get("recovery_required")
    result["persistence_failed"] = job.get("persistence_failed", False)
    result["export_destination"] = job.get("export_destination")
    result["exports"] = [
        {"id": key, "status": value["status"], "message": value["message"]}
        for key, value in job.get("exports", {}).items()
    ]
    result["calibration_scans"] = [{"id": key, **value} for key, value in job.get("charts", {}).items()
                                   if value.get("kind") == "calibration-scan" and not value.get("superseded")]
    profile = job.get("radio_profile")
    result["radio_profile"] = {k: profile[k] for k in ("label", "revision", "name", "reference_time", "radio_rotation_deg")} if profile else None
    if profile:
        result["radio_profile"]["model_id"] = profile.get("model_id", model_id(profile["label"]))
    return result



@app.get("/api/jobs/{job_id}/sources")
def source_status(job_id: str):
    job = get_job(job_id, include_ignored=True)
    rows = []
    for kind, records in job["inputs"].items():
        for record in records:
            path = Path(record["path"])
            try:
                stat = path.stat()
                readable = path.is_file() and os.access(path, os.R_OK)
                expected = record.get("metadata_signature") or record.get("upload_signature")
                current = [stat.st_size, stat.st_mtime_ns]
                state = "ready" if readable else "unreadable"
                if expected and current != expected:
                    state = "changed"
                rows.append({"kind": kind, "name": record["name"], "path": str(path), "status": state, "ignored": bool(record.get("ignored")),
                             "signature": current, "reason": "Review again after matching" if state == "changed" else ""})
            except OSError as exc:
                rows.append({"kind": kind, "name": record["name"], "path": str(path), "status": "unavailable", "reason": str(exc)})
    return {"source": job["source"], "clips": rows}


@app.put("/api/jobs/{job_id}/sources")
def relink_sources(job_id: str, body: dict = Body(...)):
    job = get_job(job_id, include_ignored=True)
    if job["source"] != "local":
        raise HTTPException(400, "Uploaded media is stored by this app; relinking is for server folders")
    if job["status"] in {"running", "queued", "cancelling"} or any(task["status"] in {"queued", "running"} for task in job.get("charts", {}).values()) or any(task["status"] in {"queued", "running"} for task in job.get("exports", {}).values()):
        raise HTTPException(409, "Finish or cancel active work before relinking source files")
    kind = body.get("kind")
    if kind not in {"fpv", "radio"}:
        raise HTTPException(400, "Choose FPV or StickCam")
    root = Path(str(body.get("path", ""))).expanduser().resolve()
    if not root.is_dir():
        raise HTTPException(400, "Choose an existing source folder on the server")
    replacements, missing, identified = {}, [], []
    for record in job["inputs"][kind]:
        candidate = root / record["name"]
        if candidate.is_file() and os.access(candidate, os.R_OK):
            stat = candidate.stat()
            recognized = bool(record.get("content_id") and projects.content_id(candidate) == record["content_id"])
            if recognized:
                identified.append(record["name"])
            replacements[record["name"]] = (str(candidate.resolve()), stat.st_size, stat.st_mtime_ns, recognized)
        else:
            missing.append(record["name"])
    if not replacements:
        raise HTTPException(400, "This folder contains none of this session's filenames")
    def update(current):
        for record in current["inputs"][kind]:
            if record["name"] not in replacements:
                continue
            path, size, modified, recognized = replacements[record["name"]]
            old_signature = record.get("metadata_signature") or record.get("upload_signature")
            changed = record.get("metadata_signature") != [size, modified]
            record.update(path=path, size=size, uploaded=size)
            if recognized:
                signature = [size, modified]
                record.update(metadata_signature=signature, content_signature=signature)
                record.pop("sha256", None)
                record.pop("upload_signature", None)
                if kind == "radio" and current.get("calibration_signatures", {}).get(record["name"]) == old_signature:
                    current["calibration_signatures"][record["name"]] = signature
                for collection in ("pair_overrides", "rejected_pairs", "review_decisions", "sync_points"):
                    for decision in current.get(collection, {}).values():
                        if decision.get(kind + "_video") == record["name"] and decision.get("source_signatures", {}).get(kind) == old_signature:
                            decision["source_signatures"][kind] = signature
                            for key in (kind + "_info",):
                                if isinstance(decision.get(key), dict):
                                    decision[key].update(size=size, mtime_ns=modified)
                try:
                    result = _results(current)
                    for clip in result["clips"][kind]:
                        if clip["name"] == record["name"] and clip.get("size") == old_signature[0] and clip.get("mtime_ns") == old_signature[1]:
                            clip.update(size=size, mtime_ns=modified, source_usable=True)
                    atomic_json(jobs.job_dir(job_id) / "results" / "results.json", result)
                except (HTTPException, TypeError):
                    pass
                if kind == "radio":
                    profile = current.get("radio_profile")
                    if profile and profile.get("name") == record["name"] and profile.get("signature") == old_signature:
                        profile.update(path=path, signature=signature)
                    legacy = current.get("overrides", {}).get(record["name"])
                    if legacy and legacy.get("source_signatures", {}).get("radio") == old_signature:
                        legacy["source_signatures"]["radio"] = signature
                projects.restore_caches(record)
                for reference in current.get("references", []):
                    if reference.get(kind + "_video") == record["name"] and reference.get("source_signatures", {}).get(kind) == old_signature:
                        reference["source_signatures"][kind] = signature
            elif changed:
                if kind == "radio" and record.get("metadata_signature") and record["name"] in current.get("calibrations", {}):
                    current.setdefault("calibration_signatures", {}).setdefault(record["name"], record["metadata_signature"])
                    current.setdefault("calibration_reviews", {}).setdefault(record["name"], {}).update(status="source_changed")
                for key in ("metadata", "metadata_signature", "sha256", "upload_signature"):
                    record.pop(key, None)
        current.update(message="Source folder relinked. Check changed files and rerun matching to reuse valid caches.")
    jobs.update(job_id, update)
    return {"relinked": len(replacements), "missing": missing, "content_identified": identified,
            "identity_note": "Size and 128 KiB head/tail samples assist relinking; this is not a full-file integrity check."}


def _tracking_mode(value):
    if value not in {"auto", "cpu", "cuda"}:
        raise HTTPException(400, "Choose automatic, CPU or CUDA tracking")
    if value == "cuda" and not tracking_capabilities().get("available"):
        raise HTTPException(400, tracking_capabilities()["reason"])
    return value


@app.get("/api/system")
def system_info():
    jobs.DATA.mkdir(parents=True, exist_ok=True)
    space = shutil.disk_usage(jobs.DATA)
    return {
        "version": "0.2.0",
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "free_bytes": space.free,
        "total_bytes": space.total,
        "max_file_bytes": MAX_FILE_SIZE,
        "tracking": tracking_capabilities(),
        "analysis_slots": 1,
        "export_slots": 1,
        "data_directory": str(jobs.DATA),
    }


@app.post("/api/rates-preview")
def preview_rates(body: dict = Body(...)):
    try:
        profile = validate_rate_profile(body.get("profile"))
        if profile is None:
            raise ValueError("Enter the rates to calculate maximum rotation speeds")
        transformed = apply_rate_profile({axis: np.array([-1., 1.]) for axis in ("roll", "pitch", "yaw")}, profile)
        return {"maximum_dps": {axis: float(np.max(np.abs(transformed[axis]))) for axis in ("roll", "pitch", "yaw")}}
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/browse")
def browse_directory(path: str | None = None):
    """List folders and supported video files on the machine running the app."""
    try:
        directory = Path(path).expanduser().resolve(strict=True) if path else Path.home().resolve(strict=True)
        if not directory.is_dir():
            raise HTTPException(400, "Choose a folder")
        entries = []
        with os.scandir(directory) as children:
            for child in children:
                try:
                    if child.is_dir(follow_symlinks=True):
                        target = Path(child.path).resolve(strict=True)
                        if target.is_dir():
                            entries.append({"name": child.name, "path": str(target), "kind": "folder"})
                    elif (
                        child.is_file(follow_symlinks=True) and Path(child.name).suffix.lower() in EXTENSIONS
                    ):
                        entries.append({"name": child.name, "kind": "video", "size": child.stat().st_size})
                except (OSError, RuntimeError):
                    continue
        entries.sort(key=lambda item: (item["kind"] != "folder", item["name"].casefold()))
        truncated = len(entries) > 500
        entries = entries[:500]
    except HTTPException:
        raise
    except FileNotFoundError as exc:
        raise HTTPException(404, "Folder not found") from exc
    except PermissionError as exc:
        raise HTTPException(403, "The app cannot read this folder") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(400, f"Cannot open this folder: {exc}") from exc
    videos = sum(item["kind"] == "video" for item in entries)
    network_shares = []
    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8")
        for line in mounts.splitlines():
            fields = line.split()
            if len(fields) < 3 or fields[2].lower() not in {"cifs", "smb3", "smbfs"}:
                continue
            mount_path = fields[1]
            for encoded, decoded in (("\\040", " "), ("\\011", "\t"), ("\\134", "\\")):
                mount_path = mount_path.replace(encoded, decoded)
            mount = Path(mount_path)
            network_shares.append({"name": mount.name or mount_path, "path": mount_path, "kind": "folder"})
    except OSError:
        pass
    return {
        "path": str(directory),
        "parent": str(directory.parent) if directory.parent != directory else None,
        "entries": entries,
        "video_count": videos,
        "truncated": truncated,
        "network_shares": network_shares,
    }


@app.get("/api/jobs")
def history():
    return {
        "jobs": [
            {
                "id": job["id"],
                "status": job["status"],
                "message": job["message"],
                "created_at": job["created_at"],
                "label": job.get("label", job["id"]),
                "radio_count": sum(not r.get("ignored") for r in job["inputs"]["radio"]),
                "fpv_count": sum(not r.get("ignored") for r in job["inputs"]["fpv"]),
                "ignored_count": sum(bool(r.get("ignored")) for records in job["inputs"].values() for r in records),
                "thumbnail_url": f"/api/jobs/{job['id']}/thumbnail" if any(not r.get("ignored") and r.get("uploaded") == r["size"] for r in job["inputs"]["fpv"]) else None,
            }
            for job in jobs.all_jobs()
        ]
    }


@app.get("/api/radio-models")
def radio_models():
    return {"version": RADIO_MODEL_VERSION, "models": RADIO_MODELS}


THUMBNAIL_LOCK = threading.Lock()


@app.get("/api/jobs/{job_id}/thumbnail")
def session_thumbnail(job_id: str):
    job = get_job(job_id)
    dated = []
    for kind, records in job["inputs"].items():
        for record in records:
            if record.get("uploaded") != record["size"]:
                continue
            try:
                path = _input_video(job, kind, record["name"])
                stat = path.stat()
                modified_ns = stat.st_mtime_ns
                if job["source"] == "upload" and record.get("original_modified_ms") is not None and record.get("upload_signature") == [stat.st_size, stat.st_mtime_ns]:
                    modified_ns = int(record["original_modified_ms"] * 1_000_000)
                dated.append((kind, record["name"], path, stat, modified_ns))
            except (HTTPException, OSError):
                continue
    fpv = [entry for entry in dated if entry[0] == "fpv"]
    if not fpv:
        raise HTTPException(404, "No available FPV recording for this thumbnail")
    # Subtract an epoch before averaging to retain nanosecond precision.
    epoch = min(row[4] for row in dated)
    mean = sum(row[4] - epoch for row in dated) / len(dated)
    ordered = sorted(fpv, key=lambda row: (abs(row[4] - epoch - mean), row[1]))
    # One still decoder at a time; no batch proxy/video conversion. Disk caches
    # survive restarts, and image URLs are loaded lazily by the session cards.
    with THUMBNAIL_LOCK:
        for _, name, path, stat, _ in ordered[:3]:
            try:
                key = hashlib.sha256(f"session-still-v1:{path}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()
                destination = jobs.job_dir(job_id) / "thumbnails" / f"{key}.jpg"
                if not destination.is_file():
                    info = _metadata(job, "fpv", name)
                    at = 90. if info["duration"] > 90.1 else 60. if info["duration"] > 60.1 else info["duration"] / 2
                    image = jpeg_frame(path, at, 400, 225)
                    current = path.stat()
                    if (current.st_size, current.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    temporary = destination.with_suffix(".tmp")
                    temporary.write_bytes(image)
                    os.replace(temporary, destination)
                return FileResponse(destination, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300", "X-Thumbnail-Source": quote(name)})
            except (HTTPException, ValueError, RuntimeError, OSError, subprocess.SubprocessError):
                continue
    raise HTTPException(422, "Could not decode an FPV session thumbnail")


@app.get("/api/deleted-sessions")
def deleted_sessions():
    return {"sessions": [{"id": job["id"], "label": job.get("label", job["id"]), "deleted_at": job["deleted_at"]}
                         for job in jobs.all_jobs(include_deleted=True) if job.get("deleted_at")]}


@app.delete("/api/jobs/{job_id}")
def delete_session(job_id: str):
    with jobs.LOCK:
        job = get_job(job_id)
        if job["status"] in {"queued", "running", "cancelling"} or any(
            item["status"] in {"queued", "running", "cancelling"}
            for collection in ("exports", "charts", "proxies") for item in job.get(collection, {}).values()):
            raise HTTPException(409, "Stop active matching, extraction or export tasks before deleting this session")
        jobs.update(job_id, lambda current: current.update(deleted_at=time.time()))
    # Hide the session only. Media, exports, fingerprints and existing download URLs remain intact.
    return {"deleted": True, "exports_preserved": True, "restorable": True}


@app.post("/api/jobs/{job_id}/restore")
def restore_session(job_id: str):
    get_job(job_id)
    jobs.update(job_id, lambda current: current.pop("deleted_at", None))
    return {"restored": True}


@app.post("/api/jobs")
def create_job(body: dict = Body(...)):
    source = body.get("source", "upload")
    if source not in {"upload", "local"}:
        raise HTTPException(400, "Source must be upload or local")
    job_id = uuid.uuid4().hex[:12]
    base = jobs.job_dir(job_id)
    inputs = {"fpv": [], "radio": []}
    try:
        for kind in inputs:
            if source == "local":
                paths = video_files(str(body.get(kind + "_path", ""))) if body.get(kind + "_path") else []
                generated = [Path(e["directory"]).resolve() for e in export_locations.records(include_pending=True)]
                paths = [p for p in paths if not any(root == p.resolve() or root in p.resolve().parents for root in generated)]
                inputs[kind] = [
                    {
                        "name": safe_name(p.name),
                        "path": str(p),
                        "size": p.stat().st_size,
                        "uploaded": p.stat().st_size,
                    }
                    for p in paths
                ]
            else:
                declarations = body.get("files", {}).get(kind, [])
                for item in declarations:
                    name = safe_name(item["name"])
                    size = int(item["size"])
                    if not 0 < size <= MAX_FILE_SIZE:
                        raise ValueError(f"{name} must be between 1 byte and 128 GiB")
                    record = {"name": name, "path": str(base / kind / name), "size": size, "uploaded": 0}
                    if item.get("last_modified") is not None:
                        modified = float(item["last_modified"])
                        if not math.isfinite(modified) or not 0 <= modified <= 10**14:
                            raise ValueError("File modified date must be a finite nonnegative timestamp")
                        record["original_modified_ms"] = modified
                    inputs[kind].append(record)
            if not inputs[kind]:
                raise ValueError("Choose at least one recording of each type")
            if len({item["name"] for item in inputs[kind]}) != len(inputs[kind]):
                raise ValueError(f"Each {kind} filename must be unique within this batch")
    except (KeyError, ValueError, TypeError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    jobs.DATA.mkdir(parents=True, exist_ok=True)
    required = (
        sum(item["size"] for records in inputs.values() for item in records) if source == "upload" else 0
    )
    if required and required > shutil.disk_usage(jobs.DATA).free - 1024**3:
        raise HTTPException(507, "Not enough disk space for this upload batch (plus 1 GiB working space)")
    for kind in inputs:
        (base / kind).mkdir(parents=True, exist_ok=True)
    try:
        presets = load_presets()
        default_rates = presets["presets"].get(presets["default"])
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc
    job = {
        "id": job_id,
        "created_at": time.time(),
        "updated_at": time.time(),
        "source": source,
        "status": "draft" if source == "local" else "uploading",
        "message": "Ready to find flights automatically" if source == "local" else "Ready for resumable uploads",
        "label": str(body.get("label", ""))[:120] or time.strftime("Session %d %b %H:%M"),
        "inputs": inputs,
        "calibrations": {},
        "settings_defaults_version": 2,
        "settings": {
            "sample_fps": 30,
            "fpv_sampling_mode": "auto",
            "fpv_visual_fps": 30,
            "stick_mode": 2,
            "visual_camera": validate_visual_camera(),
            "stick_sampling_ratio": None,
            "match_fps": 30,
            "min_overlap": 2,
            "workers": 2,
            "minimum_score": 0.55,
            "audio_enabled": True,
            "matching_strategy": "automatic",
            "embedded_seed_enabled": True, "seed_spot_checks": 2,
            "evidence_sources": normalize_sources({"fpv_visual": True, "gimbal_activity": True, "stick_inputs": False}),
            "match_sample_percent": 25,
            "match_target_kind": "all",
            "match_target_name": "",
            "match_target_names": [],
            "stop_on_confident_match": True,
            "allow_unconfigured_radio": True,
            "rate_profile": default_rates,
            "motion_source": "auto",
            "camera_uptilt_deg": 0,
            "imu_orientation": "zXy",
            "gyro_video_offset": 0,
            "decode_mode": "auto", "tracking_backend": "auto",
            "audio_boundary": 30, "audio_adaptive": True, "audio_retry_limit": 120,
        },
        "overrides": {},
        "exports": {},
        "proxies": {},
    }
    jobs.save(job)
    return _public(job)


@app.get("/api/rate-presets")
def rate_presets():
    try:
        return load_presets()
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/rate-presets")
def store_rate_preset(body: dict = Body(...)):
    try:
        return save_preset(body.get("name"), body.get("profile"), body.get("make_default", False))
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/jobs/{job_id}")
def job_status(job_id):
    return _public(get_job(job_id, allow_recovery=True))


@app.get("/api/jobs/{job_id}/backups")
def metadata_backups(job_id: str):
    get_job(job_id, allow_recovery=True)
    return {"backups": jobs.backups(job_id)}


@app.get("/api/jobs/{job_id}/project")
def save_project(job_id: str, fingerprints: bool = False):
    job = get_job(job_id, include_ignored=True)
    try:
        if fingerprints:
            return Response(projects.bundle(job), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="flight-matcher-project.zip"'})
        return Response(json.dumps(projects.document(job), indent=2, allow_nan=False), media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="flight-matcher-project.json"'})
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/projects/open")
async def open_project(request: Request):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > projects.LIMIT:
            raise HTTPException(413, "Project import is limited to 128 MiB")
    try:
        return _public(await anyio.to_thread.run_sync(projects.open_project, bytes(data)))
    except (ValueError, OSError, KeyError, BadZipFile, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/jobs/{job_id}/backups/restore")
def restore_metadata(job_id: str, body: dict = Body(...)):
    with PROCESS_LOCK:
        if any(job_id in key for key in PROCESSES):
            raise HTTPException(409, "Finish active processing before restoring metadata")
    job = get_job(job_id, allow_recovery=True)
    if not job.get("recovery_required") and (job["status"] in {"running", "queued", "cancelling"} or any(t["status"] in {"queued", "running"} for collection in ("charts", "exports", "proxies") for t in job.get(collection, {}).values())):
        raise HTTPException(409, "Finish active work before restoring metadata")
    try:
        return _public(jobs.restore(job_id, body.get("name")))
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/jobs/{job_id}")
def rename_job(job_id: str, body: dict = Body(...)):
    get_job(job_id)
    label = body.get("label")
    if not isinstance(label, str) or not label.strip():
        raise HTTPException(400, "Enter a session name")
    label = label.strip()
    if len(label) > 120:
        raise HTTPException(400, "Session names must be 120 characters or fewer")
    job = jobs.update(job_id, lambda current: current.update(label=label))
    return _public(job)


@app.put("/api/jobs/{job_id}/recordings/{kind}/{filename}/ignored")
def ignore_recording(job_id: str, kind: str, filename: str, body: dict = Body(...)):
    ignored = body.get("ignored")
    if not isinstance(ignored, bool):
        raise HTTPException(400, "Choose whether to ignore this recording")
    get_job(job_id, include_ignored=True)

    def change(current):
        if current.get("deleted_at"):
            raise HTTPException(409, "Restore this session before changing its recordings")
        if scope_busy(current):
            raise HTTPException(409, "Finish or cancel active work before ignoring or restoring recordings")
        record = _record(current, kind, filename)
        if ignored:
            record.update(ignored=True, ignored_at=time.time())
        else:
            record.pop("ignored", None)
            record.pop("ignored_at", None)
        # Keep calibration, fingerprints, alignments and exports for restoration.

    return _public(jobs.update(job_id, change))


@app.put("/api/jobs/{job_id}/upload/{kind}/{filename}")
async def upload_chunk(job_id: str, kind: str, filename: str, request: Request):
    job = get_job(job_id)
    if job["source"] != "upload" or job["status"] not in {"uploading", "draft"}:
        raise HTTPException(409, "This job does not accept uploads")
    record = _record(job, kind, filename)
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", request.headers.get("content-range", ""))
    if not match:
        raise HTTPException(400, "Supply Content-Range: bytes start-end/total")
    start, end, total = (int(value) for value in match.groups())
    if total != record["size"] or not 0 <= start <= end < total or end - start + 1 > CHUNK_LIMIT:
        raise HTTPException(400, "Invalid upload range (maximum chunk: 32 MiB)")
    if start != record["uploaded"]:
        raise HTTPException(409, f"Resume this file at byte {record['uploaded']}")
    key = (job_id, kind, filename)
    with PROCESS_LOCK:
        guard = UPLOAD_LOCKS.setdefault(key, threading.Lock())
    if not guard.acquire(blocking=False):
        raise HTTPException(409, "An upload for this file is already in progress")
    target = Path(record["path"])
    received = 0
    try:
        current_record = _record(get_job(job_id), kind, filename)
        if start != current_record["uploaded"]:
            raise HTTPException(409, f"Resume this file at byte {current_record['uploaded']}")
        if end - start + 1 > shutil.disk_usage(jobs.DATA).free - 256 * 1024**2:
            raise HTTPException(507, "Not enough disk space to continue uploading")
        async with await anyio.open_file(target, "r+b" if target.exists() else "w+b") as stream:
            await stream.truncate(start)  # Discard any partial chunk left by a lost connection.
            await stream.seek(start)
            try:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > end - start + 1:
                        raise HTTPException(400, "Chunk body exceeds its declared range")
                    await stream.write(chunk)
                if received != end - start + 1:
                    raise HTTPException(400, "Chunk body is shorter than its declared range")
                await stream.flush()
                await asyncio.to_thread(os.fsync, stream.wrapped.fileno())
            except BaseException:
                await stream.truncate(start)
                raise
        digest = None
        if end + 1 == total:

            def hash_file():
                hasher = hashlib.sha256()
                with target.open("rb") as source:
                    for chunk in iter(lambda: source.read(8 * 1024**2), b""):
                        hasher.update(chunk)
                return hasher.hexdigest()

            digest = await asyncio.to_thread(hash_file)

        def mutate(current):
            item = _record(current, kind, filename)
            item["uploaded"] = end + 1
            if digest:
                item["sha256"] = digest
                item["upload_signature"] = [target.stat().st_size, target.stat().st_mtime_ns]
            if all(i["uploaded"] == i["size"] for records in current["inputs"].values() for i in records):
                current.update(status="draft", message="All uploads complete. Ready to find flights automatically.")

        jobs.update(job_id, mutate)
        return {"uploaded": end + 1, "complete": end + 1 == total}
    finally:
        guard.release()


@app.get("/api/jobs/{job_id}/metadata/{kind}/{filename}")
def metadata(job_id, kind, filename):
    return _metadata(get_job(job_id), kind, filename)


@lru_cache(maxsize=24)
def _calibration_jpeg(video_path: str, size: int, modified_ns: int, at: float):
    # File size/mtime invalidate the bounded thumbnail cache when media changes.
    try:
        return jpeg_frame(video_path, at)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/jobs/{job_id}/frame/{kind}/{filename}")
def calibration_frame(job_id: str, kind: str, filename: str, at: float = 0):
    job = get_job(job_id)
    info = _metadata(job, kind, filename)
    if not math.isfinite(at) or not 0 <= at < info["duration"]:
        raise HTTPException(400, "Choose a frame inside the recording")
    video_path = _input_video(job, kind, filename)
    stat = video_path.stat()
    jpeg = _calibration_jpeg(str(video_path), stat.st_size, stat.st_mtime_ns, at)
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=60"})


@app.put("/api/jobs/{job_id}/calibration/{filename}")
def save_calibration(job_id: str, filename: str, config: dict = Body(...), proposal_id: str | None = None):
    job = get_job(job_id)
    if job["status"] in {"running", "queued", "cancelling"} or any(task.get("kind") == "stick-derivation" and task["status"] in {"queued", "running"} for task in job.get("charts", {}).values()):
        raise HTTPException(409, "Finish or cancel matching / StickCam derivation before changing calibration")
    _record(job, "radio", filename)
    config.pop("source_changed", None)
    config.pop("auto_generated", None)
    if proposal_id:
        directory = _chart_directory(job_id, proposal_id)
        proposals = directory / "calibration-review.json"
        if not proposals.exists() or job.get("charts", {}).get(proposal_id, {}).get("superseded"):
            raise HTTPException(409, "That calibration proposal is no longer available")
        proposal = next((row for row in json.loads(proposals.read_text())["clips"] if row["name"] == filename), None)
        path = _input_video(job, "radio", filename)
        if not proposal or [path.stat().st_size, path.stat().st_mtime_ns] != proposal.get("signature"):
            raise HTTPException(409, "Source changed; find a new calibration proposal")
    if config.get("exclude"):
        config = {"exclude": True}
        jobs.update(job_id, lambda current: current["calibrations"].update({filename: config}))
        return {"saved": True, "config": config}
    info = _metadata(job, "radio", filename)
    prior_signature = job.get("calibration_signatures", {}).get(filename)
    if prior_signature and prior_signature != info.get("source_signature"):
        config.pop("tracking_landmarks", None)
    else:
        for key in ("tracking_landmarks", "capture_tracking_geometry"):
            if key not in config and key in job.get("calibrations", {}).get(filename, {}):
                config[key] = job["calibrations"][filename][key]
    try:
        if config.get("skip_motion"):
            config = {
                "skip_motion": True,
                "reference_time": 0,
                "stickcam_mode": config.get("stickcam_mode", "wonky"),
            }
        else:
            config = validate_calibration(config, (info["width"], info["height"]))
            if config["reference_time"] >= info["duration"]:
                raise ValueError("Calibration frame is past the end of the recording")
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    def save(current):
        current["calibrations"].update({filename: config})
        record = _record(current, "radio", filename)
        if record.get("metadata_signature"):
            current.setdefault("calibration_signatures", {})[filename] = record["metadata_signature"]
        current.setdefault("calibration_reviews", {}).setdefault(filename, {}).update(status="confirmed")
        if proposal_id:
            current.setdefault("calibration_reviews", {})[filename] = {"proposal_id": proposal_id,
                "status": "confirmed", "at": config["reference_time"], "updated_at": time.time()}
    jobs.update(job_id, save)
    return {"saved": True, "config": config}


@app.put("/api/jobs/{job_id}/visual-camera")
def set_visual_camera(job_id: str, body: dict = Body(...)):
    job = get_job(job_id)
    if job["status"] in {"queued", "running", "cancelling"} or any(t.get("kind") == "fpv-preparation" and t["status"] in {"running", "queued"} for t in job.get("charts", {}).values()):
        raise HTTPException(409, "Finish or cancel motion preparation / matching before changing lens geometry")
    try:
        profile = validate_visual_camera(body)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    jobs.update(job_id, lambda current: current["settings"].update(visual_camera=profile))
    return {"visual_camera": profile}


@app.put("/api/jobs/{job_id}/stick-mode")
def set_session_stick_mode(job_id: str, body: dict = Body(...)):
    from fpv_matcher.radio.modes import validate_mode
    job = get_job(job_id)
    if job["status"] in {"running", "queued", "cancelling"} or any(t["status"] in {"running", "queued"} and t.get("kind") in {"stick-derivation", "motion-review", "calibration-scan"} for t in job.get("charts", {}).values()):
        raise HTTPException(409, "Finish or cancel matching / StickCam preparation before changing stick assignments")
    try:
        mode = validate_mode(body.get("stick_mode", 2))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    def update(current):
        current["settings"]["stick_mode"] = mode
        for config in current["calibrations"].values():
            if not config.get("exclude"):
                config["stick_mode"] = mode
    jobs.update(job_id, update)
    return {"stick_mode": mode, "calibrations": get_job(job_id)["calibrations"]}


@app.post("/api/jobs/{job_id}/calibration/{filename}/suggest")
def suggest_calibration(job_id: str, filename: str, at: float = 0, request: dict = Body(default={})):
    import cv2
    import numpy as np

    from fpv_matcher.radio.orientation import rotation_from_regions, suggest_upright_regions, suggest_radio_orientation
    from fpv_matcher.radio.reference import suggest_from_reference
    from fpv_matcher.radio.tips import suggest_visible_tip

    job = get_job(job_id)
    _record(job, "radio", filename)
    try:
        rotation = request.get("radio_rotation_deg", job["calibrations"].get(filename, {}).get("radio_rotation_deg", 0))
        if rotation not in {0, 180}:
            raise ValueError("Choose upright or upside down for this calibration frame")
        frame = calibration_frame(job_id, "radio", filename, at)
        image = cv2.imdecode(np.frombuffer(frame.body, np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError("Could not read the reference frame")
        outline = request.get("radio_outline", job["calibrations"].get(filename, {}).get("radio_outline"))
        if outline and len(outline) != 4:
            raise ValueError("Finish marking all four radio corners before suggesting regions.")
        if outline and len(outline) == 4:
            metadata = _metadata(job, "radio", filename)
            sx, sy = image.shape[1] / metadata["width"], image.shape[0] / metadata["height"]
            outline = [[x * sx, y * sy] for x, y in outline]
        proposal = None
        if not outline:
            # A small gallery of this session's confirmed radios is safer than
            # assuming every case/gimbal variant matches a catalogue photograph.
            references = [
                (name, config) for name, config in job["calibrations"].items()
                if name != filename and not config.get("skip_motion") and not config.get("exclude")
                and len(config.get("radio_outline", [])) == 4
                and all(side in config for side in ("left_gimbal", "right_gimbal"))
            ][:3]
            profile = job.get("radio_profile")
            if profile:
                try:
                    source = _input_video(job, "radio", profile["name"])
                    if [source.stat().st_size, source.stat().st_mtime_ns] == profile["signature"]:
                        references = [(profile["name"], profile["calibration"])] + [r for r in references if r[0] != profile["name"] and r[1].get("radio_orientation_confirmed")]
                        references = references[:3]
                except (HTTPException, OSError, KeyError):
                    pass
            for reference_name, config in references:
                try:
                    reference_response = calibration_frame(job_id, "radio", reference_name, config["reference_time"])
                    reference = cv2.imdecode(np.frombuffer(reference_response.body, np.uint8), cv2.IMREAD_GRAYSCALE)
                    if reference is None:
                        continue
                    info = _metadata(job, "radio", reference_name)
                    sx, sy = reference.shape[1] / info["width"], reference.shape[0] / info["height"]
                    reference_outline = [[x * sx, y * sy] for x, y in config["radio_outline"]]
                    regions = {
                        side: {key: value * (sx if key.endswith("x") or key in {"x", "width"} else sy)
                               if key in {"x", "y", "width", "height", "center_x", "center_y", "tip_x", "tip_y"}
                               else value * min(sx, sy) if key == "tip_radius" else value
                               for key, value in config[side].items()}
                        for side in ("left_gimbal", "right_gimbal")
                    }
                    candidate = suggest_from_reference(reference, image, reference_outline, regions)
                    if candidate is not None:
                        if config.get("radio_orientation_confirmed"):
                            guessed = rotation_from_regions(candidate["regions"])["rotation_deg"]
                            if guessed is not None:
                                candidate["radio_rotation_deg"] = guessed
                        proposal = {**candidate, "reference_clip": reference_name}
                        break
                except (HTTPException, OSError, ValueError, KeyError, cv2.error, subprocess.SubprocessError):
                    # An unreadable/deleted reference must not block this clip.
                    continue
        if proposal is None:
            choice = request.get("radio_model", (job.get("radio_profile") or {}).get("model_id", "auto"))
            if choice not in {"auto", "other", *(m["id"] for m in RADIO_MODELS)}:
                raise ValueError("Choose a listed radio model, Auto-detect or Other")
            if request.get("suggest_orientation", False):
                proposal = suggest_radio_orientation(image, outline, choice, preferred_rotation=rotation, allow_uncertain=True)
            else:
                proposal = suggest_upright_regions(image, outline, rotation, choice, allow_uncertain=True)
                proposal["radio_rotation_deg"] = rotation
        for region in proposal["regions"].values():
            if "tip_x" not in region:
                tip = suggest_visible_tip(image, region)
                if tip is not None:
                    region.update(tip)
        proposal["missing_tips"] = [side for side, region in proposal["regions"].items() if "tip_x" not in region]
    except (ValueError, OSError, cv2.error, subprocess.SubprocessError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"frame_width": int(image.shape[1]), "frame_height": int(image.shape[0]), **proposal}


@app.post("/api/jobs/{job_id}/calibration/{filename}/check")
def calibration_check(job_id: str, filename: str, draft: dict | None = Body(default=None)):
    import cv2
    import numpy as np

    from fpv_matcher.radio.stick_tracker import iter_sticks

    job = get_job(job_id)
    config = draft if draft is not None else job["calibrations"].get(filename)
    if not config or config.get("skip_motion") or config.get("exclude"):
        raise HTTPException(400, "Mark the gimbals and tips before checking tracking")
    if job["status"] in {"running", "queued"}:
        raise HTTPException(409, "Tracking checks are available before or after analysis")
    path = _input_video(job, "radio", filename)
    try:
        info = _metadata(job, "radio", filename)
        config = validate_calibration(config, (info["width"], info["height"]))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    frames, traces = [], []
    try:
        iterator = iter_sticks(str(path), 10, config, duration=3)
        try:
            for index, (timestamp, values, gray, trackers) in enumerate(iterator):
                traces.append({"time": timestamp, **values})
                if index % 10 == 0 or index == 29:
                    image = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
                    for tracker in trackers:
                        tracker.annotate(image)
                    cv2.putText(
                        image,
                        f"{timestamp:.2f}s | L {values['yaw_quality']:.2f} R {values['roll_quality']:.2f}",
                        (12, 26),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6,
                        (255, 220, 100),
                        2,
                    )
                    width = 560
                    image = cv2.resize(image, (width, max(1, round(gray.shape[0] * width / gray.shape[1]))))
                    frames.append(image)
        finally:
            iterator.close()
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(422, str(exc)) from exc
    if not frames:
        raise HTTPException(422, "No tracking frames available")
    directory = jobs.job_dir(job_id) / "diagnostics"
    directory.mkdir(exist_ok=True)
    destination = directory / (hashlib.sha256(filename.encode()).hexdigest() + ".jpg")
    cv2.imwrite(str(destination), np.concatenate(frames, axis=0))
    quality = float(np.mean([row["quality"] for row in traces]))
    outline_rows = [row for row in traces if "radio_orientation_quality" in row]
    outline_quality = (
        float(np.mean([row["radio_orientation_quality"] for row in outline_rows])) if outline_rows else None
    )
    outline_span = None
    if outline_rows:
        angles = np.unwrap(np.radians([row["radio_orientation_deg"] for row in outline_rows]))
        outline_span = float(np.degrees(np.max(angles) - np.min(angles)))
    return {
        "quality": quality,
        "radio_orientation_quality": outline_quality,
        "radio_orientation_span_degrees": outline_span,
        "trace": traces,
        "image_url": f"/api/jobs/{job_id}/calibration/{quote(filename, safe='')}/image?v={time.time()}",
        "message": "Check that the markers follow the actual tips throughout."
        if quality >= 0.4
        else "Tracking is weak. Adjust tip selection, regions or use audio-only matching for this clip.",
    }


@app.get("/api/jobs/{job_id}/calibration/{filename}/image")
def calibration_image(job_id, filename):
    job = get_job(job_id)
    _record(job, "radio", filename)
    path = jobs.job_dir(job_id) / "diagnostics" / (hashlib.sha256(filename.encode()).hexdigest() + ".jpg")
    if not path.exists():
        raise HTTPException(404, "Run a tracking check first")
    return FileResponse(path)


def _execute(key, command, log, timeout=24 * 3600, on_progress=None):
    if key in CANCELLED:
        raise RuntimeError("Cancelled")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, **_worker_process_options())
        with PROCESS_LOCK:
            PROCESSES[key] = process
        try:
            if key in CANCELLED:
                _stop_process(key)
            if on_progress is None:
                code = process.wait(timeout=timeout)
            else:
                started = time.monotonic()
                position, pending = 0, ""
                while True:
                    try:
                        code = process.wait(timeout=.5)
                        break
                    except subprocess.TimeoutExpired:
                        if key in CANCELLED:
                            _stop_process(key)
                        if time.monotonic() - started > timeout:
                            raise subprocess.TimeoutExpired(command, timeout)
                        with log.open(encoding="utf-8", errors="replace") as reader:
                            reader.seek(position); pending += reader.read(); position = reader.tell()
                        lines = pending.split("\n"); pending = lines.pop()
                        for line in lines:
                            if line.startswith("out_time_us="):
                                try:
                                    on_progress(float(line.split("=", 1)[1]) / 1e6)
                                except ValueError:
                                    pass
            if key in CANCELLED:
                raise RuntimeError("Cancelled")
            if code:
                raise RuntimeError(
                    log.read_text(encoding="utf-8", errors="replace")[-2000:] or f"Process exited {code}"
                )
        except BaseException:
            if process.poll() is None:
                _stop_process(key)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        _stop_process(key)
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise
        finally:
            with PROCESS_LOCK:
                PROCESSES.pop(key, None)


@app.post("/api/jobs/{job_id}/run")
def run_job(job_id: str, settings: dict = Body(default={})):
    job = get_job(job_id)
    if any(not records for records in job["inputs"].values()):
        raise HTTPException(400, "Include at least one FPV and one StickCam recording; restore ignored clips in Video lists & match scope")
    if job["status"] in {"running", "queued", "cancelling"}:
        raise HTTPException(409, "This analysis is already active")
    if any(task.get("kind") in {"stick-derivation", "fpv-preparation"} and task["status"] in {"queued", "running"} for task in job.get("charts", {}).values()):
        raise HTTPException(409, "Motion preparation is active; wait for the saved data before matching")
    if any(item["uploaded"] != item["size"] for records in job["inputs"].values() for item in records):
        raise HTTPException(409, "Finish the uploads first")
    options = {
        **job["settings"],
        "matching_strategy": job["settings"].get("matching_strategy", "selected"),
        "embedded_seed_enabled":job["settings"].get("embedded_seed_enabled",True),
        "seed_spot_checks":job["settings"].get("seed_spot_checks",2),
        "rate_profile": job["settings"].get("rate_profile"),
        "match_target_kind": "all",
        "match_target_name": "",
        "match_target_names": [],
        "stop_on_confident_match": True,
        "allow_unconfigured_radio": False,
        **{
            k: v
            for k, v in settings.items()
            if k in job["settings"]
            or k in {"embedded_seed_enabled","seed_spot_checks","audio_boundary", "audio_adaptive", "audio_retry_limit", "rate_profile", "match_target_kind", "match_target_name", "match_target_names", "stop_on_confident_match", "allow_unconfigured_radio", "motion_source", "camera_uptilt_deg", "imu_orientation", "gyro_video_offset", "decode_mode", "tracking_backend", "evidence_sources", "stick_sampling_ratio", "matching_strategy", "match_sample_percent", "fpv_sampling_mode", "fpv_visual_fps", "stick_mode", "visual_camera"}
        },
    }
    try:
        for key in ("audio_boundary", "audio_retry_limit"):
            options[key] = int(options.get(key, 30 if key == "audio_boundary" else 120))
            if options[key] not in {30, 60, 120, 300}:
                raise ValueError("Audio sampling limits must be 30, 60, 120 or 300 seconds")
        options["audio_adaptive"] = options.get("audio_adaptive", True)
        if not isinstance(options["audio_adaptive"], bool):
            raise ValueError("Adaptive audio must be true or false")
        if options["matching_strategy"] not in {"automatic", "selected", "telemetry_seed"}:
            raise ValueError("Choose automatic staged matching or selected evidence")
        sample_percent = options.get("match_sample_percent", 100)
        if isinstance(sample_percent, bool) or str(sample_percent) not in {"10", "25", "50", "100"}:
            raise ValueError("Quick Match sample must be 10%, 25%, 50% or all recordings")
        options["match_sample_percent"] = int(sample_percent)
        automatic = options["matching_strategy"] != "selected"
        if not isinstance(options["embedded_seed_enabled"],bool) or not isinstance(options["seed_spot_checks"],int) or isinstance(options["seed_spot_checks"],bool) or options["seed_spot_checks"] not in {0,1,2,3}:
            raise ValueError("Choose automatic embedded seeds and zero to three spot checks")
        if automatic:
            options["allow_unconfigured_radio"] = True
        options["stick_sampling_ratio"] = validate_ratio(options.get("stick_sampling_ratio"))
        options["fpv_sampling_mode"] = options.get("fpv_sampling_mode", "auto")
        from fpv_matcher.radio.modes import validate_mode
        options["stick_mode"] = validate_mode(options.get("stick_mode", 2))
        options["visual_camera"] = validate_visual_camera(options.get("visual_camera"))
        options["fpv_visual_fps"] = _extraction_fps(options.get("fpv_visual_fps", 30))
        visual_fps(options["fpv_sampling_mode"], options["fpv_visual_fps"], 30)
        for key in ("sample_fps", "match_fps", "min_overlap", "minimum_score"):
            options[key] = float(options[key])
            if not math.isfinite(options[key]):
                raise ValueError("Settings must be finite")
        options["workers"] = int(options["workers"])
        if (
            not 1 <= options["sample_fps"] <= 30
            or not 1 <= options["match_fps"] <= options["sample_fps"]
            or not 1 <= options["workers"] <= 4
            or not 1 <= options["min_overlap"] <= 3600
            or not 0 <= options["minimum_score"] <= 1
        ):
            raise ValueError("Invalid analysis settings")
        if not isinstance(options["audio_enabled"], bool):
            raise ValueError("Audio setting must be true or false")
        if not isinstance(options["allow_unconfigured_radio"], bool):
            raise ValueError("Audio-only StickCam setting must be true or false")
        if options["match_target_kind"] not in {"all", "fpv", "radio"}:
            raise ValueError("Choose all recordings, one FPV target, or one StickCam target")
        if not isinstance(options.get("stop_on_confident_match", True), bool):
            raise ValueError("Early review setting must be true or false")
        if options["match_target_kind"] != "all":
            kind = options["match_target_kind"]
            names = options.get("match_target_names") or ([options["match_target_name"]] if options["match_target_name"] else [])
            if not isinstance(names, list) or not names or any(not isinstance(n, str) for n in names) or len(set(names)) != len(names) or not set(names) <= {r["name"] for r in job["inputs"][kind]}:
                raise ValueError("Choose one or more distinct target videos from this session")
            options["match_target_names"] = names
        elif options["match_target_name"]:
            raise ValueError("A target video is only used for single-video matching")
        # Old clients still control the legacy audio/source settings. Explicit
        # source selections take precedence when provided by the new UI.
        selected_sources = options.get("evidence_sources")
        if "evidence_sources" not in settings and ("motion_source" in settings or "audio_enabled" in settings):
            selected_sources = None
        options["evidence_sources"] = normalize_sources(selected_sources, options.get("motion_source", "auto"), options["audio_enabled"])
        if options["matching_strategy"] == "telemetry_seed":
            options["evidence_sources"] = normalize_sources({"fpv_gyro":True,"fpv_visual":False,"stick_inputs":False,
                "gimbal_activity":True,"movement_rhythm":False,"fpv_audio":False,"stickcam_audio":False,"filename_timestamps":False})
        use_motion = motion_enabled(options["evidence_sources"])
        options["audio_enabled"] = audio_enabled_pair(options["evidence_sources"])
        if options["evidence_sources"]["movement_rhythm"] and not precise_motion_enabled(options["evidence_sources"]):
            raise ValueError("Derived-input rhythm requires StickCam derived inputs and an FPV motion source. "
                             "Choose tip-free gimbal movement to skip exact tip tracking.")
        options["motion_source"] = ("auto" if options["evidence_sources"]["fpv_gyro"] and options["evidence_sources"]["fpv_visual"] else "gyro" if options["evidence_sources"]["fpv_gyro"] else "visual")
        if use_motion and options["allow_unconfigured_radio"] and not options["audio_enabled"] and not automatic:
            raise ValueError("Audio-only StickCam matching requires boundary audio")
        options["rate_profile"] = validate_rate_profile(options["rate_profile"])
        options["motion_source"], options["camera_uptilt_deg"], options["imu_orientation"], options["gyro_video_offset"] = validate_motion_settings(options.get("motion_source", "auto"), options.get("camera_uptilt_deg", 0), options.get("imu_orientation", "zXy"), options.get("gyro_video_offset", 0))
        if options.get("decode_mode", "auto") not in {"auto", "cpu", "cuda"}:
            raise ValueError("Choose automatic, CPU or CUDA decoding")
        options.setdefault("decode_mode", "auto")
        options["tracking_backend"] = _tracking_mode(options.get("tracking_backend", "auto"))
        radio_records = job["inputs"]["radio"]
        if options["match_target_kind"] == "radio":
            radio_records = [r for r in radio_records if r["name"] in options["match_target_names"]]
        for record in radio_records:
            calibration = job["calibrations"].get(record["name"])
            if not use_motion:
                continue
            if not calibration:
                if options["allow_unconfigured_radio"]:
                    continue
                raise ValueError(f"Calibrate {record['name']} or enable audio-only matching for unconfigured StickCam")
            if calibration.get("exclude"):
                continue
            if calibration.get("skip_motion"):
                if not options["audio_enabled"] and not automatic:
                    raise ValueError("Audio-only clips require boundary audio to be enabled")
            else:
                if automatic:
                    continue  # The staged worker reports per-source/calibration failures without aborting the session.
                info = _metadata(job, "radio", record["name"])
                tracking_fps(options["sample_fps"], options["stick_sampling_ratio"], info.get("fps"))
                try:
                    validate_calibration(calibration, (info["width"], info["height"]))
                except ValueError:
                    if automatic:
                        continue
                    raise
                if options["evidence_sources"]["gimbal_activity"]:
                    activity_specification(calibration)
                if precise_motion_enabled(options["evidence_sources"]) and any("tip_x" not in calibration[side] for side in ("left_gimbal", "right_gimbal")):
                    raise ValueError(f"Click both stick tips in {record['name']} before analysis")
        if all(job["calibrations"].get(record["name"], {}).get("exclude") for record in radio_records):
            raise ValueError("Keep at least one StickCam recording in this analysis")
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    identities = {kind: {} for kind in job["inputs"]}
    for kind, records in job["inputs"].items():
        for item in records:
            try:
                stat = Path(item["path"]).stat()
            except OSError:
                if automatic:
                    continue
                raise HTTPException(409, f"{item['name']} is unavailable. Restore its source drive/share before matching.") from None
            if item.get("sha256") and item.get("upload_signature") == [stat.st_size, stat.st_mtime_ns]:
                identities[kind][item["name"]] = item["sha256"]
    current_calibrations = {}
    for name, calibration in job["calibrations"].items():
        expected = job.get("calibration_signatures", {}).get(name)
        try:
            source = _input_video(job, "radio", name)
            valid = not expected or expected == [source.stat().st_size, source.stat().st_mtime_ns]
        except (OSError, HTTPException):
            valid = False
        if valid or calibration.get("exclude") or calibration.get("skip_motion"):
            current_calibrations[name] = calibration
    fpv_records = job["inputs"]["fpv"]
    if options["match_target_kind"] == "fpv":
        fpv_records = [r for r in fpv_records if r["name"] in options["match_target_names"]]
    active_radio = [r for r in radio_records if not current_calibrations.get(r["name"], {}).get("exclude")]
    # Unknown metadata still permits a cheap scan. Known audio-free feeds plus
    # zero usable radio calibrations cannot produce a content comparison.
    audio_possible = options["audio_enabled"] and all(
        any(record.get("metadata", {}).get("has_audio") is not False for record in records)
        for records in (active_radio, fpv_records))
    motion_ready = False
    if use_motion:
        for record in active_radio:
            calibration = current_calibrations.get(record["name"], {})
            if not calibration or calibration.get("skip_motion"):
                continue
            try:
                checked = validate_calibration(calibration)
                precise_ready = precise_motion_enabled(options["evidence_sources"]) and all(
                    "tip_x" in checked[side] for side in ("left_gimbal", "right_gimbal"))
                activity_ready = False
                if options["evidence_sources"]["gimbal_activity"]:
                    try:
                        activity_specification(checked)
                        activity_ready = True
                    except ValueError:
                        pass
                motion_ready |= precise_ready or activity_ready
            except (ValueError, TypeError, KeyError):
                continue
    from fpv_matcher.matching.quick_seed import enabled as embedded_seed_enabled
    if not audio_possible and not motion_ready and not embedded_seed_enabled(options,job["inputs"]):
        raise HTTPException(409, "No selected matching method is ready. Audio is unavailable or disabled for one feed. "
                            "Save a radio outline and both gimbal regions for at least one StickCam clip to use tip-free movement; "
                            "derived-input rhythm additionally needs calibrated stick tips. No match or offset has been estimated.")
    modified_times = {kind: {item["name"]: _modified_time_status(job, item) for item in records}
                      for kind, records in job["inputs"].items()}
    config = {"clips": current_calibrations, "identities": identities, "modified_times": modified_times,
              "radio_model":(job.get("radio_profile") or {}).get("model_id","auto"),
              "clock_settings": job.get("clock_settings", {}), "no_counterpart": job.get("no_counterpart", []),
              "fpv_recorders": {r["name"]: r.get("fpv_recorder", "") for r in job["inputs"]["fpv"]}}
    config["rejected_pairs"] = [d for d in job.get("rejected_pairs",{}).values() if _decision_current(job,d)]
    config["clock_rejections"] = [d for d in job.get("clock_rejections",{}).values() if _decision_current(job,d)]
    try:
        config["confirmed_clocks"] = _effective_results(job).get("learned_clocks", [])
    except HTTPException:
        config["confirmed_clocks"] = []
    base = jobs.job_dir(job_id)
    (base / "results" / "early.json").unlink(missing_ok=True)
    (base / "results" / "progress.json").unlink(missing_ok=True)
    atomic_json(base / "radio_rois.json", config)
    # A list of paths supports local files without copying/linking multi-gigabyte media.
    atomic_json(
        base / "inputs.json",
        {kind: [item["path"] for item in records] for kind, records in job["inputs"].items()},
    )
    run_id = uuid.uuid4().hex[:10]
    key = "analysis:" + job_id + ":" + run_id
    with PROCESS_LOCK:
        CANCELLED.discard(key)

    def queue(current):
        if current["status"] in {"queued", "running", "cancelling"}:
            raise HTTPException(409, "This analysis is already active")
        if {kind:[r["name"] for r in rows] for kind,rows in active_job(current)["inputs"].items()} != {kind:[r["name"] for r in rows] for kind,rows in job["inputs"].items()}:
            raise HTTPException(409, "Session recording scope changed; refresh before starting matching")
        current.update(status="queued", message="Queued for analysis", settings=options, settings_defaults_version=2, run_id=run_id)

    jobs.update(job_id, queue)
    ANALYSIS_POOL.submit(_run_job, job_id, run_id)
    return {"status": "queued"}


def _run_job(job_id, run_id):
    try:
        with jobs.LOCK:
            job = get_job(job_id)
            if job["status"] != "queued" or job.get("run_id") != run_id:
                return
            jobs.update(job_id, lambda current: current.update(status="running", message="Extracting fingerprints"))
    except SaveError as exc:
        jobs.failed_task(job_id, str(exc))
        return
    base = jobs.job_dir(job_id)
    command = _worker_command("fpv_matcher.worker", base, jobs.DATA / "cache")
    try:
        previous_results = _results(job)
    except HTTPException:
        previous_results = None
    try:
        _execute("analysis:" + job_id + ":" + run_id, command, base / "results" / "analysis.log",
                 timeout=min(72 * 3600, max(24 * 3600, len(job["inputs"]["radio"]) * 2 * 3600)))
        if previous_results:
            from fpv_matcher.matching.merge import retain_previous
            result = retain_previous(previous_results, _results(job), job)
            atomic_json(base / "results" / "results.json", result)
        prepared = {}
        if _results(job).get("workflow",{}).get("strategy") == "telemetry_seed":
            try:
                prepared = json.loads((base / "results" / "seed-preparation.json").read_text())
            except (OSError,ValueError):
                pass
        def complete(current):
            if current["status"] != "running" or current.get("run_id") != run_id:
                return
            for item in prepared.get("clips",[]):
                name = item["name"]
                existing=current['calibrations'].get(name)
                stale=existing and current.get('calibration_signatures',{}).get(name) not in (None,item['signature'])
                if not item.get("automatic") or not item.get("calibration") or (existing and not stale):
                    continue
                try:
                    source = _input_video(current,"radio",name)
                    if [source.stat().st_size,source.stat().st_mtime_ns] != item["signature"]:
                        continue
                    current["calibrations"][name] = item["calibration"]
                    current.setdefault("calibration_signatures",{})[name] = item["signature"]
                    current.setdefault("calibration_reviews",{})[name] = {"status":"automatic_movement","source":"embedded_seed",
                        "message":item.get("message","Automatically observed radio regions; review them before precise stick derivation."),
                        "observed_seconds":item.get("observed_seconds",0),"updated_at":time.time()}
                except (OSError,HTTPException):
                    continue
            current.update(status="complete",message="Analysis complete",last_successful_at=time.time())
        jobs.update(
            job_id,
            complete,
        )
    except Exception as exc:
        status = "cancelled" if "analysis:" + job_id + ":" + run_id in CANCELLED else "failed"
        detail = (
            "Analysis cancelled; cached fingerprints can be reused."
            if status == "cancelled"
            else f"Analysis failed: {exc}"
        )
        latest = get_job(job_id)
        if latest["status"] != "interrupted" and latest.get("run_id") == run_id:
            jobs.failed_task(job_id, detail, status=status)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id):
    job = get_job(job_id)
    if job["status"] not in {"queued", "running"}:
        raise HTTPException(409, "No active analysis to cancel")
    _stop_process("analysis:" + job_id + ":" + job["run_id"])
    jobs.update(
        job_id,
        lambda current: current.update(
            status="cancelled", message="Analysis cancelled; rerun to reuse cached fingerprints."
        ),
    )
    return {"status": "cancelled"}


@app.post("/api/jobs/{job_id}/early-decision")
def decide_early(job_id: str, body: dict = Body(...)):
    with jobs.LOCK:
        job = get_job(job_id)
        pending_path = jobs.job_dir(job_id) / "results" / "early.json"
        if job["status"] != "running" or not pending_path.exists():
            raise HTTPException(409, "No early proposal is waiting")
        pending = json.loads(pending_path.read_text())
        if pending.get("status") != "pending" or body.get("nonce") != pending["nonce"] or body.get("action") not in {"confirm", "continue"}:
            raise HTTPException(409, "That proposal is no longer current")
        decision = pending_path.parent / f"decision-{pending['nonce']}.json"
        if decision.exists():
            raise HTTPException(409, "That proposal already has a decision")
        if body["action"] == "confirm":
            row = pending["candidate"]
            if pending.get("source_signatures") != _decision_signatures(job, row["radio_video"], row["fpv_video"]):
                raise HTTPException(409, "A source changed while this proposal was waiting; continue scanning and rerun matching")
            previous = job.get("pair_overrides", {}).get(row["fpv_video"])
            if previous and previous["radio_video"] != row["radio_video"] and _decision_current(job, previous) and not previous.get("rejected"):
                raise HTTPException(409, "This FPV file already has a reviewed owner. Continue scanning, then use explicit owner replacement in pair review.")
            def confirm(current):
                current.setdefault("pair_overrides", {})[row["fpv_video"]] = {
                    "radio_video": row["radio_video"], "fpv_video": row["fpv_video"],
                    "offset_seconds": row["offset_seconds"], "source_signatures": _decision_signatures(job, row["radio_video"], row["fpv_video"]), "updated_at": time.time()}
            jobs.update(job_id, confirm)
        atomic_json(decision, {"action": body["action"]})
    return {"status": "accepted"}


def _results(job):
    path = jobs.job_dir(job["id"]) / "results" / "results.json"
    if not path.is_file():
        raise HTTPException(409, "No completed matching results yet")
    return json.loads(path.read_text(encoding="utf-8"))


def _signature_revision(signature):
    return hashlib.sha256(json.dumps(signature, separators=(",", ":")).encode()).hexdigest()


def _decision_signatures(job, radio_name, fpv_name):
    return {kind: [p.stat().st_size, p.stat().st_mtime_ns]
            for kind, p in (("radio", _input_video(job, "radio", radio_name)),
                            ("fpv", _input_video(job, "fpv", fpv_name)))}


def _decision_current(job, decision):
    # Legacy decisions lack evidence binding their offset to these source bytes.
    # Keep them for review but never turn them into a current confirmed alignment.
    expected = decision.get("source_signatures")
    if not expected or not decision.get("fpv_video"):
        return False
    try:
        return expected == _decision_signatures(job, decision["radio_video"], decision["fpv_video"])
    except (OSError, HTTPException, KeyError):
        return False


def _unassessed_results(job):
    # Manual browsing and saved visual alignments do not require signal extraction.
    return {"rows": [], "clips": {"radio": [], "fpv": []}, "manual_only": True,
            "unmatched_radio": [], "unmatched_fpv": [], "errors": [], "clock": {},
            "match_scope": {"kind": "all", "sample_percent": 100, "sampled_fpv": [], "sampled_radio": []}}


def _manual_pair_row(radio_name, fpv_name, offset, rinfo, finfo):
    overlap = max(0., min(rinfo["duration"] - max(0., offset), finfo["duration"] - max(0., -offset)))
    row = clocks.date_row({"radio_video": radio_name, "fpv_video": fpv_name, "offset_seconds": offset,
                           "overlap_seconds": overlap, "clock_source": "Manual visual alignment", "support": []})
    row.update(manual_pair=True, timing_only=False, evidence="Manual visible-event alignment · no automated score",
               review_reasons=["Independently check shared visible events and refine the alignment"])
    return row


def _effective_results(job):
    job = active_job(job)
    results = _results(job) if (jobs.job_dir(job["id"]) / "results" / "results.json").is_file() else _unassessed_results(job)
    names = {kind: {r["name"] for r in records} for kind, records in job["inputs"].items()}
    def active_pair(row):
        return row.get("radio_video") in names["radio"] and row.get("fpv_video") in names["fpv"]
    results["rows"] = [r for r in results["rows"] if active_pair(r)]
    for kind in ("fpv", "radio"):
        results["clips"][kind] = [c for c in results["clips"][kind] if c["name"] in names[kind]]
        if "considered_" + kind in results:
            results["considered_" + kind] = [n for n in results["considered_" + kind] if n in names[kind]]
        scope = results.setdefault("match_scope", {})
        scope["total_" + kind] = len(names[kind])
        for prefix in ("sampled_", "deferred_", "target_"):
            key = prefix + kind
            if key in scope:
                scope[key] = [n for n in scope[key] if n in names[kind]]
        workflow = results.get("workflow", {})
        for prefix in ("unscanned_", "unresolved_"):
            key = prefix + kind
            if key in workflow:
                workflow[key] = [n for n in workflow[key] if n in names[kind]]
    scope["timing_suggestions"] = [r for r in scope.get("timing_suggestions", []) if active_pair(r)]
    results["errors"] = [e for e in results.get("errors", []) if e.get("name") in names.get(e.get("kind"), set())]
    workflow = results.get("workflow", {})
    workflow["needs_calibration"] = [n for n in workflow.get("needs_calibration", []) if n in names["radio"]]
    for key in ("unavailable_sources", "gimbal_activity_errors", "skipped_motion"):
        workflow[key] = [e for e in workflow.get(key, []) if e.get("name") in names.get(e.get("kind", "radio"), set())]
    seed_run = workflow.get("seed_search")
    if seed_run:
        if seed_run.get("seed") and not active_pair(seed_run["seed"]):
            seed_run.update(seed=None, seed_ignored=True)
        seed_run["attempts"] = [a for a in seed_run.get("attempts",[]) if a["name"] in names["radio"]]
        seed_run["spot_checks"] = [c for c in seed_run.get("spot_checks",[]) if active_pair(c)]
        seed_run["clocks"] = [c for c in seed_run.get("clocks",[]) if all(active_pair(a) for a in c.get("anchors",[]))]
    results["ignored_inputs"] = {kind: [r["name"] for r in records] for kind, records in job["_ignored_inputs"].items()}
    for radio_name, override in sorted(
        job.get("overrides", {}).items(), key=lambda item: item[1].get("updated_at", 0)
    ):
        if not _decision_current(job, {**override, "radio_video": radio_name}):
            continue
        for row in results["rows"]:
            if row["radio_video"] == radio_name or (
                override.get("fpv_video") and row["fpv_video"] == override["fpv_video"]
            ):
                row["global_assignment"] = False
        if override.get("fpv_video"):
            for row in results["rows"]:
                if row["radio_video"] == radio_name and row["fpv_video"] == override["fpv_video"]:
                    row.update(
                        global_assignment=True,
                        reviewed=True,
                        offset_seconds=override["offset_seconds"],
                        radio_common_start_seconds=max(0.0, override["offset_seconds"]),
                        fpv_common_start_seconds=max(0.0, -override["offset_seconds"]),
                    )
                    rduration = next(
                        c["duration"] for c in results["clips"]["radio"] if c["name"] == radio_name
                    )
                    fduration = next(
                        c["duration"] for c in results["clips"]["fpv"] if c["name"] == override["fpv_video"]
                    )
                    row["overlap_seconds"] = max(
                        0.0,
                        min(
                            rduration - row["radio_common_start_seconds"],
                            fduration - row["fpv_common_start_seconds"],
                        ),
                    )
    additions = ([d for d in job.get("review_decisions", {}).values() if d.get("manual_pair")]
                 + list(job.get("pair_overrides", {}).values()) + list(job.get("rejected_pairs", {}).values()))
    segment_groups = []
    for group in job.get("segment_candidates", {}).values():
        anchor = group["anchor"]
        saved = job.get("pair_overrides", {}).get(anchor["fpv_video"])
        if saved:
            valid = not saved.get("rejected") and saved.get("radio_video") == anchor["radio_video"] and abs(saved["offset_seconds"] - anchor["offset_seconds"]) < 1e-7
        else:
            saved = job.get("overrides", {}).get(anchor["radio_video"], {})
            valid = saved.get("fpv_video") == anchor["fpv_video"] and abs(saved.get("offset_seconds", float("inf")) - anchor["offset_seconds"]) < 1e-7
        if not valid or _layout_key(anchor["radio_video"], anchor["fpv_video"]) in job.get("rejected_pairs", {}):
            continue
        try:
            if any([Path(anchor[k + "_path"]).stat().st_size, Path(anchor[k + "_path"]).stat().st_mtime_ns] != anchor["source_signatures"][k] for k in ("radio", "fpv")):
                continue
        except OSError:
            continue
        segment_groups.append({"anchor": {key: anchor[key] for key in ("radio_video", "fpv_video", "offset_seconds")},
                               "clips": group["clips"], "failures": group.get("failures", []), "task_id": group["task_id"]})
        for candidate in group["clips"]:
            additions.append({"radio_video": candidate["radio_video"], "fpv_video": candidate["fpv_video"],
                              "offset_seconds": candidate["offset_seconds"], "segment": candidate})
    for addition in additions:
        if not active_pair(addition):
            continue
        name, radio_name = addition["fpv_video"], addition["radio_video"]
        row = next((r for r in results["rows"] if r["radio_video"] == radio_name and r["fpv_video"] == name), None)
        if row is None:
            radio_info = addition.get("radio_info")
            if not any(c["name"] == radio_name for c in results["clips"]["radio"]):
                if not radio_info or not _decision_current(job, addition):
                    continue
                results["clips"]["radio"].append(radio_info)
            if addition.get("row"):
                row = dict(addition["row"])
            elif addition.get("segment"):
                candidate = addition["segment"]
                row = {**(results["rows"][0] if results["rows"] else {}), "radio_video": radio_name, "fpv_video": name,
                       "score": 0., "combined_score": 0., "candidate_margin": 0., "peak_margin": 0.,
                       "confidence": "review", "evidence": "Confirmed anchor + filename timing · unverified segment",
                       "motion_included": False, "audio_included": False, "reliable_axes": 0,
                       "correlated_sections": 0, "usable_sections": 0, "section_agreement": 0.,
                       "section_spread": 0., "tracking_coverage": 0., "valid_motion_seconds": 0.,
                       "audio_best_score": 0., "audio_score_at_motion_offset": 0., "audio_fingerprint_strength": 0.,
                       "audio_fingerprint_shared_hashes": 0, "audio_fingerprint_span_seconds": 0., "audio_overlap_seconds": 0.,
                       "axis_details": {}, "alternatives": [], "global_assignment": False, "reviewed": False,
                       "offset_seconds": candidate["offset_seconds"], "overlap_seconds": candidate["overlap_seconds"],
                       "radio_common_start_seconds": candidate["radio_start"], "fpv_common_start_seconds": candidate["fpv_start"],
                       "event_score": 0., "fpv_motion_source": "not_assessed", "timestamp_agreement": None,
                       "audio_best_offset_seconds": None, "audio_fingerprint_offset_seconds": None,
                       "radio_filename_time": format_timestamp(filename_timestamp(radio_name)),
                       "fpv_filename_time": format_timestamp(filename_timestamp(name)),
                       "filename_start_delta_seconds": None if filename_timestamp(radio_name) is None else filename_timestamp(name) - filename_timestamp(radio_name),
                       "inferred_camera_clock_delta_seconds": candidate["clock_delta_seconds"], "clock_consensus_support": 1,
                       "timing_only": True, "audio_first_proposal": False, "motion_deferred": False,
                       "audio_correlated_sections": 0, "audio_usable_sections": 0, "audio_section_agreement": 0.,
                       "audio_independent_sections": 0, "audio_trend_sections": 0,
                       "audio_owner_margin": 0., "review_reasons": ["Filename timing only; verify shared content and offset"]}
            else:
                continue
            results["rows"].append(row)
        if addition.get("manual_pair") and addition.get("row"):
            row.update(addition["row"])
        info = addition.get("fpv_info") or addition.get("segment", {}).get("fpv_info")
        if info and not any(c["name"] == name for c in results["clips"]["fpv"]):
            results["clips"]["fpv"].append(info)
            if "considered_fpv" in results and name not in results["considered_fpv"]:
                results["considered_fpv"].append(name)
    for override in sorted(job.get("pair_overrides", {}).values(), key=lambda o: o.get("updated_at", 0)):
        if not _decision_current(job, override):
            for row in results["rows"]:
                if row["radio_video"] == override["radio_video"] and row["fpv_video"] == override["fpv_video"]:
                    row.update(reviewed=False, review_invalidated=True, saved_offset_seconds=override["offset_seconds"],
                               review_reason="Source changed or legacy decision has no source signature; review again")
            continue
        for row in results["rows"]:
            if row["fpv_video"] != override["fpv_video"]:
                continue
            row["global_assignment"] = False
            row["reviewed"] = False
            row["rejected"] = bool(override.get("rejected"))
            if row["radio_video"] == override["radio_video"] and not override.get("rejected"):
                offset = override["offset_seconds"]
                rd = override.get("radio_info", {}).get("duration") or next(c["duration"] for c in results["clips"]["radio"] if c["name"] == row["radio_video"])
                fd = override.get("fpv_info", {}).get("duration") or next(c["duration"] for c in results["clips"]["fpv"] if c["name"] == row["fpv_video"])
                row.update(global_assignment=True, reviewed=True, offset_seconds=offset,
                           radio_common_start_seconds=max(0., offset), fpv_common_start_seconds=max(0., -offset),
                           overlap_seconds=max(0., min(rd - max(0., offset), fd - max(0., -offset))))
    for rejection in job.get("rejected_pairs", {}).values():
        if not _decision_current(job, rejection):
            continue
        for row in results["rows"]:
            if row["radio_video"] == rejection["radio_video"] and row["fpv_video"] == rejection["fpv_video"]:
                row.update(global_assignment=False, reviewed=False, rejected=True)
    results["segment_candidates"] = segment_groups
    records = {}
    for kind, inputs in job["inputs"].items():
        for record in inputs:
            name = record["name"]
            if kind == "radio" and job.get("calibrations", {}).get(name, {}).get("exclude"):
                continue
            if kind == "fpv" and name in job.get("no_counterpart", []):
                continue
            try:
                source = _input_video(job, kind, name)
                stat = source.stat()
                signature = [stat.st_size, stat.st_mtime_ns]
                metadata = record.get("metadata") if record.get("metadata_signature") == signature else None
                if metadata is None:
                    metadata = next((c for c in results["clips"][kind] if c["name"] == name
                                     and c.get("size") == stat.st_size and c.get("mtime_ns") == stat.st_mtime_ns), None)
                if not metadata or not metadata.get("duration"):
                    continue
                records[kind, name] = {**metadata, "name": name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                                      "source_usable": True, "filename_time": filename_timestamp(name),
                                      "modified_time": _modified_time_status(job, record)["timestamp"],
                                      "clock_group": record.get("fpv_recorder", "") if kind == "fpv" else ""}
            except (OSError, HTTPException):
                continue
    def current_seed(row):
        anchor = row.get("seed_anchor") or {}
        expected = anchor.get("source_signatures",{})
        if not expected or anchor.get("fpv_video") in job.get("no_counterpart",[]):
            return False
        rejected = job.get("rejected_pairs",{}).get(_layout_key(anchor.get("radio_video",""),anchor.get("fpv_video","")))
        if rejected and _decision_current(job,rejected):
            return False
        owner = job.get("pair_overrides",{}).get(anchor.get("fpv_video"))
        if owner and owner.get("radio_video") != anchor.get("radio_video") and _decision_current(job,owner):
            return False
        return all((record := records.get((kind,anchor.get(kind+"_video")))) and
                   [record["size"],record["mtime_ns"]] == expected.get(kind) for kind in ("radio","fpv"))
    results["rows"] = [row for row in results["rows"] if not row.get("provisional_clock") or current_seed(row)]
    scope = results.get("match_scope",{})
    scope["timing_suggestions"] = [row for row in scope.get("timing_suggestions",[]) if not row.get("provisional_clock") or current_seed(row)]
    anchors = []
    for decision in list(job.get("pair_overrides", {}).values()) + [dict(o, radio_video=n) for n, o in job.get("overrides", {}).items()]:
        if (decision.get("fpv_video") and not decision.get("rejected") and _decision_current(job, decision)
                and any(r.get("reviewed") and r["radio_video"] == decision["radio_video"] and r["fpv_video"] == decision["fpv_video"] for r in results["rows"])):
            checkpoints = job.get("sync_points", {}).get(_layout_key(decision["radio_video"], decision["fpv_video"]), {})
            points = checkpoints.get("points", []) if checkpoints and _decision_current(job, checkpoints) else []
            if any(abs(p["radio_time"] - p["fpv_time"] - decision["offset_seconds"]) > .15 for p in points):
                continue
            anchors.append(decision)
    for row in results["rows"]:
        saved = job.get("review_decisions", {}).get(_layout_key(row["radio_video"], row["fpv_video"]), {})
        if saved and _decision_current(job, saved):
            row["review_state"] = saved["state"]
    rejections = [d for d in job.get("rejected_pairs",{}).values() if _decision_current(job,d)]
    clock_rejections = [dict(d,id=key) for key,d in job.get("clock_rejections",{}).items() if _decision_current(job,d)]
    clocks.enrich(results, records, anchors, job.get("clock_settings"), job["settings"].get("min_overlap", 2), rejections, clock_rejections)
    for row in results["rows"]:
        row["pair_id"] = _layout_key(row["radio_video"], row["fpv_video"])
        saved_review = job.get("review_decisions", {}).get(row["pair_id"], {})
        row["review_state"] = ("confirmed" if row.get("reviewed") else "rejected" if row.get("rejected") else
                               saved_review.get("state", "candidate") if saved_review and _decision_current(job, saved_review) else "candidate")
        annotate_alignment(row, records.get(("radio", row["radio_video"]), {}).get("duration"),
                           records.get(("fpv", row["fpv_video"]), {}).get("duration"))
    results["alignment_summary"] = {
        "available": sum(row["alignment_available"] for row in results["rows"]),
        "unavailable": sum(not row["alignment_available"] for row in results["rows"]),
        "message": "Rows without matching evidence have no estimated offset; they are not proposed pairs."}
    results["rhythm_proposals"] = [{"radio_video": row["radio_video"], "fpv_video": row["fpv_video"], **row["rhythm"]}
        for row in results["rows"] if (row.get("rhythm") or {}).get("suggested") if not row.get("reviewed") and not row.get("rejected")
        and row.get("review_state") != "later" and ("radio", row["radio_video"]) in records and ("fpv", row["fpv_video"]) in records]
    results["rhythm_proposals"].sort(key=lambda p: -p["score"])
    results["gimbal_activity_proposals"] = [{"radio_video": row["radio_video"], "fpv_video": row["fpv_video"], **row["gimbal_activity"]}
        for row in results["rows"] if (row.get("gimbal_activity") or {}).get("suggested") and not row.get("reviewed") and not row.get("rejected")
        and row.get("review_state") != "later" and ("radio", row["radio_video"]) in records and ("fpv", row["fpv_video"]) in records]
    results["gimbal_activity_proposals"].sort(key=lambda p: -p["score"])
    document = _review_document(job, results)
    results["review_summary"] = pair_review.summary(document)
    results["flights"] = pair_review.flights(document)
    assigned = [r for r in results["rows"] if r["global_assignment"]]
    results["unmatched_radio"] = [
        r["name"]
        for r in job["inputs"]["radio"]
        if r["name"] in results.get("considered_radio", [item["name"] for item in job["inputs"]["radio"]])
        if r["name"] not in results.get("excluded_radio", [])
        and not any(p["radio_video"] == r["name"] for p in assigned)
    ]
    results["unmatched_fpv"] = [
        r["name"]
        for r in job["inputs"]["fpv"]
        if r["name"] in results.get("considered_fpv", [item["name"] for item in job["inputs"]["fpv"]])
        and not any(p["fpv_video"] == r["name"] for p in assigned)
    ]
    if "workflow" in results:
        results["workflow"].update(unresolved_fpv=results["unmatched_fpv"], unresolved_radio=results["unmatched_radio"])
    return results


def _review_document(job, results):
    videos = [{"id": kind + ":" + record["name"], "kind": "stick" if kind == "radio" else "fpv", "name": record["name"],
               "metadata": record.get("metadata", {}), "no_counterpart": record["name"] in job.get("no_counterpart", []),
               "match_checked": record["name"] in results.get("match_scope", {}).get("sampled_" + kind, [])}
              for kind, records in job["inputs"].items() for record in records]
    pairs = [{"id": _layout_key(row["radio_video"], row["fpv_video"]), "fpv": "fpv:" + row["fpv_video"], "stick": "radio:" + row["radio_video"],
              "offset": row["alignment_offset_seconds"], "confirmed": row.get("reviewed", False), "review_state": row.get("review_state", "candidate"),
              "radio_start": max(0., row["alignment_offset_seconds"]), "overlap_duration": row.get("alignment_overlap_seconds") or row["overlap_seconds"],
              "method": "timestamp" if row.get("timing_only") else "content", "score": row.get("alignment_score", 0),
              "confidence": row.get("confidence", "Candidate")}
             for row in results["rows"] if row.get("alignment_available") and (row.get("alignment_suggested") or
             row.get("review_state") in {"rejected", "later"} or row.get("alignment_score", 0) >= .4)]
    return {"videos": videos, "pairs": pairs}


@app.get("/api/jobs/{job_id}/matches")
def matches(job_id):
    return _effective_results(get_job(job_id))


def _pair_info(job, radio_name, fpv_name, offset=None, allow_manual=False):
    results = _effective_results(job)
    row = next(
        (r for r in results["rows"] if r["radio_video"] == radio_name and r["fpv_video"] == fpv_name), None
    )
    if offset is not None and not allow_manual and not (row or {}).get("reviewed"):
        prediction = next((p for p in results.get("match_scope",{}).get("timing_suggestions",[])
                           if p["radio_video"]==radio_name and p["fpv_video"]==fpv_name
                           and abs(p["offset_seconds"]-float(offset))<.001),None)
        if prediction:
            row = clocks.date_row(prediction)
            annotate_alignment(row)
    if row is None and not allow_manual:
        raise HTTPException(404, "This pair has not been analyzed. Open Find a pair manually to align any two recordings.")
    manual = allow_manual or bool(row.get("manual_pair") if row else False)
    if not manual and offset is None and not row.get("alignment_available"):
        raise HTTPException(409, row.get("alignment_reason", "No matching alignment was found. Find a pair manually."))
    rinfo, finfo = _metadata(job, "radio", radio_name), _metadata(job, "fpv", fpv_name)
    if allow_manual:
        try:
            value = float(offset if offset is not None else 0)
            if isinstance(offset, bool) or not math.isfinite(value):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(400, "Offset must be a finite number") from None
        previous_row = row
        row = _manual_pair_row(radio_name, fpv_name, value, rinfo, finfo)
        if previous_row and previous_row.get("reviewed") and abs(previous_row["offset_seconds"] - value) < 1e-7:
            row["reviewed"] = True
    elif manual:
        saved = (job.get("pair_overrides", {}).get(fpv_name) or job.get("rejected_pairs", {}).get(_layout_key(radio_name, fpv_name))
                 or job.get("review_decisions", {}).get(_layout_key(radio_name, fpv_name)))
        if not saved or not _decision_current(job, saved):
            raise HTTPException(409, "This manual pairing's source changed. Open Find a pair manually and check the current videos again.")
    else:
        for kind, name in (("radio", radio_name), ("fpv", fpv_name)):
            original = next((c for c in results["clips"][kind] if c["name"] == name), None)
            if original is None or original.get("source_usable") is False:
                raise HTTPException(409, "Restore this source and rerun matching before previewing or confirming it")
            stat = _input_video(job, kind, name).stat()
            if stat.st_size != original["size"] or (original.get("mtime_ns") and stat.st_mtime_ns != original["mtime_ns"]):
                raise HTTPException(409, f"{name} changed since analysis. Run analysis again or manually review the current videos.")
    try:
        offset = float((row["offset_seconds"] if manual else row["alignment_offset_seconds"]) if offset is None else offset)
        if not math.isfinite(offset):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, "Offset must be a finite number") from None
    rstart, fstart = max(0.0, offset), max(0.0, -offset)
    duration = min(rinfo["duration"] - rstart, finfo["duration"] - fstart)
    if duration <= 0:
        raise HTTPException(422, "This offset has no overlapping interval")
    points = job.get("sync_points", {}).get(_layout_key(radio_name, fpv_name), {})
    points = points.get("points", []) if points and _decision_current(job, points) else []
    signatures = _decision_signatures(job, radio_name, fpv_name)
    if signatures["radio"] != rinfo["source_signature"] or signatures["fpv"] != finfo["source_signature"]:
        raise HTTPException(409, "A source changed while reading its metadata. Reload the previews.")
    return {
        "radio_video": radio_name,
        "fpv_video": fpv_name,
        "radio_duration": rinfo["duration"],
        "fpv_duration": finfo["duration"],
        "radio_start": rstart,
        "fpv_start": fstart,
        "overlap_duration": duration,
        "offset_seconds": offset,
        "score": row.get("alignment_score", row["combined_score"]),
        "confidence": row["confidence"],
        "evidence": ("Saved reviewed alignment · no automated content score in this run" if row.get("reviewed") and not row["combined_score"] else
                     "Tip-free gimbal movement · experimental review candidate" if row.get("alignment_method") == "gimbal_activity" else
                     "Derived-input rhythm · experimental review candidate" if row.get("alignment_method") == "rhythm" else row["evidence"]),
        "reviewed": row.get("reviewed", False) and abs(offset - row["offset_seconds"]) < 1e-7,
        "review_reason": row.get("review_reason"),
        "timing_only": row.get("timing_only", False),
        "manual_pair": manual,
        "rhythm": row.get("rhythm"),
        "gimbal_activity": row.get("gimbal_activity"),
        "alignment_method": "manual" if manual else row.get("alignment_method", "content"),
        "clock_support": row.get("clock_support", []),
        "verification": (job["pair_overrides"][fpv_name].get("verification", "independent")
                         if job.get("pair_overrides", {}).get(fpv_name, {}).get("radio_video") == radio_name
                         and _decision_current(job, job["pair_overrides"][fpv_name])
                         else "unverified" if manual else "date_only" if row.get("timing_only")
                         else "rhythm_only" if row.get("alignment_method") in {"gimbal_activity", "rhythm"} else "independent"),
        "clock_anchors": [a for m in results.get("learned_clocks", []) for a in m["anchors"]
                          if a["radio_video"] == radio_name and a["fpv_video"] == fpv_name],
        "sync_points": points,
        "sync_check": pair_review.sync_check({"offset": offset, "overlap_duration": duration,
                                              "sync_points": [{"fpv_time": p["fpv_time"], "stick_time": p["radio_time"]} for p in points]}),
        "existing_owner": next((r["radio_video"] for r in results["rows"] if r["fpv_video"] == fpv_name and r.get("reviewed") and r["radio_video"] != radio_name), None),
        "source_signatures": signatures,
        "source_revisions": {kind: _signature_revision(value) for kind, value in signatures.items()},
        "source_metadata": {"radio": rinfo, "fpv": finfo},
        "radio_url": f"/api/jobs/{job['id']}/video/radio/{quote(radio_name, safe='')}",
        "fpv_url": f"/api/jobs/{job['id']}/video/fpv/{quote(fpv_name, safe='')}",
    }


@app.post("/api/jobs/{job_id}/pair-info")
def pair_info(job_id: str, pair: dict = Body(...)):
    if not isinstance(pair.get("manual_pair", False), bool):
        raise HTTPException(400, "Manual pairing must be true or false")
    offset = pair.get("offset_seconds")
    if offset is not None:
        try:
            if isinstance(offset, bool) or not math.isfinite(float(offset)):
                raise ValueError()
        except (TypeError, ValueError):
            raise HTTPException(400, "Offset must be a finite number") from None
    info = _pair_info(get_job(job_id), pair.get("radio_video"), pair.get("fpv_video"), offset, pair.get("manual_pair", False))
    if pair.get("sync_points") is not None:
        info["sync_points"] = _clean_sync_points(pair["sync_points"], info)
        info["sync_check"] = pair_review.sync_check({"offset": info["offset_seconds"], "overlap_duration": info["overlap_duration"],
            "sync_points": [{"fpv_time": p["fpv_time"], "stick_time": p["radio_time"]} for p in info["sync_points"]]})
    return info


@app.put("/api/jobs/{job_id}/pairs/{radio_name}")
def review_pair(job_id: str, radio_name: str, body: dict = Body(...)):
    job = get_job(job_id)
    _record(job, "radio", radio_name)
    fpv_name = body.get("fpv_video")
    if "reject" in body and not isinstance(body["reject"], bool):
        raise HTTPException(400, "Reject must be true or false")
    if "rhythm_suggestion" in body and not isinstance(body["rhythm_suggestion"], bool):
        raise HTTPException(400, "Rhythm suggestion must be true or false")
    if "date_suggestion" in body and not isinstance(body["date_suggestion"], bool):
        raise HTTPException(400, "Date suggestion must be true or false")
    if "independent_check" in body and not isinstance(body["independent_check"], bool):
        raise HTTPException(400, "Independent content check must be true or false")
    manual = body.get("manual_pair", False)
    if not isinstance(manual, bool):
        raise HTTPException(400, "Manual pairing must be true or false")
    recorder = _recorder_label(body.get("fpv_recorder", _record(job, "fpv", fpv_name).get("fpv_recorder", "") if fpv_name else ""))
    if manual and not body.get("reject") and body.get("independent_check") is not True:
        raise HTTPException(400, "Confirm that you independently checked matching visible events before saving a manual pair")
    if fpv_name:
        info = _pair_info(job, radio_name, fpv_name, body.get("offset_seconds"), manual)
        if manual and body.get("source_revisions") != info["source_revisions"]:
            raise HTTPException(409, "The manual preview source changed. Reload both videos and mark the shared event again.")
        offset = info["offset_seconds"]
        if info.get("existing_owner") and not body.get("reject") and body.get("replace_owner") is not True:
            raise HTTPException(409, f"This FPV file is already confirmed with {info['existing_owner']}. Select Replace saved owner to change it.")
    else:
        _results(job)
        offset = 0.0

    def mutate(current):
        if fpv_name:
            previous = current.get("pair_overrides", {}).get(fpv_name)
            if previous and previous["radio_video"] != radio_name and _decision_current(current, previous) and not previous.get("rejected") and not body.get("reject") and body.get("replace_owner") is not True:
                raise HTTPException(409, "Another reviewed StickCam owns this FPV file. Select Replace saved owner to change it.")
            if manual and body.get("source_revisions") != {kind: _signature_revision(value) for kind, value in _decision_signatures(current, radio_name, fpv_name).items()}:
                raise HTTPException(409, "A source changed before confirmation. Reload the manual previews.")
            results = _effective_results(current)
            row = (_manual_pair_row(radio_name, fpv_name, offset, info["source_metadata"]["radio"], info["source_metadata"]["fpv"]) if manual else
                   next(r for r in results["rows"] if r["radio_video"] == radio_name and r["fpv_video"] == fpv_name))
            if not body.get("reject"):
                document = _review_document(current, results)
                proposed = {"id": _layout_key(radio_name, fpv_name), "fpv": "fpv:" + fpv_name, "stick": "radio:" + radio_name,
                            "radio_start": info["radio_start"], "overlap_duration": info["overlap_duration"]}
                conflicts = pair_review.overlap_conflicts(document, proposed)
                if conflicts:
                    raise HTTPException(409, f"This FPV part overlaps confirmed {conflicts[0]['name']} by {conflicts[0]['seconds']:.2f}s in StickCam. Correct the offset or reject the conflicting pair before confirming this split part.")
            entry = {"radio_video": radio_name, "fpv_video": fpv_name, "offset_seconds": offset, "row": row,
                     "fpv_info": {**info["source_metadata"]["fpv"], "name": fpv_name, "size": info["source_signatures"]["fpv"][0], "mtime_ns": info["source_signatures"]["fpv"][1]},
                     "radio_info": {**info["source_metadata"]["radio"], "name": radio_name, "size": info["source_signatures"]["radio"][0], "mtime_ns": info["source_signatures"]["radio"][1]},
                     "source_signatures": info["source_signatures"], "manual_pair": manual, "updated_at": time.time()}
            prior_verification = previous.get("verification", "independent") if previous and _decision_current(current, previous) else "independent"
            activity_only = row.get("alignment_method") in {"gimbal_activity", "rhythm"}
            proposal_only = row.get("timing_only") or body.get("date_suggestion") or body.get("rhythm_suggestion") or activity_only or prior_verification != "independent"
            entry["verification"] = ("independent" if body.get("independent_check") or not proposal_only else
                                     "rhythm_only" if body.get("rhythm_suggestion") or activity_only or prior_verification == "rhythm_only" else "date_only")
            entry["clock_evidence"] = {"settings": clocks.options(current.get("clock_settings")),
                                       "radio_modified": _modified_time_status(current, _record(current, "radio", radio_name)),
                                       "fpv_modified": _modified_time_status(current, _record(current, "fpv", fpv_name))}
            current.setdefault("review_decisions", {}).pop(_layout_key(radio_name, fpv_name), None)
            key = _layout_key(radio_name, fpv_name)
            if body.get("reject", False):
                current.setdefault("rejected_pairs", {})[key] = entry
                existing = current.get("pair_overrides", {}).get(fpv_name)
                if existing and existing["radio_video"] == radio_name:
                    del current["pair_overrides"][fpv_name]
            else:
                current.setdefault("pair_overrides", {})[fpv_name] = entry
                current.setdefault("rejected_pairs", {}).pop(key, None)
                if manual:
                    _record(current, "fpv", fpv_name)["fpv_recorder"] = recorder
                    if "sync_points" in body:
                        current.setdefault("sync_points", {})[key] = {"radio_video": radio_name, "fpv_video": fpv_name,
                            "source_signatures": info["source_signatures"], "points": _clean_sync_points(body["sync_points"], info)}
        else:
            current.setdefault("overrides", {})[radio_name] = {"fpv_video": None, "offset_seconds": 0., "updated_at": time.time()}
            for override in current.get("pair_overrides", {}).values():
                if override["radio_video"] == radio_name:
                    override.update(rejected=True, updated_at=time.time())

    jobs.update(job_id, mutate)
    return {"saved": True}


def _recorder_label(value):
    if not isinstance(value, str) or len(value.strip()) > 80 or any(ord(c) < 32 for c in value):
        raise HTTPException(400, "Recorder label must be text of up to 80 characters")
    return value.strip()


@app.put("/api/jobs/{job_id}/fpv-recorders")
def label_fpv_recorders(job_id: str, body: dict = Body(...)):
    job = get_job(job_id)
    names, label = body.get("names"), _recorder_label(body.get("label", ""))
    if not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names):
        raise HTTPException(400, "Choose one or more FPV files")
    for name in names:
        _record(job, "fpv", name)
    def change(current):
        for name in names:
            _record(current, "fpv", name)["fpv_recorder"] = label
    jobs.update(job_id, change)
    return _public(get_job(job_id))


@app.post("/api/jobs/{job_id}/clock-rejections")
def reject_clock_suggestion(job_id: str, body: dict = Body(...)):
    job = get_job(job_id)
    try:
        offset = float(body["offset_seconds"])
        if isinstance(body["offset_seconds"],bool) or not math.isfinite(offset):
            raise ValueError()
    except (KeyError,TypeError,ValueError):
        raise HTTPException(400,"Choose a finite predicted offset") from None
    results = _effective_results(job)
    proposal = next((p for p in results.get("match_scope",{}).get("timing_suggestions",[])
                     if p["radio_video"]==body.get("radio_video") and p["fpv_video"]==body.get("fpv_video")
                     and abs(p["offset_seconds"]-offset)<.001),None)
    if proposal is None:
        raise HTTPException(409,"This date suggestion changed; refresh suggestions before rejecting it")
    radio,fpv = proposal["radio_video"],proposal["fpv_video"]
    info = _pair_info(job,radio,fpv,offset)
    settings = clocks.options(job.get("clock_settings"))
    support = next((s for s in proposal.get("support",[]) if s),{})
    method = proposal.get("clock_method",support.get("method","modified"))
    records = {}
    for kind,name in (("radio",radio),("fpv",fpv)):
        record = _record(job,kind,name)
        records[kind] = {"duration":info[kind+"_duration"],"filename_time":filename_timestamp(name),
                         "modified_time":_modified_time_status(job,record)["timestamp"]}
    rc,fc = clocks.start(records["radio"],method,settings),clocks.start(records["fpv"],method,settings)
    if rc is None or fc is None:
        raise HTTPException(409,"This prediction no longer has usable camera dates")
    key = uuid.uuid4().hex[:16]
    entry = {"radio_video":radio,"fpv_video":fpv,"offset_seconds":offset,
             "clock_method":method,"clock_delta_seconds":rc-fc+offset,"modified_kind":settings["modified_kind"],
             "tolerance":support.get("tolerance",settings["modified_tolerance"]),
             "group_id":proposal.get("group_id",proposal.get("seed_clock_id")),
             "clock_label":proposal.get("clock_label",proposal.get("clock_source","Recorder clock")),
             "source_signatures":info["source_signatures"],"updated_at":time.time()}
    def change(current):
        if _decision_signatures(active_job(current),radio,fpv) != entry["source_signatures"]:
            raise HTTPException(409,"Source changed before saving the rejection; refresh first")
        current.setdefault("clock_rejections",{})[key] = entry
    jobs.update(job_id,change)
    return _effective_results(get_job(job_id))


@app.delete("/api/jobs/{job_id}/clock-rejections/{rejection_id}")
def restore_clock_suggestion(job_id: str, rejection_id: str):
    get_job(job_id)
    jobs.update(job_id,lambda current:current.setdefault("clock_rejections",{}).pop(rejection_id,None))
    return _effective_results(get_job(job_id))


@app.patch("/api/jobs/{job_id}/clocks")
def clock_settings(job_id: str, body: dict = Body(...)):
    job = get_job(job_id)
    try:
        settings = clocks.options({**job.get("clock_settings", {}), **body})
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    jobs.update(job_id, lambda current: current.update(clock_settings=settings))
    return _effective_results(get_job(job_id))


@app.post("/api/jobs/{job_id}/review-state")
def set_review_state(job_id: str, body: dict = Body(...)):
    job = get_job(job_id)
    radio, fpv = body.get("radio_video"), body.get("fpv_video")
    manual = body.get("manual_pair", False)
    if not isinstance(manual, bool):
        raise HTTPException(400, "Manual pairing must be true or false")
    info = _pair_info(job, radio, fpv, body.get("offset_seconds"), manual)
    if manual and body.get("source_revisions") != info["source_revisions"]:
        raise HTTPException(409, "Manual preview sources changed. Reload them before saving a review decision.")
    state = body.get("state")
    if state not in {"later", "candidate", "no_counterpart", "restore_counterpart"}:
        raise HTTPException(400, "Choose review later, restore candidate, no counterpart or restore counterpart")
    def change(current):
        if state in {"no_counterpart", "restore_counterpart"}:
            names = set(current.get("no_counterpart", []))
            if state == "no_counterpart":
                if any(d["fpv_video"] == fpv and not d.get("rejected") and _decision_current(current, d) for d in current.get("pair_overrides", {}).values()):
                    raise HTTPException(409, "Reject the saved pairing before marking this FPV as having no counterpart")
                names.add(fpv)
            else:
                names.discard(fpv)
            current["no_counterpart"] = sorted(names)
        else:
            current.setdefault("review_decisions", {})[_layout_key(radio, fpv)] = {
                "radio_video": radio, "fpv_video": fpv, "source_signatures": info["source_signatures"],
                "state": state, "updated_at": time.time(),
                **({"manual_pair": True, "offset_seconds": info["offset_seconds"],
                    "row": _manual_pair_row(radio, fpv, info["offset_seconds"], info["source_metadata"]["radio"], info["source_metadata"]["fpv"]),
                    "radio_info": {**info["source_metadata"]["radio"], "name": radio},
                    "fpv_info": {**info["source_metadata"]["fpv"], "name": fpv}} if manual else {})}
    jobs.update(job_id, change)
    return {"saved": True}


def _clean_sync_points(points, info):
    if not isinstance(points, list) or len(points) > 30:
        raise HTTPException(400, "Provide at most 30 independent event checkpoints")
    cleaned = []
    try:
        for point in points:
            f, r = float(point["fpv_time"]), float(point["radio_time"])
            if not math.isfinite(f) or not math.isfinite(r) or not 0 <= f < info["fpv_duration"] or not 0 <= r < info["radio_duration"]:
                raise ValueError("Event times must be inside their original recordings")
            cleaned.append({"fpv_time": f, "radio_time": r})
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return cleaned


@app.put("/api/jobs/{job_id}/sync-points")
def set_sync_points(job_id: str, body: dict = Body(...)):
    job = get_job(job_id)
    manual = body.get("manual_pair", False)
    if not isinstance(manual, bool):
        raise HTTPException(400, "Manual pairing must be true or false")
    info = _pair_info(job, body.get("radio_video"), body.get("fpv_video"), body.get("offset_seconds"), manual)
    if manual and body.get("source_revisions") != info["source_revisions"]:
        raise HTTPException(409, "Manual preview sources changed. Reload the videos.")
    cleaned = _clean_sync_points(body.get("points"), info)
    entry = {"radio_video": info["radio_video"], "fpv_video": info["fpv_video"], "source_signatures": info["source_signatures"], "points": cleaned}
    jobs.update(job_id, lambda current: current.setdefault("sync_points", {}).update({_layout_key(info["radio_video"], info["fpv_video"]): entry}))
    return _pair_info(get_job(job_id), info["radio_video"], info["fpv_video"], body.get("offset_seconds"), manual)


@app.post("/api/jobs/{job_id}/pairs/{radio_name}/segments")
def find_related_segments(job_id: str, radio_name: str, body: dict = Body(...)):
    from fpv_matcher.matching.segments import VERSION as SEGMENT_VERSION
    job = get_job(job_id)
    anchor = _pair_info(job, radio_name, body.get("fpv_video"))
    if not anchor["reviewed"]:
        raise HTTPException(409, "Save a verified pair alignment before using its clock difference")
    if filename_timestamp(anchor["fpv_video"]) is None:
        raise HTTPException(400, "The confirmed FPV filename has no usable timestamp; find other segments with audio or motion")
    clips = []
    recorder = _record(job, "fpv", anchor["fpv_video"]).get("fpv_recorder", "")
    for record in job["inputs"]["fpv"]:
        if record.get("fpv_recorder", "") != recorder:
            continue
        source = _input_video(job, "fpv", record["name"])
        stat = source.stat()
        signature = [stat.st_size, stat.st_mtime_ns]
        clips.append({"name": record["name"], "path": str(source), "signature": signature,
                      "metadata": record.get("metadata") if record.get("metadata_signature") == signature else None})
    anchor = {**anchor, "radio_path": str(_input_video(job, "radio", radio_name)),
              "fpv_path": str(_input_video(job, "fpv", anchor["fpv_video"]))}
    return _queue_signal_task(job_id, {"kind": "fpv-segments", "version": SEGMENT_VERSION, "anchor": anchor,
                              "clips": clips, "min_overlap": job["settings"].get("min_overlap", 2)}, "segments.json")


@app.api_route("/api/jobs/{job_id}/video/{kind}/{filename}", methods=["GET", "HEAD"])
def source_video(job_id: str, kind: str, filename: str, request: Request):
    job = get_job(job_id)
    path = _input_video(job, kind, filename)
    if request.query_params.get("proxy") == "1":
        key = hashlib.sha256((kind + "/" + filename).encode()).hexdigest()[:16]
        proxy = job["proxies"].get(key)
        if not proxy or proxy["status"] != "complete":
            raise HTTPException(409, "Playback proxy is not ready")
        path = Path(proxy["path"])
    # Starlette's FileResponse handles normal, suffix, HEAD and invalid byte
    # ranges. Sources and lightweight proxies can both be sought over LAN.
    return FileResponse(path, headers={"Accept-Ranges": "bytes", "Cache-Control": "private, no-cache"})


@app.get("/api/jobs/{job_id}/preview/{kind}/{filename}")
def browser_preview(job_id: str, kind: str, filename: str, acceleration: str = "auto"):
    job = get_job(job_id)
    source = _input_video(job, kind, filename)
    if acceleration not in {"auto", "cpu"}:
        raise HTTPException(400, "Preview acceleration must be Auto or CPU")
    return preview_manifest(source, _metadata(job, kind, filename), acceleration)


@app.get("/api/jobs/{job_id}/preview/{kind}/{filename}/{chunk}")
async def browser_preview_chunk(job_id: str, kind: str, filename: str, chunk: int, request: Request):
    job = get_job(job_id)
    source = _input_video(job, kind, filename)
    info = await anyio.to_thread.run_sync(_metadata, job, kind, filename)
    acceleration = request.query_params.get("acceleration", "auto")
    if acceleration not in {"auto", "cpu"}:
        raise HTTPException(400, "Preview acceleration must be Auto or CPU")
    descriptor = preview_manifest(source, info, acceleration)
    if request.query_params.get("source") != descriptor["source_key"]:
        raise HTTPException(409, "Source recording changed; reload this preview")
    if chunk < 0 or chunk >= descriptor["chunks"]:
        raise HTTPException(404, "Preview chunk is outside this recording")
    cancelled = threading.Event()
    key = "preview:" + uuid.uuid4().hex
    future = asyncio.get_running_loop().run_in_executor(
        PREVIEW_POOL, preview_fragment, source, info, chunk, jobs.DATA / "preview-cache", _execute, key, cancelled, acceleration)
    try:
        while not future.done():
            if await request.is_disconnected():
                raise asyncio.CancelledError()
            await asyncio.wait({future}, timeout=.25)
        output, details = await future
        return FileResponse(output, media_type="video/mp4", headers={
            "Cache-Control": "private, max-age=86400, immutable",
            "X-Preview-Start": str(details["start"]),
            "X-Preview-Duration": str(details["duration"]),
            "X-Preview-Video-Start": str(details["video_start"]),
            "X-Preview-Decoder": details.get("decoder", "cpu"),
        })
    except asyncio.CancelledError:
        cancelled.set()
        _stop_process(key)
        # Retrieve a worker exception after the disconnected request ends.
        future.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        raise
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(422, "Preview chunk could not be prepared: " + str(exc)[-600:]) from exc
    finally:
        def clear_preview_key(task):
            with PROCESS_LOCK:
                CANCELLED.discard(key)
        if future.done():
            clear_preview_key(future)
        else:
            future.add_done_callback(clear_preview_key)


@app.post("/api/jobs/{job_id}/proxy/{kind}/{filename}")
def create_proxy(job_id, kind, filename):
    job = get_job(job_id)
    source = _input_video(job, kind, filename)
    key = hashlib.sha256((kind + "/" + filename).encode()).hexdigest()[:16]
    existing = job["proxies"].get(key)
    if existing and existing["status"] in {"complete", "running", "queued"}:
        return {"id": key, **existing}
    output = jobs.job_dir(job_id) / "proxies" / (key + ".mp4")
    jobs.update(
        job_id,
        lambda current: current["proxies"].update(
            {
                key: {
                    "status": "queued",
                    "message": "Queued for a 720p browser playback proxy",
                    "path": str(output),
                }
            }
        ),
    )
    with PROCESS_LOCK:
        CANCELLED.discard("proxy:" + job_id + ":" + key)
    PROXY_POOL.submit(_make_proxy, job_id, key, source, output)
    return {"id": key, "status": "queued"}


def _make_proxy(job_id, key, source, output):
    try:
        jobs.update(
            job_id,
            lambda current: current["proxies"][key].update(
                status="running", message="Encoding a 720p browser playback proxy"
            ),
        )
        command = [
            shutil.which("ffmpeg") or "ffmpeg",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-threads",
            "2",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-filter_threads",
            "1",
            "-vf",
            "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,fps=30:start_time=0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-threads",
            "2",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(output),
        ]
        _execute("proxy:" + job_id + ":" + key, command, output.with_suffix(".log"), timeout=8 * 3600)
        jobs.update(
            job_id,
            lambda current: current["proxies"][key].update(status="complete", message="Playback proxy ready"),
        )
    except Exception as exc:
        detail = str(exc)
        if get_job(job_id)["proxies"][key]["status"] != "interrupted":
            jobs.failed_task(job_id, detail, "proxies", key)


@app.get("/api/jobs/{job_id}/proxy/{proxy_id}")
def proxy_status(job_id, proxy_id):
    proxy = get_job(job_id)["proxies"].get(proxy_id)
    if not proxy:
        raise HTTPException(404, "Proxy not found")
    return {"status": proxy["status"], "message": proxy["message"]}


def _layout_key(radio, fpv):
    return hashlib.sha256(json.dumps([radio, fpv]).encode()).hexdigest()[:20]


@app.post("/api/jobs/{job_id}/portrait-layout")
def portrait_layout(job_id: str, body: dict = Body(...)):
    job = get_job(job_id)
    info = _pair_info(job, body.get("radio_video"), body.get("fpv_video"), body.get("offset_seconds"))
    key = _layout_key(info["radio_video"], info["fpv_video"])
    layout = job.get("portrait_layouts", {}).get(key, PORTRAIT_DEFAULT)
    return {"layout": layout, "pair": info}


@app.put("/api/jobs/{job_id}/portrait-layout")
def save_portrait_layout(job_id: str, body: dict = Body(...)):
    job = get_job(job_id)
    info = _pair_info(job, body.get("radio_video"), body.get("fpv_video"), body.get("offset_seconds"))
    if not info["reviewed"]:
        raise HTTPException(409, "Save the pair alignment before editing its portrait layout")
    try:
        layout = validate_layout(body.get("layout"), info["overlap_duration"])
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    key = _layout_key(info["radio_video"], info["fpv_video"])
    jobs.update(job_id, lambda current: current.setdefault("portrait_layouts", {}).update({key: layout}))
    return {"saved": True, "layout": layout}


def _stabilization_descriptor(job, info):
    from fpv_matcher.radio.derivation import cache_path
    from fpv_matcher.video.radio_stabilization import VERSION
    calibration = job["calibrations"].get(info["radio_video"])
    expected_calibration = job.get("calibration_signatures", {}).get(info["radio_video"])
    if expected_calibration and expected_calibration != info["source_signatures"]["radio"]:
        raise HTTPException(409, "StickCam source changed; reconfirm calibration before stabilization")
    if not calibration or calibration.get("exclude") or calibration.get("skip_motion") or "radio_outline" not in calibration:
        raise HTTPException(409, "Save a visible radio outline and both tips, then prepare this StickCam before requesting a stabilized export")
    record = _record(job, "radio", info["radio_video"])
    source = _input_video(job, "radio", record["name"])
    fps = tracking_fps(job["settings"].get("sample_fps", 30), job["settings"].get("stick_sampling_ratio"), info["source_metadata"]["radio"].get("fps"))
    identity = _fpv_source_identity(record)
    status = stick_cache_status(source, jobs.DATA / "cache", calibration, fps, identity)
    if status["status"] != "cached" or status.get("outline_usable_percent", 0) <= 0:
        raise HTTPException(409, "No current usable radio outline data. Prepare this StickCam for stabilization, or export the original")
    return {"version": VERSION, "cache_path": str(cache_path(source, jobs.DATA / "cache", calibration, fps, identity)),
            "calibration_signature": hashlib.sha256(json.dumps(calibration, sort_keys=True).encode()).hexdigest(),
            "radio_rotation_deg": calibration.get("radio_rotation_deg", 0), "source_signature": info["source_signatures"]["radio"],
            "sample_fps": fps}


@app.post("/api/jobs/{job_id}/exports")
def create_export(job_id: str, body: dict = Body(...)):
    job = get_job(job_id)
    requested = body.get("pairs", [body])
    if not isinstance(requested, list) or not requested:
        raise HTTPException(400, "Choose at least one pair to export")
    fps = body.get("fps")
    profile = body.get("profile", "copy")
    try:
        output_root = export_locations.destination(body.get("destination", job.get("export_destination", "")))
        parent = output_root
        while not parent.exists():
            parent = parent.parent
    except (ValueError, OSError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    if fps is not None and fps not in {24, 25, 30, 50, 60}:
        raise HTTPException(400, "Choose original frame rates or 24, 25, 30, 50 or 60fps")
    if profile not in {"copy", "h264", "dnxhr"}:
        raise HTTPException(400, "Export profile must be copy, h264 or dnxhr")
    if profile == "copy" and fps is not None:
        raise HTTPException(400, "Fast trim preserves original frame rates")
    portrait = body.get("portrait")
    stabilized = body.get("stabilize_radio", False)
    if not isinstance(stabilized, bool):
        raise HTTPException(400, "StickCam stabilization must be true or false")
    if stabilized and portrait is not None:
        raise HTTPException(400, "Stabilization currently applies to separate aligned clips; use the portrait editor for static pane framing")
    if (stabilized or portrait is not None) and (fps is None or profile == "copy"):
        raise HTTPException(400, "Stabilization and portrait composition require accurate encoding and an explicit frame rate")
    infos = [
        _pair_info(job, pair.get("radio_video"), pair.get("fpv_video"), pair.get("offset_seconds"))
        for pair in requested
    ]
    if any(not info["reviewed"] for info in infos):
        raise HTTPException(409, "Confirm every pair and its current offset before exporting")
    if any(info["sync_check"]["status"] in {"drift", "offset_mismatch"} for info in infos) and body.get("acknowledge_sync_warning") is not True:
        raise HTTPException(409, "Independent event checkpoints disagree with this alignment. Review them or explicitly acknowledge the checkpoint warning before exporting.")
    document = _review_document(job, _effective_results(job))
    requested_parts = [{"id": _layout_key(i["radio_video"], i["fpv_video"]), "fpv": "fpv:" + i["fpv_video"], "stick": "radio:" + i["radio_video"],
                        "radio_start": i["radio_start"], "overlap_duration": i["overlap_duration"]} for i in infos]
    if any(pair_review.overlap_conflicts(document, p, requested_parts) for p in requested_parts):
        raise HTTPException(409, "Selected FPV parts overlap on the StickCam timeline. Correct or remove conflicting parts before a grouped export.")
    if stabilized:
        for info in infos:
            info["radio_stabilization_request"] = _stabilization_descriptor(job, info)
    if portrait is not None:
        if len(infos) != 1 or profile != "h264":
            raise HTTPException(400, "Portrait export requires one pair and compact MP4")
        if not infos[0]["reviewed"]:
            raise HTTPException(409, "Save the pair alignment before portrait export")
        try:
            portrait = validate_layout(portrait, infos[0]["overlap_duration"])
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
    if profile == "dnxhr":
        if any(
            metadata["width"] < 256 or metadata["height"] < 120
            for info in infos
            for metadata in info["source_metadata"].values()
        ):
            raise HTTPException(400, "DNxHR requires source dimensions of at least 256 × 120")
        # A conservative bound keeps large edit-codec batches within available storage.
        estimate = sum(
            metadata["width"] * metadata["height"] * info["overlap_duration"] * (fps or metadata.get("fps", 30)) * 0.75
            for info in infos
            for metadata in info["source_metadata"].values()
        )
        if estimate > shutil.disk_usage(parent).free - 1024**3:
            raise HTTPException(
                507,
                f"This DNxHR batch needs approximately {estimate / 1024**3:.1f} GiB of working space. Export fewer pairs or choose compact MP4.",
            )
    if len({i["fpv_video"] for i in infos}) != len(infos):
        raise HTTPException(400, "Each FPV segment can appear only once in a batch export")
    if shutil.disk_usage(parent).free < 1024**3:
        raise HTTPException(507, "Export needs at least 1 GiB of working disk space")
    signature = hashlib.sha256(
        json.dumps(
            {
                "fps": fps,
                "destination": str(output_root),
                "verify_frames": body.get("verify_frames", False),
                "profile": profile,
                "portrait": portrait,
                "stabilize_radio": stabilized,
                "radio_stabilization": [info.get("radio_stabilization_request") for info in infos],
                "pairs": [
                    {
                        key: info[key]
                        for key in ("radio_video", "fpv_video", "offset_seconds", "source_signatures")
                    }
                    for info in infos
                ],
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    with jobs.LOCK:
        # Check the latest state while reserving the export, including concurrent
        # requests from multiple browser tabs.
        current = get_job(job_id)
        for previous_id, previous in current["exports"].items():
            if previous.get("signature") == signature and previous["status"] in {
                "queued",
                "running",
                "complete",
            }:
                return {"id": previous_id}
            if previous.get("signature") == signature and previous["status"] in {"failed", "cancelled", "interrupted"}:
                jobs.update(job_id, lambda doc: doc["exports"][previous_id].update(status="queued", message="Resuming checked completed pairs"))
                with PROCESS_LOCK:
                    CANCELLED.discard("export:" + job_id + ":" + previous_id)
                EXPORT_POOL.submit(_make_export, job_id, previous_id, infos, fps, profile, portrait)
                return {"id": previous_id}
        export_id = uuid.uuid4().hex[:10]
        try:
            directory = export_locations.create_directory(output_root, job.get("label", "Session"))
        except OSError as exc:
            raise HTTPException(422, f"Cannot create the export folder: {exc}") from exc
        export_locations.register_pending({"id": export_id, "session": job_id, "directory": str(directory),
            "created": time.time(), "session_name": job.get("label", "Session"), "pairs": len(infos), "profile": profile,
            "fps": fps or "original"})
        jobs.update(
            job_id,
            lambda current: current["exports"].update(
                {
                    export_id: {
                        "status": "queued",
                        "message": "Queued portrait composite" if portrait else f"Queued {len(infos)} aligned pair(s)",
                        "portrait": portrait,
                        "pairs": infos,
                        "fps": fps,
                        "profile": profile,
                        "signature": signature,
                        "directory": str(directory), "verify_frames": body.get("verify_frames", False),
                    }
                }
            ),
        )
        jobs.update(job_id, lambda current: current.update(export_destination=str(output_root)))
    EXPORT_POOL.submit(_make_export, job_id, export_id, infos, fps, profile, portrait)
    return {"id": export_id}


def _make_export(job_id, export_id, infos, fps, profile, portrait=None):
    key = "export:" + job_id + ":" + export_id
    item = get_job(job_id)["exports"][export_id]
    directory = Path(item.get("directory") or jobs.job_dir(job_id) / "results" / "exports" / export_id)
    try:
        manifests = []
        export_started = time.monotonic()
        for index, info in enumerate(infos):
            if key in CANCELLED:
                raise RuntimeError("Cancelled")
            job = get_job(job_id)
            current_alignment = _pair_info(job, info["radio_video"], info["fpv_video"], info["offset_seconds"])
            if not current_alignment["reviewed"]:
                raise ValueError("Alignment changed or was rejected while export was queued; review it and request export again")
            sources = {kind: _input_video(job, kind, info[kind + "_video"]) for kind in ("radio", "fpv")}
            for kind, source in sources.items():
                if [source.stat().st_size, source.stat().st_mtime_ns] != info["source_signatures"][kind]:
                    raise ValueError("Source changed while export was queued; analyze it again")
            pair_dir = directory / f"pair_{index + 1:02d}"

            if not portrait:
                try:
                    saved = json.loads((pair_dir / "alignment.json").read_text())
                    from fpv_matcher.exports import _output_info
                    if (saved.get("source_signatures") == info["source_signatures"] and
                            saved.get("offset_seconds") == info["offset_seconds"] and saved.get("export_profile") == profile and
                            saved.get("export_fps") == (fps if fps is not None else "original") and
                            bool(saved.get("radio_stabilization")) == bool(info.get("radio_stabilization_request"))):
                        for kind, label in (("radio", "StickCam"), ("fpv", "FPV")):
                            _output_info(pair_dir / saved[kind + "_export"], saved["export_duration"],
                                         info["source_metadata"][kind]["fps"], frame_count=saved.get("export_frame_count"), copy=profile == "copy")
                        manifests.append((pair_dir, saved))
                        continue
                except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
                    pass

            def progress(message, pair_number=index + 1):
                jobs.update(
                    job_id,
                    lambda current: current["exports"][export_id].update(
                        status="running", message=f"Pair {pair_number}/{len(infos)} · {message}"
                    ),
                )
            def execute_export(command, log):
                if Path(command[0]).stem.lower() != "ffmpeg":
                    return _execute(key, command, log, timeout=60)
                command = [command[0], "-progress", "pipe:1", "-nostats", *command[1:]]
                source_index = 0 if log.stem == "StickCam" else 1
                duration = info["overlap_duration"]
                def update(seconds):
                    fraction = min(1., max(0., seconds / duration))
                    percent = 100 * (index + (source_index + fraction) / 2) / len(infos)
                    elapsed = time.monotonic() - export_started
                    eta = elapsed * (100 - percent) / percent if percent > 1 else None
                    jobs.update(job_id, lambda current: current["exports"][export_id].update(
                        percent=percent, elapsed_seconds=elapsed, eta_seconds=eta,
                        message=f"Pair {index + 1}/{len(infos)} · {log.stem} {fraction:.0%}"))
                return _execute(key, command, log, timeout=8 * 3600, on_progress=update)

            radio_stabilization = None
            descriptor = info.get("radio_stabilization_request")
            if descriptor:
                from fpv_matcher.video.radio_stabilization import stabilization_request
                calibration = job["calibrations"].get(info["radio_video"])
                if hashlib.sha256(json.dumps(calibration, sort_keys=True).encode()).hexdigest() != descriptor["calibration_signature"]:
                    raise ValueError("Radio calibration changed while export was queued; request the export again")
                with np.load(descriptor["cache_path"], allow_pickle=False) as values:
                    data = dict(values)
                radio_stabilization = stabilization_request(data, info["source_metadata"]["radio"],
                    source_signature=descriptor["source_signature"], radio_rotation_deg=descriptor["radio_rotation_deg"])
            if portrait:
                manifest = export_portrait(info, sources["radio"], sources["fpv"], pair_dir, fps, portrait,
                    run=lambda command, log: _execute(key, command, log, timeout=8 * 3600), progress=progress)
            else:
                manifest = export_pair(info, sources["radio"], sources["fpv"], pair_dir, fps, profile,
                    radio_stabilization=radio_stabilization, cancelled=lambda: key in CANCELLED, run=execute_export, progress=progress)
            if item.get("verify_frames") and not portrait and not radio_stabilization:
                from fpv_matcher.cut_checks import compare_cut
                flag = threading.Event()
                def stopped():
                    return key in CANCELLED
                flag.is_set = stopped
                manifest["decoded_cut_checks"] = {kind: compare_cut(sources[kind], pair_dir / manifest[kind + "_export"],
                    info[kind + "_start"], manifest["export_duration"], info["source_metadata"][kind]["fps"], flag)
                    for kind in ("radio", "fpv")}
                atomic_json(pair_dir / "alignment.json", manifest)
            manifests.append((pair_dir, manifest))
            for kind, source in sources.items():
                if [source.stat().st_size, source.stat().st_mtime_ns] != info["source_signatures"][kind]:
                    raise ValueError("Source changed during export; analyze it again")
        if key in CANCELLED:
            raise RuntimeError("Cancelled")
        current_job = get_job(job_id)
        if any(not _pair_info(current_job, info["radio_video"], info["fpv_video"], info["offset_seconds"])["reviewed"] for info in infos):
            raise ValueError("An alignment changed during export; the output was not published. Review and request it again.")
        atomic_json(directory / "flights.json", {"session": current_job.get("label"), "parts": [
            {"radio_video": manifest["radio_video"], "fpv_video": manifest["fpv_video"], "radio_start": manifest["radio_start"],
             "fpv_start": manifest["fpv_start"], "duration": manifest.get("export_duration"), "directory": pair_dir.name}
            for pair_dir, manifest in manifests]})
        export_locations.save({"id": export_id, "session": job_id, "directory": str(directory), "created": time.time(),
            "session_name": current_job.get("label", "Session"), "pairs": len(infos), "profile": profile, "fps": fps or "original"})
        jobs.update(
            job_id,
            lambda current: (
                current["exports"][export_id].update(
                    status="complete", message="Portrait composite ready" if portrait else "Aligned clips ready", directory=str(directory), percent=100, eta_seconds=0
                )
                if current["exports"][export_id]["status"] not in {"cancelled", "interrupted"}
                else None
            ),
        )
    except Exception as exc:
        status = "cancelled" if key in CANCELLED else "failed"
        detail = str(exc)
        if get_job(job_id)["exports"][export_id]["status"] != "interrupted":
            jobs.failed_task(job_id, detail, "exports", export_id, status=status)
        # Retain completed pair manifests for a checked resume. Partial files
        # are not offered as completed downloads.


@app.get("/api/jobs/{job_id}/exports/{export_id}")
def export_status(job_id, export_id):
    item = get_job(job_id)["exports"].get(export_id)
    if not item:
        raise HTTPException(404, "Export not found")
    directory = Path(item.get("directory") or jobs.job_dir(job_id) / "results" / "exports" / export_id)
    files = (
        sorted(
            str(p.relative_to(directory))
            for p in directory.glob("pair_*/*")
            if p.suffix in {".mp4", ".mov", ".json"}
        )
        if item["status"] == "complete"
        else []
    )
    return {
        "status": item["status"],
        "message": item["message"],
        "directory": str(directory),
        "percent": item.get("percent"), "elapsed_seconds": item.get("elapsed_seconds"), "eta_seconds": item.get("eta_seconds"),
        "files": files,
        "download_url": f"/api/jobs/{job_id}/exports/{export_id}/download"
        if item["status"] == "complete"
        else None,
    }


@app.post("/api/jobs/{job_id}/exports/{export_id}/cancel")
def cancel_export(job_id, export_id):
    item = get_job(job_id)["exports"].get(export_id)
    if not item or item["status"] not in {"running", "queued"}:
        raise HTTPException(409, "No active export to cancel")
    _stop_process("export:" + job_id + ":" + export_id)
    jobs.update(
        job_id,
        lambda current: current["exports"][export_id].update(status="cancelled", message="Export cancelled"),
    )
    return {"status": "cancelled"}


@app.get("/api/jobs/{job_id}/exports/{export_id}/download")
def download_export(job_id, export_id):
    item = get_job(job_id)["exports"].get(export_id)
    if not item or item["status"] != "complete":
        raise HTTPException(404, "Completed export not found")
    # Older completed jobs may already have a ZIP; it remains downloadable.
    if item.get("path") and Path(item["path"]).is_file():
        return FileResponse(item["path"], media_type="application/zip", filename="aligned_pairs.zip")
    directory = Path(item.get("directory") or jobs.job_dir(job_id) / "results" / "exports" / export_id)
    return StreamingResponse(
        stream_archive(directory),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="aligned_pairs.zip"'},
    )


@app.get("/api/jobs/{job_id}/exports/{export_id}/files/{filename:path}")
def download_clip(job_id, export_id, filename):
    item = get_job(job_id)["exports"].get(export_id)
    if not item or item["status"] != "complete":
        raise HTTPException(404, "Completed export not found")
    root = Path(item.get("directory") or jobs.job_dir(job_id) / "results" / "exports" / export_id)
    path = (root / filename).resolve()
    if (
        root.resolve() not in path.parents
        or not path.is_file()
        or path.suffix not in {".mp4", ".mov", ".json"}
    ):
        raise HTTPException(404, "Export file not found")
    return FileResponse(path, filename=path.name)


def _chart_directory(job_id, chart_id):
    if not re.fullmatch(r"[a-f0-9]{20}", chart_id):
        raise HTTPException(404, "Motion review not found")
    return jobs.job_dir(job_id) / "motion-review" / chart_id


def _chart_worker(job_id, chart_id):
    directory = _chart_directory(job_id, chart_id)
    key = f"chart:{job_id}:{chart_id}"
    def status(value, message):
        jobs.update(job_id, lambda job: job.setdefault("charts", {})[chart_id].update(status=value, message=message))
    try:
        with PROCESS_LOCK:
            if key in CANCELLED:
                status("cancelled", "Signal extraction cancelled")
                return
            request = json.loads((directory / "request.json").read_text())
            active_paths = {r["path"] for rows in get_job(job_id)["inputs"].values() for r in rows}
            requested_paths = [request.get(k) for k in ("fpv_path","radio_path")]
            requested_paths += [r.get("path") for r in request.get("clips",[])]
            requested_paths += [(request.get("anchor") or {}).get(k) for k in ("fpv_path","radio_path")]
            requested_paths += [(request.get("reference") or {}).get("path")]
            requested_paths += [r.get("path") for r in request.get("references",[])]
            if any(p and p not in active_paths for p in requested_paths):
                raise ValueError("A recording was ignored while this task was being prepared; refresh and start again")
            kind = request.get("kind", "motion-review")
            scanning = kind == "gyro-scan"
            messages = {"gyro-scan": "Comparing cached StickCam inputs with session FPV telemetry.",
                        "motor-search":"Comparing motor-band audio with embedded FPV motion.",
                        "cross-modal-search":"Comparing available visual and audio rhythms across recordings.",
                        "fingerprint-study":"Extracting cross-modal rhythms from confirmed reference pairs.",
                        "audio-validation": "Comparing audio evidence with independently labelled reference pairs.",
                        "radio-orientation": "Checking the session radio around each clip's midpoint.",
                        "audio-review": "Reading cached audio or decoding waveform envelopes.",
                        "calibration-scan": "Finding visible radio outlines and gimbals at five-second intervals.",
                        "fpv-segments": "Looking for timestamp overlaps around the confirmed FPV segment.",
                        "fpv-preparation": "Checking embedded FPV motion and preparing visual fallback data.",
                        "stick-derivation": "Deriving calibrated StickCam recordings; cached signals are reused."}
            status("running", messages.get(kind, "Reading FPV motion and tracking StickCam; cached signals are reused."))
            log = (directory / "worker.log").open("w")
            module = {"gyro-scan": "fpv_matcher.motion.gyro_scan", "audio-review": "fpv_matcher.audio.review",
                      "motor-search":"fpv_matcher.matching.fingerprint_worker",
                      "cross-modal-search":"fpv_matcher.matching.fingerprint_worker",
                      "fingerprint-study":"fpv_matcher.matching.fingerprint_worker",
                      "audio-validation": "fpv_matcher.audio.validation",
                      "radio-orientation": "fpv_matcher.radio.orientation",
                      "calibration-scan": "fpv_matcher.radio.calibration_scan",
                      "fpv-segments": "fpv_matcher.matching.segments",
                      "fpv-preparation": "fpv_matcher.motion.derivation",
                      "stick-derivation": "fpv_matcher.radio.derivation"}.get(kind, "fpv_matcher.motion.review")
            process = subprocess.Popen(_worker_command(module, directory / "request.json"),
                stdout=log, stderr=log, **_worker_process_options())
            PROCESSES[key] = process
        try:
            clip_count = max(1, len(request.get("clips", [])))
            timeout = min(72 * 3600, max(2 * 3600, clip_count * 2 * 3600))
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop_process(key)
            process.wait(timeout=10)
            raise ValueError(f"{kind} exceeded its {timeout / 3600:.0f}-hour workload limit. Completed clip caches are kept; retry to resume them.")
        finally:
            log.close()
        with PROCESS_LOCK:
            cancelled = key in CANCELLED
        if cancelled:
            status("cancelled", "Signal extraction cancelled")
        elif code:
            detail = (directory / "worker.log").read_text(errors="replace")[-1200:]
            lines=[line for line in detail.splitlines() if line.strip()]
            status("failed", lines[-1] if lines else "Signal extraction failed")
        else:
            if kind == "radio-orientation":
                result = json.loads((directory / "orientation.json").read_text())
                def save_orientation(current):
                    if (current.get("radio_profile") or {}).get("revision") == result["profile_revision"]:
                        current.setdefault("radio_orientation_checks", {}).update({row["name"]: {**row, "task_id": chart_id}
                                                                                  for row in result["clips"]})
                jobs.update(job_id, save_orientation)
            if kind == "fpv-segments":
                result = json.loads((directory / "segments.json").read_text())
                jobs.update(job_id, lambda current: current.setdefault("segment_candidates", {}).update({
                    result["anchor"]["radio_video"]: {**result, "task_id": chart_id}}))
            status("complete", {"gyro-scan": "Gyro correlation scan ready", "audio-review": "Audio waveforms ready",
                                "motor-search":"Motor-band rhythm candidates ready for review",
                                "cross-modal-search":"Combined rhythm candidates ready for review",
                                "fingerprint-study":"Confirmed-pair rhythm study ready",
                                "audio-validation": "Known-answer audio report ready",
                                "radio-orientation": "Radio orientation suggestions ready for review",
                                "calibration-scan": "Calibration proposals ready for review",
                                "fpv-segments": "Related FPV segment candidates ready for review",
                                "fpv-preparation": "FPV preparation finished; inspect per-video results",
                                "stick-derivation": "StickCam preparation finished; inspect per-video results"}.get(kind, "Motion signals ready"))
    except Exception as exc:
        jobs.failed_task(job_id, str(exc), "charts", chart_id)
    finally:
        with PROCESS_LOCK:
            PROCESSES.pop(key, None)
        with CHART_LOCK:
            CHART_TASKS.pop((job_id, chart_id), None)


def _fingerprint_record(job,kind,name):
    record=_record(job,kind,name)
    source=_input_video(job,kind,name)
    stat=source.stat()
    return {"name":name,"path":str(source),"signature":[stat.st_size,stat.st_mtime_ns],
            "identity":_fpv_source_identity(record),"metadata":record.get("metadata") if record.get("metadata_signature")==[stat.st_size,stat.st_mtime_ns] else _metadata(job,kind,name)}


@app.get("/api/jobs/{job_id}/fingerprints")
def cached_fingerprint_review(job_id: str, radio_video: str, fpv_video: str, method: str = "gimbal_activity", offset: float | None = None, low_hz: float = 150, high_hz: float = 6000):
    from fpv_matcher.matching.fingerprint_review import signals,document
    from fpv_matcher.audio.motor import band
    job=get_job(job_id)
    if offset is not None and not math.isfinite(offset):
        raise HTTPException(400,"Choose a finite offset")
    radio,fpv = _fingerprint_record(job,"radio",radio_video),_fingerprint_record(job,"fpv",fpv_video)
    try:
        settings={**job["settings"],"motor_band":band([low_hz,high_hz])}
        r,f,label,algorithm=signals(radio,fpv,job["calibrations"].get(radio_video),jobs.DATA/"cache",settings,method)
        result=document(r,f,label,algorithm,offset)
        if _decision_signatures(job,radio_video,fpv_video)!={"radio":radio["signature"],"fpv":fpv["signature"]}:
            raise ValueError("Source changed while reading its fingerprints")
        return {**result,"method":method,"radio_video":radio_video,"fpv_video":fpv_video,
                "cache_reused":True,"settings":settings,"source_signatures":{"radio":radio["signature"],"fpv":fpv["signature"]}}
    except (ValueError,TypeError,KeyError,OSError) as exc:
        raise HTTPException(409,str(exc)) from exc


@app.post("/api/jobs/{job_id}/fingerprint-study")
def start_fingerprint_study(job_id: str, body: dict = Body(default={})):
    from fpv_matcher.matching.fingerprint_worker import VERSION
    from fpv_matcher.audio.motor import band
    job=get_job(job_id)
    if scope_busy(job):
        raise HTTPException(409,"Finish active work before starting a reference study or motor-audio search")
    kind=body.get("kind","fingerprint-study")
    if kind not in {"fingerprint-study","motor-search","cross-modal-search"}:
        raise HTTPException(400,"Choose confirmed-pair study or motor audio search")
    try:
        settings={**job["settings"],"motor_band":band(body.get("motor_band"))}
    except (ValueError,TypeError) as exc:
        raise HTTPException(400,str(exc)) from exc
    if kind=="fingerprint-study":
        pairs=[]
        for row in _effective_results(job)["rows"]:
            if not row.get("reviewed"):
                continue
            decision=job.get("pair_overrides",{}).get(row["fpv_video"],{})
            if decision.get("verification","independent")!="independent":
                continue
            pairs.append({"radio":_fingerprint_record(job,"radio",row["radio_video"]),
                          "fpv":_fingerprint_record(job,"fpv",row["fpv_video"]),
                          "calibration":job["calibrations"].get(row["radio_video"]),"reference_offset":row["offset_seconds"]})
    else:
        fpv=_fingerprint_record(job,"fpv",body.get("fpv_video"))
        names=body.get("radio_names") or [r["name"] for r in job["inputs"]["radio"]]
        if not isinstance(names,list) or not names or any(not isinstance(n,str) for n in names) or len(names)!=len(set(names)):
            raise HTTPException(400,"Choose one or more included StickCam recordings")
        pairs=[{"fpv":fpv,"radio":_fingerprint_record(job,"radio",n),"calibration":job["calibrations"].get(n)} for n in names]
    if not pairs:
        raise HTTPException(400,"Confirm an independently checked pair first; date-only confirmations do not provide reference material")
    request={"kind":kind,"version":VERSION,"settings":settings,"pairs":pairs,
             "clips":[record for pair in pairs for record in (pair["radio"],pair["fpv"])]}
    return _queue_signal_task(job_id,request,"fingerprints.json")


@app.get("/api/jobs/{job_id}/fingerprint-study/{chart_id}/{index}")
def fingerprint_study_pair(job_id: str, chart_id: str, index: int):
    job=get_job(job_id)
    if index<0 or job.get("charts",{}).get(chart_id,{}).get("status")!="complete":
        raise HTTPException(404,"Saved fingerprint comparison is not ready")
    directory=_chart_directory(job_id,chart_id)
    request=json.loads((directory/"request.json").read_text())
    if request.get("kind") not in {"fingerprint-study","motor-search","cross-modal-search"} or index>=len(request["pairs"]):
        raise HTTPException(404,"Fingerprint comparison not found")
    pair=request["pairs"][index]
    if _decision_signatures(job,pair["radio"]["name"],pair["fpv"]["name"])!={kind:pair[kind]["signature"] for kind in ("radio","fpv")}:
        raise HTTPException(409,"Source changed; extract a new reference study")
    target=directory/f"pair-{index}.json"
    if not target.is_file():
        raise HTTPException(409,"This pair had insufficient data; inspect the study issues")
    return json.loads(target.read_text())


@app.post("/api/jobs/{job_id}/motion-review")
def start_motion_review(job_id: str, body: dict = Body(...)):
    job = get_job(job_id)
    fpv_name, radio_name = body.get("fpv_video"), body.get("radio_video") or None
    fpv_path = _input_video(job, "fpv", fpv_name)
    radio_path = _input_video(job, "radio", radio_name) if radio_name else None
    source = body.get("source", "auto")
    if source not in {"auto", "gyro", "visual"}:
        raise HTTPException(400, "Choose automatic, embedded motion or visual motion")
    try:
        fps = float(body.get("sample_fps", job["settings"].get("sample_fps", 30)))
        if not math.isfinite(fps) or not 1 <= fps <= 30:
            raise ValueError("Invalid extraction rate")
        ratio = validate_ratio(body.get("stick_sampling_ratio", job["settings"].get("stick_sampling_ratio")))
        radio_fps = tracking_fps(fps, ratio, _metadata(job, "radio", radio_name).get("fps")) if radio_name and ratio else fps
        profile = validate_rate_profile(body.get("rate_profile", job["settings"].get("rate_profile")))
        compare_fps = _extraction_fps(body.get("match_fps", job["settings"].get("match_fps", 30)))
        if compare_fps > fps:
            raise ValueError("Comparison frequency must not exceed the extraction frequency")
        minimum = float(body.get("min_overlap", job["settings"].get("min_overlap", 2)))
        if not math.isfinite(minimum) or not 1 <= minimum <= 3600:
            raise ValueError("Choose 1–3600 seconds minimum overlap")
        decode_mode = body.get("decode_mode", "auto")
        if decode_mode not in {"auto", "cpu", "cuda"}:
            raise ValueError("Choose automatic, CPU or CUDA decoding")
        _, uptilt, orientation, gyro_shift = validate_motion_settings(source, body.get("camera_uptilt_deg", 0), body.get("imu_orientation", "zXy"), body.get("gyro_video_offset", 0))
        visual_camera = validate_visual_camera(body.get("visual_camera", job["settings"].get("visual_camera")))
        visual_rate = visual_fps(body.get("fpv_sampling_mode", job["settings"].get("fpv_sampling_mode", "auto")), body.get("fpv_visual_fps", job["settings"].get("fpv_visual_fps", fps)), _metadata(job, "fpv", fpv_name).get("fps"))
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    request = {"fpv_video": fpv_name, "radio_video": radio_name, "fpv_path": str(fpv_path),
        "radio_path": str(radio_path) if radio_path else None, "source": source, "sample_fps": fps,
        "fpv_sample_fps": visual_rate, "visual_camera": visual_camera,
        "stick_sample_fps": radio_fps, "stick_sampling_ratio": ratio,
        "match_fps": compare_fps, "min_overlap": minimum,
        "calibration": job["calibrations"].get(radio_name), "rate_profile": profile,
        "camera_uptilt_deg": uptilt, "imu_orientation": orientation, "gyro_video_offset": gyro_shift, "decode_mode": decode_mode, "tracking_backend": _tracking_mode(body.get("tracking_backend", job["settings"].get("tracking_backend", "auto"))),
        "fpv_identity": _fpv_source_identity(_record(job, "fpv", fpv_name)),
        "radio_identity": _fpv_source_identity(_record(job, "radio", radio_name)) if radio_name else None,
        "signatures": {kind: [p.stat().st_size, p.stat().st_mtime_ns]
                       for kind, p in (("fpv", fpv_path), ("radio", radio_path)) if p}, "version": 7, "visual_version": VISUAL_VERSION, "telemetry_version": TELEMETRY_VERSION, "stick_version": STICK_VERSION, "outline_version": OUTLINE_VERSION}
    if job["calibrations"].get(radio_name, {}).get("reference_time", 0) > 0:
        request["stick_span_version"] = FULL_SPAN_VERSION
    chart_id = hashlib.sha256(json.dumps(request, sort_keys=True, allow_nan=False).encode()).hexdigest()[:20]
    directory = _chart_directory(job_id, chart_id)
    with CHART_LOCK:
        previous = get_job(job_id).get("charts", {}).get(chart_id)
        if previous and previous["status"] == "complete" and (directory / "review.json").exists():
            return {"id": chart_id, "status": "complete"}
        if (job_id, chart_id) in CHART_TASKS:
            return {"id": chart_id, "status": previous["status"]}
        if len(CHART_TASKS) >= 2:
            raise HTTPException(409, "Two motion reviews are already queued. Cancel one or wait for it to finish.")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "progress.json").unlink(missing_ok=True)
        atomic_json(directory / "request.json", {**request, "directory": str(directory), "cache": str(jobs.DATA / "cache")})
        jobs.update(job_id, lambda current: current.setdefault("charts", {}).update({chart_id: {"status": "queued", "message": "Queued for signal extraction"}}))
        with PROCESS_LOCK:
            CANCELLED.discard(f"chart:{job_id}:{chart_id}")
        CHART_TASKS[(job_id, chart_id)] = CHART_POOL.submit(_chart_worker, job_id, chart_id)
    return {"id": chart_id, "status": "queued"}


@app.post("/api/jobs/{job_id}/motion-review/{chart_id}/gyro-scan")
def start_gyro_scan(job_id: str, chart_id: str):
    job = get_job(job_id)
    parent = _chart_directory(job_id, chart_id)
    if job.get("charts", {}).get(chart_id, {}).get("status") != "complete" or not (parent / "radio.npz").exists():
        raise HTTPException(409, "Load calibrated StickCam motion signals first")
    original = json.loads((parent / "request.json").read_text())
    radio_name = original["radio_video"]
    radio_path = _input_video(job, "radio", radio_name)
    radio_signature = [radio_path.stat().st_size, radio_path.stat().st_mtime_ns]
    if radio_signature != original["signatures"]["radio"] or job["calibrations"].get(radio_name) != original["calibration"]:
        raise HTTPException(409, "StickCam source or calibration changed; load motion signals again")
    clips = []
    for record in job["inputs"]["fpv"]:
        path = _input_video(job, "fpv", record["name"])
        stat = path.stat()
        clips.append({"name": record["name"], "path": str(path), "identity": _fpv_source_identity(record),
                      "signature": [stat.st_size, stat.st_mtime_ns]})
    settings = job["settings"]
    fps = float(original.get("match_fps", settings.get("match_fps", 30)))
    minimum = float(original.get("min_overlap", settings.get("min_overlap", 2)))
    threshold = float(settings.get("minimum_score", .55))
    if not 1 <= fps <= original["sample_fps"] or not 1 <= minimum <= 3600 or not 0 <= threshold <= 1:
        raise HTTPException(400, "Invalid matching settings")
    request = {"kind": "gyro-scan", "version": 2, "telemetry_version": TELEMETRY_VERSION, "parent": chart_id, "radio_video": radio_name,
        "radio_path": str(radio_path), "radio_signature": radio_signature, "radio_signals": str(parent / "radio.npz"),
        "clips": clips, "match_fps": fps, "min_overlap": minimum, "minimum_score": threshold,
        **{key: original.get(key, default) for key, default in [("rate_profile", None), ("camera_uptilt_deg", 0),
            ("imu_orientation", "zXy"), ("gyro_video_offset", 0)]}}
    scan_id = hashlib.sha256(json.dumps(request, sort_keys=True, allow_nan=False).encode()).hexdigest()[:20]
    directory = _chart_directory(job_id, scan_id)
    with CHART_LOCK:
        previous = get_job(job_id).get("charts", {}).get(scan_id)
        if previous and previous["status"] == "complete" and (directory / "scan.json").exists():
            return {"id": scan_id, "status": "complete"}
        if (job_id, scan_id) in CHART_TASKS:
            return {"id": scan_id, "status": previous["status"]}
        if len(CHART_TASKS) >= 2:
            raise HTTPException(409, "Two signal tasks are already queued; cancel one or wait")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "progress.json").unlink(missing_ok=True)
        atomic_json(directory / "request.json", {**request, "directory": str(directory), "cache": str(jobs.DATA / "cache")})
        jobs.update(job_id, lambda current: current.setdefault("charts", {}).update({scan_id: {
            "kind": "gyro-scan", "status": "queued", "message": "Queued for session gyro comparison"}}))
        with PROCESS_LOCK:
            CANCELLED.discard(f"chart:{job_id}:{scan_id}")
        CHART_TASKS[(job_id, scan_id)] = CHART_POOL.submit(_chart_worker, job_id, scan_id)
    return {"id": scan_id, "status": "queued"}


@lru_cache(maxsize=32)
def _review_sections(directory_name, offset, fpv_modified, radio_modified):
    directory = Path(directory_name)
    request = json.loads((directory / "request.json").read_text())
    with np.load(directory / "fpv.npz", allow_pickle=False) as values:
        fpv = dict(values)
    if str(fpv["kind"].item()) == "visual_rotation":
        fpv = visual_body_rates(fpv, request.get("camera_uptilt_deg", 0))
    elif str(fpv["kind"].item()) != "visual":
        fpv = body_rates(fpv, request.get("camera_uptilt_deg", 0), request.get("imu_orientation", "zXy"), request.get("gyro_video_offset", 0))
    with np.load(directory / "radio.npz", allow_pickle=False) as values:
        radio = dict(values)
    fps = min(request.get("match_fps", 30), request["sample_fps"])
    r, f = prepare(radio, fps, True), prepare(fpv, fps, False)
    # Polarity is fixed across the whole overlap, not fitted independently per section.
    from fpv_matcher.matching.correlation import compare
    alignment = compare(r, f, fps, 2, (offset - 1 / fps, offset + 1 / fps))
    return section_support(r, f, fps, offset, alignment.details, include_windows=True)


def _queue_signal_task(job_id, request, result_name):
    chart_id = hashlib.sha256(json.dumps(request, sort_keys=True, allow_nan=False).encode()).hexdigest()[:20]
    directory = _chart_directory(job_id, chart_id)
    with CHART_LOCK:
        previous = get_job(job_id).get("charts", {}).get(chart_id)
        if previous and previous["status"] == "complete" and (directory / result_name).exists():
            return {"id": chart_id, "status": "complete"}
        if (job_id, chart_id) in CHART_TASKS:
            return {"id": chart_id, "status": previous["status"]}
        if len(CHART_TASKS) >= 2:
            raise HTTPException(409, "Two signal tasks are already queued; wait or cancel one")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "progress.json").unlink(missing_ok=True)
        atomic_json(directory / "request.json", {**request, "directory": str(directory), "cache": str(jobs.DATA / "cache")})
        jobs.update(job_id, lambda current: current.setdefault("charts", {}).update({chart_id: {
            "kind": request["kind"], "status": "queued", "message": "Queued for signal preparation"}}))
        with PROCESS_LOCK:
            CANCELLED.discard(f"chart:{job_id}:{chart_id}")
        CHART_TASKS[(job_id, chart_id)] = CHART_POOL.submit(_chart_worker, job_id, chart_id)
    return {"id": chart_id, "status": "queued"}


@app.get("/api/jobs/{job_id}/references")
def reference_pairs(job_id: str):
    return {"references": get_job(job_id).get("references", [])}


@app.put("/api/jobs/{job_id}/references")
def label_reference(job_id: str, body: dict = Body(...)):
    job = get_job(job_id)
    radio, fpv = body.get("radio_video"), body.get("fpv_video")
    _record(job, "radio", radio); _record(job, "fpv", fpv)
    if not isinstance(body.get("match"), bool):
        raise HTTPException(400, "Choose known match or known non-match")
    offset = body.get("expected_offset")
    if offset is not None:
        try:
            offset = float(offset)
            if not math.isfinite(offset):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(400, "Expected offset must be a finite number or omitted") from None
    ref = {"radio_video": radio, "fpv_video": fpv, "fpv": "fpv:" + fpv, "stick": "radio:" + radio,
           "match": body["match"], "expected_offset": offset, "tolerance": .15,
           "source_signatures": _decision_signatures(job, radio, fpv)}
    def change(current):
        previous = current.get("references", [])
        current["references"] = [r for r in previous if (r["radio_video"], r["fpv_video"]) != (radio, fpv)] + [ref]
    jobs.update(job_id, change)
    return {"saved": True}


@app.post("/api/jobs/{job_id}/validate-audio")
def validate_reference_audio(job_id: str, body: dict = Body(...)):
    from fpv_matcher.audio.evidence import POLICY_VERSION
    job = get_job(job_id)
    references = [r for r in job.get("references", []) if _decision_current(job, r)]
    if not references:
        raise HTTPException(400, "Label at least one known matching or non-matching pair with current sources first")
    videos = []
    for kind, records in job["inputs"].items():
        for record in records:
            if kind == "fpv" and not any(r["fpv_video"] == record["name"] for r in references):
                continue
            if kind == "radio" and job.get("calibrations", {}).get(record["name"], {}).get("exclude"):
                continue
            try:
                source = _input_video(job, kind, record["name"])
                videos.append({"id": kind + ":" + record["name"], "kind": "stick" if kind == "radio" else "fpv",
                    "name": record["name"], "path": str(source), "metadata": _metadata(job, kind, record["name"]),
                    "signature": [source.stat().st_size, source.stat().st_mtime_ns], "identity": _fpv_source_identity(record)})
            except (OSError, HTTPException):
                continue
    boundary = job["settings"].get("audio_boundary", 30)
    request = {"kind": "audio-validation", "policy": POLICY_VERSION, "document": {"videos": videos, "references": references,
               "min_overlap": job["settings"].get("min_overlap", 2)}, "boundary": boundary}
    return _queue_signal_task(job_id, request, "validation.json")


@app.get("/api/diagnostics")
def installation_report(job_id: str | None = None):
    from fpv_matcher.diagnostics.system import report
    return report(get_job(job_id) if job_id else None)


def _extraction_fps(value):
    try:
        fps = float(value)
        if not math.isfinite(fps) or not 1 <= fps <= 30:
            raise ValueError()
        return fps
    except (ValueError, TypeError):
        raise HTTPException(400, "Choose an extraction rate from 1 to 30 Hz") from None


@app.put("/api/jobs/{job_id}/session-radio")
def confirm_session_radio(job_id: str, body: dict = Body(...)):
    job = get_job(job_id)
    if job["status"] in {"queued", "running", "cancelling"}:
        raise HTTPException(409, "Wait for matching to finish before changing the session radio")
    if any(task.get("kind") in {"radio-orientation", "calibration-scan"} and task["status"] in {"queued", "running"} for task in job.get("charts", {}).values()):
        raise HTTPException(409, "Cancel or finish active radio checks/calibration searches before changing the session radio")
    name = body.get("radio_video")
    path = _input_video(job, "radio", name)
    config = job["calibrations"].get(name)
    if not config or config.get("skip_motion") or config.get("exclude") or len(config.get("radio_outline", [])) != 4:
        raise HTTPException(400, "Save a calibration with the radio outline and both sticks before confirming this reference")
    info = _metadata(job, "radio", name)
    try:
        config = validate_calibration(config, (info["width"], info["height"]))
        if any("tip_x" not in config[s] for s in ("left_gimbal", "right_gimbal")):
            raise ValueError("Confirm both stick tips before saving the session radio")
        label = body.get("label", "")
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > 100:
            raise ValueError("Enter a radio name/model (up to 100 characters); a custom label is fine")
        from fpv_matcher.radio.orientation import rotation_from_regions
        if rotation_from_regions({s: config[s] for s in ("left_gimbal", "right_gimbal")})["rotation_deg"] != config.get("radio_rotation_deg", 0):
            raise ValueError("Check physical left/right gimbal labels and orientation first; use a clearer, more level reference frame")
        config["radio_orientation_confirmed"] = True
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    profile = {"label": "Other" if label.strip() == "My session radio" else label.strip(), "model_id": model_id(label), "name": name, "path": str(path), "revision": uuid.uuid4().hex,
               "signature": [path.stat().st_size, path.stat().st_mtime_ns], "calibration": config,
               "width": info["width"], "height": info["height"], "reference_time": config["reference_time"],
               "radio_rotation_deg": config.get("radio_rotation_deg", 0)}
    def save(current):
        current.update(radio_profile=profile, radio_orientation_checks={})
        for task in current.get("charts", {}).values():
            if task.get("kind") == "calibration-scan":
                task["superseded"] = True
    jobs.update(job_id, save)
    return {"profile": {k: profile[k] for k in ("label", "revision", "name", "reference_time", "radio_rotation_deg")}}


@app.post("/api/jobs/{job_id}/radio-orientation")
def check_radio_orientation(job_id: str, body: dict = Body(default={})):
    from fpv_matcher.radio.orientation import VERSION
    job = get_job(job_id)
    if job["status"] in {"queued", "running", "cancelling"}:
        raise HTTPException(409, "Wait for matching to finish before starting orientation checks")
    profile = job.get("radio_profile")
    if not profile:
        raise HTTPException(400, "Confirm one calibrated radio as this session's reference first")
    path = _input_video(job, "radio", profile["name"])
    if [path.stat().st_size, path.stat().st_mtime_ns] != profile["signature"]:
        raise HTTPException(409, "The session radio reference source changed; confirm it again")
    name = body.get("radio_video")
    records = [_record(job, "radio", name)] if name else job["inputs"]["radio"]
    clips = []
    for record in records:
        config = job["calibrations"].get(record["name"], {})
        if config.get("exclude"):
            continue
        path = _input_video(job, "radio", record["name"])
        info = _metadata(job, "radio", record["name"])
        at = body.get("reference_time", config.get("reference_time", 0)) if name else config.get("reference_time", 0)
        try:
            at = float(at)
            if not math.isfinite(at) or not 0 <= at < info["duration"]:
                raise ValueError("Choose a calibration frame inside the clip")
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        clips.append({"name": record["name"], "path": str(path), "duration": info["duration"],
                      "calibration_time": at, "signature": [path.stat().st_size, path.stat().st_mtime_ns]})
    if not clips:
        raise HTTPException(400, "No included StickCam clips to check")
    return _queue_signal_task(job_id, {"kind": "radio-orientation", "version": VERSION, "reference": profile,
                              "profile_revision": profile["revision"], "clips": clips}, "orientation.json")


@app.get("/api/jobs/{job_id}/radio-orientation/{chart_id}/frame/{index}")
def radio_orientation_frame(job_id: str, chart_id: str, index: int):
    job = get_job(job_id)
    task = job.get("charts", {}).get(chart_id)
    directory = _chart_directory(job_id, chart_id)
    if not task or task.get("kind") != "radio-orientation" or task["status"] != "complete" or index < 0:
        raise HTTPException(404, "Orientation frame not available")
    result = json.loads((directory / "orientation.json").read_text())
    if index >= len(result["clips"]) or not result["clips"][index].get("image_file"):
        raise HTTPException(404, "Orientation frame not available")
    return FileResponse(directory / f"frame-{index}.jpg", media_type="image/jpeg")


@app.post("/api/jobs/{job_id}/calibration-scan")
def start_calibration_scan(job_id: str, body: dict = Body(...)):
    from fpv_matcher.radio.calibration_scan import VERSION
    job = get_job(job_id)
    try:
        seconds, start = float(body.get("search_seconds", 60)), float(body.get("start", 0))
        if not math.isfinite(seconds) or not 5 <= seconds <= 600 or not math.isfinite(start) or start < 0:
            raise ValueError("Search 5–600 seconds, starting at a nonnegative time")
        from fpv_matcher.radio.modes import validate_mode
        validate_mode(body.get("stick_mode", job["settings"].get("stick_mode", 2)))
        mode = body.get("stickcam_mode", "wonky")
        if mode not in {"fixed", "wonky"}:
            raise ValueError("Choose Fixed or Wonky StickCam")
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    name = body.get("radio_video")
    records = [_record(job, "radio", name)] if name else job["inputs"]["radio"]
    clips, references = [], []
    for record in records:
        config = job["calibrations"].get(record["name"], {})
        complete = all(all(key in config.get(side, {}) for key in ("tip_x", "tip_y")) for side in ("left_gimbal", "right_gimbal"))
        if not name and (complete or config.get("skip_motion") or config.get("exclude")):
            # Preserve confirmed/manual work. The explicit selected-clip action can seek another proposal.
            continue
        path = _input_video(job, "radio", record["name"])
        clips.append({"name": record["name"], "path": str(path), "start": start if name else 0,
                      "stickcam_mode": config.get("stickcam_mode", mode if name else "wonky"),
                      "stick_mode": config.get("stick_mode", body.get("stick_mode", job["settings"].get("stick_mode", 2))),
                      "signature": [path.stat().st_size, path.stat().st_mtime_ns]})
    if not clips:
        raise HTTPException(400, "All recordings already have saved calibrations. Use Find radio on one selected clip to propose a replacement.")
    profile = job.get("radio_profile")
    if profile:
        path = _input_video(job, "radio", profile["name"])
        if [path.stat().st_size, path.stat().st_mtime_ns] != profile["signature"]:
            raise HTTPException(409, "Session radio reference source changed; confirm a new reference")
        references.append({"name": profile["name"], "path": str(path), "calibration": profile["calibration"],
                           "signature": profile["signature"]})
    for reference_name, config in job["calibrations"].items():
        if profile and (reference_name == profile["name"] or not config.get("radio_orientation_confirmed")):
            continue
        if config.get("skip_motion") or config.get("exclude") or len(config.get("radio_outline", [])) != 4:
            continue
        path = _input_video(job, "radio", reference_name)
        references.append({"name": reference_name, "path": str(path), "calibration": config,
                           "signature": [path.stat().st_size, path.stat().st_mtime_ns]})
        if len(references) >= 3:
            break
    request = {"kind": "calibration-scan", "version": VERSION, "clips": clips,
               "references": references, "search_seconds": seconds,
               "profile_revision": (job.get("radio_profile") or {}).get("revision")}
    choice = body.get("radio_model", (profile or {}).get("model_id", "auto"))
    if choice not in {"auto", "other", *(m["id"] for m in RADIO_MODELS)}:
        raise HTTPException(400, "Choose a listed radio model, Auto-detect or Other")
    request.update(radio_model=choice, radio_model_version=RADIO_MODEL_VERSION)
    return _queue_signal_task(job_id, request, "calibration-review.json")


def _fpv_source_identity(record):
    # A previously hashed upload may have been replaced outside the application.
    try:
        stat = Path(record["path"]).stat()
        if record.get("upload_signature") == [stat.st_size, stat.st_mtime_ns]:
            return record.get("sha256")
    except OSError:
        pass
    return None


def _modified_time_status(job, record):
    try:
        path = Path(record["path"])
        stat = path.stat()
        signature = [stat.st_size, stat.st_mtime_ns]
        if record.get("camera_modified_time") is not None and record.get("content_signature") == signature:
            timestamp = float(record["camera_modified_time"])
            source = "saved original file date"
        elif job["source"] == "upload" and record.get("original_modified_ms") is not None and record.get("upload_signature") == signature:
            timestamp = float(record["original_modified_ms"]) / 1000
            source = "original file date"
        else:
            timestamp = stat.st_mtime
            source = "server file date"
        value = datetime.fromtimestamp(timestamp, timezone.utc)
        return {"timestamp": timestamp, "display": value.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "source": source, "suspect": value.year <= 1971}
    except (OSError, OverflowError, ValueError, TypeError):
        return {"timestamp": None, "display": None, "source": "unavailable", "suspect": True}


def _filename_time_status(name):
    timestamp = filename_timestamp(name)
    if timestamp is None:
        return {"timestamp": None, "display": None, "suspect": False}
    try:
        value = datetime.fromtimestamp(timestamp, timezone.utc)
        return {"timestamp": timestamp, "display": value.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "suspect": value.year <= 1971}
    except (OverflowError, ValueError, OSError):
        return {"timestamp": None, "display": None, "suspect": True}


def _audio_identifier_status(record, metadata):
    if not metadata:
        return {"stream": "unknown", "fingerprint": "unknown"}
    if not metadata.get("has_audio"):
        return {"stream": "absent", "fingerprint": "unavailable"}
    try:
        from fpv_matcher.audio.fingerprint import ALGORITHM_VERSION, validate_audio
        from fpv_matcher.cache.fingerprints import _key

        prepared, corrupt = [], False
        for boundary in (30, 60, 120, 300):
            config = {"boundary_seconds": boundary}
            key = _key(record["path"], ALGORITHM_VERSION, config, _fpv_source_identity(record))
            cached = jobs.DATA / "cache" / f"audio.{key}.npz"
            if not cached.is_file():
                continue
            try:
                with np.load(cached, allow_pickle=False) as saved:
                    if str(saved["cache_key"].item()) != key:
                        raise ValueError("Invalid fingerprint key")
                    data = {name: saved[name] for name in ("time", "features", "valid", "segments", "quality", "duration", "hashes", "hash_times")}
                validate_audio(data)
                prepared.append(boundary)
                if len(data["time"]) > 0 and float(data["quality"]) > 0:
                    return {"stream": "present", "fingerprint": "cached_useful", "boundary_seconds": boundary}
            except (OSError, ValueError, KeyError, EOFError, BadZipFile):
                corrupt = True
        return {"stream": "present", "fingerprint": "cached_empty" if prepared else "corrupt" if corrupt else "not_prepared",
                "prepared_boundaries": prepared}
    except (OSError, ValueError, KeyError, EOFError, BadZipFile):
        return {"stream": "present", "fingerprint": "corrupt"}


@app.get("/api/jobs/{job_id}/fpv-data")
def fpv_data_status(job_id: str, sample_fps: float | None = None, sampling_mode: str | None = None):
    job = get_job(job_id)
    fps = _extraction_fps(sample_fps if sample_fps is not None else job["settings"].get("fpv_visual_fps", 30))
    mode = sampling_mode or job["settings"].get("fpv_sampling_mode", "auto")
    if mode not in VISUAL_SAMPLING_MODES:
        raise HTTPException(400, "Choose automatic, half, quarter or custom visual sampling")
    clips = []
    tasks = [{"id": key, **value} for key, value in job.get("charts", {}).items() if value.get("kind") == "fpv-preparation"]
    logs = []
    for task in tasks:
        directory = _chart_directory(job_id, task["id"])
        result = directory / ("preparation.json" if task["status"] == "complete" else "progress.json")
        try:
            logs.append((result.stat().st_mtime_ns, json.loads(result.read_text())))
        except (OSError, ValueError):
            pass
    latest = {}
    for _, log in sorted(logs, key=lambda item: item[0], reverse=True):
        for row in log.get("clips", []):
            latest.setdefault(row["name"], {key: row.get(key) for key in ("status", "message", "elapsed_seconds", "reused", "sample_fps")})
    for record in job["inputs"]["fpv"]:
        try:
            metadata = _metadata(job, "fpv", record["name"])
        except HTTPException:
            metadata = record.get("metadata")
        source_fps = (metadata or {}).get("fps")
        try:
            requested_fps = visual_fps(mode, fps, source_fps)
        except ValueError:
            requested_fps = None
        embedded = embedded_status(record["path"], jobs.DATA / "cache", _fpv_source_identity(record))
        visual = fpv_cache_status(record["path"], jobs.DATA / "cache", requested_fps, _fpv_source_identity(record), job["settings"].get("visual_camera")) if requested_fps else {"status": "not_derived"}
        if visual["status"] == "not_derived":
            # Keep alternate sampling rates visible without loading every cached array.
            rates = {15., 30., float(job["settings"].get("sample_fps", 30))}
            rates.update(float(row["sample_fps"]) for _, log in logs for row in log.get("clips", []) if row.get("name") == record["name"] and row.get("sample_fps"))
            for rate in sorted(rates - {requested_fps}, reverse=True):
                saved = fpv_cache_status(record["path"], jobs.DATA / "cache", rate, _fpv_source_identity(record), job["settings"].get("visual_camera"))
                if saved["status"] == "cached":
                    visual = {**saved, "status": "other_rate"}
                    break
        clips.append({"name": record["name"], "metadata": metadata, "audio": _audio_identifier_status(record, metadata),
                      "filename_time": _filename_time_status(record["name"]),
                      "modified_time": _modified_time_status(job, record), "embedded": embedded,
                      "visual": visual, "source_fps": source_fps, "requested_fps": requested_fps,
                      "last_preparation": latest.get(record["name"])})
    return {"clips": clips, "sample_fps": fps, "sampling_mode": mode, "tasks": tasks,
            "cached": sum(c["visual"]["status"] == "cached" for c in clips),
            "embedded": sum(c["embedded"]["status"] == "available" for c in clips)}


@app.get("/api/jobs/{job_id}/fpv-data/{filename}/visual")
def saved_fpv_visual(job_id: str, filename: str, sample_fps: float,
                     start: float | None = None, end: float | None = None, fill_gap_seconds: float = 0.):
    job = get_job(job_id)
    source = _input_video(job, "fpv", filename)
    fps = _extraction_fps(sample_fps)
    try:
        data = load_saved_visual(source, jobs.DATA / "cache", fps, _fpv_source_identity(_record(job, "fpv", filename)), job["settings"].get("visual_camera"))
    except (OSError, ValueError, KeyError, EOFError, BadZipFile) as exc:
        raise HTTPException(409, "Saved visual motion is unavailable at this rate. Refresh the table or derive this clip first.") from exc
    info = _metadata(job, "fpv", filename)
    a, b = 0. if start is None else start, info["duration"] if end is None else end
    if not all(math.isfinite(v) for v in (a, b)) or not 0 <= a < b <= info["duration"]:
        raise HTTPException(400, "Choose a time window within the FPV video")
    data = visual_body_rates(data, job["settings"].get("camera_uptilt_deg", 0))
    try:
        display = display_interpolation(data, fill_gap_seconds)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    channels = [(key, key.title() + " · visual estimate", "°/s") for key in ("roll", "pitch", "yaw")]
    return {"fpv_video": filename, "sample_fps": fps, "window": [a, b], "duration": info["duration"],
            "motion_kind": "visual_rotation", "note": str(data["note"].item()), "visual_camera": validate_visual_camera(job["settings"].get("visual_camera")),
            "camera_uptilt_deg": job["settings"].get("camera_uptilt_deg", 0),
            "previous_algorithm": bool(data.get("previous_algorithm", False)), "gap_reasons": gap_summary(data), "filled_samples": int(display["_interpolated"].sum()), "fill_gap_seconds": fill_gap_seconds,
            "tracking_fps": float(data["tracking_fps"]) if "tracking_fps" in data else fps,
            "tracking_label": str(data["tracking_label"].item()) if "tracking_label" in data else "Earlier CPU extraction",
            "traces": [chart_trace(display, key, label, unit, a, b) for key, label, unit in channels],
            "usable_percent": round(float(np.mean(data["quality"] > .1)) * 100, 1)}


@app.post("/api/jobs/{job_id}/prepare-fpv")
def prepare_session_fpv(job_id: str, body: dict = Body(...)):
    job = get_job(job_id)
    if job["status"] in {"queued", "running", "cancelling"}:
        raise HTTPException(409, "Wait for matching to finish before preparing FPV motion")
    if any(task.get("kind") in {"fpv-preparation", "stick-derivation"} and task["status"] in {"queued", "running"} for task in job.get("charts", {}).values()):
        raise HTTPException(409, "Motion preparation is already active; wait or cancel it first")
    action = body.get("action", "derive")
    if action not in {"check", "derive"}:
        raise HTTPException(400, "Choose check or derive")
    names = body.get("names")
    if names is not None and (not isinstance(names, list) or not names or any(not isinstance(n, str) for n in names)):
        raise HTTPException(400, "Select one or more FPV recordings")
    available = {r["name"] for r in job["inputs"]["fpv"]}
    if names is not None and not set(names).issubset(available):
        raise HTTPException(400, "An FPV recording is not part of this session")
    try:
        visual_camera = validate_visual_camera(body.get("visual_camera", job["settings"].get("visual_camera")))
        _, uptilt, _, _ = validate_motion_settings("visual", body.get("camera_uptilt_deg", job["settings"].get("camera_uptilt_deg", 0)))
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    fps = _extraction_fps(body.get("sample_fps", job["settings"].get("fpv_visual_fps", 30)))
    mode = body.get("sampling_mode", job["settings"].get("fpv_sampling_mode", "auto"))
    if mode not in VISUAL_SAMPLING_MODES:
        raise HTTPException(400, "Choose automatic, half, quarter or custom visual sampling")
    decode = body.get("decode_mode", "auto")
    if decode not in {"auto", "cpu", "cuda"}:
        raise HTTPException(400, "Choose automatic, CPU or CUDA decoding")
    clips = []
    for record in job["inputs"]["fpv"]:
        if names is not None and record["name"] not in names:
            continue
        path = _input_video(job, "fpv", record["name"])
        try:
            rate = visual_fps(mode, fps, _metadata(job, "fpv", record["name"]).get("fps"))
        except ValueError as exc:
            raise HTTPException(400, f"{record['name']}: {exc}") from exc
        clips.append({"name": record["name"], "path": str(path), "identity": _fpv_source_identity(record),
                      "signature": [path.stat().st_size, path.stat().st_mtime_ns], "sample_fps": rate})
    if not clips:
        raise HTTPException(400, "Add FPV videos first")
    request = {"kind": "fpv-preparation", "action": action, "clips": clips, "sample_fps": fps,
               "sampling_mode": mode, "visual_camera": visual_camera,
               "decode_mode": decode, "tracking_backend": _tracking_mode(body.get("tracking_backend", job["settings"].get("tracking_backend", "auto"))), "algorithm": VISUAL_VERSION, "telemetry_version": TELEMETRY_VERSION}
    # Re-run incomplete tasks or deleted/corrupt caches; keep successful results reusable.
    if any(embedded_status(c["path"], jobs.DATA / "cache", c["identity"])["status"] == "unknown" or
           (action == "derive" and embedded_status(c["path"], jobs.DATA / "cache", c["identity"])["status"] == "unavailable" and
            fpv_cache_status(c["path"], jobs.DATA / "cache", c["sample_fps"], c["identity"], visual_camera)["status"] != "cached") for c in clips):
        request["attempt"] = uuid.uuid4().hex
    jobs.update(job_id, lambda current: current["settings"].update(fpv_visual_fps=fps, fpv_sampling_mode=mode, visual_camera=visual_camera, camera_uptilt_deg=uptilt, tracking_backend=request["tracking_backend"]))
    return _queue_signal_task(job_id, request, "preparation.json")


@app.get("/api/jobs/{job_id}/stick-data")
def stick_data_status(job_id: str, sample_fps: float | None = None, stick_sampling_ratio: str | None = None):
    job = get_job(job_id)
    fps = _extraction_fps(sample_fps if sample_fps is not None else job["settings"].get("sample_fps", 30))
    try:
        ratio = validate_ratio(stick_sampling_ratio if stick_sampling_ratio is not None else job["settings"].get("stick_sampling_ratio"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    clips = []
    previous_fps = float(job["settings"].get("sample_fps", 30))
    for clip in job["inputs"]["radio"]:
        try:
            metadata = _metadata(job, "radio", clip["name"])
        except HTTPException:
            metadata = clip.get("metadata")
        source_fps = (metadata or {}).get("fps")
        try:
            if ratio:
                source_fps = _metadata(job, "radio", clip["name"]).get("fps")
            requested = tracking_fps(fps, ratio, source_fps)
        except (ValueError, HTTPException) as exc:
            clips.append({"name": clip["name"], "metadata": metadata, "audio": _audio_identifier_status(clip, metadata),
                          "filename_time": _filename_time_status(clip["name"]),
                          "modified_time": _modified_time_status(job, clip), "status": "unavailable", "source_fps": source_fps,
                          "sampling_error": str(exc.detail if isinstance(exc, HTTPException) else exc)})
            continue
        calibration = job["calibrations"].get(clip["name"])
        status = stick_cache_status(clip["path"], jobs.DATA / "cache", calibration, requested, _fpv_source_identity(clip))
        # Other rates remain saved: switching back can reuse them immediately.
        if status["status"] == "not_derived":
            rates = {fps, previous_fps}
            if source_fps and math.isfinite(source_fps) and source_fps > 0:
                rates.update(round(source_fps * r, 8) for r in (0.5, 0.25) if source_fps * r <= 60)
            for saved_fps in sorted(rates - {requested}, reverse=True):
                previous = stick_cache_status(clip["path"], jobs.DATA / "cache", calibration, saved_fps, _fpv_source_identity(clip))
                if previous["status"] == "cached":
                    status = {**previous, "status": "other_rate"}
                    break
        activity_calibration = calibration
        try:
            stat = Path(clip["path"]).stat()
            expected = job.get("calibration_signatures", {}).get(clip["name"])
            if expected and expected != [stat.st_size, stat.st_mtime_ns]:
                activity_calibration = None
        except OSError:
            activity_calibration = None
        activity = activity_cache_status(clip["path"], jobs.DATA / "cache", activity_calibration, _fpv_source_identity(clip))
        clips.append({"name": clip["name"], "metadata": metadata, "audio": _audio_identifier_status(clip, metadata), "gimbal_activity": activity,
                      "overall_movement":scene_cache_status(clip["path"],jobs.DATA/"cache",_fpv_source_identity(clip)),
                      "filename_time": _filename_time_status(clip["name"]),
                      "modified_time": _modified_time_status(job, clip), "source_fps": source_fps,
                      "requested_fps": requested, **status})
    tasks = [{"id": key, **value} for key, value in job.get("charts", {}).items() if value.get("kind") == "stick-derivation"]
    latest_derivation = {}
    task_logs = []
    for task in tasks:
        directory = _chart_directory(job_id, task["id"])
        result_path = directory / ("derivation.json" if task["status"] == "complete" else "progress.json")
        if not result_path.is_file():
            continue
        try:
            task_logs.append((result_path.stat().st_mtime_ns, task, json.loads(result_path.read_text())))
        except (OSError, ValueError):
            continue
    for _, task, log in sorted(task_logs, key=lambda entry: entry[0], reverse=True):
        request_path = _chart_directory(job_id, task["id"]) / "request.json"
        try:
            sample_fps_used = json.loads(request_path.read_text()).get("sample_fps")
        except (OSError, ValueError):
            sample_fps_used = None
        for row in log.get("clips", []):
            if row["name"] not in latest_derivation:
                latest_derivation[row["name"]] = {key: row.get(key) for key in
                    ("elapsed_seconds", "extraction_seconds", "reused", "status", "sample_fps")}
                latest_derivation[row["name"]]["sample_fps"] = row.get("sample_fps", sample_fps_used)
        current_name = log.get("name")
        if current_name and current_name not in latest_derivation and log.get("elapsed_seconds") is not None:
            latest_derivation[current_name] = {"elapsed_seconds": log["elapsed_seconds"],
                "extraction_seconds": None, "reused": None,
                "status": task["status"] if task["status"] in {"queued", "running"} else task["status"],
                "sample_fps": log.get("sample_fps", sample_fps_used)}
    for clip in clips:
        if clip["name"] in latest_derivation:
            clip["last_derivation"] = latest_derivation[clip["name"]]
    return {"clips": clips, "sample_fps": fps, "stick_sampling_ratio": ratio, "cached": sum(c["status"] == "cached" for c in clips),
            "saved_other_rate": sum(c["status"] == "other_rate" for c in clips), "tasks": tasks}


def _stick_review_path(job_id, filename):
    key = hashlib.sha256(filename.encode("utf-8")).hexdigest()
    return jobs.job_dir(job_id) / "stick-review" / f"{key}.json"


def _stick_review_context(job, filename):
    record = _record(job, "radio", filename)
    path = _input_video(job, "radio", filename)
    info = _metadata(job, "radio", filename)
    stat = path.stat()
    signature = [stat.st_size, stat.st_mtime_ns]
    calibration = job.get("calibrations", {}).get(filename)
    valid_calibration = None
    expected_calibration_source = job.get("calibration_signatures", {}).get(filename)
    calibration_matches_source = not expected_calibration_source or expected_calibration_source == signature
    if calibration and not calibration.get("source_changed") and calibration_matches_source:
        try:
            valid_calibration = validate_calibration(calibration)
        except ValueError:
            valid_calibration = None
    calibration_digest = None
    if valid_calibration:
        geometry = {key: value for key, value in valid_calibration.items() if key not in {"tracking_landmarks", "capture_tracking_geometry"}}
        canonical = json.dumps(geometry, sort_keys=True, separators=(",", ":"), allow_nan=False)
        calibration_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    fps = _extraction_fps(job["settings"].get("sample_fps", 30))
    ratio = validate_ratio(job["settings"].get("stick_sampling_ratio"))
    requested_fps = tracking_fps(fps, ratio, info.get("fps")) if ratio else fps
    cache = {"status": "needs_calibration"}
    if valid_calibration:
        cache = stick_cache_status(path, jobs.DATA / "cache", valid_calibration, requested_fps, _fpv_source_identity(record))
    return record, path, info, signature, valid_calibration, calibration_digest, requested_fps, cache


@app.get("/api/jobs/{job_id}/stick-review/{filename}")
def get_stick_tracking_review(job_id: str, filename: str):
    job = get_job(job_id)
    try:
        _, path, info, signature, calibration, calibration_digest, requested_fps, cache = _stick_review_context(job, filename)
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    review_file = _stick_review_path(job_id, filename)
    saved = {}
    if review_file.is_file():
        try:
            saved = json.loads(review_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
    stale_source = bool(saved and saved.get("source_signature") != signature)
    annotations = [] if stale_source else saved.get("annotations", [])
    movement = None
    diagnostics = {"before": [], "current": []}
    if cache.get("status") == "cached" and calibration:
        try:
            data = cached_sticks(path, jobs.DATA / "cache", calibration, requested_fps, _fpv_source_identity(_record(job, "radio", filename)))
            count = len(data["time"])
            stride = max(1, math.ceil(count / 8000))
            indices = np.arange(0, count, stride, dtype=int)
            if count and indices[-1] != count - 1:
                indices = np.append(indices, count - 1)
            for side in ("left_tip", "right_tip"):
                if side + "_landmark" in data:
                    indices = np.union1d(indices, np.flatnonzero(data[side + "_landmark"])).astype(int)
            movement = {"time": np.asarray(data["time"])[indices].round(5).tolist(),
                        "axes": {axis: np.asarray(data[axis])[indices].round(5).tolist() for axis in ("throttle", "yaw", "pitch", "roll")},
                        "quality": np.asarray(data.get("quality", np.zeros(count)))[indices].round(4).tolist(),
                        "axis_quality": {axis: np.asarray(data.get(axis + "_quality", data.get("quality", np.zeros(count))))[indices].round(4).tolist()
                                         for axis in ("throttle", "yaw", "pitch", "roll")},
                        "sample_fps": requested_fps, "source_samples": count}
            movement["tracking"] = geometry_trace(data, calibration, info["width"], info["height"], indices)
            diagnostics["current"] = correction_diagnostics(data, calibration, review_landmarks(annotations, info["duration"], (info["width"], info["height"])), info["width"], info["height"])
            if all(f"outline_{corner}_{axis}" in data for corner in range(4) for axis in ("x", "y")):
                outline = np.asarray(
                    [[[data[f"outline_{corner}_x"][index], data[f"outline_{corner}_y"][index]] for corner in range(4)]
                     for index in indices], float)
                outline_quality = np.asarray(data.get("radio_orientation_quality", np.zeros(count)), float)[indices]
                invalid = ~np.isfinite(outline).all(axis=(1, 2))
                outline_quality = np.where(invalid, 0., np.nan_to_num(outline_quality, nan=0., posinf=0., neginf=0.))
                movement["radio_outline"] = np.nan_to_num(outline, nan=0., posinf=0., neginf=0.).round(2).tolist()
                movement["outline_quality"] = outline_quality.round(4).tolist()
            movement["landmark_events"] = [
                {"time": round(float(data["time"][index]), 5), **flags}
                for index in range(count)
                if (flags := {side: "applied" if data[side + "_landmark"][index] > 0 else "rejected"
                              for side in ("left_tip", "right_tip")
                              if side + "_landmark" in data and data[side + "_landmark"][index] != 0})
            ]
        except (OSError, ValueError, KeyError, EOFError, BadZipFile):
            cache = {"status": "unavailable"}
    try:
        landmarks = review_landmarks(annotations, info["duration"], (info["width"], info["height"]))
    except (ValueError, KeyError, TypeError):
        landmarks = []
    if not stale_source and landmarks and saved.get("baseline_calibration"):
        try:
            baseline = validate_calibration(saved["baseline_calibration"], (info["width"], info["height"]))
            baseline_fps = saved.get("baseline_sample_fps", requested_fps)
            identity = _fpv_source_identity(_record(job, "radio", filename))
            if stick_cache_status(path, jobs.DATA / "cache", baseline, baseline_fps, identity)["status"] == "cached":
                original = cached_sticks(path, jobs.DATA / "cache", baseline, baseline_fps, identity)
                diagnostics["before"] = correction_diagnostics(original, baseline, landmarks, info["width"], info["height"])
        except (OSError, ValueError, KeyError, EOFError, BadZipFile):
            pass
    return {"filename": filename, "duration": info["duration"], "width": info["width"], "height": info["height"],
            "source_signature": signature, "calibration_digest": calibration_digest, "calibration": calibration,
            "tracker_version": STICK_VERSION + OUTLINE_VERSION + FULL_SPAN_VERSION + LANDMARK_VERSION,
            "calibration_current": calibration is not None, "stick_mode": calibration.get("stick_mode", 2) if calibration else 2,
            "sample_fps": requested_fps, "cache": cache, "movement": movement,
            "annotations": annotations, "annotation_revision": saved.get("revision", 0),
            "tracking_landmark_count": len(landmarks), "landmark_diagnostics": diagnostics,
            "landmarks_pending": bool(landmarks and (not calibration or landmarks != calibration.get("tracking_landmarks", []))),
            "stale_source_annotations": len(saved.get("annotations", [])) if stale_source else 0,
            "calibration_changed": bool(saved and saved.get("calibration_digest") != calibration_digest)}


@app.put("/api/jobs/{job_id}/stick-review/{filename}")
def save_stick_tracking_review(job_id: str, filename: str, body: dict = Body(...)):
    job = get_job(job_id)
    try:
        record, path, info, signature, calibration, calibration_digest, requested_fps, _ = _stick_review_context(job, filename)
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    annotations = body.get("annotations")
    if not isinstance(annotations, list) or len(annotations) > 5000:
        raise HTTPException(400, "Review annotations must be a list of at most 5,000 points")
    cleaned = []
    for index, item in enumerate(annotations):
        if not isinstance(item, dict):
            raise HTTPException(400, f"Annotation {index + 1} must be an object")
        timestamp = item.get("time")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or not 0 <= timestamp <= info["duration"]:
            raise HTTPException(400, f"Annotation {index + 1} has a time outside this video")
        point = {"time": round(float(timestamp), 4)}
        for key in ("left_tip", "right_tip"):
            value = item.get(key)
            if value is None:
                continue
            if (not isinstance(value, list) or len(value) != 2 or
                    any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in value)):
                raise HTTPException(400, f"Annotation {index + 1} has invalid {key} coordinates")
            point[key] = [round(float(v), 6) for v in value]
        if "calibration" in item:
            try:
                point["calibration"] = validate_landmarks([{**point, "calibration": item["calibration"]}], info["duration"], (info["width"], info["height"]))[0]["calibration"]
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        controls = item.get("controls", {})
        if not isinstance(controls, dict) or set(controls) - {"throttle", "yaw", "pitch", "roll"}:
            raise HTTPException(400, f"Annotation {index + 1} has invalid control axes")
        if controls:
            cleaned_controls = {}
            for axis, value in controls.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not -1 <= value <= 1:
                    raise HTTPException(400, f"Annotation {index + 1} {axis} must be between -1 and +1")
                cleaned_controls[axis] = round(float(value), 4)
            point["controls"] = cleaned_controls
        note = item.get("note", "")
        if not isinstance(note, str) or len(note) > 500:
            raise HTTPException(400, f"Annotation {index + 1} note must be 500 characters or fewer")
        if note.strip():
            point["note"] = note.strip()
        if not any(key in point for key in ("left_tip", "right_tip", "calibration", "controls", "note")):
            raise HTTPException(400, f"Annotation {index + 1} has no tracking reference or expected control values")
        cleaned.append(point)
    try:
        landmarks = review_landmarks(cleaned, info["duration"], (info["width"], info["height"]))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    landmarks_changed = bool(calibration is not None and landmarks != calibration.get("tracking_landmarks", []))
    if landmarks_changed and (job["status"] in {"queued", "running", "cancelling"} or
            any(task.get("kind") == "stick-derivation" and task["status"] in {"queued", "running"}
                for task in job.get("charts", {}).values())):
        raise HTTPException(409, "Finish or cancel matching / StickCam derivation before changing tracking landmarks")
    review_file = _stick_review_path(job_id, filename)
    existing = {}
    if review_file.is_file():
        try:
            existing = json.loads(review_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
    if existing and existing.get("source_signature") != signature and not body.get("reset_stale_source"):
        raise HTTPException(409, "The video file changed since the saved tracking review. Start a new review before replacing those annotations.")
    revision = int(existing.get("revision", 0)) + 1 if existing.get("source_signature") == signature else 1
    saved = {"schema_version": 2, "filename": filename, "source_signature": signature,
             "calibration_digest": calibration_digest, "stick_mode": calibration.get("stick_mode", 2) if calibration else 2,
             "sample_fps": requested_fps, "duration": info["duration"], "width": info["width"], "height": info["height"],
             "revision": revision, "updated_at": datetime.now(timezone.utc).isoformat(), "annotations": cleaned}
    baseline = existing.get("baseline_calibration") if existing.get("source_signature") == signature else None
    baseline_fps = existing.get("baseline_sample_fps", requested_fps)
    if baseline is None:
        candidate = body.get("baseline_calibration") or calibration
        try:
            if candidate:
                candidate = validate_calibration(candidate, (info["width"], info["height"]))
                if stick_cache_status(path, jobs.DATA / "cache", candidate, requested_fps, _fpv_source_identity(record))["status"] == "cached":
                    baseline, baseline_fps = candidate, requested_fps
        except (ValueError, TypeError, KeyError, OSError):
            pass
    if baseline:
        saved.update(baseline_calibration=baseline, baseline_sample_fps=baseline_fps)
    atomic_json(review_file, saved)
    if calibration is not None:
        calibration = {**calibration}
        if landmarks:
            calibration["tracking_landmarks"] = landmarks
        else:
            calibration.pop("tracking_landmarks", None)
        if landmarks_changed:
            jobs.update(job_id, lambda current: current["calibrations"].update({filename: calibration}))
    return {"saved": True, "revision": revision, "annotations": cleaned,
            "calibration": calibration, "landmarks_changed": landmarks_changed,
            "landmarks_enabled": calibration is not None,
            "tracking_landmark_count": len(landmarks)}


@app.get("/api/jobs/{job_id}/stick-review/{filename}/tracking")
def stick_tracking_window(job_id: str, filename: str, start: float = 0, duration: float = 20):
    job = get_job(job_id)
    record, path, info, _, calibration, _, fps, cache = _stick_review_context(job, filename)
    if not math.isfinite(start) or not math.isfinite(duration) or not 0 <= start < info["duration"] or not 0 < duration <= 30:
        raise HTTPException(400, "Choose a tracking window inside this video, at most 30 seconds long")
    if cache.get("status") != "cached" or calibration is None:
        raise HTTPException(409, "Derive this clip with its current calibration and landmarks before loading tracking")
    data = cached_sticks(path, jobs.DATA / "cache", calibration, fps, _fpv_source_identity(record))
    end = min(info["duration"], start + duration)
    indices = np.flatnonzero((data["time"] >= start) & (data["time"] < end))
    result = {"start": start, "end": end, "time": data["time"][indices].round(5).tolist(), "sample_fps": fps,
              "tracking": geometry_trace(data, calibration, info["width"], info["height"], indices)}
    if all(f"outline_{corner}_{axis}" in data for corner in range(4) for axis in ("x", "y")):
        outline = np.asarray([np.column_stack((data[f"outline_{i}_x"][indices], data[f"outline_{i}_y"][indices])) for i in range(4)]).transpose(1, 0, 2)
        result["radio_outline"] = np.nan_to_num(outline, nan=0., posinf=0., neginf=0.).round(2).tolist()
        result["outline_quality"] = np.asarray(data.get("radio_orientation_quality", np.zeros(len(data["time"]))))[indices].round(4).tolist()
    return result


@app.post("/api/jobs/{job_id}/derive-sticks")
def derive_session_sticks(job_id: str, body: dict = Body(...)):
    job = get_job(job_id)
    if job["status"] in {"queued", "running", "cancelling"}:
        raise HTTPException(409, "Wait for matching to finish; it already derives and caches the required StickCam inputs")
    if any(task.get("kind") == "fpv-preparation" and task["status"] in {"queued", "running"} for task in job.get("charts", {}).values()):
        raise HTTPException(409, "Wait for FPV motion preparation to finish before deriving StickCam inputs")
    for key, task in job.get("charts", {}).items():
        if task.get("kind") == "stick-derivation" and task["status"] in {"queued", "running"}:
            return {"id": key, "status": task["status"]}
    fps = _extraction_fps(body.get("sample_fps", job["settings"].get("sample_fps", 30)))
    try:
        ratio = validate_ratio(body.get("stick_sampling_ratio", job["settings"].get("stick_sampling_ratio")))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    decode = body.get("decode_mode", "auto")
    if decode not in {"auto", "cpu", "cuda"}:
        raise HTTPException(400, "Choose automatic, CPU or CUDA decoding")
    selected_names = body.get("radio_names")
    if selected_names is not None and (not isinstance(selected_names, list) or not selected_names or any(not isinstance(n, str) for n in selected_names) or not set(selected_names) <= {r["name"] for r in job["inputs"]["radio"]}):
        raise HTTPException(400, "Choose existing StickCam filenames to prepare")
    clips = []
    for record in job["inputs"]["radio"]:
        if selected_names is not None and record["name"] not in selected_names:
            continue
        calibration = job["calibrations"].get(record["name"])
        if not calibration or calibration.get("skip_motion") or calibration.get("exclude"):
            continue
        path = _input_video(job, "radio", record["name"])
        expected_calibration = job.get("calibration_signatures", {}).get(record["name"])
        if expected_calibration and expected_calibration != [path.stat().st_size, path.stat().st_mtime_ns]:
            continue
        try:
            calibration = validate_calibration(calibration)
        except ValueError:
            continue
        try:
            info = _metadata(job, "radio", record["name"])
            radio_fps = tracking_fps(fps, ratio, info.get("fps")) if ratio else fps
        except ValueError as exc:
            raise HTTPException(400, f"{record['name']}: {exc}") from exc
        if body.get("capture_tracking_geometry"):
            calibration["capture_tracking_geometry"] = True
            # Activate earlier saved frame landmarks when this clip is reviewed
            # again. References belonging to changed source files stay inactive.
            review_file = _stick_review_path(job_id, record["name"])
            if review_file.is_file():
                try:
                    saved = json.loads(review_file.read_text(encoding="utf-8"))
                    if saved.get("source_signature") == [path.stat().st_size, path.stat().st_mtime_ns]:
                        landmarks = review_landmarks(saved.get("annotations", []), info["duration"], (info["width"], info["height"]))
                        if landmarks:
                            calibration["tracking_landmarks"] = landmarks
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            jobs.update(job_id, lambda current: current["calibrations"].update({record["name"]: calibration}))
        clips.append({"name": record["name"], "path": str(path), "identity": _fpv_source_identity(record),
                      "signature": [path.stat().st_size, path.stat().st_mtime_ns], "calibration": calibration,
                      "sample_fps": radio_fps, "duration_seconds": info["duration"]})
    if not clips:
        raise HTTPException(400, "Calibrate at least one StickCam video before deriving inputs")
    # A previous completed run can contain failed clips or a deleted cache: queue again if any current cache is missing.
    request = {"kind": "stick-derivation", "clips": clips, "sample_fps": fps, "decode_mode": decode, "tracking_backend": _tracking_mode(body.get("tracking_backend", job["settings"].get("tracking_backend", "auto"))),
               "stick_sampling_ratio": ratio,
               "algorithm": STICK_VERSION + OUTLINE_VERSION + FULL_SPAN_VERSION + LANDMARK_VERSION}
    if any(stick_cache_status(c["path"], jobs.DATA / "cache", c["calibration"], c["sample_fps"], c["identity"])["status"] != "cached" for c in clips):
        request["attempt"] = uuid.uuid4().hex
    jobs.update(job_id, lambda current: current["settings"].update(sample_fps=fps, stick_sampling_ratio=ratio, tracking_backend=request["tracking_backend"]))
    return {**_queue_signal_task(job_id, request, "derivation.json"),
            "calibrations": {clip["name"]: clip["calibration"] for clip in clips}}


def _queue_audio_review(job_id, original):
    job = get_job(job_id)
    from fpv_matcher.audio.fingerprint import ALGORITHM_VERSION as AUDIO_VERSION
    request = {"kind": "audio-review", "version": WAVEFORM_VERSION, "fingerprint_version": AUDIO_VERSION, "signatures": {}}
    if not original.get("fpv_video"):
        raise HTTPException(400, "Choose an FPV recording")
    try:
        row = next((r for r in _effective_results(job)["rows"] if r["fpv_video"] == original["fpv_video"] and r["radio_video"] == original.get("radio_video")), {})
        request["boundary_seconds"] = row.get("audio_boundary_seconds", job["settings"].get("audio_boundary", 30))
    except HTTPException:
        request["boundary_seconds"] = job["settings"].get("audio_boundary", 30)
    for kind in ("fpv", "radio"):
        name = original.get(kind + "_video")
        request[kind + "_video"] = name
        if name:
            path = _input_video(job, kind, name)
            request[kind + "_path"] = str(path)
            request[kind + "_identity"] = _fpv_source_identity(_record(job, kind, name))
            request["signatures"][kind] = [path.stat().st_size, path.stat().st_mtime_ns]
    return _queue_signal_task(job_id, request, "audio-review.json")


@app.post("/api/jobs/{job_id}/audio-review")
def standalone_audio_review(job_id: str, body: dict = Body(...)):
    return _queue_audio_review(job_id, body)


@app.post("/api/jobs/{job_id}/motion-review/{chart_id}/audio")
def start_audio_review(job_id: str, chart_id: str):
    job = get_job(job_id)
    parent = _chart_directory(job_id, chart_id)
    if chart_id not in job.get("charts", {}) or not (parent / "request.json").exists():
        raise HTTPException(404, "Signal comparison not found")
    return _queue_audio_review(job_id, json.loads((parent / "request.json").read_text()))


@lru_cache(maxsize=32)
def _audio_sections(directory_name, offset, fpv_modified, radio_modified):
    directory = Path(directory_name)
    with np.load(directory / "radio-audio.npz", allow_pickle=False) as values:
        radio = dict(values)
    with np.load(directory / "fpv-audio.npz", allow_pickle=False) as values:
        fpv = dict(values)
    return audio_sections(radio, fpv, offset)


@app.get("/api/jobs/{job_id}/motion-review/{chart_id}")
def motion_review(job_id: str, chart_id: str, start: float | None = None, end: float | None = None, offset: float = 0, mode: str = "body"):
    job = get_job(job_id)
    directory = _chart_directory(job_id, chart_id)
    status = job.get("charts", {}).get(chart_id)
    if not status:
        raise HTTPException(404, "Motion review not found")
    if status["status"] != "complete":
        progress_path = directory / "progress.json"
        if progress_path.exists():
            status = {**status, "progress": json.loads(progress_path.read_text())}
        return status
    request = json.loads((directory / "request.json").read_text())
    if request.get("kind") in {"fingerprint-study","motor-search","cross-modal-search"}:
        result=json.loads((directory/"fingerprints.json").read_text())
        result["rows"]=[row for row in result["rows"] if any(r["name"]==row["radio_video"] for r in job["inputs"]["radio"])
                        and any(r["name"]==row["fpv_video"] for r in job["inputs"]["fpv"])]
        return {**status,**result}
    if request.get("kind") == "radio-orientation":
        result = json.loads((directory / "orientation.json").read_text())
        for index, row in enumerate(result["clips"]):
            if row.get("image_file"):
                row["image_url"] = f"/api/jobs/{job_id}/radio-orientation/{chart_id}/frame/{index}"
        return {**status, **result}
    if request.get("kind") == "calibration-scan":
        result = json.loads((directory / "calibration-review.json").read_text())
        result["clips"]=[r for r in result["clips"] if any(c["name"]==r["name"] for c in job["inputs"]["radio"])]
        for row in result["clips"]:
            saved = job.get("calibration_reviews", {}).get(row["name"], {})
            row["confirmed"] = saved.get("proposal_id") == chart_id and saved.get("status") == "confirmed"
        return {**status, **result}
    if request.get("kind") == "fpv-segments":
        result = json.loads((directory / "segments.json").read_text())
        result["clips"]=[r for r in result["clips"] if any(c["name"]==r["fpv_video"] for c in job["inputs"]["fpv"])
                         and any(c["name"]==r["radio_video"] for c in job["inputs"]["radio"])]
        result["anchor"] = {key: result["anchor"][key] for key in ("radio_video", "fpv_video", "offset_seconds")}
        return {**status, **result}
    if request.get("kind") == "audio-validation":
        return {**status, **json.loads((directory / "validation.json").read_text())}
    if request.get("kind") == "fpv-preparation":
        result=json.loads((directory / "preparation.json").read_text())
        result["clips"]=[r for r in result["clips"] if any(c["name"]==r["name"] for c in job["inputs"]["fpv"])]
        return {**status,**result}
    if request.get("kind") == "stick-derivation":
        result = json.loads((directory / "derivation.json").read_text())
        result["clips"]=[r for r in result["clips"] if any(c["name"]==r["name"] for c in job["inputs"]["radio"])]
        for row in result["clips"]:
            record = _record(job, "radio", row["name"])
            row["current_cache"] = stick_cache_status(record["path"], jobs.DATA / "cache", job["calibrations"].get(row["name"]), row.get("sample_fps", request["sample_fps"]), _fpv_source_identity(record))
        progress_file = directory / "progress.json"
        return {**status, **result, "progress": json.loads(progress_file.read_text()) if progress_file.exists() else None}
    if request.get("kind") == "audio-review":
        if not math.isfinite(offset):
            raise HTTPException(400, "Choose a finite audio shift")
        traces, bounds = {}, []
        for kind in ("fpv", "radio"):
            name = request.get(kind + "_video")
            if not name:
                traces[kind] = {"points": [], "available": False}
                continue
            path = _input_video(job, kind, name)
            if [path.stat().st_size, path.stat().st_mtime_ns] != request["signatures"][kind]:
                raise HTTPException(409, "Audio source changed; reload the waveforms")
            shift = offset if kind == "fpv" else 0
            bounds += [shift, _metadata(job, kind, name)["duration"] + shift]
        start = min(bounds) if start is None else start
        end = max(bounds) if end is None else end
        if not all(math.isfinite(v) for v in (start, end)) or end <= start:
            raise HTTPException(400, "Choose a finite audio time window")
        for kind in ("fpv", "radio"):
            filename = directory / (kind + "-waveform.npz")
            if filename.exists():
                with np.load(filename, allow_pickle=False) as values:
                    traces[kind] = waveform_trace(dict(values), start, end, offset if kind == "fpv" else 0)
        sections = _audio_sections(str(directory), offset, (directory / "fpv-audio.npz").stat().st_mtime_ns,
                                   (directory / "radio-audio.npz").stat().st_mtime_ns) if request.get("radio_video") else None
        return {**status, **json.loads((directory / "audio-review.json").read_text()), "window": [start, end],
                "fpv": traces["fpv"], "radio": traces["radio"], "sections": sections}
    if request.get("kind") == "gyro-scan":
        radio_path = _input_video(job, "radio", request["radio_video"])
        if [radio_path.stat().st_size, radio_path.stat().st_mtime_ns] != request["radio_signature"]:
            raise HTTPException(409, "StickCam source changed; reload its signals")
        parent = json.loads((_chart_directory(job_id, request["parent"]) / "request.json").read_text())
        if job["calibrations"].get(request["radio_video"]) != parent["calibration"]:
            raise HTTPException(409, "StickCam calibration changed; reload its signals")
        if sorted(c["name"] for c in request["clips"]) != sorted(c["name"] for c in job["inputs"]["fpv"]):
            raise HTTPException(409, "Session FPV list changed; run the scan again")
        for clip in request["clips"]:
            path = _input_video(job, "fpv", clip["name"])
            if [path.stat().st_size, path.stat().st_mtime_ns] != clip["signature"]:
                raise HTTPException(409, "An FPV source changed; run the scan again")
        return {**status, **json.loads((directory / "scan.json").read_text())}
    for kind in ("fpv", "radio"):
        if request.get(kind + "_path"):
            p = _input_video(job, kind, request[kind + "_video"])
            if [p.stat().st_size, p.stat().st_mtime_ns] != request["signatures"][kind]:
                raise HTTPException(409, "Source changed; load the signals again")
    meta = json.loads((directory / "review.json").read_text())
    ranges = [[v + offset for v in meta["fpv_range"]], meta["radio_range"]]
    bounds = [v for r in ranges if r for v in r]
    start = min(bounds) if start is None else start
    end = max(bounds) if end is None else end
    if not all(math.isfinite(v) for v in (start, end, offset)) or end <= start:
        raise HTTPException(400, "Choose a finite time window with end after start")
    with np.load(directory / "fpv.npz", allow_pickle=False) as values:
        data = dict(values)
    if mode not in {"body", "sensor", "attitude"}:
        raise HTTPException(400, "Choose body rates, sensor rates or orientation")
    shift = offset
    if meta["fpv_kind"] == "visual":
        channels = [("rotation", "Image rotation", "°/s"), ("dx", "Horizontal flow", "frame widths/s"), ("dy", "Vertical flow", "frame heights/s")]
    elif meta["fpv_kind"] == "visual_rotation":
        if mode == "body":
            data = visual_body_rates(data, request.get("camera_uptilt_deg", 0))
            channels = [(key, key.title() + " · visual estimate", "°/s") for key in ("roll", "pitch", "yaw")]
        elif mode == "sensor":
            channels = [(key, "Camera " + key.upper() + " · visual estimate", "°/s") for key in ("x", "y", "z")]
        else:
            channels = []
            meta["warnings"].append("Visual relative rotation does not provide absolute attitude or gravity.")
    elif mode == "body":
        data = body_rates(data, request.get("camera_uptilt_deg", 0), request.get("imu_orientation", "zXy"), request.get("gyro_video_offset", 0))
        channels = [(k, k.title(), "°/s") for k in ("roll", "pitch", "yaw")]
        meta["fpv_note"] = f"Body rotation rates using sensor mapping {request.get('imu_orientation', 'zXy')} and camera uptilt {request.get('camera_uptilt_deg', 0):g}°. Confirm these settings for your mount. " + ("Derived from fused attitude rather than raw gyro. " if meta["fpv_kind"] == "quaternion_rates" else "Embedded gyro. ") + "Gyro-to-video timing has not been independently verified; this does not establish absolute gravity or throttle dynamics."
    elif mode == "attitude":
        channels = [(k, k.upper(), "quaternion") for k in ("qx", "qy", "qz", "qw") if k in data]
        shift += request.get("gyro_video_offset", 0)
        if not channels:
            meta["warnings"].append("This source provides raw gyro rates without embedded attitude; no absolute orientation is fabricated.")
    else:
        channels = [(k, "Sensor " + k.upper(), "°/s") for k in ("x", "y", "z")]
        shift += request.get("gyro_video_offset", 0)
    fpv = [chart_trace(data, key, label, unit, start, end, shift) for key, label, unit in channels]
    radio = []
    if meta["radio_available"]:
        with np.load(directory / "radio.npz", allow_pickle=False) as values:
            data = dict(values)
        radio = [chart_trace(data, k, k.title(), "°/s requested" if meta["rate_profile"] else "stick −1…1", start, end)
                 for k in ("roll", "pitch", "yaw")]
        radio.append(chart_trace(data, "throttle", "Throttle", "stick −1…1", start, end))
    sections = _review_sections(str(directory), offset, (directory / "fpv.npz").stat().st_mtime_ns,
                               (directory / "radio.npz").stat().st_mtime_ns) if meta["radio_available"] else None
    scene=[]
    if request.get("radio_video"):
        try:
            from fpv_matcher.radio.scene_activity import cache_path as scene_path,validate as validate_scene
            from fpv_matcher.matching.fingerprint_review import read_cache
            record=_record(job,"radio",request["radio_video"])
            saved=read_cache(scene_path(record["path"],jobs.DATA/"cache",_fpv_source_identity(record)));validate_scene(saved)
            scene=[chart_trace(saved,"activity","Residual scene movement","relative pixel change",start,end)]
        except (ValueError,OSError,KeyError,TypeError):
            pass
    return {**status, **meta, "bounds": [min(bounds), max(bounds)], "window": [start, end], "fpv": fpv, "radio": radio, "scene":scene,"sections": sections}


@app.get("/api/jobs/{job_id}/motion-review/{chart_id}/stabilization")
def motion_stabilization(job_id: str, chart_id: str):
    job = get_job(job_id)
    directory = _chart_directory(job_id, chart_id)
    if job.get("charts", {}).get(chart_id, {}).get("status") != "complete":
        raise HTTPException(409, "Load motion signals first")
    result = directory / "stabilization.json"
    if not result.exists():
        return {"poses": [], "note": "Save a StickCam calibration with the radio outline first."}
    request = json.loads((directory / "request.json").read_text())
    path = _input_video(job, "radio", request["radio_video"])
    stat = path.stat()
    if [stat.st_size, stat.st_mtime_ns] != request["signatures"]["radio"]:
        raise HTTPException(409, "Source changed; load motion signals again")
    return json.loads(result.read_text())


@app.post("/api/jobs/{job_id}/motion-review/{chart_id}/cancel")
def cancel_motion_review(job_id: str, chart_id: str):
    job = get_job(job_id)
    _chart_directory(job_id, chart_id)
    status = job.get("charts", {}).get(chart_id)
    if not status:
        raise HTTPException(404, "Motion review not found")
    if status["status"] not in {"queued", "running"}:
        return status
    _stop_process(f"chart:{job_id}:{chart_id}")
    jobs.update(job_id, lambda current: current["charts"][chart_id].update(status="cancelled", message="Signal extraction cancelled"))
    return {"status": "cancelled"}


@app.get("/api/jobs/{job_id}/plots")
def plots(job_id):
    _results(get_job(job_id))
    return {"files": sorted(p.name for p in (jobs.job_dir(job_id) / "results" / "plots").glob("*.png"))}


@app.get("/api/jobs/{job_id}/file/{filename}")
def result_file(job_id, filename):
    job = get_job(job_id)
    if filename == "analysis.log":
        path = jobs.job_dir(job_id) / "results" / filename
    elif filename == "matches.csv":
        rows = _effective_results(job)["rows"]
        keys = list(dict.fromkeys(k for row in rows for k in row if k not in {"alternatives", "axis_details"}))
        if any(row.get("reviewed") for row in rows) and "reviewed" not in keys:
            keys.append("reviewed")
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return StreamingResponse(
            iter([stream.getvalue().encode()]),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="matches.csv"'},
        )
    elif filename == "results.json":
        return _effective_results(job)
    elif re.fullmatch(r"pair_\d+\.png", filename):
        _results(job)
        path = jobs.job_dir(job_id) / "results" / "plots" / filename
    else:
        raise HTTPException(404, "File not found")
    if not path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(path)


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the FPV/StickCam matcher on your machine or LAN")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--check", action="store_true", help="Print a local installation report without starting the server")
    args = parser.parse_args()
    if args.check:
        from fpv_matcher.diagnostics.system import report
        print(json.dumps(report(), indent=2))
        return
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
