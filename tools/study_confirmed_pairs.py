"""Read-only, reproducible content study of independently confirmed flight pairs.

Outputs are separate from production caches, calibration and match decisions.
Saved offsets are reference labels only; searches must cover every feasible lag.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fpv_matcher.motion.telemetry import cached_telemetry
from fpv_matcher.video.frames import jpeg_frame


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def snapshot(session, output, audio_session=None):
    job = json.loads(session.read_text())
    rows = []
    for pair in job.get("pair_overrides", {}).values():
        if pair.get("verification") != "independent":
            continue
        sources = {}
        for kind, key in (("fpv", "fpv_video"), ("radio", "radio_video")):
            record = next(r for r in job["inputs"][kind] if r["name"] == pair[key])
            stat = Path(record["path"]).stat()
            signature = [stat.st_size, stat.st_mtime_ns]
            if signature != pair["source_signatures"][kind]:
                raise ValueError(f"Confirmed {kind} source changed: {record['name']}")
            sources[kind] = {"name": record["name"], "path": record["path"],
                             "signature": signature, "metadata": record["metadata"],
                             "content_id": record.get("content_id")}
        rows.append({"sources": sources, "reference_offset": pair["offset_seconds"], "project": "motion"})
    if audio_session:
        sister = json.loads(audio_session.read_text())
        videos = {v["id"]: v for v in sister["videos"]}
        for pair in sister["pairs"]:
            if not pair.get("confirmed") or pair.get("stale"):
                continue
            sources = {}
            for kind, key in (("fpv", "fpv"), ("radio", "stick")):
                record = videos[pair[key]]
                stat = Path(record["path"]).stat()
                signature = [stat.st_size, stat.st_mtime_ns]
                if signature != record["signature"]:
                    raise ValueError(f"Sister project source changed: {record['name']}")
                sources[kind] = {"name": record["name"], "path": record["path"],
                                 "signature": signature, "metadata": record["metadata"], "content_id": None}
            rows.append({"sources": sources, "reference_offset": pair["offset"], "project": "audio",
                         "reference_points": pair.get("sync_points", [])})
    write_json(output / "reference.json", {"session_id": job["id"], "session": job["label"],
               "pairs": rows, "reference_is_not_search_constraint": True})
    return rows


def inventory(pairs, output):
    previous = json.loads((output / "inventory.json").read_text()) if (output / "inventory.json").exists() else []
    summary = []
    for number, pair in enumerate(pairs, 1):
        for kind, record in pair["sources"].items():
            started = time.perf_counter()
            path = record["path"]
            prior = next((v for v in previous if v["pair"] == number and v["kind"] == kind and v["name"] == record["name"]), None)
            if prior and (output / f"p{number}-{kind}-contact.jpg").exists():
                summary.append(prior)
                continue
            print(f"Inspecting pair {number} {kind}: {record['name']}", flush=True)
            item = {"pair": number, "kind": kind, "name": record["name"], **record["metadata"]}
            try:
                data = cached_telemetry(path, output / "cache", record["content_id"])
                item["telemetry"] = {"kind": str(data["kind"].item()), "samples": len(data["time"]),
                    "range": [float(data["time"][0]), float(data["time"][-1])],
                    "label": str(data["label"].item()), "note": str(data["note"].item())}
                np.savez_compressed(output / f"p{number}-{kind}-telemetry.npz", **data)
            except (ValueError, RuntimeError, OSError) as exc:
                item["telemetry"] = {"unavailable": str(exc)}
            duration = record["metadata"]["duration"]
            times = np.linspace(min(2., duration / 10), max(.1, duration - 2), 8)
            sheet = Image.new("RGB", (4 * 400, 2 * 330), "#111827")
            draw = ImageDraw.Draw(sheet)
            for i, at in enumerate(times):
                frame = Image.open(io.BytesIO(jpeg_frame(path, float(at), 800, 600))).convert("RGB")
                frame.save(output / f"p{number}-{kind}-{at:.3f}.jpg", quality=92)
                frame.thumbnail((400, 300))
                x, y = (i % 4) * 400, (i // 4) * 330
                sheet.paste(frame, (x, y))
                draw.text((x + 6, y + 306), f"{kind} {at:.3f}s", fill="white")
            sheet.save(output / f"p{number}-{kind}-contact.jpg", quality=92)
            item["elapsed_seconds"] = time.perf_counter() - started
            summary.append(item)
            write_json(output / "inventory.json", summary)
            print(json.dumps(item), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audio-session", type=Path)
    parser.add_argument("--stage", choices=("inventory",), default="inventory")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    pairs = snapshot(args.session, args.output, args.audio_session)
    inventory(pairs, args.output)


if __name__ == "__main__":
    main()
