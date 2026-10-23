"""Render results after the movement-only matcher has committed its predictions."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def read(path):
    return json.loads(path.read_text())


def fmt(seconds):
    return f"{seconds:.2f}s" if seconds < 60 else f"{int(seconds//60)}m {seconds%60:.1f}s"


def report(output):
    scored = read(output / "scored-results.json")
    summary, cases = scored["summary"], scored["cases"]
    blind = read(output / "blind-results.json")
    timings = read(output / "extraction-timings.json")
    sources = read(output / "extraction-input.json")
    environment = read(output / "environment.json")
    by_id = {t["id"]: t for t in timings}
    by_source = {r["id"]: r for records in sources.values() for r in records}
    n = len(cases)
    radio_ids, fpv_ids = sorted(sources["radio"], key=lambda r: r["id"]), sorted(sources["fpv"], key=lambda r: r["id"])
    matrix = np.array([[next(r["score"] for r in blind["rows"] if r["radio"] == rid["id"] and r["fpv"] == fid["id"])
                       for fid in fpv_ids] for rid in radio_ids])
    actual = {(c["radio"], c["fpv"]) for c in cases}
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.3), gridspec_kw={"width_ratios": [1, 1.1]})
    ax = axes[0]
    image = ax.imshow(matrix, cmap="Blues", vmin=0, vmax=max(.6, matrix.max()))
    ax.set_xticks(range(n), [r["id"] for r in fpv_ids])
    ax.set_yticks(range(n), [r["id"] for r in radio_ids])
    ax.set_xlabel("FPV recording")
    ax.set_ylabel("StickCam recording")
    ax.set_title(f"Blind scores: {summary['top1_correct']}/{n} correct first choices\nGreen outline = known pair; star = suggested for review")
    for i, rid in enumerate(radio_ids):
        for j, fid in enumerate(fpv_ids):
            row = next(r for r in blind["rows"] if r["radio"] == rid["id"] and r["fpv"] == fid["id"])
            ax.text(j, i, f"{matrix[i,j]:.3f}" + (" *" if row["suggested"] else ""), ha="center", va="center",
                    color="white" if matrix[i,j] > .42 else "black")
            if (rid["id"], fid["id"]) in actual:
                ax.add_patch(plt.Rectangle((j-.46, i-.46), .92, .92, fill=False, edgecolor="#16a34a", linewidth=2.5))
    fig.colorbar(image, ax=ax, shrink=.65, label="Heuristic correlation score")
    ax = axes[1]
    labels = [r["id"] for r in radio_ids]
    extracted = [by_id[r["id"]]["cold_extraction_seconds"]/60 for r in radio_ids]
    durations = [by_id[r["id"]]["duration_seconds"]/60 for r in radio_ids]
    y = np.arange(n)
    ax.barh(y-.18, durations, .34, label="Footage duration", color="#94a3b8")
    ax.barh(y+.18, extracted, .34, label="Fresh activity extraction", color="#f97316")
    for i, value in enumerate(extracted):
        ax.text(value+.035, i+.18, fmt(value*60), va="center", fontsize=9)
    ax.set_yticks(y, labels)
    ax.invert_yaxis()
    ax.set_xlabel("Minutes")
    ax.set_title("StickCam processing time · RTX 3090\n10 Hz, pre-marked radio regions, fresh algorithm cache")
    ax.legend(loc="lower right")
    fig.suptitle("Movement-only benchmark · 13 October 2026", fontsize=16, y=.99)
    fig.text(.5, .012, "Five known pairs from two sessions; four previously studied. Experimental review suggestions, not automatic confirmation.",
             ha="center", fontsize=9)
    fig.tight_layout(rect=(0, .04, 1, .94))
    fig.savefig(output / "results.png", dpi=170)
    plt.close(fig)

    # Actual pairs are revealed only in this scoring/report stage.
    fig, axes = plt.subplots(n, 1, figsize=(14, n*2.4), squeeze=False)
    for case, (ax,) in zip(cases, axes):
        traces = {}
        for key in ("radio", "fpv"):
            with np.load(output / "signals" / f"{case[key]}.npz", allow_pickle=False) as saved:
                traces[key] = {name: saved[name] for name in saved.files}
        for kind, color, shift in (("radio", "#f97316", case["estimated_offset"]), ("fpv", "#2563eb", 0)):
            data = traces[kind]
            good = data["quality"] > .1
            values = data["energy"].copy()
            scale = max(np.std(values[good]), 1e-6) if good.any() else 1
            values /= scale
            values[~good] = np.nan
            ax.plot(data["time"]-shift, np.clip(values, -4, 4), color=color, lw=.8,
                    alpha=.85, label="Gimbal activity" if kind == "radio" else "FPV angular-change rhythm")
        for window in case["sections"]["windows"]:
            if window["matched"]:
                ax.axvspan(window["fpv_start"], window["fpv_start"]+window["duration"], color="#16a34a", alpha=.12)
        fduration = by_id[case["fpv"]]["duration_seconds"]
        ax.set_xlim(0, fduration)
        ax.set_ylim(-4.5, 4.5)
        ax.set_ylabel("Relative activity")
        detail = (f"offset {case['estimated_offset']:+.1f}s · {case['sections']['matched']}/{case['sections']['usable']} sections"
                  if case['correct_pair_score'] > 0 else "No usable alignment; traces shown unshifted")
        ax.set_title(f"Case {case['case']} · {case['radio']} / {case['fpv']} · true pair rank {case['correct_rank']} · "
                     + detail, loc="left", fontsize=10)
        ax.legend(loc="upper right", fontsize=8)
    axes[-1, 0].set_xlabel("FPV video time (seconds); StickCam shifted by the matcher's estimated offset")
    fig.tight_layout()
    fig.savefig(output / "aligned-known-pairs.png", dpi=150)
    plt.close(fig)

    radio_times = [t["cold_extraction_seconds"] for t in timings if t["kind"] == "radio"]
    fpv_times = [t["cold_extraction_seconds"] for t in timings if t["kind"] == "fpv"]
    prep = sum(t["signal_preparation_seconds"] for t in timings)
    warm = sum(t["warm_cache_read_seconds"] for t in timings)
    total = sum(radio_times)+sum(fpv_times)+prep
    source_total = sum(t["duration_seconds"] for t in timings if t["kind"] == "radio")
    lines = ["# Movement-only blind benchmark", "", "Run: 13 October 2026.", "",
             "## Result", "",
             f"- Correct first-choice StickCam owners: **{summary['top1_correct']}/{n}** FPV clips.",
             f"- Correct first-choice owners when restricted to competing clips in the same session: **{summary['within_session_top1_correct']}/{n}**.",
             f"- Reverse first-choice FPV clips: **{summary['reverse_top1_correct']}/{n}** StickCam clips.",
             f"- Review suggestions passing the existing gates: **{summary['correct_suggestions']} correct, {summary['incorrect_suggestions']} incorrect**.",
             f"- Source-bound timing references recovered within 1 second: **{summary['source_bound_sync_within_1s']}/4**.",
             "- A high-ranked candidate and a safe review suggestion are different measurements. No pair was automatically confirmed.", "",
             "![Scores and processing time](results.png)", "", "## Method and limitations", "",
             "Five distinct known flights were found across Gridworks Oct 26 and Node Court, including the audio sister project's confirmations. "
             "Other sessions had no explicit pair confirmations. Four labels are source-bound; the fifth is the earlier 0228/0008 flight explicitly identified by the user. "
             "Its historical -5.6s manual offset is not a current verified timing reference.", "",
             "The extraction and matching processes are separate. The matcher reads only opaque IDs and movement arrays from `matcher-input.json`; "
             "it does not read `truth.json`, source filenames, dates, audio, saved match offsets, exact stick inputs, or rate profiles. "
             "All 25 combinations and every feasible lag were searched with the installed production method and unchanged thresholds. "
             "No duration-similarity filter or forced one-to-one assignment was applied. Results were written before scoring.", "",
             "Radio outlines and gimbal regions were pre-marked. For four clips, geometry was reused from the development study, choosing an appearance reference by prior pose coverage. "
             "The fifth uses its saved calibration. There is one reference per clip, no exact-tip extraction, 10 Hz activity sampling and explicit tracking gaps. "
             "FPV data sources: " + ", ".join(f"{t['id']}: {t['source_kind']}" for t in timings if t['kind'] == 'fpv') + ". "
             "`quaternion_rates` means angular rates derived from embedded attitude, rather than raw gyro samples. "
             "This run does not validate the visual-only fallback for Vista/analog recordings.", "",
             "**This is a blinded regression run, not an independent five-pair holdout:** four clips previously helped develop the method; "
             "only the legacy 0228/0008 pair is new to this activity benchmark. Twenty off-diagonal combinations provide competing candidates, "
             "but the small sample and cross-session distractors do not establish general accuracy. "
             "The sample has one FPV recording per known flight; split-file ownership is not exercised here.", "",
             "## Pair outcomes", "",
             "Positive offset means the matching event occurs later in StickCam: StickCam time = FPV time + offset. "
             "Matching resolution is 0.1s; control/aircraft response delay and telemetry timing are not independently calibrated.", "",
             "| Case | Session | StickCam | FPV | Correct rank | Estimated offset | Reference | Difference | Matching sections | Review suggestion |",
             "|---|---|---|---|---:|---:|---:|---:|---:|---|"]
    for c in cases:
        legacy = " (legacy)" if not c["previously_studied"] else ""
        estimate = f"{c['estimated_offset']:+.2f}s" if c['correct_pair_score'] > 0 else "No usable match"
        difference = f"{c['signed_offset_error']:+.3f}s" if c['correct_pair_score'] > 0 else "Not assessed"
        lines.append(f"| {c['case']} | {c['session']} | {c['radio_name']} | {c['fpv_name']} | {c['correct_rank']}/{n} | "
                     f"{estimate} | {c['reference_offset']:+.3f}s{legacy} | {difference} | "
                     f"{c['sections']['matched']}/{c['sections']['usable']} | {'Yes' if c['suggested'] else 'No'} |")
    lines += ["", "## Measured processing time", "",
              f"Machine: {environment['tracking'].get('device', 'CPU')}; OpenCV {environment['opencv']}, one CPU tracking thread. "
              "Automatic CUDA decoding/tracking was enabled; sparse flow and perspective crops can use CUDA, while regional dense flow and SIFT reacquisition use CPU.", "",
              f"- Five StickCam clips ({fmt(source_total)} footage): **{fmt(sum(radio_times))}** total extraction.",
              f"- Per StickCam clip: **{fmt(min(radio_times))}–{fmt(max(radio_times))}**, median **{fmt(float(np.median(radio_times)))}**.",
              f"- FPV telemetry extraction for five clips: **{fmt(sum(fpv_times))}** total.",
              f"- Fingerprint preparation: **{fmt(prep)}** total.",
              f"- Total fresh data preparation: **{fmt(total)}**.",
              f"- All 25 blind comparisons: **{fmt(blind['matching_seconds'])}**.",
              f"- Reading the ten saved raw caches again: **{fmt(warm)}** total; excludes fingerprint preparation/matching.", "",
              "These are single-run wall-clock measurements on NAS-hosted files. The algorithm cache was fresh; the operating system/NAS cache was not flushed. "
              "Manual setup time, contact-sheet inspection, and historical calibration time are excluded. No equivalent full stick-tip derivation benchmark was run, "
              "so these timings do not establish an exact speed-up factor.", "",
              "| ID | Feed | Clip | Duration | Fresh extraction | Prepared observed coverage | Warm raw-cache read |",
              "|---|---|---|---:|---:|---:|---:|"]
    for t in timings:
        lines.append(f"| {t['id']} | {t['kind']} | {by_source[t['id']]['name']} | {fmt(t['duration_seconds'])} | "
                     f"{fmt(t['cold_extraction_seconds'])} | {t['observed_percent']:.1f}% | {t['warm_cache_read_seconds']:.4f}s |")
    diagnostic = output / "diagnostic/results.json"
    if diagnostic.exists():
        d = read(diagnostic)
        actual = d['actual_pair']
        lines += ["", "## Separate post-scoring diagnostic", "",
                  "The original 4/5 result above is preserved. After revealing the results, the failed clip was inspected at 20, 120 and 180 seconds. "
                  "A new outline and two gimbal regions were manually marked on the 120s StickCam frame, using that image alone. "
                  "The algorithm and thresholds were unchanged; all five candidate FPV traces were reused. No audio or date evidence was added. "
                  "This is failure diagnosis after scoring, not an additional blind holdout.", "",
                  f"- Prepared observed coverage: {by_id[d['radio']]['observed_percent']:.1f}% → **{d['observed_percent']:.1f}%**.",
                  f"- New StickCam derivation: **{fmt(d['extraction_seconds'])}**.",
                  f"- Correct FPV rank: **{d['correct_fpv_rank']}/5**; correct StickCam-owner rank: **{d['correct_owner_rank']}/5**.",
                  f"- Correct pair score: **{actual['score']:.3f}**; suggestion passes gates: **{actual['suggested']}**.",
                  f"- Estimated offset: **{actual['offset_seconds']:+.1f}s**, with {actual['sections']['matched']}/{actual['sections']['usable']} matching sections.",
                  f"- Competing-owner margin: **{actual['candidate_separation']:.4f}** (requires at least 0.04). "
                  "This is not a successful recovery: the lead and timing evidence remain too weak."]
    lines += ["", "## Matched traces", "", "Gaps are unobserved periods. Green bands are independently checked 2–3s sections "
              "at the matcher's best offset for the actual pair. The actual pairs are revealed only after predictions are saved.", "",
              "![Aligned movement rhythms and observed gaps](aligned-known-pairs.png)", "", "## Reproduce", "", "```bash",
              ".venv/bin/python tools/benchmark_movement.py prepare artifacts/pair-validation/another-run",
              ".venv/bin/python tools/benchmark_movement.py extract artifacts/pair-validation/another-run",
              ".venv/bin/python tools/benchmark_movement.py match artifacts/pair-validation/another-run",
              ".venv/bin/python tools/benchmark_movement.py score artifacts/pair-validation/another-run",
              ".venv/bin/python tools/report_movement_benchmark.py artifacts/pair-validation/another-run", "```", "",
              "The protocol records code hashes, seed, thresholds and excluded evidence. Session pairings, calibrations, source videos and production caches were not modified. "
              "The later viewer restart refreshed session `updated_at` housekeeping timestamps; all other saved session fields were verified unchanged. "
              "`cold-cache/` is this benchmark's isolated cache. `blind-results.json` contains committed predictions; `scored-results.json` reveals outcomes. "
              "`protocol-source/` preserves the benchmark's production source files. The benchmark exposed NumPy integer event counts that could prevent strict JSON saving; "
              "a subsequent serialization fix casts those counts to ordinary integers. Replaying all 25 comparisons verifies unchanged scores, offsets and eligibility. "
              "The frozen protocol deliberately rejects replay against changed production files; prepare a new output directory for the current code.", "",
              "## Practical conclusion", "", "For these files, fresh StickCam activity extraction is about 25 seconds per minute of footage, "
              "while matching saved fingerprints is effectively immediate. The four previously studied pairs retain distinctive movement patterns and their sync offsets. "
              "The newly evaluated pair remains unresolved because usable radio tracking is sparse, even after a separate reference correction. "
              "Improve outline/gimbal pose retention and reacquisition, then validate with at least five newly confirmed flights that have not helped develop the method. "
              "Keep explicit gaps and the current abstention gates; raising a weak candidate's confidence would not repair missing observations.", ""]
    (output / "README.md").write_text("\n".join(lines))
    paths = sorted(p for p in output.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (output / "SHA256SUMS").write_text("".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(output)}\n" for p in paths))
    print(json.dumps({"report": str(output / "README.md"), "data_preparation_seconds": total,
                      "radio_extraction_seconds": sum(radio_times), "fpv_extraction_seconds": sum(fpv_times),
                      "warm_cache_seconds": warm}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    report(parser.parse_args().output.resolve())
