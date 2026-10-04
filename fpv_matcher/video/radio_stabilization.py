"""Conservative radio framing for aligned exports, using saved outline poses.

The native FFmpeg timeline contains transformations only. It never decodes
footage in Python or keeps video frames in memory. Every exported frame is
qualified against its interpolated source outline and original pixel bounds;
missing or impossible observations use the unmodified source frame.
"""
from __future__ import annotations

import bisect
import math
from collections import Counter
from pathlib import Path

VERSION = "radio-export-safe-framing-v1"
MAX_POSE_GAP = .35
_PIXEL_MARGIN = 2.0


def _number(value, low, high, label):
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{label} must be between {low:g} and {high:g}")
    return value


def _quad(points, width, height):
    """Reject missing, clipped, self-crossing and severe perspective outlines."""
    if not isinstance(points, (list, tuple)) or len(points) != 4:
        return None, "tracking_gap"
    try:
        quad = [(float(p[0]), float(p[1])) for p in points if len(p) == 2]
    except (TypeError, ValueError, IndexError):
        return None, "invalid_outline"
    if len(quad) != 4 or not all(math.isfinite(v) for p in quad for v in p):
        return None, "invalid_outline"
    if any(x < _PIXEL_MARGIN or y < _PIXEL_MARGIN or x > width - _PIXEL_MARGIN
           or y > height - _PIXEL_MARGIN for x, y in quad):
        return None, "radio_outside_frame"
    edges = [(quad[(i + 1) % 4][0] - quad[i][0],
              quad[(i + 1) % 4][1] - quad[i][1]) for i in range(4)]
    lengths = [math.hypot(*edge) for edge in edges]
    crosses = [edges[i][0] * edges[(i + 1) % 4][1]
               - edges[i][1] * edges[(i + 1) % 4][0] for i in range(4)]
    area = abs(sum(quad[i][0] * quad[(i + 1) % 4][1]
                   - quad[(i + 1) % 4][0] * quad[i][1] for i in range(4))) / 2
    if min(lengths) < 8 or area < width * height * .005 or not (
            all(c > 0 for c in crosses) or all(c < 0 for c in crosses)):
        return None, "invalid_outline"
    aspect = (lengths[0] + lengths[2]) / (lengths[1] + lengths[3])
    if not .3 <= aspect <= 4 or any(
            max(lengths[i], lengths[i + 2]) / min(lengths[i], lengths[i + 2]) > 2
            for i in (0, 1)):
        return None, "severe_perspective"
    # Rotation/crop preserves perspective; it cannot rectify an oblique shell.
    if any(abs(sum(edges[i][axis] * edges[(i + 1) % 4][axis] for axis in (0, 1)))
           / (lengths[i] * lengths[(i + 1) % 4]) > .55 for i in range(4)):
        return None, "severe_perspective"
    return quad, None


def validate_stabilization(request):
    """Normalize a source-pixel pose request without silently accepting no data."""
    if not isinstance(request, dict):
        raise ValueError("Stabilized StickCam export needs saved radio outline poses")
    width = int(_number(request.get("width"), 16, 16384, "StickCam source width"))
    height = int(_number(request.get("height"), 16, 16384, "StickCam source height"))
    fill = _number(request.get("fill", .78), .5, .9, "Radio fill")
    maximum = _number(request.get("max_zoom", 2), 1, 3, "Maximum stabilization zoom")
    poses = request.get("poses")
    if not isinstance(poses, (list, tuple)) or not poses:
        raise ValueError("No saved radio outline observations; derive the calibrated StickCam first")
    previous, valid, normalized = -1., 0, []
    for row in poses:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            raise ValueError("Invalid saved radio pose timeline")
        at = _number(row[0], 0, 86400, "Radio pose time")
        if at <= previous:
            raise ValueError("Radio pose times must increase")
        previous = at
        quad, reason = _quad(row[1], width, height)
        quality = _number(row[3], 0, 1, "Radio pose quality") if len(row) > 3 else None
        if quality is not None and quality < .35:
            quad, reason = None, "tracking_gap"
        source = row[2] if len(row) > 2 and row[2] in {"outline", "gyro-assisted"} else "outline"
        valid += quad is not None
        normalized.append([at, quad, source, reason])
    if valid < 2:
        raise ValueError("Not enough fully visible, valid radio outlines for stabilized export")
    return {**request, "version": VERSION, "width": width, "height": height,
            "fill": fill, "max_zoom": maximum, "_qualified_poses": normalized,
            "valid_observations": valid, "pose_corner_order": "physical-upright"}


