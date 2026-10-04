"""Suggest editable gimbal regions from visible housing rims."""

from __future__ import annotations

import cv2
import numpy as np


def _rim_support(blurred, gradient_x, gradient_y, x, y, radius):
    """Measure radial edges around a rim, allowing partial hand occlusion.

    Hough votes alone reward large circles assembled from unrelated contours.
    Require edges normal to the proposed rim around multiple angular sectors.
    """
    angles = np.linspace(0, 2 * np.pi, 96, endpoint=False)
    cosine, sine = np.cos(angles)[None, :], np.sin(angles)[None, :]
    radii = np.linspace(radius * 0.9, radius * 1.1, 9)[:, None]
    map_x = (x + radii * cosine).astype(np.float32)
    map_y = (y + radii * sine).astype(np.float32)
    gx = cv2.remap(gradient_x, map_x, map_y, cv2.INTER_LINEAR)
    gy = cv2.remap(gradient_y, map_x, map_y, cv2.INTER_LINEAR)
    radial = np.abs(gx * cosine + gy * sine)
    magnitude = np.hypot(gx, gy)
    evidence = ((radial > 45) & (radial / (magnitude + 1e-5) > 0.85)).any(axis=0)
    coverage = float(evidence.mean())
    # Reward housings whose interior differs from the surrounding face, using
    # either polarity so metal gimbals in black cases remain eligible.
    inner_radii = np.linspace(radius * 0.15, radius * 0.75, 7)[:, None]
    outer_radii = np.linspace(radius * 1.15, radius * 1.4, 4)[:, None]
    inner = cv2.remap(
        blurred, (x + inner_radii * cosine).astype(np.float32),
        (y + inner_radii * sine).astype(np.float32), cv2.INTER_LINEAR,
    )
    outer = cv2.remap(
        blurred, (x + outer_radii * cosine).astype(np.float32),
        (y + outer_radii * sine).astype(np.float32), cv2.INTER_LINEAR,
    )
    contrast = float(abs(float(outer.mean()) - float(inner.mean())))
    return coverage, 0.7 + 0.3 * min(1.0, contrast / 40)


def _rectify(gray, outline):
    height, width = gray.shape
    try:
        points = np.asarray(outline, dtype=np.float32).reshape(4, 2)
    except (ValueError, TypeError) as exc:
        raise ValueError("Mark four corners around the radio body.") from exc
    if (
        not np.isfinite(points).all()
        or (points[:, 0] < 0).any() or (points[:, 0] >= width).any()
        or (points[:, 1] < 0).any() or (points[:, 1] >= height).any()
    ):
        raise ValueError("Radio outline is outside the selected frame. Redraw the corners.")
    # The marking order defines the radio's top edge, including when it is
    # rolled in the camera view. Never impose a fixed model's body aspect ratio.
    if not cv2.isContourConvex(points) or cv2.contourArea(points) < 100:
        raise ValueError("Mark the radio corners in order: top left, top right, bottom right, bottom left.")
    target_width = (np.linalg.norm(points[1] - points[0]) + np.linalg.norm(points[2] - points[3])) / 2
    target_height = (np.linalg.norm(points[3] - points[0]) + np.linalg.norm(points[2] - points[1])) / 2
    scale = min(2.0, 960 / max(target_width, target_height))
    target_width, target_height = max(100, round(target_width * scale)), max(100, round(target_height * scale))
    destination = np.asarray(
        [[0, 0], [target_width - 1, 0], [target_width - 1, target_height - 1], [0, target_height - 1]],
        dtype=np.float32,
    )
    forward = cv2.getPerspectiveTransform(points, destination)
    inverse = cv2.getPerspectiveTransform(destination, points)
    return cv2.warpPerspective(gray, forward, (target_width, target_height)), inverse


