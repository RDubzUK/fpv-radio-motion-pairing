"""Review decisions, independent timing checkpoints and flight coverage."""
from __future__ import annotations

import time


def state(pair):
    return "confirmed" if pair.get("confirmed") else pair.get("review_state", "candidate")


def independently_verified(pair):
    if not pair.get("confirmed") or pair.get("stale"):
        return False
    # Older audio/manual confirmations keep their established meaning. Date
    # confirmations need an explicit content check before teaching the clock.
    return pair.get("verification", "independent" if pair.get("method") != "timestamp" else "date_only") == "independent"


def event(doc, action, **details):
    doc.setdefault("history", []).append({"time": time.time(), "action": action, **details})
    doc["history"] = doc["history"][-500:]


def sync_check(pair):
    points = sorted(pair.get("sync_points", []), key=lambda p: p["fpv_time"])
    if not points:
        return {"status": "not_checked", "points": 0, "message": "Add independently identified events near the start and end to check sync."}
    offsets = [p["stick_time"] - p["fpv_time"] for p in points]
    errors = [value - pair["offset"] for value in offsets]
    result = {"status": "one_point", "points": len(points), "max_error": max(map(abs, errors)), "offsets": offsets,
              "message": "One event checks the offset, but cannot establish drift."}
    if result["max_error"] > .15:
        result.update(status="offset_mismatch", message="This event disagrees with the saved offset. Adjust and review the alignment.")
    if len(points) > 1 and points[-1]["fpv_time"] - points[0]["fpv_time"] >= 2:
        x = [p["fpv_time"] for p in points]
        mx, my = sum(x) / len(x), sum(offsets) / len(offsets)
        slope = sum((a - mx) * (b - my) for a, b in zip(x, offsets)) / sum((a - mx) ** 2 for a in x)
        drift = slope * pair["overlap_duration"]
        coverage = (x[-1] - x[0]) / pair["overlap_duration"] if pair["overlap_duration"] > 0 else 0
        status = "drift" if abs(drift) > .1 else "offset_mismatch" if result["max_error"] > .15 else "checked" if coverage >= .6 else "limited"
        result.update(status=status, drift_seconds=drift, drift_ppm=slope * 1e6, coverage=coverage,
                      message={"drift": "Checkpoints indicate changing sync. Review shorter ranges before export; no automatic stretching is applied.",
                               "offset_mismatch": "Checkpoints disagree with the saved offset. Adjust and review it before export.",
                               "checked": "The sampled events support a constant offset over most of the shared footage.",
                               "limited": "These events agree locally; add an event farther along the recording."}[status])
    return result


def summary(doc):
    counts = {k: 0 for k in ("confirmed", "awaiting", "later", "unmatched", "unsearched", "missing")}
    for r in (v for v in doc["videos"] if v["kind"] == "fpv"):
        pairs = [p for p in doc["pairs"] if p["fpv"] == r["id"] and not p.get("stale") and state(p) != "rejected"]
        if r.get("missing") or r.get("error"):
            key = "missing"
        elif any(p.get("confirmed") for p in pairs):
            key = "confirmed"
        elif any(state(p) == "candidate" for p in pairs):
            key = "awaiting"
        elif pairs:
            key = "later"
        elif r.get("no_counterpart") or r.get("match_checked"):
            key = "unmatched"
        else:
            key = "unsearched"
        counts[key] += 1
    counts["rejected"] = sum(state(p) == "rejected" for p in doc["pairs"])
    return counts