def stabilization_request(data, metadata, *, gyro=None, source_signature=None,
                          radio_rotation_deg=0, fill=.78, max_zoom=2.0):
    """Build an export request from *current, already cached* raw StickCam data.

    Callers must bind the cache to the current calibration and media signature.
    This helper uses the same optical observations as the preview; it does not
    run vision or treat camera gyro as pilot control data.
    """
    from fpv_matcher.radio.stabilization import review_poses
    if "outline_0_x" not in data or "radio_orientation_quality" not in data:
        raise ValueError("This StickCam cache has no radio outline; save an outline and derive it again")
    if radio_rotation_deg not in {0, 180}:
        raise ValueError("Choose upright or upside down for the StickCam calibration")
    if radio_rotation_deg == 180 and "radio_reference_rotation" not in data:
        data = {**data, "radio_reference_rotation": 180}
    poses, note = review_poses(data, gyro)
    result = {"poses": poses, "width": metadata["width"], "height": metadata["height"],
              "fill": fill, "max_zoom": max_zoom, "note": note}
    if source_signature is not None:
        result["source_signature"] = list(source_signature)
    # Validation preserves original pose format for requests passed through JSON;
    # the internal reason field is reconstructed by prepare_stabilization.
    checked = validate_stabilization(result)
    result.update(version=VERSION, valid_observations=checked["valid_observations"],
                  pose_corner_order="physical-upright")
    return result


def _pose_at(times, poses, at):
    index = bisect.bisect_left(times, at)
    if index < len(times) and abs(times[index] - at) < 1e-7:
        row = poses[index]
        return row[1], row[2], row[3]
    if index == 0 or index == len(times):
        return None, None, "tracking_gap"
    a, b = poses[index - 1], poses[index]
    if not a[1] or not b[1] or b[0] - a[0] > MAX_POSE_GAP:
        return None, None, a[3] or b[3] or "tracking_gap"
    weight = (at - a[0]) / (b[0] - a[0])
    points = [[p[axis] + weight * (q[axis] - p[axis]) for axis in (0, 1)]
              for p, q in zip(a[1], b[1], strict=True)]
    return points, "gyro-assisted" if "gyro-assisted" in (a[2], b[2]) else "outline", None


def _transform(points, center, angle):
    c, s = math.cos(angle), math.sin(angle)
    return [(center[0] + c * (x - center[0]) - s * (y - center[1]),
             center[1] + s * (x - center[0]) + c * (y - center[1])) for x, y in points]


