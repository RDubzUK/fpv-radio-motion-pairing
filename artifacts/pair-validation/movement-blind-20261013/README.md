# Movement-only blind benchmark

Run: 13 October 2026.

## Result

- Correct first-choice StickCam owners: **4/5** FPV clips.
- Correct first-choice owners when restricted to competing clips in the same session: **4/5**.
- Reverse first-choice FPV clips: **4/5** StickCam clips.
- Review suggestions passing the existing gates: **4 correct, 0 incorrect**.
- Source-bound timing references recovered within 1 second: **4/4**.
- A high-ranked candidate and a safe review suggestion are different measurements. No pair was automatically confirmed.

![Scores and processing time](results.png)

## Method and limitations

Five distinct known flights were found across Gridworks Oct 26 and Node Court, including the audio sister project's confirmations. Other sessions had no explicit pair confirmations. Four labels are source-bound; the fifth is the earlier 0228/0008 flight explicitly identified by the user. Its historical -5.6s manual offset is not a current verified timing reference.

The extraction and matching processes are separate. The matcher reads only opaque IDs and movement arrays from `matcher-input.json`; it does not read `truth.json`, source filenames, dates, audio, saved match offsets, exact stick inputs, or rate profiles. All 25 combinations and every feasible lag were searched with the installed production method and unchanged thresholds. No duration-similarity filter or forced one-to-one assignment was applied. Results were written before scoring.

Radio outlines and gimbal regions were pre-marked. For four clips, geometry was reused from the development study, choosing an appearance reference by prior pose coverage. The fifth uses its saved calibration. There is one reference per clip, no exact-tip extraction, 10 Hz activity sampling and explicit tracking gaps. FPV data sources: F01: quaternion_rates, F02: quaternion_rates, F03: quaternion_rates, F04: quaternion_rates, F05: quaternion_rates. `quaternion_rates` means angular rates derived from embedded attitude, rather than raw gyro samples. This run does not validate the visual-only fallback for Vista/analog recordings.

**This is a blinded regression run, not an independent five-pair holdout:** four clips previously helped develop the method; only the legacy 0228/0008 pair is new to this activity benchmark. Twenty off-diagonal combinations provide competing candidates, but the small sample and cross-session distractors do not establish general accuracy. The sample has one FPV recording per known flight; split-file ownership is not exercised here.

## Pair outcomes

Positive offset means the matching event occurs later in StickCam: StickCam time = FPV time + offset. Matching resolution is 0.1s; control/aircraft response delay and telemetry timing are not independently calibrated.

| Case | Session | StickCam | FPV | Correct rank | Estimated offset | Reference | Difference | Matching sections | Review suggestion |
|---|---|---|---|---:|---:|---:|---:|---:|---|
| 1 | Gridworks Oct 26 | DJI_20261011114753_0018_D.MP4 | DJI_20261011110859_0002_D.MP4 | 1/5 | +20.10s | +19.999s | +0.101s | 5/5 | Yes |
| 2 | Gridworks Oct 26 | DJI_20261011153226_0037_D.MP4 | DJI_20261011145311_0010_D.MP4 | 1/5 | +2.70s | +2.713s | -0.013s | 23/31 | Yes |
| 3 | Node Court | DJI_20260406151519_0222_D.MP4 | DJI_0003.MP4 | 1/5 | -13.10s | -13.050s | -0.050s | 15/22 | Yes |
| 4 | Node Court | DJI_20260406171042_0232_D.MP4 | DJI_20260406165630_0011_D.MP4 | 1/5 | -21.90s | -21.850s | -0.050s | 4/4 | Yes |
| 5 | Node Court | DJI_20260406162534_0228_D.MP4 | DJI_20260406161138_0008_D.MP4 | 5/5 | No usable match | -5.600s (legacy) | Not assessed | 0/0 | No |

## Measured processing time

Machine: NVIDIA GeForce RTX 3090; OpenCV 4.14.0, one CPU tracking thread. Automatic CUDA decoding/tracking was enabled; sparse flow and perspective crops can use CUDA, while regional dense flow and SIFT reacquisition use CPU.

- Five StickCam clips (20m 30.8s footage): **8m 30.6s** total extraction.
- Per StickCam clip: **1m 16.6s–2m 25.6s**, median **1m 39.7s**.
- FPV telemetry extraction for five clips: **53.36s** total.
- Fingerprint preparation: **1.80s** total.
- Total fresh data preparation: **9m 25.8s**.
- All 25 blind comparisons: **0.05s**.
- Reading the ten saved raw caches again: **0.24s** total; excludes fingerprint preparation/matching.

