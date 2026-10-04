"""Suggest current stick-tip positions; never substitute the neutral centre."""

from __future__ import annotations

import cv2
import numpy as np

from fpv_matcher.radio.auto_calibration import _rim_support


def suggest_reference_tip(reference, frame, transform, source, target):
    """Search the moving gimbal interior using a confirmed tip's appearance."""
    if not all(key in source for key in ("tip_x", "tip_y")):
        return None
    x, y, width, height = [int(round(source[key])) for key in ("x", "y", "width", "height")]
    if min(width, height) < 24:
        return None
    local_to_frame = transform @ np.asarray([[1, 0, x], [0, 1, y], [0, 0, 1]], np.float64)
    crop = cv2.warpPerspective(
        frame, local_to_frame, (width, height),
        flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderValue=127,
    )
    tx, ty = round(x + source["tip_x"]), round(y + source["tip_y"])
    candidates = []
    for fraction in (0.04, 0.06, 0.08):
        radius = max(4, round(min(width, height) * fraction))
        if tx - radius < 0 or ty - radius < 0 or tx + radius >= reference.shape[1] or ty + radius >= reference.shape[0]:
            continue
        template = reference[ty - radius:ty + radius + 1, tx - radius:tx + radius + 1]
        if float(template.std()) < 6:
            continue
        response = cv2.matchTemplate(crop, template, cv2.TM_CCOEFF_NORMED)
        yy, xx = np.indices(response.shape)
        inside = (
            ((xx + radius - source["center_x"]) / (width * 0.4)) ** 2
            + ((yy + radius - source["center_y"]) / (height * 0.4)) ** 2
        ) <= 1
        response[~inside] = -1
        _, score, _, location = cv2.minMaxLoc(response)
        alternatives = response.copy()
        alternatives[(xx - location[0]) ** 2 + (yy - location[1]) ** 2 < (radius * 2.5) ** 2] = -1
        margin = score - cv2.minMaxLoc(alternatives)[1]
        if score >= 0.70 and margin >= 0.05:
            candidates.append((score, radius, location))
    if not candidates:
        return None
    score, radius, location = max(candidates, key=lambda item: item[0])
    point = np.float32([[[x + location[0] + radius, y + location[1] + radius]]])
    mapped = cv2.perspectiveTransform(point, transform)[0, 0]
    tip_x, tip_y = float(mapped[0] - target["x"]), float(mapped[1] - target["y"])
    if not 0 <= tip_x < target["width"] or not 0 <= tip_y < target["height"]:
        return None
    scale = np.sqrt(target["width"] * target["height"] / (width * height))
    return {
        "tip_x": tip_x, "tip_y": tip_y,
        "tip_radius": float(np.clip(source.get("tip_radius", radius) * scale, 3, min(target["width"], target["height"]) * 0.18)),
        "tip_suggested": True, "tip_suggestion_method": "reference appearance", "tip_match_score": float(score),
    }


def suggest_visible_tip(frame, region):
    """Find a distinct, contrasting round cap inside the housing travel area.

    This fallback deliberately leaves flat, covered or ambiguous caps unmarked.
    It cannot infer a hidden tip from the pilot's thumb or pinch grip.
    """
    x, y, width, height = [int(round(region[key])) for key in ("x", "y", "width", "height")]
    crop = frame[y:y + height, x:x + width]
    if crop.size == 0 or min(crop.shape[:2]) < 24:
        return None
    blurred = cv2.GaussianBlur(crop, (3, 3), 0.8)
    gx = cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3)
    circles = cv2.HoughCircles(
        blurred, cv2.HOUGH_GRADIENT, dp=1, minDist=max(6, round(min(width, height) * 0.055)),
        param1=100, param2=15, minRadius=max(4, round(min(width, height) * 0.045)),
        maxRadius=max(5, round(min(width, height) * 0.14)),
    )
    if circles is None:
        return None
    yy, xx = np.indices(crop.shape)
    candidates = []
    for cx, cy, radius in circles[0].astype(float):
        if ((cx - region["center_x"]) / (width * 0.4)) ** 2 + ((cy - region["center_y"]) / (height * 0.4)) ** 2 > 1:
            continue
        if min(cx, cy, width - cx, height - cy) < radius * 1.4:
            continue
        coverage, _ = _rim_support(blurred, gx, gy, cx, cy, radius)
        distance = np.hypot(xx - cx, yy - cy)
        inside = crop[distance < radius * 0.65]
        outside = crop[(distance > radius * 1.15) & (distance < radius * 1.4)]
        contrast = float(inside.mean() - outside.mean())
        score = 0.55 * coverage + 0.45 * min(abs(contrast) / 80, 1)
        if coverage >= 0.70 and abs(contrast) >= 40:
            candidates.append((score, cx, cy, radius, contrast))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    score, cx, cy, radius, contrast = candidates[0]
    if score < 0.80:
        return None
    if any(score - rival[0] < 0.08 and np.hypot(cx - rival[1], cy - rival[2]) > radius for rival in candidates[1:]):
        return None
    # A cap's bright/dark face can refine an offset circle caused by its teeth.
    distance = np.hypot(xx - cx, yy - cy)
    local = distance <= radius
    baseline = float(np.median(crop[local]))
    weights = np.maximum((crop.astype(float) - baseline) * np.sign(contrast), 0) * local
    if weights.sum() > 0:
        refined = np.array([(weights * xx).sum(), (weights * yy).sum()]) / weights.sum()
        if np.linalg.norm(refined - [cx, cy]) <= radius * 0.45:
            cx, cy = refined
    return {
        "tip_x": float(cx), "tip_y": float(cy), "tip_radius": float(radius),
        "tip_suggested": True, "tip_suggestion_method": "visible cap", "tip_match_score": float(score),
    }
