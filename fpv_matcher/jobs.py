"""Small durable job store. Media and results remain available across restarts."""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path

from fpv_matcher.pipeline import atomic_json

DATA = (
    Path(os.environ.get("FPV_MATCHER_DATA", str(Path.home() / ".fpv-radio-motion-pairing")))
    .expanduser()
    .resolve()
)
LOCK = threading.RLock()


def job_dir(job_id: str) -> Path:
    if len(job_id) != 12 or any(character not in "0123456789abcdef" for character in job_id):
        raise KeyError("Job not found")
    return DATA / "jobs" / job_id


def get(job_id: str) -> dict:
    with LOCK:
        try:
            return json.loads((job_dir(job_id) / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise KeyError("Job not found") from None


def save(job: dict):
    with LOCK:
        atomic_json(job_dir(job["id"]) / "job.json", job)


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
