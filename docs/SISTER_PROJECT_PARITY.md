# Flight Sync improvements carried into Flight Matcher

Reviewed the live `fpv-audio-pairing` sister project on 8 October 2026, including its v0.1.1–v0.4.0 changes and the split-flight interface update. Flight Matcher remains the separate motion-capable application; neither application imports the other's runtime modules or shares session storage.

## Confirmed pairs teach relative camera dates

After independently checking shared footage and saving its alignment, Flight Matcher immediately refreshes suggestions across the session. This reads saved metadata; it does not require another audio scan or StickCam derivation.

The saved offset convention is `StickCam source seconds − FPV source seconds`. The learned camera difference is:

```text
camera difference = StickCam estimated recording-start date
                  − FPV estimated recording-start date
                  + verified sync offset
new sync estimate = camera difference + FPV start date − StickCam start date
```

Modified dates default to recording completion, so each duration is subtracted to estimate recording start. **Confirmed camera dates** lets the user choose recording-start interpretation, set modified-date tolerance (default ±5s), and include/exclude filename clocks. The UI shows raw modified-date differences, supporting filenames, predicted offsets, agreement/disagreement with independently measured alignment, and independent flight counts. A single flight stays tentative; several FPV parts of that flight do not count as independent anchors.

A 1970/default date can still be a useful relative clock. Repeated readings within a feed and conflicting anchors disable the affected clock's predictions. Copy/relink assistance retains the recorded camera dates when the bounded source identity agrees. Date disagreements do not remove audio or motion evidence. Duration only determines which intervals overlap; equal video lengths are never required.

Suggestions cover other flights and one-to-many FPV parts, including O4 split files. They are proposals for review, not confirmed pairs. Confirming a date or rhythm proposal without explicitly checking shared content cannot teach another date anchor. Reopening and saving that proposal again does not bypass the verification requirement.

## Parity audit

| Sister-project improvement | Flight Matcher status |
| --- | --- |
| Distinctive spectral hashes plus independent audio trends at one common offset | Core evidence algorithm was already identical apart from package imports. Existing section/owner gates retained. |
| Adaptive boundary audio retries | Added 30/60/120/300s choices, unresolved-only retries, strongest evidence retained per pair, saved source-bound comparison checkpoints. Default initial 30s and retry cap 120s. |
| Seed search samples one feed against the full opposite feed | Corrected. Targeted counterpart search still checks the full available StickCam list. |
| Prior results survive a narrower search | Added source-current candidate merge outside the new comparison scope. New results own their searched scope; human decisions remain source-bound. |
| Saved confirmations refresh modified-date evidence | Added on session/results load and confirmation, with user-controlled start/end interpretation and tolerance. |
| Independent content required for date anchors | Added explicit verification for date and rhythm proposals; conflicts/duplicate dates cannot manufacture matches. |
| Durable review queue | Added later/rejected/no-counterpart decisions, next review, filters and summary counts. |
| Independent sync checkpoints | Added separately entered source event times, offset/drift/coverage checks and explicit export-warning acknowledgement. No automatic time stretching. |
| Split flight review | Added a StickCam selector, non-overlapping proposed timeline, continuous StickCam preview through gaps, FPV part switching and alternative review. Conflicting split confirmations and grouped exports are blocked, allowing one source frame of rounding. |
| Preview buffering and hardware choice | Ported six-chunk read-ahead, playback intent during buffering, seek-only aborts, refill handling, Auto/CPU cache separation and optional NVIDIA/VAAPI/D3D11/VideoToolbox decode with CPU fallback. Uses on-demand fragments, not mandatory whole-video proxies. |
| Native-rate and fast exports | Added original source cadence as default, stream-copy MP4 cuts, edit-list end bounds, accurate H.264/DNxHR alternatives, disabled synthetic timecode tracks and checked output durations. Dynamic stabilization/portrait composition still require explicit encoding/frame rate. |
| Export persistence and usability | Added destination browsing/typed folders, readable non-overwriting directories, independent export index, progress/ETA, checked completed-pair resume, optional decoded cut samples and flight manifests. Existing exports/downloads survive frontend session deletion. |
| Portable projects | Added project JSON or bounded 128MiB fingerprint bundles, source head/tail identities, relinking/cache rebinding, original camera-date retention, active calibrations, radio reference and portrait layouts. Original videos and export history are excluded. This identity is not a full-file integrity hash. |
| Metadata reliability | Added ten automatic backups, explicit corrupt-JSON recovery, bounded Windows sharing-violation retries, preserved pending saves and visible unsaved stopped-task status. Workers are queued only after initial state is saved. |
| Startup/install reliability | Added explicit JS/CSS MIME types, visible import failure reporting, safe optional localStorage, an instance lock, local diagnostics and Windows source launchers. Existing Linux launcher preserves the installed custom CUDA environment. |
| Known-answer audio validation | Added source-bound match/nonmatch reference labels, optional independently measured offsets and a separate audio validation report. A report is evidence about labelled clips, not a calibrated probability. |
| Simple/Expert interface | Main app already has a guided workspace and remembered Technical tools preference. Motion/calibration tools remain available on demand instead of adopting the sister project's audio-only workflow. |
| Browser-only sister edition | Not applicable to this local FFmpeg/telemetry/tracking application. |

## Additional movement-rhythm shortlist

Requested during this review: an optional **Try movement rhythm for unresolved clips** checkbox. This compares unsigned change envelopes and bursts from saved normalized StickCam observations with changes in FPV angular rates, or experimental image motion. It ignores axis names/signs and does not need a rate profile. It runs at up to 10Hz, reuses the existing movement caches and reports independent three-second sections, event counts, timing-peak separation and competing-candidate separation.

Rhythm does not enter the normal assignment/clock matrices and cannot create a “strong” axis match. It proposes review candidates only. Gaps, static data and unstable observation availability are excluded. Radio-relative pose is still needed to distinguish a moving radio/camera from actual stick movement; this option does not provide a new uncalibrated movement detector. Throttle activity, similar flying styles, tracking errors and drone response delay can create disagreement or false similarities. The offset is coarse and must be refined against visible events. No real-footage accuracy or speed gain is claimed.

## Verification and remaining acceptance

Disposable generated recordings exercised unequal durations, independent anchors, conflicting/duplicate/default dates, split suggestions, one-feed seed coverage, audio comparison reuse, adaptive retries, source-bound review, sync checkpoints, project round trips, moved-file NPZ rebinding, metadata backups and unsaved failure-state reporting. Actual FFmpeg exports passed stream-copy native cadence, accurately encoded native cadence and 30fps conversion timing checks. CPU on-demand preview fragments were generated. Simulated movement signals recovered a known 17.3s offset with swapped/reversed axes; static, all-gap and repetitive signals did not qualify.

These checks are separate from proving performance/accuracy on the user's real recordings. Windows/macOS, cross-vendor GPU drivers, Resolve edit-list acceptance, real batch throughput, tracking accuracy and motion/gyro sign calibration still require measured acceptance. The standard Windows build does not include CUDA OpenCV; Rust remains a source-build prerequisite for the main app's telemetry parser.
