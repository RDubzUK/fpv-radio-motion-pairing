"""Export shared source intervals, preserving cadence unless CFR is requested."""

from __future__ import annotations

import io
import json
import math
import queue
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path


def _probe_data(command, scratch, run=None):
    if run:
        try:
            run(command, scratch)
            return json.loads(scratch.read_text(encoding="utf-8"))
        finally:
            scratch.unlink(missing_ok=True)
    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=60)
    return json.loads(result.stdout)


def _output_info(path, duration, source_fps, frame_count=None, copy=False, run=None):
    """Inspect completed cuts before offering them as aligned downloads."""
    data = _probe_data([
        shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,nb_frames,duration,start_time,avg_frame_rate,r_frame_rate",
        "-of", "json", str(path),
    ], path.with_suffix(".timing"), run=run)
    stream = data["streams"][0]
    actual_duration = float(stream.get("duration", 0))
    start = float(stream.get("start_time", 0))
    tolerance = max(.002, 1 / source_fps)
    if not math.isfinite(actual_duration) or not math.isfinite(start) or actual_duration <= 0:
        raise RuntimeError(f"{path.name} has invalid exported timing")
    if frame_count is not None:
        if int(stream.get("nb_frames", 0)) != frame_count or abs(actual_duration - duration) > .001:
            raise RuntimeError(f"{path.name} did not encode the complete shared interval")
    elif start < -.002 or start > tolerance + .002 or abs(actual_duration - duration) > max(.05, 3 * tolerance):
        raise RuntimeError(f"{path.name} could not preserve the requested cut timing. Choose Accurate trim with original frame rates.")
    if copy:
        # Only inspect the opening packets, including any keyframe preroll.
        # Editors must honor the MP4 edit list to hide that decoding preroll.
        packets = _probe_data([
            shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-read_intervals", "%+#512", "-show_packets", "-show_entries", "packet=pts_time,flags",
            "-of", "json", str(path),
        ], path.with_suffix(".packets"), run=run)
        times = [float(p["pts_time"]) for p in packets.get("packets", [])
                 if p.get("pts_time") not in {None, "N/A"} and "D" not in p.get("flags", "")]
        visible = [t for t in times if math.isfinite(t) and t >= -.001]
        if not visible or min(visible) > tolerance + .002:
            raise RuntimeError(f"{path.name} has no frame at the requested start. Choose Accurate trim with original frame rates.")
        stream["first_presented_packet"] = min(visible)
    stream["duration"] = actual_duration
    stream["start_time"] = start
    stream["source_fps"] = source_fps
    return stream


