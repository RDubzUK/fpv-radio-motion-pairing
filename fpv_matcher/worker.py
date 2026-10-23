"""Installed-module entry point for a cancellable batch process."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fpv_matcher.pipeline import run_batch
from fpv_matcher.session_scope import active_job


def main():
    base, cache = Path(sys.argv[1]), Path(sys.argv[2])
    job = active_job(json.loads((base / "job.json").read_text(encoding="utf-8")))
    paths = json.loads((base / "inputs.json").read_text(encoding="utf-8"))
    paths = {kind:[p for p in values if p in {r["path"] for r in job["inputs"][kind]}] for kind,values in paths.items()}
    config = json.loads((base / "radio_rois.json").read_text(encoding="utf-8"))
    from fpv_matcher.matching.quick_seed import enabled, run_seed
    if enabled(job["settings"],job["inputs"]):
        result = run_seed([Path(p) for p in paths["fpv"]],[Path(p) for p in paths["radio"]],config,
                          base / "results",cache,job["settings"],
                          progress=lambda event:print(event["message"],flush=True),
                          fallback=job["settings"].get("matching_strategy") != "telemetry_seed")
        if result is not None:
            return
    batch_settings = {k:v for k,v in job["settings"].items() if k not in {"embedded_seed_enabled","seed_spot_checks"}}
    if (job["settings"].get("matching_strategy", "selected") != "automatic"
            and job["settings"].get("match_target_kind") == "fpv"
            and job["settings"].get("match_sample_percent", 100) == 100
            and job["settings"].get("stop_on_confident_match", False)):
        from fpv_matcher.matching.incremental import run_selected
        run_selected([Path(p) for p in paths["fpv"]], [Path(p) for p in paths["radio"]], config, base / "results", cache,
                     batch_settings, progress=lambda event: print(event["message"], flush=True))
        return
    run_batch(
        [Path(p) for p in paths["fpv"]],
        [Path(p) for p in paths["radio"]],
        config,
        base / "results",
        cache,
        **batch_settings,
        progress=lambda event: print(event["message"], flush=True),
    )


if __name__ == "__main__":
    main()
