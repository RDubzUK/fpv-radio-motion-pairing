from __future__ import annotations

import argparse
import asyncio
import csv
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
from fpv_matcher.portrait import DEFAULT as PORTRAIT_DEFAULT, validate_layout, export_portrait
from fpv_matcher.config import validate_calibration
from fpv_matcher.motion.review import chart_trace
from fpv_matcher.audio.review import VERSION as WAVEFORM_VERSION, waveform_trace, audio_sections
from fpv_matcher.radio.derivation import cache_status as stick_cache_status
from fpv_matcher.radio.derivation import FULL_SPAN_VERSION
from fpv_matcher.radio.sampling import tracking_fps, validate_ratio
from fpv_matcher.radio.stick_tracker import ALGORITHM_VERSION as STICK_VERSION
from fpv_matcher.radio.stabilization import VERSION as OUTLINE_VERSION
from fpv_matcher.matching.correlation import prepare, section_support
from fpv_matcher.matching.evidence import normalize_sources, motion_enabled, audio_enabled_pair
from fpv_matcher.motion.telemetry import VERSION as TELEMETRY_VERSION, validate_motion_settings, body_rates
import numpy as np
from fpv_matcher.exports import export_pair, stream_archive
from fpv_matcher.pipeline import atomic_json
from fpv_matcher.radio.rates import validate_rate_profile, apply_rate_profile
from fpv_matcher.radio.presets import load_presets, save_preset
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
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


@asynccontextmanager
async def lifespan(app):
    jobs.recover()
    yield
    for job in jobs.all_jobs():

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


