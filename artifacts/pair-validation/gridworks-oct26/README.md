# Confirmed-flight content study — 13 October 2026

## Result

There is a useful **movement fingerprint** in this material even when a stick
tip is hidden: motion of the fingers/stick assembly inside a stabilised gimbal
region can be compared with changes in the FPV camera's angular velocity.

This experimental, tip-free rhythm method independently recovered the timing
of all four supplied reference pairs. Each correct recording ranked first
against 12 candidate FPV recordings: the four references plus eight recordings
sampled evenly from the Gridworks source list. One of those eight is only 7.28
seconds long and cannot meet this experiment's eight-second evidence minimum.
Other unconfirmed recordings are competing candidates, not assumed negatives;
some could be split parts of a flight.

This is a promising prototype, **not a validated automatic confirmer**. The
reference geometry was marked by inspecting the radio images. The sample is
small, tracking coverage remains uneven, and the fourth pair has little usable
motion. The app's matching behaviour and existing confirmations were not changed.

![Content fingerprints](content-fingerprints.png)

## Reference pairs and independent timing

Positive offset means StickCam time = FPV time + offset.

| Pair | Project | FPV | StickCam | Saved offset | Content offset | Agreeing / usable sections | Observed motion support |
|---|---|---|---|---:|---:|---:|---:|
| 1 | Gridworks Oct 26 | `DJI_20261011110859_0002_D.MP4` | `DJI_20261011114753_0018_D.MP4` | +19.999202 s | +20.1 s | 5 / 5 | 14.9 s |
| 2 | Gridworks Oct 26 | `DJI_20261011145311_0010_D.MP4` | `DJI_20261011153226_0037_D.MP4` | +2.713344 s | +2.7 s | 17 / 25 | 96.0 s |
| 3 | Audio project, Node Court | `DJI_0003.MP4` | `DJI_20260406151519_0222_D.MP4` | −13.050 s | −13.1 s | 13 / 19 | 62.2 s |
| 4 | Audio project, Node Court | `DJI_20260406165630_0011_D.MP4` | `DJI_20260406171042_0232_D.MP4` | −21.850 s | −21.9 s | 2 / 3 | 13.9 s |

Sections do not overlap, are normally three seconds long, require at least 70%
observed samples and correlation ≥0.6. A final section may be between two and
three seconds. Missing or inactive sections do not count as matches. These
section counts corroborate one fixed timing estimate; they are not independent
statistical trials or calibrated probabilities.

The search runs at 10 Hz. Its estimates are **not frame-accurate**. Stick command
and measured drone response can also have a physical/processing delay. The
audio-project documents contain saved confirmations and offsets, but no stored
frame checkpoints for these two pairs. Pair 1 has a checkpoint whose implied
offset is 19.799 seconds, 0.200 seconds away from its current saved alignment.
Nothing was changed to resolve that discrepancy; it is another reason to avoid
claiming exact ground-truth sync from these labels.

### Competing candidates

The fixed rhythm formula uses no filename, timestamp, date-derived window,
saved offset or comparison of clip lengths. It searches every feasible lag and
uses the amount of genuinely observed overlap to discount short coincidences.

| Pair | Rhythm correlation at best lag | Heuristic score | Strongest competing score | Margin | Competing agreeing / usable sections |
|---|---:|---:|---:|---:|---:|
| 1 | 0.783 | 0.552 | 0.385 | 0.167 | 2 / 4 |
| 2 | 0.477 | 0.477 | 0.428 | 0.049 | 3 / 9 |
| 3 | 0.531 | 0.531 | 0.335 | 0.196 | 2 / 11 |
| 4 | 0.711 | 0.484 | 0.390 | 0.095 | 1 / 3 |

Pair 2 has a close global-score rival. Its repeated local agreement is more
useful than its headline score alone. Pair 4 only supplies two supported
sections and should remain a review suggestion. None of these scores should be
presented as a percentage chance of correctness. Existing production thresholds
cannot simply be reused for this new signal.

## What was extracted

### FPV telemetry

All four FPV references have embedded **attitude quaternions**. The parser did
not expose a raw gyroscope stream. The two Gridworks recordings identify as DJI
O4P and supply approximately 31,233 and 334,352 derived angular-rate samples.
The other references supply 223,150 and 228,206 samples. Derived rates retain
the parser's camera basis; mounting/uptilt is not assumed to be calibrated.

Quaternions avoid an upright-horizon assumption. Angular velocity is derived
from consecutive relative rotations. This study's rhythm fingerprint uses the
magnitude of angular-rate changes, so it does not require choosing roll/pitch/
yaw signs or knowing which way up the drone is. It does not infer gravity,
metric translation, thrust or throttle from rotation.

