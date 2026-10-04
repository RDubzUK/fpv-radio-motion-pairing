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


def candidate_margin(scores: np.ndarray, row: int, column: int) -> float:
    other_rows = np.delete(scores[:, column], row)
    other_columns = np.delete(scores[row], column)
    competitor = max(
        float(np.max(other_rows)) if len(other_rows) else 0,
        float(np.max(other_columns)) if len(other_columns) else 0,
    )
    return float(scores[row, column] - competitor)
