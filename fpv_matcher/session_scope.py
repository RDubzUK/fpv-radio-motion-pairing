"""Reversible, session-local recording scope. Never remove saved media or evidence."""

from __future__ import annotations


def active_job(job):
    """Return a processing view; the durable job retains every recording."""
    result = dict(job)
    inputs = job["inputs"]
    result["inputs"] = {kind: [r for r in records if not r.get("ignored")]
                        for kind, records in inputs.items()}
    result["_ignored_inputs"] = {
        kind: [*job.get("_ignored_inputs", {}).get(kind, []),
               *(r for r in records if r.get("ignored"))]
        for kind, records in inputs.items()}
    names = {kind: {r["name"] for r in records} for kind, records in result["inputs"].items()}

    def active_pair(value):
        return value.get("radio_video") in names["radio"] and value.get("fpv_video") in names["fpv"]

    result["calibrations"] = {n: c for n, c in job.get("calibrations", {}).items() if n in names["radio"]}
    for key in ("pair_overrides", "rejected_pairs", "review_decisions", "sync_points", "clock_rejections"):
        result[key] = {n: d for n, d in job.get(key, {}).items() if active_pair(d)}
    result["overrides"] = {n: d for n, d in job.get("overrides", {}).items()
                           if n in names["radio"] and (not d.get("fpv_video") or d["fpv_video"] in names["fpv"])}
    result["segment_candidates"] = {
        key: {**group, "clips": [c for c in group.get("clips", []) if active_pair(c)]}
        for key, group in job.get("segment_candidates", {}).items() if active_pair(group.get("anchor", {}))}
    result["references"] = [r for r in job.get("references", []) if active_pair(r)]
    if job.get("radio_profile") and job["radio_profile"].get("name") not in names["radio"]:
        result["radio_profile"] = None
    return result


def scope_busy(job):
    active = {"queued", "running", "cancelling"}
    return job.get("status") in active or any(
        task.get("status") in active for collection in ("charts", "exports", "proxies")
        for task in job.get(collection, {}).values())