def _frame_geometry(quad, width, height, fill, maximum, previous, fps):
    center = [sum(point[axis] for point in quad) / 4 for axis in (0, 1)]
    angle = math.atan2(quad[1][1] - quad[0][1], quad[1][0] - quad[0][0])
    if previous:
        blend = 1 - math.exp(-1 / (fps * .15))
        center = [previous["center"][i] + blend * (center[i] - previous["center"][i]) for i in (0, 1)]
        delta = math.atan2(math.sin(angle - previous["angle"]), math.cos(angle - previous["angle"]))
        angle = previous["angle"] + blend * delta
    # FFmpeg rotates clockwise in image coordinates around the pixel centre.
    rotation = -angle
    image_center = [(width - 1) / 2, (height - 1) / 2]
    rotated = _transform(quad, image_center, rotation)
    target = _transform([center], image_center, rotation)[0]
    extent = [max(abs(point[axis] - target[axis]) for point in rotated) for axis in (0, 1)]
    c, s = abs(math.cos(rotation)), abs(math.sin(rotation))
    source_room = [min(center[i] - _PIXEL_MARGIN, (width, height)[i] - 1 - _PIXEL_MARGIN - center[i])
                   for i in (0, 1)]
    rotated_room = [min(target[i] - _PIXEL_MARGIN, (width, height)[i] - 1 - _PIXEL_MARGIN - target[i])
                    for i in (0, 1)]
    if min(*source_room, *rotated_room) <= 0:
        return None, "unsafe_frame_bounds"
    # All four inverse-mapped crop corners must remain in the recorded image.
    minimum = max(1., (c * width + s * height) / (2 * source_room[0]),
                  (s * width + c * height) / (2 * source_room[1]),
                  width / (2 * rotated_room[0]), height / (2 * rotated_room[1])) * 1.002
    radio_limit = min(width / (2 * extent[0] + 8), height / (2 * extent[1] + 8), maximum)
    if minimum > radio_limit:
        return None, "cannot_fit_radio_without_missing_pixels"
    desired = min(width * fill / (2 * max(extent[0], 1)),
                  height * fill / (2 * max(extent[1], 1)), radio_limit)
    if previous and desired > previous["zoom"]:
        desired = previous["zoom"] + (1 - math.exp(-1 / (fps * .6))) * (desired - previous["zoom"])
    zoom = max(minimum, min(desired, radio_limit))
    crop_width, crop_height = [max(2, int(v / zoom) // 2 * 2) for v in (width, height)]
    x, y = [int(round(target[i] - ((crop_width, crop_height)[i] - 1) / 2)) for i in (0, 1)]
    crop = [(x, y), (x + crop_width - 1, y), (x + crop_width - 1, y + crop_height - 1),
            (x, y + crop_height - 1)]
    source_corners = _transform(crop, image_center, -rotation)
    if any(px < _PIXEL_MARGIN or py < _PIXEL_MARGIN or px > width - 1 - _PIXEL_MARGIN
           or py > height - 1 - _PIXEL_MARGIN for px, py in source_corners):
        return None, "unsafe_frame_bounds"
    if x < 0 or y < 0 or x + crop_width > width or y + crop_height > height:
        return None, "unsafe_frame_bounds"
    if any(px < x + 2 or py < y + 2 or px > x + crop_width - 3 or py > y + crop_height - 3
           for px, py in rotated):
        return None, "cannot_fit_radio_without_missing_pixels"
    return {"rotation": rotation, "width": crop_width, "height": crop_height,
            "x": x, "y": y, "center": center, "angle": angle, "zoom": zoom}, None


def _filter_path(path):
    # FFmpeg's filter parser consumes two levels of escaping, independently of
    # shell quoting. Arguments are always passed directly to subprocess.
    value = str(Path(path).resolve())
    value = "".join("\\" + c if c in "\\':" else c for c in value)
    return "".join("\\" + c if c in "\\'[],;" else c for c in value)


def prepare_stabilization(request, directory, start, frame_count, fps, *, metadata=None, cancelled=None):
    """Write a source-safe per-output-frame FFmpeg command file and summary.

    No interpolation is performed by FFmpeg between commands: every output
    frame has already had its exact transform qualified here. Only numeric
    transforms are retained; source video frames are streamed by FFmpeg.
    """
    config = validate_stabilization(request)
    if metadata and (config["width"], config["height"]) != (metadata["width"], metadata["height"]):
        raise ValueError("Radio stabilization dimensions do not match the current StickCam source")
    width, height = [v // 2 * 2 for v in (config["width"], config["height"])]
    sx, sy = width / config["width"], height / config["height"]
    poses = config["_qualified_poses"]
    times = [row[0] for row in poses]
    command_file = Path(directory) / "radio_stabilization.commands"
    counts, fallback_reasons, intervals = Counter(), Counter(), []
    previous = last_command = None
    active_reason, range_start = None, 0
    fallback = {"rotation": 0., "width": width, "height": height, "x": 0, "y": 0}
    with command_file.open("w", encoding="utf-8") as output:
        for frame in range(frame_count):
            if frame % 256 == 0 and cancelled and cancelled():
                raise RuntimeError("Cancelled")
            source_time = start + frame / fps
            quad, source, reason = _pose_at(times, poses, source_time)
            if quad is not None:
                quad = [(point[0] * sx, point[1] * sy) for point in quad]
                quad, reason = _quad(quad, width, height)
            current = None
            if quad is not None:
                current, reason = _frame_geometry(quad, width, height, config["fill"], config["max_zoom"], previous, fps)
            if current is None:
                current, previous = fallback, None
                reason = reason or "tracking_gap"
                counts["fallback"] += 1
                fallback_reasons[reason] += 1
            else:
                previous = current
                counts["stabilized"] += 1
                counts[source] += 1
                reason = "stabilized"
            if reason != active_reason:
                if active_reason is not None and active_reason != "stabilized":
                    intervals.append({"start": round(range_start / fps, 6), "end": round(frame / fps, 6),
                                      "source_start": round(start + range_start / fps, 6), "reason": active_reason})
                active_reason, range_start = reason, frame
            parameters = (round(current["rotation"], 10), current["width"], current["height"], current["x"], current["y"])
            if parameters != last_command:
                # Trigger after the previous frame and before the intended one;
                # this avoids microsecond timestamp rounding delaying a command.
                at = max(0., (frame - .25) / fps)
                angle, cw, ch, x, y = parameters
                output.write(f"{at:.9f} [enter] rotate@radio_rotate angle {angle:.10f}, "
                             f"[enter] crop@radio_crop w {cw}, [enter] crop@radio_crop h {ch}, "
                             f"[enter] crop@radio_crop x {x}, [enter] crop@radio_crop y {y}")
                if last_command is None or (cw, ch) != last_command[1:3]:
                    # Crop changes its output link dimensions in-place. Force
                    # downstream scale reconfiguration as well on FFmpeg
                    # versions whose cached scaler does not detect that change.
                    output.write(f", [enter] scale@radio_scale w {width}, [enter] scale@radio_scale h {height}")
                output.write(";\n")
                last_command = parameters
    if active_reason and active_reason != "stabilized":
        intervals.append({"start": round(range_start / fps, 6), "end": round(frame_count / fps, 6),
                          "source_start": round(start + range_start / fps, 6), "reason": active_reason})
    if not counts["stabilized"]:
        command_file.unlink(missing_ok=True)
        raise ValueError("No safely stabilized frames in the shared interval; use the original StickCam export or improve outline tracking")
    summary = {"version": VERSION, "status": "stabilized" if not counts["fallback"] else "partially_stabilized",
               "stabilized_frames": counts["stabilized"], "fallback_frames": counts["fallback"],
               "stabilized_percent": round(100 * counts["stabilized"] / frame_count, 2),
               "gyro_assisted_frames": counts["gyro-assisted"],
               "max_zoom": config["max_zoom"], "radio_fill": config["fill"],
               "output_width": width, "output_height": height,
               "fallback_reason_frames": dict(fallback_reasons), "fallback_intervals": intervals[:200],
               "fallback_interval_count": len(intervals), "source_note": config.get("note", ""),
               "limitations": "Uses saved visible radio observations, not new detection. Geometry is checked at each exported frame; interpolation cannot prove that the radio remained visible between observations. Center/rotation are smoothed and zoom is capped. Original full frames are retained when safe framing cannot contain the whole radio without missing source pixels. Radio identity/outline accuracy still require review; hidden or off-frame parts cannot be reconstructed. Perspective and rolling shutter are not corrected."}
    chain = (f"sendcmd=f={_filter_path(command_file)},"
             f"rotate@radio_rotate=angle=0:ow=iw:oh=ih:c=black:bilinear=1,"
             f"crop@radio_crop=w={width}:h={height}:x=0:y=0:exact=1,"
             f"scale@radio_scale={width}:{height}:flags=bicubic,setsar=1")
    return chain, summary
