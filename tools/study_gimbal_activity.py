"""Study gimbal/finger activity without assuming a visible stick tip.

Hand-marked housing geometry is local study setup, not a confirmed calibration.
The fixed reference must be reacquired; missing poses produce explicit gaps.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fpv_matcher.radio.outline import RadioOutlineTracker
from fpv_matcher.video.reader import sampled_frames

# Coordinates refer to the inspected 800px contact frames, not the source size.
# Geometry was selected from image appearance alone, before content comparison.
REFERENCES = {
    1: {"time": 31.830, "outline": [[348, 283], [555, 280], [592, 505], [319, 490]],
        "centres": [[393, 326], [517, 325]], "widths": [72, 72], "control": [450, 308]},
    2: {"time": 2., "outline": [[326, 238], [564, 227], [576, 459], [308, 467]],
        "centres": [[370, 285], [525, 282]], "widths": [88, 88], "control": [445, 258]},
    3: {"time": 89.074, "outline": [[291, 181], [551, 165], [611, 448], [247, 477]],
        "centres": [[329, 240], [512, 224]], "widths": [100, 100], "control": [415, 204]},
    4: {"time": 2., "outline": [[243, 224], [493, 203], [549, 459], [233, 486]],
        "centres": [[286, 285], [462, 267]], "widths": [100, 100], "control": [370, 248]},
}
FPS, DIMENSION = 10., 960


def main(output, numbers, variant="baseline"):
    cv2.setNumThreads(1)
    pairs = json.loads((output / "reference.json").read_text())["pairs"]
    summary = []
    for number in numbers:
        suffix = "" if variant == "baseline" else "-" + variant
        target = output / f"p{number}-gimbal-activity{suffix}.npz"
        if target.exists():
            continue
        spec, record = REFERENCES[number], pairs[number-1]["sources"]["radio"]
        if variant == "alternate" and number == 2:
            spec = {"time": 53.647, "outline": [[352, 187], [600, 146], [706, 395], [347, 407]],
                    "centres": [[397, 219], [562, 194]], "widths": [88, 100], "control": [479, 183]}
        if variant == "alternate" and number == 4:
            spec = {"time": 60.183, "outline": [[258, 188], [509, 164], [588, 459], [224, 495]],
                    "centres": [[304, 250], [483, 235]], "widths": [100, 100], "control": [394, 208]}
        path = record["path"]
        scale = DIMENSION / 800
        outline = np.float32(spec["outline"]) * scale
        regions = []
        for center, width in zip(spec["centres"], spec["widths"]):
            x, y = np.asarray(center) * scale
            w = width * scale
            regions.append({"x": x-w/2, "y": y-w/2, "width": w, "height": w})
        frame_generator = sampled_frames(path, FPS, DIMENSION, start=spec["time"], duration=1/FPS)
        try:
            reference = next(frame_generator)[1]
        finally:
            frame_generator.close()
        shell = RadioOutlineTracker(outline, regions, FPS)
        shell.initialize(reference)
        shell._lost("Starting on the source timeline rather than at reference")
        previous = reference
        previous_crops, previous_quality = None, 0.
        rows, crops_for_review, times_for_review = [], [], []
        last_review = -100.
        started = time.perf_counter()
        yy, xx = np.mgrid[0:96, 0:96]
        inner = ((xx-48)**2+(yy-48)**2 < 33**2)
        transforms = []
        for region in regions:
            transforms.append(np.float64([[region["width"]/96, 0, region["x"]],
                                         [0, region["height"]/96, region["y"]], [0, 0, 1]]))
        control = np.asarray(spec["control"]) * scale
        transforms.append(np.float64([[.35, 0, control[0]-16.8], [0, .35, control[1]-16.8], [0, 0, 1]]))
        for timestamp, frame in sampled_frames(path, FPS, DIMENSION):
            transform, quality = shell.update(previous, frame)
            row = {"time": timestamp, "quality": quality, "motion_quality": min(quality, previous_quality)}
            crops = None
            for name in ("left", "right", "control"):
                for feature in ("activity", "difference", "vx", "vy", "bright_x", "bright_y", "dark_x", "dark_y"):
                    row[name + "_" + feature] = 0.
            if transform is not None:
                crops = [cv2.warpPerspective(frame, transform @ crop_transform, (96, 96),
                            flags=cv2.INTER_LINEAR|cv2.WARP_INVERSE_MAP, borderValue=127) for crop_transform in transforms]
                for i, (name, crop) in enumerate(zip(("left", "right", "control"), crops)):
                    value = crop.astype(float)
                    low, high = np.percentile(value, [10, 90])
                    normalized = np.clip((value-low) / max(15, high-low), 0, 1)
                    for key, weights in (("bright", np.maximum(normalized-.55, 0)*inner),
                                         ("dark", np.maximum(.3-normalized, 0)*inner)):
                        total = max(1e-8, weights.sum())
                        row[name+"_"+key+"_x"] = float((weights*(xx-48)).sum()/total/33)
                        row[name+"_"+key+"_y"] = float((weights*(yy-48)).sum()/total/33)
                    if previous_crops is not None and previous_quality >= .35:
                        previous_crop = previous_crops[i]
                        flow = cv2.calcOpticalFlowFarneback(previous_crop, crop, None, .5, 3, 15, 3, 5, 1.2, 0)
                        magnitude = np.linalg.norm(flow, axis=2)
                        # Prefer the moving part inside the housing; never label this a stick position.
                        moving = inner & (magnitude >= max(.15, np.percentile(magnitude[inner], 60)))
                        row[name+"_activity"] = float(np.mean(magnitude[inner])*FPS/33)
                        row[name+"_difference"] = float(np.mean(np.abs(crop.astype(float)-previous_crop)[inner])/255*FPS)
                        if moving.any():
                            row[name+"_vx"], row[name+"_vy"] = np.mean(flow[moving], axis=0)*FPS/33
                if timestamp - last_review >= 10:
                    crops_for_review.append(np.hstack(crops))
                    times_for_review.append(timestamp)
                    last_review = timestamp
            rows.append(row)
            previous, previous_crops, previous_quality = frame, crops, quality
            if len(rows) % 300 == 0:
                print(f"Pair {number}: {timestamp:.1f}s / {record['metadata']['duration']:.1f}s; elapsed {time.perf_counter()-started:.1f}s; shell {quality:.2f}", flush=True)
        data = {k: np.asarray([r[k] for r in rows], float) for k in rows[0]}
        for k in list(data):
            if k.endswith(("activity", "difference", "vx", "vy")):
                data[k+"_quality"] = data["motion_quality"]
        np.savez_compressed(target, **data)
        if crops_for_review:
            contact = np.zeros((len(crops_for_review)*118, 288), np.uint8)
            for i, (at, crop) in enumerate(zip(times_for_review, crops_for_review)):
                contact[i*118:i*118+96] = crop
                cv2.putText(contact, f"{at:.1f}s : left / right / shell control", (4, i*118+112),
                            cv2.FONT_HERSHEY_SIMPLEX, .4, 255, 1)
            cv2.imwrite(str(output/f"p{number}-rectified-gimbals{suffix}.jpg"), contact)
        item = {"pair": number, "setup": spec, "pose_coverage": float(np.mean(data['quality']>=.35)),
                "elapsed_seconds": time.perf_counter()-started, "sample_fps": FPS,
                "note": "Activity and foreground centroids are proxies, not observed stick tips or calibrated rates."}
        summary.append(item)
        (output/f"p{number}-gimbal-summary{suffix}.json").write_text(json.dumps(item, indent=2)+"\n")
        print(json.dumps(item), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pairs", nargs="+", type=int, default=[1, 2, 3, 4])
    parser.add_argument("--variant", choices=("baseline", "alternate"), default="baseline")
    args = parser.parse_args()
    main(args.output, args.pairs, args.variant)
