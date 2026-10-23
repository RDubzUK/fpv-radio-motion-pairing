"""Track a confirmed radio face, preserving explicit gaps when it is not visible.

Flow is fitted against immutable reference coordinates rather than composed
indefinitely. Sparse appearance matches reacquire the same face; neither a
catalogue silhouette nor agreement between background features is an anchor.
"""
from __future__ import annotations

import cv2
import numpy as np
import time


class RadioOutlineTracker:
    def __init__(self, outline, gimbals, sample_fps=30, backend=None):
        from fpv_matcher.video.acceleration import TrackingBackend
        self.backend = backend or TrackingBackend()
        self.outline = np.asarray(outline, np.float32).reshape(-1, 2)
        self.gimbals = gimbals
        self.transform = np.eye(3, dtype=np.float32)
        self.points = self.reference_points = None
        self.quality = 0.0
        self.reference = self.mask = None
        self.keys = self.descriptors = None
        self.detector = cv2.SIFT_create(nfeatures=1200, contrastThreshold=0.025)
        self.anchor_interval = max(1, round(float(sample_fps) * 0.75))
        self.retry_interval = max(1, round(float(sample_fps) * 0.3))
        self.global_interval = max(1, round(float(sample_fps) * 2.0))
        self.index = 0
        self.reason = "not initialized"

    def _reference_mask(self, frame):
        mask = np.zeros(frame.shape[:2], np.uint8)
        cv2.fillConvexPoly(mask, self.outline.astype(np.int32), 255)
        mask = cv2.erode(mask, np.ones((5, 5), np.uint8))
        for spec in self.gimbals:
            # Moving sticks/fingers inside the housing cannot anchor its pose.
            x, y, width, height = [int(round(spec[k])) for k in ("x", "y", "width", "height")]
            mask[max(0, y):y + height, max(0, x):x + width] = 0
        return mask

    def _geometry(self, transform, frame):
        if transform is None or not np.isfinite(transform).all() or abs(float(transform[2, 2])) < 1e-8:
            return False
        moved = cv2.perspectiveTransform(self.outline[None], transform)[0]
        height, width = frame.shape[:2]
        if (
            not np.isfinite(moved).all() or not cv2.isContourConvex(moved)
            or cv2.contourArea(self.outline, oriented=True) * cv2.contourArea(moved, oriented=True) <= 0
            or (moved < 0).any() or (moved[:, 0] >= width).any() or (moved[:, 1] >= height).any()
        ):
            return False
        area = abs(cv2.contourArea(moved))
        ratio = area / max(1, abs(cv2.contourArea(self.outline)))
        edges = np.linalg.norm(np.roll(moved, -1, axis=0) - moved, axis=1)
        edges /= np.maximum(1, np.linalg.norm(np.roll(self.outline, -1, axis=0) - self.outline, axis=1))
        return bool(.01 * width * height < area < .85 * width * height
                    and .18 < ratio < 5 and min(edges) > .15 and max(edges) / min(edges) < 3)

    def _coverage(self, source):
        if len(source) < 8:
            return 0.0
        span = np.ptp(source, axis=0) / np.maximum(1, np.ptp(self.outline, axis=0))
        area = cv2.contourArea(cv2.convexHull(np.float32(source))) / max(1, abs(cv2.contourArea(self.outline)))
        if min(span) < .25 or max(span) < .4 or area < .06:
            return 0.0
        return min(1.0, area / .18)

    def _appearance(self, frame, transform):
        """Check distributed shell patches against the original marked face."""
        local_to_frame = transform @ self.local_to_reference
        crop = self.backend.warp(frame, local_to_frame, self.reference.shape[::-1],
                                   flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderValue=127)
        crop = cv2.GaussianBlur(crop, (3, 3), 0)
        scores, sectors = [], []
        height, width = crop.shape
        for row in range(3):
            for column in range(4):
                y1, y2 = row * height // 3, (row + 1) * height // 3
                x1, x2 = column * width // 4, (column + 1) * width // 4
                mask = self.appearance_mask[y1:y2, x1:x2] > 0
                if mask.sum() < 30:
                    continue
                reference = self.reference[y1:y2, x1:x2][mask].astype(float)
                current = crop[y1:y2, x1:x2][mask].astype(float)
                if min(float(reference.std()), float(current.std())) < 6:
                    continue
                reference -= reference.mean()
                current -= current.mean()
                correlation = float(np.dot(reference, current) /
                                    max(1, np.linalg.norm(reference) * np.linalg.norm(current)))
                scores.append(correlation)
                if correlation >= .5:
                    sectors.append((row, column))
        # Hands can hide part of the face, but one logo/patch cannot validate it.
        if (len(scores) < 3 or len(sectors) < 3 or len(sectors) / len(scores) < .35
                or len({row for row, _ in sectors}) < 2 or len({col for _, col in sectors}) < 2):
            return 0.0
        return float(np.clip(np.mean([score for score in scores if score >= .5]), 0, 1))

    def initialize(self, frame):
        self.mask = self._reference_mask(frame)
        low = np.maximum(0, np.floor(self.outline.min(axis=0))).astype(int)
        high = np.minimum(frame.shape[::-1], np.ceil(self.outline.max(axis=0)) + 1).astype(int)
        crop = frame[low[1]:high[1], low[0]:high[0]]
        if not crop.size:
            return self._lost("radio outside frame")
        resize = min(1, 320 / max(crop.shape))
        size = (max(2, round(crop.shape[1] * resize)), max(2, round(crop.shape[0] * resize)))
        self.reference = cv2.GaussianBlur(cv2.resize(crop, size, interpolation=cv2.INTER_AREA), (3, 3), 0)
        self.appearance_mask = cv2.resize(self.mask[low[1]:high[1], low[0]:high[0]], size,
                                          interpolation=cv2.INTER_NEAREST)
        self.local_to_reference = np.float64([[crop.shape[1] / size[0], 0, low[0]],
                                              [0, crop.shape[0] / size[1], low[1]], [0, 0, 1]])
        self.keys, self.descriptors = self.detector.detectAndCompute(frame, self.mask)
        points = cv2.goodFeaturesToTrack(frame, maxCorners=300, qualityLevel=.01,
                                        minDistance=6, mask=self.mask, blockSize=7)
        if points is None or not self._geometry(self.transform, frame):
            return self._lost("insufficient visible shell landmarks")
        self.points = points
        self.reference_points = points.reshape(-1, 2).copy()
        coverage = self._coverage(self.reference_points)
        appearance = self._appearance(frame, self.transform)
        self.quality = min(1, len(points) / 40) * coverage * appearance
        if self.quality < .35:
            return self._lost("insufficient distributed shell appearance")
        self.reason = "reference face"
        return self.transform.copy(), self.quality

    def _lost(self, reason):
        self.points = self.reference_points = None
        self.quality, self.reason = 0.0, reason
        return None, 0.0

    def _accept(self, transform, source, target, inliers, frame, method):
        started = time.perf_counter()
        try:
            return self._accept_candidate(transform, source, target, inliers, frame, method)
        finally:
            self.backend.record("tracking_outline_validation_seconds", time.perf_counter() - started)

    def _accept_candidate(self, transform, source, target, inliers, frame, method):
        if transform is None or inliers is None or not self._geometry(transform, frame):
            return None
        selected = inliers.ravel().astype(bool)
        if selected.sum() < 8 or selected.mean() < .5:
            return None
        coverage = self._coverage(source[selected])
        projected = cv2.perspectiveTransform(source[selected, None].astype(np.float32), transform)[:, 0]
        error = np.linalg.norm(projected - target[selected], axis=1)
        if coverage == 0 or np.percentile(error, 90) > 2.5:
            return None
        appearance = self._appearance(frame, transform)
        quality = min(1, selected.sum() / 35) * float(selected.mean()) * coverage * appearance
        if quality < .35:
            return None
        self.transform = (transform / transform[2, 2]).astype(np.float32)
        self.reference_points = source[selected].astype(np.float32)
        self.points = target[selected].astype(np.float32).reshape(-1, 1, 2)
        self.quality, self.reason = float(quality), method
        return self.transform.copy(), self.quality

    def _search_region(self, frame, full_frame):
        height, width = frame.shape[:2]
        if full_frame:
            return frame, 0, 0, True
        try:
            moved = cv2.perspectiveTransform(self.outline[None], self.transform)[0]
            if not np.isfinite(moved).all():
                raise ValueError("invalid projected radio outline")
            low, high = moved.min(axis=0), moved.max(axis=0)
            span = high - low
            # Search a generous neighborhood around the last accepted pose.
            # Large/uncertain crops automatically fall back to a full-frame search.
            pad = np.maximum(48, span * 1.0)
            x1, y1 = np.floor(low - pad).astype(int)
            x2, y2 = np.ceil(high + pad).astype(int)
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(width, x2), min(height, y2)
            if x2 - x1 < 64 or y2 - y1 < 64 or (x2 - x1) * (y2 - y1) >= width * height * .7:
                raise ValueError("radio search region is too large or too small")
            return frame[y1:y2, x1:x2], x1, y1, False
        except (ValueError, cv2.error, OverflowError):
            return frame, 0, 0, True

    def _reacquire(self, frame, full_frame=False):
        if self.descriptors is None or len(self.descriptors) < 8:
            return None
        started = time.perf_counter()
        search, offset_x, offset_y, global_search = self._search_region(frame, full_frame)
        kind = "global" if global_search else "roi"
        self.backend.record(f"tracking_outline_{kind}_searches")
        try:
            sift_started = time.perf_counter()
            try:
                keys, descriptors = self.detector.detectAndCompute(search, None)
            finally:
                self.backend.record("tracking_outline_sift_seconds", time.perf_counter() - sift_started)
            if descriptors is None or len(descriptors) < 8:
                return None
            match_started = time.perf_counter()
            try:
                matcher = cv2.BFMatcher()
                reverse = {pair[0].queryIdx: pair[0].trainIdx
                           for pair in matcher.knnMatch(descriptors, self.descriptors, k=2)
                           if len(pair) == 2 and pair[0].distance < .72 * pair[1].distance}
                matches = [pair[0] for pair in matcher.knnMatch(self.descriptors, descriptors, k=2)
                           if len(pair) == 2 and pair[0].distance < .72 * pair[1].distance
                           and reverse.get(pair[0].trainIdx) == pair[0].queryIdx]
                if len(matches) < 8:
                    return None
                source = np.float32([self.keys[match.queryIdx].pt for match in matches])
                target = np.float32([keys[match.trainIdx].pt for match in matches])
                target += np.array([offset_x, offset_y], np.float32)
                transform, inliers = cv2.findHomography(source, target, cv2.RANSAC, 2.5)
            finally:
                self.backend.record("tracking_outline_match_seconds", time.perf_counter() - match_started)
            accepted = self._accept(transform, source, target, inliers, frame, "reference reacquired")
            if accepted is not None:
                self.backend.record(f"tracking_outline_{kind}_successes")
            return accepted
        finally:
            self.backend.record("tracking_outline_reacquire_seconds", time.perf_counter() - started)

    def update(self, previous, current):
        self.index += 1
        # Sparse absolute anchors complement cheaper flow. Failure to find an
        # anchor never promotes arbitrary current-frame features to references.
        full_frame_due = self.index % self.global_interval == 0
        local_due = (self.index % self.retry_interval == 0 if self.points is None
                     else self.index % self.anchor_interval == 0)
        if full_frame_due or local_due:
            anchored = self._reacquire(current, full_frame=full_frame_due)
            if anchored is not None:
                return anchored
        if self.points is None or len(self.points) < 8:
            return self._lost("radio not reacquired")
        moved, status, error = self.backend.flow(previous, current, self.points, None,
                                                     winSize=(25, 25), maxLevel=3)
        if moved is None or status is None or error is None:
            return self._lost("shell flow lost")
        backward, reverse_status, _ = self.backend.flow(current, previous, moved, None,
                                                             winSize=(25, 25), maxLevel=3)
        if backward is None or reverse_status is None:
            return self._lost("shell reverse flow lost")
        good = (status.ravel().astype(bool) & reverse_status.ravel().astype(bool)
                & (error.ravel() < 25) & np.isfinite(moved.reshape(-1, 2)).all(axis=1)
                & (np.linalg.norm(backward.reshape(-1, 2) - self.points.reshape(-1, 2), axis=1) < 1.2))
        source, target = self.reference_points[good], moved.reshape(-1, 2)[good]
        if len(source) >= 8:
            transform, inliers = cv2.findHomography(source, target, cv2.RANSAC, 2.5)
            accepted = self._accept(transform, source, target, inliers, current, "reference flow")
            if accepted is not None:
                return accepted
        return self._lost("shell geometry or appearance failed")

    def orientation(self):
        polygon = cv2.perspectiveTransform(self.outline[None], self.transform)[0]
        vector = polygon[1] - polygon[0]
        original = self.outline[1] - self.outline[0]
        return float(np.degrees(np.arctan2(vector[1], vector[0]) - np.arctan2(original[1], original[0])))

    def annotate(self, frame):
        if self.quality < .35:
            cv2.putText(frame, "Radio lost - inputs unobserved", (8, 24),
                        cv2.FONT_HERSHEY_SIMPLEX, .65, (80, 115, 255), 2, cv2.LINE_AA)
            return
        polygon = cv2.perspectiveTransform(self.outline[None], self.transform)[0].astype(np.int32)
        cv2.polylines(frame, [polygon], True, (50, 210, 255), 3)
        cv2.putText(frame, f"Radio {self.orientation():+.1f} deg / {self.quality:.2f}",
                    (max(8, polygon[0, 0]), max(24, polygon[0, 1] - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, .65, (50, 210, 255), 2, cv2.LINE_AA)
