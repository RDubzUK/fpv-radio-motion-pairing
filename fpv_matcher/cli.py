from __future__ import annotations

import argparse
from pathlib import Path

from fpv_matcher.pipeline import run_batch
from fpv_matcher.radio.stick_tracker import load_config
from fpv_matcher.radio.presets import load_presets
from fpv_matcher.video.reader import video_files


def main():
    parser = argparse.ArgumentParser(
        description="Pair FPV and StickCam recordings using motion, boundary audio and camera-clock consensus."
    )
    parser.add_argument("--fpv", required=True, help="FPV file or directory")
    parser.add_argument("--radio", required=True, help="StickCam file or directory")
    parser.add_argument("--config", required=True, help="Global or per-clip calibration JSON")
    parser.add_argument("--output", default="results")
    parser.add_argument("--cache", default=str(Path.home() / ".fpv-radio-motion-pairing" / "cache"))
    parser.add_argument("--sample-fps", type=float, default=30)
    parser.add_argument("--stick-sampling-ratio", type=float, choices=(0.5, 0.25),
                        help="Track StickCam at half or quarter of each clip's detected frame rate")
    parser.add_argument("--match-fps", type=float, default=30)
    parser.add_argument("--min-overlap", type=float, default=2)
    parser.add_argument(
        "--minimum-score",
        type=float,
        default=0.55,
        help="Minimum correlation evidence for a proposal, not a probability",
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--no-plots", action="store_true")
    parser.add_argument("--decode-mode", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--motion-source", choices=("auto", "gyro", "visual"), default="auto")
    parser.add_argument("--camera-uptilt-deg", type=float, default=0)
    parser.add_argument("--imu-orientation", default="zXy")
    parser.add_argument("--gyro-video-offset", type=float, default=0)
    parser.add_argument("--rate-preset", help="Preset name from the project's rate_presets.yaml; defaults to its default preset")
    parser.add_argument("--no-rates", action="store_true", help="Ignore saved rates")
    args = parser.parse_args()
    try:
        presets = load_presets()
        name = args.rate_preset or presets["default"]
        if args.rate_preset and name not in presets["presets"]:
            raise ValueError("That rate preset is not in rate_presets.yaml")
        profile = None if args.no_rates else presets["presets"].get(name)
        summary = run_batch(
            video_files(args.fpv),
            video_files(args.radio),
            load_config(args.config),
            Path(args.output),
            Path(args.cache),
            args.sample_fps,
            args.match_fps,
            args.min_overlap,
            args.workers,
            args.minimum_score,
            not args.no_audio,
            progress=lambda event: print(event["message"], flush=True),
            make_plots=not args.no_plots,
            rate_profile=profile, motion_source=args.motion_source, camera_uptilt_deg=args.camera_uptilt_deg,
            imu_orientation=args.imu_orientation, gyro_video_offset=args.gyro_video_offset, decode_mode=args.decode_mode,
            stick_sampling_ratio=args.stick_sampling_ratio,
        )
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Analysis failed: {exc}\n")
    for row in summary["rows"]:
        if row["global_assignment"]:
            print(
                f"{row['radio_video']} → {row['fpv_video']} · score {row['combined_score']:.3f} · {row['confidence']} · offset {row['offset_seconds']:+.3f}s"
            )
    print(f"Report: {Path(args.output) / 'matches.csv'}")


if __name__ == "__main__":
    main()
