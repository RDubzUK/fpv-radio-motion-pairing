"""Keep source-current candidates outside a targeted search's comparison scope."""
from __future__ import annotations

from pathlib import Path


def retain_previous(previous, current, job):
    if not previous:
        return current
    known = {(r["radio_video"], r["fpv_video"]) for r in current["rows"]}
    scanned_fpv = set(current.get("considered_fpv", []))
    scanned_radio = set(current.get("considered_radio", []))
    signatures = {}
    for kind, records in job["inputs"].items():
        for record in records:
            try:
                stat = Path(record["path"]).stat()
                signatures[kind, record["name"]] = [stat.st_size, stat.st_mtime_ns]
            except OSError:
                pass
    metadata = {(kind, c["name"]): c for kind, clips in previous["clips"].items() for c in clips}
    retained = 0
    for row in previous["rows"]:
        key = row["radio_video"], row["fpv_video"]
        if key in known or row.get("timing_only") or (key[0] in scanned_radio and key[1] in scanned_fpv):
            continue
        sources = [(kind, row[kind + "_video"]) for kind in ("radio", "fpv")]
        if any((kind, name) not in metadata or signatures.get((kind, name)) != [metadata[kind, name].get("size"), metadata[kind, name].get("mtime_ns")] for kind, name in sources):
            continue
        kept = dict(row, retained_previous_search=True, reviewed=False)
        # New content proposals own their FPV scope. Older alternatives remain
        # reviewable without creating a second proposed owner.
        if kept["fpv_video"] in scanned_fpv:
            kept["global_assignment"] = False
        current["rows"].append(kept)
        retained += 1
        for kind, name in sources:
            if not any(c["name"] == name for c in current["clips"][kind]):
                current["clips"][kind].append(metadata[kind, name])
    current["retained_candidates"] = retained
    return current
