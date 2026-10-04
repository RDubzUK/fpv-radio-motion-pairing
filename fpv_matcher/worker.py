"""Installed-module entry point for a cancellable batch process."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fpv_matcher.pipeline import run_batch


def main():
    base, cache = Path(sys.argv[1]), Path(sys.argv[2])
    job = json.loads((base / "job.json").read_text(encoding="utf-8"))
    paths = json.loads((base / "inputs.json").read_text(encoding="utf-8"))
    config = json.loads((base / "radio_rois.json").read_text(encoding="utf-8"))
    if job["settings"].get("match_target_kind") == "fpv" and job["settings"].get("stop_on_confident_match", False):
        from fpv_matcher.matching.incremental import run_selected
        run_selected([Path(p) for p in paths["fpv"]], [Path(p) for p in paths["radio"]], config, base / "results", cache,
                     job["settings"], progress=lambda event: print(event["message"], flush=True))
        return
    run_batch(
        [Path(p) for p in paths["fpv"]],
        [Path(p) for p in paths["radio"]],
        config,
        base / "results",
        cache,
        **job["settings"],
        progress=lambda event: print(event["message"], flush=True),
    )


if __name__ == "__main__":
    main()
