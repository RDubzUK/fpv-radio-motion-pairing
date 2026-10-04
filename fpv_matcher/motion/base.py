from __future__ import annotations

from typing import Protocol

import numpy as np


class MotionEstimator(Protocol):
    algorithm_version: str

    def extract(self, path: str, sample_fps: float) -> dict[str, np.ndarray]: ...
