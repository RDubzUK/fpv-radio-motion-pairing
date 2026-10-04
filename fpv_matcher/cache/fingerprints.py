from __future__ import annotations

import hashlib
import json
import os
import tempfile
from zipfile import BadZipFile
from pathlib import Path

import numpy as np


def _key(path: str, algorithm: str, config: dict, identity: str | None = None) -> str:
    p = Path(path).resolve()
    stat = p.stat()
    source = (
        {"sha256": identity, "size": stat.st_size}
        if identity
        else {"path": str(p), "size": stat.st_size, "mtime": stat.st_mtime_ns}
    )
    payload = json.dumps(
        {"source": source, "algorithm": algorithm, "config": config}, sort_keys=True, allow_nan=False
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def get_or_extract(
    path: str,
    cache_dir: str | Path,
    kind: str,
    algorithm: str,
    config: dict,
    extract,
    identity: str | None = None,
    validate=None,
) -> dict[str, np.ndarray]:
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    key = _key(path, algorithm, config, identity)
    target = directory / f"{kind}.{key}.npz"
    if target.exists():
        try:
            with np.load(target, allow_pickle=False) as data:
                if str(data["cache_key"].item()) == key:
                    fingerprint = {k: data[k] for k in data.files if k != "cache_key"}
                    if validate:
                        validate(fingerprint)
                    return fingerprint
        except (OSError, ValueError, KeyError, EOFError, BadZipFile):
            pass
    fingerprint = extract()
    if validate:
        validate(fingerprint)
    # An interrupted worker cannot leave a partially written cache entry.
    descriptor, name = tempfile.mkstemp(prefix=".fingerprint-", suffix=".npz", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            np.savez_compressed(stream, cache_key=np.asarray(key), **fingerprint)
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)
    return fingerprint
