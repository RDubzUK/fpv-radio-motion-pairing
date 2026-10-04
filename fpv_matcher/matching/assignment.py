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
    utility[:, :columns] = np.where(clean >= minimum_score, clean - minimum_score + 1e-9, -1e6)
    utility[np.arange(rows), columns + np.arange(rows)] = 0
    assigned_rows, assigned_columns = linear_sum_assignment(-utility)
    return [
        (int(r), int(c))
        for r, c in zip(assigned_rows, assigned_columns, strict=True)
        if c < columns and clean[r, c] >= minimum_score
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
    selected = set(assign(scores, minimum_score))
    used_fpv = {fi for _, fi in selected}
    def interval(ri, fi):
        offset = offsets[ri, fi]
        start = max(0., offset)
        return start, start + max(0., min(radio_durations[ri] - start, fpv_durations[fi] - max(0., -offset)))
    for fi in sorted(set(range(scores.shape[1])) - used_fpv, key=lambda f: float(np.max(scores[:, f])), reverse=True):
        ri = int(np.argmax(scores[:, fi]))
        other = np.delete(scores[:, fi], ri)
        if scores[ri, fi] < minimum_score or scores[ri, fi] - (float(np.max(other)) if len(other) else 0.) < .04:
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
