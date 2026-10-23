"""Post-scoring calibration diagnostic, deliberately separate from the blind run."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np

from benchmark_movement import activity, check_protocol, load_npz, matching, read, write


def diagnose(output, ident, calibration_path):
    check_protocol(output)
    cv2.setNumThreads(1)
    target = output / "diagnostic"
    target.mkdir(exist_ok=True)
    calibration = read(calibration_path)
    record = next(r for r in read(output / "extraction-input.json")["radio"] if r["id"] == ident)
    started, last = time.perf_counter(), [0.]
    def progress(event):
        now = time.perf_counter()
        if now-last[0] >= 20 or event.get("percent", 0) >= 100:
            print(json.dumps({"diagnostic": ident, "percent": event.get("percent"),
                              "elapsed_seconds": round(now-started, 1)}), flush=True)
            last[0] = now
    raw = activity.cached_activity(record["path"], target / "cache", calibration, record["identity"], progress)
    elapsed = time.perf_counter()-started
    radio = matching.prepare_signal(raw, "activity")
    np.savez_compressed(target / f"{ident}.npz", **radio)
    rows = [r for r in read(output / "blind-results.json")["rows"] if r["radio"] != ident]
    for fpv in read(output / "matcher-input.json")["fpv"]:
        rows.append({"radio": ident, "fpv": fpv["id"], **matching.compare(radio, load_npz(Path(fpv["signal"])), 2.)})
    for row in rows:
        row["candidate_separation"] = row["score"]-max(r["score"] for r in rows if r["fpv"] == row["fpv"] and r["radio"] != row["radio"])
        row["suggested"] = bool(row["eligible"] and row["candidate_separation"] >= .04)
    expected = next(r for r in read(output / "truth.json") if r["radio"] == ident)
    ranking = sorted((r for r in rows if r["radio"] == ident), key=lambda r: r["score"], reverse=True)
    actual = next(r for r in ranking if r["fpv"] == expected["fpv"])
    owner_rank = sorted((r for r in rows if r["fpv"] == expected["fpv"]), key=lambda r: r["score"], reverse=True).index(actual)+1
    result = {"description": "Post-scoring geometry-only diagnostic, not a new blind holdout or replacement score",
              "radio": ident, "calibration": calibration, "extraction_seconds": elapsed,
              "observed_percent": float(np.mean(radio["quality"] > .1)*100),
              "observed_seconds": float(np.sum(radio["quality"] > .1)/matching.FPS),
              "correct_fpv_rank": ranking.index(actual)+1, "correct_owner_rank": owner_rank,
              "actual_pair": actual, "ranking": ranking, "rows": rows}
    write(target / "results.json", result)
    print(json.dumps({k: v for k, v in result.items() if k not in {"calibration", "ranking", "rows", "actual_pair"}}), flush=True)
    print("Actual pair",json.dumps({k: v for k, v in actual.items() if k != "sections"}, default=lambda x: x.item()), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("radio_id")
    parser.add_argument("calibration", type=Path)
    args = parser.parse_args()
    diagnose(args.output, args.radio_id, args.calibration)
