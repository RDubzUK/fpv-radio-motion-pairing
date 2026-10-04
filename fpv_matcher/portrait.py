"""Persistable 9:16 split layouts and matching FFmpeg composition geometry."""
from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

DEFAULT = {"split": .6, "fpv": {"zoom": .6, "x": 0, "y": 0, "rotation": 0, "background": "blur"},
           "radio": {"zoom": 1, "x": 0, "y": 0, "rotation": 0, "background": "black"}, "audio": "radio", "in": 0, "out": None}


def validate_layout(layout, duration=None):
    if not isinstance(layout, dict):
        raise ValueError("Portrait layout must be an object")
    result = {"version": 1}
    def number(value, low, high, label):
        value = float(value)
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{label} must be between {low} and {high}")
        return value
    result["split"] = number(layout.get("split", .6), .2, .8, "FPV pane share")
    for kind in ("fpv", "radio"):
        layer = layout.get(kind, DEFAULT[kind])
        if not isinstance(layer, dict):
            raise ValueError("Each video needs a transform object")
        result[kind] = {"zoom": number(layer.get("zoom", DEFAULT[kind]["zoom"]), .1, 4, "Zoom"),
            "x": number(layer.get("x", 0), -100, 100, "Horizontal position"),
            "y": number(layer.get("y", 0), -100, 100, "Vertical position"),
            "rotation": number(layer.get("rotation", 0), -180, 180, "Rotation"), "background": layer.get("background", DEFAULT[kind]["background"])}
        if result[kind]["background"] not in {"blur", "black"}:
            raise ValueError("Choose blurred video or black background")
    result["audio"] = layout.get("audio", "radio")
    if result["audio"] not in {"radio", "fpv", "none"}:
        raise ValueError("Choose StickCam, FPV or no audio")
    result["in"] = number(layout.get("in", 0), 0, duration if duration is not None else 86400, "In point")
    result["out"] = None if layout.get("out") is None else number(layout["out"], 0, duration if duration is not None else 86400, "Out point")
    end = result["out"] if result["out"] is not None else duration
    if end is not None and end <= result["in"]:
        raise ValueError("Out point must follow the in point")
    return result


def export_portrait(info, radio_path: Path, fpv_path: Path, directory: Path, fps=30, layout=None, run=None, progress=None):
    layout = validate_layout(layout or DEFAULT, info["overlap_duration"])
    end = layout["out"] if layout["out"] is not None else info["overlap_duration"]
    count = math.floor((end - layout["in"]) * fps + 1e-7)
    if count < 1:
        raise ValueError("Portrait interval is shorter than one frame")
    duration = count / fps
    width, height = 1080, 1920
    top = round(height * layout["split"] / 2) * 2
    filters = []
    for index, kind, pane_height in ((0, "fpv", top), (1, "radio", height - top)):
        transform = layout[kind]
        metadata = info["source_metadata"][kind]
        scale = max(width / metadata["width"], pane_height / metadata["height"]) * transform["zoom"]
        sw = max(2, round(metadata["width"] * scale / 2) * 2)
        sh = max(2, round(metadata["height"] * scale / 2) * 2)
        angle = math.radians(transform["rotation"])
        rw = max(2, math.ceil((sw * abs(math.cos(angle)) + sh * abs(math.sin(angle))) / 2) * 2)
        rh = max(2, math.ceil((sh * abs(math.cos(angle)) + sw * abs(math.sin(angle))) / 2) * 2)
        base_chain = f"[{index}:v:0]fps={fps}:start_time=0:round=near,setpts=N/({fps}*TB)"
        if transform["background"] == "blur":
            filters.append(base_chain + f",split=2[src{index}][bg{index}]")
            filters.append(f"[bg{index}]scale={width}:{pane_height}:force_original_aspect_ratio=increase,crop={width}:{pane_height},gblur=sigma=24,setsar=1,format=rgba[base{index}]")
            base_chain = f"[src{index}]"
        else:
            filters.append(f"color=c=black:s={width}x{pane_height}:r={fps}:d={duration:.9f},format=rgba[base{index}]")
            base_chain += ","
        filters.append(base_chain + f"scale={sw}:{sh},setsar=1,format=rgba,rotate={angle:.12f}:ow={rw}:oh={rh}:c=black@0[layer{index}]")
        filters.append(f"[base{index}][layer{index}]overlay=x=(W-w)/2+{transform['x'] / 100 * width / 2:.6f}:y=(H-h)/2+{transform['y'] / 100 * pane_height / 2:.6f}:shortest=1:eof_action=endall:format=auto[pane{index}]")
    filters.append("[pane0][pane1]vstack=inputs=2,format=yuv420p[outv]")
    command = [shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-y", "-v", "error", "-threads", "2",
        "-ss", f"{info['fpv_start'] + layout['in']:.9f}", "-i", str(fpv_path),
        "-threads", "2", "-ss", f"{info['radio_start'] + layout['in']:.9f}", "-i", str(radio_path),
        "-filter_complex_threads", "1", "-filter_complex", ";".join(filters), "-map", "[outv]"]
    audio_kind = layout["audio"]
    if audio_kind != "none" and info["source_metadata"][audio_kind].get("has_audio"):
        command += ["-map", "0:a:0?" if audio_kind == "fpv" else "1:a:0?", "-af", f"aresample=async=1:first_pts=0,apad,atrim=duration={duration:.9f},asetpts=N/SR/TB", "-c:a", "aac", "-b:a", "192k"]
    else:
        command += ["-an"]
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "portrait_9x16.mp4"
    command += ["-frames:v", str(count), "-t", f"{duration:.9f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-threads", "2", "-map_metadata", "-1", "-movflags", "+faststart", str(destination)]
    if progress:
        progress("Encoding 1080 × 1920 portrait composite")
    if run:
        run(command, directory / "portrait.log")
    else:
        result = subprocess.run(command, capture_output=True, text=True, timeout=8 * 3600)
        if result.returncode:
            raise RuntimeError(result.stderr[-1500:])
    probe = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,nb_frames,duration", "-of", "json", str(destination)], capture_output=True, text=True, check=True, timeout=60)
    stream = json.loads(probe.stdout)["streams"][0]
    if int(stream.get("nb_frames", 0)) != count or stream["width"] != width or stream["height"] != height or abs(float(stream.get("duration", 0)) - duration) > .001:
        raise RuntimeError("Portrait export did not encode the complete selected interval")
    manifest = {**info, "layout": layout, "export": destination.name, "export_fps": fps, "export_frame_count": count,
                "export_duration": duration, "width": width, "height": height, "top_height": top,
                "note": "Both sources use the saved alignment. Static transforms are clipped independently at the horizontal split; zoom is relative to filling each pane. This export does not bake in the radio stabilisation preview."}
    (directory / "alignment.json").write_text(json.dumps(manifest, indent=2, allow_nan=False))
    return manifest