The O4 streams repeat approximately half of their quaternion samples. Direct
sample-by-sample differentiation consequently alternates near-zero values and
larger steps. Low-pass filtering before rate reduction is necessary. The app
already performs smoothing during matching resampling; these repetitions do
not prove that its current matcher omits that step. High-frequency vibration
measurements from this fused attitude stream must be treated cautiously.

As a source sanity check, image-derived rotation on the short O4 clip was
compared with its telemetry. A fixed physical rotation was fitted on the first
eight seconds and checked on the remainder. Two image-camera axes correlated
at 0.963 and 0.886; the third was weaker at 0.448. Image-derived rotation was
available over 94% of this particular study extraction. This supports useful
telemetry timing and a fixed basis relation on this clip, without establishing
accurate drone body rates, lens calibration, or general visual-motion accuracy.

The quaternion/raw-IMU distinction also agrees with the
[Gyroflow DJI documentation](https://raw.githubusercontent.com/gyroflow/docs.gyroflow.xyz/main/getting-started/supported-cameras/dji.md).
Stick commands in Acro describe requested angular rates, as explained in the
[Betaflight PID guide](https://betaflight.com/docs/wiki/guides/current/PID-Tuning-Guide).
The new rhythm measurement is a timing proxy, not a replacement for that model.

### Tip-free StickCam activity — new useful indicator

1. Mark a visible radio face and both physical gimbal regions in a study frame.
2. Locate the same radio face through source-relative feature correspondences,
   perspective geometry and appearance checks. Reject lost poses explicitly.
3. Warp each gimbal into its reference coordinates, cancelling radio movement.
4. Measure optical-flow magnitude inside its central region. Fingers and the
   stick assembly can supply movement even when the exact stick tip is covered.
5. Combine both gimbals into a motion-activity trace, preserving gaps and
   rejecting transitions into/out of observed poses.
6. Remove slow trends, compare that trace with FPV angular-rate-change activity
   over all lags, then examine separate local sections at the winning lag.

A static patch on the radio was measured as a control. At the saved timings its
correlations were −0.027, 0.170, 0.250 and 0.138, below the corresponding gimbal
activity relationships. This helps distinguish control-area movement from a
signature driven entirely by movement of the camera/radio. It does not exclude
all lighting, perspective or hand-related confounders.

### Foreground-centroid axis proxies — useful, insufficiently stable

Bright/dark foreground centroids inside the stabilised gimbals were also
measured. Fixed choices discovered on pair 1 were then applied to every other
candidate without fitting a new axis sign per recording or offset.

They recover pair 1 at +20.1 s, pair 3 at −13.1 s and pair 4 at −21.8 s. Pair 1
has approximately 0.82/0.82/0.85 correlation across the three proxy axes and five
supported sections. It beat all 100 temporally shuffled, two-second-block
controls in this exploratory comparison. Feature selection used this pair, so
that result must not be interpreted as an unbiased significance estimate.

The same fixed axis proxies **fail across the longer, better-tracked portion of
pair 2**. Its early reference produced a correct match over about 11.5 seconds,
but the more representative reference does not produce stable whole-flight
axis values. Appearance, grip, perspective and camera/body mapping remain
possible contributors. Thus these centroids should not be labelled detected
stick positions, expected pitch/roll/yaw, or calibrated degrees/second.

The unsigned movement rhythm survives this failure and recovers pair 2 at
+2.7 seconds over 96 seconds of observed support.

## The main tracking problem exposed

An early, sharp, apparently clear radio frame is not necessarily representative
of how it looks during flight. On pair 2, using the two-second reference gave
only **3.5%** observed face coverage over the whole StickCam clip. A reference
at 53.647 seconds gave **31.6%** whole-clip coverage, largely within the flight.
The radio is absent for much of the recording after the flight; those gaps
should remain absent. Within the shared flight interval, face coverage grew
from approximately 7% to 69%.

Pair 4 improved from 7.1% to 12.0% whole-clip coverage after changing to an
in-flight reference, but that is still inadequate for full input reconstruction.
These are observed-pose coverage figures, not stick-tracking success rates.

This argues for a **bank of representative radio appearances**, not relying on
one initial frame or relaxing rejection until background features are accepted.
Any reference changes must also keep a common physical gimbal coordinate system;
adding more pose coverage alone does not guarantee accurate stick inputs.

## Other indicators investigated or considered

| Indicator | Outcome in this study |
|---|---|
| Stabilised gimbal/finger activity versus FPV rate-change rhythm | Promising; recovered all four reference timings |
| Foreground centroids versus named angular-rate axes | Works on three references, fails consistently on one; diagnostic only |
| StickCam sound energy/spectral changes versus FPV rotation or attitude-derived vibration | Tested; incorrect lags/candidates often score higher; no reliable match |
| Audio spectral fingerprints | Positive control on the audio-project pairs; independently found −13.05 s and −21.80 s, with 1,002 and 1,387 shared hashes respectively |
| Shared visual scenery | Contact frames show common venues; this could narrow candidates, but repeated flights at one venue prevent treating it as flight identity |
| Take-off, landing, disarm, crashes and long pauses | Worth adding as event-level corroboration; no automatic event detector was validated here |
| OSD timers/arm states or transmitter-screen channel bars | Potentially valuable where visible; not extracted or established in this footage |
| Lens, camera model, recording mode, serial/provenance metadata | Useful for recorder groups and handling different drone clocks; not independent flight identity |
| Audio motor harmonics versus FPV vibration frequency trajectories | More specialised follow-up possibility; the tested coarse band/dynamics version failed and fused quaternions limit vibration interpretation |

The two Gridworks FPV clips have no audio. Their successful rhythm matches
therefore are not repetitions of an audio-fingerprint result.

## Recommended production direction

1. Add a separate, experimental **Gimbal activity rhythm** identifier. Keep it
   distinct from actual StickCam control reconstruction and expected body rates.
2. Extract it through the shared, validated radio-pose track, using several
   representative appearances. Cache it independently of tip positions/rates.
3. Prefer embedded attitude/gyro for the FPV rhythm. Use quality-qualified
   visual angular motion only when telemetry is unavailable; retain its gaps.
4. Rank with repeated active sections at one common lag, temporal spread and
   competing-candidate margin. Short coincidences and close rivals need review.
5. Use a coarse pass to propose pairings, then examine only promising windows
   at native StickCam rate to refine timing. Check shared visible events before
   claiming frame alignment or teaching a camera clock from motion alone.
6. Validate on additional labelled sessions, pilots, grips, inverted cameras,
   radio models and known nonmatches, with held-out recordings. This four-pair
   result does not establish a general false-match rate.

Accurate commanded pitch/roll/yaw remains a separate objective requiring
observable stick positions, calibrated travel/neutral positions, Mode/rates,
fixed physical signs and verified FPV camera/body axes.

## Isolation and reproduction

Source video, live sessions, calibration, saved pairings, production caches,
exports and the audio-only project were read only. Derived data and diagnostics
are under this study directory. No server restart or live matcher update was
made. GPU video decoding was used for the gimbal study; geometry/flow in this
runtime used OpenCV on the CPU.

Run from the project root with its existing virtual environment. Source paths
must still be available. The source signatures are recorded in `reference.json`.
The hand-marked research geometries are in `tools/study_gimbal_activity.py`.

```bash
.venv/bin/python tools/study_confirmed_pairs.py \
  --session /home/rdubz/.fpv-radio-motion-pairing/jobs/d378d9eb18ba/job.json \
  --audio-session /home/rdubz/vscode_projects/fpv-audio-pairing/data/sessions/e52f32b990524b1cb117943f2c2b0bc8/session.json \
  --output artifacts/pair-validation/gridworks-oct26
.venv/bin/python tools/study_content_signals.py artifacts/pair-validation/gridworks-oct26
.venv/bin/python tools/study_gimbal_activity.py artifacts/pair-validation/gridworks-oct26
.venv/bin/python tools/study_gimbal_activity.py artifacts/pair-validation/gridworks-oct26 --pairs 2 4 --variant alternate
.venv/bin/python tools/evaluate_content_study.py artifacts/pair-validation/gridworks-oct26 --audio-controls
.venv/bin/python tools/study_common_lag.py artifacts/pair-validation/gridworks-oct26
.venv/bin/python tools/study_activity_match.py artifacts/pair-validation/gridworks-oct26
.venv/bin/python tools/study_extra_candidates.py artifacts/pair-validation/gridworks-oct26 \
  --session /home/rdubz/.fpv-radio-motion-pairing/jobs/d378d9eb18ba/job.json
.venv/bin/python tools/study_visual_telemetry.py artifacts/pair-validation/gridworks-oct26
.venv/bin/python tools/report_content_study.py artifacts/pair-validation/gridworks-oct26
```

Principal outputs: `summary.json`, `activity-matches.json`, `common-lag.json`,
`extra-candidates.json`, `cross-modal.json`, `audio-controls.json`,
`visual-telemetry.json`, contact sheets and rectified gimbal sheets. NPZ files
retain reusable source-relative traces. Diagnostic media/data are ignored by
Git; the report and analysis tools can be versioned.