app = FastAPI(title="FPV ↔ StickCam Matcher", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


def get_job(job_id):
    try:
        return jobs.get(job_id)
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
    signature = [path.stat().st_size, path.stat().st_mtime_ns]
    record = _record(job, kind, name)
    if (
        record.get("metadata_signature") == signature
        and record.get("metadata")
        and "color" in record["metadata"]
    ):
        return record["metadata"]
    try:
        info = probe_video(path)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(422, str(exc)) from exc

    def mutate(current):
        item = _record(current, kind, name)
        item.update(metadata=info, metadata_signature=signature)
        if current["source"] == "local":
            item.update(size=info["size"], uploaded=info["size"])

    jobs.update(job["id"], mutate)
    return info


def _public(job):
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
            "calibration_reviews",
            "radio_orientation_checks",
            "settings_defaults_version",
        )
    }
    result["inputs"] = {
        kind: [{key: item.get(key) for key in ("name", "size", "uploaded", "metadata")} for item in records]
        for kind, records in job["inputs"].items()
    }
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
    result["exports"] = [
        {"id": key, "status": value["status"], "message": value["message"]}
        for key, value in job.get("exports", {}).items()
    ]
    result["calibration_scans"] = [{"id": key, **value} for key, value in job.get("charts", {}).items()
                                   if value.get("kind") == "calibration-scan"]
    profile = job.get("radio_profile")
    result["radio_profile"] = {k: profile[k] for k in ("label", "revision", "name", "reference_time", "radio_rotation_deg")} if profile else None
    return result


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
                "radio_count": len(job["inputs"]["radio"]),
                "fpv_count": len(job["inputs"]["fpv"]),
            }
            for job in jobs.all_jobs()
        ]
    }


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
                    inputs[kind].append(
                        {"name": name, "path": str(base / kind / name), "size": size, "uploaded": 0}
                    )
            if not 1 <= len(inputs[kind]) <= 80:
                raise ValueError("Choose between 1 and 80 recordings of each type")
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
        "message": "Calibrate StickCam clips" if source == "local" else "Ready for resumable uploads",
        "label": str(body.get("label", ""))[:120] or time.strftime("Session %d %b %H:%M"),
        "inputs": inputs,
        "calibrations": {},
        "settings_defaults_version": 2,
        "settings": {
            "sample_fps": 30,
            "stick_sampling_ratio": None,
            "match_fps": 30,
            "min_overlap": 2,
            "workers": 2,
            "minimum_score": 0.55,
            "audio_enabled": True,
            "evidence_sources": normalize_sources(),
            "match_target_kind": "all",
            "match_target_name": "",
            "match_target_names": [],
            "stop_on_confident_match": True,
            "allow_unconfigured_radio": False,
            "rate_profile": default_rates,
            "motion_source": "auto",
            "camera_uptilt_deg": 0,
            "imu_orientation": "zXy",
            "gyro_video_offset": 0,
            "decode_mode": "auto",
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
    return _public(get_job(job_id))


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
                current.update(status="draft", message="All uploads complete. Calibrate StickCam clips.")

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
    if job["status"] in {"running", "queued"}:
        raise HTTPException(409, "Cancel analysis before changing its calibration")
    _record(job, "radio", filename)
    if proposal_id:
        directory = _chart_directory(job_id, proposal_id)
        proposals = directory / "calibration-review.json"
        if not proposals.exists():
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
        if proposal_id:
            current.setdefault("calibration_reviews", {})[filename] = {"proposal_id": proposal_id,
                "status": "confirmed", "at": config["reference_time"], "updated_at": time.time()}
    jobs.update(job_id, save)
    return {"saved": True, "config": config}


@app.post("/api/jobs/{job_id}/calibration/{filename}/suggest")
def suggest_calibration(job_id: str, filename: str, at: float = 0, request: dict = Body(default={})):
    import cv2
    import numpy as np

    from fpv_matcher.radio.orientation import rotation_from_regions, suggest_upright_regions
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
                source = _input_video(job, "radio", profile["name"])
                if [source.stat().st_size, source.stat().st_mtime_ns] != profile["signature"]:
                    raise ValueError("Session radio reference source changed; confirm a new reference")
                references = [(profile["name"], profile["calibration"])] + [r for r in references if r[0] != profile["name"] and r[1].get("radio_orientation_confirmed")]
                references = references[:3]
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
                except (HTTPException, OSError, ValueError, KeyError, subprocess.SubprocessError):
                    # An unreadable/deleted reference must not block this clip.
                    continue
        if proposal is None:
            proposal = suggest_upright_regions(image, outline, rotation)
            proposal["radio_rotation_deg"] = rotation
        for region in proposal["regions"].values():
            if "tip_x" not in region:
                tip = suggest_visible_tip(image, region)
                if tip is not None:
                    region.update(tip)
        proposal["missing_tips"] = [side for side, region in proposal["regions"].items() if "tip_x" not in region]
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
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


def _execute(key, command, log, timeout=24 * 3600):
    if key in CANCELLED:
        raise RuntimeError("Cancelled")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        with PROCESS_LOCK:
            PROCESSES[key] = process
        try:
            if key in CANCELLED:
                _stop_process(key)
            code = process.wait(timeout=timeout)
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
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise
        finally:
            with PROCESS_LOCK:
                PROCESSES.pop(key, None)


@app.post("/api/jobs/{job_id}/run")
def run_job(job_id: str, settings: dict = Body(default={})):
    job = get_job(job_id)
    if job["status"] in {"running", "queued", "cancelling"}:
        raise HTTPException(409, "This analysis is already active")
    if any(task.get("kind") == "stick-derivation" and task["status"] in {"queued", "running"} for task in job.get("charts", {}).values()):
        raise HTTPException(409, "StickCam preparation is active; wait for the saved movement data before matching")
    if any(item["uploaded"] != item["size"] for records in job["inputs"].values() for item in records):
        raise HTTPException(409, "Finish the uploads first")
    options = {
        **job["settings"],
        "rate_profile": None,
        "match_target_kind": "all",
        "match_target_name": "",
        "match_target_names": [],
        "stop_on_confident_match": True,
        "allow_unconfigured_radio": False,
        **{
            k: v
            for k, v in settings.items()
            if k in job["settings"]
            or k in {"rate_profile", "match_target_kind", "match_target_name", "match_target_names", "stop_on_confident_match", "allow_unconfigured_radio", "motion_source", "camera_uptilt_deg", "imu_orientation", "gyro_video_offset", "decode_mode", "evidence_sources", "stick_sampling_ratio"}
        },
    }
    try:
        options["stick_sampling_ratio"] = validate_ratio(options.get("stick_sampling_ratio"))
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
        use_motion = motion_enabled(options["evidence_sources"])
        options["audio_enabled"] = audio_enabled_pair(options["evidence_sources"])
        options["motion_source"] = ("auto" if options["evidence_sources"]["fpv_gyro"] and options["evidence_sources"]["fpv_visual"] else "gyro" if options["evidence_sources"]["fpv_gyro"] else "visual")
        if use_motion and options["allow_unconfigured_radio"] and not options["audio_enabled"]:
            raise ValueError("Audio-only StickCam matching requires boundary audio")
        options["rate_profile"] = validate_rate_profile(options["rate_profile"])
        options["motion_source"], options["camera_uptilt_deg"], options["imu_orientation"], options["gyro_video_offset"] = validate_motion_settings(options.get("motion_source", "auto"), options.get("camera_uptilt_deg", 0), options.get("imu_orientation", "zXy"), options.get("gyro_video_offset", 0))
        if options.get("decode_mode", "auto") not in {"auto", "cpu", "cuda"}:
            raise ValueError("Choose automatic, CPU or CUDA decoding")
        options.setdefault("decode_mode", "auto")
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
                if not options["audio_enabled"]:
                    raise ValueError("Audio-only clips require boundary audio to be enabled")
            else:
                info = _metadata(job, "radio", record["name"])
                tracking_fps(options["sample_fps"], options["stick_sampling_ratio"], info.get("fps"))
                validate_calibration(calibration, (info["width"], info["height"]))
                if any("tip_x" not in calibration[side] for side in ("left_gimbal", "right_gimbal")):
                    raise ValueError(f"Click both stick tips in {record['name']} before analysis")
        if all(job["calibrations"].get(record["name"], {}).get("exclude") for record in radio_records):
            raise ValueError("Keep at least one StickCam recording in this analysis")
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    identities = {kind: {} for kind in job["inputs"]}
    for kind, records in job["inputs"].items():
        for item in records:
            stat = Path(item["path"]).stat()
            if item.get("sha256") and item.get("upload_signature") == [stat.st_size, stat.st_mtime_ns]:
                identities[kind][item["name"]] = item["sha256"]
    config = {"clips": job["calibrations"], "identities": identities}
    base = jobs.job_dir(job_id)
    (base / "results" / "early.json").unlink(missing_ok=True)
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
        current.update(status="queued", message="Queued for analysis", settings=options, settings_defaults_version=2, run_id=run_id)

    jobs.update(job_id, queue)
    ANALYSIS_POOL.submit(_run_job, job_id, run_id)
    return {"status": "queued"}


def _run_job(job_id, run_id):
    with jobs.LOCK:
        job = get_job(job_id)
        if job["status"] != "queued" or job.get("run_id") != run_id:
            return
        jobs.update(
            job_id, lambda current: current.update(status="running", message="Extracting fingerprints")
        )
    base = jobs.job_dir(job_id)
    command = [sys.executable, "-u", "-m", "fpv_matcher.worker", str(base), str(jobs.DATA / "cache")]
    try:
        _execute("analysis:" + job_id + ":" + run_id, command, base / "results" / "analysis.log")
        jobs.update(
            job_id,
            lambda current: (
                current.update(status="complete", message="Analysis complete")
                if current["status"] == "running" and current.get("run_id") == run_id
                else None
            ),
        )
    except Exception as exc:
        status = "cancelled" if "analysis:" + job_id + ":" + run_id in CANCELLED else "failed"
        detail = (
            "Analysis cancelled; cached fingerprints can be reused."
            if status == "cancelled"
            else f"Analysis failed: {exc}"
        )
        jobs.update(
            job_id,
            lambda current: (
                current.update(status=status, message=detail)
                if current["status"] != "interrupted" and current.get("run_id") == run_id
                else None
            ),
        )


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
            def confirm(current):
                overrides = current.setdefault("overrides", {})
                for radio_name in list(overrides):
                    if overrides[radio_name].get("fpv_video") == row["fpv_video"]:
                        del overrides[radio_name]
                overrides[row["radio_video"]] = {"fpv_video": row["fpv_video"], "offset_seconds": row["offset_seconds"], "updated_at": time.time()}
            jobs.update(job_id, confirm)
        atomic_json(decision, {"action": body["action"]})
    return {"status": "accepted"}


def _results(job):
    if job["status"] != "complete":
        raise HTTPException(409, "Analysis is not complete")
    return json.loads((jobs.job_dir(job["id"]) / "results" / "results.json").read_text(encoding="utf-8"))


def _effective_results(job):
    results = _results(job)
    for radio_name, override in sorted(
        job.get("overrides", {}).items(), key=lambda item: item[1].get("updated_at", 0)
    ):
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
    return results


@app.get("/api/jobs/{job_id}/matches")
def matches(job_id):
    return _effective_results(get_job(job_id))


def _pair_info(job, radio_name, fpv_name, offset=None):
    results = _effective_results(job)
    row = next(
        (r for r in results["rows"] if r["radio_video"] == radio_name and r["fpv_video"] == fpv_name), None
    )
    if row is None:
        raise HTTPException(404, "That pair has no matching analysis")
    rinfo, finfo = _metadata(job, "radio", radio_name), _metadata(job, "fpv", fpv_name)
    for kind, name in (("radio", radio_name), ("fpv", fpv_name)):
        original = next(c for c in results["clips"][kind] if c["name"] == name)
        stat = _input_video(job, kind, name).stat()
        if stat.st_size != original["size"] or (
            original.get("mtime_ns") and stat.st_mtime_ns != original["mtime_ns"]
        ):
            raise HTTPException(
                409, f"{name} changed since analysis. Run analysis again before previewing or exporting it."
            )
    try:
        offset = float(row["offset_seconds"] if offset is None else offset)
        if not math.isfinite(offset):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(400, "Offset must be a finite number") from None
    rstart, fstart = max(0.0, offset), max(0.0, -offset)
    duration = min(rinfo["duration"] - rstart, finfo["duration"] - fstart)
    if duration <= 0:
        raise HTTPException(422, "This offset has no overlapping interval")
    return {
        "radio_video": radio_name,
        "fpv_video": fpv_name,
        "radio_duration": rinfo["duration"],
        "fpv_duration": finfo["duration"],
        "radio_start": rstart,
        "fpv_start": fstart,
        "overlap_duration": duration,
        "offset_seconds": offset,
        "score": row["combined_score"],
        "confidence": row["confidence"],
        "evidence": row["evidence"],
        "reviewed": row.get("reviewed", False) and abs(offset - row["offset_seconds"]) < 1e-7,
        "source_signatures": {
            kind: [p.stat().st_size, p.stat().st_mtime_ns]
            for kind, p in (
                ("radio", _input_video(job, "radio", radio_name)),
                ("fpv", _input_video(job, "fpv", fpv_name)),
            )
        },
        "source_metadata": {"radio": rinfo, "fpv": finfo},
        "radio_url": f"/api/jobs/{job['id']}/video/radio/{quote(radio_name, safe='')}",
        "fpv_url": f"/api/jobs/{job['id']}/video/fpv/{quote(fpv_name, safe='')}",
    }


@app.post("/api/jobs/{job_id}/pair-info")
def pair_info(job_id: str, pair: dict = Body(...)):
    return _pair_info(
        get_job(job_id), pair.get("radio_video"), pair.get("fpv_video"), pair.get("offset_seconds")
    )


@app.put("/api/jobs/{job_id}/pairs/{radio_name}")
def review_pair(job_id: str, radio_name: str, body: dict = Body(...)):
    job = get_job(job_id)
    _record(job, "radio", radio_name)
    fpv_name = body.get("fpv_video")
    if fpv_name:
        info = _pair_info(job, radio_name, fpv_name, body.get("offset_seconds"))
        offset = info["offset_seconds"]
    else:
        _results(job)
        offset = 0.0

    def mutate(current):
        for other_name, override in current["overrides"].items():
            if fpv_name and other_name != radio_name and override.get("fpv_video") == fpv_name:
                override.update(fpv_video=None, updated_at=time.time())
        current["overrides"][radio_name] = {
            "fpv_video": fpv_name,
            "offset_seconds": offset,
            "updated_at": time.time(),
        }

    jobs.update(job_id, mutate)
    return {"saved": True}


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
def browser_preview(job_id: str, kind: str, filename: str):
    job = get_job(job_id)
    source = _input_video(job, kind, filename)
    return preview_manifest(source, _metadata(job, kind, filename))


@app.get("/api/jobs/{job_id}/preview/{kind}/{filename}/{chunk}")
async def browser_preview_chunk(job_id: str, kind: str, filename: str, chunk: int, request: Request):
    job = get_job(job_id)
    source = _input_video(job, kind, filename)
    info = await anyio.to_thread.run_sync(_metadata, job, kind, filename)
    descriptor = preview_manifest(source, info)
    if request.query_params.get("source") != descriptor["source_key"]:
        raise HTTPException(409, "Source recording changed; reload this preview")
    if chunk < 0 or chunk >= descriptor["chunks"]:
        raise HTTPException(404, "Preview chunk is outside this recording")
    cancelled = threading.Event()
    key = "preview:" + uuid.uuid4().hex
    future = asyncio.get_running_loop().run_in_executor(
        PREVIEW_POOL, preview_fragment, source, info, chunk, jobs.DATA / "preview-cache", _execute, key, cancelled)
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
        jobs.update(
            job_id,
            lambda current: (
                current["proxies"][key].update(status="failed", message=detail)
                if current["proxies"][key]["status"] != "interrupted"
                else None
            ),
        )


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


@app.post("/api/jobs/{job_id}/exports")
def create_export(job_id: str, body: dict = Body(...)):
    job = get_job(job_id)
    requested = body.get("pairs", [body])
    if not isinstance(requested, list) or not 1 <= len(requested) <= 80:
        raise HTTPException(400, "Export between 1 and 80 pairs")
    fps = body.get("fps", 30)
    profile = body.get("profile", "h264")
    if fps not in {24, 25, 30, 50, 60}:
        raise HTTPException(400, "Choose an export frame rate of 24, 25, 30, 50 or 60")
    if profile not in {"h264", "dnxhr"}:
        raise HTTPException(400, "Export profile must be h264 or dnxhr")
    portrait = body.get("portrait")
    infos = [
        _pair_info(job, pair.get("radio_video"), pair.get("fpv_video"), pair.get("offset_seconds"))
        for pair in requested
    ]
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
            metadata["width"] * metadata["height"] * info["overlap_duration"] * fps * 0.75
            for info in infos
            for metadata in info["source_metadata"].values()
        )
        if estimate > shutil.disk_usage(jobs.DATA).free - 1024**3:
            raise HTTPException(
                507,
                f"This DNxHR batch needs approximately {estimate / 1024**3:.1f} GiB of working space. Export fewer pairs or choose compact MP4.",
            )
    if len({i["radio_video"] for i in infos}) != len(infos) or len({i["fpv_video"] for i in infos}) != len(
        infos
    ):
        raise HTTPException(400, "Batch exports must contain one-to-one pairs")
    if shutil.disk_usage(jobs.DATA).free < 1024**3:
        raise HTTPException(507, "Export needs at least 1 GiB of working disk space")
    signature = hashlib.sha256(
        json.dumps(
            {
                "fps": fps,
                "profile": profile,
                "portrait": portrait,
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
        export_id = uuid.uuid4().hex[:10]
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
                    }
                }
            ),
        )
    EXPORT_POOL.submit(_make_export, job_id, export_id, infos, fps, profile, portrait)
    return {"id": export_id}