def suggest_gimbal_regions(frame: np.ndarray, radio_outline: list | None = None, radio_model="other") -> dict:
    """Require visible paired rim evidence; leave ambiguous frames for marking."""
    if frame is None or frame.size == 0:
        raise ValueError("Could not read the reference frame")
    gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    source_height, source_width = gray.shape
    if min(source_height, source_width) < 100:
        raise ValueError("Reference frame is too small to detect gimbal regions")
    inverse = None
    from fpv_matcher.radio.models import model_for, geometry_support, suggest_shell
    model = model_for(radio_model)
    if radio_outline:
        gray, inverse = _rectify(gray, radio_outline)
    height, width = gray.shape
    # Local contrast equalization amplified grass and clothing into hundreds
    # of false circles. Detect on the original, gently smoothed luminance.
    blurred = cv2.GaussianBlur(gray, (7, 7), 1.5)
    gradient_x = cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3)
    gradient_y = cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3)
    top, bottom = round(height * 0.08), round(height * 0.96)
    minimum_radius = max(12, round(min(height, width) * 0.025))
    circles = cv2.HoughCircles(
        blurred[top:bottom], cv2.HOUGH_GRADIENT, dp=1.2,
        minDist=max(18, round(width * 0.045)), param1=120, param2=40,
        minRadius=minimum_radius, maxRadius=max(minimum_radius + 1, round(min(height, width) * 0.19)),
    )
    if circles is None:
        raise ValueError("No visible gimbal rims found. Mark the radio outline or draw both regions.")
    candidates = []
    for x, y, radius in circles[0].astype(float):
        y += top
        # A marked face can clip a housing's outside edge where the hand
        # obscures the shell. Its centre must remain on the face, but requiring
        # the entire circumference here incorrectly removed the true rim.
        border_factor = 0.5 if inverse is not None else 1.15
        if min(x, width - x, y, height - y) < radius * border_factor:
            continue
        coverage, contrast = _rim_support(blurred, gradient_x, gradient_y, x, y, radius)
        if coverage >= 0.55:
            candidates.append((x, y, radius, coverage, contrast))
    pairs = []
    for index, first in enumerate(candidates):
        for second in candidates[index + 1:]:
            left, right = sorted((first, second), key=lambda c: c[0])
            radius = (left[2] + right[2]) / 2
            spacing = (right[0] - left[0]) / radius
            similarity = min(left[2], right[2]) / max(left[2], right[2])
            if not 2.3 <= spacing <= 7 or similarity < 0.65 or abs(left[1] - right[1]) > radius:
                continue
            # One exposed rim must be clear; the other may be partly covered
            # by a thumb or pinch grip. Pair geometry is supporting evidence.
            if max(left[3], right[3]) < 0.72:
                continue
            alignment = np.exp(-abs(left[1] - right[1]) / radius)
            spacing_score = np.exp(-abs(spacing - 4.0) / 3.0)
            rim_score = np.sqrt(left[3] * right[3]) * np.sqrt(left[4] * right[4])
            score = float(similarity * alignment * spacing_score * rim_score)
            if model and inverse is not None:
                score *= .9 + .1 * geometry_support(model, left, right, width, height)
            pairs.append((score, left, right))
    if not pairs:
        raise ValueError("No pair has enough visible housing-rim evidence. Mark the radio outline or draw both regions.")
    pairs.sort(key=lambda pair: pair[0], reverse=True)
    score, left, right = pairs[0]
    if score < 0.48:
        raise ValueError("The gimbal regions are uncertain. Mark the radio outline or draw both regions.")
    for rival_score, rival_left, rival_right in pairs[1:]:
        distinct = any(
            np.hypot(a[0] - b[0], a[1] - b[1]) > max(a[2], b[2])
            for a, b in ((left, rival_left), (right, rival_right))
        )
        if distinct and score - rival_score < 0.08:
            raise ValueError("Several different gimbal pairs look plausible. Mark the radio outline to constrain the search.")
    regions = []
    for x, y, radius, coverage, _ in (left, right):
        # Enclose the housing with modest padding. Oversized boxes placed the
        # neutral centre on hands and also inflated the estimated stick travel.
        side = min(round(radius * 2.5), 2 * int(min(x, width - x, y, height - y)))
        x0, y0 = round(x - side / 2), round(y - side / 2)
        x1, y1 = x0 + side, y0 + side
        center = np.asarray([[[x, y]]], dtype=np.float32)
        corners = np.asarray([[[x0, y0], [x1, y0], [x1, y1], [x0, y1]]], dtype=np.float32)
        if inverse is not None:
            corners = cv2.perspectiveTransform(corners, inverse)
            center = cv2.perspectiveTransform(center, inverse)
        low, high = corners[0].min(axis=0), corners[0].max(axis=0)
        x0, y0 = max(0, int(np.floor(low[0]))), max(0, int(np.floor(low[1])))
        x1, y1 = min(source_width, int(np.ceil(high[0]))), min(source_height, int(np.ceil(high[1])))
        region_width, region_height = x1 - x0, y1 - y0
        if min(region_width, region_height) < 24:
            raise ValueError("Gimbal regions are too small or leave the frame. Draw both regions manually.")
        regions.append({
            "x": x0, "y": y0, "width": region_width, "height": region_height,
            "center_x": float(center[0, 0, 0] - x0), "center_y": float(center[0, 0, 1] - y0),
            "travel_x": region_width * 0.4, "travel_y": region_height * 0.4,
            "tip_radius": max(3.0, min(region_width, region_height) * 0.035),
            "threshold": 70, "polarity": "dark", "rim_edge_support": coverage,
        })
    proposal = {
        "regions": {"left_gimbal": regions[0], "right_gimbal": regions[1]},
        "geometry_score": score, "detection_method": "paired housing rims",
    }
    return suggest_shell(frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), proposal, radio_model) if not radio_outline else proposal