def export_pair(
    info: dict,
    radio_path: Path,
    fpv_path: Path,
    directory: Path,
    fps: int | None = None,
    profile: str = "copy",
    run=None,
    progress=None,
    cancelled=None,
    radio_stabilization: dict | None = None,
) -> dict:
    if fps is not None and fps not in {24, 25, 30, 50, 60}:
        raise ValueError("Choose original frame rates or 24, 25, 30, 50 or 60fps")
    if profile not in {"copy", "h264", "dnxhr"}:
        raise ValueError("Export profile must be copy, h264 or dnxhr")
    if profile == "copy" and fps is not None:
        raise ValueError("Fast trim preserves each source's frame rate; select Original frame rates")
    frame_count = math.floor(info["overlap_duration"] * fps + 1e-7) if fps is not None else None
    duration = frame_count / fps if frame_count is not None else info["overlap_duration"]
    if duration <= 0 or frame_count == 0:
        raise ValueError("Shared interval is shorter than one export frame")
    directory.mkdir(parents=True, exist_ok=True)
    stabilization_chain = stabilization_summary = None
    if radio_stabilization is not None:
        if fps is None or profile == "copy":
            raise ValueError("Stabilization requires accurate encoding and an explicit export frame rate")
        from fpv_matcher.video.radio_stabilization import prepare_stabilization
        metadata = info.get("source_metadata", {}).get("radio")
        if metadata is None:
            from fpv_matcher.video.metadata import probe_video
            metadata = probe_video(radio_path)
        signature = radio_stabilization.get("source_signature")
        stat = Path(radio_path).stat()
        if signature is not None and list(signature) != [stat.st_size, stat.st_mtime_ns]:
            raise ValueError("StickCam footage changed since stabilization was prepared")
        stabilization_chain, stabilization_summary = prepare_stabilization(
            radio_stabilization, directory, info["radio_start"], frame_count, fps, metadata=metadata, cancelled=cancelled)
    outputs = []
    output_timing = {}
    for index, (kind, source, start) in enumerate(
        (("StickCam", radio_path, info["radio_start"]), ("FPV", fpv_path, info["fpv_start"]))
    ):
        stem = (
            source.stem
            if source.stem.casefold().startswith(kind.casefold() + "_")
            else f"{kind}_{source.stem}"
        )
        suffix = "_stabilized_aligned" if index == 0 and stabilization_chain else "_aligned"
        name = f"{stem}{suffix}.{'mov' if profile == 'dnxhr' else 'mp4'}"
        destination = directory / name
        metadata = info.get("source_metadata", {}).get("radio" if index == 0 else "fpv", {})
        source_fps = float(metadata.get("fps", 0))
        if not math.isfinite(source_fps) or source_fps <= 0:
            from .video.metadata import probe_video
            metadata = probe_video(source)
            source_fps = float(metadata["fps"])
        if not math.isfinite(source_fps) or source_fps <= 0 or duration < 1 / source_fps:
            raise ValueError(f"Cannot determine a usable frame rate/interval for {source.name}")
        command = [
            shutil.which("ffmpeg") or "ffmpeg",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-threads",
            "2",
            "-ss",
            f"{start:.9f}",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-t",
            f"{duration:.9f}",
        ]
        if profile == "copy":
            # Do not shift each file independently to its previous keyframe.
            # Keep preroll for decoding, hidden by the MP4 edit list at zero.
            command += ["-c", "copy", "-map_metadata", "0", "-map_chapters", "-1",
                        "-avoid_negative_ts", "disabled", "-use_editlist", "1"]
        else:
            filters = "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1"
            if fps is not None:
                filters = f"fps={fps}:start_time=0:round=near," + filters + f",setpts=N/({fps}*TB)"
                command += ["-frames:v", str(frame_count)]
            else:
                # No fps filter, -r, or frame-number clock: retain source PTS
                # cadence, including fractional rates and variable intervals.
                command += ["-vsync", "0", "-enc_time_base:v", "-1"]
            if index == 0 and stabilization_chain:
                filters += "," + stabilization_chain
            command += ["-filter_threads", "1", "-vf", filters, "-af",
                        f"aresample=async=1:first_pts=0,apad,atrim=duration={duration:.9f},asetpts=N/SR/TB",
                        "-map_metadata", "-1"]
        if profile == "dnxhr":
            command += [
                "-c:v",
                "dnxhd",
                "-profile:v",
                "dnxhr_hqx",
                "-pix_fmt",
                "yuv422p10le",
                "-c:a",
                "pcm_s24le",
                "-ar",
                "48000",
            ]
        elif profile == "h264":
            command += [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
            ]
        for key, flag in (
            ("color_primaries", "-color_primaries"),
            ("color_transfer", "-color_trc"),
            ("color_space", "-colorspace"),
            ("color_range", "-color_range"),
        ):
            value = metadata.get("color", {}).get(key)
            if profile != "copy" and value and value not in {"unknown", "unspecified"}:
                command += [flag, str(value)]
        command += [
            "-threads",
            "2",
            "-metadata",
            "timecode=00:00:00:00",
            "-write_tmcd",
            "0",
            "-movflags",
            "+faststart",
            str(destination),
        ]
        if progress:
            progress(f"{'Copying' if profile == 'copy' else 'Encoding'} {kind} · {index + 1}/2 · {source_fps:g}fps source")
        try:
            if run:
                run(command, directory / f"{kind}.log")
            else:
                result = subprocess.run(command, capture_output=True, text=True, timeout=4 * 3600)
                if result.returncode:
                    raise RuntimeError(result.stderr[-1500:])
        except RuntimeError as error:
            if profile == "copy" and str(error) != "Cancelled":
                raise RuntimeError(f"Fast trim could not copy {source.name}. Choose Accurate trim with original frame rates. {error}") from error
            raise
        if progress:
            progress(f"Checking {kind} cut timing · {index + 1}/2")
        if profile == "copy":
            from .mp4_edits import shorten
            try:
                shorten(destination, duration)
            except (OSError, ValueError, StopIteration) as exc:
                raise RuntimeError(f"Cannot bound the MP4 presentation interval. Choose Accurate trim. {exc}") from exc
        output_timing[kind] = _output_info(destination, duration, source_fps,
                                          frame_count=frame_count, copy=profile == "copy", run=run)
        try:
            (directory / f"{kind}.log").unlink(missing_ok=True)
        except OSError:
            pass  # An open log handle must not invalidate a completed clip.
        outputs.append(name)
    manifest = {
        **info,
        "radio_export": outputs[0],
        "fpv_export": outputs[1],
        "export_fps": fps if fps is not None else "original",
        "export_profile": profile,
        "export_frame_count": frame_count,
        "export_duration": duration,
        "output_timing": output_timing,
        "timeline_note": "Place both clips at the same timeline position. Fast trim requires an editor honoring MP4 edit lists."
                         if profile == "copy" else "Place both clips at the same timeline position.",
        "alignment_note": "Timing is checked against the requested shared interval within native-frame rounding. "
                          "Alignment accuracy still depends on the reviewed sync offset. Original-rate exports can have different frame counts.",
    }
    if stabilization_summary:
        manifest["radio_stabilization"] = stabilization_summary
    (directory / "alignment.json").write_text(
        json.dumps(manifest, indent=2, allow_nan=False), encoding="utf-8"
    )
    return manifest


