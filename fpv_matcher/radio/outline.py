"""Follow a user-marked radio body and estimate its camera-relative orientation."""

from __future__ import annotations

import cv2
import numpy as np


class RadioOutlineTracker:
    def __init__(self, outline, gimbals):
        self.outline = np.asarray(outline, np.float32).reshape(-1, 2)
        self.gimbals = gimbals
        self.transform = np.eye(3, dtype=np.float32)
        self.points = None
        self.quality = 0.0

    def _features(self, frame):
        mask = np.zeros(frame.shape, np.uint8)
        polygon = cv2.transform(self.outline[None, ...], self.transform[:2])[0].astype(np.int32)
        cv2.fillConvexPoly(mask, polygon, 255)
        for spec in self.gimbals:
            box = np.array(
                [
                    [
                        [spec["x"], spec["y"]],
                        [spec["x"] + spec["width"], spec["y"]],
                        [spec["x"] + spec["width"], spec["y"] + spec["height"]],
                        [spec["x"], spec["y"] + spec["height"]],
                    ]
                ],
                np.float32,
            )
            moved = cv2.transform(box, self.transform[:2])[0].astype(np.int32)
            cv2.fillConvexPoly(mask, moved, 0)
        return cv2.goodFeaturesToTrack(
            frame, maxCorners=500, qualityLevel=0.008, minDistance=6, mask=mask, blockSize=7
        )

    def initialize(self, frame):
        self.points = self._features(frame)
        self.quality = min(1.0, (0 if self.points is None else len(self.points)) / 60)
        return self.transform.copy(), self.quality

    def update(self, previous, current):
        if self.points is None or len(self.points) < 8:
            self.points = self._features(previous)
        if self.points is None or len(self.points) < 8:
            self.quality = 0.0
            return None, 0.0
        moved, status, error = cv2.calcOpticalFlowPyrLK(
            previous, current, self.points, None, winSize=(25, 25), maxLevel=3
        )
        if moved is None:
            self.points, self.quality = None, 0.0
            return None, 0.0
        backward, reverse_status, _ = cv2.calcOpticalFlowPyrLK(
            current, previous, moved, None, winSize=(25, 25), maxLevel=3
        )
        if backward is None or reverse_status is None:
            self.points, self.quality = None, 0.0
            return None, 0.0
        good = (
            status.ravel().astype(bool)
            & reverse_status.ravel().astype(bool)
            & (error.ravel() < 30)
            & (np.linalg.norm(backward.reshape(-1, 2) - self.points.reshape(-1, 2), axis=1) < 1.5)
        )
        source = self.points.reshape(-1, 2)[good]
        target = moved.reshape(-1, 2)[good]
        if len(source) >= 8:
            affine, inliers = cv2.estimateAffinePartial2D(
                source, target, method=cv2.RANSAC, ransacReprojThreshold=2.5
            )
            if affine is not None and inliers is not None and np.isfinite(affine).all():
                scale = float(np.hypot(affine[0, 0], affine[1, 0]))
                selected = inliers.ravel().astype(bool)
                if 0.75 < scale < 1.33 and selected.sum() >= 8 and selected.mean() >= 0.45:
                    self.transform = np.vstack([affine, [0, 0, 1]]).astype(np.float32) @ self.transform
                    self.points = target[selected].reshape(-1, 1, 2)
                    self.quality = min(1.0, len(source[selected]) / 40) * float(selected.mean())
                    if len(self.points) < 30:
                        added = self._features(current)
                        if added is not None:
                            self.points = np.concatenate([self.points, added])[:500]
                    return self.transform.copy(), self.quality
        self.points = self._features(current)
        self.quality = 0.0
        return None, 0.0

    def orientation(self):
        return float(np.degrees(np.arctan2(self.transform[1, 0], self.transform[0, 0])))

    def annotate(self, frame):
        polygon = cv2.transform(self.outline[None, ...], self.transform[:2])[0].astype(np.int32)
        color = (50, 210, 255) if self.quality >= 0.35 else (80, 115, 255)
        cv2.polylines(frame, [polygon], True, color, 3)
        angle = self.orientation()
        cv2.putText(
            frame,
            f"Radio {angle:+.1f} deg · {self.quality:.2f}",
            (max(8, polygon[0, 0]), max(24, polygon[0, 1] - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            color,
            2,
            cv2.LINE_AA,
        )
