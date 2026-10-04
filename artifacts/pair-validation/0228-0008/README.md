# Known flight pair: StickCam 0228 / FPV 0008

## Conclusion

The supplied pair is strongly supported by repeated audio fingerprints. The
existing extracted stick signals do not reliably support the pairing. That is
an extraction failure, not evidence that these recordings are different flights.

- StickCam: `DJI_20260406162534_0228_D.MP4`
- FPV: `DJI_20260406161138_0008_D.MP4`
- Session: `5a76c58c0100` (Node Court)

The user identified this as a true pair. That identification was not used to
restrict the audio offset search or to increase any measured score.

## Independent audio timing

| Measurement | Result |
| --- | --- |
| Best offset, StickCam time minus FPV time | −5.60 s |
| Distinct shared hashes at that offset | 769 |
| Supported non-overlapping three-second sections | 15 |
| Examined sections | 16; one was not usable |
| First-boundary-only search | −5.60 s; 565 distinct hashes |
| Last-boundary-only search | −5.55 s; 223 distinct hashes |
| Median agreeing votes, first / last boundary | −5.600 / −5.593 s |
| Other cached FPV candidates examined | 29 |
| Strongest other candidate trend score | 0.526; no accepted fingerprint match |
| This pair's fingerprint score | 0.974 |

Scores are heuristic evidence strengths, not probabilities. Hash counts from
the two boundaries can overlap in frequency signatures; do not add them as if
they were all different. The fingerprint hop is 50 ms. The agreement near both
ends supports a constant offset at that resolution; it does not establish
sample-accurate synchronisation or exclude small clock drift.

Only the first and last 30 seconds of each recording are analysed for matching
audio. The middle of the flight is not an additional audio confirmation.

## Common interval

Align FPV **00:05.60** with StickCam **00:00.00**.

| Source | Start | End | Shared duration |
| --- | --- | --- | --- |
| StickCam | 00:00.000 | 03:57.671 | 03:57.671 |
| FPV | 00:05.600 | 04:03.271 | 03:57.671 |

The clip lengths were used only for these overlap boundaries, not to identify
the pair. No source footage or session match confirmation was modified.

## What disagrees in the existing motion extraction

At the exact audio offset, the existing detector finds only **3 / 50 usable
three-second sections** with correlation of at least 0.6 on two axes, using
axis polarities fixed across the overlap. These sections are StickCam
66–69 s, 183–186 s, and 216–219 s. Moving the offset by one video sample changes
the count to two. These isolated similarities do not establish a stable match,
especially given the observed tracking error.

The unconstrained motion search prefers an incorrect offset around −238.75 s,
with only nine seconds of overlap and a weak score of 0.221. Its winning
local peak should not be mistaken for evidence for that offset. Another FPV
clip scores 0.295, also without enough repeated section agreement.

Changing the camera uptilt from 0° to 30° or 45° leaves the score near the audio
offset around 0.05. Applying the saved KISS rate curve also does not rescue the
comparison. Cross-axis checks of the existing data likewise find no consistent
whole-flight relationship. This does not establish that the current sensor
mapping is correct; it shows that a simple rate or axis setting cannot repair
these particular extracted signals.

### Confirmed capture fault: housing drift

![Saved outline drift](tracking-diagnostic.png)

At the 20-second calibration frame the outline covers the radio. By 120 seconds
it covers grass beside the radio, yet the reported outline quality is 0.90.
The estimated outline extends outside the image during 67.4% of the clip, and
does so while claiming quality ≥0.5 during 63.3% of the clip.

The current housing tracker accumulates transforms from frame-to-frame optical
flow. It checks the latest step's scale and inliers, but does not independently
confirm that the accumulated transform still describes the original radio.
Features can drift onto hands or the background and continue to agree with
each other. High feature agreement is therefore not the same as correct radio
tracking. Stick coordinates derived in that misplaced frame are unreliable.

### Additional capture weaknesses identified in the code

- The outline transform is a similarity transform. A moving camera changes the
  radio's perspective, which can alter the two gimbal locations differently.
- Tip template searches cover the complete housing crop. Matches need not be
  inside the calibrated travel region, and the best candidate is accepted
  without requiring a margin over other plausible candidates.
- A dark-component fallback can select black housing parts when a tip is
  obscured. Its confidence can still exceed the matching validity threshold.
- The two gimbal regions share a housing quality estimate; that does not
  independently verify either tip, particularly during pinching or thumbing.

These are code-supported failure mechanisms. Housing drift is directly shown
in this recording; the exact contribution of each tip failure needs separate
visual validation.

### FPV source qualification

This FPV clip provides embedded DJI Osmo Nano **attitude quaternions**. The
current parser path derives angular rates from them; it does not find a raw
gyroscope sample stream. The study reuses the cached 247,373 attitude-derived
rate samples. Sensor mapping, camera mount angle, timestamp alignment and any
camera attitude filtering still need validation. Gravity or throttle dynamics
are not used as evidence in this comparison.

## Direction for correction

1. Anchor the radio against its original appearance at intervals; use optical
   flow between anchors and perspective rectification before reading controls.
2. Reject an outline that has implausible shape, size or frame position; mark
   loss explicitly and reacquire the radio rather than continuing on background.
3. Limit tip candidates to physical travel and require distinctive appearance;
   mark covered tips as unobserved instead of guessing their positions.
4. Use the audio offset as a diagnostic reference while checking body axes and
   small controller/camera delays. Assess one fixed mapping on separate portions
   of the flight, rather than tuning each three-second section independently.
5. Count repeated multi-axis agreement only in validated tracking intervals.
   Keep strong audio proposals usable when motion capture fails.

### Absolute-reference experiment

`rectified_study.py` evaluated direct matching to the original radio appearance
and perspective rectification, independently on each sampled frame. It used
5 Hz at 800 pixels to isolate accumulated housing drift. Both the original
and experimental signals were compared at the same 5 Hz rate.

| At the audio alignment | Existing capture | Reference rectification |
| --- | --- | --- |
| Motion score | 0.046 | 0.138 |
| Accepted roll/pitch samples | 87.0% | 20.2% |
| Accepted yaw samples | 90.4% | 23.4% |
| Repeated supported sections | 4 / 51 | 0 / 7 |

The experiment took 403.5 seconds. The modest score improvement does not
establish a successful motion matcher. A single reference fails to reacquire
the housing in many views, and tip appearance/tracking still needs correction.
For example, the experimental roll correlation changes sign between the first
and second halves of the recording. No consistent whole-flight relationship
emerges from a simple axis swap.

This experiment is not a replacement for the 30 Hz production extractor. It
does not update the saved calibration or cached production movement data.
An efficient production approach should use sparse appearance anchors, flow
between them, multiple validated views, and stricter tip validity checks.

## Artifacts and reproduction

- `results.json`: measurements and complete candidate rankings.
- `cross-axis.json`: supplementary axis checks of existing extracted data.
- `evidence.png`: boundary audio support and disagreeing motion traces.
- `tracking-diagnostic.png`: outline overlays from the cached extraction.
- `rectified-results.json`: equal-rate comparison of existing and experimental capture.
- `rectified-comparison.png`: before/after diagnostic traces with unobserved intervals omitted.
- `frontend-plot-order.png`: updated motion/motion/audio/audio layout.
- `analyze.py`, `plot.py`, `rectified_study.py`: reproducible diagnostic scripts.

Run from the project directory with its virtual environment. The scripts read
the named session and media paths. Missing inexpensive audio is saved in this
study's local cache. The production motion-review and audio-review tasks also
saved this pair's signals in the session's normal shared caches.
