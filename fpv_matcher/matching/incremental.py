"""Selected-FPV search with an explicit operator decision before skipping clips."""
from __future__ import annotations

import csv
import json
import time
import uuid
from pathlib import Path

import numpy as np

from fpv_matcher.matching.assignment import assign, candidate_margin
from fpv_matcher.pipeline import atomic_json, run_batch


def run_selected(fpv_paths, radio_paths, config, output, cache, settings, progress=None):
    names = settings.get("match_target_names") or [settings.get("match_target_name")]
    targets = [p for p in fpv_paths if p.name in names]
    radios = [p for p in radio_paths if not config.get("clips", {}).get(p.name, {}).get("exclude")]
    options = {**settings, "match_target_kind": "all", "match_target_name": "", "match_target_names": [],
               "stop_on_confident_match": False, "make_plots": False}
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    rows, errors, clips, unscanned = [], [], {"fpv": {}, "radio": {}}, {}
    confirmed_radio = set(); started = time.monotonic()
    def report(stage, message, percent, **extra):
        event = {"stage": stage, "message": message, "percent": round(percent, 1), **extra}
        atomic_json(output / "progress.json", event)
        if progress: progress(event)
    for ti, target in enumerate(targets):
        scanned = set()
        for ri, radio in enumerate(radios):
            if radio.name in confirmed_radio:
                continue
            scanned.add(radio.name)
            def update(event):
                fraction = (ti + (ri + event["percent"] / 100) / max(1, len(radios))) / max(1, len(targets))
                report(event["stage"], f"FPV {ti + 1}/{len(targets)} · StickCam {ri + 1}/{len(radios)} · {event['message']}", fraction * 95,
                       **{k: v for k, v in event.items() if k not in {"stage", "message", "percent"}})
            try:
                summary = run_batch([target], [radio], config, output / "scan" / f"{ti:03d}-{ri:03d}", cache, **options, progress=update)
            except Exception as exc:
                errors.append({"kind": "comparison", "name": radio.name, "fpv_video": target.name, "error": str(exc)})
                continue
            rows.extend(summary["rows"]); errors.extend(summary["errors"])
            for kind in clips:
                clips[kind].update({c["name"]: c for c in summary["clips"][kind]})
            proposal = next((r for r in summary["rows"] if r["confidence"] == "strong"), None)
            remaining = [p.name for p in radios if p.name not in scanned and p.name not in confirmed_radio]
            if proposal and remaining:
                nonce = uuid.uuid4().hex
                pending = {"status": "pending", "nonce": nonce, "candidate": proposal, "remaining": remaining,
                           "scanned": len(scanned), "total": len(radios), "target_index": ti + 1, "targets": len(targets)}
                atomic_json(output / "early.json", pending)
                report("review", f"Strong motion evidence for {target.name}; {len(remaining)} StickCam clips remain unscanned. Review or continue.", (ti + (ri + 1) / len(radios)) / len(targets) * 95)
                decision = output / f"decision-{nonce}.json"
                while not decision.exists():
                    time.sleep(.5)
                action = json.loads(decision.read_text())["action"]
                atomic_json(output / "early.json", {**pending, "status": "resolved", "decision": action})
                if action == "confirm":
                    confirmed_radio.add(radio.name); unscanned[target.name] = remaining
                    break
        else:
            unscanned[target.name] = []
    if not rows:
        atomic_json(output / "errors.json", {"errors": errors})
        raise ValueError("No usable comparisons remain: " + "; ".join(e["error"] for e in errors[:3]))
    rn, fn = sorted(clips["radio"]), sorted(clips["fpv"])
    matrix = np.zeros((len(rn), len(fn)))
    for row in rows:
        matrix[rn.index(row["radio_video"]), fn.index(row["fpv_video"])] = row["combined_score"]
    selected = set(assign(matrix, settings.get("minimum_score", .55)))
    for row in rows:
        ri, fi = rn.index(row["radio_video"]), fn.index(row["fpv_video"])
        row["candidate_margin"] = candidate_margin(matrix, ri, fi)
        row["global_assignment"] = (ri, fi) in selected
        if row["candidate_margin"] < .08:
            row["confidence"] = "review"
    result = {"version": 3, "rows": rows, "errors": errors, "clips": {k: list(v.values()) for k, v in clips.items()},
        "rate_profile": settings.get("rate_profile"), "settings": settings,
        "clock": {"delta_seconds": None, "confidence": 0, "support": 0},
        "excluded_radio": [p.name for p in radio_paths if p not in radios], "considered_radio": [p.name for p in radios],
        "considered_fpv": names, "unscanned_radio": unscanned,
        "match_scope": {"kind": "fpv", "target": names[0] if len(names) == 1 else None, "targets": names, "early_review": True},
        "unmatched_radio": [name for name in rn if not any(r["global_assignment"] and r["radio_video"] == name for r in rows)],
        "unmatched_fpv": [name for name in names if not any(r["global_assignment"] and r["fpv_video"] == name for r in rows)],
        "elapsed_seconds": round(time.monotonic() - started, 1)}
    atomic_json(output / "results.json", result)
    keys = [k for k in rows[0] if k not in {"alternatives", "axis_details"}]
    with (output / "matches.csv.tmp").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=keys); writer.writeheader()
        writer.writerows({k: r[k] for k in keys} for r in rows)
    (output / "matches.csv.tmp").replace(output / "matches.csv")
    report("complete", "Selected FPV search complete. Confirmed early proposals may leave clips unscanned.", 100)
    return result