def _make_export(job_id, export_id, infos, fps, profile, portrait=None):
    key = "export:" + job_id + ":" + export_id
    directory = jobs.job_dir(job_id) / "results" / "exports" / export_id
    try:
        manifests = []
        for index, info in enumerate(infos):
            if key in CANCELLED:
                raise RuntimeError("Cancelled")
            job = get_job(job_id)
            sources = {kind: _input_video(job, kind, info[kind + "_video"]) for kind in ("radio", "fpv")}
            for kind, source in sources.items():
                if [source.stat().st_size, source.stat().st_mtime_ns] != info["source_signatures"][kind]:
                    raise ValueError("Source changed while export was queued; analyze it again")
            pair_dir = directory / f"pair_{index + 1:02d}"

            def progress(message, pair_number=index + 1):
                jobs.update(
                    job_id,
                    lambda current: current["exports"][export_id].update(
                        status="running", message=f"Pair {pair_number}/{len(infos)} · {message}"
                    ),
                )

            if portrait:
                manifest = export_portrait(info, sources["radio"], sources["fpv"], pair_dir, fps, portrait,
                    run=lambda command, log: _execute(key, command, log, timeout=8 * 3600), progress=progress)
            else:
                manifest = export_pair(info, sources["radio"], sources["fpv"], pair_dir, fps, profile,
                    run=lambda command, log: _execute(key, command, log, timeout=8 * 3600), progress=progress)
            manifests.append((pair_dir, manifest))
            for kind, source in sources.items():
                if [source.stat().st_size, source.stat().st_mtime_ns] != info["source_signatures"][kind]:
                    raise ValueError("Source changed during export; analyze it again")
        if key in CANCELLED:
            raise RuntimeError("Cancelled")
        jobs.update(
            job_id,
            lambda current: (
                current["exports"][export_id].update(
                    status="complete", message="Portrait composite ready" if portrait else "Aligned clips ready", directory=str(directory)
                )
                if current["exports"][export_id]["status"] not in {"cancelled", "interrupted"}
                else None
            ),
        )
    except Exception as exc:
        status = "cancelled" if key in CANCELLED else "failed"
        detail = str(exc)
        jobs.update(
            job_id,
            lambda current: (
                current["exports"][export_id].update(status=status, message=detail)
                if current["exports"][export_id]["status"] != "interrupted"
                else None
            ),
        )
        shutil.rmtree(directory, ignore_errors=True)