def overlap_conflicts(doc, pair, candidates=None):
    """Separate split recordings cannot occupy the same flight time.

    Permit one source frame of timestamp rounding at a segment boundary.
    Never trim or move a saved alignment to make incompatible parts fit.
    """
    records = {r["id"]: r for r in doc["videos"]}
    conflicts = []
    for other in candidates if candidates is not None else doc["pairs"]:
        if (other["id"] == pair["id"] or other["stick"] != pair["stick"] or other.get("stale")
            or (candidates is None and not other.get("confirmed"))):
            continue
        start = max(pair["radio_start"], other["radio_start"])
        end = min(pair["radio_start"] + pair["overlap_duration"], other["radio_start"] + other["overlap_duration"])
        rates = [records.get(k, {}).get("metadata", {}).get("fps", 30) or 30
                 for k in (pair["fpv"], other["fpv"], pair["stick"])]
        if end - start > max(1 / rate for rate in rates) + 1e-6:
            conflicts.append({"pair": other["id"], "name": records.get(other["fpv"], {}).get("name", "FPV recording"),
                              "start": start, "end": end, "seconds": end - start})
    return conflicts


def flights(doc):
    records = {r["id"]: r for r in doc["videos"]}
    owners = {}
    for p in doc["pairs"]:
        if p.get("confirmed") and not p.get("stale"):
            owners.setdefault(p["fpv"], set()).add(p["stick"])
    groups = []
    for stick in (r for r in doc["videos"] if r["kind"] == "stick"):
        pairs = [p for p in doc["pairs"] if p["stick"] == stick["id"] and not p.get("stale") and state(p) != "rejected"]
        if not pairs:
            continue
        parts = sorted(pairs, key=lambda p: (p["radio_start"], not p.get("confirmed")))
        confirmed = [p for p in parts if p.get("confirmed")]
        # Build one plausible non-overlapping sequence. Alternative/conflicting
        # candidates remain in the list so the pilot can preview and correct them.
        priority = lambda p: (not p.get("confirmed"), p.get("confidence") != "Strong", state(p) == "later",
                              p.get("method") == "timestamp", -(p.get("score") or 0), p["radio_start"], p["id"])
        chosen, blocked = [], {}
        for p in sorted(parts, key=priority):
            conflicts = overlap_conflicts(doc, p, chosen)
            other_owner = owners.get(p["fpv"], set()) - {stick["id"]}
            if conflicts or other_owner:
                blocked[p["id"]] = {"overlap_with": conflicts, "other_owner": bool(other_owner)}
            else:
                chosen.append(p)
        chosen.sort(key=lambda p: p["radio_start"])
        display_starts, cursor = {}, 0.
        for p in chosen:
            display_starts[p["id"]] = max(cursor, p["radio_start"])
            cursor = p["radio_start"] + p["overlap_duration"]
        gaps, overlaps, last_end, previous = [], [], None, []
        for p in confirmed:
            start, end = p["radio_start"], p["radio_start"] + p["overlap_duration"]
            if last_end is not None:
                if start > last_end + .1:
                    gaps.append({"start": last_end, "end": start})
            overlaps.extend({"start": c["start"], "end": c["end"]} for c in overlap_conflicts(doc, p, previous))
            previous.append(p)
            last_end = max(last_end or 0, end)
        groups.append({"id": stick["id"], "name": stick["name"], "duration": stick.get("metadata", {}).get("duration"),
                       "parts": [{"id": p["id"], "name": records.get(p["fpv"], {}).get("name", "Missing FPV"),
                                  "start": p["radio_start"], "end": p["radio_start"] + p["overlap_duration"], "state": state(p),
                                  "fpv": p["fpv"], "confidence": p.get("confidence", "Candidate"),
                                  "on_timeline": p["id"] not in blocked,
                                  "display_start": display_starts.get(p["id"], p["radio_start"]),
                                  "overlap_with": blocked.get(p["id"], {}).get("overlap_with", []),
                                  "other_owner": blocked.get(p["id"], {}).get("other_owner", False),
                                  "conflict": len(owners.get(p["fpv"], set())) > 1 or bool(blocked.get(p["id"]))} for p in parts],
                       "gaps": gaps, "overlaps": overlaps, "confirmed": len(confirmed)})
    return groups


def enrich(doc):
    doc["review_summary"] = summary(doc)
    doc["flights"] = flights(doc)
    for p in doc["pairs"]:
        p["sync_check"] = sync_check(p)
    return doc
