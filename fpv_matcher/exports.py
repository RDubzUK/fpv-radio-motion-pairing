"""Export a shared interval onto equal CFR frame counts for an edit timeline."""

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


def export_pair(
    info: dict,
    radio_path: Path,
    fpv_path: Path,
    directory: Path,
    fps: int = 30,
    profile: str = "h264",
    run=None,
    progress=None,
    radio_stabilization: dict | None = None,
    cancelled=None,
) -> dict:
    if fps not in {24, 25, 30, 50, 60}:
        raise ValueError("Export frame rate must be 24, 25, 30, 50 or 60")
    if profile not in {"h264", "dnxhr"}:
        raise ValueError("Export profile must be h264 or dnxhr")
    frame_count = math.floor(info["overlap_duration"] * fps + 1e-7)
    if frame_count < 1:
        raise ValueError("Shared interval is shorter than one export frame")
    duration = frame_count / fps
    directory.mkdir(parents=True, exist_ok=True)
    stabilization_chain = stabilization_summary = None
    if radio_stabilization is not None:
        from fpv_matcher.video.radio_stabilization import prepare_stabilization
        metadata = info.get("source_metadata", {}).get("radio")
        if metadata is None:
            from fpv_matcher.video.metadata import probe_video
            metadata = probe_video(radio_path)
        signature = radio_stabilization.get("source_signature")
        stat = Path(radio_path).stat()
        if signature is not None and list(signature) != [stat.st_size, stat.st_mtime_ns]:
            raise ValueError("StickCam footage changed since its stabilization data was saved")
        if progress:
            progress("Planning source-safe StickCam stabilization from saved outlines")
        stabilization_chain, stabilization_summary = prepare_stabilization(
            radio_stabilization, directory, info["radio_start"], frame_count, fps, metadata=metadata, cancelled=cancelled)
    outputs = []
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
        video_filters = f"fps={fps}:start_time=0:round=near,scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1,setpts=N/({fps}*TB)"
        if index == 0 and stabilization_chain:
            video_filters += "," + stabilization_chain
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
            "-filter_threads",
            "1",
            "-vf",
            video_filters,
            "-af",
            f"aresample=async=1:first_pts=0,apad,atrim=duration={duration:.9f},asetpts=N/SR/TB",
            "-frames:v",
            str(frame_count),
            "-t",
            f"{duration:.9f}",
        ]
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
        else:
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
        metadata = info.get("source_metadata", {}).get("radio" if index == 0 else "fpv", {})
        for key, flag in (
            ("color_primaries", "-color_primaries"),
            ("color_transfer", "-color_trc"),
            ("color_space", "-colorspace"),
            ("color_range", "-color_range"),
        ):
            value = metadata.get("color", {}).get(key)
            if value and value not in {"unknown", "unspecified"}:
                command += [flag, str(value)]
        command += [
            "-threads",
            "2",
            "-map_metadata",
            "-1",
            "-metadata",
            "timecode=00:00:00:00",
            "-movflags",
            "+faststart",
            str(destination),
        ]
        if progress:
            progress(f"Encoding {kind} · {index + 1}/2")
        if run:
            run(command, directory / f"{kind}.log")
        else:
            result = subprocess.run(command, capture_output=True, text=True, timeout=4 * 3600)
            if result.returncode:
                raise RuntimeError(result.stderr[-1500:])
        outputs.append(name)
    # FFmpeg may decode a damaged source without reporting a fatal error. Ensure
    # both media streams really contain the requested common frame count.
    for name in outputs:
        result = subprocess.run(
            [
                shutil.which("ffprobe") or "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_frames,duration,avg_frame_rate",
                "-of",
                "json",
                str(directory / name),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        stream = json.loads(result.stdout)["streams"][0]
        if (
            int(stream.get("nb_frames", 0)) != frame_count
            or abs(float(stream.get("duration", 0)) - duration) > 0.001
        ):
            raise RuntimeError(f"{name} did not encode the complete shared interval")
    manifest = {
        **info,
        "radio_export": outputs[0],
        "fpv_export": outputs[1],
        "export_fps": fps,
        "export_profile": profile,
        "export_frame_count": frame_count,
        "export_duration": duration,
        "timeline_note": "Place both clips at the same timeline position. They start at zero and have identical video frame counts.",
        "alignment_note": "Accuracy depends on the estimated or manually reviewed offset. CFR export does not make an uncertain estimate frame accurate.",
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


def stream_archive(directory: Path):
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
            pair_dirs = sorted(directory.glob("pair_*"))
            with zipfile.ZipFile(Writer(), "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
                for pair_dir in pair_dirs:
                    for path in sorted(pair_dir.iterdir()):
                        if path.suffix not in {".mp4", ".mov", ".json"}:
                            continue
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
