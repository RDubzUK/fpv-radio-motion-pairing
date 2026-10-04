"""Work-weighted StickCam batch progress and observed processing estimates."""
from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path


def timing_group(clip):
    calibration = clip["calibration"]
    return (calibration.get("stickcam_mode", "wonky"), bool(calibration.get("radio_outline")))


def saved_timings(directory, clips, decode_mode, algorithm, tracking_backend="cpu"):
    """Use recent successful extraction timings, never cache lookup timings."""
    current = {clip["name"]: clip for clip in clips}
    observations = {}
    try:
        files = sorted(Path(directory).parent.glob("*/derivation.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:20]
    except OSError:
        return observations
    for file in files:
        if file.parent == Path(directory):
            continue
        try:
            prior = json.loads((file.parent / "request.json").read_text())
            result = json.loads(file.read_text())
            if prior.get("decode_mode", "auto") != decode_mode or prior.get("algorithm") != algorithm:
                continue
            sources = {clip["name"]: clip for clip in prior["clips"]}
            for row in result["clips"]:
                if row.get("tracking_backend", "cpu") != tracking_backend:
                    continue
                clip = sources[row["name"]]
                duration = row.get("duration_seconds", clip.get("duration_seconds"))
                # Earlier logs did not store duration. Recover it only for the
                # same source signature, with metadata from this new request.
                if duration is None:
                    candidate = current.get(row["name"])
                    if candidate and candidate["signature"] == clip["signature"]:
                        duration = candidate.get("duration_seconds")
                seconds = row.get("extraction_seconds")
                fps = row.get("sample_fps", prior["sample_fps"])
                if row["status"] != "cached" or row.get("reused") or not duration or not seconds:
                    continue
                cost = seconds / (duration * fps)
                if math.isfinite(cost) and cost > 0:
                    observations.setdefault(timing_group(clip), []).append(cost)
        except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError):
            continue
    return {group: statistics.median(values[:20]) for group, values in observations.items()}


class StickBatchProgress:
    def __init__(self, clips, reused, emit, historical=None):
        self.clips = clips
        self.reused = reused
        self.emit = emit
        self.historical = historical or {}
        self.started = time.monotonic()
        self.observations = {}
        self.weights = [0 if saved else max(1, clip["duration_seconds"] * clip["sample_fps"])
                        for clip, saved in zip(clips, reused)]
        self.total_weight = sum(self.weights)
        self.index = 0
        self.clip_started = self.started
        self.event = {}

    def start(self, index, rows):
        self.index = index
        self.clip_started = time.monotonic()
        self.event = {}
        self.update({"percent": 0}, rows)

    def update(self, event, rows, finished=False):
        self.event.update(event)
        now = time.monotonic()
        clip = self.clips[self.index]
        elapsed = now - self.clip_started
        group = timing_group(clip)
        # Extraction emits 100% before validation/compression/cache saving.
        # Reserve the final percent until the complete clip is saved or failed.
        fraction = 1 if finished else min(.99, max(0, float(self.event.get("percent", 0)) / 100))
        observed = None
        processed = min(clip["duration_seconds"], max(0, float(self.event.get("processed_seconds", 0))))
        if not self.reused[self.index] and elapsed >= 3 and processed * clip["sample_fps"] >= 10:
            observed = elapsed / (processed * clip["sample_fps"])
        rates = {**self.historical, **self.observations}
        if observed is not None:
            rates[group] = observed
        if finished and rows[-1]["status"] == "cached" and not rows[-1].get("reused"):
            self.observations[group] = elapsed / self.weights[self.index]
            rates[group] = self.observations[group]
        settled = len(rows)
        done_weight = sum(self.weights[:settled])
        if not finished:
            done_weight += self.weights[self.index] * fraction
        percent = (done_weight / self.total_weight * 100 if self.total_weight
                   else settled / len(self.clips) * 100)
        if settled < len(self.clips):
            percent = min(99.9, percent)
        queue, remaining = [], []
        for index, item in enumerate(self.clips):
            row = rows[index] if index < settled else None
            current = index == self.index and not finished
            cost = rates.get(timing_group(item))
            if row:
                phase = "reused" if row.get("reused") else "failed" if row["status"] == "failed" else "saved"
                part, eta, spent = 100, 0, row["elapsed_seconds"]
            elif current:
                phase = "reusing" if self.reused[index] else "saving" if self.event.get("percent", 0) >= 100 else "extracting"
                part, spent = fraction * 100, elapsed
                eta = (max(0, cost * self.weights[index] - elapsed) if cost is not None else None)
                if self.reused[index]:
                    eta = None
            else:
                phase = "ready_to_reuse" if self.reused[index] else "queued"
                part, spent = 0, 0
                eta = cost * self.weights[index] if cost is not None else None
            # Cache lookup has no video tracking workload; it is shown explicitly
            # and contributes zero to the extraction estimate.
            if not row and not self.reused[index]:
                remaining.append(eta)
            queue.append({"name": item["name"], "status": phase, "percent": round(part, 1),
                          "elapsed_seconds": round(spent, 1),
                          "eta_seconds": round(eta, 1) if eta is not None else None,
                          "duration_seconds": item["duration_seconds"], "sample_fps": item["sample_fps"]})
        batch_eta = None if any(value is None for value in remaining) else sum(remaining)
        current = queue[self.index]
        self.emit({"stage": "stickcam", "name": clip["name"], "completed": settled,
                   "total": len(self.clips), "clips": list(rows), "queue": queue,
                   "percent": round(percent, 1), "clip_percent": current["percent"],
                   "elapsed_seconds": current["elapsed_seconds"], "sample_fps": clip["sample_fps"],
                   "eta_seconds": current["eta_seconds"], "clip_status": current["status"],
                   "processed_seconds": round(processed, 2), "duration_seconds": clip["duration_seconds"],
                   "batch_elapsed_seconds": round(now - self.started, 1),
                   "batch_eta_seconds": round(batch_eta, 1) if batch_eta is not None else None,
                   "estimate_basis": "observed" if observed is not None or self.observations else "history" if self.historical else "estimating",
                   **{key: value for key, value in self.event.items() if key.startswith("tracking_")},
                   "live_inputs": self.event.get("live_inputs"),
                   "updated_at": time.time()})
