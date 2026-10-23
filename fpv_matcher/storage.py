"""Atomic metadata writes, including bounded Windows sharing-violation retries."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path


class SaveError(RuntimeError):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    delays = (.05, .1, .2, .4, .5, .75)
    for attempt in range(len(delays) + 1):
        try:
            os.replace(temporary, path)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) in {5, 32, 33} and attempt < len(delays):
                time.sleep(delays[attempt])
                continue
            raise SaveError(f"Could not save {path}. The previous JSON is preserved; new data is retained at {temporary}. Check folder permissions and programs using the file. {exc}") from exc
