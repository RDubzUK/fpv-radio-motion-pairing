"""Selection of matching evidence, including migration of legacy settings."""

KEYS = ("fpv_gyro", "fpv_visual", "stick_inputs", "fpv_audio", "stickcam_audio", "filename_timestamps", "movement_rhythm", "gimbal_activity")


def normalize_sources(value=None, motion_source="auto", audio_enabled=True):
    sources = dict(zip(KEYS, (motion_source != "visual", motion_source != "gyro", True,
                             audio_enabled, audio_enabled, True, False, False)))
    if value is not None:
        if not isinstance(value, dict) or set(value) - set(KEYS):
            raise ValueError("Choose supported matching evidence sources")
        if any(not isinstance(enabled, bool) for enabled in value.values()):
            raise ValueError("Evidence source settings must be true or false")
        sources.update(value)
    if not motion_enabled(sources) and not audio_enabled_pair(sources):
        raise ValueError("Select both audio sources, or StickCam inputs/gimbal activity and an FPV motion source. Filename timestamps need an independent content anchor.")
    return sources


def motion_enabled(sources):
    return (sources["stick_inputs"] or sources.get("gimbal_activity", False)) and (sources["fpv_gyro"] or sources["fpv_visual"])


def precise_motion_enabled(sources):
    return sources["stick_inputs"] and (sources["fpv_gyro"] or sources["fpv_visual"])


def audio_enabled_pair(sources):
    return sources["fpv_audio"] and sources["stickcam_audio"]