These are single-run wall-clock measurements on NAS-hosted files. The algorithm cache was fresh; the operating system/NAS cache was not flushed. Manual setup time, contact-sheet inspection, and historical calibration time are excluded. No equivalent full stick-tip derivation benchmark was run, so these timings do not establish an exact speed-up factor.

| ID | Feed | Clip | Duration | Fresh extraction | Prepared observed coverage | Warm raw-cache read |
|---|---|---|---:|---:|---:|---:|
| R01 | radio | DJI_20260406151519_0222_D.MP4 | 3m 27.2s | 1m 16.6s | 34.5% | 0.0032s |
| R02 | radio | DJI_20261011153226_0037_D.MP4 | 6m 5.5s | 2m 25.6s | 28.8% | 0.0052s |
| R03 | radio | DJI_20261011114753_0018_D.MP4 | 3m 32.8s | 1m 39.7s | 35.7% | 0.0037s |
| R04 | radio | DJI_20260406171042_0232_D.MP4 | 3m 27.6s | 1m 28.2s | 6.7% | 0.0024s |
| R05 | radio | DJI_20260406162534_0228_D.MP4 | 3m 57.7s | 1m 40.6s | 1.2% | 0.0035s |
| F01 | fpv | DJI_0003.MP4 | 3m 43.6s | 1.28s | 99.8% | 0.0509s |
| F02 | fpv | DJI_20260406161138_0008_D.MP4 | 4m 7.7s | 23.00s | 99.8% | 0.0582s |
| F03 | fpv | DJI_20261011145311_0010_D.MP4 | 2m 47.3s | 25.30s | 99.3% | 0.0560s |
| F04 | fpv | DJI_20261011110859_0002_D.MP4 | 15.63s | 2.48s | 96.2% | 0.0064s |
| F05 | fpv | DJI_20260406165630_0011_D.MP4 | 3m 48.5s | 1.30s | 99.8% | 0.0509s |

## Separate post-scoring diagnostic

The original 4/5 result above is preserved. After revealing the results, the failed clip was inspected at 20, 120 and 180 seconds. A new outline and two gimbal regions were manually marked on the 120s StickCam frame, using that image alone. The algorithm and thresholds were unchanged; all five candidate FPV traces were reused. No audio or date evidence was added. This is failure diagnosis after scoring, not an additional blind holdout.

- Prepared observed coverage: 1.2% → **5.9%**.
- New StickCam derivation: **1m 42.5s**.
- Correct FPV rank: **1/5**; correct StickCam-owner rank: **1/5**.
- Correct pair score: **0.354**; suggestion passes gates: **False**.
- Estimated offset: **-45.0s**, with 1/4 matching sections.
- Competing-owner margin: **0.0013** (requires at least 0.04). This is not a successful recovery: the lead and timing evidence remain too weak.

## Matched traces

Gaps are unobserved periods. Green bands are independently checked 2–3s sections at the matcher's best offset for the actual pair. The actual pairs are revealed only after predictions are saved.

![Aligned movement rhythms and observed gaps](aligned-known-pairs.png)

## Reproduce

```bash
.venv/bin/python tools/benchmark_movement.py prepare artifacts/pair-validation/another-run
.venv/bin/python tools/benchmark_movement.py extract artifacts/pair-validation/another-run
.venv/bin/python tools/benchmark_movement.py match artifacts/pair-validation/another-run
.venv/bin/python tools/benchmark_movement.py score artifacts/pair-validation/another-run
.venv/bin/python tools/report_movement_benchmark.py artifacts/pair-validation/another-run
```

The protocol records code hashes, seed, thresholds and excluded evidence. Session pairings, calibrations, source videos and production caches were not modified. The later viewer restart refreshed session `updated_at` housekeeping timestamps; all other saved session fields were verified unchanged. `cold-cache/` is this benchmark's isolated cache. `blind-results.json` contains committed predictions; `scored-results.json` reveals outcomes. `protocol-source/` preserves the benchmark's production source files. The benchmark exposed NumPy integer event counts that could prevent strict JSON saving; a subsequent serialization fix casts those counts to ordinary integers. Replaying all 25 comparisons verifies unchanged scores, offsets and eligibility. The frozen protocol deliberately rejects replay against changed production files; prepare a new output directory for the current code.

## Practical conclusion

For these files, fresh StickCam activity extraction is about 25 seconds per minute of footage, while matching saved fingerprints is effectively immediate. The four previously studied pairs retain distinctive movement patterns and their sync offsets. The newly evaluated pair remains unresolved because usable radio tracking is sparse, even after a separate reference correction. Improve outline/gimbal pose retention and reacquisition, then validate with at least five newly confirmed flights that have not helped develop the method. Keep explicit gaps and the current abstention gates; raising a weak candidate's confidence would not repair missing observations.
