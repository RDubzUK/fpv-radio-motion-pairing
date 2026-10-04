from __future__ import annotations

import numpy as np
from scipy.optimize import linear_sum_assignment


def assign(scores: np.ndarray, minimum_score: float = 0.55) -> list[tuple[int, int]]:
    """One-to-one proposals with a private unmatched option for every radio clip."""
    if scores.ndim != 2 or not scores.size:
        return []
    rows, columns = scores.shape
    utility = np.full((rows, columns + rows), -1e6, dtype=float)
    clean = np.nan_to_num(scores, nan=0, posinf=0, neginf=0)
    eligible = (clean > 0) & (clean >= minimum_score)
    utility[:, :columns] = np.where(eligible, clean - minimum_score + 1e-9, -1e6)
    utility[np.arange(rows), columns + np.arange(rows)] = 0
    assigned_rows, assigned_columns = linear_sum_assignment(-utility)
    return [
        (int(r), int(c))
        for r, c in zip(assigned_rows, assigned_columns, strict=True)
        if c < columns and eligible[r, c]
    ]


def candidate_margin(scores: np.ndarray, row: int, column: int, multiple_fpv=False) -> float:
    other_rows = np.delete(scores[:, column], row)
    other_columns = np.asarray([]) if multiple_fpv else np.delete(scores[row], column)
    competitor = max(
        float(np.max(other_rows)) if len(other_rows) else 0,
        float(np.max(other_columns)) if len(other_columns) else 0,
    )
    return float(scores[row, column] - competitor)


def assign_segments(scores, offsets, radio_durations, fpv_durations, minimum_score=.55):
    """Keep one FPV owner; allow independently supported, disjoint StickCam spans."""
    scores = np.asarray(scores, float)
    if scores.ndim != 2 or not scores.size:
        return []
    if len(radio_durations) != scores.shape[0] or len(fpv_durations) != scores.shape[1]:
        raise ValueError("Pairing durations must match the candidate matrix")
    clean = np.nan_to_num(scores, nan=-1., posinf=-1., neginf=-1.)
    intervals = {}
    for ri in range(scores.shape[0]):
        for fi in range(scores.shape[1]):
            offset = offsets.get((ri, fi))
            if offset is None or not np.isfinite(offset):
                clean[ri, fi] = -1.
                continue
            start = max(0., float(offset))
            end = start + max(0., min(radio_durations[ri] - start, fpv_durations[fi] - max(0., -float(offset))))
            if not np.isfinite(end) or end <= start:
                clean[ri, fi] = -1.
                continue
            intervals[ri, fi] = start, end
    def interval(ri, fi):
        return intervals[ri, fi]
    selected = set(assign(clean, minimum_score))
    used_fpv = {fi for _, fi in selected}
    for fi in sorted(set(range(scores.shape[1])) - used_fpv, key=lambda f: float(np.max(clean[:, f])), reverse=True):
        ri = int(np.argmax(clean[:, fi]))
        other = np.delete(clean[:, fi], ri)
        if clean[ri, fi] <= 0 or clean[ri, fi] < minimum_score or clean[ri, fi] - max(0., float(np.max(other)) if len(other) else 0.) < .04:
            continue
        start, end = interval(ri, fi)
        if end <= start:
            continue
        # A small tolerance permits frame/filename boundary rounding, not duplicate coverage.
        tolerance = min(2., (end - start) * .1)
        if any(max(0., min(end, interval(r, f)[1]) - max(start, interval(r, f)[0])) > tolerance
               for r, f in selected if r == ri):
            continue
        selected.add((ri, fi))
    return sorted(selected)
