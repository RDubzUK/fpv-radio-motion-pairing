from __future__ import annotations

import cv2
import numpy as np

from fpv_matcher.video.reader import sampled_frames


class LucasKanadeEstimator:
    algorithm_version = "lk-rays-relative-camera-rotation-fov-v4"

    def __init__(self, max_corners: int = 350, replenish_below: int = 100, max_dimension: int = 960):
        self.max_corners, self.replenish_below, self.max_dimension = (
            max_corners,
            replenish_below,
            max_dimension,
        )

    def extract(self, path: str, sample_fps: float, progress=None, camera_profile=None) -> dict[str, np.ndarray]:
        from fpv_matcher.progress import ExtractionProgress
        from fpv_matcher.video.metadata import probe_video
        reporter = ExtractionProgress(probe_video(path)["duration"], callback=progress)
        from fpv_matcher.motion.visual_rotation import validate_profile, estimate_rotation
        camera_profile = validate_profile(camera_profile)
        rows = {key: [] for key in ("time", "dx", "dy", "rotation", "quality", "flow_quality", "x", "y", "z", "pose_method")}
        previous, points, previous_time = None, None, None
        for timestamp, gray in sampled_frames(path, sample_fps, self.max_dimension):
            reporter.update(timestamp)
            dx = dy = rotation = quality = 0.0
            angular_rate, angular_quality, pose_method = np.zeros(3), 0., 0
            next_points = None
            if previous is not None:
                if points is None or len(points) < self.replenish_below:
                    points = self._features(previous, points)
                if points is not None and len(points) >= 6:
                    tracked, status, error = cv2.calcOpticalFlowPyrLK(
                        previous, gray, points, None, winSize=(25, 25), maxLevel=3
                    )
                    if tracked is not None:
                        back, back_status, _ = cv2.calcOpticalFlowPyrLK(
                            gray, previous, tracked, None, winSize=(25, 25), maxLevel=3
                        )
                        good = status.ravel().astype(bool) & (error.ravel() < 35)
                        if back is not None:
                            good &= back_status.ravel().astype(bool) & (
                                np.linalg.norm(points - back, axis=2).ravel() < 1.5
                            )
                        else:
                            good[:] = False
                        source, target = points.reshape(-1, 2)[good], tracked.reshape(-1, 2)[good]
                        if len(source) >= 6:
                            angular_rate, angular_quality, pose_method = estimate_rotation(
                                source, target, (gray.shape[1], gray.shape[0]), timestamp - previous_time, camera_profile)
                            affine, inliers = cv2.estimateAffinePartial2D(
                                source, target, method=cv2.RANSAC, ransacReprojThreshold=2.5
                            )
                            if affine is not None and np.isfinite(affine).all():
                                center = np.array([gray.shape[1] / 2, gray.shape[0] / 2])
                                displacement = affine[:, :2] @ center + affine[:, 2] - center
                                delta = timestamp - previous_time
                                scale = float(np.hypot(affine[0, 0], affine[1, 0]))
                                if delta > 0 and 0.65 < scale < 1.5:
                                    # Relative image dimensions per second and degrees per second.
                                    # Rotation about the image centre must not masquerade as translation.
                                    dx = float(displacement[0] / gray.shape[1] / delta)
                                    dy = float(displacement[1] / gray.shape[0] / delta)
                                    rotation = float(
                                        np.degrees(np.arctan2(affine[1, 0], affine[0, 0])) / delta
                                    )
                                    mask = inliers.ravel().astype(bool)
                                    quality = min(1.0, int(mask.sum()) / 40) * float(mask.mean())
                                    next_points = target[mask].reshape(-1, 1, 2)
            for key, value in (
                ("time", timestamp if previous_time is None else (timestamp + previous_time) / 2),
                ("dx", dx),
                ("dy", dy),
                ("rotation", rotation),
                ("quality", angular_quality), ("flow_quality", quality),
                ("x", angular_rate[0]), ("y", angular_rate[1]), ("z", angular_rate[2]), ("pose_method", pose_method),
            ):
                rows[key].append(value)
            previous, points, previous_time = gray, next_points, timestamp
        reporter.update(0, complete=True)
        result = {k: np.asarray(v, float) for k, v in rows.items()}
        result["magnitude"] = np.hypot(result["dx"], result["dy"])
        for key in ("dx", "dy", "rotation", "magnitude"):
            result[key + "_quality"] = result["flow_quality"]
        result["horizontal_fov_deg"] = np.asarray(camera_profile["horizontal_fov_deg"])
        result["projection"] = np.asarray(camera_profile["projection"])
        result["kind"] = np.asarray("visual_rotation")
        result["label"] = np.asarray("Experimental visual angular rates · FOV lens approximation")
        result["note"] = np.asarray("Relative camera rotation in degrees/second from viewing-ray / essential-pose fits. Approximate lens FOV and projection require verification. Quality gaps are preserved. This is not embedded gyro, absolute attitude or gravity; 2D flow remains separate diagnostics.")
        return result

    def _features(self, gray, existing=None):
        mask = np.full(gray.shape, 255, np.uint8)
        if existing is not None:
            for x, y in existing.reshape(-1, 2):
                cv2.circle(mask, (int(x), int(y)), 8, 0, -1)
        count = 0 if existing is None else len(existing)
        added = cv2.goodFeaturesToTrack(
            gray,
            maxCorners=max(1, self.max_corners - count),
            qualityLevel=0.01,
            minDistance=8,
            blockSize=7,
            mask=mask,
        )
        if existing is None:
            return added
        return existing if added is None else np.concatenate([existing, added])[: self.max_corners]
