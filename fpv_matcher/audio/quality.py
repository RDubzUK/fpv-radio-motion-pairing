"""Measured results on explicitly labelled reference pairs, not probabilities."""
from __future__ import annotations

import time

from .evidence import POLICY_VERSION, assess_audio_pair
from .fingerprint import ALGORITHM_VERSION


def benchmark(doc, load_audio, boundary, progress, flag):
    refs = doc.get("references", [])
    records = {r["id"]: r for r in doc["videos"]}
    sticks = [r for r in doc["videos"] if r["kind"] == "stick" and r.get("metadata", {}).get("has_audio") and not r.get("missing")]
    fpv_ids = {r["fpv"] for r in refs}
    used = [r for r in doc["videos"] if r["id"] in fpv_ids or r in sticks]
    data, errors = {}, {}
    for i, r in enumerate(used):
        progress(40 * i / max(1, len(used)), f"Reference audio {i + 1}/{len(used)} · {r['name']}")
        try:
            data[r["id"]] = load_audio(r, boundary)
        except Exception as exc:
            errors[r["id"]] = str(exc)
    ranked = {}
    for i, fid in enumerate(sorted(fpv_ids)):
        progress(40 + 50 * i / max(1, len(fpv_ids)), f"Reference comparisons {i + 1}/{len(fpv_ids)}")
        candidates = []
        for r in sticks:
            if flag.is_set():
                raise RuntimeError("Cancelled")
            if fid in data and r["id"] in data:
                e = assess_audio_pair(data[r["id"]], data[fid], doc.get("min_overlap", 2))
                candidates.append({"stick": r["id"], "evidence": e,
                                   "score": e["score"] if e["eligible"] else e["best_score"]})
        candidates.sort(key=lambda c: c["score"], reverse=True)
        ranked[fid] = candidates
    rows, counts = [], {"true_positive": 0, "false_positive": 0, "true_negative": 0, "false_negative": 0, "skipped": 0}
    for ref in refs:
        candidate = next((c for c in ranked.get(ref["fpv"], []) if c["stick"] == ref["stick"]), None)
        row = {**ref, "fpv_name": records.get(ref["fpv"], {}).get("name"), "stick_name": records.get(ref["stick"], {}).get("name")}
        source_changed = any(ref.get("content_ids", {}).get(k) and
                             ref["content_ids"][k] != records.get(ref[k], {}).get("content_id") for k in ("fpv", "stick"))
        if not candidate or source_changed:
            counts["skipped"] += 1
            rows.append(row | {"status": "skipped", "reason": "Reference source changed" if source_changed else
                         errors.get(ref["fpv"]) or errors.get(ref["stick"]) or "Missing or unusable source/audio"})
            continue
        e = candidate["evidence"]
        others = [c["score"] for c in ranked[ref["fpv"]] if c is not candidate]
        margin = candidate["score"] - max(others, default=0)
        owner_strength = .6 * e["landmark_strength"] + .4 * e["trend_score"]
        competing_strength = max((.6 * c["evidence"]["landmark_strength"] + .4 * c["evidence"]["trend_score"] for c in ranked[ref["fpv"]] if c is not candidate), default=0)
        predicted = e["eligible"] and margin >= .08 and owner_strength - competing_strength >= .1
        offset = e["offset"] if e["eligible"] else e["best_offset"]
        error = abs(offset - ref["expected_offset"]) if ref["match"] and ref.get("expected_offset") is not None else None
        correct = predicted and (error is None or error <= ref.get("tolerance", .15))
        status = ("true_positive" if correct else "false_negative") if ref["match"] else ("false_positive" if predicted else "true_negative")
        counts[status] += 1
        rows.append(row | {"status": status, "predicted_strong": bool(predicted), "estimated_offset": offset,
                           "offset_error": error, "owner_margin": margin, "reasons": e["reasons"]})
    tp, fp, fn = counts["true_positive"], counts["false_positive"], counts["false_negative"]
    negatives = sum(not row["match"] and row["status"] != "skipped" for row in rows)
    return {"created": time.time(), "boundary": boundary, "policy": POLICY_VERSION, "algorithm": ALGORITHM_VERSION,
            "timing_references": sum(r.get("offset_error") is not None for r in rows),
            "counts": counts, "negative_references": negatives, "precision": tp / (tp + fp) if negatives and tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None, "rows": rows,
            "note": "Measured only on this labelled collection. Precision needs labelled non-matches. Wrong-offset positive cases count as failures. Audio gates only; motion fallback and learned dates are excluded. Scores remain evidence, not probabilities."}
