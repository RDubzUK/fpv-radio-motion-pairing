"""Choose a real preview alignment without treating empty scores as evidence."""
from __future__ import annotations

import math


def number(value, default=0.):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def alignment(row):
    score = max(0., number(row.get("combined_score")))
    offset = number(row.get("offset_seconds"), None)
    method, suggested = "unavailable", False
    if row.get("reviewed") or row.get("manual_pair"):
        method, suggested = "manual" if row.get("manual_pair") else "confirmed", True
    elif row.get("timing_only"):
        method, suggested = "timestamp", True
    elif score > 0 and offset is not None:
        method, suggested = "content", bool(row.get("global_assignment"))
    # Experimental evidence has its own lag. An empty axis/audio comparison's
    # default zero offset must never override that observed movement timing.
    if not row.get("reviewed") and not row.get("manual_pair") and not row.get("timing_only") and not row.get("global_assignment"):
        for key in ("gimbal_activity", "rhythm"):
            evidence = row.get(key) or {}
            value = number(evidence.get("score"))
            candidate_offset = number(evidence.get("offset_seconds"), None)
            if value > score and candidate_offset is not None:
                method, score, offset = key, value, candidate_offset
                suggested = bool(evidence.get("suggested"))
    available = method != "unavailable" and offset is not None and not row.get("review_invalidated")
    reasons = row.get("review_reasons") or []
    reason = (row.get("review_reason") if row.get("review_invalidated") else
              "No matching alignment was found. " + (str(reasons[0]) if reasons else
              "Check radio-region calibration and available audio/movement data, or find a pair manually."))
    return {"alignment_available": available, "alignment_method": method if available else "unavailable",
            "alignment_offset_seconds": offset if available else None,
            "alignment_score": score if available else 0., "alignment_suggested": available and suggested,
            "alignment_reason": "" if available else reason}


def annotate(row, radio_duration=None, fpv_duration=None):
    row.update(alignment(row))
    offset = row["alignment_offset_seconds"]
    row["alignment_overlap_seconds"] = (max(0., min(radio_duration-max(0., offset), fpv_duration-max(0., -offset)))
        if offset is not None and radio_duration is not None and fpv_duration is not None else None)
    return row
