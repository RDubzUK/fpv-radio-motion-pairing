from __future__ import annotations

import cv2
import numpy as np

from fpv_matcher.video.reader import sampled_frames


class LucasKanadeEstimator:
    algorithm_version = "lk-native-distributed-rays-rotation-v5"

    def __init__(self, max_corners: int = 600, replenish_below: int = 220, max_dimension: int = 960):
        self.max_corners, self.replenish_below, self.max_dimension = (
            max_corners,
            replenish_below,
            max_dimension,
        )

    def extract(self, path: str, sample_fps: float, progress=None, camera_profile=None) -> dict[str, np.ndarray]:
        from fpv_matcher.progress import ExtractionProgress
        from fpv_matcher.video.metadata import probe_video
        from fpv_matcher.video.acceleration import TrackingBackend
        from fpv_matcher.motion.visual_rotation import validate_profile, estimate_rotation
        info = probe_video(path)
        camera_profile = validate_profile(camera_profile)
        source_fps = float(info.get("fps") or sample_fps)
        tracking_fps = min(120., max(sample_fps, source_fps)) if camera_profile["track_full_rate"] else sample_fps
        backend = TrackingBackend()
        callback = (lambda event: progress({**event, **backend.status(), "tracking_fps": tracking_fps})) if progress else None
        reporter = ExtractionProgress(info["duration"], callback=callback)
        keys = ("time", "dx", "dy", "rotation", "quality", "flow_quality", "x", "y", "z", "pose_method", "gap_reason")
        rows = {key: [] for key in keys}
        previous, points, previous_time = None, None, None
        bucket, bucket_index = [], 0
        def emit():
            if not bucket:
                return
            weights = np.asarray([row["delta"] for row in bucket])
            valid = np.asarray([row["quality"] > .1 for row in bucket])
            angular = np.zeros(3)
            quality = 0.
            method = 0
            reason = max(set(row["gap_reason"] for row in bucket), key=lambda v: sum(r["gap_reason"] == v for r in bucket))
            # Only complete observed intervals form a rotation estimate. Gap
            # interpolation is an independent, optional display operation.
            if valid.all():
                rotation = np.eye(3)
                for row in bucket:
                    increment = cv2.Rodrigues(-np.radians(row["angular"]) * row["delta"])[0]
                    rotation = increment @ rotation
                angular = -np.degrees(cv2.Rodrigues(rotation)[0].ravel()) / weights.sum()
                quality = float(np.average([r["quality"] for r in bucket], weights=weights))
                method = max(row["pose_method"] for row in bucket)
                reason = 0
            item = {"time": (bucket[0]["start"] + bucket[-1]["end"]) / 2,
                    "quality": quality, "x": angular[0], "y": angular[1], "z": angular[2],
                    "pose_method": method, "gap_reason": reason,
                    "flow_quality": float(np.average([r["flow_quality"] for r in bucket], weights=weights))}
            for key in ("dx", "dy", "rotation"):
                item[key] = float(np.average([r[key] for r in bucket], weights=weights))
            for key in keys:
                rows[key].append(item[key])
        for timestamp, gray in sampled_frames(path, tracking_fps, self.max_dimension):
            reporter.update(timestamp)
            if previous is None:
                previous, previous_time = gray, timestamp
                continue
            dx = dy = image_rotation = flow_quality = 0.
            angular_rate, angular_quality, pose_method, reason = np.zeros(3), 0., 0, 1
            if points is None or len(points) < self.replenish_below:
                points = self._features(previous, points)
            source, target = np.empty((0, 2)), np.empty((0, 2))
            if points is not None and len(points) >= 6:
                moved, status, error = backend.flow(previous, gray, points)
                if moved is not None and status is not None and error is not None:
                    back, back_status, _ = backend.flow(gray, previous, moved)
                    good = status.ravel().astype(bool) & (error.ravel() < 35)
                    if back is not None and back_status is not None:
                        good &= back_status.ravel().astype(bool) & (np.linalg.norm(points-back, axis=2).ravel() < 1.5)
                    else:
                        good[:] = False
                    source, target = points.reshape(-1, 2)[good], moved.reshape(-1, 2)[good]
            delta = timestamp - previous_time
            angular_rate, angular_quality, pose_method, reason = estimate_rotation(
                source, target, (gray.shape[1], gray.shape[0]), delta, camera_profile, diagnostics=True)
            if angular_quality <= .1:
                # Descriptor correspondences can reacquire large inter-frame
                # rotation when the local LK search loses its landmarks.
                matched_source, matched_target = self._reacquire(previous, gray)
                fallback = estimate_rotation(matched_source, matched_target, (gray.shape[1], gray.shape[0]),
                                             delta, camera_profile, diagnostics=True)
                if fallback[1] > angular_quality:
                    source, target = matched_source, matched_target
                    angular_rate, angular_quality, pose_method, reason = fallback
            if len(source) >= 6:
                affine, inliers = cv2.estimateAffinePartial2D(source, target, method=cv2.RANSAC, ransacReprojThreshold=2.5)
                if affine is not None and inliers is not None and np.isfinite(affine).all():
                    center = np.array([gray.shape[1]/2, gray.shape[0]/2])
                    displacement = affine[:, :2] @ center + affine[:, 2] - center
                    scale = float(np.hypot(affine[0, 0], affine[1, 0]))
                    if .65 < scale < 1.5 and delta > 0:
                        dx, dy = displacement / [gray.shape[1], gray.shape[0]] / delta
                        image_rotation = float(np.degrees(np.arctan2(affine[1, 0], affine[0, 0])) / delta)
                        mask = inliers.ravel().astype(bool)
                        flow_quality = min(1., int(mask.sum())/40) * float(mask.mean())
            index = max(0, int((timestamp - 1e-8) * sample_fps))
            if bucket and index != bucket_index:
                emit(); bucket.clear()
            bucket_index = index
            bucket.append({"start": previous_time, "end": timestamp, "delta": delta, "angular": angular_rate,
                           "quality": angular_quality, "pose_method": pose_method, "gap_reason": reason,
                           "dx": dx, "dy": dy, "rotation": image_rotation, "flow_quality": flow_quality})
            # Keep all validated correspondences, not just a planar affine
            # subset: wide-angle 3D pitch/yaw does not follow a flat transform.
            points = target.astype(np.float32).reshape(-1, 1, 2) if len(target) else None
            previous, previous_time = gray, timestamp
        emit()
        reporter.update(0, complete=True)
        result = {k: np.asarray(v, float) for k, v in rows.items()}
        result["magnitude"] = np.hypot(result["dx"], result["dy"])
        for key in ("dx", "dy", "rotation", "magnitude"):
            result[key + "_quality"] = result["flow_quality"]
        result.update(backend.metadata())
        result["tracking_fps"] = np.asarray(tracking_fps)
        result["horizontal_fov_deg"] = np.asarray(camera_profile["horizontal_fov_deg"])
        result["projection"] = np.asarray(camera_profile["projection"])
        result["kind"] = np.asarray("visual_rotation")
        result["label"] = np.asarray("Experimental visual angular rates · FOV lens approximation")
        result["note"] = np.asarray("Relative camera rotation in degrees/second from viewing-ray / essential-pose fits. Native-rate intermediate tracking preserves fast motion; output intervals with unobserved motion remain gaps. Lens geometry requires verification. Not embedded gyro, absolute attitude or gravity.")
        return result

    def _reacquire(self, previous, current):
        if not hasattr(self, "descriptor_detector"):
            self.descriptor_detector = cv2.ORB_create(nfeatures=1000, fastThreshold=12)
        ka, da = self.descriptor_detector.detectAndCompute(previous, None)
        kb, db = self.descriptor_detector.detectAndCompute(current, None)
        empty = (np.empty((0, 2)), np.empty((0, 2)))
        if da is None or db is None or min(len(da), len(db)) < 20:
            return empty
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        reverse = {pair[0].queryIdx: pair[0].trainIdx for pair in matcher.knnMatch(db, da, k=2)
                   if len(pair) == 2 and pair[0].distance < .75 * pair[1].distance}
        matches = [pair[0] for pair in matcher.knnMatch(da, db, k=2)
                   if len(pair) == 2 and pair[0].distance < .75 * pair[1].distance
                   and reverse.get(pair[0].trainIdx) == pair[0].queryIdx]
        return (np.float32([ka[m.queryIdx].pt for m in matches]).reshape(-1, 2),
                np.float32([kb[m.trainIdx].pt for m in matches]).reshape(-1, 2)) if len(matches) >= 20 else empty

    def _features(self, gray, existing=None):
        mask = np.full(gray.shape, 255, np.uint8)
        if existing is not None:
            for x, y in existing.reshape(-1, 2):
                cv2.circle(mask, (int(x), int(y)), 8, 0, -1)
        added_parts = []
        height, width = gray.shape
        quota = max(1, self.max_corners // 12)
        for row in range(3):
            for column in range(4):
                y1, y2 = row*height//3, (row+1)*height//3
                x1, x2 = column*width//4, (column+1)*width//4
                occupied = 0 if existing is None else int(((existing[:, 0, 0] >= x1) & (existing[:, 0, 0] < x2)
                    & (existing[:, 0, 1] >= y1) & (existing[:, 0, 1] < y2)).sum())
                if occupied >= quota:
                    continue
                found = cv2.goodFeaturesToTrack(gray[y1:y2, x1:x2], maxCorners=quota-occupied,
                    qualityLevel=.01, minDistance=8, blockSize=7, mask=mask[y1:y2, x1:x2])
                if found is not None:
                    added_parts.append(found + np.float32([x1, y1]))
        added = np.concatenate(added_parts) if added_parts else None
        if existing is None:
            return added
        return existing if added is None else np.concatenate([existing, added])[: self.max_corners]
