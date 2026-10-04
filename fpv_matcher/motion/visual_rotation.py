"""Experimental relative camera rotation from tracked, normalized viewing rays.

FOV profiles approximate lens intrinsics; this does not measure absolute attitude,
gravity, metric translation or throttle. Camera coordinates: right/down/forward.
"""
from __future__ import annotations

import cv2
import numpy as np

DEFAULT_PROFILE = {"horizontal_fov_deg": 110., "projection": "rectilinear"}


def validate_profile(profile=None):
    profile = dict(DEFAULT_PROFILE if profile is None else profile)
    projection = profile.get("projection", "rectilinear")
    fov = float(profile.get("horizontal_fov_deg", 110))
    if projection not in {"rectilinear", "equidistant"}:
        raise ValueError("Choose rectilinear or equidistant fisheye visual projection")
    limit = 170 if projection == "rectilinear" else 200
    if not np.isfinite(fov) or not 30 <= fov <= limit:
        raise ValueError(f"Horizontal FOV must be 30–{limit} degrees for this projection")
    tracking = profile.get("track_full_rate", True)
    if not isinstance(tracking, bool):
        raise ValueError("Intermediate frame tracking must be enabled or disabled")
    return {"horizontal_fov_deg": fov, "projection": projection, "track_full_rate": tracking}


def viewing_rays(points, size, profile):
    width, height = size
    fov = np.radians(profile["horizontal_fov_deg"])
    focal = width / (2 * np.tan(fov / 2)) if profile["projection"] == "rectilinear" else width / fov
    centered = (np.asarray(points, float) - [width / 2, height / 2]) / focal
    if profile["projection"] == "equidistant":
        radius = np.linalg.norm(centered, axis=1)
        theta = radius
        rays = np.column_stack((centered * (np.sin(theta) / np.maximum(radius, 1e-12))[:, None], np.cos(theta)))
        rays[radius < 1e-10] = [0, 0, 1]
    else:
        rays = np.column_stack((centered, np.ones(len(centered))))
    rays /= np.linalg.norm(rays, axis=1)[:, None]
    return rays, focal


def _fit_rotation(a, b):
    u, _, vt = np.linalg.svd(a.T @ b)
    rotation = vt.T @ u.T
    if np.linalg.det(rotation) < 0:
        vt[-1] *= -1
        rotation = vt.T @ u.T
    return rotation


def _distributed(points, size):
    width, height = size
    cells = np.clip((points / [width, height] * [4, 3]).astype(int), [0, 0], [3, 2])
    return len(set(map(tuple, cells))) >= 5 and cv2.contourArea(cv2.convexHull(points.astype(np.float32))) >= width * height * .12


def estimate_rotation(source, target, size, delta, profile, diagnostics=False):
    result = _estimate_rotation(source, target, size, delta, profile)
    return result if diagnostics else result[:3]


GAP_REASONS = {1: "Too few landmarks", 2: "Landmarks cover too little of the image",
               3: "Viewing rays outside supported lens geometry", 4: "No consistent 3D pose",
               5: "Too little distributed pose support", 6: "Planar scene / ambiguous translation",
               7: "Rotation exceeds plausible tracking limits"}