def archive_pairs(directory: Path, manifests: list[tuple[Path, dict]]) -> Path:
    archive = directory / "aligned_pairs.zip"
    # Already-compressed MP4s are stored without expensive recompression.
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED, allowZip64=True) as stream:
        for pair_dir, manifest in manifests:
            prefix = "" if len(manifests) == 1 else pair_dir.name + "/"
            for name in (manifest["radio_export"], manifest["fpv_export"], "alignment.json"):
                stream.write(pair_dir / name, prefix + name)
    return archive


def stream_archive(directory: Path, pair_directories=None):
    """ZIP a completed export directly to the client with bounded buffering.

    Multi-gigabyte exports require one copy of each generated MP4 on disk.
    Disconnecting the client stops the producer without changing those files.
    """
    chunks = queue.Queue(maxsize=4)
    stopped = threading.Event()

    def enqueue(data):
        while not stopped.is_set():
            try:
                chunks.put(data, timeout=0.5)
                return True
            except queue.Full:
                continue
        return False

    class Writer(io.RawIOBase):
        position = 0

        def writable(self):
            return True

        def tell(self):
            return self.position

        def write(self, data):
            if not enqueue(bytes(data)):
                raise BrokenPipeError("Download disconnected")
            self.position += len(data)
            return len(data)

    def produce():
        try:
            if pair_directories is None:
                pair_dirs = sorted(directory.glob("pair_*"))
            else:
                pair_dirs = []
                for relative in pair_directories:
                    if relative != "." and (Path(relative).name != relative or relative in {"", ".."}):
                        raise ValueError("Invalid exported pair folder")
                    pair_dirs.append(directory / relative)
            with zipfile.ZipFile(Writer(), "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
                if (directory / "flights.json").is_file():
                    archive.write(directory / "flights.json", "flights.json")
                if (directory / "flights.json").is_file():
                    archive.write(directory / "flights.json", "flights.json")
                for pair_dir in pair_dirs:
                    manifest = json.loads((pair_dir / "alignment.json").read_text(encoding="utf-8"))
                    for filename in (manifest["radio_export"], manifest["fpv_export"], "alignment.json"):
                        if Path(filename).name != filename or filename in {"", ".", ".."}:
                            raise ValueError("Invalid exported clip filename")
                        path = pair_dir / filename
                        name = path.name if len(pair_dirs) == 1 else str(path.relative_to(directory))
                        info = zipfile.ZipInfo.from_file(path, arcname=name)
                        with archive.open(info, "w", force_zip64=True) as target, path.open("rb") as source:
                            for chunk in iter(lambda: source.read(1024**2), b""):
                                target.write(chunk)
        except Exception as exc:
            enqueue(exc)
        finally:
            enqueue(None)

    producer = threading.Thread(target=produce, name="zip-download", daemon=True)
    producer.start()
    try:
        while True:
            chunk = chunks.get()
            if chunk is None:
                break
            if isinstance(chunk, Exception):
                raise chunk
            if chunk:
                yield chunk
    finally:
        stopped.set()
