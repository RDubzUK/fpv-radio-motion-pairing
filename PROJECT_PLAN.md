# Project plan after review

## The intended product

A local tool for a pilot with roughly 10–40 FPV/StickCam pairs, usually 1080p–4K. It should identify corresponding recordings despite different starts, durations and camera clocks; show the evidence and shared interval; accept corrections; and create already-aligned clips for an edit timeline.

The intended flight footage includes fast acro freestyle, inverted flight, full flips/rolls and combined-axis manoeuvres. The current image-motion proxies do not establish reliable matching for that workload. Embedded telemetry now supplies body-rotation matching with explicit sensor/mount settings. The next core engine phase is evaluation and calibrated visual fallback, specified in [FREESTYLE_MOTION_DESIGN.md](FREESTYLE_MOTION_DESIGN.md), including camera calibration/mount orientation, frame-rate handling, response-delay uncertainty and a separately gated gravity/throttle model.

The original motion-pairing specification is design input. Later requests add the web/LAN workflow, audio evidence, filename clock inference, moving-radio support, a pair viewer and Resolve exports. Those requests define the current user-facing scope.

## Delivered in this review

| Need | Implementation |
| --- | --- |
| Standalone engine and CLI | Reusable batch pipeline; installed-module worker; estimator interface |
| Web frontend on LAN | Navy/purple/cyan workspace; managed `0.0.0.0:8767` service |
| Select server media folders | Independent FPV and StickCam folder browsers show supported video files and shortcuts to mounted SMB shares |
| Large input batches | Local paths, resumable chunk uploads, persistent sessions and bounded workers |
| Session organization | Persisted session names can be changed from the Sessions panel |
| Fixed and Wonky StickCam | Per-recording calibration, selected-tip tracking, housing compensation and visible tracking checks |
| Calibration assistance | Reuses a confirmed radio face/outline and housing landmarks from up to three saved calibrations in the session when distributed feature matches support a perspective transform. Otherwise requires visible paired housing rims; a marked outline constrains the search. Proposes current tip positions using reference appearances or distinct visible caps. All markers remain editable; ambiguous or covered tips require manual selection |
| Radio body orientation | Optional four-corner body outline tracking supplies an orientation transform for gimbal motion and a visible tracking quality trace |
| Unequal sampling and tracking gaps | Timestamp-aware sampling/resampling, per-axis quality, invalid-gap preservation and motion rates |
| Motion signatures | Prefer embedded FPV gyro or quaternion-derived 3D rates, sensor mapping and camera uptilt; compare against stick positions/rate requests. Approximate visual motion is the fallback. Gravity/throttle scoring remains planned |
| Reusable rates | Named YAML presets in the project, web save/load controls, the YAML default preloaded into new sessions, and CLI selection |
| Extraction feedback | Per-clip percentage and observed-speed ETA during stick/visual extraction, plus batch and chart stage progress |
| Portrait pair editor | Reviewed pairs open a persistent 9:16 layout with 60/40 split, independent transforms, trim/audio controls, blurred or solid fill, and combined MP4 export |
| Interactive motion comparison | Stacked FPV/StickCam signals, source selection, optional quaternion plots, shared cursor/zoom/pan and exploratory timing shift; cached extraction and bounded peak-preserving payloads |
| Centred StickCam preview | Tracked radio outline centres/levels/fits the shell; optional camera gyro supports short gaps only after outline agreement. Tracking loss shows the full frame |
| Optional shared audio | First/last boundary trends plus spectral landmarks; separate audio evidence |
| Select matching data | Six independent source toggles, presets, dependency hints, persisted run settings; disabled evidence skips extraction and comparison while retaining caches |
| Fast paired playback | On-demand 480p H.264/AAC fragments around the requested source time; two concurrent encodes, bounded nearby browser buffers, shared source clocks, cancellation on large seeks and an evicted preview cache |
| Audio/motion evidence display | Optional cached full-recording waveforms on the shared timeline; independent green bands for agreeing motion sections and agreeing boundary-audio sections |
| Prepare StickCam movement data | Derive all calibrated recordings before matching; shared persistent raw-input cache, per-video saved/pending status, sample count/time and derivation log |
| FPV rates and sensitivity | Optional per-axis Betaflight ACTUAL, legacy, or KISS curve mapping, with overall sensitivity normalization documented |
| Wrong camera dates/times | Relative readings, including epoch dates, and consensus clock differences from distinctive pairs |
| Focused matching | Analyze all recordings, one StickCam against the FPV set, or one FPV against the StickCam set; optional unconfigured StickCam matching uses boundary audio and a tentative learned filename-clock offset |
| Ambiguous or missing matches | Unmatched assignment options, candidate and timing separation, conservative proposal threshold |
| Review and correction | Candidate selection, linked preview, shared-interval bars, frame nudges and saved one-to-one corrections |
| Resolve export | Equal CFR frame counts, individual or reviewed-batch exports, MP4 and DNxHR/PCM choices, alignment manifests |
| Long-running operation | Durable job status, cancellation, safe reruns, cached extraction and restart recovery |
| Reviewable code | Separated core/media/web modules, readable frontend files, dependency lock and documentation |