def _estimate_rotation(source, target, size, delta, profile):
    """Return camera local angular velocity and observation quality, or a gap.

    A rotation-only ray fit handles distant/low-parallax views. Essential pose
    recovery handles translation when enough nonplanar cheirality support is
    visible. Degenerate or disagreeing solutions are rejected conservatively.
    """
    zero = (np.zeros(3), 0., 0)
    source, target = np.asarray(source), np.asarray(target)
    finite = np.isfinite(source).all(axis=1) & np.isfinite(target).all(axis=1)
    source, target = source[finite], target[finite]
    if len(source) < 20 or not np.isfinite(delta) or delta <= 0:
        return (*zero, 1)
    if not _distributed(source, size):
        return (*zero, 2)
    a, focal = viewing_rays(source, size, profile)
    b, _ = viewing_rays(target, size, profile)
    forward = (a[:, 2] > .15) & (b[:, 2] > .15)
    if forward.sum() < 20:
        return (*zero, 3)
    a, b, source, target = a[forward], b[forward], source[forward], target[forward]
    threshold = min(.025, max(.003, 2.5 / focal))
    rng = np.random.default_rng(0)
    best = None
    for _ in range(48):
        chosen = rng.choice(len(a), 3, replace=False)
        rotation = _fit_rotation(a[chosen], b[chosen])
        errors = np.linalg.norm(a @ rotation.T - b, axis=1)
        mask = errors < threshold
        if best is None or mask.sum() > best[0].sum():
            best = mask, rotation
    mask, rotation = best
    pure = None
    if mask.sum() >= 20 and mask.mean() >= .8 and _distributed(source[mask], size) and _distributed(target[mask], size):
        rotation = _fit_rotation(a[mask], b[mask])
        residual = np.linalg.norm(a @ rotation.T - b, axis=1)
        mask = residual < threshold
        if mask.mean() >= .8 and np.median(residual[mask]) < threshold * .55:
            pure = rotation, mask, .65
    # Pose recovery is unstable under pure rotation; use the strong ray fit
    # directly instead of triangulating an unobservable translation.
    solution = pure
    if solution is None:
        pa, pb = a[:, :2] / a[:, 2:], b[:, :2] / b[:, 2:]
        try:
            essential, inliers = cv2.findEssentialMat(pa, pb, np.eye(3), method=cv2.RANSAC,
                prob=.999, threshold=threshold, maxIters=500)
            if essential is None or essential.shape != (3, 3) or inliers is None:
                return (*zero, 4)
            count, rotation, translation, pose_mask = cv2.recoverPose(essential, pa, pb, np.eye(3), mask=inliers.copy())
            mask = pose_mask.ravel() > 0
            if count < 25 or mask.mean() < .65 or not _distributed(source[mask], size) or not _distributed(target[mask], size):
                return (*zero, 5)
            # Planar/depth-degenerate views do not establish a unique 3D pose.
            _, planar = cv2.findHomography(pa, pb, cv2.RANSAC, threshold)
            if planar is not None and planar.mean() > .85:
                return (*zero, 6)
            solution = rotation, mask, .5
        except cv2.error:
            return (*zero, 4)
    rotation, mask, cap = solution
    # Scene coordinates transform opposite to physical camera motion.
    vector = -cv2.Rodrigues(rotation)[0].ravel()
    rate = np.degrees(vector) / delta
    if not np.isfinite(rate).all() or np.linalg.norm(rate) > 2200 or np.linalg.norm(vector) > np.radians(45):
        return (*zero, 7)
    quality = min(cap, float(mask.mean()) * min(1., mask.sum() / 70) * cap)
    return rate, quality, 1 if pure is not None else 2, 0


def visual_body_rates(data, uptilt=0):
    from scipy.spatial.transform import Rotation
    from fpv_matcher.motion.telemetry import validate_motion_settings
    # Visual camera axes are right/down/forward. Z/X/Y is a proper cyclic
    # rotation into body forward/right/down; lowercase would negate all axes
    # and describe an impossible reflection instead of a rotation.
    _, uptilt, _, _ = validate_motion_settings("visual", uptilt, "ZXY", 0)
    vectors = np.column_stack([data[k] for k in ("z", "x", "y")])
    body = Rotation.from_euler("y", uptilt, degrees=True).apply(vectors)
    return {**data, "roll": body[:, 0], "pitch": body[:, 1], "yaw": body[:, 2],
            "_body_rates": np.asarray(True), "motion_source": np.asarray("visual_rotation"),
            "motion_label": np.asarray("Experimental visual body angular rates · approximate lens geometry")}


def gap_summary(data):
    missing = np.asarray(data["quality"]) <= .1
    if "gap_reason" not in data:
        return {"Unreliable rotation (older extraction)": int(missing.sum())} if missing.any() else {}
    reasons = np.asarray(data["gap_reason"], int)
    return {GAP_REASONS.get(int(code), "Unreliable rotation"): int(((reasons == code) & missing).sum())
            for code in np.unique(reasons[missing])}


def display_interpolation(data, maximum_span=0.):
    """A copy for charts only. Never promote estimates to matching evidence."""
    if not np.isfinite(maximum_span) or not 0 <= maximum_span <= .5:
        raise ValueError("Choose a gap span between 0 and 0.5 seconds")
    times, quality = np.asarray(data["time"]), np.asarray(data["quality"])
    filled = np.zeros(len(times), bool)
    observed = np.flatnonzero(quality > .1)
    result = dict(data)
    if maximum_span and len(observed) > 1:
        for left, right in zip(observed[:-1], observed[1:]):
            if right > left + 1 and 0 < times[right] - times[left] <= maximum_span + 1e-8:
                filled[left+1:right] = True
        for axis in ("roll", "pitch", "yaw"):
            values = np.asarray(data[axis]).copy()
            values[filled] = np.interp(times[filled], times[observed], values[observed])
            result[axis] = values
            result[axis + "_quality"] = np.where(filled, .101, quality)
    result["_interpolated"] = filled
    return result
