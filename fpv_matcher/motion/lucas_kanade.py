from __future__ import annotations

import cv2
import numpy as np

from fpv_matcher.video.reader import sampled_frames


class LucasKanadeEstimator:
    algorithm_version = "lk-center-rates-forward-backward-960-v3"

    def __init__(self, max_corners: int = 350, replenish_below: int = 100, max_dimension: int = 960):
        self.max_corners, self.replenish_below, self.max_dimension = (
            max_corners,
            replenish_below,
            max_dimension,
        )

    def extract(self, path: str, sample_fps: float, progress=None) -> dict[str, np.ndarray]:
        from fpv_matcher.progress import ExtractionProgress
        from fpv_matcher.video.metadata import probe_video
        reporter = ExtractionProgress(probe_video(path)["duration"], callback=progress)
        rows = {key: [] for key in ("time", "dx", "dy", "rotation", "quality")}
        previous, points, previous_time = None, None, None
        for timestamp, gray in sampled_frames(path, sample_fps, self.max_dimension):
            reporter.update(timestamp)
            dx = dy = rotation = quality = 0.0
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
                ("time", timestamp),
                ("dx", dx),
                ("dy", dy),
                ("rotation", rotation),
                ("quality", quality),
            ):
                rows[key].append(value)
            previous, points, previous_time = gray, next_points, timestamp
        reporter.update(0, complete=True)
        result = {k: np.asarray(v, float) for k, v in rows.items()}
        result["magnitude"] = np.hypot(result["dx"], result["dy"])
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