@app.get("/api/jobs/{job_id}/exports/{export_id}")
def export_status(job_id, export_id):
    item = get_job(job_id)["exports"].get(export_id)
    if not item:
        raise HTTPException(404, "Export not found")
    directory = jobs.job_dir(job_id) / "results" / "exports" / export_id
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
    directory = jobs.job_dir(job_id) / "results" / "exports" / export_id
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
    root = jobs.job_dir(job_id) / "results" / "exports" / export_id
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
            kind = request.get("kind", "motion-review")
            scanning = kind == "gyro-scan"
            messages = {"gyro-scan": "Comparing cached StickCam inputs with session FPV telemetry.",
                        "radio-orientation": "Checking the session radio around each clip's midpoint.",
                        "audio-review": "Reading cached audio or decoding waveform envelopes.",
                        "calibration-scan": "Finding visible radio outlines and gimbals at five-second intervals.",
                        "stick-derivation": "Deriving calibrated StickCam recordings; cached signals are reused."}
            status("running", messages.get(kind, "Reading FPV motion and tracking StickCam; cached signals are reused."))
            log = (directory / "worker.log").open("w")
            module = {"gyro-scan": "fpv_matcher.motion.gyro_scan", "audio-review": "fpv_matcher.audio.review",
                      "radio-orientation": "fpv_matcher.radio.orientation",
                      "calibration-scan": "fpv_matcher.radio.calibration_scan",
                      "stick-derivation": "fpv_matcher.radio.derivation"}.get(kind, "fpv_matcher.motion.review")
            process = subprocess.Popen([sys.executable, "-u", "-m", module, str(directory / "request.json")],
                stdout=log, stderr=log, start_new_session=True)
            PROCESSES[key] = process
        try:
            code = process.wait(timeout=2 * 3600)
        except subprocess.TimeoutExpired:
            _stop_process(key)
            process.wait(timeout=10)
            raise ValueError("Signal extraction exceeded two hours; inspect a shorter recording.")
        finally:
            log.close()
        with PROCESS_LOCK:
            cancelled = key in CANCELLED
        if cancelled:
            status("cancelled", "Signal extraction cancelled")
        elif code:
            detail = (directory / "worker.log").read_text(errors="replace")[-1200:]
            status("failed", detail.splitlines()[-1] if detail else "Signal extraction failed")
        else:
            if kind == "radio-orientation":
                result = json.loads((directory / "orientation.json").read_text())
                def save_orientation(current):
                    if (current.get("radio_profile") or {}).get("revision") == result["profile_revision"]:
                        current.setdefault("radio_orientation_checks", {}).update({row["name"]: {**row, "task_id": chart_id}
                                                                                  for row in result["clips"]})
                jobs.update(job_id, save_orientation)
            status("complete", {"gyro-scan": "Gyro correlation scan ready", "audio-review": "Audio waveforms ready",
                                "radio-orientation": "Radio orientation suggestions ready for review",
                                "calibration-scan": "Calibration proposals ready for review",
                                "stick-derivation": "StickCam preparation finished; inspect per-video results"}.get(kind, "Motion signals ready"))
    except Exception as exc:
        status("failed", str(exc))
    finally:
        with PROCESS_LOCK:
            PROCESSES.pop(key, None)
        with CHART_LOCK:
            CHART_TASKS.pop((job_id, chart_id), None)


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
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    request = {"fpv_video": fpv_name, "radio_video": radio_name, "fpv_path": str(fpv_path),
        "radio_path": str(radio_path) if radio_path else None, "source": source, "sample_fps": fps,
        "stick_sample_fps": radio_fps, "stick_sampling_ratio": ratio,
        "match_fps": compare_fps, "min_overlap": minimum,
        "calibration": job["calibrations"].get(radio_name), "rate_profile": profile,
        "camera_uptilt_deg": uptilt, "imu_orientation": orientation, "gyro_video_offset": gyro_shift, "decode_mode": decode_mode,
        "fpv_identity": _record(job, "fpv", fpv_name).get("sha256"),
        "radio_identity": _record(job, "radio", radio_name).get("sha256") if radio_name else None,
        "signatures": {kind: [p.stat().st_size, p.stat().st_mtime_ns]
                       for kind, p in (("fpv", fpv_path), ("radio", radio_path)) if p}, "version": 5, "telemetry_version": TELEMETRY_VERSION}
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
        clips.append({"name": record["name"], "path": str(path), "identity": record.get("sha256"),
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
    if str(fpv["kind"].item()) != "visual":
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
    profile = {"label": label.strip(), "name": name, "path": str(path), "revision": uuid.uuid4().hex,
               "signature": [path.stat().st_size, path.stat().st_mtime_ns], "calibration": config,
               "width": info["width"], "height": info["height"], "reference_time": config["reference_time"],
               "radio_rotation_deg": config.get("radio_rotation_deg", 0)}
    def save(current):
        current.update(radio_profile=profile, radio_orientation_checks={})
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
               "references": references, "search_seconds": seconds}
    return _queue_signal_task(job_id, request, "calibration-review.json")


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
        source_fps = (clip.get("metadata") or {}).get("fps")
        try:
            if ratio:
                source_fps = _metadata(job, "radio", clip["name"]).get("fps")
            requested = tracking_fps(fps, ratio, source_fps)
        except (ValueError, HTTPException) as exc:
            clips.append({"name": clip["name"], "status": "unavailable", "source_fps": source_fps,
                          "sampling_error": str(exc.detail if isinstance(exc, HTTPException) else exc)})
            continue
        calibration = job["calibrations"].get(clip["name"])
        status = stick_cache_status(clip["path"], jobs.DATA / "cache", calibration, requested, clip.get("sha256"))
        # Other rates remain saved: switching back can reuse them immediately.
        if status["status"] == "not_derived":
            rates = {fps, previous_fps}
            if source_fps and math.isfinite(source_fps) and source_fps > 0:
                rates.update(round(source_fps * r, 8) for r in (0.5, 0.25) if source_fps * r <= 60)
            for saved_fps in sorted(rates - {requested}, reverse=True):
                previous = stick_cache_status(clip["path"], jobs.DATA / "cache", calibration, saved_fps, clip.get("sha256"))
                if previous["status"] == "cached":
                    status = {**previous, "status": "other_rate"}
                    break
        clips.append({"name": clip["name"], "source_fps": source_fps, "requested_fps": requested, **status})
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


@app.post("/api/jobs/{job_id}/derive-sticks")
def derive_session_sticks(job_id: str, body: dict = Body(...)):
    job = get_job(job_id)
    if job["status"] in {"queued", "running", "cancelling"}:
        raise HTTPException(409, "Wait for matching to finish; it already derives and caches the required StickCam inputs")
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
    clips = []
    for record in job["inputs"]["radio"]:
        calibration = job["calibrations"].get(record["name"])
        if not calibration or calibration.get("skip_motion") or calibration.get("exclude"):
            continue
        path = _input_video(job, "radio", record["name"])
        try:
            calibration = validate_calibration(calibration)
        except ValueError:
            continue
        try:
            radio_fps = tracking_fps(fps, ratio, _metadata(job, "radio", record["name"]).get("fps")) if ratio else fps
        except ValueError as exc:
            raise HTTPException(400, f"{record['name']}: {exc}") from exc
        clips.append({"name": record["name"], "path": str(path), "identity": record.get("sha256"),
                      "signature": [path.stat().st_size, path.stat().st_mtime_ns], "calibration": calibration,
                      "sample_fps": radio_fps})
    if not clips:
        raise HTTPException(400, "Calibrate at least one StickCam video before deriving inputs")
    # A previous completed run can contain failed clips or a deleted cache: queue again if any current cache is missing.
    request = {"kind": "stick-derivation", "clips": clips, "sample_fps": fps, "decode_mode": decode,
               "stick_sampling_ratio": ratio,
               "algorithm": STICK_VERSION + OUTLINE_VERSION + FULL_SPAN_VERSION}
    if any(stick_cache_status(c["path"], jobs.DATA / "cache", c["calibration"], c["sample_fps"], c["identity"])["status"] != "cached" for c in clips):
        request["attempt"] = uuid.uuid4().hex
    jobs.update(job_id, lambda current: current["settings"].update(sample_fps=fps, stick_sampling_ratio=ratio))
    return _queue_signal_task(job_id, request, "derivation.json")


@app.post("/api/jobs/{job_id}/motion-review/{chart_id}/audio")
def start_audio_review(job_id: str, chart_id: str):
    job = get_job(job_id)
    parent = _chart_directory(job_id, chart_id)
    if job.get("charts", {}).get(chart_id, {}).get("status") != "complete" or not (parent / "review.json").exists():
        raise HTTPException(409, "Load the motion comparison first")
    original = json.loads((parent / "request.json").read_text())
    from fpv_matcher.audio.fingerprint import ALGORITHM_VERSION as AUDIO_VERSION
    request = {"kind": "audio-review", "version": WAVEFORM_VERSION, "fingerprint_version": AUDIO_VERSION}
    request["signatures"] = {}
    for kind in ("fpv", "radio"):
        name = original.get(kind + "_video")
        request[kind + "_video"] = name
        if name:
            path = _input_video(job, kind, name)
            request[kind + "_path"] = str(path)
            request[kind + "_identity"] = _record(job, kind, name).get("sha256")
            request["signatures"][kind] = [path.stat().st_size, path.stat().st_mtime_ns]
    return _queue_signal_task(job_id, request, "audio-review.json")


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
    if request.get("kind") == "radio-orientation":
        result = json.loads((directory / "orientation.json").read_text())
        for index, row in enumerate(result["clips"]):
            if row.get("image_file"):
                row["image_url"] = f"/api/jobs/{job_id}/radio-orientation/{chart_id}/frame/{index}"
        return {**status, **result}
    if request.get("kind") == "calibration-scan":
        result = json.loads((directory / "calibration-review.json").read_text())
        for row in result["clips"]:
            saved = job.get("calibration_reviews", {}).get(row["name"], {})
            row["confirmed"] = saved.get("proposal_id") == chart_id and saved.get("status") == "confirmed"
        return {**status, **result}
    if request.get("kind") == "stick-derivation":
        result = json.loads((directory / "derivation.json").read_text())
        for row in result["clips"]:
            record = _record(job, "radio", row["name"])
            row["current_cache"] = stick_cache_status(record["path"], jobs.DATA / "cache", job["calibrations"].get(row["name"]), row.get("sample_fps", request["sample_fps"]), record.get("sha256"))
        return {**status, **result}
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
    return {**status, **meta, "bounds": [min(bounds), max(bounds)], "window": [start, end], "fpv": fpv, "radio": radio, "sections": sections}


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
        keys = [k for k in rows[0] if k not in {"alternatives", "axis_details"}]
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
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
