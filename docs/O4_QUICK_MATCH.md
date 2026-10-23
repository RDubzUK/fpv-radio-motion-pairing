# O4 / embedded-motion Quick Match

Select **O4 Quick Match · automatic radio setup**, choose **0–3 movement spot
checks**, then **Run Quick Match**. Two other flights are checked by default.
The normal Automatic mode also chooses this route when all selected FPV files
are known to have no audio, embedded motion and tip-free gimbal activity are
enabled, and exact stick derivation is disabled. Use the dedicated preset for
a mixed recording pool if you want to try embedded movement first.

## Process

1. Read metadata and cache supported embedded gyro/attitude from the selected
   FPV pool. DJI attitude quaternions are converted to angular rates; they are
   not labelled raw gyroscope samples. Unsupported and too-short traces are
   skipped. The dedicated route never derives visual FPV motion or extracts audio.
2. Prepare one StickCam at a time. Reuse saved regions and movement where possible.
   Otherwise inspect the midpoint and opening frames at five-second intervals,
   looking for visible gimbal rims and a radio body. A known radio model only
   assists detection. Exact tips, stick Mode, and rates are not required.
3. Derive tip-free activity at up to 10 Hz. Lost radio poses remain gaps. If the
   clip has fewer than 12 usable seconds of varying activity, try the next clip.
4. Compare that clip against every available embedded FPV trace, across all
   feasible offsets. Stop on a distinct, well-supported seed. Current seed gates
   require a score of at least 0.45, timing-peak margin of 0.08, three matching
   independent sections, 60% section agreement, and separation of 0.06 from
   competing FPV recordings. Several independently supported, non-overlapping
   O4 parts with a consistent clock delta may corroborate the same flight.
5. Calculate a **provisional modified-date clock** from that seed. Convert
   recording end dates into start dates using each file's duration when the
   session's modified-date setting is “end”. Intersect timelines to suggest
   other pairs and O4 split parts; recording-length similarity never scores a
   match. Constant/repeated dates are excluded.
6. Optionally prepare up to three other StickCam flights and search their
   movement independently against the FPV pool. Report agreement, disagreement,
   a different content match, or insufficient evidence. Distinct unassigned
   clock offsets are separate possibilities; contradictory offsets from a
   named recorder disable that provisional clock's date suggestions.
7. Present the content seed, spot-check outcomes and date-only suggestions for
   review. Preview both feeds, refine sync and independently confirm shared
   content before exporting or teaching the confirmed camera clocks.

## Saved data and review

Automatically observed radio regions are saved as **Automatic regions · review**,
not confirmed full stick calibrations. Review them through the normal shared
calibration viewer. Correcting regions invalidates only the corresponding
fingerprint. Compatible activity and telemetry caches survive later matching runs.
Progress includes the current clip, extraction percentage/ETA, clips attempted,
and spot-check stage. A preparation log records observed seconds, elapsed time,
cache reuse and skipped clips.

The file sample percentage is disabled for this route: it searches the selected
FPV pool, then stops after the first distinct seed and the requested spot checks.
Use **Video lists & match scope** to restrict the actual target videos.

Provisional dates are kept separate from independently confirmed camera-clock
models. They cannot confirm themselves, enable exports, or silently overwrite a
saved alignment. A rejected, reassigned or changed seed invalidates its provisional
suggestions. Once a consistent, independently confirmed clock is available, it
supersedes that seed's provisional predictions.

## Limits

- A clear radio appearance does not guarantee enough reliable tracking. Poor
  clips are skipped rather than filled with inferred stick observations.
- The seed thresholds are experimental heuristics, not probabilities. Human
  review remains necessary, especially if only one StickCam was analysed.
- This route requires supported embedded motion. Use the normal visual fallback
  workflow for cameras/DVRs without telemetry.
- One seed cannot establish every drone's clock. Recorder labels and independent
  confirmations improve separation; spot checks can expose another clock group.
- Movement timing is coarse (0.1s plus uncalibrated response/timestamp delays).
  Date predictions can be several seconds off. Refine before frame-sensitive edits.

The earlier [five-pair benchmark](../artifacts/pair-validation/movement-blind-20261013/README.md)
measured the underlying fingerprint method with pre-marked regions. It did not
measure this automatic calibration/seeding workflow or establish its success rate.
