from __future__ import annotations

import copy
import json
from pathlib import Path

import cv2
import numpy as np

from fpv_matcher.config import validate_calibration
from fpv_matcher.radio.outline import RadioOutlineTracker
from fpv_matcher.video.metadata import probe_video
from fpv_matcher.video.reader import sampled_frames, reverse_sampled_frames

ALGORITHM_VERSION = "stick-reference-perspective-visible-tip-v7"
MAX_DIMENSION = 1280


def load_config(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def scaled_config(config: dict, scale: float) -> dict:
    result = copy.deepcopy(config)
    if "radio_outline" in result:
        result["radio_outline"] = [
            [coordinate * scale for coordinate in point] for point in result["radio_outline"]
        ]
    for side in ("left_gimbal", "right_gimbal"):
        for key in (
            "x",
            "y",
            "width",
            "height",
            "center_x",
            "center_y",
            "travel_x",
            "travel_y",
            "tip_x",
            "tip_y",
            "tip_radius",
        ):
            if key in result[side]:
                result[side][key] *= scale
    return result


class GimbalTracker:
    """Track housing separately from a user-selected stick tip.

    Tracking failure has zero weight. A housing template can reacquire moderate
    movement; perspective changes and occlusion still need visual review.
    """

    def __init__(self, spec: dict, wonky: bool):
        self.spec, self.wonky = spec, wonky
        self.x, self.y, self.width, self.height = (int(round(spec[k])) for k in ("x", "y", "width", "height"))
        self.transform = np.eye(3, dtype=np.float32)
        self.points, self.template, self.reference, self.last_tip = None, None, None, None
        self.last_quality = 0.0
        self.locked = True
        self.lost_frames = 0
        self.tip_lost_frames = 0
        self.reason = "not initialized"

    def _crop(self, frame):
        # Only warp the gimbal crop, never a full 4K frame for each stick.
        crop_transform = self.transform @ np.array([[1, 0, self.x], [0, 1, self.y], [0, 0, 1]], np.float32)
        return cv2.warpPerspective(
            frame,
            crop_transform,
            (self.width, self.height),
            flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
            borderValue=127,
        )

    def _features(self, frame):
        mask = np.zeros(frame.shape, np.uint8)
        corners = np.array(
            [
                [
                    [self.x, self.y],
                    [self.x + self.width, self.y],
                    [self.x + self.width, self.y + self.height],
                    [self.x, self.y + self.height],
                ]
            ],
            np.float32,
        )
        moved = cv2.perspectiveTransform(corners, self.transform)[0].astype(np.int32)
        cv2.fillConvexPoly(mask, moved, 255)
        center = cv2.perspectiveTransform(
            np.array([[[self.x + self.width / 2, self.y + self.height / 2]]], np.float32), self.transform
        )[0, 0]
        scale = float(np.hypot(self.transform[0, 0], self.transform[1, 0]))
        cv2.ellipse(
            mask,
            tuple(center.astype(int)),
            (max(3, int(self.width * 0.34 * scale)), max(3, int(self.height * 0.34 * scale))),
            float(np.degrees(np.arctan2(self.transform[1, 0], self.transform[0, 0]))),
            0,
            360,
            0,
            -1,
        )
        return cv2.goodFeaturesToTrack(
            frame, maxCorners=100, qualityLevel=0.01, minDistance=5, mask=mask, blockSize=5
        )

    def initialize(self, frame, body_transform=None, body_quality=1.0):
        self.reference = self._crop(frame)
        self.points = self._features(frame) if self.wonky else None
        if "tip_x" in self.spec:
            self.last_tip = np.array([self.spec["tip_x"], self.spec["tip_y"]], float)
            radius = max(3, round(self.spec["tip_radius"]))
            tx, ty = self.last_tip.astype(int)
            if radius <= tx < self.width - radius and radius <= ty < self.height - radius:
                self.template = self.reference[
                    ty - radius : ty + radius + 1, tx - radius : tx + radius + 1
                ].copy()
        if body_transform is not None:
            self.transform = body_transform.copy()
        return self.detect(frame, body_quality if body_transform is not None else 1.0)

    def _visible(self, frame):
        corners = np.float32([[[self.x, self.y], [self.x + self.width - 1, self.y],
                              [self.x + self.width - 1, self.y + self.height - 1],
                              [self.x, self.y + self.height - 1]]])
        moved = cv2.perspectiveTransform(corners, self.transform)[0]
        return bool(np.isfinite(moved).all() and cv2.isContourConvex(moved)
                    and (moved >= 0).all() and (moved[:, 0] < frame.shape[1]).all()
                    and (moved[:, 1] < frame.shape[0]).all())

    def _housing_agreement(self, frame):
        if self.reference is None or not self._visible(frame):
            return 0.0
        crop = self._crop(frame)
        yy, xx = np.indices(crop.shape)
        shell = (((xx - self.width / 2) / max(1, self.width * .36)) ** 2
                 + ((yy - self.height / 2) / max(1, self.height * .36)) ** 2) > 1
        reference, current = self.reference[shell].astype(float), crop[shell].astype(float)
        if len(reference) < 50 or min(reference.std(), current.std()) < 6:
            return 0.0
        reference -= reference.mean()
        current -= current.mean()
        return float(np.dot(reference, current) / max(1, np.linalg.norm(reference) * np.linalg.norm(current)))

    def gap(self, reason):
        self.last_quality = 0.0
        self.tip_lost_frames += 1
        self.reason = reason
        return 0.0, 0.0, 0.0

    def _reacquire(self, frame):
        # Match the housing, with the mobile middle of each gimbal masked out.
        if self.reference is None or frame.shape[0] < self.height or frame.shape[1] < self.width:
            return False
        mask = np.ones(self.reference.shape, np.uint8) * 255
        cv2.ellipse(
            mask,
            (self.width // 2, self.height // 2),
            (int(self.width * 0.35), int(self.height * 0.35)),
            0,
            0,
            360,
            0,
            -1,
        )
        response = cv2.matchTemplate(frame, self.reference, cv2.TM_CCOEFF_NORMED, mask=mask)
        response = np.nan_to_num(response, nan=-1, posinf=-1, neginf=-1)
        _, score, _, location = cv2.minMaxLoc(response)
        if score < 0.8:
            return False
        alternative = response.copy()
        alternative[
            max(0, location[1] - self.height // 2) : location[1] + self.height // 2 + 1,
            max(0, location[0] - self.width // 2) : location[0] + self.width // 2 + 1,
        ] = -1
        if score - float(np.max(alternative)) < 0.04:
            return False
        proposed = np.array(
            [[1, 0, location[0] - self.x], [0, 1, location[1] - self.y], [0, 0, 1]], np.float32
        )
        previous = self.transform
        self.transform = proposed
        if self._housing_agreement(frame) < .6:
            self.transform = previous
            return False
        self.points = self._features(frame)
        self.locked = True
        return True

    def update(self, previous, current, body_transform=None, body_quality=1.0):
        if body_transform is not None and body_quality >= .35:
            # The shell and gimbals share one rigid transform. Avoid rebuilding
            # two extra full-frame LK pyramids while the outline is trustworthy.
            self.transform = body_transform.copy()
            self.points = None
            self.locked = True
            self.lost_frames = 0
            return self.detect(current, body_quality)
        housing_quality = 1.0
        if self.wonky:
            housing_quality = 0.0
            if not self.locked:
                self.lost_frames += 1
                if self.lost_frames % 5 == 0 and self._reacquire(current):
                    housing_quality = 0.5
                    self.lost_frames = 0
            else:
                if self.points is None or len(self.points) < 8:
                    self.points = self._features(previous)
                if self.points is not None and len(self.points) >= 6:
                    tracked, status, error = cv2.calcOpticalFlowPyrLK(
                        previous, current, self.points, None, winSize=(25, 25), maxLevel=3
                    )
                    if tracked is not None and status is not None and error is not None:
                        backward, reverse_status, _ = cv2.calcOpticalFlowPyrLK(
                            current, previous, tracked, None, winSize=(25, 25), maxLevel=3
                        )
                        good = status.ravel().astype(bool) & (error.ravel() < 25)
                        if backward is None or reverse_status is None:
                            good[:] = False
                        else:
                            good &= reverse_status.ravel().astype(bool)
                            good &= np.linalg.norm(backward.reshape(-1, 2) - self.points.reshape(-1, 2), axis=1) < 1.2
                        source, target = self.points.reshape(-1, 2)[good], tracked.reshape(-1, 2)[good]
                        if len(source) >= 6:
                            affine, inliers = cv2.estimateAffinePartial2D(
                                source, target, method=cv2.RANSAC, ransacReprojThreshold=2.5
                            )
                            if affine is not None and inliers is not None and np.isfinite(affine).all():
                                scale = float(np.hypot(affine[0, 0], affine[1, 0]))
                                mask = inliers.ravel().astype(bool)
                                if 0.8 < scale < 1.25 and mask.sum() >= 6 and mask.mean() >= 0.5:
                                    self.transform = (
                                        np.vstack([affine, [0, 0, 1]]).astype(np.float32) @ self.transform
                                    )
                                    self.points = target[mask].reshape(-1, 1, 2)
                                    housing_quality = min(1.0, int(mask.sum()) / 20) * float(mask.mean())
                if housing_quality > 0 and self._housing_agreement(current) < .55:
                    housing_quality = 0.0
                if housing_quality == 0:
                    self.locked = False
                    self.points = None
                elif self.points is None or len(self.points) < 30:
                    self.points = self._features(current)
        return self.detect(current, housing_quality)

    def detect(self, frame, housing_quality, body_transform=None, body_quality=1.0):
        if housing_quality < .35 or not self._visible(frame):
            return self.gap("housing unobserved or outside frame")
        if self.template is None or float(self.template.std()) < 6:
            # An unspecified/flat tip cannot be inferred from a dark housing
            # blob. It remains a gap until a visible distinctive tip is marked.
            return self.gap("distinctive calibrated tip required")
        crop = self._crop(frame)
        if min(crop.shape) < max(self.template.shape):
            return self.gap("tip template exceeds crop")
        response = cv2.matchTemplate(crop, self.template, cv2.TM_CCOEFF_NORMED)
        response = np.nan_to_num(response, nan=-1, posinf=-1, neginf=-1)
        yy, xx = np.indices(response.shape)
        px, py = xx + (self.template.shape[1] - 1) / 2, yy + (self.template.shape[0] - 1) / 2
        # Calibrated travel bounds constrain every candidate. Keep a small
        # marking tolerance, without treating positions on housing as input.
        travel = ((np.abs(px - self.spec["center_x"]) <= self.spec["travel_x"] * 1.08)
                  & (np.abs(py - self.spec["center_y"]) <= self.spec["travel_y"] * 1.08))
        response[~travel] = -1
        _, score, _, location = cv2.minMaxLoc(response)
        alternatives = response.copy()
        exclusion = max(3, min(self.template.shape) * 1.2)
        alternatives[(xx - location[0]) ** 2 + (yy - location[1]) ** 2 < exclusion ** 2] = -1
        alternative = float(cv2.minMaxLoc(alternatives)[1])
        # Reacquiring an occluded cap requires stronger independent appearance.
        minimum = .76 if self.tip_lost_frames else .70
        margin = .08 if self.tip_lost_frames else .06
        if score < minimum or score - alternative < margin:
            return self.gap("tip covered or appearance ambiguous")
        position = np.array([px[location[1], location[0]], py[location[1], location[0]]])
        visual_quality = float(np.clip((score - .6) / .35, 0, 1))
        crop_position = position.copy()
        if body_transform is not None:
            # Kept for diagnostic callers that supply independently tracked
            # housing and shell poses. All transforms are projective now.
            local_to_camera = self.transform @ np.float32([[1, 0, self.x], [0, 1, self.y], [0, 0, 1]])
            tip_camera = cv2.perspectiveTransform(position.astype(np.float32).reshape(1, 1, 2), local_to_camera)
            tip_radio = cv2.perspectiveTransform(tip_camera, np.linalg.inv(body_transform))[0, 0]
            position = tip_radio - np.array([self.x, self.y])
            visual_quality *= float(np.clip(body_quality, 0, 1))
        normalized = (position - [self.spec["center_x"], self.spec["center_y"]]) / [self.spec["travel_x"], self.spec["travel_y"]]
        if not np.isfinite(normalized).all() or (np.abs(normalized) > 1.08).any():
            return self.gap("tip outside calibrated travel")
        quality = float(housing_quality * visual_quality)
        if quality <= .1:
            return self.gap("tip appearance too weak")
        self.last_tip, self.last_quality = crop_position, quality
        self.tip_lost_frames, self.reason = 0, "visible calibrated tip"
        x, y = np.clip(normalized, -1, 1)
        return float(x), float(y), quality

    def annotate(self, frame):
        color = (60, 240, 120) if self.last_quality > 0.4 else (70, 90, 255)
        corners = np.array(
            [
                [
                    [self.x, self.y],
                    [self.x + self.width, self.y],
                    [self.x + self.width, self.y + self.height],
                    [self.x, self.y + self.height],
                ]
            ],
            np.float32,
        )
        cv2.polylines(frame, [cv2.perspectiveTransform(corners, self.transform)[0].astype(np.int32)], True, color, 2)
        if self.last_tip is not None and self.last_quality > .1:
            point = np.array([[[self.x + self.last_tip[0], self.y + self.last_tip[1]]]], np.float32)
            location = cv2.perspectiveTransform(point, self.transform)[0, 0].astype(int)
            cv2.circle(frame, tuple(location), 6, color, 2)


def iter_sticks(path: str, sample_fps: float, config: dict, duration: float | None = None, _frames=None):
    info = probe_video(path)
    config = validate_calibration(config, (info["width"], info["height"]))
    reference_time = config["reference_time"]
    if reference_time >= info["duration"]:
        raise ValueError("Calibration frame is past the end of the recording")
    scale = min(1.0, MAX_DIMENSION / max(info["width"], info["height"]))
    scaled = scaled_config(config, scale)
    trackers = [
        GimbalTracker(scaled[side], scaled["stickcam_mode"] == "wonky")
        for side in ("left_gimbal", "right_gimbal")
    ]
    outline_tracker = (
        RadioOutlineTracker(
            scaled["radio_outline"], [scaled[side] for side in ("left_gimbal", "right_gimbal")], sample_fps
        )
        if "radio_outline" in scaled
        else None
    )
    previous = None
    frames = _frames if _frames is not None else sampled_frames(path, sample_fps, MAX_DIMENSION, start=reference_time, duration=duration)
    for timestamp, frame in frames:
        if outline_tracker is None:
            body_transform, body_quality = None, 0.0
        elif previous is None:
            body_transform, body_quality = outline_tracker.initialize(frame)
        else:
            body_transform, body_quality = outline_tracker.update(previous, frame)
        if body_quality < 0.35:
            body_transform = None
        if previous is not None and outline_tracker is not None and body_transform is None:
            detected = [tracker.gap("radio face unobserved") for tracker in trackers]
        else:
            detected = [
                tracker.initialize(frame, body_transform, body_quality)
                if previous is None
                else tracker.update(previous, frame, body_transform, body_quality)
                for tracker in trackers
            ]
        if outline_tracker is not None and body_transform is None:
            # A known shell loss must not silently switch to gimbal/background
            # features and report apparently observed controls.
            detected = [tracker.gap("radio face unobserved") for tracker in trackers]
        left, right = detected
        # Crops are expressed in the calibration frame's axes. Relative shell
        # tracking handles later movement; an inverted reference reverses both
        # directions. Physical left/right gimbal labels are kept separately.
        if config.get("radio_rotation_deg", 0) == 180:
            left, right = (-left[0], -left[1], left[2]), (-right[0], -right[1], right[2])
        from fpv_matcher.radio.modes import assigned_inputs
        values = {**assigned_inputs(left, right, config["stick_mode"]), "quality": (left[2] + right[2]) / 2}
        if outline_tracker is not None:
            values["radio_orientation_deg"] = outline_tracker.orientation()
            values["radio_orientation_quality"] = outline_tracker.quality
            trackers_with_outline = [*trackers, outline_tracker]
        else:
            trackers_with_outline = trackers
        for axis in ("roll", "pitch", "yaw", "throttle"):
            values[axis] *= config["signs"][axis]
        yield timestamp, values, frame, trackers_with_outline
        previous = frame


def iter_all_sticks(path, sample_fps, config):
    yield from iter_sticks(path, sample_fps, config)
    if config.get("reference_time", 0) > 0:
        frames = reverse_sampled_frames(path, sample_fps, MAX_DIMENSION, config["reference_time"])
        backwards = iter_sticks(path, sample_fps, config, _frames=frames)
        # Initialize at the same calibrated frame; it was already emitted by the forward pass.
        next(backwards, None)
        yield from backwards


def extract_sticks(path: str, sample_fps: float, config: dict, progress=None) -> dict[str, np.ndarray]:
    keys = (
        "time",
        "roll",
        "pitch",
        "yaw",
        "throttle",
        "roll_quality",
        "pitch_quality",
        "yaw_quality",
        "throttle_quality",
        "quality",
    )
    if "radio_outline" in config:
        keys += ("radio_orientation_deg", "radio_orientation_quality")
    from fpv_matcher.progress import ExtractionProgress
    reporter = ExtractionProgress(probe_video(path)["duration"], 0, progress)
    rows = {key: [] for key in keys}
    for timestamp, values, _, _ in iter_all_sticks(path, sample_fps, config):
        reporter.update(len(rows["time"]) / sample_fps)
        rows["time"].append(timestamp)
        for key, value in values.items():
            rows[key].append(value)
    reporter.update(0, complete=True)
    order = np.argsort(rows["time"])
    return {k: np.asarray(v, float)[order] for k, v in rows.items()}
