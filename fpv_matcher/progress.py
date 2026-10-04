"""Observed extraction progress; ETA is unavailable until enough work has elapsed."""
from __future__ import annotations

import time


class ExtractionProgress:
    def __init__(self, duration, start=0, callback=None):
        self.duration = max(0, duration - start)
        self.start = start
        self.callback = callback
        self.started = time.monotonic()
        self.last_emit = 0

    def update(self, timestamp, complete=False):
        if not self.callback:
            return
        now = time.monotonic()
        if not complete and now - self.last_emit < .75:
            return
        self.last_emit = now
        elapsed = now - self.started
        processed = self.duration if complete else max(0, min(self.duration, timestamp - self.start))
        fraction = min(1, processed / max(self.duration, 1e-6))
        eta = max(0, elapsed * (1 - fraction) / fraction) if elapsed >= 3 and fraction > .002 else None
        self.callback({"percent": round(100 * fraction, 1), "elapsed_seconds": round(elapsed, 1),
                       "eta_seconds": round(eta, 1) if eta is not None else None,
                       "processed_seconds": round(processed, 2), "duration_seconds": round(self.duration, 2)})
