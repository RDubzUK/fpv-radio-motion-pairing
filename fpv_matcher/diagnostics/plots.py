from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from fpv_matcher.matching.correlation import MatchResult
from fpv_matcher.matching.signals import standardized


def plot_alignment(
    radio, fpv, result: MatchResult, output: str | Path, title="", radio_audio=None, fpv_audio=None
):
    has_audio = (
        radio_audio is not None
        and fpv_audio is not None
        and len(radio_audio["time"])
        and len(fpv_audio["time"])
    )
    fig, axes = plt.subplots(5 if has_audio else 4, 1, figsize=(12, 11 if has_audio else 9), sharex=True)
    fpv_keys = ("roll", "pitch", "yaw") if bool(fpv.get("_body_rates", False)) else ("rotation", "dy", "dx")
    for ax, rkey, fkey in zip(axes, ("roll", "pitch", "yaw"), fpv_keys, strict=False):
        rq, fq = radio.get(rkey + "_quality", radio["quality"]), fpv["quality"]
        rsignal, rlabel = radio[rkey], rkey
        if f"d_{rkey}:{fkey}" in result.details and len(rsignal) > 1:
            rsignal, rlabel = np.gradient(rsignal, radio["time"]), "d_" + rkey
            rq = np.minimum(rq, np.minimum(np.r_[0, rq[:-1]], np.r_[rq[1:], 0]))
        a, b = standardized(rsignal, rq), standardized(fpv[fkey], fq)
        a[rq <= 0.1], b[fq <= 0.1] = np.nan, np.nan
        sign = result.details.get(f"{rkey}:{fkey}", result.details.get(f"d_{rkey}:{fkey}", 1)) or 1
        ax.plot(radio["time"], a, label=f"StickCam {rlabel}", lw=1)
        ax.plot(fpv["time"] + result.offset_seconds, b * sign, label=f"FPV {fkey} (sign adjusted)", lw=1)
        ax.set_ylabel("Normalised signal")
        ax.grid(alpha=0.2)
        ax.legend(loc="upper right")
    quality_ax = axes[3]
    quality_ax.plot(radio["time"], radio["quality"], label="StickCam tracking")
    quality_ax.plot(fpv["time"] + result.offset_seconds, fpv["quality"], label="FPV tracking")
    quality_ax.set_ylim(0, 1.05)
    quality_ax.set_ylabel("Quality")
    quality_ax.legend(loc="upper right")
    if has_audio:
        ax = axes[4]
        for data, offset, label in ((radio_audio, 0, "StickCam"), (fpv_audio, result.offset_seconds, "FPV")):
            for number, segment in enumerate(np.unique(data["segments"])):
                indices = data["segments"] == segment
                ax.plot(
                    data["time"][indices] + offset,
                    data["features"][indices, 0],
                    label=label if number == 0 else None,
                    lw=1,
                )
        ax.set_ylabel("Boundary audio")
        ax.legend(loc="upper right")
        ax.grid(alpha=0.2)
    axes[-1].set_xlabel("Seconds on StickCam timeline (gaps have zero matching weight)")
    fig.suptitle(
        f"{title}\nMotion score {result.score:.3f} · offset {result.offset_seconds:+.3f}s · competing peak margin {result.peak_margin:.3f}"
    )
    fig.tight_layout()
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=120)
    plt.close(fig)
