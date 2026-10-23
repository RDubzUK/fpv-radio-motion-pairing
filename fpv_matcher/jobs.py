"""Small durable job store. Media and results remain available across restarts."""

from __future__ import annotations

import copy
import json
import os
import threading
import time
import shutil
from pathlib import Path

from fpv_matcher.pipeline import atomic_json
from fpv_matcher.storage import SaveError

DATA = (
    Path(os.environ.get("FPV_MATCHER_DATA", str(Path.home() / ".fpv-radio-motion-pairing")))
    .expanduser()
    .resolve()
)
LOCK = threading.RLock()
_INSTANCE = None
SAVE_FAILURES = {}


def job_dir(job_id: str) -> Path:
    if len(job_id) != 12 or any(character not in "0123456789abcdef" for character in job_id):
        raise KeyError("Job not found")
    return DATA / "jobs" / job_id


def get(job_id: str) -> dict:
    with LOCK:
        try:
            result = json.loads((job_dir(job_id) / "job.json").read_text(encoding="utf-8"))
            for collection, task_id, patch in SAVE_FAILURES.get(job_id, []):
                target = result if collection is None else result.setdefault(collection, {}).setdefault(task_id, {})
                target.update(patch)
            return result
        except (OSError, ValueError):
            for backup in backups(job_id):
                try:
                    result = json.loads((job_dir(job_id) / "backups" / backup["name"]).read_text())
                    if result.get("id") == job_id:
                        result["recovery_required"] = backup["name"]
                        return result
                except (OSError, ValueError, AttributeError):
                    continue
            raise KeyError("Job not found") from None


def save(job: dict):
    with LOCK:
        if job.get("recovery_required"):
            raise SaveError("Restore the offered session metadata backup before editing or processing this session")
        for target in [job, *(task for collection in ("charts", "exports", "proxies") for task in job.get(collection, {}).values())]:
            if target.pop("persistence_failed", False):
                target["message"] = target.get("message", "").replace("Task stopped; this status is unsaved. ", "Task stopped. ", 1)
        source = job_dir(job["id"]) / "job.json"
        previous = backups(job["id"])
        if source.is_file() and (not previous or time.time() - previous[0]["created"] >= 60):
            json.loads(source.read_text())  # Never back up an unreadable document.
            target = source.parent / "backups" / f"{time.time_ns()}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            for backup in backups(job["id"])[10:]:
                (target.parent / backup["name"]).unlink(missing_ok=True)
        atomic_json(job_dir(job["id"]) / "job.json", job)
        SAVE_FAILURES.pop(job["id"], None)


def failed_task(job_id, message, collection=None, task_id=None, status="failed"):
    """Publish a stopped task even when its failure status cannot be saved."""
    patch = {"status": status, "message": message}
    def change(job):
        target = job if collection is None else job.setdefault(collection, {}).setdefault(task_id, {})
        target.update(patch)
        target.pop("persistence_failed", None)
    try:
        return update(job_id, change)
    except SaveError as exc:
        with LOCK:
            patch.update(persistence_failed=True, message=f"Task stopped; this status is unsaved. {message} {exc}")
            saved = [item for item in SAVE_FAILURES.get(job_id, []) if item[:2] != (collection, task_id)]
            SAVE_FAILURES[job_id] = saved + [(collection, task_id, patch)]
    except KeyError:
        pass


def backups(job_id):
    with LOCK:
        return [{"name": p.name, "created": p.stat().st_mtime} for p in
                sorted((job_dir(job_id) / "backups").glob("*.json"), reverse=True)]


def restore(job_id, name):
    with LOCK:
        if name not in {item["name"] for item in backups(job_id)}:
            raise ValueError("Choose an available metadata backup")
        doc = json.loads((job_dir(job_id) / "backups" / name).read_text())
        if doc.get("id") != job_id:
            raise ValueError("Backup belongs to another session")
        current = job_dir(job_id) / "job.json"
        # Keep export history from the current document when readable. Recovery
        # never changes completed clips or their locations.
        try:
            latest = json.loads(current.read_text())
            doc["exports"] = {**doc.get("exports", {}), **latest.get("exports", {})}
        except (OSError, ValueError):
            pass
        if current.exists():
            shutil.copyfile(current, current.with_name(f"before-restore-{time.time_ns()}.json"))
        doc.pop("recovery_required", None)
        doc.update(status="interrupted", message="Session metadata restored. Review sources before resuming.")
        for collection in ("charts", "exports", "proxies"):
            for task in doc.get(collection, {}).values():
                if task.get("status") in {"queued", "running", "cancelling"}:
                    task.update(status="interrupted", message="Metadata restored; resume explicitly")
        atomic_json(current, doc)
        return doc


def acquire_instance():
    global _INSTANCE
    DATA.mkdir(parents=True, exist_ok=True)
    handle = (DATA / ".app.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0"); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError(f"Another Flight Matcher instance is using {DATA}. Close it or use a different FPV_MATCHER_DATA folder.") from None
    _INSTANCE = handle


def release_instance():
    global _INSTANCE
    if _INSTANCE:
        _INSTANCE.close()
        _INSTANCE = None


def update(job_id: str, mutate) -> dict:
    with LOCK:
        job = get(job_id)
        mutate(job)
        job["updated_at"] = time.time()
        save(job)
        return copy.deepcopy(job)


def all_jobs(include_deleted=False) -> list[dict]:
    root = DATA / "jobs"
    if not root.exists():
        return []
    jobs = []
    for path in root.glob("*/job.json"):
        try:
            job = get(path.parent.name)
            if include_deleted or not job.get("deleted_at"):
                jobs.append(job)
        except KeyError:
            continue
    return sorted(jobs, key=lambda job: job.get("updated_at", 0), reverse=True)


def recover():
    DATA.mkdir(parents=True, exist_ok=True)
    for job in all_jobs():
        if job.get("recovery_required"):
            continue
        changed = False
        if job["status"] in {"running", "queued", "cancelling"}:
            job.update(
                status="interrupted",
                message="Analysis stopped when the server restarted. Run again to reuse cached fingerprints.",
            )
            changed = True
        for collection in ("exports", "proxies", "charts"):
            for item in job.get(collection, {}).values():
                if item["status"] in {"running", "queued", "cancelling"}:
                    item.update(
                        status="interrupted",
                        message="Stopped when the server restarted; request again to resume.",
                    )
                    changed = True
        if changed:
            save(job)
