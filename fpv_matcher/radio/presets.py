"""User-editable project rate presets, shared across sessions."""
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

import yaml

from fpv_matcher.radio.rates import validate_rate_profile

FILE = Path(__file__).resolve().parents[2] / "rate_presets.yaml"
LOCK = threading.RLock()


def load_presets():
    with LOCK:
        if not FILE.exists():
            return {"version": 1, "default": None, "presets": {}}
        if FILE.stat().st_size > 256 * 1024:
            raise ValueError("Rate preset file exceeds 256 KB")
        try:
            data = yaml.safe_load(FILE.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid rate_presets.yaml: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("presets", {}), dict):
            raise ValueError("rate_presets.yaml must contain a presets mapping")
        presets = {}
        for name, profile in data.get("presets", {}).items():
            if not isinstance(name, str) or not name.strip() or len(name) > 120 or any(ord(c) < 32 for c in name):
                raise ValueError("Preset names must be nonempty text up to 120 characters")
            profile = validate_rate_profile(profile)
            if profile is None:
                raise ValueError(f"Preset {name} has no rate profile")
            presets[name] = profile
        default = data.get("default")
        if default is not None and (not isinstance(default, str) or default not in presets):
            raise ValueError("The default preset must name an existing preset")
        return {"version": 1, "default": default, "presets": presets}


def save_preset(name, profile, make_default=False):
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120 or any(ord(c) < 32 for c in name):
        raise ValueError("Enter a preset name up to 120 characters")
    name = name.strip()
    profile = validate_rate_profile(profile)
    if profile is None:
        raise ValueError("Enter a rate profile before saving")
    if not isinstance(make_default, bool):
        raise ValueError("Default selection must be true or false")
    with LOCK:
        data = load_presets()
        data["presets"][name] = profile
        if make_default:
            data["default"] = name
        descriptor, temporary = tempfile.mkstemp(prefix=".rate-presets-", suffix=".yaml", dir=FILE.parent)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write("# Shared FPV rate presets. Model values use the same units as the web form.\n")
                yaml.safe_dump(data, stream, sort_keys=False)
            os.replace(temporary, FILE)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return data