## Acceptance criteria implemented

- A poor candidate is allowed to remain unmatched.
- Similar recording lengths do not increase a pairing score. Differing starts/ends and partial overlaps are supported; minimum evidence is measured in common seconds, not a fraction of recording length.
- Motion confidence exposes agreement counts for non-overlapping 3-second active sections. Strong proposals require sustained support spread across the overlap, as well as separation from competing offsets and clips. Agreement percentages are not presented as calibrated match probabilities.
- Stacked motion charts have an adjacent synced video preview with side-by-side/stacked layout, shared transport, drift correction and chart seeking; it can be used before batch analysis.
- A changed extraction configuration invalidates its fingerprint cache; changed matching settings reuse extraction.
- Identified offsets are relative to the original clips, including a later StickCam calibration reference.
- Manual pairings preserve one-to-one assignment and are reflected in CSV/exports.
- Preview stops at the shared interval's end and uses one selected audio source.
- A saved radio outline supplies a camera-relative orientation transform for stick positions, with tracking quality shown in the preview.
- Suggested regions, neutral centres and current tip points remain editable; dashed tip circles indicate proposals until the operator saves the calibration. The message identifies missing tips that need manual selection.
- A manually outlined radio face constrains region suggestions to that normalized face; weak or implausible pairs fail to manual calibration rather than returning arbitrary background boxes.
- Reused radio references require mutual feature matches, distributed inliers, low reprojection error and a plausible visible outline. Current tip positions require a separate appearance search inside the current gimbal; old tip coordinates are not transferred as current positions. Uncertain cap/template matches remain unmarked.
- Single-target analysis limits extraction and matching to its target plus opposite-side candidates. Audio/date-only fallback remains review evidence and requires a distinctive audio anchor to infer a relative filename-clock offset.
- A chosen Betaflight ACTUAL, legacy, or KISS rate profile changes roll/pitch/yaw setpoint curves before correlation; disabled by default and recorded in results.
- Each export contains only the shared interval, with the same frame count and timeline start for both clips.
- Restarting the web process preserves completed sessions and records interrupted work explicitly.
- Renaming a session updates its saved job record and the Sessions list.
- A failed or excluded input does not silently become a successful pair.

## Next phase: evaluate real flight footage

Recent additions: embedded FPV motion is the automatic first choice for matching and stacked review charts; StickCam gyro is confined to outline-assisted camera stabilisation. Session gyro scans compare already derived StickCam inputs against every FPV clip, cache telemetry and unsupported sources, rank candidates and expose their timing/axis evidence without auto-saving a pair. Extraction has live percentage/ETA, named rate presets live in project YAML, and reviewed pairs have a 9:16 editor/export path. NVIDIA decoding/resizing and shared outline transforms reduce repeated processing; OpenCV optical flow remains on the CPU. Real-footage validation of matching accuracy and portrait exports remains outstanding.

### Staged audio-first matching

Implemented explicit source selection for embedded FPV gyro/attitude, FPV visual motion, StickCam inputs, both audio feeds and filename timestamps. Disabled sources skip extraction and comparison while retaining saved fingerprints. The Audio + timestamps preset requires no StickCam calibration and skips all motion work; Gyro + sticks avoids audio and visual extraction. Both FPV sources selected preserve embedded-first visual fallback. Selections persist with analysis settings, and incomplete audio/motion dependencies are explained before running. Filename clocks remain dependent on an independent anchor; durations only bound overlap. Separate chart review and preparation can still load data on demand.

Automatic staged fallback remains planned: when all methods are selected, boundary audio is still extracted after motion. Change the matcher to first read metadata and cached boundary audio for the selected clips, then shortlist pairs using distinctive shared landmarks, sufficient shared time, candidate separation and filename-clock consistency where available. Accept audio-only proposals only when multiple independent signals agree and the candidate is unambiguous. For unresolved or weak-audio clips, derive or reuse StickCam movement and FPV motion, then apply the existing motion comparison with audio as supporting evidence. Motor-saturated FPV audio, short overlap, repeated noise and microphone-level differences must lead to motion fallback, not a confident audio match. Validate the confidence gates against labelled real pairs and wrong combinations before allowing audio evidence to skip motion extraction automatically.

