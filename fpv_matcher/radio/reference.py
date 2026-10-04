"""Locate a radio using an operator-confirmed face and gimbal reference."""

from __future__ import annotations

import cv2
import numpy as np

from fpv_matcher.radio.tips import suggest_reference_tip


def suggest_from_reference(reference, frame, outline, regions, detect_tips=True):
    """Return editable landmarks only when distributed feature matches agree.

    Reference images must belong to confirmed calibrations. This recognises a
    saved radio appearance. Moving tips require a separate appearance search
    after the face transform has been validated.
    """
    outline = np.asarray(outline, np.float32)
    if outline.shape != (4, 2) or not np.isfinite(outline).all():
        return None
    area = abs(cv2.contourArea(outline))
    if area < 2500 or not cv2.isContourConvex(outline):
        return None
    mask = np.zeros(reference.shape[:2], np.uint8)
    cv2.fillConvexPoly(mask, np.int32(outline), 255)
    sift = cv2.SIFT_create(nfeatures=4000, contrastThreshold=0.02)
    source_keys, source_descriptors = sift.detectAndCompute(reference, mask)
    target_keys, target_descriptors = sift.detectAndCompute(frame, None)
    if source_descriptors is None or target_descriptors is None:
        return None
    if min(len(source_descriptors), len(target_descriptors)) < 8:
        return None
    matcher = cv2.BFMatcher()
    # Mutual matches prevent many repeated rim details voting for one pixel.
    reverse = {
        first.queryIdx: first.trainIdx
        for first, second in matcher.knnMatch(target_descriptors, source_descriptors, k=2)
        if first.distance < 0.75 * second.distance
    }
    matches = [
        first for first, second in matcher.knnMatch(source_descriptors, target_descriptors, k=2)
        if first.distance < 0.75 * second.distance and reverse.get(first.trainIdx) == first.queryIdx
    ]
    if len(matches) < 8:
        return None
    source = np.float32([source_keys[match.queryIdx].pt for match in matches])
    target = np.float32([target_keys[match.trainIdx].pt for match in matches])
    transform, inliers = cv2.findHomography(source, target, cv2.RANSAC, 3.0)
    if transform is None or inliers is None or not np.isfinite(transform).all():
        return None
    keep = inliers.ravel().astype(bool)
    count = int(keep.sum())
    if count < 8 or count / len(matches) < 0.45:
        return None
    matched = source[keep]
    unique = np.unique(np.round(matched), axis=0)
    coverage = cv2.contourArea(cv2.convexHull(matched)) / area
    span = np.ptp(matched, axis=0) / np.maximum(1, np.ptp(outline, axis=0))
    # Text/logo-only matches cannot support extrapolating the whole shell.
    if len(unique) < 8 or coverage < 0.06 or min(span) < 0.25 or max(span) < 0.4:
        return None
    projected = cv2.perspectiveTransform(source[keep, None, :], transform)[:, 0]
    error = float(np.percentile(np.linalg.norm(projected - target[keep], axis=1), 90))
    if error > 2.5:
        return None
    mapped_outline = cv2.perspectiveTransform(outline[None], transform)[0]
    height, width = frame.shape[:2]
    if (
        not np.isfinite(mapped_outline).all() or not cv2.isContourConvex(mapped_outline)
        or cv2.contourArea(outline, oriented=True) * cv2.contourArea(mapped_outline, oriented=True) <= 0
        or (mapped_outline < 0).any()
        or (mapped_outline[:, 0] >= width).any() or (mapped_outline[:, 1] >= height).any()
    ):
        return None
    mapped_area = cv2.contourArea(mapped_outline)
    if not 0.015 * width * height < mapped_area < 0.8 * width * height:
        return None
    edge_scales = np.linalg.norm(np.roll(mapped_outline, -1, axis=0) - mapped_outline, axis=1)
    edge_scales /= np.maximum(1, np.linalg.norm(np.roll(outline, -1, axis=0) - outline, axis=1))
    if max(edge_scales) / min(edge_scales) > 2.0:
        return None
    result = {}
    for side in ("left_gimbal", "right_gimbal"):
        region = regions[side]
        x, y, w, h = [region[key] for key in ("x", "y", "width", "height")]
        points = np.float32([[
            [x, y], [x + w, y], [x + w, y + h], [x, y + h],
            [x + region["center_x"], y + region["center_y"]],
        ]])
        mapped = cv2.perspectiveTransform(points, transform)[0]
        low, high = np.floor(mapped[:4].min(axis=0)), np.ceil(mapped[:4].max(axis=0))
        center = mapped[4]
        if (
            not np.isfinite(mapped).all() or (low < 0).any()
            or high[0] >= width or high[1] >= height or min(high - low) < 24
            or cv2.pointPolygonTest(mapped_outline, tuple(map(float, center)), False) < 0
        ):
            return None
        new_width, new_height = map(int, high - low)
        result[side] = {
            "x": int(low[0]), "y": int(low[1]), "width": new_width, "height": new_height,
            "center_x": float(center[0] - low[0]), "center_y": float(center[1] - low[1]),
            # Tip position and travel require checking in the selected frame.
            "travel_x": new_width * 0.4, "travel_y": new_height * 0.4,
            "tip_radius": max(3.0, min(new_width, new_height) * 0.035),
            "threshold": region.get("threshold", 70), "polarity": region.get("polarity", "dark"),
        }
        tip = suggest_reference_tip(reference, frame, transform, region, result[side]) if detect_tips else None
        if tip is not None:
            result[side].update(tip)
    return {
        # Keep shell corners in image order. Physical gimbal identities are
        # independent; an inverted target puts physical left on image right.
        "radio_outline": (np.roll(mapped_outline, 2, axis=0) if mapped_outline[1, 0] < mapped_outline[0, 0]
                          else mapped_outline).tolist(), "regions": result,
        "detection_method": "saved radio reference", "reference_inliers": count,
        "reference_coverage": float(coverage), "reference_error_px": error,
    }