Prioritise evaluation of embedded telemetry matching, then the calibrated visual rotation fallback and its evaluation gates in [FREESTYLE_MOTION_DESIGN.md](FREESTYLE_MOTION_DESIGN.md). Compare requested versus measured drone body rotation rates, including while inverted; distinguish this from knowing absolute world attitude. Camera uptilt and lens geometry must be explicit. Track adjacent source frames before reducing fingerprint frequency so fast rotations are not lost at the 30 Hz extraction step. Gravity and throttle require independent attitude/translation evidence and a vehicle model; they are supporting work after the rotation path is validated.

This remains a matching feasibility question as well as a software workflow. Synthetic footage proves timestamp, assignment and export mechanics; it cannot establish the relationship between real stick movement and FPV optical motion.

The next evaluation should use 2–5 labelled real pairs and several wrong combinations, including fixed and Wonky StickCam clips. Inspect tip tracking and motion-quality plots first. Then measure whether true pairs have distinct peaks and correct common starts compared with wrong pairs. Include occlusion, blur, motor-dominated audio and unset camera dates.

The current TX16S clip exposed background/hand circles being scored above real housings. Rim evidence now rejects those proposals. Saved-reference matching places the housings on the inspected frame and a later rotated frame, but three other sampled frames still need manual assistance. This is an initial real-footage check, not proof across radio families.

Add a radio-reference catalogue as a separate phase: RadioMaster TX16S generations/case/gimbal variants, Boxer, GX12, HelloRadioSky and Jumper families. Each verified reference needs front/oblique examples, a face outline, both housing landmarks and provenance. Recognise a family only with distributed visual evidence; expose the guessed model and outline for correction, and use its geometry to constrain the housing search. A black TX16S manufacturer photograph failed to produce a reliable transform for the silver TX16S in the current clip, so a single photograph or generic silhouette must not count as model recognition. Operator-confirmed session references provide a useful fallback and can later seed a personal library. Do not infer stick positions from thumb/pinch locations alone.

After that evidence exists, tune scoring and proposal thresholds, assess longer 10–40-pair sessions, and decide whether a trained radio/landmark detector or a stronger motion estimator is justified. Neural flow and a Resolve plugin remain later work.

### Calibration review and display updates

- Motion comparison groups FPV and StickCam motion plots together, followed by the two audio waveforms, on the shared timeline.

### Known flight benchmark: StickCam 0228 / FPV 0008

The user-supplied pair `DJI_20260406162534_0228_D.MP4` / `DJI_20260406161138_0008_D.MP4` has repeated boundary fingerprint agreement: 769 distinct hashes, 15 supported three-second sections, and FPV 5.6s aligned with StickCam 0s. It ranks above 29 other cached FPV audio candidates. Existing stick extraction does not reliably corroborate it. The outline visibly drifts onto grass while reporting high quality; about 63% of samples report high outline quality while the outline extends beyond the frame.

Prioritise capture validity before lowering matching thresholds: absolute appearance anchors and radio reacquisition, perspective correction, physically constrained/distinctive tip detection, explicit occlusion gaps, and verified body-axis mapping. An independent 5 Hz reference-rectification experiment modestly improved the score but retained only 20–23% of stick samples and did not establish a repeated motion match. It is a diagnostic, not a production fix. Measurements, candidate rankings, before/after plots and reproducible scripts are saved in [the pair study](artifacts/pair-validation/0228-0008/README.md). Keep this pair as a known benchmark alongside additional true and wrong combinations.

### Extraction and rates display

- Automatic per-clip/batch calibration searches in five-second steps; review proposals, adjust markers, confirm and advance.
- Display source durations without using duration similarity for matching.
- Default extraction/comparison 30 Hz, minimum overlap two seconds; retain and label caches at older rates.
- Display calculated per-axis full-stick maximum °/s next to the rate inputs.

- Record per-clip StickCam movement derivation time, separate fresh extraction from cache reuse, and surface persisted timings in the preparation table and derivation log.
- Offer half/quarter of each StickCam clip's detected average fps alongside fixed Hz, display the effective rate, and share that rate's cached inputs across preparation, matching and chart review. Preserve other cached rates. FPV extraction/comparison rates remain independent; lower tracking rates trade temporal detail for less vision work.
- Save a human-confirmed session radio appearance/model label and outline. Check each StickCam midpoint (bounded ±5/±10s retries) and calibration frame against that reference; show identity/orientation evidence, allow manual upright/inverted corrections, preserve physical gimbal labels and invert reference-frame stick axes when needed. Flag ambiguous or changing orientation; never infer a model from silhouette alone. Persist checks and keep original source coordinates for calibration; centred preview poses use physical upright corner order.
